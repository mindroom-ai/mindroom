"""Tests for who a turn in a contact room runs as, and who may start one."""

from __future__ import annotations

from functools import partial
from typing import TYPE_CHECKING, Any
from unittest.mock import AsyncMock, MagicMock

import nio
import pytest
from structlog.testing import capture_logs

from mindroom.bot_runtime_view import BotRuntimeState
from mindroom.constants import ACTING_REQUESTER_KEY, ORIGINAL_SENDER_KEY, SKIP_MENTIONS_KEY, SOURCE_KIND_KEY
from mindroom.contacts.admission import _ContactAdmission, contact_admission
from mindroom.contacts.store import ContactParty, ContactRecord, ContactSide, update_contacts
from mindroom.dispatch_handoff import PreparedIngress
from mindroom.dispatch_replay_guard import has_newer_unresponded_in_thread
from mindroom.dispatch_source import TRUSTED_INTERNAL_RELAY_SOURCE_KIND
from mindroom.entity_resolution import current_entity_id
from mindroom.ingress_validation import IngressValidator, IngressValidatorDeps
from mindroom.logging_config import get_logger
from mindroom.matrix.client_visible_messages import ResolvedVisibleMessage
from mindroom.turn_origin import TurnIntent
from mindroom.turn_policy import TurnPolicy, TurnPolicyDeps
from tests.conftest import test_runtime_paths
from tests.identity_helpers import persist_entity_accounts
from tests.runtime_fakes import ready_runtime
from tests.test_generated_agents import CO_OWNER, OWNER, _load, _personal, _with_template

if TYPE_CHECKING:
    from pathlib import Path

    from mindroom.config.main import Config
    from mindroom.constants import RuntimePaths
    from mindroom.contacts.store import _ContactStatus
    from mindroom.turns.facts import IngressFacts

ADMIN = "@admin:localhost"
CAROL = "@carol:localhost"
DAVE = "@dave:localhost"
DAVE_BRIDGE = "@telegram_dave:localhost"
CONTACT_ROOM = "!contact:localhost"
OTHER_ROOM = "!other:localhost"
ALICE_AGENT = ContactParty(kind="agent", id="mind_alice", owner=OWNER)
BOB_AGENT = ContactParty(kind="agent", id="mind_bob", owner=CO_OWNER)
DAVE_PERSON = ContactParty(kind="person", id=DAVE, owner=DAVE)


def _setup(tmp_path: Path) -> tuple[Config, RuntimePaths]:
    """Personal agents of Alice, Bob, and Carol with contacts, an administrator, and Dave, who has a bridge alias."""
    data = _with_template()
    data["personal_agents"]["mind"]["agent"]["contacts"] = {}
    data["administrators"] = [ADMIN]
    data["authorization"] = {"aliases": {DAVE: [DAVE_BRIDGE]}}
    config = _load(tmp_path, data, entries=[_personal(), _personal(CO_OWNER), _personal(CAROL)])
    paths = test_runtime_paths(tmp_path)
    persist_entity_accounts(config, paths)
    return config, paths


def _contact(
    paths: RuntimePaths,
    *,
    requester: ContactParty = ALICE_AGENT,
    status: _ContactStatus = "active",
    room_id: str | None = CONTACT_ROOM,
) -> None:
    """Store contact ``c1`` between ``requester`` and Bob's agent, with its room unless it never had one."""
    record = ContactRecord(
        id="c1",
        status=status,
        requester=requester,
        target=BOB_AGENT,
        reason="Plan the dinner",
        requester_side=ContactSide(),
        target_side=ContactSide(),
        origin_room_id="!origin:localhost",
        origin_thread_id=None,
        room_id=room_id,
        created_at=1.0,
    )
    update_contacts(paths, lambda book: book.contacts.append(record))


def _agent(paths: RuntimePaths, agent_name: str) -> str:
    return current_entity_id(agent_name, paths).full_id


def _admission(config: Config, paths: RuntimePaths, *sender_ids: str, agent_name: str = "mind_bob") -> Any:  # noqa: ANN401
    return contact_admission(config, paths, room_id=CONTACT_ROOM, agent_name=agent_name, sender_ids=sender_ids)


def test_the_counterparty_and_the_owner_start_turns_that_run_as_the_owner(tmp_path: Path) -> None:
    """The other agent, its owner, and this agent's owner are admitted as the owner; anyone else is refused."""
    config, paths = _setup(tmp_path)
    _contact(paths)
    admitted = _ContactAdmission("c1", CO_OWNER)
    refused = _ContactAdmission("c1", None)

    assert _admission(config, paths, _agent(paths, "mind_alice")) == admitted
    assert _admission(config, paths, OWNER) == admitted
    assert _admission(config, paths, CO_OWNER) == admitted
    for stranger in (ADMIN, CAROL, _agent(paths, "mind_carol"), _agent(paths, "mind_bob")):
        assert _admission(config, paths, stranger) == refused
    # Any identity the event speaks for admits it, such as the original sender a managed relay names.
    assert _admission(config, paths, _agent(paths, "mind_carol"), OWNER) == admitted
    # The other side runs as its own owner in the same room.
    assert _admission(config, paths, CO_OWNER, agent_name="mind_alice") == _ContactAdmission("c1", OWNER)


def test_a_person_counterparty_is_admitted_under_any_of_their_aliases(tmp_path: Path) -> None:
    """A person party speaks only for themselves, by canonical ID or bridge alias, and their agent is a stranger."""
    config, paths = _setup(tmp_path)
    _contact(paths, requester=DAVE_PERSON)

    assert _admission(config, paths, DAVE) == _ContactAdmission("c1", CO_OWNER)
    assert _admission(config, paths, DAVE_BRIDGE) == _ContactAdmission("c1", CO_OWNER)
    assert _admission(config, paths, _agent(paths, "mind_alice")) == _ContactAdmission("c1", None)


@pytest.mark.parametrize("status", ["pending", "declined"])
def test_no_contact_room_admits_nothing(tmp_path: Path, status: _ContactStatus) -> None:
    """Another room, an agent that is no party, and a contact that never had a room leave the event as it is."""
    config, paths = _setup(tmp_path)
    _contact(paths)
    assert contact_admission(config, paths, room_id=OTHER_ROOM, agent_name="mind_bob", sender_ids=[OWNER]) is None
    assert _admission(config, paths, OWNER, agent_name="mind_carol") is None

    other_config, other_paths = _setup(tmp_path / status)
    _contact(other_paths, status=status, room_id=None)
    assert _admission(other_config, other_paths, OWNER) is None


def test_a_removed_contacts_room_admits_nobody(tmp_path: Path) -> None:
    """Once removed, a contact's room starts no turn of its agents, whoever writes there, their owners included."""
    config, paths = _setup(tmp_path)
    _contact(paths, status="removed")
    refused = _ContactAdmission("c1", None)

    for sender in (OWNER, CO_OWNER, _agent(paths, "mind_alice"), ADMIN):
        assert _admission(config, paths, sender) == refused
    assert _admission(config, paths, CO_OWNER, agent_name="mind_alice") == refused


def _validator(config: Config, paths: RuntimePaths, agent_name: str = "mind_bob") -> IngressValidator:
    """Return the ingress validator of ``agent_name`` with the access its config grants."""
    runtime = BotRuntimeState(orchestrator=ready_runtime(config, paths))
    matrix_id = current_entity_id(agent_name, paths)
    turn_store = MagicMock()
    turn_store.is_handled = AsyncMock(return_value=False)
    turn_store.record_turn = AsyncMock()
    policy = TurnPolicy(
        TurnPolicyDeps(
            runtime=runtime,
            logger=get_logger(__name__),
            runtime_paths=paths,
            agent_name=agent_name,
            matrix_id=matrix_id,
            agent_reply_memberships=runtime.agent_reply_memberships,
        ),
    )
    return IngressValidator(
        IngressValidatorDeps(
            runtime=runtime,
            runtime_paths=paths,
            agent_name=agent_name,
            matrix_id=matrix_id,
            turn_store=turn_store,
            turn_policy=policy,
            settle_ignored_sources=AsyncMock(),
        ),
    )


def _event(sender: str, **content: object) -> nio.RoomMessageText:
    return nio.RoomMessageText.from_dict(
        {
            "event_id": "$event",
            "sender": sender,
            "origin_server_ts": 1,
            "content": {"msgtype": "m.text", "body": "Are you free on Friday?", **content},
        },
    )


def _relay(paths: RuntimePaths, original_sender: str, recipient: str = "mind_bob") -> dict[str, object]:
    """Return the content `matrix_message` sends to ``recipient`` for ``original_sender``, its turn's requester."""
    recipient_id = _agent(paths, recipient)
    return {
        "body": f"{recipient_id} Are you free on Friday?",
        "m.mentions": {"user_ids": [recipient_id]},
        ORIGINAL_SENDER_KEY: original_sender,
        SOURCE_KIND_KEY: TRUSTED_INTERNAL_RELAY_SOURCE_KIND,
    }


async def _precheck(validator: IngressValidator, event: nio.RoomMessageText, facts: IngressFacts) -> bool:
    room = nio.MatrixRoom(CONTACT_ROOM, validator.deps.matrix_id.full_id)
    return await validator.precheck_event(room, event, facts)


@pytest.mark.asyncio
@pytest.mark.usefixtures("enforce_turn_authorization")
async def test_a_counterparty_relay_runs_as_the_owner_and_keeps_its_speaker(tmp_path: Path) -> None:
    """The other agent's message runs as Bob, still reads as the other agent's, and dispatches as a relay does."""
    config, paths = _setup(tmp_path)
    _contact(paths)
    validator = _validator(config, paths)
    alice_agent = _agent(paths, "mind_alice")
    event = _event(alice_agent, **_relay(paths, OWNER))

    facts = validator.classify(event, room_id=CONTACT_ROOM)

    assert (facts.physical_sender, facts.lane_requester_id, facts.run_requester_id) == (alice_agent, CO_OWNER, CO_OWNER)
    assert facts.contact_id == "c1"
    assert facts.acts_for_requester
    assert facts.requester_entity_name is None
    origin = facts.origin
    assert origin.speaker_id == alice_agent
    assert origin.intent is TurnIntent.TRUSTED_INTERNAL_RELAY
    assert not origin.blocks_unmentioned_managed_sender
    assert await _precheck(validator, event, facts)
    # The same message in any other room runs as the other side's owner, whom Bob's agent refuses.
    outside = validator.classify(event, room_id=OTHER_ROOM)
    assert (outside.run_requester_id, outside.contact_id) == (OWNER, None)
    assert not await _precheck(validator, event, outside)


@pytest.mark.asyncio
@pytest.mark.usefixtures("enforce_turn_authorization")
async def test_people_in_a_contact_room_run_as_the_owner_and_speak_as_themselves(tmp_path: Path) -> None:
    """The other side's owner runs as Bob but stays the speaker; Bob speaks as himself."""
    config, paths = _setup(tmp_path)
    _contact(paths)
    validator = _validator(config, paths)

    alice = validator.classify(_event(OWNER), room_id=CONTACT_ROOM)
    assert (alice.run_requester_id, alice.contact_id) == (CO_OWNER, "c1")
    assert alice.acts_for_requester
    assert alice.origin.speaker_id == OWNER
    assert await _precheck(validator, _event(OWNER), alice)

    bob = validator.classify(_event(CO_OWNER), room_id=CONTACT_ROOM)
    assert (bob.lane_requester_id, bob.run_requester_id, bob.contact_id) == (CO_OWNER, CO_OWNER, "c1")
    assert not bob.acts_for_requester
    assert bob.origin.speaker_id is None
    assert await _precheck(validator, _event(CO_OWNER), bob)


@pytest.mark.asyncio
@pytest.mark.usefixtures("enforce_turn_authorization")
async def test_a_managed_reply_for_the_counterparty_runs_as_the_owner(tmp_path: Path) -> None:
    """The other agent's reply written for its owner runs as Bob, and its unmentioned messages stay chatter."""
    config, paths = _setup(tmp_path)
    _contact(paths)
    validator = _validator(config, paths)
    alice_agent = _agent(paths, "mind_alice")

    reply = validator.classify(_event(alice_agent, **{ACTING_REQUESTER_KEY: OWNER}), room_id=CONTACT_ROOM)
    assert (reply.lane_requester_id, reply.run_requester_id) == (CO_OWNER, CO_OWNER)
    assert reply.origin.speaker_id == alice_agent

    chatter = validator.classify(_event(alice_agent, **{SKIP_MENTIONS_KEY: True}), room_id=CONTACT_ROOM)
    assert chatter.run_requester_id == CO_OWNER
    assert chatter.origin.blocks_unmentioned_managed_sender


@pytest.mark.asyncio
@pytest.mark.usefixtures("enforce_turn_authorization")
async def test_anyone_else_in_a_contact_room_starts_nothing(tmp_path: Path) -> None:
    """An administrator, a stranger, a forged relay, and an agent that is no party are refused before access."""
    config, paths = _setup(tmp_path)
    _contact(paths)
    validator = _validator(config, paths)
    carol_agent = _agent(paths, "mind_carol")
    events = [
        _event(ADMIN),
        _event(CAROL),
        # Only a managed sender may name an original sender.
        _event(CAROL, **_relay(paths, OWNER)),
        _event(carol_agent, **_relay(paths, CAROL)),
    ]

    for event in events:
        facts = validator.classify(event, room_id=CONTACT_ROOM)
        assert facts.contact_id == "c1"
        assert facts.run_requester_id != CO_OWNER
        with capture_logs() as logs:
            assert not await _precheck(validator, event, facts)
        assert [log["event"] for log in logs] == ["contact_room_sender_refused"]
    assert validator.deps.turn_store.record_turn.await_count == len(events)


@pytest.mark.asyncio
async def test_the_agents_own_messages_in_its_contact_room_stay_dropped(tmp_path: Path) -> None:
    """An agent's own messages in its contact room are its echo, its replies written for its owner included."""
    config, paths = _setup(tmp_path)
    _contact(paths)
    validator = _validator(config, paths)
    bob_agent = _agent(paths, "mind_bob")
    events = [
        _event(bob_agent, **{SKIP_MENTIONS_KEY: True}),
        # Every reply of a turn run as the owner names the owner it was written for.
        _event(bob_agent, **{ACTING_REQUESTER_KEY: CO_OWNER}),
    ]

    for event in events:
        facts = validator.classify(event, room_id=CONTACT_ROOM)
        assert (facts.lane_requester_id, facts.contact_id) == (bob_agent, None)
        with capture_logs() as logs:
            assert not await _precheck(validator, event, facts)
        assert logs == []
    validator.deps.turn_store.record_turn.assert_not_awaited()


@pytest.mark.asyncio
@pytest.mark.usefixtures("enforce_turn_authorization")
async def test_a_message_in_a_removed_contacts_room_starts_nothing(tmp_path: Path) -> None:
    """After a removal, or a leave that failed, nobody's message there starts a turn, not even the agent's owner's."""
    config, paths = _setup(tmp_path)
    _contact(paths, status="removed")
    validator = _validator(config, paths)
    bob_agent = _agent(paths, "mind_bob")
    events = [
        _event(_agent(paths, "mind_alice"), **_relay(paths, OWNER)),
        _event(OWNER),
        _event(CO_OWNER, **{"m.mentions": {"user_ids": [bob_agent]}}),
    ]

    for event in events:
        facts = validator.classify(event, room_id=CONTACT_ROOM)
        assert (facts.contact_id, facts.contact_refused) == ("c1", True)
        with capture_logs() as logs:
            assert not await _precheck(validator, event, facts)
        assert [log["event"] for log in logs] == ["contact_room_sender_refused"]
    assert validator.deps.turn_store.record_turn.await_count == len(events)


def _later(sender: str, event_id: str, **content: object) -> ResolvedVisibleMessage:
    """Return a message the thread history shows after the turn under question."""
    return ResolvedVisibleMessage(
        sender=sender,
        body="Friday works.",
        timestamp=2_000,
        event_id=event_id,
        content={"msgtype": "m.text", "body": "Friday works.", **content},
        thread_id=None,
        latest_event_id=event_id,
    )


@pytest.mark.asyncio
async def test_a_contact_turn_is_superseded_only_by_its_own_speaker(tmp_path: Path) -> None:
    """Every sender in a contact room runs as the owner, yet only a newer message of the same speaker supersedes one."""
    config, paths = _setup(tmp_path)
    _contact(paths)
    validator = _validator(config, paths)
    alice_agent = _agent(paths, "mind_alice")
    own_reply = _later(_agent(paths, "mind_bob"), "$own-reply", **{ACTING_REQUESTER_KEY: CO_OWNER})
    own_relay = _later(_agent(paths, "mind_bob"), "$own-relay", **_relay(paths, CO_OWNER, recipient="mind_alice"))

    async def superseded(turn_sender: str, later: list[ResolvedVisibleMessage]) -> bool:
        turn = PreparedIngress(
            sender=turn_sender,
            event_id="$turn",
            body="Are you free on Friday?",
            source={"content": {"msgtype": "m.text", "body": "Are you free on Friday?"}},
            server_timestamp=1_000,
        )
        facts = validator.classify(turn, room_id=CONTACT_ROOM)
        return await has_newer_unresponded_in_thread(
            turn,
            facts.run_identity,
            later,
            may_be_superseded_by_newer_requester_turn=True,
            classify=partial(validator.classify_source, room_id=CONTACT_ROOM),
            is_handled=AsyncMock(return_value=frozenset()),
            logger=get_logger(__name__),
        )

    others = [own_reply, own_relay, _later(CO_OWNER, "$bob"), _later(alice_agent, "$relay")]
    assert not await superseded(OWNER, others)
    assert await superseded(OWNER, [_later(OWNER, "$alice")])
    assert not await superseded(CO_OWNER, [own_reply, _later(OWNER, "$alice"), _later(alice_agent, "$relay")])
    assert await superseded(CO_OWNER, [_later(CO_OWNER, "$bob")])
