"""Factor analysis tool for AI Advisor -- evaluate factor performance using factorlab."""

from __future__ import annotations

import asyncio
import logging
from typing import Any

from cquant.ai_advisor.tools.base import AdvisorTool, ToolContext, ToolResult

logger = logging.getLogger(__name__)


class FactorAnalysisTool(AdvisorTool):
    name = "factor_analysis"
    description = (
        "Evaluate factor performance using IC, IC IR, and turnover metrics. "
        "Accepts factor_name (required) and optionally dataset_version, start_date, end_date. "
        "Returns factor evaluation summary."
    )

    async def invoke(self, args: dict[str, Any], ctx: ToolContext) -> ToolResult:
        factor_name = str(args.get("factor_name", "")).strip()
        if not factor_name:
            return ToolResult(success=False, content="FactorAnalysisTool requires `factor_name`.")

        dataset_version = str(args.get("dataset_version", "")).strip()
        start_date = str(args.get("start_date", "")).strip()
        end_date = str(args.get("end_date", "")).strip()

        def _evaluate() -> str:
            parts: list[str] = []

            # 1. Query factor metadata from catalog
            try:
                conditions = ["factor_name = ?"]
                params: list[Any] = [factor_name]
                if dataset_version:
                    conditions.append("dataset_version = ?")
                    params.append(dataset_version)
                if start_date:
                    conditions.append("trade_date >= ?")
                    params.append(start_date)
                if end_date:
                    conditions.append("trade_date <= ?")
                    params.append(end_date)

                where = " AND ".join(conditions)
                query = (
                    f"SELECT factor_name, trade_date, asset_id, factor_value "
                    f"FROM silver_factors WHERE {where} "
                    f"ORDER BY trade_date LIMIT 5000"
                )
                df = ctx.catalog.query(query, params)

                if df.is_empty():
                    return f"No factor data found for '{factor_name}'" + (
                        f" (dataset={dataset_version})" if dataset_version else ""
                    ) + "."

                parts.append(f"## Factor: {factor_name}")
                n_dates = df["trade_date"].n_unique() if "trade_date" in df.columns else 0
                n_assets = df["asset_id"].n_unique() if "asset_id" in df.columns else 0
                parts.append(f"- Records: {df.height}")
                parts.append(f"- Dates: {n_dates}")
                parts.append(f"- Assets: {n_assets}")

                # Descriptive stats
                if "factor_value" in df.columns:
                    fv = df["factor_value"]
                    parts.append(f"- Mean: {fv.mean():.6f}")
                    parts.append(f"- Std:  {fv.std():.6f}")
                    parts.append(f"- Min:  {fv.min():.6f}")
                    parts.append(f"- Max:  {fv.max():.6f}")

            except Exception as exc:
                logger.warning("Factor data query failed: %s", exc)
                return f"Failed to query factor data: {exc}"

            # 2. Compute IC if we have returns available
            try:
                ret_query = (
                    "SELECT asset_id, trade_date, fwd_return_1d "
                    "FROM silver_factors WHERE factor_name = ? "
                    "AND fwd_return_1d IS NOT NULL "
                    "ORDER BY trade_date LIMIT 5000"
                )
                ret_df = ctx.catalog.query(ret_query, [factor_name])
                if not ret_df.is_empty() and "fwd_return_1d" in ret_df.columns:
                    from cquant.factorlab.evaluation import FactorEvaluator

                    evaluator = FactorEvaluator(
                        factor_col="factor_value",
                        return_col="fwd_return_1d",
                        method="rank",
                        factor_name=factor_name,
                    )
                    merged = df.join(ret_df, on=["asset_id", "trade_date"], how="inner")
                    if not merged.is_empty():
                        ic_df = evaluator.ic_series(df, ret_df)
                        if not ic_df.is_empty():
                            mean_ic = ic_df["ic"].mean()
                            std_ic = ic_df["ic"].std()
                            ic_ir = mean_ic / std_ic if std_ic and std_ic > 0 else 0.0
                            parts.append("\n## IC Analysis (Rank)")
                            parts.append(f"- Mean IC: {mean_ic:.4f}")
                            parts.append(f"- IC Std:  {std_ic:.4f}")
                            parts.append(f"- IC IR:   {ic_ir:.4f}")
                            parts.append(f"- IC > 0 ratio: {(ic_df['ic'] > 0).mean():.2%}")
            except Exception as exc:
                logger.warning("IC computation failed for %s: %s", factor_name, exc)
                parts.append(f"\n_IC computation unavailable: {exc}_")

            # 3. Top/bottom quantile spread
            try:
                if "factor_value" in df.columns and "trade_date" in df.columns:
                    import polars as pl
                    quantile_df = (
                        df.with_columns(
                            pl.col("factor_value").qcut(5, labels=["Q1", "Q2", "Q3", "Q4", "Q5"]).alias("quantile")
                        )
                        .group_by("quantile")
                        .agg(pl.col("factor_value").mean().alias("mean_factor"))
                        .sort("quantile")
                    )
                    if not quantile_df.is_empty():
                        parts.append("\n## Factor Distribution by Quantile")
                        for row in quantile_df.to_dicts():
                            parts.append(f"- {row['quantile']}: mean={row['mean_factor']:.6f}")
            except Exception as exc:
                logger.debug("Quantile analysis skipped: %s", exc)

            return "\n".join(parts) if parts else f"Factor '{factor_name}' has no analyzable data."

        content = await asyncio.to_thread(_evaluate)
        return ToolResult(success=True, content=content, metadata={"factor_name": factor_name})
