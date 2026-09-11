"""Validated, conservative snapshots of live response activity."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, computed_field


@dataclass(eq=False)
class ResponseIdentity:
    """Mutable metadata for one response; equal names still identify distinct responses."""

    responder: str | None = None
    requester_id: str | None = None


class ResponseActivity(BaseModel):
    """Counts of admitted Matrix operations and OpenAI HTTP requests.

    Matrix operations include nested planning/lifecycle slots and are not a
    count of unique responses. Persisted work waiting for approval is not live.
    """

    model_config = ConfigDict(strict=True, frozen=True)

    runtime_phase: str
    admission_paused: bool | None
    active_matrix_operations: int | None = Field(ge=0)
    active_openai_requests: int = Field(ge=0)

    @computed_field
    @property
    def status(self) -> Literal["idle", "busy", "unavailable"]:
        """Fail closed until the live runtime is ready and accepting responses."""
        if self.runtime_phase != "ready" or self.admission_paused is not False or self.active_matrix_operations is None:
            return "unavailable"
        if self.active_matrix_operations or self.active_openai_requests:
            return "busy"
        return "idle"


class ActiveResponseInfo(BaseModel):
    """One response observed at a central response entry point."""

    model_config = ConfigDict(strict=True, frozen=True)

    channel: Literal["matrix", "openai"]
    responder: str | None
    requester_id: str | None


class DetailedResponseActivity(ResponseActivity):
    """Known response identities, independent of nested admission counts."""

    responses: list[ActiveResponseInfo]
