# PricePulse deployment guide

This guide describes configuration for the existing app. It does not provision services or claim a live deployment.

## Architecture

The root [`vercel.json`](../vercel.json) configures one Vercel Services project:

- `frontend` builds the Vite app from `frontend/`.
- `backend` runs the existing Docker image with service root `.` and entrypoint `backend/Dockerfile`.

Set the Vercel project's framework to **Services**. Backend rewrites cover `/api`, `/api/*`, `/auth`, `/auth/*`, `/health`, `/docs`, `/docs/*`, `/openapi.json`, and `/redoc`; the frontend catch-all follows those routes. The backend Docker build context must remain the repository root because the Dockerfile installs root `requirements.txt` and copies `backend/`.

The backend image uses Python 3.10 on Debian Bookworm via the existing Dockerfile, installs the root requirements and Playwright Chromium, and runs:

```sh
uvicorn app.main:app --host 0.0.0.0 --port ${PORT:-8000}
```

The Dockerfile defaults `PORT` to `8000`. Vercel's container routing defaults to port `80`, so configure the Vercel backend container routing `PORT` to `8000` to match the process listener. The image's `EXPOSE` line alone does not set Vercel's routing port.

## Required backend variables

Set these in the Vercel backend service environment. Keep secrets out of source control and frontend build variables.

| Variable | Purpose |
| --- | --- |
| `DATABASE_URL` | Connection URL supplied by the separately hosted PostgreSQL provider. The backend creates its SQLAlchemy engine during import. |
| `SECRET_KEY` | Private key used to sign and verify authentication tokens. |

PostgreSQL is external to Vercel. Configure its connection URL as the backend's `DATABASE_URL`; do not put it in the frontend environment. The app does not run database migrations at startup.

## Frontend API configuration

For this single-domain setup, leave `VITE_API_URL` unset. The frontend uses same-origin `/auth/...` and `/api/...` paths, and the root Vercel rewrites route those requests to the backend. The Vite development fallback remains `http://localhost:8000`.

For a separate frontend/backend deployment, `VITE_API_URL` may be set to the backend origin at frontend build time. Do not append `/api`: application calls already include their `/api/...` path. `VITE_API_URL` is public in the built frontend and must not contain credentials.

## Optional and feature-specific backend variables

Set these in the Vercel backend environment only when the corresponding behavior needs them or when overriding defaults.

| Variable(s) | Purpose and deployment guidance |
| --- | --- |
| `CORS_ORIGINS` | Comma-separated allowed browser origins. It is only needed for cross-origin browser access; the current same-origin Vercel rewrites do not require CORS for frontend API calls. |
| `FRONTEND_BASE_URL` | Frontend origin inserted into password-reset links. Set it to the deployed frontend origin when email delivery is enabled. |
| `FRONTEND_URL` | Compatibility fallback used only when `FRONTEND_BASE_URL` is unset; prefer `FRONTEND_BASE_URL`. |
| `SMTP_HOST`, `SMTP_PORT`, `SMTP_USERNAME`, `SMTP_PASSWORD`, `SMTP_FROM_EMAIL`, `SMTP_USE_SSL`, `SMTP_USE_TLS` | SMTP configuration for password-reset email delivery. Email delivery is enabled only when `SMTP_HOST`, `SMTP_USERNAME`, `SMTP_PASSWORD`, and `SMTP_FROM_EMAIL` are all configured. Port and TLS/SSL options have defaults; use values appropriate to the SMTP provider. Store credentials as backend secrets. |
| `ALGORITHM`, `ACCESS_TOKEN_EXPIRE_MINUTES` | Optional authentication algorithm and token lifetime overrides. |
| `OLLAMA_BASE_URL`, `OLLAMA_MODEL` | Ollama endpoint and model for AI-dependent workflows. Defaults are loopback `127.0.0.1:11434` and `qwen3:8b`. The loopback URL points inside the backend container, not to your computer. Ollama must be hosted separately and reachable from the backend over a secure connection; the backend image does not include Ollama or the model. |
| `OLLAMA_TIMEOUT`, `OLLAMA_IDENTITY_TIMEOUT`, `OLLAMA_RELEVANCE_TIMEOUT`, `OLLAMA_STRATEGIST_TIMEOUT` | Optional per-operation Ollama timeout overrides, in seconds. |
| `CHROMA_PERSIST_DIRECTORY` | Optional local ChromaDB persistence path. The code uses `PersistentClient`; local container files on Vercel are not durable across instance replacement or scaling. Setting this variable only changes the path; it does not provide persistent storage. |
| `SCRAPE_FRESHNESS_THRESHOLD_DAYS`, `MAX_DETAIL_PRODUCTS`, `PLAYWRIGHT_TIMEOUT`, `SCRAPE_DELAY_MS` | Optional freshness, product-count, browser-timeout, and delay controls for scraping. |
| `MARKETPLACE_TIMEOUT_SECONDS`, `AMAZON_MARKETPLACE_TIMEOUT_SECONDS` | Optional overall and Amazon collection timeout controls. |
| `SCRAPER_DEBUG`, `SCRAPER_AMAZON_ENABLED`, `SCRAPER_FLIPKART_ENABLED`, `SCRAPER_MYNTRA_ENABLED`, `SCRAPER_MEESHO_ENABLED` | Optional scraper diagnostics and per-marketplace switches. |

Playwright and marketplace scraping may encounter Vercel execution-time, memory, or other resource limits. Changing scraper timeouts does not remove those platform limits. A successful frontend/backend build does not establish that scraping or AI integrations are operational.

## Deployment steps

1. Configure the Vercel project framework as Services and keep the existing root `vercel.json` routing.
2. Provision hosted PostgreSQL. Put its connection URL in the Vercel backend's `DATABASE_URL`; set a private `SECRET_KEY` there as well.
3. Configure the backend routing port as `8000`. Leave `VITE_API_URL` unset for same-origin frontend calls.
4. If enabling AI or password-reset email, separately provision and securely connect Ollama and/or SMTP, then configure the feature-specific variables above. Set `FRONTEND_BASE_URL` to the deployed UI origin for reset links.
5. Decide how retrieval should work before relying on Chroma data. The current local persistence is not durable on Vercel containers; no remote Chroma configuration is implemented.
6. Verify health, authentication, persistence, and any enabled integrations against the selected runtime limits. `/health` checks database connectivity.

## Database migrations

Migrations are an explicit release task, not an application startup action. For a **new, empty** hosted database only, run from `backend/`:

```powershell
..\venv\Scripts\python.exe scripts\db_preflight.py preflight
..\venv\Scripts\alembic.exe -c alembic_canonical.ini upgrade head
..\venv\Scripts\python.exe scripts\db_preflight.py verify-canonical
```

Use the image's installed `python` and `alembic` commands for a deployment release task. Preflight is read-only. If a database contains existing data, stop after preflight and review its provenance; do not stamp or adopt an existing schema automatically. Fresh installs use `backend/alembic_canonical/`; the legacy graph under `backend/alembic/` remains for historical installations.

No resources are provisioned and no deployment is claimed by this guide.
