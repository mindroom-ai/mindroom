"""Typed models for workspace-authored automation files."""

from __future__ import annotations

import re
from dataclasses import dataclass
from typing import TYPE_CHECKING, Annotated, Any, Literal

from croniter import CroniterBadCronError, CroniterBadDateError, croniter
from pydantic import AfterValidator, BaseModel, BeforeValidator, ConfigDict, Field

from mindroom.config.models import WorkspaceAutomationActionName

if TYPE_CHECKING:
    from pathlib import Path

type _WorkspaceAutomationCheckType = Literal["shell"]
type _WorkspaceAutomationActionType = Literal["none"] | WorkspaceAutomationActionName

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


def _validate_regex(value: str) -> str:
    try:
        re.compile(value)
    except re.error as exc:
        msg = f"must be a valid regular expression: {exc}"
        raise ValueError(msg) from exc
    return value


def _validate_schedule(value: str) -> str:
    stripped = value.strip()
    if len(stripped.split()) != 5 or not croniter.is_valid(stripped):
        msg = "schedule must be a valid five-field cron expression"
        raise ValueError(msg)
    try:
        croniter(stripped).get_next()
    except (CroniterBadCronError, CroniterBadDateError) as exc:
        msg = "schedule cannot produce any runs"
        raise ValueError(msg) from exc
    return stripped


def _validate_version(value: object) -> int:
    if type(value) is not int or value != 1:
        msg = "version must be exactly integer 1"
        raise ValueError(msg)
    return value


_ShellCommand = Annotated[str, AfterValidator(_validate_command)]
_AutomationSchedule = Annotated[str, AfterValidator(_validate_schedule)]
_AutomationFileVersion = Annotated[int, BeforeValidator(_validate_version)]
_ActionRoom = Annotated[str | None, AfterValidator(_validate_room)]
_ActionThreadId = Annotated[str | None, AfterValidator(_validate_thread_id)]
_ActionMessage = Annotated[str | None, AfterValidator(_validate_message)]
_TriggerRegex = Annotated[str, AfterValidator(_validate_regex)]


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
    stdout_matches: _TriggerRegex | None = None
    stderr_matches: _TriggerRegex | None = None
    stdout_not_matches: _TriggerRegex | None = None
    stderr_not_matches: _TriggerRegex | None = None


def workspace_automation_trigger_has_rule(trigger: WorkspaceAutomationTrigger) -> bool:
    """Return whether a trigger contains at least one first-version rule."""
    return (
        trigger.exit_code is not None
        or trigger.stdout_matches is not None
        or trigger.stderr_matches is not None
        or trigger.stdout_not_matches is not None
        or trigger.stderr_not_matches is not None
    )


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

    version: _AutomationFileVersion
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
