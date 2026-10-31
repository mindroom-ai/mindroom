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


def _bound_config(config: Config, runtime_root: Path) -> Config:
    """Return ``config`` bound to a runtime under ``runtime_root`` whose entity accounts are persisted."""
    return bind_runtime_paths(config, test_runtime_paths(runtime_root))


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
    config = _bound_config(
        Config(
            agents={"general": {"display_name": "GeneralAgent", "model": "default"}},
            models={"default": {"provider": "openai", "id": "test-model"}},
        ),
        tmp_path,
    )
    runtime_paths = runtime_paths_for(config)
    calls: list[str] = []
    test_paths = runtime_paths

    async def flaky_user(
        _homeserver: str,
        entity_name: str,
        display_name: str,
        *,
        runtime_paths: object,
        username: str | None = None,
    ) -> AgentMatrixUser:
        del runtime_paths, username
        calls.append(entity_name)
        if len(calls) == 1:
            msg = "temporary Matrix provisioning failure"
            raise RuntimeError(msg)
        state = MatrixState.load(runtime_paths=test_paths)
        actual_username = f"actual_{entity_name}"
        state.add_account(f"agent_{entity_name}", actual_username, TEST_PASSWORD, domain="localhost")
        save_matrix_state(state, test_paths)
        return AgentMatrixUser(
            agent_name=entity_name,
            user_id=f"@{actual_username}:localhost",
            display_name=display_name,
            password=TEST_PASSWORD,
        )

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
        runtime_paths: object,
        username: str | None = None,
    ) -> AgentMatrixUser:
        del runtime_paths
        requested[entity_name] = username
        actual_username = username or f"actual_{entity_name}"
        state = MatrixState.load(runtime_paths=paths)
        state.add_account(f"agent_{entity_name}", actual_username, TEST_PASSWORD, domain="localhost")
        save_matrix_state(state, paths)
        return AgentMatrixUser(
            agent_name=entity_name,
            user_id=f"@{actual_username}:localhost",
            display_name=display_name,
            password=TEST_PASSWORD,
        )

    with patch("mindroom.orchestration.accounts.create_agent_user", new=record_user):
        await _prepare(config, [ROUTER_AGENT_NAME, "mind_alice_smith"], paths)

    assert requested == {ROUTER_AGENT_NAME: None, "mind_alice_smith": "alice.mind"}


@pytest.mark.asyncio
async def test_account_preparation_checks_a_personal_agents_stored_username_before_creating_it(tmp_path: Path) -> None:
    """The localpart collision check uses a generated agent's stored Matrix username."""
    paths = test_runtime_paths(tmp_path)
    _append_personal_agent_roster_entry(paths, personal_agent_entry(matrix_username="alice.mind"))
    config = Config.validate_with_runtime(deepcopy(PERSONAL_AGENTS_BASE), paths)
    state = MatrixState.load(runtime_paths=paths)
    state.add_account("agent_other", "alice.mind", TEST_PASSWORD, domain="localhost")
    save_matrix_state(state, paths)

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
    config = _bound_config(
        Config(
            agents={
                "general": {"display_name": "GeneralAgent", "model": "default"},
                "writer": {"display_name": "WriterAgent", "model": "default"},
            },
            models={"default": {"provider": "openai", "id": "test-model"}},
        ),
        tmp_path,
    )
    runtime_paths = runtime_paths_for(config)
    state = MatrixState.load(runtime_paths=runtime_paths)
    state.add_account("agent_router", "actual_router", TEST_PASSWORD, domain="localhost")
    state.add_account("agent_general", "shared_bot", TEST_PASSWORD, domain="localhost")
    state.add_account("agent_writer", "shared_bot", TEST_PASSWORD, domain="localhost")
    save_matrix_state(state, runtime_paths)
    calls: list[str] = []

    async def existing_user(
        _homeserver: str,
        entity_name: str,
        display_name: str,
        *,
        runtime_paths: object,
        username: str | None = None,
    ) -> AgentMatrixUser:
        del runtime_paths, username
        calls.append(entity_name)
        return AgentMatrixUser(
            agent_name=entity_name,
            user_id="@shared_bot:localhost" if entity_name != ROUTER_AGENT_NAME else "@actual_router:localhost",
            display_name=display_name,
            password=TEST_PASSWORD,
        )

    with (
        patch("mindroom.orchestration.accounts.create_agent_user", new=existing_user),
        pytest.raises(PermanentStartupError, match="shared_bot"),
    ):
        await _prepare(config, [ROUTER_AGENT_NAME, "general", "writer"])

    assert calls == [ROUTER_AGENT_NAME, "general", "writer"]


@pytest.mark.asyncio
async def test_account_preparation_rejects_internal_user_entity_id_collision(tmp_path: Path) -> None:
    """Account preparation should fail when the internal user shares an entity Matrix ID."""
    config = _bound_config(
        Config(
            agents={"general": {"display_name": "GeneralAgent", "model": "default"}},
            models={"default": {"provider": "openai", "id": "test-model"}},
            mindroom_user={"username": "mindroom_user", "display_name": "MindRoomUser"},
        ),
        tmp_path,
    )
    runtime_paths = runtime_paths_for(config)
    state = MatrixState.load(runtime_paths=runtime_paths)
    state.add_account("agent_router", "actual_router", TEST_PASSWORD, domain="localhost")
    state.add_account("agent_general", "shared_actual", TEST_PASSWORD, domain="localhost")
    state.add_account(
        "agent_user",
        "shared_actual",
        TEST_PASSWORD,
        requested_username="mindroom_user",
        domain="localhost",
    )
    save_matrix_state(state, runtime_paths)

    async def existing_user(
        _homeserver: str,
        entity_name: str,
        display_name: str,
        *,
        runtime_paths: object,
        username: str | None = None,
    ) -> AgentMatrixUser:
        del runtime_paths, username
        user_id = "@actual_router:localhost" if entity_name == ROUTER_AGENT_NAME else "@shared_actual:localhost"
        return AgentMatrixUser(
            agent_name=entity_name,
            user_id=user_id,
            display_name=display_name,
            password=TEST_PASSWORD,
        )

    with (
        patch("mindroom.orchestration.accounts.create_agent_user", new=existing_user),
        pytest.raises(PermanentStartupError, match="internal user Matrix ID"),
    ):
        await _prepare(config, [ROUTER_AGENT_NAME, "general"])


@pytest.mark.asyncio
async def test_account_preparation_validates_again_after_a_transient_validation_failure(tmp_path: Path) -> None:
    """A transient failure while validating identities retries the whole step, validation included."""
    config = _bound_config(
        Config(
            agents={"general": {"display_name": "GeneralAgent", "model": "default"}},
            models={"default": {"provider": "openai", "id": "test-model"}},
        ),
        tmp_path,
    )
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
        *,
        runtime_paths: object,
        username: str | None = None,
    ) -> AgentMatrixUser:
        del runtime_paths, username
        return AgentMatrixUser(
            agent_name=entity_name,
            user_id=f"@actual_{entity_name}:localhost",
            display_name=display_name,
            password=TEST_PASSWORD,
        )

    with (
        patch("mindroom.orchestration.runtime.retry_delay_seconds", return_value=0.0),
        patch("mindroom.orchestration.accounts.create_agent_user", new=existing_user),
        patch("mindroom.orchestration.accounts.entity_identity_registry", new=flaky_registry),
    ):
        users = await _prepare(config, [ROUTER_AGENT_NAME, "general"])

    assert validations == ["validated", "validated"]
    assert users["general"].display_name == "GeneralAgent"
    assert users[ROUTER_AGENT_NAME].display_name == "RouterAgent"
