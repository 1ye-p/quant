# 因子研究指南

本篇介绍 cQuant 因子研究全流程：因子物化 → IC 分析 → 分层收益 → 因子 DSL → 因子评估（含 IC 显著性 / 净 IC / 半衰期）。

因子是量化策略的核心原料。一个好的因子应该具备：**统计显著**（IC 显著不为零）、**单调性**（分层收益单调）、**稳定性**（半衰期合理）、**可交易**（扣费后仍有效，净 IC）。

---

## 1. 因子物化

### 1.1 内置因子库

cQuant 内置因子来自多个来源，合计 500+ 个：

| 来源 | 数量 | 说明 |
|------|------|------|
| qlib Alpha158 | 158 | 经典横截面因子（动量/反转/波动/量价等） |
| qlib Alpha360 | 360 | 360 维快照因子 |
| Alpha101 | 101 | WorldQuant Alpha101 |
| GTJA191 | 191 | 国泰君安 191 因子 |
| 自定义 | 多个 | 含分红事件因子、基本面因子 |

所有内置因子在 `BUILTIN_FACTORS` 中注册，可通过 `FactorRegistry` 统一管理。

### 1.2 CLI 物化

```bash
python -m cquant.cli.main factors \
  --dataset-version tdx_bulk_v1 \
  --start 2024-01-01 --end 2025-12-31 \
  --all
```

物化结果写入 `silver_factor_values` 表，含 `asset_id`、`trade_date`、`factor_name`、`value` 四列。

### 1.3 增量物化（数据指纹）

cQuant 采用**数据指纹**机制：物化前先计算输入数据的指纹（hash），若指纹与上次一致则跳过重算。这使得增量更新（如补充最近一周数据）只需计算新增部分，大幅节省时间。

### 1.4 因子中性化（正交化）

多个因子之间往往高度相关。cQuant 通过 `CrossSectionScorer._neutralize_factors`（截面回归残差投影）在打分阶段对因子做中性化处理：将因子对行业/风格等基础变量回归，取残差作为中性化后的因子值，消除多重共线性，提升多因子模型的稳健性。该残差投影是 cQuant 中性化的唯一实现，可在打分配置中开关。

---

## 2. 因子 DSL（自定义因子）

cQuant 提供因子 DSL（领域特定语言），让你用表达式自定义因子，无需写 Python 代码。

### 2.1 支持的函数（16 个）

| 函数 | 参数 | 说明 | 示例 |
|------|------|------|------|
| `lag` | (col, n) | 滞后 n 期 | `lag(close, 5)` |
| `ma` | (col, n) | 简单移动平均 | `ma(close, 20)` |
| `sma` | (col, n) | 简单移动平均（别名） | `sma(volume, 10)` |
| `ema` | (col, n) | 指数移动平均 | `ema(close, 12)` |
| `std` | (col, n) | 滚动标准差 | `std(close, 20)` |
| `rank` | (col) | 截面排名百分位（1/n ~ 1） | `rank(close)` |
| `delta` | (col, n) | 差分 | `delta(close, 5)` |
| `max` | (col, n) | 滚动最大值 | `max(high, 20)` |
| `min` | (col, n) | 滚动最小值 | `min(low, 20)` |
| `sum` | (col, n) | 滚动求和 | `sum(volume, 5)` |
| `abs` | (col) | 绝对值 | `abs(delta(close,1))` |
| `log` | (col) | 自然对数 | `log(volume)` |
| `sign` | (col) | 符号函数 | `sign(delta(close,5))` |
| `ts_rank` | (col, n) | 时序排名（当前值在过去 n 期的分位） | `ts_rank(close, 20)` |
| `corr` | (col1, col2, n) | 滚动相关系数 | `corr(close, volume, 10)` |
| `cov` | (col1, col2, n) | 滚动协方差 | `cov(close, volume, 10)` |

可用字段：`open`、`high`、`low`、`close`、`volume`、`amount`、`vwap` 等。

### 2.2 DSL 编辑器（Web UI）

在「因子」页面点击「新建自定义因子」，打开 Monaco DSL 编辑器（带语法高亮、自动补全、错误提示）：

![因子 DSL 编辑器](placeholder-factor-dsl.png)

示例：自定义「20 日量价相关因子」
```
corr(close, volume, 20)
```

校验通过后保存，该因子即可用于策略配置和因子评估。

---

## 3. 因子评估

因子评估是判断因子好坏的关键。cQuant 的 `FactorEvaluator`（`factorlab/evaluation.py`）提供一整套评估指标。

### 3.1 核心 IC 指标

**IC（Information Coefficient，信息系数）**：因子值与未来收益的截面相关系数。IC 越高，因子预测能力越强。

| 指标 | 函数 | 含义 | 合格标准 |
|------|------|------|----------|
| Mean IC | `mean_ic()` | 每日 IC 的均值 | 绝对值 > 0.03 |
| IC IR | `ic_ir()` | Mean IC / IC 标准差（信息比率） | > 0.5 |
| IC 正收益率 | `ic_positive_pct()` | IC > 0 的交易日占比 | > 55% |

### 3.2 IC 显著性（Newey-West HAC）

`ic_ttest()` 和 `ic_significant()` 使用 **Newey-West HAC** 估计量计算 IC 的 t 统计量，修正截面 IC 序列的自相关与异方差。

> **为什么重要**：直接用普通 t 检验会因 IC 序列自相关而高估显著性，导致"假因子"。Newey-West 修正后，只有 `|t| > 2` 且 p 值 < 0.05 的因子才算统计显著。

在 Web UI 的因子评估面板会显示 `t_stat` 和 `p_value`，标记因子是否显著。

### 3.3 净 IC（Net IC，扣费后 IC）

`net_ic()` 计算**扣减线性交易成本后**的 IC。净 IC 衡量因子在考虑换手成本后是否仍有预测力。

> 高 IC 但高换手的因子可能"赚的不够赔手续费"。净 IC 是判断因子**可交易性**的关键指标。

### 3.4 半衰期（Half-Life）

`half_life()` 估计 IC 衰减到一半所需的时间（天数）。半衰期反映因子信号的持久性。

| 半衰期 | 含义 | 建议 |
|--------|------|------|
| < 3 天 | 信号衰减极快 | 换手过高，慎用 |
| 3 ~ 10 天 | 短周期因子 | 适合日内/隔夜策略 |
| 10 ~ 30 天 | 中周期因子 | 适合周度/月度调仓 |
| > 30 天 | 长周期因子 | 适合低频策略 |

### 3.5 Rank IC 衰减（IC Decay）

`rank_ic_decay()` 计算因子对未来 1/5/10/20/60 日收益的 Rank IC，绘制衰减曲线。理想因子的衰减应平滑、单调。

在「因子」页面选中一个因子，切换到「IC 衰减」Tab 可查看衰减图。

![IC 衰减图](placeholder-ic-decay.png)

### 3.6 分层收益（Quantile Returns）

`quantile_returns()` 将股票按因子值分成 5 或 10 组（分位），计算各组的等权收益，绘制分层收益曲线。

**判断标准**：
- **单调性**：从 Q1 到 Q5（或 Q10）收益应单调递增（或递减）；
- **多空价差**：Q1 − Q5（做多最高组、做空最低组）的累计收益应为正且稳定；
- **Top 组超额**：最高分位组应显著跑赢基准。

在「因子」页面切换到「分层收益」Tab 可查看累计分层收益曲线和换手率。

### 3.7 因子换手率

`factor_turnover()` 计算因子多空组合的日均换手率。换手率过高意味着交易成本侵蚀收益。

---

## 4. 因子评估完整流程

1. **物化因子**（CLI `factors --all` 或自定义 DSL 因子）；
2. **进入「因子」页面**，选择目标因子；
3. **查看 IC 指标**：Mean IC、IC IR、IC 正收益率；
4. **检查 IC 显著性**：t 统计量 > 2、p 值 < 0.05；
5. **查看净 IC**：扣费后是否仍为正；
6. **查看半衰期**：是否符合策略周期；
7. **查看 IC 衰减**：是否平滑单调；
8. **查看分层收益**：是否单调、多空价差是否为正；
9. **发送到打分**：点击「发送到打分」，进入 ScoringPage 进行多因子合成。

---

## 5. IC 告警

cQuant 支持因子 IC 告警：当因子的滚动 IC 跌破阈值（如 Mean IC < 0.02 或不显著）时，自动触发告警。在「因子」页面可看到告警徽标，在「告警」页面可配置告警规则和通知渠道（邮件/Webhook）。

详见 [常见问题 - 告警配置](faq.md)。

---

## 6. 已知过时文档

上游 `vibe-trading` 的 `agent/src/skills/factor-research/SKILL.md`（:95-103）仍包含
「Orthogonalized Combination」（Schmidt 正交化合成）章节。该路径已从 cQuant 后端
移除——多因子合成统一使用 `CrossSectionScorer._neutralize_factors` 残差投影，
按该章节操作会失败。详见 [KNOWN_ISSUES.md §3](../../KNOWN_ISSUES.md)。

---

## 7. 相关文档

- [快速开始](getting-started.md)
- [策略配置指南](strategy-config.md) — 多因子策略如何使用因子权重
- [回测分析指南](backtest-analysis.md)
- [常见问题](faq.md)
