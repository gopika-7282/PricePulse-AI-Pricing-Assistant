"""
semantic_retrieval_service.py
==============================
Centralized AI semantic retrieval pipeline for PricePulse.

Architecture role:
  New Retailer Product
        |
        v
  build_semantic_text()      <- product_name + category + product_details
        |
        v
  get_embedding()            <- Ollama bge-m3 -> 1024-dim vector
        |
        v
  search_similar()           <- ChromaDB cosine search
        |
        v
  Candidate ProductCatalog list  <- references back to PostgreSQL

Principles:
- Uses ONLY Ollama bge-m3 for semantic understanding (no HuggingFace, no pgvector)
- ChromaDB is NOT the source of truth -- only a vector index over PostgreSQL IDs
- Does NOT make identity/merge decisions -- returns raw candidates only
- Brand / retailer_id are EXCLUDED from the semantic text (metadata only)
- Idempotent: safe to call multiple times for the same product

Does NOT:
- Apply confidence thresholds
- Automatically merge products
- Perform reranking or cross-encoding
- Use keyword synonyms or hardcoded translations
"""

import logging
from typing import Any, Dict, List

from app.services.ai.embedding_service import build_embedding_text, get_embedding
from app.services.ai.chroma_service import search_similar, store_embedding

logger = logging.getLogger(__name__)

# -- Constants -----------------------------------------------------------------

DEFAULT_TOP_K = 10   # Maximum candidates returned per search


# -- Public API ----------------------------------------------------------------

def build_semantic_text(
    product_name: str,
    category: str = "",
    product_details: str = "",
) -> str:

    return build_embedding_text(
        name=product_name,
        category=category,
        product_details=product_details,
    )


def retrieve_catalog_candidates(
    product_name: str,
    category: str = "",
    product_details: str = "",
    top_k: int = DEFAULT_TOP_K,
) -> List[Dict[str, Any]]:
    """
    Run the full semantic retrieval pipeline for a product query.

    Steps:
      1. Build structured semantic text from product fields
      2. Generate 1024-dim bge-m3 embedding via Ollama
      3. Query ChromaDB for the top-k most similar ProductCatalog entries
      4. Return structured candidate list (no merging, no decisions)

    Args:
        product_name:    Product name (required)
        category:        Product category (optional)
        product_details: Product description / specs (optional)
        top_k:           Maximum candidates to return (default 10)

    Returns:
        List of candidate dicts, each containing:
        {
            "catalog_product_id": int,    # PostgreSQL ProductCatalog.id
            "product_name":       str,    # as stored in ChromaDB metadata
            "category":           str,    # as stored in ChromaDB metadata
            "distance":           float,  # cosine distance (lower = more similar)
        }
        Ordered by ascending distance (most similar first).
        Empty list if ChromaDB has no indexed products.

    Raises:
        RuntimeError: If Ollama is unavailable or returns an error
        ValueError:   If product_name is empty
    """
    logger.info(
        "[VECTOR_SEARCH_STARTED] name='%s' category='%s' top_k=%d",
        product_name, category, top_k,
    )

    # -- Step 1: Build semantic text -------------------------------------------
    semantic_text = build_semantic_text(product_name, category, product_details)
    logger.debug("[SEMANTIC_SEARCH] semantic_text=\n%s", semantic_text)

    # -- Step 2: Generate embedding (raises RuntimeError if Ollama fails) ------
    embedding = get_embedding(
        name=product_name,
        category=category,
        product_details=product_details,
    )
    logger.info(
        "[EMBEDDING_CREATED] name='%s' dim=%d",
        product_name, len(embedding),
    )

    # -- Step 3: Search ChromaDB -----------------------------------------------
    try:
        raw_hits = search_similar(query_embedding=embedding, n_results=top_k)
    except Exception as exc:
        logger.error(
            "[MATCHING_FAILED] name='%s' error='%s'",
            product_name, exc,
        )
        raise RuntimeError(
            f"[SEMANTIC_RETRIEVAL] ChromaDB vector search failed for '{product_name}': {exc}"
        ) from exc

    logger.info(
        "[VECTOR_SEARCH_COMPLETED] name='%s' raw_hits=%d",
        product_name, len(raw_hits),
    )

    # -- Step 4: Shape candidates ----------------------------------------------
    candidates: List[Dict[str, Any]] = []
    for hit in raw_hits:
        meta = hit.get("metadata", {})
        try:
            catalog_product_id = int(hit["id"])
        except (KeyError, ValueError, TypeError):
            logger.warning(
                "[SEMANTIC_SEARCH] Skipping hit with non-integer id='%s'", hit.get("id")
            )
            continue

        candidates.append(
            {
                "catalog_product_id": catalog_product_id,
                "product_name": meta.get("product_name", ""),
                "category": meta.get("category", ""),
                "distance": hit.get("distance", 1.0),
            }
        )

    logger.info(
        "[CANDIDATES_FOUND] name='%s' candidate_count=%d",
        product_name, len(candidates),
    )
    return candidates


def index_catalog_product(
    product_id: int,
    product_name: str,
    category: str = "",
    product_details: str = "",
) -> None:
    """
    Generate and store a bge-m3 embedding for a ProductCatalog entry.

    Called immediately after a new ProductCatalog row is committed so that
    future semantic searches can retrieve it.

    This is an UPSERT -- safe to call multiple times for the same product_id.
    If Ollama is unavailable, raises RuntimeError (do NOT silently skip).

    Args:
        product_id:      PostgreSQL ProductCatalog.id
        product_name:    Canonical product name
        category:        Product category (optional)
        product_details: Product description (optional)

    Raises:
        RuntimeError: If Ollama or ChromaDB is unavailable
        ValueError:   If product_name is empty or embedding dimension is wrong
    """
    logger.info(
        "[CHROMA_INDEX_STARTED] product_id=%d name='%s' category='%s'",
        product_id, product_name, category,
    )

    embedding = get_embedding(
        name=product_name,
        category=category,
        product_details=product_details,
    )

    store_embedding(
        product_id=product_id,
        product_name=product_name,
        category=category,
        embedding=embedding,
    )

    logger.info(
        "[CHROMA_INDEX_COMPLETED] product_id=%d name='%s'",
        product_id, product_name,
    )
