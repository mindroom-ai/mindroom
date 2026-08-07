"""Unit tests for conversation identity and ingress envelope assembly.

These are characterization tests for ConversationResolver: they pin down
thread root resolution, reply-chain fallback, candidate demotion, target
building, and the per-turn cache scope so the planned refactor of this layer
has a direct safety net.
"""

from __future__ import annotations

from contextlib import asynccontextmanager
from dataclasses import dataclass, field
from types import SimpleNamespace
from typing import TYPE_CHECKING, Any, cast
from unittest.mock import AsyncMock

import nio
import pytest

from mindroom.config.agent import AgentConfig
from mindroom.config.main import Config
from mindroom.constants import SKIP_MENTIONS_KEY
from mindroom.conversation_resolver import ConversationResolver, ConversationResolverDeps
from mindroom.entity_resolution import entity_identity_registry
from mindroom.event_journal import (
    ConversationPage,
    EventClass,
    EventJournalStore,
    EventKind,
    VisibleMessage,
)
from mindroom.logging_config import get_logger
from mindroom.matrix.conversation_hydration import ConversationHydrator
from mindroom.matrix.conversation_reads import ConversationReader
from mindroom.matrix.journal_ingress import inbound_event, projected_event
from mindroom.matrix.relation_lookup import RelationLookup
from mindroom.matrix.thread_membership import ThreadMembershipLookupError
from tests.conftest import (
    bind_runtime_paths,
    make_matrix_client_mock,
    make_relation_lookup,
    runtime_paths_for,
    test_runtime_paths,
)

if TYPE_CHECKING:
    from collections.abc import AsyncIterator
    from pathlib import Path

_ROOM_ID = "!test:localhost"
_SENDER = "@user:localhost"
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


def _empty_conversation_reader() -> ConversationReader:
    """Return a reader for a harness that has no journal store behind it.

    Not the same thing as stubbing a reader that does: these harnesses build a
    resolver directly, so there is no projection to reach and a fake page is
    the honest analogue of the conversation-cache mock they already carry.
    """
    page = ConversationPage(messages=(), refresh_pending=(), next_cursor=None)
    return cast(
        "ConversationReader",
        SimpleNamespace(
            may_have_unread_history=AsyncMock(return_value=False),
            hydration_was_truncated=AsyncMock(return_value=False),
            read=AsyncMock(return_value=page),
            read_strict=AsyncMock(return_value=page),
        ),
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
            conversation_reader=conversation_reader or _empty_conversation_reader(),
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
    return nio.MatrixRoom(_ROOM_ID, "@mindroom_general:localhost")


@pytest.mark.asyncio
async def test_threaded_event_resolves_explicit_thread_root(config: Config) -> None:
    """An m.thread relation is authoritative for thread identity and the delivery target."""
    resolver = _resolver(config)

    result = await resolver.extract_dispatch_context(_room(), _threaded_event())

    assert result.context.is_thread is True
    assert result.context.thread_id == _THREAD_ROOT
    assert result.context.requires_model_history_refresh is False
    assert result.thread_context is not None
    assert result.thread_context.stable_target.resolved_thread_id == _THREAD_ROOT


@pytest.mark.asyncio
async def test_reply_chain_inherits_the_thread_the_journal_recorded(config: Config) -> None:
    """A plain reply inherits the thread its parent was admitted into."""
    resolver = _resolver(config, relations=make_relation_lookup(threads={_PARENT: _THREAD_ROOT}))

    result = await resolver.extract_dispatch_context(_room(), _reply_event())

    assert result.context.is_thread is True
    assert result.context.thread_id == _THREAD_ROOT
    assert result.thread_context is not None
    assert result.thread_context.stable_target.resolved_thread_id == _THREAD_ROOT


@pytest.mark.asyncio
async def test_reply_to_proven_thread_root_joins_that_thread(config: Config) -> None:
    """Replying to an event that provably has thread children resolves to that thread."""
    resolver = _resolver(
        config,
        conversation_reader=_conversation_reader(_projected("$child:localhost", "child")),
    )

    result = await resolver.extract_dispatch_context(_room(), _reply_event())

    assert result.context.is_thread is True
    assert result.context.thread_id == _PARENT
    assert [message.event_id for message in result.context.thread_history] == ["$child:localhost"]


@pytest.mark.asyncio
async def test_reply_to_plain_message_demotes_to_room_level(config: Config) -> None:
    """Replying to a childless event stays room-level with a room-level delivery target."""
    resolver = _resolver(config)

    result = await resolver.extract_dispatch_context(_room(), _reply_event())

    assert result.context.is_thread is False
    assert result.context.thread_id is None
    assert result.thread_context is not None
    assert result.thread_context.candidate_thread_root_id is None
    # A reply event carries a relation, so per MSC3440 it cannot become a thread root itself.
    assert result.thread_context.stable_target.source_thread_id is None
    assert result.thread_context.stable_target.resolved_thread_id is None
    assert result.thread_context.stable_target.reply_to_event_id == _EVENT_ID


@pytest.mark.asyncio
async def test_reply_to_missing_parent_keeps_unproven_candidate(config: Config) -> None:
    """An unresolvable parent demotes to room level but keeps the candidate for replay safety."""
    resolver = _resolver(config, relations=make_relation_lookup(client=_ClientWithoutEvents()))

    result = await resolver.extract_dispatch_context(_room(), _reply_event())

    assert result.context.is_thread is False
    assert result.context.thread_id is None
    assert result.thread_context is not None
    assert result.thread_context.candidate_thread_root_id == _PARENT
    assert result.thread_context.replay_guard_degraded is True
    # Unproven candidates must not adopt a thread target.
    assert result.thread_context.stable_target.resolved_thread_id is None


@pytest.mark.asyncio
async def test_room_thread_mode_skips_thread_resolution(tmp_path: Path) -> None:
    """Agents in room thread mode treat every message as room-level."""
    config = bind_runtime_paths(
        Config(agents={"general": AgentConfig(display_name="General", thread_mode="room")}),
        test_runtime_paths(tmp_path),
    )
    reader = _empty_conversation_reader()
    resolver = _resolver(config, conversation_reader=reader)

    result = await resolver.extract_dispatch_context(_room(), _threaded_event())

    assert result.context.is_thread is False
    assert result.context.thread_id is None
    assert result.thread_context is None
    reader.read.assert_not_awaited()  # type: ignore[attr-defined]
    reader.read_strict.assert_not_awaited()  # type: ignore[attr-defined]


@pytest.mark.asyncio
async def test_router_relay_context_ignores_the_thread_a_relayed_edit_names(config: Config) -> None:
    """A relayed edit cannot choose the thread the response is delivered into.

    This context deliberately skips the canonical resolver so the relay does not pay for thread
    hydration before the lock. That makes the relation written inside an ``m.new_content`` the one
    value here nothing would ever check - and Matrix ignores it in any case, placing an edit by the
    event it replaces. Reading it would let whoever authored the relayed payload pick the thread.
    """
    resolver = _resolver(config)
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

    result = await resolver.extract_trusted_router_relay_context(_room(), relayed_edit)

    assert result.context.thread_id is None
    assert result.context.is_thread is False


@pytest.mark.asyncio
async def test_router_relay_context_keeps_the_relays_own_thread_relation(config: Config) -> None:
    """A relay that really is in a thread still resolves to it without a lookup."""
    resolver = _resolver(config)

    result = await resolver.extract_trusted_router_relay_context(_room(), _threaded_event())

    assert result.context.thread_id == _THREAD_ROOT
    assert result.context.is_thread is True


@pytest.mark.asyncio
async def test_coalescing_thread_id_for_threaded_and_room_level_events(config: Config) -> None:
    """Coalescing scope follows canonical thread membership."""
    resolver = _resolver(config)

    assert await resolver.coalescing_thread_id(_room(), _threaded_event()) == _THREAD_ROOT
    assert await resolver.coalescing_thread_id(_room(), _event({"body": "plain"})) is None


@pytest.mark.asyncio
async def test_coalescing_thread_id_is_room_scoped_in_room_thread_mode(tmp_path: Path) -> None:
    """Room thread mode collapses coalescing scope to the room even for threaded events."""
    config = bind_runtime_paths(
        Config(agents={"general": AgentConfig(display_name="General", thread_mode="room")}),
        test_runtime_paths(tmp_path),
    )
    resolver = _resolver(config)

    assert await resolver.coalescing_thread_id(_room(), _threaded_event()) is None


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
        await resolver.extract_dispatch_context(_room(), _reply_event())
        await resolver.extract_dispatch_context(_room(), _reply_event())
    scoped = client.lookups

    client.lookups = 0
    await resolver.extract_dispatch_context(_room(), _reply_event())
    await resolver.extract_dispatch_context(_room(), _reply_event())
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
            "m.mentions": {"user_ids": [general_id.full_id, "@human:localhost"]},
        },
    )

    result = await resolver.extract_dispatch_context(_room(), event)

    assert result.context.am_i_mentioned is True
    assert [agent.full_id for agent in result.context.mentioned_agents] == [general_id.full_id]
    assert result.context.has_non_agent_mentions is True


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

    result = await resolver.extract_dispatch_context(_room(), event)

    assert result.context.am_i_mentioned is False
    assert result.context.mentioned_agents == []
    assert result.context.has_non_agent_mentions is False


@pytest.mark.asyncio
async def test_build_ingress_envelope_carries_event_identity(config: Config) -> None:
    """The lightweight ingress envelope mirrors the inbound event without thread extraction."""
    resolver = _resolver(config)
    event = _event({"body": "hello"})
    target = resolver.build_message_target(
        room_id=_ROOM_ID,
        thread_id=_THREAD_ROOT,
        reply_to_event_id=_EVENT_ID,
    )

    envelope = resolver.build_ingress_envelope(
        event=event,
        requester_user_id=_SENDER,
        target=target,
    )

    assert envelope.source_event_id == _EVENT_ID
    assert envelope.room_id == _ROOM_ID
    assert envelope.target == target
    assert envelope.requester_id == _SENDER
    assert envelope.sender_id == _SENDER
    assert envelope.body == "hello"
    assert envelope.mentioned_agents == ()
    assert envelope.agent_name == "general"
    assert envelope.source_kind == "message"


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
            inbound_event(_ROOM_ID, reply, EventKind.MESSAGE, EventClass.ACTIONABLE),
            projected_event(_ROOM_ID, reply, EventKind.MESSAGE, self_sender=_BOT_USER_ID),
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
async def test_unhydrated_candidate_root_still_gets_a_coalescing_scope(
    config: Config,
    tmp_path: Path,
) -> None:
    """A plain reply resolves its scope even when nothing has walked that conversation.

    Coalescing reads dispatch-safe, so an unhydrated conversation answers with
    a page that proves nothing, and an unproven root is INDETERMINATE. Raising
    there fails the whole turn, and coalescing is the one caller with nothing
    downstream to correct a wrong key with -- the batch is formed here -- so it
    repairs the answer with a strict read instead of giving up.
    """
    async with _resolver_on_a_cold_journal(config, tmp_path) as resolver:
        scope = await resolver.coalescing_thread_id(_room(), _reply_event())

    assert scope == _PARENT


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
        result = await resolver.extract_dispatch_context(_room(), _reply_event())

    assert result.context.is_thread is True
    assert result.context.thread_id == _PARENT
    assert [message.event_id for message in result.context.thread_history] == [_PARENT, _CHILD]


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
