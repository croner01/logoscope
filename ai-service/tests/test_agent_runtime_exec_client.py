"""
Tests for ai.agent_runtime.exec_client.
"""

import asyncio

from ai.agent_runtime.exec_client import (
    ExecServiceClientError,
    _iter_sse_blocks,
    _parse_sse_event_block,
    cancel_command_run,
    create_command_run,
    execute_command,
    get_command_run,
    iter_command_run_stream,
    list_command_run_events,
    precheck_command,
)


class DummyResponse:
    def __init__(self, *, status_code=200, json_payload=None, text="", headers=None, chunks=None):
        self.status_code = status_code
        self._json_payload = json_payload if isinstance(json_payload, dict) else {}
        self.text = text
        self.headers = headers or {"content-type": "application/json"}
        self._chunks = list(chunks or [])
        self.closed = False

    def json(self):
        return self._json_payload

    def iter_content(self, chunk_size=1024):
        del chunk_size
        for chunk in self._chunks:
            yield chunk

    def close(self):
        self.closed = True


def test_parse_sse_event_block_handles_json_payload():
    parsed = _parse_sse_event_block('event: command_output_delta\ndata: {"stream":"stdout","text":"ok"}\n')
    assert parsed == {
        "event": "command_output_delta",
        "data": {"stream": "stdout", "text": "ok"},
    }


def test_iter_sse_blocks_reassembles_split_chunks():
    chunks = [
        "event: command_started\ndata: {\"seq\":1}",
        "\n\nevent: command_finished\ndata: {\"seq\":2}\n\n",
    ]
    blocks = list(_iter_sse_blocks(chunks))
    assert len(blocks) == 2
    assert 'command_started' in blocks[0]
    assert 'command_finished' in blocks[1]


def test_create_precheck_get_list_and_cancel_command_run(monkeypatch):
    calls = []

    def _fake_request(method, url, json=None, timeout=None):
        calls.append((method, url, json, timeout))
        if url.endswith("/api/v1/exec/runs") and method == "POST":
            return DummyResponse(json_payload={"run": {"run_id": "cmdrun-001", "status": "running"}})
        if url.endswith("/api/v1/exec/execute") and method == "POST":
            return DummyResponse(json_payload={"run_id": "cmdrun-001", "status": "executed", "stdout": "hello"})
        if url.endswith("/api/v1/exec/precheck") and method == "POST":
            return DummyResponse(json_payload={"status": "ok", "command_type": "query", "command": "echo hello"})
        if url.endswith("/api/v1/exec/runs/cmdrun-001") and method == "GET":
            return DummyResponse(json_payload={"run": {"run_id": "cmdrun-001", "status": "completed"}})
        if "/api/v1/exec/runs/cmdrun-001/events" in url and method == "GET":
            return DummyResponse(json_payload={"run_id": "cmdrun-001", "events": [{"seq": 1}]})
        if url.endswith("/api/v1/exec/runs/cmdrun-001/cancel") and method == "POST":
            return DummyResponse(json_payload={"run": {"run_id": "cmdrun-001", "status": "cancelled"}})
        raise AssertionError(f"unexpected request {method} {url}")

    monkeypatch.setattr("ai.agent_runtime.exec_client.requests.request", _fake_request)

    async def _run():
        created = await create_command_run(
            session_id="sess-001",
            message_id="msg-001",
            action_id="act-001",
            command="echo hello",
            purpose="验证 exec-client 命令链路",
        )
        prechecked = await precheck_command(
            session_id="sess-001",
            message_id="msg-001",
            action_id="act-001",
            command="echo hello",
            purpose="预检命令",
        )
        executed = await execute_command(
            session_id="sess-001",
            message_id="msg-001",
            action_id="act-001",
            command="echo hello",
            purpose="执行命令",
        )
        fetched = await get_command_run("cmdrun-001")
        events = await list_command_run_events("cmdrun-001", after_seq=0, limit=20)
        cancelled = await cancel_command_run("cmdrun-001")
        return created, prechecked, executed, fetched, events, cancelled

    created, prechecked, executed, fetched, events, cancelled = asyncio.run(_run())

    assert created["run"]["run_id"] == "cmdrun-001"
    assert prechecked["status"] == "ok"
    assert prechecked["command_type"] == "query"
    assert executed["status"] == "executed"
    assert executed["stdout"] == "hello"
    assert fetched["run"]["status"] == "completed"
    assert events["events"][0]["seq"] == 1
    assert cancelled["run"]["status"] == "cancelled"
    assert len(calls) == 6


def test_iter_command_run_stream_parses_events(monkeypatch):
    response = DummyResponse(
        headers={"content-type": "text/event-stream"},
        chunks=[
            b'event: command_started\ndata: {"seq":1}\n\n',
            b'event: command_output_delta\ndata: {"seq":2,"stream":"stdout","text":"hello"}\n\n',
        ],
    )

    monkeypatch.setattr("ai.agent_runtime.exec_client.requests.get", lambda *args, **kwargs: response)

    items = list(iter_command_run_stream("cmdrun-001", after_seq=0))

    assert [item["event"] for item in items] == ["command_started", "command_output_delta"]
    assert items[1]["data"]["text"] == "hello"
    assert response.closed is True


def test_iter_command_run_stream_raises_on_http_error(monkeypatch):
    response = DummyResponse(status_code=404, json_payload={"detail": "run not found"})
    monkeypatch.setattr("ai.agent_runtime.exec_client.requests.get", lambda *args, **kwargs: response)

    try:
        list(iter_command_run_stream("missing-run"))
    except ExecServiceClientError as exc:
        assert "run not found" in str(exc)
    else:
        raise AssertionError("expected ExecServiceClientError")
