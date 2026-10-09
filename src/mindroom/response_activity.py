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
    """Counts of live work that a runtime restart would interrupt.

    Matrix operations include nested planning/lifecycle slots and are not a
    count of unique responses. Persisted work waiting for approval is not live.
    Script runs whose worker process a restart preserves are reported
    separately and do not make the runtime busy.
    """

    model_config = ConfigDict(strict=True, frozen=True)

    runtime_phase: str
    admission_paused: bool | None
    active_matrix_operations: int | None = Field(ge=0)
    active_openai_requests: int = Field(ge=0)
    active_calls: int | None = Field(ge=0)
    interruptible_script_runs: int | None = Field(ge=0)
    recoverable_script_runs: int | None = Field(ge=0)

    @computed_field
    @property
    def status(self) -> Literal["idle", "busy", "unavailable"]:
        """Fail closed until the live runtime is ready and every source was read."""
        if (
            self.runtime_phase != "ready"
            or self.admission_paused is not False
            or self.active_matrix_operations is None
            or self.active_calls is None
            or self.interruptible_script_runs is None
            or self.recoverable_script_runs is None
        ):
            return "unavailable"
        if (
            self.active_matrix_operations
            or self.active_openai_requests
            or self.active_calls
            or self.interruptible_script_runs
        ):
            return "busy"
        return "idle"


class ActiveResponseInfo(BaseModel):
    """One response or voice call observed at a central entry point."""

    model_config = ConfigDict(strict=True, frozen=True)

    channel: Literal["matrix", "openai", "call"]
    responder: str | None
    requester_id: str | None


class ActiveScriptRunInfo(BaseModel):
    """One unfinished background script run and whether a restart would adopt it."""

    model_config = ConfigDict(strict=True, frozen=True)

    run_id: str
    responder: str
    requester_id: str
    recoverable: bool


class DetailedResponseActivity(ResponseActivity):
    """Known response, call, and script identities, independent of nested admission counts."""

    responses: list[ActiveResponseInfo]
    script_runs: list[ActiveScriptRunInfo]
