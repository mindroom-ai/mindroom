"""Tools prove thread roots through the conversation projection, the reader turns use."""

from __future__ import annotations

from dataclasses import dataclass, field
from types import SimpleNamespace
from typing import TYPE_CHECKING, Any

import pytest

from mindroom.config.agent import AgentConfig
from mindroom.config.main import Config
from mindroom.custom_tools.attachment_helpers import resolve_current_room_thread_root
from mindroom.matrix.conversation_reads import ConversationReader, complete_thread_history
from mindroom.matrix.projected_thread_membership import resolve_thread_root_event_id
from mindroom.matrix.relation_lookup import RelationLookup
from mindroom.message_target import MessageTarget
from tests.authorization_helpers import make_test_tool_runtime_context
from tests.conftest import bind_runtime_paths, runtime_paths_for, test_runtime_paths
from tests.test_conversation_hydration import ALICE, ROOM, FakeClient, admit_all, hydrator, raw

if TYPE_CHECKING:
    from pathlib import Path

    import nio

    from mindroom.event_journal import EventJournalStore, PrincipalStore
    from mindroom.tool_system.runtime_context import ToolRuntimeContext


class _FilteredHistoryClient(FakeClient):
    """A homeserver that also accepts the event-type filter room scans send."""

    async def room_messages(
        self,
        room_id: str,
        start: str | None = None,
        direction: object = None,
        limit: int = 10,
        message_filter: object = None,
    ) -> nio.RoomMessagesResponse | nio.RoomMessagesError:
        del message_filter
        return await super().room_messages(room_id, start=start, direction=direction, limit=limit)


@pytest.fixture
def principal(journal_store: EventJournalStore) -> PrincipalStore:
    """Return the general agent's projection."""
    return journal_store.principal("agent@general")


def _context(tmp_path: Path, principal: PrincipalStore, client: FakeClient) -> ToolRuntimeContext:
    config = bind_runtime_paths(
        Config(agents={"general": AgentConfig(display_name="General")}),
        test_runtime_paths(tmp_path),
    )
    runtime = SimpleNamespace(client=client)
    return make_test_tool_runtime_context(
        agent_name="general",
        target=MessageTarget.resolve(room_id=ROOM, thread_id=None, reply_to_event_id=None),
        requester_id=ALICE,
        client=client,
        config=config,
        runtime_paths=runtime_paths_for(config),
        relations=RelationLookup(store=principal, runtime=runtime),  # type: ignore[arg-type]
        conversation_reader=ConversationReader(store=principal, hydrator=hydrator(principal, client)),
    )


def _thread_with_one_reply(**client_fields: object) -> FakeClient:
    root = raw("$root", "what should we do?", ts=1_000)
    reply = raw("$reply", "this", ts=2_000, thread_id="$root")
    return _FilteredHistoryClient(
        events={"$root": root, "$reply": reply},
        relations={"$root": [reply]},
        **client_fields,
    )


@pytest.mark.asyncio
async def test_root_proof_survives_an_empty_history_page_that_has_more_after_it(
    tmp_path: Path,
    principal: PrincipalStore,
) -> None:
    """An empty `/messages` page with an `end` token is not the end of the room, so the thread is still proven."""
    root = raw("$root", "what should we do?", ts=1_000)
    reply = raw("$reply", "this", ts=2_000, thread_id="$root")
    client = _thread_with_one_reply(pages=[([], "token-1"), ([reply, root], None)])

    root_id, error = await resolve_current_room_thread_root(_context(tmp_path, principal, client), "$root")

    assert (root_id, error) == ("$root", None)


@pytest.mark.asyncio
async def test_a_message_nobody_replied_to_in_a_thread_is_not_a_thread_root(
    tmp_path: Path,
    principal: PrincipalStore,
) -> None:
    """A relation-free message without threaded replies resolves to no thread, and the tool says so."""
    root = raw("$root", "what should we do?", ts=1_000)
    client = _FilteredHistoryClient(events={"$root": root}, pages=[([root], None)])

    root_id, error = await resolve_current_room_thread_root(_context(tmp_path, principal, client), "$root")

    assert root_id is None
    assert error == "Failed to resolve a canonical thread root for the target event."


@pytest.mark.asyncio
async def test_a_thread_reply_resolves_to_its_root(tmp_path: Path, principal: PrincipalStore) -> None:
    """A threaded reply names its root directly, with no proof read at all."""
    client = _thread_with_one_reply()

    root_id, error = await resolve_current_room_thread_root(_context(tmp_path, principal, client), "$reply")

    assert (root_id, error) == ("$root", None)
    assert client.history_pages == 0
    assert client.relation_calls == 0


@dataclass
class _RecordingClient(FakeClient):
    """A homeserver that also records which single events it was asked for."""

    fetched: list[str] = field(default_factory=list)

    async def room_get_event(
        self,
        room_id: str,
        event_id: str,
    ) -> nio.RoomGetEventResponse | nio.RoomGetEventError:
        self.fetched.append(event_id)
        return await super().room_get_event(room_id, event_id)


def _reply(event_id: str, *, to: str, ts: int) -> dict[str, Any]:
    """Return one plain rich reply, which carries no thread relation of its own."""
    source = raw(event_id, f"reply {event_id}", ts=ts)
    source["content"]["m.relates_to"] = {"m.in_reply_to": {"event_id": to}}
    return source


def _edit(event_id: str, *, replaces: str, ts: int, claimed_thread_id: str | None = None) -> dict[str, Any]:
    """Return one edit, optionally naming a thread inside its ``m.new_content``."""
    source = raw(event_id, f"edit {event_id}", ts=ts, replaces=replaces)
    if claimed_thread_id is not None:
        source["content"]["m.new_content"]["m.relates_to"] = {"rel_type": "m.thread", "event_id": claimed_thread_id}
    return source


def _thread(
    root_id: str,
    *relations: dict[str, Any],
    events: dict[str, dict[str, Any]] | None = None,
) -> _RecordingClient:
    """Return a homeserver holding one relation-free message and its relations, plus any other events given."""
    return _RecordingClient(
        events={root_id: raw(root_id, "what should we do?", ts=1_000), **(events or {})},
        relations={root_id: list(relations)},
    )


def _reader(principal: PrincipalStore, client: FakeClient) -> ConversationReader:
    return ConversationReader(store=principal, hydrator=hydrator(principal, client))


async def _resolve(principal: PrincipalStore, client: FakeClient, event_id: str) -> str | None:
    return await resolve_thread_root_event_id(
        _reader(principal, client),
        RelationLookup(store=principal, runtime=SimpleNamespace(client=client)),  # type: ignore[arg-type]
        ROOM,
        event_id,
    )


async def _complete_thread(principal: PrincipalStore, client: FakeClient, thread_id: str) -> list[str]:
    """Return the event IDs of one thread's history, which must be the whole thread.

    A root proof reading this history finds no child exactly when it holds only
    the root, and a whole history is what lets that disprove the root rather
    than leave the proof unavailable.
    """
    history = await complete_thread_history(_reader(principal, client), ROOM, thread_id)
    assert history.is_full_history
    return [message.event_id for message in history]


@pytest.mark.asyncio
@pytest.mark.parametrize("event_id", ["", "   "])
async def test_a_blank_event_id_resolves_to_no_thread_without_asking_the_homeserver(
    principal: PrincipalStore,
    event_id: str,
) -> None:
    """A blank target is rejected before any event lookup."""
    client = _RecordingClient()

    assert await _resolve(principal, client, event_id) is None
    assert client.fetched == []


@pytest.mark.asyncio
async def test_a_plain_reply_to_a_thread_reply_resolves_to_that_thread(principal: PrincipalStore) -> None:
    """A bridge's plain reply to a threaded message belongs to the thread it answers, with no proof read."""
    thread_reply = raw("$thread-reply", "reply", ts=2_000, thread_id="$root")
    client = _thread(
        "$root",
        thread_reply,
        events={"$thread-reply": thread_reply, "$plain-reply": _reply("$plain-reply", to="$thread-reply", ts=3_000)},
    )

    assert await _resolve(principal, client, "$plain-reply") == "$root"
    assert client.fetched == ["$plain-reply", "$thread-reply"]
    assert client.relation_calls == 0


@pytest.mark.asyncio
async def test_a_plain_reply_walks_transitively_to_a_threaded_ancestor(principal: PrincipalStore) -> None:
    """A chain of plain replies inherits the thread of the first threaded message it reaches."""
    thread_reply = raw("$thread-reply", "reply", ts=2_000, thread_id="$root")
    client = _thread(
        "$root",
        thread_reply,
        events={
            "$thread-reply": thread_reply,
            "$plain-reply-1": _reply("$plain-reply-1", to="$thread-reply", ts=3_000),
            "$plain-reply-2": _reply("$plain-reply-2", to="$plain-reply-1", ts=4_000),
        },
    )

    assert await _resolve(principal, client, "$plain-reply-2") == "$root"
    assert client.fetched == ["$plain-reply-2", "$plain-reply-1", "$thread-reply"]
    assert client.relation_calls == 0


@pytest.mark.asyncio
async def test_a_plain_reply_to_a_missing_message_stays_out_of_every_thread(principal: PrincipalStore) -> None:
    """A reply whose target is gone, for example redacted and purged, resolves to no thread."""
    client = _RecordingClient(events={"$plain-reply": _reply("$plain-reply", to="$gone", ts=2_000)})

    assert await _resolve(principal, client, "$plain-reply") is None
    assert client.fetched == ["$plain-reply", "$gone"]


@pytest.mark.asyncio
async def test_a_plain_reply_to_a_thread_root_resolves_to_that_thread(principal: PrincipalStore) -> None:
    """A plain reply to the message a thread hangs off is part of that thread once its replies prove it."""
    client = _thread(
        "$root",
        raw("$thread-reply", "reply", ts=2_000, thread_id="$root"),
        events={"$plain-reply": _reply("$plain-reply", to="$root", ts=3_000)},
    )

    assert await _resolve(principal, client, "$plain-reply") == "$root"


@pytest.mark.asyncio
async def test_a_plain_reply_does_not_make_its_target_a_thread_root(principal: PrincipalStore) -> None:
    """A rich reply is not a thread child, so replying to a plain message starts no thread."""
    client = _thread("$plain-root", events={"$plain-reply": _reply("$plain-reply", to="$plain-root", ts=2_000)})

    assert await _resolve(principal, client, "$plain-reply") is None
    assert await _complete_thread(principal, client, "$plain-root") == ["$plain-root"]


@pytest.mark.asyncio
async def test_an_event_the_homeserver_cannot_serve_resolves_to_no_thread(principal: PrincipalStore) -> None:
    """A failed lookup of the target itself guesses no thread when nothing local knows it either."""
    client = _RecordingClient(hidden_events={"$reply-one": "M_UNKNOWN"})

    assert await _resolve(principal, client, "$reply-one") is None
    assert client.fetched == ["$reply-one"]


@pytest.mark.asyncio
async def test_an_event_the_homeserver_cannot_serve_falls_back_to_the_thread_the_journal_admitted(
    principal: PrincipalStore,
) -> None:
    """A reply this bot already admitted keeps its thread when the homeserver will not describe it."""
    await admit_all(principal, [raw("$fresh-local-reply", "reply", ts=2_000, thread_id="$root")])
    client = _RecordingClient(hidden_events={"$fresh-local-reply": "M_UNKNOWN"})

    assert await _resolve(principal, client, "$fresh-local-reply") == "$root"


@pytest.mark.asyncio
@pytest.mark.parametrize("claimed_thread_id", [None, "$claimed-root"])
async def test_an_edit_of_a_thread_reply_resolves_through_the_message_it_edits(
    principal: PrincipalStore,
    claimed_thread_id: str | None,
) -> None:
    """An edit is placed by the event it replaces, never by a thread named inside its ``m.new_content``.

    Matrix applies ``m.new_content`` by keeping the original event's relation and ignoring every
    ``m.relates_to`` written there, so a thread named inside an edit is whatever its author typed.
    """
    thread_reply = raw("$thread-reply", "reply", ts=2_000, thread_id="$root")
    edit = _edit("$edit", replaces="$thread-reply", ts=3_000, claimed_thread_id=claimed_thread_id)
    client = _thread("$root", thread_reply, events={"$thread-reply": thread_reply, "$edit": edit})

    assert await _resolve(principal, client, "$edit") == "$root"
    assert client.fetched == ["$edit", "$thread-reply"]


@pytest.mark.asyncio
async def test_an_edit_of_a_plain_reply_inherits_the_thread_that_reply_inherits(principal: PrincipalStore) -> None:
    """Editing a bridged plain reply keeps it in the thread its reply target belongs to."""
    thread_reply = raw("$thread-reply", "reply", ts=2_000, thread_id="$root")
    client = _thread(
        "$root",
        thread_reply,
        events={
            "$thread-reply": thread_reply,
            "$plain-reply": _reply("$plain-reply", to="$thread-reply", ts=3_000),
            "$edit": _edit("$edit", replaces="$plain-reply", ts=4_000),
        },
    )

    assert await _resolve(principal, client, "$edit") == "$root"


@pytest.mark.asyncio
async def test_an_edit_of_a_thread_root_resolves_to_that_root(principal: PrincipalStore) -> None:
    """Editing the message a thread hangs off names that thread."""
    edit = _edit("$edit", replaces="$root", ts=3_000)
    client = _thread("$root", raw("$thread-reply", "reply", ts=2_000, thread_id="$root"), edit, events={"$edit": edit})

    assert await _resolve(principal, client, "$edit") == "$root"


@pytest.mark.asyncio
async def test_edits_of_a_message_do_not_make_it_a_thread_root(principal: PrincipalStore) -> None:
    """An ``m.replace`` of a relation-free message revises it; it is not a reply in a thread under it."""
    edit = _edit("$edit", replaces="$root", ts=2_000)
    client = _thread("$root", edit, _edit("$edit-2", replaces="$root", ts=3_000), events={"$edit": edit})

    assert await _resolve(principal, client, "$root") is None
    assert await _resolve(principal, client, "$edit") is None
    assert await _complete_thread(principal, client, "$root") == ["$root"]


@pytest.mark.asyncio
async def test_a_cyclic_edit_chain_resolves_to_no_thread(principal: PrincipalStore) -> None:
    """Edits that replace each other end the walk instead of recursing until a crash."""
    client = _RecordingClient(
        events={
            "$edit-a": _edit("$edit-a", replaces="$edit-b", ts=2_000),
            "$edit-b": _edit("$edit-b", replaces="$edit-a", ts=3_000),
        },
    )

    assert await _resolve(principal, client, "$edit-a") is None


@pytest.mark.asyncio
async def test_an_edit_naming_a_thread_does_not_make_that_message_a_thread_root(principal: PrincipalStore) -> None:
    """A replacement claiming a thread inside ``m.new_content`` cannot promote a plain message into a root.

    The edit replaces an event this bot never saw, so the claim is the only thing that could put
    it under the plain message. Believing it would let anyone who can send an edit start a thread
    under any message in the room.
    """
    await admit_all(principal, [_edit("$injection", replaces="$never-seen", ts=2_000, claimed_thread_id="$plain")])
    client = _thread("$plain")

    assert await _resolve(principal, client, "$plain") is None
    assert await _complete_thread(principal, client, "$plain") == ["$plain"]


@pytest.mark.asyncio
async def test_a_sticker_in_a_thread_does_not_prove_its_root(principal: PrincipalStore) -> None:
    """Only messages are thread children, so a sticker naming a root does not make it one."""
    sticker = {
        "event_id": "$sticker",
        "sender": ALICE,
        "origin_server_ts": 2_000,
        "type": "m.sticker",
        "content": {
            "body": "sticker",
            "url": "mxc://example.org/sticker",
            "info": {},
            "m.relates_to": {"rel_type": "m.thread", "event_id": "$root"},
        },
    }
    client = _thread("$root", sticker)

    assert await _resolve(principal, client, "$root") is None
    assert await _complete_thread(principal, client, "$root") == ["$root"]
