"""cquant.qlib_bridge.init — Initialize Qlib with multi-source storage backends.

Provides ``init_qlib_with_quantdb()`` which configures Qlib to read all
data from cQuant's data warehouse (or alternative sources) instead of flat files.

Supports multiple data sources via StorageFactory:
- ``quantdb`` (default): cQuant silver layer via Catalog
- ``duckdb``: direct DuckDB access via Catalog
- ``tushare``: Tushare Pro API
- ``akshare``: AKShare open-source API
"""
from __future__ import annotations

import logging
from typing import TYPE_CHECKING

from cquant.qlib_bridge._compat import QLIB_AVAILABLE, require_qlib

if TYPE_CHECKING:
    from cquant.datahub.catalog import Catalog

logger = logging.getLogger(__name__)


def init_qlib_with_quantdb(
    catalog: "Catalog | None" = None,
    region: str = "cn",
    data_source: str | None = None,
    tushare_token: str | None = None,
    **qlib_kwargs,
) -> None:
    """Initialize Qlib with configurable data backend.

    This replaces Qlib's default file-based storage with cQuant-backed
    implementations, so that all calendar, instrument, and feature queries
    are routed to the configured data source.

    Parameters
    ----------
    catalog:
        Initialized ``Catalog`` connection to cQuant's DuckDB/PostgreSQL backend.
        Required for ``quantdb`` and ``duckdb`` data sources.
    region:
        Market region (default ``"cn"`` for China A-shares).
    data_source:
        One of ``"quantdb"``, ``"duckdb"``, ``"tushare"``, ``"akshare"``.
        Falls back to ``CQUANT_QLIB_DATA_SOURCE`` env var, then ``"quantdb"``.
    tushare_token:
        Tushare Pro API token. Falls back to ``TUSHARE_TOKEN`` env var.
        Required for ``tushare`` data source.
    **qlib_kwargs:
        Additional keyword arguments passed to ``qlib.init()``.

    Raises
    ------
    ImportError
        If Qlib is not installed.
    ValueError
        If the data source is invalid or missing required configuration.

    Example
    -------
    ::

        from cquant.datahub.catalog import Catalog
        from cquant.qlib_bridge import init_qlib_with_quantdb

        # QuantDB source (default). The catalog must stay open while Qlib
        # reads from it — close it only when the Qlib session is done.
        # Catalog.close() stops the checkpoint thread and closes the DuckDB
        # connection (which flushes the WAL); if a CHECKPOINT is still in
        # flight after the join timeout, close() deliberately leaves the
        # connection open and logs an ERROR instead of aborting it.
        catalog = Catalog("data/catalog.duckdb")
        catalog.initialize()
        init_qlib_with_quantdb(catalog)
        # ... run Qlib workflows ...
        catalog.close()  # graceful shutdown; WAL checkpointed on clean close

        # Tushare source
        init_qlib_with_quantdb(data_source="tushare", tushare_token="your_token")

        # AKShare source
        init_qlib_with_quantdb(data_source="akshare")
    """
    require_qlib()

    import qlib

    from cquant.qlib_bridge.storage_factory import StorageFactory

    # Create storage factory
    factory = StorageFactory(
        data_source=data_source,
        catalog=catalog,
        tushare_token=tushare_token,
    )

    # Create providers from factory
    calendar_provider = _CalendarProvider(factory)
    instrument_provider = _InstrumentProvider(factory)
    feature_provider = _FeatureProvider(factory)

    # Build the qlib init kwargs
    init_config = {
        "region": region,
        "calendar_provider": calendar_provider,
        "instrument_provider": instrument_provider,
        "feature_provider": feature_provider,
    }
    init_config.update(qlib_kwargs)

    logger.info(
        "init_qlib_with_quantdb: initializing Qlib with region=%s, "
        "data_source=%s, calendar/instrument/feature providers",
        region,
        factory.data_source,
    )

    qlib.init(**init_config)

    logger.info("init_qlib_with_quantdb: Qlib initialized successfully (source=%s)", factory.data_source)


# ---------------------------------------------------------------------------
# Provider wrappers that delegate to StorageFactory
# ---------------------------------------------------------------------------

class _CalendarProvider:
    """Provider that creates CalendarStorage via StorageFactory."""

    def __init__(self, factory: "StorageFactory") -> None:
        self._factory = factory

    def __call__(self, freq: str = "day", future: bool = False, **kwargs):
        return self._factory.create_calendar_storage(freq=freq, future=future)


class _InstrumentProvider:
    """Provider that creates InstrumentStorage via StorageFactory."""

    def __init__(self, factory: "StorageFactory") -> None:
        self._factory = factory

    def __call__(self, market: str = "all", freq: str = "day", **kwargs):
        return self._factory.create_instrument_storage(market=market, freq=freq)


class _FeatureProvider:
    """Provider that creates FeatureStorage via StorageFactory."""

    def __init__(self, factory: "StorageFactory") -> None:
        self._factory = factory

    def __call__(self, instrument: str = "", field: str = "", freq: str = "day", **kwargs):
        return self._factory.create_feature_storage(instrument=instrument, field=field, freq=freq)
