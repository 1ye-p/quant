# cQuant

> 面向量化研究的 AI 辅助平台 — 数据接入 · 526+ 因子 · 回测分析 · ML 训练 · 知识库 · 多 Agent AI 研究助手

[![CI](https://github.com/1ye-p/quant/actions/workflows/ci.yml/badge.svg)](https://github.com/1ye-p/quant/actions/workflows/ci.yml)
![Python](https://img.shields.io/badge/Python-3.12-blue)
![License](https://img.shields.io/badge/License-MIT-green)

---

## 简介

cQuant 是一个自用的量化研究平台，将数据接入、因子挖掘、向量化回测、过拟合检测、机器学习训练、RAG 知识库和多 Agent AI 研究助手整合到一套本地优先的工具链中。通过 bridge 封装层集成 Microsoft Qlib 和 Vibe-Trading 两大开源量化框架，同时保持 cQuant 自身接口独立。

**核心特性：**

- **数据层**：DuckDB 三层数据仓库（Bronze / Silver / Gold），支持 TDX、Tushare、AKShare、Yahoo Finance 多数据源
- **因子库**：526+ 内置因子（cQuant 原生 20+、Qlib Alpha158 50+、Vibe-Trading Alpha101 101 + 国泰君安 191 + Qlib158 扩展 154），声明式 DSL + DAG 执行引擎
- **开源桥接**：`qlib_bridge/` 封装 Qlib 数据/评估/因子接口，`vibe_bridge/` 封装 Vibe-Trading Alpha 因子，外部模块只调用 bridge，不直接依赖上游 API（Swarm 团队/LLM 供应商适配未接线，已移至 `examples/vibe_trading_extras/`）
- **回测引擎**：向量化回测（含 A 股印花税、涨跌停规则、T+1 结算）+ 事件驱动引擎（Rust pyo3）+ 异步回测（BackgroundTasks + job_id 轮询）
- **策略模板**：行业轮动、市场中性、ML Model 策略、复合策略，支持信号校验与滑点模拟
- **过拟合检测**：PSR / DSR / CPCV / Walk-Forward 统计验证，基于 Bailey & Lopez de Prado 方法
- **风控系统**：DrawdownBreaker 分级熔断、ATR 止损、时间止损、行业限额、因子暴露限制、Kelly/波动率平价/MVO/Black-Litterman 仓位管理
- **ML 训练**：XGBoost / LightGBM 训练器，时序 Purged K-Fold 交叉验证，MLflow 追踪，特征重要性分析
- **知识库**：研报 / 策略文档 RAG，支持 PDF / Markdown / URL 导入，关键词 + 向量混合检索
- **AI 研究助手**：基于 Claude / GPT-4o 的多 Agent 编排（Research、Risk、Debate、Report Writer），Safety Policy 防止误触真实交易；会话 SQLite 持久化 + IntentRouter 事件路由
- **MCP Server**：将 Tushare / AKShare 数据接入和因子分析封装为 MCP 工具，Agent 可通过工具调用实时获取数据
- **Web Dashboard**：React 18 前端（TanStack Query + Recharts + TradingView lightweight-charts），10 个功能页面
- **Docker 部署**：Dockerfile + docker-compose.yml 一键启动

---

## 架构

```
cQuant/
├── lib/                     # 外部开源框架（git submodule，独立版本管理）
│   ├── qlib/                #   Microsoft Qlib（pin 到稳定 tag）
│   └── vibe-trading/        #   Vibe-Trading（追踪 main，随时同步）
├── python/cquant/           # Python 控制平面
│   ├── core/                # 共享类型、枚举、事件总线、TOML 配置
│   ├── market_calendar/     # CN/US/HK 交易日历与涨跌停规则
│   ├── datahub/             # 数据接入、DuckDB 三层仓库、基本面更新
│   ├── factorlab/           # 因子 DSL、DAG 执行、特征物化、Alpha158
│   ├── riskguard/           # 风控策略（止损/熔断/行业限制/因子暴露）、仓位 sizing
│   ├── backtest_vector/     # 向量化回测 + 信号校验 + 4 种策略模板
│   ├── backtest_event/      # 事件驱动回测（Rust pyo3）
│   ├── bt_analyzer/         # 过拟合检测（PSR/DSR/CPCV）
│   ├── ml_lab/              # XGBoost/LightGBM + MLflow + ML Pipeline
│   ├── newsflow/            # 新闻数据接入（新浪/东财/RSS）+ 情绪聚合
│   ├── knowledge_base/      # 研报知识库（RAG）
│   ├── ai_advisor/          # 多 Agent 研究助手 + 会话持久化 + IntentRouter
│   ├── portfolio_opt/       # 组合优化（MVO/风险平价/Black-Litterman/协方差估计）
│   ├── scheduler/           # 定时任务调度
│   ├── execution/           # 模拟/实盘执行接口（PaperBroker/QMT 适配器）
│   ├── api_server/          # FastAPI 服务（REST + SSE 异步回测）
│   ├── mcp_server/          # MCP 工具服务（DuckDB + AKShare 数据查询）
│   ├── qlib_bridge/         # Qlib 封装层（数据适配/评估/Alpha158 因子集）
│   ├── vibe_bridge/         # Vibe-Trading 封装层（Alpha 因子；Swarm/LLM 供应商见 examples/vibe_trading_extras/）
│   └── cli/                 # 命令行工具
├── rust/                    # Rust 高性能核心（git submodule → github.com/1ye-p/quant-rust）
│   └── crates/
│       ├── cquant-core/     #   基础金融类型
│       ├── cquant-event-engine/ #   市场回放、撮合
│       ├── cquant-portfolio/    #   持仓、风控状态机、费用模型
│       └── cquant-py/       #   PyO3 Python 绑定
├── web/                     # React 18 前端（Vite + TypeScript，10 个页面）
├── knowledge/               # 知识库数据目录（本地，gitignore 排除）
├── notebooks/               # Jupyter 研究笔记本
├── configs/                 # 各模块默认配置（TOML）+ 插件清单
├── schemas/                 # JSON Schema 契约
├── sql/duckdb/              # DuckDB DDL 文件
├── scripts/                 # 环境初始化脚本
├── Dockerfile               # Docker 构建文件
└── docker-compose.yml       # 一键启动
```

---

## 快速开始

### 1. 环境要求

| 依赖 | 说明 |
|------|------|
| Conda / Miniconda | 管理 Python 环境 |
| Python 3.12 | 已包含在 cQuanty 环境 |
| Node.js ≥ 18 | 用于前端构建 |
| Rust toolchain（可选） | 编译高性能 Rust 模块 |

### 2. 初始化环境

```bash
git clone --recurse-submodules git@github.com:1ye-p/quant.git
cd quant

# 初始化所有 submodule（rust + lib/qlib + lib/vibe-trading）
git submodule update --init --recursive

# 创建 conda 环境 + 安装依赖 + 编译 Rust wheel
./scripts/bootstrap_dev.sh
conda activate cQuanty
```

### 3. 配置环境变量

```bash
cp .env.example .env
```

编辑 `.env`，填入所需 API Key：

```env
# 数据源（至少填一个）
TUSHARE_TOKEN=<your-token>        # https://tushare.pro/register

# AI 助手（选填，用于 AI Advisor 功能）
ANTHROPIC_API_KEY=<sk-ant-xxx>    # https://console.anthropic.com/
OPENAI_API_KEY=<sk-xxx>           # 备用，可不填
```

> `.env` 已被 `.gitignore` 排除，不会提交到 Git。

### 4. 启动服务

**后端 API（FastAPI）：**

```bash
conda activate cQuanty
cd /path/to/cQuant
uvicorn cquant.api_server.app:app --port 8000 --reload
```

访问 API 文档：http://localhost:8000/api/docs

**前端 Dashboard（React）：**

```bash
cd web
npm install    # 首次安装依赖
npm run dev
```

访问：http://localhost:3000

**Docker 一键启动（可选）：**

```bash
docker-compose up -d
# 后端: http://localhost:8000  前端: http://localhost:3000
```

---

## 使用示例

### 数据接入

```python
from cquant.datahub.connectors.tushare_connector import TushareConnector
from cquant.datahub.connectors.base import DataSpec
from cquant.core.enums import Market
from datetime import date

connector = TushareConnector()  # 读取 .env 中的 TUSHARE_TOKEN
spec = DataSpec(
    symbols=["SSE:600036", "SZSE:000858"],
    start_date=date(2023, 1, 1),
    end_date=date(2024, 12, 31),
    market=Market.CN,
)
for batch in connector.fetch(spec):
    print(f"{batch.source}: {batch.data.height} 行")
```

### 因子计算

```python
from cquant.factorlab import FactorRegistry, FeaturePipeline, PipelineSpec
from cquant.factorlab.factors import BUILTIN_FACTORS
from datetime import date

registry = FactorRegistry()
for f in BUILTIN_FACTORS:
    registry.register(f)

pipeline = FeaturePipeline(registry)
spec = PipelineSpec(
    factor_names=["ret_20d", "vol_20d", "zscore_close_60d"],
    start_date=date(2023, 1, 1),
    end_date=date(2024, 12, 31),
)
result = pipeline.run(prices_df, spec)
```

### 向量化回测

```python
from cquant.backtest_vector import VectorBacktestEngine, BacktestSpec
from cquant.backtest_vector.costs import CostModel
from decimal import Decimal
from datetime import date

engine = VectorBacktestEngine()
spec = BacktestSpec(
    strategy=my_strategy,
    prices=prices_df,
    start_date=date(2023, 1, 1),
    end_date=date(2024, 12, 31),
    initial_cash=Decimal("1000000"),
    cost_model=CostModel.for_cn(),  # 含 A 股印花税
)
result = engine.run(spec)
print(f"总收益: {result.metrics.total_return:.2%}")
print(f"夏普比率: {result.metrics.sharpe_ratio:.3f}")
print(f"最大回撤: {result.metrics.max_drawdown:.2%}")
```

### 过拟合检测

```python
from cquant.bt_analyzer import AnalysisEngine, AnalysisSpec

engine = AnalysisEngine(AnalysisSpec(n_oos_windows=5, n_trials=1))
report = engine.run(backtest_result)
print(f"PSR: {report.psr:.3f}")   # 接近 1 = 统计显著
print(f"DSR: {report.dsr:.3f}")   # 多重检验修正后
print(f"过拟合评分: {report.overall_overfit_score.score:.2f}")
```

### AI 研究助手

```python
from cquant.ai_advisor import AdvisorOrchestrator, ClaudeProvider, SafetyPolicy

orchestrator = AdvisorOrchestrator(
    provider=ClaudeProvider(),  # 读取 ANTHROPIC_API_KEY
    tools=[],
    kb_service=kb_service,
    safety=SafetyPolicy(),
)
response = orchestrator.chat_sync("A 股动量策略的主要风险是什么？", session)
print(response)
```

---

## CLI 工具

```bash
# 查看系统状态
python -m cquant.cli.main status

# 从 TDX 数据库引导元数据
python -m cquant.cli.main bootstrap --target all --tdx-db tdx.db

# 摄取行情数据
python -m cquant.cli.main ingest --source tdx --start 2024-01-01 --end 2025-12-31

# 物化因子
python -m cquant.cli.main factors --dataset-version tdx_bulk_v1 --start 2024-01-01 --end 2025-12-31 --all

# 运行回测
python -m cquant.cli.main backtest --dataset-version tdx_bulk_v1 --strategy-id top10 --start 2025-01-01 --end 2025-06-30
```

---

## 测试

```bash
conda activate cQuanty

# 全量单元测试
pytest python/tests/unit -v

# 集成测试
pytest python/tests/integration -v

# Rust 单元测试（需已编译 wheel）
cargo test --manifest-path rust/Cargo.toml --all-targets
```

---

## 技术栈

| 层级 | 技术 |
|------|------|
| 数据存储 | DuckDB + Apache Parquet |
| 数值计算 | Polars + NumPy |
| 量化框架 | Microsoft Qlib（Alpha158 因子集） + Vibe-Trading（526 Alpha 因子；Swarm Teams 为可选 extras） |
| ML 框架 | XGBoost / LightGBM + MLflow |
| Web 框架 | FastAPI + Pydantic v2 |
| 前端 | React 18 + Vite + TanStack Query + TradingView lightweight-charts |
| 高性能核心 | Rust + PyO3 / maturin |
| AI Provider | Claude (Anthropic) / GPT-4o (OpenAI)（14 LLM 供应商适配见 examples/vibe_trading_extras/，未接线） |
| MCP 工具 | FastMCP Server（DuckDB + AKShare 数据查询） |
| 向量检索 | LanceDB（可选） |
| 部署 | Docker + docker-compose |

---

## 安全说明

- AI Advisor 仅支持离线研究分析，`SafetyPolicy` 拦截所有真实交易指令
- `.env` 已排除在版本控制之外，请勿将真实 API Key 写入代码或文档
- 本项目不提供投资建议，策略回测结果不代表未来收益

---

## 版本历史

详见 [CHANGELOG.md](CHANGELOG.md) 和 [ROADMAP.md](docs/plans/ROADMAP.md)。

| 版本 | 状态 | 说明 |
|------|------|------|
| Phase 0 | ✅ 完成 | Qlib 子模块集成：qlib_bridge 封装层（_compat/数据适配/评估器/因子集）、Alpha158 因子 |
| Phase 0-B | ✅ 完成 | Vibe-Trading 子模块集成：vibe_bridge（526 因子；Swarm/LLM 供应商未接线，后移至 `examples/vibe_trading_extras/`） |
| Phase 1 | ✅ 完成 | 核心模块、数据层、因子、回测、CLI + UI Bug 修复 |
| Phase 2 | ✅ 完成 | ML Lab、Newsflow、Rust 核心、过拟合检测 + 异步回测/扩展指标 |
| Phase 3 | ✅ 完成 | 知识库 RAG、AI Advisor、API Server、Web UI + 因子研究/ML 打通 |
| Phase 4 | ✅ 完成 | AI Advisor 升级：MCP 工具化、会话 SQLite 持久化、IntentRouter |

---

## License

MIT
