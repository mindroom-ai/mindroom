"""Tests for provisioning a personal agent's Matrix account and roster entry."""

from __future__ import annotations

from copy import deepcopy
from hashlib import sha256
from typing import TYPE_CHECKING, Any
from unittest.mock import AsyncMock, patch

import pytest

from mindroom.config.main import Config
from mindroom.config.personal_agents import append_personal_agent_roster_entry, read_personal_agent_roster
from mindroom.constants import resolve_runtime_paths
from mindroom.matrix.state import MatrixState
from mindroom.matrix.users import AgentMatrixUser
from mindroom.personal_agents import provision_personal_agent
from tests.conftest import test_runtime_paths
from tests.test_personal_agents_config import BASE, _entry

if TYPE_CHECKING:
    from pathlib import Path

    from mindroom.constants import RuntimePaths

_USER_ID = "@Alice.Smith:example.org"
_SUFFIXED_NAME = f"mind_alice_smith_{sha256(_USER_ID.encode()).hexdigest()[:6]}"


def _create_agent_user() -> AsyncMock:
    return AsyncMock(
        side_effect=lambda _homeserver, agent_name, display_name, _runtime_paths, username=None: AgentMatrixUser(
            agent_name=agent_name,
            user_id=f"@{username or agent_name}:localhost",
            display_name=display_name,
            password=None,
        ),
    )


async def _provision(config: Config, runtime_paths: RuntimePaths) -> str:
    return await provision_personal_agent(
        config,
        runtime_paths,
        template="mind",
        user_id=_USER_ID,
        user_display_name="Alice",
        source_room_id="!lobby:example.org",
    )


@pytest.mark.asyncio
async def test_provision_creates_account_then_roster_entry(tmp_path: Path) -> None:
    """The account is created under the generated name and the roster records the user's agent."""
    paths = test_runtime_paths(tmp_path)
    config = Config.validate_with_runtime(deepcopy(BASE), paths)
    create_user = _create_agent_user()

    with patch("mindroom.personal_agents.create_agent_user", new=create_user):
        assert await _provision(config, paths) == "mind_alice_smith"

    create_user.assert_awaited_once_with(
        "http://localhost:8008",
        "mind_alice_smith",
        "Alice's Mind",
        paths,
        username=None,
    )
    [entry] = read_personal_agent_roster(paths)
    assert entry.agent_name == "mind_alice_smith"
    assert entry.user_id == _USER_ID
    assert entry.username == "alice_smith"
    assert entry.matrix_username is None
    assert entry.user_display_name == "Alice"
    assert entry.source_room_id == "!lobby:example.org"


@pytest.mark.asyncio
async def test_provision_is_idempotent(tmp_path: Path) -> None:
    """A user who already has an agent gets the same name back without another registration."""
    paths = test_runtime_paths(tmp_path)
    config = Config.validate_with_runtime(deepcopy(BASE), paths)
    create_user = _create_agent_user()

    with patch("mindroom.personal_agents.create_agent_user", new=create_user):
        first = await _provision(config, paths)
        second = await _provision(config, paths)

    assert first == second == "mind_alice_smith"
    create_user.assert_awaited_once()
    assert len(read_personal_agent_roster(paths)) == 1


@pytest.mark.asyncio
async def test_provision_suffixes_a_clashing_name(tmp_path: Path) -> None:
    """A name another roster entry already holds gets the user ID hash suffix."""
    paths = test_runtime_paths(tmp_path)
    config = Config.validate_with_runtime(deepcopy(BASE), paths)
    append_personal_agent_roster_entry(paths, _entry("@alice_smith:example.org"))
    assert "mind_alice_smith" not in config.agents
    create_user = _create_agent_user()

    with patch("mindroom.personal_agents.create_agent_user", new=create_user):
        assert await _provision(config, paths) == _SUFFIXED_NAME

    assert create_user.await_args.args[1] == _SUFFIXED_NAME
    assert read_personal_agent_roster(paths)[-1].username == _SUFFIXED_NAME.removeprefix("mind_")


@pytest.mark.asyncio
async def test_provision_suffixes_a_taken_matrix_localpart(tmp_path: Path) -> None:
    """A name whose Matrix localpart another managed account already uses gets the hash suffix."""
    paths = test_runtime_paths(tmp_path)
    config = Config.validate_with_runtime(deepcopy(BASE), paths)
    state = MatrixState.load(runtime_paths=paths)
    state.add_account("agent_other", "mindroom_mind_alice_smith", None, domain="localhost")
    state.save(runtime_paths=paths)
    create_user = _create_agent_user()

    with patch("mindroom.personal_agents.create_agent_user", new=create_user):
        assert await _provision(config, paths) == _SUFFIXED_NAME

    assert create_user.await_args.args[1] == _SUFFIXED_NAME


@pytest.mark.asyncio
async def test_provision_fails_when_both_names_are_taken(tmp_path: Path) -> None:
    """A second clash fails that user's onboarding instead of picking another name."""
    paths = test_runtime_paths(tmp_path)
    data: dict[str, Any] = deepcopy(BASE)
    data["agents"] = {"mind_alice_smith": {"display_name": "Authored", "model": "default"}}
    data["teams"] = {_SUFFIXED_NAME: {"display_name": "Team", "role": "Team", "agents": ["mind_alice_smith"]}}
    config = Config.validate_with_runtime(data, paths)
    create_user = _create_agent_user()

    with (
        patch("mindroom.personal_agents.create_agent_user", new=create_user),
        pytest.raises(ValueError, match=f"No free personal agent name for {_USER_ID}"),
    ):
        await _provision(config, paths)

    create_user.assert_not_awaited()
    assert read_personal_agent_roster(paths) == []


@pytest.mark.asyncio
async def test_failed_registration_writes_no_roster_entry(tmp_path: Path) -> None:
    """A failed registration propagates and leaves no roster entry that a reload would load."""
    paths = test_runtime_paths(tmp_path)
    config = Config.validate_with_runtime(deepcopy(BASE), paths)
    create_user = AsyncMock(side_effect=ValueError("registration failed"))

    with (
        patch("mindroom.personal_agents.create_agent_user", new=create_user),
        pytest.raises(ValueError, match="registration failed"),
    ):
        await _provision(config, paths)

    assert read_personal_agent_roster(paths) == []


@pytest.mark.asyncio
async def test_custom_matrix_username_gets_namespace(tmp_path: Path) -> None:
    """A custom Matrix username renders the slug and gets the installation namespace appended."""
    config_path = tmp_path / "config.yaml"
    config_path.write_text("router:\n  model: default\n", encoding="utf-8")
    paths = resolve_runtime_paths(
        config_path=config_path,
        storage_path=tmp_path / "mindroom_data",
        process_env={"MATRIX_HOMESERVER": "http://localhost:8008", "MINDROOM_NAMESPACE": "abcd1234"},
    )
    data: dict[str, Any] = deepcopy(BASE)
    data["personal_agents"]["mind"]["matrix_username"] = "{username}.mind"
    config = Config.validate_with_runtime(data, paths)
    create_user = _create_agent_user()

    with patch("mindroom.personal_agents.create_agent_user", new=create_user):
        assert await _provision(config, paths) == "mind_alice_smith"

    assert create_user.await_args.kwargs == {"username": "alice_smith.mind_abcd1234"}
    assert read_personal_agent_roster(paths)[0].matrix_username == "alice_smith.mind_abcd1234"
