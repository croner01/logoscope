"""
Skill lifecycle manager — install, create, remove, update.

Manages three skill directories with decreasing priority:

+-------------------+--------------------------------------------------+
| Directory         | Purpose                                          |
+-------------------+--------------------------------------------------+
| ``builtin/``      | Shipped with the image (read-only, 7 skills)     |
| ``installed/``    | Downloaded from GitHub via ``skill install``      |
| ``custom/``       | Created by the user via ``skill create``          |
+-------------------+--------------------------------------------------+

On name collision: custom wins > installed > builtin.
All three share the same YAML format and the same loader entry.
"""
from __future__ import annotations

import json
import logging
import os
import re
import shutil
import time
from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional, Tuple

from ai.skills.adapters import SkillSource
from ai.skills.base import SkillContext
from ai.skills.builtin._helpers import _as_str

logger = logging.getLogger(__name__)

# ── Default directories ─────────────────────────────────────────────────────

BASE_DIR = os.path.abspath(os.path.join(os.path.dirname(__file__)))
BUILTIN_DIR = os.path.join(BASE_DIR, "builtin")
INSTALLED_DIR = os.getenv("LOGOSCOPE_SKILLS_INSTALLED", os.path.join(BASE_DIR, "installed"))
CUSTOM_DIR = os.getenv("LOGOSCOPE_SKILLS_CUSTOM", os.path.join(BASE_DIR, "custom"))

# ── GitHub URL patterns ─────────────────────────────────────────────────────

_GITHUB_URL_RE = re.compile(
    r"^github://"
    r"(?P<owner>[^/]+)/(?P<repo>[^/@]+)"
    r"(?:@(?P<ref>[^/]+))?"
    r"(?:/(?P<path>.+))?$"
)
_RAW_GITHUB_URL = "https://raw.githubusercontent.com/{owner}/{repo}/{ref}/{path}"

GITHUB_API_BASE = "https://api.github.com"


@dataclass
class InstallMeta:
    """Installation tracking metadata stored in the YAML ``_source`` field."""
    source_url: str = ""
    installed_at: str = ""
    file_path: str = ""


# ── GitHub URL helpers ──────────────────────────────────────────────────────

def _normalize_github_url(url: str) -> str:
    """Convert ``https://github.com/...`` URLs to ``github://`` format."""
    url = url.strip()
    if not url.startswith(("https://github.com/", "http://github.com/")):
        return url

    m = re.match(
        r"^https?://github\.com/"
        r"(?P<owner>[^/]+)"
        r"(?:/(?P<repo>[^/@]+))?"
        r"(?:/(?:blob|tree)/(?P<ref>[^/]+))?"
        r"(?:/(?P<path>.+))?"
        r"$",
        url,
    )
    if not m or not m.group("repo"):
        return url  # Can't parse — let the original validation fail

    owner, repo, ref, path = (
        m.group("owner"), m.group("repo"),
        m.group("ref"), m.group("path"),
    )
    # Rebuild as github://owner/repo[@ref][/path]
    tail = ""
    if ref:
        tail = f"@{ref}"
    if path:
        tail += f"/{path}"
    return f"github://{owner}/{repo}{tail}"


def parse_github_url(url: str) -> Optional[Dict[str, str]]:
    """Parse a GitHub skill URL into its components.

    Accepts both formats:
    - ``github://owner/repo/path/to/skill.yaml[@ref]``
    - ``https://github.com/owner/repo[/blob/ref]/path/to/skill.yaml``

    Returns:
        Dict with keys (owner, repo, ref, path) or None if invalid.
    """
    m = _GITHUB_URL_RE.match(_normalize_github_url(url).strip())
    if not m:
        return None
    return {
        "owner": m.group("owner"),
        "repo": m.group("repo"),
        "ref": m.group("ref") or "main",
        "path": m.group("path") or "",
    }


def build_raw_url(parts: Dict[str, str]) -> str:
    """Build a raw.githubusercontent.com download URL from parsed parts."""
    return _RAW_GITHUB_URL.format(**parts)


def _download_file(url: str, timeout: int = 30) -> Optional[str]:
    """Download a file from *url* and return its text content, or None."""
    import urllib.request
    import urllib.error

    try:
        req = urllib.request.Request(url, headers={"User-Agent": "logoscope/1.0"})
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            return resp.read().decode("utf-8")
    except urllib.error.HTTPError as e:
        logger.warning("HTTP %d downloading %s", e.code, url)
        return None
    except Exception as e:
        logger.warning("Failed to download %s: %s", url, e)
        return None


# ── YAML helpers ────────────────────────────────────────────────────────────

def _validate_skill_yaml(data: Dict[str, Any]) -> Optional[str]:
    """Minimal validation. Returns error message or None if valid."""
    if not isinstance(data, dict):
        return "not a dict"
    if not data.get("name"):
        return "missing 'name' field"
    steps = data.get("steps")
    if not isinstance(steps, list) or not steps:
        return "missing or empty 'steps' list"
    return None


def _ensure_dir(path: str) -> str:
    """Create directory if missing, return path."""
    os.makedirs(path, exist_ok=True)
    return path


# ═════════════════════════════════════════════════════════════════════════════
# SkillManager
# ═════════════════════════════════════════════════════════════════════════════

class SkillManager:
    """Manage skills across builtin / installed / custom directories.

    Usage::

        mgr = SkillManager()
        mgr.list_all()                    # → List[SkillSource]
        mgr.install("github://...")       # download → installed/
        mgr.create("my_skill")            # generate template → custom/
        mgr.remove("my_skill")            # delete from installed or custom
        mgr.update("k8s_pod_diagnostics") # re-download from GitHub
        mgr.get_skill_data("my_skill")    # → parsed YAML dict
    """

    def __init__(
        self,
        builtin_dir: str = BUILTIN_DIR,
        installed_dir: str = INSTALLED_DIR,
        custom_dir: str = CUSTOM_DIR,
    ):
        self._builtin_dir = builtin_dir
        self._installed_dir = _ensure_dir(installed_dir)
        self._custom_dir = _ensure_dir(custom_dir)

    # ── Scan ──────────────────────────────────────────────────────────────────

    def list_all(self) -> List[SkillSource]:
        """Return all skills from all three directories.

        When names collide across directories, only the highest-priority
        occurrence is returned (custom > installed > builtin).
        """
        seen: Dict[str, SkillSource] = {}

        for source_dir, dir_path in [
            ("builtin", self._builtin_dir),
            ("installed", self._installed_dir),
            ("custom", self._custom_dir),
        ]:
            if not os.path.isdir(dir_path):
                continue
            for fname in sorted(os.listdir(dir_path)):
                if not fname.endswith(".yaml"):
                    continue
                file_path = os.path.join(dir_path, fname)
                source = self._read_skill_source(file_path, source_dir)
                if source and source.name:
                    # Lower-priority dirs are iterated first, so a later
                    # (higher-priority) occurrence overwrites an earlier one.
                    seen[source.name] = source

        return list(seen.values())

    def list_by_source(self) -> Dict[str, List[SkillSource]]:
        """Return skills grouped by source_dir: 'builtin' | 'installed' | 'custom'."""
        groups: Dict[str, List[SkillSource]] = {
            "builtin": [],
            "installed": [],
            "custom": [],
        }
        for source_dir, dir_path in [
            ("builtin", self._builtin_dir),
            ("installed", self._installed_dir),
            ("custom", self._custom_dir),
        ]:
            if not os.path.isdir(dir_path):
                continue
            for fname in sorted(os.listdir(dir_path)):
                if not fname.endswith(".yaml"):
                    continue
                file_path = os.path.join(dir_path, fname)
                source = self._read_skill_source(file_path, source_dir)
                if source and source.name:
                    groups.setdefault(source_dir, []).append(source)
        return groups

    def get_skill(self, name: str, source: Optional[str] = None) -> Optional[SkillSource]:
        """Find a skill by name, optionally filtered to a specific source dir.

        Priority: custom > installed > builtin (when not filtered).
        """
        found: Optional[SkillSource] = None
        order = ["custom", "installed", "builtin"]

        for source_dir, dir_path in [
            ("builtin", self._builtin_dir),
            ("installed", self._installed_dir),
            ("custom", self._custom_dir),
        ]:
            if source and source_dir != source:
                continue
            if not os.path.isdir(dir_path):
                continue
            for fname in os.listdir(dir_path):
                if not fname.endswith(".yaml"):
                    continue
                file_path = os.path.join(dir_path, fname)
                s = self._read_skill_source(file_path, source_dir)
                if s and s.name == name:
                    found = s
                    if source:
                        return found  # exact match, no need to continue

        return found

    def get_skill_data(self, name: str) -> Optional[Dict[str, Any]]:
        """Return the parsed YAML data for a skill by name."""
        source = self.get_skill(name)
        if source is None or not os.path.isfile(source.file_path):
            return None
        import yaml
        with open(source.file_path, "r", encoding="utf-8") as f:
            return yaml.safe_load(f)

    # ── Install ───────────────────────────────────────────────────────────────

    def install(self, github_url: str) -> SkillSource:
        """Download a skill YAML from GitHub and save to installed/ directory.

        Args:
            github_url: Format ``github://owner/repo/path/to/skill.yaml[@ref]``
                        or ``github://owner/repo`` (looks for index.yaml).

        Returns:
            SkillSource for the installed skill.

        Raises:
            ValueError: on invalid URL, download failure, or invalid YAML.
        """
        parts = parse_github_url(github_url)
        if not parts:
            raise ValueError(
                f"Invalid GitHub URL: {github_url!r}. "
                f"Expected format: github://owner/repo/path/to/skill.yaml[@ref] "
                f"or https://github.com/owner/repo[/blob/ref]/path/to/skill.yaml"
            )

        # ── If no specific file path, try index.yaml ──────────────────────
        if not parts["path"]:
            index_url = build_raw_url({**parts, "path": "index.yaml"})
            content = _download_file(index_url)
            if content is None:
                raise ValueError(
                    f"No path given and index.yaml not found at {index_url}. "
                    f"Specify a file: github://{parts['owner']}/{parts['repo']}/skills/my_skill.yaml"
                )
            # Parse index and install all listed skills
            import yaml
            index_data = yaml.safe_load(content)
            if not isinstance(index_data, dict):
                raise ValueError("index.yaml must be a YAML mapping")
            skill_paths = index_data.get("skills", [])
            if not skill_paths:
                raise ValueError("index.yaml has no 'skills' list")
            results = []
            for sp in skill_paths:
                skill_url = f"github://{parts['owner']}/{parts['repo']}@{parts['ref']}/{sp}"
                results.append(self.install(skill_url))
            return results[0]  # return first; caller can see all via list_all()

        # ── Download the YAML file ───────────────────────────────────────
        raw_url = build_raw_url(parts)
        content = _download_file(raw_url)
        if content is None:
            raise ValueError(f"Failed to download from {raw_url}")

        import yaml
        data = yaml.safe_load(content)
        err = _validate_skill_yaml(data)
        if err:
            raise ValueError(f"Invalid skill YAML from {raw_url}: {err}")

        skill_name = data["name"]

        # Check collision with builtin — warn but allow
        builtin_path = os.path.join(self._builtin_dir, f"{skill_name}.yaml")
        if os.path.isfile(builtin_path):
            logger.warning(
                "Skill '%s' already exists in builtin — installed version will override it",
                skill_name,
            )

        # Add install tracking metadata
        data["_source"] = {
            "type": "github",
            "original_url": github_url,
            "raw_url": raw_url,
            "installed_at": _now_iso(),
        }

        # Write to installed/ directory
        dest = os.path.join(self._installed_dir, f"{skill_name}.yaml")
        with open(dest, "w", encoding="utf-8") as f:
            yaml.dump(data, f, allow_unicode=True, sort_keys=False, default_flow_style=False)

        logger.info("Installed skill '%s' from %s → %s", skill_name, github_url, dest)
        src = self._read_skill_source(dest, "installed")
        assert src is not None
        return src

    # ── Create ────────────────────────────────────────────────────────────────

    def create(self, name: str) -> SkillSource:
        """Generate a new skill YAML template in custom/.

        Args:
            name: Skill name (alphanumeric + underscores). Used as filename.

        Returns:
            SkillSource for the newly created skill.
        """
        if not re.match(r"^[a-zA-Z][a-zA-Z0-9_]*$", name):
            raise ValueError(
                f"Invalid skill name: {name!r}. Must start with a letter and "
                f"contain only letters, digits, and underscores."
            )

        dest = os.path.join(self._custom_dir, f"{name}.yaml")
        if os.path.isfile(dest):
            raise FileExistsError(f"Skill '{name}' already exists at {dest}")

        template = _build_skill_template(name)
        with open(dest, "w", encoding="utf-8") as f:
            f.write(template)

        logger.info("Created skill '%s' → %s", name, dest)
        src = self._read_skill_source(dest, "custom")
        assert src is not None
        return src

    def import_yaml(self, name: str, yaml_content: str) -> SkillSource:
        """Import a skill YAML string into custom/ (used by the frontend editor).

        Args:
            name: Skill name (should match the 'name' field in the YAML).
            yaml_content: Full YAML content.

        Returns:
            SkillSource for the imported skill.
        """
        import yaml
        data = yaml.safe_load(yaml_content)
        err = _validate_skill_yaml(data)
        if err:
            raise ValueError(f"Invalid skill YAML: {err}")

        # Use the name from the YAML if different
        skill_name = data["name"]
        dest = os.path.join(self._custom_dir, f"{skill_name}.yaml")

        with open(dest, "w", encoding="utf-8") as f:
            f.write(yaml_content)

        logger.info("Imported skill '%s' → %s", skill_name, dest)
        src = self._read_skill_source(dest, "custom")
        assert src is not None
        return src

    # ── Remove ────────────────────────────────────────────────────────────────

    def remove(self, name: str) -> bool:
        """Delete a skill from installed/ or custom/.

        Returns True if deleted, False if not found (or builtin).
        """
        for dir_path in [self._custom_dir, self._installed_dir]:
            if not os.path.isdir(dir_path):
                continue
            path = os.path.join(dir_path, f"{name}.yaml")
            if os.path.isfile(path):
                os.remove(path)
                logger.info("Removed skill '%s' from %s", name, dir_path)
                return True

        logger.warning("Skill '%s' not found in installed/ or custom/ — cannot remove", name)
        return False

    # ── Update ────────────────────────────────────────────────────────────────

    def update(self, name: str) -> bool:
        """Re-download an installed skill from its original GitHub URL.

        Returns True if updated, False if skill is not an installed/GitHub skill.
        """
        source = self.get_skill(name, source="installed")
        if source is None:
            logger.warning("Skill '%s' is not from installed/ — nothing to update", name)
            return False

        original_url = source.install_meta.get("original_url")
        if not original_url:
            logger.warning("Skill '%s' has no original URL — cannot update", name)
            return False

        logger.info("Updating skill '%s' from %s", name, original_url)
        self.install(original_url)
        return True

    def update_all(self) -> List[str]:
        """Re-download all installed skills from their original URLs.

        Returns:
            List of updated skill names.
        """
        updated: List[str] = []
        for skill in self.list_all():
            if skill.source_dir == "installed" and skill.install_meta.get("original_url"):
                try:
                    self.update(skill.name)
                    updated.append(skill.name)
                except Exception as e:
                    logger.error("Failed to update skill '%s': %s", skill.name, e)
        return updated

    # ── Auto-selection ────────────────────────────────────────────────────────

    def match_skills(
        self,
        context: SkillContext,
        *,
        min_score: float = 0.1,
        max_skills: int = 4,
    ) -> List[Tuple[SkillSource, float]]:
        """Score all skills against context for auto-selection.

        Uses the same algorithm as ``match_skill_yaml()``: trigger pattern
        matching + component type bonus.

        Returns:
            List of (SkillSource, score) sorted descending, capped at max_skills.
        """
        scored: List[Tuple[SkillSource, float]] = []
        for skill in self.list_all():
            score = self._calculate_match_score(skill, context)
            if score >= min_score:
                scored.append((skill, score))

        scored.sort(key=lambda pair: pair[1], reverse=True)
        return scored[:max_skills]

    def _calculate_match_score(self, skill: SkillSource, context: SkillContext) -> float:
        """Single skill vs context match score — same algo as match_skill_yaml()."""
        text = context.combined_text().lower()
        if not text:
            return 0.0

        patterns = skill.trigger_patterns
        if not patterns:
            return 0.0

        hits = sum(1 for p in patterns if re.search(p, text, re.IGNORECASE))
        pattern_score = min(1.0, hits / max(len(patterns), 1))

        component_bonus = 0.0
        ct = _as_str(context.component_type).lower()
        if ct and skill.applicable_components:
            for comp in skill.applicable_components:
                if comp.lower() in ct or ct in comp.lower():
                    component_bonus = 0.2
                    break

        return min(1.0, pattern_score + component_bonus)

    # ── Internal helpers ──────────────────────────────────────────────────────

    @staticmethod
    def _read_skill_source(file_path: str, source_dir: str) -> Optional[SkillSource]:
        """Read a YAML file and extract its metadata as SkillSource."""
        import yaml

        try:
            with open(file_path, "r", encoding="utf-8") as f:
                data = yaml.safe_load(f)
        except Exception as e:
            logger.debug("Failed to read skill YAML %s: %s", file_path, e)
            return None

        if not isinstance(data, dict) or not data.get("name"):
            return None

        steps = data.get("steps", []) or []
        install_meta = data.pop("_source", {}) or {}
        trigger_raw = data.get("trigger_patterns", []) or []

        return SkillSource(
            name=str(data["name"]),
            display_name=_as_str(data.get("display_name"), str(data["name"])),
            description=_as_str(data.get("description")),
            source_dir=source_dir,
            file_path=file_path,
            risk_level=_as_str(data.get("risk_level"), "low"),
            step_count=len(steps) if isinstance(steps, list) else 0,
            trigger_patterns=[str(p) for p in trigger_raw if isinstance(p, str)],
            applicable_components=list(data.get("applicable_components") or []),
            install_meta=dict(install_meta) if isinstance(install_meta, dict) else {},
        )


# ── Template builder ─────────────────────────────────────────────────────────

def _now_iso() -> str:
    """Return current UTC time in ISO-8601 format."""
    from datetime import datetime, timezone
    return datetime.now(timezone.utc).isoformat()


def _build_skill_template(name: str) -> str:
    """Generate a YAML template for a new custom skill."""
    import textwrap
    return textwrap.dedent(f"""\
    # Custom skill: {name}
    # Created by skill create at {_now_iso()}
    # Edit the steps below to define your diagnostic workflow.

    name: {name}
    display_name: "{name.replace('_', ' ').title()}"
    description: >
      你的诊断技能描述 — 说明此技能适用什么场景。
    applicable_components:
      - your-component-type
    trigger_patterns:
      - "your-trigger-keyword"
    risk_level: low
    max_steps: 3
    steps:
      - id: step-1
        title: "第一步：诊断操作标题"
        tool: generic_exec
        command: >
          kubectl get pods -n {{namespace}} 2>/dev/null
        purpose: "这一步要做什么"
        timeout: 20
        parse_hints:
          extract: ["POD_NAME", "STATUS", "RESTARTS"]

      - id: step-2
        title: "第二步：后续操作"
        tool: generic_exec
        command: >
          kubectl logs deployment/{{service_name}} -n {{namespace}} --tail=50 2>/dev/null
        purpose: "这一步要做什么"
        depends_on: ["step-1"]
        timeout: 20

      - id: step-3
        title: "第三步：结果汇总"
        tool: generic_exec
        command: >
          echo "分析完成"
        purpose: "汇总诊断结果"
        depends_on: ["step-2"]
        timeout: 10
    """)


# ═════════════════════════════════════════════════════════════════════════════
# CLI
# ═════════════════════════════════════════════════════════════════════════════

def main_cli():
    """Entry point for ``python -m ai.skills.manager <command>``."""
    import argparse

    parser = argparse.ArgumentParser(
        prog="skill",
        description="Logoscope 技能管理器 — 管理内置/安装/自定义诊断技能",
    )
    sub = parser.add_subparsers(dest="command", required=True)

    # install
    p_install = sub.add_parser("install", help="从 GitHub 安装技能")
    p_install.add_argument("url", help="github://owner/repo/path/to/skill.yaml")

    # create
    p_create = sub.add_parser("create", help="创建自定义技能")
    p_create.add_argument("name", help="技能名称（字母开头，仅字母数字下划线）")

    # list
    p_list = sub.add_parser("list", help="列出所有可用技能")
    p_list.add_argument("--source", choices=["builtin", "installed", "custom"],
                        help="按来源过滤")

    # info
    p_info = sub.add_parser("info", help="查看技能详情")
    p_info.add_argument("name", help="技能名称")
    p_info.add_argument("--source", choices=["builtin", "installed", "custom"],
                        help="指定来源")

    # remove
    p_remove = sub.add_parser("remove", help="删除已安装/自定义技能")
    p_remove.add_argument("name", help="技能名称")

    # update
    p_update = sub.add_parser("update", help="更新已安装技能")
    p_update.add_argument("name", nargs="?", help="技能名称（不指定则更新全部）")

    args = parser.parse_args()
    mgr = SkillManager()

    if args.command == "install":
        try:
            src = mgr.install(args.url)
            print(f"✅ 安装成功: {src.name} ({src.source_label})")
            print(f"   路径: {src.file_path}")
        except Exception as e:
            print(f"❌ 安装失败: {e}")
            return 1

    elif args.command == "create":
        try:
            src = mgr.create(args.name)
            print(f"✅ 创建成功: {src.name}")
            print(f"   路径: {src.file_path}")
            print(f"   请编辑该文件添加你的诊断步骤。")
        except Exception as e:
            print(f"❌ 创建失败: {e}")
            return 1

    elif args.command == "list":
        skills = mgr.list_all()
        if args.source:
            skills = [s for s in skills if s.source_dir == args.source]

        if not skills:
            print("没有找到技能。")
            return 0

        # Group for display — respect --source filter
        groups = mgr.list_by_source()
        source_filter = args.source
        for src_dir, label in [("builtin", "内置"), ("installed", "已安装"), ("custom", "自定义")]:
            if source_filter and src_dir != source_filter:
                continue
            items = [s for s in groups.get(src_dir, [])]
            if not items:
                continue
            print(f"\n[{label} ({len(items)}个)]")
            print(f"{'名称':<30} {'步骤':<6} {'风险':<6} {'触发词'}")
            print("-" * 70)
            for s in items:
                triggers = ", ".join(s.trigger_patterns[:3])
                if len(s.trigger_patterns) > 3:
                    triggers += "..."
                print(f"{s.name:<30} {s.step_count:<6} {s.risk_level:<6} {triggers}")

    elif args.command == "info":
        skill = mgr.get_skill(args.name, source=args.source)
        if not skill:
            print(f"未找到技能: {args.name}")
            return 1
        data = mgr.get_skill_data(args.name)
        print(f"名称: {skill.name}")
        print(f"显示名: {skill.display_name}")
        print(f"来源: {skill.source_label}")
        print(f"风险等级: {skill.risk_level}")
        print(f"步骤数: {skill.step_count}")
        print(f"适用组件: {', '.join(skill.applicable_components)}")
        print(f"触发词: {', '.join(skill.trigger_patterns)}")
        if skill.install_meta:
            print(f"安装源: {skill.install_meta.get('original_url', 'N/A')}")
        print(f"\n文件: {skill.file_path}")
        if data and data.get("steps"):
            print(f"\n步骤详情:")
            for i, step in enumerate(data["steps"]):
                print(f"  {i+1}. [{step.get('id','?')}] {step.get('title','')}")
                print(f"     命令: {step.get('command','')[:80]}...")
                print(f"     目的: {step.get('purpose','')}")
                if i < len(data["steps"]) - 1:
                    print()

    elif args.command == "remove":
        if mgr.remove(args.name):
            print(f"✅ 已删除: {args.name}")
        else:
            print(f"❌ 未找到: {args.name}（内置技能无法删除）")
            return 1

    elif args.command == "update":
        if args.name:
            if mgr.update(args.name):
                print(f"✅ 已更新: {args.name}")
            else:
                print(f"⚠️  无需更新或不是已安装技能: {args.name}")
        else:
            updated = mgr.update_all()
            if updated:
                print(f"✅ 已更新 {len(updated)} 个技能: {', '.join(updated)}")
            else:
                print("没有需要更新的已安装技能。")

    return 0


if __name__ == "__main__":
    import sys
    sys.exit(main_cli())


__all__ = [
    "SkillManager",
    "SkillSource",
    "InstallMeta",
    "parse_github_url",
    "build_raw_url",
    "main_cli",
]
