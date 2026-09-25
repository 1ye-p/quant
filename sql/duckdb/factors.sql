-- Factor research layer: IC summary tables consumed by /factors routes
-- (leaderboard / ic-status / ic-trend). Loaded by Catalog.initialize().

CREATE TABLE IF NOT EXISTS gold_factor_ic_summary (
    factor_name VARCHAR PRIMARY KEY,
    ic_mean DOUBLE,
    icir DOUBLE,
    ic_positive_pct DOUBLE,
    n INTEGER,
    window_start DATE,
    window_end DATE,
    updated_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
);
