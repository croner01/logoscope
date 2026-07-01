"""HistoryQuery — 历史事件查询接口（WorldView 的查询组件之一）。"""
from typing import List, Dict, Optional, Tuple
from shared_src.event.envelope import EventEnvelope
from shared_src.event.raw_event_store import RawEventStore


class HistoryQuery:
    """
    历史事件查询。

    - get_recent_events: 最近 N 条事件（可按资源类型/名称过滤）
    - get_alarms: 告警事件
    - get_events_by_type: 按类型查询
    """

    def __init__(self, event_store: RawEventStore):
        self.event_store = event_store

    def get_recent_events(self, count: int = 50,
                          entity_type: Optional[str] = None,
                          entity_name: Optional[str] = None) -> List[EventEnvelope]:
        """最近的 N 条事件。可选按 (entity_type, entity_name) 过滤。"""
        all_events = list(self.event_store.replay())
        if entity_type and entity_name:
            all_events = [
                e for e in all_events
                if getattr(e, "entity_type", None) == entity_type
                and getattr(e, "entity_name", None) == entity_name
            ]
        return all_events[-count:] if count < len(all_events) else all_events

    def get_alarms(self) -> List[EventEnvelope]:
        """告警事件。"""
        return [
            env for env in self.event_store.replay()
            if env.event_type == "alert.triggered"
        ]

    def get_events_by_type(self, event_type: str) -> List[EventEnvelope]:
        """按 event_type 查询。"""
        return [
            env for env in self.event_store.replay()
            if env.event_type == event_type
        ]
