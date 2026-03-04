"""
日志处理器 Worker

从消息队列订阅日志消息，执行完整的处理流程：
标准化 → 存储（ClickHouse + Neo4j）
"""
import asyncio
import json
import signal
import sys
import os
import logging
from typing import Dict, Any, Optional
from datetime import datetime, timezone

# 添加项目路径到 Python 路径
_CURRENT_DIR = os.path.dirname(os.path.abspath(__file__))
_SEMANTIC_ENGINE_DIR = os.path.dirname(_CURRENT_DIR)
_PROJECT_ROOT = os.path.dirname(_SEMANTIC_ENGINE_DIR)
_SHARED_SRC_DIR = os.path.join(_PROJECT_ROOT, "shared_src")

for _path in (_SEMANTIC_ENGINE_DIR, _PROJECT_ROOT, _SHARED_SRC_DIR):
    if os.path.isdir(_path) and _path not in sys.path:
        sys.path.append(_path)

from config import config
from msgqueue.interface import Message
from msgqueue.signal_parser import infer_data_type, parse_metrics_points, parse_trace_spans
from normalize.normalizer import normalize_log
from storage.adapter import StorageAdapter

logger = logging.getLogger(__name__)


def _read_bool_env(name: str, default: bool = True) -> bool:
    """读取布尔环境变量。"""
    raw = str(os.getenv(name, str(default))).strip().lower()
    if raw in {"1", "true", "yes", "on"}:
        return True
    if raw in {"0", "false", "no", "off"}:
        return False
    return default


# ⭐ P0优化：导入批量写入器
try:
    from batch.clickhouse_writer import BatchClickHouseWriter
    BATCH_WRITER_AVAILABLE = True
except ImportError:
    BATCH_WRITER_AVAILABLE = False
    logger.warning("BatchClickHouseWriter not available, falling back to single insert")


class LogWorker:
    """日志处理器 Worker"""

    def __init__(self):
        """初始化 Worker"""
        self.queue = None  # 类型根据配置动态决定
        self.storage: StorageAdapter = None
        self.running = False
        self.processed_count = 0
        self.error_count = 0
        # ⭐ P0优化：批量写入器
        self.log_writer = None
        # 默认保持与原行为一致，可通过环境变量关闭语义事件写入降低写放大
        self.enable_semantic_event_write = _read_bool_env("ENABLE_SEMANTIC_EVENT_WRITE", True)

    async def initialize(self) -> bool:
        """
        初始化 Worker

        Returns:
            bool: 是否初始化成功
        """
        try:
            # 根据配置选择队列类型
            if config.queue_type == "redis":
                from msgqueue.redis_adapter import RedisStreamAdapter

                logger.info("Connecting to Redis: %s:%s", config.redis_host, config.redis_port)
                self.queue = RedisStreamAdapter(
                    host=config.redis_host,
                    port=config.redis_port,
                    db=config.redis_db,
                    password=config.redis_password
                )
            else:  # nats
                from msgqueue.nats_adapter import NATSQueue

                logger.info("Connecting to NATS: %s", config.nats_servers)
                self.queue = NATSQueue(servers=config.nats_servers)

            success = await self.queue.connect()

            if not success:
                logger.error("Failed to connect to queue backend: %s", config.queue_type)
                return False

            # 初始化存储适配器
            logger.info("Initializing storage adapter")
            storage_config = config.get_storage_config()
            self.storage = StorageAdapter(storage_config)

            # ⭐ P0优化：初始化批量写入器
            if BATCH_WRITER_AVAILABLE and hasattr(self.storage, 'ch_client') and self.storage.ch_client:
                self.log_writer = BatchClickHouseWriter(
                    client=self.storage.ch_client,
                    table="logs",
                    batch_size=int(os.getenv("CH_BATCH_SIZE", "2000")),
                    flush_interval=float(os.getenv("CH_FLUSH_INTERVAL", "0.5")),
                    columns=(
                        "id, timestamp, observed_timestamp, service_name, pod_name, namespace, node_name, "
                        "pod_id, container_name, container_id, container_image, level, severity_number, "
                        "message, trace_id, span_id, flags, labels, attributes_json, host_ip, "
                        "cpu_limit, cpu_request, memory_limit, memory_request"
                    ),
                )
                self.log_writer.start()
                logger.info("BatchClickHouseWriter started for logs table")

            # 在mock模式下不需要连接数据库
            # StorageAdapter会自动处理mock模式
            logger.info("Storage adapter initialized (mock mode)")

            return True

        except Exception as e:
            logger.exception("Worker initialization failed: %s", e)
            return False

    async def process_message(self, message: Message) -> bool:
        """
        处理单条队列消息（logs / metrics / traces）

        Args:
            message: 队列消息

        Returns:
            bool: 处理成功返回 True，失败返回 False（用于 XACK 确认）
        """
        try:
            data_type = infer_data_type(message.subject, message.headers)
            if data_type == "logs":
                return await self._process_log_message(message)
            if data_type == "metrics":
                return await self._process_metrics_message(message)
            if data_type == "traces":
                return await self._process_traces_message(message)

            logger.warning("Skipping unsupported message type=%s subject=%s", data_type, message.subject)
            return True

        except Exception as e:
            logger.exception("Failed to process message: %s", e)
            self.error_count += 1
            # ❌ 返回 False 表示处理失败
            return False

    async def _process_log_message(self, message: Message) -> bool:
        """处理日志消息。"""
        # 调试：显示收到消息
        logger.debug("Received log message from %s", message.subject)

        # 解析消息数据
        log_data = json.loads(message.data.decode('utf-8'))

        # ⭐ DEBUG: 检查接收到的数据中的labels
        k8s_data = log_data.get('kubernetes', {})
        labels = k8s_data.get('labels', {})
        logger.debug("Received kubernetes.labels=%s", labels)
        logger.debug("kubernetes keys=%s", list(k8s_data.keys()))

        # 标准化日志
        normalized = normalize_log(log_data)

        # level 统一由 normalize_log 基于结构化字段提取。
        # 避免扫描整条 message 导致“包含 ERROR/WARN 关键词即误判级别”。

        # ⭐ 手动修复 service_name 字段
        k8s_data = log_data.get("kubernetes", {})
        k8s_pod_name = k8s_data.get("pod", "") or k8s_data.get("pod_name", "")
        attributes = log_data.get("attributes", {})
        if not k8s_pod_name:
            k8s_pod_name = attributes.get("k8s_pod_name", "")
        service_name = "unknown"
        if k8s_pod_name and isinstance(k8s_pod_name, str):
            import re
            match = re.match(r'^(.+)-[a-f0-9]{8,10}-[a-z0-9]{5,10}$', k8s_pod_name)
            if match:
                service_name = match.group(1)
            else:
                match = re.match(r'^(.+)-[a-z0-9]{5,10}$', k8s_pod_name)
                if match:
                    service_name = match.group(1)
                else:
                    service_name = k8s_pod_name
        normalized["entity"]["name"] = service_name

        # ⭐ 手动修复 k8s_context 字段
        k8s_namespace_name = k8s_data.get("namespace", "") or k8s_data.get("namespace_name", "")
        if not k8s_namespace_name:
            k8s_namespace_name = attributes.get("k8s_namespace_name", "")
        k8s_host = k8s_data.get("node", "") or k8s_data.get("node_name", "") or k8s_data.get("host", "")
        if not k8s_host:
            k8s_host = attributes.get("k8s_host", "")
        k8s_container_name = k8s_data.get("container_name", "")
        if not k8s_container_name:
            k8s_container_name = attributes.get("k8s_container_name", "")
        k8s_container_id = k8s_data.get("container_id", "") or k8s_data.get("docker_id", "")
        if not k8s_container_id:
            k8s_container_id = attributes.get("k8s_docker_id", "")
        k8s_container_image = k8s_data.get("container_image", "")
        if not k8s_container_image:
            k8s_container_image = attributes.get("k8s_container_image", "")
        k8s_pod_id = k8s_data.get("pod_id", "")
        if not k8s_pod_id:
            k8s_pod_id = attributes.get("k8s_pod_id", "")

        normalized["context"]["k8s"]["pod"] = k8s_pod_name
        normalized["context"]["k8s"]["namespace"] = k8s_namespace_name
        normalized["context"]["k8s"]["node"] = k8s_host
        normalized["context"]["k8s"]["host"] = k8s_host
        normalized["context"]["k8s"]["container_name"] = k8s_container_name
        normalized["context"]["k8s"]["container_id"] = k8s_container_id
        normalized["context"]["k8s"]["container_image"] = k8s_container_image
        normalized["context"]["k8s"]["pod_id"] = k8s_pod_id

        # ⭐ 从 attributes 中提取 labels（如果有的话）
        if "labels" in k8s_data and isinstance(k8s_data["labels"], dict):
            normalized["context"]["k8s"]["labels"] = k8s_data["labels"]
        elif "labels" in attributes and isinstance(attributes["labels"], dict):
            normalized["context"]["k8s"]["labels"] = attributes["labels"]
        elif "k8s_labels" in attributes and isinstance(attributes["k8s_labels"], dict):
            normalized["context"]["k8s"]["labels"] = attributes["k8s_labels"]

        logger.debug("Normalized data preview=%s", json.dumps(normalized, indent=2)[:1000])

        if not normalized:
            logger.warning("Failed to normalize log payload")
            self.error_count += 1
            return False

        # ⭐ P0优化：使用批量写入器或逐条写入
        if self.log_writer:
            success = self._save_event_batch(normalized)
        else:
            success = self.storage.save_event(normalized)

        if not success:
            logger.warning("Failed to save normalized event")
            return False

        self.processed_count += 1
        if self.processed_count % 10 == 0:
            logger.info("Processed logs count=%s", self.processed_count)
        return True

    async def _process_metrics_message(self, message: Message) -> bool:
        """处理 metrics 信号消息。"""
        if not self.storage:
            logger.error("Storage adapter not initialized, skip metrics message")
            return False

        message_body = json.loads(message.data.decode("utf-8"))
        metrics_points = parse_metrics_points(message_body)
        if not metrics_points:
            logger.debug("No metrics points extracted from message")
            return True

        success = self.storage.save_metrics(metrics_points)
        if success:
            self.processed_count += len(metrics_points)
            logger.info("Saved metrics points count=%s", len(metrics_points))
            return True

        logger.warning("Failed to save metrics points count=%s", len(metrics_points))
        self.error_count += 1
        return False

    async def _process_traces_message(self, message: Message) -> bool:
        """处理 traces 信号消息。"""
        if not self.storage:
            logger.error("Storage adapter not initialized, skip traces message")
            return False

        message_body = json.loads(message.data.decode("utf-8"))
        traces = parse_trace_spans(message_body)
        if not traces:
            logger.debug("No traces extracted from message")
            return True

        success = self.storage.save_traces(traces)
        if success:
            self.processed_count += len(traces)
            logger.info("Saved traces count=%s", len(traces))
            return True

        logger.warning("Failed to save traces count=%s", len(traces))
        self.error_count += 1
        return False

    async def start(self) -> None:
        """启动 Worker"""
        try:
            self.running = True
            stream_candidates = [
                os.getenv("REDIS_STREAM_LOGS", "logs.raw"),
                os.getenv("REDIS_STREAM_METRICS", "metrics.raw"),
                os.getenv("REDIS_STREAM_TRACES", "traces.raw"),
            ]
            streams = [stream for stream in dict.fromkeys(stream_candidates) if str(stream).strip()]

            # 订阅信号主题
            for stream in streams:
                logger.info("Subscribing to stream=%s", stream)
                await self.queue.subscribe(
                    subject=stream,
                    callback=self.process_message,
                    queue_group="log-workers"  # 使用队列组实现负载均衡
                )

            logger.info("Worker started and waiting for messages")

            # 保持运行
            while self.running:
                await asyncio.sleep(1)

        except Exception as e:
            logger.exception("Failed to start worker: %s", e)
            self.running = False

    def _save_event_batch(self, event: Dict[str, Any]) -> bool:
        """
        ⭐ P0优化：将事件转换为数据行并批量写入
        
        Args:
            event: 标准化的事件数据
            
        Returns:
            bool: 是否成功
        """
        try:
            # 提取数据（复制自 StorageAdapter.save_event）
            k8s_context = event.get('context', {}).get('k8s', {})
            
            # 解析时间戳（支持纳秒格式和ISO格式）
            timestamp_str = event.get('timestamp', '')
            try:
                if timestamp_str:
                    # 尝试解析纳秒格式时间戳
                    if timestamp_str.isdigit():
                        ts_ns = int(timestamp_str)
                        ts_datetime = datetime.utcfromtimestamp(ts_ns / 1_000_000_000)
                    # 尝试解析ISO格式
                    elif 'T' in timestamp_str or '-' in timestamp_str:
                        ts_datetime = datetime.fromisoformat(timestamp_str.replace('Z', '+00:00'))
                        if ts_datetime.tzinfo is not None:
                            ts_datetime = ts_datetime.astimezone(timezone.utc).replace(tzinfo=None)
                    else:
                        ts_datetime = datetime.utcnow()
                else:
                    ts_datetime = datetime.utcnow()
            except Exception as ts_err:
                logger.debug("Timestamp parse error: %s, using utcnow", ts_err)
                ts_datetime = datetime.utcnow()
            
            host = k8s_context.get('host', k8s_context.get('node', 'unknown'))
            host_ip = k8s_context.get('host_ip', '')
            resources = k8s_context.get("resources", {}) or {}

            severity_number = int(event.get("severity_number", 0) or 0)
            flags = int(event.get("flags", 0) or 0)
            attributes_json = json.dumps(event.get("_raw_attributes", {}) or {}, ensure_ascii=False)
            labels_json = json.dumps(k8s_context.get("labels", {}) or {}, ensure_ascii=False)
            
            # 准备数据行（与 StorageAdapter._save_event_native 主路径保持一致）
            row = [
                event.get('id', '') or '',
                ts_datetime,
                ts_datetime,  # observed_timestamp
                event.get('entity', {}).get('name', 'unknown') or 'unknown',
                k8s_context.get('pod', 'unknown') or 'unknown',
                k8s_context.get('namespace', 'islap') or 'islap',
                host or 'unknown',
                k8s_context.get('pod_id', '') or '',
                k8s_context.get('container_name', '') or '',
                k8s_context.get('container_id', '') or '',
                k8s_context.get('container_image', '') or '',
                event.get('event', {}).get('level', 'info') or 'info',
                severity_number,
                str(event.get('event', {}).get('raw', '') or '')[:5000],
                event.get('context', {}).get('trace_id', '') or '',
                event.get('context', {}).get('span_id', '') or '',
                flags,
                labels_json,
                attributes_json,
                host_ip,
                resources.get("cpu_limit", "") or "",
                resources.get("cpu_request", "") or "",
                resources.get("memory_limit", "") or "",
                resources.get("memory_request", "") or "",
            ]
            
            # 添加到批量写入器
            self.log_writer.add(row)
            if self.enable_semantic_event_write and self.storage:
                try:
                    self.storage._save_semantic_event(event, k8s_context, host, host_ip)
                except Exception as semantic_error:
                    logger.warning("Failed to save semantic event in batch mode: %s", semantic_error)
            stats = self.log_writer.get_stats()
            if stats['buffer_size'] % 100 == 0:
                logger.debug(
                    "Batch writer buffer_size=%s total_rows=%s",
                    stats["buffer_size"],
                    stats["total_rows"],
                )
            return True

        except Exception as e:
            logger.exception("Failed to prepare event for batch insert: %s", e)
            return False

    async def stop(self) -> None:
        """停止 Worker"""
        logger.info("Stopping worker")
        self.running = False

        # ⭐ P0优化：停止批量写入器
        if self.log_writer:
            self.log_writer.stop()
            logger.info("BatchClickHouseWriter stats: %s", self.log_writer.get_stats())

        # 关闭队列连接
        if self.queue:
            await self.queue.close()

        # 关闭存储连接
        if self.storage:
            self.storage.close()

        logger.info("Worker stopped. processed=%s errors=%s", self.processed_count, self.error_count)

    def print_stats(self) -> None:
        """打印统计信息"""
        logger.info("Worker stats processed=%s errors=%s", self.processed_count, self.error_count)


async def main():
    """主函数"""
    # 创建 Worker
    worker = LogWorker()

    # 初始化
    if not await worker.initialize():
        logger.error("Failed to initialize worker")
        sys.exit(1)

    # 信号处理
    loop = asyncio.get_running_loop()

    def signal_handler():
        logger.info("Received shutdown signal")
        asyncio.create_task(worker.stop())

    for sig in (signal.SIGTERM, signal.SIGINT):
        loop.add_signal_handler(sig, signal_handler)

    # 启动 Worker
    try:
        await worker.start()
    except KeyboardInterrupt:
        logger.info("Keyboard interrupt")
    finally:
        await worker.stop()


if __name__ == "__main__":
    # 设置事件循环策略（兼容性）
    if sys.platform == 'linux':
        asyncio.set_event_loop_policy(asyncio.DefaultEventLoopPolicy())

    # 运行
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        logger.info("Interrupted by user")
        sys.exit(0)
