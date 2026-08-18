"""Backend-neutral contract for the event journal, projection, and outbox.

Every test here runs on SQLite and on PostgreSQL. A rule that holds on only one
backend is a rule MindRoom does not actually have.

The exceptions are the two contention tests, which ask what two connections do
to the same row. SQLite has no second connection *in this process* to ask that
of -- the backend is one process behind one writer -- so running them there
would prove only that the fixture took turns. Both take the PostgreSQL-only
``rival_stores`` fixture, which is where that is spelled out.

SQLite does get a second connection, and not from a fixture: ``mindroom threads
export`` is another process with another writer on the same file. That is the
one thing here no object can stage, so ``TestCrossProcessWriters`` spawns a real
interpreter to be it.
"""

from __future__ import annotations

import asyncio
import json
import sqlite3
import sys
import threading
import time
import uuid
from contextlib import asynccontextmanager, contextmanager, suppress
from dataclasses import dataclass, field, replace
from typing import TYPE_CHECKING, Any, ClassVar, cast

import pytest
import pytest_asyncio

from mindroom.event_journal import (
    AdmissionResult,
    ApprovalCall,
    ApprovalCardReservation,
    ApprovalContinuation,
    ApprovalDecision,
    ConversationCursor,
    DeliveryAcknowledgement,
    DeliveryStage,
    DepartureObservation,
    DepartureSource,
    EventClass,
    EventJournalStore,
    EventKind,
    HistoryRecoveryOutcome,
    InboundEvent,
    InteractiveSelection,
    ProjectedEvent,
    SemanticConsumer,
    TerminalTurnWrite,
    UnreadableApprovalCard,
    delivery_transaction_id,
    reads,
    replacement_target,
)
from mindroom.event_journal.offloading import settled
from mindroom.event_journal.reads import _CONVERSATION_CURSOR_CLAUSE
from mindroom.event_journal.schema import (
    POSTGRES_DIALECT,
    SQLITE_DIALECT,
    render,
    schema_statements,
)
from mindroom.event_journal.sqlite_backend import SqliteBackend
from mindroom.interactive_models import InteractivePrompt
from mindroom.matrix_delivery import MatrixDeliveryWorker
from tests.conftest import postgres_journal_schema_url

if TYPE_CHECKING:
    from collections.abc import AsyncGenerator, AsyncIterator, Awaitable, Callable, Iterator, Mapping, Sequence
    from pathlib import Path

    from mindroom.event_journal import (
        MatrixDelivery,
        PrincipalStore,
        RefreshRequest,
        TurnRecordStore,
    )
    from mindroom.event_journal.backend import Backend, Operation, Transaction
    from mindroom.history_recovery import RoomHistoryRecovery

pytestmark = pytest.mark.asyncio

ROOM = "!room:example.org"
OTHER_ROOM = "!other:example.org"
ALICE = "@alice:example.org"
BOB = "@bob:example.org"
# The device an approval card is claimed from. Stored with the row because a
# Matrix transaction ID only deduplicates against the device that used it.
DEVICE = "SENDINGDEVICE"

_LEGACY_RESPONSE_OUTBOX_DDL = """
CREATE TABLE response_outbox (
    principal_id TEXT NOT NULL, turn_id TEXT NOT NULL,
    stage TEXT NOT NULL, room_id TEXT NOT NULL, thread_id TEXT NOT NULL,
    transaction_id TEXT NOT NULL, payload_json TEXT NOT NULL,
    edits_event_id TEXT, attempted INTEGER NOT NULL DEFAULT 0,
    sending_device_id TEXT, acknowledged_event_id TEXT, created_at_ns BIGINT NOT NULL,
    PRIMARY KEY (principal_id, turn_id, stage)
)
"""
_RELEASED_UNFENCED_MATRIX_DELIVERY_OUTBOX_DDL = """
CREATE TABLE matrix_delivery_outbox (
    principal_id TEXT NOT NULL, delivery_id TEXT NOT NULL,
    stage TEXT NOT NULL, event_type TEXT NOT NULL, room_id TEXT NOT NULL,
    thread_id TEXT NOT NULL, transaction_id TEXT NOT NULL, payload_json TEXT NOT NULL,
    edits_event_id TEXT, edit_target_pending INTEGER NOT NULL DEFAULT 0,
    attempted INTEGER NOT NULL DEFAULT 0, sending_device_id TEXT,
    acknowledged_event_id TEXT, created_at_ns BIGINT NOT NULL,
    PRIMARY KEY (principal_id, delivery_id, stage)
)
"""
_LEGACY_JOURNAL_EVENTS_DDL = """
CREATE TABLE journal_events (
    receipt_order {receipt_order},
    principal_id TEXT NOT NULL, event_id TEXT NOT NULL, room_id TEXT NOT NULL,
    thread_id TEXT NOT NULL, kind TEXT NOT NULL, sender TEXT NOT NULL,
    origin_server_ts BIGINT NOT NULL, source_json TEXT NOT NULL,
    semantic_consumer TEXT, membership_epoch BIGINT NOT NULL,
    state TEXT NOT NULL, UNIQUE (principal_id, event_id)
)
"""
_LEGACY_APPROVAL_CARDS_DDL = """
CREATE TABLE approval_cards (
    principal_id TEXT NOT NULL, room_id TEXT NOT NULL, transaction_id TEXT NOT NULL,
    card_event_id TEXT, attempted INTEGER NOT NULL, sending_device_id TEXT,
    card_json TEXT NOT NULL, resolution_json TEXT, continuation_id TEXT NOT NULL,
    continuation_generation BIGINT NOT NULL, tool_call_id TEXT NOT NULL,
    membership_epoch BIGINT NOT NULL, created_at_ns BIGINT NOT NULL,
    PRIMARY KEY (principal_id, transaction_id)
)
"""


def _install_ambiguous_legacy_response(connection: object, *, postgres: bool) -> None:
    """Install one attempted response whose Matrix outcome cannot be identified."""
    execute = cast("Any", connection).execute
    execute(_LEGACY_RESPONSE_OUTBOX_DDL)
    placeholder = "%s" if postgres else "?"
    execute(
        "INSERT INTO response_outbox VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)".replace("?", placeholder),
        (
            "agent@alice",
            "$attempted",
            "final",
            ROOM,
            "",
            "legacy-txn",
            json.dumps(text("attempted answer")),
            None,
            1,
            DEVICE,
            None,
            1,
        ),
    )


def _install_released_unfenced_delivery_schema(connection: object) -> None:
    """Install the released generic outbox that predates membership ownership."""
    execute = cast("Any", connection).execute
    execute(_RELEASED_UNFENCED_MATRIX_DELIVERY_OUTBOX_DDL)
    execute(
        "CREATE INDEX matrix_delivery_outbox_unacknowledged_scan "
        "ON matrix_delivery_outbox (principal_id, event_type, created_at_ns, delivery_id, stage) "
        "WHERE acknowledged_event_id IS NULL",
    )
    execute(
        "CREATE INDEX matrix_delivery_outbox_room_scan "
        "ON matrix_delivery_outbox (principal_id, room_id, stage, created_at_ns, delivery_id)",
    )


def _install_legacy_delivery_state(connection: object, *, postgres: bool) -> None:
    """Install representative #1834 response, card, and exact-call debt."""
    execute = cast("Any", connection).execute
    receipt_order = "BIGINT GENERATED BY DEFAULT AS IDENTITY PRIMARY KEY" if postgres else "INTEGER PRIMARY KEY"
    execute(_LEGACY_JOURNAL_EVENTS_DDL.format(receipt_order=receipt_order))
    execute(_LEGACY_RESPONSE_OUTBOX_DDL)
    execute(_LEGACY_APPROVAL_CARDS_DDL)
    execute(
        """
        CREATE TABLE approval_continuations (
            principal_id TEXT NOT NULL, approval_id TEXT NOT NULL UNIQUE,
            entity_name TEXT NOT NULL, state TEXT NOT NULL, generation BIGINT NOT NULL,
            runtime_generation TEXT, failure_reason TEXT, context_json TEXT NOT NULL,
            created_at_ns BIGINT NOT NULL, PRIMARY KEY (principal_id, approval_id)
        )
        """,
    )
    execute(
        """
        CREATE TABLE approval_continuation_sources (
            principal_id TEXT NOT NULL, approval_id TEXT NOT NULL, event_id TEXT NOT NULL,
            source_ordinal BIGINT NOT NULL, PRIMARY KEY (principal_id, approval_id, event_id)
        )
        """,
    )
    execute(
        """
        CREATE TABLE approval_continuation_calls (
            principal_id TEXT NOT NULL, approval_id TEXT NOT NULL, generation BIGINT NOT NULL,
            tool_call_id TEXT NOT NULL, call_ordinal BIGINT NOT NULL, tool_name TEXT NOT NULL,
            invoking_agent TEXT NOT NULL, expires_at_ns BIGINT NOT NULL, decision TEXT, reason TEXT,
            PRIMARY KEY (principal_id, approval_id, generation, tool_call_id)
        )
        """,
    )
    placeholder = "%s" if postgres else "?"

    def insert(sql: str, params: tuple[object, ...]) -> None:
        execute(sql.replace("?", placeholder), params)

    insert(
        """
        INSERT INTO journal_events VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        """,
        (
            1,
            "agent@alice",
            "$response",
            ROOM,
            "",
            "message",
            ALICE,
            1,
            "",
            None,
            0,
            "settled",
        ),
    )
    insert(
        """
        INSERT INTO response_outbox VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        """,
        (
            "agent@alice",
            "$response",
            "final",
            ROOM,
            "",
            "response-txn",
            json.dumps({"body": "frozen response", "msgtype": "m.text"}),
            None,
            0,
            None,
            None,
            10,
        ),
    )
    for approval_id, event_id, created_at_ns in (
        ("approval-1", None, 11),
        ("approval-2", "$unavailable-notice", 12),
    ):
        insert(
            """
            INSERT INTO response_outbox VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                "router@shared",
                f"approval-unavailable:{approval_id}",
                "final",
                ROOM,
                "$thread",
                f"unavailable-txn-{approval_id}",
                json.dumps({"body": f"{approval_id} unavailable", "msgtype": "m.notice"}),
                None,
                int(event_id is not None),
                DEVICE if event_id is not None else None,
                event_id,
                created_at_ns,
            ),
        )
    card_content = {
        "approval_id": "approval-card-1",
        "continuation_id": "approval-1",
        "continuation_generation": 0,
        "tool_call_id": "call-1",
        "tool_name": "shell",
        "thread_id": "$thread",
        "status": "pending",
    }
    insert(
        "INSERT INTO approval_cards VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
        (
            "router@shared",
            ROOM,
            "approval-txn",
            "$approval",
            1,
            DEVICE,
            json.dumps({"type": "io.mindroom.tool_approval", "content": card_content}),
            json.dumps({**card_content, "status": "approved", "body": "Approved: shell"}),
            "approval-1",
            0,
            "call-1",
            0,
            20,
        ),
    )
    insert(
        "INSERT INTO approval_cards VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
        (
            "router@shared",
            ROOM,
            "malformed-approval-txn",
            "$malformed-approval",
            1,
            DEVICE,
            "not-json",
            None,
            "approval-1",
            0,
            "call-malformed",
            0,
            23,
        ),
    )
    unattempted_card_content = {
        **card_content,
        "approval_id": "approval-card-2",
        "tool_call_id": "call-2",
        "full_arguments": {"command": "x" * 60_000},
    }
    insert(
        "INSERT INTO approval_cards VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
        (
            "router@shared",
            ROOM,
            "approval-unattempted-txn",
            None,
            0,
            None,
            json.dumps({"type": "io.mindroom.tool_approval", "content": unattempted_card_content}),
            None,
            "approval-1",
            0,
            "call-2",
            0,
            21,
        ),
    )
    pending_card_content = {
        **card_content,
        "approval_id": "approval-card-4",
        "tool_call_id": "call-4",
    }
    insert(
        "INSERT INTO approval_cards VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
        (
            "router@shared",
            ROOM,
            "approval-pending-txn",
            "$pending-approval",
            1,
            DEVICE,
            json.dumps({"type": "io.mindroom.tool_approval", "content": pending_card_content}),
            None,
            "approval-1",
            0,
            "call-4",
            0,
            22,
        ),
    )
    context = {
        "run_id": "run-1",
        "session_id": "session-1",
        "entity_kind": "agent",
        "room_id": ROOM,
        "thread_id": "$thread",
        "requester_id": ALICE,
        "response_event_id": "$waiting",
    }
    insert(
        "INSERT INTO approval_continuations VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
        ("agent@alice", "approval-1", "agent", "waiting", 0, "publisher", None, json.dumps(context), 15),
    )
    insert(
        "INSERT INTO approval_continuations VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
        (
            "agent@alice",
            "approval-2",
            "agent",
            "failing",
            0,
            None,
            "agent unavailable",
            json.dumps({**context, "response_event_id": "$waiting-acknowledged"}),
            16,
        ),
    )
    insert(
        "INSERT INTO approval_continuation_sources VALUES (?, ?, ?, ?)",
        ("agent@alice", "approval-1", "$source-1", 0),
    )
    insert(
        "INSERT INTO approval_continuation_calls VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
        ("agent@alice", "approval-1", 0, "call-1", 0, "shell", "agent", 999_999, "approved", None),
    )
    insert(
        "INSERT INTO approval_continuation_calls VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
        ("agent@alice", "approval-1", 0, "call-2", 1, "shell", "agent", 999_999, None, None),
    )
    insert(
        "INSERT INTO approval_continuation_calls VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
        ("agent@alice", "approval-1", 0, "call-3", 2, "python", "agent", 999_999, None, None),
    )
    insert(
        "INSERT INTO approval_continuation_calls VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
        ("agent@alice", "approval-1", 0, "call-4", 3, "shell", "agent", 999_999, None, None),
    )


async def _assert_legacy_delivery_state_migrated(store: EventJournalStore) -> None:
    """Assert exact responses migrate while ambiguous debt expires closed."""
    response = await store.principal("agent@alice").load_matrix_delivery(
        delivery_id="$response",
        stage=DeliveryStage.FINAL,
    )
    assert response is not None
    assert response.membership_epoch == 0
    assert response.retired is False
    assert response.payload["io.mindroom.delivery_id"] == {
        "principal": "agent@alice",
        "delivery_id": "$response",
        "stage": "final",
    }

    await _assert_legacy_unavailable_notices_migrated(store)

    router = store.principal("router@shared")
    for delivery_id in ("approval-txn", "approval-pending-txn"):
        for stage in DeliveryStage:
            assert await router.load_matrix_delivery(delivery_id=delivery_id, stage=stage) is None

    assert await router.pending_approval_card(room_id=ROOM, card_event_id="$approval") is None
    assert await router.is_terminal_approval_card(room_id=ROOM, card_event_id="$approval")
    assert await router.is_terminal_approval_card(room_id=ROOM, card_event_id="$pending-approval")

    for stage in DeliveryStage:
        assert (
            await router.load_matrix_delivery(
                delivery_id="approval-unattempted-txn",
                stage=stage,
            )
            is None
        )
        assert (
            await router.load_matrix_delivery(
                delivery_id="malformed-approval-txn",
                stage=stage,
            )
            is None
        )
    assert await router.is_terminal_approval_card(room_id=ROOM, card_event_id="$malformed-approval")

    continuation = await store.principal("agent@alice").approval_continuation("approval-1")
    assert continuation is not None
    assert [call.decision for call in continuation.calls] == [
        ApprovalDecision.APPROVED,
        ApprovalDecision.EXPIRED,
        ApprovalDecision.EXPIRED,
        ApprovalDecision.EXPIRED,
    ]
    assert continuation.state == "ready"
    assert continuation.runtime_generation is None
    assert continuation.presentation_version is None
    assert continuation.show_tool_calls is False


async def _assert_legacy_unavailable_notices_migrated(store: EventJournalStore) -> None:
    """Legacy unavailable-owner notice IDs remain valid generic delivery IDs."""
    missing = await store.principal("router@shared").load_matrix_delivery(
        delivery_id="approval-unavailable:approval-1",
        stage=DeliveryStage.FINAL,
    )
    assert missing is None
    for response_event_id, delivery_id, event_id in (
        ("$waiting-acknowledged", "approval-unavailable:approval-2", "$unavailable-notice"),
    ):
        notice = await store.principal("router@shared").load_matrix_delivery(
            delivery_id=delivery_id,
            stage=DeliveryStage.FINAL,
        )
        assert notice is not None
        assert notice.transaction_id == f"unavailable-txn-{delivery_id.removeprefix('approval-unavailable:')}"
        assert notice.attempted is True
        assert notice.retired is True
        assert notice.sending_device_id == DEVICE
        assert notice.acknowledged_event_id == event_id
        assert (
            await store.principal("router@shared").load_matrix_delivery(
                delivery_id=response_event_id,
                stage=DeliveryStage.FINAL,
            )
            is None
        )


# How long a claimer that is already inside its transaction waits for a second
# claimer to reach its own first statement. Generous next to the sub-millisecond
# round trip it covers, and only ever paid when the second claimer is blocked --
# which is the fix working.
_CONTENDED_CLAIM_WAIT_SECONDS = 0.5

# How long an await that must not finish yet is given to finish anyway before
# the test concludes it cannot. Paid in full only when the backend is correct,
# which is why it is small: an implementation that releases early gets there in
# microseconds, so no plausible machine turns a real failure into a pass.
_MUST_NOT_FINISH_SECONDS = 0.25
# How long a worker thread waits to be let go. Never reached unless the test is
# already failing, so it only bounds the damage.
_WORKER_WAIT_SECONDS = 5.0
# How long a released holder process is given to exit. It is already unblocked
# by the time this is waited on, so reaching it means that process is wedged.
_HOLDER_EXIT_SECONDS = 30.0
# The pause an operation leaves between the statements it holds a connection
# with. Short next to anything it is racing, long enough to cost no core.
_STATEMENT_GAP_SECONDS = 0.001
# How long every write outstanding at a close is given to be answered once that
# close has returned. Reached only when one was abandoned, so it bounds a
# failure rather than waiting one out.
_SETTLEMENT_WAIT_SECONDS = 5.0
# More writes at once than the writer queue used to be bounded at, because the
# bound is what decided how many a close could reach. A close that answers only
# what it dequeues wakes one parked producer per entry, so any count at or
# below the old bound of 512 passes against the bug that stranded the rest.
_WRITES_OUTNUMBERING_THE_OLD_QUEUE_BOUND = 1_100
# One transaction this small is independent of the one-million-message export
# ceiling. The tests feed one more event than the bound so moving the complete
# loop back into one write is a directly observed failure, not a timing
# inference.
_HYDRATION_TRANSACTION_EVENT_LIMIT = 256
_EVENTS_SPANNING_TWO_HYDRATION_CHUNKS = _HYDRATION_TRANSACTION_EVENT_LIMIT + 1

# `PRAGMA synchronous` reports the mode it is set to as an integer.
_SQLITE_SYNCHRONOUS_NORMAL = 1
_SQLITE_SYNCHRONOUS_FULL = 2

# A statement against a real table, so the connection a test's operation holds
# is genuinely in use rather than merely borrowed.
_INSERT_MEMBERSHIP = "INSERT INTO room_membership (principal_id, room_id, membership_epoch) VALUES (?, ?, ?)"


def _hold_the_connection(
    transaction: Transaction,
    running: threading.Event,
    release: threading.Event,
) -> None:
    """Keep real statements on a real connection until the test lets go.

    Statements rather than a parked thread, because a connection taken away
    from an operation that is between statements only raises, while one taken
    away mid-statement is what ends the process. Spaced rather than in a tight
    loop: pinning a core for the whole grace window destabilizes unrelated
    timing-sensitive tests sharing the machine, and the connection is
    continuously in use either way.
    """
    transaction.fetchall("SELECT 1 AS one")
    running.set()
    while not release.wait(_STATEMENT_GAP_SECONDS):
        transaction.fetchall("SELECT 1 AS one")


def _synchronous_mode(transaction: Transaction) -> int:
    """Return the durability the connection running this commits at."""
    row = transaction.fetchone("PRAGMA synchronous")
    assert row is not None
    return int(row["synchronous"])


def _journal_mode(transaction: Transaction) -> str:
    """Return the rollback journal the connection running this uses."""
    row = transaction.fetchone("PRAGMA journal_mode")
    assert row is not None
    return str(row["journal_mode"])


async def _release_writes_the_store_abandoned(
    backend: SqliteBackend,
    abandoned: set[asyncio.Task[object]],
) -> None:
    """End writes a close left waiting, so a failure reads as one.

    Empty on a healthy close, and reached only when the test using it is
    already failing. It exists because an abandoned write cannot be cancelled
    out of the way: ``settled`` deliberately re-enters its wait rather than
    unwinding while the future it holds is still outstanding, so cancelling the
    task does nothing and the only thing that ends it is settling that future.
    Left alone, one stranded producer turns a clear assertion into a session
    that hangs until the suite timeout fires.
    """
    for task in abandoned:
        task.cancel()
    while any(not task.done() for task in abandoned):
        queue = backend._queue
        while queue is not None and not queue.empty():
            queue.get_nowait().future.cancel()
        await asyncio.sleep(0)


async def _finished_within_grace(work: asyncio.Task[object]) -> bool:
    """Give ``work`` every chance to finish and report whether it did.

    Shielded, so waiting cannot itself cancel the thing being watched, and the
    outcome is read off the task rather than off what the wait raised.
    """
    with suppress(asyncio.CancelledError, Exception):
        await asyncio.wait_for(asyncio.shield(work), _MUST_NOT_FINISH_SECONDS)
    return work.done()


async def _membership_principals(store: EventJournalStore) -> list[str]:
    """Return the membership rows a backend test wrote, straight off the backend."""
    rows = await store.backend.read(
        lambda transaction: transaction.fetchall("SELECT principal_id FROM room_membership"),
    )
    return sorted(str(row["principal_id"]) for row in rows)


async def _held_edit_bodies(store: EventJournalStore) -> list[str]:
    """Return every replacement body still held in ``unresolved_edits``.

    Read straight off the backend because that is the whole point: a held edit
    for a target that never arrived has no reader above the table, so the only
    way to ask whether its text is still on this host is to look.
    """
    rows = await store.backend.read(
        lambda transaction: transaction.fetchall("SELECT content_json FROM unresolved_edits"),
    )
    return [str(row["content_json"]) for row in rows]


@pytest.fixture
def alice(journal_store: EventJournalStore) -> PrincipalStore:
    """Return one bound principal view."""
    return journal_store.principal("agent@alice")


def text(body: str) -> dict[str, object]:
    """Return a plain text message body."""
    return {"msgtype": "m.text", "body": body}


def edit(target: str, body: str) -> dict[str, object]:
    """Return an edit of ``target`` installing ``body``."""
    return {
        "msgtype": "m.text",
        "body": f"* {body}",
        "m.new_content": {"msgtype": "m.text", "body": body},
        "m.relates_to": {"rel_type": "m.replace", "event_id": target},
    }


def interactive_prompt(
    question: str,
    value: str,
    *,
    source_event_id: str,
    creator_agent: str = "agent",
) -> dict[str, object]:
    """Return literal Matrix content carrying one journal-authorized prompt."""
    metadata: dict[str, object] = {
        "creator_agent": creator_agent,
        "question_text": question,
        "options": {"1": value},
        "option_labels": {"1": value.title()},
    }
    metadata["source_event_id"] = source_event_id
    return {
        "msgtype": "m.text",
        "body": question,
        "io.mindroom.interactive": metadata,
    }


def interactive_edit(
    target: str,
    question: str,
    value: str,
    *,
    source_event_id: str,
) -> dict[str, object]:
    """Return one Matrix edit whose installed revision carries a prompt."""
    content = edit(target, question)
    metadata = interactive_prompt(question, value, source_event_id=source_event_id)["io.mindroom.interactive"]
    cast("dict[str, object]", content["m.new_content"])["io.mindroom.interactive"] = metadata
    content["io.mindroom.interactive"] = metadata
    return content


def reaction_content(target: str, key: str) -> dict[str, object]:
    """Return one Matrix annotation relation."""
    return {
        "m.relates_to": {
            "rel_type": "m.annotation",
            "event_id": target,
            "key": key,
        },
    }


class _PausingTransaction:
    """One real transaction that runs a hook after its first matching statement.

    With no matcher, every statement matches and this retains the ordinary
    pause-after-first-statement behavior. A matcher opens a causal window at a
    specific SQL boundary, so a race test cannot silently pause on an earlier
    lock and claim it exercised a later one.
    """

    def __init__(
        self,
        inner: Transaction,
        hook: Callable[[], object],
        statement_matches: Callable[[str], bool] | None = None,
    ) -> None:
        self._inner = inner
        self._hook = hook
        self._statement_matches = statement_matches
        self._paused = False

    def _pause(self, sql: str) -> None:
        if not self._paused and (self._statement_matches is None or self._statement_matches(sql)):
            self._paused = True
            self._hook()

    def execute(self, sql: str, params: Sequence[object] = ()) -> None:
        """Run one statement, then pause."""
        self._inner.execute(sql, params)
        self._pause(sql)

    def fetchone(self, sql: str, params: Sequence[object] = ()) -> Mapping[str, object] | None:
        """Run one query, then pause."""
        row = self._inner.fetchone(sql, params)
        self._pause(sql)
        return row

    def fetchall(self, sql: str, params: Sequence[object] = ()) -> tuple[Mapping[str, object], ...]:
        """Run one query, then pause."""
        rows = self._inner.fetchall(sql, params)
        self._pause(sql)
        return rows


@dataclass(frozen=True, slots=True)
class _PausingBackend:
    """A real backend whose writes pause partway through their transaction.

    The races the outbox guards against need one caller to still be inside its
    transaction when another starts, and the public API offers nowhere to stand
    between two statements of the same operation. Wrapping the transaction opens
    that window without substituting anything for the SQL under test.
    """

    inner: Backend
    after_first_statement: Callable[[], object]
    statement_matches: Callable[[str], bool] | None = None

    async def write[T](self, operation: Operation[T]) -> T:
        """Run one write, pausing inside its transaction."""
        return await self.inner.write(
            lambda transaction: operation(
                _PausingTransaction(
                    transaction,
                    self.after_first_statement,
                    self.statement_matches,
                ),
            ),
        )

    async def read[T](self, operation: Operation[T]) -> T:
        """Run one read, unpaused."""
        return await self.inner.read(operation)

    async def close(self) -> None:
        """Close the wrapped backend."""
        await self.inner.close()


@dataclass(slots=True)
class _HydrationWriteShape:
    """The hydration work one real backend transaction was asked to commit."""

    membership_claims: int = 0
    recovery_claims: int = 0
    projected_messages: int = 0
    hydration_markers: int = 0
    recovery_settlements: int = 0
    completeness_retractions: int = 0


class _HydrationWriteTransaction:
    """Profile hydration statements while executing them on a real transaction."""

    def __init__(self, inner: Transaction) -> None:
        self._inner = inner
        self.shape = _HydrationWriteShape()

    def execute(self, sql: str, params: Sequence[object] = ()) -> None:
        """Run one statement and record the durable hydration effect it requests."""
        if "INSERT INTO visible_messages" in sql:
            self.shape.projected_messages += 1
        if "INSERT INTO conversation_hydration" in sql:
            self.shape.hydration_markers += 1
        if "UPDATE room_history_recovery SET state = ?" in sql and params[0] == "repaired":
            self.shape.recovery_settlements += 1
        if "UPDATE conversation_hydration SET complete = 0" in sql:
            self.shape.completeness_retractions += 1
        self._inner.execute(sql, params)

    def fetchone(self, sql: str, params: Sequence[object] = ()) -> Mapping[str, object] | None:
        """Run one query and record its durable fence claims."""
        if "INSERT INTO room_membership" in sql:
            self.shape.membership_claims += 1
        if "UPDATE room_history_recovery SET state = state" in sql:
            self.shape.recovery_claims += 1
        return self._inner.fetchone(sql, params)

    def fetchall(self, sql: str, params: Sequence[object] = ()) -> tuple[Mapping[str, object], ...]:
        """Run one query without changing the write profile."""
        return self._inner.fetchall(sql, params)


@dataclass(slots=True)
class _ObservedHydrationBackend:
    """Observe and control transaction boundaries around the real backend."""

    inner: Backend
    before_write: Callable[[int], Awaitable[None]] | None = None
    after_write: Callable[[int], Awaitable[None]] | None = None
    write_shapes: list[_HydrationWriteShape] = field(default_factory=list)
    writes_started: int = 0

    async def write[T](self, operation: Operation[T]) -> T:
        """Run one profiled write, with optional boundaries for failure races."""
        self.writes_started += 1
        write_number = self.writes_started
        if self.before_write is not None:
            await self.before_write(write_number)
        observed: _HydrationWriteTransaction | None = None

        def profile(transaction: Transaction) -> T:
            nonlocal observed
            observed = _HydrationWriteTransaction(transaction)
            return operation(observed)

        result = await self.inner.write(profile)
        assert observed is not None
        self.write_shapes.append(observed.shape)
        if self.after_write is not None:
            await self.after_write(write_number)
        return result

    async def read[T](self, operation: Operation[T]) -> T:
        """Run reads unchanged on the real backend."""
        return await self.inner.read(operation)

    async def close(self) -> None:
        """Close the wrapped backend."""
        await self.inner.close()


def sidecar(content: dict[str, object]) -> dict[str, object]:
    """Return one edit whose real text lives in an attached file."""
    new_content = dict(cast("dict[str, object]", content["m.new_content"]))
    new_content["io.mindroom.long_text"] = {"version": 2, "encoding": "matrix_event_content_json"}
    new_content["url"] = "mxc://example.org/body"
    return content | {"m.new_content": new_content}


def message(
    event_id: str,
    *,
    room_id: str = ROOM,
    sender: str = ALICE,
    ts: int = 1_000,
    content: Mapping[str, object] | None = None,
    thread_id: str | None = None,
    redacts: str | None = None,
    kind: EventKind = EventKind.MESSAGE,
    event_class: EventClass = EventClass.ACTIONABLE,
) -> tuple[InboundEvent, ProjectedEvent]:
    """Return the admission and projection views of one event."""
    body = dict(content) if content is not None else text(event_id)
    inbound = InboundEvent(
        event_id=event_id,
        room_id=room_id,
        thread_id=thread_id,
        kind=kind,
        event_class=event_class,
        sender=sender,
        origin_server_ts=ts,
        source={"event_id": event_id, "content": body},
    )
    projected = ProjectedEvent(
        event_id=event_id,
        room_id=room_id,
        thread_id=thread_id,
        sender=sender,
        origin_server_ts=ts,
        content=body,
        replaces_event_id=replacement_target(body),
        redacts_event_id=redacts,
    )
    return inbound, projected


def projection(event_id: str, **kwargs: object) -> ProjectedEvent:
    """Return only the projected half of one test event."""
    return message(event_id, **kwargs)[1]  # type: ignore[arg-type]


async def admit(store: PrincipalStore, *args: object, **kwargs: object) -> AdmissionResult:
    """Admit one event built by ``message``."""
    inbound, projected = message(*args, **kwargs)  # type: ignore[arg-type]
    return await store.admit(inbound, projected)


async def bodies(store: PrincipalStore, *, thread_id: str | None = None, limit: int = 50) -> list[str]:
    """Return the visible bodies of one conversation, oldest first."""
    page = await store.read_conversation(room_id=ROOM, thread_id=thread_id, limit=limit)
    return [str(m.content["body"]) for m in page.messages]


def hydration_messages(count: int) -> tuple[ProjectedEvent, ...]:
    """Return enough distinct projected messages to exercise hydration batching."""
    return tuple(message(f"$hydrated-{index:04d}", ts=1_000 + index)[1] for index in range(count))


async def refreshes(
    store: PrincipalStore,
    *,
    thread_id: str | None = None,
    limit: int = 50,
) -> tuple[RefreshRequest, ...]:
    """Return the refetch debts one conversation read reports, the way production learns them."""
    page = await store.read_conversation(room_id=ROOM, thread_id=thread_id, limit=limit)
    return page.refresh_pending


async def _activate_interactive_question(
    store: PrincipalStore,
    question_event_id: str,
    *,
    revision_event_id: str | None = None,
    room_id: str = ROOM,
    thread_id: str | None = "$thread",
    question_text: str = "Choose",
    options: Mapping[str, str] | None = None,
    option_labels: Mapping[str, str] | None = None,
    source_event_id: str = "$turn",
    ts: int | None = None,
) -> None:
    """Admit one self-authored Matrix revision carrying an active prompt."""
    metadata = {
        "creator_agent": "agent",
        "question_text": question_text,
        "options": dict(options or {"1": "one", "👍": "one"}),
        "option_labels": dict(option_labels or {"1": "One", "👍": "One"}),
        "source_event_id": source_event_id,
    }
    content = text(question_text)
    event_id = question_event_id
    if revision_event_id is not None:
        event_id = revision_event_id
        content = edit(question_event_id, question_text)
        cast("dict[str, object]", content["m.new_content"])["io.mindroom.interactive"] = metadata
    content["io.mindroom.interactive"] = metadata
    assert (
        await admit(
            store,
            event_id,
            room_id=room_id,
            sender="alice",
            thread_id=thread_id,
            ts=ts if ts is not None else (3_000 if revision_event_id is not None else 2_000),
            content=content,
        )
        is AdmissionResult.ADMITTED
    )
    await store.settle(event_id)


async def _interactive_question_rows(store: EventJournalStore) -> list[dict[str, object]]:
    """Return currently visible, unconsumed questions as plain test evidence."""
    rows = await store.backend.read(
        lambda transaction: transaction.fetchall(
            """
            SELECT iq.principal_id, iq.question_event_id, iq.room_id, vm.thread_id,
                   iq.revision_event_id, iq.question_json, vm.membership_epoch
            FROM interactive_questions AS iq
            JOIN visible_messages AS vm
              ON vm.principal_id = iq.principal_id
             AND vm.room_id = iq.room_id
             AND vm.logical_event_id = iq.question_event_id
             AND vm.revision_event_id = iq.revision_event_id
            WHERE iq.consumed_by_source_event_id IS NULL
              AND NOT EXISTS (
                  SELECT 1 FROM redaction_tombstones AS tombstone
                  WHERE tombstone.principal_id = iq.principal_id
                    AND tombstone.room_id = iq.room_id
                    AND tombstone.redacted_event_id = iq.revision_event_id
              )
            ORDER BY iq.question_event_id
            """,
        ),
    )
    return [dict(row) for row in rows]


async def _interactive_selection_rows(store: EventJournalStore) -> list[dict[str, object]]:
    """Return source-bound selection snapshots as plain test evidence."""
    rows = await store.backend.read(
        lambda transaction: transaction.fetchall(
            """
            SELECT principal_id, source_event_id, question_event_id,
                   revision_event_id, selection_key
            FROM interactive_selections
            ORDER BY source_event_id
            """,
        ),
    )
    return [dict(row) for row in rows]


async def _membership_accepts_question(store: PrincipalStore, epoch: int) -> bool:
    """Probe active membership through the same row-locked predicate as prompt admission."""
    return await store._backend.write(
        lambda transaction: reads.claim_membership_epoch(
            transaction,
            store._principal_id,
            room_id=ROOM,
            expected_membership_epoch=epoch,
        ),
    )


class TestPrincipalIsolation:
    """One database, many bots, no way to reach across."""

    async def test_bound_views_cannot_see_each_other(self, journal_store: EventJournalStore) -> None:
        """Bound views cannot see each other."""
        first = journal_store.principal("agent@one")
        second = journal_store.principal("agent@two")

        await admit(first, "$only-mine")

        assert await bodies(first) == ["$only-mine"]
        assert await bodies(second) == []
        assert await second.load_event("$only-mine") is None
        assert [event.event_id for event in await first.pending()] == ["$only-mine"]
        assert await second.pending() == ()

    async def test_settling_is_bound_to_its_principal(self, journal_store: EventJournalStore) -> None:
        """Settling is bound to its principal."""
        first = journal_store.principal("agent@one")
        second = journal_store.principal("agent@two")
        await admit(first, "$shared-id")
        await admit(second, "$shared-id")

        await second.settle("$shared-id")

        assert await first.is_pending("$shared-id")
        assert not await second.is_pending("$shared-id")


class TestAdmission:
    """The journal decides exactly once what MindRoom accepted."""

    async def test_admitting_twice_creates_one_pending_event(self, alice: PrincipalStore) -> None:
        """Admitting twice creates one pending event."""
        assert await admit(alice, "$one") is AdmissionResult.ADMITTED
        assert await admit(alice, "$one") is AdmissionResult.DUPLICATE

        assert [event.event_id for event in await alice.pending()] == ["$one"]

    async def test_a_second_disagreeing_payload_for_one_event_id_never_projects(
        self,
        alice: PrincipalStore,
    ) -> None:
        """One event ID reaches the projection at most once, whatever it carries.

        This is why the projection needs no tiebreak for two payloads claiming
        one event ID, and the visible-message scan does. That scan merges a
        replacement bundled under `unsigned` with the standalone copy of the
        same event from the same page, so it genuinely holds two payloads for
        one ID and has to order them by content rather than by arrival.

        Nothing writing here can produce that pair. Admission conflicts on the
        `(principal_id, event_id)` primary key and returns before `project()`
        is called at all, so the losing payload is not ranked against the
        stored revision -- it never reaches the comparison. Unifying the two
        rules would move a JSON dump of every replacement onto this path, which
        streaming walks tens to hundreds of times per response, to decide a
        case it cannot observe.

        The second payload is given a later timestamp deliberately. A real
        second copy of one event carries the server's own `origin_server_ts`
        and would tie, and a tie is refused by the ordering rule as well as by
        the key -- so a tied payload would prove only that one of the two
        defences held. A payload that would win on the ordering rule isolates
        the key as the thing that stops it.
        """
        await admit(alice, "$original")
        first = await admit(alice, "$edit", ts=2_000, content=edit("$original", "first payload"))
        assert first is AdmissionResult.ADMITTED

        second = await admit(alice, "$edit", ts=3_000, content=edit("$original", "second payload"))

        assert second is AdmissionResult.DUPLICATE
        assert await bodies(alice) == ["first payload"]

    async def test_pending_replays_in_receipt_order(self, alice: PrincipalStore) -> None:
        """Replay order is admission order, not the senders' clocks."""
        await admit(alice, "$late-clock", ts=9_000)
        await admit(alice, "$early-clock", ts=1_000)

        assert [event.event_id for event in await alice.pending()] == ["$late-clock", "$early-clock"]

    async def test_context_only_events_never_become_pending(self, alice: PrincipalStore) -> None:
        """Cold history populates the conversation without starting work."""
        await admit(alice, "$history", event_class=EventClass.CONTEXT_ONLY)

        assert await alice.pending() == ()
        assert await bodies(alice) == ["$history"]

    async def test_settled_events_stay_out_of_replay(self, alice: PrincipalStore) -> None:
        """Settled events stay out of replay."""
        await admit(alice, "$one")
        await alice.settle("$one")

        assert await alice.pending() == ()
        assert await admit(alice, "$one") is AdmissionResult.DUPLICATE
        assert await alice.pending() == ()

    async def test_settlement_releases_the_replay_payload(self, alice: PrincipalStore) -> None:
        """The row outlives its payload: it is the proof, not the work item."""
        await admit(alice, "$one")
        await alice.settle("$one")

        settled = await alice.load_event("$one")
        assert settled is not None
        assert settled.source == {}

    async def test_replay_payload_survives_until_settlement(self, alice: PrincipalStore) -> None:
        """Replay payload survives until settlement."""
        await admit(alice, "$one")

        pending = await alice.pending()
        assert pending[0].source == {"event_id": "$one", "content": text("$one")}


class TestEditReduction:
    """One row per logical message, whatever order the events arrive in."""

    async def test_edit_replaces_the_visible_body(self, alice: PrincipalStore) -> None:
        """Edit replaces the visible body."""
        await admit(alice, "$original", content=text("first"))
        await admit(alice, "$edit", ts=2_000, content=edit("$original", "second"))

        assert await bodies(alice) == ["second"]

    async def test_older_edit_arriving_late_does_not_win(self, alice: PrincipalStore) -> None:
        """Older edit arriving late does not win."""
        await admit(alice, "$original", content=text("first"))
        await admit(alice, "$new", ts=3_000, content=edit("$original", "newest"))
        await admit(alice, "$old", ts=2_000, content=edit("$original", "stale"))

        assert await bodies(alice) == ["newest"]

    async def test_same_timestamp_edits_resolve_by_event_id(self, alice: PrincipalStore) -> None:
        """Timestamps tie; the total order has to come from somewhere."""
        await admit(alice, "$original", content=text("first"))
        await admit(alice, "$aaa", ts=2_000, content=edit("$original", "from-aaa"))
        await admit(alice, "$zzz", ts=2_000, content=edit("$original", "from-zzz"))

        assert await bodies(alice) == ["from-zzz"]

    async def test_same_timestamp_edits_resolve_by_event_id_either_order(
        self,
        alice: PrincipalStore,
    ) -> None:
        """Same timestamp edits resolve by event id either order."""
        await admit(alice, "$original", content=text("first"))
        await admit(alice, "$zzz", ts=2_000, content=edit("$original", "from-zzz"))
        await admit(alice, "$aaa", ts=2_000, content=edit("$original", "from-aaa"))

        assert await bodies(alice) == ["from-zzz"]

    async def test_edit_before_original_applies_when_the_original_lands(
        self,
        alice: PrincipalStore,
    ) -> None:
        """Edit before original applies when the original lands."""
        await admit(alice, "$edit", ts=2_000, content=edit("$original", "second"))
        assert await bodies(alice) == []

        await admit(alice, "$original", content=text("first"))
        assert await bodies(alice) == ["second"]

    async def test_a_stranger_cannot_evict_the_authors_pending_edit(
        self,
        alice: PrincipalStore,
    ) -> None:
        """The unresolved-edit key includes the sender, and this is why.

        Without it, anyone could send an edit for a message that has not
        arrived yet and displace the real author's edit before it could apply.
        """
        await admit(alice, "$alice-edit", sender=ALICE, ts=2_000, content=edit("$original", "authored"))
        await admit(alice, "$bob-edit", sender=BOB, ts=9_000, content=edit("$original", "forged"))

        await admit(alice, "$original", sender=ALICE, content=text("first"))

        assert await bodies(alice) == ["authored"]

    async def test_an_edit_from_another_sender_never_applies(self, alice: PrincipalStore) -> None:
        """An edit from another sender never applies."""
        await admit(alice, "$original", sender=ALICE, content=text("first"))
        await admit(alice, "$forged", sender=BOB, ts=9_000, content=edit("$original", "forged"))

        assert await bodies(alice) == ["first"]

    async def test_only_the_latest_unresolved_edit_is_kept(self, alice: PrincipalStore) -> None:
        """Only the latest unresolved edit is kept."""
        await admit(alice, "$e1", ts=2_000, content=edit("$original", "one"))
        await admit(alice, "$e2", ts=3_000, content=edit("$original", "two"))
        await admit(alice, "$e3", ts=2_500, content=edit("$original", "middle"))

        await admit(alice, "$original", content=text("first"))

        assert await bodies(alice) == ["two"]

    @pytest.mark.parametrize("edit_count", [1, 5, 25])
    async def test_edit_churn_leaves_one_row_and_no_history(
        self,
        alice: PrincipalStore,
        edit_count: int,
    ) -> None:
        """Streaming rewrites one projected row rather than accumulating messages."""
        await admit(alice, "$original", content=text("chunk 0"))
        for index in range(1, edit_count + 1):
            await admit(
                alice,
                f"$edit-{index:04d}",
                ts=1_000 + index,
                content=edit("$original", f"chunk {index}"),
            )

        page = await alice.read_conversation(room_id=ROOM, thread_id=None, limit=100)
        assert len(page.messages) == 1
        assert page.messages[0].content["body"] == f"chunk {edit_count}"
        assert page.messages[0].logical_event_id == "$original"


class TestRedaction:
    """Deleted content stops being readable in the transaction that admits it."""

    @pytest.mark.parametrize("source_first", [True, False], ids=["source-first", "redaction-first"])
    async def test_tombstoned_pending_turn_source_is_terminal(
        self,
        alice: PrincipalStore,
        *,
        source_first: bool,
    ) -> None:
        """Either admission order retires the source without removing its dedup proof."""
        if not source_first:
            await admit(alice, "$redaction", ts=2_000, redacts="$source", kind=EventKind.REDACTION)
        await admit(alice, "$source", content=text("secret"))
        if source_first:
            await admit(alice, "$redaction", ts=2_000, redacts="$source", kind=EventKind.REDACTION)

        assert not await alice.is_pending("$source")
        settled_source = await alice.load_event("$source")
        assert settled_source is not None
        assert settled_source.source == {}
        assert settled_source.semantic_consumer is None
        assert [event.event_id for event in await alice.pending()] == ["$redaction"]
        assert await bodies(alice) == []

    async def test_redaction_settlement_leaves_other_pending_work_unchanged(self, alice: PrincipalStore) -> None:
        """Only a matching turn-backed source becomes terminal."""
        await admit(alice, "$unrelated")
        reaction, _ = message("$reaction", kind=EventKind.REACTION)
        await alice.admit(reaction, None)

        await admit(alice, "$redaction", ts=2_000, redacts="$reaction", kind=EventKind.REDACTION)

        assert [event.event_id for event in await alice.pending()] == ["$unrelated", "$reaction", "$redaction"]

    async def test_redacting_the_original_removes_the_message(self, alice: PrincipalStore) -> None:
        """Redacting the original removes the message."""
        await admit(alice, "$original", content=text("secret"))
        await admit(alice, "$redaction", ts=2_000, redacts="$original", kind=EventKind.REDACTION)

        page = await alice.read_conversation(room_id=ROOM, thread_id=None, limit=50)
        assert page.messages == ()
        assert page.refresh_pending == ()

    async def test_a_redacted_original_cannot_be_resurrected(self, alice: PrincipalStore) -> None:
        """Backfill really does deliver a redaction before what it redacts."""
        await admit(alice, "$redaction", ts=2_000, redacts="$original", kind=EventKind.REDACTION)
        await admit(alice, "$original", content=text("secret"))

        assert await bodies(alice) == []

    async def test_a_redacted_edit_cannot_be_resurrected(self, alice: PrincipalStore) -> None:
        """A redacted edit cannot be resurrected."""
        await admit(alice, "$redaction", ts=3_000, redacts="$edit", kind=EventKind.REDACTION)
        await admit(alice, "$edit", ts=2_000, content=edit("$original", "deleted"))
        await admit(alice, "$original", content=text("first"))

        assert await bodies(alice) == ["first"]

    async def test_redacting_a_target_that_never_arrived_drops_its_held_edit(
        self,
        alice: PrincipalStore,
        journal_store: EventJournalStore,
    ) -> None:
        """The replacement text must not outlive the redaction of what it revises.

        An edit that arrives before its target is parked in ``unresolved_edits``
        with its body, and only the target's arrival collected it. Redacting a
        target that never arrived left the row untouched: the target never
        lands afterwards either, because ``project`` turns it away at the
        tombstone, so nothing was left to collect it for the rest of the
        membership epoch.

        The body is what makes that matter. Settlement blanks the journal's own
        ``source_json``, so this row is the last copy of the replacement text on
        the host -- and `docs/architecture/matrix-event-journal-security.md`
        states the row is deleted the moment its target lands *or is redacted*.
        """
        await admit(alice, "$edit", ts=2_000, content=edit("$original", "secret replacement"))
        assert [body for body in await _held_edit_bodies(journal_store) if "secret replacement" in body]

        await admit(alice, "$redaction", ts=3_000, redacts="$original", kind=EventKind.REDACTION)

        assert await _held_edit_bodies(journal_store) == []

    async def test_a_held_edit_never_coexists_with_a_visible_target(
        self,
        alice: PrincipalStore,
        journal_store: EventJournalStore,
    ) -> None:
        """Why the delete had to move rather than be duplicated.

        An edit is only held when its target has no visible row, and the one
        statement that makes a row visible applies and drops the held edits for
        it in the same transaction. So the two states are mutually exclusive,
        and a delete guarded on the target being visible could only ever match
        nothing -- which is how the guarded one survived without any test
        noticing it did no work.
        """
        await admit(alice, "$original", content=text("first"))
        await admit(alice, "$forged", sender=BOB, ts=2_000, content=edit("$original", "forged"))

        assert await _held_edit_bodies(journal_store) == []
        assert await bodies(alice) == ["first"]

    async def test_redacting_a_superseded_edit_changes_nothing(self, alice: PrincipalStore) -> None:
        """Redacting a superseded edit changes nothing."""
        await admit(alice, "$original", content=text("first"))
        await admit(alice, "$edit1", ts=2_000, content=edit("$original", "second"))
        await admit(alice, "$edit2", ts=3_000, content=edit("$original", "third"))

        await admit(alice, "$redaction", ts=4_000, redacts="$edit1", kind=EventKind.REDACTION)

        page = await alice.read_conversation(room_id=ROOM, thread_id=None, limit=50)
        assert [m.content["body"] for m in page.messages] == ["third"]
        assert page.refresh_pending == ()

    async def test_redacting_the_visible_edit_hides_it_and_asks_for_a_refetch(
        self,
        alice: PrincipalStore,
    ) -> None:
        """Redacting the visible edit hides it and asks for a refetch."""
        await admit(alice, "$original", content=text("first"))
        await admit(alice, "$edit", ts=2_000, content=edit("$original", "second"))

        await admit(alice, "$redaction", ts=3_000, redacts="$edit", kind=EventKind.REDACTION)

        page = await alice.read_conversation(room_id=ROOM, thread_id=None, limit=50)
        assert page.messages == ()
        assert [request.logical_event_id for request in page.refresh_pending] == ["$original"]

    async def test_no_read_of_any_kind_returns_the_redacted_revision(
        self,
        alice: PrincipalStore,
    ) -> None:
        """The gate is the cleared body, not the caller's willingness to wait."""
        await admit(alice, "$original", content=text("first"))
        await admit(alice, "$edit", ts=2_000, content=edit("$original", "deleted"))
        await admit(alice, "$redaction", ts=3_000, redacts="$edit", kind=EventKind.REDACTION)

        page = await alice.read_conversation(room_id=ROOM, thread_id=None, limit=50)
        cursor_page = await alice.read_conversation(
            room_id=ROOM,
            thread_id=None,
            limit=50,
            before=ConversationCursor(created_ts=99_999, logical_event_id="$zzzzz"),
        )
        tiny_page = await alice.read_conversation(room_id=ROOM, thread_id=None, limit=1)

        for read in (page, cursor_page, tiny_page):
            assert all(m.content["body"] != "deleted" for m in read.messages)

    async def test_the_refresh_token_survives_a_restart(
        self,
        journal_store: EventJournalStore,
    ) -> None:
        """A pending refetch is durable, so a crash cannot un-hide the content."""
        store = journal_store.principal("agent@alice")
        await admit(store, "$original", content=text("first"))
        await admit(store, "$edit", ts=2_000, content=edit("$original", "deleted"))
        await admit(store, "$redaction", ts=3_000, redacts="$edit", kind=EventKind.REDACTION)

        reopened = journal_store.principal("agent@alice")
        page = await reopened.read_conversation(room_id=ROOM, thread_id=None, limit=50)
        assert page.messages == ()
        assert len(page.refresh_pending) == 1

    async def test_a_failed_refetch_keeps_the_message_hidden(self, alice: PrincipalStore) -> None:
        """A failed refetch keeps the message hidden."""
        await admit(alice, "$original", content=text("first"))
        await admit(alice, "$edit", ts=2_000, content=edit("$original", "deleted"))
        await admit(alice, "$redaction", ts=3_000, redacts="$edit", kind=EventKind.REDACTION)

        requests = await refreshes(alice)
        assert len(requests) == 1

        still_pending = await refreshes(alice)
        assert still_pending == requests

    async def test_a_thread_read_can_repair_its_own_root(self, alice: PrincipalStore) -> None:
        """A read that can see a message must be able to repair it.

        The root belongs to the room conversation, so a thread read merges it
        in. The refetch debt it owes has to be merged in by that same read, or a
        strict thread read raises forever: it reports the root as needing a
        refetch that nothing will ever be asked to perform.
        """
        await admit(alice, "$root", content=text("first"))
        await admit(alice, "$reply", ts=2_000, thread_id="$root")
        await admit(alice, "$edit", ts=3_000, content=edit("$root", "deleted"))
        await admit(alice, "$redaction", ts=4_000, redacts="$edit", kind=EventKind.REDACTION)

        requests = await refreshes(alice, thread_id="$root")

        assert [request.logical_event_id for request in requests] == ["$root"]

    async def test_a_successful_refetch_installs_the_server_revision(
        self,
        alice: PrincipalStore,
    ) -> None:
        """A successful refetch installs the server revision."""
        await admit(alice, "$original", content=text("first"))
        await admit(alice, "$edit", ts=2_000, content=edit("$original", "deleted"))
        await admit(alice, "$redaction", ts=3_000, redacts="$edit", kind=EventKind.REDACTION)
        request = (await refreshes(alice))[0]

        installed = await alice.install_refetched_revision(
            request,
            revision_event_id="$original",
            revision_ts=1_000,
            revision_sender="alice",
            content=text("first"),
        )

        assert installed
        assert await bodies(alice) == ["first"]
        assert await refreshes(alice) == ()

    async def test_a_refetch_cannot_install_a_revision_that_was_itself_redacted(
        self,
        alice: PrincipalStore,
    ) -> None:
        """A redaction the token cannot see must still stop the install.

        Redacting a revision that is not the one on screen matches no visible
        row, so it correctly moves no refresh token -- but it does record a
        tombstone. A refetch already in flight, having picked that very revision
        off the server, still matches the token it was issued with. Without the
        tombstone check it would install a body the sender deleted, and nothing
        later disturbs it: hydration does not re-run under the same membership,
        so the deleted text would reach every prompt, summary and export of the
        room from then on.
        """
        await admit(alice, "$original", content=text("first"))
        await admit(alice, "$e1", ts=2_000, content=edit("$original", "first edit"))
        await admit(alice, "$e2", ts=3_000, content=edit("$original", "second edit"))
        await admit(alice, "$red2", ts=4_000, redacts="$e2", kind=EventKind.REDACTION)
        request = (await refreshes(alice))[0]

        # Redacting the superseded revision the refetch happens to be carrying.
        await admit(alice, "$red1", ts=5_000, redacts="$e1", kind=EventKind.REDACTION)

        installed = await alice.install_refetched_revision(
            request,
            revision_event_id="$e1",
            revision_ts=2_000,
            revision_sender="alice",
            content=text("first edit"),
        )

        assert not installed
        assert "first edit" not in await bodies(alice)

    async def test_a_newer_edit_beats_an_in_flight_refetch(self, alice: PrincipalStore) -> None:
        """The refetch read the server before the newer edit existed."""
        await admit(alice, "$original", content=text("first"))
        await admit(alice, "$edit", ts=2_000, content=edit("$original", "deleted"))
        await admit(alice, "$redaction", ts=3_000, redacts="$edit", kind=EventKind.REDACTION)
        stale_request = (await refreshes(alice))[0]

        await admit(alice, "$newer", ts=4_000, content=edit("$original", "newest"))

        installed = await alice.install_refetched_revision(
            stale_request,
            revision_event_id="$original",
            revision_ts=1_000,
            revision_sender="alice",
            content=text("first"),
        )

        assert not installed
        assert await bodies(alice) == ["newest"]

    async def test_a_stale_zero_token_cannot_drop_a_newer_outbox_projection(
        self,
        alice: PrincipalStore,
    ) -> None:
        """Revision identity must disambiguate sidecar debts projected outside ingress."""
        await admit(alice, "$target", sender="alice", content=text("first"))

        async def acknowledge_sidecar_edit(turn_id: str, event_id: str, timestamp: int) -> None:
            content = sidecar(edit("$target", event_id))
            await alice.enqueue_matrix_delivery(
                delivery_id=turn_id,
                stage=DeliveryStage.FINAL,
                room_id=ROOM,
                thread_id=None,
                payload=content,
                edits_event_id="$target",
            )
            await alice.claim_matrix_delivery(delivery_id=turn_id, stage=DeliveryStage.FINAL)
            await alice.acknowledge_matrix_delivery(
                delivery_id=turn_id,
                stage=DeliveryStage.FINAL,
                event_id=event_id,
                delivered_projections=(
                    projection(
                        event_id,
                        sender="alice",
                        ts=timestamp,
                        content=content,
                    ),
                ),
            )

        await acknowledge_sidecar_edit("$first-turn", "$first-edit", 2_000)
        stale_request = (await refreshes(alice))[0]
        await acknowledge_sidecar_edit("$second-turn", "$second-edit", 3_000)

        assert not await alice.drop_refetched_message(stale_request)
        assert [request.logical_event_id for request in await refreshes(alice)] == ["$target"]

    async def test_a_refetch_can_remove_a_message_the_server_lost(
        self,
        alice: PrincipalStore,
    ) -> None:
        """A refetch can remove a message the server lost."""
        await admit(alice, "$original", content=text("first"))
        await admit(alice, "$edit", ts=2_000, content=edit("$original", "deleted"))
        await admit(alice, "$redaction", ts=3_000, redacts="$edit", kind=EventKind.REDACTION)
        request = (await refreshes(alice))[0]

        assert await alice.drop_refetched_message(request)
        page = await alice.read_conversation(room_id=ROOM, thread_id=None, limit=50)
        assert page.messages == ()
        assert page.refresh_pending == ()


async def corrupt(store: PrincipalStore, *event_ids: str) -> None:
    """Make some admitted rows' replay payloads undecodable, in one transaction."""
    placeholders = ", ".join("?" for _ in event_ids)
    await store._backend.write(
        lambda transaction: transaction.execute(
            f"UPDATE journal_events SET source_json = ? WHERE event_id IN ({placeholders})",  # noqa: S608
            ("{", *event_ids),
        ),
    )


class TestUnreadableRowsDoNotEndTheBacklog:
    """A short page of pending work has to mean one thing, or paging is a lie."""

    async def test_a_corrupt_row_shortens_its_page_without_ending_the_backlog(
        self,
        alice: PrincipalStore,
    ) -> None:
        """A caller paging on the result length would stop here and never resume.

        Dropping the row is right -- nothing ran, so nothing may claim it did --
        but a page that comes back short for that reason is indistinguishable
        from the end of the backlog *by its length*. Everything behind the
        corrupt row would then be durable work no scan looks at again, so the
        page has to say it stopped short and where to resume.
        """
        for index in range(5):
            await admit(alice, f"$m{index}", ts=1_000 + index)
        await corrupt(alice, "$m1")

        page = await alice.pending(limit=4)

        assert [event.event_id for event in page] == ["$m0", "$m2", "$m3"]
        assert page.unreadable_rows == 1
        assert not page.reached_end
        assert [event.event_id for event in await alice.pending(limit=4, after_receipt_order=page.resume_after)] == [
            "$m4",
        ]

    async def test_a_page_of_nothing_but_corrupt_rows_still_carries_a_cursor(
        self,
        alice: PrincipalStore,
    ) -> None:
        """Otherwise the scan stalls: no events back, and no cursor to move past."""
        for index in range(3):
            await admit(alice, f"$m{index}", ts=1_000 + index)
        await corrupt(alice, "$m0", "$m1")

        page = await alice.pending(limit=2)

        assert page == ()
        assert page.unreadable_rows == 2
        assert not page.reached_end
        assert [event.event_id for event in await alice.pending(limit=2, after_receipt_order=page.resume_after)] == [
            "$m2",
        ]

    async def test_a_short_page_still_means_the_end_of_the_backlog(
        self,
        alice: PrincipalStore,
    ) -> None:
        """The signal the caller paginates on has to keep working."""
        for index in range(3):
            await admit(alice, f"$m{index}", ts=1_000 + index)

        assert len(await alice.pending(limit=10)) == 3
        assert await alice.pending(limit=10, after_receipt_order=1_000) == ()


class TestAPageIsBoundedByTheRowsItReads:
    """A page's limit counts rows the query read, not events it managed to decode."""

    async def test_a_page_stops_at_its_row_limit_with_readable_work_behind_it(
        self,
        alice: PrincipalStore,
    ) -> None:
        """The whole bound is this: one query's worth of rows, then report.

        Going back for more rows because too few decoded is what made the read
        unbounded, and it is invisible unless the events behind the corruption
        are readable -- a page that returns them read more rows than it was
        given a limit of.
        """
        for index in range(8):
            await admit(alice, f"$m{index}", ts=1_000 + index)
        await corrupt(alice, "$m0", "$m1")

        page = await alice.pending(limit=4)

        assert [event.event_id for event in page] == ["$m2", "$m3"]
        assert page.unreadable_rows == 2
        assert not page.reached_end
        # Four rows read, so the next pass starts on the fifth and nothing in
        # between is skipped or repeated.
        assert [event.event_id for event in await alice.pending(limit=4, after_receipt_order=page.resume_after)] == [
            "$m4",
            "$m5",
            "$m6",
            "$m7",
        ]

    async def test_a_corrupt_prefix_does_not_cost_one_pass_the_whole_backlog(
        self,
        alice: PrincipalStore,
    ) -> None:
        """A bounded read that keeps querying until something decodes is not bounded.

        The rows are read inside one transaction and every one of them is
        logged, so a corrupt prefix long enough turns each wake of the pending
        scan into a scan of the entire pending table. The pass has to be able
        to stop part way and say so.
        """
        for index in range(12):
            await admit(alice, f"$m{index:02d}", ts=1_000 + index)
        await corrupt(alice, *(f"$m{index:02d}" for index in range(11)))

        page = await alice.pending(limit=4)

        assert page == ()
        assert page.unreadable_rows == 4
        assert not page.reached_end
        assert page.resume_after is not None

    async def test_a_pass_that_ran_out_of_budget_resumes_past_what_it_read(
        self,
        alice: PrincipalStore,
    ) -> None:
        """Bounding the pass may not cost the events behind the corruption.

        The resume point is the last row looked at rather than the last event
        returned, which is the only version of it that exists when a page
        decodes nothing at all.
        """
        for index in range(12):
            await admit(alice, f"$m{index:02d}", ts=1_000 + index)
        await corrupt(alice, *(f"$m{index:02d}" for index in range(11)))

        seen: list[str] = []
        cursor: int | None = None
        for _ in range(12):
            page = await alice.pending(limit=4, after_receipt_order=cursor)
            seen.extend(event.event_id for event in page)
            if page.reached_end:
                break
            cursor = page.resume_after

        assert seen == ["$m11"]


class TestBoundedReads:
    """Reads are paged; there is no call that returns a whole room."""

    async def test_a_read_requires_a_positive_limit(self, alice: PrincipalStore) -> None:
        """A read requires a positive limit."""
        with pytest.raises(ValueError, match="positive limit"):
            await alice.read_conversation(room_id=ROOM, thread_id=None, limit=0)

    async def test_pages_walk_backwards_without_gaps_or_repeats(
        self,
        alice: PrincipalStore,
    ) -> None:
        """Pages walk backwards without gaps or repeats."""
        for index in range(25):
            await admit(alice, f"$m{index:03d}", ts=1_000 + index)

        seen: list[str] = []
        cursor = None
        while True:
            page = await alice.read_conversation(room_id=ROOM, thread_id=None, limit=7, before=cursor)
            seen = [m.logical_event_id for m in page.messages] + seen
            if page.next_cursor is None:
                break
            cursor = page.next_cursor

        assert seen == [f"$m{index:03d}" for index in range(25)]

    async def test_a_page_is_chronological(self, alice: PrincipalStore) -> None:
        """A page is chronological."""
        for index in range(5):
            await admit(alice, f"$m{index}", ts=1_000 + index)

        page = await alice.read_conversation(room_id=ROOM, thread_id=None, limit=5)
        assert [m.created_ts for m in page.messages] == [1_000, 1_001, 1_002, 1_003, 1_004]

    async def test_threads_are_separate_conversations(self, alice: PrincipalStore) -> None:
        """Threads are separate conversations."""
        await admit(alice, "$room-message")
        await admit(alice, "$thread-message", thread_id="$root")

        assert await bodies(alice) == ["$room-message"]
        assert await bodies(alice, thread_id="$root") == ["$thread-message"]

    async def test_a_thread_read_includes_its_root(self, alice: PrincipalStore) -> None:
        """The root has no thread relation of its own, but the thread is about it."""
        await admit(alice, "$root", ts=1_000)
        await admit(alice, "$reply", ts=2_000, thread_id="$root")

        assert await bodies(alice, thread_id="$root") == ["$root", "$reply"]

    async def test_the_root_appears_once_even_when_it_is_also_a_reply(
        self,
        alice: PrincipalStore,
    ) -> None:
        """The root appears once even when it is also a reply."""
        await admit(alice, "$root", ts=1_000, thread_id="$root")
        await admit(alice, "$reply", ts=2_000, thread_id="$root")

        assert await bodies(alice, thread_id="$root") == ["$root", "$reply"]

    async def test_a_thread_root_still_belongs_to_the_room_conversation(
        self,
        alice: PrincipalStore,
    ) -> None:
        """A thread root still belongs to the room conversation."""
        await admit(alice, "$root", ts=1_000)
        await admit(alice, "$reply", ts=2_000, thread_id="$root")

        assert await bodies(alice) == ["$root"]

    async def test_a_thread_page_respects_its_limit_with_the_root_merged(
        self,
        alice: PrincipalStore,
    ) -> None:
        """A thread page respects its limit with the root merged."""
        await admit(alice, "$root", ts=1_000)
        for index in range(5):
            await admit(alice, f"$reply{index}", ts=2_000 + index, thread_id="$root")

        page = await alice.read_conversation(room_id=ROOM, thread_id="$root", limit=3)

        assert [m.logical_event_id for m in page.messages] == ["$reply2", "$reply3", "$reply4"]

    async def test_paging_a_thread_reaches_the_root_last(self, alice: PrincipalStore) -> None:
        """Paging a thread reaches the root last."""
        await admit(alice, "$root", ts=1_000)
        for index in range(5):
            await admit(alice, f"$reply{index}", ts=2_000 + index, thread_id="$root")

        seen: list[str] = []
        cursor = None
        while True:
            page = await alice.read_conversation(
                room_id=ROOM,
                thread_id="$root",
                limit=2,
                before=cursor,
            )
            seen = [m.logical_event_id for m in page.messages] + seen
            if page.next_cursor is None:
                break
            cursor = page.next_cursor

        assert seen == ["$root", "$reply0", "$reply1", "$reply2", "$reply3", "$reply4"]

    async def test_ordering_agrees_with_python_for_mixed_case_ids(
        self,
        alice: PrincipalStore,
    ) -> None:
        """SQLite, PostgreSQL, and Python must agree on the cursor's order.

        PostgreSQL's default locale can sort ``'a'`` before ``'B'`` while
        SQLite and Python sort by byte. If the cursor column is not pinned to
        byte order, paging silently skips or repeats rows on one backend only.
        """
        identifiers = ["$aaa", "$BBB", "$aBc", "$Abc", "$zzz", "$ZZZ"]
        for event_id in identifiers:
            await admit(alice, event_id, ts=5_000)

        page = await alice.read_conversation(room_id=ROOM, thread_id=None, limit=10)
        assert [m.logical_event_id for m in page.messages] == sorted(identifiers)

    async def test_cursor_paging_matches_byte_order_across_a_timestamp_tie(
        self,
        alice: PrincipalStore,
    ) -> None:
        """Cursor paging matches byte order across a timestamp tie."""
        identifiers = ["$aaa", "$BBB", "$aBc", "$Abc", "$zzz", "$ZZZ"]
        for event_id in identifiers:
            await admit(alice, event_id, ts=5_000)

        seen: list[str] = []
        cursor = None
        while True:
            page = await alice.read_conversation(room_id=ROOM, thread_id=None, limit=2, before=cursor)
            seen = [m.logical_event_id for m in page.messages] + seen
            if page.next_cursor is None:
                break
            cursor = page.next_cursor

        assert seen == sorted(identifiers)


class TestStoreGeneration:
    """The identity a sync checkpoint is saved beside."""

    async def test_the_generation_is_minted_once_and_then_kept(
        self,
        journal_store: EventJournalStore,
    ) -> None:
        """A second open must not rewrite it.

        This is the whole mechanism. If reopening the store minted a new
        generation, every saved checkpoint would be rejected on every restart
        and the bot would resync from scratch forever. If it *overwrote* the
        stored one, no checkpoint would ever be rejected and a replaced
        database would silently resume from a token covering events it never
        saw.
        """
        first = await journal_store.generation(new_generation="born-here")
        second = await journal_store.generation(new_generation="a-later-process")

        assert first == "born-here"
        assert second == "born-here"

    async def test_a_different_database_has_a_different_generation(
        self,
        journal_store: EventJournalStore,
        tmp_path: Path,
    ) -> None:
        """Two stores must not agree, or the check proves nothing."""
        mine = await journal_store.generation(new_generation="mine")
        replacement = EventJournalStore.open_sqlite(tmp_path / "replacement.db")
        try:
            theirs = await replacement.generation(new_generation="theirs")
        finally:
            await replacement.close()

        assert mine != theirs

    async def test_the_generation_belongs_to_the_database_not_a_principal(
        self,
        journal_store: EventJournalStore,
    ) -> None:
        """Every principal in one database lost the same history if it were replaced."""
        generation = await journal_store.generation(new_generation="shared")

        await admit(journal_store.principal(ALICE), "$only-alice")
        assert await journal_store.principal(BOB).load_event("$only-alice") is None
        assert await journal_store.generation(new_generation="ignored") == generation


class TestAdmittedThreadId:
    """What the journal already knows about an event's place in a thread."""

    async def test_an_unseen_event_is_reported_as_unseen(self, alice: PrincipalStore) -> None:
        """An unseen event is reported as unseen, not as thread-less."""
        assert await alice.admitted_thread_id(room_id=ROOM, event_id="$never") == (False, None)

    async def test_a_room_event_is_admitted_and_in_no_thread(self, alice: PrincipalStore) -> None:
        """These two answers are opposite situations and only one is worth a fetch."""
        await admit(alice, "$room-message")

        assert await alice.admitted_thread_id(room_id=ROOM, event_id="$room-message") == (True, None)

    async def test_a_thread_reply_reports_its_root(self, alice: PrincipalStore) -> None:
        """A thread reply reports its root."""
        await admit(alice, "$reply", thread_id="$root")

        assert await alice.admitted_thread_id(room_id=ROOM, event_id="$reply") == (True, "$root")

    async def test_a_context_only_event_still_answers(self, alice: PrincipalStore) -> None:
        """Settlement clears the replay payload, not the relation the row records."""
        await admit(alice, "$context", thread_id="$root", event_class=EventClass.CONTEXT_ONLY)

        assert await alice.admitted_thread_id(room_id=ROOM, event_id="$context") == (True, "$root")

    async def test_another_room_does_not_answer(self, alice: PrincipalStore) -> None:
        """Another room does not answer."""
        await admit(alice, "$reply", thread_id="$root")

        assert await alice.admitted_thread_id(room_id=OTHER_ROOM, event_id="$reply") == (False, None)


class TestLatestVisibleEvent:
    """The reply target a thread-blind client is pointed at."""

    async def test_an_empty_thread_has_no_latest_event(self, alice: PrincipalStore) -> None:
        """An empty thread has no latest event."""
        assert await alice.latest_visible_event_id(room_id=ROOM, thread_id="$root") is None

    async def test_the_newest_reply_wins(self, alice: PrincipalStore) -> None:
        """The newest reply wins."""
        await admit(alice, "$root", ts=1_000)
        await admit(alice, "$early", ts=2_000, thread_id="$root")
        await admit(alice, "$late", ts=3_000, thread_id="$root")

        assert await alice.latest_visible_event_id(room_id=ROOM, thread_id="$root") == "$late"

    async def test_an_edited_message_answers_with_the_revision_on_screen(
        self,
        alice: PrincipalStore,
    ) -> None:
        """An edit is the event actually in the room, so it is what a reply quotes."""
        await admit(alice, "$root", ts=1_000)
        await admit(alice, "$child", ts=2_000, thread_id="$root")
        await admit(alice, "$child-edit", ts=3_000, thread_id="$root", content=edit("$child", "revised"))

        assert await alice.latest_visible_event_id(room_id=ROOM, thread_id="$root") == "$child-edit"

    async def test_a_redacted_revision_answers_with_its_logical_event(
        self,
        alice: PrincipalStore,
    ) -> None:
        """Quoting a deleted edit renders as nothing; the original is still there."""
        await admit(alice, "$root", ts=1_000)
        await admit(alice, "$child", ts=2_000, thread_id="$root")
        await admit(alice, "$child-edit", ts=3_000, thread_id="$root", content=edit("$child", "revised"))
        await admit(alice, "$redaction", ts=4_000, kind=EventKind.REDACTION, redacts="$child-edit")

        page = await alice.read_conversation(room_id=ROOM, thread_id="$root", limit=10)
        assert [r.logical_event_id for r in page.refresh_pending] == ["$child"]

        assert await alice.latest_visible_event_id(room_id=ROOM, thread_id="$root") == "$child"

    async def test_a_redacted_logical_event_falls_through_to_the_message_behind_it(
        self,
        alice: PrincipalStore,
    ) -> None:
        """Redacting the message itself removes the row, so the previous one answers."""
        await admit(alice, "$root", ts=1_000)
        await admit(alice, "$early", ts=2_000, thread_id="$root")
        await admit(alice, "$late", ts=3_000, thread_id="$root")
        await admit(alice, "$redaction", ts=4_000, kind=EventKind.REDACTION, redacts="$late")

        assert await alice.latest_visible_event_id(room_id=ROOM, thread_id="$root") == "$early"

    async def test_a_root_only_thread_has_no_latest_event(self, alice: PrincipalStore) -> None:
        """The root is stored in the room conversation, so a childless thread is empty.

        The caller falls back to the thread ID, which is the root's own event ID,
        so merging it here would only arrive at the same answer twice.
        """
        await admit(alice, "$root", ts=1_000)
        await admit(alice, "$root-edit", ts=2_000, content=edit("$root", "revised"))

        assert await alice.latest_visible_event_id(room_id=ROOM, thread_id="$root") is None

    async def test_another_thread_in_the_room_does_not_answer(self, alice: PrincipalStore) -> None:
        """Another thread in the room does not answer."""
        await admit(alice, "$other", ts=9_000, thread_id="$other-root")

        assert await alice.latest_visible_event_id(room_id=ROOM, thread_id="$root") is None

    async def test_a_sidecar_message_answers_with_the_revision_on_screen(
        self,
        alice: PrincipalStore,
    ) -> None:
        """A withheld body is not proof of a redaction.

        A message whose text lives in a sidecar is stored exactly like a
        redacted one -- no body, refresh owed -- but its revision is a live
        event nobody deleted, and it is the right thing to quote.
        """
        await admit(alice, "$root", ts=1_000)
        await admit(alice, "$child", ts=2_000, thread_id="$root")
        await admit(
            alice,
            "$child-edit",
            ts=3_000,
            thread_id="$root",
            content=sidecar(edit("$child", "revised")),
        )

        page = await alice.read_conversation(room_id=ROOM, thread_id="$root", limit=10)
        assert [r.logical_event_id for r in page.refresh_pending] == ["$child"]

        assert await alice.latest_visible_event_id(room_id=ROOM, thread_id="$root") == "$child-edit"

    async def test_a_rejoin_stops_the_previous_membership_from_answering(
        self,
        alice: PrincipalStore,
    ) -> None:
        """A rejoin can expose different history, so the old tail cannot be quoted."""
        await admit(alice, "$root", ts=1_000)
        await admit(alice, "$reply", ts=2_000, thread_id="$root")
        assert await alice.latest_visible_event_id(room_id=ROOM, thread_id="$root") == "$reply"

        await alice.fence_departure(ROOM, source=DepartureSource.LOCAL)

        assert await alice.latest_visible_event_id(room_id=ROOM, thread_id="$root") is None


class TestProjectedInteractivePrompts:
    """The Matrix-visible revision is the sole active-prompt authority."""

    @pytest.mark.parametrize(
        ("prompt_event_id", "prompt_ts", "reaction_event_id", "reaction_ts"),
        [
            ("$target", 2_000, "$reaction", 1_000),
            ("$z-target", 2_000, "$a-reaction", 2_000),
        ],
    )
    async def test_reaction_snapshots_the_current_prompt_without_comparing_origin_clocks(
        self,
        alice: PrincipalStore,
        prompt_event_id: str,
        prompt_ts: int,
        reaction_event_id: str,
        reaction_ts: int,
    ) -> None:
        """Admission order is local truth; unrelated Matrix origin clocks are not causal order."""
        await admit(alice, "$turn", sender=BOB, thread_id="$thread", ts=500)
        await admit(
            alice,
            prompt_event_id,
            sender="alice",
            thread_id="$thread",
            ts=prompt_ts,
            content=interactive_prompt("Choose?", "yes", source_event_id="$turn"),
        )

        await admit(
            alice,
            reaction_event_id,
            sender=BOB,
            kind=EventKind.REACTION,
            content=reaction_content(prompt_event_id, "1"),
            thread_id="$thread",
            ts=reaction_ts,
        )

        selection = await alice.claim_interactive_reaction(source_event_id=reaction_event_id)
        assert selection is not None
        assert (selection.question_text, selection.selected_value) == ("Choose?", "yes")

    async def test_delivery_acknowledgement_projects_a_prompt_before_its_echo(
        self,
        alice: PrincipalStore,
    ) -> None:
        """The durable delivery boundary must not wait for a later sync echo."""
        await admit(alice, "$turn", sender=BOB, thread_id="$thread")
        content = interactive_prompt("Choose?", "yes", source_event_id="$turn")
        await alice.enqueue_matrix_delivery(
            delivery_id="$turn",
            stage=DeliveryStage.FINAL,
            room_id=ROOM,
            thread_id="$thread",
            payload=content,
        )
        await alice.claim_matrix_delivery(delivery_id="$turn", stage=DeliveryStage.FINAL)

        acknowledgement = await alice.acknowledge_matrix_delivery(
            delivery_id="$turn",
            stage=DeliveryStage.FINAL,
            event_id="$prompt",
            delivered_projections=(
                projection("$prompt", thread_id="$thread", sender="alice", ts=2_000, content=content),
            ),
        )
        await admit(
            alice,
            "$reaction",
            sender=BOB,
            kind=EventKind.REACTION,
            content=reaction_content("$prompt", "1"),
            thread_id="$thread",
            ts=3_000,
        )

        assert acknowledgement == DeliveryAcknowledgement(settled_event_id="$prompt", bound=True)
        assert await alice.claim_interactive_reaction(source_event_id="$reaction") == InteractiveSelection(
            question_event_id="$prompt",
            question_text="Choose?",
            selection_key="1",
            selected_label="Yes",
            selected_value="yes",
            thread_id="$thread",
        )

    async def test_delivery_acknowledgement_after_departure_does_not_restore_old_projection(
        self,
        alice: PrincipalStore,
        journal_store: EventJournalStore,
    ) -> None:
        """An attempted old-membership delivery may settle without repopulating history."""
        await admit(alice, "$turn", sender=BOB, thread_id="$thread")
        content = text("Old membership answer")
        await alice.enqueue_matrix_delivery(
            delivery_id="$turn",
            stage=DeliveryStage.FINAL,
            room_id=ROOM,
            thread_id="$thread",
            payload=content,
        )
        await alice.claim_matrix_delivery(delivery_id="$turn", stage=DeliveryStage.FINAL)
        await alice.fence_departure(ROOM, source=DepartureSource.LOCAL)

        acknowledgement = await alice.acknowledge_matrix_delivery(
            delivery_id="$turn",
            stage=DeliveryStage.FINAL,
            event_id="$answer",
            delivered_projections=(
                projection("$answer", thread_id="$thread", sender="alice", ts=2_000, content=content),
            ),
            terminal_turn=TerminalTurnWrite(
                agent_name="general",
                index_event_ids=("$turn",),
                anchor_event_id="$turn",
                record_json=json.dumps({"response_event_id": "$answer"}),
            ),
        )
        await alice.note_membership_restarted(ROOM)

        assert acknowledgement == DeliveryAcknowledgement(settled_event_id="$answer", bound=True)
        assert await bodies(alice, thread_id="$thread") == []
        records = await journal_store.turn_records("general").load_all()
        assert [json.loads(record_json)["response_event_id"] for _, _, record_json in records] == ["$answer"]

    async def test_source_less_delivery_keeps_the_membership_epoch_it_was_enqueued_under(
        self,
        alice: PrincipalStore,
    ) -> None:
        """A scheduled or hook-authored delivery cannot adopt a later membership at ACK."""
        content = text("Source-less old answer")
        await alice.enqueue_matrix_delivery(
            delivery_id="scheduled-turn",
            stage=DeliveryStage.FINAL,
            room_id=ROOM,
            thread_id=None,
            payload=content,
        )
        await alice.claim_matrix_delivery(delivery_id="scheduled-turn", stage=DeliveryStage.FINAL)
        await alice.fence_departure(ROOM, source=DepartureSource.LOCAL)
        await alice.note_membership_restarted(ROOM)

        acknowledgement = await alice.acknowledge_matrix_delivery(
            delivery_id="scheduled-turn",
            stage=DeliveryStage.FINAL,
            event_id="$answer",
            delivered_projections=(projection("$answer", sender="alice", ts=2_000, content=content),),
        )

        assert acknowledgement == DeliveryAcknowledgement(settled_event_id="$answer", bound=True)
        assert await bodies(alice) == []

    async def test_delivery_marker_fences_an_old_device_echo_without_a_transaction_id(
        self,
        alice: PrincipalStore,
    ) -> None:
        """The frozen payload identifies a stale echo after Matrix loses device-local proof."""
        await alice.enqueue_matrix_delivery(
            delivery_id="scheduled-turn",
            stage=DeliveryStage.FINAL,
            room_id=ROOM,
            thread_id=None,
            payload=text("scheduled"),
        )
        stored = await alice.load_matrix_delivery(delivery_id="scheduled-turn", stage=DeliveryStage.FINAL)
        assert stored is not None
        await alice.claim_matrix_delivery(delivery_id="scheduled-turn", stage=DeliveryStage.FINAL)
        await alice.fence_departure(ROOM, source=DepartureSource.LOCAL)
        await alice.note_membership_restarted(ROOM)

        await admit(
            alice,
            "$old-device-event",
            sender="alice",
            content=stored.payload,
            event_class=EventClass.CONTEXT_ONLY,
        )

        assert await bodies(alice) == []

    async def test_a_retired_delivery_still_fences_its_late_echo_and_ack(
        self,
        alice: PrincipalStore,
    ) -> None:
        """Retirement stops recovery without deleting the old membership's identity."""
        await alice.enqueue_matrix_delivery(
            delivery_id="scheduled-turn",
            stage=DeliveryStage.FINAL,
            room_id=ROOM,
            thread_id=None,
            payload=text("scheduled"),
        )
        stored = await alice.load_matrix_delivery(delivery_id="scheduled-turn", stage=DeliveryStage.FINAL)
        assert stored is not None
        await alice.claim_matrix_delivery(delivery_id="scheduled-turn", stage=DeliveryStage.FINAL)
        await alice.fence_departure(ROOM, source=DepartureSource.LOCAL)
        await alice.note_membership_restarted(ROOM)

        assert (
            await alice.retire_matrix_delivery(
                delivery_id="scheduled-turn",
                stage=DeliveryStage.FINAL,
                room_id=ROOM,
                membership_epoch=0,
            )
            is None
        )
        retired = await alice.load_matrix_delivery(delivery_id="scheduled-turn", stage=DeliveryStage.FINAL)
        assert retired is not None
        assert retired.retired

        await admit(
            alice,
            "$late-echo",
            sender="alice",
            content=stored.payload,
            event_class=EventClass.CONTEXT_ONLY,
        )
        acknowledgement = await alice.acknowledge_matrix_delivery(
            delivery_id="scheduled-turn",
            stage=DeliveryStage.FINAL,
            event_id="$late-echo",
            delivered_projections=(projection("$late-echo", sender="alice", ts=2_000, content=stored.payload),),
        )

        assert acknowledgement == DeliveryAcknowledgement(settled_event_id="$late-echo", bound=True)
        assert await bodies(alice) == []

    async def test_retiring_an_edit_removes_an_echo_that_won_the_race(
        self,
        alice: PrincipalStore,
    ) -> None:
        """Retirement reconciles an edit projected after its absence scan."""
        await admit(alice, "$target", sender="alice", content=text("Thinking..."), ts=1_000)
        await alice.enqueue_matrix_delivery(
            delivery_id="$turn",
            stage=DeliveryStage.FINAL,
            room_id=ROOM,
            thread_id=None,
            payload=edit("$target", "old answer"),
            edits_event_id="$target",
        )
        stored = await alice.load_matrix_delivery(delivery_id="$turn", stage=DeliveryStage.FINAL)
        assert stored is not None
        await alice.claim_matrix_delivery(delivery_id="$turn", stage=DeliveryStage.FINAL)

        await admit(
            alice,
            "$physical-edit",
            sender="alice",
            content=stored.payload,
            event_class=EventClass.CONTEXT_ONLY,
            ts=2_000,
        )
        assert await bodies(alice) == ["old answer"]

        assert (
            await alice.retire_matrix_delivery(
                delivery_id="$turn",
                stage=DeliveryStage.FINAL,
                room_id=ROOM,
                membership_epoch=0,
            )
            is None
        )
        assert await bodies(alice) == []

    async def test_delivery_marker_does_not_claim_another_matrix_senders_copy(
        self,
        alice: PrincipalStore,
    ) -> None:
        """Only this principal's Matrix sender can assert its delivery identity."""
        await alice.enqueue_matrix_delivery(
            delivery_id="scheduled-turn",
            stage=DeliveryStage.FINAL,
            room_id=ROOM,
            thread_id=None,
            payload=text("copied answer"),
        )
        stored = await alice.load_matrix_delivery(delivery_id="scheduled-turn", stage=DeliveryStage.FINAL)
        assert stored is not None
        await alice.claim_matrix_delivery(delivery_id="scheduled-turn", stage=DeliveryStage.FINAL)
        await alice.fence_departure(ROOM, source=DepartureSource.LOCAL)
        await alice.note_membership_restarted(ROOM)

        await admit(
            alice,
            "$foreign-copy",
            sender=BOB,
            content=stored.payload,
            event_class=EventClass.CONTEXT_ONLY,
        )

        assert await bodies(alice) == ["copied answer"]

    async def test_delivery_marker_does_not_claim_a_same_sender_copy_in_another_room(
        self,
        alice: PrincipalStore,
    ) -> None:
        """A room-local delivery owner says nothing about the bot's event elsewhere."""
        await alice.enqueue_matrix_delivery(
            delivery_id="scheduled-turn",
            stage=DeliveryStage.FINAL,
            room_id=ROOM,
            thread_id=None,
            payload=text("copied answer"),
        )
        stored = await alice.load_matrix_delivery(delivery_id="scheduled-turn", stage=DeliveryStage.FINAL)
        assert stored is not None

        await admit(
            alice,
            "$other-room-copy",
            room_id=OTHER_ROOM,
            sender="alice",
            content=stored.payload,
            event_class=EventClass.CONTEXT_ONLY,
        )

        page = await alice.read_conversation(room_id=OTHER_ROOM, thread_id=None, limit=50)
        assert [message.content["body"] for message in page.messages] == ["copied answer"]

    @pytest.mark.parametrize("echo_before_ack", [False, True])
    async def test_stale_delivery_echo_cannot_restore_a_rejoined_membership(
        self,
        alice: PrincipalStore,
        *,
        echo_before_ack: bool,
    ) -> None:
        """ACK and sync-echo ordering cannot revive an old-membership prompt."""
        await admit(alice, "$turn", sender=BOB, thread_id="$thread")
        content = interactive_prompt("Old?", "old", source_event_id="$turn")
        assert (
            await alice.enqueue_matrix_delivery(
                delivery_id="$turn",
                stage=DeliveryStage.FINAL,
                room_id=ROOM,
                thread_id="$thread",
                payload=content,
            )
            is not None
        )
        stored = await alice.claim_matrix_delivery(delivery_id="$turn", stage=DeliveryStage.FINAL)
        assert stored is not None
        await alice.fence_departure(ROOM, source=DepartureSource.LOCAL)
        await alice.note_membership_restarted(ROOM)

        async def admit_echo() -> None:
            inbound, projected = message(
                "$prompt",
                sender="alice",
                thread_id="$thread",
                ts=2_000,
                content=stored.payload,
                event_class=EventClass.CONTEXT_ONLY,
            )
            await alice.admit(inbound, replace(projected, transaction_id=stored.transaction_id))

        if echo_before_ack:
            await admit_echo()
            assert await bodies(alice, thread_id="$thread") == []
        acknowledgement = await alice.acknowledge_matrix_delivery(
            delivery_id="$turn",
            stage=DeliveryStage.FINAL,
            event_id="$prompt",
            delivered_projections=(),
        )
        if not echo_before_ack:
            await admit_echo()

        assert acknowledgement == DeliveryAcknowledgement(settled_event_id="$prompt", bound=True)
        assert await bodies(alice, thread_id="$thread") == []

    async def test_interactive_source_waits_for_an_attempted_delivery_to_be_projected(
        self,
        alice: PrincipalStore,
    ) -> None:
        """A visible-but-unacknowledged edit can still change the active prompt."""
        await admit(alice, "$turn", sender=BOB, thread_id="$thread")
        await alice.enqueue_matrix_delivery(
            delivery_id="$turn",
            stage=DeliveryStage.FINAL,
            room_id=ROOM,
            thread_id="$thread",
            payload=interactive_prompt("Choose?", "yes", source_event_id="$turn"),
        )
        await alice.claim_matrix_delivery(delivery_id="$turn", stage=DeliveryStage.FINAL)
        reaction, _projected = message(
            "$reaction",
            sender=BOB,
            kind=EventKind.REACTION,
            content=reaction_content("$prompt", "1"),
            thread_id="$thread",
            ts=3_000,
        )

        with pytest.raises(RuntimeError, match="delivery projection is pending"):
            await alice.admit(reaction)

        assert await alice.load_event("$reaction") is None

    async def test_old_prompt_delivery_does_not_park_a_current_reaction(
        self,
        alice: PrincipalStore,
    ) -> None:
        """Only unprojected prompts from the source's membership can delay its claim."""
        await admit(alice, "$old-turn", sender=BOB, thread_id="$thread")
        assert (
            await alice.enqueue_matrix_delivery(
                delivery_id="$old-turn",
                stage=DeliveryStage.FINAL,
                room_id=ROOM,
                thread_id="$thread",
                payload=interactive_prompt("Old?", "old", source_event_id="$old-turn"),
            )
            is not None
        )
        assert (
            await alice.claim_matrix_delivery(
                delivery_id="$old-turn",
                stage=DeliveryStage.FINAL,
            )
            is not None
        )
        await alice.fence_departure(ROOM, source=DepartureSource.LOCAL)
        await alice.note_membership_restarted(ROOM)

        await admit(alice, "$current-turn", sender=BOB, thread_id="$thread")
        await admit(
            alice,
            "$current-prompt",
            sender="alice",
            thread_id="$thread",
            content=interactive_prompt("Current?", "current", source_event_id="$current-turn"),
        )
        await admit(
            alice,
            "$reaction",
            sender=BOB,
            kind=EventKind.REACTION,
            content=reaction_content("$current-prompt", "1"),
            thread_id="$thread",
        )

        selection = await alice.claim_interactive_reaction(source_event_id="$reaction")
        assert selection is not None
        assert (selection.question_text, selection.selected_value) == ("Current?", "current")

    async def test_approval_edit_debt_does_not_block_interactive_source_admission(
        self,
        alice: PrincipalStore,
    ) -> None:
        """Only response messages can owe the interactive-prompt projection gate."""
        await admit(alice, "$turn", sender=BOB, thread_id="$thread")
        await alice.enqueue_matrix_delivery(
            delivery_id="approval-card-1",
            stage=DeliveryStage.FINAL,
            event_type="io.mindroom.tool_approval",
            room_id=ROOM,
            thread_id="$thread",
            payload={"approval_id": "approval-card-1", "status": "approved"},
            edits_event_id="$approval",
        )
        await alice.claim_matrix_delivery(delivery_id="approval-card-1", stage=DeliveryStage.FINAL)
        reaction, _projected = message(
            "$reaction",
            sender=BOB,
            kind=EventKind.REACTION,
            content=reaction_content("$prompt", "1"),
            thread_id="$thread",
            ts=3_000,
        )

        assert await alice.admit(reaction) is AdmissionResult.ADMITTED
        assert await alice.load_event("$reaction") is not None

    async def test_edit_acknowledgement_projects_a_missing_target_before_its_prompt(
        self,
        alice: PrincipalStore,
    ) -> None:
        """Target and edit share the ACK transaction when sync missed both echoes."""
        await admit(alice, "$turn", sender=BOB, thread_id="$thread")
        edit_content = interactive_edit("$target", "Choose?", "yes", source_event_id="$turn")
        await alice.enqueue_matrix_delivery(
            delivery_id="$turn",
            stage=DeliveryStage.FINAL,
            room_id=ROOM,
            thread_id="$thread",
            payload=edit_content,
            edits_event_id="$target",
        )
        await alice.claim_matrix_delivery(delivery_id="$turn", stage=DeliveryStage.FINAL)

        await alice.acknowledge_matrix_delivery(
            delivery_id="$turn",
            stage=DeliveryStage.FINAL,
            event_id="$edit",
            delivered_projections=(
                projection("$target", thread_id="$thread", sender="alice", ts=2_000, content=text("Thinking...")),
                projection("$edit", thread_id="$thread", sender="alice", ts=3_000, content=edit_content),
            ),
        )
        await admit(
            alice,
            "$reaction",
            sender=BOB,
            kind=EventKind.REACTION,
            content=reaction_content("$target", "1"),
            thread_id="$thread",
            ts=4_000,
        )

        selection = await alice.claim_interactive_reaction(source_event_id="$reaction")
        assert selection is not None
        assert (selection.question_event_id, selection.question_text, selection.selected_value) == (
            "$target",
            "Choose?",
            "yes",
        )

    async def test_admitting_a_self_authored_prompt_activates_it_before_its_reaction(
        self,
        alice: PrincipalStore,
    ) -> None:
        """Removing admission reconciliation would leave the reaction unclaimed."""
        await admit(alice, "$turn", sender=BOB, thread_id="$thread")
        await admit(
            alice,
            "$target",
            sender="alice",
            thread_id="$thread",
            content=interactive_prompt("Old?", "old", source_event_id="$turn"),
            event_class=EventClass.CONTEXT_ONLY,
        )
        await admit(
            alice,
            "$reaction",
            sender=BOB,
            kind=EventKind.REACTION,
            content=reaction_content("$target", "1"),
            ts=2_000,
        )

        selection = await alice.claim_interactive_reaction(
            source_event_id="$reaction",
        )

        assert selection == InteractiveSelection(
            question_event_id="$target",
            question_text="Old?",
            selection_key="1",
            selected_label="Old",
            selected_value="old",
            thread_id="$thread",
        )

    async def test_admission_rejects_an_option_the_visible_prompt_does_not_offer(
        self,
        alice: PrincipalStore,
        journal_store: EventJournalStore,
    ) -> None:
        """An invalid reaction never creates the source snapshot that claim trusts."""
        await admit(alice, "$turn", sender=BOB, thread_id="$thread")
        await admit(
            alice,
            "$target",
            sender="alice",
            thread_id="$thread",
            content=interactive_prompt("Choose?", "valid", source_event_id="$turn"),
            event_class=EventClass.CONTEXT_ONLY,
        )
        await admit(
            alice,
            "$reaction",
            sender=BOB,
            kind=EventKind.REACTION,
            content=reaction_content("$target", "missing"),
            ts=2_000,
        )

        assert await alice.claim_interactive_reaction(source_event_id="$reaction") is None
        assert await _interactive_selection_rows(journal_store) == []
        assert [row["question_event_id"] for row in await _interactive_question_rows(journal_store)] == ["$target"]

    async def test_a_sidecar_prompt_activates_from_the_revision_admission_installed(
        self,
        alice: PrincipalStore,
    ) -> None:
        """The projection withholds an unresolved preview body but still owns its prompt metadata."""
        await admit(alice, "$turn", sender=BOB, thread_id="$thread")
        content = interactive_prompt("Large?", "large", source_event_id="$turn")
        content["io.mindroom.long_text"] = {
            "version": 2,
            "encoding": "matrix_event_content_json",
        }
        content["url"] = "mxc://example.org/body"
        await admit(
            alice,
            "$target",
            sender="alice",
            thread_id="$thread",
            content=content,
        )
        assert await alice.interactive_prompt_is_current(
            room_id=ROOM,
            question_event_id="$target",
            expected=InteractivePrompt(
                creator_agent="agent",
                question_text="Large?",
                options={"1": "large"},
                option_labels={"1": "Large"},
                source_event_id="$turn",
            ),
        )
        await admit(
            alice,
            "$reaction",
            sender=BOB,
            kind=EventKind.REACTION,
            content=reaction_content("$target", "1"),
            ts=2_000,
        )

        selection = await alice.claim_interactive_reaction(
            source_event_id="$reaction",
        )

        assert selection is not None
        assert (selection.question_text, selection.selected_value) == ("Large?", "large")

    async def test_losing_edit_cannot_reactivate_its_prompt(
        self,
        alice: PrincipalStore,
    ) -> None:
        """HTTP completion order cannot override the projection's revision order."""
        await admit(alice, "$turn", sender=BOB, thread_id="$thread")
        await admit(
            alice,
            "$target",
            sender="alice",
            thread_id="$thread",
            content=interactive_prompt("Original?", "original", source_event_id="$turn"),
        )
        await admit(
            alice,
            "$newer",
            sender="alice",
            ts=3_000,
            content=interactive_edit("$target", "New?", "new", source_event_id="$turn"),
        )
        await admit(
            alice,
            "$older",
            sender="alice",
            ts=2_000,
            content=interactive_edit("$target", "Stale?", "stale", source_event_id="$turn"),
        )
        await admit(
            alice,
            "$reaction",
            sender=BOB,
            kind=EventKind.REACTION,
            content=reaction_content("$target", "1"),
            ts=4_000,
        )

        selection = await alice.claim_interactive_reaction(
            source_event_id="$reaction",
        )

        assert selection is not None
        assert (selection.question_text, selection.selected_value) == ("New?", "new")

    async def test_out_of_order_edit_activates_when_its_target_arrives(
        self,
        alice: PrincipalStore,
    ) -> None:
        """Reconciling the incoming original instead of the visible row loses the held edit."""
        await admit(alice, "$turn", sender=BOB, thread_id="$thread")
        await admit(
            alice,
            "$edit",
            sender="alice",
            ts=2_000,
            content=interactive_edit("$target", "Held?", "held", source_event_id="$turn"),
        )
        await admit(
            alice,
            "$target",
            sender="alice",
            thread_id="$thread",
            content=interactive_prompt("Original?", "original", source_event_id="$turn"),
        )
        await admit(
            alice,
            "$reaction",
            sender=BOB,
            kind=EventKind.REACTION,
            content=reaction_content("$target", "1"),
            ts=3_000,
        )

        selection = await alice.claim_interactive_reaction(
            source_event_id="$reaction",
        )

        assert selection is not None
        assert (selection.question_text, selection.selected_value) == ("Held?", "held")

    async def test_a_held_sidecar_edit_activates_when_its_target_arrives(
        self,
        alice: PrincipalStore,
    ) -> None:
        """A winning held edit carries prompt metadata even while its preview body stays unreadable."""
        await admit(alice, "$turn", sender=BOB, thread_id="$thread")
        await admit(
            alice,
            "$edit",
            sender="alice",
            ts=2_000,
            content=sidecar(interactive_edit("$target", "Held large?", "held", source_event_id="$turn")),
        )
        await admit(
            alice,
            "$target",
            sender="alice",
            thread_id="$thread",
            content=interactive_prompt("Original?", "original", source_event_id="$turn"),
        )
        await admit(
            alice,
            "$reaction",
            sender=BOB,
            kind=EventKind.REACTION,
            content=reaction_content("$target", "1"),
            ts=3_000,
        )

        selection = await alice.claim_interactive_reaction(
            source_event_id="$reaction",
        )

        assert selection is not None
        assert (selection.question_text, selection.selected_value) == ("Held large?", "held")

    async def test_plain_edit_clears_the_active_prompt(
        self,
        alice: PrincipalStore,
        journal_store: EventJournalStore,
    ) -> None:
        """Leaving the old row active would let numeric text answer invisible options."""
        await admit(alice, "$turn", sender=BOB, thread_id="$thread")
        await admit(
            alice,
            "$target",
            sender="alice",
            thread_id="$thread",
            content=interactive_prompt("Choose?", "yes", source_event_id="$turn"),
        )

        await admit(alice, "$plain-edit", sender="alice", ts=2_000, content=edit("$target", "No question"))

        assert await _interactive_question_rows(journal_store) == []

    async def test_redacting_a_prompt_revision_erases_its_pending_selection(
        self,
        alice: PrincipalStore,
        journal_store: EventJournalStore,
    ) -> None:
        """A deleted prompt cannot survive in a source snapshot waiting to run."""
        await admit(alice, "$turn", sender=BOB)
        await admit(alice, "$target", sender="alice", content=text("Plain"))
        await admit(
            alice,
            "$edit",
            sender="alice",
            ts=2_000,
            content=interactive_edit("$target", "Secret?", "secret", source_event_id="$turn"),
        )
        await admit(
            alice,
            "$reaction",
            sender=BOB,
            kind=EventKind.REACTION,
            content=reaction_content("$target", "1"),
            ts=2_500,
        )
        assert await _interactive_selection_rows(journal_store)

        await admit(
            alice,
            "$redaction",
            sender=BOB,
            ts=3_000,
            kind=EventKind.REDACTION,
            redacts="$edit",
        )

        assert await alice.claim_interactive_reaction(source_event_id="$reaction") is None
        rows = await journal_store.backend.read(
            lambda transaction: transaction.fetchall(
                "SELECT question_json FROM interactive_questions WHERE revision_event_id = ?",
                ("$edit",),
            ),
        )
        assert rows == ()

    async def test_another_sender_cannot_forge_a_prompt_for_this_principal(
        self,
        alice: PrincipalStore,
        journal_store: EventJournalStore,
    ) -> None:
        """Trusting namespaced content without its Matrix sender would create user-owned prompts."""
        await admit(alice, "$turn", sender=BOB)
        await admit(
            alice,
            "$forged",
            sender=BOB,
            content=interactive_prompt("Forged?", "forged", source_event_id="$turn"),
        )

        assert await _interactive_question_rows(journal_store) == []

    async def test_old_source_proof_cannot_borrow_a_current_membership(
        self,
        alice: PrincipalStore,
        journal_store: EventJournalStore,
    ) -> None:
        """A stale turn cannot authorize a prompt after the room membership changes."""
        await admit(alice, "$old-turn", sender=BOB)
        await alice.fence_departure(ROOM, source=DepartureSource.LOCAL)
        await alice.note_membership_restarted(ROOM)

        await admit(
            alice,
            "$stale-question",
            sender="alice",
            content=interactive_prompt(
                "Stale?",
                "stale",
                source_event_id="$old-turn",
            ),
        )

        assert await _interactive_question_rows(journal_store) == []

    async def test_button_check_requires_the_same_visible_prompt(
        self,
        alice: PrincipalStore,
    ) -> None:
        """Post-effects cannot put regenerated buttons on a different visible event."""
        await admit(alice, "$turn", sender=BOB)
        expected = InteractivePrompt(
            creator_agent="agent",
            question_text="Choose?",
            options={"1": "yes"},
            option_labels={"1": "Yes"},
            source_event_id="$turn",
        )
        await admit(
            alice,
            "$question",
            sender="alice",
            ts=1_000,
            content=interactive_prompt("Choose?", "yes", source_event_id="$turn"),
        )
        assert await alice.interactive_prompt_is_current(
            room_id=ROOM,
            question_event_id="$question",
            expected=expected,
        )

        await admit(
            alice,
            "$plain-edit",
            sender="alice",
            ts=2_000,
            content=edit("$question", "Plain replacement"),
        )

        assert not await alice.interactive_prompt_is_current(
            room_id=ROOM,
            question_event_id="$question",
            expected=expected,
        )

    async def test_button_check_rejects_a_fenced_prompt(
        self,
        alice: PrincipalStore,
    ) -> None:
        """Post-transport button delivery must obey the same fence as projection."""
        await admit(alice, "$turn", sender=BOB)
        expected = InteractivePrompt(
            creator_agent="agent",
            question_text="Choose?",
            options={"1": "yes"},
            option_labels={"1": "Yes"},
            source_event_id="$turn",
        )
        await admit(
            alice,
            "$question",
            sender="alice",
            content=interactive_prompt("Choose?", "yes", source_event_id="$turn"),
        )
        await alice.fence_departure(ROOM, source=DepartureSource.LOCAL)

        assert not await alice.interactive_prompt_is_current(
            room_id=ROOM,
            question_event_id="$question",
            expected=expected,
        )

    @pytest.mark.parametrize("installer", ["hydration", "recovery"])
    async def test_history_install_activates_the_projected_prompt(
        self,
        alice: PrincipalStore,
        installer: str,
    ) -> None:
        """Every projection entry point must install the same prompt revision."""
        await admit(alice, "$turn", sender=BOB, thread_id="$thread")
        epoch = await alice.membership_epoch(ROOM)
        prompt = message(
            "$historical-question",
            sender="alice",
            thread_id="$thread",
            content=interactive_prompt("Historical?", "yes", source_event_id="$turn"),
        )[1]

        if installer == "hydration":
            assert await alice.install_hydrated_conversation(
                room_id=ROOM,
                thread_id="$thread",
                events=(prompt,),
                complete=True,
                expected_membership_epoch=epoch,
            )
        else:
            recovery = await alice.record_room_history_recovery(ROOM)
            assert recovery is not None
            assert await alice.install_room_history_recovery_chunk(
                recovery,
                events=(prompt,),
                expected_membership_epoch=epoch,
            )

        await admit(
            alice,
            "$reaction",
            sender=BOB,
            kind=EventKind.REACTION,
            content=reaction_content("$historical-question", "1"),
            ts=2_000,
        )

        selection = await alice.claim_interactive_reaction(
            source_event_id="$reaction",
        )

        assert selection is not None
        assert (selection.question_text, selection.selected_value) == ("Historical?", "yes")

    async def test_refetch_restores_an_unconsumed_visible_prompt(
        self,
        alice: PrincipalStore,
    ) -> None:
        """Returning to an older visible prompt revision makes that revision active again."""
        await admit(alice, "$turn", sender=BOB)
        original = interactive_prompt("Original?", "original", source_event_id="$turn")
        await admit(alice, "$target", sender="alice", content=original)
        await admit(alice, "$plain", sender="alice", ts=2_000, content=edit("$target", "Plain"))
        await admit(alice, "$redaction", ts=3_000, kind=EventKind.REDACTION, redacts="$plain")
        request = (await refreshes(alice))[0]

        assert await alice.install_refetched_revision(
            request,
            revision_event_id="$target",
            revision_ts=1_000,
            revision_sender="alice",
            content=original,
        )
        await admit(
            alice,
            "$reaction",
            sender=BOB,
            kind=EventKind.REACTION,
            content=reaction_content("$target", "1"),
            ts=4_000,
        )

        selection = await alice.claim_interactive_reaction(
            source_event_id="$reaction",
        )

        assert selection is not None
        assert selection.selected_value == "original"

    async def test_refetch_cannot_resurrect_a_consumed_prompt_revision(
        self,
        alice: PrincipalStore,
    ) -> None:
        """Consumption is durable even when projection repair shows the old revision again."""
        await admit(alice, "$turn", sender=BOB)
        original = interactive_prompt("Original?", "original", source_event_id="$turn")
        await admit(alice, "$target", sender="alice", content=original)
        await admit(
            alice,
            "$first-reaction",
            sender=BOB,
            kind=EventKind.REACTION,
            content=reaction_content("$target", "1"),
            ts=1_500,
        )
        assert await alice.claim_interactive_reaction(
            source_event_id="$first-reaction",
        )
        await alice.settle("$first-reaction")

        await admit(alice, "$plain", sender="alice", ts=2_000, content=edit("$target", "Plain"))
        await admit(alice, "$redaction", ts=3_000, kind=EventKind.REDACTION, redacts="$plain")
        request = (await refreshes(alice))[0]
        assert await alice.install_refetched_revision(
            request,
            revision_event_id="$target",
            revision_ts=1_000,
            revision_sender="alice",
            content=original,
        )
        await admit(
            alice,
            "$second-reaction",
            sender=BOB,
            kind=EventKind.REACTION,
            content=reaction_content("$target", "1"),
            ts=4_000,
        )

        assert (
            await alice.claim_interactive_reaction(
                source_event_id="$second-reaction",
            )
            is None
        )

    @pytest.mark.parametrize("ack_before_refetch", [False, True])
    async def test_refetch_discards_a_delivery_from_an_older_membership(
        self,
        alice: PrincipalStore,
        *,
        ack_before_refetch: bool,
    ) -> None:
        """Server fallback cannot restore an edit owned by an old membership."""
        await admit(alice, "$turn", sender=BOB)
        old_edit = edit("$target", "Old membership")
        assert (
            await alice.enqueue_matrix_delivery(
                delivery_id="$turn",
                stage=DeliveryStage.FINAL,
                room_id=ROOM,
                thread_id=None,
                payload=old_edit,
                edits_event_id="$target",
            )
            is not None
        )
        stored = await alice.claim_matrix_delivery(delivery_id="$turn", stage=DeliveryStage.FINAL)
        assert stored is not None
        await alice.fence_departure(ROOM, source=DepartureSource.LOCAL)
        await alice.note_membership_restarted(ROOM)

        await admit(alice, "$target", sender="alice", content=text("Current membership"))
        await admit(alice, "$new-edit", sender="alice", ts=3_000, content=edit("$target", "New edit"))
        epoch = await alice.membership_epoch(ROOM)
        assert await alice.install_hydrated_conversation(
            room_id=ROOM,
            thread_id=None,
            events=(),
            complete=True,
            expected_membership_epoch=epoch,
        )
        assert await alice.conversation_is_hydrated(room_id=ROOM, thread_id=None)
        if ack_before_refetch:
            await alice.acknowledge_matrix_delivery(
                delivery_id="$turn",
                stage=DeliveryStage.FINAL,
                event_id="$old-edit",
                delivered_projections=(),
            )
        await admit(alice, "$redaction", ts=4_000, kind=EventKind.REDACTION, redacts="$new-edit")
        request = (await refreshes(alice))[0]

        installed = await alice.install_refetched_revision(
            request,
            revision_event_id="$old-edit",
            revision_ts=2_000,
            revision_sender="alice",
            revision_transaction_id=stored.transaction_id,
            content=text("Old membership"),
        )

        assert installed
        page = await alice.read_conversation(room_id=ROOM, thread_id=None, limit=50)
        assert page.messages == ()
        assert page.refresh_pending == ()
        assert not await alice.conversation_is_hydrated(room_id=ROOM, thread_id=None)

    async def test_marker_fences_a_refetched_edit_without_a_device_transaction(
        self,
        alice: PrincipalStore,
    ) -> None:
        """The stable marker survives reduction to an edit's replacement content."""
        await admit(alice, "$turn", sender=BOB)
        old_edit = edit("$target", "Old membership")
        assert (
            await alice.enqueue_matrix_delivery(
                delivery_id="$turn",
                stage=DeliveryStage.FINAL,
                room_id=ROOM,
                thread_id=None,
                payload=old_edit,
                edits_event_id="$target",
            )
            is not None
        )
        stored = await alice.claim_matrix_delivery(delivery_id="$turn", stage=DeliveryStage.FINAL)
        assert stored is not None
        replacement = cast("dict[str, object]", stored.payload["m.new_content"])
        await alice.fence_departure(ROOM, source=DepartureSource.LOCAL)
        await alice.note_membership_restarted(ROOM)

        await admit(alice, "$target", sender="alice", content=text("Current membership"))
        await admit(alice, "$new-edit", sender="alice", ts=3_000, content=edit("$target", "New edit"))
        await admit(alice, "$redaction", ts=4_000, kind=EventKind.REDACTION, redacts="$new-edit")
        request = (await refreshes(alice))[0]

        installed = await alice.install_refetched_revision(
            request,
            revision_event_id="$old-edit",
            revision_ts=2_000,
            revision_sender="alice",
            content=replacement,
        )

        assert installed
        page = await alice.read_conversation(room_id=ROOM, thread_id=None, limit=50)
        assert page.messages == ()
        assert page.refresh_pending == ()

    async def test_stale_ack_invalidates_hydration_after_discarding_an_edit(
        self,
        alice: PrincipalStore,
    ) -> None:
        """Deleting a stale revision also revokes its reconstruction proof."""
        await admit(alice, "$turn", sender=BOB)
        old_edit = edit("$target", "Old membership")
        assert (
            await alice.enqueue_matrix_delivery(
                delivery_id="$turn",
                stage=DeliveryStage.FINAL,
                room_id=ROOM,
                thread_id=None,
                payload=old_edit,
                edits_event_id="$target",
            )
            is not None
        )
        assert await alice.claim_matrix_delivery(delivery_id="$turn", stage=DeliveryStage.FINAL) is not None
        await alice.fence_departure(ROOM, source=DepartureSource.LOCAL)
        await alice.note_membership_restarted(ROOM)
        epoch = await alice.membership_epoch(ROOM)
        assert await alice.install_hydrated_conversation(
            room_id=ROOM,
            thread_id=None,
            events=(
                projection("$target", sender="alice", content=text("Base message")),
                projection("$old-edit", sender="alice", ts=2_000, content=old_edit),
            ),
            complete=True,
            expected_membership_epoch=epoch,
        )
        assert await bodies(alice) == ["Old membership"]
        assert await alice.conversation_is_hydrated(room_id=ROOM, thread_id=None)

        await alice.acknowledge_matrix_delivery(
            delivery_id="$turn",
            stage=DeliveryStage.FINAL,
            event_id="$old-edit",
            delivered_projections=(),
        )

        assert await bodies(alice) == []
        assert not await alice.conversation_is_hydrated(room_id=ROOM, thread_id=None)

    async def test_refetch_and_stale_ack_serialize_before_reinstalling_a_revision(
        self,
        rival_stores: RivalStores,
    ) -> None:
        """A refetch cannot publish after an ACK has already discarded its owner."""
        principal_id = "agent@alice"
        refetch_store = rival_stores.first.principal(principal_id)
        ack_store = rival_stores.second.principal(principal_id)
        await admit(refetch_store, "$turn", sender=BOB)
        old_edit = edit("$target", "Old membership")
        assert (
            await refetch_store.enqueue_matrix_delivery(
                delivery_id="$turn",
                stage=DeliveryStage.FINAL,
                room_id=ROOM,
                thread_id=None,
                payload=old_edit,
                edits_event_id="$target",
            )
            is not None
        )
        assert (
            await refetch_store.claim_matrix_delivery(
                delivery_id="$turn",
                stage=DeliveryStage.FINAL,
            )
            is not None
        )
        await refetch_store.fence_departure(ROOM, source=DepartureSource.LOCAL)
        await refetch_store.note_membership_restarted(ROOM)
        await admit(refetch_store, "$target", sender="alice", content=text("Current membership"))
        await admit(refetch_store, "$new-edit", sender="alice", ts=3_000, content=edit("$target", "New edit"))
        await admit(refetch_store, "$redaction", ts=4_000, kind=EventKind.REDACTION, redacts="$new-edit")
        request = (await refreshes(refetch_store))[0]

        ownership_claimed = threading.Event()
        release_refetch = threading.Event()

        def pause_after_membership_claim() -> None:
            ownership_claimed.set()
            assert release_refetch.wait(_WORKER_WAIT_SECONDS), "the refetch transaction was never released"

        racing_refetch = EventJournalStore(
            backend=_PausingBackend(
                rival_stores.first.backend,
                pause_after_membership_claim,
                statement_matches=lambda sql: "INSERT INTO room_membership" in sql,
            ),
        ).principal(principal_id)
        refetch = asyncio.create_task(
            racing_refetch.install_refetched_revision(
                request,
                revision_event_id="$old-edit",
                revision_ts=2_000,
                revision_sender="alice",
                content=text("Old membership"),
            ),
        )
        await asyncio.to_thread(ownership_claimed.wait, _WORKER_WAIT_SECONDS)
        assert ownership_claimed.is_set(), "the refetch never claimed the membership row"

        acknowledgement = asyncio.create_task(
            ack_store.acknowledge_matrix_delivery(
                delivery_id="$turn",
                stage=DeliveryStage.FINAL,
                event_id="$old-edit",
                delivered_projections=(),
            ),
        )
        queued = asyncio.create_task(
            _await_queued_racers(
                rival_stores.database_url,
                application_name=rival_stores.racer_application_name,
                expected=1,
            ),
        )
        done, _pending = await asyncio.wait((acknowledgement, queued), return_when=asyncio.FIRST_COMPLETED)
        acknowledgement_waited = queued in done and queued.exception() is None
        release_refetch.set()
        if not queued.done():
            queued.cancel()
            with suppress(asyncio.CancelledError):
                await queued
        installed, acknowledged = await asyncio.gather(refetch, acknowledgement)

        assert acknowledgement_waited, "acknowledgement bypassed the refetch membership claim"
        assert installed
        assert acknowledged == DeliveryAcknowledgement(settled_event_id="$old-edit", bound=True)
        page = await refetch_store.read_conversation(room_id=ROOM, thread_id=None, limit=50)
        assert page.messages == ()
        assert page.refresh_pending == ()

    async def test_dropping_a_refetched_message_removes_its_prompt_from_discovery(
        self,
        alice: PrincipalStore,
    ) -> None:
        """A sidecar message absent from the server cannot keep accepting answers."""
        await admit(alice, "$turn", sender=BOB)
        content = interactive_prompt("Large?", "large", source_event_id="$turn")
        content["io.mindroom.long_text"] = {
            "version": 2,
            "encoding": "matrix_event_content_json",
        }
        content["url"] = "mxc://example.org/body"
        await admit(alice, "$target", sender="alice", content=content)
        request = (await refreshes(alice))[0]

        assert await alice.drop_refetched_message(request)
        await admit(
            alice,
            "$reaction",
            sender=BOB,
            kind=EventKind.REACTION,
            content=reaction_content("$target", "1"),
            ts=2_000,
        )

        assert (
            await alice.claim_interactive_reaction(
                source_event_id="$reaction",
            )
            is None
        )


class TestInteractiveQuestionClaims:
    """A journal source owns an immutable replayable selection."""

    async def test_a_replacement_question_does_not_overwrite_its_source_owned_selection(
        self,
        alice: PrincipalStore,
    ) -> None:
        """A visible replacement and the older source selection have independent owners."""
        await admit(alice, "$turn")
        await _activate_interactive_question(alice, "$question")
        await admit(
            alice,
            "$reaction",
            kind=EventKind.REACTION,
            content=reaction_content("$question", "1"),
            ts=2_500,
        )
        original = InteractiveSelection(
            question_event_id="$question",
            question_text="Choose",
            selection_key="1",
            selected_label="One",
            selected_value="one",
            thread_id="$thread",
        )
        assert (
            await alice.claim_interactive_reaction(
                source_event_id="$reaction",
            )
            == original
        )

        await _activate_interactive_question(
            alice,
            "$question",
            revision_event_id="$question-edit",
            question_text="Choose again",
            options={"2": "two"},
            option_labels={"2": "Two"},
        )
        assert (
            await alice.claim_interactive_reaction(
                source_event_id="$reaction",
            )
            == original
        )

        await alice.settle("$reaction")
        await admit(
            alice,
            "$replacement-reaction",
            kind=EventKind.REACTION,
            content=reaction_content("$question", "2"),
            ts=3_500,
        )
        selected_replacement = await alice.claim_interactive_reaction(
            source_event_id="$replacement-reaction",
        )

        assert selected_replacement == InteractiveSelection(
            question_event_id="$question",
            question_text="Choose again",
            selection_key="2",
            selected_label="Two",
            selected_value="two",
            thread_id="$thread",
        )

    async def test_reaction_admission_preserves_the_prompt_seen_before_an_edit(
        self,
        alice: PrincipalStore,
    ) -> None:
        """A queued reaction cannot be reinterpreted through a later prompt revision."""
        await admit(alice, "$turn")
        await _activate_interactive_question(alice, "$question")
        reaction, _ = message(
            "$reaction",
            kind=EventKind.REACTION,
            ts=2_500,
            content={
                "m.relates_to": {
                    "rel_type": "m.annotation",
                    "event_id": "$question",
                    "key": "1",
                },
            },
        )
        await alice.admit(reaction)

        await _activate_interactive_question(
            alice,
            "$question",
            revision_event_id="$question-edit",
            question_text="Choose again",
            options={"1": "new"},
            option_labels={"1": "New"},
        )

        selected = await alice.claim_interactive_reaction(
            source_event_id="$reaction",
        )

        assert selected is not None
        assert (selected.question_text, selected.selected_value) == ("Choose", "one")

        await alice.settle("$reaction")
        replacement_reaction, _ = message(
            "$replacement-reaction",
            kind=EventKind.REACTION,
            ts=3_500,
            content={
                "m.relates_to": {
                    "rel_type": "m.annotation",
                    "event_id": "$question",
                    "key": "1",
                },
            },
        )
        await alice.admit(replacement_reaction)
        replacement_selection = await alice.claim_interactive_reaction(
            source_event_id="$replacement-reaction",
        )

        assert replacement_selection is not None
        assert (replacement_selection.question_text, replacement_selection.selected_value) == (
            "Choose again",
            "new",
        )

    async def test_text_admission_preserves_the_prompt_seen_before_an_edit(
        self,
        alice: PrincipalStore,
    ) -> None:
        """A queued numeric answer cannot be reinterpreted through a later prompt revision."""
        await admit(alice, "$turn")
        await _activate_interactive_question(alice, "$question")
        await admit(alice, "$answer", thread_id="$thread", content=text("1"), ts=2_500)
        await _activate_interactive_question(
            alice,
            "$question",
            revision_event_id="$question-edit",
            question_text="Choose again",
            options={"1": "replacement"},
            option_labels={"1": "Replacement"},
        )

        selection = await alice.claim_interactive_text(
            source_event_id="$answer",
        )

        assert selection is not None
        assert (selection.question_text, selection.selected_value) == ("Choose", "one")

    async def test_interactive_reaction_claim_is_atomic_and_replayable(
        self,
        alice: PrincipalStore,
        journal_store: EventJournalStore,
    ) -> None:
        """Replaying one reaction returns the same selection instead of losing its claim."""
        await admit(alice, "$turn")
        await _activate_interactive_question(alice, "$question")
        await admit(
            alice,
            "$reaction",
            kind=EventKind.REACTION,
            content=reaction_content("$question", "👍"),
            ts=2_500,
        )
        expected = InteractiveSelection(
            question_event_id="$question",
            question_text="Choose",
            selection_key="👍",
            selected_label="One",
            selected_value="one",
            thread_id="$thread",
        )

        assert (
            await alice.claim_interactive_reaction(
                source_event_id="$reaction",
            )
            == expected
        )
        assert (
            await alice.claim_interactive_reaction(
                source_event_id="$reaction",
            )
            == expected
        )

        reaction = await alice.load_event("$reaction")
        assert reaction is not None
        assert reaction.semantic_consumer is SemanticConsumer.INTERACTIVE_REACTION
        assert await _interactive_question_rows(journal_store) == []
        rows = await _interactive_selection_rows(journal_store)
        assert [row["source_event_id"] for row in rows] == ["$reaction"]

    async def test_another_reaction_cannot_steal_an_interactive_claim(
        self,
        alice: PrincipalStore,
        journal_store: EventJournalStore,
    ) -> None:
        """A losing reaction keeps no semantic claim that would hide other routing."""
        await admit(alice, "$turn")
        await _activate_interactive_question(alice, "$question")
        for timestamp, event_id in enumerate(("$winner", "$loser"), start=2_500):
            reaction, _ = message(
                event_id,
                kind=EventKind.REACTION,
                ts=timestamp,
                content={
                    "m.relates_to": {
                        "rel_type": "m.annotation",
                        "event_id": "$question",
                        "key": "1",
                    },
                },
            )
            await alice.admit(reaction)
        assert await alice.claim_interactive_reaction(
            source_event_id="$winner",
        )

        assert (
            await alice.claim_interactive_reaction(
                source_event_id="$loser",
            )
            is None
        )

        loser = await alice.load_event("$loser")
        assert loser is not None
        assert loser.semantic_consumer is None
        assert await _interactive_question_rows(journal_store) == []
        rows = await _interactive_selection_rows(journal_store)
        assert [row["source_event_id"] for row in rows] == ["$winner"]

    async def test_interactive_text_claim_chooses_the_oldest_question_and_replays_it(
        self,
        alice: PrincipalStore,
        journal_store: EventJournalStore,
    ) -> None:
        """Database order replaces process-dictionary insertion order deterministically."""
        await admit(alice, "$turn")
        await _activate_interactive_question(alice, "$z-first", ts=1_000)
        await _activate_interactive_question(alice, "$a-second", ts=2_000)
        await admit(alice, "$answer", thread_id="$thread", content=text("1"), ts=2_500)
        expected = InteractiveSelection(
            question_event_id="$z-first",
            question_text="Choose",
            selection_key="1",
            selected_label="One",
            selected_value="one",
            thread_id="$thread",
        )

        assert (
            await alice.claim_interactive_text(
                source_event_id="$answer",
            )
            == expected
        )
        assert (
            await alice.claim_interactive_text(
                source_event_id="$answer",
            )
            == expected
        )

        rows = await _interactive_question_rows(journal_store)
        assert [row["question_event_id"] for row in rows] == ["$a-second"]
        selection_rows = await _interactive_selection_rows(journal_store)
        assert [row["source_event_id"] for row in selection_rows] == ["$answer"]

    async def test_room_level_text_claim_matches_a_room_level_question(
        self,
        alice: PrincipalStore,
    ) -> None:
        """Canonical room-level thread identity must preserve numeric answers."""
        await admit(alice, "$turn")
        await _activate_interactive_question(alice, "$question", thread_id=None)
        await admit(alice, "$answer", content=text("1"), ts=2_500)

        selection = await alice.claim_interactive_text(
            source_event_id="$answer",
        )

        assert selection is not None
        assert selection.question_event_id == "$question"
        assert selection.thread_id is None

    async def test_concurrent_text_answers_cannot_both_claim_one_question(
        self,
        alice: PrincipalStore,
    ) -> None:
        """The journal chooses one source owner when two numeric answers race."""
        await admit(alice, "$turn")
        await _activate_interactive_question(alice, "$question")
        await admit(alice, "$first-answer", thread_id="$thread", content=text("1"), ts=2_500)
        await admit(alice, "$second-answer", thread_id="$thread", content=text("1"), ts=2_600)

        claims = await asyncio.gather(
            alice.claim_interactive_text(
                source_event_id="$first-answer",
            ),
            alice.claim_interactive_text(
                source_event_id="$second-answer",
            ),
        )

        assert sum(selection is not None for selection in claims) == 1


class TestInteractiveQuestionConsumption:
    """Terminal sources and membership changes retire derived interactive state."""

    async def test_settling_a_selected_source_consumes_only_its_selection(
        self,
        alice: PrincipalStore,
        journal_store: EventJournalStore,
    ) -> None:
        """Selection consumption commits with the source's terminal journal fact."""
        await admit(alice, "$turn")
        for event_id in ("$claimed", "$active"):
            await _activate_interactive_question(alice, event_id)
        await admit(
            alice,
            "$reaction",
            kind=EventKind.REACTION,
            content=reaction_content("$claimed", "1"),
            ts=2_500,
        )
        assert await alice.claim_interactive_reaction(
            source_event_id="$reaction",
        )

        await alice.settle_many(("$reaction",))

        rows = await _interactive_question_rows(journal_store)
        assert [row["question_event_id"] for row in rows] == ["$active"]
        assert await _interactive_selection_rows(journal_store) == []

    async def test_settling_an_unrelated_source_keeps_active_questions(
        self,
        alice: PrincipalStore,
        journal_store: EventJournalStore,
    ) -> None:
        """Settling an unrelated source leaves active questions unchanged."""
        await admit(alice, "$turn")
        await _activate_interactive_question(alice, "$question")
        await admit(alice, "$other")

        await alice.settle("$other")

        rows = await _interactive_question_rows(journal_store)
        assert [row["question_event_id"] for row in rows] == ["$question"]

    async def test_departure_drops_active_questions_and_owned_selections_for_the_room(
        self,
        alice: PrincipalStore,
        journal_store: EventJournalStore,
    ) -> None:
        """The membership fence and its interactive-state deletion are one transaction."""
        await admit(alice, "$turn")
        await _activate_interactive_question(alice, "$active")
        await _activate_interactive_question(alice, "$claimed")
        await admit(alice, "$other-turn", room_id=OTHER_ROOM)
        await _activate_interactive_question(
            alice,
            "$other-room",
            room_id=OTHER_ROOM,
            source_event_id="$other-turn",
        )
        await admit(
            alice,
            "$reaction",
            kind=EventKind.REACTION,
            content=reaction_content("$claimed", "1"),
            ts=2_500,
        )
        assert await alice.claim_interactive_reaction(
            source_event_id="$reaction",
        )

        await alice.fence_departure(ROOM, source=DepartureSource.LOCAL)

        rows = await _interactive_question_rows(journal_store)
        assert [row["question_event_id"] for row in rows] == ["$other-room"]
        assert await _interactive_selection_rows(journal_store) == []


class TestDeliveryIsScopedToTheMembershipThatAuthorizedIt:
    """A turn that outlived its membership must not answer into the next one.

    The fence retires what the previous membership derived. Without this a
    turn still running when the fence committed could write some of it straight
    back after the fence had been and gone.
    """

    async def test_a_turn_admitted_under_an_ended_membership_cannot_enqueue(
        self,
        alice: PrincipalStore,
    ) -> None:
        """Fence first, then enqueue: the enqueue is refused."""
        await admit(alice, "$turn")
        await alice.fence_departure(ROOM, source=DepartureSource.LOCAL)

        transaction_id = await alice.enqueue_matrix_delivery(
            delivery_id="$turn",
            stage=DeliveryStage.FINAL,
            room_id=ROOM,
            thread_id=None,
            payload=text("answer"),
        )

        assert transaction_id is None
        assert await alice.load_matrix_delivery(delivery_id="$turn", stage=DeliveryStage.FINAL) is None

    async def test_an_admitted_turn_cannot_borrow_another_rooms_equal_epoch(
        self,
        alice: PrincipalStore,
    ) -> None:
        """Membership epochs are room-local and cannot prove the target room."""
        await admit(alice, "$turn", room_id=ROOM)

        assert not await alice.turn_membership_is_current(turn_id="$turn", room_id=OTHER_ROOM)
        transaction_id = await alice.enqueue_matrix_delivery(
            delivery_id="$turn",
            stage=DeliveryStage.FINAL,
            room_id=OTHER_ROOM,
            thread_id=None,
            payload=text("misrouted answer"),
        )

        assert transaction_id is None
        assert await alice.load_matrix_delivery(delivery_id="$turn", stage=DeliveryStage.FINAL) is None

    async def test_departure_retires_claimed_interactive_reactions_but_keeps_hooks(
        self,
        alice: PrincipalStore,
    ) -> None:
        """Only reactions that would start an answer become stale at departure."""
        await admit(alice, "$interactive", kind=EventKind.REACTION)
        await admit(alice, "$hook", kind=EventKind.REACTION)
        await alice.claim_semantic_consumer("$interactive", SemanticConsumer.INTERACTIVE_REACTION)
        await alice.claim_semantic_consumer("$hook", SemanticConsumer.REACTION_HOOKS)

        await alice.fence_departure(ROOM, source=DepartureSource.LOCAL)

        assert [event.event_id for event in await alice.pending()] == ["$hook"]

    async def test_interactive_claim_after_departure_retires_the_stale_reaction(
        self,
        alice: PrincipalStore,
    ) -> None:
        """A departure that wins the race prevents a later model-backed claim."""
        await admit(alice, "$interactive", kind=EventKind.REACTION)
        await alice.fence_departure(ROOM, source=DepartureSource.LOCAL)

        claimed = await alice.claim_semantic_consumer(
            "$interactive",
            SemanticConsumer.INTERACTIVE_REACTION,
        )

        assert claimed is None
        assert await alice.pending() == ()

    async def test_an_unattempted_row_enqueued_before_the_fence_is_retired_by_it(
        self,
        alice: PrincipalStore,
    ) -> None:
        """Enqueue first, then fence: the row remains only as its old owner."""
        await admit(alice, "$turn")
        assert (
            await alice.enqueue_matrix_delivery(
                delivery_id="$turn",
                stage=DeliveryStage.FINAL,
                room_id=ROOM,
                thread_id=None,
                payload=text("answer"),
            )
            is not None
        )

        await alice.fence_departure(ROOM, source=DepartureSource.LOCAL)

        retired = await alice.load_matrix_delivery(delivery_id="$turn", stage=DeliveryStage.FINAL)
        assert retired is not None
        assert retired.retired

    async def test_an_attempted_row_still_retries_after_a_fence_under_its_first_transaction(
        self,
        alice: PrincipalStore,
    ) -> None:
        """An attempted delivery is a different object, and refusing it is worse.

        Its outcome is unknown and the homeserver may hold it already. Only
        presenting the identical transaction ID again collapses the retry onto
        the same event; refusing it would strand the row unacknowledged while
        leaving whatever it sent visible, and re-deriving a fresh transaction
        for it would guarantee the second answer rather than prevent it.
        """
        await admit(alice, "$turn")
        first = await alice.enqueue_matrix_delivery(
            delivery_id="$turn",
            stage=DeliveryStage.FINAL,
            room_id=ROOM,
            thread_id=None,
            payload=text("answer"),
        )
        await alice.claim_matrix_delivery(delivery_id="$turn", stage=DeliveryStage.FINAL)

        await alice.fence_departure(ROOM, source=DepartureSource.LOCAL)

        retried = await alice.enqueue_matrix_delivery(
            delivery_id="$turn",
            stage=DeliveryStage.FINAL,
            room_id=ROOM,
            thread_id=None,
            payload=text("regenerated"),
        )
        claimed = await alice.claim_matrix_delivery(delivery_id="$turn", stage=DeliveryStage.FINAL)

        assert retried == first
        assert claimed is not None
        assert claimed.transaction_id == first
        assert claimed.payload["body"] == "answer"

    async def test_source_less_delivery_waits_for_an_active_membership(self, alice: PrincipalStore) -> None:
        """A schedule may deliver after rejoin, but not while no membership owns it."""
        await alice.fence_departure(ROOM, source=DepartureSource.LOCAL)

        while_departed = await alice.enqueue_matrix_delivery(
            delivery_id="scheduled-task-7",
            stage=DeliveryStage.FINAL,
            room_id=ROOM,
            thread_id=None,
            payload=text("reminder"),
        )
        await alice.note_membership_restarted(ROOM)
        after_rejoin = await alice.enqueue_matrix_delivery(
            delivery_id="scheduled-task-7",
            stage=DeliveryStage.FINAL,
            room_id=ROOM,
            thread_id=None,
            payload=text("reminder"),
        )

        assert while_departed is None
        assert after_rejoin is not None

    async def test_source_less_delivery_stages_share_one_membership(self, alice: PrincipalStore) -> None:
        """A final edit cannot adopt a later membership than its placeholder."""
        assert (
            await alice.enqueue_matrix_delivery(
                delivery_id="scheduled-task-7",
                stage=DeliveryStage.INITIAL,
                room_id=ROOM,
                thread_id=None,
                payload=text("Thinking..."),
            )
            is not None
        )
        await alice.claim_matrix_delivery(delivery_id="scheduled-task-7", stage=DeliveryStage.INITIAL)
        await alice.fence_departure(ROOM, source=DepartureSource.LOCAL)
        await alice.note_membership_restarted(ROOM)

        final = await alice.enqueue_matrix_delivery(
            delivery_id="scheduled-task-7",
            stage=DeliveryStage.FINAL,
            room_id=ROOM,
            thread_id=None,
            payload=text("reminder"),
            edits_event_id="$placeholder",
        )

        assert final is None
        assert await alice.load_matrix_delivery(delivery_id="scheduled-task-7", stage=DeliveryStage.FINAL) is None

    async def test_a_turn_under_the_current_membership_enqueues(self, alice: PrincipalStore) -> None:
        """The ordinary case still delivers."""
        await admit(alice, "$turn")

        transaction_id = await alice.enqueue_matrix_delivery(
            delivery_id="$turn",
            stage=DeliveryStage.FINAL,
            room_id=ROOM,
            thread_id=None,
            payload=text("answer"),
        )

        assert transaction_id == delivery_transaction_id("agent@alice", "$turn", "final")

    async def test_source_less_transport_keeps_its_outbox_membership_after_rejoin(
        self,
        alice: PrincipalStore,
    ) -> None:
        """A stream without a journal source still stops with its frozen owner."""
        await alice.enqueue_matrix_delivery(
            delivery_id="scheduled-stream",
            stage=DeliveryStage.INITIAL,
            room_id=ROOM,
            thread_id=None,
            payload=text("Thinking..."),
        )
        assert await alice.turn_membership_is_current(turn_id="scheduled-stream", room_id=ROOM)

        await alice.claim_matrix_delivery(delivery_id="scheduled-stream", stage=DeliveryStage.INITIAL)
        await alice.fence_departure(ROOM, source=DepartureSource.LOCAL)
        await alice.note_membership_restarted(ROOM)

        assert not await alice.turn_membership_is_current(turn_id="scheduled-stream", room_id=ROOM)

    async def test_departure_retires_an_unclaimed_source_less_stream_owner(
        self,
        alice: PrincipalStore,
    ) -> None:
        """A fence cannot erase the INITIAL owner before transport claims it."""
        await alice.enqueue_matrix_delivery(
            delivery_id="scheduled-stream",
            stage=DeliveryStage.INITIAL,
            room_id=ROOM,
            thread_id=None,
            payload=text("Thinking..."),
        )

        await alice.fence_departure(ROOM, source=DepartureSource.LOCAL)
        await alice.note_membership_restarted(ROOM)

        initial = await alice.load_matrix_delivery(delivery_id="scheduled-stream", stage=DeliveryStage.INITIAL)
        assert initial is not None
        assert initial.retired
        assert not await alice.turn_membership_is_current(turn_id="scheduled-stream", room_id=ROOM)
        assert (
            await alice.enqueue_matrix_delivery(
                delivery_id="scheduled-stream",
                stage=DeliveryStage.FINAL,
                room_id=ROOM,
                thread_id=None,
                payload=text("Done"),
            )
            is None
        )

    async def test_in_flight_transport_learns_the_membership_ended(self, alice: PrincipalStore) -> None:
        """Streaming edits never reach the outbox, so they ask this directly."""
        await admit(alice, "$turn")

        assert await alice.turn_membership_is_current(turn_id="$turn", room_id=ROOM)

        await alice.fence_departure(ROOM, source=DepartureSource.LOCAL)

        assert not await alice.turn_membership_is_current(turn_id="$turn", room_id=ROOM)

    async def test_one_rooms_fence_does_not_silence_another_room(self, alice: PrincipalStore) -> None:
        """Leaving one room says nothing about a turn running in a different one."""
        await admit(alice, "$turn")

        await alice.fence_departure(OTHER_ROOM, source=DepartureSource.LOCAL)

        assert await alice.turn_membership_is_current(turn_id="$turn", room_id=ROOM)
        assert (
            await alice.enqueue_matrix_delivery(
                delivery_id="$turn",
                stage=DeliveryStage.FINAL,
                room_id=ROOM,
                thread_id=None,
                payload=text("answer"),
            )
            is not None
        )


class TestDepartureBookkeeping:
    """One departure invalidates a room once, whichever observer sees it first."""

    async def test_a_consumed_report_leaves_the_new_projection_alone(self, alice: PrincipalStore) -> None:
        """Absorbing a report must not delete what the membership after it built."""
        await alice.fence_departure(ROOM, source=DepartureSource.LOCAL)
        await alice.note_membership_restarted(ROOM)
        await admit(alice, "$fresh", ts=5_000)

        await alice.fence_departure(ROOM, source=DepartureSource.REPORTED)

        page = await alice.read_conversation(room_id=ROOM, thread_id=None, limit=5)
        assert [m.logical_event_id for m in page.messages] == ["$fresh"]

    async def test_a_departure_with_no_report_owed_invalidates(self, alice: PrincipalStore) -> None:
        """A departure the bot never initiated drops what the old membership built."""
        await admit(alice, "$stale", ts=5_000)

        outcome = await alice.fence_departure(ROOM, source=DepartureSource.REPORTED)

        assert outcome.observation is DepartureObservation.FENCED
        page = await alice.read_conversation(room_id=ROOM, thread_id=None, limit=5)
        assert page.messages == ()

    async def test_owed_reports_are_scoped_to_one_principal(self, journal_store: EventJournalStore) -> None:
        """One bot's owed report must not absorb another bot's departure."""
        alice = journal_store.principal("agent@alice")
        bob = journal_store.principal("agent@bob")
        await alice.fence_departure(ROOM, source=DepartureSource.LOCAL)

        assert await bob.rooms_owing_departure_reports() == frozenset()
        assert (await bob.fence_departure(ROOM, source=DepartureSource.REPORTED)).fenced

    async def test_retiring_one_room_leaves_another_rooms_report_owed(self, alice: PrincipalStore) -> None:
        """Giving up on one room's report says nothing about any other room."""
        await alice.fence_departure(ROOM, source=DepartureSource.LOCAL)
        await alice.fence_departure(OTHER_ROOM, source=DepartureSource.LOCAL)

        await alice.retire_owed_departure_reports(ROOM)

        assert await alice.rooms_owing_departure_reports() == frozenset({OTHER_ROOM})
        # The retired room's report is no longer absorbed. It is recognised as
        # the departure this room is already fenced for, which is a different
        # answer from "a report was owed and this was it".
        retired = await alice.fence_departure(ROOM, source=DepartureSource.REPORTED)
        still_owed = await alice.fence_departure(OTHER_ROOM, source=DepartureSource.REPORTED)
        assert retired.observation is DepartureObservation.ALREADY_FENCED
        assert still_owed.observation is DepartureObservation.OWED_REPORT_CONSUMED


class TestByteOrderPinning:
    """Ordering that a cursor depends on must not vary with the server locale."""

    async def test_the_cursor_comparison_is_pinned_to_byte_order(self) -> None:
        """delivery_id and stage are pinned to byte ordering on both backends.

        A PostgreSQL locale whose collation is not byte order would sort them
        differently from the cursor's own comparison, and recovery would skip
        rows or revisit them. CI cannot catch that: its PostgreSQL image uses
        musl locales, which all behave like C, so the two orderings agree there
        and diverge in a glibc deployment.
        """
        ordering = "ORDER BY created_at_ns, delivery_id/*bytes*/, stage/*bytes*/"

        assert render(ordering, SQLITE_DIALECT) == "ORDER BY created_at_ns, delivery_id, stage"
        assert render(ordering, POSTGRES_DIALECT) == (
            'ORDER BY created_at_ns, delivery_id COLLATE "C", stage COLLATE "C"'
        )

    async def test_a_marker_inside_a_literal_is_refused(self) -> None:
        """Substitution is a plain rewrite and cannot tell a literal from an identifier.

        No statement embeds one today, and values are bound separately by both
        backends, so nothing user-controlled reaches the rewriter. The guard is
        there because the rewriter has no way to check that for itself.
        """
        with pytest.raises(ValueError, match="byte-order marker"):
            render("SELECT '/*bytes*/'", SQLITE_DIALECT)

    async def test_a_statement_without_the_marker_is_untouched(self) -> None:
        """The rewrite must not perturb the statements that do not opt in."""
        assert render("SELECT 1", SQLITE_DIALECT) == "SELECT 1"
        assert render("SELECT 1", POSTGRES_DIALECT) == "SELECT 1"


class TestMembershipEpoch:
    """Leaving and rejoining invalidates what the previous membership saw."""

    async def test_a_join_closes_only_its_preceding_reported_departure(
        self,
        alice: PrincipalStore,
    ) -> None:
        """Replaying an old join cannot clear a later departure fence."""
        departure = replace(
            message("$leave")[0],
            kind=EventKind.ROOM_LIFECYCLE,
            event_class=EventClass.CONTEXT_ONLY,
        )
        await alice.admit(departure, None)
        await alice.fence_departure(
            ROOM,
            source=DepartureSource.REPORTED,
            report_observation_id=departure.event_id,
        )
        join = replace(
            message("$join")[0],
            kind=EventKind.ROOM_LIFECYCLE,
            event_class=EventClass.CONTEXT_ONLY,
        )
        await alice.admit(join, None)
        await alice.close_preceding_reported_departure(
            ROOM,
            join.event_id,
        )
        assert await _membership_accepts_question(alice, 1)

        await alice.fence_departure(ROOM, source=DepartureSource.LOCAL)
        await alice.close_preceding_reported_departure(
            ROOM,
            join.event_id,
        )

        assert not await _membership_accepts_question(alice, 2)

    async def test_an_old_join_cannot_rearm_a_newer_local_departure(
        self,
        alice: PrincipalStore,
    ) -> None:
        """Delayed reports retain the leave/join pairing from their timeline order."""
        await alice.fence_departure(ROOM, source=DepartureSource.LOCAL)
        await alice.note_membership_restarted(ROOM)
        await alice.fence_departure(ROOM, source=DepartureSource.LOCAL)
        for event_id in ("$leave-1", "$join-1", "$leave-2"):
            event = replace(
                message(event_id)[0],
                kind=EventKind.ROOM_LIFECYCLE,
                event_class=EventClass.CONTEXT_ONLY,
            )
            await alice.admit(event, None)
            if event_id.startswith("$leave"):
                await alice.fence_departure(
                    ROOM,
                    source=DepartureSource.REPORTED,
                    report_observation_id=event_id,
                )
            else:
                await alice.close_preceding_reported_departure(ROOM, event_id)

        await alice.fence_departure(
            ROOM,
            source=DepartureSource.REPORTED,
            report_observation_id="$leave-1",
        )
        await alice.fence_departure(
            ROOM,
            source=DepartureSource.REPORTED,
            report_observation_id="$leave-2",
        )

        assert not await _membership_accepts_question(alice, 2)

    async def test_a_join_closes_a_preceding_truncated_departure_report(
        self,
        alice: PrincipalStore,
    ) -> None:
        """Synthetic sync-token observations participate in journal ordering."""
        await alice.fence_departure(
            ROOM,
            source=DepartureSource.REPORTED,
            report_observation_id="classic:s-left:!room:example.org",
        )
        join = replace(
            message("$join-after-truncated-leave")[0],
            kind=EventKind.ROOM_LIFECYCLE,
            event_class=EventClass.CONTEXT_ONLY,
        )
        await alice.admit(join, None)

        await alice.close_preceding_reported_departure(ROOM, join.event_id)

        assert await _membership_accepts_question(alice, 1)

    async def test_hydration_is_recorded_per_membership(self, alice: PrincipalStore) -> None:
        """Hydration is recorded per membership."""
        epoch = await alice.membership_epoch(ROOM)
        installed = await alice.install_hydrated_conversation(
            room_id=ROOM,
            thread_id=None,
            events=(message("$hydrated")[1],),
            complete=True,
            expected_membership_epoch=epoch,
        )

        assert installed
        assert await alice.conversation_is_hydrated(room_id=ROOM, thread_id=None)
        assert await bodies(alice) == ["$hydrated"]

    async def test_rejoining_invalidates_hydration(self, alice: PrincipalStore) -> None:
        """Rejoining invalidates hydration."""
        epoch = await alice.membership_epoch(ROOM)
        await alice.install_hydrated_conversation(
            room_id=ROOM,
            thread_id=None,
            events=(message("$hydrated")[1],),
            complete=True,
            expected_membership_epoch=epoch,
        )

        await alice.fence_departure(ROOM, source=DepartureSource.LOCAL)

        assert not await alice.conversation_is_hydrated(room_id=ROOM, thread_id=None)

    async def test_rejoining_clears_a_conversation_the_last_membership_proved_whole(
        self,
        alice: PrincipalStore,
    ) -> None:
        """Coverage carries forward inside a membership and never across one.

        Inside one, a walk that ran out of conversation proved something about
        the conversation rather than about itself, so a later narrower walk
        cannot take it back. A rejoin is a different membership over a slice of
        history the bot may never have seen, and nothing the previous one
        proved is allowed to speak for it.
        """
        await alice.install_hydrated_conversation(
            room_id=ROOM,
            thread_id=None,
            events=(message("$whole")[1],),
            complete=True,
            expected_membership_epoch=await alice.membership_epoch(ROOM),
        )

        await alice.fence_departure(ROOM, source=DepartureSource.LOCAL)
        await alice.note_membership_restarted(ROOM)
        await alice.install_hydrated_conversation(
            room_id=ROOM,
            thread_id=None,
            events=(message("$suffix")[1],),
            complete=False,
            expected_membership_epoch=await alice.membership_epoch(ROOM),
        )

        assert not await alice.conversation_is_complete(room_id=ROOM, thread_id=None)
        assert await alice.conversation_hydration_was_truncated(room_id=ROOM, thread_id=None)

    async def test_rejoining_drops_answers_the_previous_membership_never_sent(
        self,
        alice: PrincipalStore,
    ) -> None:
        """An unsent answer belongs to the conversation it was written for.

        Delivering it after a leave and rejoin would drop a reply to the old
        membership into the new one, where nothing asked for it.
        """
        await alice.enqueue_matrix_delivery(
            delivery_id="turn-1",
            stage=DeliveryStage.FINAL,
            room_id=ROOM,
            thread_id=None,
            payload=text("answer"),
        )

        await alice.fence_departure(ROOM, source=DepartureSource.LOCAL)

        assert await alice.unacknowledged_matrix_deliveries() == ()
        retired = await alice.load_matrix_delivery(delivery_id="turn-1", stage=DeliveryStage.FINAL)
        assert retired is not None
        assert retired.retired is True

    async def test_rejoining_keeps_an_answer_that_may_already_be_visible(
        self,
        alice: PrincipalStore,
    ) -> None:
        """An attempted delivery has an outcome only the homeserver knows.

        Deleting it would free the turn to run again and post a second answer.
        The row is what makes the retry converge instead: it still holds the
        frozen payload and the transaction that goes with it.
        """
        transaction_id = await alice.enqueue_matrix_delivery(
            delivery_id="turn-1",
            stage=DeliveryStage.FINAL,
            room_id=ROOM,
            thread_id=None,
            payload=text("answer"),
        )
        await alice.claim_matrix_delivery(delivery_id="turn-1", stage=DeliveryStage.FINAL)

        await alice.fence_departure(ROOM, source=DepartureSource.LOCAL)

        kept = await alice.load_matrix_delivery(delivery_id="turn-1", stage=DeliveryStage.FINAL)
        assert kept is not None
        assert kept.transaction_id == transaction_id
        assert kept.payload["body"] == "answer"
        assert [delivery.delivery_id for delivery in await alice.unacknowledged_matrix_deliveries()] == ["turn-1"]

    async def test_rejoining_keeps_an_answer_matrix_already_accepted(
        self,
        alice: PrincipalStore,
    ) -> None:
        """That row is the record that the message is already visible."""
        await alice.enqueue_matrix_delivery(
            delivery_id="turn-1",
            stage=DeliveryStage.FINAL,
            room_id=ROOM,
            thread_id=None,
            payload=text("answer"),
        )
        await alice.claim_matrix_delivery(delivery_id="turn-1", stage=DeliveryStage.FINAL)
        await alice.acknowledge_matrix_delivery(
            delivery_id="turn-1",
            stage=DeliveryStage.FINAL,
            event_id="$sent",
            delivered_projections=(),
        )

        await alice.fence_departure(ROOM, source=DepartureSource.LOCAL)

        stored = await alice.load_matrix_delivery(delivery_id="turn-1", stage=DeliveryStage.FINAL)
        assert stored is not None
        assert stored.acknowledged_event_id == "$sent"

    async def test_context_only_events_keep_no_payload(self, alice: PrincipalStore) -> None:
        """Otherwise the journal becomes the raw-event cache it replaces.

        A context-only event is projected at admission and never replayed, so
        it is admitted already settled — which means settlement, the step that
        clears a payload, never runs for it. Storing the source anyway retains
        every message the bot has ever seen, forever.
        """
        body = "x" * 500
        admission, projected = message("$history", content=text(body), event_class=EventClass.CONTEXT_ONLY)
        await alice.admit(admission, projected)

        stored = await alice.load_event("$history")

        assert stored is not None
        assert stored.source == {}
        assert await bodies(alice) == [body]

    async def test_actionable_events_keep_their_replay_payload(self, alice: PrincipalStore) -> None:
        """Compaction must not reach the events a crash has to replay."""
        admission, projected = message("$live", content=text("answer me"))
        await alice.admit(admission, projected)

        stored = await alice.load_event("$live")

        assert stored is not None
        assert stored.source != {}

    async def test_rejoining_removes_what_the_previous_membership_projected(
        self,
        alice: PrincipalStore,
    ) -> None:
        """Otherwise the two memberships merge into one conversation.

        Dropping only the hydration marker leaves the old messages readable,
        so the next hydration adds the new membership's view on top of a
        history this membership may not be entitled to see at all.
        """
        epoch = await alice.membership_epoch(ROOM)
        await alice.install_hydrated_conversation(
            room_id=ROOM,
            thread_id=None,
            events=(message("$before")[1],),
            complete=True,
            expected_membership_epoch=epoch,
        )
        assert await bodies(alice) == ["$before"]

        await alice.fence_departure(ROOM, source=DepartureSource.LOCAL)

        assert await bodies(alice) == []

    async def test_rejoining_keeps_the_proof_that_an_event_was_answered(
        self,
        alice: PrincipalStore,
    ) -> None:
        """The dedup record has to outlive any rejoin, or the turn runs twice."""
        admission, projected = message("$answered")
        await alice.admit(admission, projected)
        await alice.settle("$answered")

        await alice.fence_departure(ROOM, source=DepartureSource.LOCAL)

        assert await alice.load_event("$answered") is not None
        assert await alice.admit(*message("$answered")) is AdmissionResult.DUPLICATE

    async def test_hydration_racing_a_rejoin_installs_nothing(self, alice: PrincipalStore) -> None:
        """A partly applied hydration would look complete to the next reader."""
        stale_epoch = await alice.membership_epoch(ROOM)
        await alice.fence_departure(ROOM, source=DepartureSource.LOCAL)

        installed = await alice.install_hydrated_conversation(
            room_id=ROOM,
            thread_id=None,
            events=(message("$hydrated")[1],),
            complete=True,
            expected_membership_epoch=stale_epoch,
        )

        assert not installed
        assert await bodies(alice) == []
        assert not await alice.conversation_is_hydrated(room_id=ROOM, thread_id=None)


class TestBoundedHydrationInstallation:
    """Strict walks never monopolize one backend write for their full result."""

    @staticmethod
    def _assert_bounded_projection_writes(shapes: list[_HydrationWriteShape], *, expected_events: int) -> None:
        """Assert the externally relevant transaction shape of a hydration install."""
        projection_shapes = [shape for shape in shapes if shape.projected_messages]
        assert sum(shape.projected_messages for shape in projection_shapes) == expected_events
        assert len(projection_shapes) >= 2
        assert max(shape.projected_messages for shape in projection_shapes) <= _HYDRATION_TRANSACTION_EVENT_LIMIT
        assert all(shape.membership_claims == 1 for shape in projection_shapes)

    @staticmethod
    async def _install_recovery_events(
        store: PrincipalStore,
        recovery: RoomHistoryRecovery,
        events: tuple[ProjectedEvent, ...],
        *,
        expected_membership_epoch: int,
    ) -> None:
        """Install test recovery events through independently fenced chunks."""
        for offset in range(0, len(events), _HYDRATION_TRANSACTION_EVENT_LIMIT):
            assert await store.install_room_history_recovery_chunk(
                recovery,
                events=events[offset : offset + _HYDRATION_TRANSACTION_EVENT_LIMIT],
                expected_membership_epoch=expected_membership_epoch,
            )

    async def test_ordinary_hydration_projects_in_bounded_writes_then_publishes(
        self,
        journal_store: EventJournalStore,
    ) -> None:
        """Putting the complete projection loop back in one write breaks this test."""
        observed = _ObservedHydrationBackend(journal_store.backend)
        alice = EventJournalStore(backend=observed).principal("agent@alice")
        events = hydration_messages(_EVENTS_SPANNING_TWO_HYDRATION_CHUNKS)

        installed = await alice.install_hydrated_conversation(
            room_id=ROOM,
            thread_id=None,
            events=events,
            complete=True,
            attempted_policy_rank=2,
            expected_membership_epoch=await alice.membership_epoch(ROOM),
        )

        assert installed
        self._assert_bounded_projection_writes(observed.write_shapes, expected_events=len(events))
        publication_shapes = [shape for shape in observed.write_shapes if shape.hydration_markers]
        assert publication_shapes == [observed.write_shapes[-1]]
        assert publication_shapes[0].projected_messages == 0
        assert publication_shapes[0].membership_claims == 1
        assert await alice.conversation_is_hydrated(room_id=ROOM, thread_id=None)
        assert await alice.conversation_is_complete(room_id=ROOM, thread_id=None)
        coverage = await alice.conversation_hydration_coverage(room_id=ROOM, thread_id=None)
        assert coverage is not None
        assert coverage.attempted_policy_rank == 2

    async def test_empty_hydration_still_claims_the_epoch_when_it_publishes(
        self,
        journal_store: EventJournalStore,
    ) -> None:
        """Skipping the final write when there are no chunks breaks this test."""
        observed = _ObservedHydrationBackend(journal_store.backend)
        alice = EventJournalStore(backend=observed).principal("agent@alice")

        installed = await alice.install_hydrated_conversation(
            room_id=ROOM,
            thread_id=None,
            events=(),
            complete=True,
            attempted_policy_rank=2,
            expected_membership_epoch=await alice.membership_epoch(ROOM),
        )

        assert installed
        assert observed.write_shapes == [
            _HydrationWriteShape(membership_claims=1, hydration_markers=1),
        ]
        assert await alice.conversation_is_hydrated(room_id=ROOM, thread_id=None)

    async def test_recovery_chunk_claims_both_fences_before_projection(
        self,
        journal_store: EventJournalStore,
    ) -> None:
        """A recovery page must not enter a different gap or membership."""
        alice = journal_store.principal("agent@alice")
        recovery = await alice.record_room_history_recovery(ROOM)
        assert recovery is not None
        observed = _ObservedHydrationBackend(journal_store.backend)
        recovering = EventJournalStore(backend=observed).principal("agent@alice")
        events = hydration_messages(3)

        installed = await recovering.install_room_history_recovery_chunk(
            recovery,
            events=events,
            expected_membership_epoch=await recovering.membership_epoch(ROOM),
        )

        assert installed
        assert observed.write_shapes == [
            _HydrationWriteShape(
                membership_claims=1,
                recovery_claims=1,
                projected_messages=len(events),
            ),
        ]
        assert not await recovering.conversation_is_hydrated(room_id=ROOM, thread_id=None)
        assert await recovering.room_history_recovery(ROOM) == recovery

    async def test_recovery_chunk_refuses_a_superseded_exact_recovery(
        self,
        journal_store: EventJournalStore,
    ) -> None:
        """A later gap must fence every remaining page of an older walk."""
        alice = journal_store.principal("agent@alice")
        stale = await alice.record_room_history_recovery(ROOM)
        current = await alice.record_room_history_recovery(ROOM)
        assert stale is not None
        assert current is not None
        observed = _ObservedHydrationBackend(journal_store.backend)
        recovering = EventJournalStore(backend=observed).principal("agent@alice")

        installed = await recovering.install_room_history_recovery_chunk(
            stale,
            events=hydration_messages(3),
            expected_membership_epoch=await recovering.membership_epoch(ROOM),
        )

        assert not installed
        assert observed.write_shapes == [
            _HydrationWriteShape(membership_claims=1, recovery_claims=1),
        ]
        assert await recovering.room_history_recovery(ROOM) == current
        assert await bodies(recovering) == []

    async def test_recovery_chunk_refuses_a_stale_membership_epoch(
        self,
        journal_store: EventJournalStore,
    ) -> None:
        """Membership invalidation must fence projection before recovery lookup."""
        alice = journal_store.principal("agent@alice")
        recovery = await alice.record_room_history_recovery(ROOM)
        assert recovery is not None
        stale_epoch = await alice.membership_epoch(ROOM)
        await alice.fence_departure(ROOM, source=DepartureSource.LOCAL)
        observed = _ObservedHydrationBackend(journal_store.backend)
        recovering = EventJournalStore(backend=observed).principal("agent@alice")

        installed = await recovering.install_room_history_recovery_chunk(
            recovery,
            events=hydration_messages(3),
            expected_membership_epoch=stale_epoch,
        )

        assert not installed
        assert observed.write_shapes == [_HydrationWriteShape(membership_claims=1)]
        assert await bodies(recovering) == []

    async def test_recovery_chunk_rejects_more_than_the_transaction_bound(
        self,
        alice: PrincipalStore,
    ) -> None:
        """Callers cannot turn the page API back into one unbounded write."""
        recovery = await alice.record_room_history_recovery(ROOM)
        assert recovery is not None

        with pytest.raises(ValueError, match="at most 256"):
            await alice.install_room_history_recovery_chunk(
                recovery,
                events=hydration_messages(_HYDRATION_TRANSACTION_EVENT_LIMIT + 1),
                expected_membership_epoch=await alice.membership_epoch(ROOM),
            )

        assert await bodies(alice, limit=_HYDRATION_TRANSACTION_EVENT_LIMIT + 1) == []
        assert await alice.room_history_recovery(ROOM) == recovery

    async def test_history_recovery_projects_in_bounded_writes_then_settles(
        self,
        journal_store: EventJournalStore,
    ) -> None:
        """Combining recovery projection and settlement into one giant write breaks this test."""
        alice = journal_store.principal("agent@alice")
        recovery = await alice.record_room_history_recovery(ROOM)
        assert recovery is not None
        events = hydration_messages(_EVENTS_SPANNING_TWO_HYDRATION_CHUNKS)
        observed = _ObservedHydrationBackend(journal_store.backend)
        recovering = EventJournalStore(backend=observed).principal("agent@alice")
        epoch = await recovering.membership_epoch(ROOM)

        await self._install_recovery_events(
            recovering,
            recovery,
            events,
            expected_membership_epoch=epoch,
        )

        outcome = await recovering.settle_room_history_recovery(
            recovery,
            exhausted_server=True,
            attempted_policy_rank=2,
            expected_membership_epoch=epoch,
        )

        assert outcome is HistoryRecoveryOutcome.REPAIRED
        self._assert_bounded_projection_writes(observed.write_shapes, expected_events=len(events))
        final_shape = observed.write_shapes[-1]
        assert final_shape.hydration_markers == 1
        assert final_shape.recovery_settlements == 1
        assert final_shape.projected_messages == 0
        assert final_shape.membership_claims == 1
        assert final_shape.recovery_claims == 1
        assert await recovering.room_history_recovery(ROOM) is None
        assert await recovering.conversation_is_hydrated(room_id=ROOM, thread_id=None)

    async def test_failure_after_a_recovery_chunk_leaves_the_obligation_retryable(
        self,
        journal_store: EventJournalStore,
    ) -> None:
        """Publishing or settling before every chunk lands breaks this retry."""
        alice = journal_store.principal("agent@alice")
        recovery = await alice.record_room_history_recovery(ROOM)
        assert recovery is not None
        events = hydration_messages(_EVENTS_SPANNING_TWO_HYDRATION_CHUNKS)

        async def fail_second_write(write_number: int) -> None:
            if write_number == 2:
                msg = "injected failure after a committed hydration chunk"
                raise RuntimeError(msg)

        observed = _ObservedHydrationBackend(journal_store.backend, before_write=fail_second_write)
        recovering = EventJournalStore(backend=observed).principal("agent@alice")
        epoch = await recovering.membership_epoch(ROOM)

        with pytest.raises(RuntimeError, match="injected failure"):
            await self._install_recovery_events(
                recovering,
                recovery,
                events,
                expected_membership_epoch=epoch,
            )

        assert observed.write_shapes[0].projected_messages == _HYDRATION_TRANSACTION_EVENT_LIMIT
        assert not await alice.conversation_is_hydrated(room_id=ROOM, thread_id=None)
        assert await alice.conversation_hydration_coverage(room_id=ROOM, thread_id=None) is None
        assert await alice.room_history_recovery(ROOM) == recovery

        await self._install_recovery_events(
            alice,
            recovery,
            events,
            expected_membership_epoch=epoch,
        )
        outcome = await alice.settle_room_history_recovery(
            recovery,
            exhausted_server=True,
            attempted_policy_rank=2,
            expected_membership_epoch=epoch,
        )

        assert outcome is HistoryRecoveryOutcome.REPAIRED
        assert await alice.room_history_recovery(ROOM) is None
        page = await alice.read_conversation(room_id=ROOM, thread_id=None, limit=len(events) + 1)
        assert {message.logical_event_id for message in page.messages} == {event.event_id for event in events}

    async def test_truncated_recovery_finalization_does_not_scan_conversation_markers(
        self,
        journal_store: EventJournalStore,
    ) -> None:
        """The final bounded transaction must not rewrite every thread marker."""
        alice = journal_store.principal("agent@alice")
        recovery = await alice.record_room_history_recovery(ROOM)
        assert recovery is not None
        observed = _ObservedHydrationBackend(journal_store.backend)
        recovering = EventJournalStore(backend=observed).principal("agent@alice")

        outcome = await recovering.settle_room_history_recovery(
            recovery,
            exhausted_server=False,
            attempted_policy_rank=2,
            expected_membership_epoch=await recovering.membership_epoch(ROOM),
        )

        assert outcome is HistoryRecoveryOutcome.TRUNCATED
        assert observed.write_shapes == [
            _HydrationWriteShape(membership_claims=1, recovery_claims=1, hydration_markers=1),
        ]

    async def test_cancellation_after_a_chunk_leaves_ordinary_hydration_retryable(
        self,
        journal_store: EventJournalStore,
    ) -> None:
        """Publishing in a projection chunk would make cancellation look complete."""
        first_chunk_committed = asyncio.Event()
        release = asyncio.Event()

        async def pause_after_first_write(write_number: int) -> None:
            if write_number == 1:
                first_chunk_committed.set()
                await release.wait()

        observed = _ObservedHydrationBackend(journal_store.backend, after_write=pause_after_first_write)
        hydrating = EventJournalStore(backend=observed).principal("agent@alice")
        alice = journal_store.principal("agent@alice")
        events = hydration_messages(_EVENTS_SPANNING_TWO_HYDRATION_CHUNKS)
        epoch = await alice.membership_epoch(ROOM)
        install = asyncio.create_task(
            hydrating.install_hydrated_conversation(
                room_id=ROOM,
                thread_id=None,
                events=events,
                complete=True,
                attempted_policy_rank=2,
                expected_membership_epoch=epoch,
            ),
        )
        await first_chunk_committed.wait()
        install.cancel()
        with pytest.raises(asyncio.CancelledError):
            await install
        release.set()

        assert not await alice.conversation_is_hydrated(room_id=ROOM, thread_id=None)
        assert await alice.conversation_hydration_coverage(room_id=ROOM, thread_id=None) is None
        partial = await alice.read_conversation(room_id=ROOM, thread_id=None, limit=len(events))
        assert len(partial.messages) == _HYDRATION_TRANSACTION_EVENT_LIMIT

        assert await alice.install_hydrated_conversation(
            room_id=ROOM,
            thread_id=None,
            events=events,
            complete=True,
            attempted_policy_rank=2,
            expected_membership_epoch=epoch,
        )
        page = await alice.read_conversation(room_id=ROOM, thread_id=None, limit=len(events))
        assert {message.logical_event_id for message in page.messages} == {event.event_id for event in events}

    async def test_membership_fence_between_chunks_supersedes_the_whole_install(
        self,
        journal_store: EventJournalStore,
    ) -> None:
        """Omitting the per-chunk epoch claim resurrects rows behind this fence."""
        alice = journal_store.principal("agent@alice")
        stale_epoch = await alice.membership_epoch(ROOM)

        async def fence_after_first_write(write_number: int) -> None:
            if write_number == 1:
                await alice.fence_departure(ROOM, source=DepartureSource.LOCAL)

        observed = _ObservedHydrationBackend(journal_store.backend, after_write=fence_after_first_write)
        hydrating = EventJournalStore(backend=observed).principal("agent@alice")
        installed = await hydrating.install_hydrated_conversation(
            room_id=ROOM,
            thread_id=None,
            events=hydration_messages(_EVENTS_SPANNING_TWO_HYDRATION_CHUNKS),
            complete=True,
            attempted_policy_rank=2,
            expected_membership_epoch=stale_epoch,
        )

        assert not installed
        assert await alice.membership_epoch(ROOM) == stale_epoch + 1
        assert await bodies(alice, limit=_EVENTS_SPANNING_TWO_HYDRATION_CHUNKS) == []
        assert not await alice.conversation_is_hydrated(room_id=ROOM, thread_id=None)

    async def test_membership_fence_during_recovery_does_not_settle_a_newer_obligation(
        self,
        journal_store: EventJournalStore,
    ) -> None:
        """Settling recovery outside final epoch validation erases this newer obligation."""
        alice = journal_store.principal("agent@alice")
        old_recovery = await alice.record_room_history_recovery(ROOM)
        assert old_recovery is not None
        stale_epoch = await alice.membership_epoch(ROOM)
        newer_recovery: RoomHistoryRecovery | None = None

        async def fence_and_record_new_recovery(write_number: int) -> None:
            nonlocal newer_recovery
            if write_number == 1:
                await alice.fence_departure(ROOM, source=DepartureSource.LOCAL)
                await alice.note_membership_restarted(ROOM)
                await admit(alice, "$new-anchor", ts=10_000)
                newer_recovery = await alice.record_room_history_recovery(ROOM)

        observed = _ObservedHydrationBackend(journal_store.backend, after_write=fence_and_record_new_recovery)
        recovering = EventJournalStore(backend=observed).principal("agent@alice")
        events = hydration_messages(_EVENTS_SPANNING_TWO_HYDRATION_CHUNKS)

        installed = await recovering.install_room_history_recovery_chunk(
            old_recovery,
            events=events[:_HYDRATION_TRANSACTION_EVENT_LIMIT],
            expected_membership_epoch=stale_epoch,
        )
        assert installed
        refused = await recovering.install_room_history_recovery_chunk(
            old_recovery,
            events=events[_HYDRATION_TRANSACTION_EVENT_LIMIT:],
            expected_membership_epoch=stale_epoch,
        )
        assert not refused
        outcome = await recovering.settle_room_history_recovery(
            old_recovery,
            exhausted_server=True,
            attempted_policy_rank=2,
            expected_membership_epoch=stale_epoch,
        )

        assert outcome is HistoryRecoveryOutcome.SUPERSEDED
        assert newer_recovery is not None
        assert await alice.room_history_recovery(ROOM) == newer_recovery
        assert await bodies(alice, limit=len(events)) == ["$new-anchor"]
        assert not await alice.conversation_is_hydrated(room_id=ROOM, thread_id=None)


# How long a racer is given to queue behind the held row before the test calls
# it a failure, and how often that is checked. Generous because the wait is a
# condition rather than a delay: reaching it early costs nothing.
_QUEUE_TIMEOUT_SECONDS = 20.0
_QUEUE_POLL_SECONDS = 0.01

# Racer connections parked on a heavyweight lock. `pg_stat_activity` is
# cluster-wide, so the count is scoped by the application name the two rival
# stores connect under, minted per fixture and shared by nothing else -- not
# the connection holding the row, not the one running this query, not another
# xdist worker. Scoping by database would work today -- `postgres_journal_url`
# gives each worker its own `mindroom_<worker_id>` and a master run its own
# container -- but it would read isolation off the fixture's topology rather
# than off the connections being counted. Should that topology ever narrow to
# one shared database, an unrelated waiter would inflate the count, the row
# would be released before both racers were behind it, and the late one would
# decline against an already-bound row: the test passing for the one reason it
# exists to rule out, and passing silently.
_QUEUED_RACERS = """
    SELECT count(*) FROM pg_stat_activity
    WHERE application_name = %s AND wait_event_type = 'Lock'
"""


@dataclass(frozen=True, slots=True)
class RivalStores:
    """Two stores over one PostgreSQL database, and the DSN that reaches it.

    Two stores rather than two coroutines, because a store serializes its own
    writes behind one connection and one lock. Anything sharing that has
    already been made to take turns, which is the opposite of the situation
    worth testing.
    """

    first: EventJournalStore
    second: EventJournalStore
    database_url: str
    # What both stores report as their `application_name`, and the only handle
    # that tells their connections apart from every other one on the server.
    racer_application_name: str


@pytest_asyncio.fixture
async def rival_stores(postgres_journal_url: str) -> AsyncGenerator[RivalStores, None]:
    """Open two independently connected stores onto one database.

    PostgreSQL only, and deliberately not part of the backend parity sweep.
    Two SQLite stores in one process are two queues onto the same serialized
    write, so racing them here would only prove something about the fixture.

    Not because SQLite has no second connection to race -- it has one whenever
    ``mindroom threads export`` is running, which is its own process with its
    own writer. That race is real, and it is not this fixture's to stage: it
    needs a second interpreter rather than a second object, which is what
    ``TestCrossProcessWriters`` spawns.

    Only the stores carry the application name. The connection that holds the
    row and the one that watches for waiters use the bare DSN, so neither can
    be mistaken for a racer it is supposed to be observing.
    """
    database_url = postgres_journal_schema_url(postgres_journal_url)
    application_name = f"mindroom-journal-race-{uuid.uuid4().hex}"
    racer_url = f"{database_url}&application_name={application_name}"
    first = EventJournalStore.open_postgres(racer_url)
    second = EventJournalStore.open_postgres(racer_url)
    try:
        yield RivalStores(
            first=first,
            second=second,
            database_url=database_url,
            racer_application_name=application_name,
        )
    finally:
        await first.close()
        await second.close()


@contextmanager
def _outbox_row_held(database_url: str, *, principal_id: str, turn_id: str) -> Iterator[None]:
    """Lock one outbox row from a third connection until the block exits.

    The hold point has to be the row rather than anything in Python, because
    the row is the one place both implementations of the write must pass
    through. A caller parked here has finished whatever reading it does and
    has not yet written, which is exactly the interleaving that decides who
    owns the acknowledgement -- and it is the same point whether the caller
    read first or is reading and writing in one statement.
    """
    import psycopg  # noqa: PLC0415 - psycopg ships in the optional postgres extra

    with psycopg.connect(database_url) as connection:
        held = connection.execute(
            "SELECT 1 FROM matrix_delivery_outbox WHERE principal_id = %s AND delivery_id = %s FOR UPDATE",
            (principal_id, turn_id),
        ).fetchone()
        assert held is not None, "there is no enqueued delivery to hold"
        try:
            yield
        finally:
            connection.rollback()


async def _await_queued_racers(database_url: str, *, application_name: str, expected: int) -> None:
    """Wait until ``expected`` of that application's connections are parked on a lock.

    A condition, not a delay. The row may only be released once every racer is
    behind it, because a racer that has not started yet would read the bound
    row and decline on its own -- which is the losing implementation passing
    for a reason that has nothing to do with what it does under contention.
    """
    await asyncio.to_thread(_watch_queued_racers, database_url, application_name, expected)


def _watch_queued_racers(database_url: str, application_name: str, expected: int) -> None:
    """Poll until enough racers are waiting, or say how many turned up."""
    import psycopg  # noqa: PLC0415 - psycopg ships in the optional postgres extra

    deadline = time.monotonic() + _QUEUE_TIMEOUT_SECONDS
    with psycopg.connect(database_url, autocommit=True) as connection:
        while True:
            row = connection.execute(_QUEUED_RACERS, (application_name,)).fetchone()
            queued = 0 if row is None else int(row[0])
            if queued >= expected:
                return
            if time.monotonic() > deadline:
                msg = f"only {queued} of {expected} racers queued on the held outbox row"
                raise AssertionError(msg)
            time.sleep(_QUEUE_POLL_SECONDS)


def _watch_until_queued_or_finished(
    database_url: str,
    application_name: str,
    finished: threading.Event,
) -> None:
    """Block until the other writer is parked on a lock, or got past without one.

    Either answer is the interleaving this is opening a window for, which is
    why both end the wait. A writer that queued was ordered behind the row this
    transaction holds; a writer that finished was not ordered at all, and the
    caller resuming here is precisely the one whose rows then outlive it. The
    wait exists so neither outcome depends on which thread the scheduler
    happens to run first.
    """
    import psycopg  # noqa: PLC0415 - psycopg ships in the optional postgres extra

    deadline = time.monotonic() + _QUEUE_TIMEOUT_SECONDS
    with psycopg.connect(database_url, autocommit=True) as connection:
        while not finished.is_set():
            row = connection.execute(_QUEUED_RACERS, (application_name,)).fetchone()
            if row is not None and int(row[0]) > 0:
                return
            if time.monotonic() > deadline:
                msg = "the second writer neither queued behind the first nor ran to completion"
                raise AssertionError(msg)
            time.sleep(_QUEUE_POLL_SECONDS)


class TestDeliveryRetirementAndEchoAreCrossProcessOrdered:
    """Retirement and projection share the room-membership row lock."""

    async def test_retirement_blocks_a_late_echo_until_the_tombstone_commits(
        self,
        rival_stores: RivalStores,
    ) -> None:
        """An echo queued behind retirement observes the tombstone and stays hidden."""
        principal_id = "agent@alice"
        first = rival_stores.first.principal(principal_id)
        second = rival_stores.second.principal(principal_id)
        await admit(first, "$target", sender="alice", content=text("Thinking..."), ts=1_000)
        assert (
            await first.enqueue_matrix_delivery(
                delivery_id="$turn",
                stage=DeliveryStage.FINAL,
                room_id=ROOM,
                thread_id=None,
                payload=edit("$target", "old answer"),
                edits_event_id="$target",
            )
            is not None
        )
        stored = await first.claim_matrix_delivery(delivery_id="$turn", stage=DeliveryStage.FINAL)
        assert stored is not None
        retirement_claimed = threading.Event()
        release_retirement = threading.Event()

        def pause_after_claim() -> None:
            retirement_claimed.set()
            assert release_retirement.wait(_WORKER_WAIT_SECONDS), "retirement was never released"

        retiring = EventJournalStore(
            backend=_PausingBackend(
                rival_stores.first.backend,
                pause_after_claim,
                statement_matches=lambda sql: "INSERT INTO room_membership" in sql,
            ),
        ).principal(principal_id)
        retirement = asyncio.create_task(
            retiring.retire_matrix_delivery(
                delivery_id="$turn",
                stage=DeliveryStage.FINAL,
                room_id=ROOM,
                membership_epoch=0,
            ),
        )
        await asyncio.to_thread(retirement_claimed.wait, _WORKER_WAIT_SECONDS)
        assert retirement_claimed.is_set(), "retirement never claimed the membership row"

        inbound, projected = message(
            "$physical-edit",
            sender="alice",
            content=stored.payload,
            event_class=EventClass.CONTEXT_ONLY,
            ts=2_000,
        )
        echo = asyncio.create_task(second.admit(inbound, projected))
        try:
            await _await_queued_racers(
                rival_stores.database_url,
                application_name=rival_stores.racer_application_name,
                expected=1,
            )
            assert not echo.done()
            release_retirement.set()
            retired, accepted = await asyncio.gather(retirement, echo)
        finally:
            release_retirement.set()
            await asyncio.gather(retirement, echo, return_exceptions=True)

        assert retired is None
        assert accepted is AdmissionResult.ADMITTED
        assert await bodies(first) == []


class TestInteractiveActivationAndDepartureAreCrossProcessOrdered:
    """Prompt activation and departure share one PostgreSQL membership-row lock."""

    async def test_departure_waits_for_a_current_turn_registration(
        self,
        rival_stores: RivalStores,
    ) -> None:
        """An activation that wins the row claim is subsequently removed by the fence."""
        principal_id = "agent@alice"
        first = rival_stores.first.principal(principal_id)
        second = rival_stores.second.principal(principal_id)
        await admit(first, "$turn")
        registration_claimed = threading.Event()
        release_registration = threading.Event()

        def pause_after_claim() -> None:
            registration_claimed.set()
            assert release_registration.wait(_WORKER_WAIT_SECONDS), "the registration was never released"

        registering = EventJournalStore(
            backend=_PausingBackend(
                rival_stores.first.backend,
                pause_after_claim,
                statement_matches=lambda sql: "INSERT INTO room_membership" in sql,
            ),
        ).principal(principal_id)
        inbound, projected = message(
            "$question",
            sender="alice",
            thread_id="$thread",
            content=interactive_prompt("Choose", "one", source_event_id="$turn"),
        )
        registration = asyncio.create_task(registering.admit(inbound, projected))
        try:
            await asyncio.to_thread(registration_claimed.wait, _WORKER_WAIT_SECONDS)
            assert registration_claimed.is_set(), "the registration never claimed the membership row"

            departure = asyncio.create_task(second.fence_departure(ROOM, source=DepartureSource.REPORTED))
            await _await_queued_racers(
                rival_stores.database_url,
                application_name=rival_stores.racer_application_name,
                expected=1,
            )
            assert not departure.done()

            release_registration.set()
            accepted, _ = await asyncio.gather(registration, departure)
        finally:
            release_registration.set()
            await asyncio.gather(registration, return_exceptions=True)

        assert accepted is AdmissionResult.ADMITTED
        assert await _interactive_question_rows(rival_stores.first) == []


class TestAFenceCannotBeSteppedOverByAConcurrentWalk:
    """What a hydrating writer and a fencing writer do to one PostgreSQL database.

    PostgreSQL only, for the reason the acknowledgement race is: SQLite holds a
    write lock across the whole transaction, so a second store over the same
    file is a second queue onto the same serialized write and there is no
    interleaving left to produce.
    """

    async def test_a_walk_that_read_the_epoch_first_cannot_outlive_the_fence(
        self,
        rival_stores: RivalStores,
    ) -> None:
        """A hydration's epoch decision has to be a claim, not an observation.

        `test_hydration_racing_a_rejoin_installs_nothing` proves the other half
        of this rule and cannot reach this one: by the time it installs, the
        fence has already committed, so the epoch it reads is the new one and
        any implementation declines. This half is what happens when the fence
        has not committed yet.

        Two writers on one database is the deployed shape, not a contrivance:
        `mindroom threads export` opens the running install's journal in its own
        process and runs its own `ConversationHydrator` against it, so the store
        lock that serializes writes inside one process orders nothing between
        them. Under ``READ COMMITTED`` the walk's plain epoch ``SELECT`` saw the
        membership it expected, the fence then deleted every row committed at
        that instant, and the walk's rows landed behind it -- a conversation
        from a membership the bot has left, projected under an epoch no reader
        of `visible_messages` filters on, and served into every prompt from then
        on. Nothing takes them back out: the walk that runs under the new
        membership projects over them with `ON CONFLICT DO NOTHING`, so the two
        memberships end up merged into one conversation.

        Modelled with two stores over one database rather than two operating
        system processes: what makes the race possible is two writer connections
        with no lock between them, which is exactly what two stores are. The
        window is opened by pausing the walk after its first statement -- the
        real transaction, the real SQL, only held open -- and the fence is a
        real `fence_departure` on the second store.
        """
        principal_id = "agent@alice"
        reader = rival_stores.first.principal(principal_id)
        fencing = rival_stores.second.principal(principal_id)
        inside_the_walk = threading.Event()
        fence_finished = threading.Event()

        def hold_the_walk_open() -> None:
            inside_the_walk.set()
            _watch_until_queued_or_finished(
                rival_stores.database_url,
                rival_stores.racer_application_name,
                fence_finished,
            )

        hydrating = EventJournalStore(
            backend=_PausingBackend(rival_stores.first.backend, hold_the_walk_open),
        ).principal(principal_id)
        epoch = await reader.membership_epoch(ROOM)

        walk = asyncio.create_task(
            hydrating.install_hydrated_conversation(
                room_id=ROOM,
                thread_id=None,
                events=(message("$resurrected")[1],),
                complete=True,
                expected_membership_epoch=epoch,
            ),
        )
        await asyncio.to_thread(inside_the_walk.wait, _WORKER_WAIT_SECONDS)
        assert inside_the_walk.is_set(), "the walk never reached its epoch decision"
        fence = asyncio.create_task(fencing.fence_departure(ROOM, source=DepartureSource.REPORTED))
        fence.add_done_callback(lambda _: fence_finished.set())
        installed, outcome = await asyncio.gather(walk, fence)

        assert not installed, "the final publication trusted the epoch the fence superseded"
        assert outcome.observation is DepartureObservation.FENCED
        assert outcome.membership_epoch == epoch + 1
        assert await bodies(reader) == [], "the walk's messages survived the departure that was supposed to erase them"
        assert not await reader.conversation_is_hydrated(room_id=ROOM, thread_id=None)


class TestRecoveryFinalizesOnlyItsExactObligation:
    """The final recovery commit cannot consume a later process's obligation."""

    async def test_replaced_recovery_is_locked_and_refused_before_publication(
        self,
        rival_stores: RivalStores,
    ) -> None:
        """A new gap waits behind the exact recovery claim and survives settlement."""
        principal_id = "agent@alice"
        reader = rival_stores.first.principal(principal_id)
        old_recovery = await reader.record_room_history_recovery(ROOM)
        assert old_recovery is not None
        inside_final = threading.Event()
        release_final = threading.Event()

        def pause_after_recovery_claim() -> None:
            inside_final.set()
            assert release_final.wait(_WORKER_WAIT_SECONDS), "the recovery transaction was never released"

        recovering = EventJournalStore(
            backend=_PausingBackend(
                rival_stores.first.backend,
                pause_after_recovery_claim,
                statement_matches=lambda sql: "UPDATE room_history_recovery SET state = state" in sql,
            ),
        ).principal(principal_id)
        settlement = asyncio.create_task(
            recovering.settle_room_history_recovery(
                old_recovery,
                exhausted_server=True,
                attempted_policy_rank=2,
                expected_membership_epoch=await reader.membership_epoch(ROOM),
            ),
        )
        await asyncio.to_thread(inside_final.wait, _WORKER_WAIT_SECONDS)
        assert inside_final.is_set(), "the repayment never claimed its exact recovery row"
        replacement = asyncio.create_task(
            rival_stores.second.principal(principal_id).record_room_history_recovery(ROOM),
        )
        try:
            await _await_queued_racers(
                rival_stores.database_url,
                application_name=rival_stores.racer_application_name,
                expected=1,
            )
        finally:
            release_final.set()

        outcome, newer_recovery = await asyncio.gather(settlement, replacement)

        assert outcome is HistoryRecoveryOutcome.REPAIRED
        assert newer_recovery is not None
        assert newer_recovery.revision == old_recovery.revision + 1
        assert await reader.room_history_recovery(ROOM) == newer_recovery
        assert not await reader.conversation_is_hydrated(room_id=ROOM, thread_id=None)


class TestOutbox:
    """Delivery survives a crash at every point around the network call."""

    async def test_a_losing_acknowledgement_writes_neither_the_row_nor_the_record(
        self,
        journal_store: EventJournalStore,
    ) -> None:
        """First-writer-wins has to cover the record travelling beside the row.

        The acknowledgement is the proof of which event is visible, and the
        terminal record is written from that same proof. If a second caller
        loses the row but still writes its record, the outbox names one event
        and the record names another -- which is the disagreement putting them
        in one transaction was supposed to make impossible.

        Against a real store on both backends, because the guard this pins is a
        property of the SQL statement. A fake outbox cannot observe it: the
        gateway tests inject one, the turn-store tests only build the record,
        and every one of them stays green with the guard removed.
        """
        alice = journal_store.principal("agent@alice")
        await alice.enqueue_matrix_delivery(
            delivery_id="turn-1",
            stage=DeliveryStage.FINAL,
            room_id=ROOM,
            thread_id=None,
            payload=text("answer"),
        )

        def record(event_id: str) -> TerminalTurnWrite:
            return TerminalTurnWrite(
                agent_name="general",
                index_event_ids=("$source",),
                anchor_event_id="$source",
                record_json=json.dumps({"response_event_id": event_id}),
            )

        await alice.acknowledge_matrix_delivery(
            delivery_id="turn-1",
            stage=DeliveryStage.FINAL,
            event_id="$first",
            delivered_projections=(),
            terminal_turn=record("$first"),
        )
        await alice.acknowledge_matrix_delivery(
            delivery_id="turn-1",
            stage=DeliveryStage.FINAL,
            event_id="$second",
            delivered_projections=(),
            terminal_turn=record("$second"),
        )

        stored = await alice.load_matrix_delivery(delivery_id="turn-1", stage=DeliveryStage.FINAL)
        assert stored is not None
        assert stored.acknowledged_event_id == "$first", "the row was rebound by a losing caller"
        rows = await journal_store.turn_records("general").load_all()
        assert [json.loads(row[2])["response_event_id"] for row in rows] == ["$first"], (
            "the losing caller rewrote the terminal record beside the row it did not win"
        )

    async def test_only_one_of_two_concurrent_claims_may_see_an_unattempted_row(
        self,
        journal_database: Callable[[], EventJournalStore],
    ) -> None:
        """Being told a row is unattempted is permission to send without asking.

        Delivery reads ``attempted`` off what the claim reports and treats a
        blank row as proof the homeserver has never seen this transaction ID, so
        it sends straight away instead of reading the room first. Tell two
        callers that and both send; if they are logged in as different devices
        the shared transaction ID deduplicates neither, and one message gets two
        visible answers.

        Reading the row and then marking it allowed exactly that. Against one
        PostgreSQL database both claims came back unattempted, every time.
        SQLite never showed it, because ``BEGIN IMMEDIATE`` holds a write lock
        across the whole transaction there -- which is why this runs on both.
        The guarantee has to come from the statement, not from one backend
        happening to serialize.

        The claimer that gets inside first waits for the other to reach its own
        first statement, which under a correct claim never happens: the other is
        blocked on the winner's row lock. So the wait is bounded and expiring is
        the fix working. A machine slow enough to expire it early could only make
        this test pass when it should have failed, never the reverse, because a
        claim that reports ownership from its own write has one winner at every
        interleaving.
        """
        principal = "agent@alice"
        first = journal_database()
        second = journal_database()
        await first.principal(principal).enqueue_matrix_delivery(
            delivery_id="turn-1",
            stage=DeliveryStage.FINAL,
            room_id=ROOM,
            thread_id=None,
            payload=text("answer"),
        )
        reached_first_statement = threading.Event()

        def contend(store: EventJournalStore, hook: Callable[[], object]) -> Awaitable[MatrixDelivery | None]:
            paused = EventJournalStore(backend=_PausingBackend(store.backend, hook))
            return paused.principal(principal).claim_matrix_delivery(delivery_id="turn-1", stage=DeliveryStage.FINAL)

        claims = await asyncio.gather(
            contend(first, lambda: reached_first_statement.wait(_CONTENDED_CLAIM_WAIT_SECONDS)),
            contend(second, reached_first_statement.set),
        )

        assert all(claimed is not None for claimed in claims)
        unattempted = [claimed for claimed in claims if claimed is not None and not claimed.attempted]
        assert len(unattempted) == 1, (
            f"{len(unattempted)} of two concurrent claims were told the row was unattempted, "
            "and every one of them would have sent without reading the room first"
        )

    async def test_two_connections_that_both_find_the_row_unbound_produce_one_winner(
        self,
        rival_stores: RivalStores,
    ) -> None:
        """Ownership of the acknowledgement has to come from the write itself.

        The sequential test above and this one prove different halves of the
        same rule and neither is redundant. That one acknowledges twice in a
        row, so the second caller meets a row that is already bound and is told
        so; what it proves is that being told costs it the terminal record too.
        It cannot prove this half, because by the time it runs there is no race
        left to lose -- the losing implementation below passes it untouched.

        This half is what happens when nobody has been told anything yet. Two
        connections against one PostgreSQL database can both observe an unbound
        row, and an implementation that reads the column and then updates it
        hands each of them a win: the outbox keeps whichever event landed last,
        while the caller that committed first goes on reporting its own. That
        report is what every downstream record is built from, so the outbox and
        the terminal record end up naming different events -- the disagreement
        both of these tests exist to forbid, arrived at from the side the
        sequential one cannot reach.

        Both racers are parked on the row before either may write, so the
        interleaving is produced rather than hoped for. Letting them start
        freely would usually have the second one read a row the first had
        already bound, and the broken implementation would decline correctly
        for a reason that says nothing about contention.
        """
        principal_id = "agent@alice"
        first = rival_stores.first.principal(principal_id)
        second = rival_stores.second.principal(principal_id)
        await first.enqueue_matrix_delivery(
            delivery_id="turn-1",
            stage=DeliveryStage.FINAL,
            room_id=ROOM,
            thread_id=None,
            payload=text("answer"),
        )

        async def acknowledge(store: PrincipalStore, event_id: str) -> DeliveryAcknowledgement:
            return await store.acknowledge_matrix_delivery(
                delivery_id="turn-1",
                stage=DeliveryStage.FINAL,
                event_id=event_id,
                delivered_projections=(),
                terminal_turn=TerminalTurnWrite(
                    agent_name="general",
                    index_event_ids=("$source",),
                    anchor_event_id="$source",
                    record_json=json.dumps({"response_event_id": event_id}),
                ),
            )

        with _outbox_row_held(rival_stores.database_url, principal_id=principal_id, turn_id="turn-1"):
            racers = [
                asyncio.create_task(acknowledge(first, "$first")),
                asyncio.create_task(acknowledge(second, "$second")),
            ]
            await _await_queued_racers(
                rival_stores.database_url,
                application_name=rival_stores.racer_application_name,
                expected=len(racers),
            )
        reported = await asyncio.gather(*racers)

        stored = await first.load_matrix_delivery(delivery_id="turn-1", stage=DeliveryStage.FINAL)
        assert stored is not None
        winner = stored.acknowledged_event_id
        assert winner in {"$first", "$second"}, "the row names an event neither caller sent"
        assert [acknowledged.settled_event_id for acknowledged in reported] == [winner, winner], (
            "a caller reported an event it did not bind the row to"
        )
        assert [acknowledged.bound for acknowledged in reported].count(True) == 1, (
            "both callers were told their own write is what bound the row"
        )
        rows = await rival_stores.first.turn_records("general").load_all()
        assert [json.loads(row[2])["response_event_id"] for row in rows] == [winner], (
            "the terminal record names an event the outbox row does not"
        )

    async def test_the_transaction_id_is_derived_not_random(self) -> None:
        """The transaction id is derived not random."""
        first = delivery_transaction_id("agent@alice", "turn-1", "final")
        second = delivery_transaction_id("agent@alice", "turn-1", "final")
        other_stage = delivery_transaction_id("agent@alice", "turn-1", "initial")

        assert first == second
        assert first != other_stage

    async def test_enqueue_returns_the_same_transaction_across_restarts(
        self,
        alice: PrincipalStore,
    ) -> None:
        """Enqueue returns the same transaction across restarts."""
        first = await alice.enqueue_matrix_delivery(
            delivery_id="turn-1",
            stage=DeliveryStage.FINAL,
            room_id=ROOM,
            thread_id=None,
            payload=text("answer"),
        )
        second = await alice.enqueue_matrix_delivery(
            delivery_id="turn-1",
            stage=DeliveryStage.FINAL,
            room_id=ROOM,
            thread_id=None,
            payload=text("answer"),
        )

        assert first == second

    async def test_an_unattempted_delivery_can_still_change(self, alice: PrincipalStore) -> None:
        """An unattempted delivery can still change."""
        await alice.enqueue_matrix_delivery(
            delivery_id="turn-1",
            stage=DeliveryStage.FINAL,
            room_id=ROOM,
            thread_id=None,
            payload=text("draft"),
        )
        await alice.enqueue_matrix_delivery(
            delivery_id="turn-1",
            stage=DeliveryStage.FINAL,
            room_id=ROOM,
            thread_id=None,
            payload=text("final"),
        )

        claimed = await alice.claim_matrix_delivery(delivery_id="turn-1", stage=DeliveryStage.FINAL)
        assert claimed is not None
        assert claimed.payload["body"] == "final"

    async def test_claiming_freezes_the_payload(self, alice: PrincipalStore) -> None:
        """Claiming freezes the payload.

        The case this closes: Matrix accepted the old text, and the regenerated
        text could never become visible under the same transaction ID.
        """
        await alice.enqueue_matrix_delivery(
            delivery_id="turn-1",
            stage=DeliveryStage.FINAL,
            room_id=ROOM,
            thread_id=None,
            payload=text("sent"),
        )
        await alice.claim_matrix_delivery(delivery_id="turn-1", stage=DeliveryStage.FINAL)

        await alice.enqueue_matrix_delivery(
            delivery_id="turn-1",
            stage=DeliveryStage.FINAL,
            room_id=ROOM,
            thread_id=None,
            payload=text("regenerated"),
        )

        stored = await alice.load_matrix_delivery(delivery_id="turn-1", stage=DeliveryStage.FINAL)
        assert stored is not None
        assert stored.payload["body"] == "sent"

    async def test_reclaiming_sends_the_identical_delivery(self, alice: PrincipalStore) -> None:
        """Everything that goes on the wire is frozen; the claim state is not.

        The payload and the transaction ID are the reason claiming exists, and
        a second claim must reproduce them exactly. The attempt and the device
        are the opposite: they describe who took the row and from where, so the
        second claim reports the first one's work rather than repeating the
        blank state it started from.
        """
        await alice.enqueue_matrix_delivery(
            delivery_id="turn-1",
            stage=DeliveryStage.FINAL,
            room_id=ROOM,
            thread_id=None,
            payload=text("sent"),
        )
        first = await alice.claim_matrix_delivery(
            delivery_id="turn-1",
            stage=DeliveryStage.FINAL,
            sending_device_id="DEVICE1",
        )
        second = await alice.claim_matrix_delivery(
            delivery_id="turn-1",
            stage=DeliveryStage.FINAL,
            sending_device_id="DEVICE2",
        )
        assert first is not None
        assert second is not None

        assert replace(first, attempted=True) == second
        assert first.payload == second.payload
        assert first.transaction_id == second.transaction_id

        assert not first.attempted
        assert first.sending_device_id == "DEVICE1"
        assert second.attempted
        assert second.sending_device_id == "DEVICE1"

    async def test_unacknowledged_matrix_deliveries_are_replayable(self, alice: PrincipalStore) -> None:
        """Unacknowledged deliveries are replayable."""
        await alice.enqueue_matrix_delivery(
            delivery_id="turn-1",
            stage=DeliveryStage.FINAL,
            room_id=ROOM,
            thread_id=None,
            payload=text("sent"),
        )
        await alice.claim_matrix_delivery(delivery_id="turn-1", stage=DeliveryStage.FINAL)

        assert [d.delivery_id for d in await alice.unacknowledged_matrix_deliveries()] == ["turn-1"]

        await alice.acknowledge_matrix_delivery(
            delivery_id="turn-1",
            stage=DeliveryStage.FINAL,
            event_id="$sent",
            delivered_projections=(),
        )

        assert await alice.unacknowledged_matrix_deliveries() == ()

    async def test_acknowledgement_keeps_the_first_event_id(self, alice: PrincipalStore) -> None:
        """Acknowledgement keeps the first event id."""
        await alice.enqueue_matrix_delivery(
            delivery_id="turn-1",
            stage=DeliveryStage.FINAL,
            room_id=ROOM,
            thread_id=None,
            payload=text("sent"),
        )
        await alice.claim_matrix_delivery(delivery_id="turn-1", stage=DeliveryStage.FINAL)
        await alice.acknowledge_matrix_delivery(
            delivery_id="turn-1",
            stage=DeliveryStage.FINAL,
            event_id="$first",
            delivered_projections=(),
        )
        await alice.acknowledge_matrix_delivery(
            delivery_id="turn-1",
            stage=DeliveryStage.FINAL,
            event_id="$second",
            delivered_projections=(),
        )

        stored = await alice.load_matrix_delivery(delivery_id="turn-1", stage=DeliveryStage.FINAL)
        assert stored is not None
        assert stored.acknowledged_event_id == "$first"


class TestApprovalContinuations:
    """A paused Agno run remains owned by its original journal sources."""

    @staticmethod
    def continuation(*, state: str = "ready") -> ApprovalContinuation:
        """Return one exact paused-run owner."""
        return ApprovalContinuation(
            approval_id="approval-1",
            run_id="run-1",
            session_id="session-1",
            entity_kind="agent",
            entity_name="agent",
            room_id=ROOM,
            thread_id="$thread",
            requester_id=ALICE,
            response_event_id="$waiting",
            source_event_ids=("$source-1", "$source-2"),
            calls=(
                ApprovalCall(
                    tool_call_id="call-1",
                    tool_name="shell",
                    invoking_agent="agent",
                    expires_at_ns=time.time_ns() + 60_000_000_000,
                    decision=ApprovalDecision.APPROVED if state == "ready" else None,
                    human_approval_required=True,
                ),
            ),
            request_body="run it",
            state=state,
        )

    @staticmethod
    async def admit_sources(store: PrincipalStore) -> None:
        """Admit the two sources one coalesced approval turn owns."""
        await admit(store, "$source-1", ts=1_001)
        await admit(store, "$source-2", ts=1_002)

    @staticmethod
    async def remember_card(
        store: PrincipalStore,
        *,
        tool_call_id: str = "call-1",
        continuation_principal_id: str = "agent@alice",
    ) -> None:
        """Persist one current-format card for the first continuation generation."""
        reserved = await store.reserve_approval_card_deliveries(
            continuation_principal_id=continuation_principal_id,
            continuation_id="approval-1",
            expected_generation=0,
            cards=(
                ApprovalCardReservation(
                    delivery_id="approval-card-1",
                    tool_call_id=tool_call_id,
                    event_type="io.mindroom.tool_approval",
                    payload={
                        "approval_id": "approval-card-1",
                        "continuation_id": "approval-1",
                        "continuation_generation": 0,
                        "tool_call_id": tool_call_id,
                        "status": "pending",
                    },
                ),
            ),
        )
        assert reserved
        assert (
            await store.claim_matrix_delivery(
                delivery_id="approval-card-1",
                stage=DeliveryStage.INITIAL,
            )
            is not None
        )
        await store.record_matrix_delivery_device(
            delivery_id="approval-card-1",
            stage=DeliveryStage.INITIAL,
            device_id=DEVICE,
        )
        await store.acknowledge_matrix_delivery(
            delivery_id="approval-card-1",
            stage=DeliveryStage.INITIAL,
            event_id="$approval",
            delivered_projections=(),
        )

    async def test_continuation_is_reachable_from_every_owned_source(self, alice: PrincipalStore) -> None:
        """A coalesced source cannot replay outside its one paused-run owner."""
        await self.admit_sources(alice)
        continuation = self.continuation()

        created = await alice.create_approval_continuation(continuation)

        assert created == continuation
        assert await alice.approval_continuation_for_source("$source-1") == continuation
        assert await alice.approval_continuation_for_source("$source-2") == continuation

    async def test_continuation_round_trips_committed_presentation_and_visibility(
        self,
        alice: PrincipalStore,
    ) -> None:
        """A restart restores the exact pause presentation without consulting current config."""
        await self.admit_sources(alice)
        continuation = replace(
            self.continuation(),
            response_text="Before.\n\n🔧 `inspect` [1] ⏳",
            visible_response_text="Visible before approval.",
            response_tool_trace=(
                {
                    "type": "tool_call_started",
                    "tool_name": "inspect",
                    "tool_call_id": "call-1",
                },
            ),
            response_presentation_state={"kind": "team", "members": {"GeneralAgent": "Before."}},
            show_tool_calls=False,
        )

        await alice.create_approval_continuation(continuation)
        restored = await alice.approval_continuation("approval-1")

        assert restored == continuation
        assert restored is not None
        assert restored.response_presentation_state == {
            "kind": "team",
            "members": {"GeneralAgent": "Before."},
        }
        assert restored.visible_response_text == "Visible before approval."
        assert restored.presentation_version == 1
        assert restored.show_tool_calls is False

    async def test_ready_continuation_has_one_claim_winner(self, alice: PrincipalStore) -> None:
        """Only one response lifecycle may continue the exact persisted Agno run."""
        await self.admit_sources(alice)
        await alice.create_approval_continuation(self.continuation())

        winner = await alice.claim_approval_continuation("approval-1", runtime_generation="runtime-a")
        loser = await alice.claim_approval_continuation("approval-1", runtime_generation="runtime-b")

        assert winner is not None
        assert winner.state == "claimed"
        assert winner.runtime_generation == "runtime-a"
        assert loser is None

    async def test_pending_page_exposes_only_runnable_primary_source(self, alice: PrincipalStore) -> None:
        """Waiting and live claims stay hidden while ready and old claims re-enter once."""
        await self.admit_sources(alice)
        waiting = self.continuation(state="waiting")
        await alice.create_approval_continuation(waiting)

        assert list(await alice.pending(runtime_generation="runtime-a")) == []

        await alice.request_approval_failure(
            waiting.approval_id,
            "make it runnable",
            expected_state="waiting",
        )
        failing = await alice.pending(runtime_generation="runtime-a")
        assert [event.event_id for event in failing] == ["$source-1"]

    async def test_pending_card_page_exposes_unreadable_durable_debt(self, alice: PrincipalStore) -> None:
        """A corrupt card stays visible to recovery accounting without becoming actionable."""
        await self.admit_sources(alice)
        await alice.create_approval_continuation(
            replace(self.continuation(state="waiting"), runtime_generation="runtime-a"),
        )
        await self.remember_card(alice)
        await alice._backend.write(
            lambda transaction: transaction.execute(
                """
                UPDATE matrix_delivery_outbox SET payload_json = ?
                WHERE principal_id = ? AND delivery_id = ? AND stage = 'initial'
                """,
                (
                    json.dumps(
                        {
                            "approval_id": "approval-card-1",
                            "continuation_id": "approval-1",
                            "continuation_generation": 0,
                            "tool_call_id": "different-call",
                            "status": "pending",
                        },
                    ),
                    alice.principal_id,
                    "approval-card-1",
                ),
            ),
        )

        page = await alice.pending_approval_cards(room_id=ROOM)

        assert len(page) == 1
        unreadable = page[0]
        assert isinstance(unreadable, UnreadableApprovalCard)
        assert unreadable.delivery_id == "approval-card-1"
        assert unreadable.continuation_id == "approval-1"
        continuation = await alice.approval_continuation("approval-1")
        assert continuation is not None
        assert continuation.state == "waiting"

    async def test_abandoned_publication_exposes_its_primary_source_for_failure_cleanup(
        self,
        alice: PrincipalStore,
    ) -> None:
        """A crash before every card is durable cannot hide the paused source forever."""
        await self.admit_sources(alice)
        await alice.create_approval_continuation(
            replace(self.continuation(state="waiting"), runtime_generation="runtime-a"),
        )

        recovered = await alice.pending(runtime_generation="runtime-b")

        assert [event.event_id for event in recovered] == ["$source-1"]

    async def test_current_claim_is_hidden_and_old_runtime_claim_is_recoverable(self, alice: PrincipalStore) -> None:
        """A restart recovers delivery debt without replaying the coalesced source twice."""
        await self.admit_sources(alice)
        await alice.create_approval_continuation(self.continuation())
        await alice.claim_approval_continuation("approval-1", runtime_generation="runtime-a")

        assert list(await alice.pending(runtime_generation="runtime-a")) == []
        recovered = await alice.pending(runtime_generation="runtime-b")
        assert [event.event_id for event in recovered] == ["$source-1"]

    async def test_current_claim_with_final_delivery_debt_is_recoverable(self, alice: PrincipalStore) -> None:
        """A transient FINAL failure re-enters only to reconcile its frozen outbox payload."""
        await self.admit_sources(alice)
        await alice.create_approval_continuation(self.continuation())
        await alice.claim_approval_continuation("approval-1", runtime_generation="runtime-a")
        await alice.enqueue_matrix_delivery(
            delivery_id="$source-1",
            stage=DeliveryStage.FINAL,
            room_id=ROOM,
            thread_id="$thread",
            payload=text("finished"),
        )

        recovered = await alice.pending(runtime_generation="runtime-a")

        assert [event.event_id for event in recovered] == ["$source-1"]

    async def test_claimed_continuation_advances_only_from_its_current_generation(
        self,
        alice: PrincipalStore,
    ) -> None:
        """A stale lifecycle cannot replace a newer chained approval pause."""
        await self.admit_sources(alice)
        await alice.create_approval_continuation(self.continuation())
        await alice.claim_approval_continuation("approval-1", runtime_generation="runtime-a")
        calls = (
            ApprovalCall(
                tool_call_id="call-2",
                tool_name="write_file",
                invoking_agent="agent",
                expires_at_ns=time.time_ns() + 60_000_000_000,
            ),
        )

        stale = await alice.advance_approval_continuation(
            "approval-1",
            claimant_generation=1,
            run_id="run-2",
            session_id="session-1",
            calls=calls,
        )
        advanced = await alice.advance_approval_continuation(
            "approval-1",
            claimant_generation=0,
            run_id="run-2",
            session_id="session-1",
            calls=calls,
            response_text="Before.\n\n🔧 `write_file` [2] ⏳",
            response_tool_trace=(
                {
                    "type": "tool_call_started",
                    "tool_name": "write_file",
                    "tool_call_id": "call-2",
                },
            ),
            response_presentation_state={"kind": "team", "consensus": "Before."},
        )

        assert stale is None
        assert advanced is not None
        assert advanced.state == "waiting"
        assert advanced.generation == 1
        assert advanced.run_id == "run-2"
        assert advanced.runtime_generation == "runtime-a"
        assert advanced.calls == calls
        assert advanced.response_text.endswith("🔧 `write_file` [2] ⏳")
        assert advanced.response_tool_trace[-1]["tool_call_id"] == "call-2"
        assert advanced.response_presentation_state == {"kind": "team", "consensus": "Before."}

    async def test_every_card_is_reserved_before_publication_activates(
        self,
        journal_store: EventJournalStore,
    ) -> None:
        """One commit owns every exact call and its frozen Matrix delivery."""
        responder = journal_store.principal("agent@alice")
        router = journal_store.principal("router")
        await self.admit_sources(responder)
        second_call = ApprovalCall(
            tool_call_id="call-2",
            tool_name="write_file",
            invoking_agent="agent",
            expires_at_ns=time.time_ns() + 60_000_000_000,
        )
        publishing = replace(
            self.continuation(state="waiting"),
            calls=(*self.continuation(state="waiting").calls, second_call),
            runtime_generation="runtime-a",
        )
        await responder.create_approval_continuation(publishing)

        reserved = await router.reserve_approval_card_deliveries(
            continuation_principal_id="agent@alice",
            continuation_id=publishing.approval_id,
            expected_generation=0,
            cards=(
                ApprovalCardReservation(
                    delivery_id="approval-card-1",
                    tool_call_id="call-1",
                    event_type="io.mindroom.tool_approval",
                    payload={
                        "approval_id": "approval-card-1",
                        "continuation_id": publishing.approval_id,
                        "continuation_generation": publishing.generation,
                        "tool_call_id": "call-1",
                        "status": "pending",
                    },
                ),
                ApprovalCardReservation(
                    delivery_id="approval-card-2",
                    tool_call_id="call-2",
                    event_type="io.mindroom.tool_approval",
                    payload={
                        "approval_id": "approval-card-2",
                        "continuation_id": publishing.approval_id,
                        "continuation_generation": publishing.generation,
                        "tool_call_id": "call-2",
                        "status": "pending",
                    },
                ),
            ),
        )

        assert reserved is True
        activated = await responder.approval_continuation(publishing.approval_id)
        assert activated is not None
        assert activated.state == "waiting"
        assert activated.runtime_generation is None
        first = await router.load_matrix_delivery(
            delivery_id="approval-card-1",
            stage=DeliveryStage.INITIAL,
        )
        second = await router.load_matrix_delivery(
            delivery_id="approval-card-2",
            stage=DeliveryStage.INITIAL,
        )
        assert first is not None
        assert first.event_type == "io.mindroom.tool_approval"
        assert second is not None
        assert second.event_type == "io.mindroom.tool_approval"

    async def test_card_reservation_fails_closed_without_every_exact_call(
        self,
        journal_store: EventJournalStore,
    ) -> None:
        """A malformed multi-card batch owns no delivery and leaves publication fenced."""
        responder = journal_store.principal("agent@alice")
        router = journal_store.principal("router")
        await self.admit_sources(responder)
        publishing = replace(
            self.continuation(state="waiting"),
            runtime_generation="runtime-a",
        )
        await responder.create_approval_continuation(publishing)

        reserved = await router.reserve_approval_card_deliveries(
            continuation_principal_id="agent@alice",
            continuation_id=publishing.approval_id,
            expected_generation=0,
            cards=(
                ApprovalCardReservation(
                    delivery_id="approval-card-1",
                    tool_call_id="call-1",
                    event_type="io.mindroom.tool_approval",
                    payload={"approval_id": "approval-card-1", "status": "pending"},
                ),
                ApprovalCardReservation(
                    delivery_id="approval-card-2",
                    tool_call_id="missing-call",
                    event_type="io.mindroom.tool_approval",
                    payload={"approval_id": "approval-card-2", "status": "pending"},
                ),
            ),
        )

        assert reserved is False
        retained = await responder.approval_continuation(publishing.approval_id)
        assert retained is not None
        assert retained.runtime_generation == "runtime-a"
        assert (
            await router.load_matrix_delivery(
                delivery_id="approval-card-1",
                stage=DeliveryStage.INITIAL,
            )
            is None
        )
        assert (
            await router.load_matrix_delivery(
                delivery_id="approval-card-2",
                stage=DeliveryStage.INITIAL,
            )
            is None
        )

    async def test_card_reservation_rejects_payload_identity_that_disagrees_with_its_owner(
        self,
        journal_store: EventJournalStore,
    ) -> None:
        """A clickable payload cannot name a different exact call than its domain row."""
        responder = journal_store.principal("agent@alice")
        router = journal_store.principal("router@shared")
        await self.admit_sources(responder)
        publishing = replace(self.continuation(state="waiting"), runtime_generation="runtime-a")
        await responder.create_approval_continuation(publishing)

        with pytest.raises(ValueError, match="changed exact-call identity"):
            await router.reserve_approval_card_deliveries(
                continuation_principal_id=responder.principal_id,
                continuation_id=publishing.approval_id,
                expected_generation=publishing.generation,
                cards=(
                    ApprovalCardReservation(
                        delivery_id="approval-card-1",
                        tool_call_id="call-1",
                        event_type="io.mindroom.tool_approval",
                        payload={
                            "approval_id": "approval-card-1",
                            "continuation_id": publishing.approval_id,
                            "continuation_generation": publishing.generation,
                            "tool_call_id": "different-call",
                            "status": "pending",
                        },
                    ),
                ),
            )

        retained = await responder.approval_continuation(publishing.approval_id)
        assert retained is not None
        assert retained.runtime_generation == "runtime-a"
        assert (
            await router.load_matrix_delivery(
                delivery_id="approval-card-1",
                stage=DeliveryStage.INITIAL,
            )
            is None
        )

    async def test_card_reservation_after_transport_departure_keeps_the_pause_recoverable(
        self,
        journal_store: EventJournalStore,
    ) -> None:
        """A departed sender cannot reserve cards after its cleanup fence already ran."""
        responder = journal_store.principal("agent@alice")
        router = journal_store.principal("router@shared")
        await self.admit_sources(responder)
        await responder.create_approval_continuation(
            replace(self.continuation(state="waiting"), runtime_generation="runtime-a"),
        )
        await router.fence_departure(ROOM, source=DepartureSource.REPORTED)

        reserved = await router.reserve_approval_card_deliveries(
            continuation_principal_id="agent@alice",
            continuation_id="approval-1",
            expected_generation=0,
            cards=(
                ApprovalCardReservation(
                    delivery_id="approval-card-1",
                    tool_call_id="call-1",
                    event_type="io.mindroom.tool_approval",
                    payload={
                        "continuation_id": "approval-1",
                        "continuation_generation": 0,
                        "tool_call_id": "call-1",
                        "status": "pending",
                    },
                ),
            ),
        )

        assert reserved is False
        assert (
            await router.load_matrix_delivery(
                delivery_id="approval-card-1",
                stage=DeliveryStage.INITIAL,
            )
            is None
        )
        continuation = await responder.approval_continuation("approval-1")
        assert continuation is not None
        assert continuation.runtime_generation == "runtime-a"
        assert [event.event_id for event in await responder.pending(runtime_generation="runtime-b")] == ["$source-1"]

    async def test_card_reservation_serializes_with_a_cross_process_failure_fence(
        self,
        rival_stores: RivalStores,
    ) -> None:
        """Publication and failure choose one continuation state before delivery rows commit."""
        principal_id = "agent@alice"
        responder = rival_stores.first.principal(principal_id)
        await self.admit_sources(responder)
        await responder.create_approval_continuation(
            replace(self.continuation(state="waiting"), runtime_generation="runtime-a"),
        )
        reservation_read = threading.Event()
        release_reservation = threading.Event()
        failure_finished = threading.Event()

        def pause_after_continuation_read() -> None:
            reservation_read.set()
            assert release_reservation.wait(_WORKER_WAIT_SECONDS), "the reservation was never released"

        reserving = EventJournalStore(
            backend=_PausingBackend(
                rival_stores.first.backend,
                pause_after_continuation_read,
                statement_matches=lambda sql: "SELECT" in sql and "FROM approval_continuations" in sql,
            ),
        ).principal("router@shared")
        reservation = asyncio.create_task(
            reserving.reserve_approval_card_deliveries(
                continuation_principal_id=principal_id,
                continuation_id="approval-1",
                expected_generation=0,
                cards=(
                    ApprovalCardReservation(
                        delivery_id="approval-card-1",
                        tool_call_id="call-1",
                        event_type="io.mindroom.tool_approval",
                        payload={
                            "approval_id": "approval-card-1",
                            "continuation_id": "approval-1",
                            "continuation_generation": 0,
                            "tool_call_id": "call-1",
                            "status": "pending",
                        },
                    ),
                ),
            ),
        )
        try:
            await asyncio.to_thread(reservation_read.wait, _WORKER_WAIT_SECONDS)
            assert reservation_read.is_set(), "the reservation never read its continuation"
            failure = asyncio.create_task(
                rival_stores.second.principal(principal_id).request_approval_failure(
                    "approval-1",
                    "publication failed",
                    expected_state="waiting",
                    expected_generation=0,
                    expected_runtime_generation="runtime-a",
                ),
            )
            failure.add_done_callback(lambda _: failure_finished.set())
            await asyncio.to_thread(
                _watch_until_queued_or_finished,
                rival_stores.database_url,
                rival_stores.racer_application_name,
                failure_finished,
            )
        finally:
            release_reservation.set()

        reserved, failed = await asyncio.gather(reservation, failure)

        assert reserved
        assert failed is None
        current = await responder.approval_continuation("approval-1")
        assert current is not None
        assert current.state == "waiting"
        assert current.runtime_generation is None
        assert (
            await rival_stores.first.principal("router@shared").load_matrix_delivery(
                delivery_id="approval-card-1",
                stage=DeliveryStage.INITIAL,
            )
            is not None
        )

    async def test_exact_call_decision_serializes_with_a_cross_process_failure_fence(
        self,
        rival_stores: RivalStores,
    ) -> None:
        """A click and failure fence cannot commit contradictory continuation facts."""
        principal_id = "agent@alice"
        responder = rival_stores.first.principal(principal_id)
        router = rival_stores.first.principal("router@shared")
        await self.admit_sources(responder)
        await responder.create_approval_continuation(
            replace(self.continuation(state="waiting"), runtime_generation="runtime-a"),
        )
        await self.remember_card(router)
        decision_read = threading.Event()
        release_decision = threading.Event()
        failure_finished = threading.Event()

        def pause_after_continuation_read() -> None:
            decision_read.set()
            assert release_decision.wait(_WORKER_WAIT_SECONDS), "the decision was never released"

        deciding = EventJournalStore(
            backend=_PausingBackend(
                rival_stores.first.backend,
                pause_after_continuation_read,
                statement_matches=lambda sql: (
                    "SELECT principal_id, entity_name, state, generation, failure_reason" in sql
                ),
            ),
        ).principal("router@shared")
        decision = asyncio.create_task(
            deciding.resolve_continuation_approval_card(
                card_event_id="$approval",
                requested_status="approved",
                reason=None,
                resolution={"status": "approved", "body": "Approved: shell"},
            ),
        )
        try:
            await asyncio.to_thread(decision_read.wait, _WORKER_WAIT_SECONDS)
            assert decision_read.is_set(), "the decision never read its continuation"
            failure = asyncio.create_task(
                rival_stores.second.principal(principal_id).request_approval_failure(
                    "approval-1",
                    "response failed",
                    expected_state="waiting",
                    expected_generation=0,
                    expected_runtime_generation=None,
                ),
            )
            failure.add_done_callback(lambda _: failure_finished.set())
            await asyncio.to_thread(
                _watch_until_queued_or_finished,
                rival_stores.database_url,
                rival_stores.racer_application_name,
                failure_finished,
            )
        finally:
            release_decision.set()

        recorded, failed = await asyncio.gather(decision, failure)

        assert recorded.recorded
        assert recorded.resolution is not None
        assert recorded.resolution["status"] == "approved"
        assert recorded.continuation_ready
        assert failed is None
        current = await responder.approval_continuation("approval-1")
        assert current is not None
        assert current.state == "ready"
        assert current.calls[0].decision is ApprovalDecision.APPROVED

    async def test_exact_call_decision_serializes_with_responder_departure(
        self,
        rival_stores: RivalStores,
    ) -> None:
        """A click and responder departure lock continuation before card delivery."""
        responder = rival_stores.first.principal("agent@alice")
        router = rival_stores.first.principal("router@shared")
        await self.admit_sources(responder)
        await responder.create_approval_continuation(
            replace(self.continuation(state="waiting"), runtime_generation="runtime-a"),
        )
        await self.remember_card(router)
        continuation_locked = threading.Event()
        release_decision = threading.Event()
        departure_finished = threading.Event()

        def pause_after_continuation_lock() -> None:
            continuation_locked.set()
            assert release_decision.wait(_WORKER_WAIT_SECONDS), "the decision was never released"

        deciding = EventJournalStore(
            backend=_PausingBackend(
                rival_stores.first.backend,
                pause_after_continuation_lock,
                statement_matches=lambda sql: (
                    "UPDATE approval_continuations SET state = state WHERE approval_id" in sql
                ),
            ),
        ).principal("router@shared")
        decision = asyncio.create_task(
            deciding.resolve_continuation_approval_card(
                card_event_id="$approval",
                requested_status="approved",
                reason=None,
                resolution={"status": "approved", "body": "Approved: shell"},
            ),
        )
        try:
            await asyncio.to_thread(continuation_locked.wait, _WORKER_WAIT_SECONDS)
            assert continuation_locked.is_set(), "the decision never locked its continuation"
            departure = asyncio.create_task(
                rival_stores.second.principal("agent@alice").fence_departure(
                    ROOM,
                    source=DepartureSource.REPORTED,
                ),
            )
            departure.add_done_callback(lambda _: departure_finished.set())
            await asyncio.to_thread(
                _watch_until_queued_or_finished,
                rival_stores.database_url,
                rival_stores.racer_application_name,
                departure_finished,
            )
        finally:
            release_decision.set()

        recorded, departed = await asyncio.gather(decision, departure)

        assert recorded.recorded
        assert recorded.resolution is not None
        assert recorded.resolution["status"] == "approved"
        assert departed.fenced
        assert await responder.approval_continuation("approval-1") is None
        terminal = await router.load_matrix_delivery(
            delivery_id="approval-card-1",
            stage=DeliveryStage.FINAL,
        )
        assert terminal is not None
        assert terminal.payload["status"] == "approved"

    async def test_failure_request_is_guarded_by_observed_state(self, alice: PrincipalStore) -> None:
        """A stale failure observer cannot fence work that already made progress."""
        await self.admit_sources(alice)
        await alice.create_approval_continuation(self.continuation())

        stale = await alice.request_approval_failure(
            "approval-1",
            "stale recovery",
            expected_state="waiting",
        )
        failing = await alice.request_approval_failure(
            "approval-1",
            "entity is unavailable",
            expected_state="ready",
        )

        assert stale is None
        assert failing is not None
        assert failing.state == "failing"
        assert failing.failure_reason == "entity is unavailable"

    async def test_failure_request_cannot_fence_a_claim_after_final_delivery_is_enqueued(
        self,
        alice: PrincipalStore,
    ) -> None:
        """The successful FINAL debt atomically outranks a concurrent failure request."""
        await self.admit_sources(alice)
        await alice.create_approval_continuation(self.continuation())
        claimed = await alice.claim_approval_continuation("approval-1", runtime_generation="runtime-a")
        assert claimed is not None
        await alice.enqueue_matrix_delivery(
            delivery_id="$source-1",
            stage=DeliveryStage.FINAL,
            room_id=ROOM,
            thread_id="$thread",
            payload={"msgtype": "m.text", "body": "finished"},
        )

        refused = await alice.request_approval_failure(
            "approval-1",
            "entity is unavailable",
            expected_state="claimed",
            expected_generation=claimed.generation,
            expected_runtime_generation="runtime-a",
        )

        assert refused is None
        retained = await alice.approval_continuation("approval-1")
        assert retained is not None
        assert retained.state == "claimed"

    async def test_card_decision_atomically_readies_the_exact_call(self, alice: PrincipalStore) -> None:
        """The card and final call decision become durable in one transaction."""
        await self.admit_sources(alice)
        await alice.create_approval_continuation(
            replace(self.continuation(state="waiting"), runtime_generation="runtime-a"),
        )
        await self.remember_card(alice)

        recorded = await alice.resolve_continuation_approval_card(
            card_event_id="$approval",
            requested_status="approved",
            reason="Looks safe.",
            resolution={"status": "approved", "resolution_reason": "Looks safe."},
        )

        assert recorded.recorded is True
        assert recorded.resolution == {"status": "approved", "resolution_reason": "Looks safe."}
        assert recorded.continuation_ready is True
        assert recorded.source_event_ids == ("$source-1", "$source-2")
        continuation = await alice.approval_continuation_for_source("$source-1")
        assert continuation is not None
        assert continuation.state == "ready"
        assert continuation.calls[0].decision is ApprovalDecision.APPROVED
        assert continuation.calls[0].reason == "Looks safe."

        terminal = await alice.load_matrix_delivery(
            delivery_id="approval-card-1",
            stage=DeliveryStage.FINAL,
        )
        assert terminal is not None
        assert terminal.event_type == "io.mindroom.tool_approval"
        assert terminal.edits_event_id == "$approval"
        assert terminal.payload == {
            "status": "approved",
            "resolution_reason": "Looks safe.",
            "io.mindroom.delivery_id": {
                "principal": "agent@alice",
                "delivery_id": "approval-card-1",
                "stage": "final",
            },
        }
        assert terminal.attempted is False

    async def test_terminal_edit_acknowledgement_retires_card_and_completed_delivery(
        self,
        alice: PrincipalStore,
    ) -> None:
        """The tombstone replaces completed approval-domain and transport ownership."""
        await self.admit_sources(alice)
        await alice.create_approval_continuation(
            replace(self.continuation(state="waiting"), runtime_generation="runtime-a"),
        )
        await self.remember_card(alice)
        await alice.resolve_continuation_approval_card(
            card_event_id="$approval",
            requested_status="denied",
            reason="Unsafe.",
            resolution={"status": "denied", "resolution_reason": "Unsafe."},
        )

        assert (
            await alice.retire_approval_card(
                delivery_id="approval-card-1",
                card_event_id="$approval",
            )
            is False
        )
        assert (
            await alice.claim_matrix_delivery(
                delivery_id="approval-card-1",
                stage=DeliveryStage.FINAL,
            )
            is not None
        )
        await alice.acknowledge_matrix_delivery(
            delivery_id="approval-card-1",
            stage=DeliveryStage.FINAL,
            event_id="$terminal-edit",
            delivered_projections=(),
        )

        assert (
            await alice.retire_approval_card(
                delivery_id="approval-card-1",
                card_event_id="$approval",
            )
            is True
        )
        assert await alice.pending_approval_card(room_id=ROOM, card_event_id="$approval") is None
        assert await alice.is_terminal_approval_card(room_id=ROOM, card_event_id="$approval") is True
        initial = await alice.load_matrix_delivery(
            delivery_id="approval-card-1",
            stage=DeliveryStage.INITIAL,
        )
        terminal = await alice.load_matrix_delivery(
            delivery_id="approval-card-1",
            stage=DeliveryStage.FINAL,
        )
        assert initial is None
        assert terminal is None

    async def test_late_approval_atomically_expires_the_call_and_card(self, alice: PrincipalStore) -> None:
        """A click at or after the exact deadline cannot authorize execution."""
        await self.admit_sources(alice)
        expired = replace(
            self.continuation(state="waiting"),
            calls=(replace(self.continuation(state="waiting").calls[0], expires_at_ns=1),),
            runtime_generation="runtime-a",
        )
        await alice.create_approval_continuation(expired)
        await self.remember_card(alice)

        recorded = await alice.resolve_continuation_approval_card(
            card_event_id="$approval",
            requested_status="approved",
            reason=None,
            resolution={"status": "approved", "body": "Approved: shell", "resolved_by": ALICE},
        )

        assert recorded.recorded is True
        assert recorded.resolution == {
            "status": "expired",
            "body": "Expired: shell",
            "resolution_reason": "Tool approval request timed out.",
            "resolved_by": None,
        }
        continuation = await alice.approval_continuation_for_source("$source-1")
        assert continuation is not None
        assert continuation.state == "ready"
        assert continuation.calls[0].decision is ApprovalDecision.EXPIRED

    async def test_late_denial_atomically_expires_the_call_and_visible_card(self, alice: PrincipalStore) -> None:
        """A denial crossing the exact deadline must display the durable expiry winner."""
        await self.admit_sources(alice)
        expired = replace(
            self.continuation(state="waiting"),
            calls=(replace(self.continuation(state="waiting").calls[0], expires_at_ns=1),),
            runtime_generation="runtime-a",
        )
        await alice.create_approval_continuation(expired)
        await self.remember_card(alice)

        recorded = await alice.resolve_continuation_approval_card(
            card_event_id="$approval",
            requested_status="denied",
            reason="Unsafe.",
            resolution={"status": "denied", "body": "Denied: shell", "resolved_by": ALICE},
        )

        assert recorded.recorded is True
        assert recorded.resolution == {
            "status": "expired",
            "body": "Expired: shell",
            "resolution_reason": "Tool approval request timed out.",
            "resolved_by": None,
        }

    async def test_unacknowledged_card_deadline_expires_without_abandoning_unknown_send(
        self,
        journal_store: EventJournalStore,
        alice: PrincipalStore,
    ) -> None:
        """An old-device history miss cannot strand the call or authorize a duplicate card."""
        router = journal_store.principal("router@shared")
        await self.admit_sources(alice)
        expired = replace(
            self.continuation(state="waiting"),
            calls=(replace(self.continuation(state="waiting").calls[0], expires_at_ns=1),),
            runtime_generation="runtime-a",
        )
        await alice.create_approval_continuation(expired)
        assert await router.reserve_approval_card_deliveries(
            continuation_principal_id="agent@alice",
            continuation_id="approval-1",
            expected_generation=0,
            cards=(
                ApprovalCardReservation(
                    delivery_id="approval-card-1",
                    tool_call_id="call-1",
                    event_type="io.mindroom.tool_approval",
                    payload={
                        "approval_id": "approval-card-1",
                        "continuation_id": "approval-1",
                        "continuation_generation": 0,
                        "tool_call_id": "call-1",
                        "tool_name": "shell",
                        "status": "pending",
                    },
                ),
            ),
        )
        assert await router.claim_matrix_delivery(
            delivery_id="approval-card-1",
            stage=DeliveryStage.INITIAL,
            sending_device_id="OLDDEVICE",
        )

        recorded = await router.expire_unacknowledged_approval_card(delivery_id="approval-card-1")

        assert recorded.recorded is True
        assert recorded.resolution is not None
        assert recorded.resolution["status"] == "expired"
        continuation = await alice.approval_continuation("approval-1")
        assert continuation is not None
        assert continuation.state == "ready"
        assert continuation.calls[0].decision is ApprovalDecision.EXPIRED
        initial = await router.load_matrix_delivery(
            delivery_id="approval-card-1",
            stage=DeliveryStage.INITIAL,
        )
        assert initial is not None
        assert initial.attempted is True
        assert initial.sending_device_id == "OLDDEVICE"
        assert initial.acknowledged_event_id is None
        assert (
            await router.claim_matrix_delivery(
                delivery_id="approval-card-1",
                stage=DeliveryStage.FINAL,
                sending_device_id="NEWDEVICE",
            )
            is None
        )

        await router.acknowledge_matrix_delivery(
            delivery_id="approval-card-1",
            stage=DeliveryStage.INITIAL,
            event_id="$approval",
            delivered_projections=(),
        )
        terminal = await router.claim_matrix_delivery(
            delivery_id="approval-card-1",
            stage=DeliveryStage.FINAL,
            sending_device_id="NEWDEVICE",
        )
        assert terminal is not None
        assert terminal.edits_event_id == "$approval"

    async def test_approval_cannot_authorize_a_failure_fenced_continuation(self, alice: PrincipalStore) -> None:
        """A late click terminalizes the card but cannot approve work fenced for failure."""
        await self.admit_sources(alice)
        waiting = replace(self.continuation(state="waiting"), runtime_generation="runtime-a")
        await alice.create_approval_continuation(waiting)
        await self.remember_card(alice)
        failing = await alice.request_approval_failure(
            waiting.approval_id,
            "Approval publication failed safely.",
            expected_state="waiting",
        )
        assert failing is not None

        recorded = await alice.resolve_continuation_approval_card(
            card_event_id="$approval",
            requested_status="approved",
            reason=None,
            resolution={"status": "approved", "body": "Approved: shell", "resolved_by": ALICE},
        )

        assert recorded.recorded is True
        assert recorded.continuation_ready is False
        assert recorded.resolution == {
            "status": "denied",
            "body": "Denied: shell",
            "resolution_reason": "Approval publication failed safely.",
            "resolved_by": None,
        }
        continuation = await alice.approval_continuation_for_source("$source-1")
        assert continuation is not None
        assert continuation.state == "failing"
        assert continuation.calls[0].decision is ApprovalDecision.DENIED
        assert continuation.calls[0].reason == "Approval publication failed safely."

    async def test_duplicate_card_decision_preserves_the_first_winner(self, alice: PrincipalStore) -> None:
        """A later reaction can redeliver but never reverse the stored decision."""
        await self.admit_sources(alice)
        await alice.create_approval_continuation(
            replace(self.continuation(state="waiting"), runtime_generation="runtime-a"),
        )
        await self.remember_card(alice)
        await alice.resolve_continuation_approval_card(
            card_event_id="$approval",
            requested_status="approved",
            reason=None,
            resolution={"status": "approved"},
        )

        duplicate = await alice.resolve_continuation_approval_card(
            card_event_id="$approval",
            requested_status="denied",
            reason="Changed my mind.",
            resolution={"status": "denied", "resolution_reason": "Changed my mind."},
        )

        assert duplicate.recorded is False
        assert duplicate.resolution == {"status": "approved"}

    async def test_finish_requires_acknowledged_final_before_releasing_sources(
        self,
        alice: PrincipalStore,
    ) -> None:
        """A paused run cannot disappear before its frozen final answer is visible."""
        await self.admit_sources(alice)
        await alice.create_approval_continuation(self.continuation())
        await alice.claim_approval_continuation("approval-1", runtime_generation="runtime-a")

        assert await alice.finish_approval_continuation("approval-1") is False
        assert await alice.is_pending("$source-1")
        assert await alice.is_pending("$source-2")

        await alice.enqueue_matrix_delivery(
            delivery_id="$source-1",
            stage=DeliveryStage.FINAL,
            room_id=ROOM,
            thread_id="$thread",
            payload=text("finished"),
        )
        await alice.claim_matrix_delivery(delivery_id="$source-1", stage=DeliveryStage.FINAL)
        await alice.acknowledge_matrix_delivery(
            delivery_id="$source-1",
            stage=DeliveryStage.FINAL,
            event_id="$finished",
            delivered_projections=(),
        )

        assert await alice.finish_approval_continuation("approval-1") is True
        assert await alice.approval_continuation_for_source("$source-1") is None
        assert not await alice.is_pending("$source-1")
        assert not await alice.is_pending("$source-2")

    async def test_finish_serializes_with_responder_departure(
        self,
        rival_stores: RivalStores,
    ) -> None:
        """Terminal completion locks its continuation before settling sources."""
        responder = rival_stores.first.principal("agent@alice")
        await self.admit_sources(responder)
        await responder.create_approval_continuation(self.continuation())
        await responder.claim_approval_continuation("approval-1", runtime_generation="runtime-a")
        await responder.enqueue_matrix_delivery(
            delivery_id="$source-1",
            stage=DeliveryStage.FINAL,
            room_id=ROOM,
            thread_id="$thread",
            payload=text("finished"),
        )
        await responder.claim_matrix_delivery(delivery_id="$source-1", stage=DeliveryStage.FINAL)
        await responder.acknowledge_matrix_delivery(
            delivery_id="$source-1",
            stage=DeliveryStage.FINAL,
            event_id="$finished",
            delivered_projections=(),
        )
        source_settled = threading.Event()
        release_finish = threading.Event()
        departure_finished = threading.Event()

        def pause_after_source_settlement() -> None:
            source_settled.set()
            assert release_finish.wait(_WORKER_WAIT_SECONDS), "continuation completion was never released"

        finishing = EventJournalStore(
            backend=_PausingBackend(
                rival_stores.first.backend,
                pause_after_source_settlement,
                statement_matches=lambda sql: "UPDATE journal_events" in sql and "event_id =" in sql,
            ),
        ).principal("agent@alice")
        finish = asyncio.create_task(finishing.finish_approval_continuation("approval-1"))
        try:
            await asyncio.to_thread(source_settled.wait, _WORKER_WAIT_SECONDS)
            assert source_settled.is_set(), "completion never settled its first source"
            departure = asyncio.create_task(
                rival_stores.second.principal("agent@alice").fence_departure(
                    ROOM,
                    source=DepartureSource.REPORTED,
                ),
            )
            departure.add_done_callback(lambda _: departure_finished.set())
            await asyncio.to_thread(
                _watch_until_queued_or_finished,
                rival_stores.database_url,
                rival_stores.racer_application_name,
                departure_finished,
            )
        finally:
            release_finish.set()

        completed, departed = await asyncio.gather(finish, departure)

        assert completed
        assert departed.fenced
        assert await responder.approval_continuation("approval-1") is None
        assert not await responder.is_pending("$source-1")
        assert not await responder.is_pending("$source-2")

    async def test_permanently_unavailable_owner_can_discard_fenced_sources(
        self,
        journal_store: EventJournalStore,
        alice: PrincipalStore,
    ) -> None:
        """Permanent-unavailability cleanup requires its durable terminal notice."""
        await self.admit_sources(alice)
        await alice.create_approval_continuation(self.continuation(state="waiting"))

        owners = await journal_store.approval_continuations_for_entities({"agent"})
        assert [(principal, continuation.approval_id) for principal, continuation in owners] == [
            ("agent@alice", "approval-1"),
        ]

        failing = await alice.request_approval_failure(
            "approval-1",
            "agent removed",
            expected_state="waiting",
        )
        assert failing is not None
        assert (
            await alice.discard_unavailable_approval_continuation(
                "approval-1",
                notice_principal_id="router@alice",
            )
            is False
        )
        assert await alice.is_pending("$source-1")
        assert await alice.is_pending("$source-2")

        router = journal_store.principal("router@alice")
        delivery_id = await router.enqueue_unavailable_approval_notice(
            approval_id="approval-1",
            room_id=ROOM,
            thread_id="$thread",
            payload=text("agent removed"),
        )
        assert delivery_id == "approval-unavailable:approval-1:0"
        await router.claim_matrix_delivery(
            delivery_id=delivery_id,
            stage=DeliveryStage.FINAL,
        )
        await router.acknowledge_matrix_delivery(
            delivery_id=delivery_id,
            stage=DeliveryStage.FINAL,
            event_id="$unavailable",
            delivered_projections=(),
        )

        assert (
            await alice.discard_unavailable_approval_continuation(
                "approval-1",
                notice_principal_id="router@alice",
            )
            is True
        )
        assert await alice.approval_continuation("approval-1") is None
        assert not await alice.is_pending("$source-1")
        assert not await alice.is_pending("$source-2")

    async def test_unavailable_cleanup_and_router_departure_share_membership_first_lock_order(
        self,
        rival_stores: RivalStores,
    ) -> None:
        """Cross-principal cleanup cannot deadlock router departure."""
        responder = rival_stores.first.principal("agent@alice")
        router = rival_stores.first.principal("router@alice")
        await self.admit_sources(responder)
        await responder.create_approval_continuation(
            replace(self.continuation(state="waiting"), runtime_generation="runtime-a"),
        )
        await self.remember_card(router)
        assert (
            await responder.request_approval_failure(
                "approval-1",
                "agent removed",
                expected_state="waiting",
                expected_runtime_generation=None,
            )
            is not None
        )
        delivery_id = await router.enqueue_unavailable_approval_notice(
            approval_id="approval-1",
            room_id=ROOM,
            thread_id="$thread",
            payload=text("agent removed"),
        )
        assert delivery_id is not None
        assert await router.claim_matrix_delivery(delivery_id=delivery_id, stage=DeliveryStage.FINAL) is not None
        await router.acknowledge_matrix_delivery(
            delivery_id=delivery_id,
            stage=DeliveryStage.FINAL,
            event_id="$unavailable",
            delivered_projections=(),
        )

        membership_locked = threading.Event()
        release_departure = threading.Event()
        cleanup_finished = threading.Event()

        def pause_after_membership_lock() -> None:
            membership_locked.set()
            assert release_departure.wait(_WORKER_WAIT_SECONDS), "router departure was never released"

        departing = EventJournalStore(
            backend=_PausingBackend(
                rival_stores.second.backend,
                pause_after_membership_lock,
                statement_matches=lambda sql: "INSERT INTO room_membership" in sql,
            ),
        ).principal("router@alice")
        departure = asyncio.create_task(departing.fence_departure(ROOM, source=DepartureSource.LOCAL))
        try:
            await asyncio.to_thread(membership_locked.wait, _WORKER_WAIT_SECONDS)
            assert membership_locked.is_set(), "departure never locked router membership"
            cleanup = asyncio.create_task(
                responder.discard_unavailable_approval_continuation(
                    "approval-1",
                    notice_principal_id="router@alice",
                ),
            )
            cleanup.add_done_callback(lambda _: cleanup_finished.set())
            await asyncio.to_thread(
                _watch_until_queued_or_finished,
                rival_stores.database_url,
                rival_stores.racer_application_name,
                cleanup_finished,
            )
        finally:
            release_departure.set()

        departed, discarded = await asyncio.gather(departure, cleanup)

        assert departed.fenced
        assert not discarded

    async def test_stale_unavailable_notice_cannot_discard_sources(
        self,
        journal_store: EventJournalStore,
        alice: PrincipalStore,
    ) -> None:
        """An acknowledged notice must still belong to the router's active membership."""
        await self.admit_sources(alice)
        await alice.create_approval_continuation(self.continuation(state="waiting"))
        assert (
            await alice.request_approval_failure(
                "approval-1",
                "agent removed",
                expected_state="waiting",
            )
            is not None
        )

        router = journal_store.principal("router@alice")
        delivery_id = "approval-unavailable:approval-1:0"
        assert (
            await router.enqueue_matrix_delivery(
                delivery_id=delivery_id,
                stage=DeliveryStage.FINAL,
                room_id=ROOM,
                thread_id="$thread",
                payload=text("agent removed"),
            )
            is not None
        )
        assert (
            await router.claim_matrix_delivery(
                delivery_id=delivery_id,
                stage=DeliveryStage.FINAL,
            )
            is not None
        )
        await router.fence_departure(ROOM, source=DepartureSource.LOCAL)
        await router.note_membership_restarted(ROOM)
        await router.acknowledge_matrix_delivery(
            delivery_id=delivery_id,
            stage=DeliveryStage.FINAL,
            event_id="$stale-unavailable",
            delivered_projections=(),
        )

        assert (
            await alice.discard_unavailable_approval_continuation(
                "approval-1",
                notice_principal_id="router@alice",
            )
            is False
        )
        assert await alice.approval_continuation("approval-1") is not None
        assert await alice.is_pending("$source-1")
        assert await alice.is_pending("$source-2")

    async def test_stale_unavailable_notice_can_retry_under_current_membership(
        self,
        journal_store: EventJournalStore,
        alice: PrincipalStore,
    ) -> None:
        """A stale physical attempt must not strand its live logical notice obligation."""
        await self.admit_sources(alice)
        await alice.create_approval_continuation(self.continuation(state="waiting"))
        assert (
            await alice.request_approval_failure(
                "approval-1",
                "agent removed",
                expected_state="waiting",
            )
            is not None
        )

        router = journal_store.principal("router@alice")
        stale_delivery_id = await router.enqueue_unavailable_approval_notice(
            approval_id="approval-1",
            room_id=ROOM,
            thread_id="$thread",
            payload=text("agent removed"),
        )
        assert stale_delivery_id == "approval-unavailable:approval-1:0"
        assert (
            await router.claim_matrix_delivery(
                delivery_id=stale_delivery_id,
                stage=DeliveryStage.FINAL,
            )
            is not None
        )

        await router.fence_departure(ROOM, source=DepartureSource.LOCAL)
        await router.note_membership_restarted(ROOM)
        await router.acknowledge_matrix_delivery(
            delivery_id=stale_delivery_id,
            stage=DeliveryStage.FINAL,
            event_id="$stale-unavailable",
            delivered_projections=(),
        )
        assert (
            await alice.discard_unavailable_approval_continuation(
                "approval-1",
                notice_principal_id="router@alice",
            )
            is False
        )

        current_delivery_id = await router.enqueue_unavailable_approval_notice(
            approval_id="approval-1",
            room_id=ROOM,
            thread_id="$thread",
            payload=text("agent removed"),
        )
        assert current_delivery_id == "approval-unavailable:approval-1:1"
        assert current_delivery_id != stale_delivery_id
        assert (
            await router.claim_matrix_delivery(
                delivery_id=current_delivery_id,
                stage=DeliveryStage.FINAL,
            )
            is not None
        )
        await router.acknowledge_matrix_delivery(
            delivery_id=current_delivery_id,
            stage=DeliveryStage.FINAL,
            event_id="$current-unavailable",
            delivered_projections=(),
        )

        assert (
            await alice.discard_unavailable_approval_continuation(
                "approval-1",
                notice_principal_id="router@alice",
            )
            is True
        )
        assert await alice.approval_continuation("approval-1") is None
        assert not await alice.is_pending("$source-1")
        assert not await alice.is_pending("$source-2")

    async def test_continuation_owner_scans_are_cursor_paginated(
        self,
        journal_store: EventJournalStore,
        alice: PrincipalStore,
    ) -> None:
        """Unavailable-owner scans visit a bounded page and resume after its final approval."""
        for index in range(5):
            source_event_id = f"$page-source-{index}"
            await admit(alice, source_event_id, ts=2_000 + index)
            continuation = replace(
                self.continuation(state="waiting"),
                approval_id=f"approval-page-{index}",
                entity_name="removed" if index != 2 else "configured",
                source_event_ids=(source_event_id,),
            )
            assert await alice.create_approval_continuation(continuation) == continuation

        first = await journal_store.approval_continuations(limit=2)
        second = await journal_store.approval_continuations(
            limit=2,
            after=(first[-1][1].entity_name, first[-1][1].approval_id),
        )
        third = await journal_store.approval_continuations(
            limit=2,
            after=(second[-1][1].entity_name, second[-1][1].approval_id),
        )
        assert [continuation.approval_id for _principal, continuation in first] == [
            "approval-page-2",
            "approval-page-0",
        ]
        assert [continuation.approval_id for _principal, continuation in second] == [
            "approval-page-1",
            "approval-page-3",
        ]
        assert [continuation.approval_id for _principal, continuation in third] == ["approval-page-4"]

        removed_first = await journal_store.approval_continuations_for_entities(
            {"removed"},
            limit=2,
        )
        removed_second = await journal_store.approval_continuations_for_entities(
            {"removed"},
            limit=2,
            after=(removed_first[-1][1].entity_name, removed_first[-1][1].approval_id),
        )
        assert [continuation.approval_id for _principal, continuation in removed_first] == [
            "approval-page-0",
            "approval-page-1",
        ]
        assert [continuation.approval_id for _principal, continuation in removed_second] == [
            "approval-page-3",
            "approval-page-4",
        ]

    async def test_room_departure_discards_continuation_and_cards_with_its_sources(
        self,
        alice: PrincipalStore,
    ) -> None:
        """A membership fence cannot leave a continuation pointing at settled room work."""
        await self.admit_sources(alice)
        await alice.create_approval_continuation(
            replace(self.continuation(state="waiting"), runtime_generation="runtime-a"),
        )
        await self.remember_card(alice)

        await alice.fence_departure(ROOM, source=DepartureSource.REPORTED)

        assert await alice.approval_continuation("approval-1") is None
        assert not await alice.is_pending("$source-1")
        assert not await alice.is_pending("$source-2")
        assert await alice.pending_approval_cards(room_id=ROOM) == ()

    async def test_room_departure_terminalizes_router_owned_cards_before_discarding_the_continuation(
        self,
        journal_store: EventJournalStore,
        alice: PrincipalStore,
    ) -> None:
        """The responder's membership fence must preserve the router's visible card cleanup debt."""
        router = journal_store.principal("router@shared")
        await self.admit_sources(alice)
        await alice.create_approval_continuation(
            replace(self.continuation(state="waiting"), runtime_generation="runtime-a"),
        )
        await self.remember_card(router)

        await alice.fence_departure(ROOM, source=DepartureSource.REPORTED)

        assert await alice.approval_continuation("approval-1") is None
        stored = await router.pending_approval_card(room_id=ROOM, card_event_id="$approval")
        assert stored is not None
        assert stored.resolution is not None
        assert stored.resolution["status"] == "expired"
        assert stored.resolution["resolution_reason"] == "Requesting agent left the room."

    async def test_room_departure_retires_router_card_after_terminal_ack_crash(
        self,
        journal_store: EventJournalStore,
        alice: PrincipalStore,
    ) -> None:
        """A responder departure preserves a terminal card whose domain retirement crashed."""
        router = journal_store.principal("router@shared")
        await self.admit_sources(alice)
        await alice.create_approval_continuation(
            replace(self.continuation(state="waiting"), runtime_generation="runtime-a"),
        )
        await self.remember_card(router)
        await router.resolve_continuation_approval_card(
            card_event_id="$approval",
            requested_status="denied",
            reason="Unsafe.",
            resolution={"status": "denied", "resolution_reason": "Unsafe."},
        )
        assert await router.claim_matrix_delivery(
            delivery_id="approval-card-1",
            stage=DeliveryStage.FINAL,
        )
        await router.acknowledge_matrix_delivery(
            delivery_id="approval-card-1",
            stage=DeliveryStage.FINAL,
            event_id="$terminal-edit",
            delivered_projections=(),
        )

        await alice.fence_departure(ROOM, source=DepartureSource.REPORTED)

        assert await alice.approval_continuation("approval-1") is None
        assert await router.pending_approval_cards(room_id=ROOM) == ()
        assert await router.is_terminal_approval_card(room_id=ROOM, card_event_id="$approval") is True

    async def test_room_departure_retires_router_cards_that_were_never_attempted(
        self,
        journal_store: EventJournalStore,
        alice: PrincipalStore,
    ) -> None:
        """A provably invisible card cannot turn into a standalone terminal event after departure."""
        router = journal_store.principal("router@shared")
        await self.admit_sources(alice)
        await alice.create_approval_continuation(
            replace(self.continuation(state="waiting"), runtime_generation="runtime-a"),
        )
        assert await router.reserve_approval_card_deliveries(
            continuation_principal_id="agent@alice",
            continuation_id="approval-1",
            expected_generation=0,
            cards=(
                ApprovalCardReservation(
                    delivery_id="approval-card-1",
                    tool_call_id="call-1",
                    event_type="io.mindroom.tool_approval",
                    payload={
                        "approval_id": "approval-card-1",
                        "continuation_id": "approval-1",
                        "continuation_generation": 0,
                        "tool_call_id": "call-1",
                        "status": "pending",
                    },
                ),
            ),
        )

        await alice.fence_departure(ROOM, source=DepartureSource.REPORTED)

        assert await alice.approval_continuation("approval-1") is None
        assert await router.pending_approval_cards(room_id=ROOM) == ()
        assert (
            await router.load_matrix_delivery(
                delivery_id="approval-card-1",
                stage=DeliveryStage.INITIAL,
            )
            is None
        )
        assert (
            await router.load_matrix_delivery(
                delivery_id="approval-card-1",
                stage=DeliveryStage.FINAL,
            )
            is None
        )

    async def test_responder_departure_discards_a_predecided_card_that_never_became_visible(
        self,
        journal_store: EventJournalStore,
        alice: PrincipalStore,
    ) -> None:
        """A reserved INITIAL and FINAL that Matrix never saw cannot outlive their continuation."""
        router = journal_store.principal("router@shared")
        await self.admit_sources(alice)
        await alice.create_approval_continuation(
            replace(self.continuation(state="waiting"), runtime_generation="runtime-a"),
        )
        assert await router.reserve_approval_card_deliveries(
            continuation_principal_id="agent@alice",
            continuation_id="approval-1",
            expected_generation=0,
            cards=(
                ApprovalCardReservation(
                    delivery_id="approval-card-1",
                    tool_call_id="call-1",
                    event_type="io.mindroom.tool_approval",
                    payload={
                        "approval_id": "approval-card-1",
                        "continuation_id": "approval-1",
                        "continuation_generation": 0,
                        "tool_call_id": "call-1",
                        "status": "pending",
                    },
                ),
            ),
        )
        await router.enqueue_matrix_delivery(
            delivery_id="approval-card-1",
            stage=DeliveryStage.FINAL,
            event_type="io.mindroom.tool_approval",
            room_id=ROOM,
            thread_id="$thread",
            payload={"status": "expired"},
        )

        await alice.fence_departure(ROOM, source=DepartureSource.REPORTED)

        assert await alice.approval_continuation("approval-1") is None
        assert await router.pending_approval_cards(room_id=ROOM) == ()
        assert (
            await router.load_matrix_delivery(
                delivery_id="approval-card-1",
                stage=DeliveryStage.INITIAL,
            )
            is None
        )
        assert (
            await router.load_matrix_delivery(
                delivery_id="approval-card-1",
                stage=DeliveryStage.FINAL,
            )
            is None
        )

    async def test_initial_acknowledgement_binds_a_precommitted_terminal_edit(
        self,
        journal_store: EventJournalStore,
        alice: PrincipalStore,
    ) -> None:
        """A decision committed during an unknown send outcome still edits that exact card."""
        router = journal_store.principal("router@shared")
        await self.admit_sources(alice)
        await alice.create_approval_continuation(
            replace(self.continuation(state="waiting"), runtime_generation="runtime-a"),
        )
        assert await router.reserve_approval_card_deliveries(
            continuation_principal_id="agent@alice",
            continuation_id="approval-1",
            expected_generation=0,
            cards=(
                ApprovalCardReservation(
                    delivery_id="approval-card-1",
                    tool_call_id="call-1",
                    event_type="io.mindroom.tool_approval",
                    payload={
                        "approval_id": "approval-card-1",
                        "continuation_id": "approval-1",
                        "continuation_generation": 0,
                        "tool_call_id": "call-1",
                        "tool_name": "shell",
                        "status": "pending",
                    },
                ),
            ),
        )
        assert await router.claim_matrix_delivery(
            delivery_id="approval-card-1",
            stage=DeliveryStage.INITIAL,
        )
        await router.record_matrix_delivery_device(
            delivery_id="approval-card-1",
            stage=DeliveryStage.INITIAL,
            device_id=DEVICE,
        )

        await alice.fence_departure(ROOM, source=DepartureSource.REPORTED)

        terminal = await router.load_matrix_delivery(
            delivery_id="approval-card-1",
            stage=DeliveryStage.FINAL,
        )
        assert terminal is not None
        assert terminal.edits_event_id is None

        await router.acknowledge_matrix_delivery(
            delivery_id="approval-card-1",
            stage=DeliveryStage.INITIAL,
            event_id="$approval",
            delivered_projections=(),
        )

        bound = await router.load_matrix_delivery(
            delivery_id="approval-card-1",
            stage=DeliveryStage.FINAL,
        )
        assert bound is not None
        assert bound.edits_event_id == "$approval"

    async def test_router_departure_wakes_the_responder_continuation_to_fail_closed(
        self,
        journal_store: EventJournalStore,
        alice: PrincipalStore,
    ) -> None:
        """Deleting router-owned cards must not leave their responder-owned pause hidden forever."""
        router = journal_store.principal("router@shared")
        await self.admit_sources(alice)
        await alice.create_approval_continuation(
            replace(self.continuation(state="waiting"), runtime_generation="runtime-a"),
        )
        await self.remember_card(router)

        await router.fence_departure(ROOM, source=DepartureSource.REPORTED)

        continuation = await alice.approval_continuation("approval-1")
        assert continuation is not None
        assert continuation.state == "failing"
        assert continuation.failure_reason == "Approval transport left the room."
        assert continuation.calls[0].decision is ApprovalDecision.DENIED
        assert continuation.calls[0].reason == "Approval transport left the room."
        assert [event.event_id for event in await alice.pending(runtime_generation="replacement-runtime")] == [
            "$source-1",
        ]
        assert await router.pending_approval_cards(room_id=ROOM) == ()
        initial = await router.load_matrix_delivery(
            delivery_id="approval-card-1",
            stage=DeliveryStage.INITIAL,
        )
        terminal = await router.load_matrix_delivery(
            delivery_id="approval-card-1",
            stage=DeliveryStage.FINAL,
        )
        assert initial is not None
        assert initial.acknowledged_event_id == "$approval"
        assert terminal is not None
        assert terminal.edits_event_id == "$approval"
        assert terminal.payload["status"] == "denied"
        assert terminal.payload["resolution_reason"] == "Approval transport left the room."

    async def test_card_owner_departure_retires_card_after_terminal_ack_crash(
        self,
        alice: PrincipalStore,
    ) -> None:
        """A card-owner departure completes domain retirement after Matrix already acknowledged it."""
        await self.admit_sources(alice)
        await alice.create_approval_continuation(
            replace(self.continuation(state="waiting"), runtime_generation="runtime-a"),
        )
        await self.remember_card(alice)
        await alice.resolve_continuation_approval_card(
            card_event_id="$approval",
            requested_status="denied",
            reason="Unsafe.",
            resolution={"status": "denied", "resolution_reason": "Unsafe."},
        )
        assert await alice.claim_matrix_delivery(
            delivery_id="approval-card-1",
            stage=DeliveryStage.FINAL,
        )
        await alice.acknowledge_matrix_delivery(
            delivery_id="approval-card-1",
            stage=DeliveryStage.FINAL,
            event_id="$terminal-edit",
            delivered_projections=(),
        )

        await alice.fence_departure(ROOM, source=DepartureSource.REPORTED)

        assert await alice.pending_approval_cards(room_id=ROOM) == ()
        assert await alice.is_terminal_approval_card(room_id=ROOM, card_event_id="$approval") is True

    async def test_card_owner_departure_keeps_a_precommitted_terminal_edit_recoverable_after_rejoin(
        self,
        journal_store: EventJournalStore,
        alice: PrincipalStore,
    ) -> None:
        """A committed exact-call decision retains visible cleanup ownership across membership epochs."""
        router = journal_store.principal("router@shared")
        await self.admit_sources(alice)
        await alice.create_approval_continuation(
            replace(self.continuation(state="waiting"), runtime_generation="runtime-a"),
        )
        await self.remember_card(router)
        recorded = await router.resolve_continuation_approval_card(
            card_event_id="$approval",
            requested_status="denied",
            reason="Unsafe.",
            resolution={"status": "denied", "resolution_reason": "Unsafe."},
        )
        assert recorded.recorded is True
        assert await router.claim_matrix_delivery(
            delivery_id="approval-card-1",
            stage=DeliveryStage.FINAL,
            sending_device_id="OLD-DEVICE",
        )

        await router.fence_departure(ROOM, source=DepartureSource.REPORTED)
        await router.note_membership_restarted(ROOM)

        sent: list[MatrixDelivery] = []
        resolved: list[MatrixDelivery] = []

        async def send(delivery: MatrixDelivery) -> str:
            sent.append(delivery)
            return "$terminal-edit"

        async def resolve(delivery: MatrixDelivery) -> str | None:
            resolved.append(delivery)
            return None

        worker = MatrixDeliveryWorker(
            store=router,
            send=send,
            event_type="io.mindroom.tool_approval",
            resend_after_reconciliation_miss=False,
            sending_device_id="NEW-DEVICE",
            resolve_delivered=resolve,
        )

        assert await worker.flush(delivery_id="approval-card-1", stage=DeliveryStage.FINAL) == "$terminal-edit"
        assert len(resolved) == 1
        assert len(sent) == 1

        stored = await router.pending_approval_card(room_id=ROOM, card_event_id="$approval")
        assert stored is not None
        assert stored.resolution == {"status": "denied", "resolution_reason": "Unsafe."}
        initial = await router.load_matrix_delivery(
            delivery_id="approval-card-1",
            stage=DeliveryStage.INITIAL,
        )
        final = await router.load_matrix_delivery(
            delivery_id="approval-card-1",
            stage=DeliveryStage.FINAL,
        )
        assert initial is not None
        assert final is not None
        current_epoch = await router.membership_epoch(ROOM)
        assert initial.membership_epoch == current_epoch
        assert final.membership_epoch == current_epoch
        assert final.acknowledged_event_id == "$terminal-edit"
        continuation = await alice.approval_continuation("approval-1")
        assert continuation is not None
        assert continuation.calls[0].decision is ApprovalDecision.DENIED

    async def test_router_departure_denies_a_card_that_was_never_attempted(
        self,
        journal_store: EventJournalStore,
        alice: PrincipalStore,
    ) -> None:
        """An invisible card is retired while its exact call still records the fail-closed decision."""
        router = journal_store.principal("router@shared")
        await self.admit_sources(alice)
        await alice.create_approval_continuation(
            replace(self.continuation(state="waiting"), runtime_generation="runtime-a"),
        )
        assert await router.reserve_approval_card_deliveries(
            continuation_principal_id="agent@alice",
            continuation_id="approval-1",
            expected_generation=0,
            cards=(
                ApprovalCardReservation(
                    delivery_id="approval-card-1",
                    tool_call_id="call-1",
                    event_type="io.mindroom.tool_approval",
                    payload={
                        "approval_id": "approval-card-1",
                        "continuation_id": "approval-1",
                        "continuation_generation": 0,
                        "tool_call_id": "call-1",
                        "status": "pending",
                    },
                ),
            ),
        )

        await router.fence_departure(ROOM, source=DepartureSource.REPORTED)

        continuation = await alice.approval_continuation("approval-1")
        assert continuation is not None
        assert continuation.state == "failing"
        assert continuation.calls[0].decision is ApprovalDecision.DENIED
        assert continuation.calls[0].reason == "Approval transport left the room."
        assert await router.pending_approval_cards(room_id=ROOM) == ()
        assert (
            await router.load_matrix_delivery(
                delivery_id="approval-card-1",
                stage=DeliveryStage.INITIAL,
            )
            is None
        )
        assert (
            await router.load_matrix_delivery(
                delivery_id="approval-card-1",
                stage=DeliveryStage.FINAL,
            )
            is None
        )


class TestConcurrency:
    """Concurrent conversations must not produce lock failures."""

    async def test_fifty_concurrent_conversations_admit_cleanly(
        self,
        alice: PrincipalStore,
    ) -> None:
        """Fifty concurrent conversations admit cleanly."""

        async def conversation(index: int) -> None:
            for step in range(10):
                inbound, projected = message(
                    f"$c{index:02d}-{step}",
                    ts=1_000 + step,
                    thread_id=f"$thread-{index:02d}",
                )
                await alice.admit(inbound, projected)

        await asyncio.gather(*(conversation(index) for index in range(50)))

        for index in range(50):
            assert len(await bodies(alice, thread_id=f"$thread-{index:02d}")) == 10

    async def test_concurrent_admissions_of_one_event_yield_one_pending(
        self,
        alice: PrincipalStore,
    ) -> None:
        """Concurrent admissions of one event yield one pending."""
        inbound, projected = message("$contended")
        results = await asyncio.gather(*(alice.admit(inbound, projected) for _ in range(8)))

        assert results.count(AdmissionResult.ADMITTED) == 1
        assert [event.event_id for event in await alice.pending()] == ["$contended"]


class TestOffloadedStatementsOutliveTheAwaitThatStartedThem:
    """A cancelled await cannot stop a worker thread, so it must not hand on what that thread is using.

    Every backend statement runs on an ``asyncio.to_thread`` worker no
    cancellation can reach. What each rule below pins is one thing the await
    was holding while the thread ran -- the writer lock, a pooled connection,
    a connection about to be closed -- and that none of them may change hands
    until the statement has actually stopped.

    Nothing here substitutes anything for ``to_thread``: the crash these guard
    against is a real connection being taken away from a real statement, and a
    faked worker has no connection to take away.
    """

    async def test_cancellation_retrieves_a_completed_worker_failure(self) -> None:
        """The caller keeps cancellation while the worker's failure remains observable."""
        work: asyncio.Future[None] = asyncio.get_running_loop().create_future()
        waiting = asyncio.create_task(settled(work))
        await asyncio.sleep(0)
        waiting.cancel()
        await asyncio.sleep(0)
        worker_error = RuntimeError("worker failed after cancellation")
        work.set_exception(worker_error)

        with pytest.raises(asyncio.CancelledError) as cancellation:
            await waiting

        assert cancellation.value.__cause__ is worker_error

    async def test_closing_the_store_waits_for_a_write_already_on_a_worker_thread(
        self,
        journal_store: EventJournalStore,
    ) -> None:
        """Closing the store must not close the connection a write is executing on.

        Closing is not only a shutdown path -- a bot closes its store on every
        config reload -- so this is a routine edit landing while a turn is
        being written. SQLite answers a connection closed underneath a live
        statement with a segmentation fault rather than an exception, which
        takes the whole process with it.
        """
        running = threading.Event()
        release = threading.Event()

        def busy(transaction: Transaction) -> str:
            transaction.execute(_INSERT_MEMBERSHIP, ("agent@alice", ROOM, 1))
            _hold_the_connection(transaction, running, release)
            return "landed"

        writing = asyncio.create_task(journal_store.backend.write(busy))
        await asyncio.to_thread(running.wait, _WORKER_WAIT_SECONDS)
        closing = asyncio.create_task(journal_store.close())
        closed_early = await _finished_within_grace(closing)
        release.set()
        await closing

        assert not closed_early, "close() returned while a write was still executing on the connection"
        assert await writing == "landed"

    async def test_closing_the_store_waits_for_a_read_already_on_a_worker_thread(
        self,
        journal_store: EventJournalStore,
    ) -> None:
        """A close that drains only the writer leaves the same crash reachable from a read.

        Reads run on their own connections, which ``close()`` closes too, so
        the reader path owns this rule separately from the writer path.
        """
        running = threading.Event()
        release = threading.Event()

        def busy(transaction: Transaction) -> str:
            _hold_the_connection(transaction, running, release)
            return "read"

        reading = asyncio.create_task(journal_store.backend.read(busy))
        await asyncio.to_thread(running.wait, _WORKER_WAIT_SECONDS)
        closing = asyncio.create_task(journal_store.close())
        closed_early = await _finished_within_grace(closing)
        release.set()
        await closing

        assert not closed_early, "close() returned while a read was still executing on the connection"
        assert await reading == "read"

    async def test_a_cancelled_write_does_not_return_while_its_statement_runs(
        self,
        journal_store: EventJournalStore,
    ) -> None:
        """A cancelled write owns its transaction until the transaction ends.

        Returning early is what lets the caller's next act -- releasing the
        writer lock, or closing the connection -- happen behind a statement
        that is still running.
        """
        running = threading.Event()
        release = threading.Event()
        finished = threading.Event()

        def slow(transaction: Transaction) -> str:
            transaction.execute(_INSERT_MEMBERSHIP, ("agent@alice", ROOM, 1))
            running.set()
            release.wait(_WORKER_WAIT_SECONDS)
            finished.set()
            return "landed"

        writing = asyncio.create_task(journal_store.backend.write(slow))
        await asyncio.to_thread(running.wait, _WORKER_WAIT_SECONDS)
        writing.cancel()
        returned_early = await _finished_within_grace(writing)
        release.set()
        with pytest.raises(asyncio.CancelledError):
            await writing

        assert not returned_early, "the cancelled write returned while its statement was still running"
        assert finished.is_set()

    async def test_a_cancelled_read_does_not_return_while_its_statement_runs(
        self,
        journal_store: EventJournalStore,
    ) -> None:
        """A cancelled read owns its connection until its statement ends.

        On a pooled backend the connection goes back to the pool the moment
        this returns, so returning early hands a live connection to whoever
        reads next.
        """
        running = threading.Event()
        release = threading.Event()
        finished = threading.Event()

        def slow(transaction: Transaction) -> str:
            transaction.fetchall("SELECT 1 AS one")
            running.set()
            release.wait(_WORKER_WAIT_SECONDS)
            finished.set()
            return "read"

        reading = asyncio.create_task(journal_store.backend.read(slow))
        await asyncio.to_thread(running.wait, _WORKER_WAIT_SECONDS)
        reading.cancel()
        returned_early = await _finished_within_grace(reading)
        release.set()
        with pytest.raises(asyncio.CancelledError):
            await reading

        assert not returned_early, "the cancelled read returned while its statement was still running"
        assert finished.is_set()

    async def test_a_cancelled_write_keeps_the_next_write_out_of_its_transaction(
        self,
        journal_store: EventJournalStore,
    ) -> None:
        """The next write must not execute inside a cancelled write's open transaction.

        Two writes sharing one transaction corrupt each other in both
        directions, and both were observed: the second write committed the
        first one's statements along with its own, and the first write's
        ``rollback`` threw away a second write that had already reported
        success to its caller. On a durable turn ledger either one means a
        message answered twice or an answer that was never recorded.
        """
        first_running = threading.Event()
        release_first = threading.Event()
        second_started = threading.Event()

        def first(transaction: Transaction) -> str:
            transaction.execute(_INSERT_MEMBERSHIP, ("agent@alice", ROOM, 1))
            first_running.set()
            release_first.wait(_WORKER_WAIT_SECONDS)
            msg = "the cancelled write fails"
            raise RuntimeError(msg)

        def second(transaction: Transaction) -> str:
            second_started.set()
            transaction.execute(_INSERT_MEMBERSHIP, ("agent@bob", OTHER_ROOM, 1))
            return "second landed"

        cancelled = asyncio.create_task(journal_store.backend.write(first))
        await asyncio.to_thread(first_running.wait, _WORKER_WAIT_SECONDS)
        cancelled.cancel()
        following = asyncio.create_task(journal_store.backend.write(second))
        await _finished_within_grace(following)
        trespassed = second_started.is_set()
        release_first.set()
        with pytest.raises(asyncio.CancelledError):
            await cancelled

        assert not trespassed, "the next write ran inside the cancelled write's still-open transaction"
        assert await following == "second landed"
        assert await _membership_principals(journal_store) == ["agent@bob"], (
            "the failed write's rows survived, or the successful write's rows did not"
        )


class TestClosingAnswersEveryWriteItWillNotRun:
    """A write is run or refused, never abandoned -- however many are in flight.

    SQLite-only because the enqueue is: the PostgreSQL backend serializes on a
    lock, whose waiters are all woken by the releases that follow it, and has
    no queue for a producer to be stranded outside of. The rule holds on both
    backends; only one of them can be asked this question.
    """

    async def test_no_write_is_left_waiting_when_the_store_closes_under_load(
        self,
        tmp_path: Path,
    ) -> None:
        """Every write outstanding at a close is answered by that close.

        Closing is routine -- a bot closes its store on every config reload --
        so this is an ordinary edit landing while the journal is busy. The
        writer queue was bounded, and ``close()`` freed one slot per entry it
        drained: producers parked in ``put`` woke afterwards, enqueued onto a
        queue whose consumer had already been cancelled, and waited on a future
        nothing would ever settle. Producers past the bound were never woken at
        all, so re-checking the closed flag after the ``put`` would not have
        reached them either. Both outcomes are a reload the bot never returns
        from, holding a turn that Matrix has already been told was accepted.
        """
        backend = SqliteBackend.open(tmp_path / "closing.db")
        running = threading.Event()
        release = threading.Event()

        def busy(transaction: Transaction) -> str:
            transaction.execute(_INSERT_MEMBERSHIP, ("agent@alice", ROOM, 1))
            _hold_the_connection(transaction, running, release)
            return "landed"

        def queued(transaction: Transaction) -> str:
            transaction.fetchall("SELECT 1 AS one")
            return "landed"

        held = asyncio.create_task(backend.write(busy))
        await asyncio.to_thread(running.wait, _WORKER_WAIT_SECONDS)
        outstanding = [
            asyncio.create_task(backend.write(queued)) for _ in range(_WRITES_OUTNUMBERING_THE_OLD_QUEUE_BOUND)
        ]
        # One pass of the loop, so every producer has reached the point where
        # it is either queued or parked before the close starts.
        await asyncio.sleep(0)

        closing = asyncio.create_task(backend.close())
        await _finished_within_grace(closing)
        release.set()
        await closing
        answered, abandoned = await asyncio.wait(outstanding, timeout=_SETTLEMENT_WAIT_SECONDS)
        # Read eagerly rather than inside the assertion below, which stops at
        # the first answer it dislikes and would leave the rest reported as
        # exceptions nobody retrieved.
        refusals = [task.exception() for task in answered]
        await _release_writes_the_store_abandoned(backend, abandoned)

        assert not abandoned, f"{len(abandoned)} writes were left waiting on a store that had closed"
        assert await held == "landed"
        assert all(isinstance(refusal, RuntimeError) for refusal in refusals), (
            "a write the closed store never ran reported something other than a refusal"
        )


class TestTheJournalIsAtLeastAsDurableAsWhatCertifiesIt:
    """A committed row must survive every crash the checkpoint naming it survives.

    SQLite-only because the setting is. PostgreSQL commits durably by default
    and nothing here relaxes that, so its half of this rule is not configured
    but inherited.
    """

    async def test_the_writer_commits_reach_the_disk_and_the_readers_do_not_pay_for_it(
        self,
        tmp_path: Path,
    ) -> None:
        """The one connection that commits fsyncs; the ones that never commit do not.

        A Matrix sync checkpoint goes to disk through ``write_json_file_durable``,
        which fsyncs the record and the directory holding it. Under
        ``synchronous = NORMAL`` the WAL frames that checkpoint certifies do
        not, so a host reset can leave a fsynced token that resumes past events
        the journal no longer holds. Nothing asks for them again: the token
        says they were consumed, its store generation still matches, and a
        history debt is only recorded for a recovery gap the certifier can
        still see. The events are simply never answered.

        Readers are excluded rather than overlooked. Every statement that
        writes runs inside ``write``, on the writer connection, so a reader
        would pay an fsync it has nothing to flush.
        """
        backend = SqliteBackend.open(tmp_path / "durable.db")
        try:
            writer = await backend.write(_synchronous_mode)
            reader = await backend.read(_synchronous_mode)
            wal = await backend.write(_journal_mode)
        finally:
            await backend.close()

        assert writer == _SQLITE_SYNCHRONOUS_FULL, "the journal writer does not fsync the commits it reports as landed"
        assert reader == _SQLITE_SYNCHRONOUS_NORMAL, "a reader is paying for durability it cannot use"
        assert wal == "wal", "the durability this pins is the durability of WAL mode"


class TestMatrixDeliveryMigration:
    """Opening current code migrates provable debt and refuses unprovable ownership."""

    async def test_sqlite_refuses_the_released_unfenced_generic_outbox(self, tmp_path: Path) -> None:
        """SQLite fails at startup instead of accepting a schema it cannot use."""
        database_path = tmp_path / "released-unfenced-delivery.db"
        with sqlite3.connect(database_path) as connection:
            _install_released_unfenced_delivery_schema(connection)

        with pytest.raises(RuntimeError, match="generic Matrix delivery schema predates membership fencing"):
            EventJournalStore.open_sqlite(database_path)

    async def test_postgres_refuses_the_released_unfenced_generic_outbox(
        self,
        postgres_journal_url: str,
    ) -> None:
        """PostgreSQL enforces the same explicit reset boundary."""
        import psycopg  # noqa: PLC0415 - optional backend exercised explicitly

        database_url = postgres_journal_schema_url(postgres_journal_url)
        with psycopg.connect(database_url) as connection:
            _install_released_unfenced_delivery_schema(connection)
            connection.commit()

        with pytest.raises(RuntimeError, match="generic Matrix delivery schema predates membership fencing"):
            EventJournalStore.open_postgres(database_url)

    async def test_sqlite_migrates_exact_responses_and_expires_legacy_approvals(self, tmp_path: Path) -> None:
        """SQLite replaces legacy approval transport with fail-closed decisions."""
        database_path = tmp_path / "legacy-delivery.db"
        connection = sqlite3.connect(database_path)
        _install_legacy_delivery_state(connection, postgres=False)
        connection.execute(
            "CREATE INDEX response_outbox_unacknowledged_scan "
            "ON response_outbox (principal_id, created_at_ns, turn_id, stage)",
        )
        connection.commit()
        connection.close()

        store = EventJournalStore.open_sqlite(database_path)
        try:
            await _assert_legacy_delivery_state_migrated(store)
        finally:
            await store.close()

        inspected = sqlite3.connect(database_path)
        try:
            tables = {str(row[0]) for row in inspected.execute("SELECT name FROM sqlite_master WHERE type='table'")}
            indexes = {str(row[0]) for row in inspected.execute("SELECT name FROM sqlite_master WHERE type='index'")}
        finally:
            inspected.close()
        assert "response_outbox" not in tables
        assert "approval_cards_legacy_delivery" not in tables
        assert "response_outbox_unacknowledged_scan" not in indexes

    async def test_postgres_migrates_exact_responses_and_expires_legacy_approvals(
        self,
        postgres_journal_url: str,
    ) -> None:
        """Postgres applies the same fail-closed migration under its schema lock."""
        import psycopg  # noqa: PLC0415 - optional backend exercised explicitly

        database_url = postgres_journal_schema_url(postgres_journal_url)
        with psycopg.connect(database_url) as connection:
            _install_legacy_delivery_state(connection, postgres=True)
            connection.execute(
                "CREATE INDEX response_outbox_unacknowledged_scan "
                "ON response_outbox (principal_id, created_at_ns, turn_id, stage)",
            )
            connection.commit()

        store = EventJournalStore.open_postgres(database_url)
        try:
            await _assert_legacy_delivery_state_migrated(store)
        finally:
            await store.close()

        with psycopg.connect(database_url) as connection:
            legacy_response = connection.execute("SELECT to_regclass('response_outbox')").fetchone()
            legacy_cards = connection.execute("SELECT to_regclass('approval_cards_legacy_delivery')").fetchone()
            legacy_index = connection.execute(
                "SELECT to_regclass('response_outbox_unacknowledged_scan')",
            ).fetchone()
        assert legacy_response == (None,)
        assert legacy_cards == (None,)
        assert legacy_index == (None,)

    async def test_sqlite_refuses_an_attempted_unmarked_response(self, tmp_path: Path) -> None:
        """An unknown physical event is not guessed into the marker protocol."""
        database_path = tmp_path / "attempted-legacy-response.db"
        with sqlite3.connect(database_path) as connection:
            _install_ambiguous_legacy_response(connection, postgres=False)

        with pytest.raises(RuntimeError, match="legacy payload has no stable delivery marker"):
            EventJournalStore.open_sqlite(database_path)

    async def test_postgres_refuses_an_attempted_unmarked_response(
        self,
        postgres_journal_url: str,
    ) -> None:
        """PostgreSQL enforces the same fail-closed released-schema boundary."""
        import psycopg  # noqa: PLC0415 - optional backend exercised explicitly

        database_url = postgres_journal_schema_url(postgres_journal_url)
        with psycopg.connect(database_url) as connection:
            _install_ambiguous_legacy_response(connection, postgres=True)
            connection.commit()

        with pytest.raises(RuntimeError, match="legacy payload has no stable delivery marker"):
            EventJournalStore.open_postgres(database_url)


class TestConnectionSecretsStayOutOfLogs:
    """A DSN carries a password, so it must not ride along in a repr."""

    async def test_the_postgres_backend_repr_omits_its_connection_string(self) -> None:
        """The Postgres backend repr omits its connection string."""
        pytest.importorskip("psycopg")
        from mindroom.event_journal.postgres_backend import PostgresBackend  # noqa: PLC0415 - keeps psycopg optional

        backend = PostgresBackend(database_url="postgresql://someone:hunter2@db.example:5432/journal")

        rendered = repr(backend)

        assert "hunter2" not in rendered
        assert "someone" not in rendered
        assert "db.example" not in rendered


class TestSchemaUpgrades:
    """Opening the journal preserves old rows and enables current writes."""

    @staticmethod
    async def _assert_legacy_approval_call_provenance_is_unknown(backend: Backend) -> None:
        row = await backend.read(
            lambda transaction: transaction.fetchone(
                """
                SELECT human_approval_required
                FROM approval_continuation_calls
                WHERE principal_id = ? AND approval_id = ? AND tool_call_id = ?
                """,
                ("agent@alice", "approval-legacy", "call-legacy"),
            ),
        )
        assert row is not None
        assert row["human_approval_required"] is None

    @staticmethod
    def _legacy_approval_calls_schema() -> str:
        return """
            CREATE TABLE approval_continuation_calls (
                principal_id TEXT NOT NULL,
                approval_id TEXT NOT NULL,
                generation BIGINT NOT NULL,
                tool_call_id TEXT NOT NULL,
                call_ordinal BIGINT NOT NULL,
                tool_name TEXT NOT NULL,
                invoking_agent TEXT NOT NULL,
                expires_at_ns BIGINT NOT NULL,
                decision TEXT,
                reason TEXT,
                PRIMARY KEY (principal_id, approval_id, generation, tool_call_id)
            )
        """

    @staticmethod
    async def _assert_legacy_questions_are_archived(backend: Backend) -> None:
        archived = await backend.read(
            lambda transaction: transaction.fetchall(
                """
                SELECT question_event_id, claimed_source_event_id
                FROM interactive_questions_pre_selection
                ORDER BY question_event_id
                """,
            ),
        )
        assert [(row["question_event_id"], row["claimed_source_event_id"]) for row in archived] == [
            ("$claimed", "$selection"),
            ("$open", None),
        ]

    @staticmethod
    async def _assert_current_questions_work(backend: Backend) -> None:
        journal = EventJournalStore(backend)
        assert await _interactive_question_rows(journal) == []

        alice = journal.principal("agent@alice")
        await admit(alice, "$turn")
        await _activate_interactive_question(alice, "$current")

        current = await _interactive_question_rows(journal)
        assert [row["question_event_id"] for row in current] == ["$current"]

    @staticmethod
    def _create_legacy_questions_sqlite(database: sqlite3.Connection) -> None:
        database.execute(
            """
            CREATE TABLE interactive_questions (
                principal_id TEXT NOT NULL,
                question_event_id TEXT NOT NULL,
                room_id TEXT NOT NULL,
                thread_id TEXT NOT NULL,
                creator_agent TEXT NOT NULL,
                question_json TEXT NOT NULL,
                membership_epoch BIGINT NOT NULL,
                claimed_source_event_id TEXT,
                created_at_ns BIGINT NOT NULL,
                PRIMARY KEY (principal_id, question_event_id),
                UNIQUE (principal_id, claimed_source_event_id)
            )
            """,
        )
        database.execute(
            """
            CREATE INDEX interactive_questions_active
            ON interactive_questions (
                principal_id, room_id, thread_id, creator_agent,
                created_at_ns, question_event_id
            )
            WHERE claimed_source_event_id IS NULL
            """,
        )
        database.executemany(
            """
            INSERT INTO interactive_questions (
                principal_id, question_event_id, room_id, thread_id,
                creator_agent, question_json, membership_epoch,
                claimed_source_event_id, created_at_ns
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                ("agent@alice", "$open", ROOM, "$thread", "agent", '{"question_text":"Open"}', 0, None, 1),
                (
                    "agent@alice",
                    "$claimed",
                    ROOM,
                    "$thread",
                    "agent",
                    '{"question_text":"Claimed"}',
                    0,
                    "$selection",
                    2,
                ),
            ),
        )

    async def test_sqlite_archives_the_previous_question_schema_and_reopens(self, tmp_path: Path) -> None:
        """SQLite keeps legacy prompts inert while the current projection remains writable."""
        database_path = tmp_path / "previous-questions.db"
        with sqlite3.connect(database_path) as database:
            self._create_legacy_questions_sqlite(database)

        backend = SqliteBackend.open(database_path)
        try:
            await self._assert_legacy_questions_are_archived(backend)
            await self._assert_current_questions_work(backend)
        finally:
            await backend.close()

        reopened = SqliteBackend.open(database_path)
        try:
            await self._assert_legacy_questions_are_archived(reopened)
            current = await _interactive_question_rows(EventJournalStore(reopened))
            assert [row["question_event_id"] for row in current] == ["$current"]
        finally:
            await reopened.close()

    async def test_postgres_archives_the_previous_question_schema_and_reopens(
        self,
        postgres_journal_url: str,
    ) -> None:
        """PostgreSQL applies the same inert archival upgrade under its schema lock."""
        import psycopg  # noqa: PLC0415 - optional backend exercised only by this test

        from mindroom.event_journal.postgres_backend import PostgresBackend  # noqa: PLC0415

        database_url = postgres_journal_schema_url(postgres_journal_url)
        with psycopg.connect(database_url) as database:
            database.execute(
                """
                CREATE TABLE interactive_questions (
                    principal_id TEXT NOT NULL,
                    question_event_id TEXT NOT NULL,
                    room_id TEXT NOT NULL,
                    thread_id TEXT NOT NULL,
                    creator_agent TEXT NOT NULL,
                    question_json TEXT NOT NULL,
                    membership_epoch BIGINT NOT NULL,
                    claimed_source_event_id TEXT,
                    created_at_ns BIGINT NOT NULL,
                    PRIMARY KEY (principal_id, question_event_id),
                    UNIQUE (principal_id, claimed_source_event_id)
                )
                """,
            )
            database.execute(
                """
                CREATE INDEX interactive_questions_active
                ON interactive_questions (
                    principal_id, room_id, thread_id, creator_agent,
                    created_at_ns, question_event_id
                )
                WHERE claimed_source_event_id IS NULL
                """,
            )
            with database.cursor() as cursor:
                cursor.executemany(
                    """
                    INSERT INTO interactive_questions (
                        principal_id, question_event_id, room_id, thread_id,
                        creator_agent, question_json, membership_epoch,
                        claimed_source_event_id, created_at_ns
                    ) VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s)
                    """,
                    (
                        (
                            "agent@alice",
                            "$open",
                            ROOM,
                            "$thread",
                            "agent",
                            '{"question_text":"Open"}',
                            0,
                            None,
                            1,
                        ),
                        (
                            "agent@alice",
                            "$claimed",
                            ROOM,
                            "$thread",
                            "agent",
                            '{"question_text":"Claimed"}',
                            0,
                            "$selection",
                            2,
                        ),
                    ),
                )

        backend = PostgresBackend.open(database_url)
        try:
            await self._assert_legacy_questions_are_archived(backend)
            await self._assert_current_questions_work(backend)
        finally:
            await backend.close()

        reopened = PostgresBackend.open(database_url)
        try:
            await self._assert_legacy_questions_are_archived(reopened)
            current = await _interactive_question_rows(EventJournalStore(reopened))
            assert [row["question_event_id"] for row in current] == ["$current"]
        finally:
            await reopened.close()

    @staticmethod
    async def _assert_old_row_is_discarded_and_new_card_works(backend: Backend) -> None:
        old = await backend.read(
            lambda transaction: transaction.fetchone(
                "SELECT 1 AS present FROM approval_cards WHERE principal_id = ?",
                ("agent@alice",),
            ),
        )
        assert old is None
        await backend.write(
            lambda transaction: transaction.execute(
                """
                INSERT INTO approval_cards (
                    principal_id, delivery_id, continuation_id,
                    continuation_generation, tool_call_id, membership_epoch
                ) VALUES (?, ?, ?, ?, ?, ?)
                """,
                ("agent@alice", "native", "continuation-native", 0, "native", 0),
            ),
        )
        stored = await backend.read(
            lambda transaction: transaction.fetchone(
                """
                SELECT delivery_id, continuation_id, continuation_generation, tool_call_id
                FROM approval_cards WHERE principal_id = ? AND delivery_id = ?
                """,
                ("agent@alice", "native"),
            ),
        )
        assert stored is not None
        assert stored["delivery_id"] == "native"
        assert stored["continuation_id"] == "continuation-native"
        assert stored["continuation_generation"] == 0
        assert stored["tool_call_id"] == "native"

    async def test_sqlite_open_discards_the_previous_card_schema(self, tmp_path: Path) -> None:
        """SQLite drops an obsolete card while leaving the current table writable."""
        database_path = tmp_path / "previous-schema.db"
        with sqlite3.connect(database_path) as database:
            database.execute(
                """
                CREATE TABLE approval_cards (
                    principal_id TEXT NOT NULL,
                    room_id TEXT NOT NULL,
                    transaction_id TEXT NOT NULL,
                    card_event_id TEXT,
                    attempted INTEGER NOT NULL,
                    sending_device_id TEXT,
                    card_json TEXT NOT NULL,
                    resolution_json TEXT,
                    membership_epoch BIGINT NOT NULL,
                    created_at_ns BIGINT NOT NULL,
                    PRIMARY KEY (principal_id, transaction_id)
                )
                """,
            )
            database.execute(
                """
                INSERT INTO approval_cards (
                    principal_id, room_id, transaction_id, card_event_id, attempted,
                    sending_device_id, card_json, resolution_json, membership_epoch, created_at_ns
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                ("agent@alice", ROOM, "legacy", "$legacy", 1, DEVICE, '{"body":"old"}', None, 1, 1),
            )

        backend = SqliteBackend.open(database_path)
        try:
            await self._assert_old_row_is_discarded_and_new_card_works(backend)
        finally:
            await backend.close()

    async def test_sqlite_open_adds_nullable_provenance_to_previous_approval_calls(self, tmp_path: Path) -> None:
        """SQLite upgrades old calls without inventing approval provenance."""
        database_path = tmp_path / "previous-approval-calls.db"
        with sqlite3.connect(database_path) as database:
            database.execute(self._legacy_approval_calls_schema())
            database.execute(
                """
                INSERT INTO approval_continuation_calls (
                    principal_id, approval_id, generation, tool_call_id, call_ordinal,
                    tool_name, invoking_agent, expires_at_ns, decision, reason
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    "agent@alice",
                    "approval-legacy",
                    0,
                    "call-legacy",
                    0,
                    "legacy_action",
                    "agent",
                    1,
                    "approved",
                    None,
                ),
            )

        backend = SqliteBackend.open(database_path)
        try:
            await self._assert_legacy_approval_call_provenance_is_unknown(backend)
        finally:
            await backend.close()

    async def test_postgres_open_discards_the_previous_card_schema(
        self,
        postgres_journal_url: str,
    ) -> None:
        """PostgreSQL drops an obsolete card while leaving the current table writable."""
        import psycopg  # noqa: PLC0415 - optional backend exercised only by this test

        from mindroom.event_journal.postgres_backend import PostgresBackend  # noqa: PLC0415

        database_url = postgres_journal_schema_url(postgres_journal_url)
        with psycopg.connect(database_url) as database:
            database.execute(
                """
                CREATE TABLE approval_cards (
                    principal_id TEXT NOT NULL,
                    room_id TEXT NOT NULL,
                    transaction_id TEXT NOT NULL,
                    card_event_id TEXT,
                    attempted INTEGER NOT NULL,
                    sending_device_id TEXT,
                    card_json TEXT NOT NULL,
                    resolution_json TEXT,
                    membership_epoch BIGINT NOT NULL,
                    created_at_ns BIGINT NOT NULL,
                    PRIMARY KEY (principal_id, transaction_id)
                )
                """,
            )
            database.execute(
                """
                INSERT INTO approval_cards (
                    principal_id, room_id, transaction_id, card_event_id, attempted,
                    sending_device_id, card_json, resolution_json, membership_epoch, created_at_ns
                ) VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s)
                """,
                ("agent@alice", ROOM, "legacy", "$legacy", 1, DEVICE, '{"body":"old"}', None, 1, 1),
            )

        backend = PostgresBackend.open(database_url)
        try:
            await self._assert_old_row_is_discarded_and_new_card_works(backend)
        finally:
            await backend.close()

    async def test_postgres_open_adds_nullable_provenance_to_previous_approval_calls(
        self,
        postgres_journal_url: str,
    ) -> None:
        """PostgreSQL upgrades old calls without inventing approval provenance."""
        import psycopg  # noqa: PLC0415 - optional backend exercised only by this test

        from mindroom.event_journal.postgres_backend import PostgresBackend  # noqa: PLC0415

        database_url = postgres_journal_schema_url(postgres_journal_url)
        with psycopg.connect(database_url) as database:
            database.execute(self._legacy_approval_calls_schema())
            database.execute(
                """
                INSERT INTO approval_continuation_calls (
                    principal_id, approval_id, generation, tool_call_id, call_ordinal,
                    tool_name, invoking_agent, expires_at_ns, decision, reason
                ) VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s)
                """,
                (
                    "agent@alice",
                    "approval-legacy",
                    0,
                    "call-legacy",
                    0,
                    "legacy_action",
                    "agent",
                    1,
                    "approved",
                    None,
                ),
            )

        backend = PostgresBackend.open(database_url)
        try:
            await self._assert_legacy_approval_call_provenance_is_unknown(backend)
        finally:
            await backend.close()


class TestHotQueriesAreIndexCovered:
    """Every scan a running bot repeats must come from an index, ordering included."""

    # The shapes the code actually issues, ORDER BY included. An earlier
    # measurement of this dropped the trailing ORDER BY terms and reported all
    # of them clean; two were building a temporary b-tree on every pass. An
    # index that matches only the prefix of an ORDER BY cannot satisfy it, on
    # either backend.
    _QUERIES: ClassVar[dict[str, str]] = {
        "worker pending scan": (
            "SELECT * FROM journal_events WHERE principal_id=? AND state='pending' "
            "AND receipt_order>? ORDER BY receipt_order LIMIT 50"
        ),
        "replay guard thread scan": (
            "SELECT * FROM journal_events WHERE principal_id=? AND room_id=? AND thread_id=? "
            "AND state='pending' AND origin_server_ts>? AND event_id<>?"
        ),
        "projection page": (
            "SELECT * FROM visible_messages WHERE principal_id=? AND room_id=? AND thread_id=? "
            "ORDER BY created_ts DESC, logical_event_id DESC LIMIT 50"
        ),
        "projection page after a cursor": (
            "SELECT * FROM visible_messages WHERE principal_id=? AND room_id=? AND thread_id=?"  # noqa: S608 - the production clause, not input
            f"{_CONVERSATION_CURSOR_CLAUSE} ORDER BY created_ts DESC, logical_event_id DESC LIMIT 50"
        ),
        "revision point lookup": (
            "SELECT * FROM visible_messages WHERE principal_id=? AND room_id=? AND revision_event_id=?"
        ),
        "refresh-owing scan": (
            "SELECT * FROM visible_messages WHERE principal_id=? AND room_id=? AND thread_id=? "
            "AND refresh_token IS NOT NULL"
        ),
        "outbox recovery scan": (
            "SELECT * FROM matrix_delivery_outbox WHERE principal_id=? AND event_type=? "
            "AND acknowledged_event_id IS NULL AND retired=0 "
            "ORDER BY created_at_ns, delivery_id, stage LIMIT 50"
        ),
        "approval card scan": (
            "SELECT * FROM matrix_delivery_outbox AS initial "
            "JOIN approval_cards AS cards ON cards.principal_id=initial.principal_id "
            "AND cards.delivery_id=initial.delivery_id "
            "WHERE initial.principal_id=? AND initial.room_id=? AND initial.stage='initial' "
            "ORDER BY initial.created_at_ns, initial.delivery_id LIMIT 50"
        ),
        "approval card point lookup": (
            "SELECT * FROM matrix_delivery_outbox AS initial "
            "JOIN approval_cards AS cards ON cards.principal_id=initial.principal_id "
            "AND cards.delivery_id=initial.delivery_id "
            "WHERE initial.principal_id=? AND initial.acknowledged_event_id=? AND initial.stage='initial'"
        ),
        "continuation owner page": (
            "SELECT * FROM approval_continuations "
            "WHERE (entity_name, approval_id) > (?, ?) "
            "ORDER BY entity_name, approval_id LIMIT 50"
        ),
        "continuation owners for entities": (
            "SELECT * FROM approval_continuations WHERE entity_name IN (?, ?) "
            "AND (entity_name, approval_id) > (?, ?) "
            "ORDER BY entity_name, approval_id LIMIT 50"
        ),
    }

    async def test_no_hot_query_falls_back_to_a_scan_or_a_temporary_sort(self, tmp_path: Path) -> None:
        """No hot query falls back to a table scan or a temporary sort."""
        database = sqlite3.connect(tmp_path / "plans.db")
        for statement in schema_statements(SQLITE_DIALECT):
            database.execute(statement)

        offenders = {}
        for name, sql in self._QUERIES.items():
            params = tuple("x" for _ in range(sql.count("?")))
            plan = " | ".join(row[-1] for row in database.execute("EXPLAIN QUERY PLAN " + sql, params))
            if "TEMP B-TREE" in plan or "SCAN " in plan:
                offenders[name] = plan

        assert offenders == {}

    async def test_a_deep_page_seeks_its_cursor_instead_of_filtering_down_to_it(self, tmp_path: Path) -> None:
        """A deep page seeks its cursor rather than rescanning the conversation to reach it.

        Being on an index is not the same as being bounded by one, and the test
        above cannot tell the two apart: it rejects `SCAN` and `TEMP B-TREE`,
        and a cursor the backend cannot position on is neither. Spelled as the
        disjunction `created_ts < ? OR (created_ts = ? AND logical_event_id <
        ?)`, the cursor is a filter, so every page re-enters the conversation at
        its tip and walks forward through everything newer than it. The plan
        still says SEARCH, the index is still covering, and a full walk is
        quadratic.

        That walk is what an export runs, and its ceiling is 1,000,000
        messages. Measured on this schema at 500 messages a page: 0.75 s
        against 0.037 s at 100,000 messages and 77.1 s against 0.38 s at
        1,000,000, the disjunction growing 102.9x for the last 10x of messages
        where the row value grows 10.1x.

        So the plan has to name the cursor column. Under the disjunction the
        index is entered on the three equality columns alone and `created_ts`
        appears nowhere in it, which is what makes this assertion fail the
        moment the spelling drifts back.
        """
        database = sqlite3.connect(tmp_path / "cursor-plan.db")
        for statement in schema_statements(SQLITE_DIALECT):
            database.execute(statement)
        sql = self._QUERIES["projection page after a cursor"]

        plan = " | ".join(
            row[-1] for row in database.execute("EXPLAIN QUERY PLAN " + sql, tuple("x" for _ in range(sql.count("?"))))
        )

        assert "created_ts" in plan, plan

    async def test_every_ordered_index_column_carries_the_byte_order_pin_on_postgres(self) -> None:
        """Every ordered index column carries the byte-order pin on PostgreSQL.

        A query-plan test can only measure the backend it runs on, and SQLite's
        default text collation is already byte order -- so an index whose pin
        failed to render looks perfect there and is silently unusable for
        ordering on PostgreSQL, which declines it and sorts instead. This checks
        the rendered DDL rather than a plan, because that is the part that
        differs.
        """
        rendered = [" ".join(s.split()) for s in schema_statements(POSTGRES_DIALECT)]
        indexes = [s for s in rendered if "CREATE INDEX" in s]

        assert not [s for s in rendered if "/*bytes*/" in s], "byte-order marker left unexpanded"
        ordered_text_indexes = [s for s in indexes if "delivery_id" in s or "approval_continuations_owner_scan" in s]
        assert ordered_text_indexes, "expected indexes over the unpinned text columns"
        for statement in ordered_text_indexes:
            assert 'COLLATE "C"' in statement, statement


class TestTurnRecordsLiveBesideTheTurnsTheyDescribe:
    """The first half of collapsing "has this turn finished?" onto one writer.

    Today that question is answered by the journal's pending set and by a
    JSON-file ledger, which cannot share a transaction and therefore settle at
    different moments. These rows are the same records in the database that
    settles the turns, so a future writer can commit both together. Nothing
    reads them yet on purpose: a dedupe substrate that is half migrated is one
    that can answer a message twice.
    """

    @staticmethod
    async def _stored(records: TurnRecordStore) -> dict[str, str]:
        """Return one agent's records keyed by the event that indexes each.

        Read through `load_all`, which is the whole read surface: a warm-up
        rebuilds the map once and answers every later question from memory, so
        the store has no point lookup for anything else to depend on.
        """
        return {index: record for index, _anchor, record in await records.load_all()}

    async def test_a_record_is_reachable_from_every_event_that_indexes_it(
        self,
        journal_store: EventJournalStore,
    ) -> None:
        """A coalesced turn answers several sources and is found from any of them.

        Storing it by anchor alone would turn "was this source answered?" into
        a scan, and that question is asked on the ingress path for every event.
        """
        records = journal_store.turn_records("agent")
        await records.upsert(
            index_event_ids=("$a", "$b", "$c"),
            anchor_event_id="$a",
            record_json='{"anchor_event_id": "$a"}',
        )

        assert await self._stored(records) == {
            "$a": '{"anchor_event_id": "$a"}',
            "$b": '{"anchor_event_id": "$a"}',
            "$c": '{"anchor_event_id": "$a"}',
        }

    async def test_an_index_the_turn_no_longer_answers_is_dropped(
        self,
        journal_store: EventJournalStore,
    ) -> None:
        """A shrinking coalesced batch must not leave a source looking answered.

        This is the one direction that silently drops a user's message: a stale
        row would report "already handled" for a source the turn stopped
        accounting for, and nothing would ever answer it.
        """
        records = journal_store.turn_records("agent")
        await records.upsert(
            index_event_ids=("$a", "$b"),
            anchor_event_id="$a",
            record_json='{"v": 1}',
        )

        await records.upsert(index_event_ids=("$a",), anchor_event_id="$a", record_json='{"v": 2}')

        assert await self._stored(records) == {"$a": '{"v": 2}'}

    async def test_a_re_anchored_record_leaves_no_row_under_its_old_anchor(
        self,
        journal_store: EventJournalStore,
    ) -> None:
        """Dropping the anchor itself is the case a naive delete misses.

        Redaction and conflict projection re-anchor a record when the anchor is
        one of the sources being dropped -- `handled_turns` picks the last
        retained source instead. The rows to clean up are then filed under the
        *old* anchor, so a delete scoped to the anchor being written walks past
        them and the dropped source keeps reporting "already handled". Nothing
        would ever answer it.
        """
        records = journal_store.turn_records("agent")
        await records.upsert(
            index_event_ids=("$a", "$b"),
            anchor_event_id="$a",
            record_json='{"v": 1}',
        )

        # `$a` is redacted away, so the record re-anchors onto `$b`.
        await records.upsert(index_event_ids=("$b",), anchor_event_id="$b", record_json='{"v": 2}')

        assert await self._stored(records) == {"$b": '{"v": 2}'}, "the old anchor's row survived re-anchoring"

    async def test_records_are_scoped_to_the_agent_not_the_matrix_identity(
        self,
        journal_store: EventJournalStore,
    ) -> None:
        """A re-login must not lose the proof that a message was answered.

        Every other table here is keyed per (agent, Matrix identity), because
        what it holds is only meaningful beside the sync that produced it. This
        one is not: it records that a turn is finished, which stays true when
        the bot logs in as a new device. Scoping it per principal would make
        such a bot answer its whole backlog a second time.

        Transactionality comes from sharing the database, which two views of
        the same store do regardless of how they are keyed.
        """
        first = journal_store.turn_records("agent")
        other_agent = journal_store.turn_records("other-agent")
        await first.upsert(index_event_ids=("$a",), anchor_event_id="$a", record_json='{"v": 1}')

        assert await self._stored(journal_store.turn_records("agent")) == {"$a": '{"v": 1}'}
        assert await self._stored(other_agent) == {}, "one agent read another's turn records"

    async def test_a_warm_up_reads_every_record_in_a_stable_order(
        self,
        journal_store: EventJournalStore,
    ) -> None:
        """Both backends have to rebuild the same map from the same rows.

        The order is pinned to byte order rather than the server's collation,
        for the reason every other scan here is: PostgreSQL's default collation
        does not agree with SQLite's about it, and a restart that rebuilt a
        differently ordered map would be a difference nothing else would catch.
        """
        records = journal_store.turn_records("agent")
        await records.upsert(index_event_ids=("$B",), anchor_event_id="$B", record_json='{"v": "B"}')
        await records.upsert(index_event_ids=("$a",), anchor_event_id="$a", record_json='{"v": "a"}')

        assert [index for index, _anchor, _json in await records.load_all()] == ["$B", "$a"]

    async def test_compaction_forgets_what_it_is_told_to(
        self,
        journal_store: EventJournalStore,
    ) -> None:
        """The ledger compacts terminal history, so these rows have to as well."""
        records = journal_store.turn_records("agent")
        await records.upsert(index_event_ids=("$a", "$b"), anchor_event_id="$a", record_json='{"v": 1}')

        await records.forget(index_event_ids=("$a",))

        assert await self._stored(records) == {"$b": '{"v": 1}'}


# Takes the journal's write lock on a database that is not yet in WAL, which is
# the shape of a bot creating the file, and holds it until told to let go.
# Raw ``sqlite3`` rather than a store, because a store enters WAL as it opens
# and so cannot hold a lock in the state this is here to hold one in.
_HOLD_FRESH_DATABASE_SOURCE = """
import sqlite3
import sys
import time
from pathlib import Path

database, held, release = (Path(argument) for argument in sys.argv[1:4])
database.parent.mkdir(parents=True, exist_ok=True)
connection = sqlite3.connect(database, isolation_level=None)
connection.execute("PRAGMA busy_timeout = 10000")
connection.execute("BEGIN IMMEDIATE")
connection.execute("CREATE TABLE IF NOT EXISTS seed (id INTEGER PRIMARY KEY)")
held.write_text("held")
while not release.exists():
    time.sleep(0.01)
connection.execute("COMMIT")
connection.close()
"""

# Holds a real store's real write transaction open, the way an export installing
# one large hydrated conversation holds it. Only the body of the transaction is
# stand-in: the connection, the queue, the ``BEGIN IMMEDIATE`` and the lock it
# takes are the production ones, and the lock is the whole subject.
_HOLD_STORE_WRITE_SOURCE = """
import asyncio
import sys
import time
from pathlib import Path

from mindroom.event_journal import EventJournalStore

database, held, release = (Path(argument) for argument in sys.argv[1:4])


def hold(transaction):
    transaction.execute("PRAGMA user_version = 7")
    held.write_text("held")
    while not release.exists():
        time.sleep(0.01)


async def main():
    store = EventJournalStore.open_sqlite(database)
    await store.backend.write(hold)
    await store.close()


asyncio.run(main())
"""


class TestCrossProcessWriters:
    """What a second OS process writing this journal does to the one beside it.

    The export is that process. It opens the bot's journal from its own
    interpreter and hydrates through it, so the single-writer queue -- which is
    per process -- is not between them. The busy timeout is, and these are the
    tests that say what that buys, because until they existed the timeout was
    load-bearing and unexercised: every other concurrency test here shares one
    interpreter, where the queue makes the writers take turns before SQLite is
    ever asked to.
    """

    @staticmethod
    @asynccontextmanager
    async def _holding_the_write_lock(source: str, tmp_path: Path) -> AsyncIterator[Path]:
        """Run a real second process holding this journal's write lock.

        Entered once that process has the lock and not before, so a body that
        contends is contending with something. Released on the way out however
        the body ended: a test that fails while another process is sitting on
        the lock should report why it failed, not time out waiting for a
        process only it can free.
        """
        script = tmp_path / "hold.py"
        script.write_text(source)
        database = tmp_path / "tracking" / "event_journal.db"
        held = tmp_path / "held"
        release = tmp_path / "release"
        holder = await asyncio.create_subprocess_exec(
            sys.executable,
            str(script),
            str(database),
            str(held),
            str(release),
        )
        try:
            while not held.exists():
                assert holder.returncode is None, "the second process exited before it took the write lock"
                await asyncio.sleep(0.01)
            yield release
        finally:
            release.write_text("release")
            async with asyncio.timeout(_HOLDER_EXIT_SECONDS):
                await holder.wait()

    async def test_opening_waits_out_a_write_lock_another_process_holds(self, tmp_path: Path) -> None:
        """A journal being written by another process is opened, not refused.

        Entering WAL is the one statement SQLite will not run the busy handler
        for, so before this the ten-second timeout the rest of the backend
        relies on was zero here: an export pass that started while the bot was
        still creating the database died on the spot, in single-digit
        milliseconds, having waited for nothing.

        The wait is proven rather than timed. An open that refuses instead of
        waiting is finished long before the grace window, so the assertion that
        it is still running is what separates the two -- and the lock is only
        released afterwards, which means the open can only have succeeded by
        having waited for it.
        """
        database = tmp_path / "tracking" / "event_journal.db"
        async with self._holding_the_write_lock(_HOLD_FRESH_DATABASE_SOURCE, tmp_path) as release:
            opening = asyncio.create_task(asyncio.to_thread(EventJournalStore.open_sqlite, database))
            if await _finished_within_grace(opening):
                # Awaiting it is how the refusal itself gets reported, rather
                # than a bare "this finished too early".
                await opening
                pytest.fail("opening the journal returned at once instead of waiting for the lock")
            release.write_text("release")
            store = await opening
        try:
            assert await admit(store.principal(ALICE), "$after") is AdmissionResult.ADMITTED
        finally:
            await store.close()

    async def test_admission_survives_a_write_lock_another_process_holds(self, tmp_path: Path) -> None:
        """A bot admits through an export's write, late rather than never.

        The guarantee the backend actually offers two processes, stated where
        it can be checked: contention delays an admission for as long as the
        other process's transaction runs, and the busy timeout is what turns
        that into a delay instead of a refusal. Past ten seconds it is a
        refusal, which ``journal_ingress`` turns into a declined event rather
        than an accepted one -- so what this pins is the bound, not an absence
        of one.
        """
        database = tmp_path / "tracking" / "event_journal.db"
        store = EventJournalStore.open_sqlite(database)
        try:
            principal = store.principal(ALICE)
            assert await admit(principal, "$before") is AdmissionResult.ADMITTED

            async with self._holding_the_write_lock(_HOLD_STORE_WRITE_SOURCE, tmp_path) as release:
                admitting = asyncio.create_task(admit(principal, "$during"))
                if await _finished_within_grace(admitting):
                    await admitting
                    pytest.fail("admission returned at once instead of waiting for the other writer")
                release.write_text("release")
                assert await admitting is AdmissionResult.ADMITTED

            assert await bodies(principal) == ["$before", "$during"]
        finally:
            await store.close()
