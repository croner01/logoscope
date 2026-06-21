"""
Internal compatibility helpers ported from ``followup_command_spec``.

These functions are still referenced by the active v1 execution path
(``agent_runtime/service.py``, ``api/ai.py``).  They remain here as a
stepping stone until the unified engine (``AI_RUNTIME_UNIFIED_ENGINE_ENABLED``)
becomes the default, at which point this file can be deleted entirely.

``followup_command.py`` and ``followup_command_spec.py`` will be deleted once all importers are migrated to this file.
"""
from __future__ import annotations

import json
import re
from typing import Any, Dict, Tuple

from ai.command.line_normalizer import (
    ALLOWED_HEADS,
    normalize_command_line,
    repair_clickhouse_query_text,
)
from ai.command.normalizer import normalize_command_spec
from ai.command.compiler import compile_command
from ai.command.spec import CommandSpec, CommandType, RiskLevel, ToolType


_K8S_NAMESPACE_PATTERN = re.compile(r"^[a-z0-9]([-a-z0-9]*[a-z0-9])?$")
_SUPPORTED_TOOLS = {"kubectl_clickhouse_query", "k8s_clickhouse_query", "clickhouse_query", "generic_exec"}

_FOLLOWUP_REASON_GROUP_MAP: dict[str, str] = {
    "ok": "OK",
    "missing_or_invalid_command_spec": "COMPILE_ERROR",
    "glued_command_tokens": "COMPILE_ERROR",
    "unsupported_command_head": "COMPILE_ERROR",
    "invalid_kubectl_token": "COMPILE_ERROR",
    "suspicious_selector_namespace_glue": "COMPILE_ERROR",
    "glued_sql_tokens": "COMPILE_ERROR",
    "unsupported_clickhouse_readonly_query": "DENIED",
    "clickhouse_multi_statement_not_allowed": "DENIED",
    "unsupported_repair": "DENIED",
    "permission_denied_repair": "DENIED",
    "missing_target_identity": "COMPILE_ERROR",
    "target_kind_mismatch": "COMPILE_ERROR",
    "target_identity_mismatch": "COMPILE_ERROR",
    "pod_selector_requires_shell": "DENIED",
    "missing_namespace_for_k8s_clickhouse_query": "COMPILE_ERROR",
    "missing_pod_name_for_k8s_clickhouse_query": "COMPILE_ERROR",
    "pod_name_resolution_failed": "COMPILE_ERROR",
    "other": "OTHER",
}

_FOLLOWUP_REASON_CODE_ALIASES: dict[str, str] = {
    "unsupported_repair": "permission_denied_repair",
}

_FOLLOWUP_REASON_PREFIX_ALIASES: dict[str, str] = {
    "shell_exec_disabled_": "permission_denied_repair",
    "permission_denied_": "permission_denied_repair",
}


def _as_str(value: Any, default: str = "") -> str:
    if value is None:
        return default
    if isinstance(value, str):
        return value
    return str(value)


# ── Reason-code helpers ────────────────────────────────────────────────────


def normalize_reason_code(reason: Any) -> str:
    """Normalize a reason code (replaces ``normalize_followup_reason_code``)."""
    safe_reason = _as_str(reason).strip().lower()
    if not safe_reason:
        return "other"

    candidates: list[str] = []
    for candidate in (
        safe_reason,
        safe_reason.split(":", 1)[0].strip(),
        safe_reason.split(" ", 1)[0].strip(),
    ):
        if candidate and candidate not in candidates:
            candidates.append(candidate)

    for candidate in candidates:
        if candidate in _FOLLOWUP_REASON_GROUP_MAP:
            return candidate
        aliased = _FOLLOWUP_REASON_CODE_ALIASES.get(candidate)
        if aliased:
            return aliased
        for prefix, mapped in _FOLLOWUP_REASON_PREFIX_ALIASES.items():
            if candidate.startswith(prefix):
                return mapped
    return "other"


def map_reason_group(reason: Any) -> str:
    """Map a reason code to its group (replaces ``map_followup_reason_group``)."""
    normalized = normalize_reason_code(reason)
    return _FOLLOWUP_REASON_GROUP_MAP.get(normalized, "OTHER")


# ── Match key ──────────────────────────────────────────────────────────────


def build_match_key(spec: Any) -> str:
    """Build a stable match key from a command spec (replaces ``build_followup_command_spec_match_key``)."""
    if isinstance(spec, CommandSpec):
        # New-style CommandSpec — use model_dump for stable serialisation
        return json.dumps(spec.model_dump(), ensure_ascii=False, sort_keys=True)

    # Legacy dict-style spec
    try:
        safe = normalize_command_spec(spec) if isinstance(spec, dict) else None
    except Exception:
        safe = None
    if safe is None:
        return json.dumps({"tool": "generic_exec", "command": _as_str(spec.get("command") if isinstance(spec, dict) else spec)}, ensure_ascii=False, sort_keys=True)

    return json.dumps(safe.model_dump(), ensure_ascii=False, sort_keys=True)


# ── Self-repair payload ────────────────────────────────────────────────────


def build_self_repair_payload(
    *,
    reason: Any,
    detail: Any = "",
    command_spec: Any = None,
    raw_command: str = "",
) -> Dict[str, Any]:
    """Build a self-repair payload (replaces ``build_command_spec_self_repair_payload``).

    Returns a dict with keys: ``reason``, ``detail``, ``fix_hint``,
    ``suggested_spec``, ``suggested_command``.
    """
    safe_reason = _as_str(reason).strip().lower()
    safe_detail = _as_str(detail).strip()
    safe_raw_command = _as_str(raw_command).strip()

    fix_hint_map: dict[str, str] = {
        "missing_or_invalid_command_spec": "请提供 command_spec；不要只传自由文本 command。",
        "glued_command_tokens": "命令存在粘连（缺少空格），请拆分为标准 argv 形式后重试。",
        "unsupported_command_head": "命令头不在白名单中，请改为受支持的只读命令（如 kubectl/curl/clickhouse-client）。",
        "invalid_kubectl_token": "kubectl 参数存在非法字符，请按标准 argv 重新拆分（不要包含括号拼接）。",
        "suspicious_selector_namespace_glue": "selector 里疑似粘连了 namespace（如 app=query-service-nislap），请拆分为 -l app=query-service -n islap。",
        "glued_sql_tokens": "SQL 关键字存在粘连，请在关键字与标识符之间补空格后重试。",
        "unsupported_clickhouse_readonly_query": "只允许只读 SQL：SELECT / SHOW / DESCRIBE / EXPLAIN。",
        "clickhouse_multi_statement_not_allowed": "一次仅允许一条 SQL 语句，请去掉多语句分隔符 ';'。",
        "missing_target_identity": "请显式提供 target_identity（例如 database:logs）。",
        "target_kind_mismatch": "命令语义与 target_kind 不一致，请修正为匹配的执行目标。",
        "target_identity_mismatch": "命令显式作用域与 target_identity 不一致，请修正后重试。",
        "pod_selector_requires_shell": "shell 执行面默认禁用，请改为提供 pod_name，不要使用 pod_selector。",
        "missing_namespace_for_k8s_clickhouse_query": "k8s ClickHouse 查询必须提供 namespace（例如 islap）。",
        "missing_pod_name_for_k8s_clickhouse_query": "k8s ClickHouse 查询必须在 Pod 中执行，请补充 pod_name 或可解析的 pod_selector。",
        "pod_name_resolution_failed": "无法自动解析 ClickHouse Pod，请确认 namespace/selector，或显式提供 pod_name（例如 clickhouse-0）。",
    }

    fix_hint = fix_hint_map.get(
        safe_reason,
        "请补全并提交规范 command_spec（tool + args），系统将自动重新校验。",
    )

    suggested_spec: Dict[str, Any] = {}
    if safe_raw_command:
        suggested_spec = {
            "tool": "generic_exec",
            "command": normalize_command_line(safe_raw_command),
            "target_kind": "k8s_cluster",
            "target_identity": "",
            "purpose": "",
            "timeout_seconds": 20,
        }
    # Repair glued SQL in suggested spec
    if safe_reason == "glued_sql_tokens" and suggested_spec.get("command"):
        repaired = repair_clickhouse_query_text(_as_str(suggested_spec.get("command")))
        if repaired:
            suggested_spec["command"] = repaired
            suggested_spec["query"] = repaired

    return {
        "reason": safe_reason,
        "detail": safe_detail,
        "fix_code": safe_reason,
        "fix_hint": fix_hint,
        "suggested_spec": suggested_spec,
        "suggested_command_spec": suggested_spec,
        "suggested_command": normalize_command_line(safe_raw_command) if safe_raw_command else "",
    }


# ── Confirmation message (portable) ────────────────────────────────────────


def build_command_confirmation_message(
    *,
    command: str,
    command_type: str,
    risk_level: str,
    reason: str,
    requires_write_permission: bool = False,
) -> str:
    """Build a human-readable confirmation message (replaces ``_build_command_confirmation_message``)."""
    parts: list[str] = []
    if requires_write_permission:
        parts.append("⚠️ 此操作需要写权限。")
    if command_type == "repair":
        parts.append("系统检测到此命令为修复操作。")
    if risk_level in ("high", "medium"):
        parts.append(f"风险等级: {risk_level}。")
    if reason:
        parts.append(f"原因：{reason}")
    parts.append(f"\n命令: {command}")
    return "\n".join(parts)


# ── Resolve command meta (classification + confirmation) ───────────────────


def resolve_command_meta(raw_command: str) -> Tuple[Dict[str, Any], str]:
    """Classify a raw command and build a confirmation message.

    Replaces ``_resolve_followup_command_meta`` from ``followup_command``.
    Uses the unified security module for classification.

    Returns ``(command_meta, confirmation_message)``.
    """
    from ai.command.security import SessionCostState, evaluate_command
    from ai.command.compiler import compile_command
    from ai.command.spec import CommandSpec

    command = normalize_command_line(raw_command)
    if not command:
        meta = {"supported": False, "reason": "empty command", "command": ""}
        return meta, build_command_confirmation_message(command="", command_type="query", risk_level="low", reason="empty command")

    try:
        spec = CommandSpec(
            tool=ToolType.GENERIC_EXEC,
            command=command,
            target_kind="k8s_cluster",
            target_identity="",
            purpose="",
            timeout_seconds=20,
        )
        decision = evaluate_command(spec, session_cost=SessionCostState(), write_enabled=True)
        compiled = compile_command(spec)

        meta = {
            "supported": decision.allowed,
            "command": command,
            "command_type": decision.command_type.value if hasattr(decision.command_type, "value") else str(decision.command_type),
            "risk_level": decision.risk_level.value if hasattr(decision.risk_level, "value") else str(decision.risk_level),
            "requires_write_permission": decision.requires_elevation,
            "reason": decision.reason or "",
            "route": compiled.route,
            "executor_profile": compiled.executor_profile,
        }
    except Exception:
        head = command.split()[0].lower() if command else ""
        meta = {
            "supported": head in ALLOWED_HEADS,
            "command": command,
            "command_type": "query",
            "risk_level": "low",
            "requires_write_permission": False,
            "reason": "",
            "route": "remote",
            "executor_profile": "",
        }

    confirmation = build_command_confirmation_message(
        command=command,
        command_type=_as_str(meta.get("command_type"), "query"),
        risk_level=_as_str(meta.get("risk_level"), "low"),
        reason=_as_str(meta.get("reason")),
        requires_write_permission=bool(meta.get("requires_write_permission")),
    )
    return meta, confirmation


# ── Spec processing (v1-compatible dict wrappers) ──────────────────────────


def normalize_command_spec_compat(raw: Any) -> Dict[str, Any]:
    """Normalize a command spec, returning a v1-compatible dict.

    Wraps ``normalize_command_spec`` so that callers that expect a
    dict can continue using ``safe_spec.get("tool")`` without change.
    Also handles the nested ``args.command`` format used by v1 tests.
    Returns empty dict on failure.
    """
    try:
        safe = raw if isinstance(raw, dict) else {}
        # Normalize nested args format to flat format if needed
        if not safe.get("command") and safe.get("args") and isinstance(safe["args"], dict):
            flat = dict(safe)
            args = safe["args"]
            flat["command"] = _as_str(args.get("command") or args.get("query") or safe.get("command"))
            if not flat.get("target_kind"):
                flat["target_kind"] = _as_str(args.get("target_kind", "k8s_cluster"))
            if not flat.get("target_identity"):
                flat["target_identity"] = _as_str(args.get("target_identity", ""))
            if not flat.get("timeout_seconds"):
                flat["timeout_seconds"] = int(args.get("timeout_s", 20))
            safe = flat
        spec = normalize_command_spec(safe)
        return spec.model_dump() if hasattr(spec, "model_dump") else {}
    except Exception:
        return {}


def _detect_glued_sql_tokens(text: str) -> bool:
    """Detect SQL keywords glued to adjacent words (e.g. SELECTname, FROMsystem)."""
    import re as _re
    _glued_sql = _re.compile(
        r"(?i)(SELECT|FROM|WHERE|AND|OR|NOT|IN|ON|AS|WHEN|THEN|ELSE|END|CASE"
        r"|LIMIT|OFFSET|FORMAT|UNION|ALL|DISTINCT|HAVING|PREWHERE|ARRAY|JOIN"
        r"|GLOBAL|FINAL|SETTINGS|WITH|TOTALS|DESC|ASC|ORDER|GROUP|PARTITION"
        r"|BY|LEFT|RIGHT|INNER|CROSS|FULL|OUTER|SEMI|ANTI"
        r")(?=[A-Za-z0-9_])"
    )
    return bool(_glued_sql.search(text))


def compile_command_compat(
    spec: Any,
    *,
    run_sql_preflight: bool = False,
) -> Dict[str, Any]:
    """Compile a command spec, returning a v1-compatible dict.

    Wraps ``compile_command`` so that callers can keep using
    ``compile_result.get("ok")`` / ``compile_result.get("command")``
    without change.
    Adds v1-compatible error detection (glued SQL tokens, blocked operators, etc.).
    """
    try:
        if isinstance(spec, dict):
            # Quick blocked-operator check (v1 compat)
            raw_cmd = _as_str(spec.get("command") or spec.get("query") or "")
            if raw_cmd:
                blocked = {";", "&", ">", ">>", "<", "<<", "|||"}
                tokens = raw_cmd.split()
                for t in tokens:
                    if t in blocked or ";" in t:
                        return {
                            "ok": False,
                            "reason": "shell_exec_disabled_blocked_operator",
                            "command": "",
                            "command_spec": {},
                            "detail": f"命令包含不安全片段（禁止重定向/后台执行）: {t}",
                        }

            # Check for glued SQL tokens before normalising
            raw_query = _as_str(spec.get("query") or spec.get("sql") or spec.get("command") or "")
            if raw_query and _detect_glued_sql_tokens(raw_query):
                return {
                    "ok": False,
                    "reason": "glued_sql_tokens",
                    "command": "",
                    "command_spec": {},
                    "detail": "SQL 关键字存在粘连，请在关键字与标识符之间补空格后重试",
                }

            normalized = normalize_command_spec(spec)
        else:
            normalized = spec
        compiled = compile_command(normalized, namespace="islap", run_sql_preflight=run_sql_preflight)
        if not compiled.shell_command:
            return {
                "ok": False,
                "reason": "compile_failed",
                "command": "",
                "command_spec": {},
                "detail": "命令编译失败，请检查命令格式",
            }
        return {
            "ok": True,
            "command": compiled.shell_command,
            "route": compiled.route,
            "executor_profile": compiled.executor_profile,
            "command_spec": compiled.spec.model_dump() if hasattr(compiled.spec, "model_dump") else {},
            "reason": "",
        }
    except Exception as e:
        return {"ok": False, "reason": "compile_failed", "command": "", "command_spec": {}, "detail": str(e)}


# ── Embedded command text normalisation ────────────────────────────────────


def normalize_embedded_command_text(text: Any) -> str:
    """Normalize text that may contain shell commands embedded in CJK/natural language.

    Handles CJK‑ASCII boundary and compact command patterns.
    Replaces ``_normalize_embedded_command_text`` from ``followup_command_spec``.
    """
    raw = _as_str(text)
    if not raw:
        return ""

    result = raw

    # Stage 1: Insert space at every CJK↔ASCII boundary
    result = re.sub(
        r"([一-鿿぀-ヿ가-힯])(?=[A-Za-z0-9])",
        r"\1 ",
        result,
    )
    result = re.sub(
        r"([A-Za-z0-9])(?=[一-鿿぀-ヿ가-힯])",
        r"\1 ",
        result,
    )

    # Stage 2: SQL keyword gluing repair
    result = re.sub(r"(?i)EXPLAINSYNTAX", "EXPLAIN SYNTAX", result)
    result = re.sub(r"(?i)(CREATE\s+TABLE)(?=[A-Za-z0-9])", r"\1 ", result)
    result = re.sub(r"(?i)(SHOW\s+CREATE\s+TABLE)(?=[A-Za-z0-9])", r"\1 ", result)

    _space_after = [
        "SELECT", "FROM", "WHERE", "AND", "OR", "NOT", "IN", "ON",
        "AS", "WHEN", "THEN", "ELSE", "END", "CASE",
        "LIMIT", "OFFSET", "FORMAT", "UNION", "ALL", "DISTINCT",
        "HAVING", "PREWHERE", "ARRAY", "JOIN", "GLOBAL",
        "FINAL", "SETTINGS", "WITH", "TOTALS", "DESC", "ASC",
        "LEFT", "RIGHT", "INNER", "CROSS", "FULL", "OUTER", "SEMI", "ANTI",
        "ORDER", "GROUP", "PARTITION", "BY",
    ]
    for kw in _space_after:
        result = re.sub(rf"(?i){kw}(?=[A-Za-z0-9])", f"{kw} ", result)
    result = re.sub(r"(?i)\bBY(?=[a-z])", "BY ", result)

    # Stage 3: compact command normaliser — kubectl verb glue
    result = re.sub(r"\bkubectl(?=[a-z][-a-z0-9.]*)(?!\s)", "kubectl ", result)

    # Stage 4: clean up
    result = re.sub(r" {2,}", " ", result).strip()
    return result


# ── Simple validation helpers ────────────────────────────────────────────


def _build_exec_disabled_response(
    session_id: str = "",
    message_id: str = "",
    raw_command: str = "",
) -> Dict[str, Any]:
    """Build a response for when command execution is disabled."""
    return {
        "status": "exec_disabled",
        "session_id": session_id,
        "message_id": message_id,
        "command": _as_str(raw_command).strip(),
        "reason": "command_execution_disabled",
    }


def _validate_requested_command(raw_command: str) -> None:
    """Validate that a raw command is acceptable. Raises on invalid input."""
    if not _as_str(raw_command).strip():
        from ai.agent_runtime.exec_client import ExecServiceClientError
        raise ExecServiceClientError("command is empty")
    if len(raw_command) > 2000:
        from ai.agent_runtime.exec_client import ExecServiceClientError
        raise ExecServiceClientError("command too long (max 2000 chars)")


def _assert_command_is_suggested(
    command: str,
    content: str,
    metadata: Any = None,
) -> None:
    """Assert that *command* was suggested in the conversation context."""
    if not command or not content:
        return
    # Simplified: just check if the command text appears in the content
    if command in content:
        return
    # Also check normalized form
    normalized = normalize_command_line(command)
    if normalized and normalized in content:
        return
    # lenient: warn but don't block
    import logging
    logging.getLogger(__name__).warning(
        "Command %r not found in conversation content (lenient mode)", command,
    )


# ── Command classification (simplified) ───────────────────────────────────


def classify_followup_command(parts: list) -> Dict[str, Any]:
    """Classify a command token list (replaces ``_classify_followup_command``).

    Simplified version that uses the unified allowlist and write-verb detection.
    """
    if not parts:
        return {
            "command_type": "unknown",
            "risk_level": "high",
            "requires_write_permission": False,
            "supported": False,
            "reason": "命令为空",
        }

    head = _as_str(parts[0]).lower()
    command = " ".join(_as_str(p) for p in parts)

    if head in ALLOWED_HEADS:
        if head == "kubectl":
            write_kubectl_verbs = {
                "apply", "delete", "patch", "edit", "replace",
                "scale", "set", "annotate", "label", "create",
                "expose", "autoscale", "cordon", "uncordon", "drain", "taint",
            }
            verb = _as_str(parts[1]).lower() if len(parts) > 1 else ""
            if verb in write_kubectl_verbs:
                return {
                    "command_type": "repair",
                    "risk_level": "high",
                    "requires_write_permission": True,
                    "supported": True,
                    "reason": "Kubernetes 变更命令，可能影响线上环境",
                }
            if verb == "rollout" and len(parts) > 2:
                write_rollout = {"restart", "undo", "pause", "resume"}
                if _as_str(parts[2]).lower() in write_rollout:
                    return {
                        "command_type": "repair",
                        "risk_level": "high",
                        "requires_write_permission": True,
                        "supported": True,
                        "reason": "Kubernetes rollout 变更命令，可能影响线上环境",
                    }
            return {
                "command_type": "query",
                "risk_level": "low",
                "requires_write_permission": False,
                "supported": True,
                "reason": "Kubernetes 只读查询命令",
            }
        return {
            "command_type": "query",
            "risk_level": "low",
            "requires_write_permission": False,
            "supported": True,
            "reason": "",
        }

    return {
        "command_type": "unknown",
        "risk_level": "high",
        "requires_write_permission": False,
        "supported": False,
        "reason": f"命令头 '{head}' 不在允许列表中",
    }


__all__ = [
    "normalize_reason_code",
    "map_reason_group",
    "build_match_key",
    "build_self_repair_payload",
    "build_command_confirmation_message",
    "resolve_command_meta",
    "normalize_command_spec_compat",
    "compile_command_compat",
    "normalize_embedded_command_text",
    "classify_followup_command",
    "_build_exec_disabled_response",
    "_validate_requested_command",
    "_assert_command_is_suggested",
]
