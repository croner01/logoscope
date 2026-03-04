"""
TraceProcessor 单元测试
"""
import asyncio
import json
import os
import sys

# 添加项目根目录到 Python 路径
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from services.trace_processor import TraceProcessor


class FakeRedisClient:
    """用于 TraceProcessor 失败重试测试的 Redis 桩。"""

    def __init__(self):
        self.counters = {}
        self.expire_calls = []
        self.xadd_calls = []
        self.xack_calls = []
        self.delete_calls = []

    async def incr(self, key: str) -> int:
        current = self.counters.get(key, 0) + 1
        self.counters[key] = current
        return current

    async def expire(self, key: str, ttl: int) -> None:
        self.expire_calls.append((key, ttl))

    async def xadd(self, stream: str, payload, maxlen=None, approximate=True):
        self.xadd_calls.append((stream, payload, maxlen, approximate))
        return "1-0"

    async def xack(self, stream: str, group: str, message_id: str):
        self.xack_calls.append((stream, group, message_id))
        return 1

    async def delete(self, key: str):
        self.delete_calls.append(key)
        return 1


class FakeRedisStreamIntegrationClient:
    """用于 pending 回收闭环测试的 Redis Stream 桩。"""

    def __init__(self):
        self.counters = {}
        self.expire_calls = []
        self.delete_calls = []
        self.xack_calls = []
        self.dlq_entries = []
        self.pending_messages = {
            "9-1": {
                "data_type": "traces",
                "data": '{"broken":true}',
                "ingest_time": "2026-02-26T00:00:00Z",
            }
        }

    async def incr(self, key: str) -> int:
        current = self.counters.get(key, 0) + 1
        self.counters[key] = current
        return current

    async def expire(self, key: str, ttl: int) -> None:
        self.expire_calls.append((key, ttl))

    async def delete(self, key: str) -> int:
        self.delete_calls.append(key)
        return 1

    async def xadd(self, stream: str, payload, maxlen=None, approximate=True):
        self.dlq_entries.append((stream, payload, maxlen, approximate))
        return "100-0"

    async def xack(self, stream: str, group: str, message_id: str):
        self.xack_calls.append((stream, group, message_id))
        self.pending_messages.pop(message_id, None)
        return 1

    async def xautoclaim(
        self,
        name: str,
        groupname: str,
        consumername: str,
        min_idle_time: int,
        start_id: str,
        count: int,
    ):
        if not self.pending_messages:
            return ("0-0", [])

        entries = list(self.pending_messages.items())[:count]
        return ("0-0", entries)


def test_unwrap_trace_payload_direct_otlp():
    """直接 OTLP payload 应原样返回。"""
    processor = TraceProcessor()
    payload = {"resourceSpans": [{"scopeSpans": [{"spans": []}]}]}

    unwrapped = processor._unwrap_trace_payload(payload)
    assert unwrapped == payload


def test_unwrap_trace_payload_wrapped_payload():
    """队列包装格式中的 payload 应被正确提取。"""
    processor = TraceProcessor()
    payload = {
        "signal_type": "traces",
        "payload": {"resourceSpans": [{"scopeSpans": [{"spans": []}]}]},
    }

    unwrapped = processor._unwrap_trace_payload(payload)
    assert "resourceSpans" in unwrapped


def test_unwrap_trace_payload_wrapped_raw_payload():
    """当 payload 缺失时，支持从 raw_payload(JSON) 提取。"""
    processor = TraceProcessor()
    raw_payload = {"resourceSpans": [{"scopeSpans": [{"spans": []}]}]}
    payload = {
        "signal_type": "traces",
        "raw_payload": json.dumps(raw_payload),
    }

    unwrapped = processor._unwrap_trace_payload(payload)
    assert unwrapped == raw_payload


def test_process_traces_with_wrapped_queue_message():
    """process_traces 应支持队列包装格式。"""
    processor = TraceProcessor()
    captured = {"spans": None}

    async def fake_save_spans(spans):
        captured["spans"] = spans
        return True

    processor._save_spans_to_clickhouse = fake_save_spans

    wrapped = {
        "signal_type": "traces",
        "payload": {
            "resourceSpans": [{
                "resource": {
                    "attributes": [
                        {"key": "service.name", "value": {"stringValue": "checkout-service"}}
                    ]
                },
                "scopeSpans": [{
                    "spans": [{
                        "traceId": "trace-1",
                        "spanId": "span-1",
                        "parentSpanId": "",
                        "name": "GET /checkout",
                        "kind": 2,
                        "startTimeUnixNano": "1739597400000000000",
                        "endTimeUnixNano": "1739597400123000000",
                        "status": {"code": 1},
                        "attributes": [
                            {"key": "http.method", "value": {"stringValue": "GET"}}
                        ],
                    }]
                }]
            }]
        }
    }

    success = asyncio.run(processor.process_traces(json.dumps(wrapped), metadata={}))

    assert success is True
    assert processor.processed_count == 1
    assert captured["spans"] is not None
    assert captured["spans"][0]["trace_id"] == "trace-1"
    assert captured["spans"][0]["status"] == "STATUS_CODE_OK"


def test_handle_failed_message_keeps_pending_before_max_retry():
    """失败次数未超过阈值时，消息应保持 pending。"""
    processor = TraceProcessor()
    fake_redis = FakeRedisClient()
    processor.redis_client = fake_redis
    processor.max_retries = 3
    processor.retry_ttl_sec = 600

    asyncio.run(
        processor._handle_failed_message(
            stream="traces.raw",
            consumer_group="trace-processors",
            message_id="1-1",
            fields={"data_type": "traces", "data": "{}", "ingest_time": "now"}
        )
    )

    assert len(fake_redis.expire_calls) == 1
    assert len(fake_redis.xack_calls) == 0
    assert len(fake_redis.xadd_calls) == 0


def test_handle_failed_message_moves_to_dlq_after_max_retry():
    """超过最大重试后，消息应写入 DLQ 并 ACK。"""
    processor = TraceProcessor()
    fake_redis = FakeRedisClient()
    processor.redis_client = fake_redis
    processor.max_retries = 2
    processor.retry_ttl_sec = 600
    processor.dlq_stream = "traces.dlq"

    # 连续触发 3 次失败，第三次应进入 DLQ 并 ACK
    for _ in range(3):
        asyncio.run(
            processor._handle_failed_message(
                stream="traces.raw",
                consumer_group="trace-processors",
                message_id="2-1",
                fields={"data_type": "traces", "data": '{"x":1}', "ingest_time": "now"}
            )
        )

    assert len(fake_redis.xadd_calls) == 1
    assert len(fake_redis.xack_calls) == 1
    assert len(fake_redis.delete_calls) >= 1


def test_pending_recovery_failure_to_dlq_clears_pending_integration():
    """集成闭环：pending 失败消息多轮回收后应进入 DLQ 且从 pending 清除。"""
    processor = TraceProcessor()
    fake_redis = FakeRedisStreamIntegrationClient()
    processor.redis_client = fake_redis
    processor.max_retries = 2
    processor.retry_ttl_sec = 300
    processor.pending_batch_size = 10
    processor.dlq_stream = "traces.dlq"

    async def always_fail(_traces_data, _metadata):
        return False

    processor.process_traces = always_fail

    for _ in range(3):
        asyncio.run(
            processor._recover_pending_messages(
                stream="traces.raw",
                consumer_group="trace-processors",
                consumer_name="recovery-test-consumer",
            )
        )

    assert "9-1" not in fake_redis.pending_messages
    assert len(fake_redis.dlq_entries) == 1
    assert len(fake_redis.xack_calls) == 1

    dlq_stream, dlq_payload, _maxlen, _approximate = fake_redis.dlq_entries[0]
    assert dlq_stream == "traces.dlq"
    assert dlq_payload["original_stream"] == "traces.raw"
    assert dlq_payload["message_id"] == "9-1"
    assert dlq_payload["retry_count"] == "3"

    retry_key = processor._build_retry_key("traces.raw", "9-1")
    assert retry_key in fake_redis.delete_calls
