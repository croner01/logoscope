"""
Runtime v4 lightweight thread/run index store.

Phase-1 implementation keeps in-memory metadata for API v2 mapping.
Thread and run source of truth remains existing ai.agent_runtime store.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timezone
import threading
from typing import Dict, List, Optional
import uuid


def _utc_now_iso() -> str:
    return datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")


def _build_id(prefix: str) -> str:
    return f"{prefix}-{uuid.uuid4().hex[:12]}"


@dataclass
class ThreadRecord:
    thread_id: str
    session_id: str
    conversation_id: str
    title: str
    status: str = "active"
    created_at: str = field(default_factory=_utc_now_iso)
    updated_at: str = field(default_factory=_utc_now_iso)

    def to_dict(self) -> Dict[str, str]:
        return {
            "thread_id": self.thread_id,
            "session_id": self.session_id,
            "conversation_id": self.conversation_id,
            "title": self.title,
            "status": self.status,
            "created_at": self.created_at,
            "updated_at": self.updated_at,
        }


class RuntimeV4ThreadStore:
    """In-memory thread registry and run-thread index."""

    def __init__(self) -> None:
        self._lock = threading.RLock()
        self._threads: Dict[str, ThreadRecord] = {}
        self._thread_runs: Dict[str, List[str]] = {}
        self._run_thread_index: Dict[str, str] = {}
        self._thread_idempotency_runs: Dict[str, Dict[str, str]] = {}

    def clear(self) -> None:
        with self._lock:
            self._threads.clear()
            self._thread_runs.clear()
            self._run_thread_index.clear()
            self._thread_idempotency_runs.clear()

    def create_thread(
        self,
        *,
        session_id: str,
        conversation_id: str,
        title: str,
        thread_id: str = "",
    ) -> ThreadRecord:
        with self._lock:
            safe_thread_id = thread_id.strip() or _build_id("thr")
            now_iso = _utc_now_iso()
            record = ThreadRecord(
                thread_id=safe_thread_id,
                session_id=(session_id or "").strip(),
                conversation_id=(conversation_id or "").strip(),
                title=(title or "AI Runtime Thread").strip() or "AI Runtime Thread",
                created_at=now_iso,
                updated_at=now_iso,
            )
            self._threads[safe_thread_id] = record
            self._thread_runs.setdefault(safe_thread_id, [])
            self._thread_idempotency_runs.setdefault(safe_thread_id, {})
            return record

    def get_thread(self, thread_id: str) -> Optional[ThreadRecord]:
        with self._lock:
            return self._threads.get((thread_id or "").strip())

    def bind_run(self, *, thread_id: str, run_id: str) -> None:
        safe_thread_id = (thread_id or "").strip()
        safe_run_id = (run_id or "").strip()
        if not safe_thread_id or not safe_run_id:
            return
        with self._lock:
            if safe_thread_id not in self._threads:
                return
            runs = self._thread_runs.setdefault(safe_thread_id, [])
            if safe_run_id not in runs:
                runs.append(safe_run_id)
            self._run_thread_index[safe_run_id] = safe_thread_id
            self._threads[safe_thread_id].updated_at = _utc_now_iso()

    def bind_idempotency_run(self, *, thread_id: str, idempotency_key: str, run_id: str) -> None:
        safe_thread_id = (thread_id or "").strip()
        safe_key = (idempotency_key or "").strip()
        safe_run_id = (run_id or "").strip()
        if not safe_thread_id or not safe_key or not safe_run_id:
            return
        with self._lock:
            if safe_thread_id not in self._threads:
                return
            idempotency_runs = self._thread_idempotency_runs.setdefault(safe_thread_id, {})
            idempotency_runs[safe_key] = safe_run_id
            self._run_thread_index[safe_run_id] = safe_thread_id
            self._threads[safe_thread_id].updated_at = _utc_now_iso()

    def run_id_for_idempotency_key(self, *, thread_id: str, idempotency_key: str) -> str:
        safe_thread_id = (thread_id or "").strip()
        safe_key = (idempotency_key or "").strip()
        if not safe_thread_id or not safe_key:
            return ""
        with self._lock:
            idempotency_runs = self._thread_idempotency_runs.get(safe_thread_id) or {}
            return idempotency_runs.get(safe_key, "")

    def run_ids_for_thread(self, thread_id: str) -> List[str]:
        with self._lock:
            return list(self._thread_runs.get((thread_id or "").strip(), []))

    def latest_run_id_for_thread(self, thread_id: str) -> str:
        with self._lock:
            runs = self._thread_runs.get((thread_id or "").strip(), [])
            if not runs:
                return ""
            return runs[-1]

    def thread_id_for_run(self, run_id: str) -> str:
        with self._lock:
            return self._run_thread_index.get((run_id or "").strip(), "")


_thread_store: Optional[RuntimeV4ThreadStore] = None


def get_runtime_v4_thread_store() -> RuntimeV4ThreadStore:
    global _thread_store
    if _thread_store is None:
        _thread_store = RuntimeV4ThreadStore()
    return _thread_store
