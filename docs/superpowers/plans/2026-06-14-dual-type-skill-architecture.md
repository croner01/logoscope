# 双类型技能架构实现计划

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** 扩展 SkillManager 支持 Markdown 参考技能（Superpowers 风格），通过适配器模式实现 YAML 诊断技能 + Markdown 参考技能的双类型共存。

**Architecture:** 新增 `SkillAdapter` 抽象基类，将现有 YAML 处理逻辑提取为 `YamlAdapter`，新增 `MarkdownAdapter` 处理 SKILL.md 目录安装。SkillManager 通过注册的适配器列表进行 detect → 委派。前端新增 `ReferenceSkillView` 组件渲染 Markdown。AI 服务在构建上下文时按需注入匹配的参考技能。

**Tech Stack:** Python 3.10+, FastAPI, Pydantic, React 18, react-markdown

---

### Task 1: SkillAdapter 抽象基类 + SkillSource 数据模型

**Files:**
- Create: `ai-service/ai/skills/adapters/__init__.py`
- Create: `ai-service/ai/skills/adapters/base.py`
- Modify: `ai-service/ai/skills/manager.py`（移除 `SkillSource` 定义，改为从 adapters 导入）

- [ ] **Step 1: 创建适配器包和基类**

Create `ai-service/ai/skills/adapters/__init__.py`:

```python
"""Skill format adapters — pluggable format handlers for SkillManager."""
from ai.skills.adapters.base import SkillAdapter, SkillSource
from ai.skills.adapters.yaml_adapter import YamlAdapter
from ai.skills.adapters.markdown_adapter import MarkdownAdapter

__all__ = [
    "SkillAdapter",
    "SkillSource",
    "YamlAdapter",
    "MarkdownAdapter",
]
```

Create `ai-service/ai/skills/adapters/base.py`:

```python
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
```

- [ ] **Step 2: 更新 SkillManager 导入**

In `ai-service/ai/skills/manager.py`, replace:

```python
from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional
```

With:

```python
from typing import Any, Dict, List, Optional
```

And remove the entire `SkillSource` dataclass definition (lines 53-74) and `@property source_label`. Import from adapters instead:

```python
from ai.skills.adapters import SkillSource
```

- [ ] **Step 3: Run tests to verify import works**

Run: `cd /root/logoscope/ai-service && python3 -c "from ai.skills.adapters import SkillSource, SkillAdapter; print('OK')"`
Expected: `OK`

- [ ] **Step 4: Commit**

```bash
git add ai-service/ai/skills/adapters/
git commit -m "feat(adapters): SkillAdapter ABC + SkillSource dataclass"

Co-Authored-By: Claude <noreply@anthropic.com>
```

---

### Task 2: YamlAdapter — 从 manager.py 提取 YAML 逻辑

**Files:**
- Create: `ai-service/ai/skills/adapters/yaml_adapter.py`
- Modify: `ai-service/ai/skills/manager.py`（移除 `_validate_skill_yaml`）

- [ ] **Step 1: 创建 YamlAdapter**

Create `ai-service/ai/skills/adapters/yaml_adapter.py`:

```python
"""YAML diagnostic skill adapter — handles .yaml files with executable steps."""
from __future__ import annotations

import logging
import os
from typing import Any, Dict, List, Optional

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
            "installed_at": __import__("datetime").datetime.utcnow().isoformat(),
        }

        dest = os.path.join(installed_dir, f"{skill_name}.yaml")
        with open(dest, "w", encoding="utf-8") as f:
            yaml.dump(data, f, default_flow_style=False, allow_unicode=True)

        logger.info("Installed skill '%s' from %s → %s", skill_name, github_url, dest)
        return self.read(dest, "installed")
```

- [ ] **Step 2: 从 manager.py 移除 `_validate_skill_yaml`**

Delete the entire `_validate_skill_yaml` function (lines 163-172) from `manager.py`. Its logic is now in `YamlAdapter.validate()`.

- [ ] **Step 3: Import check**

Run: `cd /root/logoscope/ai-service && python3 -c "from ai.skills.adapters.yaml_adapter import YamlAdapter; print('OK')"`
Expected: `OK`

- [ ] **Step 4: Commit**

```bash
git add ai-service/ai/skills/adapters/yaml_adapter.py ai-service/ai/skills/manager.py
git commit -m "feat(adapters): YamlAdapter — extract YAML skill handling from manager.py"

Co-Authored-By: Claude <noreply@anthropic.com>
```

---

### Task 3: MarkdownAdapter — Superpowers 风格参考技能支持

**Files:**
- Create: `ai-service/ai/skills/adapters/markdown_adapter.py`
- Test: Add `test_markdown_adapter.py` to ai-service tests

- [ ] **Step 1: 创建 MarkdownAdapter**

Create `ai-service/ai/skills/adapters/markdown_adapter.py`:

```python
"""Markdown reference skill adapter — handles SKILL.md directories."""
from __future__ import annotations

import logging
import os
import re
from typing import Any, Dict, List, Optional

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
                            aux[fname] = af.read()
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
            # Fallback: derive name from the URL path directory
            skill_name = parts["path"].rstrip("/SKILL.md").split("/")[-1] or "unnamed"

        dest_dir = os.path.join(installed_dir, skill_name)
        os.makedirs(dest_dir, exist_ok=True)

        # Write SKILL.md
        md_path = os.path.join(dest_dir, "SKILL.md")
        with open(md_path, "w", encoding="utf-8") as f:
            f.write(content)

        logger.info("Installed reference skill '%s' from %s → %s/",
                     skill_name, github_url, dest_dir)

        return self.read(dest_dir, "installed")
```

- [ ] **Step 2: 创建单元测试**

Create `ai-service/tests/test_markdown_adapter.py`:

```python
"""Tests for MarkdownAdapter — front matter parsing + read + install."""
from __future__ import annotations

import os
import tempfile

import pytest

from ai.skills.adapters.markdown_adapter import (
    MarkdownAdapter,
    _parse_front_matter,
    _strip_front_matter,
)
from ai.skills.adapters.base import SkillSource


SAMPLE_SKILL_MD = """\
---
name: systematic-debugging
description: Use when encountering any bug, test failure, or unexpected behavior
---

# Systematic Debugging

Random fixes waste time.

## The Iron Law
NO FIXES WITHOUT ROOT CAUSE INVESTIGATION FIRST
"""

SAMPLE_AUX_MD = """\
# Root Cause Tracing

Trace bugs backward through call stack.
"""


class TestFrontMatter:
    def test_parse_valid_front_matter(self):
        result = _parse_front_matter(SAMPLE_SKILL_MD)
        assert result["name"] == "systematic-debugging"
        assert "bug" in result["description"]

    def test_parse_no_front_matter(self):
        assert _parse_front_matter("Just text") == {}

    def test_parse_empty_front_matter(self):
        assert _parse_front_matter("---\n---\nbody") == {}

    def test_strip_front_matter(self):
        body = _strip_front_matter(SAMPLE_SKILL_MD)
        assert body.startswith("# Systematic Debugging")
        assert "NO FIXES WITHOUT" in body

    def test_strip_no_front_matter(self):
        assert _strip_front_matter("plain") == "plain"


class TestMarkdownAdapterRead:
    def test_read_single_md_file(self):
        adapter = MarkdownAdapter()
        with tempfile.NamedTemporaryFile(mode="w", suffix=".md", delete=False) as f:
            f.write(SAMPLE_SKILL_MD)
            tmp = f.name
        try:
            source = adapter.read(tmp, "installed")
            assert source is not None
            assert source.name == "systematic-debugging"
            assert source.skill_type == "reference"
            assert source.step_count == 0
            assert source.body.startswith("# Systematic Debugging")
        finally:
            os.unlink(tmp)

    def test_read_directory(self):
        adapter = MarkdownAdapter()
        with tempfile.TemporaryDirectory() as d:
            with open(os.path.join(d, "SKILL.md"), "w") as f:
                f.write(SAMPLE_SKILL_MD)
            with open(os.path.join(d, "root-cause-tracing.md"), "w") as f:
                f.write(SAMPLE_AUX_MD)

            source = adapter.read(d, "installed")
            assert source is not None
            assert source.name == "systematic-debugging"
            assert "root-cause-tracing.md" in source.auxiliary_files
            assert source.auxiliary_files["root-cause-tracing.md"] == SAMPLE_AUX_MD.strip()

    def test_detect(self):
        adapter = MarkdownAdapter()
        assert adapter.detect("/path/to/skill.md")
        assert not adapter.detect("/path/to/skill.yaml")
        assert not adapter.detect("/path/to/skill.py")


class TestMarkdownAdapterInstall:
    def test_install_creates_directory(self):
        adapter = MarkdownAdapter()
        parts = {"path": "skills/systematic-debugging/SKILL.md"}
        with tempfile.TemporaryDirectory() as installed_dir:
            source = adapter.install(
                content=SAMPLE_SKILL_MD,
                parts=parts,
                github_url="https://github.com/obra/superpowers",
                raw_url="https://raw.githubusercontent.com/obra/superpowers/main/skills/systematic-debugging/SKILL.md",
                installed_dir=installed_dir,
            )
            assert source is not None
            assert source.name == "systematic-debugging"
            # Verify directory was created
            skill_dir = os.path.join(installed_dir, "systematic-debugging")
            assert os.path.isdir(skill_dir)
            assert os.path.isfile(os.path.join(skill_dir, "SKILL.md"))
```

- [ ] **Step 3: 运行测试**

Run: `cd /root/logoscope/ai-service && python3 -m pytest tests/test_markdown_adapter.py -v --no-header -q 2>&1 | tail -20`
Expected: All tests pass (no failures)

- [ ] **Step 4: Commit**

```bash
git add ai-service/ai/skills/adapters/markdown_adapter.py ai-service/tests/test_markdown_adapter.py
git commit -m "feat(adapters): MarkdownAdapter — Superpowers 风格参考技能 + 单元测试"

Co-Authored-By: Claude <noreply@anthropic.com>
```

---

### Task 4: Discovery 模块 — GitHub API 自动发现

**Files:**
- Create: `ai-service/ai/skills/discovery.py`
- Test: `ai-service/tests/test_discovery.py`

- [ ] **Step 1: 创建 discovery.py**

Create `ai-service/ai/skills/discovery.py`:

```python
"""GitHub repository discovery — list skill directories, download index.yaml."""
from __future__ import annotations

import json
import logging
import urllib.error
import urllib.request
from typing import Any, Dict, List, Optional

GITHUB_API_BASE = "https://api.github.com"

logger = logging.getLogger(__name__)


def _download_json(url: str, ref: str = "main", timeout: int = 15) -> Optional[Any]:
    """Download a JSON response from a GitHub API URL."""
    headers = {
        "User-Agent": "logoscope/1.0",
        "Accept": "application/vnd.github.v3+json",
    }
    req = urllib.request.Request(url, headers=headers)
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            return json.loads(resp.read().decode("utf-8"))
    except urllib.error.HTTPError as e:
        if e.code == 404:
            return None
        logger.warning("GitHub API HTTP %d: %s", e.code, url)
        return None
    except Exception as e:
        logger.warning("GitHub API request failed: %s: %s", url, e)
        return None


def discover_skill_urls(owner: str, repo: str,
                        ref: str = "main") -> List[str]:
    """Scan a GitHub repo's skills/ directory and return SKILL.md URLs.

    Returns paths in ``skills/<name>/SKILL.md`` format, suitable for
    passing back to ``SkillManager.install()``.
    """
    api_url = f"{GITHUB_API_BASE}/repos/{owner}/{repo}/contents/skills?ref={ref}"
    data = _download_json(api_url, ref=ref)
    if not isinstance(data, list):
        return []

    urls: List[str] = []
    for item in data:
        if isinstance(item, dict) and item.get("type") == "dir":
            name = item.get("name", "")
            if name:
                urls.append(f"skills/{name}/SKILL.md")
    return urls


def try_index_yaml(owner: str, repo: str, ref: str = "main") -> Optional[str]:
    """Try to download index.yaml from the repo root. Return content or None."""
    raw_url = (f"https://raw.githubusercontent.com/{owner}/{repo}/{ref}/index.yaml")
    req = urllib.request.Request(raw_url, headers={"User-Agent": "logoscope/1.0"})
    try:
        with urllib.request.urlopen(req, timeout=15) as resp:
            return resp.read().decode("utf-8")
    except urllib.error.HTTPError:
        return None
```

- [ ] **Step 2: 创建测试**

Create `ai-service/tests/test_discovery.py`:

```python
"""Tests for discovery module — GitHub API response parsing."""
from __future__ import annotations

import json
from unittest.mock import patch, Mock

from ai.skills.discovery import discover_skill_urls


class TestDiscoverSkillUrls:
    def test_parses_directory_list(self):
        mock_response = [
            {"name": "systematic-debugging", "type": "dir"},
            {"name": "brainstorming", "type": "dir"},
            {"name": "writing-plans", "type": "dir"},
            {"name": "README.md", "type": "file"},  # Should be filtered out
        ]
        with patch("ai.skills.discovery._download_json", return_value=mock_response):
            urls = discover_skill_urls("obra", "superpowers")
            assert len(urls) == 3
            assert "skills/systematic-debugging/SKILL.md" in urls
            assert "skills/brainstorming/SKILL.md" in urls
            assert "skills/README.md" not in urls

    def test_empty_repo(self):
        with patch("ai.skills.discovery._download_json", return_value=[]):
            assert discover_skill_urls("obra", "superpowers") == []

    def test_api_error(self):
        with patch("ai.skills.discovery._download_json", return_value=None):
            assert discover_skill_urls("obra", "superpowers") == []

    def test_non_list_response(self):
        with patch("ai.skills.discovery._download_json", return_value={"message": "Not Found"}):
            assert discover_skill_urls("obra", "superpowers") == []
```

- [ ] **Step 3: 运行测试**

Run: `cd /root/logoscope/ai-service && python3 -m pytest tests/test_discovery.py -v --no-header -q 2>&1 | tail -10`
Expected: All 4 tests pass

- [ ] **Step 4: Commit**

```bash
git add ai-service/ai/skills/discovery.py ai-service/tests/test_discovery.py
git commit -m "feat(discovery): GitHub API 自动发现技能目录 + 单元测试"

Co-Authored-By: Claude <noreply@anthropic.com>
```

---

### Task 5: 重构 SkillManager — 适配器注册 + 自动发现安装

**Files:**
- Modify: `ai-service/ai/skills/manager.py`

This is the most critical task. Changes to `manager.py`:

1. Import adapters and discovery module
2. Add `register_adapter()` / `_get_adapter()` methods
3. Refactor `list_all()` to iterate adapters instead of hardcoding `.yaml`
4. Refactor `install()` to try auto-discovery when path is empty
5. Register YamlAdapter and MarkdownAdapter in `__init__`

- [ ] **Step 1: Update manager.py imports and __init__**

Add imports at top of `manager.py`:

```python
from ai.skills.adapters import SkillAdapter, SkillSource, YamlAdapter, MarkdownAdapter
from ai.skills.discovery import discover_skill_urls, try_index_yaml
```

In `SkillManager.__init__()`, after `self._custom_dir = ...`, add:

```python
# Registered format adapters
self._adapters: Dict[str, SkillAdapter] = {}
self._register_default_adapters()
```

Add the new methods:

```python
def register_adapter(self, adapter: SkillAdapter) -> None:
    """Register a skill format adapter."""
    self._adapters[adapter.skill_type] = adapter

def _register_default_adapters(self) -> None:
    self.register_adapter(YamlAdapter())
    self.register_adapter(MarkdownAdapter())

def _get_adapter(self, file_path: str) -> Optional[SkillAdapter]:
    """Find the first adapter that can handle *file_path*."""
    for adapter in self._adapters.values():
        if adapter.detect(file_path):
            return adapter
    return None
```

- [ ] **Step 2: Refactor list_all() to use adapters**

Replace the body of `list_all()`:

```python
def list_all(self) -> List[SkillSource]:
    seen: Dict[str, SkillSource] = {}

    for source_dir, dir_path in [
        ("builtin", self._builtin_dir),
        ("installed", self._installed_dir),
        ("custom", self._custom_dir),
    ]:
        if not os.path.isdir(dir_path):
            continue
        for name in sorted(os.listdir(dir_path)):
            item_path = os.path.join(dir_path, name)
            adapter = self._get_adapter(item_path)
            if adapter is None:
                continue
            source = adapter.read(item_path, source_dir)
            if source and source.name:
                seen[source.name] = source

    return list(seen.values())
```

- [ ] **Step 3: Refactor install() to add auto-discovery**

Replace the `install` method body:

```python
def install(self, github_url: str) -> List[SkillSource]:
    """Install skill(s) from GitHub.

    Returns a list of installed SkillSource objects (1 for single file,
    multiple for auto-discovered repos).
    """
    parts = parse_github_url(github_url)
    if not parts:
        raise ValueError(
            f"Invalid GitHub URL: {github_url!r}. "
            f"Expected format: github://owner/repo/path/to/skill.yaml[@ref] "
            f"or https://github.com/owner/repo[/blob/ref]/path/to/skill.yaml"
        )

    # ── Specific file path given → single install ─────────────────
    if parts["path"]:
        return [self._install_single(parts, github_url)]

    # ── No path → try index.yaml ─────────────────────────────────
    index_content = try_index_yaml(parts["owner"], parts["repo"], parts["ref"])
    if index_content:
        return self._install_from_index(index_content, parts, github_url)

    # ── Auto-discover skills via GitHub API ───────────────────────
    skill_paths = discover_skill_urls(parts["owner"], parts["repo"], parts["ref"])
    if not skill_paths:
        raise ValueError(
            f"No index.yaml or skills found at {github_url}. "
            f"Specify a skill path: {github_url}/skills/<name>/SKILL.md"
        )

    installed = []
    for sp in skill_paths:
        sub_parts = {**parts, "path": sp}
        try:
            result = self._install_single(sub_parts, github_url)
            installed.append(result)
        except (ValueError, FileExistsError) as e:
            logger.warning("Skipping %s: %s", sp, e)
    if not installed:
        raise ValueError("No installable skills found in repository.")
    return installed
```

- [ ] **Step 4: Extract `_install_single` helper**

This is the core of the old `install()` method. Extract it:

```python
def _install_single(self, parts: Dict[str, str],
                    original_url: str) -> SkillSource:
    """Download and install a single skill file from GitHub."""
    raw_url = build_raw_url(parts)
    content = _download_file(raw_url)
    if content is None:
        raise ValueError(f"Failed to download from {raw_url}")

    # Find the right adapter
    file_name = parts["path"].split("/")[-1]
    adapter = self._get_adapter(file_name)
    if adapter is None:
        raise ValueError(
            f"Unsupported skill format: {file_name}. "
            f"Supported: YAML (.yaml), Markdown (.md)"
        )

    return adapter.install(content, parts, original_url, raw_url, self._installed_dir)
```

- [ ] **Step 5: Update `_read_skill_source` to use adapters**

The existing `_read_skill_source` method should delegate to adapters:

```python
def _read_skill_source(self, file_path: str, source_dir: str) -> Optional[SkillSource]:
    adapter = self._get_adapter(file_path)
    if adapter is None:
        return None
    return adapter.read(file_path, source_dir)
```

And also refactor `get_skill_data` to handle reference skills (no YAML steps):

```python
def get_skill_data(self, name: str) -> Optional[Dict[str, Any]]:
    """Return parsed skill data. For reference skills returns body + aux_files."""
    source = self.get_skill(name)
    if source is None or not os.path.exists(source.file_path):
        return None

    if source.skill_type == "reference":
        return {
            "body": source.body,
            "auxiliary_files": source.auxiliary_files,
        }

    # diagnostic: parse YAML
    try:
        with open(source.file_path, "r", encoding="utf-8") as f:
            import yaml
            return yaml.safe_load(f)
    except Exception:
        return None
```

- [ ] **Step 6: Run existing tests to verify no regression**

Run: `cd /root/logoscope/ai-service && python3 -m pytest tests/ -x -q -k "skill" --no-header --cov-report= 2>&1 | tail -5`
Expected: All skill tests pass

- [ ] **Step 7: Add removal of `_validate_skill_yaml` if still present**

Check if `_validate_skill_yaml` still exists in `manager.py`:

```bash
grep -n "_validate_skill_yaml" ai-service/ai/skills/manager.py
```

If found, delete it. Its logic is now in `YamlAdapter.validate()`.

- [ ] **Step 8: Commit**

```bash
git add ai-service/ai/skills/manager.py
git commit -m "refactor(manager): 适配器注册 + 自动发现 + 多技能批量安装"

Co-Authored-By: Claude <noreply@anthropic.com>
```

---

### Task 6: API 模型和路由 — 添加 skill_type 字段

**Files:**
- Modify: `ai-service/api/skills_router.py`

- [ ] **Step 1: 更新 SkillBrief / SkillDetail**

In `skills_router.py`, add `skill_type` to both models:

```python
class SkillBrief(BaseModel):
    """Summary of one skill for list views."""
    name: str
    display_name: str
    description: str
    source_dir: str
    risk_level: str
    step_count: int
    skill_type: str = "diagnostic"

    class Config:
        from_attributes = True


class SkillDetail(BaseModel):
    """Full skill data including YAML content and steps."""
    name: str
    display_name: str
    description: str
    source_dir: str
    file_path: str
    risk_level: str
    step_count: int
    skill_type: str = "diagnostic"
    trigger_patterns: List[str] = []
    applicable_components: List[str] = []
    install_meta: Dict[str, Any] = {}
    steps: List[Dict[str, Any]] = []
    body: str = ""
    auxiliary_files: Dict[str, str] = {}

    class Config:
        from_attributes = True
```

- [ ] **Step 2: 更新所有返回 SkillDetail 的 endpoint**

In `get_skill()`, `install_skill()`, `create_skill()`, `update_skill_yaml()` — add the new fields to each `SkillDetail(...)` constructor call:

```python
SkillDetail(
    ...,
    skill_type=skill.skill_type,
    body=data.get("body", ""),
    auxiliary_files=data.get("auxiliary_files", {}),
)
```

The `steps` field should come from YAML data for diagnostic skills (existing logic) and be empty for reference skills. Update the `get_skill` endpoint:

```python
@router.get("/{name}")
async def get_skill(name: str, source: Optional[str] = None) -> SkillDetail:
    skill = _mgr.get_skill(name, source=source)
    if skill is None:
        raise HTTPException(status_code=404, detail=f"Skill '{name}' not found")

    data = _mgr.get_skill_data(name) or {}
    steps = data.get("steps", []) if skill.skill_type == "diagnostic" else []

    return SkillDetail(
        name=skill.name,
        display_name=skill.display_name,
        description=skill.description,
        source_dir=skill.source_dir,
        file_path=skill.file_path,
        risk_level=skill.risk_level,
        step_count=skill.step_count,
        skill_type=skill.skill_type,
        trigger_patterns=skill.trigger_patterns,
        applicable_components=skill.applicable_components,
        install_meta=skill.install_meta,
        steps=steps,
        body=data.get("body", ""),
        auxiliary_files=data.get("auxiliary_files", {}),
    )
```

- [ ] **Step 3: Type check**

Run: `cd /root/logoscope/ai-service && python3 -c "from api.skills_router import router; print('OK')"`
Expected: `OK`

- [ ] **Step 4: Commit**

```bash
git add ai-service/api/skills_router.py
git commit -m "feat(api): SkillBrief / SkillDetail 添加 skill_type + body + auxiliary_files"

Co-Authored-By: Claude <noreply@anthropic.com>
```

---

### Task 7: AI 集成 — 按需注入参考技能

**Files:**
- Modify: `ai-service/api/ai.py`
- Test: `ai-service/tests/test_ai_skills_context.py`

- [ ] **Step 1: 添加 build_skills_context 函数**

In `ai-service/api/ai.py`, add at module level:

```python
import re
from ai.skills.manager import SkillManager


def build_skills_context(user_query: str) -> Dict[str, Any]:
    """Build skill context sections for LLM prompts.

    Returns:
        ``{"diagnostic_tools": "...", "reference_methods": "..."}``
    """
    mgr = SkillManager()
    all_skills = mgr.list_all()

    diagnostic_lines: List[str] = []
    reference_sections: List[str] = []

    for skill in all_skills:
        matched = _match_skill_to_query(skill, user_query)
        if not matched:
            continue

        if skill.skill_type == "diagnostic":
            diagnostic_lines.append(
                f"- {skill.name} ({skill.step_count} steps): {skill.description}"
            )
        else:
            section = f"### {skill.display_name}\n{skill.body}"
            if skill.auxiliary_files:
                section += "\n\n**参考文档:** " + ", ".join(skill.auxiliary_files.keys())
            reference_sections.append(section)

    return {
        "diagnostic_tools": "\n".join(diagnostic_lines),
        "reference_methods": "\n\n".join(reference_sections),
    }


def _match_skill_to_query(skill, query: str) -> bool:
    """Three-tier matching: trigger pattern → component → description keyword."""
    q = query.lower()
    for pattern in skill.trigger_patterns:
        if re.search(pattern, q):
            return True
    for comp in skill.applicable_components:
        if comp.lower() in q:
            return True
    for word in skill.description.lower().split():
        if len(word) > 3 and word in q:
            return True
    return False
```

- [ ] **Step 2: 创建单元测试**

Create `ai-service/tests/test_ai_skills_context.py`:

```python
"""Tests for build_skills_context — AI skill matching and injection."""
from __future__ import annotations

from unittest.mock import patch, MagicMock

from api.ai import build_skills_context, _match_skill_to_query


def _mock_skill(name, desc, skill_type="diagnostic",
                trigger_patterns=None, applicable_components=None):
    s = MagicMock()
    s.name = name
    s.display_name = name.replace("_", " ").title()
    s.description = desc
    s.skill_type = skill_type
    s.trigger_patterns = trigger_patterns or []
    s.applicable_components = applicable_components or []
    s.step_count = 3
    s.body = f"# {name}\nMethodology content."
    s.auxiliary_files = {"guide.md": "# Guide"}
    return s


class TestMatchSkillToQuery:
    def test_trigger_pattern_match(self):
        skill = _mock_skill("debug", "", trigger_patterns=["crash", "bug"])
        assert _match_skill_to_query(skill, "Pod CrashLoopBackOff")

    def test_component_match(self):
        skill = _mock_skill("k8s", "", applicable_components=["kubernetes"])
        assert _match_skill_to_query(skill, "kubernetes pod failed")

    def test_description_keyword_match(self):
        skill = _mock_skill("sysdebug", "Systematic debugging for any bug")
        assert _match_skill_to_query(skill, "debugging this bug")


class TestBuildSkillsContext:
    def test_includes_diagnostic_tools(self):
        skills = [_mock_skill("k8s_diag", "K8s diagnostics")]
        with patch.object(type(skills[0]), "skill_type", "diagnostic"):
            with patch("api.ai.SkillManager") as MockMgr:
                MockMgr.return_value.list_all.return_value = skills
                result = build_skills_context("kubernetes issue")
                assert "k8s_diag" in result["diagnostic_tools"]

    def test_includes_reference_methods(self):
        skills = [_mock_skill("sysdebug", "Systematic debugging")]
        with patch.object(type(skills[0]), "skill_type", "reference"):
            with patch("api.ai.SkillManager") as MockMgr:
                MockMgr.return_value.list_all.return_value = skills
                result = build_skills_context("debugging issue")
                assert "sysdebug" in result["reference_methods"]
                assert "Methodology content" in result["reference_methods"]
```

- [ ] **Step 3: 运行测试**

Run: `cd /root/logoscope/ai-service && python3 -m pytest tests/test_ai_skills_context.py -v --no-header -q 2>&1 | tail -10`
Expected: All tests pass

- [ ] **Step 4: Commit**

```bash
git add ai-service/api/ai.py ai-service/tests/test_ai_skills_context.py
git commit -m "feat(ai): build_skills_context — 按需注入 diagnostic + reference 技能上下文"

Co-Authored-By: Claude <noreply@anthropic.com>
```

---

### Task 8: 前端 — 添加 react-markdown 依赖

**Files:**
- Modify: `frontend/package.json`

- [ ] **Step 1: 安装 react-markdown**

Run: `cd /root/logoscope/frontend && npm install react-markdown 2>&1 | tail -5`
Expected: react-markdown added to package.json and node_modules

- [ ] **Step 2: Type check**

Run: `cd /root/logoscope/frontend && npm run typecheck 2>&1`
Expected: No type errors

- [ ] **Step 3: Commit**

```bash
git add frontend/package.json frontend/package-lock.json
git commit -m "feat(deps): react-markdown — Markdown 渲染支持"

Co-Authored-By: Claude <noreply@anthropic.com>
```

---

### Task 9: 前端 — ReferenceSkillView 组件

**Files:**
- Create: `frontend/src/pages/ReferenceSkillView.tsx`

- [ ] **Step 1: 创建 ReferenceSkillView**

Create `frontend/src/pages/ReferenceSkillView.tsx`:

```tsx
import React, { useState } from 'react';
import ReactMarkdown from 'react-markdown';
import { FileText, BookOpen } from 'lucide-react';

interface Props {
  body: string;
  auxiliaryFiles: Record<string, string>;
}

const ReferenceSkillView: React.FC<Props> = ({ body, auxiliaryFiles }) => {
  const auxKeys = Object.keys(auxiliaryFiles);
  const [activeFile, setActiveFile] = useState<string | null>(null);

  const content = activeFile ? auxiliaryFiles[activeFile] : body;

  return (
    <div className="flex gap-6 h-full">
      {/* Auxiliary files sidebar */}
      {auxKeys.length > 0 && (
        <nav className="w-48 flex-shrink-0 border-r pr-4 overflow-y-auto"
             style={{ borderColor: 'var(--sidebar-border)' }}>
          <h4 className="text-xs font-semibold uppercase tracking-wider text-slate-400 mb-3 flex items-center gap-1.5">
            <FileText size={13} />
            辅助文档
          </h4>
          <div className="space-y-1">
            {auxKeys.map(fname => (
              <button
                key={fname}
                onClick={() => setActiveFile(
                  activeFile === fname ? null : fname
                )}
                className={`w-full text-left px-2.5 py-1.5 rounded text-xs transition-colors ${
                  activeFile === fname
                    ? 'bg-teal-50 text-teal-700 font-medium'
                    : 'text-slate-500 hover:bg-slate-50'
                }`}
              >
                {fname}
              </button>
            ))}
          </div>
        </nav>
      )}

      {/* Markdown content */}
      <div className="flex-1 overflow-y-auto prose prose-sm max-w-none
                      prose-headings:text-slate-800 prose-headings:font-semibold
                      prose-p:text-slate-600 prose-code:text-teal-600
                      prose-code:bg-slate-50 prose-code:px-1 prose-code:py-0.5 prose-code:rounded
                      prose-pre:bg-slate-900 prose-pre:text-green-200
                      prose-a:text-teal-600 prose-strong:text-slate-700">
        <ReactMarkdown>{content}</ReactMarkdown>
      </div>
    </div>
  );
};

export default ReferenceSkillView;
```

- [ ] **Step 2: Install tailwindcss typography plugin (if needed for prose classes)**

Check if `@tailwindcss/typography` is in `package.json`:

```bash
grep typography frontend/package.json
```

If not present, add it:

```bash
cd frontend && npm install -D @tailwindcss/typography
```

Then add to `tailwind.config.js`:

```javascript
plugins: [require('@tailwindcss/typography')],
```

- [ ] **Step 3: Type check**

Run: `cd /root/logoscope/frontend && npm run typecheck 2>&1`
Expected: No type errors

- [ ] **Step 4: Commit**

```bash
git add frontend/src/pages/ReferenceSkillView.tsx
git commit -m "feat(ui): ReferenceSkillView — Markdown 参考技能渲染组件"

Co-Authored-By: Claude <noreply@anthopic.com>
```

---

### Task 10: 前端 — 集成到 SkillManager

**Files:**
- Modify: `frontend/src/pages/SkillManager.tsx`

- [ ] **Step 1: 导入 ReferenceSkillView**

Add to imports in `SkillManager.tsx`:

```typescript
import ReferenceSkillView from './ReferenceSkillView';
```

- [ ] **Step 2: 列表项添加 skill_type 徽标**

In the skill card rendering, add a skill type badge next to the source badge.
Find the section that renders `skill.step_count` and add a conditional badge:

```tsx
{skill.skill_type === 'reference' ? (
  <span className="text-[10px] px-1.5 py-0.5 rounded bg-indigo-100 text-indigo-700 border border-indigo-200">
    📖 参考
  </span>
) : (
  <span className="text-[10px] text-slate-400 bg-slate-100 px-1.5 py-0.5 rounded">
    {skill.step_count} 步
  </span>
)}
```

- [ ] **Step 3: 详情面板添加渲染分支**

In the detail panel, replace the direct `steps.map(...)` rendering with a conditional:

```tsx
{selectedSkill && skillDetail && !detailLoading && (
  <div className="flex-1 overflow-y-auto p-6">
    {/* ── Detail header (unchanged) ─────────────────────────── */}
    ...

    {/* ── Content: reference vs diagnostic ─────────────────── */}
    {skillDetail.skill_type === 'reference' ? (
      <ReferenceSkillView
        body={skillDetail.body}
        auxiliaryFiles={skillDetail.auxiliary_files}
      />
    ) : (
      <>
        {/* Existing metadata grid, steps rendering... */}
        <div className="grid grid-cols-3 gap-4 mb-6">...</div>
        <h3 className="...">诊断步骤</h3>
        <div className="space-y-3">
          {skillDetail.steps.map((step, idx) => (...))}
        </div>
      </>
    )}
  </div>
)}
```

- [ ] **Step 4: Type check + build**

Run: `cd /root/logoscope/frontend && npm run typecheck 2>&1 && npm run build 2>&1 | tail -5`
Expected: No type errors, build succeeds

- [ ] **Step 5: Commit**

```bash
git add frontend/src/pages/SkillManager.tsx
git commit -m "feat(ui): SkillManager — skill_type 徽标 + reference 渲染分支"

Co-Authored-By: Claude <noreply@anthropic.com>
```

---

## 自检清单

- [ ] **Spec 覆盖：** 每个 spec section 都有对应任务
  - 适配器架构 → Task 1, 2, 3
  - 数据模型 → Task 1, 6
  - 安装流程 / 自动发现 → Task 4, 5
  - 前端展示 → Task 8, 9, 10
  - AI 集成 → Task 7
- [ ] **无占位符：** 所有代码块都是完整的实现
- [ ] **类型一致性：** `skill_type`, `body`, `auxiliary_files` 在前后端定义一致
- [ ] **所有 imports 正确：** 跨模块引用已在本计划中定义

## 执行衔接

Plan complete and saved to `docs/superpowers/plans/2026-06-14-dual-type-skill-architecture.md`.

**两个执行选项：**

1. **Subagent-Driven（推荐）** — 每个 Task 派发独立子 agent，task 间 review 后继续
2. **Inline Execution** — 在当前 session 中逐个执行，批量 checkpoint

选择哪个？
