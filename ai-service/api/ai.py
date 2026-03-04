"""
AI 分析 API 端点

提供智能日志分析和链路分析的 REST API
支持基于规则的分析和 LLM 大模型分析

Date: 2026-02-09
"""

import asyncio
from collections import OrderedDict
from fastapi import APIRouter, HTTPException, Query
from typing import Dict, Any, Optional, List, Tuple
from pydantic import BaseModel
import json
import logging
import os
import re
import time
import uuid
from datetime import datetime

from ai.analyzer import get_log_analyzer
from ai.llm_service import get_llm_service, reset_llm_service, LLMService
from ai.knowledge_provider import get_knowledge_gateway, shutdown_knowledge_gateway, reload_knowledge_gateway
from ai.session_history import (
    ALLOWED_SESSION_SORT_FIELDS,
    ALLOWED_SESSION_SORT_ORDERS,
    get_ai_session_store,
)
from storage.adapter import StorageAdapter

try:
    from prometheus_client import Counter
except Exception:  # pragma: no cover
    Counter = None

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/api/v1/ai", tags=["ai"])

storage = None
_conversation_sessions: "OrderedDict[str, Dict[str, Any]]" = OrderedDict()

AI_FOLLOWUP_SESSION_CACHE_MAX = max(100, int(os.getenv("AI_FOLLOWUP_SESSION_CACHE_MAX", "1000")))
AI_FOLLOWUP_SESSION_CACHE_TTL_SECONDS = max(60, int(os.getenv("AI_FOLLOWUP_SESSION_CACHE_TTL_SECONDS", "3600")))

SUPPORTED_LLM_PROVIDERS = {"openai", "claude", "deepseek", "local"}
SUPPORTED_KB_REMOTE_PROVIDERS = {"ragflow", "generic_rest", "disabled"}
DEFAULT_LLM_DEPLOYMENT_FILE = os.path.abspath(
    os.path.join(os.path.dirname(__file__), "..", "..", "deploy", "ai-service.yaml")
)
DEFAULT_KB_DEPLOYMENT_FILE = DEFAULT_LLM_DEPLOYMENT_FILE
_ENV_NAME_PATTERN = re.compile(r"^(?P<indent>\s*)-\s+name:\s*(?P<key>[A-Za-z_][A-Za-z0-9_]*)\s*$")
_ENV_VALUE_PATTERN = re.compile(r"^(?P<indent>\s*)value:\s*(?P<value>.*)$")


def _build_counter(name: str, description: str):
    if Counter is None:
        return None
    try:
        return Counter(name, description)
    except Exception:
        return None


def _metric_inc(counter_obj: Any, amount: float = 1.0) -> None:
    if counter_obj is None:
        return
    try:
        counter_obj.inc(amount)
    except Exception:
        return


KB_MANUAL_REMEDIATION_UPDATE_TOTAL = _build_counter(
    "kb_manual_remediation_update_total",
    "Total successful manual remediation updates.",
)


def set_storage_adapter(storage_adapter: StorageAdapter):
    """设置 storage adapter"""
    global storage
    storage = storage_adapter
    try:
        from ai.similar_cases import get_case_store
        get_case_store(storage_adapter)
        gateway = get_knowledge_gateway(storage_adapter)
        gateway.start_outbox_worker()
        get_ai_session_store(storage_adapter)
    except Exception as e:
        logger.warning(f"Failed to initialize AI stores with storage adapter: {e}")


def shutdown_background_tasks() -> None:
    """关闭后台任务（Outbox worker 等）。"""
    try:
        shutdown_knowledge_gateway()
    except Exception as e:
        logger.warning(f"Failed to shutdown AI background tasks cleanly: {e}")


async def _run_blocking(func, *args, **kwargs):
    """在线程池执行阻塞 IO，避免阻塞事件循环。"""
    return await asyncio.to_thread(func, *args, **kwargs)


def _prune_conversation_sessions(now_ts: Optional[float] = None) -> None:
    """按 TTL + 容量淘汰会话缓存，避免内存无界增长。"""
    current_ts = now_ts if now_ts is not None else time.time()

    expired_keys: List[str] = []
    for conversation_id, payload in _conversation_sessions.items():
        updated_at = float(payload.get("updated_at", 0.0))
        if updated_at > 0 and (current_ts - updated_at) > AI_FOLLOWUP_SESSION_CACHE_TTL_SECONDS:
            expired_keys.append(conversation_id)

    for conversation_id in expired_keys:
        _conversation_sessions.pop(conversation_id, None)

    while len(_conversation_sessions) > AI_FOLLOWUP_SESSION_CACHE_MAX:
        _conversation_sessions.popitem(last=False)


def _get_conversation_history(conversation_id: str) -> List[Dict[str, Any]]:
    """读取会话缓存并刷新 LRU 顺序。"""
    _prune_conversation_sessions()
    payload = _conversation_sessions.get(conversation_id)
    if not isinstance(payload, dict):
        return []
    history = payload.get("history")
    if not isinstance(history, list):
        history = []
    payload["updated_at"] = time.time()
    _conversation_sessions[conversation_id] = payload
    _conversation_sessions.move_to_end(conversation_id)
    return history


def _set_conversation_history(conversation_id: str, history: List[Dict[str, Any]]) -> None:
    """写入会话缓存并执行淘汰。"""
    _conversation_sessions[conversation_id] = {
        "history": history,
        "updated_at": time.time(),
    }
    _conversation_sessions.move_to_end(conversation_id)
    _prune_conversation_sessions()


def _clear_conversation_history(conversation_id: str) -> None:
    """清理单个会话缓存。"""
    _conversation_sessions.pop(conversation_id, None)


def _is_llm_configured() -> bool:
    """判断 LLM 运行所需配置是否可用。"""
    provider = (os.getenv("LLM_PROVIDER", "openai") or "openai").strip().lower()

    if provider == "local":
        return bool(
            os.getenv("LLM_API_KEY")
            or os.getenv("LOCAL_MODEL_API_KEY")
            or os.getenv("LOCAL_MODEL_API_BASE")
            or os.getenv("LOCAL_MODEL_BASE_URL")
            or os.getenv("LOCAL_MODEL_PATH")
        )

    return bool(
        os.getenv("LLM_API_KEY")
        or os.getenv("OPENAI_API_KEY")
        or os.getenv("ANTHROPIC_API_KEY")
        or os.getenv("DEEPSEEK_API_KEY")
    )


def _build_llm_runtime_status() -> Dict[str, Any]:
    """返回当前 LLM 运行时配置状态（供后续本地 LLM 接入扩展）。"""
    provider = (os.getenv("LLM_PROVIDER", "openai") or "openai").strip().lower()
    model = (os.getenv("LLM_MODEL", "") or "").strip()
    local_api_base = (
        os.getenv("LOCAL_MODEL_API_BASE")
        or os.getenv("LOCAL_MODEL_BASE_URL")
        or os.getenv("LLM_API_BASE")
        or ""
    ).strip()

    deployment_file = _resolve_llm_deployment_file_path({})
    deployment_exists = os.path.exists(deployment_file)
    deployment_writable = deployment_exists and os.access(deployment_file, os.W_OK)

    return {
        "configured_provider": provider,
        "configured_model": model,
        "llm_enabled": _is_llm_configured(),
        "api_key_configured": bool(
            os.getenv("LLM_API_KEY")
            or os.getenv("OPENAI_API_KEY")
            or os.getenv("ANTHROPIC_API_KEY")
            or os.getenv("DEEPSEEK_API_KEY")
            or os.getenv("LOCAL_MODEL_API_KEY")
        ),
        "local_llm_ready": bool(local_api_base),
        "local_llm_api_base": local_api_base,
        "supported_providers": ["openai", "claude", "deepseek", "local"],
        "runtime_config_contract": {
            "provider": "openai|claude|deepseek|local",
            "model": "string",
            "api_base": "string(url)",
            "api_key": "string(optional, masked input)",
            "local_model_path": "string(optional)",
            "persist_to_deployment": "bool(default=true)",
            "extra": "object(optional)",
        },
        "deployment_persistence": {
            "deployment_file": deployment_file,
            "deployment_file_exists": deployment_exists,
            "deployment_file_writable": deployment_writable,
            "enabled_by_default": True,
        },
        "note": (
            "支持通过 /api/v1/ai/llm/runtime/update 更新运行时配置，并尝试同步写入部署文件；"
            "当部署文件不可访问时，仅当前进程生效。"
        ),
    }


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
            "health_path": "/api/v1/system/health",
            "search_path": "/api/v1/retrieval",
            "upsert_path": "/api/v1/kb/upsert",
        }
    return {
        "health_path": "/health",
        "search_path": "/search",
        "upsert_path": "/upsert",
    }


def _resolve_kb_deployment_file_path(extra: Dict[str, Any]) -> str:
    """解析 KB 远端配置持久化的部署文件路径。"""
    if isinstance(extra, dict):
        from_extra = _as_str(extra.get("deployment_file"))
        if from_extra:
            return os.path.abspath(from_extra)

    from_env = _as_str(os.getenv("KB_DEPLOYMENT_FILE_PATH"))
    if from_env:
        return os.path.abspath(from_env)

    return DEFAULT_KB_DEPLOYMENT_FILE


def _build_kb_runtime_status(force_refresh_provider_status: bool = False) -> Dict[str, Any]:
    provider = _normalize_kb_provider_name(_as_str(os.getenv("KB_REMOTE_PROVIDER"), "ragflow"))
    defaults = _kb_provider_defaults(provider)
    base_url = _as_str(
        os.getenv("KB_REMOTE_BASE_URL")
        or (os.getenv("KB_RAGFLOW_BASE_URL") if provider == "ragflow" else "")
    )
    api_key_configured = bool(
        os.getenv("KB_REMOTE_API_KEY")
        or (os.getenv("KB_RAGFLOW_API_KEY") if provider == "ragflow" else "")
    )
    timeout_seconds = max(1, int(_as_float(os.getenv("KB_REMOTE_TIMEOUT_SECONDS"), 5)))
    health_path = _as_str(os.getenv("KB_REMOTE_HEALTH_PATH"), defaults["health_path"])
    search_path = _as_str(os.getenv("KB_REMOTE_SEARCH_PATH"), defaults["search_path"])
    upsert_path = _as_str(os.getenv("KB_REMOTE_UPSERT_PATH"), defaults["upsert_path"])
    outbox_enabled = _as_str(os.getenv("KB_REMOTE_OUTBOX_ENABLED"), "true").lower() == "true"
    outbox_poll_seconds = max(1, int(_as_float(os.getenv("KB_REMOTE_OUTBOX_POLL_SECONDS"), 5)))
    outbox_max_attempts = max(1, int(_as_float(os.getenv("KB_REMOTE_OUTBOX_MAX_ATTEMPTS"), 5)))

    provider_status: Dict[str, Any] = {
        "remote_available": False,
        "remote_configured": bool(base_url) and provider != "disabled",
        "message": "provider status unavailable",
    }
    try:
        gateway = get_knowledge_gateway(storage)
        provider_status = gateway.get_provider_status(force_refresh=force_refresh_provider_status)
    except Exception as exc:
        provider_status["message"] = f"provider status unavailable: {exc}"

    deployment_file = _resolve_kb_deployment_file_path({})
    deployment_exists = os.path.exists(deployment_file)
    deployment_writable = deployment_exists and os.access(deployment_file, os.W_OK)

    return {
        "configured_provider": provider,
        "configured_base_url": base_url,
        "api_key_configured": api_key_configured,
        "timeout_seconds": timeout_seconds,
        "health_path": health_path,
        "search_path": search_path,
        "upsert_path": upsert_path,
        "outbox_enabled": outbox_enabled,
        "outbox_poll_seconds": outbox_poll_seconds,
        "outbox_max_attempts": outbox_max_attempts,
        "supported_providers": ["ragflow", "generic_rest", "disabled"],
        "runtime_config_contract": {
            "provider": "ragflow|generic_rest|disabled",
            "base_url": "string(url)",
            "api_key": "string(optional, masked input)",
            "timeout_seconds": "int(default=5)",
            "health_path": "string(path)",
            "search_path": "string(path)",
            "upsert_path": "string(path)",
            "outbox_enabled": "bool(default=true)",
            "outbox_poll_seconds": "int(default=5)",
            "outbox_max_attempts": "int(default=5)",
            "persist_to_deployment": "bool(default=true)",
            "extra": "object(optional)",
        },
        "provider_status": provider_status,
        "deployment_persistence": {
            "deployment_file": deployment_file,
            "deployment_file_exists": deployment_exists,
            "deployment_file_writable": deployment_writable,
            "enabled_by_default": True,
        },
        "note": (
            "默认支持 RAGFlow provider，可通过 /api/v1/ai/kb/runtime/update 在线更新。"
            "保存后会重建 KB 网关以立即生效。"
        ),
    }


class AnalyzeLogRequest(BaseModel):
    """单条日志分析请求"""
    id: str
    timestamp: str
    entity: Dict[str, Any]
    event: Dict[str, Any]
    context: Dict[str, Any] = {}


class AnalyzeTraceRequest(BaseModel):
    """链路分析请求"""
    trace_id: str


class LLMAnalyzeRequest(BaseModel):
    """LLM 分析请求"""
    log_content: str
    service_name: str = ""
    context: Dict[str, Any] = None
    use_llm: bool = True


class LLMTraceAnalyzeRequest(BaseModel):
    """LLM 链路分析请求"""
    trace_id: str
    service_name: str = ""


class LLMRuntimeConfig(BaseModel):
    """LLM 运行时配置（预留扩展接口）"""
    provider: Optional[str] = None
    model: Optional[str] = None
    api_base: Optional[str] = None
    local_model_path: Optional[str] = None
    extra: Dict[str, Any] = {}


class LLMRuntimeUpdateRequest(BaseModel):
    """LLM 运行时更新请求（支持 API key 动态更新）"""
    provider: Optional[str] = None
    model: Optional[str] = None
    api_base: Optional[str] = None
    api_key: Optional[str] = None
    local_model_path: Optional[str] = None
    clear_api_key: bool = False
    persist_to_deployment: bool = True
    extra: Dict[str, Any] = {}


class KBRemoteRuntimeConfig(BaseModel):
    """远端知识库运行时配置请求（RAGFlow/Generic REST）。"""
    provider: Optional[str] = None
    base_url: Optional[str] = None
    api_key: Optional[str] = None
    timeout_seconds: Optional[int] = None
    health_path: Optional[str] = None
    search_path: Optional[str] = None
    upsert_path: Optional[str] = None
    outbox_enabled: Optional[bool] = None
    outbox_poll_seconds: Optional[int] = None
    outbox_max_attempts: Optional[int] = None
    clear_api_key: bool = False
    persist_to_deployment: bool = True
    extra: Dict[str, Any] = {}


def _as_str(value: Any, default: str = "") -> str:
    """将任意值转为字符串。"""
    if value is None:
        return default
    text = str(value).strip()
    return text if text else default


def _as_float(value: Any, default: float = 0.0) -> float:
    """将任意值转为浮点数。"""
    try:
        return float(value)
    except (TypeError, ValueError):
        return default


def _as_list(value: Any) -> List[Any]:
    """确保返回列表。"""
    return value if isinstance(value, list) else []


def _set_env_if_present(key: str, value: str) -> None:
    """仅当 value 非空时写入环境变量。"""
    if value:
        os.environ[key] = value


def _normalize_kb_runtime_config(request: KBRemoteRuntimeConfig) -> Dict[str, Any]:
    provider = _normalize_kb_provider_name(_as_str(request.provider, _as_str(os.getenv("KB_REMOTE_PROVIDER"), "ragflow")))
    if provider not in SUPPORTED_KB_REMOTE_PROVIDERS:
        raise HTTPException(status_code=400, detail="unsupported provider")

    defaults = _kb_provider_defaults(provider)

    base_url = _as_str(request.base_url)
    if not base_url and provider != "disabled":
        base_url = _as_str(
            os.getenv("KB_REMOTE_BASE_URL")
            or (os.getenv("KB_RAGFLOW_BASE_URL") if provider == "ragflow" else "")
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
    else:
        _set_env_if_present("KB_REMOTE_BASE_URL", _as_str(normalized.get("base_url")))
    os.environ["KB_REMOTE_TIMEOUT_SECONDS"] = str(max(1, int(_as_float(normalized.get("timeout_seconds"), 5))))
    os.environ["KB_REMOTE_HEALTH_PATH"] = _as_str(normalized.get("health_path"), "/health")
    os.environ["KB_REMOTE_SEARCH_PATH"] = _as_str(normalized.get("search_path"), "/search")
    os.environ["KB_REMOTE_UPSERT_PATH"] = _as_str(normalized.get("upsert_path"), "/upsert")
    os.environ["KB_REMOTE_OUTBOX_ENABLED"] = "true" if bool(normalized.get("outbox_enabled", True)) else "false"
    os.environ["KB_REMOTE_OUTBOX_POLL_SECONDS"] = str(
        max(1, int(_as_float(normalized.get("outbox_poll_seconds"), 5)))
    )
    os.environ["KB_REMOTE_OUTBOX_MAX_ATTEMPTS"] = str(
        max(1, int(_as_float(normalized.get("outbox_max_attempts"), 5)))
    )

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
        # 兼容列表项与 env 同级缩进（如 `env:` 与 `- name:` 对齐）的 YAML 写法。
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

    item_indent = " " * env_indent
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
                # 该 key 若由 valueFrom 管理，不覆盖其结构，避免破坏 Secret 引用。
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
        "LOCAL_MODEL_API_BASE": _as_str(normalized.get("api_base"))
        if _as_str(normalized.get("provider")) == "local"
        else "",
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
        "KB_REMOTE_TIMEOUT_SECONDS": str(max(1, int(_as_float(normalized.get("timeout_seconds"), 5)))),
        "KB_REMOTE_HEALTH_PATH": _as_str(normalized.get("health_path"), "/health"),
        "KB_REMOTE_SEARCH_PATH": _as_str(normalized.get("search_path"), "/search"),
        "KB_REMOTE_UPSERT_PATH": _as_str(normalized.get("upsert_path"), "/upsert"),
        "KB_REMOTE_OUTBOX_ENABLED": "true" if bool(normalized.get("outbox_enabled", True)) else "false",
        "KB_REMOTE_OUTBOX_POLL_SECONDS": str(max(1, int(_as_float(normalized.get("outbox_poll_seconds"), 5)))),
        "KB_REMOTE_OUTBOX_MAX_ATTEMPTS": str(max(1, int(_as_float(normalized.get("outbox_max_attempts"), 5)))),
    }
    return _persist_env_updates_to_deployment_file(updates, deployment_file)


def _normalize_root_causes(raw: Any) -> List[Dict[str, Any]]:
    """统一 root causes 字段格式。"""
    normalized: List[Dict[str, Any]] = []

    for item in _as_list(raw):
        if isinstance(item, dict):
            title = _as_str(
                item.get("title")
                or item.get("name")
                or item.get("cause")
                or item.get("span_id")
            )
            description = _as_str(
                item.get("description")
                or item.get("detail")
                or item.get("reason")
            )
            if title or description:
                normalized_item: Dict[str, Any] = {
                    "title": title or description or "unknown",
                    "description": description,
                }
                if _as_str(item.get("icon")):
                    normalized_item["icon"] = _as_str(item.get("icon"))
                if _as_str(item.get("color")):
                    normalized_item["color"] = _as_str(item.get("color"))
                evidence = _as_list(item.get("evidence"))
                if evidence:
                    normalized_item["evidence"] = evidence
                normalized.append(normalized_item)
            continue

        text = _as_str(item)
        if text:
            normalized.append({"title": text, "description": ""})

    return normalized


def _normalize_solutions(raw: Any) -> List[Dict[str, Any]]:
    """统一 solutions 字段格式。"""
    normalized: List[Dict[str, Any]] = []

    for item in _as_list(raw):
        if isinstance(item, dict):
            title = _as_str(
                item.get("title")
                or item.get("name")
                or item.get("suggestion")
                or item.get("recommendation")
            )
            description = _as_str(
                item.get("description")
                or item.get("detail")
                or item.get("reason")
            )
            steps = [step for step in _as_list(item.get("steps")) if isinstance(step, str)]
            if title or description or steps:
                normalized_item: Dict[str, Any] = {
                    "title": title or description or "建议项",
                    "description": description,
                    "steps": steps,
                }
                resources = [resource for resource in _as_list(item.get("resources")) if isinstance(resource, str)]
                if resources:
                    normalized_item["resources"] = resources
                normalized.append(normalized_item)
            continue

        text = _as_str(item)
        if text:
            normalized.append({
                "title": text,
                "description": "",
                "steps": [],
            })

    return normalized


_SOLUTION_STEP_PATTERN = re.compile(r"^\s*(?:\d+[.)]|[-*])\s*(.+?)\s*$")


def _solutions_to_text(raw: Any) -> str:
    """将结构化 solutions 转换为便于人工编辑的纯文本。"""
    lines: List[str] = []
    for index, item in enumerate(_normalize_solutions(raw), start=1):
        title = _as_str(item.get("title"))
        description = _as_str(item.get("description"))
        steps = [str(step).strip() for step in _as_list(item.get("steps")) if _as_str(step)]
        if lines:
            lines.append("")
        lines.append(f"方案{index}: {title or '未命名方案'}")
        if description:
            lines.append(f"说明: {description}")
        if steps:
            lines.append("步骤:")
            for step_index, step in enumerate(steps, start=1):
                lines.append(f"{step_index}. {step}")
    return "\n".join(lines).strip()


def _normalize_solutions_from_text(solution_text: Any) -> List[Dict[str, Any]]:
    """把纯文本方案解析为结构化 solutions。"""
    text = _truncate_text(_as_str(solution_text), 6000).strip()
    if not text:
        return []
    lines = [line.strip() for line in text.splitlines() if line.strip()]
    if not lines:
        return []

    steps: List[str] = []
    description_lines: List[str] = []
    for line in lines:
        matched = _SOLUTION_STEP_PATTERN.match(line)
        if matched:
            step = _as_str(matched.group(1)).strip()
            if step:
                steps.append(step)
            continue
        cleaned = line
        if cleaned.startswith("方案"):
            cleaned = cleaned.split(":", 1)[-1].strip()
        if cleaned.startswith("说明:"):
            cleaned = cleaned.split(":", 1)[-1].strip()
        if cleaned.startswith("步骤:"):
            continue
        if cleaned:
            description_lines.append(cleaned)

    title = description_lines[0] if description_lines else "执行标准知识库处置步骤"
    description = "\n".join(description_lines[1:]) if len(description_lines) > 1 else (
        description_lines[0] if description_lines else ""
    )
    normalized = _normalize_solutions(
        [{
            "title": _truncate_text(title, 120),
            "description": _truncate_text(description, 1500),
            "steps": _normalize_string_list(steps, max_items=20, min_length=2),
        }]
    )
    return normalized


def _format_solution_text_standard(
    raw_text: str,
    *,
    summary: str = "",
    service_name: str = "",
    problem_type: str = "",
    severity: str = "",
) -> str:
    """规则模式下把方案文本格式化为标准模板。"""
    normalized = _normalize_solutions_from_text(raw_text)
    steps: List[str] = []
    if normalized:
        first = normalized[0]
        steps = [str(step).strip() for step in _as_list(first.get("steps")) if _as_str(step)]
    if not steps:
        steps = _normalize_string_list(raw_text.splitlines(), max_items=8, min_length=2)
    if not steps:
        steps = ["收集关键指标（错误率、延迟、资源水位）并定位异常时间窗口。"]

    summary_text = _truncate_text(_as_str(summary), 200)
    service = _as_str(service_name, "unknown")
    ptype = _as_str(problem_type, "unknown")
    sev = _normalize_kb_draft_severity(severity, default="medium")
    step_lines = "\n".join([f"{index}. {item}" for index, item in enumerate(steps[:12], start=1)])
    return (
        f"【目标】\n恢复 {service} 服务稳定性，避免 {ptype} 问题再次发生。\n\n"
        f"【问题上下文】\n服务={service}，类型={ptype}，级别={sev}。\n"
        f"{summary_text or '根据当前会话信息整理。'}\n\n"
        f"【处理步骤】\n{step_lines}\n\n"
        "【验证方式】\n"
        "1. 观察 15-30 分钟核心指标（错误率、P95 延迟、吞吐）是否恢复基线。\n"
        "2. 核查业务关键接口无新增错误日志。\n\n"
        "【回滚方案】\n若关键指标持续恶化，回滚最近一次配置/发布变更并恢复默认阈值。\n\n"
        "【风险与注意】\n严格按灰度范围执行，避免一次性全量变更。"
    )


def _normalize_similar_cases(raw: Any) -> List[Dict[str, str]]:
    """统一 similar cases 字段格式。"""
    normalized: List[Dict[str, str]] = []

    for item in _as_list(raw):
        if isinstance(item, dict):
            title = _as_str(
                item.get("title")
                or item.get("summary")
                or item.get("case_title")
                or item.get("problem")
            )
            description = _as_str(
                item.get("description")
                or item.get("detail")
                or item.get("resolution")
                or item.get("relevance_reason")
            )
            if title or description:
                normalized.append({
                    "title": title or description or "similar-case",
                    "description": description,
                })
            continue

        text = _as_str(item)
        if text:
            normalized.append({"title": text, "description": ""})

    return normalized


def _normalize_overview(result: Dict[str, Any], fallback_description: str = "") -> Dict[str, Any]:
    """统一 overview 字段格式。"""
    overview = result.get("overview")
    overview_data = overview if isinstance(overview, dict) else {}

    return {
        "problem": _as_str(
            overview_data.get("problem")
            or result.get("problem_type")
            or result.get("problem"),
            "unknown",
        ),
        "severity": _as_str(
            overview_data.get("severity")
            or result.get("severity"),
            "unknown",
        ),
        "description": _as_str(
            overview_data.get("description")
            or result.get("summary")
            or result.get("description"),
            fallback_description,
        ),
        "confidence": _as_float(
            overview_data.get("confidence", result.get("confidence", 0.0)),
            0.0,
        ),
    }


def _normalize_analysis_result(
    raw_result: Any,
    analysis_method: Optional[str] = None,
    fallback_description: str = "",
) -> Dict[str, Any]:
    """
    统一 trace/log AI 返回结构。

    兼容新老字段并输出标准格式：
    - overview
    - rootCauses
    - solutions
    - similarCases
    """
    result: Dict[str, Any] = raw_result if isinstance(raw_result, dict) else {}
    solution_source = (
        result.get("solutions")
        if result.get("solutions") is not None
        else result.get("suggestions")
    )
    if solution_source is None:
        solution_source = result.get("recommendations")

    normalized: Dict[str, Any] = {
        "overview": _normalize_overview(result, fallback_description=fallback_description),
        "rootCauses": _normalize_root_causes(
            result.get("rootCauses") if result.get("rootCauses") is not None else result.get("root_causes")
        ),
        "solutions": _normalize_solutions(solution_source),
        "metrics": _as_list(result.get("metrics")),
        "similarCases": _normalize_similar_cases(
            result.get("similarCases") if result.get("similarCases") is not None else result.get("similar_cases")
        ),
    }

    final_method = analysis_method or _as_str(result.get("analysis_method"))
    if final_method:
        normalized["analysis_method"] = final_method

    model = _as_str(result.get("model"))
    if model:
        normalized["model"] = model

    if isinstance(result.get("cached"), bool):
        normalized["cached"] = result.get("cached")

    if result.get("latency_ms") is not None:
        normalized["latency_ms"] = int(_as_float(result.get("latency_ms"), 0))

    if _as_str(result.get("error")):
        normalized["error"] = _as_str(result.get("error"))

    return normalized


def _trim_conversation_history(
    history: List[Dict[str, Any]],
    max_items: int = 20,
) -> List[Dict[str, Any]]:
    """限制会话历史长度，避免上下文无限增长。"""
    if max_items <= 0:
        return []
    return history[-max_items:]


def _normalize_conversation_history(raw: Any, max_items: int = 20) -> List[Dict[str, Any]]:
    """规范化前端传入的会话历史。"""
    normalized: List[Dict[str, Any]] = []
    for item in _as_list(raw):
        if not isinstance(item, dict):
            continue
        role = _as_str(item.get("role")).lower()
        content = _as_str(item.get("content"))
        if role not in {"user", "assistant"} or not content:
            continue
        normalized.append(
            {
                "role": role,
                "content": content,
                "timestamp": _as_str(item.get("timestamp")) or datetime.utcnow().isoformat() + "Z",
            }
        )
    return _trim_conversation_history(normalized, max_items=max_items)


def _session_messages_to_conversation_history(messages: List[Any], max_items: int = 40) -> List[Dict[str, Any]]:
    """将 session_store 的消息结构转为追问上下文历史结构。"""
    history: List[Dict[str, Any]] = []
    for msg in messages:
        role = _as_str(msg.get("role") if isinstance(msg, dict) else getattr(msg, "role", ""))
        content = _as_str(msg.get("content") if isinstance(msg, dict) else getattr(msg, "content", ""))
        if role not in {"user", "assistant"} or not content:
            continue
        history.append(
            {
                "role": role,
                "content": content,
                "timestamp": _as_str(
                    (
                        (msg.get("created_at") or msg.get("timestamp"))
                        if isinstance(msg, dict)
                        else getattr(msg, "created_at", "")
                    )
                ),
            }
        )
    return _trim_conversation_history(history, max_items=max_items)


def _merge_conversation_history(
    base_history: List[Dict[str, Any]],
    extra_history: List[Dict[str, Any]],
    max_items: int = 40,
) -> List[Dict[str, Any]]:
    """
    合并两段会话历史并去重。

    场景：
    - 前端仅上传增量 history（仅新问题），需要补齐已持久化历史；
    - 避免相同消息重复进入 prompt 上下文。
    """
    merged: List[Dict[str, Any]] = []
    seen: set[Tuple[str, str, str]] = set()

    for item in (base_history or []) + (extra_history or []):
        if not isinstance(item, dict):
            continue
        role = _as_str(item.get("role")).lower()
        content = _as_str(item.get("content"))
        if role not in {"user", "assistant"} or not content:
            continue
        timestamp = _as_str(item.get("timestamp"))
        key = (role, content, timestamp)
        if key in seen:
            continue
        seen.add(key)
        merged.append(
            {
                "role": role,
                "content": content,
                "timestamp": timestamp or (datetime.utcnow().isoformat() + "Z"),
            }
        )

    return _trim_conversation_history(merged, max_items=max_items)


def _build_followup_fallback_answer(
    question: str,
    analysis_context: Dict[str, Any],
    fallback_reason: str = "llm_unavailable",
) -> str:
    """
    LLM 不可用时的规则降级回答。
    尽量基于当前分析上下文给出可执行建议。
    """
    result = analysis_context.get("result") if isinstance(analysis_context, dict) else {}
    overview = result.get("overview") if isinstance(result, dict) else {}
    problem = _as_str(overview.get("problem"), "unknown")
    severity = _as_str(overview.get("severity"), "unknown")
    summary = _as_str(overview.get("description"), "暂无摘要")
    trace_id = _as_str(analysis_context.get("trace_id"))
    service_name = _as_str(analysis_context.get("service_name"), "unknown")

    hints: List[str] = [
        f"当前上下文问题类型: {problem}",
        f"严重级别: {severity}",
        f"服务: {service_name}",
        f"摘要: {summary}",
    ]
    if trace_id:
        hints.append(f"trace_id: {trace_id}")

    reason_text = "当前处于规则模式（LLM 不可用）"
    if fallback_reason == "llm_disabled_by_user":
        reason_text = "当前处于规则模式（已关闭 LLM 开关）"
    elif fallback_reason == "llm_timeout":
        reason_text = "当前处于规则模式（LLM 响应超时，已自动降级）"

    return (
        f"{reason_text}，已结合上下文给出建议。\n"
        f"你的追问：{question}\n"
        + "\n".join(f"- {line}" for line in hints)
        + "\n建议：先按根因列表逐项验证，并补充最新 ERROR/WARN 日志后继续追问。"
    )


def _build_case_analysis_result(case_obj: Any) -> Dict[str, Any]:
    """将案例对象还原为 AIAnalysis 可直接渲染的统一结构。"""
    llm_metadata = case_obj.llm_metadata if isinstance(case_obj.llm_metadata, dict) else {}
    raw_result = {
        "problem_type": case_obj.problem_type,
        "severity": case_obj.severity,
        "summary": case_obj.summary,
        "confidence": llm_metadata.get("confidence", 0.0),
        "root_causes": case_obj.root_causes or [],
        "solutions": case_obj.solutions or [],
        "similar_cases": llm_metadata.get("similar_cases", []),
    }
    return _normalize_analysis_result(
        raw_result,
        analysis_method=_as_str(llm_metadata.get("analysis_method"), "history"),
        fallback_description=case_obj.summary,
    )


def _get_case_status(case_obj: Any) -> str:
    """读取案例状态。"""
    llm_metadata = case_obj.llm_metadata if isinstance(case_obj.llm_metadata, dict) else {}
    status = _as_str(llm_metadata.get("case_status")).lower()
    if status:
        return status
    return "resolved" if bool(case_obj.resolved) else "archived"


def _collect_root_causes_from_result(result: Dict[str, Any]) -> List[str]:
    """从统一分析结果提取根因标题。"""
    root_causes = result.get("rootCauses") if isinstance(result, dict) else []
    items: List[str] = []
    for cause in _as_list(root_causes):
        if isinstance(cause, dict):
            title = _as_str(cause.get("title") or cause.get("description"))
            if title:
                items.append(title)
        else:
            text = _as_str(cause)
            if text:
                items.append(text)
    return items


def _collect_manual_remediation_steps_from_messages(messages: List[Any]) -> List[str]:
    """从追问助手回复提取候选步骤。"""
    steps: List[str] = []
    for msg in messages:
        role = _as_str(getattr(msg, "role", "") if not isinstance(msg, dict) else msg.get("role")).lower()
        if role != "assistant":
            continue
        content = _as_str(getattr(msg, "content", "") if not isinstance(msg, dict) else msg.get("content"))
        if not content:
            continue
        for line in content.splitlines():
            text = line.strip().lstrip("-").lstrip("*").strip()
            if len(text) >= 8:
                steps.append(text)
    unique_steps: List[str] = []
    seen = set()
    for step in steps:
        if step not in seen:
            seen.add(step)
            unique_steps.append(step)
    return unique_steps[:8]


def _history_safe_value(value: Any, max_depth: int = 3, max_list: int = 10, max_text_len: int = 300) -> Any:
    """将值裁剪为可追踪且可序列化的历史快照。"""
    if max_depth <= 0:
        return _truncate_text(_as_str(value), max_text_len)
    if isinstance(value, dict):
        safe: Dict[str, Any] = {}
        for index, (key, item) in enumerate(value.items()):
            if index >= max_list:
                break
            safe[str(key)] = _history_safe_value(item, max_depth=max_depth - 1, max_list=max_list, max_text_len=max_text_len)
        return safe
    if isinstance(value, list):
        return [
            _history_safe_value(item, max_depth=max_depth - 1, max_list=max_list, max_text_len=max_text_len)
            for item in value[:max_list]
        ]
    if isinstance(value, str):
        return _truncate_text(value, max_text_len)
    return value


def _history_compare_value(
    value: Any,
    max_depth: int = 6,
    max_list: int = 400,
    max_text_len: int = 10000,
) -> Any:
    """用于变更判断的稳定比较值（尽量保留完整内容，避免误判无变化）。"""
    return _history_safe_value(
        value,
        max_depth=max_depth,
        max_list=max_list,
        max_text_len=max_text_len,
    )


def _history_snapshot_value(field_name: str, value: Any) -> Any:
    """按字段生成用于历史展示的快照，兼顾可读性与信息完整度。"""
    if field_name in {"solutions", "root_causes", "tags"}:
        return _history_safe_value(value, max_depth=5, max_list=120, max_text_len=2000)
    if field_name in {"summary", "analysis_summary", "resolution"}:
        return _history_safe_value(value, max_depth=4, max_list=40, max_text_len=3000)
    return _history_safe_value(value)


def _history_values_equal(left: Any, right: Any) -> bool:
    """用于历史变更检测的稳定比较。"""
    left_safe = _history_compare_value(left)
    right_safe = _history_compare_value(right)
    return json.dumps(left_safe, ensure_ascii=False, sort_keys=True) == json.dumps(right_safe, ensure_ascii=False, sort_keys=True)


def _build_case_content_change_summary(
    existing_case: Any,
    updated_case: Any,
    previous_analysis_summary: str,
    current_analysis_summary: str,
) -> Dict[str, Any]:
    """构建知识库内容变更摘要（字段级 before/after）。"""
    tracked_fields: List[Tuple[str, Any, Any]] = [
        ("problem_type", _as_str(getattr(existing_case, "problem_type", "")), _as_str(getattr(updated_case, "problem_type", ""))),
        ("severity", _as_str(getattr(existing_case, "severity", "")), _as_str(getattr(updated_case, "severity", ""))),
        ("summary", _as_str(getattr(existing_case, "summary", "")), _as_str(getattr(updated_case, "summary", ""))),
        ("service_name", _as_str(getattr(existing_case, "service_name", "")), _as_str(getattr(updated_case, "service_name", ""))),
        ("root_causes", _as_list(getattr(existing_case, "root_causes", [])), _as_list(getattr(updated_case, "root_causes", []))),
        ("solutions", _normalize_solutions(getattr(existing_case, "solutions", [])), _normalize_solutions(getattr(updated_case, "solutions", []))),
        ("analysis_summary", _as_str(previous_analysis_summary), _as_str(current_analysis_summary)),
        ("resolution", _as_str(getattr(existing_case, "resolution", "")), _as_str(getattr(updated_case, "resolution", ""))),
        ("tags", _as_list(getattr(existing_case, "tags", [])), _as_list(getattr(updated_case, "tags", []))),
    ]

    changed_fields: List[str] = []
    changes: Dict[str, Any] = {}
    for field_name, before_value, after_value in tracked_fields:
        if _history_values_equal(before_value, after_value):
            continue
        changed_fields.append(field_name)
        changes[field_name] = {
            "before": _history_snapshot_value(field_name, before_value),
            "after": _history_snapshot_value(field_name, after_value),
        }
    return {"changed_fields": changed_fields, "changes": changes}


def _collect_requested_content_fields(request: Any) -> List[str]:
    """收集 PATCH /cases/{id} 显式提交的可编辑字段。"""
    requested_fields: List[str] = []
    if getattr(request, "problem_type", None) is not None:
        requested_fields.append("problem_type")
    if getattr(request, "severity", None) is not None:
        requested_fields.append("severity")
    if getattr(request, "summary", None) is not None:
        requested_fields.append("summary")
    if getattr(request, "service_name", None) is not None:
        requested_fields.append("service_name")
    if getattr(request, "root_causes", None) is not None:
        requested_fields.append("root_causes")
    if getattr(request, "solutions", None) is not None or getattr(request, "solutions_text", None) is not None:
        requested_fields.append("solutions")
    if getattr(request, "analysis_summary", None) is not None:
        requested_fields.append("analysis_summary")
    if getattr(request, "resolution", None) is not None:
        requested_fields.append("resolution")
    if getattr(request, "tags", None) is not None:
        requested_fields.append("tags")
    return requested_fields


def _case_store_list_change_history(
    case_store: Any,
    case_id: str,
    *,
    limit: int = 100,
    event_type: str = "content_update",
) -> List[Dict[str, Any]]:
    method = getattr(case_store, "list_case_change_history", None)
    if not callable(method):
        return []
    try:
        result = method(case_id=case_id, limit=limit, event_type=event_type)
    except TypeError:
        result = method(case_id, limit, event_type)
    except Exception as e:
        logger.warning(f"Failed to list case change history from store: {e}")
        return []
    return result if isinstance(result, list) else []


def _case_store_count_change_history(
    case_store: Any,
    case_id: str,
    *,
    event_type: str = "content_update",
) -> int:
    method = getattr(case_store, "count_case_change_history", None)
    if not callable(method):
        return 0
    try:
        value = method(case_id=case_id, event_type=event_type)
    except TypeError:
        value = method(case_id, event_type)
    except Exception as e:
        logger.warning(f"Failed to count case change history from store: {e}")
        return 0
    try:
        return max(0, int(value))
    except Exception:
        return 0


def _case_store_append_change_history(case_store: Any, case_id: str, payload: Dict[str, Any]) -> Dict[str, Any]:
    method = getattr(case_store, "append_case_change_history", None)
    if not callable(method):
        return payload
    try:
        result = method(case_id=case_id, event=payload)
    except TypeError:
        result = method(case_id, payload)
    except Exception as e:
        logger.warning(f"Failed to append case change history to store: {e}")
        return payload
    return result if isinstance(result, dict) else payload


def _extract_first_json_dict(text: str) -> Optional[Dict[str, Any]]:
    """从混合文本中提取首个 JSON 对象。"""
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


def _parse_llm_json_dict(content: str) -> Optional[Dict[str, Any]]:
    """解析 LLM 输出中的 JSON 对象，兼容 markdown 代码块。"""
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
        try:
            parsed = json.loads(candidate)
            if isinstance(parsed, dict):
                return parsed
        except json.JSONDecodeError:
            pass

        parsed = _extract_first_json_dict(candidate)
        if parsed is not None:
            return parsed

    return None


def _truncate_text(value: Any, max_len: int) -> str:
    """裁剪文本长度，避免 prompt 与返回字段膨胀。"""
    text = _as_str(value)
    if max_len <= 0:
        return ""
    return text[:max_len]


def _normalize_kb_draft_severity(value: Any, default: str = "medium") -> str:
    """规范化严重级别。"""
    severity = _as_str(value, default).strip().lower()
    aliases = {
        "sev0": "critical",
        "sev1": "high",
        "sev2": "medium",
        "sev3": "low",
        "p0": "critical",
        "p1": "high",
        "p2": "medium",
        "p3": "low",
    }
    normalized = aliases.get(severity, severity)
    if normalized not in {"critical", "high", "medium", "low", "unknown"}:
        return default
    return normalized


def _normalize_string_list(raw: Any, max_items: int = 8, min_length: int = 4) -> List[str]:
    """将任意列表归一化为去重字符串列表。"""
    items: List[str] = []
    for item in _as_list(raw):
        text = _as_str(item).strip().lstrip("-").lstrip("*").strip()
        if len(text) >= min_length:
            items.append(text)

    unique_items: List[str] = []
    seen = set()
    for item in items:
        if item not in seen:
            seen.add(item)
            unique_items.append(item)
    return unique_items[:max(1, max_items)]


def _build_rule_based_kb_draft(
    session: Dict[str, Any],
    messages: List[Any],
    include_followup: bool,
) -> Dict[str, Any]:
    """生成规则模式草稿，作为默认路径与 LLM 回退底稿。"""
    result_container = session.get("result") if isinstance(session, dict) else {}
    raw_result = result_container.get("raw") if isinstance(result_container, dict) else {}
    normalized = _normalize_analysis_result(raw_result, fallback_description=_as_str(session.get("summary_text")))
    overview = normalized.get("overview") if isinstance(normalized.get("overview"), dict) else {}
    summary = _as_str(overview.get("description"), _as_str(session.get("summary_text")))
    problem_type = _as_str(overview.get("problem"), "unknown").lower()
    severity = _normalize_kb_draft_severity(overview.get("severity"), default="medium")
    root_causes = _collect_root_causes_from_result(normalized)
    solutions = _normalize_solutions(normalized.get("solutions"))
    remediation_steps = (
        _collect_manual_remediation_steps_from_messages(messages if bool(include_followup) else [])
        if bool(include_followup)
        else []
    )

    return {
        "problem_type": problem_type,
        "severity": severity,
        "summary": summary,
        "log_content": _as_str(session.get("input_text")),
        "service_name": _as_str(session.get("service_name")),
        "root_causes": root_causes,
        "solutions": solutions,
        "analysis_summary": summary,
        "manual_remediation_steps": remediation_steps,
    }


def _build_kb_draft_quality(
    draft_case: Dict[str, Any],
    confidence_hint: Optional[float] = None,
) -> Tuple[List[str], float]:
    """评估草稿必填项完整度并给出置信度。"""
    missing_required_fields: List[str] = []
    for key in ["problem_type", "severity", "summary", "log_content", "service_name"]:
        if not _as_str(draft_case.get(key)):
            missing_required_fields.append(key)

    if not _normalize_string_list(draft_case.get("root_causes"), max_items=8, min_length=2):
        missing_required_fields.append("root_causes")

    normalized_solutions = _normalize_solutions(draft_case.get("solutions"))
    if not normalized_solutions:
        missing_required_fields.append("solutions")
    draft_case["solutions"] = normalized_solutions

    if confidence_hint is None:
        confidence = 0.86
        if missing_required_fields:
            confidence = 0.62
        elif _as_str(draft_case.get("problem_type"), "unknown").lower() == "unknown":
            confidence = 0.71
    else:
        confidence = max(0.0, min(float(confidence_hint), 1.0))
        if missing_required_fields:
            confidence = min(confidence, 0.65)

    draft_case["root_causes"] = _normalize_string_list(draft_case.get("root_causes"), max_items=8, min_length=2)
    draft_case["manual_remediation_steps"] = _normalize_string_list(
        draft_case.get("manual_remediation_steps"),
        max_items=8,
        min_length=4,
    )
    draft_case["summary"] = _truncate_text(_as_str(draft_case.get("summary")), 1000)
    draft_case["analysis_summary"] = _truncate_text(
        _as_str(draft_case.get("analysis_summary") or draft_case.get("summary")),
        1200,
    )
    draft_case["problem_type"] = _as_str(draft_case.get("problem_type"), "unknown").lower()
    draft_case["severity"] = _normalize_kb_draft_severity(draft_case.get("severity"), default="medium")
    draft_case["log_content"] = _truncate_text(_as_str(draft_case.get("log_content")), 8000)
    draft_case["service_name"] = _as_str(draft_case.get("service_name"))

    return missing_required_fields, confidence


def _build_kb_conversation_transcript(
    session: Dict[str, Any],
    messages: List[Any],
    include_followup: bool,
    normalized_result: Dict[str, Any],
) -> str:
    """构建会话文本摘要，供 LLM 进行全会话归纳。"""
    session_id = _as_str(session.get("session_id"))
    analysis_type = _as_str(session.get("analysis_type"), "log")
    service_name = _as_str(session.get("service_name"), "unknown")
    trace_id = _as_str(session.get("trace_id"))
    summary_text = _as_str(session.get("summary_text"))
    input_text = _truncate_text(_mask_sensitive_text(_as_str(session.get("input_text"))), 2000)

    max_messages = max(20, int(_as_float(os.getenv("AI_KB_DRAFT_LLM_MAX_MESSAGES", 120), 120)))
    max_message_chars = max(120, int(_as_float(os.getenv("AI_KB_DRAFT_LLM_MAX_MESSAGE_CHARS", 900), 900)))
    selected_messages = messages if include_followup else []
    selected_messages = _as_list(selected_messages)[-max_messages:]

    dialogue_lines: List[str] = []
    for msg in selected_messages:
        role = _as_str(msg.get("role") if isinstance(msg, dict) else getattr(msg, "role", "")).lower()
        content = _as_str(msg.get("content") if isinstance(msg, dict) else getattr(msg, "content", ""))
        if role not in {"user", "assistant"} or not content:
            continue
        safe_content = _truncate_text(_mask_sensitive_text(content), max_message_chars)
        dialogue_lines.append(f"{role}: {safe_content}")

    overview = normalized_result.get("overview") if isinstance(normalized_result, dict) else {}
    overview_problem = _as_str(overview.get("problem"), "unknown") if isinstance(overview, dict) else "unknown"
    overview_severity = _as_str(overview.get("severity"), "unknown") if isinstance(overview, dict) else "unknown"
    overview_description = _as_str(overview.get("description"))
    root_causes = _collect_root_causes_from_result(normalized_result)
    solutions = _normalize_solutions(normalized_result.get("solutions"))
    solution_titles = [
        _as_str(item.get("title"))
        for item in solutions
        if isinstance(item, dict) and _as_str(item.get("title"))
    ]

    transcript_lines = [
        f"session_id: {session_id}",
        f"analysis_type: {analysis_type}",
        f"service_name: {service_name}",
        f"trace_id: {trace_id or 'N/A'}",
        f"session_summary: {summary_text}",
        f"input_text: {input_text}",
        f"baseline_problem_type: {overview_problem}",
        f"baseline_severity: {overview_severity}",
        f"baseline_analysis_summary: {overview_description}",
        f"baseline_root_causes: {json.dumps(root_causes[:8], ensure_ascii=False)}",
        f"baseline_solutions: {json.dumps(solution_titles[:8], ensure_ascii=False)}",
    ]
    if dialogue_lines:
        transcript_lines.append("conversation:")
        transcript_lines.extend(dialogue_lines)
    else:
        transcript_lines.append("conversation: []")

    return "\n".join(transcript_lines)


async def _build_llm_kb_draft(
    session: Dict[str, Any],
    messages: List[Any],
    include_followup: bool,
    fallback_draft: Dict[str, Any],
) -> Dict[str, Any]:
    """使用 LLM 对整段会话进行归纳，生成知识草稿结构。"""
    result_container = session.get("result") if isinstance(session, dict) else {}
    raw_result = result_container.get("raw") if isinstance(result_container, dict) else {}
    normalized_result = _normalize_analysis_result(raw_result, fallback_description=_as_str(session.get("summary_text")))
    transcript = _build_kb_conversation_transcript(
        session,
        messages,
        include_followup=include_followup,
        normalized_result=normalized_result,
    )

    llm_timeout_seconds = max(5, int(_as_float(os.getenv("AI_KB_DRAFT_LLM_TIMEOUT_SECONDS", 45), 45)))
    llm_service = get_llm_service()
    prompt = (
        "请基于以下完整分析会话生成知识库草稿，输出严格 JSON（不要 markdown、不要额外解释）。\n"
        "JSON schema:\n"
        "{\n"
        '  "problem_type": "string",\n'
        '  "severity": "critical|high|medium|low|unknown",\n'
        '  "summary": "string",\n'
        '  "analysis_summary": "string",\n'
        '  "root_causes": ["string"],\n'
        '  "solutions": [{"title":"string","description":"string","steps":["string"]}],\n'
        '  "manual_remediation_steps": ["string"],\n'
        '  "confidence": 0.0\n'
        "}\n"
        "约束：\n"
        "1) root_causes 3-8 条，短句且可执行；\n"
        "2) solutions 1-6 条，每条要包含 title，steps 可选；\n"
        "3) manual_remediation_steps 0-8 条；\n"
        "4) 若信息不足，用基于现有上下文最合理的推断，不要留空对象。\n\n"
        "会话内容：\n"
        f"{transcript}"
    )

    response_text = await asyncio.wait_for(
        llm_service.chat(
            message=prompt,
            context={
                "analysis_session_id": _as_str(session.get("session_id")),
                "analysis_type": _as_str(session.get("analysis_type"), "log"),
                "service_name": _as_str(session.get("service_name")),
                "include_followup": bool(include_followup),
                "conversation_transcript": transcript,
            },
        ),
        timeout=llm_timeout_seconds,
    )
    parsed = _parse_llm_json_dict(response_text)
    if parsed is None:
        raise ValueError("llm_kb_draft_parse_failed")

    solution_source = (
        parsed.get("solutions")
        if parsed.get("solutions") is not None
        else parsed.get("recommendations")
    )
    llm_draft = {
        "problem_type": _as_str(
            parsed.get("problem_type") or parsed.get("problemType"),
            fallback_draft.get("problem_type"),
        ).lower(),
        "severity": _normalize_kb_draft_severity(
            parsed.get("severity"),
            default=_as_str(fallback_draft.get("severity"), "medium"),
        ),
        "summary": _as_str(parsed.get("summary"), _as_str(fallback_draft.get("summary"))),
        "analysis_summary": _as_str(
            parsed.get("analysis_summary"),
            _as_str(parsed.get("summary"), _as_str(fallback_draft.get("analysis_summary"))),
        ),
        "root_causes": _normalize_string_list(
            parsed.get("root_causes") if parsed.get("root_causes") is not None else parsed.get("rootCauses"),
            max_items=8,
            min_length=2,
        ),
        "solutions": _normalize_solutions(solution_source),
        "manual_remediation_steps": _normalize_string_list(
            parsed.get("manual_remediation_steps")
            if parsed.get("manual_remediation_steps") is not None
            else parsed.get("manualRemediationSteps"),
            max_items=8,
            min_length=4,
        ),
        "log_content": _as_str(fallback_draft.get("log_content")),
        "service_name": _as_str(fallback_draft.get("service_name")),
    }

    if not llm_draft["root_causes"]:
        llm_draft["root_causes"] = _normalize_string_list(fallback_draft.get("root_causes"), max_items=8, min_length=2)
    if not llm_draft["solutions"]:
        llm_draft["solutions"] = _normalize_solutions(fallback_draft.get("solutions"))
    if not llm_draft["manual_remediation_steps"]:
        llm_draft["manual_remediation_steps"] = _normalize_string_list(
            fallback_draft.get("manual_remediation_steps"),
            max_items=8,
            min_length=4,
        )

    confidence = _as_float(parsed.get("confidence"), 0.88)
    return {"draft_case": llm_draft, "confidence": max(0.0, min(confidence, 1.0))}


def _build_case_payload_for_remote(case_obj: Any) -> Dict[str, Any]:
    """构建远端同步 payload。"""
    llm_metadata = case_obj.llm_metadata if isinstance(case_obj.llm_metadata, dict) else {}
    return {
        "id": case_obj.id,
        "problem_type": case_obj.problem_type,
        "severity": case_obj.severity,
        "summary": case_obj.summary,
        "log_content": case_obj.log_content,
        "service_name": case_obj.service_name,
        "root_causes": case_obj.root_causes or [],
        "solutions": case_obj.solutions or [],
        "resolution": case_obj.resolution,
        "resolved": bool(case_obj.resolved),
        "case_status": _get_case_status(case_obj),
        "manual_remediation_steps": llm_metadata.get("manual_remediation_steps", []),
        "verification_result": _as_str(llm_metadata.get("verification_result")),
        "verification_notes": _as_str(llm_metadata.get("verification_notes")),
        "knowledge_version": int(_as_float(llm_metadata.get("knowledge_version", 1), 1)),
        "updated_at": case_obj.updated_at,
        "context": case_obj.context if isinstance(case_obj.context, dict) else {},
    }


def _extract_overview_summary(result: Dict[str, Any]) -> str:
    overview = result.get("overview") if isinstance(result, dict) else {}
    if isinstance(overview, dict):
        description = _as_str(overview.get("description"))
        if description:
            return description
    return _as_str(result.get("summary"))


def _mask_sensitive_text(text: str) -> str:
    """脱敏文本，避免敏感字段进入会话存储或 LLM 上下文。"""
    value = str(text or "")
    if not value:
        return ""

    masked = value
    masked = re.sub(r"(?i)\b(bearer)\s+[A-Za-z0-9\-._~+/]+=*\b", r"\1 ***", masked)
    masked = re.sub(
        r"(?i)\b(password|passwd|pwd|secret|token|api[_-]?key|authorization)\s*[:=]\s*([^\s,;]+)",
        lambda m: f"{m.group(1)}=***",
        masked,
    )
    masked = re.sub(
        r"\b([A-Za-z0-9._%+-])[A-Za-z0-9._%+-]*@([A-Za-z0-9.-]+\.[A-Za-z]{2,})\b",
        r"\1***@\2",
        masked,
    )
    masked = re.sub(r"\b\d{1,3}(?:\.\d{1,3}){3}\b", "***.***.***.***", masked)
    masked = re.sub(r"\bAKIA[0-9A-Z]{12,}\b", "AKIA***", masked)
    masked = re.sub(r"\b[A-Za-z0-9_\-]{32,}\b", lambda m: m.group(0)[:4] + "***" + m.group(0)[-2:], masked)
    return masked


def _mask_sensitive_payload(payload: Any) -> Any:
    """递归脱敏结构化对象。"""
    if isinstance(payload, dict):
        return {str(key): _mask_sensitive_payload(value) for key, value in payload.items()}
    if isinstance(payload, list):
        return [_mask_sensitive_payload(item) for item in payload]
    if isinstance(payload, str):
        return _mask_sensitive_text(payload)
    return payload


def _build_followup_references(analysis_context: Dict[str, Any]) -> List[Dict[str, str]]:
    """构建追问可解释性引用（分析结论片段 + 原始日志片段）。"""
    references: List[Dict[str, str]] = []
    raw_log_ref_index = 1
    result = analysis_context.get("result") if isinstance(analysis_context, dict) else {}
    overview = result.get("overview") if isinstance(result, dict) else {}
    if isinstance(overview, dict):
        summary = _as_str(overview.get("description") or overview.get("problem"))
        if summary:
            references.append(
                {
                    "id": "A1",
                    "type": "analysis",
                    "title": "本次分析结论片段",
                    "snippet": _mask_sensitive_text(summary)[:240],
                }
            )
    root_causes = result.get("rootCauses") if isinstance(result, dict) else []
    for index, cause in enumerate(_as_list(root_causes)[:2], start=2):
        if not isinstance(cause, dict):
            continue
        title = _as_str(cause.get("title"))
        description = _as_str(cause.get("description"))
        snippet = f"{title} {description}".strip()
        if snippet:
            references.append(
                {
                    "id": f"A{index}",
                    "type": "analysis",
                    "title": "根因片段",
                    "snippet": _mask_sensitive_text(snippet)[:240],
                }
            )

    input_text = _as_str(analysis_context.get("input_text") or analysis_context.get("log_content"))
    if input_text:
        raw_lines = [line.strip() for line in input_text.splitlines() if line.strip()]
        sample_lines = raw_lines[:2] if raw_lines else [input_text[:220]]
        for line in sample_lines:
            references.append(
                {
                    "id": f"L{raw_log_ref_index}",
                    "type": "raw_log",
                    "title": "原始日志片段",
                    "snippet": _mask_sensitive_text(line)[:240],
                }
            )
            raw_log_ref_index += 1

    followup_related_logs = analysis_context.get("followup_related_logs")
    if not followup_related_logs:
        followup_related_logs = analysis_context.get("related_logs")
    for event in _as_list(followup_related_logs)[:3]:
        if not isinstance(event, dict):
            continue
        message = _as_str(event.get("message"))
        if not message:
            continue
        level = _as_str(event.get("level"), "INFO").upper()
        timestamp = _as_str(event.get("timestamp"))
        service_name = _as_str(event.get("service_name"))
        snippet_parts = [item for item in [timestamp, level, service_name, message] if item]
        references.append(
            {
                "id": f"L{raw_log_ref_index}",
                "type": "related_log",
                "title": "追问补充日志片段",
                "snippet": _mask_sensitive_text(" ".join(snippet_parts))[:240],
            }
        )
        raw_log_ref_index += 1
    return references[:8]


def _build_context_pills(analysis_context: Dict[str, Any], analysis_session_id: str = "") -> List[Dict[str, str]]:
    """构建前端可直接展示的上下文 pills。"""
    result = analysis_context.get("result") if isinstance(analysis_context, dict) else {}
    summary = _extract_overview_summary(result) if isinstance(result, dict) else ""
    related_log_count = int(_as_float(analysis_context.get("followup_related_log_count"), 0))
    pills: List[Dict[str, str]] = []
    values = [
        ("analysis_type", _as_str(analysis_context.get("analysis_type"), "log")),
        ("service", _as_str(analysis_context.get("service_name"))),
        ("trace_id", _as_str(analysis_context.get("trace_id"))),
        ("session_id", _as_str(analysis_session_id)),
        ("summary", _as_str(summary)),
        ("input_preview", _as_str(analysis_context.get("input_text"))[:80]),
        ("related_logs", str(related_log_count) if related_log_count > 0 else ""),
    ]
    for key, value in values:
        if value:
            pills.append({"key": key, "value": _mask_sensitive_text(value)})
    return pills


def _compact_conversation_for_prompt(history: List[Dict[str, Any]]) -> Dict[str, Any]:
    """长会话自动压缩，降低 token 成本。"""
    trigger = max(6, int(os.getenv("AI_FOLLOWUP_COMPACT_TRIGGER", "12")))
    keep_recent = max(4, int(os.getenv("AI_FOLLOWUP_COMPACT_KEEP_RECENT", "8")))
    if len(history) <= trigger:
        return {"history": history[-max(keep_recent, 10):], "summary": "", "compacted": False}

    older = history[:-keep_recent]
    recent = history[-keep_recent:]
    summary_lines: List[str] = []
    for item in older[-16:]:
        role = _as_str(item.get("role"))
        content = _as_str(item.get("content")).replace("\n", " ").strip()
        if not content:
            continue
        role_label = "用户" if role == "user" else "AI"
        summary_lines.append(f"{role_label}: {content[:180]}")
    return {
        "history": recent,
        "summary": "\n".join(summary_lines[:16]),
        "compacted": True,
    }


def _estimate_token_usage(*parts: Any) -> int:
    """粗略估算 token 数，按字符数/4。"""
    total_chars = 0
    for part in parts:
        if part is None:
            continue
        if isinstance(part, (dict, list)):
            total_chars += len(str(part))
        else:
            total_chars += len(str(part))
    return max(1, total_chars // 4)


def _extract_steps_from_text(content: str, max_steps: int = 6) -> List[str]:
    lines = [line.strip(" -*\t") for line in str(content or "").splitlines()]
    steps = [line for line in lines if line and len(line) > 4]
    return steps[:max_steps]


def _build_followup_action_draft(
    *,
    action_type: str,
    message_content: str,
    session: Dict[str, Any],
    preferred_title: str = "",
    extra: Optional[Dict[str, Any]] = None,
) -> Dict[str, Any]:
    """将追问回答转换为工单/Runbook/告警抑制建议草案。"""
    action = _as_str(action_type).lower()
    if action not in {"ticket", "runbook", "alert_suppression"}:
        raise HTTPException(status_code=400, detail="unsupported action_type")

    session_id = _as_str(session.get("session_id"))
    service_name = _as_str(session.get("service_name"), "unknown")
    trace_id = _as_str(session.get("trace_id"))
    summary = _as_str(session.get("summary_text") or session.get("title"))
    title = _as_str(preferred_title) or f"[AI]{service_name} {summary or 'follow-up action'}"
    now = datetime.utcnow().isoformat() + "Z"
    extra_payload = extra or {}

    common = {
        "session_id": session_id,
        "service_name": service_name,
        "trace_id": trace_id,
        "generated_at": now,
        "source": "ai-follow-up",
    }

    if action == "ticket":
        return {
            "action_type": "ticket",
            "title": title[:160],
            "payload": {
                **common,
                "severity": _as_str(session.get("status"), "unknown"),
                "description": _mask_sensitive_text(message_content)[:2000],
                "labels": ["ai-generated", f"service:{service_name}"],
                "assignee": _as_str(extra_payload.get("assignee"), ""),
            },
        }

    if action == "runbook":
        return {
            "action_type": "runbook",
            "title": title[:160],
            "payload": {
                **common,
                "objective": _as_str(extra_payload.get("objective"), "恢复服务并验证稳定性"),
                "steps": _extract_steps_from_text(message_content),
                "rollback_plan": _as_str(extra_payload.get("rollback_plan"), "若关键指标恶化，回滚最近一次变更。"),
                "verification": _as_str(extra_payload.get("verification"), "确认错误率恢复基线且无新增关键告警。"),
            },
        }

    return {
        "action_type": "alert_suppression",
        "title": title[:160],
        "payload": {
            **common,
            "rule_scope": _as_str(extra_payload.get("rule_scope"), f"service={service_name}"),
            "condition": _as_str(extra_payload.get("condition"), "短时重复告警且已定位根因"),
            "duration_minutes": int(extra_payload.get("duration_minutes") or 30),
            "reason": _mask_sensitive_text(message_content)[:600],
            "safety_guard": _as_str(extra_payload.get("safety_guard"), "仅抑制重复噪声告警，不抑制 P1/P0 告警。"),
        },
    }


async def _persist_analysis_session(
    *,
    analysis_type: str,
    service_name: str,
    input_text: str,
    trace_id: str = "",
    context: Optional[Dict[str, Any]] = None,
    result: Optional[Dict[str, Any]] = None,
    source: str,
) -> str:
    """把分析请求及结果持久化为 AI 历史会话。"""
    try:
        session_store = get_ai_session_store(storage)
        normalized_result = result or {}
        llm_metadata = normalized_result if isinstance(normalized_result, dict) else {}
        summary_text = _extract_overview_summary(normalized_result)
        safe_context = _mask_sensitive_payload(context or {})
        safe_input_text = _mask_sensitive_text(input_text)
        session = await _run_blocking(
            session_store.create_session,
            analysis_type=analysis_type,
            service_name=service_name,
            input_text=safe_input_text,
            trace_id=trace_id,
            context=safe_context,
            result={
                "summary": summary_text,
                "raw": _mask_sensitive_payload(normalized_result),
            },
            analysis_method=_as_str(llm_metadata.get("analysis_method"), "unknown"),
            llm_model=_as_str(llm_metadata.get("model")),
            llm_provider=_as_str((context or {}).get("llm_provider") or os.getenv("LLM_PROVIDER", "")),
            source=source,
            summary_text=summary_text,
        )
        return session.session_id
    except Exception as e:
        logger.warning(f"Failed to persist AI analysis session: {e}")
        return ""


@router.post("/analyze-log")
async def analyze_log(request: AnalyzeLogRequest) -> Dict[str, Any]:
    """
    分析单条日志（基于规则）

    基于日志内容、级别和服务信息，智能识别问题并提供：
    - 问题概述
    - 根因分析
    - 解决方案建议
    - 影响指标
    - 相似案例
    """
    try:
        analyzer = get_log_analyzer(storage)

        log_data = {
            'id': request.id,
            'timestamp': request.timestamp,
            'entity': request.entity,
            'event': request.event,
            'context': request.context
        }

        result = await _run_blocking(analyzer.analyze_log, log_data)
        normalized = _normalize_analysis_result(result)
        session_id = await _persist_analysis_session(
            analysis_type="log",
            service_name=_as_str(request.entity.get("name") if isinstance(request.entity, dict) else ""),
            input_text=_as_str(
                (request.event.get("raw") if isinstance(request.event, dict) else "")
                or (request.event.get("message") if isinstance(request.event, dict) else "")
            ),
            trace_id=_as_str((request.context or {}).get("trace_id")),
            context=request.context or {},
            result=normalized,
            source="api:/analyze-log",
        )
        if session_id:
            normalized["session_id"] = session_id
        return normalized

    except Exception as e:
        logger.error(f"Error analyzing log: {e}")
        raise HTTPException(status_code=500, detail="Internal server error")


@router.post("/analyze-log-llm")
async def analyze_log_llm(request: LLMAnalyzeRequest) -> Dict[str, Any]:
    """
    分析单条日志（使用 LLM 大模型）

    使用 GPT-4 或 Claude 等大模型进行深度分析：
    - 更准确的问题识别
    - 更详细的根因分析
    - 更专业的解决方案
    - 相似案例推荐

    需要配置 LLM_API_KEY（或 provider 对应 key）环境变量
    """
    try:
        llm_enabled = _is_llm_configured()
        
        if not llm_enabled or not request.use_llm:
            analyzer = get_log_analyzer(storage)
            log_data = {
                'id': 'llm-fallback',
                'timestamp': '',
                'entity': {'name': request.service_name},
                'event': {'level': 'error', 'raw': request.log_content},
                'context': request.context or {}
            }
            result = await _run_blocking(analyzer.analyze_log, log_data)
            normalized = _normalize_analysis_result(result, analysis_method="rule-based")
            session_id = await _persist_analysis_session(
                analysis_type="log",
                service_name=request.service_name,
                input_text=request.log_content,
                trace_id=_as_str((request.context or {}).get("trace_id")),
                context=request.context or {},
                result=normalized,
                source="api:/analyze-log-llm:rule",
            )
            if session_id:
                normalized["session_id"] = session_id
            return normalized

        llm_service = get_llm_service()
        
        result = await llm_service.analyze_log(
            log_content=request.log_content,
            service_name=request.service_name,
            context=request.context,
        )
        normalized = _normalize_analysis_result(result, analysis_method="llm")
        session_id = await _persist_analysis_session(
            analysis_type="log",
            service_name=request.service_name,
            input_text=request.log_content,
            trace_id=_as_str((request.context or {}).get("trace_id")),
            context=request.context or {},
            result=normalized,
            source="api:/analyze-log-llm",
        )
        if session_id:
            normalized["session_id"] = session_id
        return normalized

    except Exception as e:
        logger.error(f"Error analyzing log with LLM: {e}")
        raise HTTPException(status_code=500, detail="Internal server error")


@router.post("/analyze-trace")
async def analyze_trace(request: AnalyzeTraceRequest) -> Dict[str, Any]:
    """
    分析整个调用链（基于规则）

    分析 trace_id 对应的完整调用链，识别：
    - 异常服务
    - 慢操作
    - 性能瓶颈
    - 调用链问题
    """
    try:
        trace_id = (request.trace_id or "").strip()
        if not trace_id:
            raise HTTPException(status_code=400, detail="trace_id is required")

        analyzer = get_log_analyzer(storage)
        result = await _run_blocking(analyzer.analyze_trace, trace_id, storage)
        normalized = _normalize_analysis_result(result)
        session_id = await _persist_analysis_session(
            analysis_type="trace",
            service_name="",
            input_text=trace_id,
            trace_id=trace_id,
            context={"trace_id": trace_id},
            result=normalized,
            source="api:/analyze-trace",
        )
        if session_id:
            normalized["session_id"] = session_id
        return normalized

    except HTTPException:
        raise
    except Exception as e:
        logger.error(f"Error analyzing trace: {e}")
        raise HTTPException(status_code=500, detail="Internal server error")


@router.post("/analyze-trace-llm")
async def analyze_trace_llm(request: LLMTraceAnalyzeRequest) -> Dict[str, Any]:
    """
    分析调用链（使用 LLM 大模型）

    使用大模型进行深度链路分析
    """
    try:
        llm_enabled = _is_llm_configured()
        
        if not llm_enabled:
            normalized = _normalize_analysis_result(
                {"error": "LLM not configured"},
                analysis_method="none",
                fallback_description="请配置 LLM_API_KEY / OPENAI_API_KEY / DEEPSEEK_API_KEY / ANTHROPIC_API_KEY 环境变量以启用 LLM 分析",
            )
            session_id = await _persist_analysis_session(
                analysis_type="trace",
                service_name=request.service_name,
                input_text=request.trace_id,
                trace_id=request.trace_id,
                context={"trace_id": request.trace_id},
                result=normalized,
                source="api:/analyze-trace-llm:none",
            )
            if session_id:
                normalized["session_id"] = session_id
            return normalized

        llm_service = get_llm_service()
        
        result = await llm_service.analyze_trace(
            trace_data=request.trace_id,
            service_name=request.service_name,
        )
        normalized = _normalize_analysis_result(result, analysis_method="llm")
        session_id = await _persist_analysis_session(
            analysis_type="trace",
            service_name=request.service_name,
            input_text=request.trace_id,
            trace_id=request.trace_id,
            context={"trace_id": request.trace_id},
            result=normalized,
            source="api:/analyze-trace-llm",
        )
        if session_id:
            normalized["session_id"] = session_id
        return normalized

    except Exception as e:
        logger.error(f"Error analyzing trace with LLM: {e}")
        raise HTTPException(status_code=500, detail="Internal server error")


@router.get("/llm/runtime")
async def get_llm_runtime_status() -> Dict[str, Any]:
    """获取 LLM 运行时状态与预留配置契约。"""
    return _build_llm_runtime_status()


@router.post("/llm/runtime/validate")
async def validate_llm_runtime_config(request: LLMRuntimeConfig) -> Dict[str, Any]:
    """校验本地/远端 LLM 运行时配置结构（预留接口，不会落盘）。"""
    normalized = {
        "provider": _as_str(request.provider, "openai"),
        "model": _as_str(request.model),
        "api_base": _as_str(request.api_base),
        "local_model_path": _as_str(request.local_model_path),
        "extra": request.extra or {},
    }
    if normalized["provider"] not in SUPPORTED_LLM_PROVIDERS:
        raise HTTPException(status_code=400, detail="unsupported provider")

    return {
        "status": "ok",
        "validated": True,
        "runtime": normalized,
        "note": "当前仅校验参数结构；后续可将该配置接入本地 LLM 动态路由能力。",
    }


@router.post("/llm/runtime/update")
async def update_llm_runtime_config(request: LLMRuntimeUpdateRequest) -> Dict[str, Any]:
    """更新 LLM 运行时配置（当前进程生效，支持 API key 更新）。"""
    normalized = {
        "provider": _as_str(request.provider, "openai"),
        "model": _as_str(request.model),
        "api_base": _as_str(request.api_base),
        "api_key": _as_str(request.api_key),
        "local_model_path": _as_str(request.local_model_path),
        "clear_api_key": bool(request.clear_api_key),
        "persist_to_deployment": bool(request.persist_to_deployment),
        "extra": request.extra or {},
    }

    if normalized["provider"] not in SUPPORTED_LLM_PROVIDERS:
        raise HTTPException(status_code=400, detail="unsupported provider")

    _apply_llm_runtime_update(normalized)
    deployment_file = _resolve_llm_deployment_file_path(normalized["extra"])
    persistence_result = {
        "persisted": False,
        "deployment_file": deployment_file,
        "updated_keys": [],
        "added_keys": [],
        "error": "deployment persistence disabled",
    }
    if normalized["persist_to_deployment"]:
        persistence_result = _persist_llm_runtime_to_deployment_file(normalized, deployment_file)

    reset_llm_service()

    note = "配置已更新到当前进程运行时。"
    if normalized["persist_to_deployment"]:
        if persistence_result.get("persisted"):
            note += " 已同步写入部署文件。"
        else:
            note += (
                " 部署文件持久化失败，当前仍仅进程生效；"
                f"原因: {persistence_result.get('error') or 'unknown'}。"
            )
    else:
        note += " 已跳过部署文件持久化。"

    return {
        "status": "ok",
        "updated": True,
        "runtime": {
            "provider": normalized["provider"],
            "model": normalized["model"],
            "api_base": normalized["api_base"],
            "local_model_path": normalized["local_model_path"],
            "api_key_updated": bool(normalized["api_key"]),
            "clear_api_key": normalized["clear_api_key"],
            "persist_to_deployment": normalized["persist_to_deployment"],
            "extra": normalized["extra"],
        },
        "deployment_persistence": persistence_result,
        "runtime_status": _build_llm_runtime_status(),
        "note": note,
    }


@router.get("/kb/runtime")
async def get_kb_runtime_status() -> Dict[str, Any]:
    """获取远端知识库运行时配置与连通状态。"""
    return _build_kb_runtime_status(force_refresh_provider_status=True)


@router.post("/kb/runtime/validate")
async def validate_kb_runtime_config(request: KBRemoteRuntimeConfig) -> Dict[str, Any]:
    """校验远端知识库运行时参数（不落盘）。"""
    normalized = _normalize_kb_runtime_config(request)
    if normalized["provider"] != "disabled" and not _as_str(normalized["base_url"]):
        raise HTTPException(status_code=400, detail="base_url is required when provider is enabled")

    return {
        "status": "ok",
        "validated": True,
        "runtime": {
            "provider": normalized["provider"],
            "base_url": normalized["base_url"],
            "api_key_updated": bool(normalized["api_key"]),
            "clear_api_key": normalized["clear_api_key"],
            "timeout_seconds": normalized["timeout_seconds"],
            "health_path": normalized["health_path"],
            "search_path": normalized["search_path"],
            "upsert_path": normalized["upsert_path"],
            "outbox_enabled": normalized["outbox_enabled"],
            "outbox_poll_seconds": normalized["outbox_poll_seconds"],
            "outbox_max_attempts": normalized["outbox_max_attempts"],
            "persist_to_deployment": normalized["persist_to_deployment"],
            "extra": normalized["extra"],
        },
        "note": "参数结构校验通过；若 provider=ragflow，默认路径可按企业网关适配后调整。",
    }


@router.post("/kb/runtime/update")
async def update_kb_runtime_config(request: KBRemoteRuntimeConfig) -> Dict[str, Any]:
    """更新远端知识库运行时配置（支持 RAGFlow 默认配置）。"""
    normalized = _normalize_kb_runtime_config(request)
    if normalized["provider"] != "disabled" and not _as_str(normalized["base_url"]):
        raise HTTPException(status_code=400, detail="base_url is required when provider is enabled")
    if normalized["api_key"] and normalized["clear_api_key"]:
        raise HTTPException(status_code=400, detail="api_key and clear_api_key cannot both be set")

    _apply_kb_runtime_update(normalized)

    deployment_file = _resolve_kb_deployment_file_path(normalized["extra"])
    persistence_result = {
        "persisted": False,
        "deployment_file": deployment_file,
        "updated_keys": [],
        "added_keys": [],
        "error": "deployment persistence disabled",
    }
    if normalized["persist_to_deployment"]:
        persistence_result = _persist_kb_runtime_to_deployment_file(normalized, deployment_file)

    # 远端 KB 配置更新后重建网关，确保 provider/outbox 参数实时生效。
    gateway = reload_knowledge_gateway(storage)
    gateway.start_outbox_worker()

    note = "KB 运行时配置已更新到当前进程。"
    if normalized["persist_to_deployment"]:
        if persistence_result.get("persisted"):
            note += " 已同步写入部署文件。"
        else:
            note += (
                " 部署文件持久化失败，当前仍仅进程生效；"
                f"原因: {persistence_result.get('error') or 'unknown'}。"
            )
    else:
        note += " 已跳过部署文件持久化。"

    return {
        "status": "ok",
        "updated": True,
        "runtime": {
            "provider": normalized["provider"],
            "base_url": normalized["base_url"],
            "api_key_updated": bool(normalized["api_key"]),
            "clear_api_key": normalized["clear_api_key"],
            "timeout_seconds": normalized["timeout_seconds"],
            "health_path": normalized["health_path"],
            "search_path": normalized["search_path"],
            "upsert_path": normalized["upsert_path"],
            "outbox_enabled": normalized["outbox_enabled"],
            "outbox_poll_seconds": normalized["outbox_poll_seconds"],
            "outbox_max_attempts": normalized["outbox_max_attempts"],
            "persist_to_deployment": normalized["persist_to_deployment"],
            "extra": normalized["extra"],
        },
        "deployment_persistence": persistence_result,
        "runtime_status": _build_kb_runtime_status(force_refresh_provider_status=True),
        "note": note,
    }


class SimilarCasesRequest(BaseModel):
    """相似案例查询请求"""
    log_content: str
    service_name: str = ""
    problem_type: str = ""
    context: Dict[str, Any] = {}
    limit: int = 5
    include_draft: bool = False


class SaveCaseRequest(BaseModel):
    """保存案例请求"""
    problem_type: str
    severity: str
    summary: str
    log_content: str
    service_name: str = ""
    root_causes: List[str] = []
    solutions: List[Dict[str, Any]] = []
    context: Dict[str, Any] = {}
    tags: List[str] = []
    llm_provider: str = ""
    llm_model: str = ""
    llm_metadata: Dict[str, Any] = {}
    source: str = "manual"
    save_mode: str = "local_only"
    remote_enabled: bool = False


class ResolveCaseRequest(BaseModel):
    """标记案例已解决请求"""
    resolution: str = ""


class KBRuntimeOptionsRequest(BaseModel):
    """知识库运行时策略请求。"""
    remote_enabled: bool = False
    retrieval_mode: str = "local"
    save_mode: str = "local_only"


class KBSearchRequest(BaseModel):
    """统一知识检索请求。"""
    query: str
    service_name: str = ""
    problem_type: str = ""
    top_k: int = 5
    retrieval_mode: str = "local"
    include_draft: bool = False


class KBFromAnalysisSessionRequest(BaseModel):
    """从分析会话生成知识草稿请求。"""
    analysis_session_id: str
    include_followup: bool = True
    history: List[Dict[str, Any]] = []
    use_llm: bool = True
    save_mode: str = "local_only"
    remote_enabled: bool = False


class ManualRemediationRequest(BaseModel):
    """人工修复步骤更新请求。"""
    manual_remediation_steps: List[str]
    verification_result: str
    verification_notes: str
    final_resolution: str = ""
    save_mode: str = "local_only"
    remote_enabled: bool = False


class UpdateCaseContentRequest(BaseModel):
    """更新知识库内容请求。"""
    problem_type: Optional[str] = None
    severity: Optional[str] = None
    summary: Optional[str] = None
    service_name: Optional[str] = None
    root_causes: Optional[List[str]] = None
    solutions: Optional[List[Dict[str, Any]]] = None
    solutions_text: Optional[str] = None
    analysis_summary: Optional[str] = None
    resolution: Optional[str] = None
    tags: Optional[List[str]] = None
    save_mode: str = "local_only"
    remote_enabled: bool = False


class KBSolutionOptimizeRequest(BaseModel):
    """知识库解决建议文本优化请求。"""
    content: str
    summary: str = ""
    service_name: str = ""
    problem_type: str = ""
    severity: str = "medium"
    use_llm: bool = True


class FollowUpMessage(BaseModel):
    """追问消息"""
    role: str
    content: str
    timestamp: Optional[str] = None
    message_id: Optional[str] = None
    metadata: Dict[str, Any] = {}


class FollowUpRequest(BaseModel):
    """追问请求"""
    question: str
    analysis_session_id: str = ""
    conversation_id: str = ""
    use_llm: bool = True
    analysis_context: Dict[str, Any] = {}
    history: List[FollowUpMessage] = []
    reset: bool = False


class HistorySessionUpdateRequest(BaseModel):
    """AI 历史会话更新请求（重命名/Pin/归档）。"""
    title: Optional[str] = None
    is_pinned: Optional[bool] = None
    is_archived: Optional[bool] = None
    status: Optional[str] = None


class FollowUpActionRequest(BaseModel):
    """将回答转换为可执行动作。"""
    action_type: str
    title: str = ""
    extra: Dict[str, Any] = {}


@router.post("/similar-cases")
async def find_similar_cases(request: SimilarCasesRequest) -> Dict[str, Any]:
    """
    查找相似案例

    基于日志内容、服务名称和问题类型，检索历史相似案例
    """
    try:
        from ai.similar_cases import get_recommender, get_case_store

        recommender = get_recommender(storage)
        case_store = get_case_store(storage)

        query_kwargs: Dict[str, Any] = {
            "log_content": request.log_content,
            "service_name": request.service_name,
            "problem_type": request.problem_type,
            "context": request.context or {},
            "limit": request.limit,
            "min_similarity": 0.2,
        }
        if request.include_draft:
            query_kwargs["include_draft"] = True

        results = recommender.find_similar_cases(**query_kwargs)

        items: List[Dict[str, Any]] = []
        for r in results:
            content_history_recent = _case_store_list_change_history(
                case_store,
                r.case.id,
                limit=3,
                event_type="content_update",
            )
            content_history_count = _case_store_count_change_history(
                case_store,
                r.case.id,
                event_type="content_update",
            )
            if content_history_count <= 0:
                content_history_count = len(_as_list((r.case.llm_metadata or {}).get("content_update_history")))
            items.append(
                {
                    "id": r.case.id,
                    "problem_type": r.case.problem_type,
                    "severity": r.case.severity,
                    "summary": r.case.summary,
                    "service_name": r.case.service_name,
                    "root_causes": r.case.root_causes,
                    "solutions": r.case.solutions,
                    "resolved": r.case.resolved,
                    "resolution": r.case.resolution,
                    "tags": r.case.tags,
                    "case_status": _get_case_status(r.case),
                    "similarity_score": r.similarity_score,
                    "matched_features": r.matched_features,
                    "relevance_reason": r.relevance_reason,
                    "content_update_history_count": content_history_count,
                    "content_update_history_recent": content_history_recent,
                }
            )
        return {
            "cases": items,
            "total": len(items),
            "query": {
                "service_name": request.service_name,
                "problem_type": request.problem_type,
            }
        }

    except Exception as e:
        logger.error(f"Error finding similar cases: {e}")
        raise HTTPException(status_code=500, detail="Internal server error")


@router.post("/cases")
async def save_case(request: SaveCaseRequest) -> Dict[str, Any]:
    """
    保存新案例到案例库

    将分析结果保存为历史案例，供后续相似案例检索使用
    """
    try:
        from ai.similar_cases import get_case_store, Case, FeatureExtractor
        import uuid

        case_store = get_case_store(storage)
        gateway = get_knowledge_gateway(storage)
        save_mode = request.save_mode if request.save_mode in {"local_only", "local_and_remote"} else "local_only"
        remote_enabled = bool(request.remote_enabled)
        runtime_options = gateway.resolve_runtime_options(
            remote_enabled=remote_enabled,
            retrieval_mode="local",
            save_mode=save_mode,
        )
        effective_save_mode = _as_str(runtime_options.get("effective_save_mode"), "local_only")

        llm_metadata = request.llm_metadata or {}
        if not isinstance(llm_metadata, dict):
            llm_metadata = {}
        llm_metadata = dict(llm_metadata)
        llm_metadata.setdefault("case_status", "archived")
        llm_metadata.setdefault("knowledge_version", 1)
        llm_metadata.setdefault("verification_result", "")
        llm_metadata.setdefault("verification_notes", "")
        llm_metadata.setdefault("manual_remediation_steps", [])
        llm_metadata.setdefault("remediation_history", [])
        llm_metadata.setdefault("content_update_history", [])
        llm_metadata.setdefault("sync_status", "not_requested")
        llm_metadata.setdefault("external_doc_id", "")
        llm_metadata.setdefault("sync_error", "")
        llm_metadata.setdefault("sync_error_code", "")

        case = Case(
            id=f"case-{uuid.uuid4().hex[:8]}",
            problem_type=request.problem_type,
            severity=request.severity,
            summary=request.summary,
            log_content=request.log_content,
            service_name=request.service_name,
            root_causes=request.root_causes,
            solutions=request.solutions,
            context=request.context or {},
            tags=request.tags,
            created_at=datetime.now().isoformat(),
            llm_provider=request.llm_provider,
            llm_model=request.llm_model,
            llm_metadata=llm_metadata,
            source=request.source or "manual",
        )

        case.similarity_features = FeatureExtractor.extract_features(
            case.log_content,
            case.service_name,
            context=request.context or {},
        )

        remote_result = gateway.upsert_remote_with_outbox(
            _build_case_payload_for_remote(case),
            save_mode=effective_save_mode,
        )
        llm_meta_copy = case.llm_metadata if isinstance(case.llm_metadata, dict) else {}
        llm_meta_copy = dict(llm_meta_copy)
        llm_meta_copy["sync_status"] = _as_str(remote_result.get("sync_status"), "not_requested")
        llm_meta_copy["external_doc_id"] = _as_str(remote_result.get("external_doc_id"))
        llm_meta_copy["sync_error"] = _as_str(remote_result.get("sync_error"))
        llm_meta_copy["sync_error_code"] = _as_str(remote_result.get("sync_error_code"))
        case.llm_metadata = llm_meta_copy

        case_store.add_case(case)

        return {
            "id": case.id,
            "message": "Case saved successfully",
            "created_at": case.created_at,
            "effective_save_mode": effective_save_mode,
            "sync_status": llm_meta_copy.get("sync_status"),
            "external_doc_id": llm_meta_copy.get("external_doc_id"),
            "sync_error": llm_meta_copy.get("sync_error"),
            "sync_error_code": llm_meta_copy.get("sync_error_code"),
            "outbox_id": _as_str(remote_result.get("outbox_id")),
        }

    except Exception as e:
        logger.error(f"Error saving case: {e}")
        raise HTTPException(status_code=500, detail="Internal server error")


@router.get("/cases")
async def list_cases(
    problem_type: Optional[str] = None,
    service_name: Optional[str] = None,
    limit: int = 20
) -> Dict[str, Any]:
    """
    列出案例库中的案例
    """
    try:
        from ai.similar_cases import get_case_store

        case_store = get_case_store(storage)

        if problem_type:
            cases = case_store.get_cases_by_type(problem_type)
        elif service_name:
            cases = case_store.get_cases_by_service(service_name)
        else:
            cases = case_store.get_all_cases()

        cases = cases[:limit]

        case_items: List[Dict[str, Any]] = []
        for c in cases:
            content_history_count = _case_store_count_change_history(
                case_store,
                c.id,
                event_type="content_update",
            )
            if content_history_count <= 0:
                content_history_count = len(_as_list((c.llm_metadata or {}).get("content_update_history")))
            case_items.append(
                {
                    "id": c.id,
                    "problem_type": c.problem_type,
                    "severity": c.severity,
                    "summary": c.summary,
                    "service_name": c.service_name,
                    "resolved": c.resolved,
                    "resolution": c.resolution,
                    "tags": c.tags,
                    "created_at": c.created_at,
                    "updated_at": c.updated_at,
                    "resolved_at": c.resolved_at,
                    "source": c.source,
                    "llm_provider": c.llm_provider,
                    "llm_model": c.llm_model,
                    "case_status": _get_case_status(c),
                    "knowledge_version": int(_as_float((c.llm_metadata or {}).get("knowledge_version"), 1)),
                    "verification_result": _as_str((c.llm_metadata or {}).get("verification_result")),
                    "verification_notes": _as_str((c.llm_metadata or {}).get("verification_notes")),
                    "manual_remediation_steps": _as_list((c.llm_metadata or {}).get("manual_remediation_steps")),
                    "sync_status": _as_str((c.llm_metadata or {}).get("sync_status")),
                    "external_doc_id": _as_str((c.llm_metadata or {}).get("external_doc_id")),
                    "sync_error": _as_str((c.llm_metadata or {}).get("sync_error")),
                    "sync_error_code": _as_str((c.llm_metadata or {}).get("sync_error_code")),
                    "last_editor": _as_str((c.llm_metadata or {}).get("last_editor")),
                    "remediation_history": _as_list((c.llm_metadata or {}).get("remediation_history")),
                    "content_update_history_count": content_history_count,
                }
            )
        return {
            "cases": case_items,
            "total": len(case_items),
        }

    except Exception as e:
        logger.error(f"Error listing cases: {e}")
        raise HTTPException(status_code=500, detail="Internal server error")


@router.get("/history")
async def list_ai_history(
    limit: int = 20,
    offset: int = 0,
    analysis_type: Optional[str] = None,
    service_name: Optional[str] = None,
    q: Optional[str] = Query(default=None, description="按会话标题/输入/追问内容搜索"),
    include_archived: bool = False,
    pinned_first: bool = True,
    sort_by: str = Query(
        default="updated_at",
        description="排序字段: updated_at|created_at|title|service_name|analysis_type",
    ),
    sort_order: str = Query(default="desc", description="排序方向: asc|desc"),
) -> Dict[str, Any]:
    """列出 AI 分析会话历史。"""
    try:
        session_store = get_ai_session_store(storage)
        safe_limit = max(1, int(limit))
        safe_offset = max(0, int(offset))
        safe_sort_by = (
            sort_by.strip().lower()
            if sort_by and sort_by.strip().lower() in ALLOWED_SESSION_SORT_FIELDS
            else "updated_at"
        )
        safe_sort_order = (
            sort_order.strip().lower()
            if sort_order and sort_order.strip().lower() in ALLOWED_SESSION_SORT_ORDERS
            else "desc"
        )
        total_all = await _run_blocking(
            session_store.count_sessions,
            analysis_type=analysis_type or "",
            service_name=service_name or "",
            include_archived=include_archived,
            search_query=q or "",
        )
        sessions = await _run_blocking(
            session_store.list_sessions,
            limit=safe_limit,
            offset=safe_offset,
            analysis_type=analysis_type or "",
            service_name=service_name or "",
            include_archived=include_archived,
            search_query=q or "",
            pinned_first=pinned_first,
            sort_by=safe_sort_by,
            sort_order=safe_sort_order,
        )
        session_ids = [session.session_id for session in sessions]
        message_counts = await _run_blocking(session_store.get_message_counts, session_ids)
        items: List[Dict[str, Any]] = []
        for session in sessions:
            session_result = session.result if isinstance(session.result, dict) else {}
            normalized = session_result.get("raw") if isinstance(session_result.get("raw"), dict) else {}
            overview = normalized.get("overview") if isinstance(normalized, dict) else {}
            summary = _as_str(
                (overview.get("description") if isinstance(overview, dict) else "")
                or session_result.get("summary")
                or session.input_text[:120]
            )
            items.append(
                {
                    "session_id": session.session_id,
                    "analysis_type": session.analysis_type,
                    "title": session.title,
                    "service_name": session.service_name,
                    "trace_id": session.trace_id,
                    "summary": summary,
                    "summary_text": _as_str(session.summary_text, summary),
                    "analysis_method": session.analysis_method,
                    "llm_model": session.llm_model,
                    "llm_provider": session.llm_provider,
                    "source": session.source,
                    "status": session.status,
                    "created_at": session.created_at,
                    "updated_at": session.updated_at,
                    "is_pinned": bool(session.is_pinned),
                    "is_archived": bool(session.is_archived),
                    "message_count": int(message_counts.get(session.session_id, 0)),
                }
            )

        return {
            "sessions": items,
            "total": len(items),
            "total_all": total_all,
            "limit": safe_limit,
            "offset": safe_offset,
            "has_more": safe_offset + len(items) < total_all,
            "sort": {
                "sort_by": safe_sort_by,
                "sort_order": safe_sort_order,
                "pinned_first": pinned_first,
            },
        }
    except Exception as e:
        logger.error(f"Error listing AI history: {e}")
        raise HTTPException(status_code=500, detail="Internal server error")


@router.patch("/history/{session_id}")
async def update_ai_history_session(session_id: str, request: HistorySessionUpdateRequest) -> Dict[str, Any]:
    """更新 AI 历史会话元信息（重命名、Pin、归档、状态）。"""
    try:
        session_store = get_ai_session_store(storage)
        existing = await _run_blocking(session_store.get_session, session_id)
        if not existing:
            raise HTTPException(status_code=404, detail="session not found")

        changes: Dict[str, Any] = {}
        if request.title is not None:
            changes["title"] = _as_str(request.title)[:180]
        if request.is_pinned is not None:
            changes["is_pinned"] = bool(request.is_pinned)
        if request.is_archived is not None:
            changes["is_archived"] = bool(request.is_archived)
        if request.status is not None:
            changes["status"] = _as_str(request.status, existing.status)

        if not changes:
            return {
                "status": "noop",
                "session_id": existing.session_id,
                "title": existing.title,
                "is_pinned": bool(existing.is_pinned),
                "is_archived": bool(existing.is_archived),
                "updated_at": existing.updated_at,
            }

        updated = await _run_blocking(session_store.update_session, session_id, **changes)
        if not updated:
            raise HTTPException(status_code=404, detail="session not found")

        return {
            "status": "ok",
            "session_id": updated.session_id,
            "title": updated.title,
            "is_pinned": bool(updated.is_pinned),
            "is_archived": bool(updated.is_archived),
            "state": updated.status,
            "updated_at": updated.updated_at,
        }
    except HTTPException:
        raise
    except Exception as e:
        logger.error(f"Error updating AI history session: {e}")
        raise HTTPException(status_code=500, detail="Internal server error")


@router.delete("/history/{session_id}")
async def delete_ai_history_session(session_id: str) -> Dict[str, Any]:
    """删除 AI 历史会话（软删除）。"""
    try:
        session_store = get_ai_session_store(storage)
        deleted = await _run_blocking(session_store.delete_session, session_id)
        if not deleted:
            raise HTTPException(status_code=404, detail="session not found")
        return {
            "status": "ok",
            "session_id": session_id,
            "message": "session deleted",
        }
    except HTTPException:
        raise
    except Exception as e:
        logger.error(f"Error deleting AI history session: {e}")
        raise HTTPException(status_code=500, detail="Internal server error")


@router.delete("/history/{session_id}/messages/{message_id}")
async def delete_ai_history_message(session_id: str, message_id: str) -> Dict[str, Any]:
    """删除会话中的单条消息（逻辑删除）。"""
    try:
        session_store = get_ai_session_store(storage)
        deleted = await _run_blocking(session_store.delete_message, session_id, message_id)
        if not deleted:
            raise HTTPException(status_code=404, detail="message not found")
        remaining_count = await _run_blocking(session_store.get_message_count, session_id)
        return {
            "status": "ok",
            "session_id": session_id,
            "message_id": message_id,
            "remaining_message_count": int(remaining_count),
            "message": "history message deleted",
        }
    except HTTPException:
        raise
    except Exception as e:
        logger.error(f"Error deleting AI history message: {e}")
        raise HTTPException(status_code=500, detail="Internal server error")


@router.get("/history/{session_id}")
async def get_ai_history_detail(session_id: str) -> Dict[str, Any]:
    """获取 AI 分析会话详情（请求、分析结果、追问消息）。"""
    try:
        session_store = get_ai_session_store(storage)
        payload = await _run_blocking(session_store.get_session_with_messages, session_id)
        if not payload:
            raise HTTPException(status_code=404, detail="session not found")

        session = payload.get("session") if isinstance(payload, dict) else {}
        messages = payload.get("messages") if isinstance(payload, dict) else []
        result_container = session.get("result") if isinstance(session, dict) else {}
        raw_result = result_container.get("raw") if isinstance(result_container, dict) else {}
        analysis_result = raw_result if isinstance(raw_result, dict) else {}

        return {
            "session_id": session.get("session_id"),
            "analysis_type": session.get("analysis_type"),
            "title": session.get("title"),
            "service_name": session.get("service_name"),
            "trace_id": session.get("trace_id"),
            "input_text": session.get("input_text"),
            "context": session.get("context") if isinstance(session.get("context"), dict) else {},
            "result": analysis_result,
            "summary": _as_str((result_container or {}).get("summary")),
            "summary_text": _as_str(session.get("summary_text"), _as_str((result_container or {}).get("summary"))),
            "analysis_method": session.get("analysis_method"),
            "llm_model": session.get("llm_model"),
            "llm_provider": session.get("llm_provider"),
            "source": session.get("source"),
            "status": session.get("status"),
            "created_at": session.get("created_at"),
            "updated_at": session.get("updated_at"),
            "is_pinned": bool(session.get("is_pinned")),
            "is_archived": bool(session.get("is_archived")),
            "message_count": payload.get("message_count", len(messages)),
            "context_pills": _build_context_pills(
                {
                    "analysis_type": session.get("analysis_type"),
                    "service_name": session.get("service_name"),
                    "trace_id": session.get("trace_id"),
                    "input_text": session.get("input_text"),
                    "result": analysis_result,
                },
                analysis_session_id=_as_str(session.get("session_id")),
            ),
            "messages": [
                {
                    "message_id": item.get("message_id"),
                    "role": item.get("role"),
                    "content": item.get("content"),
                    "timestamp": item.get("created_at"),
                    "metadata": item.get("metadata") if isinstance(item.get("metadata"), dict) else {},
                }
                for item in messages
                if isinstance(item, dict)
            ],
        }
    except HTTPException:
        raise
    except Exception as e:
        logger.error(f"Error getting AI history detail: {e}")
        raise HTTPException(status_code=500, detail="Internal server error")


@router.post("/history/{session_id}/messages/{message_id}/actions")
async def create_followup_action(
    session_id: str,
    message_id: str,
    request: FollowUpActionRequest,
) -> Dict[str, Any]:
    """将某条回答一键转换为工单/Runbook/告警抑制建议。"""
    try:
        session_store = get_ai_session_store(storage)
        payload = await _run_blocking(session_store.get_session_with_messages, session_id)
        if not payload:
            raise HTTPException(status_code=404, detail="session not found")
        session = payload.get("session") if isinstance(payload, dict) else {}
        target_message = await _run_blocking(session_store.get_message_by_id, session_id, message_id)
        if not target_message:
            raise HTTPException(status_code=404, detail="message not found")
        if _as_str(target_message.role) != "assistant":
            raise HTTPException(status_code=400, detail="only assistant message can generate action")

        draft = _build_followup_action_draft(
            action_type=request.action_type,
            message_content=target_message.content,
            session=session if isinstance(session, dict) else {},
            preferred_title=request.title,
            extra=request.extra or {},
        )
        action_id = f"act-{uuid.uuid4().hex[:12]}"
        action_payload = {
            "action_id": action_id,
            "message_id": message_id,
            "action": draft,
            "created_at": datetime.utcnow().isoformat() + "Z",
        }

        session_context = session.get("context") if isinstance(session.get("context"), dict) else {}
        drafts = session_context.get("action_drafts") if isinstance(session_context.get("action_drafts"), list) else []
        drafts.append(action_payload)
        session_context["action_drafts"] = drafts[-50:]
        await _run_blocking(session_store.update_session, session_id, context=session_context)

        return {
            "status": "ok",
            "session_id": session_id,
            "message_id": message_id,
            "action_id": action_id,
            "action": draft,
        }
    except HTTPException:
        raise
    except Exception as e:
        logger.error(f"Error creating follow-up action: {e}")
        raise HTTPException(status_code=500, detail="Internal server error")


@router.get("/cases/{case_id}")
async def get_case_detail(case_id: str) -> Dict[str, Any]:
    """获取案例详情（含可回放到 AI 分析页的分析结果结构）。"""
    try:
        from ai.similar_cases import get_case_store

        case_store = get_case_store(storage)
        case_obj = case_store.get_case(case_id)
        if not case_obj:
            raise HTTPException(status_code=404, detail="Case not found")
        content_history = _case_store_list_change_history(
            case_store,
            case_id=case_obj.id,
            limit=120,
            event_type="content_update",
        )
        if not content_history:
            # 兼容旧版本：历史仍可能保存在 llm_metadata 内。
            content_history = _as_list((case_obj.llm_metadata or {}).get("content_update_history"))
        content_history_count = _case_store_count_change_history(
            case_store,
            case_id=case_obj.id,
            event_type="content_update",
        )
        if content_history_count <= 0:
            content_history_count = len(_as_list(content_history))

        return {
            "id": case_obj.id,
            "problem_type": case_obj.problem_type,
            "severity": case_obj.severity,
            "summary": case_obj.summary,
            "log_content": case_obj.log_content,
            "service_name": case_obj.service_name,
            "root_causes": case_obj.root_causes,
            "solutions": case_obj.solutions,
            "context": case_obj.context,
            "resolved": case_obj.resolved,
            "resolution": case_obj.resolution,
            "tags": case_obj.tags,
            "created_at": case_obj.created_at,
            "updated_at": case_obj.updated_at,
            "resolved_at": case_obj.resolved_at,
            "llm_provider": case_obj.llm_provider,
            "llm_model": case_obj.llm_model,
            "llm_metadata": case_obj.llm_metadata,
            "source": case_obj.source,
            "case_status": _get_case_status(case_obj),
            "knowledge_version": int(_as_float((case_obj.llm_metadata or {}).get("knowledge_version"), 1)),
            "manual_remediation_steps": _as_list((case_obj.llm_metadata or {}).get("manual_remediation_steps")),
            "verification_result": _as_str((case_obj.llm_metadata or {}).get("verification_result")),
            "verification_notes": _as_str((case_obj.llm_metadata or {}).get("verification_notes")),
            "analysis_summary": _as_str((case_obj.llm_metadata or {}).get("analysis_summary"), case_obj.summary),
            "sync_status": _as_str((case_obj.llm_metadata or {}).get("sync_status")),
            "external_doc_id": _as_str((case_obj.llm_metadata or {}).get("external_doc_id")),
            "sync_error": _as_str((case_obj.llm_metadata or {}).get("sync_error")),
            "sync_error_code": _as_str((case_obj.llm_metadata or {}).get("sync_error_code")),
            "last_editor": _as_str((case_obj.llm_metadata or {}).get("last_editor")),
            "remediation_history": _as_list((case_obj.llm_metadata or {}).get("remediation_history")),
            "content_update_history": _as_list(content_history),
            "content_update_history_count": int(content_history_count),
            "analysis_result": _build_case_analysis_result(case_obj),
        }
    except HTTPException:
        raise
    except Exception as e:
        logger.error(f"Error getting case detail: {e}")
        raise HTTPException(status_code=500, detail="Internal server error")


@router.patch("/cases/{case_id}")
async def update_case_content(case_id: str, request: UpdateCaseContentRequest) -> Dict[str, Any]:
    """更新知识库内容（摘要、根因、方案等），并支持远端同步策略。"""
    try:
        from ai.similar_cases import get_case_store, Case, FeatureExtractor

        case_store = get_case_store(storage)
        existing = case_store.get_case(case_id)
        if not existing:
            raise HTTPException(status_code=404, detail={"code": "KBR-005", "message": "case not found"})

        requested_fields = _collect_requested_content_fields(request)
        if not requested_fields:
            raise HTTPException(
                status_code=400,
                detail={"code": "KBR-003", "message": "at least one editable field is required"},
            )

        updated = Case(**existing.to_dict())
        updated.problem_type = (
            _as_str(request.problem_type, updated.problem_type).lower()
            if request.problem_type is not None
            else updated.problem_type
        )
        updated.severity = (
            _normalize_kb_draft_severity(request.severity, default=updated.severity or "medium")
            if request.severity is not None
            else updated.severity
        )
        if request.summary is not None:
            updated.summary = _truncate_text(_as_str(request.summary), 1000)
        if request.service_name is not None:
            updated.service_name = _truncate_text(_as_str(request.service_name), 160)
        if request.root_causes is not None:
            updated.root_causes = _normalize_string_list(request.root_causes, max_items=12, min_length=2)
        if request.solutions_text is not None:
            updated.solutions = _normalize_solutions_from_text(request.solutions_text)
        elif request.solutions is not None:
            updated.solutions = _normalize_solutions(request.solutions)
        if request.resolution is not None:
            updated.resolution = _truncate_text(_as_str(request.resolution), 2000)
        if request.tags is not None:
            updated.tags = _normalize_string_list(request.tags, max_items=20, min_length=1)

        if not updated.problem_type:
            raise HTTPException(status_code=400, detail={"code": "KBR-002", "message": "problem_type must not be empty"})
        if not updated.summary:
            raise HTTPException(status_code=400, detail={"code": "KBR-002", "message": "summary must not be empty"})
        if not updated.service_name:
            raise HTTPException(status_code=400, detail={"code": "KBR-002", "message": "service_name must not be empty"})

        existing_metadata = existing.llm_metadata if isinstance(existing.llm_metadata, dict) else {}
        previous_analysis_summary = _as_str(existing_metadata.get("analysis_summary"), existing.summary)
        llm_metadata = updated.llm_metadata if isinstance(updated.llm_metadata, dict) else {}
        llm_metadata = dict(llm_metadata)
        knowledge_version = int(_as_float(llm_metadata.get("knowledge_version", 1), 1)) + 1
        llm_metadata["knowledge_version"] = knowledge_version
        llm_metadata["last_editor"] = "manual_content"
        llm_metadata["analysis_summary"] = _truncate_text(
            _as_str(request.analysis_summary, _as_str(llm_metadata.get("analysis_summary"), updated.summary)),
            1200,
        )
        if not _as_str(llm_metadata.get("case_status")):
            llm_metadata["case_status"] = _get_case_status(existing)
        updated.llm_metadata = llm_metadata
        updated.knowledge_version = knowledge_version
        updated.last_editor = "manual_content"
        updated.updated_at = datetime.utcnow().isoformat() + "Z"
        updated.source = existing.source or "manual"
        updated.similarity_features = FeatureExtractor.extract_features(
            updated.log_content,
            updated.service_name,
            context=updated.context or {},
        )

        gateway = get_knowledge_gateway(storage)
        runtime_options = gateway.resolve_runtime_options(
            remote_enabled=bool(request.remote_enabled),
            retrieval_mode="local",
            save_mode=_as_str(request.save_mode, "local_only"),
        )
        effective_save_mode = _as_str(runtime_options.get("effective_save_mode"), "local_only")
        remote_result = gateway.upsert_remote_with_outbox(
            _build_case_payload_for_remote(updated),
            save_mode=effective_save_mode,
        )
        llm_metadata["sync_status"] = _as_str(remote_result.get("sync_status"), "not_requested")
        llm_metadata["external_doc_id"] = _as_str(remote_result.get("external_doc_id"))
        llm_metadata["sync_error"] = _as_str(remote_result.get("sync_error"))
        llm_metadata["sync_error_code"] = _as_str(remote_result.get("sync_error_code"))

        change_summary = _build_case_content_change_summary(
            existing_case=existing,
            updated_case=updated,
            previous_analysis_summary=previous_analysis_summary,
            current_analysis_summary=_as_str(llm_metadata.get("analysis_summary")),
        )
        changed_fields = [str(field) for field in _as_list(change_summary.get("changed_fields"))]
        unchanged_requested_fields = [field for field in requested_fields if field not in changed_fields]
        no_effective_change_reason = ""
        if not changed_fields and requested_fields:
            no_effective_change_reason = "submitted_values_equivalent_after_normalization"
        history_entry = {
            "event_type": "content_update",
            "version": knowledge_version,
            "updated_at": updated.updated_at,
            "editor": "manual_content",
            "changed_fields": changed_fields,
            "changes": change_summary.get("changes", {}),
            "requested_fields": requested_fields,
            "unchanged_requested_fields": unchanged_requested_fields,
            "no_effective_change_reason": no_effective_change_reason,
            "effective_save_mode": effective_save_mode,
            "sync_status": llm_metadata.get("sync_status"),
            "sync_error_code": llm_metadata.get("sync_error_code"),
            "source": "api:/ai/cases/update",
            "note": (
                "manual_content_update_no_effective_change"
                if not changed_fields
                else "manual_content_update"
            ),
        }
        updated.llm_metadata = llm_metadata

        case_store.update_case(updated)
        persisted_history = _case_store_append_change_history(case_store, updated.id, history_entry)
        content_update_history_count = _case_store_count_change_history(
            case_store,
            updated.id,
            event_type="content_update",
        )
        if content_update_history_count <= 0:
            content_update_history_count = len(_as_list((updated.llm_metadata or {}).get("content_update_history")))

        if changed_fields:
            changed_fields_text = "、".join(changed_fields)
            friendly_message = (
                f"知识库更新成功：版本 v{knowledge_version}，更新字段 {changed_fields_text}，"
                f"同步状态 {llm_metadata.get('sync_status') or 'unknown'}。"
            )
        else:
            requested_fields_text = "、".join(requested_fields) if requested_fields else "未识别"
            friendly_message = (
                f"知识库内容已校验：版本 v{knowledge_version}。本次提交字段 {requested_fields_text} "
                f"与当前内容等效（规范化后无差异），未产生有效字段变更；"
                f"同步状态 {llm_metadata.get('sync_status') or 'unknown'}。"
            )

        return {
            "status": "ok",
            "case_id": updated.id,
            "knowledge_version": knowledge_version,
            "effective_save_mode": effective_save_mode,
            "sync_status": llm_metadata.get("sync_status"),
            "external_doc_id": llm_metadata.get("external_doc_id"),
            "sync_error": llm_metadata.get("sync_error"),
            "sync_error_code": llm_metadata.get("sync_error_code"),
            "outbox_id": _as_str(remote_result.get("outbox_id")),
            "updated_at": updated.updated_at,
            "last_editor": llm_metadata.get("last_editor"),
            "analysis_summary": llm_metadata.get("analysis_summary"),
            "updated_fields": changed_fields,
            "requested_fields": requested_fields,
            "unchanged_requested_fields": unchanged_requested_fields,
            "no_effective_change_reason": no_effective_change_reason,
            "history_entry": persisted_history,
            "content_update_history_count": content_update_history_count,
            "friendly_message": friendly_message,
            "message": friendly_message,
        }
    except HTTPException:
        raise
    except Exception as e:
        logger.error(f"Error updating case content: {e}")
        raise HTTPException(status_code=500, detail={"code": "KBR-010", "message": "Internal server error"})


@router.delete("/cases/{case_id}")
async def delete_case(case_id: str) -> Dict[str, Any]:
    """删除案例（ClickHouse 模式为软删除）。"""
    try:
        from ai.similar_cases import get_case_store

        case_store = get_case_store(storage)
        deleted = case_store.delete_case(case_id)
        if not deleted:
            raise HTTPException(status_code=404, detail="Case not found")
        return {
            "status": "ok",
            "id": case_id,
            "message": "Case deleted",
        }
    except HTTPException:
        raise
    except Exception as e:
        logger.error(f"Error deleting case: {e}")
        raise HTTPException(status_code=500, detail="Internal server error")


@router.patch("/cases/{case_id}/resolve")
async def resolve_case(case_id: str, request: ResolveCaseRequest) -> Dict[str, Any]:
    """标记案例为已解决。"""
    try:
        from ai.similar_cases import get_case_store

        case_store = get_case_store(storage)
        updated = case_store.mark_case_resolved(case_id, request.resolution)
        if not updated:
            raise HTTPException(status_code=404, detail="Case not found")
        return {
            "status": "ok",
            "id": updated.id,
            "resolved": updated.resolved,
            "resolution": updated.resolution,
            "resolved_at": updated.resolved_at,
            "message": "Case marked as resolved",
        }
    except HTTPException:
        raise
    except Exception as e:
        logger.error(f"Error resolving case: {e}")
        raise HTTPException(status_code=500, detail="Internal server error")


@router.get("/kb/providers/status")
async def get_kb_providers_status() -> Dict[str, Any]:
    """获取知识库 provider 运行状态。"""
    try:
        gateway = get_knowledge_gateway(storage)
        return gateway.get_provider_status()
    except Exception as e:
        logger.error(f"Error getting KB providers status: {e}")
        raise HTTPException(status_code=500, detail={"code": "KBR-010", "message": "Internal server error"})


@router.get("/kb/outbox/status")
async def get_kb_outbox_status() -> Dict[str, Any]:
    """获取远端同步 Outbox 状态。"""
    try:
        gateway = get_knowledge_gateway(storage)
        return gateway.get_outbox_status()
    except Exception as e:
        logger.error(f"Error getting KB outbox status: {e}")
        raise HTTPException(status_code=500, detail={"code": "KBR-010", "message": "Internal server error"})


@router.post("/kb/runtime/options")
async def kb_runtime_options(request: KBRuntimeOptionsRequest) -> Dict[str, Any]:
    """解析前端开关与运行时策略，返回生效模式。"""
    try:
        gateway = get_knowledge_gateway(storage)
        resolved = gateway.resolve_runtime_options(
            remote_enabled=bool(request.remote_enabled),
            retrieval_mode=_as_str(request.retrieval_mode, "local"),
            save_mode=_as_str(request.save_mode, "local_only"),
        )
        warning_code = _as_str(resolved.get("warning_code"))
        if warning_code == "KBR-006":
            raise HTTPException(
                status_code=409,
                detail={
                    "code": "KBR-006",
                    "message": _as_str(resolved.get("message"), "remote provider not configured"),
                    "effective_retrieval_mode": _as_str(resolved.get("effective_retrieval_mode"), "local"),
                    "effective_save_mode": _as_str(resolved.get("effective_save_mode"), "local_only"),
                },
            )
        if warning_code == "KBR-007":
            raise HTTPException(
                status_code=503,
                detail={
                    "code": "KBR-007",
                    "message": _as_str(resolved.get("message"), "remote provider unavailable"),
                    "effective_retrieval_mode": _as_str(resolved.get("effective_retrieval_mode"), "local"),
                    "effective_save_mode": _as_str(resolved.get("effective_save_mode"), "local_only"),
                },
            )
        return resolved
    except HTTPException:
        raise
    except Exception as e:
        logger.error(f"Error resolving KB runtime options: {e}")
        raise HTTPException(status_code=500, detail={"code": "KBR-010", "message": "Internal server error"})


@router.post("/kb/search")
async def kb_search(request: KBSearchRequest) -> Dict[str, Any]:
    """统一知识库检索（本地/联合）。"""
    query = _as_str(request.query)
    if len(query) < 3:
        raise HTTPException(status_code=400, detail={"code": "KBR-001", "message": "query length must be >= 3"})

    try:
        gateway = get_knowledge_gateway(storage)
        runtime_options = gateway.resolve_runtime_options(
            remote_enabled=_as_str(request.retrieval_mode, "local") == "hybrid",
            retrieval_mode=_as_str(request.retrieval_mode, "local"),
            save_mode="local_only",
        )
        effective_mode = _as_str(runtime_options.get("effective_retrieval_mode"), "local")
        payload = gateway.search(
            query=query,
            service_name=_as_str(request.service_name),
            problem_type=_as_str(request.problem_type),
            top_k=max(1, min(int(request.top_k or 5), 20)),
            retrieval_mode=effective_mode,
            include_draft=bool(request.include_draft),
        )
        payload["effective_mode"] = effective_mode
        payload["message"] = _as_str(payload.get("warning_message") or runtime_options.get("message"))
        payload["warning_code"] = _as_str(payload.get("warning_code"))
        return payload
    except Exception as e:
        logger.error(f"Error searching KB: {e}")
        raise HTTPException(status_code=500, detail={"code": "KBR-010", "message": "Internal server error"})


@router.post("/kb/from-analysis-session")
async def kb_from_analysis_session(request: KBFromAnalysisSessionRequest) -> Dict[str, Any]:
    """从 AI 分析会话生成知识草稿。"""
    session_id = _as_str(request.analysis_session_id)
    if not session_id:
        raise HTTPException(status_code=400, detail={"code": "KBR-001", "message": "analysis_session_id is required"})

    try:
        session_store = get_ai_session_store(storage)
        payload = await _run_blocking(session_store.get_session_with_messages, session_id)
        if not payload:
            raise HTTPException(status_code=404, detail={"code": "KBR-004", "message": "analysis session not found"})

        session = payload.get("session") if isinstance(payload, dict) else {}
        messages = payload.get("messages") if isinstance(payload, dict) else []
        max_history_items = max(80, int(_as_float(os.getenv("AI_KB_DRAFT_HISTORY_MAX_ITEMS", 240), 240)))
        stored_history = _session_messages_to_conversation_history(_as_list(messages), max_items=max_history_items)
        client_history = _normalize_conversation_history(
            _mask_sensitive_payload(request.history or []),
            max_items=max_history_items,
        )
        merged_history_messages = stored_history
        if client_history and stored_history:
            merged_history_messages = _merge_conversation_history(
                stored_history,
                client_history,
                max_items=max_history_items,
            )
        elif client_history:
            merged_history_messages = client_history

        include_followup = bool(request.include_followup)
        llm_enabled = _is_llm_configured()
        llm_requested = bool(request.use_llm)
        draft_method = "rule-based"
        llm_fallback_reason = ""

        draft_case = _build_rule_based_kb_draft(
            session=session,
            messages=merged_history_messages,
            include_followup=include_followup,
        )
        missing_required_fields, confidence = _build_kb_draft_quality(draft_case)

        if llm_enabled and llm_requested:
            try:
                llm_draft_payload = await _build_llm_kb_draft(
                    session=session,
                    messages=merged_history_messages,
                    include_followup=include_followup,
                    fallback_draft=draft_case,
                )
                llm_draft_case = llm_draft_payload.get("draft_case") if isinstance(llm_draft_payload, dict) else {}
                if isinstance(llm_draft_case, dict) and llm_draft_case:
                    draft_case = llm_draft_case
                    missing_required_fields, confidence = _build_kb_draft_quality(
                        draft_case,
                        confidence_hint=_as_float(llm_draft_payload.get("confidence"), 0.88),
                    )
                    draft_method = "llm"
                else:
                    llm_fallback_reason = "llm_empty_draft"
            except asyncio.TimeoutError:
                llm_fallback_reason = "llm_timeout"
            except ValueError:
                llm_fallback_reason = "llm_parse_error"
            except Exception as e:
                logger.warning(f"LLM kb draft generation failed, fallback to rule-based: {e}")
                llm_fallback_reason = "llm_error"
        else:
            if llm_enabled and not llm_requested:
                llm_fallback_reason = "llm_disabled_by_user"
            elif not llm_enabled:
                llm_fallback_reason = "llm_unavailable"

        gateway = get_knowledge_gateway(storage)
        runtime_options = gateway.resolve_runtime_options(
            remote_enabled=bool(request.remote_enabled),
            retrieval_mode="local",
            save_mode=_as_str(request.save_mode, "local_only"),
        )
        response = {
            "draft_case": draft_case,
            "missing_required_fields": missing_required_fields,
            "confidence": confidence,
            "save_mode_effective": _as_str(runtime_options.get("effective_save_mode"), "local_only"),
            "draft_method": draft_method,
            "llm_enabled": llm_enabled,
            "llm_requested": llm_requested,
        }
        if llm_fallback_reason:
            response["llm_fallback_reason"] = llm_fallback_reason
        return response
    except HTTPException:
        raise
    except Exception as e:
        logger.error(f"Error generating KB draft from analysis session: {e}")
        raise HTTPException(status_code=500, detail={"code": "KBR-010", "message": "Internal server error"})


@router.post("/kb/solutions/optimize")
async def optimize_kb_solution_content(request: KBSolutionOptimizeRequest) -> Dict[str, Any]:
    """优化知识库解决建议文本，输出标准规范格式。"""
    raw_content = _truncate_text(_as_str(request.content), 6000).strip()
    if not raw_content:
        raise HTTPException(status_code=400, detail={"code": "KBR-001", "message": "content is required"})

    llm_enabled = _is_llm_configured()
    llm_requested = bool(request.use_llm)
    method = "rule-based"
    llm_fallback_reason = ""
    optimized_text = _format_solution_text_standard(
        raw_content,
        summary=request.summary,
        service_name=request.service_name,
        problem_type=request.problem_type,
        severity=request.severity,
    )

    if llm_enabled and llm_requested:
        prompt = (
            "你是 SRE 知识库编辑器。请把输入的“解决建议草稿”优化成可执行、可审计的标准规范文本。\n"
            "输出规则：\n"
            "1) 仅输出正文，不要 Markdown 代码块、不要额外解释；\n"
            "2) 严格包含以下分段标题：\n"
            "【目标】\n【问题上下文】\n【处理步骤】\n【验证方式】\n【回滚方案】\n【风险与注意】\n"
            "3) 【处理步骤】必须为编号列表，3-8 步，动词开头，可直接执行；\n"
            "4) 文本简洁、专业、避免空话，总长度控制在 1200 字以内。\n\n"
            f"服务: {_as_str(request.service_name, 'unknown')}\n"
            f"问题类型: {_as_str(request.problem_type, 'unknown')}\n"
            f"严重级别: {_normalize_kb_draft_severity(request.severity, default='medium')}\n"
            f"摘要: {_truncate_text(_as_str(request.summary), 300)}\n\n"
            "待优化草稿：\n"
            f"{_mask_sensitive_text(raw_content)}"
        )
        llm_timeout_seconds = max(5, int(_as_float(os.getenv("AI_KB_SOLUTION_OPTIMIZE_TIMEOUT_SECONDS", 40), 40)))
        try:
            llm_service = get_llm_service()
            llm_answer = await asyncio.wait_for(
                llm_service.chat(
                    message=prompt,
                    context={
                        "task": "kb_solution_optimize",
                        "service_name": _as_str(request.service_name),
                        "problem_type": _as_str(request.problem_type),
                        "severity": _normalize_kb_draft_severity(request.severity, default="medium"),
                    },
                ),
                timeout=llm_timeout_seconds,
            )
            text = _truncate_text(_as_str(llm_answer), 2000).strip()
            if text:
                optimized_text = text
                method = "llm"
            else:
                llm_fallback_reason = "llm_empty_response"
        except asyncio.TimeoutError:
            llm_fallback_reason = "llm_timeout"
        except Exception as e:
            logger.warning(f"KB solution optimize failed, fallback to rule-based: {e}")
            llm_fallback_reason = "llm_error"
    else:
        if llm_enabled and not llm_requested:
            llm_fallback_reason = "llm_disabled_by_user"
        elif not llm_enabled:
            llm_fallback_reason = "llm_unavailable"

    response = {
        "optimized_text": optimized_text,
        "method": method,
        "applied_style": "standard_kb_solution_v1",
        "llm_enabled": llm_enabled,
        "llm_requested": llm_requested,
    }
    if llm_fallback_reason:
        response["llm_fallback_reason"] = llm_fallback_reason
    return response


@router.patch("/cases/{case_id}/manual-remediation")
async def update_manual_remediation(case_id: str, request: ManualRemediationRequest) -> Dict[str, Any]:
    """更新人工修复步骤并写入验证结果。"""
    steps = [str(step).strip() for step in _as_list(request.manual_remediation_steps) if _as_str(step)]
    if len(steps) < 1:
        raise HTTPException(status_code=400, detail={"code": "KBR-003", "message": "manual_remediation_steps is required"})
    invalid_steps = [step for step in steps if len(step) < 5]
    if invalid_steps:
        raise HTTPException(
            status_code=400,
            detail={"code": "KBR-003", "message": "each manual_remediation_step length must be >= 5"},
        )
    notes = _as_str(request.verification_notes)
    if len(notes) < 20:
        raise HTTPException(status_code=400, detail={"code": "KBR-002", "message": "verification_notes length must be >= 20"})

    verification_result = _as_str(request.verification_result).lower()
    if verification_result not in {"pass", "fail"}:
        raise HTTPException(status_code=400, detail={"code": "KBR-002", "message": "verification_result must be pass or fail"})

    try:
        from ai.similar_cases import get_case_store, Case

        case_store = get_case_store(storage)
        existing = case_store.get_case(case_id)
        if not existing:
            raise HTTPException(status_code=404, detail={"code": "KBR-005", "message": "case not found"})

        updated = Case(**existing.to_dict())
        llm_metadata = updated.llm_metadata if isinstance(updated.llm_metadata, dict) else {}
        llm_metadata = dict(llm_metadata)
        history_records = [item for item in _as_list(llm_metadata.get("remediation_history")) if isinstance(item, dict)]
        knowledge_version = int(_as_float(llm_metadata.get("knowledge_version", 1), 1)) + 1
        llm_metadata["manual_remediation_steps"] = steps
        llm_metadata["verification_result"] = verification_result
        llm_metadata["verification_notes"] = notes
        llm_metadata["knowledge_version"] = knowledge_version
        llm_metadata["case_status"] = "resolved" if verification_result == "pass" else "archived"
        llm_metadata["last_editor"] = "manual"
        llm_metadata["analysis_summary"] = _as_str(llm_metadata.get("analysis_summary"), updated.summary)
        updated.llm_metadata = llm_metadata
        updated.manual_remediation_steps = steps
        updated.verification_result = verification_result
        updated.verification_notes = notes
        updated.knowledge_version = knowledge_version
        updated.last_editor = "manual"
        if _as_str(request.final_resolution):
            updated.resolution = _as_str(request.final_resolution)
        updated.resolved = verification_result == "pass"
        if updated.resolved:
            updated.resolved_at = datetime.utcnow().isoformat() + "Z"
        updated.updated_at = datetime.utcnow().isoformat() + "Z"

        gateway = get_knowledge_gateway(storage)
        runtime_options = gateway.resolve_runtime_options(
            remote_enabled=bool(request.remote_enabled),
            retrieval_mode="local",
            save_mode=_as_str(request.save_mode, "local_only"),
        )
        remote_result = gateway.upsert_remote_with_outbox(
            _build_case_payload_for_remote(updated),
            save_mode=_as_str(runtime_options.get("effective_save_mode"), "local_only"),
        )
        updated.llm_metadata["sync_status"] = _as_str(remote_result.get("sync_status"), "not_requested")
        updated.llm_metadata["external_doc_id"] = _as_str(remote_result.get("external_doc_id"))
        updated.llm_metadata["sync_error"] = _as_str(remote_result.get("sync_error"))
        updated.llm_metadata["sync_error_code"] = _as_str(remote_result.get("sync_error_code"))
        updated.llm_metadata["remediation_history"] = (
            history_records
            + [
                {
                    "version": knowledge_version,
                    "updated_at": updated.updated_at,
                    "editor": "manual",
                    "manual_remediation_steps": steps,
                    "verification_result": verification_result,
                    "verification_notes": notes,
                    "final_resolution": updated.resolution,
                    "sync_status": _as_str(remote_result.get("sync_status"), "not_requested"),
                    "sync_error_code": _as_str(remote_result.get("sync_error_code")),
                    "effective_save_mode": _as_str(runtime_options.get("effective_save_mode"), "local_only"),
                }
            ]
        )[-20:]

        case_store.update_case(updated)
        remediation_change_summary = {
            "manual_remediation_steps": {
                "before": _as_list((existing.llm_metadata or {}).get("manual_remediation_steps")),
                "after": steps,
            },
            "verification_result": {
                "before": _as_str((existing.llm_metadata or {}).get("verification_result")),
                "after": verification_result,
            },
            "verification_notes": {
                "before": _as_str((existing.llm_metadata or {}).get("verification_notes")),
                "after": notes,
            },
            "final_resolution": {
                "before": _as_str(existing.resolution),
                "after": _as_str(updated.resolution),
            },
        }
        _case_store_append_change_history(
            case_store,
            updated.id,
            {
                "event_type": "manual_remediation",
                "version": knowledge_version,
                "updated_at": updated.updated_at,
                "editor": "manual",
                "changed_fields": list(remediation_change_summary.keys()),
                "changes": remediation_change_summary,
                "effective_save_mode": _as_str(runtime_options.get("effective_save_mode"), "local_only"),
                "sync_status": updated.llm_metadata.get("sync_status"),
                "sync_error_code": updated.llm_metadata.get("sync_error_code"),
                "source": "api:/ai/cases/manual-remediation",
                "note": "manual_remediation_update",
            },
        )
        _metric_inc(KB_MANUAL_REMEDIATION_UPDATE_TOTAL)

        return {
            "status": "ok",
            "case_id": updated.id,
            "knowledge_version": knowledge_version,
            "sync_status": updated.llm_metadata.get("sync_status"),
            "sync_error_code": _as_str(updated.llm_metadata.get("sync_error_code")),
            "effective_save_mode": _as_str(runtime_options.get("effective_save_mode"), "local_only"),
            "outbox_id": _as_str(remote_result.get("outbox_id")),
            "remediation_history_count": len(_as_list(updated.llm_metadata.get("remediation_history"))),
            "message": "manual remediation updated",
        }
    except HTTPException:
        raise
    except Exception as e:
        logger.error(f"Error updating manual remediation: {e}")
        raise HTTPException(status_code=500, detail={"code": "KBR-010", "message": "Internal server error"})


@router.post("/follow-up")
async def follow_up_analysis(request: FollowUpRequest) -> Dict[str, Any]:
    """追问分析接口，支持会话上下文管理。"""
    question = _as_str(request.question)
    if not question:
        raise HTTPException(status_code=400, detail="question is required")

    safe_question = _mask_sensitive_text(question)
    analysis_context = _mask_sensitive_payload(request.analysis_context or {})
    session_store = get_ai_session_store(storage)
    analysis_session_id = _as_str(request.analysis_session_id) or _as_str(analysis_context.get("session_id"))

    if not analysis_session_id:
        created = await _run_blocking(
            session_store.create_session,
            analysis_type=_as_str(analysis_context.get("analysis_type"), "log"),
            service_name=_as_str(analysis_context.get("service_name")),
            input_text=_as_str(analysis_context.get("input_text"), question),
            trace_id=_as_str(analysis_context.get("trace_id")),
            context=analysis_context,
            result={
                "summary": _extract_overview_summary(analysis_context.get("result", {}))
                if isinstance(analysis_context.get("result"), dict)
                else "",
                "raw": analysis_context.get("result", {}) if isinstance(analysis_context.get("result"), dict) else {},
            },
            analysis_method=_as_str((analysis_context.get("llm_info") or {}).get("method")),
            llm_model=_as_str((analysis_context.get("llm_info") or {}).get("model")),
            llm_provider=_as_str(os.getenv("LLM_PROVIDER", "")),
            source="api:/follow-up:init",
        )
        analysis_session_id = created.session_id
    elif not await _run_blocking(session_store.get_session, analysis_session_id):
        await _run_blocking(
            session_store.create_session,
            analysis_type=_as_str(analysis_context.get("analysis_type"), "log"),
            service_name=_as_str(analysis_context.get("service_name")),
            input_text=_as_str(analysis_context.get("input_text"), question),
            trace_id=_as_str(analysis_context.get("trace_id")),
            context=analysis_context,
            result={
                "summary": _extract_overview_summary(analysis_context.get("result", {}))
                if isinstance(analysis_context.get("result"), dict)
                else "",
                "raw": analysis_context.get("result", {}) if isinstance(analysis_context.get("result"), dict) else {},
            },
            analysis_method=_as_str((analysis_context.get("llm_info") or {}).get("method")),
            llm_model=_as_str((analysis_context.get("llm_info") or {}).get("model")),
            llm_provider=_as_str(os.getenv("LLM_PROVIDER", "")),
            source="api:/follow-up:recover",
            session_id=analysis_session_id,
        )

    conversation_id = _as_str(request.conversation_id) or f"conv-{uuid.uuid4().hex[:12]}"
    if request.reset:
        _clear_conversation_history(conversation_id)

    client_history = _normalize_conversation_history(
        [_mask_sensitive_payload(msg.model_dump()) for msg in request.history],
        max_items=40,
    )
    server_history = _get_conversation_history(conversation_id)
    stored_history: List[Dict[str, Any]] = []
    if analysis_session_id and (not server_history or bool(client_history)):
        stored_messages = await _run_blocking(session_store.get_messages, analysis_session_id, 200)
        stored_history = _session_messages_to_conversation_history(stored_messages, max_items=40)

    history = client_history or server_history
    if client_history and stored_history:
        # 前端上传增量 history 时，补齐持久化上下文，避免首轮追问丢历史。
        history = _merge_conversation_history(stored_history, client_history, max_items=40)
    elif not history:
        history = stored_history

    compacted_info = _compact_conversation_for_prompt(history)
    compacted_history = compacted_info.get("history", history)
    compacted_summary = _as_str(compacted_info.get("summary"))
    history_compacted = bool(compacted_info.get("compacted"))
    references = _build_followup_references(analysis_context)
    context_pills = _build_context_pills(analysis_context, analysis_session_id=analysis_session_id)

    last_item = history[-1] if history else {}
    if (
        isinstance(last_item, dict)
        and _as_str(last_item.get("role")).lower() == "user"
        and _as_str(last_item.get("content")) == safe_question
    ):
        user_message = {
            "role": "user",
            "content": safe_question,
            "timestamp": _as_str(last_item.get("timestamp")) or datetime.utcnow().isoformat() + "Z",
            "metadata": {"kind": "follow_up_question"},
        }
        history[-1] = {
            "role": "user",
            "content": safe_question,
            "timestamp": user_message["timestamp"],
        }
    else:
        user_message = {
            "role": "user",
            "content": safe_question,
            "timestamp": datetime.utcnow().isoformat() + "Z",
            "metadata": {"kind": "follow_up_question"},
        }
        history.append(
            {
                "role": "user",
                "content": safe_question,
                "timestamp": user_message["timestamp"],
            }
        )
    history = _trim_conversation_history(history)

    llm_enabled = _is_llm_configured()
    llm_requested = bool(request.use_llm)
    token_budget = max(1000, int(os.getenv("AI_FOLLOWUP_TOKEN_BUDGET", "12000")))
    token_warn_threshold = max(100, int(os.getenv("AI_FOLLOWUP_TOKEN_WARN_THRESHOLD", "1500")))
    llm_timeout_seconds = max(5, int(os.getenv("AI_FOLLOWUP_LLM_TIMEOUT_SECONDS", "90")))
    token_estimate = _estimate_token_usage(
        safe_question,
        compacted_history,
        compacted_summary,
        analysis_context,
        references,
    )
    token_remaining = token_budget - token_estimate
    token_warning = token_remaining < token_warn_threshold
    llm_timeout_fallback = False

    if llm_enabled and llm_requested:
        llm_service = get_llm_service()
        prompt = safe_question
        if references:
            ref_text = "\n".join(
                [f"[{ref.get('id')}] {ref.get('title')}: {ref.get('snippet')}" for ref in references]
            )
            prompt = (
                f"{safe_question}\n\n"
                "回答要求：尽量引用以下片段编号（如 [A1]、[L1]），并给出可执行步骤。\n"
                f"{ref_text}"
            )
        try:
            answer = await asyncio.wait_for(
                llm_service.chat(
                    message=prompt,
                    context={
                        "analysis_context": analysis_context,
                        "conversation_history": compacted_history[-10:],
                        "conversation_summary": compacted_summary,
                        "references": references,
                        "token_budget": token_budget,
                    },
                ),
                timeout=llm_timeout_seconds,
            )
            if _as_str(answer):
                method = "llm"
            else:
                logger.warning(
                    "AI follow-up LLM returned empty answer, fallback to rule-based "
                    f"(session_id={analysis_session_id})"
                )
                answer = _build_followup_fallback_answer(
                    safe_question,
                    analysis_context,
                    fallback_reason="llm_unavailable",
                )
                method = "rule-based"
        except asyncio.TimeoutError:
            logger.warning(
                "AI follow-up LLM timeout, fallback to rule-based answer "
                f"(timeout={llm_timeout_seconds}s, session_id={analysis_session_id})"
            )
            answer = _build_followup_fallback_answer(
                safe_question,
                analysis_context,
                fallback_reason="llm_timeout",
            )
            method = "rule-based"
            llm_timeout_fallback = True
        except Exception as e:
            error_text = _as_str(e).lower()
            is_timeout_error = (
                isinstance(e, TimeoutError)
                or "timeout" in error_text
                or "timed out" in error_text
                or "deadline exceeded" in error_text
            )
            logger.warning(
                "AI follow-up LLM error, fallback to rule-based answer "
                f"(session_id={analysis_session_id}, timeout_like={is_timeout_error}): {e}"
            )
            answer = _build_followup_fallback_answer(
                safe_question,
                analysis_context,
                fallback_reason="llm_timeout" if is_timeout_error else "llm_unavailable",
            )
            method = "rule-based"
            llm_timeout_fallback = bool(is_timeout_error)
    else:
        fallback_reason = "llm_unavailable"
        if llm_enabled and not llm_requested:
            fallback_reason = "llm_disabled_by_user"
        answer = _build_followup_fallback_answer(
            safe_question,
            analysis_context,
            fallback_reason=fallback_reason,
        )
        method = "rule-based"

    masked_answer = _mask_sensitive_text(_as_str(answer, "暂无回答"))
    assistant_message = {
        "role": "assistant",
        "content": masked_answer,
        "timestamp": datetime.utcnow().isoformat() + "Z",
        "metadata": {
            "references": references,
            "context_pills": context_pills,
            "token_budget": token_budget,
            "token_estimate": token_estimate,
            "token_remaining": token_remaining,
            "token_warning": token_warning,
            "history_compacted": history_compacted,
            "llm_timeout_fallback": llm_timeout_fallback,
        },
    }
    history.append(assistant_message)
    history = _trim_conversation_history(history)
    _set_conversation_history(conversation_id, _trim_conversation_history(
        [{"role": item.get("role"), "content": item.get("content"), "timestamp": item.get("timestamp")} for item in history],
        max_items=40,
    ))

    persisted_messages = await _run_blocking(
        session_store.append_messages,
        analysis_session_id,
        [user_message, assistant_message],
    )
    response_history = history
    if persisted_messages:
        stored_messages = await _run_blocking(session_store.get_messages, analysis_session_id, 200)
        response_history = _trim_conversation_history(
            [
                {
                    "message_id": _as_str(msg.message_id),
                    "role": _as_str(msg.role),
                    "content": _as_str(msg.content),
                    "timestamp": _as_str(msg.created_at),
                    "metadata": msg.metadata if isinstance(msg.metadata, dict) else {},
                }
                for msg in stored_messages
                if _as_str(msg.role) in {"user", "assistant"}
            ],
            max_items=40,
        )

    summary_text = _as_str(
        (analysis_context.get("result") or {}).get("overview", {}).get("description")
        if isinstance((analysis_context.get("result") or {}).get("overview"), dict)
        else analysis_context.get("input_text")
    )[:300]
    current_session = await _run_blocking(session_store.get_session, analysis_session_id)
    fallback_title = _as_str(analysis_context.get("title")) or _as_str(analysis_context.get("service_name"), "AI Session")
    updated_title = _as_str(getattr(current_session, "title", "")) or fallback_title
    await _run_blocking(
        session_store.update_session,
        analysis_session_id,
        analysis_method=method,
        llm_provider=_as_str(os.getenv("LLM_PROVIDER", "")),
        llm_model=_as_str((analysis_context.get("llm_info") or {}).get("model")),
        summary_text=summary_text,
        title=updated_title,
        status="completed",
    )

    return {
        "analysis_session_id": analysis_session_id,
        "conversation_id": conversation_id,
        "analysis_method": method,
        "llm_enabled": llm_enabled,
        "llm_requested": llm_requested,
        "answer": masked_answer,
        "history": response_history,
        "references": references,
        "context_pills": context_pills,
        "history_compacted": history_compacted,
        "conversation_summary": compacted_summary,
        "token_budget": token_budget,
        "token_estimate": token_estimate,
        "token_remaining": token_remaining,
        "token_warning": token_warning,
        "llm_timeout_fallback": llm_timeout_fallback,
    }


class TraceAnalysisRequest(BaseModel):
    """Trace 分析请求"""
    trace_id: str


@router.post("/trace/analyze")
async def analyze_trace_detailed(request: TraceAnalysisRequest) -> Dict[str, Any]:
    """
    详细分析 Trace

    分析调用链的完整信息，包括：
    - 性能瓶颈
    - 错误节点
    - 根因分析
    - 优化建议
    """
    try:
        from ai.trace_analyzer import get_trace_analyzer

        analyzer = get_trace_analyzer(storage)
        result = await _run_blocking(analyzer.analyze_trace, request.trace_id)
        payload = {
            "trace_id": result.trace_id,
            "total_duration_ms": result.total_duration_ms,
            "service_count": result.service_count,
            "span_count": result.span_count,
            "root_cause_spans": result.root_cause_spans,
            "bottleneck_spans": result.bottleneck_spans,
            "error_spans": result.error_spans,
            "recommendations": result.recommendations,
            "service_timeline": result.service_timeline,
            "critical_path": result.critical_path,
        }
        session_id = await _persist_analysis_session(
            analysis_type="trace",
            service_name="",
            input_text=request.trace_id,
            trace_id=request.trace_id,
            context={"trace_id": request.trace_id, "mode": "detailed"},
            result=payload,
            source="api:/trace/analyze",
        )
        if session_id:
            payload["session_id"] = session_id
        return payload

    except Exception as e:
        logger.error(f"Error analyzing trace: {e}")
        raise HTTPException(status_code=500, detail="Internal server error")


@router.get("/trace/{trace_id}/visualization")
async def get_trace_visualization(trace_id: str) -> Dict[str, Any]:
    """
    获取 Trace 可视化数据

    返回用于前端渲染调用链图的数据
    """
    try:
        from ai.trace_analyzer import get_trace_analyzer

        analyzer = get_trace_analyzer(storage)
        result = await _run_blocking(analyzer.get_trace_visualization_data, trace_id)

        return result

    except Exception as e:
        logger.error(f"Error getting trace visualization: {e}")
        raise HTTPException(status_code=500, detail="Internal server error")


@router.get("/health")
async def health_check():
    """健康检查"""
    llm_configured = _is_llm_configured()
    
    return {
        "status": "healthy",
        "service": "ai-service",
        "analyzer": "ready",
        "llm_enabled": llm_configured,
        "llm_provider": os.getenv("LLM_PROVIDER", "openai"),
        "llm_model": os.getenv("LLM_MODEL", "gpt-4"),
    }
