"""
msgqueue 重试与路由行为测试
"""
import asyncio
import pytest

from msgqueue import redis_adapter as redis_adapter_module
from msgqueue.interface import Message
from msgqueue.redis_adapter import RedisStreamAdapter, MAX_RETRY_ATTEMPTS

try:
    from msgqueue.worker import LogWorker
except Exception:
    LogWorker = None


def test_process_with_retry_treats_false_as_failure(monkeypatch):
    """回调返回 False 时应触发重试并最终失败。"""
    monkeypatch.setattr(redis_adapter_module, "RETRY_DELAY_SECONDS", 0)

    adapter = object.__new__(RedisStreamAdapter)
    attempts = {"count": 0}

    async def callback(_message: Message):
        attempts["count"] += 1
        return False

    message = Message(subject="logs.raw", data=b"{}", headers={})
    success, retry_count = asyncio.run(
        adapter._process_with_retry(callback, message, "1-0")
    )

    assert success is False
    assert retry_count == MAX_RETRY_ATTEMPTS
    assert attempts["count"] == MAX_RETRY_ATTEMPTS


def test_process_with_retry_success_on_first_attempt(monkeypatch):
    """回调成功时应立即返回，不进行额外重试。"""
    monkeypatch.setattr(redis_adapter_module, "RETRY_DELAY_SECONDS", 0)

    adapter = object.__new__(RedisStreamAdapter)
    attempts = {"count": 0}

    async def callback(_message: Message):
        attempts["count"] += 1
        return True

    message = Message(subject="logs.raw", data=b"{}", headers={})
    success, retry_count = asyncio.run(
        adapter._process_with_retry(callback, message, "1-1")
    )

    assert success is True
    assert retry_count == 1
    assert attempts["count"] == 1


def test_resolve_consumer_name_prefers_explicit_env(monkeypatch):
    adapter = object.__new__(RedisStreamAdapter)
    monkeypatch.setenv("REDIS_CONSUMER_NAME", "Worker_A")
    monkeypatch.setenv("HOSTNAME", "pod-name-ignored")

    consumer_name = adapter._resolve_consumer_name("log-workers")

    assert consumer_name == "worker_a-log-workers"


def test_build_message_from_entry_supports_string_keys():
    adapter = object.__new__(RedisStreamAdapter)

    parsed = adapter._build_message_from_entry(
        stream_name="traces.raw",
        message_id="1-0",
        data={
            "data": '{"signal_type":"traces"}',
            "data_type": "traces",
            "ingest_time": "2026-03-02T00:00:00Z",
            "header_tenant": "default",
        },
    )

    assert parsed is not None
    message_id_str, message_bytes, message = parsed
    assert message_id_str == "1-0"
    assert message_bytes == b'{"signal_type":"traces"}'
    assert message.subject == "traces.raw"
    assert message.headers["tenant"] == "default"
    assert message.headers["data_type"] == "traces"


@pytest.mark.skipif(LogWorker is None, reason="worker dependencies not available in test environment")
def test_worker_skips_unsupported_message_type():
    """Worker 对未知信号直接跳过并返回成功。"""
    worker = LogWorker()
    message = Message(
        subject="unknown.raw",
        data=b"{}",
        headers={"data_type": "unknown"},
    )

    result = asyncio.run(worker.process_message(message))

    assert result is True
    assert worker.processed_count == 0


class _FakeStorage:
    def __init__(self):
        self.metrics_saved = None
        self.traces_saved = None

    def save_metrics(self, points):
        self.metrics_saved = points
        return True

    def save_traces(self, spans):
        self.traces_saved = spans
        return True


@pytest.mark.skipif(LogWorker is None, reason="worker dependencies not available in test environment")
def test_worker_processes_metrics_message():
    worker = LogWorker()
    worker.storage = _FakeStorage()
    message = Message(
        subject="metrics.raw",
        data=b'{"signal_type":"metrics","payload":{"resourceMetrics":[{"resource":{"attributes":[{"key":"service.name","value":{"stringValue":"test-svc"}}]},"scopeMetrics":[{"metrics":[{"name":"cpu.usage","gauge":{"dataPoints":[{"timeUnixNano":1738892346000000000,"asDouble":0.5,"attributes":[]}]}}]}]}]}}',
        headers={"data_type": "metrics"},
    )

    result = asyncio.run(worker.process_message(message))
    assert result is True
    assert worker.storage.metrics_saved is not None
    assert len(worker.storage.metrics_saved) == 1
    assert worker.storage.metrics_saved[0]["service_name"] == "test-svc"


@pytest.mark.skipif(LogWorker is None, reason="worker dependencies not available in test environment")
def test_worker_processes_traces_message():
    worker = LogWorker()
    worker.storage = _FakeStorage()
    message = Message(
        subject="traces.raw",
        data=b'{"signal_type":"traces","payload":{"resourceSpans":[{"resource":{"attributes":[{"key":"service.name","value":{"stringValue":"test-svc"}}]},"scopeSpans":[{"spans":[{"traceId":"4bf92f3577b34da6a3ce929d0e0e4736","spanId":"00f067aa0ba902b7","parentSpanId":"","name":"GET /health","kind":"SERVER","startTimeUnixNano":1738892346000000000,"status":{"code":1},"attributes":[]}]}]}]}}',
        headers={"data_type": "traces"},
    )

    result = asyncio.run(worker.process_message(message))
    assert result is True
    assert worker.storage.traces_saved is not None
    assert len(worker.storage.traces_saved) == 1
    assert worker.storage.traces_saved[0]["status_code"] == "STATUS_CODE_OK"
