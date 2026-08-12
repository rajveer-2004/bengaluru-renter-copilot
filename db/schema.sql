-- Bengaluru Renter's Copilot — SQLite schema
-- Design principle: separate RAW scrape data from CLEANED structured data from
-- EXTRACTED fields (DistilBERT output) from MODEL PREDICTIONS. This lets us
-- re-run any downstream stage without re-scraping.

PRAGMA foreign_keys = ON;

-- ---------------------------------------------------------------------------
-- 1. scrape_runs: one row per weekly pipeline invocation. Everything
--    downstream references this so we can trace lineage and reproduce.
-- ---------------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS scrape_runs (
    run_id           INTEGER PRIMARY KEY AUTOINCREMENT,
    started_at       TEXT    NOT NULL,   -- ISO8601 UTC
    finished_at      TEXT,
    source           TEXT    NOT NULL,   -- 'nobroker' | 'telegram:<group>'
    n_raw            INTEGER,            -- rows scraped
    n_new            INTEGER,            -- rows that were new after dedup
    status           TEXT    NOT NULL,   -- 'running' | 'ok' | 'failed'
    error            TEXT,
    git_sha          TEXT                -- commit hash of the pipeline
);

-- ---------------------------------------------------------------------------
-- 2. raw_listings: exactly what came off the wire. No parsing beyond splitting
--    obvious fields. Keep the full blob so we can re-extract later.
-- ---------------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS raw_listings (
    raw_id           INTEGER PRIMARY KEY AUTOINCREMENT,
    run_id           INTEGER NOT NULL REFERENCES scrape_runs(run_id),
    source           TEXT    NOT NULL,   -- 'nobroker' | 'telegram:<group>'
    source_url       TEXT,               -- for nobroker; NULL for telegram
    source_msg_id    TEXT,               -- telegram message id; NULL for web
    scraped_at       TEXT    NOT NULL,
    raw_text         TEXT    NOT NULL,   -- full description / message body
    raw_json         TEXT,               -- any structured payload the source gave us
    content_hash     TEXT    NOT NULL,   -- sha256 of normalized raw_text for dedup
    UNIQUE (source, content_hash)
);

CREATE INDEX IF NOT EXISTS idx_raw_run     ON raw_listings(run_id);
CREATE INDEX IF NOT EXISTS idx_raw_scraped ON raw_listings(scraped_at);

-- ---------------------------------------------------------------------------
-- 3. listings: canonical, deduped, structured. One row per real-world listing.
--    A listing may be observed across multiple scrape runs — we upsert here
--    and track price changes / staleness via listing_observations.
-- ---------------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS listings (
    listing_id       INTEGER PRIMARY KEY AUTOINCREMENT,
    first_seen_at    TEXT    NOT NULL,
    last_seen_at     TEXT    NOT NULL,
    source           TEXT    NOT NULL,
    dedup_key        TEXT    NOT NULL UNIQUE,  -- fuzzy key: locality|bhk|rent_bucket|area_bucket

    -- Structured fields (Tier 1 + Tier 2 from the feature discussion)
    locality         TEXT,               -- 'HSR Layout Sector 2'
    locality_norm    TEXT,               -- normalized -> joins localities.locality_norm
    bhk              REAL,               -- 1, 1.5, 2, 2.5, 3, ...
    area_sqft        REAL,
    property_type    TEXT,               -- 'apartment' | 'independent_house' | 'villa' | 'pg'
    furnishing       TEXT,               -- 'unfurnished' | 'semi' | 'full'
    floor_num        INTEGER,
    total_floors     INTEGER,
    age_years        REAL,
    facing           TEXT,               -- 'east' | 'west' | 'north' | 'south' | 'ne' | ...
    bathrooms        INTEGER,
    balconies        INTEGER,
    parking_car      INTEGER,            -- 0/1/2
    parking_bike     INTEGER,

    -- Money
    rent_monthly     REAL,               -- INR/month  <- TARGET for pricing model
    deposit          REAL,               -- INR
    maintenance      REAL,               -- INR/month; NULL if not disclosed
    maintenance_included INTEGER,        -- 1 if included in rent, 0 if extra, NULL unknown

    -- Availability
    available_from   TEXT,               -- ISO date

    -- Provenance
    latest_raw_id    INTEGER REFERENCES raw_listings(raw_id)
);

CREATE INDEX IF NOT EXISTS idx_listings_locality ON listings(locality_norm);
CREATE INDEX IF NOT EXISTS idx_listings_bhk      ON listings(bhk);
CREATE INDEX IF NOT EXISTS idx_listings_lastseen ON listings(last_seen_at);

-- ---------------------------------------------------------------------------
-- 4. listing_observations: append-only log of each time we saw a listing.
--    Enables 'days on market', price-change detection, and time-based CV.
-- ---------------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS listing_observations (
    obs_id           INTEGER PRIMARY KEY AUTOINCREMENT,
    listing_id       INTEGER NOT NULL REFERENCES listings(listing_id),
    run_id           INTEGER NOT NULL REFERENCES scrape_runs(run_id),
    observed_at      TEXT    NOT NULL,
    rent_monthly     REAL,               -- snapshot; may change week to week
    deposit          REAL,
    raw_id           INTEGER REFERENCES raw_listings(raw_id)
);

CREATE INDEX IF NOT EXISTS idx_obs_listing ON listing_observations(listing_id);
CREATE INDEX IF NOT EXISTS idx_obs_run     ON listing_observations(run_id);

-- ---------------------------------------------------------------------------
-- 5. extractions: DistilBERT (and baselines) output for fields that live in
--    the free-text description. Kept separate from `listings` because we may
--    re-run extraction with a newer model version.
-- ---------------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS extractions (
    extraction_id    INTEGER PRIMARY KEY AUTOINCREMENT,
    listing_id       INTEGER NOT NULL REFERENCES listings(listing_id),
    raw_id           INTEGER NOT NULL REFERENCES raw_listings(raw_id),
    extractor        TEXT    NOT NULL,   -- 'distilbert-v1' | 'gemini-flash' | 'regex-v1'
    extractor_version TEXT   NOT NULL,
    extracted_at     TEXT    NOT NULL,

    -- The fields we extract from unstructured text
    tenant_pref      TEXT,               -- 'family' | 'bachelors' | 'bachelors_male' | 'bachelors_female' | 'any'
    veg_only         INTEGER,            -- 1/0/NULL
    is_owner         INTEGER,            -- 1 = owner, 0 = broker, NULL unknown
    negotiable       INTEGER,
    lock_in_months   INTEGER,
    amenities_json   TEXT,               -- JSON array of normalized amenity tags

    -- Cost/latency for the benchmark
    latency_ms       REAL,
    cost_usd         REAL,               -- 0 for local models

    UNIQUE (listing_id, extractor, extractor_version)
);

CREATE INDEX IF NOT EXISTS idx_extract_listing ON extractions(listing_id);
CREATE INDEX IF NOT EXISTS idx_extract_model   ON extractions(extractor, extractor_version);

-- ---------------------------------------------------------------------------
-- 6. localities: reference table with geospatial / market context features.
--    Populated once, refreshed rarely.
-- ---------------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS localities (
    locality_norm    TEXT PRIMARY KEY,   -- 'hsr_layout' etc.
    display_name     TEXT NOT NULL,
    lat              REAL,
    lon              REAL,
    dist_nearest_metro_km   REAL,
    nearest_metro_station   TEXT,
    dist_orr_km             REAL,
    dist_manyata_km         REAL,
    dist_ecity_km           REAL,
    dist_whitefield_km      REAL,
    dist_orr_bellandur_km   REAL
);

-- ---------------------------------------------------------------------------
-- 7. predictions: XGBoost output. One row per (listing, model_version).
-- ---------------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS predictions (
    prediction_id    INTEGER PRIMARY KEY AUTOINCREMENT,
    listing_id       INTEGER NOT NULL REFERENCES listings(listing_id),
    model_version    TEXT    NOT NULL,   -- 'xgb-v1', 'xgb-v2-quantile', ...
    predicted_rent   REAL    NOT NULL,
    predicted_p10    REAL,               -- optional lower bound (quantile model)
    predicted_p90    REAL,
    value_score      REAL    NOT NULL,   -- (predicted - actual) / predicted
    is_deal          INTEGER NOT NULL,   -- 1 if value_score >= 0.15
    predicted_at     TEXT    NOT NULL,
    UNIQUE (listing_id, model_version)
);

CREATE INDEX IF NOT EXISTS idx_pred_deal  ON predictions(is_deal);
CREATE INDEX IF NOT EXISTS idx_pred_model ON predictions(model_version);

-- ---------------------------------------------------------------------------
-- 8. benchmark_runs: DistilBERT vs Gemini Flash vs regex, aggregate metrics.
-- ---------------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS benchmark_runs (
    benchmark_id     INTEGER PRIMARY KEY AUTOINCREMENT,
    ran_at           TEXT    NOT NULL,
    extractor        TEXT    NOT NULL,
    extractor_version TEXT   NOT NULL,
    eval_set         TEXT    NOT NULL,   -- 'holdout-v1' etc.
    n_examples       INTEGER NOT NULL,
    field_accuracies_json TEXT NOT NULL, -- per-field accuracy as JSON
    macro_accuracy   REAL    NOT NULL,
    cost_per_1k_usd  REAL    NOT NULL,
    p50_latency_ms   REAL    NOT NULL,
    p95_latency_ms   REAL    NOT NULL,
    notes            TEXT
);
