"""Tests for cache-warm fork compaction."""
# ruff: noqa: D103

from __future__ import annotations

import asyncio
from dataclasses import replace
from typing import TYPE_CHECKING

import pytest
from structlog.testing import capture_logs

from mindroom.config.models import CompactionConfig, ModelConfig
from mindroom.history.policy import resolve_history_execution_plan
from mindroom.history.request_size import RequestSize, RequestSizer
from mindroom.history.summary_call import (
    CompactionForkToolCallError,
    _CompactionSummaryTimeoutError,
    request_compaction_summary,
)
from mindroom.llm.errors import MALFORMED_TOOL_CALL, ProviderError
from mindroom.llm.messages import Message, model_provenance
from mindroom.llm.provider import (
    ModelRequest,
    RequestOverrides,
    ToolCallReady,
    TurnCompleted,
    UsageReported,
    complete,
)
from mindroom.llm.usage import MessageMetrics, TokenCounts
from mindroom.providers.pipeline.guidance_retry import guidance_retry_stream
from mindroom.providers.pipeline.wrapped import WrappedProvider
from tests.fakes.scripted_model import Await, ScriptedModel
from tests.fakes.summary_model import summary_steps
from tests.history_helpers import _ALL_HISTORY_SETTINGS, _make_config, stamp_request_identity

if TYPE_CHECKING:
    from pathlib import Path


def _answered(
    model: ScriptedModel,
    request: ModelRequest,
    content: str = "Hello",
    *,
    reported: int = 1_000,
    created_at: int = 1_700_000_000,
) -> Message:
    answer = Message(
        role="assistant",
        content=content,
        created_at=created_at,
        metrics=MessageMetrics(tokens=TokenCounts(input_tokens=reported, output_tokens=10)),
        provider_data=model_provenance(model.identity.id, model.identity.provider),
    )
    stamp_request_identity(answer, model, request)
    return answer


@pytest.mark.asyncio
async def test_guidance_retry_stream_skips_retry_when_caller_retries() -> None:
    malformed = ProviderError("malformed", provider="gemini", code=MALFORMED_TOOL_CALL)

    async def fail_malformed() -> None:
        raise malformed

    inner = ScriptedModel([[Await(fail_malformed)], summary_steps("recovered")])
    wrapped = WrappedProvider(inner, guidance_retry_stream)
    request = ModelRequest(
        messages=(Message(role="user", content="compact"),),
        overrides=RequestOverrides(caller_retries=True),
    )

    with pytest.raises(ProviderError) as exc_info:
        await complete(wrapped, request)

    assert exc_info.value.code == MALFORMED_TOOL_CALL
    assert len(inner.requests) == 1

    # With caller_retries=False (normal replies), guidance retry still runs once.
    retry_inner = ScriptedModel([[Await(fail_malformed)], summary_steps("recovered")])
    retry_wrapped = WrappedProvider(retry_inner, guidance_retry_stream)
    turn = await complete(retry_wrapped, ModelRequest(messages=(Message(role="user", content="hi"),)))
    assert turn.content == "recovered"
    assert len(retry_inner.requests) == 2


def test_request_size_exposes_anchor_index_and_answered_at() -> None:
    model = ScriptedModel([])
    sizer = RequestSizer(model=model, configured_provider="openai")
    first = ModelRequest(messages=(Message(role="system", content="You help."), Message(role="user", content="Hi")))
    answer = _answered(model, first, "Hello", reported=1_200, created_at=1_700_000_123)
    later = replace(first, messages=(*first.messages, answer, Message(role="user", content="More")))

    anchored = sizer.size(later)
    assert anchored.reported is True
    assert anchored.anchor_index == 2
    assert anchored.answered_at == 1_700_000_123.0
    assert anchored == RequestSize(1_200 + model.request_tokens(later, start=2), reported=True)

    unanchored = sizer.size(first)
    assert unanchored.reported is False
    assert unanchored.anchor_index is None
    assert unanchored.answered_at is None


def test_execution_plan_exposes_compaction_context_window(tmp_path: Path) -> None:
    config, _ = _make_config(
        tmp_path,
        models={
            "default": ModelConfig(provider="openai", id="main", context_window=100_000),
            "summary": ModelConfig(provider="openai", id="small", context_window=32_000),
            "no_window": ModelConfig(provider="openai", id="unknown", context_window=None),
        },
    )
    default_plan = resolve_history_execution_plan(
        config=config,
        history_settings=replace(_ALL_HISTORY_SETTINGS, compaction_config=CompactionConfig(enabled=True)),
        active_model_name="default",
        active_context_window=None,
    )
    assert default_plan.compaction_context_window == 100_000

    explicit_plan = resolve_history_execution_plan(
        config=config,
        history_settings=replace(
            _ALL_HISTORY_SETTINGS,
            compaction_config=CompactionConfig(enabled=True, model="summary"),
        ),
        active_model_name="default",
        active_context_window=None,
    )
    assert explicit_plan.compaction_context_window == 32_000

    missing_window_plan = resolve_history_execution_plan(
        config=config,
        history_settings=replace(
            _ALL_HISTORY_SETTINGS,
            compaction_config=CompactionConfig(enabled=True, model="no_window"),
        ),
        active_model_name="default",
        active_context_window=100_000,
    )
    assert missing_window_plan.compaction_context_window is None


@pytest.mark.asyncio
@pytest.mark.parametrize("stop_reason", ["end", "tool_use"])
async def test_request_compaction_summary_rejects_tool_calls_after_billing(stop_reason: str) -> None:
    tool_call = {"id": "call_1", "type": "function", "function": {"name": "search", "arguments": "{}"}}
    metrics = MessageMetrics(tokens=TokenCounts(input_tokens=400, output_tokens=20, cache_read_tokens=300))
    assistant = Message(role="assistant", content="## Goal\nSummary text", tool_calls=[tool_call], metrics=metrics)
    model = ScriptedModel(
        [[UsageReported(metrics), ToolCallReady(tool_call), TurnCompleted(message=assistant, stop_reason=stop_reason)]],
    )
    recorded: list[MessageMetrics] = []

    async def on_usage(usage: MessageMetrics) -> None:
        recorded.append(usage)

    with pytest.raises(CompactionForkToolCallError):
        await request_compaction_summary(
            model=model,
            request=ModelRequest(messages=(Message(role="user", content="compact"),)),
            timeout_seconds=10.0,
            on_usage=on_usage,
            reject_tool_calls=True,
        )

    assert len(recorded) == 1
    assert recorded[0].tokens.cache_read_tokens == 300


@pytest.mark.asyncio
async def test_request_compaction_summary_warns_on_usage_error_and_propagates_timeout_and_cancellation() -> None:
    metrics = MessageMetrics(tokens=TokenCounts(input_tokens=100, output_tokens=10))
    turn = TurnCompleted(message=Message(role="assistant", content="summary", metrics=metrics), stop_reason="end")
    model = ScriptedModel([[UsageReported(metrics), turn]])

    async def broken_usage(_usage: MessageMetrics) -> None:
        msg = "disk full"
        raise OSError(msg)

    with capture_logs() as logs:
        result = await request_compaction_summary(
            model=model,
            request=ModelRequest(messages=(Message(role="user", content="compact"),)),
            timeout_seconds=10.0,
            on_usage=broken_usage,
            reject_tool_calls=True,
        )
    assert result == "summary"
    assert any(entry.get("event") == "compaction_summary_usage_save_failed" for entry in logs)

    async def slow() -> None:
        await asyncio.sleep(0.05)

    slow_model = ScriptedModel([[Await(slow), turn]])
    with pytest.raises(_CompactionSummaryTimeoutError):
        await request_compaction_summary(
            model=slow_model,
            request=ModelRequest(messages=(Message(role="user", content="compact"),)),
            timeout_seconds=0.01,
            on_usage=None,
            reject_tool_calls=True,
        )

    started = asyncio.Event()

    async def wait_forever() -> None:
        started.set()
        await asyncio.Future()

    cancel_model = ScriptedModel([[Await(wait_forever), turn]])
    task = asyncio.create_task(
        request_compaction_summary(
            model=cancel_model,
            request=ModelRequest(messages=(Message(role="user", content="compact"),)),
            timeout_seconds=10.0,
            on_usage=None,
            reject_tool_calls=True,
        ),
    )
    await started.wait()
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
