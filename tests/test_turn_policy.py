"""Unit tests for the pure turn policy decisions in turn_policy.py.

These are characterization tests for TurnPolicy.plan_turn: they pin down the
current ignore/route/respond behavior so the planned refactor of this layer
has a direct safety net that does not go through bot-level integration tests.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import TYPE_CHECKING

import nio
import pytest

from mindroom.config.agent import AgentConfig
from mindroom.config.auth import AuthorizationConfig
from mindroom.config.main import Config
from mindroom.constants import ROUTER_AGENT_NAME
from mindroom.conversation_resolver import MessageContext
from mindroom.entity_resolution import entity_identity_registry
from mindroom.logging_config import get_logger
from mindroom.matrix.cache.thread_history_result import thread_history_result
from mindroom.message_target import MessageTarget
from mindroom.teams import TeamIntent, TeamMode, TeamOutcome
from mindroom.turn_policy import PreparedDispatch, TurnPolicy, TurnPolicyDeps
from tests.conftest import (
    bind_runtime_paths,
    make_visible_message,
    request_envelope,
    runtime_paths_for,
    test_runtime_paths,
)

if TYPE_CHECKING:
    from pathlib import Path

    from mindroom.matrix.client import ResolvedVisibleMessage
    from mindroom.matrix.identity import MatrixID

_ROOM_ID = "!test:localhost"
_SENDER = "@user:localhost"
_EVENT_ID = "$event:localhost"


@dataclass(frozen=True)
class _RuntimeStub:
    """Minimal SupportsClientConfigOrchestrator stand-in for pure policy tests."""

    client: nio.AsyncClient | None
    config: Config
    orchestrator: None = None
    runtime_started_at: float = 0.0


@pytest.fixture
def config(tmp_path: Path) -> Config:
    """Two-agent config bound to isolated runtime paths."""
    return bind_runtime_paths(
        Config(
            agents={
                "general": AgentConfig(display_name="General"),
                "research": AgentConfig(display_name="Research"),
            },
        ),
        test_runtime_paths(tmp_path),
    )


def _policy_for(config: Config, agent_name: str) -> TurnPolicy:
    runtime_paths = runtime_paths_for(config)
    registry = entity_identity_registry(config, runtime_paths)
    return TurnPolicy(
        TurnPolicyDeps(
            runtime=_RuntimeStub(client=None, config=config),
            logger=get_logger("test_turn_policy"),
            runtime_paths=runtime_paths,
            agent_name=agent_name,
            matrix_id=registry.current_id(agent_name),
        ),
    )


def _entity_id(config: Config, entity_name: str) -> MatrixID:
    return entity_identity_registry(config, runtime_paths_for(config)).current_id(entity_name)


def _room_with_members(*user_ids: str) -> nio.MatrixRoom:
    room = nio.MatrixRoom(_ROOM_ID, "@mindroom_general:localhost")
    for user_id in user_ids:
        room.add_member(user_id, user_id, None)
    return room


def _context(
    *,
    mentioned: list[MatrixID] | None = None,
    am_i_mentioned: bool = False,
    thread_id: str | None = None,
    thread_history: list[ResolvedVisibleMessage] | None = None,
    full_history: bool = True,
) -> MessageContext:
    history: object = thread_history or []
    if full_history:
        history = thread_history_result(thread_history or [], is_full_history=True)
    return MessageContext(
        am_i_mentioned=am_i_mentioned,
        is_thread=thread_id is not None,
        thread_id=thread_id,
        thread_history=history,
        mentioned_agents=mentioned or [],
        has_non_agent_mentions=False,
    )


def _dispatch(context: MessageContext, *, agent_name: str, sender: str = _SENDER) -> PreparedDispatch:
    target = MessageTarget.resolve(_ROOM_ID, context.thread_id, _EVENT_ID)
    envelope = request_envelope(
        room_id=_ROOM_ID,
        reply_to_event_id=_EVENT_ID,
        thread_id=context.thread_id,
        prompt="hello agents",
        user_id=sender,
        target=target,
        agent_name=agent_name,
    )
    return PreparedDispatch(
        requester_user_id=sender,
        context=context,
        target=target,
        correlation_id="corr-test",
        envelope=envelope,
    )


def _text_event(body: str) -> nio.RoomMessageText:
    return nio.RoomMessageText.from_dict(
        {
            "content": {"body": body, "msgtype": "m.text"},
            "event_id": _EVENT_ID,
            "sender": _SENDER,
            "origin_server_ts": 1_000_000,
            "room_id": _ROOM_ID,
            "type": "m.room.message",
        },
    )


async def _plan(
    policy: TurnPolicy,
    room: nio.MatrixRoom,
    dispatch: PreparedDispatch,
    *,
    is_dm: bool = False,
    has_active_response: bool = False,
) -> object:
    return await policy.plan_turn(
        room,
        _text_event("hello agents"),
        dispatch,
        is_dm=is_dm,
        has_active_response_for_target=lambda _target: has_active_response,
    )


@pytest.mark.asyncio
async def test_mentioned_agent_responds_individually(config: Config) -> None:
    """A direct mention of one agent yields an individual respond plan."""
    policy = _policy_for(config, "general")
    room = _room_with_members(_SENDER, _entity_id(config, "general").full_id, _entity_id(config, "research").full_id)
    context = _context(mentioned=[_entity_id(config, "general")], am_i_mentioned=True)

    plan = await _plan(policy, room, _dispatch(context, agent_name="general"))

    assert plan.kind == "respond"
    assert plan.response_action is not None
    assert plan.response_action.kind == "individual"


@pytest.mark.asyncio
async def test_mention_of_other_agent_is_ignored(config: Config) -> None:
    """An agent must stay silent when only another agent is mentioned."""
    policy = _policy_for(config, "general")
    room = _room_with_members(_SENDER, _entity_id(config, "general").full_id, _entity_id(config, "research").full_id)
    context = _context(mentioned=[_entity_id(config, "research")])

    plan = await _plan(policy, room, _dispatch(context, agent_name="general"))

    assert plan.kind == "ignore"


@pytest.mark.asyncio
async def test_unmentioned_room_message_with_multiple_responders_is_ignored(config: Config) -> None:
    """With several visible responders, no agent self-selects for an untagged room message."""
    policy = _policy_for(config, "general")
    room = _room_with_members(_SENDER, _entity_id(config, "general").full_id, _entity_id(config, "research").full_id)

    plan = await _plan(policy, room, _dispatch(_context(), agent_name="general"))

    assert plan.kind == "ignore"


@pytest.mark.asyncio
async def test_unmentioned_room_message_with_single_responder_responds(config: Config) -> None:
    """The only visible responder answers untagged room messages."""
    policy = _policy_for(config, "general")
    room = _room_with_members(_SENDER, _entity_id(config, "general").full_id)

    plan = await _plan(policy, room, _dispatch(_context(), agent_name="general"))

    assert plan.kind == "respond"
    assert plan.response_action.kind == "individual"


@pytest.mark.asyncio
async def test_thread_continuation_by_sole_thread_agent_responds(config: Config) -> None:
    """An agent keeps answering an untagged thread it already owns."""
    policy = _policy_for(config, "general")
    room = _room_with_members(_SENDER, _entity_id(config, "general").full_id, _entity_id(config, "research").full_id)
    history = [
        make_visible_message(sender=_SENDER, body="please help"),
        make_visible_message(sender=_entity_id(config, "general").full_id, body="sure thing"),
    ]
    context = _context(thread_id="$thread:localhost", thread_history=history)

    plan = await _plan(policy, room, _dispatch(context, agent_name="general"))

    assert plan.kind == "respond"
    assert plan.response_action.kind == "individual"


@pytest.mark.asyncio
async def test_thread_owned_by_other_agent_is_ignored(config: Config) -> None:
    """An agent must not hijack an untagged thread owned by a different agent."""
    policy = _policy_for(config, "general")
    room = _room_with_members(_SENDER, _entity_id(config, "general").full_id, _entity_id(config, "research").full_id)
    history = [
        make_visible_message(sender=_SENDER, body="please help"),
        make_visible_message(sender=_entity_id(config, "research").full_id, body="on it"),
    ]
    context = _context(thread_id="$thread:localhost", thread_history=history)

    plan = await _plan(policy, room, _dispatch(context, agent_name="general"))

    assert plan.kind == "ignore"


@pytest.mark.asyncio
async def test_thread_with_two_participating_agents_forms_implicit_team_for_owner(config: Config) -> None:
    """Two agents already in one thread re-form an implicit team owned by the lowest agent ID."""
    room = _room_with_members(_SENDER, _entity_id(config, "general").full_id, _entity_id(config, "research").full_id)
    history = [
        make_visible_message(sender=_SENDER, body="please help"),
        make_visible_message(sender=_entity_id(config, "general").full_id, body="general here"),
        make_visible_message(sender=_entity_id(config, "research").full_id, body="research here"),
    ]
    context = _context(thread_id="$thread:localhost", thread_history=history)

    owner_plan = await _plan(
        _policy_for(config, "general"),
        room,
        _dispatch(context, agent_name="general"),
    )
    other_plan = await _plan(
        _policy_for(config, "research"),
        room,
        _dispatch(context, agent_name="research"),
    )

    assert owner_plan.kind == "respond"
    assert owner_plan.response_action.kind == "team"
    assert owner_plan.response_action.form_team.outcome is TeamOutcome.TEAM
    assert owner_plan.response_action.form_team.intent is TeamIntent.IMPLICIT_THREAD_TEAM
    # Formation is pure: thread-history teams carry the COLLABORATE provisional mode.
    assert owner_plan.response_action.form_team.mode is TeamMode.COLLABORATE
    assert other_plan.kind == "ignore"


@pytest.mark.asyncio
async def test_mentioning_two_agents_forms_explicit_team_for_owner_only(config: Config) -> None:
    """Tagging multiple agents forms one team surfaced by the lowest agent ID."""
    room = _room_with_members(_SENDER, _entity_id(config, "general").full_id, _entity_id(config, "research").full_id)
    mentioned = [_entity_id(config, "general"), _entity_id(config, "research")]

    owner_plan = await _plan(
        _policy_for(config, "general"),
        room,
        _dispatch(_context(mentioned=mentioned, am_i_mentioned=True), agent_name="general"),
    )
    other_plan = await _plan(
        _policy_for(config, "research"),
        room,
        _dispatch(_context(mentioned=mentioned, am_i_mentioned=True), agent_name="research"),
    )

    assert owner_plan.kind == "respond"
    assert owner_plan.response_action.kind == "team"
    assert owner_plan.response_action.form_team.intent is TeamIntent.EXPLICIT_MEMBERS
    # Formation is pure: explicitly tagged teams carry the COORDINATE provisional mode.
    assert owner_plan.response_action.form_team.mode is TeamMode.COORDINATE
    assert other_plan.kind == "ignore"


@pytest.mark.asyncio
async def test_dm_room_with_multiple_agents_forms_auto_team(config: Config) -> None:
    """Untagged DM-room messages auto-team all visible agents."""
    room = _room_with_members(_SENDER, _entity_id(config, "general").full_id, _entity_id(config, "research").full_id)

    plan = await _plan(
        _policy_for(config, "general"),
        room,
        _dispatch(_context(), agent_name="general"),
        is_dm=True,
    )

    assert plan.kind == "respond"
    assert plan.response_action.kind == "team"
    assert plan.response_action.form_team.intent is TeamIntent.DM_AUTO_TEAM
    assert plan.response_action.form_team.mode is TeamMode.COLLABORATE


@pytest.mark.asyncio
async def test_unauthorized_sender_is_ignored_even_when_mentioned(tmp_path: Path) -> None:
    """A sender outside the per-agent reply allowlist never gets a response."""
    config = bind_runtime_paths(
        Config(
            agents={"general": AgentConfig(display_name="General")},
            authorization=AuthorizationConfig(agent_reply_permissions={"general": ["@owner:localhost"]}),
        ),
        test_runtime_paths(tmp_path),
    )
    policy = _policy_for(config, "general")
    room = _room_with_members(_SENDER, _entity_id(config, "general").full_id)
    context = _context(mentioned=[_entity_id(config, "general")], am_i_mentioned=True)

    plan = await _plan(policy, room, _dispatch(context, agent_name="general"))

    assert plan.kind == "ignore"
    assert policy.can_reply_to_sender(_SENDER) is False


@pytest.mark.asyncio
async def test_internal_agent_sender_bypasses_reply_allowlist(tmp_path: Path) -> None:
    """Bot-to-bot senders are system participants and bypass per-agent reply allowlists."""
    config = bind_runtime_paths(
        Config(
            agents={
                "general": AgentConfig(display_name="General"),
                "research": AgentConfig(display_name="Research"),
            },
            authorization=AuthorizationConfig(agent_reply_permissions={"general": ["@owner:localhost"]}),
        ),
        test_runtime_paths(tmp_path),
    )
    policy = _policy_for(config, "general")

    assert policy.can_reply_to_sender(_entity_id(config, "research").full_id) is True


@pytest.mark.asyncio
async def test_unavailable_thread_history_skips_with_multiple_responders(config: Config) -> None:
    """Degraded thread policy history must not be treated as an empty thread."""
    policy = _policy_for(config, "general")
    room = _room_with_members(_SENDER, _entity_id(config, "general").full_id, _entity_id(config, "research").full_id)
    # A plain non-empty sequence carries no completeness signal, so planning history is unavailable.
    context = _context(
        thread_id="$thread:localhost",
        thread_history=[make_visible_message(sender=_SENDER, body="earlier")],
        full_history=False,
    )
    assert context.planning_thread_history_unavailable

    plan = await _plan(policy, room, _dispatch(context, agent_name="general"))

    assert plan.kind == "ignore"


@pytest.mark.asyncio
async def test_unavailable_thread_history_still_responds_as_single_visible_responder(config: Config) -> None:
    """The only visible responder continues a thread even when policy history degraded."""
    policy = _policy_for(config, "general")
    room = _room_with_members(_SENDER, _entity_id(config, "general").full_id)
    context = _context(
        thread_id="$thread:localhost",
        thread_history=[make_visible_message(sender=_SENDER, body="earlier")],
        full_history=False,
    )

    plan = await _plan(policy, room, _dispatch(context, agent_name="general"))

    assert plan.kind == "respond"
    assert plan.response_action.kind == "individual"


@pytest.mark.asyncio
async def test_active_response_queues_untagged_thread_follow_up(config: Config) -> None:
    """A human follow-up in a thread with an in-flight response enters the queued path."""
    policy = _policy_for(config, "general")
    room = _room_with_members(_SENDER, _entity_id(config, "general").full_id, _entity_id(config, "research").full_id)
    history = [
        make_visible_message(sender=_SENDER, body="please help"),
        make_visible_message(sender=_entity_id(config, "research").full_id, body="on it"),
    ]
    context = _context(thread_id="$thread:localhost", thread_history=history)

    plan = await _plan(
        policy,
        room,
        _dispatch(context, agent_name="general"),
        has_active_response=True,
    )

    assert plan.kind == "respond"
    assert plan.response_action.kind == "individual"


@pytest.mark.asyncio
async def test_router_routes_untagged_message_with_multiple_candidates(config: Config) -> None:
    """The router produces a route plan when several responders could answer."""
    policy = _policy_for(config, ROUTER_AGENT_NAME)
    room = _room_with_members(_SENDER, _entity_id(config, "general").full_id, _entity_id(config, "research").full_id)

    plan = await _plan(policy, room, _dispatch(_context(), agent_name=ROUTER_AGENT_NAME))

    assert plan.kind == "route"
    assert plan.router_event is not None


@pytest.mark.asyncio
async def test_router_ignores_untagged_message_with_single_candidate(config: Config) -> None:
    """The router stays out of the way when only one responder candidate exists."""
    policy = _policy_for(config, ROUTER_AGENT_NAME)
    room = _room_with_members(_SENDER, _entity_id(config, "general").full_id)

    plan = await _plan(policy, room, _dispatch(_context(), agent_name=ROUTER_AGENT_NAME))

    assert plan.kind == "ignore"
    assert plan.ignore_reason == "router"


@pytest.mark.asyncio
async def test_router_ignores_message_with_explicit_agent_mention(config: Config) -> None:
    """Explicitly tagged messages bypass routing entirely."""
    policy = _policy_for(config, ROUTER_AGENT_NAME)
    room = _room_with_members(_SENDER, _entity_id(config, "general").full_id, _entity_id(config, "research").full_id)
    context = _context(mentioned=[_entity_id(config, "general")])

    plan = await _plan(policy, room, _dispatch(context, agent_name=ROUTER_AGENT_NAME))

    assert plan.kind == "ignore"
    assert plan.ignore_reason == "router"


@pytest.mark.asyncio
async def test_router_only_mention_returns_rules_of_engagement_rejection(config: Config) -> None:
    """Tagging only the router yields the guidance rejection instead of routing."""
    policy = _policy_for(config, ROUTER_AGENT_NAME)
    room = _room_with_members(_SENDER, _entity_id(config, "general").full_id, _entity_id(config, "research").full_id)
    context = _context(mentioned=[_entity_id(config, ROUTER_AGENT_NAME)])

    plan = await _plan(policy, room, _dispatch(context, agent_name=ROUTER_AGENT_NAME))

    assert plan.kind == "respond"
    assert plan.response_action.kind == "reject"
    assert "Rules of engagement" in plan.response_action.rejection_message


@pytest.mark.asyncio
async def test_router_ignores_thread_already_owned_by_an_agent(config: Config) -> None:
    """The router never re-routes a thread that already has a visible agent owner."""
    policy = _policy_for(config, ROUTER_AGENT_NAME)
    room = _room_with_members(_SENDER, _entity_id(config, "general").full_id, _entity_id(config, "research").full_id)
    history = [
        make_visible_message(sender=_SENDER, body="please help"),
        make_visible_message(sender=_entity_id(config, "general").full_id, body="answering"),
    ]
    context = _context(thread_id="$thread:localhost", thread_history=history)

    plan = await _plan(policy, room, _dispatch(context, agent_name=ROUTER_AGENT_NAME))

    assert plan.kind == "ignore"
    assert plan.ignore_reason == "router"


@pytest.mark.asyncio
async def test_router_ignores_thread_with_unavailable_policy_history(config: Config) -> None:
    """Routing is skipped rather than guessed when thread policy history degraded."""
    policy = _policy_for(config, ROUTER_AGENT_NAME)
    room = _room_with_members(_SENDER, _entity_id(config, "general").full_id, _entity_id(config, "research").full_id)
    context = _context(
        thread_id="$thread:localhost",
        thread_history=[make_visible_message(sender=_SENDER, body="earlier")],
        full_history=False,
    )

    plan = await _plan(policy, room, _dispatch(context, agent_name=ROUTER_AGENT_NAME))

    assert plan.kind == "ignore"
    assert plan.ignore_reason == "router"


def test_prepared_dispatch_rejects_mismatched_envelope_target() -> None:
    """PreparedDispatch enforces that envelope and dispatch describe the same delivery."""
    target = MessageTarget.resolve(_ROOM_ID, None, _EVENT_ID)
    other_target = MessageTarget.resolve(_ROOM_ID, "$thread:localhost", _EVENT_ID)
    envelope = request_envelope(
        room_id=_ROOM_ID,
        reply_to_event_id=_EVENT_ID,
        target=other_target,
        agent_name="general",
    )

    with pytest.raises(ValueError, match="must match the resolved dispatch target"):
        PreparedDispatch(
            requester_user_id=_SENDER,
            context=_context(),
            target=target,
            correlation_id="corr-test",
            envelope=envelope,
        )
