# Deployment Foundation Design

## Goal

Package the implemented CloseReady API and durable worker as a repeatable, observable deployment suitable for an AWS Lightsail single-instance assessment environment.

## Scope

This is a Student 1 increment. It adds unauthenticated, data-free health endpoints, one container image with separate API and worker commands, a single-host Compose topology, continuous integration, and an operator runbook. It does not deploy an AWS resource, send mail, accept client documents, or build the dashboard.

## Runtime topology

- One immutable application image runs as a non-root user.
- The API and worker run as separate supervised containers from that image.
- Both processes use the same administrator-supplied access configuration, LLM configuration and file-backed SQLite database on a persistent local volume.
- The worker starts only after the API has initialized the database and passed readiness.
- This topology is limited to one Lightsail host. A future multi-host deployment must replace SQLite with a managed transactional database and migrations.

## Health contract

- `GET /health/live` returns `200 {"status":"ok"}` when the API process can serve requests.
- `GET /health/ready` verifies all core/runtime tables and performs a rollback-only write probe. It returns `200 {"status":"ready"}` only when persistent storage can serve application work.
- Health endpoints require no bearer token so the container supervisor can call them. They expose no client, provider, credential, filesystem or exception detail.
- Readiness does not call the LLM and therefore cannot consume provider credit.

## Security and operations

- Secrets and access configuration are supplied at runtime and excluded from the image.
- The service binds port 8000 inside the container; public TLS termination and firewall policy remain infrastructure responsibilities.
- The image uses fixed non-root UID/GID `10001:10001` and writes only to the mounted data volume. The administrator assigns the read-only access file to that identity before startup.
- The runbook includes startup, health verification, logs, restart behavior, backup, restore and rollback boundaries.

## Continuous integration

Every pull request and push to `main` installs the locked requirements, runs all unit tests, compiles Python sources, checks installed dependencies and builds the container image. CI uses no live credentials and performs no LLM call.

## Acceptance

- Health responses and storage failure behavior are covered by automated tests.
- The full existing suite remains green.
- The container definition has a non-root runtime, a health check and no committed credentials.
- The Compose file defines separate API and worker services sharing one persistent data volume.
- Documentation states the single-host SQLite limitation and does not claim that deployment has occurred.
