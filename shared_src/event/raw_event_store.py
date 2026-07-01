from typing import Dict, List, Optional, Generator
from .envelope import EventEnvelope


class RawEventStore:
    """原始事件存储。In-memory 实现（生产用 Kafka + WAL）。不可变 append-only。"""

    def __init__(self):
        self._store: Dict[str, EventEnvelope] = {}
        self._order: List[str] = []

    def append(self, envelope: EventEnvelope):
        import copy
        self._store[envelope.event_id] = copy.deepcopy(envelope)
        self._order.append(envelope.event_id)

    def read(self, event_id: str) -> Optional[EventEnvelope]:
        import copy
        env = self._store.get(event_id)
        return copy.deepcopy(env) if env else None

    def replay(self) -> Generator[EventEnvelope, None, None]:
        import copy
        for eid in self._order:
            yield copy.deepcopy(self._store[eid])
