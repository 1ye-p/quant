#!/usr/bin/env python
"""External indicator source spike (P2-0).

Probes akshare/tushare real interfaces for candidate indicators:
margin (两融) / northbound (北向) / valuation (估值) / breadth (宽度) /
rates (利率) / fx (汇率).

Design:
- Each probe runs in a worker thread with per-call timeout; single failure
  never aborts the round.
- Outputs structured JSON per probe: status, shape, columns, head rows,
  tail rows, earliest/latest date found, elapsed seconds.
- Historical depth: for full-history interfaces we read min/max date of the
  returned frame; for date-parameterised interfaces we additionally probe an
  early window (~2018) to verify >= 2y depth.
- tushare token: settings/env dual-read (convention of
  cquant.datahub.connectors.tushare_connector). Token value is NEVER printed.

Run: conda run -n cQuanty python scripts/spike/ext_ind_spike.py
"""
from __future__ import annotations

import concurrent.futures as cf
import json
import os
import sys
import time
import traceback
from datetime import datetime

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "..", "python"))

CALL_TIMEOUT = 90  # generous: some akshare endpoints scrape slow pages
EARLY_WINDOW = ("20180101", "20180301")  # depth probe window (~7y ago)
RECENT_WINDOW = ("20250901", "20250920")


# --------------------------------------------------------------------------
def call_with_timeout(fn, /, *args, timeout=CALL_TIMEOUT, **kwargs):
    """Run fn in a worker thread, raise TimeoutError-like dict on timeout."""
    with cf.ThreadPoolExecutor(max_workers=1) as ex:
        fut = ex.submit(fn, *args, **kwargs)
        try:
            return fut.result(timeout=timeout)
        except cf.TimeoutError:
            return {"__spike_error__": f"timeout after {timeout}s"}


def frame_summary(df, max_rows=3):
    """Compact, JSON-safe summary of a returned DataFrame."""
    import pandas as pd  # noqa: F401
    rec = {
        "shape": list(df.shape),
        "columns": [str(c) for c in df.columns],
        "head": df.head(max_rows).to_dict(orient="records"),
        "tail": df.tail(1).to_dict(orient="records"),
    }
    # find a date-like column and report range
    for c in df.columns:
        cl = str(c)
        try:
            if "date" in cl.lower() or "日期" in cl or "日期" in cl:
                if df[c].dtype == object or "datetime" in str(df[c].dtype):
                    s = df[c].astype(str)
                    rec["date_column"] = cl
                    rec["date_min"] = s.min()
                    rec["date_max"] = s.max()
                    break
        except Exception:
            continue
    return rec


def probe(label, fn, /, *args, depth_fn=None, **kwargs):
    t0 = time.monotonic()
    try:
        r = call_with_timeout(fn, *args, **kwargs)
        elapsed = round(time.monotonic() - t0, 2)
        if isinstance(r, dict) and "__spike_error__" in r:
            return {"label": label, "status": "FAIL", "error": r["__spike_error__"],
                    "elapsed_s": elapsed}
        summary = frame_summary(r)
        out = {"label": label, "status": "OK", "elapsed_s": elapsed, **summary}
        if depth_fn is not None:
            t1 = time.monotonic()
            try:
                rd = call_with_timeout(depth_fn)
                if isinstance(rd, dict) and "__spike_error__" in rd:
                    out["depth_probe"] = rd["__spike_error__"]
                else:
                    ds = frame_summary(rd, max_rows=1)
                    out["depth_probe"] = {
                        "shape": ds["shape"],
                        "date_min": ds.get("date_min"),
                        "date_max": ds.get("date_max"),
                        "head": ds["head"],
                    }
            except Exception as e:
                out["depth_probe"] = f"{type(e).__name__}: {e}"
            out["depth_elapsed_s"] = round(time.monotonic() - t1, 2)
        return out
    except Exception as e:
        return {"label": label, "status": "FAIL",
                "error": f"{type(e).__name__}: {e}",
                "trace_tail": traceback.format_exc().strip().splitlines()[-1],
                "elapsed_s": round(time.monotonic() - t0, 2)}


# --------------------------------------------------------------------------
def get_akshare():
    import akshare as ak
    return ak


def get_tushare_pro():
    """Return (pro, ts) or raise. Token via settings/env dual-read, never printed."""
    token = ""
    try:
        from cquant.core.config import settings
        token = settings.tushare_token or ""
    except Exception:
        pass
    token = token or os.environ.get("TUSHARE_TOKEN", "")
    if not token:
        raise RuntimeError("no tushare token (settings/env)")
    import tushare as ts
    ts.set_token(token)
    return ts.pro_api(), ts


def build_probes():
    probes = []
    ak = get_akshare()

    # ---- 两融 ----
    probes.append((
        "ak|margin_sse|两融余额-上交所汇总",
        lambda: ak.stock_margin_sse(start_date=RECENT_WINDOW[0], end_date=RECENT_WINDOW[1]),
        lambda: ak.stock_margin_sse(start_date=EARLY_WINDOW[0], end_date=EARLY_WINDOW[1]),
    ))
    probes.append((
        "ak|margin_szse|两融余额-深交所汇总(单日)",
        lambda: ak.stock_margin_szse(date="20250919"),
        lambda: ak.stock_margin_szse(date="20180301"),
    ))

    # ---- 北向 ----
    def _hsgt_hist():
        return ak.stock_hsgt_hist_em(symbol="北向资金")
    probes.append(("ak|hsgt_hist_em|北向资金-历史净流入(EM)", _hsgt_hist, None))
    probes.append((
        "ak|hsgt_fund_flow_summary_em|北向资金-三日/当日汇总(EM)",
        lambda: ak.stock_hsgt_fund_flow_summary_em(),
        None,
    ))

    # ---- 估值 ----
    probes.append(("ak|market_pe_lg|全A平均PE(乐咕)", lambda: ak.stock_market_pe_lg(), None))
    if hasattr(ak, "stock_a_all_pb_lg"):
        probes.append(("ak|a_all_pb_lg|全A平均PB(乐咕)", lambda: ak.stock_a_all_pb_lg(), None))
    if hasattr(ak, "stock_index_pe_lg"):
        probes.append(("ak|index_pe_lg|指数PE(乐咕,上证)", lambda: ak.stock_index_pe_lg(symbol="上证50"), None))

    # ---- 宽度 ----
    if hasattr(ak, "stock_market_activity_legu"):
        probes.append(("ak|market_activity_legu|市场赚钱效应-上涨下跌家数(乐咕)",
                       lambda: ak.stock_market_activity_legu(), None))

    # ---- 利率 ----
    # rate_interbank signature: (market, symbol, indicator); defaults resolve to
    # Shibor overnight. Custom market strings must match the chinamoney page key
    # exactly (KeyError otherwise), so use defaults.
    def _shibor():
        return ak.rate_interbank()
    probes.append(("ak|rate_interbank_shibor|Shibor隔夜", _shibor, None))
    probes.append(("ak|bond_zh_us_rate|中美国债收益率(10Y)",
                   lambda: ak.bond_zh_us_rate(start_date="20250901"), None))

    # ---- 汇率 ----
    for fname, fn_builder, lbl in [
        ("currency_boc_safe", lambda: ak.currency_boc_safe(), "人民币中间价-SAFE"),
        ("fx_spot_quote", lambda: ak.fx_spot_quote(), "外汇即期报价-CFETS"),
        ("currency_boc_sina", lambda: ak.currency_boc_sina(symbol="美元", start_date="20250901", end_date="20250920"), "中行美元牌价"),
    ]:
        if hasattr(ak, fname):
            probes.append((f"ak|{fname}|{lbl}", fn_builder, None))
        else:
            probes.append({"label": f"ak|{fname}|{lbl}", "status": "MISSING",
                           "error": "function not present in installed akshare"})

    # ---- tushare ----
    try:
        pro, _ts = get_tushare_pro()
        has_token = True
    except Exception as e:
        pro = None
        has_token = False
        probes.append({"label": "ts|__token__", "status": "FAIL",
                       "error": f"token unavailable: {e}"})

    if pro is not None:
        def _ts_wrap(method_name, **params):
            def _f():
                return getattr(pro, method_name)(**params)
            return _f
        ts_probes = [
            ("ts|margin|融资融券交易汇总(单日)",
             _ts_wrap("margin", trade_date="20250919"),
             _ts_wrap("margin", trade_date="20180301")),
            ("ts|moneyflow_hsgt|沪深港通资金流向",
             _ts_wrap("moneyflow_hsgt", start_date=RECENT_WINDOW[0], end_date=RECENT_WINDOW[1]),
             _ts_wrap("moneyflow_hsgt", start_date=EARLY_WINDOW[0], end_date=EARLY_WINDOW[1])),
            ("ts|index_dailybasic|指数估值(沪深300 PE)",
             _ts_wrap("index_dailybasic", trade_date="20250919", fields="ts_code,trade_date,pe,turnover_rate"),
             _ts_wrap("index_dailybasic", trade_date="20180301", fields="ts_code,trade_date,pe")),
            ("ts|shibor|Shibor利率",
             _ts_wrap("shibor", date="20250919"),
             _ts_wrap("shibor", date="20180301")),
            ("ts|shibor_quote|Shibor报价",
             _ts_wrap("shibor_quote", date="20250919"), None),
            ("ts|fx_obasic|外汇基础-美元兑人民币",
             _ts_wrap("fx_obasic", trade_date="20250919"), None),
        ]
        probes.extend(ts_probes)

    return probes, has_token


def main():
    results = []
    probes, has_token = build_probes()
    for p in probes:
        if isinstance(p, dict):  # pre-computed MISSING/FAIL record
            results.append(p)
            continue
        label, fn, depth_fn = p
        res = probe(label, fn, depth_fn=depth_fn)
        results.append(res)
        print(f"[{res['status']:>7}] {label} ({res.get('elapsed_s')}s)", flush=True)

    out_path = os.path.join(os.path.dirname(__file__), "ext_ind_spike_results.json")
    payload = {
        "run_at": datetime.now().isoformat(),
        "akshare_version": __import__("akshare").__version__,
        "tushare_version": __import__("tushare").__version__,
        "tushare_token_present": has_token,
        "results": results,
    }
    with open(out_path, "w", encoding="utf-8") as f:
        json.dump(payload, f, ensure_ascii=False, indent=1, default=str)
    ok = sum(1 for r in results if r["status"] == "OK")
    print(f"\n{ok}/{len(results)} probes OK -> {out_path}")


if __name__ == "__main__":
    main()
