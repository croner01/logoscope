"""
Hybrid topology build coordinator.

合并同参数并发构建，避免重复触发重型拓扑计算。
"""
import asyncio
import copy
import logging
from typing import Any, Dict, Optional, Tuple
from weakref import WeakKeyDictionary

from api.topology_build_subprocess import run_hybrid_topology_build_in_subprocess

logger = logging.getLogger(__name__)

_ALLOWED_MESSAGE_TARGET_PATTERNS = {"url", "kv", "proxy", "rpc"}
_PROCESS_ISOLATION_ENABLED = False
_PROCESS_STORAGE_CONFIG: Optional[Dict[str, Any]] = None
_PROCESS_TIMEOUT_SECONDS = 45
_PROCESS_PYTHON_EXECUTABLE: Optional[str] = None
_PROCESS_FALLBACK_LOCAL_ON_ERROR = True
_PROCESS_MAX_CONCURRENCY = 2
_PROCESS_MAX_QUEUE_SIZE = 64
_PROCESS_ACQUIRE_TIMEOUT_SECONDS = 2


class _LoopBuildState:
    """事件循环级别的 in-flight 状态。"""

    def __init__(self) -> None:
        self.lock = asyncio.Lock()
        self.inflight: Dict[Tuple[Any, ...], asyncio.Future] = {}


_STATE_BY_LOOP: "WeakKeyDictionary[asyncio.AbstractEventLoop, _LoopBuildState]" = WeakKeyDictionary()


class _LoopProcessLimiter:
    """Per-loop subprocess capacity limiter state."""

    def __init__(self, max_concurrency: int) -> None:
        self.max_concurrency = max_concurrency
        self.semaphore = asyncio.Semaphore(max_concurrency)
        self.lock = asyncio.Lock()
        self.waiters = 0


_PROCESS_LIMITER_BY_LOOP: "WeakKeyDictionary[asyncio.AbstractEventLoop, _LoopProcessLimiter]" = WeakKeyDictionary()


async def _run_blocking(func: Any, *args: Any, **kwargs: Any) -> Any:
    """Execute blocking work inline to avoid executor shutdown deadlocks."""
    return func(*args, **kwargs)


def _consume_future_exception(future: asyncio.Future) -> None:
    """Best-effort consumption to avoid 'Future exception was never retrieved' noise."""
    try:
        future.exception()
    except Exception:
        return


def configure_build_process_isolation(
    *,
    enabled: bool,
    storage_config: Optional[Dict[str, Any]] = None,
    timeout_seconds: int = 45,
    python_executable: Optional[str] = None,
    fallback_local_on_error: bool = True,
    max_concurrency: int = 2,
    max_queue_size: int = 64,
    acquire_timeout_seconds: int = 2,
) -> None:
    """Configure subprocess isolation for hybrid topology build."""
    global _PROCESS_ISOLATION_ENABLED, _PROCESS_STORAGE_CONFIG, _PROCESS_TIMEOUT_SECONDS, _PROCESS_PYTHON_EXECUTABLE
    global _PROCESS_FALLBACK_LOCAL_ON_ERROR
    global _PROCESS_MAX_CONCURRENCY, _PROCESS_MAX_QUEUE_SIZE, _PROCESS_ACQUIRE_TIMEOUT_SECONDS
    _PROCESS_ISOLATION_ENABLED = bool(enabled)
    _PROCESS_STORAGE_CONFIG = dict(storage_config or {}) if storage_config else None
    try:
        parsed_timeout = int(timeout_seconds)
    except (TypeError, ValueError):
        parsed_timeout = 45
    _PROCESS_TIMEOUT_SECONDS = max(3, min(300, parsed_timeout))
    python_text = str(python_executable).strip() if python_executable else ""
    _PROCESS_PYTHON_EXECUTABLE = python_text or None
    _PROCESS_FALLBACK_LOCAL_ON_ERROR = bool(fallback_local_on_error)
    try:
        parsed_concurrency = int(max_concurrency)
    except (TypeError, ValueError):
        parsed_concurrency = 2
    try:
        parsed_queue_size = int(max_queue_size)
    except (TypeError, ValueError):
        parsed_queue_size = 64
    try:
        parsed_acquire_timeout = int(acquire_timeout_seconds)
    except (TypeError, ValueError):
        parsed_acquire_timeout = 2

    _PROCESS_MAX_CONCURRENCY = max(1, min(64, parsed_concurrency))
    _PROCESS_MAX_QUEUE_SIZE = max(1, min(10000, parsed_queue_size))
    _PROCESS_ACQUIRE_TIMEOUT_SECONDS = max(1, min(60, parsed_acquire_timeout))


def _process_isolation_active() -> bool:
    return bool(_PROCESS_ISOLATION_ENABLED and _PROCESS_STORAGE_CONFIG)


def _get_loop_process_limiter() -> _LoopProcessLimiter:
    loop = asyncio.get_running_loop()
    limiter = _PROCESS_LIMITER_BY_LOOP.get(loop)
    if limiter is None or limiter.max_concurrency != _PROCESS_MAX_CONCURRENCY:
        limiter = _LoopProcessLimiter(_PROCESS_MAX_CONCURRENCY)
        _PROCESS_LIMITER_BY_LOOP[loop] = limiter
    return limiter


async def _acquire_process_slot() -> _LoopProcessLimiter:
    limiter = _get_loop_process_limiter()
    async with limiter.lock:
        if limiter.waiters >= _PROCESS_MAX_QUEUE_SIZE:
            raise RuntimeError(
                f"topology build subprocess queue full (limit={_PROCESS_MAX_QUEUE_SIZE})"
            )
        limiter.waiters += 1

    try:
        await asyncio.wait_for(
            limiter.semaphore.acquire(),
            timeout=float(_PROCESS_ACQUIRE_TIMEOUT_SECONDS),
        )
    except asyncio.TimeoutError as exc:
        raise RuntimeError(
            f"topology build subprocess acquire timeout after {_PROCESS_ACQUIRE_TIMEOUT_SECONDS}s"
        ) from exc
    finally:
        async with limiter.lock:
            limiter.waiters = max(0, limiter.waiters - 1)
    return limiter


async def _build_topology_with_isolation(
    hybrid_builder: Any,
    build_kwargs: Dict[str, Any],
) -> Dict[str, Any]:
    if _process_isolation_active():
        limiter: Optional[_LoopProcessLimiter] = None
        try:
            limiter = await _acquire_process_slot()
            return await run_hybrid_topology_build_in_subprocess(
                storage_config=_PROCESS_STORAGE_CONFIG or {},
                build_kwargs=build_kwargs,
                timeout_seconds=_PROCESS_TIMEOUT_SECONDS,
                python_executable=_PROCESS_PYTHON_EXECUTABLE,
            )
        except Exception as exc:
            if not _PROCESS_FALLBACK_LOCAL_ON_ERROR:
                raise
            logger.exception(
                "topology build subprocess failed, fallback to local builder: %s",
                exc,
            )
        finally:
            if limiter is not None:
                limiter.semaphore.release()
    return await _run_blocking(hybrid_builder.build_topology, **build_kwargs)


def _get_loop_state() -> _LoopBuildState:
    loop = asyncio.get_running_loop()
    state = _STATE_BY_LOOP.get(loop)
    if state is None:
        state = _LoopBuildState()
        _STATE_BY_LOOP[loop] = state
    return state


def _normalize_namespace(namespace: Optional[str]) -> Optional[str]:
    if namespace is None:
        return None
    namespace_text = str(namespace).strip()
    return namespace_text or None


def _normalize_inference_mode_for_key(inference_mode: Optional[str]) -> Optional[str]:
    if inference_mode is None:
        return None
    mode = str(inference_mode).strip().lower()
    return mode or None


def _normalize_message_target_enabled(value: Optional[Any]) -> Optional[bool]:
    if value is None:
        return None
    if isinstance(value, bool):
        return value
    if isinstance(value, (int, float)):
        return bool(value)
    return str(value).strip().lower() in {"1", "true", "yes", "on"}


def _normalize_message_target_patterns(value: Optional[Any]) -> Optional[Tuple[str, ...]]:
    if value is None:
        return None
    if isinstance(value, (list, tuple, set)):
        raw_items = [str(item) for item in value]
    else:
        raw_items = str(value).split(",")
    patterns = sorted({
        token.strip().lower()
        for token in raw_items
        if token and token.strip().lower() in _ALLOWED_MESSAGE_TARGET_PATTERNS
    })
    return tuple(patterns) if patterns else None


def _normalize_optional_int(value: Optional[Any], minimum: int, maximum: int) -> Optional[int]:
    if value is None:
        return None
    try:
        parsed = int(value)
    except (TypeError, ValueError):
        return None
    return max(minimum, min(maximum, parsed))


def _normalize_request_key_fields(
    time_window: str,
    namespace: Optional[str] = None,
    confidence_threshold: float = 0.3,
    inference_mode: Optional[str] = None,
    message_target_enabled: Optional[Any] = None,
    message_target_patterns: Optional[Any] = None,
    message_target_min_support: Optional[Any] = None,
    message_target_max_per_log: Optional[Any] = None,
) -> Dict[str, Any]:
    normalized_time_window = str(time_window or "1 HOUR").strip() or "1 HOUR"
    try:
        normalized_confidence = float(confidence_threshold)
    except (TypeError, ValueError):
        normalized_confidence = 0.3
    normalized_confidence = max(0.0, min(1.0, normalized_confidence))
    normalized_patterns = _normalize_message_target_patterns(message_target_patterns)
    return {
        "time_window": normalized_time_window,
        "namespace": _normalize_namespace(namespace),
        "confidence_threshold": normalized_confidence,
        "inference_mode": _normalize_inference_mode_for_key(inference_mode),
        "message_target_enabled": _normalize_message_target_enabled(message_target_enabled),
        "message_target_patterns": normalized_patterns,
        "message_target_min_support": _normalize_optional_int(message_target_min_support, minimum=1, maximum=20),
        "message_target_max_per_log": _normalize_optional_int(message_target_max_per_log, minimum=1, maximum=12),
    }


def _build_request_key(builder: Any, normalized_kwargs: Dict[str, Any]) -> Tuple[Any, ...]:
    patterns = normalized_kwargs.get("message_target_patterns")
    return (
        id(builder),
        normalized_kwargs["time_window"],
        normalized_kwargs["namespace"],
        round(float(normalized_kwargs["confidence_threshold"]), 6),
        normalized_kwargs["inference_mode"],
        normalized_kwargs["message_target_enabled"],
        patterns,
        normalized_kwargs["message_target_min_support"],
        normalized_kwargs["message_target_max_per_log"],
    )


def _safe_deepcopy(payload: Dict[str, Any]) -> Dict[str, Any]:
    try:
        return copy.deepcopy(payload)
    except Exception:
        logger.debug("topology deepcopy failed, fallback to shared payload", exc_info=True)
        return payload


async def build_hybrid_topology_coalesced(
    hybrid_builder: Any,
    *,
    time_window: str,
    namespace: Optional[str] = None,
    confidence_threshold: float = 0.3,
    inference_mode: Optional[str] = None,
    message_target_enabled: Optional[Any] = None,
    message_target_patterns: Optional[Any] = None,
    message_target_min_support: Optional[Any] = None,
    message_target_max_per_log: Optional[Any] = None,
) -> Dict[str, Any]:
    """
    合并同参数并发构建，复用同一个 in-flight 任务。
    """
    normalized_key_fields = _normalize_request_key_fields(
        time_window=time_window,
        namespace=namespace,
        confidence_threshold=confidence_threshold,
        inference_mode=inference_mode,
        message_target_enabled=message_target_enabled,
        message_target_patterns=message_target_patterns,
        message_target_min_support=message_target_min_support,
        message_target_max_per_log=message_target_max_per_log,
    )
    build_kwargs = {
        "time_window": time_window,
        "namespace": namespace,
        "confidence_threshold": confidence_threshold,
        "inference_mode": inference_mode,
        "message_target_enabled": message_target_enabled,
        "message_target_patterns": message_target_patterns,
        "message_target_min_support": message_target_min_support,
        "message_target_max_per_log": message_target_max_per_log,
    }
    request_key = _build_request_key(hybrid_builder, normalized_key_fields)
    state = _get_loop_state()
    loop = asyncio.get_running_loop()
    is_leader = False

    async with state.lock:
        shared_future = state.inflight.get(request_key)
        if shared_future is None:
            shared_future = loop.create_future()
            shared_future.add_done_callback(_consume_future_exception)
            state.inflight[request_key] = shared_future
            is_leader = True

    try:
        if is_leader:
            # Yield once so concurrent followers can attach to the same in-flight future.
            await asyncio.sleep(0)
            topology = await _build_topology_with_isolation(hybrid_builder, build_kwargs)
            shared_topology = _safe_deepcopy(topology)
            if not shared_future.done():
                shared_future.set_result(shared_topology)
            return _safe_deepcopy(shared_topology)

        topology = await asyncio.shield(shared_future)
        return _safe_deepcopy(topology)
    except asyncio.CancelledError as exc:
        if is_leader and not shared_future.done():
            shared_future.set_exception(exc)
        raise
    except Exception as exc:
        if is_leader and not shared_future.done():
            shared_future.set_exception(exc)
        raise
    finally:
        if is_leader:
            async with state.lock:
                current = state.inflight.get(request_key)
                if current is shared_future:
                    state.inflight.pop(request_key, None)
