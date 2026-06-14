"""Tests for discovery module — GitHub API response parsing."""
from __future__ import annotations

from unittest.mock import patch

from ai.skills.discovery import discover_skill_urls


class TestDiscoverSkillUrls:
    def test_parses_directory_list(self):
        mock_response = [
            {"name": "systematic-debugging", "type": "dir"},
            {"name": "brainstorming", "type": "dir"},
            {"name": "writing-plans", "type": "dir"},
            {"name": "README.md", "type": "file"},
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
