"""Restart-interrupted user and job follow-up turns preserve their own and subagent cost in the final reply."""

from __future__ import annotations

import asyncio
import json
import sqlite3
from contextlib import suppress
from dataclasses import asdict, replace
from typing import TYPE_CHECKING, Any
from unittest.mock import AsyncMock

import pytest

from mindroom import reply_lifecycle as rl
from mindroom.ai_run_metadata import merge_usage, prior_usage_by_run, with_prior_usage
from mindroom.approval import manager as approval_manager
from mindroom.cancellation import request_task_cancel
from mindroom.config.agent import AgentConfig
from mindroom.config.approval import ApprovalRuleConfig, ToolApprovalConfig
from mindroom.config.main import Config
from mindroom.config.models import ModelConfig, ModelPricing
from mindroom.constants import (
    AI_RUN_METADATA_KEY,
    CONTINUED_RUNS_METADATA_KEY,
    LEGACY_COST_ID,
    MATRIX_RESPONSE_EVENT_ID_METADATA_KEY,
    PRIOR_USAGE_BY_RUN_METADATA_KEY,
    PRIOR_USAGE_RUN_METADATA_KEY,
    SUBAGENT_COST_RUN_METADATA_KEY,
    SUBAGENT_RECEIPTS_RUN_METADATA_KEY,
)
from mindroom.delegation import hooks as delegation_hooks
from mindroom.delegation.background import ACKNOWLEDGED_BY, delegation_child, job_acknowledgers
from mindroom.delegation.cost import (
    SubagentCost,
    merge_by_key,
    receipts_metadata,
    receive_subagent_cost,
    received_subagent_cost_usd,
    receiving_subagent_cost,
    stored_receipts,
    subagent_cost_metadata,
)
from mindroom.delegation.state import CONTINUE_SUBAGENT, DelegationChild
from mindroom.event_journal import BackgroundApprovalDecision
from mindroom.event_journal.models import EventClass, EventKind, InboundEvent
from mindroom.history.run_checkpoint import is_recovered_run, recover_interrupted_turn
from mindroom.history.storage import new_scope_session
from mindroom.history.types import HistoryScope
from mindroom.llm.messages import Message
from mindroom.llm.runs import RunRecord, RunStatus
from mindroom.llm.usage import RunMetrics, TokenCounts
from mindroom.matrix.users import AgentMatrixUser
from mindroom.message_target import MessageTarget
from mindroom.orchestration import tool_job_runtime as tool_job_coordinator
from mindroom.providers.identity import model_identity
from mindroom.reply_presentation import Presentation, encode_presentation
from mindroom.reply_turn.request import ResponseRequest
from mindroom.response_sources import ResponseSources
from mindroom.response_turn import build_matrix_run_metadata
from mindroom.session_storage.agent_storage import create_session_storage
from mindroom.session_storage.schema import quote_identifier
from mindroom.tool_jobs.conversation import ConversationKey
from mindroom.tool_jobs.follow_ups import follow_up_event
from mindroom.tool_jobs.instances import pin_background_tool_jobs
from mindroom.tool_jobs.runtime import BackgroundJob, BackgroundOutcome, register_background_runtime
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
from tests.delegation_helpers import CostProbeTools, replay_run_metadata
from tests.fakes.scripted_model import Await, ScriptedModel, answer, calls, tool_call, usage
from tests.response_attempt_helpers import install_direct_response_admission
from tests.response_runner_helpers import _envelope, _noop_typing, _target
from tests.scripted_agent_helpers import registered_toolkit
from tests.tool_job_helpers import (
    JobApprovalCards,
    continue_stopped_jobs,
    job_owner,
    lookup,
    settle_stopped_subagent,
    start_job,
    tool_job_runtime,
    wait_for_status,
    write_saved_job,
)

if TYPE_CHECKING:
    from collections.abc import Awaitable, Callable, Coroutine, Iterator
    from pathlib import Path

    from mindroom.bot import AgentBot
    from mindroom.response_runner import ResponseRunner
    from mindroom.tool_jobs.runtime import ToolJobRuntime
    from mindroom.tool_system.worker_routing import ToolExecutionIdentity

_COST_PROBE_TOOL = "restart_cost_probe"
_SESSION = "stored-session"
_SCOPE = HistoryScope(kind="agent", scope_id="general")
_TURN = "$stored-turn"
# Priced at $10 / 1M input and $20 / 1M output -> 100k input + 10k output = $1.00 + $0.20 = $1.20 per request.
_REQ_USAGE = usage(100_000, 10_000)


@pytest.fixture(autouse=True)
def _register_cost_probe() -> Iterator[None]:
    CostProbeTools.stalled = asyncio.Event()
    with registered_toolkit(_COST_PROBE_TOOL, CostProbeTools):
        yield


def _scripted_model(bot: AgentBot, turns: list[Any]) -> ScriptedModel:
    return ScriptedModel(turns, identity=model_identity(bot.config.models["default"]))


def _priced_bot(
    tmp_path: Path,
    *,
    background_jobs: bool = False,
    with_worker: bool = False,
    extra_tools: tuple[str, ...] = (),
    worker_tools: tuple[str, ...] = (),
) -> AgentBot:
    agents: dict[str, AgentConfig] = {
        "general": AgentConfig(
            display_name="General",
            rooms=["!room:localhost"],
            tools=[_COST_PROBE_TOOL, *extra_tools],
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
        agents["worker"] = AgentConfig(
            display_name="Worker",
            model="worker_model",
            tools=[_COST_PROBE_TOOL, *worker_tools],
        )
        models["worker_model"] = ModelConfig(
            provider="openai",
            id="priced-worker",
            pricing=ModelPricing(input=10, output=20),
        )
    config = bind_runtime_paths(
        with_current_room_member_access(
            Config(
                agents=agents,
                teams={},
                models=models,
                memory={"backend": "none"},
                tool_approval=ToolApprovalConfig(
                    rules=[ApprovalRuleConfig(match="checked", action="require_approval")],
                ),
            ),
        ),
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
    CostProbeTools.stalled = asyncio.Event()
    task = asyncio.create_task(coro)
    await asyncio.wait_for(CostProbeTools.stalled.wait(), 10)
    request_task_cancel(task, process_shutdown=True)
    with suppress(asyncio.CancelledError):
        await task


def _charge(call_id: str, usd: float) -> list[Any]:
    """Return a priced request whose call receives a subagent result that cost ``usd``."""
    return calls(tool_call("charge_subagent", call_id, usd=usd), metrics=_REQ_USAGE)


def _stall(call_id: str) -> list[Any]:
    """Return a priced request whose call waits for the shutdown that cuts its attempt short."""
    return calls(tool_call("stall", call_id), metrics=_REQ_USAGE)


def _collect(call_id: str) -> list[Any]:
    """Return a priced request whose call collects the conversation's ended jobs."""
    return calls(tool_call("job", call_id, action="collect"), metrics=_REQ_USAGE)


async def _run_turn(
    monkeypatch: pytest.MonkeyPatch,
    bot: AgentBot,
    response: Coroutine[Any, Any, object],
    *turns: Any,  # noqa: ANN401
    cut_short: bool = False,
    streamed: bool = True,
) -> ScriptedModel:
    """Run ``response`` on a model scripted with ``turns``, to its answer or, ``cut_short``, a shutdown at ``stall()``."""
    model = _scripted_model(bot, list(turns))
    install_model_providers(monkeypatch, {"default": model})
    with patch_response_runner_module(
        should_use_streaming=AsyncMock(return_value=streamed),
        typing_indicator=_noop_typing,
    ):
        await (_run_until_stall_and_shutdown(response) if cut_short else response)
    return model


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


def _receipts(run: RunRecord) -> dict[str, float]:
    """Return the subagent receipts ``run``'s row stores by delegation id."""
    return (run.metadata or {}).get(SUBAGENT_RECEIPTS_RUN_METADATA_KEY, {})


def _stored_sums(runs: list[RunRecord]) -> list[object]:
    """Return the summed subagent cost each run's row stores, None for a row that stores none."""
    return [(run.metadata or {}).get(SUBAGENT_COST_RUN_METADATA_KEY) for run in runs]


@pytest.mark.asyncio
@pytest.mark.parametrize("streamed", [True, False], ids=["streamed replay", "replay delivered whole"])
async def test_restart_recovered_turn_includes_prior_attempt_own_and_subagent_cost(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    streamed: bool,
) -> None:
    """A reply that continues a restart-interrupted attempt reports both attempts' own and subagent costs."""
    bot = _priced_bot(tmp_path)
    await bot._take_over_replies()
    runner = unwrap_extracted_collaborator(bot._response_runner)
    source = "$user-turn-1"
    await _admit_user_message(bot, source_event_id=source)
    request = _user_request(source_event_id=source)
    session_id = request.response_envelope.target.session_id

    attempt = runner.generate_response(request)
    await _run_turn(monkeypatch, bot, attempt, _charge("c1", 0.5), _stall("s1"), cut_short=True)
    await _simulate_restart(bot, "gen-2")
    replay = (_charge("c2", 0.25), answer("Done.", metrics=_REQ_USAGE))
    await _run_turn(monkeypatch, bot, runner.generate_response(request), *replay, streamed=streamed)

    _, ai_run = _final_ai_run_metadata(bot)
    # 4 requests * $1.20 = $4.80 own cost; $0.50 + $0.25 = $0.75 subagent cost.
    assert ai_run["cost"] == {"usd": "4.8", "subagents_usd": "0.75"}

    runs = _stored_runs(runner, session_id)
    assert len(runs) == 2
    run_1, run_2 = runs
    assert is_recovered_run(run_1)
    # Each row holds what the turn received so far by delegation id, with the sum earlier releases read.
    assert _stored_sums([run_1, run_2]) == [pytest.approx(0.5), pytest.approx(0.75)]
    assert [len(_receipts(run)) for run in (run_1, run_2)] == [1, 2]
    assert _receipts(run_1).items() <= _receipts(run_2).items()
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
    """Two restarts in one turn count A, B, and C each once, whatever each row repeats of the rows before it."""
    bot = _priced_bot(tmp_path)
    await bot._take_over_replies()
    runner = unwrap_extracted_collaborator(bot._response_runner)
    source = "$user-turn-2"
    await _admit_user_message(bot, source_event_id=source)
    request = _user_request(source_event_id=source)
    session_id = request.response_envelope.target.session_id

    # Attempt A: 2 requests ($2.40) + $0.40 subagent cost -> restart #1.
    attempt = runner.generate_response(request)
    await _run_turn(monkeypatch, bot, attempt, _charge("ca", 0.4), _stall("sa"), cut_short=True)
    await _simulate_restart(bot, "gen-2")

    # Attempt B: continues A, spends 2 requests ($2.40) + $0.30 subagent cost -> restart #2.
    attempt = runner.generate_response(request)
    await _run_turn(monkeypatch, bot, attempt, _charge("cb", 0.3), _stall("sb"), cut_short=True)

    runs_after_b = _stored_runs(runner, session_id)
    assert len(runs_after_b) == 2
    # Run B's row repeats A's receipt under the same id beside its own, so merging the two rows counts it once.
    assert _stored_sums(runs_after_b) == [pytest.approx(0.4), pytest.approx(0.7)]
    assert _receipts(runs_after_b[0]).items() <= _receipts(runs_after_b[1]).items()

    await _simulate_restart(bot, "gen-3")

    # Attempt C: continues A and B, spends 2 requests ($2.40) + $0.10 subagent cost -> completes.
    final = (_charge("cc", 0.1), answer("Finally done.", metrics=_REQ_USAGE))
    await _run_turn(monkeypatch, bot, runner.generate_response(request), *final)

    _, ai_run = _final_ai_run_metadata(bot)
    # 6 requests * $1.20 = $7.20 own cost; 0.4 + 0.3 + 0.1 = 0.8 subagent cost.
    assert ai_run["cost"] == {"usd": "7.2", "subagents_usd": "0.8"}

    runs = _stored_runs(runner, session_id)
    assert len(runs) == 3
    assert _stored_sums(runs) == [pytest.approx(0.4), pytest.approx(0.7), pytest.approx(0.8)]
    # C's row names A and B by run id, so a third restart would count each attempt once more.
    assert list((runs[2].metadata or {})[PRIOR_USAGE_BY_RUN_METADATA_KEY]) == [runs[0].run_id, runs[1].run_id]
    assert (runs[2].metadata or {}).get(CONTINUED_RUNS_METADATA_KEY) == [runs[0].run_id, runs[1].run_id]
    ledger = _recorded_usage_tokens(bot)
    assert [(inp, out) for _, inp, out in ledger] == [(200_000, 20_000)] * 3


@pytest.mark.asyncio
async def test_restart_with_only_prior_subagent_cost_publishes_it_from_the_continuing_run(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """When only the interrupted attempt received subagent cost, the reply publishes it and the new run carries it."""
    bot = _priced_bot(tmp_path)
    await bot._take_over_replies()
    runner = unwrap_extracted_collaborator(bot._response_runner)
    source = "$user-turn-only-prior"
    await _admit_user_message(bot, source_event_id=source)
    request = _user_request(source_event_id=source)
    session_id = request.response_envelope.target.session_id

    attempt = runner.generate_response(request)
    await _run_turn(monkeypatch, bot, attempt, _charge("c1", 1.25), _stall("s1"), cut_short=True)
    await _simulate_restart(bot, "gen-2")
    await _run_turn(monkeypatch, bot, runner.generate_response(request), answer("Done.", metrics=_REQ_USAGE))

    _, ai_run = _final_ai_run_metadata(bot)
    assert ai_run["cost"] == {"usd": "3.6", "subagents_usd": "1.25"}
    runs = _stored_runs(runner, session_id)
    assert len(runs) == 2
    assert _stored_sums(runs) == [pytest.approx(1.25), pytest.approx(1.25)]
    assert _receipts(runs[0]) == _receipts(runs[1])


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

    replay = (_charge("c2", 0.2), answer("Done.", metrics=_REQ_USAGE))
    await _run_turn(monkeypatch, bot, runner.generate_response(request), *replay)

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
        attempt = runner.generate_response(request)
        await _run_turn(monkeypatch, bot, attempt, _charge(f"c{idx}", sub_cost), _stall(f"s{idx}"), cut_short=True)
        await _simulate_restart(bot, next_gen)

    # Attempt D runs past _NATIVE_RECOVERY_LIMIT (natively=False).
    final = answer("Fresh after crash loop.", metrics=_REQ_USAGE)
    await _run_turn(monkeypatch, bot, runner.generate_response(request), final)

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


def _job_conversation() -> tuple[ConversationKey, ToolExecutionIdentity]:
    """Return the conversation the bot answers in ``$thread`` and the identity its jobs there are owned by."""
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
    return key, owner


async def _completed_research_job(
    runtime: ToolJobRuntime,
    owner: ToolExecutionIdentity,
    delegation_id: str = "deleg-1",
) -> str:
    """Start a subagent job of ``owner`` that completes at once with a child that cost $0.60; return its id."""
    child = DelegationChild(
        delegation_id=delegation_id,
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

    async def finish() -> BackgroundOutcome:
        return BackgroundOutcome("completed", "Research complete.")

    job = await start_job(
        runtime,
        delegation_id,
        tool_name="worker",
        depth=0,
        owner=owner,
        kind="delegation",
        adapter={"child": asdict(child)},
        operation=finish,
    )
    await wait_for_status(runtime, job.job_id, "completed")
    return job.job_id


_FOLLOW_UP_ID = "job-follow-up:conv1:jobs1:1"


def _serve_jobs(bot: AgentBot, runtime: ToolJobRuntime | None) -> None:
    """Start an instance of ``bot``'s storage root that runs background jobs on ``runtime``, or runs none."""
    bot.config.background_tool_jobs.enabled = runtime is not None
    pin_background_tool_jobs(bot.config, bot.runtime_paths)
    if runtime is not None:
        register_background_runtime(bot.runtime_paths, runtime)


async def _run_job_turn(
    monkeypatch: pytest.MonkeyPatch,
    bot: AgentBot,
    turn: str,
    *turns: Any,  # noqa: ANN401
    cut_short: bool = False,
) -> dict[str, Any]:
    """Run the user's turn or the job follow-up on a model scripted with ``turns``; return what its reply reports."""
    runner = unwrap_extracted_collaborator(bot._response_runner)
    if turn == "user turn":
        response = runner.generate_response(_user_request(source_event_id="$asks"))
    else:
        event = await bot.journal_principal().load_event(_FOLLOW_UP_ID)
        assert event is not None
        response = runner._run_job_follow_up(event)
    await _run_turn(monkeypatch, bot, response, *turns, cut_short=cut_short)
    return {} if cut_short else _final_ai_run_metadata(bot)[1]


async def _collect_a_job_then_restart(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    crashed: str,
    *,
    acknowledged: bool = False,
) -> tuple[AgentBot, ToolJobRuntime, ConversationKey, str]:
    """Let the ``crashed`` turn collect a completed $0.60 subagent job and stall, then restart; the job runtime stays.

    Unless ``acknowledged``, the crash comes between the checkpoint that saved the job's result and its acknowledgement.
    """
    bot = _priced_bot(tmp_path, background_jobs=True)
    await bot._take_over_replies()
    key, owner = _job_conversation()
    # The jobs are saved in the journal the bot's instance opens, as the coordinator's runtime saves them.
    runtime: ToolJobRuntime = await tool_job_runtime(bot.runtime_paths.storage_root)
    _serve_jobs(bot, runtime)
    try:
        job_id = await _completed_research_job(runtime, owner)
        await _admit_user_message(bot, source_event_id="$asks")
        await bot.journal_principal().admit(
            follow_up_event(key, _FOLLOW_UP_ID, sender_id="@mindroom_general:localhost", now_ms=2),
        )
        acknowledge = runtime.acknowledge_waits
        if not acknowledged:
            # Fail the write rather than returning () so mid-turn checkpoint ack retains the claim for finalize() to release.
            monkeypatch.setattr(
                runtime,
                "acknowledge_waits",
                AsyncMock(side_effect=OSError("crashed before acknowledgement")),
            )
        await _run_job_turn(monkeypatch, bot, crashed, _collect("collect-1"), _stall("s1"), cut_short=True)
        assert [ended.job_id for ended in await runtime.ended_jobs(key)] == ([] if acknowledged else [job_id])
        monkeypatch.setattr(runtime, "acknowledge_waits", acknowledge)
        await _simulate_restart(bot, "gen-2")
    except BaseException:
        await runtime.shutdown()
        raise
    return bot, runtime, key, job_id


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("acknowledged", "collects_again", "expected_cost"),
    [
        # 2 requests in attempt 1 + 1 in attempt 2 = 3 * $1.20; the job's $0.60 reaches the replay's reply.
        (True, False, {"usd": "3.6", "subagents_usd": "0.6"}),
        # Sol fresh 1: the crash left the job unacknowledged and the replay collects it again: $0.60 once, not twice.
        (False, True, {"usd": "4.8", "subagents_usd": "0.6"}),
        # Opus B1: the replay does not collect it again, so the job stays for the turn that will, which receives it.
        (False, False, {"usd": "3.6"}),
    ],
    ids=["acknowledged", "unacknowledged and collected again", "unacknowledged and left for a later turn"],
)
async def test_restart_interrupted_job_follow_up_continues_natively_and_carries_both_attempts_cost(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    acknowledged: bool,
    collects_again: bool,
    expected_cost: dict[str, str],
) -> None:
    """A job follow-up cut short by a restart is continued natively by its replay with both attempts' costs.

    The cut-short attempt's row holds the job's cost. Its replay carries it only once a turn acknowledged the job: a
    job a crash left unacknowledged is still collected by some turn, this replay or a later one, which receives it.
    """
    # Attempt 1 collects the completed job ($0.60) and stalls; it acknowledged the job unless the crash came first.
    bot, runtime, key, job_id = await _collect_a_job_then_restart(
        tmp_path,
        monkeypatch,
        "job follow-up",
        acknowledged=acknowledged,
    )
    runner = unwrap_extracted_collaborator(bot._response_runner)
    try:
        event = await bot.journal_principal().load_event(_FOLLOW_UP_ID)
        assert event is not None
        # Attempt 2: replays the follow-up; must natively continue attempt 1 and report both attempts' costs.
        replay = (
            *([_collect("collect-2")] if collects_again else []),
            answer("Reported research.", metrics=_REQ_USAGE),
        )
        model_attempt_2 = await _run_turn(monkeypatch, bot, runner._run_job_follow_up(event), *replay)

        _, ai_run = _final_ai_run_metadata(bot)
        assert ai_run["cost"] == expected_cost
        # A job no turn acknowledged is still there for the turn that collects it.
        left = [] if acknowledged or collects_again else [job_id]
        assert [ended.job_id for ended in await runtime.ended_jobs(key)] == left

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
@pytest.mark.parametrize("crashed", ["user turn", "job follow-up"])
async def test_a_job_another_turn_acknowledged_is_reported_by_that_turn_alone(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    crashed: str,
) -> None:
    """A turn collects a subagent job and crashes before acknowledging it; another turn collects it before the replay.

    The other turn's run acknowledges the job, so that turn's reply reports its cost. The replay's cut-short row still
    holds the receipt, which it leaves out: the thread shows the job's $0.60 once.
    """
    bot, runtime, key, _ = await _collect_a_job_then_restart(tmp_path, monkeypatch, crashed)
    other = "job follow-up" if crashed == "user turn" else "user turn"
    try:
        # The other turn gets the conversation first: it collects the job, acknowledges it, and reports it.
        collects = (_collect("collect-2"), answer("Reported research.", metrics=_REQ_USAGE))
        reported = await _run_job_turn(monkeypatch, bot, other, *collects)
        assert reported["cost"] == {"usd": "2.4", "subagents_usd": "0.6"}
        assert await runtime.ended_jobs(key) == ()

        # The replay reads the result in its recovered history and reports its three requests alone.
        replayed = await _run_job_turn(monkeypatch, bot, crashed, answer("Done.", metrics=_REQ_USAGE))
        assert replayed["cost"] == {"usd": "3.6"}
    finally:
        await runtime.shutdown()


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("crashed", "acknowledged"),
    [("user turn", False), ("job follow-up", False), ("user turn", True)],
    ids=["user turn left it", "job follow-up left it", "user turn acknowledged it"],
)
async def test_a_replay_on_an_instance_that_runs_no_jobs_reads_who_acknowledged_the_saved_ones(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    crashed: str,
    acknowledged: bool,
) -> None:
    """A restart with background jobs turned off keeps the saved jobs, so its replay still tells whose each is.

    The replay carries the job its cut-short run acknowledged and leaves out the one a crash left for a later turn,
    which collects it once jobs run again: the thread shows the job's $0.60 once either way.
    """
    bot, runtime, key, job_id = await _collect_a_job_then_restart(
        tmp_path,
        monkeypatch,
        crashed,
        acknowledged=acknowledged,
    )
    other = "job follow-up" if crashed == "user turn" else "user turn"
    try:
        await runtime.shutdown()
        _serve_jobs(bot, None)
        replayed = await _run_job_turn(monkeypatch, bot, crashed, answer("Done.", metrics=_REQ_USAGE))
        assert replayed["cost"] == ({"usd": "3.6", "subagents_usd": "0.6"} if acknowledged else {"usd": "3.6"})

        # The next start runs jobs again and finds the unacknowledged one, which the turn that collects it reports.
        runtime = await tool_job_runtime(bot.runtime_paths.storage_root)
        await runtime.recover()
        _serve_jobs(bot, runtime)
        assert (job_id in {ended.job_id for ended in await runtime.ended_jobs(key)}) is not acknowledged
        if not acknowledged:
            collects = (_collect("collect-2"), answer("Reported research.", metrics=_REQ_USAGE))
            reported = await _run_job_turn(monkeypatch, bot, other, *collects)
            assert reported["cost"] == {"usd": "2.4", "subagents_usd": "0.6"}
    finally:
        await runtime.shutdown()


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("same_thread", "child"),
    [
        (False, {"written_by": "a release this one cannot read"}),
        (True, {"written_by": "a release this one cannot read"}),
        (True, {"delegation_id": "another-requester-s", "field_of_a_later_release": 1}),
    ],
    ids=["another conversation", "another requester in the thread", "another requester's, with its id"],
)
async def test_a_saved_child_no_reader_decodes_does_not_stop_another_turn_s_replay(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    same_thread: bool,
    child: dict[str, Any],
) -> None:
    """A replay reads a saved job's delegation id and nothing else of its child, so no snapshot stops it.

    A thread's session is shared by every agent and requester in it, whose jobs a turn there never collects.
    """
    bot = _priced_bot(tmp_path, background_jobs=True)
    await bot._take_over_replies()
    runner = unwrap_extracted_collaborator(bot._response_runner)
    runtime: ToolJobRuntime = await tool_job_runtime(tmp_path)
    _serve_jobs(bot, runtime)
    another = replace(_job_conversation()[1], agent_name="another-agent", requester_id="@another:localhost")

    async def finish() -> BackgroundOutcome:
        return BackgroundOutcome("completed", "Done elsewhere.")

    try:
        await start_job(
            runtime,
            "unreadable",
            tool_name="worker",
            depth=0,
            owner=another if same_thread else job_owner(),
            kind="delegation",
            adapter={"child": child},
            operation=finish,
        )
        await wait_for_status(runtime, "unreadable", "completed")
        source = "$beside-an-unreadable-job"
        await _admit_user_message(bot, source_event_id=source)
        request = _user_request(source_event_id=source)
        attempt = runner.generate_response(request)
        await _run_turn(monkeypatch, bot, attempt, _charge("c1", 0.5), _stall("s1"), cut_short=True)
        await _simulate_restart(bot, "gen-2")
        await _run_turn(monkeypatch, bot, runner.generate_response(request), answer("Done.", metrics=_REQ_USAGE))

        assert _final_ai_run_metadata(bot)[1]["cost"] == {"usd": "3.6", "subagents_usd": "0.5"}
    finally:
        await runtime.shutdown()


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "malformed",
    [
        {"adapter": None},
        {"consumed": True, "adapter": {"child": {"delegation_id": "elsewhere"}, "acknowledged_by_run": []}},
    ],
    ids=["no adapter", "an acknowledging run that is no run id"],
)
async def test_a_malformed_saved_job_does_not_stop_a_replay_on_an_instance_that_runs_no_jobs(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    malformed: dict[str, Any],
) -> None:
    """A saved job record that parses but holds what no writer saves is no receipt's job, and stops no turn's replay."""
    bot = _priced_bot(tmp_path)
    await bot._take_over_replies()
    runner = unwrap_extracted_collaborator(bot._response_runner)
    job = BackgroundJob("malformed", job_owner(), "worker", 0, kind="delegation", status="completed")
    await write_saved_job(bot.runtime_paths.storage_root, job.job_id, json.dumps({**asdict(job), **malformed}))
    source = "$beside-a-malformed-job"
    await _admit_user_message(bot, source_event_id=source)
    request = _user_request(source_event_id=source)
    attempt = runner.generate_response(request)
    await _run_turn(monkeypatch, bot, attempt, _charge("c1", 0.5), _stall("s1"), cut_short=True)
    await _simulate_restart(bot, "gen-2")
    await _run_turn(monkeypatch, bot, runner.generate_response(request), answer("Done.", metrics=_REQ_USAGE))

    assert _final_ai_run_metadata(bot)[1]["cost"] == {"usd": "3.6", "subagents_usd": "0.5"}


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
    attempt = runner.generate_response(request)
    await _run_turn(monkeypatch, bot, attempt, _charge("c1", 0.5), _stall("s1"), cut_short=True)
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
    final = answer("Recovered after lost middle attempt.", metrics=_REQ_USAGE)
    await _run_turn(monkeypatch, bot, runner.generate_response(request), final)

    _, ai_run = _final_ai_run_metadata(bot)
    assert ai_run["cost"] == {"usd": "3.6", "subagents_usd": "0.5"}


def test_every_view_of_a_turn_s_receipts_merges_to_the_same_total() -> None:
    """Saving a row twice, merging two rows, and resuming from a row each count one delegation once."""
    with receiving_subagent_cost(SubagentCost()):
        receive_subagent_cost("d1", 0.4)
        receive_subagent_cost("d1", 0.4)
        first = subagent_cost_metadata()
        receive_subagent_cost("d2", 0.3)
        second = subagent_cost_metadata(first)
    assert first == {SUBAGENT_RECEIPTS_RUN_METADATA_KEY: {"d1": 0.4}, SUBAGENT_COST_RUN_METADATA_KEY: 0.4}
    assert second[SUBAGENT_RECEIPTS_RUN_METADATA_KEY] == {"d1": 0.4, "d2": 0.3}
    assert second[SUBAGENT_COST_RUN_METADATA_KEY] == pytest.approx(0.7)
    # A save with nothing received keeps the row's; a resume goes on from it; two rows merge by id.
    assert subagent_cost_metadata(second) == second
    cost = SubagentCost(stored_receipts(second))
    with receiving_subagent_cost(cost):
        receive_subagent_cost("d3", 0.2)
        assert received_subagent_cost_usd() == pytest.approx(0.9)
        assert subagent_cost_metadata(first)[SUBAGENT_RECEIPTS_RUN_METADATA_KEY] == {"d1": 0.4, "d2": 0.3, "d3": 0.2}
    assert receipts_metadata(stored_receipts(first), stored_receipts(second)) == second
    # An older view of one id holds less, never more, whether views are merged or one is added to a turn's holder.
    assert merge_by_key(({"d1": 0.1}, {"d1": 0.4}, {"d1": 0.2}), float) == {"d1": 0.4}
    cost.add({"d1": 0.1, "d2": 0.5})
    assert cost.receipts == {"d1": 0.4, "d2": 0.5, "d3": 0.2}


def test_two_records_of_one_attempt_merge_to_the_one_that_counted_more() -> None:
    """A later record of an attempt counts more of anything, cached tokens too, and an entry that is no record is none."""
    earlier = RunMetrics(tokens=TokenCounts(input_tokens=10, output_tokens=5, cache_read_tokens=100))
    later = RunMetrics(tokens=TokenCounts(input_tokens=10, output_tokens=5, cache_read_tokens=400))

    for views in (({"run": earlier}, {"run": later}), ({"run": later}, {"run": earlier})):
        assert merge_usage(*views)["run"].tokens.cache_read_tokens == 400

    stored = {PRIOR_USAGE_BY_RUN_METADATA_KEY: {"run": later.to_dict(), "broken": "not a usage record"}}
    assert list(prior_usage_by_run(stored)) == ["run"]


async def _run_subagent_job_with_restarts(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    *,
    worker_turns: list[Any],
    restarts: int,
    expected_status: str,
    start_source: str,
    collect_source: str,
    worker_tools: tuple[str, ...] = (),
    while_stopped: Callable[[AgentBot], None] | None = None,
    while_stalled: Callable[[AgentBot, ToolJobRuntime], Awaitable[None]] | None = None,
) -> tuple[AgentBot, Any, dict[str, Any]]:
    """Run one background subagent job through ``restarts`` restarts, wait for ``expected_status``, and collect it.

    Every approval the worker's calls ask for is granted. ``while_stalled`` runs before each restart, once the worker
    waits for it, and ``while_stopped`` between its shutdown and its recovery.
    """
    bot = _priced_bot(tmp_path, background_jobs=True, with_worker=True, worker_tools=worker_tools)
    cards = JobApprovalCards(asyncio.get_running_loop().create_future())
    cards.decision.set_result(BackgroundApprovalDecision("approved", None))
    monkeypatch.setattr(approval_manager, "get_approval_store", lambda: cards)
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
        worker_model = ScriptedModel(worker_turns, identity=model_identity(bot.config.models["worker_model"]))
        leader_model_1 = _scripted_model(
            bot,
            [
                calls(
                    tool_call("run_subagent", "deleg-1", agent_name="worker", task="Investigate", background_after=0),
                    metrics=_REQ_USAGE,
                ),
                answer("Started worker.", metrics=_REQ_USAGE),
            ],
        )
        install_model_providers(monkeypatch, {"default": leader_model_1, "worker_model": worker_model})
        await _admit_user_message(bot, source_event_id=start_source)
        request_1 = _user_request(source_event_id=start_source)
        with patch_response_runner_module(
            should_use_streaming=AsyncMock(return_value=True),
            typing_indicator=_noop_typing,
        ):
            await runner.generate_response(request_1)
        for _ in range(restarts):
            await asyncio.wait_for(CostProbeTools.stalled.wait(), 10)
            CostProbeTools.stalled = asyncio.Event()
            if while_stalled is not None:
                await while_stalled(bot, runtime)
            await runtime.shutdown()
            if while_stopped is not None:
                while_stopped(bot)
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
        await wait_for_status(runtime, job_info.job_id, expected_status)
        settled_job = await lookup(runtime, job_info.job_id, owner=leader_owner, depth=0)
        collect_turns = [
            calls(tool_call("job", "collect-worker", action="collect"), metrics=_REQ_USAGE),
            answer("Worker reported back.", metrics=_REQ_USAGE),
        ]
        # The worker has ended, and one that switched the thread's model leaves the leader on its own, priced alike.
        collectors = {
            name: ScriptedModel(list(collect_turns), identity=model_identity(model))
            for name, model in bot.config.models.items()
        }
        install_model_providers(monkeypatch, collectors)
        await _admit_user_message(bot, source_event_id=collect_source)
        with patch_response_runner_module(
            should_use_streaming=AsyncMock(return_value=True),
            typing_indicator=_noop_typing,
        ):
            await runner.generate_response(_user_request(source_event_id=collect_source))
        _, ai_run = _final_ai_run_metadata(bot)
        return bot, settled_job, ai_run
    finally:
        await runtime.shutdown()


@pytest.mark.asyncio
async def test_restart_continued_subagent_job_carries_pre_restart_own_and_nested_cost_to_parent_reply(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A subagent job continued across restarts rolls all attempts' own and nested subagent costs into the parent reply."""
    bot, done, ai_run = await _run_subagent_job_with_restarts(
        tmp_path,
        monkeypatch,
        worker_turns=[
            calls(tool_call("charge_subagent", "w1", usd=0.5), metrics=_REQ_USAGE),
            calls(tool_call("stall", "ws1"), metrics=_REQ_USAGE),
            calls(tool_call("charge_subagent", "w2", usd=0.3), metrics=_REQ_USAGE),
            calls(tool_call("stall", "ws2"), metrics=_REQ_USAGE),
            answer("Worker finished.", metrics=_REQ_USAGE),
        ],
        restarts=2,
        expected_status="completed",
        start_source="$start-subagent",
        collect_source="$collect-subagent",
    )
    child_done = delegation_child(done)
    assert child_done.cost_usd == pytest.approx(6.8)
    assert ai_run["cost"] == {"usd": "2.4", "subagents_usd": "6.8"}
    # Astra #3: each worker row holds what its turn received by delegation id: its own receipts, and for a
    # continuation the turns before it, each under its own id with its own cost ($2.40).
    worker_storage = create_session_storage("worker", bot.config, bot.runtime_paths, execution_identity=None)
    worker_runs = [r for r in worker_storage.list_runs(child_done.session_id) if isinstance(r, RunRecord)]
    first, second, third = (_receipts(run) for run in worker_runs)
    assert sorted(first.values()) == [pytest.approx(0.5)]
    assert sorted(second.values()) == [pytest.approx(0.3), pytest.approx(0.5), pytest.approx(2.4)]
    assert sorted(third.values()) == [pytest.approx(0.3), pytest.approx(0.5), pytest.approx(2.4), pytest.approx(2.4)]
    assert first.items() <= second.items() <= third.items()
    assert _stored_sums(worker_runs) == [pytest.approx(0.5), pytest.approx(3.2), pytest.approx(5.6)]
    # Opus minor 3: final delegation record's run.json usage keeps only its own attempt (100k in, 10k out).
    deleg_glob = "agents/worker/workspace/.mindroom/delegations/*/*/run.json"
    record_files = list(bot.runtime_paths.storage_root.glob(deleg_glob))
    final_records = [
        json.loads(p.read_text())
        for p in record_files
        if json.loads(p.read_text())["delegation_id"] == child_done.delegation_id
    ]
    assert len(final_records) == 1
    assert final_records[0]["usage"]["input_tokens"] == 100_000
    assert final_records[0]["usage"]["output_tokens"] == 10_000


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("second_sub", "replay_sub", "expected_sub", "expected_own", "expected_stored"),
    [
        # Astra #1: A receives 0.5 + switches, B receives nothing + stalls, C receives 0.25.
        (None, 0.25, "0.75", "6", [pytest.approx(0.5), pytest.approx(0.5), pytest.approx(0.75)]),
        # Sol #1: A receives 0.5 + switches, B receives 0.25 + stalls, C receives 0.125.
        (0.25, 0.125, "0.875", "7.2", [pytest.approx(0.5), pytest.approx(0.75), pytest.approx(0.875)]),
    ],
    ids=["astra-no-b-receipt", "sol-with-b-receipt"],
)
async def test_model_switch_before_restart_does_not_double_count_subagent_cost(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    second_sub: float | None,
    replay_sub: float,
    expected_sub: str,
    expected_own: str,
    expected_stored: list[object],
) -> None:
    """A receipt the run after a model switch repeats is the same delegation, so a later restart counts it once."""
    bot = _priced_bot(tmp_path, extra_tools=("thread_model",))
    await bot._take_over_replies()
    runner = unwrap_extracted_collaborator(bot._response_runner)
    source = "$model-switch-restart"
    await _admit_user_message(bot, source_event_id=source)
    request = _user_request(source_event_id=source)
    session_id = request.response_envelope.target.session_id

    switch_call = tool_call("switch_thread_model", "sw-1", model_name="default", when="after-toolcall")
    attempt_1_turns = [
        _charge("c1", 0.5),
        calls(switch_call, metrics=_REQ_USAGE),
        *([_charge("c2", second_sub)] if second_sub is not None else []),
        _stall("s1"),
    ]
    await _run_turn(monkeypatch, bot, runner.generate_response(request), *attempt_1_turns, cut_short=True)
    await _simulate_restart(bot, "gen-2")
    replay = (_charge("c3", replay_sub), answer("Done after switch and restart.", metrics=_REQ_USAGE))
    await _run_turn(monkeypatch, bot, runner.generate_response(request), *replay)

    _, ai_run = _final_ai_run_metadata(bot)
    assert ai_run["cost"] == {"usd": expected_own, "subagents_usd": expected_sub}
    runs = _stored_runs(runner, session_id)
    assert _stored_sums(runs) == expected_stored


@pytest.mark.asyncio
@pytest.mark.parametrize("restarts", [1, 2], ids=["one-restart", "two-restarts"])
async def test_billed_empty_response_retried_before_restart_is_counted_exactly_once(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    restarts: int,
) -> None:
    """A billed empty reply (usage ledger only, no run row) retried before a restart is counted once across restarts."""
    bot = _priced_bot(tmp_path)
    await bot._take_over_replies()
    runner = unwrap_extracted_collaborator(bot._response_runner)
    source = f"$empty-retry-restart-{restarts}"
    await _admit_user_message(bot, source_event_id=source)
    request = _user_request(source_event_id=source)

    # Attempt 1: empty answer ($1.20, no run row) -> retry charges $0.50 ($1.20) + stalls ($1.20) -> restart #1.
    first = (answer("", metrics=_REQ_USAGE), _charge("c1", 0.5), _stall("s1"))
    await _run_turn(monkeypatch, bot, runner.generate_response(request), *first, cut_short=True)
    await _simulate_restart(bot, "gen-2")

    if restarts == 2:
        # Attempt 2: charges $0.10 ($1.20) + stalls ($1.20) -> restart #2.
        attempt = runner.generate_response(request)
        await _run_turn(monkeypatch, bot, attempt, _charge("c2", 0.1), _stall("s2"), cut_short=True)
        await _simulate_restart(bot, "gen-3")

    # Final replay: charges $0.25 ($1.20) + answers ($1.20).
    final = (_charge("cf", 0.25), answer("Done.", metrics=_REQ_USAGE))
    await _run_turn(monkeypatch, bot, runner.generate_response(request), *final)

    _, ai_run = _final_ai_run_metadata(bot)
    if restarts == 1:
        # 1 empty ($1.20) + 2 checkpointed ($2.40) + 2 final ($2.40) = $6.00; subagents = 0.5 + 0.25 = 0.75.
        assert ai_run["cost"] == {"usd": "6", "subagents_usd": "0.75"}
        assert sum(inp for _, inp, _ in _recorded_usage_tokens(bot)) == 500_000
    else:
        # 1 empty ($1.20) + 2 + 2 + 2 = 7 requests = $8.40; subagents = 0.5 + 0.1 + 0.25 = 0.85.
        assert ai_run["cost"] == {"usd": "8.4", "subagents_usd": "0.85"}
        assert sum(inp for _, inp, _ in _recorded_usage_tokens(bot)) == 700_000


@pytest.mark.asyncio
async def test_restart_continued_reply_passes_undated_started_at_to_budgets_to_avoid_double_count(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A restart-continued reply passes started_at=None to budgets so pre-restart usage is recounted, not double-counted."""
    bot = _priced_bot(tmp_path)
    await bot._take_over_replies()
    runner = unwrap_extracted_collaborator(bot._response_runner)

    recorded_calls: list[tuple[str | None, float | None, object]] = []
    original = bot.orchestrator.budgets.response_finished

    def capture(requester_id: str | None, cost_usd: float | None, started_at: object) -> None:
        recorded_calls.append((requester_id, cost_usd, started_at))
        original(requester_id, cost_usd, started_at)  # type: ignore[arg-type]

    monkeypatch.setattr(bot.orchestrator.budgets, "response_finished", capture)

    # Normal (non-continued) reply passes a non-None started_at datetime.
    normal_source = "$normal-budget-turn"
    await _admit_user_message(bot, source_event_id=normal_source)
    normal = runner.generate_response(_user_request(source_event_id=normal_source))
    await _run_turn(monkeypatch, bot, normal, answer("Normal.", metrics=_REQ_USAGE))
    assert len(recorded_calls) == 1
    assert recorded_calls[0][1] == pytest.approx(1.2)
    assert recorded_calls[0][2] is not None

    # Restart-continued reply must pass started_at=None.
    source = "$restart-budget-turn"
    await _admit_user_message(bot, source_event_id=source)
    request = _user_request(source_event_id=source)
    attempt = runner.generate_response(request)
    await _run_turn(monkeypatch, bot, attempt, _charge("c1", 0.5), _stall("s1"), cut_short=True)
    await _simulate_restart(bot, "gen-2")
    await _run_turn(monkeypatch, bot, runner.generate_response(request), answer("Continued.", metrics=_REQ_USAGE))

    assert len(recorded_calls) == 2
    assert recorded_calls[1][0] == "@user:localhost"
    assert recorded_calls[1][1] == pytest.approx(3.6)
    assert recorded_calls[1][2] is None


@pytest.mark.asyncio
async def test_subagent_job_refused_at_continuation_cap_still_reports_known_cost_to_parent_reply(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """When a subagent job hits the restart continuation cap and ends interrupted, its spent cost still reaches the parent."""
    _, stopped, ai_run = await _run_subagent_job_with_restarts(
        tmp_path,
        monkeypatch,
        worker_turns=[
            calls(tool_call("charge_subagent", "w1", usd=0.5), metrics=_REQ_USAGE),
            calls(tool_call("stall", "ws1"), metrics=_REQ_USAGE),
            calls(tool_call("charge_subagent", "w2", usd=0.3), metrics=_REQ_USAGE),
            calls(tool_call("stall", "ws2"), metrics=_REQ_USAGE),
            calls(tool_call("stall", "ws3"), metrics=_REQ_USAGE),
        ],
        restarts=3,
        expected_status="interrupted",
        start_source="$start-cap-subagent",
        collect_source="$collect-cap-subagent",
    )
    assert delegation_child(stopped).cost_usd == pytest.approx(6.8)
    assert ai_run["cost"] == {"usd": "2.4", "subagents_usd": "6.8"}


@pytest.mark.asyncio
async def test_a_subagent_restarted_after_a_model_switch_and_an_approval_counts_what_it_received_once(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A child that switched models, paused for an approval and was cut short after it counts each receipt once.

    Its turn's runs each store what the turn had received, the run the approval resumed too, so the continuation
    after the restart reads the same two receipts from whichever of them it finds.
    """
    switch = tool_call("switch_thread_model", "sw", model_name="worker_model", when="after-toolcall")
    bot, done, ai_run = await _run_subagent_job_with_restarts(
        tmp_path,
        monkeypatch,
        worker_tools=("thread_model",),
        worker_turns=[
            calls(tool_call("charge_subagent", "w1", usd=0.5), metrics=_REQ_USAGE),
            calls(switch, metrics=_REQ_USAGE),
            calls(tool_call("charge_subagent", "w2", usd=0.25), metrics=_REQ_USAGE),
            calls(tool_call("checked", "approve"), metrics=_REQ_USAGE),
            calls(tool_call("stall", "ws1"), metrics=_REQ_USAGE),
            answer("Worker finished.", metrics=_REQ_USAGE),
        ],
        restarts=1,
        expected_status="completed",
        start_source="$start-approved-subagent",
        collect_source="$collect-approved-subagent",
    )
    assert [tool_name for _, _, tool_name in approval_manager.get_approval_store().requested] == ["checked"]
    # The attempt that resumed the paused run ran the approved call, having received what the run had before its pause.
    assert CostProbeTools.received_when_checked == pytest.approx(0.75)
    # The worker's six requests ($7.20) and its two receipts ($0.75).
    child = delegation_child(done)
    assert child.cost_usd == pytest.approx(7.95)
    assert ai_run["cost"] == {"usd": "2.4", "subagents_usd": "7.95"}
    # The run before the switch, the run the approval paused and resumed, and the continuation, which starts from
    # the two receipts and the five requests ($6.00) of the turn the restart cut short.
    worker_storage = create_session_storage("worker", bot.config, bot.runtime_paths, execution_identity=None)
    assert _stored_sums(worker_storage.list_runs(child.session_id)) == pytest.approx([0.5, 0.75, 6.75])


@pytest.mark.asyncio
async def test_a_continuation_cut_short_before_its_first_checkpoint_hands_on_what_it_started_from(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A continuation killed during its first request stored no run, and the next one starts from what it started from."""
    restarts = []

    async def never_answers() -> None:
        CostProbeTools.stalled.set()
        await asyncio.Event().wait()

    def killed_before_its_first_checkpoint(bot: AgentBot) -> None:
        restarts.append(bot)
        if len(restarts) == 2:
            # The shutdown stored the continuation's run as interrupted; a killed process stores nothing.
            store = create_session_storage("worker", bot.config, bot.runtime_paths, execution_identity=None)
            runs = quote_identifier(store.names.log_runs)
            with store.transaction(write=True) as connection:
                connection.execute(f"DELETE FROM {runs} WHERE seq = (SELECT MAX(seq) FROM {runs})")  # noqa: S608

    bot, done, ai_run = await _run_subagent_job_with_restarts(
        tmp_path,
        monkeypatch,
        worker_turns=[
            calls(tool_call("charge_subagent", "w1", usd=0.5), metrics=_REQ_USAGE),
            calls(tool_call("stall", "ws1"), metrics=_REQ_USAGE),
            [Await(never_answers)],
            answer("Worker finished.", metrics=_REQ_USAGE),
        ],
        restarts=2,
        expected_status="completed",
        start_source="$start-rowless-continuation",
        collect_source="$collect-rowless-continuation",
        while_stopped=killed_before_its_first_checkpoint,
    )
    child = delegation_child(done)
    worker_storage = create_session_storage("worker", bot.config, bot.runtime_paths, execution_identity=None)
    # The first turn's run and the last continuation's: the continuation between them left none.
    assert len(worker_storage.list_runs(child.session_id)) == 2
    # The first turn's two requests ($2.40) and its receipt ($0.50), and the last continuation's request ($1.20).
    assert child.cost_usd == pytest.approx(4.1)
    assert ai_run["cost"] == {"usd": "2.4", "subagents_usd": "4.1"}


@pytest.mark.asyncio
async def test_a_continuation_leaves_out_what_its_cut_short_turn_stored_as_a_sum(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A subagent turn an earlier release cut short stored what it received as a sum, which its continuation drops.

    The sum may cover receipts the continuation also reads by id, so it starts from the turn's own cost alone.
    """

    def as_an_earlier_release_stored_them(bot: AgentBot) -> None:
        store = create_session_storage("worker", bot.config, bot.runtime_paths, execution_identity=None)
        with store.transaction(write=True) as connection:
            connection.execute(
                f"UPDATE {quote_identifier(store.names.log_runs)} SET metadata = json_remove(metadata, ?)",  # noqa: S608
                (f"$.{SUBAGENT_RECEIPTS_RUN_METADATA_KEY}",),
            )

    bot, done, ai_run = await _run_subagent_job_with_restarts(
        tmp_path,
        monkeypatch,
        worker_turns=[
            calls(tool_call("charge_subagent", "w1", usd=0.5), metrics=_REQ_USAGE),
            calls(tool_call("stall", "ws1"), metrics=_REQ_USAGE),
            answer("Worker finished.", metrics=_REQ_USAGE),
        ],
        restarts=1,
        expected_status="completed",
        start_source="$start-summed-subagent",
        collect_source="$collect-summed-subagent",
        while_stopped=as_an_earlier_release_stored_them,
    )
    child = delegation_child(done)
    worker_storage = create_session_storage("worker", bot.config, bot.runtime_paths, execution_identity=None)
    cut_short, _continuation = worker_storage.list_runs(child.session_id)
    assert _receipts(cut_short) == {}
    assert _stored_sums([cut_short]) == [0.5]
    # The cut-short turn's two requests ($2.40) and the continuation's one ($1.20), without the summed $0.50.
    assert child.cost_usd == pytest.approx(3.6)
    assert ai_run["cost"] == {"usd": "2.4", "subagents_usd": "3.6"}


@pytest.mark.asyncio
async def test_a_continuation_starts_from_the_nested_jobs_its_cut_short_turn_s_runs_acknowledged(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A subagent collects two jobs of its own, and a restart cuts its turn short before the second's acknowledgement.

    The run before its model switch acknowledged the first, so the continuation starts from that receipt. The second
    stays for whichever of the subagent's turns collects it again, which receives it then.
    """

    async def two_nested_jobs(bot: AgentBot, runtime: ToolJobRuntime) -> None:
        child = delegation_child(*runtime.retained_jobs())
        nested_owner = replace(job_owner(), agent_name="worker", session_id=child.session_id)
        worker_storage = create_session_storage("worker", bot.config, bot.runtime_paths, execution_identity=None)
        before_switch, _cut_short = worker_storage.list_runs(child.session_id)
        await _completed_research_job(runtime, nested_owner, "acknowledged")
        waited = await runtime.wait("acknowledged", owner=nested_owner, depth=0)
        await runtime.acknowledge_wait("acknowledged", waited.claim, adapter={ACKNOWLEDGED_BY: before_switch.run_id})
        await _completed_research_job(runtime, nested_owner, "unacknowledged")

    switch = tool_call("switch_thread_model", "sw", model_name="worker_model", when="after-toolcall")
    _, done, ai_run = await _run_subagent_job_with_restarts(
        tmp_path,
        monkeypatch,
        worker_tools=("thread_model",),
        worker_turns=[
            calls(tool_call("charge_subagent", "w1", usd=0.7, child="acknowledged"), metrics=_REQ_USAGE),
            calls(switch, metrics=_REQ_USAGE),
            calls(tool_call("charge_subagent", "w2", usd=0.6, child="unacknowledged"), metrics=_REQ_USAGE),
            calls(tool_call("stall", "ws1"), metrics=_REQ_USAGE),
            answer("Worker finished.", metrics=_REQ_USAGE),
        ],
        restarts=1,
        expected_status="completed",
        start_source="$start-nested-jobs",
        collect_source="$collect-nested-jobs",
        while_stalled=two_nested_jobs,
    )
    # The worker's five requests ($6.00) and the first job's $0.70, without the second job's $0.60.
    assert delegation_child(done).cost_usd == pytest.approx(6.7)
    assert ai_run["cost"] == {"usd": "2.4", "subagents_usd": "6.7"}


@pytest.mark.asyncio
async def test_a_continuation_a_hook_refuses_still_reports_the_cut_short_turn_s_cost(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A continuation that ends before its first run hands its caller what the turn the restart cut short cost."""
    dispatch = delegation_hooks.dispatch_external_tool_hooks

    async def refuse_continuations(**kwargs: Any) -> str | None:  # noqa: ANN401
        if kwargs["tool_name"] == CONTINUE_SUBAGENT and kwargs["before"]:
            return "A hook refused the continuation."
        return await dispatch(**kwargs)

    monkeypatch.setattr(delegation_hooks, "dispatch_external_tool_hooks", refuse_continuations)
    _, failed, ai_run = await _run_subagent_job_with_restarts(
        tmp_path,
        monkeypatch,
        worker_turns=[
            calls(tool_call("charge_subagent", "w1", usd=0.5), metrics=_REQ_USAGE),
            calls(tool_call("stall", "ws1"), metrics=_REQ_USAGE),
        ],
        restarts=1,
        expected_status="failed",
        start_source="$start-refused-subagent",
        collect_source="$collect-refused-subagent",
    )
    # The cut-short turn's two requests ($2.40) and what it received ($0.50).
    assert delegation_child(failed).cost_usd == pytest.approx(2.9)
    assert ai_run["cost"] == {"usd": "2.4", "subagents_usd": "2.9"}


def _attempt(run_id: str, *, tokens: int, **metadata: Any) -> RunRecord:  # noqa: ANN401
    """Return a cut-short attempt at ``_TURN``'s turn that used ``tokens`` input tokens, with ``metadata``."""
    return RunRecord(
        run_id=run_id,
        session_id=_SESSION,
        agent_id="general",
        status=RunStatus.cancelled,
        metrics=RunMetrics(tokens=TokenCounts(input_tokens=tokens)),
        metadata=build_matrix_run_metadata(_TURN, [], extra_metadata=metadata),
        messages=[Message(role="user", content="do work")],
    )


def _recovered(bot: AgentBot, *attempts: RunRecord, jobs: dict[str, str | None] | None = None) -> dict[str, Any]:
    """Store ``attempts`` and return the run metadata the replay of their turn carries.

    ``jobs`` names the run that acknowledged each subagent job of the conversation, by its child's delegation id.
    """
    store = unwrap_extracted_collaborator(bot._response_runner).deps.state_writer.create_storage(None)
    session = new_scope_session(session_id=_SESSION, scope=_SCOPE)
    for attempt in attempts:
        store.save_run(session, attempt, record_usage=False)
    turn = recover_interrupted_turn(store, _SESSION, _SCOPE, {_TURN}, natively=False)
    return replay_run_metadata(turn, jobs or {})


def _input_tokens(recovered: dict[str, Any]) -> dict[str, int]:
    return {run_id: usage.tokens.input_tokens for run_id, usage in prior_usage_by_run(recovered).items()}


def test_recovery_merges_what_the_attempts_rows_hold_by_id_and_leaves_out_jobs_their_runs_did_not_acknowledge(
    tmp_path: Path,
) -> None:
    """Rows that repeat each other, as after a model switch or an earlier restart, count each attempt and receipt once."""
    bot = _priced_bot(tmp_path)
    empty = RunMetrics(tokens=TokenCounts(input_tokens=7))
    first = _attempt("run-a", tokens=100, **receipts_metadata({"d1": 0.5}))
    # The second run follows a retried empty reply, which stored no row, and the first run, whose receipt it repeats.
    # It also collected three subagent jobs, which cost $0.60, $0.70 and $0.80.
    received = receipts_metadata({"d1": 0.5, "own": 0.6, "other": 0.7, "none": 0.8})
    second = _attempt("run-b", tokens=200, **with_prior_usage(received, {"empty": empty, "run-a": first.metrics}))
    # A member of the second run, a team's, collected a fourth, which cost $0.90.
    member = replace(_attempt("member", tokens=0, **receipts_metadata({"member-s": 0.9})), parent_run_id="run-b")

    # Its own run acknowledged the first job and another turn's run the second; no run acknowledged the third, or the
    # release that did kept no record of it. The member's run acknowledged the fourth.
    acknowledged_by = {"own": "run-b", "other": "another-turn-s-run", "none": None, "member-s": "member"}
    recovered = _recovered(bot, first, second, member, jobs=acknowledged_by)

    assert recovered[CONTINUED_RUNS_METADATA_KEY] == ["run-a", "run-b"]
    assert _input_tokens(recovered) == {"empty": 7, "run-a": 100, "run-b": 200}
    assert recovered[PRIOR_USAGE_RUN_METADATA_KEY]["input_tokens"] == 307
    # A job is reported by the turn whose run acknowledged it, so the receipts of the first and the fourth are carried.
    assert stored_receipts(recovered) == {"d1": 0.5, "own": 0.6, "member-s": 0.9}
    assert recovered[SUBAGENT_COST_RUN_METADATA_KEY] == 2.0
    # A delegation that is no job of the conversation, such as a subagent the turn waited for, is carried as it is.
    assert stored_receipts(_recovered(bot)) == {"d1": 0.5, "own": 0.6, "other": 0.7, "none": 0.8, "member-s": 0.9}


def test_a_job_s_acknowledger_is_read_beside_the_id_its_saved_child_names_and_nothing_else() -> None:
    """A subagent job whose saved child names an id is that receipt's job, whatever else its snapshot holds or lacks."""

    def job(kind: str, child: object, *, consumed: bool = False, run: object = None) -> BackgroundJob:
        adapter = {"child": child} if run is None else {"child": child, "acknowledged_by_run": run}
        return BackgroundJob("a-job", job_owner(), "worker", 0, kind=kind, adapter=adapter, consumed=consumed)

    jobs = [
        replace(job("delegation", {"delegation_id": "of a job without an adapter"}), adapter=None),
        replace(job("delegation", {"delegation_id": "of a job whose adapter is no mapping"}), adapter=["child"]),
        job("delegation", {"delegation_id": "acknowledged by what is no run id"}, consumed=True, run=["run-1"]),
        job("delegation", {"delegation_id": "acknowledged", "field_of_a_later_release": 1}, consumed=True, run="run-1"),
        job("delegation", {"delegation_id": "acknowledged by an earlier release"}, consumed=True),
        job("delegation", {"delegation_id": "left for a later turn"}),
        job("delegation", {"delegation_id": ["not", "an", "id"]}),
        job("delegation", {"written_by": "a release this one cannot read"}),
        job("delegation", "not a snapshot"),
        job("tool", {"delegation_id": "a tool's own adapter"}, consumed=True, run="run-1"),
    ]
    assert job_acknowledgers(jobs) == {
        "acknowledged": "run-1",
        "acknowledged by what is no run id": None,
        "acknowledged by an earlier release": None,
        "left for a later turn": None,
    }


def test_values_stored_as_sums_resume_as_one_entry_that_restart_recovery_drops(tmp_path: Path) -> None:
    """Rows an earlier release wrote hold sums without ids: a resume goes on from them, a restart's replay does not.

    That release's holder outlived a model switch, so the second run's sum covers the first run's receipt, and its
    summed prior usage covers the first run. Adding either to what the rows hold by id would count it twice.
    """
    bot = _priced_bot(tmp_path)
    first = _attempt("run-a", tokens=100, **{SUBAGENT_COST_RUN_METADATA_KEY: 0.5})
    spent = {PRIOR_USAGE_RUN_METADATA_KEY: RunMetrics(tokens=TokenCounts(input_tokens=100)).to_dict()}
    second = _attempt("run-b", tokens=200, **{SUBAGENT_COST_RUN_METADATA_KEY: 0.75}, **spent)

    # A resume of the second run reports what that release would have: its sums, each as one entry.
    assert stored_receipts(second.metadata) == {LEGACY_COST_ID: 0.75}
    assert _input_tokens(second.metadata or {}) == {LEGACY_COST_ID: 100}
    with receiving_subagent_cost(SubagentCost(stored_receipts(second.metadata))):
        receive_subagent_cost("d2", 0.25)
        assert subagent_cost_metadata(second.metadata) == {
            SUBAGENT_RECEIPTS_RUN_METADATA_KEY: {LEGACY_COST_ID: 0.75, "d2": 0.25},
            SUBAGENT_COST_RUN_METADATA_KEY: 1.0,
        }

    recovered = _recovered(bot, first, second)

    # The replay reports each run's own usage, which is exact, and no receipt, as that release's replay did.
    assert _input_tokens(recovered) == {"run-a": 100, "run-b": 200}
    assert stored_receipts(recovered) == {}
    assert SUBAGENT_COST_RUN_METADATA_KEY not in recovered


def test_a_sum_an_earlier_release_advanced_past_its_map_resumes_with_the_difference(tmp_path: Path) -> None:
    """After a rollback, an earlier release resumed a run and added what it received to the sum alone.

    The run's next resume goes on from the whole sum, the part no id names as one entry, and stores it so; a
    restart's replay carries only what the map names.
    """
    bot = _priced_bot(tmp_path)
    row = {**receipts_metadata({"d1": 1.0}), SUBAGENT_COST_RUN_METADATA_KEY: 3.0}

    assert stored_receipts(row) == {"d1": 1.0, LEGACY_COST_ID: 2.0}
    with receiving_subagent_cost(SubagentCost(stored_receipts(row))):
        assert received_subagent_cost_usd() == 3.0
        assert subagent_cost_metadata(row) == receipts_metadata({"d1": 1.0, LEGACY_COST_ID: 2.0})
    assert stored_receipts(_recovered(bot, _attempt("run-a", tokens=1, **row))) == {"d1": 1.0}


def test_a_patch_that_carries_receipts_merges_them_into_the_row_s(tmp_path: Path) -> None:
    """A delegation state's patch stores what its turn received: a holder that knows less never shrinks the row."""
    bot = _priced_bot(tmp_path)
    store = unwrap_extracted_collaborator(bot._response_runner).deps.state_writer.create_storage(None)
    session = new_scope_session(session_id=_SESSION, scope=_SCOPE)
    store.save_run(
        session,
        _attempt("run-a", tokens=1, **receipts_metadata({"d1": 0.5, "d2": 0.25})),
        record_usage=False,
    )

    assert store.patch_run_metadata(_SESSION, "run-a", {"other": True, **receipts_metadata({"d2": 0.25, "d3": 0.125})})

    run = store.get_run("run-a")
    assert run is not None
    assert stored_receipts(run.metadata) == {"d1": 0.5, "d2": 0.25, "d3": 0.125}
    assert (run.metadata or {})[SUBAGENT_COST_RUN_METADATA_KEY] == 0.875
