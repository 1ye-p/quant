# 外部指标数据源 Spike — akshare/tushare 真实接口验证（P2-0）

> 日期：2026-10-02（Task 0 spike，门控产物）
> 环境：conda `cQuanty`，akshare 1.18.55，tushare 1.4.29，真实网络调用
> 结论：**锁定 12 个指标**（akshare 12/12 可用；tushare 仅 shibor 一个接口在本机积分档位可用，其余均"无权限"，全标注"需 token/积分"）。市场宽度候选全部剔除（接口失效），建议由内部 silver 层派生。

## 探测概览

- 脚本：`scripts/spike/ext_ind_spike.py`（一键重跑，逐探测超时+异常捕获，结构化 JSON 输出到 `scripts/spike/ext_ind_spike_results.json`）
- 候选 18 项 × 双源：**11 项 OK / 6 项无权限或频控（tushare 低积分档）/ 1 项接口失效（legu 宽度）**
- 耗时：单接口 0.1–1.3s（currency_boc_safe 6.4s 为离群值，全量历史一次性拉取）

## 成败统计

| # | 候选 | akshare | tushare | 备注 |
|---|------|---------|---------|------|
| 1 | 两融余额-上交所汇总 | ✅ `stock_margin_sse` | ❌ 无权限（`margin`） | ts 需 2000 积分 |
| 2 | 两融余额-深交所汇总 | ✅ `stock_margin_szse`（单日） | 同上 | |
| 3 | 北向资金历史净流入 | ✅ `stock_hsgt_hist_em` | ❌ 无权限（`moneyflow_hsgt`） | EM 源 2014-11-17 起全史 |
| 4 | 北向资金当日汇总 | ✅ `stock_hsgt_fund_flow_summary_em` | 同上 | 仅最近交易日快照，不作历史源 |
| 5 | 全A平均PE | ✅ `stock_market_pe_lg` | ❌ 无权限（`index_dailybasic`） | lg 为**月频**（月末值） |
| 6 | 指数PE（上证50） | ✅ `stock_index_pe_lg` | 同上 | 月频 |
| 7 | 市场宽度（涨跌家数） | ❌ `stock_market_activity_legu` 接口失效 | — | 见"剔除项" |
| 8 | Shibor 隔夜 | ✅ `rate_interbank` | ⚠️ `shibor` 首调成功，复调频控 1次/分钟 | 唯一双源可用项 |
| 9 | 中美国债收益率 | ✅ `bond_zh_us_rate` | — | 含 2/5/10/30Y + 期限利差列 |
| 10 | 人民币中间价 | ✅ `currency_boc_safe`（1994 起） | ❌ 无权限（`fx_obasic`） | 6.4s 较慢 |
| 11 | 外汇即期报价 | ✅ `fx_spot_quote` | — | 实时快照无日期列，非历史序列 |
| 12 | 中行美元牌价 | ✅ `currency_boc_sina` | — | 含央行中间价列 |

### 失败形态（如实记录，不猜不编）

- **tushare 无权限**（4 个接口）：`Exception: 抱歉，您没有接口(margin|moneyflow_hsgt|index_dailybasic|fx_obasic)访问权限`——本机 token 积分档位不足（这些接口需 2000 积分档）。
- **tushare 频控**：`shibor`/`shibor_quote` 复调报"频率超限"，接口原文为 **1 次/分钟**（见 JSON：`频率超限(1次/分钟)`）。适配器若用 tushare shibor 需按此设置串行间隔（保守取更宽如 ≥60s 亦可）；akshare 源无此限制。
- **legu 宽度失效**：`stock_market_activity_legu` → `AttributeError: 'NoneType' object has no attribute 'text'`（乐咕页面改版/反爬，重试 2 次同样失败，安装版本 1.18.55 内无替代 breadth 函数）。
- 曾踩坑（已修复进脚本）：`rate_interbank` 无 `start_date/end_date` 参数（签名 `(market, symbol, indicator)`），自定义 market 串必须与页面 key 完全一致否则 `KeyError`——用默认参数即 Shibor 隔夜全史。

## 锁定首批清单（12 个）

> 字段映射列为"原始列名 → 标准帧 (trade_date, value)"。除注明外均日频、盘后/T+1 晨间发布 → `available_date_rule: B`。金额单位统一换算声明在 unit 列。

| indicator_key | display_name | unit | 类别 | 首选源接口（快照） | 字段映射 | frequency | date_rule | 历史深度 |
|---------------|--------------|------|------|--------------------|----------|-----------|-----------|----------|
| margin_fin_balance_sse | 两融余额-融资余额(沪) | 亿元 | 两融 | ak `stock_margin_sse(start_date,end_date)` | 信用交易日期→trade_date；融资余额(元)→value | D | B | ≥2018（实测 201801 窗口有数），早期可溯至 2010 |
| margin_total_balance_sse | 两融余额-融资融券余额(沪) | 亿元 | 两融 | 同上 | 信用交易日期→trade_date；融资融券余额(元)→value | D | B | 同上 |
| margin_balance_szse | 两融余额-融资余额(深) | 亿元 | 两融 | ak `stock_margin_szse(date)` **单日**，回填需逐日循环 | 融资余额(亿元)→value；date 参数→trade_date | D | B | ≥2018 |
| north_net_buy | 北向资金当日净买入 | 亿元 | 北向 | ak `stock_hsgt_hist_em(symbol="北向资金")` 全史一次拉 | 日期→trade_date；当日成交净买额→value | D | B | 2014-11-17 起（≥2y ✅）。注：2024-08 后披露口径变化，字段仍返回 |
| north_acc_net_buy | 北向资金累计净买入 | 万亿元（实测验证） | 北向 | 同上 | 日期→trade_date；历史累计净买额→value。**注：该列单位为万亿元**（实测 2023-12-29 累计 1.768 vs 当日净买 -5.66 亿元，比值 1e-4；对照公开常识北向累计峰值 ~2.3 万亿）。Task 2 适配器须按此换算到亿元（×1e4）或按原单位落库并在 unit 声明，不得与 `north_net_buy` 的亿元列混用 | D | B | 同上 |
| market_pe_all | 全A平均市盈率 | 倍 | 估值 | ak `stock_market_pe_lg()` 全史 | 日期→trade_date；平均市盈率→value | **M（月末）** | B | 1997-12 起 |
| index_pe_sse50_ttm | 上证50滚动PE | 倍 | 估值 | ak `stock_index_pe_lg(symbol="上证50")` | 日期→trade_date；滚动市盈率→value | **M（月末）** | B | 2005-01 起 |
| shibor_overnight | Shibor隔夜利率 | % | 利率 | ak `rate_interbank()`（默认参=Shibor隔夜全史）；备选 ts `shibor(date)`【需 token，1次/min】 | 报告日→trade_date；利率→value | D | B（T+1 晨间发布） | 2006-10 起 |
| cn_gov_yield_10y | 中国10年期国债收益率 | % | 利率 | ak `bond_zh_us_rate(start_date=...)` | 日期→trade_date；中国国债收益率10年→value | D | B | ≥2018（深度探针实测：20180102 起 2333 行日频，CN10Y 非空 2186 行，证据见 JSON supplements） |
| cn_yield_curve_10y2y | 期限利差(10Y-2Y) | pct | 利率 | 同上 | 日期→trade_date；中国国债收益率10年-2年→value | D | B | 同上 |
| usd_cny_parity | 美元兑人民币中间价 | CNY/100USD | 汇率 | ak `currency_boc_safe()` 全史（慢 6.4s）；备选 ak `currency_boc_sina` 央行中间价列（区间快） | 日期→trade_date；美元→value | D | B（T+1 晨间公布） | 1994-01 起 |
| usd_cny_boc | 中行美元折算价 | CNY/100USD | 汇率 | ak `currency_boc_sina(symbol="美元",start_date,end_date)` | 日期→trade_date；中行折算价→value | D | B | 区间参数可回溯（实测窗口正常） |

**candidates 优先序**：每个指标 akshare 为首选（免 token、无限频）；tushare 仅 `shibor_overnight` 有备选（ts `shibor`，需 token 且 1 次/min 频控）；两融/北向/估值/汇率的 tushare 候选全部标注 **"需 2000 积分 token，本机不可验证"**。

### 样例行对照（映射正确性证据，节选）

1) `stock_margin_sse` 原始行：`{"信用交易日期":"20250919","融资余额":1207639982663,...}` → 标准帧 `{trade_date:"2025-09-19", value:12076.4}`（元→亿元 /1e8）。
2) `stock_hsgt_hist_em` 原始行：`{"日期":"2014-11-17","当日成交净买额":120.8233,...}` → `{trade_date:"2014-11-17", value:120.8233}`（已是亿元）。注意同行 `历史累计净买额:0.01208233` 单位为**万亿元**（见 north_acc_net_buy 行注记），不能直接当亿元落库。
3) `rate_interbank` 原始行：`{"报告日":"2006-10-08","利率":2.1184}` → `{trade_date:"2006-10-08", value:2.1184}`（已是 %）。
4) `currency_boc_safe` 原始行：`{"日期":"1994-01-01","美元":870.0}` → `{trade_date:"1994-01-01", value:870.0}`（CNY/100USD）。
5) `bond_zh_us_rate` 原始行：`{"日期":"2025-09-01","中国国债收益率10年":1.8257}` → `{trade_date:"2025-09-01", value:1.8257}`（已是 %）。

## 剔除项及原因

| 候选 | 原因 | 处置建议 |
|------|------|----------|
| 市场宽度-涨跌家数（`stock_market_activity_legu`） | 接口失效（页面改版，`NoneType.text`），版本内无替代 | **由内部 silver 层派生**（全 A close vs prev close 计数），不占外部指标位 |
| 北向当日汇总（`stock_hsgt_fund_flow_summary_em`） | 仅返回最近交易日快照，非历史序列 | 仅可用于"最新值"刷新，不做回填；`north_net_buy` 已覆盖历史 |
| 外汇即期报价（`fx_spot_quote`） | 无日期列（实时快照），且假日 USD/CNY 返回 NaN | 剔除；汇率用 `usd_cny_parity`/`usd_cny_boc` |
| ts `margin` / `moneyflow_hsgt` / `index_dailybasic` / `fx_obasic` | 本机 token 积分不足，无访问权限（未验证数据形态） | 文档保留接口名，适配器实现为"tushare 备选（需 2000 积分）"，不进首批主路径 |
| ts `shibor_quote` | 1 次/分钟频控，且与 `shibor` 重复 | 用 `shibor` 即可 |

## 频控与刷新参数建议（给 Task 3）

- akshare：全部实测接口连续调用无频控；单次 0.1–1.3s。串行间隔 ≥1s 即安全；`currency_boc_safe` 全史拉取约 6.4s，建议仅回填期调用，增量刷新改用 `currency_boc_sina` 区间参数。
- tushare：本机档位 `shibor` 1 次/分钟——**tushare 路线刷新间隔必须 ≥1 分钟设置**，或干脆首批全部走 akshare。
- `stock_margin_szse` 单日一次调用：回填 2 年 ≈ 480 次调用 × 0.13s ≈ 1 分钟，串行 + 0.5s 间隔即可。

## 可复现

```bash
conda run -n cQuanty python scripts/spike/ext_ind_spike.py
# 每探测一行状态 → scripts/spike/ext_ind_spike_results.json（含列名/head 样例/日期范围/深度探针）
```

红线遵守：仅新增 spike 文档 + 脚本 + 结果 JSON；未改任何生产代码；tushare token 值全程未打印（仅记录存在性 True/False）。

## 附录：真实环境手动拉通记录（P2 验收）

- 日期：2026-10-02；环境：conda cQuanty，真实 akshare 公开接口（`rate_interbank`），真 AkshareIndicatorAdapter 生产代码路径
- 隔离：tmp catalog（`/tmp/ext_ind_pullthrough/pullthrough.duckdb`），未触碰 `data/catalog.duckdb`；tmp 库无行情，按设计种入一行锚定价格（`silver_prices_1d` max(trade_date) = 当日），否则刷新窗口拒绝猜测（NoSourceReadyError，已实测该保护生效）
- 指标：`shibor_overnight`（akshare 全史一次拉，非 szse 逐日循环）

### 执行记录（TestClient，真路由真适配器）

| 步骤 | 调用 | 耗时 | 结果 |
|------|------|------|------|
| 1. enable | `POST /api/v1/datasets/external-indicators/builtins/shibor_overnight/enable`（backfill_start=近 1 年） | 5.1s | 200；目录行 builtin/enabled；回填 summary ok |
| 2. 回填入库 | （enable 内 `_run_builtin_backfill`，真网络） | — | 247 行，范围 2025-10-09 → 2026-09-30 |
| 3. 手动增量 | `POST /api/v1/datasets/external-indicators/refresh`（keys=[shibor_overnight]） | 1.7s | 200；ok；UPSERT 幂等（总行数 247 不变） |
| 4. 管理页可见性 | `GET /api/v1/datasets/external-indicators/catalog` | — | `latest_trade_date=2026-09-30`、`stale=false`、`last_status=ok`、`source_name=akshare`（tushare 无 token 回落 akshare，与设计一致） |

### 最新数据日推进对比

| 阶段 | 行数 | 最新数据日 |
|------|------|-----------|
| 回填后 | 247 | 2026-09-30（源端最新交易日） |
| 增量后 | 247 | 2026-09-30（窗口 max−4..锚定日 重叠 UPSERT，幂等无重复） |

结论：enable→回填→手动增量→管理页可见全链真实拉通，验收第 2 条由 partial 补齐为完成。

备注（非本验收缺陷）：tmp DuckDB 在 `conda run` 硬退出下 WAL 未 checkpoint，进程内断言全部通过；跨进程复验需同进程执行（脚本 `/tmp/ext_ind_pullthrough.py` 模式）。

---

## 附录二：L2 custom_http 真实公开 API 全流程（P3 验收）

> P3 最终评审 FIX FIRST Item 1（验收 #2）：真实公开 JSON API 全流程
> 配置 → Test 样例 → 保存 → 手动刷新 → 数据可见 → DSL regime 引用跑通。
> 执行日 2026-10-02；单进程 TestClient（沿附录一模式，tmp DuckDB
> `/tmp/ext_ind_p3_catalog.duckdb`，不碰真实 `data/catalog.duckdb`）；
> 脚本 `/tmp/ext_ind_p3_realapi.py`，结果 JSON `/tmp/ext_ind_p3_result.json`。

### API 选型

| 候选 | 结论 |
|------|------|
| **NBP 波兰央行 Table A（选用）** | `https://api.nbp.pl/api/exchangerates/rates/a/usd/last/30/?format=json` — 免 key、HTTPS、返回 `rates[*]` 数组且每条记录自带 `effectiveDate`（日期）与 `mid`（数值），与 `ExtractionSpec`（records_path + field_map 需 record 内含 trade_date/value）天然匹配 |
| open.er-api.com / Frankfurter | `rates` 为扁平 `{币种: 数值}` 字典，记录内无日期字段，field_map 无法映射 trade_date（结构不匹配，非可达性问题） |

### CustomHTTPConfig（关键差异：`date_param_style='none'`，记录自带日期）

```json
{
  "method": "GET",
  "url_template": "https://api.nbp.pl/api/exchangerates/rates/a/usd/last/30/?format=json",
  "date_param_style": "none",
  "extraction": {
    "type": "jsonpath",
    "records_path": "$.rates[*]",
    "field_map": {"trade_date": "effectiveDate", "value": "mid"}
  }
}
```

目录行：`indicator_key='nbp_usd_pln'`，`frequency='daily'`，
`available_date_rule='B'`（次日可查），`backfill_start='2026-08-01'`。

### 各步输出 / 耗时 / 行数

| 步骤 | 端点 | 结果 | 耗时 |
|------|------|------|------|
| 前置 | — | tmp catalog 播种 `silver_prices_1d` 51 个交易日（锚定日 2026-10-02，兼作覆盖检查交易日基准） | 1.07s |
| 1. Test 样例 | `POST /external-indicators/test` | 200；`rows_parsed=30`，`field_map_hit=true`，样例首行 `{trade_date: 2026-08-21, value: 3.6839}`（真实 guarded_fetch 拉取+解析，不写库） | 0.83s |
| 2. 保存 | `POST /external-indicators/catalog` | 201；`source_type='custom_http'`，`enabled=true` | 0.05s |
| 3. 手动刷新 | `POST /external-indicators/refresh {keys:[nbp_usd_pln]}` | 200；`status='ok'`，`source='custom_http:nbp_table_a_usd'`，`rows_fetched=30 / rows_upserted=30`，窗口 `[2026-08-01, 2026-10-02]`（首刷无数据自动回填窗口） | 0.89s |
| 4. 数据可见 | `GET /catalog/{key}` + 列表端点 | 详情 200（`last_status='ok'`，preview 30 行）；列表 `latest_trade_date=2026-10-01`，`stale=false`；库内 30 行，`[2026-08-21, 2026-10-01]`，rule B 下 `min(available_date)=2026-08-24` | 0.04s |
| 5. DSL regime 引用 | `check_regime_indicator_coverage`（等价回测创建预检路径） | `regime.indicators = {"usdpln": "nbp_usd_pln"}`，区间取首个可见日..最新数据日 `[2026-08-24, 2026-10-01]` → **warnings = []**（覆盖完整时为空）；对照：起点提前到首个数据日 2026-08-21 → 命中 `首个可见日 2026-08-24 晚于区间起点` warning（语义非空验证） | 0.01s |

注：单 key 详情端点不回 `latest_trade_date`（新鲜度在列表层实时计算），故第 4 步以列表端点补证。

### spec §13 偏差记录

spec（`docs/superpowers/specs/2026-09-30-external-indicator-automation-design.md`，用户未跟踪文件，原文不编辑）§13 表述为「DB 不存明文密钥」；P3 实现为**明文可入库 + 保存时密钥启发式告警（`_warn_on_plaintext_secrets`，仅 warn 不拒绝）+ 回显四面脱敏（`redact_config`，GET/PATCH/CREATE 回显统一过）+ `${ENV_VAR}` 引用渲染为推荐路径（入库存引用原文，仅请求构建时在 guard 层渲染）**——plan Task 1 已明示该决策。本注记为 committed 事实记录，spec 原文由维护者自行同步。
