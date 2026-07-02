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


@pytest.mark.asyncio
async def test_agent_post_delivery_failure_settles_error_outcome(tmp_path: Path) -> None:
    """A failure after delivery started settles a terminal error instead of asserting.

    The tracked event must not be touched: with an adopted thinking-message
    stream it can already carry the full streamed reply, and the
    placeholder-only cleanup in finalize would redact it.
    """
    bot = _bot(tmp_path)
    coordinator = unwrap_extracted_collaborator(bot._response_runner)
    effect_outcomes: list[object] = []

    async def fake_post_effects(final_outcome: object, *_args: object) -> None:
        effect_outcomes.append(final_outcome)

    async def failing_process(_request: object, **kwargs: object) -> _ResponseGenerationOutcome:
        on_delivery_started = cast("Callable[[str | None], None]", kwargs["on_delivery_started"])
        on_delivery_started("$stream-event")
        msg = "delivery pipe burst"
        raise RuntimeError(msg)

    with (
        patch.object(DeliveryGateway, "send_text", new=AsyncMock(return_value="$thinking")),
        patch.object(DeliveryGateway, "finalize_streamed_response", new=AsyncMock()) as mock_finalize,
        patch.object(coordinator, "process_and_respond", new=AsyncMock(side_effect=failing_process)),
        patch_response_runner_module(
            should_use_streaming=AsyncMock(return_value=False),
            typing_indicator=_noop_typing,
            apply_post_response_effects=AsyncMock(side_effect=fake_post_effects),
        ),
    ):
        result = await coordinator.generate_response(_plain_request(_target()))

    # Previously this path tripped `assert final_delivery_outcome is not None`.
    assert result is None
    mock_finalize.assert_not_awaited()
    assert len(effect_outcomes) == 1
    assert effect_outcomes[0].terminal_status == "error"
    assert effect_outcomes[0].failure_reason == "delivery pipe burst"


@pytest.mark.asyncio
async def test_agent_regeneration_pre_delivery_failure_leaves_prior_answer_intact(tmp_path: Path) -> None:
    """A pre-delivery failure while regenerating must not redact the prior answer.

    The existing event is a real prior response, not a placeholder. Routing it
    through a forced-placeholder terminal outcome would let the gateway's
    placeholder-only cleanup redact it; the real pending-visible shape makes
    the gateway return a bookkeeping outcome without touching Matrix.
    """
    bot = _bot(tmp_path)
    coordinator = unwrap_extracted_collaborator(bot._response_runner)
    effect_outcomes: list[object] = []

    async def fake_post_effects(final_outcome: object, *_args: object) -> None:
        effect_outcomes.append(final_outcome)

    async def failing_process(_request: object, **_kwargs: object) -> _ResponseGenerationOutcome:
        msg = "regen prep exploded"
        raise RuntimeError(msg)

    request = _plain_request(_target())
    regen_request: ResponseRequest = request.__class__(
        **{**request.__dict__, "existing_event_id": "$prior_answer", "existing_event_is_placeholder": False},
    )

    with (
        patch.object(DeliveryGateway, "send_text", new=AsyncMock(return_value="$thinking")),
        patch.object(coordinator, "process_and_respond", new=AsyncMock(side_effect=failing_process)),
        patch_response_runner_module(
            should_use_streaming=AsyncMock(return_value=False),
            typing_indicator=_noop_typing,
            apply_post_response_effects=AsyncMock(side_effect=fake_post_effects),
        ),
        pytest.raises(RuntimeError, match="regen prep exploded"),
    ):
        await coordinator.generate_response(regen_request)

    # The prior answer event survives as the visible outcome target; a
    # placeholder-only cleanup would have redacted it instead.
    assert len(effect_outcomes) == 1
    assert effect_outcomes[0].terminal_status == "error"
    assert effect_outcomes[0].event_id == "$prior_answer"
    assert effect_outcomes[0].is_visible_response is True


@pytest.mark.asyncio
async def test_team_post_delivery_failure_settles_error_outcome_without_finalize(tmp_path: Path) -> None:
    """A team failure after delivery started settles a bare terminal error.

    Mirrors the agent arm: the tracked event must not be routed through
    finalize, because with an adopted thinking-message stream it can already
    hold the full streamed reply and the placeholder-only cleanup would
    redact it.
    """
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
    effect_outcomes: list[object] = []

    async def fake_post_effects(final_outcome: object, *_args: object) -> None:
        effect_outcomes.append(final_outcome)

    with (
        patch("mindroom.response_runner.should_use_streaming", new=AsyncMock(return_value=False)),
        patch(
            "mindroom.response_lifecycle.apply_post_response_effects",
            new=AsyncMock(side_effect=fake_post_effects),
        ),
        patch("mindroom.response_runner.team_response", new=AsyncMock(return_value="Team answer")),
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
            "deliver_final",
            AsyncMock(side_effect=RuntimeError("delivery pipe burst")),
        )
        _set_gateway_method(
            coordinator.deps.delivery_gateway,
            "finalize_streamed_response",
            AsyncMock(side_effect=lambda req: finalize_requests.append(req)),
        )
        _set_gateway_method(coordinator.deps.delivery_gateway, "send_text", AsyncMock(return_value="$thinking"))
        with patch.object(
            ResponseRunner,
            "run_cancellable_response",
            new=AsyncMock(side_effect=_run_response_function_directly),
        ):
            await coordinator.generate_team_response_helper(
                _response_request(prompt="Hello", user_id="@alice:localhost", thread_id="$thread-root"),
                team_agents=[fixture_entity_matrix_id("general", "localhost", runtime_paths)],
                team_mode="coordinate",
            )

    # Post-start failures settle bare: no finalize call, terminal error effects.
    assert finalize_requests == []
    assert len(effect_outcomes) == 1
    assert effect_outcomes[0].terminal_status == "error"
    assert "delivery pipe burst" in str(effect_outcomes[0].failure_reason)


@pytest.mark.asyncio
async def test_team_pre_delivery_failure_finalizes_terminal_note_and_reraises(tmp_path: Path) -> None:
    """A team failure before delivery cleans the thinking placeholder and re-raises.

    The attempt runner already sent the thinking message but the local
    run_message_id was never assigned (the attempt raised), so the transport
    outcome must classify the tracked thinking event as placeholder-only —
    otherwise the gateway leaves "Thinking..." dangling with no cleanup.
    """
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
    # The dangling thinking placeholder must be classified for cleanup; a
    # "none"-shaped outcome would leave "Thinking..." dangling forever.
    assert transport_outcome.last_physical_stream_event_id == "$thinking"
    assert transport_outcome.visible_body_state == "placeholder_only"


async def _run_response_function_directly(**kwargs: object) -> str:
    """Drive the locked closure like the attempt runner would, without swallowing."""
    response_function = cast("Callable[[str | None], Awaitable[None]]", kwargs["response_function"])
    await response_function("$thinking")
    return "$thinking"
