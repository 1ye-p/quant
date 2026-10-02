"""Custom HTTP indicator source adapter (P3-3).

Bridges :func:`~cquant.datahub.pipelines.indicator_sources.http_guard.guarded_fetch`
into the standard refresh adapter protocol
(``fetch(key, start, end) -> pl.DataFrame``). Window/import/log plumbing is
owned by ``refresh.py`` — this adapter only renders the date sequence and
normalizes guarded rows into the standard frame
``[trade_date: date, value: float]`` ascending.

Error policy:

- :class:`GuardError` from the guard layer propagates **unchanged** (the
  per-indicator try in ``run_external_indicator_refresh`` catches it and
  records ``last_error`` with the stage-tagged message — stages must not be
  rewritten here).
- Empty results *after* window filtering raise :class:`IndicatorFetchError`
  with an explicit message (same convention as the akshare day-loop).
"""

from __future__ import annotations

from datetime import date, datetime, timedelta

import polars as pl

from cquant.datahub.pipelines.indicator_sources.adapters import (
    IndicatorFetchError,
)
from cquant.datahub.pipelines.indicator_sources.http_config import CustomHTTPConfig
from cquant.datahub.pipelines.indicator_sources.http_guard import guarded_fetch

__all__ = ["HTTPIndicatorAdapter"]


def _parse_trade_date(raw: object) -> date:
    if isinstance(raw, datetime):
        return raw.date()
    if isinstance(raw, date):
        return raw.date()
    if isinstance(raw, str):
        s = raw.strip()
        if len(s) == 8 and s.isdigit():  # '20250601'
            return date(int(s[:4]), int(s[4:6]), int(s[6:8]))
        if len(s) >= 10:
            return date.fromisoformat(s[:10])
    raise IndicatorFetchError(
        f"custom_http: unparseable trade_date value {raw!r} "
        f"({type(raw).__name__}) — check extraction.field_map"
    )


def _weekday_sequence(start: date, end: date) -> list[date]:
    """Mon–Fri sequence in [start, end] — same weekday approximation as the
    akshare szse day-loop (holidays are skipped naturally by empty returns)."""
    out: list[date] = []
    d = start
    while d <= end:
        if d.weekday() < 5:
            out.append(d)
        d += timedelta(days=1)
    return out


class HTTPIndicatorAdapter:
    """Standard refresh-protocol adapter over a :class:`CustomHTTPConfig`."""

    def __init__(self, name: str, cfg: CustomHTTPConfig):
        self.name = name  # 'custom_http:' + source_name（refresh 日志/目录行用）
        self.cfg = cfg

    def fetch(self, indicator_key: str, start: date, end: date) -> pl.DataFrame:
        if self.cfg.date_param_style == "none":
            # 单请求：记录自带日期 → 窗口过滤
            rows = guarded_fetch(self.cfg, [])
            parsed = [
                {"trade_date": _parse_trade_date(r["trade_date"]), "value": r["value"]}
                for r in rows
            ]
            in_window = [r for r in parsed if start <= r["trade_date"] <= end]
            if not in_window:
                raise IndicatorFetchError(
                    f"{self.name}: guarded_fetch returned {len(parsed)} row(s) but "
                    f"none fall inside the requested window [{start}, {end}] "
                    f"(source dates: "
                    f"{sorted(r['trade_date'] for r in parsed)[:5]!r}...)"
                )
            data = in_window
        else:
            dates = _weekday_sequence(start, end)
            rows = guarded_fetch(self.cfg, dates)  # 逐日渲染；>400 由 guard 拦
            data = [
                {"trade_date": _parse_trade_date(r["trade_date"]), "value": r["value"]}
                for r in rows
            ]
            if not data:
                raise IndicatorFetchError(
                    f"{self.name}: no rows returned for {len(dates)} weekday "
                    f"requests in [{start}, {end}] (all days empty?)"
                )

        df = pl.DataFrame(
            {
                "trade_date": [r["trade_date"] for r in data],
                "value": [float(r["value"]) for r in data],
            },
            schema={"trade_date": pl.Date, "value": pl.Float64},
        )
        return df.sort("trade_date")
