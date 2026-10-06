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