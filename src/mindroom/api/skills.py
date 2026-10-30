"""API endpoints for skill inspection and editing."""

from __future__ import annotations

import re
import shutil
from typing import TYPE_CHECKING

import yaml
from fastapi import APIRouter, HTTPException, Request
from pydantic import BaseModel

from mindroom.api import config_lifecycle
from mindroom.constants import safe_replace
from mindroom.tool_system.plugins import config_plugins
from mindroom.tool_system.skills import (
    get_user_skills_dir,
    list_skill_listings,
    resolve_skill_listing,
    skill_can_edit,
)

if TYPE_CHECKING:
    from pathlib import Path

    from mindroom.tool_system.skills import SkillListing

router = APIRouter(prefix="/api/skills", tags=["skills"])

_VALID_SKILL_NAME = re.compile(r"^[a-z0-9]([a-z0-9-]*[a-z0-9])?$")


class SkillSummary(BaseModel):
    """Summary information for a skill."""

    name: str
    description: str
    origin: str
    can_edit: bool


class SkillDetail(SkillSummary):
    """Detailed skill information including content."""

    content: str


class CreateSkillRequest(BaseModel):
    """Request payload for creating a new skill."""

    name: str
    description: str


class SkillUpdateRequest(BaseModel):
    """Request payload for updating a skill."""

    content: str


def _plugin_skill_roots(request: Request) -> tuple[Path, ...]:
    """Return the skill directories of the plugins the request's committed config loaded."""
    snapshot = config_lifecycle.bind_current_request_snapshot(request)
    if snapshot.runtime_config is None:
        return ()
    return config_plugins(snapshot.runtime_config, snapshot.runtime_paths).skill_roots


def _listing(skill_name: str, request: Request) -> SkillListing | None:
    return resolve_skill_listing(skill_name, _plugin_skill_roots(request))


@router.get("")
async def list_skills(request: Request) -> list[SkillSummary]:
    """List installed skills."""
    listings = list_skill_listings(_plugin_skill_roots(request))
    return [
        SkillSummary(
            name=listing.name,
            description=listing.description,
            origin=listing.origin,
            can_edit=skill_can_edit(listing.path),
        )
        for listing in listings
    ]


@router.get("/{skill_name}")
async def get_skill(skill_name: str, request: Request) -> SkillDetail:
    """Get a specific skill and its content."""
    listing = _listing(skill_name, request)
    if listing is None:
        raise HTTPException(status_code=404, detail="Skill not found")

    try:
        content = listing.path.read_text(encoding="utf-8")
    except OSError as exc:
        raise HTTPException(status_code=500, detail="Failed to read skill content") from exc

    return SkillDetail(
        name=listing.name,
        description=listing.description,
        origin=listing.origin,
        can_edit=skill_can_edit(listing.path),
        content=content,
    )


@router.put("/{skill_name}")
async def update_skill(skill_name: str, payload: SkillUpdateRequest, request: Request) -> dict[str, bool]:
    """Update a skill's SKILL.md content."""
    listing = _listing(skill_name, request)
    if listing is None:
        raise HTTPException(status_code=404, detail="Skill not found")

    if not skill_can_edit(listing.path):
        raise HTTPException(status_code=403, detail="Skill is read-only")

    tmp_path = listing.path.with_suffix(listing.path.suffix + ".tmp")
    try:
        tmp_path.write_text(payload.content, encoding="utf-8")
        safe_replace(tmp_path, listing.path)
    except OSError as exc:
        raise HTTPException(status_code=500, detail="Failed to update skill") from exc

    return {"success": True}


@router.post("")
async def create_skill(payload: CreateSkillRequest, request: Request) -> SkillSummary:
    """Create a new user skill."""
    name = payload.name.strip()
    if not name:
        raise HTTPException(status_code=422, detail="Skill name must not be empty")
    if not _VALID_SKILL_NAME.match(name):
        raise HTTPException(
            status_code=422,
            detail="Skill name must be lowercase alphanumeric with hyphens, starting with a letter or digit",
        )

    if _listing(name, request) is not None:
        raise HTTPException(status_code=409, detail="A skill with this name already exists")

    description = payload.description.strip() or name
    skill_dir = get_user_skills_dir() / name
    try:
        skill_dir.mkdir(parents=True, exist_ok=False)
    except FileExistsError as exc:
        raise HTTPException(status_code=409, detail="A skill with this name already exists") from exc

    frontmatter = yaml.dump({"name": name, "description": description}, default_flow_style=False).strip()
    content = f"---\n{frontmatter}\n---\n\n# {name}\n"
    (skill_dir / "SKILL.md").write_text(content, encoding="utf-8")

    return SkillSummary(name=name, description=description, origin="user", can_edit=True)


@router.delete("/{skill_name}")
async def delete_skill(skill_name: str, request: Request) -> dict[str, bool]:
    """Delete a user skill."""
    listing = _listing(skill_name, request)
    if listing is None:
        raise HTTPException(status_code=404, detail="Skill not found")

    if not skill_can_edit(listing.path):
        raise HTTPException(status_code=403, detail="Skill is read-only")

    skill_dir = listing.path.parent.resolve()
    user_root = get_user_skills_dir().expanduser().resolve()
    if skill_dir == user_root:
        raise HTTPException(status_code=400, detail="Cannot delete a root-level skill")
    shutil.rmtree(skill_dir)

    return {"success": True}
