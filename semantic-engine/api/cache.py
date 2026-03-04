"""
缓存模块 - 提供简单的内存缓存功能
"""
import time
import functools
from typing import Dict, Any, Optional, Callable

# 全局缓存存储
_cache: Dict[str, Dict[str, Any]] = {}


def cached(ttl: int = 300):
    """
    缓存装饰器

    Args:
        ttl: 缓存过期时间（秒），默认 5 分钟

    Returns:
        装饰器函数
    """
    def decorator(func: Callable) -> Callable:
        @functools.wraps(func)
        def wrapper(*args, **kwargs):
            # 生成缓存键
            cache_key = f"{func.__name__}:{str(args)}:{str(kwargs)}"

            # 检查缓存
            if cache_key in _cache:
                entry = _cache[cache_key]
                if entry['expires'] > time.time():
                    return entry['value']
                else:
                    # 过期，删除缓存
                    del _cache[cache_key]

            # 执行函数
            result = func(*args, **kwargs)

            # 存入缓存
            _cache[cache_key] = {
                'value': result,
                'expires': time.time() + ttl
            }

            return result

        return wrapper
    return decorator


def clear_cache(pattern: str = None) -> int:
    """
    清除缓存

    Args:
        pattern: 匹配模式，如果为 None 则清除所有缓存

    Returns:
        int: 清除的缓存条目数
    """
    global _cache

    if pattern is None:
        count = len(_cache)
        _cache.clear()
        return count

    # 根据模式删除匹配的缓存
    keys_to_delete = [k for k in _cache.keys() if pattern in k]
    for key in keys_to_delete:
        del _cache[key]

    return len(keys_to_delete)


def get_cache_stats() -> Dict[str, Any]:
    """
    获取缓存统计信息

    Returns:
        Dict[str, Any]: 缓存统计信息
    """
    total = len(_cache)
    expired = sum(1 for entry in _cache.values() if entry['expires'] <= time.time())

    return {
        'total_entries': total,
        'expired_entries': expired,
        'active_entries': total - expired
    }
