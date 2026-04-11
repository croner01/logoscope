"""
Runtime config and deployment persistence helpers.

Extracted from `api/ai.py` to keep route module focused on HTTP orchestration.
"""

import os
import re
from typing import Any, Dict, Optional, Set, Tuple

from fastapi import HTTPException


DEFAULT_LLM_DEPLOYMENT_FILE = os.path.abspath(
    os.path.join(os.path.dirname(__file__), "..", "..", "deploy", "ai-service.yaml")
)
DEFAULT_KB_DEPLOYMENT_FILE = DEFAULT_LLM_DEPLOYMENT_FILE
SUPPORTED_KB_REMOTE_PROVIDERS: Set[str] = {"ragflow", "generic_rest", "disabled"}
_ENV_NAME_PATTERN = re.compile(r"^(?P<indent>\s*)-\s+name:\s*(?P<key>[A-Za-z_][A-Za-z0-9_]*)\s*$")
_ENV_VALUE_PATTERN = re.compile(r"^(?P<indent>\s*)value:\s*(?P<value>.*)$")


def _as_str(value: Any, default: str = "") -> str:
    if value is None:
        return default
    text = str(value).strip()
    return text if text else default


def _as_float(value: Any, default: float = 0.0) -> float:
    try:
        return float(value)
    except (TypeError, ValueError):
        return default


def _set_env_if_present(key: str, value: str) -> None:
    if value:
        os.environ[key] = value


def _normalize_kb_provider_name(value: str) -> str:
    provider = (value or "").strip().lower()
    if provider in {"", "none", "off", "local_only", "disabled"}:
        return "disabled"
    if provider in {"ragflow", "generic_rest"}:
        return provider
    return provider


def _kb_provider_defaults(provider: str) -> Dict[str, str]:
    normalized = _normalize_kb_provider_name(provider)
    if normalized == "ragflow":
        return {
            "health_path": "/api/v1/datasets",
            "search_path": "/api/v1/retrieval",
            "upsert_path": "/api/v1/datasets/{dataset_id}/documents",
        }
    return {
        "health_path": "/health",
        "search_path": "/search",
        "upsert_path": "/upsert",
    }


def _resolve_kb_deployment_file_path(extra: Dict[str, Any]) -> str:
    if isinstance(extra, dict):
        from_extra = _as_str(extra.get("deployment_file"))
        if from_extra:
            return os.path.abspath(from_extra)

    from_env = _as_str(os.getenv("KB_DEPLOYMENT_FILE_PATH"))
    if from_env:
        return os.path.abspath(from_env)

    return DEFAULT_KB_DEPLOYMENT_FILE


def _normalize_kb_runtime_config(
    request: Any,
    *,
    supported_kb_remote_providers: Optional[Set[str]] = None,
) -> Dict[str, Any]:
    supported = supported_kb_remote_providers or SUPPORTED_KB_REMOTE_PROVIDERS
    provider = _normalize_kb_provider_name(_as_str(request.provider, _as_str(os.getenv("KB_REMOTE_PROVIDER"), "ragflow")))
    if provider not in supported:
        raise HTTPException(status_code=400, detail="unsupported provider")

    defaults = _kb_provider_defaults(provider)

    base_url = _as_str(request.base_url)
    if not base_url and provider != "disabled":
        base_url = _as_str(os.getenv("KB_REMOTE_BASE_URL") or (os.getenv("KB_RAGFLOW_BASE_URL") if provider == "ragflow" else ""))

    dataset_id = _as_str(
        request.dataset_id,
        _as_str((request.extra or {}).get("dataset_id"), _as_str(os.getenv("KB_RAGFLOW_DATASET_ID") or os.getenv("KB_REMOTE_DATASET_ID"))),
    )

    timeout_seconds = request.timeout_seconds
    if timeout_seconds is None:
        timeout_seconds = max(1, int(_as_float(os.getenv("KB_REMOTE_TIMEOUT_SECONDS"), 5)))
    timeout_seconds = max(1, int(timeout_seconds))

    health_path = _as_str(request.health_path, _as_str(os.getenv("KB_REMOTE_HEALTH_PATH"), defaults["health_path"]))
    search_path = _as_str(request.search_path, _as_str(os.getenv("KB_REMOTE_SEARCH_PATH"), defaults["search_path"]))
    upsert_path = _as_str(request.upsert_path, _as_str(os.getenv("KB_REMOTE_UPSERT_PATH"), defaults["upsert_path"]))

    outbox_enabled = request.outbox_enabled
    if outbox_enabled is None:
        outbox_enabled = _as_str(os.getenv("KB_REMOTE_OUTBOX_ENABLED"), "true").lower() == "true"

    outbox_poll_seconds = request.outbox_poll_seconds
    if outbox_poll_seconds is None:
        outbox_poll_seconds = max(1, int(_as_float(os.getenv("KB_REMOTE_OUTBOX_POLL_SECONDS"), 5)))
    outbox_poll_seconds = max(1, int(outbox_poll_seconds))

    outbox_max_attempts = request.outbox_max_attempts
    if outbox_max_attempts is None:
        outbox_max_attempts = max(1, int(_as_float(os.getenv("KB_REMOTE_OUTBOX_MAX_ATTEMPTS"), 5)))
    outbox_max_attempts = max(1, int(outbox_max_attempts))

    return {
        "provider": provider,
        "base_url": base_url,
        "api_key": _as_str(request.api_key),
        "clear_api_key": bool(request.clear_api_key),
        "dataset_id": dataset_id,
        "timeout_seconds": timeout_seconds,
        "health_path": health_path,
        "search_path": search_path,
        "upsert_path": upsert_path,
        "outbox_enabled": bool(outbox_enabled),
        "outbox_poll_seconds": outbox_poll_seconds,
        "outbox_max_attempts": outbox_max_attempts,
        "persist_to_deployment": bool(request.persist_to_deployment),
        "extra": request.extra or {},
    }


def _apply_kb_runtime_update(normalized: Dict[str, Any]) -> None:
    provider = _normalize_kb_provider_name(_as_str(normalized.get("provider")))
    os.environ["KB_REMOTE_PROVIDER"] = provider

    if provider == "disabled":
        os.environ["KB_REMOTE_BASE_URL"] = ""
        os.environ.pop("KB_REMOTE_DATASET_ID", None)
        os.environ.pop("KB_RAGFLOW_DATASET_ID", None)
    else:
        _set_env_if_present("KB_REMOTE_BASE_URL", _as_str(normalized.get("base_url")))
        dataset_id = _as_str(normalized.get("dataset_id"))
        if provider == "ragflow":
            _set_env_if_present("KB_REMOTE_DATASET_ID", dataset_id)
            _set_env_if_present("KB_RAGFLOW_DATASET_ID", dataset_id)
    os.environ["KB_REMOTE_TIMEOUT_SECONDS"] = str(max(1, int(_as_float(normalized.get("timeout_seconds"), 5))))
    os.environ["KB_REMOTE_HEALTH_PATH"] = _as_str(normalized.get("health_path"), "/health")
    os.environ["KB_REMOTE_SEARCH_PATH"] = _as_str(normalized.get("search_path"), "/search")
    os.environ["KB_REMOTE_UPSERT_PATH"] = _as_str(normalized.get("upsert_path"), "/upsert")
    os.environ["KB_REMOTE_OUTBOX_ENABLED"] = "true" if bool(normalized.get("outbox_enabled", True)) else "false"
    os.environ["KB_REMOTE_OUTBOX_POLL_SECONDS"] = str(max(1, int(_as_float(normalized.get("outbox_poll_seconds"), 5))))
    os.environ["KB_REMOTE_OUTBOX_MAX_ATTEMPTS"] = str(max(1, int(_as_float(normalized.get("outbox_max_attempts"), 5))))

    if normalized.get("clear_api_key"):
        os.environ.pop("KB_REMOTE_API_KEY", None)
        os.environ.pop("KB_RAGFLOW_API_KEY", None)

    api_key = _as_str(normalized.get("api_key"))
    if api_key:
        os.environ["KB_REMOTE_API_KEY"] = api_key
        if provider == "ragflow":
            os.environ["KB_RAGFLOW_API_KEY"] = api_key


def _apply_llm_runtime_update(normalized: Dict[str, Any]) -> None:
    """将 runtime 配置更新到当前进程环境变量。"""
    provider = normalized["provider"]

    _set_env_if_present("LLM_PROVIDER", provider)
    _set_env_if_present("LLM_MODEL", normalized["model"])
    _set_env_if_present("LLM_API_BASE", normalized["api_base"])
    _set_env_if_present("LOCAL_MODEL_PATH", normalized["local_model_path"])

    if provider == "local":
        _set_env_if_present("LOCAL_MODEL_API_BASE", normalized["api_base"])

    if normalized["clear_api_key"]:
        for key in [
            "LLM_API_KEY",
            "OPENAI_API_KEY",
            "ANTHROPIC_API_KEY",
            "DEEPSEEK_API_KEY",
            "LOCAL_MODEL_API_KEY",
        ]:
            os.environ.pop(key, None)

    api_key = normalized["api_key"]
    if api_key:
        os.environ["LLM_API_KEY"] = api_key
        provider_specific_key = {
            "openai": "OPENAI_API_KEY",
            "claude": "ANTHROPIC_API_KEY",
            "deepseek": "DEEPSEEK_API_KEY",
            "local": "LOCAL_MODEL_API_KEY",
        }.get(provider)
        if provider_specific_key:
            os.environ[provider_specific_key] = api_key


def _resolve_llm_deployment_file_path(extra: Dict[str, Any]) -> str:
    """解析 LLM 部署文件路径。"""
    if isinstance(extra, dict):
        from_extra = _as_str(extra.get("deployment_file"))
        if from_extra:
            return os.path.abspath(from_extra)

    from_env = _as_str(os.getenv("LLM_DEPLOYMENT_FILE_PATH"))
    if from_env:
        return os.path.abspath(from_env)

    return DEFAULT_LLM_DEPLOYMENT_FILE


def _yaml_quote(value: str) -> str:
    escaped = str(value or "").replace("\\", "\\\\").replace("\"", "\\\"")
    return f"\"{escaped}\""


def _persist_env_updates_to_deployment_file(
    updates: Dict[str, str],
    deployment_file: str,
) -> Dict[str, Any]:
    """将一组 env 键值对写回部署文件中的 env 段。"""
    result = {
        "persisted": False,
        "deployment_file": deployment_file,
        "updated_keys": [],
        "added_keys": [],
        "error": "",
    }

    if not deployment_file:
        result["error"] = "deployment file path is empty"
        return result

    if not os.path.exists(deployment_file):
        result["error"] = f"deployment file not found: {deployment_file}"
        return result

    try:
        with open(deployment_file, "r", encoding="utf-8") as file_obj:
            content = file_obj.read()
    except Exception as exc:
        result["error"] = f"failed to read deployment file: {exc}"
        return result

    if not content.strip():
        result["error"] = "deployment file is empty"
        return result

    lines = content.splitlines()
    had_trailing_newline = content.endswith("\n")

    env_line_index = -1
    env_indent = 0
    for idx, line in enumerate(lines):
        if line.strip() == "env:":
            env_line_index = idx
            env_indent = len(line) - len(line.lstrip(" "))
            break

    if env_line_index < 0:
        result["error"] = "env section not found in deployment file"
        return result

    env_block_end = len(lines)
    for idx in range(env_line_index + 1, len(lines)):
        line = lines[idx]
        stripped = line.strip()
        if not stripped or stripped.startswith("#"):
            continue
        current_indent = len(line) - len(line.lstrip(" "))
        if current_indent < env_indent:
            env_block_end = idx
            break
        if current_indent == env_indent and not _ENV_NAME_PATTERN.match(line):
            env_block_end = idx
            break

    env_entries: Dict[str, Tuple[int, int, str]] = {}
    cursor = env_line_index + 1
    while cursor < env_block_end:
        line = lines[cursor]
        name_match = _ENV_NAME_PATTERN.match(line)
        if not name_match:
            cursor += 1
            continue

        key = name_match.group("key")
        start = cursor
        item_indent_len = len(name_match.group("indent"))
        cursor += 1
        while cursor < env_block_end:
            next_line = lines[cursor]
            next_match = _ENV_NAME_PATTERN.match(next_line)
            if next_match and len(next_match.group("indent")) == item_indent_len:
                break
            cursor += 1

        env_entries[key] = (start, cursor, name_match.group("indent"))

    item_indent = " " * (env_indent + 2)
    value_indent = f"{item_indent}  "
    if env_entries:
        any_entry = next(iter(env_entries.values()))
        item_indent = any_entry[2]
        value_indent = f"{item_indent}  "

    changed = False
    for key, value in updates.items():
        entry = env_entries.get(key)
        if entry:
            start, end, _ = entry
            value_line_idx = -1
            for idx in range(start + 1, end):
                if _ENV_VALUE_PATTERN.match(lines[idx]):
                    value_line_idx = idx
                    break
            if value_line_idx >= 0:
                new_line = f"{value_indent}value: {_yaml_quote(value)}"
                if lines[value_line_idx] != new_line:
                    lines[value_line_idx] = new_line
                    changed = True
                result["updated_keys"].append(key)
            else:
                continue
            continue

        insert_pos = env_block_end
        lines.insert(insert_pos, f"{item_indent}- name: {key}")
        lines.insert(insert_pos + 1, f"{value_indent}value: {_yaml_quote(value)}")
        env_block_end += 2
        changed = True
        result["added_keys"].append(key)

    if not changed:
        result["persisted"] = True
        return result

    new_content = "\n".join(lines)
    if had_trailing_newline:
        new_content += "\n"

    try:
        with open(deployment_file, "w", encoding="utf-8") as file_obj:
            file_obj.write(new_content)
    except Exception as exc:
        result["error"] = f"failed to write deployment file: {exc}"
        return result

    result["persisted"] = True
    return result


def _persist_llm_runtime_to_deployment_file(
    normalized: Dict[str, Any],
    deployment_file: str,
) -> Dict[str, Any]:
    """
    将 LLM 配置写入部署文件（ai-service Deployment env 段）。

    说明：
    - 仅持久化非敏感参数（provider/model/api_base/local_model_path）。
    - API key 仍建议通过 Secret 管理，不直接写入部署清单。
    """
    updates = {
        "LLM_PROVIDER": _as_str(normalized.get("provider"), "openai"),
        "LLM_MODEL": _as_str(normalized.get("model")),
        "LLM_API_BASE": _as_str(normalized.get("api_base")),
        "LOCAL_MODEL_API_BASE": _as_str(normalized.get("api_base")) if _as_str(normalized.get("provider")) == "local" else "",
        "LOCAL_MODEL_PATH": _as_str(normalized.get("local_model_path")),
    }
    return _persist_env_updates_to_deployment_file(updates, deployment_file)


def _persist_kb_runtime_to_deployment_file(
    normalized: Dict[str, Any],
    deployment_file: str,
) -> Dict[str, Any]:
    """将远端 KB 运行时配置写入部署文件（不包含 API key）。"""
    updates = {
        "KB_REMOTE_PROVIDER": _as_str(normalized.get("provider"), "disabled"),
        "KB_REMOTE_BASE_URL": _as_str(normalized.get("base_url")),
        "KB_REMOTE_DATASET_ID": _as_str(normalized.get("dataset_id")),
        "KB_RAGFLOW_DATASET_ID": _as_str(normalized.get("dataset_id")),
        "KB_REMOTE_TIMEOUT_SECONDS": str(max(1, int(_as_float(normalized.get("timeout_seconds"), 5)))),
        "KB_REMOTE_HEALTH_PATH": _as_str(normalized.get("health_path"), "/health"),
        "KB_REMOTE_SEARCH_PATH": _as_str(normalized.get("search_path"), "/search"),
        "KB_REMOTE_UPSERT_PATH": _as_str(normalized.get("upsert_path"), "/upsert"),
        "KB_REMOTE_OUTBOX_ENABLED": "true" if bool(normalized.get("outbox_enabled", True)) else "false",
        "KB_REMOTE_OUTBOX_POLL_SECONDS": str(max(1, int(_as_float(normalized.get("outbox_poll_seconds"), 5)))),
        "KB_REMOTE_OUTBOX_MAX_ATTEMPTS": str(max(1, int(_as_float(normalized.get("outbox_max_attempts"), 5)))),
    }
    return _persist_env_updates_to_deployment_file(updates, deployment_file)
