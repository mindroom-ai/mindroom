"""Worker keys pinned to one subagent, and how a pinned worker serves its normal worker's state."""

from __future__ import annotations

from typing import TYPE_CHECKING

import pytest

from mindroom.config.agent import AgentConfig, AgentPrivateConfig
from mindroom.config.main import Config
from mindroom.credentials.manager import CredentialsManager, load_scoped_credentials, sync_shared_credentials_to_worker
from mindroom.tool_system.worker_routing import (
    ToolExecutionIdentity,
    resolve_unscoped_worker_key,
    resolve_worker_key,
    resolve_worker_target,
    subagent_worker_key,
    subagent_worker_state_scope_key,
    visible_workspace_roots,
    worker_key_agent_name,
)

if TYPE_CHECKING:
    from pathlib import Path

    from mindroom.tool_system.worker_routing import WorkerScope

_SUBAGENT_ID = "0123456789abcdef0123456789abcdef"


def _identity(requester_id: str = "@alice:example.org:8448", agent_name: str = "code") -> ToolExecutionIdentity:
    return ToolExecutionIdentity(
        channel="matrix",
        agent_name=agent_name,
        requester_id=requester_id,
        room_id="!room:example.org",
        thread_id=None,
        resolved_thread_id=None,
        session_id="delegate:lead:code:" + _SUBAGENT_ID,
        tenant_id="tenant",
    )


def _key(scope: WorkerScope, requester_id: str = "@alice:example.org:8448") -> str:
    key = resolve_worker_key(scope, _identity(requester_id), agent_name="code")
    assert key is not None
    return key


def _normal_keys() -> list[str]:
    return [
        _key("shared"),
        _key("user"),
        _key("user_agent"),
        resolve_unscoped_worker_key("code", tenant_id="tenant"),
    ]


@pytest.mark.parametrize("normal_key", _normal_keys())
def test_subagent_key_round_trips_every_scope(normal_key: str) -> None:
    """A pinned key keeps its normal key's last part and resolves back to exactly that key."""
    pinned = subagent_worker_key(normal_key, _SUBAGENT_ID)

    assert pinned != normal_key
    assert pinned.split(":")[-1] == normal_key.rsplit(":", maxsplit=1)[-1]
    assert subagent_worker_state_scope_key(pinned) == normal_key
    assert subagent_worker_state_scope_key(normal_key) is None


@pytest.mark.parametrize("prefix", ["subagent-", "subagent=", "script-"])
@pytest.mark.parametrize("scope", ["user", "user_agent"])
def test_normal_key_with_lookalike_server_is_not_pinned(prefix: str, scope: WorkerScope) -> None:
    """A requester whose server name looks like a pin never makes its normal key read as pinned."""
    normal_key = _key(scope, requester_id=f"@a:{prefix}{_SUBAGENT_ID}")

    assert subagent_worker_state_scope_key(normal_key) is None
    assert subagent_worker_state_scope_key(subagent_worker_key(normal_key, _SUBAGENT_ID)) == normal_key


@pytest.mark.parametrize(
    ("state_scope_key", "subagent_id"),
    [
        ("v1:tenant:shared:code", "ABC"),
        ("v1:tenant:shared:code", _SUBAGENT_ID.upper()),
        ("not-a-key", _SUBAGENT_ID),
        ("v1:tenant:other:code", _SUBAGENT_ID),
        (f"v1:tenant:shared:subagent={_SUBAGENT_ID}:code", _SUBAGENT_ID),
    ],
)
def test_subagent_key_rejects_bad_input(state_scope_key: str, subagent_id: str) -> None:
    """Only a resolved, unpinned key and a 32-hex subagent ID make a pinned key."""
    with pytest.raises(ValueError, match="subagent"):
        subagent_worker_key(state_scope_key, subagent_id)


@pytest.mark.parametrize("normal_key", [_key("shared"), resolve_unscoped_worker_key("code", tenant_id="tenant")])
def test_agent_name_of_pinned_shared_and_unscoped_keys(normal_key: str) -> None:
    """A pinned shared or unscoped key still names the agent it serves."""
    assert worker_key_agent_name(subagent_worker_key(normal_key, _SUBAGENT_ID)) == "code"


def test_visible_roots_of_pinned_keys_match_normal(tmp_path: Path) -> None:
    """A pinned worker sees exactly the workspaces of its normal worker, private ones included."""
    policies = Config(
        agents={
            "code": AgentConfig(display_name="Code", private=AgentPrivateConfig(per="user_agent")),
            "notes": AgentConfig(display_name="Notes", private=AgentPrivateConfig(per="user")),
            "helper": AgentConfig(display_name="Helper", worker_scope="user"),
            "shared": AgentConfig(display_name="Shared", worker_scope="shared"),
        },
    ).get_agent_policies()
    cases = [
        (_key("user_agent"), frozenset({"code"})),
        (_key("user"), frozenset()),
        ("v1:tenant:shared:shared", frozenset()),
    ]
    for normal_key, private_agent_names in cases:
        expected = visible_workspace_roots(tmp_path, normal_key, policies, private_agent_names=private_agent_names)
        assert expected
        assert (
            visible_workspace_roots(
                tmp_path,
                subagent_worker_key(normal_key, _SUBAGENT_ID),
                policies,
                private_agent_names=private_agent_names,
            )
            == expected
        )


def test_pinned_runner_reuses_its_store_for_normal_key(tmp_path: Path) -> None:
    """A pinned worker reads its own store, with the shared credentials mirrored into it, for its normal key."""
    base_manager = CredentialsManager(tmp_path / "credentials")
    target = resolve_worker_target("user", "code", _identity())
    assert target.worker_key is not None
    pinned_manager = base_manager.for_worker(subagent_worker_key(target.worker_key, _SUBAGENT_ID))
    base_manager.save_credentials("openweather", {"api_key": "shared-key", "_source": "ui", "base": "yes"})
    sync_shared_credentials_to_worker(
        pinned_manager.current_worker_key or "",
        allowed_services=frozenset({"openweather"}),
        credentials_manager=base_manager,
    )
    pinned_manager.save_credentials("openweather", {"api_key": "pinned-key", "_source": "ui"})

    loaded = load_scoped_credentials("openweather", credentials_manager=pinned_manager, worker_target=target)

    assert loaded == {"api_key": "pinned-key", "_source": "ui", "base": "yes"}


def test_pinned_runner_does_not_reuse_its_store_for_another_key(tmp_path: Path) -> None:
    """A pinned worker's store answers only for its own normal key."""
    base_manager = CredentialsManager(tmp_path / "credentials")
    target = resolve_worker_target("user", "code", _identity())
    other = resolve_worker_target("user", "code", _identity(requester_id="@bob:example.org"))
    assert target.worker_key is not None
    pinned_manager = base_manager.for_worker(subagent_worker_key(target.worker_key, _SUBAGENT_ID))
    pinned_manager.save_credentials("openweather", {"api_key": "pinned-key", "_source": "ui"})

    assert load_scoped_credentials("openweather", credentials_manager=pinned_manager, worker_target=other) is None
