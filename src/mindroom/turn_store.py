"""Unified durable turn ownership for runtime flows.

Every fact about a turn is a row in the journal's database (``event_journal.turn_rows``),
and every read here is an awaited query of it. What stays in memory is only who is working
on a turn right now: the claim registry every ``TurnStore`` of one agent shares.
"""

from __future__ import annotations

import time
from dataclasses import asdict, dataclass, field
from functools import partial
from typing import TYPE_CHECKING, Any

from mindroom import constants
from mindroom.background_tasks import run_blocking_until_complete
from mindroom.event_journal import turn_rows
from mindroom.event_journal.legacy_turn_rows import import_legacy_ledger_file, legacy_summary_sources
from mindroom.history.storage import remove_history_of_redacted_events, remove_runs_answering
from mindroom.logging_config import get_logger
from mindroom.turn_record import (
    PreparedVoiceSource,
    RevisionSnapshotChangedError,
    SourceEventRevision,
    TurnRecord,
    canonicalize_turn_record,
)

if TYPE_CHECKING:
    from collections.abc import Awaitable, Callable, Collection, Sequence
    from pathlib import Path

    from mindroom.conversation_state_writer import ConversationStateWriter
    from mindroom.event_journal.backend import Transaction
    from mindroom.event_journal.models import ResponseRecoveryState
    from mindroom.history.types import HistoryScope
    from mindroom.matrix.client_visible_messages import ResolvedVisibleMessage
    from mindroom.message_target import MessageTarget
    from mindroom.session_storage.store import ConversationStore
    from mindroom.tool_system.runtime_context import ToolRuntimeSupport
    from mindroom.turn_policy import ResponseAction

logger = get_logger(__name__)
# Retention keeps a turn this long, and at most this many, unless it still owns unfinished work.
_RETENTION_MAX_AGE_SECONDS = 30 * 24 * 60 * 60
_RETENTION_MAX_TURNS = 10_000


@dataclass(frozen=True)
class TurnStoreDeps:
    """Collaborators needed to read and write durable turn state."""

    agent_name: str
    # Where "has this turn finished?" is stored. Agent-scoped rather than
    # principal-scoped: the answer stays true across a re-login, and a bot that
    # lost it would answer every outstanding message a second time.
    turn_records: turn_rows.TurnRecordStore
    # The JSON ledger this agent used before its records moved into the
    # database, imported once on first load. An installation that has been
    # answering messages keeps all of its terminal truth there, and a runtime
    # that ignored it would re-answer the entire backlog on upgrade.
    legacy_responses_file: Path | None
    state_writer: ConversationStateWriter
    tool_runtime: ToolRuntimeSupport
    # One consistent read of everything that can still own a response after its
    # task exits: the journal, the outbox, reply rows, and the stored turns.
    response_recovery_state: Callable[[TurnRecord], Awaitable[ResponseRecoveryState]]
    # Hands sources back to the journal: those a claim contention deferred, once their
    # blocking claim releases, and those a refused batch merge leaves unowned.
    retry_dispatch_sources: Callable[[str, tuple[str, ...]], None]


@dataclass(eq=False)
class TurnClaim:
    """One live owner of a turn's sources and discovery aliases in this process.

    The handle is the identity: releasing it removes exactly this owner, so a
    stale release can never drop another owner's claim on the same events.
    """

    source_event_ids: frozenset[str]
    discovery_event_ids: frozenset[str]
    _registry: _ClaimRegistry = field(repr=False)

    def release(self) -> None:
        """Give the claimed events up; a released or replaced handle does nothing."""
        self._registry.release(self)


@dataclass(frozen=True)
class ClaimRefused:
    """No owner may claim this turn: it has no sources, or a completed turn of another identity owns an alias."""


@dataclass(frozen=True)
class ClaimContended:
    """A live claim already owns one of the turn's sources or aliases."""

    blocking: TurnClaim


@dataclass(frozen=True)
class _DeferredSource:
    """A source whose claim lost to a live owner, handed back to the journal once that owner releases."""

    room_id: str
    source_event_id: str
    retry: Callable[[str, tuple[str, ...]], None]


@dataclass(eq=False)
class _ClaimRegistry:
    """The live claims of one agent's turns, shared by every ``TurnStore`` of that agent in this process.

    A runtime replacement or re-login builds a second store for the same agent
    while the first may still own turns; a registry per store would let both
    run one turn. Claims are touched only on the event loop, so each operation
    below is atomic.
    """

    _live: dict[TurnClaim, list[_DeferredSource]] = field(default_factory=dict)

    def blocking_claim(
        self,
        source_event_ids: frozenset[str],
        discovery_event_ids: frozenset[str],
        *,
        excluded: Collection[TurnClaim] = (),
    ) -> TurnClaim | None:
        """Return a live claim sharing a source with these sources or an alias with these aliases.

        Roles are compared separately: a human message and the router's relay of
        it, which indexes that message as its alias, are different turns.
        """
        return next(
            (
                claim
                for claim in self._live
                if claim not in excluded
                and (
                    not source_event_ids.isdisjoint(claim.source_event_ids)
                    or not discovery_event_ids.isdisjoint(claim.discovery_event_ids)
                )
            ),
            None,
        )

    def register(self, source_event_ids: frozenset[str], discovery_event_ids: frozenset[str]) -> TurnClaim:
        """Publish one new live claim."""
        claim = TurnClaim(source_event_ids, discovery_event_ids, self)
        self._live[claim] = []
        return claim

    def replace(self, claims: Sequence[TurnClaim], replacement: TurnClaim) -> None:
        """Retire live claims a replacement covers, moving their deferred sources onto it."""
        for claim in claims:
            self._live[replacement].extend(self._live.pop(claim, ()))

    def release(self, claim: TurnClaim) -> None:
        """Drop one live claim and hand back every source that waited for it."""
        for deferred in self._live.pop(claim, ()):
            deferred.retry(deferred.room_id, (deferred.source_event_id,))

    def retry_when_released(self, blocking: TurnClaim, deferred: _DeferredSource) -> None:
        """Hand one source back once ``blocking`` releases, or now if it already has."""
        waiting = self._live.get(blocking)
        if waiting is None:
            deferred.retry(deferred.room_id, (deferred.source_event_id,))
            return
        waiting.append(deferred)

    def owns(self, event_id: str) -> bool:
        """Return whether a live claim indexes this event or holds it deferred."""
        return any(
            event_id in claim.source_event_ids
            or event_id in claim.discovery_event_ids
            or any(deferred.source_event_id == event_id for deferred in waiting)
            for claim, waiting in self._live.items()
        )


_CLAIM_REGISTRIES: dict[str, _ClaimRegistry] = {}


def _shared_claim_registry(store_key: str, agent_name: str) -> _ClaimRegistry:
    """Return the claim registry every ``TurnStore`` of one agent over one database shares.

    Keyed by the database as well as the agent, so stores over two databases
    never see each other's claims.
    """
    return _CLAIM_REGISTRIES.setdefault(f"{store_key}\x00{agent_name}", _ClaimRegistry())


def _reset_turn_claim_registries() -> None:
    """Drop every live claim (tests and forked runtimes)."""
    _CLAIM_REGISTRIES.clear()


@dataclass(frozen=True)
class _TurnMismatchCounts:
    """How the stored turn records disagree with a turn's acknowledged final answer."""

    missing_completed_turn_count: int
    incomplete_completed_turn_count: int
    source_identity_mismatch_count: int
    anchor_mismatch_count: int
    response_event_mismatch_count: int


@dataclass(frozen=True)
class _RecoveryGap:
    """Why a response is neither complete nor still durably owned, with the counts its diagnostic logs."""

    reason: str
    pending_source_count: int | None = 0
    turn_mismatches: _TurnMismatchCounts | None = None


@dataclass(frozen=True)
class _FinalizedVisibleEcho:
    """Durable terminal state for one editable visible echo."""

    event_id: str
    is_fallback: bool


@dataclass(frozen=True)
class PendingTurn:
    """A turn whose response context is stored, with what the caller must reconcile before it answers.

    ``resumes_response`` means an earlier attempt of these sources already owned
    a response, so a visible one may already exist. ``deleted_sources`` names the
    sources deleted, directly or through the message they answer for, when the
    context was stored.
    """

    record: TurnRecord
    resumes_response: bool
    deleted_sources: frozenset[str]


@dataclass(frozen=True)
class SourcePreparation:
    """Which of a pending response's sources became terminal while it waited, and which still owe an answer.

    A source is invalid once it, or the message it answers for, was deleted,
    or another turn answered it.
    """

    invalid: tuple[str, ...] = ()
    surviving: tuple[str, ...] = ()


@dataclass
class TurnStore:
    """Own durable turn state for one entity, read and written as rows."""

    deps: TurnStoreDeps
    _claims: _ClaimRegistry = field(init=False, repr=False)
    _recovery_diagnostic_classes: set[tuple[str, tuple[str, ...], str | None]] = field(
        default_factory=set,
        init=False,
        repr=False,
    )

    def __post_init__(self) -> None:
        """Bind the agent's shared claims."""
        self._claims = _shared_claim_registry(self.deps.turn_records.state_key, self.deps.agent_name)

    async def warm(self) -> None:
        """Import the agent's pre-journal ledger file once, before anything can ask about its turns."""
        await import_legacy_ledger_file(self.deps.turn_records, path=self.deps.legacy_responses_file)

    async def cleanup(self, *, unsettled_source_event_ids: Collection[str] = ()) -> None:
        """Drop old turns that own no unfinished work, after startup recovery identifies live sources."""
        result = await self.deps.turn_records.write(
            turn_rows.retain,
            now=time.time(),
            max_age_seconds=_RETENTION_MAX_AGE_SECONDS,
            max_turns=_RETENTION_MAX_TURNS,
            unsettled_event_ids=frozenset(unsettled_source_event_ids),
        )
        if result.dropped_turn_count or result.cleared_voice_turn_count:
            logger.info(
                "turn_retention",
                agent=self.deps.agent_name,
                dropped_turn_count=result.dropped_turn_count,
                cleared_voice_turn_count=result.cleared_voice_turn_count,
            )

    # Reads.

    async def get_turn_record(self, event_id: str) -> TurnRecord | None:
        """Return the turn one source or alias answers to: a completed owner first, then its own turn."""
        return await self.deps.turn_records.read(turn_rows.get_turn_record, event_id)

    async def get_turn_record_after_writes(self, original_event_id: str) -> TurnRecord | None:
        """Return the turn a message belongs to, ordered after every write already queued in this process."""
        return await self.deps.turn_records.write(turn_rows.get_turn_record, original_event_id)

    async def is_handled(self, room_id: str, event_id: str) -> bool:
        """Return whether one event is answered, or was redacted in ``room_id``."""
        return bool(await self.handled_among(room_id, (event_id,)))

    async def handled_among(self, room_id: str, event_ids: Collection[str]) -> frozenset[str]:
        """Return which of these events are answered, or were redacted in ``room_id``."""
        if not event_ids:
            return frozenset()
        return await self.deps.turn_records.read(turn_rows.handled_among, room_id, tuple(event_ids))

    async def deleted_sources(self, turn_record: TurnRecord) -> frozenset[str]:
        """Return which sources of a stored turn are deleted, directly or through the message they answer for.

        A redacted alias that maps to no source, such as the question a
        selection answers, deletes them all.
        """
        if turn_record.turn_key is None:
            return frozenset()
        return await self.deps.turn_records.read(_deleted_sources, turn_record.turn_key)

    async def prepared_voice_for_source(self, source_event_id: str) -> PreparedVoiceSource | None:
        """Return an exact physical source's voice checkpoint."""
        return await self.deps.turn_records.read(turn_rows.prepared_voice, source_event_id)

    async def visible_echo_for_source(self, source_event_id: str) -> str | None:
        """Return the tracked visible echo for one source event."""
        echo = await self.deps.turn_records.read(turn_rows.visible_echo, source_event_id)
        return None if echo is None else echo.event_id

    async def finalized_visible_echo(self, source_event_id: str) -> _FinalizedVisibleEcho | None:
        """Return named terminal state for one tracked visible echo."""
        echo = await self.deps.turn_records.read(turn_rows.visible_echo, source_event_id)
        if echo is None or echo.is_fallback is None:
            return None
        return _FinalizedVisibleEcho(event_id=echo.event_id, is_fallback=echo.is_fallback)

    async def finalized_visible_echoes(self, source_event_ids: Sequence[str]) -> tuple[str, ...]:
        """Return every visible echo of these sources whose replacement succeeded, in source order."""
        echoes = [await self.finalized_visible_echo(source_event_id) for source_event_id in source_event_ids]
        return tuple(echo.event_id for echo in echoes if echo is not None)

    async def turn_record_for_response_event_id(self, response_event_id: str) -> TurnRecord | None:
        """Return the durable turn that owns one visible response event."""
        return await self.deps.turn_records.read(turn_rows.turn_for_response_event, response_event_id)

    async def redacted_event_ids(self, room_id: str, event_ids: tuple[str, ...]) -> frozenset[str]:
        """Return which of a room's events are redacted, for readers of stored conversations outside a reply."""
        return await self.deps.turn_records.read(turn_rows.redacted_events, room_id, event_ids)

    async def redacted_history_events(
        self,
        target: MessageTarget,
        event_ids: tuple[str, ...],
    ) -> dict[str, str | None]:
        """Return which events one conversation's stored history derives from are redacted.

        Each redacted event maps to the source a legacy summary consumed it
        through, or None.
        """
        return await self.deps.turn_records.read(_redacted_history_events, target.room_id, event_ids)

    # Writes.

    async def record_prepared_voice(
        self,
        room_id: str,
        source_event_id: str,
        prepared: PreparedVoiceSource,
    ) -> PreparedVoiceSource | None:
        """Durably preserve the first normalization unless terminal truth forbids it."""
        return await self.deps.turn_records.write(
            turn_rows.record_prepared_voice,
            room_id=room_id,
            source_event_id=source_event_id,
            prepared=prepared,
        )

    async def register_edit_revision(
        self,
        turn_record: TurnRecord,
        edited_event_id: str,
        revision: SourceEventRevision,
    ) -> TurnRecord | None:
        """Retain a physical edit on the turn an edit flow loaded, before its text enters a prompt.

        Returns the turn as stored after it, or None when retention evicted it.
        """
        turn_key = turn_record.turn_key
        assert turn_key is not None, "an edit registers on the stored turn it loaded"
        timestamp_ms, revision_id = revision
        return await self.deps.turn_records.write(
            _register_revisions,
            turn_key,
            ((revision_id, edited_event_id, timestamp_ms),),
        )

    async def record_turn(self, turn_record: TurnRecord, *, room_id: str | None = None) -> turn_rows.ReshapeResult:
        """Record one turn answered without a visible response of its own."""
        return await self.deps.turn_records.write(
            turn_rows.record_turn,
            turn_record,
            room_id=_room_of(turn_record, room_id),
        )

    async def record_responded_turn(
        self,
        turn_record: TurnRecord,
        *,
        room_id: str | None = None,
    ) -> turn_rows.ReshapeResult:
        """Record one turn answered by its visible response, attributed only to the revisions it selected."""
        return await self.deps.turn_records.write(
            turn_rows.record_responded_turn,
            turn_record,
            room_id=_room_of(turn_record, room_id),
        )

    async def record_pending_turn(self, turn_record: TurnRecord) -> PendingTurn | None:
        """Persist exact response context before generation reaches session storage; return the turn as stored.

        None means the caller stops without answering: the turn is already
        answered or handed to its final delivery, or another answered turn owns
        a source. In that last case nothing was written, and every source the
        caller's claim holds goes back to the journal on its own once that claim
        releases, so a conflicting source settles as handled and an unrelated
        one in the same batch is answered.
        """
        room_id = _room_of(turn_record, None)
        result = await self.deps.turn_records.write(
            _record_pending_turn,
            canonicalize_turn_record(turn_record, completed=False),
            room_id=room_id,
        )
        if isinstance(result, turn_rows.Refused):
            self.retry_when_claim_releases(room_id, turn_record.source_event_ids)
        return result if isinstance(result, PendingTurn) else None

    async def record_placeholder(self, turn_record: TurnRecord, response_event_id: str) -> None:
        """Record the visible message an unfinished turn's answer streams into or was recovered as."""
        turn_key = turn_record.turn_key
        assert turn_key is not None, "a placeholder belongs to a stored turn"
        await self.deps.turn_records.write(
            turn_rows.record_placeholder,
            turn_key=turn_key,
            response_event_id=response_event_id,
        )

    async def record_command_checkpoint(
        self,
        turn_record: TurnRecord,
        *,
        result_text: str | None = None,
        result_extra_content: object = None,
    ) -> TurnRecord | None:
        """Checkpoint that a command began, or its first result; return the turn as stored, or None if it is gone."""
        turn_key = turn_record.turn_key
        assert turn_key is not None, "a command checkpoints the stored turn it recorded"
        return await self.deps.turn_records.write(
            _command_checkpoint,
            turn_key,
            result_text,
            result_extra_content,
        )

    async def record_edit(
        self,
        turn_record: TurnRecord,
        *,
        edited_event_id: str,
        prompt: str,
        revision: SourceEventRevision,
    ) -> bool:
        """Commit an edit's text and revision to the turn it loaded as its regeneration claims the reply.

        Whether the turn was answered stays as it was: the regeneration's answer records that.
        """
        turn_key = turn_record.turn_key
        assert turn_key is not None, "an edit commits to the stored turn it loaded"
        return await self.deps.turn_records.write(
            turn_rows.record_edit,
            turn_key=turn_key,
            edited_event_id=edited_event_id,
            prompt=prompt,
            revision=revision,
        )

    async def record_visible_echo(self, room_id: str, source_event_id: str, echo_event_id: str) -> None:
        """Track a visible echo for one source without changing its turn's answer."""
        await self.deps.turn_records.write(
            turn_rows.record_visible_echo,
            room_id=room_id,
            source_event_id=source_event_id,
            echo_event_id=echo_event_id,
        )

    async def record_finalized_visible_echo(
        self,
        source_event_id: str,
        echo_event_id: str,
        *,
        is_fallback: bool,
    ) -> None:
        """Mark a tracked visible echo as successfully replaced; the turn's answer stays as it was."""
        await self.deps.turn_records.write(
            turn_rows.record_finalized_visible_echo,
            source_event_id=source_event_id,
            echo_event_id=echo_event_id,
            is_fallback=is_fallback,
        )

    async def claim(self, turn_record: TurnRecord) -> TurnClaim | ClaimRefused | ClaimContended:
        """Claim a turn's sources and aliases for one live owner.

        The claim is published before anything is awaited, so two ingress paths
        racing for one turn cannot both win; whatever fails after that releases
        it before propagating.
        """
        source_event_ids = frozenset(turn_record.source_event_ids)
        discovery_event_ids = frozenset(turn_record.discovery_event_ids)
        if not source_event_ids:
            return ClaimRefused()
        blocking = self._claims.blocking_claim(source_event_ids, discovery_event_ids)
        if blocking is not None:
            return ClaimContended(blocking)
        claim = self._claims.register(source_event_ids, discovery_event_ids)
        return await self._unless_alias_answered(claim, turn_record)

    async def merge_claims(self, claims: Sequence[TurnClaim], turn_record: TurnRecord) -> TurnClaim | None:
        """Replace live claims with one covering them and a turn they became, or refuse.

        The replacement covers the turn's sources and aliases and every replaced
        claim's, so ownership never shrinks across the handoff, and the swap
        happens in one step: no other owner can take an event in between. The
        replaced handles are dead from then on. The supplied claims are consumed
        whatever the outcome: a refusal releases them, and ``None`` leaves the
        turn's sources unowned.
        """
        source_event_ids = frozenset(turn_record.source_event_ids).union(
            *(claim.source_event_ids for claim in claims),
        )
        discovery_event_ids = frozenset(turn_record.discovery_event_ids).union(
            *(claim.discovery_event_ids for claim in claims),
        )
        if not turn_record.source_event_ids or self._claims.blocking_claim(
            source_event_ids,
            discovery_event_ids,
            excluded=claims,
        ):
            for claim in claims:
                claim.release()
            return None
        merged = self._claims.register(source_event_ids, discovery_event_ids)
        self._claims.replace(claims, merged)
        result = await self._unless_alias_answered(merged, turn_record)
        return result if isinstance(result, TurnClaim) else None

    async def _unless_alias_answered(self, claim: TurnClaim, turn_record: TurnRecord) -> TurnClaim | ClaimRefused:
        """Keep a just-published claim unless a completed turn of another identity owns one of its aliases."""
        try:
            for event_id in claim.discovery_event_ids:
                owner = await self.get_turn_record(event_id)
                if owner is not None and owner.completed and owner.source_event_ids != turn_record.source_event_ids:
                    claim.release()
                    return ClaimRefused()
        except BaseException:
            claim.release()
            raise
        return claim

    def retry_when_released(self, blocking: TurnClaim, room_id: str, source_event_id: str) -> None:
        """Hand a source whose claim lost to ``blocking`` back to the journal once that claim releases.

        Until then the source counts as owned, so the journal leaves it deferred
        instead of dispatching it again. A replacement from ``merge_claims``
        inherits the source.
        """
        self._claims.retry_when_released(
            blocking,
            _DeferredSource(room_id, source_event_id, self.deps.retry_dispatch_sources),
        )

    def retry_when_claim_releases(self, room_id: str, source_event_ids: tuple[str, ...]) -> None:
        """Hand each source back to the journal on its own once the live claim holding it releases.

        Until then the source counts as owned, so the journal does not dispatch
        it meanwhile. A source no claim holds stays with its running callback,
        whose own failure path retains it.
        """
        for source_event_id in source_event_ids:
            holder = self._claims.blocking_claim(frozenset({source_event_id}), frozenset())
            if holder is not None:
                self._claims.retry_when_released(
                    holder,
                    _DeferredSource(room_id, source_event_id, self.deps.retry_dispatch_sources),
                )

    def has_live_turn_claim(self, event_id: str) -> bool:
        """Return whether some unsettled turn still holds this source or alias, or a lost claim waits on one.

        A claim is taken before its turn is handed off and dropped however the
        turn ends, so its presence is the one in-process fact that distinguishes
        "a turn owns this" from "a turn owned this and is gone".
        """
        return self._claims.owns(event_id)

    async def response_recovery_ready(self, turn_record: TurnRecord) -> bool:
        """Prove that a terminal response is complete or still durably owned."""
        if any(self.has_live_turn_claim(event_id) for event_id in turn_record.indexed_event_ids):
            gap = _RecoveryGap("live_turn_claim", pending_source_count=None)
        else:
            gap = _recovery_gap(turn_record, await self.deps.response_recovery_state(turn_record))
        if gap is None:
            return True
        self._log_recovery_gap(turn_record, gap)
        return False

    def _log_recovery_gap(self, turn_record: TurnRecord, gap: _RecoveryGap) -> None:
        """Log each non-sensitive recovery boundary once per store lifetime."""
        identity = (gap.reason, turn_record.source_event_ids, turn_record.anchor_event_id)
        if identity in self._recovery_diagnostic_classes:
            return
        self._recovery_diagnostic_classes.add(identity)
        logger.info(
            "response_recovery_proof_not_ready",
            agent=self.deps.agent_name,
            reason=gap.reason,
            source_count=len(turn_record.source_event_ids),
            source_event_ids=turn_record.source_event_ids,
            anchor_event_id=turn_record.anchor_event_id,
            pending_source_count=gap.pending_source_count,
            **(asdict(gap.turn_mismatches) if gap.turn_mismatches is not None else {}),
        )

    async def prepare_edit_snapshot(
        self,
        *,
        record: TurnRecord,
        live_source_event_ids: tuple[str, ...],
        driving_revision_id: str,
        consumed_revision_ids: tuple[str, ...] = (),
        thread_history: Sequence[ResolvedVisibleMessage] = (),
    ) -> bool:
        """Return whether an edit's snapshot must not run, read under the response lock.

        ``record`` is the snapshot, built from the turn the edit loaded, and
        ``live_source_event_ids`` the sources it was built from. A deletion or
        redaction of what it consumed, or a revision registered since it was
        built that is newer than its own edit, stops it: that newer edit
        regenerates on its own. A sibling's older edit that it does not carry
        does not, or two messages of one turn edited at once would each stop
        the other.
        """
        assert record.conversation_target is not None
        assert record.turn_key is not None
        return await self.deps.turn_records.write(
            _edit_snapshot_is_stale,
            record,
            room_id=record.conversation_target.room_id,
            live_source_event_ids=live_source_event_ids,
            driving_revision_id=driving_revision_id,
            consumed_revision_ids=consumed_revision_ids,
            context_revisions=_context_revisions(thread_history),
        )

    async def prepare_pending_response_source(
        self,
        *,
        target: MessageTarget,
        source_event_ids: tuple[str, ...],
        terminal_source_event_ids: tuple[str, ...],
        thread_history: Sequence[ResolvedVisibleMessage] = (),
    ) -> SourcePreparation:
        """Return which of a pending response's sources became terminal while it waited for its lock.

        ``terminal_source_event_ids`` are the response's physical sources;
        ``source_event_ids`` also names the discovery aliases a deletion can
        reach them through. The conversation's edits the history consumes are
        recorded on the turn first. A redacted message or revision still in the
        history means the snapshot predates the redaction, and the request
        retries its preparation.
        """
        context_revisions = _context_revisions(thread_history)
        # Only an edit to register needs the writer; the check reads, so it never queues behind other writes.
        if context_revisions and source_event_ids:
            await self.deps.turn_records.write(_register_context_revisions, source_event_ids[0], context_revisions)
        preparation, context_redacted = await self.deps.turn_records.read(
            _pending_source_preparation,
            room_id=target.room_id,
            source_event_ids=source_event_ids,
            terminal_source_event_ids=terminal_source_event_ids,
            history_event_ids=tuple(message.latest_event_id for message in thread_history),
        )
        if not preparation.invalid and context_redacted:
            msg = "Conversation revision changed before locked response preparation"
            raise RevisionSnapshotChangedError(msg)
        return preparation

    def response_history_scope(
        self,
        response_action: ResponseAction,
        *,
        requester_user_id: str | None = None,
    ) -> HistoryScope:
        """Return the persisted history scope used by one response action."""
        if response_action.kind == "individual":
            return self.deps.state_writer.history_scope()
        if response_action.kind == "team":
            assert response_action.form_team is not None
            return self.deps.state_writer.team_history_scope(
                response_action.form_team.eligible_member_names,
                requester_user_id=requester_user_id,
            )
        msg = f"Response history scope is not defined for {response_action.kind!r} actions"
        raise ValueError(msg)

    def attach_response_context(
        self,
        turn_record: TurnRecord,
        *,
        history_scope: HistoryScope | None,
        conversation_target: MessageTarget,
    ) -> TurnRecord:
        """Attach the persisted regeneration context for one response."""
        return canonicalize_turn_record(
            turn_record,
            response_owner=self.deps.agent_name,
            history_scope=history_scope,
            conversation_target=conversation_target,
        )

    def build_run_metadata(
        self,
        turn_record: TurnRecord,
        *,
        additional_discovery_event_ids: tuple[str, ...] = (),
    ) -> dict[str, Any] | None:
        """Project one record into versioned recoverable run metadata.

        ``additional_discovery_event_ids`` lets one anchored run stay discoverable by
        extra triggering events, such as a numeric interactive reply whose response
        still anchors to the original question event.
        """
        projected_record = turn_record
        if additional_discovery_event_ids:
            projected_record = canonicalize_turn_record(
                turn_record,
                discovery_event_ids=(*turn_record.discovery_event_ids, *additional_discovery_event_ids),
            )
        return _run_metadata(projected_record) or None

    async def remove_stale_runs_for_edit(
        self,
        *,
        turn_record: TurnRecord,
        requester_user_id: str,
    ) -> None:
        """Remove an edited turn's persisted runs from its recorded target and history scope before it regenerates.

        History derived from redacted events goes first, so a loaded run holding a
        redacted event still retires a legacy summary before the edit removes that run.
        """
        target = turn_record.conversation_target
        scope = turn_record.history_scope
        if target is None or scope is None:
            return
        storage = self.deps.state_writer.create_storage(
            self.deps.tool_runtime.build_execution_identity(target=target, user_id=requester_user_id),
            scope=scope,
        )
        session = await run_blocking_until_complete(storage.load_session, target.session_id, scope.kind)
        if session is not None:
            await remove_history_of_redacted_events(
                storage,
                session,
                scope,
                partial(self.redacted_history_events, target),
            )
        removed_any = await run_blocking_until_complete(
            _remove_runs_for_sources,
            storage,
            target.session_id,
            scope,
            turn_record.indexed_event_ids,
        )
        if removed_any:
            self.deps.state_writer.deps.logger.info(
                "Removed stale persisted history for handled turn",
                reason="edited",
                source_event_ids=list(turn_record.source_event_ids),
                session_id=target.session_id,
                history_scope=scope.key,
            )


def _recovery_gap(turn_record: TurnRecord, state: ResponseRecoveryState) -> _RecoveryGap | None:
    """Return why one recovery snapshot proves neither a complete response nor a durable owner, if it does not."""
    pending_sources = state.pending_sources
    if all(pending or redacted for pending, redacted in zip(pending_sources, state.redacted_sources, strict=True)) or (
        turn_record.anchor_event_id is not None and state.sources_settled_by_departure
    ):
        return None
    if any(pending_sources):
        return _RecoveryGap("mixed_source_pending", pending_source_count=sum(pending_sources))
    reply = state.reply
    if reply is not None:
        # The reply's records own an AI turn's outcome, and recovery sends any row it still owes.
        return None if reply.terminal else _RecoveryGap("reply_unfinished")
    return _unreplied_turn_recovery_gap(turn_record, state)


def _unreplied_turn_recovery_gap(turn_record: TurnRecord, state: ResponseRecoveryState) -> _RecoveryGap | None:
    """Return why a turn no reply answers is neither complete nor still owned by its outbox row, if it is not."""
    if turn_record.anchor_event_id is None:
        return _RecoveryGap("missing_turn_anchor")
    final_delivery = state.final_delivery
    if final_delivery is None:
        return _RecoveryGap("missing_final_delivery")
    if final_delivery.acknowledged_event_id is None:
        return None
    completed_response_event_ids = {
        event_id
        for event_id in (final_delivery.acknowledged_event_id, final_delivery.edits_event_id)
        if event_id is not None
    }
    loaded = tuple(record for record in state.turn_records if record is not None)
    mismatches = _TurnMismatchCounts(
        missing_completed_turn_count=len(state.turn_records) - len(loaded),
        incomplete_completed_turn_count=sum(not record.completed for record in loaded),
        source_identity_mismatch_count=sum(
            record.source_event_ids != turn_record.source_event_ids for record in loaded
        ),
        anchor_mismatch_count=sum(record.anchor_event_id != turn_record.anchor_event_id for record in loaded),
        response_event_mismatch_count=sum(
            record.response_event_id not in completed_response_event_ids for record in loaded
        ),
    )
    if not any(asdict(mismatches).values()):
        return None
    return _RecoveryGap("terminal_turn_mismatch", turn_mismatches=mismatches)


def _remove_runs_for_sources(
    storage: ConversationStore,
    session_id: str,
    scope: HistoryScope,
    source_event_ids: tuple[str, ...],
) -> bool:
    """Remove what answered each source and everything after it; return whether anything went."""
    removed_any = False
    for source_event_id in source_event_ids:
        removed_any = remove_runs_answering(storage, session_id, scope, source_event_id) or removed_any
    return removed_any


def _room_of(turn_record: TurnRecord, room_id: str | None) -> str:
    """Return the room a turn write names: the one given, else its conversation's."""
    if room_id is not None:
        return room_id
    if turn_record.conversation_target is None:
        msg = "A turn write requires the room its sources were sent in"
        raise ValueError(msg)
    return turn_record.conversation_target.room_id


def _run_metadata(record: TurnRecord) -> dict[str, object]:
    """Project the record's sources, prompts, and revisions into the run metadata history and request logs read."""
    if not record.source_event_ids:
        return {}
    metadata: dict[str, object] = {
        constants.MATRIX_SOURCE_EVENT_IDS_METADATA_KEY: list(record.source_event_ids),
    }
    if record.discovery_event_ids:
        metadata[constants.MATRIX_TURN_DISCOVERY_EVENT_IDS_METADATA_KEY] = list(record.discovery_event_ids)
    if record.source_event_prompts is not None:
        metadata[constants.MATRIX_SOURCE_EVENT_PROMPTS_METADATA_KEY] = dict(record.source_event_prompts)
    if record.source_event_revisions is not None:
        metadata[constants.MATRIX_SOURCE_EVENT_REVISIONS_METADATA_KEY] = {
            event_id: list(revision) for event_id, revision in record.source_event_revisions.items()
        }
    if record.requester_id is not None:
        metadata["requester_id"] = record.requester_id
    return metadata


def _context_revisions(thread_history: Sequence[ResolvedVisibleMessage]) -> tuple[turn_rows.RevisionRegistration, ...]:
    """Return the edits of other messages a response consumes as conversation context."""
    return tuple(
        (message.latest_event_id, message.event_id, int(message.edited_timestamp or message.timestamp or 0))
        for message in thread_history
        if message.latest_event_id != message.event_id
    )


def _command_checkpoint(
    transaction: Transaction,
    agent_name: str,
    turn_key: str,
    result_text: str | None,
    result_extra_content: object,
) -> TurnRecord | None:
    if result_text is None:
        turn_rows.record_command_started(transaction, agent_name, turn_key=turn_key)
    else:
        turn_rows.record_command_result(
            transaction,
            agent_name,
            turn_key=turn_key,
            text=result_text,
            extra_content=result_extra_content,
        )
    return turn_rows.load_turn(transaction, agent_name, turn_key)


def _register_revisions(
    transaction: Transaction,
    agent_name: str,
    turn_key: str,
    revisions: Sequence[turn_rows.RevisionRegistration],
) -> TurnRecord | None:
    if not turn_rows.record_revisions(transaction, agent_name, turn_key=turn_key, revisions=revisions):
        return None
    return turn_rows.load_turn(transaction, agent_name, turn_key)


def _record_pending_turn(
    transaction: Transaction,
    agent_name: str,
    record: TurnRecord,
    *,
    room_id: str,
) -> PendingTurn | turn_rows.Answered | turn_rows.Refused:
    resumes_response = _has_pending_response_intent(transaction, agent_name, record.source_event_ids)
    result = turn_rows.record_pending_turn(transaction, agent_name, record, room_id=room_id)
    if not isinstance(result, turn_rows.Accepted):
        return result
    return PendingTurn(
        record=result.record,
        resumes_response=resumes_response,
        deleted_sources=_deleted_sources_of(transaction, agent_name, result.record),
    )


def _deleted_sources(transaction: Transaction, agent_name: str, turn_key: str) -> frozenset[str]:
    record = turn_rows.load_turn(transaction, agent_name, turn_key)
    return frozenset() if record is None else _deleted_sources_of(transaction, agent_name, record)


def _deleted_sources_of(transaction: Transaction, agent_name: str, record: TurnRecord) -> frozenset[str]:
    assert record.turn_key is not None
    if record.conversation_target is not None:
        mapped = {metadata.discovery_event_id for metadata in (record.source_event_metadata or {}).values()}
        unmapped = [alias for alias in record.discovery_event_ids if alias not in mapped]
        if turn_rows.redacted_events(transaction, agent_name, record.conversation_target.room_id, unmapped):
            return frozenset(record.source_event_ids)
    return turn_rows.deleted_sources(transaction, agent_name, record.turn_key)


def _has_pending_response_intent(transaction: Transaction, agent_name: str, source_event_ids: tuple[str, ...]) -> bool:
    return any(
        (record := turn_rows.get_turn_record(transaction, agent_name, source_event_id)) is not None
        and not record.completed
        and record.response_owner is not None
        and record.conversation_target is not None
        for source_event_id in source_event_ids
    )


def _redacted_history_events(
    transaction: Transaction,
    agent_name: str,
    room_id: str,
    event_ids: tuple[str, ...],
) -> dict[str, str | None]:
    redacted: dict[str, str | None] = dict.fromkeys(
        sorted(turn_rows.redacted_events(transaction, agent_name, room_id, event_ids)),
    )
    # LEGACY_COMPAT: Revisions consumed only through a source-level summary.
    # Legacy format: A revision a pre-v2026.9.43 compacted summary owns through its source rather than by revision id,
    # recorded in legacy_summary_revisions when its turn record was converted.
    # Last legacy release: v2026.9.42; replacement: v2026.9.43 records per-revision provenance.
    # Handling: When the source is in the history and the revision is redacted, the revision is reported with that
    # source, even if later history also names the revision, so removal clears the legacy summary as well.
    # Coverage: tests/test_turn_store.py::test_legacy_compacted_revision_uses_retained_owner_on_cold_reopen.
    summaries = legacy_summary_sources(transaction, agent_name, room_id, event_ids)
    candidates = set(event_ids)
    for revision_id in turn_rows.redacted_events(transaction, agent_name, room_id, summaries):
        if summaries[revision_id] in candidates:
            redacted[revision_id] = summaries[revision_id]
    return redacted


def _register_context_revisions(
    transaction: Transaction,
    agent_name: str,
    source_event_id: str,
    context_revisions: tuple[turn_rows.RevisionRegistration, ...],
) -> None:
    """Record the history's edits on the turn one of its sources answers to."""
    owner = turn_rows.get_turn_record(transaction, agent_name, source_event_id)
    if owner is not None and owner.turn_key is not None:
        turn_rows.record_revisions(transaction, agent_name, turn_key=owner.turn_key, revisions=context_revisions)


def _pending_source_preparation(
    transaction: Transaction,
    agent_name: str,
    *,
    room_id: str,
    source_event_ids: tuple[str, ...],
    terminal_source_event_ids: tuple[str, ...],
    history_event_ids: tuple[str, ...],
) -> tuple[SourcePreparation, bool]:
    """Split a turn's sources into invalid and surviving, and report whether its history holds a redaction."""
    sources = terminal_source_event_ids
    invalid = set(turn_rows.deleted_events(transaction, agent_name, room_id, sources))
    invalid |= turn_rows.handled_among(transaction, agent_name, room_id, sources)
    # A redacted alias that maps to none of these sources, such as the question a selection answers, deletes them all.
    for alias in turn_rows.redacted_events(transaction, agent_name, room_id, set(source_event_ids) - set(sources)):
        if set(sources).isdisjoint(turn_rows.sources_deleted_by(transaction, agent_name, room_id, alias)):
            invalid.update(sources)
    context_redacted = bool(turn_rows.redacted_events(transaction, agent_name, room_id, history_event_ids))
    return (
        SourcePreparation(
            invalid=tuple(source for source in sources if source in invalid),
            surviving=tuple(source for source in sources if source not in invalid),
        ),
        context_redacted,
    )


def _edit_snapshot_is_stale(  # noqa: PLR0911
    transaction: Transaction,
    agent_name: str,
    record: TurnRecord,
    *,
    room_id: str,
    live_source_event_ids: tuple[str, ...],
    driving_revision_id: str,
    consumed_revision_ids: tuple[str, ...],
    context_revisions: tuple[turn_rows.RevisionRegistration, ...],
) -> bool:
    turn_key = record.turn_key
    assert turn_key is not None
    if not turn_rows.record_revisions(transaction, agent_name, turn_key=turn_key, revisions=context_revisions):
        return True
    current = turn_rows.load_turn(transaction, agent_name, turn_key)
    if current is None:
        return True
    if not turn_rows.deleted_sources(transaction, agent_name, turn_key).isdisjoint(live_source_event_ids):
        return True
    selections = {
        source: revision
        for key, revision in (record.source_event_revisions or {}).items()
        if (source := record.prompt_source_event_id(key)) in live_source_event_ids
    }
    watermarks = {source: current.revision_watermark(source) for source in live_source_event_ids}
    redacted = turn_rows.redacted_events(
        transaction,
        agent_name,
        room_id,
        (
            driving_revision_id,
            *consumed_revision_ids,
            *(revision_id for _ts, revision_id in selections.values()),
            *(watermark[1] for watermark in watermarks.values() if watermark is not None),
        ),
    )
    if driving_revision_id in redacted or not redacted.isdisjoint(consumed_revision_ids):
        return True
    if any(revision_id in redacted for _ts, revision_id in selections.values()):
        return True
    # The edit's own revision: only what registered above it can stop it.
    driving = next(
        revision for revision in (record.source_event_revisions or {}).values() if revision[1] == driving_revision_id
    )
    for source, latest in watermarks.items():
        if latest is None or latest < driving:
            continue
        if (
            latest[1] in (current.revision_replay or {})
            and latest[1] not in redacted
            and selections.get(source) != latest
        ):
            return True
    return any(
        latest > snapshot
        for source, latest in watermarks.items()
        if latest is not None and latest > driving and (snapshot := record.revision_watermark(source)) is not None
    )
