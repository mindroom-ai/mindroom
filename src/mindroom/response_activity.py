"""Validated, conservative snapshots of live response activity."""

from __future__ import annotations

from typing import Literal, Self

from pydantic import BaseModel, ConfigDict, Field, computed_field, model_validator


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
    """One grouped identity and its active operation count."""

    model_config = ConfigDict(strict=True, frozen=True)

    channel: Literal["matrix", "openai"]
    responder: str | None
    requester_id: str | None
    operations: int = Field(gt=0)


class DetailedResponseActivity(ResponseActivity):
    """Operator-only identities reconciled against the authoritative counters."""

    responses: list[ActiveResponseInfo]

    @model_validator(mode="after")
    def validate_operation_totals(self) -> Self:
        """Reject detail rows that contradict either channel's aggregate count."""
        matrix = sum(row.operations for row in self.responses if row.channel == "matrix")
        openai = sum(row.operations for row in self.responses if row.channel == "openai")
        if matrix != (self.active_matrix_operations or 0) or openai != self.active_openai_requests:
            message = "Detailed response operation totals must match aggregate counts"
            raise ValueError(message)
        return self
