"""PostgreSQL-only migration safety checks; enabled only for an explicit test URL."""
import os

import pytest
from sqlalchemy import create_engine, inspect, text

from scripts.db_preflight import (
    adopt_database,
    backfill_and_constrain_competitor_count,
    inspect_database,
)

TEST_DATABASE_URL = os.getenv("PRICEPULSE_TEST_DATABASE_URL")
pytestmark = pytest.mark.skipif(
    not TEST_DATABASE_URL or not TEST_DATABASE_URL.startswith("postgresql"),
    reason="Set PRICEPULSE_TEST_DATABASE_URL to an isolated PostgreSQL test database",
)

def test_postgres_preflight_fails_closed_on_unknown_provenance_before_writes():
    engine = create_engine(TEST_DATABASE_URL)
    try:
        with engine.connect() as connection:
            with connection.begin_nested():
                catalog_id = connection.execute(
                    text("INSERT INTO product_catalog (name) VALUES ('preflight-safety-test') RETURNING id")
                ).scalar_one()
                connection.execute(text(
                    "INSERT INTO competitor_products "
                    "(catalog_product_id, platform_name, product_name, price, observation_source) "
                    "VALUES (:catalog_id, 'test', 'unknown provenance', 1, 'LEGACY_UNKNOWN')"
                ), {"catalog_id": catalog_id})

                before_version = connection.execute(text(
                    "SELECT version_num FROM canonical_alembic_version"
                )).scalar_one()
                result = inspect_database(connection)
                assert not result["safe_to_adopt"]
                assert any("trustworthy SCRAPE provenance" in reason for reason in result["reasons"])
                with pytest.raises(RuntimeError, match="trustworthy"):
                    adopt_database(connection, confirmed=True)
                assert connection.execute(text(
                    "SELECT version_num FROM canonical_alembic_version"
                )).scalar_one() == before_version
            connection.rollback()
    finally:
        engine.dispose()

def test_postgres_backfill_sets_null_counts_to_zero_before_not_null():
    engine = create_engine(TEST_DATABASE_URL)
    try:
        with engine.connect() as connection:
            with connection.begin_nested():
                connection.execute(text(
                    "ALTER TABLE price_analysis ALTER COLUMN competitor_count DROP NOT NULL"
                ))
                user_id = connection.execute(text(
                    "INSERT INTO users (retailer_name, email, password_hash) "
                    "VALUES ('test', 'contract-test@example.invalid', 'unused') RETURNING id"
                )).scalar_one()
                catalog_id = connection.execute(text(
                    "INSERT INTO product_catalog (name) VALUES ('contract-test') RETURNING id"
                )).scalar_one()
                retailer_product_id = connection.execute(text(
                    "INSERT INTO retailer_products "
                    "(user_id, catalog_product_id, cost_price, stock_quantity, minimum_profit_margin) "
                    "VALUES (:user_id, :catalog_id, 1, 0, 0) RETURNING id"
                ), {"user_id": user_id, "catalog_id": catalog_id}).scalar_one()
                connection.execute(text(
                    "INSERT INTO price_analysis (retailer_product_id, competitor_count) "
                    "VALUES (:retailer_product_id, NULL)"
                ), {"retailer_product_id": retailer_product_id})

                backfill_and_constrain_competitor_count(connection)
                assert connection.execute(text(
                    "SELECT competitor_count FROM price_analysis WHERE retailer_product_id=:id"
                ), {"id": retailer_product_id}).scalar_one() == 0
                column = next(c for c in inspect(connection).get_columns("price_analysis")
                              if c["name"] == "competitor_count")
                assert column["nullable"] is False
            connection.rollback()
    finally:
        engine.dispose()
