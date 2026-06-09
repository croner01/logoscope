"""SSE event fan-out for runtime engine."""
from __future__ import annotations

import asyncio
from typing import Any, Dict, List


class EventEmitter:
    """Publish-subscribe event bus for runtime events.

    Each run_id subscribes one or more asyncio.Queue instances.
    Events are broadcast to all subscribers of a given run.
    """

    def __init__(self):
        self._queues: Dict[str, List[asyncio.Queue]] = {}

    def subscribe(self, run_id: str) -> asyncio.Queue:
        queue: asyncio.Queue = asyncio.Queue()
        if run_id not in self._queues:
            self._queues[run_id] = []
        self._queues[run_id].append(queue)
        return queue

    def unsubscribe(self, run_id: str, queue: asyncio.Queue) -> None:
        if run_id in self._queues:
            try:
                self._queues[run_id].remove(queue)
            except ValueError:
                pass
            if not self._queues[run_id]:
                del self._queues[run_id]

    async def emit(self, run_id: str, event_type: str, payload: Dict[str, Any]) -> None:
        event = {"type": event_type, "payload": payload}
        for queue in self._queues.get(run_id, []):
            try:
                queue.put_nowait(event)
            except asyncio.QueueFull:
                pass

    async def request_approval(self, run_id: str, decision) -> bool:
        """Emit approval_required event and wait for user response.

        Returns True if approved, False if denied.
        """
        await self.emit(run_id, "approval_required", {
            "reason": decision.reason,
            "requires_elevation": decision.requires_elevation,
        })
        for queue in self._queues.get(run_id, []):
            try:
                result = await asyncio.wait_for(queue.get(), timeout=900)
                if result.get("type") == "approval_resolved":
                    return result.get("payload", {}).get("approved", False)
            except asyncio.TimeoutError:
                return False
        return False


__all__ = ["EventEmitter"]
