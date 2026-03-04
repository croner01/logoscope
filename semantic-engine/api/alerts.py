"""
告警管理 API
提供告警规则配置、检测、状态机和持久化能力。
"""
import asyncio
import base64
import binascii
import json
import logging
import os
import urllib.error
import urllib.request
import uuid
from datetime import datetime, timedelta, timezone
from typing import Any, Dict, List, Optional, Set, Tuple

from fastapi import HTTPException
from pydantic import BaseModel, Field

from storage.adapter import StorageAdapter

logger = logging.getLogger(__name__)

# 全局 storage 实例
_STORAGE_ADAPTER: Optional[StorageAdapter] = None

_RULE_TABLE_NAME: Optional[str] = None
_EVENT_TABLE_NAME: Optional[str] = None
_NOTIFICATION_TABLE_NAME: Optional[str] = None

ACTIVE_EVENT_STATUSES: Set[str] = {"pending", "firing", "acknowledged", "silenced"}
ALLOWED_NOTIFICATION_CHANNELS: Set[str] = {"inapp", "webhook"}


# 告警规则数据模型
class AlertRule(BaseModel):
    """告警规则模型"""

    id: Optional[str] = None
    name: str
    description: Optional[str] = None
    metric_name: str
    service_name: Optional[str] = None
    condition: str  # "gt", "lt", "eq", "gte", "lte"
    threshold: float
    duration: int = 60  # 持续时间（秒）
    severity: str = "warning"  # "critical", "warning", "info"
    enabled: bool = True
    labels: Dict[str, str] = Field(default_factory=dict)
    # 阶段 C：降噪与通知策略
    min_occurrence_count: int = 1
    notification_enabled: bool = True
    notification_channels: List[str] = Field(default_factory=lambda: ["inapp"])
    notification_cooldown_seconds: int = 300
    created_at: Optional[str] = None
    updated_at: Optional[str] = None


# 告警事件数据模型
class AlertEvent(BaseModel):
    """告警事件模型"""

    id: Optional[str] = None
    rule_id: str
    rule_name: str
    metric_name: str
    service_name: str
    current_value: float
    threshold: float
    condition: str
    severity: str
    message: str
    status: str = "firing"  # pending/firing/acknowledged/silenced/resolved
    fired_at: str
    resolved_at: Optional[str] = None
    first_triggered_at: Optional[str] = None
    last_triggered_at: Optional[str] = None
    acknowledged_at: Optional[str] = None
    silenced_until: Optional[str] = None
    occurrence_count: int = 1
    last_notified_at: Optional[str] = None
    notification_count: int = 0
    updated_at: Optional[str] = None
    labels: Dict[str, str] = Field(default_factory=dict)


class AlertRuleTemplate(BaseModel):
    """告警规则模板"""

    id: str
    name: str
    description: str
    metric_name: str
    condition: str
    threshold: float
    duration: int
    severity: str
    labels: Dict[str, str] = Field(default_factory=dict)


class CreateRuleFromTemplateRequest(BaseModel):
    """基于模板创建规则请求"""

    template_id: str
    name: Optional[str] = None
    description: Optional[str] = None
    service_name: Optional[str] = None
    threshold: Optional[float] = None
    duration: Optional[int] = None
    severity: Optional[str] = None
    labels: Dict[str, str] = Field(default_factory=dict)
    min_occurrence_count: Optional[int] = None
    notification_enabled: Optional[bool] = None
    notification_channels: Optional[List[str]] = None
    notification_cooldown_seconds: Optional[int] = None


# 内存存储（兜底）
_alert_rules: Dict[str, AlertRule] = {}
_alert_events: List[AlertEvent] = []
_alert_notifications: List[Dict[str, Any]] = []


_RULE_TEMPLATES: List[AlertRuleTemplate] = [
    AlertRuleTemplate(
        id="high-error-rate",
        name="高错误率",
        description="错误率超过阈值持续一段时间触发告警。",
        metric_name="error_rate",
        condition="gt",
        threshold=5.0,
        duration=120,
        severity="critical",
        labels={"category": "availability"},
    ),
    AlertRuleTemplate(
        id="high-latency-p95",
        name="高延迟 P95",
        description="P95 延迟异常升高时触发告警。",
        metric_name="latency_p95_ms",
        condition="gt",
        threshold=1000.0,
        duration=180,
        severity="warning",
        labels={"category": "performance"},
    ),
    AlertRuleTemplate(
        id="low-success-rate",
        name="低成功率",
        description="请求成功率低于阈值时触发告警。",
        metric_name="success_rate",
        condition="lt",
        threshold=99.0,
        duration=120,
        severity="critical",
        labels={"category": "availability"},
    ),
]


def _is_mock_object(value: Any) -> bool:
    if value is None:
        return False
    module_name = getattr(value.__class__, "__module__", "")
    return module_name.startswith("unittest.mock")


def _is_clickhouse_available() -> bool:
    if not _STORAGE_ADAPTER:
        return False
    ch_client = getattr(_STORAGE_ADAPTER, "ch_client", None)
    if ch_client is None or _is_mock_object(ch_client):
        return False
    return hasattr(ch_client, "execute")


def _now_utc() -> datetime:
    return datetime.now(timezone.utc)


def _now_iso() -> str:
    return _now_utc().isoformat()


def _to_datetime(value: Optional[str], default: Optional[datetime] = None) -> datetime:
    if isinstance(value, datetime):
        return value
    text = str(value or "").strip()
    if not text:
        return default or _now_utc()
    try:
        return datetime.fromisoformat(text.replace("Z", "+00:00"))
    except Exception:
        return default or _now_utc()


def _to_iso(value: Any) -> str:
    if isinstance(value, datetime):
        return value.astimezone(timezone.utc).isoformat()
    if value is None:
        return _now_iso()
    text = str(value)
    if " " in text and "T" not in text:
        return text.replace(" ", "T") + "+00:00"
    return text


def _normalize_csv_filter(value: Optional[str]) -> Set[str]:
    if not value:
        return set()
    values = set()
    for part in str(value).split(","):
        item = part.strip()
        if item:
            values.add(item)
    return values


def _safe_json_loads(raw: Any, fallback: Any) -> Any:
    if raw is None:
        return fallback
    if isinstance(raw, (dict, list)):
        return raw
    try:
        parsed = json.loads(str(raw))
        if isinstance(fallback, dict) and isinstance(parsed, dict):
            return parsed
        if isinstance(fallback, list) and isinstance(parsed, list):
            return parsed
        return fallback
    except Exception:
        return fallback


async def _run_blocking(func, *args, **kwargs):
    """在线程池执行阻塞 IO，避免阻塞事件循环。"""
    return await asyncio.to_thread(func, *args, **kwargs)


def _to_datetime_or_none(value: Optional[str]) -> Optional[datetime]:
    text = str(value or "").strip()
    if not text:
        return None
    try:
        return datetime.fromisoformat(text.replace("Z", "+00:00"))
    except Exception:
        return None


def _normalize_notification_channels(value: Any) -> List[str]:
    channels: List[str] = []
    if isinstance(value, list):
        channels = [str(item).strip().lower() for item in value]
    elif isinstance(value, str):
        channels = [part.strip().lower() for part in value.split(",")]

    normalized: List[str] = []
    for channel in channels:
        if channel and channel in ALLOWED_NOTIFICATION_CHANNELS and channel not in normalized:
            normalized.append(channel)

    return normalized or ["inapp"]


def _normalize_alert_rule(rule: AlertRule) -> AlertRule:
    rule.duration = max(0, int(rule.duration or 0))
    rule.min_occurrence_count = max(1, int(rule.min_occurrence_count or 1))
    rule.notification_enabled = bool(rule.notification_enabled)
    rule.notification_cooldown_seconds = max(0, int(rule.notification_cooldown_seconds or 0))
    rule.notification_channels = _normalize_notification_channels(rule.notification_channels)
    return rule


def _find_rule_by_id(rule_id: str) -> Optional[AlertRule]:
    return _alert_rules.get(str(rule_id or "").strip())


def _escape_sql_literal(value: Any) -> str:
    return str(value or "").replace("\\", "\\\\").replace("'", "\\'")


def _event_fingerprint(rule_id: str, service_name: str, metric_name: str) -> str:
    return f"{rule_id}:{service_name}:{metric_name}"


def _event_sort_key(event: AlertEvent) -> Tuple[str, str]:
    return (str(event.fired_at or ""), str(event.id or ""))


def _encode_cursor(event: AlertEvent) -> str:
    payload = {
        "fired_at": str(event.fired_at or ""),
        "id": str(event.id or ""),
    }
    encoded = base64.urlsafe_b64encode(
        json.dumps(payload, separators=(",", ":"), ensure_ascii=False).encode("utf-8")
    ).decode("ascii")
    return encoded.rstrip("=")


def _decode_cursor(cursor: str) -> Tuple[str, str]:
    raw = str(cursor or "").strip()
    if not raw:
        raise ValueError("cursor is empty")

    try:
        padded = raw + "=" * (-len(raw) % 4)
        decoded = base64.urlsafe_b64decode(padded.encode("ascii")).decode("utf-8")
        payload = json.loads(decoded)
    except (binascii.Error, UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise ValueError("invalid cursor format") from exc

    fired_at = str(payload.get("fired_at", "")).strip() if isinstance(payload, dict) else ""
    event_id = str(payload.get("id", "")).strip() if isinstance(payload, dict) else ""
    if not fired_at or not event_id:
        raise ValueError("cursor payload missing fired_at/id")
    return fired_at, event_id


def _evaluate_condition(rule: AlertRule, value: float) -> bool:
    if rule.condition == "gt":
        return value > rule.threshold
    if rule.condition == "lt":
        return value < rule.threshold
    if rule.condition == "eq":
        return value == rule.threshold
    if rule.condition == "gte":
        return value >= rule.threshold
    if rule.condition == "lte":
        return value <= rule.threshold
    return False


def _deduplicate_events(events: List[AlertEvent]) -> List[AlertEvent]:
    deduped: Dict[str, AlertEvent] = {}
    for event in events:
        event_id = str(event.id or "").strip()
        if not event_id:
            event.id = str(uuid.uuid4())
            event_id = event.id

        existing = deduped.get(event_id)
        if not existing:
            deduped[event_id] = event
            continue

        existing_time = _to_datetime(existing.updated_at or existing.fired_at)
        incoming_time = _to_datetime(event.updated_at or event.fired_at)
        if incoming_time >= existing_time:
            deduped[event_id] = event

    return list(deduped.values())


def _resolve_storage_tables() -> None:
    global _RULE_TABLE_NAME, _EVENT_TABLE_NAME, _NOTIFICATION_TABLE_NAME

    if not _STORAGE_ADAPTER:
        return

    database_name = (
        getattr(_STORAGE_ADAPTER, "ch_database", "")
        or (getattr(_STORAGE_ADAPTER, "config", {}) or {}).get("clickhouse", {}).get("database", "logs")
        or "logs"
    )
    _RULE_TABLE_NAME = f"{database_name}.alert_rules"
    _EVENT_TABLE_NAME = f"{database_name}.alert_events"
    _NOTIFICATION_TABLE_NAME = f"{database_name}.alert_notifications"


def _ensure_clickhouse_tables() -> None:
    if not _is_clickhouse_available():
        return

    _resolve_storage_tables()

    create_rule_table_sql = f"""
    CREATE TABLE IF NOT EXISTS {_RULE_TABLE_NAME} (
        rule_id String,
        name String,
        description String,
        metric_name String,
        service_name String,
        cond String,
        threshold Float64,
        duration UInt32,
        min_occurrence_count UInt16 DEFAULT 1,
        severity String,
        enabled UInt8,
        labels_json String,
        notification_enabled UInt8 DEFAULT 1,
        notification_channels_json String DEFAULT '["inapp"]',
        notification_cooldown_seconds UInt32 DEFAULT 300,
        created_at DateTime64(3, 'UTC'),
        updated_at DateTime64(3, 'UTC'),
        deleted UInt8 DEFAULT 0
    )
    ENGINE = ReplacingMergeTree(updated_at)
    PARTITION BY toYYYYMM(created_at)
    ORDER BY (rule_id)
    SETTINGS index_granularity = 8192
    """

    create_event_table_sql = f"""
    CREATE TABLE IF NOT EXISTS {_EVENT_TABLE_NAME} (
        event_id String,
        rule_id String,
        rule_name String,
        metric_name String,
        service_name String,
        current_value Float64,
        threshold Float64,
        cond String,
        severity String,
        message String,
        status String,
        fired_at DateTime64(3, 'UTC'),
        resolved_at Nullable(DateTime64(3, 'UTC')),
        first_triggered_at DateTime64(3, 'UTC'),
        last_triggered_at DateTime64(3, 'UTC'),
        acknowledged_at Nullable(DateTime64(3, 'UTC')),
        silenced_until Nullable(DateTime64(3, 'UTC')),
        occurrence_count UInt32,
        last_notified_at Nullable(DateTime64(3, 'UTC')),
        notification_count UInt32 DEFAULT 0,
        labels_json String,
        updated_at DateTime64(3, 'UTC'),
        deleted UInt8 DEFAULT 0
    )
    ENGINE = ReplacingMergeTree(updated_at)
    PARTITION BY toYYYYMM(fired_at)
    ORDER BY (event_id)
    SETTINGS index_granularity = 8192
    """

    create_notification_table_sql = f"""
    CREATE TABLE IF NOT EXISTS {_NOTIFICATION_TABLE_NAME} (
        notification_id String,
        event_id String,
        rule_id String,
        rule_name String,
        service_name String,
        severity String,
        event_status String,
        channel String,
        delivery_status String,
        detail String,
        created_at DateTime64(3, 'UTC')
    )
    ENGINE = MergeTree()
    PARTITION BY toYYYYMM(created_at)
    ORDER BY (created_at, notification_id)
    SETTINGS index_granularity = 8192
    """

    _STORAGE_ADAPTER.ch_client.execute(create_rule_table_sql)
    _STORAGE_ADAPTER.ch_client.execute(create_event_table_sql)
    _STORAGE_ADAPTER.ch_client.execute(create_notification_table_sql)

    # 向后兼容历史表结构（低风险增量演进）
    _STORAGE_ADAPTER.ch_client.execute(
        f"ALTER TABLE {_RULE_TABLE_NAME} ADD COLUMN IF NOT EXISTS min_occurrence_count UInt16 DEFAULT 1"
    )
    _STORAGE_ADAPTER.ch_client.execute(
        f"ALTER TABLE {_RULE_TABLE_NAME} ADD COLUMN IF NOT EXISTS notification_enabled UInt8 DEFAULT 1"
    )
    _STORAGE_ADAPTER.ch_client.execute(
        f"ALTER TABLE {_RULE_TABLE_NAME} ADD COLUMN IF NOT EXISTS notification_channels_json String DEFAULT '[\"inapp\"]'"
    )
    _STORAGE_ADAPTER.ch_client.execute(
        f"ALTER TABLE {_RULE_TABLE_NAME} ADD COLUMN IF NOT EXISTS notification_cooldown_seconds UInt32 DEFAULT 300"
    )

    _STORAGE_ADAPTER.ch_client.execute(
        f"ALTER TABLE {_EVENT_TABLE_NAME} ADD COLUMN IF NOT EXISTS last_notified_at Nullable(DateTime64(3, 'UTC'))"
    )
    _STORAGE_ADAPTER.ch_client.execute(
        f"ALTER TABLE {_EVENT_TABLE_NAME} ADD COLUMN IF NOT EXISTS notification_count UInt32 DEFAULT 0"
    )


def _persist_rule(rule: AlertRule, deleted: bool = False) -> None:
    if not _is_clickhouse_available() or not _RULE_TABLE_NAME:
        return

    sql = f"""
    INSERT INTO {_RULE_TABLE_NAME} (
        rule_id, name, description, metric_name, service_name, cond,
        threshold, duration, min_occurrence_count,
        severity, enabled, labels_json,
        notification_enabled, notification_channels_json, notification_cooldown_seconds,
        created_at, updated_at, deleted
    ) VALUES
    """

    row = {
        "rule_id": str(rule.id or ""),
        "name": str(rule.name or ""),
        "description": str(rule.description or ""),
        "metric_name": str(rule.metric_name or ""),
        "service_name": str(rule.service_name or ""),
        "cond": str(rule.condition or ""),
        "threshold": float(rule.threshold or 0.0),
        "duration": max(0, int(rule.duration or 0)),
        "min_occurrence_count": max(1, int(rule.min_occurrence_count or 1)),
        "severity": str(rule.severity or "warning"),
        "enabled": 1 if bool(rule.enabled) else 0,
        "labels_json": json.dumps(rule.labels or {}, ensure_ascii=False),
        "notification_enabled": 1 if bool(rule.notification_enabled) else 0,
        "notification_channels_json": json.dumps(_normalize_notification_channels(rule.notification_channels), ensure_ascii=False),
        "notification_cooldown_seconds": max(0, int(rule.notification_cooldown_seconds or 0)),
        "created_at": _to_datetime(rule.created_at),
        "updated_at": _to_datetime(rule.updated_at),
        "deleted": 1 if deleted else 0,
    }

    _STORAGE_ADAPTER.ch_client.execute(sql, [row])


def _persist_event(event: AlertEvent, deleted: bool = False) -> None:
    if not _is_clickhouse_available() or not _EVENT_TABLE_NAME:
        return

    sql = f"""
    INSERT INTO {_EVENT_TABLE_NAME} (
        event_id, rule_id, rule_name, metric_name, service_name,
        current_value, threshold, cond, severity, message,
        status, fired_at, resolved_at,
        first_triggered_at, last_triggered_at,
        acknowledged_at, silenced_until,
        occurrence_count, last_notified_at, notification_count,
        labels_json, updated_at, deleted
    ) VALUES
    """

    row = {
        "event_id": str(event.id or ""),
        "rule_id": str(event.rule_id or ""),
        "rule_name": str(event.rule_name or ""),
        "metric_name": str(event.metric_name or ""),
        "service_name": str(event.service_name or ""),
        "current_value": float(event.current_value or 0.0),
        "threshold": float(event.threshold or 0.0),
        "cond": str(event.condition or ""),
        "severity": str(event.severity or "warning"),
        "message": str(event.message or ""),
        "status": str(event.status or "pending"),
        "fired_at": _to_datetime(event.fired_at),
        "resolved_at": _to_datetime(event.resolved_at) if event.resolved_at else None,
        "first_triggered_at": _to_datetime(event.first_triggered_at or event.fired_at),
        "last_triggered_at": _to_datetime(event.last_triggered_at or event.fired_at),
        "acknowledged_at": _to_datetime(event.acknowledged_at) if event.acknowledged_at else None,
        "silenced_until": _to_datetime(event.silenced_until) if event.silenced_until else None,
        "occurrence_count": max(1, int(event.occurrence_count or 1)),
        "last_notified_at": _to_datetime(event.last_notified_at) if event.last_notified_at else None,
        "notification_count": max(0, int(event.notification_count or 0)),
        "labels_json": json.dumps(event.labels or {}, ensure_ascii=False),
        "updated_at": _to_datetime(event.updated_at or event.fired_at),
        "deleted": 1 if deleted else 0,
    }

    _STORAGE_ADAPTER.ch_client.execute(sql, [row])


def _persist_notification(record: Dict[str, Any]) -> None:
    if not _is_clickhouse_available() or not _NOTIFICATION_TABLE_NAME:
        return

    sql = f"""
    INSERT INTO {_NOTIFICATION_TABLE_NAME} (
        notification_id, event_id, rule_id, rule_name, service_name,
        severity, event_status, channel, delivery_status, detail, created_at
    ) VALUES
    """

    row = {
        "notification_id": str(record.get("id") or ""),
        "event_id": str(record.get("event_id") or ""),
        "rule_id": str(record.get("rule_id") or ""),
        "rule_name": str(record.get("rule_name") or ""),
        "service_name": str(record.get("service_name") or ""),
        "severity": str(record.get("severity") or "info"),
        "event_status": str(record.get("event_status") or ""),
        "channel": str(record.get("channel") or "inapp"),
        "delivery_status": str(record.get("delivery_status") or "unknown"),
        "detail": str(record.get("detail") or ""),
        "created_at": _to_datetime(record.get("created_at")),
    }

    _STORAGE_ADAPTER.ch_client.execute(sql, [row])


def _load_rules_from_storage() -> None:
    if not _is_clickhouse_available() or not _RULE_TABLE_NAME:
        return

    query = f"""
    SELECT
        rule_id, name, description, metric_name, service_name, cond,
        threshold, duration, min_occurrence_count,
        severity, enabled, labels_json,
        notification_enabled, notification_channels_json, notification_cooldown_seconds,
        created_at, updated_at
    FROM {_RULE_TABLE_NAME} FINAL
    WHERE deleted = 0
    ORDER BY updated_at DESC
    """

    rows = _STORAGE_ADAPTER.ch_client.execute(query)
    loaded: Dict[str, AlertRule] = {}
    for row in rows:
        if not row or len(row) < 17:
            continue

        rule_id = str(row[0] or "").strip()
        if not rule_id or rule_id in loaded:
            continue

        loaded[rule_id] = _normalize_alert_rule(AlertRule(
            id=rule_id,
            name=str(row[1] or ""),
            description=str(row[2] or "") or None,
            metric_name=str(row[3] or ""),
            service_name=str(row[4] or "") or None,
            condition=str(row[5] or ""),
            threshold=float(row[6] or 0.0),
            duration=int(row[7] or 0),
            min_occurrence_count=max(1, int(row[8] or 1)),
            severity=str(row[9] or "warning"),
            enabled=bool(row[10]),
            labels=_safe_json_loads(row[11], {}),
            notification_enabled=bool(row[12]) if row[12] is not None else True,
            notification_channels=_normalize_notification_channels(_safe_json_loads(row[13], ["inapp"])),
            notification_cooldown_seconds=max(0, int(row[14] or 0)),
            created_at=_to_iso(row[15]),
            updated_at=_to_iso(row[16]),
        ))

    _alert_rules.clear()
    _alert_rules.update(loaded)


def _load_events_from_storage(max_rows: int = 20000) -> None:
    if not _is_clickhouse_available() or not _EVENT_TABLE_NAME:
        return

    query = f"""
    SELECT
        event_id, rule_id, rule_name, metric_name, service_name,
        current_value, threshold, cond, severity, message,
        status, fired_at, resolved_at,
        first_triggered_at, last_triggered_at,
        acknowledged_at, silenced_until,
        occurrence_count, last_notified_at, notification_count,
        labels_json, updated_at
    FROM {_EVENT_TABLE_NAME} FINAL
    WHERE deleted = 0
    ORDER BY updated_at DESC
    LIMIT {max(1000, int(max_rows))}
    """

    rows = _STORAGE_ADAPTER.ch_client.execute(query)
    loaded: List[AlertEvent] = []
    seen_ids: Set[str] = set()
    for row in rows:
        if not row or len(row) < 22:
            continue

        event_id = str(row[0] or "").strip()
        if not event_id or event_id in seen_ids:
            continue
        seen_ids.add(event_id)

        loaded.append(
            AlertEvent(
                id=event_id,
                rule_id=str(row[1] or ""),
                rule_name=str(row[2] or ""),
                metric_name=str(row[3] or ""),
                service_name=str(row[4] or ""),
                current_value=float(row[5] or 0.0),
                threshold=float(row[6] or 0.0),
                condition=str(row[7] or ""),
                severity=str(row[8] or "warning"),
                message=str(row[9] or ""),
                status=str(row[10] or "pending"),
                fired_at=_to_iso(row[11]),
                resolved_at=_to_iso(row[12]) if row[12] else None,
                first_triggered_at=_to_iso(row[13]),
                last_triggered_at=_to_iso(row[14]),
                acknowledged_at=_to_iso(row[15]) if row[15] else None,
                silenced_until=_to_iso(row[16]) if row[16] else None,
                occurrence_count=max(1, int(row[17] or 1)),
                last_notified_at=_to_iso(row[18]) if row[18] else None,
                notification_count=max(0, int(row[19] or 0)),
                labels=_safe_json_loads(row[20], {}),
                updated_at=_to_iso(row[21]),
            )
        )

    _alert_events.clear()
    _alert_events.extend(_deduplicate_events(loaded))


def _load_notifications_from_storage(max_rows: int = 5000) -> None:
    if not _is_clickhouse_available() or not _NOTIFICATION_TABLE_NAME:
        return

    query = f"""
    SELECT
        notification_id, event_id, rule_id, rule_name, service_name,
        severity, event_status, channel, delivery_status, detail, created_at
    FROM {_NOTIFICATION_TABLE_NAME}
    ORDER BY created_at DESC
    LIMIT {max(100, int(max_rows))}
    """
    rows = _STORAGE_ADAPTER.ch_client.execute(query)
    loaded: List[Dict[str, Any]] = []
    for row in rows:
        if not row or len(row) < 11:
            continue
        loaded.append(
            {
                "id": str(row[0] or ""),
                "event_id": str(row[1] or ""),
                "rule_id": str(row[2] or ""),
                "rule_name": str(row[3] or ""),
                "service_name": str(row[4] or ""),
                "severity": str(row[5] or "info"),
                "event_status": str(row[6] or ""),
                "channel": str(row[7] or ""),
                "delivery_status": str(row[8] or "unknown"),
                "detail": str(row[9] or ""),
                "created_at": _to_iso(row[10]),
            }
        )

    _alert_notifications.clear()
    _alert_notifications.extend(list(reversed(loaded)))


def _sync_from_storage() -> None:
    if not _is_clickhouse_available():
        return
    try:
        _ensure_clickhouse_tables()
        _load_rules_from_storage()
        _load_events_from_storage()
        _load_notifications_from_storage()
    except Exception as exc:
        logger.warning(f"Failed to sync alerts from ClickHouse: {exc}")


def set_storage_adapter(adapter: StorageAdapter):
    """设置 storage adapter 实例"""
    global _STORAGE_ADAPTER
    _STORAGE_ADAPTER = adapter
    _resolve_storage_tables()
    _sync_from_storage()


def _find_event_by_id(event_id: str) -> Optional[AlertEvent]:
    for event in _alert_events:
        if str(event.id) == str(event_id):
            return event
    return None


def _notification_webhook_url() -> str:
    return str(os.getenv("ALERT_WEBHOOK_URL", "")).strip()


async def _append_notification_record(record: Dict[str, Any]) -> None:
    _alert_notifications.append(record)
    if len(_alert_notifications) > 5000:
        del _alert_notifications[:-5000]
    await _run_blocking(_persist_notification, record)


def _post_webhook(payload: Dict[str, Any], webhook_url: str, timeout_seconds: float = 5.0) -> Tuple[bool, str]:
    body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
    request = urllib.request.Request(
        webhook_url,
        data=body,
        headers={"Content-Type": "application/json"},
        method="POST",
    )
    try:
        with urllib.request.urlopen(request, timeout=timeout_seconds) as response:
            status_code = int(getattr(response, "status", 200))
            if status_code >= 400:
                return False, f"HTTP {status_code}"
            return True, "ok"
    except urllib.error.HTTPError as exc:
        return False, f"HTTP {exc.code}"
    except Exception as exc:
        return False, str(exc)


async def _notify_event(rule: Optional[AlertRule], event: AlertEvent, transition: str, force: bool = False) -> int:
    if not rule:
        return 0

    rule = _normalize_alert_rule(rule)
    if not rule.notification_enabled:
        return 0

    now_dt = _now_utc()
    now_iso = now_dt.isoformat()

    if not force:
        cooldown_seconds = max(0, int(rule.notification_cooldown_seconds or 0))
        if cooldown_seconds > 0:
            last_notified_dt = _to_datetime_or_none(event.last_notified_at)
            if last_notified_dt and (now_dt - last_notified_dt).total_seconds() < cooldown_seconds:
                return 0

    channels = _normalize_notification_channels(rule.notification_channels)
    webhook_url = _notification_webhook_url()
    attempted = 0
    delivered = 0

    for channel in channels:
        attempted += 1
        delivery_status = "ok"
        detail = ""

        if channel == "webhook":
            if not webhook_url:
                delivery_status = "skipped"
                detail = "ALERT_WEBHOOK_URL not configured"
            else:
                webhook_payload = {
                    "id": str(uuid.uuid4()),
                    "event_id": event.id,
                    "rule_id": event.rule_id,
                    "rule_name": event.rule_name,
                    "service_name": event.service_name,
                    "severity": event.severity,
                    "event_status": event.status,
                    "transition": transition,
                    "message": event.message,
                    "fired_at": event.fired_at,
                    "resolved_at": event.resolved_at,
                    "created_at": now_iso,
                }
                ok, detail = await _run_blocking(_post_webhook, webhook_payload, webhook_url)
                delivery_status = "ok" if ok else "failed"
        else:
            # inapp 通道仅记录，不执行外部投递
            delivery_status = "ok"
            detail = "inapp recorded"

        record = {
            "id": str(uuid.uuid4()),
            "event_id": str(event.id or ""),
            "rule_id": str(event.rule_id or ""),
            "rule_name": str(event.rule_name or ""),
            "service_name": str(event.service_name or ""),
            "severity": str(event.severity or "info"),
            "event_status": str(event.status or ""),
            "channel": channel,
            "delivery_status": delivery_status,
            "detail": detail,
            "transition": transition,
            "created_at": now_iso,
        }
        await _append_notification_record(record)

        if delivery_status == "ok":
            delivered += 1

    if attempted > 0:
        event.last_notified_at = now_iso
        event.notification_count = max(0, int(event.notification_count or 0)) + delivered
        event.updated_at = now_iso
        await _run_blocking(_persist_event, event)

    return delivered


def _is_rule_ready_to_fire(rule: AlertRule, event: AlertEvent, now_dt: datetime) -> bool:
    duration_seconds = max(0, int(rule.duration or 0))
    min_occurrence = max(1, int(rule.min_occurrence_count or 1))
    first_dt = _to_datetime(event.first_triggered_at or event.fired_at, default=now_dt)
    duration_ready = duration_seconds <= 0 or first_dt <= (now_dt - timedelta(seconds=duration_seconds))
    occurrence_ready = max(1, int(event.occurrence_count or 1)) >= min_occurrence
    return duration_ready and occurrence_ready


async def _resolve_event(event: AlertEvent, resolved_at: str, reason: str = "") -> bool:
    if event.status == "resolved":
        return False

    event.status = "resolved"
    event.resolved_at = resolved_at
    event.updated_at = resolved_at
    if reason:
        event.message = f"{event.message} | resolved: {reason}" if event.message else f"resolved: {reason}"
    await _run_blocking(_persist_event, event)
    return True


async def create_alert_rule(rule: AlertRule) -> Dict[str, Any]:
    """创建告警规则"""
    try:
        rule_id = str(uuid.uuid4())
        now_iso = _now_iso()
        rule.id = rule_id
        rule.created_at = now_iso
        rule.updated_at = now_iso
        rule = _normalize_alert_rule(rule)

        _alert_rules[rule_id] = rule
        await _run_blocking(_persist_rule, rule)

        logger.info(f"Created alert rule: {rule.name} (ID: {rule_id})")
        return {"status": "ok", "rule": rule.dict()}
    except Exception as e:
        logger.error(f"Error creating alert rule: {e}")
        raise HTTPException(status_code=500, detail="Internal server error")


async def update_alert_rule(rule_id: str, rule: Any) -> Dict[str, Any]:
    """更新告警规则（支持部分字段）"""
    try:
        if rule_id not in _alert_rules:
            raise HTTPException(status_code=404, detail="Rule not found")

        existing_rule = _alert_rules[rule_id]
        existing_data = existing_rule.dict()

        if isinstance(rule, AlertRule):
            incoming_data = rule.dict(exclude_unset=True)
        elif isinstance(rule, dict):
            incoming_data = {k: v for k, v in rule.items() if v is not None}
        else:
            raise HTTPException(status_code=400, detail="Invalid rule payload")

        incoming_data.pop("id", None)
        incoming_data.pop("created_at", None)

        merged_data = {**existing_data, **incoming_data}
        merged_data["id"] = rule_id
        merged_data["created_at"] = existing_rule.created_at
        merged_data["updated_at"] = _now_iso()

        updated_rule = _normalize_alert_rule(AlertRule(**merged_data))
        _alert_rules[rule_id] = updated_rule
        await _run_blocking(_persist_rule, updated_rule)

        logger.info(f"Updated alert rule: {updated_rule.name} (ID: {rule_id})")
        return {"status": "ok", "rule": updated_rule.dict()}
    except HTTPException:
        raise
    except Exception as e:
        logger.error(f"Error updating alert rule: {e}")
        raise HTTPException(status_code=500, detail="Internal server error")


async def delete_alert_rule(rule_id: str) -> Dict[str, Any]:
    """删除告警规则"""
    try:
        rule = _alert_rules.get(rule_id)
        if not rule:
            raise HTTPException(status_code=404, detail="Rule not found")

        del _alert_rules[rule_id]
        await _run_blocking(_persist_rule, rule, True)

        now_iso = _now_iso()
        for event in _alert_events:
            if event.rule_id != rule_id:
                continue
            if event.status not in ACTIVE_EVENT_STATUSES:
                continue
            if await _resolve_event(event, now_iso, reason="rule deleted"):
                await _notify_event(rule, event, transition="resolved:rule_deleted", force=True)

        logger.info(f"Deleted alert rule: {rule_id}")
        return {"status": "ok", "message": "Rule deleted"}
    except HTTPException:
        raise
    except Exception as e:
        logger.error(f"Error deleting alert rule: {e}")
        raise HTTPException(status_code=500, detail="Internal server error")


async def get_alert_rules() -> Dict[str, Any]:
    """获取所有告警规则"""
    try:
        rules = [rule.dict() for rule in _alert_rules.values()]
        rules.sort(key=lambda item: str(item.get("updated_at") or ""), reverse=True)
        return {"total": len(rules), "rules": rules}
    except Exception as e:
        logger.error(f"Error getting alert rules: {e}")
        raise HTTPException(status_code=500, detail="Internal server error")


async def get_alert_rule(rule_id: str) -> Dict[str, Any]:
    """获取单个告警规则"""
    try:
        if rule_id not in _alert_rules:
            raise HTTPException(status_code=404, detail="Rule not found")
        return {"rule": _alert_rules[rule_id].dict()}
    except HTTPException:
        raise
    except Exception as e:
        logger.error(f"Error getting alert rule: {e}")
        raise HTTPException(status_code=500, detail="Internal server error")


async def get_alert_rule_templates() -> Dict[str, Any]:
    """获取规则模板列表。"""
    templates = [item.dict() for item in _RULE_TEMPLATES]
    return {"total": len(templates), "templates": templates}


async def create_alert_rule_from_template(payload: CreateRuleFromTemplateRequest) -> Dict[str, Any]:
    """基于模板快速创建规则。"""
    template_id = str(payload.template_id or "").strip()
    template = next((item for item in _RULE_TEMPLATES if item.id == template_id), None)
    if not template:
        raise HTTPException(status_code=404, detail="Template not found")

    rule_data = template.dict()
    rule_data["name"] = str(payload.name or template.name).strip() or template.name
    rule_data["description"] = str(payload.description or template.description).strip() or template.description
    rule_data["service_name"] = str(payload.service_name or "").strip() or None
    rule_data["threshold"] = float(payload.threshold) if payload.threshold is not None else template.threshold
    rule_data["duration"] = int(payload.duration) if payload.duration is not None else template.duration
    rule_data["severity"] = str(payload.severity or template.severity or "warning")
    rule_data["labels"] = {**(template.labels or {}), **(payload.labels or {})}
    rule_data["min_occurrence_count"] = (
        max(1, int(payload.min_occurrence_count or 1))
        if payload.min_occurrence_count is not None
        else 1
    )
    rule_data["notification_enabled"] = (
        bool(payload.notification_enabled)
        if payload.notification_enabled is not None
        else True
    )
    rule_data["notification_channels"] = (
        _normalize_notification_channels(payload.notification_channels)
        if payload.notification_channels is not None
        else ["inapp"]
    )
    rule_data["notification_cooldown_seconds"] = (
        max(0, int(payload.notification_cooldown_seconds or 0))
        if payload.notification_cooldown_seconds is not None
        else 300
    )

    return await create_alert_rule(AlertRule(**rule_data))


async def get_alert_notifications(
    limit: int = 100,
    channel: Optional[str] = None,
    delivery_status: Optional[str] = None,
    event_id: Optional[str] = None,
) -> Dict[str, Any]:
    """获取告警通知记录（阶段 C）。"""
    limit = min(max(int(limit or 100), 1), 500)
    channel_filter = str(channel or "").strip().lower()
    status_filter = str(delivery_status or "").strip().lower()
    event_filter = str(event_id or "").strip()

    try:
        if _is_clickhouse_available() and _NOTIFICATION_TABLE_NAME:
            where_clauses: List[str] = []
            if channel_filter:
                where_clauses.append(f"channel = '{_escape_sql_literal(channel_filter)}'")
            if status_filter:
                where_clauses.append(f"delivery_status = '{_escape_sql_literal(status_filter)}'")
            if event_filter:
                where_clauses.append(f"event_id = '{_escape_sql_literal(event_filter)}'")

            where_sql = ""
            if where_clauses:
                where_sql = "WHERE " + " AND ".join(where_clauses)

            query = f"""
            SELECT
                notification_id, event_id, rule_id, rule_name, service_name,
                severity, event_status, channel, delivery_status, detail, created_at
            FROM {_NOTIFICATION_TABLE_NAME}
            {where_sql}
            ORDER BY created_at DESC
            LIMIT {limit}
            """
            rows = await _run_blocking(_STORAGE_ADAPTER.ch_client.execute, query)
            notifications = [
                {
                    "id": str(row[0] or ""),
                    "event_id": str(row[1] or ""),
                    "rule_id": str(row[2] or ""),
                    "rule_name": str(row[3] or ""),
                    "service_name": str(row[4] or ""),
                    "severity": str(row[5] or "info"),
                    "event_status": str(row[6] or ""),
                    "channel": str(row[7] or ""),
                    "delivery_status": str(row[8] or "unknown"),
                    "detail": str(row[9] or ""),
                    "created_at": _to_iso(row[10]),
                }
                for row in rows
                if row and len(row) >= 11
            ]
            return {"total": len(notifications), "notifications": notifications}

        notifications: List[Dict[str, Any]] = []
        for record in reversed(_alert_notifications):
            if channel_filter and str(record.get("channel", "")).lower() != channel_filter:
                continue
            if status_filter and str(record.get("delivery_status", "")).lower() != status_filter:
                continue
            if event_filter and str(record.get("event_id", "")) != event_filter:
                continue
            notifications.append(record)
            if len(notifications) >= limit:
                break

        return {"total": len(notifications), "notifications": notifications}
    except Exception as exc:
        logger.error(f"Error getting alert notifications: {exc}")
        raise HTTPException(status_code=500, detail="Internal server error")


async def get_alert_events(
    limit: int = 100,
    status: Optional[str] = None,
    severity: Optional[str] = None,
    cursor: Optional[str] = None,
    service_name: Optional[str] = None,
    search: Optional[str] = None,
) -> Dict[str, Any]:
    """获取告警事件列表（支持过滤与分页）。"""
    try:
        limit = min(max(int(limit or 50), 1), 500)
        statuses = _normalize_csv_filter(status)
        severities = _normalize_csv_filter(severity)
        service_filter = str(service_name or "").strip()
        search_filter = str(search or "").strip().lower()

        events = _deduplicate_events(_alert_events)

        filtered: List[AlertEvent] = []
        for event in events:
            if statuses and event.status not in statuses:
                continue
            if severities and event.severity not in severities:
                continue
            if service_filter and event.service_name != service_filter:
                continue
            if search_filter:
                haystack = f"{event.rule_name} {event.metric_name} {event.message}".lower()
                if search_filter not in haystack:
                    continue
            filtered.append(event)

        filtered.sort(key=_event_sort_key, reverse=True)
        total = len(filtered)

        start_index = 0
        if cursor:
            cursor_fired_at, cursor_event_id = _decode_cursor(cursor)
            cursor_key = (cursor_fired_at, cursor_event_id)
            start_index = total
            for idx, event in enumerate(filtered):
                if _event_sort_key(event) < cursor_key:
                    start_index = idx
                    break

        page_events = filtered[start_index:start_index + limit]
        has_more = (start_index + limit) < total
        next_cursor = _encode_cursor(page_events[-1]) if has_more and page_events else None

        return {
            "total": total,
            "events": [event.dict() for event in page_events],
            "limit": limit,
            "cursor": cursor,
            "has_more": has_more,
            "next_cursor": next_cursor,
        }
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    except Exception as e:
        logger.error(f"Error getting alert events: {e}")
        raise HTTPException(status_code=500, detail="Internal server error")


async def acknowledge_alert_event(event_id: str) -> Dict[str, Any]:
    """确认告警事件（acknowledged）。"""
    try:
        event = _find_event_by_id(event_id)
        if not event:
            raise HTTPException(status_code=404, detail="Alert event not found")
        if event.status == "resolved":
            raise HTTPException(status_code=400, detail="Resolved alert cannot be acknowledged")

        now_iso = _now_iso()
        event.status = "acknowledged"
        event.acknowledged_at = now_iso
        event.updated_at = now_iso
        await _run_blocking(_persist_event, event)
        await _notify_event(_find_rule_by_id(event.rule_id), event, transition="acknowledged", force=True)

        return {"status": "ok", "event": event.dict()}
    except HTTPException:
        raise
    except Exception as exc:
        logger.error(f"Error acknowledging alert event: {exc}")
        raise HTTPException(status_code=500, detail="Internal server error")


async def silence_alert_event(event_id: str, duration_seconds: int = 3600) -> Dict[str, Any]:
    """静默告警事件（silenced）。"""
    try:
        event = _find_event_by_id(event_id)
        if not event:
            raise HTTPException(status_code=404, detail="Alert event not found")
        if event.status == "resolved":
            raise HTTPException(status_code=400, detail="Resolved alert cannot be silenced")

        duration_seconds = max(1, int(duration_seconds or 3600))
        now_dt = _now_utc()
        event.status = "silenced"
        event.silenced_until = (now_dt + timedelta(seconds=duration_seconds)).isoformat()
        event.updated_at = now_dt.isoformat()
        await _run_blocking(_persist_event, event)
        await _notify_event(_find_rule_by_id(event.rule_id), event, transition="silenced", force=True)

        return {
            "status": "ok",
            "duration_seconds": duration_seconds,
            "event": event.dict(),
        }
    except HTTPException:
        raise
    except Exception as exc:
        logger.error(f"Error silencing alert event: {exc}")
        raise HTTPException(status_code=500, detail="Internal server error")


async def resolve_alert_event(event_id: str, reason: Optional[str] = None) -> Dict[str, Any]:
    """手工关闭告警事件（resolved）。"""
    try:
        event = _find_event_by_id(event_id)
        if not event:
            raise HTTPException(status_code=404, detail="Alert event not found")

        now_iso = _now_iso()
        changed = await _resolve_event(event, now_iso, reason=str(reason or "").strip())
        if changed:
            await _notify_event(_find_rule_by_id(event.rule_id), event, transition="resolved:manual", force=True)
        return {
            "status": "ok",
            "updated": changed,
            "event": event.dict(),
        }
    except HTTPException:
        raise
    except Exception as exc:
        logger.error(f"Error resolving alert event: {exc}")
        raise HTTPException(status_code=500, detail="Internal server error")


async def evaluate_alert_rules() -> Dict[str, Any]:
    """
    评估告警规则。

    状态机：pending -> firing -> acknowledged/silenced -> resolved
    """
    try:
        if not _STORAGE_ADAPTER:
            raise HTTPException(status_code=503, detail="Storage adapter not initialized")

        triggered_count = 0
        resolved_count = 0

        metrics = await _run_blocking(_STORAGE_ADAPTER.get_metrics, 1000)

        metrics_by_key: Dict[str, List[float]] = {}
        for metric in metrics:
            service_name = str(metric.get("service_name") or "").strip()
            metric_name = str(metric.get("metric_name") or "").strip()
            if not service_name or not metric_name:
                continue
            key = f"{service_name}:{metric_name}"
            metrics_by_key.setdefault(key, []).append(float(metric.get("value") or 0.0))

        avg_metrics: Dict[str, float] = {}
        for key, values in metrics_by_key.items():
            if values:
                avg_metrics[key] = sum(values) / len(values)

        _alert_events[:] = _deduplicate_events(_alert_events)

        active_events_by_fingerprint: Dict[str, AlertEvent] = {}
        for event in _alert_events:
            if event.status not in ACTIVE_EVENT_STATUSES:
                continue
            fp = _event_fingerprint(event.rule_id, event.service_name, event.metric_name)
            active_events_by_fingerprint[fp] = event

        enabled_rule_ids = {rule.id for rule in _alert_rules.values() if rule.enabled and rule.id}
        evaluated_fingerprints: Set[str] = set()
        triggered_fingerprints: Set[str] = set()

        now_dt = _now_utc()
        now_iso = now_dt.isoformat()

        for rule in _alert_rules.values():
            if not rule.enabled:
                continue
            rule = _normalize_alert_rule(rule)

            for key, avg_value in avg_metrics.items():
                service_name, metric_name = key.split(":", 1)

                if rule.metric_name != metric_name:
                    continue
                if rule.service_name and rule.service_name != service_name:
                    continue

                fp = _event_fingerprint(str(rule.id), service_name, metric_name)
                evaluated_fingerprints.add(fp)

                if not _evaluate_condition(rule, avg_value):
                    continue

                triggered_fingerprints.add(fp)
                existing_event = active_events_by_fingerprint.get(fp)

                if existing_event:
                    existing_event.current_value = avg_value
                    existing_event.message = (
                        f"{metric_name} is {avg_value:.2f}, threshold {rule.condition} {rule.threshold}"
                    )
                    existing_event.labels = rule.labels
                    existing_event.last_triggered_at = now_iso
                    existing_event.updated_at = now_iso
                    existing_event.occurrence_count = max(1, int(existing_event.occurrence_count or 1)) + 1

                    if existing_event.status == "silenced":
                        silenced_until_dt = _to_datetime(existing_event.silenced_until, default=now_dt)
                        if silenced_until_dt <= now_dt:
                            if _is_rule_ready_to_fire(rule, existing_event, now_dt):
                                existing_event.status = "firing"
                                triggered_count += 1
                                await _notify_event(rule, existing_event, transition="firing:reactivated")
                            else:
                                existing_event.status = "pending"
                    elif existing_event.status == "pending":
                        if _is_rule_ready_to_fire(rule, existing_event, now_dt):
                            existing_event.status = "firing"
                            triggered_count += 1
                            await _notify_event(rule, existing_event, transition="firing:threshold_met")
                    elif existing_event.status == "firing":
                        # 仍在触发时按 cooldown 周期发送通知，避免噪声风暴
                        await _notify_event(rule, existing_event, transition="firing:heartbeat")

                    await _run_blocking(_persist_event, existing_event)
                    continue

                event = AlertEvent(
                    id=str(uuid.uuid4()),
                    rule_id=str(rule.id),
                    rule_name=rule.name,
                    metric_name=metric_name,
                    service_name=service_name,
                    current_value=avg_value,
                    threshold=rule.threshold,
                    condition=rule.condition,
                    severity=rule.severity,
                    message=f"{metric_name} is {avg_value:.2f}, threshold {rule.condition} {rule.threshold}",
                    status="pending",
                    fired_at=now_iso,
                    first_triggered_at=now_iso,
                    last_triggered_at=now_iso,
                    occurrence_count=1,
                    updated_at=now_iso,
                    labels=rule.labels,
                )

                _alert_events.append(event)
                active_events_by_fingerprint[fp] = event

                if _is_rule_ready_to_fire(rule, event, now_dt):
                    event.status = "firing"
                    triggered_count += 1
                    await _notify_event(rule, event, transition="firing:new")
                else:
                    event.status = "pending"

                await _run_blocking(_persist_event, event)

                if event.status == "firing":
                    logger.warning(
                        "Alert triggered: %s - %s:%s = %.2f",
                        rule.name,
                        service_name,
                        metric_name,
                        avg_value,
                    )

        for fp in evaluated_fingerprints:
            if fp in triggered_fingerprints:
                continue
            active_event = active_events_by_fingerprint.get(fp)
            if not active_event:
                continue
            if active_event.status not in ACTIVE_EVENT_STATUSES:
                continue
            if await _resolve_event(active_event, now_iso):
                resolved_count += 1
                await _notify_event(_find_rule_by_id(active_event.rule_id), active_event, transition="resolved:auto")

        for event in _alert_events:
            if event.status not in ACTIVE_EVENT_STATUSES:
                continue
            if event.rule_id not in enabled_rule_ids:
                if await _resolve_event(event, now_iso, reason="rule disabled"):
                    resolved_count += 1
                    await _notify_event(_find_rule_by_id(event.rule_id), event, transition="resolved:rule_disabled")

        return {
            "status": "ok",
            "evaluated_rules": len(_alert_rules),
            "triggered_alerts": triggered_count,
            "resolved_alerts": resolved_count,
        }

    except HTTPException:
        raise
    except Exception as e:
        logger.error(f"Error evaluating alert rules: {e}")
        raise HTTPException(status_code=500, detail="Internal server error")


async def get_alert_stats() -> Dict[str, Any]:
    """获取告警统计信息。"""
    try:
        _alert_events[:] = _deduplicate_events(_alert_events)

        total_events = len(_alert_events)
        firing_events = len([e for e in _alert_events if e.status == "firing"])
        pending_events = len([e for e in _alert_events if e.status == "pending"])
        acknowledged_events = len([e for e in _alert_events if e.status == "acknowledged"])
        silenced_events = len([e for e in _alert_events if e.status == "silenced"])
        resolved_events = len([e for e in _alert_events if e.status == "resolved"])

        severity_stats: Dict[str, int] = {}
        for event in _alert_events:
            sev = event.severity
            severity_stats[sev] = severity_stats.get(sev, 0) + 1

        return {
            "total_rules": len(_alert_rules),
            "enabled_rules": len([r for r in _alert_rules.values() if r.enabled]),
            "total_events": total_events,
            "total_notifications": len(_alert_notifications),
            "pending_events": pending_events,
            "firing_events": firing_events,
            "acknowledged_events": acknowledged_events,
            "silenced_events": silenced_events,
            "resolved_events": resolved_events,
            "severity_stats": severity_stats,
            # 前端兼容字段
            "firing": firing_events,
            "resolved": resolved_events,
            "pending": pending_events,
            "acknowledged": acknowledged_events,
            "silenced": silenced_events,
            "critical": severity_stats.get("critical", 0),
            "warning": severity_stats.get("warning", 0),
            "info": severity_stats.get("info", 0),
            "by_severity": severity_stats,
        }
    except Exception as e:
        logger.error(f"Error getting alert stats: {e}")
        raise HTTPException(status_code=500, detail="Internal server error")
