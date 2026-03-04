"""Data quality route helper tests."""

from datetime import datetime, timedelta, timezone
from typing import Any, Dict, List

import pytest

from api import data_quality


class FakeStorageAdapter:
    """Simple storage stub for data quality handlers."""

    def __init__(self):
        self.calls: List[str] = []

    @property
    def ch_client(self):
        return object()

    def execute_query(self, query: str, params: Dict[str, Any] = None) -> List[Dict[str, Any]]:
        self.calls.append(" ".join(query.split()))
        sql = query.lower()

        if "count(*) as total" in sql:
            return [
                {
                    "total": 1000,
                    "unknown_count": 20,
                    "empty_count": 3,
                    "null_count": 1,
                    "empty_pod_count": 5,
                    "null_pod_count": 2,
                    "latest_timestamp": datetime.now(timezone.utc) - timedelta(seconds=60),
                    "earliest_timestamp": datetime.now(timezone.utc) - timedelta(hours=24),
                }
            ]

        if "substring(message, 1, 150)" in sql:
            return [
                {
                    "message": "request failed",
                    "timestamp": datetime(2026, 3, 1, 8, 0, tzinfo=timezone.utc),
                    "pod_name": "gateway-abc",
                    "namespace": "prod",
                }
            ]

        if "tostartofhour(timestamp)" in sql:
            return [
                {
                    "time_bucket": datetime(2026, 3, 1, 8, 0, tzinfo=timezone.utc),
                    "count": 12,
                }
            ]

        if "group by service_name" in sql and "from logs.logs" in sql:
            return [
                {"service_name": "gateway", "count": 30, "total": 50},
                {"service_name": "order", "count": 20, "total": 50},
            ]

        if "substring(message, 1, 200)" in sql:
            return [{"id": "x1"}, {"id": "x2"}]

        return []


@pytest.fixture(autouse=True)
def reset_storage():
    data_quality.set_storage_adapter(None)
    yield
    data_quality.set_storage_adapter(None)


@pytest.mark.asyncio
async def test_get_data_quality_overview_dict_rows():
    data_quality.set_storage_adapter(FakeStorageAdapter())

    result = await data_quality.get_data_quality_overview()

    assert result["status"] in {"healthy", "warning", "error"}
    assert result["total_records"] == 1000
    assert result["metrics"]["unknown"]["count"] == 20
    assert "generated_at" in result


@pytest.mark.asyncio
async def test_unknown_analysis_and_distribution():
    data_quality.set_storage_adapter(FakeStorageAdapter())

    unknown = await data_quality.get_unknown_analysis(limit=10)
    assert unknown["status"] == "ok"
    assert len(unknown["samples"]) == 1
    assert unknown["samples"][0]["namespace"] == "prod"
    assert len(unknown["time_distribution"]) == 1

    distribution = await data_quality.get_service_distribution(limit=10)
    assert distribution["status"] == "ok"
    assert distribution["total_services"] == 2
    assert distribution["services"][0]["service_name"] == "gateway"


@pytest.mark.asyncio
async def test_reprocess_unknown_data_dry_run():
    data_quality.set_storage_adapter(FakeStorageAdapter())

    result = await data_quality.reprocess_unknown_data(time_range="24 HOUR", dry_run=True)
    assert result["status"] == "ok"
    assert result["dry_run"] is True
    assert result["scanned"] == 2
