"""Configuration for constrained agent-owned repositories."""

from __future__ import annotations

import re
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator

_GITHUB_ORGANIZATION_PATTERN = re.compile(r"^[A-Za-z0-9](?:[A-Za-z0-9-]{0,37}[A-Za-z0-9])?$")


class AgentRepositoriesConfig(BaseModel):
    """Trusted global policy for agent repository provisioning."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    organization: str = Field(description="GitHub organization that owns every agent repository")
    prefix: Literal["MindRoom"] = Field(description="Fixed repository-name prefix")

    @field_validator("organization")
    @classmethod
    def validate_organization(cls, value: str) -> str:
        """Accept one canonical GitHub organization slug."""
        organization = value.strip()
        if not _GITHUB_ORGANIZATION_PATTERN.fullmatch(organization):
            msg = "agent_repositories.organization must be a valid GitHub organization slug"
            raise ValueError(msg)
        return organization
