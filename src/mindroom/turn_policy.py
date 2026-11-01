"""Pure turn policy for inbound turns."""

from __future__ import annotations

from dataclasses import dataclass, replace
from typing import TYPE_CHECKING, Any, Literal

from mindroom.authorization import (
    ReplyMembershipPendingError,
    classify_responder_candidates_from_cached_room,
    configured_responder_entities_for_room,
    get_available_responders_in_room,
    is_sender_allowed_for_agent_reply_in_room,
    is_sender_allowed_for_responder,
)
from mindroom.constants import ROUTER_AGENT_NAME, RuntimePaths
from mindroom.contacts.turns import answers_alone, leaves_to_other_agent
from mindroom.dispatch_source import ACTIVE_THREAD_FOLLOW_UP_SOURCE_KIND, ScheduledHistoryBudget
from mindroom.entity_resolution import entity_identity_registry
from mindroom.managed_room_notice import managed_room_notice, responder_display_names
from mindroom.matrix.rooms import is_dm_room
from mindroom.responder_availability import filter_materializable_responders
from mindroom.runtime_protocols import SupportsClientConfigOrchestrator  # noqa: TC001
from mindroom.teams.formation import (
    TeamIntent,
    TeamMemberStatus,
    TeamOutcome,
    TeamResolution,
    decide_team_formation,
    resolve_configured_team,
    resolve_live_shared_agent_names,
)
from mindroom.teams.spec import TeamMode
from mindroom.thread_utils import (
    AgentResponseDecision,
    check_agent_mentioned,
    decide_agent_response,
    get_agents_in_thread,
    get_all_mentioned_agents_in_thread,
    has_multiple_non_agent_users_in_thread,
    is_router_only_agent_mention,
    thread_requires_explicit_agent_targeting,
)
from mindroom.timing import timed

if TYPE_CHECKING:
    from collections.abc import Callable, Sequence

    import nio
    import structlog

    from mindroom.agent_reply_membership import AgentReplyMembershipIndex
    from mindroom.authorization import ResponderCandidatePermissions
    from mindroom.config.participation import ParticipationConfig
    from mindroom.conversation_resolver import MessageContext
    from mindroom.dispatch_handoff import DispatchEvent, MediaDispatchEvent, PreparedIngress
    from mindroom.hooks import MessageEnvelope
    from mindroom.matrix.client_visible_messages import ResolvedVisibleMessage
    from mindroom.matrix.identity import MatrixID
    from mindroom.message_target import MessageTarget


@dataclass(frozen=True)
class ResponseAction:
    """Result of the shared team-formation and should-respond decision."""

    kind: Literal["skip", "team", "individual", "reject"]
    form_team: TeamResolution | None = None
    rejection_message: str | None = None
    participation: ParticipationConfig | None = None


@dataclass(frozen=True)
class PreparedDispatch:
    """Common dispatch context reused across text and media ingress handlers."""

    requester_user_id: str
    context: MessageContext
    target: MessageTarget
    correlation_id: str
    envelope: MessageEnvelope
    current_prompt_is_structured: bool = False
    scheduled_history_budget: ScheduledHistoryBudget | None = None
    scheduled_model: str | None = None

    def __post_init__(self) -> None:
        """Require the prepared envelope and dispatch target to describe the same delivery."""
        if self.envelope.target != self.target:
            msg = "Prepared dispatch envelope target must match the resolved dispatch target"
            raise ValueError(msg)


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


_ROUTER_ONLY_MENTION_GUIDANCE = (
    "🧭 Rules of engagement: mention a specific agent or team when you want that entity to answer, or mention "
    "multiple agents when you want an ad-hoc collaboration. If one human and one agent or team are already talking "
    "in a thread, you can keep going without an explicit tag. Explicit tags select who answers next; an untagged "
    "single-human thread may also continue an eligible ad-hoc team of participating individual agents. "
    "In multi-human threads, explicitly tag who should answer; eligible existing individual agents may opt into "
    "adaptive participation, but teams do not form automatically. In a new untagged message, "
    "automatic routing can still choose an agent or team when appropriate. The router is not a conversational AI "
    "agent you can tag directly."
)


@dataclass(frozen=True)
class TurnPolicyDeps:
    """Explicit collaborators needed by pure turn policy decisions."""

    runtime: SupportsClientConfigOrchestrator
    logger: structlog.stdlib.BoundLogger
    runtime_paths: RuntimePaths
    agent_name: str
    matrix_id: MatrixID
    agent_reply_memberships: AgentReplyMembershipIndex


@dataclass(frozen=True)
class _ResponderAvailability:
    """Point-in-time responder availability threaded through one decision flow."""

    materializable_agent_names: set[str]
    live_entity_names: frozenset[str]


@dataclass(frozen=True)
class TurnPolicy:
    """Own pure decision logic for one prepared inbound turn."""

    deps: TurnPolicyDeps

    def _client(self) -> nio.AsyncClient:
        client = self.deps.runtime.client
        if client is None:
            msg = "Matrix client is not ready for turn execution"
            raise RuntimeError(msg)
        return client

    def can_reply_to_sender_in_room(
        self,
        sender_id: str,
        room_id: str,
        *,
        observed_room: nio.MatrixRoom | None = None,
    ) -> bool:
        """Decide durable turn access, retrying unresolved membership before settlement.

        ``observed_room`` is this bot's own view of the room; it only lets a denial wait for the router's join.
        """
        return is_sender_allowed_for_agent_reply_in_room(
            sender_id,
            self.deps.agent_name,
            self.deps.runtime.config,
            room_id,
            self.deps.runtime_paths,
            self.deps.agent_reply_memberships,
            require_resolved_membership=True,
            observed_room=observed_room,
        )

    def _adaptive_thread_participation(
        self,
        context: MessageContext,
        requester_user_id: str,
    ) -> ParticipationConfig | None:
        """Resolve this agent's participation for proven untagged multi-human context."""
        agent = self.deps.runtime.config.agents.get(self.deps.agent_name)
        participation = agent.participation if agent is not None else None
        if (
            participation is not None
            and context.is_thread
            and not context.planning_thread_history_unavailable
            and not context.mentioned_agents
            and not context.am_i_mentioned
            and not context.has_non_agent_mentions
            and has_multiple_non_agent_users_in_thread(
                context.planning_thread_history,
                self.deps.runtime.config,
                self.deps.runtime_paths,
                current_sender_id=requester_user_id,
            )
        ):
            return participation
        return None

    def _is_participating_thread_agent(self, context: MessageContext) -> bool:
        """Only individual agents that have already replied may judge continuation."""
        return self.deps.agent_name in self.deps.runtime.config.agents and self.deps.matrix_id in get_agents_in_thread(
            context.planning_thread_history,
            self.deps.runtime.config,
            self.deps.runtime_paths,
        )

    def adaptive_participation(
        self,
        *,
        context: MessageContext,
        room: nio.MatrixRoom,
        requester_user_id: str,
    ) -> ParticipationConfig | None:
        """Select an authorized existing agent for a proven multi-human thread."""
        participation = self._adaptive_thread_participation(context, requester_user_id)
        if participation is None or not self._is_participating_thread_agent(context):
            return None
        candidates = classify_responder_candidates_from_cached_room(
            room,
            requester_user_id,
            self.deps.runtime.config,
            self.deps.runtime_paths,
            self.deps.agent_reply_memberships,
        )
        available = self._filter_materializable_responders(candidates.allowed, self._responder_availability())
        return participation if self.deps.matrix_id in available else None

    def _responder_availability(self) -> _ResponderAvailability:
        """Snapshot in-memory responder liveness for one decision flow.

        Each decision flow takes a fresh snapshot on entry and passes it down
        instead of recomputing; snapshots are deliberately not cached on this
        long-lived policy object because liveness changes between turns.
        """
        config = self.deps.runtime.config
        orchestrator = self.deps.runtime.orchestrator
        materializable_agent_names = resolve_live_shared_agent_names(orchestrator, config=config)
        if self.deps.agent_name in config.agents:
            materializable_agent_names.add(self.deps.agent_name)
        return _ResponderAvailability(
            materializable_agent_names=materializable_agent_names,
            live_entity_names=orchestrator.running_entity_names(),
        )

    def _filter_materializable_responders(
        self,
        responder_ids: list[MatrixID],
        availability: _ResponderAvailability,
    ) -> list[MatrixID]:
        """Keep only the responder candidates that can answer now."""
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
    ) -> list[MatrixID]:
        """Return candidates from the boundary-prepared room membership snapshot."""
        availability = self._responder_availability()
        candidates = classify_responder_candidates_from_cached_room(
            room,
            requester_user_id,
            self.deps.runtime.config,
            self.deps.runtime_paths,
            self.deps.agent_reply_memberships,
        )
        if self._filter_materializable_responders(candidates.pending, availability):
            raise ReplyMembershipPendingError
        return self._filter_materializable_responders(candidates.allowed, availability)

    def _response_owner_for_team_resolution(
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
        shared_eligible_owners = self._eligible_responder_owners(form_team, shared_agent_responders)
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

    def _team_response_action(
        self,
        form_team: TeamResolution,
        responder_pool: list[MatrixID],
        pending_responders: list[MatrixID],
    ) -> ResponseAction | None:
        """Resolve one team outcome only when its execution and visible owner are proven."""
        if form_team.outcome is TeamOutcome.NONE:
            return None
        pending_ids = {responder.full_id for responder in pending_responders}
        if form_team.outcome is not TeamOutcome.REJECT and any(
            member.full_id in pending_ids for member in form_team.eligible_members
        ):
            raise ReplyMembershipPendingError
        response_owner = self._response_owner_for_team_resolution(form_team, responder_pool)
        if pending_ids:
            resolved_pool = [responder for responder in responder_pool if responder.full_id not in pending_ids]
            resolved_owner = self._response_owner_for_team_resolution(form_team, resolved_pool)
            # A proven rejection may proceed only if every remaining membership
            # outcome chooses the same authorized visible owner.
            if response_owner != resolved_owner:
                raise ReplyMembershipPendingError
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

    def _configured_team_response_action(
        self,
        availability: _ResponderAvailability,
    ) -> ResponseAction | None:
        """Return the configured-team response action for this bot when it represents a team."""
        team_config = self.deps.runtime.config.teams.get(self.deps.agent_name)
        if team_config is None:
            return None
        configured_mode = TeamMode(team_config.mode)
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
        configured_team_action = self._configured_team_response_action(self._responder_availability())
        return configured_team_action or action

    def _explicit_configured_team_rejection_action(
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
        if self.deps.agent_name not in availability.live_entity_names:
            return None

        registry = entity_identity_registry(self.deps.runtime.config, self.deps.runtime_paths)
        team_matrix_id = registry.current_id(self.deps.agent_name)
        sender_visible_ids = {responder.full_id for responder in sender_visible_responders}
        if team_matrix_id.full_id not in sender_visible_ids:
            return None

        configured_team_action = self._configured_team_response_action(availability)
        if configured_team_action is None or configured_team_action.kind != "reject":
            return None
        return configured_team_action

    async def _decide_team_for_sender(
        self,
        agents_in_thread: list[MatrixID],
        context: MessageContext,
        room: nio.MatrixRoom,
        *,
        availability: _ResponderAvailability,
        available_responders_in_room: list[MatrixID],
    ) -> TeamResolution:
        """Decide team formation using sender-visible candidates without losing explicit intent."""
        if answers_alone(
            self.deps.runtime.config,
            self.deps.runtime_paths,
            room_id=room.room_id,
            agent_name=self.deps.agent_name,
        ):
            return TeamResolution.none()
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
        # Only a DM auto team depends on DM status, and it needs an unthreaded message
        # with several responders, so skip the homeserver lookup otherwise.
        is_dm = (
            not context.is_thread
            and len(available_responders_in_room) > 1
            and await is_dm_room(self._client(), room.room_id)
        )
        return decide_team_formation(
            context.mentioned_agents,
            agents_in_thread,
            all_mentioned_in_thread,
            config=self.deps.runtime.config,
            runtime_paths=self.deps.runtime_paths,
            is_dm_room=is_dm,
            is_thread=context.is_thread,
            available_responders_in_room=available_responders_in_room,
            materializable_agent_names=availability.materializable_agent_names,
        )

    def _router_mention(
        self,
        mentioned_agents: list[MatrixID],
        *,
        has_non_agent_mentions: bool,
    ) -> Literal["router_only", "addressed_elsewhere"] | None:
        """Classify a message's mentions for the router: a question to the router itself, someone else's turn, or none."""
        if is_router_only_agent_mention(
            mentioned_agents,
            has_non_agent_mentions=has_non_agent_mentions,
            config=self.deps.runtime.config,
            runtime_paths=self.deps.runtime_paths,
        ):
            return "router_only"
        if mentioned_agents or has_non_agent_mentions:
            return "addressed_elsewhere"
        return None

    def _thread_defers_to_named_responders(
        self,
        thread_history: Sequence[ResolvedVisibleMessage],
        *,
        sender_id: str,
        available_responders: list[MatrixID],
    ) -> bool:
        """Return whether a thread's history leaves this sender's next message to the responders it names."""
        return thread_requires_explicit_agent_targeting(
            thread_history,
            sender_id=sender_id,
            config=self.deps.runtime.config,
            runtime_paths=self.deps.runtime_paths,
            membership_index=self.deps.agent_reply_memberships,
            available_responders_in_room=available_responders,
        )

    def router_mention_skips(self, room: nio.MatrixRoom, source: dict[str, Any]) -> bool | None:
        """Return whether the router may drop a message before shared ingress work by its mentions alone.

        True for a message addressed to someone else, False for a question to the router itself, and None when the
        mentions do not decide it. The same mention rule decides the router's dispatch plan.
        """
        mentioned_agents, _am_i_mentioned, has_non_agent_mentions = check_agent_mentioned(
            source,
            self.deps.matrix_id,
            self.deps.runtime.config,
            self.deps.runtime_paths,
            room=room,
        )
        mention = self._router_mention(mentioned_agents, has_non_agent_mentions=has_non_agent_mentions)
        return None if mention is None else mention == "addressed_elsewhere"

    async def router_thread_skips(
        self,
        room: nio.MatrixRoom,
        requester_user_id: str,
        thread_history: Sequence[ResolvedVisibleMessage],
    ) -> bool:
        """Return whether the router may drop an unmentioned thread message whose thread names its responders.

        The same thread rule decides the router's dispatch plan, which runs it on complete planning history.
        """
        return self._thread_defers_to_named_responders(
            thread_history,
            sender_id=requester_user_id,
            available_responders=await self.responder_candidates_for_room(room, requester_user_id),
        )

    async def _plan_router_dispatch(  # noqa: PLR0911
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
        mention = self._router_mention(context.mentioned_agents, has_non_agent_mentions=context.has_non_agent_mentions)
        ignore = DispatchPlan(kind="ignore", ignore_reason="router")
        if mention == "addressed_elsewhere":
            return ignore
        if mention is None and context.planning_thread_history_unavailable:
            self.deps.logger.info("Skipping routing: thread policy history unavailable")
            return ignore
        if not self.can_reply_to_sender_in_room(requester_user_id, room.room_id):
            return ignore
        if mention == "router_only":
            return DispatchPlan(
                kind="respond",
                response_action=ResponseAction(kind="reject", rejection_message=_ROUTER_ONLY_MENTION_GUIDANCE),
            )
        available_responders = await self.responder_candidates_for_room(room, requester_user_id)
        if context.is_thread and self._thread_defers_to_named_responders(
            context.planning_thread_history,
            sender_id=requester_user_id,
            available_responders=available_responders,
        ):
            self.deps.logger.info("Skipping routing: thread already requires explicit responder targeting")
            return ignore
        if len(available_responders) == 1:
            self.deps.logger.info("Skipping routing: only one responder candidate")
            return ignore
        return DispatchPlan(
            kind="route",
            router_message=message,
            extra_content=extra_content,
            media_events=media_events,
            router_event=router_event or event,
        )

    @timed("dispatch_action_resolution")
    async def plan_turn(
        self,
        room: nio.MatrixRoom,
        event: PreparedIngress,
        dispatch: PreparedDispatch,
        *,
        has_active_response_for_target: Callable[[MessageTarget], bool],
        extra_content: dict[str, Any] | None = None,
        media_events: list[MediaDispatchEvent] | None = None,
        router_event: DispatchEvent | None = None,
    ) -> DispatchPlan:
        """Return the explicit policy plan for one prepared inbound turn."""
        router_plan = await self._plan_router_dispatch(
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
        if self._leaves_to_other_contact_agent(dispatch):
            return DispatchPlan(kind="ignore")

        action = await self._resolve_response_action(
            dispatch,
            room,
            has_active_response_for_target=has_active_response_for_target,
        )
        if action.kind == "skip":
            return DispatchPlan(kind="ignore")
        return DispatchPlan(kind="respond", response_action=action)

    def _leaves_to_other_contact_agent(self, dispatch: PreparedDispatch) -> bool:
        """Return whether an unmentioned message in this agent's contact room is the other agent's to answer."""
        context = dispatch.context
        return (
            not context.am_i_mentioned
            and not context.mentioned_agents
            and not context.has_non_agent_mentions
            and leaves_to_other_agent(
                self.deps.runtime.config,
                self.deps.runtime_paths,
                room_id=dispatch.target.room_id,
                agent_name=self.deps.agent_name,
                sender_id=dispatch.envelope.origin.transport_sender_id,
            )
        )

    def _adaptive_response_action(
        self,
        dispatch: PreparedDispatch,
        available_responders: list[MatrixID],
    ) -> ResponseAction | None:
        """Select participation for ambient turns, including coalesced active follow-ups."""
        if not dispatch.envelope.origin.may_answer_interactive_prompt:
            return None
        participation = self._adaptive_thread_participation(
            dispatch.context,
            dispatch.requester_user_id,
        )
        if participation is None:
            return None
        if not self._is_participating_thread_agent(dispatch.context) or self.deps.matrix_id not in available_responders:
            return ResponseAction(kind="skip")
        return ResponseAction(kind="individual", participation=participation)

    async def _resolve_response_action(  # noqa: PLR0911
        self,
        dispatch: PreparedDispatch,
        room: nio.MatrixRoom,
        *,
        has_active_response_for_target: Callable[[MessageTarget], bool],
    ) -> ResponseAction:
        """Decide whether to respond as a team, individually, or not at all."""
        context = dispatch.context
        requester_user_id = dispatch.requester_user_id
        planning_thread_history = context.planning_thread_history
        availability = self._responder_availability()
        candidates = classify_responder_candidates_from_cached_room(
            room,
            requester_user_id,
            self.deps.runtime.config,
            self.deps.runtime_paths,
            self.deps.agent_reply_memberships,
        )
        available_responders_in_room = self._filter_materializable_responders(candidates.allowed, availability)
        pending_responders_in_room = self._filter_materializable_responders(candidates.pending, availability)
        registry = entity_identity_registry(self.deps.runtime.config, self.deps.runtime_paths)
        agent_matrix_id = registry.current_id(self.deps.agent_name)
        agent_is_responder_candidate = agent_matrix_id in available_responders_in_room
        if not self._has_resolved_planning_candidates(
            context,
            candidates,
            available_responders_in_room,
            pending_responders_in_room,
        ):
            return ResponseAction(kind="skip")

        # Pending members remain possible for team intent and definitive rejection.
        # They never authorize execution; the team action validates that below.
        sender_visible_responders_in_room = [*candidates.allowed, *candidates.pending]
        team_action = self._explicit_configured_team_rejection_action(
            context,
            candidates.allowed,
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
        participation_action = self._adaptive_response_action(dispatch, available_responders_in_room)
        if participation_action is not None:
            return participation_action
        agents_in_thread = get_agents_in_thread(
            planning_thread_history,
            self.deps.runtime.config,
            self.deps.runtime_paths,
        )
        if team_action is None:
            # Use sender-visible responders here so explicit team requests can distinguish
            # hidden members from visible-but-not-materializable members.
            form_team = await self._decide_team_for_sender(
                agents_in_thread,
                context,
                room,
                availability=availability,
                available_responders_in_room=sender_visible_responders_in_room,
            )
            team_action = self._explain_managed_room_team_rejection(
                self._team_response_action(
                    form_team,
                    [*available_responders_in_room, *pending_responders_in_room],
                    candidates.pending,
                ),
                room,
                requester_user_id,
                available_responders_in_room,
            )
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
            membership_index=self.deps.agent_reply_memberships,
            mentioned_agents=context.mentioned_agents,
            has_non_agent_mentions=context.has_non_agent_mentions,
            sender_id=requester_user_id,
            available_responders_in_room=available_responders_in_room,
            agents_in_thread=agents_in_thread,
            require_resolved_membership=True,
        )
        if not agent_response_decision.should_respond and agent_is_responder_candidate:
            if self._should_queue_follow_up_in_active_response_thread(
                context=context,
                target=dispatch.target,
                source_envelope=dispatch.envelope,
                has_active_response_for_target=has_active_response_for_target,
            ):
                return ResponseAction(kind="individual")
            self._log_multi_agent_thread_skip(context, agent_response_decision)
        if rejection := self._unlisted_room_mention_rejection(
            context,
            room,
            agent_response_decision,
            available_responders_in_room,
        ):
            return rejection
        return ResponseAction(kind="individual" if agent_response_decision.should_respond else "skip")

    def _unlisted_room_mention_rejection(
        self,
        context: MessageContext,
        room: nio.MatrixRoom,
        agent_response_decision: AgentResponseDecision,
        available_responders: list[MatrixID],
    ) -> ResponseAction | None:
        """Explain a direct mention that the configured room's responder boundary excludes."""
        if not context.am_i_mentioned or agent_response_decision.skip_reason != "agent_not_available":
            return None
        configured_responders = configured_responder_entities_for_room(
            room,
            self.deps.runtime.config,
            self.deps.runtime_paths,
        )
        if configured_responders is None or self.deps.matrix_id in configured_responders:
            return None
        self.deps.logger.info("Rejecting mention: agent is not configured for this room", room_id=room.room_id)
        return ResponseAction(
            kind="reject",
            rejection_message=managed_room_notice(available=self._display_names(available_responders)),
        )

    def _explain_managed_room_team_rejection(
        self,
        team_action: ResponseAction | None,
        room: nio.MatrixRoom,
        requester_user_id: str,
        available_responders: list[MatrixID],
    ) -> ResponseAction | None:
        """Use the managed-room note when only that room's responder boundary rejected a team request."""
        if team_action is None or team_action.kind != "reject" or team_action.form_team is None:
            return team_action
        rejected = [
            member for member in team_action.form_team.member_statuses if member.status is not TeamMemberStatus.ELIGIBLE
        ]
        config = self.deps.runtime.config
        configured_responders = configured_responder_entities_for_room(room, config, self.deps.runtime_paths)
        if not rejected or configured_responders is None:
            return team_action
        configured_ids = {responder.full_id for responder in configured_responders}
        joined_ids = {
            responder.full_id for responder in get_available_responders_in_room(room, config, self.deps.runtime_paths)
        }
        if any(
            member.agent.full_id in configured_ids
            or member.agent.full_id not in joined_ids
            or not is_sender_allowed_for_responder(
                requester_user_id,
                member.name,
                room.room_id,
                config,
                self.deps.runtime_paths,
                self.deps.agent_reply_memberships,
            )
            for member in rejected
        ):
            return team_action
        return replace(
            team_action,
            rejection_message=managed_room_notice(
                available=self._display_names(available_responders),
                unavailable=[config.entity_display_name(member.name) for member in rejected],
            ),
        )

    def _display_names(self, responders: list[MatrixID]) -> list[str]:
        """Return the configured display names of responder Matrix IDs."""
        return responder_display_names(responders, self.deps.runtime.config, self.deps.runtime_paths)

    def _has_resolved_planning_candidates(
        self,
        context: MessageContext,
        candidates: ResponderCandidatePermissions,
        available_responders: list[MatrixID],
        pending_responders: list[MatrixID],
    ) -> bool:
        """Require relevant grants while preserving proven absence of this owner."""
        if not context.mentioned_agents and not context.has_non_agent_mentions:
            if self.deps.matrix_id not in available_responders and self.deps.matrix_id not in pending_responders:
                return False
            if pending_responders:
                raise ReplyMembershipPendingError
        elif len(context.mentioned_agents) == 1 and context.mentioned_agents[0] in candidates.pending:
            raise ReplyMembershipPendingError
        return True

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
