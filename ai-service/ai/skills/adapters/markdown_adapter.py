"""Markdown skill format adapter — reference skills."""
from __future__ import annotations

from typing import Any, Dict, Optional

from ai.skills.adapters.base import SkillAdapter, SkillSource


class MarkdownAdapter(SkillAdapter):
    """Adapter for Markdown-format reference skills."""

    @property
    def skill_type(self) -> str:
        return "reference"

    def detect(self, file_path: str) -> bool:
        return file_path.endswith(".md")

    def read(self, file_path: str, source_dir: str) -> Optional[SkillSource]:
        raise NotImplementedError("MarkdownAdapter.read — TODO Task 3")

    def validate(self, data: Dict[str, Any]) -> Optional[str]:
        raise NotImplementedError("MarkdownAdapter.validate — TODO Task 3")

    def install(self, content: str, parts: Dict[str, str],
                github_url: str, raw_url: str,
                installed_dir: str) -> SkillSource:
        raise NotImplementedError("MarkdownAdapter.install — TODO Task 3")
