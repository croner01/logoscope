"""
LangGraph checkpoint store with optional ClickHouse persistence.
"""

from __future__ import annotations

from dataclasses import asdict
from datetime import datetime, timezone
import json
import logging
import os
import threading
from typing import Any, Dict, Optional
import uuid

from ai.runtime_v4.langgraph.state import InnerGraphState


logger = logging.getLogger(__name__)


def _as_str(value: Any, default: str = "") -> str:
    if value is None:
        return default
    if isinstance(value, str):
        return value
    return str(value)


def _to_datetime(value: Any) -> datetime:
    text = _as_str(value).strip()
    if not text:
        return datetime.now(timezone.utc)
    normalized = text.replace("Z", "+00:00")
    try:
        return datetime.fromisoformat(normalized)
    except Exception:
        return datetime.now(timezone.utc)


def _utc_now_iso() -> str:
    return datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")


class GraphCheckpointStore:
    """Checkpoint store backed by in-memory cache + ClickHouse optional persistence."""

    def __init__(self, storage_adapter: Any = None) -> None:
        self._lock = threading.RLock()
        self.storage = storage_adapter
        self._states: Dict[str, InnerGraphState] = {}
        self._checkpoint_table = self._resolve_checkpoint_table(storage_adapter)
        if self._is_clickhouse_available():
            self._ensure_clickhouse_table()

    @staticmethod
    def _resolve_checkpoint_table(storage_adapter: Any) -> str:
        default_database = (
            getattr(storage_adapter, "ch_database", "")
            or (getattr(storage_adapter, "config", {}) or {}).get("clickhouse", {}).get("database", "logs")
            or "logs"
        )
        return os.getenv(
            "AI_RUNTIME_V4_GRAPH_CHECKPOINT_TABLE",
            f"{default_database}.ai_runtime_v4_graph_checkpoints",
        )

    def attach_storage(self, storage_adapter: Any) -> None:
        with self._lock:
            self.storage = storage_adapter
            self._checkpoint_table = self._resolve_checkpoint_table(storage_adapter)
            if self._is_clickhouse_available():
                self._ensure_clickhouse_table()

    def clear(self) -> None:
        with self._lock:
            self._states.clear()

    def _is_clickhouse_available(self) -> bool:
        return bool(self.storage and getattr(self.storage, "ch_client", None))

    def _ensure_clickhouse_table(self) -> None:
        if not self._is_clickhouse_available():
            return
        create_sql = f"""
        CREATE TABLE IF NOT EXISTS {self._checkpoint_table} (
            run_id String,
            checkpoint_id String,
            phase String,
            iteration UInt32,
            done UInt8,
            state_json String,
            created_at DateTime64(3, 'UTC')
        )
        ENGINE = MergeTree()
        PARTITION BY toYYYYMM(created_at)
        ORDER BY (run_id, created_at, checkpoint_id)
        SETTINGS index_granularity = 8192
        """
        try:
            self.storage.ch_client.execute(create_sql)
        except Exception as exc:
            logger.warning("failed to ensure langgraph checkpoint table: %s", exc)

    @staticmethod
    def _state_to_payload(state: InnerGraphState) -> Dict[str, Any]:
        payload = asdict(state)
        payload["run_id"] = _as_str(payload.get("run_id"))
        payload["question"] = _as_str(payload.get("question"))
        payload["phase"] = _as_str(payload.get("phase"), "planning")
        payload["iteration"] = int(payload.get("iteration") or 0)
        payload["max_iterations"] = int(payload.get("max_iterations") or 4)
        payload["done"] = bool(payload.get("done"))
        payload["actions"] = payload.get("actions") if isinstance(payload.get("actions"), list) else []
        payload["observations"] = payload.get("observations") if isinstance(payload.get("observations"), list) else []
        payload["reflection"] = payload.get("reflection") if isinstance(payload.get("reflection"), dict) else {}
        return payload

    @staticmethod
    def _payload_to_state(payload: Dict[str, Any]) -> Optional[InnerGraphState]:
        if not isinstance(payload, dict):
            return None
        run_id = _as_str(payload.get("run_id")).strip()
        question = _as_str(payload.get("question")).strip()
        if not run_id or not question:
            return None
        return InnerGraphState(
            run_id=run_id,
            question=question,
            iteration=max(0, int(payload.get("iteration") or 0)),
            max_iterations=max(1, int(payload.get("max_iterations") or 4)),
            phase=_as_str(payload.get("phase"), "planning") or "planning",
            actions=payload.get("actions") if isinstance(payload.get("actions"), list) else [],
            observations=payload.get("observations") if isinstance(payload.get("observations"), list) else [],
            reflection=payload.get("reflection") if isinstance(payload.get("reflection"), dict) else {},
            done=bool(payload.get("done")),
        )

    def save(self, state: InnerGraphState) -> None:
        with self._lock:
            self._states[state.run_id] = state
            if not self._is_clickhouse_available():
                return
            payload = self._state_to_payload(state)
            checkpoint_id = f"gcp-{uuid.uuid4().hex[:12]}"
            row = {
                "run_id": state.run_id,
                "checkpoint_id": checkpoint_id,
                "phase": _as_str(state.phase, "planning"),
                "iteration": max(0, int(state.iteration)),
                "done": 1 if state.done else 0,
                "state_json": json.dumps(payload, ensure_ascii=False),
                "created_at": _to_datetime(_utc_now_iso()),
            }
            sql = f"""
            INSERT INTO {self._checkpoint_table} (
                run_id, checkpoint_id, phase, iteration, done, state_json, created_at
            ) VALUES
            """
            try:
                self.storage.ch_client.execute(sql, [row])
            except Exception as exc:
                logger.warning("failed to save langgraph checkpoint: %s", exc)

    def load(self, run_id: str) -> Optional[InnerGraphState]:
        safe_run_id = _as_str(run_id).strip()
        if not safe_run_id:
            return None
        with self._lock:
            cached = self._states.get(safe_run_id)
            if cached is not None:
                return cached
            if not self._is_clickhouse_available():
                return None
            sql = f"""
            SELECT state_json
            FROM {self._checkpoint_table}
            WHERE run_id = %(run_id)s
            ORDER BY created_at DESC
            LIMIT 1
            """
            try:
                rows = self.storage.ch_client.execute(sql, {"run_id": safe_run_id})
            except Exception as exc:
                logger.warning("failed to load langgraph checkpoint: %s", exc)
                return None
            if not rows:
                return None
            raw_state_json = rows[0][0] if rows[0] else ""
            try:
                payload = json.loads(_as_str(raw_state_json, "{}") or "{}")
            except Exception:
                return None
            state = self._payload_to_state(payload)
            if state is None:
                return None
            self._states[safe_run_id] = state
            return state


_checkpoint_store: Optional[GraphCheckpointStore] = None


def get_graph_checkpoint_store() -> GraphCheckpointStore:
    global _checkpoint_store
    if _checkpoint_store is None:
        _checkpoint_store = GraphCheckpointStore()
    return _checkpoint_store


def set_graph_checkpoint_storage(storage_adapter: Any) -> GraphCheckpointStore:
    store = get_graph_checkpoint_store()
    store.attach_storage(storage_adapter)
    return store
