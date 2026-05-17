"""
Planning node for runtime v4 inner graph.

Key behaviours added in this revision:
  1. Force-inject Phase-1 skill (log_flow_analyzer, priority=100) on the very
     first iteration before any other skill is selected.
  2. Inject Phase-2 skill (cross_component_correlation) on iteration==2 if
     Phase-1 completed with at least one successful step.
  3. On subsequent iterations, read ``state.reflection["failure_hints"]`` and
     generate *alternative* actions for steps that previously failed — avoiding
     the "AI keeps repeating the exact same broken command" bug.
  4. Regular rule-based skill matching for iterations beyond Phase 1/2.
"""

from __future__ import annotations

import logging
from typing import Any, Dict, List, Optional

from ai.runtime_v4.langgraph.state import InnerGraphState

logger = logging.getLogger(__name__)

_PLANNING_MAX_SKILLS = 3
_PLANNING_AUTOSELECT_MIN_SCORE = 0.35

# Names of the mandatory Phase-1 and Phase-2 skills
_PHASE1_SKILL = "log_flow_analyzer"
_PHASE2_SKILL = "cross_component_correlation"


# ──────────────────────────────────────────────────────────────────────────────
# Small utilities
# ──────────────────────────────────────────────────────────────────────────────

def _as_str(value: Any, default: str = "") -> str:
    if value is None:
        return default
    if isinstance(value, str):
        return value
    return str(value)


def _pending_action_count(state: InnerGraphState) -> int:
    return sum(
        1 for a in state.actions
        if isinstance(a, dict) and _as_str(a.get("status")) == "pending"
    )


def _build_skill_context_from_state(state: InnerGraphState):
    """Build a SkillContext from the inner graph state."""
    from ai.skills.base import SkillContext

    ctx_data: Dict[str, Any] = dict(state.skill_context)
    ctx_data.setdefault("question", state.question)
    return SkillContext.from_dict(ctx_data)


# ──────────────────────────────────────────────────────────────────────────────
# Mandatory Phase injection (P1 / P2)
# ──────────────────────────────────────────────────────────────────────────────

def _get_skill_by_name(name: str):
    """Retrieve a registered skill instance by name.  Returns None on miss."""
    try:
        from ai.skills.registry import get_registry
        registry = get_registry()
        return registry.get(name)
    except Exception:
        logger.debug("Skill registry lookup failed for %r", name)
        return None


def _inject_mandatory_skill(
    state: InnerGraphState,
    skill_name: str,
) -> bool:
    """
    Inject a mandatory skill's steps into state.actions if not already done.

    Returns True if steps were actually injected.
    """
    if skill_name in state.selected_skills:
        return False

    skill = _get_skill_by_name(skill_name)
    if skill is None:
        logger.warning(
            "Mandatory skill %r not found in registry — skipping injection", skill_name
        )
        return False

    try:
        context = _build_skill_context_from_state(state)
        steps = skill.plan_steps(context)
    except Exception:
        logger.exception("plan_steps failed for mandatory skill %r", skill_name)
        return False

    state.selected_skills.append(skill_name)
    for step in steps:
        try:
            action = step.to_action_dict(skill_name)
            state.actions.append(action)
        except Exception:
            logger.exception(
                "Failed to serialize step %r from mandatory skill %r",
                step.step_id,
                skill_name,
            )

    logger.info(
        "Planning: injected mandatory skill %r (%d steps) run_id=%s iter=%d",
        skill_name,
        len(steps),
        state.run_id,
        state.iteration,
    )
    return True


def _phase1_succeeded(state: InnerGraphState) -> bool:
    """True if at least one Phase-1 step completed successfully."""
    return any(
        isinstance(e, dict)
        and e.get("success")
        and _as_str(e.get("skill_name")) == _PHASE1_SKILL
        for e in state.evidence
    )


# ──────────────────────────────────────────────────────────────────────────────
# Failure-hint driven alternative action generation
# ──────────────────────────────────────────────────────────────────────────────

def _generate_alternative_actions_from_hints(
    state: InnerGraphState,
    failure_hints: List[Dict[str, Any]],
) -> int:
    """
    For each failure_hint, generate an alternative action using a *different*
    strategy than the one that failed.

    Returns the number of new alternative actions added.
    """
    if not failure_hints:
        return 0

    added = 0
    existing_step_ids = {
        _as_str(a.get("step_id")) for a in state.actions if isinstance(a, dict)
    }

    for hint in failure_hints:
        if not isinstance(hint, dict):
            continue

        original_step_id = _as_str(hint.get("step_id"))
        failure_category = _as_str(hint.get("failure_category"))
        alternative_strategy = _as_str(hint.get("alternative_strategy"))
        skill_name = _as_str(hint.get("skill_name"))

        if not original_step_id or not failure_category:
            continue

        # Build a new step_id for the alternative
        alt_step_id = f"{original_step_id}-alt-{state.iteration}"
        if alt_step_id in existing_step_ids:
            continue  # already generated this alternative

        alt_action = _build_alternative_action(
            state=state,
            original_step_id=original_step_id,
            alt_step_id=alt_step_id,
            failure_category=failure_category,
            alternative_strategy=alternative_strategy,
            skill_name=skill_name,
            hint=hint,
        )

        if alt_action:
            state.actions.append(alt_action)
            existing_step_ids.add(alt_step_id)
            added += 1
            logger.info(
                "Planning: alternative action %r generated for failed step %r "
                "(category=%r strategy=%r) run_id=%s",
                alt_step_id,
                original_step_id,
                failure_category,
                alternative_strategy,
                state.run_id,
            )

    return added


def _build_alternative_action(
    state: InnerGraphState,
    original_step_id: str,
    alt_step_id: str,
    failure_category: str,
    alternative_strategy: str,
    skill_name: str,
    hint: Dict[str, Any],
) -> Optional[Dict[str, Any]]:
    """
    Construct a concrete alternative action dict based on the failure category.

    Failure categories and their alternative strategies:
      resource_not_found   → broaden scope (remove namespace filter, use -A)
      permission_denied    → use read-only alternative command
      command_syntax_error → use simpler command without the offending flag
      connection_failure   → retry via different endpoint / pod selector
      resource_not_ready   → check pod status first instead of querying logs
      empty_output         → widen the time window or remove service filter
      timeout              → reduce result limit or simplify query
      unknown_failure      → use a completely different diagnostic approach
    """
    ns = _as_str(state.skill_context.get("namespace"), "islap")
    original_command = _as_str(hint.get("command"))
    original_purpose = _as_str(hint.get("purpose"))

    alt_title = f"[备选] {_as_str(hint.get('title', original_step_id))}"
    alt_purpose = f"替代失败步骤 {original_step_id!r} ({failure_category}): {original_purpose}"

    # ── Strategy implementations ──────────────────────────────────────────────
    alt_command: Optional[str] = None
    alt_spec: Optional[Dict[str, Any]] = None

    if failure_category == "resource_not_found":
        # Broaden scope: drop namespace-specific filter if command uses -n
        if "-n " in original_command or f"-n {ns}" in original_command:
            import re
            alt_command = re.sub(r"-n\s+\S+\s*", "", original_command).strip()
            alt_command = alt_command.replace("-n islap", "").replace("--namespace=islap", "")
            # Ensure we don't add -A twice
            if "-A" not in alt_command and "kubectl" in alt_command:
                # Insert -A after the kubectl verb
                parts = alt_command.split()
                if len(parts) >= 2:
                    parts.insert(2, "-A")
                    alt_command = " ".join(parts)
        else:
            alt_command = original_command  # can't improve without more context

    elif failure_category == "permission_denied":
        # Fall back to a read-only describe/get instead of exec/log
        if "kubectl exec" in original_command:
            pod_part = original_command.split("kubectl exec")[-1].split("--")[0].strip()
            alt_command = f"kubectl describe pod {pod_part} -n {ns}"
        elif "kubectl logs" in original_command:
            alt_command = original_command.replace("kubectl logs", "kubectl get pods")
        else:
            alt_command = f"kubectl get events -n {ns} --sort-by=.lastTimestamp | tail -n 30"

    elif failure_category == "command_syntax_error":
        # Strip the most common offending flags
        import re
        cleaned = original_command
        # Remove --tail=N from describe (only valid for logs)
        cleaned = re.sub(r"--tail=\d+", "", cleaned)
        # Fix -A position: move before the resource type
        cleaned = re.sub(r"kubectl\s+-A\s+(\w+)", r"kubectl \1 -A", cleaned)
        # Fix logs -l order
        cleaned = re.sub(r"kubectl\s+-A\s+logs", "kubectl logs -A", cleaned)
        alt_command = cleaned.strip()

    elif failure_category == "connection_failure":
        # Try to reach the service via a different method
        if "clickhouse" in original_command.lower():
            # Try a simpler connectivity check
            alt_command = (
                f"kubectl get pods -n {ns} -l app=clickhouse "
                "--no-headers -o custom-columns=NAME:.metadata.name,STATUS:.status.phase"
            )
        else:
            alt_command = f"kubectl get endpoints -n {ns}"

    elif failure_category == "resource_not_ready":
        # Check pod readiness instead of jumping straight to logs
        svc_hint = _as_str(hint.get("service_name") or state.skill_context.get("service_name"))
        if svc_hint:
            safe_svc = svc_hint.replace(" ", "-")
            alt_command = f"kubectl get pods -n {ns} -l app={safe_svc} -o wide"
        else:
            alt_command = f"kubectl get pods -n {ns} -o wide | grep -v Running | head -20"

    elif failure_category == "empty_output":
        # Widen the time window or remove service filter from ClickHouse queries
        if "clickhouse" in original_command.lower() or "FORMAT" in original_command:
            # Replace INTERVAL 30 MINUTE → INTERVAL 2 HOUR, remove service filter
            import re
            wider = re.sub(
                r"INTERVAL\s+\d+\s+(MINUTE|HOUR)",
                "INTERVAL 2 HOUR",
                original_command,
                flags=re.IGNORECASE,
            )
            # Remove service_name = '...' filter
            wider = re.sub(r"AND service_name = '[^']*'", "", wider)
            alt_command = wider.strip()
        else:
            # Just run without namespace restriction
            alt_command = original_command.replace(f"-n {ns}", "-A").strip()

    elif failure_category == "timeout":
        # Reduce the result set size
        import re
        reduced = re.sub(r"LIMIT\s+\d+", "LIMIT 20", original_command, flags=re.IGNORECASE)
        # Shorten time windows
        reduced = re.sub(
            r"INTERVAL\s+(\d+)\s+HOUR",
            lambda m: f"INTERVAL {max(1, int(m.group(1))//2)} HOUR",
            reduced,
            flags=re.IGNORECASE,
        )
        reduced = re.sub(
            r"INTERVAL\s+(\d+)\s+MINUTE",
            lambda m: f"INTERVAL {max(5, int(m.group(1))//2)} MINUTE",
            reduced,
            flags=re.IGNORECASE,
        )
        alt_command = reduced.strip()

    else:
        # unknown_failure: use a generic diagnostic fallback
        alt_command = (
            f"kubectl get events -n {ns} "
            "--sort-by=.lastTimestamp "
            "--field-selector=type=Warning "
            "| tail -n 50"
        )

    if alt_command is None or alt_command == original_command:
        return None  # couldn't generate a meaningful alternative

    # Build the spec (same tool type as original if detectable, else generic_exec)
    original_spec = hint.get("command_spec") or {}
    original_tool = _as_str((original_spec or {}).get("tool"))

    if original_tool == "kubectl_clickhouse_query":
        alt_spec = {
            "tool": "kubectl_clickhouse_query",
            "args": {
                "target_kind": "clickhouse_cluster",
                "target_identity": "database:logs",
                "query": alt_command,
                "timeout_s": 60,
            },
            "command": alt_command,
            "timeout_s": 60,
        }
    else:
        alt_spec = {
            "tool": "generic_exec",
            "args": {
                "command": alt_command,
                "target_kind": "runtime_node",
                "target_identity": "runtime:local",
                "timeout_s": 25,
            },
            "command": alt_command,
            "timeout_s": 25,
        }

    return {
        "step_id": alt_step_id,
        "skill_name": skill_name or "planning_recovery",
        "title": alt_title,
        "command_spec": alt_spec,
        "purpose": alt_purpose,
        "depends_on": [],  # alternatives run independently
        "parse_hints": hint.get("parse_hints") or {},
        "status": "pending",
        "is_alternative": True,
        "replaces_step_id": original_step_id,
        "failure_category": failure_category,
    }


# ──────────────────────────────────────────────────────────────────────────────
# Regular skill matching (iterations 3+)
# ──────────────────────────────────────────────────────────────────────────────

def _select_skills_by_rules(state: InnerGraphState) -> List[Any]:
    """Run rule-based skill matching and return (skill, score) pairs."""
    try:
        from ai.skills.matcher import extract_auto_selected_skills

        context = _build_skill_context_from_state(state)
        selected = extract_auto_selected_skills(
            context,
            threshold=_PLANNING_AUTOSELECT_MIN_SCORE,
            max_skills=_PLANNING_MAX_SKILLS,
        )
        return [(skill, skill.match_score(context)) for skill in selected]
    except Exception:
        logger.exception("Rule-based skill matching failed")
        return []


def _populate_actions_from_skills(
    state: InnerGraphState,
    skill_score_pairs: List[Any],
) -> None:
    """Expand each selected skill into SkillStep action dicts."""
    try:
        context = _build_skill_context_from_state(state)
    except Exception:
        return

    for skill, score in skill_score_pairs:
        if score < _PLANNING_AUTOSELECT_MIN_SCORE:
            continue
        if skill.name in state.selected_skills:
            continue  # avoid duplicates across iterations
        # Never re-inject the Phase-1/2 skills via the regular path
        if skill.name in (_PHASE1_SKILL, _PHASE2_SKILL):
            continue
        try:
            steps = skill.plan_steps(context)
        except Exception:
            logger.exception("plan_steps failed for skill %r", skill.name)
            continue

        state.selected_skills.append(skill.name)
        for step in steps:
            try:
                action = step.to_action_dict(skill.name)
                state.actions.append(action)
            except Exception:
                logger.exception(
                    "Failed to serialize step %r from skill %r",
                    step.step_id,
                    skill.name,
                )


# ──────────────────────────────────────────────────────────────────────────────
# Main planning node
# ──────────────────────────────────────────────────────────────────────────────

def run_planning(state: InnerGraphState) -> InnerGraphState:
    """
    Planning node.

    Iteration flow:
      iter==1 → Force-inject Phase-1 (log_flow_analyzer).
      iter==2 → Force-inject Phase-2 (cross_component_correlation) if Phase-1
                succeeded; also process failure_hints for alternative actions.
      iter>=3 → Read failure_hints → generate alternatives, then run regular
                skill matching for any remaining diagnostic gaps.
      Any iter→ If pending actions already exist, pass through to acting node.
      Any iter→ If max_iterations reached, mark done.
    """
    state.phase = "planning"
    state.iteration += 1

    if state.iteration > state.max_iterations:
        state.done = True
        logger.debug(
            "Planning: max iterations %d reached, done=True run_id=%s",
            state.max_iterations,
            state.run_id,
        )
        return state

    # ── If there are already pending actions, let acting handle them ──────────
    if _pending_action_count(state) > 0:
        logger.debug(
            "Planning: %d pending actions remain, passing to acting run_id=%s",
            _pending_action_count(state),
            state.run_id,
        )
        return state

    injected_count = 0

    # ── Iteration 1: Force-inject Phase-1 ────────────────────────────────────
    if state.iteration == 1:
        if _inject_mandatory_skill(state, _PHASE1_SKILL):
            injected_count += 1
        state.reflection["last_skill_selection"] = {
            "iteration": state.iteration,
            "selected": [_PHASE1_SKILL],
            "phase": "phase1_forced",
        }
        return state  # let acting run Phase-1 steps before proceeding

    # ── Iteration 2: Force-inject Phase-2 (if Phase-1 succeeded) ─────────────
    if state.iteration == 2:
        if _phase1_succeeded(state):
            if _inject_mandatory_skill(state, _PHASE2_SKILL):
                injected_count += 1
                state.reflection["last_skill_selection"] = {
                    "iteration": state.iteration,
                    "selected": [_PHASE2_SKILL],
                    "phase": "phase2_forced",
                }
        else:
            logger.info(
                "Planning: Phase-1 did not succeed — skipping Phase-2 injection run_id=%s",
                state.run_id,
            )

        # Also handle any failure hints from Phase-1 steps
        failure_hints = list(state.reflection.get("failure_hints") or [])
        if failure_hints:
            alt_count = _generate_alternative_actions_from_hints(state, failure_hints)
            logger.info(
                "Planning iter=2: generated %d alternative actions from %d failure hints run_id=%s",
                alt_count,
                len(failure_hints),
                state.run_id,
            )

        if injected_count == 0 and _pending_action_count(state) == 0:
            # Phase-2 was skipped and no alternatives — fall through to regular matching
            pass
        else:
            return state

    # ── Iterations 3+: failure_hints + regular skill matching ─────────────────
    failure_hints = list(state.reflection.get("failure_hints") or [])
    alt_count = 0
    if failure_hints:
        alt_count = _generate_alternative_actions_from_hints(state, failure_hints)
        if alt_count > 0:
            logger.info(
                "Planning iter=%d: generated %d alternative actions from hints run_id=%s",
                state.iteration,
                alt_count,
                state.run_id,
            )

    # Regular skill matching to fill remaining diagnostic gaps
    matched = _select_skills_by_rules(state)
    if matched:
        skill_names = [s.name for s, _ in matched]
        logger.info(
            "Planning: matched skills %s for run_id=%s iter=%d",
            skill_names,
            state.run_id,
            state.iteration,
        )
        _populate_actions_from_skills(state, matched)
        state.reflection["last_skill_selection"] = {
            "iteration": state.iteration,
            "selected": skill_names,
            "alternative_actions_added": alt_count,
        }
    else:
        logger.info(
            "Planning: no additional skills matched run_id=%s iter=%d",
            state.run_id,
            state.iteration,
        )
        if not state.actions and alt_count == 0:
            state.done = True

    return state
