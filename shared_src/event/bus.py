from abc import ABC, abstractmethod
from typing import Callable, Dict, List, Optional
from .envelope import EventEnvelope


class EventBus(ABC):
    """Multi-topic 事件总线抽象。"""

    TOPICS = {
        "platform.raw":              {"retention_days": 90, "partitions": 16},
        "platform.normalized":       {"retention_days": 7,  "partitions": 16},
        "platform.entity":           {"retention_days": 90, "partitions": 8},
        "platform.state":            {"retention_days": 1,  "partitions": 8},
        "platform.interaction":      {"retention_days": 90, "partitions": 16},
        "platform.graph":            {"retention_days": 90, "partitions": 8},
        "platform.alert":            {"retention_days": 30, "partitions": 4},
        "platform.workflow.command": {"retention_days": 7,  "partitions": 4},
        "platform.workflow.event":   {"retention_days": 90, "partitions": 4},
        "platform.system":           {"retention_days": 90, "partitions": 4},
    }

    @abstractmethod
    def publish(self, topic: str, envelope: EventEnvelope):
        ...

    @abstractmethod
    def subscribe(self, topic: str, group: str,
                   callback: Callable[[EventEnvelope], None]):
        ...

    @abstractmethod
    def latest_offsets(self, topic: str) -> Dict[int, int]:
        ...


class InMemoryEventBus(EventBus):
    """测试用内存实现。"""

    def __init__(self):
        self._subscribers: Dict[str, List[Callable]] = {}
        self._history: Dict[str, List[EventEnvelope]] = {}

    def publish(self, topic: str, envelope: EventEnvelope):
        if topic not in self._history:
            self._history[topic] = []
        self._history[topic].append(envelope)
        for cb in self._subscribers.get(topic, []):
            cb(envelope)

    def subscribe(self, topic: str, group: str,
                   callback: Callable[[EventEnvelope], None]):
        if topic not in self._subscribers:
            self._subscribers[topic] = []
        self._subscribers[topic].append(callback)

    def latest_offsets(self, topic: str) -> Dict[int, int]:
        return {0: len(self._history.get(topic, []))}
