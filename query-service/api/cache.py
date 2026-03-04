"""
API 缓存装饰器
为查询 API 提供内存缓存支持
"""
import time
import hashlib
import json
from typing import Dict, Any, Callable, Optional
from functools import wraps

# 内存缓存存储
_cache_store: Dict[str, tuple] = {}  # {key: (value, expiry_time)}
_cache_stats = {"hits": 0, "misses": 0, "sets": 0}


def generate_cache_key(prefix: str, **kwargs) -> str:
    """
    生成缓存键

    Args:
        prefix: 键前缀
        **kwargs: 缓存参数

    Returns:
        str: 缓存键
    """
    # 过滤掉 None 值
    params = {k: v for k, v in kwargs.items() if v is not None}

    # 按键排序确保一致性
    sorted_params = json.dumps(params, sort_keys=True)

    # 生成哈希
    hash_val = hashlib.md5(sorted_params.encode()).hexdigest()[:12]

    return f"{prefix}:{hash_val}"


def cached(ttl: int = 30, key_prefix: str = "api"):
    """
    缓存装饰器

    Args:
        ttl: 缓存生存时间（秒）
        key_prefix: 缓存键前缀

    Returns:
        装饰器函数
    """
    def decorator(func: Callable) -> Callable:
        @wraps(func)
        async def wrapper(*args, **kwargs) -> Any:
            # 生成缓存键
            cache_key = generate_cache_key(key_prefix, **kwargs)

            # 检查缓存
            if cache_key in _cache_store:
                value, expiry_time = _cache_store[cache_key]

                # 检查是否过期
                if time.time() < expiry_time:
                    _cache_stats["hits"] += 1
                    return value
                else:
                    # 删除过期缓存
                    del _cache_store[cache_key]

            # 缓存未命中，执行函数
            _cache_stats["misses"] += 1
            result = await func(*args, **kwargs)

            # 存入缓存
            expiry_time = time.time() + ttl
            _cache_store[cache_key] = (result, expiry_time)
            _cache_stats["sets"] += 1

            return result

        return wrapper
    return decorator


def clear_cache(pattern: Optional[str] = None):
    """
    清除缓存

    Args:
        pattern: 可选的模式匹配，如果为 None 则清除所有
    """
    global _cache_store

    if pattern is None:
        _cache_store.clear()
    else:
        keys_to_delete = [k for k in _cache_store.keys() if pattern in k]
        for key in keys_to_delete:
            del _cache_store[key]


def get_cache_stats() -> Dict[str, Any]:
    """
    获取缓存统计信息

    Returns:
        Dict[str, Any]: 缓存统计数据
    """
    total_requests = _cache_stats["hits"] + _cache_stats["misses"]
    hit_rate = (_cache_stats["hits"] / total_requests * 100) if total_requests > 0 else 0

    return {
        "hits": _cache_stats["hits"],
        "misses": _cache_stats["misses"],
        "sets": _cache_stats["sets"],
        "hit_rate": f"{hit_rate:.2f}%",
        "size": len(_cache_store),
    }


def reset_cache_stats():
    """重置缓存统计"""
    global _cache_stats
    _cache_stats = {"hits": 0, "misses": 0, "sets": 0}
