"""
WebSocket 实时日志流端点

提供实时日志推送功能，支持：
- WebSocket 连接管理
- ClickHouse 轮询推送
- 日志过滤和广播
"""
import logging
import json
import asyncio
import os
import re
from typing import Set, Optional, Dict, Any
from fastapi import WebSocket, WebSocketDisconnect
from datetime import datetime, timezone

logger = logging.getLogger(__name__)

WS_BROADCAST_BATCH_SIZE = max(1, int(os.getenv("WS_BROADCAST_BATCH_SIZE", "32")))
WS_SEND_TIMEOUT_SECONDS = max(0.1, float(os.getenv("WS_SEND_TIMEOUT_SECONDS", "1.0")))
POLL_QUERY_TIMEOUT_SECONDS = max(0.1, float(os.getenv("WS_POLL_QUERY_TIMEOUT_SECONDS", "5.0")))
HEALTH_CHECK_REGEX_PATTERNS = [
    r"(?i)\bkube-probe\b",
    r'(?i)"(?:GET|HEAD)\s+/health(?:z)?(?:\?[^"\s]*)?\s+HTTP/1\.[01]"',
    r'(?i)"(?:GET|HEAD)\s+/(?:ready|readiness|live|liveness)(?:\?[^"\s]*)?\s+HTTP/1\.[01]"',
    r"(?i)\b(?:readiness|liveness)[\s_-]*probe\b",
]
HEALTH_CHECK_REGEXES = [re.compile(pattern) for pattern in HEALTH_CHECK_REGEX_PATTERNS]
POD_SUFFIX_PATTERNS = [
    re.compile(r"^(.+)-[a-f0-9]{8,10}-[a-z0-9]{5,10}$", re.IGNORECASE),
    re.compile(r"^(.+)-[a-f0-9]{8,10}(?:-[a-f0-9]{4,8})?$", re.IGNORECASE),
    re.compile(r"^(.+)-[a-z0-9]{5}$", re.IGNORECASE),
    re.compile(r"^(.+)-\d+$"),
]


def _utc_now_iso() -> str:
    """Return timezone-aware UTC timestamp in ISO 8601 format."""
    return datetime.now(timezone.utc).isoformat()


def _utc_now_sql_cursor() -> str:
    """Return UTC timestamp text used by ClickHouse cursor comparisons."""
    return datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S")


def _normalize_text(value: Any) -> str:
    text = str(value or "").strip()
    if not text:
        return ""
    if text.lower() == "unknown":
        return ""
    return text


def _normalize_level(value: Any) -> str:
    text = str(value or "").strip().upper()
    if not text:
        return ""
    if text == "WARNING":
        return "WARN"
    if text in {"TRACE", "DEBUG", "INFO", "WARN", "ERROR", "FATAL"}:
        return text
    return ""


def _derive_service_from_pod(value: Any) -> str:
    pod_name = _normalize_text(value)
    if not pod_name:
        return ""
    for pattern in POD_SUFFIX_PATTERNS:
        matched = pattern.match(pod_name)
        if matched and matched.group(1):
            return matched.group(1)
    return pod_name


def _looks_like_pod_name(value: Any) -> bool:
    text = _normalize_text(value)
    if not text:
        return False
    return any(pattern.match(text) for pattern in POD_SUFFIX_PATTERNS)


def _resolve_service_name(service_name: Any, pod_name: Any = None) -> str:
    service = _normalize_text(service_name)
    pod = _normalize_text(pod_name)
    if service:
        if service == pod or _looks_like_pod_name(service):
            return _derive_service_from_pod(service)
        return service
    if pod:
        return _derive_service_from_pod(pod)
    return "unknown"


def _is_health_check_message(value: Any) -> bool:
    text = str(value or "")
    if not text:
        return False
    return any(pattern.search(text) for pattern in HEALTH_CHECK_REGEXES)


class ConnectionManager:
    """WebSocket 连接管理器"""
    
    def __init__(self):
        self.active_connections: Set[WebSocket] = set()
        self._connection_channels: Dict[WebSocket, str] = {}
        self._connection_filters: Dict[WebSocket, Dict[str, Any]] = {}

    async def _send_with_timeout(self, websocket: WebSocket, message_json: str) -> bool:
        try:
            await asyncio.wait_for(websocket.send_text(message_json), timeout=WS_SEND_TIMEOUT_SECONDS)
            return True
        except Exception as e:
            logger.warning(f"Failed to send message: {e}")
            return False
    
    async def connect(self, websocket: WebSocket, channel: str = "logs"):
        """接受新的 WebSocket 连接"""
        await websocket.accept()
        self.active_connections.add(websocket)
        self._connection_channels[websocket] = channel
        self._connection_filters[websocket] = {}
        logger.info(
            "WebSocket connected. Channel=%s Total connections: %s",
            channel,
            len(self.active_connections),
        )

    def disconnect(self, websocket: WebSocket):
        """断开 WebSocket 连接"""
        self.active_connections.discard(websocket)
        self._connection_channels.pop(websocket, None)
        self._connection_filters.pop(websocket, None)
        logger.info("WebSocket disconnected. Total connections: %s", len(self.active_connections))

    def has_connections(self, channel: Optional[str] = None) -> bool:
        """检查是否存在连接（可按 channel 过滤）"""
        if channel is None:
            return bool(self.active_connections)
        for conn in self.active_connections:
            if self._connection_channels.get(conn) == channel:
                return True
        return False

    def set_filters(self, websocket: WebSocket, filters: Optional[Dict[str, Any]] = None):
        """设置连接的日志过滤条件"""
        if websocket not in self.active_connections:
            return
        raw_filters = filters or {}
        normalized: Dict[str, Any] = {}

        service_name = _normalize_text(raw_filters.get("service_name"))
        if service_name:
            normalized["service_name"] = _resolve_service_name(service_name).lower()

        namespace = _normalize_text(raw_filters.get("namespace"))
        if namespace:
            normalized["namespace"] = namespace.lower()

        level = _normalize_level(raw_filters.get("level"))
        if level:
            normalized["level"] = level

        if bool(raw_filters.get("exclude_health_check")):
            normalized["exclude_health_check"] = True

        self._connection_filters[websocket] = normalized

    def clear_filters(self, websocket: WebSocket):
        """清空连接过滤条件"""
        if websocket in self.active_connections:
            self._connection_filters[websocket] = {}

    def get_filters(self, websocket: WebSocket) -> Dict[str, Any]:
        """获取连接过滤条件（用于回执）"""
        return dict(self._connection_filters.get(websocket) or {})

    def _matches_log_filters(self, websocket: WebSocket, log_data: Dict[str, Any]) -> bool:
        filters = self._connection_filters.get(websocket) or {}
        if not filters:
            return True

        if "service_name" in filters:
            resolved_service = _resolve_service_name(
                log_data.get("service_name"),
                log_data.get("pod_name"),
            ).lower()
            if resolved_service != filters["service_name"]:
                return False

        if "namespace" in filters:
            namespace = _normalize_text(log_data.get("namespace")).lower()
            if namespace != filters["namespace"]:
                return False

        if "level" in filters:
            level = _normalize_level(log_data.get("level"))
            if level != filters["level"]:
                return False

        if filters.get("exclude_health_check"):
            if _is_health_check_message(log_data.get("message")):
                return False

        return True

    async def broadcast(self, message: Dict[str, Any], channel: Optional[str] = None):
        """向所有连接广播消息"""
        if not self.active_connections:
            return

        target_connections = [
            connection
            for connection in self.active_connections
            if channel is None or self._connection_channels.get(connection) == channel
        ]
        if not target_connections:
            return

        log_data = None
        if message.get("type") == "log" and isinstance(message.get("data"), dict):
            log_data = message.get("data")
            target_connections = [
                connection
                for connection in target_connections
                if self._matches_log_filters(connection, log_data)
            ]
            if not target_connections:
                return

        message_json = json.dumps(message, ensure_ascii=False, default=str)
        disconnected = set()

        connections = list(target_connections)
        for offset in range(0, len(connections), WS_BROADCAST_BATCH_SIZE):
            batch = connections[offset: offset + WS_BROADCAST_BATCH_SIZE]
            results = await asyncio.gather(
                *(self._send_with_timeout(connection, message_json) for connection in batch)
            )
            for connection, ok in zip(batch, results):
                if not ok:
                    disconnected.add(connection)

        for conn in disconnected:
            self.disconnect(conn)
    
    async def send_to(self, websocket: WebSocket, message: Dict[str, Any]):
        """向指定连接发送消息"""
        try:
            message_json = json.dumps(message, ensure_ascii=False, default=str)
            await asyncio.wait_for(websocket.send_text(message_json), timeout=WS_SEND_TIMEOUT_SECONDS)
        except Exception as e:
            logger.warning(f"Failed to send message: {e}")
            self.disconnect(websocket)


manager = ConnectionManager()


async def poll_logs_from_clickhouse(
    storage_adapter,
    last_timestamp: str,
    last_id: Optional[str] = None,
    filters: Dict[str, Any] = None,
):
    """从 ClickHouse 轮询新日志。"""
    try:
        params: Dict[str, Any] = {"last_timestamp": last_timestamp}
        if last_id is None:
            prewhere_conditions = ["timestamp > toDateTime64({last_timestamp:String}, 9, 'UTC')"]
        else:
            params["last_id"] = str(last_id)
            prewhere_conditions = [
                "("
                "timestamp > toDateTime64({last_timestamp:String}, 9, 'UTC') OR "
                "(timestamp = toDateTime64({last_timestamp:String}, 9, 'UTC') AND id > {last_id:String})"
                ")"
            ]
        where_conditions = []
        
        if filters:
            if filters.get("service_name"):
                prewhere_conditions.append("service_name = {service_name:String}")
                params["service_name"] = filters["service_name"]
            if filters.get("level"):
                level_value = str(filters["level"]).strip().upper()
                if level_value == "WARNING":
                    level_value = "WARN"
                prewhere_conditions.append("level_norm = {level:String}")
                params["level"] = level_value
            if filters.get("exclude_health_check"):
                health_conditions = []
                for idx, pattern in enumerate(HEALTH_CHECK_REGEX_PATTERNS):
                    key = f"health_keyword_{idx}"
                    params[key] = pattern
                    health_conditions.append(
                        f"NOT match(message, {{{key}:String}})"
                    )
                where_conditions.append(f"({' AND '.join(health_conditions)})")
        
        prewhere_clause = f"PREWHERE {' AND '.join(prewhere_conditions)}"
        where_clause = f"WHERE {' AND '.join(where_conditions)}" if where_conditions else ""
        
        query = f"""
        SELECT 
            id, timestamp, service_name, level, message,
            pod_name, namespace, node_name, container_name,
            trace_id, span_id, labels,
            JSONExtractRaw(attributes_json, 'log_meta') AS log_meta
        FROM logs.logs
        {prewhere_clause}
        {where_clause}
        ORDER BY timestamp ASC, id ASC
        LIMIT 100
        """
        
        return await asyncio.wait_for(
            asyncio.to_thread(storage_adapter.execute_query, query, params),
            timeout=POLL_QUERY_TIMEOUT_SECONDS,
        )
    except asyncio.TimeoutError:
        logger.warning(
            "Polling logs query timed out after %.2fs; skip current round",
            POLL_QUERY_TIMEOUT_SECONDS,
        )
        return []
    except Exception as e:
        logger.error(f"Error polling logs: {e}")
        return []


async def log_poller(storage_adapter, interval: float = 2.0):
    """定时轮询新日志并广播。"""
    last_timestamp = _utc_now_sql_cursor()
    last_id: Optional[str] = None
    
    while True:
        try:
            if manager.has_connections(channel="logs"):
                new_logs = await poll_logs_from_clickhouse(storage_adapter, last_timestamp, last_id)
                
                if new_logs:
                    newest = new_logs[-1]
                    if isinstance(newest, dict):
                        latest_ts = newest.get("timestamp")
                        latest_id = newest.get("id")
                        if latest_ts is not None:
                            last_timestamp = str(latest_ts)
                            last_id = "" if latest_id is None else str(latest_id)
                    
                    for log in new_logs:
                        await manager.broadcast({
                            "type": "log",
                            "data": log,
                            "timestamp": _utc_now_iso()
                        }, channel="logs")
            else:
                # 无日志订阅者时推进游标，避免用户首次订阅时回放大量历史日志
                last_timestamp = _utc_now_sql_cursor()
                last_id = None
            
            await asyncio.sleep(interval)
            
        except asyncio.CancelledError:
            break
        except Exception as e:
            logger.error(f"Error in log poller: {e}")
            await asyncio.sleep(interval)


async def websocket_logs_endpoint(websocket: WebSocket):
    """
    WebSocket 日志流端点
    
    客户端可以发送以下消息：
    - {"action": "subscribe", "filters": {...}} - 订阅日志流（带过滤条件）
    - {"action": "unsubscribe"} - 取消订阅
    - {"action": "ping"} - 心跳检测
    """
    await manager.connect(websocket, channel="logs")
    
    try:
        await manager.send_to(websocket, {
            "type": "connected",
            "message": "WebSocket connected successfully",
            "timestamp": _utc_now_iso()
        })
        
        while True:
            try:
                data = await asyncio.wait_for(
                    websocket.receive_text(),
                    timeout=30.0
                )
                
                try:
                    message = json.loads(data)
                    action = message.get("action")
                    
                    if action == "ping":
                        await manager.send_to(websocket, {
                            "type": "pong",
                            "timestamp": _utc_now_iso()
                        })
                    
                    elif action == "subscribe":
                        filters = message.get("filters", {})
                        manager.set_filters(websocket, filters)
                        await manager.send_to(websocket, {
                            "type": "subscribed",
                            "filters": manager.get_filters(websocket),
                            "timestamp": _utc_now_iso()
                        })
                    
                    elif action == "unsubscribe":
                        manager.clear_filters(websocket)
                        await manager.send_to(websocket, {
                            "type": "unsubscribed",
                            "timestamp": _utc_now_iso()
                        })
                    
                except json.JSONDecodeError:
                    await manager.send_to(websocket, {
                        "type": "error",
                        "message": "Invalid JSON format",
                        "timestamp": _utc_now_iso()
                    })
                    
            except asyncio.TimeoutError:
                await manager.send_to(websocket, {
                    "type": "ping",
                    "timestamp": _utc_now_iso()
                })
                
    except WebSocketDisconnect:
        manager.disconnect(websocket)
    except Exception as e:
        logger.error(f"WebSocket error: {e}")
        manager.disconnect(websocket)


async def websocket_topology_endpoint(websocket: WebSocket):
    """
    WebSocket 拓扑更新端点
    
    推送拓扑变化事件
    """
    await manager.connect(websocket, channel="topology")
    
    try:
        await manager.send_to(websocket, {
            "type": "connected",
            "message": "Topology WebSocket connected",
            "timestamp": _utc_now_iso()
        })
        
        while True:
            try:
                data = await asyncio.wait_for(
                    websocket.receive_text(),
                    timeout=60.0
                )
                
                message = json.loads(data)
                
                if message.get("action") == "ping":
                    await manager.send_to(websocket, {
                        "type": "pong",
                        "timestamp": _utc_now_iso()
                    })
                    
            except asyncio.TimeoutError:
                await manager.send_to(websocket, {
                    "type": "ping",
                    "timestamp": _utc_now_iso()
                })
                
    except WebSocketDisconnect:
        manager.disconnect(websocket)
    except Exception as e:
        logger.error(f"Topology WebSocket error: {e}")
        manager.disconnect(websocket)


async def broadcast_topology_update(topology_data: Dict[str, Any]):
    """广播拓扑更新"""
    await manager.broadcast({
        "type": "topology_update",
        "data": topology_data,
        "timestamp": _utc_now_iso()
    }, channel="topology")


async def broadcast_alert(alert_data: Dict[str, Any]):
    """广播告警"""
    await manager.broadcast({
        "type": "alert",
        "data": alert_data,
        "timestamp": _utc_now_iso()
    }, channel="logs")
