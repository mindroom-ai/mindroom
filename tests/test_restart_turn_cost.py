"""Restart-interrupted user and job follow-up turns preserve their own and subagent cost in the final reply."""

from __future__ import annotations

import asyncio
import json
import sqlite3
from contextlib import suppress
from dataclasses import asdict, replace
from typing import TYPE_CHECKING, Any, ClassVar
from unittest.mock import AsyncMock

import pytest
from agno.tools import Toolkit

from mindroom import reply_lifecycle as rl
from mindroom.cancellation import request_task_cancel
from mindroom.config.agent import AgentConfig
from mindroom.config.main import Config
from mindroom.config.models import ModelConfig, ModelPricing
from mindroom.constants import (
    AI_RUN_METADATA_KEY,
    CONTINUED_RUNS_METADATA_KEY,
    MATRIX_RESPONSE_EVENT_ID_METADATA_KEY,
    SUBAGENT_COST_RUN_METADATA_KEY,
)
from mindroom.delegation.background import delegation_child
from mindroom.delegation.cost import (
    continued_subagent_cost_usd,
    receive_subagent_cost,
    received_subagent_cost_usd,
    receiving_subagent_cost,
    resumed_subagent_cost,
    subagent_cost_metadata,
)
from mindroom.delegation.state import DelegationChild
from mindroom.event_journal.models import EventClass, EventKind, InboundEvent
from mindroom.history.run_checkpoint import is_recovered_run
from mindroom.llm.runs import RunRecord, RunStatus
from mindroom.matrix.users import AgentMatrixUser
from mindroom.message_target import MessageTarget
from mindroom.orchestration import tool_job_runtime as tool_job_coordinator
from mindroom.providers.identity import model_identity
from mindroom.reply_presentation import Presentation, encode_presentation
from mindroom.reply_turn.request import ResponseRequest
from mindroom.response_sources import ResponseSources
from mindroom.response_turn import build_matrix_run_metadata
from mindroom.tool_jobs.conversation import ConversationKey
from mindroom.tool_jobs.follow_ups import follow_up_event
from mindroom.tool_jobs.instances import pin_background_tool_jobs
from mindroom.tool_jobs.runtime import BackgroundOutcome, register_background_runtime
from tests.access_schema_support import with_current_room_member_access
from tests.bot_helpers import make_test_agent_bot, unique_room_send_responses
from tests.conftest import (
    TEST_PASSWORD,
    bind_runtime_paths,
    install_model_providers,
    install_runtime_journal_support,
    make_matrix_client_mock,
    patch_response_runner_module,
    test_runtime_paths,
    unwrap_extracted_collaborator,
    wrap_extracted_collaborators,
)
from tests.fakes.scripted_model import ScriptedModel, answer, calls, tool_call, usage
from tests.response_attempt_helpers import install_direct_response_admission
from tests.response_runner_helpers import _envelope, _noop_typing, _target
from tests.scripted_agent_helpers import registered_toolkit
from tests.tool_job_helpers import (
    continue_stopped_jobs,
    job_owner,
    lookup,
    settle_stopped_subagent,
    start_job,
    tool_job_runtime,
    wait_for_status,
)

if TYPE_CHECKING:
    from collections.abc import Coroutine, Iterator
    from pathlib import Path

    from mindroom.bot import AgentBot
    from mindroom.response_runner import ResponseRunner
    from mindroom.tool_jobs.runtime import ToolJobRuntime

_COST_PROBE_TOOL = "restart_cost_probe"
# Priced at $10 / 1M input and $20 / 1M output -> 100k input + 10k output = $1.00 + $0.20 = $1.20 per request.
_REQ_USAGE = usage(100_000, 10_000)


class _CostProbeTools(Toolkit):
    """Test toolkit that charges subagent cost and can stall so a restart can cut the turn short."""

    stalled: ClassVar[asyncio.Event]

    def __init__(self) -> None:
        super().__init__(name=_COST_PROBE_TOOL, tools=[self.charge_subagent, self.stall])

    async def charge_subagent(self, usd: float) -> str:
        """Record ``usd`` of received subagent cost for this attempt."""
        receive_subagent_cost(usd)
        return f"charged {usd}"

    async def stall(self) -> str:
        """Block until the turn is cancelled by a simulated process shutdown."""
        self.stalled.set()
        await asyncio.Event().wait()
        raise AssertionError


@pytest.fixture(autouse=True)
def _register_cost_probe() -> Iterator[None]:
    _CostProbeTools.stalled = asyncio.Event()
    with registered_toolkit(_COST_PROBE_TOOL, _CostProbeTools):
        yield


def _scripted_model(bot: AgentBot, turns: list[Any]) -> ScriptedModel:
    return ScriptedModel(turns, identity=model_identity(bot.config.models["default"]))


def _priced_bot(tmp_path: Path, *, background_jobs: bool = False, with_worker: bool = False) -> AgentBot:
    agents: dict[str, AgentConfig] = {
        "general": AgentConfig(
            display_name="General",
            rooms=["!room:localhost"],
            tools=[_COST_PROBE_TOOL],
            delegate_to=["worker"] if with_worker else [],
        ),
    }
    models: dict[str, ModelConfig] = {
        "default": ModelConfig(
            provider="openai",
            id="priced-model",
            pricing=ModelPricing(input=10, output=20),
        ),
    }
    if with_worker:
        agents["worker"] = AgentConfig(display_name="Worker", model="worker_model", tools=[_COST_PROBE_TOOL])
        models["worker_model"] = ModelConfig(
            provider="openai",
            id="priced-worker",
            pricing=ModelPricing(input=10, output=20),
        )
    config = bind_runtime_paths(
        with_current_room_member_access(Config(agents=agents, teams={}, models=models, memory={"backend": "none"})),
        test_runtime_paths(tmp_path),
    )
    config.background_tool_jobs.enabled = background_jobs
    agent_user = AgentMatrixUser(
        agent_name="general",
        password=TEST_PASSWORD,
        display_name="General",
        user_id="@mindroom_general:localhost",
    )
    bot = make_test_agent_bot(agent_user, config, rooms=["!room:localhost"])
    install_direct_response_admission(bot)
    bot.client = make_matrix_client_mock(user_id="@mindroom_general:localhost")
    unique_room_send_responses(bot.client)
    install_runtime_journal_support(bot)
    wrap_extracted_collaborators(bot)
    return bot


async def _admit_user_message(bot: AgentBot, *, source_event_id: str, thread_id: str = "$thread") -> None:
    await bot.journal_principal().admit(
        InboundEvent(
            event_id=source_event_id,
            room_id="!room:localhost",
            thread_id=thread_id,
            kind=EventKind.MESSAGE,
            event_class=EventClass.ACTIONABLE,
            sender="@user:localhost",
            origin_server_ts=1,
            source={"content": {"msgtype": "m.text", "body": "do work"}},
        ),
    )


def _user_request(*, source_event_id: str, thread_id: str = "$thread") -> ResponseRequest:
    target = _target(thread_id=thread_id, reply_to_event_id=source_event_id)
    return ResponseRequest(
        thread_history=[],
        prompt="do work",
        user_id="@user:localhost",
        response_envelope=_envelope(target, source_event_id=source_event_id),
        sources=ResponseSources((source_event_id,), (source_event_id,)),
        matrix_run_metadata=build_matrix_run_metadata(source_event_id, []),
    )


async def _simulate_restart(bot: AgentBot, generation: str) -> None:
    """Advance to a new process generation and run startup reply takeover, as a real restart does."""
    bot._reply_runtime.generation = generation
    await bot._take_over_replies()


async def _run_until_stall_and_shutdown(coro: Coroutine[Any, Any, object]) -> None:
    """Run one turn until it reaches ``stall()``, then cancel it as a process shutdown."""
    _CostProbeTools.stalled = asyncio.Event()
    task = asyncio.create_task(coro)
    await asyncio.wait_for(_CostProbeTools.stalled.wait(), 10)
    request_task_cancel(task, process_shutdown=True)
    with suppress(asyncio.CancelledError):
        await task


def _final_ai_run_metadata(bot: AgentBot) -> tuple[str, dict[str, Any]]:
    """Return ``(event_id, io.mindroom.ai_run)`` from the last Matrix message edit/send carrying it."""
    for call in reversed(bot.client.room_send.await_args_list):
        content = call.kwargs.get("content") or call.args[2]
        new_content = content.get("m.new_content")
        for payload in (new_content, content):
            if isinstance(payload, dict) and AI_RUN_METADATA_KEY in payload:
                relates = content.get("m.relates_to", {})
                event_id = relates.get("event_id") or "$sent"
                return event_id, payload[AI_RUN_METADATA_KEY]
    pytest.fail("No Matrix event published io.mindroom.ai_run metadata")


def _recorded_usage_tokens(bot: AgentBot) -> list[tuple[str, int, int]]:
    """Return ``(run_id, input_tokens, output_tokens)`` rows recorded in the agent's usage ledger."""
    db_path = bot.runtime_paths.storage_root / "agents" / "general" / "sessions" / "general.db"
    with sqlite3.connect(db_path) as conn:
        rows = conn.execute("SELECT usage_data FROM general_sessions_usage ORDER BY rowid").fetchall()
    result = []
    for (raw,) in rows:
        data = json.loads(raw)
        metrics = data["metrics"]
        result.append((data["run_id"], metrics["input_tokens"], metrics["output_tokens"]))
    return result


def _stored_runs(runner: ResponseRunner, session_id: str) -> list[RunRecord]:
    storage = runner.deps.state_writer.create_storage(None)
    return [run for run in storage.list_runs(session_id) if isinstance(run, RunRecord)]


@pytest.mark.asyncio
async def test_restart_recovered_turn_includes_prior_attempt_own_and_subagent_cost(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A reply that continues a restart-interrupted attempt reports both attempts' own and subagent costs."""
    bot = _priced_bot(tmp_path)
    await bot._take_over_replies()
    runner = unwrap_extracted_collaborator(bot._response_runner)
    source = "$user-turn-1"
    await _admit_user_message(bot, source_event_id=source)
    request = _user_request(source_event_id=source)
    session_id = request.response_envelope.target.session_id

    model_attempt_1 = _scripted_model(
        bot,
        [
            calls(tool_call("charge_subagent", "c1", usd=0.5), metrics=_REQ_USAGE),
            calls(tool_call("stall", "s1"), metrics=_REQ_USAGE),
        ],
    )
    install_model_providers(monkeypatch, {"default": model_attempt_1})
    with patch_response_runner_module(should_use_streaming=AsyncMock(return_value=True), typing_indicator=_noop_typing):
        await _run_until_stall_and_shutdown(runner.generate_response(request))

    await _simulate_restart(bot, "gen-2")

    model_attempt_2 = _scripted_model(
        bot,
        [
            calls(tool_call("charge_subagent", "c2", usd=0.25), metrics=_REQ_USAGE),
            answer("Done.", metrics=_REQ_USAGE),
        ],
    )
    install_model_providers(monkeypatch, {"default": model_attempt_2})
    with patch_response_runner_module(should_use_streaming=AsyncMock(return_value=True), typing_indicator=_noop_typing):
        await runner.generate_response(request)

    _, ai_run = _final_ai_run_metadata(bot)
    # 4 requests * $1.20 = $4.80 own cost; $0.50 + $0.25 = $0.75 subagent cost.
    assert ai_run["cost"] == {"usd": "4.8", "subagents_usd": "0.75"}

    runs = _stored_runs(runner, session_id)
    assert len(runs) == 2
    run_1, run_2 = runs
    assert is_recovered_run(run_1)
    assert (run_1.metadata or {}).get(SUBAGENT_COST_RUN_METADATA_KEY) == pytest.approx(0.5)
    assert (run_2.metadata or {}).get(SUBAGENT_COST_RUN_METADATA_KEY) == pytest.approx(0.25)
    assert (run_2.metadata or {}).get(CONTINUED_RUNS_METADATA_KEY) == [run_1.run_id]
    reply_id_1 = (run_1.metadata or {}).get(MATRIX_RESPONSE_EVENT_ID_METADATA_KEY)
    reply_id_2 = (run_2.metadata or {}).get(MATRIX_RESPONSE_EVENT_ID_METADATA_KEY)
    assert reply_id_1 is not None
    assert reply_id_1 == reply_id_2

    # Usage ledger records each attempt's own tokens once — no double counting.
    ledger = _recorded_usage_tokens(bot)
    assert [(inp, out) for _, inp, out in ledger] == [(200_000, 20_000), (200_000, 20_000)]


@pytest.mark.asyncio
async def test_two_restarts_in_one_turn_sum_every_attempt_once_without_double_counting(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Two restarts in one turn count A, B, and C each once and keep stored per-run subagent costs disjoint."""
    bot = _priced_bot(tmp_path)
    await bot._take_over_replies()
    runner = unwrap_extracted_collaborator(bot._response_runner)
    source = "$user-turn-2"
    await _admit_user_message(bot, source_event_id=source)
    request = _user_request(source_event_id=source)
    session_id = request.response_envelope.target.session_id

    # Attempt A: 2 requests ($2.40) + $0.40 subagent cost -> restart #1.
    install_model_providers(
        monkeypatch,
        {
            "default": _scripted_model(
                bot,
                [
                    calls(tool_call("charge_subagent", "ca", usd=0.4), metrics=_REQ_USAGE),
                    calls(tool_call("stall", "sa"), metrics=_REQ_USAGE),
                ],
            ),
        },
    )
    with patch_response_runner_module(should_use_streaming=AsyncMock(return_value=True), typing_indicator=_noop_typing):
        await _run_until_stall_and_shutdown(runner.generate_response(request))
    await _simulate_restart(bot, "gen-2")

    # Attempt B: continues A, spends 2 requests ($2.40) + $0.30 subagent cost -> restart #2.
    install_model_providers(
        monkeypatch,
        {
            "default": _scripted_model(
                bot,
                [
                    calls(tool_call("charge_subagent", "cb", usd=0.3), metrics=_REQ_USAGE),
                    calls(tool_call("stall", "sb"), metrics=_REQ_USAGE),
                ],
            ),
        },
    )
    with patch_response_runner_module(should_use_streaming=AsyncMock(return_value=True), typing_indicator=_noop_typing):
        await _run_until_stall_and_shutdown(runner.generate_response(request))

    runs_after_b = _stored_runs(runner, session_id)
    assert len(runs_after_b) == 2
    # Run B must store ONLY its own received subagent cost (0.3), not A+B (0.7).
    assert (runs_after_b[0].metadata or {}).get(SUBAGENT_COST_RUN_METADATA_KEY) == pytest.approx(0.4)
    assert (runs_after_b[1].metadata or {}).get(SUBAGENT_COST_RUN_METADATA_KEY) == pytest.approx(0.3)

    await _simulate_restart(bot, "gen-3")

    # Attempt C: continues A and B, spends 2 requests ($2.40) + $0.10 subagent cost -> completes.
    install_model_providers(
        monkeypatch,
        {
            "default": _scripted_model(
                bot,
                [
                    calls(tool_call("charge_subagent", "cc", usd=0.1), metrics=_REQ_USAGE),
                    answer("Finally done.", metrics=_REQ_USAGE),
                ],
            ),
        },
    )
    with patch_response_runner_module(should_use_streaming=AsyncMock(return_value=True), typing_indicator=_noop_typing):
        await runner.generate_response(request)

    _, ai_run = _final_ai_run_metadata(bot)
    # 6 requests * $1.20 = $7.20 own cost; 0.4 + 0.3 + 0.1 = 0.8 subagent cost.
    assert ai_run["cost"] == {"usd": "7.2", "subagents_usd": "0.8"}

    runs = _stored_runs(runner, session_id)
    assert len(runs) == 3
    assert [(run.metadata or {}).get(SUBAGENT_COST_RUN_METADATA_KEY) for run in runs] == [
        pytest.approx(0.4),
        pytest.approx(0.3),
        pytest.approx(0.1),
    ]
    assert (runs[2].metadata or {}).get(CONTINUED_RUNS_METADATA_KEY) == [runs[0].run_id, runs[1].run_id]
    ledger = _recorded_usage_tokens(bot)
    assert [(inp, out) for _, inp, out in ledger] == [(200_000, 20_000)] * 3


@pytest.mark.asyncio
async def test_restart_with_only_prior_subagent_cost_publishes_it_and_keeps_continuing_run_disjoint(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """When only the interrupted attempt received subagent cost, the reply publishes it and the new run stores none."""
    bot = _priced_bot(tmp_path)
    await bot._take_over_replies()
    runner = unwrap_extracted_collaborator(bot._response_runner)
    source = "$user-turn-only-prior"
    await _admit_user_message(bot, source_event_id=source)
    request = _user_request(source_event_id=source)
    session_id = request.response_envelope.target.session_id

    install_model_providers(
        monkeypatch,
        {
            "default": _scripted_model(
                bot,
                [
                    calls(tool_call("charge_subagent", "c1", usd=1.25), metrics=_REQ_USAGE),
                    calls(tool_call("stall", "s1"), metrics=_REQ_USAGE),
                ],
            ),
        },
    )
    with patch_response_runner_module(should_use_streaming=AsyncMock(return_value=True), typing_indicator=_noop_typing):
        await _run_until_stall_and_shutdown(runner.generate_response(request))
    await _simulate_restart(bot, "gen-2")

    install_model_providers(monkeypatch, {"default": _scripted_model(bot, [answer("Done.", metrics=_REQ_USAGE)])})
    with patch_response_runner_module(should_use_streaming=AsyncMock(return_value=True), typing_indicator=_noop_typing):
        await runner.generate_response(request)

    _, ai_run = _final_ai_run_metadata(bot)
    assert ai_run["cost"] == {"usd": "3.6", "subagents_usd": "1.25"}
    runs = _stored_runs(runner, session_id)
    assert len(runs) == 2
    assert (runs[0].metadata or {}).get(SUBAGENT_COST_RUN_METADATA_KEY) == pytest.approx(1.25)
    assert SUBAGENT_COST_RUN_METADATA_KEY not in (runs[1].metadata or {})


@pytest.mark.asyncio
async def test_restart_with_lost_attempt_no_stored_row_publishes_remaining_cost(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A lost attempt with no stored checkpoint row still lets the replay complete and report its own cost."""
    bot = _priced_bot(tmp_path)
    await bot._take_over_replies()
    runner = unwrap_extracted_collaborator(bot._response_runner)
    source = "$user-turn-lost"
    await _admit_user_message(bot, source_event_id=source)
    request = _user_request(source_event_id=source)

    # First attempt stalls BEFORE any checkpoint (no prior tool call), so no stored row exists.
    install_model_providers(
        monkeypatch,
        {"default": ScriptedModel([calls(tool_call("stall", "s1"), metrics=_REQ_USAGE)])},
    )
    # Force an uncheckpointed loss: claim reply and mark owner lost directly without saving a run row.
    replies = bot.journal_principal().replies
    await replies.write_generation("gen-1")
    await replies.claim(
        rl.ClaimRequest(
            span_id="lost-span",
            delivery_id=source,
            sources=ResponseSources((source,), (source,)),
            bot_generation="gen-1",
            now_ns=10,
            new_reply_id=f"reply:{source}",
            entity_name="general",
            room_id="!room:localhost",
            thread_id="$thread",
            membership_epoch=0,
            empty_presentation=encode_presentation(Presentation()),
        ),
    )
    await _simulate_restart(bot, "gen-2")

    install_model_providers(
        monkeypatch,
        {
            "default": _scripted_model(
                bot,
                [
                    calls(tool_call("charge_subagent", "c2", usd=0.2), metrics=_REQ_USAGE),
                    answer("Done.", metrics=_REQ_USAGE),
                ],
            ),
        },
    )
    with patch_response_runner_module(should_use_streaming=AsyncMock(return_value=True), typing_indicator=_noop_typing):
        await runner.generate_response(request)

    _, ai_run = _final_ai_run_metadata(bot)
    assert ai_run["cost"] == {"usd": "2.4", "subagents_usd": "0.2"}


@pytest.mark.asyncio
async def test_restart_past_native_recovery_limit_still_counts_all_unlinked_attempts(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Past _NATIVE_RECOVERY_LIMIT (natively=False), prior unlinked attempts still contribute to reply cost."""
    bot = _priced_bot(tmp_path)
    await bot._take_over_replies()
    runner = unwrap_extracted_collaborator(bot._response_runner)
    source = "$user-turn-crash-loop"
    await _admit_user_message(bot, source_event_id=source)
    request = _user_request(source_event_id=source)
    session_id = request.response_envelope.target.session_id

    # 3 restarts (> _NATIVE_RECOVERY_LIMIT == 2): attempts A, B, C each cut short, then D finishes with natively=False.
    for idx, (sub_cost, next_gen) in enumerate([(0.3, "gen-2"), (0.2, "gen-3"), (0.1, "gen-4")], start=1):
        install_model_providers(
            monkeypatch,
            {
                "default": _scripted_model(
                    bot,
                    [
                        calls(tool_call("charge_subagent", f"c{idx}", usd=sub_cost), metrics=_REQ_USAGE),
                        calls(tool_call("stall", f"s{idx}"), metrics=_REQ_USAGE),
                    ],
                ),
            },
        )
        with patch_response_runner_module(
            should_use_streaming=AsyncMock(return_value=True),
            typing_indicator=_noop_typing,
        ):
            await _run_until_stall_and_shutdown(runner.generate_response(request))
        await _simulate_restart(bot, next_gen)

    # Attempt D runs past _NATIVE_RECOVERY_LIMIT (natively=False).
    install_model_providers(
        monkeypatch,
        {"default": _scripted_model(bot, [answer("Fresh after crash loop.", metrics=_REQ_USAGE)])},
    )
    with patch_response_runner_module(should_use_streaming=AsyncMock(return_value=True), typing_indicator=_noop_typing):
        await runner.generate_response(request)

    _, ai_run = _final_ai_run_metadata(bot)
    # 3 interrupted attempts * 2 reqs + 1 final req = 7 reqs * $1.20 = $8.40; subagents = 0.3 + 0.2 + 0.1 = $0.60.
    assert ai_run["cost"] == {"usd": "8.4", "subagents_usd": "0.6"}
    runs = _stored_runs(runner, session_id)
    assert len(runs) == 4
    # First two were recovered earlier then marked error on natively=False; third stayed cancelled; fourth completed.
    assert runs[0].status is RunStatus.error
    assert runs[1].status is RunStatus.error
    assert runs[2].status is RunStatus.cancelled
    assert runs[3].status is RunStatus.completed
    assert (runs[3].metadata or {}).get(CONTINUED_RUNS_METADATA_KEY) == [r.run_id for r in runs[:3]]


@pytest.mark.asyncio
async def test_restart_interrupted_job_follow_up_continues_natively_and_carries_both_attempts_cost(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A job follow-up cut short by a restart is continued natively by its replay with both attempts' costs."""
    bot = _priced_bot(tmp_path, background_jobs=True)
    await bot._take_over_replies()
    runner = unwrap_extracted_collaborator(bot._response_runner)
    key = ConversationKey(
        recipient="general",
        room_id="!room:localhost",
        thread_id="$thread",
        requester_id="@user:localhost",
        silent=False,
    )
    owner = replace(
        job_owner(),
        agent_name=key.recipient,
        requester_id=key.requester_id,
        room_id=key.room_id,
        thread_id=key.thread_id,
        resolved_thread_id=key.thread_id,
        session_id=MessageTarget.resolve(key.room_id, key.thread_id, None).session_id,
    )
    runtime: ToolJobRuntime = await tool_job_runtime(tmp_path)
    pin_background_tool_jobs(bot.config, bot.runtime_paths)
    register_background_runtime(bot.runtime_paths, runtime)

    follow_up_id = "job-follow-up:conv1:jobs1:1"
    try:
        # Seed a completed delegation job with cost_usd = 0.60 so the follow-up turn consumes it.
        child = DelegationChild(
            delegation_id="deleg-1",
            parent_tool_call_id="call-deleg-1",
            caller_agent_name="general",
            child_agent_name="worker",
            task="Research",
            session_id="child-session",
            run_id="child-run",
            model_name="default",
            requested_model_name="default",
            depth=1,
            execution_identity={},
            subagent_id="a" * 32,
            agent_mode="standard",
            persona=None,
            cost_usd=0.6,
        )

        async def _finish() -> BackgroundOutcome:
            return BackgroundOutcome("completed", "Research complete.")

        job = await start_job(
            runtime,
            "deleg-1",
            tool_name="worker",
            depth=0,
            owner=owner,
            kind="delegation",
            adapter={"child": asdict(child)},
            operation=_finish,
        )
        job_id = job.job_id
        await wait_for_status(runtime, job_id, "completed")

        await bot.journal_principal().admit(
            follow_up_event(key, follow_up_id, sender_id="@mindroom_general:localhost", now_ms=1),
        )
        event = await bot.journal_principal().load_event(follow_up_id)
        assert event is not None

        # Attempt 1: collects the completed delegation job ($0.60 subagent cost), then stalls -> restart!
        install_model_providers(
            monkeypatch,
            {
                "default": _scripted_model(
                    bot,
                    [
                        calls(tool_call("job", "collect-1", action="collect"), metrics=_REQ_USAGE),
                        calls(tool_call("stall", "s1"), metrics=_REQ_USAGE),
                    ],
                ),
            },
        )
        with patch_response_runner_module(
            should_use_streaming=AsyncMock(return_value=True),
            typing_indicator=_noop_typing,
        ):
            await _run_until_stall_and_shutdown(runner._run_job_follow_up(event))

        # The job was consumed by attempt 1 before the restart.
        assert await runtime.ended_jobs(key) == ()

        await _simulate_restart(bot, "gen-2")

        # Attempt 2: replays the follow-up; must natively continue attempt 1 and report both attempts' costs.
        model_attempt_2 = _scripted_model(bot, [answer("Reported research.", metrics=_REQ_USAGE)])
        install_model_providers(monkeypatch, {"default": model_attempt_2})
        with patch_response_runner_module(
            should_use_streaming=AsyncMock(return_value=True),
            typing_indicator=_noop_typing,
        ):
            await runner._run_job_follow_up(event)

        _, ai_run = _final_ai_run_metadata(bot)
        # 2 requests in attempt 1 + 1 request in attempt 2 = 3 * $1.20 = $3.60; subagent cost = $0.60.
        assert ai_run["cost"] == {"usd": "3.6", "subagents_usd": "0.6"}

        session_id = MessageTarget.resolve(key.room_id, key.thread_id, None).session_id
        runs = _stored_runs(runner, session_id)
        assert len(runs) == 2
        run_1, run_2 = runs
        assert is_recovered_run(run_1)
        assert run_1.status is RunStatus.completed
        assert (run_2.metadata or {}).get(CONTINUED_RUNS_METADATA_KEY) == [run_1.run_id]
        reply_id_1 = (run_1.metadata or {}).get(MATRIX_RESPONSE_EVENT_ID_METADATA_KEY)
        reply_id_2 = (run_2.metadata or {}).get(MATRIX_RESPONSE_EVENT_ID_METADATA_KEY)
        assert reply_id_1 is not None
        assert reply_id_1 == reply_id_2
        # Attempt 2 saw attempt 1's collected job result in history instead of redoing it.
        history_contents = [str(m.content) for m in model_attempt_2.requests[0].messages if m.from_history]
        assert any("Research complete." in text for text in history_contents)
    finally:
        await runtime.shutdown()


@pytest.mark.asyncio
async def test_restart_with_stored_then_lost_attempt_counts_stored_and_final(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A stored attempt followed by a lost attempt with no stored row still reports the stored and final attempts."""
    bot = _priced_bot(tmp_path)
    await bot._take_over_replies()
    runner = unwrap_extracted_collaborator(bot._response_runner)
    source = "$user-turn-stored-then-lost"
    await _admit_user_message(bot, source_event_id=source)
    request = _user_request(source_event_id=source)

    # Attempt A stores a checkpoint ($2.40 own, $0.50 subagent) -> restart #1.
    install_model_providers(
        monkeypatch,
        {
            "default": _scripted_model(
                bot,
                [
                    calls(tool_call("charge_subagent", "c1", usd=0.5), metrics=_REQ_USAGE),
                    calls(tool_call("stall", "s1"), metrics=_REQ_USAGE),
                ],
            ),
        },
    )
    with patch_response_runner_module(should_use_streaming=AsyncMock(return_value=True), typing_indicator=_noop_typing):
        await _run_until_stall_and_shutdown(runner.generate_response(request))
    await _simulate_restart(bot, "gen-2")

    # Attempt B claims the reply and dies before storing any run row -> restart #2.
    replies = bot.journal_principal().replies
    await replies.claim(
        rl.ClaimRequest(
            span_id="lost-second-attempt",
            delivery_id=source,
            sources=ResponseSources((source,), (source,)),
            bot_generation="gen-2",
            now_ns=200,
            new_reply_id=f"reply:{source}",
            entity_name="general",
            room_id="!room:localhost",
            thread_id="$thread",
            membership_epoch=0,
            empty_presentation=encode_presentation(Presentation()),
        ),
    )
    await _simulate_restart(bot, "gen-3")

    # Attempt C completes ($1.20 own, no new subagents).
    install_model_providers(
        monkeypatch,
        {"default": _scripted_model(bot, [answer("Recovered after lost middle attempt.", metrics=_REQ_USAGE)])},
    )
    with patch_response_runner_module(should_use_streaming=AsyncMock(return_value=True), typing_indicator=_noop_typing):
        await runner.generate_response(request)

    _, ai_run = _final_ai_run_metadata(bot)
    assert ai_run["cost"] == {"usd": "3.6", "subagents_usd": "0.5"}


def test_resumed_subagent_cost_with_continued_runs_keeps_per_run_metadata_disjoint() -> None:
    """An approval resume of a restart-continuing run reports A+B while storing only B on its row."""
    stored_runs_meta = {
        "run-a": {SUBAGENT_COST_RUN_METADATA_KEY: 0.4},
        "run-b": {
            SUBAGENT_COST_RUN_METADATA_KEY: 0.3,
            CONTINUED_RUNS_METADATA_KEY: ["run-a"],
        },
    }
    prior_usd = continued_subagent_cost_usd(stored_runs_meta["run-b"], stored_runs_meta.get)
    cost = resumed_subagent_cost(stored_runs_meta["run-b"], prior_usd=prior_usd)
    with receiving_subagent_cost(cost):
        receive_subagent_cost(0.2)
        # Reply publishes total (0.4 + 0.3 + 0.2 = 0.9), while stored metadata keeps only run-b's own (0.5).
        assert received_subagent_cost_usd() == pytest.approx(0.9)
        assert subagent_cost_metadata() == {SUBAGENT_COST_RUN_METADATA_KEY: pytest.approx(0.5)}


@pytest.mark.asyncio
async def test_restart_continued_subagent_job_carries_pre_restart_own_and_nested_cost_to_parent_reply(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A subagent job continued across restarts rolls all attempts' own and nested subagent costs into the parent reply."""
    bot = _priced_bot(tmp_path, background_jobs=True, with_worker=True)
    await bot._take_over_replies()
    runner = unwrap_extracted_collaborator(bot._response_runner)

    async def start_runtime() -> ToolJobRuntime:
        rt = await tool_job_runtime(
            tmp_path,
            grant=lambda _job: "allowed",
            cancel=settle_stopped_subagent(bot.config, bot.runtime_paths),
            continues=tool_job_coordinator._is_subagent_job,
        )
        pin_background_tool_jobs(bot.config, bot.runtime_paths)
        register_background_runtime(bot.runtime_paths, rt)
        return rt

    runtime = await start_runtime()
    try:
        worker_model = _scripted_model(
            bot,
            [
                # Attempt 1: $1.20 + $0.50 nested subagent + $1.20 -> stall (restart #1).
                calls(tool_call("charge_subagent", "w1", usd=0.5), metrics=_REQ_USAGE),
                calls(tool_call("stall", "ws1"), metrics=_REQ_USAGE),
                # Attempt 2 (continuation #1): $1.20 + $0.30 nested subagent + $1.20 -> stall (restart #2).
                calls(tool_call("charge_subagent", "w2", usd=0.3), metrics=_REQ_USAGE),
                calls(tool_call("stall", "ws2"), metrics=_REQ_USAGE),
                # Attempt 3 (continuation #2): $1.20 -> complete.
                answer("Worker finished.", metrics=_REQ_USAGE),
            ],
        )
        leader_model_1 = _scripted_model(
            bot,
            [
                calls(
                    tool_call("run_subagent", "deleg-1", agent_name="worker", task="Investigate", background_after=0),
                    metrics=_REQ_USAGE,
                ),
                answer("Started worker in background.", metrics=_REQ_USAGE),
            ],
        )
        worker_model = ScriptedModel(
            worker_model.turns,
            identity=model_identity(bot.config.models["worker_model"]),
        )
        install_model_providers(monkeypatch, {"default": leader_model_1, "worker_model": worker_model})

        source_1 = "$start-subagent"
        await _admit_user_message(bot, source_event_id=source_1)
        request_1 = _user_request(source_event_id=source_1)
        with patch_response_runner_module(
            should_use_streaming=AsyncMock(return_value=True),
            typing_indicator=_noop_typing,
        ):
            await runner.generate_response(request_1)

        # Wait for worker attempt 1 to stall, then restart #1.
        await asyncio.wait_for(_CostProbeTools.stalled.wait(), 10)
        _CostProbeTools.stalled = asyncio.Event()
        await runtime.shutdown()
        runtime = await start_runtime()
        await runtime.recover()
        tool_ctx = runner.deps.tool_runtime.build_turn_context(
            request_1.response_envelope.target,
            user_id="@user:localhost",
            active_model_name="default",
            source_envelope=request_1.response_envelope,
            turn_id=request_1.turn_id,
        )
        await continue_stopped_jobs(runtime, bot.config, bot.runtime_paths, tool_ctx)

        # Wait for worker attempt 2 (continuation #1) to stall, then restart #2.
        await asyncio.wait_for(_CostProbeTools.stalled.wait(), 10)
        _CostProbeTools.stalled = asyncio.Event()
        await runtime.shutdown()
        runtime = await start_runtime()
        await runtime.recover()
        tool_ctx = runner.deps.tool_runtime.build_turn_context(
            request_1.response_envelope.target,
            user_id="@user:localhost",
            active_model_name="default",
            source_envelope=request_1.response_envelope,
            turn_id=request_1.turn_id,
        )
        await continue_stopped_jobs(runtime, bot.config, bot.runtime_paths, tool_ctx)

        # Wait for worker attempt 3 (continuation #2) to complete.
        leader_owner = replace(
            job_owner(),
            agent_name="general",
            requester_id="@user:localhost",
            room_id="!room:localhost",
            thread_id="$thread",
            resolved_thread_id="$thread",
            session_id=request_1.response_envelope.target.session_id,
        )
        [job_info] = await runtime.list_jobs(owner=leader_owner, depth=0)
        await wait_for_status(runtime, job_info.job_id, "completed")
        done = await lookup(runtime, job_info.job_id, owner=leader_owner, depth=0)
        # 5 requests * $1.20 = $6.00 own + ($0.50 + $0.30) nested subagents = $6.80.
        assert delegation_child(done).cost_usd == pytest.approx(6.8)

        # Parent collects the finished job in its next reply and publishes the full $6.80 subagent cost.
        leader_model_2 = _scripted_model(
            bot,
            [
                calls(tool_call("job", "collect-worker", action="collect"), metrics=_REQ_USAGE),
                answer("Worker reported back.", metrics=_REQ_USAGE),
            ],
        )
        install_model_providers(monkeypatch, {"default": leader_model_2, "worker_model": worker_model})
        source_2 = "$collect-subagent"
        await _admit_user_message(bot, source_event_id=source_2)
        request_2 = _user_request(source_event_id=source_2)
        with patch_response_runner_module(
            should_use_streaming=AsyncMock(return_value=True),
            typing_indicator=_noop_typing,
        ):
            await runner.generate_response(request_2)

        _, ai_run = _final_ai_run_metadata(bot)
        assert ai_run["cost"] == {"usd": "2.4", "subagents_usd": "6.8"}
    finally:
        await runtime.shutdown()
