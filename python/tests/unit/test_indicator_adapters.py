"""Unit tests for tushare/akshare indicator adapters (P2-2).

All network calls are monkeypatched — zero real network (red line).
Fake modules are injected into sys.modules so the adapters' lazy imports
pick them up.
"""

from __future__ import annotations

import sys
import types
from datetime import date

import polars as pl
import pytest

from cquant.datahub.pipelines.indicator_sources.adapters import (
    AK_MAPPINGS,
    TS_MAPPINGS,
    AkshareIndicatorAdapter,
    IndicatorFetchError,
    IndicatorSourceAdapterProtocol,
    TushareIndicatorAdapter,
)

# ---------------------------------------------------------------------------
# helpers: fake akshare / tushare modules
# ---------------------------------------------------------------------------


class CallRecorder:
    def __init__(self, ret):
        self.ret = ret
        self.calls: list[dict] = []

    def __call__(self, **kwargs):
        self.calls.append(kwargs)
        return self.ret


def install_fake_module(monkeypatch, name: str, funcs: dict):
    mod = types.ModuleType(name)
    for fn_name, fn in funcs.items():
        setattr(mod, fn_name, fn)
    monkeypatch.setitem(sys.modules, name, mod)
    return mod


@pytest.fixture()
def no_tushare_token(monkeypatch):
    monkeypatch.delenv("TUSHARE_TOKEN", raising=False)
    # settings 双读的另一路：patch 类字段默认值（pydantic settings 实例属性只读）
    import cquant.core.config as cfg

    monkeypatch.setattr(
        cfg.CQuantSettings, "tushare_token", None, raising=False
    )


# ---------------------------------------------------------------------------
# mapping-table coverage
# ---------------------------------------------------------------------------


def test_mapping_tables_cover_builtin_catalog_keys():
    from cquant.datahub.pipelines.indicator_sources.builtin_registry import BUILTIN_CATALOG

    catalog_keys = {d.indicator_key for d in BUILTIN_CATALOG}
    assert set(AK_MAPPINGS) == catalog_keys  # akshare 覆盖全部 12 key
    assert set(TS_MAPPINGS) == {"shibor_overnight"}  # tushare 仅 shibor


def test_north_acc_mapping_has_scale_and_precheck():
    m = AK_MAPPINGS["north_acc_net_buy"]
    assert m.scale == 1e4  # 万亿元 → 亿元
    assert m.unit_precheck == "north_acc_trillion"


# ---------------------------------------------------------------------------
# normal frames
# ---------------------------------------------------------------------------


def test_margin_sse_range_call_and_yuan_to_yi_conversion(monkeypatch):
    raw = pl.DataFrame(
        {
            "信用交易日期": ["20250920", "20250919"],
            "融资余额": [1207639982663.0, 1200000000000.0],
            "融资融券余额": [1210000000000.0, 1205000000000.0],
        }
    )
    rec = CallRecorder(raw)
    install_fake_module(monkeypatch, "akshare", {"stock_margin_sse": rec})

    out = AkshareIndicatorAdapter().fetch(
        "margin_fin_balance_sse", date(2025, 9, 15), date(2025, 9, 20)
    )
    # 调用参数：区间接口带 YYYYMMDD start/end
    assert rec.calls == [
        {"start_date": "20250915", "end_date": "20250920"}
    ]
    # 标准帧：列、类型、升序、元→亿元换算
    assert out.columns == ["trade_date", "value"]
    assert out["trade_date"].dtype == pl.Date
    assert out["value"].dtype == pl.Float64
    assert out["trade_date"].to_list() == [date(2025, 9, 19), date(2025, 9, 20)]
    assert out["value"].to_list() == pytest.approx([12000.0, 12076.39982663])


def test_margin_total_balance_sse(monkeypatch):
    raw = pl.DataFrame(
        {"信用交易日期": ["20250919"], "融资融券余额": [1210000000000.0]}
    )
    install_fake_module(
        monkeypatch, "akshare", {"stock_margin_sse": CallRecorder(raw)}
    )
    out = AkshareIndicatorAdapter().fetch(
        "margin_total_balance_sse", date(2025, 9, 1), date(2025, 9, 30)
    )
    assert out["value"].to_list() == pytest.approx([12100.0])


def test_north_net_buy_full_history_no_scaling(monkeypatch):
    raw = pl.DataFrame(
        {
            "日期": ["2014-11-17", "2024-08-16"],
            "当日成交净买额": [120.8233, -5.66],
            "历史累计净买额": [0.01208233, 1.768],
        }
    )
    rec = CallRecorder(raw)
    install_fake_module(monkeypatch, "akshare", {"stock_hsgt_hist_em": rec})
    out = AkshareIndicatorAdapter().fetch(
        "north_net_buy", date(2014, 1, 1), date(2025, 1, 1)
    )
    assert rec.calls == [{"symbol": "北向资金"}]  # 全史一次拉，不带日期参数
    assert out["value"].to_list() == pytest.approx([120.8233, -5.66])


def test_north_acc_net_buy_times_1e4(monkeypatch):
    raw = pl.DataFrame(
        {
            "日期": ["2023-12-28", "2023-12-29"],
            "历史累计净买额": [1.767, 1.768],
        }
    )
    install_fake_module(monkeypatch, "akshare", {"stock_hsgt_hist_em": CallRecorder(raw)})
    out = AkshareIndicatorAdapter().fetch(
        "north_acc_net_buy", date(2023, 1, 1), date(2024, 1, 1)
    )
    # 万亿元 × 1e4 → 亿元（硬性预检通过后换算）
    assert out["value"].to_list() == pytest.approx([17670.0, 17680.0])


def test_north_acc_unit_precheck_raises_on_billion_magnitude(monkeypatch):
    """预检：若源列单位已变为亿元（raw 值 > 1000），×1e4 会产生荒谬值 → 抛错。"""
    raw = pl.DataFrame(
        {"日期": ["2023-12-29"], "历史累计净买额": [17680.0]}  # 已是亿元
    )
    install_fake_module(monkeypatch, "akshare", {"stock_hsgt_hist_em": CallRecorder(raw)})
    with pytest.raises(IndicatorFetchError, match="unit"):
        AkshareIndicatorAdapter().fetch(
            "north_acc_net_buy", date(2023, 1, 1), date(2024, 1, 1)
        )


def test_shibor_interbank_has_no_date_params(monkeypatch):
    raw = pl.DataFrame({"报告日": ["2006-10-08", "2006-10-09"], "利率": [2.1184, 2.1]})
    rec = CallRecorder(raw)  # rate_interbank 无 start/end 参数（spike 踩坑）
    install_fake_module(monkeypatch, "akshare", {"rate_interbank": rec})
    out = AkshareIndicatorAdapter().fetch(
        "shibor_overnight", date(2006, 10, 1), date(2006, 10, 31)
    )
    assert rec.calls == [{}]  # 不传日期参数
    assert out["value"].to_list() == pytest.approx([2.1184, 2.1])
    assert out["trade_date"].to_list() == [date(2006, 10, 8), date(2006, 10, 9)]


def test_bond_zh_us_rate_two_columns(monkeypatch):
    raw = pl.DataFrame(
        {
            "日期": ["2025-09-01", "2025-09-02"],
            "中国国债收益率10年": [1.8257, 1.83],
            "中国国债收益率10年-2年": [0.35, 0.36],
        }
    )
    rec = CallRecorder(raw)
    install_fake_module(monkeypatch, "akshare", {"bond_zh_us_rate": rec})
    a = AkshareIndicatorAdapter()
    out10y = a.fetch("cn_gov_yield_10y", date(2025, 9, 1), date(2025, 9, 30))
    outcurve = a.fetch("cn_yield_curve_10y2y", date(2025, 9, 1), date(2025, 9, 30))
    assert rec.calls[0] == {"start_date": "20250901", "end_date": "20250930"}
    assert out10y["value"].to_list() == pytest.approx([1.8257, 1.83])
    assert outcurve["value"].to_list() == pytest.approx([0.35, 0.36])


def test_usd_cny_parity_full_history_filtered(monkeypatch):
    raw = pl.DataFrame(
        {"日期": ["1994-01-01", "2020-06-30", "2025-09-01"], "美元": [870.0, 710.0, 700.0]}
    )
    install_fake_module(monkeypatch, "akshare", {"currency_boc_safe": CallRecorder(raw)})
    out = AkshareIndicatorAdapter().fetch(
        "usd_cny_parity", date(2020, 1, 1), date(2020, 12, 31)
    )
    # 全史一次拉后按窗口过滤
    assert out["trade_date"].to_list() == [date(2020, 6, 30)]


def test_usd_cny_boc_sina_range(monkeypatch):
    raw = pl.DataFrame({"日期": ["2025-09-01"], "中行折算价": [710.0]})
    rec = CallRecorder(raw)
    install_fake_module(monkeypatch, "akshare", {"currency_boc_sina": rec})
    out = AkshareIndicatorAdapter().fetch(
        "usd_cny_boc", date(2025, 9, 1), date(2025, 9, 30)
    )
    assert rec.calls == [
        {"symbol": "美元", "start_date": "20250901", "end_date": "20250930"}
    ]
    assert out["value"].to_list() == pytest.approx([710.0])


# ---------------------------------------------------------------------------
# monthly full-history
# ---------------------------------------------------------------------------


def test_monthly_indicators_full_history_then_window_filter(monkeypatch):
    pe_all = pl.DataFrame(
        {"日期": ["1997-12-31", "2019-01-31", "2020-06-30", "2025-08-31"], "平均市盈率": [40.0, 20.0, 21.0, 22.0]}
    )
    pe_50 = pl.DataFrame(
        {"日期": ["2005-01-31", "2020-06-30", "2025-08-31"], "滚动市盈率": [15.0, 10.0, 11.0]}
    )
    install_fake_module(
        monkeypatch,
        "akshare",
        {
            "stock_market_pe_lg": CallRecorder(pe_all),
            "stock_index_pe_lg": CallRecorder(pe_50),
        },
    )
    a = AkshareIndicatorAdapter()
    out1 = a.fetch("market_pe_all", date(2020, 1, 1), date(2020, 12, 31))
    out2 = a.fetch("index_pe_sse50_ttm", date(2020, 1, 1), date(2020, 12, 31))
    assert out1["trade_date"].to_list() == [date(2020, 6, 30)]
    assert out2["trade_date"].to_list() == [date(2020, 6, 30)]
    assert out1["value"].to_list() == pytest.approx([21.0])


# ---------------------------------------------------------------------------
# szse day-loop
# ---------------------------------------------------------------------------


def test_szse_day_loop_calls_each_weekday_and_assembles(monkeypatch):
    # 2024-01-01(周一，假日空返回) ~ 2024-01-05(周五)：5 个工作日调用，4 行拼装
    def fake_szse(date: str, **_):
        if date == "20240101":
            return pl.DataFrame(schema={"融资余额": pl.Float64})  # 假日空
        return pl.DataFrame({"融资余额": [float(int(date[-2:]))]})  # 无日期列，date 即参数

    rec_calls: list[str] = []

    def wrapped(date: str, **_):
        rec_calls.append(date)
        return fake_szse(date)

    install_fake_module(monkeypatch, "akshare", {"stock_margin_szse": wrapped})
    out = AkshareIndicatorAdapter().fetch(
        "margin_balance_szse", date(2024, 1, 1), date(2024, 1, 5)
    )
    assert rec_calls == ["20240101", "20240102", "20240103", "20240104", "20240105"]
    assert out["trade_date"].to_list() == [
        date(2024, 1, 2),
        date(2024, 1, 3),
        date(2024, 1, 4),
        date(2024, 1, 5),
    ]
    assert out["value"].to_list() == pytest.approx([2.0, 3.0, 4.0, 5.0])


def test_szse_day_loop_all_empty_raises(monkeypatch):
    empty = pl.DataFrame(schema={"融资余额": pl.Float64})
    install_fake_module(
        monkeypatch, "akshare", {"stock_margin_szse": lambda date, **_: empty}
    )
    with pytest.raises(IndicatorFetchError, match="empty|szse"):
        AkshareIndicatorAdapter().fetch(
            "margin_balance_szse", date(2024, 1, 1), date(2024, 1, 5)
        )


# ---------------------------------------------------------------------------
# error paths
# ---------------------------------------------------------------------------


def test_missing_column_raises(monkeypatch):
    raw = pl.DataFrame({"别的列": ["20250919"]})
    install_fake_module(monkeypatch, "akshare", {"stock_margin_sse": CallRecorder(raw)})
    with pytest.raises(IndicatorFetchError, match="column|字段"):
        AkshareIndicatorAdapter().fetch(
            "margin_fin_balance_sse", date(2025, 9, 1), date(2025, 9, 30)
        )


def test_empty_raw_result_raises(monkeypatch):
    raw = pl.DataFrame(schema={"信用交易日期": pl.String, "融资余额": pl.Float64})
    install_fake_module(monkeypatch, "akshare", {"stock_margin_sse": CallRecorder(raw)})
    with pytest.raises(IndicatorFetchError, match="empty|空"):
        AkshareIndicatorAdapter().fetch(
            "margin_fin_balance_sse", date(2025, 9, 1), date(2025, 9, 30)
        )


def test_nan_rows_dropped_but_massive_drop_raises(monkeypatch):
    # 少量 NaN：丢弃 + 保留其余行
    raw = pl.DataFrame(
        {"报告日": ["2025-09-01", "2025-09-02"], "利率": [2.0, None]}
    )
    install_fake_module(monkeypatch, "akshare", {"rate_interbank": CallRecorder(raw)})
    out = AkshareIndicatorAdapter().fetch(
        "shibor_overnight", date(2025, 9, 1), date(2025, 9, 30)
    )
    assert out.height == 1

    # 行数骤减（≥90% NaN）→ 抛错
    raw_bad = pl.DataFrame(
        {
            "报告日": [f"2025-09-{d:02d}" for d in range(1, 11)],
            "利率": [None] * 9 + [2.0],
        }
    )
    install_fake_module(monkeypatch, "akshare", {"rate_interbank": CallRecorder(raw_bad)})
    with pytest.raises(IndicatorFetchError, match="NaN"):
        AkshareIndicatorAdapter().fetch(
            "shibor_overnight", date(2025, 9, 1), date(2025, 9, 30)
        )


def test_no_mapping_raises(monkeypatch):
    install_fake_module(monkeypatch, "akshare", {})
    with pytest.raises(IndicatorFetchError, match="no mapping"):
        AkshareIndicatorAdapter().fetch("nonexistent_key", date(2025, 1, 1), date(2025, 1, 31))


def test_window_filter_empty_raises(monkeypatch):
    raw = pl.DataFrame({"日期": ["2014-11-17"], "当日成交净买额": [120.0]})
    install_fake_module(monkeypatch, "akshare", {"stock_hsgt_hist_em": CallRecorder(raw)})
    with pytest.raises(IndicatorFetchError, match="window|empty"):
        AkshareIndicatorAdapter().fetch(
            "north_net_buy", date(2024, 1, 1), date(2024, 12, 31)
        )


# ---------------------------------------------------------------------------
# tushare adapter
# ---------------------------------------------------------------------------


def _fake_tushare(monkeypatch, shibor_ret):
    pro = types.SimpleNamespace(shibor=CallRecorder(shibor_ret))
    ts = types.ModuleType("tushare")
    ts.pro_api = lambda token: pro
    monkeypatch.setitem(sys.modules, "tushare", ts)
    return pro


def test_tushare_no_token_raises_with_reason(monkeypatch, no_tushare_token):
    monkeypatch.syspath_prepend  # noqa: B018 (no-op, keep fixture explicit)
    adapter = TushareIndicatorAdapter()
    _fake_tushare(monkeypatch, pl.DataFrame())
    with pytest.raises(IndicatorFetchError, match="token"):
        adapter.fetch("shibor_overnight", date(2025, 9, 1), date(2025, 9, 30))


def test_tushare_no_mapping_raises(monkeypatch):
    _fake_tushare(monkeypatch, pl.DataFrame())
    with pytest.raises(IndicatorFetchError, match="no mapping"):
        TushareIndicatorAdapter(token="fake").fetch(
            "north_net_buy", date(2025, 9, 1), date(2025, 9, 30)
        )


def test_tushare_shibor_mapping_and_frame(monkeypatch):
    raw = pl.DataFrame(
        {"date": ["20250901", "20250902"], "on": [1.9, 1.95], "1w": [2.0, 2.0]}
    )
    pro = _fake_tushare(monkeypatch, raw)
    out = TushareIndicatorAdapter(token="fake").fetch(
        "shibor_overnight", date(2025, 9, 1), date(2025, 9, 30)
    )
    assert pro.shibor.calls == [{"start_date": "20250901", "end_date": "20250930"}]
    assert out.columns == ["trade_date", "value"]
    assert out["trade_date"].to_list() == [date(2025, 9, 1), date(2025, 9, 2)]
    assert out["value"].to_list() == pytest.approx([1.9, 1.95])


# ---------------------------------------------------------------------------
# protocol conformance
# ---------------------------------------------------------------------------


def test_adapters_satisfy_protocol():
    assert isinstance(AkshareIndicatorAdapter(), IndicatorSourceAdapterProtocol)
    assert isinstance(TushareIndicatorAdapter(token="x"), IndicatorSourceAdapterProtocol)
    assert AkshareIndicatorAdapter.name == "akshare"
    assert TushareIndicatorAdapter.name == "tushare"
