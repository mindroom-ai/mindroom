"""The one place the oracle touches the turn store, so a storage rewrite changes this file and no recording.

Every action of the vocabulary is a public ``async`` method here taking plain JSON values, and every observable is a
reader method returning plain JSON. The driver calls nothing else. When ``TurnStore`` reads become awaited queries,
``is_handled`` takes a room, or ``TurnRecord`` loses a field, the methods below change and the recorded goldens stay.
"""

from __future__ import annotations

import json
from contextlib import contextmanager
from dataclasses import dataclass, replace
from typing import TYPE_CHECKING, Any
from unittest.mock import AsyncMock, MagicMock, patch

from mindroom import handled_turns
from mindroom.event_journal import (
    DeliveryStage,
    EventClass,
    EventJournalStore,
    EventKind,
    InboundEvent,
    ProjectedEvent,
    TerminalTurnWrite,
    turn_records,
)
from mindroom.handled_turns import TurnRecordCodec, _reset_handled_turn_ledger_runtime
from mindroom.history.types import HistoryScope
from mindroom.matrix.client_visible_messages import ResolvedVisibleMessage
from mindroom.message_target import MessageTarget
from mindroom.model_selection import command_result_content_to_dict
from mindroom.turn_record import PreparedVoiceSource, SourceEventMetadata, TurnRecord, canonicalize_turn_record
from mindroom.turn_store import TurnStore, TurnStoreDeps
from tests.journal_helpers import admit_room_event

if TYPE_CHECKING:
    from collections.abc import Iterator, Sequence
    from pathlib import Path

AGENT = "agent"
PRINCIPAL = f"{AGENT}@@mindroom_agent:example.org"
REQUESTER = "@alice:example.org"
ROOMS = {"a": "!a:example.org", "b": "!b:example.org"}
TARGETS = {key: MessageTarget.resolve(room_id, f"$thread-{key}", None) for key, room_id in ROOMS.items()}
SCOPE = HistoryScope(kind="agent", scope_id=AGENT)
# Where the wall clock starts; every read of it advances one second, so writes order as they would live.
EPOCH = 1_800_000_000.0
_DAY = 24 * 60 * 60


@dataclass
class Clock:
    """The wall clock the ledger stamps records with and ages them by."""

    now: float = EPOCH

    def time(self) -> float:
        """Return the next second."""
        self.now += 1.0
        return self.now


@contextmanager
def controlled_clock(clock: Clock) -> Iterator[None]:
    """Run the turn store on ``clock`` instead of the wall clock."""
    with patch.object(handled_turns, "time", clock):
        yield


def record_json(record: TurnRecord | None) -> dict[str, Any] | None:
    """Return one record as stable JSON: every fact but the timestamp, set-like lists sorted, ``None`` fields dropped.

    An absent field and an empty one stay distinct (``source_event_metadata`` ``None`` versus ``{}`` is the
    fail-closed attribution rule of #1671).
    """
    if record is None:
        return None
    fields: dict[str, Any] = {
        "sources": list(record.source_event_ids),
        "aliases": sorted(record.discovery_event_ids) or None,
        "redacted": sorted(record.redacted_source_event_ids) or None,
        "anchor": record.anchor_event_id,
        "response": record.response_event_id,
        "completed": record.completed,
        "echo": record.visible_echo_event_id,
        "echo_fallback": record.visible_echo_is_fallback,
        "prompts": _plain(record.source_event_prompts),
        "revisions": _plain(record.source_event_revisions),
        "replay": None
        if record.revision_replay is None
        else {event_id: replay.to_record() for event_id, replay in record.revision_replay.items()},
        "suppressed_revisions": _plain(record.suppressed_source_event_revisions),
        "metadata": None
        if record.source_event_metadata is None
        else {event_id: metadata._to_record() for event_id, metadata in record.source_event_metadata.items()},
        "prepared_voice": None
        if record.prepared_voice_sources is None
        else {event_id: voice.to_record() for event_id, voice in record.prepared_voice_sources.items()},
        "response_owner": record.response_owner,
        "requester": record.requester_id,
        "command_started": record.command_execution_started or None,
        "command_result": record.command_result_text,
        "command_extra": None
        if record.command_result_extra_content is None
        else command_result_content_to_dict(record.command_result_extra_content),
        "history_scope": None if record.history_scope is None else record.history_scope.key,
        "target": None if record.conversation_target is None else record.conversation_target.to_metadata(),
    }
    return {key: value for key, value in fields.items() if value is not None}


def _plain(mapping: object) -> dict[str, Any] | None:
    return None if mapping is None else json.loads(json.dumps(dict(mapping)))  # type: ignore[call-overload]


class TurnStateAdapter:
    """One agent's turn store over a SQLite journal under ``root``, reopened as a restart does."""

    def __init__(self, root: Path, *, legacy_ledger: Path | None = None) -> None:
        self.database = root / "journal.db"
        self.legacy_ledger = legacy_ledger
        self.clock = Clock()
        # The room each event was seen in, for readers that may come to need one.
        self.event_rooms: dict[str, str] = {}

    async def open(self) -> None:
        """Open the journal and a warmed turn store, as startup does."""
        _reset_handled_turn_ledger_runtime()
        self.journal = EventJournalStore.open_sqlite(self.database)
        self.principal = self.journal.principal(PRINCIPAL)
        self.store = TurnStore(
            TurnStoreDeps(
                agent_name=AGENT,
                turn_records=self.journal.turn_records(AGENT),
                redacted_event_ids=self.principal.redacted_event_ids,
                relations=self.principal,
                legacy_responses_file=self.legacy_ledger,
                state_writer=MagicMock(),
                resolver=MagicMock(),
                tool_runtime=MagicMock(),
                response_recovery_state=AsyncMock(),
                retry_dispatch_sources=MagicMock(),
            ),
        )
        await self.store.warm()

    async def close(self) -> None:
        """Close the journal and drop the process's shared ledger state."""
        await self.journal.close()
        _reset_handled_turn_ledger_runtime()

    # Actions. Each returns its outcome as plain JSON.

    async def admit(self, events: Sequence[str], room: str) -> str:
        """Admit messages to the journal, as sync does before any turn or redaction names them."""
        for event_id in events:
            await admit_room_event(self.principal, ROOMS[room], event_id)
            self.event_rooms.setdefault(event_id, ROOMS[room])
        return "ok"

    async def pending(self, sources: Sequence[str], **turn: Any) -> str:  # noqa: ANN401
        """Record a pending turn with its response context, as ingress does before generation."""
        recorded = await self.store.record_pending_turn(self._turn(sources, completed=False, **turn))
        if recorded is None:
            return "declined"
        return "already_completed" if recorded.completed else "ok"

    async def terminal(self, sources: Sequence[str], aliases: Sequence[str] = (), room: str | None = None) -> str:
        """Record a bare terminal turn, as ingress validation and the no-response path do."""
        record = TurnRecord.create(sources, discovery_event_ids=aliases)
        if room is not None:
            record = self._with_context(canonicalize_turn_record(record, requester_id=REQUESTER), room)
        await self.store.record_turn(record)
        return "ok"

    async def responded(self, sources: Sequence[str], response: str, **turn: Any) -> str:  # noqa: ANN401
        """Record a turn answered by a visible response."""
        await self.store.record_responded_turn(self._turn(sources, completed=True, response=response, **turn))
        return "ok"

    async def edit(self, source: str, edit: str, ts: int, *, commit: bool = True) -> str:
        """Register an edit's revision and, as its regeneration claims the reply, commit its text, like the regenerator."""
        registered = await self.store.register_edit_revision(source, (ts, edit))
        if registered is None:
            return "declined"
        replay = (registered.revision_replay or {}).get(edit)
        if replay is not None and replay.redacted:
            return "redacted"
        if commit:
            await self.store.record_edit(_edited(registered, source, edit, ts))
        return "ok"

    async def record_edit(self, source: str, edit: str, ts: int) -> str:
        """Commit an edit's text from the current record without registering it, as a late regeneration does."""
        current = self.store.get_turn_record(source)
        if current is None:
            return "no_record"
        await self.store.record_edit(_edited(current, source, edit, ts))
        return "ok"

    async def prepare(
        self,
        room: str,
        sources: Sequence[str],
        terminal: Sequence[str] | None = None,
        history: Sequence[tuple[str, str | None, int]] = (),
    ) -> str:
        """Prepare a locked response: register the history's edits as context, reconcile redactions, check sources."""
        thread_history = [
            replace(
                ResolvedVisibleMessage.synthetic(sender=REQUESTER, body=event_id, event_id=event_id),
                latest_event_id=latest or event_id,
                edited_timestamp=ts if latest else None,
            )
            for event_id, latest, ts in history
        ]
        suppressed = await self.store.prepare_pending_response_source(
            target=TARGETS[room],
            source_event_ids=tuple(sources),
            terminal_source_event_ids=tuple(sources if terminal is None else terminal),
            thread_history=thread_history,
        )
        return "suppressed" if suppressed else "proceed"

    async def voice(self, source: str, body: str) -> str:
        """Checkpoint a voice source's normalized text."""
        prepared = PreparedVoiceSource.from_content(body, {"msgtype": "m.text", "body": body})
        return "ok" if await self.store.record_prepared_voice(source, prepared) is not None else "declined"

    async def echo(self, source: str, echo: str) -> str:
        """Track a visible voice echo."""
        await self.store.record_visible_echo(source, echo)
        return "ok"

    async def finalize_echo(self, source: str, echo: str, *, fallback: bool) -> str:
        """Mark a tracked echo replaced by a transcript or a fallback."""
        await self.store.record_finalized_visible_echo(source, echo, is_fallback=fallback)
        return "ok"

    async def redact(self, target: str, room: str, via: str = "live") -> str:
        """Admit a redaction to the journal, then, when it arrives live, run the redaction callback.

        ``live`` is ``bot._on_redaction``. ``journal`` is a redaction of cold history or hydration, which no
        callback follows; the next response preparation reconciles it.
        """
        room_id = ROOMS[room]
        redaction_id = f"$redaction-of-{target.removeprefix('$')}-in-{room}"
        await self.principal.admit(
            InboundEvent(
                event_id=redaction_id,
                room_id=room_id,
                thread_id=None,
                kind=EventKind.REDACTION,
                event_class=EventClass.ACTIONABLE if via == "live" else EventClass.CONTEXT_ONLY,
                sender=REQUESTER,
                origin_server_ts=0,
                source={},
            ),
            ProjectedEvent(
                event_id=redaction_id,
                room_id=room_id,
                thread_id=None,
                sender=REQUESTER,
                origin_server_ts=0,
                content={},
                replaces_event_id=None,
                redacts_event_id=target,
            ),
        )
        if via == "journal":
            return "admitted"
        return "tombstoned" if await self.store.mark_source_redacted(target, room_id=room_id) is not None else "ignored"

    async def final_ack(self, turn: str, response: str, *, publish: bool = True) -> dict[str, object]:
        """Acknowledge a turn's FINAL delivery with its terminal record, then publish it, as the delivery gateway does.

        The delivery is named by the turn's anchor, as the outbox keys it. ``publish=False`` stops after the commit,
        the window before ``publish_completed_turn`` runs.
        """
        current = self.store.get_turn_record(turn)
        room_id = current.conversation_target.room_id if current and current.conversation_target else ROOMS["a"]
        await self.principal.enqueue_matrix_delivery(
            delivery_id=turn,
            stage=DeliveryStage.FINAL,
            room_id=room_id,
            thread_id=None,
            payload={"msgtype": "m.text", "body": "answer"},
        )
        prepared = self.store.terminal_turn_record(turn, response)
        terminal = (
            None
            if prepared is None or prepared.anchor_event_id is None
            else TerminalTurnWrite(
                agent_name=AGENT,
                index_event_ids=prepared.indexed_event_ids,
                anchor_event_id=prepared.anchor_event_id,
                record_json=json.dumps(TurnRecordCodec._to_ledger_record(prepared)),
            )
        )
        acknowledgement = await self.principal.acknowledge_matrix_delivery(
            delivery_id=turn,
            stage=DeliveryStage.FINAL,
            event_id=response,
            delivered_projections=(),
            terminal_turn=terminal,
        )
        committed = acknowledgement.terminal_turn
        if committed is not None and publish:
            record = TurnRecordCodec._from_ledger_record(
                committed.index_event_ids[0],
                json.loads(committed.record_json),
            )
            assert record is not None
            await self.store.publish_completed_turn(record)
        return {
            "prepared": prepared is not None,
            "committed": committed is not None,
            "settled": acknowledgement.settled_event_id,
        }

    async def settle(self, sources: Sequence[str], *, publish: bool = True) -> str:
        """Settle a reply's sources and complete their turn in one transaction, as a reply's ``SettleSources`` does."""
        completed = await self.journal.backend.write(
            lambda transaction: turn_records.settle_turn(
                transaction,
                PRINCIPAL,
                AGENT,
                pending=tuple(sources),
                logical=tuple(sources),
            ),
        )
        if completed is None:
            return "unchanged"
        if publish:
            await self.store.publish_completed_turn(completed)
        return "completed"

    async def cleanup(self, days: int = 0, unsettled: Sequence[str] = ()) -> str:
        """Let ``days`` pass, then run retention with these journal sources still unsettled."""
        self.clock.now += days * _DAY
        await self.store.cleanup(unsettled_source_event_ids=tuple(unsettled))
        return "ok"

    async def reopen(self) -> str:
        """Restart: a fresh journal store and turn store over the same database."""
        await self.close()
        await self.open()
        return "ok"

    # Readers. Each returns plain JSON for one event id, or one room's or conversation's set of them.

    async def is_handled(self, event_id: str) -> bool:
        """Return whether the event has a terminal outcome."""
        return self.store.is_handled(event_id)

    async def turn_record(self, event_id: str) -> dict[str, Any] | None:
        """Return the record the event indexes."""
        return record_json(self.store.get_turn_record(event_id))

    async def visible_echo(self, event_id: str) -> str | None:
        """Return the visible echo tracked for a source."""
        return self.store.visible_echo_for_source(event_id)

    async def finalized_echo(self, event_id: str) -> dict[str, object] | None:
        """Return a source's replaced echo and whether a fallback replaced it."""
        finalized = self.store.finalized_visible_echo(event_id)
        return None if finalized is None else {"event_id": finalized.event_id, "is_fallback": finalized.is_fallback}

    async def pending_intent(self, event_id: str) -> bool:
        """Return whether the source owns an incomplete response attempt."""
        return self.store.has_pending_response_intent((event_id,))

    async def record_for_response(self, event_id: str) -> dict[str, Any] | None:
        """Return the turn whose visible response or echo is this event."""
        return record_json(self.store.turn_record_for_response_event_id(event_id))

    async def prepared_voice(self, event_id: str) -> dict[str, object] | None:
        """Return a source's voice checkpoint."""
        prepared = self.store.prepared_voice_for_source(event_id)
        return None if prepared is None else prepared.to_record()

    async def redacted_event_ids(self, room: str, event_ids: Sequence[str]) -> list[str]:
        """Return which of these events background readers treat as redacted in one room."""
        return sorted(await self.store.redacted_event_ids(ROOMS[room], tuple(event_ids)))

    async def redacted_history_events(self, conversation: str, event_ids: Sequence[str]) -> dict[str, str | None]:
        """Return which of these events one conversation's history cleanup removes, with their legacy summary source."""
        return dict(sorted((await self.store.redacted_history_events(TARGETS[conversation], tuple(event_ids))).items()))

    def _turn(
        self,
        sources: Sequence[str],
        *,
        completed: bool,
        response: str | None = None,
        aliases: Sequence[str] = (),
        room: str | None = None,
        senders: Sequence[str | None] | None = None,
        relays: dict[str, str] | None = None,
        revisions: dict[str, tuple[str, int]] | None = None,
    ) -> TurnRecord:
        """Build the record a turn carries.

        ``senders`` lists each source's sender (``None`` skips one; ``[]`` is an explicit empty map, ``None`` no map),
        ``relays`` names the discovery alias a relayed source answers for, and ``revisions`` the edit each source's
        prompt was taken from.
        """
        selected = {source: (ts, edit) for source, (edit, ts) in (revisions or {}).items()}
        metadata = None
        if senders is not None or relays:
            metadata = {
                source: SourceEventMetadata(sender=sender, discovery_event_id=(relays or {}).get(source))
                for source, sender in zip(sources, senders if senders is not None else [REQUESTER] * len(sources))
                if sender is not None
            }
        record = TurnRecord.create(
            sources,
            discovery_event_ids=aliases,
            response_event_id=response,
            completed=completed,
            requester_id=REQUESTER,
            source_event_prompts={source: _prompt(source, selected.get(source)) for source in sources},
            source_event_revisions=selected or None,
            source_event_metadata=metadata,
        )
        return record if room is None else self._with_context(record, room)

    def _with_context(self, record: TurnRecord, room: str) -> TurnRecord:
        for event_id in record.indexed_event_ids:
            self.event_rooms.setdefault(event_id, ROOMS[room])
        return self.store.attach_response_context(record, history_scope=SCOPE, conversation_target=TARGETS[room])


def _prompt(source: str, revision: tuple[int, str] | None) -> str:
    return f"{source} as of {revision[1]}" if revision is not None else f"{source} original"


def _edited(record: TurnRecord, source: str, edit: str, ts: int) -> TurnRecord:
    """Return ``record`` carrying one source's edited text, as the edit regenerator builds it."""
    return canonicalize_turn_record(
        record,
        source_event_prompts={
            **(record.source_event_prompts or {}),
            record.prompt_source_event_id(source): _prompt(source, (ts, edit)),
        },
        source_event_revisions={**(record.source_event_revisions or {}), source: (ts, edit)},
    )


# The writers a plain-data action may name, and the readers an observation runs.
ACTIONS = frozenset(
    {
        "admit",
        "pending",
        "terminal",
        "responded",
        "edit",
        "record_edit",
        "prepare",
        "voice",
        "echo",
        "finalize_echo",
        "redact",
        "final_ack",
        "settle",
        "cleanup",
        "reopen",
    },
)
PER_EVENT_READERS = (
    "is_handled",
    "turn_record",
    "visible_echo",
    "finalized_echo",
    "pending_intent",
    "record_for_response",
    "prepared_voice",
)
