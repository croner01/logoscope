"""Tests for API v2 runtime endpoints."""

import asyncio

import pytest
from fastapi import HTTPException

from api.ai_runtime_v2 import (
    cancel_run,
    create_thread,
    create_thread_run,
    deactivate_target,
    execute_run_command_action,
    get_run_events,
    get_run_snapshot,
    get_target,
    get_thread,
    interrupt_run,
    list_target_changes,
    list_targets,
    list_run_actions,
    list_run_approvals,
    list_thread_runs,
    register_target,
    resolve_approval,
    resolve_target_by_identity,
    resolve_target,
    stream_run_events,
)
from ai.agent_runtime import event_protocol
from ai.agent_runtime.service import AgentRuntimeService
from ai.runtime_v4 import reset_runtime_v4_bridge
from ai.runtime_v4.api_models import (
    ApprovalResolveRequest,
    CancelRequest,
    CommandActionRequest,
    InterruptRequest,
    RunCreateRequest,
    TargetDeactivateRequest,
    TargetRegisterRequest,
    TargetResolveByIdentityRequest,
    TargetResolveRequest,
    ThreadCreateRequest,
)
from ai.runtime_v4.store import get_runtime_v4_thread_store
from ai.runtime_v4.temporal.client import get_temporal_outer_client
from ai.runtime_v4.targets import get_runtime_v4_target_registry


def _build_runtime_service() -> AgentRuntimeService:
    return AgentRuntimeService(storage_adapter=None)


@pytest.fixture(autouse=True)
def _reset_v4_state(monkeypatch):
    get_runtime_v4_thread_store().clear()
    get_temporal_outer_client().clear()
    get_runtime_v4_target_registry().clear()
    reset_runtime_v4_bridge()
    runtime_service = _build_runtime_service()
    monkeypatch.setattr("api.ai.get_agent_runtime_service", lambda *_args, **_kwargs: runtime_service)
    yield runtime_service


def test_v2_create_and_get_thread():
    async def _run():
        created = await create_thread(
            ThreadCreateRequest(
                session_id="sess-v2-001",
                conversation_id="conv-v2-001",
                title="排查 query-service timeout",
            )
        )
        fetched = await get_thread(created["thread"]["thread_id"])
        return created, fetched

    created, fetched = asyncio.run(_run())
    assert created["thread"]["thread_id"].startswith("thr-")
    assert fetched["thread"]["session_id"] == "sess-v2-001"


def test_v2_targets_register_get_list_and_resolve():
    async def _run():
        registered = await register_target(
            TargetRegisterRequest(
                target_id="tgt-k8s-islap",
                target_kind="k8s_cluster",
                target_identity="namespace:islap",
                display_name="ISLAP Cluster",
                description="生产前验证集群",
                capabilities=["read_logs", "restart_workload"],
                updated_by="qa",
                reason="bootstrap",
            )
        )
        fetched = await get_target("tgt-k8s-islap")
        listed = await list_targets(status="active", target_kind="k8s_cluster", capability="restart_workload")
        resolved_allow = await resolve_target(
            "tgt-k8s-islap",
            TargetResolveRequest(required_capabilities=["read_logs", "restart_workload"]),
        )
        resolved_manual = await resolve_target(
            "tgt-k8s-islap",
            TargetResolveRequest(required_capabilities=["clickhouse_mutation"]),
        )
        resolved_unknown = await resolve_target(
            "tgt-unknown",
            TargetResolveRequest(required_capabilities=["read_logs"]),
        )
        return registered, fetched, listed, resolved_allow, resolved_manual, resolved_unknown

    registered, fetched, listed, resolved_allow, resolved_manual, resolved_unknown = asyncio.run(_run())
    assert registered["target"]["target_id"] == "tgt-k8s-islap"
    assert registered["target"]["version"] == 1
    assert fetched["target"]["target_identity"] == "namespace:islap"
    assert listed["targets"]
    assert listed["targets"][0]["target_id"] == "tgt-k8s-islap"
    assert resolved_allow["resolution"]["result"] == "allow"
    assert resolved_manual["resolution"]["result"] == "manual_required"
    assert resolved_manual["resolution"]["missing_capabilities"] == ["clickhouse_mutation"]
    assert resolved_unknown["resolution"]["result"] == "manual_required"
    assert resolved_unknown["resolution"]["registered"] is False


def test_v2_targets_deactivate_and_change_replay():
    async def _run():
        await register_target(
            TargetRegisterRequest(
                target_id="tgt-db-main",
                target_kind="clickhouse",
                target_identity="database:logs",
                capabilities=["read_logs", "run_query"],
                updated_by="qa",
                reason="init",
            )
        )
        await register_target(
            TargetRegisterRequest(
                target_id="tgt-db-main",
                target_kind="clickhouse",
                target_identity="database:logs",
                capabilities=["read_logs"],
                updated_by="qa",
                reason="remove mutation capability",
            )
        )
        deactivated = await deactivate_target(
            "tgt-db-main",
            TargetDeactivateRequest(updated_by="qa", reason="target retired"),
        )
        resolved = await resolve_target(
            "tgt-db-main",
            TargetResolveRequest(required_capabilities=["read_logs"]),
        )
        changes_all = await list_target_changes(after_seq=0, limit=20)
        changes_one = await list_target_changes(target_id="tgt-db-main", after_seq=1, limit=20)
        return deactivated, resolved, changes_all, changes_one

    deactivated, resolved, changes_all, changes_one = asyncio.run(_run())
    assert deactivated["target"]["status"] == "inactive"
    assert deactivated["target"]["version"] == 3
    assert resolved["resolution"]["result"] == "manual_required"
    assert changes_all["changes"]
    change_types = [item["change_type"] for item in changes_all["changes"]]
    assert change_types[:3] == ["target_registered", "target_updated", "target_deactivated"]
    assert changes_one["changes"]
    assert changes_one["changes"][0]["seq"] > 1


def test_v2_targets_resolve_by_identity():
    async def _run():
        await register_target(
            TargetRegisterRequest(
                target_id="tgt-host-primary",
                target_kind="host_node",
                target_identity="host:primary",
                capabilities=["read_host_state", "host_mutation"],
                metadata={
                    "cluster_id": "cluster-dev",
                    "node_name": "primary",
                    "preferred_executor_profiles": ["toolbox-node-readonly", "toolbox-node-mutating"],
                    "risk_tier": "high",
                },
                updated_by="qa",
                reason="bootstrap host target",
            )
        )
        resolved_allow = await resolve_target_by_identity(
            TargetResolveByIdentityRequest(
                target_kind="host_node",
                target_identity="host:primary",
                required_capabilities=["read_host_state"],
            )
        )
        resolved_manual = await resolve_target_by_identity(
            TargetResolveByIdentityRequest(
                target_kind="host_node",
                target_identity="host:primary",
                required_capabilities=["db_admin"],
            )
        )
        resolved_unknown = await resolve_target_by_identity(
            TargetResolveByIdentityRequest(
                target_kind="host_node",
                target_identity="host:unknown",
                required_capabilities=["read_host_state"],
            )
        )
        return resolved_allow, resolved_manual, resolved_unknown

    resolved_allow, resolved_manual, resolved_unknown = asyncio.run(_run())
    assert resolved_allow["resolution"]["result"] == "allow"
    assert resolved_allow["resolution"]["target_id"] == "tgt-host-primary"
    assert resolved_allow["resolution"]["target_identity"] == "host:primary"
    assert resolved_manual["resolution"]["result"] == "manual_required"
    assert resolved_manual["resolution"]["missing_capabilities"] == ["db_admin"]
    assert resolved_unknown["resolution"]["result"] == "manual_required"
    assert resolved_unknown["resolution"]["registered"] is False


def test_v2_targets_resolve_by_identity_host_missing_node_metadata_requires_manual():
    async def _run():
        await register_target(
            TargetRegisterRequest(
                target_id="tgt-host-without-node-meta",
                target_kind="host_node",
                target_identity="host:unknown",
                capabilities=["read_host_state"],
                metadata={
                    "cluster_id": "cluster-dev",
                    "preferred_executor_profiles": ["toolbox-node-readonly"],
                    "risk_tier": "high",
                },
                updated_by="qa",
                reason="bootstrap host target without node metadata",
            )
        )
        resolved = await resolve_target_by_identity(
            TargetResolveByIdentityRequest(
                target_kind="host_node",
                target_identity="host:unknown",
                required_capabilities=["read_host_state"],
            )
        )
        return resolved

    resolved = asyncio.run(_run())
    assert resolved["resolution"]["result"] == "manual_required"
    assert "metadata missing required fields" in str(resolved["resolution"]["reason"]).lower()
    contract = resolved["resolution"]["metadata_contract"]
    assert "node_name" in contract["missing_required_keys"]


def test_v2_targets_resolve_by_identity_ambiguous_requires_manual():
    async def _run():
        await register_target(
            TargetRegisterRequest(
                target_id="tgt-k8s-a",
                target_kind="k8s_cluster",
                target_identity="namespace:islap",
                capabilities=["read_logs"],
                updated_by="qa",
                reason="bootstrap A",
            )
        )
        await register_target(
            TargetRegisterRequest(
                target_id="tgt-k8s-b",
                target_kind="k8s_cluster",
                target_identity="namespace:islap",
                capabilities=["read_logs"],
                updated_by="qa",
                reason="bootstrap B",
            )
        )
        resolved = await resolve_target_by_identity(
            TargetResolveByIdentityRequest(
                target_kind="k8s_cluster",
                target_identity="namespace:islap",
                required_capabilities=["read_logs"],
            )
        )
        return resolved

    resolved = asyncio.run(_run())
    assert resolved["resolution"]["result"] == "manual_required"
    assert resolved["resolution"]["status"] == "ambiguous"
    assert resolved["resolution"]["registered"] is True
    assert resolved["resolution"]["ambiguous_targets"] == ["tgt-k8s-a", "tgt-k8s-b"]


def test_v2_create_run_maps_engine_and_thread(_reset_v4_state):
    runtime_service = _reset_v4_state

    async def _run():
        thread_payload = await create_thread(
            ThreadCreateRequest(
                session_id="sess-v2-run-001",
                conversation_id="conv-v2-run-001",
                title="v2 run thread",
            )
        )
        thread_id = thread_payload["thread"]["thread_id"]
        created = await create_thread_run(
            thread_id,
            RunCreateRequest(
                question="排查 query-service 5xx",
                analysis_context={"analysis_type": "log", "service_name": "query-service"},
                runtime_options={"mode": "followup_analysis"},
            ),
        )
        run_id = created["run"]["run_id"]
        fetched = await get_run_snapshot(run_id)
        return thread_id, created, fetched

    thread_id, created, fetched = asyncio.run(_run())

    assert created["workflow_id"].startswith("wf-")
    assert created["run"]["thread_id"] == thread_id
    assert created["run"]["engine"]["outer"].startswith("temporal")
    assert created["run"]["engine"]["inner"].startswith("langgraph")
    assert fetched["run"]["thread_id"] == thread_id
    assert runtime_service.get_run(created["run"]["run_id"]) is not None


def test_v2_create_run_fails_closed_when_temporal_required_unavailable(monkeypatch):
    monkeypatch.setenv("AI_RUNTIME_V4_OUTER_ENGINE", "temporal_required")
    monkeypatch.setattr(get_temporal_outer_client(), "_temporal_sdk_available", False, raising=False)

    async def _run():
        thread_payload = await create_thread(
            ThreadCreateRequest(
                session_id="sess-v2-temporal-required-001",
                conversation_id="conv-v2-temporal-required-001",
                title="v2 temporal required",
            )
        )
        thread_id = thread_payload["thread"]["thread_id"]
        await create_thread_run(
            thread_id,
            RunCreateRequest(
                question="排查 query-service 5xx",
                analysis_context={"analysis_type": "log", "service_name": "query-service"},
                runtime_options={},
            ),
        )

    with pytest.raises(HTTPException) as exc_info:
        asyncio.run(_run())
    assert exc_info.value.status_code == 503
    detail = exc_info.value.detail
    assert isinstance(detail, dict)
    assert detail["code"] == "runtime_outer_backend_unavailable"
    assert detail["message"] == "runtime v4 backend unavailable"
    assert detail["dependency"] == "temporal"
    assert "temporal_required" in detail["reason"]
    assert detail["attempt"] >= 1
    assert detail["max_attempts"] >= detail["attempt"]


def test_v2_create_run_fails_closed_when_langgraph_required_unavailable(monkeypatch):
    monkeypatch.setenv("AI_RUNTIME_V4_INNER_ENGINE", "langgraph_required")
    monkeypatch.setattr("ai.runtime_v4.langgraph.graph._langgraph_available", lambda: False)

    async def _run():
        thread_payload = await create_thread(
            ThreadCreateRequest(
                session_id="sess-v2-langgraph-required-001",
                conversation_id="conv-v2-langgraph-required-001",
                title="v2 langgraph required",
            )
        )
        thread_id = thread_payload["thread"]["thread_id"]
        await create_thread_run(
            thread_id,
            RunCreateRequest(
                question="排查 query-service 5xx",
                analysis_context={"analysis_type": "log", "service_name": "query-service"},
                runtime_options={},
            ),
        )

    with pytest.raises(HTTPException) as exc_info:
        asyncio.run(_run())
    assert exc_info.value.status_code == 503
    detail = exc_info.value.detail
    assert isinstance(detail, dict)
    assert detail["code"] == "runtime_outer_backend_unavailable"
    assert detail["message"] == "runtime v4 backend unavailable"
    assert detail["dependency"] == "langgraph"
    assert "langgraph_required" in detail["reason"]
    assert detail["attempt"] >= 1
    assert detail["max_attempts"] >= detail["attempt"]


def test_v2_create_run_retries_runtime_error_then_succeeds(monkeypatch):
    attempts = {"count": 0}
    monkeypatch.setenv("AI_RUNTIME_V2_CREATE_RUN_MAX_ATTEMPTS", "3")
    monkeypatch.setenv("AI_RUNTIME_V2_CREATE_RUN_RETRY_DELAYS_MS", "1,1")

    async def _fake_sleep(_seconds: float):
        return None

    async def _fake_create_run(_self, **_kwargs):
        attempts["count"] += 1
        if attempts["count"] < 3:
            raise RuntimeError("temporal transient unavailable")
        return {
            "workflow_id": "wf-retry-ok",
            "outer_engine": "temporal-v1",
            "inner_engine": "langgraph-v1",
            "run": {
                "run_id": "run-retry-ok",
                "status": "running",
            },
        }

    monkeypatch.setattr("api.ai_runtime_v2.asyncio.sleep", _fake_sleep)
    monkeypatch.setattr(
        "ai.runtime_v4.adapter.orchestration_bridge.RuntimeV4OrchestrationBridge.create_run",
        _fake_create_run,
    )

    async def _run():
        thread_payload = await create_thread(
            ThreadCreateRequest(
                session_id="sess-v2-retry-success-001",
                conversation_id="conv-v2-retry-success-001",
                title="v2 retry success",
            )
        )
        thread_id = thread_payload["thread"]["thread_id"]
        return await create_thread_run(
            thread_id,
            RunCreateRequest(
                question="重试成功",
                analysis_context={"analysis_type": "log", "service_name": "query-service"},
                runtime_options={},
            ),
        )

    created = asyncio.run(_run())
    assert attempts["count"] == 3
    assert created["workflow_id"] == "wf-retry-ok"
    assert created["run"]["run_id"] == "run-retry-ok"


def test_v2_create_run_reuses_idempotency_key_without_duplicate_create(monkeypatch):
    attempts = {"count": 0}
    from ai.runtime_v4.adapter.orchestration_bridge import RuntimeV4OrchestrationBridge

    original_create_run = RuntimeV4OrchestrationBridge.create_run

    async def _counted_create_run(self, **kwargs):
        attempts["count"] += 1
        return await original_create_run(self, **kwargs)

    monkeypatch.setattr(
        "ai.runtime_v4.adapter.orchestration_bridge.RuntimeV4OrchestrationBridge.create_run",
        _counted_create_run,
    )

    async def _run():
        thread_payload = await create_thread(
            ThreadCreateRequest(
                session_id="sess-v2-idempotency-001",
                conversation_id="conv-v2-idempotency-001",
                title="v2 idempotency",
            )
        )
        thread_id = thread_payload["thread"]["thread_id"]
        first = await create_thread_run(
            thread_id,
            RunCreateRequest(
                question="幂等创建 run",
                analysis_context={"analysis_type": "log", "service_name": "query-service"},
                runtime_options={},
                idempotency_key="idem-key-v2-001",
            ),
        )
        second = await create_thread_run(
            thread_id,
            RunCreateRequest(
                question="幂等创建 run",
                analysis_context={"analysis_type": "log", "service_name": "query-service"},
                runtime_options={},
                idempotency_key="idem-key-v2-001",
            ),
        )
        return first, second

    first, second = asyncio.run(_run())
    assert attempts["count"] == 1
    assert first["run"]["run_id"] == second["run"]["run_id"]
    assert first["idempotent_reused"] is False
    assert second["idempotent_reused"] is True


def test_v2_create_run_returns_structured_503_after_retry_exhausted(monkeypatch):
    monkeypatch.setenv("AI_RUNTIME_V2_CREATE_RUN_MAX_ATTEMPTS", "2")
    monkeypatch.setenv("AI_RUNTIME_V2_CREATE_RUN_RETRY_DELAYS_MS", "1")

    async def _fake_sleep(_seconds: float):
        return None

    async def _always_fail_create_run(_self, **_kwargs):
        raise RuntimeError("temporal_required connect failed: dial tcp timeout")

    monkeypatch.setattr("api.ai_runtime_v2.asyncio.sleep", _fake_sleep)
    monkeypatch.setattr(
        "ai.runtime_v4.adapter.orchestration_bridge.RuntimeV4OrchestrationBridge.create_run",
        _always_fail_create_run,
    )

    async def _run():
        thread_payload = await create_thread(
            ThreadCreateRequest(
                session_id="sess-v2-retry-fail-001",
                conversation_id="conv-v2-retry-fail-001",
                title="v2 retry fail",
            )
        )
        thread_id = thread_payload["thread"]["thread_id"]
        await create_thread_run(
            thread_id,
            RunCreateRequest(
                question="重试失败",
                analysis_context={"analysis_type": "log", "service_name": "query-service"},
                runtime_options={},
            ),
        )

    with pytest.raises(HTTPException) as exc_info:
        asyncio.run(_run())

    assert exc_info.value.status_code == 503
    detail = exc_info.value.detail
    assert isinstance(detail, dict)
    assert detail["code"] == "runtime_outer_backend_unavailable"
    assert detail["message"] == "runtime v4 backend unavailable"
    assert detail["dependency"] == "temporal"
    assert detail["attempt"] == 2
    assert detail["max_attempts"] == 2
    assert detail["retry_after_s"] >= 1
    assert "temporal_required" in detail["reason"]


def test_v2_get_thread_includes_latest_run():
    async def _run():
        thread_payload = await create_thread(
            ThreadCreateRequest(
                session_id="sess-v2-thread-latest-001",
                conversation_id="conv-v2-thread-latest-001",
                title="v2 thread latest run",
            )
        )
        thread_id = thread_payload["thread"]["thread_id"]
        await create_thread_run(
            thread_id,
            RunCreateRequest(
                question="首次分析",
                analysis_context={"analysis_type": "log", "service_name": "query-service"},
                runtime_options={},
            ),
        )
        second = await create_thread_run(
            thread_id,
            RunCreateRequest(
                question="再次分析",
                analysis_context={"analysis_type": "log", "service_name": "query-service"},
                runtime_options={},
            ),
        )
        fetched = await get_thread(thread_id)
        return second, fetched

    second, fetched = asyncio.run(_run())
    assert fetched["latest_run"] is not None
    assert fetched["latest_run"]["run_id"] == second["run"]["run_id"]


def test_v2_list_thread_runs_returns_mapped_snapshots():
    async def _run():
        thread_payload = await create_thread(
            ThreadCreateRequest(
                session_id="sess-v2-list-runs-001",
                conversation_id="conv-v2-list-runs-001",
                title="v2 list runs",
            )
        )
        thread_id = thread_payload["thread"]["thread_id"]
        created_ids = []
        for index in range(3):
            created = await create_thread_run(
                thread_id,
                RunCreateRequest(
                    question=f"run-{index}",
                    analysis_context={"analysis_type": "log", "service_name": "query-service"},
                    runtime_options={},
                ),
            )
            created_ids.append(created["run"]["run_id"])
        listed = await list_thread_runs(thread_id, after=1, limit=2)
        return created_ids, listed

    created_ids, listed = asyncio.run(_run())
    assert listed["thread_id"].startswith("thr-")
    assert listed["total"] == 3
    assert listed["next_after"] == 3
    listed_run_ids = [item["run_id"] for item in listed["runs"]]
    assert listed_run_ids == created_ids[1:]


def test_v2_approval_and_interrupt_signal_path(_reset_v4_state):
    runtime_service = _reset_v4_state

    async def _run():
        thread_payload = await create_thread(
            ThreadCreateRequest(
                session_id="sess-v2-signal-001",
                conversation_id="conv-v2-signal-001",
                title="v2 signal thread",
            )
        )
        thread_id = thread_payload["thread"]["thread_id"]
        run_created = await create_thread_run(
            thread_id,
            RunCreateRequest(
                question="执行命令前需要审批",
                analysis_context={"analysis_type": "log", "service_name": "query-service"},
                runtime_options={},
            ),
        )
        run_id = run_created["run"]["run_id"]

        runtime_service.request_approval(
            run_id,
            approval_id="apr-v2-001",
            title="执行写命令",
            reason="需要审批",
            command="kubectl rollout restart deployment/query-service",
            requires_confirmation=True,
            requires_elevation=True,
        )
        approved = await resolve_approval(
            run_id,
            "apr-v2-001",
            ApprovalResolveRequest(
                decision="approved",
                comment="允许",
                confirmed=True,
                elevated=True,
            ),
        )
        interrupted = await interrupt_run(run_id, InterruptRequest(reason="manual stop"))
        return approved, interrupted

    approved, interrupted = asyncio.run(_run())
    assert approved["approval"]["decision"] == "approved"
    assert interrupted["run"]["status"] == "cancelled"


def test_v2_cancel_run_delegates_to_bridge():
    async def _run():
        thread_payload = await create_thread(
            ThreadCreateRequest(
                session_id="sess-v2-cancel-001",
                conversation_id="conv-v2-cancel-001",
                title="v2 cancel route",
            )
        )
        thread_id = thread_payload["thread"]["thread_id"]
        run_created = await create_thread_run(
            thread_id,
            RunCreateRequest(
                question="取消运行",
                analysis_context={"analysis_type": "log", "service_name": "query-service"},
                runtime_options={},
            ),
        )
        run_id = run_created["run"]["run_id"]
        return await cancel_run(run_id, CancelRequest(reason="user_cancelled"))

    cancelled = asyncio.run(_run())
    assert cancelled["run"]["status"] == "cancelled"


def test_v2_list_run_approvals_contains_pending_and_resolved(_reset_v4_state):
    runtime_service = _reset_v4_state

    async def _run():
        thread_payload = await create_thread(
            ThreadCreateRequest(
                session_id="sess-v2-approvals-001",
                conversation_id="conv-v2-approvals-001",
                title="v2 approvals",
            )
        )
        thread_id = thread_payload["thread"]["thread_id"]
        run_created = await create_thread_run(
            thread_id,
            RunCreateRequest(
                question="审批流验证",
                analysis_context={"analysis_type": "log", "service_name": "query-service"},
                runtime_options={},
            ),
        )
        run_id = run_created["run"]["run_id"]
        runtime_service.request_approval(
            run_id,
            approval_id="apr-v2-list-001",
            title="执行写命令",
            reason="需要审批",
            command="kubectl rollout restart deployment/query-service",
            requires_confirmation=True,
            requires_elevation=True,
        )
        pending = await list_run_approvals(run_id)
        await resolve_approval(
            run_id,
            "apr-v2-list-001",
            ApprovalResolveRequest(
                decision="approved",
                comment="允许",
                confirmed=True,
                elevated=True,
            ),
        )
        resolved = await list_run_approvals(run_id)
        return pending, resolved

    pending, resolved = asyncio.run(_run())
    assert pending["approvals"]
    assert pending["approvals"][0]["approval_id"] == "apr-v2-list-001"
    assert pending["approvals"][0]["status"] == "pending"
    assert resolved["approvals"]
    assert resolved["approvals"][0]["approval_id"] == "apr-v2-list-001"
    assert resolved["approvals"][0]["status"] == "approved"


def test_v2_list_run_actions_contains_waiting_approval_action(_reset_v4_state):
    runtime_service = _reset_v4_state

    async def _run():
        thread_payload = await create_thread(
            ThreadCreateRequest(
                session_id="sess-v2-actions-approval-001",
                conversation_id="conv-v2-actions-approval-001",
                title="v2 actions approval",
            )
        )
        thread_id = thread_payload["thread"]["thread_id"]
        run_created = await create_thread_run(
            thread_id,
            RunCreateRequest(
                question="动作查询接口验证",
                analysis_context={"analysis_type": "log", "service_name": "query-service"},
                runtime_options={},
            ),
        )
        run_id = run_created["run"]["run_id"]
        runtime_service.request_approval(
            run_id,
            approval_id="apr-v2-action-001",
            title="执行写命令",
            reason="需要审批",
            command="kubectl rollout restart deployment/query-service",
            purpose="恢复 query-service",
            requires_confirmation=True,
            requires_elevation=True,
        )
        return await list_run_actions(run_id)

    result = asyncio.run(_run())
    assert result["actions"]
    assert result["actions"][0]["status"] == "waiting_approval"
    assert result["actions"][0]["approval_id"] == "apr-v2-action-001"


def test_v2_list_run_actions_ignores_non_action_events(_reset_v4_state):
    runtime_service = _reset_v4_state

    async def _run():
        thread_payload = await create_thread(
            ThreadCreateRequest(
                session_id="sess-v2-actions-non-action-001",
                conversation_id="conv-v2-actions-non-action-001",
                title="v2 actions non action",
            )
        )
        thread_id = thread_payload["thread"]["thread_id"]
        run_created = await create_thread_run(
            thread_id,
            RunCreateRequest(
                question="仅查看推理事件，不应生成动作",
                analysis_context={"analysis_type": "log", "service_name": "query-service"},
                runtime_options={},
            ),
        )
        run_id = run_created["run"]["run_id"]
        runtime_service.append_event(
            run_id,
            event_protocol.REASONING_STEP,
            {
                "step_id": "plan-1",
                "phase": "planning",
                "title": "初始化运行上下文",
                "status": "completed",
            },
        )
        runtime_service.append_event(
            run_id,
            event_protocol.RUN_STATUS_CHANGED,
            {
                "status": "running",
                "current_phase": "planning",
            },
        )
        return await list_run_actions(run_id)

    result = asyncio.run(_run())
    assert result["actions"] == []


def test_v2_list_run_actions_contains_tool_call_finished(_reset_v4_state):
    runtime_service = _reset_v4_state

    async def _run():
        thread_payload = await create_thread(
            ThreadCreateRequest(
                session_id="sess-v2-actions-finished-001",
                conversation_id="conv-v2-actions-finished-001",
                title="v2 actions finished",
            )
        )
        thread_id = thread_payload["thread"]["thread_id"]
        run_created = await create_thread_run(
            thread_id,
            RunCreateRequest(
                question="工具动作完成态查询",
                analysis_context={"analysis_type": "log", "service_name": "query-service"},
                runtime_options={},
            ),
        )
        run_id = run_created["run"]["run_id"]
        runtime_service.append_event(
            run_id,
            event_protocol.TOOL_CALL_STARTED,
            {
                "action_id": "act-v2-tool-001",
                "tool_call_id": "tool-v2-tool-001",
                "command_run_id": "cmdrun-v2-tool-001",
                "command": "kubectl get pods -n islap",
                "purpose": "检查 pod 状态",
                "title": "检查 pod",
                "status": "running",
                "executor_profile": "toolbox-k8s-readonly",
                "target_kind": "k8s_cluster",
                "target_identity": "namespace:islap",
                "approval_policy": "auto_execute",
            },
        )
        runtime_service.append_event(
            run_id,
            event_protocol.TOOL_CALL_FINISHED,
            {
                "action_id": "act-v2-tool-001",
                "tool_call_id": "tool-v2-tool-001",
                "command_run_id": "cmdrun-v2-tool-001",
                "command": "kubectl get pods -n islap",
                "purpose": "检查 pod 状态",
                "title": "检查 pod",
                "status": "completed",
                "exit_code": 0,
                "duration_ms": 18,
                "timed_out": False,
                "reason_code": "backend_unready",
            },
        )
        return await list_run_actions(run_id)

    result = asyncio.run(_run())
    assert result["actions"]
    action = result["actions"][0]
    assert action["action_id"] == "act-v2-tool-001"
    assert action["status"] == "completed"
    assert action["command_run_id"] == "cmdrun-v2-tool-001"
    assert action["exit_code"] == 0
    assert action["reason_code"] == "backend_unready"


def test_v2_list_run_actions_contains_tool_call_skipped_duplicate(_reset_v4_state):
    runtime_service = _reset_v4_state

    async def _run():
        thread_payload = await create_thread(
            ThreadCreateRequest(
                session_id="sess-v2-actions-skip-dup-001",
                conversation_id="conv-v2-actions-skip-dup-001",
                title="v2 actions skipped duplicate",
            )
        )
        thread_id = thread_payload["thread"]["thread_id"]
        run_created = await create_thread_run(
            thread_id,
            RunCreateRequest(
                question="重复命令跳过事件",
                analysis_context={"analysis_type": "log", "service_name": "query-service"},
                runtime_options={},
            ),
        )
        run_id = run_created["run"]["run_id"]
        runtime_service.append_event(
            run_id,
            event_protocol.TOOL_CALL_SKIPPED_DUPLICATE,
            {
                "action_id": "act-v2-skip-dup-001",
                "tool_call_id": "tool-v2-skip-dup-001",
                "command_run_id": "cmdrun-v2-skip-dup-001",
                "command": "kubectl get pods -n islap",
                "purpose": "检查 pod 状态",
                "title": "检查 pod",
                "status": "skipped_duplicate",
                "reason_code": "duplicate_skipped",
                "message": "同一 run 已执行过该命令，跳过重复执行。",
                "evidence_slot_id": "action:act-v2-skip-dup-001",
                "evidence_outcome": "reused",
                "evidence_reuse": True,
                "reused_evidence_ids": ["cmdrun-v2-skip-dup-001"],
                "info_gain_score": 0.41,
            },
        )
        return await list_run_actions(run_id)

    result = asyncio.run(_run())
    assert result["actions"]
    action = result["actions"][0]
    assert action["action_id"] == "act-v2-skip-dup-001"
    assert action["status"] == "skipped_duplicate"
    assert action["command_run_id"] == "cmdrun-v2-skip-dup-001"
    assert action["reason_code"] == "duplicate_skipped"
    assert action["evidence_slot_id"] == "action:act-v2-skip-dup-001"
    assert action["evidence_outcome"] == "reused"
    assert action["evidence_reuse"] is True
    assert action["reused_evidence_ids"] == ["cmdrun-v2-skip-dup-001"]
    assert float(action["info_gain_score"]) == pytest.approx(0.41, rel=1e-3)


def test_v2_command_action_delegates_to_bridge(monkeypatch):
    async def _run():
        thread_payload = await create_thread(
            ThreadCreateRequest(
                session_id="sess-v2-command-001",
                conversation_id="conv-v2-command-001",
                title="v2 command action",
            )
        )
        thread_id = thread_payload["thread"]["thread_id"]
        run_created = await create_thread_run(
            thread_id,
            RunCreateRequest(
                question="执行只读命令",
                analysis_context={"analysis_type": "log", "service_name": "query-service"},
                runtime_options={},
            ),
        )
        run_id = run_created["run"]["run_id"]

        async def _fake_execute_command(_self, *, run_id, request_payload):
            assert run_id
            assert request_payload["purpose"] == "检查 query-service pod 状态"
            return {
                "status": "completed",
                "tool_call_id": "tool-v2-001",
                "run": {
                    "run_id": run_id,
                    "status": "running",
                },
            }

        monkeypatch.setattr(
            "ai.runtime_v4.adapter.orchestration_bridge.RuntimeV4OrchestrationBridge.execute_command",
            _fake_execute_command,
        )

        return await execute_run_command_action(
            run_id,
            CommandActionRequest(
                action_id="act-v2-command-001",
                command="kubectl get pods -n islap",
                purpose="检查 query-service pod 状态",
                title="检查 pod",
                tool_name="command.exec",
                diagnosis_contract={},
                command_spec={
                    "tool": "generic_exec",
                    "args": {
                        "command": "kubectl get pods -n islap",
                        "target_kind": "k8s_cluster",
                        "target_identity": "namespace:islap",
                        "timeout_s": 20,
                    },
                    "target_kind": "k8s_cluster",
                    "target_identity": "namespace:islap",
                },
                confirmed=False,
                elevated=False,
                timeout_seconds=20,
            ),
        )

    result = asyncio.run(_run())
    assert result["status"] == "completed"
    assert result["tool_call_id"] == "tool-v2-001"


def test_v2_command_action_auto_builds_command_spec_when_missing(monkeypatch):
    async def _run():
        thread_payload = await create_thread(
            ThreadCreateRequest(
                session_id="sess-v2-command-002",
                conversation_id="conv-v2-command-002",
                title="v2 command action auto build command spec",
            )
        )
        thread_id = thread_payload["thread"]["thread_id"]
        run_created = await create_thread_run(
            thread_id,
            RunCreateRequest(
                question="执行只读命令",
                analysis_context={"analysis_type": "log", "service_name": "query-service"},
                runtime_options={},
            ),
        )
        run_id = run_created["run"]["run_id"]

        async def _fake_execute_command(_self, *, run_id, request_payload):
            assert run_id
            payload_spec = request_payload["command_spec"]
            assert payload_spec["tool"] == "generic_exec"
            assert payload_spec["args"]["command"] == "kubectl get pods -n islap"
            assert payload_spec["args"]["timeout_s"] == 20
            assert payload_spec["purpose"] == "检查 query-service pod 状态"
            return {
                "status": "completed",
                "tool_call_id": "tool-v2-002",
                "run": {
                    "run_id": run_id,
                    "status": "running",
                },
            }

        monkeypatch.setattr(
            "ai.runtime_v4.adapter.orchestration_bridge.RuntimeV4OrchestrationBridge.execute_command",
            _fake_execute_command,
        )

        return await execute_run_command_action(
            run_id,
            CommandActionRequest(
                action_id="act-v2-command-002",
                command="kubectl get pods -n islap",
                purpose="检查 query-service pod 状态",
                title="检查 pod",
                tool_name="command.exec",
                diagnosis_contract={},
                command_spec={},
                confirmed=False,
                elevated=False,
                timeout_seconds=20,
            ),
        )

    result = asyncio.run(_run())
    assert result["status"] == "completed"
    assert result["tool_call_id"] == "tool-v2-002"


def test_v2_command_action_requires_command_or_command_spec():
    async def _run():
        thread_payload = await create_thread(
            ThreadCreateRequest(
                session_id="sess-v2-command-003",
                conversation_id="conv-v2-command-003",
                title="v2 command action requires command or command_spec",
            )
        )
        thread_id = thread_payload["thread"]["thread_id"]
        run_created = await create_thread_run(
            thread_id,
            RunCreateRequest(
                question="执行只读命令",
                analysis_context={"analysis_type": "log", "service_name": "query-service"},
                runtime_options={},
            ),
        )
        run_id = run_created["run"]["run_id"]
        with pytest.raises(HTTPException) as exc_info:
            await execute_run_command_action(
                run_id,
                CommandActionRequest(
                    action_id="act-v2-command-003",
                    command="",
                    purpose="检查 query-service pod 状态",
                    title="检查 pod",
                    tool_name="command.exec",
                    diagnosis_contract={},
                    command_spec={},
                    confirmed=False,
                    elevated=False,
                    timeout_seconds=20,
                ),
            )
        return exc_info.value

    error = asyncio.run(_run())
    assert error.status_code == 400
    assert error.detail == "command_spec is required"


def test_v2_get_run_events_default_visibility_hides_repair_noise(_reset_v4_state):
    runtime_service = _reset_v4_state

    async def _run():
        thread_payload = await create_thread(
            ThreadCreateRequest(
                session_id="sess-v2-visibility-001",
                conversation_id="conv-v2-visibility-001",
                title="v2 visibility projection",
            )
        )
        thread_id = thread_payload["thread"]["thread_id"]
        run_created = await create_thread_run(
            thread_id,
            RunCreateRequest(
                question="检查默认视图事件过滤",
                analysis_context={"analysis_type": "log", "service_name": "query-service"},
                runtime_options={},
            ),
        )
        run_id = run_created["run"]["run_id"]
        runtime_service.append_event(
            run_id,
            "action_execution_retrying",
            {
                "action_id": "act-v2-visibility-001",
                "command": "kubectl get pods -n islap",
                "attempt": 1,
                "max_attempts": 3,
                "message": "retrying after timeout",
            },
        )
        runtime_service.append_event(
            run_id,
            "action_recovery_succeeded",
            {
                "action_id": "act-v2-visibility-001",
                "command": "kubectl get pods -n islap",
                "recovery_kind": "structured_command_recompiled",
            },
        )
        runtime_service.append_event(
            run_id,
            event_protocol.TOOL_CALL_FINISHED,
            {
                "action_id": "act-v2-visibility-001",
                "tool_call_id": "tool-v2-visibility-001",
                "command_run_id": "cmdrun-v2-visibility-001",
                "command": "kubectl get pods -n islap",
                "purpose": "检查 pod 状态",
                "title": "检查 pod",
                "status": "completed",
                "exit_code": 0,
                "duration_ms": 12,
                "timed_out": False,
            },
        )
        default_payload = await get_run_events(run_id, after_seq=0, limit=200, visibility="default")
        debug_payload = await get_run_events(run_id, after_seq=0, limit=200, visibility="debug")
        return default_payload, debug_payload

    default_payload, debug_payload = asyncio.run(_run())
    default_types = [str(item.get("event_type")) for item in default_payload.get("events", [])]
    debug_types = [str(item.get("event_type")) for item in debug_payload.get("events", [])]
    assert "action_execution_retrying" not in default_types
    assert "action_recovery_succeeded" not in default_types
    assert "action_execution_retrying" in debug_types
    assert "action_recovery_succeeded" in debug_types
    assert "tool_call_finished" in default_types
    assert int(default_payload["next_after_seq"]) == int(debug_payload["next_after_seq"])


def test_v2_get_run_events_delegates_visibility_to_bridge(monkeypatch):
    async def _fake_get_run_events(_self, run_id, *, after_seq, limit, visibility):
        assert run_id == "run-v2-events-bridge-001"
        assert after_seq == 3
        assert limit == 42
        assert visibility == "debug"
        return {"run_id": run_id, "next_after_seq": 5, "events": []}

    monkeypatch.setattr(
        "ai.runtime_v4.adapter.orchestration_bridge.RuntimeV4OrchestrationBridge.get_run_events",
        _fake_get_run_events,
    )

    async def _run():
        return await get_run_events("run-v2-events-bridge-001", after_seq=3, limit=42, visibility="debug")

    payload = asyncio.run(_run())
    assert payload["run_id"] == "run-v2-events-bridge-001"
    assert payload["next_after_seq"] == 5


def test_v2_event_stream_delegates_to_bridge(monkeypatch):
    class DummyStreamResult:
        def __init__(self):
            self.kind = "stream"

    dummy_result = DummyStreamResult()

    async def _fake_stream_run(_self, run_id, *, after_seq, visibility):
        assert run_id == "run-v2-stream-001"
        assert after_seq == 7
        assert visibility == "default"
        return dummy_result

    monkeypatch.setattr(
        "ai.runtime_v4.adapter.orchestration_bridge.RuntimeV4OrchestrationBridge.stream_run",
        _fake_stream_run,
    )

    async def _run():
        return await stream_run_events("run-v2-stream-001", after_seq=7)

    result = asyncio.run(_run())
    assert result is dummy_result
