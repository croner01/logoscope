"""
WebSocket 实时日志流端点

提供实时日志推送功能，支持：
- WebSocket 连接管理
- Redis Pub/Sub 订阅
- 日志过滤和推送
"""
import logging
import json
import asyncio
import os
from typing import Set, Optional, Dict, Any
from fastapi import WebSocket, WebSocketDisconnect
from datetime import datetime

logger = logging.getLogger(__name__)

active_connections: Set[WebSocket] = set()
WS_BROADCAST_BATCH_SIZE = max(1, int(os.getenv("WS_BROADCAST_BATCH_SIZE", "32")))
WS_SEND_TIMEOUT_SECONDS = max(0.1, float(os.getenv("WS_SEND_TIMEOUT_SECONDS", "1.0")))
HEALTH_CHECK_REGEX_PATTERNS = [
    r"(?i)\bkube-probe\b",
    r'(?i)"(?:GET|HEAD)\s+/health(?:z)?(?:\?[^"\s]*)?\s+HTTP/1\.[01]"',
    r'(?i)"(?:GET|HEAD)\s+/(?:ready|readiness|live|liveness)(?:\?[^"\s]*)?\s+HTTP/1\.[01]"',
    r"(?i)\b(?:readiness|liveness)[\s_-]*probe\b",
]


class ConnectionManager:
    """WebSocket 连接管理器"""
    
    def __init__(self):
        self.active_connections: Set[WebSocket] = set()
        self._redis_client = None
        self._pubsub_task = None

    async def _send_with_timeout(self, websocket: WebSocket, message_json: str) -> bool:
        try:
            await asyncio.wait_for(websocket.send_text(message_json), timeout=WS_SEND_TIMEOUT_SECONDS)
            return True
        except Exception as e:
            logger.warning(f"Failed to send message: {e}")
            return False
    
    async def connect(self, websocket: WebSocket):
        """接受新的 WebSocket 连接"""
        await websocket.accept()
        self.active_connections.add(websocket)
        logger.info(f"WebSocket connected. Total connections: {len(self.active_connections)}")
    
    def disconnect(self, websocket: WebSocket):
        """断开 WebSocket 连接"""
        self.active_connections.discard(websocket)
        logger.info(f"WebSocket disconnected. Total connections: {len(self.active_connections)}")
    
    async def broadcast(self, message: Dict[str, Any]):
        """向所有连接广播消息"""
        if not self.active_connections:
            return

        message_json = json.dumps(message, ensure_ascii=False, default=str)
        disconnected = set()

        connections = list(self.active_connections)
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
            await websocket.send_text(message_json)
        except Exception as e:
            logger.warning(f"Failed to send message: {e}")
            self.disconnect(websocket)


manager = ConnectionManager()


async def get_redis_client():
    """获取 Redis 客户端（懒加载）"""
    if manager._redis_client is None:
        try:
            import redis.asyncio as redis
            from config import settings
            
            manager._redis_client = redis.Redis(
                host=settings.redis_host,
                port=settings.redis_port,
                db=settings.redis_db,
                password=settings.redis_password,
                decode_responses=True
            )
            logger.info("Redis client initialized for WebSocket")
        except Exception as e:
            logger.error(f"Failed to initialize Redis client: {e}")
            return None
    
    return manager._redis_client


async def redis_pubsub_listener():
    """监听 Redis Pub/Sub 频道并广播日志"""
    redis_client = await get_redis_client()
    if not redis_client:
        logger.warning("Redis client not available, pub/sub disabled")
        return
    
    try:
        pubsub = redis_client.pubsub()
        await pubsub.subscribe("logs:realtime")
        logger.info("Subscribed to Redis channel: logs:realtime")
        
        async for message in pubsub.listen():
            if message["type"] == "message":
                try:
                    log_data = json.loads(message["data"])
                    await manager.broadcast({
                        "type": "log",
                        "data": log_data,
                        "timestamp": datetime.utcnow().isoformat()
                    })
                except json.JSONDecodeError as e:
                    logger.warning(f"Failed to parse log data: {e}")
                except Exception as e:
                    logger.error(f"Error processing log message: {e}")
    
    except asyncio.CancelledError:
        logger.info("Pub/Sub listener cancelled")
    except Exception as e:
        logger.error(f"Pub/Sub listener error: {e}")
    finally:
        try:
            await pubsub.unsubscribe("logs:realtime")
            await pubsub.close()
        except:
            pass


async def poll_logs_from_clickhouse(storage_adapter, last_timestamp: str, filters: Dict[str, Any] = None):
    """从 ClickHouse 轮询新日志（备用方案）"""
    try:
        prewhere_conditions = ["timestamp > toDateTime64({last_timestamp:String}, 9)"]
        where_conditions = []
        params: Dict[str, Any] = {"last_timestamp": last_timestamp}
        
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
            trace_id, span_id, labels
        FROM logs.logs
        {prewhere_clause}
        {where_clause}
        ORDER BY timestamp ASC
        LIMIT 100
        """
        
        results = await asyncio.to_thread(storage_adapter.execute_query, query, params)
        return results
    except Exception as e:
        logger.error(f"Error polling logs: {e}")
        return []


async def log_poller(storage_adapter, interval: float = 2.0):
    """定时轮询新日志并广播（备用方案）"""
    last_timestamp = datetime.utcnow().strftime('%Y-%m-%d %H:%M:%S')
    
    while True:
        try:
            if manager.active_connections:
                new_logs = await poll_logs_from_clickhouse(storage_adapter, last_timestamp)
                
                if new_logs:
                    last_timestamp = new_logs[-1].get('timestamp', last_timestamp)
                    
                    for log in new_logs:
                        await manager.broadcast({
                            "type": "log",
                            "data": log,
                            "timestamp": datetime.utcnow().isoformat()
                        })
            
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
    await manager.connect(websocket)
    
    try:
        await manager.send_to(websocket, {
            "type": "connected",
            "message": "WebSocket connected successfully",
            "timestamp": datetime.utcnow().isoformat()
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
                            "timestamp": datetime.utcnow().isoformat()
                        })
                    
                    elif action == "subscribe":
                        filters = message.get("filters", {})
                        await manager.send_to(websocket, {
                            "type": "subscribed",
                            "filters": filters,
                            "timestamp": datetime.utcnow().isoformat()
                        })
                    
                    elif action == "unsubscribe":
                        await manager.send_to(websocket, {
                            "type": "unsubscribed",
                            "timestamp": datetime.utcnow().isoformat()
                        })
                    
                except json.JSONDecodeError:
                    await manager.send_to(websocket, {
                        "type": "error",
                        "message": "Invalid JSON format",
                        "timestamp": datetime.utcnow().isoformat()
                    })
                    
            except asyncio.TimeoutError:
                await manager.send_to(websocket, {
                    "type": "ping",
                    "timestamp": datetime.utcnow().isoformat()
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
    await manager.connect(websocket)
    
    try:
        await manager.send_to(websocket, {
            "type": "connected",
            "message": "Topology WebSocket connected",
            "timestamp": datetime.utcnow().isoformat()
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
                        "timestamp": datetime.utcnow().isoformat()
                    })
                    
            except asyncio.TimeoutError:
                await manager.send_to(websocket, {
                    "type": "ping",
                    "timestamp": datetime.utcnow().isoformat()
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
        "timestamp": datetime.utcnow().isoformat()
    })


async def broadcast_alert(alert_data: Dict[str, Any]):
    """广播告警"""
    await manager.broadcast({
        "type": "alert",
        "data": alert_data,
        "timestamp": datetime.utcnow().isoformat()
    })
