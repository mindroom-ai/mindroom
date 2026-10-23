"""Runtime tool surface selection, including which authored deferred tools an agent build attaches."""

from __future__ import annotations

from dataclasses import dataclass
from typing import TYPE_CHECKING

from mindroom.config.models import EffectiveToolConfig
from mindroom.tool_system.catalog import validate_authored_tool_entry_overrides
from mindroom.tool_system.declarations import MATRIX_ROOM_RUNTIME_TOOL_NAMES

if TYPE_CHECKING:
    from collections.abc import Iterable, Mapping

    from mindroom.config.main import Config
    from mindroom.tool_system.agno_toolkits import Toolkit
    from mindroom.tool_system.tool_function import ToolFunction


@dataclass(frozen=True)
class VisibleToolSurface:
    """Runtime tool surface for one agent, deferred toolkits included unless a selection limits them."""

    runtime_tool_configs: tuple[EffectiveToolConfig, ...]


@dataclass(frozen=True)
class ToolDeferral:
    """What tool search shows of one authored deferred tool beside its functions, captured before it is adapted."""

    domain_name: str
    description: str
    # Instructions the toolkit and its functions render when eager, captured before they are suppressed.
    instructions: str | None
    function_instructions: Mapping[str, str]


@dataclass(frozen=True)
class DeferredToolkit:
    """The functions a run offers of one authored deferred tool, which stay off the provider request until searched."""

    deferral: ToolDeferral
    functions: tuple[ToolFunction, ...]

    @property
    def wire_function_names(self) -> tuple[str, ...]:
        """Return the names of the functions a run offers of the tool."""
        return tuple(function.name for function in self.functions)


def _deferred_tool_names(config: Config, agent_name: str) -> list[str]:
    return [entry.name for entry in config.resolve_entity(agent_name).authored_deferred_tool_configs]


def _initial_deferred_tools(config: Config, agent_name: str) -> list[str]:
    return [entry.name for entry in config.resolve_entity(agent_name).authored_deferred_tool_configs if entry.initial]


def _attachable_deferred_tools(config: Config, agent_name: str, requested_tools: list[str]) -> list[str]:
    """Return the requested authored deferred tools this agent's scope supports, in authored order."""
    requested = set(requested_tools)
    entity_view = config.resolve_entity(agent_name)
    return [
        tool_name
        for tool_name in _deferred_tool_names(config, agent_name)
        if tool_name in requested and not entity_view.deferred_tool_scope_incompatible_tools(tool_name)
    ]


def _normalize_effective_tool_config_overrides(
    tool_name: str,
    overrides: dict[str, object],
) -> dict[str, object]:
    return validate_authored_tool_entry_overrides(tool_name, overrides)


def _has_deferred_tools(config: Config, agent_name: str) -> bool:
    """Return whether one agent has at least one authored deferred tool."""
    return bool(config.resolve_entity(agent_name).authored_deferred_tool_configs)


def defer_toolkit(domain_name: str, description: str, toolkit: Toolkit) -> ToolDeferral:
    """Record a wire-deferred toolkit's renderable instructions, then keep them out of the system prompt.

    The system prompt is sent before tool search finds a toolkit, so its instructions travel with search results.
    """
    deferral = ToolDeferral(
        domain_name=domain_name,
        description=description,
        instructions=toolkit.instructions if toolkit.add_instructions else None,
        function_instructions={
            name: function.instructions
            for name, function in toolkit.get_async_functions().items()
            if function.add_instructions and function.instructions
        },
    )
    toolkit.add_instructions = False
    for function in (*toolkit.get_functions().values(), *toolkit.get_async_functions().values()):
        function.add_instructions = False
    return deferral


def _special_tool_names(
    agent_name: str,
    config: Config,
    delegation_depth: int,
    enable_dynamic_tools_manager: bool,
) -> list[str]:
    agent_config = config.get_agent(agent_name)
    tool_names: list[str] = []

    if agent_config.delegate_to:
        from mindroom.delegation.lifecycle import MAX_DELEGATION_DEPTH  # noqa: PLC0415

        if delegation_depth < MAX_DELEGATION_DEPTH:
            tool_names.append("delegate")

    allow_self_config = (
        agent_config.allow_self_config
        if agent_config.allow_self_config is not None
        else config.defaults.allow_self_config
    )
    if allow_self_config:
        tool_names.append("self_config")

    if agent_config.skill_learning.enabled:
        # Like Hermes, an agent that learns skills can also save them in chat, and its skill review can only call
        # tools the agent's own request offered.
        tool_names.append("skill_manage")

    if config.agent_uses_knowledge_graph(agent_name):
        tool_names.append("knowledge_graph")

    if enable_dynamic_tools_manager and _has_deferred_tools(config, agent_name):
        tool_names.append("dynamic_tools")

    return tool_names


def _tool_config_owner_precedence(config: Config, entry: EffectiveToolConfig) -> tuple[int, int, int]:
    directly_authored = entry.name == (entry.authored_name or entry.name)
    authored_from_individual_tool = not config.is_tool_preset(entry.authored_name or entry.name)
    return (int(directly_authored), int(authored_from_individual_tool), -entry.authored_order)


def _expanded_authored_entry_config(entry: EffectiveToolConfig, tool_name: str) -> EffectiveToolConfig:
    return EffectiveToolConfig(
        name=tool_name,
        tool_config_overrides=(dict(entry.tool_config_overrides) if tool_name == entry.name else {}),
        defer=entry.defer,
        initial=entry.initial,
        authored_order=entry.authored_order,
        authored_name=entry.name,
    )


def _visible_authored_tool_configs(
    config: Config,
    agent_name: str,
    *,
    attached_deferred_tools: set[str],
) -> list[EffectiveToolConfig]:
    visible_by_name: dict[str, EffectiveToolConfig] = {}
    hidden_deferred_by_name: dict[str, EffectiveToolConfig] = {}

    for authored_entry in config.resolve_entity(agent_name).authored_tool_configs:
        is_visible_owner = not authored_entry.defer or authored_entry.name in attached_deferred_tools
        for tool_name in config.expand_tool_names([authored_entry.name]):
            expanded = _expanded_authored_entry_config(authored_entry, tool_name)
            expanded_precedence = _tool_config_owner_precedence(config, expanded)
            if is_visible_owner:
                current = visible_by_name.get(tool_name)
                if current is None or expanded_precedence > _tool_config_owner_precedence(config, current):
                    visible_by_name[tool_name] = expanded
                continue
            current_hidden = hidden_deferred_by_name.get(tool_name)
            if current_hidden is None or expanded_precedence > _tool_config_owner_precedence(config, current_hidden):
                hidden_deferred_by_name[tool_name] = expanded

    resolved: list[EffectiveToolConfig] = []
    for tool_name in config.resolve_entity(agent_name).available_tools:
        visible = visible_by_name.get(tool_name)
        if visible is None:
            continue
        hidden = hidden_deferred_by_name.get(tool_name)
        if (
            hidden is not None
            and not visible.defer
            and _tool_config_owner_precedence(config, hidden) > _tool_config_owner_precedence(config, visible)
        ):
            continue
        resolved.append(visible)
    return resolved


def _append_injected_special_tool_configs(
    resolved_tool_configs: list[EffectiveToolConfig],
    *,
    agent_name: str,
    config: Config,
    delegation_depth: int,
    enable_dynamic_tools_manager: bool,
    include_matrix_room_runtime_tools: bool,
) -> list[EffectiveToolConfig]:
    matrix_room_runtime_tool_names = set(MATRIX_ROOM_RUNTIME_TOOL_NAMES)
    if include_matrix_room_runtime_tools:
        resolved_tool_configs = [
            entry for entry in resolved_tool_configs if entry.name not in matrix_room_runtime_tool_names
        ]
    tool_names = [entry.name for entry in resolved_tool_configs]
    injected_tool_names = _special_tool_names(
        agent_name=agent_name,
        config=config,
        delegation_depth=delegation_depth,
        enable_dynamic_tools_manager=enable_dynamic_tools_manager,
    )
    if include_matrix_room_runtime_tools:
        injected_tool_names.extend(MATRIX_ROOM_RUNTIME_TOOL_NAMES)
    for tool_name in injected_tool_names:
        if tool_name in tool_names:
            continue
        if (
            tool_name not in matrix_room_runtime_tool_names
            and config.resolve_entity(agent_name).authored_deferred_tool_config(tool_name) is not None
        ):
            continue
        resolved_tool_configs.append(
            EffectiveToolConfig(
                name=tool_name,
                tool_config_overrides={},
                authored_order=len(resolved_tool_configs),
                authored_name=tool_name,
            ),
        )
        tool_names.append(tool_name)
    return resolved_tool_configs


def visible_tool_surface(
    *,
    agent_name: str,
    config: Config,
    deferred_tools: Iterable[str] | None = None,
    delegation_depth: int = 0,
    enable_dynamic_tools_manager: bool = False,
    include_matrix_room_runtime_tools: bool = False,
) -> VisibleToolSurface:
    """Return the canonical runtime tool surface for one agent.

    ``deferred_tools`` limits which authored deferred tools are attached, and ``None`` attaches all of them.
    Deferred tools marked ``initial`` are always attached.
    """
    requested_tools = _deferred_tool_names(config, agent_name) if deferred_tools is None else list(deferred_tools)
    attached_deferred_tools = _attachable_deferred_tools(
        config,
        agent_name,
        [*_initial_deferred_tools(config, agent_name), *requested_tools],
    )
    runtime_tool_configs = [
        EffectiveToolConfig(
            name=entry.name,
            tool_config_overrides=_normalize_effective_tool_config_overrides(
                entry.name,
                dict(entry.tool_config_overrides),
            ),
            defer=entry.defer,
            initial=entry.initial,
            authored_order=entry.authored_order,
            authored_name=entry.authored_name,
        )
        for entry in _visible_authored_tool_configs(
            config,
            agent_name,
            attached_deferred_tools=set(attached_deferred_tools),
        )
    ]

    runtime_tool_configs = _append_injected_special_tool_configs(
        runtime_tool_configs,
        agent_name=agent_name,
        config=config,
        delegation_depth=delegation_depth,
        enable_dynamic_tools_manager=enable_dynamic_tools_manager,
        include_matrix_room_runtime_tools=include_matrix_room_runtime_tools,
    )
    return VisibleToolSurface(runtime_tool_configs=tuple(runtime_tool_configs))
