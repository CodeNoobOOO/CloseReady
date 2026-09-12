# Deployment Foundation Implementation Plan

**Goal:** Make the CloseReady API and durable worker repeatably deployable and observable on one Lightsail host.

**Architecture:** Build one non-root Python image and run API and worker as separate Compose services sharing an administrator-mounted configuration and persistent SQLite volume. The API exposes data-free liveness and database-backed readiness endpoints.

**Tech Stack:** Python 3.11, FastAPI, SQLAlchemy, Docker, Docker Compose, GitHub Actions.

**Spec:** `docs/superpowers/specs/2026-09-11-deployment-foundation-design.md`

### Task 1: Health contract

- [x] Add failing tests for public liveness, database-backed readiness and safe readiness failure.
- [x] Add a typed health response and a minimal store readiness probe.
- [x] Run the focused and full test suites.

### Task 2: Container topology

- [x] Add a non-root Dockerfile and a narrow `.dockerignore`.
- [x] Add an example Compose configuration with distinct API and worker services, startup ordering, runtime-only secrets and one persistent volume.
- [x] Add static contract tests for deployment files where they protect security or command boundaries.

### Task 3: Continuous integration

- [x] Add a GitHub Actions workflow for unit tests, compilation, dependency validation and image build.
- [x] Keep CI deterministic and free of live provider calls or credentials.

### Task 4: Operator documentation and verification

- [x] Document environment preparation, startup, health, logs, backup, restore, restart and rollback.
- [x] Update implementation status without claiming an actual Lightsail deployment.
- [x] Run the full suite, compilation, dependency and diff checks.
