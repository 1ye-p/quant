"""CSV importer for external (macro / market-level) indicators.

Loads user-provided CSV files into ``silver_external_indicators`` with:
- column mapping (csv column -> schema column)
- asset code normalization (3 input forms -> canonical ``EXCHANGE:SYMBOL``)
- ``__MARKET__`` sentinel default when no asset column is mapped (decision D1-A)
- trading-calendar alignment warnings (non-blocking)
- ``available_date`` rule A (same day) / B (next trading day, conservative default)

PIT contract: ``available_date`` is the only gate the downstream loader
(:func:`cquant.datahub.external_loader.load_external_series`) trusts, so this
importer must never write an ``available_date`` earlier than the true
publication date implied by the chosen rule.
"""

from __future__ import annotations

import logging
import re
from dataclasses import dataclass, field
from datetime import date, datetime, timedelta
from pathlib import Path

import polars as pl

from cquant.core.enums import Exchange
from cquant.datahub.catalog import Catalog
from cquant.market_calendar.service import MarketCalendarService

logger = logging.getLogger(__name__)

MARKET_SENTINEL = "__MARKET__"

_INDICATOR_KEY_RE = re.compile(r"^[a-z_0-9]+$")

# Exchange prefixes accepted in normalized form
_NORM_EXCHANGES = ("SSE:", "SZSE:", "BSE:", "HKEX:", "NYSE:", "NASDAQ:")

_UPSERT_SQL = """
INSERT INTO silver_external_indicators
    (source, indicator_key, asset_id, trade_date, value, available_date)
VALUES (?, ?, ?, ?, ?, ?)
ON CONFLICT (source, indicator_key, asset_id, trade_date)
DO UPDATE SET value = excluded.value,
              available_date = excluded.available_date,
              updated_at = NOW()
"""


def normalize_asset_id(raw: str) -> str:
    """Normalize an asset code to canonical ``EXCHANGE:SYMBOL`` form.

    Accepted input forms:
    - ``600000.SH`` / ``000001.SZ`` (Tushare style, via existing ``_to_asset_id``)
    - ``sh600000`` / ``sz000001`` (common lowercase prefixed style)
    - ``600000`` / ``000001`` (bare symbol — exchange inferred from leading digit)

    Already-normalized ids (``SSE:600000``) pass through unchanged.
    """
    s = str(raw).strip()
    if not s:
        return s
    upper = s.upper()
    # Pass-through: already normalized (SSE:600000) or foreign tickers with colon
    if ":" in s:
        return upper
    # Form 2: sh600000 / sz000001
    if upper.startswith(("SH", "SZ")) and len(upper) == 8 and upper[2:].isdigit():
        exchange = "SSE" if upper.startswith("SH") else "SZSE"
        return f"{exchange}:{upper[2:]}"
    # Form 1 + bare symbol: reuse tushare _to_asset_id for suffix form
    if "." in s:
        # local import to avoid connector dependency at module import time
        from cquant.datahub.connectors.tushare_connector import _to_asset_id

        return _to_asset_id(s)
    # Form 3: bare 6-digit A-share code — infer exchange from leading digit
    if s.isdigit() and len(s) == 6:
        if s.startswith("6"):
            return f"SSE:{s}"
        if s.startswith(("0", "3")):
            return f"SZSE:{s}"
        return f"BSE:{s}"
    # Unknown format — return as-is, caller records a warning
    return s


@dataclass
class ImportConfig:
    """Configuration for one CSV import run."""

    source: str
    indicator_key: str
    column_map: dict[str, str]  # csv_col -> {trade_date, value, asset_id?}
    available_date_rule: str = "B"  # A = trade_date / B = trade_date + 1 trading day


@dataclass
class ImportReport:
    total: int = 0
    inserted: int = 0
    deduped: int = 0
    skipped: int = 0
    skipped_reasons: list[str] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)

    def as_dict(self) -> dict:
        return {
            "total": self.total,
            "inserted": self.inserted,
            "deduped": self.deduped,
            "skipped": self.skipped,
            "skipped_reasons": self.skipped_reasons,
            "warnings": self.warnings,
        }


def preview_csv(path: str | Path, limit: int = 10) -> dict:
    """Return columns + first N rows of a CSV for the import wizard."""
    df = pl.read_csv(path, try_parse_dates=True, n_rows=limit)
    return {
        "columns": df.columns,
        "rows": [
            {k: (str(v) if not isinstance(v, (int, float, bool, type(None))) else v)
             for k, v in row.items()}
            for row in df.rows(named=True)
        ],
        "total_rows": pl.scan_csv(path).select(pl.len()).collect().item(),
    }


class ExternalIndicatorImporter:
    """Imports CSV files into ``silver_external_indicators``."""

    def __init__(self, catalog: Catalog) -> None:
        self.catalog = catalog
        self._calendar: MarketCalendarService | None = None
        try:
            self._calendar = MarketCalendarService()
        except Exception as exc:  # calendar optional — degrade to warnings
            logger.warning("MarketCalendar unavailable, calendar checks disabled: %s", exc)

    # ── public API ──────────────────────────────────────────────────────────

    def import_csv(self, path: str | Path, config: ImportConfig) -> ImportReport:
        df = pl.read_csv(path, try_parse_dates=True)
        return self.import_frame(df, config)

    def import_frame(self, df: pl.DataFrame, config: ImportConfig) -> ImportReport:
        report = ImportReport()
        self._validate_config(config, report)

        mapped = self._apply_column_map(df, config, report)
        if mapped is None or mapped.height == 0:
            report.warnings.append("CSV 无有效数据行")
            return report

        rows, seen_keys = [], set()
        for i in range(mapped.height):
            report.total += 1
            row = mapped.row(i, named=True)
            parsed = self._parse_row(row, i, config, report)
            if parsed is None:
                continue
            key = (parsed[2], parsed[3])  # asset_id + trade_date dedup within file
            if key in seen_keys:
                report.deduped += 1
                continue
            seen_keys.add(key)
            rows.append(parsed)

        if rows:
            before = self._existing_count(config, rows)
            self.catalog.executemany(_UPSERT_SQL, rows)
            report.inserted = len(rows) - before if before else len(rows)
            report.deduped += before
        return report

    def preview(self, path: str | Path, limit: int = 10) -> dict:
        """Return columns + first N rows for the import wizard."""
        return preview_csv(path, limit)

    # ── internals ───────────────────────────────────────────────────────────

    def _validate_config(self, config: ImportConfig, report: ImportReport) -> None:
        if not _INDICATOR_KEY_RE.match(config.indicator_key or ""):
            raise ValueError(
                f"indicator_key 非法：'{config.indicator_key}'，仅允许小写字母/数字/下划线 [a-z_0-9]+"
            )
        if config.available_date_rule not in ("A", "B"):
            raise ValueError(
                f"available_date_rule 非法：'{config.available_date_rule}'，仅支持 A（当天可查）或 B（次日可查）"
            )
        target_cols = set(config.column_map.values())
        missing = {"trade_date", "value"} - target_cols
        if missing:
            raise ValueError(f"column_map 缺少必需映射：{sorted(missing)}")

    def _apply_column_map(
        self, df: pl.DataFrame, config: ImportConfig, report: ImportReport
    ) -> pl.DataFrame | None:
        rename: dict[str, str] = {}
        for csv_col, schema_col in config.column_map.items():
            if csv_col not in df.columns:
                raise ValueError(f"CSV 中不存在列：'{csv_col}'（现有列：{df.columns}）")
            rename[csv_col] = schema_col
        mapped = df.rename(rename)
        if "asset_id" not in mapped.columns:
            mapped = mapped.with_columns(
                pl.lit(MARKET_SENTINEL).alias("asset_id")
            )
        keep = ["trade_date", "value", "asset_id"]
        return mapped.select(keep)

    def _parse_row(
        self, row: dict, row_idx: int, config: ImportConfig, report: ImportReport
    ) -> tuple | None:
        line_no = row_idx + 2  # +1 header, +1 1-based
        # trade_date — empty cells (None) and unparseable strings are skipped
        # per-row, never raised to the caller (a single bad cell must not 500
        # the whole import).
        raw_date = row.get("trade_date")
        if raw_date is None or str(raw_date).strip() == "":
            report.skipped += 1
            report.skipped_reasons.append(f"第 {line_no} 行：trade_date 为空，已跳过")
            return None
        try:
            trade_date = self._to_date(raw_date)
        except Exception:
            report.skipped += 1
            report.skipped_reasons.append(f"第 {line_no} 行：trade_date 无法解析：{raw_date!r}")
            return None
        # value — empty cells are skipped too (a NULL value with a valid date
        # is almost always a data error in user CSVs, not a deliberate NA)
        raw_value = row.get("value")
        if raw_value is None or str(raw_value).strip() == "":
            report.skipped += 1
            report.skipped_reasons.append(f"第 {line_no} 行：value 为空，已跳过")
            return None
        try:
            value = float(raw_value)
        except (TypeError, ValueError):
            report.skipped += 1
            report.skipped_reasons.append(f"第 {line_no} 行：value 无法转为数值：{raw_value!r}")
            return None
        # asset_id
        raw_asset = row.get("asset_id")
        if raw_asset is None or str(raw_asset).strip() == "":
            asset_id = MARKET_SENTINEL
        else:
            asset_id = normalize_asset_id(raw_asset)
            if ":" not in asset_id:
                report.warnings.append(
                    f"第 {line_no} 行：无法识别代码格式 '{raw_asset}'，按原样写入"
                )
            elif asset_id.startswith("BSE:8"):
                # 88xxxx codes are Shenwan sector indices, not BSE stocks.
                # Still normalize to BSE (non-blocking) but make the mismatch visible.
                report.warnings.append(
                    f"第 {line_no} 行：裸 6 位代码 '{raw_asset}' 首位为 8，"
                    f"疑似行业指数，请使用带交易所前缀形式（如 SSE:881101）；本次按 BSE 归一写入"
                )
        # calendar check (warning only)
        if self._calendar is not None and trade_date is not None:
            try:
                if not self._calendar.is_trading_day(trade_date, Exchange.SSE):
                    report.warnings.append(
                        f"第 {line_no} 行：{trade_date} 非 A 股交易日（已写入，请核对）"
                    )
            except Exception:
                pass
        # available_date
        available_date = self._available_date(trade_date, config.available_date_rule)
        return (config.source, config.indicator_key, asset_id, trade_date, value, available_date)

    def _available_date(self, trade_date: date, rule: str) -> date:
        if rule == "A" or self._calendar is None:
            return trade_date
        try:
            return self._calendar.next_trading_day(trade_date, Exchange.SSE, 1)
        except Exception:
            return trade_date + timedelta(days=1)

    def _existing_count(self, config: ImportConfig, rows: list[tuple]) -> int:
        """Count rows whose (asset_id, trade_date) already exist for this key."""
        try:
            existing = self.catalog.query(
                "SELECT asset_id, trade_date FROM silver_external_indicators "
                "WHERE source = ? AND indicator_key = ?",
                [config.source, config.indicator_key],
            )
            if existing.height == 0:
                return 0
            have = {(r["asset_id"], r["trade_date"]) for r in existing.rows(named=True)}
            return sum(1 for r in rows if (r[2], r[3]) in have)
        except Exception as exc:
            logger.debug("_existing_count failed: %s", exc)
            return 0

    @staticmethod
    def _to_date(v) -> date | None:
        if v is None:
            return None
        if isinstance(v, date):
            return v
        s = str(v).strip()
        clean = s.replace("/", "-").replace(".", "-")
        for fmt in ("%Y-%m-%d", "%Y%m%d", "%Y-%m-%d %H:%M:%S"):
            try:
                return datetime.strptime(clean, fmt).date()
            except ValueError:
                continue
        raise ValueError(f"unparseable date: {v!r}")
