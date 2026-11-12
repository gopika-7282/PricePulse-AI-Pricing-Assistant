# Phase 2.5 database contract

Approved decisions for competitor observation timestamps and legacy adoption.
This contract takes precedence over incomplete database dumps.

## Competitor observations

- `competitor_products.scraped_at` and
  `competitor_price_history.scraped_at` are `TIMESTAMPTZ NOT NULL DEFAULT now()`.
- Scrapers must persist the actual observation time supplied by the scraper.
  Never replace a missing historical observation time with application time.
- The database default satisfies the non-null storage contract; it does not
  prove a marketplace observation occurred. Market evidence requires explicit
  `observation_source = 'SCRAPE'` and a genuine scraper-supplied timestamp.
- Manual rows may receive the database timestamp default, but retain
  `observation_source = 'MANUAL'` and are excluded from market evidence.
- Adoption stops before any DDL or data change if trustworthy observation
  provenance is unavailable, the source is not `SCRAPE`, or a timestamp is
  missing. A non-null timestamp alone is insufficient.

## Price analysis and adoption

- `price_analysis.competitor_count` defaults to `0` and is `NOT NULL` in the
  canonical schema.
- For an otherwise adoptable legacy schema, count and backfill NULL values to
  zero, set the default, then enforce `NOT NULL` before recording the canonical
  baseline revision.
- The provenance gate must pass before any adoption DDL or data modification.

## Reference

The canonical Alembic baseline is the executable fresh-install schema. The
preflight/adoption path applies the approved constraints to eligible legacy
databases. `pricepulse_schema.sql` is an incomplete historical dump and does
not override these decisions.
