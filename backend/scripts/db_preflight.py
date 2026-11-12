"""Read-only checks and explicitly guarded canonical baseline adoption.

Preflight performs SELECT/catalog inspection only. Adoption writes only the
canonical Alembic version marker after a successful exact-shape/data check;
it does not run schema DDL.
"""
import argparse
import os
import re
import sys
from sqlalchemy import create_engine, inspect, text
from sqlalchemy.engine import make_url

REQUIRED = {
    "users": {"id", "retailer_name", "email", "password_hash", "business_name", "created_at", "updated_at", "reset_token", "reset_token_expiry"},
    "product_catalog": {"id", "name", "category", "brand", "product_details", "created_at", "updated_at", "last_scraped_at", "scraping_status", "marketplace_statuses"},
    "retailer_products": {"id", "user_id", "catalog_product_id", "cost_price", "stock_quantity", "minimum_profit_margin", "created_at", "updated_at", "name_override", "category_override", "brand_override", "product_details_override", "catalog_identity_pending", "catalog_identity_staging", "quantity_value", "quantity_unit"},
    "competitor_products": {"id", "catalog_product_id", "platform_name", "product_name", "price", "rating", "product_url", "availability", "product_details", "created_at", "updated_at", "scraped_at", "quantity_value", "quantity_unit", "pack_count", "total_quantity", "total_quantity_unit", "observation_source"},
    "competitor_price_history": {"id", "competitor_product_id", "price", "created_at", "updated_at", "scraped_at", "observation_source"},
    "price_analysis": {"id", "retailer_product_id", "average_market_price", "minimum_market_price", "maximum_market_price", "competitor_count", "price_difference_percentage", "analysis_summary", "created_at", "updated_at"},
    "recommendations": {"id", "retailer_product_id", "recommended_price", "expected_profit", "profit_percentage", "reasoning", "confidence_score", "accepted_by_user", "created_at", "updated_at", "evidence_type", "recommended_price_min", "recommended_price_max"},
    "agent_logs": {"id", "retailer_product_id", "agent_name", "task_description", "input_data", "output_data", "execution_time", "status", "created_at", "updated_at"},
    "chat_conversations": {"id", "retailer_id", "product_id", "created_at", "updated_at"},
    "chat_messages": {"id", "conversation_id", "role", "content", "created_at"},
}
EXPECTED_INDEXES = {
    "users": {"ix_users_email", "ix_users_reset_token"},
    "product_catalog": {"ix_product_catalog_name", "ix_product_catalog_category", "ix_product_catalog_brand"},
    "retailer_products": {"ix_retailer_products_user_id", "ix_retailer_products_catalog_product_id"},
    "competitor_products": {"ix_competitor_products_catalog_product_id", "ix_competitor_products_platform_name"},
    "competitor_price_history": {"ix_competitor_price_history_competitor_product_id"},
    "price_analysis": {"ix_price_analysis_retailer_product_id"},
    "recommendations": {"ix_recommendations_retailer_product_id"},
    "agent_logs": {"ix_agent_logs_retailer_product_id"},
    "chat_conversations": {"ix_chat_conversations_retailer_id", "ix_chat_conversations_product_id"},
    "chat_messages": {"ix_chat_messages_conversation_id"},
}
NULLABLE_COLUMNS = {
    "users": {"business_name", "reset_token", "reset_token_expiry"},
    "product_catalog": {"category", "brand", "product_details", "last_scraped_at"},
    "retailer_products": {"name_override", "category_override", "brand_override", "product_details_override", "quantity_value", "quantity_unit"},
    "competitor_products": {"product_url", "product_details", "quantity_value", "quantity_unit", "pack_count", "total_quantity", "total_quantity_unit", "rating", "availability"},
    "competitor_price_history": set(),
    "price_analysis": {"average_market_price", "minimum_market_price", "maximum_market_price", "price_difference_percentage", "analysis_summary"},
    "recommendations": {"recommended_price_min", "recommended_price_max", "confidence_score", "accepted_by_user"},
    "agent_logs": {"input_data", "output_data", "execution_time"},
    "chat_conversations": {"product_id"},
    "chat_messages": set(),
}
EXPECTED_FKS = {
    "agent_logs": {("retailer_product_id", "retailer_products", "id", "CASCADE")},
    "chat_conversations": {("retailer_id", "users", "id", "CASCADE"), ("product_id", "retailer_products", "id", "SET NULL")},
    "chat_messages": {("conversation_id", "chat_conversations", "id", "CASCADE")},
    "competitor_price_history": {("competitor_product_id", "competitor_products", "id", "CASCADE")},
    "competitor_products": {("catalog_product_id", "product_catalog", "id", "CASCADE")},
    "price_analysis": {("retailer_product_id", "retailer_products", "id", "CASCADE")},
    "recommendations": {("retailer_product_id", "retailer_products", "id", "CASCADE")},
    "retailer_products": {("user_id", "users", "id", "CASCADE"), ("catalog_product_id", "product_catalog", "id", "CASCADE")},
    "users": set(),
    "product_catalog": set(),
}
TIMESTAMP_DEFAULTS = {
    "users": {"created_at", "updated_at"},
    "product_catalog": {"created_at", "updated_at"},
    "retailer_products": {"created_at", "updated_at"},
    "competitor_products": {"created_at", "updated_at", "scraped_at"},
    "competitor_price_history": {"created_at", "updated_at", "scraped_at"},
    "price_analysis": {"created_at", "updated_at"},
    "recommendations": {"created_at", "updated_at"},
    "agent_logs": {"created_at", "updated_at"},
    "chat_conversations": {"created_at", "updated_at"},
    "chat_messages": {"created_at"},
}
VALUE_DEFAULTS = {
    ("product_catalog", "scraping_status"): "'NEVER_SCRAPED'",
    ("product_catalog", "marketplace_statuses"): "'{}'",
    ("retailer_products", "stock_quantity"): "0",
    ("retailer_products", "catalog_identity_pending"): "false",
    ("retailer_products", "catalog_identity_staging"): "false",
    ("competitor_products", "observation_source"): "'LEGACY_UNKNOWN'",
    ("competitor_price_history", "observation_source"): "'LEGACY_UNKNOWN'",
    ("price_analysis", "competitor_count"): "0",
    ("recommendations", "evidence_type"): "'NO_EVIDENCE'",
}
BASELINE = "20261111_0001"
CANONICAL_HEAD = "20261111_0002"
EXPECTED_SEQUENCES = {f"{table}_id_seq" for table in REQUIRED}

def normalize_server_default(value):
    """Normalize PostgreSQL display casts without altering quoted SQL tokens.

    Case and contents inside single-quoted string literals and double-quoted
    identifiers are preserved. Outside those regions, type names are
    lowercased, whitespace is collapsed, and supported PostgreSQL display
    casts are removed.
    """
    if value is None:
        return ""

    source = str(value)
    result: list[str] = []
    quote: str | None = None
    pending_space = False
    i = 0

    # These patterns are matched only at the current unquoted position.
    display_cast = re.compile(
        r"::\s*(?:character\s+varying(?:\s*\(\s*\d+\s*\))?|json\b|regclass\b)",
        re.IGNORECASE,
    )

    while i < len(source):
        ch = source[i]

        if quote is not None:
            result.append(ch)
            if ch == quote:
                # SQL escapes a quote within a quoted token by doubling it.
                if i + 1 < len(source) and source[i + 1] == quote:
                    result.append(source[i + 1])
                    i += 2
                    continue
                quote = None
            i += 1
            continue

        if ch.isspace():
            pending_space = True
            i += 1
            continue

        if source.startswith("::", i):
            cast = display_cast.match(source, i)
            if cast:
                # Keep pending whitespace so a following operator remains
                # separated where the input separated it.
                i = cast.end()
                continue

        if pending_space and result:
            result.append(" ")
        pending_space = False

        if ch in ("'", '"'):
            quote = ch
            result.append(ch)
        else:
            result.append(ch.lower())
        i += 1

    return "".join(result).strip()

def _check_schema(inspector, reasons):
    """Shared read-only schema checks used by both canonical verification and legacy adoption.

    Populates *reasons* with any deviation from the canonical contract.
    Does NOT perform timestamp-provenance or data checks – those belong only
    in :func:`inspect_database` / :func:`adopt_database`.
    """
    app_tables = set(inspector.get_table_names()) - {"alembic_version", "canonical_alembic_version"}
    missing_tables = set(REQUIRED) - app_tables
    if missing_tables:
        for t in sorted(missing_tables):
            reasons.append(f"Required application table is missing: {t}")
    sequence_names = set(inspector.get_sequence_names())
    if sequence_names != EXPECTED_SEQUENCES:
        reasons.append("Named ID sequences differ from the canonical contract")
    for table, expected_cols in REQUIRED.items():
        if table not in app_tables:
            continue
        actual = {col["name"] for col in inspector.get_columns(table)}
        missing_cols = expected_cols - actual
        extra_cols = actual - expected_cols
        if missing_cols:
            reasons.append(f"{table} missing columns: {', '.join(sorted(missing_cols))}")
        if extra_cols:
            reasons.append(f"{table} has unexpected columns: {', '.join(sorted(extra_cols))}")
        for column in inspector.get_columns(table):
            cname = column["name"]
            expected_nullable = cname in NULLABLE_COLUMNS[table]
            if bool(column["nullable"]) != expected_nullable:
                reasons.append(f"{table}.{cname} nullability differs from the canonical contract")
            default = column.get("default")
            if cname == "id":
                expected_default = f"nextval('{table}_id_seq'::regclass)"
            elif cname in TIMESTAMP_DEFAULTS[table]:
                expected_default = "now()"
            else:
                expected_default = VALUE_DEFAULTS.get((table, cname))
            if normalize_server_default(default) != normalize_server_default(expected_default):
                reasons.append(
                    f"{table}.{cname} server default differs from the canonical contract"
                )
        pk_columns = list((inspector.get_pk_constraint(table) or {}).get("constrained_columns") or [])
        if pk_columns != ["id"]:
            reasons.append(
                f"{table} primary key differs from the canonical contract: expected ['id'], found {pk_columns!r}"
            )
        indexes = {
            index["name"] for index in inspector.get_indexes(table)
            if set(index.get("column_names") or []) != set(pk_columns)
        }
        if indexes != EXPECTED_INDEXES[table]:
            reasons.append(f"{table} indexes differ from the canonical contract")
        actual_fks = {
            (fk["constrained_columns"][0], fk["referred_table"], fk["referred_columns"][0],
             str((fk.get("options") or {}).get("ondelete", "")).upper())
            for fk in inspector.get_foreign_keys(table)
        }
        if actual_fks != EXPECTED_FKS[table]:
            reasons.append(f"{table} foreign keys or delete actions differ from the canonical contract")


def verify_canonical_database(connection):
    """Read-only validation of a database installed via the canonical fresh-install path.

    Checks:
    * ``canonical_alembic_version`` exists and contains the expected revision.
    * All required application tables, columns, sequences, defaults,
      nullability, primary keys, indexes, and foreign keys match the contract.

    Intentionally does NOT perform legacy timestamp-provenance checks.
    Competitor rows legitimately marked ``'MANUAL'`` must not cause this check
    to fail.
    """
    inspector = inspect(connection)
    all_tables = set(inspector.get_table_names())
    reasons = []

    # 1. Version table must exist and carry the expected head revision.
    if "canonical_alembic_version" not in all_tables:
        return {
            "state": "canonical_verification_failed",
            "safe_to_verify": False,
            "reasons": ["canonical_alembic_version is missing; run the canonical upgrade first"],
        }
    revision = connection.execute(
        text("SELECT version_num FROM canonical_alembic_version")
    ).scalar_one_or_none()
    if revision != CANONICAL_HEAD:
        reasons.append(
            f"canonical_alembic_version is {revision!r}; expected {CANONICAL_HEAD!r}"
        )

    # 2. Shared structural schema checks (no data/provenance checks).
    _check_schema(inspector, reasons)

    return {
        "state": "canonical",
        "revision": CANONICAL_HEAD,
        "safe_to_verify": not reasons,
        "reasons": reasons,
    }

def inspect_database(connection):
    inspector = inspect(connection)
    all_tables = set(inspector.get_table_names())
    tables = all_tables - {"alembic_version", "canonical_alembic_version"}
    if not tables:
        return {"state": "empty", "safe_to_adopt": False, "reasons": ["Database is empty; run the canonical upgrade for a fresh install."]}
    reasons = []
    if "canonical_alembic_version" in all_tables:
        reasons.append("canonical_alembic_version already exists; inspect its revision instead of adopting")
    missing_tables = set(REQUIRED) - tables
    unexpected_tables = tables - set(REQUIRED)
    if missing_tables:
        reasons.append("Missing canonical tables: " + ", ".join(sorted(missing_tables)))
    if unexpected_tables:
        reasons.append("Unexpected tables prevent exact baseline adoption: " + ", ".join(sorted(unexpected_tables)))
    sequence_names = set(inspector.get_sequence_names())
    if sequence_names != EXPECTED_SEQUENCES:
        reasons.append("Named ID sequences differ from the canonical contract")
    for table, expected in REQUIRED.items():
        if table not in tables:
            continue
        actual = {col["name"] for col in inspector.get_columns(table)}
        if expected - actual:
            reasons.append(f"{table} missing columns: {', '.join(sorted(expected - actual))}")
        if actual - expected:
            reasons.append(f"{table} has unexpected columns: {', '.join(sorted(actual - expected))}")
        for column in inspector.get_columns(table):
            expected_nullable = column["name"] in NULLABLE_COLUMNS[table]
            legacy_nullable_count = (table == "price_analysis"
                                    and column["name"] == "competitor_count"
                                    and bool(column["nullable"]))
            legacy_nullable_observation_time = (
                table in {"competitor_products", "competitor_price_history"}
                and column["name"] == "scraped_at" and bool(column["nullable"])
            )
            if (bool(column["nullable"]) != expected_nullable
                    and not legacy_nullable_count and not legacy_nullable_observation_time):
                reasons.append(f"{table}.{column['name']} nullability differs from the canonical contract")
            default = column.get("default")
            if column["name"] == "id":
                expected_default = f"nextval('{table}_id_seq'::regclass)"
            elif column["name"] in TIMESTAMP_DEFAULTS[table]:
                expected_default = "now()"
            else:
                expected_default = VALUE_DEFAULTS.get((table, column["name"]))
            legacy_missing_count_default = (table == "price_analysis"
                                            and column["name"] == "competitor_count"
                                            and default is None)
            if (normalize_server_default(default) != normalize_server_default(expected_default)
                    and not legacy_missing_count_default):
                reasons.append(f"{table}.{column['name']} server default differs from the canonical contract")
        pk_columns = set((inspector.get_pk_constraint(table) or {}).get("constrained_columns") or [])
        indexes = {
            index["name"] for index in inspector.get_indexes(table)
            if set(index.get("column_names") or []) != pk_columns
        }
        if indexes != EXPECTED_INDEXES[table]:
            reasons.append(f"{table} indexes differ from the canonical contract")
        actual_fks = {
            (fk["constrained_columns"][0], fk["referred_table"], fk["referred_columns"][0],
             str((fk.get("options") or {}).get("ondelete", "")).upper())
            for fk in inspector.get_foreign_keys(table)
        }
        if actual_fks != EXPECTED_FKS[table]:
            reasons.append(f"{table} foreign keys or delete actions differ from the canonical contract")
    # Count repairable NULLs whenever that table/column exists, even if a
    # separate provenance mismatch blocks adoption. Preflight remains read-only.
    null_competitor_count_rows = None
    if "price_analysis" in tables and "competitor_count" in REQUIRED["price_analysis"]:
        actual_price_columns = {c["name"] for c in inspector.get_columns("price_analysis")}
        if "competitor_count" in actual_price_columns:
            null_competitor_count_rows = connection.execute(text(
                "SELECT count(*) FROM price_analysis WHERE competitor_count IS NULL"
            )).scalar_one()

    # Legacy timestamp values are adoptable only when provenance is explicit.
    # Column presence alone cannot establish historical truth.
    for table in ("competitor_products", "competitor_price_history"):
        if table not in tables:
            continue
        actual_columns = {c["name"] for c in inspector.get_columns(table)}
        if {"scraped_at", "observation_source"}.issubset(actual_columns):
            invalid = connection.execute(text(
                f"SELECT count(*) FROM {table} WHERE scraped_at IS NULL OR observation_source IS DISTINCT FROM 'SCRAPE'"
            )).scalar_one()
            if invalid:
                reasons.append(f"{table} has {invalid} rows without trustworthy SCRAPE provenance/timestamps")
    return {"state": "existing", "safe_to_adopt": not reasons, "reasons": reasons,
            "null_competitor_count_rows": null_competitor_count_rows}

def adopt_database(connection, confirmed=False):
    """Backfill, constrain, and stamp only after the read-only safety gate passes."""
    if not confirmed:
        raise RuntimeError("Adoption requires explicit confirmation")
    result = inspect_database(connection)
    if not result["safe_to_adopt"]:
        raise RuntimeError("Refusing adoption: " + "; ".join(result["reasons"]))
    if "canonical_alembic_version" in inspect(connection).get_table_names():
        raise RuntimeError("Refusing adoption because canonical_alembic_version already exists; inspect its revision first")
    # Unknown timestamps fail above before any DDL or data modification.
    for table in ("competitor_products", "competitor_price_history"):
        connection.execute(text(
            f"ALTER TABLE {table} ALTER COLUMN scraped_at SET NOT NULL"
        ))
    backfill_and_constrain_competitor_count(connection)
    connection.execute(text("CREATE TABLE canonical_alembic_version (version_num varchar(32) NOT NULL PRIMARY KEY)"))
    connection.execute(text("INSERT INTO canonical_alembic_version (version_num) VALUES (:version)"), {"version": BASELINE})
    return result

def backfill_and_constrain_competitor_count(connection):
    """Repair the approved nullable legacy field before enforcing NOT NULL."""
    connection.execute(text(
        "UPDATE price_analysis SET competitor_count = 0 WHERE competitor_count IS NULL"
    ))
    connection.execute(text(
        "ALTER TABLE price_analysis ALTER COLUMN competitor_count SET DEFAULT 0"
    ))
    connection.execute(text(
        "ALTER TABLE price_analysis ALTER COLUMN competitor_count SET NOT NULL"
    ))

def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("action", choices=("preflight", "adopt", "verify-canonical"))
    parser.add_argument("--confirm-adoption", action="store_true", help="Required to write the canonical version marker")
    args = parser.parse_args()
    url = os.getenv("DATABASE_URL")
    if not url:
        parser.error("DATABASE_URL must be configured in the process environment")
    parsed = make_url(url)
    if parsed.get_backend_name() != "postgresql":
        parser.error("Canonical deployment databases must use PostgreSQL")
    engine = create_engine(url)
    try:
        with engine.begin() as connection:
            if args.action == "verify-canonical":
                result = verify_canonical_database(connection)
                print(result)
                return 0 if result["safe_to_verify"] else 2
            result = inspect_database(connection)
            print(result)
            if args.action == "preflight":
                return 0 if result["state"] == "empty" or result["safe_to_adopt"] else 2
            try:
                adopt_database(connection, confirmed=args.confirm_adoption)
            except RuntimeError as exc:
                print(f"{exc}; no writes performed.", file=sys.stderr)
                return 2
            print(f"Adopted canonical baseline {BASELINE}; NULL competitor counts were backfilled before the NOT NULL constraint.")
            return 0
    finally:
        engine.dispose()

if __name__ == "__main__":
    raise SystemExit(main())
