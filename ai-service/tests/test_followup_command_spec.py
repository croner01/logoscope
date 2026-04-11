"""
Tests for structured follow-up command spec compiler.
"""

import pytest

from ai.followup_command_spec import (
    build_command_spec_self_repair_payload,
    build_followup_command_spec_match_key,
    compile_followup_command_spec,
    map_followup_reason_group,
    normalize_followup_command_spec,
    normalize_followup_reason_code,
)


def _build_spec(query: str) -> dict:
    return {
        "tool": "kubectl_clickhouse_query",
        "args": {
            "namespace": "islap",
            "pod_name": "clickhouse-0",
            "query": query,
            "timeout_s": 60,
        },
    }


def test_compile_followup_command_spec_keeps_sql_tokens_intact():
    query = (
        "SELECT container_name, attributes_json, multiSearchAnyCaseInsensitiveUTF8(attributes_json, ['timeout']) "
        "FROM logs.otel_logs WHERE optimize_read_in_order=1 ORDER BY timestamp DESC LIMIT 10"
    )
    result = compile_followup_command_spec(_build_spec(query))

    assert result["ok"] is True
    compiled_query = result["command_spec"]["args"]["query"]
    assert "container_name" in compiled_query
    assert "attributes_json" in compiled_query
    assert "ORDER BY" in compiled_query
    assert "multiSearchAnyCaseInsensitiveUTF8" in compiled_query
    assert "optimize_read_in_order" in compiled_query

    assert "c ON tainer_name" not in compiled_query
    assert "attributes_js ON" not in compiled_query
    assert "OR DER BY" not in compiled_query
    assert "multiSearchAnyCaseInsensitive UTF8" not in compiled_query
    assert "optimize_read_in_ OR der" not in compiled_query


def test_compile_followup_command_spec_rejects_unsafe_selector():
    result = compile_followup_command_spec(
        {
            "tool": "kubectl_clickhouse_query",
            "args": {
                "namespace": "islap",
                "pod_selector": "app=clickhouse;cat /etc/passwd",
                "query": "SELECT 1",
                "timeout_s": 60,
            },
        }
    )
    assert result["ok"] is False
    assert "pod_selector" in str(result.get("reason", ""))


def test_build_followup_command_spec_match_key_normalizes_whitespace_only():
    spec_a = normalize_followup_command_spec(_build_spec("SELECT 1\nFROM system.one"))
    spec_b = normalize_followup_command_spec(_build_spec("  SELECT   1   FROM   system.one  "))
    assert build_followup_command_spec_match_key(spec_a) == build_followup_command_spec_match_key(spec_b)


def test_compile_followup_command_spec_keeps_runtime_preflight_in_execution_domain(monkeypatch):
    def _fake_preflight_sql_syntax(**kwargs):
        raise AssertionError(f"unexpected local SQL preflight call: {kwargs}")

    monkeypatch.setattr(
        "ai.followup_command_spec.preflight_sql_syntax",
        _fake_preflight_sql_syntax,
    )

    result = compile_followup_command_spec(_build_spec("SELECT * FROM system.one"), run_sql_preflight=True)
    assert result["ok"] is True
    assert "kubectl -n islap exec -i" in str(result.get("command") or "")
    assert "clickhouse-0" in str(result.get("command") or "")
    assert result["command_spec"]["runtime_preflight_requested"] is True


def test_compile_followup_command_spec_supports_generic_exec():
    result = compile_followup_command_spec(
        {
            "tool": "generic_exec",
            "args": {
                "command": "kubectl get pods -n islap",
                "target_kind": "k8s_cluster",
                "target_identity": "namespace:islap",
                "timeout_s": 30,
            },
        }
    )

    assert result["ok"] is True
    assert result["command"] == "kubectl get pods -n islap"
    assert result["command_spec"]["tool"] == "generic_exec"
    assert result["command_spec"]["args"]["target_kind"] == "k8s_cluster"
    assert result["command_spec"]["args"]["target_identity"] == "namespace:islap"


def test_compile_followup_command_spec_rejects_glued_command_head_in_generic_exec():
    result = compile_followup_command_spec(
        {
            "tool": "generic_exec",
            "args": {
                "command": "kubectlgetpods -n islap -l app=query-service",
                "target_kind": "k8s_cluster",
                "target_identity": "namespace:islap",
                "timeout_s": 30,
            },
        }
    )

    assert result["ok"] is False
    assert result["reason"] == "glued_command_tokens"


@pytest.mark.parametrize(
    ("raw_reason", "expected_code", "expected_group"),
    [
        ("glued_command_tokens: token appears glued", "glued_command_tokens", "GLUE_SYNTAX"),
        ("glued_sql_tokens: sql keyword must be separated", "glued_sql_tokens", "GLUE_SYNTAX"),
        ("invalid_kubectl_token: invalid kubectl token", "invalid_kubectl_token", "GLUE_K8S_TOKEN"),
        ("suspicious_selector_namespace_glue app=query-service-nislap", "suspicious_selector_namespace_glue", "GLUE_K8S_TOKEN"),
        ("missing_or_invalid_command_spec command_spec is required", "missing_or_invalid_command_spec", "SPEC_MISSING"),
        ("missing_target_identity target_identity is required", "missing_target_identity", "SPEC_MISSING"),
        ("unsupported_command_head: unsupported command head: bash", "unsupported_command_head", "SECURITY_GUARD"),
        ("clickhouse_multi_statement_not_allowed: multiple statements", "clickhouse_multi_statement_not_allowed", "SECURITY_GUARD"),
        ("command_argv contains blocked shell operators", "unsupported_command_head", "SECURITY_GUARD"),
        ("unsupported command_spec tool: unknown_tool", "missing_or_invalid_command_spec", "SPEC_MISSING"),
        ("target_identity must look like database:<name>", "missing_target_identity", "SPEC_MISSING"),
        ("target_kind_mismatch", "target_kind_mismatch", "SPEC_MISSING"),
        ("unexpected_runtime_error", "other", "OTHER"),
    ],
)
def test_followup_reason_group_mapping_normalizes_known_and_unknown_codes(
    raw_reason: str,
    expected_code: str,
    expected_group: str,
):
    assert normalize_followup_reason_code(raw_reason) == expected_code
    assert map_followup_reason_group(raw_reason) == expected_group


def test_compile_followup_command_spec_canonicalizes_compact_kubectl_flags():
    result = compile_followup_command_spec(
        {
            "tool": "generic_exec",
            "args": {
                "command": "kubectl get pods -nislap -lapp=query-service",
                "target_kind": "k8s_cluster",
                "target_identity": "namespace:islap",
                "timeout_s": 30,
            },
        }
    )

    assert result["ok"] is True
    assert result["command"] == "kubectl get pods -n islap -l app=query-service"


def test_compile_followup_command_spec_rejects_kubectl_positional_token_with_equals():
    result = compile_followup_command_spec(
        {
            "tool": "generic_exec",
            "args": {
                "command": "kubectl get pods-nislap-lapp=query-service",
                "target_kind": "k8s_cluster",
                "target_identity": "cluster:kubernetes",
                "timeout_s": 30,
            },
        }
    )

    assert result["ok"] is False
    assert result["reason"] == "invalid_kubectl_token"


def test_compile_followup_command_spec_infers_k8s_target_when_runtime_default_is_passed():
    result = compile_followup_command_spec(
        {
            "tool": "generic_exec",
            "args": {
                "command": "kubectl get pods -n islap",
                "target_kind": "runtime_node",
                "target_identity": "runtime:local",
                "timeout_s": 20,
            },
        }
    )

    assert result["ok"] is True
    args = result["command_spec"]["args"]
    assert args["target_kind"] == "k8s_cluster"
    assert args["target_identity"] == "namespace:islap"


def test_compile_followup_command_spec_rejects_mismatched_target_identity_for_scoped_kubectl_command():
    result = compile_followup_command_spec(
        {
            "tool": "generic_exec",
            "args": {
                "command": "kubectl get pods -n islap",
                "target_kind": "k8s_cluster",
                "target_identity": "namespace:prod",
                "timeout_s": 20,
            },
        }
    )

    assert result["ok"] is False
    assert result["reason"] == "target_identity_mismatch"


def test_compile_followup_command_spec_rejects_invalid_k8s_namespace_in_generic_exec():
    result = compile_followup_command_spec(
        {
            "tool": "generic_exec",
            "args": {
                "command": "kubectl get pods -n islap-lappin(clickhouse,clickhouse-server) --no-headers",
                "target_kind": "k8s_cluster",
                "target_identity": "namespace:islap",
                "timeout_s": 20,
            },
        }
    )

    assert result["ok"] is False
    assert result["reason"] == "invalid namespace in command_spec"


def test_compile_followup_command_spec_rejects_selector_namespace_glue_in_generic_exec():
    result = compile_followup_command_spec(
        {
            "tool": "generic_exec",
            "args": {
                "command": "kubectl get pods -l app=query-service-nislap -n islap",
                "target_kind": "k8s_cluster",
                "target_identity": "namespace:islap",
                "timeout_s": 20,
            },
        }
    )

    assert result["ok"] is False
    assert result["reason"] == "suspicious_selector_namespace_glue"


def test_compile_followup_command_spec_allows_selector_without_namespace_glue():
    result = compile_followup_command_spec(
        {
            "tool": "generic_exec",
            "args": {
                "command": "kubectl get pods -l app=query-service -n islap",
                "target_kind": "k8s_cluster",
                "target_identity": "namespace:islap",
                "timeout_s": 20,
            },
        }
    )

    assert result["ok"] is True
    assert result["command"] == "kubectl get pods -l app=query-service -n islap"


def test_compile_followup_command_spec_rejects_selector_namespace_glue_without_explicit_namespace():
    result = compile_followup_command_spec(
        {
            "tool": "generic_exec",
            "args": {
                "command": "kubectl get pods -l app=query-service-nislap",
                "target_kind": "k8s_cluster",
                "target_identity": "cluster:kubernetes",
                "timeout_s": 20,
            },
        }
    )

    assert result["ok"] is False
    assert result["reason"] == "suspicious_selector_namespace_glue"


def test_compile_followup_command_spec_rejects_compact_pipe_operator_in_generic_exec():
    result = compile_followup_command_spec(
        {
            "tool": "generic_exec",
            "args": {
                "command": "echo safe|head -n 1",
                "target_kind": "runtime_node",
                "target_identity": "runtime:local",
                "timeout_s": 20,
            },
        }
    )

    assert result["ok"] is False
    assert result["reason"] == "command_argv contains blocked shell operators"


def test_compile_followup_command_spec_rejects_compact_redirection_operator_in_generic_exec():
    result = compile_followup_command_spec(
        {
            "tool": "generic_exec",
            "args": {
                "command": "echo ok 2>/tmp/logoscope-test",
                "target_kind": "runtime_node",
                "target_identity": "runtime:local",
                "timeout_s": 20,
            },
        }
    )

    assert result["ok"] is False
    assert result["reason"] == "command_argv contains blocked shell operators"


def test_compile_followup_command_spec_allows_literal_ampersand_argument_in_generic_exec():
    result = compile_followup_command_spec(
        {
            "tool": "generic_exec",
            "args": {
                "command": "echo 'a&b'",
                "target_kind": "runtime_node",
                "target_identity": "runtime:local",
                "timeout_s": 20,
            },
        }
    )

    assert result["ok"] is True
    assert result["command"] == "echo 'a&b'"


def test_compile_followup_command_spec_repairs_split_clickhouse_client_head():
    result = compile_followup_command_spec(
        {
            "tool": "generic_exec",
            "args": {
                "command": "clickhouse -client --query \"SELECT 1\"",
                "target_kind": "clickhouse_cluster",
                "target_identity": "database:default",
                "timeout_s": 20,
            },
        }
    )

    assert result["ok"] is True
    assert str(result.get("command") or "").startswith("clickhouse-client --query ")
    compiled_argv = (result.get("command_spec") or {}).get("args", {}).get("command_argv") or []
    assert compiled_argv and compiled_argv[0] == "clickhouse-client"


def test_compile_followup_command_spec_allows_kubectl_exec_clickhouse_query_with_parentheses():
    result = compile_followup_command_spec(
        {
            "tool": "generic_exec",
            "args": {
                "command": (
                    "kubectl -n islap exec deploy/clickhouse -- clickhouse-client --query "
                    "\"SELECT event_time FROM system.query_log "
                    "WHERE event_time >= now() - INTERVAL 15 MINUTE LIMIT 1\""
                ),
                "target_kind": "k8s_cluster",
                "target_identity": "namespace:islap",
                "timeout_s": 30,
            },
        }
    )

    assert result["ok"] is True
    assert str(result.get("command") or "").startswith("kubectl -n islap exec deploy/clickhouse -- clickhouse-client --query ")
    assert str((result.get("command_spec") or {}).get("tool") or "") == "generic_exec"


def test_compile_followup_command_spec_prefers_clickhouse_target_identity_over_pod_guessing():
    result = compile_followup_command_spec(
        {
            "tool": "clickhouse_query",
            "args": {
                "target_kind": "clickhouse_cluster",
                "target_identity": "database:logs",
                "query": "DESCRIBE TABLE logs.obs_traces_1m",
                "timeout_s": 60,
            },
        }
    )

    assert result["ok"] is True
    assert str(result.get("command") or "").startswith("clickhouse-client --query ")
    assert "kubectl" not in str(result.get("command") or "")
    assert result["command_spec"]["target_kind"] == "clickhouse_cluster"
    assert result["command_spec"]["target_identity"] == "database:logs"


def test_compile_followup_command_spec_k8s_clickhouse_namespace_without_pod_rejects_local_fallback(monkeypatch):
    monkeypatch.setenv("AI_RUNTIME_K8S_POD_AUTORESOLVE_ENABLED", "false")
    result = compile_followup_command_spec(
        {
            "tool": "kubectl_clickhouse_query",
            "args": {
                "namespace": "islap",
                "target_kind": "clickhouse_cluster",
                "target_identity": "database:logs",
                "query": "DESCRIBE TABLE logs.obs_traces_1m",
                "timeout_s": 60,
            },
        }
    )

    assert result["ok"] is False
    assert result["reason"] == "missing_pod_name_for_k8s_clickhouse_query"


def test_compile_followup_command_spec_k8s_clickhouse_autoresolves_pod_without_shell(monkeypatch):
    monkeypatch.setenv("AI_RUNTIME_K8S_POD_AUTORESOLVE_ENABLED", "true")
    monkeypatch.setenv("AI_RUNTIME_CLICKHOUSE_POD_SELECTOR_DEFAULT", "app=clickhouse")

    class _Completed:
        def __init__(self):
            self.returncode = 0
            self.stdout = "clickhouse-0"
            self.stderr = ""

    def _fake_run(*_args, **_kwargs):
        return _Completed()

    monkeypatch.setattr("ai.followup_command_spec.subprocess.run", _fake_run)

    result = compile_followup_command_spec(
        {
            "tool": "kubectl_clickhouse_query",
            "args": {
                "namespace": "islap",
                "target_kind": "clickhouse_cluster",
                "target_identity": "database:logs",
                "query": "DESCRIBE TABLE logs.obs_traces_1m",
                "timeout_s": 60,
            },
        }
    )

    assert result["ok"] is True
    assert result["command"].startswith(
        "kubectl -n islap exec -i clickhouse-0 -- clickhouse-client --query "
    )
    compiled_args = result["command_spec"]["args"]
    assert compiled_args["namespace"] == "islap"
    assert compiled_args["pod_name"] == "clickhouse-0"
    assert compiled_args["pod_selector"] == "app=clickhouse"


def test_compile_followup_command_spec_derives_target_identity_from_query_when_missing():
    result = compile_followup_command_spec(
        {
            "tool": "clickhouse_query",
            "args": {
                "query": "SELECT * FROM metrics.samples LIMIT 1",
                "timeout_s": 60,
            },
        }
    )

    assert result["ok"] is True
    assert result["command_spec"]["target_identity"] == "database:metrics"


def test_compile_followup_command_spec_requires_target_identity_when_query_scope_is_ambiguous():
    result = compile_followup_command_spec(
        {
            "tool": "kubectl_clickhouse_query",
            "args": {
                "query": "SELECT count() FROM otel_logs LIMIT 10",
                "timeout_s": 60,
            },
        }
    )

    assert result["ok"] is False
    assert result["reason"] == "missing_target_identity"


def test_compile_followup_command_spec_rejects_glued_sql_tokens():
    result = compile_followup_command_spec(
        {
            "tool": "kubectl_clickhouse_query",
            "args": {
                "query": "SELECTparent.service_nameASsource_service FROM logs.tracesPREWHERE timestamp>now()-INTERVAL1HOUR",
                "target_kind": "clickhouse_cluster",
                "target_identity": "database:logs",
                "timeout_s": 30,
            },
        }
    )

    assert result["ok"] is False
    assert result["reason"] == "glued_sql_tokens"
    assert "spaces" in str(result.get("detail") or "")


def test_compile_followup_command_spec_rejects_non_readonly_sql_prefix():
    result = compile_followup_command_spec(
        {
            "tool": "kubectl_clickhouse_query",
            "args": {
                "query": "INSERT INTO logs.traces VALUES (1)",
                "target_kind": "clickhouse_cluster",
                "target_identity": "database:logs",
                "timeout_s": 30,
            },
        }
    )

    assert result["ok"] is False
    assert result["reason"] == "unsupported_clickhouse_readonly_query"


def test_compile_followup_command_spec_rejects_with_prefix_sql():
    result = compile_followup_command_spec(
        {
            "tool": "kubectl_clickhouse_query",
            "args": {
                "query": "WITH 1 AS x SELECT x",
                "target_kind": "clickhouse_cluster",
                "target_identity": "database:default",
                "timeout_s": 30,
            },
        }
    )

    assert result["ok"] is False
    assert result["reason"] == "unsupported_clickhouse_readonly_query"


def test_build_command_spec_self_repair_payload_suggests_spacing_fix_for_glued_sql():
    payload = build_command_spec_self_repair_payload(
        reason="glued_sql_tokens",
        detail="sql keyword 'where' must be separated by spaces",
        command_spec={
            "tool": "kubectl_clickhouse_query",
            "args": {
                "query": "SELECTid FROM logs.otel_logsWHERE level='error' LIMIT 5",
                "target_kind": "clickhouse_cluster",
                "target_identity": "database:logs",
            },
        },
    )
    suggested = payload.get("suggested_command_spec") or {}
    query = str((suggested.get("args") or {}).get("query") or suggested.get("query") or "")
    assert payload.get("fix_code") == "glued_sql_tokens"
    assert "空格" in str(payload.get("fix_hint") or "") or "spaces" in str(payload.get("fix_hint") or "")
    assert "WHERE" in query.upper()
    assert "LOGS.OTEL_LOGS WHERE" in query.upper()


def test_build_command_spec_self_repair_payload_suggests_target_identity_when_inferable():
    payload = build_command_spec_self_repair_payload(
        reason="missing_target_identity",
        detail="target_identity is required",
        command_spec={
            "tool": "kubectl_clickhouse_query",
            "args": {
                "query": "SELECT count() FROM logs.otel_logs LIMIT 10",
                "target_kind": "clickhouse_cluster",
            },
        },
    )
    suggested = payload.get("suggested_command_spec") or {}
    args = suggested.get("args") if isinstance(suggested.get("args"), dict) else {}
    assert payload.get("fix_code") == "missing_target_identity"
    assert str(args.get("target_identity") or suggested.get("target_identity")) == "database:logs"


def test_build_command_spec_self_repair_payload_infers_k8s_target_for_missing_spec():
    payload = build_command_spec_self_repair_payload(
        reason="missing_or_invalid_command_spec",
        detail="command_spec is required",
        command_spec={},
        raw_command="kubectl get pods -n islap",
    )
    suggested = payload.get("suggested_command_spec") or {}
    args = suggested.get("args") if isinstance(suggested.get("args"), dict) else {}
    assert payload.get("fix_code") == "missing_or_invalid_command_spec"
    assert str(args.get("target_kind") or suggested.get("target_kind")) == "k8s_cluster"
    assert str(args.get("target_identity") or suggested.get("target_identity")) == "namespace:islap"


def test_build_command_spec_self_repair_payload_repairs_glued_kubectl_command():
    payload = build_command_spec_self_repair_payload(
        reason="missing_or_invalid_command_spec",
        detail="command_spec is required",
        command_spec={},
        raw_command="kubectlgetpods -nislap -lapp=query-service",
    )
    suggested = payload.get("suggested_command_spec") or {}
    args = suggested.get("args") if isinstance(suggested.get("args"), dict) else {}
    assert payload.get("fix_code") == "missing_or_invalid_command_spec"
    assert str(args.get("command") or "").startswith("kubectl get pods -n islap")
    assert "-l app=query-service" in str(args.get("command") or "")


def test_build_command_spec_self_repair_payload_prefers_raw_command_when_spec_invalid():
    payload = build_command_spec_self_repair_payload(
        reason="invalid_kubectl_token",
        detail="kubectl positional token must not include '='",
        command_spec={
            "tool": "generic_exec",
            "args": {
                "command": "kubectl get pods-nislap-lapp=query-service",
                "target_kind": "k8s_cluster",
                "target_identity": "cluster:kubernetes",
                "timeout_s": 30,
            },
        },
        raw_command="kubectl get pods -n islap -l app=query-service",
    )
    suggested = payload.get("suggested_command_spec") or {}
    args = suggested.get("args") if isinstance(suggested.get("args"), dict) else {}
    assert payload.get("fix_code") == "invalid_kubectl_token"
    assert str(args.get("command") or "").startswith("kubectl get pods -n islap")
    assert str(args.get("target_identity") or suggested.get("target_identity")) == "namespace:islap"


def test_build_command_spec_self_repair_payload_fixes_selector_namespace_glue():
    payload = build_command_spec_self_repair_payload(
        reason="suspicious_selector_namespace_glue",
        detail="selector 'app=query-service-nislap' looks glued with namespace suffix",
        command_spec={
            "tool": "generic_exec",
            "args": {
                "command": "kubectl get pods -l app=query-service-nislap -n islap",
                "target_kind": "k8s_cluster",
                "target_identity": "namespace:islap",
                "timeout_s": 20,
            },
        },
    )
    suggested = payload.get("suggested_command_spec") or {}
    args = suggested.get("args") if isinstance(suggested.get("args"), dict) else {}
    assert payload.get("fix_code") == "suspicious_selector_namespace_glue"
    assert str(args.get("command") or "") == "kubectl get pods -l app=query-service -n islap"


def test_build_command_spec_self_repair_payload_fixes_selector_namespace_glue_without_namespace_flag():
    payload = build_command_spec_self_repair_payload(
        reason="suspicious_selector_namespace_glue",
        detail="selector 'app=query-service-nislap' looks glued with namespace suffix",
        command_spec={
            "tool": "generic_exec",
            "args": {
                "command": "kubectl get pods -l app=query-service-nislap",
                "target_kind": "k8s_cluster",
                "target_identity": "cluster:kubernetes",
                "timeout_s": 20,
            },
        },
    )
    suggested = payload.get("suggested_command_spec") or {}
    args = suggested.get("args") if isinstance(suggested.get("args"), dict) else {}
    assert payload.get("fix_code") == "suspicious_selector_namespace_glue"
    assert str(args.get("command") or "") == "kubectl get pods -l app=query-service -n islap"
