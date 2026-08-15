"""Configuration boundary tests for constrained agent repositories."""

from __future__ import annotations

import pytest
from pydantic import ValidationError

from mindroom.config.main import Config


def _shared_config(**agent_overrides: object) -> dict[str, object]:
    agent: dict[str, object] = {
        "display_name": "Redwood",
        "worker_scope": "shared",
        "tools": ["agent_repository"],
    }
    agent.update(agent_overrides)
    return {
        "agent_repositories": {"organization": "example-org", "prefix": "MindRoom"},
        "agents": {"redwood": agent},
    }


def test_shared_agent_repository_opt_in_uses_global_policy() -> None:
    """A shared agent should receive repository policy only from the global block."""
    config = Config.model_validate(_shared_config())

    assert config.agent_repositories is not None
    assert config.agent_repositories.organization == "example-org"
    assert config.agent_repositories.prefix == "MindRoom"
    assert config._agent_tool_runtime_overrides("redwood", "agent_repository") == {
        "organization": "example-org",
        "prefix": "MindRoom",
    }


def test_repository_policy_rejects_consecutive_organization_hyphens() -> None:
    """Runtime policy must reject organization names GitHub cannot create."""
    data = _shared_config()
    data["agent_repositories"]["organization"] = "example--org"  # type: ignore[index]

    with pytest.raises(ValidationError, match="valid GitHub organization slug"):
        Config.model_validate(data)


def test_repository_policy_is_not_injected_into_an_unassigned_agent() -> None:
    """Global policy must not make the control-plane capability globally loadable."""
    data = _shared_config()
    data["agents"]["other"] = {"display_name": "Other", "worker_scope": "shared"}  # type: ignore[index]
    config = Config.model_validate(data)

    assert config._agent_tool_runtime_overrides("other", "agent_repository") is None


def test_private_user_agent_repository_opt_in_is_valid() -> None:
    """Private repositories should use the canonical private.per=user_agent boundary."""
    config = Config.model_validate(
        {
            "agent_repositories": {"organization": "example-org", "prefix": "MindRoom"},
            "agents": {
                "mind": {
                    "display_name": "Mind",
                    "private": {"per": "user_agent"},
                    "tools": ["agent_repository"],
                },
            },
        },
    )

    assert config.agents["mind"].private is not None
    assert config.agents["mind"].private.per == "user_agent"


@pytest.mark.parametrize(
    ("mutation", "error"),
    [
        (
            lambda data: data.pop("agent_repositories"),
            "requires the global agent_repositories policy",
        ),
        (
            lambda data: data["agent_repositories"].update({"repository_name": "owned"}),  # type: ignore[union-attr]
            "Extra inputs are not permitted",
        ),
        (
            lambda data: data["agent_repositories"].update({"prefix": "Other"}),  # type: ignore[union-attr]
            "Input should be 'MindRoom'",
        ),
        (
            lambda data: data["agents"]["redwood"].update({"worker_scope": "user"}),  # type: ignore[index,union-attr]
            "non-private agents must use worker_scope=shared",
        ),
        (
            lambda data: data["agents"]["redwood"].update({"worker_scope": "user_agent"}),  # type: ignore[index,union-attr]
            "non-private agents must use worker_scope=shared",
        ),
        (
            lambda data: data["agents"]["redwood"].update(  # type: ignore[index,union-attr]
                {"tools": [{"agent_repository": {"organization": "other"}}]},
            ),
            "does not accept per-agent overrides",
        ),
    ],
)
def test_agent_repository_config_rejects_policy_expansion(
    mutation: object,
    error: str,
) -> None:
    """Repository opt-ins must not widen the fixed global policy boundary."""
    data = _shared_config()
    mutation(data)  # type: ignore[operator]

    with pytest.raises(ValidationError, match=error):
        Config.model_validate(data)


def test_private_user_scope_repository_opt_in_is_rejected() -> None:
    """Only private.per=user_agent identifies one private agent repository owner."""
    with pytest.raises(ValidationError, match=r"private agents must use private.per=user_agent"):
        Config.model_validate(
            {
                "agent_repositories": {"organization": "example-org", "prefix": "MindRoom"},
                "agents": {
                    "mind": {
                        "display_name": "Mind",
                        "private": {"per": "user"},
                        "tools": ["agent_repository"],
                    },
                },
            },
        )


def test_default_agent_repository_opt_in_is_rejected() -> None:
    """Repository capability should require an explicit audited agent assignment."""
    with pytest.raises(ValidationError, match="must be assigned directly to an agent"):
        Config.model_validate(
            {
                "agent_repositories": {"organization": "example-org", "prefix": "MindRoom"},
                "defaults": {"tools": ["agent_repository"], "worker_scope": "shared"},
                "agents": {"redwood": {"display_name": "Redwood"}},
            },
        )
