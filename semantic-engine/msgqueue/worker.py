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
import re
from typing import Dict, Any, Optional, List, Tuple
from datetime import datetime, timezone, timedelta
from zoneinfo import ZoneInfo

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


def _build_stream_queue_group(base_group: str, stream: str) -> str:
    """根据 stream 构造稳定的 Kafka consumer group 名称。"""
    sanitized = re.sub(r"[^a-zA-Z0-9]+", "-", str(stream or "").strip().lower()).strip("-")
    if not sanitized:
        sanitized = "default"
    return f"{base_group}-{sanitized}"


def _read_bool_env(name: str, default: bool = True) -> bool:
    """读取布尔环境变量。"""
    raw = str(os.getenv(name, str(default))).strip().lower()
    if raw in {"1", "true", "yes", "on"}:
        return True
    if raw in {"0", "false", "no", "off"}:
        return False
    return default


def _build_batch_async_insert_settings() -> Dict[str, int]:
    """构建批量写入 async_insert 配置。"""
    return {
        "async_insert": 1 if _read_bool_env("CH_BATCH_ASYNC_INSERT", True) else 0,
        "wait_for_async_insert": 1 if _read_bool_env("CH_BATCH_WAIT_FOR_ASYNC_INSERT", True) else 0,
    }


def _candidate_text(value: Any) -> str:
    """返回清洗后的字符串，空值与 unknown 统一为 ''。"""
    if value is None:
        return ""
    text = str(value).strip()
    if not text:
        return ""
    if text.lower() == "unknown":
        return ""
    return text


def _derive_service_name_from_pod(pod_name: Any) -> str:
    """从 pod 名中推断服务名。"""
    pod = _candidate_text(pod_name)
    if not pod:
        return ""

    match = re.match(r"^(.+)-[a-f0-9]{8,10}-[a-z0-9]{5,10}$", pod)
    if match:
        return match.group(1)
    match = re.match(r"^(.+?)-[a-f0-9]{8,10}(-[a-f0-9]{4,8})?$", pod)
    if match:
        return match.group(1)
    match = re.match(r"^(.+)-[a-z0-9]{5}$", pod)
    if match:
        return match.group(1)
    match = re.match(r"^(.+)-\d+$", pod)
    if match:
        return match.group(1)
    return pod


_LOG_TIMESTAMP_DEFAULT_TIMEZONE_ENV = "LOG_TIMESTAMP_DEFAULT_TIMEZONE"
_LOG_TIMESTAMP_DEFAULT_TIMEZONE = (
    str(os.getenv(_LOG_TIMESTAMP_DEFAULT_TIMEZONE_ENV, "UTC") or "UTC").strip() or "UTC"
)
_TIMESTAMP_TZ_HINT_KEYS = (
    "timestamp_timezone",
    "timestamp_tz",
    "time_zone",
    "timezone",
    "tz",
    "event.timezone",
    "log_timezone",
    "source_timezone",
)
_TIMESTAMP_NUMERIC_PATTERN = re.compile(r"[+-]?\d+(?:\.\d+)?$")
_TIMESTAMP_TZ_SUFFIX_PATTERN = re.compile(r"(?:[zZ]|[+-]\d{2}:?\d{2})$")


def _offset_to_label(offset: Optional[timedelta]) -> str:
    """Convert UTC offset to +HH:MM/-HH:MM label."""
    if offset is None:
        return "UTC"
    total_seconds = int(offset.total_seconds())
    if total_seconds == 0:
        return "UTC"
    sign = "+" if total_seconds >= 0 else "-"
    abs_seconds = abs(total_seconds)
    hours, remainder = divmod(abs_seconds, 3600)
    minutes = remainder // 60
    return f"{sign}{hours:02d}:{minutes:02d}"


def _resolve_timezone_info(raw_value: Any) -> Tuple[Any, str]:
    """Resolve timezone hint into tzinfo + normalized label."""
    text = str(raw_value or "").strip()
    if not text:
        return timezone.utc, "UTC"

    if text.upper() in {"UTC", "Z"}:
        return timezone.utc, "UTC"

    offset_match = re.fullmatch(r"([+-])(\d{2}):?(\d{2})", text)
    if offset_match:
        sign, hh, mm = offset_match.groups()
        minutes = int(hh) * 60 + int(mm)
        if sign == "-":
            minutes = -minutes
        return timezone.utc if minutes == 0 else timezone(timedelta(minutes=minutes)), f"{sign}{hh}:{mm}"

    try:
        return ZoneInfo(text), text
    except Exception:
        logger.warning("Invalid timezone hint=%s, fallback to UTC", text)
        return timezone.utc, "UTC"


def _extract_timestamp_timezone_hint(
    *,
    log_data: Dict[str, Any],
    raw_attributes: Dict[str, Any],
    log_meta: Dict[str, Any],
) -> str:
    """Extract timestamp timezone hint from multiple payload locations."""
    candidates: List[Any] = []
    for key in _TIMESTAMP_TZ_HINT_KEYS:
        candidates.append(log_meta.get(key))
        candidates.append(raw_attributes.get(key))

    attributes = log_data.get("attributes", {})
    resource = log_data.get("resource", {})
    kubernetes = log_data.get("kubernetes", {})
    if isinstance(attributes, dict):
        for key in _TIMESTAMP_TZ_HINT_KEYS:
            candidates.append(attributes.get(key))
    if isinstance(resource, dict):
        for key in _TIMESTAMP_TZ_HINT_KEYS:
            candidates.append(resource.get(key))
    if isinstance(kubernetes, dict):
        for key in _TIMESTAMP_TZ_HINT_KEYS:
            candidates.append(kubernetes.get(key))

    for candidate in candidates:
        text = str(candidate or "").strip()
        if text:
            return text
    return _LOG_TIMESTAMP_DEFAULT_TIMEZONE


def _timestamp_has_explicit_tz(value: Any) -> bool:
    """Return True when timestamp string has explicit timezone suffix."""
    text = str(value or "").strip()
    if not text:
        return False
    return bool(_TIMESTAMP_TZ_SUFFIX_PATTERN.search(text))


def _timestamp_looks_numeric(value: Any) -> bool:
    """Return True when timestamp value is numeric/epoch-like."""
    if isinstance(value, (int, float)):
        return True
    text = str(value or "").strip()
    if not text:
        return False
    return bool(_TIMESTAMP_NUMERIC_PATTERN.fullmatch(text))


def _normalize_iso_fraction_precision(text: str) -> str:
    """
    Trim ISO fractional seconds to microseconds for datetime.fromisoformat.

    Python datetime parses up to 6 fractional digits. Inputs from some collectors
    may carry nanoseconds; we truncate excess precision deterministically.
    """
    normalized = str(text or "")
    # with timezone suffix, e.g. 2026-03-09T16:47:50.578950522+08:00 / +0800 / Z
    normalized = re.sub(r"(\.\d{6})\d+(?=(?:[zZ]|[+-]\d{2}:?\d{2})$)", r"\1", normalized)
    # without timezone suffix
    normalized = re.sub(r"(\.\d{6})\d+$", r"\1", normalized)
    return normalized


def _select_timestamp_input(
    *,
    log_data: Dict[str, Any],
    raw_attributes: Dict[str, Any],
    log_meta: Dict[str, Any],
) -> Dict[str, Any]:
    """
    Select best timestamp candidate and expose decision metadata.

    Priority:
      1) Container envelope raw time with explicit timezone (`time`/`collector_time`)
      2) Numeric epoch candidates
      3) Explicit timezone candidates
      4) First non-empty candidate
    """
    candidates: List[Tuple[str, Any, str]] = []

    def _append_candidate(source: str, value: Any) -> None:
        if value is None:
            return
        text = str(value).strip()
        if not text:
            return
        candidates.append((source, value, text))

    _append_candidate("raw_attributes.time_unix_nano", raw_attributes.get("time_unix_nano"))
    _append_candidate("raw_attributes.timeUnixNano", raw_attributes.get("timeUnixNano"))
    _append_candidate("event.timestamp", log_data.get("timestamp"))
    _append_candidate("event.time", log_data.get("time"))
    _append_candidate("event.@timestamp", log_data.get("@timestamp"))
    _append_candidate("raw_attributes.time", raw_attributes.get("time"))
    _append_candidate("log_meta.collector_time", log_meta.get("collector_time"))
    _append_candidate("log_meta.timestamp_utc", log_meta.get("timestamp_utc"))
    _append_candidate("log_meta.timestamp_raw", log_meta.get("timestamp_raw"))
    _append_candidate("raw_attributes.timestamp_raw", raw_attributes.get("timestamp_raw"))
    _append_candidate("raw_attributes.@timestamp", raw_attributes.get("@timestamp"))

    if not candidates:
        return {
            "value": "",
            "source": "missing",
            "selection_strategy": "missing",
        }

    stream_text = _candidate_text(raw_attributes.get("stream") or log_meta.get("stream")).lower()
    has_container_envelope = stream_text in {"stdout", "stderr"} or _candidate_text(raw_attributes.get("logtag")) != ""
    if has_container_envelope:
        for source, value, text in candidates:
            if source in {"raw_attributes.time", "log_meta.collector_time"} and _timestamp_has_explicit_tz(text):
                return {
                    "value": value,
                    "source": source,
                    "selection_strategy": "container_explicit_tz",
                }

    for source, value, text in candidates:
        if _timestamp_looks_numeric(text):
            return {
                "value": value,
                "source": source,
                "selection_strategy": "epoch_candidate",
            }

    for source, value, text in candidates:
        if _timestamp_has_explicit_tz(text):
            return {
                "value": value,
                "source": source,
                "selection_strategy": "explicit_tz_candidate",
            }

    source, value, _ = candidates[0]
    return {
        "value": value,
        "source": source,
        "selection_strategy": "first_non_empty",
    }


def _normalize_timestamp_to_utc(
    timestamp_value: Any,
    source_tz_hint: str,
) -> Dict[str, Any]:
    """
    Normalize incoming timestamp to UTC.

    Returns:
      {
        "timestamp_utc": RFC3339 UTC text,
        "timestamp_dt_utc_naive": datetime without tzinfo (UTC),
        "timestamp_raw": original text,
        "timestamp_source_tz": source timezone label,
        "timestamp_parse_strategy": strategy label,
        "timestamp_calibrated": bool,
      }
    """
    now_utc = datetime.now(timezone.utc)
    source_tzinfo, source_tz_label = _resolve_timezone_info(source_tz_hint)

    raw_text = ""
    if timestamp_value is not None:
        raw_text = str(timestamp_value).strip()

    parse_strategy = "fallback_observed"
    calibrated = True
    dt_utc = now_utc

    numeric_value: Optional[float] = None
    if isinstance(timestamp_value, (int, float)):
        numeric_value = float(timestamp_value)
    elif raw_text and re.fullmatch(r"[+-]?\d+(?:\.\d+)?", raw_text):
        try:
            numeric_value = float(raw_text)
        except (TypeError, ValueError):
            numeric_value = None

    if numeric_value is not None:
        absolute_value = abs(numeric_value)
        if absolute_value >= 1e17:
            seconds = numeric_value / 1_000_000_000
            parse_strategy = "epoch_nanoseconds"
        elif absolute_value >= 1e14:
            seconds = numeric_value / 1_000_000
            parse_strategy = "epoch_microseconds"
        elif absolute_value >= 1e11:
            seconds = numeric_value / 1_000
            parse_strategy = "epoch_milliseconds"
        else:
            seconds = numeric_value
            parse_strategy = "epoch_seconds"
        try:
            dt_utc = datetime.fromtimestamp(seconds, tz=timezone.utc)
            calibrated = False
            source_tz_label = "UTC"
        except (OSError, OverflowError, ValueError):
            parse_strategy = "fallback_observed"
            dt_utc = now_utc
            calibrated = True
    elif raw_text:
        normalized = raw_text
        if " " in normalized and "T" not in normalized:
            normalized = normalized.replace(" ", "T")
        normalized = _normalize_iso_fraction_precision(normalized)
        if normalized.endswith("Z"):
            normalized = f"{normalized[:-1]}+00:00"
        if len(normalized) >= 5 and normalized[-5] in {"+", "-"} and normalized[-3] != ":":
            normalized = f"{normalized[:-2]}:{normalized[-2:]}"

        try:
            parsed = datetime.fromisoformat(normalized)
            if parsed.tzinfo is None:
                parse_strategy = "assumed_tz"
                parsed = parsed.replace(tzinfo=source_tzinfo)
                calibrated = source_tz_label != "UTC"
            else:
                parse_strategy = "explicit_tz"
                parsed_offset = parsed.utcoffset()
                calibrated = parsed_offset not in {None, timedelta(0)}
                source_tz_label = _offset_to_label(parsed_offset)
            dt_utc = parsed.astimezone(timezone.utc)
        except Exception:
            parse_strategy = "fallback_observed"
            dt_utc = now_utc
            calibrated = True

    timestamp_utc_text = dt_utc.isoformat(timespec="microseconds").replace("+00:00", "Z")
    timestamp_dt_utc_naive = dt_utc.replace(tzinfo=None)

    return {
        "timestamp_utc": timestamp_utc_text,
        "timestamp_dt_utc_naive": timestamp_dt_utc_naive,
        "timestamp_raw": raw_text,
        "timestamp_source_tz": source_tz_label,
        "timestamp_parse_strategy": parse_strategy,
        "timestamp_calibrated": calibrated,
    }


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
        self._stopped = False
        self._stop_lock = asyncio.Lock()
        # 默认保持与原行为一致，可通过环境变量关闭语义事件写入降低写放大
        self.enable_semantic_event_write = _read_bool_env("ENABLE_SEMANTIC_EVENT_WRITE", True)
        try:
            self.log_message_max_chars = int(os.getenv("LOG_MESSAGE_MAX_CHARS", "25000"))
        except (TypeError, ValueError):
            self.log_message_max_chars = 25000
        if self.log_message_max_chars < 0:
            self.log_message_max_chars = 0

    async def initialize(self) -> bool:
        """
        初始化 Worker

        Returns:
            bool: 是否初始化成功
        """
        try:
            if config.queue_type != "kafka":
                logger.error("Unsupported queue backend: %s (kafka only)", config.queue_type)
                return False

            from msgqueue.kafka_adapter import KafkaQueue

            logger.info(
                "Connecting to Kafka: brokers=%s group=%s",
                config.kafka_brokers,
                config.kafka_group_id,
            )
            self.queue = KafkaQueue(
                servers=config.kafka_brokers,
                group_id=config.kafka_group_id,
                client_id=config.kafka_client_id,
                auto_offset_reset=config.kafka_auto_offset_reset,
                poll_timeout_ms=config.kafka_poll_timeout_ms,
                max_poll_interval_ms=config.kafka_max_poll_interval_ms,
                session_timeout_ms=config.kafka_session_timeout_ms,
                heartbeat_interval_ms=config.kafka_heartbeat_interval_ms,
                callback_offload=config.kafka_callback_offload,
                flush_offload=config.kafka_flush_offload,
                commit_error_as_warning=config.kafka_commit_error_as_warning,
                max_batch_size=config.kafka_max_batch_size,
                max_retry_attempts=config.kafka_max_retry_attempts,
                retry_delay_seconds=config.kafka_retry_delay_seconds,
            )

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
                async_insert_settings = _build_batch_async_insert_settings()
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
                    async_insert_settings=async_insert_settings,
                )
                self.log_writer.start()
                logger.info(
                    "BatchClickHouseWriter started for logs table (async_insert=%s wait_for_async_insert=%s)",
                    async_insert_settings["async_insert"],
                    async_insert_settings["wait_for_async_insert"],
                )

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
                return await self._process_log_envelope(message)
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

    async def _process_log_envelope(self, message: Message) -> bool:
        """
        处理 logs 队列消息，兼容单条与批量 envelope。

        新格式:
        {
          "signal_type": "logs",
          "batched": true,
          "record_count": N,
          "records": [...]
        }
        """
        try:
            payload = json.loads(message.data.decode("utf-8"))
        except Exception:
            # 非法 JSON 交由原路径处理并触发重试/DLQ 机制
            return await self._process_log_message(message)

        is_batched_logs = (
            isinstance(payload, dict)
            and str(payload.get("signal_type", "")).strip().lower() == "logs"
            and isinstance(payload.get("records"), list)
        )
        if not is_batched_logs:
            return await self._process_log_message(message)

        records = payload.get("records") or []
        if not records:
            logger.debug("Received empty batched logs envelope")
            return True

        return self._process_log_records_batch(records)

    async def _process_log_message(self, message: Message) -> bool:
        """处理日志消息。"""
        # 调试：显示收到消息
        logger.debug("Received log message from %s", message.subject)

        try:
            log_data = json.loads(message.data.decode("utf-8"))
        except Exception as e:
            logger.warning("Failed to decode log message payload: %s", e)
            self.error_count += 1
            return False

        return self._process_log_record(log_data)

    def _process_log_records_batch(self, records: List[Dict[str, Any]]) -> bool:
        """
        批量处理 logs envelope 中的 records，避免逐条 JSON 编解码。
        """
        normalized_events: List[Dict[str, Any]] = []
        skipped_records = 0
        for record in records:
            if not isinstance(record, dict):
                logger.warning("Unexpected log record type in batched envelope: %s", type(record))
                self.error_count += 1
                skipped_records += 1
                continue
            normalized = self._normalize_log_payload(record)
            if not normalized:
                logger.warning("Failed to normalize one record in batched logs envelope")
                self.error_count += 1
                skipped_records += 1
                continue
            normalized_events.append(normalized)

        if not normalized_events:
            if skipped_records > 0:
                logger.warning("Skipped %s invalid records in batched logs envelope; no valid records remained", skipped_records)
            return True

        if self.log_writer:
            success = self._save_events_batch(normalized_events)
        else:
            success = True
            for normalized in normalized_events:
                if not self.storage.save_event(normalized):
                    success = False
                    break

        if not success:
            logger.warning("Failed to save batched normalized events")
            return False

        if skipped_records > 0:
            logger.warning(
                "Skipped %s invalid records in batched logs envelope; saved %s valid records",
                skipped_records,
                len(normalized_events),
            )

        self.processed_count += len(normalized_events)
        if self.processed_count % 10 == 0:
            logger.info("Processed logs count=%s", self.processed_count)
        return True

    def _process_log_record(self, log_data: Dict[str, Any]) -> bool:
        """处理单条已解析的日志记录。"""
        normalized = self._normalize_log_payload(log_data)
        if not normalized:
            logger.warning("Failed to normalize log payload")
            self.error_count += 1
            return False

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

    def _normalize_log_payload(self, log_data: Dict[str, Any]) -> Optional[Dict[str, Any]]:
        """标准化并修复单条日志的上下文字段。"""
        # ⭐ DEBUG: 检查接收到的数据中的labels
        k8s_data = log_data.get("kubernetes", {})
        labels = k8s_data.get("labels", {})
        logger.debug("Received kubernetes.labels=%s", labels)
        logger.debug("kubernetes keys=%s", list(k8s_data.keys()))

        normalized = normalize_log(log_data)
        if not normalized:
            return None

        # level 统一由 normalize_log 基于结构化字段提取。
        # 避免扫描整条 message 导致“包含 ERROR/WARN 关键词即误判级别”。
        normalized.setdefault("entity", {})
        normalized.setdefault("context", {})
        normalized["context"].setdefault("k8s", {})
        existing_k8s = normalized["context"]["k8s"]

        # service_name：保留 normalize_log 结果，仅在缺失时回退推断。
        attributes = log_data.get("attributes", {})
        resource = log_data.get("resource", {})
        if not isinstance(attributes, dict):
            attributes = {}
        if not isinstance(resource, dict):
            resource = {}

        k8s_pod_name = (
            _candidate_text(k8s_data.get("pod") or k8s_data.get("pod_name"))
            or _candidate_text(attributes.get("k8s_pod_name"))
            or _candidate_text(attributes.get("pod_name"))
            or _candidate_text(existing_k8s.get("pod"))
        )
        service_name = _candidate_text(normalized["entity"].get("name"))
        if not service_name:
            service_name = _derive_service_name_from_pod(k8s_pod_name)
        if not service_name:
            service_obj = resource.get("service")
            service_name = (
                _candidate_text(log_data.get("service.name"))
                or _candidate_text(log_data.get("service_name"))
                or _candidate_text(attributes.get("service.name"))
                or _candidate_text(attributes.get("service_name"))
                or _candidate_text(resource.get("service.name"))
                or _candidate_text(resource.get("service_name"))
                or (_candidate_text(service_obj.get("name")) if isinstance(service_obj, dict) else "")
            )
        normalized["entity"]["name"] = service_name or "unknown"

        # 手动修复 k8s_context 字段（仅在有值时覆盖，保留已有标准化结果）
        k8s_namespace_name = (
            _candidate_text(k8s_data.get("namespace") or k8s_data.get("namespace_name"))
            or _candidate_text(attributes.get("k8s_namespace_name"))
            or _candidate_text(attributes.get("namespace"))
            or _candidate_text(existing_k8s.get("namespace"))
        )
        k8s_host = (
            _candidate_text(k8s_data.get("node") or k8s_data.get("node_name") or k8s_data.get("host"))
            or _candidate_text(attributes.get("k8s_host"))
            or _candidate_text(attributes.get("host"))
            or _candidate_text(existing_k8s.get("node"))
            or _candidate_text(existing_k8s.get("host"))
        )
        k8s_container_name = (
            _candidate_text(k8s_data.get("container_name"))
            or _candidate_text(attributes.get("k8s_container_name"))
            or _candidate_text(existing_k8s.get("container_name"))
        )
        k8s_container_id = (
            _candidate_text(k8s_data.get("container_id") or k8s_data.get("docker_id"))
            or _candidate_text(attributes.get("k8s_docker_id"))
            or _candidate_text(attributes.get("k8s_container_id"))
            or _candidate_text(existing_k8s.get("container_id"))
        )
        k8s_container_image = (
            _candidate_text(k8s_data.get("container_image"))
            or _candidate_text(attributes.get("k8s_container_image"))
            or _candidate_text(existing_k8s.get("container_image"))
        )
        k8s_pod_id = (
            _candidate_text(k8s_data.get("pod_id"))
            or _candidate_text(attributes.get("k8s_pod_id"))
            or _candidate_text(existing_k8s.get("pod_id"))
        )

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

        raw_attributes = normalized.get("_raw_attributes", {})
        if not isinstance(raw_attributes, dict):
            raw_attributes = {}
        raw_attributes = dict(raw_attributes)
        log_meta = raw_attributes.get("log_meta", {})
        if not isinstance(log_meta, dict):
            log_meta = {}

        selected_timestamp = _select_timestamp_input(
            log_data=normalized,
            raw_attributes=raw_attributes,
            log_meta=log_meta,
        )
        source_tz_hint = _extract_timestamp_timezone_hint(
            log_data=log_data,
            raw_attributes=raw_attributes,
            log_meta=log_meta,
        )
        normalized_ts = _normalize_timestamp_to_utc(
            selected_timestamp.get("value"),
            source_tz_hint=source_tz_hint,
        )
        normalized["timestamp"] = normalized_ts["timestamp_utc"]
        normalized["context"]["timestamp_source_tz"] = normalized_ts["timestamp_source_tz"]
        normalized["context"]["timestamp_parse_strategy"] = normalized_ts["timestamp_parse_strategy"]
        normalized["context"]["timestamp_input_source"] = selected_timestamp.get("source", "missing")
        normalized["context"]["timestamp_selection_strategy"] = selected_timestamp.get("selection_strategy", "missing")

        log_meta["timestamp_raw"] = normalized_ts["timestamp_raw"]
        log_meta["timestamp_utc"] = normalized_ts["timestamp_utc"]
        log_meta["timestamp_source_tz"] = normalized_ts["timestamp_source_tz"]
        log_meta["timestamp_parse_strategy"] = normalized_ts["timestamp_parse_strategy"]
        log_meta["timestamp_calibrated"] = bool(normalized_ts["timestamp_calibrated"])
        log_meta["timestamp_input_source"] = selected_timestamp.get("source", "missing")
        log_meta["timestamp_selection_strategy"] = selected_timestamp.get("selection_strategy", "missing")
        raw_attributes["timestamp_raw"] = normalized_ts["timestamp_raw"]
        raw_attributes["timestamp_utc"] = normalized_ts["timestamp_utc"]
        raw_attributes["timestamp_source_tz"] = normalized_ts["timestamp_source_tz"]
        raw_attributes["timestamp_parse_strategy"] = normalized_ts["timestamp_parse_strategy"]
        raw_attributes["timestamp_calibrated"] = bool(normalized_ts["timestamp_calibrated"])
        raw_attributes["timestamp_input_source"] = selected_timestamp.get("source", "missing")
        raw_attributes["timestamp_selection_strategy"] = selected_timestamp.get("selection_strategy", "missing")
        raw_attributes["log_meta"] = log_meta
        normalized["_raw_attributes"] = raw_attributes

        logger.debug("Normalized data preview=%s", json.dumps(normalized, indent=2)[:1000])
        return normalized

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
                os.getenv("KAFKA_TOPIC_LOGS", "logs.raw"),
                os.getenv("KAFKA_TOPIC_METRICS", "metrics.raw"),
                os.getenv("KAFKA_TOPIC_TRACES", "traces.raw"),
            ]
            streams = [stream for stream in dict.fromkeys(stream_candidates) if str(stream).strip()]

            # 订阅信号主题
            queue_group_base = os.getenv("QUEUE_GROUP", os.getenv("KAFKA_GROUP_ID", "log-workers"))
            group_per_stream = bool(config.queue_type == "kafka" and config.kafka_group_per_stream)

            for stream in streams:
                queue_group = (
                    _build_stream_queue_group(queue_group_base, stream)
                    if group_per_stream
                    else queue_group_base
                )
                logger.info("Subscribing to stream=%s group=%s", stream, queue_group)
                await self.queue.subscribe(
                    subject=stream,
                    callback=self.process_message,
                    queue_group=queue_group  # 使用队列组实现负载均衡
                )

            logger.info("Worker started and waiting for messages")

            # 保持运行
            while self.running:
                await asyncio.sleep(1)

        except Exception as e:
            logger.exception("Failed to start worker: %s", e)
            self.running = False

    def _prepare_event_row(self, event: Dict[str, Any]) -> Optional[Tuple[List[Any], Dict[str, Any], str, str]]:
        """
        将标准化事件转换为 ClickHouse logs 表行。

        Returns:
            Optional[Tuple[row, k8s_context, host, host_ip]]
        """
        try:
            k8s_context = event.get("context", {}).get("k8s", {})

            host = k8s_context.get("host", k8s_context.get("node", "unknown"))
            host_ip = k8s_context.get("host_ip", "")
            resources = k8s_context.get("resources", {}) or {}

            severity_number = int(event.get("severity_number", 0) or 0)
            flags = int(event.get("flags", 0) or 0)
            raw_attributes = event.get("_raw_attributes", {}) or {}
            if not isinstance(raw_attributes, dict):
                raw_attributes = {}
            raw_attributes = dict(raw_attributes)
            log_meta = raw_attributes.get("log_meta", {})
            if not isinstance(log_meta, dict):
                log_meta = {}

            selected_timestamp = _select_timestamp_input(
                log_data=event,
                raw_attributes=raw_attributes,
                log_meta=log_meta,
            )
            timestamp_input = selected_timestamp.get("value")
            source_tz_hint = _extract_timestamp_timezone_hint(
                log_data=event,
                raw_attributes=raw_attributes,
                log_meta=log_meta,
            )
            normalized_ts = _normalize_timestamp_to_utc(timestamp_input, source_tz_hint=source_tz_hint)
            ts_datetime = normalized_ts["timestamp_dt_utc_naive"]
            event["timestamp"] = normalized_ts["timestamp_utc"]
            log_meta["timestamp_raw"] = normalized_ts["timestamp_raw"]
            log_meta["timestamp_utc"] = normalized_ts["timestamp_utc"]
            log_meta["timestamp_source_tz"] = normalized_ts["timestamp_source_tz"]
            log_meta["timestamp_parse_strategy"] = normalized_ts["timestamp_parse_strategy"]
            log_meta["timestamp_calibrated"] = bool(normalized_ts["timestamp_calibrated"])
            log_meta["timestamp_input_source"] = selected_timestamp.get("source", "missing")
            log_meta["timestamp_selection_strategy"] = selected_timestamp.get("selection_strategy", "missing")
            raw_attributes["timestamp_raw"] = normalized_ts["timestamp_raw"]
            raw_attributes["timestamp_utc"] = normalized_ts["timestamp_utc"]
            raw_attributes["timestamp_source_tz"] = normalized_ts["timestamp_source_tz"]
            raw_attributes["timestamp_parse_strategy"] = normalized_ts["timestamp_parse_strategy"]
            raw_attributes["timestamp_calibrated"] = bool(normalized_ts["timestamp_calibrated"])
            raw_attributes["timestamp_input_source"] = selected_timestamp.get("source", "missing")
            raw_attributes["timestamp_selection_strategy"] = selected_timestamp.get("selection_strategy", "missing")

            raw_message = str(event.get("event", {}).get("raw", "") or "")
            message_value = raw_message

            if self.log_message_max_chars and len(raw_message) > self.log_message_max_chars:
                message_value = raw_message[: self.log_message_max_chars]
                log_meta["truncated"] = True
                log_meta["original_length"] = len(raw_message)
            else:
                log_meta.setdefault("truncated", False)

            line_count = log_meta.get("line_count")
            try:
                line_count_num = int(line_count)
            except (TypeError, ValueError):
                line_count_num = 0
            if line_count_num <= 0:
                line_count_num = message_value.count("\n") + 1 if message_value else 0
            log_meta["line_count"] = line_count_num
            if not isinstance(log_meta.get("merged"), bool):
                log_meta["merged"] = line_count_num > 1
            if not isinstance(log_meta.get("wrapped"), bool):
                log_meta["wrapped"] = False

            raw_attributes["log_meta"] = log_meta
            event["_raw_attributes"] = raw_attributes

            attributes_json = json.dumps(raw_attributes, ensure_ascii=False)
            labels_json = json.dumps(k8s_context.get("labels", {}) or {}, ensure_ascii=False)

            row = [
                event.get("id", "") or "",
                ts_datetime,
                ts_datetime,  # observed_timestamp
                event.get("entity", {}).get("name", "unknown") or "unknown",
                k8s_context.get("pod", "unknown") or "unknown",
                k8s_context.get("namespace", "islap") or "islap",
                host or "unknown",
                k8s_context.get("pod_id", "") or "",
                k8s_context.get("container_name", "") or "",
                k8s_context.get("container_id", "") or "",
                k8s_context.get("container_image", "") or "",
                event.get("event", {}).get("level", "info") or "info",
                severity_number,
                message_value,
                event.get("context", {}).get("trace_id", "") or "",
                event.get("context", {}).get("span_id", "") or "",
                flags,
                labels_json,
                attributes_json,
                host_ip,
                resources.get("cpu_limit", "") or "",
                resources.get("cpu_request", "") or "",
                resources.get("memory_limit", "") or "",
                resources.get("memory_request", "") or "",
            ]
            return row, k8s_context, host, host_ip
        except Exception as e:
            logger.exception("Failed to prepare event row for batch insert: %s", e)
            return None

    def _save_events_batch(self, events: List[Dict[str, Any]]) -> bool:
        """批量写入多条标准化日志事件。"""
        try:
            if not events:
                return True

            rows: List[List[Any]] = []
            semantic_payloads: List[Tuple[Dict[str, Any], Dict[str, Any], str, str]] = []
            skipped_events = 0
            for event in events:
                prepared = self._prepare_event_row(event)
                if not prepared:
                    skipped_events += 1
                    self.error_count += 1
                    continue
                row, k8s_context, host, host_ip = prepared
                rows.append(row)
                semantic_payloads.append((event, k8s_context, host, host_ip))

            if not rows:
                logger.warning("Skipped %s events because no event rows could be prepared for batch insert", skipped_events)
                return True

            self.log_writer.add_batch(rows)
            if self.enable_semantic_event_write and self.storage:
                for event, k8s_context, host, host_ip in semantic_payloads:
                    try:
                        self.storage._save_semantic_event(event, k8s_context, host, host_ip)
                    except Exception as semantic_error:
                        logger.warning("Failed to save semantic event in batch mode: %s", semantic_error)

            if skipped_events > 0:
                logger.warning("Skipped %s events during batch row preparation; inserted %s rows", skipped_events, len(rows))

            stats = self.log_writer.get_stats()
            if stats["buffer_size"] % 100 == 0:
                logger.debug(
                    "Batch writer buffer_size=%s total_rows=%s",
                    stats["buffer_size"],
                    stats["total_rows"],
                )
            return True
        except Exception as e:
            logger.exception("Failed to persist batched events: %s", e)
            return False

    def _save_event_batch(self, event: Dict[str, Any]) -> bool:
        """
        ⭐ P0优化：将单条事件转换为数据行并写入批量缓冲。
        """
        prepared = self._prepare_event_row(event)
        if not prepared:
            return False

        row, k8s_context, host, host_ip = prepared
        self.log_writer.add(row)
        if self.enable_semantic_event_write and self.storage:
            try:
                self.storage._save_semantic_event(event, k8s_context, host, host_ip)
            except Exception as semantic_error:
                logger.warning("Failed to save semantic event in batch mode: %s", semantic_error)

        stats = self.log_writer.get_stats()
        if stats["buffer_size"] % 100 == 0:
            logger.debug(
                "Batch writer buffer_size=%s total_rows=%s",
                stats["buffer_size"],
                stats["total_rows"],
            )
        return True

    def flush_pending_writes(self) -> bool:
        """
        刷新当前批次的缓冲数据，供队列适配器在 ACK 前调用。

        Returns:
            bool: 刷新成功返回 True；失败返回 False，触发消息重试。
        """
        if not self.log_writer:
            return True

        try:
            stats = self.log_writer.get_stats()
            if int(stats.get("buffer_size", 0)) <= 0:
                return True
            success = self.log_writer.flush()
            if not success:
                logger.error(
                    "Failed to flush pending log writer buffer before ACK (buffer_size=%s)",
                    stats.get("buffer_size", 0),
                )
            return bool(success)
        except Exception as e:
            logger.exception("Unexpected error while flushing pending writes before ACK: %s", e)
            return False

    async def stop(self) -> None:
        """停止 Worker"""
        async with self._stop_lock:
            if self._stopped:
                logger.debug("Worker stop already completed, skip duplicate call")
                return

            logger.info("Stopping worker")
            self.running = False

            # ⭐ P0优化：停止批量写入器
            if self.log_writer:
                try:
                    self.log_writer.stop()
                    logger.info("BatchClickHouseWriter stats: %s", self.log_writer.get_stats())
                except Exception as exc:
                    logger.warning("Failed to stop BatchClickHouseWriter cleanly: %s", exc)

            # 关闭队列连接
            if self.queue:
                try:
                    await self.queue.close()
                except Exception as exc:
                    logger.warning("Failed to close queue cleanly: %s", exc)

            # 关闭存储连接
            if self.storage:
                try:
                    self.storage.close()
                except Exception as exc:
                    logger.warning("Failed to close storage cleanly: %s", exc)

            self._stopped = True
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
