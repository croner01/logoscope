"""Abstract base for all skill format adapters."""
from __future__ import annotations

from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional


@dataclass
class SkillSource:
    """One skill with its origin metadata. Used by all adapters."""
    name: str
    display_name: str
    description: str
    source_dir: str          # "builtin" | "installed" | "custom"
    file_path: str
    risk_level: str = "low"
    step_count: int = 0
    skill_type: str = "diagnostic"
    trigger_patterns: List[str] = field(default_factory=list)
    applicable_components: List[str] = field(default_factory=list)
    install_meta: Dict[str, Any] = field(default_factory=dict)
    # Reference-type (Markdown) specific
    body: str = ""
    auxiliary_files: Dict[str, str] = field(default_factory=dict)

    @property
    def source_label(self) -> str:
        return {
            "builtin": "内置",
            "installed": "已安装",
            "custom": "自定义",
        }.get(self.source_dir, self.source_dir)


class SkillAdapter(ABC):
    """Base class for skill format adapters."""

    @property
    @abstractmethod
    def skill_type(self) -> str:
        """'diagnostic' | 'reference' — used in API responses."""

    @abstractmethod
    def detect(self, file_path: str) -> bool:
        """Return True if this adapter can handle the given file/directory."""

    @abstractmethod
    def read(self, file_path: str, source_dir: str) -> Optional[SkillSource]:
        """Read skill metadata from a file or directory on disk."""

    @abstractmethod
    def validate(self, data: Dict[str, Any]) -> Optional[str]:
        """Validate parsed content. Return error string or None."""

    @abstractmethod
    def install(self, content: str, parts: Dict[str, str],
                github_url: str, raw_url: str,
                installed_dir: str) -> SkillSource:
        """Install a skill from downloaded content into installed_dir."""
