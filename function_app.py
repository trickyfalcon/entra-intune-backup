import logging
import os
import json
import time
import datetime
import requests
import azure.functions as func
from azure.identity import DefaultAzureCredential, CertificateCredential
from azure.storage.blob import BlobServiceClient
from azure.keyvault.secrets import SecretClient

# Azure Function App Declaration
app = func.FunctionApp()

# ==========================================
# CONFIGURATION
# ==========================================
TENANT_ID = os.environ.get("TENANT_ID")
CLIENT_ID = os.environ.get("CLIENT_ID")
KEY_VAULT_NAME = os.environ.get("KEY_VAULT_NAME")
BACKUP_STORAGE_ACCOUNT = os.environ.get("BACKUP_STORAGE_ACCOUNT")
BACKUP_CONTAINER = "entra-backups"

GRAPH_API_BASE = "https://graph.microsoft.com/v1.0"
GRAPH_API_BETA = "https://graph.microsoft.com/beta"
DATE_STR = datetime.datetime.now().strftime("%Y-%m-%d")

# Resource List
RESOURCES = {
    "Entra_Users": ("/users?$top=100", "v1.0"),
    "Entra_Groups": ("/groups?$top=100", "v1.0"),
    "Entra_Applications": ("/applications?$top=100", "v1.0"),
    "Entra_ConditionalAccess": ("/identity/conditionalAccess/policies", "v1.0"),
    "Intune_DeviceConfigs_Legacy": ("/deviceManagement/deviceConfigurations?$top=100", "beta"),
    "Intune_SettingsCatalog": ("/deviceManagement/configurationPolicies?$top=100", "beta"),
    "Intune_AdminTemplates": ("/deviceManagement/groupPolicyConfigurations?$expand=definitionValues&$top=100", "beta"),
    "Intune_EndpointSecurity_Intents": ("/deviceManagement/intents?$expand=settings&$top=100", "beta"),
    "Intune_WindowsUpdateRings": ("/deviceManagement/deviceConfigurations?$filter=contains(bitAnd(prop_id, 1), 1)&$top=100", "beta"),
    "Intune_CompliancePolicies": ("/deviceManagement/deviceCompliancePolicies?$top=100", "v1.0"),
    "Intune_WindowsAutopilot": ("/deviceManagement/windowsAutopilotDeviceIdentities?$top=100", "v1.0"),
    "Intune_MobileApps": ("/deviceAppManagement/mobileApps?$top=100", "v1.0"),
    "Intune_Scripts": ("/deviceManagement/deviceManagementScripts?$top=100", "beta"),
    "Intune_ShellScripts": ("/deviceManagement/deviceShellScripts?$top=100", "beta"), 
}


class AzureExporter:
    def __init__(self):
        logging.info(f"Initializing Azure Premium Export: {DATE_STR}")
        
        # Use Managed Identity for Azure resources
        self.azure_credential = DefaultAzureCredential()
        
        # 1. Setup Storage using Managed Identity
        try:
            storage_url = f"https://{BACKUP_STORAGE_ACCOUNT}.blob.core.windows.net"
            self.blob_service_client = BlobServiceClient(
                account_url=storage_url,
                credential=self.azure_credential
            )
            self.container_client = self.blob_service_client.get_container_client(BACKUP_CONTAINER)
            if not self.container_client.exists():
                self.container_client.create_container()
            logging.info("Storage connection successful (Managed Identity)")
        except Exception as e:
            logging.error(f"FATAL: Storage Connection Failed. {e}")
            raise e

        # 2. Get Certificate from Key Vault
        try:
            keyvault_url = f"https://{KEY_VAULT_NAME}.vault.azure.net"
            secret_client = SecretClient(vault_url=keyvault_url, credential=self.azure_credential)
            
            # Get certificate (stored as secret for full cert including private key)
            cert_secret = secret_client.get_secret("entra-backup-cert")
            cert_value = cert_secret.value
            
            # Write cert to temp file for CertificateCredential
            cert_path = "/tmp/temp_cert.pem"
            with open(cert_path, "w") as f:
                f.write(cert_value)
            
            logging.info("Certificate retrieved from Key Vault")
        except Exception as e:
            logging.error(f"FATAL: Key Vault access failed. {e}")
            raise e

        # 3. Setup Graph API Auth using Certificate
        try:
            self.graph_credential = CertificateCredential(
                tenant_id=TENANT_ID,
                client_id=CLIENT_ID,
                certificate_path=cert_path
            )
            self.token = self.get_token()
            logging.info("Graph API Authentication Successful")
        except Exception as e:
            logging.error(f"FATAL: Graph Auth Failed. {e}")
            raise e
        finally:
            # Clean up temp cert file
            if os.path.exists(cert_path):
                os.remove(cert_path)

        self.headers = {"Authorization": f"Bearer {self.token}", "Content-Type": "application/json"}

    def get_token(self):
        return self.graph_credential.get_token("https://graph.microsoft.com/.default").token

    def make_request(self, url):
        retries = 3
        while retries > 0:
            try:
                resp = requests.get(url, headers=self.headers)
                if resp.status_code == 200:
                    return resp
                elif resp.status_code in [403, 400, 404]:
                    logging.warning(f"API Error {resp.status_code}: {url}")
                    return None
                elif resp.status_code == 429:
                    time.sleep(int(resp.headers.get("Retry-After", 10)))
                    retries -= 1
                    continue
                else:
                    logging.warning(f"Unexpected status {resp.status_code}: {url}")
                    return None
            except Exception as e:
                logging.error(f"Connection Error: {e}")
                retries -= 1
        return None

    def save_item(self, category, item):
        raw_name = item.get("displayName") or item.get("name") or item.get("id") or "unknown"
        item_id = item.get("id", "noid")
        safe_name = "".join(c for c in raw_name if c.isalnum() or c in (' ', '.', '_', '-')).strip()[:60]
        file_name = f"{safe_name} ({item_id}).json"
        
        blob_path = f"{DATE_STR}/{category}/{file_name}"
        
        try:
            blob_client = self.container_client.get_blob_client(blob_path)
            blob_client.upload_blob(json.dumps(item, indent=4), overwrite=True)
        except Exception as e:
            logging.error(f"Blob Upload failed: {e}")

    def fetch_all_pages(self, url):
        current_url = url
        while current_url:
            resp = self.make_request(current_url)
            if not resp:
                break
            data = resp.json()
            if "value" in data:
                for item in data["value"]:
                    yield item
            else:
                yield data
                break
            current_url = data.get("@odata.nextLink")

    def fetch_baselines(self):
        logging.info("--> Exporting Security Baselines...")
        templates_url = f"{GRAPH_API_BETA}/deviceManagement/templates?$top=100"
        for template in self.fetch_all_pages(templates_url):
            temp_id = template.get("id")
            temp_name = template.get("displayName")
            instances_url = f"{GRAPH_API_BETA}/deviceManagement/templates/{temp_id}/migratableInstances?$expand=settings"
            for instance in self.fetch_all_pages(instances_url):
                instance["_SourceTemplate"] = temp_name 
                self.save_item("Intune_SecurityBaselines", instance)

    def run(self):
        for name, (endpoint, version) in RESOURCES.items():
            logging.info(f"--> Exporting {name}...")
            base_url = GRAPH_API_BETA if version == "beta" else GRAPH_API_BASE
            full_url = f"{base_url}{endpoint}" if not endpoint.startswith("http") else endpoint
            for item in self.fetch_all_pages(full_url):
                self.save_item(name, item)
        self.fetch_baselines()
        logging.info("Backup completed successfully")


# Timer Trigger: Runs at 5:00 AM UTC daily
@app.schedule(schedule="0 0 5 * * *", arg_name="myTimer", run_on_startup=False, use_monitor=False) 
def daily_backup_timer(myTimer: func.TimerRequest) -> None:
    logging.info('Backup Timer Triggered')
    exporter = AzureExporter()
    exporter.run()
    logging.info('Backup Completed')
