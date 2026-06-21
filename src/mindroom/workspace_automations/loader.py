"""Loader for workspace-authored automation YAML files."""

from __future__ import annotations

import stat
from datetime import UTC, datetime
from pathlib import Path
from typing import TYPE_CHECKING, cast

import yaml
from croniter import croniter
from pydantic import ValidationError

from mindroom.workspace_automations.models import (
    LoadedWorkspaceAutomation,
    WorkspaceAutomationAction,
    WorkspaceAutomationDefinition,
    WorkspaceAutomationFile,
    WorkspaceAutomationLoadError,
    WorkspaceAutomationLoadResult,
    is_path_safe_automation_id,
    workspace_automation_trigger_has_rule,
)
from mindroom.workspace_automations.targets import resolve_action_room
from mindroom.workspaces import resolve_relative_path_within_root

if TYPE_CHECKING:
    from collections.abc import Sequence

    from mindroom.config.models import WorkspaceAutomationPolicyConfig

AUTOMATIONS_RELATIVE_PATH = Path(".mindroom") / "automations.yaml"
_MAX_AUTOMATIONS_FILE_BYTES = 256 * 1024
_SCHEDULE_INTERVAL_BASE = datetime(2026, 1, 1, tzinfo=UTC)
# Five-field cron has no year column, but month/day gaps repeat on the Gregorian
# 400-year leap cycle. Dense schedules hit the max-run cap after seeing short gaps.
_SCHEDULE_SAMPLE_SPAN_SECONDS = 401 * 366 * 24 * 60 * 60
# Safety cap so dense schedules (e.g. every minute) cannot iterate unboundedly.
_SCHEDULE_SAMPLE_MAX_RUNS = 10_000


def load_workspace_automations(
    *,
    agent_name: str,
    workspace_root: Path,
    agent_rooms: Sequence[str],
    policy: WorkspaceAutomationPolicyConfig,
) -> WorkspaceAutomationLoadResult:
    """Load and validate workspace automations for one agent workspace."""
    if not policy.enabled:
        return WorkspaceAutomationLoadResult()

    resolved_file = _resolve_automation_file(workspace_root)
    if resolved_file is None:
        return WorkspaceAutomationLoadResult()
    if isinstance(resolved_file, WorkspaceAutomationLoadError):
        return WorkspaceAutomationLoadResult(errors=(resolved_file,))
    file_path = resolved_file

    loaded_yaml = _load_yaml_file(file_path)
    if isinstance(loaded_yaml, WorkspaceAutomationLoadError):
        return WorkspaceAutomationLoadResult(errors=(loaded_yaml,))

    try:
        automation_file = WorkspaceAutomationFile.model_validate(loaded_yaml)
    except ValidationError as exc:
        return WorkspaceAutomationLoadResult(errors=tuple(_validation_errors(file_path, None, (), exc)))

    automations: list[LoadedWorkspaceAutomation] = []
    errors: list[WorkspaceAutomationLoadError] = []
    for automation_id, raw_definition in automation_file.automations.items():
        loaded_automation, entry_errors = _load_automation_entry(
            agent_name=agent_name,
            workspace_root=workspace_root,
            file_path=file_path,
            automation_id=automation_id,
            raw_definition=raw_definition,
            agent_rooms=agent_rooms,
            policy=policy,
        )
        errors.extend(entry_errors)
        if loaded_automation is not None:
            automations.append(loaded_automation)

    return WorkspaceAutomationLoadResult(automations=tuple(automations), errors=tuple(errors))


def _load_automation_entry(
    *,
    agent_name: str,
    workspace_root: Path,
    file_path: Path,
    automation_id: str,
    raw_definition: object,
    agent_rooms: Sequence[str],
    policy: WorkspaceAutomationPolicyConfig,
) -> tuple[LoadedWorkspaceAutomation | None, list[WorkspaceAutomationLoadError]]:
    if _raw_definition_is_disabled(raw_definition):
        return None, []

    entry_errors = _validate_automation_id(file_path, automation_id)
    if entry_errors:
        return None, entry_errors

    try:
        definition = WorkspaceAutomationDefinition.model_validate(raw_definition)
    except ValidationError as exc:
        return None, _validation_errors(file_path, automation_id, ("automations", automation_id), exc)

    if not definition.enabled:
        return None, []

    normalized_action = _normalize_action_room(definition.action, agent_rooms)
    policy_errors = _policy_errors(
        file_path=file_path,
        automation_id=automation_id,
        definition=definition,
        action=normalized_action,
        agent_rooms=agent_rooms,
        policy=policy,
    )
    if policy_errors:
        return None, policy_errors

    return (
        LoadedWorkspaceAutomation(
            agent_name=agent_name,
            automation_id=automation_id,
            workspace_root=workspace_root,
            file_path=file_path,
            schedule=definition.schedule,
            check=definition.check,
            trigger=definition.trigger,
            action=normalized_action,
        ),
        [],
    )


def _resolve_automation_file(workspace_root: Path) -> Path | WorkspaceAutomationLoadError | None:
    file_path = workspace_root / AUTOMATIONS_RELATIVE_PATH
    try:
        resolved_file = resolve_relative_path_within_root(
            workspace_root,
            AUTOMATIONS_RELATIVE_PATH,
            field_name="Workspace automation file",
            root_label="workspace root",
        )
    except ValueError as exc:
        return WorkspaceAutomationLoadError(
            file_path=file_path,
            automation_id=None,
            field_path=(),
            message=str(exc),
        )
    if not resolved_file.exists():
        return None
    return resolved_file


def _load_yaml_file(file_path: Path) -> object | WorkspaceAutomationLoadError:
    try:
        file_stat = file_path.stat(follow_symlinks=False)
        if not stat.S_ISREG(file_stat.st_mode):
            return WorkspaceAutomationLoadError(
                file_path=file_path,
                automation_id=None,
                field_path=(),
                message="Automation YAML must be a regular file",
            )
        if file_stat.st_size > _MAX_AUTOMATIONS_FILE_BYTES:
            return WorkspaceAutomationLoadError(
                file_path=file_path,
                automation_id=None,
                field_path=(),
                message=f"Automation YAML must not exceed {_MAX_AUTOMATIONS_FILE_BYTES} bytes",
            )
        raw_content = file_path.read_bytes()
        if len(raw_content) > _MAX_AUTOMATIONS_FILE_BYTES:
            return WorkspaceAutomationLoadError(
                file_path=file_path,
                automation_id=None,
                field_path=(),
                message=f"Automation YAML must not exceed {_MAX_AUTOMATIONS_FILE_BYTES} bytes",
            )
        return yaml.safe_load(raw_content.decode("utf-8"))
    except yaml.YAMLError as exc:
        return WorkspaceAutomationLoadError(
            file_path=file_path,
            automation_id=None,
            field_path=(),
            message=f"Could not parse automation YAML: {exc}",
        )
    except (OSError, UnicodeError) as exc:
        return WorkspaceAutomationLoadError(
            file_path=file_path,
            automation_id=None,
            field_path=(),
            message=f"Could not read automation YAML: {exc}",
        )


def _validation_errors(
    file_path: Path,
    automation_id: str | None,
    prefix: tuple[str | int, ...],
    exc: ValidationError,
) -> list[WorkspaceAutomationLoadError]:
    errors: list[WorkspaceAutomationLoadError] = []
    for error in exc.errors(include_context=False):
        field_path = prefix + tuple(error["loc"])
        errors.append(
            WorkspaceAutomationLoadError(
                file_path=file_path,
                automation_id=automation_id,
                field_path=field_path,
                message=_format_validation_message(field_path, str(error["msg"])),
            ),
        )
    return errors


def _format_validation_message(field_path: tuple[str | int, ...], message: str) -> str:
    if not field_path:
        return message
    return f"{'.'.join(str(part) for part in field_path)}: {message}"


def _validate_automation_id(file_path: Path, automation_id: str) -> list[WorkspaceAutomationLoadError]:
    if is_path_safe_automation_id(automation_id):
        return []
    return [
        WorkspaceAutomationLoadError(
            file_path=file_path,
            automation_id=automation_id,
            field_path=("automations", automation_id),
            message="Automation ID must be a single path-safe identifier matching ^[A-Za-z0-9_.-]+$",
        ),
    ]


def _raw_definition_is_disabled(raw_definition: object) -> bool:
    if not isinstance(raw_definition, dict):
        return False
    definition = cast("dict[str, object]", raw_definition)
    return definition.get("enabled") is False


def _normalize_action_room(
    action: WorkspaceAutomationAction,
    agent_rooms: Sequence[str],
) -> WorkspaceAutomationAction:
    if action.type not in {"agent_message", "matrix_message", "hook"}:
        return action
    room = resolve_action_room(action_room=action.room, agent_configured_rooms=agent_rooms)
    if room == action.room:
        return action
    return action.model_copy(update={"room": room})


def _policy_errors(
    *,
    file_path: Path,
    automation_id: str,
    definition: WorkspaceAutomationDefinition,
    action: WorkspaceAutomationAction,
    agent_rooms: Sequence[str],
    policy: WorkspaceAutomationPolicyConfig,
) -> list[WorkspaceAutomationLoadError]:
    errors: list[WorkspaceAutomationLoadError] = []

    if definition.check.timeout_seconds > policy.max_timeout_seconds:
        errors.append(
            WorkspaceAutomationLoadError(
                file_path=file_path,
                automation_id=automation_id,
                field_path=("automations", automation_id, "check", "timeout_seconds"),
                message=f"check.timeout_seconds must not exceed policy max_timeout_seconds {policy.max_timeout_seconds}",
            ),
        )

    # Schedule validity is already enforced at model validation (models._validate_schedule),
    # so the schedule is guaranteed to produce runs here.
    interval_seconds = _minimum_schedule_interval_seconds(definition.schedule)
    if interval_seconds < policy.min_interval_seconds:
        errors.append(
            WorkspaceAutomationLoadError(
                file_path=file_path,
                automation_id=automation_id,
                field_path=("automations", automation_id, "schedule"),
                message=(
                    "schedule interval must not be shorter than policy "
                    f"min_interval_seconds {policy.min_interval_seconds}"
                ),
            ),
        )

    if action.type == "none":
        return errors

    if action.type not in policy.allowed_actions:
        errors.append(
            WorkspaceAutomationLoadError(
                file_path=file_path,
                automation_id=automation_id,
                field_path=("automations", automation_id, "action", "type"),
                message=f"action.type '{action.type}' is not allowed by workspace automation policy",
            ),
        )

    if definition.trigger is None:
        errors.append(
            WorkspaceAutomationLoadError(
                file_path=file_path,
                automation_id=automation_id,
                field_path=("automations", automation_id, "trigger"),
                message="trigger must be present for non-none workspace automation actions",
            ),
        )
    elif not workspace_automation_trigger_has_rule(definition.trigger):
        errors.append(
            WorkspaceAutomationLoadError(
                file_path=file_path,
                automation_id=automation_id,
                field_path=("automations", automation_id, "trigger"),
                message="trigger must include at least one rule",
            ),
        )

    errors.extend(
        _action_room_errors(
            file_path=file_path,
            automation_id=automation_id,
            action=action,
            agent_rooms=agent_rooms,
        ),
    )
    errors.extend(
        _visible_action_errors(
            file_path=file_path,
            automation_id=automation_id,
            action=action,
        ),
    )

    return errors


def _action_room_errors(
    *,
    file_path: Path,
    automation_id: str,
    action: WorkspaceAutomationAction,
    agent_rooms: Sequence[str],
) -> list[WorkspaceAutomationLoadError]:
    if action.type not in {"agent_message", "matrix_message", "hook"}:
        return []

    errors: list[WorkspaceAutomationLoadError] = []
    if action.room is None:
        if action.type == "hook":
            return errors
        errors.append(
            WorkspaceAutomationLoadError(
                file_path=file_path,
                automation_id=automation_id,
                field_path=("automations", automation_id, "action", "room"),
                message="action.room is required unless the owning agent has exactly one configured room",
            ),
        )
    elif action.room not in agent_rooms:
        errors.append(
            WorkspaceAutomationLoadError(
                file_path=file_path,
                automation_id=automation_id,
                field_path=("automations", automation_id, "action", "room"),
                message="action.room must be one of the owning agent's configured rooms",
            ),
        )
    return errors


def _visible_action_errors(
    *,
    file_path: Path,
    automation_id: str,
    action: WorkspaceAutomationAction,
) -> list[WorkspaceAutomationLoadError]:
    if action.type not in {"agent_message", "matrix_message"}:
        return []
    if action.message is not None:
        return []
    return [
        WorkspaceAutomationLoadError(
            file_path=file_path,
            automation_id=automation_id,
            field_path=("automations", automation_id, "action", "message"),
            message="action.message is required for visible workspace automation actions",
        ),
    ]


def _minimum_schedule_interval_seconds(schedule: str) -> float:
    """Return the smallest gap between consecutive runs across a full schedule period.

    Sampling only the first gap is unsafe: irregular schedules (e.g. ``0 0 1,2 * *``)
    can have a large first gap but a much smaller minimum gap elsewhere. Sample until
    sparse schedules span the Gregorian leap cycle; dense schedules hit the max-run cap
    only after many repeated short gaps have already been observed.
    """
    iterator = croniter(schedule, _SCHEDULE_INTERVAL_BASE)
    previous_run = iterator.get_next(datetime)
    minimum_seconds = float("inf")
    for _ in range(_SCHEDULE_SAMPLE_MAX_RUNS):
        next_run = iterator.get_next(datetime)
        minimum_seconds = min(minimum_seconds, (next_run - previous_run).total_seconds())
        if (next_run - _SCHEDULE_INTERVAL_BASE).total_seconds() >= _SCHEDULE_SAMPLE_SPAN_SECONDS:
            break
        previous_run = next_run
    return minimum_seconds
