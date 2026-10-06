# limit_rules 与正确涨跌停语义的分歧目录

> 背景：P1 tradability 向量化（2026-10-05 backtest-perf-batch Task 2）以
> **零语义改动**为红线完成。本文档记录向量化过程中确认的现行行为与
> "正确" A 股涨跌停语义之间的全部分歧，供后续语义批（semantic pass）
> 逐条决策。**在语义批落地前，perf_equiv fixtures 锁定的是下述"现行"
> 行为——修改任何一条都会打破等价门，必须同步重录 fixture。**

现行实现：`cquant/backtest_vector/limit_rules.py`（deprecated 但仍是
`engine._build_tradability_today` 唯一在用的判定路径；向量化版通过
`get_limit_pct` 复用同一板位判定）。

---

## D1. 阈值算术：0.095 vs 0.099（±10% 板）

**现行**：`close >= prev_close * (1 + limit_pct - 0.005)`，即 ±10% 板的
触发带为 **±9.5%**（0.10 − 0.005）。跌停对称：`close <= prev * (1 − 0.10 + 0.005)`
= **−9.5%**。

**问题**：A 股涨跌停价按交易所规则是 `round(prev × 1.1, 2)`（两位小数、
四舍五入），9.5% 的绝对带宽明显过宽——一只 prev=10.00 的股票 11.00 才是
涨停价，但现行逻辑 10.95 即判"接近涨停"。对低价股（prev=2.00，涨停 2.20）
9.5% 带宽 = 1.90–2.20 内全部命中，误差一格价格即 5%。

**正确语义建议**：按交易所规则计算精确涨停价
`limit_up_price = round(prev_close * (1 + pct), 2)`，判定
`close >= limit_up_price`（或带半分钱的容差 `close >= limit_up_price - 0.005`
应对浮点表示）。需要 `prev_close` 精确到分，且分红除权日（adj_factor 变动）
应使用复权前价格判定。

---

## D2. ST 概念缺失：ST 股仍按板位 ±10%

**现行**：`_build_tradability_today` 从不传 `is_st`（`get_limit_pct(aid, False)`），
ST/*ST 股按其板位（主板 ±10%）判定，而真实 ST 股涨跌停为 ±5%。fixture
中 `SSE:ST0001`（±5% 日波动）从不被判涨跌停——锁定该行为。

**问题**：ST 股在真实 ±5% 涨跌停日会被漏判为"可交易"，回测会在实际
一字板无法成交的日期产生虚假成交。

**正确语义建议**：数据层提供 ST 状态（证券简称前缀 / `is_st` 列，PIT
对齐），`get_limit_pct(asset_id, is_st)` 传真实值。需要 silver 层新增
PIT 安全的 ST 标记列。

---

## D3. detect_board 前缀死代码：一切带交易所前缀的 id → MAIN ±10%（Task0 发现）

**现行**：`detect_board` 期望 `SH600xxx` / `SZ300xxx` 风格的 id（前缀 + 6 位
数字），而 silver 层全部 asset_id 为 `SSE:600160` / `SZSE:300622` /
`BSE:920436` 风格（交易所全称 + 冒号）。`asset_id[:2]` = `"SS"` / `"SZ"` /
`"BS"`，永远不匹配 `"SH"`/`"SZ"`/`"BJ"` 分支 → **一切资产落 MAIN ±10%**。
科创板（SSE:688xxx，应 ±20%）、创业板（SZSE:30xxxx，应 ±20%）、北交所
（BSE，应 ±30%）全部按 ±10% 判定。

**问题**：科创板/创业板 19.5%+ 的一字板会被漏判（band 只有 9.5%），
北交所同理；反之对这批板位也不存在误报空间（band 过窄），主要是漏报。

**正确语义建议**：`detect_board` 识别 `SSE:`/`SZSE:`/`BSE:` 前缀
（`asset_id.split(":")[-1]` 取代码段），映射 688→STAR、300/301→ChiNext、
8xx/92x→BSE。与 D1 一并处理（板位 pct 变化会改变所有阈值）。

---

## D4. 一字板判定依赖 `close == high` / `close == low`

**现行**：涨停额外要求 `close == high`（跌停 `close == low`）——浮点精确
相等。复权后（engine 的 close/high 均乘 adj_factor）该相等关系保持，但
任何 vendor 对 high/close 的独立舍入都会使判定失效。

**问题**：语义上想要的是"一字板/封死涨停"（全天无法买入），而 `close == high`
只是它的近似：盘中触及涨停但尾盘回落的"炸板"日（close < high）会被正确
排除，但 close 略低于 high 一分钱的烂板（几乎封死）也会被排除；反之
非涨停日的 close == high（普通光头阳线）若涨幅恰好落入过宽的 D1 带宽
会被**误报**为涨停。

**正确语义建议**：判定应为"收盘价 ≥ 精确涨停价"（D1 修复后 `close == high`
条件即可删除，或保留为可选的严格一字板标记）。`close == high` 与 9.5%
带宽的组合在 D1/D3 修复后基本退化为精确判定的子集。

---

## D5. 除权除息日无保护

**现行**：`prev_close` 取自 engine 的复权价序列（adj_close 或
close × adj_factor）。若 adj_factor 在 td 日变化（分红送转），复权后的
prev_close 与 td 的复权 close 同基，比值不受除权影响——**看似安全**，
但 adj_factor 的粒度/时点取决于 silver 层的复权算法；若 vendor 在除权日
对 open/high/low/close 使用了不同 factor 或 factor 有舍入，`close == high`
（D4）会随机失效。

**正确语义建议**：语义批改为"不复权价格 + 交易所涨停价规则"判定，
`round(prev_raw * (1 + pct), 2)`，与 D1/D3 一并落地。

---

## 修复顺序建议（语义批）

1. D3（前缀解析）+ D1（精确涨停价）——同一处代码，一次改；
2. D2（ST 列，依赖 silver 层 PIT 数据）；
3. D4/D5（close==high 与复权交互，D1 落地后重估是否还需要）。

每一步：先重录 perf_equiv fixture（语义变更=新基线），再改实现，
等价测试同步更新——与本次 P1 的"等价门"流程相同，只是基线换新。
## 周频调仓跨整周假期漏切（2026-10-05 perf 批 P0' 评审 I2）

`_is_rebalance_date`（engine.py）用 `weekday(cur) < weekday(prev)` 推导"每周
首交易日"。跨国庆整周停市（Tue 09-30 → Wed 10-08）时 `2 < 1` 为 False，新一周
的首个交易日**不会**成为调仓日，该周被静默跳过。此为既有引擎语义，P0' 把
`rebalance_frequency` 首次暴露给终端用户后成为可见行为。

- 已由 `python/tests/unit/test_rebalance_frequency_gaps.py` 以 `xfail(strict)`
  钉住（正常周边界仍断言通过）
- 修复方向：交易日历感知的"本周首个交易日"推导，归入语义修正批
