"""
缓存模块（兼容层）

为历史测试与新代码同时提供：
- 统一缓存键生成
- 同步/异步函数缓存装饰器
- 命中/未命中/写入统计
"""
import functools
import hashlib
import inspect
import json
import time
from typing import Any, Callable, Dict, Tuple

_cache_store: Dict[str, Tuple[Any, float]] = {}
_cache_stats: Dict[str, int] = {"hits": 0, "misses": 0, "sets": 0}


def _normalize_value(value: Any) -> Any:
    """将参数值规范化为可稳定序列化的结构。"""
    if isinstance(value, dict):
        return {str(k): _normalize_value(v) for k, v in sorted(value.items(), key=lambda item: str(item[0]))}
    if isinstance(value, (list, tuple)):
        return [_normalize_value(item) for item in value]
    if isinstance(value, set):
        return sorted((_normalize_value(item) for item in value), key=lambda item: str(item))
    return value


def generate_cache_key(key_prefix: str, **kwargs: Any) -> str:
    """基于 key 前缀和参数生成稳定缓存键（会过滤 None）。"""
    filtered = {str(k): _normalize_value(v) for k, v in kwargs.items() if v is not None}
    encoded = json.dumps(filtered, sort_keys=True, ensure_ascii=False, separators=(",", ":"))
    digest = hashlib.sha256(encoded.encode("utf-8")).hexdigest()[:16]
    return f"{(key_prefix or 'cache').strip() or 'cache'}:{digest}"


def _build_call_key(func: Callable[..., Any], key_prefix: str, args: tuple[Any, ...], kwargs: Dict[str, Any]) -> str:
    """对函数调用参数做绑定并生成缓存键。"""
    try:
        signature = inspect.signature(func)
        bound = signature.bind_partial(*args, **kwargs)
        normalized = {
            key: value
            for key, value in bound.arguments.items()
            if key != "self" and value is not None
        }
    except Exception:
        normalized = {"args": args, "kwargs": kwargs}
    return generate_cache_key(key_prefix, **normalized)


def cached(ttl: int = 300, key_prefix: str = "cache"):
    """缓存装饰器，支持 async/sync 两类函数。"""

    def decorator(func: Callable[..., Any]) -> Callable[..., Any]:
        if inspect.iscoroutinefunction(func):

            @functools.wraps(func)
            async def async_wrapper(*args: Any, **kwargs: Any) -> Any:
                cache_key = _build_call_key(func, key_prefix, args, kwargs)
                now = time.time()
                cached_entry = _cache_store.get(cache_key)
                if cached_entry and cached_entry[1] > now:
                    _cache_stats["hits"] += 1
                    return cached_entry[0]
                if cached_entry:
                    _cache_store.pop(cache_key, None)
                _cache_stats["misses"] += 1
                result = await func(*args, **kwargs)
                _cache_store[cache_key] = (result, now + max(1, int(ttl)))
                _cache_stats["sets"] += 1
                return result

            return async_wrapper

        @functools.wraps(func)
        def sync_wrapper(*args: Any, **kwargs: Any) -> Any:
            cache_key = _build_call_key(func, key_prefix, args, kwargs)
            now = time.time()
            cached_entry = _cache_store.get(cache_key)
            if cached_entry and cached_entry[1] > now:
                _cache_stats["hits"] += 1
                return cached_entry[0]
            if cached_entry:
                _cache_store.pop(cache_key, None)
            _cache_stats["misses"] += 1
            result = func(*args, **kwargs)
            _cache_store[cache_key] = (result, now + max(1, int(ttl)))
            _cache_stats["sets"] += 1
            return result

        return sync_wrapper

    return decorator


def clear_cache(pattern: str = None) -> int:
    """按 pattern 清理缓存；pattern 为 None 时清空所有缓存。"""
    if pattern is None:
        count = len(_cache_store)
        _cache_store.clear()
        return count
    keys = [key for key in list(_cache_store.keys()) if pattern in key]
    for key in keys:
        _cache_store.pop(key, None)
    return len(keys)


def reset_cache_stats() -> None:
    """重置缓存命中统计。"""
    _cache_stats["hits"] = 0
    _cache_stats["misses"] = 0
    _cache_stats["sets"] = 0


def get_cache_stats() -> Dict[str, Any]:
    """返回缓存统计与大小信息（兼容新旧字段）。"""
    hits = int(_cache_stats.get("hits", 0))
    misses = int(_cache_stats.get("misses", 0))
    sets = int(_cache_stats.get("sets", 0))
    total = hits + misses
    hit_rate = "0.00%" if total <= 0 else f"{(hits / total) * 100:.2f}%"
    size = len(_cache_store)
    return {
        "hits": hits,
        "misses": misses,
        "sets": sets,
        "hit_rate": hit_rate,
        "size": size,
        "total_entries": size,
        "expired_entries": 0,
        "active_entries": size,
    }
