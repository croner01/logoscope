"""
Tests for ClickHouse DateTime64 normalization helpers.
"""

import sys
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from core.policy_decision_store import _to_clickhouse_datetime as decision_dt
from core.runtime_history_store import _to_clickhouse_datetime as history_dt


def test_clickhouse_datetime_normalizes_iso_with_offset():
    expected = "2026-03-24 07:00:00.000"
    source = "2026-03-24T07:00:00+00:00"

    assert decision_dt(source) == expected
    assert history_dt(source) == expected


def test_clickhouse_datetime_normalizes_iso_with_z_suffix():
    expected = "2026-03-24 07:00:00.000"
    source = "2026-03-24T07:00:00Z"

    assert decision_dt(source) == expected
    assert history_dt(source) == expected


def test_clickhouse_datetime_pads_fractional_seconds():
    expected = "2026-03-24 07:00:00.900"
    source = "2026-03-24 07:00:00.9"

    assert decision_dt(source) == expected
    assert history_dt(source) == expected
