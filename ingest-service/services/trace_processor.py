"""
Traces 数据处理器（已废弃）
从 Redis Stream 读取 traces 数据并写入 ClickHouse。

注意：
- 该模块保留仅用于历史回溯/应急，不再作为主链路。
- 当前生产链路已迁移为：
  ingest-service -> Redis Stream -> semantic-engine-worker(log-workers) -> ClickHouse
"""
import json
import asyncio
import logging
from typing import Dict, Any, List, Optional
from datetime import datetime
import redis.asyncio as aioredis

from config import config

logger = logging.getLogger(__name__)


class TraceProcessor:
    """Traces 数据处理器"""

    def __init__(self):
        logger.warning(
            "TraceProcessor is deprecated. Use semantic-engine-worker(log-workers) as the active traces pipeline."
        )
        self.redis_client: Optional[aioredis.Redis] = None
        self.running = False
        self.processed_count = 0
        self.error_count = 0
        self.max_retries = max(1, int(getattr(config, "trace_processor_max_retries", 5)))
        self.retry_ttl_sec = max(60, int(getattr(config, "trace_processor_retry_ttl_sec", 86400)))
        self.dlq_stream = getattr(config, "trace_processor_dlq_stream", "traces.dlq")
        self.pending_idle_ms = max(1000, int(getattr(config, "trace_processor_pending_idle_ms", 60000)))
        self.pending_batch_size = max(1, int(getattr(config, "trace_processor_pending_batch_size", 50)))
        self.pending_recovery_interval_sec = max(
            5,
            int(getattr(config, "trace_processor_pending_recovery_interval_sec", 30))
        )

    async def initialize(self) -> bool:
        """初始化 Redis 连接"""
        try:
            self.redis_client = await aioredis.from_url(
                f"redis://{config.redis_host}:{config.redis_port}/{config.redis_db}",
                password=config.redis_password,
                decode_responses=True
            )
            await self.redis_client.ping()
            logger.info("TraceProcessor: Connected to Redis")
            return True
        except Exception as e:
            logger.error(f"TraceProcessor: Failed to connect to Redis: {e}")
            return False

    async def process_traces(self, traces_data: str, metadata: Dict[str, Any]) -> bool:
        """
        处理 traces 数据并写入 ClickHouse

        Args:
            traces_data: traces 数据（JSON 字符串）
            metadata: 元数据

        Returns:
            bool: 是否成功
        """
        try:
            # 解析队列中的 traces 数据
            data = json.loads(traces_data)
            otlp_payload = self._unwrap_trace_payload(data)

            # 提取 spans
            spans = self._extract_spans(otlp_payload)

            if not spans:
                logger.warning(
                    "TraceProcessor: No spans found in traces data, metadata=%s",
                    metadata
                )
                return True

            # 写入 ClickHouse
            success = await self._save_spans_to_clickhouse(spans)
            if not success:
                self.error_count += 1
                return False

            self.processed_count += len(spans)
            logger.info(f"TraceProcessor: Processed {len(spans)} spans")
            return True

        except Exception as e:
            logger.error(f"TraceProcessor: Failed to process traces: {e}")
            self.error_count += 1
            return False

    def _unwrap_trace_payload(self, data: Dict[str, Any]) -> Dict[str, Any]:
        """
        兼容 traces 队列消息格式，提取 OTLP payload。

        支持两种输入：
        1. 直接 OTLP JSON: {"resourceSpans": [...]}
        2. 队列包装格式: {"signal_type":"traces","payload":{...}} / {"raw_payload":"..."}
        """
        if not isinstance(data, dict):
            return {}

        # 直接 OTLP payload
        if "resourceSpans" in data:
            return data

        payload = data.get("payload")
        if isinstance(payload, dict):
            return payload
        if isinstance(payload, str):
            try:
                parsed_payload = json.loads(payload)
                if isinstance(parsed_payload, dict):
                    return parsed_payload
            except Exception:
                logger.warning("TraceProcessor: payload string is not valid JSON")

        raw_payload = data.get("raw_payload")
        if isinstance(raw_payload, str):
            try:
                parsed_raw = json.loads(raw_payload)
                if isinstance(parsed_raw, dict):
                    return parsed_raw
            except Exception:
                logger.warning("TraceProcessor: raw_payload is not valid JSON")

        return {}

    def _extract_spans(self, data: Dict[str, Any]) -> List[Dict[str, Any]]:
        """
        从 OTLP 数据中提取 spans

        Args:
            data: OTLP traces 数据

        Returns:
            List[Dict[str, Any]]: spans 列表
        """
        spans = []

        # 处理 resourceSpans
        resource_spans_list = data.get("resourceSpans", [])
        if not isinstance(resource_spans_list, list):
            resource_spans_list = [resource_spans_list]

        for resource_spans in resource_spans_list:
            resource = resource_spans.get("resource", {})
            resource_attrs = resource.get("attributes", [])
            service_name = self._get_attribute_value(resource_attrs, "service.name", "unknown")

            # 处理 scopeSpans
            scope_spans_list = resource_spans.get("scopeSpans", [])
            if not isinstance(scope_spans_list, list):
                scope_spans_list = [scope_spans_list]

            for scope_spans in scope_spans_list:
                # 处理 spans
                span_list = scope_spans.get("spans", [])
                if not isinstance(span_list, list):
                    span_list = [span_list]

                for span in span_list:
                    span_record = self._convert_span(span, service_name)
                    if span_record:
                        spans.append(span_record)

        return spans

    def _convert_span(self, span: Dict[str, Any], service_name: str) -> Optional[Dict[str, Any]]:
        """
        转换 span 为 ClickHouse 记录格式

        Args:
            span: OTLP span 数据
            service_name: 服务名称

        Returns:
            Optional[Dict[str, Any]]: 转换后的记录
        """
        try:
            trace_id = span.get("traceId", "")
            span_id = span.get("spanId", "")
            parent_span_id = span.get("parentSpanId", "")
            name = span.get("name", "")
            kind = span.get("kind", 0)

            # 处理时间戳
            start_time_unix_nano = int(span.get("startTimeUnixNano", 0) or 0)
            end_time_unix_nano = int(span.get("endTimeUnixNano", 0) or 0)
            if start_time_unix_nano <= 0:
                logger.warning("TraceProcessor: Missing startTimeUnixNano, skip span")
                return None
            if end_time_unix_nano <= 0:
                end_time_unix_nano = start_time_unix_nano

            # 转换为 datetime
            start_time = datetime.utcfromtimestamp(int(start_time_unix_nano) / 1e9)
            end_time = datetime.utcfromtimestamp(int(end_time_unix_nano) / 1e9)
            duration_ms = max((int(end_time_unix_nano) - int(start_time_unix_nano)) / 1e6, 0.0)

            # 处理状态
            status = span.get("status", {})
            status_code = status.get("code", 0)
            status_message = status.get("message", "")
            status_text = self._normalize_status(status_code)

            # 处理属性
            attributes = span.get("attributes", [])
            attrs_dict = {attr.get("key", ""): self._get_attribute_value([attr], attr.get("key", ""), "")
                         for attr in attributes}

            return {
                "trace_id": trace_id,
                "span_id": span_id,
                "parent_span_id": parent_span_id,
                "service_name": service_name,
                "operation_name": name,
                "start_time": start_time,
                "end_time": end_time,
                "duration_ms": duration_ms,
                "status": status_text,
                "status_message": status_message,
                "span_kind": kind,
                "tags": json.dumps(attrs_dict)
            }

        except Exception as e:
            logger.error(f"TraceProcessor: Failed to convert span: {e}")
            return None

    def _normalize_status(self, status_code: Any) -> str:
        """将 OTLP status.code 标准化为字符串枚举。"""
        try:
            code = int(status_code)
        except Exception:
            code = 0

        if code == 1:
            return "STATUS_CODE_OK"
        if code == 2:
            return "STATUS_CODE_ERROR"
        return "STATUS_CODE_UNSET"

    def _get_attribute_value(self, attributes: List[Dict], key: str, default: str = "") -> str:
        """获取属性值"""
        for attr in attributes:
            if attr.get("key") == key:
                value = attr.get("value", {})
                # 处理不同类型的值
                if "stringValue" in value:
                    return value["stringValue"]
                elif "intValue" in value:
                    return str(value["intValue"])
                elif "boolValue" in value:
                    return str(value["boolValue"])
        return default

    async def _save_spans_to_clickhouse(self, spans: List[Dict[str, Any]]) -> bool:
        """
        保存 spans 到 ClickHouse

        Args:
            spans: spans 列表

        Returns:
            bool: 是否成功
        """
        try:
            import aiohttp

            if not spans:
                return True

            database = getattr(config, "clickhouse_database", "logs")
            insert_query = (
                f"INSERT INTO {database}.traces "
                "(timestamp, trace_id, span_id, parent_span_id, service_name, operation_name, "
                "span_kind, status, attributes_json, events_json, links_json) FORMAT JSONEachRow"
            )

            rows = []
            for span in spans:
                attributes_json = span.get('tags', '{}')
                if isinstance(attributes_json, dict):
                    attributes_json = json.dumps(attributes_json)
                rows.append({
                    "timestamp": span["start_time"].strftime("%Y-%m-%d %H:%M:%S.%f"),
                    "trace_id": span["trace_id"],
                    "span_id": span["span_id"],
                    "parent_span_id": span["parent_span_id"],
                    "service_name": span["service_name"],
                    "operation_name": span["operation_name"],
                    "span_kind": str(span.get("span_kind", "")),
                    "status": span.get("status", "STATUS_CODE_UNSET"),
                    "attributes_json": attributes_json,
                    "events_json": "{}",
                    "links_json": "{}"
                })

            payload = "\n".join(
                json.dumps(row, ensure_ascii=False)
                for row in rows
            )

            # 发送请求到 ClickHouse
            async with aiohttp.ClientSession() as session:
                url = f"http://{config.clickhouse_host}:{config.clickhouse_port}"
                params = {"query": insert_query}
                async with session.post(url, params=params, data=payload.encode("utf-8")) as response:
                    if response.status == 200:
                        return True

                    logger.error(
                        "TraceProcessor: ClickHouse insert failed, status=%s, body=%s",
                        response.status,
                        await response.text()
                    )
                    return False

        except Exception as e:
            logger.error(f"TraceProcessor: Failed to save spans to ClickHouse: {e}")
            return False

    def _build_retry_key(self, stream: str, message_id: str) -> str:
        """构建重试计数 key。"""
        return f"trace_processor:retry:{stream}:{message_id}"

    async def _handle_failed_message(
        self,
        stream: str,
        consumer_group: str,
        message_id: str,
        fields: Dict[str, Any],
    ) -> None:
        """
        处理失败消息：重试计数、超过阈值入 DLQ 并 ACK，避免长期 pending。
        """
        retry_key = self._build_retry_key(stream, message_id)
        retry_count = await self.redis_client.incr(retry_key)
        if retry_count == 1:
            await self.redis_client.expire(retry_key, self.retry_ttl_sec)

        if retry_count <= self.max_retries:
            logger.warning(
                "TraceProcessor: message=%s processing failed (retry=%s/%s), keep pending for retry",
                message_id,
                retry_count,
                self.max_retries
            )
            return

        # 超过最大重试：写入 DLQ + ACK，避免毒消息长期悬挂
        dlq_payload = {
            "original_stream": stream,
            "message_id": message_id,
            "failed_at": datetime.utcnow().isoformat(),
            "retry_count": str(retry_count),
            "data_type": fields.get("data_type", ""),
            "ingest_time": fields.get("ingest_time", ""),
            "data": fields.get("data", ""),
        }
        try:
            await self.redis_client.xadd(self.dlq_stream, dlq_payload, maxlen=10000, approximate=True)
        except Exception as dlq_error:
            logger.error("TraceProcessor: Failed to write DLQ for message=%s: %s", message_id, dlq_error)

        await self.redis_client.xack(stream, consumer_group, message_id)
        await self.redis_client.delete(retry_key)
        logger.error(
            "TraceProcessor: message=%s exceeded max retries (%s), moved to DLQ=%s and ACKed",
            message_id,
            self.max_retries,
            self.dlq_stream
        )

    async def _process_stream_entry(
        self,
        stream: str,
        consumer_group: str,
        message_id: str,
        fields: Dict[str, Any]
    ) -> None:
        """处理单条 Redis Stream 消息。"""
        data_type = fields.get("data_type", "")
        if data_type != "traces":
            await self.redis_client.xack(stream, consumer_group, message_id)
            return

        traces_data = fields.get("data", "")
        metadata = {
            "content_type": fields.get("content_type", ""),
            "ingest_time": fields.get("ingest_time", "")
        }
        success = await self.process_traces(traces_data, metadata)

        if success:
            await self.redis_client.xack(stream, consumer_group, message_id)
            await self.redis_client.delete(self._build_retry_key(stream, message_id))
            return

        await self._handle_failed_message(stream, consumer_group, message_id, fields)

    async def _recover_pending_messages(
        self,
        stream: str,
        consumer_group: str,
        consumer_name: str
    ) -> None:
        """
        回收并处理长期 pending 消息，避免历史失败消息悬挂。
        """
        start_id = "0-0"
        total_claimed = 0

        while True:
            try:
                result = await self.redis_client.xautoclaim(
                    name=stream,
                    groupname=consumer_group,
                    consumername=consumer_name,
                    min_idle_time=self.pending_idle_ms,
                    start_id=start_id,
                    count=self.pending_batch_size
                )
            except Exception as e:
                # 某些 Redis 版本可能不支持 xautoclaim，降级为跳过
                logger.warning("TraceProcessor: XAUTOCLAIM failed, skip pending recovery: %s", e)
                return

            if not result or len(result) < 2:
                return

            next_start_id = result[0]
            entries = result[1] or []

            if not entries:
                return

            for msg_id, fields in entries:
                total_claimed += 1
                await self._process_stream_entry(stream, consumer_group, msg_id, fields)

            if next_start_id == "0-0":
                break

            start_id = next_start_id

        if total_claimed > 0:
            logger.info("TraceProcessor: Recovered and processed %s pending messages", total_claimed)

    async def start(self) -> None:
        """启动 traces 处理器"""
        if not await self.initialize():
            logger.error("TraceProcessor: Failed to initialize")
            return

        self.running = True
        logger.info("TraceProcessor: Started")

        trace_stream = getattr(config, "redis_stream_traces", config.redis_stream)
        consumer_group = "trace-processors"
        consumer_name = f"processor-{datetime.utcnow().strftime('%Y%m%d%H%M%S%f')}"

        # 创建消费者组（幂等）
        try:
            await self.redis_client.xgroup_create(
                name=trace_stream,
                groupname=consumer_group,
                id="0",
                mkstream=True
            )
            logger.info(
                "TraceProcessor: Created consumer group=%s stream=%s",
                consumer_group,
                trace_stream
            )
        except Exception as e:
            if "BUSYGROUP" in str(e):
                logger.info(
                    "TraceProcessor: Consumer group exists group=%s stream=%s",
                    consumer_group,
                    trace_stream
                )
            else:
                logger.error(f"TraceProcessor: Failed to create consumer group: {e}")
                return

        # 启动时先回收一次长期 pending 消息
        await self._recover_pending_messages(trace_stream, consumer_group, consumer_name)
        event_loop = asyncio.get_running_loop()
        next_recovery_at = event_loop.time() + self.pending_recovery_interval_sec

        while self.running:
            try:
                now = event_loop.time()
                if now >= next_recovery_at:
                    await self._recover_pending_messages(trace_stream, consumer_group, consumer_name)
                    next_recovery_at = now + self.pending_recovery_interval_sec

                # 从 Redis Stream 读取消息
                messages = await self.redis_client.xreadgroup(
                    groupname=consumer_group,
                    consumername=consumer_name,
                    streams={trace_stream: ">"},
                    count=self.pending_batch_size,
                    block=5000
                )

                if not messages:
                    continue

                for _stream_name, entries in messages:
                    for message_id, fields in entries:
                        await self._process_stream_entry(trace_stream, consumer_group, message_id, fields)

            except Exception as e:
                err_str = str(e)
                if "NOGROUP" in err_str:
                    logger.warning("TraceProcessor: Consumer group missing, recreating...")
                    try:
                        await self.redis_client.xgroup_create(
                            name=trace_stream,
                            groupname=consumer_group,
                            id="0",
                            mkstream=True
                        )
                    except Exception as create_err:
                        if "BUSYGROUP" not in str(create_err):
                            logger.error("TraceProcessor: Failed to recreate consumer group: %s", create_err)
                else:
                    logger.error(f"TraceProcessor: Error in processing loop: {e}")
                await asyncio.sleep(5)

    async def stop(self) -> None:
        """停止 traces 处理器"""
        self.running = False
        if self.redis_client:
            await self.redis_client.close()
        logger.info(f"TraceProcessor: Stopped. Processed: {self.processed_count}, Errors: {self.error_count}")


async def main():
    """主函数"""
    processor = TraceProcessor()

    # 信号处理
    import signal
    loop = asyncio.get_event_loop()

    def signal_handler():
        logger.info("TraceProcessor: Received shutdown signal")
        asyncio.create_task(processor.stop())

    for sig in (signal.SIGTERM, signal.SIGINT):
        loop.add_signal_handler(sig, signal_handler)

    # 启动处理器
    try:
        await processor.start()
    except KeyboardInterrupt:
        logger.info("TraceProcessor: Keyboard interrupt")
    finally:
        await processor.stop()


if __name__ == "__main__":
    asyncio.run(main())
