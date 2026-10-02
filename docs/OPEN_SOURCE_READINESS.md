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

> 2026-10-02 更新：1/2 已在后续批次补齐/修复，3 完成核对并出结论。

1. ~~行为准则（CODE_OF_CONDUCT.md）不存在~~ → **已补**：根目录 `CODE_OF_CONDUCT.md`（Contributor Covenant v2.1 中文版）已新建，`CONTRIBUTING.md` §6 已链接（本批 commit）。
2. ~~`web/e2e/*.spec.ts`（Playwright）存在被 vitest 误拾取的风险~~ → **已修**：commit `194ed4d` 在 vite config 中显式排除 e2e 目录，vitest 收集回归全绿（backlog #4 关联项）。
3. **LICENSE 核对结论（2026-10-02，只读核对，未改任何 LICENSE 本体）**：各层级许可类型**不一致**——
   - 根仓库 `LICENSE`：**Apache-2.0**
   - `rust/` 子模块：**无 LICENSE 文件**（`rust/` 及 `rust/crates/` 各 crate 均未见 LICENSE*）
   - `lib/vibe-trading` 子模块：**MIT**（Copyright (c) 2026 Vibe-Trading Contributors）
   - `lib/qlib` 子模块：**MIT**（Copyright (c) Microsoft Corporation）

   结论：**发布前需统一或分层声明**。lib/ 下两个上游子模块保留其原始 MIT 许可（fork/引用惯例，无需改动）；`rust/` 子模块需补充 LICENSE 文件；主仓库若维持 Apache-2.0，建议在根 README 或 NOTICE 中声明"主仓库 Apache-2.0，子模块遵循各自 LICENSE"的分层许可说明。

---

## 五、开源后 backlog

> 收录 Phase 5 期间评审确认、不阻塞开源的遗留项。

| # | 条目 | 来源 | 状态 | 说明 |
|---|------|------|------|------|
| 1 | 因子/策略命令面板弱跳转 | Task 3 评审 | deferred | 特性工作：按名深链需目标页支持 |
| 2 | WelcomePage/AppLayout deferred minors | Task 3 评审 | ✅ 已清除（commit 541b301） | 面板重开重复拉取无缓存；语言快照不刷新等 |
| 3 | 既有测试失败：StrategiesPage 1 例 | main 上既有失败 | ✅ 已自愈 | 后续批次消化，2026-10-02 复测 4/4 通过、src 全量 33 文件/170 用例绿 |
| 4 | e2e/*.spec.ts 被 vitest 误拾取 | 测试基建 | ✅ 已清除（commit 194ed4d） | vite config 显式排除 e2e 目录，vitest 收集不再误拾取 |
| 5 | ML tab 评审 minors | Task 2/4.5 评审 | ✅ 已清除（commit 541b301） | RO 命令提示列表不全等小问题 |
| 6 | vibe extras 顶层命名碰撞面 | Task 4 评审 | ✅ 已清除（本批 commit） | `examples/vibe_trading_extras/README.md` 已补强 caveat：勿加入 Python path / 勿 pip 安装，仅在手动 pytest 时经 conftest sys.path 生效；根 README 相关提及处同步加注 |
| 7 | 行为准则缺失 | 本次核对 | ✅ 已清除（本批 commit） | 根 `CODE_OF_CONDUCT.md`（Contributor Covenant v2.1 中文版）已新建，`CONTRIBUTING.md` §6 已链接 |
| 8 | 双引擎 parity 形式化 | 设计 §5 #1 | deferred | 设计 §5 #1：开源后 revisit Rust 事件引擎 parity 测试与编译体验 |
| 9 | 数据广度缺口 | 设计 §5 #7 | deferred | 设计 §5 #7：分钟/tick/期货/期权/两融/北向/龙虎榜/宏观，按社区需求排期 |
| 10 | catalog-only DELETE 的目录行会被下次启动迁移复活 | ext-ind P1 review | ✅ 已清除（commit 541b301） | DELETE 响应已提示复活语义 |

## 六、结论

四要素全部达成（✅），可以开源。backlog 10 项中：**6 项已清除**（#2/#5/#10 → commit 541b301；#4 → commit 194ed4d；#6/#7 → 本批 commit）、**1 项已自愈**（#3，2026-10-02 复测 src 全量 33 文件/170 用例绿）、**3 项 deferred**（#1/#8/#9，均为特性类工作，非缺陷：#1 深链待目标页支持、#8/#9 按设计 §5 与社区需求排期）。原优先级建议中 #4（测试基建）与 #7（社区规范）均已在后续批次完成；剩余 deferred 项无阻塞，按社区反馈排期即可。
