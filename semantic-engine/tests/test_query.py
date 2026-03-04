"""
Query API 模块单元测试

测试 api/query.py 的核心功能：
- Metrics 查询
- Traces 查询
- 统计信息获取
- 缓存管理
"""
import pytest
from unittest.mock import Mock, patch
from datetime import datetime

from api.query import (
    query_metrics,
    query_metrics_stats,
    query_traces,
    query_traces_stats,
    get_cache_statistics,
    clear_api_cache,
    set_storage_adapter
)
from fastapi import HTTPException


@pytest.fixture(autouse=True)
def clear_storage_adapter():
    """每个测试前清理 storage adapter"""
    from api import query
    original_storage = query._STORAGE_ADAPTER
    query._STORAGE_ADAPTER = None
    yield
    query._STORAGE_ADAPTER = original_storage


@pytest.fixture
def mock_storage():
    """Mock storage adapter"""
    storage = Mock()
    return storage


class TestSetStorageAdapter:
    """测试设置 storage adapter"""

    def test_set_storage_adapter(self, mock_storage):
        """测试设置 storage adapter"""
        set_storage_adapter(mock_storage)

        from api.query import _STORAGE_ADAPTER
        assert _STORAGE_ADAPTER == mock_storage


class TestQueryMetrics:
    """测试 Metrics 查询"""

    @pytest.fixture
    def setup_storage(self, mock_storage):
        """设置 storage adapter"""
        set_storage_adapter(mock_storage)

    @pytest.mark.asyncio
    async def test_query_metrics_basic(self, setup_storage, mock_storage):
        """测试基本查询"""
        mock_storage.execute_query = Mock(return_value=[
            {
                "timestamp": "2026-02-09T12:00:00Z",
                "service_name": "api-server",
                "metric_name": "cpu_usage",
                "value": 75.5,
                "labels": {"host": "server-1"}
            }
        ])

        result = await query_metrics(
            limit=100,
            service_name=None,
            metric_name=None,
            start_time=None,
            end_time=None
        )

        assert result["count"] == 1
        assert result["limit"] == 100
        assert len(result["data"]) == 1
        assert result["data"][0]["service_name"] == "api-server"

    @pytest.mark.asyncio
    async def test_query_metrics_with_service_filter(self, setup_storage, mock_storage):
        """测试按服务名过滤"""
        mock_storage.execute_query = Mock(return_value=[])

        await query_metrics(
            limit=100,
            service_name="api-server",
            metric_name=None,
            start_time=None,
            end_time=None
        )

        # 验证查询语句包含服务名过滤
        query = mock_storage.execute_query.call_args[0][0]
        assert "service_name = 'api-server'" in query

    @pytest.mark.asyncio
    async def test_query_metrics_with_metric_filter(self, setup_storage, mock_storage):
        """测试按指标名过滤"""
        mock_storage.execute_query = Mock(return_value=[])

        await query_metrics(
            limit=100,
            service_name=None,
            metric_name="cpu_usage",
            start_time=None,
            end_time=None
        )

        # 验证查询语句包含指标名过滤
        query = mock_storage.execute_query.call_args[0][0]
        assert "metric_name = 'cpu_usage'" in query

    @pytest.mark.asyncio
    async def test_query_metrics_with_time_range(self, setup_storage, mock_storage):
        """测试时间范围过滤"""
        mock_storage.execute_query = Mock(return_value=[])

        await query_metrics(
            limit=100,
            service_name=None,
            metric_name=None,
            start_time="2026-02-09T00:00:00Z",
            end_time="2026-02-09T23:59:59Z"
        )

        # 验证查询语句包含时间范围
        query = mock_storage.execute_query.call_args[0][0]
        assert "timestamp >= '2026-02-09T00:00:00Z'" in query
        assert "timestamp <= '2026-02-09T23:59:59Z'" in query

    @pytest.mark.asyncio
    async def test_query_metrics_with_all_filters(self, setup_storage, mock_storage):
        """测试所有过滤条件"""
        mock_storage.execute_query = Mock(return_value=[])

        await query_metrics(
            limit=50,
            service_name="api-server",
            metric_name="cpu_usage",
            start_time="2026-02-09T00:00:00Z",
            end_time="2026-02-09T23:59:59Z"
        )

        # 验证查询语句包含所有过滤条件
        query = mock_storage.execute_query.call_args[0][0]
        assert "service_name = 'api-server'" in query
        assert "metric_name = 'cpu_usage'" in query
        assert "timestamp >= '2026-02-09T00:00:00Z'" in query
        assert "timestamp <= '2026-02-09T23:59:59Z'" in query
        assert "LIMIT 50" in query

    @pytest.mark.asyncio
    async def test_query_metrics_no_storage(self):
        """测试没有初始化 storage"""
        from api.query import _STORAGE_ADAPTER
        _STORAGE_ADAPTER = None

        with pytest.raises(HTTPException) as exc_info:
            await query_metrics()

        assert exc_info.value.status_code == 503
        assert "not initialized" in exc_info.value.detail

    @pytest.mark.asyncio
    async def test_query_metrics_storage_error(self, setup_storage, mock_storage):
        """测试存储错误"""
        mock_storage.execute_query = Mock(side_effect=Exception("Database error"))

        with pytest.raises(HTTPException) as exc_info:
            await query_metrics()

        assert exc_info.value.status_code == 500
        assert "Database error" in exc_info.value.detail

    @pytest.mark.asyncio
    async def test_query_metrics_empty_results(self, setup_storage, mock_storage):
        """测试空结果"""
        mock_storage.execute_query = Mock(return_value=[])

        result = await query_metrics()

        assert result["count"] == 0
        assert result["data"] == []

    @pytest.mark.asyncio
    async def test_query_metrics_ordering(self, setup_storage, mock_storage):
        """测试结果排序"""
        mock_storage.execute_query = Mock(return_value=[])

        await query_metrics()

        # 验证查询包含 ORDER BY
        query = mock_storage.execute_query.call_args[0][0]
        assert "ORDER BY timestamp DESC" in query


class TestQueryMetricsStats:
    """测试 Metrics 统计信息"""

    @pytest.fixture
    def setup_storage(self, mock_storage):
        """设置 storage adapter"""
        set_storage_adapter(mock_storage)

    @pytest.mark.asyncio
    async def test_query_metrics_stats_basic(self, setup_storage, mock_storage):
        """测试基本统计"""
        # Mock 查询结果
        mock_storage.execute_query = Mock(side_effect=[
            [{"total": 1000}],  # 总数
            [
                {"service_name": "api-server", "count": 500},
                {"service_name": "frontend", "count": 300},
                {"service_name": "database", "count": 200}
            ],  # 按服务
            [
                {"metric_name": "cpu_usage", "count": 400},
                {"metric_name": "memory_usage", "count": 300},
                {"metric_name": "http_requests", "count": 300}
            ]  # 按指标名
        ])

        result = await query_metrics_stats()

        assert result["total"] == 1000
        assert result["byService"]["api-server"] == 500
        assert result["byService"]["frontend"] == 300
        assert result["byMetricName"]["cpu_usage"] == 400

    @pytest.mark.asyncio
    async def test_query_metrics_stats_no_storage(self):
        """测试没有初始化 storage"""
        from api.query import _STORAGE_ADAPTER
        _STORAGE_ADAPTER = None

        with pytest.raises(HTTPException) as exc_info:
            await query_metrics_stats()

        assert exc_info.value.status_code == 503

    @pytest.mark.asyncio
    async def test_query_metrics_stats_empty_data(self, setup_storage, mock_storage):
        """测试空数据"""
        mock_storage.execute_query = Mock(side_effect=[
            [],  # 总数 - 空列表
            [],   # 按服务
            []    # 按指标名
        ])

        result = await query_metrics_stats()

        assert result["total"] == 0
        assert result["byService"] == {}
        assert result["byMetricName"] == {}

    @pytest.mark.asyncio
    async def test_query_metrics_stats_storage_error(self, setup_storage, mock_storage):
        """测试存储错误"""
        mock_storage.execute_query = Mock(side_effect=Exception("Query failed"))

        with pytest.raises(HTTPException) as exc_info:
            await query_metrics_stats()

        assert exc_info.value.status_code == 500
        assert "Query failed" in exc_info.value.detail


class TestQueryTraces:
    """测试 Traces 查询"""

    @pytest.fixture
    def setup_storage(self, mock_storage):
        """设置 storage adapter"""
        set_storage_adapter(mock_storage)

    @pytest.mark.asyncio
    async def test_query_traces_basic(self, setup_storage, mock_storage):
        """测试基本查询"""
        mock_storage.execute_query = Mock(return_value=[
            {
                "trace_id": "trace-123",
                "span_id": "span-456",
                "parent_span_id": "span-789",
                "service_name": "api-server",
                "operation_name": "/api/users",
                "start_time_str": "2026-02-09T12:00:00Z",
                "duration_ms": 125,
                "status": "STATUS_CODE_OK"
            }
        ])

        result = await query_traces(
            limit=100,
            service_name=None,
            trace_id=None,
            start_time=None,
            end_time=None
        )

        assert result["count"] == 1
        assert result["limit"] == 100
        assert len(result["data"]) == 1
        assert result["data"][0]["trace_id"] == "trace-123"

    @pytest.mark.asyncio
    async def test_query_traces_with_service_filter(self, setup_storage, mock_storage):
        """测试按服务名过滤"""
        mock_storage.execute_query = Mock(return_value=[])

        await query_traces(
            limit=100,
            service_name="api-server",
            trace_id=None,
            start_time=None,
            end_time=None
        )

        # 验证查询语句包含服务名过滤
        query = mock_storage.execute_query.call_args[0][0]
        assert "service_name = 'api-server'" in query

    @pytest.mark.asyncio
    async def test_query_traces_with_trace_id_filter(self, setup_storage, mock_storage):
        """测试按 trace_id 过滤"""
        mock_storage.execute_query = Mock(return_value=[])

        await query_traces(
            limit=100,
            service_name=None,
            trace_id="trace-123",
            start_time=None,
            end_time=None
        )

        # 验证查询语句包含 trace_id 过滤
        query = mock_storage.execute_query.call_args[0][0]
        assert "trace_id = 'trace-123'" in query

    @pytest.mark.asyncio
    async def test_query_traces_with_time_range(self, setup_storage, mock_storage):
        """测试时间范围过滤"""
        mock_storage.execute_query = Mock(return_value=[])

        await query_traces(
            limit=100,
            service_name=None,
            trace_id=None,
            start_time="2026-02-09T00:00:00Z",
            end_time="2026-02-09T23:59:59Z"
        )

        # 验证查询语句包含时间范围
        query = mock_storage.execute_query.call_args[0][0]
        assert "start_time >= '2026-02-09T00:00:00Z'" in query
        assert "start_time <= '2026-02-09T23:59:59Z'" in query

    @pytest.mark.asyncio
    async def test_query_traces_no_storage(self):
        """测试没有初始化 storage"""
        from api.query import _STORAGE_ADAPTER
        _STORAGE_ADAPTER = None

        with pytest.raises(HTTPException) as exc_info:
            await query_traces()

        assert exc_info.value.status_code == 503

    @pytest.mark.asyncio
    async def test_query_traces_storage_error(self, setup_storage, mock_storage):
        """测试存储错误"""
        mock_storage.execute_query = Mock(side_effect=Exception("Database error"))

        with pytest.raises(HTTPException) as exc_info:
            await query_traces()

        assert exc_info.value.status_code == 500

    @pytest.mark.asyncio
    async def test_query_traces_empty_results(self, setup_storage, mock_storage):
        """测试空结果"""
        mock_storage.execute_query = Mock(return_value=[])

        result = await query_traces()

        assert result["count"] == 0
        assert result["data"] == []


class TestQueryTracesStats:
    """测试 Traces 统计信息"""

    @pytest.fixture
    def setup_storage(self, mock_storage):
        """设置 storage adapter"""
        set_storage_adapter(mock_storage)

    @pytest.mark.asyncio
    async def test_query_traces_stats_basic(self, setup_storage, mock_storage):
        """测试基本统计"""
        # Mock 查询结果
        mock_storage.execute_query = Mock(side_effect=[
            [{"total": 5000}],  # 总数
            [
                {"service_name": "api-server", "count": 3000},
                {"service_name": "frontend", "count": 2000}
            ],  # 按服务
            [
                {"operation_name": "/api/users", "count": 1500},
                {"operation_name": "/api/orders", "count": 1000}
            ],  # 按操作
            [{"avg_duration": 95.5}]  # 平均持续时间
        ])

        result = await query_traces_stats()

        assert result["total"] == 5000
        assert result["byService"]["api-server"] == 3000
        assert result["byOperation"]["/api/users"] == 1500
        assert result["avgDuration"] == 95.5

    @pytest.mark.asyncio
    async def test_query_traces_stats_no_storage(self):
        """测试没有初始化 storage"""
        from api.query import _STORAGE_ADAPTER
        _STORAGE_ADAPTER = None

        with pytest.raises(HTTPException) as exc_info:
            await query_traces_stats()

        assert exc_info.value.status_code == 503

    @pytest.mark.asyncio
    async def test_query_traces_stats_empty_data(self, setup_storage, mock_storage):
        """测试空数据"""
        mock_storage.execute_query = Mock(side_effect=[
            [],  # 总数 - 空列表
            [],   # 按服务
            [],   # 按操作
            []  # 平均持续时间 - 空列表
        ])

        result = await query_traces_stats()

        assert result["total"] == 0
        assert result["byService"] == {}
        assert result["byOperation"] == {}
        assert result["avgDuration"] == 0

    @pytest.mark.asyncio
    async def test_query_traces_stats_rounding(self, setup_storage, mock_storage):
        """测试平均值四舍五入"""
        mock_storage.execute_query = Mock(side_effect=[
            [{"total": 100}],
            [],
            [],
            [{"avg_duration": 95.456}]  # 需要四舍五入
        ])

        result = await query_traces_stats()

        assert result["avgDuration"] == 95.46

    @pytest.mark.asyncio
    async def test_query_traces_stats_storage_error(self, setup_storage, mock_storage):
        """测试存储错误"""
        mock_storage.execute_query = Mock(side_effect=Exception("Query failed"))

        with pytest.raises(HTTPException) as exc_info:
            await query_traces_stats()

        assert exc_info.value.status_code == 500


class TestCacheManagement:
    """测试缓存管理"""

    @pytest.mark.asyncio
    async def test_get_cache_statistics(self):
        """测试获取缓存统计"""
        with patch('api.query.get_cache_stats') as mock_get_stats:
            mock_get_stats.return_value = {
                "total_keys": 100,
                "hits": 1000,
                "misses": 100,
                "hit_rate": 0.91
            }

            result = await get_cache_statistics()

            assert result["total_keys"] == 100
            assert result["hit_rate"] == 0.91

    @pytest.mark.asyncio
    async def test_clear_api_cache(self):
        """测试清除 API 缓存"""
        with patch('api.query.clear_cache') as mock_clear:
            await clear_api_cache(pattern="topology:*")

            mock_clear.assert_called_once_with("topology:*")

    @pytest.mark.asyncio
    async def test_clear_api_cache_no_pattern(self):
        """测试清除所有缓存"""
        with patch('api.query.clear_cache') as mock_clear:
            await clear_api_cache(pattern=None)

            mock_clear.assert_called_once_with(None)


class TestEdgeCases:
    """测试边界情况"""

    @pytest.fixture
    def setup_storage(self, mock_storage):
        """设置 storage adapter"""
        set_storage_adapter(mock_storage)

    @pytest.mark.asyncio
    async def test_query_metrics_limit_validation(self, setup_storage, mock_storage):
        """测试 limit 参数范围验证"""
        # FastAPI 会自动验证 ge=1, le=10000
        # 这里测试查询语句是否使用了正确的 limit
        mock_storage.execute_query = Mock(return_value=[])

        await query_metrics(limit=5000)

        query = mock_storage.execute_query.call_args[0][0]
        assert "LIMIT 5000" in query

    @pytest.mark.asyncio
    async def test_query_traces_limit_validation(self, setup_storage, mock_storage):
        """测试 traces limit 参数范围验证"""
        mock_storage.execute_query = Mock(return_value=[])

        await query_traces(limit=10000)

        query = mock_storage.execute_query.call_args[0][0]
        assert "LIMIT 10000" in query

    @pytest.mark.asyncio
    async def test_query_metrics_with_null_values(self, setup_storage, mock_storage):
        """测试处理 NULL 值"""
        mock_storage.execute_query = Mock(return_value=[
            {
                "timestamp": "2026-02-09T12:00:00Z",
                "service_name": None,
                "metric_name": "cpu_usage",
                "value": None,
                "labels": {}
            }
        ])

        result = await query_metrics()

        assert result["count"] == 1
        assert result["data"][0]["service_name"] is None

    @pytest.mark.asyncio
    async def test_query_metrics_stats_with_single_result(self, setup_storage, mock_storage):
        """测试只有一个结果的统计"""
        mock_storage.execute_query = Mock(side_effect=[
            [{"total": 1}],
            [{"service_name": "api-server", "count": 1}],
            [{"metric_name": "cpu_usage", "count": 1}]
        ])

        result = await query_metrics_stats()

        assert result["total"] == 1
        assert len(result["byService"]) == 1
        assert len(result["byMetricName"]) == 1

    @pytest.mark.asyncio
    async def test_query_traces_with_none_timestamp(self, setup_storage, mock_storage):
        """测试时间戳为 None 的情况"""
        mock_storage.execute_query = Mock(return_value=[])

        # 不提供时间范围
        await query_traces(start_time=None, end_time=None)

        # 验证查询被执行
        assert mock_storage.execute_query.called
        # 查询语句被正确构建（由于没有过滤条件，可能不包含 WHERE）
        query = mock_storage.execute_query.call_args[0][0]
        # 查询应该包含基本的 SELECT 语句
        assert "SELECT" in query
        assert "FROM traces" in query
