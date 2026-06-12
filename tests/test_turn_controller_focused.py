"""Focused unit tests for TurnController built from its constructor-injected collaborators.

These tests construct ``TurnController`` directly from ``TurnControllerDeps`` —
no ``AgentBot``/orchestrator boot — with real ``TurnPolicy``, ``ConversationResolver``,
``TurnStore`` (disk-backed ledger), and ``CoalescingGate`` collaborators, and typed
recording fakes only at the execution and delivery seams (``ResponseRunner``,
``DeliveryGateway``). They pin the turn contract from
``docs/architecture/bot-runtime.md``: precheck filtering (via ``IngressValidator``),
replay-guard rejection, policy decision routing, ingress-level command dispatch
with terminal TurnStore outcomes (commands are control inputs that never enter
the coalescing gate), durable dedup across restarts, and the controller-owned
interactive selection path.
"""

from __future__ import annotations

import asyncio
from contextlib import asynccontextmanager
from dataclasses import dataclass, field, fields
from typing import TYPE_CHECKING, Any, cast

import nio
import pytest

from mindroom import constants, interactive
from mindroom.bot_runtime_view import BotRuntimeState
from mindroom.coalescing import CoalescingGate
from mindroom.config.agent import AgentConfig
from mindroom.config.auth import AuthorizationConfig
from mindroom.config.main import Config
from mindroom.constants import ROUTER_AGENT_NAME
from mindroom.conversation_resolver import ConversationResolver, ConversationResolverDeps
from mindroom.conversation_state_writer import ConversationStateWriter, ConversationStateWriterDeps
from mindroom.entity_resolution import entity_identity_registry
from mindroom.hooks import HookContextSupport, HookRegistry, HookRegistryState
from mindroom.inbound_turn_normalizer import InboundTurnNormalizer, InboundTurnNormalizerDeps
from mindroom.ingress_validation import IngressValidator, IngressValidatorDeps
from mindroom.logging_config import get_logger
from mindroom.matrix.cache.thread_history_result import thread_history_result
from mindroom.response_payload_preparation import DispatchPayloadInputs, ResponsePayloadPreparation
from mindroom.response_runner import ResponseRequest
from mindroom.tool_system.runtime_context import ToolRuntimeSupport
from mindroom.turn_controller import TurnController, TurnControllerDeps
from mindroom.turn_policy import IngressHookRunner, TurnPolicy, TurnPolicyDeps
from mindroom.turn_store import TurnStore, TurnStoreDeps
from tests.conftest import (
    bind_runtime_paths,
    make_conversation_cache_mock,
    make_matrix_client_mock,
    make_visible_message,
    runtime_paths_for,
    test_runtime_paths,
)

if TYPE_CHECKING:
    from collections.abc import AsyncIterator, Coroutine
    from pathlib import Path
    from unittest.mock import AsyncMock

    from mindroom.coalescing_batch import CoalescedBatch
    from mindroom.delivery_gateway import DeliveryGateway, SendTextRequest
    from mindroom.hooks import MessageEnvelope
    from mindroom.matrix.cache import ThreadHistoryResult
    from mindroom.matrix.event_info import EventInfo
    from mindroom.message_target import MessageTarget
    from mindroom.response_lifecycle import QueuedHumanNoticeReservation
    from mindroom.response_runner import ResponseRunner
    from mindroom.turn_policy import ResponseAction
    from mindroom.turn_policy import _ResponderAvailability as ResponderAvailability

_ROOM_ID = "!focused:localhost"
_SENDER = "@user:localhost"
_EVENT_ID = "$event:localhost"
_THREAD_ROOT = "$root:localhost"


@dataclass
class _RecordingResponseRunner:
    """Typed ResponseRunner stand-in that records the execution-seam requests.

    Mirrors the two runner seams the controller depends on: the request entry
    points (``generate_response`` / ``generate_team_response_helper``) and the
    runner-owned inbox-task ownership (``track_inbox_response``) added by the
    coalescing redesign, where the dispatch handoff completes once the request
    signals ``on_lifecycle_lock_acquired`` and the response keeps running on a
    runner-owned task.
    """

    response_event_id: str | None = "$response:localhost"
    requests: list[ResponseRequest] = field(default_factory=list)
    team_requests: list[ResponseRequest] = field(default_factory=list)
    inbox_tasks: list[asyncio.Task[None]] = field(default_factory=list)

    def active_thread_ids_for_room(self, room_id: str) -> frozenset[str | None]:  # noqa: ARG002
        return frozenset()

    def has_active_response_for_target(self, target: MessageTarget) -> bool:  # noqa: ARG002
        return False

    def reserve_waiting_human_message(
        self,
        *,
        target: MessageTarget,  # noqa: ARG002
        response_envelope: MessageEnvelope,  # noqa: ARG002
    ) -> QueuedHumanNoticeReservation:
        msg = "Queued-notice reservations are not part of these focused turn tests"
        raise AssertionError(msg)

    def track_inbox_response(self, response: Coroutine[Any, Any, None], *, name: str) -> asyncio.Task[None]:
        task = asyncio.get_running_loop().create_task(response, name=name)
        self.inbox_tasks.append(task)
        return task

    async def settle_inbox_responses(self) -> None:
        """Await every runner-owned response task, surfacing its failures."""
        for task in self.inbox_tasks:
            await task

    async def generate_response(self, request: ResponseRequest) -> str | None:
        self.requests.append(request)
        if request.on_lifecycle_lock_acquired is not None:
            request.on_lifecycle_lock_acquired()
        return self.response_event_id

    async def generate_team_response_helper(
        self,
        request: ResponseRequest,
        *,
        team_agents: object,  # noqa: ARG002
        team_mode: str,  # noqa: ARG002
    ) -> str | None:
        self.team_requests.append(request)
        if request.on_lifecycle_lock_acquired is not None:
            request.on_lifecycle_lock_acquired()
        return self.response_event_id


@dataclass
class _RecordingDeliveryGateway:
    """Typed DeliveryGateway stand-in that records visible sends."""

    sent: list[SendTextRequest] = field(default_factory=list)

    async def send_text(self, request: SendTextRequest) -> str | None:
        self.sent.append(request)
        return f"$sent-{len(self.sent)}:localhost"


@dataclass
class _SpyTurnPolicy:
    """Delegating TurnPolicy wrapper that counts policy evaluations."""

    inner: TurnPolicy
    plan_turn_calls: int = 0

    def can_reply_to_sender(self, sender_id: str) -> bool:
        return self.inner.can_reply_to_sender(sender_id)

    def responder_availability(self) -> ResponderAvailability:
        return self.inner.responder_availability()

    async def responder_candidates_for_room(
        self,
        room: nio.MatrixRoom,
        requester_user_id: str,
        availability: ResponderAvailability,
    ) -> list[object]:
        return list(await self.inner.responder_candidates_for_room(room, requester_user_id, availability))

    def effective_response_action(self, action: ResponseAction) -> ResponseAction:
        return self.inner.effective_response_action(action)

    async def plan_turn(self, *args: Any, **kwargs: Any) -> Any:  # noqa: ANN401
        self.plan_turn_calls += 1
        return await self.inner.plan_turn(*args, **kwargs)


async def _unused_hook_send_message(*_args: object, **_kwargs: object) -> str | None:
    """Hook message sending must not happen in these tests (the hook registry is empty)."""
    msg = "hook_send_message must not be called in focused turn-controller tests"
    raise AssertionError(msg)


class _UnusedEditRegenerator:
    """Edit-regeneration stub: the edit path must not run in these tests."""

    async def handle_message_edit(
        self,
        room: nio.MatrixRoom,  # noqa: ARG002
        event: nio.RoomMessageText,  # noqa: ARG002
        event_info: EventInfo,  # noqa: ARG002
        requester_user_id: str,  # noqa: ARG002
    ) -> None:
        msg = "Edit regeneration must not run in focused turn-controller tests"
        raise AssertionError(msg)


@dataclass
class _Harness:
    """One directly constructed TurnController plus its observable seams."""

    controller: TurnController
    policy: _SpyTurnPolicy
    runner: _RecordingResponseRunner
    gateway: _RecordingDeliveryGateway
    turn_store: TurnStore
    gate: CoalescingGate
    gate_batches: list[CoalescedBatch]
    conversation_cache: AsyncMock

    async def deliver(self, room: nio.MatrixRoom, event: nio.RoomMessageText) -> None:
        """Run one inbound text turn end-to-end, settling runner-owned responses.

        Conversational responses keep running on runner-owned inbox tasks after
        the gate handoff completes, so assertions must wait for those tasks too.
        """
        await self.controller.handle_text_event(room, event)
        await self.gate.drain_all()
        await self.runner.settle_inbox_responses()


def _build_harness(
    config: Config,
    storage_path: Path,
    *,
    agent_name: str = "general",
    thread_history: ThreadHistoryResult | None = None,
) -> _Harness:
    """Construct a TurnController from typed collaborators without booting a bot."""
    runtime_paths = runtime_paths_for(config)
    registry = entity_identity_registry(config, runtime_paths)
    matrix_id = registry.current_id(agent_name)
    logger = get_logger("test_turn_controller_focused")
    runtime = BotRuntimeState(
        client=make_matrix_client_mock(user_id=matrix_id.full_id),
        config=config,
        runtime_paths=runtime_paths,
        enable_streaming=True,
        orchestrator=None,
        event_cache=None,
        event_cache_write_coordinator=None,
    )
    conversation_cache = make_conversation_cache_mock()

    @asynccontextmanager
    async def _turn_scope() -> AsyncIterator[None]:
        yield

    conversation_cache.turn_scope = _turn_scope
    if thread_history is not None:
        conversation_cache.get_dispatch_thread_history.return_value = thread_history
    resolver = ConversationResolver(
        ConversationResolverDeps(
            runtime=runtime,
            logger=logger,
            runtime_paths=runtime_paths,
            agent_name=agent_name,
            matrix_id=matrix_id,
            conversation_cache=conversation_cache,
        ),
    )
    normalizer = InboundTurnNormalizer(
        InboundTurnNormalizerDeps(
            runtime=runtime,
            logger=logger,
            storage_path=storage_path,
            runtime_paths=runtime_paths,
        ),
    )
    state_writer = ConversationStateWriter(
        ConversationStateWriterDeps(
            runtime=runtime,
            logger=logger,
            runtime_paths=runtime_paths,
            agent_name=agent_name,
        ),
    )
    hook_context = HookContextSupport(
        runtime=runtime,
        logger=logger,
        runtime_paths=runtime_paths,
        agent_name=agent_name,
        hook_registry_state=HookRegistryState(HookRegistry.empty()),
        hook_send_message=_unused_hook_send_message,
    )
    tool_runtime = ToolRuntimeSupport(
        runtime=runtime,
        logger=logger,
        runtime_paths=runtime_paths,
        storage_path=storage_path,
        agent_name=agent_name,
        matrix_id=matrix_id,
        resolver=resolver,
        hook_context=hook_context,
    )
    turn_store = TurnStore(
        TurnStoreDeps(
            agent_name=agent_name,
            tracking_base_path=storage_path / "tracking",
            state_writer=state_writer,
            resolver=resolver,
            tool_runtime=tool_runtime,
        ),
    )
    policy = _SpyTurnPolicy(
        TurnPolicy(
            TurnPolicyDeps(
                runtime=runtime,
                logger=logger,
                runtime_paths=runtime_paths,
                agent_name=agent_name,
                matrix_id=matrix_id,
            ),
        ),
    )
    runner = _RecordingResponseRunner()
    gateway = _RecordingDeliveryGateway()
    controller_ref: list[TurnController] = []
    gate_batches: list[CoalescedBatch] = []

    async def _dispatch_batch(batch: CoalescedBatch) -> None:
        gate_batches.append(batch)
        await controller_ref[0].handle_coalesced_batch(batch)

    gate = CoalescingGate(
        dispatch_batch=_dispatch_batch,
        debounce_seconds=lambda: 0.0,
        is_shutting_down=lambda: False,
    )
    ingress_validator = IngressValidator(
        IngressValidatorDeps(
            runtime=runtime,
            runtime_paths=runtime_paths,
            matrix_id=matrix_id,
            turn_store=turn_store,
            turn_policy=policy,
        ),
    )
    controller = TurnController(
        TurnControllerDeps(
            runtime=runtime,
            logger=logger,
            runtime_paths=runtime_paths,
            agent_name=agent_name,
            matrix_id=matrix_id,
            conversation_cache=conversation_cache,
            resolver=resolver,
            normalizer=normalizer,
            turn_policy=cast("TurnPolicy", policy),
            ingress_hook_runner=IngressHookRunner(hook_context=hook_context),
            response_runner=cast("ResponseRunner", runner),
            delivery_gateway=cast("DeliveryGateway", gateway),
            tool_runtime=tool_runtime,
            turn_store=turn_store,
            coalescing_gate=gate,
            edit_regenerator=_UnusedEditRegenerator(),
            ingress=ingress_validator,
        ),
    )
    controller_ref.append(controller)
    return _Harness(
        controller=controller,
        policy=policy,
        runner=runner,
        gateway=gateway,
        turn_store=turn_store,
        gate=gate,
        gate_batches=gate_batches,
        conversation_cache=conversation_cache,
    )


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
        test_runtime_paths(tmp_path / "runtime"),
    )


def _entity_user_id(config: Config, entity_name: str) -> str:
    return entity_identity_registry(config, runtime_paths_for(config)).current_id(entity_name).full_id


def _room_with_members(config: Config, *entity_names: str, room_id: str = _ROOM_ID) -> nio.MatrixRoom:
    room = nio.MatrixRoom(room_id, _entity_user_id(config, entity_names[0]) if entity_names else _SENDER)
    room.add_member(_SENDER, _SENDER, None)
    for entity_name in entity_names:
        user_id = _entity_user_id(config, entity_name)
        room.add_member(user_id, user_id, None)
    return room


def _text_event(
    body: str,
    *,
    event_id: str = _EVENT_ID,
    thread_id: str | None = None,
    origin_server_ts: int = 1_000_000,
) -> nio.RoomMessageText:
    content: dict[str, Any] = {"body": body, "msgtype": "m.text"}
    if thread_id is not None:
        content["m.relates_to"] = {"rel_type": "m.thread", "event_id": thread_id}
    return nio.RoomMessageText.from_dict(
        {
            "content": content,
            "event_id": event_id,
            "sender": _SENDER,
            "origin_server_ts": origin_server_ts,
            "room_id": _ROOM_ID,
            "type": "m.room.message",
        },
    )


@pytest.mark.asyncio
async def test_replayed_turn_is_rejected_before_policy_and_recorded(config: Config, tmp_path: Path) -> None:
    """A turn superseded by a newer unresponded requester message never reaches policy.

    The dispatch replay guard must consume the turn (recording it as handled) without
    evaluating the turn plan, invoking the response runner, or sending anything.
    """
    newer_history = thread_history_result(
        [
            make_visible_message(
                sender=_SENDER,
                body="newer follow-up from the same requester",
                event_id="$newer:localhost",
                timestamp=2_000_000,
            ),
        ],
        is_full_history=True,
    )
    harness = _build_harness(config, tmp_path, thread_history=newer_history)
    room = _room_with_members(config, "general")
    event = _text_event("older superseded message", thread_id=_THREAD_ROOT, origin_server_ts=1_000_000)

    await harness.deliver(room, event)

    assert harness.policy.plan_turn_calls == 0
    assert harness.runner.requests == []
    assert harness.gateway.sent == []
    assert harness.turn_store.is_handled(event.event_id) is True


@pytest.mark.asyncio
async def test_sender_outside_reply_allowlist_is_dropped_at_precheck(tmp_path: Path) -> None:
    """An unauthorized sender is filtered at precheck and the turn gets a terminal record."""
    config = bind_runtime_paths(
        Config(
            agents={"general": AgentConfig(display_name="General")},
            authorization=AuthorizationConfig(agent_reply_permissions={"general": ["@owner:localhost"]}),
        ),
        test_runtime_paths(tmp_path / "runtime"),
    )
    harness = _build_harness(config, tmp_path)
    room = _room_with_members(config, "general")
    event = _text_event("hello from a stranger")

    await harness.deliver(room, event)

    assert harness.policy.plan_turn_calls == 0
    assert harness.runner.requests == []
    assert harness.gateway.sent == []
    # The drop is terminal: the turn is recorded so restarts never resurrect it.
    assert harness.turn_store.is_handled(event.event_id) is True


@pytest.mark.asyncio
async def test_policy_ignore_sends_nothing_and_leaves_turn_unhandled(config: Config, tmp_path: Path) -> None:
    """An ignore plan produces no response and no terminal record for a plain agent.

    Leaving the turn unrecorded is the contract: another agent in the room still
    owns the response, so this agent must not claim the turn in its ledger.
    """
    harness = _build_harness(config, tmp_path)
    room = _room_with_members(config, "general", "research")
    event = _text_event("untagged message with multiple visible responders")

    await harness.deliver(room, event)

    assert harness.policy.plan_turn_calls == 1
    assert harness.runner.requests == []
    assert harness.gateway.sent == []
    assert harness.turn_store.is_handled(event.event_id) is False


@pytest.mark.asyncio
async def test_policy_respond_crosses_seam_as_immutable_values(config: Config, tmp_path: Path) -> None:
    """A respond plan invokes the runner once with value-only ResponseRequest inputs.

    Pins the one-way ingress→execution seam: the request carries an immutable
    ``ResponsePayloadPreparation`` value (no callbacks back into the controller),
    and the recorded outcome lands in the durable turn store.
    """
    harness = _build_harness(config, tmp_path)
    room = _room_with_members(config, "general")
    event = _text_event("please summarize the build failure")

    await harness.deliver(room, event)

    # Conversational turns flow through the coalescing gate, unlike commands.
    assert len(harness.gate_batches) == 1
    assert harness.policy.plan_turn_calls == 1
    assert len(harness.runner.requests) == 1
    request = harness.runner.requests[0]

    assert request.prompt == "please summarize the build failure"
    assert request.user_id == _SENDER
    assert request.correlation_id == event.event_id
    assert request.response_envelope.requester_id == _SENDER
    assert request.response_envelope.target.room_id == _ROOM_ID
    # A rootable room-level message becomes its own thread root.
    assert request.response_envelope.target.resolved_thread_id == event.event_id

    preparation = request.payload_preparation
    assert isinstance(preparation, ResponsePayloadPreparation)
    assert preparation.prompt == "please summarize the build failure"
    assert preparation.action_kind == "individual"
    assert preparation.target_member_names is None
    assert preparation.payload_inputs == DispatchPayloadInputs(
        message_attachment_ids=(),
        trusted_attachment_ids=(),
        media_events=(),
    )
    # The envelope crosses the seam as the same value the dispatch was planned with.
    assert request.response_envelope is preparation.dispatch.envelope

    # Payload data crosses the seam as values, not closures: the old
    # prepare-after-lock callback is gone and nothing callable rides inside the
    # preparation value. The only callback on the request is the redesign's
    # inbox handoff signal, fired by the runner when it takes the lifecycle lock.
    request_field_names = {request_field.name for request_field in fields(ResponseRequest)}
    assert "prepare_after_lock" not in request_field_names
    assert request.on_lifecycle_lock_acquired is not None
    assert not any(callable(getattr(preparation, preparation_field.name)) for preparation_field in fields(preparation))

    metadata = request.matrix_run_metadata
    assert metadata is not None
    assert metadata[constants.MATRIX_RESPONSE_OWNER_METADATA_KEY] == "general"
    assert harness.turn_store.is_handled(event.event_id) is True


@pytest.mark.asyncio
async def test_command_turn_records_terminal_outcome_through_turn_store(config: Config, tmp_path: Path) -> None:
    """A ``!command`` turn executes on the router and records a terminal TurnStore outcome.

    Since the coalescing redesign, command-shaped human text is a control input:
    it dispatches directly at ingress and must never enter the coalescing gate.
    """
    harness = _build_harness(config, tmp_path, agent_name=ROUTER_AGENT_NAME)
    room = _room_with_members(config, ROUTER_AGENT_NAME, "general", "research")
    event = _text_event("!help", event_id="$command:localhost")

    await harness.deliver(room, event)

    # Commands bypass turn-policy planning, AI execution, and the coalescing gate.
    assert harness.policy.plan_turn_calls == 0
    assert harness.runner.requests == []
    assert harness.gate_batches == []
    assert len(harness.gateway.sent) == 1
    assert "command" in harness.gateway.sent[0].response_text.lower()
    assert harness.turn_store.is_handled(event.event_id) is True

    record = harness.turn_store.get_turn_record(event.event_id)
    assert record is not None
    assert record.response_event_id == "$sent-1:localhost"


@pytest.mark.asyncio
async def test_non_router_agent_consumes_command_without_responding(config: Config, tmp_path: Path) -> None:
    """Plain agents stay silent on command turns; the router owns command replies."""
    harness = _build_harness(config, tmp_path, agent_name="general")
    room = _room_with_members(config, "general")
    event = _text_event("!help", event_id="$command:localhost")

    await harness.deliver(room, event)

    assert harness.policy.plan_turn_calls == 0
    assert harness.runner.requests == []
    assert harness.gateway.sent == []
    # Commands are control inputs: even a silently consumed one never enters the gate.
    assert harness.gate_batches == []
    # The non-router agent does not claim the command turn; the router owns its
    # terminal record, so this agent's ledger must stay untouched.
    assert harness.turn_store.is_handled(event.event_id) is False


@pytest.mark.asyncio
async def test_same_turn_after_restart_produces_exactly_one_response(config: Config, tmp_path: Path) -> None:
    """Replaying one handled turn through a fresh controller never responds twice.

    The second harness shares only the on-disk handled-turn ledger, simulating a
    process restart that re-delivers the same Matrix event.
    """
    room = _room_with_members(config, "general")
    event = _text_event("respond exactly once")

    first_run = _build_harness(config, tmp_path)
    await first_run.deliver(room, event)
    assert len(first_run.runner.requests) == 1
    assert first_run.turn_store.is_handled(event.event_id) is True

    restarted_run = _build_harness(config, tmp_path)
    await restarted_run.deliver(room, _text_event("respond exactly once"))

    assert restarted_run.runner.requests == []
    assert restarted_run.gateway.sent == []
    assert restarted_run.policy.plan_turn_calls == 0
    assert restarted_run.turn_store.is_handled(event.event_id) is True


@pytest.mark.asyncio
async def test_interactive_selection_acks_generates_and_records_once(config: Config, tmp_path: Path) -> None:
    """The controller-owned selection path acks, runs generation, and records the turn."""
    harness = _build_harness(config, tmp_path)
    room = nio.MatrixRoom(_ROOM_ID, _entity_user_id(config, "general"))
    selection = interactive.InteractiveSelection(
        question_event_id="$question:localhost",
        question_text="Which option should I use?",
        selection_key="1",
        selected_label="Option 1",
        selected_value="Option 1",
        thread_id="$thread-root:localhost",
    )

    await harness.controller.handle_interactive_selection(
        room,
        selection=selection,
        user_id=_SENDER,
        source_event_id="$selection:localhost",
    )

    assert len(harness.gateway.sent) == 1
    ack_request = harness.gateway.sent[0]
    assert ack_request.response_text.startswith("You selected: 1 Option 1")
    assert ack_request.target.resolved_thread_id == selection.thread_id
    assert ack_request.target.reply_to_event_id is None

    assert len(harness.runner.requests) == 1
    request = harness.runner.requests[0]
    assert request.prompt == interactive.build_selection_prompt(selection)
    assert request.existing_event_id == "$sent-1:localhost"
    assert request.existing_event_is_placeholder is True
    assert request.response_envelope.target.reply_to_event_id == selection.question_event_id
    assert request.response_envelope.target.resolved_thread_id == selection.thread_id
    metadata = request.matrix_run_metadata
    assert metadata is not None
    assert metadata[constants.MATRIX_SOURCE_EVENT_IDS_METADATA_KEY] == ["$selection:localhost"]

    assert harness.turn_store.is_handled(selection.question_event_id) is True
    assert harness.turn_store.is_handled("$selection:localhost") is True


@pytest.mark.asyncio
async def test_interactive_selection_without_response_stays_retryable(config: Config, tmp_path: Path) -> None:
    """A selection whose generation yields no visible response must not be marked handled."""
    harness = _build_harness(config, tmp_path)
    harness.runner.response_event_id = None
    room = nio.MatrixRoom(_ROOM_ID, _entity_user_id(config, "general"))
    selection = interactive.InteractiveSelection(
        question_event_id="$question:localhost",
        question_text="Which option should I use?",
        selection_key="2",
        selected_label="Option 2",
        selected_value="Option 2",
        thread_id="$thread-root:localhost",
    )

    await harness.controller.handle_interactive_selection(
        room,
        selection=selection,
        user_id=_SENDER,
        source_event_id="$selection:localhost",
    )

    assert len(harness.runner.requests) == 1
    assert harness.turn_store.is_handled(selection.question_event_id) is False
    assert harness.turn_store.is_handled("$selection:localhost") is False
