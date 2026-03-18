"""Tests for canonical agent-policy derivation."""

from __future__ import annotations

from mindroom.agent_policy import (
    build_agent_policy_seed,
    build_agent_policy_seeds,
    resolve_agent_policy,
    resolve_agent_policy_index,
    resolve_private_knowledge_base_agent,
)
from mindroom.config.agent import AgentConfig
from mindroom.config.main import Config
from mindroom.config.models import DefaultsConfig


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

    policy = resolve_agent_policy(
        build_agent_policy_seed(
            "mind",
            config.agents["mind"],
            default_worker_scope=config.defaults.worker_scope,
        ),
        private_knowledge_base_id_prefix=config.PRIVATE_KNOWLEDGE_BASE_ID_PREFIX,
    )

    assert policy.is_private is True
    assert policy.effective_execution_scope == "user"
    assert policy.scope_label == "private.per=user"
    assert policy.scope_source == "private.per"
    assert policy.dashboard_credentials_supported is False
    assert policy.request_scoped_workspace_enabled is True


def test_resolve_agent_policy_inherits_default_worker_scope_without_private_workspace() -> None:
    """Shared agents inherit defaults.worker_scope without becoming private workspaces."""
    policy = resolve_agent_policy(
        build_agent_policy_seed(
            "general",
            AgentConfig(display_name="General"),
            default_worker_scope="user",
        ),
    )

    assert policy.effective_execution_scope == "user"
    assert policy.scope_label == "worker_scope=user"
    assert policy.scope_source == "defaults.worker_scope"
    assert policy.request_scoped_workspace_enabled is False
    assert policy.request_scoped_knowledge_enabled is False


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
        == "Delegates to private agent 'mind', so it cannot participate in teams yet."
    )
    assert index.policies["mind"].team_eligibility_reason == "Private agents cannot participate in teams yet."


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
        assert index.policies["a"].team_eligibility_reason == "Private agents cannot participate in teams yet."
        assert (
            index.policies["b"].team_eligibility_reason
            == "Delegates to private agent 'a', so it cannot participate in teams yet."
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

    policy = resolve_agent_policy(
        build_agent_policy_seed(
            "mind",
            config.agents["mind"],
            default_worker_scope=config.defaults.worker_scope,
        ),
        private_knowledge_base_id_prefix=config.PRIVATE_KNOWLEDGE_BASE_ID_PREFIX,
    )

    assert policy.private_knowledge_base_id == "__agent_private__:mind"
    assert policy.request_scoped_knowledge_enabled is True


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
