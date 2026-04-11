from ai.agent_runtime.recovery import attempt_command_recovery


def test_attempt_command_recovery_repairs_compact_clickhouse_query_spacing():
    recovery = attempt_command_recovery(
        command="",
        command_spec={
            "tool": "kubectl_clickhouse_query",
            "args": {
                "namespace": "islap",
                "pod_name": "clickhouse-0",
                "query": "DESCRIBETABLElogs.obs_traces_1m",
                "timeout_s": 30,
            },
        },
        failure_code="sql_preflight_failed",
        failure_message="sql_preflight_failed",
        max_rounds=2,
    )
    assert recovery["status"] == "recovered"
    repaired_spec = recovery.get("command_spec") or {}
    repaired_query = (repaired_spec.get("query") or "").upper()
    assert repaired_query.startswith("DESCRIBE TABLE ")


def test_attempt_command_recovery_without_spec_asks_user():
    recovery = attempt_command_recovery(
        command="",
        command_spec={},
        failure_code="sql_preflight_failed",
        failure_message="query is empty",
        max_rounds=2,
    )
    assert recovery["status"] == "ask_user"
    assert recovery["failure_code"] == "sql_preflight_failed"
