-- NTN Dashboard v2 — SQLite schema
-- All Meta + Shopify data lives here. Dashboard reads ONLY from this DB,
-- never directly from APIs, so it never breaks on rate limits.
--
-- Apply with: python3 scripts/v2/db_init.py
-- DB file: state/ntn.db (gitignored — synced via state branch)

PRAGMA foreign_keys = ON;
PRAGMA journal_mode = WAL;

-- ── Meta: per-day ad-level snapshot ───────────────────────────────────────
-- One row per (ad_id, date). UPSERT on re-run (idempotent).
CREATE TABLE IF NOT EXISTS meta_ads_daily (
    date          TEXT NOT NULL,            -- YYYY-MM-DD
    ad_id         TEXT NOT NULL,
    portal        TEXT NOT NULL,            -- SM / SML / NBP
    account_id    TEXT NOT NULL,
    account_name  TEXT,
    campaign_id   TEXT,
    campaign_name TEXT,
    adset_id      TEXT,
    adset_name    TEXT,
    ad_name       TEXT,
    spend         REAL DEFAULT 0,
    impressions   INTEGER DEFAULT 0,
    reach         INTEGER DEFAULT 0,
    clicks        INTEGER DEFAULT 0,
    inline_link_clicks INTEGER DEFAULT 0,
    outbound_clicks INTEGER DEFAULT 0,
    ctr           REAL,                     -- already in % (Meta convention)
    cpm           REAL,
    cpc           REAL,
    frequency     REAL,
    purchases     INTEGER DEFAULT 0,
    revenue       REAL DEFAULT 0,
    roas          REAL,                     -- spend > 0 ? revenue/spend : null
    purchase_roas_default REAL,             -- Meta's purchase_roas (default attribution)
    purchase_roas_1d_click REAL,            -- 1-day click attribution window
    purchase_roas_7d_click REAL,            -- 7-day click attribution window
    landing_page_views INTEGER DEFAULT 0,
    add_to_cart   INTEGER DEFAULT 0,
    initiate_checkout INTEGER DEFAULT 0,
    video_p25_views INTEGER DEFAULT 0,
    video_p50_views INTEGER DEFAULT 0,
    video_p75_views INTEGER DEFAULT 0,
    video_thruplay  INTEGER DEFAULT 0,
    video_avg_time_watched_sec REAL,
    fetched_at    TEXT,                     -- ISO timestamp of this fetch
    PRIMARY KEY (date, ad_id)
);
CREATE INDEX IF NOT EXISTS idx_mad_date         ON meta_ads_daily(date);
CREATE INDEX IF NOT EXISTS idx_mad_portal_date  ON meta_ads_daily(portal, date);
CREATE INDEX IF NOT EXISTS idx_mad_campaign     ON meta_ads_daily(campaign_id, date);
CREATE INDEX IF NOT EXISTS idx_mad_adid         ON meta_ads_daily(ad_id);

-- ── Meta: per-ad metadata (one row per ad_id, lifetime) ──────────────────
-- Updated on each ingest. Holds derived classifications (category etc).
CREATE TABLE IF NOT EXISTS meta_ads_meta (
    ad_id         TEXT PRIMARY KEY,
    portal        TEXT,
    account_id    TEXT,
    campaign_id   TEXT,
    adset_id      TEXT,
    ad_name       TEXT,
    creative_id   TEXT,
    creative_object_url TEXT,                -- Shopify product URL pulled from creative
    created_time  TEXT,                      -- ISO from Meta
    first_seen    TEXT,                      -- first day with spend > 0
    last_seen     TEXT,                      -- last day with spend > 0
    days_active   INTEGER DEFAULT 0,         -- count of days with spend > 0
    total_spend   REAL DEFAULT 0,            -- lifetime spend
    total_revenue REAL DEFAULT 0,
    total_purchases INTEGER DEFAULT 0,
    -- DERIVED CLASSIFICATIONS (recomputed by classify_ads.py on rule change)
    category      TEXT,                      -- Skin/Hair/Crystal HD/Crystal Acc/Jewellery/Perfumes/AI Bot/Nutra/Other
    creative_type TEXT,                      -- Paras/Static/Motion/Partnership/AI/Other
    sentiment     TEXT,                      -- e.g. problem_solution, offer, testimonial, before_after
    product       TEXT,                      -- e.g. AM PM Pigmentation
    ntn_code      TEXT,                      -- e.g. NTN237 (if found in name)
    classification_version INTEGER DEFAULT 1
);
CREATE INDEX IF NOT EXISTS idx_amm_category    ON meta_ads_meta(category);
CREATE INDEX IF NOT EXISTS idx_amm_creative    ON meta_ads_meta(creative_type);
CREATE INDEX IF NOT EXISTS idx_amm_product     ON meta_ads_meta(product);
CREATE INDEX IF NOT EXISTS idx_amm_portal      ON meta_ads_meta(portal);

-- ── Meta: campaign metadata ──────────────────────────────────────────────
CREATE TABLE IF NOT EXISTS meta_campaigns (
    campaign_id   TEXT PRIMARY KEY,
    portal        TEXT,
    account_id    TEXT,
    name          TEXT,
    status        TEXT,
    effective_status TEXT,
    objective     TEXT,
    start_time    TEXT,
    stop_time     TEXT,
    daily_budget  REAL,                     -- in INR (Meta returns paise → /100)
    lifetime_budget REAL,
    last_synced   TEXT
);
CREATE INDEX IF NOT EXISTS idx_mc_portal       ON meta_campaigns(portal);
CREATE INDEX IF NOT EXISTS idx_mc_status       ON meta_campaigns(effective_status);

-- ── Meta: live active-status snapshot ───────────────────────────────────
-- One row per (snapshot_time, account_id). Captures the TRUE count of
-- campaigns + ads in effective_status=ACTIVE at snapshot time, fetched
-- straight from Meta's Graph API (filtered, summary=total_count).
--
-- Why this exists on top of meta_ads_meta.effective_status: the per-ad
-- filter only counts ads that ALSO had spend in the dashboard window.
-- An ad that's ACTIVE but hasn't spent yet (e.g., just turned on mid-day)
-- is still missing. This snapshot is window-independent and matches
-- exactly what Ads Manager shows.
CREATE TABLE IF NOT EXISTS meta_active_snapshot (
    snapshot_time TEXT NOT NULL,                -- ISO timestamp
    portal        TEXT NOT NULL,                -- SM / SML / NBP
    account_id    TEXT NOT NULL,
    account_name  TEXT,
    active_camps  INTEGER NOT NULL DEFAULT 0,
    active_ads    INTEGER NOT NULL DEFAULT 0,
    PRIMARY KEY (snapshot_time, account_id)
);
CREATE INDEX IF NOT EXISTS idx_mas_time   ON meta_active_snapshot(snapshot_time);
CREATE INDEX IF NOT EXISTS idx_mas_portal ON meta_active_snapshot(portal);

-- ── Meta: ad-set targeting (one row per adset_id) ────────────────────────
-- Targeting (custom-audience inclusions/exclusions) lives at the ad-set
-- level in Meta. Cached so we don't hammer the API every ingest — only
-- re-fetch if last_fetched_at is missing or older than 7 days.
CREATE TABLE IF NOT EXISTS meta_adsets (
    adset_id          TEXT PRIMARY KEY,
    portal            TEXT,
    account_id        TEXT,
    campaign_id       TEXT,
    name              TEXT,
    audiences_incl    TEXT,                  -- comma-separated audience names
    audiences_excl    TEXT,                  -- comma-separated audience names
    targeting_summary TEXT,                  -- short human summary (geo + spec etc)
    targeting_json    TEXT,                  -- raw JSON (future use)
    last_fetched_at   TEXT
);
CREATE INDEX IF NOT EXISTS idx_madsets_campaign ON meta_adsets(campaign_id);
CREATE INDEX IF NOT EXISTS idx_madsets_portal   ON meta_adsets(portal);

-- ── Shopify: orders (one row per order) ──────────────────────────────────
CREATE TABLE IF NOT EXISTS shopify_orders (
    order_id      TEXT PRIMARY KEY,
    portal        TEXT NOT NULL,
    order_number  TEXT,                     -- the human "Sxxxx" name
    created_at    TEXT,                     -- ISO
    cancelled_at  TEXT,
    financial_status TEXT,
    total_price   REAL,
    subtotal_price REAL,
    currency      TEXT,
    customer_id   TEXT,
    customer_email TEXT,
    landing_site  TEXT,
    referring_site TEXT,
    source_name   TEXT,
    -- parsed UTMs (from landing_site or note_attributes)
    utm_source    TEXT,
    utm_medium    TEXT,
    utm_campaign  TEXT,                     -- often Meta campaign_id
    utm_content   TEXT,                     -- often Meta ad_id
    utm_term      TEXT
);
CREATE INDEX IF NOT EXISTS idx_so_portal_date  ON shopify_orders(portal, created_at);
CREATE INDEX IF NOT EXISTS idx_so_utm_camp     ON shopify_orders(utm_campaign);
CREATE INDEX IF NOT EXISTS idx_so_utm_content  ON shopify_orders(utm_content);

-- ── Shopify: line items (one row per product per order) ──────────────────
CREATE TABLE IF NOT EXISTS shopify_order_items (
    order_id      TEXT NOT NULL,
    portal        TEXT NOT NULL,
    line_id       TEXT NOT NULL,            -- Shopify line_item.id
    product_id    TEXT,
    variant_id    TEXT,
    sku           TEXT,
    product_title TEXT,
    variant_title TEXT,
    quantity      INTEGER,
    price         REAL,
    line_revenue  REAL,
    PRIMARY KEY (order_id, line_id),
    FOREIGN KEY (order_id) REFERENCES shopify_orders(order_id)
);
CREATE INDEX IF NOT EXISTS idx_soi_product     ON shopify_order_items(product_title);
CREATE INDEX IF NOT EXISTS idx_soi_sku         ON shopify_order_items(sku);
CREATE INDEX IF NOT EXISTS idx_soi_portal      ON shopify_order_items(portal);

-- ── Precomputed daily rollups (powers fast dashboard queries) ────────────
-- Rebuilt nightly after meta + shopify ingest. Keyed by all the dims we
-- want to filter on. NULL means "all" — so a row with category=NULL is
-- the cross-category total.
CREATE TABLE IF NOT EXISTS kpi_daily_rollup (
    date          TEXT NOT NULL,
    portal        TEXT,                     -- NULL = all portals
    category      TEXT,                     -- NULL = all categories
    creative_type TEXT,                     -- NULL = all creative types
    sentiment     TEXT,                     -- NULL = all sentiments
    product       TEXT,                     -- NULL = all products
    n_ads_active        INTEGER DEFAULT 0,  -- ads with spend > 0 this day
    n_campaigns_active  INTEGER DEFAULT 0,
    spend         REAL DEFAULT 0,
    impressions   INTEGER DEFAULT 0,
    reach         INTEGER DEFAULT 0,
    clicks        INTEGER DEFAULT 0,
    purchases     INTEGER DEFAULT 0,
    revenue       REAL DEFAULT 0,
    roas          REAL,                     -- spend-weighted blended
    cpm           REAL,
    ctr           REAL,                     -- aggregated
    cpc           REAL,
    add_to_cart   INTEGER DEFAULT 0,
    landing_page_views INTEGER DEFAULT 0,
    -- Success rate (recomputed for the trailing window ending on this date)
    n_ads_published_3d  INTEGER DEFAULT 0,  -- ads first_seen in last 3 days
    n_ads_survived_7d_3d INTEGER DEFAULT 0, -- of those, how many days_active >= 7
    n_ads_published_7d  INTEGER DEFAULT 0,
    n_ads_survived_7d_7d INTEGER DEFAULT 0,
    n_ads_published_30d INTEGER DEFAULT 0,
    n_ads_survived_7d_30d INTEGER DEFAULT 0,
    success_rate_3d  REAL,                  -- survived/published as %
    success_rate_7d  REAL,
    success_rate_30d REAL
);
CREATE UNIQUE INDEX IF NOT EXISTS idx_kdr_uniq ON kpi_daily_rollup(
    date, portal, category, creative_type, sentiment, product
);
CREATE INDEX IF NOT EXISTS idx_kdr_date ON kpi_daily_rollup(date);

-- ── Sentiment label lookup ───────────────────────────────────────────────
-- User tags ad/campaign names with `_st1_`, `_st2_`, etc. The classifier
-- extracts the code and stores it in meta_ads_meta.sentiment. This table
-- maps codes → human-readable labels so the dashboard can show meaningful
-- text. User updates this table directly when they assign meanings.
CREATE TABLE IF NOT EXISTS sentiment_labels (
    code        TEXT PRIMARY KEY,            -- 'st1', 'st2', etc.
    label       TEXT,                        -- 'Problem-Solution' (set by user)
    description TEXT,                        -- longer explanation, optional
    is_active   INTEGER DEFAULT 1,           -- 0 = retired, hide from dashboard
    created_at  TEXT,
    updated_at  TEXT
);

-- ── Product / NTN code lookup ────────────────────────────────────────────
-- NTN codes appear in ad names like `_NTN237_`. Classifier extracts the
-- code; this table maps it to the product name + category for fast joins.
-- Seeded from the memory file `reference_ntn_codes.md`.
CREATE TABLE IF NOT EXISTS product_ntn_labels (
    ntn_code    TEXT PRIMARY KEY,            -- 'NTN237'
    product     TEXT,                        -- 'AM/PM Pigmentation Combo'
    category    TEXT,                        -- 'Skin' / 'Hair' / 'Crystal' / etc.
    is_active   INTEGER DEFAULT 1,
    notes       TEXT,
    updated_at  TEXT
);

-- ── Provenance: when did each ingestor last run successfully ─────────────
CREATE TABLE IF NOT EXISTS ingest_log (
    job_name      TEXT,                     -- e.g. ingest_meta, ingest_shopify
    target_date   TEXT,                     -- the date being ingested
    started_at    TEXT,
    finished_at   TEXT,
    status        TEXT,                     -- success / failed / partial
    rows_written  INTEGER,
    error_message TEXT,
    PRIMARY KEY (job_name, target_date, started_at)
);
CREATE INDEX IF NOT EXISTS idx_il_status ON ingest_log(status, target_date);

-- ── Antariksh dashboard rollups ──────────────────────────────────────────
-- Pre-aggregated daily numbers so the Antariksh home page renders historical
-- views instantly (calendar / category / creative split all read from here).
-- Rebuilt by scripts/v2/build_antariksh_rollup.py.

-- Meta side, at (date, portal, category, creative_type) grain. spend is REAL
-- ground truth; meta_purchases/meta_revenue are pixel-attributed (used only
-- for the per-category / per-creative split, which Shopify can't provide).
CREATE TABLE IF NOT EXISTS antariksh_daily (
    date           TEXT NOT NULL,
    portal         TEXT NOT NULL,           -- SM / SML / NBP
    category       TEXT NOT NULL,           -- canonical category, 'Other' if untagged
    creative_type  TEXT NOT NULL,           -- Paras/Motion/Static/Partnership/AI/Wanda/Other
    spend          REAL    DEFAULT 0,       -- Meta spend (real Rupees)
    impressions    INTEGER DEFAULT 0,
    clicks         INTEGER DEFAULT 0,
    meta_purchases REAL    DEFAULT 0,       -- Meta-attributed orders (pixel)
    meta_revenue   REAL    DEFAULT 0,       -- Meta-attributed revenue (pixel)
    ad_count       INTEGER DEFAULT 0,       -- distinct ads with spend>0
    PRIMARY KEY (date, portal, category, creative_type)
);
CREATE INDEX IF NOT EXISTS idx_antk_daily_date ON antariksh_daily(date);

-- Shopify GROUND TRUTH per (date, portal): real orders + revenue + prepaid.
-- prepaid_orders / cod_orders drive the Prepaid% block; delivered% needs a
-- courier feed not yet ingested, so it has no column here yet.
CREATE TABLE IF NOT EXISTS antariksh_shopify_daily (
    date           TEXT NOT NULL,
    portal         TEXT NOT NULL,
    orders         INTEGER DEFAULT 0,       -- non-cancelled
    revenue        REAL    DEFAULT 0,       -- SUM(total_price), non-cancelled
    prepaid_orders INTEGER DEFAULT 0,       -- financial_status in (paid, partially_paid)
    cod_orders     INTEGER DEFAULT 0,       -- financial_status = pending (COD proxy)
    PRIMARY KEY (date, portal)
);

-- 5-minute live snapshot history: today's running Shopify sales/orders +
-- hourly Meta spend. 'ALL' row = all portals combined. Powers the hero KPIs
-- and the live sparkline; older rows kept for the intraday trend.
CREATE TABLE IF NOT EXISTS antariksh_live (
    ts      TEXT NOT NULL,                  -- ISO timestamp of the snapshot
    portal  TEXT NOT NULL,                  -- SM / SML / NBP / ALL
    sales   REAL    DEFAULT 0,              -- today's Shopify sales so far
    orders  INTEGER DEFAULT 0,              -- today's order count so far
    spend   REAL    DEFAULT 0,              -- today's Meta spend (hourly refresh)
    PRIMARY KEY (ts, portal)
);
