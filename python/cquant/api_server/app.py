"""cquant.api_server.app — FastAPI application factory.

Usage::

    # Run locally:
    uvicorn cquant.api_server.app:app --host 0.0.0.0 --port 8000 --reload

    # Or via Python:
    from cquant.api_server.app import create_app
    app = create_app()
"""

from __future__ import annotations


# Disable system proxy for data fetching
import os
os.environ.setdefault("NO_PROXY", "*")
os.environ.setdefault("no_proxy", "*")
os.environ.pop("HTTP_PROXY", None)
os.environ.pop("HTTPS_PROXY", None)
os.environ.pop("http_proxy", None)
os.environ.pop("https_proxy", None)

# Load .env file
try:
    from dotenv import load_dotenv
    load_dotenv()
except ImportError:
    pass
import logging
from contextlib import asynccontextmanager

from fastapi import FastAPI, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse

from cquant.api_server.routes import (
    advisor,
    backtests,
    datasets,
    factors,
    indicators,
    live,
    market,
    ml,
    news,
    optimize,
    pipeline,
    risk,
    scoring,
    share,
    strategies,
    health,
    knowledge,
    plugins,
    trading,
    alerts,
    jobs,
    metrics,
)
from cquant.api_server.middleware import (
    StructuredLoggingMiddleware,
    configure_structured_logging,
)

logger = logging.getLogger(__name__)

_VERSION = "0.1.0"
_TITLE = "cQuant Research API"
_DESCRIPTION = (
    "REST API for the cQuant quantitative research and backtesting platform. "
    "Provides access to market data, factor values, backtest results, "
    "the knowledge base, AI research advisor, and trading operations."
)


def _get_limiter_key(request: Request) -> str:
    """Use client IP as rate limit key."""
    return request.client.host if request.client else "unknown"


@asynccontextmanager
async def _lifespan(app: FastAPI):
    """Manage startup and shutdown lifecycle."""
    from cquant.api_server.data_scheduler import start_data_scheduler
    from cquant.api_server.deps import get_catalog
    app.state.data_scheduler = start_data_scheduler(get_catalog())
    # 每小时告警检查
    scheduler = app.state.data_scheduler
    if scheduler is not None:
        from cquant.api_server.alert_checker import run_all_checks
        catalog_for_alerts = get_catalog()
        try:
            scheduler.add_job(
                run_all_checks,
                "interval",
                hours=1,
                id="hourly_alert_check",
                args=[catalog_for_alerts],
                replace_existing=True,
            )
            logger.info("Hourly alert check registered")
        except Exception as exc:
            logger.warning("Failed to register hourly_alert_check: %s", exc)
    yield
    scheduler = getattr(app.state, "data_scheduler", None)
    if scheduler is not None:
        try:
            scheduler.shutdown(wait=False)
            logger.info("DataScheduler shut down cleanly")
        except Exception as exc:
            logger.debug("DataScheduler shutdown error: %s", exc)
    # WAL governance: close the catalog DuckDB connection cleanly — DuckDB
    # checkpoints and removes the .wal file on close. Never block exit, but
    # make failures visible.
    try:
        from cquant.api_server.deps import _get_catalog, get_catalog

        get_catalog().close()
        _get_catalog.cache_clear()
        logger.info("Catalog closed cleanly on shutdown (WAL checkpointed)")
    except Exception as exc:
        logger.error("catalog close on shutdown failed: %s", exc)


def create_app(
    *,
    cors_origins: list[str] | None = None,
    debug: bool = False,
) -> FastAPI:
    """Create and configure the FastAPI application."""
    app = FastAPI(
        title=_TITLE,
        description=_DESCRIPTION,
        version=_VERSION,
        docs_url="/api/docs",
        redoc_url="/api/redoc",
        openapi_url="/api/openapi.json",
        debug=debug,
        lifespan=_lifespan,
    )

    # ── Structured logging ────────────────────────────────────────────────────
    # Install the JSON formatter early so every subsequent log line (CORS,
    # rate limiting, startup banner) is structured. Set CQUANT_LOG_JSON=0 for
    # a human-readable format during local development.
    configure_structured_logging()

    # ── Structured request logging + Prometheus http_requests_total ───────────
    app.add_middleware(StructuredLoggingMiddleware)

    # ── CORS ──────────────────────────────────────────────────────────────────
    origins = cors_origins or ["http://localhost:3000", "http://localhost:5173"]
    app.add_middleware(
        CORSMiddleware,
        allow_origins=origins,
        allow_credentials=True,
        allow_methods=["*"],
        allow_headers=["*"],
    )

    # ── Rate Limiting ─────────────────────────────────────────────────────────
    from slowapi import Limiter
    from slowapi.errors import RateLimitExceeded

    limiter = Limiter(key_func=_get_limiter_key, default_limits=["100/second"])
    app.state.limiter = limiter

    @app.exception_handler(RateLimitExceeded)
    async def _rate_limit_handler(request: Request, exc: RateLimitExceeded) -> JSONResponse:
        return JSONResponse(
            status_code=429,
            content={"detail": "Rate limit exceeded. Please slow down.", "code": "rate_limited"},
        )

    # Per-path rate limiting middleware: /trading/* at 10/s, others at 100/s (default)
    import time
    from collections import defaultdict

    _request_counts: dict[str, list[float]] = defaultdict(list)
    _TRADING_LIMIT = 10  # req/s
    _DEFAULT_LIMIT = 100  # req/s

    @app.middleware("http")
    async def _rate_limit_middleware(request: Request, call_next):
        client_ip = request.client.host if request.client else "unknown"
        path = request.url.path
        now = time.time()

        is_trading = path.startswith("/api/v1/trading/")
        limit = _TRADING_LIMIT if is_trading else _DEFAULT_LIMIT
        window_key = f"{client_ip}:{'trading' if is_trading else 'default'}"

        # Clean old entries (1-second window)
        _request_counts[window_key] = [
            t for t in _request_counts[window_key] if now - t < 1.0
        ]

        if len(_request_counts[window_key]) >= limit:
            return JSONResponse(
                status_code=429,
                content={"detail": "Rate limit exceeded. Please slow down.", "code": "rate_limited"},
            )

        _request_counts[window_key].append(now)
        return await call_next(request)

    # ── Global exception handler ───────────────────────────────────────────────
    @app.exception_handler(Exception)
    async def _global_handler(request: Request, exc: Exception) -> JSONResponse:
        logger.exception("Unhandled exception on %s %s", request.method, request.url)
        return JSONResponse(
            status_code=500,
            content={"detail": "Internal server error", "code": "internal_error"},
        )

    # ── Routers ────────────────────────────────────────────────────────────────
    from fastapi import Depends
    from cquant.api_server.deps import verify_api_key

    _auth = [Depends(verify_api_key)]

    prefix = "/api/v1"
    app.include_router(health.router)
    # /metrics is intentionally unauthenticated so Prometheus can scrape it
    # without an API key; restrict it at the reverse proxy in production.
    app.include_router(metrics.router)
    app.include_router(datasets.router, prefix=prefix, dependencies=_auth)
    app.include_router(factors.router, prefix=prefix, dependencies=_auth)
    app.include_router(backtests.router, prefix=prefix, dependencies=_auth)
    app.include_router(knowledge.router, prefix=prefix, dependencies=_auth)
    app.include_router(advisor.router, prefix=prefix, dependencies=_auth)
    app.include_router(plugins.router, prefix=prefix, dependencies=_auth)
    app.include_router(news.router, prefix=prefix, dependencies=_auth)
    app.include_router(strategies.router, prefix=prefix, dependencies=_auth)
    app.include_router(ml.router, prefix=prefix, dependencies=_auth)
    app.include_router(live.router, prefix=prefix, dependencies=_auth)
    app.include_router(trading.router, prefix=prefix, dependencies=_auth)
    app.include_router(optimize.router, prefix=prefix, dependencies=_auth)
    app.include_router(risk.router, prefix=prefix, dependencies=_auth)
    app.include_router(scoring.router, prefix=prefix, dependencies=_auth)
    app.include_router(alerts.router, prefix=prefix, dependencies=_auth)
    app.include_router(jobs.router, prefix=prefix, dependencies=_auth)
    app.include_router(pipeline.router, prefix=prefix, dependencies=_auth)
    app.include_router(indicators.router, prefix=prefix, dependencies=_auth)
    app.include_router(market.router, prefix=prefix, dependencies=_auth)
    app.include_router(share.router, prefix=prefix, dependencies=_auth)

    logger.info("cQuant API v%s ready — docs at /api/docs", _VERSION)
    return app


# Module-level app instance for uvicorn
app: FastAPI = create_app()


