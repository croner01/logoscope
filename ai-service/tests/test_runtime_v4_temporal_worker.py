"""
Tests for runtime v4 Temporal worker lifecycle.
"""

import asyncio

import pytest

from ai.runtime_v4.temporal.worker import get_temporal_worker_runtime, start_temporal_worker, stop_temporal_worker


def test_temporal_worker_skips_when_outer_engine_is_local(monkeypatch):
    monkeypatch.setenv("AI_RUNTIME_V4_OUTER_ENGINE", "temporal_local")
    result = asyncio.run(start_temporal_worker())
    runtime = get_temporal_worker_runtime()

    assert result is None
    assert runtime.running is False


def test_temporal_worker_required_fails_when_sdk_unavailable(monkeypatch):
    monkeypatch.setenv("AI_RUNTIME_V4_OUTER_ENGINE", "temporal_required")
    monkeypatch.setenv("AI_RUNTIME_V4_TEMPORAL_ADDRESS", "temporal-frontend:7233")
    monkeypatch.setattr("ai.runtime_v4.temporal.worker.temporal_worker_available", lambda: False)

    with pytest.raises(RuntimeError) as exc_info:
        asyncio.run(start_temporal_worker())
    assert "temporalio SDK is unavailable" in str(exc_info.value)


def test_temporal_worker_required_fails_when_worker_disabled(monkeypatch):
    monkeypatch.setenv("AI_RUNTIME_V4_OUTER_ENGINE", "temporal_required")
    monkeypatch.setenv("AI_RUNTIME_V4_TEMPORAL_ADDRESS", "temporal-frontend:7233")
    monkeypatch.setenv("AI_RUNTIME_V4_TEMPORAL_WORKER_ENABLED", "false")

    with pytest.raises(RuntimeError) as exc_info:
        asyncio.run(start_temporal_worker())
    assert "WORKER_ENABLED=false" in str(exc_info.value)


def test_temporal_worker_stop_is_idempotent():
    asyncio.run(stop_temporal_worker())
    runtime = get_temporal_worker_runtime()
    assert runtime.running is False
