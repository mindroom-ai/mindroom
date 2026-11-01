"""Dispatch one turn from the coalescing gate's exit to its recorded outcome: prepare, decide, execute, record."""

from __future__ import annotations

import asyncio
import time
from dataclasses import dataclass, replace
from functools import partial
from typing import TYPE_CHECKING, Any

from mindroom.attachments.storage import parse_attachment_ids_from_event_source
from mindroom.authorization import is_requester_joined_to_room
from mindroom.bot_runtime_view import RunPhase
from mindroom.constants import (
    ATTACHMENT_IDS_KEY,
    ORIGINAL_SENDER_KEY,
    SCHEDULED_HISTORY_LIMIT_KEY,
    SCHEDULED_MODEL_KEY,
    VOICE_RAW_AUDIO_FALLBACK_KEY,
    VOICE_TRANSCRIPT_KEY,
    RuntimePaths,
)
from mindroom.dispatch_replay_guard import (
    has_newer_unresponded_in_thread,
    has_newer_unresponded_journal_thread_event,
)
from mindroom.dispatch_source import (
    ScheduledHistoryBudget,
    scheduled_history_limit_from_content,
)
from mindroom.hooks import MessageEnvelope, hook_ingress_policy
from mindroom.logging_config import bound_log_context
from mindroom.matrix.event_info import EventInfo
from mindroom.matrix.media import is_matrix_media_dispatch_event
from mindroom.matrix.member_display_names import room_member_display_names
from mindroom.matrix.thread_diagnostics import is_thread_history_degraded
from mindroom.matrix.thread_history_result import ThreadHistoryResult
from mindroom.reply_turn.request import ResponseRequest
from mindroom.response_admission import ResponseAdmissionRefusedError
from mindroom.response_payload_preparation import (
    DispatchPayloadInputs,
    ResponsePayloadPreparation,
)
from mindroom.response_sources import ResponseSources
from mindroom.router.relay import execute_router_relay
from mindroom.teams.formation import TeamIntent, select_ad_hoc_team_mode
from mindroom.thread_utils import mentions_another_participant
from mindroom.timestamp_formatting import normalize_timestamp_ms
from mindroom.timing import (
    DispatchPipelineTiming,
    emit_elapsed_timing,
    event_timing_scope,
    get_dispatch_pipeline_timing,
)
from mindroom.timing import timing_scope as timing_scope_context
from mindroom.turn_origin import TurnIntent
from mindroom.turn_policy import PreparedDispatch, ResponseAction
from mindroom.turn_record import TurnRecord, canonicalize_turn_record

if TYPE_CHECKING:
    from collections.abc import Callable, Mapping, Sequence

    import nio
    import structlog

    from mindroom.bot_runtime_view import BotRuntimeView
    from mindroom.coalescing.batch import PreparedTurn
    from mindroom.command_turn_executor import CommandTurnExecutor
    from mindroom.conversation_resolver import ConversationResolver, MessageContext
    from mindroom.delivery_gateway import DeliveryGateway
    from mindroom.dispatch_handoff import DispatchEvent, PreparedIngress
    from mindroom.event_journal import PendingTurnView
    from mindroom.hooks.ingress import IngressHookRunner
    from mindroom.inbound_turn_normalizer import InboundTurnNormalizer
    from mindroom.ingress_validation import IngressValidator
    from mindroom.matrix.client_visible_messages import ResolvedVisibleMessage
    from mindroom.matrix.identity import MatrixID
    from mindroom.response_runner import ResponseRunner
    from mindroom.response_sources import SourceFate
    from mindroom.teams.spec import TeamMode
    from mindroom.turn_policy import DispatchPlan, TurnPolicy
    from mindroom.turn_store import TurnClaim, TurnStore
    from mindroom.turns.interactive_selection import InteractiveSelections
    from mindroom.visible_response_reconciliation import VisibleResponseReconciler


def _room_level_context_event(event: PreparedIngress) -> PreparedIngress:
    """Return an event view without its Matrix relation, so a turn decided room-level is targeted like a plain message."""
    content = event.source.get("content")
    if not isinstance(content, dict) or "m.relates_to" not in content:
        return event
    stripped_content = dict(content)
    stripped_content.pop("m.relates_to", None)
    return replace(event, source={**event.source, "content": stripped_content})


# Ingress treats only a human's router relay as a handoff, so the router's relay of a
# task a bot account scheduled arrives as a notice and must keep its per-run settings too.
_SCHEDULED_RUN_INTENTS = frozenset({TurnIntent.SCHEDULED_FIRE, TurnIntent.ROUTER_HANDOFF, TurnIntent.ROUTER_NOTICE})
# Hook dispatches come only from managed senders, such as automations choosing a model for their prompt.
_PER_RUN_MODEL_INTENTS = _SCHEDULED_RUN_INTENTS | {TurnIntent.HOOK_DISPATCH}


def _scheduled_model_for_dispatch(event: DispatchEvent, origin_intent: TurnIntent) -> str | None:
    """Accept a per-run model only from trusted scheduled fires, hook dispatches, or the router's relays of them."""
    if origin_intent not in _PER_RUN_MODEL_INTENTS:
        return None
    content = event.source.get("content") if isinstance(event.source, dict) else None
    if not isinstance(content, dict):
        return None
    model = content.get(SCHEDULED_MODEL_KEY)
    return model if isinstance(model, str) and model else None


def _scheduled_history_budget_for_dispatch(
    event: DispatchEvent,
    origin_intent: TurnIntent,
) -> ScheduledHistoryBudget | None:
    """Return the trusted history budget and prompt source for one scheduled dispatch."""
    if origin_intent not in _SCHEDULED_RUN_INTENTS:
        return None
    content = event.source.get("content") if isinstance(event.source, dict) else None
    if not isinstance(content, dict):
        return None
    history_limit = scheduled_history_limit_from_content(content)
    if history_limit is None:
        return None
    source_event_id = (
        event.event_id
        if origin_intent is TurnIntent.SCHEDULED_FIRE
        else EventInfo.from_event(event.source).reply_to_event_id
    )
    if source_event_id is None:
        return None
    return ScheduledHistoryBudget(limit=history_limit, source_event_id=source_event_id)


@dataclass(frozen=True)
class _PreparedTextDispatch:
    turn: PreparedTurn
    handled_turn: TurnRecord
    dispatch: PreparedDispatch
    dispatch_started_at: float


def _attachment_parts(prepared: _PreparedTextDispatch) -> tuple[list[str], list[str], dict[str, Any]]:
    turn = prepared.turn
    payload_metadata = turn.payload
    message_attachment_ids = (
        list(payload_metadata.attachment_ids)
        if payload_metadata.attachment_ids is not None
        else parse_attachment_ids_from_event_source(turn.event.source)
    )
    trusted_attachment_ids = (
        list(payload_metadata.attachment_ids) if payload_metadata.attachment_ids is not None else []
    )
    extra_content: dict[str, Any] = {}
    if message_attachment_ids:
        extra_content[ATTACHMENT_IDS_KEY] = message_attachment_ids
    if payload_metadata.original_sender is not None:
        extra_content[ORIGINAL_SENDER_KEY] = payload_metadata.original_sender
    if payload_metadata.raw_audio_fallback:
        extra_content[VOICE_RAW_AUDIO_FALLBACK_KEY] = True
    if payload_metadata.voice_transcript:
        extra_content[VOICE_TRANSCRIPT_KEY] = True
    if turn.media_events and ORIGINAL_SENDER_KEY not in extra_content:
        extra_content[ORIGINAL_SENDER_KEY] = turn.facts.run_requester_id
    if prepared.dispatch.scheduled_history_budget is not None:
        extra_content[SCHEDULED_HISTORY_LIMIT_KEY] = prepared.dispatch.scheduled_history_budget.limit
    if prepared.dispatch.scheduled_model is not None:
        extra_content[SCHEDULED_MODEL_KEY] = prepared.dispatch.scheduled_model
    return message_attachment_ids, trusted_attachment_ids, extra_content


def _tracked_route_turn(
    prepared: _PreparedTextDispatch,
    *,
    route_event: DispatchEvent,
    single_direct_media_route: bool,
) -> TurnRecord | None:
    if single_direct_media_route:
        return None
    if not prepared.handled_turn.is_coalesced and (
        not prepared.handled_turn.source_event_ids
        or prepared.handled_turn.source_event_ids[0] == prepared.turn.event.event_id
    ):
        return None
    if list(prepared.handled_turn.source_event_ids) == [route_event.event_id]:
        return None
    return prepared.handled_turn


async def run_response_action(
    runner: ResponseRunner,
    request: ResponseRequest,
    action: ResponseAction,
    *,
    team_mode: TeamMode | None = None,
) -> SourceFate:
    """Run one prepared request through the runner entry its team or individual response action names.

    ``team_mode`` replaces the team's resolved mode, as the mode chosen for an ad hoc team does.
    """
    if action.kind != "team":
        return await runner.generate_response(request)
    assert action.form_team is not None
    mode = action.form_team.mode if team_mode is None else team_mode
    assert mode is not None
    return await runner.generate_team_response_helper(
        request,
        member_names=action.form_team.eligible_member_names,
        team_mode=mode.value,
    )


@dataclass(frozen=True)
class TurnDispatchDeps:
    """Collaborators needed to dispatch a turn after the coalescing gate."""

    runtime: BotRuntimeView
    logger: structlog.stdlib.BoundLogger
    runtime_paths: RuntimePaths
    agent_name: str
    matrix_id: MatrixID
    pending_turns: PendingTurnView
    resolver: ConversationResolver
    normalizer: InboundTurnNormalizer
    command_executor: CommandTurnExecutor
    turn_policy: TurnPolicy
    ingress_hook_runner: IngressHookRunner
    response_runner: ResponseRunner
    delivery_gateway: DeliveryGateway
    turn_store: TurnStore
    ingress: IngressValidator
    visible_responses: VisibleResponseReconciler
    retry_dispatch_sources: Callable[[str, tuple[str, ...]], None]
    selections: InteractiveSelections


@dataclass
class TurnDispatcher:
    """Own one turn from the coalescing gate's exit to its recorded outcome."""

    deps: TurnDispatchDeps

    def _client(self) -> nio.AsyncClient:
        client = self.deps.runtime.client
        if client is None:
            msg = "Matrix client is not ready for turn execution"
            raise RuntimeError(msg)
        return client

    async def handle_prepared_turn(self, turn: PreparedTurn) -> None:
        """Dispatch one logical turn emitted directly by the coalescing gate."""
        if await self.deps.selections.dispatch_selection_turn(turn):
            return
        for notice in (*turn.notices, *turn.queued_follow_up_notices):
            notice.release()
        timing_scope = event_timing_scope(turn.event.event_id)
        dispatch_timing = get_dispatch_pipeline_timing(turn.event.source)
        if dispatch_timing is not None:
            dispatch_timing.mark("gate_exit")
        async with self.deps.resolver.turn_lookup_scope():
            dispatch_start = time.monotonic()
            if not await self.dispatch_turn(turn):
                # Their callbacks deferred these sources to the gate, so they go back to the journal, whose ingress
                # decides them one by one.
                self.deps.retry_dispatch_sources(turn.room.room_id, turn.handled_turn.source_event_ids)
            emit_elapsed_timing(
                "coalescing.handle_turn.dispatch_text_message",
                dispatch_start,
                source_event_count=len(turn.handled_turn.source_event_ids),
                timing_scope=timing_scope,
            )

    async def dispatch_turn(self, turn: PreparedTurn) -> bool:
        """Run the normal text or command dispatch pipeline for a prepared turn.

        The turn's per-event claims become one claim on the whole turn, which this
        dispatch owns until a response task takes it. A command that bypassed the
        coalescing gate arrives here too, under the claim its ingress handed over.
        Returns False when the claims cannot cover the turn, because another live
        owner shares an event or a completed turn answered an alias; the caller
        still owns the sources then.
        """
        claim = await self.deps.turn_store.merge_claims(turn.claims, turn.handled_turn)
        if claim is None:
            return False
        await self._dispatch_claimed_text_turn(turn, claim)
        return True

    async def _dispatch_claimed_text_turn(self, turn: PreparedTurn, claim: TurnClaim) -> None:
        """Run the dispatch pipeline under the turn's merged claim until a response task takes it."""
        held: TurnClaim | None = claim

        def take_claim() -> TurnClaim:
            nonlocal held
            taken, held = held, None
            assert taken is not None
            return taken

        timing_scope_token = None
        try:
            dispatch_timing = get_dispatch_pipeline_timing(turn.event.source)
            prepared = await self._prepare_text_dispatch(
                turn,
                dispatch_timing=dispatch_timing,
            )
            if prepared is None:
                return
            timing_scope_token = timing_scope_context.set(event_timing_scope(turn.event.event_id))
            async with self.deps.ingress.admitted_reply_decision(
                turn.facts.run_requester_id,
                turn.room.room_id,
                unanswered=prepared.handled_turn.source_event_ids,
            ) as allowed:
                if not allowed:
                    return
                if await self._blocked_before_plan(
                    turn.room,
                    prepared,
                ):
                    return
                message_attachment_ids, trusted_attachment_ids, router_extra_content = _attachment_parts(prepared)
                if dispatch_timing is not None:
                    dispatch_timing.mark("dispatch_plan_start")
                plan = await self.deps.turn_policy.plan_turn(
                    turn.room,
                    turn.event,
                    prepared.dispatch,
                    has_active_response_for_target=self.deps.response_runner.has_active_response_for_target,
                    extra_content=router_extra_content or None,
                    media_events=list(turn.media_events) or None,
                    router_event=turn.media_events[0]
                    if turn.media_events and len(prepared.handled_turn.source_event_ids) == 1
                    else turn.event,
                )
                if dispatch_timing is not None:
                    dispatch_timing.mark("dispatch_plan_ready")
                await self._apply_turn_plan(
                    turn.room,
                    prepared,
                    plan,
                    message_attachment_ids=message_attachment_ids,
                    trusted_attachment_ids=trusted_attachment_ids,
                    take_claim=take_claim,
                )
        finally:
            if held is not None:
                held.release()
            if timing_scope_token is not None:
                timing_scope_context.reset(timing_scope_token)

    async def _prepare_text_dispatch(
        self,
        turn: PreparedTurn,
        *,
        dispatch_timing: DispatchPipelineTiming | None,
    ) -> _PreparedTextDispatch | None:
        if dispatch_timing is not None:
            dispatch_timing.mark("dispatch_start")
        dispatch_started_at = time.monotonic()
        if dispatch_timing is not None:
            dispatch_timing.mark("dispatch_prepare_start")
        prepared = await self._prepare_dispatch(turn)
        if dispatch_timing is not None:
            dispatch_timing.mark("dispatch_prepare_ready")
        if prepared is None:
            return None
        return _PreparedTextDispatch(
            turn=turn,
            # The turn record already carries each source's discovery alias, such as the human message a router
            # relayed, so edits and redactions of that message reach this responder's persisted runs.
            handled_turn=canonicalize_turn_record(
                turn.handled_turn,
                requester_id=prepared.requester_user_id,
            ),
            dispatch=prepared,
            dispatch_started_at=dispatch_started_at,
        )

    async def _blocked_before_plan(
        self,
        room: nio.MatrixRoom,
        prepared: _PreparedTextDispatch,
    ) -> bool:
        requester_user_id = prepared.turn.facts.run_requester_id
        event = prepared.turn.event
        visible_responses = self.deps.visible_responses
        if prepared.turn.command is not None:
            command_owned = await self.deps.command_executor.execute_if_owned(
                room=room,
                event=event,
                requester_user_id=requester_user_id,
                acts_for_requester=prepared.turn.facts.acts_for_requester,
                command=prepared.turn.command,
                target=prepared.dispatch.target,
                handled_turn=prepared.handled_turn,
            )
            if not command_owned:
                await visible_responses.settle_source_events_ignored(prepared.handled_turn)
            return True

        may_be_superseded = (
            prepared.dispatch.envelope.origin.may_be_superseded_by_newer_requester_turn
            and prepared.handled_turn.sources_all_from_requester(requester_user_id)
            # While another run still waits in the queue, whether another
            # requester's follow-ups or an entity's reply written for this
            # requester, a newer message from this requester may only be answered
            # after it, so letting it absorb this turn would answer out of receipt
            # order or not at all.
            and not prepared.turn.queues_other_run(requester_user_id)
        )
        thread_history = prepared.dispatch.context.thread_history
        thread_id = prepared.dispatch.target.resolved_thread_id
        if is_thread_history_degraded(thread_history):
            # Thread history degraded, so only pending journal events can prove a newer message.
            skips_turn = await has_newer_unresponded_journal_thread_event(
                room_id=room.room_id,
                event=event,
                requester_user_id=requester_user_id,
                thread_id=thread_id,
                may_be_superseded_by_newer_requester_turn=may_be_superseded,
                pending_turns=self.deps.pending_turns,
                classify=self.deps.ingress.classify_source,
                is_handled=partial(self.deps.turn_store.handled_among, room.room_id),
                for_another_participant=partial(self._for_another_participant, room),
                logger=self.deps.logger,
            )
            if may_be_superseded and not skips_turn:
                self.deps.logger.warning(
                    "Thread replay guard degraded; proceeding without negative newer-message proof",
                    event_id=event.event_id,
                    room_id=room.room_id,
                    thread_id=thread_id,
                    thread_read_degraded=True,
                )
        else:
            skips_turn = await self._has_newer_unresponded_in_thread(
                room,
                event,
                requester_user_id,
                thread_history,
                may_be_superseded_by_newer_requester_turn=may_be_superseded,
            )
        if skips_turn:
            return await visible_responses.settle_superseded_turn(prepared.handled_turn)
        return False

    async def _apply_turn_plan(
        self,
        room: nio.MatrixRoom,
        prepared: _PreparedTextDispatch,
        plan: DispatchPlan,
        *,
        message_attachment_ids: list[str],
        trusted_attachment_ids: list[str],
        take_claim: Callable[[], TurnClaim],
    ) -> None:
        visible_responses = self.deps.visible_responses
        if plan.kind == "ignore":
            router_outcome = (
                await self._router_handled_turn_outcome(room.room_id, prepared.handled_turn)
                if plan.ignore_reason == "router"
                else None
            )
            if router_outcome is not None:
                await self.deps.turn_store.record_responded_turn(router_outcome, room_id=room.room_id)
            else:
                await visible_responses.settle_source_events_ignored(prepared.handled_turn)
            return
        if plan.kind == "route":
            await self._execute_route_plan(room, prepared, plan)
            return

        response_action = plan.response_action
        assert response_action is not None
        # Orderly shutdown has closed task ownership, so no response is planned after it.
        if self.deps.runtime.phase is RunPhase.SHUTTING_DOWN:
            raise ResponseAdmissionRefusedError
        response_history_scope = (
            self.deps.turn_store.response_history_scope(
                response_action,
                requester_user_id=prepared.dispatch.requester_user_id,
            )
            if response_action.kind in {"individual", "team"}
            else None
        )

        payload_inputs = DispatchPayloadInputs(
            message_attachment_ids=tuple(message_attachment_ids),
            trusted_attachment_ids=tuple(trusted_attachment_ids),
            media_events=prepared.turn.media_events,
            raw_audio_fallback=prepared.turn.payload.raw_audio_fallback is True,
            voice_transcript=prepared.turn.payload.voice_transcript is True,
        )
        handled_turn = self.deps.turn_store.attach_response_context(
            prepared.handled_turn,
            history_scope=response_history_scope,
            conversation_target=prepared.dispatch.target,
        )
        pending_turn = await self.deps.turn_store.record_pending_turn(handled_turn)
        if pending_turn is None or await visible_responses.settle_deleted_sources(pending_turn, room_id=room.room_id):
            return
        handled_turn = pending_turn.record

        # The inbox handoff is complete once the runner takes the conversation's
        # response lock; the response itself keeps running on a runner-owned task.
        response_started = asyncio.Event()
        response_task = self.deps.response_runner.inbox.track_inbox_response(
            self._execute_response_action(
                room,
                prepared.turn.event,
                prepared.dispatch,
                response_action,
                payload_inputs,
                dispatch_started_at=prepared.dispatch_started_at,
                handled_turn=handled_turn,
                matrix_run_metadata=self.deps.turn_store.build_run_metadata(handled_turn),
                on_lifecycle_lock_acquired=response_started.set,
                reconcile_visible_response=pending_turn.resumes_response,
            ),
            name=f"inbox_response:{prepared.turn.event.event_id}",
            room_id=room.room_id,
            recovery_proof_ready=lambda: self.deps.turn_store.response_recovery_ready(handled_turn),
            # The claim moves to the response task, which releases it when it ends. If this dispatch is
            # cancelled while waiting for the lifecycle lock, it must not reopen the source meanwhile.
            claim=take_claim(),
            source_event_ids=handled_turn.source_event_ids,
        )
        started_wait = asyncio.ensure_future(response_started.wait())
        try:
            await asyncio.wait({started_wait, response_task}, return_when=asyncio.FIRST_COMPLETED)
        finally:
            started_wait.cancel()
        # Surface pre-lock failures to the caller's containment; post-lock
        # failures (including a fast failure racing the FIRST_COMPLETED wait)
        # belong to the runner-owned task. Pre-lock CANCELLATION is
        # deliberately NOT surfaced: this coroutine was not itself cancelled,
        # and re-raising CancelledError here would corrupt the gate drain's
        # own cancellation state.
        if response_task.done() and not response_task.cancelled() and not response_started.is_set():
            response_task.result()

    async def _execute_route_plan(
        self,
        room: nio.MatrixRoom,
        prepared: _PreparedTextDispatch,
        plan: DispatchPlan,
    ) -> None:
        turn = prepared.turn
        route_event = plan.router_event or turn.event
        single_direct_media_route = (
            is_matrix_media_dispatch_event(route_event)
            and turn.media_events == (route_event,)
            and prepared.handled_turn.source_event_ids == (turn.event.event_id,)
        )
        tracked_turn = _tracked_route_turn(
            prepared,
            route_event=route_event,
            single_direct_media_route=single_direct_media_route,
        )
        handled_turn = (
            self.deps.turn_store.attach_response_context(
                tracked_turn,
                history_scope=None,
                conversation_target=prepared.dispatch.target,
            )
            if tracked_turn is not None
            else None
        )
        # Config application waits until the router's selection and its relay delivery finish.
        async with self.deps.runtime.admission_fence():
            await execute_router_relay(
                self.deps,
                room,
                route_event,
                prepared.dispatch.context.thread_history,
                prepared.dispatch.target.resolved_thread_id,
                message=turn.event.body if turn.media_events else plan.router_message,
                requester_user_id=prepared.dispatch.requester_user_id,
                extra_content=plan.extra_content,
                media_events=None if single_direct_media_route else plan.media_events,
                handled_turn=handled_turn,
                scheduled_prompt=turn.event.body if prepared.dispatch.scheduled_history_budget is not None else None,
            )

    async def _has_newer_unresponded_in_thread(
        self,
        room: nio.MatrixRoom,
        event: PreparedIngress,
        requester_user_id: str,
        thread_history: Sequence[ResolvedVisibleMessage],
        *,
        may_be_superseded_by_newer_requester_turn: bool,
    ) -> bool:
        """Return True when a newer unresponded message from the same requester exists."""
        return await has_newer_unresponded_in_thread(
            event,
            requester_user_id,
            thread_history,
            may_be_superseded_by_newer_requester_turn=may_be_superseded_by_newer_requester_turn,
            classify=self.deps.ingress.classify_source,
            is_handled=partial(self.deps.turn_store.handled_among, room.room_id),
            for_another_participant=partial(self._for_another_participant, room),
            logger=self.deps.logger,
        )

    async def _for_another_participant(
        self,
        room: nio.MatrixRoom,
        event_id: str,
        source: Mapping[str, object],
    ) -> bool:
        """Return whether a message mentions only other agents or people, so this agent ignores it.

        A voice message is addressed by the transcript it was prepared with, not by its audio event.
        """
        prepared_voice = await self.deps.turn_store.prepared_voice_for_source(event_id)
        content = prepared_voice.to_content() if prepared_voice is not None else source.get("content")
        mentions = mentions_another_participant(
            {"content": content},
            self.deps.matrix_id,
            self.deps.runtime.config,
            self.deps.runtime_paths,
            room=room,
        )
        return mentions is True

    async def _agent_mention_may_wake(
        self,
        room: nio.MatrixRoom,
        envelope: MessageEnvelope,
        context: MessageContext,
    ) -> bool:
        """Return whether an agent-written message may wake this entity.

        The person it acts for must still be joined to the room, and agents stop waking each
        other once a conversation has the configured number of consecutive agent messages.
        """
        config = self.deps.runtime.config
        if envelope.origin.acting_sender_id is not None and not await is_requester_joined_to_room(
            self._client(),
            room,
            envelope.requester_id,
            config,
            self.deps.runtime_paths,
        ):
            self.deps.logger.info("agent_mention_ignored_requester_not_joined", requester_id=envelope.requester_id)
            return False
        limit = config.defaults.max_consecutive_agent_replies
        if await self.deps.resolver.consecutive_agent_messages(room.room_id, context.thread_id, limit=limit) >= limit:
            self.deps.logger.info("agent_mention_ignored_consecutive_agent_limit", limit=limit)
            return False
        return True

    async def _settle_unmentioned_managed_sender_before_hydration(
        self,
        turn: PreparedTurn,
        context_event: PreparedIngress,
    ) -> bool:
        """Settle managed chatter before thread hydration can make it retryable."""
        room, event, facts, handled_turn = turn.room, turn.event, turn.facts, turn.handled_turn
        requester_user_id = facts.run_requester_id
        origin = facts.origin
        if not origin.blocks_unmentioned_managed_sender:
            return False
        mentioned_agents, am_i_mentioned = self.deps.resolver.pre_hydration_mentions(
            room=room,
            event=event,
            payload_metadata=turn.payload,
        )
        if am_i_mentioned or not handled_turn.sources_all_from_requester(requester_user_id):
            return False
        target = self.deps.resolver.build_message_target(
            room_id=room.room_id,
            thread_id=turn.coalescing_key.thread_id,
            reply_to_event_id=event.event_id,
            event_source=context_event.source,
        )
        envelope = self.deps.resolver.build_envelope(
            event=event,
            facts=facts,
            target=target,
            mentioned_agents=mentioned_agents,
            attachment_ids=turn.payload.attachment_ids,
            active_follow_up=turn.active_follow_up,
        )
        if await self._message_received_hook_settles_turn(
            room=room,
            envelope=envelope,
            handled_turn=handled_turn,
        ):
            return True
        self.deps.logger.debug(
            "ignore_unmentioned_agent_event",
            agent=origin.requester_entity_name,
            event_label="message",
            user_id=requester_user_id,
        )
        await self.deps.visible_responses.settle_source_events_ignored(handled_turn)
        return True

    async def _message_received_hook_settles_turn(
        self,
        *,
        room: nio.MatrixRoom,
        envelope: MessageEnvelope,
        handled_turn: TurnRecord,
    ) -> bool:
        """Run the authorized ingress hook and settle when it blocks dispatch."""
        hooks_start = time.monotonic()
        async with self.deps.ingress.admitted_reply_decision(
            envelope.requester_id,
            room.room_id,
            unanswered=handled_turn.source_event_ids,
        ) as allowed:
            if not allowed:
                return True
            suppressed = await self.deps.ingress_hook_runner.emit_message_received_hooks(
                envelope=envelope,
                correlation_id=envelope.source_event_id,
                policy=hook_ingress_policy(envelope),
            )
        emit_elapsed_timing(
            "dispatch_handoff.prepare_dispatch.emit_message_received_hooks",
            hooks_start,
            suppressed=suppressed,
        )
        if not suppressed:
            return False
        await self.deps.visible_responses.settle_source_events_ignored(handled_turn)
        return True

    async def _prepare_dispatch(self, turn: PreparedTurn) -> PreparedDispatch | None:
        """Build the shared dispatch context for one prepared inbound turn."""
        room, event, facts, handled_turn = turn.room, turn.event, turn.facts, turn.handled_turn
        requester_user_id = facts.run_requester_id
        thread_id = turn.coalescing_key.thread_id
        context_event = _room_level_context_event(event) if thread_id is None else event
        if await self._settle_unmentioned_managed_sender_before_hydration(turn, context_event):
            return None
        target_start = time.monotonic()
        # The thread ingress decided, under the room's current thread mode. The
        # context reads the thread the target says the source is in, so the
        # two cannot disagree about which conversation this turn belongs to.
        target = self.deps.resolver.build_message_target(
            room_id=room.room_id,
            thread_id=thread_id,
            # A response to a finished reply answers the message that reply answered.
            reply_to_event_id=self.deps.ingress.entity_final_reply_original_event_id(event) or event.event_id,
            event_source=context_event.source,
        )
        emit_elapsed_timing(
            "dispatch_handoff.prepare_dispatch.build_message_target",
            target_start,
            resolved_thread_id=target.resolved_thread_id,
        )
        extract_context_start = time.monotonic()
        # A router relay of a human's message reads only the context the relay names.
        if turn.command is None and facts.router_relay and facts.original_sender_is_human:
            context = await self.deps.resolver.extract_trusted_router_relay_context(
                room,
                context_event,
                thread_id=target.source_thread_id,
                payload_metadata=turn.payload,
            )
            context_path = "trusted_router_relay"
        else:
            context = await self.deps.resolver.extract_dispatch_context(
                room,
                context_event,
                thread_id=target.source_thread_id,
                payload_metadata=turn.payload,
            )
            context_path = "normal" if turn.command is None else "command"
        emit_elapsed_timing(
            "dispatch_handoff.prepare_dispatch.extract_context",
            extract_context_start,
            path=context_path,
        )
        envelope_start = time.monotonic()
        envelope = self.deps.resolver.build_envelope(
            event=event,
            facts=facts,
            target=target,
            mentioned_agents=context.mentioned_agents,
            attachment_ids=turn.payload.attachment_ids,
            active_follow_up=turn.active_follow_up,
        )
        emit_elapsed_timing(
            "dispatch_handoff.prepare_dispatch.build_message_envelope",
            envelope_start,
            source_kind=envelope.source_kind,
        )
        if await self._message_received_hook_settles_turn(
            room=room,
            envelope=envelope,
            handled_turn=handled_turn,
        ):
            return None

        origin = envelope.origin
        sender_agent_name = origin.requester_entity_name
        blocks_unmentioned_managed_sender = origin.blocks_unmentioned_managed_sender
        if (
            blocks_unmentioned_managed_sender
            and not context.am_i_mentioned
            and handled_turn.sources_all_from_requester(requester_user_id)
        ):
            self.deps.logger.debug(
                "ignore_unmentioned_agent_event",
                agent=sender_agent_name,
                event_label="message",
                user_id=requester_user_id,
            )
            await self.deps.visible_responses.settle_source_events_ignored(handled_turn)
            return None
        if blocks_unmentioned_managed_sender and not await self._agent_mention_may_wake(room, envelope, context):
            await self.deps.visible_responses.settle_source_events_ignored(handled_turn)
            return None

        return PreparedDispatch(
            requester_user_id=requester_user_id,
            context=context,
            target=target,
            correlation_id=event.event_id,
            envelope=envelope,
            current_prompt_is_structured=turn.current_prompt_is_structured,
            scheduled_history_budget=_scheduled_history_budget_for_dispatch(event, origin.intent),
            scheduled_model=_scheduled_model_for_dispatch(event, origin.intent),
        )

    async def _router_handled_turn_outcome(
        self,
        room_id: str,
        handled_turn: TurnRecord,
    ) -> TurnRecord | None:
        """Return the turn an ignored router turn records answered: by the visible echo it published, if any.

        The router takes its echo as its answer here deliberately; publishing an
        echo never records one by itself.
        """
        echoes = await self.deps.turn_store.finalized_visible_echoes(handled_turn.source_event_ids)
        if not echoes:
            return None
        handled = await self.deps.turn_store.handled_among(room_id, handled_turn.source_event_ids)
        if handled.issuperset(handled_turn.source_event_ids):
            return None
        return canonicalize_turn_record(handled_turn, response_event_id=echoes[0])

    async def _execute_response_action(
        self,
        room: nio.MatrixRoom,
        event: DispatchEvent,
        dispatch: PreparedDispatch,
        action: ResponseAction,
        payload_inputs: DispatchPayloadInputs,
        *,
        dispatch_started_at: float,
        handled_turn: TurnRecord,
        matrix_run_metadata: dict[str, Any] | None = None,
        on_lifecycle_lock_acquired: Callable[[], None] | None = None,
        reconcile_visible_response: bool = False,
    ) -> None:
        """Execute one final response path for a prepared dispatch action."""
        action = self.deps.turn_policy.effective_response_action(action)
        dispatch_timing = get_dispatch_pipeline_timing(event.source)
        if dispatch_timing is not None:
            dispatch_timing.note(response_action_kind=action.kind)

        with bound_log_context(
            agent_id=self.deps.agent_name,
            requester_id=dispatch.requester_user_id,
            room_id=dispatch.target.room_id,
            thread_id=dispatch.target.resolved_thread_id,
            session_id=dispatch.target.session_id,
            reply_to_event_id=event.event_id,
            correlation_id=dispatch.correlation_id,
        ):
            if action.kind == "reject":
                assert action.rejection_message is not None
                response_event_id = (
                    await self.deps.visible_responses.recovered_response_event_id(
                        handled_turn,
                        room_id=dispatch.target.room_id,
                    )
                    if reconcile_visible_response
                    else None
                )
                response_event_id = await self.deps.visible_responses.deliver_recoverable_text(
                    handled_turn,
                    target=dispatch.target,
                    response_text=action.rejection_message,
                    # One rejection, then the turn is terminal, so this send
                    # satisfies the outbox's once-per-(turn, stage) rule.
                    recovered_response_event_id=response_event_id,
                )
                await self.deps.turn_store.record_responded_turn(
                    canonicalize_turn_record(handled_turn, response_event_id=response_event_id),
                )
                if dispatch_timing is not None and response_event_id is not None:
                    dispatch_timing.mark_first_visible_reply("final", substantive=True)
                    dispatch_timing.mark("response_complete")
                    dispatch_timing.emit_summary(self.deps.logger, outcome="reject")
                return

            if not dispatch.context.am_i_mentioned:
                self.deps.logger.info("Will respond: only agent in thread")

            context_ready_monotonic = time.monotonic()

            if dispatch_timing is not None and isinstance(dispatch.context.thread_history, ThreadHistoryResult):
                dispatch_timing.note(**dispatch.context.thread_history.diagnostics)

            self.deps.logger.info("Processing", event_id=event.event_id)
            current_timestamp_ms = normalize_timestamp_ms(event.server_timestamp)
            target_member_names: tuple[str, ...] | None = None
            team_mode: TeamMode | None = None
            if action.kind == "team":
                assert action.form_team is not None
                assert action.form_team.mode is not None
                target_member_names = action.form_team.eligible_member_names
                if action.form_team.intent is not TeamIntent.CONFIGURED_TEAM and event.body:
                    team_mode = await select_ad_hoc_team_mode(
                        event.body,
                        action.form_team.eligible_member_names,
                        self.deps.runtime.config,
                        self.deps.runtime_paths,
                    )
            payload_preparation = ResponsePayloadPreparation(
                dispatch=dispatch,
                prompt=event.body,
                action_kind=action.kind,
                payload_inputs=payload_inputs,
                target_member_names=target_member_names,
                dispatch_started_at=dispatch_started_at,
                context_ready_monotonic=context_ready_monotonic,
            )

            response_request = ResponseRequest(
                thread_history=dispatch.context.thread_history,
                member_display_names=room_member_display_names(room),
                prompt=event.body,
                sources=ResponseSources(
                    pending_event_ids=tuple(
                        dict.fromkeys((event.event_id, *handled_turn.source_event_ids)),
                    ),
                    logical_source_event_ids=handled_turn.source_event_ids,
                    discovery_event_ids=handled_turn.discovery_event_ids,
                ),
                user_id=dispatch.requester_user_id,
                # A replay finds the reply its earlier attempt left through the reply's records.
                response_envelope=dispatch.envelope,
                correlation_id=dispatch.correlation_id,
                matrix_run_metadata=matrix_run_metadata,
                requires_model_history_refresh=dispatch.context.requires_model_history_refresh,
                scheduled_history_budget=dispatch.scheduled_history_budget,
                scheduled_model=dispatch.scheduled_model,
                participation=action.participation,
                payload_preparation=payload_preparation,
                current_timestamp_ms=current_timestamp_ms,
                current_prompt_is_structured=dispatch.current_prompt_is_structured,
                pipeline_timing=dispatch_timing,
                on_lifecycle_lock_acquired=on_lifecycle_lock_acquired,
                prepare_source_turn=lambda history: self.deps.turn_store.prepare_pending_response_source(
                    target=dispatch.target,
                    source_event_ids=handled_turn.source_event_ids,
                    thread_history=history,
                ),
            )
            fate = await run_response_action(self.deps.response_runner, response_request, action, team_mode=team_mode)
            # A reply's records settle the sources it took and record the turn answered; the rest settle here.
            await self.deps.visible_responses.settle_turn_fate(fate, handled_turn, room_id=dispatch.target.room_id)
