"""Typed models for workspace-authored automation files."""

from __future__ import annotations

import re
from dataclasses import dataclass
from typing import TYPE_CHECKING, Annotated, Any, Literal

from croniter import croniter
from pydantic import AfterValidator, BaseModel, ConfigDict, Field

if TYPE_CHECKING:
    from pathlib import Path

type _WorkspaceAutomationCheckType = Literal["shell"]
type _WorkspaceAutomationActionType = Literal["none", "agent_message", "matrix_message", "hook"]

_AUTOMATION_ID_PATTERN = re.compile(r"^[A-Za-z0-9_.-]+$")
_MATRIX_EVENT_ID_PATTERN = re.compile(r"^\$[^\s]+$")
_DEFAULT_OUTPUT_TAIL_LINES = 100


def is_path_safe_automation_id(value: str) -> bool:
    """Return whether an automation ID is a single path-safe identifier."""
    return _AUTOMATION_ID_PATTERN.fullmatch(value) is not None


def _validate_command(value: str) -> str:
    stripped = value.strip()
    if not stripped:
        msg = "check.command must not be empty"
        raise ValueError(msg)
    return stripped


def _validate_room(value: str | None) -> str | None:
    if value is None:
        return None
    stripped = value.strip()
    if not stripped:
        msg = "action.room must not be empty when provided"
        raise ValueError(msg)
    return stripped


def _validate_thread_id(value: str | None) -> str | None:
    if value is None:
        return None
    stripped = value.strip()
    if not _MATRIX_EVENT_ID_PATTERN.fullmatch(stripped):
        msg = "action.thread_id must be a literal Matrix event ID"
        raise ValueError(msg)
    return stripped


def _validate_message(value: str | None) -> str | None:
    if value is None:
        return None
    stripped = value.strip()
    if not stripped:
        msg = "action.message must not be empty when provided"
        raise ValueError(msg)
    return stripped


def _validate_schedule(value: str) -> str:
    stripped = value.strip()
    if len(stripped.split()) != 5 or not croniter.is_valid(stripped):
        msg = "schedule must be a valid five-field cron expression"
        raise ValueError(msg)
    return stripped


_ShellCommand = Annotated[str, AfterValidator(_validate_command)]
_AutomationSchedule = Annotated[str, AfterValidator(_validate_schedule)]
_ActionRoom = Annotated[str | None, AfterValidator(_validate_room)]
_ActionThreadId = Annotated[str | None, AfterValidator(_validate_thread_id)]
_ActionMessage = Annotated[str | None, AfterValidator(_validate_message)]


class WorkspaceAutomationCheck(BaseModel):
    """A deterministic workspace automation check."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    type: _WorkspaceAutomationCheckType
    command: _ShellCommand
    timeout_seconds: int = Field(ge=1)
    tail: int = Field(default=_DEFAULT_OUTPUT_TAIL_LINES, ge=1)


class WorkspaceAutomationTrigger(BaseModel):
    """Trigger rules for deciding when an automation action fires."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    exit_code: int | None = None


class WorkspaceAutomationAction(BaseModel):
    """Action to perform after an automation trigger matches."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    type: _WorkspaceAutomationActionType
    room: _ActionRoom = None
    thread_id: _ActionThreadId = None
    message: _ActionMessage = None


class WorkspaceAutomationDefinition(BaseModel):
    """One authored automation definition."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    enabled: bool = True
    schedule: _AutomationSchedule
    check: WorkspaceAutomationCheck
    trigger: WorkspaceAutomationTrigger | None = None
    action: WorkspaceAutomationAction


class WorkspaceAutomationFile(BaseModel):
    """Top-level workspace automation YAML file."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    version: Literal[1]
    automations: dict[str, Any]


@dataclass(frozen=True)
class WorkspaceAutomationLoadError:
    """Structured validation error from loading one automation file."""

    file_path: Path
    automation_id: str | None
    field_path: tuple[str | int, ...]
    message: str


@dataclass(frozen=True)
class LoadedWorkspaceAutomation:
    """Normalized runtime record for one enabled workspace automation."""

    agent_name: str
    automation_id: str
    workspace_root: Path
    file_path: Path
    schedule: str
    check: WorkspaceAutomationCheck
    trigger: WorkspaceAutomationTrigger | None
    action: WorkspaceAutomationAction


@dataclass(frozen=True)
class WorkspaceAutomationLoadResult:
    """Result of loading one agent workspace automation file."""

    automations: tuple[LoadedWorkspaceAutomation, ...] = ()
    errors: tuple[WorkspaceAutomationLoadError, ...] = ()
