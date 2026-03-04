"""
测试storage/adapter.py模块
"""
import pytest
from unittest.mock import Mock, patch, MagicMock
from storage.adapter import StorageAdapter


class TestStorageAdapterInit:
    """测试StorageAdapter初始化"""

    def test_init_with_default_config(self):
        """使用默认配置初始化"""
        adapter = StorageAdapter()
        assert adapter.config is not None
        assert adapter.config['clickhouse']['host'] == 'localhost'
        assert adapter.config['neo4j']['host'] == 'localhost'

    def test_init_with_custom_config(self):
        """使用自定义配置初始化"""
        config = {
            "clickhouse": {
                "host": "test-host",
                "port": 9000,
                "database": "test_db"
            },
            "neo4j": {
                "host": "neo4j-test",
                "port": 7687
            }
        }
        adapter = StorageAdapter(config)
        assert adapter.config['clickhouse']['host'] == 'test-host'
        assert adapter.config['neo4j']['host'] == 'neo4j-test'


class TestSaveEvent:
    """测试事件保存"""

    @pytest.fixture
    def mock_adapter(self):
        """创建Mock adapter"""
        adapter = StorageAdapter()
        # Mock ch_client以避免真实数据库连接
        adapter.ch_client = Mock()
        adapter.ch_client.execute = Mock(return_value=None)
        adapter.ch_http_client = None
        return adapter

    def test_save_event_with_k8s_fields(self, mock_adapter, sample_event):
        """测试保存事件时正确写入K8s字段"""
        # Mock execute方法
        mock_adapter.ch_client.execute.return_value = None

        # 调用save_event
        result = mock_adapter.save_event(sample_event)

        # 验证调用成功
        assert result is True

        # 验证execute被调用
        assert mock_adapter.ch_client.execute.called

        # 获取调用的参数
        call_args = mock_adapter.ch_client.execute.call_args
        query = call_args[0][0]
        data = call_args[0][1]

        # 验证SQL INSERT语句
        assert "INSERT INTO logs" in query

        # 验证数据包含K8s字段
        assert len(data) == 1
        event_data = data[0]

        # 验证字段顺序（ClickHouse要求）
        assert event_data[0] == sample_event['id']  # id
        assert event_data[2] == sample_event['entity']['name']  # service_name
        assert event_data[3] == sample_event['context']['k8s']['pod']  # pod_name ✅
        assert event_data[4] == sample_event['context']['k8s']['namespace']  # namespace ✅
        assert event_data[5] == sample_event['context']['k8s']['node']  # node_name ✅

    def test_save_event_http_mode(self, sample_event):
        """测试HTTP模式保存事件"""
        adapter = StorageAdapter()
        # Mock HTTP客户端
        adapter.ch_http_client = {
            'url': 'http://localhost:8123',
            'database': 'logs',
            'user': 'default',
            'password': ''
        }

        with patch('storage.adapter.requests.post') as mock_post:
            mock_post.return_value = Mock(status_code=200)
            result = adapter._save_event_http(sample_event)

            assert result is True
            assert mock_post.called

            # 验证POST请求包含K8s字段
            call_args = mock_post.call_args
            import json
            body = json.loads(call_args[1]['data'])

            assert 'pod_name' in body
            assert 'namespace' in body
            assert 'node_name' in body

    def test_save_event_mock_mode(self, sample_event):
        """测试Mock模式（无数据库连接）"""
        adapter = StorageAdapter()
        adapter.ch_client = None
        adapter.ch_http_client = None

        result = adapter.save_event(sample_event)

        # Mock模式应该返回True
        assert result is True


class TestK8sFieldsMapping:
    """测试K8s字段映射"""

    def test_native_mode_k8s_field_extraction(self, sample_event):
        """测试Native模式下K8s字段正确提取"""
        adapter = StorageAdapter()
        adapter.ch_client = Mock()
        adapter.ch_client.execute = Mock(return_value=None)

        adapter.save_event(sample_event)

        # 获取调用参数
        call_args = adapter.ch_client.execute.call_args
        data = call_args[0][1][0]  # 第一行数据

        # 验证K8s字段映射
        assert data[3] == 'test-pod-123', f"Expected 'test-pod-123', got '{data[3]}'"
        assert data[4] == 'default', f"Expected 'default', got '{data[4]}'"
        assert data[5] == 'node-1', f"Expected 'node-1', got '{data[5]}'"

    def test_http_mode_k8s_field_extraction(self, sample_event):
        """测试HTTP模式下K8s字段正确提取"""
        adapter = StorageAdapter()
        adapter.ch_http_client = {
            'url': 'http://localhost:8123',
            'database': 'logs',
            'user': 'default',
            'password': ''
        }

        with patch('storage.adapter.requests.post') as mock_post:
            mock_post.return_value = Mock(status_code=200)
            adapter._save_event_http(sample_event)

            # 获取POST body
            call_args = mock_post.call_args
            import json
            body = json.loads(call_args[1]['data'])

            # 验证K8s字段
            assert body['pod_name'] == 'test-pod-123'
            assert body['namespace'] == 'default'
            assert body['node_name'] == 'node-1'

    def test_k8s_fields_with_unknown_values(self):
        """测试K8s字段为unknown时的处理"""
        adapter = StorageAdapter()
        adapter.ch_client = Mock()
        adapter.ch_client.execute = Mock(return_value=None)

        # 创建K8s字段为unknown的事件
        event = {
            "id": "test-id",
            "timestamp": "2026-02-07T00:00:00Z",
            "entity": {"type": "service", "name": "test", "instance": "unknown"},
            "event": {"type": "log", "level": "info", "raw": "test"},
            "context": {
                "k8s": {
                    "namespace": "unknown",
                    "pod": "unknown",
                    "node": "unknown"
                }
            },
            "relations": []
        }

        adapter.save_event(event)

        # 验证unknown值正确写入
        call_args = adapter.ch_client.execute.call_args
        data = call_args[0][1][0]

        assert data[3] == 'unknown'
        assert data[4] == 'unknown'
        assert data[5] == 'unknown'


class TestErrorHandling:
    """测试错误处理"""

    def test_database_connection_error(self, sample_event):
        """测试数据库连接错误处理"""
        adapter = StorageAdapter()
        adapter.ch_client = Mock()
        adapter.ch_client.execute.side_effect = Exception("Connection failed")

        result = adapter.save_event(sample_event)

        # 应该返回False并记录错误
        assert result is False

    def test_http_request_error(self, sample_event):
        """测试HTTP请求错误处理"""
        adapter = StorageAdapter()
        adapter.ch_http_client = {
            'url': 'http://localhost:8123',
            'database': 'logs',
            'user': 'default',
            'password': ''
        }

        with patch('storage.adapter.requests.post') as mock_post:
            mock_post.side_effect = Exception("Network error")
            result = adapter._save_event_http(sample_event)

            assert result is False


class TestDataIntegrity:
    """测试数据完整性"""

    def test_all_required_fields_present(self, sample_event):
        """测试所有必需字段都存在"""
        adapter = StorageAdapter()
        adapter.ch_client = Mock()
        adapter.ch_client.execute = Mock(return_value=None)

        adapter.save_event(sample_event)

        call_args = adapter.ch_client.execute.call_args
        data = call_args[0][1][0]

        # ClickHouse表字段顺序：
        # id, timestamp, service_name, pod_name, namespace, node_name,
        # level, message, trace_id, span_id, labels, host_ip

        assert len(data) == 12  # 12个字段
        assert data[0] != ''  # id
        assert data[1] is not None  # timestamp
        assert data[2] != ''  # service_name
        assert data[6] != ''  # level

    def test_timestamp_format(self, sample_event):
        """测试时间戳格式正确"""
        from datetime import datetime

        adapter = StorageAdapter()
        adapter.ch_client = Mock()
        adapter.ch_client.execute = Mock(return_value=None)

        adapter.save_event(sample_event)

        call_args = adapter.ch_client.execute.call_args
        data = call_args[0][1][0]

        # 验证timestamp是datetime对象
        assert isinstance(data[1], datetime)

    def test_message_truncation(self):
        """测试超长消息被截断"""
        adapter = StorageAdapter()
        adapter.ch_client = Mock()
        adapter.ch_client.execute = Mock(return_value=None)

        # 创建超长消息（>5000字符）
        long_message = "x" * 10000
        event = {
            "id": "test-id",
            "timestamp": "2026-02-07T00:00:00Z",
            "entity": {"type": "service", "name": "test", "instance": "test"},
            "event": {"type": "log", "level": "info", "raw": long_message},
            "context": {"k8s": {"namespace": "default", "pod": "test", "node": "node1"}},
            "relations": []
        }

        adapter.save_event(event)

        call_args = adapter.ch_client.execute.call_args
        data = call_args[0][1][0]

        # 验证消息被截断到5000字符
        assert len(data[7]) <= 5000
