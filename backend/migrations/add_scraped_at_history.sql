-- Existing history rows cannot be assigned a trustworthy observation time here.
-- Fail before DDL; use the canonical preflight/adoption process for populated data.
DO $$
BEGIN
    IF EXISTS (SELECT 1 FROM competitor_price_history) THEN
        RAISE EXCEPTION 'Cannot add scraped_at: history rows require verified observation provenance';
    END IF;
END $$;

ALTER TABLE competitor_price_history ADD COLUMN scraped_at TIMESTAMP WITH TIME ZONE;

ALTER TABLE competitor_price_history ALTER COLUMN scraped_at SET NOT NULL;
ALTER TABLE competitor_price_history ALTER COLUMN scraped_at SET DEFAULT now();
