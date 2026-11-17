"""Whose Matrix account a personal agent may use, decided from the current turn alone."""

from __future__ import annotations

from typing import TYPE_CHECKING, Literal
from unittest.mock import MagicMock

import pytest

from mindroom.config.main import Config
from mindroom.config.personal_agents import PersonalAgentRoster, PersonalAgentRosterEntry, personal_agent_roster_path
from mindroom.constants import ROUTER_AGENT_NAME
from mindroom.contacts.store import ContactParty, ContactRecord, ContactSide, update_contacts
from mindroom.custom_tools.matrix_user_authority import MatrixUserActor, MatrixUserRefused, matrix_user_actor
from mindroom.dispatch_source import (
    ACTIVE_THREAD_FOLLOW_UP_SOURCE_KIND,
    EDIT_SOURCE_KIND,
    EXTERNAL_TRIGGER_SOURCE_KIND,
    HOOK_DISPATCH_SOURCE_KIND,
    HOOK_SOURCE_KIND,
    IMAGE_SOURCE_KIND,
    INTERACTIVE_SELECTION_SOURCE_KIND,
    MEDIA_SOURCE_KIND,
    MESSAGE_SOURCE_KIND,
    SCHEDULED_SOURCE_KIND,
    SILENT_SCHEDULE_SOURCE_KIND,
    TRUSTED_INTERNAL_RELAY_SOURCE_KIND,
    VOICE_SOURCE_KIND,
)
from mindroom.entity_resolution import current_entity_id
from mindroom.hooks import MessageEnvelope
from mindroom.message_target import MessageTarget
from mindroom.tool_system.runtime_context import detached_tool_scope, tool_runtime_context
from mindroom.tool_system.worker_routing import build_tool_execution_identity
from mindroom.turns.facts import IngressFacts
from tests.authorization_helpers import make_test_tool_runtime_context
from tests.conftest import make_conversation_reader_mock, make_relation_lookup, test_runtime_paths
from tests.identity_helpers import persist_entity_accounts
from tests.response_runner_helpers import _bot

if TYPE_CHECKING:
    from collections.abc import Callable
    from contextlib import AbstractContextManager
    from pathlib import Path

    from mindroom.constants import RuntimePaths
    from mindroom.tool_system.runtime_context import ToolRuntimeContext
    from mindroom.tool_system.worker_routing import ToolExecutionIdentity

    # Enters the circumstances of one tool call.
    type _Scope = Callable[[Config, RuntimePaths], AbstractContextManager[object]]

OWNER = "@alice:example.com"
OWNER_ALIAS = "@telegram_alice:example.com"
STRANGER = "@bob:example.com"
AGENT = "mind_alice"
# The account recorded for Alice's agent, under a username other than the one MindRoom would generate.
AGENT_USER_ID = "@recorded_mind_alice:localhost"
SHARED_AGENT = "helper"
ROOM = "!room:example.com"
CONTACT_ROOM = "!contact:example.com"
THE_OWNERS_ACTOR = MatrixUserActor(agent_name=AGENT, agent_user_id=AGENT_USER_ID, person_user_id=OWNER)

NOT_A_TURN = "matrix_user works only in a chat turn or through the MCP gateway."
NOT_FOR_A_TEAM = "matrix_user is not available to a team."
NOT_PERSONAL = "matrix_user works only for a personal agent."
NOT_THE_OWNER = "Only the agent's owner may use matrix_user."
NOT_DIRECT = (
    "matrix_user works only when the owner asks directly: not from a hook, a trigger, a contact, "
    "or another agent's message."
)


@pytest.fixture
def setup(tmp_path: Path) -> tuple[Config, RuntimePaths]:
    """A shared agent and Alice's personal agent, which has a contact room besides the room she talks to it in."""
    paths = test_runtime_paths(tmp_path)
    entry = PersonalAgentRosterEntry(
        template="mind",
        user_id=OWNER,
        username="alice",
        agent_name=AGENT,
        matrix_username=None,
        user_display_name="Alice",
        source_room_id="!lobby:example.com",
    )
    roster_path = personal_agent_roster_path(paths)
    roster_path.parent.mkdir(parents=True, exist_ok=True)
    roster_path.write_text(PersonalAgentRoster(entries=[entry]).model_dump_json(), encoding="utf-8")
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


def _turn_context(
    config: Config,
    paths: RuntimePaths,
    *,
    agent_name: str = AGENT,
    requester_id: str = OWNER,
    room_id: str = ROOM,
    **fields: object,
) -> ToolRuntimeContext:
    """Return the context of a chat turn Alice's own message started, with ``fields`` changed."""
    return make_test_tool_runtime_context(
        agent_name=agent_name,
        target=MessageTarget.resolve(room_id, None, "$event"),
        requester_id=requester_id,
        client=MagicMock(),
        config=config,
        runtime_paths=paths,
        relations=make_relation_lookup(),
        conversation_reader=make_conversation_reader_mock(),
        **{"source_kind": MESSAGE_SOURCE_KIND, **fields},
    )


def _detached_identity(paths: RuntimePaths, channel: Literal["mcp", "openai_compat"]) -> ToolExecutionIdentity:
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


@pytest.mark.parametrize(
    "source_kind",
    [
        MESSAGE_SOURCE_KIND,
        VOICE_SOURCE_KIND,
        IMAGE_SOURCE_KIND,
        MEDIA_SOURCE_KIND,
        EDIT_SOURCE_KIND,
        ACTIVE_THREAD_FOLLOW_UP_SOURCE_KIND,
        INTERACTIVE_SELECTION_SOURCE_KIND,
    ],
)
def test_the_owner_asking_directly_is_the_person(setup: tuple[Config, RuntimePaths], source_kind: str) -> None:
    """The owner's own turn with their personal agent acts as the owner, through the agent's recorded account."""
    config, paths = setup
    with tool_runtime_context(_turn_context(config, paths, source_kind=source_kind)):
        assert matrix_user_actor(config, paths) == THE_OWNERS_ACTOR


@pytest.mark.parametrize("source_kind", [SCHEDULED_SOURCE_KIND, SILENT_SCHEDULE_SOURCE_KIND])
def test_the_owners_own_schedule_is_allowed(setup: tuple[Config, RuntimePaths], source_kind: str) -> None:
    """A schedule the owner set runs as the owner's own request."""
    config, paths = setup
    with tool_runtime_context(_turn_context(config, paths, source_kind=source_kind)):
        assert matrix_user_actor(config, paths) == THE_OWNERS_ACTOR


def test_the_owners_gateway_call_is_allowed(setup: tuple[Config, RuntimePaths]) -> None:
    """The owner's MCP gateway call has no chat turn, only the identity the gateway authenticated."""
    config, paths = setup
    with detached_tool_scope(_detached_identity(paths, "mcp")):
        assert matrix_user_actor(config, paths) == THE_OWNERS_ACTOR


def test_an_alias_of_the_owner_is_the_owner(setup: tuple[Config, RuntimePaths]) -> None:
    """The owner writing from a bridged account is still the owner, and the person acted for is the owner's own ID."""
    config, paths = setup
    with tool_runtime_context(_turn_context(config, paths, requester_id=OWNER_ALIAS)):
        assert matrix_user_actor(config, paths) == THE_OWNERS_ACTOR


def _turn(**fields: object) -> _Scope:
    return lambda config, paths: tool_runtime_context(_turn_context(config, paths, **fields))


def _requested_by_managed_account(entity_name: str) -> _Scope:
    """A turn with no human requester runs as a managed account, such as the answering bot's own."""
    return lambda config, paths: tool_runtime_context(
        _turn_context(config, paths, requester_id=current_entity_id(entity_name, paths).full_id),
    )


def _detached(channel: Literal["mcp", "openai_compat"] | None) -> _Scope:
    return lambda _config, paths: detached_tool_scope(None if channel is None else _detached_identity(paths, channel))


@pytest.mark.parametrize(
    ("scope", "message"),
    [
        pytest.param(_turn(agent_name=SHARED_AGENT), NOT_PERSONAL, id="shared-agent"),
        pytest.param(_turn(requester_id=STRANGER), NOT_THE_OWNER, id="another-person"),
        pytest.param(_requested_by_managed_account(AGENT), NOT_THE_OWNER, id="the-agent-itself"),
        pytest.param(_requested_by_managed_account(ROUTER_AGENT_NAME), NOT_THE_OWNER, id="the-router"),
        pytest.param(_turn(transport_agent_name="crew"), NOT_FOR_A_TEAM, id="team-member"),
        pytest.param(_turn(source_kind=HOOK_SOURCE_KIND), NOT_DIRECT, id="hook"),
        pytest.param(_turn(source_kind=HOOK_DISPATCH_SOURCE_KIND), NOT_DIRECT, id="hook-dispatch"),
        pytest.param(_turn(source_kind=EXTERNAL_TRIGGER_SOURCE_KIND), NOT_DIRECT, id="external-trigger"),
        pytest.param(_turn(source_kind=TRUSTED_INTERNAL_RELAY_SOURCE_KIND), NOT_DIRECT, id="relay"),
        pytest.param(_turn(source_kind=None), NOT_DIRECT, id="no-source-kind"),
        pytest.param(_turn(acts_for_requester=True), NOT_DIRECT, id="another-agent-wrote-it"),
        pytest.param(_turn(room_id=CONTACT_ROOM), NOT_DIRECT, id="contact-room"),
        pytest.param(_detached("openai_compat"), NOT_A_TURN, id="openai-compat"),
        pytest.param(_detached(None), NOT_A_TURN, id="no-identity"),
    ],
)
def test_refusals(
    setup: tuple[Config, RuntimePaths],
    scope: _Scope,
    message: str,
) -> None:
    """Anything but the owner asking their own personal agent directly is refused, saying why."""
    config, paths = setup
    with scope(config, paths), pytest.raises(MatrixUserRefused) as refused:
        matrix_user_actor(config, paths)
    assert str(refused.value) == message


def _envelope(target: MessageTarget, facts: IngressFacts) -> MessageEnvelope:
    return MessageEnvelope(
        source_event_id="$event",
        target=target,
        body="hello",
        attachment_ids=(),
        mentioned_agents=(),
        agent_name="general",
        origin=facts.origin,
    )


def test_the_turn_context_says_whether_an_agent_wrote_its_message_for_the_requester(tmp_path: Path) -> None:
    """A turn's context carries whether another managed account wrote its message for the person it runs as."""
    bot = _bot(tmp_path)
    target = MessageTarget.resolve("!room:localhost", None, "$event")
    own = IngressFacts(
        physical_sender="@user:localhost",
        source_kind=MESSAGE_SOURCE_KIND,
        lane_requester_id="@user:localhost",
    )
    written_for = IngressFacts(
        physical_sender="@mindroom_other:localhost",
        source_kind=MESSAGE_SOURCE_KIND,
        lane_requester_id="@mindroom_other:localhost",
        acting_requester_id="@user:localhost",
        sender_entity_name="other",
    )

    def built(facts: IngressFacts) -> ToolRuntimeContext:
        context = bot.tool_runtime_support.build_context(
            target,
            user_id=facts.run_requester_id,
            source_envelope=_envelope(target, facts),
        )
        assert context is not None
        assert context.requester_id == "@user:localhost"
        return context

    assert built(own).acts_for_requester is False
    assert built(written_for).acts_for_requester is True
