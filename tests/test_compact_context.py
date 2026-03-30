"""Tests for session compaction and history scrubbing."""

from __future__ import annotations

import asyncio
import tempfile
from contextlib import asynccontextmanager
from pathlib import Path
from types import SimpleNamespace
from typing import TYPE_CHECKING
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from agno.agent import Agent
from agno.db.base import SessionType
from agno.db.sqlite import SqliteDb
from agno.media import Audio, File, Image, Video
from agno.models.base import Model
from agno.models.message import Message
from agno.models.response import ModelResponse
from agno.run.agent import RunOutput
from agno.run.base import RunStatus
from agno.session.agent import AgentSession
from agno.session.summary import SessionSummary
from agno.tools.function import Function, FunctionCall

from mindroom.agents import create_agent, create_session_storage, get_agent_session, get_seen_event_ids
from mindroom.bot import AgentBot
from mindroom.compaction import (
    _WRAPPER_OVERHEAD_TOKENS,
    PendingCompaction,
    _build_summary_input,
    _estimate_runs_tokens,
    _estimate_serialized_run_tokens,
    _scrub_history_messages_from_sessions,
    apply_pending_compaction,
    clear_pending_compaction,
    compact_session_now,
    get_visible_session_runs,
    queue_pending_compaction,
)
from mindroom.config.agent import AgentConfig
from mindroom.config.main import Config
from mindroom.config.models import CompactionConfig, DefaultsConfig, ModelConfig
from mindroom.constants import MINDROOM_COMPACTION_METADATA_KEY, RuntimePaths, resolve_runtime_paths
from mindroom.custom_tools.compact_context import CompactContextTools, _format_outcome
from mindroom.matrix.users import AgentMatrixUser
from mindroom.tool_system.worker_routing import ToolExecutionIdentity
from tests.conftest import bind_runtime_paths, runtime_paths_for

if TYPE_CHECKING:
    from collections.abc import AsyncIterator, Iterator


class FakeModel(Model):
    """Minimal Agno model for deterministic history-storage tests."""

    def invoke(self, *_args: object, **_kwargs: object) -> ModelResponse:
        """Return a fixed sync response."""
        return ModelResponse(content="hello")

    async def ainvoke(self, *_args: object, **_kwargs: object) -> ModelResponse:
        """Return a fixed async response."""
        return ModelResponse(content="hello")

    def invoke_stream(self, *_args: object, **_kwargs: object) -> Iterator[ModelResponse]:
        """Yield a fixed sync streaming response."""
        yield ModelResponse(content="hello")

    async def ainvoke_stream(self, *_args: object, **_kwargs: object) -> AsyncIterator[ModelResponse]:
        """Yield a fixed async streaming response."""
        yield ModelResponse(content="hello")

    def _parse_provider_response(self, response: ModelResponse, *_args: object, **_kwargs: object) -> ModelResponse:
        return response

    def _parse_provider_response_delta(
        self,
        response: ModelResponse,
        *_args: object,
        **_kwargs: object,
    ) -> ModelResponse:
        return response


def _runtime_paths(tmp_path: object) -> RuntimePaths:
    base_path = Path(str(tmp_path))
    return resolve_runtime_paths(
        config_path=base_path / "config.yaml",
        storage_path=base_path,
    )


def _runtime_bound_config(config: Config, tmp_path: object | None = None) -> Config:
    return bind_runtime_paths(config, _runtime_paths(tmp_path or tempfile.mkdtemp()))


def _make_config(tmp_path: object) -> Config:
    return _runtime_bound_config(
        Config(
            agents={"test_agent": AgentConfig(display_name="Test Agent")},
            defaults=DefaultsConfig(tools=[]),
            models={"default": ModelConfig(provider="openai", id="test-model")},
        ),
        tmp_path,
    )


def _coerce_agent_session(raw_session: object) -> AgentSession:
    if isinstance(raw_session, AgentSession):
        return raw_session
    session = AgentSession.from_dict(raw_session)
    if session is not None:
        return session
    msg = f"Unsupported session payload: {type(raw_session).__name__}"
    raise TypeError(msg)


def _make_storage(tmp_path: Path) -> SqliteDb:
    return SqliteDb(
        session_table="test_agent_sessions",
        db_file=str(tmp_path / "test-agent.db"),
    )


def _make_plain_agent(storage: SqliteDb, *, store_history_messages: bool) -> Agent:
    return Agent(
        id="test_agent",
        model=FakeModel(id="fake-model", provider="fake"),
        db=storage,
        add_history_to_context=True,
        store_history_messages=store_history_messages,
    )


def _single_run_compaction_window(run: RunOutput, *, reserve_tokens: int = 1024) -> int:
    desired_budget = _estimate_serialized_run_tokens(run) + _WRAPPER_OVERHEAD_TOKENS + 32
    return int((desired_budget + reserve_tokens + 2000) / 0.9) + 1


def test_build_summary_input_serializes_message_media_without_raw_content() -> None:
    """Compaction input should include media metadata without dumping raw bytes."""
    run = RunOutput(
        run_id="r1",
        messages=[
            Message(
                role="user",
                content="uploaded artifacts",
                images=[Image(content=b"raw-image-bytes", mime_type="image/png", alt_text="architecture diagram")],
                files=[File(content=b"%PDF-raw", filename="spec.pdf", mime_type="application/pdf")],
                audio=[Audio(content=b"raw-audio", transcript="meeting notes")],
                videos=[Video(content=b"raw-video", revised_prompt="demo clip")],
                image_output=Image(url="mxc://mindroom/image", alt_text="generated mockup"),
                file_output=File(content="raw file body", filename="report.txt", mime_type="text/plain"),
            ),
        ],
        status=RunStatus.running,
    )

    summary_input, included_runs, budget_exhausted = _build_summary_input(
        previous_summary=None,
        compacted_runs=[run],
        max_input_tokens=10_000,
    )

    assert included_runs == [run]
    assert budget_exhausted is False
    assert "<images>" in summary_input
    assert "<files>" in summary_input
    assert "<audio>" in summary_input
    assert "<videos>" in summary_input
    assert "<image_output>" in summary_input
    assert "<file_output>" in summary_input
    assert "architecture diagram" in summary_input
    assert "spec.pdf" in summary_input
    assert "meeting notes" in summary_input
    assert "demo clip" in summary_input
    assert "mxc://mindroom/image" in summary_input
    assert "report.txt" in summary_input
    assert "raw-image-bytes" not in summary_input
    assert "%PDF-raw" not in summary_input
    assert "raw-audio" not in summary_input
    assert "raw-video" not in summary_input
    assert "raw file body" not in summary_input


async def _seed_session(
    storage: SqliteDb,
    *,
    session_id: str,
    turn_count: int,
) -> Agent:
    agent = _make_plain_agent(storage, store_history_messages=False)
    for turn_number in range(1, turn_count + 1):
        prompt = f"turn-{turn_number} " * 20
        await agent.arun(
            prompt,
            session_id=session_id,
            metadata={"matrix_seen_event_ids": [f"$e{turn_number}"]},
        )
    return agent


@pytest.mark.asyncio
async def test_create_agent_does_not_store_history_messages(tmp_path: Path) -> None:
    """Newly created agents should avoid persisting replayed history messages."""
    config = _make_config(tmp_path)
    runtime_paths = runtime_paths_for(config)

    with patch("mindroom.ai.get_model_instance", return_value=FakeModel(id="fake-model", provider="fake")):
        agent = create_agent(
            "test_agent",
            config,
            runtime_paths,
            execution_identity=None,
        )

    await agent.arun("first turn", session_id="sid")
    await agent.arun("second turn", session_id="sid")

    storage = create_session_storage("test_agent", config, runtime_paths, execution_identity=None)
    session = get_agent_session(storage, "sid")
    assert session is not None
    assert len(session.runs or []) == 2

    stored_session = _coerce_agent_session(storage.get_session("sid", SessionType.AGENT))
    for run in stored_session.runs or []:
        assert run.messages is not None
        assert not any(message.from_history for message in run.messages)


@pytest.mark.asyncio
async def test_manual_compaction_uses_compaction_model_window_for_budget(tmp_path: Path) -> None:
    """Manual compaction should normalize reserve tokens to the summary model window."""
    config = _runtime_bound_config(
        Config(
            agents={"test_agent": AgentConfig(display_name="Test Agent")},
            defaults=DefaultsConfig(
                tools=[],
                compaction=CompactionConfig(
                    enabled=True,
                    model="compact",
                    reserve_tokens=16384,
                ),
            ),
            models={
                "default": ModelConfig(provider="openai", id="chat-model", context_window=128000),
                "compact": ModelConfig(provider="openai", id="compact-model", context_window=16000),
            },
        ),
        tmp_path,
    )
    runtime_paths = runtime_paths_for(config)
    storage = create_session_storage("test_agent", config, runtime_paths, execution_identity=None)
    await _seed_session(storage, session_id="sid", turn_count=3)
    tool = CompactContextTools(
        "test_agent",
        config,
        runtime_paths,
        execution_identity=ToolExecutionIdentity(
            channel="openai_compat",
            agent_name="test_agent",
            requester_id="@user:example.org",
            room_id=None,
            thread_id=None,
            resolved_thread_id=None,
            session_id="sid",
        ),
    )

    with (
        patch("mindroom.custom_tools.compact_context.get_tool_runtime_context", return_value=None),
        patch(
            "mindroom.ai.get_model_instance",
            return_value=FakeModel(id="compact-model", provider="fake"),
        ),
        patch(
            "mindroom.compaction._generate_compaction_summary",
            new_callable=AsyncMock,
            return_value=SessionSummary(summary="## Goal\nqueued", topics=[]),
        ),
    ):
        outcome_text = await tool.compact_context(keep_recent_runs=1)

    assert outcome_text.startswith("Compaction queued:")


@pytest.mark.asyncio
async def test_scrub_history_messages_from_sessions_removes_history_and_preserves_get_messages(tmp_path: Path) -> None:
    """Scrubbing should remove duplicated history rows without changing message replay."""
    storage = _make_storage(tmp_path)
    agent = _make_plain_agent(storage, store_history_messages=True)
    await agent.arun("first turn", session_id="sid")
    await agent.arun("second turn", session_id="sid")

    session = _coerce_agent_session(storage.get_session("sid", SessionType.AGENT))
    expected_messages = [message.content for message in session.get_messages()]

    stats = _scrub_history_messages_from_sessions(storage)

    assert stats.sessions_scanned == 1
    assert stats.sessions_changed == 1
    assert stats.messages_removed == 2
    assert stats.size_before_bytes > stats.size_after_bytes

    scrubbed_session = _coerce_agent_session(storage.get_session("sid", SessionType.AGENT))
    assert [message.content for message in scrubbed_session.get_messages()] == expected_messages
    assert not any(message.from_history for run in scrubbed_session.runs or [] for message in run.messages or [])


@pytest.mark.asyncio
async def test_compact_session_now_multi_pass_compacts_multiple_runs(tmp_path: Path) -> None:
    """Immediate compaction should keep compacting until the session fits the target window."""
    storage = _make_storage(tmp_path)
    agent = await _seed_session(storage, session_id="sid", turn_count=5)
    session = _coerce_agent_session(storage.get_session("sid", SessionType.AGENT))
    assert session.runs is not None
    recent_run = session.runs[-1]
    compaction_window = _single_run_compaction_window(session.runs[0])
    summary_responses = [SessionSummary(summary=f"## Goal\npass {index}", topics=[]) for index in range(1, 5)]

    with patch(
        "mindroom.compaction._generate_compaction_summary",
        new_callable=AsyncMock,
        side_effect=summary_responses,
    ) as mock_summary:
        result = await compact_session_now(
            storage=storage,
            session_id="sid",
            agent=agent,
            model=MagicMock(id="compact-model"),
            mode="auto",
            window_tokens=16000,
            threshold_tokens=8000,
            reserve_tokens=1024,
            keep_recent_tokens=_estimate_runs_tokens([recent_run]),
            notify=True,
            compaction_model_context_window=compaction_window,
            max_passes=10,
        )

    assert result is not None
    updated_session, outcome = result
    assert mock_summary.await_count == 4
    assert outcome.compacted_run_count == 4
    assert outcome.runs_before == 5
    assert outcome.runs_after == 2
    assert len(updated_session.runs or []) == 5
    assert len(get_visible_session_runs(updated_session)) == 1
    assert updated_session.summary is not None
    assert updated_session.summary.summary == "## Goal\npass 4"
    metadata = updated_session.metadata[MINDROOM_COMPACTION_METADATA_KEY]
    assert metadata["seen_event_ids"] == ["$e1", "$e2", "$e3", "$e4"]
    assert updated_session.runs is not None
    assert metadata["last_compacted_run_id"] == updated_session.runs[3].run_id
    assert get_seen_event_ids(updated_session) == {"$e1", "$e2", "$e3", "$e4", "$e5"}


@pytest.mark.asyncio
async def test_queue_pending_compaction_multi_pass_tracks_cutoff_run_id(tmp_path: Path) -> None:
    """Deferred compaction should remember the last run hidden behind the summary cutoff."""
    config = _make_config(tmp_path)
    storage = _make_storage(tmp_path)
    agent = await _seed_session(storage, session_id="sid", turn_count=5)
    pending_buffer: list[PendingCompaction] = []
    session = _coerce_agent_session(storage.get_session("sid", SessionType.AGENT))
    assert session.runs is not None
    compaction_window = _single_run_compaction_window(session.runs[0])
    summary_responses = [SessionSummary(summary=f"## Goal\ndeferred {index}", topics=[]) for index in range(1, 5)]

    with (
        patch(
            "mindroom.compaction._generate_compaction_summary",
            new_callable=AsyncMock,
            side_effect=summary_responses,
        ) as mock_summary,
        patch("mindroom.compaction.create_session_storage", return_value=storage),
    ):
        queued = await queue_pending_compaction(
            storage=storage,
            session_id="sid",
            agent_name="test_agent",
            config=config,
            runtime_paths=runtime_paths_for(config),
            execution_identity=None,
            model=MagicMock(id="compact-model"),
            keep_recent_runs=1,
            window_tokens=16000,
            threshold_tokens=8000,
            reserve_tokens=1024,
            notify=True,
            compaction_model_context_window=compaction_window,
            max_passes=10,
            pending_buffer=pending_buffer,
        )
        assert queued is not None
        assert len(pending_buffer) == 1
        pending = pending_buffer[-1]
        assert pending.last_compacted_run_id == session.runs[3].run_id
        assert mock_summary.await_count == 4

        await agent.arun(
            "turn-6 " * 20,
            session_id="sid",
            metadata={"matrix_seen_event_ids": ["$e6"]},
        )
        applied = await apply_pending_compaction(pending)

    assert applied is not None
    assert applied.compacted_run_count == 4
    assert applied.runs_before == 6
    assert applied.runs_after == 2
    saved_session = _coerce_agent_session(storage.get_session("sid", SessionType.AGENT))
    assert len(saved_session.runs or []) == 6
    assert len(get_visible_session_runs(saved_session)) == 2
    assert saved_session.summary is not None
    assert saved_session.summary.summary == "## Goal\ndeferred 4"
    assert get_seen_event_ids(saved_session) == {"$e1", "$e2", "$e3", "$e4", "$e5", "$e6"}


@pytest.mark.asyncio
async def test_second_turn_waits_for_queued_compaction_to_apply(  # noqa: PLR0915
    tmp_path: Path,
) -> None:
    """The next turn in a thread should wait until queued compaction is committed."""
    config = _runtime_bound_config(
        Config(
            agents={
                "test_agent": AgentConfig(
                    display_name="Test Agent",
                    rooms=["!room:localhost"],
                ),
            },
            defaults=DefaultsConfig(
                tools=[],
                enable_streaming=False,
                show_stop_button=False,
                compaction=CompactionConfig(reserve_tokens=1024, notify=False),
            ),
            models={
                "default": ModelConfig(
                    provider="openai",
                    id="test-model",
                    context_window=16000,
                ),
            },
        ),
        tmp_path,
    )
    runtime_paths = runtime_paths_for(config)
    session_id = "!room:localhost:$thread-root"
    storage = create_session_storage("test_agent", config, runtime_paths, execution_identity=None)
    agent = await _seed_session(storage, session_id=session_id, turn_count=3)

    bot = AgentBot(
        AgentMatrixUser(
            agent_name="test_agent",
            password="test-password",  # noqa: S106
            display_name="Test Agent",
            user_id="@mindroom_test_agent:localhost",
        ),
        tmp_path,
        config=config,
        runtime_paths=runtime_paths,
        rooms=["!room:localhost"],
        enable_streaming=False,
    )
    bot.client = MagicMock()
    bot._prepare_memory_and_model_context = MagicMock(
        side_effect=lambda prompt, thread_history, model_prompt=None: (
            prompt,
            thread_history,
            model_prompt or prompt,
            thread_history,
        ),
    )
    bot._knowledge_for_agent = MagicMock(return_value=None)

    async def fake_ensure_request_knowledge_managers(*_args: object, **_kwargs: object) -> dict[str, object]:
        return {}

    async def fake_send_response(*_args: object, **_kwargs: object) -> str:
        return "$thinking"

    async def fake_deliver_generated_response(*_args: object, **_kwargs: object) -> SimpleNamespace:
        return SimpleNamespace(
            event_id="$response",
            suppressed=False,
            option_map=None,
            options_list=None,
        )

    bot._ensure_request_knowledge_managers = fake_ensure_request_knowledge_managers
    bot._send_response = fake_send_response
    bot._deliver_generated_response = fake_deliver_generated_response

    compaction_queued = asyncio.Event()
    allow_first_turn_to_finish = asyncio.Event()
    second_turn_started = asyncio.Event()
    tool_messages: list[str] = []
    second_turn_visible_runs: list[int] = []
    second_turn_total_runs: list[int] = []
    second_turn_summaries: list[str | None] = []
    pending_buffer: list[PendingCompaction] = []
    ai_call_count = 0

    @asynccontextmanager
    async def noop_typing_indicator(*_args: object, **_kwargs: object) -> AsyncIterator[None]:
        yield

    def discard_background_task(coro: object, *args: object, **kwargs: object) -> None:
        _ = (args, kwargs)
        if asyncio.iscoroutine(coro):
            coro.close()

    async def fake_ai_response(*_args: object, **kwargs: object) -> str:
        nonlocal ai_call_count
        ai_call_count += 1

        resolved_session_id = kwargs["session_id"]
        execution_identity = kwargs["execution_identity"]
        collector = kwargs["compaction_outcomes_collector"]
        assert isinstance(resolved_session_id, str)
        assert isinstance(collector, list)

        if ai_call_count == 1:
            tool = CompactContextTools(
                "test_agent",
                config,
                runtime_paths,
                execution_identity,
                pending_compaction_buffer=pending_buffer,
            )
            tool_messages.append(await tool.compact_context(keep_recent_runs=1))
            assert len(pending_buffer) == 1
            compaction_queued.set()

            await allow_first_turn_to_finish.wait()

            await agent.arun(
                "turn-4 " * 20,
                session_id=resolved_session_id,
                metadata={"matrix_seen_event_ids": ["$e4"]},
            )
            outcome = await apply_pending_compaction(pending_buffer[-1])
            pending_buffer.clear()
            assert outcome is not None
            collector.append(outcome)
            return "first response"

        second_turn_started.set()
        session = get_agent_session(storage, resolved_session_id)
        assert session is not None
        second_turn_total_runs.append(len(session.runs or []))
        second_turn_visible_runs.append(len(get_visible_session_runs(session)))
        second_turn_summaries.append(session.summary.summary if session.summary is not None else None)
        return "second response"

    with (
        patch("mindroom.bot.ai_response", new=fake_ai_response),
        patch("mindroom.bot.create_background_task", side_effect=discard_background_task),
        patch("mindroom.bot.should_use_streaming", new_callable=AsyncMock, return_value=False),
        patch("mindroom.bot.typing_indicator", new=noop_typing_indicator),
        patch("mindroom.ai.get_model_instance", return_value=MagicMock(id="compact-model")),
        patch(
            "mindroom.compaction._generate_compaction_summary",
            new_callable=AsyncMock,
            return_value=SessionSummary(summary="## Goal\nqueued", topics=[]),
        ),
    ):
        first_turn = asyncio.create_task(
            bot._generate_response(
                room_id="!room:localhost",
                prompt="first turn",
                reply_to_event_id="$event-1",
                thread_id="$thread-root",
                thread_history=[],
                user_id="@user:localhost",
            ),
        )

        await compaction_queued.wait()

        second_turn = asyncio.create_task(
            bot._generate_response(
                room_id="!room:localhost",
                prompt="second turn",
                reply_to_event_id="$event-2",
                thread_id="$thread-root",
                thread_history=[],
                user_id="@user:localhost",
            ),
        )

        with pytest.raises(asyncio.TimeoutError):
            await asyncio.wait_for(second_turn_started.wait(), timeout=0.1)

        allow_first_turn_to_finish.set()

        await first_turn
        await second_turn

    assert len(tool_messages) == 1
    assert tool_messages[0].startswith("Compaction queued:")
    assert "Will apply after this response finishes." in tool_messages[0]
    assert second_turn_total_runs == [4]
    assert second_turn_visible_runs == [2]
    assert second_turn_summaries == ["## Goal\nqueued"]


@pytest.mark.asyncio
async def test_manual_compaction_reported_from_agno_tool_task_should_persist(tmp_path: Path) -> None:
    """Manual compaction triggered through Agno's tool-task path should persist to storage."""
    pending_buffer: list[PendingCompaction] = []
    clear_pending_compaction(pending_buffer)
    config = _make_config(tmp_path)
    runtime_paths = runtime_paths_for(config)
    storage = create_session_storage("test_agent", config, runtime_paths, execution_identity=None)
    agent = await _seed_session(storage, session_id="sid", turn_count=4)
    session = _coerce_agent_session(storage.get_session("sid", SessionType.AGENT))
    assert session.runs is not None
    compaction_window = _single_run_compaction_window(session.runs[0])
    child_pending_seen: list[int] = []

    async def compact_tool() -> str:
        outcome = await queue_pending_compaction(
            storage=storage,
            session_id="sid",
            agent_name="test_agent",
            config=config,
            runtime_paths=runtime_paths,
            execution_identity=None,
            model=MagicMock(id="compact-model"),
            keep_recent_runs=2,
            window_tokens=16000,
            threshold_tokens=8000,
            reserve_tokens=1024,
            notify=True,
            compaction_model_context_window=compaction_window,
            max_passes=10,
            pending_buffer=pending_buffer,
        )
        assert outcome is not None
        child_pending_seen.append(len(pending_buffer))
        return _format_outcome(outcome)

    function_call = FunctionCall(
        function=Function.from_callable(compact_tool, name="compact_context"),
        call_id="call-1",
    )
    model = FakeModel(id="fake-model", provider="fake")

    try:
        with patch(
            "mindroom.compaction._generate_compaction_summary",
            new_callable=AsyncMock,
            return_value=SessionSummary(summary="## Goal\nsummary", topics=[]),
        ):
            async for _ in model.arun_function_calls(
                function_calls=[function_call],
                function_call_results=[],
                skip_pause_check=True,
            ):
                pass

        assert child_pending_seen == [1]
        assert len(pending_buffer) == 1
        assert isinstance(function_call.result, str)
        assert "Compaction queued:" in function_call.result
        assert "Will apply after this response finishes." in function_call.result
        assert "- Visible runs: 4 -> 3" in function_call.result

        queued_session = _coerce_agent_session(storage.get_session("sid", SessionType.AGENT))
        assert len(queued_session.runs or []) == 4
        assert queued_session.summary is None

        await agent.arun(
            "turn-5 " * 20,
            session_id="sid",
            metadata={"matrix_seen_event_ids": ["$e5"]},
        )
        applied = await apply_pending_compaction(pending_buffer[-1])
        pending_buffer.clear()

        assert applied is not None
        persisted_session = _coerce_agent_session(storage.get_session("sid", SessionType.AGENT))
        assert len(persisted_session.runs or []) == 5
        assert len(get_visible_session_runs(persisted_session)) == 3
        assert persisted_session.summary is not None
        assert persisted_session.summary.summary == "## Goal\nsummary"
    finally:
        clear_pending_compaction(pending_buffer)


@pytest.mark.asyncio
async def test_compact_session_now_partial_failure_preserves_last_successful_pass(tmp_path: Path) -> None:
    """A later pass failure should preserve and persist the most recent successful pass."""
    storage = _make_storage(tmp_path)
    agent = await _seed_session(storage, session_id="sid", turn_count=4)
    session = _coerce_agent_session(storage.get_session("sid", SessionType.AGENT))
    assert session.runs is not None
    recent_run = session.runs[-1]
    compaction_window = _single_run_compaction_window(session.runs[0])

    with patch(
        "mindroom.compaction._generate_compaction_summary",
        new_callable=AsyncMock,
        side_effect=[
            SessionSummary(summary="## Goal\nfirst pass", topics=[]),
            RuntimeError("boom"),
        ],
    ) as mock_summary:
        result = await compact_session_now(
            storage=storage,
            session_id="sid",
            agent=agent,
            model=MagicMock(id="compact-model"),
            mode="auto",
            window_tokens=16000,
            threshold_tokens=8000,
            reserve_tokens=1024,
            keep_recent_tokens=_estimate_runs_tokens([recent_run]),
            notify=True,
            compaction_model_context_window=compaction_window,
            max_passes=10,
        )

    assert result is not None
    updated_session, outcome = result
    assert mock_summary.await_count == 2
    assert outcome.compacted_run_count == 1
    assert len(updated_session.runs or []) == 4
    assert len(get_visible_session_runs(updated_session)) == 3
    assert updated_session.summary is not None
    assert updated_session.summary.summary == "## Goal\nfirst pass"
    metadata = updated_session.metadata[MINDROOM_COMPACTION_METADATA_KEY]
    assert metadata["seen_event_ids"] == ["$e1"]
    assert get_seen_event_ids(updated_session) == {"$e1", "$e2", "$e3", "$e4"}
