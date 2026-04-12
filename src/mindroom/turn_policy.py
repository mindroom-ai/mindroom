"""Pure turn policy and ingress hook enrichment for inbound turns."""

from __future__ import annotations

from dataclasses import dataclass
from typing import TYPE_CHECKING, Any, Literal

from mindroom.authorization import (
    filter_agents_by_sender_permissions,
    get_available_agents_for_sender,
    is_sender_allowed_for_agent_reply,
)
from mindroom.constants import ROUTER_AGENT_NAME, RuntimePaths
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
from mindroom.hooks.ingress import HookIngressPolicy, is_automation_source_kind
from mindroom.inbound_turn_normalizer import DispatchPayload
from mindroom.matrix.identity import MatrixID, is_agent_id
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
    has_multiple_non_agent_users_in_thread,
    should_agent_respond,
)
from mindroom.timing import timed

if TYPE_CHECKING:
    from collections.abc import Callable

    import nio
    import structlog

    from mindroom.bot_runtime_view import BotRuntimeView
    from mindroom.conversation_resolver import (
        DispatchEvent,
        MediaDispatchEvent,
        MessageContext,
        TextDispatchEvent,
    )
    from mindroom.message_target import MessageTarget


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
    """Pure policy output for one normalized inbound turn."""

    kind: Literal["ignore", "route", "respond"]
    response_action: ResponseAction | None = None
    router_message: str | None = None
    extra_content: dict[str, Any] | None = None
    media_events: list[MediaDispatchEvent] | None = None
    router_event: DispatchEvent | None = None
    ignore_reason: Literal["router"] | None = None


@dataclass(frozen=True)
class PreparedHookedPayload:
    """Concrete payload returned after ingress enrichment hooks run."""

    payload: DispatchPayload
    envelope: MessageEnvelope
    strip_transient_enrichment_after_run: bool
    system_enrichment_items: tuple[EnrichmentItem, ...]


@dataclass
class IngressHookRunner:
    """Own ingress hook emission and message or system enrichment updates."""

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
        model_prompt = payload.model_prompt
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
                base_model_prompt = payload.model_prompt if payload.model_prompt is not None else payload.prompt
                model_prompt = f"{base_model_prompt.rstrip()}\n\n{enrichment_block}"
                strip_transient_enrichment_after_run = True

        return PreparedHookedPayload(
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


@dataclass(frozen=True)
class TurnPolicyDeps:
    """Explicit collaborators needed by pure turn policy decisions."""

    runtime: BotRuntimeView
    logger: structlog.stdlib.BoundLogger
    runtime_paths: RuntimePaths
    agent_name: str
    matrix_id: MatrixID


@dataclass(frozen=True)
class TurnPolicy:
    """Own pure decision logic for one prepared inbound turn."""

    deps: TurnPolicyDeps

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
        return min(response_owners, key=lambda value: value.full_id)

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
        if self.deps.matrix_id != response_owner:
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

    def effective_response_action(self, action: ResponseAction) -> ResponseAction:
        """Apply configured-team execution behavior before running one response action."""
        if action.kind == "team":
            return action
        configured_team_action = self.configured_team_response_action()
        return configured_team_action or action

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
            self.deps.matrix_id,
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

    async def plan_router_dispatch(
        self,
        room: nio.MatrixRoom,
        event: DispatchEvent,
        dispatch: PreparedDispatch,
        *,
        message: str | None = None,
        extra_content: dict[str, Any] | None = None,
        media_events: list[MediaDispatchEvent] | None = None,
        router_event: DispatchEvent | None = None,
    ) -> DispatchPlan | None:
        """Return one router-specific dispatch plan when this entity is the router."""
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
                return DispatchPlan(kind="ignore", ignore_reason="router")
            available_agents = get_available_agents_for_sender(
                room,
                requester_user_id,
                self.deps.runtime.config,
                self.deps.runtime_paths,
            )
            if len(available_agents) == 1:
                self.deps.logger.info("Skipping routing: only one agent present")
                return DispatchPlan(kind="ignore", ignore_reason="router")
            return DispatchPlan(
                kind="route",
                router_message=message,
                extra_content=extra_content,
                media_events=media_events,
                router_event=router_event or event,
            )

        return DispatchPlan(kind="ignore", ignore_reason="router")

    @timed("dispatch_action_resolution")
    async def plan_turn(
        self,
        room: nio.MatrixRoom,
        event: TextDispatchEvent,
        dispatch: PreparedDispatch,
        *,
        is_dm: bool,
        has_active_response_for_target: Callable[[MessageTarget], bool],
        extra_content: dict[str, Any] | None = None,
        media_events: list[MediaDispatchEvent] | None = None,
        router_event: DispatchEvent | None = None,
    ) -> DispatchPlan:
        """Return the explicit policy plan for one prepared inbound turn."""
        router_plan = await self.plan_router_dispatch(
            room,
            event,
            dispatch,
            message=event.body if media_events else None,
            extra_content=extra_content,
            media_events=media_events,
            router_event=router_event,
        )
        if router_plan is not None:
            return router_plan

        action = await self.resolve_response_action(
            dispatch.context,
            room,
            dispatch.requester_user_id,
            event.body,
            is_dm,
            target=dispatch.target,
            source_envelope=dispatch.envelope,
            has_active_response_for_target=has_active_response_for_target,
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
        has_active_response_for_target: Callable[[MessageTarget], bool] | None = None,
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
                has_active_response_for_target=has_active_response_for_target,
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
        has_active_response_for_target: Callable[[MessageTarget], bool] | None,
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
        if has_active_response_for_target is None:
            return False
        return has_active_response_for_target(target)
