"""
LangChain follow-up 运行时。
"""

import asyncio
import json
import logging
import os
import re
from typing import Any, Callable, Dict, List, Optional

from ai.langchain_runtime.memory import build_memory_context
from ai.langchain_runtime.prompts import FOLLOWUP_SYSTEM_PROMPT, build_followup_prompt
from ai.langchain_runtime.schemas import StructuredAnswer
from ai.langchain_runtime.tools import collect_tool_observations
from ai.followup_command_spec import compile_followup_command_spec, normalize_followup_command_spec
from ai.llm_stream_helpers import collect_chat_response

try:
    from langchain_core.output_parsers import PydanticOutputParser
except Exception:  # pragma: no cover - 依赖可选
    PydanticOutputParser = None

logger = logging.getLogger(__name__)
_KUBECTL_VERB_PATTERN = (
    "getpods|get|describe|logs|exec|top|rollout|apply|delete|patch|edit|replace|scale|set|annotate|label|"
    "create|expose|autoscale|cordon|uncordon|drain|taint"
)
_DEFERRED_STRUCTURED_SPEC_REASONS = {
    "pod_name_resolution_failed",
    "clickhouse pod not found",
}
_CLICKHOUSE_SQL_MULTIWORD_KEYWORD_REPAIRS = (
    ("ORDERBY", "ORDER BY"),
    ("GROUPBY", "GROUP BY"),
    ("INNERJOIN", "INNER JOIN"),
    ("LEFTJOIN", "LEFT JOIN"),
    ("RIGHTJOIN", "RIGHT JOIN"),
    ("FULLJOIN", "FULL JOIN"),
    ("CROSSJOIN", "CROSS JOIN"),
    ("SHOWCREATETABLE", "SHOW CREATE TABLE"),
    ("DESCRIBETABLE", "DESCRIBE TABLE"),
    ("EXPLAINTABLE", "EXPLAIN TABLE"),
)
_CLICKHOUSE_SQL_SPACE_REQUIRED_KEYWORDS = (
    "SELECT",
    "FROM",
    "WHERE",
    "HAVING",
    "LIMIT",
    "OFFSET",
    "FORMAT",
    "INTO",
    "VALUES",
    "AND",
)


def _as_str(value: Any, default: str = "") -> str:
    text = str(value).strip() if value is not None else ""
    return text if text else default


def _as_list(value: Any) -> List[Any]:
    return value if isinstance(value, list) else []


def _as_bool(value: Any, default: bool = False) -> bool:
    if isinstance(value, bool):
        return value
    if value is None:
        return bool(default)
    text = str(value).strip().lower()
    if text in {"1", "true", "yes", "on"}:
        return True
    if text in {"0", "false", "no", "off"}:
        return False
    return bool(default)


def _as_int(value: Any, default: int = 0) -> int:
    try:
        return int(value)
    except Exception:
        return int(default)


def _model_to_dict(value: Any) -> Dict[str, Any]:
    """Compat helper for Pydantic v1/v2 models."""
    if isinstance(value, dict):
        return value
    if hasattr(value, "model_dump"):
        try:
            dumped = value.model_dump()
            if isinstance(dumped, dict):
                return dumped
        except Exception:
            return {}
    if hasattr(value, "dict"):
        try:
            dumped = value.dict()
            if isinstance(dumped, dict):
                return dumped
        except Exception:
            return {}
    return {}


def _should_defer_structured_spec_compile(
    reason: str,
    command_spec: Dict[str, Any],
) -> bool:
    safe_reason = _as_str(reason).strip().lower()
    if safe_reason not in _DEFERRED_STRUCTURED_SPEC_REASONS:
        return False
    return _as_str(command_spec.get("tool")).strip().lower() == "kubectl_clickhouse_query"


def _structured_answer_from_payload(payload: Dict[str, Any]) -> Optional[StructuredAnswer]:
    """Compat helper for Pydantic v1/v2 StructuredAnswer parsing."""
    if not isinstance(payload, dict):
        return None
    if hasattr(StructuredAnswer, "model_validate"):
        try:
            return StructuredAnswer.model_validate(payload)
        except Exception:
            return None
    if hasattr(StructuredAnswer, "parse_obj"):
        try:
            return StructuredAnswer.parse_obj(payload)
        except Exception:
            return None
    return None


def _collapse_unquoted_whitespace(text: str) -> str:
    chars: List[str] = []
    quote_char = ""
    escaped = False
    pending_space = False

    for char in text:
        if escaped:
            chars.append(char)
            escaped = False
            continue
        if char == "\\":
            chars.append(char)
            escaped = True
            continue
        if quote_char:
            chars.append(char)
            if char == quote_char:
                quote_char = ""
            continue
        if char in {"'", '"'}:
            if pending_space and chars:
                chars.append(" ")
            pending_space = False
            chars.append(char)
            quote_char = char
            continue
        if char.isspace():
            pending_space = True
            continue
        if pending_space and chars:
            chars.append(" ")
        pending_space = False
        chars.append(char)

    return "".join(chars).strip()


def _repair_clickhouse_query_text(query_text: str) -> str:
    repaired = _as_str(query_text)
    if not repaired:
        return ""

    for compact, expanded in _CLICKHOUSE_SQL_MULTIWORD_KEYWORD_REPAIRS:
        repaired = re.sub(rf"{compact}", f" {expanded} ", repaired)

    for keyword in _CLICKHOUSE_SQL_SPACE_REQUIRED_KEYWORDS:
        repaired = re.sub(
            rf"{keyword}(?=[A-Za-z0-9_(])",
            f" {keyword} ",
            repaired,
        )

    repaired = re.sub(r"DESC(?=(?:\s+LIMIT|\s+OFFSET|\s+FORMAT|\s*$))", " DESC ", repaired)
    repaired = re.sub(r"ASC(?=(?:\s+LIMIT|\s+OFFSET|\s+FORMAT|\s*$))", " ASC ", repaired)
    repaired = re.sub(r"(?i)(SHOW\s+CREATE\s+TABLE)([A-Za-z0-9_])", r"\1 \2", repaired)
    repaired = re.sub(r"(?i)(DESCRIBE\s+TABLE)([A-Za-z0-9_])", r"\1 \2", repaired)
    repaired = re.sub(r"(?i)(EXPLAIN\s+TABLE)([A-Za-z0-9_])", r"\1 \2", repaired)
    return _collapse_unquoted_whitespace(repaired)


def _repair_clickhouse_query_spacing(command: str) -> str:
    text = _as_str(command)
    if not text:
        return ""

    lowered = text.lower()
    if "clickhouse-client" not in lowered and " clickhouse " not in f" {lowered} ":
        return text

    def _replace_quoted(match: re.Match[str]) -> str:
        prefix = _as_str(match.group("prefix"))
        quote = _as_str(match.group("quote"))
        body = _as_str(match.group("body"))
        return f"{prefix}{quote}{_repair_clickhouse_query_text(body)}{quote}"

    def _replace_unquoted(match: re.Match[str]) -> str:
        prefix = _as_str(match.group("prefix"))
        body = _as_str(match.group("body"))
        return f"{prefix}{_repair_clickhouse_query_text(body)}"

    text = re.sub(
        r"(?is)(?P<prefix>(?:--query|-q)\s+)(?P<quote>['\"])(?P<body>.*?)(?P=quote)",
        _replace_quoted,
        text,
    )
    text = re.sub(
        r"(?is)(?P<prefix>(?:--query|-q)=)(?P<quote>['\"])(?P<body>.*?)(?P=quote)",
        _replace_quoted,
        text,
    )
    text = re.sub(
        r"(?i)(?P<prefix>(?:--query|-q)\s+)(?P<body>[^\s\"'][^\s]*)",
        _replace_unquoted,
        text,
    )
    text = re.sub(
        r"(?i)(?P<prefix>(?:--query|-q)=)(?P<body>[^\s\"'][^\s]*)",
        _replace_unquoted,
        text,
    )
    text = re.sub(
        r"(?i)(\bkubectl\b[^\n]*?\bexec\b[^\n]*?)\s+-(?:it|ti)\b(?=[^\n]*?\s+--\s+(?:clickhouse-client|clickhouse)\b)",
        r"\1 -i",
        text,
    )
    text = re.sub(
        r"(?i)(\bkubectl\b[^\n]*?\bexec\b[^\n]*?)\s+-t\b(?=[^\n]*?\s+--\s+(?:clickhouse-client|clickhouse)\b)",
        r"\1",
        text,
    )
    return text


def _normalize_action_command(raw: Any) -> str:
    command = _as_str(raw)
    if not command:
        return ""
    command = re.sub(r"^\s*(?:执行命令|命令)\s*[:：]\s*", "", command, flags=re.IGNORECASE)
    command = command.strip("`").strip()
    for head in (
        "clickhouse-client",
        "clickhouse",
        "kubectl",
        "curl",
        "rg",
        "grep",
        "cat",
        "tail",
        "head",
        "awk",
        "jq",
        "ls",
        "echo",
        "pwd",
        "sed",
        "helm",
        "systemctl",
        "service",
    ):
        command = re.sub(
            rf"(?<![A-Za-z0-9_.-])({re.escape(head)})(?=-[A-Za-z])",
            r"\1 ",
            command,
            flags=re.IGNORECASE,
        )
    # 常见断句错误修复：-it$(...) / --clickhouse -client / -nislapexec 等
    command = re.sub(
        rf"(^|[\s(])kubectl(?=(?:{_KUBECTL_VERB_PATTERN})\b)",
        r"\1kubectl ",
        command,
        flags=re.IGNORECASE,
    )
    command = re.sub(r"(^|[\s(])kubectldescribepods(?=[\s\-]|$)", r"\1kubectl describe pods", command, flags=re.IGNORECASE)
    command = re.sub(r"(^|[\s(])kubectldescribepod(?=[\s\-]|$)", r"\1kubectl describe pod", command, flags=re.IGNORECASE)
    command = re.sub(r"(\bkubectl\s+)getpods(?=[\s\-]|$)", r"\1get pods", command, flags=re.IGNORECASE)
    command = re.sub(r"(\bkubectl\s+)describepods(?=[\s\-]|$)", r"\1describe pods", command, flags=re.IGNORECASE)
    command = re.sub(r"(\bkubectl\s+)describepod(?=[\s\-]|$)", r"\1describe pod", command, flags=re.IGNORECASE)
    command = re.sub(r"(\bkubectl\s+exec)\s*-n([A-Za-z0-9._-]+)-it(?=\s|$)", r"\1 -n \2 -it", command, flags=re.IGNORECASE)
    command = re.sub(r"(^|[\s(])-n([A-Za-z0-9._-]+)(?=-[A-Za-z])", r"\1-n \2 ", command, flags=re.IGNORECASE)
    command = re.sub(r"(^|[\s(])-l([A-Za-z0-9._-]+=)", r"\1-l \2", command, flags=re.IGNORECASE)
    command = re.sub(r"(^|[\s(])-o([A-Za-z][A-Za-z0-9_.-]*=)", r"\1-o \2", command, flags=re.IGNORECASE)
    command = re.sub(
        r"(?<!\S)--namespace([a-z0-9](?:[-a-z0-9]*[a-z0-9])?)(?=\s|$)",
        r"--namespace \1",
        command,
        flags=re.IGNORECASE,
    )
    command = re.sub(
        r"(?<!\S)--selector([A-Za-z0-9._-]+=[A-Za-z0-9._:/-]+)(?=\s|$)",
        r"--selector \1",
        command,
        flags=re.IGNORECASE,
    )
    command = re.sub(r"(?<!\S)--tail(\d+)(?=\s|$)", r"--tail=\1", command, flags=re.IGNORECASE)
    command = re.sub(r"(\bkubectl\s+get\s+pods)-n", r"\1 -n ", command, flags=re.IGNORECASE)
    command = re.sub(
        r"([A-Za-z0-9._=-]+)-o(jsonpath=[^\s]+|json|yaml|wide|name)\b",
        r"\1 -o \2",
        command,
        flags=re.IGNORECASE,
    )
    command = re.sub(r"([A-Za-z0-9._=-]+)--([A-Za-z])", r"\1 --\2", command)
    command = re.sub(r"(-n\s+[A-Za-z0-9._-]+)-l([A-Za-z0-9._-]+=)", r"\1 -l \2", command, flags=re.IGNORECASE)
    command = re.sub(r"(-l\s+[A-Za-z0-9._-]+=[A-Za-z0-9._-]+)-o([A-Za-z][A-Za-z0-9_.-]*=)", r"\1 -o \2", command, flags=re.IGNORECASE)
    command = re.sub(
        r"(\bkubectl\s+(?:get|describe|logs|exec|top|rollout|apply|delete|patch|edit|replace|scale|set|annotate|label|create|expose|autoscale|cordon|uncordon|drain|taint))(?=--)",
        r"\1 ",
        command,
        flags=re.IGNORECASE,
    )
    command = re.sub(r"\)(--[A-Za-z])", r") \1", command)
    command = re.sub(r"(--[A-Za-z][\w-]*)(--[A-Za-z][\w-]*)", r"\1 \2", command)
    command = re.sub(r"(--[A-Za-z][\w-]*)(<[^>\s]+>)(?=--)", r"\1 \2 ", command)
    command = re.sub(r"(--[A-Za-z][\w-]*)(<[^>\s]+>)", r"\1 \2", command)
    command = re.sub(r"\)\s+--(clickhouse-client|clickhouse)(?=\s|$)", r") -- \1", command, flags=re.IGNORECASE)
    command = re.sub(r"(--[A-Za-z][\w-]*)(?=(['\"]))", r"\1 ", command)
    command = re.sub(r"(\bgrep\s+-[A-Za-z]\d+)(?=(['\"]))", r"\1 ", command, flags=re.IGNORECASE)
    command = re.sub(r"(-[A-Za-z]{1,4})\$\(", r"\1 $(", command)
    command = re.sub(r"([A-Za-z0-9_)}\]\"'])\$\(", r"\1 $(", command)
    command = re.sub(r"\s--([A-Za-z][\w]*)\s-([A-Za-z][\w-]*)(?=\s|$)", r" -- \1-\2", command)
    command = re.sub(r"\s--\s*(clickhouse)\s-([A-Za-z][\w-]*)(?=\s|$)", r" -- \1-\2", command, flags=re.IGNORECASE)
    command = re.sub(r"\s--(sh|bash)-c(?=\s|$)", r" -- \1 -c", command, flags=re.IGNORECASE)
    command = re.sub(r"(^|\s)-n([A-Za-z0-9._-]+)getpods(?=\s|$)", r"\1-n \2 get pods", command, flags=re.IGNORECASE)
    command = re.sub(
        r"(^|\s)-n([A-Za-z0-9._-]+)(get|describe|logs|exec|top|rollout|apply|delete|patch|edit|replace|scale|set|annotate|label|create|expose|autoscale|cordon|uncordon|drain|taint)(?=\s|$)",
        r"\1-n \2 \3",
        command,
        flags=re.IGNORECASE,
    )
    command = _repair_clickhouse_query_spacing(command)
    command = re.sub(r"(^|\s)(--[A-Za-z][\w-]*|-[A-Za-z])(?=(['\"]))", r"\1\2 ", command)
    command = re.sub(r"(?i)SHOWCREATETABLE", "SHOW CREATE TABLE", command)
    command = re.sub(r"(?i)DESCRIBETABLE", "DESCRIBE TABLE", command)
    command = re.sub(r"(?i)EXPLAINTABLE", "EXPLAIN TABLE", command)
    command = re.sub(r"(?i)(SHOW\s+CREATE\s+TABLE)([A-Za-z0-9_])", r"\1 \2", command)
    command = re.sub(r"(?i)(DESCRIBE\s+TABLE)([A-Za-z0-9_])", r"\1 \2", command)
    command = re.sub(r"(?i)(EXPLAIN\s+TABLE)([A-Za-z0-9_])", r"\1 \2", command)
    command = re.sub(r"\s*(\|\||&&|\|)\s*", r" \1 ", command)
    command = re.sub(r"\s*;\s*", " ; ", command)
    command = re.sub(r"\b(head|tail)-(\d+)\b", r"\1 -\2", command, flags=re.IGNORECASE)
    command = re.sub(
        r"\bgrep\s+-(?P<flags>[ivneEfFowx]+?)(?P<pattern>[A-Za-z0-9_./:-]+)\b",
        r"grep -\g<flags> \g<pattern>",
        command,
        flags=re.IGNORECASE,
    )
    return re.sub(r"\s+", " ", command).strip()


def _looks_like_placeholder_echo_command(command: str) -> bool:
    normalized = _normalize_action_command(command)
    if not normalized:
        return False
    lowered = normalized.lower()
    if not (lowered.startswith("echo ") or lowered.startswith("printf ")):
        return False
    if re.search(r"[\u4e00-\u9fff]", normalized):
        return True
    placeholder_phrases = (
        "please ",
        "search ",
        "check ",
        "look up ",
        "open the ",
        "go to ",
        "example:",
    )
    return any(phrase in lowered for phrase in placeholder_phrases)


def _extract_first_json_dict(text: str) -> Optional[Dict[str, Any]]:
    decoder = json.JSONDecoder()
    content = _as_str(text)
    for index, ch in enumerate(content):
        if ch not in ("{", "["):
            continue
        try:
            parsed, _ = decoder.raw_decode(content[index:])
        except json.JSONDecodeError:
            continue
        if isinstance(parsed, dict):
            return parsed
    return None


def _parse_structured_answer(content: str) -> Optional[StructuredAnswer]:
    raw = _as_str(content)
    if not raw:
        return None

    candidates: List[str] = [raw]
    fenced_blocks = re.findall(r"```(?:json)?\s*([\s\S]*?)```", raw, flags=re.IGNORECASE)
    for block in fenced_blocks:
        block_text = _as_str(block)
        if block_text:
            candidates.append(block_text)

    for candidate in candidates:
        payload: Optional[Dict[str, Any]] = None
        try:
            parsed = json.loads(candidate)
            payload = parsed if isinstance(parsed, dict) else None
        except json.JSONDecodeError:
            payload = _extract_first_json_dict(candidate)

        if not isinstance(payload, dict):
            continue
        try:
            parsed = _structured_answer_from_payload(payload)
            if parsed is not None:
                return parsed
        except Exception:
            continue

    return None


def _looks_like_json_payload(raw: str) -> bool:
    text = _as_str(raw).strip()
    if not text:
        return False
    if text.startswith("{") or text.startswith("["):
        return True
    if text.startswith("```") and ("{" in text[:120] or "[" in text[:120]):
        return True
    return False


def _sanitize_json_like_answer(raw: str) -> Dict[str, Any]:
    payload = _extract_first_json_dict(raw)
    if not isinstance(payload, dict):
        return {
            "answer": "模型返回了结构化草稿，但解析失败，已忽略原始 JSON。请补充关键上下文后重试。",
            "analysis_summary": "",
            "missing_evidence": [],
        }

    conclusion = _as_str(payload.get("conclusion")).strip()
    if not conclusion:
        for key in ("final_answer", "answer", "analysis_summary", "diagnosis", "root_cause"):
            candidate = _as_str(payload.get(key)).strip()
            if candidate:
                conclusion = candidate
                break

    summary = _as_str(payload.get("summary")).strip()
    if not summary:
        for key in ("detail", "details", "reasoning", "analysis", "next_step", "next_steps", "plan"):
            value = payload.get(key)
            if isinstance(value, list):
                candidate = "；".join(_as_str(item).strip() for item in value if _as_str(item).strip())
            else:
                candidate = _as_str(value).strip()
            if candidate:
                summary = candidate
                break

    missing_evidence = [
        _as_str(item).strip()
        for item in _as_list(payload.get("missing_evidence"))
        if _as_str(item).strip()
    ]
    if not missing_evidence:
        missing_evidence = [
            _as_str(item).strip()
            for item in _as_list(payload.get("evidence_gaps"))
            if _as_str(item).strip()
        ]

    action_titles: List[str] = []
    if isinstance(payload.get("actions"), list):
        for item in payload.get("actions") or []:
            if not isinstance(item, dict):
                continue
            action_title = _as_str(item.get("title")).strip() or _as_str(item.get("action")).strip()
            if action_title:
                action_titles.append(action_title)
            if len(action_titles) >= 3:
                break

    lines: List[str] = []
    if conclusion:
        lines.append(f"结论：{conclusion}")
    if summary and summary != conclusion:
        lines.append(f"补充说明：{summary}")
    if action_titles:
        lines.append("建议动作：")
        lines.extend(f"- {item}" for item in action_titles)
    if missing_evidence:
        lines.append("仍缺失证据：")
        lines.extend(f"- {item}" for item in missing_evidence)
    if not lines:
        keys = ",".join(sorted(str(key) for key in payload.keys())[:8])
        suffix = f"（已返回字段：{keys}）" if keys else ""
        lines.append(f"模型返回了结构化草稿，但字段不完整，已忽略原始 JSON。请补充关键上下文后重试。{suffix}")

    summary_seed = conclusion or summary or lines[0]
    return {
        "answer": "\n".join(lines).strip(),
        "analysis_summary": summary_seed[:280],
        "missing_evidence": missing_evidence,
    }


def _format_cause_line(item: Dict[str, Any]) -> str:
    title = _as_str(item.get("title"))
    if not title:
        return ""
    confidence = _as_str(item.get("confidence"), "medium")
    evidence = [sid for sid in _as_list(item.get("evidence_ids")) if _as_str(sid)]
    evidence_text = f" [{','.join(evidence)}]" if evidence else ""
    return f"- {title}（置信度:{confidence}）{evidence_text}"


def _resolve_renderable_action_command(action: Any) -> str:
    raw_command_spec = getattr(action, "command_spec", None)
    raw_command_spec = _model_to_dict(raw_command_spec)
    command_spec = normalize_followup_command_spec(raw_command_spec)
    if not command_spec:
        return ""
    compiled = compile_followup_command_spec(command_spec)
    if not bool(compiled.get("ok")):
        return ""
    return _normalize_action_command(compiled.get("command"))


def _render_structured_answer(answer: StructuredAnswer) -> str:
    lines: List[str] = []
    conclusion = _as_str(answer.conclusion)
    if conclusion:
        lines.append(f"结论：{conclusion}")

    if answer.request_flow:
        lines.append("请求流程：")
        lines.extend(f"- {item}" for item in answer.request_flow if _as_str(item))

    if answer.root_causes:
        lines.append("根因分析：")
        cause_lines = [
            _format_cause_line(_model_to_dict(item))
            for item in answer.root_causes
        ]
        lines.extend([line for line in cause_lines if line])

    if answer.actions:
        lines.append("执行步骤：")
        sorted_actions = sorted(
            answer.actions,
            key=lambda item: int(getattr(item, "priority", 1) or 1),
        )
        for action in sorted_actions:
            action_text = _as_str(getattr(action, "action", ""))
            title = _as_str(getattr(action, "title", ""))
            command = _resolve_renderable_action_command(action)
            outcome = _as_str(getattr(action, "expected_outcome", ""))
            rendered_text = action_text or title
            if command:
                rendered_text = f"{rendered_text or '执行命令'}（`{command}`）"
            if not rendered_text:
                continue
            suffix = f"（预期:{outcome}）" if outcome else ""
            lines.append(f"- P{max(1, int(getattr(action, 'priority', 1) or 1))} {rendered_text}{suffix}")

    if answer.verification:
        lines.append("验证：")
        lines.extend(f"- {item}" for item in answer.verification if _as_str(item))

    if answer.rollback:
        lines.append("回滚：")
        lines.extend(f"- {item}" for item in answer.rollback if _as_str(item))

    if answer.missing_evidence:
        lines.append("仍缺失证据：")
        lines.extend(f"- {item}" for item in answer.missing_evidence if _as_str(item))

    summary = _as_str(answer.summary)
    if summary:
        lines.append(f"补充说明：{summary}")

    return "\n".join(lines).strip()


def _extract_structured_actions(answer: StructuredAnswer) -> List[Dict[str, Any]]:
    """提取结构化动作，供上层接口回传可执行计划。"""
    if not answer.actions:
        return []
    normalized: List[Dict[str, Any]] = []
    sorted_actions = sorted(
        answer.actions,
        key=lambda item: int(getattr(item, "priority", 1) or 1),
    )
    for index, action in enumerate(sorted_actions, start=1):
        title = _as_str(getattr(action, "title", ""))
        action_text = _as_str(getattr(action, "action", ""))
        skill_name = _as_str(getattr(action, "skill_name", "")).strip()
        expected_outcome = _as_str(getattr(action, "expected_outcome", ""))
        raw_command_spec = getattr(action, "command_spec", None)
        raw_command_spec = _model_to_dict(raw_command_spec)
        command_spec = normalize_followup_command_spec(raw_command_spec)
        command = ""
        command_display = _normalize_action_command(getattr(action, "command", ""))
        command_spec_compile_reason = ""
        if skill_name and not command_spec:
            command_spec_compile_reason = ""
        elif not command_spec:
            command_spec_compile_reason = "missing_structured_spec"
        else:
            compiled = compile_followup_command_spec(command_spec)
            if bool(compiled.get("ok")):
                compiled_command_spec = (
                    compiled.get("command_spec")
                    if isinstance(compiled.get("command_spec"), dict)
                    else command_spec
                )
                command = _normalize_action_command(
                    compiled.get("command")
                    or _model_to_dict(compiled_command_spec).get("command")
                    or command_spec.get("command")
                )
                command_spec = (
                    compiled_command_spec
                )
            else:
                command_spec_compile_reason = _as_str(compiled.get("reason"), "invalid_structured_spec")
                if _should_defer_structured_spec_compile(command_spec_compile_reason, command_spec):
                    command = _normalize_action_command(
                        command_display
                        or command_spec.get("command")
                        or _model_to_dict(command_spec.get("args")).get("command")
                    )
                    command_spec_compile_reason = ""
        if not command:
            command = command_display
        if not title and action_text:
            title = action_text
        if not title and skill_name:
            title = f"执行技能: {skill_name}"
        if not title and command_display:
            title = f"执行命令: {command_display}"
        if not title and command:
            title = f"执行命令: {command}"
        if not title and not command:
            continue
        command_type = _as_str(getattr(action, "command_type", "unknown"), "unknown").lower()
        risk_level = _as_str(getattr(action, "risk_level", "high"), "high").lower()
        requires_write_permission = _as_bool(getattr(action, "requires_write_permission", False))
        requires_elevation = _as_bool(getattr(action, "requires_elevation", requires_write_permission))
        requires_confirmation = _as_bool(getattr(action, "requires_confirmation", True), True)
        has_valid_structured_spec = bool(command and command_spec and not command_spec_compile_reason)
        executable = _as_bool(getattr(action, "executable", has_valid_structured_spec), has_valid_structured_spec)
        if not has_valid_structured_spec:
            executable = False
        reason = _as_str(getattr(action, "reason", ""))
        if command_spec_compile_reason:
            reason = f"{reason}; {command_spec_compile_reason}".strip("; ").strip()
        if _looks_like_placeholder_echo_command(command_display):
            executable = False
            command_type = "unknown"
            requires_write_permission = False
            requires_elevation = False
            requires_confirmation = True
            risk_level = "low" if risk_level in {"", "low"} else risk_level
            placeholder_reason = "命令内容更像人工说明，不是可执行排查命令"
            reason = f"{reason}; {placeholder_reason}".strip("; ").strip()
        normalized.append(
            {
                "id": f"langchain-act-{index}",
                "priority": max(1, _as_int(getattr(action, "priority", 1), 1)),
                "title": title[:220],
                "action": action_text,
                "skill_name": skill_name,
                "command": command_display or command,
                "command_spec": command_spec if isinstance(command_spec, dict) else {},
                "command_type": command_type or "unknown",
                "risk_level": risk_level or "high",
                "executable": executable,
                "requires_write_permission": requires_write_permission,
                "requires_elevation": requires_elevation,
                "requires_confirmation": requires_confirmation,
                "expected_outcome": expected_outcome[:220],
                "reason": reason[:220],
            }
        )
    return normalized


def _build_format_instructions() -> str:
    if PydanticOutputParser is not None:
        try:
            parser = PydanticOutputParser(pydantic_object=StructuredAnswer)
            return parser.get_format_instructions()
        except Exception as exc:
            logger.warning("Failed to build pydantic format instructions, fallback to static schema: %s", exc)
    return json.dumps(
        {
            "conclusion": "string",
            "request_flow": ["string"],
            "root_causes": [
                {
                    "title": "string",
                    "confidence": "high|medium|low",
                    "evidence_ids": ["A1", "L2"],
                }
            ],
            "actions": [{"priority": 1, "action": "string", "expected_outcome": "string"}],
            "actions_contract_note": (
                "action 为描述；默认应输出 command_spec（结构化命令）；若命中已注册技能，可输出 skill_name 由系统自动展开。"
                "command 为 display_only 兼容字段，不参与执行。"
            ),
            "actions_schema": [
                {
                    "priority": 1,
                    "title": "执行读路径延迟排查技能",
                    "action": "优先收集 query-service 与 ClickHouse 读路径证据",
                    "skill_name": "observability_read_path_latency",
                    "expected_outcome": "自动展开为结构化读路径排查命令链",
                    "reason": "该场景已命中注册技能，优先复用技能",
                },
                {
                    "priority": 1,
                    "title": "string",
                    "action": "string",
                    "command_spec": {
                        "tool": "kubectl_clickhouse_query",
                        "args": {
                            "namespace": "islap",
                            "target_kind": "clickhouse_cluster",
                            "target_identity": "database:logs",
                            "query": "SELECT ...",
                            "timeout_s": 60,
                        },
                    },
                    "command": "display_only",
                    "command_type": "query|repair|unknown",
                    "risk_level": "low|high",
                    "executable": True,
                    "requires_write_permission": False,
                    "requires_elevation": False,
                    "requires_confirmation": True,
                    "expected_outcome": "string",
                    "reason": "string",
                },
                {
                    "priority": 1,
                    "title": "string",
                    "action": "string",
                    "command_spec": {
                        "tool": "generic_exec",
                        "args": {
                            "command": "kubectl -n islap get pods -l app=query-service --tail=50",
                            "target_kind": "k8s_cluster",
                            "target_identity": "namespace:islap",
                            "timeout_s": 60,
                        },
                    },
                    "command": "display_only",
                    "command_type": "query|repair|unknown",
                    "risk_level": "low|high",
                    "executable": True,
                    "requires_write_permission": False,
                    "requires_elevation": False,
                    "requires_confirmation": True,
                    "expected_outcome": "string",
                    "reason": "string",
                },
            ],
            "verification": ["string"],
            "rollback": ["string"],
            "missing_evidence": ["string"],
            "summary": "string",
        },
        ensure_ascii=False,
    )


def _estimate_tokens(*parts: Any) -> int:
    total_chars = 0
    for part in parts:
        if part is None:
            continue
        total_chars += len(str(part))
    return max(1, total_chars // 4)


def _should_stream_raw_tokens() -> bool:
    """
    LangChain 结构化输出默认不把原始 token 直接暴露给前端。

    模型在该模式下被要求输出 JSON，直接流式展示会让对话框出现裸 JSON，
    与最终需要呈现的“结论/动作/验证”文本不一致。若确需调试，可显式开启。
    """
    return _as_bool(os.getenv("AI_FOLLOWUP_LANGCHAIN_STREAM_RAW_TOKENS"), False)


def _build_followup_prompt_payload(
    *,
    question: str,
    analysis_context: Dict[str, Any],
    memory_summary: str,
    long_term_memory_summary: str,
    recent_history: str,
    subgoals: List[Dict[str, Any]],
    reflection: Dict[str, Any],
    tool_observations: Dict[str, Any],
    references: List[Dict[str, str]],
) -> Dict[str, Any]:
    return {
        "question": question,
        "analysis_context": analysis_context,
        "memory_summary": memory_summary,
        "long_term_memory_summary": long_term_memory_summary,
        "recent_history": recent_history,
        "subgoals_json": json.dumps(subgoals, ensure_ascii=False),
        "reflection_json": json.dumps(reflection, ensure_ascii=False),
        "tool_observations_json": json.dumps(tool_observations, ensure_ascii=False),
        "references_json": json.dumps(references, ensure_ascii=False),
        "format_instructions": _build_format_instructions(),
    }


async def run_followup_langchain(
    *,
    question: str,
    analysis_context: Dict[str, Any],
    compacted_history: List[Dict[str, Any]],
    compacted_summary: str,
    references: List[Dict[str, str]],
    subgoals: List[Dict[str, Any]],
    reflection: Dict[str, Any],
    long_term_memory: Dict[str, Any],
    llm_enabled: bool,
    llm_requested: bool,
    token_budget: int,
    token_warning: bool,
    llm_timeout_seconds: int,
    llm_service: Any,
    fallback_builder: Callable[..., str],
    llm_first_token_timeout_seconds: int = 20,
    stream_token_callback: Optional[Callable[[str], Any]] = None,
) -> Dict[str, Any]:
    """
    运行 LangChain 风格追问链路（P0）。

    返回字段：
    - answer: 文本回答
    - analysis_method: "langchain" | "rule-based"
    - llm_timeout_fallback: 是否超时降级
    """
    if not llm_enabled:
        return {
            "answer": fallback_builder(
                question,
                analysis_context,
                fallback_reason="llm_unavailable",
                reflection=reflection,
            ),
            "analysis_method": "rule-based",
            "llm_timeout_fallback": False,
            "actions": [],
            "missing_evidence": [],
            "analysis_summary": "",
        }
    if not llm_requested:
        return {
            "answer": fallback_builder(
                question,
                analysis_context,
                fallback_reason="llm_disabled_by_user",
                reflection=reflection,
            ),
            "analysis_method": "rule-based",
            "llm_timeout_fallback": False,
            "actions": [],
            "missing_evidence": [],
            "analysis_summary": "",
        }

    memory_view = build_memory_context(compacted_history, compacted_summary)
    long_term_memory_summary = _as_str(
        (long_term_memory or {}).get("summary")
        or (long_term_memory or {}).get("long_term_memory_summary")
    )
    if long_term_memory_summary:
        memory_view = build_memory_context(
            compacted_history,
            compacted_summary,
            long_term_memory_summary=long_term_memory_summary,
        )
    tool_observations = collect_tool_observations(
        question=question,
        analysis_context=analysis_context,
        references=references,
        subgoals=subgoals,
        reflection=reflection,
    )
    prompt_payload = _build_followup_prompt_payload(
        question=question,
        analysis_context=analysis_context,
        memory_summary=memory_view.get("memory_summary", ""),
        long_term_memory_summary=memory_view.get("long_term_memory_summary", ""),
        recent_history=memory_view.get("recent_history", ""),
        subgoals=subgoals,
        reflection=reflection,
        tool_observations=tool_observations,
        references=references,
    )
    estimated_tokens = _estimate_tokens(prompt_payload)
    if token_budget > 0 and estimated_tokens > token_budget:
        reduced_payload = _build_followup_prompt_payload(
            question=question,
            analysis_context=analysis_context,
            memory_summary=memory_view.get("memory_summary", "")[:400],
            long_term_memory_summary=memory_view.get("long_term_memory_summary", "")[:800],
            recent_history=memory_view.get("recent_history", "")[:900],
            subgoals=subgoals[:4],
            reflection={
                "next_actions": _as_list(reflection.get("next_actions"))[:4],
                "gaps": _as_list(reflection.get("gaps"))[:4],
                "final_confidence": reflection.get("final_confidence"),
            },
            tool_observations={
                "log_query": _as_list(tool_observations.get("log_query"))[:3],
                "reference_lookup": _as_list(tool_observations.get("reference_lookup"))[:3],
                "subgoal_gap_analyzer": tool_observations.get("subgoal_gap_analyzer"),
                "web_search": tool_observations.get("web_search"),
            },
            references=references[:4],
        )
        prompt_payload = reduced_payload
    prompt = build_followup_prompt(prompt_payload)
    message = f"{FOLLOWUP_SYSTEM_PROMPT}\n\n{prompt}"

    llm_timeout_fallback = False
    raw_token_stream_enabled = _should_stream_raw_tokens()
    try:
        result = await collect_chat_response(
            llm_service=llm_service,
            message=message,
            context={
                "engine": "langchain",
                "token_budget": token_budget,
                "token_warning": token_warning,
                "analysis_context": analysis_context,
                "conversation_history": compacted_history[-10:],
                "conversation_summary": compacted_summary,
                "long_term_memory": long_term_memory,
                "references": references,
                "subgoals": subgoals,
                "reflection": reflection,
                "tool_observations": tool_observations,
                "raw_token_stream_enabled": raw_token_stream_enabled,
            },
            total_timeout_seconds=max(5, int(llm_timeout_seconds)),
            first_token_timeout_seconds=max(1, int(llm_first_token_timeout_seconds)),
            on_token=stream_token_callback if raw_token_stream_enabled else None,
        )
        answer_text = _as_str(result)
        if not answer_text:
            raise ValueError("llm empty answer")

        structured = _parse_structured_answer(answer_text)
        if structured is None:
            if _looks_like_json_payload(answer_text):
                sanitized = _sanitize_json_like_answer(answer_text)
                safe_answer = _as_str(sanitized.get("answer")).strip() or fallback_builder(
                    question,
                    analysis_context,
                    fallback_reason="llm_structured_parse_failed",
                    reflection=reflection,
                )
                safe_missing = [
                    _as_str(item).strip()
                    for item in _as_list(sanitized.get("missing_evidence"))
                    if _as_str(item).strip()
                ]
                safe_summary = (
                    _as_str(sanitized.get("analysis_summary")).strip()
                    or (safe_answer.splitlines()[0][:280] if safe_answer else "")
                )
                return {
                    "answer": safe_answer,
                    "analysis_method": "langchain",
                    "llm_timeout_fallback": False,
                    "actions": [],
                    "missing_evidence": safe_missing,
                    "analysis_summary": safe_summary,
                }
            summary_seed = answer_text.strip().splitlines()[0][:280] if answer_text.strip() else ""
            return {
                "answer": answer_text,
                "analysis_method": "langchain",
                "llm_timeout_fallback": False,
                "actions": [],
                "missing_evidence": [],
                "analysis_summary": summary_seed,
            }
        missing_evidence = [
            _as_str(item).strip()
            for item in _as_list(structured.missing_evidence)
            if _as_str(item).strip()
        ]
        analysis_summary = (
            _as_str(structured.conclusion).strip()
            or _as_str(structured.summary).strip()
            or ""
        )
        rendered_answer = _render_structured_answer(structured).strip()
        if not rendered_answer and _looks_like_json_payload(answer_text):
            rendered_answer = _as_str(_sanitize_json_like_answer(answer_text).get("answer")).strip()
        return {
            "answer": rendered_answer or answer_text,
            "analysis_method": "langchain",
            "llm_timeout_fallback": False,
            "actions": _extract_structured_actions(structured),
            "missing_evidence": missing_evidence,
            "analysis_summary": analysis_summary[:280],
        }
    except asyncio.TimeoutError:
        llm_timeout_fallback = True
        logger.warning("LangChain follow-up timeout, fallback to rule-based")
    except Exception as e:
        timeout_like = any(word in _as_str(e).lower() for word in ["timeout", "timed out", "deadline"])
        llm_timeout_fallback = timeout_like
        logger.warning(f"LangChain follow-up failed, fallback to rule-based: {e}")

    fallback_reason = "llm_timeout" if llm_timeout_fallback else "llm_unavailable"
    return {
        "answer": fallback_builder(
            question,
            analysis_context,
            fallback_reason=fallback_reason,
            reflection=reflection,
        ),
        "analysis_method": "rule-based",
        "llm_timeout_fallback": llm_timeout_fallback,
        "actions": [],
        "missing_evidence": [],
        "analysis_summary": "",
    }
