# cQuant Changelog

所有重要变更按版本和日期记录在此文件中。
格式参考 [Keep a Changelog](https://keepachangelog.com/zh-CN/1.0.0/)。

---

## [Unreleased]

### 引擎修复 B2：universe resolver 锚定 + 指数排除，IC 过滤 — 2026-09-23

**修复（高危 B2，联合评审发现）**
- `backtest_vector/universe.py` resolver 不再锚定 `CURRENT_DATE`，改为锚定数据最新 `max(trade_date)` 回溯 30 天（与 datasets.py 同族修法）：stale 数据下 sse/cyb 等预设不再静默解析为空
- 默认 universe（"all"/未知预设）从「返回 None 不过滤」改为返回排除板块指数（880xxx/881xxx）的资产列表；prefix/like 预设同样排除指数——显式包含指数仍通过 `idx_*` 预设（index 类型）opt-in，不受影响
- IC 计算（`api_server/routes/factors.py` `_compute_ic` / `_compute_ic_matrix`）在因子值侧排除板块指数，IC 样本不再被指数污染
- 指数排除谓词抽为公共常量 `cquant.datahub.universe.INDEX_EXCLUSION_SQL`（原私有 `_INDEX_EXCLUSION` 保留 alias），引擎侧 import 复用，防 datahub/引擎两层漂移

**⚠️ 结果修正说明**
- 此前默认 universe 的回测与 IC 计算结果被板块指数污染；本修复后的结果变化属**修正**，历史结果不做"顺带修正"

### Web UI 全功能增强 Batch 3 — 2026-05-15

**AI 会话历史侧边栏**
- `AdvisorPage` 左侧新增 `SessionSidebar`：显示历史会话列表（预览文本 + 轮数 + 时间），点击切换，支持新建会话
- 会话 ID 显示在右上角，方便追踪
- 多角色面板与单次对话模式均支持会话持久化

**OverviewPage 完全重构为仪表盘**
- 4 个统计卡片（数据集版本 / 回测记录 / 知识库文档 / 活跃策略）
- 8 个快捷入口卡片（所有功能页面一键直达）
- 最近回测列表（含状态徽章 + 跳转链接）
- 最近入库文档卡片（最多 6 条）
- 底部系统信息横幅（版本 + 测试状态 + API 地址）

### Web UI 全功能增强 Batch 2 — 2026-05-15

**PnLChart 通用图表组件**
- `web/src/components/charts/PnLChart.tsx`：基于 lightweight-charts (TradingView)，NAV折线 + 回撤面积双轴，支持 ResizeObserver 自适应宽度

**BacktestsPage 深度升级（3-Tab 设计）**
- 概览 Tab：PSR / DSR / 过拟合评分 + 分析摘要卡片
- Tearsheet Tab：PnLChart NAV&回撤曲线 + 原始 JSON 预览
- 过拟合分析 Tab：OverfitScore 进度条 + Walk-forward OOS Sharpe 柱状图（颜色编码正负）+ CPCV 窗口颜色格 + 多重检验修正结果

**LivePage 升级**
- 替换 Recharts 为 PnLChart（lightweight-charts），更专业的金融图表体验

### Web UI 全功能增强 Batch 1 — 2026-05-14

**技术升级**
- 引入 Tailwind CSS 3.4 + PostCSS（替换所有 inline style）
- 新增依赖：`lightweight-charts`（TradingView级PnL图）、`@monaco-editor/react`（VS Code同款策略编辑器）
- 新增 SSE hook `useAdvisorStream.ts`（多角色面板流式更新）
- 新增 `useJobPoller.ts`（异步 Job 轮询）

**后端新增（47条路由）**
- `routes/news.py`：新闻事件浏览（/events, /events/{id}, /stats）
- `routes/strategies.py`：策略配置 CRUD（5条路由，存 meta_strategy_configs）
- `routes/ml.py`：ML 实验/Job（experiments, jobs, feature-importance）
- `routes/live.py`：实盘模拟展示（strategies/pnl/positions/risk）
- `routes/factors.py`：新增 definitions + 异步 IC/IR 计算 + job 查询
- `routes/backtests.py`：新增 tearsheet + validation-windows + multiple-testing
- `routes/advisor.py`：新增 SSE stream + session history + agent outputs
- `sql/duckdb/meta.sql`：3 张新表（meta_strategy_configs / meta_factor_analytics / meta_ml_jobs）

**前端新增（10个页面，总计导航分组化）**
- `FactorsPage`：因子定义卡片 + IC/IR 异步计算 + Recharts 折线图
- `StrategiesPage`：策略列表 + Monaco Editor JSON 编辑器 + CRUD
- `MLLabPage`：Experiment 对比表格 + Job 提交表单 + 特征重要性水平柱状图
- `NewsPage`：新闻时间线 + 情绪颜色点 + 资产徽章 + 来源/类型过滤
- `LivePage`：⚠️ 模拟展示警告 + 策略选择器 + 风险仪表盘 + 时序图
- `AdvisorPage`（深度升级）：多角色面板（Research/Risk/Debate Agent 各自卡片）+ SSE 流式输出 + Report Writer 全宽区域 + 单次对话切换

### Phase 2 进行中

**Step 10 — `python/cquant/bt_analyzer`**
- `AnalysisSpec`：过拟合分析全局配置（OOS窗口数、CPCV splits、benchmark Sharpe、n_trials）
- `SharpeMetrics`：PSR（Probabilistic Sharpe Ratio）+ DSR（Deflated SR）— 基于 Bailey & Lopez de Prado (2012)；自相关调整有效样本量 T_eff、偏度/峰度高阶矩修正
- `WalkForwardAnalyzer`：滑动 OOS 窗口，每窗口计算 Sharpe/return/drawdown/IR
- `CPCVAnalyzer`：组合净化交叉验证（Lopez de Prado Ch.12），产出 C(n,k) 个独立评估
- `MultipleTestingCorrector`：Bonferroni / BHY（FDR控制）/ Bailey-Lopez 三种修正
- `SensitivityAnalyzer`：滚动 Sharpe 变异系数作为参数稳定性近似
- `StabilityAnalyzer`：时段分割 + 牛/熊/高低波动率 regime 分桶
- `AnalysisEngine`：统一入口，生成 `AnalysisReport`（含 overfit_score、summary 供 ai_advisor 消费）
- `sql/duckdb/analysis.sql`：gold_bt_analysis_runs / gold_bt_validation_windows / gold_bt_multiple_testing DDL
- `backtest_vector/metrics.py` bugfix：`cummax()` → `np.maximum.accumulate()`，补充 numpy import

#### Added — 2026-05-11 (Step 8+9)

**Step 8 — `python/cquant/ml_lab`**
- `Trainer` ABC + `ModelArtifact` dataclass（可序列化模型元数据）
- `XGBTrainer`：XGBoost 回归训练器，Polars → numpy 内部转换，保存 `.json` artifact
- `LGBMTrainer`：LightGBM 回归训练器，保存 `.txt` booster artifact
- `MLDataset`：从 `FeatureSetVersion`（内存）或 DuckDB `gold_factor_values`（持久化）加载特征矩阵，时序 train/valid/test split
- `WalkForwardValidator`：n_splits × rolling train/valid 分割，支持 gap_days 防 lookahead
- `ExperimentTracker`：MLflow 封装，自动降级（无 server 时 no-op），settings.mlflow 读取 URI/实验名
- 辅助函数：`infer_feature_names`、`frame_to_matrix`、`target_to_vector`、`regression_metrics`

**Step 9 — `python/cquant/newsflow`**
- `NewsConnector` ABC（asyncio，backfill + poll 双模式）
- `NewsSpec` + `RawNewsEnvelope` 数据契约
- `SinaFinanceConnector`：Sina 财经 Roll API 轮询
- `EastmoneyConnector`：东方财富公告 API 轮询
- `RSSConnector`：通用 RSS/Atom 解析（stdlib xml，无需 feedparser）
- `NewsNormalizer`：raw envelope → silver_news_events schema，自动 CJK ticker 提取（6位代码）
- `PITGate`：点时可用性过滤（available_at <= query_time）
- `NewsIngestionOrchestrator`：async fan-in + dedup + DuckDB 写入，sync 包装支持 Jupyter
- `sql/duckdb/news.sql`：silver_news_events DDL（含 dedupe_key UNIQUE 约束）
- Catalog DDL 列表新增 `news.sql`

**辅助**
- `environment.yml` 新增 `mlflow>=2.14`、`pydantic-settings>=2.0`

**Step 11 — Rust workspace 骨架**
- `rust/Cargo.toml`：workspace（4 个 crate），Edition 2021，workspace dependencies（pyo3/serde/rust_decimal）
- `rust/crates/cquant-core`：基础类型（Instrument, Exchange, AssetClass, OrderSide, OrderType, Market, Price, Timestamp）
- `rust/crates/cquant-event-engine`：BarEvent, OrderIntent, FillEvent, EventEngine trait, MarketReplay 骨架
- `rust/crates/cquant-portfolio`：PortfolioStateMachine + costs.rs（CostModel，与 Python 完全 parity）+ risk.rs（RiskStateMachine trait + SimpleRiskStateMachine）
- `rust/crates/cquant-py`：PyO3 bindings（PyCostModel, PyRiskSnapshot, cost_model_cn/us/hk）
- `rust/pyproject.toml`：maturin 构建配置
- `rust/crates/cquant-portfolio/src/costs.rs` 包含 8 个 Rust 单元测试，与 Python test_costs.py 完全对应

**Step 12 — riskguard Rust 强化**
- `python/cquant/riskguard/bridge.py`：RustRiskBridge，duck-type 探针加载 cquant_py，无 Rust wheel 时自动降级 Python fallback
- `python/cquant/backtest_event/__init__.py` + `engine.py`：EventBacktestEngine 外观，Rust 不可用时委托给 VectorBacktestEngine，保持稳定 API

**Step 13 — 跨引擎 parity 测试框架**
- `python/tests/parity/test_cost_parity.py`：8 个 Python ↔ Rust CostModel parity 测试，importorskip 自动跳过未编译 wheel
- `python/tests/parity/test_engine_parity.py`：vector vs event 引擎 parity 框架，fallback 模式验证 + Rust 就绪后的 TODO 扩展点

**Phase 2 完成**

### Phase 3 进行中

#### Added — 2026-05-11 (Step 14)

**Step 14 — `python/cquant/knowledge_base` MVP**
- schemas：IngestRequest / LoadedDocument / DocumentMeta / SearchQuery / SearchHit / SearchResponse
- 四个文档加载器：PDF（pdfplumber+PyMuPDF双引擎）/ URL（httpx+trafilatura）/ Markdown（frontmatter解析）/ Tabular（CSV/Excel+摘要文本）
- TextChunker：markdown heading感知 + 固定窗口降级，支持重叠
- EmbeddingProvider ABC + NullEmbeddingProvider（零向量，不阻塞全文检索）
- KBFilesystem：raw_ingested / processed / by_type 三层不可变目录
- KBCatalog：DuckDB kb_* 表 CRUD，content_hash 去重，search 日志
- VectorStore ABC + LanceVectorStore（lancedb 未安装时 no-op 降级）
- HybridSearch：RRF 融合（keyword 35% + semantic 45%），无向量时自动 keyword-only
- KnowledgeBaseService：统一外观，工厂方法 `create()`
- `sql/duckdb/knowledge.sql`：16 张 kb_* 表 DDL
- bugfix `datahub/catalog.py`：_split_statements 正则移除 SQL 注释后再按分号分割

**Step 15 — `python/cquant/ai_advisor`（完整版）**
- `providers/base.py`：LLMProvider ABC + Message + ModelResponse + FallbackProvider + async→sync 安全包装（Jupyter 兼容）
- `providers/claude.py`：ClaudeProvider（Anthropic SDK，读取 ANTHROPIC_API_KEY，model 默认 claude-opus-4-6，无 key 时 graceful degradation）
- `providers/openai_provider.py`：OpenAIProvider（备用，读取 OPENAI_API_KEY，model 默认 gpt-4o）
- `policies/safety.py`：SafetyPolicy — FORBIDDEN_TOOLS deny-by-default + response 正则扫描，阻止任何 live-trading 指令
- `tools/base.py`：AdvisorTool ABC + ToolContext（双重 safety gate：authorize + read_only 检查）+ ToolResult
- 8 个工具：KnowledgeSearchTool / ReportSummaryTool / EntityRelationTool / SimilarDocumentsTool / BacktestResultTool / AnalysisReportTool / RiskSnapshotTool / BacktestRunTool
- `agents/base.py`：AgentRole ABC + LLMRole（工具调用、6轮历史、响应裁剪）+ 正则提取 run_id/doc_id/strategy_id
- 5 个 Agent：ResearchAgent / RiskAgent / ExecutionAgent（offline-only）/ DebateAgent / ReportWriterAgent
- `context/rag.py`：RAGContext 3层构建（L1 元数据摘要 / L2 检索 chunks / L3 工具列表）
- `orchestrator.py`：AdvisorOrchestrator — chat() + generate_report() + chat_sync()；路由 risk/execution 分支；fallback markdown（无 API key 时）；最终 SafetyPolicy.validate_response()
- `knowledge_base/store/vector_lance.py`：补充 similar_to_document() 方法（centroid 搜索）
- 新增 10 个单元测试（SafetyPolicy / FallbackProvider / ToolContext 安全门）

**Step 16 — `python/cquant/api_server`**
- `app.py`：FastAPI 应用工厂 `create_app()`，CORS + 全局异常处理，docs 在 `/api/docs`
- `deps.py`：FastAPI 依赖注入（Catalog / KnowledgeBaseService lru_cache 单例）
- `routes/health.py`：`GET /health`
- `routes/datasets.py`：`GET /api/v1/datasets` + `/{version_id}`
- `routes/factors.py`：`GET /api/v1/factors` + `/versions`
- `routes/backtests.py`：`GET /api/v1/backtests` + `/{run_id}` + `/{run_id}/analysis` + `/{run_id}/risk`
- `routes/knowledge.py`：`POST /api/v1/knowledge/ingest` + `/search` + `GET /docs` + `/docs/{doc_id}`
- `routes/advisor.py`：`POST /api/v1/advisor/chat` + `/report` + `DELETE /sessions/{session_id}`（内存 session 管理）
- `routes/plugins.py`：`GET /api/v1/plugins`（从 Registry 动态读取）
- 共 21 条路由，全部验证通过

**Step 17 — `web/`（React Web Dashboard 骨架）**
- `package.json`：React 18 + Vite 5 + TypeScript + TanStack Query v5 + React Router v6 + Recharts + Zod
- `vite.config.ts`：路径别名 `@/` + proxy `/api → http://localhost:8000`
- `src/lib/api.ts`：全量 API client（datasetsApi / backtestsApi / knowledgeApi / advisorApi）+ 类型定义
- `src/lib/queryKeys.ts`：TanStack Query key 工厂
- `src/components/layout/AppLayout.tsx`：左侧导航栏 + Outlet
- `src/components/ui/StatusBadge.tsx`：状态徽章组件
- `src/pages/OverviewPage.tsx`：总览仪表盘（统计卡片 + 最近回测）
- `src/pages/DatasetsPage.tsx`：数据集版本列表
- `src/pages/BacktestsPage.tsx`：回测运行列表 + 状态徽章
- `src/pages/KnowledgePage.tsx`：知识库混合搜索 + 文档列表
- `src/pages/AdvisorPage.tsx`：AI 研究助手聊天界面（多轮对话，session 持久化）
- `src/router.tsx`：BrowserRouter 配置（5 个页面）
- `src/main.tsx`：React 应用入口

**Phase 3 全部完成 🎉**

### 启动方式
```bash
# 后端
conda activate cQuanty
uvicorn cquant.api_server.app:app --host 0.0.0.0 --port 8000 --reload

# 前端
cd web && npm install && npm run dev
# 访问 http://localhost:3000
```

---

## [0.1.0] — 2026-05-11

### Phase 1 完成（项目骨架 + 核心量化模块）

#### Added

**Step 1 — 环境初始化**
- `environment.yml`：conda cQuanty 环境定义（Python 3.12 + 核心依赖）
- `pyproject.toml`：hatchling 构建后端，ruff/mypy/pytest 配置
- `.gitmodules`：Rust submodule 占位（待替换为真实 cquant-rust repo URL）
- `.gitignore`：覆盖 Python/Rust/conda/知识库数据文件
- `.github/workflows/ci.yml`：Python lint+test + Rust lint+test（含占位跳过逻辑）
- `scripts/bootstrap_dev.sh`：一键初始化 conda 环境 + Rust wheel
- `scripts/build_rust.sh`：maturin 构建 Rust wheel
- `scripts/seed_sample_data.sh`：测试 fixture 数据种子脚本（占位）
- `configs/defaults/`：backtest.toml（含 CN 印花税）、market_calendar.toml（CN/US/HK）、datahub.toml、registry.toml
- 项目目录骨架：`knowledge/`、`plugins/builtin/`、`schemas/`、`sql/duckdb/`、`notebooks/`

**Step 2 — `python/cquant/core`**
- `enums.py`：Market, Exchange, AssetClass, AssetStatus, Currency, Frequency, OrderSide, OrderType, AdjMethod, RiskDecisionType, EngineType
- `types.py`：Asset, Bar, Signal, TargetWeights, OrderIntent, Order, OrderFill, RiskDecision, RiskSnapshot（Pydantic v2）
- `errors.py`：完整异常继承树（CQuantError → 子类）
- `bus.py`：轻量同步事件总线（支持 Jupyter 模式）
- `clock.py`：WallClock + SimulationClock 抽象

**Step 3 — `python/cquant/market_calendar`**
- CN/US/HK 三地交易日历（内置节假日 2020-2026）
- `CNTradingRules`：涨跌停规则（±10% / ±5% ST / ±20% 科创板创业板）、T+1 结算、停牌注入接口
- `AdjustmentFactor`：前复权/后复权/不复权，支持 inject_factors() 离线注入
- `MarketCalendarService`：统一外观，T+1 (CN) / T+2 (US/HK) 自动路由

**Step 4 — `python/cquant/datahub`**
- `DataConnector` ABC + `DataSpec` + `RawBatch` 数据契约
- 内置 Connectors：AKShare（A 股日线）、Tushare（含 adj_factor、trade_cal）、Yahoo Finance（美股/港股）、CSV/Parquet 本地文件
- `Catalog`：DuckDB 元数据管理，dataset 版本注册
- `SilverNormalizer`：多源原始数据 → Silver 标准列名，统一 asset_id 格式
- DuckDB DDL：`sql/duckdb/bronze.sql`、`silver.sql`、`gold.sql`

**Step 5 — `python/cquant/factorlab`**
- `Factor` ABC + `FactorContext` + `FactorRegistry`
- 内置因子 8 个：ret_1/5/20/60d（动量）、vol_20/60d（波动率）、zscore_close_60d、ma_20d_ratio（技术）
- `UniverseBuilder`：流动性过滤 + 停牌过滤 + 交易所过滤 + top_n 截取
- `FeaturePipeline`：有序因子 DAG 执行，错误隔离，返回 `FeatureSetVersion`

**Step 6 — `python/cquant/riskguard`（Python 基础版）**
- `RiskPolicy` / `PositionSizer` ABC
- `PositionLimitPolicy`：单标的仓位上限检查
- `EqualWeightSizer`：等权重，支持多空
- `VolParitySizer`：波动率平价，无数据时降级等权
- 数据模型：RiskLimit, RiskContext, RiskBudget, SizingContext

**Step 7 — `python/cquant/backtest_vector`**
- `CostModel`：佣金制（双边 0.03%，最低 5 元）+ A 股印花税（单边卖出 0.1%）+ 滑点；含 for_cn/for_us/for_hk 预设
- `Strategy` ABC + `StrategyContext`
- `VectorBacktestEngine`：向量化回测核心，返回 BacktestResult
- `BacktestMetrics`：total_return / annualized_return / sharpe / max_drawdown / calmar / win_rate / profit_factor

**Step 7b — 契约冻结 + registry + schemas + 测试**
- `PluginManifest`：18 种 capability type，JSON Schema 校验
- `Registry`：manifest 发现、entrypoint 动态加载、能力查询
- `schemas/plugin-manifest.schema.json`
- 23 个单元测试，全部通过（`pytest python/tests/unit/ -v`）

#### Technical Decisions
- conda `cQuanty` 虚拟环境（Python 3.12）
- Git submodule 方式管理 Rust 子仓库
- DuckDB + Parquet 三层数据湖（Bronze/Silver/Gold）
- 费用模型 Python/Rust parity 测试（Rust 侧 Phase 2 实现）

---

*此文件由 Claude Code 自动维护，每次 `/ccg:execute` 完成后更新。*
