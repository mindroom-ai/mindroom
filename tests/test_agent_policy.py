"""Tests for canonical agent-policy derivation."""

from __future__ import annotations

from typing import TYPE_CHECKING

from mindroom.agent_policy import (
    build_agent_policy_seeds,
    resolve_agent_policy_from_data,
    resolve_agent_policy_index,
    resolve_private_knowledge_base_agent,
)
from mindroom.config.agent import AgentConfig, AgentPrivateConfig
from mindroom.config.main import Config
from mindroom.config.models import DefaultsConfig
from mindroom.tool_system.worker_routing import (
    agent_workspace_root_path,
    private_instance_scope_root_path,
    visible_workspace_roots,
)

if TYPE_CHECKING:
    from pathlib import Path


def test_resolve_agent_policy_uses_private_scope_and_private_label() -> None:
    """Private agents resolve scope from private.per and stay dashboard-isolated."""
    config = Config(
        defaults=DefaultsConfig(worker_scope="shared"),
        agents={
            "mind": AgentConfig(
                display_name="Mind",
                private={"per": "user"},
            ),
        },
    )

    policy = resolve_agent_policy_from_data(
        "mind",
        config.agents["mind"],
        default_worker_scope=config.defaults.worker_scope,
        private_knowledge_base_id_prefix=config.PRIVATE_KNOWLEDGE_BASE_ID_PREFIX,
    )

    assert policy.is_private is True
    assert policy.effective_execution_scope == "user"
    assert policy.scope_label == "private.per=user"
    assert policy.scope_source == "private.per"
    assert policy.dashboard_credentials_supported is False
    assert policy.private_workspace_enabled is True


def test_resolve_agent_policy_inherits_default_worker_scope_without_private_workspace() -> None:
    """Shared agents inherit defaults.worker_scope without becoming private workspaces."""
    policy = resolve_agent_policy_from_data(
        "general",
        AgentConfig(display_name="General"),
        default_worker_scope="user",
    )

    assert policy.effective_execution_scope == "user"
    assert policy.scope_label == "worker_scope=user"
    assert policy.scope_source == "defaults.worker_scope"
    assert policy.private_workspace_enabled is False
    assert policy.private_agent_knowledge_enabled is False


def test_policy_private_root_is_shared_between_typed_and_raw_config() -> None:
    """Backends read raw config and workers read typed config, so both derive the same private workspace path."""
    raw_agents = {
        "mind": {"display_name": "Mind", "private": {"per": "user_agent", "root": " workspace/mind "}},
        "notes": {"display_name": "Notes", "private": {"per": "user"}},
        "helper": {"display_name": "Helper", "worker_scope": "user"},
    }
    typed_agents = {name: AgentConfig.model_validate(data) for name, data in raw_agents.items()}

    for agents in (raw_agents, typed_agents):
        policies = resolve_agent_policy_index(
            build_agent_policy_seeds(agents, default_worker_scope=None),
        ).policies
        assert {name: policy.private_root for name, policy in policies.items()} == {
            "mind": "workspace/mind",
            "notes": None,
            "helper": None,
        }


def test_visible_workspace_roots_follow_private_visibility_and_scope(tmp_path: Path) -> None:
    """User-agent keys see a private workspace only with a private policy; user keys see only user-scope agents."""
    agents = {
        "mind": AgentConfig(display_name="Mind", private=AgentPrivateConfig(per="user_agent")),
        "notes": AgentConfig(display_name="Notes", private=AgentPrivateConfig(per="user")),
        "helper": AgentConfig(display_name="Helper", worker_scope="user"),
        "inherits_user": AgentConfig(display_name="Inherits User"),
        "shared": AgentConfig(display_name="Shared", worker_scope="shared"),
        "per_pair": AgentConfig(display_name="Per Pair", worker_scope="user_agent"),
    }
    policies = Config(agents=agents, defaults={"worker_scope": "user"}).get_agent_policies()
    user_agent_key = "v1:tenant:user_agent:@alice:localhost:mind"
    user_key = "v1:tenant:user:@alice:localhost"

    def roots(worker_key: str, *private_agent_names: str) -> tuple[Path, ...]:
        return visible_workspace_roots(
            tmp_path,
            worker_key,
            policies,
            private_agent_names=frozenset(private_agent_names),
        )

    assert roots(user_agent_key, "mind") == (
        private_instance_scope_root_path(tmp_path, user_agent_key) / "mind" / "mind_data",
    )
    assert roots(user_agent_key) == (agent_workspace_root_path(tmp_path, "mind"),)
    # A private agent the policies do not know keeps its default private workspace, never the shared one.
    assert visible_workspace_roots(tmp_path, user_agent_key, {}, private_agent_names=frozenset({"mind"})) == roots(
        user_agent_key,
        "mind",
    )
    assert roots(user_key) == (
        agent_workspace_root_path(tmp_path, "helper"),
        agent_workspace_root_path(tmp_path, "inherits_user"),
        private_instance_scope_root_path(tmp_path, user_key) / "notes" / "notes_data",
    )
    assert roots("v1:tenant:shared:helper") == (agent_workspace_root_path(tmp_path, "helper"),)


def test_resolve_agent_policy_index_marks_private_team_ineligibility() -> None:
    """Delegation into a private agent makes only the affected team members ineligible."""
    seeds = build_agent_policy_seeds(
        {
            "helper": AgentConfig(display_name="Helper"),
            "leader": AgentConfig(display_name="Leader", delegate_to=["mind"]),
            "mind": AgentConfig(display_name="Mind", private={"per": "user"}),
        },
        default_worker_scope=None,
    )

    index = resolve_agent_policy_index(seeds)

    assert index.policies["helper"].team_eligibility_reason is None
    assert (
        index.policies["leader"].team_eligibility_reason
        == "Delegates to private agent 'mind', so it cannot participate in teams."
    )
    assert index.policies["mind"].team_eligibility_reason == "Private agents cannot be configured as team members."


def test_resolve_agent_policy_index_is_order_independent_for_cycles() -> None:
    """Cyclic delegation should resolve the same private reachability for every query order."""
    agent_items = [
        ("a", AgentConfig(display_name="A", private={"per": "user"}, delegate_to=["b"])),
        ("b", AgentConfig(display_name="B", delegate_to=["a"])),
    ]

    forward_index = resolve_agent_policy_index(
        build_agent_policy_seeds(dict(agent_items), default_worker_scope=None),
    )
    reverse_index = resolve_agent_policy_index(
        build_agent_policy_seeds(dict(reversed(agent_items)), default_worker_scope=None),
    )

    for index in (forward_index, reverse_index):
        assert index.delegation_closures == {
            "a": frozenset({"a", "b"}),
            "b": frozenset({"a", "b"}),
        }
        assert index.private_targets_by_agent == {
            "a": ("a",),
            "b": ("a",),
        }
        assert index.policies["a"].team_eligibility_reason == "Private agents cannot be configured as team members."
        assert (
            index.policies["b"].team_eligibility_reason
            == "Delegates to private agent 'a', so it cannot participate in teams."
        )


def test_private_knowledge_base_derives_from_policy_seed() -> None:
    """Private knowledge derives a synthetic base ID only when enabled with a path."""
    config = Config(
        agents={
            "mind": AgentConfig(
                display_name="Mind",
                private={
                    "per": "user",
                    "knowledge": {
                        "enabled": True,
                        "path": "memory",
                    },
                },
            ),
        },
    )

    policy = resolve_agent_policy_from_data(
        "mind",
        config.agents["mind"],
        default_worker_scope=config.defaults.worker_scope,
        private_knowledge_base_id_prefix=config.PRIVATE_KNOWLEDGE_BASE_ID_PREFIX,
    )

    assert policy.private_knowledge_base_id == "__agent_private__:mind"
    assert policy.private_agent_knowledge_enabled is True


def test_resolve_private_knowledge_base_agent_requires_active_private_knowledge() -> None:
    """Reverse lookup only resolves agents with active private knowledge bindings."""
    seeds = build_agent_policy_seeds(
        {
            "mind": AgentConfig(
                display_name="Mind",
                private={
                    "per": "user",
                    "knowledge": {
                        "enabled": True,
                        "path": "memory",
                    },
                },
            ),
            "assistant": AgentConfig(display_name="Assistant", private={"per": "user"}),
        },
        default_worker_scope=None,
    )

    assert resolve_private_knowledge_base_agent("__agent_private__:mind", seeds) == "mind"
    assert resolve_private_knowledge_base_agent("__agent_private__:assistant", seeds) is None
