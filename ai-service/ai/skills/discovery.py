"""GitHub repository discovery — list skill directories, download index.yaml."""
from __future__ import annotations

import json
import logging
import urllib.error
import urllib.request
from typing import Any, List, Optional

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


def discover_skill_urls(owner: str, repo: str, ref: str = "main") -> List[str]:
    """Scan a GitHub repo's skills/ directory and return SKILL.md URLs."""
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
    raw_url = f"https://raw.githubusercontent.com/{owner}/{repo}/{ref}/index.yaml"
    req = urllib.request.Request(raw_url, headers={"User-Agent": "logoscope/1.0"})
    try:
        with urllib.request.urlopen(req, timeout=15) as resp:
            return resp.read().decode("utf-8")
    except urllib.error.HTTPError:
        return None
