"""Managed Matrix account preparation, tested without an orchestrator."""

from __future__ import annotations

from copy import deepcopy
from typing import TYPE_CHECKING
from unittest.mock import AsyncMock, patch

import pytest

from mindroom.config.main import Config
from mindroom.constants import ROUTER_AGENT_NAME
from mindroom.matrix.state import MatrixState
from mindroom.matrix.users import AgentMatrixUser
from mindroom.orchestration.accounts import prepare_managed_accounts
from mindroom.personal_agents import _append_personal_agent_roster_entry
from mindroom.startup_errors import PermanentStartupError
from tests.conftest import TEST_PASSWORD, bind_runtime_paths, runtime_paths_for, test_runtime_paths
from tests.matrix_state_helpers import save_matrix_state
from tests.test_personal_agents_config import BASE as PERSONAL_AGENTS_BASE
from tests.test_personal_agents_config import _entry as personal_agent_entry

if TYPE_CHECKING:
    from pathlib import Path

    from mindroom.constants import RuntimePaths


def _general_config(tmp_path: Path, **sections: object) -> Config:
    """Return a config with the `general` agent, bound to a runtime under ``tmp_path`` whose accounts are persisted."""
    return bind_runtime_paths(
        Config(
            agents={"general": {"display_name": "GeneralAgent", "model": "default"}},
            models={"default": {"provider": "openai", "id": "test-model"}},
            **sections,
        ),
        test_runtime_paths(tmp_path),
    )


def _matrix_user(entity_name: str, localpart: str, display_name: str) -> AgentMatrixUser:
    return AgentMatrixUser(
        agent_name=entity_name,
        user_id=f"@{localpart}:localhost",
        display_name=display_name,
        password=TEST_PASSWORD,
    )


def _persist_account(paths: RuntimePaths, entity_name: str, localpart: str) -> None:
    state = MatrixState.load(runtime_paths=paths)
    state.add_account(f"agent_{entity_name}", localpart, TEST_PASSWORD, domain="localhost")
    save_matrix_state(state, paths)


async def _prepare(
    config: Config,
    entity_names: list[str],
    runtime_paths: RuntimePaths | None = None,
) -> dict[str, AgentMatrixUser]:
    """Prepare the entity accounts a reload of a running runtime prepares."""
    return await prepare_managed_accounts(
        config,
        runtime_paths or runtime_paths_for(config),
        entity_names,
        include_internal_user=False,
        update_runtime_state=False,
    )


@pytest.mark.asyncio
async def test_account_preparation_retries_transient_create_agent_user_failure(tmp_path: Path) -> None:
    """Account preparation should retry transient Matrix provisioning failures."""
    config = _general_config(tmp_path)
    paths = runtime_paths_for(config)
    calls: list[str] = []

    async def flaky_user(_homeserver: str, entity_name: str, display_name: str, **_kwargs: object) -> AgentMatrixUser:
        calls.append(entity_name)
        if len(calls) == 1:
            msg = "temporary Matrix provisioning failure"
            raise RuntimeError(msg)
        _persist_account(paths, entity_name, f"actual_{entity_name}")
        return _matrix_user(entity_name, f"actual_{entity_name}", display_name)

    with (
        patch("mindroom.orchestration.runtime.retry_delay_seconds", return_value=0.0),
        patch("mindroom.orchestration.accounts.create_agent_user", new=flaky_user),
    ):
        users = await _prepare(config, [ROUTER_AGENT_NAME, "general"])

    assert calls == [ROUTER_AGENT_NAME, ROUTER_AGENT_NAME, "general"]
    assert users[ROUTER_AGENT_NAME].user_id == "@actual_router:localhost"
    assert users["general"].user_id == "@actual_general:localhost"


@pytest.mark.asyncio
async def test_account_preparation_requests_personal_agent_username(tmp_path: Path) -> None:
    """A generated agent's stored Matrix username is requested, so a wiped matrix_state recreates the same ID."""
    paths = test_runtime_paths(tmp_path)
    _append_personal_agent_roster_entry(paths, personal_agent_entry(matrix_username="alice.mind"))
    config = Config.validate_with_runtime(deepcopy(PERSONAL_AGENTS_BASE), paths)
    requested: dict[str, str | None] = {}

    async def record_user(
        _homeserver: str,
        entity_name: str,
        display_name: str,
        *,
        username: str | None = None,
        **_kwargs: object,
    ) -> AgentMatrixUser:
        requested[entity_name] = username
        _persist_account(paths, entity_name, username or f"actual_{entity_name}")
        return _matrix_user(entity_name, username or f"actual_{entity_name}", display_name)

    with patch("mindroom.orchestration.accounts.create_agent_user", new=record_user):
        await _prepare(config, [ROUTER_AGENT_NAME, "mind_alice_smith"], paths)

    assert requested == {ROUTER_AGENT_NAME: None, "mind_alice_smith": "alice.mind"}


@pytest.mark.asyncio
async def test_account_preparation_checks_a_personal_agents_stored_username_before_creating_it(tmp_path: Path) -> None:
    """The localpart collision check uses a generated agent's stored Matrix username."""
    paths = test_runtime_paths(tmp_path)
    _append_personal_agent_roster_entry(paths, personal_agent_entry(matrix_username="alice.mind"))
    config = Config.validate_with_runtime(deepcopy(PERSONAL_AGENTS_BASE), paths)
    _persist_account(paths, "other", "alice.mind")
    create = AsyncMock()

    with (
        patch("mindroom.orchestration.accounts.create_agent_user", new=create),
        pytest.raises(PermanentStartupError, match=r"'alice\.mind'"),
    ):
        await _prepare(config, ["mind_alice_smith"], paths)

    create.assert_not_awaited()


@pytest.mark.asyncio
async def test_account_preparation_rejects_duplicate_persisted_matrix_ids(tmp_path: Path) -> None:
    """Account preparation should fail permanently when persisted entity IDs are ambiguous."""
    config = bind_runtime_paths(
        Config(
            agents={
                "general": {"display_name": "GeneralAgent", "model": "default"},
                "writer": {"display_name": "WriterAgent", "model": "default"},
            },
            models={"default": {"provider": "openai", "id": "test-model"}},
        ),
        test_runtime_paths(tmp_path),
    )
    paths = runtime_paths_for(config)
    for entity_name, localpart in (
        (ROUTER_AGENT_NAME, "actual_router"),
        ("general", "shared_bot"),
        ("writer", "shared_bot"),
    ):
        _persist_account(paths, entity_name, localpart)
    calls: list[str] = []

    async def existing_user(
        _homeserver: str,
        entity_name: str,
        display_name: str,
        **_kwargs: object,
    ) -> AgentMatrixUser:
        calls.append(entity_name)
        localpart = "actual_router" if entity_name == ROUTER_AGENT_NAME else "shared_bot"
        return _matrix_user(entity_name, localpart, display_name)

    with (
        patch("mindroom.orchestration.accounts.create_agent_user", new=existing_user),
        pytest.raises(PermanentStartupError, match="shared_bot"),
    ):
        await _prepare(config, [ROUTER_AGENT_NAME, "general", "writer"])

    assert calls == [ROUTER_AGENT_NAME, "general", "writer"]


@pytest.mark.asyncio
async def test_account_preparation_rejects_internal_user_entity_id_collision(tmp_path: Path) -> None:
    """Account preparation should fail when the internal user shares an entity Matrix ID."""
    config = _general_config(tmp_path, mindroom_user={"username": "mindroom_user", "display_name": "MindRoomUser"})
    paths = runtime_paths_for(config)
    state = MatrixState.load(runtime_paths=paths)
    state.add_account("agent_router", "actual_router", TEST_PASSWORD, domain="localhost")
    state.add_account("agent_general", "shared_actual", TEST_PASSWORD, domain="localhost")
    state.add_account(
        "agent_user",
        "shared_actual",
        TEST_PASSWORD,
        requested_username="mindroom_user",
        domain="localhost",
    )
    save_matrix_state(state, paths)

    async def existing_user(
        _homeserver: str,
        entity_name: str,
        display_name: str,
        **_kwargs: object,
    ) -> AgentMatrixUser:
        localpart = "actual_router" if entity_name == ROUTER_AGENT_NAME else "shared_actual"
        return _matrix_user(entity_name, localpart, display_name)

    with (
        patch("mindroom.orchestration.accounts.create_agent_user", new=existing_user),
        pytest.raises(PermanentStartupError, match="internal user Matrix ID"),
    ):
        await _prepare(config, [ROUTER_AGENT_NAME, "general"])


@pytest.mark.asyncio
async def test_account_preparation_validates_again_after_a_transient_validation_failure(tmp_path: Path) -> None:
    """A transient failure while validating identities retries the whole step, validation included."""
    config = _general_config(tmp_path)
    validations: list[str] = []

    def flaky_registry(*_args: object) -> object:
        validations.append("validated")
        if len(validations) == 1:
            msg = "matrix_state.yaml briefly unreadable"
            raise OSError(msg)
        return object()

    async def existing_user(
        _homeserver: str,
        entity_name: str,
        display_name: str,
        **_kwargs: object,
    ) -> AgentMatrixUser:
        return _matrix_user(entity_name, f"actual_{entity_name}", display_name)

    with (
        patch("mindroom.orchestration.runtime.retry_delay_seconds", return_value=0.0),
        patch("mindroom.orchestration.accounts.create_agent_user", new=existing_user),
        patch("mindroom.orchestration.accounts.entity_identity_registry", new=flaky_registry),
    ):
        users = await _prepare(config, [ROUTER_AGENT_NAME, "general"])

    assert validations == ["validated", "validated"]
    assert users["general"].display_name == "GeneralAgent"
    assert users[ROUTER_AGENT_NAME].display_name == "RouterAgent"


@pytest.mark.asyncio
async def test_entity_preparation_leaves_a_configured_internal_user_alone(tmp_path: Path) -> None:
    """Building bots prepares only their accounts, even when the config has an internal user."""
    config = _general_config(tmp_path, mindroom_user={"username": "mindroom_user", "display_name": "MindRoomUser"})

    async def existing_user(
        _homeserver: str,
        entity_name: str,
        display_name: str,
        **_kwargs: object,
    ) -> AgentMatrixUser:
        return _matrix_user(entity_name, f"mindroom_{entity_name}", display_name)

    with patch("mindroom.orchestration.accounts.create_agent_user", new=AsyncMock(side_effect=existing_user)) as create:
        await _prepare(config, ["general"])

    assert [call.args[1] for call in create.await_args_list] == ["general"]


@pytest.mark.asyncio
@pytest.mark.parametrize("update_runtime_state", [True, False])
async def test_a_transient_internal_user_failure_reports_retries_only_when_asked(
    tmp_path: Path,
    update_runtime_state: bool,
) -> None:
    """The internal user's retry step reports its retries in the runtime state only during startup, as entities do."""
    config = _general_config(tmp_path, mindroom_user={"username": "mindroom_user", "display_name": "MindRoomUser"})
    attempts: list[str] = []

    async def flaky_user(_homeserver: str, entity_name: str, display_name: str, **_kwargs: object) -> AgentMatrixUser:
        attempts.append(entity_name)
        if attempts == ["user"]:
            msg = "temporary Matrix provisioning failure"
            raise RuntimeError(msg)
        return _matrix_user(entity_name, f"mindroom_{entity_name}", display_name)

    with (
        patch("mindroom.orchestration.runtime.retry_delay_seconds", return_value=0.0),
        patch("mindroom.orchestration.runtime.set_runtime_starting") as report,
        patch("mindroom.orchestration.accounts.create_agent_user", new=flaky_user),
    ):
        await prepare_managed_accounts(
            config,
            runtime_paths_for(config),
            [],
            include_internal_user=True,
            update_runtime_state=update_runtime_state,
        )

    assert attempts == ["user", "user"]
    assert report.called is update_runtime_state
