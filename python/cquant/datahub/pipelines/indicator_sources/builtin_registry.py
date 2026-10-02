"""Builtin external-indicator registry (P2-1).

Single source of truth for the 12 locked builtin indicators
(docs/research/ext-ind-spike-result.md「锁定首批清单（12 个）」— 逐条照抄，
勿增删). Adapters (P2-2) and the refresh pipeline (P2-3) consume this
registry; no fetching logic lives here.
"""

from __future__ import annotations

import os
from dataclasses import dataclass


@dataclass(frozen=True)
class BuiltinIndicatorDef:
    indicator_key: str
    display_name: str
    unit: str | None
    description: str
    candidates: tuple[str, ...]  # 优先序（spike：akshare 首选；仅 shibor 有 ts 备选）
    available_date_rule: str  # spike 锁定全部 'B'
    frequency: str  # 'daily' | 'weekly' | 'monthly'
    default_backfill_years: int = 2


BUILTIN_CATALOG: tuple[BuiltinIndicatorDef, ...] = (
    BuiltinIndicatorDef(
        indicator_key="margin_fin_balance_sse",
        display_name="两融余额-融资余额(沪)",
        unit="亿元",
        description="两融：沪市融资余额，akshare stock_margin_sse，原始元需 /1e8 换算为亿元",
        candidates=("akshare",),
        available_date_rule="B",
        frequency="daily",
    ),
    BuiltinIndicatorDef(
        indicator_key="margin_total_balance_sse",
        display_name="两融余额-融资融券余额(沪)",
        unit="亿元",
        description="两融：沪市融资融券余额，akshare stock_margin_sse，原始元需 /1e8 换算为亿元",
        candidates=("akshare",),
        available_date_rule="B",
        frequency="daily",
    ),
    BuiltinIndicatorDef(
        indicator_key="margin_balance_szse",
        display_name="两融余额-融资余额(深)",
        unit="亿元",
        description="两融：深市融资余额，akshare stock_margin_szse（单日接口，回填需逐日循环），已是亿元",
        candidates=("akshare",),
        available_date_rule="B",
        frequency="daily",
    ),
    BuiltinIndicatorDef(
        indicator_key="north_net_buy",
        display_name="北向资金当日净买入",
        unit="亿元",
        description="北向：北向资金当日成交净买额，akshare stock_hsgt_hist_em 全史一次拉，已是亿元",
        candidates=("akshare",),
        available_date_rule="B",
        frequency="daily",
    ),
    BuiltinIndicatorDef(
        indicator_key="north_acc_net_buy",
        display_name="北向资金累计净买入",
        unit="万亿元",
        description=(
            "北向：北向资金历史累计净买额。注意原生单位为万亿元（spike 实测），"
            "适配器须 ×1e4 换算为亿元或按原单位落库，不得与 north_net_buy 的亿元列混用"
        ),
        candidates=("akshare",),
        available_date_rule="B",
        frequency="daily",
    ),
    BuiltinIndicatorDef(
        indicator_key="market_pe_all",
        display_name="全A平均市盈率",
        unit="倍",
        description="估值：全A平均市盈率（月末值），akshare stock_market_pe_lg 全史",
        candidates=("akshare",),
        available_date_rule="B",
        frequency="monthly",
    ),
    BuiltinIndicatorDef(
        indicator_key="index_pe_sse50_ttm",
        display_name="上证50滚动PE",
        unit="倍",
        description="估值：上证50滚动市盈率（月末值），akshare stock_index_pe_lg",
        candidates=("akshare",),
        available_date_rule="B",
        frequency="monthly",
    ),
    BuiltinIndicatorDef(
        indicator_key="shibor_overnight",
        display_name="Shibor隔夜利率",
        unit="%",
        description="利率：Shibor 隔夜拆借利率，akshare rate_interbank 全史；备选 tushare shibor（需 token，1 次/min 频控）",
        candidates=("akshare", "tushare"),
        available_date_rule="B",
        frequency="daily",
    ),
    BuiltinIndicatorDef(
        indicator_key="cn_gov_yield_10y",
        display_name="中国10年期国债收益率",
        unit="%",
        description="利率：中国 10 年期国债到期收益率，akshare bond_zh_us_rate",
        candidates=("akshare",),
        available_date_rule="B",
        frequency="daily",
    ),
    BuiltinIndicatorDef(
        indicator_key="cn_yield_curve_10y2y",
        display_name="期限利差(10Y-2Y)",
        unit="pct",
        description="利率：中国国债期限利差（10Y−2Y），akshare bond_zh_us_rate 同源两列相减",
        candidates=("akshare",),
        available_date_rule="B",
        frequency="daily",
    ),
    BuiltinIndicatorDef(
        indicator_key="usd_cny_parity",
        display_name="美元兑人民币中间价",
        unit="CNY/100USD",
        description="汇率：美元兑人民币中间价，akshare currency_boc_safe 全史（较慢），备选 currency_boc_sina 央行中间价列",
        candidates=("akshare",),
        available_date_rule="B",
        frequency="daily",
    ),
    BuiltinIndicatorDef(
        indicator_key="usd_cny_boc",
        display_name="中行美元折算价",
        unit="CNY/100USD",
        description="汇率：中国银行美元折算价，akshare currency_boc_sina 区间接口",
        candidates=("akshare",),
        available_date_rule="B",
        frequency="daily",
    ),
)

BUILTIN_BY_KEY: dict[str, BuiltinIndicatorDef] = {
    d.indicator_key: d for d in BUILTIN_CATALOG
}


def tushare_ready() -> bool:
    """tushare 源是否就绪：settings.tushare_token 或环境变量 TUSHARE_TOKEN
    双读（沿 tushare_connector.py 的优先级惯例：settings 优先、env 兜底）。"""
    try:
        from cquant.core.config import settings

        if settings.tushare_token:
            return True
    except Exception:
        pass
    return bool(os.environ.get("TUSHARE_TOKEN"))


def source_ready(name: str) -> bool:
    """候选源就绪态：tushare → tushare_ready()；akshare → 恒 True（免 token）。"""
    if name == "tushare":
        return tushare_ready()
    return True
