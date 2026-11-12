# PricePulse CI/CD Guide

## Workflow architecture

`.github/workflows/ci.yml` runs on branch and version-tag pushes, pull requests targeting `main`, and manual dispatch. It installs root `requirements.txt` using Python 3.10, validates app imports, applies the canonical Alembic schema to ephemeral PostgreSQL 16 for database regression and backend tests, runs frontend tests and the Vite production build from the committed lockfile, then validates both Docker builds without publishing. Backend JUnit results are uploaded even on test failure; frontend build output is an Actions artifact.

CI supplies test-only database credentials. It does not load `backend/.env`, use a user's database, call live marketplace sites, or require Ollama/Qwen3:8b. The existing test suite exercises scraper parsing/diagnostics, agent and pricing behavior, retrieval, and mocked LLM interfaces. Chroma storage is redirected to the runner temporary directory via `CHROMA_PERSIST_DIRECTORY`; local operation retains its existing repository `chroma_db` default. Tests report under the combined Backend tests job.

The legacy Alembic graph is retained for existing installations; it is not the fresh-install path. The canonical baseline is frozen independently from ORM metadata, and CI compares its ten-table contract plus adoption behavior against the independent schema reference. Populated databases must use the read-only preflight and explicit adoption process; missing observation provenance blocks adoption.

## Execution sequence

The canonical PostgreSQL regression, backend, frontend, and Docker validation jobs run independently. PostgreSQL jobs wait for service health before migration and tests. Frontend runs `npm ci`, `npm test`, then `npm run build`. Docker builds both images without a registry push.

`release.yml` starts only after CI completes successfully for a push whose ref begins with `v`. Its verify job confirms that the pushed tag still points at the successful CI commit, then repeats backend, migration, frontend, and image checks before creating a versioned archive with frontend assets, backend/frontend Docker archives, build metadata (version, commit SHA, run ID), and a SHA-256 checksum. It attaches these files to a GitHub Release with generated notes. It does not deploy or publish to an external registry.

## Local verification commands

Run from PowerShell. The root `venv` is developer-only; GitHub Actions creates an isolated Python environment from `requirements.txt`.

```powershell
$env:DATABASE_URL = "postgresql+psycopg2://<user>:<password>@localhost:5432/<database>"
$env:SECRET_KEY = "<local-random-secret>"
python -m pip install -r requirements.txt
Set-Location backend
python -c "from app.main import app; print(app.title)"
alembic -c alembic_canonical.ini upgrade head
pytest -ra
Set-Location ..\frontend
npm ci
npm test
npm run build
Set-Location ..
docker compose config
docker compose build
```

Use a disposable database for migration checks; never use production. Compose requires `POSTGRES_PASSWORD` and `SECRET_KEY` in the process environment (set them with `$env:POSTGRES_PASSWORD = "<local-password>"` and `$env:SECRET_KEY = "<local-random-secret>"`). PostgreSQL and Chroma use persistent named volumes. Apply migrations explicitly before using the app: `docker compose run --rm backend alembic -c alembic_canonical.ini upgrade head`.

## GitHub setup

1. Ensure Actions are enabled and `main` is the default branch.
2. Commit the workflow/container files and open a PR to `main`.
3. Optionally protect `main` by requiring `Backend tests (pytest)`, `Frontend tests and production build`, and `Docker build validation`.
4. No Actions secrets are required for CI or release artifacts. Runtime credentials (database password, `SECRET_KEY`, optional SMTP credentials, and future hosting credentials) belong in the deployment target's secret store, not repository files or logs.

## Release procedure

1. Merge reviewed changes to the release branch.
2. Create and push a semantic version tag, e.g. `git tag -a v1.0.0 -m "PricePulse v1.0.0"` then `git push origin v1.0.0`.
3. Wait for CI to pass, then confirm the downstream Release workflow's verify job passes.
4. Download the GitHub Release bundle and verify with `sha256sum -c pricepulse-v1.0.0.tar.gz.sha256`.

Tag pushes start both CI and Release. Release repeats required checks because Actions has no implicit dependency between separately triggered workflows.

## Troubleshooting

- **Backend import fails:** Set `DATABASE_URL` and `SECRET_KEY`; CI supplies test-only values.
- **Postgres health check fails:** Check service logs, port conflicts, and test credentials. Never substitute a production URL.
- **Migration fails:** Inspect the failing revision/graph and validate against disposable PostgreSQL; CI does not alter production schemas.
- **Unexpected Chroma directory:** Set `CHROMA_PERSIST_DIRECTORY`; local default remains repo `chroma_db`.
- **Ollama test fails:** Mock service boundaries for standard CI. An optional live integration run must explicitly configure its endpoint and model outside normal CI.
- **`npm ci` fails:** Check consistency between `package.json` and the committed lockfile; avoid unnecessary regeneration.
- **Docker build fails:** Run `docker compose build` and inspect dependency errors. The backend image installs the Chromium runtime for its pinned Playwright package. Build contexts exclude `.env`, venv, node_modules, tests, and Chroma data.
- **Release artifact missing:** Check the `v*` ref, contents write permission, and successful verify job.

## Deployment prerequisites

Production hosting has not been selected. Before automatic deployment, choose a target and define its environment, registry if needed, credentials, health checks, migration and rollback policy, public frontend API URL/CORS origins, persistent PostgreSQL and Chroma storage, and a reachable Ollama service/model. Add narrowly scoped environment secrets after selecting the target. This pipeline does not deploy to production.
