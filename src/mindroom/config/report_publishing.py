"""Published-report policy configuration."""

from __future__ import annotations

from pydantic import BaseModel, ConfigDict, Field, field_serializer

from mindroom.report_access_policy import ReportAccessPolicy


class ReportPublishingConfig(BaseModel):
    """Policy for creating new published reports."""

    model_config = ConfigDict(extra="forbid")

    default_access_policy: ReportAccessPolicy = Field(
        default=ReportAccessPolicy.PUBLIC,
        description="Default access policy for newly published reports",
    )
    allow_public: bool = Field(
        default=True,
        description="Whether agents may create new public bearer-link reports",
    )

    @field_serializer("default_access_policy")
    def serialize_default_access_policy(self, value: ReportAccessPolicy) -> str:
        """Serialize policy as portable YAML-safe text."""
        return value.value
