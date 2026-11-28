"""Shared test harness helpers for streaming ``io.mindroom.ai_run`` metadata tests."""

from __future__ import annotations

import asyncio
from contextlib import asynccontextmanager, contextmanager
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Any
from unittest.mock import AsyncMock, MagicMock

import nio

from mindroom.ai_run_metadata import model_payload
from mindroom.approval.tool_approval import shutdown_approval_runtime
from mindroom.config.agent import AgentConfig
from mindroom.config.main import Config
from mindroom.config.models import ModelConfig, ModelPricing
from mindroom.constants import AI_RUN_METADATA_KEY, STREAM_STATUS_KEY
from mindroom.event_journal import ApprovalDecisionMetadata, CallKey, EventClass, EventKind, InboundEvent
from mindroom.llm.messages import Message
from mindroom.llm.provider import ModelIdentity, TextDelta, TurnCompleted, UsageReported
from mindroom.llm.usage import MessageMetrics
from mindroom.matrix.users import AgentMatrixUser
from mindroom.matrix.visible_body import visible_content_from_content
from mindroom.providers.identity import model_identity
from mindroom.tool_system.authoring import Toolkit
from mindroom.turn_policy import ResponseAction
from mindroom.turn_record import TurnRecord
from tests.access_schema_support import with_current_room_member_access
from tests.bot_helpers import make_test_agent_bot
from tests.conftest import (
    TEST_PASSWORD,
    bind_runtime_paths,
    initialize_approval_store_for_test,
    install_model_providers,
    install_runtime_journal_support,
    make_matrix_client_mock,
    runtime_paths_for,
    test_runtime_paths,
    unwrap_extracted_collaborator,
    wrap_extracted_collaborators,
)
from tests.fakes.scripted_model import Await, ScriptedModel, ScriptStep
from tests.response_attempt_helpers import install_direct_response_admission
from tests.response_runner_helpers import _target
from tests.scripted_agent_helpers import registered_toolkit

if TYPE_CHECKING:
    from collections.abc import AsyncIterator, Callable, Coroutine, Iterator, Mapping, Sequence
    from pathlib import Path

    import pytest

    from mindroom.bot import AgentBot
    from mindroom.config.budgets import BudgetsConfig
    from mindroom.config.cold_cache import ColdCacheConfig
    from mindroom.event_journal import ApprovalContinuation
    from mindroom.response_runner import ResponseRunner

SOURCE = "$event"
ROOM = "!room:localhost"
THREAD = "$thread"
REQUESTER = "@user:localhost"
ECHO_TOOL = "streaming_model_meta_echo"


def gated_answer(
    prefix: str,
    gate: asyncio.Event,
    suffix: str,
    *,
    metrics: MessageMetrics | None = None,
) -> list[ScriptStep]:
    """Stream ``prefix``, pause until ``gate`` is set, then stream ``suffix`` and complete."""
    final = metrics or MessageMetrics()
    return [
        TextDelta(prefix),
        Await(gate.wait),
        TextDelta(suffix),
        UsageReported(final),
        TurnCompleted(message=Message(role="assistant", content=prefix + suffix, metrics=final), stop_reason="end"),
    ]


def visible_content(event_content: Mapping[str, Any]) -> dict[str, Any]:
    """Return the visible Matrix content, following an edit into ``m.new_content``."""
    return dict(visible_content_from_content(event_content))


def ai_run(event_content: Mapping[str, Any]) -> dict[str, Any] | None:
    """Return ``io.mindroom.ai_run`` from a Matrix message or edit payload."""
    visible = visible_content(event_content)
    payload = visible.get(AI_RUN_METADATA_KEY)
    return dict(payload) if isinstance(payload, dict) else None


def stream_status(event_content: Mapping[str, Any]) -> str | None:
    """Return ``io.mindroom.stream_status`` from a Matrix message or edit payload."""
    visible = visible_content(event_content)
    status = visible.get(STREAM_STATUS_KEY)
    return status if isinstance(status, str) else None


def assert_no_json_floats(value: object) -> None:
    """Matrix event JSON forbids floats; verify none appear anywhere in a payload."""
    assert not isinstance(value, float), f"Unexpected float in Matrix payload: {value!r}"
    if isinstance(value, dict):
        for item in value.values():
            assert_no_json_floats(item)
    elif isinstance(value, list | tuple):
        for item in value:
            assert_no_json_floats(item)


def assert_provisional_contract(payload: Mapping[str, Any] | None) -> Mapping[str, Any]:
    """Assert every forbidden terminal/diagnostics field is absent and no floats appear."""
    assert payload is not None
    assert payload["version"] == 1
    for forbidden in ("status", "run_id", "session_id", "error", "prepared_context", "compaction"):
        assert forbidden not in payload
    usage_payload = payload.get("usage")
    if isinstance(usage_payload, dict):
        assert "duration" not in usage_payload
    assert_no_json_floats(payload)
    return payload


def assert_provisional_ai_run(
    event_content: Mapping[str, Any],
    expected_model: Mapping[str, str],
    *,
    expected_tools: int | None = None,
) -> None:
    """Assert ``event_content`` carries provisional, status-less ``io.mindroom.ai_run`` for ``expected_model``."""
    payload = ai_run(event_content)
    expected: dict[str, Any] = {"version": 1, "model": dict(expected_model)}
    if expected_tools is not None:
        expected["tools"] = {"count": expected_tools}
    assert payload == expected
    assert "status" not in payload
    assert "run_id" not in payload
    assert "session_id" not in payload


def answered_model_payload(config: Config, model_name: str) -> dict[str, str]:
    """Return the model payload once a canonical provider identity has answered."""
    identity = model_identity(config.models[model_name])
    return model_payload(config, model_name, model_id=identity.id, provider=identity.provider)


@dataclass
class Homeserver:
    """In-memory Matrix server recording every ``m.room.message`` payload in send order."""

    events: dict[str, str] = field(default_factory=dict)
    writes: list[dict[str, Any]] = field(default_factory=list)

    async def room_send(
        self,
        *,
        room_id: str,
        message_type: str,
        content: dict[str, Any],
        tx_id: str | None = None,
        **_kwargs: object,
    ) -> nio.RoomSendResponse:
        """Record ``m.room.message`` writes and return a synthetic event ID."""
        event_id = self.events.setdefault(tx_id or f"anon-{len(self.events)}", f"$sent{len(self.events)}")
        if message_type == "m.room.message":
            self.writes.append(content)
        return nio.RoomSendResponse(event_id=event_id, room_id=room_id)


def streaming_metadata_config(
    tmp_path: Path,
    *,
    budgets: BudgetsConfig | None = None,
    cold_cache: ColdCacheConfig | None = None,
    cheap_model: str | None = None,
    tools: Sequence[str] = (),
    priced: bool = False,
) -> Config:
    """Build a test config with primary, large, cheap, writer, and checker models."""
    return bind_runtime_paths(
        with_current_room_member_access(
            Config(
                agents={
                    "general": AgentConfig(
                        display_name="General",
                        rooms=[ROOM],
                        model="default",
                        cheap_model=cheap_model,
                        cold_cache=cold_cache,
                        tools=list(tools),
                    ),
                    "writer": AgentConfig(display_name="Writer", role="Writes.", model="writer-model"),
                    "checker": AgentConfig(display_name="Checker", role="Checks.", model="checker-model"),
                },
                models=(
                    {
                        "default": ModelConfig(
                            provider="openai",
                            id="gpt-primary",
                            display_name="Primary",
                            context_window=100_000,
                            pricing=ModelPricing(input=2, output=8),
                        ),
                        "large": ModelConfig(
                            provider="anthropic",
                            id="claude-large",
                            display_name="Large",
                            context_window=200_000,
                            pricing=ModelPricing(input=5, output=25),
                        ),
                        "cheap": ModelConfig(provider="openai", id="gpt-mini", display_name="Mini"),
                        "writer-model": ModelConfig(
                            provider="openai",
                            id="writer-model",
                            context_window=64_000,
                            pricing=ModelPricing(input=1, output=4),
                        ),
                        "checker-model": ModelConfig(
                            provider="openai",
                            id="checker-model",
                            context_window=32_000,
                            pricing=ModelPricing(input=1, output=4),
                        ),
                    }
                    if priced
                    else {
                        "default": ModelConfig(provider="openai", id="gpt-primary", display_name="Primary"),
                        "large": ModelConfig(provider="anthropic", id="claude-large", display_name="Large"),
                        "cheap": ModelConfig(provider="openai", id="gpt-mini", display_name="Mini"),
                        "writer-model": ModelConfig(provider="openai", id="writer-model"),
                        "checker-model": ModelConfig(provider="openai", id="checker-model"),
                    }
                ),
                budgets=budgets,
                memory={"backend": "none"},
            ),
        ),
        test_runtime_paths(tmp_path),
    )


@dataclass
class TurnHarness:
    """Configured bot, response runner, homeserver spy, and scripted model providers."""

    bot: AgentBot
    runner: ResponseRunner
    homeserver: Homeserver
    models: dict[str, ScriptedModel]


async def setup_turn(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    *,
    scripts: Mapping[str, Sequence[Sequence[ScriptStep]]],
    streaming: bool = True,
    budgets: BudgetsConfig | None = None,
    cold_cache: ColdCacheConfig | None = None,
    cheap_model: str | None = None,
    tools: Sequence[str] = (),
    thread_id: str | None = None,
    priced: bool = False,
) -> TurnHarness:
    """Create a streaming-ready ``TurnHarness`` with scripted model providers."""
    config = streaming_metadata_config(
        tmp_path,
        budgets=budgets,
        cold_cache=cold_cache,
        cheap_model=cheap_model,
        tools=tools,
        priced=priced,
    )
    agent_user = AgentMatrixUser(
        agent_name="general",
        password=TEST_PASSWORD,
        display_name="General",
        user_id="@mindroom_general:localhost",
    )
    bot = make_test_agent_bot(agent_user, config, runtime_paths_for(config), rooms=[ROOM])
    install_direct_response_admission(bot)
    bot.client = make_matrix_client_mock(user_id="@mindroom_general:localhost")
    install_runtime_journal_support(bot)
    wrap_extracted_collaborators(bot)
    await bot._reply_runtime.start()
    target = _target(thread_id=thread_id)
    await bot.journal_principal().admit(
        InboundEvent(
            event_id=SOURCE,
            room_id=ROOM,
            thread_id=thread_id,
            kind=EventKind.MESSAGE,
            event_class=EventClass.ACTIONABLE,
            sender=REQUESTER,
            origin_server_ts=1,
            source={},
        ),
    )
    await bot._turn_store.record_pending_turn(
        bot._turn_store.attach_response_context(
            TurnRecord.create([SOURCE], requester_id=REQUESTER),
            history_scope=bot._turn_store.response_history_scope(ResponseAction(kind="individual")),
            conversation_target=target,
        ),
    )
    progress = bot.config.defaults.streaming
    progress.update_interval = 0.0
    progress.min_update_interval = 0.0
    progress.max_idle = 0.0
    models = {
        name: ScriptedModel(
            [list(steps) for steps in turns],
            identity=(
                model_identity(bot.config.models[name])
                if priced
                else ModelIdentity(
                    id=bot.config.models[name].id,
                    provider=bot.config.models[name].provider,
                )
            ),
        )
        for name, turns in scripts.items()
    }
    install_model_providers(monkeypatch, models)
    homeserver = Homeserver()
    bot.client.room_send = AsyncMock(side_effect=homeserver.room_send)
    presence = MagicMock(spec=nio.PresenceGetResponse)
    presence.presence = "online" if streaming else "offline"
    presence.last_active_ago = 0
    bot.client.get_presence = AsyncMock(return_value=presence)
    return TurnHarness(
        bot=bot,
        runner=unwrap_extracted_collaborator(bot._response_runner),
        homeserver=homeserver,
        models=models,
    )


@contextmanager
def register_echo_tool() -> Iterator[None]:
    """Register the lightweight echo toolkit used by streaming approval tests."""

    class _EchoTools(Toolkit):
        def __init__(self) -> None:
            super().__init__(name="echo_tools", tools=[self.echo])

        def echo(self, text: str) -> str:
            return f"echo:{text}"

    with registered_toolkit(ECHO_TOOL, _EchoTools):
        yield


def install_approval_store(turn: TurnHarness) -> None:
    """Initialize the approval store the turn's bot pauses into, with approval cards that deliver at once."""

    async def prepare_event(_room: str, _thread: str | None, content: dict[str, Any], _sender: str) -> dict[str, Any]:
        return content

    initialize_approval_store_for_test(
        turn.bot.runtime_paths,
        prepare_approval_event=prepare_event,
        send_approval_delivery=AsyncMock(return_value="$approval-card"),
        resolve_approval_delivery=AsyncMock(return_value=None),
        cards=turn.bot._journal_store.principal("router@shared"),
        sender_bot=lambda _entity_name, _room_id: object(),
        sender_device_id=AsyncMock(return_value="DEVICE"),
    )


async def wait_for_writes(
    turn: TurnHarness,
    predicate: Callable[[dict[str, Any]], bool],
    *,
    start: int = 0,
    attempts: int = 200,
) -> list[dict[str, Any]]:
    """Poll ``turn.homeserver.writes[start:]`` until at least one write satisfies ``predicate``."""
    for _ in range(attempts):
        matched = [w for w in turn.homeserver.writes[start:] if predicate(w)]
        if matched:
            return matched
        await asyncio.sleep(0.01)
    seen = [(stream_status(w), visible_content(w).get("body"), ai_run(w)) for w in turn.homeserver.writes[start:]]
    msg = f"Timed out waiting for expected Matrix write; seen={seen!r}"
    raise AssertionError(msg)


@asynccontextmanager
async def running_task[T](
    coro: Coroutine[Any, Any, T],
    *gates: asyncio.Event,
) -> AsyncIterator[asyncio.Task[T]]:
    """Run ``coro`` as a task and guarantee every gate is released and the task cleaned up on exit."""
    task = asyncio.create_task(coro)
    try:
        yield task
    finally:
        for gate in gates:
            gate.set()
        if not task.done():
            task.cancel()
            await asyncio.gather(task, return_exceptions=True)


async def approve_call(
    turn: TurnHarness,
    call_id: str,
    *,
    source_event_id: str = SOURCE,
) -> ApprovalContinuation:
    """Approve ``call_id`` on the pending approval for ``source_event_id`` and return the updated continuation."""
    approvals = turn.bot.journal_principal().approvals
    continuation = await approvals.approval_continuation_for_source(source_event_id)
    assert continuation is not None
    await approvals.resolve_approval_call(
        CallKey(continuation.approval_id, continuation.generation, call_id),
        requested_status="approved",
        reason=None,
        metadata=ApprovalDecisionMetadata(resolved_by=REQUESTER),
    )
    updated = await approvals.approval_continuation(continuation.approval_id)
    assert updated is not None
    return updated


async def stop_reply(turn: TurnHarness, receipt_order: int, *, source_event_id: str = SOURCE) -> None:
    """Finalize a durable user Stop on the reply created for ``source_event_id``."""
    reply = await turn.bot.journal_principal().replies.for_sources((source_event_id,))
    assert reply is not None
    assert reply.event_id is not None
    assert await turn.bot._user_stop_reconciler.finalize(reply.event_id, receipt_order, room_id=ROOM)


__all__ = [
    "ECHO_TOOL",
    "REQUESTER",
    "ROOM",
    "SOURCE",
    "THREAD",
    "Homeserver",
    "TurnHarness",
    "ai_run",
    "answered_model_payload",
    "approve_call",
    "assert_no_json_floats",
    "assert_provisional_ai_run",
    "assert_provisional_contract",
    "gated_answer",
    "install_approval_store",
    "register_echo_tool",
    "running_task",
    "setup_turn",
    "shutdown_approval_runtime",
    "stop_reply",
    "stream_status",
    "streaming_metadata_config",
    "visible_content",
    "wait_for_writes",
]
