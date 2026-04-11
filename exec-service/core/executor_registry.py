"""
Executor template registry and resolution helpers.
"""

from __future__ import annotations

import json
import os
import re
import shlex
from typing import Any, Dict, List, Optional


EXECUTOR_PROFILE_CATALOG: List[Dict[str, str]] = [
    {
        "executor_type": "sandbox_pod",
        "executor_profile": "busybox-readonly",
        "target_kind": "runtime_workspace",
        "target_identity": "workspace:local",
        "rollout_stage": "phase-2",
        "summary": "轻量本地诊断命令",
        "example_template": 'python3 -c "import sys; print(sys.argv[1])" {command_quoted}',
    },
    {
        "executor_type": "privileged_sandbox_pod",
        "executor_profile": "busybox-mutating",
        "target_kind": "runtime_workspace",
        "target_identity": "workspace:local",
        "rollout_stage": "phase-2",
        "summary": "工作区内高风险变更命令",
        "example_template": 'python3 -c "import sys; print(sys.argv[1])" {command_quoted}',
    },
    {
        "executor_type": "sandbox_pod",
        "executor_profile": "toolbox-k8s-readonly",
        "target_kind": "k8s_cluster",
        "target_identity": "namespace:default",
        "rollout_stage": "phase-3",
        "summary": "Kubernetes 只读排查命令",
        "example_template": "curl -sS -X POST <toolbox-gateway-url>/exec -d command={command_quoted}",
    },
    {
        "executor_type": "privileged_sandbox_pod",
        "executor_profile": "toolbox-k8s-mutating",
        "target_kind": "k8s_cluster",
        "target_identity": "namespace:default",
        "rollout_stage": "phase-3",
        "summary": "Kubernetes 变更与恢复命令",
        "example_template": "curl -sS -X POST <toolbox-gateway-url>/exec -d command={command_quoted}",
    },
    {
        "executor_type": "sandbox_pod",
        "executor_profile": "toolbox-clickhouse-readonly",
        "target_kind": "clickhouse_cluster",
        "target_identity": "database:default",
        "rollout_stage": "phase-3",
        "summary": "ClickHouse 只读查询",
        "example_template": "curl -sS -X POST <db-toolbox-url>/exec -d command={command_quoted}",
    },
    {
        "executor_type": "privileged_sandbox_pod",
        "executor_profile": "toolbox-clickhouse-mutating",
        "target_kind": "clickhouse_cluster",
        "target_identity": "database:default",
        "rollout_stage": "phase-3",
        "summary": "ClickHouse 高风险变更",
        "example_template": "curl -sS -X POST <db-toolbox-url>/exec -d command={command_quoted}",
    },
    {
        "executor_type": "sandbox_pod",
        "executor_profile": "toolbox-openstack-readonly",
        "target_kind": "openstack_project",
        "target_identity": "project:default",
        "rollout_stage": "phase-5",
        "summary": "OpenStack 只读控制面查询",
        "example_template": "curl -sS -X POST <openstack-executor-url>/exec -d command={command_quoted}",
    },
    {
        "executor_type": "privileged_sandbox_pod",
        "executor_profile": "toolbox-openstack-mutating",
        "target_kind": "openstack_project",
        "target_identity": "project:default",
        "rollout_stage": "phase-5",
        "summary": "OpenStack 高风险控制面命令",
        "example_template": "curl -sS -X POST <openstack-executor-url>/exec -d command={command_quoted}",
    },
    {
        "executor_type": "sandbox_pod",
        "executor_profile": "toolbox-postgres-readonly",
        "target_kind": "postgres_cluster",
        "target_identity": "database:default",
        "rollout_stage": "phase-5",
        "summary": "PostgreSQL 只读查询",
        "example_template": "curl -sS -X POST <postgres-executor-url>/exec -d command={command_quoted}",
    },
    {
        "executor_type": "privileged_sandbox_pod",
        "executor_profile": "toolbox-postgres-mutating",
        "target_kind": "postgres_cluster",
        "target_identity": "database:default",
        "rollout_stage": "phase-5",
        "summary": "PostgreSQL 变更命令",
        "example_template": "curl -sS -X POST <postgres-executor-url>/exec -d command={command_quoted}",
    },
    {
        "executor_type": "sandbox_pod",
        "executor_profile": "toolbox-mysql-readonly",
        "target_kind": "mysql_cluster",
        "target_identity": "database:default",
        "rollout_stage": "phase-5",
        "summary": "MySQL 只读查询",
        "example_template": "curl -sS -X POST <mysql-executor-url>/exec -d command={command_quoted}",
    },
    {
        "executor_type": "privileged_sandbox_pod",
        "executor_profile": "toolbox-mysql-mutating",
        "target_kind": "mysql_cluster",
        "target_identity": "database:default",
        "rollout_stage": "phase-5",
        "summary": "MySQL 变更命令",
        "example_template": "curl -sS -X POST <mysql-executor-url>/exec -d command={command_quoted}",
    },
    {
        "executor_type": "ssh_gateway",
        "executor_profile": "host-ssh-readonly",
        "target_kind": "host_node",
        "target_identity": "host:primary",
        "rollout_stage": "phase-4",
        "summary": "主机级只读检查命令",
        "example_template": "curl -sS -X POST <ssh-gateway-url>/exec -d command={command_quoted} -d target={target_identity_quoted}",
    },
    {
        "executor_type": "ssh_gateway",
        "executor_profile": "host-ssh-mutating",
        "target_kind": "host_node",
        "target_identity": "host:primary",
        "rollout_stage": "phase-4",
        "summary": "主机级高风险变更命令",
        "example_template": "curl -sS -X POST <ssh-gateway-url>/exec -d command={command_quoted} -d target={target_identity_quoted}",
    },
    {
        "executor_type": "sandbox_pod",
        "executor_profile": "toolbox-node-readonly",
        "target_kind": "host_node",
        "target_identity": "host:primary",
        "rollout_stage": "phase-4",
        "summary": "节点级只读主机诊断命令",
        "example_template": "curl -sS -X POST <toolbox-node-gateway-url>/exec -d command={command_quoted} -d target={target_identity_quoted}",
    },
    {
        "executor_type": "privileged_sandbox_pod",
        "executor_profile": "toolbox-node-mutating",
        "target_kind": "host_node",
        "target_identity": "host:primary",
        "rollout_stage": "phase-4",
        "summary": "节点级高风险主机变更命令",
        "example_template": "curl -sS -X POST <toolbox-node-gateway-url>/exec -d command={command_quoted} -d target={target_identity_quoted}",
    },
    {
        "executor_type": "sandbox_pod",
        "executor_profile": "toolbox-http-readonly",
        "target_kind": "http_endpoint",
        "target_identity": "http:unknown",
        "rollout_stage": "phase-3",
        "summary": "HTTP 只读探测请求",
        "example_template": "curl -sS -X POST <http-proxy-url>/exec -d command={command_quoted}",
    },
    {
        "executor_type": "external_control_plane",
        "executor_profile": "toolbox-http-mutating",
        "target_kind": "http_endpoint",
        "target_identity": "http:unknown",
        "rollout_stage": "phase-5",
        "summary": "HTTP 变更请求与外部控制面调用",
        "example_template": "curl -sS -X POST <http-proxy-url>/exec -d command={command_quoted}",
    },
]


def as_str(value: Any, default: str = "") -> str:
    if value is None:
        return default
    if isinstance(value, str):
        return value
    return str(value)


def _safe_dict(value: Any) -> Dict[str, Any]:
    return value if isinstance(value, dict) else {}


def _safe_list(value: Any) -> List[Any]:
    return value if isinstance(value, list) else []


def _is_unknown_token(value: Any) -> bool:
    text = as_str(value).strip().lower()
    if not text:
        return True
    if text in {"unknown", "n/a", "na", "none", "null", "unset"}:
        return True
    return text.endswith(":unknown")


def _is_mutating_profile(profile: str) -> bool:
    lowered = as_str(profile).strip().lower()
    return any(token in lowered for token in ("mutating", "mutation", "write", "privileged"))


def _to_json_text(value: Any) -> str:
    try:
        return json.dumps(value if isinstance(value, dict) else {}, ensure_ascii=False, sort_keys=True)
    except Exception:
        return "{}"


def _normalize_profiles(value: Any) -> List[str]:
    normalized: List[str] = []
    seen = set()
    for item in _safe_list(value):
        profile = as_str(item).strip().lower()
        if not profile or profile in seen:
            continue
        seen.add(profile)
        normalized.append(profile)
    return normalized


def _extract_execution_scope(
    *,
    resolved_target_context: Dict[str, Any],
    target_identity: str,
) -> Dict[str, str]:
    safe_context = _safe_dict(resolved_target_context)
    metadata = _safe_dict(safe_context.get("metadata"))
    scope = _safe_dict(safe_context.get("execution_scope"))
    identity = as_str(scope.get("target_identity"), as_str(target_identity)).strip()
    node_name = as_str(scope.get("node_name"), as_str(metadata.get("node_name"))).strip()
    if _is_unknown_token(node_name) and identity.lower().startswith("host:"):
        node_name = as_str(identity.split(":", 1)[1]).strip()
    cluster_id = as_str(scope.get("cluster_id"), as_str(metadata.get("cluster_id"))).strip()
    namespace = as_str(scope.get("namespace"), as_str(metadata.get("namespace"))).strip()
    return {
        "cluster_id": cluster_id,
        "namespace": namespace,
        "node_name": node_name,
        "target_kind": as_str(scope.get("target_kind"), as_str(safe_context.get("target_kind"))).strip(),
        "target_identity": identity,
        "target_id": as_str(safe_context.get("target_id")).strip(),
    }


def _select_effective_profile(
    *,
    requested_profile: str,
    resolved_target_context: Dict[str, Any],
) -> str:
    metadata = _safe_dict(_safe_dict(resolved_target_context).get("metadata"))
    preferred = _normalize_profiles(metadata.get("preferred_executor_profiles"))
    safe_requested = as_str(requested_profile).strip().lower()
    if not preferred:
        return safe_requested
    if safe_requested and safe_requested in preferred:
        return safe_requested
    requested_mutating = _is_mutating_profile(safe_requested)
    for profile in preferred:
        if _is_mutating_profile(profile) == requested_mutating:
            return profile
    return preferred[0]


def _catalog_executor_type_for_profile(
    *,
    executor_profile: str,
    target_kind: str,
    fallback_executor_type: str,
) -> str:
    safe_profile = as_str(executor_profile).strip().lower()
    safe_kind = as_str(target_kind).strip().lower()
    for item in EXECUTOR_PROFILE_CATALOG:
        item_profile = as_str(item.get("executor_profile")).strip().lower()
        item_kind = as_str(item.get("target_kind")).strip().lower()
        if item_profile != safe_profile:
            continue
        if safe_kind and item_kind and item_kind != safe_kind:
            continue
        return as_str(item.get("executor_type"), fallback_executor_type)
    return as_str(fallback_executor_type)


def _allow_local_process_executor() -> bool:
    raw = as_str(os.getenv("EXEC_ALLOW_LOCAL_PROCESS_EXECUTOR")).strip().lower()
    return raw in {"1", "true", "yes", "on"}


def _normalize_key(value: Any) -> str:
    normalized = re.sub(r"[^a-z0-9]+", "_", as_str(value).strip().lower())
    normalized = normalized.strip("_")
    return normalized or "default"


def executor_template_env_names(executor_type: str, executor_profile: str) -> List[str]:
    safe_executor_type = as_str(executor_type, "local_process")
    safe_executor_profile = as_str(executor_profile, "local-default")
    return [
        f"EXEC_EXECUTOR_TEMPLATE__{_normalize_key(safe_executor_profile).upper()}",
        f"EXEC_EXECUTOR_TEMPLATE__{_normalize_key(safe_executor_type).upper()}",
    ]


def _template_context(
    *,
    command: str,
    executor_type: str,
    executor_profile: str,
    target_kind: str,
    target_identity: str,
    target_cluster_id: str = "",
    target_namespace: str = "",
    target_node_name: str = "",
    resolved_target_context: Optional[Dict[str, Any]] = None,
) -> Dict[str, str]:
    safe_command = as_str(command)
    safe_target_identity = as_str(target_identity)
    return {
        "command": safe_command,
        "command_quoted": shlex.quote(safe_command),
        "executor_type": as_str(executor_type, "local_process"),
        "executor_profile": as_str(executor_profile, "local-default"),
        "target_kind": as_str(target_kind, "runtime_node"),
        "target_identity": safe_target_identity,
        "target_identity_quoted": shlex.quote(safe_target_identity),
        "target_cluster_id": as_str(target_cluster_id),
        "target_cluster_id_quoted": shlex.quote(as_str(target_cluster_id)),
        "target_namespace": as_str(target_namespace),
        "target_namespace_quoted": shlex.quote(as_str(target_namespace)),
        "target_node_name": as_str(target_node_name),
        "target_node_name_quoted": shlex.quote(as_str(target_node_name)),
        "resolved_target_context_json": _to_json_text(resolved_target_context),
        "resolved_target_context_json_quoted": shlex.quote(_to_json_text(resolved_target_context)),
    }


def resolve_executor(
    *,
    command: str,
    executor_type: str,
    executor_profile: str,
    target_kind: str,
    target_identity: str,
    resolved_target_context: Optional[Dict[str, Any]] = None,
) -> Dict[str, Any]:
    raw_executor_type = as_str(executor_type, "local_process")
    requested_executor_profile = as_str(executor_profile, "local-default").strip().lower()
    safe_target_kind = as_str(target_kind, "runtime_node")
    safe_target_identity = as_str(target_identity, "runtime:local")
    safe_context = _safe_dict(resolved_target_context)
    scope = _extract_execution_scope(
        resolved_target_context=safe_context,
        target_identity=safe_target_identity,
    )
    if as_str(scope.get("target_kind")).strip():
        safe_target_kind = as_str(scope.get("target_kind"))
    if as_str(scope.get("target_identity")).strip():
        safe_target_identity = as_str(scope.get("target_identity"))
    target_cluster_id = as_str(scope.get("cluster_id"))
    target_namespace = as_str(scope.get("namespace"))
    target_node_name = as_str(scope.get("node_name"))
    safe_executor_profile = _select_effective_profile(
        requested_profile=requested_executor_profile,
        resolved_target_context=safe_context,
    ) or requested_executor_profile
    safe_executor_type = _catalog_executor_type_for_profile(
        executor_profile=safe_executor_profile,
        target_kind=safe_target_kind,
        fallback_executor_type=raw_executor_type,
    )
    safe_command = as_str(command)
    if as_str(safe_target_kind).strip().lower() == "host_node" and _is_unknown_token(target_node_name):
        return {
            "requested_executor_type": raw_executor_type,
            "requested_executor_profile": requested_executor_profile,
            "effective_executor_type": "",
            "effective_executor_profile": "",
            "target_kind": safe_target_kind,
            "target_identity": safe_target_identity,
            "target_cluster_id": target_cluster_id,
            "target_namespace": target_namespace,
            "target_node_name": target_node_name,
            "dispatch_backend": "target_resolution_blocked",
            "dispatch_mode": "blocked",
            "dispatch_reason": "host_node target requires resolved node_name",
            "dispatch_template_env": "",
            "dispatch_requires_template": True,
            "dispatch_ready": False,
            "dispatch_degraded": True,
            "resolved_command": "",
            "resolved_target_context": safe_context,
        }
    context = _template_context(
        command=safe_command,
        executor_type=safe_executor_type,
        executor_profile=safe_executor_profile,
        target_kind=safe_target_kind,
        target_identity=safe_target_identity,
        target_cluster_id=target_cluster_id,
        target_namespace=target_namespace,
        target_node_name=target_node_name,
        resolved_target_context=safe_context,
    )

    # V4 hardening: local process execution is disabled by default.
    # Commands must run through controlled template executors.
    if raw_executor_type == "local_process" and not _allow_local_process_executor():
        return {
            "requested_executor_type": raw_executor_type,
            "requested_executor_profile": requested_executor_profile,
            "effective_executor_type": "",
            "effective_executor_profile": "",
            "target_kind": safe_target_kind,
            "target_identity": safe_target_identity,
            "target_cluster_id": target_cluster_id,
            "target_namespace": target_namespace,
            "target_node_name": target_node_name,
            "dispatch_backend": "local_process_disabled",
            "dispatch_mode": "blocked",
            "dispatch_reason": "local_process executor is disabled; use controlled executor profile",
            "dispatch_template_env": "",
            "dispatch_requires_template": True,
            "dispatch_ready": False,
            "dispatch_degraded": True,
            "resolved_command": "",
            "resolved_target_context": safe_context,
        }

    candidate_env_names = executor_template_env_names(safe_executor_type, safe_executor_profile)
    dispatch_requires_template = True

    for env_name in candidate_env_names:
        template = as_str(os.getenv(env_name)).strip()
        if not template:
            continue
        try:
            resolved_command = template.format(**context).strip()
        except KeyError as exc:
            return {
                "requested_executor_type": safe_executor_type,
                "requested_executor_profile": requested_executor_profile,
                "effective_executor_type": "",
                "effective_executor_profile": "",
                "target_kind": safe_target_kind,
                "target_identity": safe_target_identity,
                "target_cluster_id": target_cluster_id,
                "target_namespace": target_namespace,
                "target_node_name": target_node_name,
                "dispatch_backend": "template_unavailable",
                "dispatch_mode": "blocked",
                "dispatch_reason": f"invalid executor template {env_name}: missing {exc}",
                "dispatch_template_env": env_name,
                "dispatch_requires_template": dispatch_requires_template,
                "dispatch_ready": False,
                "dispatch_degraded": True,
                "resolved_command": "",
                "resolved_target_context": safe_context,
            }
        if resolved_command:
            dispatch_reason = f"resolved via {env_name}"
            if safe_executor_profile != requested_executor_profile:
                dispatch_reason = (
                    f"{dispatch_reason}; profile overridden by target metadata: "
                    f"{requested_executor_profile} -> {safe_executor_profile}"
                )
            return {
                "requested_executor_type": raw_executor_type,
                "requested_executor_profile": requested_executor_profile,
                "effective_executor_type": safe_executor_type,
                "effective_executor_profile": safe_executor_profile,
                "target_kind": safe_target_kind,
                "target_identity": safe_target_identity,
                "target_cluster_id": target_cluster_id,
                "target_namespace": target_namespace,
                "target_node_name": target_node_name,
                "dispatch_backend": "template_executor",
                "dispatch_mode": "remote_template",
                "dispatch_reason": dispatch_reason,
                "dispatch_template_env": env_name,
                "dispatch_requires_template": dispatch_requires_template,
                "dispatch_ready": True,
                "dispatch_degraded": False,
                "resolved_command": resolved_command,
                "resolved_target_context": safe_context,
            }

    return {
        "requested_executor_type": raw_executor_type,
        "requested_executor_profile": requested_executor_profile,
        "effective_executor_type": "",
        "effective_executor_profile": "",
        "target_kind": safe_target_kind,
        "target_identity": safe_target_identity,
        "target_cluster_id": target_cluster_id,
        "target_namespace": target_namespace,
        "target_node_name": target_node_name,
        "dispatch_backend": "template_unavailable",
        "dispatch_mode": "blocked",
        "dispatch_reason": "executor template not configured; controlled executor unavailable",
        "dispatch_template_env": "",
        "dispatch_requires_template": dispatch_requires_template,
        "dispatch_ready": False,
        "dispatch_degraded": True,
        "resolved_command": "",
        "resolved_target_context": safe_context,
    }


def list_executor_statuses() -> List[Dict[str, Any]]:
    rows: List[Dict[str, Any]] = []
    for item in EXECUTOR_PROFILE_CATALOG:
        resolution = resolve_executor(
            command="true",
            executor_type=as_str(item.get("executor_type"), "local_process"),
            executor_profile=as_str(item.get("executor_profile"), "local-default"),
            target_kind=as_str(item.get("target_kind"), "runtime_node"),
            target_identity=as_str(item.get("target_identity"), "runtime:local"),
        )
        rows.append(
            {
                "executor_type": as_str(item.get("executor_type"), "local_process"),
                "executor_profile": as_str(item.get("executor_profile"), "local-default"),
                "target_kind": as_str(item.get("target_kind"), "runtime_node"),
                "target_identity": as_str(item.get("target_identity"), "runtime:local"),
                "candidate_template_envs": executor_template_env_names(
                    as_str(item.get("executor_type"), "local_process"),
                    as_str(item.get("executor_profile"), "local-default"),
                ),
                "rollout_stage": as_str(item.get("rollout_stage")),
                "summary": as_str(item.get("summary")),
                "example_template": as_str(item.get("example_template")),
                "dispatch_backend": as_str(resolution.get("dispatch_backend"), "template_unavailable"),
                "dispatch_mode": as_str(resolution.get("dispatch_mode"), "local_process"),
                "dispatch_reason": as_str(resolution.get("dispatch_reason")),
                "dispatch_template_env": as_str(resolution.get("dispatch_template_env")),
                "dispatch_requires_template": bool(resolution.get("dispatch_requires_template")),
                "dispatch_ready": bool(resolution.get("dispatch_ready")),
                "dispatch_degraded": bool(resolution.get("dispatch_degraded")),
                "effective_executor_type": as_str(resolution.get("effective_executor_type")),
                "effective_executor_profile": as_str(resolution.get("effective_executor_profile")),
            }
        )
    return rows


__all__ = ["executor_template_env_names", "list_executor_statuses", "resolve_executor"]
