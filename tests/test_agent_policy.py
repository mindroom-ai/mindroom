"""Tests for canonical agent-policy derivation."""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

import pytest

from mindroom.agent_policy import (
    PolicyInputs,
    build_agent_policy_seeds,
    resolve_agent_policy,
    resolve_agent_policy_index,
)
from mindroom.config.agent import AgentConfig, AgentPrivateConfig
from mindroom.config.main import Config
from mindroom.config.models import DefaultsConfig
from mindroom.constants import resolve_runtime_paths
from mindroom.runtime_resolution import resolve_agent_execution
from mindroom.tool_system.worker_routing import (
    ToolExecutionIdentity,
    agent_workspace_root_path,
    build_agent_toolkit_worker_target,
    private_instance_scope_root_path,
    resolve_worker_key,
    visible_workspace_roots,
)
from mindroom.workers.backends._dedicated_worker_common import resolved_agent_policies_from_config_data

if TYPE_CHECKING:
    from pathlib import Path

    from mindroom.tool_system.worker_routing import WorkerScope


def _typed_inputs(agents: dict[str, AgentConfig]) -> dict[str, PolicyInputs]:
    return {name: PolicyInputs.from_agent(agent) for name, agent in agents.items()}


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
        "mind",
        PolicyInputs.from_agent(config.agents["mind"]),
        default_worker_scope=config.defaults.worker_scope,
    )

    assert policy.is_private is True
    assert policy.effective_execution_scope == "user"
    assert policy.scope_label == "private.per=user"
    assert policy.scope_source == "private.per"
    assert policy.dashboard_credentials_supported is False
    assert policy.private_workspace_enabled is True


def test_resolve_agent_policy_inherits_default_worker_scope_without_private_workspace() -> None:
    """Shared agents inherit defaults.worker_scope without becoming private workspaces."""
    policy = resolve_agent_policy(
        "general",
        PolicyInputs.from_agent(AgentConfig(display_name="General")),
        default_worker_scope="user",
    )

    assert policy.effective_execution_scope == "user"
    assert policy.scope_label == "worker_scope=user"
    assert policy.scope_source == "defaults.worker_scope"
    assert policy.private_workspace_enabled is False
    assert policy.private_agent_knowledge_enabled is False


def test_policy_private_root_is_shared_between_typed_draft_and_backend_config() -> None:
    """Backends read the config file, the dashboard sends drafts, and the primary reads typed config: same root."""
    raw_agents: dict[str, Any] = {
        "mind": {"display_name": "Mind", "private": {"per": "user_agent", "root": " workspace/mind "}},
        "notes": {"display_name": "Notes", "private": {"per": "user"}},
        "helper": {"display_name": "Helper", "worker_scope": "user"},
    }
    typed = resolve_agent_policy_index(
        build_agent_policy_seeds(
            _typed_inputs({name: AgentConfig.model_validate(data) for name, data in raw_agents.items()}),
            default_worker_scope=None,
        ),
    ).policies
    draft = resolve_agent_policy_index(
        build_agent_policy_seeds(
            {name: PolicyInputs.model_validate(data) for name, data in raw_agents.items()},
            default_worker_scope=None,
        ),
    ).policies
    backend = resolved_agent_policies_from_config_data({"agents": raw_agents})

    for policies in (typed, draft, backend):
        assert {name: policy.private_root for name, policy in policies.items()} == {
            "mind": "workspace/mind",
            "notes": None,
            "helper": None,
        }
    assert typed == draft == backend


@pytest.mark.parametrize(
    ("inputs", "default_worker_scope", "expected_scope", "expected_source"),
    [
        ({"private": {"per": "user"}, "worker_scope": "shared"}, "user_agent", "user", "private.per"),
        ({"private": {}, "worker_scope": "user_agent"}, "user", "user_agent", "agent.worker_scope"),
        ({"private": {"per": None}}, "user", "user", "defaults.worker_scope"),
        ({"private": {"root": "notes"}}, None, None, "unscoped"),
    ],
)
def test_private_without_per_falls_through_to_worker_scope_then_defaults(
    inputs: dict[str, Any],
    default_worker_scope: WorkerScope | None,
    expected_scope: WorkerScope | None,
    expected_source: str,
) -> None:
    """A private block without ``per`` keeps the agent private but lets its other scopes decide where it runs."""
    policy = resolve_agent_policy(
        "mind",
        PolicyInputs.model_validate(inputs),
        default_worker_scope=default_worker_scope,
    )

    assert policy.is_private is True
    assert policy.effective_execution_scope == expected_scope
    assert policy.scope_source == expected_source
    assert policy.private_workspace_enabled is (expected_scope not in {None, "shared"})


@pytest.mark.parametrize(
    ("enabled", "path", "expected"),
    [
        (None, "memory", True),
        (True, "memory", True),
        (False, "memory", False),
        (None, None, False),
        (True, None, False),
        (False, None, False),
    ],
)
def test_private_knowledge_is_on_with_a_path_unless_disabled(
    enabled: bool | None,
    path: str | None,
    expected: bool,
) -> None:
    """Private knowledge needs a path, and only an explicit ``enabled: false`` turns it off."""
    policy = resolve_agent_policy(
        "mind",
        PolicyInputs.model_validate({"private": {"per": "user", "knowledge": {"enabled": enabled, "path": path}}}),
        default_worker_scope=None,
    )

    assert policy.private_knowledge_base_id == ("__agent_private__:mind" if expected else None)
    assert policy.private_agent_knowledge_enabled is expected


def test_typed_agent_inputs_match_its_authored_policy_fields() -> None:
    """``PolicyInputs.from_agent`` carries every field the policy reads from a validated agent."""
    private_agent = AgentConfig(
        display_name="Mind",
        delegate_to=["helper"],
        private={"per": "user_agent", "root": "notes", "knowledge": {"enabled": False, "path": "kb"}},
    )
    scoped_agent = AgentConfig(display_name="Helper", worker_scope="shared")

    assert PolicyInputs.from_agent(private_agent) == PolicyInputs.model_validate(
        {
            "delegate_to": ["helper"],
            "private": {"per": "user_agent", "root": "notes", "knowledge": {"enabled": False, "path": "kb"}},
        },
    )
    assert PolicyInputs.from_agent(scoped_agent) == PolicyInputs(worker_scope="shared")
    assert PolicyInputs.from_agent(AgentConfig(display_name="Helper")) == PolicyInputs()


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
        _typed_inputs(
            {
                "helper": AgentConfig(display_name="Helper"),
                "leader": AgentConfig(display_name="Leader", delegate_to=["mind"]),
                "mind": AgentConfig(display_name="Mind", private={"per": "user"}),
            },
        ),
        default_worker_scope=None,
    )

    index = resolve_agent_policy_index(seeds)

    assert index.team_eligibility_reasons == {
        "helper": None,
        "leader": "Delegates to private agent 'mind', so it cannot participate in teams.",
        "mind": "Private agents cannot be configured as team members.",
    }


def test_resolve_agent_policy_index_is_order_independent_for_cycles() -> None:
    """Cyclic delegation should resolve the same private reachability for every query order."""
    agent_items = [
        ("a", AgentConfig(display_name="A", private={"per": "user"}, delegate_to=["b"])),
        ("b", AgentConfig(display_name="B", delegate_to=["a"])),
    ]

    forward_index = resolve_agent_policy_index(
        build_agent_policy_seeds(_typed_inputs(dict(agent_items)), default_worker_scope=None),
    )
    reverse_index = resolve_agent_policy_index(
        build_agent_policy_seeds(_typed_inputs(dict(reversed(agent_items))), default_worker_scope=None),
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
        assert index.team_eligibility_reasons == {
            "a": "Private agents cannot be configured as team members.",
            "b": "Delegates to private agent 'a', so it cannot participate in teams.",
        }


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

    policy = config.resolve_agent("mind").policy

    assert policy.private_knowledge_base_id == "__agent_private__:mind"
    assert policy.private_agent_knowledge_enabled is True


def test_private_knowledge_base_lookup_requires_active_private_knowledge() -> None:
    """Reverse lookup resolves only a private agent whose private knowledge is on."""
    config = Config(
        agents={
            "mind": AgentConfig(
                display_name="Mind",
                private={"per": "user", "knowledge": {"enabled": True, "path": "memory"}},
            ),
            "assistant": AgentConfig(display_name="Assistant", private={"per": "user"}),
            "helper": AgentConfig(display_name="Helper"),
        },
    )

    assert config.get_private_knowledge_base_agent("__agent_private__:mind") == "mind"
    assert config.get_private_knowledge_base_agent("mind") is None
    assert config.get_private_knowledge_base_agent("other_prefix:mind") is None
    assert config.get_private_knowledge_base_agent("__agent_private__:") is None
    assert config.get_private_knowledge_base_agent("__agent_private__:ghost") is None
    assert config.get_private_knowledge_base_agent("__agent_private__:helper") is None
    assert config.get_private_knowledge_base_agent("__agent_private__:assistant") is None


def _identity(requester_id: str = "@alice:example.org") -> ToolExecutionIdentity:
    return ToolExecutionIdentity(
        channel="matrix",
        agent_name="general",
        requester_id=requester_id,
        room_id="!room:example.org",
        thread_id="$thread",
        resolved_thread_id="$thread",
        session_id="s1",
    )


@pytest.mark.parametrize(
    ("agent_settings", "identity", "process_env", "expected_worker_key"),
    [
        pytest.param(
            {"worker_scope": "shared"},
            _identity(),
            {},
            resolve_worker_key("shared", _identity(), agent_name="general"),
            id="shared",
        ),
        pytest.param(
            {"worker_scope": "user"},
            _identity(),
            {"CUSTOMER_ID": "tenant-123"},
            resolve_worker_key("user", _identity(), agent_name="general"),
            id="user",
        ),
        pytest.param(
            {"worker_scope": "user_agent"},
            _identity(),
            {},
            resolve_worker_key("user_agent", _identity(), agent_name="general"),
            id="user_agent",
        ),
        pytest.param(
            {"private": {"per": "user_agent"}},
            _identity(),
            {},
            resolve_worker_key("user_agent", _identity(), agent_name="general"),
            id="private",
        ),
        pytest.param(
            {"worker_scope": "shared"},
            None,
            {"CUSTOMER_ID": "tenant-123"},
            "v1:tenant-123:shared:general",
            id="shared_tenant_without_identity",
        ),
        pytest.param({}, None, {"CUSTOMER_ID": "tenant-123"}, None, id="unscoped"),
    ],
)
def test_agent_execution_carries_the_agent_worker_target(
    tmp_path: Path,
    agent_settings: dict[str, Any],
    identity: ToolExecutionIdentity | None,
    process_env: dict[str, str],
    expected_worker_key: str | None,
) -> None:
    """The execution's worker key and identity are those of the target the agent's toolkits and operations use."""
    runtime_paths = resolve_runtime_paths(
        config_path=tmp_path / "config.yaml",
        storage_path=tmp_path,
        process_env=process_env,
    )
    config = Config.validate_with_runtime(
        {
            "models": {"default": {"provider": "ollama", "id": "test-model"}},
            "agents": {"general": {"display_name": "General", **agent_settings}},
        },
        runtime_paths,
    )
    policy = config.resolve_agent("general").policy

    execution = resolve_agent_execution("general", config, runtime_paths, identity)

    assert execution.worker_target == build_agent_toolkit_worker_target(
        policy.effective_execution_scope,
        "general",
        is_private=policy.is_private,
        execution_identity=identity,
        runtime_paths=runtime_paths,
    )
    assert execution.worker_key == expected_worker_key
    assert execution.worker_target.worker_key == execution.worker_key
    assert execution.execution_scope == policy.effective_execution_scope
    if identity is not None:
        assert execution.execution_identity is identity
