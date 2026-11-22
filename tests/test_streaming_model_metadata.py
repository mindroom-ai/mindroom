"""Tests for early model metadata on placeholders, streaming edits, pauses, and terminal notes."""

from __future__ import annotations

import asyncio
import time
from dataclasses import dataclass, field, replace
from typing import TYPE_CHECKING, Any
from unittest.mock import AsyncMock, MagicMock, patch

import nio
import pytest
from structlog.testing import capture_logs

from mindroom import reply_lifecycle as rl
from mindroom.ai_run_metadata import (
    build_streaming_ai_run_metadata_content,
    build_terminal_note_ai_run_metadata_content,
    model_payload,
    streaming_ai_run_content,
)
from mindroom.approval.response import continuation_target
from mindroom.approval.tool_approval import shutdown_approval_runtime
from mindroom.config.agent import AgentConfig
from mindroom.config.approval import ApprovalRuleConfig, ToolApprovalConfig
from mindroom.config.budgets import BudgetsConfig
from mindroom.config.cold_cache import ColdCacheConfig
from mindroom.config.main import Config
from mindroom.config.models import ModelConfig, ModelPricing
from mindroom.constants import (
    AI_RUN_METADATA_KEY,
    STREAM_STATUS_APPROVAL_PENDING,
    STREAM_STATUS_CANCELLED,
    STREAM_STATUS_COMPLETED,
    STREAM_STATUS_ERROR,
    STREAM_STATUS_KEY,
    STREAM_STATUS_PENDING,
    STREAM_STATUS_STREAMING,
)
from mindroom.delivery_gateway import FinalDeliveryRequest
from mindroom.event_journal import ApprovalDecisionMetadata, CallKey, EventClass, EventKind, InboundEvent
from mindroom.llm.messages import Message
from mindroom.llm.provider import ModelIdentity, TextDelta, TurnCompleted, UsageReported
from mindroom.llm.usage import MessageMetrics
from mindroom.matrix.client_visible_messages import ResolvedVisibleMessage
from mindroom.matrix.users import AgentMatrixUser
from mindroom.matrix.visible_body import visible_content_from_content
from mindroom.message_target import MessageTarget
from mindroom.reply_presentation import AGENT_PLACEHOLDER, TEAM_PLACEHOLDER, NoteKind
from mindroom.reply_scope import SpanHandle
from mindroom.response_sources import Owned
from mindroom.streaming import send_streaming_response
from mindroom.tool_system.agno_toolkits import Toolkit
from mindroom.turn_policy import ResponseAction
from mindroom.turn_record import TurnRecord
from tests.access_schema_support import with_current_room_member_access
from tests.approval_continuation_helpers import dispatch_approval
from tests.bot_helpers import make_test_agent_bot
from tests.budget_helpers import budget_monitor_with_spend
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
from tests.fakes.scripted_model import Await, ScriptedModel, ScriptStep, answer, calls, tool_call, usage
from tests.response_attempt_helpers import install_direct_response_admission
from tests.response_runner_helpers import _plain_request, _target
from tests.scripted_agent_helpers import registered_toolkit
from tests.streaming_helpers import FakeReplyRecords

if TYPE_CHECKING:
    from collections.abc import AsyncIterator, Iterator, Mapping, Sequence
    from pathlib import Path

    from mindroom.bot import AgentBot
    from mindroom.reply_turn.request import ResponseRequest
    from mindroom.response_runner import ResponseRunner
    from mindroom.turn_model import TurnModel

_SOURCE = "$event"


def _gated_answer(
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


_ROOM = "!room:localhost"
_THREAD = "$thread"
_REQUESTER = "@user:localhost"
_ECHO_TOOL = "streaming_model_meta_echo"


def _visible_content(event_content: Mapping[str, Any]) -> dict[str, Any]:
    """Return the visible Matrix content, following an edit into ``m.new_content``."""
    return dict(visible_content_from_content(event_content))


def _ai_run(event_content: Mapping[str, Any]) -> dict[str, Any] | None:
    """Return ``io.mindroom.ai_run`` from a Matrix message or edit payload."""
    visible = _visible_content(event_content)
    ai_run = visible.get(AI_RUN_METADATA_KEY)
    return dict(ai_run) if isinstance(ai_run, dict) else None


def _stream_status(event_content: Mapping[str, Any]) -> str | None:
    """Return ``io.mindroom.stream_status`` from a Matrix message or edit payload."""
    visible = _visible_content(event_content)
    status = visible.get(STREAM_STATUS_KEY)
    return status if isinstance(status, str) else None


def _assert_provisional_ai_run(event_content: Mapping[str, Any], expected_model: Mapping[str, str]) -> None:
    """Assert ``event_content`` carries provisional, status-less ``io.mindroom.ai_run`` for ``expected_model``."""
    ai_run = _ai_run(event_content)
    assert ai_run == {"version": 1, "model": dict(expected_model)}
    assert "status" not in ai_run
    assert "run_id" not in ai_run
    assert "session_id" not in ai_run


@dataclass
class _Homeserver:
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
        event_id = self.events.setdefault(tx_id or f"anon-{len(self.events)}", f"$sent{len(self.events)}")
        if message_type == "m.room.message":
            self.writes.append(content)
        return nio.RoomSendResponse(event_id=event_id, room_id=room_id)


def _config(
    tmp_path: Path,
    *,
    budgets: BudgetsConfig | None = None,
    cold_cache: ColdCacheConfig | None = None,
    cheap_model: str | None = None,
    tools: Sequence[str] = (),
) -> Config:
    return bind_runtime_paths(
        with_current_room_member_access(
            Config(
                agents={
                    "general": AgentConfig(
                        display_name="General",
                        rooms=[_ROOM],
                        model="default",
                        cheap_model=cheap_model,
                        cold_cache=cold_cache,
                        tools=list(tools),
                    ),
                    "writer": AgentConfig(display_name="Writer", role="Writes.", model="writer-model"),
                    "checker": AgentConfig(display_name="Checker", role="Checks.", model="checker-model"),
                },
                models={
                    "default": ModelConfig(provider="openai", id="gpt-primary", display_name="Primary"),
                    "large": ModelConfig(provider="anthropic", id="claude-large", display_name="Large"),
                    "cheap": ModelConfig(provider="openai", id="gpt-mini", display_name="Mini"),
                    "writer-model": ModelConfig(provider="openai", id="writer-model"),
                    "checker-model": ModelConfig(provider="openai", id="checker-model"),
                },
                budgets=budgets,
                memory={"backend": "none"},
            ),
        ),
        test_runtime_paths(tmp_path),
    )


@dataclass
class _TurnHarness:
    bot: AgentBot
    runner: ResponseRunner
    homeserver: _Homeserver
    models: dict[str, ScriptedModel]


async def _setup_turn(
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
) -> _TurnHarness:
    config = _config(
        tmp_path,
        budgets=budgets,
        cold_cache=cold_cache,
        cheap_model=cheap_model,
        tools=tools,
    )
    agent_user = AgentMatrixUser(
        agent_name="general",
        password=TEST_PASSWORD,
        display_name="General",
        user_id="@mindroom_general:localhost",
    )
    bot = make_test_agent_bot(agent_user, config, runtime_paths_for(config), rooms=[_ROOM])
    install_direct_response_admission(bot)
    bot.client = make_matrix_client_mock(user_id="@mindroom_general:localhost")
    install_runtime_journal_support(bot)
    wrap_extracted_collaborators(bot)
    await bot._reply_runtime.start()
    target = _target(thread_id=thread_id)
    await bot.journal_principal().admit(
        InboundEvent(
            event_id=_SOURCE,
            room_id=_ROOM,
            thread_id=thread_id,
            kind=EventKind.MESSAGE,
            event_class=EventClass.ACTIONABLE,
            sender=_REQUESTER,
            origin_server_ts=1,
            source={},
        ),
    )
    await bot._turn_store.record_pending_turn(
        bot._turn_store.attach_response_context(
            TurnRecord.create([_SOURCE], requester_id=_REQUESTER),
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
            identity=ModelIdentity(
                id=bot.config.models[name].id,
                provider=bot.config.models[name].provider,
            ),
        )
        for name, turns in scripts.items()
    }
    install_model_providers(monkeypatch, models)
    homeserver = _Homeserver()
    bot.client.room_send = AsyncMock(side_effect=homeserver.room_send)
    presence = MagicMock(spec=nio.PresenceGetResponse)
    presence.presence = "online" if streaming else "offline"
    presence.last_active_ago = 0
    bot.client.get_presence = AsyncMock(return_value=presence)
    return _TurnHarness(
        bot=bot,
        runner=unwrap_extracted_collaborator(bot._response_runner),
        homeserver=homeserver,
        models=models,
    )


@pytest.fixture
def echo_tool() -> Iterator[None]:
    """Register a lightweight echo toolkit for approval tests."""

    class _EchoTools(Toolkit):
        def __init__(self) -> None:
            super().__init__(name="echo_tools", tools=[self.echo])

        def echo(self, text: str) -> str:
            return f"echo:{text}"

    with registered_toolkit(_ECHO_TOOL, _EchoTools):
        yield


# ---------------------------------------------------------------------------
# 1. Helper unit tests
# ---------------------------------------------------------------------------


def test_streaming_and_terminal_note_ai_run_metadata_helpers(tmp_path: Path) -> None:
    """Provisional helpers omit terminal/run fields; terminal note helper always sets a terminal status."""
    config = _config(tmp_path)
    expected_model = {"config": "default", "display_name": "Primary", "id": "gpt-primary", "provider": "openai"}

    assert model_payload(config, "default") == expected_model
    provisional = build_streaming_ai_run_metadata_content(config, "default")
    assert provisional == {AI_RUN_METADATA_KEY: {"version": 1, "model": expected_model}}
    assert streaming_ai_run_content(expected_model) == provisional
    for forbidden in ("status", "run_id", "session_id"):
        assert forbidden not in provisional[AI_RUN_METADATA_KEY]

    for status in ("completed", "cancelled", "error"):
        with_model = build_terminal_note_ai_run_metadata_content(status, model=expected_model)
        assert with_model == {AI_RUN_METADATA_KEY: {"version": 1, "status": status, "model": expected_model}}
        without_model = build_terminal_note_ai_run_metadata_content(status, model=None)
        assert without_model == {AI_RUN_METADATA_KEY: {"version": 1, "status": status}}


# ---------------------------------------------------------------------------
# 2. StreamingResponse unit tests (initial send, progress, mid-stream switch, final)
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_streaming_initial_send_progress_switch_and_final_settled_metadata(tmp_path: Path) -> None:
    """Initial send without placeholder and progress edits carry active_model; final uses settled metadata."""
    config = _config(tmp_path)
    config.defaults.streaming.update_interval = 0.0
    config.defaults.streaming.min_update_interval = 0.0
    config.defaults.streaming.max_idle = 0.0
    client = make_matrix_client_mock(user_id="@mindroom_general:localhost")
    homeserver = _Homeserver()
    client.room_send = AsyncMock(side_effect=homeserver.room_send)
    records = FakeReplyRecords()

    active_model = [model_payload(config, "default")]
    run_metadata_content: dict[str, Any] = {}

    async def chunks() -> AsyncIterator[str]:
        yield "Initial chunk"
        await asyncio.sleep(0.02)
        yield " plus streaming chunk from default"
        await asyncio.sleep(0.02)
        active_model[0] = model_payload(config, "large")
        yield " and second chunk after switch."
        await asyncio.sleep(0.02)
        run_metadata_content[AI_RUN_METADATA_KEY] = {
            "version": 1,
            "run_id": "run-123",
            "session_id": "sess-123",
            "status": "completed",
            "model": model_payload(config, "large"),
            "tools": {"count": 1},
        }

    outcome = await send_streaming_response(
        client,
        MessageTarget.resolve(_ROOM, None, None, room_mode=True),
        config,
        runtime_paths_for(config),
        chunks(),
        extra_content=run_metadata_content,
        active_model=lambda: active_model[0],
        **records.hooks(),
    )
    assert outcome.visible_body_state == "visible_body"
    assert len(homeserver.writes) >= 3

    initial_write = homeserver.writes[0]
    assert _stream_status(initial_write) == STREAM_STATUS_PENDING
    _assert_provisional_ai_run(initial_write, model_payload(config, "default"))

    initial_streaming = [
        write
        for write in homeserver.writes[:-1]
        if _stream_status(write) == STREAM_STATUS_STREAMING
        and (_ai_run(write) or {}).get("model") == model_payload(config, "default")
    ]
    assert initial_streaming
    _assert_provisional_ai_run(initial_streaming[0], model_payload(config, "default"))

    switched_progress = [
        write
        for write in homeserver.writes[1:-1]
        if _stream_status(write) == STREAM_STATUS_STREAMING
        and (_ai_run(write) or {}).get("model") == model_payload(config, "large")
    ]
    assert switched_progress, "expected a streaming progress edit carrying the switched model"
    _assert_provisional_ai_run(switched_progress[0], model_payload(config, "large"))

    final_write = homeserver.writes[-1]
    assert _stream_status(final_write) == STREAM_STATUS_COMPLETED
    assert _ai_run(final_write) == run_metadata_content[AI_RUN_METADATA_KEY]


@pytest.mark.asyncio
async def test_streaming_final_never_leaves_ai_run_omitted_or_statusless(tmp_path: Path) -> None:
    """Final streamed edit stamps status=completed when settled metadata is absent or status-less."""
    config = _config(tmp_path)
    config.defaults.streaming.update_interval = 0.0
    config.defaults.streaming.min_update_interval = 0.0
    config.defaults.streaming.max_idle = 0.0
    model = model_payload(config, "default")

    for extra_content, expected_ai_run in [
        (
            {},
            {"version": 1, "status": "completed", "model": model},
        ),
        (
            {AI_RUN_METADATA_KEY: {"version": 1, "prepared_context": {"tokens": 42}}},
            {"version": 1, "prepared_context": {"tokens": 42}, "model": model, "status": "completed"},
        ),
    ]:
        client = make_matrix_client_mock(user_id="@mindroom_general:localhost")
        homeserver = _Homeserver()
        client.room_send = AsyncMock(side_effect=homeserver.room_send)
        records = FakeReplyRecords()

        async def chunks() -> AsyncIterator[str]:
            yield "Done"

        await send_streaming_response(
            client,
            MessageTarget.resolve(_ROOM, None, None, room_mode=True),
            config,
            runtime_paths_for(config),
            chunks(),
            extra_content=dict(extra_content),
            active_model=lambda: model,
            **records.hooks(),
        )
        final_write = homeserver.writes[-1]
        assert _stream_status(final_write) == STREAM_STATUS_COMPLETED
        assert _ai_run(final_write) == expected_ai_run


# ---------------------------------------------------------------------------
# 3. Agent & team placeholder, streaming, final, budget, cold-cache, switch
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_agent_placeholder_streaming_and_final_metadata_lifecycle(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Agent Thinking... placeholder, streaming edit, and completed final carry early then settled ai_run."""
    gate = asyncio.Event()
    turn = await _setup_turn(
        tmp_path,
        monkeypatch,
        scripts={
            "default": [_gated_answer("Hello ", gate, "world!", metrics=usage(10, 5))],
        },
    )
    expected_model = model_payload(turn.bot.config, "default")

    task = asyncio.create_task(turn.runner.generate_response(_plain_request(_target())))
    try:
        for _ in range(100):
            if any(_stream_status(w) == STREAM_STATUS_STREAMING for w in turn.homeserver.writes):
                break
            await asyncio.sleep(0.01)
        gate.set()
        assert await asyncio.wait_for(task, timeout=10) == Owned()
    finally:
        gate.set()

    placeholder = turn.homeserver.writes[0]
    assert _visible_content(placeholder)["body"] == AGENT_PLACEHOLDER
    assert _stream_status(placeholder) == STREAM_STATUS_PENDING
    _assert_provisional_ai_run(placeholder, expected_model)

    streaming_writes = [w for w in turn.homeserver.writes if _stream_status(w) == STREAM_STATUS_STREAMING]
    assert streaming_writes
    for write in streaming_writes:
        _assert_provisional_ai_run(write, expected_model)

    final_write = turn.homeserver.writes[-1]
    assert _stream_status(final_write) == STREAM_STATUS_COMPLETED
    final_ai_run = _ai_run(final_write)
    assert final_ai_run is not None
    assert final_ai_run["status"] == "completed"
    assert final_ai_run["model"] == expected_model
    assert "run_id" in final_ai_run
    assert "session_id" in final_ai_run
    assert final_ai_run["usage"]["total_tokens"] == 15


@pytest.mark.asyncio
async def test_team_placeholder_streaming_and_final_metadata_lifecycle(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Team Thinking... placeholder, streaming edits, and final carry early then settled ai_run."""
    gate = asyncio.Event()
    turn = await _setup_turn(
        tmp_path,
        monkeypatch,
        scripts={
            "default": [
                calls(tool_call("delegate_task_to_members", "call-1", task="Check")),
                _gated_answer("Consensus ", gate, "reached."),
            ],
            "writer-model": [answer("Writer note.")],
            "checker-model": [answer("Checker note.")],
        },
    )
    expected_model = model_payload(turn.bot.config, "default")

    task = asyncio.create_task(
        turn.runner.generate_team_response_helper(
            _plain_request(_target()),
            member_names=["writer", "checker"],
            team_mode="collaborate",
        ),
    )
    try:
        for _ in range(200):
            if any("Consensus" in str(_visible_content(w).get("body", "")) for w in turn.homeserver.writes):
                break
            await asyncio.sleep(0.01)
        gate.set()
        assert await asyncio.wait_for(task, timeout=10) == Owned()
    finally:
        gate.set()

    placeholder = turn.homeserver.writes[0]
    assert _visible_content(placeholder)["body"] == TEAM_PLACEHOLDER
    assert _stream_status(placeholder) == STREAM_STATUS_PENDING
    _assert_provisional_ai_run(placeholder, expected_model)

    streaming_writes = [w for w in turn.homeserver.writes if _stream_status(w) == STREAM_STATUS_STREAMING]
    assert streaming_writes
    for write in streaming_writes:
        _assert_provisional_ai_run(write, expected_model)

    final_write = turn.homeserver.writes[-1]
    assert _stream_status(final_write) == STREAM_STATUS_COMPLETED
    final_ai_run = _ai_run(final_write)
    assert final_ai_run is not None
    assert final_ai_run["status"] == "completed"
    assert final_ai_run["model"] == expected_model
    assert "run_id" in final_ai_run
    assert "session_id" in final_ai_run


@pytest.mark.asyncio
async def test_agent_placeholder_applies_budget_without_double_logging_and_keeps_unbudgeted_cold_cache(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Over-budget agent placeholder uses fallback model, logs fallback once, and passes unbudgeted model to cold-cache."""
    turn = await _setup_turn(
        tmp_path,
        monkeypatch,
        scripts={"cheap": [answer("Budgeted reply.")]},
        budgets=BudgetsConfig(monthly_limit_usd=10.0, fallback_model="cheap"),
    )
    turn.bot.config.models["default"].pricing = ModelPricing(input=5, output=30)
    monkeypatch.setattr(
        turn.bot.orchestrator,
        "_budgets",
        budget_monitor_with_spend(turn.bot.runtime_paths, {_REQUESTER: 15.0}),
    )

    observed_cold_cache_models: list[str] = []
    original_cold_cache = turn.runner._cold_cache_model

    async def spy_cold_cache(
        request: ResponseRequest,
        *,
        runtime_model: TurnModel,
        thread_id: str | None,
    ) -> str:
        observed_cold_cache_models.append(runtime_model.model_name)
        return await original_cold_cache(request, runtime_model=runtime_model, thread_id=thread_id)

    monkeypatch.setattr(turn.runner, "_cold_cache_model", spy_cold_cache)

    with capture_logs() as logs:
        assert await turn.runner.generate_response(_plain_request(_target())) == Owned()

    placeholder = turn.homeserver.writes[0]
    assert _stream_status(placeholder) == STREAM_STATUS_PENDING
    _assert_provisional_ai_run(placeholder, model_payload(turn.bot.config, "cheap"))

    # _cold_cache_model must still receive the pre-budget primary model.
    assert observed_cold_cache_models == ["default"]

    # Budget fallback must be logged exactly once (in prepare_response_runtime, not placeholder resolution).
    fallback_logs = [entry for entry in logs if entry.get("event") == "budget_fallback_applied"]
    assert len(fallback_logs) == 1


@pytest.mark.asyncio
async def test_cold_cache_switch_updates_streaming_model_after_placeholder(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Placeholder uses primary model; once cold-cache switches to cheap, streaming and final edits carry cheap."""
    gate = asyncio.Event()
    turn = await _setup_turn(
        tmp_path,
        monkeypatch,
        scripts={
            "cheap": [_gated_answer("Cheap ", gate, "answer.")],
        },
        cheap_model="cheap",
        cold_cache=ColdCacheConfig(),
        thread_id=_THREAD,
    )
    monkeypatch.setattr("mindroom.response_runner.select_cold_cache_model", AsyncMock(return_value="cheap"))
    stale_ts_ms = int((time.time() - 7200) * 1000)
    request = replace(
        _plain_request(_target(thread_id=_THREAD)),
        thread_history=[
            ResolvedVisibleMessage.synthetic(
                sender="@mindroom_general:localhost",
                body="Earlier reply",
                timestamp=stale_ts_ms,
                event_id="$earlier",
                thread_id=_THREAD,
            ),
        ],
    )

    task = asyncio.create_task(turn.runner.generate_response(request))
    try:
        for _ in range(100):
            if any(_stream_status(w) == STREAM_STATUS_STREAMING for w in turn.homeserver.writes):
                break
            await asyncio.sleep(0.01)
        gate.set()
        assert await asyncio.wait_for(task, timeout=10) == Owned()
    finally:
        gate.set()

    placeholder = turn.homeserver.writes[0]
    _assert_provisional_ai_run(placeholder, model_payload(turn.bot.config, "default"))

    streaming_writes = [w for w in turn.homeserver.writes if _stream_status(w) == STREAM_STATUS_STREAMING]
    assert streaming_writes
    for write in streaming_writes:
        _assert_provisional_ai_run(write, model_payload(turn.bot.config, "cheap"))

    final_ai_run = _ai_run(turn.homeserver.writes[-1])
    assert final_ai_run is not None
    assert final_ai_run["status"] == "completed"
    assert final_ai_run["model"] == model_payload(turn.bot.config, "cheap")


@pytest.mark.asyncio
async def test_mid_turn_switch_thread_model_after_toolcall_updates_streaming_model(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """switch_thread_model(after-toolcall) updates io.mindroom.ai_run.model on subsequent streaming edits."""
    second_attempt_gate = asyncio.Event()
    turn = await _setup_turn(
        tmp_path,
        monkeypatch,
        tools=["thread_model"],
        thread_id=_THREAD,
        scripts={
            "default": [
                calls(
                    tool_call(
                        "switch_thread_model",
                        "switch-1",
                        model_name="large",
                        when="after-toolcall",
                    ),
                    content="Switching now. ",
                ),
            ],
            "large": [_gated_answer("Continuing on large ", second_attempt_gate, "model.")],
        },
    )

    task = asyncio.create_task(turn.runner.generate_response(_plain_request(_target(thread_id=_THREAD))))
    try:
        for _ in range(200):
            if any("Continuing on large" in str(_visible_content(w).get("body", "")) for w in turn.homeserver.writes):
                break
            await asyncio.sleep(0.01)
        second_attempt_gate.set()
        assert await asyncio.wait_for(task, timeout=10) == Owned()
    finally:
        second_attempt_gate.set()

    default_model = model_payload(turn.bot.config, "default")
    large_model = model_payload(turn.bot.config, "large")

    # Placeholder and first attempt's streaming writes carry default.
    _assert_provisional_ai_run(turn.homeserver.writes[0], default_model)
    first_streaming = [
        w
        for w in turn.homeserver.writes
        if _stream_status(w) == STREAM_STATUS_STREAMING
        and "Switching now." in str(_visible_content(w).get("body", ""))
        and "Continuing on large" not in str(_visible_content(w).get("body", ""))
    ]
    assert first_streaming
    _assert_provisional_ai_run(first_streaming[0], default_model)

    # Second attempt's streaming writes carry large.
    switched_streaming = [
        w
        for w in turn.homeserver.writes
        if _stream_status(w) == STREAM_STATUS_STREAMING
        and "Continuing on large" in str(_visible_content(w).get("body", ""))
    ]
    assert switched_streaming
    _assert_provisional_ai_run(switched_streaming[0], large_model)

    # Final completed write carries settled large metadata.
    final_ai_run = _ai_run(turn.homeserver.writes[-1])
    assert final_ai_run is not None
    assert final_ai_run["status"] == "completed"
    assert final_ai_run["model"] == large_model


@pytest.mark.asyncio
async def test_provisional_model_never_masks_recorder_run_metadata_fallback(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """When run_metadata_content is empty, deliver_final preserves recorder.run_metadata and stamps status=completed."""
    turn = await _setup_turn(
        tmp_path,
        monkeypatch,
        streaming=False,
        scripts={"default": [answer("unused")]},
    )
    gateway = unwrap_extracted_collaborator(turn.bot._delivery_gateway)
    target = _target()
    request = _plain_request(target)
    lifecycle = turn.runner.locked.build_lifecycle(request, response_kind="ai")

    async with turn.bot._reply_runtime.span_scope() as slot:
        handle = await turn.bot._reply_runtime.claim(
            sources=request.sources,
            room_id=_ROOM,
            thread_id=None,
            placeholder=AGENT_PLACEHOLDER,
            show_tool_calls=True,
        )
        assert isinstance(handle, SpanHandle)
        slot.handle = handle
        handle.reply_model = model_payload(turn.bot.config, "default")
        placeholder_id = await turn.runner._send_placeholder(
            request,
            history_scope=turn.bot._turn_store.response_history_scope(ResponseAction(kind="individual")),
            execution_identity=turn.runner.deps.tool_runtime.build_execution_identity(
                target=target,
                user_id=request.user_id,
            ),
            placeholder_message=AGENT_PLACEHOLDER,
        )
        assert placeholder_id is not None
        # Simulate final_extra_content() carrying only status-less prepared_history fallback from recorder.run_metadata.
        outcome = await gateway.deliver_final(
            FinalDeliveryRequest(
                target=target,
                existing_event_id=placeholder_id,
                response_text="Final text",
                identity=lifecycle.identity,
                tool_trace=None,
                extra_content={AI_RUN_METADATA_KEY: {"version": 1, "prepared_context": {"tokens": 77}}},
            ),
        )
    assert outcome.is_visible_response
    final_ai_run = _ai_run(turn.homeserver.writes[-1])
    assert final_ai_run == {
        "version": 1,
        "prepared_context": {"tokens": 77},
        "model": model_payload(turn.bot.config, "default"),
        "status": "completed",
    }


@pytest.mark.asyncio
async def test_deliver_final_over_placeholder_with_no_extra_content_writes_terminal_ai_run(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Finalizing over a placeholder whose span set reply_model never drops io.mindroom.ai_run."""
    turn = await _setup_turn(
        tmp_path,
        monkeypatch,
        streaming=False,
        scripts={"default": [answer("unused")]},
    )
    gateway = unwrap_extracted_collaborator(turn.bot._delivery_gateway)
    target = _target()
    request = _plain_request(target)
    lifecycle = turn.runner.locked.build_lifecycle(request, response_kind="ai")

    async with turn.bot._reply_runtime.span_scope() as slot:
        handle = await turn.bot._reply_runtime.claim(
            sources=request.sources,
            room_id=_ROOM,
            thread_id=None,
            placeholder=AGENT_PLACEHOLDER,
            show_tool_calls=True,
        )
        assert isinstance(handle, SpanHandle)
        slot.handle = handle
        handle.reply_model = model_payload(turn.bot.config, "default")
        placeholder_id = await turn.runner._send_placeholder(
            request,
            history_scope=turn.bot._turn_store.response_history_scope(ResponseAction(kind="individual")),
            execution_identity=turn.runner.deps.tool_runtime.build_execution_identity(
                target=target,
                user_id=request.user_id,
            ),
            placeholder_message=AGENT_PLACEHOLDER,
        )
        assert placeholder_id is not None
        outcome = await gateway.deliver_final(
            FinalDeliveryRequest(
                target=target,
                existing_event_id=placeholder_id,
                response_text="Final text without run metadata",
                identity=lifecycle.identity,
                tool_trace=None,
                extra_content=None,
            ),
        )
    assert outcome.is_visible_response
    assert _stream_status(turn.homeserver.writes[-1]) == STREAM_STATUS_COMPLETED
    assert _ai_run(turn.homeserver.writes[-1]) == {
        "version": 1,
        "status": "completed",
        "model": model_payload(turn.bot.config, "default"),
    }


# ---------------------------------------------------------------------------
# 4. Terminal-note contract tests (stop, error, restart owed note, approval failure)
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_terminal_notes_always_carry_terminal_ai_run_status(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Stop/cancel note, error note, and post-crash owed note (handle=None) all carry terminal io.mindroom.ai_run."""
    # (a) User stop / cancel note
    gate = asyncio.Event()
    turn_cancel = await _setup_turn(
        tmp_path / "cancel",
        monkeypatch,
        scripts={"default": [_gated_answer("Working", gate, " more")]},
    )
    cancel_task = asyncio.create_task(turn_cancel.runner.generate_response(_plain_request(_target())))
    try:
        for _ in range(100):
            if any(_stream_status(w) == STREAM_STATUS_STREAMING for w in turn_cancel.homeserver.writes):
                break
            await asyncio.sleep(0.01)
        reply = await turn_cancel.bot.journal_principal().replies.for_sources((_SOURCE,))
        assert reply is not None
        assert reply.event_id is not None
        assert await turn_cancel.bot._user_stop_reconciler.finalize(reply.event_id, 7, room_id=_ROOM)
        await asyncio.wait_for(cancel_task, timeout=10)
    finally:
        gate.set()

    cancel_final = turn_cancel.homeserver.writes[-1]
    assert _stream_status(cancel_final) == STREAM_STATUS_CANCELLED
    assert _ai_run(cancel_final) == {
        "version": 1,
        "status": "cancelled",
        "model": model_payload(turn_cancel.bot.config, "default"),
    }

    # (b) Error note when stream fails before completing
    turn_error = await _setup_turn(
        tmp_path / "error",
        monkeypatch,
        scripts={"default": [[TextDelta("Partial")]]},
    )

    with patch("mindroom.delivery_gateway.send_streaming_response", side_effect=RuntimeError("boom")):
        await turn_error.runner.generate_response(_plain_request(_target()))

    error_final = turn_error.homeserver.writes[-1]
    assert _stream_status(error_final) == STREAM_STATUS_ERROR
    assert _ai_run(error_final) == {
        "version": 1,
        "status": "error",
        "model": model_payload(turn_error.bot.config, "default"),
    }

    # (c) Post-crash restart owed note (_flush_owed_write with write.handle=None)
    turn_owed = await _setup_turn(
        tmp_path / "owed",
        monkeypatch,
        scripts={"default": [answer("unused")]},
    )
    gateway_owed = unwrap_extracted_collaborator(turn_owed.bot._delivery_gateway)
    request = _plain_request(_target())
    async with turn_owed.bot._reply_runtime.span_scope() as slot:
        handle = await turn_owed.bot._reply_runtime.claim(
            sources=request.sources,
            room_id=_ROOM,
            thread_id=None,
            placeholder=AGENT_PLACEHOLDER,
            show_tool_calls=True,
        )
        assert isinstance(handle, SpanHandle)
        slot.handle = handle
        handle.reply_model = model_payload(turn_owed.bot.config, "default")
        placeholder_id = await turn_owed.runner._send_placeholder(
            request,
            history_scope=turn_owed.bot._turn_store.response_history_scope(ResponseAction(kind="individual")),
            execution_identity=turn_owed.runner.deps.tool_runtime.build_execution_identity(
                target=_target(),
                user_id=request.user_id,
            ),
            placeholder_message=AGENT_PLACEHOLDER,
        )
        assert placeholder_id is not None
        reply_id = handle.reply_id
        span = handle.span
    # Simulate a crash leaving an owed note on the reply, then flush with no running span handle (write.handle=None).
    await turn_owed.bot.journal_principal().replies.update(
        reply_id,
        lambda current: rl.dispatch_failed(current, span, error_text="Crash before delivery", now_ns=time.time_ns()),
    )
    await gateway_owed._flush_owed_write(reply_id)

    owed_write = turn_owed.homeserver.writes[-1]
    assert _stream_status(owed_write) == STREAM_STATUS_ERROR
    assert _ai_run(owed_write) == {"version": 1, "status": "error"}


# ---------------------------------------------------------------------------
# 5. Approval pause, resumed stream_progress, and approval failure note tests
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_approval_pause_resumed_stream_progress_and_final_carry_model_metadata(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    echo_tool: None,
) -> None:
    """Approval pause edit, resumed stream_progress edits, and final edit carry model metadata."""
    del echo_tool
    resume_gate = asyncio.Event()
    turn = await _setup_turn(
        tmp_path,
        monkeypatch,
        tools=[_ECHO_TOOL],
        scripts={
            "default": [
                calls(tool_call("echo", "call-1", text="hi")),
                _gated_answer("Resumed ", resume_gate, "finished."),
            ],
        },
    )
    turn.bot.config.tool_approval = ToolApprovalConfig(
        rules=[ApprovalRuleConfig(match="echo", action="require_approval")],
    )
    turn.bot.config.background_tool_jobs.enabled = False

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
    expected_model = model_payload(turn.bot.config, "default")
    try:
        assert await turn.runner.generate_response(_plain_request(_target())) == Owned()

        pause_writes = [w for w in turn.homeserver.writes if _stream_status(w) == STREAM_STATUS_APPROVAL_PENDING]
        assert pause_writes
        _assert_provisional_ai_run(pause_writes[-1], expected_model)

        continuation = await turn.bot.journal_principal().approvals.approval_continuation_for_source(_SOURCE)
        assert continuation is not None
        assert continuation.runtime_model_name == "default"
        writes_before_resume = len(turn.homeserver.writes)

        # Approve and resume the paused run while watching resumed stream_progress edits.
        await turn.bot.journal_principal().approvals.resolve_approval_call(
            CallKey(continuation.approval_id, continuation.generation, "call-1"),
            requested_status="approved",
            reason=None,
            metadata=ApprovalDecisionMetadata(resolved_by=_REQUESTER),
        )
        updated = await turn.bot.journal_principal().approvals.approval_continuation(continuation.approval_id)
        assert updated is not None
        resume_task = asyncio.create_task(dispatch_approval(turn.runner.approvals, updated, target=_target()))
        try:
            for _ in range(200):
                resumed_writes = turn.homeserver.writes[writes_before_resume:]
                if any(_stream_status(w) == STREAM_STATUS_STREAMING for w in resumed_writes):
                    break
                await asyncio.sleep(0.01)
            resume_gate.set()
            await asyncio.wait_for(resume_task, timeout=10)
        finally:
            resume_gate.set()

        resumed_writes = turn.homeserver.writes[writes_before_resume:]
        resumed_streaming = [w for w in resumed_writes if _stream_status(w) == STREAM_STATUS_STREAMING]
        assert resumed_streaming
        for write in resumed_streaming:
            _assert_provisional_ai_run(write, expected_model)

        final_write = resumed_writes[-1]
        assert _stream_status(final_write) == STREAM_STATUS_COMPLETED
        final_ai_run = _ai_run(final_write)
        assert final_ai_run is not None
        assert final_ai_run["status"] == "completed"
        assert final_ai_run["model"] == expected_model
    finally:
        await shutdown_approval_runtime()


@pytest.mark.asyncio
async def test_approval_failure_note_carries_terminal_ai_run_with_model(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    echo_tool: None,
) -> None:
    """write_approval_failure_note writes terminal io.mindroom.ai_run with status and continuation model."""
    del echo_tool
    turn = await _setup_turn(
        tmp_path,
        monkeypatch,
        tools=[_ECHO_TOOL],
        scripts={"default": [calls(tool_call("echo", "call-1", text="hi"))]},
    )
    turn.bot.config.tool_approval = ToolApprovalConfig(
        rules=[ApprovalRuleConfig(match="echo", action="require_approval")],
    )
    turn.bot.config.background_tool_jobs.enabled = False

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
    try:
        assert await turn.runner.generate_response(_plain_request(_target())) == Owned()
        continuation = await turn.bot.journal_principal().approvals.approval_continuation_for_source(_SOURCE)
        assert continuation is not None
        gateway = unwrap_extracted_collaborator(turn.bot._delivery_gateway)
        assert await gateway.write_approval_failure_note(
            continuation.response_event_id,
            approval_id=continuation.approval_id,
            note=NoteKind.ERROR,
            text="Approval timed out.",
            target=continuation_target(continuation),
            model_name=continuation.runtime_model_name,
        )
        note_write = turn.homeserver.writes[-1]
        assert _stream_status(note_write) == STREAM_STATUS_ERROR
        assert _ai_run(note_write) == {
            "version": 1,
            "status": "error",
            "model": model_payload(turn.bot.config, "default"),
        }
    finally:
        await shutdown_approval_runtime()
