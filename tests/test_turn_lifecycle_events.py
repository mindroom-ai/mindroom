"""A turn tells plugins when it starts, pauses for an approval, and resumes, and exactly once how it ended."""

from __future__ import annotations

import asyncio
from contextlib import asynccontextmanager
from dataclasses import replace
from typing import TYPE_CHECKING
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from mindroom.approval.tool_approval import shutdown_approval_runtime
from mindroom.background_tasks import wait_for_background_tasks
from mindroom.cancellation import request_task_cancel
from mindroom.config.main import Config
from mindroom.config.participation import ParticipationConfig
from mindroom.constants import ROUTER_AGENT_NAME
from mindroom.event_journal import DepartureSource
from mindroom.event_journal.approval_signals import EndedApproval
from mindroom.final_delivery import AwaitingApproval, Cancelled, Completed, FinalDeliveryOutcome, Owed
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
from mindroom.orchestrator import _MultiAgentOrchestrator
from mindroom.plugin_api import TurnOutcome
from mindroom.post_response_effects import ResponseOutcome
from mindroom.reply_lifecycle import ReplyState
from mindroom.response_turn import CompletedApprovalRun
from mindroom.tool_system.runtime_context import get_tool_runtime_context
from tests.approval_continuation_helpers import approval_continuation, dispatch_approval
from tests.bot_helpers import _hook_plugin
from tests.conftest import (
    bind_runtime_paths,
    patch_response_runner_module,
    publish_hook_plugin,
    runtime_paths_for,
    test_runtime_paths,
    unwrap_extracted_collaborator,
)
from tests.fakes.scripted_model import Await, ScriptStep, answer, calls, tool_call
from tests.journal_membership_helpers import admit_room_membership
from tests.response_runner_helpers import _noop_typing, _plain_request, _target
from tests.test_cancelled_response_hook import _lifecycle_with, _no_effects, _response_hook_service
from tests.test_reply_endings_characterization import (  # noqa: F401 - echo_tool is a pytest fixture
    _ECHO_TOOL,
    _explode,
    _require_echo_approval,
    _silent_schedule,
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
    _responding_in_place,
    _resume_until_cancelled,
    _run_approval_wakes,
)
from tests.test_reply_records_turns import _model_stream, _stop

if TYPE_CHECKING:
    from collections.abc import AsyncIterator
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


def _publish_hooks(bot: AgentBot, hooks: list[object]) -> None:
    """Publish a plugin whose hooks are ``hooks`` beside the bot's other plugins."""
    bot.orchestrator.hook_registry_state.registry = publish_hook_plugin(
        bot.config,
        bot.runtime_paths,
        "turn-events",
        hooks,
    ).hooks


def _record_turn_events(bot: AgentBot, seen: _Seen, events: tuple[str, ...] = _TURN_EVENTS) -> None:
    """Publish a plugin whose hooks record ``events`` into ``seen`` beside the bot's other plugins."""
    _publish_hooks(bot, _recorders(seen, events))


def _events(seen: _Seen) -> list[tuple[str, str | None]]:
    return [(event, outcome) for event, _turn, outcome in seen]


@asynccontextmanager
async def _approval_turns(tmp_path: Path, seen: _Seen, *, requires_human: bool) -> AsyncIterator[AgentBot]:
    """Start an approval bot whose turn events record into ``seen``; its paused turns end as the runtime ends them."""
    async with _approval_bot(tmp_path, requires_human=requires_human) as bot:
        _record_turn_events(bot, seen)
        # As the runtime does: the approval's end ends the paused turn, through the bot of its entity.
        stop = bot._journal_store.approval_signals.follow_ended_approvals(bot.end_paused_turn)
        try:
            yield bot
            assert await wait_for_background_tasks(timeout=5, owner=bot._runtime_view)
            # Every turn whose approval ended took its end: none stays open.
            assert unwrap_extracted_collaborator(bot._response_runner).locked._turn_ends == {}
        finally:
            stop()


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
    # A turn no approval owns keeps nothing for one to take.
    assert turn.runner.locked._turn_ends == {}


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


@pytest.mark.parametrize("streaming", [True, False], ids=["streamed", "whole"])
@pytest.mark.parametrize(
    ("decision", "outcome"),
    [
        pytest.param([Await(_explode)], "failed", id="check-fails"),
        pytest.param(answer('{"action": "stay_silent", "reason": "Nothing to add."}'), "completed", id="declined"),
    ],
)
async def test_a_participation_check_that_fails_ends_the_turn_failed_and_one_that_declines_completed(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    streaming: bool,
    decision: list[ScriptStep],
    outcome: str,
) -> None:
    """Neither shows a reply, but a check whose model call raised fails the turn, while a decision not to answer completes it."""
    turn = await _turn(tmp_path, monkeypatch, streaming=streaming, scripts={"default": [decision]})
    seen: _Seen = []
    _record_turn_events(turn.bot, seen, (*_TURN_EVENTS, EVENT_MESSAGE_CANCELLED))

    await turn.runner.generate_response(replace(_plain_request(_target()), participation=ParticipationConfig()))

    assert _events(seen) == [(EVENT_TURN_STARTED, None), (EVENT_TURN_ENDED, outcome)]
    assert turn.homeserver.writes == []


async def test_an_error_before_the_participation_decision_ends_the_turn_failed(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A delivery that raises before the participation check decided stays quiet, and fails the turn."""
    turn = await _turn(tmp_path, monkeypatch, streaming=False, scripts={"default": []})
    seen: _Seen = []
    _record_turn_events(turn.bot, seen, (*_TURN_EVENTS, EVENT_MESSAGE_CANCELLED))

    with patch.object(turn.runner, "_deliver_turn", new=AsyncMock(side_effect=RuntimeError("delivery broke"))):
        await turn.runner.generate_response(replace(_plain_request(_target()), participation=ParticipationConfig()))

    assert _events(seen) == [(EVENT_TURN_STARTED, None), (EVENT_TURN_ENDED, "failed")]
    assert turn.homeserver.writes == []


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

    _publish_hooks(turn.bot, [cancelling_start, *_recorders(seen, (EVENT_TURN_ENDED, EVENT_MESSAGE_CANCELLED))])

    with pytest.raises(asyncio.CancelledError):
        await turn.runner.generate_response(_plain_request(_target()))

    assert _events(seen) == [(EVENT_MESSAGE_CANCELLED, None), (EVENT_TURN_ENDED, "cancelled")]


async def test_a_process_shutdown_mid_turn_ends_it_cancelled(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """A turn a process shutdown cuts short while its agent's hooks still run ends cancelled; its replay is a new turn."""
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

    assert _events(seen) == [(EVENT_TURN_STARTED, None), (EVENT_TURN_ENDED, "cancelled")]


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
    async with _approval_turns(tmp_path, seen, requires_human=False) as bot:
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
    async with _approval_turns(tmp_path, seen, requires_human=False) as bot:
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
    """The failed resume leaves its turn's end to its approval, whose settlement ends the turn failed once."""
    seen: _Seen = []
    async with _approval_turns(tmp_path, seen, requires_human=False) as bot:
        await _respond(bot, resume=AsyncMock(side_effect=RuntimeError("tool exploded")))

    assert _events(seen) == [
        (EVENT_TURN_STARTED, None),
        (EVENT_TURN_PAUSED, None),
        (EVENT_TURN_RESUMED, None),
        (EVENT_TURN_ENDED, "failed"),
    ]


async def test_a_silent_schedule_s_resume_that_reports_nothing_ends_the_turn_completed(tmp_path: Path) -> None:
    """A silent schedule's resumed answer that reports nothing ends its turn completed, as its turn's own answer would."""
    seen: _Seen = []
    async with _approval_turns(tmp_path, seen, requires_human=False) as bot:
        silent = _silent_schedule(_plain_request(_target()))
        await _respond(bot, request=silent, resume=AsyncMock(return_value=CompletedApprovalRun("NO_REPLY", {})))

    assert _events(seen) == [
        (EVENT_TURN_STARTED, None),
        (EVENT_TURN_PAUSED, None),
        (EVENT_TURN_RESUMED, None),
        (EVENT_TURN_ENDED, "completed"),
    ]


async def test_a_stop_during_a_resume_ends_the_turn_cancelled_once(tmp_path: Path) -> None:
    """The stopped resume leaves its turn's end to its approval, whose settlement its Stop wakes ends it cancelled once."""
    seen: _Seen = []
    async with _approval_turns(tmp_path, seen, requires_human=False) as bot:
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
    async with _approval_turns(tmp_path, seen, requires_human=True) as bot:
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


async def test_a_run_approved_in_place_whose_error_became_its_reply_ends_the_turn_failed(tmp_path: Path) -> None:
    """The run's own failure, not its reply's completed state, says how a turn its approval ends ended."""
    seen: _Seen = []
    async with _approval_turns(tmp_path, seen, requires_human=False) as bot:

        async def approve_in_place_then_fail(*_args: object, **_kwargs: object) -> str:
            context = get_tool_runtime_context()
            assert context is not None
            assert context.cli_approval_handler is not None
            cli_call = {"kind": "agent_cli", "call_id": "inner", "parent_bash_call_id": "bash"}
            await context.cli_approval_handler(replace(_paused(), cli_call=cli_call))
            # The run records no completion, as a run whose error became its reply's text.
            return "Error: the model failed."

        with patch_response_runner_module(
            stream_agent_response=_model_stream(AsyncMock(side_effect=approve_in_place_then_fail)),
            should_use_streaming=AsyncMock(return_value=False),
            typing_indicator=_noop_typing,
        ):
            await unwrap_extracted_collaborator(bot._response_runner).generate_response(_plain_request(_target()))

    assert _events(seen) == [(EVENT_TURN_STARTED, None), (EVENT_TURN_ENDED, "failed")]


@pytest.mark.parametrize(
    ("settled", "reply_state", "outcome"),
    [
        (TurnOutcome.FAILED, ReplyState.COMPLETED, "failed"),
        (TurnOutcome.COMPLETED, ReplyState.FAILED, "failed"),
        (TurnOutcome.COMPLETED, ReplyState.GONE, "cancelled"),
        (None, ReplyState.COMPLETED, "completed"),
    ],
    ids=["error-completed-the-reply", "owed-answer-refused", "settled-then-departed", "never-settled-here"],
)
async def test_an_approval_ends_its_turn_as_its_reply_ended_unless_a_completed_reply_hides_how_its_run_ended(
    tmp_path: Path,
    settled: TurnOutcome | None,
    reply_state: ReplyState,
    outcome: str,
) -> None:
    """How the run settled here refines only a completed reply; any other reply state is how the approval ended."""
    seen: _Seen = []
    async with _approval_turns(tmp_path, seen, requires_human=True) as bot:
        runner = unwrap_extracted_collaborator(bot._response_runner)
        continuation = approval_continuation(turn_id="turn-1")
        if settled is not None:
            runner.locked._turn_ends["turn-1"] = settled
        await runner.approvals.end_paused_turn(EndedApproval(continuation, reply_state))

    assert seen == [(EVENT_TURN_ENDED, "turn-1", outcome)]


async def test_a_stopping_bot_waits_for_the_turn_end_an_ended_approval_started(tmp_path: Path) -> None:
    """The turn end an ended approval starts is the bot's own work, which its stop waits for before plugins stop."""
    started, release = asyncio.Event(), asyncio.Event()
    finished: list[str | None] = []

    @hook(EVENT_TURN_ENDED, name="slow_turn_end")
    async def slow_turn_end(ctx: TurnLifecycleContext) -> None:
        started.set()
        await release.wait()
        finished.append(ctx.turn_id)

    async with _approval_bot(tmp_path, requires_human=True) as bot:
        _publish_hooks(bot, [slow_turn_end])
        bot.end_paused_turn(EndedApproval(approval_continuation(turn_id="turn-1"), ReplyState.COMPLETED))
        await asyncio.wait_for(started.wait(), timeout=5)
        # As the bot's stop drains the work it owns.
        drained = asyncio.create_task(wait_for_background_tasks(timeout=5, owner=bot._runtime_view))
        await asyncio.sleep(0)
        assert not drained.done()
        release.set()
        assert await drained

    assert finished == ["turn-1"]


async def test_a_turn_cut_short_while_it_waits_in_place_for_approval_ends_once(tmp_path: Path) -> None:
    """A run cut short while it waits in place leaves its end to its approval, whose failed settlement ends it once."""
    seen: _Seen = []
    async with _approval_turns(tmp_path, seen, requires_human=True) as bot:
        runner = unwrap_extracted_collaborator(bot._response_runner)
        async with _responding_in_place(bot) as response:
            request_task_cancel(response, cancel_source="sync_restart")
            await asyncio.wait({response}, timeout=5)
            assert response.done()
        # The cut-short wait fenced its approval for the restart; the source worker settles it.
        failing = await bot.journal_principal().approvals.approval_continuation_for_source("$event")
        assert failing is not None
        assert failing.state == "failing"
        await dispatch_approval(runner.approvals, failing, target=_target())
        assert await bot.journal_principal().approvals.approval_continuation_for_source("$event") is None

    assert _events(seen) == [(EVENT_TURN_STARTED, None), (EVENT_TURN_ENDED, "failed")]


@pytest.mark.parametrize(("cancel_source", "outcome"), [("user_stop", "cancelled"), ("sync_restart", "failed")])
async def test_a_resume_cut_short_before_its_run_starts_ends_the_turn_once(
    tmp_path: Path,
    cancel_source: str,
    outcome: str,
) -> None:
    """A Stop or a restart that lands while a claimed resume prepares, before its run starts, ends the paused turn."""
    seen: _Seen = []
    preparing = asyncio.Event()

    async def prepare_until_cancelled(*_args: object, **_kwargs: object) -> None:
        # As an agent with mid-turn judging refreshes the thread before its resume runs.
        preparing.set()
        await asyncio.Event().wait()

    async with _approval_turns(tmp_path, seen, requires_human=False) as bot:
        runner = unwrap_extracted_collaborator(bot._response_runner)
        with patch.object(runner.locked, "refresh_mid_turn_context_for_approval", new=prepare_until_cancelled):
            response = asyncio.create_task(_respond(bot))
            await asyncio.wait_for(preparing.wait(), timeout=5)
            request_task_cancel(response, cancel_source=cancel_source)
            await asyncio.wait({response}, timeout=5)
        # The cut-short resume fenced its claimed approval; the source worker settles it.
        failing = await bot.journal_principal().approvals.approval_continuation_for_source("$event")
        assert failing is not None
        assert failing.state == "failing"
        await dispatch_approval(runner.approvals, failing, target=_target())

    assert _events(seen) == [(EVENT_TURN_STARTED, None), (EVENT_TURN_PAUSED, None), (EVENT_TURN_ENDED, outcome)]


async def test_a_resume_released_to_replay_at_shutdown_ends_no_turn(tmp_path: Path) -> None:
    """A resume a shutdown releases to replay ends nothing: its sources answer again on the next start."""
    seen: _Seen = []
    async with _approval_turns(tmp_path, seen, requires_human=False) as bot:
        resuming = asyncio.Event()
        response = asyncio.create_task(_respond(bot, resume=_resume_until_cancelled(resuming)))
        await asyncio.wait_for(resuming.wait(), timeout=5)
        request_task_cancel(response, process_shutdown=True)
        await asyncio.wait({response}, timeout=5)
        failing = await bot.journal_principal().approvals.approval_continuation_for_source("$event")
        assert failing is not None
        await dispatch_approval(
            unwrap_extracted_collaborator(bot._response_runner).approvals,
            failing,
            target=_target(),
        )
        assert await bot.journal_principal().approvals.approval_continuation_for_source("$event") is None
        assert await bot._reply_runtime.store.is_pending("$event")

    assert _events(seen) == [(EVENT_TURN_STARTED, None), (EVENT_TURN_PAUSED, None), (EVENT_TURN_RESUMED, None)]


async def test_a_paused_turn_ends_cancelled_when_its_agent_leaves_the_room(tmp_path: Path) -> None:
    """Leaving the room drops the approval a turn waits for, and with it ends the turn."""
    seen: _Seen = []
    async with _approval_turns(tmp_path, seen, requires_human=True) as bot:
        request = _plain_request(_target())
        await _respond(bot, request=request)
        await admit_room_membership(bot.journal_principal(), _target().room_id, "leave", source=DepartureSource.LOCAL)

    assert seen == [
        (EVENT_TURN_STARTED, request.turn_id, None),
        (EVENT_TURN_PAUSED, request.turn_id, None),
        (EVENT_TURN_ENDED, request.turn_id, "cancelled"),
    ]


async def test_leaving_the_room_while_a_resume_runs_ends_the_turn_cancelled_once(tmp_path: Path) -> None:
    """The departure drops the approval and cancels its running resume; the turn still ends once."""
    seen: _Seen = []
    async with _approval_turns(tmp_path, seen, requires_human=False) as bot:
        resuming = asyncio.Event()
        response = asyncio.create_task(_respond(bot, resume=_resume_until_cancelled(resuming)))
        await asyncio.wait_for(resuming.wait(), timeout=5)
        await admit_room_membership(bot.journal_principal(), _target().room_id, "leave", source=DepartureSource.LOCAL)
        await bot._reply_runtime.departed(_target().room_id)
        await asyncio.wait({response}, timeout=5)

    assert _events(seen) == [
        (EVENT_TURN_STARTED, None),
        (EVENT_TURN_PAUSED, None),
        (EVENT_TURN_RESUMED, None),
        (EVENT_TURN_ENDED, "cancelled"),
    ]


async def test_leaving_the_room_while_a_run_waits_in_place_for_approval_ends_the_turn_cancelled_once(
    tmp_path: Path,
) -> None:
    """The departure drops the approval a run waits for in place and cancels the run; the turn still ends once."""
    seen: _Seen = []
    async with _approval_turns(tmp_path, seen, requires_human=True) as bot, _responding_in_place(bot) as response:
        await admit_room_membership(bot.journal_principal(), _target().room_id, "leave", source=DepartureSource.LOCAL)
        await bot._reply_runtime.departed(_target().room_id)
        await asyncio.wait({response}, timeout=5)
        assert response.done()

    assert _events(seen) == [(EVENT_TURN_STARTED, None), (EVENT_TURN_ENDED, "cancelled")]


async def test_a_paused_reply_whose_requester_lost_access_ends_its_turn_failed(tmp_path: Path) -> None:
    """An approval that can no longer resume for its requester fails the paused turn."""
    seen: _Seen = []
    async with _approval_turns(tmp_path, seen, requires_human=False) as bot:
        with patch(
            "mindroom.approval.continuation_runner.is_sender_allowed_for_entity_replies_in_room",
            return_value=False,
        ):
            await _respond(bot, resume=AsyncMock(return_value=CompletedApprovalRun("Never resumed.", {})))

    assert _events(seen) == [(EVENT_TURN_STARTED, None), (EVENT_TURN_PAUSED, None), (EVENT_TURN_ENDED, "failed")]


async def test_a_paused_turn_ends_through_its_entity_s_bot_or_the_router_s_once_the_entity_is_gone(
    tmp_path: Path,
) -> None:
    """A removed agent has no bot, so the router ends its paused turns."""
    config = bind_runtime_paths(Config(), test_runtime_paths(tmp_path))
    orchestrator = _MultiAgentOrchestrator(runtime_paths_for(config), config=config)
    general, router = MagicMock(), MagicMock()
    orchestrator.fleet.add("general", general)
    orchestrator.fleet.add(ROUTER_AGENT_NAME, router)

    for entity_name in ("general", "removed"):
        orchestrator._end_paused_turn(EndedApproval(approval_continuation(entity_name=entity_name), None))

    general.end_paused_turn.assert_called_once()
    router.end_paused_turn.assert_called_once()
    assert router.end_paused_turn.call_args.args[0].continuation.entity_name == "removed"


async def test_the_turn_events_are_built_in_and_plugins_cannot_add_turn_events() -> None:
    """The turn events are built in, the `turn` namespace is reserved, and start and resume hooks time out sooner."""
    assert set(_TURN_EVENTS) <= BUILTIN_EVENT_NAMES
    assert [validate_event_name(event_name) for event_name in _TURN_EVENTS] == list(_TURN_EVENTS)
    assert [default_timeout_ms_for_event(event_name) for event_name in _TURN_EVENTS] == [2000, 3000, 2000, 3000]
    with pytest.raises(ValueError, match="reserved namespace"):
        validate_event_name("turn:custom")


def _lifecycle(tmp_path: Path, hooks: list[object], *, shutdown: bool = False) -> ResponseLifecycle:
    registry = HookRegistry.from_plugins([_hook_plugin("turn", hooks)])
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

    await _lifecycle(tmp_path, hooks).start_turn(resumed=False)

    assert seen == ["this_agent", "this_room"]


async def _no_approval(_end: TurnOutcome) -> bool:
    return False


async def _an_approval(_end: TurnOutcome) -> bool:
    return True


@pytest.mark.parametrize(
    ("ending", "events"),
    [(Completed(), []), (Owed(), []), (AwaitingApproval(), [(EVENT_TURN_PAUSED, None)])],
    ids=["completed", "owed", "paused"],
)
async def test_a_settlement_leaves_the_end_of_a_turn_an_approval_owns_to_that_approval(
    tmp_path: Path,
    ending: Completed | Owed | AwaitingApproval,
    events: list[tuple[str, str | None]],
) -> None:
    """A resumed run, or one that waited in place, reports a pause but never the end its approval's settlement emits."""
    seen: _Seen = []
    lifecycle = _lifecycle(tmp_path, _recorders(seen))

    async with lifecycle.turn(approval_owns_end=_an_approval):
        await _finalize(lifecycle, FinalDeliveryOutcome(ending=ending))

    assert _events(seen) == events


async def test_a_turn_no_plugin_follows_to_its_end_reads_no_approval_to_end_it(tmp_path: Path) -> None:
    """With no `turn:ended` hook, a settled turn skips the journal read that decides who owns its end."""
    approval_owns_end = AsyncMock(return_value=False)
    lifecycle = _lifecycle(tmp_path, [])

    async with lifecycle.turn(approval_owns_end=approval_owns_end):
        await _finalize(lifecycle, FinalDeliveryOutcome(ending=Completed()))

    approval_owns_end.assert_not_awaited()


async def _finalize(
    lifecycle: ResponseLifecycle,
    outcome: FinalDeliveryOutcome,
    post_response_outcome: ResponseOutcome | None = None,
) -> None:
    await lifecycle.finalize(
        outcome,
        build_post_response_outcome=lambda _outcome: post_response_outcome or ResponseOutcome(),
        post_response_deps=_no_effects,
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
    lifecycle = _lifecycle(tmp_path, _recorders(seen))

    with pytest.raises(type(raised)):
        async with lifecycle.turn(approval_owns_end=_no_approval):
            raise raised

    assert _events(seen) == [(EVENT_TURN_ENDED, outcome)]


async def test_a_cancellation_during_finalization_ends_the_turn_once_as_finally_settled(tmp_path: Path) -> None:
    """A turn finalized twice, as a cancellation during finalization does, ends once with the second settlement."""
    seen: _Seen = []
    lifecycle = _lifecycle(tmp_path, _recorders(seen))

    async def finalized_twice_then_cancelled() -> None:
        async with lifecycle.turn(approval_owns_end=_no_approval):
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

    lifecycle = _lifecycle(tmp_path, [cancelled_observer, later_observer])
    async with lifecycle.turn(approval_owns_end=_no_approval):
        await _finalize(lifecycle, FinalDeliveryOutcome(ending=ending))

    assert seen == [event_name]


@pytest.mark.parametrize(("ending", "outcome"), [(Completed(), "failed"), (Owed(), "completed")], ids=["shown", "owed"])
async def test_an_answer_the_outbox_owes_completes_its_turn_whatever_its_run_reports(
    tmp_path: Path,
    ending: Completed | Owed,
    outcome: str,
) -> None:
    """A shown answer that only reports its run's error fails the turn; one the outbox still owes completes it.

    A resumed reply's owed answer reports no successful run, so the run's report cannot tell an owed answer's outcome.
    """
    seen: _Seen = []
    lifecycle = _lifecycle(tmp_path, _recorders(seen))

    async with lifecycle.turn(approval_owns_end=_no_approval):
        await _finalize(lifecycle, FinalDeliveryOutcome(ending=ending), ResponseOutcome(run_succeeded=False))

    assert _events(seen) == [(EVENT_TURN_ENDED, outcome)]
    assert isinstance(seen[0][2], TurnOutcome)


async def test_a_turn_settling_as_mindroom_shuts_down_still_reports_how_it_settled(tmp_path: Path) -> None:
    """Shutdown stops a turn's other best-effort hooks, but not the pause or end that tells plugins where it stands."""
    seen: _Seen = []
    lifecycle = _lifecycle(tmp_path, _recorders(seen), shutdown=True)

    async with lifecycle.turn(approval_owns_end=_no_approval):
        await lifecycle.start_turn(resumed=False)
        await _finalize(lifecycle, FinalDeliveryOutcome(ending=AwaitingApproval()))

    assert _events(seen) == [(EVENT_TURN_STARTED, None), (EVENT_TURN_PAUSED, None)]
