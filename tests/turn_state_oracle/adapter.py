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

from mindroom import turn_store
from mindroom.event_journal import (
    DeliveryStage,
    EventClass,
    EventJournalStore,
    EventKind,
    InboundEvent,
    ProjectedEvent,
    TerminalTurnWrite,
    journal,
    turn_rows,
)
from mindroom.event_journal.legacy_turn_rows import legacy_summary_sources
from mindroom.history.types import HistoryScope
from mindroom.matrix.client_visible_messages import ResolvedVisibleMessage
from mindroom.message_target import MessageTarget
from mindroom.model_selection import command_result_content_to_dict
from mindroom.turn_record import PreparedVoiceSource, SourceEventMetadata, TurnRecord, canonicalize_turn_record
from mindroom.turn_store import TurnStore, TurnStoreDeps, _reset_turn_claim_registries
from tests.journal_helpers import admit_room_event

if TYPE_CHECKING:
    from collections.abc import Iterator, Sequence
    from pathlib import Path

    from mindroom.event_journal.backend import Transaction

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
    with patch.object(turn_rows, "time", clock), patch.object(turn_store, "time", clock):
        yield


def _metadata_json(metadata: SourceEventMetadata) -> dict[str, object]:
    """Return one source's attribution in the recordings' shape, naming only the facts it holds."""
    fields = {
        "sender": metadata.sender,
        "timestamp_ms": metadata.timestamp_ms,
        "discovery_event_id": metadata.discovery_event_id,
        "speaker": metadata.speaker,
    }
    return {name: value for name, value in fields.items() if name == "sender" or value is not None}


def record_json(record: TurnRecord | None) -> dict[str, Any] | None:
    """Return one record as stable JSON: every fact it carries, set-like lists sorted, ``None`` fields dropped.

    An absent field and an empty one stay distinct (``source_event_metadata`` ``None`` versus ``{}`` is the
    fail-closed attribution rule of #1671). The turn key is a random identity and is left out.
    """
    if record is None:
        return None
    fields: dict[str, Any] = {
        "sources": list(record.source_event_ids),
        "aliases": sorted(record.discovery_event_ids) or None,
        "anchor": record.anchor_event_id,
        "response": record.response_event_id,
        "completed": record.completed,
        "prompts": _plain(record.source_event_prompts),
        "revisions": _plain(record.source_event_revisions),
        "replay": None
        if record.revision_replay is None
        else {
            event_id: {
                "source_event_id": replay.source_event_id,
                "timestamp_ms": replay.timestamp_ms,
                "response_event_id": replay.response_event_id,
            }
            for event_id, replay in record.revision_replay.items()
        },
        "metadata": None
        if record.source_event_metadata is None
        else {event_id: _metadata_json(metadata) for event_id, metadata in record.source_event_metadata.items()},
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
        """Open the journal and a started turn store, as startup does."""
        _reset_turn_claim_registries()
        self.journal = EventJournalStore.open_sqlite(self.database)
        self.principal = self.journal.principal(PRINCIPAL)
        self.store = TurnStore(
            TurnStoreDeps(
                agent_name=AGENT,
                turn_records=self.journal.turn_records(AGENT),
                legacy_responses_file=self.legacy_ledger,
                state_writer=MagicMock(),
                tool_runtime=MagicMock(),
                response_recovery_state=AsyncMock(),
                retry_dispatch_sources=MagicMock(),
            ),
        )
        await self.store.warm()
        # A converted turn's sources were seen in its room before any action named them.
        rows = await self.journal.backend.read(
            lambda transaction: transaction.fetchall(
                "SELECT source.event_id, turn.room_id FROM turn_sources AS source JOIN turns AS turn "
                "ON turn.agent_name = source.agent_name AND turn.turn_key = source.turn_key "
                "WHERE source.agent_name = ? AND turn.room_id IS NOT NULL",
                (AGENT,),
            ),
        )
        for row in rows:
            self.event_rooms.setdefault(str(row["event_id"]), str(row["room_id"]))

    async def close(self) -> None:
        """Close the journal and drop the process's live claims."""
        await self.journal.close()
        _reset_turn_claim_registries()

    def _room_of(self, event_id: str) -> str:
        """Return the room an event was seen or recorded in, as the runtime always knows it."""
        return self.event_rooms.get(event_id, ROOMS["a"])

    # Actions. Each returns its outcome as plain JSON.

    async def admit(self, events: Sequence[str], room: str) -> str:
        """Admit messages to the journal, as sync does before any turn or redaction names them."""
        for event_id in events:
            await admit_room_event(self.principal, ROOMS[room], event_id)
            self.event_rooms.setdefault(event_id, ROOMS[room])
        return "ok"

    async def pending(self, sources: Sequence[str], **turn: Any) -> str:  # noqa: ANN401
        """Record a pending turn with its response context, as ingress does before generation."""
        record = self._turn(sources, completed=False, **turn)
        room_id = (
            record.conversation_target.room_id
            if record.conversation_target is not None
            else self._room_of(record.source_event_ids[0])
        )
        result = await self.store.deps.turn_records.write(turn_rows.record_pending_turn, record, room_id=room_id)
        if isinstance(result, turn_rows.Refused):
            return "declined"
        return "already_completed" if isinstance(result, turn_rows.Answered) else "ok"

    async def terminal(self, sources: Sequence[str], aliases: Sequence[str] = (), room: str | None = None) -> str:
        """Record a bare terminal turn, as ingress validation and the no-response path do."""
        record = TurnRecord.create(sources, discovery_event_ids=aliases)
        if room is not None:
            record = self._with_context(canonicalize_turn_record(record, requester_id=REQUESTER), room)
        await self.store.record_turn(record, room_id=ROOMS[room] if room else self._room_of(sources[0]))
        return "ok"

    async def responded(self, sources: Sequence[str], response: str, **turn: Any) -> str:  # noqa: ANN401
        """Record a turn answered by a visible response."""
        record = self._turn(sources, completed=True, response=response, **turn)
        await self.store.record_responded_turn(record, room_id=self._room_of(sources[0]))
        return "ok"

    async def edit(self, source: str, edit: str, ts: int, *, commit: bool = True) -> str:
        """Register an edit's revision and, as its regeneration claims the reply, commit its text, like the regenerator."""
        loaded = await self.store.get_turn_record_after_writes(source)
        if loaded is None:
            return "declined"
        registered = await self.store.register_edit_revision(loaded, source, (ts, edit))
        if registered is None:
            return "declined"
        if await self.store.redacted_event_ids(self._room_of(source), (edit,)):
            return "redacted"
        if commit:
            await self.store.record_edit(
                loaded,
                edited_event_id=source,
                prompt=_prompt(source, (ts, edit)),
                revision=(ts, edit),
            )
        return "ok"

    async def record_edit(self, source: str, edit: str, ts: int) -> str:
        """Commit an edit's text to the current turn without registering it first, as a late regeneration does."""
        current = await self.store.get_turn_record(source)
        if current is None:
            return "no_record"
        await self.store.record_edit(
            current,
            edited_event_id=source,
            prompt=_prompt(source, (ts, edit)),
            revision=(ts, edit),
        )
        return "ok"

    async def prepare(
        self,
        room: str,
        sources: Sequence[str],
        terminal: Sequence[str] | None = None,
        history: Sequence[tuple[str, str | None, int]] = (),
    ) -> str:
        """Prepare a locked response: register the history's edits as context, then check its sources."""
        thread_history = [
            replace(
                ResolvedVisibleMessage.synthetic(sender=REQUESTER, body=event_id, event_id=event_id),
                latest_event_id=latest or event_id,
                edited_timestamp=ts if latest else None,
            )
            for event_id, latest, ts in history
        ]
        preparation = await self.store.prepare_pending_response_source(
            target=TARGETS[room],
            source_event_ids=tuple(sources),
            terminal_source_event_ids=tuple(sources if terminal is None else terminal),
            thread_history=thread_history,
        )
        return "suppressed" if preparation.invalid else "proceed"

    async def voice(self, source: str, body: str) -> str:
        """Checkpoint a voice source's normalized text."""
        prepared = PreparedVoiceSource.from_content(body, {"msgtype": "m.text", "body": body})
        recorded = await self.store.record_prepared_voice(self._room_of(source), source, prepared)
        return "ok" if recorded is not None else "declined"

    async def echo(self, source: str, echo: str) -> str:
        """Track a visible voice echo."""
        await self.store.record_visible_echo(self._room_of(source), source, echo)
        return "ok"

    async def finalize_echo(self, source: str, echo: str, *, fallback: bool) -> str:
        """Mark a tracked echo replaced by a transcript or a fallback."""
        await self.store.record_finalized_visible_echo(source, echo, is_fallback=fallback)
        return "ok"

    async def redact(self, target: str, room: str, via: str = "live") -> str:
        """Admit a redaction to the journal, which records it for the agent and applies it in one transaction.

        ``live`` is a redaction sync delivers; ``journal`` one of cold history or hydration. Both take effect at
        admission; a live one then runs the bot's callback, which only ends replies.
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
        return "tombstoned" if await self.store.redacted_event_ids(room_id, (target,)) else "ignored"

    async def final_ack(self, turn: str, response: str, *, publish: bool = True) -> dict[str, object]:  # noqa: ARG002
        """Hand a turn's answer to its FINAL delivery and acknowledge it, as the delivery gateway does.

        The delivery is named by the turn's anchor, as the outbox keys it, and binds the turn its producer recorded.
        The acknowledgement completes the bound turn in its own transaction, so nothing is published after it.
        """
        current = await self.store.get_turn_record(turn)
        room_id = current.conversation_target.room_id if current and current.conversation_target else ROOMS["a"]
        await self.principal.enqueue_final_answer(
            delivery_id=turn,
            room_id=room_id,
            thread_id=None,
            payload={"msgtype": "m.text", "body": "answer"},
            turn_key=None if current is None else current.turn_key,
        )
        acknowledgement = await self.principal.acknowledge_matrix_delivery(
            delivery_id=turn,
            stage=DeliveryStage.FINAL,
            event_id=response,
            delivered_projections=(),
            terminal_turn=TerminalTurnWrite(agent_name=AGENT, delivery_id=turn, response_event_id=response),
        )
        after = await self.store.get_turn_record(turn)
        return {
            "prepared": current is not None and not current.completed,
            "committed": current is not None and not current.completed and after is not None and after.completed,
            "settled": acknowledgement.settled_event_id,
        }

    async def settle(self, sources: Sequence[str], *, publish: bool = True) -> str:  # noqa: ARG002
        """Settle a reply's sources and complete their turn in one transaction, as a reply's ``SettleSources`` does."""

        def settle(transaction: Transaction) -> TurnRecord | None:
            journal.settle_many(transaction, PRINCIPAL, tuple(sources))
            return turn_rows.complete_turn_of(transaction, AGENT, tuple(sources))

        completed = await self.journal.backend.write(settle)
        return "unchanged" if completed is None else "completed"

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
        """Return whether the event has a terminal outcome in the room it was seen in."""
        return await self.store.is_handled(self._room_of(event_id), event_id)

    async def turn_record(self, event_id: str) -> dict[str, Any] | None:
        """Return the turn the event answers to, with the redaction and echo facts its record used to carry."""
        return await self._legacy_view(await self.store.get_turn_record(event_id))

    async def visible_echo(self, event_id: str) -> str | None:
        """Return the visible echo tracked for a source."""
        return await self.store.visible_echo_for_source(event_id)

    async def finalized_echo(self, event_id: str) -> dict[str, object] | None:
        """Return a source's replaced echo and whether a fallback replaced it."""
        finalized = await self.store.finalized_visible_echo(event_id)
        return None if finalized is None else {"event_id": finalized.event_id, "is_fallback": finalized.is_fallback}

    async def pending_intent(self, event_id: str) -> bool:
        """Return whether the source owns an incomplete response attempt."""
        return await self.store.deps.turn_records.read(turn_store._has_pending_response_intent, (event_id,))

    async def record_for_response(self, event_id: str) -> dict[str, Any] | None:
        """Return the turn whose visible response or echo is this event."""
        return await self._legacy_view(await self.store.turn_record_for_response_event_id(event_id))

    async def prepared_voice(self, event_id: str) -> dict[str, object] | None:
        """Return a source's voice checkpoint."""
        prepared = await self.store.prepared_voice_for_source(event_id)
        return None if prepared is None else prepared.to_record()

    async def redacted_event_ids(self, room: str, event_ids: Sequence[str]) -> list[str]:
        """Return which of these events background readers treat as redacted in one room."""
        return sorted(await self.store.redacted_event_ids(ROOMS[room], tuple(event_ids)))

    async def redacted_history_events(self, conversation: str, event_ids: Sequence[str]) -> dict[str, str | None]:
        """Return which of these events one conversation's history cleanup removes, with their legacy summary source."""
        return dict(sorted((await self.store.redacted_history_events(TARGETS[conversation], tuple(event_ids))).items()))

    async def _legacy_view(self, record: TurnRecord | None) -> dict[str, Any] | None:
        """Return ``record_json`` with the facts a stored record used to carry, read from where they live now.

        Redacted ids of the turn and redacted revisions come from the agent's redactions in the turn's room, legacy
        summary provenance from the conversion's mapping, and the turn-wide echo from its first echoed source.
        """
        view = record_json(record)
        if record is None or view is None:
            return view
        room_id = (
            record.conversation_target.room_id
            if record.conversation_target is not None
            else self._room_of(record.source_event_ids[0])
        )
        replay = record.revision_replay or {}
        redacted = await self.store.redacted_event_ids(room_id, (*record.indexed_event_ids, *replay))
        summaries = await self.store.deps.turn_records.read(legacy_summary_sources, room_id, tuple(replay))
        if indexed := sorted(redacted.intersection(record.indexed_event_ids)):
            view["redacted"] = indexed
        for revision_id, entry in view.get("replay", {}).items():
            entry["redacted"] = revision_id in redacted
            entry["legacy_summary_provenance"] = revision_id in summaries
        echo = next(
            (echo for source in record.source_event_ids if (echo := (record.visible_echoes or {}).get(source))),
            None,
        )
        if echo is not None:
            view["echo"] = echo.event_id
            if echo.is_fallback is not None:
                view["echo_fallback"] = echo.is_fallback
        return dict(sorted(view.items()))

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
