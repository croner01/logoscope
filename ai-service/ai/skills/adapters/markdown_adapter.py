"""Markdown reference skill adapter — handles SKILL.md directories."""
from __future__ import annotations

import logging
import os
from typing import Any, Dict, Optional

import yaml

from ai.skills.adapters.base import SkillAdapter, SkillSource

logger = logging.getLogger(__name__)


def _parse_front_matter(content: str) -> dict:
    """Parse YAML front matter from a Markdown file."""
    if content.startswith("---"):
        parts = content.split("---", 2)
        if len(parts) >= 3:
            try:
                return yaml.safe_load(parts[1]) or {}
            except yaml.YAMLError:
                return {}
    return {}


def _strip_front_matter(content: str) -> str:
    """Remove YAML front matter, return the Markdown body."""
    if content.startswith("---"):
        parts = content.split("---", 2)
        if len(parts) >= 3:
            return parts[2].strip()
    return content.strip()


class MarkdownAdapter(SkillAdapter):
    """Handles Superpowers-style SKILL.md reference skills.

    Each skill is stored as a directory:
        installed/<skill_name>/
            SKILL.md                  ← main entry point
            root-cause-tracing.md     ← auxiliary files
            defense-in-depth.md
            ...
    """

    @property
    def skill_type(self) -> str:
        return "reference"

    def detect(self, file_path: str) -> bool:
        if file_path.endswith(".md"):
            return True
        if os.path.isdir(file_path):
            return os.path.isfile(os.path.join(file_path, "SKILL.md"))
        return False

    def validate(self, data: Dict[str, Any]) -> Optional[str]:
        if not isinstance(data, dict):
            return "not a dict"
        if not data.get("name"):
            return "missing 'name' field in front matter"
        return None

    def read(self, file_path: str, source_dir: str) -> Optional[SkillSource]:
        md_path: str
        base_dir: Optional[str] = None

        if os.path.isdir(file_path):
            md_path = os.path.join(file_path, "SKILL.md")
            base_dir = file_path
        else:
            md_path = file_path
            base_dir = os.path.dirname(file_path) if os.path.isfile(file_path) else None

        if not os.path.isfile(md_path):
            return None

        with open(md_path, "r", encoding="utf-8") as f:
            content = f.read()

        front = _parse_front_matter(content)
        body = _strip_front_matter(content)

        aux: Dict[str, str] = {}
        if base_dir and os.path.isdir(base_dir):
            for fname in sorted(os.listdir(base_dir)):
                if fname == "SKILL.md" or fname.startswith("."):
                    continue
                fpath = os.path.join(base_dir, fname)
                if os.path.isfile(fpath) and self.detect(fpath):
                    try:
                        with open(fpath, "r", encoding="utf-8") as af:
                            aux[fname] = af.read().strip()
                    except Exception:
                        logger.warning("Failed to read auxiliary file: %s", fpath)

        return SkillSource(
            name=front.get("name", os.path.basename(base_dir or "") or "unknown"),
            display_name=front.get("display_name", front.get("name", "")),
            description=front.get("description", ""),
            source_dir=source_dir,
            file_path=md_path,
            risk_level=front.get("risk_level", "low"),
            step_count=0,
            skill_type="reference",
            trigger_patterns=front.get("trigger_patterns", []),
            applicable_components=front.get("applicable_components", []),
            body=body,
            auxiliary_files=aux,
        )

    def install(self, content: str, parts: Dict[str, str],
                github_url: str, raw_url: str,
                installed_dir: str) -> SkillSource:
        front = _parse_front_matter(content)
        skill_name = front.get("name")
        if not skill_name:
            skill_name = parts["path"].rstrip("/SKILL.md").split("/")[-1] or "unnamed"

        dest_dir = os.path.join(installed_dir, skill_name)
        os.makedirs(dest_dir, exist_ok=True)

        md_path = os.path.join(dest_dir, "SKILL.md")
        with open(md_path, "w", encoding="utf-8") as f:
            f.write(content)

        logger.info("Installed reference skill '%s' from %s → %s/",
                     skill_name, github_url, dest_dir)

        return self.read(dest_dir, "installed")
