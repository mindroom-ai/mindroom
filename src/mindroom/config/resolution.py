"""What one agent, team, or the defaults scope gets from the config, resolved once with every inherited value filled in.

Each per-entity fact has one function here, in dependency order, so the config validators can call the ones they need;
``resolve_scope`` and ``resolve_agent`` gather them into one frozen record. Hot-reload replaces the
``Config``, so a record never outlives the operation that resolved it, and nothing caches one.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import TYPE_CHECKING, Literal

from mindroom.agent_policy import PolicyInputs, ResolvedAgentPolicy, resolve_agent_policy
from mindroom.config.automations import Automation, ExplorationAutomation
from mindroom.config.models import CompactionConfig, CompactionOverrideConfig, EffectiveToolConfig
from mindroom.constants import ROUTER_AGENT_NAME
from mindroom.history.types import HistoryPolicy, ResolvedHistorySettings
from mindroom.tool_system.worker_routing import unsupported_shared_only_integration_names

if TYPE_CHECKING:
    from collections.abc import Iterable, Mapping

    from mindroom.config.agent import AgentConfig, TeamConfig
    from mindroom.config.main import Config
    from mindroom.config.memory import MemoryBackend, MemorySearchConfig
    from mindroom.config.models import FileAccess

# A deployment's approved-egress switch gives every agent that takes the default tools this tool.
_APPROVED_EGRESS_TOOL_NAME = "approved_egress"


@dataclass(frozen=True)
class _AuthoredOptionalModel:
    """Static authored semantics for an optional model override field."""

    kind: Literal["unset", "clear", "value"]
    value: str | None = None


def _authored_optional_model(model_name: str | None, *, field_is_set: bool) -> _AuthoredOptionalModel:
    if not field_is_set:
        return _AuthoredOptionalModel(kind="unset")
    if model_name is None:
        return _AuthoredOptionalModel(kind="clear")
    return _AuthoredOptionalModel(kind="value", value=model_name)


def authored_compaction_model_fields(
    compaction: CompactionConfig | CompactionOverrideConfig,
) -> dict[str, _AuthoredOptionalModel]:
    """Return authored tri-state semantics for every optional compaction model field."""
    return {
        "model": _authored_optional_model(compaction.model, field_is_set="model" in compaction.model_fields_set),
        "fallback_model": _authored_optional_model(
            compaction.fallback_model,
            field_is_set="fallback_model" in compaction.model_fields_set,
        ),
    }


def _compaction_enabled(override: CompactionOverrideConfig, *, defaults_enabled: bool) -> bool:
    """Return whether an authored override block turns text compaction on.

    An authored ``enabled`` decides; an override that sets nothing or only clears model fields keeps the defaults'
    choice, and any other override turns compaction on.
    """
    if "enabled" in override.model_fields_set:
        return override.enabled is True
    cleared = {
        name for name, authored in authored_compaction_model_fields(override).items() if authored.kind == "clear"
    }
    return defaults_enabled if override.model_fields_set <= cleared else True


def _history_policy(num_history_runs: int | None, num_history_messages: int | None) -> HistoryPolicy:
    if num_history_messages is not None:
        return HistoryPolicy(mode="messages", limit=num_history_messages)
    if num_history_runs is not None:
        return HistoryPolicy(mode="runs", limit=num_history_runs)
    return HistoryPolicy(mode="all")


def configured_entity(config: Config, name: str) -> AgentConfig | TeamConfig:
    """Return the authored config of one configured agent or team."""
    if name in config.agents:
        return config.agents[name]
    if name in config.teams:
        return config.teams[name]
    msg = f"Unknown entity: {name}"
    raise ValueError(msg)


def _compaction_config(
    defaults: CompactionConfig | None,
    override: CompactionOverrideConfig | None,
) -> CompactionConfig:
    """Return the effective text compaction config: the defaults with an agent's or team's authored override applied."""
    merged = defaults.model_dump() if defaults is not None else {}
    if override is not None:
        authored = override.model_dump(exclude_unset=True)
        for field_name, value in authored.items():
            if value is None:
                merged.pop(field_name, None)
            else:
                merged[field_name] = value
        if authored.get("threshold_tokens") is not None:
            merged.pop("threshold_percent", None)
        if authored.get("threshold_percent") is not None:
            merged.pop("threshold_tokens", None)
        merged["enabled"] = _compaction_enabled(override, defaults_enabled=defaults is not None and defaults.enabled)
    return CompactionConfig.model_validate(merged)


def _history_settings(config: Config, entity: AgentConfig | TeamConfig | None) -> ResolvedHistorySettings:
    defaults = config.defaults
    runs, messages = (entity.num_history_runs, entity.num_history_messages) if entity is not None else (None, None)
    if runs is None and messages is None:
        runs, messages = defaults.num_history_runs, defaults.num_history_messages
    own_limit = entity.max_tool_calls_from_history if entity is not None else None
    compaction = entity.compaction if entity is not None else None
    return ResolvedHistorySettings(
        policy=_history_policy(runs, messages),
        max_tool_calls_from_history=own_limit if own_limit is not None else defaults.max_tool_calls_from_history,
        compaction_config=_compaction_config(defaults.compaction, compaction),
        has_authored_compaction_config=defaults.compaction is not None or compaction is not None,
    )


def memory_backend(config: Config, name: str | None) -> MemoryBackend:
    """Return an agent's effective memory backend; every other scope uses the global one."""
    agent = config.agents.get(name) if name is not None else None
    if agent is not None and agent.memory_backend is not None:
        return agent.memory_backend
    return config.memory.backend


def entity_timezone(config: Config, name: str | None) -> str:
    """Return an agent's own timezone; every other scope, and a name no agent has, uses the global one."""
    agent = config.agents.get(name) if name is not None else None
    return agent.timezone if agent is not None and agent.timezone is not None else config.timezone


def show_tool_calls(config: Config, name: str | None) -> bool:
    """Return whether an agent shows its tool calls; every other scope, and a name no agent has, uses the default."""
    agent = config.agents.get(name) if name is not None else None
    if agent is not None and agent.show_tool_calls is not None:
        return agent.show_tool_calls
    return config.defaults.show_tool_calls


def show_reasoning(config: Config, name: str | None) -> bool:
    """Return whether an agent's streamed replies show its model's reasoning; a name no agent has uses the default."""
    agent = config.agents.get(name) if name is not None else None
    if agent is not None and agent.show_reasoning is not None:
        return agent.show_reasoning
    return config.defaults.show_reasoning


def file_access(config: Config, name: str | None) -> FileAccess:
    """Return an agent's effective file_access; every other scope, and a name no agent has, uses the default."""
    agent = config.agents.get(name) if name is not None else None
    return agent.file_access if agent is not None and agent.file_access is not None else config.defaults.file_access


def memory_search(config: Config, name: str | None) -> MemorySearchConfig:
    """Return an agent's effective file-memory search settings; every other scope uses the global ones."""
    agent = config.agents.get(name) if name is not None else None
    override = agent.memory_search if agent is not None else None
    # exclude_none keeps the "None inherits" tri-state; the deep copy keeps the record from aliasing memory.search.
    update = override.model_dump(exclude_none=True) if override is not None else {}
    return config.memory.search.model_copy(update=update, deep=True)


def uses_knowledge_graph(config: Config, agent_name: str) -> bool:
    """Return whether an agent builds and reads a knowledge graph."""
    agent = config.get_agent(agent_name)
    return agent.knowledge_graph if agent.knowledge_graph is not None else config.defaults.knowledge_graph


def edits_knowledge_graph(config: Config, agent_name: str) -> bool:
    """Return whether an agent may delete facts and entities from its graph, merge entities, and rewrite summaries."""
    agent = config.get_agent(agent_name)
    edits = (
        agent.knowledge_graph_edits
        if agent.knowledge_graph_edits is not None
        else config.defaults.knowledge_graph_edits
    )
    return edits and uses_knowledge_graph(config, agent_name)


def automation_block_reason(config: Config, agent_name: str, automation: Automation) -> str | None:
    """Return why an agent cannot run ``automation``, or None when it can.

    A private agent runs each automation for each requester in that requester's personal room, so it must own
    personal rooms and cannot post in a configured room, which would show one requester's files to everyone in it.
    `prompt_curation` and `dreaming` maintain file memory, which needs the file backend; `exploration` needs the
    knowledge graph, and a room for a shared agent.
    A plugin automation's own needs are checked when it fires, once its plugin is loaded.
    """
    private = config.get_agent(agent_name).private is not None
    if private:
        if config.personal_rooms is None or config.personal_rooms.agent != agent_name:
            return "is private; its automations post in personal rooms, so personal_rooms.agent must name it"
        if automation.room is not None:
            return f"is private; {automation.name} posts in each requester's personal room and cannot set room"
    if automation.requires_file_memory and memory_backend(config, agent_name) != "file":
        return f"needs memory_backend: file for {automation.name}"
    if isinstance(automation, ExplorationAutomation):
        if not uses_knowledge_graph(config, agent_name):
            return "needs knowledge_graph: true for exploration"
        # Its prompt lists what every requester's conversations looked up, so the operator picks who sees it.
        if automation.room is None and not private:
            return "needs room for exploration, because its prompt lists what conversations looked up"
    return None


def agent_automations(config: Config, agent_name: str) -> tuple[Automation, ...]:
    """Return an agent's own automations, else the default automations nothing blocks for it."""
    agent = config.get_agent(agent_name)
    if agent.automations is not None:
        return tuple(agent.automations)
    return tuple(
        entry for entry in config.defaults.automations if automation_block_reason(config, agent_name, entry) is None
    )


def cheap_model(config: Config, agent_name: str) -> str | None:
    """Return the agent's cheap model; an inherited default the agent already runs is no cheaper choice."""
    agent = config.get_agent(agent_name)
    if agent.cheap_model is not None:
        return agent.cheap_model
    default = config.defaults.cheap_model
    return None if default == agent.model else default


def agent_policy(config: Config, agent_name: str) -> ResolvedAgentPolicy:
    """Return one agent's execution policy: its worker scope, scope label, and private storage and knowledge."""
    return resolve_agent_policy(
        agent_name,
        PolicyInputs.from_agent(config.get_agent(agent_name)),
        default_worker_scope=config.defaults.worker_scope,
    )


def authored_tool_entries(config: Config, agent_name: str) -> tuple[EffectiveToolConfig, ...]:
    """Return an agent's tool entries in authored order: its own, then the defaults it takes, settings merged.

    These are what the config asks for; which of them a runtime can build is the tool registry's answer
    (`AgentTools.tool_names`). The approved-egress switch adds its tool to the defaults of every agent that takes them.
    """
    # why-lazy: module-top catalog import pulls runtime tool registry paths and loads agents+tools at config import.
    from mindroom.tool_system.catalog import apply_authored_overrides  # noqa: PLC0415

    agent = config.get_agent(agent_name)
    defaults = {entry.name: entry for entry in config.defaults.tools} if agent.include_default_tools else {}
    named = {entry.name for entry in agent.tools}
    entries: list[tuple[str, dict[str, object], bool, bool]] = [
        (
            entry.name,
            apply_authored_overrides(
                apply_authored_overrides({}, defaults[entry.name].overrides) if entry.name in defaults else {},
                entry.overrides,
            ),
            entry.defer,
            entry.initial,
        )
        for entry in agent.tools
    ]
    entries.extend(
        (entry.name, apply_authored_overrides({}, entry.overrides), False, False)
        for entry in defaults.values()
        if entry.name not in named
    )
    if (
        agent.include_default_tools
        and config.approved_egress_enabled
        and _APPROVED_EGRESS_TOOL_NAME not in named | set(defaults)
    ):
        entries.append((_APPROVED_EGRESS_TOOL_NAME, {}, False, False))
    return tuple(
        EffectiveToolConfig(
            name=name,
            tool_config_overrides=overrides,
            defer=defer,
            initial=initial,
            authored_order=order,
            authored_name=name,
        )
        for order, (name, overrides, defer, initial) in enumerate(entries)
    )


@dataclass(frozen=True)
class AgentToolFacts:
    """What one agent's config decides about its toolkits, before a turn or the runtime decides the rest.

    ``authored`` are its tool entries (see `authored_tool_entries`). ``injected`` are toolkits its settings add without
    an entry: delegation, self-config, agent management, skill saving, contacts, and its graph. ``disabled`` are
    toolkits its settings turn off even when an entry names them. ``tool_names`` are every toolkit its entries name, presets and
    implied toolkits expanded, less the disabled ones; ``startup_tool_names`` the eager and initial ones among them.
    ``scope_incompatible`` maps a deferred entry to the expanded toolkits its execution scope cannot run.
    ``worker_tools`` is the resolved worker tool list, or None to follow the runtime's sandbox settings.
    """

    agent_name: str
    authored: tuple[EffectiveToolConfig, ...]
    injected: tuple[str, ...]
    disabled: frozenset[str]
    tool_names: tuple[str, ...]
    startup_tool_names: frozenset[str]
    scope_incompatible: Mapping[str, tuple[str, ...]]
    worker_tools: tuple[str, ...] | None


def expanded_tool_names(config: Config, tool_names: list[str], disabled: frozenset[str]) -> tuple[str, ...]:
    """Return ``tool_names`` with presets and implied toolkits expanded, less disabled toolkits."""
    return tuple(name for name in config.expand_tool_names(tool_names) if name not in disabled)


def agent_tool_facts(config: Config, agent_name: str, *, policy: ResolvedAgentPolicy) -> AgentToolFacts:
    """Return what ``agent_name``'s config decides about its toolkits."""
    agent = config.get_agent(agent_name)
    graph = uses_knowledge_graph(config, agent_name)
    allow_self_config = (
        agent.allow_self_config if agent.allow_self_config is not None else config.defaults.allow_self_config
    )
    injected = [
        name
        for name, enabled in (
            ("delegate", bool(agent.delegate_to)),
            ("self_config", allow_self_config),
            ("agent_manager", config.generated_agent(agent_name) is not None or agent.created_agents_per_owner > 0),
            # Like Hermes, an agent that learns skills can also save them in chat, and its skill review can only call
            # tools the agent's own request offered.
            ("skill_manage", agent.skill_learning.enabled),
            # A contact answers only through matrix_message, so an agent with contacts always has it.
            ("contacts", agent.contacts is not None),
            ("matrix_message", agent.contacts is not None),
            ("knowledge_graph", graph),
        )
        if enabled
    ]
    disabled = frozenset(
        name
        for name, off in (
            ("memory", memory_backend(config, agent_name) == "none"),
            ("knowledge_graph", not graph),
            ("delegate", not agent.delegate_to),
        )
        if off
    )
    authored = authored_tool_entries(config, agent_name)

    def expanded(entries: Iterable[EffectiveToolConfig]) -> tuple[str, ...]:
        return expanded_tool_names(config, [entry.name for entry in entries], disabled)

    scope = policy.effective_execution_scope
    worker_tools = agent.worker_tools if agent.worker_tools is not None else config.defaults.worker_tools
    return AgentToolFacts(
        agent_name=agent_name,
        authored=authored,
        injected=tuple(injected),
        disabled=disabled,
        tool_names=expanded(authored),
        startup_tool_names=frozenset(expanded(entry for entry in authored if not entry.defer or entry.initial)),
        scope_incompatible={
            entry.name: incompatible
            for entry in authored
            if entry.defer
            and (
                incompatible := tuple(
                    unsupported_shared_only_integration_names(config.expand_tool_names([entry.name]), scope),
                )
            )
        },
        worker_tools=None if worker_tools is None else tuple(config.expand_tool_names(worker_tools)),
    )


@dataclass(frozen=True)
class ResolvedScope:
    """What every scope gets: an agent, a team, the router, or the defaults scope of an ad hoc team (``name`` None)."""

    history_settings: ResolvedHistorySettings
    max_tool_calls_per_turn: int
    file_access: FileAccess
    memory_backend: MemoryBackend
    timezone: str
    show_tool_calls: bool


@dataclass(frozen=True)
class ResolvedAgent(ResolvedScope):
    """What one configured agent gets."""

    policy: ResolvedAgentPolicy
    tools: AgentToolFacts
    knowledge_base_ids: tuple[str, ...]
    automations: tuple[Automation, ...]
    markdown: bool
    knowledge_graph: bool


def resolve_scope(config: Config, name: str | None) -> ResolvedScope:
    """Return what one agent, team, or the router gets in common with every scope; None is the defaults scope."""
    entity = configured_entity(config, name) if name is not None and name != ROUTER_AGENT_NAME else None
    own_calls = entity.max_tool_calls_per_turn if entity is not None else None
    return ResolvedScope(
        history_settings=_history_settings(config, entity),
        max_tool_calls_per_turn=own_calls if own_calls is not None else config.defaults.max_tool_calls_per_turn,
        file_access=file_access(config, name),
        memory_backend=memory_backend(config, name),
        timezone=entity_timezone(config, name),
        show_tool_calls=show_tool_calls(config, name),
    )


def entity_model_name(config: Config, name: str) -> str:
    """Return the authored model of an agent, team, or the router; an unknown name raises."""
    if name == ROUTER_AGENT_NAME:
        return config.router.model
    if name in config.teams:
        return config.teams[name].model
    if name in config.agents:
        return config.agents[name].model
    available = sorted(set(config.agents) | set(config.teams) | {ROUTER_AGENT_NAME})
    msg = f"Unknown entity: {name}. Available entities: {', '.join(available)}"
    raise ValueError(msg)


def agent_knowledge_base_ids(config: Config, agent_name: str) -> tuple[str, ...]:
    """Return the knowledge bases one agent searches: its authored ones, then its private base."""
    private_base = agent_policy(config, agent_name).private_knowledge_base_id
    return (*config.get_agent(agent_name).knowledge_bases, *((private_base,) if private_base is not None else ()))


def resolve_agent(config: Config, name: str) -> ResolvedAgent:
    """Return what one configured agent gets."""
    agent = config.get_agent(name)
    policy = agent_policy(config, name)
    return ResolvedAgent(
        **vars(resolve_scope(config, name)),
        policy=policy,
        tools=agent_tool_facts(config, name, policy=policy),
        knowledge_base_ids=agent_knowledge_base_ids(config, name),
        automations=agent_automations(config, name),
        markdown=agent.markdown if agent.markdown is not None else config.defaults.markdown,
        knowledge_graph=uses_knowledge_graph(config, name),
    )
