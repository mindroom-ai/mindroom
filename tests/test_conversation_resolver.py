"""Unit tests for conversation identity and ingress envelope assembly.

These are characterization tests for ConversationResolver: they pin down
the one thread decision ingress makes, reply-chain fallback, the context read
in the decided thread, target building, and the per-turn cache scope.
"""

from __future__ import annotations

from contextlib import asynccontextmanager
from dataclasses import dataclass, field, replace
from types import SimpleNamespace
from typing import TYPE_CHECKING, Any, cast
from unittest.mock import AsyncMock

import nio
import pytest

from mindroom.config.agent import AgentConfig
from mindroom.config.main import Config
from mindroom.constants import ATTACHMENT_IDS_KEY, SKIP_MENTIONS_KEY
from mindroom.conversation_resolver import ConversationResolver, ConversationResolverDeps
from mindroom.dispatch_source import TRUSTED_INTERNAL_RELAY_SOURCE_KIND
from mindroom.entity_resolution import entity_identity_registry
from mindroom.event_journal import (
    ConversationCursor,
    ConversationPage,
    EventClass,
    EventJournalStore,
    EventKind,
    RefreshRequest,
    VisibleMessage,
)
from mindroom.logging_config import get_logger
from mindroom.matrix.conversation_hydration import ConversationHydrator
from mindroom.matrix.conversation_reads import ConversationReader
from mindroom.matrix.identity import MatrixID
from mindroom.matrix.journal_ingress import _inbound_event, _projected_event
from mindroom.matrix.relation_lookup import RelationLookup
from mindroom.matrix.thread_membership import RelatedEventUnavailableError, ThreadMembershipLookupError
from mindroom.message_target import MessageTarget
from mindroom.turn_origin import TurnTrust
from tests.conftest import (
    bind_runtime_paths,
    make_ingress_facts,
    make_matrix_client_mock,
    make_relation_lookup,
    runtime_paths_for,
    test_runtime_paths,
)

if TYPE_CHECKING:
    from collections.abc import AsyncIterator, Callable
    from pathlib import Path

_ROOM_ID = "!test:localhost"
_SENDER = "@user:localhost"
_HUMAN_USER_ID = "@human:localhost"
_BOT_USER_ID = "@mindroom_general:localhost"
_EVENT_ID = "$event:localhost"
_THREAD_ROOT = "$root:localhost"
_PARENT = "$parent:localhost"
_CHILD = "$child:localhost"


@dataclass(frozen=True)
class _RuntimeStub:
    """Minimal SupportsClientConfig stand-in for resolver tests."""

    client: nio.AsyncClient | None
    config: Config


@pytest.fixture
def config(tmp_path: Path) -> Config:
    """Single-agent config bound to isolated runtime paths."""
    return bind_runtime_paths(
        Config(agents={"general": AgentConfig(display_name="General")}),
        test_runtime_paths(tmp_path),
    )


def _conversation_reader(*messages: VisibleMessage) -> ConversationReader:
    """Return a reader over a fixed page, for a harness with no journal store.

    Not the same thing as stubbing a reader that has a real store behind it:
    these harnesses build a resolver directly, so there is no projection to
    reach and a fixed page is the honest analogue of the conversation-cache
    mock they already carry.
    """
    page = ConversationPage(messages=messages, refresh_pending=(), next_cursor=None)
    return cast(
        "ConversationReader",
        SimpleNamespace(
            may_have_unread_history=AsyncMock(return_value=False),
            hydration_was_truncated=AsyncMock(return_value=False),
            read=AsyncMock(return_value=page),
            read_strict=AsyncMock(return_value=page),
        ),
    )


def _projected(event_id: str, body: str) -> VisibleMessage:
    """Return one projected message in the thread the reply targets."""
    return VisibleMessage(
        logical_event_id=event_id,
        room_id=_ROOM_ID,
        thread_id=_PARENT,
        sender=_SENDER,
        created_ts=1_000,
        revision_event_id=event_id,
        revision_ts=1_000,
        content={"msgtype": "m.text", "body": body},
    )


@dataclass
class _ClientWithoutEvents:
    """A homeserver that has never heard of the event being asked about."""

    async def room_get_event(self, room_id: str, event_id: str) -> nio.RoomGetEventError:
        """Report the event as missing."""
        del room_id, event_id
        return nio.RoomGetEventError("not found", "M_NOT_FOUND")


@dataclass
class _ClientCountingLookups:
    """A homeserver that records how many point lookups it was asked for."""

    lookups: int = 0

    async def room_get_event(self, room_id: str, event_id: str) -> nio.RoomGetEventError:
        """Count one lookup and report the event as missing."""
        del room_id, event_id
        self.lookups += 1
        return nio.RoomGetEventError("not found", "M_NOT_FOUND")


def _resolver(
    config: Config,
    *,
    relations: RelationLookup | None = None,
    conversation_reader: ConversationReader | None = None,
) -> ConversationResolver:
    runtime_paths = runtime_paths_for(config)
    registry = entity_identity_registry(config, runtime_paths)
    return ConversationResolver(
        ConversationResolverDeps(
            runtime=_RuntimeStub(client=make_matrix_client_mock(), config=config),
            logger=get_logger("test_conversation_resolver"),
            runtime_paths=runtime_paths,
            agent_name="general",
            matrix_id=registry.current_id("general"),
            relations=relations or make_relation_lookup(),
            conversation_reader=conversation_reader or _conversation_reader(),
        ),
    )


def _event(content: dict[str, Any], *, event_id: str = _EVENT_ID) -> nio.RoomMessageText:
    source = {
        "content": {"msgtype": "m.text", **content},
        "event_id": event_id,
        "sender": _SENDER,
        "origin_server_ts": 1_000_000,
        "room_id": _ROOM_ID,
        "type": "m.room.message",
    }
    return nio.RoomMessageText.from_dict(source)


def _threaded_event(body: str = "in thread") -> nio.RoomMessageText:
    return _event(
        {
            "body": body,
            "m.relates_to": {"rel_type": "m.thread", "event_id": _THREAD_ROOT},
        },
    )


def _reply_event(body: str = "a reply") -> nio.RoomMessageText:
    return _event(
        {
            "body": body,
            "m.relates_to": {"m.in_reply_to": {"event_id": _PARENT}},
        },
    )


def _room() -> nio.MatrixRoom:
    room = nio.MatrixRoom(_ROOM_ID, "@mindroom_general:localhost")
    # A synced cache is what makes an absent mention mean absent rather than "not fetched yet".
    room.members_synced = True
    return room


@pytest.mark.asyncio
@pytest.mark.parametrize("incomplete", ["refresh", "cursor", "truncated"])
async def test_dispatch_repairs_pending_refresh_before_counting_participants(config: Config, incomplete: str) -> None:
    """Repair a withheld streamed reply before planning, without retrying permanent bounds."""
    messages = (
        replace(_projected(_THREAD_ROOT, "First participant"), thread_id=_THREAD_ROOT),
        replace(_projected("$second", "Second participant"), thread_id=_THREAD_ROOT, sender=_HUMAN_USER_ID),
    )
    reader = _conversation_reader(*messages)
    reader.read.return_value = ConversationPage(  # type: ignore[attr-defined]
        messages=messages,
        refresh_pending=(RefreshRequest(_ROOM_ID, _THREAD_ROOT, "$answer", "$final-edit", 1, 1),)
        if incomplete == "refresh"
        else (),
        next_cursor=ConversationCursor(500, "$older") if incomplete == "cursor" else None,
    )
    reader.hydration_was_truncated.return_value = incomplete == "truncated"  # type: ignore[attr-defined]
    resolver = _resolver(config, conversation_reader=reader)

    context = await resolver.extract_dispatch_context(_room(), _threaded_event(), thread_id=_THREAD_ROOT)

    if incomplete == "refresh":
        reader.read_strict.assert_awaited_once()  # type: ignore[attr-defined]
        assert not context.planning_thread_history_unavailable
        assert {message.sender for message in context.planning_thread_history} == {_SENDER, _HUMAN_USER_ID}
    else:
        reader.read_strict.assert_not_awaited()  # type: ignore[attr-defined]
        assert context.planning_thread_history_unavailable


@pytest.mark.asyncio
async def test_threaded_event_resolves_explicit_thread_root(config: Config) -> None:
    """An m.thread relation is authoritative for the turn's thread and its context."""
    resolver = _resolver(config)

    thread_id = await resolver.coalescing_thread_id(_room(), _threaded_event())
    context = await resolver.extract_dispatch_context(_room(), _threaded_event(), thread_id=thread_id)

    assert thread_id == _THREAD_ROOT
    assert context.is_thread is True
    assert context.thread_id == _THREAD_ROOT
    assert context.requires_model_history_refresh is False


@pytest.mark.asyncio
async def test_reply_chain_inherits_the_thread_the_journal_recorded(config: Config) -> None:
    """A plain reply inherits the thread its parent was admitted into."""
    resolver = _resolver(config, relations=make_relation_lookup(threads={_PARENT: _THREAD_ROOT}))

    assert await resolver.coalescing_thread_id(_room(), _reply_event()) == _THREAD_ROOT


@pytest.mark.asyncio
async def test_reply_to_proven_thread_root_joins_that_thread(config: Config) -> None:
    """Replying to an event that provably has thread children resolves to that thread."""
    resolver = _resolver(
        config,
        conversation_reader=_conversation_reader(_projected("$child:localhost", "child")),
    )

    thread_id = await resolver.coalescing_thread_id(_room(), _reply_event())
    context = await resolver.extract_dispatch_context(_room(), _reply_event(), thread_id=thread_id)

    assert thread_id == _PARENT
    assert context.is_thread is True
    assert [message.event_id for message in context.thread_history] == ["$child:localhost"]


@pytest.mark.asyncio
async def test_reaction_target_thread_is_read_from_local_state_while_history_is_unread(config: Config) -> None:
    """A reaction's hook context wants its target's thread, which a page with unread history still proves."""
    reader = _conversation_reader(_projected(_CHILD, "child"))
    reader.may_have_unread_history.return_value = True  # type: ignore[attr-defined]
    resolver = _resolver(config, conversation_reader=reader)

    assert await resolver.resolve_related_event_thread_id_dispatch_snapshot_best_effort(_ROOM_ID, _PARENT) == _PARENT


@pytest.mark.asyncio
async def test_reply_to_plain_message_demotes_to_room_level(config: Config) -> None:
    """Replying to a childless event stays room-level with a room-level delivery target."""
    resolver = _resolver(config)
    reply = _reply_event()

    thread_id = await resolver.coalescing_thread_id(_room(), reply)
    target = resolver.build_message_target(
        room_id=_ROOM_ID,
        thread_id=thread_id,
        reply_to_event_id=reply.event_id,
        event_source=reply.source,
    )

    assert thread_id is None
    # A reply event carries a relation, so per MSC3440 it cannot become a thread root itself.
    assert target.source_thread_id is None
    assert target.resolved_thread_id is None
    assert target.reply_to_event_id == _EVENT_ID


@pytest.mark.asyncio
async def test_reply_to_a_parent_the_homeserver_will_not_serve_is_unplaceable(config: Config) -> None:
    """A parent the homeserver will not serve cannot place the turn, so ingress ends it rather than guess a thread."""
    resolver = _resolver(config, relations=make_relation_lookup(client=_ClientWithoutEvents()))

    with pytest.raises(RelatedEventUnavailableError):
        await resolver.coalescing_thread_id(_room(), _reply_event())


@pytest.mark.parametrize(("thread_mode", "one_conversation"), [("thread", False), ("room", True)])
def test_room_is_one_conversation_only_in_room_thread_mode(
    tmp_path: Path,
    thread_mode: str,
    one_conversation: bool,
) -> None:
    """Room thread mode makes the whole room, threads included, one conversation."""
    config = bind_runtime_paths(
        Config(agents={"general": AgentConfig(display_name="General", thread_mode=thread_mode)}),
        test_runtime_paths(tmp_path),
    )

    assert _resolver(config).room_is_one_conversation(_ROOM_ID) is one_conversation


@pytest.mark.asyncio
async def test_room_thread_mode_decides_every_message_room_level(tmp_path: Path) -> None:
    """Agents in room thread mode place an ordinary threaded message at room level without reading its thread."""
    config = bind_runtime_paths(
        Config(agents={"general": AgentConfig(display_name="General", thread_mode="room")}),
        test_runtime_paths(tmp_path),
    )
    reader = _conversation_reader()
    resolver = _resolver(config, conversation_reader=reader)

    thread_id = await resolver.coalescing_thread_id(_room(), _threaded_event())

    assert thread_id is None
    reader.read.assert_not_awaited()  # type: ignore[attr-defined]
    reader.read_strict.assert_not_awaited()  # type: ignore[attr-defined]


@pytest.mark.asyncio
async def test_a_thread_the_target_names_in_a_room_mode_room_is_read_like_any_other(tmp_path: Path) -> None:
    """Context reads any thread it is given in a room-mode room, which is where a per-fire thread arrives."""
    config = bind_runtime_paths(
        Config(agents={"general": AgentConfig(display_name="General", thread_mode="room")}),
        test_runtime_paths(tmp_path),
    )
    reader = _conversation_reader()
    resolver = _resolver(config, conversation_reader=reader)

    context = await resolver.extract_dispatch_context(_room(), _threaded_event(), thread_id=_THREAD_ROOT)

    assert (context.is_thread, context.thread_id) == (True, _THREAD_ROOT)
    reader.read.assert_awaited_once()  # type: ignore[attr-defined]


@pytest.mark.asyncio
async def test_router_relay_context_ignores_the_thread_a_relayed_edit_names(config: Config) -> None:
    """A relayed edit cannot choose the thread the response is delivered into.

    The relation written inside an ``m.new_content`` is one Matrix ignores, placing an edit by the
    event it replaces. Reading it would let whoever authored the relayed payload pick the thread,
    so the relay context takes the thread ingress decided from the edited original.
    """
    resolver = _resolver(config, relations=make_relation_lookup(threads={_PARENT: None}))
    relayed_edit = _event(
        {
            "body": "* updated",
            "m.new_content": {
                "body": "updated",
                "msgtype": "m.text",
                "m.relates_to": {"rel_type": "m.thread", "event_id": "$claimed:localhost"},
            },
            "m.relates_to": {"rel_type": "m.replace", "event_id": _PARENT},
        },
    )

    thread_id = await resolver.coalescing_thread_id(_room(), relayed_edit)
    context = await resolver.extract_trusted_router_relay_context(_room(), relayed_edit, thread_id=thread_id)

    assert thread_id is None
    assert context.thread_id is None
    assert context.is_thread is False
    assert context.requires_model_history_refresh is False


@pytest.mark.asyncio
async def test_router_relay_context_keeps_the_relays_own_thread_relation(config: Config) -> None:
    """A relay in a thread keeps it and defers the thread read until after the lock."""
    reader = _conversation_reader()
    resolver = _resolver(config, conversation_reader=reader)

    context = await resolver.extract_trusted_router_relay_context(_room(), _threaded_event(), thread_id=_THREAD_ROOT)

    assert context.thread_id == _THREAD_ROOT
    assert context.is_thread is True
    assert context.thread_history == ()
    assert context.requires_model_history_refresh is True
    assert context.planning_thread_history_unavailable is True
    reader.read.assert_not_awaited()  # type: ignore[attr-defined]
    reader.read_strict.assert_not_awaited()  # type: ignore[attr-defined]


@pytest.mark.asyncio
async def test_coalescing_thread_id_for_threaded_and_room_level_events(config: Config) -> None:
    """Coalescing scope follows canonical thread membership."""
    resolver = _resolver(config)

    assert await resolver.coalescing_thread_id(_room(), _threaded_event()) == _THREAD_ROOT
    assert await resolver.coalescing_thread_id(_room(), _event({"body": "plain"})) is None


def test_build_message_target_for_thread_message(config: Config) -> None:
    """A known thread id resolves to thread-level delivery."""
    resolver = _resolver(config)

    target = resolver.build_message_target(
        room_id=_ROOM_ID,
        thread_id=_THREAD_ROOT,
        reply_to_event_id=_EVENT_ID,
    )

    assert target.resolved_thread_id == _THREAD_ROOT
    assert target.session_id == f"{_ROOM_ID}:{_THREAD_ROOT}"


def test_build_message_target_starts_thread_at_rootable_room_message(config: Config) -> None:
    """A room-level message that can be a thread root becomes the new thread root."""
    resolver = _resolver(config)
    event = _event({"body": "plain"})

    target = resolver.build_message_target(
        room_id=_ROOM_ID,
        thread_id=None,
        reply_to_event_id=_EVENT_ID,
        event_source=event.source,
    )

    assert target.source_thread_id is None
    assert target.resolved_thread_id == _EVENT_ID


def test_build_message_target_room_mode_override_stays_room_level(config: Config) -> None:
    """A room thread-mode override discards thread identity from the target."""
    resolver = _resolver(config)

    target = resolver.build_message_target(
        room_id=_ROOM_ID,
        thread_id=_THREAD_ROOT,
        reply_to_event_id=_EVENT_ID,
        thread_mode_override="room",
    )

    assert target.resolved_thread_id is None
    assert target.session_id == _ROOM_ID


@pytest.mark.asyncio
async def test_the_turn_scope_makes_one_turn_pay_for_one_lookup(config: Config) -> None:
    """The per-turn scope is what stops one turn refetching the same event."""
    client = _ClientCountingLookups()
    resolver = _resolver(config, relations=make_relation_lookup(client=client))

    async with resolver.turn_lookup_scope():
        for _ in range(2):
            with pytest.raises(RelatedEventUnavailableError):
                await resolver.coalescing_thread_id(_room(), _reply_event())
    scoped = client.lookups

    client.lookups = 0
    for _ in range(2):
        with pytest.raises(RelatedEventUnavailableError):
            await resolver.coalescing_thread_id(_room(), _reply_event())
    unscoped = client.lookups

    # The exact counts are the resolver's business; that the scope reduces them
    # is this seam's. What one turn costs per event is pinned against the real
    # lookup in `tests/test_relation_lookup.py`.
    assert scoped > 0
    assert scoped < unscoped


@pytest.mark.asyncio
async def test_dispatch_context_extracts_agent_mentions(config: Config) -> None:
    """m.mentions on the inbound event resolve to configured agent identities."""
    registry = entity_identity_registry(config, runtime_paths_for(config))
    general_id = registry.current_id("general")
    resolver = _resolver(config)
    event = _event(
        {
            "body": "hello @general",
            "m.mentions": {"user_ids": [general_id.full_id, _HUMAN_USER_ID]},
        },
    )

    context = await resolver.extract_dispatch_context(_room(), event, thread_id=None)

    assert context.am_i_mentioned is True
    assert [agent.full_id for agent in context.mentioned_agents] == [general_id.full_id]
    assert context.has_non_agent_mentions is False


@pytest.mark.parametrize(
    ("membership", "expected"),
    [
        ("absent", False),
        ("invited", False),
        ("joined", True),
    ],
)
@pytest.mark.asyncio
async def test_dispatch_context_only_counts_joined_non_agent_mentions(
    config: Config,
    membership: str,
    expected: bool,
) -> None:
    """Only a human who is joined to the room should suppress automatic agent replies."""
    resolver = _resolver(config)
    room = _room()
    if membership != "absent":
        room.add_member(
            _HUMAN_USER_ID,
            "Human",
            None,
            invited=membership == "invited",
        )
    event = _event(
        {
            "body": f"hello {_HUMAN_USER_ID}",
            "m.mentions": {"user_ids": [_HUMAN_USER_ID]},
        },
    )

    context = await resolver.extract_dispatch_context(room, event, thread_id=None)

    assert context.has_non_agent_mentions is expected


@pytest.mark.asyncio
async def test_skip_mentions_metadata_suppresses_mention_extraction(config: Config) -> None:
    """The skip-mentions content flag disables mention handling for one event."""
    registry = entity_identity_registry(config, runtime_paths_for(config))
    general_id = registry.current_id("general")
    resolver = _resolver(config)
    event = _event(
        {
            "body": "hello @general",
            "m.mentions": {"user_ids": [general_id.full_id]},
            SKIP_MENTIONS_KEY: True,
        },
    )

    context = await resolver.extract_dispatch_context(_room(), event, thread_id=None)

    assert context.am_i_mentioned is False
    assert context.mentioned_agents == []
    assert context.has_non_agent_mentions is False


@pytest.mark.asyncio
async def test_build_envelope_carries_event_identity(config: Config) -> None:
    """The envelope mirrors the inbound event and its classified provenance without thread extraction."""
    resolver = _resolver(config)
    event = _event({"body": "hello"})
    target = resolver.build_message_target(
        room_id=_ROOM_ID,
        thread_id=_THREAD_ROOT,
        reply_to_event_id=_EVENT_ID,
    )

    envelope = resolver.build_envelope(event=event, facts=make_ingress_facts(_SENDER), target=target)

    assert envelope.source_event_id == _EVENT_ID
    assert envelope.room_id == _ROOM_ID
    assert envelope.target == target
    assert envelope.requester_id == _SENDER
    assert envelope.sender_id == _SENDER
    assert envelope.body == "hello"
    assert envelope.mentioned_agents == ()
    assert envelope.agent_name == "general"
    assert envelope.source_kind == "message"


@pytest.mark.parametrize(("body", "expected_body"), [(None, "hello"), ("", "hello"), ("override", "override")])
def test_envelope_carries_mentions_body_fallback_attachments_and_relay_provenance(
    config: Config,
    body: str | None,
    expected_body: str,
) -> None:
    """The envelope keeps mentions, the body fallback, attachments, and the relay provenance its facts carry."""
    resolver = _resolver(config)
    event = _event({"body": "hello", ATTACHMENT_IDS_KEY: [" a ", "a", "b"]})
    target = MessageTarget.resolve(_ROOM_ID, _THREAD_ROOT, _EVENT_ID)
    facts = make_ingress_facts(
        _BOT_USER_ID,
        source_kind=TRUSTED_INTERNAL_RELAY_SOURCE_KIND,
        requester_user_id=_HUMAN_USER_ID,
        original_sender=_HUMAN_USER_ID,
        original_sender_is_human=True,
        hook_source="test-hook",
        message_received_depth=2,
        sender_entity_name="general",
    )

    envelope = resolver.build_envelope(
        event=event,
        facts=facts,
        target=target,
        mentioned_agents=[MatrixID.parse(_BOT_USER_ID)],
        body=body,
    )

    assert envelope.body == expected_body
    assert envelope.attachment_ids == ("a", "b")
    assert envelope.mentioned_agents == ("general",)
    assert envelope.hook_source == "test-hook"
    assert envelope.message_received_depth == 2
    assert envelope.origin == facts.origin
    assert envelope.origin.trust is TurnTrust.TRUSTED_USER_RELAY
    assert envelope.dispatch_policy_source_kind is None


def _parse(source: dict[str, Any]) -> nio.Event:
    event = nio.Event.parse_event(source)
    assert isinstance(event, nio.Event)
    return event


@dataclass
class _HomeserverWithAThread:
    """A homeserver holding a thread the journal has never been told about.

    ``$parent`` is relation-free and has one ``m.thread`` child, so it is a
    real thread root by MSC3440. Nothing about that is knowable locally, which
    is the whole point: it is what a room looks like before anything has walked
    the conversation the reply names.
    """

    _root: dict[str, Any] = field(
        default_factory=lambda: {
            "event_id": _PARENT,
            "sender": _SENDER,
            "origin_server_ts": 1_000,
            "type": "m.room.message",
            "room_id": _ROOM_ID,
            "content": {"msgtype": "m.text", "body": "thread root"},
        },
    )
    _child: dict[str, Any] = field(
        default_factory=lambda: {
            "event_id": _CHILD,
            "sender": _SENDER,
            "origin_server_ts": 1_100,
            "type": "m.room.message",
            "room_id": _ROOM_ID,
            "content": {
                "msgtype": "m.text",
                "body": "in the thread",
                "m.relates_to": {"rel_type": "m.thread", "event_id": _PARENT},
            },
        },
    )

    async def room_get_event(
        self,
        room_id: str,
        event_id: str,
    ) -> nio.RoomGetEventResponse | nio.RoomGetEventError:
        """Return one stored event."""
        del room_id
        source = {_PARENT: self._root, _CHILD: self._child}.get(event_id)
        if source is None:
            return nio.RoomGetEventError("not found", "M_NOT_FOUND")
        response = nio.RoomGetEventResponse()
        response.event = _parse(source)
        return response

    async def room_get_event_relations(
        self,
        *,
        room_id: str,
        event_id: str,
        direction: nio.MessageDirection = nio.MessageDirection.back,
        recurse: bool = False,
        minimum_recursion_depth: int | None = None,
    ) -> AsyncIterator[nio.Event]:
        """Yield the thread's one child."""
        del room_id, direction, recurse, minimum_recursion_depth
        if event_id == _PARENT:
            yield _parse(self._child)


@dataclass
class _HomeserverWithNoThread(_HomeserverWithAThread):
    """A homeserver whose ``$parent`` is an ordinary message with no children.

    The mirror of ``_HomeserverWithAThread``: the repair must be able to answer
    "not a thread root" as definitely as it answers the other way, or every
    reply to a plain message would open a thread on it.
    """

    async def room_get_event_relations(
        self,
        *,
        room_id: str,
        event_id: str,
        direction: nio.MessageDirection = nio.MessageDirection.back,
        recurse: bool = False,
        minimum_recursion_depth: int | None = None,
    ) -> AsyncIterator[nio.Event]:
        """Report that nothing relates to the candidate."""
        del room_id, event_id, direction, recurse, minimum_recursion_depth
        return
        yield  # pragma: no cover - unreachable, keeps this an async generator


@dataclass
class _HomeserverRefusingTheRelationWalk(_HomeserverWithAThread):
    """A homeserver that serves the event but will not answer for its relations.

    The one case that stays genuinely unprovable: the strict repair runs and
    still cannot say whether the candidate is a thread root, so the caller must
    fail closed rather than guess a coalescing scope.
    """

    async def room_get_event_relations(
        self,
        *,
        room_id: str,
        event_id: str,
        direction: nio.MessageDirection = nio.MessageDirection.back,
        recurse: bool = False,
        minimum_recursion_depth: int | None = None,
    ) -> AsyncIterator[nio.Event]:
        """Refuse the walk the way nio reports a server that ignored `recurse`."""
        del room_id, event_id, direction, recurse, minimum_recursion_depth
        raise nio.InsufficientRecursionDepthError(required=0, reported=None)
        yield  # pragma: no cover - unreachable, keeps this an async generator


@asynccontextmanager
async def _resolver_on_a_cold_journal(
    config: Config,
    tmp_path: Path,
    *,
    client: object | None = None,
) -> AsyncIterator[ConversationResolver]:
    """Yield a resolver whose only local knowledge is the inbound reply itself.

    A real store and a real reader rather than the fixed-page doubles the rest
    of this file uses, because the behaviour under test is what the projection
    reports about a conversation it holds nothing of. A double that answers a
    fixed page cannot express the difference between "empty" and "unknown",
    which is the difference being tested.
    """
    store = EventJournalStore.open_sqlite(tmp_path / "event_journal.db")
    try:
        principal = store.principal("agent@general")
        reply = _parse(_reply_event().source)
        await principal.admit(
            _inbound_event(_ROOM_ID, reply, EventKind.MESSAGE, EventClass.ACTIONABLE),
            _projected_event(_ROOM_ID, reply, EventKind.MESSAGE, self_sender=_BOT_USER_ID),
        )
        runtime = _RuntimeStub(
            client=cast("nio.AsyncClient", client if client is not None else _HomeserverWithAThread()),
            config=config,
        )
        runtime_paths = runtime_paths_for(config)
        registry = entity_identity_registry(config, runtime_paths)
        yield ConversationResolver(
            ConversationResolverDeps(
                runtime=runtime,
                logger=get_logger("test_conversation_resolver"),
                runtime_paths=runtime_paths,
                agent_name="general",
                matrix_id=registry.current_id("general"),
                relations=RelationLookup(store=principal, runtime=runtime),
                conversation_reader=ConversationReader(
                    store=principal,
                    hydrator=ConversationHydrator(
                        store=principal,
                        runtime=runtime,
                        self_sender=_BOT_USER_ID,
                    ),
                ),
            ),
        )
    finally:
        await store.close()


@pytest.mark.asyncio
async def test_unhydrated_candidate_root_is_not_demoted_to_room_level(
    config: Config,
    tmp_path: Path,
) -> None:
    """The repaired read must answer the real thread, not merely stop raising.

    A repair that resolved room level would keep every reply into an existing
    thread out of it, which is the failure this whole path exists to prevent.
    The history proves the answer came from the hydrated thread rather than
    from the empty page the dispatch-safe read started with.
    """
    async with _resolver_on_a_cold_journal(config, tmp_path) as resolver:
        thread_id = await resolver.coalescing_thread_id(_room(), _reply_event())
        context = await resolver.extract_dispatch_context(_room(), _reply_event(), thread_id=thread_id)

    assert thread_id == _PARENT
    assert context.is_thread is True
    assert [message.event_id for message in context.thread_history] == [_PARENT, _CHILD]


@pytest.mark.asyncio
async def test_unhydrated_childless_candidate_resolves_to_the_room(
    config: Config,
    tmp_path: Path,
) -> None:
    """The repair answers "not a thread root" as definitely as it answers the other way.

    Same cold journal, same unproven candidate; only the server's answer
    differs. A repair that could only ever say "threaded" would open a thread
    on every plain message anybody replied to.
    """
    async with _resolver_on_a_cold_journal(config, tmp_path, client=_HomeserverWithNoThread()) as resolver:
        scope = await resolver.coalescing_thread_id(_room(), _reply_event())

    assert scope is None


@pytest.mark.asyncio
async def test_coalescing_still_fails_closed_when_the_repair_cannot_prove_the_root(
    config: Config,
    tmp_path: Path,
) -> None:
    """A repair that cannot answer must not invent a scope.

    The homeserver serves the candidate but refuses the relation walk, so even
    the strict read cannot say whether it is a thread root. Guessing would put
    the batch under the wrong key with nothing later to correct it.
    """
    async with _resolver_on_a_cold_journal(
        config,
        tmp_path,
        client=_HomeserverRefusingTheRelationWalk(),
    ) as resolver:
        with pytest.raises(ThreadMembershipLookupError):
            await resolver.coalescing_thread_id(_room(), _reply_event())


@dataclass
class _HomeserverRefusingEveryEvent(_HomeserverWithAThread):
    """A homeserver that answers every event lookup with one fixed error."""

    errcode: str = "M_NOT_FOUND"

    async def room_get_event(
        self,
        room_id: str,
        event_id: str,
    ) -> nio.RoomGetEventResponse | nio.RoomGetEventError:
        """Refuse the event, whatever it is."""
        del room_id, event_id
        return nio.RoomGetEventError("refused", self.errcode)


def _reference_event() -> nio.RoomMessageText:
    return _event(
        {
            "body": "a reference",
            "m.relates_to": {"rel_type": "m.reference", "event_id": "$fabricated:localhost"},
        },
    )


@pytest.mark.asyncio
@pytest.mark.parametrize("errcode", ["M_NOT_FOUND", "M_FORBIDDEN"])
@pytest.mark.parametrize("make_event", [_reply_event, _reference_event])
async def test_coalescing_reports_a_relation_target_the_homeserver_will_not_serve(
    config: Config,
    tmp_path: Path,
    errcode: str,
    make_event: Callable[[], nio.RoomMessageText],
) -> None:
    """A target that is missing or hidden fails the same way on every retry.

    Anyone in the room can name one, so the caller has to be able to tell it
    from a lookup that failed for a moment, or one message holds up the room.
    """
    async with _resolver_on_a_cold_journal(
        config,
        tmp_path,
        client=_HomeserverRefusingEveryEvent(errcode=errcode),
    ) as resolver:
        with pytest.raises(RelatedEventUnavailableError):
            await resolver.coalescing_thread_id(_room(), make_event())


@dataclass
class _HomeserverServingAStateEvent(_HomeserverWithAThread):
    """A homeserver whose ``$parent`` is a membership event, not a message."""

    async def room_get_event(
        self,
        room_id: str,
        event_id: str,
    ) -> nio.RoomGetEventResponse | nio.RoomGetEventError:
        """Serve a join event under the ID every relation in these tests names."""
        del room_id, event_id
        response = nio.RoomGetEventResponse()
        response.event = _parse(
            {
                "event_id": _PARENT,
                "sender": _SENDER,
                "origin_server_ts": 1_000,
                "type": "m.room.member",
                "state_key": _SENDER,
                "room_id": _ROOM_ID,
                "content": {"membership": "join"},
            },
        )
        return response


@pytest.mark.asyncio
async def test_coalescing_reports_a_relation_target_that_is_not_a_message(
    config: Config,
    tmp_path: Path,
) -> None:
    """An event's type never changes, so a reply to a join fails the same way on every retry."""
    async with _resolver_on_a_cold_journal(
        config,
        tmp_path,
        client=_HomeserverServingAStateEvent(),
    ) as resolver:
        with pytest.raises(RelatedEventUnavailableError):
            await resolver.coalescing_thread_id(_room(), _reply_event())


@pytest.mark.asyncio
async def test_coalescing_keeps_a_server_failure_retryable(
    config: Config,
    tmp_path: Path,
) -> None:
    """A homeserver that failed to answer may answer next time, so it is not a refusal."""
    async with _resolver_on_a_cold_journal(
        config,
        tmp_path,
        client=_HomeserverRefusingEveryEvent(errcode="M_UNKNOWN"),
    ) as resolver:
        with pytest.raises(ThreadMembershipLookupError) as raised:
            await resolver.coalescing_thread_id(_room(), _reply_event())

    assert not isinstance(raised.value, RelatedEventUnavailableError)


@pytest.mark.asyncio
@pytest.mark.parametrize("thread_mode", ["thread", "room"])
async def test_edit_context_reads_its_recorded_thread_strictly_whatever_the_thread_mode(
    tmp_path: Path,
    thread_mode: str,
) -> None:
    """Edit regeneration reads the recorded thread strictly, even after its room switched to room mode."""
    config = bind_runtime_paths(
        Config(agents={"general": AgentConfig(display_name="General", thread_mode=thread_mode)}),
        test_runtime_paths(tmp_path),
    )
    reader = _conversation_reader()
    resolver = _resolver(config, conversation_reader=reader)

    context = await resolver.extract_message_context(_room(), _event({"body": "edited"}), thread_id=_THREAD_ROOT)

    assert (context.is_thread, context.thread_id) == (True, _THREAD_ROOT)
    reader.read_strict.assert_awaited_once()
    reader.read.assert_not_awaited()


@pytest.mark.asyncio
@pytest.mark.parametrize(("next_cursor", "refresh"), [(None, False), (ConversationCursor(500, "$older"), True)])
async def test_dispatch_context_asks_for_a_model_history_refresh_only_for_a_partial_thread(
    config: Config,
    next_cursor: ConversationCursor | None,
    refresh: bool,
) -> None:
    """A page of a longer thread is not the thread, so the prompt must read the rest before answering."""
    page = ConversationPage(messages=(_projected(_PARENT, "first"),), refresh_pending=(), next_cursor=next_cursor)
    reader = _conversation_reader()
    reader.read.return_value = page
    reader.read_strict.return_value = page
    resolver = _resolver(config, conversation_reader=reader)

    context = await resolver.extract_dispatch_context(_room(), _event({"body": "next"}), thread_id=_PARENT)

    assert context.thread_id == _PARENT
    assert context.requires_model_history_refresh is refresh
