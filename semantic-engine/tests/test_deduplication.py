"""
Storage Deduplication 模块单元测试

测试 storage/deduplication.py 的核心功能：
- DataDeduplicator 类的所有方法
- 语义去重逻辑
- 时间窗口去重
- 缓存管理
- 统计信息
"""
import pytest
from unittest.mock import Mock, patch
from datetime import datetime, timedelta, timezone
from collections import defaultdict

# 导入需要的模块，确保在测试类中可用
import storage.deduplication as dedup_module
DataDeduplicator = dedup_module.DataDeduplicator
get_deduplicator = dedup_module.get_deduplicator
save_event_with_deduplication = dedup_module.save_event_with_deduplication


@pytest.fixture
def mock_storage():
    """Mock storage adapter"""
    storage = Mock()
    storage.ch_client = None
    storage.execute_query = Mock(return_value=[])
    return storage


@pytest.fixture
def duplicator(mock_storage):
    """创建 DataDeduplicator 实例"""
    return DataDeduplicator(mock_storage)


class TestDataDeduplicatorInit:
    """测试 DataDeduplicator 初始化"""

    def test_init(self, mock_storage):
        """测试初始化"""
        duplicator = DataDeduplicator(mock_storage)

        assert duplicator.storage == mock_storage
        assert duplicator._event_id_cache == set()
        assert duplicator._semantic_key_cache == set()
        assert duplicator._stats == {
            "total_processed": 0,
            "duplicates_found": 0,
            "duplicates_by_id": 0,
            "duplicates_by_semantic": 0
        }

    def test_cache_ttl_default(self, mock_storage):
        """测试默认缓存 TTL"""
        duplicator = DataDeduplicator(mock_storage)

        assert duplicator._cache_ttl == timedelta(minutes=5)


class TestIsDuplicateEvent:
    """测试 is_duplicate_event 方法"""

    def test_not_duplicate(self, duplicator):
        """测试不重复的事件"""
        event = {
            "id": "evt-123",
            "entity": {"name": "api-server"},
            "timestamp": "2026-02-09T12:34:56.250Z",
            "event": {"raw": "Test message"}
        }

        is_dup, reason = duplicator.is_duplicate_event(event, check_existing=False)

        assert is_dup is False
        assert reason is None
        assert duplicator._stats["total_processed"] == 1

    def test_duplicate_by_id(self, duplicator):
        """测试基于 ID 的重复检测"""
        event = {
            "id": "evt-123",
            "entity": {"name": "api-server"},
            "timestamp": "2026-02-09T12:34:56.250Z",
            "event": {"raw": "Test message"}
        }

        # 第一次不重复
        is_dup1, reason1 = duplicator.is_duplicate_event(event, check_existing=False)
        assert is_dup1 is False

        # 添加到缓存
        duplicator._event_id_cache.add("evt-123")

        # 第二次重复
        is_dup2, reason2 = duplicator.is_duplicate_event(event, check_existing=False)
        assert is_dup2 is True
        assert "duplicate_id" in reason2

    def test_duplicate_by_semantic(self, duplicator):
        """测试语义重复检测"""
        event = {
            "id": "evt-456",  # 不同 ID
            "entity": {"name": "api-server"},
            "timestamp": "2026-02-09T12:34:56.250Z",
            "event": {"raw": "Test message"}
        }

        # 第一次不重复
        is_dup1, reason1 = duplicator.is_duplicate_event(event, check_existing=False)
        assert is_dup1 is False

        # 第二次也检查，但需要先添加到缓存
        # 直接使用第一次生成的语义键
        semantic_key = duplicator._generate_semantic_key(event)
        duplicator._semantic_key_cache.add(semantic_key)

        is_dup2, reason2 = duplicator.is_duplicate_event(event, check_existing=False)
        assert is_dup2 is True
        assert "duplicate_semantic" in reason2

    def test_duplicate_by_time_window(self, duplicator):
        """测试时间窗口重复检测"""
        event = {
            "id": "evt-789",
            "entity": {"name": "api-server"},
            "timestamp": "2026-02-09T12:34:56.250Z",
            "event": {"raw": "Test message"}
        }

        # 生成语义键并添加到缓存
        semantic_key = duplicator._generate_semantic_key(event)
        duplicator._semantic_key_cache.add(semantic_key)

        is_dup, reason = duplicator.is_duplicate_event(event, check_existing=False)
        # 由于语义键已存在，会被检测为重复
        assert is_dup is True

    def test_missing_required_fields(self, duplicator):
        """测试缺少必需字段"""
        # 缺少所有必需字段
        event = {"id": "evt-123"}

        is_dup, reason = duplicator.is_duplicate_event(event, check_existing=False)
        assert is_dup is False  # 缺少字段不视为重复

    def test_exception_handling(self, duplicator):
        """测试异常处理"""
        # 传入无效数据导致异常
        event = None

        # 应该捕获异常并返回 False
        is_dup, reason = duplicator.is_duplicate_event(event, check_existing=False)
        assert is_dup is False


class TestIsDuplicateById:
    """测试 _is_duplicate_by_id 方法"""

    def test_empty_event_id(self, duplicator):
        """测试空 event_id"""
        result = duplicator._is_duplicate_by_id("", check_existing=False)
        assert result is False

    def test_none_event_id(self, duplicator):
        """测试 None event_id"""
        result = duplicator._is_duplicate_by_id(None, check_existing=False)
        assert result is False

    def test_duplicate_in_cache(self, duplicator):
        """测试缓存中的重复"""
        duplicator._event_id_cache.add("evt-123")

        result = duplicator._is_duplicate_by_id("evt-123", check_existing=False)
        assert result is True

    def test_check_existing_with_no_client(self, duplicator):
        """测试没有 ClickHouse 客户端"""
        duplicator.storage.ch_client = None

        result = duplicator._is_duplicate_by_id("evt-123", check_existing=True)
        assert result is False

    def test_check_existing_database_query(self, duplicator):
        """测试数据库查询"""
        duplicator.storage.ch_client = Mock()
        duplicator.storage.execute_query = Mock(return_value=[[1]])

        result = duplicator._is_duplicate_by_id("evt-123", check_existing=True)
        assert result is True
        assert "evt-123" in duplicator._event_id_cache

    def test_check_existing_database_query_escapes_event_id(self, duplicator):
        """event_id 进入 SQL 前应进行字符串转义。"""
        duplicator.storage.ch_client = Mock()
        captured = {}

        def _capture(query):
            captured["query"] = query
            return [[0]]

        duplicator.storage.execute_query = Mock(side_effect=_capture)

        duplicator._is_duplicate_by_id("evt-' OR 1=1 --", check_existing=True)

        assert "PREWHERE id = 'evt-'' OR 1=1 --'" in captured["query"]

    def test_check_existing_database_not_found(self, duplicator):
        """测试数据库未找到"""
        duplicator.storage.ch_client = Mock()
        duplicator.storage.execute_query = Mock(return_value=[[0]])

        result = duplicator._is_duplicate_by_id("evt-123", check_existing=True)
        assert result is False

    def test_check_existing_database_error(self, duplicator):
        """测试数据库错误"""
        duplicator.storage.ch_client = Mock()
        duplicator.storage.execute_query = Mock(side_effect=Exception("DB error"))

        result = duplicator._is_duplicate_by_id("evt-123", check_existing=True)
        assert result is False


class TestGenerateSemanticKey:
    """测试 _generate_semantic_key 方法"""

    def test_generate_valid_key(self, duplicator):
        """测试生成有效的语义键"""
        event = {
            "entity": {"name": "api-server"},
            "timestamp": "2026-02-09T12:34:56.250Z",
            "event": {"raw": "Test message"}
        }

        key = duplicator._generate_semantic_key(event)

        # 正确的哈希值: MD5("Test message")[:16]
        # MD5("Test message")[:16] = 82dfa5549ebc9afc
        assert key == "api-server|2026-02-09T12:34:56|82dfa5549ebc9afc"
        assert "api-server" in key
        assert "12:34:56" in key

    def test_missing_service_name(self, duplicator):
        """测试缺少 service_name"""
        event = {
            "entity": {},
            "timestamp": "2026-02-09T12:34:56.250Z",
            "event": {"raw": "Test message"}
        }

        key = duplicator._generate_semantic_key(event)
        assert key is None

    def test_missing_timestamp(self, duplicator):
        """测试缺少 timestamp"""
        event = {
            "entity": {"name": "api-server"},
            "event": {"raw": "Test message"}
        }

        key = duplicator._generate_semantic_key(event)
        assert key is None

    def test_missing_message(self, duplicator):
        """测试缺少 message"""
        event = {
            "entity": {"name": "api-server"},
            "timestamp": "2026-02-09T12:34:56.250Z",
            "event": {}
        }

        key = duplicator._generate_semantic_key(event)
        assert key is None

    def test_timestamp_without_nanoseconds(self, duplicator):
        """测试不带纳秒的时间戳"""
        event = {
            "entity": {"name": "api-server"},
            "timestamp": "2026-02-09T12:34:56Z",
            "event": {"raw": "Test message"}
        }

        key = duplicator._generate_semantic_key(event)
        # 不带纳秒的时间戳会保留 'Z'
        assert key == "api-server|2026-02-09T12:34:56Z|82dfa5549ebc9afc"

    def test_different_messages_generate_different_keys(self, duplicator):
        """测试不同消息生成不同的键"""
        event1 = {
            "entity": {"name": "api-server"},
            "timestamp": "2026-02-09T12:34:56.250Z",
            "event": {"raw": "Message 1"}
        }
        event2 = {
            "entity": {"name": "api-server"},
            "timestamp": "2026-02-09T12:34:56.250Z",
            "event": {"raw": "Message 2"}
        }

        key1 = duplicator._generate_semantic_key(event1)
        key2 = duplicator._generate_semantic_key(event2)

        assert key1 != key2

    def test_exception_handling(self, duplicator):
        """测试异常处理"""
        # 传入 None
        key = duplicator._generate_semantic_key(None)
        assert key is None


class TestIsDuplicateBySemanticKey:
    """测试 _is_duplicate_by_semantic_key 方法"""

    def test_empty_semantic_key(self, duplicator):
        """测试空的语义键"""
        result = duplicator._is_duplicate_by_semantic_key("", check_existing=False)
        assert result is False

    def test_none_semantic_key(self, duplicator):
        """测试 None 语义键"""
        result = duplicator._is_duplicate_by_semantic_key(None, check_existing=False)
        assert result is False

    def test_duplicate_in_cache(self, duplicator):
        """测试缓存中的重复"""
        key = "api-server|2026-02-09T12:34:56|82dfa5549ebc9afc"
        duplicator._semantic_key_cache.add(key)

        result = duplicator._is_duplicate_by_semantic_key(key, check_existing=False)
        assert result is True

    def test_check_existing_with_no_client(self, duplicator):
        """测试没有 ClickHouse 客户端"""
        duplicator.storage.ch_client = None

        result = duplicator._is_duplicate_by_semantic_key(
            "api-server|2026-02-09T12:34:56|82dfa5549ebc9afc",
            check_existing=True
        )
        assert result is False

    def test_check_existing_database_query(self, duplicator):
        """测试数据库查询"""
        duplicator.storage.ch_client = Mock()
        duplicator.storage.execute_query = Mock(return_value=[[1]])

        result = duplicator._is_duplicate_by_semantic_key(
            "api-server|2026-02-09T12:34:56|82dfa5549ebc9afc",
            check_existing=True
        )
        assert result is True
        assert "api-server|2026-02-09T12:34:56|82dfa5549ebc9afc" in duplicator._semantic_key_cache

    def test_check_existing_database_query_escapes_semantic_key_parts(self, duplicator):
        """语义键拆分后的 service/message_hash 进入 SQL 前应转义。"""
        duplicator.storage.ch_client = Mock()
        captured = {}

        def _capture(query):
            captured["query"] = query
            return [[0]]

        duplicator.storage.execute_query = Mock(side_effect=_capture)

        duplicator._is_duplicate_by_semantic_key(
            "api-'srv|2026-02-09T12:34:56|82dfa55'49ebc9afc",
            check_existing=True,
        )

        assert "WHERE service_name = 'api-''srv'" in captured["query"]
        assert "substring(MD5(message), 1, 16) = '82dfa55''49ebc9afc'" in captured["query"]

    def test_check_existing_database_not_found(self, duplicator):
        """测试数据库未找到"""
        duplicator.storage.ch_client = Mock()
        duplicator.storage.execute_query = Mock(return_value=[[0]])

        result = duplicator._is_duplicate_by_semantic_key(
            "api-server|2026-02-09T12:34:56|82dfa5549ebc9afc",
            check_existing=True
        )
        assert result is False

    def test_invalid_semantic_key_format(self, duplicator):
        """测试无效的语义键格式"""
        # 缺少分隔符
        invalid_key = "invalid_key"

        result = duplicator._is_duplicate_by_semantic_key(invalid_key, check_existing=True)
        assert result is False


class TestIsDuplicateByTimeWindow:
    """测试 _is_duplicate_by_time_window 方法"""

    def test_duplicate_in_cache(self, duplicator):
        """测试缓存中的重复"""
        event = {
            "entity": {"name": "api-server"},
            "timestamp": "2026-02-09T12:34:56.250Z",
            "event": {"raw": "Test message"}
        }

        # _is_duplicate_by_time_window 检查 time_key 和完整的 message_hash
        # time_key = "api-server|2026-02-09T12:34:56"
        # message_hash = 完整的 MD5 哈希（32个字符）
        import hashlib
        time_key = "api-server|2026-02-09T12:34:56"
        message_hash = hashlib.md5("Test message".encode('utf-8')).hexdigest()

        # 添加 time_key 到缓存
        duplicator._semantic_key_cache.add(time_key)
        # 添加完整的哈希键到缓存
        duplicator._semantic_key_cache.add(f"{time_key}|{message_hash}")

        result = duplicator._is_duplicate_by_time_window(event)
        assert result is True

    def test_not_duplicate(self, duplicator):
        """测试不重复"""
        event = {
            "entity": {"name": "api-server"},
            "timestamp": "2026-02-09T12:34:56.250Z",
            "event": {"raw": "Test message"}
        }

        result = duplicator._is_duplicate_by_time_window(event)
        assert result is False

    def test_missing_service_name(self, duplicator):
        """测试缺少服务名"""
        event = {
            "entity": {},
            "timestamp": "2026-02-09T12:34:56.250Z",
            "event": {"raw": "Test message"}
        }

        result = duplicator._is_duplicate_by_time_window(event)
        assert result is False

    def test_missing_timestamp(self, duplicator):
        """测试缺少时间戳"""
        event = {
            "entity": {"name": "api-server"},
            "event": {"raw": "Test message"}
        }

        result = duplicator._is_duplicate_by_time_window(event)
        assert result is False

    def test_missing_message(self, duplicator):
        """测试缺少消息"""
        event = {
            "entity": {"name": "api-server"},
            "timestamp": "2026-02-09T12:34:56.250Z",
            "event": {}
        }

        result = duplicator._is_duplicate_by_time_window(event)
        assert result is False


class TestClearCache:
    """测试 clear_cache 方法"""

    @pytest.fixture
    def duplicator_with_cache(self, mock_storage):
        """创建带缓存的 duplicator"""
        duplicator = DataDeduplicator(mock_storage)
        # 添加一些缓存数据
        duplicator._event_id_cache.add("evt-1")
        duplicator._event_id_cache.add("evt-2")
        duplicator._semantic_key_cache.add("key-1")
        duplicator._semantic_key_cache.add("key-2")
        return duplicator

    def test_clear_cache(self, duplicator_with_cache):
        """测试清空缓存"""
        assert len(duplicator_with_cache._event_id_cache) == 2
        assert len(duplicator_with_cache._semantic_key_cache) == 2

        duplicator_with_cache.clear_cache()

        assert len(duplicator_with_cache._event_id_cache) == 0
        assert len(duplicator_with_cache._semantic_key_cache) == 0


class TestGetStats:
    """测试 get_stats 方法"""

    def test_get_stats_initial(self, duplicator):
        """测试初始统计"""
        stats = duplicator.get_stats()

        assert stats["total_processed"] == 0
        assert stats["duplicates_found"] == 0
        assert stats["duplicates_by_id"] == 0
        assert stats["duplicates_by_semantic"] == 0
        assert stats["duplicate_rate"] == 0.0
        assert stats["id_cache_size"] == 0
        assert stats["semantic_cache_size"] == 0

    def test_get_stats_with_data(self, duplicator):
        """测试有数据的统计"""
        duplicator._stats = {
            "total_processed": 100,
            "duplicates_found": 10,
            "duplicates_by_id": 5,
            "duplicates_by_semantic": 5
        }
        duplicator._event_id_cache.add("evt-1")
        duplicator._semantic_key_cache.add("key-1")

        stats = duplicator.get_stats()

        assert stats["total_processed"] == 100
        assert stats["duplicates_found"] == 10
        assert stats["duplicate_rate"] == 0.1
        assert stats["id_cache_size"] == 1
        assert stats["semantic_cache_size"] == 1

    def test_get_stats_cache_age(self, duplicator):
        """测试缓存时间统计"""
        old_timestamp = datetime(2026, 2, 9, 12, 0, 0, tzinfo=timezone.utc)
        duplicator._cache_timestamp = old_timestamp

        stats = duplicator.get_stats()
        assert stats["cache_age_seconds"] > 0


class TestAnalyzeDuplicateSources:
    """测试 analyze_duplicate_sources 方法"""

    @pytest.fixture
    def duplicator_with_client(self, mock_storage):
        """创建带 ClickHouse 客户端的 duplicator"""
        duplicator = DataDeduplicator(mock_storage)
        mock_storage.ch_client = Mock()
        return duplicator

    def test_analyze_without_client(self, duplicator):
        """测试没有 ClickHouse 客户端"""
        duplicator.storage.ch_client = None

        result = duplicator.analyze_duplicate_sources()

        assert "error" in result
        assert "not available" in result["error"]

    def test_analyze_basic(self, duplicator_with_client):
        """测试基本分析"""
        duplicator_with_client.storage.execute_query = Mock(return_value=[
            (
                "api-server",
                datetime(2026, 2, 9, 12, 34, 56),
                "abc123",
                5,
                ["evt-1", "evt-2", "evt-3", "evt-4", "evt-5"]
            )
        ])

        result = duplicator_with_client.analyze_duplicate_sources()

        assert result["time_window"] == "1 HOUR"
        assert result["total_duplicate_groups"] == 1
        assert result["duplicates_by_service"]["api-server"] == 5
        assert result["total_duplicate_events"] == 5
        assert len(result["worst_offenders"]) == 1

    def test_analyze_multiple_services(self, duplicator_with_client):
        """测试多个服务的分析"""
        duplicator_with_client.storage.execute_query = Mock(return_value=[
            (
                "api-server",
                datetime(2026, 2, 9, 12, 34, 56),
                "abc123",
                3,
                ["evt-1", "evt-2"]
            ),
            (
                "database",
                datetime(2026, 2, 9, 12, 34, 56),
                "def456",
                2,
                ["evt-3", "evt-4"]
            )
        ])

        result = duplicator_with_client.analyze_duplicate_sources()

        assert result["total_duplicate_groups"] == 2
        assert result["duplicates_by_service"]["api-server"] == 3
        assert result["duplicates_by_service"]["database"] == 2

    def test_analyze_with_limit(self, duplicator_with_client):
        """测试分析限制"""
        # 创建 15 个有效行
        mock_rows = [
            (
                f"service-{i}",
                datetime(2026, 2, 9, 12, 34, 56),
                f"hash{i}",
                i + 1,
                [f"evt-{i}-1", f"evt-{i}-2"]
            )
            for i in range(15)
        ]
        duplicator_with_client.storage.execute_query = Mock(return_value=mock_rows)

        result = duplicator_with_client.analyze_duplicate_sources(limit=10)

        # 应该只返回 10 个 worst offenders（最多前10个）
        assert len(result["worst_offenders"]) == 10

    def test_analyze_exception(self, duplicator_with_client):
        """测试异常处理"""
        duplicator_with_client.storage.execute_query = Mock(side_effect=Exception("Query failed"))

        result = duplicator_with_client.analyze_duplicate_sources()

        assert "error" in result


class TestGetDeduplicator:
    """测试 get_deduplicator 函数"""

    def test_get_deduplicator_creates_new_instance(self, mock_storage):
        """测试创建新实例"""
        from storage.deduplication import _deduplicator

        # 重置全局变量
        dedup_module._deduplicator = None

        result = get_deduplicator(mock_storage)

        assert result is not None
        assert isinstance(result, DataDeduplicator)
        assert result.storage == mock_storage

    def test_get_deduplicator_returns_cached(self, mock_storage):
        """测试返回缓存的实例"""
        # 重置全局变量
        dedup_module._deduplicator = None

        duplicator1 = get_deduplicator(mock_storage)
        duplicator2 = get_deduplicator(mock_storage)

        assert duplicator1 is duplicator2  # 应该是同一个实例


class TestSaveEventWithDeduplication:
    """测试 save_event_with_deduplication 集成函数"""

    def test_save_non_duplicate_event(self, mock_storage):
        """测试保存不重复的事件"""
        event = {
            "id": "evt-123",
            "entity": {"name": "api-server"},
            "timestamp": "2026-02-09T12:34:56.250Z",
            "event": {"raw": "Test message"}
        }

        mock_storage.save_event = Mock(return_value=True)

        result = save_event_with_deduplication(mock_storage, event)

        assert result is True
        mock_storage.save_event.assert_called_once_with(event)

    def test_save_duplicate_event(self, mock_storage):
        """测试保存重复的事件"""
        event = {
            "id": "evt-123",
            "entity": {"name": "api-server"},
            "timestamp": "2026-02-09T12:34:56.250Z",
            "event": {"raw": "Test message"}
        }

        mock_storage.save_event = Mock(return_value=True)

        with patch('storage.deduplication.get_deduplicator') as mock_get_dedup:
            mock_dedup = Mock()
            mock_dedup.is_duplicate_event = Mock(return_value=(True, "duplicate_test"))
            mock_get_dedup.return_value = mock_dedup

            result = save_event_with_deduplication(mock_storage, event)

            assert result is True  # 返回 True 表示"处理成功"
            # 但实际上不应该保存（因为是重复的）
            # 由于我们 mock 返回 True，save_event 不会被调用

    def test_save_with_deduplicator_error(self, mock_storage):
        """测试去重器错误时会抛出异常"""
        event = {
            "id": "evt-123",
            "entity": {"name": "api-server"},
            "timestamp": "2026-02-09T12:34:56.250Z",
            "event": {"raw": "Test message"}
        }

        mock_storage.save_event = Mock(return_value=True)

        with patch('storage.deduplication.get_deduplicator') as mock_get_dedup:
            mock_dedup_instance = Mock()
            mock_dedup_instance.is_duplicate_event = Mock(side_effect=Exception("Dedup error"))
            mock_get_dedup.return_value = mock_dedup_instance

            # 去重器抛出异常时，save_event_with_deduplication 也会抛出异常
            with pytest.raises(Exception, match="Dedup error"):
                save_event_with_deduplication(mock_storage, event)


class TestEdgeCases:
    """测试边界情况"""

    def test_event_with_nested_entity(self, duplicator):
        """测试嵌套 entity 的事件"""
        event = {
            "id": "evt-123",
            "entity": {
                "name": "api-server",
                "type": "service",
                "namespace": "default"
            },
            "timestamp": "2026-02-09T12:34:56.250Z",
            "event": {"raw": "Test message"}
        }

        is_dup, reason = duplicator.is_duplicate_event(event, check_existing=False)
        assert is_dup is False

    def test_event_with_complex_timestamp(self, duplicator):
        """测试复杂时间戳格式"""
        event = {
            "id": "evt-123",
            "entity": {"name": "api-server"},
            "timestamp": "2026-02-09T12:34:56.123456789Z",  # 更多纳秒
            "event": {"raw": "Test message"}
        }

        key = duplicator._generate_semantic_key(event)
        assert key is not None
        # 应该只保留秒级精度
        assert "12:34:56" in key
        assert "123456789" not in key

    def test_empty_message(self, duplicator):
        """测试空消息"""
        event = {
            "id": "evt-123",
            "entity": {"name": "api-server"},
            "timestamp": "2026-02-09T12:34:56.250Z",
            "event": {"raw": ""}
        }

        # 空字符串会被 all() 检查判定为 False，返回 None
        key = duplicator._generate_semantic_key(event)
        assert key is None

    def test_unicode_message(self, duplicator):
        """测试 Unicode 消息"""
        event = {
            "id": "evt-123",
            "entity": {"name": "api-server"},
            "timestamp": "2026-02-09T12:34:56.250Z",
            "event": {"raw": "测试消息 🚀"}
        }

        key = duplicator._generate_semantic_key(event)
        assert key is not None
        # 应该能处理 Unicode
        assert "api-server" in key

    def test_very_long_message(self, duplicator):
        """测试非常长的消息"""
        long_message = "A" * 10000
        event = {
            "id": "evt-123",
            "entity": {"name": "api-server"},
            "timestamp": "2026-02-09T12:34:56.250Z",
            "event": {"raw": long_message}
        }

        key = duplicator._generate_semantic_key(event)
        assert key is not None
        # MD5 哈希应该是固定长度
        hash_part = key.split("|")[-1]
        assert len(hash_part) == 16  # MD5 的前 16 个字符

    def test_special_characters_in_message(self, duplicator):
        """测试消息中包含特殊字符"""
        event = {
            "id": "evt-123",
            "entity": {"name": "api-server"},
            "timestamp": "2026-02-09T12:34:56.250Z",
            "event": {"raw": "Test\n\t\rmessage<>\"'&"}
        }

        key = duplicator._generate_semantic_key(event)
        assert key is not None

    def test_concurrent_events_same_semantic_key(self, duplicator):
        """测试相同语义键的并发事件"""
        event1 = {
            "id": "evt-1",
            "entity": {"name": "api-server"},
            "timestamp": "2026-02-09T12:34:56.250Z",
            "event": {"raw": "Same message"}
        }
        event2 = {
            "id": "evt-2",
            "entity": {"name": "api-server"},
            "timestamp": "2026-02-09T12:34:56.250Z",
            "event": {"raw": "Same message"}
        }

        key1 = duplicator._generate_semantic_key(event1)
        key2 = duplicator._generate_semantic_key(event2)

        assert key1 == key2  # 应该生成相同的语义键


class TestIntegrationScenarios:
    """测试集成场景"""

    def test_full_deduplication_workflow(self, duplicator):
        """测试完整的去重工作流"""
        events = [
            {
                "id": "evt-1",
                "entity": {"name": "service-a"},
                "timestamp": "2026-02-09T12:00:00.000Z",
                "event": {"raw": "Message 1"}
            },
            {
                "id": "evt-2",
                "entity": {"name": "service-a"},
                "timestamp": "2026-02-09T12:00:00.100Z",  # 不同毫秒
                "event": {"raw": "Message 2"}
            },
            {
                "id": "evt-1",  # 重复 ID
                "entity": {"name": "service-a"},
                "timestamp": "2026-02-09T12:00:00.000Z",
                "event": {"raw": "Message 1"}
            },
            {
                "id": "evt-3",
                "entity": {"name": "service-b"},
                "timestamp": "2026-02-09T12:00:00.000Z",
                "event": {"raw": "Message 3"}
            }
        ]

        # 检查每个事件
        results = [duplicator.is_duplicate_event(e, check_existing=False) for e in events]

        # evt-1: 第一次不重复
        assert results[0][0] is False

        # evt-2: 不重复
        assert results[1][0] is False

        # evt-1 第二次: 由于使用 check_existing=False 且未添加到缓存，不会被检测为重复
        # 实际应用中，第一次处理后会添加到缓存，或者使用 check_existing=True
        # 这里我们手动添加到缓存来模拟真实场景
        duplicator._event_id_cache.add("evt-1")
        results[2] = duplicator.is_duplicate_event(events[2], check_existing=False)

        assert results[2][0] is True
        assert "duplicate_id" in results[2][1]

        # evt-3: 不重复
        assert results[3][0] is False

        # 验证统计
        stats = duplicator.get_stats()
        assert stats["total_processed"] == 5  # 4次初始检查 + 1次重新检查
        assert stats["duplicates_found"] == 1
        assert stats["duplicates_by_id"] == 1

    def test_cache_cleanup_after_ttl(self, duplicator):
        """测试缓存 TTL 后的清理"""
        # 添加旧缓存
        duplicator._cache_timestamp = datetime.now(timezone.utc) - timedelta(minutes=10)

        # 清空缓存
        duplicator.clear_cache()

        # 验证缓存已清空
        assert len(duplicator._event_id_cache) == 0
        assert len(duplicator._semantic_key_cache) == 0
        # 验证时间戳已更新
        assert (datetime.now(timezone.utc) - duplicator._cache_timestamp).total_seconds() < 1
