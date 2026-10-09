"""Sync curated business guidance from backend/knowledge/business_knowledge.json."""
from app.services.ai.chroma_service import seed_business_knowledge


if __name__ == "__main__":
    print(f"Synced {seed_business_knowledge()} business knowledge documents.")
