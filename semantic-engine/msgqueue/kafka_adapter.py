"""
Kafka 适配器

实现 MessageQueue 接口，使用 Kafka 作为消息队列。
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
from typing import Any, Callable, Dict, List, Optional, Tuple

try:
    from aiokafka import AIOKafkaConsumer, AIOKafkaProducer
    from aiokafka.structs import OffsetAndMetadata, TopicPartition

    KAFKA_AVAILABLE = True
except Exception:
    KAFKA_AVAILABLE = False

from .interface import Message, MessageQueue

logger = logging.getLogger(__name__)


def _run_callback_in_isolated_loop(callback: Callable[[Message], Any], message: Message) -> Any:
    """在独立线程事件循环中执行协程回调。"""
    return asyncio.run(callback(message))


class KafkaQueue(MessageQueue):
    """Kafka 队列实现。"""

    def __init__(
        self,
        servers: str = "kafka:9092",
        group_id: str = "log-workers",
        client_id: str = "semantic-engine",
        auto_offset_reset: str = "latest",
        poll_timeout_ms: int = 1000,
        max_poll_interval_ms: int = 300000,
        session_timeout_ms: int = 45000,
        heartbeat_interval_ms: int = 3000,
        callback_offload: bool = True,
        flush_offload: bool = True,
        commit_error_as_warning: bool = True,
        max_batch_size: int = 200,
        max_retry_attempts: int = 3,
        retry_delay_seconds: int = 2,
    ) -> None:
        if not KAFKA_AVAILABLE:
            raise ImportError("aiokafka not installed. Run: pip install aiokafka")

        self.servers = str(servers or "kafka:9092")
        self.bootstrap_servers = [item.strip() for item in self.servers.split(",") if item.strip()]
        self.group_id = group_id
        self.client_id = client_id
        self.auto_offset_reset = auto_offset_reset if auto_offset_reset in {"earliest", "latest"} else "latest"
        self.poll_timeout_ms = max(100, int(poll_timeout_ms))
        self.max_poll_interval_ms = max(self.poll_timeout_ms, int(max_poll_interval_ms))
        self.session_timeout_ms = max(6000, int(session_timeout_ms))
        self.heartbeat_interval_ms = max(1000, int(heartbeat_interval_ms))
        if self.heartbeat_interval_ms >= self.session_timeout_ms:
            self.heartbeat_interval_ms = max(1000, self.session_timeout_ms // 3)
        self.callback_offload = bool(callback_offload)
        self.flush_offload = bool(flush_offload)
        self.commit_error_as_warning = bool(commit_error_as_warning)
        self.max_batch_size = max(1, int(max_batch_size))
        self.max_retry_attempts = max(1, int(max_retry_attempts))
        self.retry_delay_seconds = max(0, int(retry_delay_seconds))
        self.flush_retry_attempts = max(1, int(os.getenv("KAFKA_FLUSH_RETRY_ATTEMPTS", "2")))
        self.flush_retry_delay_seconds = max(0, float(os.getenv("KAFKA_FLUSH_RETRY_DELAY_SECONDS", "1")))

        self._dlq_enabled = str(os.getenv("DLQ_ENABLED", "true")).strip().lower() in {"1", "true", "yes", "on"}
        self._dlq_max_retries = max(1, int(os.getenv("DLQ_MAX_RETRIES", "3")))

        self._producer: Optional[AIOKafkaProducer] = None
        self._consumers: List[AIOKafkaConsumer] = []
        self._consumer_tasks: List[asyncio.Task] = []
        self._connected = False
        self._stopping = False

    async def connect(self) -> bool:
        """建立 Kafka 连接（producer）。"""
        if self._connected and self._producer:
            return True

        try:
            self._producer = AIOKafkaProducer(
                bootstrap_servers=self.bootstrap_servers,
                client_id=self.client_id,
                acks="all",
            )
            await self._producer.start()
            self._connected = True
            self._stopping = False
            logger.info("Connected to Kafka: %s", ",".join(self.bootstrap_servers))
            return True
        except Exception as exc:
            logger.error("Failed to connect Kafka producer: %s", exc)
            self._connected = False
            self._producer = None
            return False

    async def publish(self, subject: str, message: bytes, headers: Dict[str, str] = None) -> bool:
        """发布消息到 Kafka topic。"""
        if not self.is_connected():
            connected = await self.connect()
            if not connected:
                return False

        kafka_headers: List[Tuple[str, bytes]] = []
        for key, value in (headers or {}).items():
            kafka_headers.append((str(key), str(value).encode("utf-8")))

        try:
            await self._producer.send_and_wait(
                topic=subject,
                value=message,
                headers=kafka_headers or None,
            )
            return True
        except Exception as exc:
            logger.error("Failed to publish Kafka message topic=%s: %s", subject, exc)
            return False

    async def subscribe(
        self,
        subject: str,
        callback: Callable[[Message], Any],
        queue_group: str = None,
    ) -> None:
        """订阅 Kafka topic 并启动消费循环。"""
        if not self.is_connected():
            connected = await self.connect()
            if not connected:
                raise RuntimeError("kafka producer not connected")

        consumer_group = queue_group or self.group_id
        consumer = AIOKafkaConsumer(
            subject,
            bootstrap_servers=self.bootstrap_servers,
            group_id=consumer_group,
            client_id=self.client_id,
            enable_auto_commit=False,
            auto_offset_reset=self.auto_offset_reset,
            max_poll_interval_ms=self.max_poll_interval_ms,
            session_timeout_ms=self.session_timeout_ms,
            heartbeat_interval_ms=self.heartbeat_interval_ms,
        )
        await consumer.start()

        self._consumers.append(consumer)
        task = asyncio.create_task(self._consume_loop(consumer, callback))
        self._consumer_tasks.append(task)
        logger.info(
            "Subscribed to Kafka topic=%s group=%s poll_timeout_ms=%s max_batch_size=%s",
            subject,
            consumer_group,
            self.poll_timeout_ms,
            self.max_batch_size,
        )

    async def _consume_loop(self, consumer: AIOKafkaConsumer, callback: Callable[[Message], Any]) -> None:
        """消费循环，成功处理后手动提交 offset。"""
        try:
            while self._connected and not self._stopping:
                records_map = await consumer.getmany(
                    timeout_ms=self.poll_timeout_ms,
                    max_records=self.max_batch_size,
                )
                if not records_map:
                    continue

                for topic_partition, records in records_map.items():
                    await self._consume_partition_records(consumer, topic_partition, records, callback)
        except asyncio.CancelledError:
            logger.info("Kafka consumer loop cancelled")
        except Exception as exc:
            logger.exception("Kafka consumer loop failed: %s", exc)

    async def _consume_partition_records(
        self,
        consumer: AIOKafkaConsumer,
        topic_partition: TopicPartition,
        records: List[Any],
        callback: Callable[[Message], Any],
    ) -> None:
        """
        处理单个分区批次消息。

        策略：
        - 仅在分区批次层面执行一次 flush_pending_writes；
        - 仅提交连续可确认（成功或已转入 DLQ）的最后 offset；
        - 遇到不可确认消息时停止当前分区后续处理，避免越过失败消息提交 offset。
        """
        if not records:
            return

        last_committable_offset: Optional[int] = None
        successful_count = 0
        dlq_count = 0

        for record in records:
            message = self._build_message(record)
            success, retry_count = await self._process_with_retry(callback, message, record)
            if success:
                last_committable_offset = record.offset
                successful_count += 1
                continue

            moved_to_dlq = False
            if self._dlq_enabled and retry_count >= self._dlq_max_retries:
                moved_to_dlq = await self._move_to_dlq(record, message, "max retries exceeded")
                if moved_to_dlq:
                    last_committable_offset = record.offset
                    dlq_count += 1

            if not moved_to_dlq:
                logger.warning(
                    "Stop processing topic=%s partition=%s at offset=%s due to uncommittable message",
                    record.topic,
                    record.partition,
                    record.offset,
                )
                break

        if last_committable_offset is None:
            return

        if successful_count > 0:
            flush_success = await self._flush_pending_writes_with_retry(callback)
            if not flush_success:
                logger.warning(
                    "Skip commit for topic=%s partition=%s offset=%s because flush_pending_writes failed",
                    topic_partition.topic,
                    topic_partition.partition,
                    last_committable_offset,
                )
                return

        await self._commit_record(consumer, topic_partition, last_committable_offset)
        logger.debug(
            "Committed topic=%s partition=%s offset=%s batch_ok=%s batch_dlq=%s",
            topic_partition.topic,
            topic_partition.partition,
            last_committable_offset,
            successful_count,
            dlq_count,
        )

    async def _commit_record(
        self,
        consumer: AIOKafkaConsumer,
        topic_partition: TopicPartition,
        offset: int,
    ) -> None:
        """提交单条消息 offset。"""
        try:
            await consumer.commit({topic_partition: OffsetAndMetadata(offset + 1, "")})
        except Exception as exc:
            if self.commit_error_as_warning and self._is_rebalance_commit_error(exc):
                logger.warning(
                    "Kafka commit skipped due to rebalance topic=%s partition=%s offset=%s err=%s",
                    topic_partition.topic,
                    topic_partition.partition,
                    offset,
                    exc,
                )
                return
            logger.error(
                "Failed to commit Kafka offset topic=%s partition=%s offset=%s err=%s",
                topic_partition.topic,
                topic_partition.partition,
                offset,
                exc,
            )

    async def _flush_pending_writes(self, callback: Callable[[Message], Any]) -> bool:
        """在提交 offset 前刷新批量写缓冲，避免先提交后落库失败。"""
        callback_owner = getattr(callback, "__self__", None)
        flush_fn = getattr(callback_owner, "flush_pending_writes", None) if callback_owner is not None else None
        if flush_fn is None:
            return True
        try:
            if asyncio.iscoroutinefunction(flush_fn):
                result = await flush_fn()
            elif self.flush_offload:
                result = await asyncio.to_thread(flush_fn)
            else:
                result = flush_fn()
            return bool(result)
        except Exception as exc:
            logger.exception("flush_pending_writes failed before Kafka commit: %s", exc)
            return False

    async def _flush_pending_writes_with_retry(self, callback: Callable[[Message], Any]) -> bool:
        """按配置重试 flush_pending_writes，降低瞬时故障导致的提交阻塞。"""
        for attempt in range(1, self.flush_retry_attempts + 1):
            if await self._flush_pending_writes(callback):
                return True

            if attempt >= self.flush_retry_attempts:
                break

            delay_seconds = self.flush_retry_delay_seconds * attempt
            if delay_seconds > 0:
                logger.warning(
                    "flush_pending_writes failed (attempt=%s/%s), retry in %.2fs",
                    attempt,
                    self.flush_retry_attempts,
                    delay_seconds,
                )
                await asyncio.sleep(delay_seconds)

        return False

    async def _process_with_retry(
        self,
        callback: Callable[[Message], Any],
        message: Message,
        record: Any,
    ) -> Tuple[bool, int]:
        """处理消息，失败时按配置重试。"""
        for attempt in range(1, self.max_retry_attempts + 1):
            try:
                result = await self._invoke_callback(callback, message)
                if bool(result):
                    return True, attempt

                logger.warning(
                    "Kafka callback returned false topic=%s partition=%s offset=%s attempt=%s",
                    record.topic,
                    record.partition,
                    record.offset,
                    attempt,
                )
            except Exception as exc:
                logger.error(
                    "Kafka callback exception topic=%s partition=%s offset=%s attempt=%s err=%s",
                    record.topic,
                    record.partition,
                    record.offset,
                    attempt,
                    exc,
                )

            if attempt < self.max_retry_attempts and self.retry_delay_seconds > 0:
                await asyncio.sleep(self.retry_delay_seconds)

        return False, self.max_retry_attempts

    async def _invoke_callback(self, callback: Callable[[Message], Any], message: Message) -> Any:
        """执行消费回调，必要时下沉到线程池避免阻塞 Kafka 心跳协程。"""
        if asyncio.iscoroutinefunction(callback):
            if self.callback_offload:
                return await asyncio.to_thread(_run_callback_in_isolated_loop, callback, message)
            return await callback(message)

        if self.callback_offload:
            return await asyncio.to_thread(callback, message)
        return callback(message)

    def _build_message(self, record: Any) -> Message:
        """将 Kafka record 转换为统一 Message 模型。"""
        headers: Dict[str, str] = {}
        for item in list(record.headers or []):
            key = str(item[0]) if len(item) > 0 else ""
            value_raw = item[1] if len(item) > 1 else b""
            if not key:
                continue
            try:
                headers[key] = value_raw.decode("utf-8") if isinstance(value_raw, bytes) else str(value_raw)
            except Exception:
                headers[key] = str(value_raw)

        return Message(
            subject=record.topic,
            data=record.value or b"",
            headers=headers or None,
        )

    async def _move_to_dlq(self, record: Any, message: Message, reason: str) -> bool:
        """将处理失败消息写入 DLQ topic。"""
        if not self._producer:
            return False

        try:
            payload_text = message.data.decode("utf-8", errors="replace")
            dlq_payload = {
                "source_topic": record.topic,
                "source_partition": record.partition,
                "source_offset": record.offset,
                "reason": reason,
                "payload": payload_text,
                "headers": message.headers or {},
            }
            dlq_topic = f"{record.topic}.dlq"
            await self._producer.send_and_wait(
                topic=dlq_topic,
                value=json.dumps(dlq_payload, ensure_ascii=False).encode("utf-8"),
                headers=[
                    ("source_topic", str(record.topic).encode("utf-8")),
                    ("reason", str(reason).encode("utf-8")),
                ],
            )
            logger.warning(
                "Moved Kafka message to DLQ topic=%s source=%s/%s/%s",
                dlq_topic,
                record.topic,
                record.partition,
                record.offset,
            )
            return True
        except Exception as exc:
            logger.error(
                "Failed to move Kafka message to DLQ topic=%s offset=%s err=%s",
                record.topic,
                record.offset,
                exc,
            )
            return False

    async def close(self) -> None:
        """关闭消费者与 producer。"""
        self._stopping = True

        for task in list(self._consumer_tasks):
            task.cancel()
        if self._consumer_tasks:
            await asyncio.gather(*self._consumer_tasks, return_exceptions=True)
        self._consumer_tasks.clear()

        for consumer in list(self._consumers):
            try:
                await consumer.stop()
            except Exception as exc:
                logger.warning("Failed to stop Kafka consumer: %s", exc)
        self._consumers.clear()

        if self._producer:
            try:
                await self._producer.stop()
            except Exception as exc:
                logger.warning("Failed to stop Kafka producer: %s", exc)
            finally:
                self._producer = None

        self._connected = False
        logger.info("Kafka connection closed")

    def is_connected(self) -> bool:
        """检查连接状态。"""
        return self._connected and self._producer is not None

    @staticmethod
    def _is_rebalance_commit_error(error: Exception) -> bool:
        """识别由消费组重平衡引起的可恢复 commit 错误。"""
        text = str(error).lower()
        return (
            "commitfailederror" in text
            or "illegalstateerror" in text
            or "rebalance" in text
            or "unknownmemberid" in text
            or "illegalgeneration" in text
        )
