"""ResponseRunner team-helper streaming lifecycle: delivery failures, cancellation persistence, and prompt merging."""

from __future__ import annotations

import asyncio
from contextlib import suppress
from dataclasses import dataclass, replace
from functools import partial
from types import SimpleNamespace
from typing import TYPE_CHECKING, cast
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from mindroom.bot import AgentBot
from mindroom.cancellation import (
    cancel_failure_reason,
    classify_cancel_source,
    current_task_is_process_shutdown,
    request_task_cancel,
)
from mindroom.config.agent import AgentConfig, AgentPrivateConfig
from mindroom.config.main import Config
from mindroom.config.models import ModelConfig
from mindroom.constants import (
    MATRIX_RESPONSE_EVENT_ID_METADATA_KEY,
    ROUTER_AGENT_NAME,
)
from mindroom.conversation_state_writer import ConversationStateWriter, ConversationStateWriterDeps
from mindroom.delivery_gateway import DeliveryGateway
from mindroom.final_delivery import FinalDeliveryOutcome, StreamTransportOutcome
from mindroom.history.session_context import create_scope_session_storage, load_scope_session
from mindroom.history.types import HistoryScope
from mindroom.hooks import (
    EVENT_MESSAGE_AFTER_RESPONSE,
    EVENT_SESSION_STARTED,
    AfterResponseContext,
    HookRegistry,
    SessionHookContext,
    hook,
)
from mindroom.llm.messages import Message
from mindroom.llm.runs import RunStatus, TeamRunRecord
from mindroom.message_target import MessageTarget
from mindroom.prompt_message_tags import render_msg_tag
from mindroom.response_runner import (
    ResponseRunner,
)
from mindroom.response_turn import (
    AttemptResolved,
    CompletedAttempt,
    HandledAttempt,
    TurnAdapter,
    TurnContinuationRunState,
    TurnPartialSnapshot,
    stream_response_turn,
)
from mindroom.runtime_shutdown import ORDERLY_SHUTDOWN
from mindroom.session_storage.types import ConversationSession
from mindroom.streaming import StreamingDeliveryError
from mindroom.teams.spec import TeamMode
from mindroom.tool_system.context_bound_streams import closing_async_stream
from tests.access_schema_support import with_current_room_member_access
from tests.ai_user_id_helpers import (
    _build_response_runner,
    _config,
    _config_with_team,
    _config_with_team_matrix_message,
    _install_inert_post_response_effects,
    _knowledge_access_support,
    _make_bot,
    _response_request,
    _runtime_paths,
    _set_gateway_method,
    bind_runtime_paths,
    team_orchestrator,
)
from tests.bot_helpers import (
    _handled_response_event_id,
    _hook_plugin,
    unique_room_send_responses,
)
from tests.conftest import install_model_providers, seed_session
from tests.fakes.scripted_model import Await, ScriptedModel, answer
from tests.team_turn_helpers import scripted_team_stream

if TYPE_CHECKING:
    from collections.abc import AsyncIterator, Awaitable, Callable
    from pathlib import Path

    from mindroom.constants import RuntimePaths
    from mindroom.delivery_gateway import StreamingDeliveryRequest
    from mindroom.response_turn import ResponseTurnContext, TurnRunState, TurnSinks
    from mindroom.session_storage.store import ConversationStore
    from mindroom.teams.envelope import TeamTurnSpec

TEAM_SESSION_ID = "!test:localhost:$thread-root"


def _team_store(config: Config, runtime_paths: RuntimePaths, *, scope_id: str = "ultimate") -> ConversationStore:
    """Open the team scope's conversation store, where a team reply of ``config`` stores its runs."""
    return create_scope_session_storage(
        scope=HistoryScope(kind="team", scope_id=scope_id),
        config=config,
        runtime_paths=runtime_paths,
        execution_identity=None,
    )


def _team_session(
    session_id: str = TEAM_SESSION_ID,
    *,
    team_id: str = "ultimate",
    runs: list[TeamRunRecord] | None = None,
) -> ConversationSession:
    return ConversationSession(
        session_id=session_id,
        kind="team",
        owner_id=team_id,
        metadata={},
        runs=list(runs or []),
        created_at=1,
    )


def _start_team_session(store: ConversationStore, session_id: str, *, team_id: str = "ultimate") -> None:
    """Make the store write that creates a team reply's session row, as the reply's first write does."""
    store.update_metadata(_team_session(session_id, team_id=team_id), lambda _metadata: {"started": True})


def _persisted_runs(store: ConversationStore) -> list[TeamRunRecord]:
    """Return the team runs the reply's session holds, in the order they were stored."""
    session = store.load_session(TEAM_SESSION_ID)
    assert session is not None
    return [run for run in session.runs if isinstance(run, TeamRunRecord)]


def _assert_interrupted_messages(
    run: TeamRunRecord,
    *,
    response_event_id: str,
    assistant_body: str,
) -> None:
    assert run.messages is not None
    assert [message.role for message in run.messages] == ["user", "assistant"]
    assert 'event_id="$user_msg"' in cast("str", run.messages[0].content)
    assert "Hello" in cast("str", run.messages[0].content)
    assert run.metadata is not None
    assert run.metadata["matrix_response_event_id"] == response_event_id
    assert run.messages[1].content == assistant_body


def _streamed(event_id: str | None, body: str) -> StreamTransportOutcome:
    """Return what a stream that ran to its end left: ``body``, shown in ``event_id``."""
    return StreamTransportOutcome(
        last_physical_stream_event_id=event_id,
        rendered_body=body,
        visible_body_state="visible_body",
    )


@dataclass(frozen=True)
class _Shown:
    """A running attempt that has shown ``text``, as an interruption records it."""

    text: str

    def partial_snapshot(self) -> TurnPartialSnapshot:
        return TurnPartialSnapshot(assistant_text=self.text)


type _Attempt = Callable[[TurnRunState], AsyncIterator[str | AttemptResolved]]


def _driven_team_stream(
    config: Config,
    runtime_paths: RuntimePaths,
    attempt: _Attempt,
    *,
    scope_id: str = "ultimate",
) -> Callable[..., AsyncIterator[str]]:
    """Return a ``team_response_stream`` stand-in: the real turn driver around ``attempt``, in the team's scope.

    The driver writes the turn's record and stores an interrupted turn, as it does around a team's attempts.
    """

    def team_response_stream(_spec: TeamTurnSpec, ctx: ResponseTurnContext, *, sinks: TurnSinks) -> AsyncIterator[str]:
        assert ctx.session_id is not None
        adapter = TurnAdapter[str](
            load_scope=partial(
                load_scope_session,
                scope=HistoryScope(kind="team", scope_id=scope_id),
                session_id=ctx.session_id,
                config=config,
                runtime_paths=runtime_paths,
                execution_identity=ctx.execution_identity,
            ),
            run_attempt=lambda run, _continuation: attempt(run),
            make_text_chunk=lambda text: text,
            user_message=ctx.prompt,
        )
        return stream_response_turn(
            ctx,
            adapter,
            sinks,
            continuation=TurnContinuationRunState.initial(
                prompt=ctx.prompt,
                model_prompt=None,
                current_timestamp_ms=None,
                current_prompt_is_structured=ctx.current_prompt_is_structured,
                current_event_id=ctx.reply_to_event_id,
                run_id=ctx.run_id,
                continuation_model_prompt_tail="",
            ),
        )

    return team_response_stream


def _shows_then_waits(text: str = "Team hello", *, before: Callable[[], None] | None = None) -> _Attempt:
    """Return an attempt that runs ``before``, shows ``text``, and then runs until its turn is cut short."""

    async def attempt(run: TurnRunState) -> AsyncIterator[str | AttemptResolved]:
        if before is not None:
            before()
        run.attempt = _Shown(text)
        yield text
        await asyncio.Event().wait()
        yield AttemptResolved(CompletedAttempt())

    return attempt


async def _fail_after_the_first_chunk(request: StreamingDeliveryRequest) -> StreamTransportOutcome:
    """Fail a stream's delivery after its first chunk, as the streamer does: it closes the stream, then raises."""
    async with closing_async_stream(request.response_stream):
        await anext(request.response_stream)
    raise StreamingDeliveryError(RuntimeError("boom"))


@pytest.mark.asyncio
async def test_generate_team_response_helper_preserves_raw_prompt_when_model_prompt_supplies_tail(
    tmp_path: Path,
) -> None:
    """Team responses should keep the raw user text when model_prompt only adds transient tails."""
    runtime_paths = _runtime_paths(tmp_path)
    config = bind_runtime_paths(_config_with_team(), runtime_paths)
    bot = _make_bot(tmp_path, config=config, runtime_paths=runtime_paths, agent_name="ultimate")

    with (
        patch("mindroom.response_runner.should_use_streaming", new=AsyncMock(return_value=False)),
        patch(
            "mindroom.response_runner.team_response_stream",
            new=scripted_team_stream("Team answer"),
        ) as team_stream,
    ):
        coordinator = _build_response_runner(
            bot,
            config=config,
            runtime_paths=runtime_paths,
            storage_path=tmp_path,
            message_target=MessageTarget.resolve("!test:localhost", "$thread-root", "$user_msg"),
            orchestrator=team_orchestrator(config, runtime_paths),
        )

        await coordinator.generate_team_response_helper(
            replace(
                _response_request(prompt="Describe this image", user_id="@alice:localhost", thread_id="$thread-root"),
                model_prompt="Available attachment IDs: att_1. Use tool calls to inspect or process them.",
            ),
            member_names=["general"],
            team_mode="coordinate",
        )

    _spec, ctx = team_stream.call_args.args
    message = ctx.prompt
    assert "Describe this image" in message
    assert "Available attachment IDs: att_1" in message


@pytest.mark.asyncio
@pytest.mark.parametrize("team_mode", ["coordinate", "collaborate"])
async def test_generate_team_response_helper_runs_the_team_in_its_mode(tmp_path: Path, team_mode: str) -> None:
    """The team runs in the mode its request names."""
    runtime_paths = _runtime_paths(tmp_path)
    config = bind_runtime_paths(_config_with_team(), runtime_paths)
    bot = _make_bot(tmp_path, config=config, runtime_paths=runtime_paths, agent_name="ultimate")

    with (
        patch("mindroom.response_runner.should_use_streaming", new=AsyncMock(return_value=False)),
        patch(
            "mindroom.response_runner.team_response_stream",
            new=scripted_team_stream("Team answer"),
        ) as team_stream,
    ):
        coordinator = _build_response_runner(
            bot,
            config=config,
            runtime_paths=runtime_paths,
            storage_path=tmp_path,
            message_target=MessageTarget.resolve("!test:localhost", "$thread-root", "$user_msg"),
            orchestrator=team_orchestrator(config, runtime_paths),
        )

        await coordinator.generate_team_response_helper(
            _response_request(prompt="Plan the launch", user_id="@alice:localhost", thread_id="$thread-root"),
            member_names=["general"],
            team_mode=team_mode,
        )

    spec, _ctx = team_stream.call_args.args
    assert spec.mode is TeamMode(team_mode)


@pytest.mark.asyncio
async def test_process_shutdown_blocks_team_terminal_delivery_after_generation_consumes_cancel(
    tmp_path: Path,
) -> None:
    """A team provider that consumes process cancellation must leave its durable turn for replay."""
    runtime_paths = _runtime_paths(tmp_path)
    config = bind_runtime_paths(_config_with_team(), runtime_paths)
    bot = _make_bot(tmp_path, config=config, runtime_paths=runtime_paths, agent_name="ultimate")
    generation_started = asyncio.Event()

    async def cancellation_resistant_team_response_stream(*_args: object, **_kwargs: object) -> AsyncIterator[str]:
        generation_started.set()
        try:
            await asyncio.Event().wait()
        except asyncio.CancelledError:
            assert current_task_is_process_shutdown()
        yield "late team response"

    with (
        patch("mindroom.response_runner.should_use_streaming", new=AsyncMock(return_value=False)),
        patch("mindroom.response_runner.team_response_stream", new=cancellation_resistant_team_response_stream),
        patch("mindroom.response_lifecycle.apply_post_response_effects", new=AsyncMock(return_value=None)),
    ):
        coordinator = _build_response_runner(
            bot,
            config=config,
            runtime_paths=runtime_paths,
            storage_path=tmp_path,
            message_target=MessageTarget.resolve("!test:localhost", "$thread-root", "$user_msg"),
            orchestrator=team_orchestrator(config, runtime_paths),
        )
        deliver_final = AsyncMock(
            return_value=FinalDeliveryOutcome(
                terminal_status="completed",
                event_id="$team-response",
            ),
        )
        _set_gateway_method(coordinator.deps.delivery_gateway, "deliver_final", deliver_final)
        task = asyncio.create_task(
            coordinator.generate_team_response_helper(
                _response_request(prompt="Hello", user_id="@alice:localhost", thread_id="$thread-root"),
                member_names=["general"],
                team_mode="coordinate",
            ),
        )
        await generation_started.wait()
        request_task_cancel(task, process_shutdown=True)
        with pytest.raises(asyncio.CancelledError):
            await task

    deliver_final.assert_not_awaited()


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("boundary", "expected_phase"),
    [
        ("stream", "streaming_response"),
        ("final", "final_delivery"),
    ],
)
async def test_team_delivery_exposes_fixed_shutdown_phase(
    tmp_path: Path,
    boundary: str,
    expected_phase: str,
) -> None:
    """Real team streaming and final gateway boundaries retain fixed labels."""
    runtime_paths = _runtime_paths(tmp_path)
    config = bind_runtime_paths(_config_with_team(), runtime_paths)
    bot = _make_bot(tmp_path, config=config, runtime_paths=runtime_paths, agent_name="ultimate")
    coordinator = _build_response_runner(
        bot,
        config=config,
        runtime_paths=runtime_paths,
        storage_path=tmp_path,
        message_target=MessageTarget.resolve("!test:localhost", "$thread-root", "$user_msg"),
        orchestrator=team_orchestrator(config, runtime_paths),
    )
    _install_inert_post_response_effects(coordinator)
    boundary_started = asyncio.Event()
    boundary_cancelled = asyncio.Event()
    release_boundary = asyncio.Event()

    async def retained_boundary(_request: object) -> StreamTransportOutcome | FinalDeliveryOutcome:
        boundary_started.set()
        try:
            await asyncio.Event().wait()
        except asyncio.CancelledError:
            boundary_cancelled.set()
            await release_boundary.wait()
            raise

    gateway = coordinator.deps.delivery_gateway
    if boundary == "stream":
        _set_gateway_method(
            gateway,
            "deliver_stream",
            DeliveryGateway.deliver_stream.__get__(gateway, DeliveryGateway),
        )
        _set_gateway_method(gateway, "_deliver_stream", AsyncMock(side_effect=retained_boundary))
    else:
        _set_gateway_method(
            gateway,
            "deliver_stream",
            AsyncMock(return_value=_streamed("$team-stream", "Team answer")),
        )
        _set_gateway_method(gateway, "_finalize_streamed_response", AsyncMock(side_effect=retained_boundary))

    async def fake_team_response_stream(*_args: object, **_kwargs: object) -> AsyncIterator[str]:
        yield "Team answer"

    async def team_response_owner() -> None:
        await coordinator.generate_team_response_helper(
            _response_request(prompt="Hello", user_id="@alice:localhost", thread_id="$thread-root"),
            member_names=["general"],
            team_mode="coordinate",
        )

    with (
        patch("mindroom.response_runner.should_use_streaming", new=AsyncMock(return_value=True)),
        patch("mindroom.response_runner.team_response_stream", new=fake_team_response_stream),
    ):
        response_task = coordinator.inbox.track_inbox_response(
            team_response_owner(),
            name=f"test_team_{boundary}_shutdown_phase",
            recovery_proof_ready=lambda: True,
            room_id="!room:example.org",
        )
        await boundary_started.wait()
        coordinator.inbox.begin_process_shutdown()
        await boundary_cancelled.wait()

        try:
            assert coordinator.inbox.pending_response_phase_counts == {expected_phase: 1}
        finally:
            release_boundary.set()

        assert await coordinator.inbox.drain_inbox_responses(
            cancel_after_seconds=0.1,
            shutdown_intent=ORDERLY_SHUTDOWN,
        )
        await asyncio.gather(response_task, return_exceptions=True)


@pytest.mark.asyncio
async def test_generate_team_response_appends_matrix_tool_prompt_context(tmp_path: Path) -> None:
    """Team Matrix targeting context should reach the model through transient enrichment."""
    runtime_paths = _runtime_paths(tmp_path)
    config = bind_runtime_paths(_config_with_team_matrix_message(), runtime_paths)
    bot = _make_bot(tmp_path, config=config, runtime_paths=runtime_paths, agent_name="ultimate")
    model_messages: list[str] = []
    target_contexts: list[str] = []

    async def fake_team_response_stream(
        _spec: TeamTurnSpec,
        ctx: ResponseTurnContext,
        *,
        sinks: TurnSinks,  # noqa: ARG001
    ) -> AsyncIterator[str]:
        model_messages.append(ctx.prompt)
        target_contexts.extend(
            item.text for item in ctx.transient_enrichment_items if item.key == "matrix_message_target"
        )
        yield "Team answer"

    with (
        patch("mindroom.response_runner.should_use_streaming", new=AsyncMock(return_value=False)),
        patch("mindroom.response_runner.team_response_stream", new=fake_team_response_stream),
    ):
        coordinator = _build_response_runner(
            bot,
            config=config,
            runtime_paths=runtime_paths,
            storage_path=tmp_path,
            message_target=MessageTarget.resolve("!test:localhost", "$thread-root", "$user_msg"),
            orchestrator=team_orchestrator(config, runtime_paths),
        )
        _install_inert_post_response_effects(coordinator)

        await coordinator.generate_team_response_helper(
            _response_request(prompt="Hello", user_id="@alice:localhost", thread_id="$thread-root"),
            member_names=["general"],
            team_mode="coordinate",
        )

    assert model_messages
    assert "[Matrix metadata for tool calls]" not in model_messages[0]
    assert len(target_contexts) == 1
    assert "!test:localhost" in target_contexts[0]
    assert "$thread-root" in target_contexts[0]


@pytest.mark.asyncio
async def test_team_turn_checks_its_own_room_for_redacted_history(tmp_path: Path) -> None:
    """Team turns ask about redactions in the response's own conversation."""
    runtime_paths = _runtime_paths(tmp_path)
    config = bind_runtime_paths(_config_with_team_matrix_message(), runtime_paths)
    bot = _make_bot(tmp_path, config=config, runtime_paths=runtime_paths, agent_name="ultimate")
    lookups: list[tuple[str, tuple[str, ...]]] = []

    async def redacted_history_events(target: MessageTarget, event_ids: tuple[str, ...]) -> dict[str, str | None]:
        lookups.append((target.session_id, event_ids))
        return {}

    async def fake_team_response_stream(
        _spec: TeamTurnSpec,
        ctx: ResponseTurnContext,
        *,
        sinks: TurnSinks,  # noqa: ARG001
    ) -> AsyncIterator[str]:
        assert ctx.redacted_history_events is not None
        await ctx.redacted_history_events(("$event",))
        yield "Team answer"

    with (
        patch("mindroom.response_runner.should_use_streaming", new=AsyncMock(return_value=False)),
        patch("mindroom.response_runner.team_response_stream", new=fake_team_response_stream),
    ):
        coordinator = _build_response_runner(
            bot,
            config=config,
            runtime_paths=runtime_paths,
            storage_path=tmp_path,
            message_target=MessageTarget.resolve("!test:localhost", "$thread-root", "$user_msg"),
            orchestrator=team_orchestrator(config, runtime_paths),
        )
        coordinator.deps = replace(coordinator.deps, redacted_history_events=redacted_history_events)
        _install_inert_post_response_effects(coordinator)

        await coordinator.generate_team_response_helper(
            _response_request(prompt="Hello", user_id="@alice:localhost", thread_id="$thread-root"),
            member_names=["general"],
            team_mode="coordinate",
        )

    assert lookups == [("!test:localhost:$thread-root", ("$event",))]


@pytest.mark.asyncio
async def test_generate_team_response_allows_explicit_private_ad_hoc_member(tmp_path: Path) -> None:
    """ResponseRunner preflight should not reject direct private members before the team runs."""
    runtime_paths = _runtime_paths(tmp_path)
    config = bind_runtime_paths(
        with_current_room_member_access(
            Config(
                agents={
                    "private_worker": AgentConfig(
                        display_name="PrivateWorker",
                        private=AgentPrivateConfig(per="user", root="private_worker_data"),
                    ),
                    "calculator": AgentConfig(display_name="Calculator"),
                },
                models={"default": ModelConfig(provider="openai", id="test-model")},
            ),
        ),
        runtime_paths,
    )
    bot = _make_bot(tmp_path, config=config, runtime_paths=runtime_paths, agent_name="calculator")
    seen_agent_names: list[list[str]] = []

    async def fake_team_response_stream(
        spec: TeamTurnSpec,
        _ctx: ResponseTurnContext,
        *,
        sinks: TurnSinks,  # noqa: ARG001
    ) -> AsyncIterator[str]:
        seen_agent_names.append(list(spec.agent_names))
        yield "Team answer"

    with (
        patch("mindroom.response_runner.should_use_streaming", new=AsyncMock(return_value=False)),
        patch("mindroom.response_runner.team_response_stream", new=fake_team_response_stream),
    ):
        coordinator = _build_response_runner(
            bot,
            config=config,
            runtime_paths=runtime_paths,
            storage_path=tmp_path,
            message_target=MessageTarget.resolve("!test:localhost", "$thread-root", "$user_msg"),
            orchestrator=team_orchestrator(config, runtime_paths),
        )
        _install_inert_post_response_effects(coordinator)

        await coordinator.generate_team_response_helper(
            _response_request(prompt="Hello", user_id="@alice:localhost", thread_id="$thread-root"),
            member_names=["private_worker", "calculator"],
            team_mode="coordinate",
        )

    assert seen_agent_names == [["private_worker", "calculator"]]
    bot._conversation_state_writer.team_history_scope.assert_called_once_with(
        ("private_worker", "calculator"),
        requester_user_id="@alice:localhost",
    )


@pytest.mark.asyncio
@pytest.mark.parametrize("history_boundary_event_id", [None, "$selection"])
async def test_generate_team_response_passes_resolved_correlation_id_to_the_team_turn(
    tmp_path: Path,
    history_boundary_event_id: str | None,
) -> None:
    """Team execution should share the lifecycle/tool-runtime correlation id."""
    runtime_paths = _runtime_paths(tmp_path)
    config = bind_runtime_paths(_config_with_team(), runtime_paths)
    bot = _make_bot(tmp_path, config=config, runtime_paths=runtime_paths, agent_name="ultimate")
    seen_contexts: list[ResponseTurnContext] = []

    async def fake_team_response_stream(
        _spec: TeamTurnSpec,
        ctx: ResponseTurnContext,
        *,
        sinks: TurnSinks,  # noqa: ARG001
    ) -> AsyncIterator[str]:
        seen_contexts.append(ctx)
        yield "Team answer"

    with (
        patch("mindroom.response_runner.should_use_streaming", new=AsyncMock(return_value=False)),
        patch("mindroom.response_runner.team_response_stream", new=fake_team_response_stream),
    ):
        coordinator = _build_response_runner(
            bot,
            config=config,
            runtime_paths=runtime_paths,
            storage_path=tmp_path,
            message_target=MessageTarget.resolve("!test:localhost", "$thread-root", "$original"),
            orchestrator=team_orchestrator(config, runtime_paths),
        )
        _install_inert_post_response_effects(coordinator)

        await coordinator.generate_team_response_helper(
            replace(
                _response_request(
                    prompt="Regenerate team edit",
                    user_id="@alice:localhost",
                    thread_id="$thread-root",
                    reply_to_event_id="$original",
                    correlation_id="$edit",
                ),
                history_boundary_event_id=history_boundary_event_id,
            ),
            member_names=["general"],
            team_mode="coordinate",
        )

    [ctx] = seen_contexts
    assert ctx.reply_to_event_id == "$original"
    assert ctx.correlation_id == "$edit"
    assert ctx.history_boundary_event_id == history_boundary_event_id


@pytest.mark.asyncio
async def test_generate_team_response_preserves_model_prompt_in_persisted_session(
    tmp_path: Path,
) -> None:
    """Team persisted model prompts should stay intact for later provider cache reuse."""
    runtime_paths = _runtime_paths(tmp_path)
    config = bind_runtime_paths(_config_with_team(), runtime_paths)
    bot = _make_bot(tmp_path, config=config, runtime_paths=runtime_paths, agent_name="ultimate")
    store = _team_store(config, runtime_paths)

    async def fake_team_response_stream(
        _spec: TeamTurnSpec,
        ctx: ResponseTurnContext,
        *,
        sinks: TurnSinks,
    ) -> AsyncIterator[str]:
        model_message = ctx.prompt
        run_id = ctx.run_id
        assert run_id is not None
        assert sinks.turn_recorder is not None
        sinks.turn_recorder.record_completed(
            run_id=run_id,
            run_metadata=None,
            assistant_text="Team answer",
            completed_tools=(),
        )
        seed_session(
            store,
            _team_session(
                runs=[
                    TeamRunRecord(
                        run_id=run_id,
                        content="Team answer",
                        messages=[
                            Message(role="user", content="Earlier context"),
                            Message(role="assistant", content="Earlier answer"),
                            Message(role="user", content=model_message),
                            Message(role="assistant", content="Team answer"),
                        ],
                        team_id=None,
                    ),
                ],
            ),
        )
        yield "Team answer"

    with (
        patch("mindroom.response_runner.should_use_streaming", new=AsyncMock(return_value=False)),
        patch("mindroom.response_runner.team_response_stream", new=fake_team_response_stream),
    ):
        coordinator = _build_response_runner(
            bot,
            config=config,
            runtime_paths=runtime_paths,
            storage_path=tmp_path,
            team_history_storage=store,
            message_target=MessageTarget.resolve("!test:localhost", "$thread-root", "$user_msg"),
            orchestrator=team_orchestrator(config, runtime_paths),
        )

        await coordinator.generate_team_response_helper(
            replace(
                _response_request(prompt="Describe this image", user_id="@alice:localhost", thread_id="$thread-root"),
                model_prompt="Available attachment IDs: att_1. Use tool calls to inspect or process them.",
            ),
            member_names=["general"],
            team_mode="coordinate",
        )

    [persisted_run] = _persisted_runs(store)
    assert persisted_run.messages is not None
    assert persisted_run.messages[0].content == "Earlier context"
    assert "Describe this image" in cast("str", persisted_run.messages[2].content)
    assert "Available attachment IDs: att_1" in cast("str", persisted_run.messages[2].content)


@pytest.mark.asyncio
async def test_generate_team_response_preserves_model_prompt(tmp_path: Path) -> None:
    """Team runs should keep the model-facing prompt that Agno persisted."""
    runtime_paths = _runtime_paths(tmp_path)
    config = bind_runtime_paths(_config_with_team(), runtime_paths)
    bot = _make_bot(tmp_path, config=config, runtime_paths=runtime_paths, agent_name="ultimate")
    store = _team_store(config, runtime_paths)
    seen_run_ids: list[str | None] = []

    async def fake_team_response_stream(
        _spec: TeamTurnSpec,
        ctx: ResponseTurnContext,
        *,
        sinks: TurnSinks,
    ) -> AsyncIterator[str]:
        model_message = ctx.prompt
        run_id = ctx.run_id
        seen_run_ids.append(run_id)
        assert run_id is not None
        assert sinks.turn_recorder is not None
        sinks.turn_recorder.record_completed(
            run_id=run_id,
            run_metadata=None,
            assistant_text="Team answer",
            completed_tools=(),
        )
        seed_session(
            store,
            _team_session(
                runs=[
                    TeamRunRecord(
                        run_id=run_id,
                        content="Team answer",
                        messages=[
                            Message(role="user", content=model_message),
                            Message(role="assistant", content="Team answer"),
                        ],
                        team_id=None,
                    ),
                ],
            ),
        )
        yield "Team answer"

    with (
        patch("mindroom.response_runner.should_use_streaming", new=AsyncMock(return_value=False)),
        patch("mindroom.response_runner.team_response_stream", new=fake_team_response_stream),
    ):
        coordinator = _build_response_runner(
            bot,
            config=config,
            runtime_paths=runtime_paths,
            storage_path=tmp_path,
            team_history_storage=store,
            message_target=MessageTarget.resolve("!test:localhost", "$thread-root", "$user_msg"),
            orchestrator=team_orchestrator(config, runtime_paths),
        )

        coordinator.deps.delivery_gateway.send_text.return_value = "$msg"

        await coordinator.generate_team_response_helper(
            replace(
                _response_request(prompt="Describe this image", user_id="@alice:localhost", thread_id="$thread-root"),
                model_prompt="Available attachment IDs: att_1. Use tool calls to inspect or process them.",
            ),
            member_names=["general"],
            team_mode="coordinate",
        )

    [persisted_run] = _persisted_runs(store)
    assert seen_run_ids == [persisted_run.run_id]
    assert persisted_run.messages is not None
    assert "Describe this image" in cast("str", persisted_run.messages[0].content)
    assert "Available attachment IDs: att_1" in cast("str", persisted_run.messages[0].content)


@pytest.mark.asyncio
@pytest.mark.parametrize("streaming", [False, True])
async def test_team_session_started_fires_once_for_the_reply_that_creates_its_session(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    streaming: bool,
) -> None:
    """The first team reply's own run save creates the team session; its hooks run once, before it is reported."""
    runtime_paths = _runtime_paths(tmp_path)
    config = bind_runtime_paths(_config_with_team(), runtime_paths)
    bot = _make_bot(tmp_path, config=config, runtime_paths=runtime_paths, agent_name="ultimate")
    store = _team_store(config, runtime_paths)
    sequence: list[str] = []

    @hook(EVENT_SESSION_STARTED)
    async def started(ctx: SessionHookContext) -> None:
        sequence.append(f"started:{ctx.scope.key}:{ctx.agent_name}:{ctx.session_id}:{ctx.thread_id}")

    @hook(EVENT_MESSAGE_AFTER_RESPONSE)
    async def after(ctx: AfterResponseContext) -> None:
        sequence.append(f"after:{ctx.result.envelope.body}")

    # The leader answers both turns itself; the member never runs.
    install_model_providers(monkeypatch, {"default": ScriptedModel([answer("Team hello"), answer("Team again")])})
    orchestrator = team_orchestrator(config, runtime_paths)
    with (
        patch("mindroom.response_runner.should_use_streaming", new=AsyncMock(return_value=streaming)),
        patch("mindroom.response_lifecycle.apply_post_response_effects", new=AsyncMock(return_value=None)),
    ):
        coordinator = _build_response_runner(
            bot,
            config=config,
            runtime_paths=runtime_paths,
            storage_path=tmp_path,
            hook_registry=HookRegistry.from_plugins([_hook_plugin("session-hooks", [started, after])]),
            team_history_storage=store,
            message_target=MessageTarget.resolve("!test:localhost", "$thread-root", "$user_msg"),
            orchestrator=orchestrator,
        )
        unique_room_send_responses(bot.client)
        streamed = iter(range(1, 100))

        async def consume_delivery(request: StreamingDeliveryRequest) -> StreamTransportOutcome:
            rendered = "".join([str(chunk) async for chunk in request.response_stream])
            event_id = f"$streamed{next(streamed)}"
            request.visible_event_id_callback(event_id)
            return _streamed(event_id, rendered)

        coordinator.deps.delivery_gateway.deliver_stream.side_effect = consume_delivery

        for prompt, event_id in (("Hello", "$user_msg"), ("Hello again", "$user_msg2")):
            await coordinator.generate_team_response_helper(
                _response_request(
                    prompt=prompt,
                    reply_to_event_id=event_id,
                    user_id="@alice:localhost",
                    thread_id="$thread-root",
                ),
                member_names=["general"],
                team_mode="coordinate",
            )

    assert sequence == [
        f"started:team:ultimate:ultimate:{TEAM_SESSION_ID}:$thread-root",
        "after:Hello",
        "after:Hello again",
    ]
    assert len(_persisted_runs(store)) == 2
    # Both replies ran the delivery mode under test.
    assert coordinator.deps.delivery_gateway.deliver_stream.await_count == (2 if streaming else 0)


@pytest.mark.asyncio
@pytest.mark.parametrize("stored_by", ["run", "interrupted record"])
async def test_generate_team_response_helper_streaming_emits_session_started_after_persisted_delivery_error(
    tmp_path: Path,
    stored_by: str,
) -> None:
    """session:started fires for a team stream that fails after its session persisted, by the run or its record.

    The delivery closes the stream when it fails; the turn driver then stores the stopped turn's record off the event
    loop, which creates the session row when the run wrote none.
    """
    runtime_paths = _runtime_paths(tmp_path)
    config = bind_runtime_paths(_config_with_team(), runtime_paths)
    bot = MagicMock(spec=AgentBot)
    bot.logger = MagicMock()
    bot.client = AsyncMock()
    bot.agent_name = "ultimate"
    bot.storage_path = tmp_path
    bot.config = config
    bot.runtime_paths = runtime_paths
    bot._knowledge_access_support = _knowledge_access_support()

    store = _team_store(config, runtime_paths)
    sequence: list[str] = []

    @hook(EVENT_SESSION_STARTED)
    async def started(ctx: SessionHookContext) -> None:
        sequence.append(f"started:{ctx.scope.key}:{ctx.session_id}:{ctx.thread_id}")

    registry = HookRegistry.from_plugins([_hook_plugin("session-hooks", [started])])

    def run_writes() -> None:
        if stored_by == "run":
            _start_team_session(store, TEAM_SESSION_ID)
        sequence.append("stream")

    with (
        patch("mindroom.response_runner.should_use_streaming", new=AsyncMock(return_value=True)),
        patch(
            "mindroom.response_runner.team_response_stream",
            new=_driven_team_stream(config, runtime_paths, _shows_then_waits(before=run_writes)),
        ),
    ):
        coordinator = _build_response_runner(
            bot,
            config=config,
            runtime_paths=runtime_paths,
            storage_path=tmp_path,
            hook_registry=registry,
            team_history_storage=store,
            message_target=MessageTarget.resolve("!test:localhost", "$thread-root", "$user_msg"),
            orchestrator=team_orchestrator(config, runtime_paths),
        )

        async def deliver_and_fail(request: StreamingDeliveryRequest) -> StreamTransportOutcome:
            sequence.append("deliver")
            return await _fail_after_the_first_chunk(request)

        coordinator.deps.delivery_gateway.deliver_stream.side_effect = deliver_and_fail
        request = replace(
            _response_request(prompt="Hello", user_id="@alice:localhost", thread_id="$thread-root"),
            existing_event_id="$placeholder",
        )

        resolution = await coordinator.generate_team_response_helper(
            request,
            member_names=["general"],
            team_mode="coordinate",
        )

    # The reply's own event: the placeholder its stream and notes write into.
    assert resolution == "$thinking"
    assert sequence == [
        "deliver",
        "stream",
        "started:team:ultimate:!test:localhost:$thread-root:$thread-root",
    ]
    assert store.load_session(TEAM_SESSION_ID) is not None


@pytest.mark.asyncio
async def test_generate_team_response_helper_streaming_delivery_carries_live_metadata_collector(
    tmp_path: Path,
) -> None:
    """The team streaming delivery request carries the live metadata collector.

    The turn driver fills the collector at terminal settle, before the
    stream's final edit snapshots extra_content; without the live dict the
    ai_run payload never reaches Matrix in streaming mode (the finalize
    happy path sends no extra edit).
    """
    runtime_paths = _runtime_paths(tmp_path)
    config = bind_runtime_paths(_config_with_team(), runtime_paths)
    bot = MagicMock(spec=AgentBot)
    bot.logger = MagicMock()
    bot.client = AsyncMock()
    bot.agent_name = "ultimate"
    bot.storage_path = tmp_path
    bot.config = config
    bot.runtime_paths = runtime_paths
    bot._knowledge_access_support = _knowledge_access_support()

    captured_extra_content: list[object] = []

    with (
        patch("mindroom.response_runner.should_use_streaming", new=AsyncMock(return_value=True)),
        patch("mindroom.response_runner.team_response_stream") as mock_team_stream,
    ):
        coordinator = _build_response_runner(
            bot,
            config=config,
            runtime_paths=runtime_paths,
            storage_path=tmp_path,
            message_target=MessageTarget.resolve("!test:localhost", "$thread-root", "$user_msg"),
            orchestrator=team_orchestrator(config, runtime_paths),
        )

        async def consume_delivery(request: object) -> StreamTransportOutcome:
            async for _chunk in request.response_stream:
                pass
            # Snapshot after stream exhaustion, like the real final edit.
            captured_extra_content.append(request.extra_content)
            return _streamed("$team-final", "Team hello")

        coordinator.deps.delivery_gateway.deliver_stream.side_effect = consume_delivery

        def fake_team_response_stream(
            _spec: TeamTurnSpec,
            _ctx: ResponseTurnContext,
            *,
            sinks: TurnSinks,
        ) -> AsyncIterator[str]:
            async def fake_stream() -> AsyncIterator[str]:
                collector = sinks.run_metadata_collector
                assert isinstance(collector, dict)
                collector["io.mindroom.ai_run"] = {"usage": {"output_tokens": 5}}
                yield "Team hello"

            return fake_stream()

        mock_team_stream.side_effect = fake_team_response_stream

        await coordinator.generate_team_response_helper(
            _response_request(prompt="Hello", user_id="@alice:localhost", thread_id="$thread-root"),
            member_names=["general"],
            team_mode="coordinate",
        )

    assert captured_extra_content == [{"io.mindroom.ai_run": {"usage": {"output_tokens": 5}}}]


@pytest.mark.asyncio
async def test_generate_team_response_helper_persists_interrupted_history_when_stream_delivery_fails(
    tmp_path: Path,
) -> None:
    """A team stream whose delivery fails stores the stopped turn's canonical interrupted replay from its partial text.

    The delivery closes the stream when it fails, so the turn driver records the turn as stopped, naming the reply's
    event.
    """
    runtime_paths = _runtime_paths(tmp_path)
    config = bind_runtime_paths(_config_with_team(), runtime_paths)
    bot = _make_bot(tmp_path, config=config, runtime_paths=runtime_paths, agent_name="ultimate")
    store = _team_store(config, runtime_paths)

    with (
        patch("mindroom.response_runner.should_use_streaming", new=AsyncMock(return_value=True)),
        patch(
            "mindroom.response_runner.team_response_stream",
            new=_driven_team_stream(config, runtime_paths, _shows_then_waits()),
        ),
    ):
        coordinator = _build_response_runner(
            bot,
            config=config,
            runtime_paths=runtime_paths,
            storage_path=tmp_path,
            hook_registry=HookRegistry.empty(),
            team_history_storage=store,
            message_target=MessageTarget.resolve("!test:localhost", "$thread-root", "$user_msg"),
            orchestrator=team_orchestrator(config, runtime_paths),
        )
        _install_inert_post_response_effects(coordinator)
        coordinator.deps.delivery_gateway.deliver_stream.side_effect = _fail_after_the_first_chunk

        resolution = await coordinator.generate_team_response_helper(
            _response_request(prompt="Hello", user_id="@alice:localhost", thread_id="$thread-root"),
            member_names=["general"],
            team_mode="coordinate",
        )

    # The reply's own event: the placeholder its stream and notes write into.
    assert resolution == "$thinking"
    [persisted_run] = _persisted_runs(store)
    _assert_interrupted_messages(
        persisted_run,
        response_event_id="$thinking",
        assistant_body="Team hello\n\n(turn stopped before completion)",
    )


@pytest.mark.asyncio
async def test_generate_team_response_helper_persists_minimal_interrupted_history_after_task_cancel(
    tmp_path: Path,
) -> None:
    """Lifecycle-owned team cancellation should persist one minimal interrupted turn."""
    runtime_paths = _runtime_paths(tmp_path)
    config = bind_runtime_paths(_config_with_team(), runtime_paths)
    bot = MagicMock(spec=AgentBot)
    bot.logger = MagicMock()
    bot.client = AsyncMock()
    bot.agent_name = "ultimate"
    bot.storage_path = tmp_path
    bot.config = config
    bot.runtime_paths = runtime_paths
    bot._knowledge_access_support = _knowledge_access_support()

    store = _team_store(config, runtime_paths)
    started = asyncio.Event()

    async def waits_until_cancelled(_run: TurnRunState) -> AsyncIterator[str | AttemptResolved]:
        started.set()
        await asyncio.Event().wait()
        yield AttemptResolved(CompletedAttempt())

    async def fake_run_cancellable_response(**kwargs: object) -> str:
        response_function = cast("Callable[[str | None], Awaitable[object]]", kwargs["response_function"])
        on_cancelled = cast("Callable[[str], None]", kwargs["on_cancelled"])
        task = asyncio.create_task(response_function("$thinking"))
        await started.wait()
        task.cancel()
        try:
            await task
        except asyncio.CancelledError as cancelled:
            # The attempt runner reports how its attempt was cancelled, as a Stop's reaction or a restart does.
            on_cancelled(cancel_failure_reason(classify_cancel_source(cancelled)))
        return "$thinking"

    with (
        patch.object(
            ResponseRunner,
            "_run_cancellable_response",
            new=AsyncMock(side_effect=fake_run_cancellable_response),
        ),
        patch("mindroom.response_runner.should_use_streaming", new=AsyncMock(return_value=False)),
        patch("mindroom.response_lifecycle.apply_post_response_effects", new=AsyncMock(return_value=None)),
        patch(
            "mindroom.response_runner.team_response_stream",
            new=_driven_team_stream(config, runtime_paths, waits_until_cancelled),
        ),
    ):
        coordinator = _build_response_runner(
            bot,
            config=config,
            runtime_paths=runtime_paths,
            storage_path=tmp_path,
            team_history_storage=store,
            message_target=MessageTarget.resolve("!test:localhost", "$thread-root", "$user_msg"),
            orchestrator=team_orchestrator(config, runtime_paths),
        )
        _set_gateway_method(coordinator.deps.delivery_gateway, "edit_text", AsyncMock())

        resolution = await coordinator.generate_team_response_helper(
            _response_request(prompt="Hello", user_id="@alice:localhost", thread_id="$thread-root"),
            member_names=["general"],
            team_mode="coordinate",
        )

    # An interruption whose note did not land leaves its turn to replay.
    assert resolution is None
    [persisted_run] = _persisted_runs(store)
    # The interrupted run belongs to the team whose history scope stored it.
    assert persisted_run.team_id == "ultimate"
    _assert_interrupted_messages(
        persisted_run,
        response_event_id="$thinking",
        assistant_body="(turn stopped before completion)",
    )


@pytest.mark.asyncio
async def test_generate_team_response_helper_persists_interrupted_history_when_final_delivery_is_cancelled(
    tmp_path: Path,
) -> None:
    """Delivery cancellation after a completed provider run must not create replay."""
    runtime_paths = _runtime_paths(tmp_path)
    config = bind_runtime_paths(_config_with_team(), runtime_paths)
    bot = _make_bot(tmp_path, config=config, runtime_paths=runtime_paths, agent_name="ultimate")
    store = _team_store(config, runtime_paths)

    with (
        patch("mindroom.response_runner.should_use_streaming", new=AsyncMock(return_value=False)),
        patch("mindroom.response_lifecycle.apply_post_response_effects", new=AsyncMock(return_value=None)),
        patch(
            "mindroom.response_runner.team_response_stream",
            new=scripted_team_stream("🤝 Team Response:\n\nTeam hello", completes=True),
        ),
    ):
        coordinator = _build_response_runner(
            bot,
            config=config,
            runtime_paths=runtime_paths,
            storage_path=tmp_path,
            team_history_storage=store,
            message_target=MessageTarget.resolve("!test:localhost", "$thread-root", "$user_msg"),
            orchestrator=team_orchestrator(config, runtime_paths),
        )
        _set_gateway_method(
            coordinator.deps.delivery_gateway,
            "deliver_final",
            AsyncMock(side_effect=asyncio.CancelledError("delivery cancel")),
        )

        resolution = await coordinator.generate_team_response_helper(
            _response_request(prompt="Hello", user_id="@alice:localhost", thread_id="$thread-root"),
            member_names=["general"],
            team_mode="coordinate",
        )

    # The reply's own event: the placeholder its stream and notes write into.
    # An interruption whose note did not land leaves its turn to replay.
    assert resolution is None
    assert store.load_session(TEAM_SESSION_ID) is None


@pytest.mark.asyncio
async def test_generate_team_response_helper_preserves_visible_stream_when_finalize_returns_cancelled(
    tmp_path: Path,
) -> None:
    """Delivery-stage cancellation after a completed team stream must not create replay."""
    runtime_paths = _runtime_paths(tmp_path)
    config = bind_runtime_paths(_config_with_team(), runtime_paths)
    bot = _make_bot(tmp_path, config=config, runtime_paths=runtime_paths, agent_name="ultimate")
    store = _team_store(config, runtime_paths)

    with (
        patch("mindroom.response_runner.should_use_streaming", new=AsyncMock(return_value=True)),
        patch("mindroom.response_lifecycle.apply_post_response_effects", new=AsyncMock(return_value=None)),
        patch(
            "mindroom.response_runner.team_response_stream",
            new=scripted_team_stream("Team hello", completes=True),
        ),
    ):
        coordinator = _build_response_runner(
            bot,
            config=config,
            runtime_paths=runtime_paths,
            storage_path=tmp_path,
            team_history_storage=store,
            message_target=MessageTarget.resolve("!test:localhost", "$thread-root", "$user_msg"),
            orchestrator=team_orchestrator(config, runtime_paths),
        )
        _set_gateway_method(
            coordinator.deps.delivery_gateway,
            "finalize_streamed_response",
            AsyncMock(
                return_value=FinalDeliveryOutcome(
                    terminal_status="cancelled",
                    event_id="$team-msg",
                    is_visible_response=True,
                    final_visible_body="Team hello",
                    delivery_kind="sent",
                    failure_reason="delivery_cancelled",
                ),
            ),
        )

        async def consume_stream(request: object) -> StreamTransportOutcome:
            accumulated = ""
            async for chunk in request.response_stream:
                accumulated += str(chunk)
            return _streamed("$team-msg", accumulated)

        _set_gateway_method(
            coordinator.deps.delivery_gateway,
            "deliver_stream",
            AsyncMock(side_effect=consume_stream),
        )

        resolution = await coordinator.generate_team_response_helper(
            _response_request(prompt="Hello", user_id="@alice:localhost", thread_id="$thread-root"),
            member_names=["general"],
            team_mode="coordinate",
        )

    # The reply's own event: the placeholder its stream and notes write into.
    # An interruption whose note did not land leaves its turn to replay.
    assert resolution is None
    assert store.load_session(TEAM_SESSION_ID) is None


@pytest.mark.asyncio
async def test_generate_team_response_helper_stores_a_structured_prompt_as_written_when_its_stream_is_closed(
    tmp_path: Path,
) -> None:
    """A structured team prompt is stored as the model read it when a closed stream leaves its turn interrupted."""
    runtime_paths = _runtime_paths(tmp_path)
    config = bind_runtime_paths(_config_with_team(), runtime_paths)
    bot = _make_bot(tmp_path, config=config, runtime_paths=runtime_paths, agent_name="ultimate")
    store = _team_store(config, runtime_paths)
    structured_prompt = "\n".join(
        (
            "<messages>",
            render_msg_tag(sender="@alice:localhost", body="First", event_id="$first"),
            render_msg_tag(sender="@alice:localhost", body="Hello", event_id="$user_msg"),
            "</messages>",
        ),
    )
    persisted_prompt = f"{structured_prompt}\n\n<mindroom_message_context>persist me</mindroom_message_context>"
    request = replace(
        _response_request(
            prompt=structured_prompt,
            model_prompt=persisted_prompt,
            user_id="@alice:localhost",
            thread_id="$thread-root",
        ),
        current_prompt_is_structured=True,
    )

    with (
        patch("mindroom.response_runner.should_use_streaming", new=AsyncMock(return_value=True)),
        patch("mindroom.response_lifecycle.apply_post_response_effects", new=AsyncMock(return_value=None)),
        patch(
            "mindroom.response_runner.team_response_stream",
            new=_driven_team_stream(config, runtime_paths, _shows_then_waits()),
        ),
    ):
        coordinator = _build_response_runner(
            bot,
            config=config,
            runtime_paths=runtime_paths,
            storage_path=tmp_path,
            team_history_storage=store,
            message_target=MessageTarget.resolve("!test:localhost", "$thread-root", "$user_msg"),
            orchestrator=team_orchestrator(config, runtime_paths),
        )
        _set_gateway_method(
            coordinator.deps.delivery_gateway,
            "deliver_stream",
            AsyncMock(side_effect=_fail_after_the_first_chunk),
        )

        resolution = await coordinator.generate_team_response_helper(
            request,
            member_names=["general"],
            team_mode="coordinate",
        )

    # The reply's own event: the placeholder its stream and notes write into.
    assert resolution == "$thinking"
    [persisted_run] = _persisted_runs(store)
    assert persisted_run.messages is not None
    assert [message.role for message in persisted_run.messages] == ["user", "assistant"]
    assert persisted_run.messages[0].content == persisted_prompt
    assert persisted_run.metadata is not None
    assert persisted_run.metadata["matrix_response_event_id"] == "$thinking"
    assert persisted_run.messages[1].content == "Team hello\n\n(turn stopped before completion)"


@pytest.mark.asyncio
async def test_generate_team_response_helper_preserves_visible_stream_on_late_finalize_error(
    tmp_path: Path,
) -> None:
    """Late streamed team finalization errors should preserve the visible stream as an error outcome."""
    runtime_paths = _runtime_paths(tmp_path)
    config = bind_runtime_paths(_config_with_team(), runtime_paths)
    bot = _make_bot(tmp_path, config=config, runtime_paths=runtime_paths, agent_name="ultimate")
    store = _team_store(config, runtime_paths)

    with (
        patch("mindroom.response_runner.should_use_streaming", new=AsyncMock(return_value=True)),
        patch("mindroom.response_lifecycle.apply_post_response_effects", new=AsyncMock(return_value=None)),
        patch("mindroom.response_runner.team_response_stream", new=scripted_team_stream("Team hello")),
    ):
        coordinator = _build_response_runner(
            bot,
            config=config,
            runtime_paths=runtime_paths,
            storage_path=tmp_path,
            team_history_storage=store,
            message_target=MessageTarget.resolve("!test:localhost", "$thread-root", "$user_msg"),
            orchestrator=team_orchestrator(config, runtime_paths),
        )
        _set_gateway_method(
            coordinator.deps.delivery_gateway,
            "finalize_streamed_response",
            AsyncMock(
                return_value=FinalDeliveryOutcome(
                    terminal_status="error",
                    event_id="$team-msg",
                    is_visible_response=True,
                    final_visible_body="Team hello",
                    delivery_kind="sent",
                    failure_reason="delivery crash",
                ),
            ),
        )

        async def consume_stream(request: object) -> StreamTransportOutcome:
            accumulated = ""
            async for chunk in request.response_stream:
                accumulated += str(chunk)
            return _streamed("$team-msg", accumulated)

        _set_gateway_method(
            coordinator.deps.delivery_gateway,
            "deliver_stream",
            AsyncMock(side_effect=consume_stream),
        )

        resolution = await coordinator.generate_team_response_helper(
            _response_request(prompt="Hello", user_id="@alice:localhost", thread_id="$thread-root"),
            member_names=["general"],
            team_mode="coordinate",
        )

    # The reply's own event: the placeholder its stream and notes write into.
    assert resolution == "$thinking"


@pytest.mark.asyncio
async def test_generate_team_response_helper_settles_late_failure_without_finalize(
    tmp_path: Path,
) -> None:
    """Raw late team failures settle a bare terminal error without finalize.

    Mirrors the agent arm: the tracked event must not be routed through the
    placeholder-only cleanup, because an adopted thinking-message stream can
    already hold the full streamed reply under the same event id.
    """
    runtime_paths = _runtime_paths(tmp_path)
    config = bind_runtime_paths(_config_with_team(), runtime_paths)
    bot = _make_bot(tmp_path, config=config, runtime_paths=runtime_paths, agent_name="ultimate")

    with (
        patch("mindroom.response_runner.should_use_streaming", new=AsyncMock(return_value=True)),
        patch("mindroom.response_lifecycle.apply_post_response_effects", new=AsyncMock(return_value=None)),
        patch("mindroom.response_runner.team_response_stream"),
    ):
        coordinator = _build_response_runner(
            bot,
            config=config,
            runtime_paths=runtime_paths,
            storage_path=tmp_path,
            message_target=MessageTarget.resolve("!test:localhost", "$thread-root", "$user_msg"),
            orchestrator=team_orchestrator(config, runtime_paths),
        )
        _set_gateway_method(
            coordinator.deps.delivery_gateway,
            "deliver_stream",
            AsyncMock(side_effect=RuntimeError("stream boom")),
        )
        _set_gateway_method(
            coordinator.deps.delivery_gateway,
            "finalize_streamed_response",
            AsyncMock(
                return_value=FinalDeliveryOutcome(
                    terminal_status="error",
                    event_id=None,
                    failure_reason="stream boom",
                ),
            ),
        )

        resolution = await coordinator.generate_team_response_helper(
            _response_request(prompt="Hello", user_id="@alice:localhost", thread_id="$thread-root"),
            member_names=["general"],
            team_mode="coordinate",
        )

    # The reply's own event: the placeholder its stream and notes write into.
    assert resolution == "$thinking"
    coordinator.deps.delivery_gateway.finalize_streamed_response.assert_not_awaited()
    coordinator.deps.delivery_gateway.deps.response_hooks.emit_cancelled_response.assert_awaited_once()


@pytest.mark.asyncio
async def test_generate_team_response_helper_persists_original_user_message_for_cancelled_team_run(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Cancelled team replay should store the raw user turn, not the shaped model prompt."""
    runtime_paths = _runtime_paths(tmp_path)
    config = bind_runtime_paths(_config_with_team(), runtime_paths)
    bot = _make_bot(tmp_path, config=config, runtime_paths=runtime_paths, agent_name="ultimate")
    store = _team_store(config, runtime_paths)

    async def stopped() -> None:
        reason = "user_stop"
        raise asyncio.CancelledError(reason)

    # The leader is stopped while its first request streams; the member never runs.
    leader = ScriptedModel([[Await(stopped)]])
    install_model_providers(monkeypatch, {"default": leader})

    async def fake_run_cancellable_response(**kwargs: object) -> str:
        response_function = cast("Callable[[str | None], Awaitable[object]]", kwargs["response_function"])
        on_cancelled = cast("Callable[[str], None] | None", kwargs.get("on_cancelled"))
        with suppress(asyncio.CancelledError):
            await response_function("$thinking")
        if on_cancelled is not None:
            on_cancelled("cancelled_by_user")
        return "$thinking"

    with (
        patch.object(
            ResponseRunner,
            "_run_cancellable_response",
            new=AsyncMock(side_effect=fake_run_cancellable_response),
        ),
        patch("mindroom.response_runner.should_use_streaming", new=AsyncMock(return_value=False)),
        patch("mindroom.response_lifecycle.apply_post_response_effects", new=AsyncMock(return_value=None)),
    ):
        orchestrator = team_orchestrator(config, runtime_paths)
        coordinator = _build_response_runner(
            bot,
            config=config,
            runtime_paths=runtime_paths,
            storage_path=tmp_path,
            team_history_storage=store,
            message_target=MessageTarget.resolve("!test:localhost", "$thread-root", "$user_msg"),
            orchestrator=orchestrator,
        )

        resolution = await coordinator.generate_team_response_helper(
            _response_request(prompt="Hello", user_id="@alice:localhost", thread_id="$thread-root"),
            member_names=["general"],
            team_mode="coordinate",
        )

    # The reply's own event: the placeholder its stream and notes write into.
    assert resolution == "$thinking"
    [request] = leader.requests
    model_prompt = request.messages[-1].content
    assert isinstance(model_prompt, str)
    assert model_prompt != "Hello"
    assert 'Current message:\n<msg event_id="$user_msg" from="@alice:localhost">' in model_prompt
    assert "Hello" in model_prompt
    [persisted_run] = _persisted_runs(store)
    _assert_interrupted_messages(
        persisted_run,
        response_event_id="$thinking",
        assistant_body="(turn stopped before completion)",
    )


@pytest.mark.asyncio
async def test_generate_team_response_helper_emits_session_started_after_persisted_cancellation(
    tmp_path: Path,
) -> None:
    """session:started should still fire when a cancelled team run has already persisted the session."""
    runtime_paths = _runtime_paths(tmp_path)
    config = bind_runtime_paths(_config_with_team(), runtime_paths)
    bot = MagicMock(spec=AgentBot)
    bot.logger = MagicMock()
    bot.client = AsyncMock()
    bot.agent_name = "ultimate"
    bot.storage_path = tmp_path
    bot.config = config
    bot.runtime_paths = runtime_paths
    bot._knowledge_access_support = _knowledge_access_support()

    store = _team_store(config, runtime_paths)
    sequence: list[str] = []

    @hook(EVENT_SESSION_STARTED)
    async def started(ctx: SessionHookContext) -> None:
        sequence.append(f"started:{ctx.scope.key}:{ctx.session_id}:{ctx.thread_id}")

    registry = HookRegistry.from_plugins([_hook_plugin("session-hooks", [started])])

    async def fake_team_response_stream(
        _spec: TeamTurnSpec,
        ctx: ResponseTurnContext,
        *,
        sinks: TurnSinks,  # noqa: ARG001
    ) -> AsyncIterator[str]:
        cancel_message = "cancel"
        session_id = ctx.session_id
        assert isinstance(session_id, str)
        _start_team_session(store, session_id)
        sequence.append("team")
        raise asyncio.CancelledError(cancel_message)
        yield

    async def fake_run_cancellable_response(**kwargs: object) -> str:
        response_function = cast("Callable[[str | None], Awaitable[object]]", kwargs["response_function"])
        on_cancelled = cast("Callable[[str], None]", kwargs["on_cancelled"])
        try:
            await response_function("$thinking")
        except asyncio.CancelledError as cancelled:
            # The attempt runner reports how its attempt was cancelled, as a Stop's reaction or a restart does.
            on_cancelled(cancel_failure_reason(classify_cancel_source(cancelled)))
        return "$thinking"

    with (
        patch.object(
            ResponseRunner,
            "_run_cancellable_response",
            new=AsyncMock(side_effect=fake_run_cancellable_response),
        ),
        patch("mindroom.response_runner.should_use_streaming", new=AsyncMock(return_value=False)),
        patch("mindroom.response_lifecycle.apply_post_response_effects", new=AsyncMock(return_value=None)),
        patch("mindroom.response_runner.team_response_stream", new=fake_team_response_stream),
    ):
        coordinator = _build_response_runner(
            bot,
            config=config,
            runtime_paths=runtime_paths,
            storage_path=tmp_path,
            hook_registry=registry,
            team_history_storage=store,
            message_target=MessageTarget.resolve("!test:localhost", "$thread-root", "$user_msg"),
            orchestrator=team_orchestrator(config, runtime_paths),
        )
        _set_gateway_method(coordinator.deps.delivery_gateway, "edit_text", AsyncMock())

        resolution = await coordinator.generate_team_response_helper(
            _response_request(prompt="Hello", user_id="@alice:localhost", thread_id="$thread-root"),
            member_names=["general"],
            team_mode="coordinate",
        )

    # An interruption whose note did not land leaves its turn to replay.
    assert resolution is None
    assert sequence == [
        "team",
        "started:team:ultimate:!test:localhost:$thread-root:$thread-root",
    ]


@pytest.mark.asyncio
async def test_generate_team_response_helper_streaming_emits_session_started_after_persisted_cancellation(
    tmp_path: Path,
) -> None:
    """session:started should still fire when a cancelled team stream has already persisted the session."""
    runtime_paths = _runtime_paths(tmp_path)
    config = bind_runtime_paths(_config_with_team(), runtime_paths)
    bot = MagicMock(spec=AgentBot)
    bot.logger = MagicMock()
    bot.client = AsyncMock()
    bot.agent_name = "ultimate"
    bot.storage_path = tmp_path
    bot.config = config
    bot.runtime_paths = runtime_paths
    bot._knowledge_access_support = _knowledge_access_support()

    store = _team_store(config, runtime_paths)
    sequence: list[str] = []

    @hook(EVENT_SESSION_STARTED)
    async def started(ctx: SessionHookContext) -> None:
        sequence.append(f"started:{ctx.scope.key}:{ctx.session_id}:{ctx.thread_id}")

    registry = HookRegistry.from_plugins([_hook_plugin("session-hooks", [started])])

    async def fake_run_cancellable_response(**kwargs: object) -> str:
        response_function = cast("Callable[[str | None], Awaitable[object]]", kwargs["response_function"])
        on_cancelled = cast("Callable[[str], None] | None", kwargs.get("on_cancelled"))
        with suppress(asyncio.CancelledError):
            await response_function("$thinking")
        if on_cancelled is not None:
            on_cancelled("cancelled_by_user")
        return "$thinking"

    with (
        patch.object(
            ResponseRunner,
            "_run_cancellable_response",
            new=AsyncMock(side_effect=fake_run_cancellable_response),
        ),
        patch("mindroom.response_runner.should_use_streaming", new=AsyncMock(return_value=True)),
        patch("mindroom.response_runner.team_response_stream") as mock_team_stream,
    ):
        coordinator = _build_response_runner(
            bot,
            config=config,
            runtime_paths=runtime_paths,
            storage_path=tmp_path,
            hook_registry=registry,
            team_history_storage=store,
            message_target=MessageTarget.resolve("!test:localhost", "$thread-root", "$user_msg"),
            orchestrator=team_orchestrator(config, runtime_paths),
        )

        async def consume_delivery_and_cancel(request: object) -> StreamTransportOutcome:
            accumulated = ""
            async for chunk in request.response_stream:
                accumulated += str(chunk)
                sequence.append(f"deliver:{accumulated}")
            return _streamed("$team-msg", accumulated)

        coordinator.deps.delivery_gateway.deliver_stream.side_effect = consume_delivery_and_cancel

        def fake_team_response_stream(
            _spec: TeamTurnSpec,
            ctx: ResponseTurnContext,
            *,
            sinks: TurnSinks,  # noqa: ARG001
        ) -> AsyncIterator[str]:
            async def fake_stream() -> AsyncIterator[str]:
                cancel_message = "cancel"
                session_id = ctx.session_id
                assert isinstance(session_id, str)
                _start_team_session(store, session_id)
                sequence.append("stream")
                yield "Team hello"
                raise asyncio.CancelledError(cancel_message)

            return fake_stream()

        mock_team_stream.side_effect = fake_team_response_stream

        resolution = await coordinator.generate_team_response_helper(
            _response_request(prompt="Hello", user_id="@alice:localhost", thread_id="$thread-root"),
            member_names=["general"],
            team_mode="coordinate",
        )

    # The reply's own event: the placeholder its stream and notes write into.
    assert resolution == "$thinking"
    assert sequence == [
        "stream",
        "deliver:Team hello",
        "started:team:ultimate:!test:localhost:$thread-root:$thread-root",
    ]


@pytest.mark.asyncio
async def test_generate_team_response_helper_uses_persisted_team_scope_for_session_started_hooks(
    tmp_path: Path,
) -> None:
    """Ad hoc team session hooks should scope to the persisted team scope, not the router bot."""
    runtime_paths = _runtime_paths(tmp_path)
    config = bind_runtime_paths(_config(), runtime_paths)
    bot = _make_bot(tmp_path, config=config, runtime_paths=runtime_paths, agent_name=ROUTER_AGENT_NAME)

    store = _team_store(config, runtime_paths, scope_id="team_general")
    sequence: list[str] = []

    @hook(EVENT_SESSION_STARTED, agents=["team_general"], rooms=["!test:localhost"])
    async def started(ctx: SessionHookContext) -> None:
        sequence.append(f"started:{ctx.scope.key}:{ctx.agent_name}")

    registry = HookRegistry.from_plugins([_hook_plugin("session-hooks", [started])])

    async def fails(_run: TurnRunState) -> AsyncIterator[str | AttemptResolved]:
        # The turn driver stores the failed turn's record, which creates the team's session.
        yield AttemptResolved(HandledAttempt("Team hello", error=RuntimeError("Team hello")))

    with (
        patch("mindroom.response_runner.should_use_streaming", new=AsyncMock(return_value=False)),
        patch(
            "mindroom.response_runner.team_response_stream",
            new=_driven_team_stream(config, runtime_paths, fails, scope_id="team_general"),
        ),
        patch("mindroom.response_lifecycle.apply_post_response_effects", new=AsyncMock(return_value=None)),
    ):
        coordinator = _build_response_runner(
            bot,
            config=config,
            runtime_paths=runtime_paths,
            storage_path=tmp_path,
            hook_registry=registry,
            team_history_storage=store,
            message_target=MessageTarget.resolve("!test:localhost", "$thread-root", "$user_msg"),
            orchestrator=team_orchestrator(config, runtime_paths),
        )

        await coordinator.generate_team_response_helper(
            _response_request(prompt="Hello", user_id="@alice:localhost", thread_id="$thread-root"),
            member_names=["general"],
            team_mode="coordinate",
        )

    assert sequence == ["started:team:team_general:team_general"]
    assert len(_persisted_runs(store)) == 1


@pytest.mark.asyncio
@pytest.mark.parametrize("streaming", [False, True], ids=["blocking", "streaming"])
async def test_generate_team_response_helper_links_its_reply_event_to_the_reported_team_run(
    tmp_path: Path,
    streaming: bool,
) -> None:
    """A team reply links its Matrix event onto the team run its attempt reported, in the team's session."""
    runtime_paths = _runtime_paths(tmp_path)
    config = bind_runtime_paths(_config_with_team(), runtime_paths)
    bot = _make_bot(tmp_path, config=config, runtime_paths=runtime_paths, agent_name="ultimate")
    store = _team_store(config, runtime_paths)

    def run_team_attempt(sinks: TurnSinks) -> None:
        seed_session(
            store,
            _team_session(
                runs=[
                    TeamRunRecord(
                        run_id="team-run",
                        team_id="ultimate",
                        session_id=TEAM_SESSION_ID,
                        status=RunStatus.completed,
                        content="Team hello",
                    ),
                ],
            ),
        )
        assert sinks.turn_recorder is not None
        sinks.turn_recorder.record_completed(
            run_id="team-run",
            run_metadata=None,
            assistant_text="Team hello",
            completed_tools=(),
        )

    def fake_team_response_stream(
        _spec: TeamTurnSpec,
        _ctx: ResponseTurnContext,
        *,
        sinks: TurnSinks,
    ) -> AsyncIterator[str]:
        async def stream() -> AsyncIterator[str]:
            run_team_attempt(sinks)
            yield "Team hello"

        return stream()

    with (
        patch("mindroom.response_runner.should_use_streaming", new=AsyncMock(return_value=streaming)),
        patch("mindroom.response_runner.team_response_stream", new=fake_team_response_stream),
        patch(
            "mindroom.response_lifecycle.apply_post_response_effects",
            new=AsyncMock(return_value=None),
        ) as mock_post_response_effects,
    ):
        coordinator = _build_response_runner(
            bot,
            config=config,
            runtime_paths=runtime_paths,
            storage_path=tmp_path,
            team_history_storage=store,
            message_target=MessageTarget.resolve("!test:localhost", "$thread-root", "$user_msg"),
            orchestrator=team_orchestrator(config, runtime_paths),
        )

        async def consume_delivery(request: StreamingDeliveryRequest) -> StreamTransportOutcome:
            async for _chunk in request.response_stream:
                pass
            return _streamed("$team-final", "Team hello")

        coordinator.deps.delivery_gateway.deliver_stream.side_effect = consume_delivery
        # The runner's state writer is a test double; its linkage writes through the real writer.
        coordinator.deps.state_writer.apersist_response_event_id_in_session_run = AsyncMock(
            side_effect=ConversationStateWriter(
                ConversationStateWriterDeps(
                    runtime=SimpleNamespace(config=config),
                    logger=MagicMock(),
                    runtime_paths=runtime_paths,
                    agent_name="ultimate",
                ),
            ).apersist_response_event_id_in_session_run,
        )

        await coordinator.generate_team_response_helper(
            _response_request(prompt="Hello", user_id="@alice:localhost", thread_id="$thread-root"),
            member_names=["general"],
            team_mode="coordinate",
        )

    assert mock_post_response_effects.await_args is not None
    _final_delivery, outcome, post_response_deps = mock_post_response_effects.await_args.args
    assert outcome.response_run_id == "team-run"
    assert post_response_deps.persist_response_event_id is not None
    await post_response_deps.persist_response_event_id(outcome.response_run_id, "$reply")
    assert [run.metadata for run in _persisted_runs(store)] == [{MATRIX_RESPONSE_EVENT_ID_METADATA_KEY: "$reply"}]


@pytest.mark.asyncio
async def test_generate_team_response_helper_merges_raw_prompt_into_model_prompt(
    tmp_path: Path,
) -> None:
    """Ad hoc team responses should keep the user request when model_prompt only adds metadata."""
    runtime_paths = _runtime_paths(tmp_path)
    config = bind_runtime_paths(_config(), runtime_paths)
    bot = _make_bot(tmp_path, config=config, runtime_paths=runtime_paths, agent_name=ROUTER_AGENT_NAME)

    with (
        patch("mindroom.response_runner.should_use_streaming", new=AsyncMock(return_value=False)),
        patch("mindroom.response_lifecycle.apply_post_response_effects", new=AsyncMock(return_value=None)),
        patch("mindroom.response_runner.team_response_stream", new=scripted_team_stream("Team hello")) as team_stream,
    ):
        coordinator = _build_response_runner(
            bot,
            config=config,
            runtime_paths=runtime_paths,
            storage_path=tmp_path,
            message_target=MessageTarget.resolve("!test:localhost", "$thread-root", "$user_msg"),
            orchestrator=team_orchestrator(config, runtime_paths),
        )

        resolution = await coordinator.generate_team_response_helper(
            _response_request(
                prompt="What is in the image?",
                model_prompt="Available attachment IDs: att_img. Use tool calls to inspect or process them.",
                user_id="@alice:localhost",
                thread_id="$thread-root",
            ),
            member_names=["general"],
            team_mode="coordinate",
        )

    assert _handled_response_event_id(resolution) == "$thinking"
    _spec, ctx = team_stream.call_args.args
    message = ctx.prompt
    assert "What is in the image?" in message
    assert "Available attachment IDs: att_img. Use tool calls to inspect or process them." in message


@pytest.mark.asyncio
async def test_generate_team_response_helper_uses_delivery_result_failure_reason_for_cancelled_stream(
    tmp_path: Path,
) -> None:
    """Typed gateway outcomes should preserve their canonical failure_reason."""
    runtime_paths = _runtime_paths(tmp_path)
    config = bind_runtime_paths(_config_with_team(), runtime_paths)
    bot = _make_bot(tmp_path, config=config, runtime_paths=runtime_paths, agent_name="ultimate")

    store = _team_store(config, runtime_paths)

    with (
        patch("mindroom.response_runner.should_use_streaming", new=AsyncMock(return_value=True)),
        patch("mindroom.response_runner.team_response_stream") as mock_team_stream,
        patch("mindroom.response_lifecycle.apply_post_response_effects", new=AsyncMock(return_value=None)),
    ):
        coordinator = _build_response_runner(
            bot,
            config=config,
            runtime_paths=runtime_paths,
            storage_path=tmp_path,
            hook_registry=HookRegistry.empty(),
            team_history_storage=store,
            message_target=MessageTarget.resolve("!test:localhost", "$thread-root", "$user_msg"),
            orchestrator=team_orchestrator(config, runtime_paths),
        )
        _set_gateway_method(
            coordinator.deps.delivery_gateway,
            "deliver_stream",
            AsyncMock(
                return_value=_streamed("$team-msg", "Team hello"),
            ),
        )
        _set_gateway_method(
            coordinator.deps.delivery_gateway,
            "finalize_streamed_response",
            AsyncMock(
                return_value=FinalDeliveryOutcome(
                    terminal_status="error",
                    event_id=None,
                    failure_reason="stream failure",
                ),
            ),
        )

        def fake_team_response_stream(
            _spec: TeamTurnSpec,
            ctx: ResponseTurnContext,
            *,
            sinks: TurnSinks,  # noqa: ARG001
        ) -> AsyncIterator[str]:
            async def fake_stream() -> AsyncIterator[str]:
                session_id = ctx.session_id
                assert isinstance(session_id, str)
                seed_session(store, _team_session(session_id, team_id="ultimate"))
                yield "Team hello"

            return fake_stream()

        mock_team_stream.side_effect = fake_team_response_stream

        resolution = await coordinator.generate_team_response_helper(
            _response_request(prompt="Hello", user_id="@alice:localhost", thread_id="$thread-root"),
            member_names=["general"],
            team_mode="coordinate",
        )

    # The reply's own event: the placeholder its stream and notes write into.
    assert resolution == "$thinking"


@pytest.mark.asyncio
async def test_generate_team_response_helper_persists_interrupted_history_after_errored_stream(
    tmp_path: Path,
) -> None:
    """A team stream whose run failed after showing text ends normally and stores the failed turn's replay.

    The turn driver records the failure and stores it before it yields the failure text, so the stream's delivery
    completes and the stored record names the reply's event.
    """
    runtime_paths = _runtime_paths(tmp_path)
    config = bind_runtime_paths(_config_with_team(), runtime_paths)
    bot = _make_bot(tmp_path, config=config, runtime_paths=runtime_paths, agent_name="ultimate")
    store = _team_store(config, runtime_paths)

    async def fails_after_partial_text(run: TurnRunState) -> AsyncIterator[str | AttemptResolved]:
        run.attempt = _Shown("Team partial")
        yield "Team partial"
        yield AttemptResolved(HandledAttempt("Team run failed", error=RuntimeError("Team run failed")))

    with (
        patch("mindroom.response_runner.should_use_streaming", new=AsyncMock(return_value=True)),
        patch(
            "mindroom.response_runner.team_response_stream",
            new=_driven_team_stream(config, runtime_paths, fails_after_partial_text),
        ),
    ):
        coordinator = _build_response_runner(
            bot,
            config=config,
            runtime_paths=runtime_paths,
            storage_path=tmp_path,
            hook_registry=HookRegistry.empty(),
            team_history_storage=store,
            message_target=MessageTarget.resolve("!test:localhost", "$thread-root", "$user_msg"),
            orchestrator=team_orchestrator(config, runtime_paths),
        )
        _install_inert_post_response_effects(coordinator)

        async def consume_delivery(request: StreamingDeliveryRequest) -> StreamTransportOutcome:
            rendered = "".join([str(chunk) async for chunk in request.response_stream])
            return _streamed("$team-final", rendered)

        coordinator.deps.delivery_gateway.deliver_stream.side_effect = consume_delivery

        await coordinator.generate_team_response_helper(
            _response_request(prompt="Hello", user_id="@alice:localhost", thread_id="$thread-root"),
            member_names=["general"],
            team_mode="coordinate",
        )

    [persisted_run] = _persisted_runs(store)
    _assert_interrupted_messages(
        persisted_run,
        response_event_id="$thinking",
        assistant_body="Team partial\n\n(turn failed before completion)",
    )
