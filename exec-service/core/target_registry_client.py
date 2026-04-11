"""
Target registry gate client for exec-service precheck.

Security behavior:
- `enforced`: registry decision is mandatory, failures degrade to manual_required.
- `audit`: query registry but do not block execution path.
- `disabled`: skip registry lookup.
"""

from __future__ import annotations

import json
import os
from typing import Any, Dict, List, Optional, Tuple
from urllib.error import HTTPError, URLError
from urllib.parse import quote, urlencode
from urllib.request import Request, urlopen


def as_str(value: Any, default: str = "") -> str:
    if value is None:
        return default
    if isinstance(value, str):
        return value
    return str(value)


def _normalize_capabilities(values: Any) -> List[str]:
    if not isinstance(values, list):
        return []
    normalized: List[str] = []
    seen = set()
    for item in values:
        capability = as_str(item).strip().lower()
        if not capability or capability in seen:
            continue
        seen.add(capability)
        normalized.append(capability)
    return normalized


def _normalize_tokens(values: Any) -> List[str]:
    if not isinstance(values, list):
        return []
    normalized: List[str] = []
    seen = set()
    for item in values:
        token = as_str(item).strip()
        if not token or token in seen:
            continue
        seen.add(token)
        normalized.append(token)
    return normalized


def _required_metadata_keys(target_kind: str) -> List[str]:
    safe_kind = as_str(target_kind).strip().lower()
    if safe_kind == "host_node":
        return ["cluster_id", "node_name", "preferred_executor_profiles", "risk_tier"]
    if safe_kind in {"k8s_cluster", "clickhouse_cluster", "openstack_project"}:
        return ["cluster_id", "preferred_executor_profiles", "risk_tier"]
    return ["preferred_executor_profiles", "risk_tier"]


def _normalize_execution_scope(value: Any) -> Dict[str, Any]:
    safe = value if isinstance(value, dict) else {}
    return {
        "cluster_id": as_str(safe.get("cluster_id")),
        "namespace": as_str(safe.get("namespace")),
        "node_name": as_str(safe.get("node_name")),
        "target_kind": as_str(safe.get("target_kind")),
        "target_identity": as_str(safe.get("target_identity")),
    }


def _normalize_metadata_contract(value: Any, *, target_kind: str = "", target_identity: str = "") -> Dict[str, Any]:
    safe = value if isinstance(value, dict) else {}
    required_keys = _normalize_tokens(safe.get("required_keys"))
    fallback_required = _required_metadata_keys(target_kind)
    if not required_keys:
        required_keys = list(fallback_required)
    missing_required_keys = _normalize_tokens(safe.get("missing_required_keys"))
    metadata = safe.get("metadata") if isinstance(safe.get("metadata"), dict) else {}
    execution_scope = _normalize_execution_scope(safe.get("execution_scope"))
    if not execution_scope.get("target_kind"):
        execution_scope["target_kind"] = as_str(target_kind)
    if not execution_scope.get("target_identity"):
        execution_scope["target_identity"] = as_str(target_identity)
    return {
        "required_keys": required_keys,
        "missing_required_keys": missing_required_keys,
        "metadata": dict(metadata),
        "execution_scope": execution_scope,
    }


def target_registry_mode() -> str:
    default_mode = "disabled" if os.environ.get("PYTEST_CURRENT_TEST") is not None else "enforced"
    raw = as_str(os.getenv("EXEC_TARGET_REGISTRY_MODE"), default_mode).strip().lower()
    if raw in {"disabled", "off", "false", "0"}:
        return "disabled"
    if raw in {"audit", "shadow", "observe"}:
        return "audit"
    return "enforced"


def _base_url() -> str:
    return (
        as_str(os.getenv("EXEC_TARGET_REGISTRY_BASE_URL"))
        or as_str(os.getenv("AI_SERVICE_BASE_URL"))
        or "http://ai-service:8090"
    ).strip().rstrip("/")


def _timeout_seconds() -> float:
    raw = as_str(os.getenv("EXEC_TARGET_REGISTRY_TIMEOUT_MS"), "800")
    try:
        parsed = int(raw)
    except Exception:
        parsed = 800
    bounded = max(50, min(5000, parsed))
    return float(bounded) / 1000.0


def _http_json(
    *,
    method: str,
    path: str,
    query: Optional[Dict[str, Any]] = None,
    payload: Optional[Dict[str, Any]] = None,
) -> Tuple[bool, Dict[str, Any], str]:
    base = _base_url()
    safe_path = "/" + as_str(path).lstrip("/")
    url = f"{base}{safe_path}"
    if isinstance(query, dict) and query:
        filtered = {key: as_str(value) for key, value in query.items() if as_str(value).strip() != ""}
        if filtered:
            url = f"{url}?{urlencode(filtered)}"
    body: Optional[bytes] = None
    headers = {"Content-Type": "application/json"}
    if payload is not None:
        body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
    request = Request(url=url, data=body, headers=headers, method=method.upper())
    try:
        with urlopen(request, timeout=_timeout_seconds()) as response:
            raw = response.read().decode("utf-8", errors="replace")
            parsed = json.loads(raw) if raw.strip() else {}
            if isinstance(parsed, dict):
                return True, parsed, ""
            return False, {}, "response_not_json_object"
    except HTTPError as exc:
        try:
            raw_error = exc.read().decode("utf-8", errors="replace")
        except Exception:
            raw_error = ""
        error_detail = raw_error.strip() or as_str(exc.reason, as_str(exc))
        return False, {}, f"http_{exc.code}:{error_detail}"
    except URLError as exc:
        return False, {}, f"unreachable:{as_str(exc.reason, as_str(exc))}"
    except Exception as exc:
        return False, {}, f"request_failed:{as_str(exc)}"


def _select_target_by_identity(
    *,
    target_kind: str,
    target_identity: str,
) -> Tuple[str, Dict[str, Any], str]:
    ok, payload, error = _http_json(
        method="GET",
        path="/api/v2/targets",
        query={
            "target_kind": as_str(target_kind).strip().lower(),
            "limit": "500",
        },
    )
    if not ok:
        return "", {}, error
    raw_targets = payload.get("targets")
    if not isinstance(raw_targets, list):
        return "", {}, "invalid_target_list"

    safe_kind = as_str(target_kind).strip().lower()
    safe_identity = as_str(target_identity).strip().lower()
    matches: List[Dict[str, Any]] = []
    for item in raw_targets:
        if not isinstance(item, dict):
            continue
        item_kind = as_str(item.get("target_kind")).strip().lower()
        item_identity = as_str(item.get("target_identity")).strip().lower()
        item_id = as_str(item.get("target_id")).strip()
        if not item_id:
            continue
        if safe_kind and item_kind != safe_kind:
            continue
        if safe_identity and item_identity != safe_identity:
            continue
        matches.append(item)
    if not matches:
        return "", {}, ""
    active = [item for item in matches if as_str(item.get("status")).strip().lower() == "active"]
    selected = active[0] if active else matches[0]
    return as_str(selected.get("target_id")), selected, ""


def _extract_resolution(
    *,
    payload: Dict[str, Any],
    fallback_target_id: str,
    required_capabilities: List[str],
    mode: str,
    matched_target_payload: Optional[Dict[str, Any]] = None,
) -> Optional[Dict[str, Any]]:
    resolution = payload.get("resolution")
    if not isinstance(resolution, dict):
        return None
    resolution_target_kind = as_str(
        resolution.get("target_kind"),
        as_str((matched_target_payload or {}).get("target_kind")),
    )
    resolution_target_identity = as_str(
        resolution.get("target_identity"),
        as_str((matched_target_payload or {}).get("target_identity")),
    )
    metadata_contract = _normalize_metadata_contract(
        resolution.get("metadata_contract"),
        target_kind=resolution_target_kind,
        target_identity=resolution_target_identity,
    )
    missing_required_keys = _normalize_tokens(metadata_contract.get("missing_required_keys"))
    resolved_result = as_str(resolution.get("result")).strip().lower()
    if resolved_result not in {"allow", "manual_required"}:
        resolved_result = "manual_required"
    if missing_required_keys:
        resolved_result = "manual_required"
    enforced = mode == "enforced"
    effective_result = resolved_result if enforced else "allow"
    resolved_target = resolution.get("target")
    resolved_target_context = resolution.get("resolved_target_context")
    resolved_reason = as_str(resolution.get("reason"), "target registry resolved")
    if missing_required_keys:
        metadata_reason = f"target metadata missing required fields: {', '.join(missing_required_keys)}"
        resolved_reason = f"{resolved_reason}; {metadata_reason}" if resolved_reason else metadata_reason
    normalized_target = (
        dict(resolved_target)
        if isinstance(resolved_target, dict)
        else dict(matched_target_payload if isinstance(matched_target_payload, dict) else {})
    )
    return {
        "enabled": True,
        "mode": mode,
        "applied": enforced,
        "result": effective_result,
        "reason": resolved_reason,
        "target_id": as_str(resolution.get("target_id"), fallback_target_id),
        "registered": bool(resolution.get("registered")),
        "status": as_str(resolution.get("status"), "unknown"),
        "target_kind": resolution_target_kind,
        "target_identity": resolution_target_identity,
        "required_capabilities": list(required_capabilities),
        "missing_capabilities": _normalize_capabilities(resolution.get("missing_capabilities")),
        "matched_capabilities": _normalize_capabilities(resolution.get("matched_capabilities")),
        "lookup_error": "",
        "resolve_error": "",
        "target": normalized_target,
        "metadata_contract": metadata_contract,
        "resolved_target_context": (
            dict(resolved_target_context)
            if isinstance(resolved_target_context, dict)
            else {
                "target_id": as_str(resolution.get("target_id"), fallback_target_id),
                "target_kind": resolution_target_kind,
                "target_identity": resolution_target_identity,
                "metadata": dict(metadata_contract.get("metadata") or {}),
                "execution_scope": dict(metadata_contract.get("execution_scope") or {}),
            }
        ),
        "ambiguous_targets": _normalize_tokens(resolution.get("ambiguous_targets")),
        "source": "ai_runtime_v2",
    }


def _manual_result(
    *,
    mode: str,
    target_id: str,
    required_capabilities: List[str],
    reason: str,
    registered: bool,
    status: str,
    target: Optional[Dict[str, Any]] = None,
    lookup_error: str = "",
    resolve_error: str = "",
    target_kind: str = "",
    target_identity: str = "",
) -> Dict[str, Any]:
    enforced = mode == "enforced"
    if enforced:
        result = "manual_required"
        final_reason = reason
    else:
        result = "allow"
        final_reason = f"{reason} (audit mode)"
    return {
        "enabled": mode != "disabled",
        "mode": mode,
        "applied": enforced,
        "result": result,
        "reason": final_reason,
        "target_id": as_str(target_id),
        "registered": bool(registered),
        "status": as_str(status, "unknown"),
        "target_kind": as_str(target_kind),
        "target_identity": as_str(target_identity),
        "required_capabilities": list(required_capabilities),
        "missing_capabilities": list(required_capabilities),
        "matched_capabilities": [],
        "lookup_error": as_str(lookup_error),
        "resolve_error": as_str(resolve_error),
        "target": dict(target) if isinstance(target, dict) else {},
        "metadata_contract": _normalize_metadata_contract(
            {},
            target_kind=as_str(target_kind),
            target_identity=as_str(target_identity),
        ),
        "resolved_target_context": {
            "target_id": as_str(target_id),
            "target_kind": as_str(target_kind),
            "target_identity": as_str(target_identity),
            "metadata": {},
            "execution_scope": _normalize_execution_scope(
                {
                    "target_kind": as_str(target_kind),
                    "target_identity": as_str(target_identity),
                }
            ),
        },
        "ambiguous_targets": [],
        "source": "ai_runtime_v2",
    }


def evaluate_target_registry_gate(
    *,
    target_id: str,
    target_kind: str,
    target_identity: str,
    required_capabilities: List[str],
    action_id: str = "",
    reason: str = "",
) -> Dict[str, Any]:
    mode = target_registry_mode()
    required = _normalize_capabilities(required_capabilities)
    safe_target_id = as_str(target_id).strip()
    safe_target_kind = as_str(target_kind).strip()
    safe_target_identity = as_str(target_identity).strip()
    safe_reason = as_str(reason).strip()

    if mode == "disabled":
        return {
            "enabled": False,
            "mode": mode,
            "applied": False,
            "result": "allow",
            "reason": "target registry disabled",
            "target_id": safe_target_id,
            "registered": True,
            "status": "unknown",
            "target_kind": safe_target_kind,
            "target_identity": safe_target_identity,
            "required_capabilities": required,
            "missing_capabilities": [],
            "matched_capabilities": [],
            "lookup_error": "",
            "resolve_error": "",
            "target": {},
            "metadata_contract": _normalize_metadata_contract(
                {},
                target_kind=safe_target_kind,
                target_identity=safe_target_identity,
            ),
            "resolved_target_context": {
                "target_id": safe_target_id,
                "target_kind": safe_target_kind,
                "target_identity": safe_target_identity,
                "metadata": {},
                "execution_scope": _normalize_execution_scope(
                    {
                        "target_kind": safe_target_kind,
                        "target_identity": safe_target_identity,
                    }
                ),
            },
            "source": "disabled",
        }

    matched_target_payload: Dict[str, Any] = {}
    if not safe_target_id:
        ok, payload, resolve_error = _http_json(
            method="POST",
            path="/api/v2/targets/resolve/by-identity",
            payload={
                "target_kind": safe_target_kind,
                "target_identity": safe_target_identity,
                "required_capabilities": required,
                "action_id": as_str(action_id),
                "reason": safe_reason or "exec precheck target gate",
            },
        )
        if ok:
            extracted = _extract_resolution(
                payload=payload,
                fallback_target_id="",
                required_capabilities=required,
                mode=mode,
                matched_target_payload=matched_target_payload,
            )
            if extracted is not None:
                return extracted
            return _manual_result(
                mode=mode,
                target_id="",
                required_capabilities=required,
                reason="target registry resolve response invalid",
                registered=False,
                status="unknown",
                resolve_error="invalid_resolution_payload",
                target_kind=safe_target_kind,
                target_identity=safe_target_identity,
            )
        # Compatible with rolling upgrade: fallback only when new route is not found.
        if not resolve_error.startswith("http_404"):
            return _manual_result(
                mode=mode,
                target_id="",
                required_capabilities=required,
                reason=f"target registry resolve unavailable: {resolve_error}",
                registered=False,
                status="unknown",
                resolve_error=resolve_error,
                target_kind=safe_target_kind,
                target_identity=safe_target_identity,
            )

        resolved_target_id, matched_target_payload, lookup_error = _select_target_by_identity(
            target_kind=safe_target_kind,
            target_identity=safe_target_identity,
        )
        if lookup_error:
            return _manual_result(
                mode=mode,
                target_id="",
                required_capabilities=required,
                reason=f"target registry lookup unavailable: {lookup_error}",
                registered=False,
                status="unknown",
                lookup_error=lookup_error,
                target_kind=safe_target_kind,
                target_identity=safe_target_identity,
            )
        safe_target_id = resolved_target_id
        if not safe_target_id:
            return _manual_result(
                mode=mode,
                target_id="",
                required_capabilities=required,
                reason="target not registered",
                registered=False,
                status="unknown",
                target_kind=safe_target_kind,
                target_identity=safe_target_identity,
            )

    ok, payload, resolve_error = _http_json(
        method="POST",
        path=f"/api/v2/targets/{quote(safe_target_id, safe='')}/resolve",
        payload={
            "required_capabilities": required,
            "action_id": as_str(action_id),
            "reason": safe_reason or "exec precheck target gate",
        },
    )
    if not ok:
        return _manual_result(
            mode=mode,
            target_id=safe_target_id,
            required_capabilities=required,
            reason=f"target registry resolve unavailable: {resolve_error}",
            registered=False,
            status="unknown",
            target=matched_target_payload,
            resolve_error=resolve_error,
            target_kind=safe_target_kind,
            target_identity=safe_target_identity,
        )

    extracted = _extract_resolution(
        payload=payload,
        fallback_target_id=safe_target_id,
        required_capabilities=required,
        mode=mode,
        matched_target_payload=matched_target_payload,
    )
    if extracted is None:
        return _manual_result(
            mode=mode,
            target_id=safe_target_id,
            required_capabilities=required,
            reason="target registry resolve response invalid",
            registered=False,
            status="unknown",
            target=matched_target_payload,
            resolve_error="invalid_resolution_payload",
            target_kind=safe_target_kind,
            target_identity=safe_target_identity,
        )
    return extracted


__all__ = ["evaluate_target_registry_gate", "target_registry_mode"]
