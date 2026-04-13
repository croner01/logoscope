"""
Follow-up orchestration helpers for timeout profile, SSE events and readonly auto-exec.
"""

from __future__ import annotations

import asyncio
import hashlib
import os
import re
import time
from datetime import datetime, timedelta, timezone
from typing import Any, Dict, List, Optional

from ai.agent_runtime.exec_client import (
    ExecServiceClientError,
    create_command_run,
    get_command_run,
    iter_command_run_stream,
    precheck_command,
)
from ai.followup_command import (
    _FOLLOWUP_COMMAND_DEFAULT_TIMEOUT,
    _is_truthy_env,
    _normalize_followup_command_match_key,
    _normalize_followup_command_line,
    _resolve_followup_command_meta,
)
from ai.followup_command_spec import compile_followup_command_spec, normalize_followup_command_spec

_AUTO_EXEC_SAFE_QUERY_HEADS = {
    "kubectl",
    "curl",
    "clickhouse-client",
    "clickhouse",
    "rg",
    "grep",
    "cat",
    "tail",
    "head",
    "jq",
    "ls",
    "echo",
    "pwd",
}


def _as_str(value: Any, default: str = "") -> str:
    text = str(value).strip() if value is not None else ""
    return text if text else default


def _as_text(value: Any, default: str = "") -> str:
    if value is None:
        return default
    if isinstance(value, str):
        return value
    return str(value)


def _as_list(value: Any) -> List[Any]:
    return value if isinstance(value, list) else []


def _as_float(value: Any, default: float = 0.0) -> float:
    try:
        if value is None:
            return float(default)
        return float(value)
    except Exception:
        return float(default)


def _parse_optional_iso_datetime(value: Any) -> Optional[datetime]:
    text = _as_str(value).strip()
    if not text:
        return None
    candidate = text.replace(" ", "T")
    if candidate.endswith("Z"):
        candidate = f"{candidate[:-1]}+00:00"
    try:
        parsed = datetime.fromisoformat(candidate)
    except Exception:
        return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed.astimezone(timezone.utc)


def _to_utc_iso_text(value: datetime) -> str:
    return value.astimezone(timezone.utc).replace(microsecond=0).isoformat().replace("+00:00", "Z")


def _resolve_followup_evidence_window(
    analysis_context: Optional[Dict[str, Any]],
    *,
    default_minutes: int = 15,
) -> Dict[str, str]:
    context_payload = analysis_context if isinstance(analysis_context, dict) else {}

    def _parse_first_valid_iso(*candidates: Any) -> Optional[datetime]:
        for candidate in candidates:
            parsed = _parse_optional_iso_datetime(candidate)
            if parsed is not None:
                return parsed
        return None

    explicit_start = _parse_first_valid_iso(
        context_payload.get("request_flow_window_start"),
        context_payload.get("followup_related_start_time"),
        context_payload.get("evidence_window_start"),
    )
    explicit_end = _parse_first_valid_iso(
        context_payload.get("request_flow_window_end"),
        context_payload.get("followup_related_end_time"),
        context_payload.get("evidence_window_end"),
    )
    if explicit_start and explicit_end and explicit_start <= explicit_end:
        return {
            "start_iso": _to_utc_iso_text(explicit_start),
            "end_iso": _to_utc_iso_text(explicit_end),
        }

    anchor_candidates = [
        context_payload.get("source_log_timestamp"),
        context_payload.get("related_log_anchor_timestamp"),
        context_payload.get("followup_related_anchor_utc"),
        context_payload.get("timestamp"),
    ]
    anchor_dt: Optional[datetime] = None
    for candidate in anchor_candidates:
        parsed = _parse_optional_iso_datetime(candidate)
        if parsed is not None:
            anchor_dt = parsed
            break
    if anchor_dt is None:
        return {}

    raw_minutes = int(_as_float(context_payload.get("request_flow_window_minutes"), default_minutes))
    window_minutes = max(1, min(120, raw_minutes))
    start_dt = anchor_dt - timedelta(minutes=window_minutes)
    end_dt = anchor_dt + timedelta(minutes=window_minutes)
    return {
        "start_iso": _to_utc_iso_text(start_dt),
        "end_iso": _to_utc_iso_text(end_dt),
    }


def _build_k8s_logs_evidence_command(
    *,
    namespace: str,
    service_name: str,
    window_start_iso: str = "",
) -> str:
    target_service = _as_str(service_name).strip() or "query-service"
    if window_start_iso:
        return f"kubectl -n {namespace} logs -l app={target_service} --since-time={window_start_iso} --tail=200"
    return f"kubectl -n {namespace} logs -l app={target_service} --since=15m --tail=200"


def _build_clickhouse_query_log_evidence_command(
    *,
    namespace: str,
    window_start_iso: str = "",
    window_end_iso: str = "",
) -> str:
    if window_start_iso and window_end_iso:
        return (
            f"kubectl -n {namespace} exec deploy/clickhouse -- clickhouse-client --query "
            "\"SELECT event_time,query_id,exception_code,exception,query "
            "FROM system.query_log "
            f"WHERE event_time >= toDateTime64('{window_start_iso}', 9, 'UTC') "
            f"AND event_time <= toDateTime64('{window_end_iso}', 9, 'UTC') "
            "ORDER BY event_time DESC LIMIT 20\""
        )
    return (
        f"kubectl -n {namespace} exec deploy/clickhouse -- clickhouse-client --query "
        "\"SELECT event_time,query_id,exception_code,exception,query "
        "FROM system.query_log "
        "WHERE event_time >= now() - INTERVAL 15 MINUTE "
        "ORDER BY event_time DESC LIMIT 20\""
    )


def _build_clickhouse_processes_evidence_command(
    *,
    namespace: str,
    window_start_iso: str = "",
    window_end_iso: str = "",
) -> str:
    if window_start_iso and window_end_iso:
        return (
            f"kubectl -n {namespace} exec deploy/clickhouse -- clickhouse-client --query "
            "\"SELECT "
            f"toDateTime64('{window_start_iso}', 9, 'UTC') AS evidence_window_start, "
            f"toDateTime64('{window_end_iso}', 9, 'UTC') AS evidence_window_end, "
            "now() AS collected_at, query_id, elapsed, read_rows, read_bytes, memory_usage, query "
            "FROM system.processes ORDER BY elapsed DESC LIMIT 20\""
        )
    return (
        f"kubectl -n {namespace} exec deploy/clickhouse -- clickhouse-client --query "
        "\"SELECT now() AS collected_at, query_id, elapsed, read_rows, read_bytes, memory_usage, query "
        "FROM system.processes ORDER BY elapsed DESC LIMIT 20\""
    )


def _build_clickhouse_metrics_evidence_command(
    *,
    namespace: str,
    window_start_iso: str = "",
    window_end_iso: str = "",
) -> str:
    if window_start_iso and window_end_iso:
        return (
            f"kubectl -n {namespace} exec deploy/clickhouse -- clickhouse-client --query "
            "\"SELECT "
            f"toDateTime64('{window_start_iso}', 9, 'UTC') AS evidence_window_start, "
            f"toDateTime64('{window_end_iso}', 9, 'UTC') AS evidence_window_end, "
            "now() AS collected_at, metric, value FROM system.metrics "
            "WHERE metric IN ('Query','Merge','BackgroundMergesAndMutationsPoolTask','DelayedInserts') "
            "ORDER BY metric\""
        )
    return (
        f"kubectl -n {namespace} exec deploy/clickhouse -- clickhouse-client --query "
        "\"SELECT now() AS collected_at, metric, value FROM system.metrics "
        "WHERE metric IN ('Query','Merge','BackgroundMergesAndMutationsPoolTask','DelayedInserts') "
        "ORDER BY metric\""
    )


def _derive_template_expected_signal(command: str) -> str:
    safe_command = _normalize_followup_command_line(command).strip().lower()
    if "from system.processes" in safe_command:
        return "命中故障时间窗内的长耗时查询、读行量或内存占用异常。"
    if "from system.metrics" in safe_command:
        return "命中与慢查询相关的后台任务或并发指标异常。"
    if "system.query_log" in safe_command:
        return "命中故障时间窗内的 query_id、exception_code、exception 或慢查询样本。"
    if "kubectl" in safe_command and " logs " in f" {safe_command} ":
        return "命中故障时间窗内的 ERROR/WARN/Traceback/慢查询告警关键日志。"
    if "top pod" in safe_command:
        return "命中故障时间窗内的 CPU/内存资源异常。"
    if "describe pod" in safe_command:
        return "命中与故障相关的配置、重启或事件异常。"
    return "返回可直接确认或排除当前根因候选的关键证据。"


def _build_evidence_gap_keywords(evidence_gaps: List[str]) -> List[str]:
    keywords: List[str] = []
    seen: set[str] = set()
    for item in _as_list(evidence_gaps):
        gap_text = _as_str(item).strip().lower()
        if not gap_text:
            continue
        for token in re.findall(r"[a-z0-9_./:-]{3,}|[\u4e00-\u9fff]{2,}", gap_text, flags=re.IGNORECASE):
            normalized = _as_str(token).strip().lower()
            if not normalized or normalized in seen:
                continue
            seen.add(normalized)
            keywords.append(normalized)
            if len(keywords) >= 64:
                return keywords
    return keywords


def _score_action_relevance_to_gaps(action: Dict[str, Any], gap_keywords: List[str]) -> int:
    if not isinstance(action, dict) or not gap_keywords:
        return 0
    text_blob = " ".join(
        [
            _as_str(action.get("title")).lower(),
            _as_str(action.get("purpose")).lower(),
            _as_str(action.get("question")).lower(),
            _as_str(action.get("command")).lower(),
            _as_str(action.get("reason")).lower(),
        ]
    ).strip()
    if not text_blob:
        return 0
    score = 0
    for keyword in gap_keywords:
        if keyword and keyword in text_blob:
            score += 3 if len(keyword) >= 6 else 1
    return score


def _select_iteration_actions_by_evidence_gaps(
    actions: List[Dict[str, Any]],
    evidence_gaps: List[str],
    *,
    max_items: int = 8,
) -> List[Dict[str, Any]]:
    safe_actions = [item for item in _as_list(actions) if isinstance(item, dict)]
    if not safe_actions:
        return []
    safe_limit = max(1, min(int(max_items or len(safe_actions)), len(safe_actions)))
    safe_gaps = [_as_str(item).strip() for item in _as_list(evidence_gaps) if _as_str(item).strip()]
    if not safe_gaps:
        return safe_actions[:safe_limit]
    gap_keywords = _build_evidence_gap_keywords(safe_gaps)
    if not gap_keywords:
        return []
    scored: List[tuple[int, int, Dict[str, Any]]] = []
    for index, action in enumerate(safe_actions):
        score = _score_action_relevance_to_gaps(action, gap_keywords)
        if score <= 0:
            continue
        scored.append((score, index, action))
    if not scored:
        return []
    scored.sort(key=lambda item: (-item[0], item[1]))
    return [item[2] for item in scored[:safe_limit]]


def _summarize_iteration_actions(actions: List[Dict[str, Any]], *, max_items: int = 3) -> str:
    labels: List[str] = []
    for action in _as_list(actions):
        action_dict = action if isinstance(action, dict) else {}
        command = _normalize_followup_command_line(_as_str(action_dict.get("command"))).strip()
        action_text = _normalize_followup_command_line(_as_str(action_dict.get("action"))).strip()
        title = _normalize_followup_command_line(_as_str(action_dict.get("title"))).strip()
        label = command or action_text or title
        if not label:
            continue
        labels.append(label[:100])
        if len(labels) >= max(1, int(max_items or 3)):
            break
    return "；".join(labels)


def _is_low_trust_non_executable_action(action_dict: Dict[str, Any]) -> bool:
    source = _as_str(action_dict.get("source")).strip().lower()
    reason = _as_str(action_dict.get("reason")).strip().lower()
    if source == "answer_command":
        return True
    return "answer_command_requires_structured_action" in reason


def _normalize_non_executable_template_command(command: str) -> str:
    safe_command = _normalize_followup_command_line(command).strip()
    if not safe_command:
        return ""
    try:
        command_meta, _ = _resolve_followup_command_meta(safe_command)
    except Exception:
        return ""
    if (
        not bool(command_meta.get("supported"))
        or _as_str(command_meta.get("command_type")).strip().lower() != "query"
    ):
        return ""
    compiled = compile_followup_command_spec(
        {
            "tool": "generic_exec",
            "args": {
                "command": safe_command,
                "timeout_s": 30,
            },
        },
        run_sql_preflight=False,
    )
    if not bool(compiled.get("ok")):
        return ""
    compiled_command = _normalize_followup_command_line(_as_str(compiled.get("command"))).strip()
    compiled_spec = normalize_followup_command_spec(compiled.get("command_spec"))
    if not compiled_command or not compiled_spec:
        return ""
    args = compiled_spec.get("args") if isinstance(compiled_spec.get("args"), dict) else {}
    argv = args.get("command_argv") if isinstance(args, dict) else None
    if not isinstance(argv, list) or not argv:
        return ""
    head = _as_str(argv[0]).strip().lower()
    if head not in _AUTO_EXEC_SAFE_QUERY_HEADS:
        return ""
    return compiled_command


def _build_non_executable_command_templates(
    actions: List[Dict[str, Any]],
    *,
    analysis_context: Optional[Dict[str, Any]] = None,
    max_items: int = 3,
) -> List[str]:
    """在缺少可执行候选命令时，给出可补全 command_spec 的命令模板。"""
    safe_limit = max(1, min(int(max_items or 3), 6))
    templates: List[str] = []
    seen: set[str] = set()
    context_payload = analysis_context if isinstance(analysis_context, dict) else {}
    namespace = _as_str(context_payload.get("namespace"), "islap") or "islap"
    service_name = _as_str(context_payload.get("service_name")).strip()
    trace_id = _as_str(context_payload.get("trace_id")).strip()
    evidence_window = _resolve_followup_evidence_window(context_payload)
    window_start_iso = _as_str(evidence_window.get("start_iso")).strip()
    window_end_iso = _as_str(evidence_window.get("end_iso")).strip()

    def _append(command: str) -> None:
        safe_command = _as_str(command).strip()
        if not safe_command or safe_command in seen:
            return
        seen.add(safe_command)
        templates.append(safe_command)

    def _append_context_defaults() -> None:
        if service_name:
            _append(
                _build_k8s_logs_evidence_command(
                    namespace=namespace,
                    service_name=service_name,
                    window_start_iso=window_start_iso,
                )
            )
        elif trace_id:
            _append(
                _build_k8s_logs_evidence_command(
                    namespace=namespace,
                    service_name="query-service",
                    window_start_iso=window_start_iso,
                )
            )

    for action in _as_list(actions):
        if len(templates) >= safe_limit:
            break
        action_dict = action if isinstance(action, dict) else {}
        if bool(action_dict.get("executable")):
            continue
        command = _normalize_followup_command_line(_as_str(action_dict.get("command"))).strip()
        if command:
            if _is_low_trust_non_executable_action(action_dict):
                command = ""
            else:
                normalized_template = _normalize_non_executable_template_command(command)
                if normalized_template:
                    _append(normalized_template)
                    continue
        text_blob = " ".join(
            [
                _as_str(action_dict.get("title")),
                _as_str(action_dict.get("purpose")),
                _as_str(action_dict.get("reason")),
            ]
        ).lower()
        if any(token in text_blob for token in ["temporal", "日志", "trace", "error", "cancel"]):
            _append_context_defaults()
            continue
        if any(token in text_blob for token in ["进程", "process", "running query", "长时间运行"]):
            _append(
                _build_clickhouse_processes_evidence_command(
                    namespace=namespace,
                    window_start_iso=window_start_iso,
                    window_end_iso=window_end_iso,
                )
            )
            continue
        if any(token in text_blob for token in ["指标", "metric", "merge", "mutation", "后台任务"]):
            _append(
                _build_clickhouse_metrics_evidence_command(
                    namespace=namespace,
                    window_start_iso=window_start_iso,
                    window_end_iso=window_end_iso,
                )
            )
            continue
        if any(token in text_blob for token in ["clickhouse", "慢查询", "锁", "sql", "query_log", "code:184"]):
            _append(
                _build_clickhouse_query_log_evidence_command(
                    namespace=namespace,
                    window_start_iso=window_start_iso,
                    window_end_iso=window_end_iso,
                )
            )
            continue
        if any(token in text_blob for token in ["连接池", "pool", "timeout", "配置"]):
            _append(f"kubectl -n {namespace} describe pod -l app={service_name or 'query-service'}")
            continue
        if any(token in text_blob for token in ["cpu", "内存", "网络", "资源"]):
            _append(f"kubectl -n {namespace} top pod -l app={service_name or 'query-service'}")

    if not templates:
        _append_context_defaults()
    if not templates:
        _append(f"kubectl -n {namespace} get pods --show-labels")
    return templates[:safe_limit]


def _is_low_signal_template_command(command: str) -> bool:
    safe_command = _normalize_followup_command_line(command).strip().lower()
    if not safe_command:
        return True
    if re.match(r"^kubectl\s+-n\s+[a-z0-9-]+\s+get\s+pods\s+--show-labels$", safe_command):
        return True
    return False


def _build_template_action_id(command: str) -> str:
    normalized = _normalize_followup_command_line(command).strip()
    if not normalized:
        return "tmpl-unknown"
    digest = hashlib.sha1(normalized.encode("utf-8")).hexdigest()[:8]
    return f"tmpl-{digest}"


def _build_structured_template_actions(
    *,
    actions: List[Dict[str, Any]],
    analysis_context: Optional[Dict[str, Any]] = None,
    max_items: int = 2,
) -> List[Dict[str, Any]]:
    templates = _build_non_executable_command_templates(
        actions,
        analysis_context=analysis_context,
        max_items=max_items,
    )
    if not templates:
        return []
    normalized_templates = [
        _normalize_non_executable_template_command(item)
        for item in templates
    ]
    non_low_signal_templates = [
        item for item in normalized_templates
        if item and not _is_low_signal_template_command(item)
    ]
    has_reflection_action = any(
        _as_str((item if isinstance(item, dict) else {}).get("source")).strip().lower() == "reflection"
        for item in _as_list(actions)
    )
    allow_low_signal_fallback = (not non_low_signal_templates) and has_reflection_action

    existing_ids = {
        _as_str((item if isinstance(item, dict) else {}).get("id")).strip()
        for item in _as_list(actions)
        if _as_str((item if isinstance(item, dict) else {}).get("id")).strip()
    }
    existing_id_to_command: Dict[str, str] = {}
    existing_command_to_id: Dict[str, str] = {}
    for item in _as_list(actions):
        item_dict = item if isinstance(item, dict) else {}
        existing_action_id = _as_str(item_dict.get("id")).strip()
        existing_command = _normalize_non_executable_template_command(item_dict.get("command"))
        if existing_action_id:
            existing_id_to_command[existing_action_id] = existing_command
        if existing_command:
            existing_command_to_id[existing_command] = existing_action_id
    max_priority = 0
    for item in _as_list(actions):
        if not isinstance(item, dict):
            continue
        max_priority = max(max_priority, int(_as_float(item.get("priority"), 0)))

    built: List[Dict[str, Any]] = []
    seen_commands: set[str] = set()
    context_payload = analysis_context if isinstance(analysis_context, dict) else {}
    evidence_window = _resolve_followup_evidence_window(context_payload)
    window_start_iso = _as_str(evidence_window.get("start_iso")).strip()
    window_end_iso = _as_str(evidence_window.get("end_iso")).strip()
    for command in templates:
        normalized_command = _normalize_non_executable_template_command(command)
        if not normalized_command:
            continue
        if _is_low_signal_template_command(normalized_command) and not allow_low_signal_fallback:
            continue
        if normalized_command in seen_commands:
            continue
        compiled = compile_followup_command_spec(
            {
                "tool": "generic_exec",
                "args": {
                    "command": normalized_command,
                    "timeout_s": 30,
                },
            },
            run_sql_preflight=False,
        )
        if not bool(compiled.get("ok")):
            continue
        command_spec = normalize_followup_command_spec(compiled.get("command_spec"))
        compiled_command = _normalize_followup_command_line(_as_str(compiled.get("command")))
        if not command_spec or not compiled_command:
            continue
        if compiled_command in existing_command_to_id:
            # Avoid recreating the same template action across replan iterations.
            continue
        action_id = _build_template_action_id(compiled_command)
        if action_id in existing_ids and existing_id_to_command.get(action_id) != compiled_command:
            suffix = 2
            candidate_action_id = f"{action_id}-{suffix}"
            while candidate_action_id in existing_ids and existing_id_to_command.get(candidate_action_id) != compiled_command:
                suffix += 1
                candidate_action_id = f"{action_id}-{suffix}"
            action_id = candidate_action_id
        existing_ids.add(action_id)
        existing_id_to_command[action_id] = compiled_command
        existing_command_to_id[compiled_command] = action_id
        seen_commands.add(compiled_command)
        built.append(
            {
                "id": action_id,
                "source": "template_command",
                "priority": max_priority + len(built) + 1,
                "title": f"自动补证据命令：{compiled_command[:120]}",
                "purpose": "从重规划模板自动生成并执行证据采集命令",
                "question": "",
                "action_type": "query",
                "command": compiled_command,
                "command_spec": command_spec,
                "command_type": "query",
                "risk_level": "low",
                "executable": True,
                "requires_confirmation": False,
                "requires_write_permission": False,
                "requires_elevation": False,
                "reason": "structured_template_ready_for_auto_exec",
                "expected_signal": _derive_template_expected_signal(compiled_command),
                "evidence_window_start": window_start_iso,
                "evidence_window_end": window_end_iso,
            }
        )
    return built[: max(1, int(max_items or 2))]


def _build_command_plan_detail(
    *,
    command: str,
    purpose: str,
    reason: str,
    expected_outcome: str,
) -> str:
    safe_command = _as_str(command).strip()
    safe_purpose = _as_str(purpose).strip() or "补齐当前证据缺口并推进排查"
    safe_reason = _as_str(reason).strip() or "该命令与当前证据缺口匹配，先执行以获取关键事实。"
    safe_expected = _as_str(expected_outcome).strip() or "输出可用于判断下一步是否收敛。"
    lines = [
        "执行前计划：",
        f"1. 计划命令：{safe_command}",
        f"2. 执行目的：{safe_purpose}",
        f"3. 执行原因：{safe_reason}",
        f"4. 预期结果：{safe_expected}",
    ]
    return "\n".join(lines)


def _is_runtime_pause_signal(exc: BaseException) -> bool:
    if exc is None:
        return False
    if bool(getattr(exc, "is_runtime_pause_signal", False)):
        return True
    return exc.__class__.__name__ == "_RuntimePauseForPendingAction"


def _resolve_followup_timeout_profile() -> Dict[str, int]:
    """解析追问链路超时预算（阶段预算 + 总预算）。"""
    llm_total_timeout_seconds = max(
        5,
        int(
            _as_float(
                os.getenv(
                    "AI_FOLLOWUP_LLM_TOTAL_TIMEOUT_SECONDS",
                    os.getenv("AI_FOLLOWUP_LLM_TIMEOUT_SECONDS", "90"),
                ),
                90,
            )
        ),
    )
    return {
        "request_deadline_seconds": max(20, int(_as_float(os.getenv("AI_FOLLOWUP_REQUEST_DEADLINE_SECONDS"), 150))),
        "session_prepare_timeout_seconds": max(
            3,
            int(_as_float(os.getenv("AI_FOLLOWUP_SESSION_PREPARE_TIMEOUT_SECONDS"), 12)),
        ),
        "history_load_timeout_seconds": max(3, int(_as_float(os.getenv("AI_FOLLOWUP_HISTORY_LOAD_TIMEOUT_SECONDS"), 12))),
        "long_term_memory_timeout_seconds": max(
            3,
            int(_as_float(os.getenv("AI_FOLLOWUP_LONG_TERM_MEMORY_TIMEOUT_SECONDS"), 15)),
        ),
        "react_memory_timeout_seconds": max(
            2,
            int(_as_float(os.getenv("AI_FOLLOWUP_REACT_MEMORY_TIMEOUT_SECONDS"), 8)),
        ),
        "llm_first_token_timeout_seconds": max(
            1,
            int(_as_float(os.getenv("AI_FOLLOWUP_LLM_FIRST_TOKEN_TIMEOUT_SECONDS"), 20)),
        ),
        "llm_total_timeout_seconds": llm_total_timeout_seconds,
        "persist_timeout_seconds": max(3, int(_as_float(os.getenv("AI_FOLLOWUP_PERSIST_TIMEOUT_SECONDS"), 12))),
    }


def _resolve_followup_auto_exec_readonly_enabled() -> bool:
    return _is_truthy_env("AI_FOLLOWUP_AUTO_EXEC_READONLY_ENABLED", True)


def _resolve_followup_auto_exec_max_actions() -> int:
    return max(0, min(5, int(_as_float(os.getenv("AI_FOLLOWUP_AUTO_EXEC_READONLY_MAX_ACTIONS"), 2))))


def _resolve_followup_auto_exec_timeout_seconds() -> int:
    return max(
        3,
        min(
            120,
            int(
                _as_float(
                    os.getenv("AI_FOLLOWUP_AUTO_EXEC_COMMAND_TIMEOUT_SECONDS"),
                    _FOLLOWUP_COMMAND_DEFAULT_TIMEOUT,
                )
            ),
        ),
    )


def _describe_template_action_execution_mode(*, allow_auto_exec_readonly: bool) -> str:
    """Describe how generated template actions will be handled in the current run."""
    if not allow_auto_exec_readonly:
        return "当前运行已禁用只读自动执行，请手动执行或开启自动执行后继续。"
    if not _resolve_followup_auto_exec_readonly_enabled():
        return "系统当前已关闭只读自动执行，请手动执行模板命令后继续。"
    if not _is_truthy_env("AI_FOLLOWUP_COMMAND_EXEC_ENABLED", True):
        return "命令执行链路当前不可用，请手动执行模板命令后继续。"
    return "已进入自动执行链路。"


def _require_spec_for_repair_enabled() -> bool:
    return _is_truthy_env("AI_FOLLOWUP_COMMAND_REQUIRE_SPEC_FOR_REPAIR", False)


def _resolve_followup_react_max_iterations() -> int:
    return max(1, min(4, int(_as_float(os.getenv("AI_FOLLOWUP_REACT_MAX_ITERATIONS"), 2))))


def _resolve_followup_react_retry_per_command() -> int:
    return max(0, min(3, int(_as_float(os.getenv("AI_FOLLOWUP_REACT_RETRY_PER_COMMAND"), 1))))


def _remaining_timeout(deadline_ts: float, floor_seconds: float = 0.5) -> float:
    return max(float(floor_seconds), deadline_ts - time.perf_counter())


def _format_sse_event(event: str, payload: Dict[str, Any]) -> str:
    import json

    data = json.dumps(payload if isinstance(payload, dict) else {}, ensure_ascii=False)
    safe_event = _as_str(event, "message").replace("\n", "").strip() or "message"
    return f"event: {safe_event}\ndata: {data}\n\n"


async def _emit_followup_event(
    event_callback: Optional[Any],
    event_name: str,
    payload: Dict[str, Any],
    logger: Optional[Any] = None,
) -> None:
    if not callable(event_callback):
        return
    try:
        maybe_awaitable = event_callback(event_name, payload if isinstance(payload, dict) else {})
        if asyncio.iscoroutine(maybe_awaitable):
            await maybe_awaitable
    except Exception as exc:
        if _is_runtime_pause_signal(exc):
            raise
        if logger is not None:
            logger.warning("Emit follow-up stream event failed: %s", exc)


def _build_followup_auto_exec_skip_observation(
    *,
    session_id: str,
    message_id: str,
    action_id: str,
    command: str,
    reason: str,
    reason_code: str = "",
    reused_command_run_id: str = "",
    reused_evidence_ids: Optional[List[str]] = None,
) -> Dict[str, Any]:
    safe_reason_code = _as_str(reason_code).strip().lower()
    if not safe_reason_code:
        lowered_reason = _as_str(reason).strip().lower()
        if "已执行过该命令" in lowered_reason or "重复执行" in lowered_reason:
            safe_reason_code = "duplicate_skipped"
        elif "执行网关未就绪" in lowered_reason:
            safe_reason_code = "backend_unready"
        elif "预检" in lowered_reason:
            safe_reason_code = "precheck_blocked"
        elif "受控只读命令" in lowered_reason:
            safe_reason_code = "policy_blocked"
        else:
            safe_reason_code = "skipped_unknown"
    safe_reused_command_run_id = _as_str(reused_command_run_id).strip()
    safe_reused_evidence_ids = [
        _as_str(item).strip()
        for item in _as_list(reused_evidence_ids)
        if _as_str(item).strip()
    ]
    if safe_reused_command_run_id and safe_reused_command_run_id not in safe_reused_evidence_ids:
        safe_reused_evidence_ids.append(safe_reused_command_run_id)
    return {
        "status": "skipped",
        "session_id": session_id,
        "message_id": message_id,
        "action_id": action_id,
        "command": command,
        "message": reason,
        "reason_code": safe_reason_code,
        "auto_executed": False,
        "command_run_id": safe_reused_command_run_id,
        "reused_command_run_id": safe_reused_command_run_id,
        "reused_evidence_ids": safe_reused_evidence_ids,
        "evidence_reuse": bool(safe_reused_evidence_ids),
    }


def _resolve_latest_success_observation(
    *,
    command: str,
    observations: List[Dict[str, Any]],
) -> Optional[Dict[str, Any]]:
    command_key = _normalize_followup_command_match_key(command)
    if not command_key:
        return None
    latest: Optional[Dict[str, Any]] = None
    for item in _as_list(observations):
        obs = item if isinstance(item, dict) else {}
        if _normalize_followup_command_match_key(_as_str(obs.get("command"))) != command_key:
            continue
        status = _as_str(obs.get("status")).strip().lower()
        exit_code = int(_as_float(obs.get("exit_code"), 0))
        if status != "executed" or exit_code != 0:
            continue
        latest = obs
    return latest


def _build_followup_auto_exec_failed_observation(
    *,
    session_id: str,
    message_id: str,
    action_id: str,
    command: str,
    command_meta: Dict[str, Any],
    message: str,
    auto_executed: bool = False,
) -> Dict[str, Any]:
    return {
        "status": "failed",
        "session_id": session_id,
        "message_id": message_id,
        "action_id": action_id,
        "command": command,
        "command_type": _as_str(command_meta.get("command_type"), "query"),
        "risk_level": _as_str(command_meta.get("risk_level"), "low"),
        "message": message,
        "auto_executed": auto_executed,
    }


def _unwrap_exec_stream_payload(event_payload: Dict[str, Any]) -> Dict[str, Any]:
    safe_payload = event_payload if isinstance(event_payload, dict) else {}
    nested_payload = safe_payload.get("payload")
    if isinstance(nested_payload, dict):
        return nested_payload
    return safe_payload


def _build_followup_exec_terminal_observation_from_run(
    *,
    session_id: str,
    message_id: str,
    action_id: str,
    command: str,
    command_meta: Dict[str, Any],
    run_payload: Dict[str, Any],
) -> Optional[Dict[str, Any]]:
    safe_run = run_payload if isinstance(run_payload, dict) else {}
    if not safe_run:
        return None
    final_status = _as_str(safe_run.get("status"), "completed").lower()
    exit_code = int(_as_float(safe_run.get("exit_code"), 0))
    timed_out = bool(safe_run.get("timed_out")) or exit_code in {-9, -15}
    if timed_out:
        obs_status = "timed_out"
    else:
        obs_status = "executed" if final_status == "completed" else final_status
    stderr_text = _as_text(safe_run.get("stderr"))
    stdout_text = _as_text(safe_run.get("stdout"))
    if obs_status == "executed" and exit_code == 0:
        message = "命令执行完成"
    elif obs_status == "cancelled":
        message = "命令已取消"
    elif timed_out:
        message = "命令执行超时"
    else:
        message = stderr_text or "命令执行失败"
    return {
        "session_id": session_id,
        "message_id": message_id,
        "action_id": action_id,
        "command": command,
        "command_type": _as_str(command_meta.get("command_type"), "query"),
        "risk_level": _as_str(command_meta.get("risk_level"), "low"),
        "command_run_id": _as_str(safe_run.get("command_run_id") or safe_run.get("run_id")),
        "auto_executed": True,
        "status": obs_status,
        "exit_code": exit_code,
        "timed_out": timed_out,
        "stdout": stdout_text,
        "stderr": stderr_text,
        "duration_ms": int(_as_float(safe_run.get("duration_ms"), 0)),
        "output_truncated": bool(safe_run.get("output_truncated")),
        "message": message,
        "stream_event": "command_finished",
    }


def _build_followup_exec_stream_observation(
    *,
    session_id: str,
    message_id: str,
    action_id: str,
    command: str,
    command_meta: Dict[str, Any],
    event_name: str,
    event_payload: Dict[str, Any],
) -> Optional[Dict[str, Any]]:
    safe_event = _as_str(event_name).lower()
    safe_payload = _unwrap_exec_stream_payload(event_payload if isinstance(event_payload, dict) else {})
    run_payload = safe_payload.get("run") if isinstance(safe_payload.get("run"), dict) else {}
    command_run_id = _as_str(safe_payload.get("command_run_id") or run_payload.get("command_run_id"))
    base = {
        "session_id": session_id,
        "message_id": message_id,
        "action_id": action_id,
        "command": command,
        "command_type": _as_str(command_meta.get("command_type"), "query"),
        "risk_level": _as_str(command_meta.get("risk_level"), "low"),
        "command_run_id": command_run_id,
        "auto_executed": True,
    }

    if safe_event == "command_started":
        return {
            **base,
            "status": "running",
            "message": "命令开始执行",
            "stream_event": safe_event,
        }

    if safe_event == "command_output_delta":
        text = _as_text(safe_payload.get("text"))
        stream = _as_str(safe_payload.get("stream"), "stdout")
        return {
            **base,
            "status": "running",
            "message": text or f"{stream} 输出更新",
            "detail": text,
            "text": text,
            "stream": stream,
            "output_truncated": bool(safe_payload.get("output_truncated")),
            "stream_event": safe_event,
        }

    if safe_event not in {"command_finished", "command_cancelled"}:
        return None

    final_status = _as_str(safe_payload.get("status") or run_payload.get("status"), "completed").lower()
    exit_code = int(_as_float(run_payload.get("exit_code"), 0))
    timed_out = bool(run_payload.get("timed_out")) or exit_code in {-9, -15}
    if timed_out:
        obs_status = "timed_out"
    else:
        obs_status = "executed" if final_status == "completed" else final_status
    stderr_text = _as_text(run_payload.get("stderr"))
    stdout_text = _as_text(run_payload.get("stdout"))
    if obs_status == "executed" and exit_code == 0:
        message = "命令执行完成"
    elif obs_status == "cancelled":
        message = "命令已取消"
    elif timed_out:
        message = "命令执行超时"
    else:
        message = stderr_text or "命令执行失败"
    return {
        **base,
        "status": obs_status,
        "exit_code": exit_code,
        "timed_out": timed_out,
        "stdout": stdout_text,
        "stderr": stderr_text,
        "duration_ms": int(_as_float(run_payload.get("duration_ms"), 0)),
        "output_truncated": bool(run_payload.get("output_truncated")),
        "message": message,
        "stream_event": safe_event,
    }


async def _stream_followup_exec_runtime(
    *,
    session_id: str,
    message_id: str,
    action_id: str,
    command: str,
    command_meta: Dict[str, Any],
    exec_run_id: str,
    event_callback: Optional[Any] = None,
    logger: Optional[Any] = None,
) -> Optional[Dict[str, Any]]:
    final_observation: Optional[Dict[str, Any]] = None

    async def _recover_terminal_observation() -> Optional[Dict[str, Any]]:
        try:
            snapshot = await get_command_run(exec_run_id, timeout_seconds=8)
            run_payload = snapshot.get("run") if isinstance(snapshot.get("run"), dict) else {}
            terminal_observation = _build_followup_exec_terminal_observation_from_run(
                session_id=session_id,
                message_id=message_id,
                action_id=action_id,
                command=command,
                command_meta=command_meta,
                run_payload=run_payload,
            )
            if isinstance(terminal_observation, dict):
                await _emit_followup_event(event_callback, "observation", terminal_observation, logger=logger)
                return terminal_observation
        except ExecServiceClientError:
            return None
        return None

    async def _handle_event(item: Dict[str, Any]) -> None:
        nonlocal final_observation
        event_name = _as_str(item.get("event"))
        event_payload = item.get("data") if isinstance(item.get("data"), dict) else {}
        observation = _build_followup_exec_stream_observation(
            session_id=session_id,
            message_id=message_id,
            action_id=action_id,
            command=command,
            command_meta=command_meta,
            event_name=event_name,
            event_payload=event_payload,
        )
        if not isinstance(observation, dict):
            return
        if observation.get("status") not in {"running", ""}:
            final_observation = observation
        await _emit_followup_event(event_callback, "observation", observation, logger=logger)

    if os.environ.get("PYTEST_CURRENT_TEST") is not None:
        for item in iter_command_run_stream(exec_run_id, after_seq=0):
            if isinstance(item, dict):
                await _handle_event(item)
        if not isinstance(final_observation, dict):
            final_observation = await _recover_terminal_observation()
        return final_observation

    loop = asyncio.get_running_loop()
    queue: asyncio.Queue = asyncio.Queue()
    sentinel = object()

    def _produce() -> None:
        try:
            for item in iter_command_run_stream(exec_run_id, after_seq=0):
                loop.call_soon_threadsafe(queue.put_nowait, item)
        except Exception as exc:
            loop.call_soon_threadsafe(
                queue.put_nowait,
                {
                    "event": "stream_error",
                    "data": {
                        "detail": _as_str(exc),
                    },
                },
            )
        finally:
            loop.call_soon_threadsafe(queue.put_nowait, sentinel)

    producer_task = asyncio.create_task(asyncio.to_thread(_produce))
    try:
        while True:
            item = await queue.get()
            if item is sentinel:
                break
            if not isinstance(item, dict):
                continue
            if _as_str(item.get("event")).lower() == "stream_error":
                detail = _as_str((item.get("data") or {}).get("detail"), "exec-service stream failed")
                raise ExecServiceClientError(detail)
            await _handle_event(item)
    finally:
        await producer_task

    if not isinstance(final_observation, dict):
        final_observation = await _recover_terminal_observation()

    return final_observation


def _is_auto_exec_safe_query_command(raw_command: str, command_meta: Dict[str, Any]) -> bool:
    _ = raw_command
    if _as_str(command_meta.get("command_type")).lower() != "query":
        return False
    command_spec = (
        command_meta.get("command_spec")
        if isinstance(command_meta.get("command_spec"), dict)
        else {}
    )
    safe_spec = normalize_followup_command_spec(command_spec)
    if not safe_spec:
        return False
    tool = _as_str(safe_spec.get("tool")).strip().lower()
    if tool in {"kubectl_clickhouse_query", "k8s_clickhouse_query", "clickhouse_query"}:
        return True
    if tool != "generic_exec":
        return False
    args = safe_spec.get("args") if isinstance(safe_spec.get("args"), dict) else {}
    argv_value = args.get("command_argv") if isinstance(args, dict) else None
    if not isinstance(argv_value, list):
        argv_value = safe_spec.get("command_argv")
    if not isinstance(argv_value, list):
        return False
    argv = [_as_str(item).strip() for item in argv_value if _as_str(item).strip()]
    if not argv:
        return False
    if len(argv) > 64:
        return False
    head = _as_str(argv[0]).lower()
    return head in _AUTO_EXEC_SAFE_QUERY_HEADS


async def _run_followup_readonly_auto_exec(
    *,
    session_id: str,
    message_id: str,
    actions: List[Dict[str, Any]],
    run_blocking: Any,
    allow_auto_exec_readonly: bool = True,
    executed_commands: Optional[set[str]] = None,
    prior_observations: Optional[List[Dict[str, Any]]] = None,
    event_callback: Optional[Any] = None,
    logger: Optional[Any] = None,
) -> List[Dict[str, Any]]:
    """自动执行追问动作中的只读查询命令。"""
    if not allow_auto_exec_readonly:
        return []
    if not _resolve_followup_auto_exec_readonly_enabled():
        return []
    if not _is_truthy_env("AI_FOLLOWUP_COMMAND_EXEC_ENABLED", True):
        return []

    max_actions = _resolve_followup_auto_exec_max_actions()
    if max_actions <= 0:
        return []

    timeout_seconds = _resolve_followup_auto_exec_timeout_seconds()
    observations: List[Dict[str, Any]] = []
    seen_commands: set = set()
    executed_set = executed_commands if isinstance(executed_commands, set) else set()
    safe_prior_observations = [item for item in _as_list(prior_observations) if isinstance(item, dict)]

    for action in actions:
        if len(observations) >= max_actions:
            break
        action_dict = action if isinstance(action, dict) else {}
        action_id = _as_str(action_dict.get("id")) or f"auto-{len(observations) + 1}"
        command_spec = normalize_followup_command_spec(action_dict.get("command_spec"))
        planned_command = _normalize_followup_command_line(_as_str(action_dict.get("command")))
        if not command_spec:
            observation = {
                "status": "semantic_incomplete",
                "session_id": session_id,
                "message_id": message_id,
                "action_id": action_id,
                "command": planned_command,
                "command_type": "unknown",
                "risk_level": _as_str(action_dict.get("risk_level"), "high"),
                "message": "missing_or_invalid_command_spec: command_spec is required for readonly auto-exec",
                "auto_executed": False,
            }
            observations.append(observation)
            await _emit_followup_event(event_callback, "observation", observation, logger=logger)
            continue

        compiled = compile_followup_command_spec(command_spec, run_sql_preflight=True)
        if not bool(compiled.get("ok")):
            compile_reason = _as_str(compiled.get("reason"), "command_spec compile failed")
            compile_detail = _as_str(compiled.get("detail")).strip()
            semantic_message = compile_reason if not compile_detail else f"{compile_reason}: {compile_detail}"
            observation = {
                "status": "semantic_incomplete",
                "session_id": session_id,
                "message_id": message_id,
                "action_id": action_id,
                "command": planned_command,
                "command_type": "unknown",
                "risk_level": _as_str(action_dict.get("risk_level"), "high"),
                "message": semantic_message,
                "auto_executed": False,
            }
            observations.append(observation)
            await _emit_followup_event(event_callback, "observation", observation, logger=logger)
            continue

        raw_command = _normalize_followup_command_line(_as_str(compiled.get("command")))
        action_dict["command_spec"] = (
            compiled.get("command_spec")
            if isinstance(compiled.get("command_spec"), dict)
            else command_spec
        )
        action_dict["command"] = raw_command
        if not raw_command or raw_command in seen_commands:
            continue
        seen_commands.add(raw_command)
        action_command_type = _as_str(action_dict.get("command_type")).lower()
        action_executable = bool(action_dict.get("executable"))
        if not action_executable or action_command_type == "unknown":
            semantic_message = (
                _as_str(action_dict.get("reason"))
                or "命令语义未补全，已跳过自动执行。请补全命令参数后继续。"
            )
            observation = {
                "status": "semantic_incomplete",
                "session_id": session_id,
                "message_id": message_id,
                "action_id": action_id,
                "command": raw_command,
                "command_type": action_command_type or "unknown",
                "risk_level": _as_str(action_dict.get("risk_level"), "high"),
                "message": semantic_message,
                "auto_executed": False,
            }
            observations.append(observation)
            await _emit_followup_event(event_callback, "observation", observation, logger=logger)
            continue

        command_purpose = _as_str(
            action_dict.get("purpose")
            or action_dict.get("title")
            or action_dict.get("question")
            or "自动执行只读排查命令"
        ).strip() or "自动执行只读排查命令"
        effective_command_spec = (
            action_dict.get("command_spec")
            if isinstance(action_dict.get("command_spec"), dict)
            else command_spec
        )
        effective_args = effective_command_spec.get("args") if isinstance(effective_command_spec, dict) else {}
        target_kind = _as_str(
            (effective_args or {}).get("target_kind")
            or (effective_command_spec or {}).get("target_kind")
        )
        target_identity = _as_str(
            (effective_args or {}).get("target_identity")
            or (effective_command_spec or {}).get("target_identity")
        )

        try:
            precheck = await precheck_command(
                session_id=session_id,
                message_id=message_id,
                action_id=action_id,
                command=raw_command,
                purpose=command_purpose,
                timeout_seconds=min(timeout_seconds, 10),
                target_kind=target_kind,
                target_identity=target_identity,
            )
        except ExecServiceClientError as exc:
            execution_payload = _build_followup_auto_exec_failed_observation(
                session_id=session_id,
                message_id=message_id,
                action_id=action_id,
                command=raw_command,
                command_meta={},
                message=f"exec-service 预检失败: {exc}",
                auto_executed=False,
            )
            observations.append(execution_payload)
            await _emit_followup_event(event_callback, "observation", execution_payload, logger=logger)
            continue

        safe_precheck = precheck if isinstance(precheck, dict) else {}
        normalized_command = _normalize_followup_command_line(_as_str(safe_precheck.get("command"), raw_command))
        if not normalized_command:
            normalized_command = raw_command
        if normalized_command in executed_set:
            reused_observation = _resolve_latest_success_observation(
                command=normalized_command,
                observations=safe_prior_observations + observations,
            )
            reused_command_run_id = _as_str((reused_observation or {}).get("command_run_id")).strip()
            observation = _build_followup_auto_exec_skip_observation(
                session_id=session_id,
                message_id=message_id,
                action_id=action_id,
                command=normalized_command,
                reason="同一 run 已执行过该命令，跳过重复执行。",
                reason_code="duplicate_skipped",
                reused_command_run_id=reused_command_run_id,
                reused_evidence_ids=[reused_command_run_id] if reused_command_run_id else [],
            )
            observations.append(observation)
            await _emit_followup_event(event_callback, "observation", observation, logger=logger)
            continue
        if normalized_command != raw_command and normalized_command in seen_commands:
            continue
        seen_commands.add(normalized_command)

        precheck_status = _as_str(safe_precheck.get("status")).lower()
        precheck_command_type = _as_str(safe_precheck.get("command_type")).lower()
        if (
            _require_spec_for_repair_enabled()
            and not command_spec
            and precheck_command_type == "repair"
        ):
            blocked_payload = {
                "status": "permission_required",
                "action_id": action_id,
                "command": normalized_command,
                "session_id": session_id,
                "message_id": message_id,
                "auto_executed": False,
                "command_type": "repair",
                "risk_level": _as_str(safe_precheck.get("risk_level"), "high"),
                "message": "高风险写命令需提供 command_spec（结构化命令）后才可审批执行。",
            }
            observations.append(blocked_payload)
            await _emit_followup_event(event_callback, "observation", blocked_payload, logger=logger)
            continue
        command_meta = {
            "command_type": _as_str(safe_precheck.get("command_type"), "unknown"),
            "risk_level": _as_str(safe_precheck.get("risk_level"), "high"),
            "requires_write_permission": bool(safe_precheck.get("requires_write_permission")),
            "supported": precheck_status == "ok",
            "reason": _as_str(safe_precheck.get("message")),
            "command_spec": effective_command_spec if isinstance(effective_command_spec, dict) else {},
        }
        action_dict["command"] = normalized_command
        action_dict["command_type"] = _as_str(command_meta.get("command_type"), "unknown")
        action_dict["risk_level"] = _as_str(command_meta.get("risk_level"), "high")
        action_dict["requires_write_permission"] = bool(command_meta.get("requires_write_permission"))
        action_dict["requires_elevation"] = bool(safe_precheck.get("requires_elevation")) or bool(command_meta.get("requires_write_permission"))
        action_dict["executable"] = precheck_status != "permission_required" and bool(normalized_command)

        if precheck_status in {"permission_required", "confirmation_required", "elevation_required"}:
            gate_payload = {
                **safe_precheck,
                "action_id": action_id,
                "command": normalized_command,
                "command_spec": effective_command_spec if isinstance(effective_command_spec, dict) else {},
                "command_spec_present": bool(
                    isinstance(effective_command_spec, dict) and effective_command_spec
                ),
                "session_id": session_id,
                "message_id": message_id,
                "auto_executed": False,
            }
            observations.append(gate_payload)
            await _emit_followup_event(event_callback, "observation", gate_payload, logger=logger)
            continue
        if (
            _as_str(command_meta.get("command_type")).lower() == "repair"
            or bool(command_meta.get("requires_write_permission"))
        ):
            gate_payload = {
                **safe_precheck,
                "status": "permission_required",
                "action_id": action_id,
                "command": normalized_command,
                "command_spec": effective_command_spec if isinstance(effective_command_spec, dict) else {},
                "command_spec_present": bool(
                    isinstance(effective_command_spec, dict) and effective_command_spec
                ),
                "session_id": session_id,
                "message_id": message_id,
                "auto_executed": False,
                "command_type": "repair",
                "risk_level": _as_str(command_meta.get("risk_level"), "high"),
                "requires_write_permission": True,
                "requires_elevation": True,
                "message": _as_str(
                    safe_precheck.get("message"),
                    "检测到写操作命令，需提权审批通过后执行。",
                ),
            }
            observations.append(gate_payload)
            await _emit_followup_event(event_callback, "observation", gate_payload, logger=logger)
            continue
        if precheck_status != "ok":
            observation = _build_followup_auto_exec_skip_observation(
                session_id=session_id,
                message_id=message_id,
                action_id=action_id,
                command=normalized_command,
                reason=_as_str(safe_precheck.get("message"), f"命令预检未通过: {precheck_status or 'unknown'}"),
                reason_code="precheck_blocked",
            )
            observations.append(observation)
            await _emit_followup_event(event_callback, "observation", observation, logger=logger)
            continue
        if bool(safe_precheck.get("dispatch_requires_template")) and bool(safe_precheck.get("dispatch_degraded")):
            observation = _build_followup_auto_exec_skip_observation(
                session_id=session_id,
                message_id=message_id,
                action_id=action_id,
                command=normalized_command,
                reason="执行网关未就绪，命令未自动执行。",
                reason_code="backend_unready",
            )
            observations.append(observation)
            await _emit_followup_event(event_callback, "observation", observation, logger=logger)
            continue
        if not _is_auto_exec_safe_query_command(normalized_command, command_meta):
            observation = _build_followup_auto_exec_skip_observation(
                session_id=session_id,
                message_id=message_id,
                action_id=action_id,
                command=normalized_command,
                reason="自动执行仅支持受控只读命令。",
                reason_code="policy_blocked",
            )
            observations.append(observation)
            await _emit_followup_event(event_callback, "observation", observation, logger=logger)
            continue

        plan_reason = (
            _as_str(action_dict.get("reason")).strip()
            or _as_str(command_meta.get("reason")).strip()
            or "该命令用于补齐当前证据缺口。"
        )
        plan_expected = (
            _as_str(action_dict.get("expected_outcome")).strip()
            or _as_str(action_dict.get("question")).strip()
            or _as_str(action_dict.get("title")).strip()
            or "用于判断是否进入下一步排查。"
        )
        plan_iteration = int(_as_float(action_dict.get("iteration"), 0))
        await _emit_followup_event(
            event_callback,
            "thought",
            {
                "phase": "action",
                "title": "执行前计划",
                "status": "info",
                "iteration": plan_iteration if plan_iteration > 0 else None,
                "action_id": action_id,
                "detail": _build_command_plan_detail(
                    command=normalized_command,
                    purpose=command_purpose,
                    reason=plan_reason,
                    expected_outcome=plan_expected,
                ),
                "plan": {
                    "commands": [normalized_command],
                    "purpose": command_purpose,
                    "reason": plan_reason,
                    "expected_outcome": plan_expected,
                },
            },
            logger=logger,
        )

        try:
            exec_response = await create_command_run(
                session_id=session_id,
                message_id=message_id,
                action_id=action_id,
                command=normalized_command,
                command_spec=effective_command_spec if isinstance(effective_command_spec, dict) else {},
                purpose=command_purpose,
                timeout_seconds=timeout_seconds,
                target_kind=target_kind,
                target_identity=target_identity,
            )
            exec_run = exec_response.get("run") if isinstance(exec_response.get("run"), dict) else None
            if isinstance(exec_run, dict):
                execution_payload = await _stream_followup_exec_runtime(
                    session_id=session_id,
                    message_id=message_id,
                    action_id=action_id,
                    command=normalized_command,
                    command_meta=command_meta,
                    exec_run_id=_as_str(exec_run.get("run_id")),
                    event_callback=event_callback,
                    logger=logger,
                )
                if not isinstance(execution_payload, dict):
                    execution_payload = _build_followup_auto_exec_failed_observation(
                        session_id=session_id,
                        message_id=message_id,
                        action_id=action_id,
                        command=normalized_command,
                        command_meta=command_meta,
                        message="命令流式执行结束，但未收到最终状态。",
                        auto_executed=True,
                    )
                    await _emit_followup_event(event_callback, "observation", execution_payload, logger=logger)
                if (
                    _as_str(execution_payload.get("status")).lower() == "executed"
                    and int(_as_float(execution_payload.get("exit_code"), 0)) == 0
                ):
                    executed_set.add(normalized_command)
                observations.append(execution_payload)
                continue

            if isinstance(exec_response, dict) and _as_str(exec_response.get("status")):
                execution_payload = {
                    **exec_response,
                    "action_id": action_id,
                    "auto_executed": False,
                    "command": _as_str(exec_response.get("command"), normalized_command),
                    "command_type": _as_str(exec_response.get("command_type"), _as_str(command_meta.get("command_type"), "query")),
                    "risk_level": _as_str(exec_response.get("risk_level"), _as_str(command_meta.get("risk_level"), "low")),
                    "message_id": message_id,
                    "session_id": session_id,
                }
                if (
                    _as_str(execution_payload.get("status")).lower() == "executed"
                    and int(_as_float(execution_payload.get("exit_code"), 0)) == 0
                ):
                    executed_set.add(normalized_command)
                observations.append(execution_payload)
                await _emit_followup_event(event_callback, "observation", execution_payload, logger=logger)
                continue
        except ExecServiceClientError as exc:
            execution_payload = _build_followup_auto_exec_failed_observation(
                session_id=session_id,
                message_id=message_id,
                action_id=action_id,
                command=normalized_command,
                command_meta=command_meta,
                message=f"exec-service 执行失败: {exc}",
                auto_executed=False,
            )
            observations.append(execution_payload)
            await _emit_followup_event(event_callback, "observation", execution_payload, logger=logger)
            continue
        except Exception as exc:
            if _is_runtime_pause_signal(exc):
                raise
            execution_payload = _build_followup_auto_exec_failed_observation(
                session_id=session_id,
                message_id=message_id,
                action_id=action_id,
                command=normalized_command,
                command_meta=command_meta,
                message=f"自动执行异常: {exc}",
                auto_executed=False,
            )
            observations.append(execution_payload)
            await _emit_followup_event(event_callback, "observation", execution_payload, logger=logger)
            continue

    return observations


def _latest_observation_maps(action_observations: List[Dict[str, Any]]) -> tuple[Dict[str, Dict[str, Any]], Dict[str, Dict[str, Any]]]:
    by_action_id: Dict[str, Dict[str, Any]] = {}
    by_command: Dict[str, Dict[str, Any]] = {}
    for item in _as_list(action_observations):
        if not isinstance(item, dict):
            continue
        action_id = _as_str(item.get("action_id"))
        command = _normalize_followup_command_line(_as_str(item.get("command")))
        if action_id:
            by_action_id[action_id] = item
        if command:
            by_command[command] = item
    return by_action_id, by_command


def _count_command_failures(action_observations: List[Dict[str, Any]]) -> Dict[str, int]:
    counts: Dict[str, int] = {}
    for item in _as_list(action_observations):
        if not isinstance(item, dict):
            continue
        command = _normalize_followup_command_line(_as_str(item.get("command")))
        if not command:
            continue
        status = _as_str(item.get("status")).lower()
        if status == "executed":
            exit_code = int(_as_float(item.get("exit_code"), 0))
            if exit_code == 0 and not bool(item.get("output_truncated")):
                continue
            counts[command] = counts.get(command, 0) + 1
            continue
        if status == "failed":
            counts[command] = counts.get(command, 0) + 1
            continue
        if status == "skipped":
            reason_code = _as_str(item.get("reason_code")).strip().lower()
            if reason_code in {"backend_unready"}:
                counts[command] = counts.get(command, 0) + 1
    return counts


def _select_followup_react_iteration_actions(
    *,
    actions: List[Dict[str, Any]],
    action_observations: List[Dict[str, Any]],
    retry_per_command: int,
    max_items: int = 8,
) -> List[Dict[str, Any]]:
    """选择下一轮可自动执行的查询动作。"""
    safe_max_items = max(1, min(int(max_items or 8), 20))
    by_action_id, by_command = _latest_observation_maps(action_observations)
    command_failures = _count_command_failures(action_observations)
    selected: List[Dict[str, Any]] = []
    seen_commands: set[str] = set()

    def _latest_success_for_command(command_text: str) -> Optional[Dict[str, Any]]:
        return _resolve_latest_success_observation(
            command=command_text,
            observations=action_observations,
        )

    for action in _as_list(actions):
        if len(selected) >= safe_max_items:
            break
        action_dict = action if isinstance(action, dict) else {}
        if not bool(action_dict.get("executable")):
            continue
        if _as_str(action_dict.get("command_type")).lower() != "query":
            continue
        command = _normalize_followup_command_line(_as_str(action_dict.get("command")))
        if not command or command in seen_commands:
            continue
        action_id = _as_str(action_dict.get("id"))
        observation = by_action_id.get(action_id) or by_command.get(command)
        should_retry = False
        if observation is None:
            should_retry = True
        else:
            status = _as_str(observation.get("status")).lower()
            if status == "executed":
                exit_code = int(_as_float(observation.get("exit_code"), 0))
                should_retry = exit_code != 0 or bool(observation.get("output_truncated"))
            elif status == "failed":
                should_retry = True
            elif status == "skipped":
                reason_code = _as_str(observation.get("reason_code")).strip().lower()
                if reason_code in {"backend_unready"}:
                    should_retry = True
                elif reason_code == "duplicate_skipped":
                    source_observation = _latest_success_for_command(command)
                    if source_observation is None:
                        # Duplicate-skipped but no reusable source evidence; retry to collect source.
                        should_retry = True
                    else:
                        should_retry = bool(source_observation.get("output_truncated"))
        if not should_retry:
            continue
        if command_failures.get(command, 0) > max(0, int(retry_per_command)):
            continue
        seen_commands.add(command)
        selected.append(action_dict)

    return selected


async def _run_followup_auto_exec_react_loop(
    *,
    session_id: str,
    message_id: str,
    actions: List[Dict[str, Any]],
    analysis_context: Optional[Dict[str, Any]] = None,
    run_blocking: Any,
    build_react_loop_fn: Any,
    allow_auto_exec_readonly: bool = True,
    executed_commands: Optional[set[str]] = None,
    initial_action_observations: Optional[List[Dict[str, Any]]] = None,
    initial_evidence_gaps: Optional[List[str]] = None,
    initial_summary: str = "",
    emit_iteration_thoughts: bool = True,
    event_callback: Optional[Any] = None,
    logger: Optional[Any] = None,
) -> Dict[str, Any]:
    """
    在单次请求内执行多轮 ReAct 自动执行闭环。

    返回:
    - action_observations: 所有轮次的观察
    - react_loop: 最终闭环状态
    - react_iterations: 每轮摘要
    """
    working_actions: List[Dict[str, Any]] = [item for item in _as_list(actions) if isinstance(item, dict)]
    max_iterations = _resolve_followup_react_max_iterations()
    retry_per_command = _resolve_followup_react_retry_per_command()
    all_observations: List[Dict[str, Any]] = [
        item for item in _as_list(initial_action_observations) if isinstance(item, dict)
    ]
    react_iterations: List[Dict[str, Any]] = []
    final_react_loop: Dict[str, Any] = {}
    active_evidence_gaps = [
        _as_str(item).strip()
        for item in _as_list(initial_evidence_gaps)
        if _as_str(item).strip()
    ]
    active_summary = _as_str(initial_summary).strip()

    for iteration in range(1, max_iterations + 1):
        # Always gate by executable query candidates, including the first round.
        # This prevents semantic_incomplete/manual actions (for example glued raw command text)
        # from entering auto-exec and polluting pending_command_request/business-question prompts.
        candidate_actions = _select_followup_react_iteration_actions(
            actions=working_actions,
            action_observations=all_observations,
            retry_per_command=retry_per_command,
            max_items=max(1, len(working_actions)),
        )
        iteration_actions = _select_iteration_actions_by_evidence_gaps(
            candidate_actions,
            active_evidence_gaps,
            max_items=max(1, len(candidate_actions)),
        )
        if active_evidence_gaps and not iteration_actions:
            if candidate_actions:
                # 缺口词未命中动作时，退回候选动作，避免循环提前终止。
                iteration_actions = candidate_actions
                if emit_iteration_thoughts:
                    await _emit_followup_event(
                        event_callback,
                        "thought",
                        {
                            "phase": "summary",
                            "title": f"第 {iteration} 轮总结：缺口未命中，回退到候选命令",
                            "detail": (
                                f"当前缺口：{'；'.join(active_evidence_gaps[:4])}；"
                                "未匹配到高相关命令，回退执行候选命令并继续复盘。"
                            ),
                            "status": "warning",
                            "iteration": iteration,
                        },
                        logger=logger,
                    )
            elif emit_iteration_thoughts:
                fallback_templates = _build_non_executable_command_templates(
                    working_actions,
                    analysis_context=analysis_context,
                    max_items=3,
                )
                fallback_hint = (
                    f"建议先补全并执行：{'；'.join(fallback_templates[:3])}；"
                    "再把每条命令按 command_spec(tool+args+target_identity) 回填给大模型继续重规划。"
                    if fallback_templates
                    else "请先补全 command_spec(tool+args+target_identity) 后继续重规划。"
                )
                await _emit_followup_event(
                    event_callback,
                    "thought",
                    {
                        "phase": "summary",
                        "title": f"第 {iteration} 轮总结：缺口未命中，且暂无可执行候选命令",
                        "detail": (
                            f"当前缺口：{'；'.join(active_evidence_gaps[:4])}；"
                            "当前计划没有可自动执行的结构化查询命令，已进入重规划并等待补全 command_spec；"
                            f"{fallback_hint}"
                        ),
                        "status": "warning",
                        "iteration": iteration,
                    },
                    logger=logger,
                )
        if not iteration_actions:
            template_actions = _build_structured_template_actions(
                actions=working_actions,
                analysis_context=analysis_context,
                max_items=max(1, _resolve_followup_auto_exec_max_actions()),
            )
            if template_actions:
                working_actions.extend(template_actions)
                iteration_actions = template_actions
                if emit_iteration_thoughts:
                    execution_mode_detail = _describe_template_action_execution_mode(
                        allow_auto_exec_readonly=allow_auto_exec_readonly
                    )
                    await _emit_followup_event(
                        event_callback,
                        "thought",
                        {
                            "phase": "action",
                            "title": f"第 {iteration} 轮：使用结构化模板命令自动补证据",
                            "detail": (
                                "当前计划缺少可直接执行候选，已从重规划模板生成结构化只读命令；"
                                f"{execution_mode_detail}"
                                f"本轮模板命令 {len(template_actions)} 条。"
                            ),
                            "status": "info",
                            "iteration": iteration,
                        },
                        logger=logger,
                    )
            else:
                break

        pre_summary_parts: List[str] = []
        if active_summary:
            pre_summary_parts.append(f"当前结论摘要：{active_summary[:180]}")
        if active_evidence_gaps:
            pre_summary_parts.append(f"待补证据：{'；'.join(active_evidence_gaps[:4])}")
        command_preview = _summarize_iteration_actions(iteration_actions, max_items=3)
        if command_preview:
            pre_summary_parts.append(f"本轮命令：{command_preview}")
        if emit_iteration_thoughts:
            await _emit_followup_event(
                event_callback,
                "thought",
                {
                    "phase": "summary",
                    "title": f"第 {iteration} 轮总结：先补齐缺失证据再执行命令",
                    "detail": "；".join(part for part in pre_summary_parts if part)[:480],
                    "status": "info",
                    "iteration": iteration,
                },
                logger=logger,
            )
        await _emit_followup_event(
            event_callback,
            "plan",
            {
                "stage": "react_execute",
                "iteration": iteration,
                "candidate_actions": len(iteration_actions),
                "evidence_gaps": active_evidence_gaps[:6],
            },
            logger=logger,
        )
        observations = await _run_followup_readonly_auto_exec(
            session_id=session_id,
            message_id=message_id,
            actions=iteration_actions,
            allow_auto_exec_readonly=allow_auto_exec_readonly,
            executed_commands=executed_commands,
            prior_observations=all_observations,
            run_blocking=run_blocking,
            event_callback=event_callback,
            logger=logger,
        )
        for obs in observations:
            if isinstance(obs, dict):
                obs["iteration"] = iteration
        all_observations.extend(observations)

        final_react_loop = build_react_loop_fn(
            actions=working_actions,
            action_observations=all_observations,
            analysis_context=analysis_context,
        )
        execute = final_react_loop.get("execute") if isinstance(final_react_loop.get("execute"), dict) else {}
        observe = final_react_loop.get("observe") if isinstance(final_react_loop.get("observe"), dict) else {}
        replan = final_react_loop.get("replan") if isinstance(final_react_loop.get("replan"), dict) else {}
        replan_needed = bool(replan.get("needed"))
        confidence = float(_as_float(observe.get("confidence"), 0.0))
        unresolved_actions = int(_as_float(observe.get("unresolved_actions"), 0))
        next_actions = [
            _as_str(item).strip()
            for item in _as_list(replan.get("next_actions"))
            if _as_str(item).strip()
        ]
        post_summary_parts = [
            (
                f"observed={int(_as_float(execute.get('observed_actions'), 0))}, "
                f"success={int(_as_float(execute.get('executed_success'), 0))}, "
                f"failed={int(_as_float(execute.get('executed_failed'), 0))}"
            ),
            f"confidence={confidence:.2f}",
            f"unresolved={unresolved_actions}",
        ]
        if next_actions:
            post_summary_parts.append(f"下一轮证据缺口：{'；'.join(next_actions[:3])}")
        if emit_iteration_thoughts:
            await _emit_followup_event(
                event_callback,
                "thought",
                {
                    "phase": "summary",
                    "title": (
                        f"第 {iteration} 轮执行后总结："
                        f"{'证据仍不足，继续补充' if replan_needed else '证据收敛，可输出结论'}"
                    ),
                    "detail": "；".join(post_summary_parts)[:480],
                    "status": "warning" if replan_needed else "success",
                    "iteration": iteration,
                },
                logger=logger,
            )
        active_summary = _as_str(final_react_loop.get("summary")).strip() or active_summary
        if replan_needed:
            if next_actions:
                active_evidence_gaps = next_actions[:8]
            else:
                fallback_gaps = [
                    _as_str(item.get("summary")).strip()
                    for item in _as_list(replan.get("items"))
                    if isinstance(item, dict) and _as_str(item.get("summary")).strip()
                ]
                active_evidence_gaps = fallback_gaps[:8]
        else:
            active_evidence_gaps = []
        react_iterations.append(
            {
                "iteration": iteration,
                "candidate_actions": len(iteration_actions),
                "observed_actions": len(observations),
                "react_loop": final_react_loop,
            }
        )
        await _emit_followup_event(
            event_callback,
            "replan",
            {
                "iteration": iteration,
                "react_loop": final_react_loop,
            },
            logger=logger,
        )
        if len(observations) <= 0:
            break
        if not bool((final_react_loop.get("replan") or {}).get("needed")):
            break

    if not final_react_loop:
        final_react_loop = build_react_loop_fn(
            actions=working_actions,
            action_observations=all_observations,
            analysis_context=analysis_context,
        )
    return {
        "actions": working_actions,
        "action_observations": all_observations,
        "react_loop": final_react_loop,
        "react_iterations": react_iterations,
    }
