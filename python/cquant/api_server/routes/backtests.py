"""Backtest run routes."""

from __future__ import annotations

import asyncio
import html
import json
import logging
import math
import pathlib
import re
import time
import uuid
from datetime import date, datetime, timezone

from fastapi import APIRouter, BackgroundTasks, HTTPException, Query
from fastapi.responses import HTMLResponse, Response
from pydantic import BaseModel, Field

from cquant.api_server.deps import CatalogDep, run_job_async

_ARTIFACTS_BASE = pathlib.Path("data/backtest_artifacts").resolve()
_UUID_RE = re.compile(r'^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$')


def _safe_metrics_path(run_id: str) -> pathlib.Path | None:
    """Return path to metrics file only if run_id is a valid UUID and resolves within base dir."""
    if not _UUID_RE.match(run_id):
        return None
    p = (_ARTIFACTS_BASE / f"{run_id}.json").resolve()
    if not str(p).startswith(str(_ARTIFACTS_BASE)):
        return None
    return p


def _html_to_pdf(html_bytes: bytes) -> bytes | None:
    """Convert HTML bytes to PDF bytes. Returns None if no PDF engine is available.

    Tries weasyprint first (lighter, no browser process), then playwright as fallback.
    """
    # Try weasyprint
    try:
        from weasyprint import HTML  # type: ignore[import-untyped]

        return HTML(string=html_bytes.decode("utf-8")).write_pdf()
    except ImportError:
        pass
    except Exception as exc:
        logger.warning("weasyprint PDF generation failed: %s", exc)

    # Try playwright (synchronous API)
    try:
        from playwright.sync_api import sync_playwright  # type: ignore[import-untyped]

        with sync_playwright() as p:
            browser = p.chromium.launch(headless=True)
            try:
                page = browser.new_page()
                page.set_content(html_bytes.decode("utf-8"), wait_until="networkidle")
                pdf = page.pdf(format="A4", print_background=True)
                return pdf
            finally:
                browser.close()
    except ImportError:
        pass
    except Exception as exc:
        logger.warning("playwright PDF generation failed: %s", exc)

    return None


def _fmt_metric(key: str, value: float | None) -> dict:
    """格式化单个指标为模板友好的 dict。

    Tuple format: (label, is_pct, invert)
    invert=True: higher value is worse (e.g. volatility, drawdown) — always show as "negative" class.
    invert=False: higher is better — positive=green, negative=red.
    """
    METRIC_LABELS: dict[str, tuple[str, bool, bool]] = {
        "total_return":       ("总收益率",         True,  False),
        "annualized_return":  ("年化收益",          True,  False),
        "cagr":               ("年化收益（CAGR）",   True,  False),
        "sharpe_ratio":       ("Sharpe Ratio",     False, False),
        "sortino_ratio":      ("Sortino Ratio",    False, False),
        "calmar_ratio":       ("Calmar Ratio",     False, False),
        "win_rate":           ("胜率",              True,  False),
        "max_drawdown":       ("最大回撤",          True,  True),   # always bad
        "annualized_volatility": ("年化波动率",         True,  True),   # always bad
        "information_ratio":  ("信息比率（IR）",      False, False),
        "tracking_error":     ("跟踪误差（TE）",      True,  True),   # always bad
        "alpha":              ("超额收益 Alpha",     True,  False),
        "beta":               ("Beta",              False, False),
    }
    label, is_pct, invert = METRIC_LABELS.get(key, (key, False, False))
    if value is None:
        return {"label": label, "value": "—", "cls": ""}
    display = f"{value * 100:.2f}%" if is_pct else f"{value:.3f}"
    if value == 0:
        cls = ""
    elif invert:
        cls = "negative"          # high value = bad for these metrics
    else:
        cls = "positive" if value > 0 else "negative"
    return {"label": label, "value": display, "cls": cls}


logger = logging.getLogger(__name__)

router = APIRouter(prefix="/backtests", tags=["backtests"])

# ── Job persistence (DuckDB-backed) ──────────────────────────────────────────

_JOB_DDL = """
CREATE TABLE IF NOT EXISTS _api_jobs (
    job_id     VARCHAR PRIMARY KEY,
    job_type   VARCHAR NOT NULL DEFAULT 'backtest',
    status     VARCHAR NOT NULL DEFAULT 'running',
    run_id     VARCHAR,
    error      VARCHAR,
    created_at TIMESTAMP NOT NULL,
    updated_at TIMESTAMP NOT NULL
)
"""


def _ensure_job_table(catalog) -> None:
    """Create the _api_jobs table if it doesn't exist."""
    try:
        catalog.execute(_JOB_DDL)
    except Exception as exc:
        logger.debug("_ensure_job_table: %s (likely already exists)", exc)


_schema_ensured = False


def _ensure_schema_extensions(catalog) -> None:
    """Add optional columns to gold_backtest_runs (idempotent, runs once per process)."""
    global _schema_ensured
    if _schema_ensured:
        return
    try:
        catalog.execute(
            "ALTER TABLE gold_backtest_runs ADD COLUMN IF NOT EXISTS benchmark_asset_id VARCHAR DEFAULT ''"
        )
    except Exception as exc:
        logger.debug("_ensure_schema_extensions: %s", exc)
    _schema_ensured = True


def _save_job(catalog, job_id: str, job_type: str, status: str,
              run_id: str | None = None, error: str | None = None) -> None:
    """Insert or update a job record in DuckDB. Preserves created_at on updates."""
    now = datetime.now(tz=timezone.utc).isoformat()
    try:
        catalog.execute(
            "INSERT INTO _api_jobs "
            "(job_id, job_type, status, run_id, error, created_at, updated_at) "
            "VALUES (?, ?, ?, ?, ?, ?, ?) "
            "ON CONFLICT (job_id) DO UPDATE SET "
            "status = excluded.status, run_id = excluded.run_id, "
            "error = excluded.error, created_at = _api_jobs.created_at, "
            "updated_at = excluded.updated_at",
            [job_id, job_type, status, run_id, error, now, now],
        )
    except Exception as exc:
        logger.warning("Failed to persist job %s: %s", job_id, exc)


def _load_job(catalog, job_id: str) -> dict | None:
    """Load a job record from DuckDB. Returns None if not found."""
    try:
        df = catalog.query(
            "SELECT job_id, job_type, status, run_id, error FROM _api_jobs WHERE job_id = ?",
            [job_id],
        )
        if df.is_empty():
            return None
        row = df.to_dicts()[0]
        return {
            "status": row["status"],
            "run_id": row["run_id"],
            "error": row["error"],
        }
    except Exception:
        return None


from cquant.api_server.schemas.common import WalkForwardConfig


class MLTrainConfig(BaseModel):
    """Configuration for one-click ML training before backtest.

    Only used when strategy_type == "MLModelStrategy" and train_mode == "new".
    """
    train_mode: str = "existing"  # "existing" | "new"
    model_type: str = "lgbm"  # "lgbm" | "xgb" | qlib model types
    model_id_prefix: str = "ml"  # prefix for the generated model_id
    n_splits: int = 3  # walk-forward folds
    gap_days: int = 5  # purge gap between train/validation
    model_params: dict | None = None  # hyperparameter overrides


class BacktestCreateBody(BaseModel):
    strategy_id: str
    dataset_version: str
    start_date: str  # ISO date
    end_date: str  # ISO date
    top_n: int = 10
    sort_factor: str = "ret_20d"
    feature_set_version: str = ""
    # ML strategy support
    strategy_type: str = "StaticTopN"  # "StaticTopN" | "MLModelStrategy" | "MultiFactor" | "MarketNeutral" | "SectorRotation" | "Combo"
    model_version: str = ""  # required when strategy_type == "MLModelStrategy" (and train_mode != "new")
    label_name: str = "ret_5d"  # prediction label for MLModelStrategy
    # One-click ML train+backtest config
    ml_config: MLTrainConfig | None = None
    # Dataset split (OOS)
    train_end_date: str = ""  # if set, backtest only runs on data after this date (OOS)
    # Walk-forward config (optional)
    walk_forward: WalkForwardConfig | None = None
    eval_mode: str | None = None  # "train" | "valid" | "test" | "all"
    # MarketNeutral params
    short_n: int = 10
    # SectorRotation params
    sector_map: dict[str, str] = {}
    top_sectors: int = 3
    top_n_per_sector: int = 3
    # Combo params
    sub_strategy_configs: list[dict] = []
    combo_method: str = "equal_weight"
    # CustomWeightStrategy params
    custom_weights: dict[str, float] | None = None
    # IndicatorSignalStrategy params
    entry_conditions: list[str] | None = None
    exit_conditions: list[str] | None = None
    indicator_specs: list[dict] | None = None
    # Universe filtering
    universe_id: str = "all"
    # Benchmark
    benchmark_asset_id: str = ""
    # Cross-sectional scoring integration
    scoring_run_id: str = ""  # if set, use pre-computed scores as ranking signal
    # MultiFactor missing-factor handling
    missing_factor_strategy: str = "fill_0"
    penalty_per_missing: float = 0.5
    # MultiFactor weights (UI strategy config -> engine). None = legacy fallback.
    factor_weights: dict[str, float] | None = None
    # DSL strategy spec (StrategyDSL dict). None = read from strategy config
    # when strategy_type == "DSL" (same config-priority semantics as factor_weights).
    dsl_spec: dict | None = None
    # BreakoutPullback params
    breakout_config: dict | None = None


def _run_backtest(catalog, spec):
    """Run backtest in thread pool (CPU-bound)."""
    from cquant.backtest_vector.run import BacktestRunner

    runner = BacktestRunner(catalog)
    return runner.run(spec)


def _validate_factor_weights(
    factor_weights: dict[str, float] | None,
    factors: list[str],
) -> dict[str, float] | None:
    """Validate factor_weights against the strategy's factor list.

    Rules:
    - None (legacy strategies without weights) passes through unchanged.
    - Every key must be in the strategy's factors list — unknown keys are
      rejected (with a warning log) rather than silently ignored.
    - All-zero weights are rejected (degenerate composite score).

    Raises ValueError on invalid input.
    """
    if factor_weights is None:
        return None
    if not factor_weights:
        raise ValueError("factor_weights 不能为空：请在策略配置中为各因子设置权重")
    unknown = [k for k in factor_weights if k not in factors]
    if unknown:
        logger.warning(
            "factor_weights 包含策略 factors 之外的因子（将被拒绝）: %s (factors=%s)",
            unknown, factors,
        )
        raise ValueError(
            f"factor_weights 含无效因子: {unknown}，不在策略因子列表 {factors} 中"
        )
    if all(w == 0 for w in factor_weights.values()):
        raise ValueError("factor_weights 全为零：合成得分恒为 0，请设置非零权重")
    return factor_weights


def _load_metrics(path: pathlib.Path) -> dict:
    """Load metrics JSON from an artifacts file (blocking I/O, run via to_thread)."""
    with open(path) as f:
        return json.load(f)


@router.post("", status_code=201)
async def create_backtest(
    body: BacktestCreateBody,
    background_tasks: BackgroundTasks,
    catalog: CatalogDep,
) -> dict:
    """触发回测，立即返回 job_id（后台异步执行）。"""
    # Look up strategy config to get top_n / sort_factor if not overridden
    strat_df = catalog.query(
        "SELECT parsed_config FROM meta_strategy_configs WHERE strategy_id = ?",
        [body.strategy_id],
    )
    if strat_df.is_empty():
        raise HTTPException(status_code=404, detail=f"Strategy '{body.strategy_id}' not found")

    parsed = json.loads(strat_df["parsed_config"].item()) if strat_df["parsed_config"].item() else {}

    # Use strategy config values as defaults, allow overrides from request body
    top_n = body.top_n if body.top_n != 10 else parsed.get("top_n", 10)
    sort_factor = body.sort_factor if body.sort_factor != "ret_20d" else (
        parsed.get("factors", ["ret_20d"])[0] if parsed.get("factors") else "ret_20d"
    )

    # MultiFactor weights: strategy config first (UI stores factor_weights
    # here as part of the saved config); an explicit non-None body value
    # overrides for a single run (including {} which is then rejected by
    # validation). None for legacy strategies → engine falls back to the
    # historical {sort_factor: 1.0} behavior.
    cfg_factors = list(parsed.get("factors", []))
    factor_weights_raw = parsed.get("factor_weights")
    if body.factor_weights is not None:
        factor_weights_raw = body.factor_weights
    try:
        factor_weights = _validate_factor_weights(factor_weights_raw, cfg_factors)
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e))

    # ML strategy params: read from strategy config first, then request body
    strategy_type = body.strategy_type if body.strategy_type != "StaticTopN" else parsed.get("strategy_type", "StaticTopN")

    # DSL spec: strategy config first (dslToStrategyConfig stores dsl_spec in
    # the saved config); an explicit non-None body value overrides for a
    # single run (same semantics as factor_weights above).
    dsl_spec = body.dsl_spec
    if dsl_spec is None and strategy_type == "DSL":
        dsl_spec = parsed.get("dsl_spec")

    # Request-time DSL validation: reject malformed/missing specs with 400
    # instead of failing asynchronously inside the background job.
    if strategy_type == "DSL":
        if not dsl_spec:
            raise HTTPException(
                status_code=400,
                detail="dsl_spec is required for DSL strategy (in request body or saved strategy config).",
            )
        from cquant.strategy_dsl.schema import StrategyDSL
        from pydantic import ValidationError as PydanticValidationError
        try:
            StrategyDSL.from_dict(dsl_spec)
        except PydanticValidationError as e:
            raise HTTPException(
                status_code=400,
                detail=f"Invalid dsl_spec: {e.errors()[:5]}",
            )

    model_version = body.model_version or parsed.get("model_id", "")
    label_name = body.label_name if body.label_name != "ret_5d" else parsed.get("label_name", "ret_5d")

    # ── One-click ML train+backtest ───────────────────────────────────────────
    # If strategy_type == "MLModelStrategy" and ml_config.train_mode == "new",
    # train the model first and inject model_id into backtest.
    ml_config = body.ml_config
    if strategy_type == "MLModelStrategy" and ml_config and ml_config.train_mode == "new":
        # Auto-detect feature_set_version for ML training if not set
        ml_feature_set = body.feature_set_version
        if not ml_feature_set:
            fsv_df = catalog.query(
                "SELECT feature_set_version FROM gold_factor_values "
                "GROUP BY feature_set_version ORDER BY MAX(trade_date) DESC LIMIT 1"
            )
            if not fsv_df.is_empty():
                ml_feature_set = fsv_df["feature_set_version"].item()
        if not ml_feature_set:
            raise HTTPException(
                status_code=422,
                detail="ML train_mode='new' requires feature_set_version. "
                       "Run the factor pipeline first or provide feature_set_version.",
            )

        # Infer feature names from factor values
        factor_df = catalog.query(
            "SELECT DISTINCT factor_name FROM gold_factor_values WHERE feature_set_version = ?",
            [ml_feature_set],
        )
        all_factors = factor_df["factor_name"].to_list() if not factor_df.is_empty() else []
        feature_names = [f for f in all_factors if f != label_name]
        if not feature_names:
            raise HTTPException(
                status_code=422,
                detail=f"No factor values found for feature_set_version='{ml_feature_set}'. "
                       "Run the factor pipeline first.",
            )

        # Load dataset
        from cquant.ml_lab.datasets import MLDataset

        dataset = MLDataset.from_catalog(
            catalog=catalog,
            feature_set_version=ml_feature_set,
            feature_names=feature_names,
            target_name=label_name,
        )

        # Run the ML pipeline synchronously (inside the async endpoint via to_thread)
        from cquant.ml_lab.pipeline import run_ml_prediction_pipeline

        try:
            model_version = await asyncio.to_thread(
                run_ml_prediction_pipeline,
                catalog=catalog,
                features=dataset.data,
                target_col=label_name,
                model_id_prefix=ml_config.model_id_prefix,
                n_splits=ml_config.n_splits,
                gap_days=ml_config.gap_days,
                model_type=ml_config.model_type,
                model_params=ml_config.model_params,
            )
            logger.info("One-click ML training completed: model_id=%s", model_version)
        except Exception as exc:
            logger.exception("One-click ML training failed")
            raise HTTPException(
                status_code=500,
                detail="ML training failed. Check server logs for details.",
            )
    # ── End one-click ML train+backtest ───────────────────────────────────────

    # MarketNeutral / SectorRotation / Combo params
    short_n = body.short_n if body.short_n != 10 else parsed.get("short_n", 10)
    sector_map = body.sector_map or parsed.get("sector_map", {})
    top_sectors = body.top_sectors if body.top_sectors != 3 else parsed.get("top_sectors", 3)
    top_n_per_sector = body.top_n_per_sector if body.top_n_per_sector != 3 else parsed.get("top_n_per_sector", 3)
    sub_strategy_configs = body.sub_strategy_configs or parsed.get("sub_strategy_configs", [])
    combo_method = body.combo_method if body.combo_method != "equal_weight" else parsed.get("combo_method", "equal_weight")
    custom_weights = body.custom_weights or parsed.get("custom_weights", {}) or {}
    entry_conditions = body.entry_conditions or parsed.get("entry_conditions", [])
    exit_conditions = body.exit_conditions or parsed.get("exit_conditions", [])
    indicator_specs = body.indicator_specs or parsed.get("indicator_specs", [])
    universe_id = body.universe_id if body.universe_id != "all" else parsed.get("universe_id", "all")
    missing_factor_strategy = body.missing_factor_strategy if body.missing_factor_strategy != "fill_0" else parsed.get("missing_factor_handling", "fill_0")
    penalty_per_missing = body.penalty_per_missing if body.penalty_per_missing != 0.5 else parsed.get("penalty_per_missing", 0.5)

    from cquant.backtest_vector.run import BacktestRunSpec

    try:
        start = date.fromisoformat(body.start_date)
        end = date.fromisoformat(body.end_date)
    except ValueError as e:
        raise HTTPException(status_code=422, detail=f"Invalid date format: {e}")

    # OOS split: if train_end_date is set, adjust backtest start to next day
    if body.train_end_date:
        try:
            train_end = date.fromisoformat(body.train_end_date)
            oos_start = date.fromordinal(train_end.toordinal() + 1)
            if oos_start > start:
                start = oos_start
                logger.info("OOS split: adjusted start_date to %s (train_end_date=%s)", start, train_end)
        except ValueError as e:
            raise HTTPException(status_code=422, detail=f"Invalid train_end_date format: {e}")

    # Scoring run 校验 + 日期范围截断
    scoring_date_warning: str = ""
    if body.scoring_run_id:
        try:
            scoring_meta = catalog.query(
                "SELECT start_date, end_date, status FROM meta_scoring_runs WHERE run_id = ?",
                [body.scoring_run_id],
            )
            if scoring_meta.is_empty():
                raise HTTPException(
                    status_code=404,
                    detail=f"打分任务 '{body.scoring_run_id}' 不存在",
                )
            scoring_status = scoring_meta["status"][0]
            if scoring_status != "completed":
                raise HTTPException(
                    status_code=422,
                    detail=f"打分任务 '{body.scoring_run_id}' 尚未完成（当前状态: {scoring_status}），"
                           f"请等待打分完成后再提交回测",
                )
            s_start = str(scoring_meta["start_date"].item()).split()[0]
            s_end = str(scoring_meta["end_date"].item()).split()[0]
            bt_start = body.start_date
            bt_end = body.end_date
            effective_start = max(bt_start, s_start)
            effective_end = min(bt_end, s_end)
            if effective_start > effective_end:
                raise HTTPException(
                    status_code=400,
                    detail=f"回测日期范围 ({bt_start}~{bt_end}) 与打分结果范围 ({s_start}~{s_end}) 无交集",
                )
            if effective_start != bt_start or effective_end != bt_end:
                scoring_date_warning = (
                    f"回测范围已截断为 {effective_start}~{effective_end}（受打分数据范围限制）"
                )
                body = body.model_copy(update={
                    "start_date": effective_start,
                    "end_date": effective_end,
                })
                # Re-parse start/end so BacktestRunSpec receives truncated dates
                start = date.fromisoformat(effective_start)
                end = date.fromisoformat(effective_end)
        except HTTPException:
            raise
        except Exception as e:
            logger.warning("Failed to check scoring date range: %s", e)

    # Auto-detect feature_set_version if not provided
    feature_set_version = body.feature_set_version
    if not feature_set_version:
        fsv_df = catalog.query(
            "SELECT feature_set_version FROM gold_factor_values "
            "GROUP BY feature_set_version ORDER BY MAX(trade_date) DESC LIMIT 1"
        )
        if not fsv_df.is_empty():
            feature_set_version = fsv_df["feature_set_version"].item()

    spec = BacktestRunSpec(
        dataset_version=body.dataset_version,
        strategy_id=body.strategy_id,
        start_date=start,
        end_date=end,
        feature_set_version=feature_set_version,
        top_n=top_n,
        sort_factor=sort_factor,
        tags=parsed.get("risk_limits", {}),
        strategy_type=strategy_type,
        model_version=model_version,
        label_name=label_name,
        eval_mode=body.eval_mode,
        walk_forward=body.walk_forward,
        short_n=short_n,
        sector_map=sector_map,
        top_sectors=top_sectors,
        top_n_per_sector=top_n_per_sector,
        sub_strategy_configs=sub_strategy_configs,
        combo_method=combo_method,
        custom_weights=custom_weights,
        entry_conditions=entry_conditions,
        exit_conditions=exit_conditions,
        indicator_specs=indicator_specs,
        universe_id=universe_id,
        benchmark_asset_id=body.benchmark_asset_id,
        scoring_run_id=body.scoring_run_id,
        missing_factor_strategy=missing_factor_strategy,
        penalty_per_missing=penalty_per_missing,
        breakout_config=body.breakout_config or parsed.get("breakout_config", {}),
        factor_weights=factor_weights,
        dsl_spec=dsl_spec or {},
    )

    _ensure_schema_extensions(catalog)
    _ensure_job_table(catalog)
    job_id = str(uuid.uuid4())
    _save_job(catalog, job_id, job_type="backtest", status="running")

    def _run_job() -> None:
        try:
            run_id = _run_backtest(catalog, spec)
            _save_job(catalog, job_id, "backtest", "completed", run_id=run_id)
            # Auto-trigger overfitting analysis after successful backtest
            try:
                from cquant.bt_analyzer.run import (
                    AnalysisRunner, AnalysisRunSpec, load_result,
                )
                runner = AnalysisRunner(catalog)
                runner.run(
                    load_result(run_id, catalog),
                    AnalysisRunSpec(backtest_run_id=run_id),
                )
                logger.info("Auto-analysis completed for run %s", run_id)
            except Exception as analysis_exc:
                # Surface the failure in the job record (status + error) instead
                # of a silent warning log — the backtest itself completed.
                logger.exception("Auto-analysis failed for run %s", run_id)
                _save_job(
                    catalog, job_id, "backtest", "failed", run_id=run_id,
                    error=(
                        f"Backtest completed (run_id={run_id}) but auto-analysis "
                        f"failed: {str(analysis_exc)[:200]}"
                    ),
                )
        except Exception as exc:
            logger.exception("Backtest job %s failed", job_id)
            _save_job(catalog, job_id, "backtest", "failed", error=f"Backtest failed: {str(exc)[:200]}")

    background_tasks.add_task(run_job_async, _run_job)
    return {"job_id": job_id, "strategy_id": body.strategy_id, "status": "running", "warning": scoring_date_warning}


@router.get("/jobs/{job_id}")
async def get_job_status(job_id: str, catalog: CatalogDep) -> dict:
    """查询回测任务运行状态。

    Returns {job_id, status: running|completed|failed, run_id: str|None, error: str|None}
    """
    _ensure_job_table(catalog)
    job = _load_job(catalog, job_id)
    if not job:
        raise HTTPException(status_code=404, detail=f"Job '{job_id}' not found")
    return {"job_id": job_id, **job}


@router.get("")
async def list_backtests(
    catalog: CatalogDep,
    offset: int = Query(default=0, ge=0),
    limit: int = Query(default=50, ge=1, le=200),
    status: str = "",
    engine: str = "",
    strategy_id: str = "",
    start_date: str = "",
    end_date: str = "",
    sort_by: str = "started_at",
    sort_order: str = "desc",
) -> dict:
    """List backtest runs, including in-flight jobs from _api_jobs."""
    _ensure_job_table(catalog)

    # Build dynamic WHERE clauses for non-empty filters
    conditions = []
    params: list = []
    if status:
        conditions.append("status = ?")
        params.append(status)
    if engine:
        conditions.append("engine = ?")
        params.append(engine)
    if strategy_id:
        conditions.append("strategy_id = ?")
        params.append(strategy_id)
    if start_date:
        conditions.append("started_at >= ?")
        params.append(start_date)
    if end_date:
        # Ensure end_date covers the full day (handle both YYYY-MM-DD and full datetime)
        end_ts = end_date if "T" in end_date else end_date + "T23:59:59"
        conditions.append("started_at <= ?")
        params.append(end_ts)

    where = f"WHERE {' AND '.join(conditions)}" if conditions else ""

    # Validate sort column to prevent SQL injection
    # Metric columns are resolved from on-disk JSON files, not from SQL
    _METRIC_SORTS = {"sharpe_ratio", "total_return", "max_drawdown"}
    allowed_sorts = {"started_at", "strategy_id", "status", "engine", "completed_at"} | _METRIC_SORTS
    sort_col = sort_by if sort_by in allowed_sorts else "started_at"
    sort_dir = "ASC" if sort_order.lower() == "asc" else "DESC"

    # 1. Normal backtest list from gold_backtest_runs
    total_df = catalog.query(
        f"SELECT COUNT(*) as cnt FROM gold_backtest_runs {where}", params
    )
    total = total_df["cnt"].item() if not total_df.is_empty() else 0

    if sort_col in _METRIC_SORTS:
        # Metric-based sorting: load all matching runs, resolve metrics from disk, sort in Python
        df = catalog.query(
            f"SELECT run_id, engine, strategy_id, dataset_version, started_at, "
            f"completed_at, status FROM gold_backtest_runs {where}",
            params,
        )
        all_runs = df.to_dicts()

        # Enrich with metric values from on-disk JSON files
        _metric_vals: dict[str, float | None] = {}
        for row in all_runs:
            rid = row["run_id"]
            mpath = _safe_metrics_path(rid)
            if mpath and mpath.exists():
                try:
                    m = json.loads(mpath.read_text())
                    raw = m.get(sort_col)
                    _metric_vals[rid] = float(raw) if raw is not None else None
                except (json.JSONDecodeError, ValueError, TypeError):
                    _metric_vals[rid] = None
            else:
                _metric_vals[rid] = None

        reverse = sort_dir == "DESC"
        # Sort non-null by metric value; nulls always last regardless of direction
        non_null = [r for r in all_runs if _metric_vals.get(r["run_id"]) is not None]
        nulls = [r for r in all_runs if _metric_vals.get(r["run_id"]) is None]
        non_null.sort(key=lambda r: _metric_vals[r["run_id"]], reverse=reverse)
        all_runs = non_null + nulls

        items = all_runs[offset: offset + limit]
    else:
        df = catalog.query(
            f"SELECT run_id, engine, strategy_id, dataset_version, started_at, "
            f"completed_at, status FROM gold_backtest_runs {where} "
            f"ORDER BY {sort_col} {sort_dir} LIMIT ? OFFSET ?",
            params + [limit, offset],
        )
        items = df.to_dicts()

    # 2. Running/pending jobs from _api_jobs (always count for total; merge items only on first page)
    running_df = catalog.query(
        "SELECT job_id, status, created_at, error, run_id "
        "FROM _api_jobs "
        "WHERE job_type = 'backtest' AND status IN ('running', 'pending')"
    )
    running_rows = running_df.to_dicts() if not running_df.is_empty() else []
    if offset == 0 and running_rows:
        existing_run_ids = {item.get("run_id") for item in items}
        for row in running_rows:
            if row.get("run_id") and row["run_id"] in existing_run_ids:
                continue
            items.insert(0, {
                "run_id": row.get("run_id") or row["job_id"],
                "engine": "",
                "strategy_id": "",
                "dataset_version": "",
                "started_at": row.get("created_at", ""),
                "completed_at": None,
                "status": row["status"],
                "error": row.get("error"),
                "is_running_job": True,  # W-3: flag for frontend to disable detail navigation
            })
    total += len(running_rows)

    return {"items": items, "total": total}


@router.get("/compare")
async def compare_backtests(run_ids: str, catalog: CatalogDep) -> dict:
    """批量获取多个回测的关键指标和净值曲线，用于横向对比。

    Args:
        run_ids: 逗号分隔的 run_id，最多 6 个
    """
    ids = [r.strip() for r in run_ids.split(",") if r.strip()]
    if not ids:
        raise HTTPException(status_code=400, detail="run_ids 不能为空")
    if len(ids) > 6:
        raise HTTPException(status_code=400, detail="最多同时对比 6 个回测")

    # 批量查询所有 run 的元数据 — 1 次 DB 查询
    in_ph = ",".join(["?" for _ in ids])
    runs_df = catalog.query(
        f"SELECT run_id, strategy_id, engine, status, started_at, dataset_version "
        f"FROM gold_backtest_runs WHERE run_id IN ({in_ph})",
        ids,
    )
    runs_by_id = {r["run_id"]: r for r in runs_df.to_dicts()}

    # 批量查询所有 run 的净值快照 — 1 次 DB 查询
    snaps_df = catalog.query(
        f"SELECT run_id, trade_date, nav FROM gold_portfolio_snapshots "
        f"WHERE run_id IN ({in_ph}) ORDER BY run_id, trade_date",
        ids,
    )
    snaps_by_run: dict[str, list[dict]] = {}
    for row in snaps_df.to_dicts():
        snaps_by_run.setdefault(row["run_id"], []).append(
            {"date": str(row["trade_date"]), "nav": float(row["nav"])}
        )

    # 从磁盘加载指标（无法批量，但是纯 I/O）
    results = []
    for run_id in ids:
        run_info = runs_by_id.get(run_id)
        if not run_info:
            continue

        metrics: dict = {}
        metrics_path = _safe_metrics_path(run_id)
        if metrics_path and metrics_path.exists():
            try:
                metrics = json.loads(metrics_path.read_text())
            except Exception:
                pass

        results.append({
            "run_id": run_id,
            "strategy_id": run_info.get("strategy_id", ""),
            "engine": run_info.get("engine", ""),
            "status": run_info.get("status", ""),
            "started_at": str(run_info.get("started_at", "")),
            "dataset_version": run_info.get("dataset_version", ""),
            "metrics": metrics,
            "nav_series": snaps_by_run.get(run_id, []),
        })

    return {"runs": results}


class StatisticalTestBody(BaseModel):
    """Request body for statistical significance test between backtests."""
    backtest_ids: list[str]
    test_type: str = "psr_diff"  # "psr_diff" | "bootstrap" | "mcs"
    confidence: float = Field(default=0.95, ge=0.5, le=0.999)
    block_size: int | None = Field(default=None, ge=1, description="Block size for block bootstrap (auto if omitted)")


@router.post("/compare/statistical-test")
async def statistical_test(
    body: StatisticalTestBody,
    catalog: CatalogDep,
) -> dict:
    """Run statistical significance test between backtests.

    Compares return distributions of multiple backtests using one of:
    - psr_diff: Jobson & Korkie (1981) Sharpe ratio difference test
    - bootstrap: Bootstrap confidence interval for Sharpe difference
    - mcs: Model Confidence Set to identify statistically best strategies

    Args:
        body: backtest_ids (2-6 run IDs), test_type, confidence level.
    """
    import numpy as np

    if len(body.backtest_ids) < 2:
        raise HTTPException(status_code=400, detail="At least 2 backtest IDs required")
    if len(body.backtest_ids) > 6:
        raise HTTPException(status_code=400, detail="Maximum 6 backtests for comparison")
    if body.test_type not in ("psr_diff", "bootstrap", "mcs"):
        raise HTTPException(
            status_code=422,
            detail=f"Invalid test_type '{body.test_type}'. Use: psr_diff, bootstrap, mcs",
        )

    # Validate UUIDs
    for bid in body.backtest_ids:
        if not _UUID_RE.match(bid):
            raise HTTPException(status_code=400, detail=f"Invalid backtest ID format: '{bid}'")

    # Load returns for each backtest
    in_ph = ",".join(["?" for _ in body.backtest_ids])
    returns_df = catalog.query(
        f"SELECT run_id, portfolio_return FROM gold_portfolio_snapshots "
        f"WHERE run_id IN ({in_ph}) ORDER BY run_id, trade_date",
        body.backtest_ids,
    )
    if returns_df.is_empty():
        raise HTTPException(status_code=404, detail="No return data found for provided backtest IDs")

    returns_by_run: dict[str, np.ndarray] = {}
    for row in returns_df.to_dicts():
        returns_by_run.setdefault(row["run_id"], []).append(float(row["portfolio_return"]))

    # Convert to numpy arrays, preserving order from request
    returns_arrays = []
    valid_ids = []
    for bid in body.backtest_ids:
        arr = returns_by_run.get(bid)
        if arr and len(arr) >= 2:
            returns_arrays.append(np.array(arr))
            valid_ids.append(bid)

    if len(returns_arrays) < 2:
        raise HTTPException(status_code=422, detail="Need at least 2 backtests with sufficient return data")

    from cquant.bt_analyzer.statistical_tests import (
        bootstrap_test,
        mcs_test,
        psr_difference,
    )

    if body.test_type == "psr_diff":
        if len(valid_ids) != 2:
            raise HTTPException(
                status_code=422,
                detail="psr_diff test requires exactly 2 backtests",
            )
        result = psr_difference(returns_arrays[0], returns_arrays[1])
        return {
            "test_type": "psr_diff",
            "backtest_ids": valid_ids,
            "result": result,
        }

    elif body.test_type == "bootstrap":
        if len(valid_ids) != 2:
            raise HTTPException(
                status_code=422,
                detail="bootstrap test requires exactly 2 backtests",
            )
        result = bootstrap_test(returns_arrays[0], returns_arrays[1], block_size=body.block_size)
        return {
            "test_type": "bootstrap",
            "backtest_ids": valid_ids,
            "result": result,
        }

    else:  # mcs
        result = mcs_test(returns_arrays, confidence=body.confidence)
        # Map indices back to run IDs
        for r in result["results"]:
            r["run_id"] = valid_ids[r["index"]]
        return {
            "test_type": "mcs",
            "backtest_ids": valid_ids,
            "result": result,
        }


@router.get("/best-recent")
async def best_recent_backtest(catalog: CatalogDep, days: int = 7) -> dict:
    """返回最近 N 天 Sharpe 最高的已完成回测摘要（用于 Dashboard）。"""
    days = max(1, min(365, int(days)))

    cutoff = f"CURRENT_DATE - INTERVAL '{days} days'"
    df = catalog.query(
        f"SELECT run_id, strategy_id, started_at FROM gold_backtest_runs "
        f"WHERE status = 'completed' AND started_at >= {cutoff} "
        f"ORDER BY started_at DESC LIMIT 20"
    )
    if df.is_empty():
        return {"run_id": None, "strategy_id": None, "sharpe": None, "max_drawdown": None, "cagr": None}

    best = None
    best_sharpe = float('-inf')
    for row in df.to_dicts():
        mpath = _safe_metrics_path(row["run_id"])
        if not mpath or not mpath.exists():
            continue
        try:
            m = json.loads(mpath.read_text())
        except Exception:
            continue
        s = m.get("sharpe_ratio")
        if s is not None and float(s) > best_sharpe:
            best_sharpe = float(s)
            best = {
                "run_id": row["run_id"],
                "strategy_id": row["strategy_id"],
                "sharpe": round(float(s), 3),
                "max_drawdown": round(float(m.get("max_drawdown") or 0) * 100, 2),
                "cagr": round(float(m.get("cagr") or 0) * 100, 2),
            }

    return best or {"run_id": None, "strategy_id": None, "sharpe": None, "max_drawdown": None, "cagr": None}


@router.get("/{run_id}")
async def get_backtest(run_id: str, catalog: CatalogDep) -> dict:
    """Get a specific backtest run with metrics."""
    df = catalog.query(
        "SELECT * FROM gold_backtest_runs WHERE run_id = ?", [run_id]
    )
    if df.is_empty():
        raise HTTPException(status_code=404, detail=f"Backtest run '{run_id}' not found")

    result = df.to_dicts()[0]

    # Load metrics from artifacts file (offload to thread to avoid blocking event loop)
    metrics_path = _safe_metrics_path(run_id)
    if metrics_path and metrics_path.exists():
        try:
            result["metrics"] = await asyncio.to_thread(_load_metrics, metrics_path)
        except (json.JSONDecodeError, OSError) as e:
            logger.warning("Failed to load metrics for run %s: %s", run_id, e)
            result["metrics"] = {}
    else:
        result["metrics"] = {}

    return result


class AnalysisTriggerBody(BaseModel):
    """Request body for triggering overfitting analysis."""
    embargo_days: int = Field(default=0, ge=0, le=365)


@router.post("/{run_id}/analyze")
async def trigger_analysis(
    run_id: str,
    background_tasks: BackgroundTasks,
    catalog: CatalogDep,
    body: AnalysisTriggerBody | None = None,
) -> dict:
    """触发指定回测的过拟合分析（后台异步执行）。

    分析结果写入 gold_bt_analysis_runs。
    通过 GET /backtests/{run_id}/analysis 查看结果。
    """
    # 验证回测存在且已完成
    df = catalog.query(
        "SELECT run_id, status FROM gold_backtest_runs WHERE run_id = ?", [run_id]
    )
    if df.is_empty():
        raise HTTPException(status_code=404, detail=f"Backtest run '{run_id}' not found")
    if df["status"][0] != "completed":
        raise HTTPException(status_code=422, detail="Only completed backtests can be analyzed")

    embargo_days = body.embargo_days if body else 0

    _ensure_job_table(catalog)
    job_id = str(uuid.uuid4())
    _save_job(catalog, job_id, job_type="analysis", status="running")

    def _run_analysis() -> None:
        try:
            from cquant.bt_analyzer.run import (
                AnalysisRunner, AnalysisRunSpec, load_result,
            )
            # Reconstruct the BacktestResult from persisted artifacts and pass
            # it separately from the AnalysisRunSpec — the runner signature is
            # run(result: BacktestResult, spec: AnalysisRunSpec).
            result = load_result(run_id, catalog)
            runner = AnalysisRunner(catalog)
            report = runner.run(result, AnalysisRunSpec(
                backtest_run_id=run_id,
                embargo_days=embargo_days,
            ))
            _save_job(catalog, job_id, "analysis", "completed", run_id=report.analysis_run_id)
        except Exception as exc:
            logger.exception("Analysis job %s failed", job_id)
            _save_job(catalog, job_id, "analysis", "failed", run_id=run_id, error=f"Analysis failed: {str(exc)[:200]}")

    background_tasks.add_task(run_job_async, _run_analysis)
    return {"job_id": job_id, "run_id": run_id, "status": "running"}


@router.get("/{run_id}/analysis")
async def get_backtest_analysis(run_id: str, catalog: CatalogDep) -> dict:
    """Get post-backtest analysis report for a run."""
    df = catalog.query(
        "SELECT * FROM gold_bt_analysis_runs WHERE backtest_run_id = ? "
        "ORDER BY created_at DESC LIMIT 1",
        [run_id],
    )
    if df.is_empty():
        raise HTTPException(status_code=404, detail=f"No analysis found for run '{run_id}'")
    return df.to_dicts()[0]


@router.get("/{run_id}/risk")
async def get_backtest_risk(run_id: str, catalog: CatalogDep, limit: int = 20) -> dict:
    """Get risk snapshots for a backtest run."""
    df = catalog.query(
        "SELECT * FROM gold_risk_snapshots WHERE run_id = ? "
        "ORDER BY snapshot_ts DESC LIMIT ?",
        [run_id, limit],
    )
    return {"items": df.to_dicts(), "total": df.height}


@router.get("/{run_id}/risk-rolling")
async def get_risk_rolling(
    run_id: str,
    catalog: CatalogDep,
    window: int = Query(default=60, ge=1, le=504),
) -> dict:
    """Get rolling risk metrics for a backtest run."""
    df = catalog.query(
        'SELECT trade_date, "window", '
        "rolling_var AS var_95, rolling_cvar AS cvar_95, "
        "rolling_vol AS volatility, rolling_sharpe AS sharpe_ratio, "
        'rolling_beta AS beta '
        'FROM gold_risk_rolling WHERE run_id = ? AND "window" = ? ORDER BY trade_date',
        [run_id, window],
    )
    if df.is_empty():
        raise HTTPException(status_code=404, detail=f"No rolling risk data for run '{run_id}'")
    return {
        "run_id": run_id,
        "window": window,
        "data": df.to_dicts(),
    }


@router.get("/{run_id}/drawdowns")
async def get_drawdowns(
    run_id: str,
    catalog: CatalogDep,
) -> dict:
    """Get drawdown periods for a backtest run."""
    df = catalog.query(
        "SELECT * FROM gold_drawdown_periods WHERE run_id = ? ORDER BY period_id",
        [run_id],
    )
    if df.is_empty():
        return {"run_id": run_id, "periods": []}
    return {
        "run_id": run_id,
        "periods": df.to_dicts(),
    }


@router.get("/{run_id}/drawdown-timeseries")
async def get_drawdown_timeseries(
    run_id: str,
    catalog: CatalogDep,
) -> dict:
    """Get daily underwater drawdown values for charting."""
    df = catalog.query(
        "SELECT trade_date, nav FROM gold_portfolio_snapshots "
        "WHERE run_id = ? ORDER BY trade_date",
        [run_id],
    )
    if df.is_empty():
        raise HTTPException(status_code=404, detail=f"No return data for run '{run_id}'")

    navs = df["nav"].to_list()
    dates = df["trade_date"].to_list()
    # Compute daily returns from NAV series
    returns = [0.0] + [(navs[i] - navs[i - 1]) / navs[i - 1] if navs[i - 1] > 0 else 0.0 for i in range(1, len(navs))]
    nav = 1.0
    peak = 1.0
    data = []
    for i, r in enumerate(returns):
        nav *= (1 + r)
        peak = max(peak, nav)
        dd = (nav - peak) / peak if peak > 0 else 0.0
        data.append({"trade_date": str(dates[i]), "drawdown": dd})

    return {"run_id": run_id, "data": data}


@router.get("/{run_id}/return-distribution")
async def get_return_distribution(
    run_id: str,
    catalog: CatalogDep,
    bins: int = Query(default=50, ge=2, le=1000),
) -> dict:
    """Get return distribution histogram data for a backtest run."""
    import numpy as np

    df = catalog.query(
        "SELECT portfolio_return FROM gold_portfolio_snapshots WHERE run_id = ? ORDER BY trade_date",
        [run_id],
    )
    if df.is_empty():
        raise HTTPException(status_code=404, detail=f"No return data for run '{run_id}'")

    returns = df["portfolio_return"].to_numpy()
    counts, bin_edges = np.histogram(returns, bins=bins)

    data = []
    for i in range(len(counts)):
        data.append({
            "bin_start": float(bin_edges[i]),
            "bin_end": float(bin_edges[i + 1]),
            "count": int(counts[i]),
            "bin_label": f"{bin_edges[i]:.3f}",
        })

    return {
        "run_id": run_id,
        "bins": bins,
        "data": data,
        "stats": {
            "mean": float(np.mean(returns)),
            "std": float(np.std(returns)),
            "skewness": float(np.mean(((returns - np.mean(returns)) / np.std(returns)) ** 3)) if np.std(returns) > 0 else 0.0,
            "kurtosis": float(np.mean(((returns - np.mean(returns)) / np.std(returns)) ** 4)) if np.std(returns) > 0 else 0.0,
            "min": float(np.min(returns)),
            "max": float(np.max(returns)),
        },
    }


def _fetch_position_prices(catalog, run_id: str) -> tuple[list[str], "pd.DataFrame"]:
    """Fetch asset IDs and price data for a backtest run.

    Returns (asset_ids, pandas DataFrame with trade_date, asset_id, close).
    Raises HTTPException on missing data.
    """
    import pandas as pd

    pos_df = catalog.query(
        "SELECT DISTINCT asset_id FROM gold_positions WHERE run_id = ?",
        [run_id],
    )
    if pos_df.is_empty():
        raise HTTPException(status_code=404, detail=f"No positions found for run '{run_id}'")
    asset_ids = pos_df["asset_id"].to_list()

    snap_df = catalog.query(
        "SELECT MIN(trade_date) as start_date, MAX(trade_date) as end_date "
        "FROM gold_portfolio_snapshots WHERE run_id = ?",
        [run_id],
    )
    if snap_df.is_empty():
        raise HTTPException(status_code=404, detail=f"No snapshots for run '{run_id}'")
    start_date = str(snap_df["start_date"].item()).split()[0]
    end_date = str(snap_df["end_date"].item()).split()[0]

    asset_ph = ",".join(["?" for _ in asset_ids])
    price_df = catalog.query(
        f"SELECT trade_date, asset_id, close FROM silver_prices_1d "
        f"WHERE asset_id IN ({asset_ph}) AND trade_date >= ? AND trade_date <= ? "
        f"ORDER BY trade_date",
        asset_ids + [start_date, end_date],
    )
    if price_df.is_empty():
        raise HTTPException(status_code=404, detail="No price data available")

    return asset_ids, price_df.to_pandas()


@router.get("/{run_id}/correlation")
async def get_correlation_matrix(
    run_id: str,
    catalog: CatalogDep,
    window: int = Query(default=60, ge=2, le=504),
) -> dict:
    """Get asset correlation matrix for a backtest run."""
    from cquant.backtest_vector.risk_analysis import compute_correlation_matrix

    _, pdf = _fetch_position_prices(catalog, run_id)
    return compute_correlation_matrix(pdf, window=window)


@router.get("/{run_id}/factor-exposure")
async def get_factor_exposure(
    run_id: str,
    catalog: CatalogDep,
    window: int = Query(default=20, ge=5, le=120),
) -> dict:
    """Get factor exposure time series for a backtest run."""
    from cquant.backtest_vector.risk_analysis import compute_factor_exposures

    _, pdf = _fetch_position_prices(catalog, run_id)
    return compute_factor_exposures(pdf, window=window)


@router.get("/{run_id}/stress-test")
async def get_stress_test(
    run_id: str,
    catalog: CatalogDep,
    custom_start: str | None = Query(default=None, description="Custom stress period start (YYYY-MM-DD)"),
    custom_end: str | None = Query(default=None, description="Custom stress period end (YYYY-MM-DD)"),
) -> dict:
    """Run stress test scenarios on a backtest run."""
    from cquant.backtest_vector.risk_analysis import run_stress_test

    # Validate date format
    for label, val in [("custom_start", custom_start), ("custom_end", custom_end)]:
        if val is not None:
            try:
                date.fromisoformat(val)
            except (ValueError, TypeError):
                raise HTTPException(status_code=422, detail=f"Invalid {label} format: '{val}'. Use YYYY-MM-DD.")

    # Get portfolio returns with trade dates
    ret_df = catalog.query(
        "SELECT trade_date, portfolio_return FROM gold_portfolio_snapshots WHERE run_id = ? ORDER BY trade_date",
        [run_id],
    )
    if ret_df.is_empty():
        raise HTTPException(status_code=404, detail=f"No return data for run '{run_id}'")

    returns = ret_df["portfolio_return"].to_numpy()
    trade_dates = ret_df["trade_date"].to_numpy()

    # Get NAV series
    snap_df = catalog.query(
        "SELECT nav FROM gold_portfolio_snapshots WHERE run_id = ? ORDER BY trade_date",
        [run_id],
    )
    nav_series = snap_df["nav"].to_numpy() if not snap_df.is_empty() else None

    result = run_stress_test(
        returns,
        nav_series=nav_series,
        trade_dates=trade_dates,
        custom_start=custom_start,
        custom_end=custom_end,
    )
    return result


@router.get("/{run_id}/risk-contribution")
async def get_risk_contribution(
    run_id: str,
    catalog: CatalogDep,
    window: int = Query(default=60, ge=2, le=504),
) -> dict:
    """Get risk contribution by asset for a backtest run."""
    from cquant.backtest_vector.risk_analysis import compute_risk_contribution

    # Get latest positions with weights
    pos_df = catalog.query(
        "SELECT asset_id, weight FROM gold_positions WHERE run_id = ? "
        "AND trade_date = (SELECT MAX(trade_date) FROM gold_positions WHERE run_id = ?)",
        [run_id, run_id],
    )
    if pos_df.is_empty():
        raise HTTPException(status_code=404, detail=f"No positions found for run '{run_id}'")

    weights = {row["asset_id"]: float(row["weight"]) for row in pos_df.to_dicts()}

    _, pdf = _fetch_position_prices(catalog, run_id)
    return compute_risk_contribution(weights, pdf, window=window)


_EMPTY_IS = {
    "total_is_bps": 0.0,
    "total_is_pct": 0.0,
    "components": {"delay_cost_bps": 0.0, "trading_cost_bps": 0.0, "missed_trade_cost_bps": 0.0},
    "by_asset": [],
    "by_date": [],
    "by_order_size": [],
    "timeseries": [],
}


def _compute_implementation_shortfall(catalog, run_id: str) -> dict:
    """Compute Implementation Shortfall (IS) analysis for a backtest run.

    For each fill:
    - Decision price = close on the previous trading day (signal date)
    - Execution price = fill price
    - IS = (exec_price - decision_price) / decision_price * direction
    - Delay cost = (open_on_exec_day - decision_price) / decision_price * direction
    - Trading cost = (exec_price - open_on_exec_day) / open_on_exec_day * direction
    """
    fills_df = catalog.query(
        "SELECT fill_id, trade_date, asset_id, side, qty, price, notional "
        "FROM gold_fills WHERE run_id = ? ORDER BY trade_date",
        [run_id],
    )
    if fills_df.is_empty():
        return _EMPTY_IS

    asset_ids = fills_df["asset_id"].unique().to_list()

    # Get all trade dates we need: fill dates + previous trading days
    all_fill_dates = fills_df["trade_date"].unique().sort()
    min_date = str(all_fill_dates[0])
    max_date = str(all_fill_dates[-1])

    # Query prices for all needed dates (a buffer before min_date for prev-day lookup)
    asset_ph = ",".join(["?" for _ in asset_ids])
    prices_df = catalog.query(
        f"SELECT trade_date, asset_id, open, close FROM silver_prices_1d "
        f"WHERE asset_id IN ({asset_ph}) AND trade_date >= (?::DATE - INTERVAL 10 DAY) "
        f"AND trade_date <= ?::DATE ORDER BY asset_id, trade_date",
        asset_ids + [min_date, max_date],
    )
    if prices_df.is_empty():
        return _EMPTY_IS

    # Build price lookup: (asset_id, trade_date) -> {open, close}
    price_lookup: dict[tuple[str, str], dict[str, float]] = {}
    for row in prices_df.iter_rows(named=True):
        key = (row["asset_id"], str(row["trade_date"]))
        price_lookup[key] = {
            "open": float(row["open"]) if row["open"] else 0.0,
            "close": float(row["close"]) if row["close"] else 0.0,
        }

    # Build sorted date list per asset for prev-day lookup
    dates_by_asset: dict[str, list[str]] = {}
    for row in prices_df.iter_rows(named=True):
        aid = row["asset_id"]
        d = str(row["trade_date"])
        dates_by_asset.setdefault(aid, [])
        if not dates_by_asset[aid] or dates_by_asset[aid][-1] != d:
            dates_by_asset[aid].append(d)
    # Already sorted from ORDER BY

    def _prev_trade_date(asset_id: str, trade_date: str) -> str | None:
        """Get the previous trading date for an asset before trade_date."""
        dates = dates_by_asset.get(asset_id, [])
        for i, d in enumerate(dates):
            if d == trade_date and i > 0:
                return dates[i - 1]
        return None

    # Compute IS for each fill
    per_fill = []
    for row in fills_df.iter_rows(named=True):
        td = str(row["trade_date"])
        aid = row["asset_id"]
        side = row["side"]
        exec_price = float(row["price"])
        notional = float(row["notional"])
        direction = 1.0 if side == "buy" else -1.0

        prev_td = _prev_trade_date(aid, td)
        if prev_td is None:
            continue

        decision_key = (aid, prev_td)
        exec_key = (aid, td)
        decision_data = price_lookup.get(decision_key)
        exec_data = price_lookup.get(exec_key)

        if not decision_data or not exec_data:
            continue

        decision_price = decision_data["close"]  # close on signal date
        open_price = exec_data["open"]  # open on execution date

        if decision_price <= 0 or open_price <= 0 or exec_price <= 0:
            continue

        # IS = (exec - decision) / decision * direction (positive = worse for buyer)
        is_pct = (exec_price - decision_price) / decision_price * direction
        delay_pct = (open_price - decision_price) / decision_price * direction
        trading_pct = (exec_price - open_price) / open_price * direction

        per_fill.append({
            "asset_id": aid,
            "trade_date": td,
            "side": side,
            "notional": notional,
            "is_bps": is_pct * 10000,
            "delay_bps": delay_pct * 10000,
            "trading_bps": trading_pct * 10000,
            "num_trades": 1,
        })

    if not per_fill:
        return _EMPTY_IS

    import polars as pl

    pf = pl.DataFrame(per_fill)

    # Weighted average IS by notional
    total_notional = float(pf["notional"].sum())
    total_is_bps = float((pf["is_bps"] * pf["notional"]).sum() / total_notional) if total_notional > 0 else 0.0
    total_delay_bps = float((pf["delay_bps"] * pf["notional"]).sum() / total_notional) if total_notional > 0 else 0.0
    total_trading_bps = float((pf["trading_bps"] * pf["notional"]).sum() / total_notional) if total_notional > 0 else 0.0

    # By asset
    by_asset_df = pf.group_by("asset_id").agg([
        pl.col("is_bps").mean().alias("is_bps"),
        pl.col("num_trades").sum().alias("num_trades"),
    ]).sort("is_bps", descending=True)
    by_asset = [
        {"asset_id": r["asset_id"], "is_bps": round(float(r["is_bps"]), 2), "num_trades": int(r["num_trades"])}
        for r in by_asset_df.iter_rows(named=True)
    ]

    # By date
    by_date_df = pf.group_by("trade_date").agg(
        ((pl.col("is_bps") * pl.col("notional")).sum() / pl.col("notional").sum()).alias("weighted_is_bps")
    ).sort("trade_date")
    by_date_rows = [
        {"date": r["trade_date"], "is_bps": round(float(r["weighted_is_bps"]), 2)}
        for r in by_date_df.iter_rows(named=True)
    ]

    # By order size bucket
    pf_with_bucket = pf.with_columns(
        pl.when(pl.col("notional") < 50_000)
        .then(pl.lit("<50K"))
        .when(pl.col("notional") < 200_000)
        .then(pl.lit("50K-200K"))
        .when(pl.col("notional") < 500_000)
        .then(pl.lit("200K-500K"))
        .when(pl.col("notional") < 1_000_000)
        .then(pl.lit("500K-1M"))
        .otherwise(pl.lit(">1M"))
        .alias("bucket")
    )
    by_size_df = pf_with_bucket.group_by("bucket").agg([
        pl.col("is_bps").mean().alias("is_bps"),
        pl.col("num_trades").sum().alias("count"),
    ]).sort("is_bps", descending=True)
    by_order_size = [
        {"bucket": r["bucket"], "is_bps": round(float(r["is_bps"]), 2), "count": int(r["count"])}
        for r in by_size_df.iter_rows(named=True)
    ]

    # Cumulative IS timeseries
    ts_df = pf.group_by("trade_date").agg(
        ((pl.col("is_bps") * pl.col("notional")).sum() / pl.col("notional").sum()).alias("weighted_is_bps")
    ).sort("trade_date")
    cum_is = 0.0
    timeseries = []
    for r in ts_df.iter_rows(named=True):
        daily_is = float(r["weighted_is_bps"])
        cum_is += daily_is
        timeseries.append({"date": r["trade_date"], "cumulative_is_bps": round(cum_is, 2)})

    return {
        "total_is_bps": round(total_is_bps, 2),
        "total_is_pct": round(total_is_bps / 100, 4),
        "components": {
            "delay_cost_bps": round(total_delay_bps, 2),
            "trading_cost_bps": round(total_trading_bps, 2),
            "missed_trade_cost_bps": 0.0,  # requires order-level data with intended vs filled qty
        },
        "by_asset": by_asset,
        "by_date": by_date_rows,
        "by_order_size": by_order_size,
        "timeseries": timeseries,
    }


@router.get("/{run_id}/tca")
async def get_backtest_tca(run_id: str, catalog: CatalogDep) -> dict:
    """Get TCA for a backtest run, including Implementation Shortfall analysis."""
    df = catalog.query(
        "SELECT * FROM gold_bt_tca WHERE analysis_run_id IN "
        "(SELECT analysis_run_id FROM gold_bt_analysis_runs WHERE backtest_run_id = ? "
        "ORDER BY created_at DESC LIMIT 1)",
        [run_id],
    )
    if df.is_empty():
        raise HTTPException(status_code=404, detail=f"No TCA data for run '{run_id}'")
    result = df.to_dicts()[0]
    try:
        result["implementation_shortfall"] = _compute_implementation_shortfall(catalog, run_id)
    except Exception:
        logger.warning("IS computation failed for run %s", run_id, exc_info=True)
    return result



# ── HTML Report SVG helpers ───────────────────────────────────────────────────

def _nav_to_svg(
    nav_dates: list[str],
    nav_values: list[float],
    bm_values: list[float] | None = None,
    width: int = 900,
    height: int = 320,
) -> str:
    """Render NAV + optional benchmark as an inline SVG (no external dependencies)."""
    if not nav_values:
        return '<p style="text-align:center;color:#94a3b8;padding:40px">暂无净值数据</p>'
    PL, PR, PT, PB = 62, 20, 20, 32
    cw, ch = width - PL - PR, height - PT - PB
    all_vals = list(nav_values) + (list(bm_values) if bm_values else [])
    lo, hi = min(all_vals), max(all_vals)
    rng = (hi - lo) or 0.01
    n = len(nav_values)

    def px(i: int, total: int = 0) -> float:
        return PL + i * cw / max((total or n) - 1, 1)

    def py(v: float) -> float:
        return PT + ch - (v - lo) / rng * ch

    def polyline(vals: list[float], total: int = 0) -> str:
        return " ".join(f"{px(i, total):.1f},{py(v):.1f}" for i, v in enumerate(vals))

    parts: list[str] = [
        f'<svg viewBox="0 0 {width} {height}" xmlns="http://www.w3.org/2000/svg" style="width:100%;height:auto">',
        f'<rect x="{PL}" y="{PT}" width="{cw}" height="{ch}" fill="#f8fafc" rx="4"/>',
    ]
    # Y grid + labels
    for t in range(5):
        frac = t / 4
        v = lo + frac * rng
        y = PT + ch * (1 - frac)
        parts += [
            f'<line x1="{PL}" y1="{y:.0f}" x2="{PL + cw}" y2="{y:.0f}" stroke="#e2e8f0" stroke-dasharray="3,3"/>',
            f'<text x="{PL - 6}" y="{y + 4:.0f}" text-anchor="end" font-size="10" fill="#94a3b8">{v:.3f}</text>',
        ]
    # X labels (up to 6)
    x_step = max(1, n // 6)
    for i in range(0, n, x_step):
        lbl = html.escape(nav_dates[i][:7] if len(nav_dates[i]) >= 7 else nav_dates[i])
        parts.append(f'<text x="{px(i):.0f}" y="{height - 4}" text-anchor="middle" font-size="10" fill="#94a3b8">{lbl}</text>')
    # Benchmark
    if bm_values:
        parts.append(f'<polyline points="{polyline(bm_values, len(bm_values))}" fill="none" stroke="#94a3b8" stroke-width="1.5" stroke-dasharray="5,3"/>')
    # Area fill — use chart bottom (py(lo)) so polygon is always inside the viewBox
    # Using py(max(lo, 1.0)) would break when all values < 1.0 (yields negative Y)
    base_y = float(PT + ch)
    pts = polyline(nav_values)
    parts.append(f'<polygon points="{PL:.0f},{base_y:.0f} {pts} {PL + cw:.0f},{base_y:.0f}" fill="rgba(37,99,235,0.07)"/>')
    # Line
    parts.append(f'<polyline points="{pts}" fill="none" stroke="#2563eb" stroke-width="2" stroke-linejoin="round"/>')
    # Axes
    parts += [
        f'<line x1="{PL}" y1="{PT}" x2="{PL}" y2="{PT + ch}" stroke="#e2e8f0"/>',
        f'<line x1="{PL}" y1="{PT + ch}" x2="{PL + cw}" y2="{PT + ch}" stroke="#e2e8f0"/>',
        '</svg>',
    ]
    return ''.join(parts)


def _annual_returns_svg(years: list[str], rets: list[float], width: int = 600, height: int = 220) -> str:
    """Render annual returns as an inline SVG bar chart."""
    if not years or not rets:
        return ''
    PL, PR, PT, PB = 48, 20, 16, 28
    cw, ch = width - PL - PR, height - PT - PB
    max_abs = max(abs(r) for r in rets) or 0.01
    zero_y = PT + ch / 2
    scale = ch / 2 / max_abs
    n = len(years)
    bar_w = max(6, cw / n * 0.55)
    gap = cw / n
    parts: list[str] = [
        f'<svg viewBox="0 0 {width} {height}" xmlns="http://www.w3.org/2000/svg" style="width:100%;height:auto">',
        f'<line x1="{PL}" y1="{zero_y:.0f}" x2="{PL + cw}" y2="{zero_y:.0f}" stroke="#e2e8f0"/>',
    ]
    for i, (year, ret) in enumerate(zip(years, rets)):
        cx = PL + gap * i + gap / 2
        bh = abs(ret) * scale
        by = zero_y - bh if ret >= 0 else zero_y
        col = "#16a34a" if ret >= 0 else "#dc2626"
        parts += [
            f'<rect x="{cx - bar_w / 2:.1f}" y="{by:.1f}" width="{bar_w:.1f}" height="{bh:.1f}" fill="{col}" rx="2"/>',
            f'<text x="{cx:.0f}" y="{PT + ch + PB - 3}" text-anchor="middle" font-size="10" fill="#64748b">{year}</text>',
            f'<text x="{cx:.0f}" y="{(by - 3 if ret >= 0 else by + bh + 11):.0f}" text-anchor="middle" font-size="9" fill="{col}">{ret * 100:.1f}%</text>',
        ]
    parts.append('</svg>')
    return ''.join(parts)


def _drawdown_to_svg(drawdowns: list[dict], width: int = 800, height: int = 200) -> str:
    """Render drawdown underwater chart as inline SVG polyline.

    Args:
        drawdowns: list of dicts with 'trade_date' and 'drawdown' keys (drawdown <= 0).
        width: SVG width in pixels.
        height: SVG height in pixels.
    """
    if not drawdowns:
        return '<p style="text-align:center;color:#94a3b8;padding:40px">暂无回撤数据</p>'
    PL, PR, PT, PB = 52, 20, 16, 28
    cw, ch = width - PL - PR, height - PT - PB
    vals = [float(d.get("drawdown", 0)) for d in drawdowns]
    dates = [str(d.get("trade_date", "")) for d in drawdowns]
    lo = min(vals) if min(vals) < 0 else -0.01
    hi = 0.0
    rng = hi - lo
    n = len(vals)

    def px(i: int) -> float:
        return PL + i * cw / max(n - 1, 1)

    def py(v: float) -> float:
        return PT + ch - (v - lo) / rng * ch

    parts: list[str] = [
        f'<svg viewBox="0 0 {width} {height}" xmlns="http://www.w3.org/2000/svg" style="width:100%;height:auto">',
        f'<rect x="{PL}" y="{PT}" width="{cw}" height="{ch}" fill="#fef2f2" rx="4"/>',
    ]
    # Y grid
    for t in range(5):
        frac = t / 4
        v = lo + frac * rng
        y = PT + ch * (1 - frac)
        parts += [
            f'<line x1="{PL}" y1="{y:.0f}" x2="{PL + cw}" y2="{y:.0f}" stroke="#fee2e2" stroke-dasharray="3,3"/>',
            f'<text x="{PL - 6}" y="{y + 4:.0f}" text-anchor="end" font-size="10" fill="#94a3b8">{v * 100:.1f}%</text>',
        ]
    # X labels
    x_step = max(1, n // 6)
    for i in range(0, n, x_step):
        lbl = html.escape(dates[i][:7] if len(dates[i]) >= 7 else dates[i])
        parts.append(f'<text x="{px(i):.0f}" y="{height - 4}" text-anchor="middle" font-size="10" fill="#94a3b8">{lbl}</text>')
    # Zero line
    parts.append(f'<line x1="{PL}" y1="{py(0):.0f}" x2="{PL + cw}" y2="{py(0):.0f}" stroke="#fca5a5" stroke-width="1"/>')
    # Area fill
    base_y = float(PT + ch)
    pts = " ".join(f"{px(i):.1f},{py(v):.1f}" for i, v in enumerate(vals))
    parts.append(f'<polygon points="{PL:.0f},{base_y:.0f} {pts} {PL + cw:.0f},{base_y:.0f}" fill="rgba(220,38,38,0.12)"/>')
    # Line
    parts.append(f'<polyline points="{pts}" fill="none" stroke="#dc2626" stroke-width="1.5" stroke-linejoin="round"/>')
    # Axes
    parts += [
        f'<line x1="{PL}" y1="{PT}" x2="{PL}" y2="{PT + ch}" stroke="#e2e8f0"/>',
        f'<line x1="{PL}" y1="{PT + ch}" x2="{PL + cw}" y2="{PT + ch}" stroke="#e2e8f0"/>',
        '</svg>',
    ]
    return ''.join(parts)


def _rolling_vol_to_svg(data: list[dict], width: int = 800, height: int = 200) -> str:
    """Render rolling volatility line chart as inline SVG.

    Args:
        data: list of dicts with 'trade_date' and 'volatility' keys.
        width: SVG width in pixels.
        height: SVG height in pixels.
    """
    if not data:
        return '<p style="text-align:center;color:#94a3b8;padding:40px">暂无滚动波动率数据</p>'
    PL, PR, PT, PB = 52, 20, 16, 28
    cw, ch = width - PL - PR, height - PT - PB
    vals = [float(d.get("volatility", 0)) for d in data]
    dates = [str(d.get("trade_date", "")) for d in data]
    lo = min(vals)
    hi = max(vals)
    rng = (hi - lo) or 0.01
    n = len(vals)

    def px(i: int) -> float:
        return PL + i * cw / max(n - 1, 1)

    def py(v: float) -> float:
        return PT + ch - (v - lo) / rng * ch

    parts: list[str] = [
        f'<svg viewBox="0 0 {width} {height}" xmlns="http://www.w3.org/2000/svg" style="width:100%;height:auto">',
        f'<rect x="{PL}" y="{PT}" width="{cw}" height="{ch}" fill="#f8fafc" rx="4"/>',
    ]
    # Y grid
    for t in range(5):
        frac = t / 4
        v = lo + frac * rng
        y = PT + ch * (1 - frac)
        parts += [
            f'<line x1="{PL}" y1="{y:.0f}" x2="{PL + cw}" y2="{y:.0f}" stroke="#e2e8f0" stroke-dasharray="3,3"/>',
            f'<text x="{PL - 6}" y="{y + 4:.0f}" text-anchor="end" font-size="10" fill="#94a3b8">{v * 100:.1f}%</text>',
        ]
    # X labels
    x_step = max(1, n // 6)
    for i in range(0, n, x_step):
        lbl = html.escape(dates[i][:7] if len(dates[i]) >= 7 else dates[i])
        parts.append(f'<text x="{px(i):.0f}" y="{height - 4}" text-anchor="middle" font-size="10" fill="#94a3b8">{lbl}</text>')
    # Area fill
    base_y = float(PT + ch)
    pts = " ".join(f"{px(i):.1f},{py(v):.1f}" for i, v in enumerate(vals))
    parts.append(f'<polygon points="{PL:.0f},{base_y:.0f} {pts} {PL + cw:.0f},{base_y:.0f}" fill="rgba(234,179,8,0.10)"/>')
    # Line
    parts.append(f'<polyline points="{pts}" fill="none" stroke="#eab308" stroke-width="1.5" stroke-linejoin="round"/>')
    # Axes
    parts += [
        f'<line x1="{PL}" y1="{PT}" x2="{PL}" y2="{PT + ch}" stroke="#e2e8f0"/>',
        f'<line x1="{PL}" y1="{PT + ch}" x2="{PL + cw}" y2="{PT + ch}" stroke="#e2e8f0"/>',
        '</svg>',
    ]
    return ''.join(parts)


def _return_dist_to_svg(returns: list[float], bins: int = 30, width: int = 800, height: int = 200) -> str:
    """Render return distribution histogram as inline SVG.

    Args:
        returns: list of daily return values.
        bins: number of histogram bins.
        width: SVG width in pixels.
        height: SVG height in pixels.
    """
    if not returns:
        return '<p style="text-align:center;color:#94a3b8;padding:40px">暂无收益率数据</p>'
    PL, PR, PT, PB = 52, 20, 16, 28
    cw, ch = width - PL - PR, height - PT - PB

    # Manual histogram binning (no numpy dependency)
    lo_r, hi_r = min(returns), max(returns)
    if lo_r == hi_r:
        lo_r -= 0.001
        hi_r += 0.001
    bin_w = (hi_r - lo_r) / bins
    counts = [0] * bins
    for r in returns:
        idx = int((r - lo_r) / bin_w)
        if idx >= bins:
            idx = bins - 1
        counts[idx] += 1

    max_count = max(counts) or 1
    bar_w = max(2, cw / bins - 1)
    lo_hi_range = hi_r - lo_r

    parts: list[str] = [
        f'<svg viewBox="0 0 {width} {height}" xmlns="http://www.w3.org/2000/svg" style="width:100%;height:auto">',
        f'<rect x="{PL}" y="{PT}" width="{cw}" height="{ch}" fill="#f8fafc" rx="4"/>',
    ]
    # Bars
    for i, cnt in enumerate(counts):
        x = PL + i * (cw / bins)
        bh = cnt / max_count * ch
        by = PT + ch - bh
        center = lo_r + (i + 0.5) * bin_w
        color = "#16a34a" if center >= 0 else "#dc2626"
        parts.append(f'<rect x="{x:.1f}" y="{by:.1f}" width="{bar_w:.1f}" height="{bh:.1f}" fill="{color}" opacity="0.7" rx="1"/>')
    # Zero line
    zero_x = PL + (0 - lo_r) / lo_hi_range * cw
    if PL <= zero_x <= PL + cw:
        parts.append(f'<line x1="{zero_x:.0f}" y1="{PT}" x2="{zero_x:.0f}" y2="{PT + ch}" stroke="#475569" stroke-width="1" stroke-dasharray="4,2"/>')
    # X labels
    for i in range(0, bins, max(1, bins // 8)):
        val = lo_r + (i + 0.5) * bin_w
        x = PL + (i + 0.5) * (cw / bins)
        parts.append(f'<text x="{x:.0f}" y="{height - 4}" text-anchor="middle" font-size="9" fill="#94a3b8">{val * 100:.1f}%</text>')
    # Axes
    parts += [
        f'<line x1="{PL}" y1="{PT}" x2="{PL}" y2="{PT + ch}" stroke="#e2e8f0"/>',
        f'<line x1="{PL}" y1="{PT + ch}" x2="{PL + cw}" y2="{PT + ch}" stroke="#e2e8f0"/>',
        '</svg>',
    ]
    return ''.join(parts)


def _tca_pie_svg(tca: dict, width: int = 400, height: int = 300) -> str:
    """Render TCA breakdown as inline SVG pie chart.

    Args:
        tca: dict with keys like 'total_commission', 'total_slippage', 'total_stamp_duty'.
        width: SVG width in pixels.
        height: SVG height in pixels.
    """
    cx, cy, r = width / 2, (height - 40) / 2, min(width, height - 40) / 2 - 10
    slices = [
        ("佣金", float(tca.get("total_commission", 0)), "#3b82f6"),
        ("滑点", float(tca.get("total_slippage", 0)), "#f59e0b"),
        ("印花税", float(tca.get("total_stamp_duty", 0)), "#8b5cf6"),
    ]
    total = sum(s[1] for s in slices)
    if total <= 0:
        return '<p style="text-align:center;color:#94a3b8;padding:40px">暂无交易成本数据</p>'

    parts: list[str] = [
        f'<svg viewBox="0 0 {width} {height}" xmlns="http://www.w3.org/2000/svg" style="width:100%;height:auto">',
    ]

    # Draw pie slices
    angle = -math.pi / 2  # start from top
    for label, val, color in slices:
        if val <= 0:
            continue
        frac = val / total
        end_angle = angle + frac * 2 * math.pi
        if frac >= 0.999:
            # Full circle — draw a circle element instead of a degenerate arc
            parts.append(
                f'<circle cx="{cx:.1f}" cy="{cy:.1f}" r="{r:.1f}" '
                f'fill="{color}" stroke="#fff" stroke-width="2"/>'
            )
        else:
            large_arc = 1 if frac > 0.5 else 0
            x1 = cx + r * math.cos(angle)
            y1 = cy + r * math.sin(angle)
            x2 = cx + r * math.cos(end_angle)
            y2 = cy + r * math.sin(end_angle)
            parts.append(
                f'<path d="M {cx:.1f},{cy:.1f} L {x1:.1f},{y1:.1f} '
                f'A {r:.1f},{r:.1f} 0 {large_arc},1 {x2:.1f},{y2:.1f} Z" '
                f'fill="{color}" stroke="#fff" stroke-width="2"/>'
            )
        # Label
        mid_angle = angle + frac * math.pi
        lx = cx + (r * 0.65) * math.cos(mid_angle)
        ly = cy + (r * 0.65) * math.sin(mid_angle)
        if frac > 0.05:
            parts.append(
                f'<text x="{lx:.0f}" y="{ly:.0f}" text-anchor="middle" dominant-baseline="middle" '
                f'font-size="11" fill="#fff" font-weight="600">{frac * 100:.0f}%</text>'
            )
        angle = end_angle

    # Legend
    legend_y = height - 28
    legend_x = 20
    for label, val, color in slices:
        if val > 0:
            parts.append(f'<rect x="{legend_x}" y="{legend_y}" width="10" height="10" fill="{color}" rx="2"/>')
            parts.append(f'<text x="{legend_x + 14}" y="{legend_y + 9}" font-size="11" fill="#475569">{label}: {val:.2f}</text>')
            legend_x += len(label) * 14 + 60

    parts.append('</svg>')
    return ''.join(parts)


@router.get("/{run_id}/export")
async def export_backtest_report(
    run_id: str,
    catalog: CatalogDep,
    format: str = "html",
) -> Response:
    """生成回测独立报告（内嵌 SVG 图表，无外部依赖）。
    支持 format=html（默认）和 format=pdf。
    """
    from datetime import datetime as dt
    from jinja2 import Environment, FileSystemLoader, select_autoescape

    if format not in ("html", "pdf"):
        raise HTTPException(status_code=400, detail=f"Unsupported format '{format}'. Supported: 'html', 'pdf'.")

    # 1. 加载运行元数据（合并为单次查询）
    try:
        run_df = catalog.query(
            "SELECT run_id, strategy_id, engine, status, dataset_version, "
            "benchmark_asset_id, started_at, completed_at "
            "FROM gold_backtest_runs WHERE run_id = ?",
            [run_id],
        )
    except Exception as exc:
        # Legacy schema without the ALTER-added benchmark_asset_id column
        logger.warning("export: benchmark_asset_id missing for %s: %s", run_id, exc)
        run_df = catalog.query(
            "SELECT run_id, strategy_id, engine, status, dataset_version, "
            "started_at, completed_at "
            "FROM gold_backtest_runs WHERE run_id = ?",
            [run_id],
        )
    if run_df.is_empty():
        raise HTTPException(status_code=404, detail=f"Backtest run '{run_id}' not found")
    run_info = run_df.to_dicts()[0]

    # 2. 加载指标
    metrics: dict = {}
    mpath = _safe_metrics_path(run_id)
    if mpath and mpath.exists():
        try:
            metrics = json.loads(mpath.read_text())
        except Exception:
            pass

    key_metrics = [
        _fmt_metric(k, metrics.get(k))
        for k in [
            "total_return", "annualized_return", "sharpe_ratio", "max_drawdown",
            "sortino_ratio", "calmar_ratio", "win_rate", "annualized_volatility",
            "information_ratio", "tracking_error", "alpha", "beta",
        ]
    ]

    # 3. 加载净值曲线
    snaps_df = catalog.query(
        "SELECT trade_date, nav FROM gold_portfolio_snapshots "
        "WHERE run_id = ? ORDER BY trade_date",
        [run_id],
    )
    snaps = snaps_df.to_dicts() if not snaps_df.is_empty() else []
    nav_dates = [str(r["trade_date"]) for r in snaps]
    nav_values = [float(r["nav"]) for r in snaps]

    # 4. 加载基准曲线（如果有）
    bm_values: list[float] = []
    bm_asset = run_info.get("benchmark_asset_id") or ""
    if bm_asset and nav_dates:
        try:
            bm_df = catalog.query(
                "SELECT trade_date, close FROM silver_prices_1d "
                "WHERE asset_id = ? AND trade_date >= ? AND trade_date <= ? ORDER BY trade_date",
                [bm_asset, nav_dates[0], nav_dates[-1]],
            )
            if not bm_df.is_empty():
                first = float(bm_df["close"][0])
                bm_values = [float(r["close"]) / first for r in bm_df.to_dicts()]
        except Exception as exc:
            logger.warning("export: benchmark curve unavailable for %s: %s", run_id, exc)

    # 5. 年度收益
    annual_returns: list[tuple[str, float]] = []
    if nav_dates and nav_values:
        import polars as pl
        df_nav = pl.DataFrame({"date": nav_dates, "nav": nav_values}).with_columns(
            pl.col("date").str.slice(0, 4).alias("year")
        )
        for year in sorted(df_nav["year"].unique().to_list()):
            yr_df = df_nav.filter(pl.col("year") == year).sort("date")
            if len(yr_df) >= 2:
                annual_returns.append((year, float(yr_df["nav"][-1]) / float(yr_df["nav"][0]) - 1))

    # 6. 最近 20 笔交易
    fills_df = catalog.query(
        "SELECT trade_date, asset_id, side, qty, price FROM gold_fills "
        "WHERE run_id = ? ORDER BY trade_date DESC LIMIT 20",
        [run_id],
    )
    fills = fills_df.to_dicts() if not fills_df.is_empty() else []

    # 7. 加载回撤数据
    drawdown_periods: list[dict] = []
    try:
        dd_df = catalog.query(
            "SELECT * FROM gold_drawdown_periods WHERE run_id = ? ORDER BY period_id",
            [run_id],
        )
        if not dd_df.is_empty():
            drawdown_periods = dd_df.to_dicts()
    except Exception as exc:
        logger.warning("export: drawdown periods unavailable for %s: %s", run_id, exc)

    # Compute daily drawdown series for chart
    drawdown_series: list[dict] = []
    if nav_dates and nav_values:
        peak = nav_values[0]
        for i, (d, v) in enumerate(zip(nav_dates, nav_values)):
            peak = max(peak, v)
            dd = (v - peak) / peak if peak > 0 else 0.0
            drawdown_series.append({"trade_date": d, "drawdown": dd})

    # 8. 加载滚动风险指标
    rolling_risk: list[dict] = []
    try:
        risk_rolling_df = catalog.query(
            'SELECT trade_date, rolling_vol AS volatility '
            'FROM gold_risk_rolling WHERE run_id = ? AND "window" = 60 ORDER BY trade_date',
            [run_id],
        )
        if not risk_rolling_df.is_empty():
            rolling_risk = risk_rolling_df.to_dicts()
    except Exception as exc:
        logger.warning("export: rolling risk unavailable for %s: %s", run_id, exc)

    # 9. 加载收益率分布
    returns_list: list[float] = []
    ret_df = catalog.query(
        "SELECT portfolio_return FROM gold_portfolio_snapshots WHERE run_id = ? ORDER BY trade_date",
        [run_id],
    )
    if not ret_df.is_empty():
        returns_list = ret_df["portfolio_return"].to_list()

    # 10. 加载 TCA 数据
    tca_data: dict = {}
    try:
        tca_df = catalog.query(
            "SELECT * FROM gold_bt_tca WHERE analysis_run_id IN "
            "(SELECT analysis_run_id FROM gold_bt_analysis_runs WHERE backtest_run_id = ? "
            "ORDER BY created_at DESC LIMIT 1)",
            [run_id],
        )
        if not tca_df.is_empty():
            tca_data = tca_df.to_dicts()[0]
            tca_data["implementation_shortfall"] = _compute_implementation_shortfall(catalog, run_id)
    except Exception as exc:
        logger.warning("export: TCA data unavailable for %s: %s", run_id, exc)


    # 12. 生成服务端 SVG 图表（无外部依赖，离线可用）
    nav_svg = _nav_to_svg(nav_dates, nav_values, bm_values=bm_values or None)
    annual_svg = _annual_returns_svg(
        [y for y, _ in annual_returns], [r for _, r in annual_returns]
    )
    drawdown_svg = _drawdown_to_svg(drawdown_series)
    rolling_vol_svg = _rolling_vol_to_svg(rolling_risk)
    return_dist_svg = _return_dist_to_svg(returns_list)
    tca_svg = _tca_pie_svg(tca_data)

    # 13. 渲染 HTML（启用 autoescape 防止 XSS）
    tmpl_dir = pathlib.Path(__file__).parent.parent / "templates"
    env = Environment(
        loader=FileSystemLoader(str(tmpl_dir)),
        autoescape=select_autoescape(["html"]),
    )
    template = env.get_template("backtest_report.html")

    html_content = template.render(
        run_id=run_id,
        strategy_id=run_info.get("strategy_id", ""),
        engine=run_info.get("engine", ""),
        status=run_info.get("status", ""),
        dataset_version=run_info.get("dataset_version", ""),
        start_date=nav_dates[0] if nav_dates else "—",
        end_date=nav_dates[-1] if nav_dates else "—",
        key_metrics=key_metrics,
        nav_svg=nav_svg,
        annual_svg=annual_svg,
        fills=fills,
        has_annual=bool(annual_returns),
        generated_at=dt.now().strftime("%Y-%m-%d %H:%M"),
        # New sections
        drawdown_svg=drawdown_svg,
        drawdown_periods=drawdown_periods,
        has_drawdown=bool(drawdown_series),
        rolling_vol_svg=rolling_vol_svg,
        has_rolling_vol=bool(rolling_risk),
        return_dist_svg=return_dist_svg,
        has_return_dist=bool(returns_list),
        tca_svg=tca_svg,
        tca_data=tca_data,
        has_tca=bool(tca_data),
    )

    # 9. 文件大小守护（PRD: < 2MB）
    content_bytes = html_content.encode("utf-8")
    if len(content_bytes) > 2 * 1024 * 1024:
        logger.warning("HTML report for %s is %d bytes (> 2MB)", run_id, len(content_bytes))

    # 10. 根据 format 返回 HTML 或 PDF
    if format == "pdf":
        pdf_bytes = _html_to_pdf(content_bytes)
        if pdf_bytes is None:
            raise HTTPException(
                status_code=501,
                detail="PDF generation requires weasyprint or playwright. Install with: pip install weasyprint",
            )
        filename = f"backtest_report_{run_id[:12]}.pdf"
        return Response(
            content=pdf_bytes,
            media_type="application/pdf",
            headers={"Content-Disposition": f'attachment; filename="{filename}"'},
        )

    filename = f"backtest_report_{run_id[:12]}.html"
    return HTMLResponse(
        content=content_bytes.decode("utf-8"),
        headers={"Content-Disposition": f'attachment; filename="{filename}"'},
    )


@router.get("/{run_id}/tearsheet")
async def get_tearsheet(run_id: str, catalog: CatalogDep) -> dict:
    """Portfolio returns time series for tearsheet rendering."""
    run_df = catalog.query(
        "SELECT run_id, engine, strategy_id, status FROM gold_backtest_runs WHERE run_id = ? LIMIT 1",
        [run_id],
    )
    if run_df.is_empty():
        raise HTTPException(status_code=404, detail=f"Backtest run '{run_id}' not found")

    analysis_df = catalog.query(
        "SELECT overall_overfit_score, psr, dsr, summary "
        "FROM gold_bt_analysis_runs WHERE backtest_run_id = ? ORDER BY created_at DESC LIMIT 1",
        [run_id],
    )
    analysis = analysis_df.to_dicts()[0] if not analysis_df.is_empty() else {}

    risk_df = catalog.query(
        "SELECT snapshot_ts, drawdown, gross_leverage, var_95 "
        "FROM gold_risk_snapshots WHERE run_id = ? ORDER BY snapshot_ts",
        [run_id],
    )

    snapshots_df = catalog.query(
        "SELECT trade_date, nav, cash, positions_count "
        "FROM gold_portfolio_snapshots WHERE run_id = ? ORDER BY trade_date",
        [run_id],
    )

    # Load benchmark NAV series
    benchmark_nav: list[dict] = []
    benchmark_asset_id = ""
    try:
        run_meta = catalog.query(
            "SELECT benchmark_asset_id, started_at, completed_at "
            "FROM gold_backtest_runs WHERE run_id = ?",
            [run_id],
        )
        if not run_meta.is_empty():
            row = run_meta.to_dicts()[0]
            benchmark_asset_id = row.get("benchmark_asset_id") or ""
            if benchmark_asset_id and not snapshots_df.is_empty():
                dates = snapshots_df["trade_date"].to_list()
                start_d = str(min(dates))
                end_d = str(max(dates))
                bm_df = catalog.query(
                    "SELECT trade_date, close FROM silver_prices_1d "
                    "WHERE asset_id = ? AND trade_date >= ? AND trade_date <= ? "
                    "ORDER BY trade_date",
                    [benchmark_asset_id, start_d, end_d],
                )
                if not bm_df.is_empty():
                    first_close = float(bm_df["close"][0])
                    if first_close > 0:
                        benchmark_nav = [
                            {
                                "date": str(r["trade_date"]),
                                "nav": float(r["close"]) / first_close,
                            }
                            for r in bm_df.to_dicts()
                        ]
    except Exception as e:
        logger.warning("Failed to load benchmark NAV for %s: %s", run_id, e)

    # Load rebalance dates from fills (distinct trade dates where fills occurred)
    rebalance_dates: list[str] = []
    try:
        fills_df = catalog.query(
            "SELECT DISTINCT trade_date FROM gold_fills WHERE run_id = ? ORDER BY trade_date",
            [run_id],
        )
        if not fills_df.is_empty():
            rebalance_dates = [str(d) for d in fills_df["trade_date"].to_list()]
    except Exception as e:
        logger.warning("Failed to load rebalance dates for %s: %s", run_id, e)

    # Load net-of-fee NAV series (only present when a fee_model was configured)
    net_nav: list[dict] = []
    try:
        from pathlib import Path
        import polars as _pl
        net_path = Path("data/backtest_artifacts") / f"{run_id}_net_returns.parquet"
        if net_path.exists():
            net_df = _pl.read_parquet(net_path)
            if not net_df.is_empty() and "net_nav" in net_df.columns:
                net_nav = [
                    {"date": str(r["trade_date"]), "nav": float(r["net_nav"])}
                    for r in net_df.select(["trade_date", "net_nav"]).to_dicts()
                ]
    except Exception as e:
        logger.warning("Failed to load net-fee series for %s: %s", run_id, e)

    return {
        "run": run_df.to_dicts()[0],
        "analysis": analysis,
        "risk_series": risk_df.to_dicts(),
        "snapshots": snapshots_df.to_dicts() if not snapshots_df.is_empty() else [],
        "note": "portfolio_returns are not yet persisted; use risk_series for PnL approximation",
        "benchmark_asset_id": benchmark_asset_id,
        "benchmark_nav": benchmark_nav,
        "rebalance_dates": rebalance_dates,
        "net_nav": net_nav,
    }


@router.get("/{run_id}/validation-windows")
async def get_validation_windows(run_id: str, catalog: CatalogDep) -> dict:
    """Walk-forward and CPCV validation windows for overfitting analysis."""
    analysis_df = catalog.query(
        "SELECT analysis_run_id FROM gold_bt_analysis_runs WHERE backtest_run_id = ? "
        "ORDER BY created_at DESC LIMIT 1",
        [run_id],
    )
    if analysis_df.is_empty():
        raise HTTPException(status_code=404, detail=f"No analysis found for run '{run_id}'")

    analysis_run_id = analysis_df["analysis_run_id"].item()
    wf_df = catalog.query(
        "SELECT * FROM gold_bt_validation_windows WHERE analysis_run_id = ? AND method = 'walk_forward'",
        [analysis_run_id],
    )
    cpcv_df = catalog.query(
        "SELECT * FROM gold_bt_validation_windows WHERE analysis_run_id = ? AND method = 'cpcv'",
        [analysis_run_id],
    )
    return {"walk_forward": wf_df.to_dicts(), "cpcv": cpcv_df.to_dicts()}


@router.get("/{run_id}/multiple-testing")
async def get_multiple_testing(run_id: str, catalog: CatalogDep) -> dict:
    """Multiple hypothesis testing corrections."""
    analysis_df = catalog.query(
        "SELECT analysis_run_id FROM gold_bt_analysis_runs WHERE backtest_run_id = ? "
        "ORDER BY created_at DESC LIMIT 1",
        [run_id],
    )
    if analysis_df.is_empty():
        raise HTTPException(status_code=404, detail=f"No analysis found for run '{run_id}'")

    analysis_run_id = analysis_df["analysis_run_id"].item()
    mt_df = catalog.query(
        "SELECT method, n_trials, alpha, results_json, accepted "
        "FROM gold_bt_multiple_testing WHERE analysis_run_id = ?",
        [analysis_run_id],
    )
    return {m["method"]: m for m in mt_df.to_dicts()}


@router.get("/{run_id}/fills")
async def get_backtest_fills(
    run_id: str,
    catalog: CatalogDep,
    offset: int = 0,
    limit: int = 50,
    sort_by: str = "trade_date",
    sort_order: str = "desc",
) -> dict:
    """Get fill records for a backtest run with pagination and sorting."""
    offset = max(0, offset)
    limit = max(1, min(limit, 500))
    allowed_sorts = {"trade_date", "asset_id", "side", "qty", "price", "notional", "total_cost"}
    if sort_by not in allowed_sorts:
        sort_by = "trade_date"
    order = "DESC" if sort_order.lower() == "desc" else "ASC"

    count_df = catalog.query(
        "SELECT COUNT(*) as cnt FROM gold_fills WHERE run_id = ?", [run_id]
    )
    total = count_df["cnt"].item() if not count_df.is_empty() else 0

    df = catalog.query(
        f"SELECT trade_date, asset_id, side, qty, price, notional, "
        f"commission, stamp_duty, slippage, total_cost "
        f"FROM gold_fills WHERE run_id = ? ORDER BY {sort_by} {order} LIMIT ? OFFSET ?",
        [run_id, limit, offset],
    )
    return {"items": df.to_dicts(), "total": total, "offset": offset, "limit": limit}


@router.get("/{run_id}/round-trips")
async def get_backtest_round_trips(
    run_id: str,
    catalog: CatalogDep,
) -> dict:
    """Match fills into round-trip trades with MFE/MAE.

    Uses FIFO matching: for each asset, the earliest buy is matched with the
    earliest sell to form a round-trip. For each round-trip, computes P&L,
    holding days, and Maximum Favorable/Adverse Excursion (MFE/MAE) by looking
    up intraperiod prices from silver_prices_1d.
    """
    # 1. Validate run exists
    run_df = catalog.query(
        "SELECT run_id, status FROM gold_backtest_runs WHERE run_id = ?", [run_id]
    )
    if run_df.is_empty():
        raise HTTPException(status_code=404, detail=f"Backtest run '{run_id}' not found")

    # 2. Load all fills sorted by asset, then date
    fills_df = catalog.query(
        "SELECT trade_date, asset_id, side, price, qty "
        "FROM gold_fills WHERE run_id = ? ORDER BY asset_id, trade_date",
        [run_id],
    )
    if fills_df.is_empty():
        return {
            "total_round_trips": 0,
            "avg_holding_days": 0.0,
            "win_rate": 0.0,
            "avg_win_pct": 0.0,
            "avg_loss_pct": 0.0,
            "round_trips": [],
        }

    fills = fills_df.to_dicts()

    def _parse_date(date_str: str) -> date:
        """Parse date string to date object."""
        return date.fromisoformat(str(date_str)[:10])

    # 3. FIFO match buys to sells per asset
    queues: dict[str, list[dict]] = {}  # asset_id -> list of open buy fills
    round_trips: list[dict] = []
    assets_needing_prices: set[str] = set()
    min_date = None
    max_date = None

    for fill in fills:
        asset_id = fill["asset_id"]
        side = fill["side"]
        trade_date = str(fill["trade_date"])[:10]
        price = float(fill["price"])
        qty = int(fill["qty"])

        if side == "buy":
            queues.setdefault(asset_id, []).append({
                "date": trade_date,
                "price": price,
                "qty": qty,
            })
        elif side == "sell":
            queue = queues.get(asset_id, [])
            remaining_sell = qty
            while queue and remaining_sell > 0:
                buy = queue[0]
                matched_qty = min(buy["qty"], remaining_sell)
                direction = "long"
                direction_mult = 1.0

                buy_price = buy["price"]
                sell_price = price
                pnl = (sell_price - buy_price) * matched_qty * direction_mult
                pnl_pct = (sell_price / buy_price - 1) * direction_mult if buy_price > 0 else 0.0

                try:
                    bd = _parse_date(buy["date"])
                    sd = _parse_date(trade_date)
                    holding_days = max(1, (sd - bd).days)
                except Exception:
                    holding_days = 1

                rt = {
                    "asset_id": asset_id,
                    "direction": direction,
                    "entry_date": buy["date"],
                    "entry_price": buy_price,
                    "exit_date": trade_date,
                    "exit_price": sell_price,
                    "holding_days": holding_days,
                    "pnl": round(pnl, 4),
                    "pnl_pct": round(pnl_pct, 6),
                    "mfe": 0.0,
                    "mae": 0.0,
                }
                round_trips.append(rt)
                assets_needing_prices.add(asset_id)

                # Track date range for price lookup
                if min_date is None or buy["date"] < min_date:
                    min_date = buy["date"]
                if max_date is None or trade_date > max_date:
                    max_date = trade_date

                buy["qty"] -= matched_qty
                remaining_sell -= matched_qty
                if buy["qty"] <= 0:
                    queue.pop(0)

    if not round_trips:
        return {
            "total_round_trips": 0,
            "avg_holding_days": 0.0,
            "win_rate": 0.0,
            "avg_win_pct": 0.0,
            "avg_loss_pct": 0.0,
            "round_trips": [],
        }

    # 4. Fetch price data for MFE/MAE calculation
    asset_list = sorted(assets_needing_prices)
    asset_ph = ",".join(["?" for _ in asset_list])
    price_df = catalog.query(
        f"SELECT trade_date, asset_id, close FROM silver_prices_1d "
        f"WHERE asset_id IN ({asset_ph}) AND trade_date >= ? AND trade_date <= ? "
        f"ORDER BY asset_id, trade_date",
        asset_list + [min_date, max_date],
    )

    # Build price lookup: (asset_id, date_str) -> close
    price_map: dict[tuple[str, str], float] = {}
    if not price_df.is_empty():
        for row in price_df.to_dicts():
            key = (row["asset_id"], str(row["trade_date"])[:10])
            price_map[key] = float(row["close"])

    # 5. Compute MFE/MAE for each round-trip
    for rt in round_trips:
        aid = rt["asset_id"]
        entry_date = rt["entry_date"]
        exit_date = rt["exit_date"]
        entry_price = rt["entry_price"]
        direction = rt["direction"]

        # Collect all prices during the holding period (inclusive)
        period_prices: list[float] = []
        for (p_asset, p_date), p_close in price_map.items():
            if p_asset == aid and entry_date <= p_date <= exit_date:
                period_prices.append(p_close)

        # Include entry and exit prices even if no market data
        if not period_prices:
            period_prices = [entry_price, rt["exit_price"]]

        if direction == "long":
            mfe = max(period_prices) - entry_price
            mae = entry_price - min(period_prices)
        else:  # short
            mfe = entry_price - min(period_prices)
            mae = max(period_prices) - entry_price

        rt["mfe"] = round(mfe, 4)
        rt["mae"] = round(mae, 4)

    # 6. Compute summary stats
    total = len(round_trips)
    avg_holding = sum(rt["holding_days"] for rt in round_trips) / total
    wins = [rt for rt in round_trips if rt["pnl"] > 0]
    losses = [rt for rt in round_trips if rt["pnl"] <= 0]
    win_rate = len(wins) / total if total > 0 else 0.0
    avg_win_pct = (
        sum(rt["pnl_pct"] for rt in wins) / len(wins) if wins else 0.0
    )
    avg_loss_pct = (
        sum(rt["pnl_pct"] for rt in losses) / len(losses) if losses else 0.0
    )

    return {
        "total_round_trips": total,
        "avg_holding_days": round(avg_holding, 2),
        "win_rate": round(win_rate, 4),
        "avg_win_pct": round(avg_win_pct, 6),
        "avg_loss_pct": round(avg_loss_pct, 6),
        "round_trips": round_trips,
    }


@router.get("/{run_id}/walk-forward-folds")
async def get_walk_forward_folds(run_id: str, catalog: CatalogDep) -> dict:
    """Get walk-forward fold details for a run."""
    run_df = catalog.query(
        "SELECT is_walk_forward, aggregated_metrics_json FROM gold_backtest_runs WHERE run_id = ?",
        [run_id],
    )
    if run_df.is_empty():
        raise HTTPException(status_code=404, detail=f"Backtest run '{run_id}' not found")

    run = run_df.to_dicts()[0]
    if not run.get("is_walk_forward"):
        raise HTTPException(status_code=400, detail="This run is not a walk-forward backtest")

    folds_df = catalog.query(
        "SELECT * FROM gold_wf_folds WHERE run_id = ? ORDER BY fold_id",
        [run_id],
    )

    folds = []
    for row in folds_df.to_dicts():
        fold_metrics = {}
        metrics_path = _safe_metrics_path(row["fold_run_id"])
        if metrics_path and metrics_path.exists():
            try:
                fold_metrics = json.loads(metrics_path.read_text())
            except Exception:
                pass
        folds.append({**row, "metrics": fold_metrics})

    aggregated = {}
    if run.get("aggregated_metrics_json"):
        try:
            aggregated = json.loads(run["aggregated_metrics_json"])
        except Exception:
            pass

    return {
        "run_id": run_id,
        "n_folds": len(folds),
        "folds": folds,
        "aggregated": aggregated,
    }


# ── Strategy Ranking and Sensitivity Analysis ─────────────────────────────────


class StrategyRankBody(BaseModel):
    """Request body for strategy ranking."""
    run_ids: list[str]
    weights: dict[str, float] | None = None


class SensitivityBody(BaseModel):
    """Request body for parameter sensitivity analysis."""
    param_grid: dict[str, list]
    primary_metric: str = "sharpe_ratio"
    max_combinations: int = 100


@router.post("/rank")
async def rank_strategies(
    body: StrategyRankBody,
    catalog: CatalogDep,
) -> dict:
    """Rank multiple strategies across multiple dimensions.

    Accepts a list of run_ids and optional custom weights, returns
    ranked strategies with composite scores.
    """
    if not body.run_ids:
        raise HTTPException(status_code=400, detail="run_ids cannot be empty")
    if len(body.run_ids) > 20:
        raise HTTPException(status_code=400, detail="Maximum 20 strategies for ranking")

    # Load metrics for each run
    results = []
    for run_id in body.run_ids:
        metrics_path = _safe_metrics_path(run_id)
        if not metrics_path or not metrics_path.exists():
            logger.warning("Metrics not found for run %s, skipping", run_id)
            continue

        try:
            metrics = json.loads(metrics_path.read_text())
            # Add strategy_id if not present
            if "strategy_id" not in metrics:
                run_df = catalog.query(
                    "SELECT strategy_id FROM gold_backtest_runs WHERE run_id = ?",
                    [run_id],
                )
                if not run_df.is_empty():
                    metrics["strategy_id"] = run_df["strategy_id"].item()
            results.append((run_id, metrics))
        except Exception as e:
            logger.warning("Failed to load metrics for run %s: %s", run_id, e)

    if not results:
        raise HTTPException(status_code=404, detail="No valid metrics found for provided run_ids")

    # Parse weights if provided
    from cquant.backtest_vector.strategy_ranker import RankingWeights, StrategyRanker

    weights = None
    if body.weights:
        try:
            weights = RankingWeights(**body.weights)
        except Exception as e:
            raise HTTPException(status_code=422, detail=f"Invalid weights: {e}")

    # Run ranking
    try:
        ranking_result = StrategyRanker.from_backtest_results(results, weights=weights)
        return {
            "ranked_strategies": ranking_result.ranked_strategies,
            "summary": ranking_result.summary(),
            "weights": ranking_result.weights,
        }
    except Exception as e:
        logger.exception("Strategy ranking failed")
        raise HTTPException(status_code=500, detail=f"Ranking failed: {str(e)[:200]}")


@router.post("/{run_id}/sensitivity")
async def run_sensitivity_analysis(
    run_id: str,
    body: SensitivityBody,
    background_tasks: BackgroundTasks,
    catalog: CatalogDep,
) -> dict:
    """Run parameter sensitivity analysis for a backtest run.

    Tests all combinations of parameters in the grid and returns
    robustness metrics.
    """
    # Validate run exists
    run_df = catalog.query(
        "SELECT run_id, status FROM gold_backtest_runs WHERE run_id = ?", [run_id]
    )
    if run_df.is_empty():
        raise HTTPException(status_code=404, detail=f"Backtest run '{run_id}' not found")
    if run_df["status"][0] != "completed":
        raise HTTPException(status_code=422, detail="Only completed backtests can be analyzed")

    # Validate parameter grid
    if not body.param_grid:
        raise HTTPException(status_code=400, detail="param_grid cannot be empty")

    # Check total combinations
    total_combinations = 1
    for values in body.param_grid.values():
        total_combinations *= len(values)
    if total_combinations > body.max_combinations:
        raise HTTPException(
            status_code=422,
            detail=f"Too many combinations ({total_combinations}). Max allowed: {body.max_combinations}"
        )

    # Ensure job table exists
    _ensure_job_table(catalog)

    # Generate job_id and save initial status
    job_id = str(uuid.uuid4())
    _save_job(catalog, job_id, job_type="sensitivity", status="running", run_id=run_id)

    def _run_sensitivity():
        start_time = time.time()
        timeout_seconds = 1800  # 30 minutes
        try:
            # Load original backtest run info
            run_row = catalog.query(
                "SELECT strategy_id, dataset_version, start_date, end_date, tags, "
                "signal_set_version, benchmark_asset_id "
                "FROM gold_backtest_runs WHERE run_id = ?",
                [run_id],
            )
            if run_row.is_empty():
                raise ValueError(f"Backtest run '{run_id}' not found")

            run_info = run_row.to_dicts()[0]

            # Load the backtest spec to reconstruct for sensitivity analysis
            from cquant.backtest_vector.run import BacktestRunner, BacktestRunSpec

            # Parse tags to get strategy parameters
            tags = json.loads(run_info.get("tags") or "{}")

            # Build a BacktestRunSpec from the original run
            spec = BacktestRunSpec(
                dataset_version=run_info["dataset_version"],
                strategy_id=run_info["strategy_id"],
                start_date=date.fromisoformat(run_info["start_date"]),
                end_date=date.fromisoformat(run_info["end_date"]),
                feature_set_version=run_info.get("signal_set_version") or "",
                benchmark_asset_id=run_info.get("benchmark_asset_id") or "",
                top_n=tags.get("top_n", 10),
                sort_factor=tags.get("sort_factor", "ret_20d"),
                tags=tags,
            )

            # Build the BacktestSpec using the runner
            runner = BacktestRunner(catalog)
            prices = runner._load_prices(spec)
            if prices.is_empty():
                raise ValueError(f"No price data for {spec.start_date} to {spec.end_date}")

            features = runner._load_features(spec)
            strategy = runner._build_strategy(spec)
            cost_model = runner._detect_cost_model(prices)

            from cquant.backtest_vector.engine import BacktestSpec

            base_spec = BacktestSpec(
                strategy=strategy,
                prices=prices,
                start_date=spec.start_date,
                end_date=spec.end_date,
                initial_cash=spec.initial_cash,
                cost_model=cost_model,
                features=features,
                tags=spec.tags,
                risk_policies=spec.risk_policies,
                extra={"catalog": catalog},
            )

            # Create ParameterGrid and run sensitivity analysis
            from cquant.backtest_vector.sensitivity import GridSearchSensitivity, ParameterGrid

            param_grid = ParameterGrid(body.param_grid)
            analyzer = GridSearchSensitivity(
                base_spec=base_spec,
                param_grid=param_grid,
                primary_metric=body.primary_metric,
            )

            # Timeout check before running
            if time.time() - start_time > timeout_seconds:
                _save_job(catalog, job_id, "sensitivity", "failed",
                          run_id=run_id, error="Timeout: exceeded 30 minutes")
                return

            result = analyzer.run(catalog)

            # Save results
            result_data = {
                "summary": result.summary(),
                "best_params": result.best_params,
                "best_metric_value": result.best_metric_value,
                "robustness_score": result.robustness_score,
                "param_grid": body.param_grid,
                "primary_metric": body.primary_metric,
                "total_combinations": len(result.combinations),
            }

            # Save result to artifacts
            result_dir = pathlib.Path("data/sensitivity_artifacts")
            result_dir.mkdir(parents=True, exist_ok=True)
            result_path = result_dir / f"{job_id}.json"
            result_path.write_text(json.dumps(result_data, indent=2, default=str))

            _save_job(catalog, job_id, "sensitivity", "completed", run_id=run_id)
            logger.info("Sensitivity analysis completed for job %s", job_id)

        except Exception as e:
            logger.exception("Sensitivity analysis failed for job %s", job_id)
            _save_job(catalog, job_id, "sensitivity", "failed",
                      run_id=run_id, error=f"Sensitivity failed: {str(e)[:300]}")

    background_tasks.add_task(run_job_async, _run_sensitivity)
    return {"job_id": job_id, "run_id": run_id, "status": "running"}


@router.get("/{run_id}/sensitivity/{job_id}")
async def get_sensitivity_result(
    run_id: str,
    job_id: str,
    catalog: CatalogDep,
) -> dict:
    """Get sensitivity analysis result.

    Returns job status and results when completed.
    """
    _ensure_job_table(catalog)
    job = _load_job(catalog, job_id)
    if not job:
        raise HTTPException(status_code=404, detail=f"Job '{job_id}' not found")

    if job.get("run_id") and job["run_id"] != run_id:
        raise HTTPException(status_code=404, detail=f"Job '{job_id}' does not belong to run '{run_id}'")

    if not _UUID_RE.match(job_id):
        raise HTTPException(status_code=400, detail="Invalid job_id format")

    result = None
    if job["status"] == "completed":
        result_path = pathlib.Path("data/sensitivity_artifacts") / f"{job_id}.json"
        if result_path.exists():
            result = json.loads(result_path.read_text())

    return {
        "job_id": job_id,
        "run_id": run_id,
        "status": job["status"],
        "result": result,
        "error": job.get("error"),
    }


@router.get("/{run_id}/sensitivity/history")
async def get_sensitivity_history(run_id: str, catalog: CatalogDep) -> dict:
    """Get sensitivity scan history for a backtest run."""
    if not _UUID_RE.match(run_id):
        raise HTTPException(status_code=400, detail="Invalid run_id format")
    _ensure_job_table(catalog)
    try:
        df = catalog.query(
            "SELECT job_id, status, created_at, updated_at, error "
            "FROM _api_jobs WHERE job_type = 'sensitivity' AND run_id = ? "
            "ORDER BY created_at DESC LIMIT 10",
            [run_id],
        )
        rows = []
        for row in df.to_dicts():
            rows.append({
                "job_id": row["job_id"],
                "status": row["status"],
                "created_at": row["created_at"],
                "completed_at": row["updated_at"] if row["status"] in ("completed", "failed") else None,
                "error": row.get("error"),
            })
        return {"history": rows}
    except Exception as exc:
        logger.warning("Failed to fetch sensitivity history for run %s: %s", run_id, exc)
        return {"history": []}


# ── Calendar Analysis ───────────────────────────────────────────────────────


@router.get("/{run_id}/calendar-analysis")
async def get_calendar_analysis(run_id: str, catalog: CatalogDep):
    """Calendar effect analysis for a backtest run (month, weekday, month-end)."""
    from cquant.bt_analyzer.calendar_analysis import CalendarAnalyzer
    import polars as pl

    metrics_path = _safe_metrics_path(run_id)
    if metrics_path is None:
        raise HTTPException(status_code=400, detail="Invalid run_id format")

    # Load portfolio returns from the backtest artifacts
    if not metrics_path.exists():
        raise HTTPException(status_code=404, detail=f"Backtest run '{run_id}' not found")

    with open(metrics_path) as f:
        bt_data = json.load(f)

    # Extract nav series and convert to returns
    snapshots = bt_data.get("snapshots", [])
    if not snapshots:
        raise HTTPException(status_code=422, detail="No snapshot data available for calendar analysis")

    nav_data = []
    prev_nav = None
    for snap in snapshots:
        nav = snap.get("nav", 1.0)
        ret = (nav / prev_nav - 1) if prev_nav and prev_nav > 0 else 0.0
        nav_data.append({"trade_date": snap.get("trade_date", ""), "returns": ret})
        prev_nav = nav

    df = pl.DataFrame(nav_data)
    analyzer = CalendarAnalyzer()
    result = analyzer.analyze(df)
    return result.to_dict()


# ── Trade Analysis ──────────────────────────────────────────────────────────


@router.get("/{run_id}/trade-analysis")
async def get_trade_analysis(run_id: str, catalog: CatalogDep):
    """Trade-level analysis: holding periods, win/loss streaks, profit factor."""
    from cquant.bt_analyzer.trade_analysis import TradeAnalyzer
    import polars as pl

    metrics_path = _safe_metrics_path(run_id)
    if metrics_path is None:
        raise HTTPException(status_code=400, detail="Invalid run_id format")

    if not metrics_path.exists():
        raise HTTPException(status_code=404, detail=f"Backtest run '{run_id}' not found")

    # Load fills from the fills table
    try:
        fills_df = catalog.query(
            "SELECT trade_date, asset_id, side, price, qty, notional "
            "FROM silver_fills WHERE run_id = ? ORDER BY asset_id, trade_date",
            [run_id],
        )
    except Exception:
        # Fallback: try loading from artifacts
        fills_df = None

    if fills_df is None or fills_df.is_empty():
        raise HTTPException(status_code=422, detail="No trade/fill data available for trade analysis")

    analyzer = TradeAnalyzer()
    result = analyzer.analyze(fills_df)
    return result.to_dict()
