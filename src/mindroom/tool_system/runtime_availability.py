"""Requester-aware runtime readiness for configured tools."""

from __future__ import annotations

from dataclasses import dataclass
from typing import TYPE_CHECKING

from mindroom.config.models import EffectiveToolConfig
from mindroom.credentials import get_runtime_credentials_manager
from mindroom.tool_system.catalog import (
    TOOL_METADATA,
    ToolConfigurationNotReadyError,
    validate_tool_runtime_configuration,
)
from mindroom.tool_system.dynamic_toolkits import visible_tool_surface
from mindroom.tool_system.worker_routing import build_agent_toolkit_worker_target

if TYPE_CHECKING:
    from collections.abc import Mapping

    from mindroom.config.main import Config
    from mindroom.constants import RuntimePaths
    from mindroom.credentials import CredentialsManager
    from mindroom.runtime_resolution import ResolvedAgentExecution
    from mindroom.tool_system.worker_routing import ResolvedWorkerTarget


@dataclass(frozen=True, slots=True)
class ResolvedRuntimeToolAvailability:
    """Ready tool configs plus configured tools that still need trusted setup."""

    ready_tool_configs: tuple[EffectiveToolConfig, ...]
    setup_required_tool_names: frozenset[str]
    runtime_overrides_by_tool: dict[str, dict[str, object] | None]


def tool_setup_guidance(tool_name: str) -> str:
    """Return registered setup guidance without exposing configuration values."""
    metadata = TOOL_METADATA.get(tool_name)
    if metadata is not None and metadata.helper_text:
        return metadata.helper_text
    return f"Ask the requester or operator to configure tool '{tool_name}' through its trusted setup flow."


def resolve_runtime_tool_availability(
    tool_configs: tuple[EffectiveToolConfig, ...],
    runtime_paths: RuntimePaths,
    *,
    credentials_manager: CredentialsManager,
    worker_target: ResolvedWorkerTarget | None,
    runtime_overrides_by_tool: Mapping[str, dict[str, object] | None],
    allowed_shared_services: frozenset[str] | None,
) -> ResolvedRuntimeToolAvailability:
    """Resolve setup-gated tools before model-visible toolkit construction."""
    ready: list[EffectiveToolConfig] = []
    setup_required: set[str] = set()
    for entry in tool_configs:
        metadata = TOOL_METADATA.get(entry.name)
        if metadata is None or not metadata.runtime_config_required:
            ready.append(entry)
            continue
        try:
            validate_tool_runtime_configuration(
                entry.name,
                runtime_paths,
                credentials_manager=credentials_manager,
                tool_config_overrides=entry.tool_config_overrides,
                runtime_overrides=runtime_overrides_by_tool.get(entry.name),
                allowed_shared_services=allowed_shared_services,
                worker_target=worker_target,
            )
        except ToolConfigurationNotReadyError:
            setup_required.add(entry.name)
            continue
        ready.append(entry)
    return ResolvedRuntimeToolAvailability(
        ready_tool_configs=tuple(ready),
        setup_required_tool_names=frozenset(setup_required),
        runtime_overrides_by_tool=dict(runtime_overrides_by_tool),
    )


def resolve_agent_runtime_tool_availability(
    agent_name: str,
    config: Config,
    runtime_paths: RuntimePaths,
    execution: ResolvedAgentExecution,
    *,
    visible_tool_configs: tuple[EffectiveToolConfig, ...],
    hidden_tool_names: frozenset[str],
    disabled_tool_names: frozenset[str],
    disable_runtime_capabilities: bool,
) -> ResolvedRuntimeToolAvailability:
    """Resolve every configured tool against one agent request identity."""
    if disable_runtime_capabilities:
        return ResolvedRuntimeToolAvailability((), frozenset(), {})
    entity = config.resolve_entity(agent_name)
    all_configured_tools = visible_tool_surface(
        agent_name=agent_name,
        config=config,
        loaded_tools=[entry.name for entry in entity.authored_deferred_tool_configs],
        enable_dynamic_tools_manager=False,
    ).runtime_tool_configs
    candidates = tuple(
        entry
        for entry in all_configured_tools
        if entry.name not in hidden_tool_names and entry.name not in disabled_tool_names
    )
    runtime_overrides_by_tool = {entry.name: entity.tool_runtime_overrides(entry.name) for entry in candidates}
    worker_target = build_agent_toolkit_worker_target(
        execution.execution_scope,
        agent_name,
        is_private=execution.is_private,
        execution_identity=execution.execution_identity,
        runtime_paths=runtime_paths,
    )
    configured_availability = resolve_runtime_tool_availability(
        candidates,
        runtime_paths,
        credentials_manager=get_runtime_credentials_manager(runtime_paths),
        worker_target=worker_target,
        runtime_overrides_by_tool=runtime_overrides_by_tool,
        allowed_shared_services=(
            config.get_worker_grantable_credentials() if execution.execution_scope is not None else None
        ),
    )
    ready_tool_configs = [
        entry
        for entry in visible_tool_configs
        if entry.name not in hidden_tool_names
        and entry.name not in disabled_tool_names
        and entry.name not in configured_availability.setup_required_tool_names
    ]
    if configured_availability.setup_required_tool_names:
        ready_tool_configs.append(
            EffectiveToolConfig(
                name="tool_setup",
                tool_config_overrides={},
                authored_order=len(visible_tool_configs),
                authored_name="tool_setup",
            ),
        )
    return ResolvedRuntimeToolAvailability(
        ready_tool_configs=tuple(ready_tool_configs),
        setup_required_tool_names=configured_availability.setup_required_tool_names,
        runtime_overrides_by_tool=configured_availability.runtime_overrides_by_tool,
    )


__all__ = [
    "ResolvedRuntimeToolAvailability",
    "resolve_agent_runtime_tool_availability",
    "resolve_runtime_tool_availability",
    "tool_setup_guidance",
]
