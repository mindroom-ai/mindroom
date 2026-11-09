"""A plugin stores an agent's worker target and loads it back after a restart, never with another worker in it."""

from __future__ import annotations

import json
from dataclasses import replace
from typing import TYPE_CHECKING, Any

import pytest

from mindroom.config.agent import AgentConfig, AgentPrivateConfig
from mindroom.config.main import Config
from mindroom.constants import resolve_runtime_paths
from mindroom.plugin_api import ResolvedWorkerTarget, dump_worker_target, load_worker_target
from mindroom.runtime_resolution import resolve_agent_execution
from mindroom.tool_system.worker_routing import build_tool_execution_identity
from tests.conftest import bind_runtime_paths

if TYPE_CHECKING:
    from collections.abc import Callable
    from pathlib import Path


def _target(tmp_path: Path, agent: AgentConfig) -> ResolvedWorkerTarget:
    """The target one of Alice's turns of ``agent`` in a room resolves, under a tenant and an account."""
    paths = resolve_runtime_paths(
        config_path=tmp_path / "config.yaml",
        storage_path=tmp_path / "storage",
        process_env={"CUSTOMER_ID": "tenant-1", "ACCOUNT_ID": "account-1"},
    )
    config = bind_runtime_paths(Config(agents={"coder": agent}), paths)
    identity = build_tool_execution_identity(
        channel="matrix",
        agent_name="coder",
        runtime_paths=paths,
        requester_id="@alice:localhost",
        room_id="!room:localhost",
        thread_id="$thread",
        resolved_thread_id="$thread",
        session_id="session-1",
    )
    return resolve_agent_execution("coder", config, paths, identity).worker_target


_AGENTS = {
    "shared": AgentConfig(display_name="Coder", worker_scope="shared"),
    "private per user": AgentConfig(display_name="Coder", private=AgentPrivateConfig(per="user")),
    "private per user and agent": AgentConfig(display_name="Coder", private=AgentPrivateConfig(per="user_agent")),
    "unscoped": AgentConfig(display_name="Coder"),
}


@pytest.mark.parametrize("agent", _AGENTS.values(), ids=_AGENTS.keys())
def test_a_dumped_target_loads_back_as_the_same_target_after_passing_through_json(
    tmp_path: Path,
    agent: AgentConfig,
) -> None:
    """Every field survives a JSON round trip, the identity's included."""
    target = _target(tmp_path, agent)

    stored = json.loads(json.dumps(dump_worker_target(target)))

    assert load_worker_target(stored) == target
    assert stored["version"] == 1


def test_a_private_target_keeps_its_requester_s_worker(tmp_path: Path) -> None:
    """A loaded private target still names its requester and the agents visible in that worker."""
    target = _target(tmp_path, _AGENTS["private per user and agent"])
    assert target.worker_key is not None
    assert "@alice:localhost" in target.worker_key

    loaded = load_worker_target(json.loads(json.dumps(dump_worker_target(target))))

    assert (loaded.worker_key, loaded.private_agent_names) == (target.worker_key, frozenset({"coder"}))
    assert loaded.execution_identity == target.execution_identity


def _stored(tmp_path: Path) -> dict[str, object]:
    return json.loads(json.dumps(dump_worker_target(_target(tmp_path, _AGENTS["private per user and agent"]))))


def _identity(stored: dict[str, object], **changes: object) -> dict[str, object]:
    identity = stored["execution_identity"]
    assert isinstance(identity, dict)
    return {**stored, "execution_identity": {**identity, **changes}}


_MALFORMED: dict[str, Callable[[dict[str, Any]], object]] = {
    "not an object": lambda _: [],
    "a future version": lambda stored: {**stored, "version": 2},
    "no version": lambda stored: {key: value for key, value in stored.items() if key != "version"},
    "an unknown field": lambda stored: {**stored, "extra": None},
    "a missing field": lambda stored: {key: value for key, value in stored.items() if key != "worker_key"},
    "an unknown scope": lambda stored: {**stored, "worker_scope": "room"},
    "a number for a name": lambda stored: {**stored, "routing_agent_name": 1},
    "a string for the private agents": lambda stored: {**stored, "private_agent_names": "coder"},
    "a number among the private agents": lambda stored: {**stored, "private_agent_names": [1]},
    "an identity that is not an object": lambda stored: {**stored, "execution_identity": "alice"},
    "an unknown identity channel": lambda stored: _identity(stored, channel="email"),
    "an unknown identity field": lambda stored: _identity(stored, extra=None),
    # A worker key that is not the one its scope, agent, and identity resolve to, here another person's worker.
    "another person's worker key": lambda stored: {
        **stored,
        "worker_key": str(stored["worker_key"]).replace("@alice", "@mallory"),
    },
    "another person's identity": lambda stored: _identity(stored, requester_id="@mallory:localhost"),
    "another agent": lambda stored: {**stored, "routing_agent_name": "other"},
    "another agent's visibility": lambda stored: {**stored, "private_agent_names": ["coder", "other"]},
}


@pytest.mark.parametrize("change", _MALFORMED.values(), ids=_MALFORMED.keys())
def test_a_target_that_is_malformed_or_names_another_worker_raises_value_error(
    tmp_path: Path,
    change: Callable[[dict[str, Any]], object],
) -> None:
    """Load refuses anything it does not know and any target whose worker its own fields do not resolve to."""
    with pytest.raises(ValueError, match="worker target"):
        load_worker_target(change(_stored(tmp_path)))


def test_a_target_with_no_identity_round_trips(tmp_path: Path) -> None:
    """A target that resolves no worker, here an isolating scope without a requester, loads back as it was."""
    target = replace(_target(tmp_path, _AGENTS["unscoped"]), execution_identity=None, tenant_id=None, account_id=None)

    assert load_worker_target(json.loads(json.dumps(dump_worker_target(target)))) == target
