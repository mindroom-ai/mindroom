"""A turn tells plugins when it starts, pauses for an approval, and resumes, and exactly once how it ended."""

from __future__ import annotations

import asyncio
from dataclasses import replace
from typing import TYPE_CHECKING
from unittest.mock import AsyncMock, patch

import pytest

from mindroom.approval.tool_approval import shutdown_approval_runtime
from mindroom.cancellation import request_task_cancel
from mindroom.final_delivery import AwaitingApproval, Cancelled, Completed, FinalDeliveryOutcome
from mindroom.hooks import (
    BUILTIN_EVENT_NAMES,
    EVENT_MESSAGE_AFTER_RESPONSE,
    EVENT_MESSAGE_CANCELLED,
    EVENT_TURN_ENDED,
    EVENT_TURN_PAUSED,
    EVENT_TURN_RESUMED,
    EVENT_TURN_STARTED,
    HookContext,
    HookRegistry,
    TurnLifecycleContext,
    default_timeout_ms_for_event,
    hook,
    validate_event_name,
)
from mindroom.llm.provider import TextDelta
from mindroom.logging_config import get_logger
from mindroom.post_response_effects import PostResponseEffectsDeps, ResponseOutcome
from mindroom.response_turn import CompletedApprovalRun
from mindroom.tool_system.runtime_context import get_tool_runtime_context
from tests.approval_continuation_helpers import dispatch_approval
from tests.conftest import patch_response_runner_module, publish_hook_plugin, unwrap_extracted_collaborator
from tests.fakes.scripted_model import Await, answer, calls, tool_call
from tests.response_runner_helpers import _noop_typing, _plain_request, _target
from tests.test_cancelled_response_hook import _lifecycle_with, _response_hook_service
from tests.test_reply_endings_characterization import (  # noqa: F401 - echo_tool is a pytest fixture
    _ECHO_TOOL,
    _explode,
    _require_echo_approval,
    _streams_then_blocks,
    _team_reply,
    _team_scripts,
    _turn,
    _wait_shown,
    echo_tool,
)
from tests.test_reply_endings_characterization import _stop as _stop_turn
from tests.test_reply_records_approvals import (
    _approval_bot,
    _journal_wakes,
    _paused,
    _respond,
    _resume_until_cancelled,
    _run_approval_wakes,
)
from tests.test_reply_records_turns import _model_stream, _stop
from tests.test_tool_hooks import _plugin

if TYPE_CHECKING:
    from pathlib import Path

    from mindroom.bot import AgentBot
    from mindroom.response_lifecycle import ResponseLifecycle

pytestmark = pytest.mark.asyncio

_TURN_EVENTS = (EVENT_TURN_STARTED, EVENT_TURN_PAUSED, EVENT_TURN_RESUMED, EVENT_TURN_ENDED)

type _Seen = list[tuple[str, str | None, str | None]]


def _recorders(seen: _Seen, events: tuple[str, ...] = _TURN_EVENTS) -> list[object]:
    """Return one hook per event that records the event, its turn ID, and the outcome a turn event carries."""

    def recorder(event_name: str) -> object:
        @hook(event_name, name=f"record_{event_name.replace(':', '_')}")
        async def record(ctx: HookContext) -> None:
            outcome = ctx.outcome if isinstance(ctx, TurnLifecycleContext) else None
            seen.append((ctx.event_name, ctx.turn_id, outcome))

        return record

    return [recorder(event_name) for event_name in events]


def _record_turn_events(bot: AgentBot, seen: _Seen, events: tuple[str, ...] = _TURN_EVENTS) -> None:
    """Publish a plugin whose hooks record ``events`` into ``seen`` beside the bot's other plugins."""
    bot.orchestrator.hook_registry_state.registry = publish_hook_plugin(
        bot.config,
        bot.runtime_paths,
        "turn-events",
        _recorders(seen, events),
    ).hooks


def _events(seen: _Seen) -> list[tuple[str, str | None]]:
    return [(event, outcome) for event, _turn, outcome in seen]


@pytest.mark.usefixtures("echo_tool")
@pytest.mark.parametrize("streaming", [True, False], ids=["streamed", "whole"])
async def test_a_reply_starts_its_turn_and_ends_it_completed_after_its_answer(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    streaming: bool,
) -> None:
    """The turn starts before its tool call and ends completed once message:after_response saw its answer."""
    turn = await _turn(
        tmp_path,
        monkeypatch,
        streaming=streaming,
        tools=[_ECHO_TOOL],
        scripts={"default": [calls(tool_call("echo", "call-1", text="hi")), answer("The answer.")]},
    )
    seen: _Seen = []
    _record_turn_events(turn.bot, seen, (*_TURN_EVENTS, EVENT_MESSAGE_AFTER_RESPONSE))
    request = _plain_request(_target())

    await turn.runner.generate_response(request)

    assert seen == [
        (EVENT_TURN_STARTED, request.turn_id, None),
        (EVENT_MESSAGE_AFTER_RESPONSE, request.turn_id, None),
        (EVENT_TURN_ENDED, request.turn_id, "completed"),
    ]


@pytest.mark.parametrize("streaming", [True, False], ids=["streamed", "whole"])
async def test_a_model_failure_ends_the_turn_failed_though_its_reply_shows_the_error(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    streaming: bool,
) -> None:
    """A run that fails answers with its error, which completes the reply but fails the turn."""
    turn = await _turn(tmp_path, monkeypatch, streaming=streaming, scripts={"default": [[Await(_explode)]]})
    seen: _Seen = []
    _record_turn_events(turn.bot, seen)

    await turn.runner.generate_response(_plain_request(_target()))

    assert _events(seen) == [(EVENT_TURN_STARTED, None), (EVENT_TURN_ENDED, "failed")]


async def test_a_stopped_reply_ends_its_turn_cancelled_once(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """A Stop mid-stream ends the turn cancelled, after its message:cancelled."""
    running = asyncio.Event()
    turn = await _turn(
        tmp_path,
        monkeypatch,
        streaming=True,
        scripts={"default": [_streams_then_blocks("Partial answer", running)]},
    )
    seen: _Seen = []
    _record_turn_events(turn.bot, seen, (*_TURN_EVENTS, EVENT_MESSAGE_CANCELLED))
    response = asyncio.create_task(turn.runner.generate_response(_plain_request(_target())))
    await asyncio.wait_for(running.wait(), timeout=10)
    await _wait_shown(turn, "Partial answer")

    assert await _stop_turn(turn)
    await asyncio.wait_for(response, timeout=10)

    assert _events(seen) == [
        (EVENT_TURN_STARTED, None),
        (EVENT_MESSAGE_CANCELLED, None),
        (EVENT_TURN_ENDED, "cancelled"),
    ]


async def test_a_cancellation_while_turn_started_hooks_run_settles_the_turn_cancelled(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A turn cancelled as it starts settles as any cancelled turn does, with message:cancelled before its end."""
    turn = await _turn(tmp_path, monkeypatch, streaming=False, scripts={"default": [answer("Never shown.")]})
    seen: _Seen = []

    @hook(EVENT_TURN_STARTED, name="cancelling_start")
    async def cancelling_start(_ctx: TurnLifecycleContext) -> None:
        raise asyncio.CancelledError

    turn.bot.orchestrator.hook_registry_state.registry = publish_hook_plugin(
        turn.bot.config,
        turn.bot.runtime_paths,
        "turn-events",
        [cancelling_start, *_recorders(seen, (EVENT_TURN_ENDED, EVENT_MESSAGE_CANCELLED))],
    ).hooks

    with pytest.raises(asyncio.CancelledError):
        await turn.runner.generate_response(_plain_request(_target()))

    assert _events(seen) == [(EVENT_MESSAGE_CANCELLED, None), (EVENT_TURN_ENDED, "cancelled")]


async def test_a_process_shutdown_mid_turn_ends_no_turn(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """A turn MindRoom's shutdown cuts short fires no turn:ended; its replay is a turn of its own."""
    running = asyncio.Event()
    turn = await _turn(
        tmp_path,
        monkeypatch,
        streaming=True,
        scripts={"default": [_streams_then_blocks("Partial answer", running)]},
    )
    seen: _Seen = []
    _record_turn_events(turn.bot, seen)
    response = asyncio.create_task(turn.runner.generate_response(_plain_request(_target())))
    await asyncio.wait_for(running.wait(), timeout=10)

    request_task_cancel(response, process_shutdown=True)
    with pytest.raises(asyncio.CancelledError):
        await asyncio.wait_for(response, timeout=10)

    assert _events(seen) == [(EVENT_TURN_STARTED, None)]


async def test_a_team_reply_is_one_turn_that_ends_completed(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """A team's members answer inside the team's one turn."""
    turn = await _turn(
        tmp_path,
        monkeypatch,
        streaming=False,
        scripts=_team_scripts(consensus=answer("Both facts.")),
    )
    seen: _Seen = []
    _record_turn_events(turn.bot, seen)

    await _team_reply(turn)

    assert _events(seen) == [(EVENT_TURN_STARTED, None), (EVENT_TURN_ENDED, "completed")]
    assert len({turn_id for _event, turn_id, _outcome in seen}) == 1


async def test_an_empty_prompt_runs_no_turn(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """A request that runs nothing starts no turn."""
    turn = await _turn(tmp_path, monkeypatch, streaming=False, scripts={"default": [[TextDelta("never")]]})
    seen: _Seen = []
    _record_turn_events(turn.bot, seen)
    await turn.runner.generate_response(replace(_plain_request(_target()), prompt=" "))

    assert seen == []


@pytest.mark.usefixtures("echo_tool")
async def test_a_reply_that_pauses_for_approval_ends_no_turn(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """A tool call waiting for approval pauses the turn; the turn has not ended."""
    turn = await _turn(
        tmp_path,
        monkeypatch,
        streaming=True,
        tools=[_ECHO_TOOL],
        scripts={"default": [calls(tool_call("echo", "call-1", text="hi"), content="Before approval")]},
    )
    _require_echo_approval(turn)
    seen: _Seen = []
    _record_turn_events(turn.bot, seen)
    request = _plain_request(_target())
    try:
        await turn.runner.generate_response(request)
    finally:
        await shutdown_approval_runtime()

    assert seen == [(EVENT_TURN_STARTED, request.turn_id, None), (EVENT_TURN_PAUSED, request.turn_id, None)]


async def test_an_approved_reply_pauses_resumes_and_ends_as_one_turn(tmp_path: Path) -> None:
    """The pause, the resume the approval runs, and the end carry the turn ID of the request that started the turn."""
    seen: _Seen = []
    async with _approval_bot(tmp_path, requires_human=False) as bot:
        _record_turn_events(bot, seen)
        request = _plain_request(_target())
        await _respond(bot, request=request, resume=AsyncMock(return_value=CompletedApprovalRun("Approved.", {})))

    assert seen == [
        (EVENT_TURN_STARTED, request.turn_id, None),
        (EVENT_TURN_PAUSED, request.turn_id, None),
        (EVENT_TURN_RESUMED, request.turn_id, None),
        (EVENT_TURN_ENDED, request.turn_id, "completed"),
    ]


async def test_each_pause_of_a_chained_approval_resumes_the_same_turn(tmp_path: Path) -> None:
    """A resume that pauses again pauses the turn again; the last resume ends it."""
    seen: _Seen = []
    async with _approval_bot(tmp_path, requires_human=False) as bot:
        _record_turn_events(bot, seen)
        resume = AsyncMock(side_effect=[_paused(run_id="run-2"), CompletedApprovalRun("Both read.", {})])
        await _respond(bot, resume=resume)

    assert _events(seen) == [
        (EVENT_TURN_STARTED, None),
        (EVENT_TURN_PAUSED, None),
        (EVENT_TURN_RESUMED, None),
        (EVENT_TURN_PAUSED, None),
        (EVENT_TURN_RESUMED, None),
        (EVENT_TURN_ENDED, "completed"),
    ]
    assert len({turn_id for _event, turn_id, _outcome in seen}) == 1


async def test_a_resume_that_fails_ends_the_turn_failed_once(tmp_path: Path) -> None:
    """The failed resume ends the turn; the approval's settlement afterwards does not end it again."""
    seen: _Seen = []
    async with _approval_bot(tmp_path, requires_human=False) as bot:
        _record_turn_events(bot, seen)
        await _respond(bot, resume=AsyncMock(side_effect=RuntimeError("tool exploded")))

    assert _events(seen) == [
        (EVENT_TURN_STARTED, None),
        (EVENT_TURN_PAUSED, None),
        (EVENT_TURN_RESUMED, None),
        (EVENT_TURN_ENDED, "failed"),
    ]


async def test_a_stop_during_a_resume_ends_the_turn_cancelled_once(tmp_path: Path) -> None:
    """The stopped resume ends the turn; the settlement its Stop wakes does not end it again."""
    seen: _Seen = []
    async with _approval_bot(tmp_path, requires_human=False) as bot:
        _record_turn_events(bot, seen)
        resuming = asyncio.Event()
        response = asyncio.create_task(_respond(bot, resume=_resume_until_cancelled(resuming)))
        await asyncio.wait_for(resuming.wait(), timeout=5)
        with _journal_wakes(bot):
            assert await asyncio.wait_for(await _stop(bot, "$sent1", 5), timeout=5)
            await asyncio.wait_for(response, timeout=5)
        # The cancelled resume already ran the settlement its wake asks for.
        assert await unwrap_extracted_collaborator(bot._response_runner).handoff_approval_source("$event") is None

    assert _events(seen) == [
        (EVENT_TURN_STARTED, None),
        (EVENT_TURN_PAUSED, None),
        (EVENT_TURN_RESUMED, None),
        (EVENT_TURN_ENDED, "cancelled"),
    ]


async def test_a_stop_on_a_paused_reply_ends_its_turn_cancelled(tmp_path: Path) -> None:
    """A paused turn whose approval a Stop settles without resuming ends cancelled, under its own turn ID."""
    seen: _Seen = []
    async with _approval_bot(tmp_path, requires_human=True) as bot:
        _record_turn_events(bot, seen)
        request = _plain_request(_target())
        await _respond(bot, request=request)
        with _journal_wakes(bot) as wakes:
            assert await asyncio.wait_for(await _stop(bot, "$sent1", 5), timeout=5)
        await _run_approval_wakes(bot, wakes)

    assert seen == [
        (EVENT_TURN_STARTED, request.turn_id, None),
        (EVENT_TURN_PAUSED, request.turn_id, None),
        (EVENT_TURN_ENDED, request.turn_id, "cancelled"),
    ]


async def test_a_turn_cut_short_while_it_waits_in_place_for_approval_ends_once(tmp_path: Path) -> None:
    """A run that waits in place for an approval keeps its turn running; the settlement after it ends the turn no more."""
    seen: _Seen = []
    async with _approval_bot(tmp_path, requires_human=True) as bot:
        _record_turn_events(bot, seen)
        runner = unwrap_extracted_collaborator(bot._response_runner)

        async def wait_in_place(*_args: object, **_kwargs: object) -> str:
            context = get_tool_runtime_context()
            assert context is not None
            assert context.cli_approval_handler is not None
            cli_call = {"kind": "agent_cli", "call_id": "inner", "parent_bash_call_id": "bash"}
            await context.cli_approval_handler(replace(_paused(), cli_call=cli_call))
            return "Never answered."

        published = asyncio.Event()
        publish_generation = runner.approvals._publish_generation

        async def publish_then_wait(*args: object, **kwargs: object) -> object:
            result = await publish_generation(*args, **kwargs)
            published.set()
            return result

        with (
            patch_response_runner_module(
                stream_agent_response=_model_stream(AsyncMock(side_effect=wait_in_place)),
                should_use_streaming=AsyncMock(return_value=False),
                typing_indicator=_noop_typing,
            ),
            patch.object(runner.approvals, "_publish_generation", new=publish_then_wait),
        ):
            response = asyncio.create_task(runner.generate_response(_plain_request(_target())))
            await asyncio.wait_for(published.wait(), timeout=5)
            request_task_cancel(response, cancel_source="sync_restart")
            await asyncio.wait({response}, timeout=5)
            assert response.done()
        # The cut-short wait fenced its approval for the restart; the source worker settles it.
        failing = await bot.journal_principal().approvals.approval_continuation_for_source("$event")
        assert failing is not None
        assert failing.state == "failing"
        await dispatch_approval(runner.approvals, failing, target=_target())
        assert await bot.journal_principal().approvals.approval_continuation_for_source("$event") is None

    assert _events(seen) == [(EVENT_TURN_STARTED, None), (EVENT_TURN_ENDED, "cancelled")]


async def test_a_paused_reply_whose_requester_lost_access_ends_its_turn_failed(tmp_path: Path) -> None:
    """An approval that can no longer resume for its requester fails the paused turn."""
    seen: _Seen = []
    async with _approval_bot(tmp_path, requires_human=False) as bot:
        _record_turn_events(bot, seen)
        with patch(
            "mindroom.approval.continuation_runner.is_sender_allowed_for_entity_replies_in_room",
            return_value=False,
        ):
            await _respond(bot, resume=AsyncMock(return_value=CompletedApprovalRun("Never resumed.", {})))

    assert _events(seen) == [(EVENT_TURN_STARTED, None), (EVENT_TURN_PAUSED, None), (EVENT_TURN_ENDED, "failed")]


async def test_the_turn_events_are_built_in_and_plugins_cannot_add_turn_events() -> None:
    """Hooks on the reply's start and resume answer sooner than those on its pause and end."""
    assert set(_TURN_EVENTS) <= BUILTIN_EVENT_NAMES
    assert [validate_event_name(event_name) for event_name in _TURN_EVENTS] == list(_TURN_EVENTS)
    assert [default_timeout_ms_for_event(event_name) for event_name in _TURN_EVENTS] == [2000, 3000, 2000, 3000]
    with pytest.raises(ValueError, match="reserved namespace"):
        validate_event_name("turn:custom")


def _lifecycle(
    tmp_path: Path,
    seen: _Seen,
    *,
    shutdown: bool = False,
    hooks: list[object] | None = None,
) -> ResponseLifecycle:
    registry = HookRegistry.from_plugins([_plugin("turn", _recorders(seen) if hooks is None else hooks)])
    _config, service = _response_hook_service(tmp_path, registry)
    return _lifecycle_with(service, process_shutdown_requested=lambda: shutdown)


async def test_a_hook_scoped_to_agents_or_rooms_sees_only_their_turns(tmp_path: Path) -> None:
    """A turn event is in scope for the entity that answers and the room it answers in."""
    seen: list[str] = []

    def recorder(name: str, **scope: list[str]) -> object:
        @hook(EVENT_TURN_STARTED, name=name, **scope)
        async def record(_ctx: TurnLifecycleContext) -> None:
            seen.append(name)

        return record

    hooks = [
        recorder("this_agent", agents=["code"]),
        recorder("other_agent", agents=["other"]),
        recorder("this_room", rooms=["!room:localhost"]),
        recorder("other_room", rooms=["!other:localhost"]),
    ]

    await _lifecycle(tmp_path, [], hooks=hooks).start_turn(resumed=False)

    assert seen == ["this_agent", "this_room"]


async def _finalize(lifecycle: ResponseLifecycle, outcome: FinalDeliveryOutcome) -> None:
    await lifecycle.finalize(
        outcome,
        build_post_response_outcome=lambda _outcome: ResponseOutcome(),
        post_response_deps=lambda: PostResponseEffectsDeps(logger=get_logger("tests.turn_lifecycle")),
    )


@pytest.mark.parametrize(
    ("raised", "outcome"),
    [(asyncio.CancelledError(), "cancelled"), (RuntimeError("boom"), "failed")],
    ids=["cancelled", "failed"],
)
async def test_a_settlement_that_raises_before_finalizing_still_ends_its_turn(
    tmp_path: Path,
    raised: BaseException,
    outcome: str,
) -> None:
    """A settlement that raises before its finalization ends the turn by what it raised, and the error propagates."""
    seen: _Seen = []
    lifecycle = _lifecycle(tmp_path, seen)

    with pytest.raises(type(raised)):
        async with lifecycle.turn():
            raise raised

    assert _events(seen) == [(EVENT_TURN_ENDED, outcome)]


async def test_a_cancellation_during_finalization_ends_the_turn_once_as_finally_settled(tmp_path: Path) -> None:
    """A turn finalized twice, as a cancellation during finalization does, ends once with the second settlement."""
    seen: _Seen = []
    lifecycle = _lifecycle(tmp_path, seen)

    async def finalized_twice_then_cancelled() -> None:
        async with lifecycle.turn():
            await lifecycle.start_turn(resumed=True)
            await _finalize(lifecycle, FinalDeliveryOutcome(ending=Completed()))
            await _finalize(lifecycle, FinalDeliveryOutcome(ending=Cancelled("user_stop")))
            raise asyncio.CancelledError

    with pytest.raises(asyncio.CancelledError):
        await finalized_twice_then_cancelled()

    assert _events(seen) == [(EVENT_TURN_RESUMED, None), (EVENT_TURN_ENDED, "cancelled")]


@pytest.mark.parametrize(
    ("event_name", "ending"),
    [(EVENT_TURN_PAUSED, AwaitingApproval()), (EVENT_TURN_ENDED, Completed())],
    ids=["paused", "ended"],
)
async def test_a_cancelled_observer_of_a_pause_or_end_leaves_the_others_to_see_it(
    tmp_path: Path,
    event_name: str,
    ending: AwaitingApproval | Completed,
) -> None:
    """One cancelled observer of how a turn settled does not keep the next from seeing it, nor raise out of the turn."""
    seen: list[str] = []

    @hook(event_name, name="cancelled_observer")
    async def cancelled_observer(_ctx: TurnLifecycleContext) -> None:
        raise asyncio.CancelledError

    @hook(event_name, name="later_observer")
    async def later_observer(ctx: TurnLifecycleContext) -> None:
        seen.append(ctx.event_name)

    lifecycle = _lifecycle(tmp_path, [], hooks=[cancelled_observer, later_observer])
    async with lifecycle.turn():
        await _finalize(lifecycle, FinalDeliveryOutcome(ending=ending))

    assert seen == [event_name]


async def test_a_pause_at_process_shutdown_reports_nothing_more(tmp_path: Path) -> None:
    """Orderly shutdown stops a settling turn's pause and end events, as it stops its other best-effort hooks."""
    seen: _Seen = []
    lifecycle = _lifecycle(tmp_path, seen, shutdown=True)

    async with lifecycle.turn():
        await lifecycle.start_turn(resumed=False)
        await _finalize(lifecycle, FinalDeliveryOutcome(ending=AwaitingApproval()))

    assert _events(seen) == [(EVENT_TURN_STARTED, None)]
