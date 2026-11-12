"""Independent static comparison: PostgreSQL dump, approved deltas, and Alembic DDL."""
from importlib.util import module_from_spec, spec_from_file_location
from pathlib import Path
import re

from scripts.db_preflight import EXPECTED_FKS, EXPECTED_INDEXES, EXPECTED_SEQUENCES, REQUIRED

ROOT = Path(__file__).resolve().parents[2]
DUMP = (ROOT / "pricepulse_schema.sql").read_text(encoding="utf-8")
REVISION = ROOT / "backend" / "alembic_canonical" / "versions" / "20261111_0001_canonical_baseline.py"
spec = spec_from_file_location("canonical_baseline", REVISION)
baseline = module_from_spec(spec)
spec.loader.exec_module(baseline)

def table_columns(sql, table):
    match = re.search(rf"CREATE TABLE (?:public\.)?{re.escape(table)}\s*\((.*?)\n\)", sql, re.S | re.I)
    if not match:
        return set()
    columns = set()
    for line in match.group(1).splitlines():
        line = line.strip().rstrip(",")
        if not line or line.upper().startswith(("PRIMARY KEY", "FOREIGN KEY", "CONSTRAINT", "UNIQUE ")):
            continue
        columns.add(line.split()[0].strip('"'))
    return columns

def column_specs(sql, table):
    match = re.search(rf"CREATE TABLE (?:public\.)?{re.escape(table)}\s*\((.*?)\n\)", sql, re.S | re.I)
    if not match:
        return {}
    specs = {}
    for raw_line in match.group(1).splitlines():
        line = raw_line.strip().rstrip(",")
        if not line or line.upper().startswith(("PRIMARY KEY", "FOREIGN KEY", "CONSTRAINT", "UNIQUE ")):
            continue
        name, definition = line.split(None, 1)
        not_null = bool(re.search(r"\s+NOT NULL$", definition, re.I))
        definition = re.sub(r"\s+NOT NULL$", "", definition, flags=re.I)
        default = None
        default_match = re.search(r"\s+DEFAULT\s+(.*)$", definition, re.I)
        if default_match:
            default = default_match.group(1).strip()
            definition = definition[:default_match.start()].strip()
        specs[name.strip('"')] = (definition, not_null, default)
    return specs

def normalize_type(value):
    value = re.sub(r"\s+", " ", value.strip().lower())
    value = value.replace("character varying", "varchar").replace("double precision", "float")
    return value

def normalize_default(value):
    if value is None:
        return None
    value = value.lower().replace("::character varying", "").replace("::json", "")
    return re.sub(r"\s+", "", value).strip("()")

def foreign_keys(sql, schema_prefix="public."):
    found = set()
    explicit = (r"ALTER TABLE ONLY public\.(\w+)\s+ADD CONSTRAINT [^\s]+ FOREIGN KEY \((\w+)\) "
                r"REFERENCES public\.(\w+)\((\w+)\) ON DELETE (CASCADE|SET NULL|NO ACTION|RESTRICT)")
    found.update((table, column, target, target_column, action.upper())
                 for table, column, target, target_column, action in re.findall(explicit, sql, re.I))
    table_pattern = r"CREATE TABLE (?:public\.)?(\w+)\s*\((.*?)\n\)"
    fk_pattern = r"FOREIGN KEY\s*\((\w+)\)\s+REFERENCES\s+(?:public\.)?(\w+)\s*\((\w+)\)\s+ON DELETE\s+(CASCADE|SET NULL|NO ACTION|RESTRICT)"
    for table, body in re.findall(table_pattern, sql, re.S | re.I):
        found.update((table, column, target, target_column, action.upper())
                     for column, target, target_column, action in re.findall(fk_pattern, body, re.I))
    return found

def test_dump_and_canonical_baseline_cover_the_ten_table_contract():
    dump_tables = set(re.findall(r"CREATE TABLE public\.(\w+)\s*\(", DUMP)) - {"alembic_version"}
    assert dump_tables == set(REQUIRED)
    statements = baseline._DDL
    baseline_sql = "\n".join(statements)
    for table, canonical_columns in REQUIRED.items():
        dump_set = table_columns(DUMP, table)
        # The two provenance fields are required by the current source separation work.
        approved_additions = {"observation_source"} if table in {"competitor_products", "competitor_price_history"} else set()
        assert canonical_columns - approved_additions == dump_set
        assert table_columns(baseline_sql, table) == canonical_columns

        old_specs, new_specs = column_specs(DUMP, table), column_specs(baseline_sql, table)
        for column in dump_set:
            old_type, old_not_null, old_default = old_specs[column]
            new_type, new_not_null, new_default = new_specs[column]
            assert normalize_type(old_type) == normalize_type(new_type), (table, column, old_type, new_type)
            if (table, column) == ("price_analysis", "competitor_count"):
                # Phase 2.5 explicitly approves backfill-to-zero followed by NOT NULL.
                assert not old_not_null and new_not_null
            else:
                assert old_not_null == new_not_null, (table, column)
            if (table, column) in {
                ("price_analysis", "competitor_count"),
                ("retailer_products", "stock_quantity"),
            }:
                assert old_default is None and normalize_default(new_default) == "0"
            else:
                assert normalize_default(old_default) == normalize_default(new_default), (table, column, old_default, new_default)

    dump_sequences = set(re.findall(r"CREATE SEQUENCE public\.(\w+)", DUMP))
    baseline_sequences = set(re.findall(r"CREATE SEQUENCE (\w+)", baseline_sql))
    assert dump_sequences == EXPECTED_SEQUENCES == baseline_sequences

    dump_indexes = set(re.findall(r"CREATE (?:UNIQUE )?INDEX (\w+)", DUMP))
    baseline_indexes = set(re.findall(r"CREATE (?:UNIQUE )?INDEX (\w+)", baseline_sql))
    redundant_pk_indexes = {f"ix_{table}_id" for table in dump_tables
                            if f"ix_{table}_id" in dump_indexes}
    assert dump_indexes - redundant_pk_indexes == baseline_indexes
    assert len(EXPECTED_INDEXES) == 10

    dump_fks = foreign_keys(DUMP)
    baseline_fks = foreign_keys(baseline_sql, "")
    expected_fks = {
        (table, column, target, target_column, action)
        for table, items in EXPECTED_FKS.items()
        for column, target, target_column, action in items
    }
    assert dump_fks == expected_fks == baseline_fks

def test_approved_contract_deltas_are_explicit_in_the_audit():
    audit = (ROOT / "backend" / "docs" / "canonical_schema_audit.md").read_text(encoding="utf-8")
    for phrase in ("competitor_count", "observation_source", "scraped_at", "stock_quantity"):
        assert phrase in audit

def test_legacy_timestamp_helpers_fail_closed_instead_of_copying_created_at():
    migrations = ROOT / "backend" / "migrations"
    for filename, table in (
        ("add_scraped_at.sql", "competitor_products"),
        ("add_scraped_at_history.sql", "competitor_price_history"),
    ):
        sql = (migrations / filename).read_text(encoding="utf-8").lower()
        assert f"if exists (select 1 from {table})" in sql
        assert "raise exception" in sql
        assert "set scraped_at = created_at" not in sql

    legacy_revision = (ROOT / "backend" / "alembic" / "versions" /
                       "e77f5f74115f_standardize_models.py").read_text(encoding="utf-8")
    guard = legacy_revision.index("Refusing legacy migration")
    first_mutation = legacy_revision.index("tables_with_created")
    assert guard < first_mutation
