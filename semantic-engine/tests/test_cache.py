"""
Cache API 单元测试
测试缓存装饰器和缓存管理功能
"""
import pytest
import time
import asyncio
from unittest.mock import patch
from api.cache import (
    generate_cache_key,
    cached,
    clear_cache,
    get_cache_stats,
    reset_cache_stats
)


class TestGenerateCacheKey:
    """测试缓存键生成"""

    def test_generate_cache_key_basic(self):
        """测试基本键生成"""
        key = generate_cache_key("api", service_name="test", limit=10)
        assert key.startswith("api:")
        assert len(key.split(":")) == 2  # prefix:hash

    def test_generate_cache_key_with_none_values(self):
        """测试过滤 None 值"""
        key1 = generate_cache_key("api", service_name="test", limit=None)
        key2 = generate_cache_key("api", service_name="test")
        # None 值应该被过滤，所以两个键应该相同
        assert key1 == key2

    def test_generate_cache_key_consistency(self):
        """测试键生成的一致性"""
        # 相同参数应该生成相同的键
        key1 = generate_cache_key("api", service_name="test", limit=10)
        key2 = generate_cache_key("api", service_name="test", limit=10)
        assert key1 == key2

    def test_generate_cache_key_order_independence(self):
        """测试参数顺序无关性"""
        # 参数顺序不同但值相同，应该生成相同的键
        key1 = generate_cache_key("api", service_name="test", limit=10, namespace="default")
        key2 = generate_cache_key("api", namespace="default", service_name="test", limit=10)
        assert key1 == key2

    def test_generate_cache_key_different_params(self):
        """测试不同参数生成不同键"""
        key1 = generate_cache_key("api", service_name="test1")
        key2 = generate_cache_key("api", service_name="test2")
        assert key1 != key2


class TestCachedDecorator:
    """测试缓存装饰器"""

    @pytest.fixture(autouse=True)
    def clear_cache_before_each_test(self):
        """每个测试前清理缓存"""
        from api.cache import _cache_store, _cache_stats
        _cache_store.clear()
        _cache_stats["hits"] = 0
        _cache_stats["misses"] = 0
        _cache_stats["sets"] = 0
        yield
        _cache_store.clear()
        _cache_stats["hits"] = 0
        _cache_stats["misses"] = 0
        _cache_stats["sets"] = 0

    @pytest.mark.asyncio
    async def test_cached_first_call_miss(self):
        """测试首次调用缓存未命中"""
        @cached(ttl=10, key_prefix="test")
        async def mock_function(value):
            return value * 2

        result = await mock_function(5)
        assert result == 10

        # 验证统计
        stats = get_cache_stats()
        assert stats["hits"] == 0
        assert stats["misses"] == 1
        assert stats["sets"] == 1

    @pytest.mark.asyncio
    async def test_cached_second_call_hit(self):
        """测试第二次调用缓存命中"""
        call_count = 0

        @cached(ttl=10, key_prefix="test")
        async def mock_function(value):
            nonlocal call_count
            call_count += 1
            return value * 2

        # 第一次调用
        result1 = await mock_function(5)
        assert result1 == 10
        assert call_count == 1

        # 第二次调用（应该从缓存读取）
        result2 = await mock_function(5)
        assert result2 == 10
        assert call_count == 1  # 函数不应该被再次调用

        # 验证统计
        stats = get_cache_stats()
        assert stats["hits"] == 1
        assert stats["misses"] == 1
        assert stats["sets"] == 1

    @pytest.mark.asyncio
    async def test_cached_different_params(self):
        """测试不同参数分别缓存"""
        call_count = 0

        @cached(ttl=10, key_prefix="test")
        async def mock_function(value):
            nonlocal call_count
            call_count += 1
            return value * 2

        result1 = await mock_function(value=5)
        result2 = await mock_function(value=10)

        assert result1 == 10
        assert result2 == 20
        assert call_count == 2  # 应该调用两次

    @pytest.mark.asyncio
    async def test_cache_expiry(self):
        """测试缓存过期"""
        call_count = 0

        @cached(ttl=1, key_prefix="test")
        async def mock_function(value):
            nonlocal call_count
            call_count += 1
            return value * 2

        # 第一次调用
        result1 = await mock_function(5)
        assert result1 == 10
        assert call_count == 1

        # 等待缓存过期
        await asyncio.sleep(1.5)

        # 第二次调用（缓存已过期）
        result2 = await mock_function(5)
        assert result2 == 10
        assert call_count == 2  # 应该再次调用函数

    @pytest.mark.asyncio
    async def test_cache_with_none_params(self):
        """测试 None 参数过滤"""
        call_count = 0

        @cached(ttl=10, key_prefix="test")
        async def mock_function(value, extra=None):
            nonlocal call_count
            call_count += 1
            return value * 2

        # 两次调用，extra 参数为 None
        await mock_function(5, None)
        await mock_function(5, None)

        # 应该只调用一次（None 被过滤）
        assert call_count == 1

    @pytest.mark.asyncio
    async def test_cache_custom_ttl(self):
        """测试自定义 TTL"""
        @cached(ttl=100, key_prefix="test")
        async def mock_function(value):
            return value * 2

        result = await mock_function(5)
        assert result == 10

        # 验证缓存已设置
        from api.cache import _cache_store
        assert len(_cache_store) == 1

        # 获取实际的缓存键
        cache_key = list(_cache_store.keys())[0]
        assert cache_key.startswith("test:")

        # 检查过期时间
        _, expiry_time = _cache_store[cache_key]
        expected_expiry = time.time() + 100
        # 允许 1 秒误差
        assert abs(expiry_time - expected_expiry) < 1


class TestClearCache:
    """测试清除缓存"""

    @pytest.fixture(autouse=True)
    def setup_cache(self):
        """设置测试缓存"""
        from api.cache import _cache_store
        _cache_store.clear()
        # 添加一些测试数据
        _cache_store["api:key1"] = ("value1", time.time() + 100)
        _cache_store["api:key2"] = ("value2", time.time() + 100)
        _cache_store["metrics:key3"] = ("value3", time.time() + 100)
        yield
        _cache_store.clear()

    def test_clear_all_cache(self):
        """测试清除所有缓存"""
        clear_cache(None)

        from api.cache import _cache_store
        assert len(_cache_store) == 0

    def test_clear_cache_with_pattern(self):
        """测试按模式清除缓存"""
        clear_cache("api:")

        from api.cache import _cache_store
        assert "api:key1" not in _cache_store
        assert "api:key2" not in _cache_store
        # 不匹配的应该保留
        assert "metrics:key3" in _cache_store

    def test_clear_cache_nonexistent_pattern(self):
        """测试清除不存在的模式"""
        initial_size = len(get_cache_stats())

        clear_cache("nonexistent:")

        # 应该没有删除任何东西
        assert len(get_cache_stats()) == initial_size


class TestResetCacheStats:
    """测试重置缓存统计"""

    def test_reset_cache_stats(self):
        """测试重置统计"""
        # 导入并设置统计
        import api.cache
        api.cache._cache_stats["hits"] = 100
        api.cache._cache_stats["misses"] = 50
        api.cache._cache_stats["sets"] = 80

        reset_cache_stats()

        # 重新导入模块以获取更新后的值
        import importlib
        importlib.reload(api.cache)
        assert api.cache._cache_stats["hits"] == 0
        assert api.cache._cache_stats["misses"] == 0
        assert api.cache._cache_stats["sets"] == 0

    def test_get_cache_stats_empty(self, clear_stats):
        """测试空缓存统计"""
        stats = get_cache_stats()

        assert stats["hits"] == 0
        assert stats["misses"] == 0
        assert stats["sets"] == 0
        assert stats["hit_rate"] == "0.00%"
        assert stats["size"] == 0

    def test_get_cache_stats_with_data(self):
        """测试有数据的缓存统计"""
        from api.cache import _cache_stats, _cache_store
        _cache_stats["hits"] = 10
        _cache_stats["misses"] = 5
        _cache_stats["sets"] = 8
        _cache_store["key1"] = ("value", time.time() + 100)

        stats = get_cache_stats()

        assert stats["hits"] == 10
        assert stats["misses"] == 5
        assert stats["sets"] == 8
        # 命中率 = 10 / (10 + 5) = 66.67%
        assert stats["hit_rate"] == "66.67%"
        assert stats["size"] == 1

    def test_get_cache_stats_hit_rate_calculation(self):
        """测试命中率计算"""
        from api.cache import _cache_stats
        _cache_stats["hits"] = 0
        _cache_stats["misses"] = 0

        stats = get_cache_stats()
        assert stats["hit_rate"] == "0.00%"

        _cache_stats["hits"] = 5
        _cache_stats["misses"] = 5

        stats = get_cache_stats()
        assert stats["hit_rate"] == "50.00%"

        _cache_stats["hits"] = 100
        _cache_stats["misses"] = 0

        stats = get_cache_stats()
        assert stats["hit_rate"] == "100.00%"


class TestResetCacheStats:
    """测试重置缓存统计"""

    def test_reset_cache_stats(self):
        """测试重置统计"""
        # 导入并设置统计
        import api.cache
        api.cache._cache_stats["hits"] = 100
        api.cache._cache_stats["misses"] = 50
        api.cache._cache_stats["sets"] = 80

        reset_cache_stats()

        # 重新导入模块以获取更新后的值
        import importlib
        importlib.reload(api.cache)
        assert api.cache._cache_stats["hits"] == 0
        assert api.cache._cache_stats["misses"] == 0
        assert api.cache._cache_stats["sets"] == 0


class TestCacheIntegration:
    """测试缓存集成场景"""

    @pytest.fixture(autouse=True)
    def clear_cache(self):
        """每个测试前清理缓存"""
        from api.cache import _cache_store, _cache_stats
        _cache_store.clear()
        _cache_stats["hits"] = 0
        _cache_stats["misses"] = 0
        _cache_stats["sets"] = 0
        yield
        _cache_store.clear()
        _cache_stats["hits"] = 0
        _cache_stats["misses"] = 0
        _cache_stats["sets"] = 0

    @pytest.mark.asyncio
    async def test_cache_lifecycle(self):
        """测试完整缓存生命周期"""
        @cached(ttl=1, key_prefix="lifecycle")
        async def test_func(value):
            return value * 2

        # 1. 首次调用 - 缓存未命中
        result1 = await test_func(5)
        assert result1 == 10
        stats = get_cache_stats()
        assert stats["misses"] == 1

        # 2. 第二次调用 - 缓存命中
        result2 = await test_func(5)
        assert result2 == 10
        stats = get_cache_stats()
        assert stats["hits"] == 1

        # 3. 清除缓存
        clear_cache("lifecycle:")

        # 4. 再次调用 - 缓存未命中（因为被清除了）
        result3 = await test_func(5)
        assert result3 == 10
        stats = get_cache_stats()
        assert stats["misses"] == 2

    @pytest.mark.asyncio
    async def test_multiple_functions_cache(self):
        """测试多个函数使用缓存"""
        @cached(ttl=10, key_prefix="func1")
        async def func1(value):
            return value * 2

        @cached(ttl=10, key_prefix="func2")
        async def func2(value):
            return value * 3

        result1 = await func1(5)
        result2 = await func2(5)

        assert result1 == 10
        assert result2 == 15

        # 验证缓存大小
        stats = get_cache_stats()
        assert stats["size"] == 2

    @pytest.mark.asyncio
    async def test_cache_with_exception(self):
        """测试函数抛出异常时缓存行为"""
        @cached(ttl=10, key_prefix="error")
        async def failing_func():
            raise ValueError("Test error")

        # 第一次调用应该抛出异常
        with pytest.raises(ValueError, match="Test error"):
            await failing_func()

        # 异常不应该被缓存
        # 第二次调用应该仍然抛出异常
        with pytest.raises(ValueError, match="Test error"):
            await failing_func()

        # 统计应该反映 misses（没有缓存）
        stats = get_cache_stats()
        assert stats["misses"] == 2
        assert stats["hits"] == 0
