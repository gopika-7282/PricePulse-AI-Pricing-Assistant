"""
test_product_identity_rag.py
============================
Tests for the new Product Identity Agent (Qwen3 via Ollama) and ChromaDB RAG layer.

Product Identity Agent:
  PostgreSQL candidate retrieval
        |
        v
  Product Identity Agent
        |
        v
  Qwen3:8b via Ollama
        |
        v
  MATCH_EXISTING / UNCERTAIN_MATCH / NEW_PRODUCT

RAG Layer:
  ChromaDB text-document retrieval (no embedding models)
        |
        v
  pricing_rag / product_rag / chatbot_rag collections

Run with:
  cd E:\\GOPIKA\\PrintPulse\\backend
  ..\\venv\\Scripts\\python.exe -m pytest tests/test_product_identity_rag.py -v
"""

import os
import sys
import pytest
from unittest.mock import patch, MagicMock
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

# Ensure backend is in sys.path
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

# SQLite in-memory for unit tests (no Postgres required)
import app.config as cfg
cfg.DATABASE_URL = "sqlite:///:memory:"

TEST_ENGINE = create_engine("sqlite:///:memory:", connect_args={"check_same_thread": False})
TestingSession = sessionmaker(bind=TEST_ENGINE, autocommit=False, autoflush=False)

import app.database as db_module
db_module.engine = TEST_ENGINE
db_module.SessionLocal = TestingSession

from app.database import Base
from app.models.product_catalog import ProductCatalog

from app.services.product_identity_service import (
    evaluate_product_identity,
    IdentityDecision,
    retrieve_postgres_candidates,
    extract_search_tokens,
)


# ============================================================================
# Database Fixtures
# ============================================================================

@pytest.fixture(scope="session", autouse=True)
def create_tables():
    Base.metadata.create_all(bind=TEST_ENGINE)
    yield
    Base.metadata.drop_all(bind=TEST_ENGINE)


@pytest.fixture
def db():
    """Fresh transactional session that rolls back after each test."""
    connection = TEST_ENGINE.connect()
    transaction = connection.begin()
    session = TestingSession(bind=connection)
    yield session
    session.close()
    transaction.rollback()
    connection.close()


def make_catalog(db, name, category="Personal Care", brand="TestBrand", details=""):
    c = ProductCatalog(name=name, category=category, brand=brand, product_details=details)
    db.add(c)
    db.flush()
    return c


# ============================================================================
# Token Extraction Tests
# ============================================================================

class TestTokenExtraction:

    def test_basic_tokens(self):
        tokens = extract_search_tokens("Turmeric Soap")
        assert "turmeric" in tokens
        assert "soap" in tokens

    def test_stop_words_excluded(self):
        tokens = extract_search_tokens("natural organic herbal soap")
        assert "soap" in tokens
        # Stop words must be removed
        assert "natural" not in tokens
        assert "organic" not in tokens
        assert "herbal" not in tokens

    def test_short_tokens_excluded(self):
        tokens = extract_search_tokens("A BC soap")
        assert "soap" in tokens
        assert "a" not in tokens
        assert "bc" not in tokens

    def test_empty_input(self):
        tokens = extract_search_tokens("")
        assert tokens == []

    def test_multilingual_tokens(self):
        # Regional names should tokenize normally (Qwen3 handles the reasoning)
        tokens = extract_search_tokens("Haldi Soap 100g")
        assert "haldi" in tokens
        assert "soap" in tokens


# ============================================================================
# PostgreSQL Candidate Retrieval Tests
# ============================================================================

class TestPostgresCandidateRetrieval:

    def test_exact_name_match_found(self, db):
        """Exact name match should be in candidates."""
        make_catalog(db, "Turmeric Soap", details="Herbal bath soap")
        candidates = retrieve_postgres_candidates(db, "Turmeric Soap")
        names = [c.name for c in candidates]
        assert "Turmeric Soap" in names

    def test_no_match_empty_catalog(self, db):
        """Empty catalog returns empty candidate list."""
        candidates = retrieve_postgres_candidates(db, "Turmeric Soap")
        assert candidates == []

    def test_candidate_limit(self, db):
        """Candidates should be limited to at most 5."""
        for i in range(10):
            make_catalog(db, f"Soap Variant {i}", details="turmeric soap variant")
        candidates = retrieve_postgres_candidates(db, "Turmeric Soap", limit=5)
        assert len(candidates) <= 5

    def test_category_filtering(self, db):
        """Category match should boost candidate retrieval."""
        make_catalog(db, "Goat Milk Soap", category="Personal Care", details="handmade soap")
        make_catalog(db, "Goat Milk Shampoo", category="Hair Care", details="hair shampoo")
        candidates = retrieve_postgres_candidates(
            db, "Goat Milk Soap", category="Personal Care"
        )
        names = [c.name for c in candidates]
        assert "Goat Milk Soap" in names


# ============================================================================
# Product Identity Agent Tests (Mocked LLM)
# ============================================================================

class TestProductIdentityAgent:
    """
    Tests for evaluate_product_identity.
    LLM calls are mocked to avoid requiring Ollama during unit tests.
    """

    def _mock_llm_match(self, catalog_id):
        """Return a mock LLM response indicating MATCH_EXISTING."""
        return {
            "success": True,
            "data": {
                "decision": "MATCH_EXISTING",
                "matched_catalog_id": catalog_id,
                "confidence": 0.95,
                "reason": "Identical product name and description",
                "identity_factors": ["name_match", "description_match"],
            },
            "error": None,
        }

    def _mock_llm_not_match(self):
        """Return a mock LLM response indicating NEW_PRODUCT."""
        return {
            "success": True,
            "data": {
                "decision": "NEW_PRODUCT",
                "matched_catalog_id": None,
                "confidence": 0.97,
                "reason": "Different product type",
                "identity_factors": ["product_type_mismatch"],
            },
            "error": None,
        }

    def _mock_llm_uncertain(self):
        """Return a mock LLM response indicating UNCERTAIN_MATCH."""
        return {
            "success": True,
            "data": {
                "decision": "UNCERTAIN_MATCH",
                "matched_catalog_id": None,
                "confidence": 0.50,
                "reason": "Ambiguous product description",
                "identity_factors": ["ambiguous_name"],
            },
            "error": None,
        }

    # ------------------------------------------------------------------
    # MATCH scenarios
    # ------------------------------------------------------------------

    def test_identical_product_match(self, db):
        """MATCH: Identical name and description."""
        cat = make_catalog(db, "Turmeric Soap", details="Natural herbal turmeric soap 100g")
        with patch("app.services.product_identity_service.generate_structured",
                   return_value=self._mock_llm_match(cat.id)):
            result = evaluate_product_identity(
                db, "Turmeric Soap", product_details="Natural herbal turmeric soap 100g"
            )
        assert result.decision == IdentityDecision.MATCH_EXISTING
        assert result.matched_catalog_id == cat.id
        assert result.confidence >= 0.9

    def test_different_brand_same_product_match(self, db):
        """MATCH: Different brand, same product -> should match (brand neutral)."""
        cat = make_catalog(db, "Turmeric Soap", brand="Siva's Organic",
                           details="Natural turmeric soap")
        with patch("app.services.product_identity_service.generate_structured",
                   return_value=self._mock_llm_match(cat.id)):
            result = evaluate_product_identity(
                db, "Turmeric Soap", brand="Patanjali",
                product_details="Natural turmeric soap"
            )
        assert result.decision == IdentityDecision.MATCH_EXISTING
        assert result.matched_catalog_id == cat.id

    def test_spelling_variation_match(self, db):
        """MATCH: Spelling variations should match via Qwen3 reasoning."""
        cat = make_catalog(db, "Hibiscus Hair Oil", details="Herbal hibiscus hair oil 200ml")
        with patch("app.services.product_identity_service.generate_structured",
                   return_value=self._mock_llm_match(cat.id)):
            result = evaluate_product_identity(
                db, "Hibiscus Hair Oil", product_details="Herbal hibiscus hair oil"
            )
        assert result.decision == IdentityDecision.MATCH_EXISTING

    def test_multilingual_regional_name_match(self, db):
        """MATCH: Regional/multilingual names matched by Qwen3 reasoning."""
        cat = make_catalog(db, "Turmeric Soap", details="Natural herbal bathing soap 100g")
        with patch("app.services.product_identity_service.generate_structured",
                   return_value=self._mock_llm_match(cat.id)):
            result = evaluate_product_identity(
                db, "Haldi Soap", product_details="Natural herbal bathing soap 100g"
            )
        assert result.decision == IdentityDecision.MATCH_EXISTING

    def test_same_product_different_wording_match(self, db):
        """MATCH: Same product with different description wording."""
        cat = make_catalog(db, "Goat Milk Soap", details="Handmade goat milk soap bar")
        with patch("app.services.product_identity_service.generate_structured",
                   return_value=self._mock_llm_match(cat.id)):
            result = evaluate_product_identity(
                db, "Goat Milk Soap", product_details="Pure goat milk bathing bar"
            )
        assert result.decision == IdentityDecision.MATCH_EXISTING

    # ------------------------------------------------------------------
    # NOT_MATCH / NEW_PRODUCT scenarios
    # ------------------------------------------------------------------

    def test_different_product_type_no_match(self, db):
        """NOT_MATCH: Different product type -> Soap vs Shampoo."""
        make_catalog(db, "Turmeric Soap", details="Turmeric bath soap")
        with patch("app.services.product_identity_service.generate_structured",
                   return_value=self._mock_llm_not_match()):
            result = evaluate_product_identity(
                db, "Turmeric Shampoo", product_details="Turmeric hair shampoo"
            )
        assert result.decision == IdentityDecision.NEW_PRODUCT

    def test_completely_different_product_no_match(self, db):
        """NOT_MATCH: Completely different product -> Soap vs Chocolate Cake."""
        make_catalog(db, "Turmeric Soap", details="Herbal turmeric bath soap")
        with patch("app.services.product_identity_service.generate_structured",
                   return_value=self._mock_llm_not_match()):
            result = evaluate_product_identity(
                db, "Chocolate Cake", product_details="Rich dark chocolate cake"
            )
        assert result.decision == IdentityDecision.NEW_PRODUCT

    def test_different_ingredient_no_match(self, db):
        """NOT_MATCH: Different key ingredient -> Turmeric Soap vs Neem Soap."""
        make_catalog(db, "Turmeric Soap", details="Herbal turmeric soap")
        with patch("app.services.product_identity_service.generate_structured",
                   return_value=self._mock_llm_not_match()):
            result = evaluate_product_identity(
                db, "Neem Soap", product_details="Neem antibacterial soap"
            )
        assert result.decision == IdentityDecision.NEW_PRODUCT

    def test_same_category_different_product_no_match(self, db):
        """NOT_MATCH: Same category, clearly different product."""
        make_catalog(db, "Hibiscus Hair Oil", category="Hair Care",
                     details="Herbal hibiscus oil for hair growth")
        with patch("app.services.product_identity_service.generate_structured",
                   return_value=self._mock_llm_not_match()):
            result = evaluate_product_identity(
                db, "Coconut Hair Oil", category="Hair Care",
                product_details="Pure coconut oil for hair"
            )
        assert result.decision == IdentityDecision.NEW_PRODUCT

    # ------------------------------------------------------------------
    # UNCERTAIN_MATCH scenarios
    # ------------------------------------------------------------------

    def test_ambiguous_description_uncertain(self, db):
        """UNCERTAIN_MATCH: Ambiguous description that Qwen3 cannot confidently resolve."""
        make_catalog(db, "Herbal Face Wash", details="Gentle herbal face cleanser")
        with patch("app.services.product_identity_service.generate_structured",
                   return_value=self._mock_llm_uncertain()):
            result = evaluate_product_identity(
                db, "Herbal Cleanser", product_details="Natural gentle cleanser"
            )
        assert result.decision == IdentityDecision.UNCERTAIN_MATCH

    # ------------------------------------------------------------------
    # LLM Failure Fallback
    # ------------------------------------------------------------------

    def test_llm_failure_falls_back_to_uncertain(self, db):
        """LLM unavailable -> should fall back to UNCERTAIN_MATCH (never crash)."""
        make_catalog(db, "Turmeric Soap", details="Herbal turmeric soap")
        with patch("app.services.product_identity_service.generate_structured",
                   return_value={"success": False, "data": None, "error": "Ollama unavailable"}):
            result = evaluate_product_identity(
                db, "Turmeric Soap", product_details="Herbal turmeric soap"
            )
        assert result.decision == IdentityDecision.UNCERTAIN_MATCH
        assert result.confidence == 0.0

    def test_empty_catalog_returns_new_product(self, db):
        """Empty catalog -> always NEW_PRODUCT without LLM call."""
        result = evaluate_product_identity(
            db, "Brand New Product", product_details="Something entirely new"
        )
        assert result.decision == IdentityDecision.NEW_PRODUCT

    def test_invalid_llm_id_rejected(self, db):
        """LLM returns ID not in candidate set -> forced UNCERTAIN_MATCH."""
        cat = make_catalog(db, "Turmeric Soap", details="Herbal turmeric soap")
        fake_id = cat.id + 9999  # ID that does not exist in candidates
        with patch("app.services.product_identity_service.generate_structured",
                   return_value={
                       "success": True,
                       "data": {
                           "decision": "MATCH_EXISTING",
                           "matched_catalog_id": fake_id,
                           "confidence": 0.95,
                           "reason": "Hallucinated match",
                           "identity_factors": [],
                       },
                       "error": None,
                   }):
            result = evaluate_product_identity(
                db, "Turmeric Soap", product_details="Herbal turmeric soap"
            )
        assert result.decision == IdentityDecision.UNCERTAIN_MATCH


# ============================================================================
# ChromaDB RAG Tests
# ============================================================================

class TestChromaRAG:
    """
    Tests for the new RAG-oriented chroma_service.
    Uses an in-memory/tmp ChromaDB for isolation.
    Does NOT require any external embedding model.
    """

    @pytest.fixture(autouse=True)
    def patch_chroma_dir(self, tmp_path, monkeypatch):
        """Use a temp directory for ChromaDB during tests."""
        import app.services.ai.chroma_service as chroma_svc
        monkeypatch.setattr(chroma_svc, "CHROMA_PERSIST_DIR", str(tmp_path / "test_chroma"))
        monkeypatch.setattr(chroma_svc, "_chroma_client", None)  # Reset singleton

    def test_store_and_retrieve_document(self):
        """Basic: Store a document and retrieve it by query."""
        from app.services.ai.chroma_service import (
            store_document, retrieve_relevant, PRICING_RAG_COLLECTION
        )

        store_document(
            collection_name=PRICING_RAG_COLLECTION,
            doc_id="test_doc_1",
            text="Competitor product: Turmeric Soap\nPlatform: Flipkart\nPrice: Rs.150.0",
            metadata={"type": "competitor_price", "platform": "Flipkart"},
        )

        results = retrieve_relevant(
            collection_name=PRICING_RAG_COLLECTION,
            query="Turmeric Soap competitor price Flipkart",
            n_results=3,
        )

        assert len(results) >= 1
        assert any("Turmeric Soap" in r["text"] for r in results)

    def test_empty_collection_returns_empty(self):
        """Empty collection returns empty list, no errors."""
        from app.services.ai.chroma_service import retrieve_relevant, PRODUCT_RAG_COLLECTION

        results = retrieve_relevant(
            collection_name=PRODUCT_RAG_COLLECTION,
            query="anything",
        )
        assert results == []

    def test_store_documents_batch(self):
        """Batch store and retrieve multiple documents."""
        from app.services.ai.chroma_service import (
            store_documents_batch, retrieve_relevant, PRODUCT_RAG_COLLECTION
        )

        docs = [
            {"id": "prod_1", "text": "Product: Hibiscus Hair Oil\nCategory: Hair Care", "metadata": {"type": "catalog_product"}},
            {"id": "prod_2", "text": "Product: Coconut Hair Oil\nCategory: Hair Care", "metadata": {"type": "catalog_product"}},
            {"id": "prod_3", "text": "Product: Neem Face Wash\nCategory: Personal Care", "metadata": {"type": "catalog_product"}},
        ]

        count = store_documents_batch(PRODUCT_RAG_COLLECTION, docs)
        assert count == 3

        results = retrieve_relevant(PRODUCT_RAG_COLLECTION, "Hair Oil products", n_results=5)
        assert len(results) >= 1

    def test_index_competitor_price(self):
        """index_competitor_price stores in both pricing_rag and chatbot_rag."""
        from app.services.ai.chroma_service import (
            index_competitor_price, retrieve_relevant,
            PRICING_RAG_COLLECTION, CHATBOT_RAG_COLLECTION
        )

        index_competitor_price(
            catalog_product_id=1,
            competitor_product_id=101,
            platform="Amazon",
            product_name="Turmeric Face Soap",
            price=199.0,
            category="Personal Care",
        )

        pricing_results = retrieve_relevant(PRICING_RAG_COLLECTION, "Turmeric Face Soap price")
        chatbot_results = retrieve_relevant(CHATBOT_RAG_COLLECTION, "Turmeric Face Soap price")

        assert len(pricing_results) >= 1
        assert len(chatbot_results) >= 1

    def test_retrieve_chatbot_context_returns_string(self):
        """retrieve_chatbot_context returns a formatted string."""
        from app.services.ai.chroma_service import (
            index_competitor_price, retrieve_chatbot_context
        )

        index_competitor_price(
            catalog_product_id=2,
            competitor_product_id=202,
            platform="Flipkart",
            product_name="Hibiscus Hair Oil 200ml",
            price=350.0,
            category="Hair Care",
        )

        context = retrieve_chatbot_context(
            query="What is the Flipkart price for Hibiscus Hair Oil?",
            catalog_product_id=2,
        )

        assert isinstance(context, str)
        # When documents are available, context should not be empty
        assert len(context) > 0

    def test_retrieve_pricing_context_returns_string(self):
        """retrieve_pricing_context returns a formatted string."""
        from app.services.ai.chroma_service import (
            index_pricing_recommendation, retrieve_pricing_context
        )

        index_pricing_recommendation(
            retailer_product_id=10,
            catalog_product_id=3,
            product_name="Goat Milk Soap",
            recommended_price=299.0,
            reasoning="Market average is Rs.280. Price set at competitive level.",
            market_avg=280.0,
            min_price=220.0,
            max_price=350.0,
        )

        context = retrieve_pricing_context(
            query="Goat Milk Soap pricing recommendation",
            catalog_product_id=3,
        )

        assert isinstance(context, str)
        assert len(context) > 0

    def test_index_product_catalog(self):
        """index_product_catalog stores in product_rag and chatbot_rag."""
        from app.services.ai.chroma_service import (
            index_product_catalog, retrieve_relevant,
            PRODUCT_RAG_COLLECTION, CHATBOT_RAG_COLLECTION
        )

        index_product_catalog(
            catalog_product_id=5,
            product_name="Neem Face Wash",
            category="Personal Care",
            product_details="Antibacterial neem face wash for acne control",
        )

        prod_results = retrieve_relevant(PRODUCT_RAG_COLLECTION, "Neem Face Wash")
        chatbot_results = retrieve_relevant(CHATBOT_RAG_COLLECTION, "Neem Face Wash")

        assert len(prod_results) >= 1
        assert len(chatbot_results) >= 1

    def test_upsert_is_idempotent(self):
        """Calling store_document multiple times with same id does not duplicate."""
        from app.services.ai.chroma_service import (
            store_document, get_collection, PRICING_RAG_COLLECTION
        )

        for i in range(3):
            store_document(
                collection_name=PRICING_RAG_COLLECTION,
                doc_id="idempotent_test",
                text="Test document text version " + str(i),
                metadata={"iteration": str(i)},
            )

        collection = get_collection(PRICING_RAG_COLLECTION)
        # Should still have only 1 document with this ID
        result = collection.get(ids=["idempotent_test"])
        assert len(result["ids"]) == 1

    def test_empty_query_returns_empty(self):
        """Empty query string returns empty list without error."""
        from app.services.ai.chroma_service import retrieve_relevant, CHATBOT_RAG_COLLECTION

        results = retrieve_relevant(CHATBOT_RAG_COLLECTION, "")
        assert results == []

    def test_delete_document(self):
        """Deleted document should not be returned in future queries."""
        from app.services.ai.chroma_service import (
            store_document, delete_document, get_collection, PRODUCT_RAG_COLLECTION
        )

        store_document(
            collection_name=PRODUCT_RAG_COLLECTION,
            doc_id="delete_test",
            text="To be deleted document",
            metadata={},
        )

        collection = get_collection(PRODUCT_RAG_COLLECTION)
        before_count = collection.count()

        delete_document(PRODUCT_RAG_COLLECTION, "delete_test")

        after_count = collection.count()
        assert after_count == before_count - 1
