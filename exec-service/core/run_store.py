"""
In-memory command run store.
"""

from __future__ import annotations

import asyncio
import threading
from typing import Any, Callable, Dict, List, Optional

from core.runtime_history_store import (
    clickhouse_enabled as runtime_history_clickhouse_enabled,
    list_run_records,
    load_run_record,
    persist_run_record,
)


def as_str(value: Any, default: str = "") -> str:
    if value is None:
        return default
    if isinstance(value, str):
        return value
    return str(value)


class CommandRunStore:
    """Stores command run records, tasks, and process handles."""

    def __init__(self) -> None:
        self._lock = threading.RLock()
        self._runs: Dict[str, Dict[str, Any]] = {}
        self._tasks: Dict[str, asyncio.Task] = {}
        self._processes: Dict[str, asyncio.subprocess.Process] = {}

    def save_run(self, run: Dict[str, Any]) -> Dict[str, Any]:
        safe_run = dict(run) if isinstance(run, dict) else {}
        run_id = as_str(safe_run.get("run_id"))
        if run_id:
            with self._lock:
                self._runs[run_id] = safe_run
            persist_run_record(safe_run)
        return dict(safe_run)

    def get_run(self, run_id: str) -> Optional[Dict[str, Any]]:
        safe_run_id = as_str(run_id)
        with self._lock:
            run = self._runs.get(safe_run_id)
        if not isinstance(run, dict) and runtime_history_clickhouse_enabled():
            run = load_run_record(safe_run_id)
            if isinstance(run, dict):
                with self._lock:
                    self._runs[safe_run_id] = dict(run)
        return dict(run) if isinstance(run, dict) else None

    def mutate_run(self, run_id: str, mutator: Callable[[Dict[str, Any]], Dict[str, Any] | None]) -> Optional[Dict[str, Any]]:
        safe_run_id = as_str(run_id)
        with self._lock:
            current = dict(self._runs.get(safe_run_id) or {})
        if not current and runtime_history_clickhouse_enabled():
            loaded = load_run_record(safe_run_id)
            if isinstance(loaded, dict):
                current = dict(loaded)
                with self._lock:
                    self._runs[safe_run_id] = dict(current)
        if not current:
            return None
        with self._lock:
            updated = mutator(current)
            if isinstance(updated, dict):
                current = updated
            self._runs[safe_run_id] = current
        persist_run_record(current)
        return dict(current)

    def list_runs(self, limit: int = 100) -> List[Dict[str, Any]]:
        safe_limit = max(1, min(int(limit or 100), 1000))
        if runtime_history_clickhouse_enabled():
            rows = list_run_records(limit=safe_limit)
            if rows:
                with self._lock:
                    for row in rows:
                        run_id = as_str((row if isinstance(row, dict) else {}).get("run_id"))
                        if run_id:
                            self._runs[run_id] = dict(row)
                return [dict(item) for item in rows]
        with self._lock:
            rows = list(self._runs.values())[-safe_limit:]
        return [dict(item) for item in rows]

    def register_task(self, run_id: str, task: asyncio.Task) -> None:
        safe_run_id = as_str(run_id)
        with self._lock:
            self._tasks[safe_run_id] = task

    def get_task(self, run_id: str) -> Optional[asyncio.Task]:
        safe_run_id = as_str(run_id)
        with self._lock:
            return self._tasks.get(safe_run_id)

    def pop_task(self, run_id: str) -> Optional[asyncio.Task]:
        safe_run_id = as_str(run_id)
        with self._lock:
            return self._tasks.pop(safe_run_id, None)

    def register_process(self, run_id: str, process: asyncio.subprocess.Process) -> None:
        safe_run_id = as_str(run_id)
        with self._lock:
            self._processes[safe_run_id] = process

    def get_process(self, run_id: str) -> Optional[asyncio.subprocess.Process]:
        safe_run_id = as_str(run_id)
        with self._lock:
            return self._processes.get(safe_run_id)

    def pop_process(self, run_id: str) -> Optional[asyncio.subprocess.Process]:
        safe_run_id = as_str(run_id)
        with self._lock:
            return self._processes.pop(safe_run_id, None)
