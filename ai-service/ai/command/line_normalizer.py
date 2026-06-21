"""
Command line text processing utilities.

Portable replacements for ``followup_command`` text-processing functions.
"""
from __future__ import annotations

import os
import re
import shlex
from typing import List, Tuple


# ── Constants ──────────────────────────────────────────────────────────────

# Command heads that are allowed for diagnostic execution
ALLOWED_HEADS: Tuple[str, ...] = (
    "kubectl", "helm",
    "clickhouse-client", "clickhouse",
    "curl", "wget",
    "grep", "rg", "cat", "tail", "head", "awk", "sed", "jq", "sort", "uniq",
    "ls", "echo", "printf", "pwd", "env", "id", "whoami", "hostname",
    "pgrep", "pidof", "ps", "ss", "netstat", "lsof",
    "df", "du", "stat", "free", "uptime", "uname",
    "top", "htop", "vmstat", "iostat", "mpstat",
    "find", "xargs", "wc",
    "systemctl", "service", "journalctl", "dmesg",
    "timedatectl", "hostnamectl", "nslookup", "dig", "ping", "traceroute",
    "openstack", "nova", "neutron", "cinder", "glance", "keystone",
    "psql", "postgres", "mysql", "mariadb",
    "timeout", "nice", "nohup",
)
REPAIR_HEADS: Tuple[str, ...] = (
    "kubectl", "clickhouse-client", "clickhouse", "helm",
    "grep", "rg", "head", "tail", "sort", "awk", "sed",
)
CHAIN_OPERATORS: set[str] = {"|", "||", "&&"}
BLOCKED_OPERATORS: set[str] = {";", "|&", ">", ">>", "<", "<<", "|||"}

_CLICKHOUSE_SQL_MULTIWORD_KEYWORD_REPAIRS: Tuple[Tuple[str, str], ...] = (
    (r"(?i)\bLEFT\s+JOIN\b", "LEFT JOIN"),
    (r"(?i)\bRIGHT\s+JOIN\b", "RIGHT JOIN"),
    (r"(?i)\bINNER\s+JOIN\b", "INNER JOIN"),
    (r"(?i)\bOUTER\s+JOIN\b", "OUTER JOIN"),
    (r"(?i)\bCROSS\s+JOIN\b", "CROSS JOIN"),
    (r"(?i)\bFULL\s+JOIN\b", "FULL JOIN"),
    (r"(?i)\bGROUP\s+BY\b", "GROUP BY"),
    (r"(?i)\bORDER\s+BY\b", "ORDER BY"),
    (r"(?i)\bPARTITION\s+BY\b", "PARTITION BY"),
    (r"(?i)\bLEFT\s+SEMI\s+JOIN\b", "LEFT SEMI JOIN"),
    (r"(?i)\bLEFT\s+ANTI\s+JOIN\b", "LEFT ANTI JOIN"),
    (r"(?i)\bRIGHT\s+SEMI\s+JOIN\b", "RIGHT SEMI JOIN"),
)
_CLICKHOUSE_SQL_SPACE_REQUIRED_KEYWORDS: Tuple[str, ...] = (
    "SELECT", "FROM", "WHERE", "AND", "OR", "NOT", "IN", "ON",
    "AS", "WHEN", "THEN", "ELSE", "END", "CASE",
    "LIMIT", "OFFSET", "FORMAT", "UNION", "ALL", "DISTINCT",
    "HAVING", "PREWHERE", "ARRAY", "JOIN", "GLOBAL",
    "FINAL", "SETTINGS", "WITH", "TOTALS",
)


# ── Helpers ────────────────────────────────────────────────────────────────


def _as_str(value, default: str = "") -> str:
    if value is None:
        return default
    if isinstance(value, str):
        return value
    return str(value)


def is_truthy_env(name: str, default: bool = False) -> bool:
    """Check if an environment variable is set to a truthy value."""
    raw = _as_str(os.getenv(name))
    if not raw:
        return default
    return raw.strip().lower() in {"1", "true", "yes", "on"}


# ── Whitespace / text processing ───────────────────────────────────────────


def collapse_unquoted_whitespace(text: str) -> str:
    """Collapse whitespace outside quotes without altering quoted args."""
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


# ── ClickHouse query text repair ───────────────────────────────────────────


def repair_clickhouse_query_text(query_text: str) -> str:
    """Normalise SQL keywords and spacing in a ClickHouse query string."""
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
    return collapse_unquoted_whitespace(repaired)


# ── Command-line normalisation ─────────────────────────────────────────────


def normalize_command_line(line: str) -> str:
    """Normalize a raw command line string.

    Strips backticks, list markers, prompt prefixes, and repairs spacing.
    """
    normalized = _as_str(line).strip()
    if normalized.startswith("`") and normalized.endswith("`") and len(normalized) > 2:
        normalized = normalized[1:-1].strip()
    normalized = re.sub(r"^\s*(?:[-*•]\s+|\d+\.\s+)", "", normalized)
    normalized = re.sub(r"^\s*P\d+\s+", "", normalized, flags=re.IGNORECASE)
    normalized = re.sub(r"^\s*(?:执行命令|命令)\s*[:：]\s*", "", normalized)
    if normalized.startswith("$"):
        normalized = normalized[1:].strip()

    # Repair glued head-flag patterns (e.g. kubectl-nislaplogs)
    first_token = normalized.split(maxsplit=1)[0].lower() if normalized else ""
    if first_token not in ALLOWED_HEADS:
        for head in REPAIR_HEADS:
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


def _repair_clickhouse_query_spacing(command: str) -> str:
    """Repair ClickHouse query spacing inside a kubectl command."""
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
        return f"{prefix}{quote}{repair_clickhouse_query_text(body)}{quote}"

    def _replace_unquoted(match: re.Match[str]) -> str:
        prefix = _as_str(match.group("prefix"))
        body = _as_str(match.group("body"))
        return f"{prefix}{repair_clickhouse_query_text(body)}"

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


def _repair_command_spacing(command: str) -> str:
    """Repair common command spacing issues."""
    text = _as_str(command).strip()
    if not text:
        return ""
    for head in REPAIR_HEADS:
        text = re.sub(
            rf"(?i)(?<=\b{re.escape(head)})(?=[a-z][-a-z0-9.]*)(?!\s)",
            " ",
            text,
        )
    text = re.sub(r"\s+", " ", text)
    text = re.sub(r"(?i)(\bexec\b)(?=[a-z][-a-z0-9.]*)", r"\1 ", text)
    text = re.sub(r"\|(\s*)(cat|head|tail|grep|awk|sort|uniq|wc)(?=[a-z][-a-z])", r"|\1\2 ", text)
    text = re.sub(r"(?i)(\bread\b)\s*-([a-z0-9])", r"\1 -\2", text)
    text = re.sub(r"\s+", " ", text).strip()
    # Clean up tool prefix in command text
    text = re.sub(r"(?i)tool[:\s]+generic_exec\s*", "", text).strip()
    return text


def normalize_command_match_key(command: str) -> str:
    """Generate a stable match key for command deduplication."""
    normalized = normalize_command_line(command)
    if not normalized:
        return ""
    try:
        tokens = shlex.split(normalized)
    except Exception:
        return re.sub(r"\s+", " ", normalized).strip()
    if not tokens:
        return ""
    return "\x1f".join(tokens)


def looks_like_command(text: str) -> bool:
    """Check if text looks like an executable command."""
    candidate = normalize_command_line(text)
    if not candidate:
        return False
    head = _as_str(candidate.split(" ", 1)[0]).lower()
    if head not in ALLOWED_HEADS:
        return False
    return bool(re.match(r"^[A-Za-z0-9_.\-]+(?:\s+.+)?$", candidate))


def extract_commands_from_text(
    content: str,
    limit: int = 12,
) -> List[str]:
    """Extract shell commands from free-text content (fenced blocks, inline)."""
    text = _as_str(content)
    if not text:
        return []

    commands: List[str] = []
    seen: set[str] = set()
    fence_pattern = re.compile(r"```(?:bash|sh|shell|zsh)?\s*([\s\S]*?)```", re.IGNORECASE)
    inline_pattern = re.compile(r"`([^`\n]+)`")

    def _append(candidate: str) -> None:
        normalized = normalize_command_line(candidate)
        if not normalized or normalized.startswith("#"):
            return
        if not looks_like_command(normalized):
            return
        if normalized in seen:
            return
        seen.add(normalized)
        commands.append(normalized)

    for block in fence_pattern.findall(text):
        for line in _as_str(block).splitlines():
            _append(line)
            if len(commands) >= limit:
                return commands

    for inline in inline_pattern.findall(text):
        _append(inline)
        if len(commands) >= limit:
            return commands

    for line in text.splitlines():
        stripped = line.strip()
        if stripped.startswith("$") or looks_like_command(stripped):
            _append(line)
            if len(commands) >= limit:
                return commands

    return commands


def normalize_command_loose_match_key(command: str) -> str:
    """Generate a loose match key that handles operator equivalences."""
    normalized = normalize_command_line(command)
    if not normalized:
        return ""
    try:
        import shlex as _shlex
        lexer = _shlex.shlex(normalized, posix=True, punctuation_chars="|&;<>")
        lexer.whitespace_split = True
        lexer.commenters = ""
        raw_tokens = [_as_str(token).strip() for token in lexer]
    except Exception:
        return ""

    tokens: List[str] = []
    all_operators = CHAIN_OPERATORS.union(BLOCKED_OPERATORS)
    for token in raw_tokens:
        if not token:
            continue
        if token in all_operators:
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
            and tokens[index] not in all_operators
            and tokens[index + 1] in CHAIN_OPERATORS
            and tokens[index + 2] not in all_operators
        ):
            merged.append(f"{tokens[index]}{tokens[index + 1]}{tokens[index + 2]}")
            index += 3
            continue
        merged.append(tokens[index])
        index += 1
    return "\x1f".join(merged)


__all__ = [
    "ALLOWED_HEADS",
    "REPAIR_HEADS",
    "CHAIN_OPERATORS",
    "BLOCKED_OPERATORS",
    "normalize_command_line",
    "normalize_command_match_key",
    "normalize_command_loose_match_key",
    "repair_clickhouse_query_text",
    "collapse_unquoted_whitespace",
    "looks_like_command",
    "extract_commands_from_text",
    "is_truthy_env",
]
