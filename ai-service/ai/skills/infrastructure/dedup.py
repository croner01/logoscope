"""Command deduplication and execution caching skills."""

from __future__ import annotations

import hashlib
import time
from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional

from pydantic import BaseModel


class CommandSignature(BaseModel):
    """Canonical representation of a command for deduplication."""

    command: str
    normalized_command: str
    signature_hash: str
    command_type: str = "unknown"
    risk_level: str = "high"
    timestamp: float = field(default_factory=time.time)


class DeduplicationResult(BaseModel):
    """Result of command deduplication analysis."""

    new_commands: List[str] = []
    duplicate_commands: List[Dict[str, Any]] = []
    equivalent_commands: List[Dict[str, Any]] = []
    deduplication_ratio: float = 0.0
    skipped_count: int = 0
    reasons: List[str] = []


class ExecutionCacheEntry(BaseModel):
    """Cached execution result."""

    command_hash: str
    command: str
    exit_code: Optional[int] = None
    stdout: str = ""
    stderr: str = ""
    cached_at: float = field(default_factory=time.time)
    expires_at: float = 0
    hit_count: int = 0


class CommandDeduplicator:
    """
    Command deduplication service.

    Analyzes command sequences to identify:
    - Exact duplicates
    - Semantically equivalent commands
    - Redundant commands
    - Cache-eligible commands
    """

    def __init__(self, cache_ttl_seconds: int = 300):
        self._cache: Dict[str, ExecutionCacheEntry] = {}
        self._history: List[CommandSignature] = []
        self._cache_ttl = cache_ttl_seconds

    def _normalize_command(self, command: str) -> str:
        """Normalize command for comparison."""
        import re
        # Remove extra whitespace
        normalized = " ".join(command.split())
        # Remove trailing semicolons
        normalized = normalized.rstrip(";")
        # Normalize common patterns
        normalized = re.sub(r"--tail=\d+", "--tail=N", normalized)
        normalized = re.sub(r"--since=\d+[mhd]", "--since=T", normalized)
        return normalized.strip()

    def _compute_hash(self, command: str) -> str:
        """Compute signature hash for a command."""
        normalized = self._normalize_command(command)
        return hashlib.sha256(normalized.encode()).hexdigest()[:16]

    def analyze_commands(
        self,
        commands: List[str],
        existing_history: Optional[List[Dict[str, Any]]] = None,
    ) -> DeduplicationResult:
        """
        Analyze a list of commands and identify duplicates.

        Args:
            commands: List of commands to analyze
            existing_history: Optional history of previously executed commands

        Returns:
            DeduplicationResult with analysis results
        """
        result = DeduplicationResult()
        if not commands:
            return result

        seen_hashes: Dict[str, int] = {}
        history_set = set()

        # Build history lookup from existing history
        if existing_history:
            for item in existing_history:
                cmd = item.get("command", "")
                if cmd:
                    h = self._compute_hash(cmd)
                    history_set.add(h)

        for i, cmd in enumerate(commands):
            if not cmd.strip():
                continue

            normalized = self._normalize_command(cmd)
            cmd_hash = self._compute_hash(cmd)

            # Check for exact duplicate in current batch
            if cmd_hash in seen_hashes:
                result.duplicate_commands.append({
                    "command": cmd,
                    "normalized": normalized,
                    "hash": cmd_hash,
                    "first_seen_at": seen_hashes[cmd_hash],
                    "duplicate_at": i,
                    "reason": "exact duplicate in current batch",
                })
                result.skipped_count += 1
                continue

            # Check for duplicate in history
            if cmd_hash in history_set:
                result.duplicate_commands.append({
                    "command": cmd,
                    "normalized": normalized,
                    "hash": cmd_hash,
                    "reason": "already executed in this session",
                })
                result.skipped_count += 1
                continue

            # Check cache
            cached = self._cache.get(cmd_hash)
            if cached and cached.expires_at > time.time():
                result.duplicate_commands.append({
                    "command": cmd,
                    "cached_at": cached.cached_at,
                    "exit_code": cached.exit_code,
                    "reason": "found in cache",
                })
                result.skipped_count += 1
                cached.hit_count += 1
                continue

            # New command
            seen_hashes[cmd_hash] = i
            history_set.add(cmd_hash)
            result.new_commands.append(cmd)

        # Calculate deduplication ratio
        total = len(commands)
        if total > 0:
            result.deduplication_ratio = result.skipped_count / total

        return result

    def cache_result(
        self,
        command: str,
        exit_code: Optional[int],
        stdout: str,
        stderr: str,
        ttl_seconds: Optional[int] = None,
    ) -> None:
        """Cache an execution result."""
        cmd_hash = self._compute_hash(command)
        ttl = ttl_seconds or self._cache_ttl
        self._cache[cmd_hash] = ExecutionCacheEntry(
            command_hash=cmd_hash,
            command=command,
            exit_code=exit_code,
            stdout=stdout,
            stderr=stderr,
            expires_at=time.time() + ttl,
        )

    def get_cached(self, command: str) -> Optional[ExecutionCacheEntry]:
        """Get cached result for a command."""
        cmd_hash = self._compute_hash(command)
        cached = self._cache.get(cmd_hash)
        if cached and cached.expires_at > time.time():
            return cached
        return None

    def clear_expired(self) -> int:
        """Clear expired cache entries. Returns count of cleared entries."""
        now = time.time()
        expired = [k for k, v in self._cache.items() if v.expires_at <= now]
        for k in expired:
            self._cache.pop(k, None)
        return len(expired)

    def get_stats(self) -> Dict[str, Any]:
        """Get deduplication statistics."""
        total_cache_hits = sum(e.hit_count for e in self._cache.values())
        return {
            "cache_size": len(self._cache),
            "total_cache_hits": total_cache_hits,
            "history_size": len(self._history),
        }


# Global deduplicator instance
_global_deduplicator: Optional[CommandDeduplicator] = None


def get_deduplicator() -> CommandDeduplicator:
    """Get the global deduplicator instance."""
    global _global_deduplicator
    if _global_deduplicator is None:
        _global_deduplicator = CommandDeduplicator()
    return _global_deduplicator


def set_deduplicator(deduplicator: CommandDeduplicator) -> None:
    """Set the global deduplicator instance."""
    global _global_deduplicator
    _global_deduplicator = deduplicator
