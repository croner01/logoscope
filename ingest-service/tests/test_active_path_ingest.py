"""
Ingest active path tests.

聚焦当前生效链路：
ingest-service -> Redis Stream -> semantic-engine-worker
"""
import json
import os
import sys

import pytest

# 添加项目根目录到 Python 路径
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from api import ingest as ingest_api
from services import queue_writer


def test_get_stream_name_by_data_type(monkeypatch):
    """信号类型应路由到各自 stream。"""
    monkeypatch.setattr(ingest_api.config, "redis_stream_logs", "logs.test")
    monkeypatch.setattr(ingest_api.config, "redis_stream_metrics", "metrics.test")
    monkeypatch.setattr(ingest_api.config, "redis_stream_traces", "traces.test")
    monkeypatch.setattr(ingest_api.config, "redis_stream", "default.test")

    assert ingest_api._get_stream_name("logs") == "logs.test"
    assert ingest_api._get_stream_name("metrics") == "metrics.test"
    assert ingest_api._get_stream_name("traces") == "traces.test"
    assert ingest_api._get_stream_name("unknown") == "default.test"


def test_build_non_log_message_preserves_signal_type_for_json_payload():
    payload_dict = {"resourceSpans": [{"scopeSpans": []}]}
    message = queue_writer._build_non_log_queue_message("traces", payload_dict, '{"x":1}')
    assert message["signal_type"] == "traces"
    assert "payload" in message
    assert message["payload"]["resourceSpans"][0]["scopeSpans"] == []


def test_build_non_log_message_falls_back_to_raw_payload():
    message = queue_writer._build_non_log_queue_message("metrics", None, "not-json")
    assert message["signal_type"] == "metrics"
    assert message["raw_payload"] == "not-json"


def test_build_log_messages_split_otlp_records():
    """OTLP logs 应拆分为多条队列消息，而不是聚合成一条。"""
    payload_dict = {
        "resourceLogs": [
            {
                "resource": {
                    "attributes": [
                        {"key": "service.name", "value": {"stringValue": "checkout-service"}},
                        {"key": "k8s.pod.name", "value": {"stringValue": "checkout-7ff8fff99f-hznvp"}},
                    ]
                },
                "scopeLogs": [
                    {
                        "logRecords": [
                            {"timeUnixNano": "1", "severityText": "INFO", "body": {"stringValue": "first"}},
                            {"timeUnixNano": "2", "severityText": "ERROR", "body": {"stringValue": "second"}},
                        ]
                    }
                ],
            }
        ]
    }

    messages = queue_writer._build_log_queue_messages(payload_dict, json.dumps(payload_dict), metadata={})
    assert len(messages) == 2
    assert messages[0]["log"] == "first"
    assert messages[1]["log"] == "second"
    assert isinstance(messages[0].get("service.name"), str)


@pytest.mark.asyncio
async def test_write_to_queue_traces_keeps_signal_shape(monkeypatch):
    """
    traces 信号写队列时应保留 signal_type/payload 结构，
    避免被误转成日志消息。
    """
    captured = {}

    async def fake_ensure_connection() -> bool:
        return False

    async def fake_write_to_memory(stream: str, data_type: str, payload: str, metadata):
        captured["stream"] = stream
        captured["data_type"] = data_type
        captured["payload"] = payload
        return {"status": "success", "mode": "memory_queue"}

    monkeypatch.setattr(queue_writer, "_ensure_redis_connection", fake_ensure_connection)
    monkeypatch.setattr(queue_writer, "_write_to_memory_queue", fake_write_to_memory)

    payload = json.dumps(
        {
            "resourceSpans": [
                {
                    "resource": {"attributes": [{"key": "service.name", "value": {"stringValue": "api"}}]},
                    "scopeSpans": [{"spans": [{"traceId": "t1", "spanId": "s1"}]}],
                }
            ]
        }
    )

    result = await queue_writer.write_to_queue("traces.raw", "traces", payload, {})
    assert result["status"] == "success"
    assert captured["stream"] == "traces.raw"
    assert captured["data_type"] == "traces"

    message = json.loads(captured["payload"])
    assert message["signal_type"] == "traces"
    assert "payload" in message
    assert "resourceSpans" in message["payload"]


def test_extract_attributes_supports_scalar_types():
    attrs_list = [
        {"key": "string", "value": {"stringValue": "v"}},
        {"key": "int", "value": {"intValue": 1}},
        {"key": "double", "value": {"doubleValue": 1.5}},
        {"key": "bool", "value": {"boolValue": True}},
    ]
    result = queue_writer.extract_attributes(attrs_list)
    assert result == {"string": "v", "int": 1, "double": 1.5, "bool": True}
