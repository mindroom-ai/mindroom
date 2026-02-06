"""API endpoints for skill inspection and editing."""

from __future__ import annotations

import re
import shutil

from fastapi import APIRouter, HTTPException
from pydantic import BaseModel

from mindroom.skills import (
    get_user_skills_dir,
    list_skill_listings,
    resolve_skill_listing,
    skill_can_edit,
)

router = APIRouter(prefix="/api/skills", tags=["skills"])

_VALID_SKILL_NAME = re.compile(r"^[a-z0-9][a-z0-9-]*$")


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


@router.get("")
async def list_skills() -> list[SkillSummary]:
    """List installed skills."""
    listings = list_skill_listings()
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
async def get_skill(skill_name: str) -> SkillDetail:
    """Get a specific skill and its content."""
    listing = resolve_skill_listing(skill_name)
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
async def update_skill(skill_name: str, payload: SkillUpdateRequest) -> dict[str, bool]:
    """Update a skill's SKILL.md content."""
    listing = resolve_skill_listing(skill_name)
    if listing is None:
        raise HTTPException(status_code=404, detail="Skill not found")

    if not skill_can_edit(listing.path):
        raise HTTPException(status_code=403, detail="Skill is read-only")

    tmp_path = listing.path.with_suffix(listing.path.suffix + ".tmp")
    try:
        tmp_path.write_text(payload.content, encoding="utf-8")
        tmp_path.replace(listing.path)
    except OSError as exc:
        raise HTTPException(status_code=500, detail="Failed to update skill") from exc

    return {"success": True}


@router.post("")
async def create_skill(payload: CreateSkillRequest) -> SkillSummary:
    """Create a new user skill."""
    name = payload.name.strip()
    if not name:
        raise HTTPException(status_code=422, detail="Skill name must not be empty")
    if not _VALID_SKILL_NAME.match(name):
        raise HTTPException(
            status_code=422,
            detail="Skill name must be lowercase alphanumeric with hyphens, starting with a letter or digit",
        )

    if resolve_skill_listing(name) is not None:
        raise HTTPException(status_code=409, detail="A skill with this name already exists")

    description = payload.description.strip() or name
    skill_dir = get_user_skills_dir() / name
    skill_dir.mkdir(parents=True, exist_ok=True)

    content = f"---\nname: {name}\ndescription: {description}\n---\n\n# {name}\n"
    (skill_dir / "SKILL.md").write_text(content, encoding="utf-8")

    return SkillSummary(name=name, description=description, origin="user", can_edit=True)


@router.delete("/{skill_name}")
async def delete_skill(skill_name: str) -> dict[str, bool]:
    """Delete a user skill."""
    listing = resolve_skill_listing(skill_name)
    if listing is None:
        raise HTTPException(status_code=404, detail="Skill not found")

    if not skill_can_edit(listing.path):
        raise HTTPException(status_code=403, detail="Skill is read-only")

    skill_dir = listing.path.parent
    shutil.rmtree(skill_dir)

    return {"success": True}
