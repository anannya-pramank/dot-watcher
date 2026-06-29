-- dot_schema.sql
-- Run this once in your Supabase SQL editor (or via psql) before
-- running dot_watcher.py or dot_backfill.py.
-- ---------------------------------------------------------------

CREATE TABLE IF NOT EXISTS dot_documents (
    -- Primary key: SHA-1 of pdf_url (first 16 hex chars)
    id            TEXT        PRIMARY KEY,

    title         TEXT,
    publish_date  TEXT,           -- stored as MM/DD/YYYY (matches existing CSV format)
    pdf_url       TEXT        NOT NULL UNIQUE,
    category      TEXT        NOT NULL,
    scraped_at    TEXT,           -- MM/DD/YYYY string from scraper

    -- Supabase-side audit timestamps
    created_at    TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    updated_at    TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

-- Auto-update updated_at on any row change
CREATE OR REPLACE FUNCTION set_updated_at()
RETURNS TRIGGER LANGUAGE plpgsql AS $$
BEGIN
    NEW.updated_at = NOW();
    RETURN NEW;
END;
$$;

DROP TRIGGER IF EXISTS dot_documents_set_updated_at ON dot_documents;
CREATE TRIGGER dot_documents_set_updated_at
    BEFORE UPDATE ON dot_documents
    FOR EACH ROW EXECUTE FUNCTION set_updated_at();

-- Useful query indexes
CREATE INDEX IF NOT EXISTS idx_dot_documents_category
    ON dot_documents (category);

CREATE INDEX IF NOT EXISTS idx_dot_documents_scraped_at
    ON dot_documents (scraped_at);

-- Optional: full-text search on title
CREATE INDEX IF NOT EXISTS idx_dot_documents_title_fts
    ON dot_documents USING gin (to_tsvector('english', coalesce(title, '')));


-- ── Row-Level Security (enable if using anon/service-role split) ─────────────
-- ALTER TABLE dot_documents ENABLE ROW LEVEL SECURITY;
--
-- Read-only anon access:
-- CREATE POLICY "anon_read" ON dot_documents
--     FOR SELECT USING (true);
--
-- Write restricted to service role (default behaviour when RLS is enabled):
-- Only the service_role key can INSERT / UPDATE / DELETE.


-- ── Helpful views ────────────────────────────────────────────────────────────

-- Latest 50 items across all categories
CREATE OR REPLACE VIEW dot_latest AS
SELECT *
FROM   dot_documents
ORDER  BY scraped_at DESC, created_at DESC
LIMIT  50;

-- Count per category
CREATE OR REPLACE VIEW dot_category_counts AS
SELECT   category,
         COUNT(*) AS total
FROM     dot_documents
GROUP BY category
ORDER BY total DESC;
