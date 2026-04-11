"""
Tests for graph/service_sync.py.
"""
from datetime import datetime, timezone
from typing import Any, Dict, List

import pytest

from graph import service_sync


class _FakeClickHouseClient:
    """可控返回值的 ClickHouse 客户端桩。"""

    def __init__(self):
        self.queries: List[str] = []

    def execute(self, query: str):
        normalized = " ".join(query.split())
        self.queries.append(normalized)

        if "FROM system.columns" in normalized:
            return [
                ("logs", "service_name"),
                ("logs", "timestamp"),
                ("traces", "service_name"),
                ("traces", "timestamp"),
                ("events", "service_name"),
                ("events", "timestamp"),
                ("metrics", "service_name"),
                ("metrics", "timestamp"),
            ]

        if "FROM logs.logs" in normalized:
            return [
                ("frontend", 10, datetime(2026, 3, 2, 1, 0, tzinfo=timezone.utc)),
                ("unknown", 5, datetime(2026, 3, 2, 1, 5, tzinfo=timezone.utc)),
            ]

        if "FROM logs.traces" in normalized:
            return [
                ("query-service", 8, datetime(2026, 3, 2, 2, 0, tzinfo=timezone.utc)),
                ("semantic-engine", 5, datetime(2026, 3, 2, 3, 0, tzinfo=timezone.utc)),
            ]

        if "FROM logs.events" in normalized:
            return [
                ("semantic-engine", 2, datetime(2026, 3, 2, 4, 0, tzinfo=timezone.utc)),
            ]

        if "FROM logs.metrics" in normalized:
            return [
                ("query-service", 3, datetime(2026, 3, 2, 5, 0, tzinfo=timezone.utc)),
            ]

        return []


class _FakeSession:
    """Neo4j Session 桩，模拟同步前后覆盖变化。"""

    def __init__(self):
        self.run_calls: List[Dict[str, Any]] = []
        self._service_query_count = 0

    def run(self, query: str, **params):
        normalized = " ".join(query.split())
        self.run_calls.append({"query": normalized, "params": params})

        if "RETURN s.id AS service_id" in normalized:
            self._service_query_count += 1
            if self._service_query_count == 1:
                return [{"service_id": "frontend"}]
            return [
                {"service_id": "frontend"},
                {"service_id": "query-service"},
                {"service_id": "semantic-engine"},
            ]

        # UNWIND 批量写入语句无需返回内容。
        return []

    def __enter__(self):
        return self

    def __exit__(self, exc_type, exc, tb):
        return False


class _FakeNeo4jDriver:
    def __init__(self):
        self.session_obj = _FakeSession()

    def session(self):
        return self.session_obj


class _FakeStorage:
    def __init__(self, with_neo4j: bool = True):
        self.ch_client = _FakeClickHouseClient()
        self.neo4j_driver = _FakeNeo4jDriver() if with_neo4j else None


def test_collect_clickhouse_service_inventory_multi_source():
    storage = _FakeStorage()
    inventory = service_sync._collect_clickhouse_service_inventory(storage)

    assert set(inventory.keys()) == {"frontend", "query-service", "semantic-engine"}
    assert inventory["frontend"]["logs_count"] == 10
    assert inventory["query-service"]["traces_count"] == 8
    assert inventory["query-service"]["metrics_count"] == 3
    assert inventory["semantic-engine"]["events_count"] == 2
    assert inventory["semantic-engine"]["total_count"] == 7


@pytest.mark.asyncio
async def test_sync_services_from_logs_improves_coverage():
    storage = _FakeStorage()

    result = await service_sync.sync_services_from_logs(storage)

    assert result["status"] == "completed"
    assert result["total_services"] == 3
    assert result["synced_count"] == 3
    assert result["failed_count"] == 0
    assert result["coverage_before"]["coverage_percent"] == pytest.approx(33.33, abs=0.01)
    assert result["coverage_after"]["coverage_percent"] == 100.0
    assert result["newly_covered_services"] == 2

    merge_calls = [
        call
        for call in storage.neo4j_driver.session_obj.run_calls
        if "UNWIND $services AS svc" in call["query"]
    ]
    assert len(merge_calls) == 1
    payload = merge_calls[0]["params"]["services"]
    assert {item["service_id"] for item in payload} == {"frontend", "query-service", "semantic-engine"}


@pytest.mark.asyncio
async def test_get_sync_status_returns_missing_services():
    storage = _FakeStorage()

    status = await service_sync.get_sync_status(storage)

    assert status["clickhouse_services"] == 3
    assert status["neo4j_services"] == 1
    assert status["missing_services"] == 2
    assert set(status["missing_service_ids"]) == {"query-service", "semantic-engine"}
