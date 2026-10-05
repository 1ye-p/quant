"""Cross-sectional factor scoring engine."""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from typing import Literal

import numpy as np
import polars as pl
import duckdb

logger = logging.getLogger(__name__)


@dataclass
class FactorWeight:
    """单因子权重配置。"""
    factor_name: str
    weight: float = 1.0
    direction: Literal["long", "short"] = "long"


@dataclass
class ScoringConfig:
    """截面打分配置。

    fill_null 枚举（4 选项，A3 与回测策略侧统一）：
      - "median" / "mean" / "zero"：按截面统计量/常数填充缺失值后参与打分
      - "risk_penalty"：不填充；合成分按 缺失因子数 × penalty_per_missing 扣减
        （公式与 ``backtest_vector/strategies/multi_factor.py`` 的
        ``missing_factor_strategy="risk_penalty"`` 同语义：null 不填充、
        按可用列计数 null、扣分作用于合成分）

    注意："exclude" 仅回测策略侧（multi_factor）支持——scorer 无持仓概念，
    不在本枚举内。两模块枚举文档互见：multi_factor.MissingFactorStrategy
    的 "fill_0"/"fill_median" 对应本枚举的 "zero"/"median"。
    """
    name: str
    factors: list[FactorWeight]
    neutralize: list[str] = field(default_factory=list)
    winsorize: tuple[float, float] = (0.01, 0.99)
    fill_null: Literal["median", "mean", "zero", "risk_penalty"] = "median"
    penalty_per_missing: float = 0.5


class CrossSectionScorer:
    """截面打分器。"""

    def __init__(self, catalog: duckdb.DuckDBPyConnection):
        self.catalog = catalog

    def score(
        self,
        config: ScoringConfig,
        feature_set_version: str,
        start_date: str,
        end_date: str,
    ) -> pl.DataFrame:
        """
        执行截面打分。

        Returns: pl.DataFrame with columns [trade_date, asset_id, score, rank]
        """
        factors_df = self._load_factors(config, feature_set_version, start_date, end_date)
        if factors_df.is_empty():
            return pl.DataFrame({"trade_date": [], "asset_id": [], "score": [], "rank": []})

        scored = self._normalize_cross_section(factors_df, config)
        scored = self._neutralize_factors(scored, config, start_date, end_date)
        if config.fill_null == "risk_penalty":
            # 中性化走 numpy 残差（null→NaN 往返），扣罚按 null 计数——
            # 此处还原为 null 以保持 risk_penalty 语义（z 分支已 fill_nan(0)，
            # 还原后的 NaN 只可能来自原始缺失）
            factor_cols = [
                fw.factor_name for fw in config.factors if fw.factor_name in scored.columns
            ]
            if factor_cols:
                scored = scored.with_columns(
                    [
                        pl.when(~pl.col(c).is_nan()).then(pl.col(c)).otherwise(None).alias(c)
                        for c in factor_cols
                    ]
                )
        scored = self._weighted_sum(scored, config.factors, config)
        scored = scored.with_columns(
            pl.col("score").over("trade_date").rank(descending=True).alias("rank")
        )
        return scored.select(["trade_date", "asset_id", "score", "rank"])

    def _load_factors(
        self, config: ScoringConfig, feature_set_version: str, start_date: str, end_date: str
    ) -> pl.DataFrame:
        """从 gold_factor_values 加载指定因子，pivot 为宽表。"""
        factor_names = [fw.factor_name for fw in config.factors]
        in_ph = ",".join(["?" for _ in factor_names])
        params = [feature_set_version] + factor_names + [start_date, end_date]
        df = self.catalog.query(
            f"SELECT asset_id, trade_date, factor_name, value "
            f"FROM gold_factor_values "
            f"WHERE feature_set_version = ? AND factor_name IN ({in_ph}) "
            f"AND trade_date >= ? AND trade_date <= ?",
            params,
        )
        if df.is_empty():
            return df
        return df.pivot(
            index=["asset_id", "trade_date"],
            columns="factor_name",
            values="value",
        )

    def _normalize_cross_section(self, df: pl.DataFrame, config: ScoringConfig) -> pl.DataFrame:
        """截面标准化：winsorize + fill_null + zscore。"""
        factor_cols = [fw.factor_name for fw in config.factors]
        factor_cols = [c for c in factor_cols if c in df.columns]
        if not factor_cols:
            return df

        lo, hi = config.winsorize
        for col in factor_cols:
            q_lo = pl.col(col).quantile(lo).over("trade_date")
            q_hi = pl.col(col).quantile(hi).over("trade_date")
            df = df.with_columns(
                pl.when(pl.col(col) < q_lo)
                .then(q_lo)
                .when(pl.col(col) > q_hi)
                .then(q_hi)
                .otherwise(pl.col(col))
                .alias(col)
            )

        if config.fill_null == "median":
            for col in factor_cols:
                median_val = pl.col(col).median().over("trade_date")
                df = df.with_columns(pl.col(col).fill_null(median_val))
        elif config.fill_null == "mean":
            for col in factor_cols:
                mean_val = pl.col(col).mean().over("trade_date")
                df = df.with_columns(pl.col(col).fill_null(mean_val))
        elif config.fill_null == "risk_penalty":
            # 与 multi_factor 的 risk_penalty 同语义：null 不填充，
            # 留给 _weighted_sum 按缺失数扣罚（winsorize/zscore 均跳过 null）
            pass
        else:
            df = df.with_columns([pl.col(c).fill_null(0.0) for c in factor_cols])

        for col in factor_cols:
            mean_val = pl.col(col).mean().over("trade_date")
            std_val = pl.col(col).std().over("trade_date")
            z_expr = (pl.col(col) - mean_val) / std_val
            if config.fill_null == "risk_penalty":
                # 对齐 multi_factor：std=0 的常数列 z 分为 NaN→0（null 保持 null）
                z_expr = z_expr.fill_nan(0.0)
            df = df.with_columns(z_expr.alias(col))

        return df

    def _neutralize_factors(
        self, df: pl.DataFrame, config: ScoringConfig, start_date: str, end_date: str
    ) -> pl.DataFrame:
        """中性化：截面回归残差法。

        对每个截面日期，将因子值对中性化变量（市值/行业）做 OLS 回归，
        取残差作为中性化后的因子值。

        Parameters
        ----------
        df : pl.DataFrame
            包含 [asset_id, trade_date, factor1, factor2, ...] 的宽表。
        config : ScoringConfig
            包含 neutralize 列表，如 ["market_cap", "industry"]。
        start_date, end_date : str
            数据日期范围，用于加载中性化数据。

        Returns
        -------
        pl.DataFrame
            中性化后的因子 DataFrame，原列被残差替换。
        """
        if not config.neutralize:
            return df

        factor_cols = [fw.factor_name for fw in config.factors]
        factor_cols = [c for c in factor_cols if c in df.columns]
        if not factor_cols:
            return df

        # Load neutralization data
        neutral_data = self._load_neutralization_data(
            config.neutralize, start_date, end_date
        )
        if neutral_data.is_empty():
            logger.warning(
                "No neutralization data loaded; skipping neutralization for: %s",
                config.neutralize,
            )
            return df

        # Join neutralization data to factor DataFrame
        df = df.join(neutral_data, on=["asset_id", "trade_date"], how="left")

        # Build X column names for regression
        x_cols = []
        if "market_cap" in config.neutralize and "ln_mktcap" in df.columns:
            x_cols.append("ln_mktcap")
        if "industry" in config.neutralize:
            industry_cols = [c for c in df.columns if c.startswith("industry_")]
            x_cols.extend(industry_cols)

        if not x_cols:
            logger.warning(
                "Neutralization columns not found after join; skipping. "
                "Available columns: %s",
                df.columns,
            )
            # Drop helper columns and return
            drop_cols = [
                c
                for c in df.columns
                if c.startswith("industry_") or c in ("ln_mktcap",)
            ]
            return df.drop(drop_cols) if drop_cols else df

        # For each date cross-section, regress each factor on X and take residuals
        dates = df["trade_date"].unique().sort().to_list()
        neutralized_parts = []

        for trade_date in dates:
            mask = df["trade_date"] == trade_date
            section = df.filter(mask)

            # Build X matrix
            X_parts = []
            for xc in x_cols:
                col_vals = section[xc].to_numpy().astype(float)
                X_parts.append(col_vals)

            X = np.column_stack(X_parts) if X_parts else None

            if X is None or X.shape[0] < X.shape[1] + 2:
                # Not enough observations for regression; keep original
                neutralized_parts.append(section)
                continue

            # Add intercept
            X_with_intercept = np.column_stack([np.ones(X.shape[0]), X])

            # Compute projection matrix: residuals = (I - X(X'X)^-1 X') y
            try:
                pinv = np.linalg.pinv(X_with_intercept)
                hat_matrix = X_with_intercept @ pinv
                residual_proj = np.eye(hat_matrix.shape[0]) - hat_matrix
            except np.linalg.LinAlgError:
                logger.warning(
                    "Singular matrix for date %s; skipping neutralization", trade_date
                )
                neutralized_parts.append(section)
                continue

            # Apply residual projection to each factor column
            section_np = {}
            for col in factor_cols:
                y = section[col].to_numpy().astype(float)
                # Replace NaN with 0 for regression, then restore NaN mask
                nan_mask = np.isnan(y)
                y_clean = np.where(nan_mask, 0.0, y)
                residuals = residual_proj @ y_clean
                residuals[nan_mask] = np.nan
                section_np[col] = residuals

            # Reconstruct section DataFrame with neutralized values
            section = section.with_columns(
                [pl.Series(name=col, values=section_np[col]) for col in factor_cols]
            )
            neutralized_parts.append(section)

        if not neutralized_parts:
            return df

        result = pl.concat(neutralized_parts)

        # Drop helper columns
        drop_cols = [c for c in result.columns if c.startswith("industry_")]
        if "ln_mktcap" in result.columns:
            drop_cols.append("ln_mktcap")
        if drop_cols:
            result = result.drop(drop_cols)

        return result

    def _load_neutralization_data(
        self, neutralize: list[str], start_date: str, end_date: str
    ) -> pl.DataFrame:
        """加载中性化所需数据（市值、行业）。

        Returns: pl.DataFrame with columns [asset_id, trade_date, ln_mktcap, industry_*]
        """
        parts: list[pl.DataFrame] = []

        # Load market cap from silver_valuation_daily (trade_date-aligned, PIT-correct)
        if "market_cap" in neutralize:
            try:
                mktcap_df = self.catalog.query(
                    """
                    SELECT asset_id, trade_date, market_cap
                    FROM silver_valuation_daily
                    WHERE trade_date >= ? AND trade_date <= ?
                      AND market_cap IS NOT NULL AND market_cap > 0
                    """,
                    [start_date, end_date],
                )
                if not mktcap_df.is_empty():
                    mktcap_df = mktcap_df.with_columns(
                        pl.col("market_cap").log().alias("ln_mktcap")
                    ).drop("market_cap")
                    parts.append(mktcap_df)
            except Exception as exc:
                logger.warning("Failed to load market_cap from silver_valuation_daily: %s", exc)

        # Load industry from silver_assets
        if "industry" in neutralize:
            try:
                industry_df = self.catalog.query(
                    """
                    SELECT asset_id, industry
                    FROM silver_assets
                    WHERE industry IS NOT NULL AND industry != ''
                    """
                )
                if not industry_df.is_empty():
                    # One-hot encode industry
                    industries = industry_df["industry"].unique().sort().to_list()
                    for ind in industries:
                        safe_name = ind.replace(" ", "_").replace("/", "_")
                        industry_df = industry_df.with_columns(
                            (pl.col("industry") == ind).cast(pl.Float64).alias(f"industry_{safe_name}")
                        )
                    industry_df = industry_df.drop("industry")

                    # Get distinct trade_dates from factors
                    dates_df = self.catalog.query(
                        "SELECT DISTINCT trade_date FROM gold_factor_values "
                        "WHERE trade_date >= ? AND trade_date <= ? ORDER BY trade_date",
                        [start_date, end_date],
                    )
                    if not dates_df.is_empty():
                        # Cross join to create per-date industry mapping
                        industry_df = industry_df.join(dates_df, how="cross")
                        parts.append(industry_df)
            except Exception as exc:
                logger.warning("Failed to load industry from silver_assets: %s", exc)

        if not parts:
            return pl.DataFrame()

        # Merge all neutralization data
        result = parts[0]
        for part in parts[1:]:
            result = result.join(part, on=["asset_id", "trade_date"], how="full", coalesce=True)

        return result

    def _weighted_sum(
        self,
        df: pl.DataFrame,
        factors: list[FactorWeight],
        config: ScoringConfig | None = None,
    ) -> pl.DataFrame:
        """加权求和得到综合得分。

        当 ``config.fill_null == "risk_penalty"`` 时按 multi_factor 同语义合成：
        null 分项计 0（sum_horizontal 忽略 null），再按可用因子列的 null 数
        × ``penalty_per_missing`` 扣减合成分——扣分作用于合成分，不填充原值。
        """
        if config is not None and config.fill_null == "risk_penalty":
            available = [fw for fw in factors if fw.factor_name in df.columns]
            if not available:
                return df.with_columns(pl.lit(0.0).alias("score"))
            composite = pl.sum_horizontal(
                [
                    pl.col(fw.factor_name)
                    * fw.weight
                    * (-1.0 if fw.direction == "short" else 1.0)
                    for fw in available
                ]
            )
            null_count = pl.sum_horizontal(
                [pl.col(fw.factor_name).is_null().cast(pl.Int32) for fw in available]
            )
            return df.with_columns(
                (composite - null_count * config.penalty_per_missing).alias("score")
            )

        expr = None
        for fw in factors:
            if fw.factor_name not in df.columns:
                continue
            sign = -1.0 if fw.direction == "short" else 1.0
            term = pl.col(fw.factor_name) * fw.weight * sign
            expr = term if expr is None else expr + term
        if expr is None:
            df = df.with_columns(pl.lit(0.0).alias("score"))
        else:
            df = df.with_columns(expr.alias("score"))
        return df
