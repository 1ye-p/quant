# 回测配置指南

本篇介绍回测运行参数：日期范围、初始资金、基准、复权价、成本模型与净费模型（FeeModel）。

cQuant 的回测引擎位于 `backtest_vector/`（向量化，基于 vectorbt + Polars），是默认引擎。事件驱动引擎（Rust，`backtest_event/`）用于精细化撮合，两者成本模型保持一致（parity）。

---

## 1. 基本参数

| 参数 | 默认值 | 说明 |
|------|--------|------|
| `start_date` / `end_date` | — | 回测起止日期（必填） |
| `initial_cash` | 1,000,000 | 初始资金（CNY，默认 100 万） |
| `benchmark_asset_id` | `000300.SH` | 基准指数（沪深 300） |
| `dataset_version` | — | 数据集版本（必填，对应摄入返回的 `version_id`） |
| `strategy_id` / 策略配置 | — | 策略（必填） |
| `engine` | `vector` | 引擎：`vector`（向量化）/ `event`（事件驱动）/ `walk_forward`（滚动重训） |

**初始资金优先级**：命令行参数 > TOML 默认值（`configs/defaults/backtest.toml` 的 `[engine].initial_cash`）> 硬编码 100 万。

---

## 2. 日期范围与调仓

- **日期范围**：建议至少覆盖一个完整市场周期（牛+熊），过短的回测易过拟合；
- **交易日历**：cQuant 按 CN/US/HK 交易日历自动跳过非交易日（节假日、周末）；
- **调仓频率**（`rebalance_frequency`）：`1d` / `1w` / `1mo`。引擎只在调仓日生成信号，其余日期仅执行风控（止损/退场）；
- **fill policy**：默认 `next_bar_open`（次日开盘成交），避免用当日收盘价成交造成未来函数。

> 回测结果会记录 `rebalance_dates`（实际调仓日），在 NAV 图上以标记显示。

---

## 3. 基准（Benchmark）

基准用于计算相对指标：**Alpha、Beta、Information Ratio（IR）、Tracking Error（TE）**。

- 默认 `000300.SH`（沪深 300）；
- 市场中性策略建议用中证 500 或沪深 300；
- 行业轮动建议用对应行业指数。

> **Benchmark 空值警告**：若基准数据缺失，UI 会显示警告，相对指标（Alpha/Beta/IR/TE）将无法计算。请确保基准在回测区间内有完整数据。

---

## 4. 复权价

cQuant 统一使用**复权价**进行回测和因子计算，避免分红/送股造成的价格跳空。

- **默认**：前复权（`pre`）；
- **机制**：通过 `adjusted_ohlc_sql` helper 生成复权 SQL，所有回测/因子路径统一调用；启动时检查 `adj_factor` 覆盖率，覆盖率不足会告警；
- **fills 表**：保留 `raw_close`（原始价）列，便于核对。

> 复权正确性有专门测试覆盖（`python/tests/` 下的复权测试）。详见 [FAQ - 复权](faq.md)。

### 4.1 复权口径（精确表述）

实际口径来自 `backtest_vector/prices.py::adjusted_ohlc_sql`（回测与因子物化的唯一共享入口）：

| 字段 | 口径 |
|------|------|
| `open` / `high` / `low` | `原始价 × adj_factor`（前复权） |
| `close` | `COALESCE(adj_close, close × adj_factor)` —— 优先使用供应商 `adj_close`，为空时回落到 `close × adj_factor` |
| `volume` / `amount` | **不复权**（保留真实成交量/成交额，成交量约束按真实盘口执行） |

**分红处理方式**：前复权因子 `adj_factor` 已内含分红除息与送转的影响，即回测价格序列中分红体现为价格因子的平滑调整，**不是**"分红现金到账再投资"的显式建模。若研究需要显式的分红现金流（现金再投资口径），应从 `silver_corporate_actions` 表（`GET /api/v1/datasets/corporate-actions?asset_id=...` 可查单只股票历史）自行构建对照核算。

**除权核对**：当价格序列出现跳空、需要判断是除权还是真实波动时，用同一日期对照 `silver_corporate_actions` 的 `ex_date`（除权除息日）即可区分。

---

## 5. 成本模型（CostModel）

成本模型模拟**每笔成交**的交易成本（与下面的 FeeModel 不同）。定义在 `backtest_vector/costs.py`，CN/US/HK 三套预设。

### 5.1 CN（A 股）默认

| 成分 | 模型 | 费率 | 说明 |
|------|------|------|------|
| 佣金 | `pct` | 0.03%（万分之三） | 双边收取，单笔最低 5 元 |
| 印花税 | `pct` | 0.1%（千分之一） | **仅卖出**收取 |
| 滑点 | `pct` | 0.01%（万分之一） | 双边 |
| 借券 | — | 默认关闭 | 融券/做空时启用 |

### 5.2 US / HK 预设

| 市场 | 佣金 | 印花税 | 做空 |
|------|------|--------|------|
| US | 0.01%，最低 $1 | 无 | 允许 |
| HK | 0.03%，最低 HK$50 | 双边 0.13% | 允许 |

系统根据 `asset_id` 的交易所前缀（`SSE:`/`SZSE:`/`NYSE:`/`HKEX:`）自动检测并选择对应 `CostModel`。

### 5.3 成交量约束

事件引擎支持 `max_volume_share`（单笔订单不超过当 bar 成交量的 5%，默认），模拟市场冲击。向量化引擎支持类似的成交量参与约束。

---

## 6. 净费模型（FeeModel）

`FeeModel`（`backtest_vector/fees.py`）模拟**基金层面**的费用，叠加在毛 NAV 收益之上。这是对冲基金标准的 "2 and 20" 费率模型。

> **区别**：`CostModel` 是**单笔交易**成本（佣金/税/滑点）；`FeeModel` 是**基金 NAV**层面的管理费和业绩报酬。两者独立。

### 6.1 两个费用组件

| 组件 | 字段 | 默认 | 说明 |
|------|------|------|------|
| 管理费 | `mgmt_fee_annual` | 1%（0.01） | 年化管理费，按日扣减（`/252`），无条件收取 |
| 业绩报酬 | `perf_fee` | 0（关闭） | 超额收益的提成（如 0.20 = 20% carry） |

### 6.2 业绩报酬的两种计算约定

| 约定 | `use_hwm` | 说明 |
|------|-----------|------|
| **高水位（HWM）** | `True`（默认，对冲基金标准） | 仅当 NAV 创历史新高时，对新高的部分收业绩报酬；回撤不重复收费，峰值棘轮上升 |
| **简单门槛** | `False` | 每日收益超过 `hurdle/252` 即收业绩报酬 |

| 参数 | 说明 | 示例 |
|------|------|------|
| `hurdle` | 年化门槛率（业绩报酬只对超过门槛的部分收取） | 0.0（无门槛）/ 0.05（5%） |

### 6.3 典型配置示例

**"2 and 20"（2% 管理 + 20% 业绩，HWM，无门槛）**：
```python
FeeModel(mgmt_fee_annual=0.02, perf_fee=0.20, hurdle=0.0, use_hwm=True)
```

**仅 1% 管理费（公募/ETF 思路）**：
```python
FeeModel(mgmt_fee_annual=0.01, perf_fee=0.0)
```

**前端配置**：在回测运行弹窗的「净费模型」区域配置，回测结果会同时展示毛收益和净收益。

---

## 7. 走查（Walk-Forward）与回测模式

| `engine` | 说明 |
|----------|------|
| `vector` | 标准向量化回测（全区间一次性） |
| `walk_forward` | 滚动重训：按窗口切分，逐段训练+预测+回测，叠加结果。用于 ML 策略防止未来函数、评估样本外表现 |
| `event` | 事件驱动（Rust），逐 tick 撮合，精确模拟限价单/滑点 |

`eval_mode`（针对 ML）：`train` / `valid` / `test` / `all`，控制使用哪个数据划分。

---

## 8. 可复现性（随机种子）

cQuant 使用**局部 RNG 透传**（`random_seed` 传入 `BacktestSpec`），消除全局 seed 污染。同一 `random_seed` 下回测结果可精确复现，且并发回测互不干扰。

---

## 9. 异步回测与并发控制

回测通过 API 异步执行（`BackgroundTasks` + `job_id` 轮询）：
1. 提交回测 → 返回 `job_id`；
2. 前端轮询 `/backtests/{job_id}` 获取进度；
3. 完成后查看结果。

系统通过**全局 job 信号量**限制并发回测数，避免资源耗尽。

---

## 10. 相关文档

- [策略配置指南](strategy-config.md)
- [回测分析指南](backtest-analysis.md) — 14 个分析 Tab
- [常见问题](faq.md)
