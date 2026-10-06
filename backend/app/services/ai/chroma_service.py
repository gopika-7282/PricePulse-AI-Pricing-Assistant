"""
chroma_service.py
=================
ChromaDB RAG (Retrieval-Augmented Generation) layer for PricePulse.

Architecture role:
- PostgreSQL  ->  source of truth (products, prices, competitors, recommendations)
- ChromaDB    ->  RAG context retrieval layer (text documents for LLM grounding)

Responsibilities:
1. Initialize a persistent ChromaDB client (writes to project_root/chroma_db/)
2. Create or reuse RAG collections for different context types
3. Store text documents with metadata for retrieval
4. Retrieve top-N relevant documents by text query for LLM context
5. Support chatbot retrieval and pricing recommendation context

ChromaDB uses its own built-in embedding function for text-based retrieval.
NO external embedding models (bge-m3, MiniLM, SentenceTransformers) are used.

Collections:
- "pricing_rag"  -- competitor prices, market observations, pricing history
- "product_rag"  -- product descriptions, catalog information
- "chatbot_rag"  -- combined context for chatbot queries

Does NOT:
- Make any matching / catalog decisions (that is product_identity_service)
- Serve as the authoritative database (PostgreSQL is source of truth)
- Use BGE, MiniLM, SentenceTransformers, or any HuggingFace model
"""

import logging
import hashlib
import math
import re
from pathlib import Path
from typing import Any, Dict, List, Optional

import chromadb
from chromadb.config import Settings
from chromadb.api.types import EmbeddingFunction

logger = logging.getLogger(__name__)

# ---- Paths & constants -------------------------------------------------------

_PROJECT_ROOT = Path(__file__).resolve().parents[4]   # ./PrintPulse
CHROMA_PERSIST_DIR = str(_PROJECT_ROOT / "chroma_db")

PRICING_RAG_COLLECTION = "pricing_rag"
PRODUCT_RAG_COLLECTION  = "product_rag"
CHATBOT_RAG_COLLECTION  = "chatbot_rag"

# ---- Singleton client --------------------------------------------------------

_chroma_client: Optional[chromadb.ClientAPI] = None


class _HashEmbedding(EmbeddingFunction[List[str]]):
    """Small deterministic lexical embedding; avoids Chroma's default MiniLM model."""
    def __init__(self) -> None:
        pass

    @staticmethod
    def name() -> str:
        return "pricepulse_hash_embedding_v1"

    def get_config(self) -> Dict[str, Any]:
        return {"dimensions": 384}

    @staticmethod
    def build_from_config(config: Dict[str, Any]) -> "_HashEmbedding":
        return _HashEmbedding()

    def __call__(self, input: List[str]) -> List[List[float]]:
        vectors = []
        for document in input:
            vector = [0.0] * 384
            tokens = re.findall(r"[\w]+", document.lower())
            for token in tokens:
                digest = hashlib.blake2b(token.encode("utf-8"), digest_size=8).digest()
                idx = int.from_bytes(digest[:4], "big") % len(vector)
                sign = 1.0 if digest[4] & 1 else -1.0
                vector[idx] += sign
            magnitude = math.sqrt(sum(value * value for value in vector)) or 1.0
            vectors.append([value / magnitude for value in vector])
        return vectors

    def embed_query(self, input: Any) -> Any:
        if isinstance(input, str):
            return self([input])[0]
        return self(list(input))

    def embed_documents(self, input: List[str]) -> List[List[float]]:
        return self(input)


def _get_client() -> chromadb.ClientAPI:
    """Lazily create and return the persistent ChromaDB client."""
    global _chroma_client
    if _chroma_client is None:
        logger.info("[CHROMA_RAG] Initialising persistent client at: %s", CHROMA_PERSIST_DIR)
        _chroma_client = chromadb.PersistentClient(
            path=CHROMA_PERSIST_DIR,
            settings=Settings(anonymized_telemetry=False),
        )
        logger.info("[CHROMA_RAG] Client ready.")
    return _chroma_client


def get_collection(collection_name: str) -> chromadb.Collection:
    """Return (and lazily create/reuse) a named RAG collection."""
    client = _get_client()
    logger.debug("[CHROMA_RAG] Getting or creating collection: '%s'", collection_name)
    collection = client.get_or_create_collection(
        name=collection_name,
        metadata={"description": "RAG context collection: " + collection_name},
        embedding_function=_HashEmbedding(),
    )
    logger.debug(
        "[CHROMA_RAG] Collection '%s' ready. Document count: %d",
        collection_name, collection.count(),
    )
    return collection


# ---- Storage -----------------------------------------------------------------

def store_document(
    collection_name: str,
    doc_id: str,
    text: str,
    metadata: Optional[Dict[str, Any]] = None,
) -> None:
    """Upsert a text document into a RAG collection."""
    if not text or not text.strip():
        logger.warning("[CHROMA_RAG] Skipping empty document id='%s'", doc_id)
        return

    collection = get_collection(collection_name)
    meta = metadata or {"source": "unspecified"}

    collection.upsert(
        ids=[doc_id],
        documents=[text],
        metadatas=[meta],
    )

    logger.info(
        "[CHROMA_RAG] Document stored/updated: collection='%s' id='%s'",
        collection_name, doc_id,
    )


def store_documents_batch(
    collection_name: str,
    documents: List[Dict[str, Any]],
) -> int:
    """
    Upsert multiple documents into a RAG collection in one batch.

    Args:
        collection_name: Target RAG collection name
        documents: List of dicts with keys: id (str), text (str), metadata (dict, optional)

    Returns:
        Number of documents successfully stored.
    """
    if not documents:
        return 0

    valid_ids: List[str] = []
    valid_texts: List[str] = []
    valid_metas: List[Dict[str, Any]] = []

    for doc in documents:
        doc_id = str(doc.get("id", ""))
        text   = str(doc.get("text", "")).strip()
        meta   = doc.get("metadata") or {}

        if not doc_id or not text:
            logger.warning("[CHROMA_RAG] Skipping document with empty id or text: %s", doc_id)
            continue

        valid_ids.append(doc_id)
        valid_texts.append(text)
        valid_metas.append(meta or {"source": "unspecified"})

    if not valid_ids:
        return 0

    collection = get_collection(collection_name)
    collection.upsert(ids=valid_ids, documents=valid_texts, metadatas=valid_metas)

    logger.info(
        "[CHROMA_RAG] Batch stored: collection='%s' count=%d",
        collection_name, len(valid_ids),
    )
    return len(valid_ids)


# ---- Retrieval ---------------------------------------------------------------

def retrieve_relevant(
    collection_name: str,
    query: str,
    n_results: int = 5,
    where: Optional[Dict[str, Any]] = None,
) -> List[Dict[str, Any]]:
    """
    Retrieve the top-N most relevant documents for a query.

    Returns:
        List of dicts with keys: id, text, metadata, distance.
        Empty list if collection is empty or query fails.
    """
    if not query or not query.strip():
        logger.warning("[CHROMA_RAG] Empty query for collection '%s'", collection_name)
        return []

    try:
        collection = get_collection(collection_name)
        total = collection.count()

        if total == 0:
            logger.info("[CHROMA_RAG] Collection '%s' is empty.", collection_name)
            return []

        actual_n = min(n_results, total)

        query_kwargs: Dict[str, Any] = {
            "query_texts": [query],
            "n_results": actual_n,
            "include": ["documents", "metadatas", "distances"],
        }
        if where:
            query_kwargs["where"] = where

        results = collection.query(**query_kwargs)

        ids       = results["ids"][0]
        documents = results["documents"][0]
        metadatas = results["metadatas"][0]
        distances = results["distances"][0]

        hits = [
            {
                "id":       doc_id,
                "text":     text,
                "metadata": meta,
                "distance": dist,
            }
            for doc_id, text, meta, dist in zip(ids, documents, metadatas, distances)
        ]

        logger.info(
            "[CHROMA_RAG] Retrieved %d documents from '%s'.",
            len(hits), collection_name,
        )
        return hits

    except Exception as exc:
        logger.error(
            "[CHROMA_RAG] Retrieval failed for collection '%s': %s",
            collection_name, exc,
        )
        return []


def delete_document(collection_name: str, doc_id: str) -> None:
    """Remove a document from a RAG collection by ID."""
    try:
        collection = get_collection(collection_name)
        collection.delete(ids=[doc_id])
        logger.info(
            "[CHROMA_RAG] Deleted document id='%s' from collection='%s'",
            doc_id, collection_name,
        )
    except Exception as exc:
        logger.warning(
            "[CHROMA_RAG] Failed to delete document id='%s': %s",
            doc_id, exc,
        )


# ---- Pricing RAG helpers -----------------------------------------------------

def index_competitor_price(
    catalog_product_id: int,
    competitor_product_id: int,
    platform: str,
    product_name: str,
    price: float,
    category: str = "",
    extra_meta: Optional[Dict[str, Any]] = None,
) -> None:
    """Index a competitor price observation into the pricing RAG collection."""
    doc_id = "comp_" + str(competitor_product_id)
    text = (
        "Competitor product: " + product_name + "\n"
        "Platform: " + platform + "\n"
        "Price: Rs." + str(round(price, 2)) + "\n"
        "Category: " + category + "\n"
        "Catalog Product ID: " + str(catalog_product_id)
    )
    meta: Dict[str, Any] = {
        "type": "competitor_price",
        "catalog_product_id": str(catalog_product_id),
        "competitor_product_id": str(competitor_product_id),
        "platform": platform,
        "price": str(price),
        "category": category,
    }
    if extra_meta:
        meta.update({k: str(v) for k, v in extra_meta.items()})

    store_document(collection_name=PRICING_RAG_COLLECTION, doc_id=doc_id, text=text, metadata=meta)
    store_document(collection_name=CHATBOT_RAG_COLLECTION, doc_id="pricing_" + doc_id, text=text, metadata=meta)


def index_product_catalog(
    catalog_product_id: int,
    product_name: str,
    category: str = "",
    product_details: str = "",
) -> None:
    """Index a catalog product description into the product RAG collection."""
    doc_id = "catalog_" + str(catalog_product_id)
    text = (
        "Product: " + product_name + "\n"
        "Category: " + category + "\n"
        "Details: " + product_details
    ).strip()
    meta: Dict[str, Any] = {
        "type": "catalog_product",
        "catalog_product_id": str(catalog_product_id),
        "product_name": product_name,
        "category": category,
    }
    store_document(collection_name=PRODUCT_RAG_COLLECTION, doc_id=doc_id, text=text, metadata=meta)
    store_document(collection_name=CHATBOT_RAG_COLLECTION, doc_id="product_" + doc_id, text=text, metadata=meta)


def index_pricing_recommendation(
    retailer_product_id: int,
    catalog_product_id: int,
    product_name: str,
    recommended_price: float,
    reasoning: str,
    market_avg: float = 0.0,
    min_price: float = 0.0,
    max_price: float = 0.0,
    user_id: Optional[int] = None,
) -> None:
    """Index a pricing recommendation into the pricing RAG collection."""
    doc_id = "rec_" + (str(user_id) + "_" if user_id is not None else "") + str(retailer_product_id)
    text = (
        "Pricing recommendation for: " + product_name + "\n"
        "Recommended price: Rs." + str(round(recommended_price, 2)) + "\n"
        "Market average: Rs." + str(round(market_avg, 2)) + "\n"
        "Market range: Rs." + str(round(min_price, 2)) + " - Rs." + str(round(max_price, 2)) + "\n"
        "Reasoning: " + reasoning
    )
    meta: Dict[str, Any] = {
        "type": "pricing_recommendation",
        "retailer_product_id": str(retailer_product_id),
        "catalog_product_id": str(catalog_product_id),
        "product_name": product_name,
        "recommended_price": str(recommended_price),
        "user_id": str(user_id) if user_id is not None else "",
    }
    store_document(collection_name=PRICING_RAG_COLLECTION, doc_id=doc_id, text=text, metadata=meta)
    store_document(collection_name=CHATBOT_RAG_COLLECTION, doc_id="pricing_" + doc_id, text=text, metadata=meta)


# ---- Chatbot RAG retrieval ---------------------------------------------------

def retrieve_chatbot_context(
    query: str,
    catalog_product_id: Optional[int] = None,
    n_results: int = 5,
    retailer_product_id: Optional[int] = None,
    user_id: Optional[int] = None,
) -> str:
    """
    Retrieve relevant RAG context for a chatbot query.

    Returns formatted context string for LLM prompt, or empty string.
    """
    where_filter = None
    if catalog_product_id is not None:
        where_filter = {"catalog_product_id": str(catalog_product_id)}

    hits = retrieve_relevant(
        collection_name=CHATBOT_RAG_COLLECTION,
        query=query,
        n_results=n_results,
        where=where_filter,
    )

    if not hits:
        if catalog_product_id is not None:
            return ""
        pricing_hits = retrieve_relevant(
            collection_name=PRICING_RAG_COLLECTION, query=query,
            n_results=max(1, n_results // 2),
        )
        product_hits = retrieve_relevant(
            collection_name=PRODUCT_RAG_COLLECTION, query=query,
            n_results=max(1, n_results // 2),
        )
        hits = pricing_hits + product_hits

    if retailer_product_id is not None:
        hits = [hit for hit in hits if hit.get("metadata", {}).get("type") != "pricing_recommendation"
                or (hit.get("metadata", {}).get("retailer_product_id") == str(retailer_product_id)
                    and (user_id is None or hit.get("metadata", {}).get("user_id") == str(user_id)))]

    if not hits:
        return ""

    parts = ["=== RETRIEVED CONTEXT ==="]
    for i, hit in enumerate(hits, 1):
        parts.append("\n[Context " + str(i) + "]")
        parts.append(hit["text"])
    return "\n".join(parts)


def retrieve_pricing_context(
    query: str,
    catalog_product_id: Optional[int] = None,
    n_results: int = 8,
    retailer_product_id: Optional[int] = None,
    user_id: Optional[int] = None,
) -> str:
    """Retrieve relevant pricing context for the Strategist Agent."""
    where_filter = None
    if catalog_product_id is not None:
        where_filter = {"catalog_product_id": str(catalog_product_id)}

    hits = retrieve_relevant(
        collection_name=PRICING_RAG_COLLECTION,
        query=query,
        n_results=n_results,
        where=where_filter,
    )

    if retailer_product_id is not None:
        hits = [hit for hit in hits if hit.get("metadata", {}).get("type") != "pricing_recommendation"
                or (hit.get("metadata", {}).get("retailer_product_id") == str(retailer_product_id)
                    and (user_id is None or hit.get("metadata", {}).get("user_id") == str(user_id)))]

    if not hits:
        return ""

    parts = ["=== PRICING CONTEXT ==="]
    for i, hit in enumerate(hits, 1):
        parts.append("\n[Source " + str(i) + "]")
        parts.append(hit["text"])
    return "\n".join(parts)
