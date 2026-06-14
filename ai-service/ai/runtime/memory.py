"""
Session memory — command dedup, result journal, LLM context injection.

Extends the unified ``SessionMemory`` with optional ClickHouse persistence
via ``ClickHouseHistoryStore``.
"""
from __future__ import annotations

import hashlib
import json
from typing import Any, Dict, List, Optional

from ai.command.spec import CommandSpec


def _as_str(value: Any, default: str = "") -> str:
    if value is None:
        return default
    if isinstance(value, str):
        return value
    return str(value)


class SessionMemory:
    """Session-level command dedup and result memory.

    Unified replacement for executed_set, command_run_index, react_memory,
    runtime_thread_memory, and LTM. Single fingerprint algorithm for
    both store and lookup.

    Optionally persists to ClickHouse via ``history_store`` for cross-session
    query and dedup.
    """

    MAX_ENTRIES = 50

    def __init__(self, *, history_store: Any = None):
        """Initialize SessionMemory.

        Args:
            history_store: Optional ``ClickHouseHistoryStore`` instance.
                           If provided, every ``record()`` call also persists
                           to ClickHouse. The in-memory cache remains the
                           primary check for ``is_duplicate()`` within a session.
        """
        self._entries: Dict[str, dict] = {}
        self._history_store = history_store

    def _get_history_store(self):
        """Lazy import to avoid circular dependency."""
        if self._history_store is None:
            return None
        return self._history_store

    def fingerprint(self, spec: CommandSpec) -> str:
        """Compute stable fingerprint from a CommandSpec.

        Hash of (tool, normalized command text) only.
        target_kind and target_identity are routing hints that may vary
        between LLM iterations — they do not change the command's identity.
        """
        payload = {
            "tool": str(spec.tool.value),
            "command": " ".join(_as_str(spec.command).split()),
        }
        raw = json.dumps(payload, ensure_ascii=False, sort_keys=True)
        return hashlib.sha1(raw.encode("utf-8")).hexdigest()[:16]

    def is_duplicate(self, spec: CommandSpec) -> bool:
        """Check if this exact spec was already seen (executed or blocked).

        Checks in-memory cache first. If not found and a ``history_store``
        is configured, also checks ClickHouse for cross-session duplicates.
        """
        fp = self.fingerprint(spec)
        if fp in self._entries:
            return True
        store = self._get_history_store()
        if store is not None:
            records = store.query_by_fingerprint(fp, limit=1)
            if records:
                # Warm the cache
                self._entries[fp] = {
                    "fingerprint": fp,
                    "command": _as_str(spec.command),
                    "status": records[0].status,
                }
                return True
        return False

    def was_previously_blocked(self, spec: CommandSpec) -> bool:
        """Check if this spec was previously blocked by security."""
        fp = self.fingerprint(spec)
        entry = self._entries.get(fp)
        if entry is None:
            return False
        return bool(entry.get("blocked", False))

    def record(
        self,
        spec: CommandSpec,
        *,
        exit_code: int = 0,
        summary: str = "",
        output_preview: str = "",
        run_id: str = "",
        session_id: str = "",
    ) -> None:
        """Record a successful or failed command execution.

        Updates in-memory cache. If ``history_store`` is configured, also
        writes to ClickHouse for cross-session persistence.
        """
        fp = self.fingerprint(spec)
        self._entries[fp] = {
            "fingerprint": fp,
            "command": _as_str(spec.command),
            "target_kind": _as_str(spec.target_kind),
            "target_identity": _as_str(spec.target_identity),
            "exit_code": exit_code,
            "summary": _as_str(summary),
            "output_truncated_preview": _as_str(output_preview)[:2000],
        }
        self._cap_entries()

        # Persist to ClickHouse if configured
        store = self._get_history_store()
        if store is not None:
            from ai.command.history import CommandRecord
            store.record(CommandRecord(
                fingerprint=fp,
                command=_as_str(spec.command),
                command_type=_as_str(spec.command_type.value if hasattr(spec.command_type, 'value') else spec.command_type),
                tool=_as_str(spec.tool.value if hasattr(spec.tool, 'value') else spec.tool),
                purpose=_as_str(spec.purpose),
                status="success" if exit_code == 0 else "failed",
                exit_code=exit_code,
                target_kind=_as_str(spec.target_kind),
                target_identity=_as_str(spec.target_identity),
                run_id=run_id,
                session_id=session_id,
            ))

    def record_blocked(self, spec: CommandSpec, reason: str = "") -> None:
        """Record a command blocked by security policy.

        Persists to ClickHouse if configured (status=blocked).
        """
        fp = self.fingerprint(spec)
        self._entries[fp] = {
            "fingerprint": fp,
            "command": _as_str(spec.command),
            "blocked": True,
            "reason": _as_str(reason),
        }
        self._cap_entries()

        store = self._get_history_store()
        if store is not None:
            from ai.command.history import CommandRecord
            store.record(CommandRecord(
                fingerprint=fp,
                command=_as_str(spec.command),
                status="blocked",
                purpose=reason,
            ))

    def context_for_llm(self, max_chars: int = 4000) -> str:
        """Build compact text block for LLM context injection."""
        executed = [e for e in self._entries.values() if not e.get("blocked")]
        if not executed:
            return ""

        lines = ["## 已执行的诊断命令 (本次会话)", ""]
        total = 0
        for entry in executed:
            cmd = entry.get("command", "")
            summary = entry.get("summary", "")
            exit_code = entry.get("exit_code", 0)
            status = "✓" if exit_code == 0 else "✗"
            line = f"- {status} `{cmd}`"
            if summary:
                line += f" — {summary}"
            if total + len(line) > max_chars:
                break
            lines.append(line)
            total += len(line) + 1
        return "\n".join(lines)

    def snapshot(self) -> list:
        return list(self._entries.values())

    def _cap_entries(self) -> None:
        if len(self._entries) > self.MAX_ENTRIES:
            keys = list(self._entries.keys())[:len(self._entries) - self.MAX_ENTRIES]
            for k in keys:
                del self._entries[k]


__all__ = ["SessionMemory"]
