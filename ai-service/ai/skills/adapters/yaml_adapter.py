"""YAML diagnostic skill adapter — handles .yaml files with executable steps."""
from __future__ import annotations

import logging
import os
from datetime import datetime, timezone
from typing import Any, Dict, Optional

import yaml

from ai.skills.adapters.base import SkillAdapter, SkillSource

logger = logging.getLogger(__name__)


class YamlAdapter(SkillAdapter):
    """Handles standard .yaml diagnostic skills with steps / tool / command."""

    @property
    def skill_type(self) -> str:
        return "diagnostic"

    def detect(self, file_path: str) -> bool:
        return file_path.endswith(".yaml")

    def validate(self, data: Dict[str, Any]) -> Optional[str]:
        if not isinstance(data, dict):
            return "not a dict"
        if not data.get("name"):
            return "missing 'name' field"
        steps = data.get("steps")
        if not isinstance(steps, list) or not steps:
            return "missing or empty 'steps' list"
        return None

    def read(self, file_path: str, source_dir: str) -> Optional[SkillSource]:
        try:
            with open(file_path, "r", encoding="utf-8") as f:
                data = yaml.safe_load(f)
        except Exception:
            logger.exception("Failed to read YAML skill: %s", file_path)
            return None
        if not isinstance(data, dict):
            return None
        steps = data.get("steps", [])
        return SkillSource(
            name=data.get("name", ""),
            display_name=data.get("display_name", data.get("name", "")),
            description=data.get("description", ""),
            source_dir=source_dir,
            file_path=file_path,
            risk_level=data.get("risk_level", "low"),
            step_count=len(steps),
            skill_type="diagnostic",
            trigger_patterns=data.get("trigger_patterns", []),
            applicable_components=data.get("applicable_components", []),
            install_meta=data.get("_source", {}),
        )

    def install(self, content: str, parts: Dict[str, str],
                github_url: str, raw_url: str,
                installed_dir: str) -> SkillSource:
        data = yaml.safe_load(content)
        err = self.validate(data)
        if err:
            raise ValueError(f"Invalid skill YAML from {raw_url}: {err}")

        skill_name = data["name"]
        data["_source"] = {
            "type": "github",
            "original_url": github_url,
            "raw_url": raw_url,
            "installed_at": datetime.now(timezone.utc).isoformat(),
        }

        dest = os.path.join(installed_dir, f"{skill_name}.yaml")
        with open(dest, "w", encoding="utf-8") as f:
            yaml.dump(data, f, default_flow_style=False, allow_unicode=True)

        logger.info("Installed skill '%s' from %s → %s", skill_name, github_url, dest)
        return self.read(dest, "installed")
