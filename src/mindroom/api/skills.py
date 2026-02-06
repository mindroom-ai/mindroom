"""API endpoints for skill inspection and editing."""

from __future__ import annotations

from fastapi import APIRouter, HTTPException
from pydantic import BaseModel

from mindroom.skills import list_skill_listings, resolve_skill_listing, skill_can_edit

router = APIRouter(prefix="/api/skills", tags=["skills"])


class SkillSummary(BaseModel):
    """Summary information for a skill."""

    name: str
    description: str
    origin: str
    can_edit: bool


class SkillDetail(SkillSummary):
    """Detailed skill information including content."""

    content: str


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
