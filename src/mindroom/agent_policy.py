"""Canonical agent-policy derivation from authored config fields."""

from __future__ import annotations

from dataclasses import dataclass
from typing import TYPE_CHECKING, Any, Literal, cast

from mindroom.config.agent import AgentConfig

if TYPE_CHECKING:
    from collections.abc import Mapping

    from mindroom.tool_system.worker_routing import WorkerScope

PrivateWorkerScope = Literal["user", "user_agent"]
AgentPolicySource = Literal["private.per", "agent.worker_scope", "defaults.worker_scope", "unscoped"]
DEFAULT_PRIVATE_KNOWLEDGE_BASE_ID_PREFIX = "__agent_private__:"


@dataclass(frozen=True)
class AgentPolicySeed:
    """Minimal authored fields required to derive one agent policy."""

    agent_name: str
    delegate_to: tuple[str, ...]
    is_private: bool
    private_scope: PrivateWorkerScope | None
    worker_scope: WorkerScope | None
    default_worker_scope: WorkerScope | None
    private_knowledge_enabled: bool


@dataclass(frozen=True)
class ResolvedAgentPolicy:
    """Canonical backend-derived policy for one agent."""

    agent_name: str
    is_private: bool
    effective_execution_scope: WorkerScope | None
    scope_label: str
    scope_source: AgentPolicySource
    dashboard_credentials_supported: bool
    team_eligibility_reason: str | None
    private_knowledge_base_id: str | None
    request_scoped_workspace_enabled: bool
    request_scoped_knowledge_enabled: bool


@dataclass(frozen=True)
class ResolvedAgentPolicyIndex:
    """Resolved policies plus intermediate graph data for shared consumers."""

    policies: dict[str, ResolvedAgentPolicy]
    delegation_closures: dict[str, frozenset[str]]
    private_targets_by_agent: dict[str, tuple[str, ...]]


def _coerce_worker_scope(value: object) -> WorkerScope | None:
    if value in {"shared", "user", "user_agent"}:
        return cast("WorkerScope", value)
    return None


def _coerce_private_scope(value: object) -> PrivateWorkerScope | None:
    if value in {"user", "user_agent"}:
        return cast("PrivateWorkerScope", value)
    return None


def build_agent_policy_seed(
    agent_name: str,
    agent_data: AgentConfig | Mapping[str, Any],
    *,
    default_worker_scope: WorkerScope | None,
) -> AgentPolicySeed:
    """Build one policy seed from typed config or draft payload data."""
    if isinstance(agent_data, AgentConfig):
        private_config = agent_data.private
        private_knowledge = private_config.knowledge if private_config is not None else None
        return AgentPolicySeed(
            agent_name=agent_name,
            delegate_to=tuple(agent_data.delegate_to),
            is_private=private_config is not None,
            private_scope=private_config.per if private_config is not None else None,
            worker_scope=agent_data.worker_scope,
            default_worker_scope=default_worker_scope,
            private_knowledge_enabled=(
                private_knowledge is not None and private_knowledge.enabled and private_knowledge.path is not None
            ),
        )

    raw_private = agent_data.get("private")
    raw_private_mapping = raw_private if isinstance(raw_private, dict) else None
    raw_private_knowledge = raw_private_mapping.get("knowledge") if raw_private_mapping is not None else None
    raw_private_knowledge_mapping = raw_private_knowledge if isinstance(raw_private_knowledge, dict) else None
    raw_delegate_to = agent_data.get("delegate_to")
    delegate_to: tuple[str, ...] = ()
    if isinstance(raw_delegate_to, list | tuple):
        delegate_to = tuple(target for target in raw_delegate_to if isinstance(target, str))
    private_knowledge_path = (
        raw_private_knowledge_mapping.get("path") if raw_private_knowledge_mapping is not None else None
    )
    return AgentPolicySeed(
        agent_name=agent_name,
        delegate_to=delegate_to,
        is_private=raw_private is not None,
        private_scope=(
            _coerce_private_scope(raw_private_mapping.get("per")) if raw_private_mapping is not None else None
        ),
        worker_scope=_coerce_worker_scope(agent_data.get("worker_scope")),
        default_worker_scope=default_worker_scope,
        private_knowledge_enabled=(
            raw_private_knowledge_mapping is not None
            and raw_private_knowledge_mapping.get("enabled") is not False
            and isinstance(private_knowledge_path, str)
        ),
    )


def build_agent_policy_seeds(
    agents: Mapping[str, AgentConfig | Mapping[str, Any]],
    *,
    default_worker_scope: WorkerScope | None,
) -> dict[str, AgentPolicySeed]:
    """Build policy seeds for all configured agents."""
    return {
        agent_name: build_agent_policy_seed(
            agent_name,
            agent_data,
            default_worker_scope=default_worker_scope,
        )
        for agent_name, agent_data in agents.items()
    }


def _resolved_scope_and_source(seed: AgentPolicySeed) -> tuple[WorkerScope | None, str, AgentPolicySource]:
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


def resolve_agent_policy(
    seed: AgentPolicySeed,
    *,
    team_eligibility_reason: str | None = None,
    private_knowledge_base_id_prefix: str = DEFAULT_PRIVATE_KNOWLEDGE_BASE_ID_PREFIX,
) -> ResolvedAgentPolicy:
    """Resolve one canonical agent policy from one authored seed."""
    execution_scope, scope_label, scope_source = _resolved_scope_and_source(seed)
    private_knowledge_base_id = None
    if seed.is_private and seed.private_knowledge_enabled:
        private_knowledge_base_id = f"{private_knowledge_base_id_prefix}{seed.agent_name}"
    request_scoped_workspace_enabled = seed.is_private and execution_scope not in {None, "shared"}
    request_scoped_knowledge_enabled = private_knowledge_base_id is not None and execution_scope not in {
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
        team_eligibility_reason=team_eligibility_reason,
        private_knowledge_base_id=private_knowledge_base_id,
        request_scoped_workspace_enabled=request_scoped_workspace_enabled,
        request_scoped_knowledge_enabled=request_scoped_knowledge_enabled,
    )


def resolve_agent_policy_from_data(
    agent_name: str,
    agent_data: AgentConfig | Mapping[str, Any],
    *,
    default_worker_scope: WorkerScope | None,
    team_eligibility_reason: str | None = None,
    private_knowledge_base_id_prefix: str = DEFAULT_PRIVATE_KNOWLEDGE_BASE_ID_PREFIX,
) -> ResolvedAgentPolicy:
    """Resolve one canonical agent policy from typed config or draft payload data."""
    return resolve_agent_policy(
        build_agent_policy_seed(
            agent_name,
            agent_data,
            default_worker_scope=default_worker_scope,
        ),
        team_eligibility_reason=team_eligibility_reason,
        private_knowledge_base_id_prefix=private_knowledge_base_id_prefix,
    )


def get_agent_delegation_closure(
    agent_name: str,
    seeds: Mapping[str, AgentPolicySeed],
    *,
    closures: dict[str, frozenset[str]] | None = None,
    visiting: frozenset[str] = frozenset(),
) -> frozenset[str]:
    """Return one agent plus all agents reachable through transitive delegation."""
    if closures is None:
        closures = {}
    if agent_name in closures:
        return closures[agent_name]
    # Keep the argument for compatibility with existing callers. The closure walk is now
    # iterative so we never cache or observe partial results from an active cycle.
    _ = visiting

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


def get_private_team_targets(
    agent_name: str,
    seeds: Mapping[str, AgentPolicySeed],
    *,
    closures: dict[str, frozenset[str]] | None = None,
) -> tuple[str, ...]:
    """Return private agents reachable from one team member, including itself."""
    closure_cache = closures if closures is not None else {}
    return tuple(
        sorted(
            target_name
            for target_name in get_agent_delegation_closure(
                agent_name,
                seeds,
                closures=closure_cache,
            )
            if (target_seed := seeds.get(target_name)) is not None and target_seed.is_private
        ),
    )


def get_unsupported_team_agents(
    agent_names: list[str],
    seeds: Mapping[str, AgentPolicySeed],
    *,
    closures: dict[str, frozenset[str]] | None = None,
) -> dict[str, tuple[str, ...] | None]:
    """Return unsupported team members keyed by agent name."""
    closure_cache = closures if closures is not None else {}
    unsupported_agents: dict[str, tuple[str, ...] | None] = {}
    for agent_name in agent_names:
        if agent_name not in seeds:
            unsupported_agents[agent_name] = None
            continue
        private_targets = get_private_team_targets(agent_name, seeds, closures=closure_cache)
        if private_targets:
            unsupported_agents[agent_name] = private_targets
    return unsupported_agents


def team_eligibility_reason(
    agent_name: str,
    *,
    private_targets: tuple[str, ...] | None,
) -> str | None:
    """Return the concise editor-facing team-eligibility reason for one agent."""
    if private_targets is None:
        return f"Unknown agent '{agent_name}'."
    if not private_targets:
        return None
    if agent_name in private_targets:
        return "Private agents cannot participate in teams yet."
    if len(private_targets) == 1:
        return f"Delegates to private agent '{private_targets[0]}', so it cannot participate in teams yet."
    return (
        "Delegates to private agents "
        f"{', '.join(repr(target) for target in private_targets)}, so it cannot participate in teams yet."
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
        return f"{prefix} includes private agent '{agent_name}'; private agents cannot participate in teams yet"
    if len(private_targets) == 1:
        return (
            f"{prefix} includes agent '{agent_name}' which reaches private agent "
            f"'{private_targets[0]}' via delegation; private agents cannot participate in teams yet"
        )
    return (
        f"{prefix} includes agent '{agent_name}' which reaches private agents "
        f"{', '.join(repr(target) for target in private_targets)} via delegation; "
        "private agents cannot participate in teams yet"
    )


def resolve_agent_policy_index(
    seeds: Mapping[str, AgentPolicySeed],
    *,
    private_knowledge_base_id_prefix: str = DEFAULT_PRIVATE_KNOWLEDGE_BASE_ID_PREFIX,
) -> ResolvedAgentPolicyIndex:
    """Resolve canonical policies for all agents from one shared seed set."""
    closure_cache: dict[str, frozenset[str]] = {}
    private_targets_by_agent = {
        agent_name: get_private_team_targets(
            agent_name,
            seeds,
            closures=closure_cache,
        )
        for agent_name in seeds
    }
    policies = {
        agent_name: resolve_agent_policy(
            seed,
            team_eligibility_reason=team_eligibility_reason(
                agent_name,
                private_targets=private_targets_by_agent[agent_name],
            ),
            private_knowledge_base_id_prefix=private_knowledge_base_id_prefix,
        )
        for agent_name, seed in seeds.items()
    }
    return ResolvedAgentPolicyIndex(
        policies=policies,
        delegation_closures=closure_cache,
        private_targets_by_agent=private_targets_by_agent,
    )


def resolve_private_knowledge_base_agent(
    base_id: str,
    seeds: Mapping[str, AgentPolicySeed],
    *,
    private_knowledge_base_id_prefix: str = DEFAULT_PRIVATE_KNOWLEDGE_BASE_ID_PREFIX,
) -> str | None:
    """Return the owning agent for one synthetic private knowledge base ID."""
    if not base_id.startswith(private_knowledge_base_id_prefix):
        return None
    agent_name = base_id.removeprefix(private_knowledge_base_id_prefix)
    seed = seeds.get(agent_name)
    if seed is None:
        return None
    policy = resolve_agent_policy(
        seed,
        private_knowledge_base_id_prefix=private_knowledge_base_id_prefix,
    )
    if policy.private_knowledge_base_id != base_id:
        return None
    return agent_name
