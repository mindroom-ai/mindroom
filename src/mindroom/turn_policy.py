"""Pure turn policy and ingress hook enrichment for inbound turns."""

from __future__ import annotations

import time
from dataclasses import dataclass
from typing import TYPE_CHECKING, Any, Literal

from mindroom.authorization import is_sender_allowed_for_agent_reply, responder_candidate_entities_for_room
from mindroom.constants import ROUTER_AGENT_NAME, RuntimePaths
from mindroom.dispatch_source import ACTIVE_THREAD_FOLLOW_UP_SOURCE_KIND
from mindroom.entity_resolution import entity_identity_registry
from mindroom.hooks import (
    EVENT_MESSAGE_ENRICH,
    EVENT_MESSAGE_RECEIVED,
    EVENT_SYSTEM_ENRICH,
    EnrichmentItem,
    HookContextSupport,
    HookIngressPolicy,
    MessageEnrichContext,
    MessageEnvelope,
    MessageReceivedContext,
    SystemEnrichContext,
    emit,
    emit_collect,
    render_enrichment_block,
)
from mindroom.inbound_turn_normalizer import DispatchPayload
from mindroom.responder_availability import (
    filter_materializable_responders,
    live_responder_entity_names,
    materializable_agent_names_for_orchestrator,
)
from mindroom.runtime_protocols import SupportsClientConfigOrchestrator  # noqa: TC001
from mindroom.teams import (
    TeamIntent,
    TeamMode,
    TeamOutcome,
    TeamResolution,
    decide_team_formation,
    resolve_configured_team,
)
from mindroom.thread_utils import (
    AgentResponseDecision,
    decide_agent_response,
    get_agents_in_thread,
    get_all_mentioned_agents_in_thread,
    has_multiple_non_agent_users_in_thread,
    is_router_only_agent_mention,
    thread_requires_explicit_agent_targeting,
)
from mindroom.timing import emit_elapsed_timing, timed

if TYPE_CHECKING:
    from collections.abc import Callable

    import nio
    import structlog

    from mindroom.conversation_resolver import MessageContext
    from mindroom.dispatch_handoff import DispatchEvent, MediaDispatchEvent, TextDispatchEvent
    from mindroom.matrix.identity import MatrixID
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
    current_prompt_is_structured: bool = False

    def __post_init__(self) -> None:
        """Require the prepared envelope and dispatch target to describe the same delivery."""
        if self.envelope.target != self.target:
            msg = "Prepared dispatch envelope target must match the resolved dispatch target"
            raise ValueError(msg)


@dataclass(frozen=True)
class _DispatchPlan:
    """Pure policy output for one normalized inbound turn."""

    kind: Literal["ignore", "route", "respond"]
    response_action: ResponseAction | None = None
    router_message: str | None = None
    extra_content: dict[str, Any] | None = None
    media_events: list[MediaDispatchEvent] | None = None
    router_event: DispatchEvent | None = None
    ignore_reason: Literal["router"] | None = None


_ROUTER_ONLY_MENTION_GUIDANCE = (
    "🧭 Rules of engagement: mention a specific agent or team when you want that entity to answer, or mention "
    "multiple agents when you want an ad-hoc collaboration. If one human and one agent or team are already talking "
    "in a thread, you can keep going without an explicit tag. Once a thread has multiple human users or multiple "
    "agent/team participants, explicitly tag the agent, team, or agents you want next. In a new untagged message, "
    "automatic routing can still choose an agent or team when appropriate. The router is not a conversational AI "
    "agent you can tag directly."
)


@dataclass(frozen=True)
class _PreparedHookedPayload:
    """Concrete payload returned after ingress enrichment hooks run."""

    payload: DispatchPayload
    envelope: MessageEnvelope
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
    ) -> _PreparedHookedPayload:
        """Run message:enrich and return the model-facing payload."""
        started = time.monotonic()
        hook_registered = self.hook_context.registry.has_hooks(EVENT_MESSAGE_ENRICH)
        item_count = 0

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
            dispatch_policy_source_kind=dispatch.envelope.dispatch_policy_source_kind,
            origin=dispatch.envelope.origin,
        )
        model_prompt = payload.model_prompt
        if hook_registered:
            context = MessageEnrichContext(
                **self.hook_context.base_kwargs(EVENT_MESSAGE_ENRICH, dispatch.correlation_id),
                envelope=envelope,
                target_entity_name=target_entity_name,
                target_member_names=target_member_names,
            )
            items = await emit_collect(self.hook_context.registry, EVENT_MESSAGE_ENRICH, context)
            item_count = len(items)
            if items:
                enrichment_block = render_enrichment_block(items)
                base_model_prompt = payload.model_prompt if payload.model_prompt is not None else payload.prompt
                model_prompt = f"{base_model_prompt.rstrip()}\n\n{enrichment_block}"

        emit_elapsed_timing(
            "response_payload.apply_message_enrichment",
            started,
            room_id=dispatch.envelope.room_id,
            target_entity_name=target_entity_name,
            hook_registered=hook_registered,
            enrichment_item_count=item_count,
        )
        return _PreparedHookedPayload(
            payload=DispatchPayload(
                prompt=payload.prompt,
                model_prompt=model_prompt,
                media=payload.media,
                attachment_ids=payload.attachment_ids,
            ),
            envelope=envelope,
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
        started = time.monotonic()
        hook_registered = self.hook_context.registry.has_hooks(EVENT_SYSTEM_ENRICH)

        def finish(items: list[EnrichmentItem]) -> list[EnrichmentItem]:
            emit_elapsed_timing(
                "response_payload.apply_system_enrichment",
                started,
                room_id=dispatch.envelope.room_id,
                target_entity_name=target_entity_name,
                hook_registered=hook_registered,
                enrichment_item_count=len(items),
            )
            return items

        if not hook_registered:
            return finish([])
        context = SystemEnrichContext(
            **self.hook_context.base_kwargs(EVENT_SYSTEM_ENRICH, dispatch.correlation_id),
            envelope=envelope,
            target_entity_name=target_entity_name,
            target_member_names=target_member_names,
        )
        return finish(await emit_collect(self.hook_context.registry, EVENT_SYSTEM_ENRICH, context))


@dataclass(frozen=True)
class TurnPolicyDeps:
    """Explicit collaborators needed by pure turn policy decisions."""

    runtime: SupportsClientConfigOrchestrator
    logger: structlog.stdlib.BoundLogger
    runtime_paths: RuntimePaths
    agent_name: str
    matrix_id: MatrixID


@dataclass(frozen=True)
class _ResponderAvailability:
    """Point-in-time responder availability threaded through one decision flow.

    ``None`` values mean live runtime state is unknown, so availability
    filtering must not narrow responder candidates.
    """

    materializable_agent_names: set[str] | None
    live_entity_names: set[str] | None


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

    def responder_availability(self) -> _ResponderAvailability:
        """Snapshot in-memory responder liveness for one decision flow.

        Each decision flow takes a fresh snapshot on entry and passes it down
        instead of recomputing; snapshots are deliberately not cached on this
        long-lived policy object because liveness changes between turns.
        """
        materializable_agent_names = materializable_agent_names_for_orchestrator(
            self.deps.runtime.orchestrator,
            self.deps.runtime.config,
        )
        if materializable_agent_names is not None and self.deps.agent_name in self.deps.runtime.config.agents:
            materializable_agent_names = materializable_agent_names | {self.deps.agent_name}
        live_entity_names = (
            live_responder_entity_names(
                self.deps.runtime.orchestrator,
                self.deps.runtime.config,
            )
            if materializable_agent_names is not None
            else None
        )
        return _ResponderAvailability(
            materializable_agent_names=materializable_agent_names,
            live_entity_names=live_entity_names,
        )

    def filter_materializable_responders(
        self,
        responder_ids: list[MatrixID],
        availability: _ResponderAvailability,
    ) -> list[MatrixID]:
        """Keep only materializable responder candidates when live state is known."""
        return filter_materializable_responders(
            responder_ids,
            self.deps.runtime.config,
            self.deps.runtime_paths,
            materializable_agent_names=availability.materializable_agent_names,
            live_entity_names=availability.live_entity_names,
        )

    async def responder_candidates_for_room(
        self,
        room: nio.MatrixRoom,
        requester_user_id: str,
        availability: _ResponderAvailability,
    ) -> list[MatrixID]:
        """Return sender-visible candidates filtered by live responder availability."""
        available_responders = await responder_candidate_entities_for_room(
            self.deps.runtime.client,
            room,
            requester_user_id,
            self.deps.runtime.config,
            self.deps.runtime_paths,
        )
        return self.filter_materializable_responders(available_responders, availability)

    def response_owner_for_team_resolution(
        self,
        form_team: TeamResolution,
        responder_pool: list[MatrixID],
    ) -> MatrixID | None:
        """Return the single live bot that should surface this resolution."""
        if form_team.outcome is TeamOutcome.NONE:
            return None

        if self._requires_shared_owner_for_explicit_private_resolution(form_team):
            return self._shared_owner_for_explicit_private_resolution(form_team, responder_pool)

        response_owner = self._select_response_owner(
            self._eligible_responder_owners(form_team, responder_pool),
        )
        if response_owner is not None:
            return response_owner

        if form_team.intent is TeamIntent.EXPLICIT_MEMBERS and form_team.outcome is TeamOutcome.REJECT:
            # A reject can be visible even when no requested member is eligible;
            # explicit configured-team rejects rely on this when the team bot is
            # the only live responder.
            return self._select_response_owner(responder_pool)

        return None

    @staticmethod
    def _select_response_owner(response_owners: list[MatrixID]) -> MatrixID | None:
        """Return the stable visible owner from one candidate set."""
        if not response_owners:
            return None
        return min(response_owners, key=lambda value: value.full_id)

    @staticmethod
    def _eligible_responder_owners(
        form_team: TeamResolution,
        responder_pool: list[MatrixID],
    ) -> list[MatrixID]:
        """Return eligible team members that are present in the live responder pool."""
        responder_pool_ids = {responder.full_id for responder in responder_pool}
        return [member for member in form_team.eligible_members if member.full_id in responder_pool_ids]

    def _shared_owner_for_explicit_private_resolution(
        self,
        form_team: TeamResolution,
        responder_pool: list[MatrixID],
    ) -> MatrixID | None:
        """Return a live shared-agent owner for a private explicit team or reject."""
        shared_agent_responders = self._live_shared_agent_responders(responder_pool)
        shared_responder_ids = {responder.full_id for responder in shared_agent_responders}
        shared_eligible_owners = [
            member for member in form_team.eligible_members if member.full_id in shared_responder_ids
        ]
        return self._select_response_owner(shared_eligible_owners or shared_agent_responders)

    def _requires_shared_owner_for_explicit_private_resolution(self, form_team: TeamResolution) -> bool:
        """Return whether a private ad hoc team resolution needs a shared visible owner."""
        if form_team.intent is not TeamIntent.EXPLICIT_MEMBERS:
            return False
        if form_team.outcome not in {TeamOutcome.TEAM, TeamOutcome.REJECT}:
            return False

        registry = entity_identity_registry(self.deps.runtime.config, self.deps.runtime_paths)
        members = [*form_team.requested_members, *form_team.eligible_members]
        for member in members:
            entity_name = registry.current_entity_name_for_user_id(member.full_id, include_router=False)
            if entity_name is None:
                continue
            agent_config = self.deps.runtime.config.agents.get(entity_name)
            if agent_config is not None and agent_config.private is not None:
                return True
        return False

    def _live_shared_agent_responders(self, responder_pool: list[MatrixID]) -> list[MatrixID]:
        """Return fallback responders that can execute ad hoc team runs as agents."""
        registry = entity_identity_registry(self.deps.runtime.config, self.deps.runtime_paths)
        shared_responders: list[MatrixID] = []
        for responder in responder_pool:
            entity_name = registry.current_entity_name_for_user_id(responder.full_id)
            if entity_name is None:
                continue
            agent_config = self.deps.runtime.config.agents.get(entity_name)
            if agent_config is not None and agent_config.private is None:
                shared_responders.append(responder)
        return shared_responders

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

    def configured_team_response_action(
        self,
        availability: _ResponderAvailability,
    ) -> ResponseAction | None:
        """Return the configured-team response action for this bot when it represents a team."""
        team_config = self.deps.runtime.config.teams.get(self.deps.agent_name)
        if team_config is None:
            return None
        configured_mode = TeamMode.COORDINATE if team_config.mode == "coordinate" else TeamMode.COLLABORATE
        registry = entity_identity_registry(self.deps.runtime.config, self.deps.runtime_paths)
        team_agents = [registry.current_id(agent_name) for agent_name in team_config.agents]
        team_resolution = resolve_configured_team(
            self.deps.agent_name,
            team_agents,
            configured_mode,
            self.deps.runtime.config,
            self.deps.runtime_paths,
            materializable_agent_names=availability.materializable_agent_names,
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
        if action.kind != "individual":
            return action
        configured_team_action = self.configured_team_response_action(self.responder_availability())
        return configured_team_action or action

    def explicit_configured_team_rejection_action(
        self,
        context: MessageContext,
        sender_visible_responders: list[MatrixID],
        availability: _ResponderAvailability,
    ) -> ResponseAction | None:
        """Return the explicit configured-team rejection action for this live team bot."""
        if self.deps.agent_name not in self.deps.runtime.config.teams:
            return None
        if not context.am_i_mentioned:
            return None
        if availability.live_entity_names is not None and self.deps.agent_name not in availability.live_entity_names:
            return None

        registry = entity_identity_registry(self.deps.runtime.config, self.deps.runtime_paths)
        team_matrix_id = registry.current_id(self.deps.agent_name)
        sender_visible_ids = {responder.full_id for responder in sender_visible_responders}
        if team_matrix_id.full_id not in sender_visible_ids:
            return None

        configured_team_action = self.configured_team_response_action(availability)
        if configured_team_action is None or configured_team_action.kind != "reject":
            return None
        return configured_team_action

    async def decide_team_for_sender(
        self,
        agents_in_thread: list[MatrixID],
        context: MessageContext,
        room: nio.MatrixRoom,
        requester_user_id: str,
        is_dm: bool,
        *,
        availability: _ResponderAvailability,
        available_responders_in_room: list[MatrixID] | None = None,
    ) -> TeamResolution:
        """Decide team formation using sender-visible candidates without losing explicit intent."""
        planning_thread_history = context.planning_thread_history
        if (
            context.is_thread
            and not context.mentioned_agents
            and has_multiple_non_agent_users_in_thread(
                planning_thread_history,
                self.deps.runtime.config,
                self.deps.runtime_paths,
            )
        ):
            return TeamResolution.none()

        all_mentioned_in_thread = get_all_mentioned_agents_in_thread(
            planning_thread_history,
            self.deps.runtime.config,
            self.deps.runtime_paths,
        )
        if available_responders_in_room is None:
            available_responders_in_room = await self.responder_candidates_for_room(
                room,
                requester_user_id,
                availability,
            )
        return decide_team_formation(
            context.mentioned_agents,
            agents_in_thread,
            all_mentioned_in_thread,
            room=room,
            config=self.deps.runtime.config,
            runtime_paths=self.deps.runtime_paths,
            is_dm_room=is_dm,
            is_thread=context.is_thread,
            available_responders_in_room=available_responders_in_room,
            materializable_agent_names=availability.materializable_agent_names,
            allow_explicit_private_agents=True,
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
    ) -> _DispatchPlan | None:
        """Return one router-specific dispatch plan when this entity is the router."""
        if self.deps.agent_name != ROUTER_AGENT_NAME:
            return None

        context = dispatch.context
        planning_thread_history = context.planning_thread_history
        requester_user_id = dispatch.requester_user_id
        if is_router_only_agent_mention(
            context.mentioned_agents,
            has_non_agent_mentions=context.has_non_agent_mentions,
            config=self.deps.runtime.config,
            runtime_paths=self.deps.runtime_paths,
        ):
            plan = _DispatchPlan(
                kind="respond",
                response_action=ResponseAction(
                    kind="reject",
                    rejection_message=_ROUTER_ONLY_MENTION_GUIDANCE,
                ),
            )
        elif context.mentioned_agents or context.has_non_agent_mentions:
            plan = _DispatchPlan(kind="ignore", ignore_reason="router")
        elif context.planning_thread_history_unavailable:
            self.deps.logger.info("Skipping routing: thread policy history unavailable")
            plan = _DispatchPlan(kind="ignore", ignore_reason="router")
        else:
            available_responders = await self.responder_candidates_for_room(
                room,
                requester_user_id,
                self.responder_availability(),
            )
            if context.is_thread and thread_requires_explicit_agent_targeting(
                planning_thread_history,
                sender_id=requester_user_id,
                config=self.deps.runtime.config,
                runtime_paths=self.deps.runtime_paths,
                available_responders_in_room=available_responders,
            ):
                self.deps.logger.info("Skipping routing: thread already requires explicit responder targeting")
                plan = _DispatchPlan(kind="ignore", ignore_reason="router")
            elif len(available_responders) == 1:
                self.deps.logger.info("Skipping routing: only one responder candidate")
                plan = _DispatchPlan(kind="ignore", ignore_reason="router")
            else:
                plan = _DispatchPlan(
                    kind="route",
                    router_message=message,
                    extra_content=extra_content,
                    media_events=media_events,
                    router_event=router_event or event,
                )
        return plan

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
    ) -> _DispatchPlan:
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
            dispatch,
            room,
            is_dm,
            has_active_response_for_target=has_active_response_for_target,
        )
        if action.kind == "skip":
            return _DispatchPlan(kind="ignore")
        return _DispatchPlan(kind="respond", response_action=action)

    async def resolve_response_action(
        self,
        dispatch: PreparedDispatch,
        room: nio.MatrixRoom,
        is_dm: bool,
        *,
        has_active_response_for_target: Callable[[MessageTarget], bool],
    ) -> ResponseAction:
        """Decide whether to respond as a team, individually, or not at all."""
        context = dispatch.context
        requester_user_id = dispatch.requester_user_id
        planning_thread_history = context.planning_thread_history
        availability = self.responder_availability()
        sender_visible_responders_in_room = await responder_candidate_entities_for_room(
            self.deps.runtime.client,
            room,
            requester_user_id,
            self.deps.runtime.config,
            self.deps.runtime_paths,
        )
        available_responders_in_room = self.filter_materializable_responders(
            sender_visible_responders_in_room,
            availability,
        )
        registry = entity_identity_registry(self.deps.runtime.config, self.deps.runtime_paths)
        agent_matrix_id = registry.current_id(self.deps.agent_name)
        agent_is_responder_candidate = agent_matrix_id.full_id in {
            responder.full_id for responder in available_responders_in_room
        }
        team_action = self.explicit_configured_team_rejection_action(
            context,
            sender_visible_responders_in_room,
            availability,
        )
        if (
            context.planning_thread_history_unavailable
            and not context.am_i_mentioned
            and not context.mentioned_agents
            and not context.has_non_agent_mentions
        ):
            should_continue_active_thread = (
                agent_is_responder_candidate
                and self._should_queue_follow_up_in_active_response_thread(
                    context=context,
                    target=dispatch.target,
                    source_envelope=dispatch.envelope,
                    has_active_response_for_target=has_active_response_for_target,
                )
            )
            single_visible_self = (
                len(available_responders_in_room) == 1 and available_responders_in_room[0] == agent_matrix_id
            )
            if should_continue_active_thread or single_visible_self:
                return ResponseAction(kind="individual")
            return ResponseAction(kind="skip")
        agents_in_thread = get_agents_in_thread(
            planning_thread_history,
            self.deps.runtime.config,
            self.deps.runtime_paths,
        )
        if team_action is None:
            # Use sender-visible responders here so explicit team requests can distinguish
            # hidden members from visible-but-not-materializable members.
            form_team = await self.decide_team_for_sender(
                agents_in_thread,
                context,
                room,
                requester_user_id,
                is_dm,
                availability=availability,
                available_responders_in_room=sender_visible_responders_in_room,
            )
            team_action = self.team_response_action(form_team, available_responders_in_room)
        if team_action is not None:
            return team_action

        agent_response_decision = decide_agent_response(
            agent_name=self.deps.agent_name,
            am_i_mentioned=context.am_i_mentioned,
            is_thread=context.is_thread,
            room=room,
            thread_history=planning_thread_history,
            config=self.deps.runtime.config,
            runtime_paths=self.deps.runtime_paths,
            mentioned_agents=context.mentioned_agents,
            has_non_agent_mentions=context.has_non_agent_mentions,
            sender_id=requester_user_id,
            available_responders_in_room=available_responders_in_room,
            agents_in_thread=agents_in_thread,
        )
        if not agent_response_decision.should_respond:
            if agent_is_responder_candidate and self._should_queue_follow_up_in_active_response_thread(
                context=context,
                target=dispatch.target,
                source_envelope=dispatch.envelope,
                has_active_response_for_target=has_active_response_for_target,
            ):
                return ResponseAction(kind="individual")
            if agent_is_responder_candidate:
                self._log_multi_agent_thread_skip(
                    context,
                    agent_response_decision,
                )
            return ResponseAction(kind="skip")

        return ResponseAction(kind="individual")

    def _log_multi_agent_thread_skip(
        self,
        context: MessageContext,
        agent_response_decision: AgentResponseDecision,
    ) -> None:
        """Log the multi-agent thread branch selected by individual response policy."""
        if agent_response_decision.skip_reason != "multiple_agents_in_thread":
            return

        agents_in_thread = agent_response_decision.sender_visible_thread_agents
        if len(agents_in_thread) < 2:
            return

        self.deps.logger.info(
            "Skipping response: multiple agents in thread require explicit mention",
            agent_name=self.deps.agent_name,
            thread_id=context.thread_id,
            agents_in_thread=[agent.full_id for agent in agents_in_thread],
        )

    def _should_queue_follow_up_in_active_response_thread(
        self,
        *,
        context: MessageContext,
        target: MessageTarget,
        source_envelope: MessageEnvelope,
        has_active_response_for_target: Callable[[MessageTarget], bool],
    ) -> bool:
        """Return whether one human follow-up should enter the queued-response path."""
        if not context.is_thread:
            return False
        if context.mentioned_agents or context.has_non_agent_mentions:
            return False
        if not source_envelope.origin.may_answer_interactive_prompt:
            return False
        policy_source_kind = source_envelope.dispatch_policy_source_kind or source_envelope.source_kind
        if policy_source_kind == ACTIVE_THREAD_FOLLOW_UP_SOURCE_KIND:
            return True
        return has_active_response_for_target(target)
