"""YAML skill format adapter — diagnostic skills."""
from __future__ import annotations

from typing import Any, Dict, Optional

from ai.skills.adapters.base import SkillAdapter, SkillSource


class YamlAdapter(SkillAdapter):
    """Adapter for YAML-format diagnostic skills."""

    @property
    def skill_type(self) -> str:
        return "diagnostic"

    def detect(self, file_path: str) -> bool:
        return file_path.endswith(".yaml") or file_path.endswith(".yml")

    def read(self, file_path: str, source_dir: str) -> Optional[SkillSource]:
        raise NotImplementedError("YamlAdapter.read — TODO Task 2")

    def validate(self, data: Dict[str, Any]) -> Optional[str]:
        raise NotImplementedError("YamlAdapter.validate — TODO Task 2")

    def install(self, content: str, parts: Dict[str, str],
                github_url: str, raw_url: str,
                installed_dir: str) -> SkillSource:
        raise NotImplementedError("YamlAdapter.install — TODO Task 2")
