"""Tests for the shared Agent Keys permission rule."""

from __future__ import annotations

from typing import TYPE_CHECKING

from mindroom.agent_reply_membership import AgentReplyMembershipIndex
from mindroom.config.main import Config
from mindroom.constants import resolve_runtime_paths
from mindroom.egress_broker.key_access import egress_key_access

if TYPE_CHECKING:
    from pathlib import Path

    from mindroom.constants import RuntimePaths

_ENV = {
    "MINDROOM_SANDBOX_PROXY_URL": "http://sandbox-runner.test",
    "MINDROOM_EGRESS_BROKER_PORT": "8768",
    # A dedicated backend, where a personal agent's worker belongs to one requester.
    "MINDROOM_WORKER_BACKEND": "docker",
}


def _agent(*tools: str, **extra: object) -> dict[str, object]:
    return {
        "display_name": "Agent",
        "tools": list(tools),
        "include_default_tools": False,
        "access": {"users": ["@alice:example.org", "@bob:example.org"]},
        **extra,
    }


def _setup(tmp_path: Path) -> tuple[Config, RuntimePaths]:
    config = Config(
        agents={
            "personal": _agent("shell", "egress_credentials", private={"per": "user_agent"}),
            "shared": _agent(
                "shell",
                "egress_credentials",
                worker_scope="shared",
                credential_managers=["@bob:example.org"],
            ),
            "calculator": _agent("calculator"),
        },
    )
    runtime_paths = resolve_runtime_paths(
        config_path=tmp_path / "config.yaml",
        storage_path=tmp_path,
        process_env=_ENV,
    )
    return config, runtime_paths


def test_personal_agent_requester_can_manage(tmp_path: Path) -> None:
    """A requester who may use a personal agent manages its keys."""
    config, runtime_paths = _setup(tmp_path)

    access = egress_key_access("@alice:example.org", "personal", config, runtime_paths, AgentReplyMembershipIndex())

    assert access == (False, True)


def test_shared_agent_non_manager_cannot_manage(tmp_path: Path) -> None:
    """A user of a shared agent who is not a credential manager may use it but not manage its keys."""
    config, runtime_paths = _setup(tmp_path)

    access = egress_key_access("@alice:example.org", "shared", config, runtime_paths, AgentReplyMembershipIndex())

    assert access == (True, False)


def test_agent_without_broker_scope_is_ineligible(tmp_path: Path) -> None:
    """An agent that does not use Agent Keys is not eligible for anyone."""
    config, runtime_paths = _setup(tmp_path)

    access = egress_key_access("@alice:example.org", "calculator", config, runtime_paths, AgentReplyMembershipIndex())

    assert access is None
