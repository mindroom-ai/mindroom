"""Dispatch planning and explicit executor paths extracted from ``bot.py``."""

from __future__ import annotations

import asyncio
import time
from collections.abc import Awaitable, Callable, Sequence
from dataclasses import dataclass, replace
from typing import TYPE_CHECKING, Any, Literal, Protocol, cast

import nio

from mindroom.attachments import merge_attachment_ids, parse_attachment_ids_from_event_source
from mindroom.coalescing import PreparedTextEvent
from mindroom.commands.handler import CommandEvent, CommandHandlerContext
from mindroom.constants import (
    ATTACHMENT_IDS_KEY,
    ROUTER_AGENT_NAME,
    STREAM_STATUS_COMPLETED,
    STREAM_STATUS_KEY,
    STREAM_STATUS_PENDING,
    RuntimePaths,
)
from mindroom.conversation_resolver import (
    DispatchEvent,
    MediaDispatchEvent,
    MessageContext,
    TextDispatchEvent,
)
from mindroom.delivery_gateway import (
    SuppressedPlaceholderCleanupError as _SuppressedPlaceholderCleanupError,
)
from mindroom.error_handling import get_user_friendly_error_message
from mindroom.handled_turns import HandledTurnLedger, HandledTurnState
from mindroom.hooks import EnrichmentItem, MessageEnvelope
from mindroom.hooks.ingress import hook_ingress_policy, is_automation_source_kind
from mindroom.inbound_turn_normalizer import DispatchPayload, TextNormalizationRequest
from mindroom.matrix.event_info import EventInfo
from mindroom.matrix.identity import MatrixID, extract_agent_name, is_agent_id
from mindroom.message_target import MessageTarget
from mindroom.teams import TeamIntent, TeamOutcome, TeamResolution
from mindroom.timing import timed

if TYPE_CHECKING:
    from pathlib import Path

    import structlog

    from mindroom.commands.parsing import Command
    from mindroom.config.main import Config
    from mindroom.matrix.client import ResolvedVisibleMessage
    from mindroom.orchestrator import MultiAgentOrchestrator
    from mindroom.tool_system.runtime_context import ToolRuntimeContext

type DispatchPayloadBuilder = Callable[[MessageContext], Awaitable[DispatchPayload]]
type ClientGetter = Callable[[], nio.AsyncClient | None]
type ConfigGetter = Callable[[], Config]
type MatrixIDGetter = Callable[[], MatrixID]
type LoggerGetter = Callable[[], structlog.stdlib.BoundLogger]
type OrchestratorGetter = Callable[[], MultiAgentOrchestrator | None]
type HandledTurnLedgerGetter = Callable[[], HandledTurnLedger]
type ResolveTextDispatchEventFn = Callable[[TextNormalizationRequest], Awaitable[PreparedTextEvent]]
type ExtractDispatchContextFn = Callable[[nio.MatrixRoom, DispatchEvent], Awaitable[MessageContext]]
type HydrateDispatchContextFn = Callable[[nio.MatrixRoom, DispatchEvent, MessageContext], Awaitable[None]]
type BuildMessageTargetFn = Callable[..., MessageTarget]
type BuildMessageEnvelopeFn = Callable[..., MessageEnvelope]
type EmitMessageReceivedHooksFn = Callable[..., Awaitable[bool]]
type MarkSourceEventsRespondedFn = Callable[[HandledTurnState], None]
type DeriveConversationContextFn = Callable[
    [str, EventInfo],
    Awaitable[tuple[bool, str | None, Sequence[ResolvedVisibleMessage]]],
]
type RequesterUserIdForEventFn = Callable[[CommandEvent], str]
type HandleCommandFn = Callable[..., Awaitable[None]]
type RunSkillCommandToolFn = Callable[..., Awaitable[str]]
type SendResponseFn = Callable[..., Awaitable[str | None]]
type SendSkillCommandResponseFn = Callable[..., Awaitable[str | None]]
type BuildToolRuntimeContextFn = Callable[..., ToolRuntimeContext | None]
type ThreadAgentsLookupFn = Callable[[Sequence[ResolvedVisibleMessage], Config, RuntimePaths], list[MatrixID]]
type ThreadUserCountLookupFn = Callable[[Sequence[ResolvedVisibleMessage], Config, RuntimePaths], bool]
type RoomAgentsLookupFn = Callable[[nio.MatrixRoom, str, Config, RuntimePaths], list[MatrixID]]
type FilterAgentsBySenderPermissionsFn = Callable[[list[MatrixID], str, Config, RuntimePaths], list[MatrixID]]
type DecideTeamFormationFn = Callable[..., Awaitable[TeamResolution]]
type DecideTeamForSenderFn = Callable[..., Awaitable[TeamResolution]]
type ShouldAgentRespondFn = Callable[..., bool]
type SuggestAgentForMessageFn = Callable[
    [str, list[MatrixID], Config, RuntimePaths, Sequence[ResolvedVisibleMessage]],
    Awaitable[str | None],
]
type RegisterRoutedAttachmentFn = Callable[..., Awaitable[str | None]]
type EditMessageFn = Callable[..., Awaitable[bool]]
type GenerateResponseFn = Callable[..., Awaitable[str | None]]
type ActiveResponseForTargetFn = Callable[[MessageTarget], bool]


class PreparedHookedPayload(Protocol):
    """Typed payload surface returned after message enrichment hooks run."""

    payload: DispatchPayload
    envelope: MessageEnvelope
    strip_transient_enrichment_after_run: bool
    system_enrichment_items: tuple[EnrichmentItem, ...]


type GenerateTeamResponseHelperFn = Callable[..., Awaitable[str | None]]
type ApplyMessageEnrichmentFn = Callable[..., Awaitable[PreparedHookedPayload]]
type ApplySystemEnrichmentFn = Callable[..., Awaitable[list[EnrichmentItem]]]
type BuildDispatchPayloadFn = Callable[..., Awaitable[DispatchPayload]]
type ReceivedMonotonicFromSourceFn = Callable[[dict[str, Any] | None], float | None]
type FinalizeDispatchFailureFn = Callable[..., Awaitable[str | None]]
type LogDispatchLatencyFn = Callable[..., None]
type IsDmRoomFn = Callable[[nio.AsyncClient, str], Awaitable[bool]]
type SenderAllowedForReplyFn = Callable[[str, str, Config, RuntimePaths], bool]
type ResolveLiveSharedAgentNamesFn = Callable[[MultiAgentOrchestrator, Config], set[str]]
type GetConfiguredAgentsForRoomFn = Callable[[str, Config, RuntimePaths], list[MatrixID]]


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

    client_getter: ClientGetter
    config_getter: ConfigGetter
    runtime_paths: RuntimePaths
    storage_path: Path
    agent_name: str
    matrix_id_getter: MatrixIDGetter
    logger_getter: LoggerGetter
    orchestrator_getter: OrchestratorGetter
    handled_turn_ledger_getter: HandledTurnLedgerGetter
    resolve_text_dispatch_event: ResolveTextDispatchEventFn
    extract_dispatch_context: ExtractDispatchContextFn
    hydrate_dispatch_context: HydrateDispatchContextFn
    build_message_target: BuildMessageTargetFn
    build_message_envelope: BuildMessageEnvelopeFn
    emit_message_received_hooks: EmitMessageReceivedHooksFn
    mark_source_events_responded: MarkSourceEventsRespondedFn
    derive_conversation_context: DeriveConversationContextFn
    requester_user_id_for_event: RequesterUserIdForEventFn
    handle_command_fn: HandleCommandFn
    run_skill_command_tool_fn: RunSkillCommandToolFn
    send_response: SendResponseFn
    send_response_kw: SendResponseFn
    send_skill_command_response: SendSkillCommandResponseFn
    build_tool_runtime_context: BuildToolRuntimeContextFn
    get_agents_in_thread_fn: ThreadAgentsLookupFn
    get_all_mentioned_agents_in_thread_fn: ThreadAgentsLookupFn
    has_multiple_non_agent_users_in_thread_fn: ThreadUserCountLookupFn
    get_available_agents_for_sender_fn: RoomAgentsLookupFn
    filter_agents_by_sender_permissions_fn: FilterAgentsBySenderPermissionsFn
    decide_team_formation_fn: DecideTeamFormationFn
    decide_team_for_sender_fn: DecideTeamForSenderFn
    should_agent_respond_fn: ShouldAgentRespondFn
    suggest_agent_for_message_fn: SuggestAgentForMessageFn
    register_routed_attachment: RegisterRoutedAttachmentFn
    edit_message: EditMessageFn
    generate_response: GenerateResponseFn
    has_active_response_for_target_fn: ActiveResponseForTargetFn
    generate_team_response_helper: GenerateTeamResponseHelperFn
    apply_message_enrichment: ApplyMessageEnrichmentFn
    apply_system_enrichment: ApplySystemEnrichmentFn
    build_dispatch_payload: BuildDispatchPayloadFn
    received_monotonic_from_source: ReceivedMonotonicFromSourceFn
    finalize_dispatch_failure_fn: FinalizeDispatchFailureFn
    log_dispatch_latency_fn: LogDispatchLatencyFn
    is_dm_room_fn: IsDmRoomFn
    is_sender_allowed_for_agent_reply_fn: SenderAllowedForReplyFn
    resolve_live_shared_agent_names_fn: ResolveLiveSharedAgentNamesFn
    get_configured_agents_for_room_fn: GetConfiguredAgentsForRoomFn


@dataclass(frozen=True)
class DispatchPlanner:
    """Own dispatch planning plus explicit command and router executor paths."""

    deps: DispatchPlannerDeps

    def _client(self) -> nio.AsyncClient:
        client = self.deps.client_getter()
        if client is None:
            msg = "Matrix client is not ready for dispatch planning"
            raise RuntimeError(msg)
        return client

    def _config(self) -> Config:
        return self.deps.config_getter()

    def _logger(self) -> structlog.stdlib.BoundLogger:
        return self.deps.logger_getter()

    def _matrix_id(self) -> MatrixID:
        return self.deps.matrix_id_getter()

    def _orchestrator(self) -> MultiAgentOrchestrator | None:
        return self.deps.orchestrator_getter()

    def _handled_turn_ledger(self) -> HandledTurnLedger:
        return self.deps.handled_turn_ledger_getter()

    def can_reply_to_sender(self, sender_id: str) -> bool:
        """Return whether this entity may reply to ``sender_id``."""
        return self.deps.is_sender_allowed_for_agent_reply_fn(
            sender_id,
            self.deps.agent_name,
            self._config(),
            self.deps.runtime_paths,
        )

    def materializable_agent_names(self) -> set[str] | None:
        """Return live shared agent names that can currently answer."""
        orchestrator = self._orchestrator()
        if orchestrator is None:
            return None
        return self.deps.resolve_live_shared_agent_names_fn(orchestrator, self._config())

    def filter_materializable_agents(
        self,
        agent_ids: list[MatrixID],
        materializable_agent_names: set[str] | None,
    ) -> list[MatrixID]:
        """Keep only agents that can currently be materialized."""
        if materializable_agent_names is None:
            return agent_ids
        config = self._config()
        return [
            agent_id
            for agent_id in agent_ids
            if (agent_id.agent_name(config, self.deps.runtime_paths) or agent_id.username) in materializable_agent_names
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
        all_mentioned_in_thread = self.deps.get_all_mentioned_agents_in_thread_fn(
            context.thread_history,
            self._config(),
            self.deps.runtime_paths,
        )
        if available_agents_in_room is None:
            available_agents_in_room = self.deps.get_available_agents_for_sender_fn(
                room,
                requester_user_id,
                self._config(),
                self.deps.runtime_paths,
            )
        if materializable_agent_names is None:
            materializable_agent_names = self.materializable_agent_names()
        return await self.deps.decide_team_formation_fn(
            self._matrix_id(),
            context.mentioned_agents,
            agents_in_thread,
            all_mentioned_in_thread,
            room=room,
            message=message,
            config=self._config(),
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
        context = await self.deps.extract_dispatch_context(room, event)
        target = self.deps.build_message_target(
            room_id=room.room_id,
            thread_id=context.thread_id,
            reply_to_event_id=event.event_id,
            event_source=event.source,
        )
        correlation_id = event.event_id
        envelope = self.deps.build_message_envelope(
            room_id=room.room_id,
            event=event,
            requester_user_id=requester_user_id,
            context=context,
            target=target,
        )
        ingress_policy = hook_ingress_policy(envelope)
        suppressed = await self.deps.emit_message_received_hooks(
            envelope=envelope,
            correlation_id=correlation_id,
            policy=ingress_policy,
        )
        if suppressed:
            self.deps.mark_source_events_responded(handled_turn)
            return None

        sender_agent_name = extract_agent_name(requester_user_id, self._config(), self.deps.runtime_paths)
        if sender_agent_name and not context.am_i_mentioned and not ingress_policy.bypass_unmentioned_agent_gate:
            self._logger().debug(f"Ignoring {event_label} from other agent (not mentioned)")
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
        return await self.deps.resolve_text_dispatch_event(
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
        agents_in_thread = self.deps.get_agents_in_thread_fn(
            context.thread_history,
            self._config(),
            self.deps.runtime_paths,
        )
        sender_visible = self.deps.filter_agents_by_sender_permissions_fn(
            agents_in_thread,
            requester_user_id,
            self._config(),
            self.deps.runtime_paths,
        )

        if not context.mentioned_agents and not context.has_non_agent_mentions and not sender_visible:
            if context.is_thread and self.deps.has_multiple_non_agent_users_in_thread_fn(
                context.thread_history,
                self._config(),
                self.deps.runtime_paths,
            ):
                self._logger().info("Skipping routing: multiple non-agent users in thread (mention required)")
                return self._router_ignore_plan(handled_turn, event.event_id)
            available_agents = self.deps.get_available_agents_for_sender_fn(
                room,
                requester_user_id,
                self._config(),
                self.deps.runtime_paths,
            )
            if len(available_agents) == 1:
                self._logger().info("Skipping routing: only one agent present")
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
                or self._handled_turn_ledger().visible_echo_event_id_for_sources(tracked_handled_turn.source_event_ids)
            )
            if visible_router_echo_event_id is not None and any(
                not self._handled_turn_ledger().has_responded(source_event_id)
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
            await self.deps.is_dm_room_fn(self._client(), room.room_id),
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
        agents_in_thread = self.deps.get_agents_in_thread_fn(
            context.thread_history,
            self._config(),
            self.deps.runtime_paths,
        )
        available_agents_in_room = self.deps.get_available_agents_for_sender_fn(
            room,
            requester_user_id,
            self._config(),
            self.deps.runtime_paths,
        )
        materializable_agent_names = self.materializable_agent_names()
        responder_pool = self.filter_materializable_agents(
            available_agents_in_room,
            materializable_agent_names,
        )
        form_team = await self.deps.decide_team_for_sender_fn(
            agents_in_thread,
            context,
            room,
            requester_user_id,
            message,
            is_dm,
            available_agents_in_room,
            materializable_agent_names,
        )
        team_action = self.team_response_action(form_team, responder_pool)
        if team_action is not None:
            return team_action

        if not self.deps.should_agent_respond_fn(
            agent_name=self.deps.agent_name,
            am_i_mentioned=context.am_i_mentioned,
            is_thread=context.is_thread,
            room=room,
            thread_history=context.thread_history,
            config=self._config(),
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
        if is_agent_id(source_envelope.sender_id, self._config(), self.deps.runtime_paths):
            return False
        return self.deps.has_active_response_for_target_fn(target)

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
            return await self.deps.send_skill_command_response(
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
                runtime_context = self.deps.build_tool_runtime_context(
                    MessageTarget.resolve(room_id, thread_id, event.event_id),
                    user_id=requester_user_id,
                    agent_name=agent_name,
                    source_envelope=source_envelope,
                )
            return await self.deps.run_skill_command_tool_fn(
                config=self._config(),
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
            config=self._config(),
            runtime_paths=self.deps.runtime_paths,
            storage_path=self.deps.storage_path,
            logger=self._logger(),
            handled_turn_ledger=self._handled_turn_ledger(),
            derive_conversation_context=self.deps.derive_conversation_context,
            requester_user_id_for_event=self.deps.requester_user_id_for_event,
            build_message_target=self.deps.build_message_target,
            send_response=self.deps.send_response,
            send_skill_command_response=send_skill_command_response,
            run_skill_command_tool=run_skill_command_tool,
        )
        await self.deps.handle_command_fn(
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
        available_agents = self.deps.get_configured_agents_for_room_fn(
            room.room_id,
            self._config(),
            self.deps.runtime_paths,
        )
        available_agents = self.deps.filter_agents_by_sender_permissions_fn(
            available_agents,
            permission_sender_id,
            self._config(),
            self.deps.runtime_paths,
        )
        if not available_agents:
            self._logger().debug(
                "No configured agents to route to in this room for sender",
                sender=permission_sender_id,
            )
            return

        self._logger().info("Handling AI routing", event_id=event.event_id)

        routing_text = message or event.body
        suggested_agent = await self.deps.suggest_agent_for_message_fn(
            routing_text,
            available_agents,
            self._config(),
            self.deps.runtime_paths,
            thread_history,
        )

        if not suggested_agent:
            response_text = (
                "⚠️ I couldn't determine which agent should help with this. "
                "Please try mentioning an agent directly with @ or rephrase your request."
            )
            self._logger().warning("Router failed to determine agent")
        else:
            response_text = f"@{suggested_agent} could you help with this?"

        target_thread_mode = (
            self._config().get_entity_thread_mode(suggested_agent, self.deps.runtime_paths, room_id=room.room_id)
            if suggested_agent
            else None
        )
        resolved_target = self.deps.build_message_target(
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
                            self.deps.register_routed_attachment(
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

        event_id = await self.deps.send_response_kw(
            room_id=room.room_id,
            reply_to_event_id=event.event_id,
            response_text=response_text,
            thread_id=resolved_target.thread_id,
            target=resolved_target,
            extra_content=routed_extra_content or None,
        )
        if event_id:
            self._logger().info("Routed to agent", suggested_agent=suggested_agent)
            self.deps.mark_source_events_responded(
                (handled_turn or HandledTurnState.from_source_event_id(event.event_id)).with_response_event_id(
                    event_id,
                ),
            )
        else:
            self._logger().error("Failed to route to agent", agent=suggested_agent)

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
        if action.kind == "reject":
            assert action.rejection_message is not None
            response_event_id = await self.deps.send_response_kw(
                room_id=room.room_id,
                reply_to_event_id=event.event_id,
                response_text=action.rejection_message,
                thread_id=dispatch.context.thread_id,
            )
            self.deps.mark_source_events_responded(
                handled_turn.with_response_event_id(response_event_id),
            )
            return

        if not dispatch.context.am_i_mentioned:
            self._logger().info("Will respond: only agent in thread")

        placeholder_text = "Thinking..."
        target_member_names: tuple[str, ...] | None = None
        if action.kind == "team":
            placeholder_text = "🤝 Team Response: Thinking..."
            assert action.form_team is not None
            assert action.form_team.mode is not None
            target_member_names = tuple(
                member.agent_name(self._config(), self.deps.runtime_paths) or member.username
                for member in action.form_team.eligible_members
            )

        placeholder_event_id = await self.deps.send_response_kw(
            room_id=room.room_id,
            reply_to_event_id=event.event_id,
            response_text=placeholder_text,
            thread_id=dispatch.context.thread_id,
            extra_content={STREAM_STATUS_KEY: STREAM_STATUS_PENDING},
        )
        placeholder_ready_monotonic = time.monotonic()

        try:
            if dispatch.context.requires_full_thread_history:
                await self.deps.hydrate_dispatch_context(room, event, dispatch.context)
            context_ready_monotonic = time.monotonic()
            payload = await self.deps.build_dispatch_payload(payload_builder, dispatch.context)
            prepared_payload = await self.deps.apply_message_enrichment(
                dispatch,
                payload,
                target_entity_name=self.deps.agent_name,
                target_member_names=target_member_names,
            )
            system_enrichment_items = await self.deps.apply_system_enrichment(
                dispatch,
                prepared_payload.envelope,
                target_entity_name=self.deps.agent_name,
                target_member_names=target_member_names,
            )
            if system_enrichment_items:
                prepared_payload = cast(
                    "PreparedHookedPayload",
                    replace(
                        cast("Any", prepared_payload),
                        system_enrichment_items=tuple(system_enrichment_items),
                    ),
                )
            payload_ready_monotonic = time.monotonic()
        except Exception as error:
            response_event_id = await self.deps.finalize_dispatch_failure_fn(
                room_id=room.room_id,
                reply_to_event_id=event.event_id,
                thread_id=dispatch.context.thread_id,
                placeholder_event_id=placeholder_event_id,
                error=error,
            )
            if response_event_id is not None:
                self.deps.mark_source_events_responded(
                    handled_turn.with_response_event_id(response_event_id),
                )
            return

        self.deps.log_dispatch_latency_fn(
            event_id=event.event_id,
            action_kind=action.kind,
            placeholder_event_id=placeholder_event_id,
            dispatch_started_at=dispatch_started_at,
            placeholder_ready_monotonic=placeholder_ready_monotonic,
            context_ready_monotonic=context_ready_monotonic,
            payload_ready_monotonic=payload_ready_monotonic,
        )

        self._logger().info(processing_log, event_id=event.event_id)
        received_monotonic = self.deps.received_monotonic_from_source(event.source)
        try:
            if action.kind == "team":
                assert action.form_team is not None
                assert action.form_team.mode is not None
                response_event_id = await self.deps.generate_team_response_helper(
                    room_id=room.room_id,
                    reply_to_event_id=event.event_id,
                    thread_id=dispatch.context.thread_id,
                    target=dispatch.target,
                    payload=prepared_payload.payload,
                    team_agents=action.form_team.eligible_members,
                    team_mode=action.form_team.mode,
                    thread_history=dispatch.context.thread_history,
                    requester_user_id=dispatch.requester_user_id,
                    existing_event_id=placeholder_event_id,
                    existing_event_is_placeholder=placeholder_event_id is not None,
                    response_envelope=prepared_payload.envelope,
                    strip_transient_enrichment_after_run=prepared_payload.strip_transient_enrichment_after_run,
                    system_enrichment_items=prepared_payload.system_enrichment_items,
                    correlation_id=dispatch.correlation_id,
                    matrix_run_metadata=matrix_run_metadata,
                )
            else:
                response_event_id = await self.deps.generate_response(
                    room_id=room.room_id,
                    prompt=prepared_payload.payload.prompt,
                    reply_to_event_id=event.event_id,
                    thread_id=dispatch.context.thread_id,
                    target=dispatch.target,
                    thread_history=dispatch.context.thread_history,
                    user_id=dispatch.requester_user_id,
                    media=prepared_payload.payload.media,
                    attachment_ids=prepared_payload.payload.attachment_ids,
                    existing_event_id=placeholder_event_id,
                    existing_event_is_placeholder=placeholder_event_id is not None,
                    model_prompt=prepared_payload.payload.model_prompt,
                    strip_transient_enrichment_after_run=prepared_payload.strip_transient_enrichment_after_run,
                    system_enrichment_items=prepared_payload.system_enrichment_items,
                    response_envelope=prepared_payload.envelope,
                    correlation_id=dispatch.correlation_id,
                    matrix_run_metadata=matrix_run_metadata,
                    received_monotonic=received_monotonic,
                )
        except _SuppressedPlaceholderCleanupError:
            self._logger().warning(
                "Suppressed placeholder cleanup failed",
                source_event_id=event.event_id,
                placeholder_event_id=placeholder_event_id,
                correlation_id=dispatch.correlation_id,
            )
            return
        if response_event_id is not None:
            self.deps.mark_source_events_responded(
                handled_turn.with_response_event_id(response_event_id),
            )

    async def finalize_dispatch_failure(
        self,
        *,
        room_id: str,
        reply_to_event_id: str,
        thread_id: str | None,
        placeholder_event_id: str | None,
        error: Exception,
    ) -> str | None:
        """Convert post-placeholder setup failures into a visible terminal message."""
        error_text = get_user_friendly_error_message(error, self.deps.agent_name)
        terminal_extra_content = {STREAM_STATUS_KEY: STREAM_STATUS_COMPLETED}
        if placeholder_event_id is None:
            return await self.deps.send_response(
                room_id,
                reply_to_event_id,
                error_text,
                thread_id,
                extra_content=terminal_extra_content,
            )

        placeholder_updated = await self.deps.edit_message(
            room_id,
            placeholder_event_id,
            error_text,
            thread_id,
            extra_content=terminal_extra_content,
        )
        if placeholder_updated:
            return placeholder_event_id

        return await self.deps.send_response(
            room_id,
            reply_to_event_id,
            error_text,
            thread_id,
            extra_content=terminal_extra_content,
        )

    def log_dispatch_latency(
        self,
        *,
        event_id: str,
        action_kind: str,
        placeholder_event_id: str | None,
        dispatch_started_at: float,
        placeholder_ready_monotonic: float,
        context_ready_monotonic: float,
        payload_ready_monotonic: float,
    ) -> None:
        """Emit startup latency metrics for dispatch decisions that will respond."""
        self._logger().info(
            "Response startup latency",
            event_id=event_id,
            action_kind=action_kind,
            placeholder_event_id=placeholder_event_id,
            placeholder_visible_ms=round((placeholder_ready_monotonic - dispatch_started_at) * 1000, 1),
            context_hydration_ms=round((context_ready_monotonic - placeholder_ready_monotonic) * 1000, 1),
            payload_hydration_ms=round((payload_ready_monotonic - context_ready_monotonic) * 1000, 1),
            startup_total_ms=round((payload_ready_monotonic - dispatch_started_at) * 1000, 1),
        )
