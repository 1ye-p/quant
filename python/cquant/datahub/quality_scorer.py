"""cquant.datahub.quality_scorer — Data quality scoring engine.

Evaluates market data quality across three dimensions:
- Completeness: missing values, gaps in trading days
- Consistency: outlier detection, cross-field validation
- Freshness: staleness of data relative to expected update schedule

Usage::

    from cquant.datahub.quality_scorer import DataQualityScorer

    scorer = DataQualityScorer(catalog)
    report = scorer.score("silver_daily", "2024-01-01", "2025-06-30")
    print(report.overall_score)  # 0.0 - 1.0
    print(report.completeness_score)
    print(report.consistency_score)
    print(report.freshness_score)
"""

from __future__ import annotations

import logging
import math
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from typing import Any

logger = logging.getLogger(__name__)


@dataclass
class CompletenessReport:
    """Completeness quality metrics."""
    score: float  # 0.0 - 1.0
    total_expected_cells: int
    total_missing_cells: int
    missing_pct: float
    trading_day_gaps: int  # number of unexpected gaps in trading calendar
    gap_details: list[str] = field(default_factory=list)
    column_missing: dict[str, float] = field(default_factory=dict)  # col -> missing_pct


@dataclass
class ConsistencyReport:
    """Consistency quality metrics."""
    score: float  # 0.0 - 1.0
    outlier_count: int
    outlier_pct: float
    negative_price_count: int
    zero_volume_count: int
    high_low_inverted: int  # high < low
    ohlc_violations: int  # open/close outside high/low range
    cross_field_issues: list[str] = field(default_factory=list)


@dataclass
class FreshnessReport:
    """Freshness quality metrics."""
    score: float  # 0.0 - 1.0
    latest_date: str
    expected_latest_date: str
    lag_days: int
    staleness_warning: bool


@dataclass
class DataQualityReport:
    """Combined data quality report."""
    overall_score: float
    completeness: CompletenessReport
    consistency: ConsistencyReport
    freshness: FreshnessReport
    table_name: str
    date_range: tuple[str, str]
    scored_at: str = ""

    def to_dict(self) -> dict[str, Any]:
        """Serialize to API-friendly dict."""
        return {
            "overall_score": round(self.overall_score, 4),
            "table_name": self.table_name,
            "date_range": list(self.date_range),
            "scored_at": self.scored_at,
            "completeness": {
                "score": round(self.completeness.score, 4),
                "total_expected_cells": self.completeness.total_expected_cells,
                "total_missing_cells": self.completeness.total_missing_cells,
                "missing_pct": round(self.completeness.missing_pct, 4),
                "trading_day_gaps": self.completeness.trading_day_gaps,
                "gap_details": self.completeness.gap_details[:20],
                "column_missing": {k: round(v, 4) for k, v in self.completeness.column_missing.items()},
            },
            "consistency": {
                "score": round(self.consistency.score, 4),
                "outlier_count": self.consistency.outlier_count,
                "outlier_pct": round(self.consistency.outlier_pct, 4),
                "negative_price_count": self.consistency.negative_price_count,
                "zero_volume_count": self.consistency.zero_volume_count,
                "high_low_inverted": self.consistency.high_low_inverted,
                "ohlc_violations": self.consistency.ohlc_violations,
                "cross_field_issues": self.consistency.cross_field_issues[:20],
            },
            "freshness": {
                "score": round(self.freshness.score, 4),
                "latest_date": self.freshness.latest_date,
                "expected_latest_date": self.freshness.expected_latest_date,
                "lag_days": self.freshness.lag_days,
                "staleness_warning": self.freshness.staleness_warning,
            },
        }


# Weights for overall score
_WEIGHTS = {"completeness": 0.45, "consistency": 0.35, "freshness": 0.20}


class QualityQueryError(RuntimeError):
    """Raised when the quality scorer cannot query the target table.

    Previously this failure mode returned an all-zero report, which was
    indistinguishable from a legitimately terrible dataset. Callers should
    surface this error (e.g. HTTP 500) instead of presenting fake zeros.
    """


class DataQualityScorer:
    """Score data quality for market data tables.

    Parameters
    ----------
    catalog : Any
        A DuckDB Catalog instance with ``query()`` and ``execute()`` methods.
    """

    def __init__(self, catalog: Any):
        self.catalog = catalog

    def score(
        self,
        table_name: str = "silver_prices_1d",
        start_date: str = "2024-01-01",
        end_date: str = "2025-12-31",
    ) -> DataQualityReport:
        """Run full data quality scoring.

        Parameters
        ----------
        table_name : str
            DuckDB table to score.
        start_date, end_date : str
            Date range to evaluate (YYYY-MM-DD).

        Returns
        -------
        DataQualityReport
        """
        # Load data
        try:
            df = self.catalog.query(
                f"SELECT * FROM {table_name} "
                f"WHERE trade_date >= ? AND trade_date <= ? "
                f"ORDER BY trade_date, asset_id",
                [start_date, end_date],
            )
        except Exception as e:
            logger.error("Failed to query %s: %s", table_name, e)
            raise QualityQueryError(
                f"Failed to query table '{table_name}' for quality scoring: {e}"
            ) from e

        if df.is_empty():
            empty_comp = CompletenessReport(0, 0, 0, 1.0, 0)
            empty_cons = ConsistencyReport(0, 0, 1.0, 0, 0, 0, 0)
            empty_fresh = FreshnessReport(0, "", "", 999, True)
            return DataQualityReport(0, empty_comp, empty_cons, empty_fresh, table_name, (start_date, end_date))

        comp = self._score_completeness(df, start_date, end_date)
        cons = self._score_consistency(df)
        fresh = self._score_freshness(df, end_date)

        overall = (
            _WEIGHTS["completeness"] * comp.score
            + _WEIGHTS["consistency"] * cons.score
            + _WEIGHTS["freshness"] * fresh.score
        )

        return DataQualityReport(
            overall_score=overall,
            completeness=comp,
            consistency=cons,
            freshness=fresh,
            table_name=table_name,
            date_range=(start_date, end_date),
            scored_at=datetime.utcnow().isoformat(),
        )

    def _score_completeness(
        self, df: Any, start_date: str, end_date: str
    ) -> CompletenessReport:
        """Score data completeness."""
        import polars as pl

        total_rows = len(df)
        columns = df.columns

        # Count missing per column
        col_missing: dict[str, float] = {}
        total_missing = 0
        for col in columns:
            null_count = df[col].null_count()
            pct = null_count / total_rows if total_rows > 0 else 0.0
            col_missing[col] = pct
            total_missing += null_count

        total_cells = total_rows * len(columns)
        missing_pct = total_missing / total_cells if total_cells > 0 else 1.0

        # Check trading day gaps
        if "trade_date" in df.columns:
            dates = sorted(df["trade_date"].unique().to_list())
            gap_count = 0
            gap_details: list[str] = []
            for i in range(1, len(dates)):
                d1 = _parse_date(str(dates[i - 1]))
                d2 = _parse_date(str(dates[i]))
                if d1 and d2:
                    diff = (d2 - d1).days
                    # Allow weekends (2 days) and holidays (up to 5 days)
                    if diff > 5:
                        gap_count += 1
                        if len(gap_details) < 20:
                            gap_details.append(f"{dates[i-1]} -> {dates[i]} ({diff} days)")
        else:
            gap_count = 0
            gap_details = []

        # Score: penalize missing data and gaps
        score = max(0.0, 1.0 - missing_pct - gap_count * 0.02)
        score = min(1.0, max(0.0, score))

        return CompletenessReport(
            score=score,
            total_expected_cells=total_cells,
            total_missing_cells=total_missing,
            missing_pct=missing_pct,
            trading_day_gaps=gap_count,
            gap_details=gap_details,
            column_missing=col_missing,
        )

    def _score_consistency(self, df: Any) -> ConsistencyReport:
        """Score data consistency (outliers, OHLC violations)."""
        total_rows = len(df)
        issues: list[str] = []
        outlier_count = 0
        neg_price = 0
        zero_vol = 0
        hl_inverted = 0
        ohlc_violations = 0

        cols = set(df.columns)

        # Negative prices
        if "close" in cols:
            neg_price = len(df.filter(df["close"] < 0))
            if neg_price > 0:
                issues.append(f"{neg_price} rows with negative close price")

        # Zero volume
        if "volume" in cols:
            zero_vol = len(df.filter(df["volume"] == 0))
            # Some zero-volume days are legitimate (holidays), so don't penalize heavily

        # High < Low
        if "high" in cols and "low" in cols:
            hl_inverted = len(df.filter(df["high"] < df["low"]))
            if hl_inverted > 0:
                issues.append(f"{hl_inverted} rows where high < low")

        # Open/Close outside High/Low range
        if all(c in cols for c in ["open", "close", "high", "low"]):
            oc_outside = len(df.filter(
                (df["open"] > df["high"]) | (df["open"] < df["low"])
                | (df["close"] > df["high"]) | (df["close"] < df["low"])
            ))
            ohlc_violations = oc_outside
            if oc_outside > 0:
                issues.append(f"{oc_outside} rows where open/close outside high-low range")

        # Outlier detection: returns > 5 std from mean
        if "close" in cols and total_rows > 10:
            try:
                returns = df["close"].pct_change().drop_nulls()
                if len(returns) > 0:
                    mean_ret = returns.mean()
                    std_ret = returns.std()
                    if std_ret and std_ret > 0:
                        outliers = returns.filter((returns - mean_ret).abs() > 5 * std_ret)
                        outlier_count = len(outliers)
            except Exception:
                pass

        outlier_pct = outlier_count / total_rows if total_rows > 0 else 0.0

        # Score
        penalty = (
            neg_price * 0.01
            + hl_inverted * 0.005
            + ohlc_violations * 0.003
            + outlier_pct * 0.5
        )
        score = max(0.0, 1.0 - penalty)
        score = min(1.0, max(0.0, score))

        return ConsistencyReport(
            score=score,
            outlier_count=outlier_count,
            outlier_pct=outlier_pct,
            negative_price_count=neg_price,
            zero_volume_count=zero_vol,
            high_low_inverted=hl_inverted,
            ohlc_violations=ohlc_violations,
            cross_field_issues=issues,
        )

    def _score_freshness(self, df: Any, expected_end: str) -> FreshnessReport:
        """Score data freshness."""
        latest_date = ""
        if "trade_date" in df.columns:
            dates = df["trade_date"].sort()
            latest_date = str(dates.tail(1)[0]) if len(dates) > 0 else ""

        try:
            latest_dt = _parse_date(latest_date)
            expected_dt = _parse_date(expected_end)
            if latest_dt and expected_dt:
                lag = (expected_dt - latest_dt).days
            else:
                lag = 999
        except Exception:
            lag = 999

        staleness_warning = lag > 7
        # Score: 1.0 if fresh, degrades with lag
        if lag <= 1:
            score = 1.0
        elif lag <= 3:
            score = 0.9
        elif lag <= 7:
            score = 0.7
        elif lag <= 30:
            score = 0.4
        else:
            score = max(0.0, 1.0 - lag / 365)

        return FreshnessReport(
            score=score,
            latest_date=latest_date,
            expected_latest_date=expected_end,
            lag_days=lag,
            staleness_warning=staleness_warning,
        )


def _parse_date(date_str: str):
    """Parse a date string."""
    from datetime import datetime as dt
    try:
        cleaned = str(date_str)[:10].replace("/", "-")
        return dt.strptime(cleaned, "%Y-%m-%d").date()
    except (ValueError, TypeError):
        return None
