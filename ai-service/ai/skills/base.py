"""
Skill base classes and data models.

A DiagnosticSkill encapsulates a focused diagnostic strategy: it knows
when to activate (trigger patterns / component types) and how to execute
(ordered SkillSteps that compile to command_specs).
"""

from __future__ import annotations

import re
from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from typing import Any, Dict, List


def _as_str(value: Any, default: str = "") -> str:
    if value is None:
        return default
    if isinstance(value, str):
        return value.strip()
    return str(value).strip()


def _as_list(value: Any) -> List[Any]:
    return value if isinstance(value, list) else []


@dataclass
class SkillContext:
    """Runtime context passed to a skill for step generation."""

    # ── Core fields ──────────────────────────────────────────────────────────
    question: str = ""
    service_name: str = ""
    log_content: str = ""
    log_level: str = ""
    component_type: str = ""
    trace_id: str = ""
    namespace: str = "islap"
    previous_observations: List[Dict[str, Any]] = field(default_factory=list)
    extra: Dict[str, Any] = field(default_factory=dict)

    # ── Cross-component correlation fields (Phase 2) ──────────────────────────
    # OpenStack X-Request-ID (req-xxxxxxxx-xxxx-...) or generic request_id
    request_id: str = ""
    # OpenStack specific req-xxx format, distinct from generic request_id
    os_request_id: str = ""
    # ISO timestamp of the triggering log entry
    log_timestamp: str = ""
    # Best available anchor kind: "trace_id" | "request_id" | "os_request_id" | "time_window"
    correlation_anchor: str = ""
    # Corresponding anchor value
    correlation_anchor_value: str = ""
    # Components identified as relevant from the triggering log
    related_components: List[str] = field(default_factory=list)
    # Structured data-flow produced by log_flow_analyzer (Phase 1)
    data_flow: List[Dict[str, Any]] = field(default_factory=list)
    # Evidence time-window boundaries (ISO strings) populated by Phase 1
    evidence_window_start: str = ""
    evidence_window_end: str = ""

    @classmethod
    def from_dict(cls, data: Dict[str, Any]) -> "SkillContext":
        """Build from analysis_context dict."""
        safe = data if isinstance(data, dict) else {}

        # Resolve the best correlation anchor
        trace_id = _as_str(safe.get("trace_id"))
        request_id = _as_str(safe.get("request_id") or safe.get("X-Request-ID", ""))
        os_request_id = _as_str(safe.get("os_request_id") or safe.get("openstack_request_id", ""))

        if trace_id:
            anchor, anchor_value = "trace_id", trace_id
        elif os_request_id:
            anchor, anchor_value = "os_request_id", os_request_id
        elif request_id:
            anchor, anchor_value = "request_id", request_id
        else:
            anchor, anchor_value = "time_window", ""

        return cls(
            question=_as_str(safe.get("question")),
            service_name=_as_str(safe.get("service_name")),
            log_content=_as_str(safe.get("log_content") or safe.get("message", "")),
            log_level=_as_str(safe.get("log_level") or safe.get("level", "")),
            component_type=_as_str(safe.get("component_type")),
            trace_id=trace_id,
            namespace=_as_str(safe.get("namespace"), "islap"),
            previous_observations=_as_list(safe.get("previous_observations")),
            extra=safe,
            # correlation fields
            request_id=request_id,
            os_request_id=os_request_id,
            log_timestamp=_as_str(safe.get("log_timestamp") or safe.get("timestamp", "")),
            correlation_anchor=_as_str(safe.get("correlation_anchor")) or anchor,
            correlation_anchor_value=_as_str(safe.get("correlation_anchor_value")) or anchor_value,
            related_components=_as_list(safe.get("related_components")),
            data_flow=_as_list(safe.get("data_flow")),
            evidence_window_start=_as_str(safe.get("evidence_window_start", "")),
            evidence_window_end=_as_str(safe.get("evidence_window_end", "")),
        )

    def combined_text(self) -> str:
        """All searchable text merged for pattern matching."""
        parts = [self.question, self.log_content, self.service_name, self.component_type]
        return " ".join(p for p in parts if p)


@dataclass
class SkillStep:
    """
    One diagnostic command within a skill chain.

    ``command_spec`` must be compatible with
    ``ai.followup_command_spec.compile_followup_command_spec()``.
    Two tool types are supported:
    - ``generic_exec``: shell commands (kubectl, curl, grep, ...)
    - ``kubectl_clickhouse_query``: ClickHouse SQL via kubectl exec
    """

    step_id: str
    title: str
    command_spec: Dict[str, Any]
    purpose: str
    depends_on: List[str] = field(default_factory=list)
    parse_hints: Dict[str, Any] = field(default_factory=dict)

    def to_action_dict(self, skill_name: str) -> Dict[str, Any]:
        """Serialize as an action entry for InnerGraphState.actions."""
        return {
            "step_id": self.step_id,
            "skill_name": skill_name,
            "title": self.title,
            "command_spec": self.command_spec,
            "purpose": self.purpose,
            "depends_on": list(self.depends_on),
            "parse_hints": dict(self.parse_hints),
            "status": "pending",
        }


@dataclass
class SkillMatchDetails:
    """Detailed rule-based match result for a skill/context pair."""

    pattern_hits: int = 0
    pattern_score: float = 0.0
    component_bonus: float = 0.0
    total_score: float = 0.0


class DiagnosticSkill(ABC):
    """
    Base class for all diagnostic skills.

    Subclasses define:
    - Static metadata (name, display_name, description, ...)
    - Trigger patterns (regexes that activate this skill)
    - ``plan_steps()`` -- the core logic that produces the command chain
    """

    name: str = ""
    display_name: str = ""
    description: str = ""
    applicable_components: List[str] = []
    trigger_patterns: List[re.Pattern] = []
    risk_level: str = "low"
    max_steps: int = 4

    @abstractmethod
    def plan_steps(self, context: SkillContext) -> List[SkillStep]:
        """Generate ordered diagnostic steps for the given context."""

    def _count_pattern_hits(self, text: str) -> int:
        """Count regex hits against the merged context text."""
        import re as _re

        pattern_hits = 0
        for pattern in self.trigger_patterns:
            # Accept either compiled re.Pattern or plain string (auto-compile)
            if isinstance(pattern, str):
                if _re.search(pattern, text, _re.IGNORECASE):
                    pattern_hits += 1
            elif hasattr(pattern, "search"):
                if pattern.search(text):
                    pattern_hits += 1
        return pattern_hits

    def _component_bonus(self, context: SkillContext) -> float:
        """Small relevance boost when component_type and skill scope align."""
        if not context.component_type or not self.applicable_components:
            return 0.0

        ct_lower = context.component_type.lower()
        for comp in self.applicable_components:
            if comp.lower() in ct_lower or ct_lower in comp.lower():
                return 0.2
        return 0.0

    def match_details(self, context: SkillContext) -> SkillMatchDetails:
        """Return detailed rule-based matching evidence for this context."""
        text = context.combined_text().lower()
        if not text:
            return SkillMatchDetails()

        pattern_hits = self._count_pattern_hits(text)
        pattern_score = min(1.0, pattern_hits / max(1, len(self.trigger_patterns)))
        component_bonus = self._component_bonus(context)
        total_score = min(1.0, pattern_score + component_bonus)

        return SkillMatchDetails(
            pattern_hits=pattern_hits,
            pattern_score=pattern_score,
            component_bonus=component_bonus,
            total_score=total_score,
        )

    def match_score(self, context: SkillContext) -> float:
        """Return a relevance score in [0.0, 1.0] for this context."""
        return self.match_details(context).total_score

    def to_catalog_entry(self) -> Dict[str, Any]:
        """Serialized form for LLM prompt injection."""
        return {
            "name": self.name,
            "display_name": self.display_name,
            "description": self.description,
            "applicable_components": list(self.applicable_components),
            "risk_level": self.risk_level,
            "max_steps": self.max_steps,
        }

    def __repr__(self) -> str:
        return f"<{self.__class__.__name__} name={self.name!r}>"
