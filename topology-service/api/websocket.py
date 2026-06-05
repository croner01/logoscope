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
import time
from typing import Set, Dict, Any, Optional, Tuple
from fastapi import WebSocket, WebSocketDisconnect
from datetime import datetime, timezone
import hashlib

from api.topology_build_coordinator import build_hybrid_topology_coalesced

logger = logging.getLogger(__name__)
WS_BROADCAST_BATCH_SIZE = max(1, int(os.getenv("WS_BROADCAST_BATCH_SIZE", "32")))
WS_SEND_TIMEOUT_SECONDS = max(0.1, float(os.getenv("WS_SEND_TIMEOUT_SECONDS", "1.0")))

# ── 拓扑推送稳定性控制 ──
# 同一订阅两次推送之间的最小间隔（秒），减少推送频率避免前端频繁重绘
TOPOLOGY_WS_MIN_PUSH_INTERVAL = max(5.0, float(os.getenv("TOPOLOGY_WS_MIN_PUSH_INTERVAL_SECONDS", "15.0")))
# 新节点必须连续出现 N 次后才纳入推送，防止瞬时噪音导致拓扑抖动
TOPOLOGY_WS_NEW_NODE_BUFFER_COUNT = max(1, int(os.getenv("TOPOLOGY_WS_NEW_NODE_BUFFER_COUNT", "2")))
# 节点变化数（新增+移除）至少达到此值才触发推送，避免 metrics 波动导致的无效推送
TOPOLOGY_WS_MIN_NODE_CHANGE_FOR_PUSH = max(1, int(os.getenv("TOPOLOGY_WS_MIN_NODE_CHANGE_FOR_PUSH", "1")))


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
        # 推送节流：记录每个订阅的上次推送时间戳（monotonic seconds）
        self._last_push_time_by_key: Dict[str, float] = {}
        # 新节点缓冲：{subscription_key: {node_id: consecutive_appearance_count}}
        self._new_node_candidates_by_key: Dict[str, Dict[str, int]] = {}
        # 上次已推送的拓扑（仅在实际推送时更新，作为变化检测的基线）
        self._last_pushed_topology_by_key: Dict[str, Dict[str, Any]] = {}

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
            self._last_push_time_by_key.pop(key, None)
            self._last_pushed_topology_by_key.pop(key, None)
            self._new_node_candidates_by_key.pop(key, None)

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
            new_sub = self._normalize_subscription(merged)
            # 参数变更时重置推送节流，使新订阅的首次推送立即生效
            old_key = self._subscription_key(current)
            new_key = self._subscription_key(new_sub)
            if old_key != new_key:
                self.reset_push_state(new_key)
            self._subscriptions[websocket] = new_sub
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

    def _extract_node_ids(self, topology_data: Dict[str, Any]) -> Set[str]:
        """从拓扑数据中提取节点 ID 集合"""
        ids: Set[str] = set()
        for node in (topology_data.get("nodes") or []):
            node_id = node.get("id") or node.get("node_key", "")
            if node_id:
                ids.add(node_id)
        return ids

    def should_push_topology(
        self,
        subscription_key: str,
        topology_data: Dict[str, Any],
    ) -> Tuple[bool, Optional[Dict[str, Any]]]:
        """
        判断是否应该推送拓扑更新，执行以下稳定性控制：

        1. 最小推送间隔检查 — 同一订阅两次推送间隔不低于 TOPOLOGY_WS_MIN_PUSH_INTERVAL
        2. 新节点缓冲确认 — 新出现的节点必须连续出现 TOPOLOGY_WS_NEW_NODE_BUFFER_COUNT 次后才纳入
        3. 变化显著性检查 — 节点变化数（新增+移除）达到 TOPOLOGY_WS_MIN_NODE_CHANGE_FOR_PUSH 才推送

        Returns:
            (should_push, filtered_topology_or_none)
        """
        if not topology_data:
            return False, None

        now = time.monotonic()
        # 对比基线是上次推送的拓扑（而非上次缓存的），确保缓冲节点在连续出现后能被正确确认
        prev_topology = self._last_pushed_topology_by_key.get(subscription_key)

        # 首次拓扑：无条件推送
        if not prev_topology:
            self._update_push_state(subscription_key, topology_data, now)
            return True, topology_data

        # ── 1. 最小推送间隔 ──
        last_push = self._last_push_time_by_key.get(subscription_key, 0.0)
        elapsed = now - last_push
        if elapsed < TOPOLOGY_WS_MIN_PUSH_INTERVAL:
            # 缓存最新数据但不推送，下次推送时会用最新的
            self._cache_latest_topology(subscription_key, topology_data)
            return False, None

        # ── 2. 计算节点变化 ──
        current_node_ids = self._extract_node_ids(topology_data)
        prev_node_ids = self._extract_node_ids(prev_topology)

        raw_new_node_ids = current_node_ids - prev_node_ids
        removed_node_ids = prev_node_ids - current_node_ids

        # ── 3. 新节点缓冲 ──
        buffer = self._new_node_candidates_by_key.setdefault(subscription_key, {})
        confirmed_new_node_ids: Set[str] = set()

        for node_id in raw_new_node_ids:
            count = buffer.get(node_id, 0) + 1
            buffer[node_id] = count
            if count >= TOPOLOGY_WS_NEW_NODE_BUFFER_COUNT:
                confirmed_new_node_ids.add(node_id)

        # 清理不再出现的缓冲节点
        stale_ids = [nid for nid in buffer if nid not in raw_new_node_ids and nid not in current_node_ids]
        for nid in stale_ids:
            del buffer[nid]

        total_node_change = len(confirmed_new_node_ids) + len(removed_node_ids)

        # ── 4. 变化显著性检查 ──
        if total_node_change < TOPOLOGY_WS_MIN_NODE_CHANGE_FOR_PUSH:
            self._cache_latest_topology(subscription_key, topology_data)
            return False, None

        # ── 5. 构建过滤后的拓扑（移除未确认的新节点） ──
        if raw_new_node_ids != confirmed_new_node_ids:
            unconfirmed_ids = raw_new_node_ids - confirmed_new_node_ids
            filtered_nodes = [
                node for node in (topology_data.get("nodes") or [])
                if (node.get("id") or node.get("node_key", "")) not in unconfirmed_ids
            ]
            # 过滤涉及未确认节点的边
            filtered_edges = [
                edge for edge in (topology_data.get("edges") or [])
                if (edge.get("source") or edge.get("source_node_key", "")) not in unconfirmed_ids
                and (edge.get("target") or edge.get("target_node_key", "")) not in unconfirmed_ids
            ]
            filtered_topology = {**topology_data, "nodes": filtered_nodes, "edges": filtered_edges}
            logger.info(
                "Topology push for %s: buffering %d new node(s), pushing %d confirmed, %d removed",
                subscription_key, len(unconfirmed_ids), len(confirmed_new_node_ids), len(removed_node_ids),
            )
        else:
            filtered_topology = topology_data

        self._update_push_state(subscription_key, filtered_topology, now)
        return True, filtered_topology

    def _cache_latest_topology(self, subscription_key: str, topology_data: Dict[str, Any]) -> None:
        """缓存最新拓扑数据但不标记为已推送（保留推送时间不变）"""
        topology_str = json.dumps(topology_data, sort_keys=True, default=str)
        self._last_topology_hash_by_key[subscription_key] = hashlib.md5(topology_str.encode()).hexdigest()
        self._last_topology_data_by_key[subscription_key] = topology_data

    def _update_push_state(self, subscription_key: str, topology_data: Dict[str, Any], push_time: float) -> None:
        """更新推送状态：缓存拓扑 + 记录推送时间 + 更新推送基线"""
        topology_str = json.dumps(topology_data, sort_keys=True, default=str)
        self._last_topology_hash_by_key[subscription_key] = hashlib.md5(topology_str.encode()).hexdigest()
        self._last_topology_data_by_key[subscription_key] = topology_data
        self._last_pushed_topology_by_key[subscription_key] = topology_data
        self._last_push_time_by_key[subscription_key] = push_time

    def reset_push_state(self, subscription_key: str) -> None:
        """重置订阅的推送状态（订阅参数变更时调用，使首次推送立即生效）"""
        self._last_push_time_by_key.pop(subscription_key, None)
        self._last_pushed_topology_by_key.pop(subscription_key, None)
        self._new_node_candidates_by_key.pop(subscription_key, None)

    def cache_topology(self, subscription_key: str, topology_data: Dict[str, Any]) -> None:
        """缓存拓扑数据（subscribe/get 等即时推送后调用，同步更新推送基线防止 poller 重复推送）"""
        topology_str = json.dumps(topology_data, sort_keys=True, default=str)
        self._last_topology_hash_by_key[subscription_key] = hashlib.md5(topology_str.encode()).hexdigest()
        self._last_topology_data_by_key[subscription_key] = topology_data
        self._last_pushed_topology_by_key[subscription_key] = topology_data

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
    定时轮询拓扑变化并推送（带稳定性控制）

    稳定性策略：
    - 最小推送间隔：避免高频推送
    - 新节点缓冲：瞬态节点不立即推送
    - 变化显著性：仅节点数变化超过阈值才推送

    Args:
        hybrid_builder: 混合拓扑构建器
        interval: 轮询间隔（秒）
    """
    logger.info("Topology poller started (stability: min_push=%.0fs, new_node_buffer=%d, min_change=%d)",
                TOPOLOGY_WS_MIN_PUSH_INTERVAL, TOPOLOGY_WS_NEW_NODE_BUFFER_COUNT, TOPOLOGY_WS_MIN_NODE_CHANGE_FOR_PUSH)

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

                    should_push, filtered_topology = topology_manager.should_push_topology(
                        subscription_key, topology,
                    )
                    if not should_push or filtered_topology is None:
                        continue

                    message = {
                        "type": "topology_update",
                        "data": filtered_topology,
                        "subscription": params,
                        "timestamp": _utc_now_iso(),
                    }
                    await topology_manager.broadcast_to(connections, message)
                    logger.debug("Topology change sent for subscription %s", subscription_key)

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
