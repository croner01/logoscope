"""Tests for extracted query parameter helpers."""

from datetime import timedelta

from api import query_params


def test_sanitize_interval_normalizes_and_blocks_injection():
    assert query_params.sanitize_interval("15 minutes", default_value="7 DAY") == "15 MINUTE"
    assert query_params.sanitize_interval("1 HOUR; DROP TABLE logs.logs --", default_value="7 DAY") == "7 DAY"


def test_normalize_optional_str_list_dedups_and_splits():
    result = query_params.normalize_optional_str_list([" INFO, WARN ", "ERROR", "WARN"])
    assert result == ["INFO", "WARN", "ERROR"]


def test_build_time_filter_clause_prefers_absolute_window():
    clause, params = query_params.build_time_filter_clause(
        column_name="timestamp",
        time_window="1 HOUR",
        start_time="2026-03-01T00:00:00Z",
        end_time="2026-03-01T01:00:00Z",
        param_prefix="logs",
    )
    assert "parseDateTimeBestEffort" in clause
    assert params["logs_start"] == "2026-03-01T00:00:00Z"
    assert params["logs_end"] == "2026-03-01T01:00:00Z"


def test_interval_to_timedelta_supports_units():
    assert query_params.interval_to_timedelta("5 MINUTE") == timedelta(minutes=5)
    assert query_params.interval_to_timedelta("2 HOUR") == timedelta(hours=2)
    assert query_params.interval_to_timedelta("1 WEEK") == timedelta(weeks=1)
