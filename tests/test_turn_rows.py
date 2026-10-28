"""Turn state as rows: reshape, the eligibility guard, lookup precedence, delivery binding, and retention."""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

import pytest

from mindroom.event_journal import EventJournalStore, journal, turn_rows
from mindroom.event_journal.turn_rows import Accepted, Answered, Refused
from mindroom.history.types import HistoryScope
from mindroom.message_target import MessageTarget
from mindroom.turn_record import (
    PreparedVoiceSource,
    RevisionReplay,
    SourceEventMetadata,
    SourceVisibleEcho,
    TurnRecord,
)
from tests.test_event_journal_store import admit

if TYPE_CHECKING:
    from collections.abc import Callable, Mapping, Sequence


pytestmark = pytest.mark.asyncio

AGENT = "agent"
OTHER_AGENT = "other"
ROOM = "!a:example.org"
OTHER_ROOM = "!b:example.org"
ALICE = "@alice:example.org"
BOB = "@bob:example.org"
TARGET = MessageTarget.resolve(ROOM, "$thread", None)
SCOPE = HistoryScope(kind="agent", scope_id=AGENT)
VOICE = PreparedVoiceSource.from_content("transcript", {"msgtype": "m.text", "body": "transcript"})


def turn(
    sources: Sequence[str],
    *,
    aliases: Sequence[str] = (),
    prompts: Mapping[str, str] | None = None,
    revisions: Mapping[str, tuple[int, str]] | None = None,
    replay: Mapping[str, RevisionReplay] | None = None,
    metadata: Mapping[str, SourceEventMetadata] | None = None,
    target: MessageTarget | None = TARGET,
    response: str | None = None,
    requester: str | None = ALICE,
    voice: Mapping[str, PreparedVoiceSource] | None = None,
    echoes: Mapping[str, SourceVisibleEcho] | None = None,
) -> TurnRecord:
    """Return one candidate turn; every source gets an original-body prompt unless ``prompts`` says otherwise."""
    return TurnRecord.create(
        sources,
        discovery_event_ids=aliases,
        response_event_id=response,
        completed=False,
        source_event_prompts={source: f"{source} body" for source in sources} if prompts is None else prompts,
        source_event_revisions=revisions,
        revision_replay=replay,
        source_event_metadata=metadata,
        prepared_voice_sources=voice,
        visible_echoes=echoes,
        requester_id=requester,
        response_owner=AGENT if target is not None else None,
        history_scope=SCOPE if target is not None else None,
        conversation_target=target,
    )


async def write(store: EventJournalStore, operation: Callable[..., Any], *args: object, **kwargs: object) -> Any:  # noqa: ANN401
    """Run one turn-row operation for ``AGENT`` in its own write transaction."""
    return await store.backend.write(lambda transaction: operation(transaction, AGENT, *args, **kwargs))


async def read(store: EventJournalStore, operation: Callable[..., Any], *args: object, **kwargs: object) -> Any:  # noqa: ANN401
    """Run one turn-row read for ``AGENT``."""
    return await store.backend.read(lambda transaction: operation(transaction, AGENT, *args, **kwargs))


async def pending(store: EventJournalStore, record: TurnRecord, room_id: str = ROOM) -> turn_rows.ReshapeResult:
    """Record one pending turn in ``room_id``."""
    return await write(store, turn_rows.record_pending_turn, record, room_id=room_id)


async def accepted(store: EventJournalStore, record: TurnRecord, room_id: str = ROOM) -> TurnRecord:
    """Record one pending turn and return it, requiring the write to land."""
    result = await pending(store, record, room_id)
    assert isinstance(result, Accepted), result
    return result.record


async def lookup(store: EventJournalStore, event_id: str) -> TurnRecord | None:
    """Return the turn one event answers to."""
    return await read(store, turn_rows.get_turn_record, event_id)


async def redact(store: EventJournalStore, event_id: str, room_id: str = ROOM) -> None:
    """Record one redaction for ``AGENT``."""
    await write(store, turn_rows.record_redaction, room_id=room_id, event_id=event_id)


async def column(store: EventJournalStore, sql: str, *params: object) -> list[tuple[object, ...]]:
    """Return raw rows of one query, for asserting what is stored."""
    rows = await store.backend.read(lambda transaction: transaction.fetchall(sql, params))
    return [tuple(row.values()) if isinstance(row, dict) else tuple(row) for row in rows]


@pytest.fixture
def store(journal_store: EventJournalStore) -> EventJournalStore:
    """One open journal per backend."""
    return journal_store


class TestReshape:
    """Forming a turn whose ordered sources differ from every stored turn."""

    async def test_a_different_ordered_tuple_gets_a_new_key_and_never_changes_an_existing_one(
        self,
        store: EventJournalStore,
    ) -> None:
        """A different ordered tuple gets a new key and never changes an existing one."""
        first = await accepted(store, turn(["$a", "$b"]))
        same = await accepted(store, turn(["$a", "$b"]))
        reordered = await accepted(store, turn(["$b", "$a"]))

        assert same.turn_key == first.turn_key
        assert reordered.turn_key != first.turn_key
        assert reordered.source_event_ids == ("$b", "$a")
        assert reordered.anchor_event_id == "$a"
        assert await read(store, turn_rows.load_turn, first.turn_key) is None

    async def test_watermarks_and_consumed_revision_proof_survive_reshape(self, store: EventJournalStore) -> None:
        """Watermarks and consumed revision proof survive reshape."""
        first = await accepted(
            store,
            turn(["$a"], prompts={"$a": "edited"}, revisions={"$a": (20, "$e1")}),
        )
        # Another message's edit consumed only as context is this turn's provenance too.
        await write(store, turn_rows.record_revisions, turn_key=first.turn_key, revisions=[("$ex", "$x", 15)])
        answered = await write(
            store,
            turn_rows.record_responded_turn,
            turn(["$a"], prompts={"$a": "edited"}, revisions={"$a": (20, "$e1")}, response="$r1"),
            room_id=ROOM,
        )
        assert isinstance(answered, Accepted)
        assert answered.record.revision_replay is not None
        assert answered.record.revision_replay["$e1"].response_event_id == "$r1"
        # A completed owner cannot be absorbed: a batch naming its source is refused.
        assert await pending(store, turn(["$a", "$b"])) == Refused(("$a",))

        regrouped = await accepted(store, turn(["$c"], prompts={"$c": "c"}))
        await write(store, turn_rows.record_revisions, turn_key=regrouped.turn_key, revisions=[("$ec", "$c", 30)])
        merged = await accepted(store, turn(["$c", "$d"], prompts={"$d": "d"}))

        assert merged.revision_replay == {"$ec": RevisionReplay("$c", 30)}
        assert merged.source_event_prompts == {"$c": "c", "$d": "d"}

    async def test_reshape_keeps_a_selected_edit_and_its_watermark_against_an_older_late_write(
        self,
        store: EventJournalStore,
    ) -> None:
        """Reshape keeps a selected edit and its watermark against an older late write."""
        first = await accepted(store, turn(["$a"], prompts={"$a": "newest"}, revisions={"$a": (30, "$e3")}))
        merged = await accepted(
            store,
            turn(["$a", "$b"], prompts={"$a": "older", "$b": "b"}, revisions={"$a": (10, "$e1")}),
        )

        assert merged.turn_key != first.turn_key
        assert merged.source_event_prompts == {"$a": "newest", "$b": "b"}
        assert merged.source_event_revisions == {"$a": (30, "$e3")}
        assert merged.revision_replay == {"$e1": RevisionReplay("$a", 10), "$e3": RevisionReplay("$a", 30)}

    async def test_an_unfinished_owner_with_other_context_is_absorbed_and_its_placeholder_adopted(
        self,
        store: EventJournalStore,
    ) -> None:
        """An unfinished owner with other context is absorbed and its placeholder adopted."""
        old = await accepted(store, turn(["$a"]))
        await write(store, turn_rows.record_placeholder, turn_key=old.turn_key, response_event_id="$placeholder")
        moved_target = MessageTarget.resolve(ROOM, None, None)
        merged = await accepted(store, turn(["$a", "$b"], target=moved_target))

        assert merged.response_event_id == "$placeholder"
        assert merged.conversation_target == moved_target

    async def test_detached_sources_keep_a_record_only_for_voice_or_echo(self, store: EventJournalStore) -> None:
        """Detached sources keep a record only for voice or echo."""
        await write(store, turn_rows.record_prepared_voice, room_id=ROOM, source_event_id="$voice", prepared=VOICE)
        await write(store, turn_rows.record_visible_echo, room_id=ROOM, source_event_id="$echoed", echo_event_id="$e")
        batch = await accepted(store, turn(["$voice", "$echoed", "$plain", "$kept"]))
        await write(
            store,
            turn_rows.record_revisions,
            turn_key=batch.turn_key,
            revisions=[("$voice-edit", "$voice", 5), ("$plain-edit", "$plain", 6)],
        )

        kept = await accepted(store, turn(["$kept"]))

        assert kept.revision_replay is None
        assert await lookup(store, "$plain") is None
        voice_turn = await lookup(store, "$voice")
        assert voice_turn is not None
        assert voice_turn.source_event_ids == ("$voice",)
        assert voice_turn.prepared_voice_sources == {"$voice": VOICE}
        assert voice_turn.revision_replay == {"$voice-edit": RevisionReplay("$voice", 5)}
        assert voice_turn.source_event_prompts is None
        assert voice_turn.conversation_target is None
        echo_turn = await lookup(store, "$echoed")
        assert echo_turn is not None
        assert echo_turn.visible_echoes == {"$echoed": SourceVisibleEcho("$e")}

    async def test_a_reformed_batch_keeps_an_echo_on_one_source_and_a_checkpoint_on_another(
        self,
        store: EventJournalStore,
    ) -> None:
        """A reformed batch keeps an echo on one source and a checkpoint on another."""
        await write(store, turn_rows.record_visible_echo, room_id=ROOM, source_event_id="$m1", echo_event_id="$echo")
        await write(store, turn_rows.record_prepared_voice, room_id=ROOM, source_event_id="$m2", prepared=VOICE)

        batch = await accepted(store, turn(["$m1", "$m2"]))
        assert await write(
            store,
            turn_rows.record_finalized_visible_echo,
            source_event_id="$m1",
            echo_event_id="$echo",
            is_fallback=False,
        )

        assert batch.visible_echoes == {"$m1": SourceVisibleEcho("$echo")}
        assert batch.prepared_voice_sources == {"$m2": VOICE}
        assert await read(store, turn_rows.visible_echo, "$m1") == SourceVisibleEcho("$echo", is_fallback=False)

    async def test_completed_or_bound_owners_refuse_and_the_same_completed_tuple_is_answered(
        self,
        store: EventJournalStore,
    ) -> None:
        """Completed or bound owners refuse and the same completed tuple is answered."""
        await write(store, turn_rows.record_turn, TurnRecord.create(["$done"]), room_id=ROOM)
        bound = await accepted(store, turn(["$bound"]))
        await write(store, turn_rows.bind_final_delivery, delivery_id="$bound", room_id=ROOM, turn_key=bound.turn_key)

        assert await pending(store, turn(["$done", "$fresh"])) == Refused(("$done",))
        assert await pending(store, turn(["$fresh", "$bound"])) == Refused(("$bound",))
        answered = await pending(store, turn(["$done"]))
        assert isinstance(answered, Answered)
        # A bound turn is frozen: its own tuple is answered without a context change.
        frozen = await pending(store, turn(["$bound"], target=MessageTarget.resolve(ROOM, None, None)))
        assert isinstance(frozen, Answered)
        assert frozen.record.conversation_target == TARGET
        assert await lookup(store, "$fresh") is None

    async def test_attribution_is_proven_per_source(self, store: EventJournalStore) -> None:
        """Attribution is proven per source."""
        single = await accepted(store, turn(["$s"]))
        coalesced = await accepted(store, turn(["$c1", "$c2"]))
        explicit = await accepted(
            store,
            turn(["$e1", "$e2"], metadata={"$e2": SourceEventMetadata(BOB, 5.0)}),
        )

        assert single.requester_id_for_source("$s") == ALICE
        assert coalesced.source_event_metadata == {}
        assert coalesced.requester_id_for_source("$c1") is None
        assert explicit.requester_id_for_source("$e1") is None
        assert explicit.requester_id_for_source("$e2") == BOB
        # A coalesced write without attribution keeps what an earlier write proved.
        regrouped = await accepted(store, turn(["$s", "$c3"]))
        assert regrouped.requester_id_for_source("$s") == ALICE
        assert regrouped.requester_id_for_source("$c3") is None

    async def test_a_turn_recorded_in_one_room_refuses_a_write_naming_another(self, store: EventJournalStore) -> None:
        """A turn recorded in one room refuses a write naming another."""
        await accepted(store, turn(["$a"]))

        with pytest.raises(ValueError, match="another room"):
            await pending(store, turn(["$a"], target=MessageTarget.resolve(OTHER_ROOM, None, None)), OTHER_ROOM)
        # An answered turn elsewhere refuses a batch naming its source like any answered owner.
        await write(store, turn_rows.record_turn, TurnRecord.create(["$done"]), room_id=OTHER_ROOM)
        assert await pending(store, turn(["$done", "$b"])) == Refused(("$done",))


class TestAliases:
    """An original and the router relay that answers for it."""

    async def test_alias_indexing_never_moves_physical_membership_or_checkpoints(
        self,
        store: EventJournalStore,
    ) -> None:
        """Alias indexing never moves physical membership or checkpoints."""
        await write(store, turn_rows.record_prepared_voice, room_id=ROOM, source_event_id="$o", prepared=VOICE)
        relay = await accepted(
            store,
            turn(["$r"], aliases=["$o"], metadata={"$r": SourceEventMetadata(ALICE, discovery_event_id="$o")}),
        )

        original = await lookup(store, "$o")
        assert original is not None
        assert original.source_event_ids == ("$o",)
        assert original.prepared_voice_sources == {"$o": VOICE}
        assert relay.discovery_event_ids == ("$o",)
        assert not await read(store, turn_rows.is_answered, "$o")

    async def test_a_completed_relay_answers_for_its_unfinished_original(self, store: EventJournalStore) -> None:
        """A completed relay answers for its unfinished original."""
        original = await accepted(store, turn(["$o"]))
        relay = turn(["$r"], aliases=["$o"], metadata={"$r": SourceEventMetadata(ALICE, discovery_event_id="$o")})
        await write(
            store,
            turn_rows.record_responded_turn,
            TurnRecord.create(
                relay.source_event_ids,
                discovery_event_ids=relay.discovery_event_ids,
                source_event_metadata=relay.source_event_metadata,
                response_event_id="$relay-answer",
                conversation_target=TARGET,
                requester_id=ALICE,
            ),
            room_id=ROOM,
        )

        found = await lookup(store, "$o")
        assert found is not None
        assert found.source_event_ids == ("$r",)
        assert found.response_event_id == "$relay-answer"
        assert await read(store, turn_rows.is_answered, "$o")
        assert await read(store, turn_rows.handled_among, ROOM, ["$o", "$x"]) == {"$o"}
        # The original keeps its own physical turn, and its own later write is refused.
        assert await read(store, turn_rows.load_turn, original.turn_key) is not None
        assert await pending(store, turn(["$o"])) == Refused(("$o",))

    async def test_without_a_completed_owner_the_physical_owner_then_the_newest_alias_owner_answer(
        self,
        store: EventJournalStore,
    ) -> None:
        """Without a completed owner the physical owner then the newest alias owner answer."""
        await accepted(store, turn(["$r1"], aliases=["$x"]))
        newer = await accepted(store, turn(["$r2"], aliases=["$x"]))

        found = await lookup(store, "$x")
        assert found is not None
        assert found.turn_key == newer.turn_key
        physical = await accepted(store, turn(["$x"]))
        found = await lookup(store, "$x")
        assert found is not None
        assert found.turn_key == physical.turn_key

    async def test_an_alias_a_completed_turn_owns_is_not_indexed_again(self, store: EventJournalStore) -> None:
        """An alias a completed turn owns is not indexed again."""
        await write(store, turn_rows.record_turn, TurnRecord.create(["$o"]), room_id=ROOM)

        relay = await accepted(store, turn(["$r"], aliases=["$o"]))

        assert relay.discovery_event_ids == ()


class TestRedactionGuard:
    """No write stores text a redaction removed, whichever lands first."""

    async def test_a_redaction_before_the_row_exists_blocks_its_payload(self, store: EventJournalStore) -> None:
        """A redaction before the row exists blocks its payload."""
        await redact(store, "$s")

        stored = await accepted(store, turn(["$s", "$t"]))
        voice = await write(store, turn_rows.record_prepared_voice, room_id=ROOM, source_event_id="$s", prepared=VOICE)

        assert stored.source_event_prompts == {"$t": "$t body"}
        assert voice is None
        assert await read(store, turn_rows.deleted_sources, stored.turn_key) == {"$s"}
        assert await read(store, turn_rows.handled_among, ROOM, ["$s", "$t"]) == {"$s"}

    async def test_a_payload_built_before_the_redaction_is_dropped_when_it_lands_late(
        self,
        store: EventJournalStore,
    ) -> None:
        """A payload built before the redaction is dropped when it lands late."""
        snapshot = turn(["$s"])
        await accepted(store, snapshot)
        await write(store, turn_rows.record_prepared_voice, room_id=ROOM, source_event_id="$s", prepared=VOICE)
        await redact(store, "$s")
        after_redaction = await lookup(store, "$s")
        await accepted(store, snapshot)

        assert after_redaction is not None
        assert after_redaction.source_event_prompts is None
        assert after_redaction.prepared_voice_sources is None
        relooked = await lookup(store, "$s")
        assert relooked is not None
        assert relooked.source_event_prompts is None

    async def test_a_redaction_in_another_room_changes_nothing(self, store: EventJournalStore) -> None:
        """A redaction in another room changes nothing."""
        stored = await accepted(store, turn(["$s"]))

        await redact(store, "$s", room_id=OTHER_ROOM)

        assert (await lookup(store, "$s")) == stored
        assert await read(store, turn_rows.redacted_events, ROOM, ["$s"]) == frozenset()
        assert await read(store, turn_rows.handled_among, ROOM, ["$s"]) == frozenset()
        assert await read(store, turn_rows.handled_among, OTHER_ROOM, ["$s"]) == {"$s"}

    async def test_an_alias_redaction_deletes_its_relay_and_a_late_write_stores_nothing_across_restart(
        self,
        journal_database: Callable[[], EventJournalStore],
    ) -> None:
        """An alias redaction deletes its relay and a late write stores nothing across restart."""
        store = journal_database()
        relay = turn(["$r"], aliases=["$o"], metadata={"$r": SourceEventMetadata(ALICE, discovery_event_id="$o")})
        stored = await accepted(store, relay)

        await redact(store, "$o")
        await accepted(store, relay)
        await store.close()
        reopened = journal_database()

        found = await lookup(reopened, "$r")
        assert found is not None
        assert found.source_event_prompts is None
        assert await read(reopened, turn_rows.deleted_sources, stored.turn_key) == {"$r"}
        assert await column(reopened, "SELECT prompt FROM turn_sources WHERE event_id = '$r'") == [(None,)]

    async def test_a_physical_source_is_never_read_as_another_sources_alias(self, store: EventJournalStore) -> None:
        """A physical source is never read as another sources alias."""
        stored = await accepted(
            store,
            turn(["$s1", "$s2"], metadata={"$s2": SourceEventMetadata(ALICE, discovery_event_id="$s1")}),
        )

        await redact(store, "$s1")

        assert await read(store, turn_rows.deleted_sources, stored.turn_key) == {"$s1"}
        found = await lookup(store, "$s2")
        assert found is not None
        assert found.source_event_prompts == {"$s2": "$s2 body"}

    async def test_a_redacted_selection_is_rejected_while_a_newer_edit_is_accepted_across_reopen(
        self,
        journal_database: Callable[[], EventJournalStore],
    ) -> None:
        """A redacted selection is rejected while a newer edit is accepted across reopen."""
        store = journal_database()
        stored = await accepted(store, turn(["$s"]))
        key = stored.turn_key
        assert await write(
            store,
            turn_rows.record_edit,
            turn_key=key,
            edited_event_id="$s",
            prompt="E1 text",
            revision=(10, "$e1"),
        )

        await redact(store, "$e1")
        # Nothing newer is selected now, so only the redaction refuses a late write carrying E1.
        late = turn(["$s"], prompts={"$s": "E1 text"}, revisions={"$s": (10, "$e1")})
        assert isinstance(await pending(store, late), Accepted)
        assert not await write(
            store,
            turn_rows.record_edit,
            turn_key=key,
            edited_event_id="$s",
            prompt="E1 again",
            revision=(10, "$e1"),
        )
        refused = await read(store, turn_rows.load_turn, key)
        assert refused is not None
        assert refused.source_event_prompts is None
        assert await write(
            store,
            turn_rows.record_edit,
            turn_key=key,
            edited_event_id="$s",
            prompt="E2 text",
            revision=(20, "$e2"),
        )
        assert isinstance(await pending(store, late), Accepted)
        assert not await write(
            store,
            turn_rows.record_edit,
            turn_key=key,
            edited_event_id="$s",
            prompt="E1 again",
            revision=(10, "$e1"),
        )
        await store.close()
        reopened = journal_database()

        found = await read(reopened, turn_rows.load_turn, key)
        assert found is not None
        assert found.source_event_prompts == {"$s": "E2 text"}
        assert found.source_event_revisions == {"$s": (20, "$e2")}
        stored_text = await column(reopened, "SELECT prompt FROM turn_sources")
        assert all("E1" not in str(value) for (value,) in stored_text)

    async def test_redacting_a_selected_revision_lets_the_earlier_body_be_selected_again(
        self,
        store: EventJournalStore,
    ) -> None:
        """Redacting a selected revision lets the earlier body be selected again."""
        stored = await accepted(store, turn(["$s"], prompts={"$s": "E1"}, revisions={"$s": (10, "$e1")}))

        await redact(store, "$e1")
        cleared = await lookup(store, "$s")
        await accepted(store, turn(["$s"], prompts={"$s": "original"}))

        assert cleared is not None
        assert cleared.source_event_prompts is None
        assert cleared.source_event_revisions is None
        assert cleared.revision_replay == {"$e1": RevisionReplay("$s", 10)}
        restored = await read(store, turn_rows.load_turn, stored.turn_key)
        assert restored is not None
        assert restored.source_event_prompts == {"$s": "original"}


class TestCompletion:
    """Every completion path clears voice plaintext."""

    @pytest.mark.parametrize("path", ["terminal", "responded", "settle", "acknowledged"])
    async def test_completion_clears_the_voice_checkpoint(self, store: EventJournalStore, path: str) -> None:
        """Completion clears the voice checkpoint."""
        await write(store, turn_rows.record_prepared_voice, room_id=ROOM, source_event_id="$v", prepared=VOICE)
        stored = await accepted(store, turn(["$v"], prompts={}))
        assert await read(store, turn_rows.prepared_voice, "$v") == VOICE

        if path == "terminal":
            await write(store, turn_rows.record_turn, TurnRecord.create(["$v"]), room_id=ROOM)
        elif path == "responded":
            await write(store, turn_rows.record_responded_turn, turn(["$v"], response="$r"), room_id=ROOM)
        elif path == "settle":
            await store.backend.write(
                lambda transaction: turn_rows.settle_turn(
                    transaction,
                    f"{AGENT}@@bot:example.org",
                    AGENT,
                    pending=("$v",),
                    logical=("$v",),
                ),
            )
        else:
            await write(store, turn_rows.bind_final_delivery, delivery_id="$v", room_id=ROOM, turn_key=stored.turn_key)
            await write(store, turn_rows.complete_bound_delivery, delivery_id="$v", response_event_id="$ack")

        assert await read(store, turn_rows.is_answered, "$v")
        assert await read(store, turn_rows.prepared_voice, "$v") is None
        assert await column(store, "SELECT prepared_voice_json FROM turn_sources") == [(None,)]

    async def test_a_responded_turn_keeps_the_newest_response_and_proves_only_consumed_revisions(
        self,
        store: EventJournalStore,
    ) -> None:
        """A responded turn keeps the newest response and proves only consumed revisions."""
        await accepted(store, turn(["$s"], prompts={"$s": "e1"}, revisions={"$s": (10, "$e1")}))
        await write(store, turn_rows.record_responded_turn, turn(["$s"], response="$r1"), room_id=ROOM)
        second = await write(
            store,
            turn_rows.record_responded_turn,
            turn(["$s"], response="$r2"),
            room_id=ROOM,
            consumed_revisions=[(10, "$e1")],
        )

        assert isinstance(second, Accepted)
        assert second.record.response_event_id == "$r2"
        assert second.record.revision_replay == {"$e1": RevisionReplay("$s", 10, response_event_id="$r2")}

    async def test_echo_publication_never_becomes_the_turns_answer(self, store: EventJournalStore) -> None:
        """Echo publication never becomes the turns answer."""
        await accepted(store, turn(["$v1", "$v2"]))
        for source, echo in (("$v1", "$echo1"), ("$v2", "$echo2")):
            await write(store, turn_rows.record_visible_echo, room_id=ROOM, source_event_id=source, echo_event_id=echo)
            await write(
                store,
                turn_rows.record_finalized_visible_echo,
                source_event_id=source,
                echo_event_id=echo,
                is_fallback=False,
            )

        found = await lookup(store, "$v1")
        assert found is not None
        assert found.response_event_id is None
        assert not found.completed
        owner = await read(store, turn_rows.turn_for_response_event, "$echo2")
        assert owner is not None
        assert owner.turn_key == found.turn_key

    async def test_a_transcript_is_never_overwritten_by_a_fallback(self, store: EventJournalStore) -> None:
        """A transcript is never overwritten by a fallback."""
        await write(store, turn_rows.record_visible_echo, room_id=ROOM, source_event_id="$v", echo_event_id="$e")
        assert await write(
            store,
            turn_rows.record_finalized_visible_echo,
            source_event_id="$v",
            echo_event_id="$e",
            is_fallback=False,
        )
        assert not await write(
            store,
            turn_rows.record_finalized_visible_echo,
            source_event_id="$v",
            echo_event_id="$e",
            is_fallback=True,
        )

        assert await read(store, turn_rows.visible_echo, "$v") == SourceVisibleEcho("$e", is_fallback=False)

    async def test_command_checkpoints_only_grow(self, store: EventJournalStore) -> None:
        """Command checkpoints only grow."""
        stored = await accepted(store, turn(["$cmd"]))
        await write(store, turn_rows.record_command_started, turn_key=stored.turn_key)
        assert await write(store, turn_rows.record_command_result, turn_key=stored.turn_key, text="first")
        assert not await write(store, turn_rows.record_command_result, turn_key=stored.turn_key, text="second")
        await accepted(store, turn(["$cmd"]))

        found = await lookup(store, "$cmd")
        assert found is not None
        assert found.command_execution_started
        assert found.command_result_text == "first"


class TestFinalDelivery:
    """A non-AI FINAL delivery binds the turn it answers when it becomes durable."""

    async def test_the_acknowledgement_completes_the_bound_turn_keeping_its_placeholder(
        self,
        store: EventJournalStore,
    ) -> None:
        """The acknowledgement completes the bound turn keeping its placeholder."""
        stored = await accepted(store, turn(["$a", "$b"], aliases=["$alias"]))
        await write(store, turn_rows.record_placeholder, turn_key=stored.turn_key, response_event_id="$placeholder")

        sources = await write(
            store,
            turn_rows.bind_final_delivery,
            delivery_id="$b",
            room_id=ROOM,
            turn_key=stored.turn_key,
        )
        completed = await write(store, turn_rows.complete_bound_delivery, delivery_id="$b", response_event_id="$ack")
        again = await write(store, turn_rows.complete_bound_delivery, delivery_id="$b", response_event_id="$late")

        assert sources == ("$a", "$b", "$alias")
        assert completed is not None
        assert completed.completed
        assert completed.response_event_id == "$placeholder"
        assert again is None

    async def test_a_reshape_before_binding_leaves_a_stale_delivery_unbound(self, store: EventJournalStore) -> None:
        """A reshape before binding leaves a stale delivery unbound."""
        produced = await accepted(store, turn(["$a", "$b"]))
        reshaped = await accepted(store, turn(["$b"]))

        assert reshaped.anchor_event_id == produced.anchor_event_id
        assert (
            await write(
                store,
                turn_rows.bind_final_delivery,
                delivery_id="$b",
                room_id=ROOM,
                turn_key=produced.turn_key,
            )
            is None
        )
        assert await write(store, turn_rows.complete_bound_delivery, delivery_id="$b", response_event_id="$ack") is None
        found = await lookup(store, "$b")
        assert found is not None
        assert not found.completed

    async def test_a_bound_turn_is_never_absorbed(self, store: EventJournalStore) -> None:
        """A bound turn is never absorbed."""
        bound = await accepted(store, turn(["$a"]))
        await write(store, turn_rows.bind_final_delivery, delivery_id="$a", room_id=ROOM, turn_key=bound.turn_key)

        assert await pending(store, turn(["$a", "$b"])) == Refused(("$a",))
        assert await write(store, turn_rows.record_turn, TurnRecord.create(["$b", "$a"]), room_id=ROOM) == Refused(
            ("$a",),
        )
        assert await read(store, turn_rows.load_turn, bound.turn_key) is not None


class TestRetention:
    """Rows go by age and count; unfinished work and its owners stay; old plaintext goes separately."""

    async def _age(self, store: EventJournalStore, event_id: str, updated_at: float) -> None:
        await store.backend.write(
            lambda transaction: transaction.execute(
                "UPDATE turns SET updated_at = ? WHERE turn_key = "
                "(SELECT turn_key FROM turn_sources WHERE event_id = ?)",
                (updated_at, event_id),
            ),
        )

    async def _retain(
        self,
        store: EventJournalStore,
        *,
        unsettled: Sequence[str] = (),
        max_turns: int = 100,
    ) -> turn_rows.RetentionResult:
        return await write(
            store,
            turn_rows.retain,
            now=1_000_000.0,
            max_age_seconds=1000.0,
            max_turns=max_turns,
            unsettled_event_ids=unsettled,
        )

    async def test_old_turns_go_unless_they_own_unfinished_work(self, store: EventJournalStore) -> None:
        """Old turns go unless they own unfinished work."""
        await write(store, turn_rows.record_turn, TurnRecord.create(["$old-done"]), room_id=ROOM)
        await write(store, turn_rows.record_turn, TurnRecord.create(["$unsettled-done"]), room_id=ROOM)
        revised = await write(store, turn_rows.record_turn, TurnRecord.create(["$revised"]), room_id=ROOM)
        await write(store, turn_rows.record_revisions, turn_key=revised.record.turn_key, revisions=[("$rev", "$x", 1)])
        await accepted(store, turn(["$pending"]))
        await write(
            store,
            turn_rows.record_visible_echo,
            room_id=ROOM,
            source_event_id="$echo-only",
            echo_event_id="$e",
        )
        await write(store, turn_rows.record_prepared_voice, room_id=ROOM, source_event_id="$voice", prepared=VOICE)
        await accepted(store, turn(["$deleted"]))
        await redact(store, "$deleted")
        await write(store, turn_rows.record_turn, TurnRecord.create(["$young"]), room_id=ROOM)
        for event_id in ("$old-done", "$unsettled-done", "$revised", "$pending", "$echo-only", "$voice", "$deleted"):
            await self._age(store, event_id, 1.0)
        await self._age(store, "$young", 999_999.0)

        result = await self._retain(store, unsettled=["$unsettled-done", "$rev"])

        remaining = {
            event_id
            for event_id in ("$old-done", "$unsettled-done", "$revised", "$pending", "$echo-only", "$voice")
            if await lookup(store, event_id) is not None
        }
        assert remaining == {"$unsettled-done", "$revised", "$pending", "$echo-only"}
        assert await lookup(store, "$deleted") is None
        assert await lookup(store, "$young") is not None
        assert result.dropped_turn_count == 3

    async def test_the_count_cap_keeps_the_newest_ordinary_turns_and_every_required_one(
        self,
        store: EventJournalStore,
    ) -> None:
        """The count cap keeps the newest ordinary turns and every required one."""
        for index in range(4):
            await write(store, turn_rows.record_turn, TurnRecord.create([f"$done{index}"]), room_id=ROOM)
            await self._age(store, f"$done{index}", 999_990.0 + index)
        await accepted(store, turn(["$pending"]))
        await self._age(store, "$pending", 999_000.0)

        await self._retain(store, max_turns=2)

        kept = {f"$done{index}" for index in range(4) if await lookup(store, f"$done{index}") is not None}
        assert kept == {"$done2", "$done3"}
        assert await lookup(store, "$pending") is not None

    async def test_a_settled_checkpoint_loses_its_plaintext_but_keeps_its_echo(self, store: EventJournalStore) -> None:
        """A settled checkpoint loses its plaintext but keeps its echo."""
        await write(store, turn_rows.record_prepared_voice, room_id=ROOM, source_event_id="$v", prepared=VOICE)
        await write(store, turn_rows.record_visible_echo, room_id=ROOM, source_event_id="$v", echo_event_id="$echo")
        await write(store, turn_rows.record_prepared_voice, room_id=ROOM, source_event_id="$busy", prepared=VOICE)
        intent = await accepted(store, turn(["$intent"], prompts={}))
        await write(store, turn_rows.record_prepared_voice, room_id=ROOM, source_event_id="$intent", prepared=VOICE)

        result = await self._retain(store, unsettled=["$busy"])

        assert result.cleared_voice_turn_count == 1
        assert await read(store, turn_rows.prepared_voice, "$v") is None
        assert await read(store, turn_rows.visible_echo, "$v") == SourceVisibleEcho("$echo")
        assert await read(store, turn_rows.prepared_voice, "$busy") == VOICE
        assert await read(store, turn_rows.prepared_voice, "$intent") == VOICE
        assert intent.turn_key is not None

    async def test_a_detached_checkpoint_with_echo_and_revision_loses_its_transcript_after_settlement(
        self,
        journal_database: Callable[[], EventJournalStore],
    ) -> None:
        """A detached checkpoint with echo and revision loses its transcript after settlement."""
        store = journal_database()
        await write(store, turn_rows.record_prepared_voice, room_id=ROOM, source_event_id="$v", prepared=VOICE)
        await write(store, turn_rows.record_visible_echo, room_id=ROOM, source_event_id="$v", echo_event_id="$echo")
        batch = await accepted(store, turn(["$v", "$t"]))
        await write(store, turn_rows.record_revisions, turn_key=batch.turn_key, revisions=[("$v-edit", "$v", 3)])
        await accepted(store, turn(["$t"]))
        await self._age(store, "$v", 1.0)

        await self._retain(store)
        await store.close()
        reopened = journal_database()

        detached = await lookup(reopened, "$v")
        assert detached is not None
        assert detached.source_event_ids == ("$v",)
        assert detached.prepared_voice_sources is None
        assert detached.visible_echoes == {"$v": SourceVisibleEcho("$echo")}
        assert await column(reopened, "SELECT prepared_voice_json FROM turn_sources WHERE event_id = '$v'") == [
            (None,),
        ]


class TestReads:
    """Readers that the runtime asks by conversation, response, or room."""

    async def test_turns_for_a_session_and_the_owner_of_a_response(self, store: EventJournalStore) -> None:
        """Turns for a session and the owner of a response."""
        first = await accepted(store, turn(["$a"]))
        await write(store, turn_rows.record_responded_turn, turn(["$b"], response="$answer"), room_id=ROOM)
        await accepted(store, turn(["$c"], target=MessageTarget.resolve(ROOM, "$other", None)))

        session = await read(store, turn_rows.turns_for_session, TARGET.session_id)
        owner = await read(store, turn_rows.turn_for_response_event, "$answer")

        assert [record.source_event_ids for record in session] == [("$a",), ("$b",)]
        assert session[0].turn_key == first.turn_key
        assert owner is not None
        assert owner.source_event_ids == ("$b",)

    async def test_two_turns_claiming_one_response_is_an_error(self, store: EventJournalStore) -> None:
        """Two turns claiming one response is an error."""
        await write(store, turn_rows.record_responded_turn, turn(["$a"], response="$same"), room_id=ROOM)
        await write(store, turn_rows.record_responded_turn, turn(["$b"], response="$same"), room_id=ROOM)

        with pytest.raises(RuntimeError, match="Multiple turns"):
            await read(store, turn_rows.turn_for_response_event, "$same")


class TestAgentRedaction:
    """A redaction applies to every Matrix login of its agent and to no other agent."""

    async def test_every_principal_of_the_agent_loses_the_message_and_another_agent_keeps_it(
        self,
        journal_database: Callable[[], EventJournalStore],
    ) -> None:
        """Every principal of the agent loses the message and another agent keeps it."""
        store = journal_database()
        current = f"{AGENT}@@bot:example.org"
        earlier = f"{AGENT}@@old-login:example.org"
        control = f"{OTHER_AGENT}@@other:example.org"
        for principal_id in (current, earlier, control):
            await admit(store.principal(principal_id), "$m", room_id=ROOM)
        await accepted(store, turn(["$m"]))

        await store.backend.write(
            lambda transaction: journal.redact_for_agent(
                transaction,
                AGENT,
                room_id=ROOM,
                event_id="$m",
                receipt_order=99,
            ),
        )
        await store.close()
        reopened = journal_database()

        visible = await column(reopened, "SELECT principal_id FROM visible_messages WHERE logical_event_id = '$m'")
        journal_rows = await column(
            reopened,
            "SELECT principal_id, state, source_json FROM journal_events WHERE event_id = '$m' ORDER BY principal_id",
        )
        assert visible == [(control,)]
        settled = {principal_id: (state, source) for principal_id, state, source in journal_rows}
        assert settled[current] == ("settled", "")
        assert settled[earlier] == ("settled", "")
        assert settled[control][0] == "pending"
        assert "$m" in str(settled[control][1])
        found = await lookup(reopened, "$m")
        assert found is not None
        assert found.source_event_prompts is None
        assert await column(reopened, "SELECT agent_name, room_id, event_id FROM event_redactions") == [
            (AGENT, ROOM, "$m"),
        ]

    async def test_the_principals_of_an_agent_are_matched_by_their_exact_prefix(
        self,
        store: EventJournalStore,
    ) -> None:
        """The principals of an agent are matched by their exact prefix."""
        for principal_id in (f"{AGENT}@@a:example.org", f"{AGENT.upper()}@@b:example.org", f"{AGENT}x@@c:example.org"):
            await admit(store.principal(principal_id), "$m", room_id=ROOM)

        principals = await store.backend.read(
            lambda transaction: journal._principals_of_agent(transaction, AGENT, room_id=ROOM),
        )

        assert principals == (f"{AGENT}@@a:example.org",)
