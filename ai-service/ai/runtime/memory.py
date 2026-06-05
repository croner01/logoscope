"""Session memory — command dedup, result journal, LLM context injection."""
from __future__ import annotations

import hashlib
import json
from typing import Any, Dict, List

from ai.command.spec import CommandSpec


def _as_str(value: Any, default: str = "") -> str:
    if value is None:
        return default
    if isinstance(value, str):
        return value
    return str(value)


class SessionMemory:
    """Session-level command dedup and result memory.

    Unified replacement for ExecutionJournal, react_memory,
    and long_term_memory. Single fingerprint algorithm for
    both store and lookup (fixes audit C1).
    """

    MAX_ENTRIES = 50

    def __init__(self):
        self._entries: Dict[str, dict] = {}

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
        """Check if this exact spec was already executed.

        Blocked entries don't count as duplicates — they were never executed.
        """
        fp = self.fingerprint(spec)
        entry = self._entries.get(fp)
        if entry is None:
            return False
        return not entry.get("blocked", False)

    def record(
        self,
        spec: CommandSpec,
        *,
        exit_code: int = 0,
        summary: str = "",
        output_preview: str = "",
    ) -> None:
        """Record a successful or failed command execution."""
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

    def record_blocked(self, spec: CommandSpec, reason: str = "") -> None:
        """Record a command blocked by security policy (does NOT count for dedup)."""
        fp = self.fingerprint(spec)
        self._entries[fp] = {
            "fingerprint": fp,
            "command": _as_str(spec.command),
            "blocked": True,
            "reason": _as_str(reason),
        }
        self._cap_entries()

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
