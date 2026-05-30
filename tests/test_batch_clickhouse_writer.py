"""
测试 shared_src/batch/clickhouse_writer.py - BatchClickHouseWriter
"""
import os
import pytest
import sys
import threading
import time
from unittest.mock import Mock, MagicMock

sys.path.insert(0, os.path.join(os.path.dirname(__file__), '..', 'shared_src'))

from batch.clickhouse_writer import BatchClickHouseWriter, MultiTableBatchWriter


class MockClickHouseClient:
    """模拟 ClickHouse 客户端"""
    
    def __init__(self):
        self.inserted_data = []
        self.insert_count = 0
        self._lock = threading.Lock()
    
    def execute(self, query, data=None):
        with self._lock:
            if data:
                self.inserted_data.extend(data)
                self.insert_count += 1
            return []


class TestBatchClickHouseWriter:
    """测试 BatchClickHouseWriter 类"""

    def test_init_default_params(self):
        """测试默认参数"""
        client = MockClickHouseClient()
        writer = BatchClickHouseWriter(client, "test_table")
        
        assert writer.batch_size == 2000  # 默认值
        assert writer.flush_interval == 0.5  # 默认值

    def test_init_custom_params(self):
        """测试自定义参数"""
        client = MockClickHouseClient()
        writer = BatchClickHouseWriter(
            client, "test_table",
            batch_size=100,
            flush_interval=0.1
        )
        
        assert writer.batch_size == 100
        assert writer.flush_interval == 0.1

    def test_add_single_row(self):
        """测试添加单行数据"""
        client = MockClickHouseClient()
        writer = BatchClickHouseWriter(client, "test_table", batch_size=5)
        
        result = writer.add(["id1", "data1"])
        
        assert result is False  # 未达到 batch_size
        assert len(writer._buffer) == 1

    def test_add_triggers_flush(self):
        """测试添加数据触发刷新"""
        client = MockClickHouseClient()
        writer = BatchClickHouseWriter(client, "test_table", batch_size=3)
        
        writer.add(["id1", "data1"])
        writer.add(["id2", "data2"])
        result = writer.add(["id3", "data3"])  # 触发刷新
        
        assert result is True
        assert len(writer._buffer) == 0
        assert len(client.inserted_data) == 3

    def test_add_batch(self):
        """测试批量添加数据"""
        client = MockClickHouseClient()
        writer = BatchClickHouseWriter(client, "test_table", batch_size=5)
        
        rows = [["id1", "data1"], ["id2", "data2"], ["id3", "data3"]]
        result = writer.add_batch(rows)
        
        assert result is False
        assert len(writer._buffer) == 3

    def test_manual_flush(self):
        """测试手动刷新"""
        client = MockClickHouseClient()
        writer = BatchClickHouseWriter(client, "test_table", batch_size=100)
        
        writer.add(["id1", "data1"])
        writer.add(["id2", "data2"])
        
        result = writer.flush()
        
        assert result is True
        assert len(writer._buffer) == 0
        assert len(client.inserted_data) == 2

    def test_flush_chunked_by_max_insert_rows_per_query(self, monkeypatch):
        """测试 CH_MAX_INSERT_ROWS_PER_QUERY 会限制单次写入行数。"""
        monkeypatch.setenv("CH_MAX_INSERT_ROWS_PER_QUERY", "2")
        client = MockClickHouseClient()
        writer = BatchClickHouseWriter(client, "test_table", batch_size=100)

        writer.add_batch([
            ["id1", "data1"],
            ["id2", "data2"],
            ["id3", "data3"],
            ["id4", "data4"],
            ["id5", "data5"],
        ])
        result = writer.flush()

        assert result is True
        assert client.insert_count == 3
        assert len(client.inserted_data) == 5

    def test_get_stats(self):
        """测试统计信息"""
        client = MockClickHouseClient()
        writer = BatchClickHouseWriter(client, "test_table", batch_size=2)
        
        writer.add(["id1", "data1"])
        writer.add(["id2", "data2"])  # 触发刷新
        
        stats = writer.get_stats()
        
        assert stats["total_rows"] == 2
        assert stats["total_flushes"] == 1
        assert stats["total_errors"] == 0

    def test_background_flush(self):
        """测试后台定时刷新"""
        client = MockClickHouseClient()
        writer = BatchClickHouseWriter(
            client, "test_table",
            batch_size=1000,  # 大 batch_size，不会触发
            flush_interval=0.1  # 短间隔
        )
        
        writer.start()
        writer.add(["id1", "data1"])
        
        time.sleep(0.3)  # 等待后台刷新
        
        stats = writer.get_stats()
        writer.stop()
        
        assert stats["total_flushes"] >= 1
        assert len(client.inserted_data) == 1


class TestMultiTableBatchWriter:
    """测试 MultiTableBatchWriter 类"""

    def test_get_writer(self):
        """测试获取写入器"""
        client = MockClickHouseClient()
        manager = MultiTableBatchWriter(client)
        
        writer1 = manager.get_writer("table1")
        writer2 = manager.get_writer("table2")
        writer1_again = manager.get_writer("table1")
        
        assert writer1 is writer1_again  # 相同表返回同一实例
        assert writer1 is not writer2

    def test_add_to_table(self):
        """测试添加数据到指定表"""
        client = MockClickHouseClient()
        manager = MultiTableBatchWriter(client, default_batch_size=2)
        
        manager.add("table1", ["id1", "data1"])
        manager.add("table1", ["id2", "data2"])  # 触发刷新
        
        assert len(client.inserted_data) == 2

    def test_get_all_stats(self):
        """测试获取所有表的统计"""
        client = MockClickHouseClient()
        manager = MultiTableBatchWriter(client, default_batch_size=2)
        
        manager.add("table1", ["id1", "data1"])
        manager.add("table2", ["id2", "data2"])
        
        stats = manager.get_all_stats()
        
        assert "table1" in stats
        assert "table2" in stats
