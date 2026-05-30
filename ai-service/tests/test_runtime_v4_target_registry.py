"""
Tests for runtime v4 target registry persistence behavior.
"""

from types import SimpleNamespace
from unittest.mock import Mock

import pytest

from ai.runtime_v4.targets.service import RuntimeV4TargetRegistry, ensure_runtime_v4_default_targets


def _build_storage_adapter(execute):
    return SimpleNamespace(
        ch_database="logs",
        ch_client=SimpleNamespace(execute=execute),
        config={"clickhouse": {"database": "logs"}},
    )


def test_registry_bootstrap_and_upsert_insert_rows_to_clickhouse():
    execute = Mock(return_value=[])
    registry = RuntimeV4TargetRegistry(storage_adapter=_build_storage_adapter(execute))

    result = registry.upsert_target(
        target_id="tgt-k8s-islap",
        target_kind="k8s_cluster",
        target_identity="namespace:islap",
        display_name="ISLAP",
        description="cluster",
        capabilities=["read_logs", "restart_workload"],
        credential_scope={"namespace": "islap"},
        metadata={"env": "dev"},
        updated_by="qa",
        reason="init",
        run_id="run-001",
        action_id="act-001",
    )

    assert result["target"]["target_id"] == "tgt-k8s-islap"
    sql_calls = [str(call.args[0]).strip() for call in execute.call_args_list]
    assert any("CREATE TABLE IF NOT EXISTS logs.ai_runtime_v4_targets" in sql for sql in sql_calls)
    assert any("CREATE TABLE IF NOT EXISTS logs.ai_runtime_v4_target_changes" in sql for sql in sql_calls)
    assert any(sql == "DROP VIEW IF EXISTS logs.v_ai_runtime_v4_targets_latest" for sql in sql_calls)
    assert any("INSERT INTO logs.ai_runtime_v4_targets" in sql for sql in sql_calls)
    assert any("INSERT INTO logs.ai_runtime_v4_target_changes" in sql for sql in sql_calls)


def test_registry_get_target_loads_from_clickhouse_when_cache_is_empty():
    row = [
        "tgt-db-main",
        "clickhouse",
        "database:logs",
        "Main DB",
        "primary database",
        '["read_logs","run_query"]',
        '{"database":"logs"}',
        '{"region":"cn"}',
        "active",
        2,
        "qa",
        "2026-03-23T00:00:00Z",
        "2026-03-23T00:01:00Z",
    ]

    def _execute(sql, params=None):
        text = str(sql)
        if "FROM system.tables" in text:
            return []
        if "SELECT max(seq)" in text:
            return [[0]]
        if "FROM logs.ai_runtime_v4_targets" in text and "WHERE target_id =" in text:
            return [row]
        return []

    registry = RuntimeV4TargetRegistry(storage_adapter=_build_storage_adapter(_execute))
    registry.clear()

    target = registry.get_target("tgt-db-main")

    assert target is not None
    assert target["target_kind"] == "clickhouse"
    assert target["capabilities"] == ["read_logs", "run_query"]
    assert target["version"] == 2


def test_registry_list_changes_reads_from_clickhouse():
    change_row = [
        3,
        "tchg-001",
        "target_updated",
        "tgt-http-001",
        "http_endpoint",
        "run-003",
        "act-003",
        "policy update",
        '{"status":"active"}',
        '{"status":"active","version":3}',
        "2026-03-23T00:02:00Z",
        "qa",
    ]

    def _execute(sql, params=None):
        text = str(sql)
        if "FROM system.tables" in text:
            return []
        if "SELECT max(seq)" in text:
            return [[3]]
        if "FROM logs.ai_runtime_v4_target_changes" in text and "WHERE seq >" in text:
            assert params["after_seq"] == 2
            assert params["target_id"] == "tgt-http-001"
            return [change_row]
        return []

    registry = RuntimeV4TargetRegistry(storage_adapter=_build_storage_adapter(_execute))

    changes = registry.list_changes(target_id="tgt-http-001", after_seq=2, limit=10)

    assert changes
    assert changes[0]["seq"] == 3
    assert changes[0]["change_id"] == "tchg-001"
    assert changes[0]["target_id"] == "tgt-http-001"


def test_registry_host_target_missing_node_metadata_requires_manual():
    registry = RuntimeV4TargetRegistry(storage_adapter=None)
    registry.upsert_target(
        target_id="tgt-host-01",
        target_kind="host_node",
        target_identity="host:unknown",
        display_name="worker-01",
        description="host target without node metadata",
        capabilities=["read_host_state"],
        credential_scope={},
        metadata={
            "cluster_id": "cluster-dev",
            "preferred_executor_profiles": ["toolbox-node-readonly"],
            "risk_tier": "high",
        },
        updated_by="qa",
        reason="unit-test",
    )

    resolved = registry.resolve_target(
        target_id="tgt-host-01",
        required_capabilities=["read_host_state"],
        reason="exec precheck target capability gate",
    )

    assert resolved["result"] == "manual_required"
    assert "metadata missing required fields" in str(resolved["reason"]).lower()
    contract = resolved["metadata_contract"]
    assert "node_name" in contract["missing_required_keys"]


def test_ensure_runtime_v4_default_targets_creates_k8s_target(monkeypatch: pytest.MonkeyPatch):
    monkeypatch.setenv("AI_RUNTIME_V4_TARGET_AUTO_SEED_ENABLED", "true")
    monkeypatch.setenv("AI_RUNTIME_V4_TARGET_DEFAULT_NAMESPACE", "islap")
    monkeypatch.setenv("AI_RUNTIME_V4_TARGET_DEFAULT_CLUSTER_ID", "cluster-dev")
    monkeypatch.setenv(
        "AI_RUNTIME_V4_TARGET_DEFAULT_K8S_PROFILES",
        "toolbox-k8s-readonly,toolbox-k8s-mutating",
    )

    registry = RuntimeV4TargetRegistry(storage_adapter=None)
    result = ensure_runtime_v4_default_targets(registry)

    assert result["enabled"] is True
    assert "namespace:islap" in result["created"]

    resolved = registry.resolve_target_by_identity(
        target_kind="k8s_cluster",
        target_identity="namespace:islap",
        required_capabilities=["read_logs"],
        reason="exec precheck target capability gate",
    )
    assert resolved["registered"] is True
    assert resolved["result"] == "allow"
    contract = resolved["metadata_contract"]
    assert contract["missing_required_keys"] == []
    assert contract["metadata"]["cluster_id"] == "cluster-dev"
    assert contract["metadata"]["preferred_executor_profiles"] == [
        "toolbox-k8s-readonly",
        "toolbox-k8s-mutating",
    ]


def test_ensure_runtime_v4_default_targets_repairs_missing_required_metadata(monkeypatch: pytest.MonkeyPatch):
    monkeypatch.setenv("AI_RUNTIME_V4_TARGET_AUTO_SEED_ENABLED", "true")
    monkeypatch.setenv("AI_RUNTIME_V4_TARGET_DEFAULT_NAMESPACE", "islap")
    monkeypatch.setenv("AI_RUNTIME_V4_TARGET_DEFAULT_CLUSTER_ID", "cluster-dev")
    monkeypatch.setenv(
        "AI_RUNTIME_V4_TARGET_DEFAULT_K8S_PROFILES",
        "toolbox-k8s-readonly,toolbox-k8s-mutating",
    )

    registry = RuntimeV4TargetRegistry(storage_adapter=None)
    registry.upsert_target(
        target_id="tgt-k8s-islap",
        target_kind="k8s_cluster",
        target_identity="namespace:islap",
        display_name="islap namespace",
        description="legacy target without metadata",
        capabilities=["read_logs"],
        credential_scope={"namespace": "islap"},
        metadata={},
        updated_by="qa",
        reason="legacy seed",
    )

    result = ensure_runtime_v4_default_targets(registry)
    assert "namespace:islap" in result["updated"]

    resolved = registry.resolve_target_by_identity(
        target_kind="k8s_cluster",
        target_identity="namespace:islap",
        required_capabilities=["read_logs"],
        reason="exec precheck target capability gate",
    )
    assert resolved["registered"] is True
    assert resolved["result"] == "allow"
    assert resolved["metadata_contract"]["missing_required_keys"] == []


def test_resolve_target_by_identity_ignores_inactive_duplicates():
    registry = RuntimeV4TargetRegistry(storage_adapter=None)
    registry.upsert_target(
        target_id="auto-k8s-cluster-namespace-islap",
        target_kind="k8s_cluster",
        target_identity="namespace:islap",
        display_name="islap namespace",
        description="auto seeded",
        capabilities=["read_logs"],
        credential_scope={"namespace": "islap"},
        metadata={
            "cluster_id": "cluster-local",
            "namespace": "islap",
            "risk_tier": "high",
            "preferred_executor_profiles": ["toolbox-k8s-readonly"],
        },
        updated_by="qa",
        reason="unit-test",
    )
    registry.upsert_target(
        target_id="tgt-k8s-islap",
        target_kind="k8s_cluster",
        target_identity="namespace:islap",
        display_name="legacy seed",
        description="legacy duplicate",
        capabilities=["read_logs"],
        credential_scope={"namespace": "islap"},
        metadata={
            "cluster_id": "cluster-local",
            "namespace": "islap",
            "risk_tier": "high",
            "preferred_executor_profiles": ["toolbox-k8s-readonly"],
        },
        updated_by="qa",
        reason="unit-test",
    )
    registry.deactivate_target(
        "tgt-k8s-islap",
        updated_by="qa",
        reason="deactivate duplicate",
    )

    resolved = registry.resolve_target_by_identity(
        target_kind="k8s_cluster",
        target_identity="namespace:islap",
        required_capabilities=["read_logs"],
        reason="exec precheck target capability gate",
    )

    assert resolved["registered"] is True
    assert resolved["result"] == "allow"
    assert resolved["status"] == "active"
    assert resolved["target_id"] == "auto-k8s-cluster-namespace-islap"
    assert resolved.get("ambiguous_targets") in (None, [])
