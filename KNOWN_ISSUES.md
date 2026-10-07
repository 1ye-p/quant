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
