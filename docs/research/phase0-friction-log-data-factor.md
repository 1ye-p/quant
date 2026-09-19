# Phase 0 摩擦记录 — 数据层 + 因子层（课题 A 走查前半）

> 走查日期：2026-09-19（会话起始 2026-09-06）
> 代理：研究员走查代理（Claude Code）
> 方式：CLI 命令 + curl 调 UI 实际调用的 API（localhost:8000/api/v1）+ 读 UI 源码核对屏幕可达性。未写脚本直连 DuckDB。
> 环境前情：走查开始时 API server **未运行**，由代理自行启动（`uvicorn cquant.api_server.app:app`）。

## 摩擦总表

### Task 1 数据层

| # | 站点 | 摩擦描述 | 级别 | 自救方式 | 研究员可信度问题 |
|---|------|---------|------|---------|----------------|
| 1-1 | CLI status 首跑 | `python -m cquant.cli.main status` 直接崩溃：DuckDB WAL 重放内部错误（catalog.duckdb.wal），同一错误连带 API server 启动失败 | 🔴 | 手工把 `data/catalog.duckdb.wal` 移走备份后恢复（需文件系统操作，研究员不该做） | 平台可用性本身不可靠；WAL 损坏意味着上次写入可能丢数据，无任何提示 |
| 1-2 | CLI 与 API 互斥 | API server 运行时再跑 CLI status 报 `Conflicting lock is held`（DuckDB 单写者），CLI 与 UI 不可同时用 | 🟠 | 先停 API server 再跑 CLI，跑完再启 | status 输出即便能跑也只有数据集计数，不回答覆盖问题 |
| 1-3 | 覆盖查询（日期范围+股票池） | `/datasets` 只给全集 min/max/asset_count（2024-01-02~2025-12-31, 6675 资产）；无任何端点支持"任意日期区间×股票池"的覆盖统计 | 🟠 | 只能写 SQL 直查 silver_prices_1d | 6675 资产里混入 SSE:880xxx（TDX 板块指数，见 1-8），覆盖数字虚高 |
| 1-4 | `/datasets/quality` | recent_assets/daily_coverage/bottom_assets 全部锚定 `CURRENT_DATE`，数据止于 2025-12-31（stale 262 天）→ 这三项恒为 0/空数组 | 🟠 | 无法自救，只能自己算 | null_rate=0 + outlier 4827 条并存但 UI 质量分仍是 100，指标自相矛盾 |
| 1-5 | 质量报告下钻 | `/{version_id}/quality-report` 仅数据集级 score=100/rows/fields；详细 scorer `/quality/{table}` 允许列表是 `silver_daily/silver_fundamentals/...`，**不含真实表名 silver_prices_1d**；`silver_daily` 返回全 0 假报告（吞异常）。"2024 年某 ST 股财务覆盖率"在 UI/API 完全无法得出 | 🔴 | 无（表名白名单就写错了） | 分股票/分年度的财务覆盖不可核实 → 基本面因子样本量存疑无交代 |
| 1-6 | PIT 股票池 | `/datasets/universe/pit?as_of_date=2024-06-28` 返回 count=0；`/universe/stats` 返回 `{}`——上市/退市日期缺失，PIT 功能空转 | 🔴 | 无 | 幸存者偏差无法评估；多因子回测是否包含已退市股完全不可知 |
| 1-7 | 增量摄取进度 | UI（DatasetsPage）有触发按钮 + schedule 状态，但只有 pending/running/success/error 四态 + last_data_date；无 "N/6675" 粒度进度（trigger 返回 `{status:"triggered"}` 即结束） | 🟡 | 只能靠 last_data_date 变化推断 | 摄取失败时只有 last_error 字符串，无部分成功交代 |
| 1-8 | 验证"某日某股除权跳空" | 无 corporate_actions 端点（全 routes grep 无果）、PriceChart 无复权切换；唯一线索是 `/{id}/anomalies` 列涨跌幅>25% 的跳变，但无法区分除权 vs 真实波动；且 top 异常全是 SSE:880xxx 板块指数 | 🔴 | 只能外部查行情软件核对 | 价格未复权/复权方式无交代 → 动量类因子 IC 可信度存疑 |

### Task 2 因子层

| # | 站点 | 摩擦描述 | 级别 | 自救方式 | 研究员可信度问题 |
|---|------|---------|------|---------|----------------|
| 2-1 | IC 计算可达性 | 端到端可用：选物化因子 ret_20d → POST `/factors/analytics/compute`（202 异步）→ 轮询 job → ~10s 出结果（mean_ic 0.164/IR 0.73/465 obs + decay + 分层）。但首次用 `/available` 里的 ROC20 提交即报错"未找到因子…请先在「因子研究」页面物化"，而 **web/src 全库无 materialize 入口**，物化只能靠 CLI `factors` 命令 | 🟠 | CLI `python -m cquant.cli.main factors --all` | IC 0.164 对 20 日动量异常高：样本含 880xxx 板块指数、无涨跌停/停牌过滤说明，结果大概率偏乐观 |
| 2-2 | 因子目录一致性 | `/factors/available` 509 个（alpha360 命名 close_1/ROC20…），`/factors/definitions` 635 个，实际物化进 gold_factor_values 的只有 ~20 个（ret_20d/vol_20d/rsi_14d…，cQuant 命名）。三套名单互不对齐：**available 里没有 ret_20d** | 🟠 | 试错：提交 IC 报错后才知道哪些真的能用 | 目录里的因子≠能算的因子，选择面是幻觉 |
| 2-3 | 策略构建器选不到研究成果 | FactorSelector（`web/src/components/strategies/FactorSelector.tsx:24-25`）唯一数据源是 `/factors/available`——实测保存自定义 DSL 因子（cf_196490c38a）后 `/available` 不含它；物化基础因子 ret_20d 也不在。**IC 验证过的因子和自定义因子都无法进入策略构建** | 🔴 | 无（断链在数据源选择） | 因子研究→策略的闭环断裂，前期 IC 工作无法落地 |
| 2-4 | DSL 自定义因子 | 保存/删除/preview 端点齐全且可用，但 preview 样本取 `CURRENT_DATE-30d`，数据 stale → 永远返回"无样本数据，仅语法验证通过"，**数值预览功能对当前数据失效**；另有两套表达式语法（Qlib 风格 `$close/Ref` 会报语法错，DSL 是小写 `lag/close`），易踩坑 | 🟠 | 语法对不对只有提交才知道 | 自定义因子在进入策略前无法验证数值正确性 |
| 2-5 | IC 排行/汇总 | `/factors/ic-leaderboard` 恒空、`/factors/ic-status` 直接抛错：依赖的 `gold_factor_ic_summary` **表不存在**（IC 结果只写 meta_factor_analytics per-job）。FactorsPage 的 IC 状态面板因此静默空白 | 🔴 | 无 | 跨因子横向对比（课题 A 核心）没有可用视图，只能逐个提交 job 再手抄结果 |
| 2-6 | IC→分层/衰减导航 | quintile/decay/correlation 是 FactorsPage 同级 tab（selection/ic/quintile/correlation/decay），selectedFactor 跨 tab 保持 → IC 结果→看分层 = 1 次点击；但 ICAnalysisTab 内无任何直达链接/按钮，IC 计算结果行与分层视图无关联 | 🟡 | 手动切 tab | — |
| 2-7 | 相关性矩阵 | CorrelationTab 可用（选因子→点计算→热图），但需手动勾选因子组合；`components/charts/FactorCorrelationMatrix.tsx` 存在却未被 CorrelationTab 引用（自绘热图）；无高相关告警、无一键剔除/替代建议 | 🟡 | 肉眼看热图自己取舍 | "两个高相关因子怎么取舍"零支持，纯靠研究员经验 |

## 逐步记录

### Task 1 数据层

**步骤 1 — CLI status**：首跑即崩（WAL 重放 INTERNAL Error，`data/catalog.duckdb.wal` 584 字节，8月22日残留）。自救：`mv catalog.duckdb.wal /tmp/…bak` 后 API 可启动。WAL 修复后重跑 CLI status 又因 API server 持锁失败（DuckDB Conflicting lock, PID 即 uvicorn）。结论：CLI status 在"API 运行中"这一常态下不可用，且其输出也只是数据集条目而非覆盖报告。级别 🔴（首次）/🟠（互斥）。

**步骤 2 — 覆盖查询**：`GET /api/v1/datasets` 正常，返回 2 个 daily_bar 版本（current: 2024-01-01~2025-12-31, 6675 资产, 3,024,951 行, source=tdx）。`GET /datasets/quality?version=tdx_bulk_v1` 给出 n_assets/min_date/max_date/total_rows/outlier_count(4827)，但 recent_assets=0、daily_coverage=[]、bottom_assets=[]（全部锚 CURRENT_DATE-30/90d，数据 stale 262 天）。"任意日期区间×股票池覆盖"无端点支持 → 🟠（SQL 才能算）。

**步骤 3 — 质量报告**：`/{vid}/quality-report` = score 100 / 15 fields / 0 issues + suggestion"Run data quality scorer"。`/datasets/quality/silver_prices_1d` → 400 "not in allowed list"（白名单 silver_daily/silver_fundamentals/silver_stock_info/bronze_daily）；`/quality/silver_daily` 返回 score 0 全空的假报告（吞掉表不存在异常）。QualityReport.tsx 仅渲染 score ring + issues 表 + suggestions，无按股票/按年下钻。"2024 某 ST 股财务覆盖率"不可得 → 🔴。

**步骤 4 — 增量摄取进度**：DatasetsPage.tsx:195-260 有 scheduleStatus（last_status/last_data_date/next_run）+ trigger 按钮；`POST /datasets/schedule/trigger` 返回 `{status:"triggered"}` 后只能轮询 `/datasets/schedule` 四态。无每资产进度（未触发真实摄取，基于代码+响应 shape 判断）→ 🟡。

**步骤 5 — 除权跳空**：全 routes grep 无 corporate_actions/adjust/复权；PriceChart.tsx 无复权切换；唯一间接证据 `/{vid}/anomalies`（change_pct>25% 列表，top 全是 SSE:880xxx 板块指数——顺带证实股票池被指数污染）。无法确认跳空成因 → 🔴，外部工具核对。

### Task 2 因子层

**步骤 1 — IC 计算**：`/factors/versions` 找到唯一 feature_set（df433225…，2024-01-03~2025-12-31, 5300 万行）。用 available 里的 ROC20 提交 → 秒败："未找到因子…请先在「因子研究」页面物化"（web/src 无任何 materialize 调用，提示语指向不存在的人口）。改用物化的 ret_20d → 提交即返回 job_id，~10-12s 完成，summary 含 mean_ic/ir/hit_rate/observations/rank_ic_decay(10 lags)/quantile_returns(5 组)。功能本身达标，但入口断裂 → 🟠。

**步骤 2 — 因子选择体验**：`/factors/available` 509 个（categories: alpha360×360, counting/extrema/quantile/regression/rsi_like/volume/volume_rsi 各 15…）；FactorSelector/FactorsPage 均有搜索框。但名单不含 ret_20d/vol_20d/rsi_14d 等实际可用因子 → 搜索再快也搜不到能跑的 → 🟠。

**步骤 3 — DSL 断链验证（实测）**：`POST /factors/custom` 保存 `walkthrough_test_mom`（cf_196490c38a）成功 → `GET /factors/available` 复查不含它；FactorSelector.tsx:24-25 数据源仅 `factorsApi.getAvailable()` → 策略构建器选不到自定义因子。清理：DELETE 成功。preview 因 stale 数据只做语法验证。→ 断链 🔴 + preview 失效 🟠。

**步骤 4 — IC→分层导航**：FactorsPage.tsx:30/249-279 五个 tab，selectedFactor 贯穿；ICAnalysisTab.tsx 无 navigate/setActiveTab。跳转成本 1 次点击但无引导 → 🟡。

**步骤 5 — 相关性**：`POST /factors/analytics/factor-correlation` 存在；CorrelationTab 需 ≥2 手选因子 + 点计算；charts/FactorCorrelationMatrix 组件闲置；无剔除建议 → 🟡。

**附 — IC 排行**：`/factors/ic-leaderboard` 空、`/factors/ic-status` 报 `Table gold_factor_ic_summary does not exist`（FactorsPage:52 正在用此端点 → UI 静默空白）→ 🔴。

## 统计

- 数据层：8 条 — 🔴×4（WAL 崩溃、质量下钻不可达、PIT 空转、除权不可验证） / 🟠×3（CLI/API 互斥、覆盖区间查询、quality 锚定当前日期） / 🟡×1（摄取进度粗粒度）
- 因子层：8 条 — 🔴×2（策略构建器选不到 IC 验证因子/自定义因子、IC 汇总表缺失排行空转） / 🟠×4（物化无入口、三套因子名单不一致、preview 失效+双语法、IC 结果可信度存疑） / 🟡×2（IC→分层无直达、相关性无取舍支持）

## 研究员视角可信度要点（跨条目）

1. 股票池 6675 含 880xxx 板块指数，所有统计（覆盖、IC）未剔除 → 数字系统性失真。
2. PIT universe 空转 + 无退市数据 → 幸存者偏差无交代。
3. 价格复权方式无交代、除权不可查 → 动量/波动因子 IC 0.164 的可信度存疑。
4. 质量分 100 与 outlier 4827 并存 → 质量报告不能作为数据可信依据。
