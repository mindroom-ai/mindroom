"""Loader for workspace-authored automation YAML files."""

from __future__ import annotations

from datetime import UTC, datetime
from pathlib import Path
from typing import TYPE_CHECKING, cast

import yaml
from croniter import CroniterBadCronError, CroniterBadDateError, croniter
from pydantic import ValidationError

from mindroom.workspace_automations.models import (
    LoadedWorkspaceAutomation,
    WorkspaceAutomationAction,
    WorkspaceAutomationDefinition,
    WorkspaceAutomationFile,
    WorkspaceAutomationLoadError,
    WorkspaceAutomationLoadResult,
    WorkspaceAutomationTrigger,
    is_path_safe_automation_id,
)

if TYPE_CHECKING:
    from collections.abc import Sequence

    from mindroom.config.models import WorkspaceAutomationPolicyConfig

AUTOMATIONS_RELATIVE_PATH = Path(".mindroom") / "automations.yaml"
_SCHEDULE_INTERVAL_BASE = datetime(2026, 1, 1, tzinfo=UTC)


def load_workspace_automations(
    *,
    agent_name: str,
    workspace_root: Path,
    agent_rooms: Sequence[str],
    policy: WorkspaceAutomationPolicyConfig,
) -> WorkspaceAutomationLoadResult:
    """Load and validate workspace automations for one agent workspace."""
    file_path = workspace_root / AUTOMATIONS_RELATIVE_PATH
    if not policy.enabled or not file_path.exists():
        return WorkspaceAutomationLoadResult()

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
        if _raw_definition_is_disabled(raw_definition):
            continue

        entry_errors = _validate_automation_id(file_path, automation_id)
        if entry_errors:
            errors.extend(entry_errors)
            continue

        try:
            definition = WorkspaceAutomationDefinition.model_validate(raw_definition)
        except ValidationError as exc:
            errors.extend(_validation_errors(file_path, automation_id, ("automations", automation_id), exc))
            continue

        if not definition.enabled:
            continue

        normalized_action = _normalize_action_room(definition.action, agent_rooms)
        policy_errors = _policy_errors(
            file_path=file_path,
            automation_id=automation_id,
            definition=definition,
            action=normalized_action,
            policy=policy,
            agent_rooms=agent_rooms,
        )
        if policy_errors:
            errors.extend(policy_errors)
            continue

        automations.append(
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
        )

    return WorkspaceAutomationLoadResult(automations=tuple(automations), errors=tuple(errors))


def _load_yaml_file(file_path: Path) -> object | WorkspaceAutomationLoadError:
    try:
        return yaml.safe_load(file_path.read_text(encoding="utf-8"))
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
    if action.type not in {"agent_message", "matrix_message"} or action.room is not None or len(agent_rooms) != 1:
        return action
    return action.model_copy(update={"room": agent_rooms[0]})


def _policy_errors(
    *,
    file_path: Path,
    automation_id: str,
    definition: WorkspaceAutomationDefinition,
    action: WorkspaceAutomationAction,
    policy: WorkspaceAutomationPolicyConfig,
    agent_rooms: Sequence[str],
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

    try:
        interval_seconds = _minimum_schedule_interval_seconds(definition.schedule)
    except (CroniterBadCronError, CroniterBadDateError) as exc:
        errors.append(
            WorkspaceAutomationLoadError(
                file_path=file_path,
                automation_id=automation_id,
                field_path=("automations", automation_id, "schedule"),
                message=f"schedule cannot produce any runs: {exc}",
            ),
        )
        return errors
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
                message="trigger must be present for visible workspace automation actions",
            ),
        )
    elif not _trigger_has_rule(definition.trigger):
        errors.append(
            WorkspaceAutomationLoadError(
                file_path=file_path,
                automation_id=automation_id,
                field_path=("automations", automation_id, "trigger", "exit_code"),
                message="trigger.exit_code must be provided",
            ),
        )

    if action.type in {"agent_message", "matrix_message"} and action.room is None and len(agent_rooms) != 1:
        errors.append(
            WorkspaceAutomationLoadError(
                file_path=file_path,
                automation_id=automation_id,
                field_path=("automations", automation_id, "action", "room"),
                message="action.room is required unless the owning agent has exactly one configured room",
            ),
        )

    return errors


def _trigger_has_rule(trigger: WorkspaceAutomationTrigger) -> bool:
    return trigger.exit_code is not None


def _minimum_schedule_interval_seconds(schedule: str) -> float:
    iterator = croniter(schedule, _SCHEDULE_INTERVAL_BASE)
    first_run = iterator.get_next(datetime)
    second_run = iterator.get_next(datetime)
    return (second_run - first_run).total_seconds()
