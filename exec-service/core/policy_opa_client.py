"""
OPA policy decision client and decision merge helpers.
"""

from __future__ import annotations

import json
import os
from typing import Any, Dict, Tuple
from urllib.error import URLError
from urllib.request import Request, urlopen


_RESULT_ORDER = {
    "allow": 0,
    "confirm": 1,
    "elevate": 2,
    "manual_required": 3,
    "deny": 4,
}


def as_str(value: Any, default: str = "") -> str:
    if value is None:
        return default
    if isinstance(value, str):
        return value
    return str(value)


def as_bool(value: Any, default: bool = False) -> bool:
    if isinstance(value, bool):
        return value
    if value is None:
        return default
    normalized = as_str(value).strip().lower()
    if normalized in {"1", "true", "yes", "on"}:
        return True
    if normalized in {"0", "false", "no", "off"}:
        return False
    return default


def policy_mode() -> str:
    default_mode = "local" if os.environ.get("PYTEST_CURRENT_TEST") is not None else "opa_enforced"
    raw = as_str(os.getenv("EXEC_POLICY_DECISION_MODE"), default_mode).strip().lower()
    if raw in {"opa_enforced", "opa-shadow", "opa_shadow", "local"}:
        normalized = raw.replace("-", "_")
    else:
        normalized = default_mode
    if normalized in {"local", "opa_shadow"}:
        allow_non_enforced = as_bool(
            os.getenv("EXEC_POLICY_ALLOW_NON_ENFORCED_MODES"),
            os.environ.get("PYTEST_CURRENT_TEST") is not None,
        )
        if not allow_non_enforced:
            return "opa_enforced"
    return normalized


def opa_policy_package() -> str:
    return as_str(os.getenv("EXEC_POLICY_PACKAGE"), "runtime.command.v1")


def _opa_url() -> str:
    return as_str(os.getenv("EXEC_POLICY_OPA_URL"), "http://opa:8181/v1/data/runtime/command/v1").strip()


def _opa_timeout_seconds() -> float:
    timeout_ms = max(10, min(5000, int(as_str(os.getenv("EXEC_POLICY_OPA_TIMEOUT_MS"), "800"))))
    return float(timeout_ms) / 1000.0


def _normalize_result(value: Any) -> str:
    normalized = as_str(value).strip().lower()
    if normalized in _RESULT_ORDER:
        return normalized
    if normalized in {"approved", "ok", "pass"}:
        return "allow"
    if normalized in {"rejected", "blocked", "block"}:
        return "deny"
    return ""


def _strictest_result(left: str, right: str) -> str:
    safe_left = _normalize_result(left) or "deny"
    safe_right = _normalize_result(right) or "deny"
    return safe_left if _RESULT_ORDER[safe_left] >= _RESULT_ORDER[safe_right] else safe_right


def _parse_opa_result(body: Dict[str, Any]) -> Tuple[bool, Dict[str, Any]]:
    if not isinstance(body, dict):
        return False, {}
    payload = body.get("result")
    if isinstance(payload, bool):
        return True, {
            "result": "allow" if payload else "deny",
            "reason": "opa boolean result",
            "package": opa_policy_package(),
        }
    if isinstance(payload, dict):
        parsed_result = _normalize_result(
            payload.get("decision")
            or payload.get("result")
            or payload.get("action")
            or payload.get("effect")
            or ("allow" if payload.get("allow") is True else "deny" if payload.get("allow") is False else "")
        )
        if not parsed_result:
            return False, {}
        return True, {
            "result": parsed_result,
            "reason": as_str(payload.get("reason"), as_str(payload.get("message"), "opa decision")),
            "package": as_str(payload.get("package"), opa_policy_package()),
        }
    return False, {}


def _query_opa_decision(input_payload: Dict[str, Any]) -> Tuple[bool, Dict[str, Any], str]:
    url = _opa_url()
    request_body = json.dumps({"input": input_payload if isinstance(input_payload, dict) else {}}, ensure_ascii=False).encode("utf-8")
    request = Request(
        url=url,
        data=request_body,
        headers={"Content-Type": "application/json"},
        method="POST",
    )
    try:
        with urlopen(request, timeout=_opa_timeout_seconds()) as response:
            raw = response.read().decode("utf-8", errors="replace")
            payload = json.loads(raw) if raw.strip() else {}
            ok, parsed = _parse_opa_result(payload if isinstance(payload, dict) else {})
            if not ok:
                return False, {}, "opa_result_invalid"
            return True, parsed, ""
    except URLError as exc:
        return False, {}, f"opa_unreachable:{as_str(exc.reason)}"
    except Exception as exc:
        return False, {}, f"opa_query_failed:{as_str(exc)}"


def evaluate_policy_decision(
    *,
    local_result: str,
    local_reason: str,
    input_payload: Dict[str, Any],
) -> Dict[str, Any]:
    safe_local_result = _normalize_result(local_result) or "deny"
    safe_local_reason = as_str(local_reason)
    mode = policy_mode()
    local_decision = {
        "result": safe_local_result,
        "reason": safe_local_reason,
        "engine": "python-inline",
        "package": opa_policy_package(),
        "mode": mode,
        "source": "local",
        "opa_available": False,
        "opa_result": "",
        "opa_reason": "",
    }
    if mode == "local":
        return local_decision

    ok, opa_decision, error_reason = _query_opa_decision(input_payload)
    if not ok:
        if mode == "opa_enforced":
            return {
                "result": "deny",
                "reason": f"opa unavailable (fail-closed): {error_reason}",
                "engine": "opa",
                "package": opa_policy_package(),
                "mode": mode,
                "source": "opa",
                "opa_available": False,
                "opa_result": "",
                "opa_reason": error_reason,
                "local_result": safe_local_result,
                "local_reason": safe_local_reason,
            }
        local_decision["opa_reason"] = error_reason
        return local_decision

    remote_result = _normalize_result(opa_decision.get("result")) or "deny"
    remote_reason = as_str(opa_decision.get("reason"), "opa decision")
    remote_package = as_str(opa_decision.get("package"), opa_policy_package())
    if mode == "opa_shadow":
        decision = dict(local_decision)
        decision["opa_available"] = True
        decision["opa_result"] = remote_result
        decision["opa_reason"] = remote_reason
        decision["opa_package"] = remote_package
        return decision

    use_strictest = as_bool(os.getenv("EXEC_POLICY_ENFORCE_STRICTEST_WITH_LOCAL"), True)
    effective_result = _strictest_result(safe_local_result, remote_result) if use_strictest else remote_result
    effective_reason = remote_reason
    if use_strictest and effective_result != remote_result:
        effective_reason = f"{remote_reason}; strictest(local={safe_local_result}, opa={remote_result})"
    return {
        "result": effective_result,
        "reason": effective_reason,
        "engine": "opa",
        "package": remote_package,
        "mode": mode,
        "source": "opa",
        "opa_available": True,
        "opa_result": remote_result,
        "opa_reason": remote_reason,
        "local_result": safe_local_result,
        "local_reason": safe_local_reason,
    }


__all__ = ["evaluate_policy_decision", "policy_mode"]
