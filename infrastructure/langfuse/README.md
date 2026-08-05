# Local Langfuse

This directory uses the official Langfuse v3 Docker Compose pinned from
`langfuse/langfuse@f9a65d91c3293aa4fa5e3b3aabc975342f442cab`.
Only host port bindings were adapted to avoid conflicts with the ERP stack and
to keep every service on `127.0.0.1`.

## Start

Docker Desktop is required. From the project root:

```powershell
powershell -ExecutionPolicy Bypass -File infrastructure\langfuse\start.ps1
```

The script generates local secrets once, bootstraps one organization/project,
and configures the ERP backend exporter. Open <http://127.0.0.1:3000> after the
containers are healthy.

View the local administrator login and project API keys:

```powershell
powershell -ExecutionPolicy Bypass -File infrastructure\langfuse\show-credentials.ps1
```

The public key starts with `pk-lf-`; the secret key starts with `sk-lf-`.
Both are created locally and are not obtained from Langfuse Cloud.

On a managed company computer where Docker is prohibited, do not run this stack.
Deploy the same directory on an approved internal Linux VM or container platform,
then set the ERP backend's `LANGFUSE_BASE_URL`, public key, and secret key to that
internal project. Disable local export without disabling SQL trace persistence:

```powershell
powershell -ExecutionPolicy Bypass -File infrastructure\langfuse\disable-erp-export.ps1
```

## Langfuse Cloud

Create a Langfuse Cloud project and generate a project public/secret key pair.
Configure the ERP backend without printing the secret key:

```powershell
# EU data region (default)
powershell -ExecutionPolicy Bypass -File infrastructure\langfuse\configure-cloud.ps1

# Use `-Region us` for a project created in the US data region.
powershell -ExecutionPolicy Bypass -File infrastructure\langfuse\verify-cloud.ps1
```

The verification event contains only the fixed name
`erp-cloud-connectivity-check`; it does not include prompts, answers, document
chunks, identities, tenant/organization values, or business identifiers.

## Data boundary

- Web UI: `127.0.0.1:3000`
- PostgreSQL: `127.0.0.1:55432`
- ClickHouse HTTP/native: `127.0.0.1:18123` / `127.0.0.1:19000`
- Redis: `127.0.0.1:16379`
- MinIO API/console: `127.0.0.1:9090` / `127.0.0.1:9091`
- Self-host telemetry is disabled.
- ERP exports only the allowlisted, redacted metrics implemented in the backend;
  business prompts, answers, document chunks, order numbers, and identity fields
  are not exported.

Stop without deleting data:

```powershell
docker compose --env-file infrastructure\langfuse\.env `
  -f infrastructure\langfuse\compose.yaml stop
```
