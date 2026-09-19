"""MarketSeriesContext — 外部指标 → ``__MARKET__`` 哨兵伪 panel 包装层（spike A）。

职责（docs/research/phase3-spike-result.md 适配项 #3）：
  1. ``load_external_series``（PIT：available_date <= as_of）取各指标序列；
  2. rename ``value -> 指标别名``、加 ``asset_id='__MARKET__'`` 常量列 → 伪 panel；
  3. ``compile_expression(expr, extra_columns=别名集)`` 在拼好的宽表伪 panel 上
     求值，返回末行标量（regime 规则求值的用法）。

缺失日（迟到数据）语义：PIT 过滤发生在求值之前，panel 只含已到位行，
滚动函数窗口在过滤后的连续行上计算 —— 即「最近可得观测」，不存在窗口
越过 PIT 边界的可能（spike §4 已验证）。

缓存策略（避免逐 as_of 全量重建的 O(N²) 查询/内存）：
  - 每个 indicator_key 只查询一次「全历史修订行」（不去重，含
    available_date / updated_at 列），之后任意 as_of 的 PIT 视图在本地
    复刻 loader SQL 语义：先 ``available_date <= as_of`` 过滤，再按
    trade_date 取 ``updated_at`` 最大行（arg_max 等价）——顺序与 SQL 一致，
    同一 trade_date 多次修订时可见旧修订（全局 arg_max 后过滤会丢行）；
  - 宽表伪 panel 同样基于缓存的修订行在视图时重建（过滤 → arg_max →
    按 trade_date 外连接），不再预去重；
  - 缓存为 LRU（容量默认 32，环境变量 ``CQUANT_MARKET_PANEL_CACHE``
    可调；设为 0 完全禁用缓存，退回逐次查询的原始行为）。
"""

from __future__ import annotations

import logging
import os
from collections import OrderedDict
from datetime import date

import polars as pl

from cquant.datahub.external_loader import (
    MARKET_SENTINEL,
    load_external_series,
    load_external_series_revisions,
)
from cquant.factorlab.dsl_evaluator import compile_expression

logger = logging.getLogger(__name__)

_DEFAULT_CACHE_SIZE = 32
# 全历史加载的 PIT 截止哨兵（available_date <= date.max 即全部行）
_LOAD_ALL = date.max


def _cache_maxsize() -> int:
    raw = os.environ.get("CQUANT_MARKET_PANEL_CACHE", "")
    try:
        return int(raw) if raw != "" else _DEFAULT_CACHE_SIZE
    except ValueError:
        return _DEFAULT_CACHE_SIZE


def _empty_series(col: str) -> pl.DataFrame:
    return pl.DataFrame(schema={"trade_date": pl.Date, col: pl.Float64, "asset_id": pl.Utf8})


class MarketSeriesContext:
    """外部市场指标的 PIT 伪 panel 求值上下文。

    Usage::

        ctx = MarketSeriesContext(catalog)
        ctx.evaluate(
            "pct_change(active_cap, 5) < -0.03",
            as_of=date(2025, 3, 31),
            indicators={"active_cap": "cn_active_cap"},
        )
    """

    def __init__(self, catalog) -> None:
        self._catalog = catalog
        self._maxsize = _cache_maxsize()
        self._cache_enabled = self._maxsize > 0
        # indicator_key -> 全历史修订行 raw [trade_date, value, available_date,
        # updated_at]（不去重，保留被修订覆盖的旧行）
        self._raw_cache: OrderedDict[str, pl.DataFrame] = OrderedDict()
        # 底层 catalog 查询计数（测试/诊断用）
        self.load_count = 0

    # ── 缓存原语 ─────────────────────────────────────────────────────────

    def _raw_full(self, indicator_key: str) -> pl.DataFrame:
        """取指标全历史修订行（含 available_date/updated_at），带 LRU 逐出。"""
        cached = self._raw_cache.get(indicator_key)
        if cached is not None:
            self._raw_cache.move_to_end(indicator_key)
            return cached
        raw = load_external_series_revisions(self._catalog, indicator_key, _LOAD_ALL)
        self.load_count += 1
        self._raw_cache[indicator_key] = raw
        while len(self._raw_cache) > self._maxsize:
            self._raw_cache.popitem(last=False)
        return raw

    @staticmethod
    def _pit_view(raw: pl.DataFrame, as_of: date) -> pl.DataFrame:
        """本地复刻 loader SQL 的 PIT 语义（顺序一致，修订等价）。

        SQL 是 ``WHERE available_date <= ?`` 之后才
        ``arg_max(value, updated_at) ... GROUP BY trade_date``；因此本地视图
        也必须先过滤可见行，再按 trade_date 取 updated_at 最大者 —— 若先在
        全历史上取赢家再过滤，赢家修订尚未到位的 trade_date 会整行丢失。
        """
        if raw.is_empty():
            return raw
        visible = raw.filter(pl.col("available_date") <= as_of)
        if visible.is_empty():
            return visible
        return (
            visible.sort("updated_at")
            .unique(subset=["trade_date"], keep="last")
            .sort("trade_date")
            .drop("updated_at")
        )

    # ── 单指标序列 ────────────────────────────────────────────────────────

    def series(
        self,
        indicator_key: str,
        as_of: date,
        alias: str | None = None,
    ) -> pl.DataFrame:
        """取单个指标的 PIT 序列并整形为伪 panel 片段。

        Returns
        -------
        pl.DataFrame
            ``[trade_date, <alias 或 indicator_key>, asset_id]``，
            ``asset_id`` 恒为 ``'__MARKET__'``，按 trade_date 升序。
            指标在该 as_of 下无任何已到位行时返回空表。
        """
        col = alias or indicator_key
        if not self._cache_enabled:
            raw = load_external_series(self._catalog, indicator_key, as_of)
            self.load_count += 1
        else:
            # [trade_date, value, available_date] — arg_max 等价视图
            raw = self._pit_view(self._raw_full(indicator_key), as_of)
        if raw.is_empty():
            return _empty_series(col)
        if "available_date" in raw.columns:
            raw = raw.drop("available_date")
        return raw.rename({"value": col}).with_columns(
            pl.lit(MARKET_SENTINEL).alias("asset_id")
        ).select(["trade_date", col, "asset_id"])

    # ── 宽表伪 panel ─────────────────────────────────────────────────────

    def panel(self, as_of: date, indicators: dict[str, str]) -> pl.DataFrame:
        """多指标外连接拼接为一张宽表伪 panel（列名 = 别名）。

        Parameters
        ----------
        as_of:
            PIT 截止日（含）。
        indicators:
            ``{列别名: indicator_key}``，即 ``RegimeDef.indicators``。
            各指标到位日期不同时按 trade_date 外连接 —— 某指标迟到的行
            该列为 null，由表达式自然传播。
        """
        if not self._cache_enabled:
            return self._panel_uncached(as_of, indicators)
        return self._panel_cached(as_of, indicators)

    def _panel_cached(self, as_of: date, indicators: dict[str, str]) -> pl.DataFrame:
        """缓存路径：基于 per-key 修订行缓存在视图时重建宽表。

        每个指标先做与 loader SQL 等价的本地 PIT 视图（过滤 → arg_max），
        再按 trade_date 外连接 —— 与无缓存路径逐片段查询的行为一致，
        仅数据源换成缓存的全历史修订行。
        """
        panel: pl.DataFrame | None = None
        for alias, key in indicators.items():
            view = self._pit_view(self._raw_full(key), as_of)
            if view.is_empty():
                continue
            frag = (
                view.drop("available_date")
                .rename({"value": alias})
                .select(["trade_date", alias])
            )
            if panel is None:
                panel = frag
            else:
                panel = panel.join(frag, on="trade_date", how="full", coalesce=True)
        if panel is None:
            return pl.DataFrame(
                schema={
                    "trade_date": pl.Date,
                    "asset_id": pl.Utf8,
                    **{a: pl.Float64 for a in indicators},
                }
            )
        return (
            panel.sort("trade_date")
            .with_columns(pl.lit(MARKET_SENTINEL).alias("asset_id"))
            .select(["trade_date"] + list(indicators.keys()) + ["asset_id"])
        )

    def _panel_uncached(self, as_of: date, indicators: dict[str, str]) -> pl.DataFrame:
        """无缓存路径：逐指标按 as_of 查询（原始行为）。"""
        panel: pl.DataFrame | None = None
        for alias, key in indicators.items():
            frag = self.series(key, as_of, alias=alias)
            if frag.is_empty():
                continue
            if panel is None:
                panel = frag
            else:
                # asset_id 恒为哨兵常量，按 trade_date 全外连接（合并主键）即可
                panel = panel.join(
                    frag.drop("asset_id"), on="trade_date", how="full", coalesce=True
                )
        if panel is None:
            panel = pl.DataFrame(
                schema={
                    "trade_date": pl.Date,
                    "asset_id": pl.Utf8,
                    **{a: pl.Float64 for a in indicators},
                }
            )
        else:
            # 右侧独有行的 asset_id 为 null → 回填哨兵
            panel = panel.with_columns(pl.col("asset_id").fill_null(MARKET_SENTINEL))
            # 保证所有别名列都存在（某指标完全缺失时为 null 列）
            missing = [a for a in indicators if a not in panel.columns]
            if missing:
                panel = panel.with_columns(
                    [pl.lit(None, dtype=pl.Float64).alias(a) for a in missing]
                )
        return panel.sort("trade_date").select(
            ["trade_date"] + list(indicators.keys()) + ["asset_id"]
        )

    # ── 表达式求值 ────────────────────────────────────────────────────────

    def evaluate(
        self,
        expr: str,
        as_of: date,
        indicators: dict[str, str] | None = None,
    ) -> float:
        """在 as_of 的宽表伪 panel 末行上求值表达式，返回标量。

        Parameters
        ----------
        expr:
            DSL 表达式；其中的列名通过 ``indicators`` 解析为外部指标。
        indicators:
            ``{列别名: indicator_key}``；为空时表达式只能引用内建列。

        Returns
        -------
        float
            伪 panel 最后一行的表达式值（比较运算符已 cast 为 0/1）。

        Raises
        ------
        ValueError
            panel 为空（指标在该 as_of 下完全无数据）或末行值为 null
            （窗口预热不足 / 指标迟到导致该行缺失）。调用方
            （RegimeStateMachine）捕获后走 hold-state 兜底。
        """
        indicators = indicators or {}
        panel = self.panel(as_of, indicators)
        if panel.is_empty():
            raise ValueError(
                f"market panel empty at {as_of} for indicators {sorted(indicators)}"
            )
        compiled = compile_expression(expr, extra_columns=set(indicators.keys()))
        valued = panel.with_columns(compiled.alias("__value__"))
        last = valued.tail(1)["__value__"][0]
        if last is None:
            raise ValueError(
                f"expression '{expr}' evaluated to null at {as_of} "
                "(insufficient window or missing indicator row)"
            )
        return float(last)
