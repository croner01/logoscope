"""Tests for extracted trace-lite inference helpers."""

from datetime import datetime, timezone

from api import trace_lite_inference


def test_parse_json_dict_handles_invalid_payloads():
    assert trace_lite_inference.parse_json_dict({"k": "v"}) == {"k": "v"}
    assert trace_lite_inference.parse_json_dict("") == {}
    assert trace_lite_inference.parse_json_dict('{"a":1}') == {"a": 1}
    assert trace_lite_inference.parse_json_dict('["x"]') == {}
    assert trace_lite_inference.parse_json_dict("{bad-json}") == {}


def test_extract_request_id_from_attrs_and_message():
    assert (
        trace_lite_inference.extract_request_id(
            {"x-request-id": "req-12345"},
            message="",
        )
        == "req-12345"
    )
    assert (
        trace_lite_inference.extract_request_id(
            {},
            message="request_id=req-67890 processing",
        )
        == "req-67890"
    )
    assert (
        trace_lite_inference.extract_request_id(
            {},
            message="no id in this message",
        )
        == ""
    )


def test_to_datetime_parses_iso_and_falls_back():
    parsed = trace_lite_inference.to_datetime("2026-03-01T01:02:03Z")
    assert isinstance(parsed, datetime)
    assert parsed.year == 2026
    assert parsed.month == 3
    assert parsed.day == 1
    assert parsed.tzinfo == timezone.utc

    fallback = trace_lite_inference.to_datetime("not-a-datetime")
    assert isinstance(fallback, datetime)
    assert fallback.tzinfo == timezone.utc


def test_to_datetime_normalizes_naive_and_aware_values_to_utc():
    naive = trace_lite_inference.to_datetime("2026-03-01 01:02:03")
    aware = trace_lite_inference.to_datetime("2026-03-01T09:02:03+08:00")

    assert naive.tzinfo == timezone.utc
    assert aware.tzinfo == timezone.utc
    assert aware.hour == 1


def test_infer_trace_lite_fragments_request_id_and_time_window():
    rows = [
        {
            "id": "1",
            "timestamp": "2026-03-01T00:00:00Z",
            "service_name": "gateway",
            "namespace": "prod",
            "message": "request_id=req-1 start",
            "trace_id": "trace-1",
            "attributes_json": '{"request_id":"req-1"}',
        },
        {
            "id": "2",
            "timestamp": "2026-03-01T00:00:01Z",
            "service_name": "order",
            "namespace": "prod",
            "message": "request_id=req-1 process",
            "trace_id": "trace-1",
            "attributes_json": '{"request_id":"req-1"}',
        },
        {
            "id": "3",
            "timestamp": "2026-03-01T00:00:10Z",
            "service_name": "billing",
            "namespace": "prod",
            "message": "start billing",
            "trace_id": "trace-2",
            "attributes_json": "{}",
        },
        {
            "id": "4",
            "timestamp": "2026-03-01T00:00:10.500Z",
            "service_name": "ledger",
            "namespace": "prod",
            "message": "ledger done",
            "trace_id": "trace-2",
            "attributes_json": "{}",
        },
    ]

    fragments, stats = trace_lite_inference.infer_trace_lite_fragments_from_logs(
        rows,
        fallback_window_sec=1.0,
    )
    edge_ids = {item["fragment_id"] for item in fragments}

    assert "gateway->order" in edge_ids
    assert "billing->ledger" in edge_ids
    assert stats["request_id_edges"] >= 1
    assert stats["time_window_edges"] >= 1


def test_infer_trace_lite_fragments_handles_mixed_timestamp_timezones():
    rows = [
        {
            "id": "1",
            "timestamp": "2026-03-01T00:00:00Z",
            "service_name": "gateway",
            "namespace": "prod",
            "message": "request_id=req-1 start",
            "trace_id": "trace-1",
            "attributes_json": '{"request_id":"req-1"}',
        },
        {
            "id": "2",
            "timestamp": "2026-03-01 00:00:01",
            "service_name": "order",
            "namespace": "prod",
            "message": "request_id=req-1 process",
            "trace_id": "trace-1",
            "attributes_json": '{"request_id":"req-1"}',
        },
    ]

    fragments, stats = trace_lite_inference.infer_trace_lite_fragments_from_logs(rows)
    assert any(item["fragment_id"] == "gateway->order" for item in fragments)
    assert stats["request_id_edges"] >= 1
