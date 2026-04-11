"""
Command policy helpers for exec-service.
"""

import json
import os
import re
import shlex
import time
from typing import Any, Dict, List


BLOCKED_FRAGMENTS = ("`", "\n", "\r")
CHAIN_OPERATORS = {
    "|",
    "|&",
    "||",
    "&&",
    ";",
}
BLOCKED_OPERATORS = {
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
DEFAULT_ALLOWED_HEADS = {
    "kubectl",
    "curl",
    "clickhouse-client",
    "clickhouse",
    "openstack",
    "psql",
    "postgres",
    "mysql",
    "mariadb",
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
COMMAND_REPAIR_HEADS = tuple(sorted(DEFAULT_ALLOWED_HEADS, key=len, reverse=True))
MAX_COMMAND_CHARS = max(24, int(os.getenv("EXEC_COMMAND_MAX_CHARS", "320")))
AUTO_REWRITE_MAX_ATTEMPTS = 1
KUBECTL_VERB_PATTERN = (
    "getpods|get|describe|logs|exec|top|rollout|apply|delete|patch|edit|replace|scale|set|annotate|label|"
    "create|expose|autoscale|cordon|uncordon|drain|taint"
)

COMMAND_PREFIX_PATTERN = re.compile(
    r"^\s*(?:请(?:先|帮我)?(?:执行|运行)?|执行|运行|command|cmd)\s*(?:以下)?\s*(?:命令)?\s*[:：]\s*",
    flags=re.IGNORECASE,
)

KUBECTL_FLAGS_WITH_VALUE = {
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
KUBECTL_BOOLEAN_FLAGS = {
    "-a",
    "-A",
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
TEMPLATE_PLACEHOLDER_PATTERN = re.compile(r"^<[A-Za-z][A-Za-z0-9_:\-]*>$")
TEMPLATE_PLACEHOLDER_INLINE_PATTERN = re.compile(r"(?<![A-Za-z0-9_])<[A-Za-z][A-Za-z0-9_:\-]*>(?![A-Za-z0-9_])")
KUBECTL_QUERY_ALLOWED_FLAGS_WITH_VALUE = {
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
    "--since",
    "--since-time",
    "--tail",
    "--max-log-requests",
    "--pod-running-timeout",
    "--revision",
    "--for",
    "--timeout",
}
KUBECTL_QUERY_ALLOWED_BOOLEAN_FLAGS = {
    "-a",
    "-A",
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
    "--previous",
    "--timestamps",
    "--all-containers",
}
KUBECTL_QUERY_ALLOWED_OUTPUT_PREFIXES = (
    "json",
    "yaml",
    "wide",
    "name",
    "jsonpath",
    "jsonpath-as-json",
    "custom-columns",
    "go-template",
)
CLICKHOUSE_SQL_MULTIWORD_KEYWORD_REPAIRS = (
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
CLICKHOUSE_SQL_SPACE_REQUIRED_KEYWORDS = (
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
DEFAULT_COMMAND_CATALOG = {
    "kubernetes": {
        "readonly_verbs": [
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
        ],
        "mutating_verbs": [
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
        ],
        "rollout": {
            "readonly_subverbs": ["status", "history"],
            "mutating_subverbs": ["restart", "undo", "pause", "resume"],
        },
        "query_whitelist": {
            "verbs": [
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
                "rollout",
            ],
            "rollout_subverbs": ["status", "history"],
        },
    },
    "openstack": {
        "readonly_verbs": ["list", "show"],
        "mutating_verbs": [
            "create",
            "delete",
            "set",
            "unset",
            "add",
            "remove",
            "reboot",
            "pause",
            "unpause",
            "lock",
            "unlock",
            "start",
            "stop",
            "shelve",
            "unshelve",
            "migrate",
            "live-migrate",
            "resize",
            "confirm",
            "revert",
            "failover",
        ],
    },
    "sql_clients": {
        "readonly_prefixes": ["select", "show", "describe", "desc", "explain", "with"],
        "mutating_prefixes": [
            "insert",
            "update",
            "delete",
            "alter",
            "create",
            "drop",
            "truncate",
            "grant",
            "revoke",
            "replace",
            "merge",
            "analyze",
            "vacuum",
        ],
    },
}
_COMMAND_CATALOG_CACHE: Dict[str, Any] = {
    "loaded_at": 0.0,
    "path": "",
    "mtime": -1.0,
    "data": None,
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
        return bool(default)
    text = as_str(value).strip().lower()
    if text in {"1", "true", "yes", "on"}:
        return True
    if text in {"0", "false", "no", "off"}:
        return False
    return bool(default)


def _normalize_token_list(value: Any) -> List[str]:
    if not isinstance(value, list):
        return []
    normalized: List[str] = []
    seen = set()
    for item in value:
        token = as_str(item).strip().lower()
        if not token or token in seen:
            continue
        seen.add(token)
        normalized.append(token)
    return normalized


def _deep_merge_catalog(defaults: Dict[str, Any], override: Dict[str, Any]) -> Dict[str, Any]:
    merged = dict(defaults)
    for key, value in override.items():
        existing = merged.get(key)
        if isinstance(existing, dict) and isinstance(value, dict):
            merged[key] = _deep_merge_catalog(existing, value)
            continue
        merged[key] = value
    return merged


def _resolve_command_catalog_path() -> str:
    from_env = as_str(os.getenv("EXEC_COMMAND_CATALOG_FILE")).strip()
    if from_env:
        return from_env
    return os.path.abspath(
        os.path.join(os.path.dirname(__file__), "..", "config", "command_catalog.json")
    )


def _load_command_catalog() -> Dict[str, Any]:
    ttl_seconds = max(
        1,
        min(300, int(as_str(os.getenv("EXEC_COMMAND_CATALOG_CACHE_TTL_SECONDS"), "5"))),
    )
    now_ts = time.time()
    path = _resolve_command_catalog_path()
    cache_data = _COMMAND_CATALOG_CACHE.get("data")
    cache_path = as_str(_COMMAND_CATALOG_CACHE.get("path"))
    cache_mtime = float(_COMMAND_CATALOG_CACHE.get("mtime") or -1.0)
    cache_loaded_at = float(_COMMAND_CATALOG_CACHE.get("loaded_at") or 0.0)

    mtime = -1.0
    try:
        mtime = os.path.getmtime(path)
    except Exception:
        mtime = -1.0

    if (
        isinstance(cache_data, dict)
        and cache_path == path
        and cache_mtime == mtime
        and (now_ts - cache_loaded_at) < ttl_seconds
    ):
        return cache_data

    catalog: Dict[str, Any] = _deep_merge_catalog(DEFAULT_COMMAND_CATALOG, {})
    if mtime >= 0:
        try:
            with open(path, "r", encoding="utf-8") as file_obj:
                payload = json.load(file_obj)
            if isinstance(payload, dict):
                catalog = _deep_merge_catalog(catalog, payload)
        except Exception:
            catalog = _deep_merge_catalog(DEFAULT_COMMAND_CATALOG, {})

    _COMMAND_CATALOG_CACHE["data"] = catalog
    _COMMAND_CATALOG_CACHE["path"] = path
    _COMMAND_CATALOG_CACHE["mtime"] = mtime
    _COMMAND_CATALOG_CACHE["loaded_at"] = now_ts
    return catalog


def _catalog_token_set(path: List[str], fallback: List[str]) -> set[str]:
    catalog = _load_command_catalog()
    cursor: Any = catalog
    for key in path:
        if not isinstance(cursor, dict):
            return set(_normalize_token_list(fallback))
        cursor = cursor.get(key)
    if not isinstance(cursor, list):
        return set(_normalize_token_list(fallback))
    normalized = _normalize_token_list(cursor)
    return set(normalized) if normalized else set(_normalize_token_list(fallback))


def allowed_heads() -> set:
    configured = as_str(os.getenv("EXEC_ALLOWED_HEADS"), "")
    if not configured:
        return set(DEFAULT_ALLOWED_HEADS)
    return {item.strip().lower() for item in configured.split(",") if item.strip()}


def is_test_permissive_enabled() -> bool:
    return as_bool(os.getenv("EXEC_COMMAND_TEST_PERMISSIVE"), False)


def is_shell_emergency_enabled() -> bool:
    return as_bool(os.getenv("AI_RUNTIME_SHELL_EMERGENCY_ENABLED"), False)


def _collapse_unquoted_whitespace(text: str) -> str:
    """Collapse whitespace outside quotes without changing quoted arguments."""
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
    repaired = as_str(query_text)
    if not repaired:
        return ""

    for compact, expanded in CLICKHOUSE_SQL_MULTIWORD_KEYWORD_REPAIRS:
        repaired = re.sub(rf"{compact}", f" {expanded} ", repaired)

    for keyword in CLICKHOUSE_SQL_SPACE_REQUIRED_KEYWORDS:
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
    text = as_str(command)
    if not text:
        return ""

    lowered = text.lower()
    if "clickhouse-client" not in lowered and " clickhouse " not in f" {lowered} ":
        return text

    def _replace_quoted(match: re.Match[str]) -> str:
        prefix = as_str(match.group("prefix"))
        quote = as_str(match.group("quote"))
        body = as_str(match.group("body"))
        return f"{prefix}{quote}{_repair_clickhouse_query_text(body)}{quote}"

    def _replace_unquoted(match: re.Match[str]) -> str:
        prefix = as_str(match.group("prefix"))
        body = as_str(match.group("body"))
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
    # kubectl exec 执行 clickhouse 非交互查询时，-i/-t 会导致受控执行面偶发阻塞；
    # 命令已携带 --query/-q 时统一移除交互标志，避免命令无输出超时。
    text = re.sub(
        (
            r"(?i)(\bkubectl\b[^\n]*?\bexec\b[^\n]*?)\s+-(?:i|it|ti)\b"
            r"(?=[^\n]*?\s+--\s+(?:clickhouse-client|clickhouse)\b)"
            r"(?=[^\n]*?(?:--query|-q)(?:\s|=|\"|'))"
        ),
        r"\1",
        text,
    )
    return text


def _repair_command_spacing(command: str) -> str:
    text = as_str(command).strip()
    if not text:
        return ""
    # 常见错误：kubectlexec / kubectlgetpods
    text = re.sub(
        rf"(^|[\s(])kubectl(?=(?:{KUBECTL_VERB_PATTERN})\b)",
        r"\1kubectl ",
        text,
        flags=re.IGNORECASE,
    )
    text = re.sub(r"(\bkubectl\s+)getpods(?=[\s\-]|$)", r"\1get pods", text, flags=re.IGNORECASE)
    text = re.sub(r"(\bkubectl\s+logs)-n([A-Za-z0-9._-]+)(?=\s|$)", r"\1 -n \2", text, flags=re.IGNORECASE)
    text = re.sub(r"(\bkubectl\s+logs)-([A-Za-z0-9._-]+)(?=\s|$)", r"\1 -n \2", text, flags=re.IGNORECASE)
    text = re.sub(r"(\bkubectl\s+exec)\s*-n([A-Za-z0-9._-]+)-it(?=\s|$)", r"\1 -n \2 -it", text, flags=re.IGNORECASE)
    text = re.sub(r"(^|[\s(])-n([A-Za-z0-9._-]+)(?=-[A-Za-z])", r"\1-n \2 ", text, flags=re.IGNORECASE)
    text = re.sub(
        r"(^|\s)-n\s+([A-Za-z0-9._-]+)(get|describe|logs|exec|top|rollout|apply|delete|patch|edit|replace|scale|set|annotate|label|create|expose|autoscale|cordon|uncordon|drain|taint)(?=\s|$)",
        r"\1-n \2 \3",
        text,
        flags=re.IGNORECASE,
    )
    text = re.sub(r"(^|[\s(])-l([A-Za-z0-9._-]+=)", r"\1-l \2", text, flags=re.IGNORECASE)
    text = re.sub(r"(^|[\s(])-o([A-Za-z][A-Za-z0-9_.-]*=)", r"\1-o \2", text, flags=re.IGNORECASE)
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
    text = re.sub(r"(-n\s+[A-Za-z0-9._-]+)-l([A-Za-z0-9._-]+=)", r"\1 -l \2", text, flags=re.IGNORECASE)
    text = re.sub(r"(-l\s+[A-Za-z0-9._-]+=[A-Za-z0-9._-]+)-o([A-Za-z][A-Za-z0-9_.-]*=)", r"\1 -o \2", text, flags=re.IGNORECASE)
    text = re.sub(r"\)(--[A-Za-z])", r") \1", text)
    text = re.sub(r"(--[A-Za-z][\w-]*)(--[A-Za-z][\w-]*)", r"\1 \2", text)
    text = re.sub(r"(--[A-Za-z][\w-]*)(<[^>\s]+>)(?=--)", r"\1 \2 ", text)
    text = re.sub(r"(--[A-Za-z][\w-]*)(<[^>\s]+>)", r"\1 \2", text)
    text = re.sub(r"\)\s+--(clickhouse-client|clickhouse)(?=\s|$)", r") -- \1", text, flags=re.IGNORECASE)
    text = re.sub(r"(--[A-Za-z][\w-]*)(?=(['\"]))", r"\1 ", text)
    text = re.sub(r"(-[A-Za-z]{1,4})\$\(", r"\1 $(", text)
    text = re.sub(r"\s--([A-Za-z][\w]*)\s-([A-Za-z][\w-]*)(?=\s|$)", r" -- \1-\2", text)
    text = re.sub(r"\s--\s*(clickhouse)\s-([A-Za-z][\w-]*)(?=\s|$)", r" -- \1-\2", text, flags=re.IGNORECASE)
    text = re.sub(r"(^|\s)-n([A-Za-z0-9._-]+)getpods(?=\s|$)", r"\1-n \2 get pods", text, flags=re.IGNORECASE)
    text = re.sub(
        r"(^|\s)-n([A-Za-z0-9._-]+)(get|describe|logs|exec|top|rollout|apply|delete|patch|edit|replace|scale|set|annotate|label|create|expose|autoscale|cordon|uncordon|drain|taint)(?=\s|$)",
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
    return _collapse_unquoted_whitespace(text)


def parse_command_segments(command: str) -> Dict[str, Any]:
    permissive = is_test_permissive_enabled()
    try:
        lexer = shlex.shlex(command, posix=True, punctuation_chars="|&;<>")
        lexer.whitespace_split = True
        lexer.commenters = ""
        segments: List[List[str]] = []
        current: List[str] = []
        operators: List[str] = []
        for token in lexer:
            normalized = as_str(token).strip()
            if not normalized:
                continue
            if normalized in CHAIN_OPERATORS:
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
            if normalized in BLOCKED_OPERATORS:
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


def contains_blocked_operator(command: str) -> bool:
    return not bool(parse_command_segments(command).get("ok"))


def normalize_command(command: str) -> str:
    normalized = as_str(command).strip()
    if normalized.startswith("`") and normalized.endswith("`") and len(normalized) > 2:
        normalized = normalized[1:-1].strip()
    normalized = re.sub(r"^\s*(?:执行命令|命令)\s*[:：]\s*", "", normalized)
    first_token = normalized.split(maxsplit=1)[0].lower() if normalized else ""
    if first_token not in allowed_heads():
        for head in COMMAND_REPAIR_HEADS:
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
    return _repair_command_spacing(normalized)


def _unwrap_full_code_fence(command: str) -> str:
    text = as_str(command).strip()
    if not (text.startswith("```") and text.endswith("```")):
        return text
    lines = text.splitlines()
    if len(lines) < 2:
        return text
    body = "\n".join(lines[1:-1]).strip()
    return body or text


def _unwrap_single_backtick(command: str) -> str:
    text = as_str(command).strip()
    if len(text) >= 2 and text.startswith("`") and text.endswith("`") and text.count("`") == 2:
        return text[1:-1].strip()
    return text


def _strip_wrapper_quotes(command: str) -> str:
    text = as_str(command).strip()
    if len(text) < 2:
        return text
    if (text.startswith('"') and text.endswith('"')) or (text.startswith("'") and text.endswith("'")):
        return text[1:-1].strip()
    return text


def _strip_prompt_and_prefix(command: str) -> str:
    text = as_str(command).strip()
    if not text:
        return ""
    stripped = re.sub(r"^\s*[$#>]\s*", "", text)
    stripped = COMMAND_PREFIX_PATTERN.sub("", stripped)
    return stripped.strip()


def _pick_first_candidate_line(command: str) -> str:
    lines = [as_str(item).strip() for item in as_str(command).splitlines()]
    lines = [line for line in lines if line]
    if not lines:
        return ""
    heads = allowed_heads()
    for line in lines:
        candidate = _strip_prompt_and_prefix(line)
        parts = candidate.split()
        if parts and as_str(parts[0]).lower() in heads:
            return candidate
    return _strip_prompt_and_prefix(lines[0])


def _repair_glued_head(command: str) -> str:
    text = as_str(command).strip()
    if not text:
        return ""
    lowered = text.lower()
    candidate_heads = sorted(
        [
            head for head in allowed_heads()
            if head in {"kubectl", "curl", "helm", "openstack", "clickhouse-client", "clickhouse", "systemctl", "service"}
        ],
        key=len,
        reverse=True,
    )
    for head in candidate_heads:
        if not lowered.startswith(head):
            continue
        if len(text) <= len(head):
            return text
        next_char = text[len(head)]
        if next_char.isspace():
            return text
        return f"{text[:len(head)]} {text[len(head):]}".strip()
    return text


def rewrite_unknown_command(command: str) -> Dict[str, Any]:
    raw = as_str(command)
    candidate = raw
    rewrite_steps: List[str] = []

    unwrapped_fence = _unwrap_full_code_fence(candidate)
    if unwrapped_fence != candidate:
        rewrite_steps.append("unwrap_code_fence")
        candidate = unwrapped_fence

    unwrapped_tick = _unwrap_single_backtick(candidate)
    if unwrapped_tick != candidate:
        rewrite_steps.append("unwrap_backtick")
        candidate = unwrapped_tick

    unwrapped_quotes = _strip_wrapper_quotes(candidate)
    if unwrapped_quotes != candidate:
        rewrite_steps.append("unwrap_quotes")
        candidate = unwrapped_quotes

    line_candidate = _pick_first_candidate_line(candidate)
    if line_candidate and line_candidate != candidate:
        rewrite_steps.append("pick_candidate_line")
        candidate = line_candidate

    no_prefix = _strip_prompt_and_prefix(candidate)
    if no_prefix and no_prefix != candidate:
        rewrite_steps.append("strip_prompt_prefix")
        candidate = no_prefix

    # Prefix stripping may expose wrapped command text (for example: 请执行命令：`kubectl ...`).
    unwrapped_tick_after_prefix = _unwrap_single_backtick(candidate)
    if unwrapped_tick_after_prefix != candidate:
        rewrite_steps.append("unwrap_backtick_after_prefix")
        candidate = unwrapped_tick_after_prefix

    unwrapped_quotes_after_prefix = _strip_wrapper_quotes(candidate)
    if unwrapped_quotes_after_prefix != candidate:
        rewrite_steps.append("unwrap_quotes_after_prefix")
        candidate = unwrapped_quotes_after_prefix

    repaired_head = _repair_glued_head(candidate)
    if repaired_head != candidate:
        rewrite_steps.append("repair_glued_head")
        candidate = repaired_head

    final_candidate = normalize_command(candidate)
    if final_candidate != candidate:
        rewrite_steps.append("normalize_command")

    return {
        "candidate": final_candidate,
        "applied": bool(final_candidate and final_candidate != normalize_command(raw)),
        "steps": rewrite_steps,
    }


def _consume_kubectl_flag(parts: List[str], index: int) -> int:
    token = as_str(parts[index]).strip().lower()
    if not token.startswith("-"):
        return index
    if token.startswith("--"):
        if "=" in token:
            return index + 1
        if token in KUBECTL_BOOLEAN_FLAGS:
            return index + 1
        return min(len(parts), index + 2)
    if token in KUBECTL_BOOLEAN_FLAGS:
        return index + 1
    if token in KUBECTL_FLAGS_WITH_VALUE:
        return min(len(parts), index + 2)
    if len(token) > 2 and token[:2] in KUBECTL_FLAGS_WITH_VALUE:
        return index + 1
    return index + 1


def _extract_kubectl_verbs(parts: List[str]) -> tuple[str, str]:
    cursor = 1
    verb = ""
    while cursor < len(parts):
        token = as_str(parts[cursor]).strip().lower()
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
        token = as_str(parts[cursor]).strip().lower()
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


def _extract_clickhouse_query(parts: List[str]) -> str:
    for index in range(1, len(parts)):
        token = as_str(parts[index]).strip()
        lowered = token.lower()
        if lowered in {"--query", "-q"}:
            if index + 1 < len(parts):
                return " ".join(as_str(item) for item in parts[index + 1 :]).strip()
            return ""
        if lowered.startswith("--query="):
            return token.split("=", 1)[1].strip()
        if lowered.startswith("-q=") and len(token) > 3:
            return token[3:].strip()
        if lowered.startswith("-q") and len(token) > 2 and lowered != "-query":
            return token[2:].strip()
    return ""


def _extract_sql_query(parts: List[str], *, head: str) -> str:
    safe_head = as_str(head).strip().lower()
    if safe_head in {"psql", "postgres"}:
        for index in range(1, len(parts)):
            token = as_str(parts[index]).strip()
            lowered = token.lower()
            if lowered in {"-c", "--command"}:
                if index + 1 < len(parts):
                    return as_str(parts[index + 1]).strip()
                return ""
            if lowered.startswith("--command="):
                return token.split("=", 1)[1].strip()
            if lowered.startswith("-c") and len(token) > 2:
                return token[2:].strip()
        return ""
    if safe_head in {"mysql", "mariadb"}:
        for index in range(1, len(parts)):
            token = as_str(parts[index]).strip()
            lowered = token.lower()
            if lowered in {"-e", "--execute"}:
                if index + 1 < len(parts):
                    return as_str(parts[index + 1]).strip()
                return ""
            if lowered.startswith("--execute="):
                return token.split("=", 1)[1].strip()
            if lowered.startswith("-e") and len(token) > 2:
                return token[2:].strip()
        return ""
    return ""


def _extract_option_value(parts: List[str], flags: set[str], default: str = "") -> str:
    for index in range(1, len(parts)):
        token = as_str(parts[index]).strip()
        lowered = token.lower()
        if lowered in flags:
            if index + 1 < len(parts):
                return as_str(parts[index + 1]).strip()
            return default
        for flag in flags:
            if lowered.startswith(f"{flag}="):
                return token.split("=", 1)[1].strip()
    return default


def _has_explicit_blocked_operators(command: str) -> bool:
    try:
        lexer = shlex.shlex(command, posix=True, punctuation_chars="|&;<>")
        lexer.whitespace_split = True
        lexer.commenters = ""
        for token in lexer:
            if as_str(token).strip() in BLOCKED_OPERATORS:
                return True
    except Exception:
        return True
    return False


def _is_allowed_kubectl_exec_substitution(command: str) -> bool:
    """
    Allow a narrow shell-substitution shape used by kubectl exec target selection:
    `kubectl ... exec ... $(kubectl ... get pods ...) -- <remote-cmd>`.
    """
    normalized = as_str(command).strip().lower()
    if not normalized or not normalized.startswith("kubectl "):
        return False
    if "$(" not in normalized:
        return False
    if " exec " not in f" {normalized} ":
        return False
    if " -- " not in f" {normalized} ":
        return False
    return bool(re.search(r"\$\(\s*kubectl\b", normalized))


def _contains_unresolved_template_placeholder(parts: List[str]) -> bool:
    for token in parts:
        normalized = as_str(token).strip()
        if not normalized:
            continue
        if TEMPLATE_PLACEHOLDER_PATTERN.match(normalized):
            return True
    return False


def _contains_unresolved_template_placeholder_text(command: str) -> bool:
    return bool(TEMPLATE_PLACEHOLDER_INLINE_PATTERN.search(as_str(command)))


def _extract_kubectl_exec_command(parts: List[str]) -> List[str]:
    for index, token in enumerate(parts):
        if as_str(token).strip() == "--":
            return [as_str(item) for item in parts[index + 1 :] if as_str(item).strip()]
    return []


def _build_command_meta(
    *,
    supported: bool,
    command: str,
    reason: str,
    command_type: str = "unknown",
    risk_level: str = "high",
    requires_write_permission: bool = False,
    command_family: str = "unknown",
    approval_policy: str = "deny",
    executor_type: str = "local_process",
    executor_profile: str = "local-default",
    target_kind: str = "runtime_node",
    target_identity: str = "runtime:local",
) -> Dict[str, Any]:
    return {
        "supported": bool(supported),
        "command_type": as_str(command_type, "unknown"),
        "risk_level": as_str(risk_level, "high"),
        "requires_write_permission": bool(requires_write_permission),
        "reason": as_str(reason),
        "command": as_str(command),
        "command_family": as_str(command_family, "unknown"),
        "approval_policy": as_str(approval_policy, "deny"),
        "executor_type": as_str(executor_type, "local_process"),
        "executor_profile": as_str(executor_profile, "local-default"),
        "target_kind": as_str(target_kind, "runtime_node"),
        "target_identity": as_str(target_identity, "runtime:local"),
    }


def _read_only_meta(
    *,
    command: str,
    reason: str,
    command_family: str,
    executor_type: str,
    executor_profile: str,
    target_kind: str,
    target_identity: str,
) -> Dict[str, Any]:
    return _build_command_meta(
        supported=True,
        command=command,
        reason=reason,
        command_type="query",
        risk_level="low",
        requires_write_permission=False,
        command_family=command_family,
        approval_policy="auto_execute",
        executor_type=executor_type,
        executor_profile=executor_profile,
        target_kind=target_kind,
        target_identity=target_identity,
    )


def _mutating_meta(
    *,
    command: str,
    reason: str,
    command_family: str,
    executor_type: str,
    executor_profile: str,
    target_kind: str,
    target_identity: str,
) -> Dict[str, Any]:
    return _build_command_meta(
        supported=True,
        command=command,
        reason=reason,
        command_type="repair",
        risk_level="high",
        requires_write_permission=True,
        command_family=command_family,
        approval_policy="elevation_required",
        executor_type=executor_type,
        executor_profile=executor_profile,
        target_kind=target_kind,
        target_identity=target_identity,
    )


def _permissive_elevation_meta(command: str, reason: str) -> Dict[str, Any]:
    safe_reason = f"{reason}; test permissive requires elevation approval"
    head = as_str(command).split(maxsplit=1)[0].strip().lower()

    # Even in test permissive mode, keep execution on controlled executor profiles.
    # This avoids silently falling back to local process and bypassing gateway/RBAC lanes.
    if head == "kubectl":
        namespace = as_str(os.getenv("EXEC_DEFAULT_K8S_NAMESPACE"), "default") or "default"
        try:
            parts = shlex.split(command)
            namespace = _extract_option_value(
                parts,
                {"-n", "--namespace"},
                namespace,
            ) or namespace
        except Exception:
            pass
        return _mutating_meta(
            command=command,
            reason=safe_reason,
            command_family="kubernetes",
            executor_type="privileged_sandbox_pod",
            executor_profile="toolbox-k8s-mutating",
            target_kind="k8s_cluster",
            target_identity=f"namespace:{namespace}",
        )

    if head in {"clickhouse-client", "clickhouse"}:
        database = as_str(os.getenv("EXEC_DEFAULT_CLICKHOUSE_DATABASE"), "default") or "default"
        try:
            parts = shlex.split(command)
            database = _extract_option_value(parts, {"-d", "--database"}, database) or database
        except Exception:
            pass
        return _mutating_meta(
            command=command,
            reason=safe_reason,
            command_family="clickhouse",
            executor_type="privileged_sandbox_pod",
            executor_profile="toolbox-clickhouse-mutating",
            target_kind="clickhouse_cluster",
            target_identity=f"database:{database}",
        )

    if head == "openstack":
        project_name = as_str(os.getenv("OS_PROJECT_NAME"), "default") or "default"
        try:
            parts = shlex.split(command)
            project_name = _extract_option_value(parts, {"--os-project-name"}, project_name) or project_name
        except Exception:
            pass
        return _mutating_meta(
            command=command,
            reason=safe_reason,
            command_family="openstack",
            executor_type="privileged_sandbox_pod",
            executor_profile="toolbox-openstack-mutating",
            target_kind="openstack_project",
            target_identity=f"project:{project_name}",
        )

    if head == "curl":
        target_url = "http:unknown"
        try:
            parts = shlex.split(command)
            target_url = next(
                (token for token in parts[1:] if as_str(token).startswith(("http://", "https://"))),
                "",
            ) or target_url
        except Exception:
            pass
        return _mutating_meta(
            command=command,
            reason=safe_reason,
            command_family="http",
            executor_type="external_control_plane",
            executor_profile="toolbox-http-mutating",
            target_kind="http_endpoint",
            target_identity=target_url,
        )

    if head in {"systemctl", "service"}:
        return _mutating_meta(
            command=command,
            reason=safe_reason,
            command_family="host_control",
            executor_type="ssh_gateway",
            executor_profile="host-ssh-mutating",
            target_kind="host_node",
            target_identity=as_str(os.getenv("EXEC_DEFAULT_HOST_TARGET"), "host:primary"),
        )

    return _mutating_meta(
        command=command,
        reason=safe_reason,
        command_family="shell",
        executor_type="privileged_sandbox_pod",
        executor_profile="busybox-mutating",
        target_kind="runtime_workspace",
        target_identity="workspace:local",
    )


def _merge_chained_command_meta(
    *,
    command: str,
    segment_metas: List[Dict[str, Any]],
    operators: List[str],
) -> Dict[str, Any]:
    for index, segment_meta in enumerate(segment_metas):
        if bool(segment_meta.get("supported")):
            continue
        return _build_command_meta(
            supported=False,
            command=command,
            reason=f"segment {index + 1} unsupported: {as_str(segment_meta.get('reason'), 'unsupported command type')}",
            command_type=as_str(segment_meta.get("command_type"), "unknown"),
            risk_level=as_str(segment_meta.get("risk_level"), "high"),
            requires_write_permission=bool(segment_meta.get("requires_write_permission")),
            command_family="composite",
            approval_policy="deny",
            executor_type=as_str(segment_meta.get("executor_type"), "local_process"),
            executor_profile=as_str(segment_meta.get("executor_profile"), "local-default"),
            target_kind=as_str(segment_meta.get("target_kind"), "runtime_node"),
            target_identity=as_str(segment_meta.get("target_identity"), "runtime:local"),
        )

    first_meta = segment_metas[0]
    requires_write = any(bool(item.get("requires_write_permission")) for item in segment_metas)
    command_type = "repair" if requires_write else "query"
    risk_level = "high" if requires_write else "low"
    chain_reason = (
        f"chained command ({len(segment_metas)} segments, operators: {' '.join(operators)})"
        if operators
        else f"chained command ({len(segment_metas)} segments)"
    )
    families = {as_str(item.get("command_family"), "").strip() for item in segment_metas}
    family = families.pop() if len(families) == 1 else "composite"
    return _build_command_meta(
        supported=True,
        command=command,
        reason=chain_reason,
        command_type=command_type,
        risk_level=risk_level,
        requires_write_permission=requires_write,
        command_family=family,
        approval_policy="elevation_required" if requires_write else "auto_execute",
        executor_type=as_str(first_meta.get("executor_type"), "local_process"),
        executor_profile=as_str(first_meta.get("executor_profile"), "local-default"),
        target_kind=as_str(first_meta.get("target_kind"), "runtime_node"),
        target_identity=as_str(first_meta.get("target_identity"), "runtime:local"),
    )


def classify_command(command: str) -> Dict[str, Any]:
    permissive = is_test_permissive_enabled()
    normalized = normalize_command(command)
    if not normalized:
        return _build_command_meta(
            supported=False,
            command="",
            reason="command is empty",
        )
    if len(normalized) > MAX_COMMAND_CHARS:
        return _build_command_meta(
            supported=False,
            command=normalized,
            reason=f"command exceeds max chars({MAX_COMMAND_CHARS})",
        )
    if _contains_unresolved_template_placeholder_text(normalized):
        return _build_command_meta(
            supported=False,
            command=normalized,
            reason="command contains unresolved template placeholders",
        )
    has_blocked_fragment = any(fragment in normalized for fragment in BLOCKED_FRAGMENTS)
    has_shell_substitution = "$(" in normalized
    has_blocked_operator = _has_explicit_blocked_operators(normalized)
    if permissive and (has_blocked_fragment or has_blocked_operator):
        return _permissive_elevation_meta(
            normalized,
            "test permissive bypass blocked fragments/operators",
        )
    if has_shell_substitution and not permissive and not is_shell_emergency_enabled():
        if not _is_allowed_kubectl_exec_substitution(normalized):
            return _build_command_meta(
                supported=False,
                command=normalized,
                reason="shell substitution is disabled by policy",
            )
    if has_blocked_fragment and not permissive:
        return _build_command_meta(
            supported=False,
            command=normalized,
            reason="command contains blocked fragments/operators",
        )
    parsed = parse_command_segments(normalized)
    if not bool(parsed.get("ok")) and not permissive:
        return _build_command_meta(
            supported=False,
            command=normalized,
            reason="command contains blocked fragments/operators",
        )
    segments = parsed.get("segments") if isinstance(parsed.get("segments"), list) else []
    operators = parsed.get("operators") if isinstance(parsed.get("operators"), list) else []
    if len(segments) > 1:
        segment_metas: List[Dict[str, Any]] = []
        for segment_tokens in segments:
            segment_command = " ".join(shlex.quote(as_str(token)) for token in segment_tokens).strip()
            if not segment_command:
                return _build_command_meta(
                    supported=False,
                    command=normalized,
                    reason="command contains empty chained segment",
                )
            segment_metas.append(classify_command(segment_command))
        return _merge_chained_command_meta(
            command=normalized,
            segment_metas=segment_metas,
            operators=[as_str(item) for item in operators],
        )

    try:
        parts: List[str] = shlex.split(normalized)
    except Exception as exc:
        if permissive:
            return _permissive_elevation_meta(
                normalized,
                f"test permissive bypass parse failure: {exc}",
            )
        return _build_command_meta(
            supported=False,
            command=normalized,
            reason=f"command parse failed: {exc}",
        )
    if not parts:
        return _build_command_meta(
            supported=False,
            command=normalized,
            reason="empty command tokens",
        )

    if _contains_unresolved_template_placeholder(parts):
        return _build_command_meta(
            supported=False,
            command=normalized,
            reason="command contains unresolved template placeholders",
        )

    head = as_str(parts[0]).lower()
    if head not in allowed_heads():
        if permissive:
            return _permissive_elevation_meta(
                normalized,
                "test permissive bypass head allowlist",
            )
        return _build_command_meta(
            supported=False,
            command=normalized,
            reason="command head not allowed",
        )

    if head == "kubectl":
        verb, sub_verb = _extract_kubectl_verbs(parts)
        namespace = _extract_option_value(
            parts,
            {"-n", "--namespace"},
            as_str(os.getenv("EXEC_DEFAULT_K8S_NAMESPACE"), "default"),
        ) or "default"
        readonly = _catalog_token_set(
            ["kubernetes", "readonly_verbs"],
            [
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
            ],
        )
        mutating = _catalog_token_set(
            ["kubernetes", "mutating_verbs"],
            [
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
            ],
        )
        rollout_readonly = _catalog_token_set(
            ["kubernetes", "rollout", "readonly_subverbs"],
            ["status", "history"],
        )
        rollout_mutating = _catalog_token_set(
            ["kubernetes", "rollout", "mutating_subverbs"],
            ["restart", "undo", "pause", "resume"],
        )
        if verb == "rollout":
            if sub_verb in rollout_readonly:
                return _read_only_meta(
                    command=normalized,
                    reason="kubectl rollout read-only command",
                    command_family="kubernetes",
                    executor_type="sandbox_pod",
                    executor_profile="toolbox-k8s-readonly",
                    target_kind="k8s_cluster",
                    target_identity=f"namespace:{namespace}",
                )
            if sub_verb in rollout_mutating:
                return _mutating_meta(
                    command=normalized,
                    reason="kubectl rollout mutating command",
                    command_family="kubernetes",
                    executor_type="privileged_sandbox_pod",
                    executor_profile="toolbox-k8s-mutating",
                    target_kind="k8s_cluster",
                    target_identity=f"namespace:{namespace}",
                )
            return _build_command_meta(
                supported=False,
                command=normalized,
                reason="unsupported kubectl rollout sub-command",
                command_family="kubernetes",
                target_kind="k8s_cluster",
                target_identity=f"namespace:{namespace}",
            )
        if verb in readonly:
            return _read_only_meta(
                command=normalized,
                reason="kubectl read-only command",
                command_family="kubernetes",
                executor_type="sandbox_pod",
                executor_profile="toolbox-k8s-readonly",
                target_kind="k8s_cluster",
                target_identity=f"namespace:{namespace}",
            )
        if verb in mutating:
            return _mutating_meta(
                command=normalized,
                reason="kubectl write command",
                command_family="kubernetes",
                executor_type="privileged_sandbox_pod",
                executor_profile="toolbox-k8s-mutating",
                target_kind="k8s_cluster",
                target_identity=f"namespace:{namespace}",
            )
        if verb == "exec":
            remote_parts = _extract_kubectl_exec_command(parts)
            if not remote_parts:
                if permissive:
                    return _permissive_elevation_meta(
                        normalized,
                        "kubectl exec missing remote command",
                    )
                return _build_command_meta(
                    supported=False,
                    command=normalized,
                    reason="kubectl exec missing remote command",
                    command_family="kubernetes",
                    target_kind="k8s_cluster",
                    target_identity=f"namespace:{namespace}",
                )
            remote_command = " ".join(shlex.quote(item) for item in remote_parts).strip()
            if not remote_command:
                if permissive:
                    return _permissive_elevation_meta(
                        normalized,
                        "kubectl exec remote command is empty",
                    )
                return _build_command_meta(
                    supported=False,
                    command=normalized,
                    reason="kubectl exec remote command is empty",
                    command_family="kubernetes",
                    target_kind="k8s_cluster",
                    target_identity=f"namespace:{namespace}",
                )
            remote_meta = classify_command(remote_command)
            if bool(remote_meta.get("supported")):
                remote_reason = as_str(remote_meta.get("reason"), "kubectl exec remote command")
                if bool(remote_meta.get("requires_write_permission")):
                    return _mutating_meta(
                        command=normalized,
                        reason=f"kubectl exec mutating remote command: {remote_reason}",
                        command_family="kubernetes",
                        executor_type="privileged_sandbox_pod",
                        executor_profile="toolbox-k8s-mutating",
                        target_kind="k8s_cluster",
                        target_identity=f"namespace:{namespace}",
                    )
                return _read_only_meta(
                    command=normalized,
                    reason=f"kubectl exec read-only remote command: {remote_reason}",
                    command_family="kubernetes",
                    executor_type="sandbox_pod",
                    executor_profile="toolbox-k8s-readonly",
                    target_kind="k8s_cluster",
                    target_identity=f"namespace:{namespace}",
                )
            if permissive:
                return _permissive_elevation_meta(
                    normalized,
                    f"kubectl exec remote command unsupported: {as_str(remote_meta.get('reason'))}",
                )
            return _build_command_meta(
                supported=False,
                command=normalized,
                reason=f"kubectl exec remote command unsupported: {as_str(remote_meta.get('reason'))}",
                command_family="kubernetes",
                target_kind="k8s_cluster",
                target_identity=f"namespace:{namespace}",
            )
        return _build_command_meta(
            supported=False,
            command=normalized,
            reason="unsupported kubectl command verb",
            command_family="kubernetes",
            target_kind="k8s_cluster",
            target_identity=f"namespace:{namespace}",
        )

    if head in {"clickhouse-client", "clickhouse"}:
        query_text = _extract_clickhouse_query(parts)
        database = _extract_option_value(
            parts,
            {"-d", "--database"},
            as_str(os.getenv("EXEC_DEFAULT_CLICKHOUSE_DATABASE"), "default"),
        ) or "default"
        if not query_text:
            return _build_command_meta(
                supported=False,
                command=normalized,
                reason="clickhouse command missing --query/-q",
                command_family="clickhouse",
                executor_type="sandbox_pod",
                executor_profile="toolbox-clickhouse-readonly",
                target_kind="clickhouse_cluster",
                target_identity=f"database:{database}",
            )
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
            return _read_only_meta(
                command=normalized,
                reason="clickhouse read-only query",
                command_family="clickhouse",
                executor_type="sandbox_pod",
                executor_profile="toolbox-clickhouse-readonly",
                target_kind="clickhouse_cluster",
                target_identity=f"database:{database}",
            )
        if compact_query.startswith(mutating_prefixes):
            return _mutating_meta(
                command=normalized,
                reason="clickhouse mutating command",
                command_family="clickhouse",
                executor_type="privileged_sandbox_pod",
                executor_profile="toolbox-clickhouse-mutating",
                target_kind="clickhouse_cluster",
                target_identity=f"database:{database}",
            )
        return _build_command_meta(
            supported=False,
            command=normalized,
            reason="clickhouse query semantics are ambiguous",
            command_family="clickhouse",
            executor_type="sandbox_pod",
            executor_profile="toolbox-clickhouse-readonly",
            target_kind="clickhouse_cluster",
            target_identity=f"database:{database}",
        )

    if head == "openstack":
        significant_tokens: List[str] = []
        cursor = 1
        while cursor < len(parts):
            token = as_str(parts[cursor]).strip()
            lowered = token.lower()
            if not token:
                cursor += 1
                continue
            if lowered.startswith("-"):
                if "=" in lowered:
                    cursor += 1
                    continue
                if cursor + 1 < len(parts) and not as_str(parts[cursor + 1]).startswith("-"):
                    cursor += 2
                    continue
                cursor += 1
                continue
            significant_tokens.append(lowered)
            cursor += 1
        readonly_verbs = _catalog_token_set(
            ["openstack", "readonly_verbs"],
            ["list", "show"],
        )
        mutating_verbs = _catalog_token_set(
            ["openstack", "mutating_verbs"],
            [
                "create",
                "delete",
                "set",
                "unset",
                "add",
                "remove",
                "reboot",
                "pause",
                "unpause",
                "lock",
                "unlock",
                "start",
                "stop",
                "shelve",
                "unshelve",
                "migrate",
                "live-migrate",
                "resize",
                "confirm",
                "revert",
                "failover",
            ],
        )
        project_name = (
            _extract_option_value(parts, {"--os-project-name"})
            or as_str(os.getenv("OS_PROJECT_NAME"), "")
            or "default"
        )
        if any(token in readonly_verbs for token in significant_tokens):
            return _read_only_meta(
                command=normalized,
                reason="openstack read-only command",
                command_family="openstack",
                executor_type="sandbox_pod",
                executor_profile="toolbox-openstack-readonly",
                target_kind="openstack_project",
                target_identity=f"project:{project_name}",
            )
        if any(token in mutating_verbs for token in significant_tokens):
            return _mutating_meta(
                command=normalized,
                reason="openstack mutating command",
                command_family="openstack",
                executor_type="privileged_sandbox_pod",
                executor_profile="toolbox-openstack-mutating",
                target_kind="openstack_project",
                target_identity=f"project:{project_name}",
            )
        return _build_command_meta(
            supported=False,
            command=normalized,
            reason="openstack command semantics are ambiguous",
            command_family="openstack",
            executor_type="sandbox_pod",
            executor_profile="toolbox-openstack-readonly",
            target_kind="openstack_project",
            target_identity=f"project:{project_name}",
        )

    if head in {"psql", "postgres", "mysql", "mariadb"}:
        query_text = _extract_sql_query(parts, head=head)
        if head in {"psql", "postgres"}:
            database = (
                _extract_option_value(parts, {"-d", "--dbname"})
                or _extract_option_value(parts, {"--database"})
                or as_str(os.getenv("EXEC_DEFAULT_POSTGRES_DATABASE"), "default")
                or "default"
            )
            command_family = "postgres"
            target_kind = "postgres_cluster"
            readonly_profile = "toolbox-postgres-readonly"
            mutating_profile = "toolbox-postgres-mutating"
        else:
            database = (
                _extract_option_value(parts, {"-D", "--database"})
                or as_str(os.getenv("EXEC_DEFAULT_MYSQL_DATABASE"), "default")
                or "default"
            )
            command_family = "mysql"
            target_kind = "mysql_cluster"
            readonly_profile = "toolbox-mysql-readonly"
            mutating_profile = "toolbox-mysql-mutating"
        target_identity = f"database:{database}"
        if not query_text:
            return _build_command_meta(
                supported=False,
                command=normalized,
                reason=f"{head} command missing query payload",
                command_family=command_family,
                target_kind=target_kind,
                target_identity=target_identity,
            )
        compact_query = re.sub(r"\s+", " ", re.sub(r"[\"'`]", "", query_text)).strip().lower()
        readonly_prefixes = tuple(
            _catalog_token_set(
                ["sql_clients", "readonly_prefixes"],
                ["select", "show", "describe", "desc", "explain", "with"],
            )
        )
        mutating_prefixes = tuple(
            _catalog_token_set(
                ["sql_clients", "mutating_prefixes"],
                [
                    "insert",
                    "update",
                    "delete",
                    "alter",
                    "create",
                    "drop",
                    "truncate",
                    "grant",
                    "revoke",
                    "replace",
                    "merge",
                    "analyze",
                    "vacuum",
                ],
            )
        )
        if compact_query.startswith(readonly_prefixes):
            return _read_only_meta(
                command=normalized,
                reason=f"{head} read-only query",
                command_family=command_family,
                executor_type="sandbox_pod",
                executor_profile=readonly_profile,
                target_kind=target_kind,
                target_identity=target_identity,
            )
        if compact_query.startswith(mutating_prefixes):
            return _mutating_meta(
                command=normalized,
                reason=f"{head} mutating query",
                command_family=command_family,
                executor_type="privileged_sandbox_pod",
                executor_profile=mutating_profile,
                target_kind=target_kind,
                target_identity=target_identity,
            )
        return _build_command_meta(
            supported=False,
            command=normalized,
            reason=f"{head} query semantics are ambiguous",
            command_family=command_family,
            target_kind=target_kind,
            target_identity=target_identity,
        )

    if head in {"cat", "tail", "head", "grep", "rg", "awk", "jq", "ls", "echo", "pwd"}:
        return _read_only_meta(
            command=normalized,
            reason="read-only command",
            command_family="shell",
            executor_type="sandbox_pod",
            executor_profile="busybox-readonly",
            target_kind="runtime_workspace",
            target_identity="workspace:local",
        )

    if head == "sed":
        if "-i" in parts or any(as_str(item).startswith("--in-place") for item in parts):
            return _mutating_meta(
                command=normalized,
                reason="sed in-place edit command",
                command_family="shell",
                executor_type="privileged_sandbox_pod",
                executor_profile="busybox-mutating",
                target_kind="runtime_workspace",
                target_identity="workspace:local",
            )
        return _read_only_meta(
            command=normalized,
            reason="sed read-only transform",
            command_family="shell",
            executor_type="sandbox_pod",
            executor_profile="busybox-readonly",
            target_kind="runtime_workspace",
            target_identity="workspace:local",
        )

    if head == "helm":
        readonly_verbs = {"list", "status", "history", "get", "show", "search", "template", "lint", "version", "env"}
        verb = as_str(parts[1]).lower() if len(parts) > 1 else ""
        namespace = _extract_option_value(
            parts,
            {"-n", "--namespace"},
            as_str(os.getenv("EXEC_DEFAULT_K8S_NAMESPACE"), "default"),
        ) or "default"
        if verb in readonly_verbs:
            return _read_only_meta(
                command=normalized,
                reason="helm read-only command",
                command_family="helm",
                executor_type="sandbox_pod",
                executor_profile="toolbox-k8s-readonly",
                target_kind="k8s_cluster",
                target_identity=f"namespace:{namespace}",
            )
        return _mutating_meta(
            command=normalized,
            reason="helm release mutating command",
            command_family="helm",
            executor_type="privileged_sandbox_pod",
            executor_profile="toolbox-k8s-mutating",
            target_kind="k8s_cluster",
            target_identity=f"namespace:{namespace}",
        )

    if head in {"systemctl", "service"}:
        readonly_verbs = {"status", "is-active", "is-enabled", "show", "list-units", "list-unit-files", "cat"}
        verb = ""
        if head == "systemctl":
            verb = as_str(parts[1]).lower() if len(parts) > 1 else ""
        else:
            verb = as_str(parts[2]).lower() if len(parts) > 2 else ""
        if verb in readonly_verbs:
            return _read_only_meta(
                command=normalized,
                reason="host service inspection command",
                command_family="host_control",
                executor_type="ssh_gateway",
                executor_profile="host-ssh-readonly",
                target_kind="host_node",
                target_identity=as_str(os.getenv("EXEC_DEFAULT_HOST_TARGET"), "host:primary"),
            )
        return _mutating_meta(
            command=normalized,
            reason="host service mutating command",
            command_family="host_control",
            executor_type="ssh_gateway",
            executor_profile="host-ssh-mutating",
            target_kind="host_node",
            target_identity=as_str(os.getenv("EXEC_DEFAULT_HOST_TARGET"), "host:primary"),
        )

    if head == "curl":
        lowered = normalized.lower()
        method = "get"
        write_markers = (
            "--request post",
            "--request put",
            "--request patch",
            "--request delete",
            "-x post",
            "-x put",
            "-x patch",
            "-x delete",
            "--data",
            "--data-raw",
            "--data-binary",
            "-d ",
        )
        if any(marker in lowered for marker in write_markers):
            method = "write"
        target_url = next((token for token in parts[1:] if as_str(token).startswith(("http://", "https://"))), "")
        target_identity = target_url or "http:unknown"
        if method == "get":
            return _read_only_meta(
                command=normalized,
                reason="curl GET-like query",
                command_family="http",
                executor_type="sandbox_pod",
                executor_profile="toolbox-http-readonly",
                target_kind="http_endpoint",
                target_identity=target_identity,
            )
        return _mutating_meta(
            command=normalized,
            reason="curl state-changing request",
            command_family="http",
            executor_type="external_control_plane",
            executor_profile="toolbox-http-mutating",
            target_kind="http_endpoint",
            target_identity=target_identity,
        )

    if permissive:
        return _permissive_elevation_meta(
            normalized,
            "test permissive bypass unsupported command type",
        )
    return _build_command_meta(
        supported=False,
        command=normalized,
        reason="unsupported command type",
    )


def classify_command_with_auto_rewrite(command: str) -> Dict[str, Any]:
    primary = classify_command(command)
    normalized_original = normalize_command(command)
    result = {
        **primary,
        "rewrite_applied": False,
        "original_command": normalized_original,
        "rewrite_attempts": [],
    }
    if bool(primary.get("supported")):
        return result

    reason = as_str(primary.get("reason")).lower()
    retryable_reason_fragments = (
        "command parse failed",
        "command head not allowed",
        "unsupported command type",
        "command contains blocked fragments/operators",
        "command is empty",
        "empty command tokens",
    )
    if not any(fragment in reason for fragment in retryable_reason_fragments):
        return result

    rewrite = rewrite_unknown_command(command)
    candidate = as_str(rewrite.get("candidate")).strip()
    result["rewrite_attempts"] = [
        {
            "candidate": candidate,
            "steps": rewrite.get("steps") if isinstance(rewrite.get("steps"), list) else [],
        }
    ]
    if not candidate or candidate == normalized_original:
        return result

    attempt = classify_command(candidate)
    if bool(attempt.get("supported")):
        return {
            **attempt,
            "rewrite_applied": True,
            "original_command": normalized_original,
            "rewrite_reason": "auto_rewrite_unknown_or_malformed",
            "rewrite_attempts": result["rewrite_attempts"][:AUTO_REWRITE_MAX_ATTEMPTS],
            "reason": f"{as_str(attempt.get('reason'))}; auto rewritten from malformed/unknown command",
        }

    return result


def _is_kubectl_output_value_allowed(value: str) -> bool:
    normalized = as_str(value).strip().lower()
    if not normalized:
        return False
    if normalized in {"json", "yaml", "wide", "name"}:
        return True
    if normalized.startswith("jsonpath=") or normalized.startswith("jsonpath-as-json="):
        return True
    if normalized.startswith("custom-columns="):
        return True
    if normalized.startswith("go-template="):
        return True
    if normalized.startswith("go-template-file="):
        return True
    return False


def _extract_kubectl_positionals(parts: List[str]) -> List[str]:
    positionals: List[str] = []
    cursor = 1
    while cursor < len(parts):
        token = as_str(parts[cursor]).strip()
        if not token:
            cursor += 1
            continue
        lowered = token.lower()
        if lowered.startswith("-"):
            next_cursor = _consume_kubectl_flag(parts, cursor)
            cursor = next_cursor if next_cursor > cursor else cursor + 1
            continue
        positionals.append(token)
        cursor += 1
    return positionals


def _validate_kubectl_query_template(parts: List[str]) -> tuple[bool, str]:
    positionals = _extract_kubectl_positionals(parts)
    if not positionals:
        return False, "kubectl query missing verb"
    verb = as_str(positionals[0]).strip().lower()
    sub_verb = as_str(positionals[1]).strip().lower() if len(positionals) > 1 else ""
    query_verbs = _catalog_token_set(
        ["kubernetes", "query_whitelist", "verbs"],
        [
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
            "rollout",
        ],
    )
    rollout_query_subverbs = _catalog_token_set(
        ["kubernetes", "query_whitelist", "rollout_subverbs"],
        ["status", "history"],
    )

    if verb == "exec":
        return False, "kubectl exec must go through approval"

    if verb == "rollout" and sub_verb not in rollout_query_subverbs:
        return False, "kubectl rollout query only allows status/history"

    # Validate flag templates/values for query whitelist.
    has_selector = False
    cursor = 1
    while cursor < len(parts):
        token = as_str(parts[cursor]).strip()
        if not token:
            cursor += 1
            continue
        lowered = token.lower()
        if not lowered.startswith("-"):
            cursor += 1
            continue

        if lowered in KUBECTL_QUERY_ALLOWED_BOOLEAN_FLAGS:
            cursor += 1
            continue

        if "=" in lowered:
            flag, value = lowered.split("=", 1)
            if flag not in KUBECTL_QUERY_ALLOWED_FLAGS_WITH_VALUE:
                return False, f"kubectl query flag not whitelisted: {flag}"
            if flag in {"-l", "--selector"} and as_str(value).strip():
                has_selector = True
            if flag in {"-o", "--output"} and not _is_kubectl_output_value_allowed(value):
                return False, f"kubectl output format not whitelisted: {value}"
            cursor += 1
            continue

        if lowered in KUBECTL_QUERY_ALLOWED_FLAGS_WITH_VALUE:
            if cursor + 1 >= len(parts):
                return False, f"kubectl query flag missing value: {token}"
            value = as_str(parts[cursor + 1]).strip()
            if lowered in {"-l", "--selector"} and value:
                has_selector = True
            if lowered in {"-o", "--output"} and not _is_kubectl_output_value_allowed(value):
                return False, f"kubectl output format not whitelisted: {value}"
            cursor += 2
            continue

        return False, f"kubectl query flag not whitelisted: {token}"

    if verb in {"version", "cluster-info", "api-resources", "api-versions"}:
        return True, "kubectl readonly template matched"
    if verb == "rollout":
        return len(positionals) >= 3, "kubectl rollout query missing target resource"
    if verb == "top":
        return len(positionals) >= 3, "kubectl top query missing target"
    if verb == "logs":
        if len(positionals) >= 2 or has_selector:
            return True, "kubectl readonly template matched"
        return False, "kubectl logs query missing resource target"
    if verb in {"get", "describe", "events", "wait", "explain"}:
        return len(positionals) >= 2, f"kubectl {verb} query missing resource target"
    if verb in query_verbs:
        return len(positionals) >= 2, f"kubectl {verb} query missing resource target"
    return False, f"kubectl query verb not in whitelist: {verb}"


def _validate_clickhouse_query_template(parts: List[str]) -> tuple[bool, str]:
    query_text = _extract_clickhouse_query(parts)
    if not query_text:
        return False, "clickhouse query missing --query/-q"
    normalized_query = as_str(query_text).strip()
    if not normalized_query:
        return False, "clickhouse query is empty"
    stripped_for_semicolon = normalized_query.rstrip().rstrip(";")
    if ";" in stripped_for_semicolon:
        return False, "clickhouse query contains multi-statement separator"

    compact = re.sub(r"\s+", " ", re.sub(r"[`\"]", "", normalized_query)).strip().lower()
    if compact.startswith(("show create table ", "describe table ", "desc table ", "explain table ")):
        table_ref = compact.split(" ", 3)[-1] if " " in compact else ""
        if not table_ref:
            return False, "clickhouse query missing table identifier"
        return True, "clickhouse readonly template matched"
    if compact.startswith(("select ", "show ", "describe ", "desc ", "explain ", "with ")):
        return True, "clickhouse readonly template matched"
    return False, "clickhouse readonly template not matched"


def _validate_openstack_query_template(parts: List[str]) -> tuple[bool, str]:
    significant_tokens: List[str] = []
    cursor = 1
    while cursor < len(parts):
        token = as_str(parts[cursor]).strip()
        lowered = token.lower()
        if not token:
            cursor += 1
            continue
        if lowered.startswith("-"):
            if "=" in lowered:
                cursor += 1
                continue
            if cursor + 1 < len(parts) and not as_str(parts[cursor + 1]).startswith("-"):
                cursor += 2
                continue
            cursor += 1
            continue
        significant_tokens.append(lowered)
        cursor += 1
    readonly_verbs = _catalog_token_set(
        ["openstack", "readonly_verbs"],
        ["list", "show"],
    )
    mutating_verbs = _catalog_token_set(
        ["openstack", "mutating_verbs"],
        [
            "create",
            "delete",
            "set",
            "unset",
            "add",
            "remove",
            "reboot",
            "pause",
            "unpause",
            "lock",
            "unlock",
            "start",
            "stop",
            "shelve",
            "unshelve",
            "migrate",
            "live-migrate",
            "resize",
            "confirm",
            "revert",
            "failover",
        ],
    )
    if any(token in mutating_verbs for token in significant_tokens):
        return False, "openstack mutating command must go through approval"
    if any(token in readonly_verbs for token in significant_tokens):
        return True, "openstack readonly template matched"
    return False, "openstack readonly template not matched"


def _validate_sql_client_query_template(parts: List[str], *, head: str) -> tuple[bool, str]:
    query_text = _extract_sql_query(parts, head=head)
    if not query_text:
        return False, f"{head} query missing -c/-e payload"
    normalized_query = as_str(query_text).strip()
    if not normalized_query:
        return False, f"{head} query is empty"
    stripped_for_semicolon = normalized_query.rstrip().rstrip(";")
    if ";" in stripped_for_semicolon:
        return False, f"{head} query contains multi-statement separator"
    compact = re.sub(r"\s+", " ", re.sub(r"[`\"]", "", normalized_query)).strip().lower()
    readonly_prefixes = tuple(
        _catalog_token_set(
            ["sql_clients", "readonly_prefixes"],
            ["select", "show", "describe", "desc", "explain", "with"],
        )
    )
    if compact.startswith(tuple(f"{prefix} " for prefix in readonly_prefixes)):
        return True, f"{head} readonly template matched"
    if compact in readonly_prefixes:
        return True, f"{head} readonly template matched"
    return False, f"{head} readonly template not matched"


def evaluate_query_whitelist(command: str, command_meta: Dict[str, Any]) -> Dict[str, Any]:
    safe_meta = command_meta if isinstance(command_meta, dict) else {}
    if not bool(safe_meta.get("supported")):
        return {"whitelisted": False, "reason": "unsupported command cannot be whitelisted"}
    if as_str(safe_meta.get("command_type")).lower() != "query":
        return {"whitelisted": False, "reason": "only query commands are eligible for no-approval whitelist"}

    normalized = normalize_command(command)
    if not normalized:
        return {"whitelisted": False, "reason": "empty command"}
    parsed = parse_command_segments(normalized)
    if not bool(parsed.get("ok")):
        return {"whitelisted": False, "reason": "command parse failed"}
    segments = parsed.get("segments") if isinstance(parsed.get("segments"), list) else []
    operators = [as_str(item) for item in (parsed.get("operators") if isinstance(parsed.get("operators"), list) else [])]
    if any(item not in {"|", "|&"} for item in operators):
        return {"whitelisted": False, "reason": "only pipeline chaining is eligible for no-approval whitelist"}
    if not segments:
        return {"whitelisted": False, "reason": "empty command segments"}

    for segment in segments:
        if not isinstance(segment, list) or not segment:
            return {"whitelisted": False, "reason": "invalid command segment"}
        parts = [as_str(item) for item in segment]
        head = as_str(parts[0]).strip().lower()

        if head == "kubectl":
            ok, reason = _validate_kubectl_query_template(parts)
            if not ok:
                return {"whitelisted": False, "reason": reason}
            continue

        if head in {"clickhouse-client", "clickhouse"}:
            ok, reason = _validate_clickhouse_query_template(parts)
            if not ok:
                return {"whitelisted": False, "reason": reason}
            continue

        if head == "openstack":
            ok, reason = _validate_openstack_query_template(parts)
            if not ok:
                return {"whitelisted": False, "reason": reason}
            continue

        if head in {"psql", "postgres", "mysql", "mariadb"}:
            ok, reason = _validate_sql_client_query_template(parts, head=head)
            if not ok:
                return {"whitelisted": False, "reason": reason}
            continue

        if head in {"rg", "grep", "cat", "tail", "head", "jq", "ls", "echo", "pwd"}:
            continue

        return {"whitelisted": False, "reason": f"query head not in whitelist templates: {head}"}

    return {"whitelisted": True, "reason": "query command matches whitelist template constraints"}
