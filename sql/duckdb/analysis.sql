-- analysis.sql — Backtest analysis mart tables.

CREATE TABLE IF NOT EXISTS gold_bt_analysis_runs (
    analysis_run_id         VARCHAR PRIMARY KEY,
    backtest_run_id         VARCHAR NOT NULL,
    overall_overfit_score   DOUBLE NOT NULL,
    dsr                     DOUBLE NOT NULL,
    psr                     DOUBLE NOT NULL,
    summary                 VARCHAR,
    created_at              TIMESTAMPTZ NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_bt_analysis_runs_backtest
    ON gold_bt_analysis_runs (backtest_run_id);

CREATE TABLE IF NOT EXISTS gold_bt_tca (
    analysis_run_id     VARCHAR PRIMARY KEY,
    total_turnover      DOUBLE,
    total_commission    DOUBLE,
    total_stamp_duty    DOUBLE,
    total_slippage      DOUBLE,
    total_cost          DOUBLE,
    cost_per_trade      DOUBLE,
    cost_pct_turnover   DOUBLE,
    num_trades          INTEGER,
    avg_trade_size      DOUBLE
);

CREATE TABLE IF NOT EXISTS gold_bt_validation_windows (
    analysis_run_id         VARCHAR NOT NULL,
    window_id               INTEGER NOT NULL,
    method                  VARCHAR NOT NULL,    -- 'walk_forward' | 'cpcv'
    train_start             DATE,
    train_end               DATE,
    test_start              DATE NOT NULL,
    test_end                DATE NOT NULL,
    metrics_json            JSON,
    PRIMARY KEY (analysis_run_id, method, window_id)
);

CREATE TABLE IF NOT EXISTS gold_bt_multiple_testing (
    analysis_run_id         VARCHAR NOT NULL,
    method                  VARCHAR NOT NULL,    -- 'bonferroni' | 'bhy' | 'bailey_lopez'
    n_trials                INTEGER NOT NULL,
    alpha                   DOUBLE NOT NULL,
    results_json            JSON,
    accepted                BOOLEAN NOT NULL,
    PRIMARY KEY (analysis_run_id, method)
);

-- gold_risk_rolling
CREATE TABLE IF NOT EXISTS gold_risk_rolling (
    run_id VARCHAR,
    trade_date DATE,
    "window" INTEGER,
    rolling_var DOUBLE,
    rolling_cvar DOUBLE,
    rolling_vol DOUBLE,
    rolling_sharpe DOUBLE,
    rolling_beta DOUBLE,
    PRIMARY KEY (run_id, trade_date, "window")
);

-- gold_drawdown_periods
CREATE TABLE IF NOT EXISTS gold_drawdown_periods (
    run_id VARCHAR,
    period_id INTEGER,
    start_date DATE,
    trough_date DATE,
    recovery_date DATE,
    max_drawdown DOUBLE,
    duration_days INTEGER,
    recovery_days INTEGER,
    underwater_days INTEGER,
    PRIMARY KEY (run_id, period_id)
);
