"""
ConfigMap YAML hot-reload loader for diagnostic skills.

When skills are deployed as Kubernetes ConfigMaps mounted at
``/etc/ai-skills/`` (or the path specified by env var
``AI_SKILLS_CONFIGMAP_DIR``), this module:

  1. Loads all ``*.yaml`` files at startup and registers the skills they
     define with the global skill registry.
  2. Starts a background thread that watches the file mtimes and re-loads
     any file that has changed (hot-reload without pod restart).

YAML skill format (one per file, or multiple skills in a list):

    # Single skill
    name: my-custom-skill
    display_name: "自定义诊断技能"
    description: "诊断 foo-service 的常见错误"
    applicable_components:
      - foo-service
    trigger_patterns:
      - "foo.*error"
      - "bar.*timeout"
    risk_level: low
    steps:
      - step_id: custom-step-1
        title: "查询 foo-service 错误日志"
        tool: generic_exec
        command: "kubectl logs -n islap -l app=foo-service --tail=100 -A"
        purpose: "获取最近错误日志"
        timeout_s: 20
      - step_id: custom-step-2
        title: "统计 foo 错误频率"
        tool: kubectl_clickhouse_query
        query: >
          SELECT level, count() FROM logs.events
          WHERE service_name='foo-service'
          AND timestamp >= now() - INTERVAL 30 MINUTE
          GROUP BY level FORMAT PrettyCompact
        purpose: "错误频率分析"
        timeout_s: 45
        depends_on:
          - custom-step-1

    ---  # separator for multiple skills in one file

    name: another-skill
    ...
"""

from __future__ import annotations

import logging
import os
import re
import threading
import time
from pathlib import Path
from typing import Any, Dict, List, Optional

logger = logging.getLogger(__name__)

# ── Configuration ─────────────────────────────────────────────────────────────
_DEFAULT_CONFIGMAP_DIR = "/etc/ai-skills"
_POLL_INTERVAL_S = 30  # seconds between mtime checks

# Module-level state
_loader_thread: Optional[threading.Thread] = None
_loaded_files: Dict[str, float] = {}  # path → last mtime
_loader_lock = threading.Lock()


# ──────────────────────────────────────────────────────────────────────────────
# YAML-to-skill bridge
# ──────────────────────────────────────────────────────────────────────────────

def _as_str(v: Any, default: str = "") -> str:
    return default if v is None else (v if isinstance(v, str) else str(v)).strip()


def _build_command_spec(step: Dict[str, Any]) -> Dict[str, Any]:
    """Convert a YAML step dict into a command_spec dict."""
    tool = _as_str(step.get("tool"), "generic_exec")
    timeout_s = int(step.get("timeout_s") or 20)

    if tool == "kubectl_clickhouse_query":
        query = _as_str(step.get("query") or step.get("command"))
        return {
            "tool": "kubectl_clickhouse_query",
            "args": {
                "target_kind": "clickhouse_cluster",
                "target_identity": "database:logs",
                "query": query,
                "timeout_s": timeout_s,
            },
            "command": f"clickhouse-client --query {query!r}",
            "timeout_s": timeout_s,
        }
    else:
        command = _as_str(step.get("command") or step.get("query"))
        return {
            "tool": "generic_exec",
            "args": {
                "command": command,
                "target_kind": "runtime_node",
                "target_identity": "runtime:local",
                "timeout_s": timeout_s,
            },
            "command": command,
            "timeout_s": timeout_s,
        }


def _compile_skill_class(skill_def: Dict[str, Any]):
    """
    Dynamically create a DiagnosticSkill subclass from a YAML skill definition.
    Returns the class (unregistered).
    """
    from ai.skills.base import DiagnosticSkill, SkillContext, SkillStep

    name = _as_str(skill_def.get("name"))
    if not name:
        raise ValueError("Skill definition missing 'name'")

    display_name = _as_str(skill_def.get("display_name"), name)
    description = _as_str(skill_def.get("description"))
    applicable_components = list(skill_def.get("applicable_components") or [])
    risk_level = _as_str(skill_def.get("risk_level"), "low")

    # Compile trigger patterns
    raw_patterns = skill_def.get("trigger_patterns") or []
    compiled_patterns = []
    for pat in raw_patterns:
        try:
            compiled_patterns.append(re.compile(_as_str(pat), re.IGNORECASE))
        except re.error as exc:
            logger.warning("ConfigMapLoader: invalid regex %r in skill %r: %s", pat, name, exc)

    # Build step definitions (captured by closure)
    steps_defs = list(skill_def.get("steps") or [])
    max_steps = max(len(steps_defs), 1)

    def plan_steps(self: DiagnosticSkill, context: SkillContext) -> List[SkillStep]:
        result: List[SkillStep] = []
        ns = _as_str(context.namespace, "islap")
        svc = _as_str(context.service_name)

        for sdef in steps_defs:
            if not isinstance(sdef, dict):
                continue

            # Allow simple {namespace} / {service} substitutions in commands
            cmd_raw = _as_str(sdef.get("command") or sdef.get("query"))
            cmd = cmd_raw.replace("{namespace}", ns).replace("{service}", svc)

            # Re-build spec with substituted command
            substituted = dict(sdef)
            if "command" in substituted:
                substituted["command"] = cmd
            if "query" in substituted:
                substituted["query"] = cmd

            try:
                spec = _build_command_spec(substituted)
            except Exception as exc:
                logger.warning(
                    "ConfigMapLoader: failed to build spec for step %r in skill %r: %s",
                    sdef.get("step_id"),
                    self.name,
                    exc,
                )
                continue

            depends_on = list(sdef.get("depends_on") or [])
            parse_hints = dict(sdef.get("parse_hints") or {})

            result.append(
                SkillStep(
                    step_id=_as_str(sdef.get("step_id"), f"{name}-step-{len(result)+1}"),
                    title=_as_str(sdef.get("title"), _as_str(sdef.get("purpose"))),
                    command_spec=spec,
                    purpose=_as_str(sdef.get("purpose")),
                    depends_on=depends_on,
                    parse_hints=parse_hints,
                )
            )
        return result

    # Build the class dynamically
    skill_cls = type(
        f"ConfigMapSkill_{name.replace('-', '_').replace('.', '_')}",
        (DiagnosticSkill,),
        {
            "name": name,
            "display_name": display_name,
            "description": description,
            "applicable_components": applicable_components,
            "trigger_patterns": compiled_patterns,
            "risk_level": risk_level,
            "max_steps": max_steps,
            "plan_steps": plan_steps,
            "_from_configmap": True,
        },
    )
    return skill_cls


# ──────────────────────────────────────────────────────────────────────────────
# File loading
# ──────────────────────────────────────────────────────────────────────────────

def _parse_yaml_skill_file(path: Path) -> List[Dict[str, Any]]:
    """
    Parse a YAML file and return a list of skill definition dicts.
    Supports both single-skill and multi-document (---) YAML files.
    """
    try:
        import yaml
    except ImportError:
        logger.error("PyYAML not installed; cannot load ConfigMap skills")
        return []

    try:
        with open(path, "r", encoding="utf-8") as fh:
            documents = list(yaml.safe_load_all(fh))
        # Flatten: each document may be a dict (single skill) or list of dicts
        result: List[Dict[str, Any]] = []
        for doc in documents:
            if doc is None:
                continue
            if isinstance(doc, list):
                result.extend(d for d in doc if isinstance(d, dict))
            elif isinstance(doc, dict):
                result.append(doc)
        return result
    except Exception as exc:
        logger.warning("ConfigMapLoader: failed to parse %s: %s", path, exc)
        return []


def _register_skills_from_file(path: Path) -> int:
    """
    Load and register all skills defined in a YAML file.
    Returns the count of successfully registered skills.
    """
    from ai.skills.registry import register_skill, get_registry

    skill_defs = _parse_yaml_skill_file(path)
    if not skill_defs:
        return 0

    registered = 0
    registry = get_registry()

    for sdef in skill_defs:
        if not isinstance(sdef, dict):
            continue
        try:
            skill_cls = _compile_skill_class(sdef)
            # Only register if not already present (or replace if from configmap)
            existing = registry.get(skill_cls.name)
            if existing and not getattr(existing, "_from_configmap", False):
                logger.info(
                    "ConfigMapLoader: skill %r already registered by code; skipping YAML override",
                    skill_cls.name,
                )
                continue
            # Instantiate and register
            instance = skill_cls()
            registry[skill_cls.name] = instance
            registered += 1
            logger.info(
                "ConfigMapLoader: registered skill %r from %s",
                skill_cls.name,
                path.name,
            )
        except Exception as exc:
            logger.warning(
                "ConfigMapLoader: failed to register skill from %s: %s",
                path,
                exc,
                exc_info=True,
            )

    return registered


def _load_all_from_dir(directory: str) -> int:
    """Load all *.yaml files from *directory*. Returns total registered count."""
    dir_path = Path(directory)
    if not dir_path.exists() or not dir_path.is_dir():
        logger.debug("ConfigMapLoader: directory %s not found; skipping", directory)
        return 0

    total = 0
    for yaml_file in sorted(dir_path.glob("*.yaml")):
        count = _register_skills_from_file(yaml_file)
        if count:
            _loaded_files[str(yaml_file)] = yaml_file.stat().st_mtime
            total += count
    return total


# ──────────────────────────────────────────────────────────────────────────────
# Hot-reload background thread
# ──────────────────────────────────────────────────────────────────────────────

def _watch_loop(directory: str, poll_interval: int) -> None:
    """Background thread: poll for mtime changes and reload changed files."""
    logger.info(
        "ConfigMapLoader: watcher started for %s (interval=%ds)",
        directory,
        poll_interval,
    )
    while True:
        time.sleep(poll_interval)
        try:
            dir_path = Path(directory)
            if not dir_path.exists():
                continue

            for yaml_file in sorted(dir_path.glob("*.yaml")):
                path_str = str(yaml_file)
                try:
                    current_mtime = yaml_file.stat().st_mtime
                except OSError:
                    continue

                with _loader_lock:
                    known_mtime = _loaded_files.get(path_str, 0.0)

                if current_mtime > known_mtime + 0.1:
                    logger.info(
                        "ConfigMapLoader: file changed, reloading %s",
                        yaml_file.name,
                    )
                    count = _register_skills_from_file(yaml_file)
                    with _loader_lock:
                        _loaded_files[path_str] = current_mtime
                    logger.info(
                        "ConfigMapLoader: reloaded %d skill(s) from %s",
                        count,
                        yaml_file.name,
                    )
        except Exception as exc:
            logger.warning("ConfigMapLoader: watcher error: %s", exc)


# ──────────────────────────────────────────────────────────────────────────────
# Public API
# ──────────────────────────────────────────────────────────────────────────────

def start_configmap_loader(
    directory: Optional[str] = None,
    poll_interval: int = _POLL_INTERVAL_S,
) -> None:
    """
    Start the ConfigMap skill loader.

    1. Immediately loads all *.yaml files from *directory* (defaults to
       ``AI_SKILLS_CONFIGMAP_DIR`` env var or ``/etc/ai-skills``).
    2. Starts a daemon background thread that polls for mtime changes and
       hot-reloads modified files every *poll_interval* seconds.

    Safe to call multiple times — subsequent calls are no-ops.
    """
    global _loader_thread

    skill_dir = (
        directory
        or os.getenv("AI_SKILLS_CONFIGMAP_DIR", _DEFAULT_CONFIGMAP_DIR)
    )

    # Initial load
    count = _load_all_from_dir(skill_dir)
    if count:
        logger.info(
            "ConfigMapLoader: loaded %d skill(s) from %s at startup",
            count,
            skill_dir,
        )

    # Start watcher thread
    with _loader_lock:
        if _loader_thread is not None and _loader_thread.is_alive():
            return  # already running

        t = threading.Thread(
            target=_watch_loop,
            args=(skill_dir, poll_interval),
            name="ai-skill-configmap-watcher",
            daemon=True,
        )
        t.start()
        _loader_thread = t
        logger.info(
            "ConfigMapLoader: hot-reload watcher thread started (tid=%d)",
            t.ident or 0,
        )
