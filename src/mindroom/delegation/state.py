"""Serializable runtime ownership for fresh delegated child runs."""

from __future__ import annotations

from dataclasses import asdict, dataclass, field
from typing import TYPE_CHECKING, Any, Literal, Protocol, cast

if TYPE_CHECKING:
    from collections.abc import Collection, Mapping

    from mindroom.agent_modes import AgentMode
    from mindroom.config.main import Config
    from mindroom.constants import RuntimePaths
    from mindroom.knowledge.refresh_scheduler import KnowledgeRefreshScheduler
    from mindroom.llm.runs import RunRequirement

DELEGATION_STATE_KEY = "mindroom_delegation"

type PersonaSourceKind = Literal["inline", "profile", "workflow"]
_SOURCE_KINDS: tuple[PersonaSourceKind, ...] = ("inline", "profile", "workflow")


@dataclass(frozen=True)
class SubagentPersona:
    """The authored presentation of one child: its authored prompt and optional tool, skill, and knowledge subsets."""

    source_kind: PersonaSourceKind
    source_name: str
    system_prompt: str
    tools: tuple[str, ...] | None = None
    skills: tuple[str, ...] | None = None
    knowledge: tuple[str, ...] | None = None

    @property
    def kept_skills(self) -> tuple[str, ...] | None:
        """Return the caller skills this persona keeps, or None for every one."""
        return self._kept(self.skills)

    @property
    def kept_knowledge(self) -> tuple[str, ...] | None:
        """Return the caller knowledge bases this persona can search, or None for every one."""
        return self._kept(self.knowledge)

    def _kept(self, names: tuple[str, ...] | None) -> tuple[str, ...] | None:
        # Skills and knowledge follow an explicit tool list: a persona that lists tools but not them keeps
        # none, so a child narrowed to a few tools never gains skill or knowledge-search functions.
        if names is None and self.tools is not None:
            return ()
        return names

    @classmethod
    def from_dict(cls, data: Mapping[str, object]) -> SubagentPersona:
        """Read a snapshot that ``asdict`` wrote into delegation state or a session record."""
        source_kind, source_name, system_prompt, tools = (
            data["source_kind"],
            data["source_name"],
            data["system_prompt"],
            data["tools"],
        )
        # LEGACY_COMPAT: Authored subagent personas persisted without skills.
        # Legacy format: Persona snapshots in parent delegation state and subagent session records omitted skills; from v2026.10.236 every authored child ran without skills, while v2026.10.231 through v2026.10.235 still loaded caller skills without listing them, as functions for a child without a tool list and as documents for a minimal child.
        # Last legacy release: v2026.10.245; replacement: the next release persists each persona's skills, null keeping every caller skill.
        # Handling: An absent skills field reads as no skills, matching the latest legacy releases, so retained authored children and their follow-ups keep running without skills.
        # Coverage: tests/test_delegation_sessions.py::test_persona_snapshot_without_skills_reads_as_skill_less.
        skills = data.get("skills", ())
        # LEGACY_COMPAT: Authored subagent personas persisted without knowledge.
        # Legacy format: Persona snapshots in parent delegation state and subagent session records omitted knowledge; an authored child with a tool list had no knowledge search and one without a tool list searched every caller knowledge base.
        # Last legacy release: v2026.10.262; replacement: the next release persists each persona's knowledge bases, null following its tool list.
        # Handling: An absent knowledge field reads as null, which follows the tool list exactly as those releases did, so retained authored children and their follow-ups keep their knowledge search.
        # Coverage: tests/test_delegation_sessions.py::test_persona_snapshot_without_knowledge_follows_its_tool_list.
        knowledge = data.get("knowledge")
        if (
            source_kind not in _SOURCE_KINDS
            or not isinstance(source_name, str)
            or not isinstance(system_prompt, str)
            or not _optional_names(tools)
            or not _optional_names(skills)
            or not _optional_names(knowledge)
        ):
            msg = "Invalid subagent persona snapshot"
            raise TypeError(msg)
        return cls(
            source_kind=cast("PersonaSourceKind", source_kind),
            source_name=source_name,
            system_prompt=system_prompt,
            tools=None if tools is None else tuple(cast("list[str] | tuple[str, ...]", tools)),
            skills=None if skills is None else tuple(cast("list[str] | tuple[str, ...]", skills)),
            knowledge=None if knowledge is None else tuple(cast("list[str] | tuple[str, ...]", knowledge)),
        )


def _optional_names(value: object) -> bool:
    return value is None or (isinstance(value, (list, tuple)) and all(isinstance(name, str) for name in value))


@dataclass
class DelegationChild:
    """One parent requirement and its stable child execution identity."""

    delegation_id: str
    parent_tool_call_id: str
    caller_agent_name: str
    child_agent_name: str
    task: str
    session_id: str
    run_id: str
    model_name: str
    depth: int
    execution_identity: dict[str, object]
    subagent_id: str | None = None
    previous_delegation_id: str | None = None
    parent_requirement_id: str = ""
    storage_bindings: dict[str, dict[str, object]] = field(default_factory=dict)
    record_locator: dict[str, object] = field(default_factory=dict)
    status: Literal["running", "paused", "completed", "failed", "cancelled", "denied"] = "running"
    result: str | None = None
    # LEGACY_COMPAT: Delegated children persisted without an agent mode.
    # Legacy format: Parent delegation state and subagent session records omitted agent_mode; every child ran in standard mode.
    # Last legacy release: v2026.10.18; replacement: the next release persists the child's standard or minimal mode.
    # Handling: The dataclass default reads an absent mode as standard, so retained children and follow-ups keep their behavior.
    # Coverage: tests/test_delegation_minimal_mode.py::test_child_snapshot_without_mode_continues_in_standard_mode.
    agent_mode: AgentMode = "standard"
    # The model the child was asked to use before the requester's budget picked the one it runs on.
    requested_model_name: str | None = None
    # LEGACY_COMPAT: Delegated children persisted without a persona.
    # Legacy format: Parent delegation state and subagent session records omitted persona; every child ran its configured prompt.
    # Last legacy release: v2026.10.230; replacement: the next release persists each child's authored persona or null.
    # Handling: The dataclass default and from_dict read an absent persona as a configured-agent child, so retained children keep their behavior.
    # Coverage: tests/test_delegation_sessions.py::test_child_snapshot_without_persona_reads_as_configured_child.
    persona: SubagentPersona | None = None

    @classmethod
    def from_dict(cls, data: Mapping[str, Any]) -> DelegationChild:
        """Read a snapshot written by ``dataclasses.asdict``."""
        fields: dict[str, Any] = dict(data)
        persona = fields.pop("persona", None)
        return cls(**fields, persona=None if persona is None else SubagentPersona.from_dict(persona))


@dataclass
class DelegationHookState:
    """Durable before/after phases for one external parent tool call."""

    execution_identity: dict[str, object]
    arguments: dict[str, Any]
    started_at: float
    tool_name: str = "run_subagent"
    blocked_result: str | None = None
    after_called: bool = False


@dataclass
class DelegationPendingTool:
    """Exact child attempt and local call behind one projected approval."""

    child: DelegationChild
    tool_call_id: str
    toolkit_name: str | None = None


@dataclass
class DelegationState:
    """Persisted parent waits; workspace exports never own this state."""

    children: list[DelegationChild] = field(default_factory=list)
    gates: dict[str, bool] = field(default_factory=dict)
    hooks: dict[str, DelegationHookState] = field(default_factory=dict)
    pending_tools: list[dict[str, Any]] = field(default_factory=list)
    pending_tool_sources: dict[str, DelegationPendingTool] = field(default_factory=dict)
    pending_requirements: list[dict[str, Any]] = field(default_factory=list)
    pending_agent_name: str | None = None
    pending_child_id: str | None = None
    storage_bindings: dict[str, dict[str, object]] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        """Return an independent JSON-compatible snapshot."""
        return asdict(self)

    @classmethod
    def from_metadata(cls, metadata: dict[str, Any] | None) -> DelegationState:
        """Read the driver-owned snapshot attached to an Agno run."""
        stored = (metadata or {}).get(DELEGATION_STATE_KEY)
        if stored is None:
            return cls()
        if not isinstance(stored, dict):
            msg = "Invalid delegation runtime state"
            raise TypeError(msg)
        snapshot = cast("dict[str, Any]", stored)
        return cls(
            children=[DelegationChild.from_dict(child) for child in snapshot.get("children", [])],
            gates=dict(snapshot.get("gates", {})),
            hooks={key: DelegationHookState(**value) for key, value in snapshot.get("hooks", {}).items()},
            pending_tools=list(snapshot.get("pending_tools", [])),
            pending_tool_sources={
                call_id: DelegationPendingTool(
                    child=DelegationChild.from_dict(source["child"]),
                    tool_call_id=source["tool_call_id"],
                    toolkit_name=source.get("toolkit_name"),
                )
                for call_id, source in snapshot.get("pending_tool_sources", {}).items()
            },
            pending_requirements=list(snapshot.get("pending_requirements", [])),
            pending_agent_name=snapshot.get("pending_agent_name"),
            pending_child_id=snapshot.get("pending_child_id"),
            storage_bindings=dict(snapshot.get("storage_bindings", {})),
        )

    def clear_pending(self) -> None:
        """Clear only the presented approval generation, retaining sibling results."""
        self.pending_tools = []
        self.pending_tool_sources = {}
        self.pending_requirements = []
        self.pending_agent_name = None
        self.pending_child_id = None


def delegation_card_id(member_id: str | None, call_id: str) -> str:
    """Return the id of a delegation call's gate and approval card: ``member_id:call_id`` for a team member's call."""
    return f"{member_id}:{call_id}" if member_id else call_id


def member_delegation_card(requirement: RunRequirement, member_ids: Collection[str]) -> tuple[str, str] | None:
    """Return the member and its own call id when ``requirement`` is the card of a delegation call a member made.

    The team holds such a card, so its requirement names no member and its call id is ``member_id:call_id`` for one
    of ``member_ids``. A child's projected tool id, ``delegation_id:call_id``, names no member, so it is not such a card.
    """
    card_id = requirement.tool_execution.tool_call_id
    if requirement.member_agent_id is not None or not card_id:
        return None
    member_id, _, call_id = card_id.partition(":")
    return (member_id, call_id) if call_id and member_id in member_ids else None


class ChildResponseRunner(Protocol):
    """Normal response envelope supplied by the caller of the native driver."""

    async def __call__(
        self,
        child: DelegationChild,
        *,
        prompt: str,
        config: Config,
        runtime_paths: RuntimePaths,
        refresh_scheduler: KnowledgeRefreshScheduler | None,
        supports_native_tool_approval: bool,
    ) -> str:
        """Execute a child response in its retained scope, reporting exact run IDs."""
        ...
