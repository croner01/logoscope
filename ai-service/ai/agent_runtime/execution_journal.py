"""
Session-level command dedup and result memory.

Each ExecutionJournal lives for the duration of one agent run.
It prevents re-executing identical commands and provides past
results to the LLM for context-aware planning.
"""
from __future__ import annotations

import hashlib
import json
from typing import Any, Dict, List, Optional

from ai.agent_runtime.models import utc_now_iso


def _as_str(value: Any, default: str = "") -> str:
    if value is None:
        return default
    if isinstance(value, str):
        return value
    return str(value)


class ExecutionJournal:
    """Session-level command dedup and result memory."""

    MAX_ENTRIES = 50

    def __init__(self, entries: List[Dict[str, Any]] | None = None):
        self._entries: Dict[str, Dict[str, Any]] = {}
        if entries:
            for entry in entries:
                fp = _as_str(entry.get("fingerprint")).strip()
                if fp:
                    self._entries[fp] = dict(entry)

    # ── public API ──────────────────────────────────────────────────────────

    def fingerprint(self, command_spec: Dict[str, Any]) -> str:
        """Compute a stable fingerprint from a command_spec.

        Hash of (tool, target_kind, target_identity, normalized command text).
        Parameter values ARE part of the fingerprint — we only skip exact duplicates.
        """
        if not isinstance(command_spec, dict):
            return hashlib.sha1(b"empty").hexdigest()[:16]

        tool = _as_str(command_spec.get("tool")).strip().lower()
        args = command_spec.get("args") if isinstance(command_spec.get("args"), dict) else {}

        command = _as_str(args.get("command") or command_spec.get("command")).strip()
        query = _as_str(args.get("query") or command_spec.get("query")).strip()
        action = command or query

        target_kind = _as_str(
            args.get("target_kind") or command_spec.get("target_kind")
        ).strip()
        target_identity = _as_str(
            args.get("target_identity") or command_spec.get("target_identity")
        ).strip()

        payload = {
            "tool": tool,
            "action": " ".join(action.split()),  # normalize whitespace
            "target_kind": target_kind,
            "target_identity": target_identity,
        }
        raw = json.dumps(payload, ensure_ascii=False, sort_keys=True)
        return hashlib.sha1(raw.encode("utf-8")).hexdigest()[:16]

    def lookup(self, fingerprint: str) -> Dict[str, Any] | None:
        """Return cached journal entry or None."""
        fp = _as_str(fingerprint).strip()
        if not fp:
            return None
        return self._entries.get(fp)

    def record(
        self,
        fingerprint: str,
        command: str,
        target_kind: str,
        target_identity: str,
        exit_code: int,
        summary: str,
        output_preview: str,
        *,
        channel: str = "remote",
    ) -> None:
        """Record a command execution in the journal."""
        fp = _as_str(fingerprint).strip()
        if not fp:
            return

        self._entries[fp] = {
            "fingerprint": fp,
            "command": _as_str(command).strip(),
            "target_kind": _as_str(target_kind).strip(),
            "target_identity": _as_str(target_identity).strip(),
            "executed_at": utc_now_iso(),
            "exit_code": int(exit_code or 0),
            "summary": _as_str(summary).strip(),
            "output_truncated_preview": _as_str(output_preview)[:2000],
            "channel": _as_str(channel).strip() or "remote",
        }

        # Cap entries
        if len(self._entries) > self.MAX_ENTRIES:
            sorted_entries = sorted(
                self._entries.values(),
                key=lambda e: _as_str(e.get("executed_at", "")),
            )
            self._entries = {
                e["fingerprint"]: e
                for e in sorted_entries[-self.MAX_ENTRIES:]
            }

    def context_for_llm(self, max_chars: int = 4000) -> str:
        """Build a summary of all executed commands for LLM context injection.

        Returns a compact text block listing each command and its outcome.
        """
        if not self._entries:
            return ""

        lines = ["## 已执行的诊断命令 (本次会话)", ""]
        total = 0
        for entry in sorted(
            self._entries.values(),
            key=lambda e: _as_str(e.get("executed_at", "")),
        ):
            cmd = _as_str(entry.get("command", "")).strip()
            summary = _as_str(entry.get("summary", "")).strip()
            exit_code = entry.get("exit_code", 0)
            status = "✓" if exit_code == 0 else "✗"
            line = f"- {status} `{cmd}`"
            if summary:
                line += f" — {summary}"
            if total + len(line) > max_chars:
                lines.append(f"  ... (还有 {len(self._entries) - len(lines) + 2} 条已省略)")
                break
            lines.append(line)
            total += len(line) + 1

        return "\n".join(lines)

    def to_list(self) -> List[Dict[str, Any]]:
        """Serialize entries for storage in AgentRun.summary_json."""
        return sorted(
            self._entries.values(),
            key=lambda e: _as_str(e.get("executed_at", "")),
        )

    @classmethod
    def from_summary(cls, summary_json: Dict[str, Any]) -> "ExecutionJournal":
        """Restore journal from AgentRun.summary_json."""
        entries = summary_json.get("execution_journal") if isinstance(summary_json, dict) else None
        if isinstance(entries, list):
            return cls(entries=entries)
        return cls()


__all__ = ["ExecutionJournal"]
