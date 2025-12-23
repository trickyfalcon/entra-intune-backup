# Entra ID & Intune Backup Function - Deployment Guide

This guide walks through deploying an Azure Function that automatically backs up Entra ID and Intune configurations daily.

## What Gets Backed Up

| Category | Resources |
|----------|-----------|
| **Entra ID** | Users, Groups, Applications, Conditional Access Policies |
| **Intune** | Device Configurations, Settings Catalog, Admin Templates, Endpoint Security, Compliance Policies, Autopilot, Mobile Apps, Scripts, Security Baselines |

---

## Prerequisites

- Azure CLI installed and logged in (`az login`)
- Azure Functions Core Tools v4 (`npm install -g azure-functions-core-tools@4`)
- Python 3.11
- OpenSSL (for certificate generation)
- Appropriate Azure permissions (Subscription Contributor, Entra ID Global Admin or Application Admin)

---

## Project Structure

```
Azurebackup/
├── function_app.py          # Main function code
├── full_cert.pem            # Certificate (private + public key)
├── requirements.txt         # s
├── host.json                # Function host configuration
└── local.settings.json      # Local development settings (not deployed)
```

### requirements.txt

```
azure-functions
azure-identity
azure-storage-blob
requests
```

### host.json

```json
{
  "version": "2.0",
  "logging": {
    "applicationInsights": {
      "samplingSettings": {
        "isEnabled": true,
        "excludedTypes": "Request"
      }
    }
  },
  "extensionBundle": {
    "id": "Microsoft.Azure.Functions.ExtensionBundle",
    "version": "[4.*, 5.0.0)"
  },
  "functionTimeout": "01:00:00"
}
```

### local.settings.json

```json
{
  "IsEncrypted": false,
  "Values": {
    "AzureWebJobsStorage": "",
    "FUNCTIONS_WORKER_RUNTIME": "python"
  }
}
```

---

## Step 1: Set Variables

```bash
RESOURCE_GROUP="rg-entra-backup"
LOCATION="eastus"
STORAGE_ACCOUNT="stentrabackup$(openssl rand -hex 4)"
FUNC_STORAGE="stfuncbackup$(openssl rand -hex 4)"
FUNCTION_APP="func-entra-backup-$(openssl rand -hex 4)"

echo "Storage Account: $UNT"
echo "Function Storage: $FUNC_STORAGE"
echo "Function App: $FUNCTION_APP"
```

> **Important:** Save these values for later reference.

---

## Step 2: Create Azure Resources

### Resource Group

```bash
az group create --name $RESOURCE_GROUP --location $LOCATION
```

### Storage Account for Backups

```bash
az storage account create \
  --name $STORAGE_ACCOUNT \
  --resource-group $RESOURCE_GROUP \
  --location $LOCATION \
  --sku Standard_LRS

az storage container create \
  --name entra-backups \
  --account-name $STORAGE_ACCOUNT
```

### Storage Account for Function App

```bash
az storage account create \
  --name $FUNC_STORAGE \
  --resource-group $RESOURCE_GROUP \
  --location $LOCATION \
  --sku Standard_LRS
```

### Function App (Premium Plan)

```bash
# Create Premium plan (required for 1-hour timeout)
az functionapp plan create \
  --name "plan-entra-backup" \
  --resource-group $RESOURCE_GROUP \
  --location $LOCATION \
  --sku EP1 \
  --is-linux

# Create Function App
az functionapp create \
  --name $FUNCTION_APP \
  --resource-group $RESOURCE_GROUP \
  --storage-account $FUNC_STORAGE \
  --plan "plan-entra-backup" \
  --runtime python \
  --runtime-version 3.11 \
  --functions-version 4 \
  --os-type Linux
```

---

## Step 3: Create App Registration & Certificate

### Create App Registration

```bash
APP_ID=$(az ad app create \
  --display-name "Entra-Intune-Backup-Service" \
  --query appId -o tsv)

echo "CLIENT_ID: $APP_ID"

# Create Service Principal
az ad sp create --id $APP_ID
```

### Generate Certificate

```bash
# Generate self-signed certificate (valid for 1 year)
openssl req -x509 -nodes -days 365 \
  -newkey rsa:2048 \
  -keyout private.pem \
  -out public.pem \
  -subj "/CN=EntraBackupCert"

# Combine private + public into full_cert.pem (used by the function)
cat private.pem public.pem > full_cert.pem

# Upload public certificate to App Registration
az ad app credential reset \
  --id $APP_ID \
  --cert @public.pem \
  --append
```

> **Important:** Copy `full_cert.pem` to your project directory before deploying.

---

## Step 4: Grant Graph API Permissions

### Via Azure Portal

1. Go to **Azure Portal** → **Microsoft Entra ID** → **App registrations**
2. Select **Entra-Intune-Backup-Service**
3. Click **API permissions** → **Add a permission** → **Microsoft Graph** → **Application permissions**
4. Add the following permissions:

| Permission | Purpose |
|------------|---------|
| `User.Read.All` | Backup users |
| `Group.Read.All` | Backup groups |
| `Application.Read.All` | Backup app registrations |
| `Policy.Read.All` | Backup Conditional Access policies |
| `DeviceManagementConfiguration.Read.All` | Backup Intune device configurations |
| `DeviceManagementApps.Read.All` | Backup Intune apps |
| `DeviceManagementManagedDevices.Read.All` | Backup Autopilot devices |

5. Click **Grant admin consent for [Your Tenant]**

---

## Step 5: Configure Function App Settings

```bash
# Get Tenant ID
TENANT_ID=$(az account show --query tenantId -o tsv)

# Get Backup Storage CString
BACKUP_CONN=$(az storage account show-connection-string \
  --name $STORAGE_ACCOUNT \
  --resource-group $RESOURCE_GROUP \
  --query connectionString -o tsv)

# Set App Settings
az functionapp config appsettings set \
  --name $FUNCTION_APP \
  --resource-group $RESOURCE_GROUP \
  --settings \
    "TENANT_ID=$TENANT_ID" \
    "CLIENT_ID=$APP_ID" \
    "BACKUP_STORAGE_CONNECTION_STRING=$BACKUP_CONN"
```

### Verify Settings

```bash
az functionapp config appsettings list \
  --name $FUNCTION_APP \
  --resource-group $RESOURCE_GROUP \
  --query "[?name=='TENANT_ID' || name=='CLIENT_ID' || name=='BACKUP_STORAGE_CONNECTION_STRING'].{name:name, value:value}" \
  -o table
```

---

## Step 6: Add CORS for Portal Testing

```bash
az functionapp cors add \
  --name $FUNCTION_APP \
  --resource-group $RESOURCE_GROUP \
  --allowed-origins "https://portal.azure.com"
```

---

## Step 7: Deploy the Function

### Ensure Project Structure

```bash
cd /path/to/your/project

# Verify files exist
ls -la function_app.py full_cert.pem requirements.txt host.json

# Verify Python syntax
python3 -m py_compile function_app.py && echo "Syntax OK"
```

### Deploy

```bash
func azure functionapp publish $FUNCTION_APP
```

### Verify Deployment

```bash
# Wait for function to register
sleep 30

# Check function is listed
az functionapp function list \
  --name $FUNCTION_APP \
  --resource-group $RESOURCE_GROUP \
  -o table
```

If the function doesn't appear, restart the app:

```bash
az functionapp restart \
  --name $FUNCTION_APP \
  --resource-group $RESOURCE_GROUP

sleep 30

az functionapp function list \
  --name $FUNCTION_APP \
  --resource-group $RESOURCE_GROUP \
  -o table
```

---

## Step 8: Test the Function

### Manual Trigger via CLI

```bash
# Get master key
MASTER_KEY=$(az functionapp keys list \
  --name $FUNCTION_APP \
  --resource-group $RESOURCE_GROUP \
  --query "masterKey" -o tsv)

# Trigger the function
curl -X POST \
  "https://$FUNCTION_APP.azurewebsites.net/admin/functions/daily_backup_timer" \
  -H "x-functions-key: $MASTER_KEY" \
  -H "Content-Type: application/json" \
  -d '{}'
```

### Verify Backups Created

```bash
# Get connection string
BACKUP_CONN=$(az storage account show-connection-string \
  --name $STORAGE_ACCOUNT \
  --resource-group $RESOURCE_GROUP \
  --query connectionString -o tsv)

# List blobs
az storage blob list \
  --container-name "entra-backups" \
  --connection-string "$BACKUP_CONN" \
  -o table
```

Or check in the Azure Portal:
1. Go to **Storage Account** → **Containers** → **entra-backups**
2. You should see folders organized by date (e.g., `2025-12-22/`)

---

## Monitoring

### Application Insights

1. Go to **Azure Portal** → **Application Insights** → **[Your Function App]**
2. Click* and run:

```kusto
traces
| where timestamp > ago(1h)
| order by timestamp desc
```

### Function Monitor

1. Go to **Function App** → **Functions** → **daily_backup_timer** → **Monitor**
2. View invocation history and logs

---

## Schedule

The function runs automatically at **5:00 AM UTC daily**.

Cron expression: `0 0 5 * * *`

To change the schedule, modify this line in `function_app.py`:

```python
@app.schedule(schedule="0 0 5 * * *", arg_name="myTimer", run_on_startup=False, uitor=False)
```

---

## Backup Structure

Backups are organized in the storage container as:

```
entra-backups/
└── 2025-12-22/
    ├── Entra_Users/
    │   ├── John Doe (abc123).json
    │   └── Jane Smith (def456).json
    ├── Entra_Groups/
    ├── Entra_Applications/
    ├── Entrigs_Legacy/
    ├── Intune_SettingsCatalog/
    ├── Intune_CompliancePolicies/
    └── ...
```

---

## Troubleshooting

### Function Not Listed After Deployment

```bash
az functionapp restart --name $FUNCTION_APP --resource-group $RESOURCE_GROUP
sleep 30
az functionapp function list --name $FUNCTION_APP --resource-group $RESOURCE_GROUP -o table
`tion Errors

- Verify certificate is in the project root directory
- Ensure certificate is uploaded to App Registration
- Check that CLIENT_ID and TENANT_ID are correct in App Settings

### Permission Errors

- Verify all Graph API permissions are granted
- Ensure **admin consent** was clicked
- Check the App Registration has a Service Principal created

### Portal CORS Error

```bash
az functionapp cors add \
  --name $FUNCTION_APP \
  --resource-group $RESOURCE_GROUP \
  --allowed-origins "https://portal.azure.com"
```

---

## Security Recommendations

1. **Use Key Vault for Certificate Storage** - Instead of deploying the certificate with the code, store it in Azure Key Vault and reference it at runtime.

2. **Enable Managed Identity** - Consider using Managed Identity instead of App Registration for Azure resource access.

3. **Restrict Network Access** - Configure Private Endpoints or IP restrictions for the Function App and Storage Account.

4. **Enable Soft Delete** - Enable soft delete on the storage account to protect against accidental deletion.

5. **Set Retention Policy** - Configure lifecycle management to automatically delete old backups.

---

## Resource Summary

| Resource | Purpose |
|----------|---------|
| `rg-entra-backup` | Resource Group |
| `stentrabackupXXXX` | Storage for backup files |
| `stfuncbackupXXXX` | Storage for Function App |
| `plan-entra-backup` | Premium App Service Plan (EP1) |
| `func-entra-backup-XXXX` | Function App |
| `Entra-Intune-Backup-Service` | App Registration |

---

## Cost Estimate

| Resource | Approximate Monthly Cost |
|----------|-------------------------|
| Premium Function Plan (EP1) | ~$150-180 |
| Storage (depends on data size) | ~$5-20 |
| Application Insights | ~$2-10 |

> **Tip:** If backups complete in under 10 minutes, consider using a Consumption plan instead of Premium to reduce costs.
