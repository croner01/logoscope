"""Regression tests for ClickHouse native client concurrency safety."""

import os
import sys
import threading
import time
from typing import Any, List


# 添加 query-service 根目录到 Python 路径
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from storage import adapter as query_storage_adapter  # noqa: E402
from logoscope_storage import adapter as shared_adapter  # noqa: E402


class _ConcurrencySensitiveClient:
    """Fake ClickHouse client that fails on concurrent execute for same instance."""

    instances: List["_ConcurrencySensitiveClient"] = []
    _instances_lock = threading.Lock()
    _sequence = 0

    def __init__(self, **_: Any):
        with self._instances_lock:
            type(self)._sequence += 1
            self.client_id = type(self)._sequence
            type(self).instances.append(self)
        self._in_flight = 0
        self._in_flight_lock = threading.Lock()

    @classmethod
    def reset(cls) -> None:
        with cls._instances_lock:
            cls.instances = []
            cls._sequence = 0

    def execute(self, query: str, *args: Any, **kwargs: Any):
        with self._in_flight_lock:
            if self._in_flight > 0:
                raise RuntimeError("simultaneous execute on same client")
            self._in_flight += 1

        try:
            # 放大并发窗口，模拟真实请求重叠。
            time.sleep(0.03)
            normalized = " ".join(str(query).split())
            if kwargs.get("with_column_types"):
                rows = [(self.client_id, normalized)]
                columns = [("client_id", "UInt32"), ("sql", "String")]
                return rows, columns
            if normalized == "SELECT 1":
                return [(1,)]
            return [(self.client_id, normalized)]
        finally:
            with self._in_flight_lock:
                self._in_flight -= 1

    def disconnect(self) -> None:
        return None


def test_execute_query_uses_thread_local_native_client(monkeypatch):
    """Concurrent execute_query calls should not share one native client instance."""
    _ConcurrencySensitiveClient.reset()

    monkeypatch.setattr(shared_adapter, "CLICKHOUSE_DRIVER_AVAILABLE", True)
    monkeypatch.setattr(shared_adapter, "NEO4J_AVAILABLE", False)
    monkeypatch.setattr(shared_adapter, "ClickHouseClient", _ConcurrencySensitiveClient)
    monkeypatch.setattr(shared_adapter.StorageAdapter, "_init_tables", lambda self: None)

    storage = query_storage_adapter.StorageAdapter(
        {
            "clickhouse": {
                "host": "localhost",
                "port": 9000,
                "database": "logs",
                "user": "default",
                "password": "",
            },
            "neo4j": {
                "host": "localhost",
                "port": 7687,
                "user": "neo4j",
                "password": "password",
                "database": "neo4j",
            },
        }
    )

    barrier = threading.Barrier(3)
    worker_client_ids: List[int] = []
    errors: List[Exception] = []

    def _worker(idx: int) -> None:
        try:
            barrier.wait(timeout=2.0)
            rows = storage.execute_query(
                "SELECT {value:Int32} AS value",
                {"value": idx},
            )
            worker_client_ids.append(int(rows[0]["client_id"]))
        except Exception as exc:  # pragma: no cover - only for assertion capture
            errors.append(exc)

    threads = [threading.Thread(target=_worker, args=(i,)) for i in (1, 2)]
    for thread in threads:
        thread.start()

    barrier.wait(timeout=2.0)

    for thread in threads:
        thread.join(timeout=2.0)

    assert not errors
    assert len(worker_client_ids) == 2
    # 两个线程应拿到不同 client，避免共享连接并发 execute。
    assert len(set(worker_client_ids)) == 2
    # 启动线程 + 两个工作线程各持有一个 native client。
    assert len(_ConcurrencySensitiveClient.instances) >= 3

    storage.close()
