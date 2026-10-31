"""The typed contract for controlling one supervised script process, shared by the primary and its workers.

Kubernetes script workers can outlive a primary upgrade, so the protocol carries its own version, requests reject
fields the receiver does not know, and responses tolerate fields a newer worker adds.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Annotated, Any, Literal

from pydantic import AfterValidator, BaseModel, ConfigDict, Field

from mindroom.script_runs.models import validated_script_run_id

# Bump when the worker launch/status/cancel protocol or script SDK gateway contract
# becomes incompatible with an already-running worker from an earlier image.
SCRIPT_PROTOCOL_VERSION = 2

__all__ = [
    "SCRIPT_PROTOCOL_VERSION",
    "SandboxScriptCancelResponse",
    "SandboxScriptControlRequest",
    "SandboxScriptRunRequest",
    "SandboxScriptRunResponse",
    "SandboxScriptStatusRequest",
    "SandboxScriptStatusResponse",
    "ScriptReceipt",
    "ScriptRunId",
    "ScriptWorkerError",
    "WorkerScriptCancel",
    "WorkerScriptStatus",
]


_ScriptProcessState = Literal["running", "exited", "unknown"]


class ScriptWorkerError(RuntimeError):
    """A script process-control request failed: its worker or local supervisor refused it or could not be reached."""


@dataclass(frozen=True, slots=True)
class WorkerScriptStatus:
    """Normalized state from one supervisor handle."""

    state: _ScriptProcessState
    output: str = ""
    exit_code: int | None = None

    @classmethod
    def unknown_handle(cls) -> WorkerScriptStatus:
        """Return the lifecycle fact used when a supervisor lost its handle."""
        return cls(state="unknown")


@dataclass(frozen=True, slots=True)
class WorkerScriptCancel:
    """Normalized cancellation signal outcome."""

    cancel_requested: bool
    already_finished: bool
    unknown_handle: bool


def _validate_protocol_version(value: int) -> int:
    if value != SCRIPT_PROTOCOL_VERSION:
        message = f"Input should be {SCRIPT_PROTOCOL_VERSION}"
        raise ValueError(message)
    return value


ScriptRunId = Annotated[str, AfterValidator(validated_script_run_id)]
_ProtocolVersion = Annotated[int, Field(strict=True), AfterValidator(_validate_protocol_version)]


class SandboxScriptRunRequest(BaseModel):
    """Validated launch description for one already-snapshotted script."""

    model_config = ConfigDict(extra="forbid")

    protocol_version: _ProtocolVersion
    run_id: ScriptRunId
    worker_key: str = Field(min_length=1, max_length=1024)
    state_scope_worker_key: str | None = Field(default=None, min_length=1, max_length=1024)
    source_digest: str = Field(min_length=64, max_length=64, pattern=r"[0-9a-f]{64}")
    gateway_url: str = Field(min_length=1, max_length=2048)
    max_runtime_seconds: int = Field(gt=0, strict=True)
    private_agent_names: list[str] | None = Field(default=None, max_length=128)
    config_snapshot: dict[str, Any] | None = None


class SandboxScriptStatusRequest(BaseModel):
    """Worker identity for a status poll, sent in the body because access logs record URLs."""

    model_config = ConfigDict(extra="forbid")

    worker_key: str = Field(min_length=1, max_length=1024)


class SandboxScriptControlRequest(BaseModel):
    """Worker identity and signal strength for a cancellation; the run's handle is derived, never sent."""

    model_config = ConfigDict(extra="forbid")

    worker_key: str = Field(min_length=1, max_length=1024)
    force: bool = False


class ScriptReceipt(BaseModel):
    """What every script route answers: whether the operation succeeded, and why not."""

    ok: bool = Field(strict=True)
    error: str | None = None


class SandboxScriptRunResponse(ScriptReceipt):
    """Worker launch receipt."""


class SandboxScriptStatusResponse(ScriptReceipt):
    """Normalized supervisor status receipt."""

    state: _ScriptProcessState
    output: str = ""
    exit_code: int | None = Field(default=None, strict=True)


class SandboxScriptCancelResponse(ScriptReceipt):
    """Normalized supervisor cancellation receipt."""

    cancel_requested: bool = Field(strict=True)
    already_finished: bool = Field(default=False, strict=True)
    unknown_handle: bool = Field(default=False, strict=True)
