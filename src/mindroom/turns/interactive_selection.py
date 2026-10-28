"""Admit an answer to an interactive question behind earlier ingress, then run its response."""

from __future__ import annotations

import asyncio
from dataclasses import dataclass, replace
from typing import TYPE_CHECKING

from mindroom import interactive
from mindroom.background_tasks import run_coroutine_until_complete
from mindroom.coalescing.batch import PendingEvent, requester_coalescing_key
from mindroom.constants import STREAM_STATUS_ERROR, STREAM_STATUS_KEY
from mindroom.delivery_gateway import SendTextRequest
from mindroom.dispatch_handoff import PreparedIngress
from mindroom.dispatch_source import INTERACTIVE_SELECTION_SOURCE_KIND, MESSAGE_SOURCE_KIND
from mindroom.error_handling import get_user_friendly_error_message
from mindroom.hooks import MessageEnvelope
from mindroom.inbound_turn_normalizer import DispatchPayloadWithAttachmentsRequest
from mindroom.matrix.member_display_names import room_member_display_names
from mindroom.response_lifecycle import response_lifecycle_reservation_context
from mindroom.response_runner import ResponseRequest
from mindroom.response_sources import ResponseSources
from mindroom.turn_policy import ResponseAction
from mindroom.turn_record import TurnRecord, canonicalize_turn_record

if TYPE_CHECKING:
    from collections.abc import Awaitable, Callable

    import nio
    import structlog

    from mindroom.coalescing.batch import PreparedTurn
    from mindroom.coalescing.ingress_ticket import IngressTicket
    from mindroom.constants import RuntimePaths
    from mindroom.conversation_resolver import ConversationResolver
    from mindroom.delivery_gateway import DeliveryGateway
    from mindroom.inbound_turn_normalizer import InboundTurnNormalizer
    from mindroom.ingress_validation import IngressValidator
    from mindroom.message_target import MessageTarget
    from mindroom.response_runner import ResponseRunner
    from mindroom.turn_store import TurnStore
    from mindroom.turns.facts import IngressFacts
    from mindroom.visible_response_reconciliation import VisibleResponseReconciler


# A selection queues as an ``interactive_selection`` barrier behind earlier ingress, but its
# answer replies to a message, so mid-turn judging treats it as one.
_SELECTION_RESPONSE_SOURCE_KIND = MESSAGE_SOURCE_KIND


@dataclass(frozen=True)
class InteractiveSelectionDispatch:
    """One admitted selection carried through receipt-ordered coalescing, which dispatch then runs."""

    selection: interactive.InteractiveSelection
    response_target: MessageTarget
    source_event_id: str
    # The answering event's facts with the selection source kind.
    facts: IngressFacts


@dataclass(frozen=True)
class InteractiveSelectionsDeps:
    """Collaborators needed to admit and run interactive selections."""

    logger: structlog.stdlib.BoundLogger
    runtime_paths: RuntimePaths
    agent_name: str
    resolver: ConversationResolver
    normalizer: InboundTurnNormalizer
    ingress: IngressValidator
    response_runner: ResponseRunner
    delivery_gateway: DeliveryGateway
    turn_store: TurnStore
    visible_responses: VisibleResponseReconciler
    retry_dispatch_sources: Callable[[str, tuple[str, ...]], None]
    settle_dispatch_sources: Callable[[tuple[str, ...]], Awaitable[None]]
    dispatch_source_is_terminal: Callable[[str], Awaitable[bool]]


@dataclass
class InteractiveSelections:
    """Own one interactive selection from its answer's admission to its recorded response."""

    deps: InteractiveSelectionsDeps

    def _interactive_selection_response_envelope(
        self,
        *,
        target: MessageTarget,
        facts: IngressFacts,
        source_event_id: str,
        selected_value: str,
        attachment_ids: tuple[str, ...] = (),
    ) -> MessageEnvelope:
        """Build the canonical response envelope for one interactive selection."""
        return MessageEnvelope(
            source_event_id=source_event_id,
            target=target,
            body=f"The user selected: {selected_value}",
            attachment_ids=attachment_ids,
            mentioned_agents=(),
            agent_name=self.deps.agent_name,
            origin=replace(facts, source_kind=_SELECTION_RESPONSE_SOURCE_KIND).origin,
        )

    async def _start_interactive_selection(self, room: nio.MatrixRoom, dispatch: InteractiveSelectionDispatch) -> None:
        """Transfer one selection response from journal dispatch to runner ownership."""
        response_target = dispatch.response_target
        source_event_id = dispatch.source_event_id
        response_envelope = self._interactive_selection_response_envelope(
            target=response_target,
            facts=dispatch.facts,
            source_event_id=source_event_id,
            selected_value=dispatch.selection.selected_value,
        )
        try:
            reservation = await self.deps.response_runner.reserve_response_lifecycle(response_envelope)
        except BaseException:
            self.deps.retry_dispatch_sources(response_target.room_id, (source_event_id,))
            raise

        async def run_owned_response() -> None:
            response_claim_transferred = False
            try:
                with response_lifecycle_reservation_context(reservation):
                    await reservation.wait_until_acquired()
                    response = self._handle_interactive_selection(
                        room,
                        selection=dispatch.selection,
                        facts=dispatch.facts,
                        source_event_id=source_event_id,
                        response_target=response_target,
                    )
                    response_claim_transferred = True
                    source_handed_off = await response
                # Terminal delivery normally settles the discovery alias in its
                # outbox transaction. This idempotent fallback also handles an
                # already-terminal replay that has no new delivery to enqueue.
                if not source_handed_off:
                    await self.deps.settle_dispatch_sources((source_event_id,))
            except BaseException:
                if not response_claim_transferred:
                    self.deps.retry_dispatch_sources(response_target.room_id, (source_event_id,))
                raise
            finally:
                await reservation.release()

        owned_response = run_owned_response()
        try:
            self.deps.response_runner.track_inbox_response(
                owned_response,
                name=f"interactive_selection_response:{source_event_id}",
                room_id=response_target.room_id,
                # No turn record proves a selection reply recoverable, so orderly
                # shutdown waits out its budget for one still running.
                recovery_proof_ready=lambda: False,
                on_failure=lambda: self.deps.retry_dispatch_sources(response_target.room_id, (source_event_id,)),
                source_event_ids=(source_event_id,),
            )
        except BaseException:
            owned_response.close()
            await reservation.release()
            self.deps.retry_dispatch_sources(response_target.room_id, (source_event_id,))
            raise
        # Ownership registration is synchronous, and the task cannot execute
        # until this callback yields after reporting its deferred handoff.

    def _interactive_selection_target(
        self,
        room_id: str,
        selection: interactive.InteractiveSelection,
    ) -> MessageTarget:
        """Return the canonical response target for one interactive selection."""
        return self.deps.resolver.build_message_target(
            room_id=room_id,
            thread_id=selection.thread_id,
            reply_to_event_id=selection.question_event_id,
        )

    async def enqueue_interactive_selection(
        self,
        ticket: IngressTicket,
        room: nio.MatrixRoom,
        *,
        selection: interactive.InteractiveSelection,
        facts: IngressFacts,
        source_event_id: str,
    ) -> None:
        """Queue a selection handoff as a FIFO barrier behind earlier ingress.

        ``facts`` classify the answering event: a digit message or a reaction.
        """
        response_target = self._interactive_selection_target(room.room_id, selection)
        facts = replace(facts, source_kind=INTERACTIVE_SELECTION_SOURCE_KIND)
        pending_event = PendingEvent(
            event=PreparedIngress(
                sender=facts.physical_sender,
                event_id=source_event_id,
                body="",
                source={},
            ),
            room=room,
            facts=facts,
            selection=InteractiveSelectionDispatch(
                selection=selection,
                response_target=response_target,
                source_event_id=source_event_id,
                facts=facts,
            ),
        )
        ticket.admit(
            requester_coalescing_key(
                room.room_id,
                response_target.lifecycle_key.thread_id,
                facts.run_requester_id,
            ),
            pending_event,
        )

    async def dispatch_selection_turn(self, turn: PreparedTurn) -> bool:
        """Start one receipt-ordered selection barrier and transfer its source to the runner; False for any other turn."""
        dispatch = turn.selection
        if dispatch is None:
            return False
        if len(turn.handled_turn.source_event_ids) != 1:
            msg = "Interactive selection dispatch must be a single FIFO barrier"
            raise ValueError(msg)
        await self._start_interactive_selection(turn.room, dispatch)
        return True

    async def _handle_interactive_selection(
        self,
        room: nio.MatrixRoom,
        *,
        selection: interactive.InteractiveSelection,
        facts: IngressFacts,
        source_event_id: str,
        response_target: MessageTarget,
    ) -> bool:
        """Own claim settlement around one validated interactive selection."""
        try:
            return await self._execute_interactive_selection(
                room,
                selection=selection,
                facts=facts,
                source_event_id=source_event_id,
                response_target=response_target,
            )
        except BaseException as error:

            async def source_is_terminal_after_cancellation() -> bool:
                return await self.deps.dispatch_source_is_terminal(source_event_id)

            source_is_terminal = await run_coroutine_until_complete(
                source_is_terminal_after_cancellation(),
            )
            if source_is_terminal:
                if isinstance(error, asyncio.CancelledError):
                    raise
                return False
            raise

    async def _execute_interactive_selection(
        self,
        room: nio.MatrixRoom,
        *,
        selection: interactive.InteractiveSelection,
        facts: IngressFacts,
        source_event_id: str,
        response_target: MessageTarget,
    ) -> bool:
        """Authorize and execute one selection after its caller transfers claim ownership."""
        async with self.deps.ingress.admitted_reply_decision(
            facts.run_requester_id,
            room.room_id,
            unanswered=(source_event_id,),
        ) as allowed:
            if not allowed:
                return False
            return await self._execute_admitted_interactive_selection(
                room,
                selection=selection,
                facts=facts,
                source_event_id=source_event_id,
                response_target=response_target,
            )

    async def _execute_admitted_interactive_selection(
        self,
        room: nio.MatrixRoom,
        *,
        selection: interactive.InteractiveSelection,
        facts: IngressFacts,
        source_event_id: str,
        response_target: MessageTarget,
    ) -> bool:
        """Execute one authorized selection while replacement admission remains reserved."""
        if await self._interactive_selection_is_durably_terminal(room.room_id, source_event_id):
            return False
        thread_history = (
            await self.deps.resolver.fetch_thread_history(
                room.room_id,
                selection.thread_id,
            )
            if selection.thread_id
            else []
        )
        selection_handled_turn = self.deps.turn_store.attach_response_context(
            TurnRecord.create(
                [source_event_id],
                discovery_event_ids=(
                    (selection.question_event_id,) if source_event_id != selection.question_event_id else ()
                ),
                requester_id=facts.run_requester_id,
            ),
            history_scope=self.deps.turn_store.response_history_scope(ResponseAction(kind="individual")),
            conversation_target=response_target,
        )
        pending_turn = await self.deps.turn_store.record_pending_turn(selection_handled_turn)
        if pending_turn is None or pending_turn.deleted_sources:
            await self._require_durable_interactive_selection(room.room_id, source_event_id)
            return False
        selection_handled_turn = pending_turn.record
        # This acknowledgement is the placeholder the selection's answer then
        # edits, which is what its `interactive_span_id` below says, so it
        # is the turn's initial delivery and not its answer. Staging it that way
        # also keeps it from settling the journal source: a placeholder
        # discharges nothing, and a crash before the model finished would
        # otherwise leave "Processing your response..." in the room with
        # nothing pending to replay.
        ack_event_id, interactive_span_id = await self.deps.visible_responses.deliver_selection_acknowledgement(
            selection_handled_turn,
            target=response_target,
            response_text=(
                f"You selected: {selection.selection_key} {selection.selected_label}\n\nProcessing your response..."
            ),
            delivery_turn_id=source_event_id,
        )
        if not ack_event_id:
            self.deps.logger.error(
                "Failed to send acknowledgment for interactive selection",
                source_event_id=source_event_id,
            )
            raise self._interactive_selection_retry_error(source_event_id)
        # A reaction or numeric answer identifies the selection but does not
        # carry the question's attachment context, so rebuild it from the
        # conversation that asked the question.
        try:
            selection_payload = await self.deps.normalizer.build_dispatch_payload_with_attachments(
                DispatchPayloadWithAttachmentsRequest(
                    room_id=room.room_id,
                    prompt=interactive.build_selection_prompt(selection),
                    current_attachment_ids=[],
                    thread_id=selection.thread_id,
                    media_thread_id=response_target.resolved_thread_id,
                    thread_history=thread_history,
                ),
            )
        except Exception as error:
            response_event_id = await self._finalize_dispatch_failure(
                target=response_target,
                error=error,
                handled_turn=selection_handled_turn,
                existing_event_id=ack_event_id,
            )
            if response_event_id is not None:
                await self._require_durable_interactive_selection(room.room_id, source_event_id)
                return False
            raise self._interactive_selection_retry_error(source_event_id) from error
        selection_attachment_ids = tuple(selection_payload.attachment_ids or ())
        selection_matrix_run_metadata = self.deps.turn_store.build_run_metadata(selection_handled_turn)
        response_envelope = self._interactive_selection_response_envelope(
            target=response_target,
            facts=facts,
            source_event_id=source_event_id,
            selected_value=selection.selected_value,
            attachment_ids=selection_attachment_ids,
        )

        source_handoff = asyncio.Event()
        await self.deps.response_runner.generate_response(
            ResponseRequest(
                prompt=selection_payload.prompt,
                model_prompt=selection_payload.model_prompt,
                thread_history=thread_history,
                sources=ResponseSources(
                    pending_event_ids=tuple(
                        dict.fromkeys((source_event_id, *selection_handled_turn.source_event_ids)),
                    ),
                    logical_source_event_ids=selection_handled_turn.source_event_ids,
                    discovery_event_ids=selection_handled_turn.discovery_event_ids,
                ),
                history_boundary_event_id=source_event_id,
                member_display_names=room_member_display_names(room),
                existing_event_id=ack_event_id,
                interactive_span_id=interactive_span_id,
                user_id=facts.run_requester_id,
                attachment_ids=selection_attachment_ids or None,
                response_envelope=response_envelope,
                matrix_run_metadata=selection_matrix_run_metadata,
                prepare_source_turn=lambda history: self.deps.turn_store.prepare_pending_response_source(
                    target=response_target,
                    source_event_ids=selection_handled_turn.indexed_event_ids,
                    terminal_source_event_ids=selection_handled_turn.source_event_ids,
                    thread_history=history,
                ),
                source_handoff=source_handoff,
            ),
        )
        if source_handoff.is_set():
            return True
        await self._require_durable_interactive_selection(room.room_id, source_event_id)
        return False

    async def _interactive_selection_is_durably_terminal(self, room_id: str, source_event_id: str) -> bool:
        """Return whether the exact selection source is durably terminal.

        Two facts on purpose: the turn's rows are the agent's, the journal row
        this login's, and either can say yes without the other.
        """
        return await self.deps.turn_store.is_handled(
            room_id,
            source_event_id,
        ) or await self.deps.dispatch_source_is_terminal(source_event_id)

    async def _require_durable_interactive_selection(self, room_id: str, source_event_id: str) -> None:
        """Fail retryably until one selection source reaches durable terminal truth."""
        if await self._interactive_selection_is_durably_terminal(room_id, source_event_id):
            return
        raise self._interactive_selection_retry_error(source_event_id)

    @staticmethod
    def _interactive_selection_retry_error(source_event_id: str) -> RuntimeError:
        """Return the shared retry signal for a selection without terminal truth."""
        return RuntimeError(f"Interactive selection {source_event_id} has no durable terminal outcome")

    async def _finalize_dispatch_failure(
        self,
        *,
        target: MessageTarget,
        error: Exception,
        handled_turn: TurnRecord,
        existing_event_id: str | None = None,
    ) -> str | None:
        """Convert dispatch setup failures into a visible terminal message.

        A reply that owns ``existing_event_id`` shows the failure through its
        records, which settle the journal sources, record the turn answered,
        and own the notice until it is delivered. Otherwise nothing durable owns
        the notice, so it is sent directly and the turn records it as its answer.
        """
        error_text = get_user_friendly_error_message(
            error,
            self.deps.agent_name,
            runtime_paths=self.deps.runtime_paths,
        )
        if existing_event_id is not None and await self.deps.delivery_gateway.fail_reply_dispatch(
            existing_event_id,
            error_text,
        ):
            return existing_event_id
        response_event_id = await self.deps.delivery_gateway.send_text(
            SendTextRequest(
                target=target,
                response_text=error_text,
                extra_content={STREAM_STATUS_KEY: STREAM_STATUS_ERROR},
            ),
        )
        if response_event_id is not None:
            await self.deps.turn_store.record_responded_turn(
                canonicalize_turn_record(handled_turn, response_event_id=response_event_id),
                room_id=target.room_id,
            )
        return response_event_id
