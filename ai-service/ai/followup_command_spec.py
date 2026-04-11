"""
Structured follow-up command spec helpers.

Root-cause fix for command spacing issues:
- AI outputs structured command spec (tool + params)
- backend compiles spec to deterministic shell command
- avoid relying on free-text shell spacing repairs
"""

from __future__ import annotations

import json
import os
import re
import shlex
import subprocess
from typing import Any, Dict


_K8S_NAMESPACE_PATTERN = re.compile(r"^[a-z0-9]([-a-z0-9]*[a-z0-9])?$")
_SUPPORTED_TOOLS = {"kubectl_clickhouse_query", "k8s_clickhouse_query", "clickhouse_query", "generic_exec"}
_CLICKHOUSE_TARGET_IDENTITY_PATTERN = re.compile(r"^database:[A-Za-z0-9_.-]+$")
_GENERIC_EXEC_BLOCKED_TOKENS = {
    "|",
    "|&",
    "||",
    "&&",
    ";",
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
_CLICKHOUSE_READONLY_PREFIXES = ("select", "show", "describe", "explain")
_SQL_KEYWORDS_REQUIRE_SPACE_AFTER = (
    "SELECT",
    "FROM",
    "WHERE",
    "PREWHERE",
    "GROUP",
    "ORDER",
    "LIMIT",
    "OFFSET",
    "HAVING",
    "JOIN",
    "UNION",
    "EXPLAIN",
    "DESCRIBE",
    "SHOW",
    "WITH",
    "INTERVAL",
)
_SQL_KEYWORDS_REQUIRE_SPACE_BEFORE = (
    "FROM",
    "WHERE",
    "PREWHERE",
    "GROUP",
    "ORDER",
    "LIMIT",
    "OFFSET",
    "HAVING",
    "JOIN",
    "UNION",
)
_SQL_COMPACT_MULTIWORD_REPAIRS = (
    ("EXPLAINPIPELINE", "EXPLAIN PIPELINE"),
    ("SHOWCREATETABLE", "SHOW CREATE TABLE"),
    ("DESCRIBETABLE", "DESCRIBE TABLE"),
    ("EXPLAINTABLE", "EXPLAIN TABLE"),
    ("ORDERBY", "ORDER BY"),
    ("GROUPBY", "GROUP BY"),
)
_GENERIC_EXEC_TARGET_KIND_PATTERN = re.compile(r"^[a-z][a-z0-9_:-]{1,63}$")
_GENERIC_EXEC_ALLOWED_HEADS = {
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
    "openstack",
    "psql",
    "postgres",
    "mysql",
    "mariadb",
    "timeout",
    "ps",
    "ss",
}
_GENERIC_EXEC_ALLOWED_HEAD_PREFIXES = tuple(
    sorted(_GENERIC_EXEC_ALLOWED_HEADS, key=len, reverse=True)
)
_KUBECTL_COMPACT_VERB_EXPANSIONS: dict[str, list[str]] = {
    "getpods": ["get", "pods"],
    "getsvc": ["get", "svc"],
    "getservices": ["get", "services"],
    "describepod": ["describe", "pod"],
    "describepods": ["describe", "pods"],
}
_KUBECTL_FLAGS_WITH_VALUE = {
    "-n",
    "--namespace",
    "-l",
    "--selector",
    "--field-selector",
    "-o",
    "--output",
    "-c",
    "--container",
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

_FOLLOWUP_REASON_GROUP_MAP: dict[str, str] = {
    "glued_command_tokens": "GLUE_SYNTAX",
    "glued_sql_tokens": "GLUE_SYNTAX",
    "invalid_kubectl_token": "GLUE_K8S_TOKEN",
    "suspicious_selector_namespace_glue": "GLUE_K8S_TOKEN",
    "missing_or_invalid_command_spec": "SPEC_MISSING",
    "missing_structured_spec": "SPEC_MISSING",
    "missing_target_identity": "SPEC_MISSING",
    "answer_command_requires_structured_action": "SPEC_MISSING",
    "no_executable_query_candidates": "SPEC_MISSING",
    "semantic_incomplete": "SPEC_MISSING",
    "target_kind_mismatch": "SPEC_MISSING",
    "target_identity_mismatch": "SPEC_MISSING",
    "missing_namespace_for_k8s_clickhouse_query": "SPEC_MISSING",
    "missing_pod_name_for_k8s_clickhouse_query": "SPEC_MISSING",
    "pod_name_resolution_failed": "SPEC_MISSING",
    "unsupported_command_head": "SECURITY_GUARD",
    "clickhouse_multi_statement_not_allowed": "SECURITY_GUARD",
    "unsupported_clickhouse_readonly_query": "SECURITY_GUARD",
    "pod_selector_requires_shell": "SECURITY_GUARD",
}

_FOLLOWUP_REASON_CODE_ALIASES: dict[str, str] = {
    "command_spec is empty": "missing_or_invalid_command_spec",
    "command is required in command_spec.args": "missing_or_invalid_command_spec",
    "invalid command text in command_spec.args.command": "missing_or_invalid_command_spec",
    "command_argv is empty": "missing_or_invalid_command_spec",
    "command_argv exceeds max length(64)": "missing_or_invalid_command_spec",
    "invalid namespace in command_spec": "missing_or_invalid_command_spec",
    "query is required in command_spec.args": "missing_or_invalid_command_spec",
    "query is too long in command_spec": "missing_or_invalid_command_spec",
    "namespace is required when pod_selector is provided": "missing_namespace_for_k8s_clickhouse_query",
    "namespace is required when pod_name is provided": "missing_namespace_for_k8s_clickhouse_query",
    "target_identity must look like database:<name>": "missing_target_identity",
    "command_argv contains blocked fragments": "unsupported_command_head",
    "command_argv contains blocked shell operators": "unsupported_command_head",
    "command_argv contains shell substitution": "unsupported_command_head",
    "pod_selector contains unsafe characters": "invalid_kubectl_token",
    "pod_name contains unsafe characters": "invalid_kubectl_token",
}

_FOLLOWUP_REASON_PREFIX_ALIASES: dict[str, str] = {
    "unsupported command_spec tool:": "missing_or_invalid_command_spec",
    "unsupported target_kind in command_spec:": "missing_or_invalid_command_spec",
}


def _extract_cli_flag_value(command_argv: list[str], *, short_flag: str = "", long_flag: str = "") -> str:
    if not command_argv:
        return ""
    safe_short = _as_str(short_flag).strip()
    safe_long = _as_str(long_flag).strip()
    for index, token in enumerate(command_argv):
        current = _as_str(token).strip()
        if not current:
            continue
        if safe_short and current == safe_short:
            if index + 1 < len(command_argv):
                candidate = _as_str(command_argv[index + 1]).strip()
                if candidate and not candidate.startswith("-"):
                    return candidate
            continue
        if safe_long and current == safe_long:
            if index + 1 < len(command_argv):
                candidate = _as_str(command_argv[index + 1]).strip()
                if candidate and not candidate.startswith("-"):
                    return candidate
            continue
        if safe_long and current.startswith(f"{safe_long}="):
            return _as_str(current.split("=", 1)[1]).strip()
        if safe_short and current.startswith(safe_short) and len(current) > len(safe_short):
            compact_value = _as_str(current[len(safe_short):]).strip()
            if compact_value and not compact_value.startswith("-"):
                return compact_value
    return ""


def normalize_followup_reason_code(reason: Any) -> str:
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


def map_followup_reason_group(reason: Any) -> str:
    normalized = normalize_followup_reason_code(reason)
    return _FOLLOWUP_REASON_GROUP_MAP.get(normalized, "OTHER")


def _extract_namespace_from_target_identity(target_identity: Any) -> str:
    safe_identity = _as_str(target_identity).strip().lower()
    if not safe_identity.startswith("namespace:"):
        return ""
    namespace = _as_str(safe_identity.split(":", 1)[1]).strip().lower()
    if namespace and _K8S_NAMESPACE_PATTERN.match(namespace):
        return namespace
    return ""


def _repair_kubectl_selector_namespace_glue(
    command_argv: list[str],
    *,
    namespace_hint: str = "",
) -> tuple[list[str], bool, str]:
    if not command_argv or _as_str(command_argv[0]).strip().lower() != "kubectl":
        return command_argv, False, ""

    namespace_in_argv = _extract_cli_flag_value(command_argv, short_flag="-n", long_flag="--namespace").strip().lower()
    effective_namespace = namespace_in_argv
    if not effective_namespace:
        safe_hint = _as_str(namespace_hint).strip().lower()
        if safe_hint and _K8S_NAMESPACE_PATTERN.match(safe_hint):
            effective_namespace = safe_hint

    selector = _extract_cli_flag_value(command_argv, short_flag="-l", long_flag="--selector").strip()
    if not selector:
        return command_argv, False, ""

    suffix = f"-n{effective_namespace}" if effective_namespace else ""
    selector_parts = [item.strip() for item in selector.split(",") if item.strip()]
    if not selector_parts:
        return command_argv, False, ""

    inferred_namespace = ""
    if not effective_namespace:
        for part in selector_parts:
            if "=" not in part:
                continue
            _, value = part.split("=", 1)
            suffix_match = re.search(
                r"(?i)-n([a-z0-9]([-a-z0-9]*[a-z0-9])?)$",
                _as_str(value).strip(),
            )
            if not suffix_match:
                continue
            candidate = _as_str(suffix_match.group(1)).strip().lower()
            if candidate and _K8S_NAMESPACE_PATTERN.match(candidate):
                inferred_namespace = candidate
                effective_namespace = candidate
                suffix = f"-n{effective_namespace}"
                break

    if not effective_namespace:
        return command_argv, False, ""

    repaired_parts: list[str] = []
    changed = False
    for part in selector_parts:
        if "=" not in part:
            repaired_parts.append(part)
            continue
        key, value = part.split("=", 1)
        safe_key = key.strip()
        safe_value = value.strip()
        lowered_value = safe_value.lower()
        if lowered_value.endswith(suffix) and len(safe_value) > len(suffix):
            candidate = safe_value[: -len(suffix)].rstrip("-")
            if candidate:
                safe_value = candidate
                changed = True
        repaired_parts.append(f"{safe_key}={safe_value}")

    if not changed:
        return command_argv, False, ""

    repaired_selector = ",".join(repaired_parts)
    repaired_argv: list[str] = []
    index = 0
    selector_replaced = False
    while index < len(command_argv):
        token = _as_str(command_argv[index]).strip()
        if token in {"-l", "--selector"}:
            repaired_argv.append(token)
            if index + 1 < len(command_argv):
                next_value = _as_str(command_argv[index + 1]).strip()
                repaired_argv.append(repaired_selector if not selector_replaced else next_value)
                selector_replaced = True
                index += 2
                continue
            index += 1
            continue
        if token.startswith("--selector="):
            if not selector_replaced:
                repaired_argv.append(f"--selector={repaired_selector}")
                selector_replaced = True
            else:
                repaired_argv.append(token)
            index += 1
            continue
        repaired_argv.append(token)
        index += 1

    if not namespace_in_argv and effective_namespace:
        repaired_argv.extend(["-n", effective_namespace])
    return repaired_argv, True, repaired_selector


def _infer_generic_exec_target(command_argv: list[str]) -> tuple[str, str, bool]:
    if not command_argv:
        return "", "", False

    head = _as_str(command_argv[0]).strip().lower()
    if not head:
        return "", "", False

    if head == "kubectl":
        kubectl_scope_argv = command_argv
        if "--" in command_argv:
            kubectl_scope_argv = command_argv[: command_argv.index("--")]
        namespace = _extract_cli_flag_value(kubectl_scope_argv, short_flag="-n", long_flag="--namespace").lower()
        if namespace and _K8S_NAMESPACE_PATTERN.match(namespace):
            return "k8s_cluster", f"namespace:{namespace}", True
        return "k8s_cluster", "cluster:kubernetes", False

    if head in {"clickhouse-client", "clickhouse"}:
        database = _extract_cli_flag_value(command_argv, short_flag="-d", long_flag="--database")
        if database and re.match(r"^[A-Za-z_][A-Za-z0-9_]*$", database):
            return "clickhouse_cluster", f"database:{database}", True
        return "clickhouse_cluster", "database:default", False

    if head == "openstack":
        cloud = _extract_cli_flag_value(command_argv, long_flag="--os-cloud")
        if cloud:
            return "openstack_cluster", f"cloud:{cloud}", True
        return "openstack_cluster", "cloud:default", False

    if head in {"psql", "postgres"}:
        database = (
            _extract_cli_flag_value(command_argv, short_flag="-d", long_flag="--dbname")
            or _extract_cli_flag_value(command_argv, long_flag="--database")
        )
        if database:
            return "postgres_cluster", f"database:{database}", True
        return "postgres_cluster", "database:default", False

    if head in {"mysql", "mariadb"}:
        database = _extract_cli_flag_value(command_argv, short_flag="-D", long_flag="--database")
        if database:
            return "mysql_cluster", f"database:{database}", True
        return "mysql_cluster", "database:default", False

    return "", "", False


def _as_str(value: Any, default: str = "") -> str:
    if value is None:
        return default
    if isinstance(value, str):
        return value
    return str(value)


def _collapse_spaces(text: str) -> str:
    return re.sub(r"\s+", " ", _as_str(text)).strip()


def _normalize_clickhouse_query_text(query_text: str) -> str:
    normalized = _as_str(query_text).replace("\r", " ").replace("\n", " ")
    normalized = re.sub(r"\s+", " ", normalized).strip()
    if not normalized:
        return ""
    return normalized


def _extract_sql_first_keyword(query_text: str) -> str:
    normalized = _normalize_clickhouse_query_text(query_text)
    if not normalized:
        return ""
    match = re.match(r"^\s*([A-Za-z]+)", normalized)
    if not match:
        return ""
    return _as_str(match.group(1)).lower()


def _detect_glued_clickhouse_keyword(query_text: str) -> str:
    normalized = _normalize_clickhouse_query_text(query_text)
    if not normalized:
        return ""

    for keyword in _SQL_KEYWORDS_REQUIRE_SPACE_AFTER:
        if re.search(rf"(?i)\b{keyword}(?=[A-Za-z0-9_(])", normalized):
            return keyword.lower()
    for keyword in _SQL_KEYWORDS_REQUIRE_SPACE_BEFORE:
        pattern = rf"(?i)(?<=[A-Za-z0-9\)]){keyword}\b"
        if keyword == "WHERE":
            pattern = rf"(?i)(?<=[A-Za-z0-9\)])(?<!PRE)WHERE\b"
        if re.search(pattern, normalized):
            return keyword.lower()
    if re.search(r"(?<=[A-Za-z0-9_\)])AS(?=[A-Za-z_])", normalized):
        return "as"
    if re.search(r"(?<=[a-z0-9_\)])as(?=[a-z_])", normalized):
        return "as"
    if re.search(r"(?i)\bINTERVAL\d+[A-Za-z_]+\b", normalized):
        return "interval"
    return ""


def _repair_clickhouse_query_spacing_for_suggestion(query_text: str) -> str:
    normalized = _normalize_clickhouse_query_text(query_text)
    if not normalized:
        return ""
    repaired = normalized
    for compact, expanded in _SQL_COMPACT_MULTIWORD_REPAIRS:
        repaired = re.sub(rf"(?i)\b{compact}\b", expanded, repaired)
    for keyword in _SQL_KEYWORDS_REQUIRE_SPACE_AFTER:
        repaired = re.sub(rf"(?i)\b{keyword}(?=[A-Za-z0-9_(])", f"{keyword} ", repaired)
    for keyword in _SQL_KEYWORDS_REQUIRE_SPACE_BEFORE:
        pattern = rf"(?i)(?<=[A-Za-z0-9\)]){keyword}\b"
        if keyword == "WHERE":
            pattern = rf"(?i)(?<=[A-Za-z0-9\)])(?<!PRE)WHERE\b"
        repaired = re.sub(pattern, f" {keyword}", repaired)
    repaired = re.sub(r"(?<=[A-Za-z0-9_\)])AS(?=[A-Za-z_])", " AS ", repaired)
    repaired = re.sub(r"(?<=[a-z0-9_\)])as(?=[a-z_])", " as ", repaired)
    repaired = re.sub(r"(?i)\bINTERVAL(\d+)([A-Za-z_]+)\b", r"INTERVAL \1 \2", repaired)
    return _normalize_clickhouse_query_text(repaired)


def _extract_clickhouse_database(query_text: str) -> str:
    normalized = _normalize_clickhouse_query_text(query_text)
    if not normalized:
        return ""
    patterns = (
        r"(?i)\b(?:FROM|JOIN|TABLE|DESCRIBE\s+TABLE|SHOW\s+CREATE\s+TABLE|INTO)\s+([A-Za-z_][A-Za-z0-9_]*)\.",
        r"(?i)\bUSE\s+([A-Za-z_][A-Za-z0-9_]*)\b",
    )
    for pattern in patterns:
        match = re.search(pattern, normalized)
        if match:
            return _as_str(match.group(1))
    return ""


def _as_positive_int(value: Any, default: int) -> int:
    try:
        parsed = int(value)
    except Exception:
        return int(default)
    if parsed <= 0:
        return int(default)
    return parsed


def _sql_preflight_enabled() -> bool:
    raw = _as_str(os.getenv("AI_RUNTIME_SQL_PREFLIGHT_ENABLED"), "true").strip().lower()
    if raw in {"0", "false", "no", "off"}:
        return False
    if os.getenv("PYTEST_CURRENT_TEST") is not None and raw in {"", "1", "true", "yes", "on"}:
        return False
    return True


def _shell_emergency_enabled() -> bool:
    raw = _as_str(os.getenv("AI_RUNTIME_SHELL_EMERGENCY_ENABLED"), "false").strip().lower()
    return raw in {"1", "true", "yes", "on"}


def _k8s_pod_autoresolve_enabled() -> bool:
    raw = _as_str(os.getenv("AI_RUNTIME_K8S_POD_AUTORESOLVE_ENABLED")).strip().lower()
    if raw in {"0", "false", "no", "off"}:
        return False
    if raw in {"1", "true", "yes", "on"}:
        return True
    return os.getenv("PYTEST_CURRENT_TEST") is None


def _resolve_clickhouse_pod_name(*, namespace: str, pod_selector: str, timeout_s: int) -> Dict[str, Any]:
    safe_namespace = _as_str(namespace).strip().lower()
    safe_selector = _collapse_spaces(_as_str(pod_selector))
    if not safe_namespace or not _K8S_NAMESPACE_PATTERN.match(safe_namespace):
        return {"ok": False, "reason": "invalid namespace", "detail": "namespace is invalid for pod lookup"}
    if not safe_selector:
        return {"ok": False, "reason": "pod_selector is required", "detail": "pod_selector is required for pod lookup"}
    if any(ch in safe_selector for ch in "'\"`$;&|<>"):
        return {"ok": False, "reason": "pod_selector contains unsafe characters", "detail": "pod_selector contains unsafe characters"}

    safe_timeout = max(3, min(30, _as_positive_int(timeout_s, 8)))
    try:
        pod_lookup = subprocess.run(
            [
                "kubectl",
                "-n",
                safe_namespace,
                "get",
                "pods",
                "-l",
                safe_selector,
                "-o",
                "jsonpath={.items[0].metadata.name}",
            ],
            capture_output=True,
            text=True,
            timeout=safe_timeout,
            check=False,
        )
    except subprocess.TimeoutExpired:
        return {
            "ok": False,
            "reason": "pod lookup timed out",
            "detail": f"pod lookup timed out ({safe_timeout}s)",
        }
    except Exception as exc:
        return {"ok": False, "reason": "pod lookup failed", "detail": _as_str(exc)}

    if int(pod_lookup.returncode) != 0:
        return {
            "ok": False,
            "reason": "pod lookup failed",
            "detail": _as_str(pod_lookup.stderr, "pod lookup failed"),
        }
    pod_name = _as_str(pod_lookup.stdout).strip()
    if not pod_name:
        return {
            "ok": False,
            "reason": "clickhouse pod not found",
            "detail": "no clickhouse pod matched selector",
        }
    return {"ok": True, "pod_name": pod_name, "pod_selector": safe_selector}


def _normalize_command_argv(raw: Any) -> list[str]:
    if isinstance(raw, list):
        values = [_as_str(item).strip() for item in raw]
        return [item for item in values if item]
    return []


def _detect_glued_command_head(head: str) -> str:
    safe_head = _as_str(head).strip().lower()
    if not safe_head:
        return ""
    for allowed_head in _GENERIC_EXEC_ALLOWED_HEAD_PREFIXES:
        if safe_head == allowed_head:
            return ""
        if safe_head.startswith(allowed_head):
            suffix = safe_head[len(allowed_head):]
            if suffix and re.match(r"^[a-z0-9._-]+$", suffix):
                return allowed_head
    return ""


def _canonicalize_kubectl_command_argv(command_argv: list[str]) -> list[str]:
    if not command_argv:
        return []
    canonical: list[str] = []
    for index, token in enumerate(command_argv):
        safe_token = _as_str(token).strip()
        if not safe_token:
            continue
        if index == 0:
            canonical.append("kubectl")
            continue
        lowered = safe_token.lower()
        if lowered in _KUBECTL_COMPACT_VERB_EXPANSIONS:
            canonical.extend(_KUBECTL_COMPACT_VERB_EXPANSIONS[lowered])
            continue
        if lowered.startswith("-n") and safe_token != "-n":
            value = safe_token[2:].strip()
            if value and not value.startswith("-"):
                canonical.extend(["-n", value])
                continue
        if lowered.startswith("-l") and safe_token != "-l":
            value = safe_token[2:].strip()
            if value and not value.startswith("-"):
                canonical.extend(["-l", value])
                continue
        if lowered.startswith("-o") and safe_token != "-o":
            value = safe_token[2:].strip()
            if value and not value.startswith("-"):
                canonical.extend(["-o", value])
                continue
        if lowered.startswith("--namespace="):
            value = safe_token.split("=", 1)[1].strip()
            if value:
                canonical.extend(["--namespace", value])
                continue
        if lowered.startswith("--namespace") and lowered != "--namespace":
            value = safe_token[len("--namespace"):].strip()
            if value:
                canonical.extend(["--namespace", value])
                continue
        if lowered.startswith("--selector="):
            value = safe_token.split("=", 1)[1].strip()
            if value:
                canonical.extend(["--selector", value])
                continue
        if lowered.startswith("--selector") and lowered != "--selector":
            value = safe_token[len("--selector"):].strip()
            if value:
                canonical.extend(["--selector", value])
                continue
        if lowered.startswith("--output="):
            value = safe_token.split("=", 1)[1].strip()
            if value:
                canonical.extend(["--output", value])
                continue
        if lowered.startswith("--output") and lowered != "--output":
            value = safe_token[len("--output"):].strip()
            if value:
                canonical.extend(["--output", value])
                continue
        canonical.append(safe_token)
    return canonical


def _repair_generic_exec_command_for_suggestion(command_text: str) -> str:
    repaired = _collapse_spaces(_as_str(command_text))
    if not repaired:
        return ""
    if len(repaired) >= 2 and repaired[0] in {"'", '"'} and repaired[-1] == repaired[0]:
        repaired = repaired[1:-1].strip()
    repaired = re.sub(
        r"(?i)^kubectl(?=(getpods|getsvc|getservices|get|describe|logs|exec|top|rollout|apply|delete|patch|edit|replace|scale|set|annotate|label|create|expose|autoscale|cordon|uncordon|drain|taint)\b)",
        "kubectl ",
        repaired,
    )
    repaired = re.sub(r"(?i)\bgetpods\b", "get pods", repaired)
    repaired = re.sub(r"(?i)\bgetsvc\b", "get svc", repaired)
    repaired = re.sub(r"(?i)\bgetservices\b", "get services", repaired)
    repaired = re.sub(r"(?i)\bdescribepod\b", "describe pod", repaired)
    repaired = re.sub(r"(?i)\bdescribepods\b", "describe pods", repaired)
    repaired = re.sub(r"(?i)^clickhouse\s+-client(?=\s|$)", "clickhouse-client", repaired)
    repaired = re.sub(r"(?i)(?<!\S)-n([a-z0-9]([-a-z0-9]*[a-z0-9])?)\b", r"-n \1", repaired)
    repaired = re.sub(r"(?i)(?<!\S)-l([a-z0-9._-]+=[a-z0-9._:/-]+)\b", r"-l \1", repaired)
    repaired = re.sub(r"(?i)(?<!\S)-o(jsonpath=[^ ]+|json|yaml|wide|name)\b", r"-o \1", repaired)
    repaired = re.sub(
        r"(?i)(?<!\S)--namespace([a-z0-9]([-a-z0-9]*[a-z0-9])?)\b",
        r"--namespace \1",
        repaired,
    )
    repaired = re.sub(r"(?i)(?<!\S)--selector([a-z0-9._-]+=[a-z0-9._:/-]+)\b", r"--selector \1", repaired)
    repaired = re.sub(r"(?i)(?<!\S)--output(jsonpath=[^ ]+|json|yaml|wide|name)\b", r"--output \1", repaired)
    return _collapse_spaces(repaired)


def _canonicalize_clickhouse_client_argv(command_argv: list[str]) -> list[str]:
    if len(command_argv) >= 2:
        head = _as_str(command_argv[0]).strip().lower()
        second = _as_str(command_argv[1]).strip().lower()
        if head == "clickhouse" and second in {"-client", "client"}:
            return ["clickhouse-client", *command_argv[2:]]
    return command_argv


def _tokenize_shell_punctuation(command_text: str) -> list[str]:
    normalized = _as_str(command_text).strip()
    if not normalized:
        return []
    try:
        lexer = shlex.shlex(normalized, posix=True, punctuation_chars="|&;<>")
        lexer.whitespace_split = True
        return [token for token in (_as_str(item).strip() for item in lexer) if token]
    except Exception:
        return []


def preflight_sql_syntax(
    *,
    namespace: str,
    pod_selector: str,
    query: str,
    timeout_s: int,
) -> Dict[str, Any]:
    safe_query = _normalize_clickhouse_query_text(query)
    if not safe_query:
        return {"ok": False, "reason": "sql_preflight_failed", "detail": "query is empty"}
    if not _sql_preflight_enabled():
        return {"ok": True, "skipped": True}

    safe_timeout = max(3, min(60, _as_positive_int(timeout_s, 20)))
    try:
        pod_lookup = subprocess.run(
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
            capture_output=True,
            text=True,
            timeout=safe_timeout,
            check=False,
        )
    except subprocess.TimeoutExpired:
        return {
            "ok": False,
            "reason": "sql_preflight_failed",
            "detail": f"pod lookup timed out ({safe_timeout}s)",
        }
    except Exception as exc:
        return {
            "ok": False,
            "reason": "sql_preflight_failed",
            "detail": f"pod lookup failed: {exc}",
        }
    if int(pod_lookup.returncode) != 0:
        return {
            "ok": False,
            "reason": "sql_preflight_failed",
            "detail": _as_str(pod_lookup.stderr, "pod lookup failed"),
        }

    pod_name = _as_str(pod_lookup.stdout).strip()
    if not pod_name:
        return {
            "ok": False,
            "reason": "sql_preflight_failed",
            "detail": "no clickhouse pod matched selector",
        }

    preflight_query = f"EXPLAIN SYNTAX {safe_query}"
    try:
        explain_result = subprocess.run(
            [
                "kubectl",
                "-n",
                namespace,
                "exec",
                "-i",
                pod_name,
                "--",
                "clickhouse-client",
                "--query",
                preflight_query,
            ],
            capture_output=True,
            text=True,
            timeout=safe_timeout,
            check=False,
        )
    except subprocess.TimeoutExpired:
        return {
            "ok": False,
            "reason": "sql_preflight_failed",
            "detail": f"EXPLAIN SYNTAX timed out ({safe_timeout}s)",
        }
    except Exception as exc:
        return {
            "ok": False,
            "reason": "sql_preflight_failed",
            "detail": f"EXPLAIN SYNTAX failed: {exc}",
        }
    if int(explain_result.returncode) != 0:
        return {
            "ok": False,
            "reason": "sql_preflight_failed",
            "detail": _as_str(explain_result.stderr, "EXPLAIN SYNTAX failed"),
        }
    return {"ok": True}


def normalize_followup_command_spec(raw: Any) -> Dict[str, Any]:
    if isinstance(raw, dict):
        source = raw
    elif isinstance(raw, str):
        text = raw.strip()
        if not text:
            return {}
        try:
            parsed = json.loads(text)
        except Exception:
            return {}
        source = parsed if isinstance(parsed, dict) else {}
    else:
        return {}

    tool = _as_str(source.get("tool")).strip().lower()
    if not tool:
        return {}
    args_source = source.get("args") if isinstance(source.get("args"), dict) else {}
    namespace = _as_str(args_source.get("namespace") or source.get("namespace")).strip()
    pod_selector = _as_str(args_source.get("pod_selector") or source.get("pod_selector")).strip()
    target_kind = _as_str(args_source.get("target_kind") or source.get("target_kind")).strip().lower()
    target_identity = _as_str(args_source.get("target_identity") or source.get("target_identity")).strip()
    target_id = _as_str(args_source.get("target_id") or source.get("target_id")).strip()
    pod_name = _as_str(args_source.get("pod_name") or source.get("pod_name")).strip()
    command = _as_str(args_source.get("command") or source.get("command")).strip()
    command_argv = _normalize_command_argv(
        args_source.get("command_argv") or args_source.get("argv") or source.get("command_argv") or source.get("argv")
    )
    query = _as_str(
        args_source.get("query")
        or args_source.get("sql")
        or source.get("query")
        or source.get("sql")
    ).strip()
    timeout_s = _as_positive_int(
        args_source.get("timeout_s")
        or source.get("timeout_s")
        or source.get("timeout_seconds"),
        60,
    )

    normalized: Dict[str, Any] = {
        "tool": tool,
        "args": {
            "namespace": namespace,
            "pod_selector": pod_selector,
            "pod_name": pod_name,
            "target_kind": target_kind,
            "target_identity": target_identity,
            "target_id": target_id,
            "command": command,
            "command_argv": command_argv,
            "query": query,
            "timeout_s": timeout_s,
        },
    }
    if namespace:
        normalized["namespace"] = namespace
    if pod_selector:
        normalized["pod_selector"] = pod_selector
    if pod_name:
        normalized["pod_name"] = pod_name
    if target_kind:
        normalized["target_kind"] = target_kind
    if target_identity:
        normalized["target_identity"] = target_identity
    if target_id:
        normalized["target_id"] = target_id
    if command:
        normalized["command"] = command
    if command_argv:
        normalized["command_argv"] = command_argv
    if query:
        normalized["query"] = query
        normalized["sql"] = query
    normalized["timeout_s"] = timeout_s
    purpose = _as_str(source.get("purpose")).strip()
    if purpose:
        normalized["purpose"] = purpose
    return normalized


def build_followup_command_spec_match_key(spec: Any) -> str:
    safe = normalize_followup_command_spec(spec)
    if not safe:
        return ""
    tool = _as_str(safe.get("tool")).lower()
    args = safe.get("args") if isinstance(safe.get("args"), dict) else {}
    if tool in _SUPPORTED_TOOLS:
        return json.dumps(
            {
                "tool": "kubectl_clickhouse_query",
                "namespace": _as_str(args.get("namespace") or safe.get("namespace")).lower(),
                "pod_selector": _collapse_spaces(_as_str(args.get("pod_selector") or safe.get("pod_selector")).lower()),
                "target_identity": _as_str(args.get("target_identity") or safe.get("target_identity")).lower(),
                "query": _normalize_clickhouse_query_text(
                    _as_str(args.get("query") or safe.get("query") or safe.get("sql"))
                ),
            },
            ensure_ascii=False,
            sort_keys=True,
        )
    return json.dumps(safe, ensure_ascii=False, sort_keys=True)


def compile_followup_command_spec(spec: Any, *, run_sql_preflight: bool = False) -> Dict[str, Any]:
    safe = normalize_followup_command_spec(spec)
    if not safe:
        return {"ok": False, "reason": "command_spec is empty"}

    tool = _as_str(safe.get("tool")).lower()
    if tool not in _SUPPORTED_TOOLS:
        return {"ok": False, "reason": f"unsupported command_spec tool: {tool or 'unknown'}"}

    if tool == "generic_exec":
        args = safe.get("args") if isinstance(safe.get("args"), dict) else {}
        target_kind = _as_str(args.get("target_kind") or safe.get("target_kind")).strip().lower()
        target_identity = _as_str(args.get("target_identity") or safe.get("target_identity")).strip()
        target_id = _as_str(args.get("target_id") or safe.get("target_id")).strip()
        timeout_s = _as_positive_int(args.get("timeout_s") or safe.get("timeout_s"), 20)
        command_argv = _normalize_command_argv(
            args.get("command_argv") or args.get("argv") or safe.get("command_argv") or safe.get("argv")
        )
        command_text = _as_str(args.get("command") or safe.get("command")).strip()
        if command_text:
            shell_tokens = _tokenize_shell_punctuation(command_text)
            if any(token in _GENERIC_EXEC_BLOCKED_TOKENS for token in shell_tokens):
                return {"ok": False, "reason": "command_argv contains blocked shell operators"}
        if not command_argv:
            if not command_text:
                return {"ok": False, "reason": "command is required in command_spec.args"}
            try:
                command_argv = [item for item in shlex.split(command_text) if _as_str(item).strip()]
            except Exception:
                return {"ok": False, "reason": "invalid command text in command_spec.args.command"}
        if not command_argv:
            return {"ok": False, "reason": "command_argv is empty"}
        command_argv = _canonicalize_clickhouse_client_argv(command_argv)
        if len(command_argv) > 64:
            return {"ok": False, "reason": "command_argv exceeds max length(64)"}
        if any(any(ch in token for ch in ("\n", "\r", "`")) for token in command_argv):
            return {"ok": False, "reason": "command_argv contains blocked fragments"}
        if any(token in _GENERIC_EXEC_BLOCKED_TOKENS for token in command_argv):
            return {"ok": False, "reason": "command_argv contains blocked shell operators"}
        if any("$(" in token for token in command_argv):
            return {"ok": False, "reason": "command_argv contains shell substitution"}
        head = _as_str(command_argv[0]).strip().lower()
        glued_head_prefix = _detect_glued_command_head(head)
        if glued_head_prefix:
            return {
                "ok": False,
                "reason": "glued_command_tokens",
                "detail": f"command head '{head}' should be separated (expected '{glued_head_prefix} ...')",
            }
        if head not in _GENERIC_EXEC_ALLOWED_HEADS:
            return {"ok": False, "reason": "unsupported_command_head", "detail": f"unsupported command head: {head}"}
        if head == "kubectl":
            command_argv = _canonicalize_kubectl_command_argv(command_argv)
            if not command_argv:
                return {"ok": False, "reason": "command_argv is empty"}
            head = _as_str(command_argv[0]).strip().lower()
        if head == "kubectl":
            kubectl_scope_argv = command_argv
            if "--" in command_argv:
                kubectl_scope_argv = command_argv[: command_argv.index("--")]
            namespace = _extract_cli_flag_value(kubectl_scope_argv, short_flag="-n", long_flag="--namespace").lower()
            if namespace and not _K8S_NAMESPACE_PATTERN.match(namespace):
                return {"ok": False, "reason": "invalid namespace in command_spec"}
            namespace_hint = _extract_namespace_from_target_identity(target_identity)
            _, has_selector_namespace_glue, repaired_selector = _repair_kubectl_selector_namespace_glue(
                kubectl_scope_argv,
                namespace_hint=namespace_hint,
            )
            if has_selector_namespace_glue:
                selector = _extract_cli_flag_value(kubectl_scope_argv, short_flag="-l", long_flag="--selector")
                return {
                    "ok": False,
                    "reason": "suspicious_selector_namespace_glue",
                    "detail": (
                        f"selector '{selector}' looks glued with namespace suffix; "
                        f"try selector '{repaired_selector}'"
                    ),
                }
            if any(("(" in token or ")" in token) for token in kubectl_scope_argv[1:]):
                return {
                    "ok": False,
                    "reason": "invalid_kubectl_token",
                    "detail": "kubectl argv contains unsupported token characters",
                }
            index = 1
            while index < len(kubectl_scope_argv):
                safe_token = _as_str(kubectl_scope_argv[index]).strip()
                if not safe_token or safe_token.startswith("-"):
                    if safe_token in _KUBECTL_FLAGS_WITH_VALUE:
                        index += 2
                        continue
                    if "=" in safe_token:
                        flag_name = safe_token.split("=", 1)[0]
                        if flag_name in _KUBECTL_FLAGS_WITH_VALUE:
                            index += 1
                            continue
                    index += 1
                    continue
                if "=" in safe_token:
                    return {
                        "ok": False,
                        "reason": "invalid_kubectl_token",
                        "detail": "kubectl positional token must not include '='",
                    }
                index += 1
        inferred_target_kind, inferred_target_identity, strict_identity = _infer_generic_exec_target(command_argv)
        if inferred_target_kind and (not target_kind or target_kind == "runtime_node"):
            target_kind = inferred_target_kind
        if inferred_target_identity and (not target_identity or target_identity == "runtime:local"):
            target_identity = inferred_target_identity
        if target_kind and not _GENERIC_EXEC_TARGET_KIND_PATTERN.match(target_kind):
            return {"ok": False, "reason": "invalid target_kind in command_spec"}
        if inferred_target_kind and target_kind and target_kind != inferred_target_kind:
            return {
                "ok": False,
                "reason": "target_kind_mismatch",
                "detail": f"command implies target_kind={inferred_target_kind}, got {target_kind}",
            }
        if strict_identity and inferred_target_identity and target_identity and target_identity != inferred_target_identity:
            return {
                "ok": False,
                "reason": "target_identity_mismatch",
                "detail": f"command implies target_identity={inferred_target_identity}, got {target_identity}",
            }
        if not target_kind:
            target_kind = "unknown"
        if not target_identity:
            target_identity = "unknown"
        command = " ".join(shlex.quote(token) for token in command_argv)
        return {
            "ok": True,
            "tool": "generic_exec",
            "command": command,
            "command_spec": {
                "tool": "generic_exec",
                "args": {
                    "command": command,
                    "command_argv": command_argv,
                    "target_kind": target_kind,
                    "target_identity": target_identity,
                    "target_id": target_id,
                    "timeout_s": timeout_s,
                },
                "command": command,
                "command_argv": command_argv,
                "target_kind": target_kind,
                "target_identity": target_identity,
                "target_id": target_id,
                "timeout_s": timeout_s,
            },
        }

    args = safe.get("args") if isinstance(safe.get("args"), dict) else {}
    namespace = _as_str(args.get("namespace") or safe.get("namespace")).strip().lower()
    if namespace and not _K8S_NAMESPACE_PATTERN.match(namespace):
        return {"ok": False, "reason": "invalid namespace in command_spec"}

    selector = _collapse_spaces(_as_str(args.get("pod_selector") or safe.get("pod_selector")))
    pod_name = _collapse_spaces(_as_str(args.get("pod_name") or safe.get("pod_name")))
    if selector and any(ch in selector for ch in "'\"`$;&|<>"):
        return {"ok": False, "reason": "pod_selector contains unsafe characters"}
    if pod_name and any(ch in pod_name for ch in "'\"`$;&|<>"):
        return {"ok": False, "reason": "pod_name contains unsafe characters"}

    execution_sql = _normalize_clickhouse_query_text(_as_str(args.get("query") or safe.get("query") or safe.get("sql")))
    if not execution_sql:
        return {"ok": False, "reason": "query is required in command_spec.args"}
    if len(execution_sql) > 8000:
        return {"ok": False, "reason": "query is too long in command_spec"}
    compact_sql = execution_sql.rstrip().rstrip(";")
    if ";" in compact_sql:
        return {
            "ok": False,
            "reason": "clickhouse_multi_statement_not_allowed",
            "detail": "query contains multi-statement separator",
        }
    glued_keyword = _detect_glued_clickhouse_keyword(execution_sql)
    if glued_keyword:
        return {
            "ok": False,
            "reason": "glued_sql_tokens",
            "detail": f"sql keyword '{glued_keyword}' must be separated by spaces",
        }
    sql_first_keyword = _extract_sql_first_keyword(execution_sql)
    if sql_first_keyword not in _CLICKHOUSE_READONLY_PREFIXES:
        return {
            "ok": False,
            "reason": "unsupported_clickhouse_readonly_query",
            "detail": "only SELECT/SHOW/DESCRIBE/EXPLAIN are allowed",
        }
    timeout_s = _as_positive_int(args.get("timeout_s") or safe.get("timeout_s"), 60)
    display_sql = _normalize_clickhouse_query_text(_as_str(safe.get("display_sql") or execution_sql))
    target_kind = _as_str(args.get("target_kind") or safe.get("target_kind"), "clickhouse_cluster").strip().lower()
    if not target_kind:
        target_kind = "clickhouse_cluster"
    if target_kind != "clickhouse_cluster":
        return {"ok": False, "reason": f"unsupported target_kind in command_spec: {target_kind}"}
    target_identity = _as_str(args.get("target_identity") or safe.get("target_identity")).strip()
    if not target_identity:
        inferred_database = _extract_clickhouse_database(execution_sql)
        if inferred_database:
            target_identity = f"database:{inferred_database}"
    if not _CLICKHOUSE_TARGET_IDENTITY_PATTERN.match(target_identity):
        if not target_identity:
            return {
                "ok": False,
                "reason": "missing_target_identity",
                "detail": "target_identity is required when query does not uniquely identify a database target",
            }
        return {"ok": False, "reason": "target_identity must look like database:<name>"}
    target_id = _as_str(args.get("target_id") or safe.get("target_id")).strip()

    escaped_query = execution_sql.replace("\\", "\\\\").replace('"', '\\"')
    safe_tool = _as_str(tool).strip().lower()
    require_k8s_exec = safe_tool in {"kubectl_clickhouse_query", "k8s_clickhouse_query"}
    if selector and pod_name:
        return {"ok": False, "reason": "pod_selector and pod_name cannot be used together"}
    if require_k8s_exec and not namespace:
        return {"ok": False, "reason": "missing_namespace_for_k8s_clickhouse_query"}
    if selector:
        if not namespace:
            return {"ok": False, "reason": "namespace is required when pod_selector is provided"}
        if _k8s_pod_autoresolve_enabled():
            resolved = _resolve_clickhouse_pod_name(
                namespace=namespace,
                pod_selector=selector,
                timeout_s=min(timeout_s, 15),
            )
            if bool(resolved.get("ok")):
                pod_name = _collapse_spaces(_as_str(resolved.get("pod_name")))
            elif not _shell_emergency_enabled():
                return {
                    "ok": False,
                    "reason": "pod_name_resolution_failed",
                    "detail": _as_str(resolved.get("detail") or resolved.get("reason") or "pod lookup failed"),
                }
        if pod_name:
            command = f"kubectl -n {namespace} exec -i {pod_name} -- clickhouse-client --query \"{escaped_query}\""
        else:
            if not _shell_emergency_enabled():
                return {
                    "ok": False,
                    "reason": "pod_selector_requires_shell",
                    "detail": "shell execution is disabled; provide pod_name instead of pod_selector",
                }
            command = (
                f"kubectl -n {namespace} exec -i "
                f"$(kubectl -n {namespace} get pods -l {selector} -o jsonpath='{{.items[0].metadata.name}}') "
                f"-- clickhouse-client --query \"{escaped_query}\""
            )
    elif pod_name:
        if not namespace:
            return {"ok": False, "reason": "namespace is required when pod_name is provided"}
        command = f"kubectl -n {namespace} exec -i {pod_name} -- clickhouse-client --query \"{escaped_query}\""
    elif namespace:
        default_selector = _as_str(os.getenv("AI_RUNTIME_CLICKHOUSE_POD_SELECTOR_DEFAULT"), "app=clickhouse").strip()
        if _k8s_pod_autoresolve_enabled() and default_selector:
            resolved = _resolve_clickhouse_pod_name(
                namespace=namespace,
                pod_selector=default_selector,
                timeout_s=min(timeout_s, 15),
            )
            if bool(resolved.get("ok")):
                pod_name = _collapse_spaces(_as_str(resolved.get("pod_name")))
                selector = _collapse_spaces(_as_str(resolved.get("pod_selector") or default_selector))
                command = f"kubectl -n {namespace} exec -i {pod_name} -- clickhouse-client --query \"{escaped_query}\""
            elif require_k8s_exec:
                return {
                    "ok": False,
                    "reason": "pod_name_resolution_failed",
                    "detail": _as_str(resolved.get("detail") or resolved.get("reason") or "pod lookup failed"),
                }
            else:
                command = f"clickhouse-client --query \"{escaped_query}\""
        elif require_k8s_exec:
            return {"ok": False, "reason": "missing_pod_name_for_k8s_clickhouse_query"}
        else:
            command = f"clickhouse-client --query \"{escaped_query}\""
    else:
        command = f"clickhouse-client --query \"{escaped_query}\""
    return {
        "ok": True,
        "tool": "kubectl_clickhouse_query",
        "command": command,
        "command_spec": {
            "tool": "kubectl_clickhouse_query",
            "args": {
                "namespace": namespace,
                "pod_selector": selector,
                "pod_name": pod_name,
                "target_kind": target_kind,
                "target_identity": target_identity,
                "target_id": target_id,
                "query": execution_sql,
                "timeout_s": timeout_s,
            },
            "namespace": namespace,
            "pod_selector": selector,
            "pod_name": pod_name,
            "target_kind": target_kind,
            "target_identity": target_identity,
            "target_id": target_id,
            "query": execution_sql,
            "timeout_s": timeout_s,
            "execution_sql": execution_sql,
            "display_sql": display_sql,
            "runtime_preflight_requested": bool(run_sql_preflight),
        },
    }


def build_command_spec_self_repair_payload(
    *,
    reason: Any,
    detail: Any = "",
    command_spec: Any = None,
    raw_command: str = "",
) -> Dict[str, Any]:
    safe_reason = _as_str(reason).strip().lower()
    safe_detail = _as_str(detail).strip()
    safe_raw_command = _as_str(raw_command).strip()
    safe_spec = normalize_followup_command_spec(command_spec)
    fix_hint = "请补全并提交规范 command_spec（tool + args），系统将自动重新校验。"
    suggested_spec: Dict[str, Any] = {}
    suggested_command = ""

    if safe_reason == "missing_or_invalid_command_spec":
        fix_hint = "请提供 command_spec；不要只传自由文本 command。"
    elif safe_reason == "glued_command_tokens":
        fix_hint = "命令存在粘连（缺少空格），请拆分为标准 argv 形式后重试。"
    elif safe_reason == "unsupported_command_head":
        fix_hint = "命令头不在白名单中，请改为受支持的只读命令（如 kubectl/curl/clickhouse-client）。"
    elif safe_reason == "invalid_kubectl_token":
        fix_hint = "kubectl 参数存在非法字符，请按标准 argv 重新拆分（不要包含括号拼接）。"
    elif safe_reason == "suspicious_selector_namespace_glue":
        fix_hint = "selector 里疑似粘连了 namespace（如 app=query-service-nislap），请拆分为 -l app=query-service -n islap。"
    elif safe_reason == "glued_sql_tokens":
        fix_hint = "SQL 关键字存在粘连，请在关键字与标识符之间补空格后重试。"
    elif safe_reason == "unsupported_clickhouse_readonly_query":
        fix_hint = "只允许只读 SQL：SELECT / SHOW / DESCRIBE / EXPLAIN。"
    elif safe_reason == "clickhouse_multi_statement_not_allowed":
        fix_hint = "一次仅允许一条 SQL 语句，请去掉多语句分隔符 ';'。"
    elif safe_reason == "missing_target_identity":
        fix_hint = "请显式提供 target_identity（例如 database:logs）。"
    elif safe_reason == "target_kind_mismatch":
        fix_hint = "命令语义与 target_kind 不一致，请修正为匹配的执行目标。"
    elif safe_reason == "target_identity_mismatch":
        fix_hint = "命令显式作用域与 target_identity 不一致，请修正后重试。"
    elif safe_reason == "pod_selector_requires_shell":
        fix_hint = "shell 执行面默认禁用，请改为提供 pod_name，不要使用 pod_selector。"
    elif safe_reason == "missing_namespace_for_k8s_clickhouse_query":
        fix_hint = "k8s ClickHouse 查询必须提供 namespace（例如 islap）。"
    elif safe_reason == "missing_pod_name_for_k8s_clickhouse_query":
        fix_hint = "k8s ClickHouse 查询必须在 Pod 中执行，请补充 pod_name 或可解析的 pod_selector。"
    elif safe_reason == "pod_name_resolution_failed":
        fix_hint = "无法自动解析 ClickHouse Pod，请确认 namespace/selector，或显式提供 pod_name（例如 clickhouse-0）。"

    candidate_spec: Dict[str, Any] = {}
    prefer_raw_command = safe_reason in {
        "missing_or_invalid_command_spec",
        "glued_command_tokens",
        "unsupported_command_head",
        "invalid_kubectl_token",
        "suspicious_selector_namespace_glue",
    }
    if safe_spec and not (prefer_raw_command and safe_raw_command):
        try:
            candidate_spec = json.loads(json.dumps(safe_spec))
        except Exception:
            candidate_spec = dict(safe_spec)
    if not candidate_spec and safe_raw_command:
        inferred_argv: list[str] = []
        try:
            inferred_argv = [item for item in shlex.split(safe_raw_command) if _as_str(item).strip()]
        except Exception:
            inferred_argv = []
        inferred_target_kind, inferred_target_identity, _ = _infer_generic_exec_target(inferred_argv)
        candidate_spec = {
            "tool": "generic_exec",
            "args": {
                "command": safe_raw_command,
                "target_kind": inferred_target_kind or "",
                "target_identity": inferred_target_identity or "",
                "timeout_s": 20,
            },
        }

    if candidate_spec:
        candidate_args = candidate_spec.get("args") if isinstance(candidate_spec.get("args"), dict) else {}
        query_text = _as_str(
            candidate_args.get("query")
            or candidate_spec.get("query")
            or candidate_spec.get("sql")
        ).strip()
        changed = False

        if safe_reason == "glued_sql_tokens" and query_text:
            repaired = _repair_clickhouse_query_spacing_for_suggestion(query_text)
            if repaired and repaired != query_text:
                candidate_args["query"] = repaired
                candidate_spec["query"] = repaired
                candidate_spec["sql"] = repaired
                changed = True
        elif safe_reason in {"missing_or_invalid_command_spec", "glued_command_tokens", "unsupported_command_head"}:
            raw_text = _as_str(candidate_args.get("command") or candidate_spec.get("command")).strip()
            repaired = _repair_generic_exec_command_for_suggestion(raw_text)
            if repaired and repaired != raw_text:
                candidate_args["command"] = repaired
                candidate_spec["command"] = repaired
                candidate_args["command_argv"] = []
                candidate_spec["command_argv"] = []
                changed = True
        elif safe_reason == "suspicious_selector_namespace_glue":
            parsed_argv = _normalize_command_argv(
                candidate_args.get("command_argv") or candidate_spec.get("command_argv")
            )
            if not parsed_argv:
                raw_text = _as_str(candidate_args.get("command") or candidate_spec.get("command")).strip()
                if raw_text:
                    try:
                        parsed_argv = [item for item in shlex.split(raw_text) if _as_str(item).strip()]
                    except Exception:
                        parsed_argv = []
            if parsed_argv and _as_str(parsed_argv[0]).strip().lower() == "kubectl":
                parsed_argv = _canonicalize_kubectl_command_argv(parsed_argv)
                namespace_hint = _extract_namespace_from_target_identity(
                    candidate_args.get("target_identity") or candidate_spec.get("target_identity")
                )
                repaired_argv, repaired, _ = _repair_kubectl_selector_namespace_glue(
                    parsed_argv,
                    namespace_hint=namespace_hint,
                )
                if repaired:
                    repaired_command = " ".join(shlex.quote(token) for token in repaired_argv)
                    candidate_args["command_argv"] = repaired_argv
                    candidate_spec["command_argv"] = repaired_argv
                    candidate_args["command"] = repaired_command
                    candidate_spec["command"] = repaired_command
                    changed = True
        elif safe_reason == "clickhouse_multi_statement_not_allowed" and query_text:
            first_stmt = _normalize_clickhouse_query_text(query_text.split(";", 1)[0])
            if first_stmt and first_stmt != query_text:
                candidate_args["query"] = first_stmt
                candidate_spec["query"] = first_stmt
                candidate_spec["sql"] = first_stmt
                changed = True
        elif safe_reason == "missing_target_identity":
            target_identity = _as_str(
                candidate_args.get("target_identity")
                or candidate_spec.get("target_identity")
            ).strip()
            if not target_identity and query_text:
                inferred_db = _extract_clickhouse_database(query_text)
                if inferred_db:
                    inferred_identity = f"database:{inferred_db}"
                    candidate_args["target_identity"] = inferred_identity
                    candidate_spec["target_identity"] = inferred_identity
                    changed = True
        elif safe_reason == "pod_selector_requires_shell":
            if _as_str(candidate_args.get("pod_selector")).strip():
                candidate_args["pod_selector"] = ""
                candidate_spec["pod_selector"] = ""
                changed = True
        elif safe_reason in {"target_kind_mismatch", "target_identity_mismatch"}:
            inferred_target_kind, inferred_target_identity, _ = _infer_generic_exec_target(
                _normalize_command_argv(candidate_args.get("command_argv"))
            )
            if not inferred_target_kind:
                raw_text = _as_str(candidate_args.get("command") or candidate_spec.get("command")).strip()
                if raw_text:
                    try:
                        parsed_argv = [item for item in shlex.split(raw_text) if _as_str(item).strip()]
                    except Exception:
                        parsed_argv = []
                    inferred_target_kind, inferred_target_identity, _ = _infer_generic_exec_target(parsed_argv)
            if inferred_target_kind:
                candidate_args["target_kind"] = inferred_target_kind
                candidate_spec["target_kind"] = inferred_target_kind
                changed = True
            if inferred_target_identity:
                candidate_args["target_identity"] = inferred_target_identity
                candidate_spec["target_identity"] = inferred_target_identity
                changed = True

        if changed:
            candidate_spec["args"] = candidate_args
            compiled = compile_followup_command_spec(candidate_spec, run_sql_preflight=False)
            if bool(compiled.get("ok")):
                suggested_spec = (
                    compiled.get("command_spec")
                    if isinstance(compiled.get("command_spec"), dict)
                    else candidate_spec
                )
                suggested_command = _as_str(compiled.get("command")).strip()
            else:
                suggested_spec = candidate_spec
        elif safe_reason in {
            "missing_or_invalid_command_spec",
            "glued_command_tokens",
            "unsupported_command_head",
            "invalid_kubectl_token",
            "suspicious_selector_namespace_glue",
        } and candidate_spec:
            compiled = compile_followup_command_spec(candidate_spec, run_sql_preflight=False)
            if bool(compiled.get("ok")):
                suggested_spec = (
                    compiled.get("command_spec")
                    if isinstance(compiled.get("command_spec"), dict)
                    else candidate_spec
                )
                suggested_command = _as_str(compiled.get("command")).strip()

    payload: Dict[str, Any] = {
        "fix_code": safe_reason or "missing_or_invalid_command_spec",
        "fix_hint": fix_hint,
    }
    if safe_detail:
        payload["fix_detail"] = safe_detail
    if suggested_spec:
        payload["suggested_command_spec"] = suggested_spec
    if suggested_command:
        payload["suggested_command"] = suggested_command
    return payload
