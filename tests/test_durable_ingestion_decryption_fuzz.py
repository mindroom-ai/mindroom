"""Generated durable-ingestion sequences preserve late-decryption identity."""

from __future__ import annotations

from dataclasses import dataclass, replace
from typing import TYPE_CHECKING, Literal
from uuid import UUID, uuid4

import nio
import pytest
from hypothesis import HealthCheck, given, settings
from hypothesis import strategies as st
from nio.durable import RecordKind, SyncBatch, SyncRecord
from nio.durable.model import CryptoEvidence

from mindroom.event_journal import EventKind
from mindroom.matrix.durable_ingestion import consume_one_ingestion_batch
from tests.test_durable_ingestion_admission import Session

if TYPE_CHECKING:
    from collections.abc import Callable

    from mindroom.event_journal import EventJournalStore, PrincipalStore

_ROOM = "!decryption-fuzz:example.org"
_THREAD = "$thread"
_EVENT_ID = "$cipher"
_SENDER = "@alice:example.org"
_BODY = "clear request"

type _Operation = Literal["opaque", "clear", "settle", "reopen"]


@dataclass(frozen=True, slots=True)
class _Action:
    principal: int
    operation: _Operation


@dataclass(slots=True)
class _ExpectedState:
    first_observation: Literal["opaque", "clear"] | None = None
    clear_observed: bool = False
    settled: bool = False

    def observe(
        self,
        operation: Literal["opaque", "clear"],
        opaque_provenance: nio.TimelineEventProvenance,
    ) -> None:
        if self.first_observation is None and (
            operation == "clear" or opaque_provenance is nio.TimelineEventProvenance.HISTORY
        ):
            self.first_observation = operation
        if operation == "clear":
            self.clear_observed = True


@st.composite
def _action_sequences(draw: st.DrawFn) -> tuple[_Action, ...]:
    """Build a short sequence containing both refinement orders and durable duplicates."""
    clear_first = draw(st.booleans())
    first: Literal["opaque", "clear"] = "clear" if clear_first else "opaque"
    second: Literal["opaque", "clear"] = "opaque" if clear_first else "clear"
    other_first = draw(st.sampled_from(("opaque", "clear")))
    generated = draw(
        st.lists(
            st.one_of(
                st.builds(
                    _Action,
                    principal=st.integers(min_value=0, max_value=1),
                    operation=st.sampled_from(("opaque", "clear")),
                ),
                st.sampled_from((_Action(0, "settle"), _Action(1, "settle"), _Action(-1, "reopen"))),
            ),
            max_size=8,
        ),
    )
    return (
        _Action(1, other_first),
        _Action(0, first),
        _Action(-1, "reopen"),
        _Action(0, second),
        _Action(0, first),
        _Action(1, "clear"),
        *generated,
        _Action(0, "settle"),
        _Action(-1, "reopen"),
        _Action(0, second),
        _Action(1, "settle"),
        _Action(-1, "reopen"),
        _Action(1, other_first),
    )


def _opaque_record(provenance: nio.TimelineEventProvenance) -> SyncRecord:
    return SyncRecord(
        RecordKind.TIMELINE,
        _ROOM,
        {
            "type": "m.room.encrypted",
            "event_id": _EVENT_ID,
            "sender": _SENDER,
            "origin_server_ts": 10,
            "content": {
                "algorithm": "m.megolm.v1.aes-sha2",
                "sender_key": "sender-key",
                "device_id": "ALICE",
                "session_id": "session",
                "ciphertext": "AgAAAA",
            },
        },
        provenance=provenance,
        membership_epoch=0,
    )


def _clear_record() -> SyncRecord:
    return replace(
        _opaque_record(nio.TimelineEventProvenance.LIVE),
        clear={
            "type": "m.room.message",
            "event_id": _EVENT_ID,
            "sender": _SENDER,
            "origin_server_ts": 10,
            "content": {
                "msgtype": "m.text",
                "body": _BODY,
                "m.relates_to": {"rel_type": "m.thread", "event_id": _THREAD},
            },
        },
        crypto=CryptoEvidence(False, "sender-key", "session"),
        provenance=nio.TimelineEventProvenance.LIVE,
    )


async def _principal_for(
    store: EventJournalStore,
    *,
    principal_id: str,
    stream_id: UUID,
) -> PrincipalStore:
    principal = store.principal(principal_id)
    consumer = await principal.load_or_create_ingestion_consumer(new_generation=uuid4())
    await principal.bind_ingestion_stream(generation=consumer.generation, stream_id=stream_id)
    return principal


async def _assert_expected(principal: PrincipalStore, expected: _ExpectedState) -> None:
    stored = await principal.load_event(_EVENT_ID)
    pending = await principal.pending(limit=10)
    thread_page = await principal.read_conversation(room_id=_ROOM, thread_id=_THREAD, limit=10)
    room_page = await principal.read_conversation(room_id=_ROOM, thread_id=None, limit=10)

    if expected.first_observation is None:
        assert stored is None
        assert list(pending) == []
        assert thread_page.messages == ()
        assert room_page.messages == ()
        return

    assert stored is not None
    assert stored.kind is (EventKind.MESSAGE if expected.first_observation == "clear" else EventKind.OPAQUE_HISTORY)
    assert stored.thread_id == (_THREAD if expected.first_observation == "clear" else None)
    expected_pending = expected.first_observation == "clear" and not expected.settled
    assert [event.event_id for event in pending] == ([_EVENT_ID] if expected_pending else [])
    assert [message.logical_event_id for message in thread_page.messages] == (
        [_EVENT_ID] if expected.clear_observed else []
    )
    assert [message.content["body"] for message in thread_page.messages] == ([_BODY] if expected.clear_observed else [])
    assert room_page.messages == ()


async def _open_principals(
    store: EventJournalStore,
    principal_ids: tuple[str, str],
    stream_ids: tuple[UUID, UUID],
) -> tuple[PrincipalStore, PrincipalStore]:
    return (
        await _principal_for(store, principal_id=principal_ids[0], stream_id=stream_ids[0]),
        await _principal_for(store, principal_id=principal_ids[1], stream_id=stream_ids[1]),
    )


@pytest.mark.asyncio
@pytest.mark.timeout(180)
@settings(
    deadline=None,
    max_examples=36,
    print_blob=True,
    suppress_health_check=(HealthCheck.function_scoped_fixture,),
)
@given(
    actions=_action_sequences(),
    opaque_provenance=st.sampled_from(tuple(nio.TimelineEventProvenance)),
)
async def test_generated_opaque_clear_sequences_preserve_exactly_one_durable_identity(
    journal_database: Callable[[], EventJournalStore],
    actions: tuple[_Action, ...],
    opaque_provenance: nio.TimelineEventProvenance,
) -> None:
    """Reordering late decryption cannot revive, duplicate, or cross principal work."""
    suffix = uuid4().hex
    principal_ids = (f"agent-a-{suffix}", f"agent-b-{suffix}")
    stream_ids = (uuid4(), uuid4())
    sequences = [0, 0]
    expected = [_ExpectedState(), _ExpectedState()]
    store = journal_database()
    try:
        principals = await _open_principals(store, principal_ids, stream_ids)

        for action in actions:
            if action.operation == "reopen":
                await store.close()
                store = journal_database()
                principals = await _open_principals(store, principal_ids, stream_ids)
            elif action.operation == "settle":
                await principals[action.principal].settle(_EVENT_ID)
                expected[action.principal].settled = True
            else:
                sequences[action.principal] += 1
                record = _opaque_record(opaque_provenance) if action.operation == "opaque" else _clear_record()
                batch = SyncBatch(
                    stream_ids[action.principal],
                    sequences[action.principal],
                    (record,),
                )
                session = Session(batch)

                await consume_one_ingestion_batch(
                    session,
                    principals[action.principal],
                    account_id=principal_ids[action.principal],
                )

                assert session.acked == [batch]
                expected[action.principal].observe(action.operation, opaque_provenance)

            for principal, state in zip(principals, expected, strict=True):
                await _assert_expected(principal, state)
    finally:
        await store.close()
