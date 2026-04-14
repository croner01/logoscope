"""Project knowledge pack loader and runtime selector."""

from __future__ import annotations

from functools import lru_cache
import logging
import os
from pathlib import Path
import re
from typing import Any, Dict, List, Optional


PROJECT_KNOWLEDGE_PACK_VERSION = "2026-04-14.v2"
logger = logging.getLogger(__name__)

_SERVICE_MANIFEST = {
    "ai-service": {"asset": "services/ai-service.md", "aliases": ["ai-service"]},
    "frontend": {"asset": "services/frontend.md", "aliases": ["frontend"]},
    "ingest-service": {"asset": "services/ingest-service.md", "aliases": ["ingest-service"]},
    "query-service": {"asset": "services/query-service.md", "aliases": ["query-service"]},
    "semantic-engine": {"asset": "services/semantic-engine.md", "aliases": ["semantic-engine"]},
    "topology-service": {"asset": "services/topology-service.md", "aliases": ["topology-service"]},
}

_PATH_MANIFEST = {
    "ai-runtime-diagnosis": {
        "asset": "paths/ai-runtime-diagnosis.md",
        "keywords": ["runtime", "follow-up", "followup", "blocked_reason", "planning", "command"],
        "related_services": ["ai-service", "frontend"],
    },
    "log-ingest-query": {
        "asset": "paths/log-ingest-query.md",
        "keywords": ["log", "logs", "clickhouse", "query", "query_log", "code:241"],
        "related_services": ["ingest-service", "semantic-engine", "query-service"],
    },
    "topology-generation-preview": {
        "asset": "paths/topology-generation-preview.md",
        "keywords": ["topology", "edge", "hybrid", "preview", "graph"],
        "related_services": ["semantic-engine", "topology-service", "query-service"],
    },
    "trace-request-correlation": {
        "asset": "paths/trace-request-correlation.md",
        "keywords": ["trace", "request_id", "request id", "time window", "timestamp"],
        "related_services": ["frontend", "ai-service", "query-service"],
    },
}


def _as_str(value: Any, default: str = "") -> str:
    if value is None:
        return default
    if isinstance(value, str):
        return value.strip()
    return str(value).strip()


def extract_markdown_sections(content: str) -> Dict[str, str]:
    sections: Dict[str, List[str]] = {}
    current = ""
    for raw_line in str(content or "").splitlines():
        line = raw_line.rstrip()
        heading = re.match(r"^##\s+(.+?)\s*$", line)
        if heading:
            current = heading.group(1).strip()
            sections.setdefault(current, [])
            continue
        if current:
            sections[current].append(line)
    return {key: "\n".join(value).strip() for key, value in sections.items()}


@lru_cache(maxsize=4)
def load_project_knowledge_registry(knowledge_root: Path) -> Dict[str, Dict[str, Dict[str, Any]]]:
    root = Path(knowledge_root)
    registry = {"services": {}, "paths": {}}

    if not root.exists():
        logger.warning("Project knowledge root does not exist: %s", root)
        return registry

    for service_name, meta in _SERVICE_MANIFEST.items():
        asset_path = root / meta["asset"]
        if not asset_path.exists():
            logger.warning("Project knowledge asset missing: %s", asset_path)
            continue
        content = asset_path.read_text(encoding="utf-8")
        sections = extract_markdown_sections(content)
        registry["services"][service_name] = {
            "name": service_name,
            "asset_path": str(asset_path),
            "summary": sections.get("Summary", ""),
            "preferred_evidence": sections.get("Preferred Evidence Sources", ""),
            "cautions": sections.get("Common Failures and Cautions", ""),
            "entry_hints": sections.get("Diagnosis Entry Hints", ""),
            "sources": sections.get("Sources", ""),
        }

    for path_name, meta in _PATH_MANIFEST.items():
        asset_path = root / meta["asset"]
        if not asset_path.exists():
            logger.warning("Project knowledge asset missing: %s", asset_path)
            continue
        content = asset_path.read_text(encoding="utf-8")
        sections = extract_markdown_sections(content)
        registry["paths"][path_name] = {
            "name": path_name,
            "asset_path": str(asset_path),
            "summary": sections.get("Summary", ""),
            "preferred_evidence": sections.get("Preferred Evidence Sources", ""),
            "first_checks": sections.get("Recommended First Checks", ""),
            "misreads": sections.get("Common Misreads", ""),
            "sources": sections.get("Sources", ""),
            "keywords": list(meta["keywords"]),
            "related_services": list(meta["related_services"]),
        }

    return registry


def _default_knowledge_root() -> Path:
    configured_root = _as_str(os.getenv("AI_PROJECT_KNOWLEDGE_ROOT")).strip()
    candidates: List[Path] = []

    if configured_root:
        candidates.append(Path(configured_root))

    module_path = Path(__file__).resolve()
    base_candidates = [
        module_path.parents[1] if len(module_path.parents) > 1 else None,
        module_path.parents[2] if len(module_path.parents) > 2 else None,
        Path.cwd(),
    ]
    for base in base_candidates:
        if base is None:
            continue
        candidate = Path(base) / "docs" / "superpowers" / "knowledge"
        if candidate not in candidates:
            candidates.append(candidate)

    for candidate in candidates:
        if candidate.exists():
            return candidate

    return candidates[0] if candidates else Path("docs") / "superpowers" / "knowledge"


def _normalize_lines(text: str, *, max_lines: int = 3) -> List[str]:
    lines: List[str] = []
    for raw in str(text or "").splitlines():
        line = raw.strip().lstrip("- ").strip()
        if not line:
            continue
        if line in lines:
            continue
        lines.append(line)
        if len(lines) >= max_lines:
            break
    return lines


def select_project_knowledge(
    analysis_context: Dict[str, Any],
    *,
    knowledge_root: Optional[Path] = None,
) -> Dict[str, Any]:
    safe_context = analysis_context if isinstance(analysis_context, dict) else {}
    registry = load_project_knowledge_registry(Path(knowledge_root or _default_knowledge_root()))
    knowledge_available = bool(registry["services"]) or bool(registry["paths"])
    safe_service = _as_str(safe_context.get("service_name")).lower()
    search_text = " ".join(
        item
        for item in [
            _as_str(safe_context.get("question")),
            _as_str(safe_context.get("input_text")),
            _as_str(safe_context.get("analysis_type")),
            safe_service,
        ]
        if item
    ).lower()

    primary_service = registry["services"].get(safe_service, {})
    selected_path_name = ""
    selected_path: Dict[str, Any] = {}
    best_score = 0
    for path_name, asset in registry["paths"].items():
        score = sum(1 for keyword in asset.get("keywords", []) if keyword.lower() in search_text)
        if safe_service and safe_service in asset.get("related_services", []):
            score += 1
        if score > best_score:
            best_score = score
            selected_path_name = path_name
            selected_path = asset

    related_services: List[str] = []
    for service_name in selected_path.get("related_services", []):
        if not service_name or service_name == safe_service:
            continue
        related_services.append(service_name)
        if len(related_services) >= 2:
            break

    entry_hints: List[str] = []
    entry_hints.extend(_normalize_lines(primary_service.get("entry_hints", ""), max_lines=2))
    entry_hints.extend(_normalize_lines(selected_path.get("first_checks", ""), max_lines=3))
    entry_hints = entry_hints[:5]

    cautions: List[str] = []
    cautions.extend(_normalize_lines(primary_service.get("cautions", ""), max_lines=2))
    cautions.extend(_normalize_lines(selected_path.get("misreads", ""), max_lines=2))
    cautions = cautions[:3]

    prompt_lines: List[str] = []
    if primary_service:
        prompt_lines.append(f"服务摘要: {primary_service.get('summary', '')}")
    if selected_path:
        prompt_lines.append(f"链路摘要: {selected_path.get('summary', '')}")
    if entry_hints:
        prompt_lines.append("优先排查入口:")
        prompt_lines.extend(f"- {item}" for item in entry_hints)
    if cautions:
        prompt_lines.append("注意误判:")
        prompt_lines.extend(f"- {item}" for item in cautions)

    selection_reason_parts: List[str] = []
    if primary_service:
        selection_reason_parts.append(f"service={safe_service}")
    if selected_path_name:
        selection_reason_parts.append(f"path={selected_path_name}")
    if not selection_reason_parts and not knowledge_available:
        selection_reason_parts.append("fallback=knowledge_unavailable")
    elif not selection_reason_parts:
        selection_reason_parts.append("fallback=minimal")

    return {
        "knowledge_pack_version": PROJECT_KNOWLEDGE_PACK_VERSION,
        "knowledge_primary_service": safe_service if primary_service else "",
        "knowledge_primary_path": selected_path_name,
        "knowledge_related_services": related_services,
        "knowledge_selection_reason": ",".join(selection_reason_parts),
        "knowledge_entry_hints": entry_hints,
        "project_knowledge_prompt": "\n".join(item for item in prompt_lines if item).strip(),
    }
