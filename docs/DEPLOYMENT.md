# Deployment preparation

## Frontend on Vercel

Import this repository and set the Vercel project root to `frontend`. The build
command is `npm run build`; the output directory is `dist`. Set
`VITE_API_URL` to the HTTPS base URL of the separately hosted API (for example,
`https://api.example.com`). `frontend/vercel.json` enables client-side route
fallbacks. Vercel hosts only the static React application; it cannot reach a
developer's local Ollama process.

## API and PostgreSQL

Run FastAPI on a persistent container or VM, with a managed PostgreSQL service.
Configure `DATABASE_URL` using the provider's PostgreSQL connection string and
`SECRET_KEY` using the host's secret manager. Set `CORS_ORIGINS` to the exact
Vercel production origin(s), comma-separated (for example,
`https://your-project.vercel.app`), and `FRONTEND_BASE_URL` to the primary UI
origin for password-reset links. The frontend reads `VITE_API_URL` at build
time; set it to the hosted FastAPI HTTPS origin in Vercel. The API uses
credentialed CORS with the explicit `CORS_ORIGINS` allowlist and permits the
Authorization header used by login-protected requests. The `/health` endpoint checks
PostgreSQL connectivity. Set `ALGORITHM` and `ACCESS_TOKEN_EXPIRE_MINUTES` as
needed; `OLLAMA_BASE_URL`, `OLLAMA_MODEL`, and the four `OLLAMA_*_TIMEOUT`
values configure AI. Optional reset-email delivery requires `SMTP_HOST`,
`SMTP_PORT`, `SMTP_USERNAME`, `SMTP_PASSWORD`, and `SMTP_FROM_EMAIL`.
`CHROMA_PERSIST_DIRECTORY` is optional for a disposable student deployment and
should point to durable storage if AI retrieval data must survive restarts.
Run the canonical migration with
`alembic -c alembic_canonical.ini upgrade head` from `backend/` before starting
the API. Existing databases require `python scripts/db_preflight.py preflight`
and a reviewed adoption decision; never stamp a database whose historical
competitor timestamps lack explicit `SCRAPE` provenance.

## Persistent retrieval data and model runtime

Mount durable storage at `CHROMA_PERSIST_DIRECTORY` and back it up independently;
ChromaDB is a retrieval index, while PostgreSQL remains authoritative. The
development default is local Ollama with `qwen3:8b`. A hosted backend needs an
LLM runtime reachable from that backend over a private, secured network, or a
separately approved hosted inference provider. Do not point a public hosted API
at a developer workstation. No paid provider or hosting plan is selected here.

Keep secrets in the hosting provider's secret store. Do not upload `.env` files
or expose credentials in Vite variables: `VITE_*` values are public in the
browser bundle.
