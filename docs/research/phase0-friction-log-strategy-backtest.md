# Phase 0 摩擦记录 — 策略与回测层（课题 A 走查后半）

> 走查日期：2026-09-19（会话起始 2026-09-06）
> 代理：研究员走查代理（Claude Code）
> 方式：curl 调 UI 实际调用的 API（localhost:8000/api/v1）+ 读 UI 源码核对屏幕可达性。唯一一次直连 DuckDB 尝试（列物化因子名）被 API 持锁阻断，改用 API 探测。
> 环境前情：走查开始时 API server **再次未运行且 WAL 再次损坏**（与前半 1-1 同一病灶复发），删 `data/catalog.duckdb.wal` 后重启恢复。

## 摩擦总表

### Task 3 策略与回测

| # | 站点 | 摩擦描述 | 级别 | 自救方式 | 研究员可信度问题 |
|---|------|---------|------|---------|----------------|
| 3-1 | 环境 | API server 未运行；启动即崩：WAL 重放 INTERNAL Error（catalog.duckdb.wal，与走查前半 1-1 **同一故障复发**，间隔一次会话） | 🔴 | 手工删 .wal（需文件系统操作） | WAL 反复损坏=未 checkpoint 的写入会丢，回测结果落库可靠性存疑 |
| 3-2 | StrategyBuilder 选因子 | FactorSelector 唯一数据源 `/factors/available`（FactorSelector.tsx:24-25）实测返回 509 个 **Alpha360 命名**因子（close_1/STD20/ROC20…）。构建器默认因子 `ret_20d/vol_20d`（StrategyBuilder.tsx:34）**不在列表里**；实测物化可用的 ret_20d/vol_20d/vol_60d/price_high_20d_ratio/turnover_rate_20d 全都选不到。课题 A 要的"3 动量/波动率因子"在 UI 上**一个都配不出来** | 🔴 | 无（断链继承自 2-3，此处确认对策略配置的实际打击面） | 屏幕上给的因子（STD20 等）实测未物化（`/factors?factor_name=STD20` total=0），选了也跑不通；能跑的又不在列表——选择面双向都是幻觉 |
| 3-3 | 多因子权重 | **UI 配的多因子权重根本不进回测引擎**：`BacktestCreateBody` 只有单一 `sort_factor`（backtests.py:211）；`create_backtest` 取 `factors[0]` 当 sort_factor（backtests.py:285-287）；向量化引擎 MultiFactor 硬编码 `factor_weights={spec.sort_factor: 1.0}`（backtest_vector/run.py:1090）。StrategyBuilder 里精心配的 3 因子权重表 + 相关性提示全是死配置，实际回测=单因子 | 🔴 | 只能走 Scoring 打分（scoring_run_id）路线绕行 | 研究员以为在回测多因子组合，实际只回测了第一个因子——**结果解释与配置不符**，这是最危险的一类静默错误 |
| 3-4 | 运行回测（流程本身） | 端到端可用：POST `/strategies` 建策略 → POST `/backtests` 得 job_id → 轮询 `/backtests/jobs/{id}`，3 次轮询 ~15s 出结果（2024Q1、6675 资产、MultiFactor top10）。异步+轮询设计顺畅 | ✅ | — | 但 job 状态只有 running/completed/failed 三态，无进度百分比；`/jobs/queue` 有 waiting/running 计数而无单 job 进度 |
| 3-5 | 成交明细 | 指标报 `total_trades: 199`，但 `/fills` total=0、`/round-trips` 0 笔——向量化引擎声称持久化 fills（run.py:460 `_persist_fills`）但本次 run 无记录。Fills tab 空表、TradeAnalysisTab 无米下锅 | 🔴 | 无 | 199 笔交易却查无成交：换手率 0.36 与成本核算无法核对，费用模型是否生效不可验证 |
| 3-6 | TCA / 压力测试 | `/tca`、`/stress-test` 均 500 Internal server error（可能与 fills 缺失联动） | 🟠 | 无 | 交易成本只有 metrics 里一个 `cost_model_config: "default"` 字符串，费率/滑点参数无交代 |
| 3-7 | 过拟合分析（PSR/DSR） | 回测完成的自动分析**静默失败**：手动 POST `/analyze` 得到明确报错 `'AnalysisRunSpec' object has no attribute 'portfolio_returns'`（代码 bug），自动路径只写日志不在 job 里报错。`/analysis` 永远 "No analysis found" → OverfittingTab 无数据可显 | 🔴 | 无 | PSR/DSR/CPCV 全链路不可用，"策略可信吗"的第一支柱塌了 |
| 3-8 | 基准 | `benchmark_asset_id` 默认空 → metrics 里 beta/IR/tracking_error/alpha 全 null；UI 只有 `no_benchmark_hint`（"未设置基准"）一句话 | 🟠 | BacktestRunModal 里手填 benchmark id | 跑赢/跑输市场无从谈起；-42.66% 总收益没有对照系 |
| 3-9 | 导出 | `GET /{run_id}/export?format=html` 与 `format=pdf` 均 500。UI Overview 导出菜单里的 PDF 按钮指向该端点=必失败；JSON 导出是前端把已取数据 downloadJson（可用）；Fills tab 有 CSV 按钮但 fills 为空（3-5）。Risk/Tearsheet/TCA/TradeAnalysis 无任何导出 | 🔴 | 无（html/pdf 双挂） | 报告拿不走：给同事/上级审阅只能截图 |
| 3-10 | 13 tab 有用性分诊 | 一眼有用：overview / tearsheet / overfitting(若有数据) / fills(若有数据) / risk / tca / trade-analysis。需要上下文：calendar（日历效应）、advanced（月/周效应混合体）、walkforward。看不懂/错位：model-compare、feature-importance、model-diagnostics 是 ML 实验专属 tab，却挂在每个回测详情下，且把**回测 run_id 当 modelVersion 传**（BacktestFeatureImportanceTab.tsx:9 `modelVersion={selectedId}`）——非 ML 回测点开必空/报错 | 🟠 | 忽略这三个 tab | 12 个常显 tab 里 3 个与当前对象无关且必然空转，稀释注意力；tab 标签 i18n 齐全（zh-CN.json:2344-2357）无空洞键 |
| 3-11 | Walk-Forward 可达性 | 条件显示：仅 `engine === 'walk_forward'` 才显示 tab（BacktestDetailPage.tsx:38-39）；而 engine 是否为 walk_forward 由提交时 RunModal splitMode='walkforward' 决定（BacktestRunModal.tsx:561-567）。普通回测想看 WF = 回列表重新以 WF 模式提交一次（重跑整个回测），无"就地补跑 WF" | 🟡 | 重新提交 WF 模式回测 | WF 配置（n_splits/gap/window_type）在 RunModal 有表单，可达；但"同一策略两种模式结果对比"要手工对两个 run |
| 3-12 | 敏感性分析 | Overview tab 内按钮展开（`showSensitivity` 默认 false，OverviewTab.tsx:35,181），点开→提交 job→轮询。2 次点击可达，路径最短的可信度工具 | 🟡 | — | 依赖 optimize/sensitivity 后端 job（本次未深测数值正确性） |

## 逐步记录

### Step 1 — StrategyBuilder 配置多因子 TopN

读 `web/src/components/strategies/StrategyBuilder.tsx` + `FactorSelector.tsx`，curl `/factors/available`：509 个因子全部 Alpha360 命名（15 类：alpha360/kbar/rolling_*…），不含 ret_20d/vol_20d。Builder 本身能力齐全：8 种策略类型、因子权重表（FactorWeightTable）、≥2 因子时相关性提示（FactorCorrelationHint）、缺失因子处理（含 risk_penalty）、sizer/policy 参数化、快速止损/回撤熔断、市场规则——**表单丰富但喂不进真数据**。探测物化因子：ret_20d/vol_20d/vol_60d/price_high_20d_ratio/turnover_rate_20d 存在，STD20/ROC20/roc_20d/momentum_20d/turnover_rate_60d 均为 0。结论：UI 上"3 动量/波动因子"配置任务失败（3-2、2-3 的实际打击面确认），且配了权重也白配（3-3）。配置体验本身（表单/权重/风控/模板管理 StrategyTemplateManager）是全流程最顺的一站。

### Step 2 — 运行回测（实测跑通）

`POST /api/v1/strategies`（wt_multifac_a，MultiFactor，factors=[ret_20d,vol_20d,vol_60d] weights 0.5/0.3/0.2，stop_loss 8%）→ 201 一发入魂。`POST /api/v1/backtests`（dataset_version=当前 daily_bar version_id，2024-01-01~2024-03-31）→ 0.1s 返回 job_id。轮询 3 次（5s 间隔）~15s completed，**run_id=`2d680e4a-9d56-42e1-9d9c-c57c436f7a1c`**。指标：总收益 -42.66%、Sharpe -0.22、MDD -59.65%、199 笔、37 交易日——收益离谱但流程通。随后发现三处后端塌方：fills 空（3-5）、tca/stress-test 500（3-6）、自动过拟合分析静默失败 + 手动触发实锤代码 bug（3-7）、export 双 500（3-9）。另注意 `create_backtest` 用哨兵值判断"用户是否覆盖"（top_n!=10 等，backtests.py:284-292），显式传默认值会被策略配置覆盖——边角但反直觉。

### Step 3 — 13 tab 有用性

源码分诊结果见 3-10。补充：BacktestModelCompareTab 依赖 compareStore 里预选的模型 id（跨页面状态，回测上下文里无入口教你怎么选）；WalkForwardTab 本身质量好（聚合指标+fold 表+时间轴，216→99 行精炼），问题是入口条件（3-11）。OverfittingTab 结构完整（overfit score 分级、fold 指标卡、CPCV embargo 可调），纯粹被后端 3-7 饿死。i18n 抽查 tab 标签/overfitting/walkforward 键均有实值，未发现空洞键。

### Step 4 — "策略可信吗"最短路径

- PSR/DSR：详情页 1 click 到 overfitting tab（路由 `/backtests/:id/overfitting` 直达，router.tsx:70）——**路径最短但内容不可达**（3-7）。
- Walk-Forward：非 WF run 根本没有该 tab；最短路径=回测列表→重提 WF 模式→新详情页→tab，4 步 + 一次完整重跑。
- 敏感性：Overview 内 2 click（3-12）。
- 基准对照：默认无基准，IR/alpha 恒 null（3-8）。
- 样本量交代：metrics 无交易天数之外的 obs 数/自由度；PSR 所需 N 在 UI 无披露。四根支柱里一根断（PSR）、一根绕（WF）、一根缺基准、一根勉强（敏感性）。

### Step 5 — 导出

API 侧：`/export?format=html|pdf` 双 500（3-9）；fills/round-trips 有端点但数据空。UI 侧：Overview 导出菜单 PDF（指向挂的端点）+ JSON（前端本地可行）；Fills 有 CSV 按钮（数据空）；其余 tab 零导出。 tearsheet 是在线渲染 tab（`/tearsheet` 端点正常返回 risk_series），但无法落成文件带走——"能看不能拿"。

## 与前半走查的衔接

- 2-3（FactorSelector 数据源断链）在此确认为策略层 🔴：默认因子/物化因子均不可选。
- 2-5（IC 汇总表缺失）叠加 3-3（权重死配置）：课题 A "多因子"在**因子选择、因子组合、结果验证**三段全部降级，实际能做的只有"单因子 TopN + 敏感性"。
- 1-1（WAL 损坏）复发（3-1），应升级为平台级 P0 而非一次性事件。
