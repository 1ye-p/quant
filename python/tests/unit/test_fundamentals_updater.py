"""Unit tests for cquant.datahub.pipelines.fundamentals_updater."""

from __future__ import annotations

from datetime import time
from unittest.mock import MagicMock, patch

import pytest


class TestUpdateFundamentals:
    def test_unsupported_source_raises_value_error(self) -> None:
        from cquant.datahub.pipelines.fundamentals_updater import update_fundamentals
        catalog = MagicMock()
        with pytest.raises(ValueError, match="Unsupported fundamentals source"):
            update_fundamentals(catalog, source="bloomberg", asset_ids=["SSE:600036"])

    def test_empty_asset_list_returns_zero(self) -> None:
        from cquant.datahub.pipelines.fundamentals_updater import update_fundamentals
        catalog = MagicMock()
        result = update_fundamentals(catalog, source="tushare", asset_ids=[])
        assert result == 0

    def test_tushare_connector_unavailable_returns_zero(self) -> None:
        from cquant.datahub.pipelines.fundamentals_updater import update_fundamentals
        catalog = MagicMock()
        with patch(
            "cquant.datahub.pipelines.fundamentals_updater._update_from_tushare",
            return_value=0,
        ):
            result = update_fundamentals(catalog, source="tushare", asset_ids=["SSE:600036"])
        assert result == 0

    def test_upsert_records_returns_written_count(self) -> None:
        from cquant.datahub.pipelines.fundamentals_updater import _upsert_records
        catalog = MagicMock()
        records = [
            {"asset_id": "SSE:600036", "report_date": "2026-01-01", "pe_ttm": 12.5},
            {"asset_id": "SSE:000001", "report_date": "2026-01-01", "pe_ttm": 8.3},
        ]
        result = _upsert_records(catalog, records, "2026-01-01T00:00:00+00:00")
        assert result == 2
        assert catalog.execute.call_count == 2

    def test_upsert_empty_records_returns_zero(self) -> None:
        from cquant.datahub.pipelines.fundamentals_updater import _upsert_records
        catalog = MagicMock()
        result = _upsert_records(catalog, [], "2026-01-01")
        assert result == 0
        catalog.execute.assert_not_called()

    def test_upsert_db_error_per_row_continues(self) -> None:
        from cquant.datahub.pipelines.fundamentals_updater import _upsert_records
        catalog = MagicMock()
        catalog.execute.side_effect = [Exception("DB locked"), None]
        records = [
            {"asset_id": "SSE:600036", "report_date": "2026-01-01"},
            {"asset_id": "SSE:000001", "report_date": "2026-01-01"},
        ]
        result = _upsert_records(catalog, records, "2026-01-01")
        assert result == 1  # first failed, second succeeded


class TestAkshareAnnounceDate:
    """akshare 路径 announce_date 必须按 D4-A 固定披露截止日推导（> report_date）。"""

    @pytest.mark.parametrize(
        "report_date, expected_announce",
        [
            ("2025-03-31", "2025-04-30"),
            ("2025-06-30", "2025-08-31"),
            ("2025-09-30", "2025-10-31"),
            ("2024-12-31", "2025-04-30"),
        ],
    )
    def test_announce_date_uses_fixed_deadline(self, report_date, expected_announce) -> None:
        import sys
        from types import ModuleType
        from unittest.mock import patch

        import pandas as pd

        from cquant.datahub.pipelines import fundamentals_updater as fu

        fake_ak = ModuleType("akshare")
        fake_ak.stock_financial_abstract = lambda symbol: pd.DataFrame(
            {"item": ["净资产收益率"], "value": ["12.5%"]}
        )

        captured: dict = {}

        def fake_upsert(_catalog, records, _updated_at):
            captured["records"] = records
            return len(records)

        with patch.dict(sys.modules, {"akshare": fake_ak}), patch.object(
            fu, "_upsert_records", side_effect=fake_upsert
        ):
            n = fu._update_from_akshare(
                MagicMock(), ["SSE:600036"], report_date, "2025-05-01T00:00:00+00:00"
            )

        assert n == 1
        row = captured["records"][0]
        assert row["announce_date"] == expected_announce
        assert row["announce_date"] > row["report_date"]


class TestRegisterFundamentalsJob:
    def test_registers_job_with_scheduler(self) -> None:
        from cquant.datahub.pipelines.fundamentals_updater import register_fundamentals_job
        scheduler = MagicMock()
        catalog = MagicMock()
        register_fundamentals_job(scheduler, catalog)
        scheduler.add_job.assert_called_once()
        config = scheduler.add_job.call_args[0][0]
        assert config.job_id == "daily_fundamentals_update"
        assert config.run_time == time(18, 0)
        assert config.enabled is True

    def test_registers_with_custom_run_time(self) -> None:
        from cquant.datahub.pipelines.fundamentals_updater import register_fundamentals_job
        scheduler = MagicMock()
        catalog = MagicMock()
        register_fundamentals_job(scheduler, catalog, run_hour=20, run_minute=30)
        config = scheduler.add_job.call_args[0][0]
        assert config.run_time == time(20, 30)

    def test_callback_is_callable(self) -> None:
        from cquant.datahub.pipelines.fundamentals_updater import register_fundamentals_job
        scheduler = MagicMock()
        catalog = MagicMock()
        register_fundamentals_job(scheduler, catalog, source="akshare")
        callback = scheduler.add_job.call_args[0][1]
        assert callable(callback)
