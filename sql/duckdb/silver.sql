-- silver.sql — Normalized, canonical market data tables.
-- All prices in local currency; timestamps UTC-aware.

-- ── Instrument master ──────────────────────────────────────────────────────────
CREATE TABLE IF NOT EXISTS silver_assets (
    asset_id          VARCHAR PRIMARY KEY,     -- '{exchange}:{symbol}', e.g. 'SSE:600036'
    symbol            VARCHAR NOT NULL,
    exchange          VARCHAR NOT NULL,        -- Exchange enum value
    asset_class       VARCHAR NOT NULL,
    currency          VARCHAR NOT NULL,
    name              VARCHAR DEFAULT '',
    name_en           VARCHAR DEFAULT '',
    status            VARCHAR DEFAULT 'active',
    lot_size          INTEGER DEFAULT 100,
    tick_size         DECIMAL(18, 8) DEFAULT 0.01,
    list_date         DATE,
    delist_date       DATE,
    industry          VARCHAR,
    sector            VARCHAR,
    effective_from    DATE NOT NULL,
    effective_to      DATE,                    -- NULL = current
    updated_at        TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

-- ── Daily OHLCV bars ──────────────────────────────────────────────────────────
CREATE TABLE IF NOT EXISTS silver_prices_1d (
    asset_id          VARCHAR NOT NULL,
    trade_date        DATE NOT NULL,
    open              DECIMAL(18, 6) NOT NULL,
    high              DECIMAL(18, 6) NOT NULL,
    low               DECIMAL(18, 6) NOT NULL,
    close             DECIMAL(18, 6) NOT NULL,
    volume            DECIMAL(24, 2) NOT NULL,
    amount            DECIMAL(24, 2) DEFAULT 0,  -- Turnover in local currency
    adj_factor        DECIMAL(18, 8) DEFAULT 1,  -- Corporate action factor
    adj_close         DECIMAL(18, 6),             -- Forward-adjusted close
    is_suspended      BOOLEAN DEFAULT FALSE,
    limit_up          DECIMAL(18, 6),
    limit_down        DECIMAL(18, 6),
    source            VARCHAR NOT NULL,
    ingestion_id      VARCHAR,
    PRIMARY KEY (asset_id, trade_date)
);

CREATE INDEX IF NOT EXISTS idx_silver_prices_1d_date
    ON silver_prices_1d (trade_date);

-- ── Corporate actions ─────────────────────────────────────────────────────────
CREATE TABLE IF NOT EXISTS silver_corporate_actions (
    action_id         VARCHAR PRIMARY KEY,
    asset_id          VARCHAR NOT NULL,
    action_type       VARCHAR NOT NULL,        -- 'dividend', 'split', 'rights', 'merger'
    ex_date           DATE NOT NULL,
    record_date       DATE,
    pay_date          DATE,
    ratio             DECIMAL(18, 8),          -- Split ratio or rights ratio
    cash_amount       DECIMAL(18, 6),          -- Dividend cash amount per share
    currency          VARCHAR,
    description       VARCHAR,
    source            VARCHAR NOT NULL,
    created_at        TIMESTAMPTZ DEFAULT NOW()
);

CREATE INDEX IF NOT EXISTS idx_silver_corporate_actions_asset_date
    ON silver_corporate_actions (asset_id, ex_date);

-- ── Trading calendar ──────────────────────────────────────────────────────────
CREATE TABLE IF NOT EXISTS silver_trading_calendar (
    exchange          VARCHAR NOT NULL,
    trade_date        DATE NOT NULL,
    is_open           BOOLEAN NOT NULL,
    open_time         VARCHAR,
    close_time        VARCHAR,
    source            VARCHAR NOT NULL,
    PRIMARY KEY (exchange, trade_date)
);

-- ── Dataset versions / lineage ────────────────────────────────────────────────
CREATE TABLE IF NOT EXISTS silver_dataset_versions (
    version_id        VARCHAR PRIMARY KEY,
    dataset_name      VARCHAR NOT NULL,
    frequency         VARCHAR NOT NULL,
    start_date        DATE NOT NULL,
    end_date          DATE NOT NULL,
    asset_count       INTEGER,
    row_count         BIGINT,
    storage_uri       VARCHAR,
    content_hash      VARCHAR(64),
    source            VARCHAR NOT NULL,
    created_at        TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    is_current        BOOLEAN DEFAULT TRUE
);

-- ── Fundamental data ──────────────────────────────────────────────────────────
CREATE TABLE IF NOT EXISTS silver_fundamentals (
    asset_id            VARCHAR NOT NULL,
    report_date         DATE NOT NULL,          -- Period end date (e.g., 2024-12-31)
    pe_ttm              DOUBLE,
    pb                  DOUBLE,
    ps_ttm              DOUBLE,
    ev_ebitda           DOUBLE,
    dividend_yield      DOUBLE,
    roe                 DOUBLE,
    roa                 DOUBLE,
    gross_margin        DOUBLE,
    net_margin          DOUBLE,
    revenue_growth_yoy  DOUBLE,
    earnings_growth_yoy DOUBLE,
    market_cap          DOUBLE,
    announce_date       DATE,           -- PIT: actual disclosure date
    total_assets        DOUBLE,
    total_debt          DOUBLE,
    source              VARCHAR DEFAULT 'tushare',
    updated_at          TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    PRIMARY KEY (asset_id, report_date)
);

CREATE INDEX IF NOT EXISTS idx_silver_fundamentals_asset
    ON silver_fundamentals (asset_id, report_date DESC);

-- PIT migration: announce_date index + deprecate market_cap
CREATE INDEX IF NOT EXISTS idx_silver_fundamentals_announce
    ON silver_fundamentals (asset_id, announce_date DESC);

-- market_cap deprecated: PIT-incorrect. Use silver_valuation_daily.market_cap
COMMENT ON COLUMN silver_fundamentals.market_cap IS 'DEPRECATED: PIT-incorrect. Use silver_valuation_daily.market_cap';

CREATE TABLE IF NOT EXISTS silver_valuation_daily (
    asset_id        VARCHAR NOT NULL,
    trade_date      DATE NOT NULL,
    pe_ttm          DOUBLE,
    pb              DOUBLE,
    ps_ttm          DOUBLE,
    market_cap      DOUBLE,
    turnover_rate   DOUBLE,
    dividend_yield  DOUBLE,
    source          VARCHAR DEFAULT 'tushare',
    updated_at      TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    PRIMARY KEY (asset_id, trade_date)
);

CREATE INDEX IF NOT EXISTS idx_silver_valuation_daily_date
    ON silver_valuation_daily (trade_date);

-- ── External indicators (D1-A: market-level rows use '__MARKET__' sentinel) ──
CREATE TABLE IF NOT EXISTS silver_external_indicators (
    source          VARCHAR NOT NULL,
    indicator_key   VARCHAR NOT NULL,
    asset_id        VARCHAR NOT NULL DEFAULT '__MARKET__',
    trade_date      DATE NOT NULL,
    value           DOUBLE,
    available_date  DATE NOT NULL,
    updated_at      TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    PRIMARY KEY (source, indicator_key, asset_id, trade_date)
);

CREATE INDEX IF NOT EXISTS idx_ext_ind_date
    ON silver_external_indicators (indicator_key, trade_date);
