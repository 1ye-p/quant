"""Unified external indicator refresh task (P2-3).

编排：due 判定 → 三档源解析（pinned → candidates 首个 ready）→
增量（max(trade_date)+1 起，含 5 日重叠吃近端修订）/回填窗口 →
adapter.fetch → import_frame → refresh_log + 目录状态写回。

锚定日一律 ``silver_prices_1d max(trade_date)``，禁用 CURRENT_DATE
（B2 规则：价格库停更不得产生假刷新窗口）。逐指标独立 try：单指标失败
只写 error 状态/日志，其余继续。同源指标间串行 ``inter_source_delay``
（akshare 免费源礼貌；测试注入 0）。
"""

from __future__ import annotations

import logging
import time
from dataclasses import dataclass, field
from datetime import date, datetime, timedelta, timezone

import polars as pl

from cquant.datahub.catalog import Catalog
from cquant.datahub.pipelines.external_indicator_importer import (
    ExternalIndicatorImporter,
    ImportConfig,
)
from cquant.datahub.pipelines.indicator_catalog import mark_refresh_result
from cquant.datahub.pipelines.indicator_sources.adapters import (
    AkshareIndicatorAdapter,  # re-export convenience  # noqa: F401
    IndicatorFetchError,
    IndicatorSourceAdapterProtocol,
    TushareIndicatorAdapter,
)
from cquant.datahub.pipelines.indicator_sources.builtin_registry import (
    BUILTIN_BY_KEY,
    source_ready,
)

logger = logging.getLogger(__name__)

#: 增量窗口重叠天数（fetch start = max(trade_date) − 4 → 5 日含端点）
OVERLAP_DAYS = 5

#: due 判定阈值（自然日）：weekly 与月频容差口径一致（≥6 / ≥25 天）
_DUE_DAYS = {"daily": 0, "weekly": 6, "monthly": 25}


class NoSourceReadyError(Exception):
    """candidates 中无任何就绪源（且无有效 pinned）。"""


# ── 结果形状 ─────────────────────────────────────────────────────────────────


@dataclass
class IndicatorRefreshResult:
    indicator_key: str
    source: str | None = None
    status: str = "ok"  # 'ok' | 'error' | 'skipped'
    rows_fetched: int = 0
    rows_upserted: int = 0
    error: str | None = None
    range_start: date | None = None
    range_end: date | None = None

    def as_dict(self) -> dict:
        return {
            "indicator_key": self.indicator_key,
            "source": self.source,
            "status": self.status,
            "rows_fetched": self.rows_fetched,
            "rows_upserted": self.rows_upserted,
            "error": self.error,
            "range_start": str(self.range_start) if self.range_start else None,
            "range_end": str(self.range_end) if self.range_end else None,
        }


@dataclass
class RefreshSummary:
    trigger: str
    started_at: datetime
    finished_at: datetime | None = None
    results: list[IndicatorRefreshResult] = field(default_factory=list)

    @property
    def ok_count(self) -> int:
        return sum(1 for r in self.results if r.status == "ok")

    @property
    def error_count(self) -> int:
        return sum(1 for r in self.results if r.status == "error")

    def as_dict(self) -> dict:
        return {
            "trigger": self.trigger,
            "started_at": self.started_at.isoformat(),
            "finished_at": self.finished_at.isoformat() if self.finished_at else None,
            "results": [r.as_dict() for r in self.results],
            "ok": self.ok_count,
            "error": self.error_count,
        }


# ── 三档源解析 ───────────────────────────────────────────────────────────────


def resolve_source(
    entry: dict, adapters: dict[str, IndicatorSourceAdapterProtocol]
) -> IndicatorSourceAdapterProtocol:
    """三档优先级：

    1. ``pinned_source`` — 仅 ``source_type='builtin'`` 行有效（carried
       finding：csv/custom_http 行的残留 pinned 一律忽略，按 candidates 解析）
    2. registry 条目 candidates 顺序中第一个 ready 的源
       （tushare ready = ``tushare_ready()``；akshare 恒 ready）

    无可用源抛 :class:`NoSourceReadyError`。
    """
    key = entry.get("indicator_key")
    if entry.get("source_type") == "builtin" and entry.get("pinned_source"):
        pinned = entry["pinned_source"]
        adapter = adapters.get(pinned)
        if adapter is not None and source_ready(pinned):
            return adapter

    reg = BUILTIN_BY_KEY.get(key)
    if reg is None:
        raise NoSourceReadyError(
            f"{key!r}: not in builtin registry — P2 refresh only covers builtin rows"
        )
    for name in reg.candidates:
        adapter = adapters.get(name)
        if adapter is not None and source_ready(name):
            return adapter
    raise NoSourceReadyError(
        f"{key!r}: no ready source among candidates {list(reg.candidates)}"
    )


# ── 刷新编排 ─────────────────────────────────────────────────────────────────


class _RefreshImporter(ExternalIndicatorImporter):
    """继承 CSV importer 的落库路径，但目录写穿降级为 no-op：

    基类 ``_write_through_catalog`` 会 upsert ``source_type='csv'``，把
    builtin 目录行改成 csv（后续刷新会被当成非 builtin 跳过）。目录行的
    运行时字段（last_status/source_name/last_refresh_at）由本模块
    ``mark_refresh_result`` 以实际解析源写回，不经过基类写穿。
    """

    def _write_through_catalog(self, config: ImportConfig) -> None:  # noqa: D102
        return None


_FRAME_COLUMN_MAP = {"trade_date": "trade_date", "value": "value"}


def run_external_indicator_refresh(
    catalog: Catalog,
    keys: list[str] | None = None,
    backfill: bool = False,
    trigger: str = "scheduled",
    adapters: dict | None = None,
    inter_source_delay: float = 0.5,
) -> RefreshSummary:
    """统一刷新入口（enable 端点按本签名调用）。

    - ``keys`` 显式给定则逐个处理（不检查 enabled/due，但仍只刷 builtin 行）；
      否则枚举目录中 enabled 且 due 的行（daily 每日；weekly ≥6 天；monthly ≥25 天）。
    - ``backfill=True`` 强制回填窗口（backfill_start..锚定日）；否则增量
      （max(trade_date)−4 .. 锚定日，5 日重叠吃近端修订；无数据自动转回填）。
    - 逐指标独立 try；同源指标间 ``inter_source_delay`` 串行间隔。
    """
    started = datetime.now(timezone.utc)
    summary = RefreshSummary(trigger=trigger, started_at=started)
    if adapters is None:
        adapters = {
            "akshare": AkshareIndicatorAdapter(),
            "tushare": TushareIndicatorAdapter(),
        }

    rows_by_key = {
        r["indicator_key"]: r
        for r in catalog.query(
            "SELECT indicator_key, display_name, source_type, pinned_source, "
            "available_date_rule, frequency, backfill_start, enabled, "
            "last_refresh_at, last_status "
            "FROM silver_external_indicator_catalog"
        ).rows(named=True)
    }

    if keys is not None:
        targets = [k for k in keys]
    else:
        now = datetime.now(timezone.utc)
        targets = [
            r["indicator_key"]
            for r in rows_by_key.values()
            if r.get("enabled") and _is_due(r, now)
        ]

    anchor = _anchor_date(catalog)
    importer = _RefreshImporter(catalog)
    last_source: str | None = None

    for key in targets:
        entry = rows_by_key.get(key)
        if entry is None:
            summary.results.append(
                IndicatorRefreshResult(key, status="error", error="not in catalog")
            )
            continue
        if entry.get("source_type") != "builtin":
            # P2 只刷 builtin；csv/custom_http（含 L2 custom_http，P3）跳过
            summary.results.append(
                IndicatorRefreshResult(
                    key, status="skipped", error="non-builtin source_type"
                )
            )
            continue

        run_id = None
        result = IndicatorRefreshResult(key)
        try:
            if anchor is None:
                raise NoSourceReadyError(
                    "anchor unavailable: silver_prices_1d is empty — refusing "
                    "to guess a refresh window"
                )
            adapter = resolve_source(entry, adapters)
            result.source = adapter.name
            start, end = _window(catalog, entry, anchor, backfill)
            result.range_start, result.range_end = start, end

            run_id = _log_start(catalog, key, adapter.name, trigger, start, end)
            if adapter.name == last_source and inter_source_delay > 0:
                time.sleep(inter_source_delay)
            last_source = adapter.name

            df = adapter.fetch(key, start, end)
            result.rows_fetched = df.height
            rule = entry.get("available_date_rule") or "B"
            report = importer.import_frame(
                df,
                ImportConfig(
                    source=adapter.name,
                    indicator_key=key,
                    column_map=dict(_FRAME_COLUMN_MAP),
                    available_date_rule=rule,
                ),
            )
            result.rows_upserted = report.inserted
            result.status = "ok"
            _log_finish(catalog, run_id, "ok", result)
            mark_refresh_result(
                catalog, key, status="ok", source_name=adapter.name
            )
        except IndicatorFetchError as exc:
            result.status, result.error = "error", str(exc)
            _finish_error(catalog, run_id, key, result, exc)
        except Exception as exc:  # noqa: BLE001 — 逐指标容错，其余继续
            result.status, result.error = "error", f"{type(exc).__name__}: {exc}"
            logger.exception("refresh failed for %s", key)
            _finish_error(catalog, run_id, key, result, exc)
        summary.results.append(result)

    summary.finished_at = datetime.now(timezone.utc)
    return summary


# ── due / 窗口 / 日志 ────────────────────────────────────────────────────────


def _is_due(row: dict, now: datetime) -> bool:
    freq = str(row.get("frequency") or "daily").lower()
    if freq == "daily":
        return True
    lr = row.get("last_refresh_at")
    if lr is None:
        return True
    if isinstance(lr, str):
        lr = datetime.fromisoformat(lr)
    if lr.tzinfo is None:
        lr = lr.replace(tzinfo=timezone.utc)
    return (now - lr).days >= _DUE_DAYS.get(freq, 0)


def _anchor_date(catalog: Catalog) -> date | None:
    return catalog.query(
        "SELECT max(trade_date) AS a FROM silver_prices_1d"
    ).item(0, "a")


def _window(
    catalog: Catalog, entry: dict, anchor: date, backfill: bool
) -> tuple[date, date]:
    """增量 = max(trade_date)−(OVERLAP_DAYS−1) .. anchor（5 日含端点重叠）；
    无数据或 backfill=True → backfill_start（缺省锚定日−default_backfill_years）
    .. anchor。"""
    reg = BUILTIN_BY_KEY[entry["indicator_key"]]
    max_td = catalog.query(
        "SELECT max(trade_date) AS m FROM silver_external_indicators "
        "WHERE indicator_key = ?",
        [entry["indicator_key"]],
    ).item(0, "m")

    if not backfill and max_td is not None:
        start = max_td - timedelta(days=OVERLAP_DAYS - 1)
        return start, anchor

    bf_start = entry.get("backfill_start")
    if bf_start is None:
        try:
            bf_start = anchor.replace(year=anchor.year - reg.default_backfill_years)
        except ValueError:  # 02-29 → 02-28
            bf_start = anchor.replace(
                year=anchor.year - reg.default_backfill_years, day=28
            )
    if isinstance(bf_start, str):
        bf_start = date.fromisoformat(bf_start[:10])
    return bf_start, anchor


def _log_start(
    catalog: Catalog, key: str, source: str, trigger: str,
    start: date, end: date,
) -> int:
    df = catalog.query(
        "INSERT INTO silver_external_indicator_refresh_log "
        "(indicator_key, source_name, started_at, status, trigger, "
        " range_start, range_end) VALUES (?, ?, NOW(), 'running', ?, ?, ?) "
        "RETURNING run_id",
        [key, source, trigger, start, end],
    )
    return int(df.item(0, 0))


def _log_finish(
    catalog: Catalog, run_id: int | None, status: str, result: IndicatorRefreshResult
) -> None:
    if run_id is None:
        return
    catalog.execute(
        "UPDATE silver_external_indicator_refresh_log SET finished_at = NOW(), "
        "status = ?, rows_fetched = ?, rows_upserted = ?, error = ? "
        "WHERE run_id = ?",
        [status, result.rows_fetched, result.rows_upserted, result.error, run_id],
    )


def _finish_error(
    catalog: Catalog,
    run_id: int | None,
    key: str,
    result: IndicatorRefreshResult,
    exc: Exception,
) -> None:
    _log_finish(catalog, run_id, "error", result)
    try:
        mark_refresh_result(
            catalog, key, status="error", error=str(exc),
            source_name=result.source,
        )
    except Exception:  # noqa: BLE001 — 状态写回失败不掩盖原始错误
        logger.warning("mark_refresh_result failed for %s", key, exc_info=True)


__all__ = [
    "AkshareIndicatorAdapter",
    "IndicatorRefreshResult",
    "IndicatorFetchError",
    "NoSourceReadyError",
    "OVERLAP_DAYS",
    "RefreshSummary",
    "resolve_source",
    "run_external_indicator_refresh",
]
