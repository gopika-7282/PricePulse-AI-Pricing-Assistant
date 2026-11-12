# Canonical schema audit

## Source and scope

The approved timestamp and adoption decisions are recorded in
[`phase_2_5_schema_contract.md`](phase_2_5_schema_contract.md). The independent
historical schema reference available here is the PostgreSQL dump
[`pricepulse_schema.sql`](../../pricepulse_schema.sql). The ten application
tables in that dump were compared with the frozen Alembic baseline
`alembic_canonical/versions/20261111_0001_canonical_baseline.py`; the dump does
not override the approved contract.

## Table and column comparison

| Table | Dump column set | Canonical baseline column set | Sequences and defaults |
|---|---|---|---|
| `users` | `id, retailer_name, email, password_hash, business_name, created_at, updated_at, reset_token, reset_token_expiry` | Same | `users_id_seq`; timestamps `now()`; unique email and reset-token indexes |
| `product_catalog` | `id, name, category, brand, product_details, created_at, updated_at, last_scraped_at, scraping_status, marketplace_statuses` | Same | `product_catalog_id_seq`; timestamps `now()`; status `'NEVER_SCRAPED'`; JSON `{}` |
| `retailer_products` | `id, user_id, catalog_product_id, cost_price, stock_quantity, minimum_profit_margin, created_at, updated_at, name_override, category_override, brand_override, product_details_override, catalog_identity_pending, catalog_identity_staging, quantity_value, quantity_unit` | Same | `retailer_products_id_seq`; timestamps `now()`; identity flags false; baseline additionally defaults `stock_quantity` to zero |
| `competitor_products` | `id, catalog_product_id, platform_name, product_name, price, rating, product_url, availability, product_details, created_at, updated_at, scraped_at, quantity_value, quantity_unit, pack_count, total_quantity, total_quantity_unit` | Same plus `observation_source` | `competitor_products_id_seq`; timestamps `now()`; baseline source defaults to `LEGACY_UNKNOWN` |
| `competitor_price_history` | `id, competitor_product_id, price, created_at, updated_at, scraped_at` | Same plus `observation_source` | `competitor_price_history_id_seq`; timestamps `now()`; baseline source defaults to `LEGACY_UNKNOWN` |
| `price_analysis` | `id, retailer_product_id, average_market_price, minimum_market_price, maximum_market_price, competitor_count, price_difference_percentage, analysis_summary, created_at, updated_at` | Same | `price_analysis_id_seq`; timestamps `now()`; baseline defaults count to zero and requires it |
| `recommendations` | `id, retailer_product_id, recommended_price, expected_profit, profit_percentage, reasoning, confidence_score, accepted_by_user, created_at, updated_at, evidence_type, recommended_price_min, recommended_price_max` | Same | `recommendations_id_seq`; timestamps `now()`; evidence type `NO_EVIDENCE` |
| `agent_logs` | `id, retailer_product_id, agent_name, task_description, input_data, output_data, execution_time, status, created_at, updated_at` | Same | `agent_logs_id_seq`; timestamps `now()` |
| `chat_conversations` | `id, retailer_id, product_id, created_at, updated_at` | Same | `chat_conversations_id_seq`; timestamps `now()` |
| `chat_messages` | `id, conversation_id, role, content, created_at` | Same | `chat_messages_id_seq`; timestamp `now()` |

All ten ID columns use their named PostgreSQL sequences with `nextval` defaults.
All timestamp columns that are non-null in the dump retain `DEFAULT now()` in
the baseline. The nullable `last_scraped_at` catalog field stays nullable.

## Constraints, indexes, and known differences

- The baseline preserves all ten foreign-key relationships and their dump
  delete actions: cascading deletes for owned/dependent rows and `SET NULL` for
  `chat_conversations.product_id`.
- Business indexes from the dump are retained, including the two user indexes
  and the catalog, platform, retailer, chat, and history foreign-key indexes.
  Standalone `ix_*_id` indexes are omitted from the new baseline because each
  duplicates its primary-key index. No migration drops those indexes from an
  existing database.
- The dump has `competitor_count` nullable with no default. The approved task
  instruction requires NULL backfill to zero and NOT NULL; the canonical
  baseline has default zero and NOT NULL, and the follow-up revision performs
  the guarded backfill before enforcing NOT NULL on an adoptable schema.
- The dump has no `observation_source` columns. These are added so manual
  entries and legacy unknowns cannot masquerade as scraped market evidence.
- The dump and approved contract both require competitor `scraped_at` to be
  `TIMESTAMPTZ NOT NULL DEFAULT now()`. Manual rows may receive that storage
  default but remain `MANUAL`; the default is never treated as evidence.
- The dump has no `stock_quantity` default. The baseline uses server default
  zero while API creation still requires a non-negative stock quantity.
- `alembic_version` from the legacy migration graph is preserved. The new graph
  uses its own `canonical_alembic_version` table, which is not part of the
  ten-table application schema.

## Adoption safety

Preflight is read-only. Adoption requires `--confirm-adoption`, the expected
table/column/default/index/foreign-key shape, and every existing competitor
row to have a non-null timestamp explicitly marked `SCRAPE`. A missing source
column or any `LEGACY_UNKNOWN`/`MANUAL` row blocks adoption before DDL or data
modification. After this gate, adoption enforces both timestamp NOT NULL
constraints, backfills NULL `competitor_count` rows to zero, sets its default,
and enforces NOT NULL before stamping the baseline revision. No production
database was connected during this work.
