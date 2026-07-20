"""Tests for compaction summary calls and summary-input construction."""
# ruff: noqa: D103, TC003

from __future__ import annotations

import asyncio
from pathlib import Path
from unittest.mock import patch

import pytest
from agno.exceptions import ContextWindowExceededError, ModelProviderError, ModelRateLimitError
from agno.media import Image
from agno.models.message import Message
from agno.models.response import ModelResponse
from structlog.testing import capture_logs

import mindroom.background_tasks as background_tasks_module
from mindroom.agent_storage import create_session_storage, get_agent_session
from mindroom.background_tasks import wait_for_background_tasks
from mindroom.config.models import CompactionOverrideConfig
from mindroom.history.compaction import (
    _build_summary_input,
    _compaction_replay_messages,
)
from mindroom.history.storage import (
    read_scope_state,
    write_scope_state,
)
from mindroom.history.summary_call import generate_compaction_summary, is_context_window_rejection
from mindroom.history.types import (
    HistoryPolicy,
    HistoryScope,
    HistoryScopeState,
    ResolvedHistorySettings,
)
from mindroom.prompts import COMPACTION_SUMMARY_PROMPT
from mindroom.token_budget import approximate_o200k_tokens
from tests.conftest import (
    FakeModel,
    prepare_history_for_run_for_test,
)
from tests.history_helpers import (  # noqa: F401
    _ALL_HISTORY_SETTINGS,
    RecordingModel,
    _agent,
    _close_test_storages,
    _completed_run,
    _make_config,
    _session,
)


@pytest.mark.asyncio
async def test_compaction_call_timeout_raises_runtime_error() -> None:
    class _SlowSummaryModel(FakeModel):
        async def aresponse(self, *_args: object, **_kwargs: object) -> ModelResponse:
            await asyncio.sleep(0.05)
            return ModelResponse(content="merged summary")

    with (
        patch("mindroom.history.summary_call.MINDROOM_COMPACTION_CHUNK_TIMEOUT_SECONDS", 0.01),
        pytest.raises(RuntimeError, match=r"compaction summary timed out after 0.01s"),
    ):
        await generate_compaction_summary(
            model=_SlowSummaryModel(id="summary-model", provider="fake"),
            summary_input="Current prompt",
            summary_prompt=COMPACTION_SUMMARY_PROMPT,
        )


@pytest.mark.asyncio
async def test_compaction_summary_uses_configured_system_prompt() -> None:
    """Compaction summaries should use the configured prompt text."""
    model = RecordingModel(id="summary-model", provider="fake")

    await generate_compaction_summary(
        model=model,
        summary_input="Current prompt",
        summary_prompt="Custom compaction instructions.",
    )

    assert model.seen_messages[0].role == "system"
    assert model.seen_messages[0].content == "Custom compaction instructions."


@pytest.mark.asyncio
async def test_compaction_call_timeout_returns_without_waiting_for_cancellation_cleanup() -> None:
    class _SlowToUnwindSummaryModel(FakeModel):
        def __init__(self, *, model_id: str, provider: str) -> None:
            super().__init__(id=model_id, provider=provider)
            self.started = asyncio.Event()
            self.cancelled = asyncio.Event()
            self.finished = asyncio.Event()

        async def aresponse(self, *_args: object, **_kwargs: object) -> ModelResponse:
            self.started.set()
            try:
                await asyncio.sleep(10)
            except asyncio.CancelledError:
                self.cancelled.set()
                await asyncio.sleep(0.05)
                raise
            finally:
                self.finished.set()
            raise AssertionError

    model = _SlowToUnwindSummaryModel(model_id="summary-model", provider="fake")
    start = asyncio.get_running_loop().time()

    with (
        patch("mindroom.history.summary_call.MINDROOM_COMPACTION_CHUNK_TIMEOUT_SECONDS", 0.01),
        pytest.raises(RuntimeError, match=r"compaction summary timed out after 0.01s"),
    ):
        await generate_compaction_summary(
            model=model,
            summary_input="Current prompt",
            summary_prompt=COMPACTION_SUMMARY_PROMPT,
        )

    assert asyncio.get_running_loop().time() - start < 0.04
    await asyncio.wait_for(model.started.wait(), timeout=0.1)
    await asyncio.wait_for(model.cancelled.wait(), timeout=0.1)
    await asyncio.wait_for(model.finished.wait(), timeout=0.2)


@pytest.mark.asyncio
async def test_compaction_call_timeout_raises_even_when_provider_returns_after_cancel() -> None:
    class _SwallowingCancelSummaryModel(FakeModel):
        def __init__(self, *, model_id: str, provider: str) -> None:
            super().__init__(id=model_id, provider=provider)
            self.started = asyncio.Event()
            self.cancelled = asyncio.Event()
            self.finished = asyncio.Event()
            self.release_after_cancel = asyncio.Event()

        async def aresponse(self, *_args: object, **_kwargs: object) -> ModelResponse:
            self.started.set()
            try:
                await asyncio.sleep(10)
            except asyncio.CancelledError:
                self.cancelled.set()
                await self.release_after_cancel.wait()
                return ModelResponse(content="merged summary")
            finally:
                self.finished.set()
            raise AssertionError

    model = _SwallowingCancelSummaryModel(model_id="summary-model", provider="fake")

    with (
        patch("mindroom.history.summary_call.MINDROOM_COMPACTION_CHUNK_TIMEOUT_SECONDS", 0.01),
        pytest.raises(RuntimeError, match=r"compaction summary timed out after 0.01s"),
    ):
        await generate_compaction_summary(
            model=model,
            summary_input="Current prompt",
            summary_prompt=COMPACTION_SUMMARY_PROMPT,
        )

    await asyncio.wait_for(model.started.wait(), timeout=0.1)
    await asyncio.wait_for(model.cancelled.wait(), timeout=0.1)
    assert not model.finished.is_set()
    model.release_after_cancel.set()
    await asyncio.wait_for(model.finished.wait(), timeout=0.2)


@pytest.mark.asyncio
async def test_compaction_provider_timeout_propagates_unchanged() -> None:
    class _ProviderTimeoutModel(FakeModel):
        async def aresponse(self, *_args: object, **_kwargs: object) -> ModelResponse:
            msg = "provider timeout"
            raise TimeoutError(msg)

    with pytest.raises(TimeoutError, match="provider timeout"):
        await generate_compaction_summary(
            model=_ProviderTimeoutModel(id="summary-model", provider="fake"),
            summary_input="Current prompt",
            summary_prompt=COMPACTION_SUMMARY_PROMPT,
        )


@pytest.mark.asyncio
async def test_compaction_summary_cancels_model_task_when_outer_call_is_cancelled() -> None:
    class _BlockingSummaryModel(FakeModel):
        def __init__(self, *, model_id: str, provider: str) -> None:
            super().__init__(id=model_id, provider=provider)
            self.started = asyncio.Event()
            self.cancelled = asyncio.Event()
            self.response_task: asyncio.Task[object] | None = None

        async def aresponse(self, *_args: object, **_kwargs: object) -> ModelResponse:
            self.response_task = asyncio.current_task()
            self.started.set()
            try:
                await asyncio.Event().wait()
            except asyncio.CancelledError:
                self.cancelled.set()
                raise
            raise AssertionError

    model = _BlockingSummaryModel(model_id="summary-model", provider="fake")
    summary_task = asyncio.create_task(
        generate_compaction_summary(
            model=model,
            summary_input="Current prompt",
            summary_prompt=COMPACTION_SUMMARY_PROMPT,
        ),
    )

    await asyncio.wait_for(model.started.wait(), timeout=0.1)
    summary_task.cancel()

    with pytest.raises(asyncio.CancelledError):
        await asyncio.wait_for(summary_task, timeout=0.1)

    await asyncio.wait_for(model.cancelled.wait(), timeout=0.1)
    assert model.response_task is not None
    assert model.response_task.done() is True
    assert model.response_task.cancelled() is True


@pytest.mark.asyncio
async def test_compaction_summary_outer_cancellation_returns_without_waiting_for_provider_cleanup() -> None:
    class _SlowCancelCleanupSummaryModel(FakeModel):
        def __init__(self, *, model_id: str, provider: str) -> None:
            super().__init__(id=model_id, provider=provider)
            self.started = asyncio.Event()
            self.cancelled = asyncio.Event()
            self.finished = asyncio.Event()

        async def aresponse(self, *_args: object, **_kwargs: object) -> ModelResponse:
            self.started.set()
            try:
                await asyncio.Event().wait()
            except asyncio.CancelledError:
                self.cancelled.set()
                await asyncio.sleep(0.05)
                raise
            finally:
                self.finished.set()
            raise AssertionError

    model = _SlowCancelCleanupSummaryModel(model_id="summary-model", provider="fake")
    summary_task = asyncio.create_task(
        generate_compaction_summary(
            model=model,
            summary_input="Current prompt",
            summary_prompt=COMPACTION_SUMMARY_PROMPT,
        ),
    )

    await asyncio.wait_for(model.started.wait(), timeout=0.1)
    summary_task.cancel()
    start = asyncio.get_running_loop().time()

    with pytest.raises(asyncio.CancelledError):
        await asyncio.wait_for(summary_task, timeout=0.02)

    assert asyncio.get_running_loop().time() - start < 0.04
    await asyncio.wait_for(model.cancelled.wait(), timeout=0.1)
    await asyncio.wait_for(model.finished.wait(), timeout=0.2)


@pytest.mark.asyncio
async def test_compaction_summary_outer_cancellation_wins_over_provider_cleanup_error() -> None:
    class _CleanupErrorSummaryModel(FakeModel):
        def __init__(self, *, model_id: str, provider: str) -> None:
            super().__init__(id=model_id, provider=provider)
            self.started = asyncio.Event()
            self.cancelled = asyncio.Event()
            self.response_task: asyncio.Task[object] | None = None

        async def aresponse(self, *_args: object, **_kwargs: object) -> ModelResponse:
            self.response_task = asyncio.current_task()
            self.started.set()
            try:
                await asyncio.Event().wait()
            except asyncio.CancelledError:
                self.cancelled.set()
                msg = "provider cleanup failed"
                raise RuntimeError(msg) from None
            raise AssertionError

    model = _CleanupErrorSummaryModel(model_id="summary-model", provider="fake")
    summary_task = asyncio.create_task(
        generate_compaction_summary(
            model=model,
            summary_input="Current prompt",
            summary_prompt=COMPACTION_SUMMARY_PROMPT,
        ),
    )

    await asyncio.wait_for(model.started.wait(), timeout=0.1)
    summary_task.cancel()

    with pytest.raises(asyncio.CancelledError):
        await asyncio.wait_for(summary_task, timeout=0.1)

    await asyncio.wait_for(model.cancelled.wait(), timeout=0.1)
    assert model.response_task is not None
    assert model.response_task.done() is True
    with pytest.raises(RuntimeError, match="provider cleanup failed"):
        model.response_task.result()


@pytest.mark.asyncio
async def test_compaction_timeout_cleanup_detaches_after_grace_window() -> None:
    class _DetachedTimeoutCleanupSummaryModel(FakeModel):
        def __init__(self, *, model_id: str, provider: str) -> None:
            super().__init__(id=model_id, provider=provider)
            self.started = asyncio.Event()
            self.cancelled = asyncio.Event()
            self.release_cleanup = asyncio.Event()
            self.finished = asyncio.Event()

        async def aresponse(self, *_args: object, **_kwargs: object) -> ModelResponse:
            self.started.set()
            try:
                await asyncio.sleep(10)
            except asyncio.CancelledError:
                self.cancelled.set()
                await self.release_cleanup.wait()
                return ModelResponse(content="merged summary")
            finally:
                self.finished.set()
            raise AssertionError

    model = _DetachedTimeoutCleanupSummaryModel(model_id="summary-model", provider="fake")

    with (
        patch("mindroom.history.summary_call.MINDROOM_COMPACTION_CHUNK_TIMEOUT_SECONDS", 0.01),
        patch("mindroom.history.summary_call._COMPACTION_CANCEL_DRAIN_TIMEOUT_SECONDS", 0.01),
        pytest.raises(RuntimeError, match=r"compaction summary timed out after 0.01s"),
    ):
        await generate_compaction_summary(
            model=model,
            summary_input="Current prompt",
            summary_prompt=COMPACTION_SUMMARY_PROMPT,
        )

    await asyncio.wait_for(model.started.wait(), timeout=0.1)
    await asyncio.wait_for(model.cancelled.wait(), timeout=0.1)
    await asyncio.sleep(0)
    assert not background_tasks_module._background_tasks
    model.release_cleanup.set()
    await asyncio.wait_for(model.finished.wait(), timeout=0.2)
    await wait_for_background_tasks(timeout=0.1)


@pytest.mark.asyncio
async def test_compaction_call_timeout_falls_back_in_runtime(
    tmp_path: Path,
) -> None:
    class _SlowSummaryModel(FakeModel):
        async def aresponse(self, *_args: object, **_kwargs: object) -> ModelResponse:
            await asyncio.sleep(0.05)
            return ModelResponse(content="merged summary")

    config, runtime_paths = _make_config(
        tmp_path,
        compaction=CompactionOverrideConfig(enabled=True),
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
    scope = HistoryScope(kind="agent", scope_id="test_agent")
    write_scope_state(session, scope, HistoryScopeState(force_compact_before_next_run=True))
    storage.upsert_session(session)

    with (
        patch(
            "mindroom.model_loading.get_model_instance",
            return_value=_SlowSummaryModel(id="summary-model", provider="fake"),
        ),
        patch("mindroom.history.summary_call.MINDROOM_COMPACTION_CHUNK_TIMEOUT_SECONDS", 0.01),
        capture_logs() as logs,
    ):
        prepared = await prepare_history_for_run_for_test(
            agent=_agent(db=storage),
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

    persisted = get_agent_session(storage, "session-1")
    assert persisted is not None
    assert persisted.summary is None
    assert len(persisted.runs) == 4
    assert read_scope_state(persisted, scope).force_compact_before_next_run is False
    assert prepared.compaction_outcomes == []
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

    summary_input, included_runs = _build_summary_input(
        previous_summary=None,
        compacted_runs=[big_run, small_run],
        max_input_tokens=220,
        history_settings=_ALL_HISTORY_SETTINGS,
        token_estimator=approximate_o200k_tokens,
    )

    assert [run.run_id for run in included_runs] == ["run-big"]
    assert "Run truncated to fit compaction budget." in summary_input
    assert 'run_id="run-big"' in summary_input


def test_build_summary_input_oversized_run_preserves_messages_before_tool_schema() -> None:
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
        "tools_schema": [{"name": f"tool_{index}", "description": "x" * 2000} for index in range(30)],
    }

    summary_input, included_runs = _build_summary_input(
        previous_summary=None,
        compacted_runs=[run],
        max_input_tokens=280,
        history_settings=_ALL_HISTORY_SETTINGS,
        token_estimator=approximate_o200k_tokens,
    )

    assert [included_run.run_id for included_run in included_runs] == ["run-big-metadata"]
    assert root_request in summary_input
    assert "tools_schema" not in summary_input


def test_build_summary_input_oversized_run_omits_empty_filtered_metadata() -> None:
    run = _completed_run(
        "run-big-bulky-metadata",
        messages=[
            Message(role="user", content="u" * 800),
            Message(role="assistant", content="a" * 800),
        ],
    )
    run.metadata = {
        "tools_schema": [{"name": "deployment_status", "description": "x" * 2_000}],
        "model_params": {"temperature": 0.2},
    }

    summary_input, included_runs = _build_summary_input(
        previous_summary=None,
        compacted_runs=[run],
        max_input_tokens=220,
        history_settings=_ALL_HISTORY_SETTINGS,
        token_estimator=approximate_o200k_tokens,
    )

    assert included_runs == [run]
    assert "Run truncated to fit compaction budget." in summary_input
    assert "<run_metadata>" not in summary_input


def test_build_summary_input_normal_run_omits_empty_filtered_metadata() -> None:
    run = _completed_run("run-bulky-metadata")
    run.metadata = {
        "tools_schema": [{"name": "deployment_status", "description": "x" * 1_000}],
        "model_params": {"temperature": 0.2},
    }

    summary_input, included_runs = _build_summary_input(
        previous_summary=None,
        compacted_runs=[run],
        max_input_tokens=10_000,
        history_settings=_ALL_HISTORY_SETTINGS,
        token_estimator=approximate_o200k_tokens,
    )

    assert included_runs == [run]
    assert "<run_metadata>" not in summary_input


def test_build_summary_input_normal_run_omits_only_bulky_metadata() -> None:
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
        "matrix_event_id": "$request",
        "started_at": "2026-07-17T20:00:00Z",
        "durable_outcome": {"state": "delivered"},
        "tools_schema": [{"name": "deployment_status", "description": "x" * 1_000}],
        "model_params": {"temperature": 0.2},
    }
    run.metadata = metadata.copy()

    summary_input, included_runs = _build_summary_input(
        previous_summary=None,
        compacted_runs=[run],
        max_input_tokens=10_000,
        history_settings=_ALL_HISTORY_SETTINGS,
        token_estimator=approximate_o200k_tokens,
    )

    assert included_runs == [run]
    assert run.metadata == metadata
    assert "tools_schema" not in summary_input
    assert "model_params" not in summary_input
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

    summary_input, included_runs = _build_summary_input(
        previous_summary=previous_summary,
        compacted_runs=[run],
        max_input_tokens=1_001,
        history_settings=_ALL_HISTORY_SETTINGS,
        token_estimator=approximate_o200k_tokens,
    )

    assert included_runs == []
    assert "<previous_summary>" in summary_input
    assert previous_summary in summary_input
    assert "TAIL-FACT-MUST-SURVIVE" in summary_input
    assert "<new_conversation>" not in summary_input


def test_build_summary_input_returns_no_progress_when_run_envelope_cannot_fit() -> None:
    summary_input, included_runs = _build_summary_input(
        previous_summary=None,
        compacted_runs=[_completed_run("run-1")],
        max_input_tokens=1,
        history_settings=_ALL_HISTORY_SETTINGS,
        token_estimator=approximate_o200k_tokens,
    )

    assert summary_input == ""
    assert included_runs == []


def test_build_summary_input_preserves_previous_summary_text() -> None:
    run = _completed_run("run-1")

    summary_input, included_runs = _build_summary_input(
        previous_summary="Useful prior conversation.\n\n## Your Identity\nIDENTITY.md\nCurrent Date and Time",
        compacted_runs=[run],
        max_input_tokens=1_000,
        history_settings=_ALL_HISTORY_SETTINGS,
        token_estimator=approximate_o200k_tokens,
    )

    assert included_runs == [run]
    assert "<previous_summary>" in summary_input
    assert "Useful prior conversation" in summary_input
    assert "IDENTITY.md" in summary_input
    assert "Current Date and Time" in summary_input
    assert "run-1 question" in summary_input
    assert "run-1 answer" in summary_input


def test_compaction_replay_messages_exclude_legacy_persisted_prompt_roles() -> None:
    run = _completed_run(
        "run-1",
        messages=[
            Message(role="system", content="legacy system prompt"),
            Message(role="developer", content="legacy developer prompt"),
            Message(role="instructions", content="legacy custom prompt"),
            Message(role="user", content="user request"),
            Message(role="assistant", content="assistant answer"),
            Message(role="tool", content="tool result"),
        ],
    )

    replay_messages = _compaction_replay_messages(
        run,
        ResolvedHistorySettings(
            policy=HistoryPolicy(mode="all"),
            max_tool_calls_from_history=None,
            system_message_role="instructions",
        ),
    )

    assert [(message.role, message.content) for message in replay_messages] == [
        ("user", "user request"),
        ("assistant", "assistant answer"),
        ("tool", "tool result"),
    ]


def test_build_summary_input_excludes_legacy_persisted_prompt_roles() -> None:
    run = _completed_run(
        "run-1",
        messages=[
            Message(role="system", content="Persisted system prompt that should not be summarized"),
            Message(role="developer", content="Persisted developer prompt that should not be summarized"),
            Message(role="instructions", content="Persisted custom prompt that should not be summarized"),
            Message(role="user", content="user request"),
            Message(role="assistant", content="assistant answer"),
            Message(role="tool", content="tool result"),
        ],
    )

    summary_input, included_runs = _build_summary_input(
        previous_summary=None,
        compacted_runs=[run],
        history_settings=ResolvedHistorySettings(
            policy=HistoryPolicy(mode="all"),
            max_tool_calls_from_history=None,
            system_message_role="instructions",
        ),
        max_input_tokens=1_000,
        token_estimator=approximate_o200k_tokens,
    )

    assert included_runs == [run]
    assert "Persisted system prompt" not in summary_input
    assert "Persisted developer prompt" not in summary_input
    assert "Persisted custom prompt" not in summary_input
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

    summary_input, included_runs = _build_summary_input(
        previous_summary=None,
        compacted_runs=[run],
        history_settings=ResolvedHistorySettings(
            policy=HistoryPolicy(mode="all"),
            max_tool_calls_from_history=1,
        ),
        max_input_tokens=1_000,
        token_estimator=approximate_o200k_tokens,
    )

    assert included_runs == [run]
    assert "call-1" not in summary_input
    assert "first result" not in summary_input
    assert "call-2" in summary_input
    assert "second result" in summary_input


# --- ISSUE-246 review round 7 G3: the durable context-window classifier is narrow ---


@pytest.mark.parametrize(
    ("error", "expected"),
    [
        pytest.param(
            ModelRateLimitError(
                "Request too large for gpt-4o in organization org-x on tokens per min (TPM): "
                "Limit 30000, Requested 62051. Visit platform.openai.com to learn more.",
                status_code=429,
            ),
            False,
            id="tpm-rate-limit-is-never-terminal",
        ),
        pytest.param(
            ModelProviderError("max_tokens is too large: 300000 > 128000", status_code=400),
            False,
            id="output-max-tokens-validation-not-terminal",
        ),
        pytest.param(
            ModelProviderError("413 Request Entity Too Large", status_code=413),
            False,
            id="proxy-body-size-not-terminal",
        ),
        pytest.param(
            ModelProviderError("upstream timeout while checking maximum context length", status_code=503),
            False,
            id="transient-5xx-wins-over-fragment",
        ),
        pytest.param(
            ModelProviderError("request too large", status_code=400),
            False,
            id="broad-shrink-fragment-alone-not-terminal",
        ),
        pytest.param(
            ContextWindowExceededError("prompt is too long for this model"),
            True,
            id="typed-context-window-error",
        ),
        pytest.param(
            ModelProviderError(
                "Input validation failed: prompt exceeds the maximum context length of this model",
                status_code=400,
            ),
            True,
            id="explicit-input-context-phrase",
        ),
        pytest.param(
            ModelProviderError("error code: context_length_exceeded", status_code=400),
            True,
            id="provider-input-context-error-code",
        ),
        pytest.param(
            ModelProviderError("prompt is too long: 210000 tokens > 200000 maximum"),
            True,
            id="unclassified-default-status-with-explicit-phrase",
        ),
    ],
)
def test_is_context_window_rejection_is_narrow(error: Exception, expected: bool) -> None:
    """Round-7 G3: only typed errors and explicit input-context wording are terminal.

    Transient shapes (rate-limit types, retryable status codes) classify
    before any message matching, so TPM 429s, output-cap validation, proxy
    body-size limits, and 5xx never mint a durable wrong diagnosis.
    """
    assert is_context_window_rejection(error) is expected
