"""
Tests for ai.agent_runtime.store.
"""

from types import SimpleNamespace
from unittest.mock import Mock

from ai.agent_runtime.models import AgentRun
from ai.agent_runtime.store import AgentRuntimeStore


def _build_storage_adapter(execute):
    return SimpleNamespace(
        ch_database="logs",
        ch_client=SimpleNamespace(execute=execute),
        config={"clickhouse": {"database": "logs"}},
    )


def test_save_run_includes_conversation_id_in_clickhouse_row():
    execute = Mock(return_value=[])
    store = AgentRuntimeStore(storage_adapter=_build_storage_adapter(execute))
    run = AgentRun(
        run_id="run-001",
        session_id="sess-001",
        conversation_id="conv-001",
        analysis_type="log",
        engine="agent-runtime-v1",
        runtime_version="v1",
        user_message_id="msg-u-001",
        assistant_message_id="msg-001",
        status="running",
        question="排查 query-service 超时",
    )

    store.save_run(run)

    insert_calls = [
        call for call in execute.call_args_list
        if "INSERT INTO logs.ai_agent_runs" in call.args[0]
    ]
    assert insert_calls
    inserted_row = insert_calls[-1].args[1][0]
    assert inserted_row["conversation_id"] == "conv-001"


def test_get_run_deserializes_conversation_id_from_clickhouse():
    row = [
        "run-002",
        "sess-002",
        "conv-002",
        "log",
        "agent-runtime-v1",
        "v1",
        "msg-u-002",
        "msg-002",
        "query-service",
        "trace-002",
        "blocked",
        "{}",
        "{}",
        '{"current_phase":"blocked","blocked_reason":"approval_rejected"}',
        "",
        "",
        "2026-03-19T00:00:00Z",
        "2026-03-19T00:00:01Z",
        "2026-03-19T00:00:01Z",
    ]

    def _execute(sql, params=None):
        text = str(sql)
        if "FROM system.tables" in text:
            return []
        if "SELECT" in text and "FROM logs.ai_agent_runs" in text:
            return [row]
        return []

    store = AgentRuntimeStore(storage_adapter=_build_storage_adapter(_execute))
    store._get_run_read_source = lambda: (store.run_table, True)  # noqa: SLF001

    run = store.get_run("run-002")

    assert run is not None
    assert run.conversation_id == "conv-002"
    assert run.status == "blocked"
    assert run.summary_json["blocked_reason"] == "approval_rejected"


def test_store_bootstrap_adds_conversation_id_column_before_rebuilding_latest_view():
    execute = Mock(return_value=[])

    AgentRuntimeStore(storage_adapter=_build_storage_adapter(execute))

    sql_calls = [str(call.args[0]).strip() for call in execute.call_args_list]
    alter_index = next(
        index for index, sql in enumerate(sql_calls)
        if "ALTER TABLE logs.ai_agent_runs" in sql and "ADD COLUMN IF NOT EXISTS conversation_id" in sql
    )
    drop_view_index = next(
        index for index, sql in enumerate(sql_calls)
        if sql == "DROP VIEW IF EXISTS logs.v_ai_agent_runs_latest"
    )
    create_view_index = next(
        index for index, sql in enumerate(sql_calls)
        if "CREATE VIEW IF NOT EXISTS logs.v_ai_agent_runs_latest AS" in sql
    )

    assert alter_index < drop_view_index < create_view_index
