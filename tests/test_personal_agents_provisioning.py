"""Tests for provisioning a personal agent's Matrix account and roster entry."""

from __future__ import annotations

import asyncio
import threading
from copy import deepcopy
from hashlib import sha256
from typing import TYPE_CHECKING, Any
from unittest.mock import AsyncMock, patch

import pytest

from mindroom.config.main import Config
from mindroom.config.personal_agents import personal_agent_roster_lock_path, read_personal_agent_roster
from mindroom.constants import resolve_runtime_paths
from mindroom.file_locks import file_lock_is_held
from mindroom.matrix.identity import managed_account_key
from mindroom.matrix.state import MatrixState
from mindroom.matrix.users import AgentMatrixUser
from mindroom.personal_agents import (
    _append_personal_agent_roster_entry,
    _settle_roster_entry,
    provision_personal_agent,
)
from tests.conftest import test_runtime_paths
from tests.matrix_state_helpers import save_matrix_state
from tests.test_personal_agents_config import BASE, _entry

if TYPE_CHECKING:
    from pathlib import Path

    from mindroom.constants import RuntimePaths

_USER_ID = "@Alice.Smith:localhost"
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


def _hashed(slug: str, user_id: str) -> str:
    return f"mind_{slug}_{sha256(user_id.encode()).hexdigest()[:6]}"


async def _provision(
    config: Config,
    runtime_paths: RuntimePaths,
    user_id: str = _USER_ID,
    source_user_id: str | None = None,
) -> str:
    name, _changed = await provision_personal_agent(
        config,
        runtime_paths,
        template="mind",
        user_id=user_id,
        user_display_name="Alice",
        source_user_id=source_user_id or user_id,
        source_room_id="!lobby:example.org",
    )
    return name


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
    assert entry.source_user_id is None
    assert not entry.pending


@pytest.mark.asyncio
async def test_provision_records_the_alias_that_onboarded_the_owner(tmp_path: Path) -> None:
    """The roster keeps the bridge alias whose join onboarded the owner, so recovery after a restart retries it."""
    paths = test_runtime_paths(tmp_path)
    config = Config.validate_with_runtime(deepcopy(BASE), paths)

    with patch("mindroom.personal_agents.create_agent_user", new=_create_agent_user()):
        await _provision(config, paths, source_user_id="@bridge_alice:localhost")

    assert read_personal_agent_roster(paths)[0].source_user_id == "@bridge_alice:localhost"


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
    _append_personal_agent_roster_entry(paths, _entry("@alice_smith:example.org"))
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
    save_matrix_state(state, paths)
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
async def test_failed_registration_leaves_only_a_pending_entry(tmp_path: Path) -> None:
    """A failed registration propagates and leaves a pending entry, which a reload does not load."""
    paths = test_runtime_paths(tmp_path)
    config = Config.validate_with_runtime(deepcopy(BASE), paths)
    create_user = AsyncMock(side_effect=ValueError("registration failed"))

    with (
        patch("mindroom.personal_agents.create_agent_user", new=create_user),
        pytest.raises(ValueError, match="registration failed"),
    ):
        await _provision(config, paths)

    assert [entry.pending for entry in read_personal_agent_roster(paths)] == [True]
    assert "mind_alice_smith" not in Config.validate_with_runtime(deepcopy(BASE), paths).agents


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


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("user_id", "reason"),
    [
        (
            "@ali*ce:evil.org",
            "cannot own a personal agent: credential_managers: "
            "credential_managers must contain concrete Matrix user IDs: @ali*ce:evil.org",
        ),
        (
            "@a b:evil.org",
            "cannot own a personal agent: credential_managers: "
            "credential_managers must contain concrete Matrix user IDs: @a b:evil.org",
        ),
        ("@a[b]c:evil.org", "cannot own an agent: brackets in a Matrix ID are not supported"),
    ],
)
async def test_unsupported_owner_id_gets_no_account_or_roster_entry(tmp_path: Path, user_id: str, reason: str) -> None:
    """An ID that would break the generated agent's access fails only that user's onboarding, before registration."""
    paths = test_runtime_paths(tmp_path)
    config = Config.validate_with_runtime(deepcopy(BASE), paths)
    create_user = _create_agent_user()

    with (
        patch("mindroom.personal_agents.create_agent_user", new=create_user),
        pytest.raises(ValueError, match=r"cannot own (a personal|an) agent") as raised,
    ):
        await _provision(config, paths, user_id)

    assert str(raised.value) == f"{user_id} {reason}"

    create_user.assert_not_awaited()
    assert read_personal_agent_roster(paths) == []


@pytest.mark.asyncio
async def test_provision_does_not_adopt_a_leftover_account(tmp_path: Path) -> None:
    """An account already stored under the plain agent name, such as a removed agent's, counts as taken."""
    paths = test_runtime_paths(tmp_path)
    config = Config.validate_with_runtime(deepcopy(BASE), paths)
    state = MatrixState.load(runtime_paths=paths)
    state.add_account("agent_mind_alice_smith", "mindroom_mind_alice_smith", None, domain="localhost")
    save_matrix_state(state, paths)
    create_user = _create_agent_user()

    with patch("mindroom.personal_agents.create_agent_user", new=create_user):
        assert await _provision(config, paths) == _SUFFIXED_NAME

    assert create_user.await_args.args[1] == _SUFFIXED_NAME


@pytest.mark.asyncio
async def test_provision_refuses_a_mindroom_account(tmp_path: Path) -> None:
    """A MindRoom account the live config does not know yet never gets an agent of its own."""
    paths = test_runtime_paths(tmp_path)
    config = Config.validate_with_runtime(deepcopy(BASE), paths)
    state = MatrixState.load(runtime_paths=paths)
    state.add_account("agent_mind_bob", "mindroom_mind_bob", None, domain="localhost")
    save_matrix_state(state, paths)
    create_user = _create_agent_user()

    with (
        patch("mindroom.personal_agents.create_agent_user", new=create_user),
        pytest.raises(ValueError, match="MindRoom account"),
    ):
        await _provision(config, paths, "@mindroom_mind_bob:localhost")

    create_user.assert_not_awaited()


@pytest.mark.asyncio
async def test_remote_user_always_gets_the_hashed_name(tmp_path: Path) -> None:
    """A user on another server gets the hashed name even when the plain one is free."""
    paths = test_runtime_paths(tmp_path)
    config = Config.validate_with_runtime(deepcopy(BASE), paths)
    create_user = _create_agent_user()

    with patch("mindroom.personal_agents.create_agent_user", new=create_user):
        assert await _provision(config, paths, "@alice:example.org") == _hashed("alice", "@alice:example.org")


@pytest.mark.asyncio
@pytest.mark.parametrize("section", ["agents", "teams", "personal_agents"])
async def test_provision_treats_saved_but_unloaded_names_as_taken(tmp_path: Path, section: str) -> None:
    """An agent, team, or template saved to the config file but not reloaded yet keeps its name from a new user's agent."""
    paths = test_runtime_paths(tmp_path)
    config = Config.validate_with_runtime(deepcopy(BASE), paths)
    paths.config_path.write_text(f"{section}:\n  mind_alice_smith:\n    display_name: Saved\n", encoding="utf-8")

    with patch("mindroom.personal_agents.create_agent_user", new=_create_agent_user()):
        assert await _provision(config, paths) == _SUFFIXED_NAME


@pytest.mark.asyncio
async def test_interrupted_provisioning_resumes_with_its_own_account(tmp_path: Path) -> None:
    """A registration interrupted after its account was saved resumes under the same names instead of finding them taken."""
    paths = test_runtime_paths(tmp_path)
    config = Config.validate_with_runtime(deepcopy(BASE), paths)
    user_id = "@alice:example.org"  # A remote user has only the hashed name.
    registered = _create_agent_user().side_effect
    attempts = 0

    def register(
        homeserver: str,
        agent_name: str,
        display_name: str,
        runtime_paths: RuntimePaths,
        username: str | None = None,
    ) -> AgentMatrixUser:
        nonlocal attempts
        attempts += 1
        state = MatrixState.load(runtime_paths=runtime_paths)
        state.add_account(
            managed_account_key(agent_name),
            username or f"mindroom_{agent_name}",
            None,
            domain="localhost",
        )
        save_matrix_state(state, runtime_paths)
        if attempts == 1:
            msg = "process died after saving the account"
            raise RuntimeError(msg)
        return registered(homeserver, agent_name, display_name, runtime_paths, username=username)

    with patch("mindroom.personal_agents.create_agent_user", new=AsyncMock(side_effect=register)):
        with pytest.raises(RuntimeError, match="process died"):
            await _provision(config, paths, user_id)
        assert _hashed("alice", user_id) not in Config.validate_with_runtime(deepcopy(BASE), paths).agents
        assert await _provision(config, paths, user_id) == _hashed("alice", user_id)

    [entry] = read_personal_agent_roster(paths)
    assert entry.agent_name == _hashed("alice", user_id)
    assert not entry.pending
    assert _hashed("alice", user_id) in Config.validate_with_runtime(deepcopy(BASE), paths).agents


@pytest.mark.asyncio
async def test_failed_registration_without_an_account_retries_under_the_current_template(tmp_path: Path) -> None:
    """A registration that failed before saving an account picks its names again, so a fixed pattern reaches it."""
    paths = test_runtime_paths(tmp_path)
    data: dict[str, Any] = deepcopy(BASE)
    data["personal_agents"]["mind"]["matrix_username"] = "{username}.outside"
    with (
        patch("mindroom.personal_agents.create_agent_user", new=AsyncMock(side_effect=RuntimeError("M_EXCLUSIVE"))),
        pytest.raises(RuntimeError, match="M_EXCLUSIVE"),
    ):
        await _provision(Config.validate_with_runtime(data, paths), paths)

    data["personal_agents"]["mind"]["matrix_username"] = "{username}.mind"
    create_user = _create_agent_user()
    with patch("mindroom.personal_agents.create_agent_user", new=create_user):
        assert await _provision(Config.validate_with_runtime(data, paths), paths) == "mind_alice_smith"

    assert create_user.await_args.kwargs == {"username": "alice_smith.mind"}
    assert [(entry.matrix_username, entry.pending) for entry in read_personal_agent_roster(paths)] == [
        ("alice_smith.mind", False),
    ]


@pytest.mark.asyncio
async def test_registration_runs_outside_the_roster_lock(tmp_path: Path) -> None:
    """The roster lock is free while the account registers, so config saves never wait on the homeserver."""
    paths = test_runtime_paths(tmp_path)
    config = Config.validate_with_runtime(deepcopy(BASE), paths)
    registered = _create_agent_user().side_effect
    lock_held: list[bool] = []

    def register(*args: Any, **kwargs: Any) -> AgentMatrixUser:  # noqa: ANN401
        lock_held.append(file_lock_is_held(personal_agent_roster_lock_path(paths)))
        return registered(*args, **kwargs)

    with patch("mindroom.personal_agents.create_agent_user", new=AsyncMock(side_effect=register)):
        await _provision(config, paths)

    assert lock_held == [False]


@pytest.mark.asyncio
async def test_concurrent_same_slug_users_get_distinct_names(tmp_path: Path) -> None:
    """Two users with the same slug onboarding at once get distinct agents, accounts, and roster entries."""
    paths = test_runtime_paths(tmp_path)
    config = Config.validate_with_runtime(deepcopy(BASE), paths)
    create_user = _create_agent_user()
    registered = create_user.side_effect

    async def slow_registration(*args: object, **kwargs: object) -> AgentMatrixUser:
        await asyncio.sleep(0.05)
        return registered(*args, **kwargs)

    create_user.side_effect = slow_registration
    with patch("mindroom.personal_agents.create_agent_user", new=create_user):
        names = await asyncio.gather(
            _provision(config, paths, "@alice:localhost"),
            _provision(config, paths, "@ALICE:localhost"),
        )

    assert len(set(names)) == 2
    assert "mind_alice" in names
    assert sorted(call.args[1] for call in create_user.await_args_list) == sorted(names)
    assert sorted(entry.agent_name for entry in read_personal_agent_roster(paths)) == sorted(names)


@pytest.mark.asyncio
async def test_cancelled_provisioning_finishes_its_roster_write_under_the_lock(tmp_path: Path) -> None:
    """Cancelling provisioning mid-write waits for the write, so no other provisioning reads a stale roster."""
    paths = test_runtime_paths(tmp_path)
    config = Config.validate_with_runtime(deepcopy(BASE), paths)
    writing, release = threading.Event(), threading.Event()

    def slow_activate(*args: Any) -> None:  # noqa: ANN401
        writing.set()
        release.wait(5)
        _settle_roster_entry(*args)

    with (
        patch("mindroom.personal_agents.create_agent_user", new=_create_agent_user()),
        patch("mindroom.personal_agents._settle_roster_entry", new=slow_activate),
    ):
        task = asyncio.create_task(_provision(config, paths))
        try:
            assert await asyncio.to_thread(writing.wait, 5)
            task.cancel()
            await asyncio.sleep(0.05)
            assert not task.done()
        finally:
            release.set()
        with pytest.raises(asyncio.CancelledError):
            await task

    assert [(entry.agent_name, entry.pending) for entry in read_personal_agent_roster(paths)] == [
        ("mind_alice_smith", False),
    ]
