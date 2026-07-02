"""Tests for the shared locked-turn delivery state machine and terminal arms."""

from __future__ import annotations

from typing import TYPE_CHECKING, cast
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from mindroom.bot import AgentBot
from mindroom.delivery_gateway import DeliveryGateway
from mindroom.message_target import MessageTarget
from mindroom.response_runner import ResponseRunner, _DeliveryProgress, _ResponseGenerationOutcome
from tests.conftest import bind_runtime_paths, patch_response_runner_module, unwrap_extracted_collaborator
from tests.identity_helpers import fixture_entity_matrix_id
from tests.test_ai_user_id import (
    _build_response_runner,
    _config_with_team,
    _knowledge_access_support,
    _response_request,
    _runtime_paths,
    _set_gateway_method,
    _team_orchestrator,
)
from tests.test_response_runner_focused import _bot, _noop_typing, _plain_request, _target

if TYPE_CHECKING:
    from collections.abc import Awaitable, Callable
    from pathlib import Path

    from mindroom.response_runner import ResponseRequest


def test_delivery_progress_transitions() -> None:
    """The delivery-progress state machine tracks events and terminal reasons."""
    progress = _DeliveryProgress(tracked_event_id=None)
    assert progress.terminal_event_id(_plain_request(_target()), None) is None

    progress.track_event(None)
    assert progress.tracked_event_id is None
    progress.track_event("$first")
    progress.track_event("$second")
    assert progress.tracked_event_id == "$second"

    progress.note_delivery_started(None)
    assert progress.stage_started is True
    assert progress.tracked_event_id == "$second"

    progress.note_task_cancelled("cancelled_by_user")
    assert progress.cancelled is True
    assert progress.failure_reason == "cancelled_by_user"

    # terminal_event_id falls back to the run message only when nothing is tracked.
    fresh = _DeliveryProgress(tracked_event_id=None)
    assert fresh.terminal_event_id(_plain_request(_target()), "$thinking") == "$thinking"
    kept = _DeliveryProgress(tracked_event_id="$existing")
    assert kept.terminal_event_id(_plain_request(_target()), "$thinking") == "$existing"


def test_delivery_progress_placeholder_fallback() -> None:
    """With nothing tracked, a placeholder existing event is the terminal target."""
    request = _plain_request(_target())
    placeholder_request: ResponseRequest = request.__class__(
        **{**request.__dict__, "existing_event_id": "$placeholder", "existing_event_is_placeholder": True},
    )
    progress = _DeliveryProgress(tracked_event_id=None)
    assert progress.terminal_event_id(placeholder_request, None) == "$placeholder"


@pytest.mark.asyncio
async def test_agent_post_delivery_failure_finalizes_error_outcome(tmp_path: Path) -> None:
    """A failure after delivery started finalizes a terminal error instead of asserting."""
    bot = _bot(tmp_path)
    coordinator = unwrap_extracted_collaborator(bot._response_runner)
    finalize_requests: list[object] = []

    async def fake_finalize(finalize_request: object) -> object:
        finalize_requests.append(finalize_request)
        outcome = MagicMock()
        outcome.terminal_status = "error"
        outcome.final_visible_event_id = "$thinking"
        outcome.mark_handled = True
        return outcome

    async def failing_process(_request: object, **kwargs: object) -> _ResponseGenerationOutcome:
        on_delivery_started = cast("Callable[[str | None], None]", kwargs["on_delivery_started"])
        on_delivery_started("$stream-event")
        msg = "delivery pipe burst"
        raise RuntimeError(msg)

    with (
        patch.object(DeliveryGateway, "send_text", new=AsyncMock(return_value="$thinking")),
        patch.object(DeliveryGateway, "finalize_streamed_response", new=AsyncMock(side_effect=fake_finalize)),
        patch.object(coordinator, "process_and_respond", new=AsyncMock(side_effect=failing_process)),
        patch_response_runner_module(
            should_use_streaming=AsyncMock(return_value=False),
            typing_indicator=_noop_typing,
            apply_post_response_effects=AsyncMock(return_value=None),
        ),
    ):
        result = await coordinator.generate_response(_plain_request(_target()))

    # Previously this path tripped `assert final_delivery_outcome is not None`.
    assert result == "$thinking"
    assert len(finalize_requests) == 1
    transport_outcome = finalize_requests[0].stream_transport_outcome
    assert transport_outcome.terminal_status == "error"
    assert transport_outcome.failure_reason == "delivery pipe burst"


@pytest.mark.asyncio
async def test_team_pre_delivery_failure_finalizes_terminal_note_and_reraises(tmp_path: Path) -> None:
    """A team failure before delivery edits an error note and still re-raises."""
    runtime_paths = _runtime_paths(tmp_path)
    config = bind_runtime_paths(_config_with_team(), runtime_paths)
    bot = MagicMock(spec=AgentBot)
    bot.logger = MagicMock()
    bot.stop_manager = MagicMock()
    bot.stop_manager.remove_stop_button = AsyncMock()
    bot.client = AsyncMock()
    bot.agent_name = "ultimate"
    bot.storage_path = tmp_path
    bot.config = config
    bot.runtime_paths = runtime_paths
    bot._knowledge_access_support = _knowledge_access_support()

    finalize_requests: list[object] = []

    async def fake_finalize(finalize_request: object) -> object:
        finalize_requests.append(finalize_request)
        outcome = MagicMock()
        outcome.terminal_status = "error"
        outcome.final_visible_event_id = "$thinking"
        outcome.mark_handled = True
        return outcome

    with (
        patch("mindroom.response_runner.should_use_streaming", new=AsyncMock(return_value=False)),
        patch("mindroom.response_lifecycle.apply_post_response_effects", new=AsyncMock(return_value=None)),
        patch(
            "mindroom.response_runner.team_response",
            new=AsyncMock(side_effect=RuntimeError("team prep exploded")),
        ),
        patch("mindroom.response_runner.typing_indicator", _noop_typing),
    ):
        coordinator = _build_response_runner(
            bot,
            config=config,
            runtime_paths=runtime_paths,
            storage_path=tmp_path,
            requester_id="@alice:localhost",
            message_target=MessageTarget.resolve("!test:localhost", "$thread-root", "$user_msg"),
            orchestrator=_team_orchestrator(config, runtime_paths),
        )
        _set_gateway_method(
            coordinator.deps.delivery_gateway,
            "finalize_streamed_response",
            AsyncMock(side_effect=fake_finalize),
        )
        _set_gateway_method(coordinator.deps.delivery_gateway, "send_text", AsyncMock(return_value="$thinking"))
        with (
            patch.object(
                ResponseRunner,
                "run_cancellable_response",
                new=AsyncMock(side_effect=_run_response_function_directly),
            ),
            pytest.raises(RuntimeError, match="team prep exploded"),
        ):
            await coordinator.generate_team_response_helper(
                _response_request(prompt="Hello", user_id="@alice:localhost", thread_id="$thread-root"),
                team_agents=[fixture_entity_matrix_id("general", "localhost", runtime_paths)],
                team_mode="coordinate",
            )

    # Previously the exception propagated raw with no terminal note or finalize.
    assert len(finalize_requests) == 1
    transport_outcome = finalize_requests[0].stream_transport_outcome
    assert transport_outcome.terminal_status == "error"
    assert "team prep exploded" in str(transport_outcome.failure_reason)


async def _run_response_function_directly(**kwargs: object) -> str:
    """Drive the locked closure like the attempt runner would, without swallowing."""
    response_function = cast("Callable[[str | None], Awaitable[None]]", kwargs["response_function"])
    await response_function("$thinking")
    return "$thinking"
