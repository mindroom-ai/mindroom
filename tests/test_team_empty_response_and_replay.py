"""Tests for the team empty-run guard."""

from __future__ import annotations

from typing import TYPE_CHECKING
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from agno.models.message import Message
from agno.models.response import ToolExecution
from agno.run.agent import RunContentEvent as AgentRunContentEvent
from agno.run.agent import RunOutput
from agno.run.base import RunStatus
from agno.run.team import RunErrorEvent as TeamRunErrorEvent
from agno.run.team import TeamRunOutput

from mindroom import ai_runtime
from mindroom.dynamic_tool_continuation import DYNAMIC_TOOL_CONTINUATION_LIMIT
from mindroom.history.turn_recorder import TurnRecorder
from mindroom.knowledge.utils import _KnowledgeResolution
from mindroom.teams import TeamMode, team_response, team_response_stream
from tests.conftest import make_turn_context, runtime_paths_for
from tests.identity_helpers import entity_ids
from tests.test_team_dynamic_continuation import _dynamic_tool_team_output
from tests.test_team_media_fallback import _build_test_config, _make_test_agent, _make_test_team

if TYPE_CHECKING:
    from collections.abc import AsyncIterator
    from contextlib import AbstractContextManager

    from agno.team import Team as AgnoTeam


def _make_orchestrator() -> tuple[MagicMock, object]:
    config = _build_test_config()
    orchestrator = MagicMock()
    orchestrator.config = config
    orchestrator.runtime_paths = runtime_paths_for(config)
    orchestrator.knowledge_managers = {}
    orchestrator.agent_bots = {"general": MagicMock()}
    return orchestrator, config


def _team_patches(mock_team: AgnoTeam) -> list[AbstractContextManager[object]]:
    fake_agent = _make_test_agent("GeneralAgent")
    return [
        patch("mindroom.teams.create_agent", return_value=fake_agent),
        patch("mindroom.teams.resolve_agent_knowledge_access", return_value=_KnowledgeResolution(knowledge=None)),
        patch("mindroom.teams._create_team_instance", return_value=mock_team),
    ]


def _empty_team_run(run_id: str) -> TeamRunOutput:
    output = TeamRunOutput(content="", run_id=run_id, session_id="session-1")
    output.status = RunStatus.completed
    return output


def _completed_team_run(content: str) -> TeamRunOutput:
    output = TeamRunOutput(content=content, run_id="team-run-final", session_id="session-1")
    output.status = RunStatus.completed
    return output


@pytest.mark.asyncio
async def test_team_response_retries_once_after_empty_completed_run() -> None:
    """One empty completed team run is discarded and retried before answering."""
    orchestrator, _config = _make_orchestrator()
    mock_team = _make_test_team()
    mock_team.arun = AsyncMock(side_effect=[_empty_team_run("team-run-1"), _completed_team_run("Recovered answer")])

    patches = _team_patches(mock_team)
    with patches[0], patches[1], patches[2]:
        response = await team_response(
            agent_names=["general"],
            mode=TeamMode.COORDINATE,
            message="Say something.",
            turn_recorder=TurnRecorder(user_message="Say something."),
            orchestrator=orchestrator,
            execution_identity=None,
            ctx=make_turn_context(session_id="session-1"),
        )

    assert "Recovered answer" in response
    assert mock_team.arun.await_count == 2


@pytest.mark.asyncio
async def test_team_response_returns_fallback_notice_when_retry_is_also_empty() -> None:
    """A second empty completed team run surfaces the shared fallback notice."""
    orchestrator, _config = _make_orchestrator()
    mock_team = _make_test_team()
    mock_team.arun = AsyncMock(side_effect=[_empty_team_run("team-run-1"), _empty_team_run("team-run-2")])
    recorder = TurnRecorder(user_message="Say something.")

    patches = _team_patches(mock_team)
    with patches[0], patches[1], patches[2]:
        response = await team_response(
            agent_names=["general"],
            mode=TeamMode.COORDINATE,
            message="Say something.",
            turn_recorder=recorder,
            orchestrator=orchestrator,
            execution_identity=None,
            ctx=make_turn_context(session_id="session-1"),
        )

    assert response == ai_runtime.EMPTY_RESPONSE_NOTICE
    assert mock_team.arun.await_count == 2
    # The fallback notice stays out of the recorded turn.
    assert recorder.outcome == "completed"
    assert recorder.assistant_text == ""


@pytest.mark.asyncio
async def test_team_response_does_not_preserve_seen_ids_for_double_empty_run() -> None:
    """An empty turn with no model-visible run leaves its Matrix events unseen."""
    orchestrator, _config = _make_orchestrator()
    mock_team = _make_test_team()
    mock_team.arun = AsyncMock(side_effect=[_empty_team_run("team-run-1"), _empty_team_run("team-run-2")])

    patches = _team_patches(mock_team)
    with patches[0], patches[1], patches[2], patch("mindroom.teams._persist_bound_seen_event_ids") as persist_seen:
        await team_response(
            agent_names=["general"],
            mode=TeamMode.COORDINATE,
            message="Say something.",
            turn_recorder=TurnRecorder(user_message="Say something."),
            orchestrator=orchestrator,
            execution_identity=None,
            ctx=make_turn_context(session_id="session-1", reply_to_event_id="$source"),
        )

    persist_seen.assert_not_called()


@pytest.mark.asyncio
async def test_team_response_does_not_preserve_seen_ids_for_paused_run() -> None:
    """Paused runs are absent from Agno model history and cannot consume Matrix events."""
    orchestrator, _config = _make_orchestrator()
    mock_team = _make_test_team()
    paused_run = TeamRunOutput(content="Approval required", run_id="team-run-1", session_id="session-1")
    paused_run.status = RunStatus.paused
    mock_team.arun = AsyncMock(return_value=paused_run)

    patches = _team_patches(mock_team)
    with patches[0], patches[1], patches[2], patch("mindroom.teams._persist_bound_seen_event_ids") as persist_seen:
        await team_response(
            agent_names=["general"],
            mode=TeamMode.COORDINATE,
            message="Run the action.",
            turn_recorder=TurnRecorder(user_message="Run the action."),
            orchestrator=orchestrator,
            execution_identity=None,
            ctx=make_turn_context(session_id="session-1", reply_to_event_id="$source"),
        )

    persist_seen.assert_not_called()


@pytest.mark.asyncio
async def test_team_response_stream_yields_fallback_notice_when_retry_is_also_empty() -> None:
    """The streaming empty-run guard retries once, then yields the notice chunk."""
    orchestrator, config = _make_orchestrator()

    async def empty_stream(run_id: str) -> AsyncIterator[object]:
        yield _empty_team_run(run_id)

    mock_team = _make_test_team()
    mock_team.arun = MagicMock(side_effect=[empty_stream("team-run-1"), empty_stream("team-run-2")])

    patches = _team_patches(mock_team)
    with patches[0], patches[1], patches[2]:
        chunks = [
            chunk
            async for chunk in team_response_stream(
                agent_ids=[entity_ids(config, runtime_paths_for(config))["general"]],
                mode=TeamMode.COORDINATE,
                message="Say something.",
                turn_recorder=TurnRecorder(user_message="Say something."),
                orchestrator=orchestrator,
                execution_identity=None,
                ctx=make_turn_context(session_id="session-1"),
            )
        ]

    assert mock_team.arun.call_count == 2
    rendered = "".join(chunk.content if hasattr(chunk, "content") else str(chunk) for chunk in chunks)
    assert ai_runtime.EMPTY_RESPONSE_NOTICE in rendered
    # The discarded attempts' fallback documents must not leak ahead of the notice.
    assert "No team response generated." not in rendered


@pytest.mark.asyncio
async def test_team_response_stream_does_not_preserve_seen_ids_for_double_empty_run() -> None:
    """Discarded streaming attempts cannot consume Matrix events."""
    orchestrator, config = _make_orchestrator()

    async def empty_stream(run_id: str) -> AsyncIterator[object]:
        yield _empty_team_run(run_id)

    mock_team = _make_test_team()
    mock_team.arun = MagicMock(side_effect=[empty_stream("team-run-1"), empty_stream("team-run-2")])

    patches = _team_patches(mock_team)
    with patches[0], patches[1], patches[2], patch("mindroom.teams._persist_bound_seen_event_ids") as persist_seen:
        _ = [
            chunk
            async for chunk in team_response_stream(
                agent_ids=[entity_ids(config, runtime_paths_for(config))["general"]],
                mode=TeamMode.COORDINATE,
                message="Say something.",
                turn_recorder=TurnRecorder(user_message="Say something."),
                orchestrator=orchestrator,
                execution_identity=None,
                ctx=make_turn_context(session_id="session-1", reply_to_event_id="$source"),
            )
        ]

    persist_seen.assert_not_called()


@pytest.mark.asyncio
async def test_team_response_stream_does_not_preserve_seen_ids_for_paused_run() -> None:
    """Paused streaming runs are absent from model history and cannot consume Matrix events."""
    orchestrator, config = _make_orchestrator()
    paused_run = TeamRunOutput(content="Approval required", run_id="team-run-1", session_id="session-1")
    paused_run.status = RunStatus.paused

    async def paused_stream() -> AsyncIterator[object]:
        yield paused_run

    mock_team = _make_test_team()
    mock_team.arun = MagicMock(return_value=paused_stream())

    patches = _team_patches(mock_team)
    with patches[0], patches[1], patches[2], patch("mindroom.teams._persist_bound_seen_event_ids") as persist_seen:
        _ = [
            chunk
            async for chunk in team_response_stream(
                agent_ids=[entity_ids(config, runtime_paths_for(config))["general"]],
                mode=TeamMode.COORDINATE,
                message="Run the action.",
                turn_recorder=TurnRecorder(user_message="Run the action."),
                orchestrator=orchestrator,
                execution_identity=None,
                ctx=make_turn_context(session_id="session-1", reply_to_event_id="$source"),
            )
        ]

    persist_seen.assert_not_called()


@pytest.mark.asyncio
async def test_team_response_stream_empty_event_stream_retries_then_notices() -> None:
    """A stream that ends with no events at all still triggers the empty-run guard.

    Real Agno team streams never emit a terminal run output object, so the
    guard must fire from the plain end-of-stream resolution.
    """
    orchestrator, config = _make_orchestrator()

    async def silent_stream() -> AsyncIterator[object]:
        return
        yield  # pragma: no cover - makes this an async generator

    mock_team = _make_test_team()
    mock_team.arun = MagicMock(side_effect=[silent_stream(), silent_stream()])
    recorder = TurnRecorder(user_message="Say something.")

    patches = _team_patches(mock_team)
    with patches[0], patches[1], patches[2]:
        chunks = [
            chunk
            async for chunk in team_response_stream(
                agent_ids=[entity_ids(config, runtime_paths_for(config))["general"]],
                mode=TeamMode.COORDINATE,
                message="Say something.",
                turn_recorder=recorder,
                orchestrator=orchestrator,
                execution_identity=None,
                ctx=make_turn_context(session_id="session-1"),
            )
        ]

    assert mock_team.arun.call_count == 2
    rendered = "".join(chunk.content if hasattr(chunk, "content") else str(chunk) for chunk in chunks)
    assert ai_runtime.EMPTY_RESPONSE_NOTICE in rendered
    # The notice-only turn records an empty completion, not the placeholder document.
    assert recorder.outcome == "completed"
    assert recorder.assistant_text == ""


@pytest.mark.asyncio
async def test_team_response_discards_whitespace_only_completed_run() -> None:
    """Whitespace-only completed content triggers the empty-run guard."""
    orchestrator, _config = _make_orchestrator()
    whitespace_run = TeamRunOutput(content="\n\n  ", run_id="team-run-1", session_id="session-1")
    whitespace_run.status = RunStatus.completed
    mock_team = _make_test_team()
    mock_team.arun = AsyncMock(side_effect=[whitespace_run, _completed_team_run("Recovered answer")])

    patches = _team_patches(mock_team)
    with patches[0], patches[1], patches[2]:
        response = await team_response(
            agent_names=["general"],
            mode=TeamMode.COORDINATE,
            message="Say something.",
            turn_recorder=TurnRecorder(user_message="Say something."),
            orchestrator=orchestrator,
            execution_identity=None,
            ctx=make_turn_context(session_id="session-1"),
        )

    assert "Recovered answer" in response
    assert mock_team.arun.await_count == 2


@pytest.mark.asyncio
async def test_team_response_ignores_history_messages_for_empty_detection() -> None:
    """Session-history assistant messages folded into run messages are not output.

    Agno copies prior turns into ``response.messages`` with
    ``from_history=True``; counting them as visible output made the guard
    dead after the first turn of a session and recycled old text as the
    reply.
    """
    orchestrator, _config = _make_orchestrator()
    history_only_run = TeamRunOutput(
        content=None,
        run_id="team-run-1",
        session_id="session-1",
        messages=[Message(role="assistant", content="Previous turn answer", from_history=True)],
    )
    history_only_run.status = RunStatus.completed
    mock_team = _make_test_team()
    mock_team.arun = AsyncMock(side_effect=[history_only_run, _completed_team_run("Recovered answer")])

    patches = _team_patches(mock_team)
    with patches[0], patches[1], patches[2]:
        response = await team_response(
            agent_names=["general"],
            mode=TeamMode.COORDINATE,
            message="Say something.",
            turn_recorder=TurnRecorder(user_message="Say something."),
            orchestrator=orchestrator,
            execution_identity=None,
            ctx=make_turn_context(session_id="session-1"),
        )

    assert "Recovered answer" in response
    assert "Previous turn answer" not in response
    assert mock_team.arun.await_count == 2


@pytest.mark.asyncio
async def test_team_empty_retry_shares_budget_with_dynamic_continuations() -> None:
    """One empty retry plus dynamic-tool continuations stay within the shared budget."""
    orchestrator, _config = _make_orchestrator()
    mock_team = _make_test_team()
    mock_team.arun = AsyncMock(
        side_effect=[
            _empty_team_run("team-run-1"),
            *[_dynamic_tool_team_output() for _ in range(DYNAMIC_TOOL_CONTINUATION_LIMIT)],
        ],
    )

    patches = _team_patches(mock_team)
    with patches[0], patches[1], patches[2]:
        response = await team_response(
            agent_names=["general"],
            mode=TeamMode.COORDINATE,
            message="Keep loading tools.",
            turn_recorder=TurnRecorder(user_message="Keep loading tools."),
            orchestrator=orchestrator,
            execution_identity=None,
            ctx=make_turn_context(session_id="session-1"),
        )

    # The empty retry borrows one continuation slot: 1 discarded empty run
    # plus LIMIT dynamic-tool runs, settling on the limit message.
    assert mock_team.arun.await_count == DYNAMIC_TOOL_CONTINUATION_LIMIT + 1
    assert "did not produce a final answer" in response


@pytest.mark.asyncio
async def test_team_response_records_empty_replayable_text_for_tool_only_run() -> None:
    """A tool-only blocking run keeps the fallback placeholder out of the recorded turn."""
    orchestrator, _config = _make_orchestrator()
    mock_team = _make_test_team()
    tool_only_run = TeamRunOutput(
        content="",
        run_id="team-run-1",
        session_id="session-1",
        member_responses=[
            RunOutput(
                agent_name="GeneralAgent",
                content="",
                tools=[
                    ToolExecution(
                        tool_call_id="call-1",
                        tool_name="get_time",
                        tool_args={},
                        result="noon",
                    ),
                ],
            ),
        ],
    )
    tool_only_run.status = RunStatus.completed
    mock_team.arun = AsyncMock(return_value=tool_only_run)
    recorder = TurnRecorder(user_message="Run the tool.")

    patches = _team_patches(mock_team)
    with patches[0], patches[1], patches[2]:
        response = await team_response(
            agent_names=["general"],
            mode=TeamMode.COORDINATE,
            message="Run the tool.",
            turn_recorder=recorder,
            orchestrator=orchestrator,
            execution_identity=None,
            ctx=make_turn_context(session_id="session-1"),
        )

    # The visible response keeps the display chrome; the recorded turn does not
    # replay it (matches the streaming path's event_has_visible guard).
    assert "No team response generated." in response
    assert recorder.outcome == "completed"
    assert recorder.assistant_text == ""


@pytest.mark.asyncio
async def test_team_response_stream_records_interrupted_turn_when_stream_errors() -> None:
    """A mid-stream team error with partial output marks the recorder interrupted."""
    orchestrator, config = _make_orchestrator()

    async def stream() -> AsyncIterator[object]:
        yield AgentRunContentEvent(agent_name="GeneralAgent", content="Member answer")
        yield TeamRunErrorEvent(content="provider exploded")

    mock_team = _make_test_team()
    mock_team.arun = MagicMock(return_value=stream())
    recorder = TurnRecorder(user_message="Analyze this.")

    patches = _team_patches(mock_team)
    with patches[0], patches[1], patches[2]:
        chunks = [
            chunk
            async for chunk in team_response_stream(
                agent_ids=[entity_ids(config, runtime_paths_for(config))["general"]],
                mode=TeamMode.COORDINATE,
                message="Analyze this.",
                turn_recorder=recorder,
                orchestrator=orchestrator,
                execution_identity=None,
                ctx=make_turn_context(session_id="session-1"),
            )
        ]

    rendered = "".join(chunk.content if hasattr(chunk, "content") else str(chunk) for chunk in chunks)
    assert "error" in rendered.lower()
    assert recorder.outcome == "interrupted"
    assert "Member answer" in recorder.assistant_text


@pytest.mark.asyncio
async def test_team_response_stream_records_interrupted_turn_on_errored_run_output() -> None:
    """A terminal errored run output after partial output marks the recorder interrupted."""
    orchestrator, config = _make_orchestrator()
    errored_run = TeamRunOutput(content="", run_id="team-run-1", session_id="session-1")
    errored_run.status = RunStatus.error

    async def stream() -> AsyncIterator[object]:
        yield AgentRunContentEvent(agent_name="GeneralAgent", content="Member answer")
        yield errored_run

    mock_team = _make_test_team()
    mock_team.arun = MagicMock(return_value=stream())
    recorder = TurnRecorder(user_message="Analyze this.")

    patches = _team_patches(mock_team)
    with patches[0], patches[1], patches[2]:
        _ = [
            chunk
            async for chunk in team_response_stream(
                agent_ids=[entity_ids(config, runtime_paths_for(config))["general"]],
                mode=TeamMode.COORDINATE,
                message="Analyze this.",
                turn_recorder=recorder,
                orchestrator=orchestrator,
                execution_identity=None,
                ctx=make_turn_context(session_id="session-1"),
            )
        ]

    assert recorder.outcome == "interrupted"
    assert "Member answer" in recorder.assistant_text


@pytest.mark.asyncio
async def test_team_response_stream_records_error_when_error_has_no_partial() -> None:
    """A zero-output team error still records the user turn for replay."""
    orchestrator, config = _make_orchestrator()

    async def stream() -> AsyncIterator[object]:
        yield TeamRunErrorEvent(content="provider exploded")

    mock_team = _make_test_team()
    mock_team.arun = MagicMock(return_value=stream())
    recorder = TurnRecorder(user_message="Analyze this.")

    patches = _team_patches(mock_team)
    with patches[0], patches[1], patches[2]:
        _ = [
            chunk
            async for chunk in team_response_stream(
                agent_ids=[entity_ids(config, runtime_paths_for(config))["general"]],
                mode=TeamMode.COORDINATE,
                message="Analyze this.",
                turn_recorder=recorder,
                orchestrator=orchestrator,
                execution_identity=None,
                ctx=make_turn_context(session_id="session-1"),
            )
        ]

    assert recorder.outcome == "interrupted"
    assert recorder.interruption_status is RunStatus.error
    assert recorder.assistant_text == ""


@pytest.mark.asyncio
async def test_team_response_stream_records_interrupted_turn_when_stream_raises() -> None:
    """A raw exception from the model stream records partial work interrupted."""
    orchestrator, config = _make_orchestrator()

    stream_error = "transport died"

    async def stream() -> AsyncIterator[object]:
        yield AgentRunContentEvent(agent_name="GeneralAgent", content="Member answer")
        raise RuntimeError(stream_error)

    mock_team = _make_test_team()
    mock_team.arun = MagicMock(return_value=stream())
    recorder = TurnRecorder(user_message="Analyze this.")

    patches = _team_patches(mock_team)
    with patches[0], patches[1], patches[2]:
        chunks = [
            chunk
            async for chunk in team_response_stream(
                agent_ids=[entity_ids(config, runtime_paths_for(config))["general"]],
                mode=TeamMode.COORDINATE,
                message="Analyze this.",
                turn_recorder=recorder,
                orchestrator=orchestrator,
                execution_identity=None,
                ctx=make_turn_context(session_id="session-1"),
            )
        ]

    rendered = "".join(chunk.content if hasattr(chunk, "content") else str(chunk) for chunk in chunks)
    assert "transport died" in rendered
    assert recorder.outcome == "interrupted"
    assert "Member answer" in recorder.assistant_text
