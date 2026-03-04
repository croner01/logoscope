"""
测试 shared_src/batch/redis_consumer.py - BatchProcessor 和相关类
"""
import os
import pytest
import sys
import asyncio
from unittest.mock import Mock, AsyncMock, MagicMock

sys.path.insert(0, os.path.join(os.path.dirname(__file__), '..', '..', 'shared_src'))

from batch.redis_consumer import (
    BatchConfig,
    Message,
    DeadLetterQueue,
    BatchProcessor
)


class TestBatchConfig:
    """测试 BatchConfig 类"""

    def test_default_values(self):
        """测试默认值"""
        config = BatchConfig()
        
        assert config.redis_batch_size == 100
        assert config.redis_block_ms == 100
        assert config.dlq_enabled is True
        assert config.dlq_max_retries == 3

    def test_env_override(self, monkeypatch):
        """测试环境变量覆盖"""
        monkeypatch.setenv("REDIS_BATCH_SIZE", "500")
        monkeypatch.setenv("DLQ_ENABLED", "false")
        
        config = BatchConfig()
        
        assert config.redis_batch_size == 500
        assert config.dlq_enabled is False


class TestMessage:
    """测试 Message 类"""

    def test_init_default(self):
        """测试默认初始化"""
        msg = Message(subject="test.stream", data=b"test data")
        
        assert msg.subject == "test.stream"
        assert msg.data == b"test data"
        assert msg.headers == {}
        assert msg.message_id is None
        assert msg.retry_count == 0

    def test_init_with_all_params(self):
        """测试完整参数初始化"""
        msg = Message(
            subject="test.stream",
            data=b"test data",
            headers={"key": "value"},
            message_id="msg-123",
            retry_count=2
        )
        
        assert msg.headers == {"key": "value"}
        assert msg.message_id == "msg-123"
        assert msg.retry_count == 2


class TestDeadLetterQueue:
    """测试 DeadLetterQueue 类"""

    @pytest.mark.asyncio
    async def test_get_dlq_name(self):
        """测试获取 DLQ 名称"""
        redis_client = AsyncMock()
        dlq = DeadLetterQueue(redis_client)
        
        assert dlq.get_dlq_name("logs.raw") == "logs.raw:dlq"
        
        dlq_with_prefix = DeadLetterQueue(redis_client, prefix="prefix:")
        assert dlq_with_prefix.get_dlq_name("logs.raw") == "prefix:logs.raw:dlq"

    @pytest.mark.asyncio
    async def test_add_to_dlq(self):
        """测试添加消息到 DLQ"""
        redis_client = AsyncMock()
        redis_client.xadd = AsyncMock(return_value="dlq-msg-1")
        
        dlq = DeadLetterQueue(redis_client)
        result = await dlq.add_to_dlq(
            stream="logs.raw",
            message_id="msg-123",
            message_data=b"test data",
            error="Processing failed",
            retry_count=3
        )
        
        assert result is True
        redis_client.xadd.assert_called_once()

    @pytest.mark.asyncio
    async def test_get_dlq_messages(self):
        """测试获取 DLQ 消息"""
        redis_client = AsyncMock()
        redis_client.xrange = AsyncMock(return_value=[
            (b"dlq-1", {b"data": b"test", b"error": b"failed"})
        ])
        
        dlq = DeadLetterQueue(redis_client)
        messages = await dlq.get_dlq_messages("logs.raw")
        
        assert len(messages) == 1


class TestBatchProcessor:
    """测试 BatchProcessor 类"""

    @pytest.mark.asyncio
    async def test_process_batch_success(self):
        """测试批量处理成功"""
        redis_client = AsyncMock()
        redis_client.xack = AsyncMock(return_value=1)
        
        processor = BatchProcessor(redis_client)
        
        messages = [
            (b"msg-1", {b"data": b"test1"}),
            (b"msg-2", {b"data": b"test2"}),
        ]
        
        async def success_callback(msg):
            pass
        
        results = await processor.process_batch(
            messages=messages,
            callback=success_callback,
            stream="test.stream",
            group="test-group"
        )
        
        assert results["total"] == 2
        assert results["success"] == 2
        assert results["failed"] == 0

    @pytest.mark.asyncio
    async def test_process_batch_with_failure(self):
        """测试批量处理包含失败"""
        redis_client = AsyncMock()
        redis_client.xack = AsyncMock(return_value=1)
        redis_client.xadd = AsyncMock(return_value="dlq-1")
        
        config = BatchConfig()
        config.dlq_enabled = True
        
        processor = BatchProcessor(redis_client, config)
        
        messages = [
            (b"msg-1", {b"data": b"test1"}),
        ]
        
        async def fail_callback(msg):
            raise Exception("Processing error")
        
        results = await processor.process_batch(
            messages=messages,
            callback=fail_callback,
            stream="test.stream",
            group="test-group"
        )
        
        assert results["total"] == 1
        assert results["success"] == 0
        assert results["failed"] == 1
        assert results["dlq"] == 1

    def test_get_stats(self):
        """测试获取统计信息"""
        redis_client = AsyncMock()
        processor = BatchProcessor(redis_client)
        
        stats = processor.get_stats()
        
        assert "total_messages" in stats
        assert "successful_messages" in stats
        assert "failed_messages" in stats
        assert "dlq_messages" in stats
