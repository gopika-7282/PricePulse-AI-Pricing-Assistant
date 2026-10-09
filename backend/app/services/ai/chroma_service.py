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
- "business_knowledge" -- curated, shared business guidance (no retailer-private data)

Does NOT:
- Make any matching / catalog decisions (that is product_identity_service)
- Serve as the authoritative database (PostgreSQL is source of truth)
- Use BGE, MiniLM, SentenceTransformers, or any HuggingFace model
"""

import logging
import hashlib
import json
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
BUSINESS_KNOWLEDGE_COLLECTION = "business_knowledge"
BUSINESS_KNOWLEDGE_PATH = _PROJECT_ROOT / "backend" / "knowledge" / "business_knowledge.json"
_BUSINESS_KNOWLEDGE_DIGEST: Optional[str] = None

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


def seed_business_knowledge(documents: Optional[List[Dict[str, Any]]] = None) -> int:
    """Sync curated, shared business guidance into its own Chroma collection."""
    global _BUSINESS_KNOWLEDGE_DIGEST
    if documents is None:
        with BUSINESS_KNOWLEDGE_PATH.open("r", encoding="utf-8") as handle:
            documents = json.load(handle)
    normalized = [{"id": str(doc["id"]), "text": str(doc["text"]).strip(),
                   "metadata": {"source": "business_knowledge_seed", "topic": str(doc.get("topic", "general")),
                                "category": _primary_business_category(doc),
                                "categories": _business_categories(doc)}}
                  for doc in documents if doc.get("id") and str(doc.get("text", "")).strip()]
    digest = hashlib.sha256(json.dumps(normalized, sort_keys=True, ensure_ascii=False).encode("utf-8")).hexdigest()
    if digest == _BUSINESS_KNOWLEDGE_DIGEST:
        return len(normalized)
    collection = get_collection(BUSINESS_KNOWLEDGE_COLLECTION)
    existing = collection.get(where={"source": "business_knowledge_seed"}, include=["metadatas"])
    expected_ids = {doc["id"] for doc in normalized}
    obsolete = [item_id for item_id in existing.get("ids", []) if item_id not in expected_ids]
    if obsolete:
        collection.delete(ids=obsolete)
    stored = store_documents_batch(BUSINESS_KNOWLEDGE_COLLECTION, normalized)
    _BUSINESS_KNOWLEDGE_DIGEST = digest
    logger.info("Business knowledge synced: %d curated documents", stored)
    return stored


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


_BUSINESS_STOP_WORDS = {"about", "after", "also", "and", "are", "can", "could", "does", "from", "have",
                       "define", "definition", "difference", "explain", "from", "help", "how", "into", "is",
                       "make", "mean", "means", "please", "should", "tell", "that", "the", "their", "them",
                       "then", "there", "this", "what", "when", "where", "which", "with", "would", "your"}

_BUSINESS_CATEGORY_RULES = {
    "profit": ("profit", "margin", "markup", "contribution", "break-even", "unit economics", "costing"),
    "inventory": ("inventory", "stock", "reorder", "safety stock", "procurement", "working capital"),
    "promotion": ("promotion", "discount", "bundle", "clearance", "markdown", "festival"),
    "competitor_analysis": ("competitor", "competitive", "market positioning", "market analysis"),
    "product_positioning": ("positioning", "premium", "value-based", "product launch", "product differentiation"),
    "customer_segment": ("customer", "segmentation", "target audience", "customer research"),
    "sales_planning": ("sales planning", "product launch", "launch price", "sales plan"),
    "ecommerce": ("e-commerce", "ecommerce", "marketplace", "shipping", "listing", "channel fee"),
    "forecasting": ("forecast", "demand", "sales history", "seasonal", "elasticity", "price test"),
    "packaging": ("pack-size", "pack size", "pack quantity", "packaging", "unit-price", "per gram", "per millilitre"),
    "pricing": ("pricing", "price", "cost-plus", "psychological", "penetration", "dynamic"),
    "business_strategy": ("business strategy", "business planning", "assortment", "portfolio", "launch"),
}


def _business_categories(document: Dict[str, Any]) -> List[str]:
    text = " ".join(str(document.get(key, "")) for key in ("id", "topic", "text")).casefold()
    return [category for category, terms in _BUSINESS_CATEGORY_RULES.items()
            if any(term in text for term in terms)] or ["business_strategy"]


def _primary_business_category(document: Dict[str, Any]) -> str:
    topic = str(document.get("topic", "")).casefold()
    topic_rules = (
        ("inventory", "inventory"), ("stock", "inventory"), ("reorder", "inventory"),
        ("profit", "profit"), ("margin", "profit"), ("markup", "profit"), ("contribution", "profit"),
        ("promotion", "promotion"), ("discount", "promotion"), ("bundle", "promotion"),
        ("competitor", "competitor_analysis"), ("competitive", "competitor_analysis"),
        ("position", "product_positioning"), ("premium", "product_positioning"),
        ("customer", "customer_segment"), ("segmentation", "customer_segment"),
        ("e-commerce", "ecommerce"), ("marketplace", "ecommerce"),
        ("forecast", "forecasting"), ("demand", "forecasting"),
        ("sales planning", "sales_planning"), ("launch", "sales_planning"),
        ("pack", "packaging"), ("quantity", "packaging"),
        ("market", "market_analysis"), ("pricing", "pricing"), ("price", "pricing"),
    )
    for marker, category in topic_rules:
        if marker in topic:
            return category
    categories = _business_categories(document)
    return categories[0] if categories else "business_strategy"


def _business_query_categories(query: str) -> List[str]:
    text = query.casefold()
    categories = []
    mappings = (
        ("inventory", ("stock", "inventory", "overstock", "reorder", "slow-moving", "safety stock")),
        ("profit", ("profit", "margin", "markup", "break-even", "contribution", "cost")),
        ("promotion", ("discount", "promotion", "bundle", "clearance", "offer")),
        ("competitor_analysis", ("competitor", "competitive", "compare", "cheapest", "market price")),
        ("product_positioning", ("position", "premium", "organic", "natural", "target customer")),
        ("customer_segment", ("customer", "segment", "audience", "buyer")),
        ("ecommerce", ("e-commerce", "ecommerce", "marketplace", "listing", "shipping")),
        ("forecasting", ("forecast", "demand", "elasticity", "seasonality")),
        ("sales_planning", ("launch", "sales plan", "new product")),
        ("packaging", ("pack size", "pack-size", "per gram", "per ml", "quantity")),
        ("pricing", ("pricing", "price", "pricing strategy", "price floor")),
    )
    for category, markers in mappings:
        if any(marker in text for marker in markers):
            categories.append(category)
    return categories


_BUSINESS_QUERY_SYNONYMS = {
    "competitive": ("competitor", "comparable offer", "positioning"),
    "discounts": ("discount", "promotion", "bundle"),
    "organic": ("natural", "premium", "documented ingredient quality"),
    "position": ("positioning", "differentiation", "perceived value"),
    "increase": ("price change", "cost floor", "customer value"),
    "increasing": ("price change", "cost floor", "customer value"),
    "overstock": ("inventory", "excess stock", "slow moving", "dead stock"),
    "overstocked": ("inventory", "excess stock", "slow moving", "dead stock"),
    "reorder": ("reorder point", "lead time", "safety stock"),
    "restock": ("reorder point", "lead time", "safety stock"),
    "profitability": ("profit", "contribution", "margin", "unit economics"),
    "discount": ("promotion", "markdown", "bundle", "contribution"),
    "premium": ("positioning", "differentiation", "perceived value"),
    "forecast": ("demand", "sales history", "seasonality"),
    "pack": ("pack size", "unit price", "quantity normalization"),
    "markup": ("margin", "selling price", "cost price"),
}


def retrieve_business_knowledge(query: str, n_results: int = 5) -> str:
    """Retrieve shared conceptual guidance separately from private PricePulse data."""
    if not query or not query.strip():
        return ""
    original_terms = {token for token in re.findall(r"[a-z0-9]+", query.casefold())
                      if len(token) > 2 and token not in _BUSINESS_STOP_WORDS}
    if not original_terms:
        return ""
    seed_business_knowledge()
    expanded_terms = {token for synonym in (synonym for term in original_terms for synonym in _BUSINESS_QUERY_SYNONYMS.get(term, ()))
                      for token in re.findall(r"[a-z0-9]+", synonym.casefold()) if len(token) > 2}
    retrieval_query = query + (" " + " ".join(sorted(expanded_terms)) if expanded_terms else "")
    query_categories = _business_query_categories(query)
    category_filter = {"category": {"$in": query_categories}} if query_categories else None
    hits = retrieve_relevant(BUSINESS_KNOWLEDGE_COLLECTION, retrieval_query,
                             n_results=max(n_results * 4, 24), where=category_filter)
    ranked = []
    for hit in hits:
        metadata = hit.get("metadata", {})
        searchable_text = " ".join((hit.get("text", ""), str(metadata.get("topic", "")),
                                    str(metadata.get("category", "")),
                                    " ".join(metadata.get("categories", [])) if isinstance(metadata.get("categories"), list) else ""))
        content_terms = set(re.findall(r"[a-z0-9]+", searchable_text.casefold()))
        direct_matches = len(original_terms & content_terms)
        synonym_matches = len(expanded_terms & content_terms)
        if direct_matches or synonym_matches >= 2:
            ranked.append((direct_matches * 2 + synonym_matches, hit))
    ranked.sort(key=lambda item: item[0], reverse=True)
    relevant = [hit for _, hit in ranked[:n_results]]
    if not relevant:
        return ""
    return "\n\n".join(f"[{hit.get('metadata', {}).get('topic', 'business')}] {hit['text']}" for hit in relevant)


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
    details = (extra_meta or {}).get("product_details")
    fields = [
        "Competitor product: " + product_name,
        "Platform: " + platform,
        "Price: Rs." + str(round(price, 2)),
        "Category: " + category,
    ]
    if details:
        fields.append("Product details: " + str(details))
    fields.append("Catalog Product ID: " + str(catalog_product_id))
    text = "\n".join(fields)
    meta: Dict[str, Any] = {
        "type": "competitor_price",
        "catalog_product_id": str(catalog_product_id),
        "competitor_product_id": str(competitor_product_id),
        "platform": platform,
        "price": str(price),
        "category": category,
    }
    if extra_meta:
        meta.update({k: str(v) for k, v in extra_meta.items() if v is not None and str(v).strip()})

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


def index_retailer_product(
    retailer_product_id: int,
    user_id: int,
    catalog_product_id: int,
    product_name: str,
    category: str = "",
    brand: str = "",
    product_details: str = "",
    cost_price: Optional[float] = None,
    stock_quantity: Optional[int] = None,
    minimum_profit_margin: Optional[float] = None,
    quantity_value: Optional[float] = None,
    quantity_unit: Optional[str] = None,
) -> None:
    """Upsert one retailer-owned product context document for chatbot use."""
    doc_id = f"retailer_{user_id}_{retailer_product_id}"
    text = (
        f"Retailer product: {product_name}\nCategory: {category}\nBrand: {brand}\n"
        f"Product details: {product_details}\nCost price: {cost_price}\n"
        f"Pack quantity: {quantity_value} {quantity_unit}\nStock quantity: {stock_quantity} packs\nMinimum profit margin: {minimum_profit_margin}%"
    )
    metadata = {
        "type": "retailer_product",
        "retailer_product_id": str(retailer_product_id),
        "user_id": str(user_id),
        "catalog_product_id": str(catalog_product_id),
        "product_name": product_name,
    }
    store_document(collection_name=CHATBOT_RAG_COLLECTION, doc_id=doc_id, text=text, metadata=metadata)


def delete_retailer_product_context(retailer_product_id: int, user_id: int) -> None:
    """Delete private product and recommendation docs, preserving shared catalog context."""
    delete_document(CHATBOT_RAG_COLLECTION, f"retailer_{user_id}_{retailer_product_id}")
    for recommendation_id in (f"rec_{user_id}_{retailer_product_id}", f"rec_{retailer_product_id}"):
        delete_document(PRICING_RAG_COLLECTION, recommendation_id)
        delete_document(CHATBOT_RAG_COLLECTION, f"pricing_{recommendation_id}")


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
    evidence_type: str = "NO_EVIDENCE",
    price_range_min: Optional[float] = None,
    price_range_max: Optional[float] = None,
) -> None:
    """Index a pricing recommendation into the pricing RAG collection."""
    doc_id = "rec_" + (str(user_id) + "_" if user_id is not None else "") + str(retailer_product_id)
    fields = [
        "Pricing recommendation for: " + product_name,
        "Recommended price: Rs." + str(round(recommended_price, 2)),
    ]
    if price_range_min is not None and price_range_max is not None:
        fields.append("Suggested price range: Rs." + str(round(price_range_min, 2)) + " - Rs." + str(round(price_range_max, 2)))
    fields.extend([
        "Recommendation saved for the retailer's product.",
    ])
    if market_avg > 0:
        fields.insert(2, "Market average: Rs." + str(round(market_avg, 2)))
    if min_price > 0 and max_price > 0:
        fields.insert(3, "Market range: Rs." + str(round(min_price, 2)) + " - Rs." + str(round(max_price, 2)))
    text = "\n".join(fields)
    meta: Dict[str, Any] = {
        "type": "pricing_recommendation",
        "retailer_product_id": str(retailer_product_id),
        "catalog_product_id": str(catalog_product_id),
        "product_name": product_name,
        "recommended_price": str(recommended_price),
        "recommended_price_min": str(price_range_min) if price_range_min is not None else "",
        "recommended_price_max": str(price_range_max) if price_range_max is not None else "",
        "evidence_type": evidence_type,
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
        n_results=max(n_results, 20),
        where=where_filter,
    )

    if not hits:
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
        def allowed_for_retailer(hit):
            metadata = hit.get("metadata", {})
            kind = metadata.get("type")
            if kind not in {"pricing_recommendation", "retailer_product"}:
                return True  # shared catalog and competitor observations
            return (user_id is not None
                    and metadata.get("retailer_product_id") == str(retailer_product_id)
                    and metadata.get("user_id") == str(user_id))
        hits = [hit for hit in hits if allowed_for_retailer(hit)]

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
    competitor_product_ids: Optional[List[int]] = None,
) -> str:
    """Retrieve relevant pricing context for the Strategist Agent."""
    where_filter = None
    if competitor_product_ids is None and catalog_product_id is not None:
        where_filter = {"catalog_product_id": str(catalog_product_id)}
    if competitor_product_ids is not None:
        if competitor_product_ids:
            where_filter = {"$and": [
                {"catalog_product_id": str(catalog_product_id)},
                {"competitor_product_id": {"$in": [str(value) for value in competitor_product_ids]}},
            ]}

    hits = retrieve_relevant(
        collection_name=PRICING_RAG_COLLECTION,
        query=query,
        n_results=max(n_results, 20) if competitor_product_ids == [] else n_results,
        where=where_filter,
    )

    if retailer_product_id is not None:
        def is_useful_pricing_hit(hit):
            metadata = hit.get("metadata", {})
            if metadata.get("type") != "pricing_recommendation":
                return True
            # A previous recommendation for this same product is an output,
            # not independent market evidence for the next estimate.
            if metadata.get("retailer_product_id") == str(retailer_product_id):
                return False
            return user_id is None or metadata.get("user_id") in (None, "", str(user_id))
        hits = [hit for hit in hits if is_useful_pricing_hit(hit)]

    if not hits:
        return ""

    parts = ["=== PRICING CONTEXT ==="]
    for i, hit in enumerate(hits, 1):
        parts.append("\n[Source " + str(i) + "]")
        parts.append(hit["text"])
    return "\n".join(parts)
