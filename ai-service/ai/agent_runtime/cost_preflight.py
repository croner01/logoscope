"""
Cost preflight gate for AI agent commands.

Estimates command cost and decides whether to auto-execute,
warn, or block pending manual approval.
"""
from __future__ import annotations

import re
from dataclasses import dataclass
from enum import Enum
from typing import Any, Dict


class Decision(Enum):
    AUTO = "auto"
    WARN = "warn"
    BLOCK = "block"


@dataclass
class PreflightResult:
    decision: Decision
    reason: str = ""
    estimated_cost: Dict[str, Any] | None = None


def _as_str(value: Any, default: str = "") -> str:
    if value is None:
        return default
    if isinstance(value, str):
        return value
    return str(value)


def _as_int(value: Any, default: int = 0) -> int:
    try:
        return int(value)
    except Exception:
        return int(default)


_ALL_NAMESPACES_PATTERN = re.compile(r"(?:\s|^)-(?:-all-namespaces|A)(?:\s|$)")
_LARGE_TIME_WINDOW_RE = re.compile(
    r"(?:INTERVAL\s+'?\s*(\d+)\s*(?:DAY|MONTH|WEEK))",
    re.IGNORECASE,
)
_SCAN_COUNT_RE = re.compile(r"(?:COUNT|count)\(\*\)", re.IGNORECASE)


class CostPreflight:
    """Estimates command cost and decides auto/warn/block."""

    # Default thresholds (configurable via kwargs)
    DEFAULT_THRESHOLDS: Dict[str, Any] = {
        "estimated_rows": 100_000,
        "time_window_days": 1,
        "target_nodes": 3,
        "session_command_limit": 10,
        "all_namespaces_block": True,
    }

    def __init__(self, **thresholds: Any):
        self.thresholds = {**self.DEFAULT_THRESHOLDS, **thresholds}

    def evaluate(self, command_spec: Dict[str, Any], cost_tracker: Dict[str, Any]) -> PreflightResult:
        """Evaluate a command_spec against cost thresholds.

        Args:
            command_spec: Normalized command spec dict.
            cost_tracker: Session cost state from AgentRun.summary_json.cost_tracker.

        Returns:
            PreflightResult with decision and reason.
        """
        safe_spec = command_spec if isinstance(command_spec, dict) else {}
        safe_tracker = cost_tracker if isinstance(cost_tracker, dict) else {}

        args = safe_spec.get("args") if isinstance(safe_spec.get("args"), dict) else {}
        command = _as_str(args.get("command") or safe_spec.get("command")).strip()
        query = _as_str(args.get("query") or safe_spec.get("query")).strip()

        checks: list[tuple[bool, str]] = []

        # ── Session command limit ──────────────────────────────────────────
        executed = _as_int(safe_tracker.get("commands_executed"), 0)
        limit = _as_int(self.thresholds.get("session_command_limit"), 10)
        if executed >= limit:
            checks.append((True, f"会话已执行 {executed} 条命令，达到上限 {limit}"))

        # ── All-namespaces check ───────────────────────────────────────────
        if command and self.thresholds.get("all_namespaces_block"):
            if _ALL_NAMESPACES_PATTERN.search(command):
                checks.append((True, "命令包含 --all-namespaces/-A，范围过大"))

        # ── Large time window check ────────────────────────────────────────
        time_text = command + " " + query
        time_match = _LARGE_TIME_WINDOW_RE.search(time_text)
        if time_match:
            days = int(time_match.group(1))
            max_days = _as_int(self.thresholds.get("time_window_days"), 1)
            if days > max_days:
                checks.append((True, f"查询时间窗口 {days} 天超过限制 {max_days} 天"))

        # ── Full scan check ────────────────────────────────────────────────
        if query and _SCAN_COUNT_RE.search(query) and "WHERE" not in query.upper():
            checks.append((True, "全表 COUNT 扫描可能代价较高"))

        # ── Node scope check ───────────────────────────────────────────────
        if command and ("describe nodes" in command.lower() or "get nodes" in command.lower()):
            target_kind = _as_str(
                args.get("target_kind") or safe_spec.get("target_kind")
            ).strip()
            if target_kind == "k8s_cluster":
                checks.append((True, "全集群节点查询范围过大"))

        # ── Combine results ─────────────────────────────────────────────────
        if checks:
            reasons = [reason for triggered, reason in checks if triggered]
            return PreflightResult(
                decision=Decision.BLOCK,
                reason="; ".join(reasons),
                estimated_cost={"triggers": len(checks)},
            )

        return PreflightResult(
            decision=Decision.AUTO,
            reason="代价在阈值以内",
        )


__all__ = ["CostPreflight", "Decision", "PreflightResult"]
