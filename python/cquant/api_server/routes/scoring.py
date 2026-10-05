"""Cross-sectional scoring API routes."""

from __future__ import annotations

import json
import logging
import uuid
from datetime import datetime

from fastapi import APIRouter, BackgroundTasks
from pydantic import BaseModel
from typing import Literal

from cquant.api_server.deps import CatalogDep, run_job_async

logger = logging.getLogger(__name__)
router = APIRouter(prefix="/scoring", tags=["scoring"])

_SCORING_DDL = [
    """
    CREATE TABLE IF NOT EXISTS meta_scoring_runs (
        run_id        VARCHAR PRIMARY KEY,
        config_name   VARCHAR NOT NULL,
        config_json   VARCHAR NOT NULL,
        feature_set_version VARCHAR,
        start_date    DATE,
        end_date      DATE,
        status        VARCHAR NOT NULL DEFAULT 'pending',
        created_at    TIMESTAMP NOT NULL,
        completed_at  TIMESTAMP
    )
    """,
    """
    CREATE TABLE IF NOT EXISTS gold_cross_section_scores (
        run_id     VARCHAR NOT NULL,
        trade_date DATE NOT NULL,
        asset_id   VARCHAR NOT NULL,
        score      DOUBLE,
        rank       INTEGER,
        PRIMARY KEY (run_id, trade_date, asset_id)
    )
    """,
]
_tables_ensured = False


def _ensure_scoring_tables(catalog) -> None:
    global _tables_ensured
    if _tables_ensured:
        return
    for ddl in _SCORING_DDL:
        try:
            catalog.execute(ddl)
        except Exception as exc:
            logger.debug("_ensure_scoring_tables: %s", exc)
    _tables_ensured = True


class ScoringConfigBody(BaseModel):
    """POST /scoring/run 请求体。

    fill_null 枚举与 ScoringConfig 对齐（4 选项，A3）：
    median / mean / zero / risk_penalty。"exclude" 仅回测策略侧
    （multi_factor 的 missing_factor_strategy）支持，此处 422。
    penalty_per_missing 仅在 fill_null="risk_penalty" 时生效（默认 0.5）。
    """

    name: str
    factors: list[dict]  # [{factor_name, weight, direction}]
    feature_set_version: str
    start_date: str
    end_date: str
    neutralize: list[str] = []
    winsorize: list[float] = [0.01, 0.99]
    fill_null: Literal["median", "mean", "zero", "risk_penalty"] = "median"
    penalty_per_missing: float = 0.5


def _get_catalog():
    """Lazy import to avoid circular deps."""
    from cquant.api_server.deps import get_catalog
    return get_catalog()


def _run_scoring_task(run_id: str, body: ScoringConfigBody, catalog):
    """Background task to run scoring."""
    try:
        from cquant.factorlab.cross_section_scorer import (
            CrossSectionScorer, ScoringConfig, FactorWeight,
        )
        import polars as pl

        catalog.execute(
            "UPDATE meta_scoring_runs SET status = 'running' WHERE run_id = ?", [run_id]
        )

        factors = [
            FactorWeight(
                factor_name=f["factor_name"],
                weight=f.get("weight", 1.0),
                direction=f.get("direction", "long"),
            )
            for f in body.factors
        ]
        config = ScoringConfig(
            name=body.name,
            factors=factors,
            neutralize=body.neutralize,
            winsorize=tuple(body.winsorize),
            fill_null=body.fill_null,
            penalty_per_missing=body.penalty_per_missing,
        )

        scorer = CrossSectionScorer(catalog)
        result = scorer.score(config, body.feature_set_version, body.start_date, body.end_date)

        if not result.is_empty():
            scored = result.with_columns(pl.lit(run_id).alias("run_id")).select(
                ["run_id", "trade_date", "asset_id", "score", "rank"]
            )
            rows = scored.rows()
            assert not rows or len(rows[0]) == 5, (
                f"Column mismatch: {len(rows[0])} values vs 5 placeholders"
            )
            catalog.upsert(
                "gold_cross_section_scores",
                ["run_id", "trade_date", "asset_id", "score", "rank"],
                rows,
                ["run_id", "trade_date", "asset_id"],
            )

        catalog.execute(
            "UPDATE meta_scoring_runs SET status = 'completed', completed_at = ? WHERE run_id = ?",
            [datetime.now().isoformat(), run_id],
        )
    except Exception:
        catalog.execute(
            "UPDATE meta_scoring_runs SET status = 'error', completed_at = ? WHERE run_id = ?",
            [datetime.now().isoformat(), run_id],
        )
        logger.exception("Scoring task %s failed", run_id)


@router.post("/run")
async def run_scoring(
    body: ScoringConfigBody,
    background_tasks: BackgroundTasks,
    catalog: CatalogDep,
) -> dict:
    """提交截面打分任务。"""
    _ensure_scoring_tables(catalog)
    run_id = f"score_{uuid.uuid4().hex[:12]}"

    catalog.execute(
        "INSERT INTO meta_scoring_runs "
        "(run_id, config_name, config_json, feature_set_version, start_date, end_date, status, created_at) "
        "VALUES (?, ?, ?, ?, ?, ?, 'pending', ?)",
        [run_id, body.name, json.dumps(body.model_dump()), body.feature_set_version,
         body.start_date, body.end_date, datetime.now().isoformat()],
    )

    background_tasks.add_task(run_job_async, _run_scoring_task, run_id, body, catalog)
    return {"run_id": run_id, "status": "pending"}


@router.get("/results/{run_id}")
async def get_scoring_result(
    run_id: str,
    catalog: CatalogDep,
    offset: int = 0,
    limit: int = 50,
    trade_date: str = "",
) -> dict:
    """获取打分结果（支持分页）。"""

    run_df = catalog.query(
        "SELECT run_id, config_name, feature_set_version, start_date, end_date, "
        "status, created_at, completed_at FROM meta_scoring_runs WHERE run_id = ?",
        [run_id],
    )
    if run_df.is_empty():
        return {"error": "Run not found"}

    run_info = run_df.to_dicts()[0]

    # 总记录数
    count_df = catalog.query(
        "SELECT COUNT(*) as total FROM gold_cross_section_scores WHERE run_id = ?",
        [run_id],
    )
    total = count_df.to_dicts()[0]["total"] if not count_df.is_empty() else 0

    # 分页结果
    if trade_date:
        result_df = catalog.query(
            "SELECT trade_date, asset_id, score, rank FROM gold_cross_section_scores "
            "WHERE run_id = ? AND trade_date = ? ORDER BY trade_date DESC, rank ASC "
            "LIMIT ? OFFSET ?",
            [run_id, trade_date, limit, offset],
        )
    else:
        result_df = catalog.query(
            "SELECT trade_date, asset_id, score, rank FROM gold_cross_section_scores "
            "WHERE run_id = ? ORDER BY trade_date DESC, rank ASC "
            "LIMIT ? OFFSET ?",
            [run_id, limit, offset],
        )

    # 得分分布（用于前端直方图）
    dist_df = catalog.query(
        "SELECT score FROM gold_cross_section_scores WHERE run_id = ? AND score IS NOT NULL",
        [run_id],
    )
    score_bins: list[dict] = []
    if not dist_df.is_empty():
        import polars as pl
        scores = dist_df["score"].drop_nulls()
        if len(scores) > 0:
            try:
                hist = scores.hist(bin_count=20)
                score_bins = hist.to_dicts()
            except Exception as hist_exc:
                import logging
                logging.getLogger(__name__).debug("Histogram failed: %s", hist_exc)

    # 可用交易日列表
    dates_df = catalog.query(
        "SELECT DISTINCT trade_date FROM gold_cross_section_scores "
        "WHERE run_id = ? ORDER BY trade_date DESC LIMIT 50",
        [run_id],
    )
    available_dates = [
        r["trade_date"].isoformat() if hasattr(r["trade_date"], "isoformat") else str(r["trade_date"])
        for r in dates_df.to_dicts()
    ] if not dates_df.is_empty() else []

    return {
        "run": run_info,
        "results": result_df.to_dicts() if not result_df.is_empty() else [],
        "total": total,
        "offset": offset,
        "limit": limit,
        "score_distribution": score_bins,
        "available_dates": available_dates,
    }


@router.get("/snapshots")
async def list_scoring_snapshots(catalog: CatalogDep, limit: int = 20) -> dict:
    """列出已保存的打分快照。"""

    try:
        df = catalog.query(
            "SELECT run_id, config_name, feature_set_version, start_date, end_date, "
            "status, created_at, completed_at FROM meta_scoring_runs "
            "ORDER BY created_at DESC LIMIT ?",
            [limit],
        )
    except Exception:
        return {"items": []}

    return {"items": df.to_dicts() if not df.is_empty() else []}
