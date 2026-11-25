"""Tests for cache-warm fork compaction."""
# ruff: noqa: D103

from __future__ import annotations

import asyncio
from dataclasses import replace
from datetime import UTC, datetime
from typing import TYPE_CHECKING

import pytest
from pydantic import BaseModel
from structlog.testing import capture_logs

from mindroom.config.models import CompactionConfig, ModelConfig
from mindroom.history.policy import resolve_history_execution_plan
from mindroom.history.request_size import RequestSize, RequestSizer
from mindroom.history.summary_call import (
    CompactionForkToolCallError,
    _CompactionSummaryTimeoutError,
    request_compaction_summary,
)
from mindroom.history.summary_fork import (
    ScopeForkSource,
    SummaryFork,
    build_scope_fork,
    clear_fork_frames,
    holds_fold_basis,
    lookup_fork_frame,
    prepare_fork_request,
    remember_fork_frame,
    warm_cache_ttl_seconds,
)
from mindroom.history.summary_message import compaction_summary_message
from mindroom.history.types import ScopeSummary
from mindroom.hooks.enrichment import is_transient_context
from mindroom.llm.errors import MALFORMED_TOOL_CALL, ProviderError
from mindroom.llm.media import Image
from mindroom.llm.messages import Message, model_provenance
from mindroom.llm.provider import (
    ModelIdentity,
    ModelRequest,
    NativeCompactionReplay,
    RequestOverrides,
    ToolCallReady,
    ToolSpec,
    TurnCompleted,
    UsageReported,
    complete,
)
from mindroom.llm.usage import MessageMetrics, TokenCounts
from mindroom.providers.pipeline.guidance_retry import guidance_retry_stream
from mindroom.providers.pipeline.wrapped import WrappedProvider
from tests.fakes.scripted_model import Await, ScriptedModel
from tests.fakes.summary_model import summary_steps
from tests.history_helpers import (
    _ALL_HISTORY_SETTINGS,
    TEST_AGENT_SCOPE,
    _completed_run,
    _make_config,
    stamp_request_identity,
)

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


class _StructuredAnswer(BaseModel):
    answer: str


def _warm_fork(
    *,
    model: ScriptedModel | None = None,
    created_at: int = 1_700_000_000,
    reported: int = 1_000,
    context_window: int = 100_000,
    reserve_tokens: int = 4_096,
    mid_turn: bool = False,
    **request_kwargs: object,
) -> tuple[ScriptedModel, SummaryFork]:
    active = model or ScriptedModel([])
    tool = ToolSpec(name="search", description="Search", parameters={"type": "object"}, strict=None)
    base_kwargs = {"tools": (tool,), **request_kwargs}
    first = ModelRequest(
        messages=(Message(role="system", content="You help."), Message(role="user", content="Hi")),
        **base_kwargs,  # type: ignore[arg-type]
    )
    answer = _answered(active, first, "Hello", reported=reported, created_at=created_at)
    live = replace(first, messages=(*first.messages, answer))
    fork = SummaryFork(
        sizer=RequestSizer(model=active, configured_provider="openai"),
        request=live,
        context_window=context_window,
        reserve_tokens=reserve_tokens,
        mid_turn=mid_turn,
    )
    return active, fork


def test_warm_cache_ttl_seconds() -> None:
    assert warm_cache_ttl_seconds({"extended_cache_time": True, "cache_system_prompt": True}) == 3300.0
    assert warm_cache_ttl_seconds({"extended_cache_time": True, "cache_system_prompt": False}) == 300.0
    assert warm_cache_ttl_seconds({"extended_cache_time": False, "cache_system_prompt": True}) == 300.0
    assert warm_cache_ttl_seconds({}) == 300.0


def test_prepare_fork_request_happy_path_preserves_prefix_and_pins_hold(tmp_path: Path) -> None:
    hold_path = tmp_path / "hold.json"
    model, fork = _warm_fork(prompt_hold=hold_path, retry_budget=object())

    fork_request, skip_reason = prepare_fork_request(
        fork,
        summary_prompt="CUSTOM COMPACTION PROMPT",
        summary_model_identity=model.identity,
        now=1_700_000_100.0,
    )

    assert skip_reason is None
    assert fork_request is not None
    assert fork_request.tools == fork.request.tools
    assert fork_request.messages[:-1] == fork.request.messages
    assert fork_request.overrides.caller_retries is True
    assert fork_request.overrides.prompt_cache is True
    assert fork_request.prompt_hold == hold_path
    assert fork_request.prompt_hold_pin == []
    assert fork_request.retry_budget is None
    appended = fork_request.messages[-1]
    assert appended.role == "user"
    assert is_transient_context(appended.content)
    assert "CUSTOM COMPACTION PROMPT" in str(appended.content)


def test_prepare_fork_request_gates() -> None:
    model, fork = _warm_fork()

    # 1. Different summary model
    assert prepare_fork_request(
        fork,
        summary_prompt="Summarize",
        summary_model_identity=ModelIdentity(id="other", provider="Synthetic"),
        now=1_700_000_050.0,
    ) == (None, "different_model")

    # 2. Unsupported claude_code provider
    cc_model = ScriptedModel([], identity=ModelIdentity(id="claude", provider="claude_code"))
    _, cc_fork = _warm_fork(model=cc_model)
    assert prepare_fork_request(
        cc_fork,
        summary_prompt="Summarize",
        summary_model_identity=cc_model.identity,
        now=1_700_000_050.0,
    ) == (None, "unsupported_provider")

    # 3. native_compaction, output_schema, tool_choice
    _, native_fork = _warm_fork(native_compaction=NativeCompactionReplay(route="r", threshold=50_000))
    assert prepare_fork_request(
        native_fork,
        summary_prompt="Summarize",
        summary_model_identity=native_fork.sizer.model.identity,
        now=1_700_000_050.0,
    ) == (None, "native_compaction_active")

    _, schema_fork = _warm_fork(output_schema=_StructuredAnswer)
    assert prepare_fork_request(
        schema_fork,
        summary_prompt="Summarize",
        summary_model_identity=schema_fork.sizer.model.identity,
        now=1_700_000_050.0,
    ) == (None, "output_schema_set")

    _, choice_fork = _warm_fork(tool_choice="none")
    assert prepare_fork_request(
        choice_fork,
        summary_prompt="Summarize",
        summary_model_identity=choice_fork.sizer.model.identity,
        now=1_700_000_050.0,
    ) == (None, "tool_choice_set")

    # 4. No assistant anchor
    no_assistant = replace(fork, request=replace(fork.request, messages=fork.request.messages[:2]))
    assert prepare_fork_request(
        no_assistant,
        summary_prompt="Summarize",
        summary_model_identity=model.identity,
        now=1_700_000_050.0,
    ) == (None, "no_assistant_anchor")

    # 5. Anchored on older assistant message, NOT the newest assistant message
    older_anchored = replace(
        fork,
        request=replace(
            fork.request,
            messages=(
                *fork.request.messages,
                Message(role="user", content="Follow up"),
                Message(role="assistant", content="Unanchored newest reply"),
            ),
        ),
    )
    assert prepare_fork_request(
        older_anchored,
        summary_prompt="Summarize",
        summary_model_identity=model.identity,
        now=1_700_000_050.0,
    ) == (None, "prefix_not_anchored")

    # 6. Standard 300s TTL vs 3300s extended TTL + future timestamp
    assert prepare_fork_request(
        fork,
        summary_prompt="Summarize",
        summary_model_identity=model.identity,
        now=1_700_000_300.0,
    ) == (None, "cache_cold")
    assert prepare_fork_request(
        fork,
        summary_prompt="Summarize",
        summary_model_identity=model.identity,
        now=1_699_999_999.0,
    ) == (None, "cache_cold")

    extended_model = ScriptedModel(
        [],
        model_params={"extended_cache_time": True, "cache_system_prompt": True},
    )
    _, ext_fork = _warm_fork(model=extended_model)
    req_ext, reason_ext = prepare_fork_request(
        ext_fork,
        summary_prompt="Summarize",
        summary_model_identity=extended_model.identity,
        now=1_700_003_299.0,
    )
    assert req_ext is not None
    assert reason_ext is None
    assert prepare_fork_request(
        ext_fork,
        summary_prompt="Summarize",
        summary_model_identity=extended_model.identity,
        now=1_700_003_300.0,
    ) == (None, "cache_cold")

    # 7. Real context-window fit guard
    _, tight_fork = _warm_fork(reported=95_000, context_window=100_000, reserve_tokens=4_096)
    assert prepare_fork_request(
        tight_fork,
        summary_prompt="Summarize",
        summary_model_identity=tight_fork.sizer.model.identity,
        now=1_700_000_050.0,
    ) == (None, "exceeds_context_window")


def test_holds_fold_basis_checks_summary_runs_content_and_digests() -> None:
    run1 = _completed_run(
        "r1",
        messages=[Message(role="user", content="hello"), Message(role="assistant", content="world")],
    )
    run2 = _completed_run(
        "r2",
        messages=[Message(role="user", content="next"), Message(role="assistant", content="done")],
    )
    summary_msg = compaction_summary_message("Prior facts", run_local=False)
    history = [
        summary_msg,
        replace(run1.messages[0], from_history=True),
        replace(run1.messages[1], from_history=True),
    ]

    assert holds_fold_basis(history, basis_summary="Prior facts", basis_runs=[run1], scope=TEST_AGENT_SCOPE)
    assert holds_fold_basis(history, basis_summary="  Prior facts \n", basis_runs=[run1], scope=TEST_AGENT_SCOPE)
    assert holds_fold_basis(history[1:], basis_summary=None, basis_runs=[run1], scope=TEST_AGENT_SCOPE)

    # Extra/missing run (limited replay or concurrent commit)
    assert not holds_fold_basis(history, basis_summary="Prior facts", basis_runs=[run1, run2], scope=TEST_AGENT_SCOPE)
    assert not holds_fold_basis(history, basis_summary="Prior facts", basis_runs=[], scope=TEST_AGENT_SCOPE)

    # Summary mismatch or duplicate summary
    assert not holds_fold_basis(history, basis_summary="Different", basis_runs=[run1], scope=TEST_AGENT_SCOPE)
    assert not holds_fold_basis(history, basis_summary=None, basis_runs=[run1], scope=TEST_AGENT_SCOPE)
    assert not holds_fold_basis(history[1:], basis_summary="Prior facts", basis_runs=[run1], scope=TEST_AGENT_SCOPE)
    assert not holds_fold_basis(
        [summary_msg, summary_msg, *history[1:]],
        basis_summary="Prior facts",
        basis_runs=[run1],
        scope=TEST_AGENT_SCOPE,
    )

    # Redacted / mutated content
    mutated = [summary_msg, replace(run1.messages[0], content="redacted", from_history=True), history[2]]
    assert not holds_fold_basis(mutated, basis_summary="Prior facts", basis_runs=[run1], scope=TEST_AGENT_SCOPE)

    # None digest (URL-only image whose content cannot be digested locally)
    url_msg = Message(role="user", content="look", images=[Image(url="https://example.com/a.png")])
    url_run = replace(run1, messages=[url_msg, run1.messages[1]])
    assert not holds_fold_basis(
        [replace(url_msg, from_history=True), history[2]],
        basis_summary=None,
        basis_runs=[url_run],
        scope=TEST_AGENT_SCOPE,
    )


def test_fork_frame_lru_isolation_sanitization_and_eviction(tmp_path: Path) -> None:
    clear_fork_frames()
    try:

        async def custom_on_send(_body: object) -> None:
            pass

        template = ModelRequest(
            messages=(Message(role="user", content="secret"),),
            tools=(ToolSpec(name="ping", description=None, parameters={}, strict=None),),
            retry_budget=object(),
            on_send=custom_on_send,
            prompt_hold_pin=[{"held": True}],
        )
        system = (Message(role="system", content="System prompt"),)
        key_a = (tmp_path / "a.db", "agent:code", "sess-1")
        key_b = (tmp_path / "b.db", "agent:code", "sess-1")
        key_c = (tmp_path / "a.db", "agent:other", "sess-1")

        remember_fork_frame(key_a, template, system)
        found = lookup_fork_frame(key_a)
        assert found is not None
        stored_template, stored_system = found
        assert stored_template.messages == ()
        assert stored_template.retry_budget is None
        assert stored_template.on_send is not custom_on_send
        assert stored_template.prompt_hold_pin is None
        assert stored_template.tools == template.tools
        assert stored_system == system
        assert lookup_fork_frame(key_b) is None
        assert lookup_fork_frame(key_c) is None

        # Fill beyond 64 entries after refreshing key_a so key_a stays while the oldest unrefreshed key evicts.
        for idx in range(64):
            remember_fork_frame((tmp_path / "a.db", "agent:code", f"extra-{idx}"), template, system)
        assert lookup_fork_frame(key_a) is None
        assert lookup_fork_frame((tmp_path / "a.db", "agent:code", "extra-0")) is not None
        remember_fork_frame((tmp_path / "a.db", "agent:code", "extra-64"), template, system)
        # extra-0 was refreshed by lookup above, so extra-1 is evicted instead.
        assert lookup_fork_frame((tmp_path / "a.db", "agent:code", "extra-0")) is not None
        assert lookup_fork_frame((tmp_path / "a.db", "agent:code", "extra-1")) is None
    finally:
        clear_fork_frames()


def test_build_scope_fork_constructs_turnless_whole_history_request() -> None:
    model = ScriptedModel([])
    system = (Message(role="system", content="System"),)
    run = _completed_run(
        "r1",
        messages=[Message(role="user", content="hello"), Message(role="assistant", content="world")],
    )
    source = ScopeForkSource(
        sizer=RequestSizer(model=model, configured_provider="openai"),
        template=ModelRequest(messages=()),
        system_messages=system,
        context_window=100_000,
        reserve_tokens=4_096,
    )
    fork = build_scope_fork(
        source,
        scope=TEST_AGENT_SCOPE,
        summary=ScopeSummary(text="Earlier", updated_at=datetime.now(UTC)),
        runs=[run],
    )
    assert fork.mid_turn is False
    assert [message.role for message in fork.request.messages] == ["system", "user", "user", "assistant"]
    assert "Earlier" in str(fork.request.messages[1].content)
    assert fork.request.messages[2].content == "hello"
    assert fork.request.messages[3].content == "world"
