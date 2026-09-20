# 开源就绪核对清单（Open Source Readiness）

> 核对日期：2026-09-21
> 依据：`docs/superpowers/specs/2026-09-16-research-loop-optimization-design.md` §1「开源前必补」四要素（安装体验、文档、示例数据、安全默认）
> 性质：只读核对 + 文档补全（本清单与 `CONTRIBUTING.md` 即本次交付）

## 一、四要素核对结论

| 要素 | 状态 | 证据 |
|------|------|------|
| 安装体验 | ✅ 已达成 | `scripts/bootstrap_dev.sh` 存在、可执行、`bash -n` 语法校验通过；README「快速开始」逐条命令与实际文件比对一致（详见下文核对明细） |
| 文档 | ✅ 已达成 | 根 `README.md`（架构/快速开始/CLI/示例代码）、`CLAUDE.md`（模块索引）、`docs/security.md`、`CONTRIBUTING.md`（本次新建）、本清单（本次新建） |
| 示例数据 | ✅ 已达成 | `examples/demo_data/`（demo_prices_1d.csv、demo_strategy.yaml、market_breadth.csv、generate_demo_data.py）；演示流路由 `welcome` 已注册（`web/src/router.tsx:62` → `web/src/pages/WelcomePage.tsx`）；后端 `python/cquant/api_server/routes/demo.py` 已挂载（`app.py:42,253`） |
| 安全默认 | ✅ 已达成 | `python/cquant/api_server/deps.py:287` 默认 `CQUANT_AUTH_MODE=strict`（default-deny），`dev` 模式为逃生门且对交易端点仍拒绝（`deps.py:289-301`）；`docs/security.md` 覆盖认证模型、key 生成/轮换、部署清单 |

## 二、README 命令核对明细

| README 命令 | 实际依据 | 结论 |
|-------------|----------|------|
| `git submodule update --init --recursive` | `.gitmodules`（rust + lib/qlib + lib/vibe-trading） | ✅ |
| `./scripts/bootstrap_dev.sh` | `scripts/bootstrap_dev.sh` 存在、可执行、语法有效 | ✅ |
| `conda activate cQuanty` | `environment.yml` 环境名 cQuanty | ✅ |
| `cp .env.example .env` | `.env.example` 存在 | ✅ |
| `uvicorn cquant.api_server.app:app --port 8000` | `python/cquant/api_server/app.py` 存在 | ✅ |
| `cd web && npm install && npm run dev` | `web/package.json`（scripts.dev 存在） | ✅ |
| `python -m cquant.cli.main status/bootstrap/ingest/factors/backtest` | `python/cquant/cli/main.py` 存在，子命令齐全 | ✅ |
| `docker-compose up -d` | 根目录 `docker-compose.yml` 存在 | ✅ |

## 三、安全默认核对明细

- **default-deny**：`deps.py` 中 `mode = os.getenv("CQUANT_AUTH_MODE", "strict")`，未配置即 strict。
- **dev 逃生门**：`CQUANT_AUTH_MODE=dev` 仅放行非交易端点；交易端点始终要求认证；dev 模式且未设 key 时打印明确警告（`deps.py:287-301`）。
- **key 生成**：CLI 提供 `cquant auth generate-key`（见 `docs/security.md` Key Generation 节）。
- **文档**：`docs/security.md` 含 Authentication Model / Environment Variables / Key Generation / Dev Mode / Key Rotation / Deployment Checklist 六节。

## 四、已知缺口（不阻塞开源，收录入 backlog）

1. 行为准则（CODE_OF_CONDUCT.md）不存在——CONTRIBUTING 中暂不链接，开源后按社区惯例补充。
2. `web/e2e/*.spec.ts`（Playwright）存在被 vitest 误拾取的风险（见 backlog #3）。
3. `LICENSE` 已存在（类型见文件本身）；若计划采用双重许可（核心 + Rust 子模块），发布前确认各 LICENSE 头一致。

---

## 五、开源后 backlog

> 收录 Phase 5 期间评审确认、不阻塞开源的遗留项。

| # | 条目 | 来源 | 说明 |
|---|------|------|------|
| 1 | 因子/策略命令面板弱跳转 | Task 3 评审 | Cmd+K 面板跳转到目标页后，无按名称深链参数，无法自动定位/选中具体因子或策略 |
| 2 | WelcomePage/AppLayout deferred minors | Task 3 评审 | 面板重开重复拉取无缓存；语言快照不刷新等 |
| 3 | 既有测试失败：StrategiesPage 1 例 | main 上既有失败 | 单例断言失败，非本次改动引入 |
| 4 | e2e/*.spec.ts 被 vitest 误拾取 | 测试基建 | `web/e2e/` 下 Playwright spec 与 vitest 的默认 include 规则冲突，需在 vite config 中显式排除 |
| 5 | ML tab 评审 minors | Task 2/4.5 评审 | RO 命令提示列表不全等小问题 |
| 6 | vibe extras 顶层命名碰撞面 | Task 4 评审 | 已移至 examples（commit 50f913b），但 examples 内脚本若被直接 pip 安装/导入，顶层模块名仍有与主包碰撞的隐患，需文档提醒或改造为包内相对引用 |
| 7 | 行为准则缺失 | 本次核对 | 新建 CODE_OF_CONDUCT.md 并在 CONTRIBUTING 链接 |
| 8 | 双引擎 parity 形式化 | 设计 §5 #1 | 主路径为向量化引擎；开源后 revisit Rust 事件引擎 parity 测试与编译体验 |
| 9 | 数据广度缺口 | 设计 §5 #7 | 分钟/tick/期货/期权/两融/北向/龙虎榜/宏观；开源后按社区需求排期 |

## 六、结论

四要素全部达成（✅），可以开源。以上 9 条 backlog 均为非阻塞项，建议开源后按 #4（测试基建，影响贡献者 CI 体验）与 #7（社区规范）优先处理。
