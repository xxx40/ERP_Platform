# Local infrastructure

This stack provides PostgreSQL with pgvector and MinIO for local development.
It binds services to `127.0.0.1`; it is not an internet-facing production setup.

If Docker Desktop is not installed, open PowerShell as Administrator and run:

```powershell
Set-Location <项目目录>
powershell -ExecutionPolicy Bypass -File infrastructure\install-docker.ps1
```

Restart Windows if the installer enables WSL2 or requests a restart. Start Docker
Desktop once and wait until its engine reports that it is running.

```powershell
docker compose --env-file infrastructure/.env `
  -f infrastructure/compose.yaml up -d
```

Verify services:

```powershell
docker compose --env-file infrastructure/.env `
  -f infrastructure/compose.yaml ps
docker compose --env-file infrastructure/.env `
  -f infrastructure/compose.yaml exec postgres `
  psql -U erp_assistant -d erp_assistant -c "SELECT extversion FROM pg_extension WHERE extname = 'vector';"
```

MinIO API: <http://127.0.0.1:9000>

MinIO console: <http://127.0.0.1:9001>

Stop containers without deleting data:

```powershell
docker compose --env-file infrastructure/.env `
  -f infrastructure/compose.yaml stop
```

Do not use `down -v` unless local PostgreSQL and MinIO data should be deleted.
