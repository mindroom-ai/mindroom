"""Workspace automation target resolution."""

from __future__ import annotations

import logging
from dataclasses import dataclass
from typing import TYPE_CHECKING

from mindroom.runtime_resolution import resolve_agent_runtime
from mindroom.workspace_instances import load_workspace_instance_records

if TYPE_CHECKING:
    from collections.abc import Sequence
    from pathlib import Path

    from mindroom.config.main import Config
    from mindroom.config.models import WorkspaceAutomationPolicyConfig
    from mindroom.constants import RuntimePaths
    from mindroom.runtime_resolution import ResolvedAgentRuntime

_LOGGER = logging.getLogger(__name__)
_REQUESTER_SCOPED_SKIP_REASON = "requester-scoped workspace automations require a live requester identity"


@dataclass(frozen=True)
class WorkspaceAutomationTarget:
    """Resolved runtime target for one concrete workspace automation instance."""

    agent_name: str
    agent_configured_rooms: tuple[str, ...]
    policy: WorkspaceAutomationPolicyConfig
    agent_runtime: ResolvedAgentRuntime
    workspace_root: Path


def iter_workspace_automation_targets(
    config: Config,
    runtime_paths: RuntimePaths,
) -> list[WorkspaceAutomationTarget]:
    """Return concrete workspace instances with enabled automations and resolved runtimes."""
    targets: list[WorkspaceAutomationTarget] = []
    targets.extend(_iter_shared_workspace_automation_targets(config, runtime_paths))
    targets.extend(_iter_private_workspace_automation_targets(config, runtime_paths))
    return targets


def _iter_shared_workspace_automation_targets(
    config: Config,
    runtime_paths: RuntimePaths,
) -> list[WorkspaceAutomationTarget]:
    """Return config-driven shared agents with enabled automations and a resolved workspace."""
    targets: list[WorkspaceAutomationTarget] = []
    for agent_name, agent_config in config.agents.items():
        policy = config.get_agent_workspace_automation_policy(agent_name)
        if not policy.enabled:
            _LOGGER.debug(
                "Skipping workspace automation target for agent '%s': workspace automations are disabled.",
                agent_name,
            )
            continue

        if agent_config.private is not None:
            continue

        agent_runtime = resolve_agent_runtime(
            agent_name,
            config,
            runtime_paths,
            execution_identity=None,
            create=True,
        )
        if agent_runtime.workspace is None:
            _LOGGER.info(
                "Skipping workspace automation target for agent '%s': no usable workspace resolved.",
                agent_name,
            )
            continue
        if agent_runtime.execution_scope in {"user", "user_agent"}:
            _LOGGER.info(
                "Skipping workspace automation target for agent '%s': worker_scope=%s; %s.",
                agent_name,
                agent_runtime.execution_scope,
                _REQUESTER_SCOPED_SKIP_REASON,
            )
            continue

        targets.append(
            WorkspaceAutomationTarget(
                agent_name=agent_name,
                agent_configured_rooms=tuple(agent_config.rooms),
                policy=policy,
                agent_runtime=agent_runtime,
                workspace_root=agent_runtime.workspace.root,
            ),
        )

    return targets


def _iter_private_workspace_automation_targets(
    config: Config,
    runtime_paths: RuntimePaths,
) -> list[WorkspaceAutomationTarget]:
    """Return registry-driven private workspace instances with enabled automations."""
    targets: list[WorkspaceAutomationTarget] = []
    for record in load_workspace_instance_records(runtime_paths):
        if record.is_private is not True:
            continue

        agent_config = config.agents.get(record.agent_name)
        if agent_config is None:
            _LOGGER.info(
                "Skipping private workspace automation target for agent '%s': agent no longer exists.",
                record.agent_name,
            )
            continue
        if agent_config.private is None:
            _LOGGER.info(
                "Skipping private workspace automation target for agent '%s': agent is no longer private.",
                record.agent_name,
            )
            continue

        policy = config.get_agent_workspace_automation_policy(record.agent_name)
        if not policy.enabled:
            _LOGGER.debug(
                "Skipping private workspace automation target for agent '%s': workspace automations are disabled.",
                record.agent_name,
            )
            continue
        if record.execution_identity is None:
            _LOGGER.info(
                "Skipping private workspace automation target for agent '%s': missing execution identity.",
                record.agent_name,
            )
            continue

        agent_runtime = resolve_agent_runtime(
            record.agent_name,
            config,
            runtime_paths,
            execution_identity=record.execution_identity,
            create=False,
        )
        if agent_runtime.workspace is None:
            _LOGGER.info(
                "Skipping private workspace automation target for agent '%s': no usable workspace resolved.",
                record.agent_name,
            )
            continue
        if not record.workspace_root.is_dir():
            _LOGGER.info(
                "Skipping private workspace automation target for agent '%s': registered workspace does not exist.",
                record.agent_name,
            )
            continue
        if not _paths_match(agent_runtime.workspace.root, record.workspace_root):
            _LOGGER.info(
                "Skipping private workspace automation target for agent '%s': registered workspace root is stale.",
                record.agent_name,
            )
            continue

        targets.append(
            WorkspaceAutomationTarget(
                agent_name=record.agent_name,
                agent_configured_rooms=tuple(agent_config.rooms),
                policy=policy,
                agent_runtime=agent_runtime,
                workspace_root=agent_runtime.workspace.root,
            ),
        )

    return targets


def _paths_match(left: Path, right: Path) -> bool:
    return left.expanduser().resolve() == right.expanduser().resolve()


def resolve_action_room(
    *,
    action_room: str | None,
    agent_configured_rooms: Sequence[str],
) -> str | None:
    """Resolve an authored action room without Matrix alias lookups."""
    if action_room is not None:
        return action_room
    if len(agent_configured_rooms) == 1:
        return agent_configured_rooms[0]
    return None


__all__ = [
    "WorkspaceAutomationTarget",
    "iter_workspace_automation_targets",
    "resolve_action_room",
]
