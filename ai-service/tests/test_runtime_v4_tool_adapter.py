"""Tests for the OpenHands tool adapter."""

from ai.runtime_v4.backend.tool_adapter import (
    map_skill_step_to_runtime_command,
    map_tool_call_to_runtime_command,
)


def test_map_generic_exec_tool_call_to_runtime_command_request():
    payload = map_tool_call_to_runtime_command(
        run_id="run-oh-tool-001",
        tool_name="generic_exec",
        tool_args={
            "command": "kubectl -n islap logs deploy/query-service --since=15m --tail=200",
            "purpose": "collect query-service timeout evidence",
            "target_kind": "k8s_cluster",
            "target_identity": "namespace:islap",
            "timeout_s": 20,
        },
    )

    assert payload["tool_name"] == "command.exec"
    assert payload["command"].startswith("kubectl -n islap logs")
    assert payload["confirmed"] is False
    assert payload["command_spec"]["tool"] == "generic_exec"


def test_map_generic_exec_tool_call_clamps_timeout():
    payload = map_tool_call_to_runtime_command(
        run_id="run-oh-tool-002",
        tool_name="generic_exec",
        tool_args={
            "command": "kubectl get pods",
            "timeout_s": 999,
        },
    )

    assert payload["timeout_seconds"] == 180
    assert payload["command_spec"]["args"]["timeout_s"] == 180


def test_map_skill_step_to_runtime_command_preserves_skill_metadata():
    payload = map_skill_step_to_runtime_command(
        run_id="run-oh-tool-003",
        skill_name="observability_read_path_latency",
        step={
            "step_id": "read-latency-log-tail",
            "title": "拉取 query-service 读路径日志",
            "purpose": "确认超时症状",
            "command_spec": {
                "tool": "generic_exec",
                "args": {
                    "command": "kubectl -n islap logs -l app=query-service --since=15m --tail=200",
                    "target_kind": "runtime_node",
                    "target_identity": "runtime:local",
                    "timeout_s": 20,
                },
            },
        },
    )

    assert payload["tool_name"] == "command.exec"
    assert payload["skill_name"] == "observability_read_path_latency"
    assert payload["step_id"] == "read-latency-log-tail"
    assert payload["command"].startswith("kubectl -n islap logs")
    assert payload["confirmed"] is False
    assert payload["elevated"] is False
    assert payload["command_spec"]["tool"] == "generic_exec"


def test_map_structured_tool_call_preserves_extra_args():
    payload = map_tool_call_to_runtime_command(
        run_id="run-oh-tool-004",
        tool_name="kubectl_clickhouse_query",
        tool_args={
            "query": "SELECT count() FROM system.query_log",
            "namespace": "islap",
            "pod_name": "clickhouse-0",
            "purpose": "检查慢查询",
            "title": "查看 clickhouse query_log",
            "timeout_s": 30,
        },
    )

    assert payload["tool_name"] == "command.exec"
    assert payload["purpose"] == "检查慢查询"
    assert payload["title"] == "查看 clickhouse query_log"
    assert payload["command_spec"]["tool"] == "kubectl_clickhouse_query"
    assert payload["command_spec"]["args"]["query"] == "SELECT count() FROM system.query_log"
    assert payload["command_spec"]["args"]["namespace"] == "islap"
    assert payload["command_spec"]["args"]["pod_name"] == "clickhouse-0"
    assert payload["command_spec"]["args"]["timeout_s"] == 30
