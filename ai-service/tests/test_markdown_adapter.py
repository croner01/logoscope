"""Tests for MarkdownAdapter — front matter parsing + read + install."""
from __future__ import annotations

import os
import tempfile

from ai.skills.adapters.markdown_adapter import (
    MarkdownAdapter,
    _parse_front_matter,
    _strip_front_matter,
)

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

    def test_read_nonexistent(self):
        adapter = MarkdownAdapter()
        assert adapter.read("/nonexistent/path.md", "installed") is None

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
            assert source.skill_type == "reference"
            skill_dir = os.path.join(installed_dir, "systematic-debugging")
            assert os.path.isdir(skill_dir)
            assert os.path.isfile(os.path.join(skill_dir, "SKILL.md"))

    def test_install_fallback_name(self):
        adapter = MarkdownAdapter()
        parts = {"path": "skills/my_skill/SKILL.md"}
        content = "---\ndescription: No name field\n---\nBody text"
        with tempfile.TemporaryDirectory() as installed_dir:
            source = adapter.install(
                content=content, parts=parts,
                github_url="", raw_url="",
                installed_dir=installed_dir,
            )
            assert source is not None
            assert source.name == "my_skill"  # fallback from path
