# PricePulse — Agentic AI Pricing Intelligence Assistant

## Overview

PricePulse is an Agentic AI-powered pricing intelligence platform designed
for small and growing e-commerce retailers.

It automatically identifies products, retrieves relevant catalog candidates,
checks data freshness, collects live competitor prices from marketplaces,
analyzes market conditions, and generates explainable profit-aware pricing
recommendations.

The system combines:

- Agentic AI
- LangGraph
- Qwen via Ollama
- PostgreSQL
- RAG
- Live marketplace scraping
- React
- FastAPI

The goal is to provide intelligent pricing capabilities without the
complexity, platform lock-in, and cost of traditional enterprise pricing
solutions.

---

## Project Status

🚧 Under active development

The current development focus is the complete end-to-end Agentic AI workflow,
including product identity, catalog lifecycle, freshness management,
marketplace scraping, RAG, pricing agents, and React integration.

## Ollama timeout configuration

Timeouts are positive finite seconds and are validated when the backend loads
its configuration. Defaults are `OLLAMA_TIMEOUT=180` for general Ollama calls,
`OLLAMA_IDENTITY_TIMEOUT=180` for Product Identity,
`OLLAMA_RELEVANCE_TIMEOUT=45` for Scout relevance filtering, and
`OLLAMA_STRATEGIST_TIMEOUT=180` for Strategist calls. Set any of these in the
backend environment to override its default. The current compliance agent is
deterministic and does not make an Ollama call.

---

## Core Workflow

```text
Retailer
   ↓
React Frontend
   ↓
FastAPI
   ↓
LangGraph Orchestrator
   ↓
Product Identity Agent
   ↓
PostgreSQL Candidate Retrieval
   ↓
Qwen
   ↓
MATCH / UNCERTAIN / NOT_MATCH
   ↓
Catalog + Freshness Lifecycle
   ↓
Scout Agent
   ↓
Live Marketplace Tools
   ├── Flipkart
   ├── Amazon
   ├── Myntra
   └── Meesho
   ↓
Competitor Data
   ↓
RAG Retrieval
   ↓
Strategist Agent
   ↓
Compliance Agent
   ↓
Price Recommendation
   ↓
PostgreSQL
   ↓
React Dashboard

---

## CI/CD

PricePulse uses GitHub Actions to check the existing FastAPI backend, React/Vite frontend, PostgreSQL migrations, and Docker builds. CI runs on branch pushes, pull requests targeting `main`, and manual dispatch. Backend CI installs Python 3.10 dependencies from the root `requirements.txt`, validates imports, applies Alembic migrations against an isolated PostgreSQL 16 test database, and runs the existing pytest suite. Frontend CI uses the Node test runner (`npm test`) and creates the Vite production build (`npm run build`) using the committed lockfile. See [docs/CI_CD_GUIDE.md](docs/CI_CD_GUIDE.md) for commands and troubleshooting.

The backend suite exercises scraper parsing and diagnostics, agent/pricing workflow behavior, retrieval, and mocked LLM interfaces. Normal CI does not scrape live marketplaces or require Ollama/Qwen3:8b. Chroma uses an isolated temporary location in CI; local operation keeps the existing `chroma_db` default. PostgreSQL migrations run only against the ephemeral CI service.

Docker validation builds backend and frontend images without publishing them. After CI succeeds for a pushed `v*` tag, the release workflow verifies that exact tag commit and creates a versioned GitHub Release bundle with frontend assets, Docker image archives, build metadata, and a checksum. No GitHub Actions secrets are required for checks or artifacts. Runtime credentials must be supplied by the chosen deployment environment.

Production deployment is not configured because the hosting target has not been selected. Automatic deployment requires a target, runtime secret storage, public frontend API/CORS configuration, persistent database and Chroma storage, a migration/rollback policy, and a reachable Ollama endpoint with Qwen3:8b. No production deployment is performed by these workflows.
