"""
Semantic Engine 主应用缓存/去重路由测试

覆盖以下接口函数：
- GET /api/v1/cache/stats
- DELETE /api/v1/cache
- POST /api/v1/cache/clear
- GET /api/v1/deduplication/stats
- POST /api/v1/deduplication/clear-cache
"""
from types import SimpleNamespace
from unittest.mock import Mock, patch

import pytest


@pytest.fixture
def restore_main_storage():
    """测试后恢复 main.storage，避免污染其他用例。"""
    import main

    original = main.storage
    yield
    main.storage = original


class TestMainCacheRoutes:
    """测试缓存路由函数。"""

    @pytest.mark.asyncio
    async def test_get_cache_stats_api(self):
        """测试缓存统计接口返回值。"""
        from main import get_cache_stats_api

        expected = {"total_entries": 5, "expired_entries": 1, "active_entries": 4}
        with patch("main.get_cache_stats", return_value=expected):
            result = await get_cache_stats_api()

        assert result == expected

    @pytest.mark.asyncio
    async def test_clear_cache_delete_api(self):
        """测试 DELETE 清缓存接口。"""
        from main import clear_cache_delete_api

        with patch("main.clear_cache", return_value=3) as mock_clear:
            result = await clear_cache_delete_api("topology")

        assert result["status"] == "ok"
        assert result["cleared"] == 3
        assert result["pattern"] == "topology"
        mock_clear.assert_called_once_with("topology")

    @pytest.mark.asyncio
    async def test_clear_cache_post_api(self):
        """测试 POST 兼容清缓存接口。"""
        from main import clear_cache_api

        with patch("main.clear_cache", return_value=7) as mock_clear:
            result = await clear_cache_api(None)

        assert result["status"] == "ok"
        assert result["message"] == "Cache cleared"
        assert result["cleared"] == 7
        assert result["pattern"] is None
        mock_clear.assert_called_once_with(None)


class TestMainDeduplicationRoutes:
    """测试去重路由函数。"""

    @pytest.mark.asyncio
    async def test_get_deduplication_stats_without_storage(self, restore_main_storage):
        """测试 storage 不可用时返回默认统计。"""
        import main
        from main import get_deduplication_stats_api

        main.storage = None
        result = await get_deduplication_stats_api()

        assert result["total_processed"] == 0
        assert result["duplicates_found"] == 0
        assert result["duplicate_rate"] == 0.0
        assert result["id_cache_size"] == 0
        assert result["semantic_cache_size"] == 0

    @pytest.mark.asyncio
    async def test_get_deduplication_stats_with_deduplicator(self, restore_main_storage):
        """测试返回 deduplicator 统计结果。"""
        import main
        from main import get_deduplication_stats_api

        mock_deduplicator = Mock()
        mock_deduplicator.get_stats.return_value = {
            "total_processed": 100,
            "duplicates_found": 20,
            "duplicate_rate": 0.2,
            "id_cache_size": 10,
            "semantic_cache_size": 15,
        }
        main.storage = SimpleNamespace(deduplicator=mock_deduplicator)

        result = await get_deduplication_stats_api()

        assert result["total_processed"] == 100
        assert result["duplicates_found"] == 20
        assert result["duplicate_rate"] == 0.2
        mock_deduplicator.get_stats.assert_called_once()

    @pytest.mark.asyncio
    async def test_clear_deduplication_cache_api(self, restore_main_storage):
        """测试清除 dedup 缓存接口。"""
        import main
        from main import clear_deduplication_cache_api

        mock_deduplicator = Mock()
        main.storage = SimpleNamespace(deduplicator=mock_deduplicator)

        result = await clear_deduplication_cache_api()

        assert result["status"] == "ok"
        assert result["message"] == "Deduplication cache cleared"
        mock_deduplicator.clear_cache.assert_called_once()
