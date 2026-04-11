"""
Tests for structured follow-up executor (argv dispatcher).
"""

from ai.followup_command import execute_action_spec


def _build_args() -> dict:
    return {
        "namespace": "islap",
        "pod_selector": "app=clickhouse",
        "query": "SELECT 1",
        "timeout_s": 30,
    }


def test_execute_action_spec_uses_argv_dispatch(monkeypatch):
    calls = []

    def _fake_execute_followup_argv(argv, timeout_seconds):
        calls.append((list(argv), int(timeout_seconds)))
        if "get" in argv and "pods" in argv:
            return {
                "status": "executed",
                "exit_code": 0,
                "duration_ms": 10,
                "stdout": "clickhouse-0",
                "stderr": "",
                "output_truncated": False,
                "timed_out": False,
            }
        return {
            "status": "executed",
            "exit_code": 0,
            "duration_ms": 20,
            "stdout": "ok",
            "stderr": "",
            "output_truncated": False,
            "timed_out": False,
        }

    monkeypatch.setattr("ai.followup_command._execute_followup_argv", _fake_execute_followup_argv)

    result = execute_action_spec("kubectl_clickhouse_query", _build_args(), {})

    assert result["status"] == "executed"
    assert result["attempt"] == 1
    assert result["max_attempts"] == 2
    assert len(calls) == 2
    assert calls[0][0][0:6] == ["kubectl", "-n", "islap", "get", "pods", "-l"]
    assert "clickhouse-0" in calls[1][0]
    assert all(timeout == 60 for _, timeout in calls)


def test_execute_action_spec_retries_once_on_timeout(monkeypatch):
    calls = []
    query_attempt = {"count": 0}

    def _fake_execute_followup_argv(argv, timeout_seconds):
        calls.append((list(argv), int(timeout_seconds)))
        if "get" in argv and "pods" in argv:
            return {
                "status": "executed",
                "exit_code": 0,
                "duration_ms": 8,
                "stdout": "clickhouse-0",
                "stderr": "",
                "output_truncated": False,
                "timed_out": False,
            }
        query_attempt["count"] += 1
        if query_attempt["count"] == 1:
            return {
                "status": "failed",
                "exit_code": -9,
                "duration_ms": 60000,
                "stdout": "",
                "stderr": "timeout",
                "output_truncated": False,
                "timed_out": True,
            }
        return {
            "status": "executed",
            "exit_code": 0,
            "duration_ms": 1200,
            "stdout": "ok",
            "stderr": "",
            "output_truncated": False,
            "timed_out": False,
        }

    monkeypatch.setattr("ai.followup_command._execute_followup_argv", _fake_execute_followup_argv)

    result = execute_action_spec("kubectl_clickhouse_query", _build_args(), {})

    assert result["status"] == "executed"
    assert result["attempt"] == 2
    assert result["max_attempts"] == 2
    assert query_attempt["count"] == 2
    assert len(calls) == 3
    assert all(timeout == 60 for _, timeout in calls)


def test_execute_action_spec_blocks_after_two_timeouts(monkeypatch):
    query_attempt = {"count": 0}

    def _fake_execute_followup_argv(argv, timeout_seconds):
        _ = timeout_seconds
        if "get" in argv and "pods" in argv:
            return {
                "status": "executed",
                "exit_code": 0,
                "duration_ms": 5,
                "stdout": "clickhouse-0",
                "stderr": "",
                "output_truncated": False,
                "timed_out": False,
            }
        query_attempt["count"] += 1
        return {
            "status": "failed",
            "exit_code": -9,
            "duration_ms": 60000,
            "stdout": "",
            "stderr": "timeout",
            "output_truncated": False,
            "timed_out": True,
        }

    monkeypatch.setattr("ai.followup_command._execute_followup_argv", _fake_execute_followup_argv)

    result = execute_action_spec("kubectl_clickhouse_query", _build_args(), {})

    assert result["status"] == "blocked"
    assert result["timed_out"] is True
    assert result["attempt"] == 2
    assert result["max_attempts"] == 2
    assert "缩小时间窗口" in str(result.get("next_suggestion", ""))
    assert query_attempt["count"] == 2
