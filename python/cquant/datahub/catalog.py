"""cquant.datahub.catalog — DuckDB-backed dataset catalog.

Manages dataset registration, version lineage, and SQL query access
over the Bronze / Silver / Gold layers.
"""

from __future__ import annotations

import hashlib
import logging
import os
import threading
import time
import uuid
from contextlib import contextmanager
from datetime import datetime, timezone
from pathlib import Path
from typing import TYPE_CHECKING, Any, Iterator

import polars as pl

from cquant.core.errors import CatalogError

if TYPE_CHECKING:
    from cquant.datahub.backend import CatalogBackend

logger = logging.getLogger(__name__)

_DDL_FILES = [
    "sql/duckdb/bronze.sql",
    "sql/duckdb/silver.sql",
    "sql/duckdb/news.sql",
    "sql/duckdb/analysis.sql",
    "sql/duckdb/gold.sql",
    "sql/duckdb/knowledge.sql",
    "sql/duckdb/meta.sql",
]

# DuckDB WAL replay-failure signatures. Observed examples:
#   "IO Error: Failure while replaying WAL file ...: Corrupt WAL file: ...
#    computed checksum ... does not match stored checksum ..."
#   "Internal Error: ... WriteAheadLog ..."
# Deliberately narrow: plain IO errors (missing file, permissions, directory)
# must NOT match — see test_self_heal_no_false_positive.
_WAL_CORRUPTION_SIGNATURES = (
    "writeaheadlog",
    "wal file",
    "replaying wal",
    "replay wal",
    "internal error",
)

_DEFAULT_CHECKPOINT_INTERVAL_SEC = 600.0


def _is_wal_corruption_error(exc: Exception) -> bool:
    """Return True if *exc* looks like a DuckDB WAL replay/corruption failure."""
    msg = str(exc).lower()
    return any(sig in msg for sig in _WAL_CORRUPTION_SIGNATURES)


def _self_heal_wal(db_path: Path) -> bool:
    """Quarantine a corrupt WAL file so the caller can retry the connection.

    Renames ``<db>.wal`` to ``<db>.wal.corrupt-<unix_ts>``. Returns True if a
    WAL was quarantined (caller should retry the connection), False otherwise
    (no WAL, empty WAL, or rename failed).
    """
    wal = Path(str(db_path) + ".wal")
    if not wal.exists() or wal.stat().st_size == 0:
        return False
    backup = wal.with_name(f"{wal.name}.corrupt-{int(time.time())}")
    try:
        wal.rename(backup)
    except OSError as rename_exc:
        logger.error(
            "WAL self-heal failed: could not quarantine %s: %s", wal, rename_exc
        )
        return False
    logger.error(
        "检测到 WAL 损坏已隔离，上次未落盘写入可能丢失，备份于 %s "
        "(WAL corruption detected and quarantined; un-checkpointed writes may "
        "be lost; backup at %s)",
        backup,
        backup,
    )
    return True


@contextmanager
def _observe_duckdb(operation: str) -> Iterator[None]:
    """Observe a DuckDB operation's latency into the Prometheus histogram.

    Best-effort: if ``prometheus_client`` or the metrics module is unavailable,
    or the histogram is disabled, this is a transparent no-op. Lives here in
    ``catalog.py`` (rather than the metrics route) to avoid importing the API
    layer from the data layer.
    """
    start = time.perf_counter()
    try:
        yield
    finally:
        try:
            from cquant.api_server.routes.metrics import (
                duckdb_query_duration_seconds,
            )

            if duckdb_query_duration_seconds is not None:
                duckdb_query_duration_seconds.labels(operation=operation).observe(
                    time.perf_counter() - start
                )
        except Exception:  # pragma: no cover — metrics are best-effort
            pass


def _try_alter_add_column(backend, stmt: str, exc: Exception) -> bool:
    """Auto-heal: if a DDL statement fails because a column is missing, try ALTER TABLE ADD COLUMN.

    Returns True if the error was resolved (caller should continue), False otherwise.
    Handles the common case where a stale DuckDB file doesn't have a newly-added column
    that an INDEX or ALTER depends on.
    """
    exc_str = str(exc)
    if "does not have a column named" not in exc_str:
        return False

    import re

    # Extract table name and missing column from the error message
    # Pattern: Table "xxx" does not have a column named "yyy"
    m = re.search(r'Table "(\w+)" does not have a column named "(\w+)"', exc_str)
    if not m:
        return False

    table_name, col_name = m.group(1), m.group(2)

    # Try to find the column definition in the original DDL file
    # For now, add as nullable DOUBLE (safe default for numeric columns)
    # If it's a DATE column, we detect from context
    col_type = "DATE" if "date" in col_name.lower() else "DOUBLE"
    alter_sql = f"ALTER TABLE {table_name} ADD COLUMN IF NOT EXISTS {col_name} {col_type}"

    try:
        backend.execute(alter_sql)
        logger.info("Auto-healed: added column %s %s to %s", col_name, col_type, table_name)
        # Re-try the original statement
        backend.execute(stmt)
        return True
    except Exception:
        return False


class Catalog:
    """Database-agnostic catalog for the cQuant data lake.

    Usage::

        catalog = Catalog("data/catalog.duckdb")
        catalog.initialize()          # Run DDL on first use
        catalog.query("SELECT COUNT(*) FROM silver_prices_1d")
    """

    def __init__(
        self,
        db_path: str | Path = "data/catalog.duckdb",
        repo_root: str | Path | None = None,
        backend: CatalogBackend | None = None,
        read_only: bool = False,
    ) -> None:
        self._repo_root = Path(repo_root) if repo_root else Path.cwd()
        self._stop_event: threading.Event | None = None
        self._checkpoint_thread: threading.Thread | None = None
        self._checkpoint_interval: float = 0.0
        if backend is not None:
            self._backend: CatalogBackend = backend
            self._db_path = Path(db_path)
        else:
            self._db_path = Path(db_path)
            if not read_only:
                self._db_path.parent.mkdir(parents=True, exist_ok=True)
            self._backend = self._connect_duckdb_with_self_heal(
                str(self._db_path), read_only=read_only
            )
            if not read_only:
                self._start_checkpoint_thread()

    def _get_conn(self):
        """Compatibility shim — returns the raw backend connection if available."""
        return getattr(self._backend, "_conn", None)

    # ------------------------------------------------------------------
    # Connection self-heal (WAL governance)
    # ------------------------------------------------------------------

    def _connect_duckdb_with_self_heal(self, db_path: str, read_only: bool):
        """Open a DuckDB backend, quarantining a corrupt WAL and retrying once.

        Both CLI and API obtain their connection through :class:`Catalog`, so
        self-healing here covers both entry points automatically.
        """
        from cquant.datahub.backends.duckdb_backend import DuckDBBackend

        try:
            return DuckDBBackend(db_path, read_only=read_only)
        except Exception as exc:
            if read_only or not _is_wal_corruption_error(exc):
                raise
            if not _self_heal_wal(self._db_path):
                raise
            try:
                return DuckDBBackend(db_path, read_only=read_only)
            except Exception as retry_exc:
                raise CatalogError(
                    f"Catalog connect failed even after WAL self-heal. "
                    f"WAL backup is next to {db_path} (*.wal.corrupt-<ts>). "
                    f"Retry error: {retry_exc}"
                ) from retry_exc

    # ------------------------------------------------------------------
    # Periodic CHECKPOINT (WAL governance)
    # ------------------------------------------------------------------

    def _start_checkpoint_thread(self) -> None:
        """Start the periodic CHECKPOINT daemon thread.

        Interval comes from ``CQUANT_CHECKPOINT_INTERVAL_SEC`` (default 600s);
        0 or negative disables periodic checkpointing entirely.
        """
        raw = os.environ.get("CQUANT_CHECKPOINT_INTERVAL_SEC")
        try:
            interval = (
                float(raw) if raw is not None else _DEFAULT_CHECKPOINT_INTERVAL_SEC
            )
        except ValueError:
            logger.warning(
                "Invalid CQUANT_CHECKPOINT_INTERVAL_SEC=%r, using default %ss",
                raw,
                _DEFAULT_CHECKPOINT_INTERVAL_SEC,
            )
            interval = _DEFAULT_CHECKPOINT_INTERVAL_SEC
        if interval <= 0:
            logger.info("Periodic catalog CHECKPOINT disabled (interval=%s)", raw)
            return
        self._checkpoint_interval = interval
        self._stop_event = threading.Event()
        self._checkpoint_thread = threading.Thread(
            target=self._checkpoint_loop,
            name="cquant-catalog-checkpoint",
            daemon=True,
        )
        self._checkpoint_thread.start()
        logger.info(
            "Periodic catalog CHECKPOINT started (interval=%ss, db=%s)",
            interval,
            self._db_path,
        )

    def _checkpoint_loop(self) -> None:
        assert self._stop_event is not None
        while not self._stop_event.wait(self._checkpoint_interval):
            try:
                self.checkpoint()
            except Exception as exc:  # never kill the loop
                logger.error("Periodic catalog CHECKPOINT failed: %s", exc)

    def checkpoint(self) -> None:
        """Force a DuckDB CHECKPOINT — flushes the WAL into the database file."""
        self._backend.execute("CHECKPOINT")

    def initialize(self) -> None:
        """Execute all DDL scripts to create tables if they do not exist."""
        for ddl_file in _DDL_FILES:
            path = self._repo_root / ddl_file
            if not path.exists():
                logger.warning("DDL file not found, skipping: %s", path)
                continue
            sql = path.read_text(encoding="utf-8")
            for stmt in _split_statements(sql):
                try:
                    self._backend.execute(stmt)
                except Exception as exc:
                    if _try_alter_add_column(self._backend, stmt, exc):
                        continue
                    raise CatalogError(f"DDL failed in {ddl_file}: {exc}\n\n{stmt}") from exc
        logger.info("Catalog initialized at %s", self._db_path)

    def query(self, sql: str, params: list[Any] | None = None) -> pl.DataFrame:
        """Execute *sql* and return the result as a Polars DataFrame."""
        with _observe_duckdb("query"):
            try:
                return self._backend.query(sql, params)
            except CatalogError:
                raise
            except Exception as exc:
                raise CatalogError(f"Query failed: {exc}\n\nSQL: {sql}") from exc

    def execute(self, sql: str, params: list[Any] | None = None) -> None:
        """Execute a non-SELECT statement (INSERT, UPDATE, DELETE)."""
        with _observe_duckdb("execute"):
            try:
                self._backend.execute(sql, params)
            except CatalogError:
                raise
            except Exception as exc:
                raise CatalogError(f"Execute failed: {exc}\n\nSQL: {sql}") from exc

    def executemany(self, sql: str, rows: list[tuple]) -> None:
        """Execute statement for multiple rows."""
        with _observe_duckdb("executemany"):
            try:
                self._backend.executemany(sql, rows)
            except CatalogError:
                raise
            except Exception as exc:
                raise CatalogError(f"Executemany failed: {exc}\n\nSQL: {sql}") from exc

    def upsert(
        self,
        table: str,
        columns: list[str],
        rows: list[tuple],
        conflict_columns: list[str],
    ) -> None:
        """Insert or update rows on conflict."""
        try:
            self._backend.upsert(table, columns, rows, conflict_columns)
        except CatalogError:
            raise
        except Exception as exc:
            raise CatalogError(f"Upsert failed: {exc}\n\nTable: {table}") from exc

    def register_dataset(
        self,
        dataset_name: str,
        frequency: str,
        start_date: str,
        end_date: str,
        asset_count: int,
        row_count: int,
        storage_uri: str,
        source: str,
        data_for_hash: bytes | None = None,
    ) -> str:
        """Register a new dataset version and return the version_id."""
        version_id = str(uuid.uuid4())
        content_hash = hashlib.sha256(data_for_hash).hexdigest() if data_for_hash else ""
        now = datetime.now(tz=timezone.utc).isoformat()

        # Mark previous current version as non-current
        self.execute(
            """
            UPDATE silver_dataset_versions
            SET is_current = FALSE
            WHERE dataset_name = ? AND is_current = TRUE
            """,
            [dataset_name],
        )

        self.execute(
            """
            INSERT INTO silver_dataset_versions
                (version_id, dataset_name, frequency, start_date, end_date,
                 asset_count, row_count, storage_uri, content_hash, source, created_at, is_current)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, TRUE)
            """,
            [
                version_id, dataset_name, frequency, start_date, end_date,
                asset_count, row_count, storage_uri, content_hash, source, now,
            ],
        )
        return version_id

    def get_data_quality_summary(
        self, table: str = "silver_prices_1d"
    ) -> dict:
        """返回价格数据的质量诊断摘要。

        Parameters
        ----------
        table:
            要检查的表名，默认 ``"silver_prices_1d"``。

        Returns
        -------
        包含以下键的字典：

        - ``total_rows``: 总行数
        - ``asset_count``: 资产数量
        - ``date_range``: ``{"start": date, "end": date}``
        - ``zero_close_count``: close <= 0 的异常行数
        - ``suspended_count``: 停牌行数
        """
        try:
            stats = self.query(f"""
                SELECT
                    COUNT(*) AS total_rows,
                    COUNT(DISTINCT asset_id) AS asset_count,
                    MIN(trade_date) AS start_date,
                    MAX(trade_date) AS end_date,
                    SUM(CASE WHEN close <= 0 THEN 1 ELSE 0 END) AS zero_close_count,
                    SUM(CASE WHEN is_suspended THEN 1 ELSE 0 END) AS suspended_count
                FROM {table}
            """)
        except Exception:
            return {
                "total_rows": 0,
                "asset_count": 0,
                "date_range": {"start": None, "end": None},
                "zero_close_count": 0,
                "suspended_count": 0,
            }

        if stats.is_empty():
            return {
                "total_rows": 0,
                "asset_count": 0,
                "date_range": {"start": None, "end": None},
                "zero_close_count": 0,
                "suspended_count": 0,
            }

        row = stats.row(0, named=True)
        return {
            "total_rows": int(row["total_rows"] or 0),
            "asset_count": int(row["asset_count"] or 0),
            "date_range": {
                "start": row["start_date"],
                "end": row["end_date"],
            },
            "zero_close_count": int(row["zero_close_count"] or 0),
            "suspended_count": int(row["suspended_count"] or 0),
        }

    # ------------------------------------------------------------------
    # Factor descriptions
    # ------------------------------------------------------------------

    _CREATE_FACTOR_DESCRIPTIONS = """\
CREATE TABLE IF NOT EXISTS meta_factor_descriptions (
    factor_name         VARCHAR PRIMARY KEY,
    description         TEXT,
    category            VARCHAR,
    formula             TEXT,
    data_source         VARCHAR,
    lookback_days       INTEGER,
    notes               TEXT,
    created_at          TIMESTAMPTZ NOT NULL,
    updated_at          TIMESTAMPTZ NOT NULL
);"""

    def write_meta_factor_descriptions(self, df: pl.DataFrame) -> int:
        """Upsert factor descriptions into ``meta_factor_descriptions``.

        Creates the table on first call.  Expects *df* to contain at least a
        ``factor_name`` column.  Missing optional columns are filled with
        ``None`` / current timestamp.

        Returns the number of rows written.
        """
        if df.is_empty():
            return 0

        self.execute(self._CREATE_FACTOR_DESCRIPTIONS)

        now = datetime.now(tz=timezone.utc).isoformat()
        df = df.with_columns(
            pl.col("created_at").fill_null(pl.lit(now)) if "created_at" in df.columns else pl.lit(now).alias("created_at"),
            pl.col("updated_at").fill_null(pl.lit(now)) if "updated_at" in df.columns else pl.lit(now).alias("updated_at"),
        )

        columns = df.columns
        rows = [tuple(row) for row in df.iter_rows()]
        conflict_cols = ["factor_name"]
        self.upsert("meta_factor_descriptions", columns, rows, conflict_cols)
        return len(rows)

    def read_meta_factor_descriptions(
        self, factor_names: list[str] | None = None
    ) -> pl.DataFrame:
        """Read factor descriptions.

        If *factor_names* is provided, returns only matching rows.  Otherwise
        returns all rows.
        """
        if factor_names:
            placeholders = ", ".join(["?"] * len(factor_names))
            return self.query(
                f"SELECT * FROM meta_factor_descriptions WHERE factor_name IN ({placeholders})",
                factor_names,
            )
        return self.query("SELECT * FROM meta_factor_descriptions")

    # ------------------------------------------------------------------
    # Model registry
    # ------------------------------------------------------------------

    _CREATE_MODEL_REGISTRY = """\
CREATE TABLE IF NOT EXISTS meta_model_registry (
    model_name          VARCHAR PRIMARY KEY,
    model_type          VARCHAR NOT NULL,
    framework           VARCHAR,
    version             VARCHAR,
    params_json         JSON,
    metrics_json        JSON,
    artifact_path       VARCHAR,
    status              VARCHAR DEFAULT 'registered',
    description         TEXT,
    created_at          TIMESTAMPTZ NOT NULL,
    updated_at          TIMESTAMPTZ NOT NULL
);"""

    def write_meta_model_registry(self, df: pl.DataFrame) -> int:
        """Upsert model entries into ``meta_model_registry``.

        Creates the table on first call.  Expects *df* to contain at least
        ``model_name`` and ``model_type`` columns.

        Returns the number of rows written.
        """
        if df.is_empty():
            return 0

        self.execute(self._CREATE_MODEL_REGISTRY)

        now = datetime.now(tz=timezone.utc).isoformat()
        df = df.with_columns(
            pl.col("created_at").fill_null(pl.lit(now)) if "created_at" in df.columns else pl.lit(now).alias("created_at"),
            pl.col("updated_at").fill_null(pl.lit(now)) if "updated_at" in df.columns else pl.lit(now).alias("updated_at"),
        )

        columns = df.columns
        rows = [tuple(row) for row in df.iter_rows()]
        conflict_cols = ["model_name"]
        self.upsert("meta_model_registry", columns, rows, conflict_cols)
        return len(rows)

    def read_meta_model_registry(self) -> pl.DataFrame:
        """Read all entries from the model registry."""
        return self.query("SELECT * FROM meta_model_registry")

    def close(self) -> None:
        """Stop the checkpoint thread and close the backend connection.

        DuckDB checkpoints (and removes) the WAL file on clean connection
        close, so closing the catalog is what keeps ``catalog.duckdb.wal``
        from lingering between runs.
        """
        if self._stop_event is not None:
            self._stop_event.set()
        if self._checkpoint_thread is not None and self._checkpoint_thread.is_alive():
            self._checkpoint_thread.join(timeout=5)
        self._backend.close()

    def __enter__(self) -> "Catalog":
        return self

    def __exit__(self, *_: Any) -> None:
        self.close()


def _split_statements(sql: str) -> list[str]:
    """Split a SQL string into individual statements.

    Strips single-line (--) comments and dot-commands (.read) before splitting
    on semicolons, so that comment text never ends up in an executed statement.
    """
    import re
    # Remove single-line comments (-- ...) and dot-commands (.foo)
    no_comments = re.sub(r"(--[^\n]*)", "", sql)
    no_comments = re.sub(r"^\s*\.[^\n]*$", "", no_comments, flags=re.MULTILINE)

    statements = []
    for raw in no_comments.split(";"):
        stmt = raw.strip()
        if stmt:
            statements.append(stmt + ";")
    return statements


def create_catalog() -> Catalog:
    """Create catalog from environment configuration."""
    import os

    backend_type = os.environ.get("CQUANT_DB_BACKEND", "duckdb")
    if backend_type == "postgresql":
        dsn = os.environ["CQUANT_PG_DSN"]
        from cquant.datahub.backends.postgres_backend import PostgresBackend

        return Catalog(backend=PostgresBackend(dsn))
    else:
        db_path = os.environ.get("CQUANT_DB_PATH", "data/catalog.duckdb")
        return Catalog(db_path=db_path)
