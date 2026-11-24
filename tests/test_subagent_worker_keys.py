"""Worker keys pinned to one subagent or one named worker, and how a pinned worker serves its normal worker's state."""

from __future__ import annotations

from typing import TYPE_CHECKING

import pytest

from mindroom.config.agent import AgentConfig, AgentPrivateConfig
from mindroom.config.main import Config
from mindroom.credentials.manager import CredentialsManager, load_scoped_credentials, sync_shared_credentials_to_worker
from mindroom.tool_system.worker_routing import (
    ToolExecutionIdentity,
    named_worker_key,
    pinned_worker_kind,
    pinned_worker_state_scope_key,
    resolve_unscoped_worker_key,
    resolve_worker_key,
    resolve_worker_target,
    subagent_worker_key,
    visible_workspace_roots,
    worker_key_agent_name,
)

if TYPE_CHECKING:
    from collections.abc import Callable
    from pathlib import Path

    from mindroom.tool_system.worker_routing import WorkerScope

_SUBAGENT_ID = "0123456789abcdef0123456789abcdef"
# Each kind of pinned key, built from a normal key and a 32-hex ID.
_PINS = pytest.mark.parametrize(("pin", "kind"), [(subagent_worker_key, "subagent"), (named_worker_key, "named")])


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


@_PINS
@pytest.mark.parametrize("normal_key", _normal_keys())
def test_pinned_key_round_trips_every_scope(pin: Callable[[str, str], str], kind: str, normal_key: str) -> None:
    """A pinned key keeps its normal key's last part and resolves back to exactly that key and its kind."""
    pinned = pin(normal_key, _SUBAGENT_ID)

    assert pinned != normal_key
    assert pinned.split(":")[-1] == normal_key.rsplit(":", maxsplit=1)[-1]
    assert pinned_worker_state_scope_key(pinned) == normal_key
    assert pinned_worker_kind(pinned) == kind
    assert pinned_worker_state_scope_key(normal_key) is None
    assert pinned_worker_kind(normal_key) is None


def test_a_subagent_and_a_named_worker_with_one_id_have_keys_of_their_own() -> None:
    """The kind is part of the pin, so a name's digest never reaches a subagent's worker."""
    normal_key = _key("user_agent")

    assert subagent_worker_key(normal_key, _SUBAGENT_ID) != named_worker_key(normal_key, _SUBAGENT_ID)


@_PINS
@pytest.mark.parametrize("prefix", ["subagent-", "subagent=", "named-", "named=", "script-"])
@pytest.mark.parametrize("scope", ["user", "user_agent"])
def test_normal_key_with_lookalike_server_is_not_pinned(
    pin: Callable[[str, str], str],
    kind: str,
    prefix: str,
    scope: WorkerScope,
) -> None:
    """A requester whose server name looks like a pin never makes its normal key read as pinned."""
    normal_key = _key(scope, requester_id=f"@a:{prefix}{_SUBAGENT_ID}")

    assert pinned_worker_kind(normal_key) is None
    assert pinned_worker_state_scope_key(normal_key) is None
    assert pinned_worker_state_scope_key(pin(normal_key, _SUBAGENT_ID)) == normal_key
    assert pinned_worker_kind(pin(normal_key, _SUBAGENT_ID)) == kind


@_PINS
@pytest.mark.parametrize(
    ("state_scope_key", "pin_id"),
    [
        ("v1:tenant:shared:code", "ABC"),
        ("v1:tenant:shared:code", _SUBAGENT_ID.upper()),
        ("not-a-key", _SUBAGENT_ID),
        ("v1:tenant:other:code", _SUBAGENT_ID),
        (f"v1:tenant:shared:subagent={_SUBAGENT_ID}:code", _SUBAGENT_ID),
        (f"v1:tenant:shared:named={_SUBAGENT_ID}:code", _SUBAGENT_ID),
    ],
)
def test_pinned_key_rejects_bad_input(
    pin: Callable[[str, str], str],
    kind: str,
    state_scope_key: str,
    pin_id: str,
) -> None:
    """Only a resolved, unpinned key and a 32-hex ID make a pinned key."""
    with pytest.raises(ValueError, match=kind):
        pin(state_scope_key, pin_id)


@_PINS
@pytest.mark.parametrize("normal_key", [_key("shared"), resolve_unscoped_worker_key("code", tenant_id="tenant")])
def test_agent_name_of_pinned_shared_and_unscoped_keys(
    pin: Callable[[str, str], str],
    kind: str,  # noqa: ARG001
    normal_key: str,
) -> None:
    """A pinned shared or unscoped key still names the agent it serves."""
    assert worker_key_agent_name(pin(normal_key, _SUBAGENT_ID)) == "code"


@_PINS
def test_visible_roots_of_pinned_keys_match_normal(
    pin: Callable[[str, str], str],
    kind: str,  # noqa: ARG001
    tmp_path: Path,
) -> None:
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
                pin(normal_key, _SUBAGENT_ID),
                policies,
                private_agent_names=private_agent_names,
            )
            == expected
        )


@_PINS
def test_pinned_runner_reuses_its_store_for_normal_key(
    pin: Callable[[str, str], str],
    kind: str,  # noqa: ARG001
    tmp_path: Path,
) -> None:
    """A pinned worker reads its own store, with the shared credentials mirrored into it, for its normal key."""
    base_manager = CredentialsManager(tmp_path / "credentials")
    target = resolve_worker_target("user", "code", _identity())
    assert target.worker_key is not None
    pinned_manager = base_manager.for_worker(pin(target.worker_key, _SUBAGENT_ID))
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
