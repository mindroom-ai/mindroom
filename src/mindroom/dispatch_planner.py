"""Dispatch planning and explicit executor paths extracted from ``bot.py``."""

from __future__ import annotations

import asyncio
import time
from collections.abc import Awaitable, Callable, Sequence
from dataclasses import dataclass
from typing import TYPE_CHECKING, Any, Literal, Protocol

import nio

from mindroom.attachments import merge_attachment_ids, parse_attachment_ids_from_event_source
from mindroom.authorization import (
    filter_agents_by_sender_permissions,
    get_available_agents_for_sender,
    get_effective_sender_id_for_reply_permissions,
    is_sender_allowed_for_agent_reply,
)
from mindroom.commands.handler import (
    CommandEvent,
    CommandHandlerContext,
    _run_skill_command_tool,
    handle_command,
)
from mindroom.constants import (
    ATTACHMENT_IDS_KEY,
    ORIGINAL_SENDER_KEY,
    ROUTER_AGENT_NAME,
    STREAM_STATUS_COMPLETED,
    STREAM_STATUS_KEY,
    RuntimePaths,
)
from mindroom.conversation_resolver import (
    ConversationResolver,
    DispatchEvent,
    MediaDispatchEvent,
    MessageContext,
    TextDispatchEvent,
)
from mindroom.delivery_gateway import (
    DeliveryGateway,
    SendTextRequest,
    SuppressedPlaceholderCleanupError,
)
from mindroom.error_handling import get_user_friendly_error_message
from mindroom.handled_turns import HandledTurnLedger, HandledTurnState
from mindroom.hooks import (
    EVENT_MESSAGE_ENRICH,
    EVENT_MESSAGE_RECEIVED,
    EVENT_SYSTEM_ENRICH,
    EnrichmentItem,
    HookContextSupport,
    MessageEnrichContext,
    MessageEnvelope,
    MessageReceivedContext,
    SystemEnrichContext,
    emit,
    emit_collect,
    render_enrichment_block,
)
from mindroom.hooks.ingress import HookIngressPolicy, hook_ingress_policy, is_automation_source_kind
from mindroom.inbound_turn_normalizer import DispatchPayload, InboundTurnNormalizer, TextNormalizationRequest
from mindroom.matrix.identity import MatrixID, extract_agent_name, is_agent_id
from mindroom.matrix.rooms import is_dm_room
from mindroom.message_target import MessageTarget
from mindroom.post_response_effects import record_handled_turn
from mindroom.response_coordinator import ResponseCoordinator, ResponseRequest
from mindroom.routing import suggest_agent_for_message
from mindroom.team_runtime_resolution import resolve_live_shared_agent_names
from mindroom.teams import (
    TeamIntent,
    TeamMode,
    TeamOutcome,
    TeamResolution,
    decide_team_formation,
    resolve_configured_team,
)
from mindroom.thread_utils import (
    get_agents_in_thread,
    get_all_mentioned_agents_in_thread,
    get_configured_agents_for_room,
    has_multiple_non_agent_users_in_thread,
    should_agent_respond,
)
from mindroom.timing import timed

if TYPE_CHECKING:
    from pathlib import Path

    import structlog

    from mindroom.bot_runtime_view import BotRuntimeView
    from mindroom.coalescing import PreparedTextEvent
    from mindroom.commands.parsing import Command
    from mindroom.matrix.client import ResolvedVisibleMessage
    from mindroom.tool_system.runtime_context import ToolRuntimeSupport

type DispatchPayloadBuilder = Callable[[MessageContext], Awaitable[DispatchPayload]]


class PreparedHookedPayload(Protocol):
    """Typed payload surface returned after message enrichment hooks run."""

    payload: DispatchPayload
    envelope: MessageEnvelope
    strip_transient_enrichment_after_run: bool
    system_enrichment_items: tuple[EnrichmentItem, ...]


@dataclass(frozen=True)
class _ResolvedPreparedHookedPayload:
    """Concrete prepared payload used after planner-side enrichment updates."""

    payload: DispatchPayload
    envelope: MessageEnvelope
    strip_transient_enrichment_after_run: bool
    system_enrichment_items: tuple[EnrichmentItem, ...]


@dataclass
class DispatchHookService:
    """Own planner-facing hook ingress and enrichment behavior."""

    hook_context: HookContextSupport

    async def emit_message_received_hooks(
        self,
        *,
        envelope: MessageEnvelope,
        correlation_id: str,
        policy: HookIngressPolicy,
    ) -> bool:
        """Emit message:received and return whether hooks suppressed processing."""
        if not self.hook_context.registry.has_hooks(EVENT_MESSAGE_RECEIVED):
            return False
        if not policy.rerun_message_received:
            return False

        context = MessageReceivedContext(
            **self.hook_context.base_kwargs(EVENT_MESSAGE_RECEIVED, correlation_id),
            envelope=envelope,
            skip_plugin_names=policy.skip_message_received_plugin_names,
        )
        await emit(self.hook_context.registry, EVENT_MESSAGE_RECEIVED, context)
        return context.suppress

    async def apply_message_enrichment(
        self,
        dispatch: PreparedDispatch,
        payload: DispatchPayload,
        *,
        target_entity_name: str,
        target_member_names: tuple[str, ...] | None,
    ) -> PreparedHookedPayload:
        """Run message:enrich and return the model-facing payload."""
        envelope = MessageEnvelope(
            source_event_id=dispatch.envelope.source_event_id,
            room_id=dispatch.envelope.room_id,
            target=dispatch.envelope.target,
            requester_id=dispatch.envelope.requester_id,
            sender_id=dispatch.envelope.sender_id,
            body=dispatch.envelope.body,
            attachment_ids=(
                tuple(payload.attachment_ids)
                if payload.attachment_ids is not None
                else dispatch.envelope.attachment_ids
            ),
            mentioned_agents=dispatch.envelope.mentioned_agents,
            agent_name=target_entity_name,
            source_kind=dispatch.envelope.source_kind,
            hook_source=dispatch.envelope.hook_source,
            message_received_depth=dispatch.envelope.message_received_depth,
        )
        model_prompt: str | None = None
        strip_transient_enrichment_after_run = False
        if self.hook_context.registry.has_hooks(EVENT_MESSAGE_ENRICH):
            context = MessageEnrichContext(
                **self.hook_context.base_kwargs(EVENT_MESSAGE_ENRICH, dispatch.correlation_id),
                envelope=envelope,
                target_entity_name=target_entity_name,
                target_member_names=target_member_names,
            )
            items = await emit_collect(self.hook_context.registry, EVENT_MESSAGE_ENRICH, context)
            if items:
                enrichment_block = render_enrichment_block(items)
                model_prompt = f"{payload.prompt.rstrip()}\n\n{enrichment_block}"
                strip_transient_enrichment_after_run = True

        return _ResolvedPreparedHookedPayload(
            payload=DispatchPayload(
                prompt=payload.prompt,
                model_prompt=model_prompt,
                media=payload.media,
                attachment_ids=payload.attachment_ids,
            ),
            envelope=envelope,
            strip_transient_enrichment_after_run=strip_transient_enrichment_after_run,
            system_enrichment_items=(),
        )

    async def apply_system_enrichment(
        self,
        dispatch: PreparedDispatch,
        envelope: MessageEnvelope,
        *,
        target_entity_name: str,
        target_member_names: tuple[str, ...] | None,
    ) -> list[EnrichmentItem]:
        """Run system:enrich and return system-prompt enrichment items."""
        if not self.hook_context.registry.has_hooks(EVENT_SYSTEM_ENRICH):
            return []
        context = SystemEnrichContext(
            **self.hook_context.base_kwargs(EVENT_SYSTEM_ENRICH, dispatch.correlation_id),
            envelope=envelope,
            target_entity_name=target_entity_name,
            target_member_names=target_member_names,
        )
        return await emit_collect(self.hook_context.registry, EVENT_SYSTEM_ENRICH, context)


def _received_monotonic_from_source(source: dict[str, Any] | None) -> float | None:
    """Return the inbound receipt timestamp persisted on one event source."""
    if not isinstance(source, dict):
        return None
    raw_received_monotonic = source.get("com.mindroom.received_monotonic")
    if isinstance(raw_received_monotonic, float | int):
        return float(raw_received_monotonic)
    return None


@dataclass(frozen=True)
class ResponseAction:
    """Result of the shared team-formation and should-respond decision."""

    kind: Literal["skip", "team", "individual", "reject"]
    form_team: TeamResolution | None = None
    rejection_message: str | None = None


@dataclass(frozen=True)
class PreparedDispatch:
    """Common dispatch context reused across text and media ingress handlers."""

    requester_user_id: str
    context: MessageContext
    target: MessageTarget
    correlation_id: str
    envelope: MessageEnvelope


@dataclass(frozen=True)
class DispatchPlan:
    """Planner output for one normalized inbound turn."""

    kind: Literal["ignore", "route", "respond"]
    response_action: ResponseAction | None = None
    router_message: str | None = None
    extra_content: dict[str, Any] | None = None
    media_events: list[MediaDispatchEvent] | None = None
    handled_turn: HandledTurnState | None = None
    router_event: DispatchEvent | None = None
    handled_turn_outcome: HandledTurnState | None = None


@dataclass(frozen=True)
class DispatchPlannerDeps:
    """Explicit collaborators needed by dispatch planning and execution."""

    runtime: BotRuntimeView
    logger: structlog.stdlib.BoundLogger
    handled_turn_ledger: HandledTurnLedger
    runtime_paths: RuntimePaths
    storage_path: Path
    agent_name: str
    matrix_id: MatrixID
    normalizer: InboundTurnNormalizer
    resolver: ConversationResolver
    delivery_gateway: DeliveryGateway
    response_coordinator: ResponseCoordinator
    hook_service: DispatchHookService
    tool_runtime: ToolRuntimeSupport


@dataclass(frozen=True)
class DispatchPlanner:
    """Own dispatch planning plus explicit command and router executor paths."""

    deps: DispatchPlannerDeps

    def _client(self) -> nio.AsyncClient:
        client = self.deps.runtime.client
        if client is None:
            msg = "Matrix client is not ready for dispatch planning"
            raise RuntimeError(msg)
        return client

    def _matrix_id(self) -> MatrixID:
        return self.deps.matrix_id

    def _mark_source_events_responded(self, handled_turn: HandledTurnState) -> None:
        """Mark one or more source events as handled by the same response."""
        record_handled_turn(self.deps.handled_turn_ledger, handled_turn)

    def _requester_user_id_for_event(self, event: CommandEvent) -> str:
        """Return the effective requester for per-user reply checks."""
        source = event.source if isinstance(event.source, dict) else None
        content = source.get("content") if source is not None else None
        if (
            event.sender == self.deps.matrix_id.full_id
            and isinstance(content, dict)
            and isinstance(content.get(ORIGINAL_SENDER_KEY), str)
        ):
            return content[ORIGINAL_SENDER_KEY]
        return get_effective_sender_id_for_reply_permissions(
            event.sender,
            source,
            self.deps.runtime.config,
            self.deps.runtime_paths,
        )

    def can_reply_to_sender(self, sender_id: str) -> bool:
        """Return whether this entity may reply to ``sender_id``."""
        return is_sender_allowed_for_agent_reply(
            sender_id,
            self.deps.agent_name,
            self.deps.runtime.config,
            self.deps.runtime_paths,
        )

    def materializable_agent_names(self) -> set[str] | None:
        """Return live shared agent names that can currently answer."""
        orchestrator = self.deps.runtime.orchestrator
        if orchestrator is None:
            return None
        return resolve_live_shared_agent_names(orchestrator, config=self.deps.runtime.config)

    def filter_materializable_agents(
        self,
        agent_ids: list[MatrixID],
        materializable_agent_names: set[str] | None,
    ) -> list[MatrixID]:
        """Keep only agents that can currently be materialized."""
        if materializable_agent_names is None:
            return agent_ids
        return [
            agent_id
            for agent_id in agent_ids
            if (agent_id.agent_name(self.deps.runtime.config, self.deps.runtime_paths) or agent_id.username)
            in materializable_agent_names
        ]

    def response_owner_for_team_resolution(
        self,
        form_team: TeamResolution,
        responder_pool: list[MatrixID],
    ) -> MatrixID | None:
        """Return the single live bot that should surface this resolution."""
        if form_team.outcome is TeamOutcome.NONE:
            return None

        response_owners = form_team.eligible_members
        if (
            not response_owners
            and form_team.outcome is TeamOutcome.REJECT
            and form_team.intent is TeamIntent.EXPLICIT_MEMBERS
        ):
            response_owners = responder_pool

        if not response_owners:
            return None
        return min(response_owners, key=lambda x: x.full_id)

    def team_response_action(
        self,
        form_team: TeamResolution,
        responder_pool: list[MatrixID],
    ) -> ResponseAction | None:
        """Return the response action implied by one team resolution."""
        if form_team.outcome is TeamOutcome.NONE:
            return None
        response_owner = self.response_owner_for_team_resolution(form_team, responder_pool)
        if response_owner is None:
            return ResponseAction(kind="skip")
        if self._matrix_id() != response_owner:
            return ResponseAction(kind="skip")
        if form_team.outcome is TeamOutcome.TEAM:
            return ResponseAction(kind="team", form_team=form_team)
        if form_team.outcome is TeamOutcome.INDIVIDUAL:
            return ResponseAction(kind="individual")
        assert form_team.reason is not None
        return ResponseAction(
            kind="reject",
            form_team=form_team,
            rejection_message=form_team.reason,
        )

    def configured_team_response_action(self) -> ResponseAction | None:
        """Return the configured-team response action for this bot when it represents a team."""
        team_config = self.deps.runtime.config.teams.get(self.deps.agent_name)
        if team_config is None:
            return None
        configured_mode = TeamMode.COORDINATE if team_config.mode == "coordinate" else TeamMode.COLLABORATE
        team_agents = [
            MatrixID.from_agent(agent_name, self.deps.matrix_id.domain, self.deps.runtime_paths)
            for agent_name in team_config.agents
        ]
        team_resolution = resolve_configured_team(
            self.deps.agent_name,
            team_agents,
            configured_mode,
            self.deps.runtime.config,
            self.deps.runtime_paths,
            materializable_agent_names=self.materializable_agent_names(),
        )
        if team_resolution.outcome is TeamOutcome.TEAM:
            return ResponseAction(kind="team", form_team=team_resolution)
        if team_resolution.outcome is TeamOutcome.REJECT and team_resolution.reason is not None:
            return ResponseAction(
                kind="reject",
                form_team=team_resolution,
                rejection_message=team_resolution.reason,
            )
        return None

    async def decide_team_for_sender(
        self,
        agents_in_thread: list[MatrixID],
        context: MessageContext,
        room: nio.MatrixRoom,
        requester_user_id: str,
        message: str,
        is_dm: bool,
        *,
        available_agents_in_room: list[MatrixID] | None = None,
        materializable_agent_names: set[str] | None = None,
    ) -> TeamResolution:
        """Decide team formation using sender-visible candidates without losing explicit intent."""
        all_mentioned_in_thread = get_all_mentioned_agents_in_thread(
            context.thread_history,
            self.deps.runtime.config,
            self.deps.runtime_paths,
        )
        if available_agents_in_room is None:
            available_agents_in_room = get_available_agents_for_sender(
                room,
                requester_user_id,
                self.deps.runtime.config,
                self.deps.runtime_paths,
            )
        if materializable_agent_names is None:
            materializable_agent_names = self.materializable_agent_names()
        return await decide_team_formation(
            self._matrix_id(),
            context.mentioned_agents,
            agents_in_thread,
            all_mentioned_in_thread,
            room=room,
            message=message,
            config=self.deps.runtime.config,
            runtime_paths=self.deps.runtime_paths,
            is_dm_room=is_dm,
            is_thread=context.is_thread,
            available_agents_in_room=available_agents_in_room,
            materializable_agent_names=materializable_agent_names,
        )

    async def prepare_dispatch(
        self,
        room: nio.MatrixRoom,
        event: DispatchEvent,
        requester_user_id: str,
        *,
        event_label: str,
        handled_turn: HandledTurnState,
    ) -> PreparedDispatch | None:
        """Run shared sender-gating, context extraction, and hook ingress preparation."""
        context = await self.deps.resolver.extract_dispatch_context(room, event)
        target = self.deps.resolver.build_message_target(
            room_id=room.room_id,
            thread_id=context.thread_id,
            reply_to_event_id=event.event_id,
            event_source=event.source,
        )
        correlation_id = event.event_id
        envelope = self.deps.resolver.build_message_envelope(
            room_id=room.room_id,
            event=event,
            requester_user_id=requester_user_id,
            context=context,
            target=target,
        )
        ingress_policy = hook_ingress_policy(envelope)
        suppressed = await self.deps.hook_service.emit_message_received_hooks(
            envelope=envelope,
            correlation_id=correlation_id,
            policy=ingress_policy,
        )
        if suppressed:
            self._mark_source_events_responded(handled_turn)
            return None

        sender_agent_name = extract_agent_name(requester_user_id, self.deps.runtime.config, self.deps.runtime_paths)
        if sender_agent_name and not context.am_i_mentioned and not ingress_policy.bypass_unmentioned_agent_gate:
            self.deps.logger.debug(f"Ignoring {event_label} from other agent (not mentioned)")
            return None

        return PreparedDispatch(
            requester_user_id=requester_user_id,
            context=context,
            target=target,
            correlation_id=correlation_id,
            envelope=envelope,
        )

    async def resolve_text_dispatch_event(self, event: TextDispatchEvent) -> PreparedTextEvent:
        """Return one canonical text event for hooks, routing, and command handling."""
        return await self.deps.normalizer.resolve_text_event(
            TextNormalizationRequest(event=event),
        )

    async def plan_router_dispatch(
        self,
        room: nio.MatrixRoom,
        event: DispatchEvent,
        dispatch: PreparedDispatch,
        *,
        message: str | None = None,
        extra_content: dict[str, Any] | None = None,
        media_events: list[MediaDispatchEvent] | None = None,
        handled_turn: HandledTurnState | None = None,
        router_event: DispatchEvent | None = None,
    ) -> DispatchPlan | None:
        """Return one router-specific dispatch plan when this bot is the router."""
        if self.deps.agent_name != ROUTER_AGENT_NAME:
            return None

        context = dispatch.context
        requester_user_id = dispatch.requester_user_id
        agents_in_thread = get_agents_in_thread(
            context.thread_history,
            self.deps.runtime.config,
            self.deps.runtime_paths,
        )
        sender_visible = filter_agents_by_sender_permissions(
            agents_in_thread,
            requester_user_id,
            self.deps.runtime.config,
            self.deps.runtime_paths,
        )

        if not context.mentioned_agents and not context.has_non_agent_mentions and not sender_visible:
            if context.is_thread and has_multiple_non_agent_users_in_thread(
                context.thread_history,
                self.deps.runtime.config,
                self.deps.runtime_paths,
            ):
                self.deps.logger.info("Skipping routing: multiple non-agent users in thread (mention required)")
                return self._router_ignore_plan(handled_turn, event.event_id)
            available_agents = get_available_agents_for_sender(
                room,
                requester_user_id,
                self.deps.runtime.config,
                self.deps.runtime_paths,
            )
            if len(available_agents) == 1:
                self.deps.logger.info("Skipping routing: only one agent present")
                return self._router_ignore_plan(handled_turn, event.event_id)
            return DispatchPlan(
                kind="route",
                router_message=message,
                extra_content=extra_content,
                media_events=media_events,
                handled_turn=handled_turn,
                router_event=router_event or event,
            )

        return self._router_ignore_plan(handled_turn, event.event_id)

    def _router_ignore_plan(
        self,
        handled_turn: HandledTurnState | None,
        event_id: str,
    ) -> DispatchPlan:
        tracked_handled_turn = handled_turn or HandledTurnState.from_source_event_id(event_id)
        handled_turn_outcome: HandledTurnState | None = None
        if tracked_handled_turn is not None:
            visible_router_echo_event_id = (
                tracked_handled_turn.visible_echo_event_id
                or self.deps.handled_turn_ledger.visible_echo_event_id_for_sources(
                    tracked_handled_turn.source_event_ids,
                )
            )
            if visible_router_echo_event_id is not None and any(
                not self.deps.handled_turn_ledger.has_responded(source_event_id)
                for source_event_id in tracked_handled_turn.source_event_ids
            ):
                handled_turn_outcome = tracked_handled_turn.with_response_event_id(visible_router_echo_event_id)
        return DispatchPlan(kind="ignore", handled_turn_outcome=handled_turn_outcome)

    @timed("dispatch_action_resolution")
    async def plan_dispatch(
        self,
        room: nio.MatrixRoom,
        event: TextDispatchEvent,
        dispatch: PreparedDispatch,
        *,
        extra_content: dict[str, Any] | None = None,
        media_events: list[MediaDispatchEvent] | None = None,
        handled_turn: HandledTurnState | None = None,
        router_event: DispatchEvent | None = None,
    ) -> DispatchPlan:
        """Return the explicit plan for one prepared text dispatch."""
        tracked_handled_turn = handled_turn or HandledTurnState.from_source_event_id(event.event_id)
        router_plan = await self.plan_router_dispatch(
            room,
            event,
            dispatch,
            message=event.body if media_events else None,
            extra_content=extra_content,
            media_events=media_events,
            handled_turn=(
                tracked_handled_turn
                if tracked_handled_turn.is_coalesced
                or (
                    tracked_handled_turn.source_event_ids and tracked_handled_turn.source_event_ids[0] != event.event_id
                )
                else None
            ),
            router_event=router_event
            or (media_events[0] if media_events and len(tracked_handled_turn.source_event_ids) == 1 else event),
        )
        if router_plan is not None:
            return router_plan

        action = await self.resolve_response_action(
            dispatch.context,
            room,
            dispatch.requester_user_id,
            event.body,
            await is_dm_room(self._client(), room.room_id),
            target=dispatch.target,
            source_envelope=dispatch.envelope,
        )
        if action.kind == "skip":
            return DispatchPlan(kind="ignore")
        return DispatchPlan(kind="respond", response_action=action)

    async def resolve_response_action(
        self,
        context: MessageContext,
        room: nio.MatrixRoom,
        requester_user_id: str,
        message: str,
        is_dm: bool,
        *,
        target: MessageTarget | None = None,
        source_envelope: MessageEnvelope | None = None,
    ) -> ResponseAction:
        """Decide whether to respond as a team, individually, or not at all."""
        agents_in_thread = get_agents_in_thread(
            context.thread_history,
            self.deps.runtime.config,
            self.deps.runtime_paths,
        )
        available_agents_in_room = get_available_agents_for_sender(
            room,
            requester_user_id,
            self.deps.runtime.config,
            self.deps.runtime_paths,
        )
        materializable_agent_names = self.materializable_agent_names()
        responder_pool = self.filter_materializable_agents(
            available_agents_in_room,
            materializable_agent_names,
        )
        form_team = await self.decide_team_for_sender(
            agents_in_thread,
            context,
            room,
            requester_user_id,
            message,
            is_dm,
            available_agents_in_room=available_agents_in_room,
            materializable_agent_names=materializable_agent_names,
        )
        team_action = self.team_response_action(form_team, responder_pool)
        if team_action is not None:
            return team_action

        if not should_agent_respond(
            agent_name=self.deps.agent_name,
            am_i_mentioned=context.am_i_mentioned,
            is_thread=context.is_thread,
            room=room,
            thread_history=context.thread_history,
            config=self.deps.runtime.config,
            runtime_paths=self.deps.runtime_paths,
            mentioned_agents=context.mentioned_agents,
            has_non_agent_mentions=context.has_non_agent_mentions,
            sender_id=requester_user_id,
        ):
            if self._should_queue_follow_up_in_active_response_thread(
                context=context,
                target=target,
                source_envelope=source_envelope,
            ):
                return ResponseAction(kind="individual")
            return ResponseAction(kind="skip")

        return ResponseAction(kind="individual")

    def _should_queue_follow_up_in_active_response_thread(
        self,
        *,
        context: MessageContext,
        target: MessageTarget | None,
        source_envelope: MessageEnvelope | None,
    ) -> bool:
        """Return whether one human follow-up should enter the queued-response path."""
        if target is None or source_envelope is None or not context.is_thread:
            return False
        if context.mentioned_agents or context.has_non_agent_mentions:
            return False
        if is_automation_source_kind(source_envelope.source_kind):
            return False
        if is_agent_id(source_envelope.sender_id, self.deps.runtime.config, self.deps.runtime_paths):
            return False
        return self.deps.response_coordinator.has_active_response_for_target(target)

    async def execute_command(
        self,
        room: nio.MatrixRoom,
        event: TextDispatchEvent,
        requester_user_id: str,
        command: Command,
        *,
        source_envelope: MessageEnvelope | None = None,
    ) -> None:
        """Run the explicit command executor path."""
        event = await self.resolve_text_dispatch_event(event)

        async def send_skill_command_response(
            *,
            room_id: str,
            reply_to_event_id: str,
            thread_id: str | None,
            thread_history: Sequence[ResolvedVisibleMessage],
            prompt: str,
            agent_name: str,
            user_id: str | None,
            reply_to_event: nio.RoomMessageText | None = None,
        ) -> str | None:
            return await self.deps.response_coordinator.send_skill_command_response(
                room_id=room_id,
                reply_to_event_id=reply_to_event_id,
                thread_id=thread_id,
                thread_history=thread_history,
                prompt=prompt,
                agent_name=agent_name,
                user_id=user_id,
                reply_to_event=reply_to_event,
                source_envelope=source_envelope,
            )

        async def send_response(
            room_id: str,
            reply_to_event_id: str | None,
            response_text: str,
            thread_id: str | None,
            reply_to_event: nio.RoomMessageText | None = None,
            skip_mentions: bool = False,
        ) -> str | None:
            return await self.deps.delivery_gateway.send_text(
                SendTextRequest(
                    room_id=room_id,
                    reply_to_event_id=reply_to_event_id,
                    response_text=response_text,
                    thread_id=thread_id,
                    reply_to_event=reply_to_event,
                    skip_mentions=skip_mentions,
                ),
            )

        async def run_skill_command_tool(
            *,
            agent_name: str,
            command_tool: str,
            skill_name: str,
            args_text: str,
            requester_user_id: str | None = None,
            room_id: str | None = None,
            thread_id: str | None = None,
        ) -> str:
            runtime_context = None
            if room_id is not None:
                runtime_context = self.deps.tool_runtime.build_context(
                    MessageTarget.resolve(room_id, thread_id, event.event_id),
                    user_id=requester_user_id,
                    agent_name=agent_name,
                    source_envelope=source_envelope,
                )
            return await _run_skill_command_tool(
                config=self.deps.runtime.config,
                runtime_paths=self.deps.runtime_paths,
                agent_name=agent_name,
                storage_path=self.deps.storage_path,
                command_tool=command_tool,
                skill_name=skill_name,
                args_text=args_text,
                requester_user_id=requester_user_id,
                room_id=room_id,
                thread_id=thread_id,
                runtime_context=runtime_context,
            )

        context = CommandHandlerContext(
            client=self._client(),
            config=self.deps.runtime.config,
            runtime_paths=self.deps.runtime_paths,
            storage_path=self.deps.storage_path,
            logger=self.deps.logger,
            handled_turn_ledger=self.deps.handled_turn_ledger,
            derive_conversation_context=self.deps.resolver.derive_conversation_context,
            requester_user_id_for_event=self._requester_user_id_for_event,
            build_message_target=self.deps.resolver.build_message_target,
            send_response=send_response,
            send_skill_command_response=send_skill_command_response,
            run_skill_command_tool=run_skill_command_tool,
        )
        await handle_command(
            context=context,
            room=room,
            event=event,
            command=command,
            requester_user_id=requester_user_id,
        )

    async def execute_router_relay(
        self,
        room: nio.MatrixRoom,
        event: DispatchEvent,
        thread_history: Sequence[ResolvedVisibleMessage],
        thread_id: str | None = None,
        message: str | None = None,
        *,
        requester_user_id: str,
        extra_content: dict[str, Any] | None = None,
        media_events: list[MediaDispatchEvent] | None = None,
        handled_turn: HandledTurnState | None = None,
    ) -> None:
        """Run the explicit router relay executor path."""
        assert self.deps.agent_name == ROUTER_AGENT_NAME

        permission_sender_id = requester_user_id
        available_agents = get_configured_agents_for_room(
            room.room_id,
            self.deps.runtime.config,
            self.deps.runtime_paths,
        )
        available_agents = filter_agents_by_sender_permissions(
            available_agents,
            permission_sender_id,
            self.deps.runtime.config,
            self.deps.runtime_paths,
        )
        if not available_agents:
            self.deps.logger.debug(
                "No configured agents to route to in this room for sender",
                sender=permission_sender_id,
            )
            return

        self.deps.logger.info("Handling AI routing", event_id=event.event_id)

        routing_text = message or event.body
        suggested_agent = await suggest_agent_for_message(
            routing_text,
            available_agents,
            self.deps.runtime.config,
            self.deps.runtime_paths,
            thread_history,
        )

        if not suggested_agent:
            response_text = (
                "⚠️ I couldn't determine which agent should help with this. "
                "Please try mentioning an agent directly with @ or rephrase your request."
            )
            self.deps.logger.warning("Router failed to determine agent")
        else:
            response_text = f"@{suggested_agent} could you help with this?"

        target_thread_mode = (
            self.deps.runtime.config.get_entity_thread_mode(
                suggested_agent,
                self.deps.runtime_paths,
                room_id=room.room_id,
            )
            if suggested_agent
            else None
        )
        resolved_target = self.deps.resolver.build_message_target(
            room_id=room.room_id,
            thread_id=thread_id,
            reply_to_event_id=event.event_id,
            event_source=event.source,
            thread_mode_override=target_thread_mode,
        )
        thread_event_id = resolved_target.resolved_thread_id
        routed_extra_content = dict(extra_content) if extra_content is not None else {}
        routed_media_events = list(media_events or [])
        if not routed_media_events and isinstance(
            event,
            nio.RoomMessageFile
            | nio.RoomEncryptedFile
            | nio.RoomMessageVideo
            | nio.RoomEncryptedVideo
            | nio.RoomMessageImage
            | nio.RoomEncryptedImage,
        ):
            routed_media_events.append(event)
        if routed_media_events:
            routed_attachment_ids = merge_attachment_ids(
                parse_attachment_ids_from_event_source({"content": routed_extra_content}),
                [
                    attachment_id
                    for attachment_id in await asyncio.gather(
                        *(
                            self.deps.normalizer.register_routed_attachment(
                                room_id=room.room_id,
                                thread_id=thread_event_id,
                                event=media_event,
                            )
                            for media_event in routed_media_events
                        ),
                    )
                    if attachment_id is not None
                ],
            )
            if routed_attachment_ids:
                routed_extra_content[ATTACHMENT_IDS_KEY] = routed_attachment_ids
            else:
                routed_extra_content.pop(ATTACHMENT_IDS_KEY, None)

        event_id = await self.deps.delivery_gateway.send_text(
            SendTextRequest(
                room_id=room.room_id,
                reply_to_event_id=event.event_id,
                response_text=response_text,
                thread_id=resolved_target.thread_id,
                target=resolved_target,
                extra_content=routed_extra_content or None,
            ),
        )
        tracked_handled_turn = (
            handled_turn or HandledTurnState.from_source_event_id(event.event_id)
        ).with_response_context(
            response_owner=self.deps.agent_name,
            history_scope=None,
            conversation_target=resolved_target,
        )
        if event_id:
            self.deps.logger.info("Routed to agent", suggested_agent=suggested_agent)
            self._mark_source_events_responded(
                tracked_handled_turn.with_response_event_id(
                    event_id,
                ),
            )
        else:
            self.deps.logger.error("Failed to route to agent", agent=suggested_agent)

    def _effective_response_action(self, action: ResponseAction) -> ResponseAction:
        """Apply configured-team execution behavior before running one response action."""
        if action.kind == "team":
            return action
        configured_team_action = self.configured_team_response_action()
        return configured_team_action or action

    async def execute_response_action(  # noqa: C901
        self,
        room: nio.MatrixRoom,
        event: DispatchEvent,
        dispatch: PreparedDispatch,
        action: ResponseAction,
        payload_builder: DispatchPayloadBuilder,
        *,
        processing_log: str,
        dispatch_started_at: float,
        handled_turn: HandledTurnState,
        matrix_run_metadata: dict[str, Any] | None = None,
    ) -> None:
        """Execute the final response path for a prepared dispatch action."""
        action = self._effective_response_action(action)

        if action.kind == "reject":
            assert action.rejection_message is not None
            response_event_id = await self.deps.delivery_gateway.send_text(
                SendTextRequest(
                    room_id=room.room_id,
                    reply_to_event_id=event.event_id,
                    response_text=action.rejection_message,
                    thread_id=dispatch.context.thread_id,
                ),
            )
            self._mark_source_events_responded(
                handled_turn.with_response_event_id(response_event_id),
            )
            return

        if not dispatch.context.am_i_mentioned:
            self.deps.logger.info("Will respond: only agent in thread")

        target_member_names: tuple[str, ...] | None = None
        if action.kind == "team":
            assert action.form_team is not None
            assert action.form_team.mode is not None
            target_member_names = tuple(
                member.agent_name(self.deps.runtime.config, self.deps.runtime_paths) or member.username
                for member in action.form_team.eligible_members
            )

        try:
            if dispatch.context.requires_full_thread_history:
                await self.deps.resolver.hydrate_dispatch_context(room, event, dispatch.context)
            context_ready_monotonic = time.monotonic()
            payload = await payload_builder(dispatch.context)
            prepared_payload = await self.deps.hook_service.apply_message_enrichment(
                dispatch,
                payload,
                target_entity_name=self.deps.agent_name,
                target_member_names=target_member_names,
            )
            system_enrichment_items = await self.deps.hook_service.apply_system_enrichment(
                dispatch,
                prepared_payload.envelope,
                target_entity_name=self.deps.agent_name,
                target_member_names=target_member_names,
            )
            if system_enrichment_items:
                prepared_payload = _ResolvedPreparedHookedPayload(
                    payload=prepared_payload.payload,
                    envelope=prepared_payload.envelope,
                    strip_transient_enrichment_after_run=prepared_payload.strip_transient_enrichment_after_run,
                    system_enrichment_items=tuple(system_enrichment_items),
                )
            payload_ready_monotonic = time.monotonic()
        except Exception as error:
            response_event_id = await self.finalize_dispatch_failure(
                room_id=room.room_id,
                reply_to_event_id=event.event_id,
                thread_id=dispatch.context.thread_id,
                error=error,
            )
            if response_event_id is not None:
                self._mark_source_events_responded(
                    handled_turn.with_response_event_id(response_event_id),
                )
            return

        self.log_dispatch_latency(
            event_id=event.event_id,
            action_kind=action.kind,
            dispatch_started_at=dispatch_started_at,
            context_ready_monotonic=context_ready_monotonic,
            payload_ready_monotonic=payload_ready_monotonic,
        )

        self.deps.logger.info(processing_log, event_id=event.event_id)
        received_monotonic = _received_monotonic_from_source(event.source)
        try:
            if action.kind == "team":
                assert action.form_team is not None
                assert action.form_team.mode is not None
                response_event_id = await self.deps.response_coordinator.generate_team_response_helper(
                    ResponseRequest(
                        room_id=room.room_id,
                        reply_to_event_id=event.event_id,
                        thread_id=dispatch.context.thread_id,
                        thread_history=dispatch.context.thread_history,
                        prompt=prepared_payload.payload.prompt,
                        model_prompt=prepared_payload.payload.model_prompt,
                        user_id=dispatch.requester_user_id,
                        media=prepared_payload.payload.media,
                        attachment_ids=tuple(prepared_payload.payload.attachment_ids or ()),
                        response_envelope=prepared_payload.envelope,
                        correlation_id=dispatch.correlation_id,
                        target=dispatch.target,
                        matrix_run_metadata=matrix_run_metadata,
                        system_enrichment_items=prepared_payload.system_enrichment_items,
                        strip_transient_enrichment_after_run=prepared_payload.strip_transient_enrichment_after_run,
                        received_monotonic=received_monotonic,
                    ),
                    team_agents=action.form_team.eligible_members,
                    team_mode=action.form_team.mode.value,
                )
            else:
                response_event_id = await self.deps.response_coordinator.generate_response(
                    ResponseRequest(
                        room_id=room.room_id,
                        reply_to_event_id=event.event_id,
                        thread_id=dispatch.context.thread_id,
                        thread_history=dispatch.context.thread_history,
                        prompt=prepared_payload.payload.prompt,
                        model_prompt=prepared_payload.payload.model_prompt,
                        user_id=dispatch.requester_user_id,
                        media=prepared_payload.payload.media,
                        attachment_ids=tuple(prepared_payload.payload.attachment_ids or ()),
                        response_envelope=prepared_payload.envelope,
                        correlation_id=dispatch.correlation_id,
                        target=dispatch.target,
                        matrix_run_metadata=matrix_run_metadata,
                        system_enrichment_items=prepared_payload.system_enrichment_items,
                        strip_transient_enrichment_after_run=prepared_payload.strip_transient_enrichment_after_run,
                        received_monotonic=received_monotonic,
                    ),
                )
        except SuppressedPlaceholderCleanupError:
            self.deps.logger.warning(
                "Suppressed response cleanup failed",
                source_event_id=event.event_id,
                correlation_id=dispatch.correlation_id,
            )
            raise
        if response_event_id is not None:
            self._mark_source_events_responded(
                handled_turn.with_response_event_id(response_event_id),
            )

    async def finalize_dispatch_failure(
        self,
        *,
        room_id: str,
        reply_to_event_id: str,
        thread_id: str | None,
        error: Exception,
    ) -> str | None:
        """Convert dispatch setup failures into a visible terminal message."""
        error_text = get_user_friendly_error_message(error, self.deps.agent_name)
        terminal_extra_content = {STREAM_STATUS_KEY: STREAM_STATUS_COMPLETED}
        return await self.deps.delivery_gateway.send_text(
            SendTextRequest(
                room_id=room_id,
                reply_to_event_id=reply_to_event_id,
                response_text=error_text,
                thread_id=thread_id,
                extra_content=terminal_extra_content,
            ),
        )

    def log_dispatch_latency(
        self,
        *,
        event_id: str,
        action_kind: str,
        dispatch_started_at: float,
        context_ready_monotonic: float,
        payload_ready_monotonic: float,
    ) -> None:
        """Emit startup latency metrics for dispatch decisions that will respond."""
        self.deps.logger.info(
            "Response startup latency",
            event_id=event_id,
            action_kind=action_kind,
            context_hydration_ms=round((context_ready_monotonic - dispatch_started_at) * 1000, 1),
            payload_hydration_ms=round((payload_ready_monotonic - context_ready_monotonic) * 1000, 1),
            startup_total_ms=round((payload_ready_monotonic - dispatch_started_at) * 1000, 1),
        )
