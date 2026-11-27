"""Tests for provider-native compaction checkpoint extraction and compaction:after hook emission."""

from __future__ import annotations

import asyncio
from dataclasses import replace
from typing import TYPE_CHECKING, Any
from unittest.mock import patch

import pytest

from mindroom.agent_loop.model_loop import LoopHooks
from mindroom.automations.steps import AUTOMATION_HOOK_PREFIX
from mindroom.config.models import ModelConfig
from mindroom.constants import HOOK_SOURCE_RUN_METADATA_KEY, TURN_INTENT_RUN_METADATA_KEY
from mindroom.history.native_compaction_hook import native_compaction_after_turn_hook
from mindroom.history.request_size import RequestSizer
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
from mindroom.llm.provider import Capabilities, ModelIdentity, ModelRequest, NativeCompactionReplay, TurnCompleted
from mindroom.llm.request_identity import REQUEST_IDENTITY_KEY, request_prefixes
from mindroom.llm.runs import RunRecord
from mindroom.llm.usage import MessageMetrics, RunMetrics, TokenCounts
from mindroom.native_compaction import (
    ENCRYPTED_REPLAY_RESET_KEY,
    NATIVE_CHECKPOINT_KEY,
    NativeCheckpoint,
    extract_native_checkpoint,
    native_checkpoint_state,
    native_replay,
)
from mindroom.prompt_message_tags import render_msg_tag
from mindroom.tool_system.runtime_context import tool_runtime_context
from mindroom.turn_origin import TurnIntent
from tests.agent_loop.helpers import echo, gated, run_loop, spec, toolkit
from tests.fakes.scripted_model import ScriptedModel, tool_call
from tests.history_helpers import _hook_runtime_context, _make_config, _plugin

if TYPE_CHECKING:
    from collections.abc import Callable, Sequence
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
    """Scripted model whose request_tokens projects from the latest native checkpoint or reset."""

    def request_tokens(self, request: ModelRequest, *, start: int = 0) -> int:
        route = request.native_compaction.route if request.native_compaction is not None else None
        projected = native_replay(
            request.messages[start:],
            route,
            reset_scope=self.capabilities.replay_reset_scope,
            stripped=request.strip_encrypted_replay,
        ).messages
        return super().request_tokens(replace(request, messages=tuple(projected)), start=0)


def _ckpt_msg(
    content: str = "Done.",
    *,
    items: list[dict[str, Any]] | None = None,
    checkpoint_prefix: bool = False,
    metrics: MessageMetrics | None = None,
    tool_calls: list[dict[str, Any]] | None = None,
    message_id: str | None = None,
    extra_provider_data: dict[str, Any] | None = None,
) -> Message:
    kwargs: dict[str, Any] = {"role": "assistant", "content": content, "tool_calls": tool_calls}
    if message_id is not None:
        kwargs["id"] = message_id
    if metrics is not None:
        kwargs["metrics"] = metrics
    message = Message(**kwargs)
    provider_data: dict[str, Any] = dict(extra_provider_data or {})
    if items is not None or checkpoint_prefix:
        state = native_checkpoint_state(items or [], _NATIVE, checkpoint_prefix=checkpoint_prefix)
        provider_data[NATIVE_CHECKPOINT_KEY] = state
    if provider_data:
        message.provider_data = provider_data
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
    capabilities: Capabilities | None = None,
    session_runs: Sequence[AnyRunRecord] | Callable[[], Sequence[AnyRunRecord]] = (),
) -> tuple[AnyRunRecord, LoopHooks, _NativeScriptedModel]:
    model = _NativeScriptedModel(
        turns=[[TurnCompleted(message=msg)] for msg in turns],
        identity=ModelIdentity(id=model_id, provider=provider),
        capabilities=capabilities or Capabilities(),
    )
    hooks = LoopHooks(
        after_turn=(
            native_compaction_after_turn_hook(
                config=config,
                scope=_SCOPE,
                model_name=model_name,
                model=model,
                session_runs=session_runs,
            ),
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
async def test_noops_approval_resume_and_single_iteration_fallback(tmp_path: Path) -> None:
    """No-ops emit nothing; approval resume does not double-emit; single-iteration usage falls back to re-sizing."""
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

    # 3. Single-iteration usage (billed == context) falls back to re-sizing post-checkpoint request.
    observed.clear()
    single_iter = MessageMetrics(
        tokens=TokenCounts(input_tokens=5_000, cache_read_tokens=1_000, output_tokens=20),
        context=TokenCounts(input_tokens=5_000, cache_read_tokens=1_000),
    )
    single_turn = _ckpt_msg("Done", items=[{"type": "compaction", "content": "S"}], metrics=single_iter)
    await _run_native(
        config,
        runtime_context,
        [single_turn],
        [Message(role="user", content="Long context " * 100)],
    )
    assert len(observed) == 1
    assert observed[0].token_count_after is not None
    assert observed[0].token_count_after < 6_000


@pytest.mark.asyncio
async def test_native_compaction_preserves_session_run_metadata_and_exclusions(tmp_path: Path) -> None:
    """Replayed history matching session_runs preserves user attribution and excludes runtime/automation prompts."""
    config, runtime_context, observed = _setup(tmp_path)
    user_q = Message(role="user", content="Alice question")
    user_a = Message(role="assistant", content="Alice answer")
    job_prompt = Message(role="user", content="Background job finished runtime prompt")
    job_reply = Message(role="assistant", content="Job follow-up reply")
    auto_prompt = Message(role="user", content="Automation trigger prompt")
    auto_reply = Message(role="assistant", content="Automation reply")
    stored_runs = [
        RunRecord(run_id="run-user", agent_id="test_agent", user_id="@alice:localhost", messages=[user_q, user_a]),
        RunRecord(
            run_id="run-job",
            agent_id="test_agent",
            messages=[job_prompt, job_reply],
            metadata={TURN_INTENT_RUN_METADATA_KEY: TurnIntent.JOB_FOLLOW_UP},
        ),
        RunRecord(
            run_id="run-auto",
            agent_id="test_agent",
            messages=[auto_prompt, auto_reply],
            metadata={HOOK_SOURCE_RUN_METADATA_KEY: f"{AUTOMATION_HOOK_PREFIX}nightly"},
        ),
    ]
    messages = [
        Message(role="system", content="System prompt"),
        *(replace(msg, from_history=True) for run in stored_runs for msg in run.messages),
        Message(role="user", content="Current turn question"),
    ]
    record = RunRecord(run_id="run-now", agent_id="test_agent", user_id="@bob:localhost", metrics=RunMetrics())
    turn = _ckpt_msg("Final answer", items=[{"type": "compaction", "content": "Summary"}])

    await _run_native(
        config,
        runtime_context,
        [turn],
        messages,
        record=record,
        session_runs=lambda: stored_runs,
    )

    assert len(observed) == 1
    assert observed[0].transcript == (
        TranscriptLine(role="user", sender="@alice:localhost", display_name=None, text="Alice question"),
        TranscriptLine(role="assistant", sender=None, display_name=None, text="Alice answer"),
        TranscriptLine(role="assistant", sender=None, display_name=None, text="Job follow-up reply"),
        TranscriptLine(role="user", sender="@bob:localhost", display_name=None, text="Current turn question"),
    )


@pytest.mark.asyncio
async def test_openrouter_encrypted_replay_recovery_and_prior_reset_boundary(tmp_path: Path) -> None:
    """OpenRouter encrypted-replay recovery and prior reset boundaries cover full history in transcript and sizing."""
    reset_scope = "openrouter:openai/gpt-5.4"
    capabilities = Capabilities(replay_reset_scope=reset_scope)
    config, runtime_context, observed = _setup(tmp_path, provider="openrouter", model_id="openai/gpt-5.4")

    # 1. Recovery on the compacting turn itself (response carries ENCRYPTED_REPLAY_RESET_KEY).
    earliest_text = "Earliest pre-compaction question " * 80
    old_ckpt = _ckpt_msg(
        "Earlier checkpoint reply",
        items=[{"type": "compaction", "id": "cmp_old", "encrypted_content": "stale-enc"}],
    )
    old_ckpt.from_history = True
    anchored_reply = Message(
        role="assistant",
        content="Pre-strip answer",
        from_history=True,
        metrics=MessageMetrics(context=TokenCounts(input_tokens=9_999)),
    )
    messages = [
        Message(role="system", content="System"),
        Message(role="user", content=earliest_text, from_history=True),
        old_ckpt,
        Message(role="user", content="Mid-history question", from_history=True),
        anchored_reply,
        Message(role="user", content="Current question"),
    ]
    probe_model = _NativeScriptedModel(
        turns=[],
        identity=ModelIdentity(id="openai/gpt-5.4", provider="openrouter"),
        capabilities=capabilities,
    )
    sent_request = replace(_TEMPLATE, messages=tuple(messages))
    pre_strip_prefixes = request_prefixes(probe_model, sent_request)
    anchored_reply.provider_data = {REQUEST_IDENTITY_KEY: pre_strip_prefixes[4]}

    recovered_turn = _ckpt_msg(
        "Recovered answer",
        items=[{"type": "compaction", "id": "cmp_new", "encrypted_content": "fresh-enc"}],
        extra_provider_data={ENCRYPTED_REPLAY_RESET_KEY: reset_scope},
    )
    await _run_native(
        config,
        runtime_context,
        [recovered_turn],
        messages,
        provider="openrouter",
        model_id="openai/gpt-5.4",
        capabilities=capabilities,
    )

    assert len(observed) == 1
    ctx = observed[0]
    full_stripped_tokens = probe_model.request_tokens(replace(sent_request, strip_encrypted_replay=True))
    pre_strip_tokens = probe_model.request_tokens(sent_request)
    anchored_pre_strip = RequestSizer(probe_model, "openrouter").size(sent_request)
    assert anchored_pre_strip.reported is True
    assert full_stripped_tokens > pre_strip_tokens
    assert ctx.token_count_before == full_stripped_tokens
    assert ctx.token_count_before != anchored_pre_strip.tokens
    assert ctx.token_count_after is not None
    assert ctx.token_count_after < ctx.token_count_before
    assert ctx.transcript[0].text == earliest_text

    # 2. Prior ENCRYPTED_REPLAY_RESET_KEY boundary on reset_scope without a reset on the new compacting turn.
    observed.clear()
    prior_reset = Message(
        role="assistant",
        content="Prior recovered reply",
        from_history=True,
        provider_data={ENCRYPTED_REPLAY_RESET_KEY: reset_scope},
        metrics=MessageMetrics(context=TokenCounts(input_tokens=40)),
    )
    messages_after_reset = [
        Message(role="system", content="System"),
        Message(role="user", content=earliest_text, from_history=True),
        old_ckpt,
        prior_reset,
        Message(role="user", content="Post-reset question"),
    ]
    post_reset_request = replace(_TEMPLATE, messages=tuple(messages_after_reset))
    prior_reset_prefixes = request_prefixes(probe_model, post_reset_request)
    prior_reset.provider_data = {
        ENCRYPTED_REPLAY_RESET_KEY: reset_scope,
        REQUEST_IDENTITY_KEY: prior_reset_prefixes[3],
    }
    plain_ckpt_turn = _ckpt_msg(
        "Post-reset compacted answer",
        items=[{"type": "compaction", "id": "cmp_post", "encrypted_content": "post-enc"}],
    )
    await _run_native(
        config,
        runtime_context,
        [plain_ckpt_turn],
        messages_after_reset,
        provider="openrouter",
        model_id="openai/gpt-5.4",
        capabilities=capabilities,
    )

    assert len(observed) == 1
    ctx2 = observed[0]
    no_scope_model = _NativeScriptedModel(
        turns=[],
        identity=ModelIdentity(id="openai/gpt-5.4", provider="openrouter"),
    )
    expected_post_reset_tokens = probe_model.request_tokens(post_reset_request)
    checkpoint_only_tokens = no_scope_model.request_tokens(post_reset_request)
    assert expected_post_reset_tokens > checkpoint_only_tokens
    assert ctx2.token_count_before == expected_post_reset_tokens
    assert ctx2.token_count_after is not None
    assert ctx2.token_count_after < ctx2.token_count_before
    assert ctx2.transcript[0].text == earliest_text


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
