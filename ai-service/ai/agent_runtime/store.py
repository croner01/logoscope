"""
AI agent runtime store.
"""

from __future__ import annotations

from datetime import datetime
import json
import logging
import os
import time
from typing import Any, Dict, List, Optional, Tuple

from ai.agent_runtime.models import AgentRun, RunEvent


logger = logging.getLogger(__name__)


def _as_str(value: Any, default: str = "") -> str:
    text = str(value).strip() if value is not None else ""
    return text if text else default


def _to_datetime(value: Any) -> datetime:
    text = _as_str(value)
    if not text:
        return datetime.utcnow()
    normalized = text.replace("Z", "+00:00")
    try:
        return datetime.fromisoformat(normalized)
    except Exception:
        return datetime.utcnow()


class AgentRuntimeStore:
    """Run/event store with ClickHouse persistence and in-memory fallback."""

    def __init__(self, storage_adapter: Any = None):
        self.storage = storage_adapter
        self._runs: Dict[str, AgentRun] = {}
        self._events: Dict[str, List[RunEvent]] = {}

        default_database = (
            getattr(storage_adapter, "ch_database", "")
            or (getattr(storage_adapter, "config", {}) or {}).get("clickhouse", {}).get("database", "logs")
            or "logs"
        )
        self.run_table = os.getenv("AI_AGENT_RUN_CH_TABLE", f"{default_database}.ai_agent_runs")
        self.event_table = os.getenv("AI_AGENT_RUN_EVENT_CH_TABLE", f"{default_database}.ai_agent_run_events")
        self.run_latest_view = os.getenv("AI_AGENT_RUN_LATEST_VIEW", f"{default_database}.v_ai_agent_runs_latest")
        self._read_source_cache_ttl_seconds = max(5, int(os.getenv("AI_AGENT_RUN_READ_SOURCE_CACHE_TTL_SECONDS", "30")))
        self._run_read_source_cache: Optional[Tuple[str, bool]] = None
        self._run_read_source_cache_checked_at = 0.0

        if self._is_clickhouse_available():
            self._ensure_clickhouse_tables()

    def attach_storage(self, storage_adapter: Any) -> None:
        self.storage = storage_adapter
        self._run_read_source_cache = None
        self._run_read_source_cache_checked_at = 0.0
        if self._is_clickhouse_available():
            self._ensure_clickhouse_tables()

    def _is_clickhouse_available(self) -> bool:
        return bool(self.storage and getattr(self.storage, "ch_client", None))

    @staticmethod
    def _split_table_name(table_name: str) -> Tuple[str, str]:
        normalized = str(table_name or "").strip()
        if "." in normalized:
            db_name, tbl_name = normalized.split(".", 1)
            return db_name, tbl_name
        return "default", normalized

    def _table_exists(self, table_name: str) -> bool:
        if not self._is_clickhouse_available():
            return False
        db_name, tbl_name = self._split_table_name(table_name)
        try:
            rows = self.storage.ch_client.execute(
                """
                SELECT count()
                FROM system.tables
                WHERE database = %(database)s
                  AND name = %(name)s
                """,
                {"database": db_name, "name": tbl_name},
            )
            return bool(rows and rows[0] and int(rows[0][0]) > 0)
        except Exception:
            return False

    def _get_run_read_source(self) -> Tuple[str, bool]:
        now_ts = time.time()
        cached = self._run_read_source_cache
        if cached is not None and (now_ts - self._run_read_source_cache_checked_at) < self._read_source_cache_ttl_seconds:
            return cached

        if self._table_exists(self.run_latest_view):
            self._run_read_source_cache = (self.run_latest_view, False)
        else:
            self._run_read_source_cache = (self.run_table, True)
        self._run_read_source_cache_checked_at = now_ts
        return self._run_read_source_cache

    def _ensure_clickhouse_tables(self) -> None:
        if not self._is_clickhouse_available():
            return

        alter_run_table_sql = f"""
        ALTER TABLE {self.run_table}
        ADD COLUMN IF NOT EXISTS conversation_id String DEFAULT '' AFTER session_id
        """

        create_run_sql = f"""
        CREATE TABLE IF NOT EXISTS {self.run_table} (
            run_id String,
            session_id String,
            conversation_id String,
            analysis_type String,
            engine String,
            runtime_version String,
            user_message_id String,
            assistant_message_id String,
            service_name String,
            trace_id String,
            status String,
            input_json String,
            context_json String,
            summary_json String,
            error_code String,
            error_detail String,
            created_at DateTime64(3, 'UTC'),
            updated_at DateTime64(3, 'UTC'),
            ended_at Nullable(DateTime64(3, 'UTC'))
        )
        ENGINE = ReplacingMergeTree(updated_at)
        PARTITION BY toYYYYMM(created_at)
        ORDER BY (run_id)
        SETTINGS index_granularity = 8192
        """

        create_event_sql = f"""
        CREATE TABLE IF NOT EXISTS {self.event_table} (
            run_id String,
            event_id String,
            seq UInt64,
            event_type String,
            payload_json String,
            created_at DateTime64(3, 'UTC')
        )
        ENGINE = MergeTree()
        PARTITION BY toYYYYMM(created_at)
        ORDER BY (run_id, seq, created_at, event_id)
        SETTINGS index_granularity = 8192
        """

        create_run_latest_view_sql = f"""
        CREATE VIEW IF NOT EXISTS {self.run_latest_view} AS
        SELECT
            run_id,
            argMax(session_id, _updated_at) AS session_id,
            argMax(conversation_id, _updated_at) AS conversation_id,
            argMax(analysis_type, _updated_at) AS analysis_type,
            argMax(engine, _updated_at) AS engine,
            argMax(runtime_version, _updated_at) AS runtime_version,
            argMax(user_message_id, _updated_at) AS user_message_id,
            argMax(assistant_message_id, _updated_at) AS assistant_message_id,
            argMax(service_name, _updated_at) AS service_name,
            argMax(trace_id, _updated_at) AS trace_id,
            argMax(status, _updated_at) AS status,
            argMax(input_json, _updated_at) AS input_json,
            argMax(context_json, _updated_at) AS context_json,
            argMax(summary_json, _updated_at) AS summary_json,
            argMax(error_code, _updated_at) AS error_code,
            argMax(error_detail, _updated_at) AS error_detail,
            max(created_at) AS created_at,
            max(_updated_at) AS updated_at,
            argMax(ended_at, _updated_at) AS ended_at
        FROM
        (
            SELECT *, updated_at AS _updated_at
            FROM {self.run_table}
        )
        GROUP BY run_id
        """

        self.storage.ch_client.execute(create_run_sql)
        self.storage.ch_client.execute(alter_run_table_sql)
        self.storage.ch_client.execute(create_event_sql)
        self.storage.ch_client.execute(f"DROP VIEW IF EXISTS {self.run_latest_view}")
        self.storage.ch_client.execute(create_run_latest_view_sql)

    def _insert_run_to_clickhouse(self, run: AgentRun) -> None:
        if not self._is_clickhouse_available():
            return
        sql = f"""
        INSERT INTO {self.run_table} (
            run_id, session_id, conversation_id, analysis_type, engine, runtime_version,
            user_message_id, assistant_message_id, service_name, trace_id, status,
            input_json, context_json, summary_json, error_code, error_detail,
            created_at, updated_at, ended_at
        ) VALUES
        """
        row = {
            "run_id": run.run_id,
            "session_id": run.session_id,
            "conversation_id": run.conversation_id,
            "analysis_type": run.analysis_type,
            "engine": run.engine,
            "runtime_version": run.runtime_version,
            "user_message_id": run.user_message_id,
            "assistant_message_id": run.assistant_message_id,
            "service_name": run.service_name,
            "trace_id": run.trace_id,
            "status": run.status,
            "input_json": json.dumps(run.input_json or {}, ensure_ascii=False),
            "context_json": json.dumps(run.context_json or {}, ensure_ascii=False),
            "summary_json": json.dumps(run.summary_json or {}, ensure_ascii=False),
            "error_code": run.error_code,
            "error_detail": run.error_detail,
            "created_at": _to_datetime(run.created_at),
            "updated_at": _to_datetime(run.updated_at),
            "ended_at": _to_datetime(run.ended_at) if run.ended_at else None,
        }
        try:
            self.storage.ch_client.execute(sql, [row])
        except Exception as exc:
            logger.warning("failed to insert ai agent run: %s", exc)

    def _insert_event_to_clickhouse(self, event: RunEvent) -> None:
        if not self._is_clickhouse_available():
            return
        sql = f"""
        INSERT INTO {self.event_table} (
            run_id, event_id, seq, event_type, payload_json, created_at
        ) VALUES
        """
        row = {
            "run_id": event.run_id,
            "event_id": event.event_id,
            "seq": int(event.seq),
            "event_type": event.event_type,
            "payload_json": json.dumps(event.payload or {}, ensure_ascii=False),
            "created_at": _to_datetime(event.created_at),
        }
        try:
            self.storage.ch_client.execute(sql, [row])
        except Exception as exc:
            logger.warning("failed to insert ai agent event: %s", exc)

    def save_run(self, run: AgentRun) -> AgentRun:
        self._runs[run.run_id] = run
        self._insert_run_to_clickhouse(run)
        return run

    @staticmethod
    def _row_to_run(row: Tuple[Any, ...]) -> AgentRun:
        return AgentRun(
            run_id=_as_str(row[0]),
            session_id=_as_str(row[1]),
            conversation_id=_as_str(row[2]),
            analysis_type=_as_str(row[3]),
            engine=_as_str(row[4]),
            runtime_version=_as_str(row[5]),
            user_message_id=_as_str(row[6]),
            assistant_message_id=_as_str(row[7]),
            service_name=_as_str(row[8]),
            trace_id=_as_str(row[9]),
            status=_as_str(row[10]),
            input_json=json.loads(_as_str(row[11], "{}") or "{}"),
            context_json=json.loads(_as_str(row[12], "{}") or "{}"),
            summary_json=json.loads(_as_str(row[13], "{}") or "{}"),
            error_code=_as_str(row[14]),
            error_detail=_as_str(row[15]),
            created_at=_as_str(row[16]),
            updated_at=_as_str(row[17]),
            ended_at=_as_str(row[18]) or None,
        )

    def get_run(self, run_id: str, *, fresh: bool = False) -> Optional[AgentRun]:
        rid = _as_str(run_id)
        if not rid:
            return None
        cached = self._runs.get(rid)
        if cached is not None and not bool(fresh):
            return cached
        if not self._is_clickhouse_available():
            return cached
        source_table, need_final = self._get_run_read_source()
        final_clause = " FINAL" if need_final else ""
        sql = f"""
        SELECT
            run_id, session_id, conversation_id, analysis_type, engine, runtime_version,
            user_message_id, assistant_message_id, service_name, trace_id, status,
            input_json, context_json, summary_json, error_code, error_detail,
            created_at, updated_at, ended_at
        FROM {source_table}{final_clause}
        WHERE run_id = %(run_id)s
        LIMIT 1
        """
        try:
            rows = self.storage.ch_client.execute(sql, {"run_id": rid})
        except Exception as exc:
            logger.warning("failed to fetch ai agent run: %s", exc)
            return cached
        if not rows:
            return cached
        run = self._row_to_run(rows[0])
        self._runs[run.run_id] = run
        return run

    def list_runs_by_thread(
        self,
        *,
        session_id: str = "",
        conversation_id: str = "",
        limit: int = 10,
    ) -> List[AgentRun]:
        safe_session_id = _as_str(session_id)
        safe_conversation_id = _as_str(conversation_id)
        safe_limit = max(1, min(int(limit or 10), 100))

        cached_runs = [
            run
            for run in self._runs.values()
            if (
                (not safe_session_id or _as_str(run.session_id) == safe_session_id)
                and (not safe_conversation_id or _as_str(run.conversation_id) == safe_conversation_id)
            )
        ]
        cached_runs.sort(key=lambda item: _to_datetime(item.updated_at), reverse=True)
        if cached_runs and not self._is_clickhouse_available():
            return cached_runs[:safe_limit]
        if not self._is_clickhouse_available():
            return cached_runs[:safe_limit]

        source_table, need_final = self._get_run_read_source()
        final_clause = " FINAL" if need_final else ""
        conditions = ["1 = 1"]
        params: Dict[str, Any] = {"limit": safe_limit}
        if safe_session_id:
            conditions.append("session_id = %(session_id)s")
            params["session_id"] = safe_session_id
        if safe_conversation_id:
            conditions.append("conversation_id = %(conversation_id)s")
            params["conversation_id"] = safe_conversation_id
        sql = f"""
        SELECT
            run_id, session_id, conversation_id, analysis_type, engine, runtime_version,
            user_message_id, assistant_message_id, service_name, trace_id, status,
            input_json, context_json, summary_json, error_code, error_detail,
            created_at, updated_at, ended_at
        FROM {source_table}{final_clause}
        WHERE {' AND '.join(conditions)}
        ORDER BY updated_at DESC
        LIMIT %(limit)s
        """
        try:
            rows = self.storage.ch_client.execute(sql, params)
        except Exception as exc:
            logger.warning("failed to list ai agent runs by thread: %s", exc)
            return cached_runs[:safe_limit]
        results: List[AgentRun] = []
        for row in rows:
            try:
                run = self._row_to_run(row)
            except Exception:
                continue
            self._runs[run.run_id] = run
            results.append(run)
        return results[:safe_limit] if results else cached_runs[:safe_limit]

    def append_event(self, event: RunEvent) -> RunEvent:
        bucket = self._events.setdefault(event.run_id, [])
        bucket.append(event)
        self._insert_event_to_clickhouse(event)
        return event

    def list_events(self, run_id: str, after_seq: int = 0, limit: int = 500) -> List[RunEvent]:
        rid = _as_str(run_id)
        if not rid:
            return []
        safe_after = max(0, int(after_seq or 0))
        safe_limit = max(1, min(int(limit or 500), 5000))
        cached = self._events.get(rid)
        if cached is not None:
            return [item for item in cached if int(item.seq) > safe_after][:safe_limit]
        if not self._is_clickhouse_available():
            return []
        sql = f"""
        SELECT run_id, event_id, seq, event_type, payload_json, created_at
        FROM {self.event_table}
        WHERE run_id = %(run_id)s
          AND seq > %(after_seq)s
        ORDER BY seq ASC
        LIMIT %(limit)s
        """
        try:
            rows = self.storage.ch_client.execute(
                sql,
                {"run_id": rid, "after_seq": safe_after, "limit": safe_limit},
            )
        except Exception as exc:
            logger.warning("failed to list ai agent events: %s", exc)
            return []
        events: List[RunEvent] = []
        for row in rows:
            try:
                payload = json.loads(_as_str(row[4], "{}") or "{}")
            except Exception:
                payload = {}
            events.append(
                RunEvent(
                    run_id=_as_str(row[0]),
                    event_id=_as_str(row[1]),
                    seq=int(row[2] or 0),
                    event_type=_as_str(row[3]),
                    payload=payload if isinstance(payload, dict) else {},
                    created_at=_as_str(row[5]),
                )
            )
        if events:
            self._events.setdefault(rid, []).extend(events)
        return events

    def get_next_seq(self, run_id: str) -> int:
        rid = _as_str(run_id)
        cached = self._events.get(rid, [])
        if cached:
            return int(cached[-1].seq) + 1
        events = self.list_events(rid, after_seq=0, limit=1_000_000)
        if not events:
            return 1
        return int(events[-1].seq) + 1
