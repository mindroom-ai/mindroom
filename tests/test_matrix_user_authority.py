"""Whose Matrix account a personal agent may use, decided by who the call runs for and its room, not what started it."""

from __future__ import annotations

from typing import TYPE_CHECKING, Literal

import pytest

from mindroom.config.main import Config
from mindroom.constants import ROUTER_AGENT_NAME
from mindroom.contacts.store import ContactParty, ContactRecord, ContactSide, update_contacts
from mindroom.custom_tools.matrix_user_authority import MatrixUserActor, MatrixUserRefused, matrix_user_actor
from mindroom.dispatch_source import (
    EXTERNAL_TRIGGER_SOURCE_KIND,
    HOOK_SOURCE_KIND,
    MESSAGE_SOURCE_KIND,
    SCHEDULED_SOURCE_KIND,
    SILENT_SCHEDULE_SOURCE_KIND,
    TRUSTED_INTERNAL_RELAY_SOURCE_KIND,
)
from mindroom.entity_resolution import current_entity_id
from mindroom.matrix.identity import managed_account_key
from mindroom.matrix.state import update_matrix_state
from mindroom.tool_system.runtime_context import detached_tool_scope
from mindroom.tool_system.worker_routing import build_tool_execution_identity
from tests.conftest import test_runtime_paths
from tests.identity_helpers import persist_entity_accounts
from tests.matrix_user_helpers import AGENT, OWNER, ROOM, owner_turn, write_owner_roster

if TYPE_CHECKING:
    from collections.abc import Callable
    from contextlib import AbstractContextManager
    from pathlib import Path

    from mindroom.constants import RuntimePaths
    from mindroom.tool_system.worker_routing import ToolExecutionIdentity

    # Enters the circumstances of one tool call.
    type _Scope = Callable[[Config, RuntimePaths], AbstractContextManager[object]]

OWNER_ALIAS = "@telegram_alice:example.com"
STRANGER = "@bob:example.com"
# The account recorded for Alice's agent, under a username other than the one MindRoom would generate.
AGENT_USER_ID = "@recorded_mind_alice:localhost"
SHARED_AGENT = "helper"
TEAM = "crew"
CONTACT_ROOM = "!contact:example.com"
THE_OWNERS_ACTOR = MatrixUserActor(agent_name=AGENT, agent_user_id=AGENT_USER_ID, person_user_id=OWNER)

NOT_A_TURN = "matrix_user works only in a chat turn or through the MCP gateway."
NOT_PERSONAL = "matrix_user works only for a personal agent."
NOT_THE_OWNER = "Only the agent's owner may use matrix_user."
NOT_IN_A_CONTACT_ROOM = "matrix_user is not available in a contact conversation."
NO_ACCOUNT = "matrix_user is not available: this agent has no Matrix account yet."

_Channel = Literal["matrix", "mcp", "openai_compat"]


@pytest.fixture
def setup(tmp_path: Path) -> tuple[Config, RuntimePaths]:
    """A shared agent and Alice's personal agent, which has a contact room besides the room she talks to it in."""
    paths = test_runtime_paths(tmp_path)
    write_owner_roster(paths)
    data = {
        "models": {"default": {"provider": "openai", "id": "m"}},
        "agents": {SHARED_AGENT: {"display_name": "Helper", "model": "default"}},
        "personal_agents": {"mind": {"agent": {"display_name": "{user_display_name}'s Mind", "model": "default"}}},
        "authorization": {"aliases": {OWNER: [OWNER_ALIAS]}},
    }
    config = Config.validate_with_runtime(data, paths)
    persist_entity_accounts(config, paths, usernames={AGENT: "recorded_mind_alice"})
    contact = ContactRecord(
        id="c1",
        status="active",
        requester=ContactParty(kind="agent", id=AGENT, owner=OWNER),
        target=ContactParty(kind="person", id=STRANGER, owner=STRANGER),
        reason="Plan the dinner",
        requester_side=ContactSide(),
        target_side=ContactSide(),
        origin_room_id=ROOM,
        origin_thread_id=None,
        room_id=CONTACT_ROOM,
    )
    update_contacts(paths, lambda book: book.contacts.append(contact))
    return config, paths


def _detached_identity(paths: RuntimePaths, channel: _Channel) -> ToolExecutionIdentity:
    """Return the identity a runner without a Matrix conversation binds for Alice's call to her personal agent."""
    return build_tool_execution_identity(
        channel=channel,
        agent_name=AGENT,
        runtime_paths=paths,
        requester_id=OWNER,
        room_id=None,
        thread_id=None,
        resolved_thread_id=None,
        session_id=None,
    )


def _turn(**fields: object) -> _Scope:
    """A chat turn Alice's own message started, unless ``fields`` say otherwise."""
    return lambda config, paths: owner_turn(config, paths, **{"source_kind": MESSAGE_SOURCE_KIND, **fields})


def _requested_by_managed_account(entity_name: str) -> _Scope:
    """A turn with no human requester runs as a managed account, such as the answering bot's own."""
    return lambda config, paths: _turn(requester_id=current_entity_id(entity_name, paths).full_id)(config, paths)


def _detached(channel: _Channel | None) -> _Scope:
    return lambda _config, paths: detached_tool_scope(None if channel is None else _detached_identity(paths, channel))


def _owners_turn_without_a_recorded_account(config: Config, paths: RuntimePaths) -> AbstractContextManager[object]:
    """The owner's turn with an agent whose Matrix account the runtime has not recorded yet."""
    update_matrix_state(paths, lambda state: state.accounts.pop(managed_account_key(AGENT)))
    return _turn()(config, paths)


@pytest.mark.parametrize(
    "scope",
    [
        *(
            pytest.param(_turn(source_kind=source_kind), id=f"owners-turn-started-by-{source_kind or 'nothing'}")
            for source_kind in (
                MESSAGE_SOURCE_KIND,
                SCHEDULED_SOURCE_KIND,
                SILENT_SCHEDULE_SOURCE_KIND,
                HOOK_SOURCE_KIND,
                EXTERNAL_TRIGGER_SOURCE_KIND,
                TRUSTED_INTERNAL_RELAY_SOURCE_KIND,
                None,
            )
        ),
        pytest.param(_detached("mcp"), id="owners-gateway-call"),
        pytest.param(_turn(transport_agent_name=TEAM), id="team-run-the-owner-started"),
        pytest.param(_turn(requester_id=OWNER_ALIAS), id="alias-of-the-owner"),
    ],
)
def test_the_owners_calls_are_allowed(setup: tuple[Config, RuntimePaths], scope: _Scope) -> None:
    """The owner's call acts as the owner, through the agent's recorded account.

    A turn that runs for the owner qualifies whatever started it; so does the owner's MCP gateway call, which has no
    chat turn, only the identity the gateway authenticated; so does the personal agent answering as a member of a team
    the owner asked; and so does the owner writing from a bridged account, the person acted for being the owner's own ID.
    """
    config, paths = setup
    with scope(config, paths):
        assert matrix_user_actor(config, paths) == THE_OWNERS_ACTOR


@pytest.mark.parametrize(
    ("scope", "message"),
    [
        pytest.param(_detached(None), NOT_A_TURN, id="no-identity"),
        pytest.param(_detached("openai_compat"), NOT_A_TURN, id="openai-compat"),
        pytest.param(_detached("matrix"), NOT_A_TURN, id="matrix-identity-without-a-turn"),
        pytest.param(_turn(agent_name=SHARED_AGENT), NOT_PERSONAL, id="shared-agent"),
        pytest.param(_turn(requester_id=STRANGER), NOT_THE_OWNER, id="another-person"),
        pytest.param(_turn(requester_id=STRANGER, transport_agent_name=TEAM), NOT_THE_OWNER, id="another-persons-team"),
        pytest.param(_requested_by_managed_account(AGENT), NOT_THE_OWNER, id="the-agent-itself"),
        pytest.param(_requested_by_managed_account(ROUTER_AGENT_NAME), NOT_THE_OWNER, id="the-router"),
        pytest.param(_turn(room_id=CONTACT_ROOM), NOT_IN_A_CONTACT_ROOM, id="contact-room"),
        pytest.param(_owners_turn_without_a_recorded_account, NO_ACCOUNT, id="no-recorded-account"),
    ],
)
def test_refusals(
    setup: tuple[Config, RuntimePaths],
    scope: _Scope,
    message: str,
) -> None:
    """Anyone but the owner, a call outside a chat turn or the gateway, and a contact room are refused, saying why."""
    config, paths = setup
    with scope(config, paths), pytest.raises(MatrixUserRefused) as refused:
        matrix_user_actor(config, paths)
    assert str(refused.value) == message
