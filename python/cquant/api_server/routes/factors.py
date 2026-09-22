"""Factor values and analytics routes."""

from __future__ import annotations

import asyncio
import logging
import os
import threading
import time
import uuid
from datetime import datetime, timezone

from fastapi import APIRouter, BackgroundTasks, HTTPException
import re

from pydantic import BaseModel, Field, field_validator

from cquant.api_server.deps import CatalogDep, run_job_async
from cquant.datahub.universe import INDEX_EXCLUSION_SQL

logger = logging.getLogger(__name__)
router = APIRouter(prefix="/factors", tags=["factors"])


_custom_factor_table_ensured = False


def _ensure_custom_factor_table(catalog) -> None:
    """幂等创建自定义因子表（进程内只执行一次）。"""
    global _custom_factor_table_ensured
    if _custom_factor_table_ensured:
        return
    try:
        catalog.execute("""
            CREATE TABLE IF NOT EXISTS meta_custom_factors (
                factor_id   VARCHAR PRIMARY KEY,
                name        VARCHAR UNIQUE NOT NULL,
                expression  VARCHAR NOT NULL,
                description VARCHAR DEFAULT '',
                created_at  TIMESTAMP DEFAULT CURRENT_TIMESTAMP
            )
        """)
        _custom_factor_table_ensured = True
    except Exception as exc:
        logger.debug("_ensure_custom_factor_table: %s", exc)


_ic_summary_table_ensured = False


def _ensure_ic_summary_table(catalog) -> None:
    """幂等创建 IC 汇总表（进程内只执行一次）。"""
    global _ic_summary_table_ensured
    if _ic_summary_table_ensured:
        return
    try:
        catalog.execute("""
            CREATE TABLE IF NOT EXISTS gold_factor_ic_summary (
                factor_name VARCHAR PRIMARY KEY,
                ic_mean DOUBLE,
                icir DOUBLE,
                ic_positive_pct DOUBLE,
                n INTEGER,
                window_start DATE,
                window_end DATE,
                updated_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
            )
        """)
        _ic_summary_table_ensured = True
    except Exception as exc:
        logger.debug("_ensure_ic_summary_table: %s", exc)


def _upsert_ic_summary(
    catalog,
    factor_name: str,
    ic_mean: float,
    icir: float,
    ic_positive_pct: float,
    n: int,
    window_start: str | None,
    window_end: str | None,
) -> None:
    """Upsert 单因子 IC 汇总（供 leaderboard / ic-status / ic-trend 读取）。"""
    try:
        _ensure_ic_summary_table(catalog)
        catalog.execute(
            """
            INSERT INTO gold_factor_ic_summary
                (factor_name, ic_mean, icir, ic_positive_pct, n, window_start, window_end, updated_at)
            VALUES (?, ?, ?, ?, ?, ?, ?, NOW())
            ON CONFLICT (factor_name) DO UPDATE SET
                ic_mean = excluded.ic_mean,
                icir = excluded.icir,
                ic_positive_pct = excluded.ic_positive_pct,
                n = excluded.n,
                window_start = excluded.window_start,
                window_end = excluded.window_end,
                updated_at = excluded.updated_at
            """,
            [factor_name, ic_mean, icir, ic_positive_pct, n, window_start, window_end],
        )
    except Exception as exc:
        logger.warning("upsert gold_factor_ic_summary failed for %s: %s", factor_name, exc)


class CustomFactorCreateBody(BaseModel):
    name: str = Field(..., max_length=64)
    expression: str = Field(..., max_length=500)
    description: str = Field(default="", max_length=200)
    expression_type: str = "polars"

    @field_validator("name")
    @classmethod
    def validate_name(cls, v: str) -> str:
        v = v.strip()
        if not re.match(r'^[A-Za-z_][A-Za-z0-9_]*$', v):
            raise ValueError("因子名称只允许字母、数字、下划线，且不能以数字开头")
        return v


class CustomFactorPreviewBody(BaseModel):
    expression: str = Field(..., max_length=500)
    feature_set_version: str = ""


@router.get("")
async def list_factor_values(
    catalog: CatalogDep,
    feature_set_version: str | None = None,
    factor_name: str | None = None,
    limit: int = 100,
) -> dict:
    """List factor values with optional filtering."""
    conditions = ["1=1"]
    params: list = []
    if feature_set_version:
        conditions.append("feature_set_version = ?")
        params.append(feature_set_version)
    if factor_name:
        conditions.append("factor_name = ?")
        params.append(factor_name)
    params.append(limit)

    where = " AND ".join(conditions)
    df = catalog.query(
        f"SELECT feature_set_version, factor_name, trade_date, asset_id, value "
        f"FROM gold_factor_values WHERE {where} ORDER BY trade_date DESC LIMIT ?",
        params,
    )
    return {"items": df.to_dicts(), "total": df.height}


@router.get("/versions")
async def list_feature_set_versions(catalog: CatalogDep) -> dict:
    """List distinct feature set versions."""
    df = catalog.query(
        "SELECT DISTINCT feature_set_version, MIN(trade_date) AS start_date, "
        "MAX(trade_date) AS end_date, COUNT(*) AS row_count "
        "FROM gold_factor_values GROUP BY feature_set_version ORDER BY end_date DESC"
    )
    return {"items": df.to_dicts()}


class ICComputeBody(BaseModel):
    factor_name: str
    feature_set_version: str
    horizon_days: int = 1


class ICMatrixBody(BaseModel):
    factor_names: list[str]
    feature_set_version: str
    horizon_days: int = 1


@router.post("/analytics/matrix", status_code=202)
async def compute_ic_matrix(
    body: ICMatrixBody,
    background_tasks: BackgroundTasks,
    catalog: CatalogDep,
) -> dict:
    """Submit an async multi-factor IC correlation matrix job."""
    job_id = str(uuid.uuid4())
    now = datetime.now(tz=timezone.utc).isoformat()
    catalog.execute(
        "INSERT INTO meta_factor_analytics "
        "(job_id, factor_name, feature_set_version, horizon_days, status, submitted_at) "
        "VALUES (?, ?, ?, ?, 'pending', ?)",
        [job_id, "__matrix__:" + ",".join(body.factor_names), body.feature_set_version, body.horizon_days, now],
    )
    background_tasks.add_task(run_job_async, _compute_ic_matrix, job_id, body, catalog)
    return {"job_id": job_id, "status": "submitted"}


# 已物化因子集合的短 TTL 缓存：gold_factor_values 是千万行级大表，
# /available 高频端点不应每请求都跑 SELECT DISTINCT。
_materialized_cache: tuple[float, frozenset] | None = None  # (monotonic_ts, names)
_materialized_cache_lock = threading.Lock()


def _materialized_cache_ttl() -> float:
    """TTL 秒数，env CQUANT_FACTOR_CATALOG_TTL_SEC（默认 60，0=禁用缓存）。"""
    raw = os.getenv("CQUANT_FACTOR_CATALOG_TTL_SEC", "60")
    try:
        return max(0.0, float(raw))
    except ValueError:
        return 60.0


def _get_materialized_names(catalog) -> set:
    """带 TTL 缓存的已物化因子集合查询。"""
    global _materialized_cache
    ttl = _materialized_cache_ttl()
    if ttl > 0:
        cached = _materialized_cache
        if cached is not None and (time.monotonic() - cached[0]) < ttl:
            return set(cached[1])

    try:
        df = catalog.query("SELECT DISTINCT factor_name FROM gold_factor_values")
        materialized = set(df["factor_name"].to_list()) if not df.is_empty() else set()
    except Exception as exc:
        logger.debug("gold_factor_values unavailable: %s", exc)
        materialized = set()

    if ttl > 0:
        with _materialized_cache_lock:
            _materialized_cache = (time.monotonic(), frozenset(materialized))
    return materialized


def _factor_catalog_state(catalog) -> tuple[dict, set, list[dict]]:
    """单一事实源：因子目录状态（注册表 + 已物化 + 自定义）。

    /available 与 /definitions 共用此函数，消除三套名单漂移。
    Returns (builtin_by_name, materialized_names, custom_rows).
    """
    from cquant.factorlab.factors import BUILTIN_FACTORS

    builtin_by_name = {f.name: f for f in BUILTIN_FACTORS}

    # 已物化因子：gold_factor_values 的 factor_name distinct（短 TTL 缓存）
    materialized = _get_materialized_names(catalog)

    # 自定义因子
    custom_rows: list[dict] = []
    try:
        _ensure_custom_factor_table(catalog)
        custom_df = catalog.query(
            "SELECT factor_id, name, expression, description FROM meta_custom_factors ORDER BY created_at DESC"
        )
        if not custom_df.is_empty():
            custom_rows = custom_df.to_dicts()
    except Exception as exc:
        logger.debug("meta_custom_factors unavailable: %s", exc)

    return builtin_by_name, materialized, custom_rows


@router.get("/definitions")
async def factor_definitions(catalog: CatalogDep) -> dict:
    """列出所有可用因子定义（内置 + 自定义，与 /available 同源）。"""
    builtin_by_name, _, custom_rows = _factor_catalog_state(catalog)

    items = [
        {"name": f.name, "description": f.description, "tags": f.tags, "source": "builtin"}
        for f in builtin_by_name.values()
    ]
    for row in custom_rows:
        items.append({
            "name": row["name"],
            "description": row["description"] or f"自定义: {row['expression'][:40]}",
            "tags": ["custom"],
            "source": "custom",
            "factor_id": row["factor_id"],
            "expression": row["expression"],
        })

    return {"items": items, "total": len(items)}


@router.get("/custom")
async def list_custom_factors(catalog: CatalogDep) -> dict:
    """列出所有自定义因子。"""
    _ensure_custom_factor_table(catalog)
    df = catalog.query(
        "SELECT factor_id, name, expression, description, created_at "
        "FROM meta_custom_factors ORDER BY created_at DESC"
    )
    return {"items": df.to_dicts() if not df.is_empty() else []}


@router.post("/custom", status_code=201)
async def create_custom_factor(body: CustomFactorCreateBody, catalog: CatalogDep) -> dict:
    """创建自定义因子（含语法验证）。"""
    import uuid as _uuid
    from cquant.factorlab.factors.expression_factor import ExpressionFactor

    if body.expression_type == "dsl":
        from cquant.factorlab.dsl_evaluator import compile_expression, DSLError
        try:
            compile_expression(body.expression)
        except (SyntaxError, DSLError) as e:
            raise HTTPException(status_code=400, detail=f"DSL 表达式错误: {e}")
    else:
        validation = ExpressionFactor.validate_expression(body.expression)
        if not validation["valid"]:
            raise HTTPException(status_code=400, detail=validation["error"])

    # Check builtin name conflict
    from cquant.factorlab.factors import BUILTIN_FACTORS
    builtin_names = {f.name for f in BUILTIN_FACTORS}
    if body.name in builtin_names:
        raise HTTPException(status_code=409, detail=f"因子名称 '{body.name}' 与内置因子冲突")

    _ensure_custom_factor_table(catalog)
    existing = catalog.query(
        "SELECT factor_id FROM meta_custom_factors WHERE name = ?",
        [body.name],
    )
    if not existing.is_empty():
        raise HTTPException(status_code=409, detail=f"因子名称 '{body.name}' 已存在")

    factor_id = f"cf_{_uuid.uuid4().hex[:10]}"
    catalog.execute(
        "INSERT INTO meta_custom_factors (factor_id, name, expression, description) VALUES (?, ?, ?, ?)",
        [factor_id, body.name, body.expression, body.description],
    )
    _invalidate_factors_cache()
    return {"factor_id": factor_id, "name": body.name, "status": "created"}


_QLIB_SYNTAX_RE = re.compile(r"\$|\b(Ref|Mean|Std|Rank|Corr|Delta|Sum|Max|Min|Abs|Sign|IdxMax|IdxMin)\s*\(")


def _qlib_syntax_hint(expression: str) -> str:
    """检测 Qlib 语法（$close / Ref(...) 大写函数）并返回提示，否则空字符串。"""
    if _QLIB_SYNTAX_RE.search(expression):
        return "（检测到 Qlib 语法，本编辑器使用小写 DSL，例如 $close/Ref(close,20) 应写作 ref(close,20)）"
    return ""


@router.post("/custom/preview")
async def preview_custom_factor(body: CustomFactorPreviewBody, catalog: CatalogDep) -> dict:
    """预览自定义因子：用数据中最近交易日回溯 30 天的样本试算，返回前10行结果。"""
    import polars as pl
    from cquant.factorlab.factors.expression_factor import ExpressionFactor

    # Syntax-only check first (before loading sample data)
    syntax_check = ExpressionFactor.validate_expression(body.expression)
    if not syntax_check["valid"]:
        return {"valid": False, "error": syntax_check["error"] + _qlib_syntax_hint(body.expression), "preview": []}

    try:
        sample_df = catalog.query(
            "SELECT asset_id, trade_date, open, high, low, close, volume, amount "
            "FROM silver_prices_1d "
            "WHERE trade_date >= (SELECT max(trade_date) - INTERVAL '30 days' FROM silver_prices_1d) "
            "ORDER BY asset_id, trade_date "
            "LIMIT 50"
        )
    except Exception:
        sample_df = pl.DataFrame()

    if sample_df.is_empty():
        return {"valid": True, "error": None, "preview": [], "note": "无样本数据，仅语法验证通过"}

    # Runtime check with real data (subsumes syntax check, so no need to repeat)
    runtime_check = ExpressionFactor.validate_expression(body.expression, sample_df)
    if not runtime_check["valid"]:
        return {"valid": False, "error": runtime_check["error"] + _qlib_syntax_hint(body.expression), "preview": []}

    factor = ExpressionFactor("__preview__", body.expression)
    result = factor.compute(sample_df, None)  # type: ignore
    preview_vals = result.to_list()[:10]
    preview = [
        {
            "asset_id": str(sample_df["asset_id"][i]),
            "trade_date": str(sample_df["trade_date"][i]),
            "value": round(float(v), 6) if v is not None else None,
        }
        for i, v in enumerate(preview_vals)
    ]
    return {"valid": True, "error": None, "preview": preview}


@router.delete("/custom/{factor_id}")
async def delete_custom_factor(factor_id: str, catalog: CatalogDep) -> dict:
    """删除自定义因子。"""
    _ensure_custom_factor_table(catalog)
    existing = catalog.query(
        "SELECT factor_id FROM meta_custom_factors WHERE factor_id = ?",
        [factor_id],
    )
    if existing.is_empty():
        raise HTTPException(status_code=404, detail=f"Custom factor '{factor_id}' not found")
    catalog.execute("DELETE FROM meta_custom_factors WHERE factor_id = ?", [factor_id])
    _invalidate_factors_cache()
    return {"factor_id": factor_id, "status": "deleted"}


@router.post("/analytics/compute", status_code=202)
async def compute_ic_analytics(
    body: ICComputeBody,
    background_tasks: BackgroundTasks,
    catalog: CatalogDep,
) -> dict:
    """Submit an async IC/IR computation job."""
    job_id = str(uuid.uuid4())
    now = datetime.now(tz=timezone.utc).isoformat()
    catalog.execute(
        "INSERT INTO meta_factor_analytics "
        "(job_id, factor_name, feature_set_version, horizon_days, status, submitted_at) "
        "VALUES (?, ?, ?, ?, 'pending', ?)",
        [job_id, body.factor_name, body.feature_set_version, body.horizon_days, now],
    )
    background_tasks.add_task(run_job_async, _compute_ic, job_id, body, catalog)
    return {"job_id": job_id, "status": "submitted"}


@router.get("/analytics/{job_id}")
async def get_ic_analytics(job_id: str, catalog: CatalogDep) -> dict:
    df = catalog.query(
        "SELECT * FROM meta_factor_analytics WHERE job_id = ?", [job_id]
    )
    if df.is_empty():
        raise HTTPException(status_code=404, detail=f"Job '{job_id}' not found")
    return df.to_dicts()[0]


def _compute_ic(job_id: str, body: ICComputeBody, catalog: CatalogDep) -> None:
    """Background task: compute IC series for a factor."""
    import polars as pl
    import json
    import numpy as np
    from collections import defaultdict

    try:
        catalog.execute(
            "UPDATE meta_factor_analytics SET status = 'running' WHERE job_id = ?", [job_id]
        )

        # Verify gold_factor_values table exists and has data
        try:
            table_check = catalog.query(
                "SELECT COUNT(*) AS cnt FROM gold_factor_values "
                "WHERE feature_set_version = ? AND factor_name = ?",
                [body.feature_set_version, body.factor_name],
            )
            row_count = table_check["cnt"][0] if not table_check.is_empty() else 0
        except Exception as table_err:
            raise ValueError(
                f"无法查询 gold_factor_values 表，请先物化因子。"
                f"错误: {table_err}"
            ) from table_err

        if row_count == 0:
            raise ValueError(
                f"未找到因子 '{body.factor_name}' 在版本 '{body.feature_set_version}' 的数据。"
                f"请先在「因子研究」页面物化该因子，然后再计算 IC。"
            )

        # Load factor values and next-period returns. Exclude sector/block
        # indices (880xxx/881xxx) on the factor-value side — the inner join
        # with prices then keeps them out of the IC sample.
        factor_df = catalog.query(
            "SELECT trade_date, asset_id, value FROM gold_factor_values "
            f"WHERE feature_set_version = ? AND factor_name = ? "
            f"AND {INDEX_EXCLUSION_SQL} ORDER BY trade_date, asset_id",
            [body.feature_set_version, body.factor_name],
        )
        if factor_df.is_empty():
            raise ValueError(
                f"未找到因子 '{body.factor_name}' 的数据。请先物化该因子。"
            )

        # Try to load next-bar returns from silver_prices_1d
        ret_name = f"ret_{body.horizon_days}d"
        try:
            price_df = catalog.query(
                "SELECT trade_date, asset_id, close FROM silver_prices_1d ORDER BY asset_id, trade_date"
            )
        except Exception as price_err:
            raise ValueError(
                f"无法查询 silver_prices_1d 表: {price_err}"
            ) from price_err

        if price_df.is_empty():
            raise ValueError("silver_prices_1d 无价格数据，请先摄取市场数据。")

        price_with_ret = (
            price_df.sort(["asset_id", "trade_date"])
            .with_columns(
                (pl.col("close") / pl.col("close").shift(body.horizon_days).over("asset_id") - 1)
                .alias(ret_name)
            )
        )

        merged = factor_df.join(
            price_with_ret.select(["trade_date", "asset_id", ret_name]),
            on=["trade_date", "asset_id"],
            how="inner",
        ).drop_nulls()

        # Compute daily IC = cross-sectional rank correlation
        series = []
        for dt, group in merged.group_by("trade_date"):
            if group.height < 5:
                continue
            f_rank = group["value"].rank().to_numpy()
            r_rank = group[ret_name].rank().to_numpy()
            ic = float(np.corrcoef(f_rank, r_rank)[0, 1]) if len(f_rank) > 1 else 0.0
            series.append({"trade_date": str(dt[0]), "ic": round(ic, 6)})

        series.sort(key=lambda x: x["trade_date"])
        ic_values = [s["ic"] for s in series]
        summary = {
            "mean_ic": round(float(np.mean(ic_values)), 6) if ic_values else 0.0,
            "ir": round(float(np.mean(ic_values) / (np.std(ic_values) + 1e-12)), 4) if ic_values else 0.0,
            "hit_rate": round(float(sum(1 for v in ic_values if v > 0) / max(len(ic_values), 1)), 4),
            "observations": len(ic_values),
        }

        # ── Rank IC decay（lag 1-10）──────────────────────────────
        sorted_dates = sorted(merged["trade_date"].unique().to_list())
        date_map: dict = {dt[0]: grp for dt, grp in merged.group_by("trade_date")}
        rank_ic_decay = []
        for lag in range(1, 11):
            decay_ics = []
            for i in range(len(sorted_dates) - lag):
                date_t = sorted_dates[i]
                date_tlag = sorted_dates[i + lag]
                factor_t = date_map[date_t].select(["asset_id", "value"])
                ret_tlag = date_map.get(date_tlag, pl.DataFrame()).select(["asset_id", ret_name])
                joined = factor_t.join(ret_tlag, on="asset_id", how="inner")
                if joined.height < 5:
                    continue
                f_arr = joined["value"].rank().to_numpy()
                r_arr = joined[ret_name].rank().to_numpy()
                ic_val = float(np.corrcoef(f_arr, r_arr)[0, 1])
                if not np.isnan(ic_val):
                    decay_ics.append(ic_val)
            rank_ic_decay.append({
                "lag": lag,
                "ic": round(float(np.mean(decay_ics)), 6) if decay_ics else 0.0,
            })

        # ── Quantile returns（5 分组）────────────────────────────
        q_buckets: dict[int, list[float]] = defaultdict(list)
        for dt in sorted_dates:
            group = date_map[dt]
            if group.height < 10:
                continue
            sorted_g = group.sort("value")
            n = len(sorted_g)
            q_size = n // 5
            for q in range(5):
                start_idx = q * q_size
                end_idx = (q + 1) * q_size if q < 4 else n
                sliced = sorted_g.slice(start_idx, end_idx - start_idx)
                _m = sliced[ret_name].mean()
                mean_ret = float(_m) if _m is not None else 0.0
                q_buckets[q + 1].append(mean_ret)
        quantile_returns = [
            {"quantile": q, "mean_return": round(float(np.mean(vals)), 6)}
            for q, vals in sorted(q_buckets.items())
        ]

        # ── Factor turnover（Top 20%）────────────────────────────
        top_n_assets = max(1, int(0.2 * merged["asset_id"].n_unique()))
        turnovers: list[float] = []
        prev_top: set[str] = set()
        for dt in sorted_dates:
            today_top = set(
                date_map[dt]
                .sort("value", descending=True)
                .head(top_n_assets)["asset_id"]
                .to_list()
            )
            if prev_top:
                overlap = len(today_top & prev_top)
                turnovers.append(1.0 - overlap / max(len(today_top), 1))
            prev_top = today_top
        factor_turnover = round(float(np.mean(turnovers)), 4) if turnovers else 0.0

        # 追加到 summary
        summary["rank_ic_decay"] = rank_ic_decay
        summary["quantile_returns"] = quantile_returns
        summary["factor_turnover"] = factor_turnover

        # ── 净 IC（线性换手惩罚）─────────────────────────────────
        # net_ic = mean_ic - cost_rate × factor_turnover
        # 惩罚高换手因子的交易成本拖累。默认 30bps 单边费率。
        net_cost_rate = 0.003
        summary["net_ic"] = round(
            float(summary["mean_ic"]) - net_cost_rate * factor_turnover, 6
        )

        # ── IC 显著性检验（Newey-West HAC）+ 半衰期 ──────────────
        try:
            from cquant.factorlab.evaluation import FactorEvaluator

            ic_arr = np.asarray(ic_values, dtype=float)
            ttest = FactorEvaluator.ic_ttest(ic_arr)
            summary["ic_ttest"] = {
                "t_stat": round(float(ttest["t_stat"]), 4) if np.isfinite(ttest["t_stat"]) else None,
                "p_value": round(float(ttest["p_value"]), 4) if np.isfinite(ttest["p_value"]) else None,
                "ci_lower": round(float(ttest["ci_lower"]), 6) if np.isfinite(ttest["ci_lower"]) else None,
                "ci_upper": round(float(ttest["ci_upper"]), 6) if np.isfinite(ttest["ci_upper"]) else None,
                "n": int(ttest["n"]),
                "significant": bool(
                    ttest["n"] >= 30
                    and np.isfinite(ttest["p_value"])
                    and ttest["p_value"] < 0.05
                ),
            }
            decay_ics_arr = np.asarray(
                [d["ic"] for d in rank_ic_decay], dtype=float
            )
            hl = FactorEvaluator.half_life(decay_ics_arr)
            summary["ic_half_life"] = round(float(hl), 2) if hl is not None else None
        except Exception:
            logger.debug("IC t-test / half-life computation skipped", exc_info=True)

        catalog.execute(
            "UPDATE meta_factor_analytics SET status = 'done', series_json = ?, summary_json = ?, completed_at = ? WHERE job_id = ?",
            [json.dumps(series), json.dumps(summary), datetime.now(tz=timezone.utc).isoformat(), job_id],
        )

        # 落 IC 汇总表（供 ic-leaderboard / ic-status / ic-trend 读取）
        _upsert_ic_summary(
            catalog,
            body.factor_name,
            float(summary["mean_ic"]),
            float(summary["ir"]),
            float(summary["hit_rate"]),
            int(summary["observations"]),
            series[0]["trade_date"] if series else None,
            series[-1]["trade_date"] if series else None,
        )
    except Exception as exc:
        error_msg = str(exc)
        logger.exception("IC compute job %s failed: %s", job_id, error_msg)
        # 用户友好的错误信息
        if "No factor values found" in error_msg or "未找到因子" in error_msg:
            user_msg = error_msg
        elif "INTERNAL Error" in error_msg or "unique_ptr" in error_msg:
            user_msg = (
                f"DuckDB 内部错误，可能是因子数据未物化或表结构问题。"
                f"请先在「因子研究」页面物化因子 '{body.factor_name}'。"
                f"原始错误: {error_msg[:200]}"
            )
        else:
            user_msg = f"IC 计算失败: {error_msg[:300]}"
        catalog.execute(
            "UPDATE meta_factor_analytics SET status = 'error', error_text = ?, completed_at = ? WHERE job_id = ?",
            [user_msg, datetime.now(tz=timezone.utc).isoformat(), job_id],
        )


def _compute_ic_matrix(job_id: str, body: ICMatrixBody, catalog: CatalogDep) -> None:
    """Background task: compute IC series for multiple factors and their correlation matrix."""
    import polars as pl
    import json
    import numpy as np

    try:
        catalog.execute(
            "UPDATE meta_factor_analytics SET status = 'running' WHERE job_id = ?", [job_id]
        )

        ret_name = f"ret_{body.horizon_days}d"
        price_df = catalog.query(
            "SELECT trade_date, asset_id, close FROM silver_prices_1d ORDER BY asset_id, trade_date"
        )
        if price_df.is_empty():
            raise ValueError("silver_prices_1d 无价格数据")

        price_with_ret = (
            price_df.sort(["asset_id", "trade_date"])
            .with_columns(
                (pl.col("close") / pl.col("close").shift(body.horizon_days).over("asset_id") - 1)
                .alias(ret_name)
            )
        )

        # Compute IC series for each factor
        factor_ics: dict[str, list[tuple[str, float]]] = {}
        for factor_name in body.factor_names:
            factor_df = catalog.query(
                "SELECT trade_date, asset_id, value FROM gold_factor_values "
                f"WHERE feature_set_version = ? AND factor_name = ? "
                f"AND {INDEX_EXCLUSION_SQL} ORDER BY trade_date, asset_id",
                [body.feature_set_version, factor_name],
            )
            if factor_df.is_empty():
                continue

            merged = factor_df.join(
                price_with_ret.select(["trade_date", "asset_id", ret_name]),
                on=["trade_date", "asset_id"],
                how="inner",
            ).drop_nulls()

            series = []
            for dt, group in merged.group_by("trade_date"):
                if group.height < 5:
                    continue
                f_rank = group["value"].rank().to_numpy()
                r_rank = group[ret_name].rank().to_numpy()
                ic = float(np.corrcoef(f_rank, r_rank)[0, 1]) if len(f_rank) > 1 else 0.0
                if not np.isnan(ic):
                    series.append((str(dt[0]), ic))
            series.sort(key=lambda x: x[0])
            factor_ics[factor_name] = series

        if not factor_ics:
            raise ValueError("未找到任何因子数据")

        # Build aligned IC matrix (dates x factors)
        all_dates = sorted(set(d for ics in factor_ics.values() for d, _ in ics))
        factor_names = sorted(factor_ics.keys())
        ic_map: dict[str, dict[str, float]] = {fn: dict(ics) for fn, ics in factor_ics.items()}

        ic_matrix = []
        for dt in all_dates:
            row = [ic_map.get(fn, {}).get(dt, 0.0) for fn in factor_names]
            ic_matrix.append(row)

        ic_array = np.array(ic_matrix)
        # Correlation between factor IC series
        corr = np.corrcoef(ic_array, rowvar=False)
        corr = np.nan_to_num(corr, nan=0.0).tolist()

        # Summary stats per factor
        factor_stats = {}
        for i, fn in enumerate(factor_names):
            vals = ic_array[:, i].tolist()
            factor_stats[fn] = {
                "mean_ic": round(float(np.mean(vals)), 6),
                "ir": round(float(np.mean(vals) / (np.std(vals) + 1e-12)), 4),
                "hit_rate": round(float(sum(1 for v in vals if v > 0) / max(len(vals), 1)), 4),
            }

        summary = {
            "factors": factor_names,
            "correlation": corr,
            "factor_stats": factor_stats,
            "observations": len(all_dates),
        }

        catalog.execute(
            "UPDATE meta_factor_analytics SET status = 'done', series_json = ?, summary_json = ?, completed_at = ? WHERE job_id = ?",
            [json.dumps([]), json.dumps(summary), datetime.now(tz=timezone.utc).isoformat(), job_id],
        )

        # 落 IC 汇总表（每个因子一条）
        for fn, stats in factor_stats.items():
            _upsert_ic_summary(
                catalog,
                fn,
                float(stats["mean_ic"]),
                float(stats["ir"]),
                float(stats["hit_rate"]),
                len(all_dates),
                all_dates[0] if all_dates else None,
                all_dates[-1] if all_dates else None,
            )
    except Exception as exc:
        error_msg = str(exc)
        logger.exception("IC matrix job %s failed: %s", job_id, error_msg)
        catalog.execute(
            "UPDATE meta_factor_analytics SET status = 'error', error_text = ?, completed_at = ? WHERE job_id = ?",
            [f"IC 矩阵计算失败: {error_msg[:300]}", datetime.now(tz=timezone.utc).isoformat(), job_id],
        )


# ── Factor Quintile Returns ───────────────────────────────────────────────────

class QuintileRequest(BaseModel):
    factor_name: str
    feature_set_version: str
    horizon_days: int = 5
    start_date: str = ""
    end_date: str = ""
    n_groups: int = Field(default=5, ge=2, le=20)


@router.post("/analytics/quintiles")
async def compute_quintile_returns(body: QuintileRequest, catalog: CatalogDep) -> dict:
    """计算因子分层收益（Q1–Qn 平均收益）。"""
    import polars as pl

    ret_name = f"ret_{body.horizon_days}d"
    start = body.start_date or "2023-01-01"
    end = body.end_date or "2025-12-31"

    factor_df = catalog.query(
        "SELECT asset_id, trade_date, value FROM gold_factor_values "
        "WHERE feature_set_version = ? AND factor_name = ? "
        "AND trade_date >= ? AND trade_date <= ?",
        [body.feature_set_version, body.factor_name, start, end],
    )
    if factor_df.is_empty():
        return {"factor_name": body.factor_name, "n_groups": body.n_groups, "groups": []}

    prices_df = catalog.query(
        "SELECT asset_id, trade_date, close FROM silver_prices_1d "
        "WHERE trade_date >= ? AND trade_date <= ?",
        [start, end],
    )
    prices_df = prices_df.with_columns(
        (pl.col("close") / pl.col("close").shift(body.horizon_days).over("asset_id", order_by="trade_date") - 1)
        .alias(ret_name)
    )

    joined = factor_df.join(
        prices_df.select(["asset_id", "trade_date", ret_name]),
        on=["asset_id", "trade_date"],
        how="inner",
    ).drop_nulls()

    if joined.is_empty():
        return {"factor_name": body.factor_name, "n_groups": body.n_groups, "groups": []}

    n = body.n_groups
    # 全 Polars 向量化分组：rank → quintile，无 Python 循环
    labeled = (
        joined
        .with_columns(
            pl.col("value").rank(method="average").over("trade_date").alias("_rank"),
            pl.col("value").count().over("trade_date").alias("_n"),
        )
        .filter(pl.col("_n") >= n)
        .with_columns(
            ((pl.col("_rank") - 1) / pl.col("_n") * n)
            .cast(pl.Int32)
            .clip(0, n - 1)
            .add(1)
            .alias("quintile")  # Int32, not Utf8 — ensures numeric sort is correct
        )
        .drop(["_rank", "_n"])
    )

    if labeled.is_empty():
        return {"factor_name": body.factor_name, "n_groups": body.n_groups, "groups": [], "cumulative_returns": []}

    group_stats = (
        labeled.group_by("quintile")
        .agg([
            pl.col(ret_name).mean().alias("mean_return"),
            pl.col(ret_name).std().alias("std_return"),
            pl.col(ret_name).count().alias("count"),
        ])
        .sort("quintile")
    )

    # Cumulative returns per quintile over time
    cumulative = (
        labeled
        .group_by(["trade_date", "quintile"])
        .agg(pl.col(ret_name).mean().alias("mean_ret"))
        .sort("trade_date")
        .pivot(index="trade_date", columns="quintile", values="mean_ret")
        .sort("trade_date")
    )

    q_cols = [c for c in cumulative.columns if c != "trade_date"]
    cumulative = cumulative.with_columns([
        ((1 + pl.col(c)).cum_prod() - 1).alias(c)
        for c in q_cols
    ])

    cum_data = cumulative.to_dicts()
    cum_returns = [
        {("q" + k if k not in ("trade_date",) else k): v for k, v in row.items()}
        for row in cum_data
    ]

    return {
        "factor_name": body.factor_name,
        "horizon_days": body.horizon_days,
        "n_groups": n,
        "groups": group_stats.to_dicts(),
        "cumulative_returns": cum_returns,
    }


# ── Factor Correlation Matrix ─────────────────────────────────────────────────

class FactorCorrelationRequest(BaseModel):
    factor_names: list[str]
    feature_set_version: str
    start_date: str = ""
    end_date: str = ""


@router.post("/analytics/factor-correlation")
async def compute_factor_correlation(body: FactorCorrelationRequest, catalog: CatalogDep) -> dict:
    """计算多因子值之间的截面均值相关矩阵。"""
    import polars as pl

    if len(body.factor_names) < 2:
        return {"error": "需要至少 2 个因子", "factors": [], "matrix": []}
    if len(body.factor_names) > 50:
        return {"error": "因子数量不能超过 50 个", "factors": [], "matrix": []}

    start = body.start_date or "2023-01-01"
    end = body.end_date or "2025-12-31"

    # 使用参数化占位符，避免 SQL 注入
    in_placeholders = ",".join(["?" for _ in body.factor_names])
    params = [body.feature_set_version] + list(body.factor_names) + [start, end]

    df = catalog.query(
        f"SELECT asset_id, trade_date, factor_name, value FROM gold_factor_values "
        f"WHERE feature_set_version = ? AND factor_name IN ({in_placeholders}) "
        f"AND trade_date >= ? AND trade_date <= ?",
        params,
    )
    if df.is_empty():
        return {"error": "无数据", "factors": body.factor_names, "matrix": []}

    wide = df.pivot(index=["asset_id", "trade_date"], columns="factor_name", values="value")
    factor_cols = [c for c in body.factor_names if c in wide.columns]

    matrix = []
    for f1 in factor_cols:
        for f2 in factor_cols:
            if f1 == f2:
                corr: float | None = 1.0
            else:
                pair = wide.select([f1, f2]).drop_nulls()
                corr = float(pair[f1].corr(pair[f2])) if len(pair) >= 10 else None
            matrix.append({"factor_a": f1, "factor_b": f2, "correlation": corr})

    return {"factors": factor_cols, "matrix": matrix}


# ── Quick Factor Correlation (for StrategyBuilder hints) ─────────────────────

class QuickCorrelationBody(BaseModel):
    factors: list[str] = Field(..., min_length=2, max_length=50)
    feature_set_version: str = ""


@router.post("/correlation")
async def compute_quick_correlation(body: QuickCorrelationBody, catalog: CatalogDep) -> dict:
    """计算所选因子之间的 Pearson 相关矩阵，用于前端因子相关性提示。

    如果未指定 feature_set_version，自动取最新版本。
    标记 |r| > 0.7 为高相关。
    CPU-heavy computation runs in thread pool to avoid blocking the event loop.
    """
    import asyncio

    return await asyncio.to_thread(_compute_correlation_sync, body, catalog)


def _compute_correlation_sync(body: QuickCorrelationBody, catalog) -> dict:
    """Synchronous correlation computation (runs in thread pool)."""
    import polars as pl

    # Auto-detect latest version if not provided
    version = body.feature_set_version
    if not version:
        try:
            ver_df = catalog.query(
                "SELECT DISTINCT feature_set_version FROM gold_factor_values "
                "ORDER BY feature_set_version DESC LIMIT 1"
            )
            if not ver_df.is_empty():
                version = ver_df["feature_set_version"][0]
        except Exception:
            pass

    if not version:
        return {
            "correlation_matrix": {},
            "warnings": ["无因子数据版本，请先物化因子"],
        }

    # Fetch factor values
    in_placeholders = ",".join(["?" for _ in body.factors])
    params = [version] + list(body.factors)
    df = catalog.query(
        f"SELECT asset_id, trade_date, factor_name, value FROM gold_factor_values "
        f"WHERE feature_set_version = ? AND factor_name IN ({in_placeholders})",
        params,
    )

    if df.is_empty():
        return {
            "correlation_matrix": {},
            "warnings": ["未找到因子数据，请先物化所选因子"],
        }

    # Pivot to wide format and compute pairwise correlation
    wide = df.pivot(index=["asset_id", "trade_date"], columns="factor_name", values="value")
    factor_cols = [c for c in body.factors if c in wide.columns]

    if len(factor_cols) < 2:
        return {
            "correlation_matrix": {},
            "warnings": ["至少需要 2 个已物化的因子才能计算相关性"],
        }

    # Build correlation matrix
    matrix: dict[str, dict[str, float | None]] = {}
    warnings: list[str] = []

    for f1 in factor_cols:
        matrix[f1] = {}
        for f2 in factor_cols:
            if f1 == f2:
                matrix[f1][f2] = 1.0
            elif f2 in matrix and f1 in matrix[f2]:
                # Mirror value
                matrix[f1][f2] = matrix[f2][f1]
            else:
                pair = wide.select([f1, f2]).drop_nulls()
                corr = float(pair[f1].corr(pair[f2])) if len(pair) >= 10 else None
                matrix[f1][f2] = round(corr, 4) if corr is not None else None

                # High-correlation warning (only emit once per pair)
                if corr is not None and abs(corr) > 0.7 and f1 < f2:
                    warnings.append(
                        f"因子 {f1} 与 {f2} 高度相关 (r={corr:.2f})，建议移除其中一个以避免多重共线性"
                    )

    return {
        "correlation_matrix": matrix,
        "warnings": warnings,
    }


@router.get("/ic-leaderboard")
async def ic_leaderboard(catalog: CatalogDep, limit: int = 5) -> dict:
    """返回 IC 绝对值最高的 Top N 因子（用于 Dashboard 排行榜）。"""
    _ensure_ic_summary_table(catalog)
    try:
        df = catalog.query(
            "SELECT factor_name, ic_mean, icir, ic_positive_pct, n, "
            "window_start, window_end, updated_at "
            "FROM gold_factor_ic_summary "
            "WHERE ic_mean IS NOT NULL "
            "ORDER BY ABS(ic_mean) DESC LIMIT ?",
            [limit],
        )
        if df.is_empty():
            return {"items": [], "message": "尚无 IC 计算，请先在「因子研究」页面对因子计算 IC"}
        return {"items": df.to_dicts()}
    except Exception as exc:
        logger.warning("ic-leaderboard query failed: %s", exc)
        return {"items": [], "message": f"IC 汇总表读取失败: {exc}"}


@router.get("/ic-status")
async def factor_ic_status(
    catalog: CatalogDep,
    feature_set_version: str = "",
    threshold: float = 0.02,
    window_days: int = 20,
) -> dict:
    """批量检查因子 IC 状态，返回哪些因子 IC 低于阈值。"""
    _ensure_ic_summary_table(catalog)
    empty_resp = {
        "items": [],
        "threshold": threshold,
        "window_days": window_days,
        "message": "尚无 IC 计算，请先在「因子研究」页面对因子计算 IC",
    }
    try:
        # 汇总表按 factor_name 主键，每因子一行最新结果；按窗口过滤 updated_at
        df = catalog.query(
            "SELECT factor_name, ic_mean, icir, ic_positive_pct, n, updated_at "
            "FROM gold_factor_ic_summary "
            "WHERE updated_at >= CURRENT_TIMESTAMP - (? * INTERVAL '1 DAY') "
            "ORDER BY factor_name",
            [window_days],
        )
        if df.is_empty():
            # 窗口外还有数据时给出不同提示
            try:
                any_df = catalog.query("SELECT COUNT(*) AS cnt FROM gold_factor_ic_summary")
                if not any_df.is_empty() and any_df["cnt"][0] > 0:
                    empty_resp = {
                        **empty_resp,
                        "message": f"近 {window_days} 天内无 IC 更新（表中有历史数据，请扩大 window_days 或重新计算）",
                    }
            except Exception:
                pass
            return empty_resp

        items = []
        for row in df.to_dicts():
            fn = row["factor_name"]
            ic = float(row["ic_mean"]) if row["ic_mean"] is not None else 0.0
            is_alert = abs(ic) < threshold
            items.append({
                "factor_name": fn,
                "mean_ic": round(ic, 6),
                "ir": round(float(row["icir"]), 4) if row["icir"] is not None else None,
                "hit_rate": round(float(row["ic_positive_pct"]), 4) if row["ic_positive_pct"] is not None else None,
                "is_alert": is_alert,
                "alert_message": f"IC 绝对值 {abs(ic):.4f} < 阈值 {threshold}" if is_alert else None,
            })

        # Sort: alert factors first, then by abs(IC) ascending
        items.sort(key=lambda x: (not x["is_alert"], abs(x["mean_ic"])))

        return {"items": items, "threshold": threshold, "window_days": window_days}
    except Exception as exc:
        logger.warning("factor_ic_status failed: %s", exc)
        return {"items": [], "threshold": threshold, "window_days": window_days, "error": str(exc)}

# Cached static factor descriptions (Alpha360 描述层；动态部分不缓存)
_factors_cache: dict | None = None


def _invalidate_factors_cache() -> None:
    """自定义因子 CRUD 后清空静态描述缓存与已物化集合缓存，使下次请求重新装配。"""
    global _factors_cache, _materialized_cache
    _factors_cache = None
    with _materialized_cache_lock:
        _materialized_cache = None


def _load_description_map() -> dict[str, dict]:
    """静态描述层（Alpha158/360 中文名称、公式等），进程内缓存一次。"""
    global _factors_cache
    if _factors_cache is not None:
        return _factors_cache

    from cquant.factorlab.factor_descriptions import FactorDescriptionManager

    mgr = FactorDescriptionManager(db_path=":memory:")
    try:
        mgr.load_default_descriptions()
        df = mgr.read_all()
    except Exception:
        return {}
    finally:
        mgr.close()

    desc_map: dict[str, dict] = {}
    if not df.is_empty():
        for row in df.to_dicts():
            desc_map[row.get("factor_name", "")] = row
    _factors_cache = desc_map
    return desc_map


@router.get("/available")
async def list_available_factors(catalog: CatalogDep) -> dict:
    """返回可用因子 = 已物化 ∪ 可注册（内置注册表）∪ 自定义（C3 一致性）。

    - ``factors`` / ``items``: 可运行因子（cQuant 命名），带 ``status``
      （materialized=已有值 / ready=注册表可跑）和 ``is_custom`` 标注。
    - ``reference_factors``: Alpha360 描述层参考名单（仅描述用，status=reference，
      不进默认选择列表）。

    保持既有 ``factors`` + ``categories`` 响应 shape 兼容（前端按 name 渲染）。
    """
    builtin_by_name, materialized, custom_rows = _factor_catalog_state(catalog)
    custom_by_name = {r["name"]: r for r in custom_rows}
    runnable = set(builtin_by_name) | materialized | set(custom_by_name)

    desc_map = _load_description_map()

    def _desc_fields(name: str, fallback_desc: str) -> dict:
        d = desc_map.get(name, {})
        return {
            "label_zh": d.get("display_name") or name,
            "description": d.get("description") or fallback_desc,
            "formula": d.get("formula", ""),
            "economic_meaning": d.get("economic_meaning", ""),
            "use_case": d.get("use_case", ""),
        }

    items: list[dict] = []
    category_map: dict[str, list[str]] = {}
    for name in sorted(runnable):
        factor = builtin_by_name.get(name)
        custom_row = custom_by_name.get(name)
        if custom_row is not None:
            category = "自定义因子"
            fallback = custom_row.get("description") or f"自定义: {custom_row['expression'][:40]}"
        else:
            category = (desc_map.get(name, {}) or {}).get("category") or (
                factor.tags[0] if factor is not None and factor.tags else "未分类"
            )
            fallback = factor.description if factor is not None else ""
        item = {
            "name": name,
            "label_en": name,
            "category": category,
            "status": "materialized" if name in materialized else "ready",
            "is_custom": custom_row is not None,
            **_desc_fields(name, fallback),
        }
        items.append(item)
        category_map.setdefault(category, []).append(name)

    reference_factors: list[dict] = []
    for name in sorted(set(desc_map) - runnable):
        row = desc_map[name]
        reference_factors.append({
            "name": name,
            "label_zh": row.get("display_name", ""),
            "label_en": name,
            "category": row.get("category", "未分类"),
            "description": row.get("description", ""),
            "formula": row.get("formula", ""),
            "economic_meaning": row.get("economic_meaning", ""),
            "use_case": row.get("use_case", ""),
            "status": "reference",
            "is_custom": False,
        })

    categories = [
        {"name": cat, "label_zh": cat, "label_en": cat, "factors": names}
        for cat, names in sorted(category_map.items())
    ]
    return {
        "factors": items,
        "items": items,
        "categories": categories,
        "reference_factors": reference_factors,
        "total": len(items),
    }


# ── Factor Templates ─────────────────────────────────────────────────────────

@router.get("/templates")
async def list_factor_templates() -> dict:
    """List all preset factor templates."""
    from cquant.factorlab.factor_templates import list_templates
    items = list_templates()
    return {"items": items, "total": len(items)}


@router.get("/templates/{template_id}")
async def get_factor_template(template_id: str) -> dict:
    """Get a single preset factor template by id."""
    from cquant.factorlab.factor_templates import get_template
    tpl = get_template(template_id)
    if tpl is None:
        raise HTTPException(status_code=404, detail=f"Template '{template_id}' not found")
    return tpl


@router.get("/dsl/functions")
async def dsl_functions() -> dict:
    """Return available DSL functions for frontend autocomplete."""
    from cquant.factorlab.dsl_functions import get_function_descriptions, AVAILABLE_COLUMNS
    return {
        "functions": get_function_descriptions(),
        "columns": sorted(AVAILABLE_COLUMNS),
        "examples": [
            {"name": "5日动量截面排名", "expression": "rank(close / lag(close, 5) - 1)"},
            {"name": "20日波动率系数", "expression": "std(close, 20) / ma(close, 20)"},
            {"name": "量比", "expression": "ema(volume, 5) / ema(volume, 20)"},
            {"name": "20日威廉指标", "expression": "(close - min(low, 20)) / (max(high, 20) - min(low, 20))"},
            {"name": "价量相关性", "expression": "corr(close, volume, 10)"},
        ],
    }


@router.get("/ic-trend")
async def get_ic_trend(catalog: CatalogDep, days: int = 30) -> dict:
    """返回近 N 天每日 IC 均值趋势。"""
    try:
        df = catalog.query(
            "SELECT DATE(updated_at) as date, AVG(ABS(ic_mean)) as avg_ic "
            "FROM gold_factor_ic_summary "
            "WHERE updated_at >= CURRENT_DATE - ? * INTERVAL '1 DAY' "
            "GROUP BY DATE(updated_at) "
            "ORDER BY date",
            [days],
        )
        items = [
            {"date": str(r["date"]), "avg_ic": round(float(r["avg_ic"]), 6) if r["avg_ic"] else 0.0}
            for r in df.to_dicts()
        ] if not df.is_empty() else []
        return {"items": items, "days": days}
    except Exception as exc:
        logger.debug("get_ic_trend failed: %s", exc)
        return {"items": [], "days": days}
