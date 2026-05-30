"""
Topology Service WebSocket - 实时拓扑推送

提供拓扑实时更新功能：
- WebSocket 连接管理
- 拓扑变化检测
- 定时推送更新
"""
import logging
import json
import asyncio
import os
from typing import Set, Dict, Any, Optional
from fastapi import WebSocket, WebSocketDisconnect
from datetime import datetime, timezone
import hashlib

from api.topology_build_coordinator import build_hybrid_topology_coalesced

logger = logging.getLogger(__name__)
WS_BROADCAST_BATCH_SIZE = max(1, int(os.getenv("WS_BROADCAST_BATCH_SIZE", "32")))
WS_SEND_TIMEOUT_SECONDS = max(0.1, float(os.getenv("WS_SEND_TIMEOUT_SECONDS", "1.0")))


def _utc_now_iso() -> str:
    """Return timezone-aware UTC timestamp in ISO 8601 format."""
    return datetime.now(timezone.utc).isoformat()


class TopologyConnectionManager:
    """拓扑 WebSocket 连接管理器"""

    def __init__(self):
        self.active_connections: Set[WebSocket] = set()
        self._subscriptions: Dict[WebSocket, Dict[str, Any]] = {}
        self._last_topology_hash_by_key: Dict[str, str] = {}
        self._last_topology_data_by_key: Dict[str, Dict[str, Any]] = {}

    def _normalize_subscription(self, params: Optional[Dict[str, Any]] = None) -> Dict[str, Any]:
        payload = params or {}
        time_window = str(payload.get("time_window") or "1 HOUR").strip() or "1 HOUR"
        namespace = payload.get("namespace")
        namespace = str(namespace).strip() if namespace is not None else None
        if namespace == "":
            namespace = None
        try:
            confidence_threshold = float(payload.get("confidence_threshold", 0.3))
        except (TypeError, ValueError):
            confidence_threshold = 0.3
        confidence_threshold = max(0.0, min(1.0, confidence_threshold))
        inference_mode = str(payload.get("inference_mode") or "rule").strip().lower()
        if inference_mode not in {"rule", "hybrid_score"}:
            inference_mode = "rule"
        message_target_enabled = payload.get("message_target_enabled")
        if message_target_enabled is None:
            normalized_message_target_enabled = None
        else:
            normalized_message_target_enabled = str(message_target_enabled).strip().lower() in {"1", "true", "yes", "on"}

        raw_patterns = payload.get("message_target_patterns")
        if isinstance(raw_patterns, list):
            pattern_text = ",".join(str(item) for item in raw_patterns)
        else:
            pattern_text = str(raw_patterns or "")
        allowed_patterns = {"url", "kv", "proxy", "rpc"}
        pattern_tokens = [token.strip().lower() for token in pattern_text.split(",") if token.strip()]
        normalized_patterns = sorted({token for token in pattern_tokens if token in allowed_patterns})
        if not normalized_patterns:
            normalized_patterns = None

        min_support = payload.get("message_target_min_support")
        try:
            normalized_min_support = int(min_support) if min_support is not None else None
        except (TypeError, ValueError):
            normalized_min_support = None
        if normalized_min_support is not None:
            normalized_min_support = max(1, min(20, normalized_min_support))

        max_per_log = payload.get("message_target_max_per_log")
        try:
            normalized_max_per_log = int(max_per_log) if max_per_log is not None else None
        except (TypeError, ValueError):
            normalized_max_per_log = None
        if normalized_max_per_log is not None:
            normalized_max_per_log = max(1, min(12, normalized_max_per_log))
        return {
            "time_window": time_window,
            "namespace": namespace,
            "confidence_threshold": confidence_threshold,
            "inference_mode": inference_mode,
            "message_target_enabled": normalized_message_target_enabled,
            "message_target_patterns": normalized_patterns,
            "message_target_min_support": normalized_min_support,
            "message_target_max_per_log": normalized_max_per_log,
        }

    def _subscription_key(self, params: Optional[Dict[str, Any]] = None) -> str:
        normalized = self._normalize_subscription(params)
        namespace = normalized["namespace"] or "*"
        threshold = f"{normalized['confidence_threshold']:.3f}"
        inference_mode = normalized.get("inference_mode", "rule")
        enabled = "default" if normalized.get("message_target_enabled") is None else str(bool(normalized.get("message_target_enabled"))).lower()
        patterns = ",".join(normalized.get("message_target_patterns") or ["default"])
        min_support = normalized.get("message_target_min_support")
        max_per_log = normalized.get("message_target_max_per_log")
        min_text = "default" if min_support is None else str(min_support)
        max_text = "default" if max_per_log is None else str(max_per_log)
        return f"{normalized['time_window']}|{namespace}|{threshold}|{inference_mode}|{enabled}|{patterns}|{min_text}|{max_text}"

    def _cleanup_stale_topology_cache_keys(self) -> None:
        """Remove cached topology entries that no active subscription references."""
        active_keys = {
            self._subscription_key(params)
            for params in self._subscriptions.values()
        }
        stale_keys = [
            key for key in self._last_topology_hash_by_key.keys()
            if key not in active_keys
        ]
        for key in stale_keys:
            self._last_topology_hash_by_key.pop(key, None)
            self._last_topology_data_by_key.pop(key, None)

    async def connect(self, websocket: WebSocket):
        """接受新的 WebSocket 连接"""
        await websocket.accept()
        self.active_connections.add(websocket)
        self._subscriptions[websocket] = self._normalize_subscription()
        logger.info(f"Topology WebSocket connected. Total: {len(self.active_connections)}")

    def disconnect(self, websocket: WebSocket):
        """断开 WebSocket 连接"""
        self.active_connections.discard(websocket)
        self._subscriptions.pop(websocket, None)
        self._cleanup_stale_topology_cache_keys()
        logger.info(f"Topology WebSocket disconnected. Total: {len(self.active_connections)}")

    async def send_to(self, websocket: WebSocket, message: Dict[str, Any]):
        """向指定连接发送消息"""
        try:
            message_json = json.dumps(message, ensure_ascii=False, default=str)
            await asyncio.wait_for(websocket.send_text(message_json), timeout=WS_SEND_TIMEOUT_SECONDS)
        except Exception as e:
            logger.warning(f"Failed to send topology message: {e}")
            self.disconnect(websocket)

    async def _send_with_timeout(self, websocket: WebSocket, message_json: str) -> bool:
        try:
            await asyncio.wait_for(websocket.send_text(message_json), timeout=WS_SEND_TIMEOUT_SECONDS)
            return True
        except Exception as e:
            logger.warning(f"Failed to broadcast to connection: {e}")
            return False

    async def broadcast(self, message: Dict[str, Any]):
        """向所有连接广播消息"""
        if not self.active_connections:
            return

        await self.broadcast_to(list(self.active_connections), message)

    async def broadcast_to(self, connections: Any, message: Dict[str, Any]):
        """向指定连接集合广播消息（批量发送 + 慢连接隔离）。"""
        if not connections:
            return

        message_json = json.dumps(message, ensure_ascii=False, default=str)
        disconnected = set()
        connection_list = list(connections)

        for offset in range(0, len(connection_list), WS_BROADCAST_BATCH_SIZE):
            batch = connection_list[offset: offset + WS_BROADCAST_BATCH_SIZE]
            results = await asyncio.gather(
                *(self._send_with_timeout(connection, message_json) for connection in batch)
            )
            for connection, ok in zip(batch, results):
                if not ok:
                    disconnected.add(connection)

        for conn in disconnected:
            self.disconnect(conn)

    def update_subscription(self, websocket: WebSocket, params: Dict[str, Any]):
        """更新订阅参数"""
        if websocket in self._subscriptions:
            current = self._subscriptions.get(websocket) or self._normalize_subscription()
            merged = {**current, **(params or {})}
            self._subscriptions[websocket] = self._normalize_subscription(merged)
            self._cleanup_stale_topology_cache_keys()

    def get_subscription(self, websocket: WebSocket) -> Dict[str, Any]:
        """获取订阅参数"""
        return self._subscriptions.get(websocket, self._normalize_subscription())

    def get_subscription_groups(self) -> Dict[str, Dict[str, Any]]:
        """
        将连接按订阅参数分组，避免重复构建同一份拓扑。
        返回:
            {
              "key": {"params": {...}, "connections": [ws1, ws2]}
            }
        """
        groups: Dict[str, Dict[str, Any]] = {}
        for connection in list(self.active_connections):
            params = self.get_subscription(connection)
            key = self._subscription_key(params)
            if key not in groups:
                groups[key] = {
                    "params": self._normalize_subscription(params),
                    "connections": [],
                }
            groups[key]["connections"].append(connection)
        return groups

    def get_subscription_key(self, websocket: WebSocket) -> str:
        return self._subscription_key(self.get_subscription(websocket))

    def has_topology_changed(self, subscription_key: str, topology_data: Dict[str, Any]) -> bool:
        """检查指定订阅下的拓扑是否发生变化"""
        if not topology_data:
            return False

        topology_str = json.dumps(topology_data, sort_keys=True, default=str)
        current_hash = hashlib.md5(topology_str.encode()).hexdigest()
        previous_hash = self._last_topology_hash_by_key.get(subscription_key)
        if current_hash != previous_hash:
            self._last_topology_hash_by_key[subscription_key] = current_hash
            self._last_topology_data_by_key[subscription_key] = topology_data
            return True

        return False

    def cache_topology(self, subscription_key: str, topology_data: Dict[str, Any]) -> None:
        topology_str = json.dumps(topology_data, sort_keys=True, default=str)
        self._last_topology_hash_by_key[subscription_key] = hashlib.md5(topology_str.encode()).hexdigest()
        self._last_topology_data_by_key[subscription_key] = topology_data

    def get_last_topology(self, subscription_key: Optional[str] = None) -> Optional[Dict[str, Any]]:
        """获取最后一次拓扑数据（可按订阅键）"""
        if subscription_key:
            return self._last_topology_data_by_key.get(subscription_key)
        if self._last_topology_data_by_key:
            return next(iter(self._last_topology_data_by_key.values()))
        return None


topology_manager = TopologyConnectionManager()


async def topology_poller(hybrid_builder, interval: float = 5.0):
    """
    定时轮询拓扑变化并推送

    Args:
        hybrid_builder: 混合拓扑构建器
        interval: 轮询间隔（秒）
    """
    logger.info("Topology poller started")

    while True:
        try:
            if topology_manager.active_connections:
                groups = topology_manager.get_subscription_groups()
                for subscription_key, group in groups.items():
                    params = group.get("params", {})
                    connections = group.get("connections", [])
                    if not connections:
                        continue

                    topology = await build_hybrid_topology_coalesced(
                        hybrid_builder,
                        time_window=params.get("time_window", "1 HOUR"),
                        namespace=params.get("namespace"),
                        confidence_threshold=params.get("confidence_threshold", 0.3),
                        inference_mode=params.get("inference_mode", "rule"),
                        message_target_enabled=params.get("message_target_enabled"),
                        message_target_patterns=params.get("message_target_patterns"),
                        message_target_min_support=params.get("message_target_min_support"),
                        message_target_max_per_log=params.get("message_target_max_per_log"),
                    )
                    if not topology_manager.has_topology_changed(subscription_key, topology):
                        continue

                    message = {
                        "type": "topology_update",
                        "data": topology,
                        "subscription": params,
                        "timestamp": _utc_now_iso(),
                    }
                    await topology_manager.broadcast_to(connections, message)
                    logger.debug("Topology change detected and sent for subscription %s", subscription_key)

            await asyncio.sleep(interval)

        except asyncio.CancelledError:
            logger.info("Topology poller cancelled")
            break
        except Exception as e:
            logger.error(f"Error in topology poller: {e}")
            await asyncio.sleep(interval)


async def topology_websocket_endpoint(websocket: WebSocket, hybrid_builder):
    """
    拓扑 WebSocket 端点

    客户端可以发送以下消息：
    - {"action": "subscribe", "params": {...}} - 更新订阅参数
    - {"action": "get"} - 获取当前拓扑
    - {"action": "ping"} - 心跳检测
    """
    await topology_manager.connect(websocket)

    try:
        await topology_manager.send_to(websocket, {
            "type": "connected",
            "message": "Topology WebSocket connected",
            "timestamp": _utc_now_iso(),
        })

        # 发送当前订阅下的缓存拓扑
        sub_key = topology_manager.get_subscription_key(websocket)
        last_topology = topology_manager.get_last_topology(sub_key)
        if last_topology:
            await topology_manager.send_to(websocket, {
                "type": "topology_update",
                "data": last_topology,
                "timestamp": _utc_now_iso(),
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
                        await topology_manager.send_to(websocket, {
                            "type": "pong",
                            "timestamp": _utc_now_iso(),
                        })

                    elif action == "subscribe":
                        params = message.get("params", {})
                        topology_manager.update_subscription(websocket, params)
                        normalized = topology_manager.get_subscription(websocket)
                        await topology_manager.send_to(websocket, {
                            "type": "subscribed",
                            "params": normalized,
                            "timestamp": _utc_now_iso(),
                        })
                        topology = await build_hybrid_topology_coalesced(
                            hybrid_builder,
                            time_window=normalized.get("time_window", "1 HOUR"),
                            namespace=normalized.get("namespace"),
                            confidence_threshold=normalized.get("confidence_threshold", 0.3),
                            inference_mode=normalized.get("inference_mode", "rule"),
                            message_target_enabled=normalized.get("message_target_enabled"),
                            message_target_patterns=normalized.get("message_target_patterns"),
                            message_target_min_support=normalized.get("message_target_min_support"),
                            message_target_max_per_log=normalized.get("message_target_max_per_log"),
                        )
                        topology_manager.cache_topology(topology_manager.get_subscription_key(websocket), topology)
                        await topology_manager.send_to(websocket, {
                            "type": "topology_update",
                            "data": topology,
                            "subscription": normalized,
                            "timestamp": _utc_now_iso(),
                        })

                    elif action == "get":
                        subscription = topology_manager.get_subscription(websocket)
                        topology = await build_hybrid_topology_coalesced(
                            hybrid_builder,
                            time_window=subscription.get("time_window", "1 HOUR"),
                            namespace=subscription.get("namespace"),
                            confidence_threshold=subscription.get("confidence_threshold", 0.3),
                            inference_mode=subscription.get("inference_mode", "rule"),
                            message_target_enabled=subscription.get("message_target_enabled"),
                            message_target_patterns=subscription.get("message_target_patterns"),
                            message_target_min_support=subscription.get("message_target_min_support"),
                            message_target_max_per_log=subscription.get("message_target_max_per_log"),
                        )
                        topology_manager.cache_topology(topology_manager.get_subscription_key(websocket), topology)
                        await topology_manager.send_to(websocket, {
                            "type": "topology_update",
                            "data": topology,
                            "subscription": subscription,
                            "timestamp": _utc_now_iso(),
                        })

                except json.JSONDecodeError:
                    await topology_manager.send_to(websocket, {
                        "type": "error",
                        "message": "Invalid JSON format",
                        "timestamp": _utc_now_iso(),
                    })

            except asyncio.TimeoutError:
                await topology_manager.send_to(websocket, {
                    "type": "ping",
                    "timestamp": _utc_now_iso(),
                })

    except WebSocketDisconnect:
        topology_manager.disconnect(websocket)
    except Exception as e:
        logger.error(f"Topology WebSocket error: {e}")
        topology_manager.disconnect(websocket)


async def broadcast_topology_event(event_type: str, data: Dict[str, Any]):
    """
    广播拓扑事件

    Args:
        event_type: 事件类型（node_added, node_removed, edge_added, edge_removed）
        data: 事件数据
    """
    await topology_manager.broadcast({
        "type": event_type,
        "data": data,
        "timestamp": _utc_now_iso(),
    })
