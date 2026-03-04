"""
Redis Stream 批量消费配置和工具
"""
import os
from typing import Dict, Any, List, Optional, Callable
import logging
import asyncio
from datetime import datetime

logger = logging.getLogger(__name__)

MAX_RETRY_ATTEMPTS = 3
RETRY_DELAY_SECONDS = 1


class BatchConfig:
    """批量处理配置"""
    
    def __init__(self):
        self.redis_batch_size = int(os.getenv("REDIS_BATCH_SIZE", "100"))
        self.redis_block_ms = int(os.getenv("REDIS_BLOCK_MS", "100"))
        self.dlq_enabled = os.getenv("DLQ_ENABLED", "true").lower() == "true"
        self.dlq_max_retries = int(os.getenv("DLQ_MAX_RETRIES", "3"))


class Message:
    """消息封装类"""
    
    def __init__(
        self,
        subject: str,
        data: bytes,
        headers: Optional[Dict[str, str]] = None,
        message_id: Optional[str] = None,
        retry_count: int = 0
    ):
        self.subject = subject
        self.data = data
        self.headers = headers or {}
        self.message_id = message_id
        self.retry_count = retry_count


class DeadLetterQueue:
    """
    死信队列管理
    
    处理消费失败的消息
    """
    
    def __init__(self, redis_client, prefix: str = ""):
        self.redis = redis_client
        self.prefix = prefix
    
    def get_dlq_name(self, stream: str) -> str:
        """获取死信队列名称"""
        return f"{self.prefix}{stream}:dlq"
    
    async def add_to_dlq(
        self,
        stream: str,
        message_id: str,
        message_data: bytes,
        error: str,
        retry_count: int
    ) -> bool:
        """
        添加消息到死信队列

        Args:
            stream: 原始流名称
            message_id: 原始消息 ID
            message_data: 消息数据
            error: 错误信息
            retry_count: 重试次数

        Returns:
            bool: 是否成功
        """
        dlq_name = self.get_dlq_name(stream)
        
        try:
            await self.redis.xadd(
                dlq_name,
                {
                    "data": message_data,
                    "original_id": message_id,
                    "original_stream": stream,
                    "error": error,
                    "failed_at": datetime.utcnow().isoformat(),
                    "retry_count": retry_count
                }
            )
            logger.warning(f"Message {message_id} moved to DLQ: {dlq_name}")
            return True
        except Exception as e:
            logger.error(f"Failed to add message to DLQ: {e}")
            return False
    
    async def get_dlq_messages(self, stream: str, count: int = 100) -> List[Dict]:
        """获取死信队列中的消息"""
        dlq_name = self.get_dlq_name(stream)
        try:
            result = await self.redis.xrange(dlq_name, count=count)
            return result
        except Exception as e:
            logger.error(f"Failed to get DLQ messages: {e}")
            return []
    
    async def replay_dlq_message(self, stream: str, dlq_message_id: str) -> bool:
        """
        重放死信队列中的消息

        Args:
            stream: 原始流名称
            dlq_message_id: 死信队列消息 ID

        Returns:
            bool: 是否成功
        """
        dlq_name = self.get_dlq_name(stream)
        
        try:
            messages = await self.redis.xrange(dlq_name, dlq_message_id, dlq_message_id)
            if not messages:
                return False
            
            msg_id, data = messages[0]
            
            await self.redis.xadd(stream, {"data": data.get(b"data", b"")})
            await self.redis.xdel(dlq_name, msg_id)
            
            logger.info(f"Replayed DLQ message {dlq_message_id} to {stream}")
            return True
        except Exception as e:
            logger.error(f"Failed to replay DLQ message: {e}")
            return False


class BatchProcessor:
    """
    批量消息处理器
    
    处理从 Redis Stream 批量读取的消息
    """
    
    def __init__(
        self,
        redis_client,
        batch_config: Optional[BatchConfig] = None
    ):
        self.redis = redis_client
        self.config = batch_config or BatchConfig()
        self.dlq = DeadLetterQueue(redis_client)
        self._stats = {
            "total_messages": 0,
            "successful_messages": 0,
            "failed_messages": 0,
            "dlq_messages": 0
        }
    
    def get_stats(self) -> Dict[str, int]:
        """获取统计信息"""
        return self._stats.copy()
    
    async def process_batch(
        self,
        messages: List[tuple],
        callback: Callable[[Message], Any],
        stream: str,
        group: str
    ) -> Dict[str, Any]:
        """
        处理批量消息

        Args:
            messages: 消息列表 [(msg_id, data), ...]
            callback: 处理回调函数
            stream: 流名称
            group: 消费组名称

        Returns:
            Dict: 处理结果统计
        """
        results = {
            "total": len(messages),
            "success": 0,
            "failed": 0,
            "dlq": 0
        }
        
        for msg_id, data in messages:
            self._stats["total_messages"] += 1
            
            message_id_str = msg_id.decode('utf-8') if isinstance(msg_id, bytes) else str(msg_id)
            
            message_data = data.get(b'data', b'') if isinstance(data, dict) else b''
            
            msg = Message(
                subject=stream,
                data=message_data if isinstance(message_data, bytes) else str(message_data).encode(),
                message_id=message_id_str
            )
            
            success = await self._process_with_retry(callback, msg)
            
            if success:
                results["success"] += 1
                self._stats["successful_messages"] += 1
                await self.redis.xack(stream, group, msg_id)
            else:
                results["failed"] += 1
                self._stats["failed_messages"] += 1
                
                if self.config.dlq_enabled:
                    await self.dlq.add_to_dlq(
                        stream,
                        message_id_str,
                        message_data,
                        "Max retries exceeded",
                        msg.retry_count
                    )
                    results["dlq"] += 1
                    self._stats["dlq_messages"] += 1
                    await self.redis.xack(stream, group, msg_id)
        
        return results
    
    async def _process_with_retry(
        self,
        callback: Callable[[Message], Any],
        message: Message
    ) -> bool:
        """带重试的消息处理"""
        for attempt in range(MAX_RETRY_ATTEMPTS):
            try:
                if asyncio.iscoroutinefunction(callback):
                    await callback(message)
                else:
                    callback(message)
                return True
            except Exception as e:
                message.retry_count = attempt + 1
                logger.warning(
                    f"Message processing failed (attempt {attempt + 1}/{MAX_RETRY_ATTEMPTS}): {e}"
                )
                if attempt < MAX_RETRY_ATTEMPTS - 1:
                    await asyncio.sleep(RETRY_DELAY_SECONDS * (attempt + 1))
        
        return False
