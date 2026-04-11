"""
Tests for runtime v4 LangGraph checkpoint persistence.
"""

from types import SimpleNamespace
from unittest.mock import Mock

from ai.runtime_v4.langgraph.checkpoint import GraphCheckpointStore
from ai.runtime_v4.langgraph.state import InnerGraphState


def _build_storage_adapter(execute):
    return SimpleNamespace(
        ch_database="logs",
        ch_client=SimpleNamespace(execute=execute),
        config={"clickhouse": {"database": "logs"}},
    )


def test_checkpoint_store_save_and_load_from_memory():
    store = GraphCheckpointStore()
    state = InnerGraphState(
        run_id="run-graph-001",
        question="检查 query-service 状态",
        iteration=1,
        max_iterations=4,
        phase="planning",
    )
    store.save(state)
    loaded = store.load("run-graph-001")

    assert loaded is not None
    assert loaded.run_id == "run-graph-001"
    assert loaded.question == "检查 query-service 状态"
    assert loaded.iteration == 1


def test_checkpoint_store_bootstrap_and_save_insert_rows_to_clickhouse():
    execute = Mock(return_value=[])
    store = GraphCheckpointStore(storage_adapter=_build_storage_adapter(execute))

    state = InnerGraphState(
        run_id="run-graph-002",
        question="执行一次内环推理",
        iteration=2,
        max_iterations=5,
        phase="observing",
        done=False,
    )
    store.save(state)

    sql_calls = [str(call.args[0]).strip() for call in execute.call_args_list]
    assert any("CREATE TABLE IF NOT EXISTS logs.ai_runtime_v4_graph_checkpoints" in sql for sql in sql_calls)
    assert any("INSERT INTO logs.ai_runtime_v4_graph_checkpoints" in sql for sql in sql_calls)


def test_checkpoint_store_loads_from_clickhouse_when_cache_empty():
    state_json = (
        '{"run_id":"run-graph-003","question":"恢复中断态","iteration":3,'
        '"max_iterations":6,"phase":"replan","actions":[],"observations":[],'
        '"reflection":{},"done":true}'
    )

    def _execute(sql, params=None):
        text = str(sql)
        if "SELECT state_json" in text:
            assert params["run_id"] == "run-graph-003"
            return [[state_json]]
        return []

    store = GraphCheckpointStore(storage_adapter=_build_storage_adapter(_execute))
    store.clear()
    loaded = store.load("run-graph-003")

    assert loaded is not None
    assert loaded.run_id == "run-graph-003"
    assert loaded.iteration == 3
    assert loaded.done is True
