import importlib.util
from pathlib import Path

import sqlalchemy as sa
import pytest

MigrationContext = pytest.importorskip("alembic.migration").MigrationContext
Operations = pytest.importorskip("alembic.operations").Operations


def _load_migration(filename, module_name):
    migration_path = Path(__file__).parents[1] / "alembic" / "versions" / filename
    spec = importlib.util.spec_from_file_location(module_name, migration_path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_snapshot_repair_adds_only_missing_columns_without_data_loss():
    engine = sa.create_engine("sqlite:///:memory:")
    with engine.begin() as connection:
        connection.execute(sa.text("CREATE TABLE retailer_products (id INTEGER PRIMARY KEY, name_override VARCHAR)"))
        connection.execute(sa.text("INSERT INTO retailer_products (id, name_override) VALUES (1, 'Existing name')"))
        migration_context = MigrationContext.configure(connection)
        with Operations.context(migration_context):
            _load_migration("e5e7a52c1120_repair_retailer_detail_snapshot_columns.py", "snapshot_repair").upgrade()
            _load_migration("e6d128c9af41_add_pending_catalog_identity_flag.py", "identity_pending").upgrade()
            _load_migration("e7f3b2a41c89_add_catalog_identity_staging_flag.py", "identity_staging").upgrade()
        columns = {column["name"] for column in sa.inspect(connection).get_columns("retailer_products")}
        assert {"name_override", "category_override", "brand_override", "product_details_override", "catalog_identity_pending", "catalog_identity_staging"} <= columns
        assert connection.execute(sa.text("SELECT name_override FROM retailer_products WHERE id=1")).scalar_one() == "Existing name"
        assert connection.execute(sa.text("SELECT catalog_identity_pending FROM retailer_products WHERE id=1")).scalar_one() in (False, 0)
        assert connection.execute(sa.text("SELECT catalog_identity_staging FROM retailer_products WHERE id=1")).scalar_one() in (False, 0)
    engine.dispose()
