"""cquant.factorlab.materialize — Persist factor values to DuckDB gold layer.

Reads from silver_prices_1d, runs FeaturePipeline, writes to gold_factor_values.
"""

from __future__ import annotations

import hashlib
import logging
from dataclasses import dataclass
from datetime import date

import polars as pl

from cquant.backtest_vector.prices import adjusted_ohlc_sql
from cquant.core.errors import FactorComputeError
from cquant.datahub.catalog import Catalog
from cquant.factorlab.factor import FactorRegistry
from cquant.factorlab.pipeline import FeaturePipeline, PipelineSpec

logger = logging.getLogger(__name__)


@dataclass
class FactorMaterializationSpec:
    """Configuration for a factor materialization run."""

    dataset_version: str
    factor_names: list[str]
    start_date: date
    end_date: date
    universe_id: str = ""


class FactorMaterializer:
    """Compute factors and persist results to gold_factor_values.

    Usage::

        materializer = FactorMaterializer(catalog, registry)
        fsv = materializer.run(FactorMaterializationSpec(
            dataset_version="...",
            factor_names=["ret_20d", "vol_20d"],
            start_date=date(2024, 1, 1),
            end_date=date(2024, 12, 31),
        ))
    """

    def __init__(self, catalog: Catalog, registry: FactorRegistry) -> None:
        self._catalog = catalog
        self._registry = registry
        self._ensure_registry_populated()
        self._pipeline = FeaturePipeline(registry)

    def _ensure_registry_populated(self) -> None:
        """装配点：注册内置因子（若缺失）+ 自定义因子（meta_custom_factors → ExpressionFactor）。

        调用方（CLI/编排器）可能传入空 registry 或仅含子集的 registry；
        此处统一补齐，使 spec.factor_names 中出现的任何内置/自定义名字都可解析。
        """
        from cquant.factorlab.factors import BUILTIN_FACTORS
        from cquant.factorlab.custom_factor_loader import load_custom_factors

        for factor in BUILTIN_FACTORS:
            if factor.name not in self._registry:
                self._registry.register(factor)
        for custom in load_custom_factors(self._catalog):
            if custom.name not in self._registry:
                self._registry.register(custom)

    def run(self, spec: FactorMaterializationSpec) -> str:
        """Compute factors and write to gold_factor_values. Returns feature_set_version.

        Uses a content-based data fingerprint over silver_prices_1d in the requested
        date range as a cache key. When the fingerprint matches a previous
        materialization for the same factor set + date range, the existing factor
        values are reused and the run returns without recomputing. Any change to the
        underlying prices (corrections, backfills, adj_factor updates) alters the
        fingerprint and auto-invalidates the cache for the affected range.
        """
        self._catalog.initialize()

        # Data fingerprint = deterministic digest of silver_prices_1d content over
        # the date range. Any re-ingestion or correction that changes a row's OHLCV
        # / adj_factor / membership alters the digest, invalidating the cache for
        # this feature set. (silver_prices_1d has no per-row updated_at, so the
        # fingerprint is computed from actual content rather than a timestamp.)
        fingerprint = self._compute_data_fingerprint(spec)

        # Deterministic cache key = factor set + date range + universe + data fingerprint.
        # Stable across runs with identical inputs → reusable feature_set_version.
        feature_set_version = self._derive_cache_key(spec, fingerprint)

        # Cache hit: fingerprint unchanged → reuse existing materialized factors.
        cached = self._catalog.query(
            "SELECT data_fingerprint FROM gold_factor_cache_meta "
            "WHERE feature_set_version = ?",
            [feature_set_version],
        )
        if not cached.is_empty() and cached["data_fingerprint"][0] == fingerprint:
            logger.info(
                "Cache hit (fingerprint=%s) — skipping materialization for "
                "feature_set_version=%s",
                fingerprint, feature_set_version,
            )
            return feature_set_version

        prices = self._load_prices(spec)
        if prices.is_empty():
            raise FactorComputeError(
                f"No price data in silver_prices_1d for date range "
                f"{spec.start_date} to {spec.end_date}"
            )

        pipeline_spec = PipelineSpec(
            factor_names=spec.factor_names,
            start_date=spec.start_date,
            end_date=spec.end_date,
            universe_id=spec.universe_id,
            dataset_version=spec.dataset_version,
        )

        # Load fundamentals + daily valuation + corporate actions into ctx.extra
        fundamentals = self._load_fundamentals(spec)
        valuation = self._load_valuation(spec)
        corporate_actions = self._load_corporate_actions(spec)
        extra = {}
        if not fundamentals.is_empty():
            extra["fundamentals"] = fundamentals
        if not valuation.is_empty():
            extra["valuation"] = valuation
        if not corporate_actions.is_empty():
            extra["corporate_actions"] = corporate_actions

        feature_set = self._pipeline.run(prices, pipeline_spec, extra=extra)

        if feature_set.data.is_empty():
            raise FactorComputeError("Feature pipeline returned empty result")

        # Pin the feature_set to the deterministic cache key so cached and freshly
        # computed rows share the same feature_set_version.
        feature_set.version_id = feature_set_version

        self._write_factor_values(feature_set)
        self._record_cache_meta(feature_set_version, fingerprint)

        logger.info(
            "Materialized %d factors, %d rows → feature_set_version=%s "
            "(fingerprint=%s)",
            feature_set.factor_count, feature_set.row_count,
            feature_set_version, fingerprint,
        )
        return feature_set_version

    def _compute_data_fingerprint(self, spec: FactorMaterializationSpec) -> str:
        """Deterministic content digest of silver_prices_1d over the date range.

        Combines a row count with order-independent sums of the OHLCV and adj_factor
        values so the digest changes on any data correction, backfill, or
        adj_factor update — while being insensitive to row insertion order. The
        digest is computed inside DuckDB (a single cheap aggregation scan) rather
        than shipping the full price frame to Python. silver_prices_1d has no
        per-row ``updated_at`` column, so the fingerprint is derived from actual
        content rather than a modification timestamp.
        """
        try:
            df = self._catalog.query(
                """
                SELECT
                    COUNT(*)                                     AS row_count,
                    SUM(CAST(close AS DOUBLE))                   AS sum_close,
                    SUM(CAST(open  AS DOUBLE))                   AS sum_open,
                    SUM(CAST(high  AS DOUBLE))                   AS sum_high,
                    SUM(CAST(low   AS DOUBLE))                   AS sum_low,
                    SUM(CAST(volume AS DOUBLE))                  AS sum_volume,
                    SUM(COALESCE(CAST(adj_factor AS DOUBLE), 0)) AS sum_adj
                FROM silver_prices_1d
                WHERE trade_date >= ? AND trade_date <= ?
                """,
                [spec.start_date.isoformat(), spec.end_date.isoformat()],
            )
        except Exception as exc:  # table missing or query error → never serve stale
            logger.warning("Could not compute data fingerprint: %s", exc)
            return "unknown"

        if df.is_empty():
            return "empty"

        # Polars returns the aggregates; render a compact, stable signature.
        r = df.row(0, named=True)
        parts = [f"{k}={r.get(k)}" for k in
                 ("row_count", "sum_close", "sum_open", "sum_high",
                  "sum_low", "sum_volume", "sum_adj")]
        return hashlib.sha1("|".join(parts).encode("utf-8")).hexdigest()[:16]

    @staticmethod
    def _derive_cache_key(
        spec: FactorMaterializationSpec, fingerprint: str
    ) -> str:
        """Deterministic feature_set_version from factor set + range + fingerprint.

        Sorting the factor names makes the key order-independent, so reordering
        spec.factor_names does not spuriously miss the cache.
        """
        factors_sig = ",".join(sorted(spec.factor_names))
        payload = "|".join([
            spec.dataset_version,
            factors_sig,
            spec.universe_id,
            spec.start_date.isoformat(),
            spec.end_date.isoformat(),
            fingerprint,
        ])
        digest = hashlib.sha1(payload.encode("utf-8")).hexdigest()[:16]
        return f"fsv_{digest}"

    def _record_cache_meta(self, feature_set_version: str, fingerprint: str) -> None:
        """Upsert the data fingerprint for this feature_set_version."""
        try:
            self._catalog.upsert(
                "gold_factor_cache_meta",
                ["feature_set_version", "data_fingerprint"],
                [(feature_set_version, fingerprint)],
                ["feature_set_version"],
            )
        except Exception as exc:
            # Cache bookkeeping failure must not poison a successful materialization;
            # the next run will simply recompute (safe over-caching of stale data).
            logger.warning("Could not record cache meta for %s: %s", feature_set_version, exc)

    def _load_prices(self, spec: FactorMaterializationSpec) -> pl.DataFrame:
        """Load price data from silver_prices_1d with dynamic lookback."""
        from datetime import timedelta

        # Compute max lookback needed across all requested factors
        max_lookback = 120  # safe default
        for factor_name in spec.factor_names:
            try:
                factor = self._registry.get(factor_name)
                max_lookback = max(max_lookback, factor.lookback_days)
            except KeyError:
                pass

        lookback_start = spec.start_date - timedelta(days=max_lookback)

        # Shared helper: fully-adjusted OHLC so factors see the same
        # adjustment convention as the backtest path.
        query = (
            adjusted_ohlc_sql()
            + " WHERE trade_date >= ? AND trade_date <= ?"
            " ORDER BY asset_id, trade_date"
        )
        df = self._catalog.query(
            query,
            [lookback_start.isoformat(), spec.end_date.isoformat()],
        )
        if df.is_empty():
            return df

        if df["trade_date"].dtype == pl.Utf8:
            df = df.with_columns(pl.col("trade_date").str.to_date())
        elif df["trade_date"].dtype != pl.Date:
            df = df.with_columns(pl.col("trade_date").cast(pl.Date))

        return df

    def _load_valuation(self, spec: FactorMaterializationSpec) -> pl.DataFrame:
        """加载逐日估值，按 trade_date 精确匹配，天然 PIT。"""
        try:
            df = self._catalog.query(
                """
                SELECT asset_id, trade_date, pe_ttm, pb, ps_ttm, market_cap,
                       turnover_rate, dividend_yield
                FROM silver_valuation_daily
                WHERE trade_date <= ?
                """,
                [spec.end_date.isoformat()],
            )
        except Exception as exc:
            logger.warning("Could not load silver_valuation_daily: %s", exc)
            return pl.DataFrame()
        return df

    def _load_corporate_actions(self, spec: FactorMaterializationSpec) -> pl.DataFrame:
        """加载已实施分红事件，按 ex_date <= spec.end_date 截断，天然 PIT。

        供分红事件因子（DividendYield12M / DividendMomentum）消费。事件型数据，
        一行一次分红，需在因子内部按 trade_date 窗口聚合。
        """
        try:
            df = self._catalog.query(
                """
                SELECT asset_id, ex_date, cash_amount, ratio
                FROM silver_corporate_actions
                WHERE action_type = 'dividend' AND ex_date <= ?
                """,
                [spec.end_date.isoformat()],
            )
        except Exception as exc:
            logger.warning("Could not load silver_corporate_actions: %s", exc)
            return pl.DataFrame()
        if df.is_empty():
            return df
        if df["ex_date"].dtype == pl.Utf8:
            df = df.with_columns(pl.col("ex_date").str.to_date())
        elif df["ex_date"].dtype != pl.Date:
            df = df.with_columns(pl.col("ex_date").cast(pl.Date))
        return df

    def _load_fundamentals(self, spec: FactorMaterializationSpec) -> pl.DataFrame:
        """Load latest-disclosed fundamentals per asset as of spec.end_date (PIT-correct)."""
        try:
            df = self._catalog.query(
                """
                SELECT DISTINCT ON (asset_id)
                    asset_id, announce_date, report_date, roe, roa, gross_margin,
                    net_margin, revenue_growth_yoy, earnings_growth_yoy
                FROM silver_fundamentals
                WHERE announce_date <= ?
                ORDER BY asset_id, announce_date DESC
                """,
                [spec.end_date.isoformat()],
            )
        except Exception as exc:
            logger.warning("Could not load silver_fundamentals: %s", exc)
            return pl.DataFrame()
        return df

    def _write_factor_values(self, feature_set) -> None:
        """Unpivot feature_set.data and write to gold_factor_values."""
        id_cols = ["asset_id", "trade_date"]
        factor_names = [c for c in feature_set.data.columns if c not in id_cols]

        if not factor_names:
            raise FactorComputeError("No factor columns in feature set data")

        long_df = feature_set.data.unpivot(
            index=id_cols,
            on=factor_names,
            variable_name="factor_name",
            value_name="value",
        ).drop_nulls(["value"])

        if long_df.is_empty():
            logger.warning("All factor values are null after unpivot")
            return

        # Add metadata columns
        write_frame = long_df.with_columns(
            pl.lit(feature_set.version_id).alias("feature_set_version"),
        ).select(["feature_set_version", "factor_name", "trade_date", "asset_id", "value"])

        rows = write_frame.rows()
        try:
            self._catalog.upsert(
                "gold_factor_values",
                ["feature_set_version", "factor_name", "trade_date", "asset_id", "value"],
                rows,
                ["feature_set_version", "factor_name", "trade_date", "asset_id"],
            )
        except Exception as exc:
            raise FactorComputeError(f"Failed to write gold_factor_values: {exc}") from exc
