"""MarketSeriesContext — 外部指标 → ``__MARKET__`` 哨兵伪 panel 包装层（spike A）。

职责（docs/research/phase3-spike-result.md 适配项 #3）：
  1. ``load_external_series``（PIT：available_date <= as_of）取各指标序列；
  2. rename ``value -> 指标别名``、加 ``asset_id='__MARKET__'`` 常量列 → 伪 panel；
  3. ``compile_expression(expr, extra_columns=别名集)`` 在拼好的宽表伪 panel 上
     求值，返回末行标量（regime 规则求值的用法）。

缺失日（迟到数据）语义：PIT 过滤发生在求值之前，panel 只含已到位行，
滚动函数窗口在过滤后的连续行上计算 —— 即「最近可得观测」，不存在窗口
越过 PIT 边界的可能（spike §4 已验证）。
"""

from __future__ import annotations

import logging
from datetime import date

import polars as pl

from cquant.datahub.external_loader import MARKET_SENTINEL, load_external_series
from cquant.factorlab.dsl_evaluator import compile_expression

logger = logging.getLogger(__name__)


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
        # (as_of, indicators 快照) → 已拼接宽表伪 panel（同一天多表达式复用）
        self._panel_cache: dict[
            tuple[date, tuple[tuple[str, str], ...]], pl.DataFrame
        ] = {}

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
        raw = load_external_series(self._catalog, indicator_key, as_of)
        if raw.is_empty():
            return pl.DataFrame(
                schema={"trade_date": pl.Date, col: pl.Float64, "asset_id": pl.Utf8}
            )
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
        cache_key = (as_of, tuple(sorted(indicators.items())))
        cached = self._panel_cache.get(cache_key)
        if cached is not None:
            return cached

        panel: pl.DataFrame | None = None
        for alias, key in indicators.items():
            frag = self.series(key, as_of, alias=alias)
            if frag.is_empty():
                continue
            if panel is None:
                panel = frag
            else:
                # asset_id 恒为哨兵常量，按 trade_date 全外连接即可
                panel = panel.join(frag.drop("asset_id"), on="trade_date", how="full")
        if panel is None:
            panel = pl.DataFrame(
                schema={
                    "trade_date": pl.Date,
                    "asset_id": pl.Utf8,
                    **{a: pl.Float64 for a in indicators},
                }
            )
        else:
            # 保证所有别名列都存在（某指标完全缺失时为 null 列）
            missing = [a for a in indicators if a not in panel.columns]
            if missing:
                panel = panel.with_columns(
                    [pl.lit(None, dtype=pl.Float64).alias(a) for a in missing]
                )
        panel = panel.sort("trade_date").select(
            ["trade_date"] + list(indicators.keys()) + ["asset_id"]
        )
        self._panel_cache[cache_key] = panel
        return panel

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
