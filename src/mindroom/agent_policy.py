"""Canonical agent-policy derivation from authored config fields."""

from __future__ import annotations

from dataclasses import dataclass
from typing import TYPE_CHECKING, Annotated, Literal

from pydantic import BaseModel, ConfigDict, Field, StringConstraints

from mindroom.tool_system.worker_routing import WorkerScope  # noqa: TC001

if TYPE_CHECKING:
    from collections.abc import Mapping

    from mindroom.config.agent import AgentConfig

_PrivateWorkerScope = Literal["user", "user_agent"]
_AgentPolicySource = Literal["private.per", "agent.worker_scope", "defaults.worker_scope", "unscoped"]
# The prefix of a private agent's synthetic knowledge base ID, which authored knowledge bases may not use.
PRIVATE_KNOWLEDGE_BASE_ID_PREFIX = "__agent_private__:"


class PolicyKnowledgeInputs(BaseModel):
    """The ``private.knowledge`` fields an agent's policy reads."""

    model_config = ConfigDict(extra="ignore")

    enabled: bool | None = None
    path: str | None = None


class PolicyPrivateInputs(BaseModel):
    """The ``private`` fields an agent's policy reads."""

    model_config = ConfigDict(extra="ignore")

    per: _PrivateWorkerScope | None = None
    root: Annotated[str, StringConstraints(strip_whitespace=True)] | None = None
    knowledge: PolicyKnowledgeInputs | None = None


class PolicyInputs(BaseModel):
    """The authored fields of one agent its execution policy derives from: a validated agent, a draft, or a file's.

    A ``private`` block makes the agent private. Its ``per`` scopes the agent; without it the agent's own
    ``worker_scope`` does, then ``defaults.worker_scope``, else the agent is unscoped. Private knowledge is on when it
    has a ``path`` and ``enabled`` is not false.
    """

    model_config = ConfigDict(extra="ignore")

    worker_scope: WorkerScope | None = None
    private: PolicyPrivateInputs | None = None
    delegate_to: list[str] = Field(default_factory=list)

    @classmethod
    def from_agent(cls, agent: AgentConfig) -> PolicyInputs:
        """Return the policy inputs of one validated agent config."""
        return cls.model_validate(agent, from_attributes=True)


@dataclass(frozen=True)
class AgentPolicySeed:
    """Minimal authored fields required to derive one agent policy."""

    agent_name: str
    delegate_to: tuple[str, ...]
    is_private: bool
    private_scope: _PrivateWorkerScope | None
    worker_scope: WorkerScope | None
    default_worker_scope: WorkerScope | None
    private_knowledge_enabled: bool
    private_root: str | None


@dataclass(frozen=True)
class ResolvedAgentPolicy:
    """Canonical backend-derived policy for one agent."""

    agent_name: str
    is_private: bool
    effective_execution_scope: WorkerScope | None
    scope_label: str
    scope_source: _AgentPolicySource
    dashboard_credentials_supported: bool
    private_knowledge_base_id: str | None
    private_workspace_enabled: bool
    private_agent_knowledge_enabled: bool
    private_root: str | None
    """The authored ``private.root``; ``worker_routing.private_root_name`` applies its default."""


@dataclass(frozen=True)
class ResolvedAgentPolicyIndex:
    """Resolved policies plus each agent's team eligibility."""

    policies: dict[str, ResolvedAgentPolicy]
    team_eligibility_reasons: dict[str, str | None]
    """Why each agent cannot be a team member, or None when it can."""


def _build_agent_policy_seed(
    agent_name: str,
    inputs: PolicyInputs,
    *,
    default_worker_scope: WorkerScope | None,
) -> AgentPolicySeed:
    """Build one policy seed from one agent's policy inputs."""
    private = inputs.private
    knowledge = private.knowledge if private is not None else None
    return AgentPolicySeed(
        agent_name=agent_name,
        delegate_to=tuple(inputs.delegate_to),
        is_private=private is not None,
        private_scope=private.per if private is not None else None,
        worker_scope=inputs.worker_scope,
        default_worker_scope=default_worker_scope,
        private_knowledge_enabled=(
            knowledge is not None and knowledge.enabled is not False and knowledge.path is not None
        ),
        private_root=private.root if private is not None else None,
    )


def build_agent_policy_seeds(
    agents: Mapping[str, PolicyInputs],
    *,
    default_worker_scope: WorkerScope | None,
) -> dict[str, AgentPolicySeed]:
    """Build policy seeds for all configured agents."""
    return {
        agent_name: _build_agent_policy_seed(
            agent_name,
            inputs,
            default_worker_scope=default_worker_scope,
        )
        for agent_name, inputs in agents.items()
    }


def _resolved_scope_and_source(seed: AgentPolicySeed) -> tuple[WorkerScope | None, str, _AgentPolicySource]:
    if seed.private_scope is not None:
        return seed.private_scope, f"private.per={seed.private_scope}", "private.per"
    if seed.worker_scope is not None:
        return seed.worker_scope, f"worker_scope={seed.worker_scope}", "agent.worker_scope"
    if seed.default_worker_scope is not None:
        return seed.default_worker_scope, f"worker_scope={seed.default_worker_scope}", "defaults.worker_scope"
    return None, "unscoped", "unscoped"


def dashboard_credentials_supported_for_scope(worker_scope: WorkerScope | None) -> bool:
    """Return whether dashboard credential management supports one execution scope."""
    return worker_scope in {None, "shared"}


def _resolve_agent_policy(seed: AgentPolicySeed) -> ResolvedAgentPolicy:
    """Resolve one canonical agent policy from one authored seed."""
    execution_scope, scope_label, scope_source = _resolved_scope_and_source(seed)
    private_knowledge_base_id = (
        f"{PRIVATE_KNOWLEDGE_BASE_ID_PREFIX}{seed.agent_name}"
        if seed.is_private and seed.private_knowledge_enabled
        else None
    )
    private_workspace_enabled = seed.is_private and execution_scope not in {None, "shared"}
    private_agent_knowledge_enabled = private_knowledge_base_id is not None and execution_scope not in {
        None,
        "shared",
    }
    return ResolvedAgentPolicy(
        agent_name=seed.agent_name,
        is_private=seed.is_private,
        effective_execution_scope=execution_scope,
        scope_label=scope_label,
        scope_source=scope_source,
        dashboard_credentials_supported=dashboard_credentials_supported_for_scope(execution_scope),
        private_knowledge_base_id=private_knowledge_base_id,
        private_workspace_enabled=private_workspace_enabled,
        private_agent_knowledge_enabled=private_agent_knowledge_enabled,
        private_root=seed.private_root if seed.is_private else None,
    )


def resolve_agent_policy(
    agent_name: str,
    inputs: PolicyInputs,
    *,
    default_worker_scope: WorkerScope | None,
) -> ResolvedAgentPolicy:
    """Resolve one agent's canonical policy from its policy inputs."""
    return _resolve_agent_policy(
        _build_agent_policy_seed(
            agent_name,
            inputs,
            default_worker_scope=default_worker_scope,
        ),
    )


def get_agent_delegation_closure(
    agent_name: str,
    seeds: Mapping[str, AgentPolicySeed],
    *,
    closures: dict[str, frozenset[str]] | None = None,
) -> frozenset[str]:
    """Return one agent plus all agents reachable through transitive delegation."""
    if closures is None:
        closures = {}
    if agent_name in closures:
        return closures[agent_name]

    reachable: set[str] = set()
    pending = [agent_name]
    while pending:
        current_agent_name = pending.pop()
        if current_agent_name in reachable:
            continue
        if current_agent_name != agent_name and current_agent_name in closures:
            reachable.update(closures[current_agent_name])
            continue
        reachable.add(current_agent_name)
        current_seed = seeds.get(current_agent_name)
        if current_seed is None:
            continue
        pending.extend(current_seed.delegate_to)

    result = frozenset(reachable)
    closures[agent_name] = result
    return result


def _get_private_team_targets(
    agent_name: str,
    seeds: Mapping[str, AgentPolicySeed],
    *,
    closures: dict[str, frozenset[str]],
) -> tuple[str, ...]:
    """Return private agents reachable from one team member, including itself."""
    return tuple(
        sorted(
            target_name
            for target_name in get_agent_delegation_closure(agent_name, seeds, closures=closures)
            if (target_seed := seeds.get(target_name)) is not None and target_seed.is_private
        ),
    )


def get_unsupported_team_agents(
    agent_names: list[str],
    seeds: Mapping[str, AgentPolicySeed],
    *,
    allow_direct_private_agents: bool = False,
) -> dict[str, tuple[str, ...] | None]:
    """Return unsupported team members keyed by agent name."""
    closure_cache: dict[str, frozenset[str]] = {}
    unsupported_agents: dict[str, tuple[str, ...] | None] = {}
    for agent_name in agent_names:
        if agent_name not in seeds:
            unsupported_agents[agent_name] = None
            continue
        private_targets = _get_private_team_targets(agent_name, seeds, closures=closure_cache)
        if allow_direct_private_agents and agent_name in private_targets:
            private_targets = tuple(target for target in private_targets if target != agent_name)
        if private_targets:
            unsupported_agents[agent_name] = private_targets
    return unsupported_agents


def _team_eligibility_reason(
    agent_name: str,
    *,
    private_targets: tuple[str, ...],
) -> str | None:
    """Return the concise editor-facing team-eligibility reason for one agent."""
    if not private_targets:
        return None
    if agent_name in private_targets:
        return "Private agents cannot be configured as team members."
    if len(private_targets) == 1:
        return f"Delegates to private agent '{private_targets[0]}', so it cannot participate in teams."
    return (
        "Delegates to private agents "
        f"{', '.join(repr(target) for target in private_targets)}, so it cannot participate in teams."
    )


def unsupported_team_agent_message(
    agent_name: str,
    *,
    prefix: str,
    private_targets: tuple[str, ...] | None,
) -> str:
    """Return the user-facing error for one unsupported team member."""
    if private_targets is None:
        return f"{prefix} references unknown agent '{agent_name}'"
    if agent_name in private_targets:
        return (
            f"{prefix} includes private agent '{agent_name}'; private agents are only supported "
            "in explicit Matrix ad hoc teams with requester identity"
        )
    if len(private_targets) == 1:
        return (
            f"{prefix} includes agent '{agent_name}' which reaches private agent "
            f"'{private_targets[0]}' via delegation; private delegation is not supported for teams"
        )
    return (
        f"{prefix} includes agent '{agent_name}' which reaches private agents "
        f"{', '.join(repr(target) for target in private_targets)} via delegation; "
        "private delegation is not supported for teams"
    )


def resolve_agent_policy_index(seeds: Mapping[str, AgentPolicySeed]) -> ResolvedAgentPolicyIndex:
    """Resolve canonical policies for all agents from one shared seed set."""
    closure_cache: dict[str, frozenset[str]] = {}
    private_targets_by_agent = {
        agent_name: _get_private_team_targets(agent_name, seeds, closures=closure_cache) for agent_name in seeds
    }
    return ResolvedAgentPolicyIndex(
        policies={agent_name: _resolve_agent_policy(seed) for agent_name, seed in seeds.items()},
        team_eligibility_reasons={
            agent_name: _team_eligibility_reason(agent_name, private_targets=private_targets)
            for agent_name, private_targets in private_targets_by_agent.items()
        },
    )


__all__ = [
    "PRIVATE_KNOWLEDGE_BASE_ID_PREFIX",
    "AgentPolicySeed",
    "PolicyInputs",
    "PolicyKnowledgeInputs",
    "PolicyPrivateInputs",
    "ResolvedAgentPolicy",
    "ResolvedAgentPolicyIndex",
    "build_agent_policy_seeds",
    "dashboard_credentials_supported_for_scope",
    "get_agent_delegation_closure",
    "get_unsupported_team_agents",
    "resolve_agent_policy",
    "resolve_agent_policy_index",
    "unsupported_team_agent_message",
]
