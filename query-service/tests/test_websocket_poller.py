"""
WebSocket 日志轮询游标测试
"""
import asyncio
import os
import sys
from typing import Any, Dict, List

import pytest

# 添加 query-service 根目录到 Python 路径
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from api import websocket


class FakeStorageAdapter:
    """记录 execute_query 调用参数的存储桩。"""

    def __init__(self, results: List[Dict[str, Any]]):
        self.results = results
        self.calls: List[Dict[str, Any]] = []

    def execute_query(self, query: str, params: Dict[str, Any] = None) -> List[Dict[str, Any]]:
        self.calls.append(
            {
                "query": " ".join(query.split()),
                "params": params or {},
            }
        )
        return self.results


class SlowWebSocket:
    """模拟慢连接，用于发送超时测试。"""

    async def send_text(self, _text: str):
        await asyncio.sleep(0.2)


class FastWebSocket:
    """模拟正常连接。"""

    def __init__(self):
        self.messages: List[str] = []

    async def send_text(self, text: str):
        self.messages.append(text)


@pytest.fixture(autouse=True)
def inline_to_thread(monkeypatch):
    """避免测试触发真实线程池，提升稳定性与执行速度。"""

    async def _run_inline(func, /, *args, **kwargs):
        return func(*args, **kwargs)

    monkeypatch.setattr(websocket.asyncio, "to_thread", _run_inline)


@pytest.mark.asyncio
async def test_poll_logs_from_clickhouse_uses_compound_cursor():
    """当存在 last_id 时，应使用 (timestamp, id) 复合游标。"""
    storage = FakeStorageAdapter(results=[])

    await websocket.poll_logs_from_clickhouse(
        storage_adapter=storage,
        last_timestamp="2026-03-09 01:02:03",
        last_id="cursor-42",
    )

    assert len(storage.calls) == 1
    call = storage.calls[0]
    query = call["query"]
    params = call["params"]

    assert (
        "timestamp > toDateTime64({last_timestamp:String}, 9, 'UTC') OR "
        "(timestamp = toDateTime64({last_timestamp:String}, 9, 'UTC') AND id > {last_id:String})"
    ) in query
    assert "ORDER BY timestamp ASC, id ASC" in query
    assert params["last_timestamp"] == "2026-03-09 01:02:03"
    assert params["last_id"] == "cursor-42"


@pytest.mark.asyncio
async def test_poll_logs_from_clickhouse_without_last_id_uses_timestamp_only():
    """未提供 last_id 时保留 timestamp 游标条件。"""
    storage = FakeStorageAdapter(results=[])

    await websocket.poll_logs_from_clickhouse(
        storage_adapter=storage,
        last_timestamp="2026-03-09 01:02:03",
        last_id=None,
    )

    assert len(storage.calls) == 1
    call = storage.calls[0]
    query = call["query"]
    params = call["params"]

    assert "timestamp > toDateTime64({last_timestamp:String}, 9, 'UTC')" in query
    assert "id > {last_id:String}" not in query
    assert "last_id" not in params


@pytest.mark.asyncio
async def test_poll_logs_from_clickhouse_timeout_returns_empty(monkeypatch):
    """查询超时时应返回空结果，避免轮询协程被长期阻塞。"""

    async def slow_to_thread(_func, /, *_args, **_kwargs):
        await asyncio.sleep(0.2)
        return []

    monkeypatch.setattr(websocket.asyncio, "to_thread", slow_to_thread)
    monkeypatch.setattr(websocket, "POLL_QUERY_TIMEOUT_SECONDS", 0.01)

    results = await websocket.poll_logs_from_clickhouse(
        storage_adapter=object(),
        last_timestamp="2026-03-09 01:02:03",
    )

    assert results == []


@pytest.mark.asyncio
async def test_log_poller_carries_last_id_between_rounds(monkeypatch):
    """轮询器在下一轮查询应带上上一轮最后一条日志 id。"""
    poll_calls: List[Dict[str, Any]] = []
    broadcasts: List[Dict[str, Any]] = []

    async def fake_poll(storage_adapter, last_timestamp: str, last_id: str = None, filters=None):
        poll_calls.append({"last_timestamp": last_timestamp, "last_id": last_id})
        if len(poll_calls) == 1:
            return [
                {"id": "id-1", "timestamp": "2026-03-09 10:00:00.000000000", "message": "m1"},
                {"id": "id-2", "timestamp": "2026-03-09 10:00:00.000000000", "message": "m2"},
            ]
        return []

    async def fake_broadcast(message: Dict[str, Any], channel: str = None):
        broadcasts.append({"message": message, "channel": channel})

    sleep_calls = {"count": 0}

    async def fake_sleep(_: float):
        sleep_calls["count"] += 1
        if sleep_calls["count"] >= 2:
            raise asyncio.CancelledError

    monkeypatch.setattr(websocket, "poll_logs_from_clickhouse", fake_poll)
    monkeypatch.setattr(websocket.manager, "has_connections", lambda channel=None: True)
    monkeypatch.setattr(websocket.manager, "broadcast", fake_broadcast)
    monkeypatch.setattr(websocket.asyncio, "sleep", fake_sleep)

    await websocket.log_poller(storage_adapter=object(), interval=0.01)

    assert len(poll_calls) == 2
    assert poll_calls[0]["last_id"] is None
    assert poll_calls[1]["last_timestamp"] == "2026-03-09 10:00:00.000000000"
    assert poll_calls[1]["last_id"] == "id-2"
    assert len(broadcasts) == 2


@pytest.mark.asyncio
async def test_connection_manager_send_to_timeout_disconnects(monkeypatch):
    """慢连接发送超时后应自动断开，避免阻塞。"""
    manager = websocket.ConnectionManager()
    ws = SlowWebSocket()
    manager.active_connections.add(ws)

    monkeypatch.setattr(websocket, "WS_SEND_TIMEOUT_SECONDS", 0.01)

    await manager.send_to(ws, {"type": "ping"})

    assert ws not in manager.active_connections


@pytest.mark.asyncio
async def test_connection_manager_send_to_success_keeps_connection():
    """正常连接发送成功后应保持在线。"""
    manager = websocket.ConnectionManager()
    ws = FastWebSocket()
    manager.active_connections.add(ws)

    await manager.send_to(ws, {"type": "ping"})

    assert ws in manager.active_connections
    assert len(ws.messages) == 1
