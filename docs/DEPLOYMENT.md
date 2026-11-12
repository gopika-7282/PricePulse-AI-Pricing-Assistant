# Student-demo deployment guide

This document describes a target architecture only. The repository does not provision services or claim a live deployment.

## Target architecture

- **Frontend:** Vercel static React/Vite project with project root `frontend`, build command `npm run build`, output `dist`.
- **API:** a persistent Docker web service built from repository root context and `backend/Dockerfile`. The image installs Python dependencies and Playwright Chromium and runs `uvicorn app.main:app` on the platform-provided `PORT`.
- **Database:** hosted PostgreSQL. Use a new empty database for the canonical fresh-install migration.
- **ChromaDB:** local persistent client on a durable disk mounted into the API container. Keep one API instance while using the local client.
- **Model:** a secured, reachable Ollama service with `qwen3:8b`. Ollama is a separate runtime; deploying the frontend or API does not host the model.

Deploying the frontend to Vercel alone does not deploy the FastAPI backend, PostgreSQL, ChromaDB data, or Ollama. The full LangGraph workflow depends on the backend reaching Ollama and Chroma. Do not expose an unauthenticated Ollama API on a public interface; use private networking or a secured proxy.

## Provider considerations

Vercel can serve the static frontend, while the current API includes long scraping and streamed workflow requests, which are a poor fit for short serverless request limits. A Docker web service such as Render or a managed VM can run the existing backend image. The browser runtime and ChromaDB need more memory and persistence than typical free, sleeping container tiers provide. On Render, free web services sleep and have ephemeral filesystems; attaching persistent disks requires a paid service. Review [Render free services](https://render.com/docs/free), [persistent disks](https://render.com/docs/disks), and [web services](https://render.com/docs/web-services) for current limits and pricing before selecting a plan.

A hosted PostgreSQL free tier can work for a small demo, subject to capacity, sleep, backup, and retention limits. For example, review [Neon's current free plan](https://neon.com/blog/neon-free-plan-1-gb-per-project) and connection guidance before use. Keep the database and API in nearby regions. A rented GPU or always-on machine for Ollama may be the largest recurring cost; there is no guarantee of a free host that can continuously serve Qwen3 8B with adequate performance. Provider limits and prices can change.

## Frontend settings

Create the Vercel project with:

- Root directory: `frontend`
- Framework/build: Vite / `npm run build`
- Output directory: `dist`
- Build environment variable: `VITE_API_URL=https://<your-api-host>`

`VITE_API_URL` is compiled into browser JavaScript and is public. Do not put credentials, private URLs, or secrets in any `VITE_*` variable. `frontend/vercel.json` already supplies an SPA route fallback.

## Backend configuration

Set these in the API host's environment/secret manager. Do not upload `.env` files.

| Variable | Required | Purpose |
| --- | --- | --- |
| `DATABASE_URL` | Yes | Hosted PostgreSQL SQLAlchemy URL; use provider TLS settings |
| `SECRET_KEY` | Yes | Strong random JWT signing secret |
| `CORS_ORIGINS` | Yes | Comma-separated exact HTTPS frontend origins; include intended preview origins only |
| `FRONTEND_BASE_URL` | Recommended | Primary frontend origin for password-reset links; `FRONTEND_URL` is a compatibility fallback |
| `OLLAMA_BASE_URL` | For full AI workflow | Reachable secured Ollama base URL used with `/api/generate` |
| `OLLAMA_MODEL` | For full AI workflow | Preserve `qwen3:8b` |
| `OLLAMA_TIMEOUT` | Optional | General Ollama timeout, seconds; default 180 |
| `OLLAMA_IDENTITY_TIMEOUT` | Optional | Product identity timeout, seconds; default 180 |
| `OLLAMA_RELEVANCE_TIMEOUT` | Optional | Relevance timeout, seconds; default 45 |
| `OLLAMA_STRATEGIST_TIMEOUT` | Optional | Pricing estimate timeout, seconds; default 180 |
| `CHROMA_PERSIST_DIRECTORY` | Yes for durable retrieval | Set to the mounted disk path, for example `/app/chroma_db` |
| `SCRAPE_FRESHNESS_THRESHOLD_DAYS` | Optional | Freshness window; default 7 |
| `MAX_DETAIL_PRODUCTS` | Optional | Detail cap; default 10 |
| `PLAYWRIGHT_TIMEOUT` | Optional | Browser timeout in milliseconds; default 30000 |
| `SCRAPE_DELAY_MS` | Optional | Scraper delay setting; default 1500 |
| `MARKETPLACE_TIMEOUT_SECONDS` | Optional | General per-marketplace bound; default 25 |
| `AMAZON_MARKETPLACE_TIMEOUT_SECONDS` | Optional | Amazon bound; default 180 |
| `SCRAPER_DEBUG` | Optional | Enables additional scraper diagnostics |
| `SCRAPER_AMAZON_ENABLED`, `SCRAPER_FLIPKART_ENABLED`, `SCRAPER_MYNTRA_ENABLED`, `SCRAPER_MEESHO_ENABLED` | Optional | Per-platform switches; default true |
| `SMTP_HOST`, `SMTP_PORT`, `SMTP_USE_SSL`, `SMTP_USE_TLS`, `SMTP_USERNAME`, `SMTP_PASSWORD`, `SMTP_FROM_EMAIL` | Optional | Configure all required mail fields to enable password-reset email delivery |

The service must listen on the host-provided `PORT`; the Dockerfile already does. Configure the platform health check to `GET /health`. This endpoint verifies PostgreSQL connectivity and returns an error if the database is unavailable.

## ChromaDB storage

The current code uses `chromadb.PersistentClient(path=...)`, not Chroma Cloud or a remote HTTP client. Mount a durable volume at the configured `CHROMA_PERSIST_DIRECTORY`, such as `/app/chroma_db`. Back it up if retrieval state matters. Chroma is a retrieval index; PostgreSQL remains the authoritative record store. Without durable storage, Chroma files can be lost when an ephemeral container is replaced. Do not scale this local Chroma client across multiple API replicas sharing no common index. Moving to remote Chroma would require a code change in `backend/app/services/ai/chroma_service.py` and a separate architecture review.

## Ollama/Qwen3 service

The default backend URL `http://127.0.0.1:11434` means the backend container itself. Set `OLLAMA_BASE_URL` to the private network address of the Ollama host. Install Ollama there and pull the exact configured model (`qwen3:8b`). The model artifact is several gigabytes and inference memory/compute requirements exceed the download size. The backend workflow uses Ollama for identity decisions, borderline relevance, pricing estimates when market evidence is insufficient, and generated chat. If the model is unreachable, the API can still start, but model-dependent requests may fail or return unavailable results. Keep the Ollama endpoint private or behind an authenticated proxy.

## PostgreSQL migration procedure

For a new, empty hosted database, set `DATABASE_URL` in a trusted deployment environment and run from `backend/`:

```powershell
..\venv\Scripts\python.exe scripts\db_preflight.py preflight
..\venv\Scripts\alembic.exe -c alembic_canonical.ini upgrade head
..\venv\Scripts\python.exe scripts\db_preflight.py verify-canonical
```

The root environment path above is for the developer workstation. In a deployment container or one-off job, use that image's installed commands instead:

```sh
python scripts/db_preflight.py preflight
alembic -c alembic_canonical.ini upgrade head
python scripts/db_preflight.py verify-canonical
```

Run the migration as an explicit release/deploy task before serving requests; the API does not migrate the database at startup. Preflight is read-only. For any database containing existing data, stop after preflight and review the result and provenance. Do not run adoption or stamp an existing schema automatically. The legacy graph under `backend/alembic/` remains for historical installations; canonical fresh installs use `backend/alembic_canonical/`.

## Suggested rollout order

1. Select providers and regions; confirm costs, persistent disk, PostgreSQL retention/backup, network access, and the Ollama compute plan.
2. Create a new hosted PostgreSQL database; keep its URL in backend secret storage.
3. Provision a secured Ollama host, install `qwen3:8b`, and confirm the API host can reach it over a private or protected route.
4. Create the API Docker service using root build context and `backend/Dockerfile`; set backend variables and health path `/health`.
5. Attach a persistent Chroma volume at `/app/chroma_db`; set `CHROMA_PERSIST_DIRECTORY`; run a single API replica for the local Chroma client.
6. Run canonical preflight, migration, and canonical verification against the new empty database.
7. Create the Vercel frontend project and set `VITE_API_URL` to the API HTTPS origin.
8. Set `CORS_ORIGINS` to the deployed Vercel origin and `FRONTEND_BASE_URL` to the primary UI origin.
9. Verify login, product creation, CORS, streamed analysis, Ollama calls, marketplace status handling, recommendation persistence, and Chroma data after an API restart.
10. Configure optional SMTP if password-reset delivery is part of the demo, then document backups, monitoring, and who owns each service.

No hosting resources are created by following this document. Review the provider's current terms and pricing before provisioning.
