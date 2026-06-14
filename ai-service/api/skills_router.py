"""
Skills management REST API — list / install / create / edit / delete / update.

Provides the backend endpoints consumed by the frontend Skill Manager page
and the ``skill`` CLI tool.
"""
from __future__ import annotations

import logging
from typing import Any, Dict, List, Optional

from fastapi import APIRouter, HTTPException
from pydantic import BaseModel, Field

from ai.skills.manager import SkillManager

logger = logging.getLogger(__name__)
router = APIRouter(tags=["skills"])
# Note: prefix "/api/v1/skills" is set in main.py via app.include_router(router, prefix=...)

# Keep one instance around — its constructor ensures the dirs exist.
_mgr = SkillManager()


# ── Request / response models ───────────────────────────────────────────────

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
    """Full skill data including YAML/markdown content and steps."""
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


class InstallRequest(BaseModel):
    url: str = Field(..., description="github://owner/repo/path/to/skill.yaml")


class CreateRequest(BaseModel):
    name: str = Field(..., description="Skill name (alphanumeric + underscores)")


class ImportRequest(BaseModel):
    name: str = Field(..., description="Skill name")
    yaml_content: str = Field(..., description="Full YAML content of the skill")


# ── Endpoints ────────────────────────────────────────────────────────────────

@router.get("")
async def list_skills(source: Optional[str] = None) -> List[SkillBrief]:
    """List all available skills, optionally filtered by *source*."""
    all_skills = _mgr.list_all()
    if source:
        all_skills = [s for s in all_skills if s.source_dir == source]
    return [
        SkillBrief(
            name=s.name,
            display_name=s.display_name,
            description=s.description,
            source_dir=s.source_dir,
            risk_level=s.risk_level,
            step_count=s.step_count,
        )
        for s in sorted(all_skills, key=lambda x: x.name)
    ]


@router.get("/{name}")
async def get_skill(name: str, source: Optional[str] = None) -> SkillDetail:
    """Get full skill details including YAML steps."""
    skill = _mgr.get_skill(name, source=source)
    if skill is None:
        raise HTTPException(status_code=404, detail=f"Skill '{name}' not found")

    data = _mgr.get_skill_data(name) or {}

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
        steps=data.get("steps", []) if skill.skill_type == "diagnostic" else [],
        body=data.get("body", ""),
        auxiliary_files=data.get("auxiliary_files", {}),
    )


@router.post("/install")
async def install_skill(body: InstallRequest) -> SkillDetail:
    """Download a skill YAML from GitHub and install it."""
    try:
        results = _mgr.install(body.url)
    except (ValueError, FileExistsError) as e:
        raise HTTPException(status_code=400, detail=str(e))

    if not results:
        raise HTTPException(status_code=400, detail="No skills were installed")
    src = results[0]

    data = _mgr.get_skill_data(src.name) or {}
    return SkillDetail(
        name=src.name,
        display_name=src.display_name,
        description=src.description,
        source_dir=src.source_dir,
        file_path=src.file_path,
        risk_level=src.risk_level,
        step_count=src.step_count,
        skill_type=src.skill_type,
        trigger_patterns=src.trigger_patterns,
        applicable_components=src.applicable_components,
        install_meta=src.install_meta,
        steps=data.get("steps", []) if src.skill_type == "diagnostic" else [],
        body=data.get("body", ""),
        auxiliary_files=data.get("auxiliary_files", {}),
    )


@router.post("/create")
async def create_skill(body: CreateRequest) -> SkillDetail:
    """Create a new custom skill from a template."""
    try:
        src = _mgr.create(body.name)
    except (ValueError, FileExistsError) as e:
        raise HTTPException(status_code=400, detail=str(e))

    data = _mgr.get_skill_data(src.name) or {}
    return SkillDetail(
        name=src.name,
        display_name=src.display_name,
        description=src.description,
        source_dir=src.source_dir,
        file_path=src.file_path,
        risk_level=src.risk_level,
        step_count=src.step_count,
        skill_type=src.skill_type,
        trigger_patterns=src.trigger_patterns,
        applicable_components=src.applicable_components,
        install_meta=src.install_meta,
        steps=data.get("steps", []) if src.skill_type == "diagnostic" else [],
        body=data.get("body", ""),
        auxiliary_files=data.get("auxiliary_files", {}),
    )


@router.put("/{name}")
async def update_skill_yaml(name: str, body: ImportRequest) -> SkillDetail:
    """Replace the YAML content of a custom skill (frontend editor save)."""
    # Verify it exists and is in custom/
    skill = _mgr.get_skill(name, source="custom")
    if skill is None:
        raise HTTPException(
            status_code=404,
            detail=f"Skill '{name}' not found in custom/. Only custom skills can be edited via API.",
        )

    try:
        src = _mgr.import_yaml(name, body.yaml_content)
    except (ValueError, FileExistsError) as e:
        raise HTTPException(status_code=400, detail=str(e))

    data = _mgr.get_skill_data(src.name) or {}
    return SkillDetail(
        name=src.name,
        display_name=src.display_name,
        description=src.description,
        source_dir=src.source_dir,
        file_path=src.file_path,
        risk_level=src.risk_level,
        step_count=src.step_count,
        skill_type=src.skill_type,
        trigger_patterns=src.trigger_patterns,
        applicable_components=src.applicable_components,
        install_meta=src.install_meta,
        steps=data.get("steps", []) if src.skill_type == "diagnostic" else [],
        body=data.get("body", ""),
        auxiliary_files=data.get("auxiliary_files", {}),
    )


@router.delete("/{name}")
async def delete_skill(name: str) -> Dict[str, Any]:
    """Delete a skill from installed/ or custom/."""
    deleted = _mgr.remove(name)
    if not deleted:
        raise HTTPException(
            status_code=404,
            detail=f"Skill '{name}' not found in installed/ or custom/. Builtin skills cannot be deleted.",
        )
    return {"status": "deleted", "name": name}


@router.post("/{name}/update")
async def update_skill(name: str) -> Dict[str, Any]:
    """Re-download an installed skill from its original GitHub URL."""
    success = _mgr.update(name)
    if not success:
        raise HTTPException(
            status_code=400,
            detail=f"Skill '{name}' is not a GitHub-installed skill or has no source URL.",
        )
    return {"status": "updated", "name": name}
