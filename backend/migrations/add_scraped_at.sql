-- Add scraped_at column to competitor_products
ALTER TABLE competitor_products ADD COLUMN scraped_at TIMESTAMP WITH TIME ZONE;

-- Populate existing records using created_at for scraped_at
UPDATE competitor_products SET scraped_at = created_at WHERE scraped_at IS NULL;

-- Make scraped_at NOT NULL and add default
ALTER TABLE competitor_products ALTER COLUMN scraped_at SET NOT NULL;
ALTER TABLE competitor_products ALTER COLUMN scraped_at SET DEFAULT CURRENT_TIMESTAMP;
