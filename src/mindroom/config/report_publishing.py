"""Published-report policy configuration."""

from __future__ import annotations

from typing import Literal

from pydantic import BaseModel, ConfigDict, Field

ReportAccessPolicy = Literal["public", "origin_room"]


class ReportPublishingConfig(BaseModel):
    """Policy for creating new published reports."""

    model_config = ConfigDict(extra="forbid")

    default_access_policy: ReportAccessPolicy = Field(
        default="public",
        description="Default access policy for newly published reports",
    )
    allow_public: bool = Field(
        default=True,
        description="Whether agents may create new public bearer-link reports",
    )
