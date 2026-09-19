-- gold.sql — Research and analytics mart tables (factor values, signals, runs).

-- ── Universe membership ────────────────────────────────────────────────────────
CREATE TABLE IF NOT EXISTS gold_universe_membership (
    universe_id       VARCHAR NOT NULL,
    trade_date        DATE NOT NULL,
    asset_id          VARCHAR NOT NULL,
    weight_hint       DECIMAL(10, 6) DEFAULT 0,  -- Optional target weight hint
    membership_reason VARCHAR,
    PRIMARY KEY (universe_id, trade_date, asset_id)
);

-- ── Factor values ─────────────────────────────────────────────────────────────
CREATE TABLE IF NOT EXISTS gold_factor_values (
    feature_set_version VARCHAR NOT NULL,
    factor_name         VARCHAR NOT NULL,
    trade_date          DATE NOT NULL,
    asset_id            VARCHAR NOT NULL,
    value               DOUBLE NOT NULL,
    asof_ts             TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    PRIMARY KEY (feature_set_version, factor_name, trade_date, asset_id)
);

CREATE INDEX IF NOT EXISTS idx_gold_factor_values_date_factor
    ON gold_factor_values (trade_date, factor_name);

-- ── Factor materialization cache ──────────────────────────────────────────────
-- Records the content fingerprint of silver_prices_1d under which a
-- feature_set_version was materialized. A run recomputes the fingerprint (a
-- digest of OHLCV/adj_factor aggregates over the date range) and, if it matches
-- the cached value, reuses the existing factor values instead of recomputing.
-- Auto-invalidates whenever silver_prices_1d content changes (corrections,
-- backfills, adj_factor updates).
CREATE TABLE IF NOT EXISTS gold_factor_cache_meta (
    feature_set_version VARCHAR NOT NULL,
    data_fingerprint    VARCHAR,
    materialized_at     TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    PRIMARY KEY (feature_set_version)
);

-- ── Signals ────────────────────────────────────────────────────────────────────
CREATE TABLE IF NOT EXISTS gold_signals (
    signal_set_version  VARCHAR NOT NULL,
    strategy_id         VARCHAR NOT NULL,
    trade_date          DATE NOT NULL,
    asset_id            VARCHAR NOT NULL,
    signal              DOUBLE NOT NULL,
    direction           VARCHAR,
    confidence          DOUBLE DEFAULT 1.0,
    target_weight       DOUBLE,
    PRIMARY KEY (signal_set_version, strategy_id, trade_date, asset_id)
);

-- ── ML predictions ────────────────────────────────────────────────────────────
CREATE TABLE IF NOT EXISTS gold_predictions (
    model_version       VARCHAR NOT NULL,
    trade_date          DATE NOT NULL,
    asset_id            VARCHAR NOT NULL,
    prediction          DOUBLE NOT NULL,
    horizon             VARCHAR NOT NULL,        -- e.g. '1d', '5d', '20d'
    label_name          VARCHAR NOT NULL,        -- e.g. 'ret_5d', 'direction'
    confidence          DOUBLE,
    PRIMARY KEY (model_version, trade_date, asset_id, label_name)
);

-- ── Backtest runs ──────────────────────────────────────────────────────────────
CREATE TABLE IF NOT EXISTS gold_backtest_runs (
    run_id              VARCHAR PRIMARY KEY,
    engine              VARCHAR NOT NULL,        -- 'vector' | 'event'
    strategy_id         VARCHAR NOT NULL,
    dataset_version     VARCHAR NOT NULL,
    signal_set_version  VARCHAR,
    cost_model_config   JSON,
    risk_policy_version VARCHAR,
    started_at          TIMESTAMPTZ NOT NULL,
    completed_at        TIMESTAMPTZ,
    status              VARCHAR DEFAULT 'running',  -- 'running' | 'completed' | 'failed'
    metrics_uri         VARCHAR,                 -- Path to metrics Parquet artifact
    tearsheet_uri       VARCHAR,
    error_message       VARCHAR,
    tags                JSON,
    is_walk_forward     BOOLEAN DEFAULT FALSE,
    n_folds             INTEGER,
    aggregated_metrics_json JSON
);

-- ── Pre-trade risk decisions ───────────────────────────────────────────────────
CREATE TABLE IF NOT EXISTS gold_pretrade_decisions (
    decision_id         VARCHAR PRIMARY KEY,
    run_id              VARCHAR NOT NULL,
    engine              VARCHAR NOT NULL,
    trade_date          DATE NOT NULL,
    strategy_id         VARCHAR NOT NULL,
    asset_id            VARCHAR NOT NULL,
    requested_qty       DECIMAL(24, 6),
    approved_qty        DECIMAL(24, 6),
    decision            VARCHAR NOT NULL,        -- 'approved' | 'clipped' | 'rejected'
    reasons             JSON,
    policy_names        JSON
);

-- ── Portfolio risk snapshots ───────────────────────────────────────────────────
CREATE TABLE IF NOT EXISTS gold_risk_snapshots (
    snapshot_id         VARCHAR PRIMARY KEY,
    run_id              VARCHAR NOT NULL,
    snapshot_ts         TIMESTAMPTZ NOT NULL,
    strategy_id         VARCHAR NOT NULL,
    gross_leverage      DOUBLE DEFAULT 0,
    net_leverage        DOUBLE DEFAULT 0,
    beta                DOUBLE,
    drawdown            DOUBLE DEFAULT 0,
    var_95              DOUBLE,
    cvar_95             DOUBLE,
    sector_exposure     JSON,
    factor_exposure     JSON
);

-- ── Risk budgets ──────────────────────────────────────────────────────────────
CREATE TABLE IF NOT EXISTS gold_risk_budgets (
    budget_id           VARCHAR PRIMARY KEY,
    run_id              VARCHAR NOT NULL,
    strategy_id         VARCHAR NOT NULL,
    risk_budget         DOUBLE,
    capital_budget      DOUBLE,
    turnover_budget     DOUBLE,
    effective_from      DATE,
    effective_to        DATE
);

-- ── Fills (order executions) ─────────────────────────────────────────────────
CREATE TABLE IF NOT EXISTS gold_fills (
    fill_id             VARCHAR PRIMARY KEY,
    run_id              VARCHAR NOT NULL,
    trade_date          DATE NOT NULL,
    asset_id            VARCHAR NOT NULL,
    side                VARCHAR NOT NULL,        -- 'buy' | 'sell'
    qty                 INTEGER NOT NULL,
    price               DOUBLE NOT NULL,
    notional            DOUBLE NOT NULL,
    commission          DOUBLE DEFAULT 0,
    stamp_duty          DOUBLE DEFAULT 0,
    slippage            DOUBLE DEFAULT 0,
    total_cost          DOUBLE DEFAULT 0,
    raw_close           DOUBLE,                 -- unadjusted execution price (= adjusted close / adj_factor)
    created_at          TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

CREATE INDEX IF NOT EXISTS idx_gold_fills_run_date
    ON gold_fills (run_id, trade_date);

CREATE INDEX IF NOT EXISTS idx_gold_fills_asset_date
    ON gold_fills (asset_id, trade_date);

-- ── Portfolio snapshots ──────────────────────────────────────────────────────
CREATE TABLE IF NOT EXISTS gold_portfolio_snapshots (
    snapshot_id         VARCHAR PRIMARY KEY,
    run_id              VARCHAR NOT NULL,
    trade_date          DATE NOT NULL,
    cash                DOUBLE NOT NULL,
    nav                 DOUBLE NOT NULL,
    portfolio_return    DOUBLE,
    positions_count     INTEGER DEFAULT 0,
    gross_exposure      DOUBLE DEFAULT 0,
    net_exposure        DOUBLE DEFAULT 0,
    created_at          TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

CREATE INDEX IF NOT EXISTS idx_gold_portfolio_snapshots_run_date
    ON gold_portfolio_snapshots (run_id, trade_date);

-- ── Share links ────────────────────────────────────────────────────────────
CREATE TABLE IF NOT EXISTS shares (
    share_id      VARCHAR PRIMARY KEY,
    content_type  VARCHAR NOT NULL,        -- 'backtest', 'strategy', 'factor', 'report'
    content_id    VARCHAR NOT NULL,
    created_by    VARCHAR,
    created_at    TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
    expires_at    TIMESTAMP
);

-- ── AI research reports (Phase 4 T6) ───────────────────────────────────────
CREATE TABLE IF NOT EXISTS gold_research_reports (
    report_id   VARCHAR PRIMARY KEY,
    run_id      VARCHAR NOT NULL,
    content_md  VARCHAR NOT NULL,
    created_at  TIMESTAMPTZ NOT NULL
);
