"""
msgqueue worker timestamp calibration tests.
"""
import re

import pytest

try:
    from msgqueue.worker import LogWorker
    from msgqueue.worker import _normalize_timestamp_to_utc
    from msgqueue.worker import _select_timestamp_input
except Exception:
    LogWorker = None
    _normalize_timestamp_to_utc = None
    _select_timestamp_input = None


@pytest.mark.skipif(_normalize_timestamp_to_utc is None, reason="worker dependencies not available in test environment")
def test_normalize_timestamp_keeps_explicit_utc():
    result = _normalize_timestamp_to_utc("2026-03-07T13:53:24.299Z", source_tz_hint="Asia/Shanghai")

    assert result["timestamp_utc"] == "2026-03-07T13:53:24.299000Z"
    assert result["timestamp_parse_strategy"] == "explicit_tz"
    assert result["timestamp_calibrated"] is False


@pytest.mark.skipif(_normalize_timestamp_to_utc is None, reason="worker dependencies not available in test environment")
def test_normalize_timestamp_converts_explicit_offset():
    result = _normalize_timestamp_to_utc("2026-03-07T21:53:24.299+08:00", source_tz_hint="UTC")

    assert result["timestamp_utc"] == "2026-03-07T13:53:24.299000Z"
    assert result["timestamp_parse_strategy"] == "explicit_tz"
    assert result["timestamp_source_tz"] == "+08:00"
    assert result["timestamp_calibrated"] is True


@pytest.mark.skipif(_normalize_timestamp_to_utc is None, reason="worker dependencies not available in test environment")
def test_normalize_timestamp_supports_high_precision_offset_text():
    result = _normalize_timestamp_to_utc("2026-03-07T21:53:24.299123789+08:00", source_tz_hint="UTC")

    assert result["timestamp_utc"] == "2026-03-07T13:53:24.299123Z"
    assert result["timestamp_parse_strategy"] == "explicit_tz"
    assert result["timestamp_source_tz"] == "+08:00"


@pytest.mark.skipif(_normalize_timestamp_to_utc is None, reason="worker dependencies not available in test environment")
def test_normalize_timestamp_assumes_source_timezone_for_naive_text():
    result = _normalize_timestamp_to_utc("2026-03-07 21:53:24.299", source_tz_hint="Asia/Shanghai")

    assert result["timestamp_utc"] == "2026-03-07T13:53:24.299000Z"
    assert result["timestamp_parse_strategy"] == "assumed_tz"
    assert result["timestamp_source_tz"] == "Asia/Shanghai"


@pytest.mark.skipif(_normalize_timestamp_to_utc is None, reason="worker dependencies not available in test environment")
def test_normalize_timestamp_supports_epoch_nanoseconds():
    result = _normalize_timestamp_to_utc("1771134804299000000", source_tz_hint="Asia/Shanghai")

    assert result["timestamp_utc"].endswith("Z")
    assert result["timestamp_parse_strategy"] == "epoch_nanoseconds"
    assert result["timestamp_source_tz"] == "UTC"
    assert result["timestamp_calibrated"] is False


@pytest.mark.skipif(_normalize_timestamp_to_utc is None, reason="worker dependencies not available in test environment")
def test_normalize_timestamp_supports_epoch_zero():
    result = _normalize_timestamp_to_utc("0", source_tz_hint="Asia/Shanghai")

    assert result["timestamp_utc"] == "1970-01-01T00:00:00.000000Z"
    assert result["timestamp_parse_strategy"] == "epoch_seconds"
    assert result["timestamp_source_tz"] == "UTC"
    assert result["timestamp_calibrated"] is False


@pytest.mark.skipif(_normalize_timestamp_to_utc is None, reason="worker dependencies not available in test environment")
def test_normalize_timestamp_fallbacks_to_observed_for_invalid_text():
    result = _normalize_timestamp_to_utc("invalid-timestamp", source_tz_hint="Asia/Shanghai")

    assert result["timestamp_parse_strategy"] == "fallback_observed"
    assert result["timestamp_calibrated"] is True
    assert re.match(r"^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}", result["timestamp_utc"])
    assert result["timestamp_utc"].endswith("Z")


@pytest.mark.skipif(_select_timestamp_input is None, reason="worker dependencies not available in test environment")
def test_select_timestamp_prefers_container_explicit_time():
    selected = _select_timestamp_input(
        log_data={"timestamp": "2026-03-09T16:47:50.578950Z"},
        raw_attributes={
            "time": "2026-03-09T16:47:50.578950+08:00",
            "stream": "stdout",
            "logtag": "F",
        },
        log_meta={},
    )

    assert selected["source"] == "raw_attributes.time"
    assert selected["selection_strategy"] == "container_explicit_tz"


@pytest.mark.skipif(_select_timestamp_input is None, reason="worker dependencies not available in test environment")
def test_select_timestamp_prefers_epoch_candidate():
    selected = _select_timestamp_input(
        log_data={"timestamp": "1771134804299000000"},
        raw_attributes={},
        log_meta={},
    )

    assert selected["source"] == "event.timestamp"
    assert selected["selection_strategy"] == "epoch_candidate"


@pytest.mark.skipif(LogWorker is None, reason="worker dependencies not available in test environment")
def test_process_log_records_batch_skips_invalid_records_instead_of_failing_whole_batch():
    worker = LogWorker.__new__(LogWorker)
    worker.error_count = 0
    worker.processed_count = 0
    worker.log_writer = object()

    saved_events = []

    def _fake_normalize(record):
        if not isinstance(record, dict):
            return None
        if record.get("ok"):
            return {"id": record["id"]}
        return None

    def _fake_save_events_batch(events):
        saved_events.extend(events)
        return True

    worker._normalize_log_payload = _fake_normalize
    worker._save_events_batch = _fake_save_events_batch

    success = worker._process_log_records_batch([
        {"id": "good-1", "ok": True},
        {"id": "bad-1", "ok": False},
        "bad-type",
    ])

    assert success is True
    assert [event["id"] for event in saved_events] == ["good-1"]
    assert worker.error_count == 2
    assert worker.processed_count == 1


@pytest.mark.skipif(LogWorker is None, reason="worker dependencies not available in test environment")
def test_save_events_batch_skips_unpreparable_rows_without_failing_valid_rows():
    worker = LogWorker.__new__(LogWorker)
    worker.error_count = 0
    worker.enable_semantic_event_write = False
    worker.storage = None

    class _DummyWriter:
        def __init__(self):
            self.rows = []

        def add_batch(self, rows):
            self.rows.extend(rows)

        def get_stats(self):
            return {"buffer_size": len(self.rows), "total_rows": len(self.rows)}

    writer = _DummyWriter()
    worker.log_writer = writer

    def _fake_prepare(event):
        if event.get("bad"):
            return None
        return ([event["id"]], {}, "host-1", "10.0.0.1")

    worker._prepare_event_row = _fake_prepare

    success = worker._save_events_batch([
        {"id": "good-1"},
        {"id": "bad-1", "bad": True},
        {"id": "good-2"},
    ])

    assert success is True
    assert writer.rows == [["good-1"], ["good-2"]]
    assert worker.error_count == 1
