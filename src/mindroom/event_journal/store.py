"""The principal-bound store view runtime code is given.

One database may hold many bots, but no runtime caller ever sees the column
that separates them. Operational methods take no ``principal_id`` at all, so
reading or settling another bot's rows is not something a caller can express,
rather than something it is trusted not to do.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import TYPE_CHECKING
from uuid import UUID

from . import (
    approval_continuations,
    approval_signals,
    interactive_questions,
    journal,
    reads,
    replies,
    tool_jobs,
    turn_rows,
)
from .approval_signals import ApprovalSignals
from .identity import agent_of_principal
from .models import (
    IngestionConsumer,
    IngestionConsumerBindingError,
)
from .tool_jobs import SavedToolJob  # noqa: TC001 - part of this module's runtime return types
from .views import (
    ApprovalDeliveryView,
    DispatchView,
    ExportProjectionView,
    IngestionBatchAdmissionView,
    PendingTurnView,
    RelationView,
)

if TYPE_CHECKING:
    from collections.abc import Callable
    from pathlib import Path

    from mindroom.interactive_models import InteractivePrompt
    from mindroom.turn_record import TurnRecord

    from .approval_continuations import ApprovalContinuation
    from .backend import Backend, Transaction
    from .interactive_questions import InteractiveSelection
    from .models import (
        AdmissionResult,
        InboundEvent,
        ResponseRecoveryState,
        RoomMembershipPosition,
    )
    from .projection import ProjectedEvent

_DEFAULT_APPROVAL_CONTINUATION_OWNER_LIMIT = 100


def _consumer(transaction: Transaction, principal_id: str, generation: UUID, stream_id: UUID | None) -> IngestionConsumer:  # fmt: skip
    if stream_id is None:
        transaction.execute("INSERT INTO matrix_sync_consumers (principal_id, consumer_generation, stream_id) VALUES (?, ?, NULL) ON CONFLICT (principal_id) DO NOTHING", (principal_id, str(generation)))  # fmt: skip
    else:
        transaction.execute("UPDATE matrix_sync_consumers SET stream_id = ? WHERE principal_id = ? AND consumer_generation = ? AND (stream_id IS NULL OR stream_id = ?) AND NOT EXISTS (SELECT 1 FROM matrix_sync_consumers WHERE stream_id = ?)", (str(stream_id), principal_id, str(generation), str(stream_id), str(stream_id)))  # fmt: skip
    row = transaction.fetchone("SELECT * FROM matrix_sync_consumers WHERE principal_id = ?", (principal_id,))
    if row is None:
        raise IngestionConsumerBindingError
    try:
        consumer = IngestionConsumer(UUID(str(row["consumer_generation"])), None if row["stream_id"] is None else UUID(str(row["stream_id"])))  # fmt: skip
    except (KeyError, TypeError, ValueError) as error:
        raise IngestionConsumerBindingError from error
    if stream_id is not None and consumer != IngestionConsumer(generation, stream_id):
        raise IngestionConsumerBindingError
    return consumer


def _deleted_events(
    transaction: Transaction,
    principal_id: str,
    room_id: str,
    event_ids: tuple[str, ...],
) -> frozenset[str]:
    # Deletions are kept per agent, which a principal's id names.
    return turn_rows.deleted_events(transaction, agent_of_principal(principal_id), room_id, event_ids)


@dataclass(frozen=True, slots=True)
class PrincipalStore(
    DispatchView,
    PendingTurnView,
    RelationView,
    ExportProjectionView,
    ApprovalDeliveryView,
    IngestionBatchAdmissionView,
):
    """Everything one bot may durably do, scoped to that bot.

    Each view it inherits is one collaborator's slice; the operations declared
    here are the ones no collaborator's view names.
    """

    _approval_signals: ApprovalSignals

    async def load_or_create_ingestion_consumer(self, *, new_generation: UUID) -> IngestionConsumer:  # noqa: D102
        return await self._write(_consumer, new_generation, None)

    async def bind_ingestion_stream(self, *, generation: UUID, stream_id: UUID) -> IngestionConsumer:  # noqa: D102
        try:
            return await self._write(_consumer, generation, stream_id)
        except Exception as error:
            if getattr(error, "sqlite_errorname", None) != "SQLITE_CONSTRAINT_UNIQUE" and getattr(error, "sqlstate", None) != "23505":  # fmt: skip
                raise
            owner = await self._backend.read(lambda tx: tx.fetchone("SELECT principal_id FROM matrix_sync_consumers WHERE stream_id = ?", (str(stream_id),)))  # fmt: skip
            if owner is not None and owner["principal_id"] != self._principal_id:
                raise IngestionConsumerBindingError from error
            raise

    async def admit(
        self,
        event: InboundEvent,
        projected: ProjectedEvent | None = None,
    ) -> AdmissionResult:
        """Admit one event and update the projection in a single transaction."""
        return await self._write(journal.admit, event, projected)

    async def response_recovery_state(self, turn_record: TurnRecord) -> ResponseRecoveryState:
        """Read one response's durable handoff through the reserved recovery lane."""
        return await self._recovery_read(journal.response_recovery_state, turn_record)

    async def ingestion_membership_position(self, room_id: str) -> RoomMembershipPosition | None:
        """Return the producer position, or None until its first membership admission."""
        return await self._read(journal.ingestion_membership_position, room_id)

    async def interactive_prompt_is_current(
        self,
        *,
        room_id: str,
        question_event_id: str,
        expected: InteractivePrompt,
    ) -> bool:
        """Return whether projection still exposes one prompt in its active membership."""
        return await self._write(
            interactive_questions.prompt_is_current,
            room_id=room_id,
            question_event_id=question_event_id,
            expected=expected,
        )

    async def claim_interactive_text(
        self,
        *,
        source_event_id: str,
    ) -> InteractiveSelection | None:
        """Atomically transfer the oldest eligible selection to one text source."""
        return await self._write(interactive_questions.claim_text, source_event_id=source_event_id)

    async def deleted_event_ids(self, room_id: str, event_ids: tuple[str, ...]) -> frozenset[str]:
        """Return which of these events are deleted: redacted, or answering for a relayed message that was."""
        return await self._read(_deleted_events, room_id, event_ids)

    async def later_message_exists(
        self,
        *,
        room_id: str,
        thread_id: str | None,
        source_event_ids: tuple[str, ...],
        excluded_senders: frozenset[str],
        whole_room: bool = False,
    ) -> bool:
        """Return whether someone outside ``excluded_senders`` wrote in the conversation after these sources."""
        return await self._read(
            reads.later_message_exists,
            room_id=room_id,
            thread_id=thread_id,
            source_event_ids=source_event_ids,
            excluded_senders=excluded_senders,
            whole_room=whole_room,
        )


@dataclass(frozen=True, slots=True)
class EventJournalStore:
    """The shared backend, which hands out per-principal views."""

    backend: Backend
    # What this store's committed approval writes tell the process; its backend publishes them here.
    approval_signals: ApprovalSignals

    @classmethod
    def open_sqlite(cls, database_path: Path) -> EventJournalStore:
        """Open a single-writer SQLite store."""
        from .sqlite_backend import SqliteBackend  # noqa: PLC0415 - backend chosen at runtime

        signals = ApprovalSignals()
        return cls(backend=SqliteBackend.open(database_path, publish=signals.publish), approval_signals=signals)

    @classmethod
    def open_postgres(cls, database_url: str) -> EventJournalStore:
        """Open a PostgreSQL store."""
        from .postgres_backend import PostgresBackend  # noqa: PLC0415 - keeps psycopg optional

        signals = ApprovalSignals()
        return cls(backend=PostgresBackend.open(database_url, publish=signals.publish), approval_signals=signals)

    def principal(self, principal_id: str) -> PrincipalStore:
        """Return the bound view for one bot."""
        if not principal_id:
            msg = "An event-journal principal requires an identity"
            raise ValueError(msg)
        return PrincipalStore(
            _backend=self.backend,
            _principal_id=principal_id,
            _approval_signals=self.approval_signals,
        )

    async def load_approval_signals(self) -> None:
        """Load every principal's paused replies into the approval signals, before any bot follows them."""
        await self.backend.write(approval_signals.load)

    async def approval_continuations(
        self,
        *,
        limit: int = _DEFAULT_APPROVAL_CONTINUATION_OWNER_LIMIT,
        after: str | None = None,
    ) -> tuple[tuple[str, ApprovalContinuation], ...]:
        """Return one bounded page of continuations after the approval id ``after``, with their journal principals."""
        return await self.backend.read(
            lambda transaction: approval_continuations.all_owners(
                transaction,
                limit=limit,
                after=after,
            ),
        )

    async def hold_exclusively(self, identity: str) -> bool:
        """Claim this journal for one runtime until it closes; ``False`` when another runtime holds it."""
        return await self.backend.hold_exclusively(identity)

    async def still_usable(self) -> bool:
        """Return whether this runtime's claim on the journal still holds and the journal can still be used."""
        return await self.backend.still_usable()

    async def end_entity_replies(self, ends: Callable[[str], bool], *, now_ns: int) -> int:
        """End the open replies of the entities ``ends`` names; return how many ended."""
        return await self.backend.write(
            lambda transaction: replies.end_entity_replies(transaction, ends, now_ns=now_ns),
        )

    async def generation(self, *, new_generation: str) -> str:
        """Return this database's identity, minting it the first time it is opened.

        Shared across principals rather than per-principal: the thing being
        identified is the database, and every principal in it lost the same
        history if it were replaced.
        """
        return await self.backend.write(
            lambda transaction: journal.store_generation(transaction, new_generation=new_generation),
        )

    async def existing_generation(self) -> str | None:
        """Return this database's identity without minting one, or ``None`` if unused.

        The distinction matters to the caller that decides whether this install
        may use the database at all: a database with no generation has never
        been opened by anything, which is a different operator problem from one
        carrying another install's generation. Minting on the way past would
        erase that difference.
        """
        return await self.backend.read(journal.read_generation)

    def turn_records(self, agent_name: str) -> turn_rows.TurnRecordStore:
        """Return the agent-scoped turn-row view.

        Not a ``PrincipalStore`` method, because turn records are not scoped to
        a Matrix identity. They record that a message was answered, which stays
        true across a re-login; scoping them per principal would make a bot
        that logs in as a new device answer everything a second time.
        """
        if not agent_name:
            msg = "Turn records require an agent name"
            raise ValueError(msg)
        return turn_rows.TurnRecordStore(backend=self.backend, agent_name=agent_name)

    def tool_jobs(self, runtime_generation: str) -> ToolJobStore:
        """Return the background tool jobs as one runtime generation saves them.

        Jobs belong to the install, not to a bot, so this is not a principal view
        either. The generation fences writes: whichever runtime last took
        ownership is the only one whose saves land.
        """
        return ToolJobStore(_backend=self.backend, _runtime_generation=runtime_generation)

    async def close(self) -> None:
        """Release every connection the backend owns."""
        await self.backend.close()


@dataclass(frozen=True, slots=True)
class ToolJobStore:
    """The install's background tool jobs, written only by the runtime generation that last took them over.

    A snapshot and its outcome payload commit together, so a reader never sees
    one without the other, and a crash leaves no payload that nothing references.
    """

    _backend: Backend
    _runtime_generation: str

    async def take_ownership(self) -> None:
        """Become the only writer of every job; an older runtime's later saves are refused."""
        await self._backend.write(
            lambda transaction: tool_jobs.take_ownership(transaction, self._runtime_generation),
        )

    async def require_ownership(self) -> None:
        """Raise ``ToolJobOwnershipLostError`` unless this generation still owns every job."""
        await self._backend.write(
            lambda transaction: tool_jobs.require_ownership(transaction, self._runtime_generation),
        )

    async def load_all(self) -> tuple[SavedToolJob, ...]:
        """Return every saved job."""
        return await self._backend.read(tool_jobs.load_all)

    async def accept(self, job_id: str, job_json: str) -> None:
        """Save a newly accepted job, refusing an ID that is still saved."""
        await self._backend.write(
            lambda transaction: tool_jobs.accept(transaction, self._runtime_generation, job_id, job_json),
        )

    async def save(
        self,
        job_id: str,
        job_json: str,
        result_payload_json: str | None = None,
        *,
        ends_approvals: bool,
    ) -> None:
        """Replace a job's snapshot, with its outcome payload when one is given; a save that ends the job's approvals denies its cards."""
        await self._backend.write(
            lambda transaction: tool_jobs.save(
                transaction,
                self._runtime_generation,
                job_id,
                job_json,
                result_payload_json,
                ends_approvals=ends_approvals,
            ),
        )

    async def load_payload(self, job_id: str) -> str | None:
        """Return a job's outcome payload, or ``None`` when it has none or no longer exists."""
        return await self._backend.read(lambda transaction: tool_jobs.load_payload(transaction, job_id))

    async def delete(self, job_id: str) -> None:
        """Forget a job together with its payload."""
        await self._backend.write(
            lambda transaction: tool_jobs.delete(transaction, self._runtime_generation, job_id),
        )
