"""
Tests for runtime v4 Temporal outer client.
"""

import asyncio

from ai.runtime_v4.temporal.client import TemporalOuterClient


class _FakeHandle:
    async def query(self, _query_name):
        return {
            "run_id": "run-temporal-001",
            "status": "running",
            "created_at": "2026-03-24T00:00:00Z",
            "updated_at": "2026-03-24T00:00:00Z",
        }


class _FakeRemoteClient:
    async def start_workflow(self, *_args, **_kwargs):
        return _FakeHandle()


def test_start_run_remote_returns_minimal_snapshot_when_run_fetch_is_eventually_consistent(monkeypatch):
    monkeypatch.setattr("ai.runtime_v4.temporal.client.temporal_workflow_available", lambda: True)

    client = TemporalOuterClient()

    async def _missing_run(_run_id):
        return {}

    monkeypatch.setattr(client, "_fetch_run_payload", _missing_run)

    async def _run():
        return await client._start_run_remote(
            remote_client=_FakeRemoteClient(),
            thread_id="thr-001",
            session_id="sess-001",
            question="排查 query-service",
            analysis_context={"analysis_type": "log", "service_name": "query-service"},
            runtime_options={},
        )

    result = asyncio.run(_run())

    assert result["workflow_id"].startswith("wf-")
    assert result["outer_engine"] == "temporal-v1"
    assert result["run"]["run_id"] == "run-temporal-001"
    assert result["run"]["status"] == "running"
    assert result["run"]["context_json"]["thread_id"] == "thr-001"


def test_signal_user_input_falls_back_when_workflow_mapping_missing(monkeypatch):
    client = TemporalOuterClient()

    async def _raise_missing_workflow(*, run_id, signal_name, payload):
        raise RuntimeError("temporal_required but workflow_id for run is missing")

    async def _fallback_submit(payload):
        return {
            "run": {"run_id": payload.get("run_id"), "status": "running"},
            "user_input": {"text": payload.get("text"), "source": payload.get("source")},
        }

    monkeypatch.setattr(client, "_signal_remote", _raise_missing_workflow)
    monkeypatch.setattr(
        "ai.runtime_v4.temporal.client.activities.submit_user_input_activity",
        _fallback_submit,
    )

    result = asyncio.run(
        client.signal_user_input(
            run_id="run-legacy-001",
            text="继续排查",
            source="user",
        )
    )

    assert (result.get("run") or {}).get("run_id") == "run-legacy-001"
    assert (result.get("user_input") or {}).get("text") == "继续排查"
