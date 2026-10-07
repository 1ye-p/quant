# KNOWN_ISSUES

> 首段建立于 F3（2026-10-07）。F5 将补全文档清理并入本文件。

## 1. catalog 备份与生产库同盘（F3 边界声明）

- **现状**：`cquant.scheduler.catalog_backup` 将每日 03:40 备份写入
  `data/backups/catalog-YYYYMMDD.duckdb.gz`（日备留 7，周一文件为周备留 4）。
  备份目录与 `data/catalog.duckdb` 在**同一块盘**上。
- **影响**：磁盘故障 / 误删 `data/` 时，备份与生产库同时丢失；备份仅能
  防御应用层损坏（如 WAL 损坏——历史上已发生 4 次 `wal.corrupt`）与误操作，
  不能防御介质级故障。
- **缓解待办**：将 `data/backups` 同步到外部存储（rsync/对象存储/另一卷），
  或支持 `CQUANT_BACKUP_DIR` 指向异盘路径。当前未实现，属已知接受的风险。

## 2. 恢复演练记录

- 2026-10-07：`artifacts/backups/drill-2026-10-07.md`（首次演练，F3）

## 3. vibe-trading factor-research SKILL.md「Orthogonalized Combination」章节过时（F5）

- **现状**：`lib/vibe-trading/agent/src/skills/factor-research/SKILL.md:95-103`
  仍保留完整的「Orthogonalized Combination」章节，指引对因子做 Schmidt
  正交化后再等权合成。cQuant 侧该路径已删除（A3-5 移除
  `orthogonalize.py`），多因子合成统一走 `CrossSectionScorer._neutralize_factors`
  残差投影。
- **影响**：AI agent 阅读该 SKILL.md 会被误导，尝试调用后端已不存在的
  Schmidt 正交化路径并失败；主仓正确指引见
  [docs/user-guide/factor-research.md](docs/user-guide/factor-research.md)。
- **处置**：主仓侧已在 user-guide 加注并清理残留引用（F5）；上游 issue
  草案见 `docs/upstream/vibe-trading-orthogonalized-combination-issue.md`
  （用户提交后回填链接至 PRD §F5）。上游无响应则另立决策是否本地分支。
