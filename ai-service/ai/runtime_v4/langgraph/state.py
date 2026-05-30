"""
LangGraph inner-loop state definitions.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Dict, List


@dataclass
class InnerGraphState:
    run_id: str
    question: str
    iteration: int = 0
    max_iterations: int = 4
    phase: str = "planning"
    actions: List[Dict[str, Any]] = field(default_factory=list)
    observations: List[Dict[str, Any]] = field(default_factory=list)
    reflection: Dict[str, Any] = field(default_factory=dict)
    done: bool = False
    # Skill-related fields (ai-runtime-lab)
    skill_context: Dict[str, Any] = field(default_factory=dict)
    selected_skills: List[str] = field(default_factory=list)
    evidence: List[Dict[str, Any]] = field(default_factory=list)
