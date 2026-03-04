"""
Redis Stream适配器 - 批量消费版本

实现MessageQueue接口，使用同步Redis客户端包装在异步接口中
这是最稳定可靠的方式，避免异步API的兼容性问题
包含详细的调试日志用于追踪消息处理

⚠️ P0优化:
- 批量消息消费（REDIS_BATCH_SIZE）
- 死信队列支持（DLQ_ENABLED）
- 连接健康检查
- 自动重连机制
- 处理失败重试
"""
import asyncio
import json
import logging
import os
import threading
import time
from datetime import datetime
from typing import Callable, Optional, Any, Dict
from concurrent.futures import ThreadPoolExecutor

try:
    import redis
    REDIS_AVAILABLE = True
except ImportError:
    REDIS_AVAILABLE = False

from .interface import MessageQueue, Message

logger = logging.getLogger(__name__)

# 重试配置
MAX_RETRY_ATTEMPTS = 3
RETRY_DELAY_SECONDS = 2

# ⭐ 批量处理配置（从环境变量读取）
REDIS_BATCH_SIZE = int(os.getenv("REDIS_BATCH_SIZE", "100"))
REDIS_BLOCK_MS = int(os.getenv("REDIS_BLOCK_MS", "100"))
DLQ_ENABLED = os.getenv("DLQ_ENABLED", "true").lower() == "true"
DLQ_MAX_RETRIES = int(os.getenv("DLQ_MAX_RETRIES", "3"))
REDIS_PENDING_RECOVERY_ENABLED = os.getenv("REDIS_PENDING_RECOVERY_ENABLED", "true").lower() == "true"
REDIS_PENDING_IDLE_MS = max(1000, int(os.getenv("REDIS_PENDING_IDLE_MS", "60000")))
REDIS_PENDING_BATCH_SIZE = max(1, int(os.getenv("REDIS_PENDING_BATCH_SIZE", "100")))
REDIS_PENDING_RECOVERY_INTERVAL_SEC = max(5, int(os.getenv("REDIS_PENDING_RECOVERY_INTERVAL_SEC", "30")))


class RedisStreamAdapter(MessageQueue):
    """Redis Stream队列实现（使用同步客户端包装）"""

    def __init__(
        self,
        host: str = "redis",
        port: int = 6379,
        db: int = 0,
        password: str = None,
        max_len: int = 10000
    ):
        """
        初始化Redis客户端

        Args:
            host: Redis主机地址
            port: Redis端口
            db: Redis数据库编号
            password: Redis密码（可选）
            max_len: Stream最大长度（近似值）
        """
        if not REDIS_AVAILABLE:
            raise ImportError("redis not installed. Run: pip install redis")

        self.host = host
        self.port = port
        self.db = db
        self.password = password
        self.max_len = max_len
        self._redis_client = None
        self._executor = ThreadPoolExecutor(max_workers=2, thread_name_prefix="redis_")
        self._connected = False
        self._consumer_tasks = []
        self._last_health_check = 0
        self._health_check_interval = 30  # 健康检查间隔（秒）
        self._reconnect_attempts = 0
        self._max_reconnect_attempts = 5
        self._pending_recovery_enabled = REDIS_PENDING_RECOVERY_ENABLED
        self._pending_recovery_idle_ms = REDIS_PENDING_IDLE_MS
        self._pending_recovery_batch_size = REDIS_PENDING_BATCH_SIZE
        self._pending_recovery_interval_sec = REDIS_PENDING_RECOVERY_INTERVAL_SEC
        self._pending_recovery_supported = True

    def _create_client(self) -> Any:
        """创建同步Redis客户端"""
        return redis.Redis(
            host=self.host,
            port=self.port,
            db=self.db,
            password=self.password,
            decode_responses=False,  # 返回字节数据
            socket_timeout=10,
            socket_connect_timeout=10,
            retry_on_timeout=True,
            health_check_interval=30
        )

    async def health_check(self) -> Dict[str, Any]:
        """
        健康检查

        Returns:
            Dict: 健康状态
        """
        current_time = time.time()

        # 距离上次检查不足间隔时间，跳过
        if current_time - self._last_health_check < self._health_check_interval:
            return {
                "status": "ok",
                "connected": self._connected,
                "skipped": True,
                "reason": "Health check interval not reached"
            }

        self._last_health_check = current_time

        if not self._connected or not self._redis_client:
            return {
                "status": "error",
                "connected": False,
                "message": "Not connected to Redis"
            }

        try:
            loop = asyncio.get_event_loop()
            result = await loop.run_in_executor(
                self._executor,
                lambda: self._redis_client.ping()
            )

            return {
                "status": "ok",
                "connected": True,
                "ping": result,
                "host": self.host,
                "port": self.port
            }
        except Exception as e:
            logger.error(f"Health check failed: {e}")
            return {
                "status": "error",
                "connected": False,
                "error": str(e)
            }

    async def ensure_connection(self) -> bool:
        """
        确保连接可用，如果断开则尝试重连

        Returns:
            bool: 是否连接成功
        """
        if self._connected and self._redis_client:
            # 快速健康检查
            try:
                loop = asyncio.get_event_loop()
                await loop.run_in_executor(
                    self._executor,
                    self._redis_client.ping
                )
                return True
            except Exception as e:
                logger.warning(f"Connection health check failed: {e}")
                self._connected = False

        # 尝试重连
        return await self._reconnect()

    async def _reconnect(self) -> bool:
        """
        重连到 Redis（带重试机制）

        Returns:
            bool: 是否重连成功
        """
        for attempt in range(self._max_reconnect_attempts):
            try:
                logger.info(f"Reconnection attempt {attempt + 1}/{self._max_reconnect_attempts}")
                success = await self.connect()
                if success:
                    self._reconnect_attempts = 0
                    return True
            except Exception as e:
                logger.warning(f"Reconnection attempt {attempt + 1} failed: {e}")

            if attempt < self._max_reconnect_attempts - 1:
                await asyncio.sleep(RETRY_DELAY_SECONDS * (attempt + 1))

        self._reconnect_attempts += 1
        logger.error("Max reconnection attempts reached")
        return False

    async def connect(self) -> bool:
        """
        连接到Redis服务器

        Returns:
            bool: 是否连接成功
        """
        if not REDIS_AVAILABLE:
            logger.error("Redis client not available")
            return False

        try:
            loop = asyncio.get_event_loop()
            self._redis_client = await loop.run_in_executor(
                self._executor,
                self._create_client
            )

            # 测试连接
            await loop.run_in_executor(
                self._executor,
                self._redis_client.ping
            )

            self._connected = True
            logger.info(f"Connected to Redis: {self.host}:{self.port}")
            return True

        except Exception as e:
            logger.error(f"Failed to connect to Redis: {e}")
            return False

    async def publish(
        self,
        subject: str,
        message: bytes,
        headers: dict = None
    ) -> bool:
        """
        发布消息到Stream

        Args:
            subject: Stream名称（主题）
            message: 消息数据
            headers: 可选的消息头

        Returns:
            bool: 是否发布成功
        """
        if not self.is_connected():
            logger.warning("Redis not connected, attempting to reconnect...")
            await self.connect()

        try:
            # 准备消息数据
            data = {b"data": message}
            if headers:
                data.update({
                    f"header_{k}".encode(): v.encode() if isinstance(v, str) else v
                    for k, v in headers.items()
                })

            # 在线程池中执行XADD
            loop = asyncio.get_event_loop()
            message_id = await loop.run_in_executor(
                self._executor,
                lambda: self._redis_client.xadd(
                    subject if isinstance(subject, str) else subject.decode('utf-8'),
                    {k.decode('utf-8') if isinstance(k, bytes) else k: v.decode('utf-8') if isinstance(v, bytes) else v for k, v in data.items()},
                    maxlen=self.max_len,
                    approximate=True
                )
            )

            logger.info(f"Published to {subject} ({len(message)} bytes, ID: {message_id})")
            return True

        except Exception as e:
            logger.error(f"Failed to publish message: {e}")
            return False

    async def subscribe(
        self,
        subject: str,
        callback: Callable[[Message], Any],
        queue_group: str = None
    ) -> None:
        """
        订阅Stream（消费组模式）

        Args:
            subject: Stream名称
            callback: 消息处理回调
            queue_group: 消费者组名称
        """
        if not self.is_connected():
            await self.connect()

        # 使用消费组名称
        group_name = queue_group or "default-group"
        consumer_name = self._resolve_consumer_name(group_name)

        try:
            # 创建消费组（如果已存在会失败）
            loop = asyncio.get_event_loop()
            try:
                await loop.run_in_executor(
                    self._executor,
                    lambda: self._redis_client.xgroup_create(
                        subject if isinstance(subject, str) else subject.decode('utf-8'),
                        group_name if isinstance(group_name, str) else group_name.decode('utf-8'),
                        id='0',
                        mkstream=True
                    )
                )
                logger.info(f"Created consumer group: {group_name}")
            except Exception as e:
                error_msg = str(e)
                if "BUSYGROUP" in error_msg or "already exists" in error_msg:
                    logger.debug(f"Consumer group already exists: {group_name}")
                else:
                    raise

            # 创建消费任务
            task = asyncio.create_task(
                self._consume_messages(subject, group_name, consumer_name, callback)
            )
            self._consumer_tasks.append(task)

            logger.info(f"Subscribed to {subject} (group: {group_name})")

        except Exception as e:
            logger.error(f"Failed to subscribe: {e}")
            raise

    async def _ensure_consumer_group(
        self,
        subject: str,
        group_name: str
    ) -> None:
        """
        确保消费者组存在，不存在则创建

        Args:
            subject: Stream名称
            group_name: 消费者组名称
        """
        loop = asyncio.get_event_loop()
        try:
            await loop.run_in_executor(
                self._executor,
                lambda: self._redis_client.xgroup_create(
                    subject,
                    group_name,
                    id='0',
                    mkstream=True
                )
            )
            logger.info(f"Recreated consumer group: {group_name}")
        except Exception as e:
            error_msg = str(e)
            if "BUSYGROUP" in error_msg or "already exists" in error_msg:
                logger.debug(f"Consumer group already exists: {group_name}")
            else:
                raise

    @staticmethod
    def _sanitize_consumer_token(raw: str) -> str:
        """清理 consumer token，避免 Redis consumer 名称中出现非法字符。"""
        normalized = str(raw or "").strip().lower()
        if not normalized:
            return ""
        sanitized = "".join(ch if ch.isalnum() or ch in ("-", "_", ".") else "-" for ch in normalized)
        return sanitized.strip("-_.")

    def _resolve_consumer_name(self, group_name: str) -> str:
        """
        构建稳定 consumer 名称。

        默认优先级：
        1) REDIS_CONSUMER_NAME（显式指定）
        2) HOSTNAME（K8s Pod 名）
        3) 回退 consumer-<id(self)>
        """
        preferred = os.getenv("REDIS_CONSUMER_NAME") or os.getenv("HOSTNAME")
        preferred_token = self._sanitize_consumer_token(preferred)
        group_token = self._sanitize_consumer_token(group_name) or "group"
        if preferred_token:
            return f"{preferred_token}-{group_token}"
        return f"consumer-{id(self)}-{group_token}"

    def _build_message_from_entry(
        self,
        stream_name: Any,
        message_id: Any,
        data: Any
    ) -> Optional[tuple]:
        """从 Redis Stream entry 构建 Message 对象。"""
        if not isinstance(data, dict):
            logger.warning("Unexpected stream entry data type=%s", type(data))
            return None

        message_id_str = message_id.decode("utf-8") if isinstance(message_id, bytes) else str(message_id)

        message_data = data.get(b"data", data.get("data", b""))
        if message_data is None:
            message_data = b""
        message_bytes = (
            message_data
            if isinstance(message_data, bytes)
            else str(message_data).encode("utf-8")
        )

        headers = {}
        for key, value in data.items():
            key_str = key.decode("utf-8") if isinstance(key, bytes) else str(key)
            if key_str.startswith("header_"):
                header_key = key_str.replace("header_", "", 1)
                header_value = value.decode("utf-8") if isinstance(value, bytes) else str(value)
                headers[header_key] = header_value

        for meta_key in ("data_type", "ingest_time"):
            raw_value = data.get(meta_key, data.get(meta_key.encode("utf-8")))
            if raw_value is not None:
                headers[meta_key] = raw_value.decode("utf-8") if isinstance(raw_value, bytes) else str(raw_value)

        subject = stream_name.decode("utf-8") if isinstance(stream_name, bytes) else str(stream_name)
        message = Message(subject=subject, data=message_bytes, headers=headers if headers else None)
        return message_id_str, message_bytes, message

    async def _recover_pending_messages(
        self,
        subject: str,
        group_name: str,
        consumer_name: str,
        callback: Callable[[Message], Any]
    ) -> None:
        """回收长期 pending 消息，避免旧 consumer 遗留堆积。"""
        if not self._pending_recovery_enabled or not self._pending_recovery_supported:
            return

        loop = asyncio.get_event_loop()
        start_id = "0-0"
        recovered_count = 0
        ack_count = 0
        dlq_count = 0

        while self._connected:
            try:
                result = await loop.run_in_executor(
                    self._executor,
                    lambda s=subject, g=group_name, c=consumer_name, sid=start_id: self._redis_client.xautoclaim(
                        s,
                        g,
                        c,
                        min_idle_time=self._pending_recovery_idle_ms,
                        start_id=sid,
                        count=self._pending_recovery_batch_size
                    )
                )
            except Exception as e:
                err = str(e).lower()
                if "unknown command" in err or "wrong number of arguments" in err:
                    self._pending_recovery_supported = False
                    logger.warning("Pending recovery disabled: xautoclaim unsupported (%s)", e)
                else:
                    logger.warning("Pending recovery failed on %s/%s: %s", subject, group_name, e)
                return

            if not result or len(result) < 2:
                return

            next_start_id = result[0]
            entries = result[1] or []
            if not entries:
                return

            for message_id, data in entries:
                recovered_count += 1
                parsed = self._build_message_from_entry(subject, message_id, data)
                if not parsed:
                    # 非法消息体不应长期卡在 pending，直接 ACK 并记录。
                    try:
                        await loop.run_in_executor(
                            self._executor,
                            lambda mid=message_id: self._redis_client.xack(subject, group_name, mid)
                        )
                        ack_count += 1
                    except Exception as ack_err:
                        logger.error("Failed to ACK malformed pending message %s: %s", message_id, ack_err)
                    continue

                message_id_str, message_bytes, msg = parsed
                success, retry_count = await self._process_with_retry(callback, msg, message_id_str)
                if success:
                    try:
                        await loop.run_in_executor(
                            self._executor,
                            lambda mid=message_id: self._redis_client.xack(subject, group_name, mid)
                        )
                        ack_count += 1
                    except Exception as ack_err:
                        logger.error("Failed to ACK recovered message %s: %s", message_id_str, ack_err)
                    continue

                if DLQ_ENABLED:
                    await self._move_to_dlq(
                        subject,
                        message_id_str,
                        message_bytes,
                        "Recovered pending message max retries exceeded",
                        retry_count
                    )
                    try:
                        await loop.run_in_executor(
                            self._executor,
                            lambda mid=message_id: self._redis_client.xack(subject, group_name, mid)
                        )
                        ack_count += 1
                        dlq_count += 1
                    except Exception as ack_err:
                        logger.error("Failed to ACK recovered DLQ message %s: %s", message_id_str, ack_err)

            if str(next_start_id) == "0-0":
                break
            start_id = next_start_id

        if recovered_count > 0:
            logger.info(
                "Recovered pending messages stream=%s group=%s recovered=%s acked=%s dlq=%s",
                subject,
                group_name,
                recovered_count,
                ack_count,
                dlq_count,
            )

    async def _consume_messages(
        self,
        subject: str,
        group_name: str,
        consumer_name: str,
        callback: Callable[[Message], Any]
    ) -> None:
        """
        持续消费消息

        Args:
            subject: Stream名称
            group_name: 消费者组名称
            consumer_name: 消费者名称
            callback: 消息处理回调
        """
        logger.info(f"Starting consumer: {consumer_name}")

        loop = asyncio.get_event_loop()
        iteration_count = 0
        subject_str = subject if isinstance(subject, str) else subject.decode('utf-8')
        group_str = group_name if isinstance(group_name, str) else group_name.decode('utf-8')
        consumer_str = consumer_name if isinstance(consumer_name, str) else consumer_name.decode('utf-8')
        next_recovery_at = 0.0

        while self._connected:
            try:
                iteration_count += 1
                if iteration_count % 10 == 0:
                    logger.info(f"Consumer iteration #{iteration_count}, connected={self._connected}")

                # 周期回收长期 pending，避免旧 consumer 积压。
                now = loop.time()
                if now >= next_recovery_at:
                    await self._recover_pending_messages(subject_str, group_str, consumer_str, callback)
                    next_recovery_at = now + self._pending_recovery_interval_sec

                # ⭐ 使用环境变量配置的批量大小和阻塞时间
                batch_size = REDIS_BATCH_SIZE
                block_ms = REDIS_BLOCK_MS

                results = await loop.run_in_executor(
                    self._executor,
                    lambda s=subject_str, g=group_str, c=consumer_str, bs=batch_size, bm=block_ms: self._redis_client.xreadgroup(
                        g, c, {s: '>'}, count=bs, block=bm
                    )
                )

                if results:
                    logger.info(f"Received {len(results)} stream(s)")

                    # results是[[stream_name, [message1, message2, ...]]]
                    for stream_name, messages in results:
                        logger.info(f"Processing stream: {stream_name}, {len(messages)} messages")

                        # ⭐ P0优化：批量ACK - 收集所有成功处理的消息ID
                        ack_ids = []
                        dlq_items = []

                        # messages是[[msg_id, data], ...] 或 ((msg_id, data), ...)
                        for msg_item in messages:
                            if isinstance(msg_item, (list, tuple)) and len(msg_item) == 2:
                                message_id, data = msg_item[0], msg_item[1]

                                # 转换bytes到string便于处理
                                if isinstance(message_id, bytes):
                                    message_id_str = message_id.decode('utf-8')
                                else:
                                    message_id_str = str(message_id)

                                logger.info(f"Processing message {message_id_str}")

                                try:
                                    parsed = self._build_message_from_entry(stream_name, message_id, data)
                                    if not parsed:
                                        logger.warning("Failed to parse stream entry message_id=%s", message_id_str)
                                        continue

                                    _parsed_id, message_bytes, msg = parsed
                                    logger.debug(
                                        "Message data length: %s, preview: %s",
                                        len(message_bytes),
                                        message_bytes[:100],
                                    )

                                    logger.info(f"Calling callback for message {message_id_str}")

                                    # ⚠️ 带重试的消息处理
                                    success, retry_count = await self._process_with_retry(callback, msg, message_id_str)

                                    if success:
                                        logger.info(f"Callback completed for message {message_id_str}")
                                        # ⭐ 收集成功处理的消息ID，稍后批量ACK
                                        ack_ids.append(message_id)
                                    else:
                                        # ⭐ P1优化：死信队列支持
                                        if DLQ_ENABLED:
                                            dlq_items.append({
                                                'message_id': message_id,
                                                'message_id_str': message_id_str,
                                                'message_bytes': message_bytes,
                                                'retry_count': retry_count
                                            })
                                        else:
                                            logger.error(f"Failed to process message {message_id_str} after {retry_count} retries")

                                except Exception as e:
                                    logger.error(f"Error processing message {message_id_str}: {e}")
                                    import traceback
                                    traceback.print_exc()
                            else:
                                logger.warning(f"Unexpected message format: {type(msg_item)}")

                        # ⭐ P0优化：批量ACK所有成功处理的消息
                        if ack_ids:
                            try:
                                await loop.run_in_executor(
                                    self._executor,
                                    lambda aids=ack_ids: self._redis_client.xack(
                                        subject_str, group_str, *aids
                                    )
                                )
                                logger.info(f"Batch ACK sent for {len(ack_ids)} messages")
                            except Exception as e:
                                logger.error(f"Failed to send batch ACK: {e}")
                                # 批量ACK失败，尝试逐个ACK
                                for mid in ack_ids:
                                    try:
                                        await loop.run_in_executor(
                                            self._executor,
                                            lambda m=mid: self._redis_client.xack(
                                                subject_str, group_str, m
                                            )
                                        )
                                    except Exception as ack_err:
                                        logger.error(f"Failed to ACK message {mid}: {ack_err}")

                        # ⭐ P1优化：批量处理死信队列
                        if dlq_items:
                            for item in dlq_items:
                                await self._move_to_dlq(
                                    subject_str, 
                                    item['message_id_str'], 
                                    item['message_bytes'], 
                                    "Max retries exceeded", 
                                    item['retry_count']
                                )
                            # 批量ACK已移入DLQ的消息
                            dlq_ids = [item['message_id'] for item in dlq_items]
                            try:
                                await loop.run_in_executor(
                                    self._executor,
                                    lambda dids=dlq_ids: self._redis_client.xack(
                                        subject_str, group_str, *dids
                                    )
                                )
                                logger.warning(f"Batch ACK for {len(dlq_ids)} DLQ messages")
                            except Exception as e:
                                logger.error(f"Failed to batch ACK DLQ messages: {e}")

            except asyncio.CancelledError:
                logger.info(f"Consumer {consumer_name} cancelled")
                break
            except Exception as e:
                error_msg = str(e)
                if "NOGROUP" in error_msg:
                    logger.warning(f"NOGROUP error detected, recreating consumer group: {group_name}")
                    try:
                        await self._ensure_consumer_group(subject_str, group_str)
                        logger.info(f"Successfully recreated consumer group, resuming consumption")
                    except Exception as recreate_err:
                        logger.error(f"Failed to recreate consumer group: {recreate_err}")
                        await asyncio.sleep(2)
                else:
                    logger.error(f"Error in consume loop: {e}")
                    import traceback
                    traceback.print_exc()
                    await asyncio.sleep(1)

    async def _process_with_retry(
        self,
        callback: Callable[[Message], Any],
        message: Message,
        message_id: str
    ) -> tuple:
        """
        带重试机制的消息处理

        Args:
            callback: 消息处理回调
            message: 消息对象
            message_id: 消息ID

        Returns:
            tuple: (是否处理成功, 重试次数)
        """
        for attempt in range(MAX_RETRY_ATTEMPTS):
            try:
                if asyncio.iscoroutinefunction(callback):
                    callback_result = await callback(message)
                else:
                    callback_result = callback(message)

                # 回调显式返回 False 视为处理失败，触发重试与 DLQ
                if callback_result is False:
                    raise RuntimeError("callback returned False")

                return (True, attempt + 1)
            except Exception as e:
                logger.warning(
                    f"Message processing attempt {attempt + 1}/{MAX_RETRY_ATTEMPTS} "
                    f"failed for {message_id}: {e}"
                )
                if attempt < MAX_RETRY_ATTEMPTS - 1:
                    await asyncio.sleep(RETRY_DELAY_SECONDS * (attempt + 1))

        return (False, MAX_RETRY_ATTEMPTS)

    async def _move_to_dlq(
        self,
        stream: str,
        message_id: str,
        message_data: bytes,
        error: str,
        retry_count: int
    ) -> bool:
        """
        将失败消息移入死信队列

        Args:
            stream: 原始流名称
            message_id: 原始消息ID
            message_data: 消息数据
            error: 错误信息
            retry_count: 重试次数

        Returns:
            bool: 是否成功
        """
        dlq_stream = f"{stream}:dlq"
        
        try:
            loop = asyncio.get_event_loop()
            await loop.run_in_executor(
                self._executor,
                lambda: self._redis_client.xadd(
                    dlq_stream,
                    {
                        "data": message_data,
                        "original_id": message_id,
                        "original_stream": stream,
                        "error": error,
                        "failed_at": datetime.utcnow().isoformat(),
                        "retry_count": retry_count
                    }
                )
            )
            logger.warning(f"Message {message_id} moved to DLQ: {dlq_stream}")
            return True
        except Exception as e:
            logger.error(f"Failed to move message to DLQ: {e}")
            return False

    async def close(self) -> None:
        """关闭连接"""
        # 取消所有消费者任务
        for task in self._consumer_tasks:
            if not task.done():
                task.cancel()
                try:
                    await task
                except asyncio.CancelledError:
                    pass

        self._consumer_tasks.clear()

        # 关闭Redis连接
        if self._redis_client:
            try:
                loop = asyncio.get_event_loop()
                await loop.run_in_executor(
                    self._executor,
                    self._redis_client.close
                )
                self._connected = False
                logger.info("Redis connection closed")
            except Exception as e:
                logger.error(f"Error closing Redis connection: {e}")

        # 关闭线程池
        self._executor.shutdown(wait=True)

    def is_connected(self) -> bool:
        """检查连接状态"""
        return self._connected and self._redis_client is not None
