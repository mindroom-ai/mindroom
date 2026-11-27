"""Tests for provider-native compaction checkpoint extraction and compaction:after hook emission."""

from __future__ import annotations

import asyncio
from dataclasses import replace
from typing import TYPE_CHECKING, Any
from unittest.mock import patch

import pytest

from mindroom.agent_loop.model_loop import LoopHooks
from mindroom.config.models import ModelConfig
from mindroom.history.native_compaction_hook import native_compaction_after_turn_hook
from mindroom.history.types import HistoryScope
from mindroom.hooks import (
    EVENT_COMPACTION_AFTER,
    EVENT_COMPACTION_BEFORE,
    CompactionHookContext,
    HookRegistry,
    TranscriptLine,
    hook,
)
from mindroom.llm.messages import Message
from mindroom.llm.provider import ModelIdentity, ModelRequest, NativeCompactionReplay, TurnCompleted
from mindroom.llm.runs import RunRecord
from mindroom.llm.usage import MessageMetrics, RunMetrics, TokenCounts
from mindroom.native_compaction import (
    NATIVE_CHECKPOINT_KEY,
    NativeCheckpoint,
    extract_native_checkpoint,
    native_checkpoint_state,
    native_replay,
)
from mindroom.prompt_message_tags import render_msg_tag
from mindroom.tool_system.runtime_context import tool_runtime_context
from tests.agent_loop.helpers import echo, gated, run_loop, spec, toolkit
from tests.fakes.scripted_model import ScriptedModel, tool_call
from tests.history_helpers import _hook_runtime_context, _make_config, _plugin

if TYPE_CHECKING:
    from collections.abc import Sequence
    from pathlib import Path

    from mindroom.config.main import Config
    from mindroom.llm.runs import AnyRunRecord
    from mindroom.tool_system.runtime_context import ToolRuntimeContext
    from mindroom.tool_system.tool_function import AdaptedToolkit

_ROUTE = "route-native-1"
_SCOPE = HistoryScope(kind="agent", scope_id="test_agent")
_NATIVE = NativeCompactionReplay(route=_ROUTE, threshold=1000)
_TEMPLATE = ModelRequest(messages=(), native_compaction=_NATIVE)


class _NativeScriptedModel(ScriptedModel):
    """Scripted model whose request_tokens projects from the latest native checkpoint."""

    def request_tokens(self, request: ModelRequest, *, start: int = 0) -> int:
        route = request.native_compaction.route if request.native_compaction is not None else None
        projected = native_replay(request.messages[start:], route).messages
        return super().request_tokens(replace(request, messages=tuple(projected)), start=0)


def _ckpt_msg(
    content: str = "Done.",
    *,
    items: list[dict[str, Any]] | None = None,
    checkpoint_prefix: bool = False,
    metrics: MessageMetrics | None = None,
    tool_calls: list[dict[str, Any]] | None = None,
    message_id: str | None = None,
) -> Message:
    kwargs: dict[str, Any] = {"role": "assistant", "content": content, "tool_calls": tool_calls}
    if message_id is not None:
        kwargs["id"] = message_id
    if metrics is not None:
        kwargs["metrics"] = metrics
    message = Message(**kwargs)
    if items is not None or checkpoint_prefix:
        state = native_checkpoint_state(items or [], _NATIVE, checkpoint_prefix=checkpoint_prefix)
        message.provider_data = {NATIVE_CHECKPOINT_KEY: state}
    return message


def _setup(
    tmp_path: Path,
    *,
    provider: str = "anthropic",
    model_id: str = "claude-sonnet-4-6",
    register_after: bool = True,
) -> tuple[Config, ToolRuntimeContext, list[CompactionHookContext]]:
    config, paths = _make_config(tmp_path)
    config.models["native"] = ModelConfig(provider=provider, id=model_id)
    observed: list[CompactionHookContext] = []

    @hook(EVENT_COMPACTION_BEFORE)
    async def on_before(ctx: CompactionHookContext) -> None:
        observed.append(ctx)

    @hook(EVENT_COMPACTION_AFTER)
    async def on_after(ctx: CompactionHookContext) -> None:
        observed.append(ctx)

    callbacks: list[object] = [on_before, on_after] if register_after else [on_before]
    registry = HookRegistry.from_plugins([_plugin("native-hooks", callbacks)])
    ctx = _hook_runtime_context(config=config, runtime_paths=paths, registry=registry, session_id="session-native")
    return config, ctx, observed


async def _run_native(
    config: Config,
    runtime_context: ToolRuntimeContext,
    turns: Sequence[Message],
    messages: list[Message],
    *,
    provider: str = "anthropic",
    model_id: str = "claude-sonnet-4-6",
    model_name: str = "native",
    tools: tuple[AdaptedToolkit, ...] = (),
    record: RunRecord | None = None,
) -> tuple[AnyRunRecord, LoopHooks, _NativeScriptedModel]:
    model = _NativeScriptedModel(
        turns=[[TurnCompleted(message=msg)] for msg in turns],
        identity=ModelIdentity(id=model_id, provider=provider),
    )
    hooks = LoopHooks(
        after_turn=(
            native_compaction_after_turn_hook(config=config, scope=_SCOPE, model_name=model_name, model=model),
        ),
    )
    with tool_runtime_context(runtime_context):
        _events, rec = await run_loop(
            spec(model, *tools),
            messages,
            hooks=hooks,
            record=record,
            request_template=_TEMPLATE,
        )
    return rec, hooks, model


def test_extract_native_checkpoint_handles_valid_noops_replay_prefix_and_malformed() -> None:
    """Extraction recognizes readable/encrypted checkpoints and ignores no-ops, prefixes, and malformed entries."""
    readable = _ckpt_msg(items=[{"type": "compaction", "content": "Claude summary"}], message_id="msg-anth")
    encrypted = _ckpt_msg(
        items=[{"type": "compaction", "id": "cmp_1", "encrypted_content": "opaque-bytes"}],
        message_id="msg-oai",
    )
    assert extract_native_checkpoint(readable) == NativeCheckpoint("msg-anth", _ROUTE, "Claude summary", False)
    assert extract_native_checkpoint(encrypted) == NativeCheckpoint("msg-oai", _ROUTE, None, True)
    for ignored in (
        _ckpt_msg(items=[{"type": "compaction", "content": None}]),
        _ckpt_msg(items=[{"type": "compaction", "content": "   "}]),
        _ckpt_msg(checkpoint_prefix=True),
        Message(
            role="user",
            content="hi",
            provider_data={NATIVE_CHECKPOINT_KEY: {"route": _ROUTE, "items": [{"type": "compaction", "content": "x"}]}},
        ),
        Message(
            role="assistant",
            content="hi",
            provider_data={NATIVE_CHECKPOINT_KEY: {"route": "", "items": [{"type": "compaction", "content": "x"}]}},
        ),
    ):
        assert extract_native_checkpoint(ignored) is None


@pytest.mark.asyncio
async def test_anthropic_readable_checkpoint_emits_after_only_with_split_usage_and_replayed_transcript(
    tmp_path: Path,
) -> None:
    """Anthropic checkpoints emit compaction:after only, with split-iteration tokens and native_replay transcript."""
    config, runtime_context, observed = _setup(tmp_path)
    split_metrics = MessageMetrics(
        tokens=TokenCounts(input_tokens=8_000, cache_read_tokens=3_000, cache_write_tokens=1_000, output_tokens=50),
        context=TokenCounts(input_tokens=900, cache_read_tokens=400, cache_write_tokens=100),
    )
    earlier = _ckpt_msg("After first compaction", items=[{"type": "compaction", "content": "Old summary"}])
    earlier.from_history = True
    messages = [
        Message(role="system", content="System prompt"),
        Message(role="user", content="Should be dropped by native_replay", from_history=True),
        earlier,
        Message(
            role="user",
            content=render_msg_tag(sender="@alice:localhost", display_name="Alice", body="Earlier question"),
            from_history=True,
        ),
        Message(role="user", content="Current turn question"),
    ]
    record = RunRecord(run_id="run-anth", agent_id="test_agent", user_id="@bob:localhost", metrics=RunMetrics())
    turn = _ckpt_msg(
        "Final answer",
        items=[{"type": "compaction", "content": "Anthropic plaintext summary"}],
        metrics=split_metrics,
    )
    await _run_native(config, runtime_context, [turn], messages, record=record)

    assert [ctx.event_name for ctx in observed] == [EVENT_COMPACTION_AFTER]
    ctx = observed[0]
    assert (ctx.kind, ctx.mode, ctx.compaction_summary, ctx.token_count_after) == (
        "native",
        "auto",
        "Anthropic plaintext summary",
        1_400,
    )
    assert ctx.token_count_before > 0
    assert (ctx.summary_model, ctx.compacted_run_count, ctx.session_id, ctx.requester_id) == (
        "native",
        None,
        "session-native",
        "@user:localhost",
    )
    assert ctx.compacted_at is not None
    assert ctx.compacted_at.endswith("Z")
    assert ctx.workspace_root == runtime_context.agent_workspace_root(create=False)
    assert ctx.transcript == (
        TranscriptLine(role="assistant", sender=None, display_name=None, text="After first compaction"),
        TranscriptLine(role="user", sender="@alice:localhost", display_name="Alice", text="Earlier question"),
        TranscriptLine(role="user", sender="@bob:localhost", display_name=None, text="Current turn question"),
    )


@pytest.mark.asyncio
async def test_openai_encrypted_checkpoint_and_subsequent_replay_turn_emit_once(tmp_path: Path) -> None:
    """OpenAI encrypted checkpoints emit summary=None with re-sized tokens, and replay turns do not double-emit."""
    config, runtime_context, observed = _setup(tmp_path, provider="openai", model_id="gpt-5.4")
    turns = [
        _ckpt_msg(
            "",
            tool_calls=[tool_call("echo", "c1", text="ok")],
            items=[{"type": "compaction", "id": "cmp_1", "encrypted_content": "enc"}],
        ),
        _ckpt_msg("All done", checkpoint_prefix=True),
    ]
    messages = [
        Message(role="system", content="System"),
        Message(role="user", content="Long conversation history " * 120, from_history=True),
        Message(role="user", content="Do the work"),
    ]
    await _run_native(
        config,
        runtime_context,
        turns,
        messages,
        provider="openai",
        model_id="gpt-5.4",
        model_name="",
        tools=(toolkit(echo),),
    )

    assert [ctx.event_name for ctx in observed] == [EVENT_COMPACTION_AFTER]
    ctx = observed[0]
    assert (ctx.kind, ctx.mode, ctx.compaction_summary, ctx.summary_model) == ("native", "auto", None, "gpt-5.4")
    assert ctx.token_count_after is not None
    assert ctx.token_count_after < ctx.token_count_before


@pytest.mark.asyncio
async def test_noops_approval_resume_dedupe_and_single_iteration_fallback(tmp_path: Path) -> None:
    """No-ops emit nothing; approval resume and duplicate message IDs never double-emit; single-iter usage re-sizes."""
    config, runtime_context, observed = _setup(tmp_path)

    # 1. Anthropic null/blank no-ops emit nothing.
    noop_turns = [
        _ckpt_msg("", tool_calls=[tool_call("echo", "c1", text="1")], items=[{"type": "compaction", "content": None}]),
        _ckpt_msg("Done", items=[{"type": "compaction", "content": "   "}]),
    ]
    await _run_native(config, runtime_context, noop_turns, [Message(role="user", content="Hi")], tools=(toolkit(echo),))
    assert observed == []

    # 2. Approval resume after a checkpointed paused tool call does not double-emit.
    messages = [Message(role="user", content="Run gated tool")]
    record = RunRecord(run_id="run-resume", agent_id="test_agent", metrics=RunMetrics())
    resume_turns = [
        _ckpt_msg(
            "",
            tool_calls=[tool_call("echo", "c-gate", text="approved")],
            items=[{"type": "compaction", "content": "Paused summary"}],
            message_id="msg-paused",
        ),
        _ckpt_msg("Resumed answer", checkpoint_prefix=True),
    ]
    _, resume_hooks, resume_model = await _run_native(
        config,
        runtime_context,
        resume_turns,
        messages,
        tools=(gated(echo),),
        record=record,
    )
    assert len(observed) == 1
    record.requirements[0].confirm()
    with tool_runtime_context(runtime_context):
        await run_loop(
            spec(resume_model, gated(echo)),
            messages,
            hooks=resume_hooks,
            record=record,
            request_template=_TEMPLATE,
            resumed=record.requirements,
        )
    assert [ctx.compaction_summary for ctx in observed] == ["Paused summary"]

    # 3. Duplicate checkpoint message_id dedupes in-loop, and single-iteration usage falls back to re-sizing.
    observed.clear()
    single_iter = MessageMetrics(
        tokens=TokenCounts(input_tokens=5_000, cache_read_tokens=1_000, output_tokens=20),
        context=TokenCounts(input_tokens=5_000, cache_read_tokens=1_000),
    )
    dup_msg = _ckpt_msg("Paused", items=[{"type": "compaction", "content": "S"}], metrics=single_iter, message_id="dup")
    await _run_native(
        config,
        runtime_context,
        [replace(dup_msg, tool_calls=[tool_call("echo", "c2", text="x")]), dup_msg],
        [Message(role="user", content="Long context " * 100)],
        tools=(toolkit(echo),),
    )
    assert len(observed) == 1
    assert observed[0].token_count_after is not None
    assert observed[0].token_count_after < 6_000


@pytest.mark.asyncio
async def test_native_hook_fast_path_error_isolation_and_cancellation(tmp_path: Path) -> None:
    """Fast path skips sizing without after-hooks; RuntimeError is isolated while CancelledError propagates."""
    config, before_only_ctx, observed = _setup(tmp_path, register_after=False)
    ckpt = _ckpt_msg("Survived", items=[{"type": "compaction", "content": "S"}])
    with patch("mindroom.history.native_compaction_hook.request_sizer", side_effect=AssertionError("must not size")):
        await _run_native(config, before_only_ctx, [ckpt], [Message(role="user", content="Hi")])
    assert observed == []

    config, full_ctx, observed = _setup(tmp_path)
    with patch("mindroom.history.native_compaction_hook.request_sizer", side_effect=RuntimeError("boom")):
        rec, _, _ = await _run_native(config, full_ctx, [ckpt], [Message(role="user", content="Hi")])
    assert rec.content == "Survived"
    assert observed == []

    with (
        patch("mindroom.history.native_compaction_hook.emit_compaction_hook", side_effect=asyncio.CancelledError),
        pytest.raises(asyncio.CancelledError),
    ):
        await _run_native(config, full_ctx, [ckpt], [Message(role="user", content="Hi")])
