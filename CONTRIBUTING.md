# 贡献指南（CONTRIBUTING）

感谢关注 cQuant！本指南说明如何搭建开发环境、提交代码以及项目对决策记录的要求。

## 1. 开发环境搭建

一次性引导脚本会创建 conda 环境、安装依赖并编译 Rust wheel：

```bash
git clone --recurse-submodules <repo-url>
cd quant
git submodule update --init --recursive
./scripts/bootstrap_dev.sh
conda activate cQuanty
```

详细步骤（环境变量、启动 API/前端、Docker）见根目录 [README.md](README.md)「快速开始」。

## 2. 开发工作流

- **分支**：`main` 为主干，功能开发使用 `feat/<topic>` 短生命周期分支。
- **提交规范**：遵循 [Conventional Commits](https://www.conventionalcommits.org/zh-hans/)——`feat:` / `fix:` / `chore:` / `docs:` / `test:` / `refactor:` 等。
- **提交前自查**：只提交你修改的文件；`git status` 中无关的本地文件（数据、构建产物、临时 HTML）不要带入。

## 3. 测试与 Lint 期望

| 层 | 命令 | 说明 |
|----|------|------|
| Python 单元/集成测试 | `pytest python/tests -v` | 需先 `conda activate cQuanty` |
| Python lint | `ruff check python/` | 规则见 `pyproject.toml` `[tool.ruff]` |
| 前端单元测试 | `cd web && npm run test` | vitest |
| 前端 lint/build | `cd web && npm run lint && npm run build` | ESLint |
| e2e | `cd web && npm run test:e2e` | Playwright |
| Rust | `cargo test --manifest-path rust/Cargo.toml` | 子模块改动时 |

提交前请保证与你改动相关层的测试与 lint 通过。

## 4. 决策记录规范

**git log + docs/specs 是项目决策的唯一事实记录。** 项目不依赖口头讨论或聊天记录作为决策依据：

- **重大决策**（架构选型、接口契约变更、数据模型调整、依赖去留）必须有承载物，二选一：
  1. 一条说明 *为什么* 的 commit（提交信息写清动机与取舍，不止写了什么）；
  2. `docs/superpowers/specs/` 下的设计/决策文档。
- **纯口头或聊天中的决定不生效**：如果某次讨论改变了行为，请把它沉淀为上述两种形式之一后再实施。
- 回溯某个决策时，用 `git log --grep` 与 `docs/superpowers/specs/` 检索。

## 5. 提交 PR / 补丁

1. Fork（或分支）→ 改动 → 补测试 → 跑相关测试与 lint。
2. Commit message 符合第 2 节规范。
3. PR 描述包含：动机（为什么）、改动点（是什么）、验证方式（怎么确认）。

## 6. 行为准则

暂未单独提供 CODE_OF_CONDUCT 文件；开源后将以仓库文件形式补充。在此之前，请遵循常规开源社区礼仪：尊重、聚焦技术、对新手友好。

## 7. 安全相关

发现安全漏洞请勿直接开公开 issue，参见 [docs/security.md](docs/security.md) 的认证模型与部署清单，通过私密渠道联系维护者。
