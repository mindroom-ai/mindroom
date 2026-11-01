"""Tests for compaction summary calls and summary-input construction."""
# ruff: noqa: D103, TC003

from __future__ import annotations

import asyncio
from pathlib import Path
from typing import TYPE_CHECKING
from unittest.mock import patch

import httpx
import pytest
from structlog.testing import capture_logs

import mindroom.background_tasks as background_tasks_module
from mindroom.background_tasks import wait_for_background_tasks
from mindroom.config.models import CompactionOverrideConfig
from mindroom.constants import (
    AI_RUN_METADATA_KEY,
    DEFAULT_COMPACTION_TIMEOUT_SECONDS,
    MATRIX_SOURCE_EVENT_PROMPTS_METADATA_KEY,
    PRIOR_USAGE_RUN_METADATA_KEY,
)
from mindroom.history.storage import (
    read_scope_state,
    with_force_compaction,
)
from mindroom.history.summary_call import (
    SummaryRetryPolicy,
    _CompactionSummaryTimeoutError,
    generate_compaction_summary,
    is_summary_timeout,
)
from mindroom.history.summary_input import _compaction_replay_messages, build_summary_input
from mindroom.llm.media import Image
from mindroom.llm.messages import Message
from mindroom.llm.provider import RequestOverrides
from mindroom.llm.usage import MessageMetrics
from mindroom.prompts import COMPACTION_SUMMARY_PROMPT
from mindroom.providers.errors import classify_provider_error, provider_failure
from mindroom.session_storage.agent_storage import create_session_storage
from mindroom.session_storage.schema import TableNames
from tests.conftest import (
    SETTLE_TIMEOUT_SECONDS,
    install_model_providers,
    prepare_history_for_run_for_test,
    seed_session,
)
from tests.fakes.scripted_model import Await
from tests.fakes.summary_model import summary_model, summary_steps, summary_turn
from tests.fakes.token_counter import quarter_tokens
from tests.history_helpers import (
    _ALL_HISTORY_SETTINGS,
    _completed_run,
    _make_config,
    _session,
    replay_settings,
)
from tests.session_storage.agno_layout import converted_store

if TYPE_CHECKING:
    from mindroom.llm.runs import AnyRunRecord


@pytest.mark.asyncio
async def test_compaction_call_timeout_raises_owned_timeout() -> None:
    async def slow() -> None:
        await asyncio.sleep(0.05)

    with pytest.raises(_CompactionSummaryTimeoutError, match=r"compaction summary timed out after 0.01s"):
        await generate_compaction_summary(
            model=summary_model([Await(slow), *summary_steps("merged summary")]),
            summary_input="Current prompt",
            summary_prompt=COMPACTION_SUMMARY_PROMPT,
            timeout_seconds=0.01,
            on_usage=None,
        )


@pytest.mark.asyncio
async def test_a_summary_whose_provider_reported_no_usage_records_none() -> None:
    """A response that is timed but carries no counters reported no usage, so the summary records none."""
    recorded: list[MessageMetrics] = []

    async def on_usage(usage: MessageMetrics) -> None:
        recorded.append(usage)

    await generate_compaction_summary(
        model=summary_model(summary_turn("merged summary", usage=MessageMetrics(duration=0.4))),
        summary_input="Current prompt",
        summary_prompt=COMPACTION_SUMMARY_PROMPT,
        timeout_seconds=DEFAULT_COMPACTION_TIMEOUT_SECONDS,
        on_usage=on_usage,
    )

    assert recorded == []


@pytest.mark.asyncio
async def test_the_summary_request_is_one_call_without_provider_retries_a_thinking_setting_or_cache() -> None:
    """The request is sent once, without tools, leaving the transport timeout to the route."""
    model = summary_model()

    await generate_compaction_summary(
        model=model,
        summary_input="Current prompt",
        summary_prompt=COMPACTION_SUMMARY_PROMPT,
        timeout_seconds=42.0,
        on_usage=None,
    )

    [request] = model.requests
    assert request.tools == ()
    assert request.tool_choice is None
    assert request.overrides == RequestOverrides(caller_retries=True, prompt_cache=False, omit_thinking_setting=True)


class _ProviderCall:
    """A summary reply that blocks until cancelled, recording what the provider request saw."""

    def __init__(self) -> None:
        self.started = asyncio.Event()
        self.cancelled = asyncio.Event()
        self.finished = asyncio.Event()
        self.release = asyncio.Event()
        self.task: asyncio.Task[object] | None = None
        # The task awaiting the summary call, and whether it had already returned when the cancellation reached here.
        self.caller: asyncio.Task[str] | None = None
        self.caller_done_when_cancelled = False

    async def swallowing_cancel(self) -> None:
        self.started.set()
        try:
            await asyncio.sleep(10)
        except asyncio.CancelledError:
            self.cancelled.set()
            await self.release.wait()
            return
        finally:
            self.finished.set()
        raise AssertionError

    async def blocking(self) -> None:
        self.task = asyncio.current_task()
        self.started.set()
        try:
            await asyncio.Event().wait()
        except asyncio.CancelledError:
            self.cancelled.set()
            raise
        raise AssertionError

    async def slow_cancel_cleanup(self) -> None:
        self.started.set()
        try:
            await asyncio.Event().wait()
        except asyncio.CancelledError:
            self.caller_done_when_cancelled = self.caller is not None and self.caller.done()
            self.cancelled.set()
            await self.release.wait()
            raise
        finally:
            self.finished.set()
        raise AssertionError

    async def failing_cleanup(self) -> None:
        self.task = asyncio.current_task()
        self.started.set()
        try:
            await asyncio.Event().wait()
        except asyncio.CancelledError:
            self.cancelled.set()
            msg = "provider cleanup failed"
            raise RuntimeError(msg) from None
        raise AssertionError


@pytest.mark.asyncio
async def test_compaction_call_timeout_returns_without_waiting_for_cancellation_cleanup() -> None:
    call = _ProviderCall()
    summary_task = asyncio.create_task(
        generate_compaction_summary(
            model=summary_model([Await(call.slow_cancel_cleanup)]),
            summary_input="Current prompt",
            summary_prompt=COMPACTION_SUMMARY_PROMPT,
            timeout_seconds=0.01,
            on_usage=None,
        ),
    )
    call.caller = summary_task

    try:
        await asyncio.wait_for(call.cancelled.wait(), timeout=SETTLE_TIMEOUT_SECONDS)
        # Only a call that waited, however briefly, lets the cancellation reach the request before it returns.
        assert call.caller_done_when_cancelled, "the call waited for its cancelled request"
        with pytest.raises(_CompactionSummaryTimeoutError, match=r"compaction summary timed out after 0.01s"):
            summary_task.result()
    finally:
        call.release.set()
    await asyncio.wait_for(call.finished.wait(), timeout=SETTLE_TIMEOUT_SECONDS)


@pytest.mark.asyncio
async def test_compaction_call_timeout_raises_even_when_provider_returns_after_cancel() -> None:
    call = _ProviderCall()

    with pytest.raises(_CompactionSummaryTimeoutError, match=r"compaction summary timed out after 0.01s"):
        await generate_compaction_summary(
            model=summary_model([Await(call.swallowing_cancel)]),
            summary_input="Current prompt",
            summary_prompt=COMPACTION_SUMMARY_PROMPT,
            timeout_seconds=0.01,
            on_usage=None,
        )

    await asyncio.wait_for(call.started.wait(), timeout=0.1)
    await asyncio.wait_for(call.cancelled.wait(), timeout=0.1)
    assert not call.finished.is_set()
    call.release.set()
    await asyncio.wait_for(call.finished.wait(), timeout=0.2)


@pytest.mark.asyncio
async def test_compaction_provider_timeout_propagates_unchanged() -> None:
    with pytest.raises(TimeoutError, match="provider timeout"):
        await generate_compaction_summary(
            model=summary_model(TimeoutError("provider timeout")),
            summary_input="Current prompt",
            summary_prompt=COMPACTION_SUMMARY_PROMPT,
            timeout_seconds=DEFAULT_COMPACTION_TIMEOUT_SECONDS,
            on_usage=None,
        )


@pytest.mark.asyncio
async def test_compaction_summary_cancels_model_task_when_outer_call_is_cancelled() -> None:
    call = _ProviderCall()
    summary_task = asyncio.create_task(
        generate_compaction_summary(
            model=summary_model([Await(call.blocking)]),
            summary_input="Current prompt",
            summary_prompt=COMPACTION_SUMMARY_PROMPT,
            timeout_seconds=DEFAULT_COMPACTION_TIMEOUT_SECONDS,
            on_usage=None,
        ),
    )

    await asyncio.wait_for(call.started.wait(), timeout=0.1)
    summary_task.cancel()

    with pytest.raises(asyncio.CancelledError):
        await asyncio.wait_for(summary_task, timeout=0.1)

    await asyncio.wait_for(call.cancelled.wait(), timeout=0.1)
    assert call.task is not None
    assert call.task.done() is True
    assert call.task.cancelled() is True


@pytest.mark.asyncio
async def test_compaction_summary_outer_cancellation_returns_without_waiting_for_provider_cleanup() -> None:
    call = _ProviderCall()
    summary_task = asyncio.create_task(
        generate_compaction_summary(
            model=summary_model([Await(call.slow_cancel_cleanup)]),
            summary_input="Current prompt",
            summary_prompt=COMPACTION_SUMMARY_PROMPT,
            timeout_seconds=DEFAULT_COMPACTION_TIMEOUT_SECONDS,
            on_usage=None,
        ),
    )
    call.caller = summary_task

    try:
        await asyncio.wait_for(call.started.wait(), timeout=SETTLE_TIMEOUT_SECONDS)
        summary_task.cancel()
        await asyncio.wait_for(call.cancelled.wait(), timeout=SETTLE_TIMEOUT_SECONDS)
        # Only a call that waited, however briefly, lets the cancellation reach the request before it returns.
        assert call.caller_done_when_cancelled, "the cancelled call waited for its cancelled request"
        assert summary_task.cancelled()
    finally:
        call.release.set()
    await asyncio.wait_for(call.finished.wait(), timeout=SETTLE_TIMEOUT_SECONDS)


@pytest.mark.asyncio
async def test_compaction_summary_outer_cancellation_wins_over_provider_cleanup_error() -> None:
    call = _ProviderCall()
    summary_task = asyncio.create_task(
        generate_compaction_summary(
            model=summary_model([Await(call.failing_cleanup)]),
            summary_input="Current prompt",
            summary_prompt=COMPACTION_SUMMARY_PROMPT,
            timeout_seconds=DEFAULT_COMPACTION_TIMEOUT_SECONDS,
            on_usage=None,
        ),
    )

    await asyncio.wait_for(call.started.wait(), timeout=0.1)
    summary_task.cancel()

    with pytest.raises(asyncio.CancelledError):
        await asyncio.wait_for(summary_task, timeout=0.1)

    await asyncio.wait_for(call.cancelled.wait(), timeout=0.1)
    assert call.task is not None
    assert call.task.done() is True
    with pytest.raises(RuntimeError, match="provider cleanup failed"):
        call.task.result()


@pytest.mark.asyncio
async def test_compaction_timeout_cleanup_detaches_after_grace_window() -> None:
    call = _ProviderCall()

    with (
        patch("mindroom.history.summary_call._COMPACTION_CANCEL_DRAIN_TIMEOUT_SECONDS", 0.01),
        pytest.raises(_CompactionSummaryTimeoutError, match=r"compaction summary timed out after 0.01s"),
    ):
        await generate_compaction_summary(
            model=summary_model([Await(call.swallowing_cancel)]),
            summary_input="Current prompt",
            summary_prompt=COMPACTION_SUMMARY_PROMPT,
            timeout_seconds=0.01,
            on_usage=None,
        )

    await asyncio.wait_for(call.started.wait(), timeout=0.1)
    await asyncio.wait_for(call.cancelled.wait(), timeout=0.1)
    await asyncio.sleep(0)
    assert not background_tasks_module._background_tasks
    call.release.set()
    await asyncio.wait_for(call.finished.wait(), timeout=0.2)
    await wait_for_background_tasks(timeout=0.1)


@pytest.mark.asyncio
async def test_compaction_call_timeout_falls_back_in_runtime(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    async def slow() -> None:
        await asyncio.sleep(0.05)

    config, runtime_paths = _make_config(
        tmp_path,
        compaction=CompactionOverrideConfig(
            enabled=True,
            timeout_seconds=0.01,
        ),
        context_window=64_000,
    )
    storage = create_session_storage("test_agent", config, runtime_paths, execution_identity=None)
    session = _session(
        "session-1",
        runs=[
            _completed_run("run-1"),
            _completed_run("run-2"),
            _completed_run("run-3"),
            _completed_run("run-4"),
        ],
    )
    session.metadata = with_force_compaction(session.metadata)
    seed_session(storage, session)

    install_model_providers(monkeypatch, {"default": summary_model([Await(slow), *summary_steps("merged summary")])})
    with capture_logs() as logs:
        prepared = await prepare_history_for_run_for_test(
            agent_name="test_agent",
            full_prompt="Current prompt",
            session_id="session-1",
            runtime_paths=runtime_paths,
            config=config,
            execution_identity=None,
            storage=storage,
            session=session,
        )
        await wait_for_background_tasks(timeout=0.2)

    persisted = storage.load_session("session-1")
    assert persisted is not None
    assert persisted.summary is None
    assert len(persisted.runs) == 4
    assert read_scope_state(persisted).force_compact_before_next_run is False
    assert prepared.compaction_reply_outcome == "timeout"
    events = [entry["event"] for entry in logs]
    assert "Compaction failed; continuing without compaction" in events
    assert "Timed-out compaction request" not in events


def test_build_summary_input_advances_past_oversized_oldest_run() -> None:
    big_run = _completed_run(
        "run-big",
        messages=[
            Message(role="user", content="u" * 800),
            Message(role="assistant", content="a" * 800),
        ],
    )
    small_run = _completed_run("run-small")

    summary_input, included_runs = build_summary_input(
        previous_summary=None,
        compacted_runs=[big_run, small_run],
        max_input_tokens=220,
        history_settings=_ALL_HISTORY_SETTINGS,
        token_estimator=quarter_tokens,
    )

    assert [run.run_id for run in included_runs] == ["run-big"]
    assert "Run truncated to fit compaction budget." in summary_input
    assert 'run_id="run-big"' in summary_input


def test_build_summary_input_oversized_run_preserves_messages_before_omitted_metadata() -> None:
    root_request = "Look into how the automatic memory flush in mindroom is supposed to work."
    run = _completed_run(
        "run-big-metadata",
        messages=[
            Message(role="user", content=root_request),
            Message(role="assistant", content="I will investigate."),
        ],
    )
    run.metadata = {
        "matrix_event_id": "$root",
        "thread_id": "$root",
        MATRIX_SOURCE_EVENT_PROMPTS_METADATA_KEY: {f"$event_{index}": "x" * 2000 for index in range(30)},
    }

    summary_input, included_runs = build_summary_input(
        previous_summary=None,
        compacted_runs=[run],
        max_input_tokens=280,
        history_settings=_ALL_HISTORY_SETTINGS,
        token_estimator=quarter_tokens,
    )

    assert [included_run.run_id for included_run in included_runs] == ["run-big-metadata"]
    assert root_request in summary_input
    assert MATRIX_SOURCE_EVENT_PROMPTS_METADATA_KEY not in summary_input


def test_build_summary_input_oversized_run_omits_empty_filtered_metadata() -> None:
    run = _completed_run(
        "run-big-bulky-metadata",
        messages=[
            Message(role="user", content="u" * 800),
            Message(role="assistant", content="a" * 800),
        ],
    )
    run.metadata = {
        MATRIX_SOURCE_EVENT_PROMPTS_METADATA_KEY: {"$request": "x" * 2_000},
        "model_params": {"temperature": 0.2},
    }

    summary_input, included_runs = build_summary_input(
        previous_summary=None,
        compacted_runs=[run],
        max_input_tokens=220,
        history_settings=_ALL_HISTORY_SETTINGS,
        token_estimator=quarter_tokens,
    )

    assert included_runs == [run]
    assert "Run truncated to fit compaction budget." in summary_input
    assert "<run_metadata>" not in summary_input


def test_build_summary_input_normal_run_omits_empty_filtered_metadata() -> None:
    run = _completed_run("run-bulky-metadata")
    run.metadata = {
        MATRIX_SOURCE_EVENT_PROMPTS_METADATA_KEY: {"$request": "x" * 1_000},
        "model_params": {"temperature": 0.2},
    }

    summary_input, included_runs = build_summary_input(
        previous_summary=None,
        compacted_runs=[run],
        max_input_tokens=10_000,
        history_settings=_ALL_HISTORY_SETTINGS,
        token_estimator=quarter_tokens,
    )

    assert included_runs == [run]
    assert "<run_metadata>" not in summary_input


def test_build_summary_input_normal_run_omits_non_summary_metadata() -> None:
    run = _completed_run(
        "run-normal-metadata",
        messages=[
            Message(role="user", content="Look up the deployment outcome."),
            Message(
                role="assistant",
                content="I will inspect it.",
                tool_calls=[
                    {
                        "id": "call-deployment",
                        "type": "function",
                        "function": {"name": "deployment_status", "arguments": '{"deployment_id":"deploy-1"}'},
                    },
                ],
            ),
            Message(
                role="tool",
                content='{"state":"succeeded"}',
                tool_call_id="call-deployment",
                images=[Image(url="https://example.test/deployment.png")],
            ),
            Message(role="assistant", content="The deployment succeeded."),
        ],
    )
    metadata = {
        AI_RUN_METADATA_KEY: {"compaction": {"decision": "required"}},
        MATRIX_SOURCE_EVENT_PROMPTS_METADATA_KEY: {"$request": "Look up the deployment outcome."},
        "matrix_event_id": "$request",
        "started_at": "2026-07-17T20:00:00Z",
        "durable_outcome": {"state": "delivered"},
        "model_params": {"temperature": 0.2},
        PRIOR_USAGE_RUN_METADATA_KEY: {"input_tokens": 100, "output_tokens": 5},
    }
    run.metadata = metadata.copy()

    summary_input, included_runs = build_summary_input(
        previous_summary=None,
        compacted_runs=[run],
        max_input_tokens=10_000,
        history_settings=_ALL_HISTORY_SETTINGS,
        token_estimator=quarter_tokens,
    )

    assert included_runs == [run]
    assert run.metadata == metadata
    assert "model_params" not in summary_input
    assert PRIOR_USAGE_RUN_METADATA_KEY not in summary_input
    assert AI_RUN_METADATA_KEY not in summary_input
    assert MATRIX_SOURCE_EVENT_PROMPTS_METADATA_KEY not in summary_input
    assert "$request" in summary_input
    assert "2026-07-17T20:00:00Z" in summary_input
    assert "durable_outcome" in summary_input
    assert "Look up the deployment outcome." in summary_input
    assert "call-deployment" in summary_input
    assert "deployment_status" in summary_input
    assert '{"state":"succeeded"}' in summary_input
    assert "The deployment succeeded." in summary_input
    assert "https://example.test/deployment.png" in summary_input


def test_build_summary_input_preserves_complete_near_cap_summary_without_claiming_progress() -> None:
    run = _completed_run("run-1")
    previous_summary = ("word " * 975) + "TAIL-FACT-MUST-SURVIVE"

    summary_input, included_runs = build_summary_input(
        previous_summary=previous_summary,
        compacted_runs=[run],
        max_input_tokens=1_001,
        history_settings=_ALL_HISTORY_SETTINGS,
        token_estimator=quarter_tokens,
    )

    assert included_runs == []
    assert "<previous_summary>" in summary_input
    assert previous_summary in summary_input
    assert "TAIL-FACT-MUST-SURVIVE" in summary_input
    assert "<new_conversation>" not in summary_input


def test_build_summary_input_returns_no_progress_when_run_envelope_cannot_fit() -> None:
    summary_input, included_runs = build_summary_input(
        previous_summary=None,
        compacted_runs=[_completed_run("run-1")],
        max_input_tokens=1,
        history_settings=_ALL_HISTORY_SETTINGS,
        token_estimator=quarter_tokens,
    )

    assert summary_input == ""
    assert included_runs == []


def test_build_summary_input_preserves_previous_summary_text() -> None:
    run = _completed_run("run-1")

    summary_input, included_runs = build_summary_input(
        previous_summary="Useful prior conversation.\n\n## Your Identity\nIDENTITY.md\nCurrent Date and Time",
        compacted_runs=[run],
        max_input_tokens=1_000,
        history_settings=_ALL_HISTORY_SETTINGS,
        token_estimator=quarter_tokens,
    )

    assert included_runs == [run]
    assert "<previous_summary>" in summary_input
    assert "Useful prior conversation" in summary_input
    assert "IDENTITY.md" in summary_input
    assert "Current Date and Time" in summary_input
    assert "run-1 question" in summary_input
    assert "run-1 answer" in summary_input


def test_build_summary_input_renders_a_block_list_one_block_per_line() -> None:
    """A message stored as content blocks reaches the summary input with each block serialized on its own line."""
    blocks = [{"type": "text", "text": "first block"}, {"type": "text", "text": "second block"}]
    run = _completed_run("run-1", messages=[Message(role="user", content=blocks)])

    summary_input, _ = build_summary_input(
        previous_summary=None,
        compacted_runs=[run],
        max_input_tokens=1_000,
        history_settings=_ALL_HISTORY_SETTINGS,
        token_estimator=quarter_tokens,
    )

    assert '{"text":"first block","type":"text"}\n{"text":"second block","type":"text"}' in summary_input


def _converted_run(tmp_path: Path, messages: list[Message]) -> AnyRunRecord:
    """Return ``run-1`` holding ``messages`` as an earlier release stored it, once its database converts."""
    stored = _completed_run("run-1", messages=messages).to_dict()
    run = converted_store(TableNames("code").database_path(tmp_path), stored).get_run("run-1")
    assert run is not None
    return run


def test_compaction_replay_messages_exclude_legacy_persisted_prompt_roles(tmp_path: Path) -> None:
    """A run an earlier release stored with its system prompt converts without it, so compaction never reads it."""
    run = _converted_run(
        tmp_path,
        [
            Message(role="system", content="legacy system prompt"),
            Message(role="user", content="user request"),
            Message(role="assistant", content="assistant answer"),
            Message(role="tool", content="tool result"),
        ],
    )

    replay_messages = _compaction_replay_messages(
        run,
        replay_settings(),
    )

    assert [(message.role, message.content) for message in replay_messages] == [
        ("user", "user request"),
        ("assistant", "assistant answer"),
        ("tool", "tool result"),
    ]


def test_build_summary_input_excludes_legacy_persisted_prompt_roles(tmp_path: Path) -> None:
    """A run an earlier release stored with its system prompt converts without it, so no summary input holds it."""
    run = _converted_run(
        tmp_path,
        [
            Message(role="system", content="Persisted system prompt that should not be summarized"),
            Message(role="user", content="user request"),
            Message(role="assistant", content="assistant answer"),
            Message(role="tool", content="tool result"),
        ],
    )

    summary_input, included_runs = build_summary_input(
        previous_summary=None,
        compacted_runs=[run],
        history_settings=replay_settings(),
        max_input_tokens=1_000,
        token_estimator=quarter_tokens,
    )

    assert included_runs == [run]
    assert "Persisted system prompt" not in summary_input
    assert "user request" in summary_input
    assert "assistant answer" in summary_input
    assert "tool result" in summary_input


def test_build_summary_input_honors_tool_call_history_limit() -> None:
    run = _completed_run(
        "run-1",
        messages=[
            Message(role="user", content="use tools"),
            Message(
                role="assistant",
                content="first tool",
                tool_calls=[{"id": "call-1", "type": "function", "function": {"name": "first", "arguments": "{}"}}],
            ),
            Message(role="tool", content="first result", tool_call_id="call-1"),
            Message(
                role="assistant",
                content="second tool",
                tool_calls=[{"id": "call-2", "type": "function", "function": {"name": "second", "arguments": "{}"}}],
            ),
            Message(role="tool", content="second result", tool_call_id="call-2"),
        ],
    )

    summary_input, included_runs = build_summary_input(
        previous_summary=None,
        compacted_runs=[run],
        history_settings=replay_settings(max_tool_calls_from_history=1),
        max_input_tokens=1_000,
        token_estimator=quarter_tokens,
    )

    assert included_runs == [run]
    assert "call-1" not in summary_input
    assert "first result" not in summary_input
    assert "call-2" in summary_input
    assert "second result" in summary_input


def test_owned_timeout_classification_does_not_depend_on_message_text() -> None:
    """Renaming an engine deadline message cannot change retry or lifecycle decisions."""
    error = _CompactionSummaryTimeoutError("deadline expired")
    assert SummaryRetryPolicy().should_shrink(error)
    assert is_summary_timeout(error)


def test_a_compaction_failure_is_a_timeout_by_its_type_alone() -> None:
    """A transport timeout counts by MindRoom's marker, and no error by its wording or the code a provider sent."""
    timed_out = provider_failure(httpx.PoolTimeout(""), provider="fake")
    assert timed_out is not None
    assert is_summary_timeout(timed_out)
    assert not is_summary_timeout(
        classify_provider_error("upstream request timed out", provider="fake", status_code=504),
    )
    assert not is_summary_timeout(classify_provider_error("busy", provider="fake", status_code=500, code="timeout"))
    refused = provider_failure(httpx.ConnectError("connection refused"), provider="fake")
    assert refused is not None
    assert not is_summary_timeout(refused)
    assert not is_summary_timeout(RuntimeError("lock wait timed out"))


def test_build_summary_input_oversized_run_keeps_the_call_its_result_answers() -> None:
    """A tool result cut short still names the call and arguments that produced it."""
    run = _completed_run(
        "run-big-result",
        messages=[
            Message(
                role="assistant",
                tool_calls=[
                    {
                        "id": "c1",
                        "type": "function",
                        "function": {"name": "read_file", "arguments": '{"path": "big_a.py"}'},
                    },
                ],
            ),
            Message(role="tool", tool_call_id="c1", content="x" * 4_000),
        ],
    )

    summary_input, included_runs = build_summary_input(
        previous_summary=None,
        compacted_runs=[run],
        max_input_tokens=300,
        history_settings=_ALL_HISTORY_SETTINGS,
        token_estimator=quarter_tokens,
    )

    assert [included_run.run_id for included_run in included_runs] == ["run-big-result"]
    assert "Run truncated to fit compaction budget." in summary_input
    assert "big_a.py" in summary_input
    # A mid-turn fold summarizes a result apart from its call message; its own arguments still name the file.
    result_only = _completed_run(
        "run-big-lone-result",
        messages=[
            Message(role="tool", tool_name="read_file", tool_args={"path": "big_b.py"}, content="y" * 4_000),
        ],
    )

    summary_input, _included = build_summary_input(
        previous_summary=None,
        compacted_runs=[result_only],
        max_input_tokens=300,
        history_settings=_ALL_HISTORY_SETTINGS,
        token_estimator=quarter_tokens,
    )

    assert "Run truncated to fit compaction budget." in summary_input
    assert "big_b.py" in summary_input


def test_each_tool_result_names_its_own_call_in_the_summary_input() -> None:
    """Results read correctly apart from their call message, even without call ids or in a different order."""
    run = _completed_run(
        "run-batch",
        messages=[
            Message(
                role="assistant",
                tool_calls=[
                    {"type": "function", "function": {"name": "write_file", "arguments": '{"path": "a.txt"}'}},
                    {"type": "function", "function": {"name": "missing_tool", "arguments": '{"path": "b.txt"}'}},
                ],
            ),
            # Agno appends the error for an unavailable tool before the results of the calls it runs.
            Message(role="tool", tool_name="missing_tool", content="Error: The requested tool does not exist."),
            Message(
                role="tool",
                tool_name="write_file",
                tool_args={"content": "x" * 2_000, "path": "a.txt"},
                content="Wrote a.txt",
            ),
        ],
    )

    summary_input, _included = build_summary_input(
        previous_summary=None,
        compacted_runs=[run],
        max_input_tokens=100_000,
        history_settings=_ALL_HISTORY_SETTINGS,
        token_estimator=quarter_tokens,
    )

    assert '<message role="tool" tool_name="missing_tool">\nError: The requested tool does not exist.' in summary_input
    write_result = summary_input.index('<message role="tool" tool_name="write_file">\nWrote a.txt')
    args = summary_input[summary_input.rindex("<tool_args>", 0, write_result) : write_result]
    assert "a.txt" in args
    assert "x" * 300 not in args


@pytest.mark.parametrize("max_input_tokens", [100_000, 300], ids=["whole", "excerpt"])
def test_a_failed_tool_result_is_marked_in_the_summary_input(max_input_tokens: int) -> None:
    """A summary can tell a call that failed from one that returned, whether its run is summarized whole or cut short."""
    run = _completed_run(
        "run-failed-call",
        messages=[
            Message(
                role="tool",
                tool_call_id="c1",
                tool_name="shell",
                content="make: *** No rule to make target 'deploy'.",
                tool_call_error=True,
            ),
            Message(role="tool", tool_call_id="c2", tool_name="shell", content="y" * 4_000, tool_call_error=False),
        ],
    )

    summary_input, _included = build_summary_input(
        previous_summary=None,
        compacted_runs=[run],
        max_input_tokens=max_input_tokens,
        history_settings=_ALL_HISTORY_SETTINGS,
        token_estimator=quarter_tokens,
    )

    assert '<message role="tool" tool_call_id="c1" tool_name="shell" error="true">\nmake: *** No rule' in summary_input
    assert '<message role="tool" tool_call_id="c2" tool_name="shell">\nyyy' in summary_input
