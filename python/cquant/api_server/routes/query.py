"""Read-only data browser query routes.

Structured (non-raw-SQL) query endpoint over a whitelist of tables.
All identifiers (table names, column names, operators, sort direction) are
validated against allowlists; all values are passed as SQL parameters —
the request body can never inject SQL text.
"""

from __future__ import annotations

import logging
import re
from datetime import date
from typing import Any

from fastapi import APIRouter, HTTPException
from pydantic import BaseModel, Field

from cquant.api_server.deps import CatalogDep

logger = logging.getLogger(__name__)
router = APIRouter(prefix="/query", tags=["query"])

# ---------------------------------------------------------------------------
# Allowlists — the only tables/columns/operators the endpoint will touch.
# ---------------------------------------------------------------------------

#: Read-only whitelist of queryable tables (silver data + meta config).
ALLOWED_TABLES: frozenset[str] = frozenset({
    "silver_prices_1d",
    "silver_assets",
    "silver_fundamentals",
    "silver_valuation_daily",
    "silver_external_indicators",
    "silver_corporate_actions",
    "silver_trading_calendar",
    "silver_dataset_versions",
    "gold_factor_values",
    "meta_strategy_configs",
    "meta_factor_analytics",
    "meta_factor_descriptions",
    "meta_ml_jobs",
    "meta_model_registry",
    "meta_custom_factors",
    "meta_custom_universes",
})

#: Structured WHERE operators (values always parameterized; NULL ops take none).
ALLOWED_OPS: frozenset[str] = frozenset({
    "=", "!=", ">", "<", ">=", "<=", "LIKE", "IN", "IS NULL", "IS NOT NULL",
})

#: Sort directions.
ALLOWED_DIRS: frozenset[str] = frozenset({"asc", "desc"})

#: Hard cap on rows returned per request.
MAX_LIMIT = 1000

_IDENT_RE = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*$")


class WhereClause(BaseModel):
    col: str
    op: str
    val: Any = None


class OrderByClause(BaseModel):
    col: str
    dir: str = "asc"


class QueryBody(BaseModel):
    table: str
    columns: list[str] | None = None   # None → SELECT *
    where: list[WhereClause] | None = None
    order_by: list[OrderByClause] | None = None
    limit: int = Field(default=200, ge=1, le=MAX_LIMIT)


class CoverageBody(BaseModel):
    start_date: date
    end_date: date
    sample_limit: int = Field(default=20, ge=1, le=200)
    include_indices: bool = False


# ---------------------------------------------------------------------------
# Validation helpers
# ---------------------------------------------------------------------------

def _table_columns(catalog: Any, table: str) -> list[str]:
    """Fetch the real column list from information_schema (with caching)."""
    df = catalog.query(
        "SELECT column_name FROM information_schema.columns "
        "WHERE table_name = ? ORDER BY ordinal_position",
        [table],
    )
    return df["column_name"].to_list()


def _validate_ident(value: str, kind: str) -> str:
    """Reject anything that is not a plain identifier (injection guard)."""
    if not value or not _IDENT_RE.match(value):
        raise HTTPException(
            status_code=400,
            detail=f"Invalid {kind}: {value!r}",
        )
    return value


def _build_sql(body: QueryBody, valid_columns: list[str]) -> tuple[str, list[Any]]:
    """Compose the parameterized SELECT. Identifiers are pre-validated;
    values only ever enter the SQL as ``?`` parameters."""
    col_set = set(valid_columns)

    if body.columns is None:
        select_expr = "*"
    else:
        cols = [_validate_ident(c, "column") for c in body.columns]
        unknown = [c for c in cols if c not in col_set]
        if unknown:
            raise HTTPException(
                status_code=400,
                detail=f"Unknown columns for {body.table}: {unknown}",
            )
        select_expr = ", ".join(f'"{c}"' for c in cols)

    params: list[Any] = []
    clauses: list[str] = []
    for w in body.where or []:
        col = _validate_ident(w.col, "column")
        if col not in col_set:
            raise HTTPException(
                status_code=400, detail=f"Unknown column in WHERE: {w.col!r}"
            )
        op = w.op.upper() if w.op.upper() in ("IS NULL", "IS NOT NULL") else w.op
        if op not in ALLOWED_OPS:
            raise HTTPException(status_code=400, detail=f"Operator not allowed: {w.op!r}")
        if op in ("IS NULL", "IS NOT NULL"):
            clauses.append(f'"{col}" {op}')
        elif op == "IN":
            if not isinstance(w.val, list) or not w.val:
                raise HTTPException(
                    status_code=400, detail="IN operator requires a non-empty list value"
                )
            placeholders = ", ".join("?" for _ in w.val)
            clauses.append(f'"{col}" IN ({placeholders})')
            params.extend(w.val)
        else:
            clauses.append(f'"{col}" {op} ?')
            params.append(w.val)

    order_parts: list[str] = []
    for ob in body.order_by or []:
        col = _validate_ident(ob.col, "column")
        if col not in col_set:
            raise HTTPException(
                status_code=400, detail=f"Unknown column in ORDER BY: {ob.col!r}"
            )
        direction = ob.dir.lower()
        if direction not in ALLOWED_DIRS:
            raise HTTPException(status_code=400, detail=f"Invalid sort dir: {ob.dir!r}")
        order_parts.append(f'"{col}" {direction.upper()}')

    sql = f'SELECT {select_expr} FROM "{body.table}"'
    if clauses:
        sql += " WHERE " + " AND ".join(clauses)
    if order_parts:
        sql += " ORDER BY " + ", ".join(order_parts)
    sql += " LIMIT ?"
    params.append(min(body.limit, MAX_LIMIT))
    return sql, params


def _serialize_rows(df) -> list[dict]:
    """Make DuckDB/Polars types JSON-safe."""
    import datetime as _dt

    out: list[dict] = []
    for row in df.to_dicts():
        clean = {}
        for k, v in row.items():
            if isinstance(v, (date, _dt.datetime)):
                clean[k] = v.isoformat()
            elif isinstance(v, _dt.timedelta):
                clean[k] = v.total_seconds()
            elif isinstance(v, (bytes, bytearray)):
                clean[k] = v.hex()
            elif hasattr(v, "__float__"):
                clean[k] = float(v)
            else:
                clean[k] = v
        out.append(clean)
    return out


# ---------------------------------------------------------------------------
# Endpoints
# ---------------------------------------------------------------------------

@router.get("/tables")
async def list_queryable_tables() -> dict:
    """Return the table whitelist (for the browser dropdown)."""
    return {"tables": sorted(ALLOWED_TABLES)}


@router.post("")
async def structured_query(body: QueryBody, catalog: CatalogDep) -> dict:
    """Execute a validated, structured SELECT against a whitelisted table."""
    if body.table not in ALLOWED_TABLES:
        raise HTTPException(
            status_code=400,
            detail=f"Table not allowed: {body.table!r}. "
                   f"Allowed: {sorted(ALLOWED_TABLES)}",
        )

    try:
        valid_columns = _table_columns(catalog, body.table)
    except Exception as exc:
        logger.warning("query: information_schema lookup failed: %s", exc)
        raise HTTPException(status_code=500, detail="Schema lookup failed") from exc

    if not valid_columns:
        # Table in whitelist but not created yet in this database.
        return {"columns": [], "rows": [], "total": 0, "table": body.table}

    sql, params = _build_sql(body, valid_columns)
    try:
        df = catalog.query(sql, params)
    except Exception as exc:
        logger.warning("query: execution failed: %s (%s params)", exc, params)
        raise HTTPException(status_code=400, detail=f"Query failed: {exc}") from exc

    return {
        "table": body.table,
        "columns": list(df.columns),
        "rows": _serialize_rows(df),
        "total": df.height,
    }


@router.post("/coverage")
async def coverage_query(body: CoverageBody, catalog: CatalogDep) -> dict:
    """Preset: how many stocks had prices in [start_date, end_date].

    Excludes index assets (``%:88%``) unless ``include_indices`` is set.
    Returns the distinct count plus a sample asset list.
    """
    if body.start_date > body.end_date:
        raise HTTPException(
            status_code=400, detail="start_date must be <= end_date"
        )
    idx_cond = "" if body.include_indices else "AND asset_id NOT LIKE '%:88%'"

    try:
        count_df = catalog.query(
            f"SELECT COUNT(DISTINCT asset_id) AS n_assets "
            f"FROM silver_prices_1d "
            f"WHERE trade_date BETWEEN ? AND ? {idx_cond}",
            [body.start_date, body.end_date],
        )
        sample_df = catalog.query(
            f"SELECT DISTINCT asset_id "
            f"FROM silver_prices_1d "
            f"WHERE trade_date BETWEEN ? AND ? {idx_cond} "
            f"ORDER BY asset_id LIMIT ?",
            [body.start_date, body.end_date, body.sample_limit],
        )
    except Exception as exc:
        logger.warning("coverage query failed: %s", exc)
        raise HTTPException(status_code=400, detail=f"Coverage query failed: {exc}") from exc

    n_assets = int(count_df.to_dicts()[0].get("n_assets", 0)) if not count_df.is_empty() else 0
    sample = sample_df["asset_id"].to_list() if not sample_df.is_empty() else []
    return {
        "start_date": body.start_date.isoformat(),
        "end_date": body.end_date.isoformat(),
        "n_assets": n_assets,
        "sample_assets": sample,
        "include_indices": body.include_indices,
    }
