"""First-phase OpenHands-backed runtime v4 inner backend."""

from __future__ import annotations

from typing import Any, Dict, List

from ai.runtime_v4.backend.base import RuntimeBackendRequest, RuntimeBackendResult
from ai.runtime_v4.backend.tool_adapter import (
    map_skill_step_to_runtime_command,
    map_tool_call_to_runtime_command,
)


def _as_bool(value: Any, default: bool = False) -> bool:
    if value is None:
        return bool(default)
    if isinstance(value, bool):
        return value
    if isinstance(value, str):
        return value.strip().lower() in {"1", "true", "yes", "on"}
    return bool(value)


def _as_int(value: Any, default: int = 0) -> int:
    try:
        if value is None:
            return int(default)
        return int(str(value).strip())
    except Exception:
        return int(default)


def _as_str(value: Any, default: str = "") -> str:
    if value is None:
        return default
    if isinstance(value, str):
        return value
    return str(value)


def _as_list(value: Any) -> List[Any]:
    return value if isinstance(value, list) else []


def _as_dict(value: Any) -> Dict[str, Any]:
    return value if isinstance(value, dict) else {}


def _build_skill_context(request: RuntimeBackendRequest):
    from ai.skills.base import SkillContext

    ctx_data: Dict[str, Any] = dict(request.analysis_context or {})
    ctx_data.setdefault("question", request.question)
    return SkillContext.from_dict(ctx_data)


def _select_skill_steps(request: RuntimeBackendRequest) -> tuple[List[str], List[Dict[str, Any]]]:
    try:
        import ai.skills  # noqa: F401 - importing registers built-in skills
        from ai.skills.matcher import extract_auto_selected_skills
    except Exception:
        return [], []

    context = _build_skill_context(request)
    max_skills = max(1, min(_as_int(request.runtime_options.get("max_skills"), 3), 5))
    threshold = float(request.runtime_options.get("skill_threshold") or 0.35)
    selected = extract_auto_selected_skills(
        context,
        threshold=max(0.0, min(threshold, 1.0)),
        max_skills=max_skills,
    )
    selected_names: List[str] = []
    tool_calls: List[Dict[str, Any]] = []
    for skill in selected:
        selected_names.append(skill.name)
        try:
            steps = skill.plan_steps(context)
        except Exception:
            continue
        for step in steps[: max(1, int(getattr(skill, "max_steps", 4) or 4))]:
            try:
                tool_calls.append(
                    map_skill_step_to_runtime_command(
                        run_id=request.run_id,
                        skill_name=skill.name,
                        step=step.to_action_dict(skill.name),
                    )
                )
            except Exception:
                continue
    return selected_names, tool_calls


def _map_provider_tool_call(
    *,
    run_id: str,
    item: Dict[str, Any],
) -> Dict[str, Any]:
    safe_item = _as_dict(item)
    safe_command_spec = _as_dict(safe_item.get("command_spec"))
    args = _as_dict(safe_command_spec.get("args"))
    timeout_seconds = max(
        3,
        min(
            _as_int(
                safe_item.get("timeout_seconds")
                or args.get("timeout_s")
                or safe_item.get("timeout_s"),
                20,
            ),
            180,
        ),
    )
    if _as_str(safe_item.get("tool_name")).strip() == "command.exec" and safe_command_spec:
        mapped = {
            "run_id": _as_str(run_id),
            "tool_name": "command.exec",
            "command": _as_str(safe_item.get("command")).strip(),
            "purpose": _as_str(safe_item.get("purpose") or "OpenHands requested command"),
            "title": _as_str(safe_item.get("title") or "OpenHands 工具调用"),
            "timeout_seconds": timeout_seconds,
            "command_spec": safe_command_spec,
            "confirmed": False,
            "elevated": False,
        }
    else:
        mapped = map_tool_call_to_runtime_command(
            run_id=run_id,
            tool_name=_as_str(safe_item.get("tool_name") or safe_item.get("tool") or "generic_exec"),
            tool_args=(
                _as_dict(safe_item.get("tool_args"))
                or _as_dict(safe_item.get("arguments"))
                or _as_dict(safe_item.get("args"))
                or safe_item
            ),
        )
    for key in ("action_id", "skill_name", "step_id"):
        value = _as_str(safe_item.get(key)).strip()
        if value:
            mapped[key] = value
    return mapped


def _default_backend_payload(request: RuntimeBackendRequest) -> Dict[str, Any]:
    readonly = _as_bool(request.runtime_options.get("auto_exec_readonly"), default=True)
    enable_skills = _as_bool(request.runtime_options.get("enable_skills"), default=True)
    selected_skills: List[str] = []
    tool_calls: List[Dict[str, Any]] = []
    if enable_skills:
        selected_skills, tool_calls = _select_skill_steps(request)
    if readonly:
        tool_calls.append(
            map_tool_call_to_runtime_command(
                run_id=request.run_id,
                tool_name="generic_exec",
                tool_args={
                    "command": "kubectl -n islap get pods",
                    "purpose": "bootstrap readonly cluster inventory",
                    "target_kind": "k8s_cluster",
                    "target_identity": "namespace:islap",
                    "timeout_s": 20,
                },
            )
        )
    return {
        "provider": "static-fallback",
        "mode": "readonly" if readonly else "approval_gated",
        "question": request.question,
        "analysis_context": dict(request.analysis_context or {}),
        "tool_calls": tool_calls,
        "selected_skills": selected_skills,
        "thoughts": [],
    }


def _merge_tool_calls(
    provider_tool_calls: List[Dict[str, Any]],
    skill_tool_calls: List[Dict[str, Any]],
) -> List[Dict[str, Any]]:
    if not provider_tool_calls:
        return list(skill_tool_calls)
    if not skill_tool_calls:
        return list(provider_tool_calls)
    return list(provider_tool_calls) + list(skill_tool_calls)


def _merge_unique_strings(preferred: List[str], fallback: List[str]) -> List[str]:
    merged: List[str] = []
    for item in list(preferred) + list(fallback):
        value = _as_str(item).strip()
        if value and value not in merged:
            merged.append(value)
    return merged


class OpenHandsBackend:
    """Minimal OpenHands backend skeleton for runtime v4 integration."""

    def backend_name(self) -> str:
        return "openhands-v1"

    def run(self, request: RuntimeBackendRequest) -> RuntimeBackendResult:
        try:
            from ai.runtime_v4.backend.openhands_provider import get_openhands_provider

            provider = get_openhands_provider()
            provider_result = _as_dict(provider.run(request))
        except Exception:
            provider = None
            provider_result = {}

        payload = _default_backend_payload(request)
        if provider_result:
            payload["provider"] = (
                _as_str(provider_result.get("provider")).strip()
                or _as_str(provider_result.get("provider_name")).strip()
                or _as_str(provider.__class__.__name__ if provider is not None else "").strip()
                or "provider"
            )
            payload["mode"] = _as_str(provider_result.get("mode")).strip() or _as_str(payload.get("mode"))
            provider_selected_skills = [
                _as_str(item).strip()
                for item in _as_list(provider_result.get("selected_skills"))
                if _as_str(item).strip()
            ]
            payload["selected_skills"] = _merge_unique_strings(
                provider_selected_skills,
                [_as_str(item).strip() for item in _as_list(payload.get("selected_skills")) if _as_str(item).strip()],
            )
            payload["thoughts"] = [
                _as_str(item).strip()
                for item in _as_list(provider_result.get("thoughts"))
                if _as_str(item).strip()
            ]
            provider_tool_calls = [
                _map_provider_tool_call(run_id=request.run_id, item=_as_dict(item))
                for item in _as_list(provider_result.get("tool_calls"))
                if isinstance(item, dict)
            ]
            payload["tool_calls"] = _merge_tool_calls(provider_tool_calls, _as_list(payload.get("tool_calls")))

        return RuntimeBackendResult(
            inner_engine=self.backend_name(),
            payload=payload,
        )
