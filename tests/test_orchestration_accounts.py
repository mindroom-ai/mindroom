"""Managed Matrix account preparation, tested without an orchestrator."""

from __future__ import annotations

from copy import deepcopy
from typing import TYPE_CHECKING
from unittest.mock import AsyncMock, MagicMock, patch

import nio
import pytest

from mindroom.config.main import Config
from mindroom.config.matrix import MindRoomUserConfig
from mindroom.constants import ROUTER_AGENT_NAME
from mindroom.matrix.state import MatrixState
from mindroom.matrix.users import INTERNAL_USER_ACCOUNT_KEY, AgentMatrixUser
from mindroom.matrix_identifiers import agent_username_localpart
from mindroom.orchestration.accounts import prepare_managed_accounts
from mindroom.personal_agents import _append_personal_agent_roster_entry
from mindroom.requester_identity import mindroom_user_id
from mindroom.startup_errors import PermanentStartupError
from tests.conftest import (
    TEST_ACCESS_TOKEN,
    TEST_PASSWORD,
    bind_runtime_paths,
    runtime_paths_for,
    test_runtime_paths,
)
from tests.matrix_state_helpers import save_matrix_state
from tests.test_personal_agents_config import BASE as PERSONAL_AGENTS_BASE
from tests.test_personal_agents_config import _entry as personal_agent_entry

if TYPE_CHECKING:
    from pathlib import Path

    from mindroom.constants import RuntimePaths

_DEFAULT_INTERNAL_USER = MindRoomUserConfig()
_MINDROOM_USER = {"username": "mindroom_user", "display_name": "MindRoomUser"}


def _general_config(tmp_path: Path, extra_agents: dict[str, object] | None = None, **sections: object) -> Config:
    """Return a config with the `general` agent, bound to a runtime under ``tmp_path`` whose accounts are persisted."""
    return bind_runtime_paths(
        Config(
            agents={"general": {"display_name": "GeneralAgent", "model": "default"}, **(extra_agents or {})},
            models={"default": {"provider": "openai", "id": "test-model"}},
            **sections,
        ),
        test_runtime_paths(tmp_path),
    )


def _internal_user_config(paths: RuntimePaths, username: str, display_name: str = "MindRoomUser") -> Config:
    """Return a config with only the router and the internal user, of which only the router's account is persisted."""
    _persist_account(paths, ROUTER_AGENT_NAME, "mindroom_router")
    return Config.validate_with_runtime(
        {
            "models": {"default": {"provider": "openai", "id": "test-model"}},
            "mindroom_user": {"username": username, "display_name": display_name},
        },
        paths,
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


async def _existing_user(_homeserver: str, entity_name: str, display_name: str, **_kwargs: object) -> AgentMatrixUser:
    """Stand in for `create_agent_user` loading an account that is already persisted."""
    return _matrix_user(entity_name, f"actual_{entity_name}", display_name)


def _created(create: AsyncMock) -> list[str]:
    """Return the entities whose accounts ``create`` was asked for, in order."""
    return [call.args[1] for call in create.await_args_list]


async def _prepare(
    config: Config,
    entity_names: list[str],
    runtime_paths: RuntimePaths | None = None,
    *,
    include_internal_user: bool = False,
) -> dict[str, AgentMatrixUser]:
    """Prepare accounts as a running runtime does: the entities', and the internal user's when asked."""
    return await prepare_managed_accounts(
        config,
        runtime_paths or runtime_paths_for(config),
        entity_names,
        include_internal_user=include_internal_user,
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
@pytest.mark.parametrize("include_internal_user", [False, True])
async def test_account_preparation_creates_no_account_while_a_requested_localpart_is_taken(
    tmp_path: Path,
    include_internal_user: bool,
) -> None:
    """No account is created, the internal user's included, while any requested localpart is taken.

    The check uses a generated agent's stored Matrix username.
    """
    paths = test_runtime_paths(tmp_path)
    _append_personal_agent_roster_entry(paths, personal_agent_entry(matrix_username="alice.mind"))
    config = Config.validate_with_runtime(deepcopy(PERSONAL_AGENTS_BASE) | {"mindroom_user": _MINDROOM_USER}, paths)
    _persist_account(paths, "other", "alice.mind")
    create = AsyncMock()

    with (
        patch("mindroom.orchestration.accounts.create_agent_user", new=create),
        pytest.raises(PermanentStartupError, match=r"'alice\.mind'"),
    ):
        await _prepare(
            config,
            [ROUTER_AGENT_NAME, "mind_alice_smith"],
            paths,
            include_internal_user=include_internal_user,
        )

    create.assert_not_awaited()


@pytest.mark.asyncio
async def test_account_preparation_checks_entity_accounts_against_the_internal_account_the_server_created(
    tmp_path: Path,
) -> None:
    """A server may give the internal user another localpart; no entity account is created over the one it took."""
    paths = test_runtime_paths(tmp_path)
    config = Config.validate_with_runtime(
        {
            "agents": {"general": {"display_name": "GeneralAgent", "model": "default"}},
            "models": {"default": {"provider": "openai", "id": "test-model"}},
            "mindroom_user": _MINDROOM_USER,
        },
        paths,
    )
    general_localpart = agent_username_localpart("general", paths)

    async def server_renames_internal_user(
        _homeserver: str,
        entity_name: str,
        display_name: str,
        **_kwargs: object,
    ) -> AgentMatrixUser:
        _persist_account(paths, entity_name, general_localpart)
        return _matrix_user(entity_name, general_localpart, display_name)

    with (
        patch(
            "mindroom.orchestration.accounts.create_agent_user",
            new=AsyncMock(side_effect=server_renames_internal_user),
        ) as create,
        pytest.raises(PermanentStartupError, match="localpart collision"),
    ):
        await _prepare(config, [ROUTER_AGENT_NAME, "general"], paths, include_internal_user=True)

    assert _created(create) == ["user"]


@pytest.mark.asyncio
async def test_account_preparation_rejects_duplicate_persisted_matrix_ids(tmp_path: Path) -> None:
    """Account preparation should fail permanently when persisted entity IDs are ambiguous."""
    config = _general_config(tmp_path, {"writer": {"display_name": "WriterAgent", "model": "default"}})
    paths = runtime_paths_for(config)
    for entity_name in ("general", "writer"):
        _persist_account(paths, entity_name, "shared_bot")

    with (
        patch("mindroom.orchestration.accounts.create_agent_user", new=AsyncMock(side_effect=_existing_user)) as create,
        pytest.raises(PermanentStartupError, match="shared_bot"),
    ):
        await _prepare(config, [ROUTER_AGENT_NAME, "general", "writer"])

    assert _created(create) == [ROUTER_AGENT_NAME, "general", "writer"]


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("entity_names", "include_internal_user", "created"),
    [
        pytest.param([ROUTER_AGENT_NAME, "general"], False, [ROUTER_AGENT_NAME, "general"], id="entities"),
        pytest.param([], True, ["user"], id="internal-user"),
    ],
)
async def test_account_preparation_rejects_internal_user_entity_id_collision(
    tmp_path: Path,
    entity_names: list[str],
    include_internal_user: bool,
    created: list[str],
) -> None:
    """Preparing entity accounts, or only the internal user's, fails when the internal user shares an entity ID."""
    config = _general_config(tmp_path, mindroom_user=_MINDROOM_USER)
    paths = runtime_paths_for(config)
    _persist_account(paths, "general", "mindroom_user")

    with (
        patch("mindroom.orchestration.accounts.create_agent_user", new=AsyncMock(side_effect=_existing_user)) as create,
        pytest.raises(PermanentStartupError, match="internal user Matrix ID"),
    ):
        await _prepare(config, entity_names, include_internal_user=include_internal_user)

    assert _created(create) == created


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

    with (
        patch("mindroom.orchestration.runtime.retry_delay_seconds", return_value=0.0),
        patch("mindroom.orchestration.accounts.create_agent_user", new=_existing_user),
        patch("mindroom.orchestration.accounts.entity_identity_registry", new=flaky_registry),
    ):
        users = await _prepare(config, [ROUTER_AGENT_NAME, "general"])

    assert validations == ["validated", "validated"]
    assert users["general"].display_name == "GeneralAgent"
    assert users[ROUTER_AGENT_NAME].display_name == "RouterAgent"


@pytest.mark.asyncio
async def test_entity_preparation_leaves_a_configured_internal_user_alone(tmp_path: Path) -> None:
    """Building bots prepares only their accounts, even when the config has an internal user."""
    config = _general_config(tmp_path, mindroom_user=_MINDROOM_USER)
    create = AsyncMock(side_effect=_existing_user)

    with patch("mindroom.orchestration.accounts.create_agent_user", new=create):
        await _prepare(config, ["general"])

    assert _created(create) == ["general"]


@pytest.mark.asyncio
@pytest.mark.parametrize("update_runtime_state", [True, False])
async def test_a_transient_internal_user_failure_reports_retries_only_when_asked(
    tmp_path: Path,
    update_runtime_state: bool,
) -> None:
    """The internal user's retry step reports its retries in the runtime state only during startup, as entities do."""
    config = _general_config(tmp_path, mindroom_user=_MINDROOM_USER)
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


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("username", "display_name", "server_localpart"),
    [
        pytest.param(
            _DEFAULT_INTERNAL_USER.username,
            _DEFAULT_INTERNAL_USER.display_name,
            f"{_DEFAULT_INTERNAL_USER.username}_test",
            id="default-identity",
        ),
        pytest.param("alice", "Alice Smith", "alice", id="custom-identity"),
    ],
)
async def test_internal_user_preparation_registers_a_missing_account(
    tmp_path: Path,
    username: str,
    display_name: str,
    server_localpart: str,
) -> None:
    """A missing internal account registers the configured identity with a generated password under the server's ID."""
    paths = test_runtime_paths(tmp_path)
    config = _internal_user_config(paths, username, display_name)
    client = AsyncMock()
    client.register.return_value = nio.RegisterResponse(
        user_id=f"@{server_localpart}:localhost",
        device_id="TEST_DEVICE",
        access_token=TEST_ACCESS_TOKEN,
    )
    client_context = MagicMock()
    client_context.__aenter__.return_value = client

    with patch("mindroom.matrix.users.matrix_client", return_value=client_context):
        await _prepare(config, [], paths, include_internal_user=True)

    account = MatrixState.load(runtime_paths=paths).accounts[INTERNAL_USER_ACCOUNT_KEY]
    assert account.username == server_localpart
    assert account.password
    assert account.password != "user_secure_password"  # noqa: S105
    client.register.assert_called_once_with(username=username, password=account.password, device_name="mindroom_agent")
    client.set_displayname.assert_called_once_with(display_name)


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("username", "localpart", "requested_username", "domain"),
    [
        pytest.param(
            _DEFAULT_INTERNAL_USER.username,
            _DEFAULT_INTERNAL_USER.username,
            None,
            "localhost",
            id="matching-identity",
        ),
        pytest.param("alice", "actual_mindroom_user", "alice", "matrix.example", id="drifted-identity"),
    ],
)
async def test_internal_user_preparation_keeps_a_persisted_account_without_a_matrix_request(
    tmp_path: Path,
    username: str,
    localpart: str,
    requested_username: str | None,
    domain: str,
) -> None:
    """A persisted internal account keeps its stored identity and password; the configured username only proposes one."""
    paths = test_runtime_paths(tmp_path)
    config = _internal_user_config(paths, username)
    state = MatrixState.load(runtime_paths=paths)
    state.add_account(
        INTERNAL_USER_ACCOUNT_KEY,
        localpart,
        "existing_password",
        requested_username=requested_username,
        domain=domain,
    )
    save_matrix_state(state, paths)
    matrix_client = MagicMock()

    with patch("mindroom.matrix.users.matrix_client", new=matrix_client):
        await _prepare(config, [], paths, include_internal_user=True)

    account = MatrixState.load(runtime_paths=paths).accounts[INTERNAL_USER_ACCOUNT_KEY]
    assert (account.username, account.password, account.domain) == (localpart, "existing_password", domain)
    assert mindroom_user_id(config, paths) == f"@{localpart}:{domain}"
    matrix_client.assert_not_called()
