"""
Follow-up command extraction, classification, and execution helpers.

This module keeps command security and permission policy logic out of API routes,
so `api/ai.py` can focus on request orchestration.
"""

import os
import re
import shlex
import subprocess
import time
from typing import Any, Dict, List, Optional, Tuple

from fastapi import HTTPException

_FOLLOWUP_COMMAND_FENCE_PATTERN = re.compile(r"```(?:bash|sh|shell|zsh)?\s*([\s\S]*?)```", re.IGNORECASE)
_FOLLOWUP_COMMAND_INLINE_PATTERN = re.compile(r"`([^`\n]+)`")
_FOLLOWUP_COMMAND_BLOCKED_FRAGMENTS = ("`", "\n", "\r")
_FOLLOWUP_COMMAND_TEMPLATE_PLACEHOLDER = re.compile(r"^<[A-Za-z][A-Za-z0-9_:\-]*>$")
_FOLLOWUP_COMMAND_CHAIN_OPERATORS = {
    "|",
    "|&",
    "||",
    "&&",
    ";",
}
_FOLLOWUP_COMMAND_BLOCKED_OPERATORS = {
    "&",
    ">",
    ">>",
    "<",
    "<<",
    "<<<",
    "<>",
    "<&",
    ">&",
    "&>",
    ">|",
}
_FOLLOWUP_COMMAND_UNSAFE_DETAIL = "命令包含不安全片段（禁止重定向/后台执行）"
_FOLLOWUP_COMMAND_MAX_CHARS = max(24, int(os.getenv("AI_FOLLOWUP_COMMAND_MAX_CHARS", "320")))
_FOLLOWUP_COMMAND_MAX_OUTPUT_CHARS = max(512, int(os.getenv("AI_FOLLOWUP_COMMAND_MAX_OUTPUT_CHARS", "12000")))
_FOLLOWUP_COMMAND_DEFAULT_TIMEOUT = max(3, min(120, int(os.getenv("AI_FOLLOWUP_COMMAND_TIMEOUT_SECONDS", "20"))))
_STRUCTURED_ACTION_PER_ATTEMPT_TIMEOUT_SECONDS = 60
_STRUCTURED_ACTION_MAX_ATTEMPTS = 2
_K8S_NAMESPACE_PATTERN = re.compile(r"^[a-z0-9]([-a-z0-9]*[a-z0-9])?$")
_FOLLOWUP_COMMAND_ALLOWED_HEADS = {
    "kubectl",
    "curl",
    "clickhouse-client",
    "clickhouse",
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
}
_FOLLOWUP_COMMAND_REPAIR_HEADS = tuple(
    sorted(_FOLLOWUP_COMMAND_ALLOWED_HEADS, key=len, reverse=True)
)
_KUBECTL_VERB_PATTERN = (
    "getpods|get|describe|logs|exec|top|rollout|apply|delete|patch|edit|replace|scale|set|annotate|label|"
    "create|expose|autoscale|cordon|uncordon|drain|taint"
)

_KUBECTL_FLAGS_WITH_VALUE = {
    "-n",
    "--namespace",
    "-c",
    "--container",
    "-o",
    "--output",
    "-l",
    "--selector",
    "--field-selector",
    "--context",
    "--kubeconfig",
    "--cluster",
    "--user",
    "--token",
    "--as",
    "--as-group",
    "--server",
    "--request-timeout",
    "-f",
    "--filename",
    "-k",
    "--kustomize",
}
_KUBECTL_BOOLEAN_FLAGS = {
    "-a",
    "-i",
    "-t",
    "-it",
    "-ti",
    "--all-namespaces",
    "--watch",
    "--watch-only",
    "--ignore-not-found",
    "--no-headers",
    "--show-labels",
    "--recursive",
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


def _has_curl_local_output(parts: List[str]) -> bool:
    for index, token in enumerate(parts):
        normalized_token = _as_str(token)
        normalized_lower = normalized_token.lower()
        if normalized_token in {"-o", "-D", "--output", "--dump-header", "--output-dir"}:
            if index + 1 < len(parts):
                return True
        if normalized_token.startswith("-o") and len(normalized_token) > 2:
            return True
        if normalized_token.startswith("-D") and len(normalized_token) > 2:
            return True
        if normalized_lower.startswith("--output="):
            return True
        if normalized_lower.startswith("--dump-header="):
            return True
        if normalized_lower.startswith("--output-dir="):
            return True
        if normalized_token in {"-O", "-J", "--remote-name", "--remote-name-all", "--remote-header-name"}:
            return True
    return False


def _has_curl_unsafe_config(parts: List[str]) -> bool:
    for index, token in enumerate(parts):
        normalized_token = _as_str(token)
        normalized_lower = normalized_token.lower()
        if normalized_token in {"-K", "--config"}:
            if index + 1 < len(parts):
                return True
        if normalized_token.startswith("-K") and len(normalized_token) > 2:
            return True
        if normalized_lower.startswith("--config="):
            return True
        if normalized_token == "--libcurl":
            return True
        if normalized_lower.startswith("--libcurl="):
            return True
    return False


def _contains_unresolved_template_placeholder(parts: List[str]) -> bool:
    for token in parts:
        normalized = _as_str(token).strip()
        if not normalized:
            continue
        if _FOLLOWUP_COMMAND_TEMPLATE_PLACEHOLDER.match(normalized):
            return True
    return False


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


def _is_truthy_env(name: str, default: bool = False) -> bool:
    raw = _as_str(os.getenv(name))
    if not raw:
        return default
    return raw.strip().lower() in {"1", "true", "yes", "on"}


def _followup_test_permissive_enabled() -> bool:
    return _is_truthy_env("AI_FOLLOWUP_COMMAND_TEST_PERMISSIVE", False)


def _shell_emergency_enabled() -> bool:
    return _is_truthy_env("AI_RUNTIME_SHELL_EMERGENCY_ENABLED", False)


def _collapse_unquoted_whitespace(text: str) -> str:
    """压缩引号外空白，避免改写引号内参数语义。"""
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


def _repair_followup_command_spacing(command: str) -> str:
    text = _as_str(command).strip()
    if not text:
        return ""
    for head in _FOLLOWUP_COMMAND_REPAIR_HEADS:
        text = re.sub(
            rf"(?<![A-Za-z0-9_.-])({re.escape(head)})(?=-[A-Za-z])",
            r"\1 ",
            text,
            flags=re.IGNORECASE,
        )
    # 常见错误：kubectlexec / kubectlgetpods
    text = re.sub(
        rf"(^|[\s(])kubectl(?=(?:{_KUBECTL_VERB_PATTERN})\b)",
        r"\1kubectl ",
        text,
        flags=re.IGNORECASE,
    )
    text = re.sub(r"(^|[\s(])kubectldescribepods(?=[\s\-]|$)", r"\1kubectl describe pods", text, flags=re.IGNORECASE)
    text = re.sub(r"(^|[\s(])kubectldescribepod(?=[\s\-]|$)", r"\1kubectl describe pod", text, flags=re.IGNORECASE)
    text = re.sub(r"(\bkubectl\s+)getpods(?=[\s\-]|$)", r"\1get pods", text, flags=re.IGNORECASE)
    text = re.sub(r"(\bkubectl\s+)describepods(?=[\s\-]|$)", r"\1describe pods", text, flags=re.IGNORECASE)
    text = re.sub(r"(\bkubectl\s+)describepod(?=[\s\-]|$)", r"\1describe pod", text, flags=re.IGNORECASE)
    text = re.sub(r"(\bkubectl\s+logs)-n([A-Za-z0-9._-]+)(?=\s|$)", r"\1 -n \2", text, flags=re.IGNORECASE)
    text = re.sub(r"(\bkubectl\s+logs)-([A-Za-z0-9._-]+)(?=\s|$)", r"\1 -n \2", text, flags=re.IGNORECASE)
    text = re.sub(r"(\bkubectl\s+exec)\s*-n([A-Za-z0-9._-]+)-it(?=\s|$)", r"\1 -n \2 -it", text, flags=re.IGNORECASE)
    text = re.sub(r"(^|[\s(])-n([A-Za-z0-9._-]+)(?=-[A-Za-z])", r"\1-n \2 ", text, flags=re.IGNORECASE)
    text = re.sub(
        r"(^|\s)-n\s+([A-Za-z0-9._-]+)(getpods|get|describe|logs|exec|top|rollout|apply|delete|patch|edit|replace|scale|set|annotate|label|create|expose|autoscale|cordon|uncordon|drain|taint)(?=\s|$)",
        r"\1-n \2 \3",
        text,
        flags=re.IGNORECASE,
    )
    text = re.sub(r"(^|[\s(])-l([A-Za-z0-9._-]+=)", r"\1-l \2", text, flags=re.IGNORECASE)
    text = re.sub(r"(^|[\s(])-o([A-Za-z][A-Za-z0-9_.-]*=)", r"\1-o \2", text, flags=re.IGNORECASE)
    text = re.sub(
        r"(?<!\S)--namespace([a-z0-9](?:[-a-z0-9]*[a-z0-9])?)(?=\s|$)",
        r"--namespace \1",
        text,
        flags=re.IGNORECASE,
    )
    text = re.sub(
        r"(?<!\S)--selector([A-Za-z0-9._-]+=[A-Za-z0-9._:/-]+)(?=\s|$)",
        r"--selector \1",
        text,
        flags=re.IGNORECASE,
    )
    text = re.sub(r"(?<!\S)--tail(\d+)(?=\s|$)", r"--tail=\1", text, flags=re.IGNORECASE)
    text = re.sub(
        r"\bkubectl-n([A-Za-z0-9._-]+)(getpods|get|describe|logs|exec|top|rollout|apply|delete|patch|edit|replace|scale|set|annotate|label|create|expose|autoscale|cordon|uncordon|drain|taint)(?=[\s-]|$)",
        r"kubectl -n \1 \2",
        text,
        flags=re.IGNORECASE,
    )
    text = re.sub(
        r"(\bkubectl\s+-n\s+[A-Za-z0-9._-]+\s+)getpods(?=[\s-]|$)",
        r"\1get pods",
        text,
        flags=re.IGNORECASE,
    )
    text = re.sub(r"(\bkubectl\s+get\s+pods)-n", r"\1 -n ", text, flags=re.IGNORECASE)
    text = re.sub(r"(?<=\w)-l([A-Za-z0-9._-]+=)", r" -l \1", text, flags=re.IGNORECASE)
    text = re.sub(r"(?<=\w)-o([A-Za-z][A-Za-z0-9_.-]*=)", r" -o \1", text, flags=re.IGNORECASE)
    text = re.sub(
        r"([A-Za-z0-9._=-]+)-o(jsonpath=[^\s]+|json|yaml|wide|name)\b",
        r"\1 -o \2",
        text,
        flags=re.IGNORECASE,
    )
    text = re.sub(r"([A-Za-z0-9._=-]+)--([A-Za-z])", r"\1 --\2", text)
    text = re.sub(r"(-n\s+[A-Za-z0-9._-]+)-l([A-Za-z0-9._-]+=)", r"\1 -l \2", text, flags=re.IGNORECASE)
    text = re.sub(r"(-l\s+[A-Za-z0-9._-]+=[A-Za-z0-9._-]+)-o([A-Za-z][A-Za-z0-9_.-]*=)", r"\1 -o \2", text, flags=re.IGNORECASE)
    text = re.sub(
        r"(\bkubectl\s+(?:get|describe|logs|exec|top|rollout|apply|delete|patch|edit|replace|scale|set|annotate|label|create|expose|autoscale|cordon|uncordon|drain|taint))(?=--)",
        r"\1 ",
        text,
        flags=re.IGNORECASE,
    )
    text = re.sub(r"\)(--[A-Za-z])", r") \1", text)
    text = re.sub(r"(--[A-Za-z][\w-]*)(--[A-Za-z][\w-]*)", r"\1 \2", text)
    # 常见错误：--host<HOST>--port<PORT> -> --host <HOST> --port <PORT>
    text = re.sub(r"(--[A-Za-z][\w-]*)(<[^>\s]+>)(?=--)", r"\1 \2 ", text)
    text = re.sub(r"(--[A-Za-z][\w-]*)(<[^>\s]+>)", r"\1 \2", text)
    text = re.sub(r"\)\s+--(clickhouse-client|clickhouse)(?=\s|$)", r") -- \1", text, flags=re.IGNORECASE)
    text = re.sub(r"(--[A-Za-z][\w-]*)(?=(['\"]))", r"\1 ", text)
    text = re.sub(r"(\bgrep\s+-[A-Za-z]\d+)(?=(['\"]))", r"\1 ", text, flags=re.IGNORECASE)
    # 常见错误：-it$(kubectl ...) -> -it $(kubectl ...)
    text = re.sub(r"(-[A-Za-z]{1,4})\$\(", r"\1 $(", text)
    text = re.sub(r"([A-Za-z0-9_)}\]\"'])\$\(", r"\1 $(", text)
    # 常见错误：--clickhouse -client -> -- clickhouse-client
    text = re.sub(r"\s--([A-Za-z][\w]*)\s-([A-Za-z][\w-]*)(?=\s|$)", r" -- \1-\2", text)
    text = re.sub(r"\s--\s*(clickhouse)\s-([A-Za-z][\w-]*)(?=\s|$)", r" -- \1-\2", text, flags=re.IGNORECASE)
    text = re.sub(r"\s--(sh|bash)-c(?=\s|$)", r" -- \1 -c", text, flags=re.IGNORECASE)
    # 常见错误：-nislapexec / -nislapgetpods
    text = re.sub(r"(^|\s)-n([A-Za-z0-9._-]+)getpods(?=\s|$)", r"\1-n \2 get pods", text, flags=re.IGNORECASE)
    text = re.sub(
        r"(^|\s)-n([A-Za-z0-9._-]+)(getpods|get|describe|logs|exec|top|rollout|apply|delete|patch|edit|replace|scale|set|annotate|label|create|expose|autoscale|cordon|uncordon|drain|taint)(?=\s|$)",
        r"\1-n \2 \3",
        text,
        flags=re.IGNORECASE,
    )
    text = _repair_clickhouse_query_spacing(text)
    text = re.sub(r"(^|\s)(--[A-Za-z][\w-]*|-[A-Za-z])(?=(['\"]))", r"\1\2 ", text)
    text = re.sub(r"(?i)SHOWCREATETABLE", "SHOW CREATE TABLE", text)
    text = re.sub(r"(?i)DESCRIBETABLE", "DESCRIBE TABLE", text)
    text = re.sub(r"(?i)EXPLAINTABLE", "EXPLAIN TABLE", text)
    text = re.sub(r"(?i)(SHOW\s+CREATE\s+TABLE)([A-Za-z0-9_])", r"\1 \2", text)
    text = re.sub(r"(?i)(DESCRIBE\s+TABLE)([A-Za-z0-9_])", r"\1 \2", text)
    text = re.sub(r"(?i)(EXPLAIN\s+TABLE)([A-Za-z0-9_])", r"\1 \2", text)
    text = re.sub(r"\s*(\|\||&&|\|)\s*", r" \1 ", text)
    text = re.sub(r"\s*;\s*", " ; ", text)
    text = re.sub(r"\b(head|tail)-(\d+)\b", r"\1 -\2", text, flags=re.IGNORECASE)
    text = re.sub(
        r"\bgrep\s+-(?P<flags>[ivneEfFowx]+?)(?P<pattern>[A-Za-z0-9_./:-]+)\b",
        r"grep -\g<flags> \g<pattern>",
        text,
        flags=re.IGNORECASE,
    )
    return _collapse_unquoted_whitespace(text)


def _normalize_followup_command_line(line: str) -> str:
    normalized = _as_str(line).strip()
    if normalized.startswith("`") and normalized.endswith("`") and len(normalized) > 2:
        normalized = normalized[1:-1].strip()
    normalized = re.sub(r"^\s*(?:[-*•]\s+|\d+\.\s+)", "", normalized)
    normalized = re.sub(r"^\s*P\d+\s+", "", normalized, flags=re.IGNORECASE)
    normalized = re.sub(r"^\s*(?:执行命令|命令)\s*[:：]\s*", "", normalized)
    if normalized.startswith("$"):
        normalized = normalized[1:].strip()
    first_token = normalized.split(maxsplit=1)[0].lower() if normalized else ""
    if first_token not in _FOLLOWUP_COMMAND_ALLOWED_HEADS:
        for head in _FOLLOWUP_COMMAND_REPAIR_HEADS:
            pattern = rf"^({re.escape(head)})(?=(?:--|-)[A-Za-z])"
            if re.search(pattern, normalized, flags=re.IGNORECASE):
                normalized = re.sub(
                    pattern,
                    r"\1 ",
                    normalized,
                    count=1,
                    flags=re.IGNORECASE,
                )
                break
    normalized = re.sub(r"(^|\s)(--[A-Za-z][\w-]*|-[A-Za-z])(?=(['\"]))", r"\1\2 ", normalized)
    return _repair_followup_command_spacing(normalized)


def _looks_like_command(text: str) -> bool:
    candidate = _normalize_followup_command_line(text)
    if not candidate:
        return False
    head = _as_str(candidate.split(" ", 1)[0]).lower()
    if head not in _FOLLOWUP_COMMAND_ALLOWED_HEADS:
        return False
    return bool(re.match(r"^[A-Za-z0-9_.\-]+(?:\s+.+)?$", candidate))


def _normalize_followup_command_match_key(command: str) -> str:
    normalized = _normalize_followup_command_line(command)
    if not normalized:
        return ""
    try:
        tokens = shlex.split(normalized)
    except Exception:
        return re.sub(r"\s+", " ", normalized).strip()
    if not tokens:
        return ""
    return "\x1f".join(tokens)


def _normalize_followup_command_loose_match_key(command: str) -> str:
    """生成受控宽松匹配 key，用于识别引号与链式分隔符的等价写法。"""
    normalized = _normalize_followup_command_line(command)
    if not normalized:
        return ""
    try:
        lexer = shlex.shlex(normalized, posix=True, punctuation_chars="|&;<>")
        lexer.whitespace_split = True
        lexer.commenters = ""
        raw_tokens = [_as_str(token).strip() for token in lexer]
    except Exception:
        return ""

    tokens: List[str] = []
    operator_tokens = _FOLLOWUP_COMMAND_CHAIN_OPERATORS.union(_FOLLOWUP_COMMAND_BLOCKED_OPERATORS)
    for token in raw_tokens:
        if not token:
            continue
        if token in operator_tokens:
            tokens.append(token)
            continue
        normalized_token = re.sub(r"\s*(\|\||&&|\|)\s*", r"\1", token)
        tokens.append(normalized_token)

    if not tokens:
        return ""

    merged: List[str] = []
    index = 0
    while index < len(tokens):
        if (
            index + 2 < len(tokens)
            and tokens[index] not in operator_tokens
            and tokens[index + 1] in _FOLLOWUP_COMMAND_CHAIN_OPERATORS
            and tokens[index + 2] not in operator_tokens
        ):
            merged.append(f"{tokens[index]}{tokens[index + 1]}{tokens[index + 2]}")
            index += 3
            continue
        merged.append(tokens[index])
        index += 1
    return "\x1f".join(merged)


def _extract_commands_from_message_content(content: str, limit: int = 12) -> List[str]:
    text = _as_str(content)
    if not text:
        return []

    commands: List[str] = []
    seen: set[str] = set()

    def _append(candidate: str) -> None:
        normalized = _normalize_followup_command_line(candidate)
        if not normalized or normalized.startswith("#"):
            return
        if not _looks_like_command(normalized):
            return
        if normalized in seen:
            return
        seen.add(normalized)
        commands.append(normalized)

    for block in _FOLLOWUP_COMMAND_FENCE_PATTERN.findall(text):
        for line in _as_str(block).splitlines():
            _append(line)
            if len(commands) >= limit:
                return commands

    for inline in _FOLLOWUP_COMMAND_INLINE_PATTERN.findall(text):
        _append(inline)
        if len(commands) >= limit:
            return commands

    for line in text.splitlines():
        stripped_line = line.strip()
        if stripped_line.startswith("$") or _looks_like_command(stripped_line):
            _append(line)
            if len(commands) >= limit:
                return commands

    return commands


def _extract_commands_from_actions_metadata(actions: Any, limit: int = 12) -> List[str]:
    commands: List[str] = []
    seen: set[str] = set()
    for item in _as_list(actions):
        if len(commands) >= limit:
            break
        payload = item if isinstance(item, dict) else {}
        command = _normalize_followup_command_line(payload.get("command"))
        if not command or not _looks_like_command(command):
            continue
        command_key = _normalize_followup_command_match_key(command)
        if not command_key or command_key in seen:
            continue
        seen.add(command_key)
        commands.append(command)
    return commands


def _is_sed_inplace_token(token: str) -> bool:
    normalized = _as_str(token).strip().lower()
    if not normalized:
        return False
    if normalized == "-i" or normalized.startswith("-i"):
        return True
    if normalized == "--in-place" or normalized.startswith("--in-place="):
        return True
    if normalized.startswith("-") and not normalized.startswith("--") and "i" in normalized[1:]:
        return True
    return False


def _consume_kubectl_flag(parts: List[str], index: int) -> int:
    token = _as_str(parts[index]).strip().lower()
    if not token.startswith("-"):
        return index
    if token.startswith("--"):
        if "=" in token:
            return index + 1
        if token in _KUBECTL_BOOLEAN_FLAGS:
            return index + 1
        return min(len(parts), index + 2)
    if token in _KUBECTL_BOOLEAN_FLAGS:
        return index + 1
    if token in _KUBECTL_FLAGS_WITH_VALUE:
        return min(len(parts), index + 2)
    if len(token) > 2 and token[:2] in _KUBECTL_FLAGS_WITH_VALUE:
        return index + 1
    return index + 1


def _extract_kubectl_verbs(parts: List[str]) -> tuple[str, str]:
    cursor = 1
    verb = ""
    while cursor < len(parts):
        token = _as_str(parts[cursor]).strip().lower()
        if not token:
            cursor += 1
            continue
        if token.startswith("-"):
            next_cursor = _consume_kubectl_flag(parts, cursor)
            cursor = next_cursor if next_cursor > cursor else cursor + 1
            continue
        verb = token
        cursor += 1
        break

    sub_verb = ""
    while cursor < len(parts):
        token = _as_str(parts[cursor]).strip().lower()
        if not token:
            cursor += 1
            continue
        if token.startswith("-"):
            next_cursor = _consume_kubectl_flag(parts, cursor)
            cursor = next_cursor if next_cursor > cursor else cursor + 1
            continue
        sub_verb = token
        break
    return verb, sub_verb


def _extract_kubectl_exec_command(parts: List[str]) -> List[str]:
    for index, token in enumerate(parts):
        if _as_str(token).strip() == "--":
            return [_as_str(item) for item in parts[index + 1 :] if _as_str(item).strip()]
    return []


def _extract_clickhouse_query(parts: List[str]) -> str:
    for index in range(1, len(parts)):
        token = _as_str(parts[index]).strip()
        lowered = token.lower()
        if lowered in {"--query", "-q"}:
            if index + 1 < len(parts):
                return " ".join(_as_str(item) for item in parts[index + 1 :]).strip()
            return ""
        if lowered.startswith("--query="):
            return token.split("=", 1)[1].strip()
        if lowered.startswith("-q=") and len(token) > 3:
            return token[3:].strip()
        if lowered.startswith("-q") and len(token) > 2 and lowered != "-query":
            return token[2:].strip()
    return ""


def _classify_followup_command(parts: List[str]) -> Dict[str, Any]:
    if not parts:
        return {
            "command_type": "unknown",
            "risk_level": "high",
            "requires_write_permission": False,
            "supported": False,
            "reason": "命令为空",
        }

    if _contains_unresolved_template_placeholder(parts):
        return {
            "command_type": "unknown",
            "risk_level": "high",
            "requires_write_permission": False,
            "supported": False,
            "reason": "命令包含占位符参数，需先补全具体值后再执行",
        }

    head = _as_str(parts[0]).lower()
    if head == "kubectl":
        verb, sub_verb = _extract_kubectl_verbs(parts)
        readonly = {
            "get",
            "describe",
            "logs",
            "top",
            "events",
            "wait",
            "version",
            "cluster-info",
            "explain",
            "api-resources",
            "api-versions",
        }
        mutating = {
            "apply",
            "delete",
            "patch",
            "edit",
            "replace",
            "scale",
            "set",
            "annotate",
            "label",
            "create",
            "expose",
            "autoscale",
            "cordon",
            "uncordon",
            "drain",
            "taint",
        }
        if verb == "rollout":
            if sub_verb in {"status", "history"}:
                return {
                    "command_type": "query",
                    "risk_level": "low",
                    "requires_write_permission": False,
                    "supported": True,
                    "reason": "Kubernetes rollout 只读查询命令",
                }
            if sub_verb in {"restart", "undo", "pause", "resume"}:
                return {
                    "command_type": "repair",
                    "risk_level": "high",
                    "requires_write_permission": True,
                    "supported": True,
                    "reason": "Kubernetes rollout 变更命令，可能影响线上环境",
                }
            return {
                "command_type": "unknown",
                "risk_level": "high",
                "requires_write_permission": False,
                "supported": False,
                "reason": "暂不支持的 kubectl rollout 子命令",
            }
        if verb in readonly:
            return {
                "command_type": "query",
                "risk_level": "low",
                "requires_write_permission": False,
                "supported": True,
                "reason": "Kubernetes 只读查询命令",
            }
        if verb in mutating:
            return {
                "command_type": "repair",
                "risk_level": "high",
                "requires_write_permission": True,
                "supported": True,
                "reason": "Kubernetes 变更命令，可能影响线上环境",
            }
        if verb == "exec":
            remote_parts = _extract_kubectl_exec_command(parts)
            if not remote_parts:
                return {
                    "command_type": "unknown",
                    "risk_level": "high",
                    "requires_write_permission": False,
                    "supported": False,
                    "reason": "kubectl exec 缺少远端命令（需使用 -- <command>）",
                }
            remote_meta = _classify_followup_command(remote_parts)
            if not bool(remote_meta.get("supported")):
                return {
                    "command_type": "unknown",
                    "risk_level": "high",
                    "requires_write_permission": False,
                    "supported": False,
                    "reason": f"kubectl exec 远端命令不受支持: {_as_str(remote_meta.get('reason'))}",
                }
            if bool(remote_meta.get("requires_write_permission")):
                return {
                    "command_type": "repair",
                    "risk_level": "high",
                    "requires_write_permission": True,
                    "supported": True,
                    "reason": f"kubectl exec 远端命令可能写入: {_as_str(remote_meta.get('reason'))}",
                }
            return {
                "command_type": "query",
                "risk_level": "low",
                "requires_write_permission": False,
                "supported": True,
                "reason": f"kubectl exec 远端只读命令: {_as_str(remote_meta.get('reason'))}",
            }
        return {
            "command_type": "unknown",
            "risk_level": "high",
            "requires_write_permission": False,
            "supported": False,
            "reason": "暂不支持的 kubectl 子命令或参数格式",
        }

    if head == "curl":
        if _has_curl_unsafe_config(parts):
            return {
                "command_type": "repair",
                "risk_level": "high",
                "requires_write_permission": True,
                "supported": True,
                "reason": "curl 使用配置文件/导出参数，存在高风险副作用",
            }
        if _has_curl_local_output(parts):
            return {
                "command_type": "repair",
                "risk_level": "high",
                "requires_write_permission": True,
                "supported": True,
                "reason": "curl 包含本地文件输出参数，可能修改本地文件",
            }
        method = "GET"
        has_body_payload = False
        has_get_query_flag = False
        for index, token in enumerate(parts):
            normalized_token = _as_str(token)
            normalized_lower = normalized_token.lower()
            if normalized_token == "-X" and index + 1 < len(parts):
                method = _as_str(parts[index + 1]).upper()
            elif normalized_token.startswith("-X") and len(normalized_token) > 2:
                method = _as_str(normalized_token[2:]).upper()
            elif normalized_lower == "--request" and index + 1 < len(parts):
                method = _as_str(parts[index + 1]).upper()
            elif normalized_lower.startswith("--request="):
                method = _as_str(normalized_token.split("=", 1)[1]).upper()
            elif normalized_token == "-G" or normalized_lower == "--get":
                has_get_query_flag = True
            elif (
                normalized_token == "-F"
                or normalized_token.startswith("-F")
                or normalized_lower
                in {
                    "-d",
                    "--data",
                    "--data-raw",
                    "--data-binary",
                    "--data-urlencode",
                    "--form",
                    "--json",
                    "-t",
                    "--upload-file",
                }
                or normalized_token.startswith("-d")
                or normalized_token.startswith("-T")
                or normalized_lower.startswith("--data=")
                or normalized_lower.startswith("--data-raw=")
                or normalized_lower.startswith("--data-binary=")
                or normalized_lower.startswith("--data-urlencode=")
                or normalized_lower.startswith("--form=")
                or normalized_lower.startswith("--json=")
                or normalized_lower.startswith("--upload-file=")
            ):
                has_body_payload = True
        if has_body_payload and method in {"GET", "HEAD"} and not has_get_query_flag:
            method = "POST"
        if method in {"GET", "HEAD"} and (not has_body_payload or has_get_query_flag):
            return {
                "command_type": "query",
                "risk_level": "low",
                "requires_write_permission": False,
                "supported": True,
                "reason": "HTTP 只读查询命令",
            }
        return {
            "command_type": "repair",
            "risk_level": "high",
            "requires_write_permission": True,
            "supported": True,
            "reason": f"HTTP {method} 请求可能触发写操作",
        }

    if head in {"clickhouse-client", "clickhouse"}:
        query_text = _extract_clickhouse_query(parts)
        if not query_text:
            return {
                "command_type": "unknown",
                "risk_level": "high",
                "requires_write_permission": False,
                "supported": False,
                "reason": "ClickHouse 命令缺少 --query/-q，无法判定语义",
            }
        compact_query = re.sub(r"\s+", "", re.sub(r"[\"'`]", "", query_text)).lower()
        readonly_prefixes = ("select", "show", "describe", "desc", "explain", "with")
        mutating_prefixes = (
            "insert",
            "alter",
            "create",
            "drop",
            "truncate",
            "optimize",
            "system",
            "delete",
            "update",
            "rename",
            "grant",
            "revoke",
        )
        if compact_query.startswith(readonly_prefixes):
            return {
                "command_type": "query",
                "risk_level": "low",
                "requires_write_permission": False,
                "supported": True,
                "reason": "ClickHouse 只读查询命令",
            }
        if compact_query.startswith(mutating_prefixes):
            return {
                "command_type": "repair",
                "risk_level": "high",
                "requires_write_permission": True,
                "supported": True,
                "reason": "ClickHouse 变更命令，可能修改数据或结构",
            }
        return {
            "command_type": "unknown",
            "risk_level": "high",
            "requires_write_permission": False,
            "supported": False,
            "reason": "ClickHouse 查询语义不明确，需人工确认",
        }

    if head in {"rg", "grep", "cat", "tail", "head", "jq", "ls", "echo", "pwd"}:
        return {
            "command_type": "query",
            "risk_level": "low",
            "requires_write_permission": False,
            "supported": True,
            "reason": "本地只读排查命令",
        }

    if head == "awk":
        return {
            "command_type": "repair",
            "risk_level": "high",
            "requires_write_permission": True,
            "supported": True,
            "reason": "awk 脚本可执行系统命令或写文件，按高风险处理",
        }

    if head == "sed":
        return {
            "command_type": "repair",
            "risk_level": "high",
            "requires_write_permission": True,
            "supported": True,
            "reason": "sed 可能通过脚本语义写文件，按高风险处理",
        }

    if head in {"helm", "systemctl", "service"}:
        return {
            "command_type": "repair",
            "risk_level": "high",
            "requires_write_permission": True,
            "supported": True,
            "reason": "该命令可能触发配置或运行状态变更",
        }

    return {
        "command_type": "unknown",
        "risk_level": "high",
        "requires_write_permission": False,
        "supported": False,
        "reason": "命令类型未在安全白名单中",
    }


def _resolve_followup_write_enabled() -> bool:
    """兼容新旧开关，统一解析是否允许执行写命令。"""
    return _is_truthy_env(
        "AI_FOLLOWUP_COMMAND_WRITE_ENABLED",
        _is_truthy_env("AI_FOLLOWUP_COMMAND_ALLOW_WRITE", False),
    )


def _build_command_confirmation_message(
    *,
    command: str,
    command_type: str,
    risk_level: str,
    reason: str,
    requires_write_permission: bool,
) -> str:
    kind_label = "查询命令" if command_type == "query" else "修复命令" if command_type == "repair" else "未知命令"
    risk_label = "高风险" if risk_level == "high" else "低风险"
    permission_line = "该命令可能修改系统资源，需要写权限。" if requires_write_permission else "该命令为只读查询，不会主动修改资源。"
    return (
        f"即将执行 {kind_label}（{risk_label}）：{reason}\n"
        f"{permission_line}\n"
        f"命令：{command}\n"
        "请确认是否继续执行。"
    )


def _build_followup_exec_disabled_response(
    session_id: str,
    message_id: str,
    command: str,
) -> Dict[str, Any]:
    return {
        "status": "permission_required",
        "session_id": session_id,
        "message_id": message_id,
        "command": _as_str(command).strip(),
        "message": "命令执行能力已关闭，请联系管理员开启 AI_FOLLOWUP_COMMAND_EXEC_ENABLED。",
        "requires_confirmation": False,
        "requires_write_permission": False,
    }


def _validate_requested_followup_command(raw_command: str) -> None:
    if not raw_command:
        raise HTTPException(status_code=400, detail="command is empty")
    if len(raw_command) > _FOLLOWUP_COMMAND_MAX_CHARS:
        raise HTTPException(status_code=400, detail=f"command exceeds max chars({_FOLLOWUP_COMMAND_MAX_CHARS})")
    if _followup_test_permissive_enabled():
        return
    if any(fragment in raw_command for fragment in _FOLLOWUP_COMMAND_BLOCKED_FRAGMENTS) or _contains_blocked_shell_operator(raw_command):
        raise HTTPException(status_code=400, detail=_FOLLOWUP_COMMAND_UNSAFE_DETAIL)


def _assert_followup_command_is_suggested(
    raw_command: str,
    message_content: str,
    message_metadata: Dict[str, Any],
) -> None:
    suggested_commands = _extract_commands_from_message_content(message_content)
    suggested_commands.extend(
        _extract_commands_from_actions_metadata(
            message_metadata.get("actions"),
            limit=12,
        )
    )
    requested_match_key = _normalize_followup_command_match_key(raw_command)
    requested_loose_match_key = _normalize_followup_command_loose_match_key(raw_command)

    normalized_suggestions: set[str] = set()
    loose_suggestions: set[str] = set()
    for item in suggested_commands:
        strict_key = _normalize_followup_command_match_key(item)
        if strict_key:
            normalized_suggestions.add(strict_key)
        loose_key = _normalize_followup_command_loose_match_key(item)
        if loose_key:
            loose_suggestions.add(loose_key)

    if requested_match_key in normalized_suggestions:
        return
    if requested_loose_match_key and requested_loose_match_key in loose_suggestions:
        return
    if requested_match_key in loose_suggestions:
        return
    if requested_loose_match_key and requested_loose_match_key in normalized_suggestions:
        return
    if requested_match_key not in normalized_suggestions:
        raise HTTPException(status_code=400, detail="command is not present in assistant message suggestions")


def _parse_followup_command_segments(raw_command: str) -> Dict[str, Any]:
    permissive = _followup_test_permissive_enabled()
    try:
        lexer = shlex.shlex(raw_command, posix=True, punctuation_chars="|&;<>")
        lexer.whitespace_split = True
        lexer.commenters = ""
        segments: List[List[str]] = []
        current: List[str] = []
        operators: List[str] = []
        for token in lexer:
            normalized = _as_str(token).strip()
            if not normalized:
                continue
            if normalized in _FOLLOWUP_COMMAND_CHAIN_OPERATORS:
                if not current:
                    return {
                        "ok": False,
                        "reason": "empty segment around chain operator",
                        "blocked_operator": normalized,
                    }
                segments.append(current)
                operators.append(normalized)
                current = []
                continue
            if normalized in _FOLLOWUP_COMMAND_BLOCKED_OPERATORS:
                if not permissive:
                    return {
                        "ok": False,
                        "reason": "blocked operator",
                        "blocked_operator": normalized,
                    }
            current.append(normalized)
        if not current:
            if operators:
                return {
                    "ok": False,
                    "reason": "empty trailing segment",
                    "blocked_operator": operators[-1],
                }
            return {
                "ok": False,
                "reason": "empty command",
                "blocked_operator": "",
            }
        segments.append(current)
        return {
            "ok": True,
            "segments": segments,
            "operators": operators,
        }
    except Exception:
        return {
            "ok": False,
            "reason": "command parse failed",
            "blocked_operator": "",
        }


def _merge_followup_segment_meta(
    segment_metas: List[Dict[str, Any]],
    operators: List[str],
) -> Dict[str, Any]:
    for index, segment_meta in enumerate(segment_metas):
        if bool(segment_meta.get("supported")):
            continue
        return {
            "supported": False,
            "command_type": _as_str(segment_meta.get("command_type"), "unknown"),
            "risk_level": _as_str(segment_meta.get("risk_level"), "high"),
            "requires_write_permission": bool(segment_meta.get("requires_write_permission")),
            "reason": f"第 {index + 1} 段命令不受支持: {_as_str(segment_meta.get('reason'), '命令类型未在安全白名单中')}",
        }

    requires_write = any(bool(item.get("requires_write_permission")) for item in segment_metas)
    command_type = "repair" if requires_write else "query"
    risk_level = "high" if requires_write else "low"
    operator_text = " ".join(operators)
    return {
        "supported": True,
        "command_type": command_type,
        "risk_level": risk_level,
        "requires_write_permission": requires_write,
        "reason": (
            f"链式命令（{len(segment_metas)} 段，操作符: {operator_text}）"
            if operator_text
            else "单段命令"
        ),
    }


def _resolve_followup_command_meta(raw_command: str) -> Tuple[Dict[str, Any], str]:
    parsed = _parse_followup_command_segments(raw_command)
    if not bool(parsed.get("ok")):
        raise HTTPException(status_code=400, detail=_FOLLOWUP_COMMAND_UNSAFE_DETAIL)

    segments = parsed.get("segments") if isinstance(parsed.get("segments"), list) else []
    operators = parsed.get("operators") if isinstance(parsed.get("operators"), list) else []
    segment_metas: List[Dict[str, Any]] = []
    for segment_tokens in segments:
        if not isinstance(segment_tokens, list) or not segment_tokens:
            raise HTTPException(status_code=400, detail="command parse failed: empty segment")
        command_meta = _classify_followup_command([_as_str(token) for token in segment_tokens])
        segment_metas.append(command_meta)

    if len(segment_metas) == 1:
        command_meta = segment_metas[0]
    else:
        command_meta = _merge_followup_segment_meta(segment_metas, [_as_str(item) for item in operators])
    if _followup_test_permissive_enabled() and not bool(command_meta.get("supported")):
        command_meta = {
            **command_meta,
            "supported": True,
            "command_type": "repair",
            "risk_level": "high",
            "requires_write_permission": True,
            "reason": (
                f"{_as_str(command_meta.get('reason'), 'unsupported command')}; "
                "test permissive fallback requires elevation approval"
            ),
        }
    confirmation_message = _build_command_confirmation_message(
        command=raw_command,
        command_type=_as_str(command_meta.get("command_type"), "unknown"),
        risk_level=_as_str(command_meta.get("risk_level"), "high"),
        reason=_as_str(command_meta.get("reason"), "请人工复核命令风险"),
        requires_write_permission=bool(command_meta.get("requires_write_permission")),
    )
    return command_meta, confirmation_message


def _build_followup_unsupported_command_response(
    *,
    session_id: str,
    message_id: str,
    raw_command: str,
    command_meta: Dict[str, Any],
) -> Dict[str, Any]:
    return {
        "status": "permission_required",
        "session_id": session_id,
        "message_id": message_id,
        "command": raw_command,
        "command_type": _as_str(command_meta.get("command_type"), "unknown"),
        "risk_level": _as_str(command_meta.get("risk_level"), "high"),
        "requires_confirmation": False,
        "requires_write_permission": bool(command_meta.get("requires_write_permission")),
        "message": _as_str(command_meta.get("reason"), "命令类型不在允许列表中"),
    }


def _build_followup_write_disabled_response(
    *,
    session_id: str,
    message_id: str,
    raw_command: str,
    command_meta: Dict[str, Any],
    confirmation_message: str,
) -> Dict[str, Any]:
    return {
        "status": "permission_required",
        "session_id": session_id,
        "message_id": message_id,
        "command": raw_command,
        "command_type": _as_str(command_meta.get("command_type"), "repair"),
        "risk_level": _as_str(command_meta.get("risk_level"), "high"),
        "requires_confirmation": False,
        "requires_write_permission": True,
        "message": "写命令开关未开启，仅支持查询操作。",
        "confirmation_message": confirmation_message,
    }


def _build_followup_elevation_required_response(
    *,
    session_id: str,
    message_id: str,
    raw_command: str,
    command_meta: Dict[str, Any],
    confirmation_message: str,
) -> Dict[str, Any]:
    return {
        "status": "elevation_required",
        "session_id": session_id,
        "message_id": message_id,
        "command": raw_command,
        "command_type": _as_str(command_meta.get("command_type"), "repair"),
        "risk_level": _as_str(command_meta.get("risk_level"), "high"),
        "requires_confirmation": True,
        "requires_write_permission": True,
        "requires_elevation": True,
        "message": "该命令属于写操作（含重启/删除），需提权确认后执行。",
        "confirmation_message": confirmation_message,
    }


def _build_followup_confirmation_required_response(
    *,
    session_id: str,
    message_id: str,
    raw_command: str,
    command_meta: Dict[str, Any],
    confirmation_message: str,
) -> Dict[str, Any]:
    return {
        "status": "confirmation_required",
        "session_id": session_id,
        "message_id": message_id,
        "command": raw_command,
        "command_type": _as_str(command_meta.get("command_type"), "unknown"),
        "risk_level": _as_str(command_meta.get("risk_level"), "high"),
        "requires_confirmation": True,
        "requires_write_permission": bool(command_meta.get("requires_write_permission")),
        "confirmation_message": confirmation_message,
    }


def _build_followup_command_execution_response(
    *,
    session_id: str,
    message_id: str,
    raw_command: str,
    command_meta: Dict[str, Any],
    confirmation_message: str,
    execution_result: Dict[str, Any],
) -> Dict[str, Any]:
    return {
        "status": execution_result.get("status"),
        "session_id": session_id,
        "message_id": message_id,
        "command": raw_command,
        "command_type": _as_str(command_meta.get("command_type"), "unknown"),
        "risk_level": _as_str(command_meta.get("risk_level"), "high"),
        "requires_confirmation": True,
        "requires_write_permission": bool(command_meta.get("requires_write_permission")),
        "confirmation_message": confirmation_message,
        "exit_code": int(execution_result.get("exit_code") or 0),
        "duration_ms": int(execution_result.get("duration_ms") or 0),
        "stdout": _as_str(execution_result.get("stdout")),
        "stderr": _as_str(execution_result.get("stderr")),
        "output_truncated": bool(execution_result.get("output_truncated")),
        "timed_out": bool(execution_result.get("timed_out")),
    }


def _resolve_followup_command_gate_response(
    *,
    session_id: str,
    message_id: str,
    raw_command: str,
    command_meta: Dict[str, Any],
    confirmation_message: str,
    confirmed: bool,
    elevated: bool,
) -> Optional[Dict[str, Any]]:
    if not bool(command_meta.get("supported")):
        return _build_followup_unsupported_command_response(
            session_id=session_id,
            message_id=message_id,
            raw_command=raw_command,
            command_meta=command_meta,
        )

    if bool(command_meta.get("requires_write_permission")):
        if not _resolve_followup_write_enabled() and not _followup_test_permissive_enabled():
            return _build_followup_write_disabled_response(
                session_id=session_id,
                message_id=message_id,
                raw_command=raw_command,
                command_meta=command_meta,
                confirmation_message=confirmation_message,
            )
        if not elevated:
            return _build_followup_elevation_required_response(
                session_id=session_id,
                message_id=message_id,
                raw_command=raw_command,
                command_meta=command_meta,
                confirmation_message=confirmation_message,
            )

    if not confirmed:
        return _build_followup_confirmation_required_response(
            session_id=session_id,
            message_id=message_id,
            raw_command=raw_command,
            command_meta=command_meta,
            confirmation_message=confirmation_message,
        )
    return None


def _truncate_command_output(text: str) -> Tuple[str, bool]:
    content = _as_str(text)
    if len(content) <= _FOLLOWUP_COMMAND_MAX_OUTPUT_CHARS:
        return content, False
    clipped = content[: _FOLLOWUP_COMMAND_MAX_OUTPUT_CHARS]
    return f"{clipped}\n...<truncated>...", True


def _contains_blocked_shell_operator(command: str) -> bool:
    if _followup_test_permissive_enabled():
        return False
    parsed = _parse_followup_command_segments(command)
    return not bool(parsed.get("ok"))


def _execute_followup_command(
    command: str,
    timeout_seconds: int,
) -> Dict[str, Any]:
    started = time.perf_counter()
    permissive = _followup_test_permissive_enabled()
    parsed = _parse_followup_command_segments(command)
    if not bool(parsed.get("ok")):
        raise HTTPException(status_code=400, detail=_FOLLOWUP_COMMAND_UNSAFE_DETAIL)
    segments = parsed.get("segments") if isinstance(parsed.get("segments"), list) else []
    operators = parsed.get("operators") if isinstance(parsed.get("operators"), list) else []
    if not segments:
        raise HTTPException(status_code=400, detail="命令为空，无法执行")
    if len(command) > _FOLLOWUP_COMMAND_MAX_CHARS:
        raise HTTPException(status_code=400, detail=f"命令长度超过限制({_FOLLOWUP_COMMAND_MAX_CHARS})")
    if (
        not permissive
        and (any(fragment in command for fragment in _FOLLOWUP_COMMAND_BLOCKED_FRAGMENTS) or _contains_blocked_shell_operator(command))
    ):
        raise HTTPException(status_code=400, detail=_FOLLOWUP_COMMAND_UNSAFE_DETAIL)

    safe_timeout = max(3, min(120, int(timeout_seconds or _FOLLOWUP_COMMAND_DEFAULT_TIMEOUT)))
    try:
        requires_shell = bool(operators) or "$(" in command or "`" in command
        if requires_shell and not _shell_emergency_enabled():
            raise HTTPException(status_code=400, detail="shell syntax is disabled by policy")
        if requires_shell:
            completed = subprocess.run(
                ["/bin/bash", "-lc", command],
                capture_output=True,
                text=True,
                timeout=safe_timeout,
                check=False,
            )
        else:
            parts = [_as_str(token) for token in segments[0]]
            completed = subprocess.run(
                parts,
                capture_output=True,
                text=True,
                timeout=safe_timeout,
                check=False,
            )
    except FileNotFoundError:
        head = _as_str((segments[0] if segments else [""])[0])
        raise HTTPException(status_code=400, detail=f"命令不存在: {head}") from None
    except subprocess.TimeoutExpired as exc:
        elapsed_ms = int((time.perf_counter() - started) * 1000)
        stdout_text, stdout_truncated = _truncate_command_output(_as_str(exc.stdout))
        stderr_text, stderr_truncated = _truncate_command_output(_as_str(exc.stderr))
        return {
            "status": "failed",
            "exit_code": -1,
            "duration_ms": elapsed_ms,
            "stdout": stdout_text,
            "stderr": stderr_text or f"命令执行超时（>{safe_timeout}s）",
            "output_truncated": stdout_truncated or stderr_truncated,
            "timed_out": True,
        }
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"命令执行失败: {e}") from e

    elapsed_ms = int((time.perf_counter() - started) * 1000)
    stdout_text, stdout_truncated = _truncate_command_output(_as_str(completed.stdout))
    stderr_text, stderr_truncated = _truncate_command_output(_as_str(completed.stderr))
    return {
        "status": "executed" if int(completed.returncode) == 0 else "failed",
        "exit_code": int(completed.returncode),
        "duration_ms": elapsed_ms,
        "stdout": stdout_text,
        "stderr": stderr_text,
        "output_truncated": stdout_truncated or stderr_truncated,
        "timed_out": False,
    }


def _execute_followup_argv(
    argv: List[str],
    timeout_seconds: int,
) -> Dict[str, Any]:
    started = time.perf_counter()
    safe_timeout = max(3, min(120, int(timeout_seconds or _STRUCTURED_ACTION_PER_ATTEMPT_TIMEOUT_SECONDS)))
    try:
        completed = subprocess.run(
            [_as_str(token) for token in argv if _as_str(token)],
            capture_output=True,
            text=True,
            timeout=safe_timeout,
            check=False,
        )
    except FileNotFoundError:
        head = _as_str((argv or [""])[0])
        raise HTTPException(status_code=400, detail=f"命令不存在: {head}") from None
    except subprocess.TimeoutExpired as exc:
        elapsed_ms = int((time.perf_counter() - started) * 1000)
        stdout_text, stdout_truncated = _truncate_command_output(_as_str(exc.stdout))
        stderr_text, stderr_truncated = _truncate_command_output(_as_str(exc.stderr))
        return {
            "status": "failed",
            "exit_code": -9,
            "duration_ms": elapsed_ms,
            "stdout": stdout_text,
            "stderr": stderr_text or f"命令执行超时（>{safe_timeout}s）",
            "output_truncated": stdout_truncated or stderr_truncated,
            "timed_out": True,
        }
    except Exception as exc:
        raise HTTPException(status_code=500, detail=f"命令执行失败: {exc}") from exc

    elapsed_ms = int((time.perf_counter() - started) * 1000)
    stdout_text, stdout_truncated = _truncate_command_output(_as_str(completed.stdout))
    stderr_text, stderr_truncated = _truncate_command_output(_as_str(completed.stderr))
    return {
        "status": "executed" if int(completed.returncode) == 0 else "failed",
        "exit_code": int(completed.returncode),
        "duration_ms": elapsed_ms,
        "stdout": stdout_text,
        "stderr": stderr_text,
        "output_truncated": stdout_truncated or stderr_truncated,
        "timed_out": False,
    }


def _default_structured_next_suggestion() -> str:
    return "建议先缩小时间窗口或 limit，再提高 timeout 重试。"


def _resolve_kubectl_clickhouse_query_argv(args: Dict[str, Any]) -> Dict[str, Any]:
    namespace = _as_str(args.get("namespace"), "islap").strip().lower()
    if not namespace or not _K8S_NAMESPACE_PATTERN.match(namespace):
        return {"ok": False, "reason": "invalid namespace"}
    pod_selector = _as_str(args.get("pod_selector"), "app=clickhouse").strip()
    if not pod_selector:
        return {"ok": False, "reason": "pod_selector is required"}
    if any(ch in pod_selector for ch in "'\"`$;&|<>"):
        return {"ok": False, "reason": "pod_selector contains unsafe characters"}
    query = _as_str(args.get("query")).replace("\n", " ").replace("\r", " ").strip()
    if not query:
        return {"ok": False, "reason": "query is required"}

    pod_lookup_result = _execute_followup_argv(
        [
            "kubectl",
            "-n",
            namespace,
            "get",
            "pods",
            "-l",
            pod_selector,
            "-o",
            "jsonpath={.items[0].metadata.name}",
        ],
        timeout_seconds=_STRUCTURED_ACTION_PER_ATTEMPT_TIMEOUT_SECONDS,
    )
    if int(pod_lookup_result.get("exit_code") or 0) != 0:
        return {
            "ok": False,
            "reason": _as_str(pod_lookup_result.get("stderr"), "failed to resolve clickhouse pod"),
            "pod_lookup": pod_lookup_result,
        }
    pod_name = _as_str(pod_lookup_result.get("stdout")).strip()
    if not pod_name:
        return {"ok": False, "reason": "clickhouse pod not found", "pod_lookup": pod_lookup_result}

    return {
        "ok": True,
        "command_argv": [
            "kubectl",
            "-n",
            namespace,
            "exec",
            "-i",
            pod_name,
            "--",
            "clickhouse-client",
            "--query",
            query,
        ],
        "command_display": (
            f"kubectl -n {namespace} exec -i {pod_name} -- clickhouse-client --query \"{query}\""
        ),
    }


def execute_action_spec(
    tool: str,
    args: Dict[str, Any],
    context: Optional[Dict[str, Any]] = None,
) -> Dict[str, Any]:
    safe_tool = _as_str(tool).strip().lower()
    safe_args = args if isinstance(args, dict) else {}
    _ = context if isinstance(context, dict) else {}

    if safe_tool not in {"kubectl_clickhouse_query", "k8s_clickhouse_query", "clickhouse_query"}:
        return {
            "status": "blocked",
            "tool": safe_tool or "unknown",
            "timed_out": False,
            "attempt": 0,
            "max_attempts": _STRUCTURED_ACTION_MAX_ATTEMPTS,
            "exit_code": -1,
            "stdout": "",
            "stderr": "",
            "output_truncated": False,
            "next_suggestion": _default_structured_next_suggestion(),
            "message": f"unsupported structured action tool: {safe_tool or 'unknown'}",
        }

    resolved = _resolve_kubectl_clickhouse_query_argv(safe_args)
    if not bool(resolved.get("ok")):
        lookup_payload = resolved.get("pod_lookup") if isinstance(resolved.get("pod_lookup"), dict) else {}
        return {
            "status": "blocked",
            "tool": "kubectl_clickhouse_query",
            "command": _as_str(resolved.get("command_display")),
            "timed_out": bool(lookup_payload.get("timed_out")),
            "attempt": 1,
            "max_attempts": _STRUCTURED_ACTION_MAX_ATTEMPTS,
            "exit_code": int(lookup_payload.get("exit_code") or -1),
            "stdout": _as_str(lookup_payload.get("stdout")),
            "stderr": _as_str(lookup_payload.get("stderr") or resolved.get("reason")),
            "output_truncated": bool(lookup_payload.get("output_truncated")),
            "next_suggestion": _default_structured_next_suggestion(),
            "message": _as_str(resolved.get("reason"), "structured action resolve failed"),
        }

    command_argv = resolved.get("command_argv") if isinstance(resolved.get("command_argv"), list) else []
    command_display = _as_str(resolved.get("command_display"))
    attempts = _STRUCTURED_ACTION_MAX_ATTEMPTS
    last_result: Dict[str, Any] = {}

    for attempt in range(1, attempts + 1):
        result = _execute_followup_argv(
            [_as_str(token) for token in command_argv],
            timeout_seconds=_STRUCTURED_ACTION_PER_ATTEMPT_TIMEOUT_SECONDS,
        )
        timed_out = bool(result.get("timed_out")) or int(result.get("exit_code") or 0) == -9
        status = _as_str(result.get("status"), "failed").strip().lower() or "failed"
        should_retry = timed_out and attempt < attempts
        if should_retry:
            last_result = {
                **result,
                "status": "retrying",
                "command": command_display,
                "tool": "kubectl_clickhouse_query",
                "timed_out": timed_out,
                "attempt": attempt,
                "max_attempts": attempts,
                "next_suggestion": _default_structured_next_suggestion(),
            }
            continue
        if timed_out and attempt >= attempts:
            status = "blocked"
        last_result = {
            **result,
            "status": status,
            "command": command_display,
            "tool": "kubectl_clickhouse_query",
            "timed_out": timed_out,
            "attempt": attempt,
            "max_attempts": attempts,
            "next_suggestion": _default_structured_next_suggestion(),
        }
        break

    if not last_result:
        last_result = {
            "status": "failed",
            "command": command_display,
            "tool": "kubectl_clickhouse_query",
            "timed_out": False,
            "attempt": attempts,
            "max_attempts": attempts,
            "exit_code": -1,
            "duration_ms": 0,
            "stdout": "",
            "stderr": "structured action execution failed without result",
            "output_truncated": False,
            "next_suggestion": _default_structured_next_suggestion(),
        }
    return last_result
