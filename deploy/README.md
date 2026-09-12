# Single-host deployment runbook

This runbook packages the CloseReady API and durable worker for one AWS Lightsail Linux instance using Docker Compose. It is an assessment topology, not evidence that the repository has already been deployed. Both services use the same local Docker volume and must remain on one host while the application uses SQLite.

## Host prerequisites

- A supported Linux Lightsail instance with Docker Engine and Docker Compose v2.
- A firewall that exposes only SSH and the chosen HTTPS entry point. Do not expose SQLite, access files or the worker.
- TLS termination through a configured reverse proxy or load balancer before external assessment traffic.
- Enough disk space for the database, backups and container logs.

Clone the repository and deploy an identified commit or release tag. Do not deploy an unreviewed working tree.

## Runtime configuration

From the repository root, copy `deploy/runtime.env.example` to `deploy/runtime.env`, then fill the approved provider configuration. This ignored file contains the LLM key and must be readable only by the deployment administrator.

Create `deploy/secrets/server-config.json` from the documented access-config schema. Store only SHA-256 hashes of independently generated high-entropy bearer tokens; never put the bearer tokens themselves in this file. The committed `examples/server-config.json` is synthetic and cannot authenticate a real user.

The runtime environment file is read by Docker and remains administrator-owned. The access file is read inside the container by the fixed runtime UID/GID `10001:10001`; assign it to that identity before startup:

```sh
chmod 600 deploy/runtime.env
sudo chown 10001:10001 deploy/secrets/server-config.json
sudo chmod 400 deploy/secrets/server-config.json
```

Validate the Compose structure without printing resolved environment values:

```sh
docker compose -f deploy/compose.yaml config --quiet
```

## Start and verify

Build and start both services:

```sh
docker compose -f deploy/compose.yaml up --detach --build
docker compose -f deploy/compose.yaml ps
curl --fail --silent http://127.0.0.1:8000/health/live
curl --fail --silent http://127.0.0.1:8000/health/ready
```

Expected responses are `{"status":"ok"}` and `{"status":"ready"}`. Readiness checks all application tables and performs a rollback-only write probe, but never calls the LLM. Business routes remain bearer-authenticated.

Inspect bounded logs without printing environment configuration:

```sh
docker compose -f deploy/compose.yaml logs --tail 200 api worker
```

The API container is healthy only after persistent storage is readable. The worker waits for that health check, then resumes queued work. Expired in-progress inference is routed to human review instead of being silently replayed.

## Backup

SQLite's backup API can create a transactionally consistent backup while the services are running. First create the backup inside the persistent volume:

```sh
docker compose -f deploy/compose.yaml exec -T api python -c "import sqlite3; source=sqlite3.connect('/data/closeready.db'); target=sqlite3.connect('/data/closeready-backup.db'); source.backup(target); target.close(); source.close()"
mkdir -p backups
docker compose -f deploy/compose.yaml cp api:/data/closeready-backup.db ./backups/closeready-backup.db
chmod 600 ./backups/closeready-backup.db
```

Copy backups to an approved encrypted location with defined retention. The repository and container image are not backup destinations. Test restoration before relying on a backup.

## Restore drill

Restoration replaces business state, so schedule downtime and retain the existing database until verification succeeds.

Stage a copy for the fixed container identity. `deploy/restore` is excluded from both Git and the image build context:

```sh
mkdir -p deploy/restore
cp ./backups/closeready-backup.db deploy/restore/closeready-backup.db
sudo chown -R 10001:10001 deploy/restore
sudo chmod 500 deploy/restore
sudo chmod 400 deploy/restore/closeready-backup.db
docker compose -f deploy/compose.yaml stop worker api
docker compose -f deploy/compose.yaml run --rm --no-deps -v "$(pwd)/deploy/restore:/restore:ro" api python -c "import sqlite3; source=sqlite3.connect('file:/restore/closeready-backup.db?mode=ro', uri=True); target=sqlite3.connect('/data/closeready.db'); source.backup(target); target.close(); source.close()"
docker compose -f deploy/compose.yaml up --detach
curl --fail --silent http://127.0.0.1:8000/health/ready
```

Verify a known synthetic or authorised case and its audit records after restoration. Keep the pre-restore volume snapshot until that verification passes, then securely remove the restore staging copy.

## Upgrade and rollback

Back up the database, fetch the reviewed commit, rebuild, and wait for readiness. This code supports schema version 1 only; stop before deployment if a future release requires an unsupported schema migration.

For application rollback, check out the previous reviewed commit and rebuild the services. Do not roll the database backward unless the release changed its schema and provides an explicit migration/rollback procedure.

## Current production boundary

The containers run as a non-root user with a read-only root filesystem, dropped Linux capabilities and a writable data volume. Provider keys and access configuration are mounted at runtime and excluded from the image build context. Public exposure still requires TLS, firewall rules, host patching, secret rotation, encrypted off-host backups and monitoring configured in the actual Lightsail environment.
