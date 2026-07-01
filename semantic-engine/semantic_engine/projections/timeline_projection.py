"""TimelineProjection — 状态演化时间线（内存实现，生产用 ClickHouse MV）。"""
from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import Dict, List, Optional


@dataclass
class StateTransition:
    """状态转换记录。"""
    entity_id: str
    from_state: str
    to_state: str
    timestamp: datetime
    event_id: str = ""


class TimelineProjection:
    """
    状态演化时间线投影。

    - 记录实体的状态转换历史
    - 支持时间窗口过滤
    - 生产环境由 ClickHouse Materialized View 支持
    """

    def __init__(self):
        self._transitions: Dict[str, List[StateTransition]] = {}

    def record_transition(self, entity_id: str, from_state: str, to_state: str,
                          timestamp: Optional[datetime] = None,
                          event_id: str = "") -> None:
        """记录一次状态转换。"""
        ts = timestamp or datetime.utcnow()
        transition = StateTransition(
            entity_id=entity_id,
            from_state=from_state,
            to_state=to_state,
            timestamp=ts,
            event_id=event_id,
        )
        if entity_id not in self._transitions:
            self._transitions[entity_id] = []
        self._transitions[entity_id].append(transition)

    def get_timeline(self, entity_id: str, window: str = "1 HOUR") -> List[StateTransition]:
        """获取实体的状态演化链。"""
        transitions = self._transitions.get(entity_id, [])
        if not transitions:
            return []

        # 时间窗口过滤（以最新事件为基准向回看）
        latest = max(t.timestamp for t in transitions)
        window_seconds = self._parse_window(window)
        cutoff = latest - timedelta(seconds=window_seconds) if window_seconds else datetime.min

        return sorted(
            [t for t in transitions if t.timestamp >= cutoff],
            key=lambda t: t.timestamp,
        )

    def has_state_changed(self, entity_id: str, window_minutes: int = 5) -> bool:
        """指定窗口内（从当前时间回看）状态是否变化过。"""
        transitions = self._transitions.get(entity_id, [])
        if not transitions:
            return False
        now = datetime.utcnow()
        cutoff = now - timedelta(minutes=window_minutes)
        return any(t.timestamp >= cutoff for t in transitions)

    def _parse_window(self, window: str) -> int:
        """解析时间窗口字符串为秒数。"""
        window = window.upper().strip()
        parts = window.split()
        if len(parts) != 2:
            return 3600  # 默认 1 小时
        try:
            value = int(parts[0])
            unit = parts[1]
            if unit in ("SECOND", "SECONDS"):
                return value
            elif unit in ("MINUTE", "MINUTES"):
                return value * 60
            elif unit in ("HOUR", "HOURS"):
                return value * 3600
            elif unit in ("DAY", "DAYS"):
                return value * 86400
            return 3600
        except (ValueError, IndexError):
            return 3600
