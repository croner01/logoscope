"""
Command execution history — ClickHouse-backed unified memory store.

Replaces 5 ad-hoc memory systems (executed_set, command_run_index,
react_memory, runtime_thread_memory, LTM) with a single append-only
ClickHouse table, keyed by ``fingerprint`` for cross-run dedup.

Usage::

    store = ClickHouseHistoryStore()
    await store.ensure_table()

    # Record a command execution
    await store.record(CommandRecord(
        fingerprint=make_fingerprint("kubectl get pods", "list pods"),
        command="kubectl get pods -A",
        command_type="QUERY",
        tool="generic_exec",
        status="success",
        exit_code=0,
        run_id="run-001",
        session_id="sess-001",
    ))

    # Check if a command was recently executed
    records = await store.query_by_fingerprint("abc123...")
    recent_failures = await store.count_recent_failures("abc123...")
"""
from __future__ import annotations

import hashlib
import json
import logging
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional

from pydantic import BaseModel, Field

logger = logging.getLogger(__name__)


# ── Data model ─────────────────────────────────────────────────────────────

class CommandRecord(BaseModel):
    """A single command execution record, persisted to ClickHouse."""

    fingerprint: str = ""
    command: str = ""
    command_type: str = "unknown"  # QUERY | REPAIR
    tool: str = "generic_exec"
    purpose: str = ""
    status: str = "unknown"  # success | failed | blocked
    exit_code: Optional[int] = None
    failure_category: str = ""  # from observing._classify_failure()
    stdout: str = ""
    stderr: str = ""
    target_kind: str = ""
    target_identity: str = ""
    run_id: str = ""
    session_id: str = ""
    created_at: str = ""

    def model_post_init(self, __context: Any) -> None:
        if not self.created_at:
            self.created_at = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S")
        if not self.fingerprint and self.command:
            self.fingerprint = make_fingerprint(self.command, self.purpose)


def make_fingerprint(command: str, purpose: str = "") -> str:
    """Stable fingerprint for cross-run command dedup.

    Uses only (tool-normalized command, purpose), deliberately excluding
    run_id / session_id so the same command across different runs produces
    the same fingerprint.
    """
    payload = {
        "command": " ".join(str(command or "").strip().split()),
        "purpose": str(purpose or "").strip(),
    }
    raw = json.dumps(payload, ensure_ascii=False, sort_keys=True)
    return hashlib.sha1(raw.encode("utf-8")).hexdigest()[:16]


# ── ClickHouse schema ─────────────────────────────────────────────────────

CREATE_TABLE_SQL = """
CREATE TABLE IF NOT EXISTS ai.command_history (
    fingerprint          String,
    command              String,
    command_type         String,
    tool                 String,
    purpose              String,
    status               String,
    exit_code            Nullable(Int32),
    failure_category     String,
    stdout               String,
    stderr               String,
    target_kind          String,
    target_identity      String,
    run_id               String,
    session_id           String,
    created_at           DateTime,
    updated_at           DateTime DEFAULT now()
)
ENGINE = ReplacingMergeTree(updated_at)
ORDER BY (fingerprint, created_at)
"""

CREATE_DB_SQL = "CREATE DATABASE IF NOT EXISTS ai"

# ── Queries ───────────────────────────────────────────────────────────────

QUERY_BY_FINGERPRINT = """
SELECT
    fingerprint, command, command_type, tool, purpose,
    status, exit_code, failure_category,
    stdout, stderr, target_kind, target_identity,
    run_id, session_id, created_at
FROM ai.command_history
WHERE fingerprint = %(fingerprint)s
ORDER BY created_at DESC
LIMIT %(limit)s
"""

COUNT_RECENT_FAILURES = """
SELECT count() AS cnt
FROM ai.command_history
WHERE fingerprint = %(fingerprint)s
  AND status = 'failed'
  AND created_at >= now() - INTERVAL %(hours)s HOUR
"""

QUERY_RECENT_FOR_SESSION = """
SELECT
    fingerprint, command, command_type, tool, purpose,
    status, exit_code, failure_category,
    stdout, stderr, target_kind, target_identity,
    run_id, session_id, created_at
FROM ai.command_history
WHERE session_id = %(session_id)s
ORDER BY created_at DESC
LIMIT %(limit)s
"""

QUERY_FAILED_COMMANDS_FOR_LLM = """
SELECT command, status, failure_category, purpose, created_at
FROM ai.command_history
WHERE session_id = %(session_id)s
  AND status = 'failed'
ORDER BY created_at DESC
LIMIT %(limit)s
"""

INSERT_RECORD = """
INSERT INTO ai.command_history (
    fingerprint, command, command_type, tool, purpose,
    status, exit_code, failure_category,
    stdout, stderr, target_kind, target_identity,
    run_id, session_id, created_at
) VALUES (
    %(fingerprint)s, %(command)s, %(command_type)s, %(tool)s, %(purpose)s,
    %(status)s, %(exit_code)s, %(failure_category)s,
    %(stdout)s, %(stderr)s, %(target_kind)s, %(target_identity)s,
    %(run_id)s, %(session_id)s, %(created_at)s
)
"""


# ── Store ──────────────────────────────────────────────────────────────────

class ClickHouseHistoryStore:
    """ClickHouse-backed command history store.

    Thread-safe (each operation creates its own cursor). Lightweight —
    designed to be called from both Claude SDK and LangGraph runtimes.
    """

    def __init__(
        self,
        host: str = "localhost",
        port: int = 9000,
        *,
        _client: Any = None,  # for dependency injection / testing
    ):
        self._host = host
        self._port = port
        self._client = _client

    # ── Connection ──────────────────────────────────────────────────────

    def _get_client(self):
        """Lazy-init ClickHouse client."""
        if self._client is None:
            from clickhouse_driver import Client as ClickHouseClient
            self._client = ClickHouseClient(host=self._host, port=self._port)
        return self._client

    # ── Schema management ───────────────────────────────────────────────

    def ensure_database(self) -> None:
        """Create the ``ai`` database if it doesn't exist."""
        client = self._get_client()
        client.execute(CREATE_DB_SQL)
        logger.info("Ensured database: ai")

    def ensure_table(self) -> None:
        """Create the ``command_history`` table if it doesn't exist."""
        client = self._get_client()
        client.execute(CREATE_TABLE_SQL)
        logger.info("Ensured table: ai.command_history")

    # ── Write ───────────────────────────────────────────────────────────

    def record(self, entry: CommandRecord) -> None:
        """Persist a command execution record."""
        client = self._get_client()
        client.execute(INSERT_RECORD, entry.model_dump())
        logger.debug("Recorded command: fp=%s cmd=%s status=%s",
                     entry.fingerprint, entry.command[:80], entry.status)

    def record_dict(self, **kwargs: Any) -> CommandRecord:
        """Build and persist a CommandRecord from keyword args."""
        entry = CommandRecord(**kwargs)
        self.record(entry)
        return entry

    # ── Read ────────────────────────────────────────────────────────────

    def query_by_fingerprint(
        self,
        fingerprint: str,
        *,
        limit: int = 10,
    ) -> List[CommandRecord]:
        """Return all records matching a fingerprint, newest first."""
        client = self._get_client()
        rows = client.execute(
            QUERY_BY_FINGERPRINT,
            {"fingerprint": fingerprint, "limit": limit},
        )
        return [self._row_to_record(r) for r in rows]

    def count_recent_failures(
        self,
        fingerprint: str,
        *,
        hours: int = 24,
    ) -> int:
        """Count how many times this command failed in the last N hours."""
        client = self._get_client()
        rows = client.execute(
            COUNT_RECENT_FAILURES,
            {"fingerprint": fingerprint, "hours": hours},
        )
        return rows[0][0] if rows else 0

    def query_recent_for_session(
        self,
        session_id: str,
        *,
        limit: int = 50,
    ) -> List[CommandRecord]:
        """Return all command records for a session, newest first."""
        client = self._get_client()
        rows = client.execute(
            QUERY_RECENT_FOR_SESSION,
            {"session_id": session_id, "limit": limit},
        )
        return [self._row_to_record(r) for r in rows]

    def query_failed_commands_for_llm(
        self,
        session_id: str,
        *,
        limit: int = 10,
    ) -> List[Dict[str, Any]]:
        """Return failed commands as simple dicts for LLM context injection."""
        client = self._get_client()
        rows = client.execute(
            QUERY_FAILED_COMMANDS_FOR_LLM,
            {"session_id": session_id, "limit": limit},
        )
        return [
            {
                "command": r[0],
                "status": r[1],
                "failure_category": r[2],
                "purpose": r[3],
                "created_at": str(r[4]),
            }
            for r in rows
        ]

    # ── Helpers ─────────────────────────────────────────────────────────

    @staticmethod
    def _row_to_record(row: tuple) -> CommandRecord:
        """Convert a ClickHouse result row to a CommandRecord."""
        return CommandRecord(
            fingerprint=row[0],
            command=row[1],
            command_type=row[2],
            tool=row[3],
            purpose=row[4],
            status=row[5],
            exit_code=row[6],
            failure_category=row[7],
            stdout=row[8] or "",
            stderr=row[9] or "",
            target_kind=row[10],
            target_identity=row[11],
            run_id=row[12],
            session_id=row[13],
            created_at=str(row[14]) if row[14] else "",
        )


# ── Module-level helpers ──────────────────────────────────────────────────

def build_context_for_llm(
    records: List[CommandRecord],
    *,
    max_chars: int = 4000,
) -> str:
    """Build compact Markdown text block from command history for LLM injection.

    Replaces ``SessionMemory.context_for_llm()``.
    """
    if not records:
        return ""

    lines = ["## 已执行的诊断命令 (本次会话)", ""]
    total = 0
    for entry in records:
        cmd = entry.command or ""
        status = entry.status or "unknown"
        exit_code = entry.exit_code
        marker = "✓" if status == "success" else ("✗" if status in ("failed", "blocked") else "?")
        line = f"- {marker} `{cmd}`"
        if exit_code is not None:
            line += f" (exit={exit_code})"
        if entry.purpose:
            line += f" — {entry.purpose}"
        if entry.failure_category:
            line += f" [{entry.failure_category}]"
        if total + len(line) > max_chars:
            break
        lines.append(line)
        total += len(line) + 1

    return "\n".join(lines)


__all__ = [
    "CommandRecord",
    "make_fingerprint",
    "ClickHouseHistoryStore",
    "build_context_for_llm",
]
