"""Live provisional token usage, cost, context, and tool-count metadata tests on Matrix replies."""

from __future__ import annotations

import asyncio
from dataclasses import replace
from functools import partial
from typing import TYPE_CHECKING, Any
from unittest.mock import patch

import pytest

from mindroom import reply_lifecycle as rl
from mindroom.agent_cli.shell_contract import AgentCliShellEnv
from mindroom.agent_loop.model_loop import EMPTY_REQUEST, LoopHooks, run_model_loop
from mindroom.agent_loop.spec import RunContext, UnstoredSession
from mindroom.agent_loop.tools import PreparedTools
from mindroom.ai_run_metadata import (
    build_prepared_history_metadata_content,
    build_terminal_note_ai_run_metadata_content,
    model_payload,
)
from mindroom.approval.tool_approval import shutdown_approval_runtime
from mindroom.config.approval import ApprovalRuleConfig, ToolApprovalConfig
from mindroom.constants import (
    AI_RUN_METADATA_KEY,
    STREAM_STATUS_APPROVAL_PENDING,
    STREAM_STATUS_CANCELLED,
    STREAM_STATUS_COMPLETED,
    STREAM_STATUS_ERROR,
    STREAM_STATUS_PENDING,
    STREAM_STATUS_STREAMING,
)
from mindroom.delegation.cost import SubagentCost
from mindroom.event_journal import ApprovalCall, ApprovalDecision
from mindroom.event_journal.approval_calls import approval_arguments_digest
from mindroom.history.types import PreparedHistoryState
from mindroom.live_frames.publisher import LivePublisher
from mindroom.llm.messages import Message, MessageMeta, ModelProvenance
from mindroom.llm.provider import ModelIdentity, TextDelta, TurnCompleted, UsageReported
from mindroom.llm.runs import RunRecord, RunRequirement, RunStatus, TeamRunRecord, ToolExecution
from mindroom.llm.usage import MessageMetrics, RunMetrics, TokenCounts
from mindroom.providers.pipeline.fallback import ChainModel, fallback_stream
from mindroom.providers.pipeline.wrapped import WrappedProvider
from mindroom.reply_ai_run_progress import ReplyAiRunProgress, build_provisional_ai_run_content
from mindroom.reply_presentation import AGENT_PLACEHOLDER, NoteKind, Presentation, Segment, encode_presentation
from mindroom.reply_scope import SpanHandle, pause_decision
from mindroom.response_sources import Owned, ResponseSources
from mindroom.session_storage.agent_storage import create_session_storage
from mindroom.session_storage.types import ConversationSession
from mindroom.streaming import StreamingResponse
from mindroom.teams.leader import seed_team_resume_ai_run
from mindroom.tool_system.authoring import Toolkit
from tests.approval_continuation_helpers import approval_continuation, dispatch_approval
from tests.conftest import (
    install_model_providers,
    make_matrix_client_mock,
    runtime_paths_for,
    unwrap_extracted_collaborator,
)
from tests.fakes.scripted_model import Await, ScriptedModel, answer, calls, tool_call, usage
from tests.response_runner_helpers import _plain_request, _target
from tests.scripted_agent_helpers import registered_toolkit
from tests.streaming_helpers import FakeReplyRecords
from tests.streaming_metadata_helpers import (
    ECHO_TOOL,
    REQUESTER,
    ROOM,
    SOURCE,
    THREAD,
    ai_run,
    answered_model_payload,
    approve_call,
    assert_provisional_ai_run,
    assert_provisional_contract,
    gated_answer,
    install_approval_store,
    register_echo_tool,
    running_task,
    setup_turn,
    stop_reply,
    stream_status,
    streaming_metadata_config,
    visible_content,
    wait_for_writes,
)

if TYPE_CHECKING:
    from collections.abc import Iterator
    from pathlib import Path


@pytest.fixture
def echo_tool() -> Iterator[None]:
    """Register a lightweight echo toolkit for approval tests."""
    with register_echo_tool():
        yield


def test_provisional_ai_run_metadata_and_progress_unit(tmp_path: Path) -> None:
    """Provisional helpers omit terminal keys/floats, fall back to prepared context, and track subagent cost."""
    config = streaming_metadata_config(tmp_path, priced=True)
    prepared = PreparedHistoryState(prepared_context_tokens=12_345, native_history=None)
    metrics = RunMetrics(duration=4.5, time_to_first_token=0.25)
    metrics.add(
        MessageMetrics(tokens=TokenCounts(input_tokens=1_000, output_tokens=50, total_tokens=1_050)),
        model_id="gpt-primary",
        provider="OpenAI",
    )
    live_messages = [
        Message(
            role="assistant",
            content="hi",
            metrics=MessageMetrics(tokens=TokenCounts(input_tokens=1_500, output_tokens=50, total_tokens=1_550)),
            meta=MessageMeta(model=ModelProvenance("gpt-primary", "OpenAI")),
        ),
    ]

    # Live messages win over prepared_history; zero tool_count is omitted; subagents_usd is explicit.
    built = build_provisional_ai_run_content(
        config=config,
        model_name="default",
        model="gpt-primary",
        model_provider="OpenAI",
        metrics=metrics,
        messages=live_messages,
        prepared_history=prepared,
        tool_count=0,
        subagents_usd=0.0125,
    )[AI_RUN_METADATA_KEY]
    assert_provisional_contract(built)
    assert built["usage"] == {
        "input_tokens": 1_000,
        "output_tokens": 50,
        "total_tokens": 1_050,
        "time_to_first_token": "0.25",
        "uncached_input_tokens": 1_000,
    }
    assert built["cost"] == {"usd": "0.0024", "subagents_usd": "0.0125"}
    assert built["context"] == {"input_tokens": 1_500, "window_tokens": 100_000}
    assert "tools" not in built

    # Unpriced model omits cost rather than fabricating "0".
    unpriced = streaming_metadata_config(tmp_path / "unpriced")
    unpriced_payload = build_provisional_ai_run_content(
        config=unpriced,
        model_name="default",
        model="gpt-primary",
        model_provider="OpenAI",
        metrics=metrics,
        tool_count=2,
    )[AI_RUN_METADATA_KEY]
    assert "cost" not in unpriced_payload
    assert unpriced_payload["tools"] == {"count": 2}

    # ReplyAiRunProgress tracks prepared fallback, request updates, model switches, and subagent receipts.
    progress = ReplyAiRunProgress()
    assert not progress.has_metrics()
    progress.set_model(model_payload(config, "default"))
    assert not progress.has_metrics()
    assert progress.payload() == {"version": 1, "model": model_payload(config, "default")}

    progress.set_prepared_context(config, "default", prepared)
    assert progress.has_metrics()
    assert (progress.payload() or {})["context"] == {"input_tokens": 12_345, "window_tokens": 100_000}
    v_prepared = progress.snapshot_version()

    record = RunRecord(
        run_id="r1",
        session_id="s1",
        model="gpt-primary",
        model_provider="OpenAI",
        model_name="default",
        metrics=metrics,
        tools=[ToolExecution(tool_call_id="c1", tool_name="echo")],
    )
    progress.update_from_run(
        config,
        model_name="default",
        record=record,
        messages=live_messages,
        prepared_history=prepared,
    )
    v_updated = progress.snapshot_version()
    assert v_updated != v_prepared

    subagent_cost = SubagentCost()
    progress.bind_subagent_cost(subagent_cost)
    subagent_cost.add({"child-1": 0.05})
    assert progress.snapshot_version() != v_updated
    with_sub = assert_provisional_contract(progress.payload())
    assert with_sub["cost"] == {"usd": "0.0024", "subagents_usd": "0.05"}
    assert with_sub["tools"] == {"count": 1}

    # Terminal note preserves provisional metrics without copying run_id/session_id.
    terminal = build_terminal_note_ai_run_metadata_content(
        "cancelled",
        provisional={**(progress.payload() or {}), "run_id": "leaked", "session_id": "leaked"},
    )[AI_RUN_METADATA_KEY]
    assert terminal["status"] == "cancelled"
    assert "run_id" not in terminal
    assert "session_id" not in terminal
    assert terminal["usage"]["total_tokens"] == 1_050
    assert terminal["cost"] == {"usd": "0.0024", "subagents_usd": "0.05"}
    assert terminal["tools"] == {"count": 1}

    # Switching models keeps cumulative spend while dropping attempt-local context and tools.
    progress.set_model(model_payload(config, "large"))
    switched = progress.payload() or {}
    assert switched["model"] == model_payload(config, "large")
    assert "context" not in switched
    assert "tools" not in switched
    assert switched["usage"]["total_tokens"] == 1_050


def test_seed_from_paused_preserves_prepared_context_tokens_unit(tmp_path: Path) -> None:
    """seed_from_paused and seed_team_resume_ai_run fall back to persisted prepared_context.tokens when no input usage exists."""
    config = streaming_metadata_config(tmp_path, priced=True)
    prepared_metadata = build_prepared_history_metadata_content(
        PreparedHistoryState(prepared_context_tokens=9_876, native_history=None),
    )
    paused_agent = RunRecord(
        run_id="r-paused",
        session_id="s1",
        status=RunStatus.paused,
        model="gpt-primary",
        model_provider="OpenAI",
        model_name="default",
        metrics=RunMetrics(),
        metadata=prepared_metadata,
        messages=[Message(role="assistant", content="", metrics=MessageMetrics())],
        tools=[ToolExecution(tool_call_id="c1", tool_name="echo")],
    )
    resumed_agent = ReplyAiRunProgress()
    resumed_agent.seed_from_paused(config, model_name="default", persisted=paused_agent)
    assert (resumed_agent.payload() or {})["context"] == {"input_tokens": 9_876, "window_tokens": 100_000}

    paused_team = TeamRunRecord(
        run_id="t-paused",
        session_id="s1",
        team_id="team-1",
        status=RunStatus.paused,
        model="gpt-primary",
        model_provider="OpenAI",
        model_name="default",
        metrics=RunMetrics(),
        metadata=prepared_metadata,
        messages=[Message(role="assistant", content="", metrics=MessageMetrics())],
        tools=[ToolExecution(tool_call_id="c1", tool_name="delegate_task_to_members")],
    )
    resumed_team = ReplyAiRunProgress()
    seed_team_resume_ai_run(
        resumed_team,
        config=config,
        model_name="default",
        persisted=paused_team,
        subagent_cost=SubagentCost(),
    )
    assert (resumed_team.payload() or {})["context"] == {"input_tokens": 9_876, "window_tokens": 100_000}


@pytest.mark.asyncio
@pytest.mark.parametrize("show_tool_calls", [True, False], ids=["tools-shown", "tools-hidden"])
async def test_streaming_blocked_tool_emits_live_usage_and_stop_preserves_it(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    *,
    show_tool_calls: bool,
) -> None:
    """While a tool call blocks, progress edits publish Request #1 usage/cost/context/tools and Stop preserves them."""
    tool_started = asyncio.Event()
    tool_release = asyncio.Event()

    class _SlowTool(Toolkit):
        def __init__(self) -> None:
            super().__init__(name="slow_tools", tools=[self.slow_op])

        async def slow_op(self) -> str:
            tool_started.set()
            await tool_release.wait()
            return "done"

    req1_metrics = MessageMetrics(
        tokens=TokenCounts(input_tokens=1_000, output_tokens=50, total_tokens=1_050),
        time_to_first_token=0.25,
    )
    with registered_toolkit("streaming_live_slow", _SlowTool):
        turn = await setup_turn(
            tmp_path,
            monkeypatch,
            tools=["streaming_live_slow"],
            priced=True,
            scripts={
                "default": [
                    calls(tool_call("slow_op", "c1"), metrics=req1_metrics),
                    answer("Done.", usage(2_000, 100)),
                ],
            },
        )
        turn.bot.config.agents["general"].show_tool_calls = show_tool_calls
        turn.bot.config.background_tool_jobs.enabled = False
        placeholder_model = model_payload(turn.bot.config, "default")
        answered_model = answered_model_payload(turn.bot.config, "default")

        async with running_task(turn.runner.generate_response(_plain_request(_target())), tool_release) as task:
            await asyncio.wait_for(tool_started.wait(), timeout=10)
            live_edits = await wait_for_writes(
                turn,
                lambda w: (
                    stream_status(w) == STREAM_STATUS_STREAMING
                    and "usage" in (ai_run(w) or {})
                    and "tools" in (ai_run(w) or {})
                ),
            )

            placeholder = turn.homeserver.writes[0]
            assert stream_status(placeholder) == STREAM_STATUS_PENDING
            assert_provisional_ai_run(placeholder, placeholder_model)

            live_ai_run = assert_provisional_contract(ai_run(live_edits[-1]))
            assert live_ai_run["model"] == answered_model
            assert live_ai_run["usage"] == {
                "input_tokens": 1_000,
                "output_tokens": 50,
                "total_tokens": 1_050,
                "time_to_first_token": "0.25",
                "uncached_input_tokens": 1_000,
            }
            assert live_ai_run["cost"] == {"usd": "0.0024"}
            assert live_ai_run["context"] == {"input_tokens": 1_000, "window_tokens": 100_000}
            assert live_ai_run["tools"] == {"count": 1}

            await stop_reply(turn, 11)
            await asyncio.wait_for(task, timeout=10)

    cancelled_write = turn.homeserver.writes[-1]
    assert stream_status(cancelled_write) == STREAM_STATUS_CANCELLED
    assert ai_run(cancelled_write) == {
        "version": 1,
        "status": "cancelled",
        "model": answered_model,
        "usage": live_ai_run["usage"],
        "cost": {"usd": "0.0024"},
        "context": {"input_tokens": 1_000, "window_tokens": 100_000},
        "tools": {"count": 1},
    }


@pytest.mark.asyncio
async def test_mid_request_usage_reported_updates_live_edit_without_double_counting(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Mid-request UsageReported(final=False) updates live edits before completion without double-counting."""
    gate = asyncio.Event()
    turn = await setup_turn(
        tmp_path,
        monkeypatch,
        priced=True,
        scripts={
            "default": [
                [
                    UsageReported(usage(1_200, 30), final=False),
                    TextDelta("Streaming "),
                    Await(gate.wait),
                    TextDelta("complete."),
                    UsageReported(usage(1_200, 80), final=True),
                    TurnCompleted(
                        message=Message(role="assistant", content="Streaming complete.", metrics=usage(1_200, 80)),
                        stop_reason="end",
                    ),
                ],
            ],
        },
    )

    async with running_task(turn.runner.generate_response(_plain_request(_target())), gate) as task:
        live_edits = await wait_for_writes(
            turn,
            lambda w: (
                stream_status(w) == STREAM_STATUS_STREAMING and "Streaming" in str(visible_content(w).get("body", ""))
            ),
        )
        live_ai_run = assert_provisional_contract(ai_run(live_edits[-1]))
        assert live_ai_run["model"] == answered_model_payload(turn.bot.config, "default")
        assert live_ai_run["usage"]["input_tokens"] == 1_200
        assert live_ai_run["usage"]["output_tokens"] == 30
        assert live_ai_run["usage"]["total_tokens"] == 1_230
        # 1200 * 2/1e6 + 30 * 8/1e6 = 0.00264
        assert live_ai_run["cost"] == {"usd": "0.00264"}
        assert live_ai_run["context"] == {"input_tokens": 1_200, "window_tokens": 100_000}

        gate.set()
        assert await asyncio.wait_for(task, timeout=10) == Owned()

    final_ai_run = ai_run(turn.homeserver.writes[-1])
    assert final_ai_run is not None
    assert final_ai_run["status"] == "completed"
    # Final counts the single request exactly once (no double-counting from the mid-request projection).
    assert final_ai_run["usage"]["input_tokens"] == 1_200
    assert final_ai_run["usage"]["output_tokens"] == 80
    assert final_ai_run["usage"]["total_tokens"] == 1_280
    # 1200 * 2/1e6 + 80 * 8/1e6 = 0.00304
    assert final_ai_run["cost"] == {"usd": "0.00304"}
    assert final_ai_run["context"] == {"input_tokens": 1_200, "window_tokens": 100_000}


@pytest.mark.asyncio
async def test_mid_stream_stop_captures_cutoff_usage_on_cancelled_note(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Cancelling an in-flight model stream after UsageReported preserves its cut-off billable usage on Stop."""
    usage_seen = asyncio.Event()
    gate = asyncio.Event()

    async def _hold_after_usage() -> None:
        usage_seen.set()
        await gate.wait()

    turn = await setup_turn(
        tmp_path,
        monkeypatch,
        priced=True,
        scripts={
            "default": [
                [
                    TextDelta("Working "),
                    UsageReported(usage(500, 20)),
                    Await(_hold_after_usage),
                    TextDelta("never"),
                    TurnCompleted(
                        message=Message(role="assistant", content="Working never", metrics=usage(500, 20)),
                        stop_reason="end",
                    ),
                ],
            ],
        },
    )
    async with running_task(turn.runner.generate_response(_plain_request(_target())), gate) as task:
        await wait_for_writes(turn, lambda w: stream_status(w) == STREAM_STATUS_STREAMING)
        await asyncio.wait_for(usage_seen.wait(), timeout=10)
        await stop_reply(turn, 12)
        await asyncio.wait_for(task, timeout=10)

    cancelled_write = turn.homeserver.writes[-1]
    assert stream_status(cancelled_write) == STREAM_STATUS_CANCELLED
    cancelled_ai_run = ai_run(cancelled_write)
    assert cancelled_ai_run is not None
    assert cancelled_ai_run["status"] == "cancelled"
    assert "run_id" not in cancelled_ai_run
    assert cancelled_ai_run["usage"]["input_tokens"] == 500
    assert cancelled_ai_run["usage"]["output_tokens"] == 20
    assert cancelled_ai_run["cost"] == {"usd": "0.00116"}


@pytest.mark.asyncio
async def test_model_switch_accumulates_prior_usage_without_double_counting(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Model switch carries Attempt 1's spend onto Attempt 2's live streaming edit and sums without double-counting."""
    switch_gate = asyncio.Event()
    turn_switch = await setup_turn(
        tmp_path / "switch",
        monkeypatch,
        tools=["thread_model"],
        thread_id=THREAD,
        priced=True,
        scripts={
            "default": [
                calls(
                    tool_call("thread_model", "sw-1", action="switch", model_name="large", when="after-toolcall"),
                    content="Switching. ",
                    metrics=usage(1_000, 100),
                ),
            ],
            "large": [gated_answer("On large ", switch_gate, "now.", metrics=usage(2_000, 200))],
        },
    )
    async with running_task(
        turn_switch.runner.generate_response(_plain_request(_target(thread_id=THREAD))),
        switch_gate,
    ) as task:
        switched_live = await wait_for_writes(
            turn_switch,
            lambda w: (
                stream_status(w) == STREAM_STATUS_STREAMING and "On large" in str(visible_content(w).get("body", ""))
            ),
        )
        live_ai_run = assert_provisional_contract(ai_run(switched_live[-1]))
        assert live_ai_run["model"] == model_payload(turn_switch.bot.config, "large")
        assert live_ai_run["usage"]["input_tokens"] == 1_000
        assert live_ai_run["usage"]["output_tokens"] == 100
        assert live_ai_run["cost"] == {"usd": "0.0028"}
        assert "tools" not in live_ai_run
        switch_gate.set()
        assert await asyncio.wait_for(task, timeout=10) == Owned()

    final_switch = ai_run(turn_switch.homeserver.writes[-1])
    assert final_switch is not None
    assert final_switch["status"] == "completed"
    assert final_switch["usage"]["input_tokens"] == 3_000
    assert final_switch["usage"]["output_tokens"] == 300
    assert final_switch["cost"] == {"usd": "0.0178"}


@pytest.mark.asyncio
async def test_delegated_child_updates_parent_subagents_usd_without_clobbering_live_usage(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A delegated child never clobbers the parent's live usage/model while its settled cost joins subagents_usd."""
    parent_gate = asyncio.Event()
    turn_delegate = await setup_turn(
        tmp_path / "delegate",
        monkeypatch,
        priced=True,
        scripts={
            "default": [
                calls(tool_call("run_subagent", "sub-1", agent_name="writer", task="Draft"), metrics=usage(1_000, 50)),
                gated_answer("Parent ", parent_gate, "finished.", metrics=usage(1_500, 100)),
            ],
            "writer-model": [answer("Child draft.", usage(4_000, 200))],
        },
    )
    turn_delegate.bot.config.agents["general"].delegate_to = ["writer"]
    turn_delegate.bot.config.background_tool_jobs.enabled = False
    parent_model = answered_model_payload(turn_delegate.bot.config, "default")

    async with running_task(turn_delegate.runner.generate_response(_plain_request(_target())), parent_gate) as task:
        parent_live = await wait_for_writes(
            turn_delegate,
            lambda w: (
                stream_status(w) == STREAM_STATUS_STREAMING and "Parent" in str(visible_content(w).get("body", ""))
            ),
        )
        live_ai_run = assert_provisional_contract(ai_run(parent_live[-1]))
        # Parent keeps its own model, tokens, and context; child cost appears strictly under subagents_usd.
        assert live_ai_run["model"] == parent_model
        assert live_ai_run["usage"]["input_tokens"] == 1_000
        assert live_ai_run["usage"]["output_tokens"] == 50
        assert live_ai_run["context"] == {"input_tokens": 1_000, "window_tokens": 100_000}
        assert live_ai_run["cost"] == {"usd": "0.0024", "subagents_usd": "0.0048"}
        parent_gate.set()
        assert await asyncio.wait_for(task, timeout=10) == Owned()


@pytest.mark.asyncio
async def test_approval_pause_and_resume_carry_live_usage(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    echo_tool: None,
) -> None:
    """Pause edit carries pre-pause usage; resumed progress edits preserve pre-pause usage and add post-resume usage."""
    del echo_tool
    resume_gate = asyncio.Event()
    turn = await setup_turn(
        tmp_path,
        monkeypatch,
        tools=[ECHO_TOOL],
        priced=True,
        scripts={
            "default": [
                calls(tool_call("echo", "call-1", text="hi"), metrics=usage(1_000, 100)),
                calls(tool_call("echo", "call-2", text="again"), metrics=usage(2_000, 200)),
                gated_answer("All ", resume_gate, "done.", metrics=usage(500, 50)),
            ],
        },
    )
    turn.bot.config.tool_approval = ToolApprovalConfig(
        rules=[ApprovalRuleConfig(match="echo", action="require_approval")],
    )
    turn.bot.config.background_tool_jobs.enabled = False
    install_approval_store(turn)
    try:
        assert await turn.runner.generate_response(_plain_request(_target())) == Owned()
        pause_writes = [w for w in turn.homeserver.writes if stream_status(w) == STREAM_STATUS_APPROVAL_PENDING]
        assert pause_writes
        pause_ai_run = assert_provisional_contract(ai_run(pause_writes[-1]))
        assert pause_ai_run["usage"]["input_tokens"] == 1_000
        assert pause_ai_run["usage"]["output_tokens"] == 100
        assert pause_ai_run["cost"] == {"usd": "0.0028"}
        assert pause_ai_run["context"] == {"input_tokens": 1_000, "window_tokens": 100_000}
        assert pause_ai_run["tools"] == {"count": 1}

        # Clear approval rule before resume so call-2 runs inline during the resumed turn.
        turn.bot.config.tool_approval = ToolApprovalConfig(rules=[])
        updated = await approve_call(turn, "call-1")
        writes_before_resume = len(turn.homeserver.writes)

        # Hold Request #3 at resume_gate until the post-Request-#2 live edit has landed deterministically.
        async with running_task(
            dispatch_approval(turn.runner.approvals, updated, target=_target()),
            resume_gate,
        ) as resume_task:
            after_req2_writes = await wait_for_writes(
                turn,
                lambda w: (
                    stream_status(w) == STREAM_STATUS_STREAMING
                    and (ai_run(w) or {}).get("usage", {}).get("input_tokens") == 3_000
                    and (ai_run(w) or {}).get("tools", {}).get("count") == 2
                ),
                start=writes_before_resume,
            )
            resumed_streaming = [
                w for w in turn.homeserver.writes[writes_before_resume:] if stream_status(w) == STREAM_STATUS_STREAMING
            ]
            assert len(resumed_streaming) >= 2
            first_resumed = assert_provisional_contract(ai_run(resumed_streaming[0]))
            assert first_resumed["usage"]["input_tokens"] == 1_000
            assert first_resumed["usage"]["output_tokens"] == 100
            assert first_resumed["cost"] == {"usd": "0.0028"}
            assert first_resumed["tools"] == {"count": 1}

            after_req2 = assert_provisional_contract(ai_run(after_req2_writes[-1]))
            assert after_req2["usage"]["output_tokens"] == 300
            assert after_req2["cost"] == {"usd": "0.0084"}
            assert after_req2["context"] == {"input_tokens": 2_000, "window_tokens": 100_000}
            assert after_req2["tools"] == {"count": 2}

            resume_gate.set()
            await asyncio.wait_for(resume_task, timeout=10)

        final_ai_run = ai_run(turn.homeserver.writes[-1])
        assert final_ai_run is not None
        assert final_ai_run["status"] == "completed"
        assert final_ai_run["usage"]["input_tokens"] == 3_500
        assert final_ai_run["usage"]["output_tokens"] == 350
    finally:
        await shutdown_approval_runtime()


@pytest.mark.asyncio
async def test_approval_resume_keeps_prepared_context_estimate_without_provider_input_usage(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    echo_tool: None,
) -> None:
    """When Request #1 reports no input usage, both pause and first resumed edits retain the prepared context estimate."""
    del echo_tool
    resume_gate = asyncio.Event()
    turn = await setup_turn(
        tmp_path,
        monkeypatch,
        tools=[ECHO_TOOL],
        priced=True,
        scripts={
            "default": [
                calls(tool_call("echo", "call-1", text="hi"), metrics=MessageMetrics()),
                gated_answer("Resumed ", resume_gate, "ok.", metrics=MessageMetrics()),
            ],
        },
    )
    turn.bot.config.tool_approval = ToolApprovalConfig(
        rules=[ApprovalRuleConfig(match="echo", action="require_approval")],
    )
    turn.bot.config.background_tool_jobs.enabled = False
    install_approval_store(turn)
    try:
        assert await turn.runner.generate_response(_plain_request(_target())) == Owned()
        pause_writes = [w for w in turn.homeserver.writes if stream_status(w) == STREAM_STATUS_APPROVAL_PENDING]
        assert pause_writes
        pause_ai_run = assert_provisional_contract(ai_run(pause_writes[-1]))
        assert "context" in pause_ai_run
        expected_context = pause_ai_run["context"]
        assert expected_context["window_tokens"] == 100_000
        assert expected_context["input_tokens"] > 0

        updated = await approve_call(turn, "call-1")
        writes_before_resume = len(turn.homeserver.writes)
        async with running_task(
            dispatch_approval(turn.runner.approvals, updated, target=_target()),
            resume_gate,
        ) as resume_task:
            resumed_streaming = await wait_for_writes(
                turn,
                lambda w: (
                    stream_status(w) == STREAM_STATUS_STREAMING and "Resumed" in str(visible_content(w).get("body", ""))
                ),
                start=writes_before_resume,
            )
            first_resumed = assert_provisional_contract(ai_run(resumed_streaming[0]))
            assert first_resumed["context"] == expected_context
            assert first_resumed["tools"] == {"count": 1}

            resume_gate.set()
            await asyncio.wait_for(resume_task, timeout=10)
    finally:
        await shutdown_approval_runtime()


@pytest.mark.asyncio
async def test_approval_resume_failure_before_seeding_keeps_paused_metrics_for_same_config_model(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    echo_tool: None,
) -> None:
    """A pre-seeding resume failure on the same configured model ('OpenAI' vs 'openai') keeps context and tools."""
    del echo_tool
    turn = await setup_turn(
        tmp_path,
        monkeypatch,
        tools=[ECHO_TOOL],
        priced=True,
        scripts={"default": [calls(tool_call("echo", "call-1", text="hi"), metrics=usage(1_000, 100))]},
    )
    turn.bot.config.tool_approval = ToolApprovalConfig(
        rules=[ApprovalRuleConfig(match="echo", action="require_approval")],
    )
    turn.bot.config.background_tool_jobs.enabled = False
    install_approval_store(turn)
    try:
        assert await turn.runner.generate_response(_plain_request(_target())) == Owned()
        pause_writes = [w for w in turn.homeserver.writes if stream_status(w) == STREAM_STATUS_APPROVAL_PENDING]
        assert pause_writes
        pause_ai_run = assert_provisional_contract(ai_run(pause_writes[-1]))
        # Runtime answering identity carries canonical "OpenAI", whereas config model_payload carries "openai".
        assert pause_ai_run["model"]["provider"] == "OpenAI"
        assert model_payload(turn.bot.config, "default")["provider"] == "openai"

        updated = await approve_call(turn, "call-1")

        def _fail_create_agent(*_args: object, **_kwargs: object) -> object:
            msg = "tool unavailable during resume preparation"
            raise RuntimeError(msg)

        # Fails after note_reply_model(config, "default") sets live_handle.reply_model ("openai"), before seed_from_paused.
        with patch("mindroom.approval.execution.create_agent", side_effect=_fail_create_agent):
            await dispatch_approval(turn.runner.approvals, updated, target=_target())

        error_write = turn.homeserver.writes[-1]
        assert stream_status(error_write) == STREAM_STATUS_ERROR
        error_ai_run = ai_run(error_write)
        assert error_ai_run is not None
        assert error_ai_run["status"] == "error"
        assert error_ai_run["model"] == answered_model_payload(turn.bot.config, "default")
        assert error_ai_run["usage"] == pause_ai_run["usage"]
        assert error_ai_run["cost"] == {"usd": "0.0028"}
        assert error_ai_run["context"] == {"input_tokens": 1_000, "window_tokens": 100_000}
        assert error_ai_run["tools"] == {"count": 1}
        assert "error" in error_ai_run
    finally:
        await shutdown_approval_runtime()


@pytest.mark.asyncio
async def test_pause_then_stop_without_resume_and_owed_note_preserve_metrics(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    echo_tool: None,
) -> None:
    """Pause -> Stop without resume and owed interruption notes recover provisional metrics from Presentation.ai_run."""
    del echo_tool
    turn = await setup_turn(
        tmp_path,
        monkeypatch,
        tools=[ECHO_TOOL],
        priced=True,
        scripts={"default": [calls(tool_call("echo", "call-1", text="hi"), metrics=usage(1_000, 100))]},
    )
    turn.bot.config.tool_approval = ToolApprovalConfig(
        rules=[ApprovalRuleConfig(match="echo", action="require_approval")],
    )
    turn.bot.config.background_tool_jobs.enabled = False
    install_approval_store(turn)
    try:
        assert await turn.runner.generate_response(_plain_request(_target())) == Owned()
        continuation = await turn.bot.journal_principal().approvals.approval_continuation_for_source(SOURCE)
        assert continuation is not None
        assert await turn.bot._user_stop_reconciler.finalize(continuation.response_event_id, 19, room_id=ROOM)
        gateway = unwrap_extracted_collaborator(turn.bot._delivery_gateway)
        assert await gateway.write_approval_failure_note(
            continuation.response_event_id,
            approval_id=continuation.approval_id,
            note=NoteKind.ERROR,
            text="Approval cancelled.",
            target=_target(),
            model_name=continuation.runtime_model_name,
        )
        cancelled_write = turn.homeserver.writes[-1]
        assert stream_status(cancelled_write) == STREAM_STATUS_CANCELLED
        assert ai_run(cancelled_write) == {
            "version": 1,
            "status": "cancelled",
            "model": answered_model_payload(turn.bot.config, "default"),
            "usage": {
                "input_tokens": 1_000,
                "output_tokens": 100,
                "total_tokens": 1_100,
                "uncached_input_tokens": 1_000,
            },
            "cost": {"usd": "0.0028"},
            "context": {"input_tokens": 1_000, "window_tokens": 100_000},
            "tools": {"count": 1},
        }
    finally:
        await shutdown_approval_runtime()

    # Owed interruption note flushed without a live span also falls back to Presentation.ai_run.
    turn_owed = await setup_turn(
        tmp_path / "owed",
        monkeypatch,
        priced=True,
        scripts={"default": [answer("unused")]},
    )
    saved_ai_run = {
        "version": 1,
        "model": answered_model_payload(turn_owed.bot.config, "default"),
        "usage": {"input_tokens": 600, "output_tokens": 40, "total_tokens": 640, "uncached_input_tokens": 600},
        "cost": {"usd": "0.00152"},
        "context": {"input_tokens": 600, "window_tokens": 100_000},
        "tools": {"count": 1},
    }
    claimed = await turn_owed.bot._reply_runtime.claim(
        sources=ResponseSources((SOURCE,), (SOURCE,)),
        room_id=ROOM,
        thread_id=None,
        placeholder=AGENT_PLACEHOLDER,
        show_tool_calls=True,
    )
    assert isinstance(claimed, SpanHandle)
    await turn_owed.bot.journal_principal().replies.update(
        claimed.reply_id,
        lambda current: rl.Transition(
            outcome=rl.Outcome.APPLIED,
            reply=replace(
                current,
                event_id="$owed-ev",
                state=rl.ReplyState.FAILED,
                presentation=encode_presentation(
                    Presentation(
                        segments=(Segment(kind="answer", text="Partial work", span_id=claimed.span_id),),
                        ai_run=saved_ai_run,
                    ),
                ),
                owed_write=rl.OwedWrite(
                    span_id=claimed.span_id,
                    note=NoteKind.INTERRUPTED,
                    text=None,
                ),
            ),
        ),
    )
    gateway = unwrap_extracted_collaborator(turn_owed.bot._delivery_gateway)
    await gateway._flush_owed_write(claimed.reply_id)
    owed_write = turn_owed.homeserver.writes[-1]
    assert stream_status(owed_write) == STREAM_STATUS_ERROR
    assert ai_run(owed_write) == {**saved_ai_run, "status": "error"}


@pytest.mark.asyncio
async def test_cli_approval_recovery_preserves_checkpointed_attempt_spend_live_and_final(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    echo_tool: None,
) -> None:
    """CLI approval recovery preserves the checkpointed attempt's spend on live resumed edits and the final edit."""
    del echo_tool
    follow_up_gate = asyncio.Event()
    turn = await setup_turn(
        tmp_path,
        monkeypatch,
        tools=["shell", ECHO_TOOL],
        priced=True,
        scripts={"default": [gated_answer("Recovered ", follow_up_gate, "answer.", metrics=usage(2_000, 200))]},
    )
    turn.bot.config.background_tool_jobs.enabled = False
    monkeypatch.setattr("mindroom.agent_cli.response_owner.minimal_shell_problems", lambda *_args: [])
    monkeypatch.setattr(
        "mindroom.agent_cli.response_owner.agent_cli_shell_env",
        lambda _config, _runtime_paths, _agent_name, token: AgentCliShellEnv("http://127.0.0.1:8765", token),
    )
    install_approval_store(turn)
    try:
        # Persist a checkpointed cancelled minimal run with nonzero priced spend.
        session_id = _target().session_id
        checkpoint_metrics = RunMetrics()
        checkpoint_metrics.add(usage(1_000, 100), model_id="gpt-primary", provider="OpenAI")
        persisted = RunRecord(
            run_id="cli-saved-run",
            session_id=session_id,
            agent_id="general",
            status=RunStatus.cancelled,
            model="gpt-primary",
            model_provider="OpenAI",
            model_name="default",
            metrics=checkpoint_metrics,
            metadata={"agent_mode": "minimal"},
            messages=[
                Message(role="user", content="Run hidden echo"),
                Message(
                    role="assistant",
                    content="Checking...",
                    metrics=usage(1_000, 100),
                    meta=MessageMeta(model=ModelProvenance("gpt-primary", "OpenAI")),
                ),
            ],
        )
        storage = create_session_storage("general", turn.bot.config, runtime_paths_for(turn.bot.config), None)
        storage.save_run(
            ConversationSession(session_id=session_id, kind="agent", owner_id="general"),
            persisted,
            record_usage=False,
        )

        echo_args = {"text": "hi"}
        requirement = RunRequirement(
            ToolExecution(
                tool_call_id="hidden-echo",
                tool_name="echo",
                tool_args=echo_args,
                requires_confirmation=True,
            ),
        )

        claimed = await turn.bot._reply_runtime.claim(
            sources=ResponseSources((SOURCE,), (SOURCE,)),
            room_id=ROOM,
            thread_id=None,
            placeholder=AGENT_PLACEHOLDER,
            show_tool_calls=True,
        )
        assert isinstance(claimed, SpanHandle)
        await turn.bot.journal_principal().replies.update(
            claimed.reply_id,
            lambda current: rl.Transition(outcome=rl.Outcome.APPLIED, reply=replace(current, event_id="$cli-paused")),
        )
        paused_reply = await turn.bot.journal_principal().replies.load(claimed.reply_id)
        assert paused_reply is not None
        claimed.note_reply(paused_reply)

        expected_identity = turn.runner.deps.tool_runtime.build_turn_context(
            _target(),
            user_id=REQUESTER,
            agent_name="general",
            active_model_name="default",
            turn_id=None,
        ).execution_identity
        continuation = approval_continuation(
            approval_id="cli-approval-1",
            correlation_id=SOURCE,
            run_id="cli-saved-run",
            session_id=session_id,
            entity_name="general",
            room_id=ROOM,
            thread_id=None,
            requester_id=REQUESTER,
            execution_identity=expected_identity,
            response_event_id="$cli-paused",
            sources=ResponseSources((SOURCE,), (SOURCE,)),
            final_delivery_id=claimed.span.delivery_id,
            runtime_model_name="default",
            request_body="Run hidden echo",
            state="ready",
            calls=(
                ApprovalCall(
                    "hidden-echo",
                    "echo",
                    "general",
                    100,
                    decision=ApprovalDecision.APPROVED,
                    toolkit_name=ECHO_TOOL,
                    arguments_digest=approval_arguments_digest(echo_args),
                ),
            ),
            cli_call={
                "kind": "agent_cli",
                "delegation_depth": 0,
                "toolkit": ECHO_TOOL,
                "function": "echo",
                "arguments": echo_args,
                "call_id": "hidden-echo",
                "parent_bash_call_id": "bash-parent",
                "pending_calls": [requirement.tool_execution.to_dict()],
            },
        )
        gateway = unwrap_extracted_collaborator(turn.bot._delivery_gateway)
        assert await gateway.pause_shown_reply(
            claimed,
            pause_decision(
                claimed,
                Presentation(segments=(Segment(kind="answer", text="Checking..."),)),
                in_place=False,
                stage=None,
            ),
            enqueue=lambda req, row: turn.bot.journal_principal().approvals.pause_for_approval(continuation, req, row),
        )
        stored = await turn.bot.journal_principal().approvals.approval_continuation(continuation.approval_id)
        assert stored is not None

        writes_before = len(turn.homeserver.writes)
        async with running_task(
            dispatch_approval(turn.runner.approvals, stored, target=_target()),
            follow_up_gate,
        ) as task:
            live_edits = await wait_for_writes(
                turn,
                lambda w: (
                    stream_status(w) == STREAM_STATUS_STREAMING
                    and "Recovered" in str(visible_content(w).get("body", ""))
                ),
                start=writes_before,
            )
            live_ai_run = assert_provisional_contract(ai_run(live_edits[-1]))
            # Follow-up streaming edit preserves checkpointed attempt's spend before follow-up request settles.
            assert live_ai_run["usage"]["input_tokens"] == 1_000
            assert live_ai_run["usage"]["output_tokens"] == 100
            assert live_ai_run["cost"] == {"usd": "0.0028"}

            follow_up_gate.set()
            await asyncio.wait_for(task, timeout=10)

        final_ai_run = ai_run(turn.homeserver.writes[-1])
        assert final_ai_run is not None
        assert final_ai_run["status"] == "completed"
        # Final counts checkpoint (1000/100 @ 0.0028) + follow-up (2000/200 @ 0.0056) exactly once.
        assert final_ai_run["usage"]["input_tokens"] == 3_000
        assert final_ai_run["usage"]["output_tokens"] == 300
        assert final_ai_run["cost"] == {"usd": "0.0084"}
    finally:
        await shutdown_approval_runtime()


@pytest.mark.asyncio
async def test_team_streaming_aggregates_leader_and_member_usage_with_leader_context(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Live team progress edits sum leader + active/ended member usage while keeping leader-only model and context."""
    member_tool_started = asyncio.Event()
    member_tool_release = asyncio.Event()
    consensus_gate = asyncio.Event()

    class _MemberSlowTool(Toolkit):
        def __init__(self) -> None:
            super().__init__(name="member_slow_tools", tools=[self.member_step])

        async def member_step(self) -> str:
            member_tool_started.set()
            await member_tool_release.wait()
            return "checked"

    with registered_toolkit("streaming_member_slow", _MemberSlowTool):
        turn = await setup_turn(
            tmp_path,
            monkeypatch,
            priced=True,
            scripts={
                "default": [
                    calls(tool_call("delegate_task_to_members", "del-1", task="Check"), metrics=usage(1_000, 100)),
                    gated_answer("Final synthesis ", consensus_gate, "complete.", metrics=usage(2_000, 200)),
                ],
                "writer-model": [
                    calls(tool_call("member_step", "m1"), metrics=usage(500, 50)),
                    answer("Writer note.", usage(200, 20)),
                ],
                "checker-model": [answer("Checker note.", usage(300, 30))],
            },
        )
        turn.bot.config.agents["writer"].tools = ["streaming_member_slow"]
        turn.bot.config.background_tool_jobs.enabled = False
        leader_model = answered_model_payload(turn.bot.config, "default")

        async with running_task(
            turn.runner.generate_team_response_helper(
                _plain_request(_target()),
                member_names=["writer", "checker"],
                team_mode="collaborate",
            ),
            member_tool_release,
            consensus_gate,
        ) as task:
            # Wait for both writer's Request #1 usage AND its recorded tool batch while member_step blocks.
            await asyncio.wait_for(member_tool_started.wait(), timeout=10)
            mid_member_edits = await wait_for_writes(
                turn,
                lambda w: (
                    stream_status(w) == STREAM_STATUS_STREAMING
                    and (ai_run(w) or {}).get("usage", {}).get("input_tokens", 0) >= 1_500
                    and (ai_run(w) or {}).get("tools", {}).get("count", 0) >= 2
                ),
            )
            mid_ai_run = assert_provisional_contract(ai_run(mid_member_edits[-1]))
            assert mid_ai_run["model"] == leader_model
            assert mid_ai_run["context"] == {"input_tokens": 1_000, "window_tokens": 100_000}
            assert mid_ai_run["tools"]["count"] >= 2

            member_tool_release.set()
            consensus_edits = await wait_for_writes(
                turn,
                lambda w: (
                    stream_status(w) == STREAM_STATUS_STREAMING
                    and "Final synthesis" in str(visible_content(w).get("body", ""))
                ),
            )
            live_ai_run = assert_provisional_contract(ai_run(consensus_edits[-1]))
            assert live_ai_run["model"] == leader_model
            # Context reflects leader's Request #1 (1_000 / 100_000), never a member's window or prompt size.
            assert live_ai_run["context"] == {"input_tokens": 1_000, "window_tokens": 100_000}
            # Leader (1000/100 @ 2/8 = 0.0028) + writer (700/70 @ 1/4 = 0.00098) + checker (300/30 @ 1/4 = 0.00042).
            assert live_ai_run["usage"]["input_tokens"] == 2_000
            assert live_ai_run["usage"]["output_tokens"] == 200
            assert live_ai_run["cost"] == {"usd": "0.0042"}
            assert live_ai_run["tools"] == {"count": 2}
            consensus_gate.set()
            assert await asyncio.wait_for(task, timeout=10) == Owned()


@pytest.mark.asyncio
async def test_team_approval_resume_preserves_pre_pause_member_usage_on_live_and_final_edits(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    echo_tool: None,
) -> None:
    """When Member A finishes before Member B pauses, resumed live edits AND final edit keep Leader + A + B."""
    del echo_tool
    resume_gate = asyncio.Event()

    class _WriterTool(Toolkit):
        def __init__(self) -> None:
            super().__init__(name="writer_tools", tools=[self.writer_step])

        async def writer_step(self) -> str:
            return "drafted"

    with registered_toolkit("streaming_writer_step", _WriterTool):
        turn = await setup_turn(
            tmp_path,
            monkeypatch,
            priced=True,
            scripts={
                "default": [
                    calls(tool_call("delegate_task_to_members", "del-1", task="Work"), metrics=usage(1_000, 100)),
                    gated_answer("Combined ", resume_gate, "result.", metrics=usage(2_000, 200)),
                ],
                "writer-model": [
                    calls(tool_call("writer_step", "w1"), metrics=usage(400, 40)),
                    answer("Writer finished.", usage(200, 20)),
                ],
                "checker-model": [
                    calls(tool_call("echo", "call-1", text="verify"), metrics=usage(300, 30)),
                    answer("Checker approved.", usage(100, 10)),
                ],
            },
        )
        turn.bot.config.agents["writer"].tools = ["streaming_writer_step"]
        turn.bot.config.agents["checker"].tools = [ECHO_TOOL]
        turn.bot.config.tool_approval = ToolApprovalConfig(
            rules=[ApprovalRuleConfig(match="echo", action="require_approval")],
        )
        turn.bot.config.background_tool_jobs.enabled = False
        leader_model = answered_model_payload(turn.bot.config, "default")
        install_approval_store(turn)
        try:
            assert (
                await turn.runner.generate_team_response_helper(
                    _plain_request(_target()),
                    member_names=["writer", "checker"],
                    team_mode="collaborate",
                )
                == Owned()
            )
            pause_writes = [w for w in turn.homeserver.writes if stream_status(w) == STREAM_STATUS_APPROVAL_PENDING]
            assert pause_writes
            pause_ai_run = assert_provisional_contract(ai_run(pause_writes[-1]))
            # Leader (1000/100 @ 0.0028) + writer (600/60 @ 0.00084) + checker pre-pause (300/30 @ 0.00042).
            assert pause_ai_run["usage"]["input_tokens"] == 1_900
            assert pause_ai_run["usage"]["output_tokens"] == 190
            assert pause_ai_run["cost"] == {"usd": "0.00406"}
            assert pause_ai_run["tools"] == {"count": 3}

            updated = await approve_call(turn, "call-1")
            writes_before = len(turn.homeserver.writes)

            async with running_task(
                dispatch_approval(turn.runner.approvals, updated, target=_target()),
                resume_gate,
            ) as resume_task:
                live_edits = await wait_for_writes(
                    turn,
                    lambda w: (
                        stream_status(w) == STREAM_STATUS_STREAMING
                        and "Combined" in str(visible_content(w).get("body", ""))
                    ),
                    start=writes_before,
                )
                live_ai_run = assert_provisional_contract(ai_run(live_edits[-1]))
                assert live_ai_run["model"] == leader_model
                assert live_ai_run["context"] == {"input_tokens": 1_000, "window_tokens": 100_000}
                # Leader Req #1 (1000/100) + writer (600/60) + checker total (400/40) = 2000/200, cost 0.0042.
                assert live_ai_run["usage"]["input_tokens"] == 2_000
                assert live_ai_run["usage"]["output_tokens"] == 200
                assert live_ai_run["cost"] == {"usd": "0.0042"}
                assert live_ai_run["tools"] == {"count": 3}

                resume_gate.set()
                await asyncio.wait_for(resume_task, timeout=10)

            final_write = turn.homeserver.writes[-1]
            assert stream_status(final_write) == STREAM_STATUS_COMPLETED
            # Pre-pause completed member output is not duplicated by handover reconciliation.
            assert str(visible_content(final_write).get("body", "")).count("Writer finished.") == 1
            final_ai_run = ai_run(final_write)
            assert final_ai_run is not None
            assert final_ai_run["status"] == "completed"
            # Leader (3000/300 @ 0.0084) + writer (600/60 @ 0.00084) + checker (400/40 @ 0.00056) = 4000/400, 0.0098.
            assert final_ai_run["usage"]["input_tokens"] == 4_000
            assert final_ai_run["usage"]["output_tokens"] == 400
            assert final_ai_run["cost"] == {"usd": "0.0098"}
            assert final_ai_run["tools"] == {"count": 3}
        finally:
            await shutdown_approval_runtime()


@pytest.mark.asyncio
async def test_fallback_wrapped_endpoint_primary_keeps_measured_context_across_followup_requests(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    echo_tool: None,
) -> None:
    """An endpoint-bearing primary wrapped in fallback_stream keeps measured context across follow-up requests."""
    del echo_tool
    followup_gate = asyncio.Event()
    turn = await setup_turn(
        tmp_path,
        monkeypatch,
        tools=[ECHO_TOOL],
        priced=True,
        scripts={
            "default": [
                calls(tool_call("echo", "c1", text="hi"), metrics=usage(5_200, 100)),
                gated_answer("Follow-up ", followup_gate, "complete.", metrics=usage(6_000, 150)),
            ],
            "large": [answer("unused fallback")],
        },
    )
    turn.bot.config.background_tool_jobs.enabled = False
    base_identity = turn.models["default"].identity
    turn.models["default"].identity = ModelIdentity(
        id=base_identity.id,
        provider=base_identity.provider,
        endpoint="https://api.openai.example/v1",
    )
    fallback_provider = turn.models["large"]
    wrapped_primary = WrappedProvider(
        turn.models["default"],
        partial(
            fallback_stream,
            model_name="default",
            chain=(ChainModel("large", 200_000, lambda: fallback_provider, f"large-{tmp_path.name}"),),
            cooldown_key=f"default-{tmp_path.name}",
        ),
    )
    install_model_providers(monkeypatch, {**turn.models, "default": wrapped_primary})

    async with running_task(turn.runner.generate_response(_plain_request(_target())), followup_gate) as task:
        live_edits = await wait_for_writes(
            turn,
            lambda w: (
                stream_status(w) == STREAM_STATUS_STREAMING and "Follow-up" in str(visible_content(w).get("body", ""))
            ),
        )
        live_ai_run = assert_provisional_contract(ai_run(live_edits[-1]))
        assert live_ai_run["model"] == answered_model_payload(turn.bot.config, "default")
        # Request #2's ModelSelected with endpoint never resets Request #1's measured 5,200 context to turn-start estimate.
        assert live_ai_run["context"] == {"input_tokens": 5_200, "window_tokens": 100_000}
        assert live_ai_run["usage"]["input_tokens"] == 5_200
        assert live_ai_run["usage"]["output_tokens"] == 100
        assert live_ai_run["tools"] == {"count": 1}

        followup_gate.set()
        assert await asyncio.wait_for(task, timeout=10) == Owned()

    final_ai_run = ai_run(turn.homeserver.writes[-1])
    assert final_ai_run is not None
    assert final_ai_run["status"] == "completed"
    assert final_ai_run["usage"]["input_tokens"] == 11_200
    assert final_ai_run["usage"]["output_tokens"] == 250
    assert final_ai_run["context"] == {"input_tokens": 6_000, "window_tokens": 100_000}


@pytest.mark.asyncio
async def test_mid_stream_stop_without_usage_on_followup_request_preserves_prior_measured_context(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    echo_tool: None,
) -> None:
    """Stopping a follow-up request that streamed text before reporting usage keeps Request #1's measured context."""
    del echo_tool
    gate = asyncio.Event()
    turn = await setup_turn(
        tmp_path,
        monkeypatch,
        tools=[ECHO_TOOL],
        priced=True,
        scripts={
            "default": [
                calls(tool_call("echo", "c1", text="hi"), metrics=usage(5_200, 100)),
                [
                    TextDelta("Partial answer "),
                    Await(gate.wait),
                    TextDelta("never"),
                    TurnCompleted(
                        message=Message(role="assistant", content="Partial answer never", metrics=usage(6_000, 120)),
                        stop_reason="end",
                    ),
                ],
            ],
        },
    )
    turn.bot.config.background_tool_jobs.enabled = False

    async with running_task(turn.runner.generate_response(_plain_request(_target())), gate) as task:
        await wait_for_writes(
            turn,
            lambda w: (
                stream_status(w) == STREAM_STATUS_STREAMING
                and "Partial answer" in str(visible_content(w).get("body", ""))
            ),
        )
        await stop_reply(turn, 15)
        await asyncio.wait_for(task, timeout=10)

    cancelled_write = turn.homeserver.writes[-1]
    assert stream_status(cancelled_write) == STREAM_STATUS_CANCELLED
    cancelled_ai_run = ai_run(cancelled_write)
    assert cancelled_ai_run is not None
    assert cancelled_ai_run["status"] == "cancelled"
    assert "run_id" not in cancelled_ai_run
    # Cut-off Request #2 billed no usage, so Request #1's measured 5,200 context is retained instead of turn-start estimate.
    assert cancelled_ai_run["context"] == {"input_tokens": 5_200, "window_tokens": 100_000}
    assert cancelled_ai_run["usage"]["input_tokens"] == 5_200
    assert cancelled_ai_run["usage"]["output_tokens"] == 100
    assert cancelled_ai_run["tools"] == {"count": 1}


@pytest.mark.asyncio
async def test_in_flight_usage_projection_error_does_not_disrupt_stream() -> None:
    """An exception while projecting mid-request usage/model updates is caught and never breaks the model loop."""
    model = ScriptedModel(
        [
            [
                UsageReported(usage(100, 10), final=False),
                TextDelta("Still works."),
                TurnCompleted(
                    message=Message(role="assistant", content="Still works.", metrics=usage(100, 10)),
                    stop_reason="end",
                ),
            ],
        ],
    )
    record = RunRecord(
        run_id="r-guard",
        session_id="s-guard",
        model=model.identity.id,
        model_provider=model.identity.provider,
        metrics=RunMetrics(),
    )
    seen_after_accept: list[int] = []

    def _fail_projection(*_args: object, **_kwargs: object) -> object:
        msg = "synthetic projection failure"
        raise RuntimeError(msg)

    with patch("mindroom.agent_loop.model_loop._in_flight_state", side_effect=_fail_projection):
        events = [
            event
            async for event in run_model_loop(
                model,
                [Message(role="user", content="hi")],
                PreparedTools(functions={}),
                record=record,
                run=RunContext(run_id="r-guard", session=UnstoredSession("s-guard")),
                hooks=LoopHooks(on_usage_updated=lambda st: seen_after_accept.append(st.request_index)),
                max_tool_calls_per_turn=None,
                request_template=EMPTY_REQUEST,
            )
        ]

    assert any(isinstance(e, TextDelta) and e.text == "Still works." for e in events)
    assert record.content == "Still works."
    assert seen_after_accept == [1]


@pytest.mark.asyncio
async def test_failed_progress_edit_does_not_mark_ai_run_delivered_and_live_checkpoint_emits_silent_usage(
    tmp_path: Path,
) -> None:
    """Failed edits keep ai_run uncommitted, live-frame checkpoints emit silent usage, and hidden turns create no event."""
    config = streaming_metadata_config(tmp_path, priced=True)
    progress = ReplyAiRunProgress()
    metrics = RunMetrics()
    metrics.add(usage(1_000, 50), model_id="gpt-primary", provider="OpenAI")
    progress.update_from_run(
        config,
        model_name="default",
        record=RunRecord(
            run_id="r1",
            session_id="s1",
            model="gpt-primary",
            model_provider="OpenAI",
            model_name="default",
            metrics=metrics,
            tools=[ToolExecution(tool_call_id="c1", tool_name="slow_op")],
        ),
        messages=[],
    )

    # Without an existing visible event, has_uncommitted_ai_run stays False so usage never creates a hidden message.
    hidden = StreamingResponse(
        target=_target(),
        config=config,
        runtime_paths=runtime_paths_for(config),
        show_tool_calls=False,
        ai_run_progress=progress,
        **FakeReplyRecords().hooks(),
    )
    assert not hidden.has_uncommitted_ai_run()

    # Adopted placeholder with live frames active and show_tool_calls=False.
    client = make_matrix_client_mock(user_id="@mindroom_general:localhost")
    streaming = StreamingResponse(
        target=_target(),
        config=config,
        runtime_paths=runtime_paths_for(config),
        event_id="$placeholder",
        last_update=100.0,
        update_interval=5.0,
        show_tool_calls=False,
        placeholder_progress_sent=True,
        ai_run_progress=progress,
        **FakeReplyRecords().hooks(),
    )
    streaming._shows_stream = True
    live = LivePublisher(client=client, room_id=ROOM)
    live.active = True
    streaming.live = live
    assert streaming.has_uncommitted_ai_run()

    sent_payloads: list[dict[str, Any]] = []
    outcomes = iter([None, "$edit-ok"])

    async def fake_edit(
        _client: object,
        _room: str,
        _event: str,
        content: dict[str, Any],
        _text: str,
        **_kw: object,
    ) -> str | None:
        sent_payloads.append(content)
        return next(outcomes)

    with (
        patch("mindroom.streaming.edit_message_result", side_effect=fake_edit),
        patch("mindroom.streaming.time.time", side_effect=[106.0, 106.0, 112.0, 112.0]),
    ):
        # First checkpoint edit fails -> version must stay uncommitted.
        await streaming._throttled_send(client)
        assert len(sent_payloads) == 1
        assert streaming.has_uncommitted_ai_run()

        # Second checkpoint edit succeeds -> version is now committed and payload carries live usage & tools.
        await streaming._throttled_send(client)
        assert len(sent_payloads) == 2
        assert not streaming.has_uncommitted_ai_run()
        delivered_ai_run = assert_provisional_contract(ai_run(sent_payloads[-1]))
        assert delivered_ai_run["usage"]["total_tokens"] == 1_050
        assert delivered_ai_run["cost"] == {"usd": "0.0024"}
        assert delivered_ai_run["tools"] == {"count": 1}
