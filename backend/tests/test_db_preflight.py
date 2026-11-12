from scripts.db_preflight import inspect_database

class FakeResult:
    def __init__(self, value): self.value = value
    def scalar_one(self): return self.value
    def scalar_one_or_none(self): return self.value

class FakeConnection:
    def __init__(self, tables, columns, counts=None):
        self.tables, self.columns, self.counts = tables, columns, counts or {}
        self.commands = []
    def execute(self, statement, params=None):
        query = str(statement)
        self.commands.append(query)
        if "price_analysis" in query: return FakeResult(self.counts.get("nulls", 0))
        if "competitor_products" in query: return FakeResult(self.counts.get("competitors", 0))
        return FakeResult(self.counts.get("history", 0))

class FakeInspector:
    def __init__(self, tables, columns):
        self.tables, self.columns = tables, columns
        self.nullable_overrides = {}
        self.default_overrides = {}
        self.pk_overrides = {}
    def get_table_names(self): return list(self.tables)
    def get_sequence_names(self):
        from scripts.db_preflight import EXPECTED_SEQUENCES
        return list(EXPECTED_SEQUENCES)
    def get_columns(self, table):
        from scripts.db_preflight import NULLABLE_COLUMNS, TIMESTAMP_DEFAULTS, VALUE_DEFAULTS
        def default(name):
            if name == "id": return f"nextval('{table}_id_seq'::regclass)"
            if name in TIMESTAMP_DEFAULTS[table]: return "now()"
            return VALUE_DEFAULTS.get((table, name))
        return [{"name": name,
                 "nullable": self.nullable_overrides.get(table, {}).get(name, name in NULLABLE_COLUMNS[table]),
                 "default": self.default_overrides.get(table, {}).get(name, default(name))}
                for name in self.columns.get(table, set())]
    def get_indexes(self, table):
        from scripts.db_preflight import EXPECTED_INDEXES
        return [{"name": name} for name in EXPECTED_INDEXES[table]]
    def get_pk_constraint(self, table):
        return {"constrained_columns": self.pk_overrides.get(table, ["id"])}
    def get_foreign_keys(self, table):
        from scripts.db_preflight import EXPECTED_FKS
        return [{"constrained_columns": [col], "referred_table": target,
                 "referred_columns": [target_col], "options": {"ondelete": action}}
                for col, target, target_col, action in EXPECTED_FKS[table]]

def test_empty_database_preflight_never_adopts():
    from unittest.mock import patch
    with patch("scripts.db_preflight.inspect") as inspector:
        inspector.return_value.get_table_names.return_value = ["alembic_version"]
        assert inspect_database(FakeConnection(set(), {}))["state"] == "empty"

def test_postgresql_varchar_display_casts_match_approved_defaults():
    from scripts.db_preflight import normalize_server_default
    assert normalize_server_default("'NEVER_SCRAPED'::character varying") == "'NEVER_SCRAPED'"
    assert normalize_server_default("'LEGACY_UNKNOWN'::character varying(16)") == "'LEGACY_UNKNOWN'"
    assert normalize_server_default("'NO_EVIDENCE'::character varying") == "'NO_EVIDENCE'"

def test_canonical_verification_requires_expected_revision_and_ignores_adoption_marker():
    from unittest.mock import patch
    from scripts.db_preflight import CANONICAL_HEAD, REQUIRED, verify_canonical_database
    tables = set(REQUIRED) | {"canonical_alembic_version"}
    columns = {table: set(required) for table, required in REQUIRED.items()}
    connection = FakeConnection(tables, columns)
    connection.canonical_revision = CANONICAL_HEAD
    original_execute = connection.execute
    def execute(statement, params=None):
        if "SELECT version_num FROM canonical_alembic_version" in str(statement):
            return FakeResult(connection.canonical_revision)
        return original_execute(statement, params)
    connection.execute = execute
    with patch("scripts.db_preflight.inspect", return_value=FakeInspector(tables, columns)):
        result = verify_canonical_database(connection)
        connection.canonical_revision = "unknown_revision"
        wrong_revision = verify_canonical_database(connection)
    assert result["safe_to_verify"]
    assert result["revision"] == CANONICAL_HEAD
    assert not wrong_revision["safe_to_verify"]
    assert any("expected" in reason for reason in wrong_revision["reasons"])

def test_missing_trusted_timestamp_provenance_blocks_adoption():
    from unittest.mock import patch
    tables = {"users", "product_catalog", "retailer_products", "competitor_products", "competitor_price_history", "price_analysis", "recommendations", "agent_logs", "chat_conversations", "chat_messages"}
    cols = {table: set(required) for table, required in __import__("scripts.db_preflight", fromlist=["REQUIRED"]).REQUIRED.items()}
    with patch("scripts.db_preflight.inspect", return_value=FakeInspector(tables, cols)):
        result = inspect_database(FakeConnection(tables, cols, {"competitors": 3}))
    assert not result["safe_to_adopt"]
    assert any("trustworthy" in reason for reason in result["reasons"])

def test_null_competitor_counts_are_reported_but_do_not_block_guarded_backfill():
    from unittest.mock import patch
    from scripts.db_preflight import REQUIRED
    tables = set(REQUIRED)
    columns = {table: set(required) for table, required in REQUIRED.items()}
    with patch("scripts.db_preflight.inspect", return_value=FakeInspector(tables, columns)):
        result = inspect_database(FakeConnection(tables, columns, {"nulls": 4}))
    assert result["safe_to_adopt"]
    assert result["null_competitor_count_rows"] == 4

def test_nullable_legacy_competitor_count_is_adoptable_for_backfill():
    from unittest.mock import patch
    from scripts.db_preflight import REQUIRED
    tables = set(REQUIRED)
    columns = {table: set(required) for table, required in REQUIRED.items()}
    inspector = FakeInspector(tables, columns)
    inspector.nullable_overrides = {"price_analysis": {"competitor_count": True}}
    inspector.default_overrides = {"price_analysis": {"competitor_count": None}}
    with patch("scripts.db_preflight.inspect", return_value=inspector):
        result = inspect_database(FakeConnection(tables, columns, {"nulls": 2}))
    assert result["safe_to_adopt"]
    assert result["null_competitor_count_rows"] == 2

def test_null_competitor_count_is_counted_even_when_unknown_provenance_blocks_adoption():
    from unittest.mock import patch
    from scripts.db_preflight import REQUIRED
    tables = set(REQUIRED)
    columns = {table: set(required) for table, required in REQUIRED.items()}
    columns["competitor_products"].remove("observation_source")
    connection = FakeConnection(tables, columns, {"nulls": 3})
    with patch("scripts.db_preflight.inspect", return_value=FakeInspector(tables, columns)):
        result = inspect_database(connection)
    assert not result["safe_to_adopt"]
    assert result["null_competitor_count_rows"] == 3

def test_unknown_timestamp_provenance_fails_before_adoption_writes():
    from unittest.mock import patch
    from scripts.db_preflight import REQUIRED, adopt_database
    tables = set(REQUIRED)
    columns = {table: set(required) for table, required in REQUIRED.items()}
    connection = FakeConnection(tables, columns, {"competitors": 1})
    with patch("scripts.db_preflight.inspect", return_value=FakeInspector(tables, columns)):
        try:
            adopt_database(connection, confirmed=True)
        except RuntimeError as error:
            assert "trustworthy" in str(error)
        else:
            raise AssertionError("Adoption should reject untrusted historical timestamps")
    assert not any(statement.lstrip().startswith(("UPDATE", "ALTER", "CREATE", "INSERT"))
                   for statement in connection.commands)

def test_adoption_backfills_null_counts_before_setting_not_null():
    from unittest.mock import patch
    from scripts.db_preflight import REQUIRED, adopt_database
    tables = set(REQUIRED)
    columns = {table: set(required) for table, required in REQUIRED.items()}
    connection = FakeConnection(tables, columns, {"nulls": 4})
    inspector = FakeInspector(tables, columns)
    inspector.nullable_overrides = {"price_analysis": {"competitor_count": True}}
    inspector.default_overrides = {"price_analysis": {"competitor_count": None}}
    with patch("scripts.db_preflight.inspect", return_value=inspector):
        adopt_database(connection, confirmed=True)
    writes = [statement.strip().split(" ", 1)[0] for statement in connection.commands
              if statement.lstrip().startswith(("UPDATE", "ALTER", "CREATE", "INSERT"))]
    assert writes == ["ALTER", "ALTER", "UPDATE", "ALTER", "ALTER", "CREATE", "INSERT"]

def test_trusted_nonnull_scrape_rows_get_timestamp_constraints_before_adoption_stamp():
    from unittest.mock import patch
    from scripts.db_preflight import REQUIRED, adopt_database
    tables = set(REQUIRED)
    columns = {table: set(required) for table, required in REQUIRED.items()}
    connection = FakeConnection(tables, columns)
    with patch("scripts.db_preflight.inspect", return_value=FakeInspector(tables, columns)):
        adopt_database(connection, confirmed=True)
    writes = [statement.strip() for statement in connection.commands
              if statement.lstrip().startswith(("ALTER", "UPDATE", "CREATE", "INSERT"))]
    assert writes[:2] == [
        "ALTER TABLE competitor_products ALTER COLUMN scraped_at SET NOT NULL",
        "ALTER TABLE competitor_price_history ALTER COLUMN scraped_at SET NOT NULL",
    ]
    assert writes[-1].startswith("INSERT INTO canonical_alembic_version")


def test_normalization_preserves_case_and_escaped_quoted_tokens():
    from scripts.db_preflight import normalize_server_default

    assert normalize_server_default("'MANUAL'::character varying") != normalize_server_default("'manual'::character varying")
    assert normalize_server_default("'it''s MANUAL'::json") == "'it''s MANUAL'"
    assert normalize_server_default('"MixedCaseIdentifier"::regclass') == '"MixedCaseIdentifier"'
    assert normalize_server_default('"a""B"::json') == '"a""B"'
    assert normalize_server_default("'text ::json and ::character varying(16)'") == "'text ::json and ::character varying(16)'"
    assert normalize_server_default('"field::json"') == '"field::json"'
    assert normalize_server_default(None) == ""


def test_all_approved_canonical_string_defaults_match_postgresql_display_casts():
    from scripts.db_preflight import VALUE_DEFAULTS, normalize_server_default

    string_defaults = [
        default for default in VALUE_DEFAULTS.values()
        if default.startswith("'") and default.endswith("'")
    ]
    assert string_defaults
    for default in string_defaults:
        assert normalize_server_default(default + "::character varying") == normalize_server_default(default)


def _run_canonical_verification(
    tables=None, columns=None, *, revision=None, configure=None, configure_connection=None
):
    from unittest.mock import patch
    from scripts.db_preflight import CANONICAL_HEAD, REQUIRED, verify_canonical_database

    tables = set(REQUIRED) | {"canonical_alembic_version"} if tables is None else set(tables)
    columns = (
        {table: set(required) for table, required in REQUIRED.items()}
        if columns is None else {table: set(names) for table, names in columns.items()}
    )
    inspector = FakeInspector(tables, columns)
    if configure:
        configure(inspector)
    connection = FakeConnection(tables, columns)
    connection.rows = {"competitor_products": []}
    connection.canonical_revision = CANONICAL_HEAD if revision is None else revision
    if configure_connection:
        configure_connection(connection)
    original_execute = connection.execute

    def execute(statement, params=None):
        if "SELECT version_num FROM canonical_alembic_version" in str(statement):
            connection.commands.append(str(statement))
            return FakeResult(connection.canonical_revision)
        return original_execute(statement, params)

    connection.execute = execute
    with patch("scripts.db_preflight.inspect", return_value=inspector):
        result = verify_canonical_database(connection)
    return result, connection


def test_canonical_verification_accepts_expected_revision_and_valid_schema():
    from scripts.db_preflight import CANONICAL_HEAD

    result, _ = _run_canonical_verification()
    assert result["safe_to_verify"] is True
    assert result["revision"] == CANONICAL_HEAD


def test_canonical_verification_rejects_incorrect_revision():
    result, _ = _run_canonical_verification(revision="unknown_revision")
    assert result["safe_to_verify"] is False
    assert any("expected" in reason for reason in result["reasons"])


def test_canonical_verification_rejects_missing_version_table():
    from scripts.db_preflight import REQUIRED

    result, _ = _run_canonical_verification(tables=set(REQUIRED))
    assert result["safe_to_verify"] is False
    assert any("canonical_alembic_version is missing" in reason for reason in result["reasons"])


def test_canonical_verification_rejects_missing_application_table():
    from scripts.db_preflight import REQUIRED

    tables = set(REQUIRED) | {"canonical_alembic_version"}
    tables.remove("competitor_products")
    result, _ = _run_canonical_verification(tables=tables)
    assert result["safe_to_verify"] is False
    assert any("competitor_products" in reason for reason in result["reasons"])


def test_canonical_verification_rejects_missing_required_column():
    from scripts.db_preflight import REQUIRED

    columns = {table: set(required) for table, required in REQUIRED.items()}
    columns["competitor_products"].remove("price")
    result, _ = _run_canonical_verification(columns=columns)
    assert result["safe_to_verify"] is False
    assert any("competitor_products missing columns: price" in reason for reason in result["reasons"])


def test_canonical_verification_rejects_incorrect_server_default():
    def configure(inspector):
        inspector.default_overrides = {"product_catalog": {"scraping_status": "'manual'"}}

    result, _ = _run_canonical_verification(configure=configure)
    assert result["safe_to_verify"] is False
    assert any("product_catalog.scraping_status server default" in reason for reason in result["reasons"])


def test_canonical_verification_rejects_incorrect_nullability():
    def configure(inspector):
        inspector.nullable_overrides = {"product_catalog": {"name": True}}

    result, _ = _run_canonical_verification(configure=configure)
    assert result["safe_to_verify"] is False
    assert any("product_catalog.name nullability" in reason for reason in result["reasons"])


def test_canonical_verification_rejects_incorrect_foreign_key():
    def configure(inspector):
        inspector.get_foreign_keys = lambda table: (
            [] if table == "competitor_products"
            else FakeInspector.get_foreign_keys(inspector, table)
        )

    result, _ = _run_canonical_verification(configure=configure)
    assert result["safe_to_verify"] is False
    assert any("competitor_products foreign keys" in reason for reason in result["reasons"])


def test_canonical_verification_ignores_legitimate_manual_competitor_rows():
    def insert_manual_record(connection):
        connection.rows["competitor_products"].append({
            "id": 101,
            "observation_source": "MANUAL",
            "scraped_at": None,
        })

    result, connection = _run_canonical_verification(configure_connection=insert_manual_record)
    assert connection.rows["competitor_products"][0]["observation_source"] == "MANUAL"
    assert result["safe_to_verify"] is True
    assert all("competitor_products" not in query for query in connection.commands)


def test_canonical_verification_is_read_only_and_skips_adoption_checks():
    from unittest.mock import patch
    from scripts.db_preflight import CANONICAL_HEAD, REQUIRED, verify_canonical_database

    tables = set(REQUIRED) | {"canonical_alembic_version"}
    columns = {table: set(required) for table, required in REQUIRED.items()}
    inspector = FakeInspector(tables, columns)
    connection = FakeConnection(tables, columns)
    connection.canonical_revision = CANONICAL_HEAD
    original_execute = connection.execute

    def execute(statement, params=None):
        if "SELECT version_num FROM canonical_alembic_version" in str(statement):
            connection.commands.append(str(statement))
            return FakeResult(CANONICAL_HEAD)
        return original_execute(statement, params)

    connection.execute = execute
    with (
        patch("scripts.db_preflight.inspect", return_value=inspector),
        patch("scripts.db_preflight.inspect_database", side_effect=AssertionError("legacy adoption check called")),
        patch("scripts.db_preflight.adopt_database", side_effect=AssertionError("adoption called")),
    ):
        result = verify_canonical_database(connection)

    assert result["safe_to_verify"] is True
    assert connection.commands
    assert all(statement.lstrip().upper().startswith("SELECT") for statement in connection.commands)
    assert not any(
        statement.lstrip().upper().startswith(("UPDATE", "ALTER", "CREATE", "INSERT", "DELETE", "DROP"))
        for statement in connection.commands
    )


def test_canonical_verification_rejects_missing_primary_key():
    def configure(inspector):
        inspector.pk_overrides = {"product_catalog": []}

    result, _ = _run_canonical_verification(configure=configure)
    assert result["safe_to_verify"] is False
    assert any("product_catalog primary key" in reason for reason in result["reasons"])


def test_canonical_verification_rejects_incorrect_primary_key_column():
    def configure(inspector):
        inspector.pk_overrides = {"product_catalog": ["brand"]}

    result, _ = _run_canonical_verification(configure=configure)
    assert result["safe_to_verify"] is False
    assert any("product_catalog primary key" in reason for reason in result["reasons"])


def test_canonical_verification_accepts_id_primary_key():
    def configure(inspector):
        inspector.pk_overrides = {"product_catalog": ["id"]}

    result, _ = _run_canonical_verification(configure=configure)
    assert result["safe_to_verify"] is True
