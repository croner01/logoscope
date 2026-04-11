"""
In-memory command event store with SSE subscribers.
"""

from __future__ import annotations

import asyncio
import threading
import uuid
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional

from core.runtime_history_store import (
    clickhouse_enabled as runtime_history_clickhouse_enabled,
    list_event_records,
    persist_event_record,
)


def utc_now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def build_event_id() -> str:
    return f"evt-{uuid.uuid4().hex[:12]}"


def as_str(value: Any, default: str = "") -> str:
    if value is None:
        return default
    if isinstance(value, str):
        return value
    return str(value)


class CommandEventStore:
    """Stores command run events and manages live subscribers."""

    def __init__(self) -> None:
        self._lock = threading.RLock()
        self._events: Dict[str, List[Dict[str, Any]]] = {}
        self._subscribers: Dict[str, List[asyncio.Queue]] = {}

    def append_event(
        self,
        run_id: str,
        event_type: str,
        payload: Optional[Dict[str, Any]] = None,
    ) -> Dict[str, Any]:
        safe_run_id = as_str(run_id)
        with self._lock:
            bucket = self._events.setdefault(safe_run_id, [])
            event = {
                "event_id": build_event_id(),
                "run_id": safe_run_id,
                "seq": len(bucket) + 1,
                "event_type": as_str(event_type),
                "created_at": utc_now_iso(),
                "payload": payload if isinstance(payload, dict) else {},
            }
            bucket.append(event)
            subscribers = list(self._subscribers.get(safe_run_id, []))
        persist_event_record(event)
        for queue in subscribers:
            try:
                queue.put_nowait(event)
            except Exception:
                continue
        return dict(event)

    def list_events(self, run_id: str, after_seq: int = 0, limit: int = 500) -> List[Dict[str, Any]]:
        safe_run_id = as_str(run_id)
        safe_after = max(0, int(after_seq or 0))
        safe_limit = max(1, min(int(limit or 500), 5000))
        with self._lock:
            bucket = list(self._events.get(safe_run_id, []))
        if runtime_history_clickhouse_enabled():
            if not bucket:
                loaded = list_event_records(
                    run_id=safe_run_id,
                    after_seq=safe_after,
                    limit=safe_limit,
                )
                if loaded:
                    with self._lock:
                        existing = self._events.setdefault(safe_run_id, [])
                        existing.extend(loaded)
                    return [dict(item) for item in loaded]
            else:
                max_cached_seq = max(int(item.get("seq", 0)) for item in bucket) if bucket else 0
                if max_cached_seq <= safe_after:
                    loaded = list_event_records(
                        run_id=safe_run_id,
                        after_seq=safe_after,
                        limit=safe_limit,
                    )
                    if loaded:
                        with self._lock:
                            existing = self._events.setdefault(safe_run_id, [])
                            existing.extend(loaded)
                        return [dict(item) for item in loaded]
        return [dict(item) for item in bucket if int(item.get("seq", 0)) > safe_after][:safe_limit]

    def subscribe(self, run_id: str) -> asyncio.Queue:
        queue: asyncio.Queue = asyncio.Queue()
        safe_run_id = as_str(run_id)
        with self._lock:
            self._subscribers.setdefault(safe_run_id, []).append(queue)
        return queue

    def unsubscribe(self, run_id: str, queue: asyncio.Queue) -> None:
        safe_run_id = as_str(run_id)
        with self._lock:
            queues = self._subscribers.get(safe_run_id, [])
            self._subscribers[safe_run_id] = [item for item in queues if item is not queue]
            if not self._subscribers[safe_run_id]:
                self._subscribers.pop(safe_run_id, None)
