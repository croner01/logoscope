"""
Follow-up planning helpers (subgoals/actions).

Extracted from `api/ai.py` to keep route file focused on orchestration.
"""

import re
import shlex
from datetime import datetime, timedelta, timezone
from typing import Any, Callable, Dict, List, Optional

from ai.followup_command import (
    _classify_followup_command,
    _extract_commands_from_message_content,
    _normalize_followup_command_match_key,
    _normalize_followup_command_line,
    _resolve_followup_command_meta,
)
from ai.followup_command_spec import (
    build_command_spec_self_repair_payload,
    build_followup_command_spec_match_key,
    compile_followup_command_spec,
    normalize_followup_command_spec,
)


def _as_str(value: Any, default: str = "") -> str:
    if value is None:
        return default
    if isinstance(value, str):
        return value
    return str(value)


def _as_list(value: Any) -> List[Any]:
    if isinstance(value, list):
        return value
    return []


def _as_float(value: Any, default: float = 0.0) -> float:
    try:
        if value is None:
            return float(default)
        return float(value)
    except Exception:
        return float(default)


def _as_bool(value: Any, default: bool = False) -> bool:
    if isinstance(value, bool):
        return value
    if value is None:
        return bool(default)
    normalized = _as_str(value).strip().lower()
    if normalized in {"1", "true", "yes", "on"}:
        return True
    if normalized in {"0", "false", "no", "off"}:
        return False
    return bool(default)


def _unknown_command_meta() -> Dict[str, Any]:
    return {
        "command_type": "unknown",
        "risk_level": "high",
        "requires_write_permission": False,
        "supported": False,
        "reason": "命令解析失败",
    }


def _resolve_action_command_meta(command: str) -> Dict[str, Any]:
    normalized_command = _normalize_followup_command_line(command)
    if not _as_str(normalized_command):
        return {}
    try:
        command_meta, _ = _resolve_followup_command_meta(normalized_command)
        if isinstance(command_meta, dict):
            return command_meta
    except Exception:
        pass
    try:
        return _classify_followup_command(shlex.split(normalized_command))
    except Exception:
        return _unknown_command_meta()


def _is_low_trust_answer_command(action_dict: Dict[str, Any]) -> bool:
    source = _as_str(action_dict.get("source")).strip().lower()
    reason = _as_str(action_dict.get("reason")).strip().lower()
    if source == "answer_command":
        return True
    return "answer_command_requires_structured_action" in reason


def _infer_query_template_command_spec(command: str) -> tuple[str, Dict[str, Any]]:
    """
    将模板命令推断为可回填的 command_spec 草稿。
    仅返回只读 query 命令，避免把高风险或语义不完整命令带入下一轮。
    """
    normalized_command = _normalize_followup_command_line(command)
    if not _as_str(normalized_command):
        return "", {}
    command_meta = _resolve_action_command_meta(normalized_command)
    if (
        not bool(command_meta.get("supported"))
        or _as_str(command_meta.get("command_type")).strip().lower() != "query"
    ):
        return "", {}
    compiled = compile_followup_command_spec(
        {
            "tool": "generic_exec",
            "args": {
                "command": normalized_command,
                "timeout_s": 30,
            },
        }
    )
    if not bool(compiled.get("ok")):
        return "", {}
    compiled_command = _as_str(compiled.get("command")).strip()
    compiled_spec = normalize_followup_command_spec(compiled.get("command_spec"))
    if not compiled_command or not compiled_spec:
        return "", {}
    return compiled_command, compiled_spec


def _extract_command_from_hint_line(text: str) -> str:
    safe_text = _as_str(text).strip()
    if not safe_text:
        return ""
    prefix = "补全结构化命令后执行："
    if not safe_text.startswith(prefix):
        return ""
    body = safe_text[len(prefix) :]
    marker_index = body.find("（")
    if marker_index >= 0:
        body = body[:marker_index]
    normalized = _normalize_followup_command_line(body)
    return re.sub(r"(?i)\bclickhouse\s+-client\b", "clickhouse-client", normalized)


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
    explicit_start = _parse_optional_iso_datetime(context_payload.get("request_flow_window_start"))
    explicit_end = _parse_optional_iso_datetime(context_payload.get("request_flow_window_end"))
    if explicit_start and explicit_end and explicit_start <= explicit_end:
        return {
            "start_iso": _to_utc_iso_text(explicit_start),
            "end_iso": _to_utc_iso_text(explicit_end),
        }

    anchor_candidates = [
        context_payload.get("source_log_timestamp"),
        context_payload.get("related_log_anchor_timestamp"),
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


def _build_non_executable_query_command_hints(
    actions: List[Dict[str, Any]],
    *,
    analysis_context: Optional[Dict[str, Any]] = None,
    max_items: int = 4,
) -> List[str]:
    """在无可执行命令时，给出可补全 command_spec 的查询模板。"""
    safe_limit = max(1, min(int(max_items or 4), 8))
    hints: List[str] = []
    seen: set[str] = set()
    context_payload = analysis_context if isinstance(analysis_context, dict) else {}
    namespace = _as_str(context_payload.get("namespace"), "islap") or "islap"
    service_name = _as_str(context_payload.get("service_name")).strip()
    trace_id = _as_str(context_payload.get("trace_id")).strip()
    evidence_window = _resolve_followup_evidence_window(context_payload)
    window_start_iso = _as_str(evidence_window.get("start_iso")).strip()
    window_end_iso = _as_str(evidence_window.get("end_iso")).strip()

    def _append_context_aware_defaults(reason: str) -> None:
        if service_name:
            _append_hint(
                _build_k8s_logs_evidence_command(
                    namespace=namespace,
                    service_name=service_name,
                    window_start_iso=window_start_iso,
                ),
                reason or "回收当前服务在故障时间窗口的原始日志证据",
            )
        if trace_id:
            _append_hint(
                _build_k8s_logs_evidence_command(
                    namespace=namespace,
                    service_name=service_name or "query-service",
                    window_start_iso=window_start_iso,
                ),
                f"围绕 trace_id={trace_id[:16]} 回看当前服务日志上下文",
            )

    def _append_hint(command: str, reason: str) -> None:
        safe_command = _as_str(command).strip()
        safe_reason = _as_str(reason).strip() or "补齐当前证据缺口"
        if not safe_command:
            return
        line = f"补全结构化命令后执行：{safe_command}（{safe_reason}）"
        if line in seen:
            return
        seen.add(line)
        hints.append(line)

    for action in _as_list(actions):
        if len(hints) >= safe_limit:
            break
        action_dict = action if isinstance(action, dict) else {}
        if bool(action_dict.get("executable")):
            continue
        command = _as_str(action_dict.get("command")).strip()
        title = _as_str(action_dict.get("title"))
        purpose = _as_str(action_dict.get("purpose"))
        reason = _as_str(action_dict.get("reason"))
        text_blob = " ".join([title, purpose, reason]).lower()
        if command:
            if _is_low_trust_answer_command(action_dict):
                command = ""
            else:
                template_command, _ = _infer_query_template_command_spec(command)
                if template_command:
                    _append_hint(template_command, "来自当前计划但缺少可执行结构化参数")
                    continue
        if any(token in text_blob for token in ["temporal", "日志", "trace", "error", "cancel"]):
            _append_context_aware_defaults("补齐当前服务错误上下文与调用链线索")
            continue
        if any(token in text_blob for token in ["clickhouse", "慢查询", "锁", "sql", "query_log", "code:184"]):
            _append_hint(
                _build_clickhouse_query_log_evidence_command(
                    namespace=namespace,
                    window_start_iso=window_start_iso,
                    window_end_iso=window_end_iso,
                ),
                "补齐 ClickHouse 查询失败证据，确认异常码与原始 SQL",
            )
            continue
        if any(token in text_blob for token in ["连接池", "pool", "timeout", "配置"]):
            _append_hint(
                f"kubectl -n {namespace} describe pod -l app={service_name or 'query-service'}",
                "核对当前服务的超时和连接配置",
            )
            continue
        if any(token in text_blob for token in ["cpu", "内存", "网络", "资源"]):
            _append_hint(
                f"kubectl -n {namespace} top pod -l app={service_name or 'query-service'}",
                "确认当前服务在故障时间窗口是否存在资源压力",
            )

    if not hints:
        _append_context_aware_defaults("先回收当前服务的直接证据，再细化结构化命令")
    if not hints:
        _append_hint(f"kubectl -n {namespace} get pods --show-labels", "先确认可排查目标，再补全具体查询命令")

    return hints[:safe_limit]


def _build_followup_subgoals(
    question: str,
    analysis_context: Dict[str, Any],
    references: List[Dict[str, str]],
) -> List[Dict[str, Any]]:
    """基于追问内容和上下文拆解子目标。"""
    question_text = _as_str(question).lower()
    result = analysis_context.get("result") if isinstance(analysis_context, dict) else {}
    result_dict = result if isinstance(result, dict) else {}
    overview = result_dict.get("overview") if isinstance(result_dict.get("overview"), dict) else {}
    trace_id = _as_str(analysis_context.get("trace_id"))
    request_id = _as_str(analysis_context.get("request_id"))

    data_flow = (
        result_dict.get("dataFlow")
        if result_dict.get("dataFlow") is not None
        else result_dict.get("data_flow")
    )
    data_flow_dict = data_flow if isinstance(data_flow, dict) else {}
    data_flow_path = _as_list(data_flow_dict.get("path"))
    root_causes = _as_list(
        result_dict.get("rootCauses")
        if result_dict.get("rootCauses") is not None
        else result_dict.get("root_causes")
    )
    solutions = _as_list(
        result_dict.get("solutions")
        if result_dict.get("solutions") is not None
        else result_dict.get("recommendations")
    )
    related_logs = _as_list(
        analysis_context.get("followup_related_logs")
        or analysis_context.get("related_logs")
    )
    related_log_count = len(related_logs)
    if related_log_count <= 0:
        related_log_count = int(
            _as_float(
                analysis_context.get("followup_related_log_count")
                or (
                    result_dict.get("agent", {}).get("related_log_count")
                    if isinstance(result_dict.get("agent"), dict)
                    else 0
                ),
                0,
            )
        )

    evidence_count = len(references)
    overview_problem_text = _as_str(overview.get("problem")).lower()
    overview_description_text = _as_str(overview.get("description")).lower()
    sql_perf_context_text = " ".join(
        [
            question_text,
            overview_problem_text,
            overview_description_text,
        ]
    )
    is_sql_performance_context = any(
        token in sql_perf_context_text
        for token in [
            "ch_query_slow",
            "slow query",
            "clickhouse",
            "sql",
            "prewhere",
            "慢查询",
            "查询慢",
            "耗时",
            "延迟",
            "latency",
            "performance",
        ]
    )
    need_chain = any(token in question_text for token in ["链路", "调用链", "trace", "请求流程", "路径", "request flow"])
    need_root = any(token in question_text for token in ["根因", "原因", "异常", "报错", "why", "error"])
    need_fix = any(token in question_text for token in ["修复", "解决", "优化", "建议", "步骤", "fix", "mitigation"])
    need_verify = any(token in question_text for token in ["验证", "回归", "复测", "指标", "监控", "verify", "regression"])
    if not (need_chain or need_root or need_fix or need_verify):
        need_root = True
        need_fix = True

    subgoals: List[Dict[str, Any]] = [
        {
            "id": "sg_scope",
            "title": "澄清追问范围与输出目标",
            "status": "completed",
            "reason": "已识别追问意图与上下文范围",
            "evidence": [_as_str(overview.get("problem"), "unknown"), _as_str(overview.get("severity"), "unknown")],
            "next_action": "",
        }
    ]

    if need_chain:
        if trace_id or len(data_flow_path) >= 2 or related_log_count >= 3:
            status = "completed"
            reason = "调用链证据充足，可还原请求路径"
        elif request_id or related_log_count > 0:
            status = "in_progress"
            reason = "已有部分链路线索，但缺少完整调用路径"
        else:
            status = "needs_data"
            reason = "缺少 trace/request 维度证据，无法稳定还原链路"
        subgoals.append(
            {
                "id": "sg_path",
                "title": "还原请求/调用链路",
                "status": status,
                "reason": reason,
                "evidence": [
                    f"trace_id={trace_id or 'N/A'}",
                    f"request_id={request_id or 'N/A'}",
                    f"related_logs={related_log_count}",
                ],
                "next_action": "补充 trace_id/request_id，或在页面点击“横向拉取日志”后再追问",
            }
        )

    if need_root:
        if root_causes:
            status = "completed"
            reason = "已有根因候选可直接展开"
        elif evidence_count >= 2 or related_log_count > 0:
            status = "in_progress"
            reason = "已有日志片段与分析摘要，可推导根因但置信度一般"
        else:
            status = "needs_data"
            reason = (
                "缺少慢查询执行证据，根因判断不稳定"
                if is_sql_performance_context
                else "缺少关键错误证据，根因判断不稳定"
            )
        root_next_action = (
            "补充慢查询样本、表结构与 EXPLAIN 结果后再确认根因优先级"
            if is_sql_performance_context
            else "补充 ERROR/Traceback 日志后再确认根因优先级"
        )
        subgoals.append(
            {
                "id": "sg_root",
                "title": "定位并解释根因",
                "status": status,
                "reason": reason,
                "evidence": [
                    f"root_causes={len(root_causes)}",
                    f"references={evidence_count}",
                    f"related_logs={related_log_count}",
                ],
                "next_action": root_next_action,
            }
        )

    if need_fix:
        if solutions:
            status = "completed"
            reason = "已有可执行方案，可直接给出优先级和步骤"
        elif root_causes or evidence_count >= 2:
            status = "in_progress"
            reason = "可先给止血方案，但中长期优化仍需更多证据"
        else:
            status = "pending"
            reason = "缺少足够证据，不建议直接给强结论修复方案"
        subgoals.append(
            {
                "id": "sg_fix",
                "title": "生成修复路径与执行步骤",
                "status": status,
                "reason": reason,
                "evidence": [f"solutions={len(solutions)}", f"references={evidence_count}"],
                "next_action": "先锁定首个失败节点，再输出短期止血 + 中长期优化",
            }
        )

    if need_verify:
        if (trace_id or request_id) and (root_causes or solutions):
            status = "in_progress"
            reason = "可给出验证路径，但需要具体回归指标"
        elif solutions:
            status = "pending"
            reason = "有修复建议但缺少可绑定的验证样本"
        else:
            status = "needs_data"
            reason = "缺少修复候选与样本，无法定义回归闭环"
        subgoals.append(
            {
                "id": "sg_verify",
                "title": "定义验证与回归闭环",
                "status": status,
                "reason": reason,
                "evidence": [f"trace_id={trace_id or 'N/A'}", f"request_id={request_id or 'N/A'}"],
                "next_action": "输出可观测指标与回归脚本清单（延迟/错误率/关键日志命中）",
            }
        )

    return subgoals


def _build_followup_actions(
    *,
    question: str,
    answer: str,
    reflection: Dict[str, Any],
    langchain_actions: Optional[List[Dict[str, Any]]] = None,
    max_items: int = 8,
    mask_text: Optional[Callable[[str], str]] = None,
) -> List[Dict[str, Any]]:
    """构建追问可执行计划动作（ReAct phase-1: Plan）。"""
    safe_max_items = max(1, min(int(max_items or 8), 20))
    actions: List[Dict[str, Any]] = []
    seen_keys: set = set()
    mask_fn = mask_text if callable(mask_text) else (lambda text: text)

    def _append_action(action_payload: Dict[str, Any]) -> None:
        if len(actions) >= safe_max_items:
            return
        command_key = _as_str(action_payload.get("command")).replace("\n", " ").strip()
        command_match_key = _normalize_followup_command_match_key(command_key)
        command_spec_key = build_followup_command_spec_match_key(action_payload.get("command_spec"))
        title_key = _as_str(action_payload.get("title")).strip().lower()
        if command_match_key:
            dedupe_key = f"cmd::{command_match_key}"
        elif command_spec_key:
            dedupe_key = f"spec::{command_spec_key}"
        else:
            dedupe_key = f"title::{title_key}"
        if dedupe_key in seen_keys:
            return
        seen_keys.add(dedupe_key)
        actions.append(action_payload)

    def _normalize_action_text(raw: Any) -> str:
        text = _as_str(raw).strip()
        if not text:
            return ""
        return _as_str(mask_fn(text))

    def _normalize_action_command(raw: Any) -> str:
        normalized = _normalize_followup_command_line(raw)
        # Keep canonical ClickHouse client head to avoid turning it into
        # `clickhouse -client` after generic normalization.
        return re.sub(r"(?i)\bclickhouse\s+-client\b", "clickhouse-client", normalized)

    def _new_action_id(index: int, source: str) -> str:
        return f"{source}-{index}"

    def _try_repair_structured_spec(
        *,
        command: str,
        command_spec: Dict[str, Any],
        compile_reason: str = "",
        compile_detail: str = "",
    ) -> tuple[Dict[str, Any], str, str]:
        safe_command = _normalize_action_command(command)
        safe_spec = normalize_followup_command_spec(command_spec)
        safe_reason = _as_str(compile_reason).strip()
        safe_detail = _as_str(compile_detail).strip()
        if not safe_command and not safe_spec:
            return safe_spec, safe_reason, safe_command

        repair_reason = safe_reason
        if not repair_reason or repair_reason == "missing_structured_spec":
            repair_reason = "missing_or_invalid_command_spec"

        repair_payload = build_command_spec_self_repair_payload(
            reason=repair_reason,
            detail=safe_detail,
            command_spec=safe_spec,
            raw_command=safe_command,
        )
        suggested_spec = normalize_followup_command_spec(
            repair_payload.get("suggested_command_spec")
            if isinstance(repair_payload.get("suggested_command_spec"), dict)
            else {}
        )
        if not suggested_spec:
            return safe_spec, safe_reason, safe_command

        compiled_suggested = compile_followup_command_spec(suggested_spec)
        if not bool(compiled_suggested.get("ok")):
            fallback_reason = safe_reason or _as_str(compiled_suggested.get("reason"))
            return suggested_spec, fallback_reason, safe_command

        repaired_spec = (
            compiled_suggested.get("command_spec")
            if isinstance(compiled_suggested.get("command_spec"), dict)
            else suggested_spec
        )
        repaired_command = _normalize_action_command(compiled_suggested.get("command"))
        if not repaired_command:
            repaired_command = safe_command
        return repaired_spec, "", repaired_command

    index_counter = 0

    for item in _as_list(langchain_actions):
        if len(actions) >= safe_max_items:
            break
        item_dict = item if isinstance(item, dict) else {}
        action_text = _normalize_action_text(item_dict.get("action"))
        action_title = _normalize_action_text(item_dict.get("title"))
        command_spec = normalize_followup_command_spec(item_dict.get("command_spec"))
        command_spec_compile_reason = ""
        command_spec_compile_detail = ""
        if command_spec:
            compiled = compile_followup_command_spec(command_spec)
            if bool(compiled.get("ok")):
                command = _normalize_action_command(compiled.get("command"))
                command_spec = (
                    compiled.get("command_spec")
                    if isinstance(compiled.get("command_spec"), dict)
                    else command_spec
                )
            else:
                command = _normalize_action_command(item_dict.get("command"))
                command_spec_compile_reason = _as_str(compiled.get("reason"))
                command_spec_compile_detail = _as_str(compiled.get("detail"))
        else:
            command = _normalize_action_command(item_dict.get("command"))
            if command:
                command_spec_compile_reason = "missing_structured_spec"
        if not command:
            fallback_source = action_text or action_title
            commands = _extract_commands_from_message_content(fallback_source, limit=1)
            command = _normalize_action_command(_as_str(commands[0]) if commands else "")
            if command and not command_spec_compile_reason:
                command_spec_compile_reason = "missing_structured_spec"

        initial_spec_compile_reason = _as_str(command_spec_compile_reason).strip()
        had_invalid_or_missing_spec = bool(command_spec_compile_reason)
        if had_invalid_or_missing_spec and (command or command_spec):
            repaired_spec, repaired_reason, repaired_command = _try_repair_structured_spec(
                command=command,
                command_spec=command_spec,
                compile_reason=command_spec_compile_reason,
                compile_detail=command_spec_compile_detail,
            )
            if repaired_command:
                command = repaired_command
            if repaired_spec:
                command_spec = repaired_spec
            command_spec_compile_reason = _as_str(repaired_reason).strip()
        if command_spec_compile_reason and command:
            command_meta_for_invalid = _resolve_action_command_meta(command)
            has_shell_chain = bool(re.search(r"\|\||&&|\||;", command))
            should_drop_command = (
                command_spec_compile_reason != "missing_structured_spec"
                or has_shell_chain
                or not bool(command_meta_for_invalid.get("supported"))
            )
            if should_drop_command:
                # command_spec 未通过校验时，不继续透传高风险自由文本命令，
                # 避免把粘连/脚本化命令污染到执行与展示链路。
                command = ""
                command_spec = {}

        if not action_text and not action_title and not command:
            continue
        expected = _normalize_action_text(item_dict.get("expected_outcome"))
        command_meta = _resolve_action_command_meta(command)
        command_type = _as_str(command_meta.get("command_type"), "unknown")
        model_action_type = _normalize_action_text(item_dict.get("action_type")).lower()
        model_command_type = _normalize_action_text(item_dict.get("command_type")).lower()
        if model_action_type not in {"query", "write", "manual"}:
            model_action_type = ""
        # 模型返回 unknown 时，优先采用本地分类结果，避免可识别命令退化为人工语义确认。
        if model_command_type not in {"query", "repair"}:
            model_command_type = ""
        resolved_command_type = (
            command_type
            if command_type in {"query", "repair"}
            else (model_command_type or "unknown")
        )
        derived_action_type = (
            "query"
            if resolved_command_type == "query"
            else "write"
            if resolved_command_type == "repair"
            else "manual"
        )
        if model_action_type == "manual":
            resolved_action_type = (
                "manual"
                if (not bool(command and command_meta.get("supported")) or derived_action_type == "manual")
                else derived_action_type
            )
        else:
            resolved_action_type = derived_action_type
        resolved_write_permission = bool(command_meta.get("requires_write_permission"))
        if item_dict.get("requires_write_permission") is not None:
            resolved_write_permission = _as_bool(item_dict.get("requires_write_permission"), resolved_write_permission)
        resolved_executable = bool(command and command_meta.get("supported"))
        spec_repaired = had_invalid_or_missing_spec and not command_spec_compile_reason and bool(command_spec)
        if item_dict.get("executable") is not None and not spec_repaired:
            resolved_executable = _as_bool(item_dict.get("executable"), resolved_executable) and bool(command and command_meta.get("supported"))
        resolved_confirmation = bool(command and command_meta.get("supported"))
        if item_dict.get("requires_confirmation") is not None and not spec_repaired:
            resolved_confirmation = _as_bool(item_dict.get("requires_confirmation"), resolved_confirmation)
        resolved_requires_elevation = resolved_write_permission
        if item_dict.get("requires_elevation") is not None:
            resolved_requires_elevation = _as_bool(item_dict.get("requires_elevation"), resolved_requires_elevation)
        resolved_reason = (
            _normalize_action_text(item_dict.get("reason"))
            or command_spec_compile_reason
            or _as_str(command_meta.get("reason"))
        )
        if spec_repaired and command and isinstance(command_spec, dict) and command_spec:
            lowered_reason = _as_str(resolved_reason).lower()
            if any(
                token in lowered_reason
                for token in [
                    "missing_structured_spec",
                    "missing_or_invalid_command_spec",
                    "unsupported_command_head",
                    "glued_command_tokens",
                    "invalid_kubectl_token",
                ]
            ):
                resolved_reason = ""
        index_counter += 1
        _append_action(
            {
                "id": _new_action_id(index_counter, "lc"),
                "source": "langchain",
                "priority": max(1, int(_as_float(item_dict.get("priority"), index_counter))),
                "title": (action_title or action_text or f"执行命令: {command}")[:220],
                "purpose": expected[:220],
                "question": "",
                "action_type": resolved_action_type,
                "command": command,
                "command_spec": command_spec if isinstance(command_spec, dict) else {},
                "command_type": resolved_command_type or "unknown",
                "risk_level": _normalize_action_text(item_dict.get("risk_level")).lower() or _as_str(command_meta.get("risk_level"), "high"),
                "executable": resolved_executable,
                "requires_confirmation": resolved_confirmation,
                "requires_write_permission": resolved_write_permission,
                "requires_elevation": resolved_requires_elevation,
                "reason": resolved_reason,
                "spec_repaired": bool(spec_repaired),
                "spec_repair_from_reason": initial_spec_compile_reason if spec_repaired else "",
            }
        )

    for command in _extract_commands_from_message_content(answer, limit=safe_max_items):
        if len(actions) >= safe_max_items:
            break
        command_text = _as_str(command)
        if not command_text:
            continue
        display_command = command_text
        inferred_spec, inferred_reason, inferred_command = _try_repair_structured_spec(
            command=command_text,
            command_spec={},
            compile_reason="missing_structured_spec",
        )
        if inferred_command:
            command_text = inferred_command
        if inferred_reason:
            command_text = ""
        command_meta = _resolve_action_command_meta(command_text)
        inferred_spec_ready = bool(isinstance(inferred_spec, dict) and inferred_spec and command_text)
        inferred_supported = bool(command_text and command_meta.get("supported"))
        # 来自回答正文的自由文本命令只用于“排查建议”，不直接自动执行。
        # 原因：该来源缺少结构化上下文约束，容易出现半截命令/语义漂移（例如 app=que）。
        answer_reason = inferred_reason or _as_str(command_meta.get("reason"))
        if not inferred_reason and inferred_spec_ready and inferred_supported:
            answer_reason = "answer_command_requires_structured_action"
        index_counter += 1
        action_title = (
            f"执行命令: {command_text}"[:220]
            if command_text
            else f"来自回答正文的命令建议（需补全结构化命令）: {display_command}"[:220]
        )
        _append_action(
            {
                "id": _new_action_id(index_counter, "ans"),
                "source": "answer_command",
                "priority": index_counter,
                "title": action_title,
                "purpose": "来自回答正文的命令建议",
                "question": "",
                "action_type": "query"
                if _as_str(command_meta.get("command_type")) == "query"
                else "write"
                if _as_str(command_meta.get("command_type")) == "repair"
                else "manual",
                "command": command_text,
                "command_spec": inferred_spec if (isinstance(inferred_spec, dict) and command_text) else {},
                "command_type": _as_str(command_meta.get("command_type"), "unknown"),
                "risk_level": _as_str(command_meta.get("risk_level"), "high"),
                "executable": False,
                "requires_confirmation": False,
                "requires_write_permission": bool(command_meta.get("requires_write_permission")),
                "requires_elevation": bool(command_meta.get("requires_write_permission")),
                "reason": answer_reason,
            }
        )

    for item in _as_list((reflection or {}).get("next_actions")):
        if len(actions) >= safe_max_items:
            break
        action_text = _normalize_action_text(item)
        if not action_text:
            continue
        commands = _extract_commands_from_message_content(action_text, limit=1)
        command = _as_str(commands[0]) if commands else ""
        inferred_spec, inferred_reason, inferred_command = _try_repair_structured_spec(
            command=command,
            command_spec={},
            compile_reason="missing_structured_spec",
        )
        if inferred_command:
            command = inferred_command
        if inferred_reason:
            command = ""
        command_meta = _resolve_action_command_meta(command)
        index_counter += 1
        _append_action(
            {
                "id": _new_action_id(index_counter, "rf"),
                "source": "reflection",
                "priority": index_counter,
                "title": action_text[:220],
                "purpose": "来自反思闭环的下一步动作",
                "question": "",
                "action_type": "query"
                if _as_str(command_meta.get("command_type")) == "query"
                else "write"
                if _as_str(command_meta.get("command_type")) == "repair"
                else "manual",
                "command": command,
                "command_spec": inferred_spec if (isinstance(inferred_spec, dict) and command) else {},
                "command_type": _as_str(command_meta.get("command_type"), "unknown"),
                "risk_level": _as_str(command_meta.get("risk_level"), "high"),
                "executable": bool(command and command_meta.get("supported") and inferred_spec),
                "requires_confirmation": bool(command and command_meta.get("supported") and inferred_spec),
                "requires_write_permission": bool(command_meta.get("requires_write_permission")),
                "requires_elevation": bool(command_meta.get("requires_write_permission")),
                "reason": inferred_reason or _as_str(command_meta.get("reason")),
            }
        )

    return actions[:safe_max_items]


def _build_followup_react_loop(
    *,
    actions: List[Dict[str, Any]],
    action_observations: List[Dict[str, Any]],
    analysis_context: Optional[Dict[str, Any]] = None,
    max_next_actions: int = 4,
) -> Dict[str, Any]:
    """构建 ReAct 可控闭环状态：Plan -> Policy -> Execute -> Observe -> Replan."""
    safe_actions = [item for item in _as_list(actions) if isinstance(item, dict)]
    safe_observations = [item for item in _as_list(action_observations) if isinstance(item, dict)]
    obs_by_action_id = {
        _as_str(item.get("action_id")): item
        for item in safe_observations
        if _as_str(item.get("action_id"))
    }
    obs_by_command: Dict[str, Dict[str, Any]] = {}
    for item in safe_observations:
        command_key = _normalize_followup_command_match_key(_as_str(item.get("command")))
        if not command_key:
            continue
        if command_key not in obs_by_command:
            obs_by_command[command_key] = item

    def _resolve_action_observation(action_payload: Dict[str, Any]) -> Optional[Dict[str, Any]]:
        safe_action_id = _as_str(action_payload.get("id")).strip()
        safe_command = _as_str(action_payload.get("command")).strip()
        observation = obs_by_action_id.get(safe_action_id)
        if observation is None and safe_command:
            observation = obs_by_command.get(_normalize_followup_command_match_key(safe_command))
        return observation

    def _resolve_evidence_slot_id(
        action_payload: Dict[str, Any],
        fallback_index: int,
    ) -> str:
        explicit_slot_id = _as_str(action_payload.get("evidence_slot_id")).strip()
        if explicit_slot_id:
            return explicit_slot_id
        action_id = _as_str(action_payload.get("id")).strip()
        if action_id:
            return f"action:{action_id}"
        command = _as_str(action_payload.get("command")).strip()
        command_match_key = _normalize_followup_command_match_key(command)
        if command_match_key:
            return f"command:{command_match_key}"
        return f"slot:{fallback_index}"

    def _resolve_expected_signal(action_payload: Dict[str, Any]) -> str:
        return (
            _as_str(action_payload.get("expected_signal")).strip()
            or _as_str(action_payload.get("purpose")).strip()
        )

    def _match_expected_signal(
        expected_signal: str,
        observation_payload: Dict[str, Any],
    ) -> tuple[bool, str]:
        safe_expected = _as_str(expected_signal).strip().lower()
        if not safe_expected:
            return True, "expected_signal_missing"
        expected_tokens = [
            token
            for token in re.findall(r"[a-z0-9_./:-]{4,}|[\u4e00-\u9fff]{2,}", safe_expected, flags=re.IGNORECASE)
            if token not in {"证据", "命令", "日志", "查询", "输出", "current", "error", "warn"}
        ][:8]
        if not expected_tokens:
            return True, "expected_signal_tokens_empty"
        evidence_text = " ".join(
            [
                _as_str(observation_payload.get("stdout")),
                _as_str(observation_payload.get("stderr")),
                _as_str(observation_payload.get("message")),
                _as_str(observation_payload.get("detail")),
                _as_str(observation_payload.get("text")),
            ]
        ).lower()
        if not evidence_text.strip():
            return False, "observation_output_empty"
        matched_tokens = [token for token in expected_tokens if token in evidence_text]
        min_required = 1 if len(expected_tokens) <= 2 else 2
        if len(matched_tokens) >= min_required:
            return True, f"matched_tokens={','.join(matched_tokens[:4])}"
        return False, f"matched_tokens={','.join(matched_tokens[:4]) or 'none'}"

    def _resolve_evidence_quality(observation_payload: Dict[str, Any]) -> str:
        status = _as_str(observation_payload.get("status")).strip().lower()
        if status != "executed":
            return "none"
        exit_code = int(_as_float(observation_payload.get("exit_code"), 0))
        if exit_code != 0:
            return "none"
        if bool(observation_payload.get("output_truncated")):
            return "partial"
        return "full"

    full_evidence_by_command: Dict[str, Dict[str, Any]] = {}
    full_evidence_by_run_id: Dict[str, Dict[str, Any]] = {}
    for obs in safe_observations:
        obs_dict = obs if isinstance(obs, dict) else {}
        if _resolve_evidence_quality(obs_dict) != "full":
            continue
        command_key = _normalize_followup_command_match_key(_as_str(obs_dict.get("command")))
        if command_key and command_key not in full_evidence_by_command:
            full_evidence_by_command[command_key] = obs_dict
        command_run_id = _as_str(obs_dict.get("command_run_id")).strip()
        if command_run_id and command_run_id not in full_evidence_by_run_id:
            full_evidence_by_run_id[command_run_id] = obs_dict

    plan_total = len(safe_actions)
    query_total = sum(1 for item in safe_actions if _as_str(item.get("command_type")) == "query")
    write_total = sum(1 for item in safe_actions if _as_str(item.get("command_type")) == "repair")
    unknown_total = sum(
        1
        for item in safe_actions
        if _as_str(item.get("command_type")) == "unknown" and _as_str(item.get("command")).strip()
    )
    executable_total = sum(1 for item in safe_actions if bool(item.get("executable")))
    non_executable_query_like_total = 0
    spec_blocked_total = 0
    for action in safe_actions:
        action_dict = action if isinstance(action, dict) else {}
        if bool(action_dict.get("executable")):
            continue
        command_type = _as_str(action_dict.get("command_type")).strip().lower()
        text_blob = " ".join(
            [
                _as_str(action_dict.get("title")),
                _as_str(action_dict.get("purpose")),
                _as_str(action_dict.get("reason")),
            ]
        ).lower()
        if (
            command_type in {"query", "unknown"}
            or any(token in text_blob for token in ["查询", "query", "日志", "sql", "trace"])
        ):
            non_executable_query_like_total += 1
        reason_text = _as_str(action_dict.get("reason")).lower()
        if any(
            marker in reason_text
            for marker in [
                "missing_structured_spec",
                "missing_or_invalid_command_spec",
                "glued_sql_tokens",
                "glued_command_tokens",
                "invalid_kubectl_token",
                "command_argv contains blocked shell operators",
                "unsupported_command_head",
                "semantic_incomplete",
            ]
        ):
            spec_blocked_total += 1

    executed_success = 0
    executed_failed = 0
    skipped_total = 0
    skipped_by_policy_total = 0
    skipped_duplicate_total = 0
    skipped_backend_unready_total = 0
    semantic_incomplete_total = 0
    permission_required = 0
    confirmation_required = 0
    elevation_required = 0
    for obs in safe_observations:
        status = _as_str(obs.get("status")).lower()
        if status == "executed":
            exit_code = int(_as_float(obs.get("exit_code"), 0))
            if exit_code == 0:
                executed_success += 1
            else:
                executed_failed += 1
        elif status == "failed":
            executed_failed += 1
        elif status == "skipped":
            skipped_total += 1
            reason_code = _as_str(obs.get("reason_code")).strip().lower()
            if reason_code == "duplicate_skipped":
                skipped_duplicate_total += 1
            elif reason_code == "backend_unready":
                skipped_backend_unready_total += 1
            else:
                skipped_by_policy_total += 1
        elif status == "semantic_incomplete":
            semantic_incomplete_total += 1
        elif status == "permission_required":
            permission_required += 1
        elif status == "confirmation_required":
            confirmation_required += 1
        elif status == "elevation_required":
            elevation_required += 1

    unresolved: List[Dict[str, Any]] = []
    for action in safe_actions:
        action_id = _as_str(action.get("id"))
        command = _as_str(action.get("command"))
        obs = _resolve_action_observation(action)
        if obs is None:
            if bool(action.get("executable")) and _as_str(action.get("command_type")) == "query":
                unresolved.append(
                    {
                        "action_id": action_id,
                        "reason": "query_not_observed",
                        "title": _as_str(action.get("title")),
                        "command": command,
                    }
                )
            continue

        status = _as_str(obs.get("status")).lower()
        if status == "executed":
            exit_code = int(_as_float(obs.get("exit_code"), 0))
            if exit_code == 0:
                if bool(obs.get("output_truncated")):
                    unresolved.append(
                        {
                            "action_id": action_id,
                            "reason": "partial_evidence",
                            "title": _as_str(action.get("title")),
                            "command": command,
                            "message": "命令输出被截断，证据不完整。",
                            "command_type": _as_str(action.get("command_type")),
                        }
                    )
                    continue
                expected_signal = _resolve_expected_signal(action if isinstance(action, dict) else {})
                signal_match, signal_reason = _match_expected_signal(expected_signal, obs if isinstance(obs, dict) else {})
                if not signal_match:
                    unresolved.append(
                        {
                            "action_id": action_id,
                            "reason": "signal_not_matched",
                            "title": _as_str(action.get("title")),
                            "command": command,
                            "message": f"命令执行成功但未命中预期证据信号：{signal_reason}",
                            "command_type": _as_str(action.get("command_type")),
                        }
                    )
                    continue
                continue
            unresolved.append(
                {
                    "action_id": action_id,
                    "reason": "query_failed",
                    "title": _as_str(action.get("title")),
                    "command": command,
                    "message": _as_str(obs.get("stderr") or obs.get("message")),
                }
            )
            continue

        if status in {
            "failed",
            "skipped",
            "semantic_incomplete",
            "permission_required",
            "confirmation_required",
            "elevation_required",
        }:
            unresolved_item = {
                "action_id": action_id,
                "reason": status,
                "title": _as_str(action.get("title")),
                "command": command,
                "message": _as_str(obs.get("message") or obs.get("stderr")),
                "command_type": _as_str(action.get("command_type")),
            }
            if status == "skipped":
                unresolved_item["reason_code"] = _as_str(obs.get("reason_code")).strip().lower()
            unresolved.append(unresolved_item)

    next_actions: List[str] = []
    replan_items: List[Dict[str, Any]] = []
    replan_candidate_count = 0
    for item in unresolved:
        reason = _as_str(item.get("reason"))
        command = _as_str(item.get("command"))
        message = _as_str(item.get("message"))
        command_type = _as_str(item.get("command_type")).lower()
        execution_disposition = "manual_followup_needed"
        needs_replan = True
        append_next_action = True
        if reason in {"permission_required", "confirmation_required", "elevation_required"}:
            if command_type == "unknown" or "占位符" in message or "语义" in message:
                next_line = f"补全命令参数后继续执行：{command}"
                execution_disposition = "semantic_completion_required"
            else:
                next_line = f"对命令进行人工确认/提权后再执行：{command}"
                execution_disposition = "approval_required"
        elif reason in {"query_failed", "failed"}:
            if message:
                next_line = f"复核并重试命令：{command}（失败信息：{message[:120]}）"
            else:
                next_line = f"复核并重试命令：{command}"
            execution_disposition = "failed"
        elif reason == "partial_evidence":
            next_line = f"补采完整输出后再评估：{command}"
            execution_disposition = "partial_evidence"
        elif reason == "signal_not_matched":
            next_line = f"命令输出未命中预期信号，需改用更贴近故障信号的命令：{command}"
            execution_disposition = "signal_not_matched"
        elif reason == "semantic_incomplete":
            next_line = f"补全命令参数后继续执行：{command}"
            execution_disposition = "semantic_completion_required"
        elif reason == "skipped":
            reason_code = _as_str(item.get("reason_code")).strip().lower()
            if reason_code == "duplicate_skipped":
                next_line = f"同一 run 已执行过该命令，无需重试：{command}"
                execution_disposition = "skipped_duplicate"
                needs_replan = False
            elif reason_code == "backend_unready":
                next_line = f"执行网关未就绪，建议稍后重试：{command}"
                execution_disposition = "backend_unready"
            else:
                next_line = f"该命令未被系统自动执行，原因是策略限制：{command}"
                execution_disposition = "skipped_by_policy"
                append_next_action = False
        elif reason == "query_not_observed":
            next_line = f"补执行查询命令并回填观察：{command}"
            execution_disposition = "observation_missing"
        else:
            next_line = _as_str(item.get("title"))
        replan_items.append(
            {
                "action_id": _as_str(item.get("action_id")),
                "reason": reason,
                "command": command,
                "message": message,
                "reason_code": _as_str(item.get("reason_code")).strip().lower(),
                "title": _as_str(item.get("title")),
                "summary": next_line,
                "execution_disposition": execution_disposition,
            }
        )
        if not needs_replan:
            continue
        replan_candidate_count += 1
        if not append_next_action:
            continue
        if len(next_actions) >= max(1, int(max_next_actions or 4)):
            continue
        if next_line and next_line not in next_actions:
            next_actions.append(next_line)
    no_executable_query_candidates = (
        plan_total > 0
        and executable_total <= 0
        and len(safe_observations) <= 0
        and (
            query_total > 0
            or non_executable_query_like_total > 0
            or spec_blocked_total > 0
        )
    )
    if no_executable_query_candidates:
        hint_lines = _build_non_executable_query_command_hints(
            safe_actions,
            analysis_context=analysis_context if isinstance(analysis_context, dict) else {},
            max_items=max_next_actions,
        )
        generated_ready_templates = 0
        for line in hint_lines:
            suggested_command = _extract_command_from_hint_line(line)
            _, suggested_command_spec = _infer_query_template_command_spec(suggested_command)
            template_summary = line
            template_disposition = "structured_spec_required"
            template_message = "generated_non_executable_query_template"
            template_title = "建议补全命令模板"
            if isinstance(suggested_command_spec, dict) and suggested_command_spec:
                generated_ready_templates += 1
                template_summary = line.replace(
                    "补全结构化命令后执行：",
                    "可直接执行（已生成 command_spec）：",
                    1,
                )
                template_disposition = "structured_spec_ready"
                template_message = "generated_structured_query_template"
                template_title = "建议执行结构化命令"
            if len(next_actions) < max(1, int(max_next_actions or 4)) and template_summary not in next_actions:
                next_actions.append(template_summary)
            replan_items.append(
                {
                    "action_id": "",
                    "reason": "command_template_suggested",
                    "command": "",
                    "message": template_message,
                    "title": template_title,
                    "summary": template_summary,
                    "execution_disposition": template_disposition,
                    "suggested_command": suggested_command,
                    "suggested_command_spec": suggested_command_spec,
                }
            )
        if generated_ready_templates > 0:
            no_exec_summary = (
                "当前计划暂无自动执行动作，但已生成结构化查询命令模板；"
                "可直接执行或确认后继续。"
            )
            no_exec_disposition = "structured_spec_ready"
        else:
            no_exec_summary = "当前计划没有可自动执行的结构化查询命令，请先补全 command_spec 后继续执行。"
            no_exec_disposition = "structured_spec_required"
        if no_exec_summary not in next_actions and len(next_actions) < max(1, int(max_next_actions or 4)):
            next_actions.insert(0, no_exec_summary)
        replan_items.append(
            {
                "action_id": "",
                "reason": "no_executable_query_candidates",
                "command": "",
                "message": no_exec_summary,
                "title": "缺少可执行查询命令",
                "summary": no_exec_summary,
                "execution_disposition": no_exec_disposition,
            }
        )
    if unknown_total > 0 and len(next_actions) < max(1, int(max_next_actions or 4)):
        next_actions.append("存在语义不完整动作，已转入补充语义流程后继续执行。")

    evidence_slot_map: Dict[str, Dict[str, Any]] = {}
    required_evidence_slots: List[str] = []
    missing_evidence_slots: List[str] = []
    observed_executable_actions = 0
    evidence_filled_slots = 0
    evidence_reused_slots = 0
    evidence_missing_slots = 0
    evidence_partial_slots = 0
    slot_index = 0
    for action in safe_actions:
        action_payload = action if isinstance(action, dict) else {}
        if not bool(action_payload.get("executable")):
            continue
        slot_index += 1
        action_id = _as_str(action_payload.get("id")).strip()
        command = _as_str(action_payload.get("command")).strip()
        title = _as_str(action_payload.get("title")).strip()
        slot_id = _resolve_evidence_slot_id(action_payload, slot_index)
        required_evidence_slots.append(slot_id)

        obs = _resolve_action_observation(action_payload)
        outcome = "missing"
        reused = False
        reason_code = ""
        evidence_quality = "none"
        evidence_ids: List[str] = []
        expected_signal = _resolve_expected_signal(action_payload)
        signal_match = False
        signal_match_reason = "observation_missing"
        if obs is not None:
            observed_executable_actions += 1
            status = _as_str(obs.get("status")).strip().lower()
            reason_code = _as_str(obs.get("reason_code")).strip().lower()
            command_run_id = _as_str(obs.get("command_run_id")).strip()
            observation_payload = obs if isinstance(obs, dict) else {}
            evidence_quality = _resolve_evidence_quality(observation_payload)
            signal_match, signal_match_reason = _match_expected_signal(expected_signal, observation_payload)
            if status == "executed" and int(_as_float(obs.get("exit_code"), 0)) == 0:
                if evidence_quality == "full" and signal_match:
                    outcome = "filled"
                elif evidence_quality == "partial":
                    outcome = "partial"
                    reason_code = reason_code or "output_truncated"
                else:
                    outcome = "missing"
                if command_run_id:
                    evidence_ids.append(command_run_id)
            elif status == "skipped" and reason_code == "duplicate_skipped":
                supporting_obs: Optional[Dict[str, Any]] = None
                command_key = _normalize_followup_command_match_key(command)
                if command_run_id and command_run_id in full_evidence_by_run_id:
                    supporting_obs = full_evidence_by_run_id.get(command_run_id)
                elif command_key and command_key in full_evidence_by_command:
                    supporting_obs = full_evidence_by_command.get(command_key)
                if isinstance(supporting_obs, dict):
                    signal_match, signal_match_reason = _match_expected_signal(expected_signal, supporting_obs)
                    evidence_quality = "reused"
                    if signal_match:
                        outcome = "reused"
                        reused = True
                    else:
                        outcome = "missing"
                        reason_code = "duplicate_reuse_signal_mismatch"
                else:
                    outcome = "missing"
                    reason_code = "duplicate_reuse_without_valid_source"
                    evidence_quality = "none"
                if command_run_id:
                    evidence_ids.append(command_run_id)

        if outcome in {"filled", "reused"}:
            evidence_filled_slots += 1
            if outcome == "reused":
                evidence_reused_slots += 1
        else:
            evidence_missing_slots += 1
            missing_evidence_slots.append(slot_id)
            if outcome == "partial":
                evidence_partial_slots += 1

        evidence_slot_map[slot_id] = {
            "slot_id": slot_id,
            "required": True,
            "status": outcome,
            "action_id": action_id,
            "title": title,
            "command": command,
            "reason_code": reason_code,
            "evidence_reuse": reused,
            "evidence_ids": evidence_ids,
            "expected_signal": expected_signal,
            "evidence_quality": evidence_quality,
            "signal_match": signal_match,
            "signal_match_reason": signal_match_reason,
        }

    next_best_commands: List[Dict[str, Any]] = []
    next_best_seen: set[str] = set()
    for item in replan_items:
        replan_item = item if isinstance(item, dict) else {}
        command = _as_str(replan_item.get("command")).strip() or _as_str(replan_item.get("suggested_command")).strip()
        if not command:
            continue
        execution_disposition = _as_str(replan_item.get("execution_disposition")).strip().lower()
        reason = _as_str(replan_item.get("reason")).strip().lower()
        if execution_disposition == "skipped_duplicate":
            continue
        action_id = _as_str(replan_item.get("action_id")).strip()
        slot_id = f"action:{action_id}" if action_id else f"command:{_normalize_followup_command_match_key(command) or command[:80]}"
        dedupe_key = f"{slot_id}|{command}"
        if dedupe_key in next_best_seen:
            continue
        next_best_seen.add(dedupe_key)
        why = _as_str(replan_item.get("summary")).strip()
        if not why:
            why = "补齐当前证据缺口，提升结论确定性。"
        expected_signal = "返回可直接确认/排除根因的关键证据。"
        if reason in {"query_failed", "failed"}:
            expected_signal = "确认失败原因是否可复现，或异常是否已经消失。"
        elif execution_disposition == "semantic_completion_required":
            expected_signal = "补齐可执行参数后，确认该证据槽位是否可被填补。"
        next_best_commands.append(
            {
                "slot_id": slot_id,
                "action_id": action_id or None,
                "command": command,
                "why": why,
                "expected_signal": expected_signal,
                "branch_if_positive": "证据补齐后收敛根因候选并提高置信度。",
                "branch_if_negative": "转向下一个证据槽位继续排查。",
                "execution_disposition": execution_disposition or "followup",
                "reason": reason or execution_disposition or "followup",
            }
        )
        if len(next_best_commands) >= min(2, max(1, int(max_next_actions or 4))):
            break

    plan_coverage = 1.0 if plan_total <= 0 else round(min(1.0, len(obs_by_action_id) / max(plan_total, 1)), 2)
    exec_coverage = 1.0 if executable_total <= 0 else round(min(1.0, observed_executable_actions / max(executable_total, 1)), 2)
    evidence_coverage = 1.0 if len(required_evidence_slots) <= 0 else round(min(1.0, evidence_filled_slots / max(len(required_evidence_slots), 1)), 2)

    model_confidence = 0.35
    if plan_total > 0:
        model_confidence += (executed_success / plan_total) * 0.45
        model_confidence -= (executed_failed / plan_total) * 0.25
    model_confidence = round(max(0.1, min(0.95, model_confidence)), 2)

    evidence_confidence = 0.25
    evidence_confidence += evidence_coverage * 0.55
    evidence_confidence += exec_coverage * 0.20
    if executable_total > 0:
        evidence_confidence -= (executed_failed / max(executable_total, 1)) * 0.20
    evidence_confidence = round(max(0.1, min(0.98, evidence_confidence)), 2)
    final_confidence = round(max(0.1, min(0.98, (model_confidence * 0.35) + (evidence_confidence * 0.65))), 2)

    replan_needed = (
        replan_candidate_count > 0
        or executed_failed > 0
        or skipped_by_policy_total > 0
        or skipped_backend_unready_total > 0
        or unknown_total > 0
        or no_executable_query_candidates
        or evidence_missing_slots > 0
    )
    if (
        evidence_missing_slots > 0
        and skipped_by_policy_total <= 0
        and len(next_actions) < max(1, int(max_next_actions or 4))
    ):
        for slot_id in missing_evidence_slots:
            slot_payload = evidence_slot_map.get(slot_id) if isinstance(evidence_slot_map.get(slot_id), dict) else {}
            command = _as_str(slot_payload.get("command")).strip()
            if not command:
                continue
            line = f"补齐证据槽位 {slot_id}：{command}"
            if line not in next_actions:
                next_actions.append(line)
            if len(next_actions) >= max(1, int(max_next_actions or 4)):
                break
    summary = (
        f"plan={plan_total}, observed={len(obs_by_action_id)}, "
        f"success={executed_success}, failed={executed_failed}, "
        f"skipped_policy={skipped_by_policy_total}, skipped_duplicate={skipped_duplicate_total}, "
        f"exec_coverage={exec_coverage}, evidence_coverage={evidence_coverage}, partial_evidence={evidence_partial_slots}, "
        f"replan={str(replan_needed).lower()}"
    )

    return {
        "phase": "replan" if replan_needed else "finalized",
        "plan": {
            "total_actions": plan_total,
            "query_actions": query_total,
            "write_actions": write_total,
            "unknown_actions": unknown_total,
            "executable_actions": executable_total,
            "non_executable_query_like_actions": non_executable_query_like_total,
            "spec_blocked_actions": spec_blocked_total,
        },
        "policy": {
            "permission_required": permission_required,
            "confirmation_required": confirmation_required,
            "elevation_required": elevation_required,
            "semantic_incomplete": semantic_incomplete_total,
            "write_actions_blocked_by_default": True,
        },
        "execute": {
            "observed_actions": len(obs_by_action_id),
            "executed_success": executed_success,
            "executed_failed": executed_failed,
            "skipped": skipped_total,
            "skipped_by_policy": skipped_by_policy_total,
            "skipped_duplicate": skipped_duplicate_total,
            "skipped_backend_unready": skipped_backend_unready_total,
        },
        "observe": {
            # Backward-compatible aliases: existing code paths still read coverage/confidence.
            "coverage": plan_coverage,
            "unresolved_actions": len(unresolved),
            "confidence": model_confidence,
            "plan_coverage": plan_coverage,
            "exec_coverage": exec_coverage,
            "evidence_coverage": evidence_coverage,
            "model_confidence": model_confidence,
            "evidence_confidence": evidence_confidence,
            "final_confidence": final_confidence,
            "required_evidence_slots": required_evidence_slots,
            "missing_evidence_slots": missing_evidence_slots,
            "evidence_filled_slots": evidence_filled_slots,
            "evidence_reused_slots": evidence_reused_slots,
            "evidence_missing_slots": evidence_missing_slots,
            "evidence_partial_slots": evidence_partial_slots,
            "evidence_slot_map": evidence_slot_map,
        },
        "replan": {
            "needed": replan_needed,
            "skipped_total": skipped_total,
            "skipped_by_policy": skipped_by_policy_total,
            "skipped_duplicate": skipped_duplicate_total,
            "skipped_backend_unready": skipped_backend_unready_total,
            "items": replan_items,
            "next_actions": next_actions,
            "next_best_commands": next_best_commands,
        },
        "summary": summary,
    }


def _append_followup_react_summary(
    *,
    answer: str,
    react_loop: Dict[str, Any],
) -> str:
    """在回答末尾附加闭环结果，便于前端与用户快速确认执行状态。"""
    base = _as_str(answer).strip()
    if not base:
        base = "暂无回答"
    loop = react_loop if isinstance(react_loop, dict) else {}
    execute = loop.get("execute") if isinstance(loop.get("execute"), dict) else {}
    observe = loop.get("observe") if isinstance(loop.get("observe"), dict) else {}
    plan = loop.get("plan") if isinstance(loop.get("plan"), dict) else {}
    replan = loop.get("replan") if isinstance(loop.get("replan"), dict) else {}
    observed = int(_as_float(execute.get("observed_actions"), 0))
    executable_actions = int(_as_float(plan.get("executable_actions"), 0))
    replan_needed = bool(replan.get("needed"))
    if observed <= 0 and not replan_needed:
        return base

    lines = [
        base,
        "",
        "闭环状态：",
        (
            f"- 自动观察: observed={observed}, success={int(_as_float(execute.get('executed_success'), 0))}, "
            f"failed={int(_as_float(execute.get('executed_failed'), 0))}"
        ),
        (
            f"- 覆盖率: plan={observe.get('plan_coverage', observe.get('coverage'))}, "
            f"exec={observe.get('exec_coverage', observe.get('coverage'))}, "
            f"evidence={observe.get('evidence_coverage', observe.get('coverage'))}"
        ),
        (
            f"- 置信度: model={observe.get('model_confidence', observe.get('confidence'))}, "
            f"evidence={observe.get('evidence_confidence', observe.get('confidence'))}, "
            f"final={observe.get('final_confidence', observe.get('confidence'))}"
        ),
    ]
    if observed <= 0 and executable_actions <= 0:
        lines.append("- 当前没有生成通过校验的结构化命令，以上结论仍属于待验证诊断草稿。")
    next_actions = _as_list(replan.get("next_actions"))
    if replan_needed and next_actions:
        lines.append("- 下一步:")
        for item in next_actions[:4]:
            item_text = _as_str(item)
            if item_text:
                lines.append(f"  - {item_text}")
    skipped_by_policy = int(_as_float(replan.get("skipped_by_policy"), 0))
    if skipped_by_policy > 0:
        lines.append(f"- 策略未自动执行: {skipped_by_policy} 条，详情见执行状态。")
    missing_slots = [item for item in _as_list(observe.get("missing_evidence_slots")) if _as_str(item)]
    if missing_slots:
        lines.append(f"- 待补证据槽位: {', '.join(missing_slots[:4])}")
    return "\n".join(lines).strip()


def _prioritize_followup_actions_with_react_memory(
    *,
    actions: List[Dict[str, Any]],
    react_memory: Dict[str, Any],
    max_items: int = 8,
    max_append: int = 2,
) -> List[Dict[str, Any]]:
    """把 react_memory 里的失败命令前置为下一轮优先动作。"""
    safe_max_items = max(1, min(int(max_items or 8), 20))
    safe_max_append = max(0, min(int(max_append or 2), 5))
    safe_actions = [dict(item) for item in _as_list(actions) if isinstance(item, dict)]
    failed_commands = [
        _as_str(item)
        for item in _as_list((react_memory or {}).get("failed_commands"))
        if _as_str(item)
    ]
    if not failed_commands:
        return safe_actions[:safe_max_items]

    command_rank: Dict[str, int] = {}
    for index, command in enumerate(failed_commands):
        if command not in command_rank:
            command_rank[command] = index

    existing_commands = {_as_str(item.get("command")) for item in safe_actions if _as_str(item.get("command"))}
    append_actions: List[Dict[str, Any]] = []
    append_counter = 0
    for command in failed_commands:
        if append_counter >= safe_max_append:
            break
        if command in existing_commands:
            continue
        command_meta = _resolve_action_command_meta(command)
        append_counter += 1
        append_actions.append(
            {
                "id": f"rm-{append_counter}",
                "source": "react_memory_retry",
                "priority": 1,
                "title": f"复核历史失败命令: {command}"[:220],
                "purpose": "来自上一轮失败命令，优先复核执行环境与参数",
                "question": "",
                "action_type": "query"
                if _as_str(command_meta.get("command_type")) == "query"
                else "write"
                if _as_str(command_meta.get("command_type")) == "repair"
                else "manual",
                "command": command,
                "command_type": _as_str(command_meta.get("command_type"), "unknown"),
                "risk_level": _as_str(command_meta.get("risk_level"), "high"),
                "executable": bool(command and command_meta.get("supported")),
                "requires_confirmation": bool(command and command_meta.get("supported")),
                "requires_write_permission": bool(command_meta.get("requires_write_permission")),
                "requires_elevation": bool(command_meta.get("requires_write_permission")),
                "reason": "来自 react memory 的失败命令重试建议",
            }
        )
        existing_commands.add(command)

    merged: List[Dict[str, Any]] = safe_actions + append_actions
    indexed: List[Dict[str, Any]] = []
    for idx, action in enumerate(merged):
        action_command = _as_str(action.get("command"))
        priority_rank = command_rank.get(action_command, len(command_rank) + idx)
        indexed.append({"index": idx, "priority_rank": priority_rank, "action": action})

    indexed.sort(key=lambda item: (item["priority_rank"], item["index"]))
    prioritized: List[Dict[str, Any]] = []
    for order, item in enumerate(indexed[:safe_max_items], start=1):
        action = dict(item["action"])
        action["priority"] = order
        action["react_memory_priority"] = bool(_as_str(action.get("command")) in command_rank)
        prioritized.append(action)
    return prioritized
