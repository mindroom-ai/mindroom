"""Tests for the shared locked-turn delivery state machine and terminal arms."""

from __future__ import annotations

from typing import TYPE_CHECKING, cast
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from mindroom.bot import AgentBot
from mindroom.constants import STREAM_STATUS_KEY, STREAM_STATUS_PENDING
from mindroom.delivery_gateway import DeliveryGateway
from mindroom.matrix.client_delivery import DeliveredMatrixEvent
from mindroom.message_target import MessageTarget
from mindroom.response_runner import ResponseRunner, _DeliveryProgress, _ResponseGenerationOutcome
from tests.ai_user_id_helpers import (
    _build_response_runner,
    _config_with_team,
    _knowledge_access_support,
    _response_request,
    _runtime_paths,
    _set_gateway_method,
    _team_orchestrator,
)
from tests.conftest import bind_runtime_paths, patch_response_runner_module, unwrap_extracted_collaborator
from tests.identity_helpers import fixture_entity_matrix_id
from tests.response_runner_helpers import _bot, _noop_typing, _plain_request, _target

if TYPE_CHECKING:
    from collections.abc import Awaitable, Callable
    from pathlib import Path

    from mindroom.response_runner import ResponseRequest


class _FakeHomeserver:
    """A room that dedups on transaction ID and can lose one confirmation.

    Both halves matter. Deduplication is what lets a resend under the same
    transaction ID collapse onto an event the server already has, and losing a
    confirmation is the only way to produce the state this test is about: a
    send whose outcome the client cannot know.
    """

    def __init__(self, *, accepts_first: bool) -> None:
        self.events: list[tuple[str, str | None]] = []
        self.placeholder_sends: list[tuple[str | None, object]] = []
        self._event_id_by_transaction: dict[str, str] = {}
        self._accepts_first = accepts_first
        self._seen_first = False

    async def send(
        self,
        _client: object,
        _room_id: str,
        content: dict[str, object],
        *,
        operation: str = "send_message",
        retry_sync_recovery: bool = False,
        transaction_id: str | None = None,
    ) -> DeliveredMatrixEvent | None:
        """Accept one send, deduplicating on transaction ID like a homeserver."""
        del operation, retry_sync_recovery
        if content.get("body") == "Thinking...":
            self.placeholder_sends.append((transaction_id, content.get(STREAM_STATUS_KEY)))
        if transaction_id is not None and transaction_id in self._event_id_by_transaction:
            return DeliveredMatrixEvent(
                event_id=self._event_id_by_transaction[transaction_id],
                content_sent=dict(content),
            )
        first = not self._seen_first
        self._seen_first = True
        if first and not self._accepts_first:
            return None
        event_id = f"$event-{len(self.events)}"
        self.events.append((event_id, str(content.get("body", ""))))
        if transaction_id is not None:
            self._event_id_by_transaction[transaction_id] = event_id
        # The first send's confirmation never comes back, whether or not the
        # server kept the event.
        return None if first else DeliveredMatrixEvent(event_id=event_id, content_sent=dict(content))

    def placeholders(self) -> list[str]:
        """Return every visible placeholder event this room ever accepted."""
        return [event_id for event_id, body in self.events if body == "Thinking..."]


@pytest.mark.asyncio
@pytest.mark.parametrize("server_kept_the_placeholder", [True, False])
async def test_one_turn_never_shows_two_placeholders(
    tmp_path: Path,
    *,
    server_kept_the_placeholder: bool,
) -> None:
    """An unconfirmed placeholder send must not be answered with a second one.

    The turn's placeholder goes out through a durable ``INITIAL`` outbox row so
    that exactly one thing owns it. When that send cannot be confirmed the row
    is deliberately left unacknowledged, because it is the only record that
    something may already be in the room under its transaction ID -- and
    recovery will resend it under that same ID, collapsing onto the event if
    the server kept it and creating it if it did not.

    Sending a fallback placeholder outside the outbox breaks that in both
    directions. If the server kept the first one, the fallback is a second
    visible "Thinking..." immediately, and only the fallback is ever edited
    into the answer. If it did not, the fallback is in the room and recovery
    still owes the row, so the next start adds the duplicate instead.

    The count is the assertion. A turn produces at most one placeholder, no
    matter which way the unconfirmed send actually went.
    """
    bot = _bot(tmp_path)
    coordinator = unwrap_extracted_collaborator(bot._response_runner)
    homeserver = _FakeHomeserver(accepts_first=server_kept_the_placeholder)

    async def die_before_answering(_request: object, **_kwargs: object) -> _ResponseGenerationOutcome:
        msg = "process died before the answer"
        raise RuntimeError(msg)

    with (
        patch("mindroom.delivery_gateway.send_message_result", new=homeserver.send),
        patch.object(coordinator, "_process_and_respond", new=AsyncMock(side_effect=die_before_answering)),
        patch_response_runner_module(
            should_use_streaming=AsyncMock(return_value=False),
            typing_indicator=_noop_typing,
            apply_post_response_effects=AsyncMock(),
        ),
    ):
        with pytest.raises(RuntimeError, match="process died before the answer"):
            await coordinator.generate_response(_plain_request(_target()))
        assert len(homeserver.placeholders()) <= 1, "the turn itself put two placeholders in the room"

        # The turn died before enqueueing FINAL, so nothing supersedes the
        # placeholder row and the next start resends it.
        await bot._delivery_gateway.recover_deliveries()

    assert homeserver.placeholders() == ["$event-0"], (
        "one turn produced more than one visible placeholder across failure and recovery"
    )
    # Every attempt carried one owned transaction ID -- which is what makes the
    # resend collapse rather than add a message -- and the pending marker that
    # makes clients render it as a placeholder.
    transaction_ids = {transaction_id for transaction_id, _ in homeserver.placeholder_sends}
    assert len(transaction_ids) == 1, "the placeholder was attempted under more than one transaction"
    assert None not in transaction_ids, "a placeholder went out on the unowned direct path"
    assert {status for _, status in homeserver.placeholder_sends} == {STREAM_STATUS_PENDING}


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
    effect_response_outcomes: list[object] = []

    async def fake_post_effects(final_outcome: object, response_outcome: object, *_args: object) -> None:
        effect_outcomes.append(final_outcome)
        effect_response_outcomes.append(response_outcome)

    async def failing_process(_request: object, **kwargs: object) -> _ResponseGenerationOutcome:
        on_delivery_started = cast("Callable[[str | None], None]", kwargs["on_delivery_started"])
        collector = cast("list[str]", kwargs["attempt_run_id_collector"])
        collector.append("run-attempt-1")
        on_delivery_started("$stream-event")
        msg = "delivery pipe burst"
        raise RuntimeError(msg)

    with (
        patch.object(DeliveryGateway, "send_text", new=AsyncMock(return_value="$thinking")),
        patch.object(DeliveryGateway, "finalize_streamed_response", new=AsyncMock()) as mock_finalize,
        patch.object(coordinator, "_process_and_respond", new=AsyncMock(side_effect=failing_process)),
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
    # The caller-owned collector keeps the real attempt id on raising paths.
    assert effect_response_outcomes[0].response_run_id == "run-attempt-1"


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
        patch.object(coordinator, "_process_and_respond", new=AsyncMock(side_effect=failing_process)),
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
            "_run_cancellable_response",
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
                "_run_cancellable_response",
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
