from scripts.db_preflight import inspect_database

class FakeResult:
    def __init__(self, value): self.value = value
    def scalar_one(self): return self.value

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
    def get_pk_constraint(self, table): return {"constrained_columns": ["id"]}
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
