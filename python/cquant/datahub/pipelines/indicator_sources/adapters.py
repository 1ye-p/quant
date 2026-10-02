"""tushare/akshare indicator adapters (P2-2).

两源适配器：把 akshare/tushare 原始返回转成统一标准帧
``[trade_date: date, value: float]``（按 trade_date 升序，已按 [start, end] 过滤）。

映射唯一事实源：docs/research/ext-ind-spike-result.md
（「锁定首批清单」+「样例对照」+「踩坑」）。

设计要点
--------
- per-key 映射表（模块级 ``AK_MAPPINGS`` / ``TS_MAPPINGS``）集中字段映射、
  重命名、单位换算、拉取模式（range / full / day_loop）——不散落 if/elif。
- 字段缺失 / 接口异常 / 空结果一律抛 :class:`IndicatorFetchError`，
  绝不静默写空值（空值经 loader 进 regime 是静默语义错误）。
- NaN 行：丢弃并 warning；若丢弃后行数骤减（>90% 被丢）抛错——源端
  schema 漂移的信号（自由裁量，见 :func:`_standardize` docstring）。
- ``north_acc_net_buy`` 源列为万亿元，硬性单位预检（×1e4 前校验量级），
  防止披露口径再变导致荒谬值落库。
- 不做频控/间隔（T3 刷新管）：szse 逐日循环的间隔、tushare shibor
  1次/min 均由调用方串行控制。
"""

from __future__ import annotations

import os
from dataclasses import dataclass
from datetime import date, timedelta
from typing import Any, Protocol, runtime_checkable

import polars as pl

__all__ = [
    "AK_MAPPINGS",
    "TS_MAPPINGS",
    "AkshareIndicatorAdapter",
    "IndicatorFetchError",
    "IndicatorSourceAdapterProtocol",
    "TushareIndicatorAdapter",
]


class IndicatorFetchError(Exception):
    """字段缺失/接口异常/空结果——一律抛，绝不静默写空值。"""


@runtime_checkable
class IndicatorSourceAdapterProtocol(Protocol):
    """适配器协议：name 属性 + fetch(indicator_key, start, end) → 标准帧。"""

    name: str

    def fetch(self, indicator_key: str, start: date, end: date) -> pl.DataFrame: ...


# ---------------------------------------------------------------------------
# per-key 映射表（spike 文档逐条照抄）
# ---------------------------------------------------------------------------

#: 拉取模式：
#:   range    — 区间接口，传 start_date/end_date（YYYYMMDD）
#:   full     — 全史一次拉（无日期参数或固定参数），拉完按窗口过滤
#:   day_loop — 单日接口逐日循环（start..end 每个工作日一调）
@dataclass(frozen=True)
class AkIndicatorMapping:
    func: str
    date_col: str
    value_col: str
    mode: str = "range"
    scale: float | None = None  # value × scale（None = 不换算）
    fixed_kwargs: tuple[tuple[str, Any], ...] = ()  # mode=full/range 的固定参数
    unit_precheck: str | None = None  # 换算前的量级硬预检（见 _UNIT_PRECHECKS）


AK_MAPPINGS: dict[str, AkIndicatorMapping] = {
    # 两融 — 沪市区间接口，元 → 亿元（/1e8）
    "margin_fin_balance_sse": AkIndicatorMapping(
        func="stock_margin_sse",
        date_col="信用交易日期",
        value_col="融资余额",
        scale=1e-8,
    ),
    "margin_total_balance_sse": AkIndicatorMapping(
        func="stock_margin_sse",
        date_col="信用交易日期",
        value_col="融资融券余额",
        scale=1e-8,
    ),
    # 两融 — 深市单日接口，逐日循环；源列已是亿元
    "margin_balance_szse": AkIndicatorMapping(
        func="stock_margin_szse",
        date_col="",  # 单日接口无日期列，date 参数即 trade_date
        value_col="融资余额",
        mode="day_loop",
    ),
    # 北向 — EM 源全史一次拉（2014-11-17 起）；当日净买额已是亿元
    "north_net_buy": AkIndicatorMapping(
        func="stock_hsgt_hist_em",
        date_col="日期",
        value_col="当日成交净买额",
        mode="full",
        fixed_kwargs=(("symbol", "北向资金"),),
    ),
    # 北向累计 — 源列「历史累计净买额」为**万亿元**（spike 实测），
    # ×1e4 换算到亿元，与 north_net_buy 的亿元列同量纲可比；换算前硬预检
    "north_acc_net_buy": AkIndicatorMapping(
        func="stock_hsgt_hist_em",
        date_col="日期",
        value_col="历史累计净买额",
        mode="full",
        fixed_kwargs=(("symbol", "北向资金"),),
        scale=1e4,
        unit_precheck="north_acc_trillion",
    ),
    # 估值 — 月频（月末值），全史一次拉后按窗口过滤
    "market_pe_all": AkIndicatorMapping(
        func="stock_market_pe_lg",
        date_col="日期",
        value_col="平均市盈率",
        mode="full",
    ),
    "index_pe_sse50_ttm": AkIndicatorMapping(
        func="stock_index_pe_lg",
        date_col="日期",
        value_col="滚动市盈率",
        mode="full",
        fixed_kwargs=(("symbol", "上证50"),),
    ),
    # 利率 — rate_interbank 无 start/end 参数（spike 踩坑：签名
    # (market, symbol, indicator)，默认参即 Shibor 隔夜全史），拉完过滤
    "shibor_overnight": AkIndicatorMapping(
        func="rate_interbank",
        date_col="报告日",
        value_col="利率",
        mode="full",
    ),
    "cn_gov_yield_10y": AkIndicatorMapping(
        func="bond_zh_us_rate",
        date_col="日期",
        value_col="中国国债收益率10年",
    ),
    "cn_yield_curve_10y2y": AkIndicatorMapping(
        func="bond_zh_us_rate",
        date_col="日期",
        value_col="中国国债收益率10年-2年",
    ),
    # 汇率 — 中间价全史（慢 ~6s，仅回填期用；T3 增量可换 sina 央行中间价列）
    "usd_cny_parity": AkIndicatorMapping(
        func="currency_boc_safe",
        date_col="日期",
        value_col="美元",
        mode="full",
    ),
    "usd_cny_boc": AkIndicatorMapping(
        func="currency_boc_sina",
        date_col="日期",
        value_col="中行折算价",
        fixed_kwargs=(("symbol", "美元"),),
    ),
}


@dataclass(frozen=True)
class TsIndicatorMapping:
    api: str  # pro client 方法名
    date_col: str
    value_col: str
    scale: float | None = None


# tushare 侧：本机积分档位仅 shibor 可用（spike）；其余接口需 2000 积分，
# 首批不进 tushare 主路径 → fetch 时抛 no mapping
TS_MAPPINGS: dict[str, TsIndicatorMapping] = {
    "shibor_overnight": TsIndicatorMapping(
        api="shibor",  # 频控 1次/min——间隔由调用方管，适配器不管
        date_col="date",
        value_col="on",  # overnight
    ),
}


# ---------------------------------------------------------------------------
# 单位硬预检（换算前）
# ---------------------------------------------------------------------------


def _precheck_north_acc_trillion(values: pl.Series) -> None:
    """north_acc_net_buy 源列应为万亿元量级（北向累计峰值 ~2.3，即 < ~100）。

    若源端口径变为亿元（raw 值 > 1000），×1e4 会产生荒谬值 → 抛错，
    提示人工核对 spike 文档后再改映射。
    """
    if values.len() == 0:
        return
    max_abs = float(values.abs().max())
    if max_abs > 1000.0:
        raise IndicatorFetchError(
            "north_acc_net_buy unit precheck failed: raw max |value|="
            f"{max_abs:.4g} exceeds 万亿元 magnitude (>1000); source unit "
            "likely changed (亿元?). Refusing ×1e4 conversion — see "
            "docs/research/ext-ind-spike-result.md north_acc_net_buy note."
        )


_UNIT_PRECHECKS = {"north_acc_trillion": _precheck_north_acc_trillion}


# ---------------------------------------------------------------------------
# 标准化（两源共用）
# ---------------------------------------------------------------------------


def _to_polars(raw: Any, context: str) -> pl.DataFrame:
    if isinstance(raw, pl.DataFrame):
        return raw
    try:
        return pl.from_pandas(raw)
    except Exception as exc:  # noqa: BLE001 — 统一转译为适配器错误
        raise IndicatorFetchError(
            f"{context}: unexpected return type {type(raw)!r}: {exc}"
        ) from exc


def _parse_date_col(df: pl.DataFrame, col: str, context: str) -> pl.Series:
    """str 日期 → date。兼容 'YYYY-MM-DD' / 'YYYYMMDD'（两种源都有）。"""
    s = df[col].cast(pl.String).str.replace_all("-", "")
    parsed = s.str.strptime(pl.Date, "%Y%m%d", strict=False)
    n_null = parsed.null_count()
    if parsed.len() > 0 and n_null == parsed.len():
        raise IndicatorFetchError(f"{context}: unparseable dates in column {col!r}")
    return parsed


def _standardize(
    raw: Any,
    *,
    date_col: str,
    value_col: str,
    scale: float | None,
    start: date,
    end: date,
    context: str,
    unit_precheck: str | None = None,
) -> pl.DataFrame:
    """原始帧 → 标准帧 [trade_date, value]（升序、窗口过滤）。

    NaN 策略（自由裁量）：value 为 null/NaN 的行**丢弃 + warning**——
    单点缺口（假日/缺披露）属正常；但若丢弃 >90% 行则视为 schema/口径
    漂移信号，抛错而非静默产出残缺序列。
    """
    df = _to_polars(raw, context)
    if df.height == 0:
        raise IndicatorFetchError(f"{context}: empty result from source interface")
    for col in (date_col, value_col):
        if col and col not in df.columns:
            raise IndicatorFetchError(
                f"{context}: missing column {col!r}; got {df.columns} "
                "(source schema drift?)"
            )

    out = pl.DataFrame(
        {
            "trade_date": _parse_date_col(df, date_col, context),
            "value": df[value_col].cast(pl.Float64, strict=False),
        }
    ).drop_nulls()

    dropped = df.height - out.height
    if dropped > 0:
        if out.height == 0 or dropped / df.height >= 0.9:
            raise IndicatorFetchError(
                f"{context}: {dropped}/{df.height} rows dropped as NaN — "
                "massive value loss suggests schema/unit drift, refusing "
                "to return a near-empty series"
            )
        import warnings

        warnings.warn(
            f"{context}: dropped {dropped} NaN value rows", stacklevel=2
        )

    if unit_precheck is not None:
        check = _UNIT_PRECHECKS.get(unit_precheck)
        if check is not None:
            check(out["value"])

    if scale is not None:
        out = out.with_columns((pl.col("value") * scale).alias("value"))

    out = (
        out.filter((pl.col("trade_date") >= start) & (pl.col("trade_date") <= end))
        .sort("trade_date")
        .select(["trade_date", "value"])
    )
    if out.height == 0:
        raise IndicatorFetchError(
            f"{context}: empty after window filter [{start}, {end}]"
        )
    return out


# ---------------------------------------------------------------------------
# akshare adapter
# ---------------------------------------------------------------------------


class AkshareIndicatorAdapter:
    """akshare 指标适配器（lazy import，免 token；12 key 全覆盖）。

    不做频控：akshare 实测接口连续调用无频控，串行间隔由调用方（T3）管。
    """

    name = "akshare"

    def __init__(self) -> None:
        self._ak: Any = None

    def _client(self) -> Any:
        if self._ak is None:
            try:
                import akshare as ak  # lazy（realtime_connector.py 先例）
            except ImportError as exc:
                raise IndicatorFetchError(
                    f"akshare not installed: {exc}"
                ) from exc
            self._ak = ak
        return self._ak

    def fetch(self, indicator_key: str, start: date, end: date) -> pl.DataFrame:
        m = AK_MAPPINGS.get(indicator_key)
        if m is None:
            raise IndicatorFetchError(
                f"akshare adapter: no mapping for indicator_key {indicator_key!r}"
            )
        ak = self._client()
        fn = getattr(ak, m.func, None)
        if not callable(fn):
            raise IndicatorFetchError(
                f"akshare adapter: interface {m.func!r} not found "
                "(akshare version drift?)"
            )
        ctx = f"akshare[{indicator_key} → {m.func}]"
        s, e = start.strftime("%Y%m%d"), end.strftime("%Y%m%d")

        try:
            if m.mode == "day_loop":
                raw = self._day_loop(fn, m, start, end, ctx)
                return self._standardize_day_loop(raw, m, start, end, ctx)
            if m.mode == "full":
                kwargs = dict(m.fixed_kwargs)
            else:  # range
                kwargs = dict(m.fixed_kwargs)
                kwargs.update(start_date=s, end_date=e)
            raw = fn(**kwargs)
        except IndicatorFetchError:
            raise
        except Exception as exc:  # noqa: BLE001 — 接口异常统一转译
            raise IndicatorFetchError(f"{ctx}: source call failed: {exc}") from exc
        return _standardize(
            raw,
            date_col=m.date_col,
            value_col=m.value_col,
            scale=m.scale,
            start=start,
            end=end,
            context=ctx,
            unit_precheck=m.unit_precheck,
        )

    # -- szse 单日接口逐日循环 --------------------------------------------

    def _day_loop(
        self,
        fn: Any,
        m: AkIndicatorMapping,
        start: date,
        end: date,
        ctx: str,
    ) -> list[dict[str, Any]]:
        """start..end 每个工作日（周一~周五）一调；假日/空返回跳过。

        频控间隔由调用方管（spike：串行 + 0.5s 间隔即可，本适配器不加
        sleep——单测零等待）。全程空 → 抛错。
        """
        rows: list[dict[str, Any]] = []
        d = start
        while d <= end:
            if d.weekday() < 5:  # 工作日近似（节假日由空返回自然跳过）
                try:
                    one = fn(date=d.strftime("%Y%m%d"))
                except Exception as exc:  # noqa: BLE001
                    raise IndicatorFetchError(
                        f"{ctx}: day-loop call failed at {d}: {exc}"
                    ) from exc
                one_df = _to_polars(one, ctx)
                if one_df.height > 0:
                    if m.value_col not in one_df.columns:
                        raise IndicatorFetchError(
                            f"{ctx}: missing column {m.value_col!r} at {d}; "
                            f"got {one_df.columns}"
                        )
                    rows.append(
                        {
                            "trade_date": d,
                            "value": float(one_df[m.value_col][0]),
                        }
                    )
            d += timedelta(days=1)
        if not rows:
            raise IndicatorFetchError(
                f"{ctx}: day-loop produced no rows in [{start}, {end}] "
                "(all days empty?)"
            )
        return rows

    def _standardize_day_loop(
        self,
        rows: list[dict[str, Any]],
        m: AkIndicatorMapping,
        start: date,
        end: date,
        ctx: str,
    ) -> pl.DataFrame:
        df = pl.DataFrame(
            {
                "trade_date": [r["trade_date"] for r in rows],
                "value": [r["value"] for r in rows],
            },
            schema={"trade_date": pl.Date, "value": pl.Float64},
        )
        if m.scale is not None:
            df = df.with_columns((pl.col("value") * m.scale).alias("value"))
        df = df.sort("trade_date").select(["trade_date", "value"])
        return df


# ---------------------------------------------------------------------------
# tushare adapter
# ---------------------------------------------------------------------------


class TushareIndicatorAdapter:
    """tushare 指标适配器（lazy import；当前仅 ``shibor_overnight`` 映射）。

    token 双读（tushare_connector.py:34 惯例）：显式参数 > settings > 环境变量
    TUSHARE_TOKEN。无 token / 无权限 → IndicatorFetchError 带原因。
    频控（shibor 1次/min）由调用方串行控制，适配器不加间隔。
    """

    name = "tushare"

    def __init__(self, token: str | None = None) -> None:
        if token:
            self._token: str = token
        else:
            tok = ""
            try:
                from cquant.core.config import settings

                tok = settings.tushare_token or ""
            except Exception:  # noqa: BLE001 — settings 不可用时走 env
                tok = ""
            self._token = tok or os.environ.get("TUSHARE_TOKEN", "")
        self._pro: Any = None

    def _client(self) -> Any:
        if not self._token:
            raise IndicatorFetchError(
                "tushare adapter: no token available (explicit arg > "
                "settings.tushare_token > TUSHARE_TOKEN env); this source "
                "requires a token"
            )
        if self._pro is None:
            try:
                import tushare as ts  # lazy
            except ImportError as exc:
                raise IndicatorFetchError(
                    f"tushare not installed: {exc}"
                ) from exc
            try:
                self._pro = ts.pro_api(self._token)
            except Exception as exc:  # noqa: BLE001
                raise IndicatorFetchError(
                    f"tushare adapter: pro_api init failed: {exc}"
                ) from exc
        return self._pro

    def fetch(self, indicator_key: str, start: date, end: date) -> pl.DataFrame:
        m = TS_MAPPINGS.get(indicator_key)
        if m is None:
            raise IndicatorFetchError(
                f"tushare adapter: no mapping for indicator_key "
                f"{indicator_key!r} (spike: only shibor_overnight available "
                "on current token tier; others need 2000-point token)"
            )
        pro = self._client()
        ctx = f"tushare[{indicator_key} → {m.api}]"
        try:
            raw = getattr(pro, m.api)(
                start_date=start.strftime("%Y%m%d"), end_date=end.strftime("%Y%m%d")
            )
        except Exception as exc:  # noqa: BLE001
            raise IndicatorFetchError(
                f"{ctx}: source call failed (no permission / rate limit?): {exc}"
            ) from exc
        return _standardize(
            raw,
            date_col=m.date_col,
            value_col=m.value_col,
            scale=m.scale,
            start=start,
            end=end,
            context=ctx,
        )
