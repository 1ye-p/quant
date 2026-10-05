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
    updated_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    -- IC 口径版本（A3-3）：'v2_top20' = Top20% 截面口径（A3-1/2 统一后）；
    -- NULL = 历史行（旧 Top100 口径，未重算）
    algo_version VARCHAR
);
