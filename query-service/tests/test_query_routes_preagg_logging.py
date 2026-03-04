"""
preagg 运行态日志节流测试。
"""
import os
import sys

import pytest

_QUERY_SERVICE_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if _QUERY_SERVICE_DIR not in sys.path:
    sys.path.insert(0, _QUERY_SERVICE_DIR)

from api import query_routes


@pytest.fixture(autouse=True)
def _restore_preagg_globals():
    original_storage = query_routes._STORAGE_ADAPTER
    original_status = dict(query_routes._PREAGG_RUNTIME_STATUS)
    original_last_state = query_routes._PREAGG_LAST_LOGGED_STATE
    original_expected_fn = query_routes._get_expected_preagg_tables
    original_loader = query_routes.obs_query_utils._load_preagg_tables
    try:
        yield
    finally:
        query_routes._STORAGE_ADAPTER = original_storage
        query_routes._PREAGG_RUNTIME_STATUS = original_status
        query_routes._PREAGG_LAST_LOGGED_STATE = original_last_state
        query_routes._get_expected_preagg_tables = original_expected_fn
        query_routes.obs_query_utils._load_preagg_tables = original_loader


def test_refresh_preagg_status_logs_missing_once_until_state_changes(monkeypatch: pytest.MonkeyPatch):
    warning_calls = []
    info_calls = []

    monkeypatch.setattr(query_routes, "_get_expected_preagg_tables", lambda: ("obs_counts_1m", "obs_traces_1m"))
    monkeypatch.setattr(query_routes.obs_query_utils, "_load_preagg_tables", lambda _adapter: [])
    monkeypatch.setattr(query_routes.logger, "warning", lambda message, *args: warning_calls.append((message, args)))
    monkeypatch.setattr(query_routes.logger, "info", lambda message, *args: info_calls.append((message, args)))

    query_routes._STORAGE_ADAPTER = object()
    query_routes._PREAGG_LAST_LOGGED_STATE = None

    first = query_routes.refresh_preagg_runtime_status(force_reload=False)
    second = query_routes.refresh_preagg_runtime_status(force_reload=False)

    assert first["ready"] is False
    assert second["ready"] is False
    assert len(warning_calls) == 1
    assert len(info_calls) == 0


def test_refresh_preagg_status_logs_again_when_state_changes(monkeypatch: pytest.MonkeyPatch):
    warning_calls = []
    info_calls = []
    available_tables = []

    monkeypatch.setattr(query_routes, "_get_expected_preagg_tables", lambda: ("obs_counts_1m", "obs_traces_1m"))
    monkeypatch.setattr(
        query_routes.obs_query_utils,
        "_load_preagg_tables",
        lambda _adapter: list(available_tables),
    )
    monkeypatch.setattr(query_routes.logger, "warning", lambda message, *args: warning_calls.append((message, args)))
    monkeypatch.setattr(query_routes.logger, "info", lambda message, *args: info_calls.append((message, args)))

    query_routes._STORAGE_ADAPTER = object()
    query_routes._PREAGG_LAST_LOGGED_STATE = None

    available_tables[:] = []
    missing_snapshot = query_routes.refresh_preagg_runtime_status(force_reload=False)
    available_tables[:] = ["obs_counts_1m", "obs_traces_1m"]
    ready_snapshot = query_routes.refresh_preagg_runtime_status(force_reload=False)

    assert missing_snapshot["ready"] is False
    assert ready_snapshot["ready"] is True
    assert len(warning_calls) == 1
    assert len(info_calls) == 1
