# 退市（Delisting）端到端验证结论

> 验证日期：2026-09-19 ｜ 测试：`python/tests/unit/test_delisting_e2e.py`（4 项，全绿）
> 引擎版本：`cquant.backtest_vector`（VectorBacktestEngine + AShareFillSimulator）

## 结论（一句话）

**中途退市的股票在引擎中的语义是：价格流在最后一个有效交易日自然终止，持仓以"市值核销"（mark-to-zero）退出 NAV，不产生清算 fill，也不以最后价格冻结估值；存活资产 NAV 全程正常。**

## 场景

3 资产、30 个日历日、周度调仓、无风控策略（避免 fired_tiers 干扰）：

- **A**（10 元起，缓涨）：全程存活；
- **B**（20 元起，缓涨）：第 16 日后停止报价（最后数据日 2025-01-17 < 回测 end 2025-01-31）→ 退市特征；
- **C**（30 元起，缓涨）：全程存活。

价格数据经 `run._forward_fill_long_prices` + `run._handle_delisting` 预处理（与生产 run.py 路径一致）后喂给引擎。

## 验证结果

| # | 断言 | 结果 |
|---|------|------|
| 1 | B 在最后数据日之后**无任何 fill**（无买入、无卖出、无清算） | ✅ |
| 2 | NAV 全程为正、覆盖到回测结束附近；A/C 全程有成交 | ✅ |
| 3 | B 的持仓在退市后第一个 NAV 快照处被**一次性核销**（NAV 骤降 ≈ B 的市值占比 ~1/3，上界 40%），核销后 NAV 继续随 A/C 变动（不冻结） | ✅ |
| 4 | 价格矩阵保证：B 的行止于最后有效日（无尾部 forward-fill 合成价）；A 延伸到回测结束 | ✅ |

## 引擎处理链（源码依据）

1. **`run.py::_forward_fill_long_prices`**（约 L53-100 注释 + 实现）：forward-fill 只回填"中间缺口"，**不尾部填充**——退市股票最后有效行之后没有可搬运的行，尾部单元格保持 NULL。
2. **`run.py::_handle_delisting`**（L106-148）：显式契约——每个资产只保留到最后一个非 NULL `close` 的行；退市股**从价格流中消失**，文档原文："*once their last valid price is reached, the asset simply disappears from the price stream, which surfaces as a 0.0 lookup (no-trade) in the FillSimulator, matching the intended 'settle at last valid price then drop' semantics without inventing synthetic prices*"。
3. **`fill_simulator.py::_calculate_sell_qty`**（L380-435）：存在显式的 "Handle delist forced liquidation" 分支——当 `_check_tradability` 返回 `TradabilityReason.DELISTED` 时经 `market_rules.handle_delist` 强平。但该分支要求退市日**当天仍有 bar**（可查 tradability）；本场景 B 退市后连价格行都没有（lookup 为空 → 价格 0），`current_w` 计算为 0，不触发卖出，持仓留在账上、在 `_calculate_nav` 中按价格 0 估值 → **等效于全额核销**。
4. **净效果**：NAV 在退市后第一个快照一次性下跌 ≈ 持仓市值（保守口径：退市清算价值按 0 计，而非按最后价格结算）。对回测而言这是**偏保守（pessimistic）**的处理——真实 A 股退市整理期通常还能收回部分价值。

## 研究员使用提示

- 回测中若持仓股中途退市，组合收益会体现为该股市值的一次性全额损失；评估含退市股的策略时，实际结果大概率**好于**回测（保守偏差）。
- 若需要"按最后价格结算"的口径，可在喂给引擎前给退市股补一行最后有效价的重复 bar（使其触发 `handle_delist` 强平分支）——当前默认路径不这样做（不发明合成价格）。

## 后续可选项（非缺陷）

- 退市整理期折价清算（如按最后价 × 折扣系数核销）——需数据支持退市整理期成交价。
