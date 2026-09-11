"""Read-only confirmation of a runtime configuration reload."""

from __future__ import annotations

from typing import Literal

from pydantic import BaseModel, ConfigDict, Field


class ConfigReloadStatus(BaseModel):
    """Latest reload result, identified by the source bytes read by its loader.

    Completion means the reload plan returned successfully, not that every
    configured bot is healthy. An incomplete parse has no proven fingerprint.
    """

    model_config = ConfigDict(strict=True, frozen=True)

    status: Literal["unavailable", "pending", "applied", "failed", "restart_required"] = "unavailable"
    fingerprint: str | None = Field(default=None, pattern=r"^[0-9a-f]{64}$")
