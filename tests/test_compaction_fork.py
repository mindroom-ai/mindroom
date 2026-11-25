"""Tests for cache-warm fork compaction."""
# ruff: noqa: D103

from __future__ import annotations

import asyncio
import json
import os
import time
from dataclasses import replace
from datetime import UTC, datetime
from functools import partial
from typing import TYPE_CHECKING, Any
from uuid import uuid4

import httpx2
import pytest
from pydantic import BaseModel
from structlog.testing import capture_logs

from mindroom.agent_loop.model_loop import LoopHooks
from mindroom.agent_loop.run import run_agent
from mindroom.agent_loop.spec import AgentSpec, RunContext, UnstoredSession
from mindroom.config.models import CompactionConfig, ModelConfig
from mindroom.history.compaction import compact_scope_history
from mindroom.history.mid_turn_compaction import mid_turn_compaction_hook
from mindroom.history.policy import resolve_history_execution_plan
from mindroom.history.replay import configured_replay_plan
from mindroom.history.request_messages import TurnMessages, build_request, scope_history_messages
from mindroom.history.request_size import RequestFrame, RequestSize, RequestSizer, request_sizer
from mindroom.history.runtime import compact_scope_now, prepare_scope_history
from mindroom.history.session_context import ScopeSessionContext
from mindroom.history.storage import new_scope_session, read_scope_state, set_force_compaction
from mindroom.history.summarizer import Summarizer, SummaryModel, summarize_runs
from mindroom.history.summary_call import (
    CompactionForkToolCallError,
    _CompactionSummaryTimeoutError,
    request_compaction_summary,
)
from mindroom.history.summary_fork import (
    SummaryFork,
    build_scope_fork,
    clear_fork_frames,
    holds_fold_basis,
    lookup_fork_frame,
    prepare_fork_request,
    remember_fork_frame,
    warm_cache_ttl_seconds,
)
from mindroom.history.summary_message import compaction_summary_message, is_compaction_summary
from mindroom.history.types import HistoryPreparationInputs, ScopeSummary
from mindroom.hooks.enrichment import is_transient_context
from mindroom.llm.errors import (
    MALFORMED_TOOL_CALL,
    AuthenticationFailed,
    ContextWindowExceeded,
    ModelSafeguardRefusal,
    ProviderError,
    ProviderUnavailable,
)
from mindroom.llm.events import RunFinished
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
from mindroom.llm.request_identity import CACHE_ROUTING_KEY, cache_routing_digest
from mindroom.llm.runs import RunRecord
from mindroom.llm.usage import MessageMetrics, TokenCounts
from mindroom.media_inputs import MediaInputs
from mindroom.prompts import CODEX_DEFAULT_INSTRUCTIONS, COMPACTION_SUMMARY_PROMPT
from mindroom.providers.openai.build import ProviderRuntime, build_provider
from mindroom.providers.pipeline.guidance_retry import guidance_retry_stream
from mindroom.providers.pipeline.prompt_hold import prompt_hold_path
from mindroom.providers.pipeline.wrapped import WrappedProvider
from mindroom.run_storage import RunStorage
from mindroom.session_storage.agent_storage import create_session_storage
from mindroom.session_storage.store import persist_to
from mindroom.tool_system.tool_function import AdaptedToolkit, ToolFunction
from mindroom.tool_system.worker_routing import ToolExecutionIdentity
from tests.conftest import delete_runs, install_model_providers, seed_session
from tests.fakes.scripted_model import Await, ScriptedModel, answer, calls, tool_call, usage
from tests.fakes.summary_model import summary_model, summary_steps, summary_turn
from tests.history_helpers import (
    _ALL_HISTORY_SETTINGS,
    TEST_AGENT_SCOPE,
    TOOL_RESULT,
    RecordingCompactionLifecycle,
    SummaryCalls,
    _completed_run,
    _make_config,
    _session,
    stamp_request_identity,
    summary_rows_of,
)
from tests.providers.anthropic_family_support import isolated_provider_environment, provider_for, sse_reply
from tests.providers.gemini_ollama_support import build_adapter as build_gemini_adapter
from tests.providers.gemini_ollama_support import runtime_paths_for
from tests.providers.golden_support import httpx2_transport, pipeline_provider, streamed_reply
from tests.providers.openai_family_support import openai_family_adapter, write_cli_logins

if TYPE_CHECKING:
    from collections.abc import Callable, Iterator
    from pathlib import Path

    import httpx

    from mindroom.llm.provider import ModelProvider

_ID = ModelIdentity(id="scripted-model", provider="Synthetic")
_SYS = (Message(role="system", content="You help."),)
_TOOL = ToolSpec(name="search", description="Search", parameters={"type": "object"}, strict=None)
_TS = 1_700_000_000


class _StructuredAnswer(BaseModel):
    answer: str


@pytest.fixture(autouse=True)
def _reset_fork_frames() -> Iterator[None]:
    clear_fork_frames()
    yield
    clear_fork_frames()


def _msg(role: str, content: str | None) -> Message:
    return Message(role=role, content=content)


def _answered(
    model: ModelProvider,
    req: ModelRequest,
    text: str = "Hello",
    reported: int = 1_000,
    ts: int = _TS,
) -> Message:
    reply = Message(
        role="assistant",
        content=text,
        created_at=ts,
        metrics=MessageMetrics(tokens=TokenCounts(input_tokens=reported, output_tokens=10)),
        provider_data=model_provenance(model.identity.id, model.identity.provider),
    )
    stamp_request_identity(reply, model, req)
    return reply


def _raise_step(error: BaseException) -> Await:
    async def _raise() -> None:
        raise error

    return Await(_raise)


def _wait_step(started: asyncio.Event) -> Await:
    async def _wait() -> None:
        started.set()
        await asyncio.Future()

    return Await(_wait)


async def _broken_usage(*_args: object) -> None:
    msg = "disk full"
    raise OSError(msg)


def _fork_config(tmp_path: Path, **comp_kw: Any) -> tuple[Any, Any, Any]:  # noqa: ANN401
    models = {
        "default": ModelConfig(provider="openai", id="scripted-model", context_window=100_000),
        "other": ModelConfig(provider="openai", id="other-model", context_window=100_000),
    }
    config, paths = _make_config(tmp_path, defaults_compaction=CompactionConfig(enabled=True, **comp_kw), models=models)
    return config, paths, create_session_storage("test_agent", config, paths, execution_identity=None)


def _scope_ctx(storage: Any, sid: str, config: Any, paths: Any) -> ScopeSessionContext:  # noqa: ANN401
    factory = lambda: create_session_storage("test_agent", config, paths, execution_identity=None)  # noqa: E731
    return ScopeSessionContext(TEST_AGENT_SCOPE, storage, storage.load_session(sid), sid, factory)


def _inputs(config: Any, *, compaction_model: str | None = None) -> HistoryPreparationInputs:  # noqa: ANN401
    settings = config.resolve_scope("test_agent").history_settings
    if compaction_model is not None:
        settings = replace(
            settings,
            compaction_config=settings.compaction_config.model_copy(update={"model": compaction_model}),
        )
    plan = resolve_history_execution_plan(
        config=config,
        history_settings=settings,
        active_model_name="default",
        active_context_window=None,
    )
    return HistoryPreparationInputs(history_settings=settings, execution_plan=plan)


def _install_default(mp: pytest.MonkeyPatch, model: ModelProvider, **extra: Any) -> None:  # noqa: ANN401
    install_model_providers(
        mp,
        {"default": summary_model(model_id=model.identity.id, provider=model.identity.provider), **extra},
    )


def _warm_run(
    model: ScriptedModel,
    tpl: ModelRequest,
    reported: int,
    ts: int | None = None,
    run_id: str = "r1",
) -> RunRecord:
    u = _msg("user", "q1")
    a = _answered(model, replace(tpl, messages=(*_SYS, u)), "a1", reported, int(time.time()) if ts is None else ts)
    return _completed_run(run_id, messages=[u, a])


def _warm_fork(
    model: ScriptedModel | None = None,
    reported: int = 1_000,
    window: int = 100_000,
    **req_kw: object,
) -> tuple[ScriptedModel, SummaryFork]:
    active = model or ScriptedModel([])
    first = ModelRequest(messages=(*_SYS, _msg("user", "Hi")), **{"tools": (_TOOL,), **req_kw})  # type: ignore[arg-type]
    live = replace(first, messages=(*first.messages, _answered(active, first, reported=reported)))
    return active, SummaryFork(RequestSizer(active, "openai"), live, window, 4_096, False)


def _gate(
    fork: SummaryFork,
    *,
    identity: ModelIdentity | None = None,
    now: float = _TS + 50.0,
) -> tuple[ModelRequest | None, str | None]:
    return prepare_fork_request(
        fork,
        summary_prompt="Summarize",
        summary_model_identity=identity or fork.sizer.model.identity,
        now=now,
    )


def _summarizer(
    primary: ModelProvider,
    *,
    fallback: ModelProvider | None = None,
    on_usage: Any = None,  # noqa: ANN401
    timeout_s: float = 10.0,
    sid: str = "sess-matrix",
) -> Summarizer:
    fb = SummaryModel(fallback, "fallback", 50_000) if fallback else None
    return Summarizer(
        SummaryModel(primary, "primary", 50_000),
        fb,
        COMPACTION_SUMMARY_PROMPT,
        timeout_s,
        _ALL_HISTORY_SETTINGS,
        sid,
        TEST_AGENT_SCOPE,
        on_usage,
    )


def _responses_sse(n: int, text: str, *, input_tokens: int, cached_tokens: int | None = None) -> httpx.Response:
    toks: dict[str, Any] = {"input_tokens": input_tokens, "output_tokens": 25, "total_tokens": input_tokens + 25}
    if cached_tokens is not None:
        toks["input_tokens_details"] = {"cached_tokens": cached_tokens}
    item = {
        "id": f"msg_{n}",
        "type": "message",
        "role": "assistant",
        "status": "completed",
        "content": [{"type": "output_text", "text": text, "annotations": []}],
    }
    return streamed_reply(
        {
            "id": f"resp_{n}",
            "object": "response",
            "created_at": 1,
            "model": "gpt-5.4",
            "status": "completed",
            "incomplete_details": None,
            "error": None,
            "output": [item],
            "usage": toks,
        },
    )


async def _wire_fork(
    model: ModelProvider,
    provider: str,
    req: ModelRequest,
    *,
    mid_turn: bool = False,
    reserve: int = 16_384,
) -> str:
    fork = SummaryFork(RequestSizer(model, provider), req, 200_000, reserve, mid_turn)
    fork_req, reason = prepare_fork_request(
        fork,
        summary_prompt=COMPACTION_SUMMARY_PROMPT,
        summary_model_identity=model.identity,
    )
    assert (reason, fork_req is not None) == (None, True)
    assert fork_req is not None
    if req.prompt_hold is not None:
        assert fork_req.prompt_hold_pin == []
    return await request_compaction_summary(
        model=model,
        request=fork_req,
        timeout_seconds=10.0,
        on_usage=None,
        reject_tool_calls=True,
    )


# ---------------------------------------------------------------------------
# Unit tests: retry guard, request anchor metadata, summary_call, summary_fork
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_guidance_retry_stream_skips_retry_when_caller_retries() -> None:
    malformed = ProviderError("malformed", provider="gemini", code=MALFORMED_TOOL_CALL)
    inner = ScriptedModel([[_raise_step(malformed)], summary_steps("recovered")])
    req = ModelRequest(messages=(_msg("user", "compact"),), overrides=RequestOverrides(transient_retries="caller"))
    with pytest.raises(ProviderError) as exc_info:
        await complete(WrappedProvider(inner, guidance_retry_stream), req)
    assert (exc_info.value.code, len(inner.requests)) == (MALFORMED_TOOL_CALL, 1)

    retry_inner = ScriptedModel([[_raise_step(malformed)], summary_steps("recovered")])
    turn = await complete(
        WrappedProvider(retry_inner, guidance_retry_stream),
        ModelRequest(messages=(_msg("user", "hi"),)),
    )
    assert (turn.content, len(retry_inner.requests)) == ("recovered", 2)


def test_request_size_exposes_anchor_index_and_answered_at() -> None:
    model = ScriptedModel([])
    sizer = RequestSizer(model, "openai")
    first = ModelRequest(messages=(*_SYS, _msg("user", "Hi")))
    later = replace(
        first,
        messages=(*first.messages, _answered(model, first, reported=1_200, ts=_TS + 123), _msg("user", "More")),
    )

    anchored = sizer.size(later)
    assert (anchored.reported, anchored.anchor_index, anchored.answered_at) == (True, 2, float(_TS + 123))
    assert anchored == RequestSize(1_200 + model.request_tokens(later, start=2), reported=True)
    unanchored = sizer.size(first)
    assert (unanchored.reported, unanchored.anchor_index, unanchored.answered_at) == (False, None, None)


def test_execution_plan_exposes_compaction_context_window(tmp_path: Path) -> None:
    models = {
        "default": ModelConfig(provider="openai", id="main", context_window=100_000),
        "summary": ModelConfig(provider="openai", id="small", context_window=32_000),
        "no_window": ModelConfig(provider="openai", id="unknown", context_window=None),
    }
    config, _ = _make_config(tmp_path, models=models)
    for model_name, active_win, expected in [
        (None, None, 100_000),
        ("summary", None, 32_000),
        ("no_window", 100_000, None),
    ]:
        settings = replace(_ALL_HISTORY_SETTINGS, compaction_config=CompactionConfig(enabled=True, model=model_name))
        plan = resolve_history_execution_plan(
            config=config,
            history_settings=settings,
            active_model_name="default",
            active_context_window=active_win,
        )
        assert plan.compaction_context_window == expected


@pytest.mark.asyncio
@pytest.mark.parametrize("stop_reason", ["end", "tool_use"])
async def test_request_compaction_summary_rejects_tool_calls_after_billing(stop_reason: str) -> None:
    tc = tool_call("search", "call_1")
    metrics = MessageMetrics(tokens=TokenCounts(input_tokens=400, output_tokens=20, cache_read_tokens=300))
    assistant = Message(role="assistant", content="## Goal\nSummary", tool_calls=[tc], metrics=metrics)
    model = ScriptedModel(
        [[UsageReported(metrics), ToolCallReady(tc), TurnCompleted(message=assistant, stop_reason=stop_reason)]],
    )
    recorded: list[MessageMetrics] = []

    async def on_usage(_identity: ModelIdentity, u: MessageMetrics) -> None:
        recorded.append(u)

    req = ModelRequest(messages=(_msg("user", "compact"),))
    with pytest.raises(CompactionForkToolCallError):
        await request_compaction_summary(
            model=model,
            request=req,
            timeout_seconds=10.0,
            on_usage=on_usage,
            reject_tool_calls=True,
        )
    assert (len(recorded), recorded[0].tokens.cache_read_tokens) == (1, 300)


@pytest.mark.asyncio
async def test_request_compaction_summary_warns_on_usage_error_and_propagates_timeout_and_cancellation() -> None:
    metrics = MessageMetrics(tokens=TokenCounts(input_tokens=100, output_tokens=10))
    turn = TurnCompleted(message=Message(role="assistant", content="summary", metrics=metrics), stop_reason="end")
    call = partial(
        request_compaction_summary,
        request=ModelRequest(messages=(_msg("user", "compact"),)),
        reject_tool_calls=True,
    )

    with capture_logs() as logs:
        assert (
            await call(
                model=ScriptedModel([[UsageReported(metrics), turn]]),
                timeout_seconds=10.0,
                on_usage=_broken_usage,
            )
            == "summary"
        )
    assert any(entry.get("event") == "compaction_summary_usage_save_failed" for entry in logs)

    with pytest.raises(_CompactionSummaryTimeoutError):
        await call(
            model=ScriptedModel([[Await(lambda: asyncio.sleep(0.05)), turn]]),
            timeout_seconds=0.01,
            on_usage=None,
        )

    started = asyncio.Event()
    task = asyncio.create_task(
        call(model=ScriptedModel([[_wait_step(started), turn]]), timeout_seconds=10.0, on_usage=None),
    )
    await started.wait()
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task


def test_warm_cache_ttl_seconds() -> None:
    ext = {"extended_cache_time": True, "cache_system_prompt": True}
    for identity, expected in [
        (None, 3300.0),
        (ModelIdentity(id="claude-sonnet-5", provider="Anthropic"), 3300.0),
        (ModelIdentity(id="anthropic/claude-sonnet-5", provider="OpenRouter"), 3300.0),
        (ModelIdentity(id="openai/gpt-5", provider="OpenRouter"), 240.0),
        (ModelIdentity(id="deepseek-chat", provider="DeepSeek"), 240.0),
    ]:
        assert warm_cache_ttl_seconds(ext, identity=identity) == expected
    assert warm_cache_ttl_seconds({"extended_cache_time": True, "cache_system_prompt": False}) == 240.0
    assert warm_cache_ttl_seconds({"extended_cache_time": False, "cache_system_prompt": True}) == 240.0
    assert warm_cache_ttl_seconds({}) == 240.0


def test_prepare_fork_request_happy_path_preserves_prefix_and_pins_hold(tmp_path: Path) -> None:
    hold_path = tmp_path / "hold.json"
    model, fork = _warm_fork(prompt_hold=hold_path, retry_budget=object())
    fork_req, reason = prepare_fork_request(
        fork,
        summary_prompt="CUSTOM PROMPT",
        summary_model_identity=model.identity,
        now=_TS + 100.0,
    )
    assert reason is None
    assert fork_req is not None
    assert (fork_req.tools, fork_req.messages[:-1]) == (fork.request.tools, fork.request.messages)
    assert (fork_req.overrides.transient_retries, fork_req.overrides.prompt_cache) == ("caller", True)
    assert (fork_req.prompt_hold, fork_req.prompt_hold_pin, fork_req.retry_budget) == (hold_path, [], None)
    prompt_text = str(fork_req.messages[-1].content)
    assert fork_req.messages[-1].role == "user"
    assert is_transient_context(fork_req.messages[-1].content)
    assert "CUSTOM PROMPT" in prompt_text
    assert "<mindroom_transient_context>" in prompt_text
    assert "Do NOT copy or project standing system-prompt rules" in prompt_text


def test_prepare_fork_request_gates() -> None:
    _, fork = _warm_fork()
    assert _gate(fork, identity=ModelIdentity(id="other", provider="Synthetic")) == (None, "different_model")
    for cc_provider in ("claude_code", "Anthropic (Claude Code)"):
        cc_model = ScriptedModel([], identity=ModelIdentity(id="claude", provider=cc_provider))
        assert _gate(_warm_fork(cc_model)[1]) == (None, "unsupported_provider")

    older_msgs = (*fork.request.messages, _msg("user", "More"), _msg("assistant", "Unanchored"))
    uncached = ScriptedModel([], model_params={"cache_system_prompt": False})
    alice_routed = ScriptedModel([], model_params={"prompt_cache_key": "cache-alice", "session_id": "sess-alice"})
    bob_routed = ScriptedModel([], model_params={"prompt_cache_key": "cache-bob", "session_id": "sess-bob"})
    _, alice_fork = _warm_fork(alice_routed)
    assert _gate(alice_fork)[1] is None
    bob_against_alice = replace(alice_fork, sizer=RequestSizer(bob_routed, "openai"))
    unbound_against_alice = replace(alice_fork, sizer=RequestSizer(ScriptedModel([]), "openai"))
    legacy_answer = replace(
        alice_fork.request.messages[-1],
        provider_data={
            k: v for k, v in (alice_fork.request.messages[-1].provider_data or {}).items() if k != CACHE_ROUTING_KEY
        },
    )
    routed_against_legacy = replace(
        alice_fork,
        request=replace(alice_fork.request, messages=(*alice_fork.request.messages[:-1], legacy_answer)),
    )
    for bad_fork, expected in [
        (_warm_fork(uncached)[1], "prompt_cache_disabled"),
        (
            _warm_fork(native_compaction=NativeCompactionReplay(route="r", threshold=50_000))[1],
            "native_compaction_active",
        ),
        (_warm_fork(output_schema=_StructuredAnswer)[1], "output_schema_set"),
        (_warm_fork(tool_choice="none")[1], "tool_choice_set"),
        (replace(fork, request=replace(fork.request, messages=fork.request.messages[:2])), "no_assistant_anchor"),
        (replace(fork, request=replace(fork.request, messages=older_msgs)), "prefix_not_anchored"),
        (bob_against_alice, "routing_key_mismatch"),
        (unbound_against_alice, "routing_key_mismatch"),
        (routed_against_legacy, "routing_key_mismatch"),
        (_warm_fork(reported=95_000, window=100_000)[1], "exceeds_context_window"),
    ]:
        assert _gate(bad_fork) == (None, expected)

    assert _gate(fork, now=_TS + 239.0)[1] is None
    assert _gate(fork, now=_TS + 240.0) == (None, "cache_cold")
    assert _gate(fork, now=_TS - 1.0) == (None, "cache_cold")
    ext_params = {"extended_cache_time": True, "cache_system_prompt": True}
    ext_model = ScriptedModel(
        [],
        identity=ModelIdentity(id="claude-sonnet-5", provider="Anthropic"),
        model_params=ext_params,
    )
    _, ext_fork = _warm_fork(ext_model)
    assert _gate(ext_fork, now=_TS + 3299.0)[1] is None
    assert _gate(ext_fork, now=_TS + 3300.0) == (None, "cache_cold")


def test_holds_fold_basis_checks_summary_runs_content_and_digests() -> None:
    r1 = _completed_run("r1", messages=[_msg("user", "hello"), _msg("assistant", "world")])
    r2 = _completed_run("r2", messages=[_msg("user", "next"), _msg("assistant", "done")])
    smsg = compaction_summary_message("Prior facts", run_local=False)
    hist = [smsg, replace(r1.messages[0], from_history=True), replace(r1.messages[1], from_history=True)]

    assert holds_fold_basis(hist, basis_summary="Prior facts", basis_runs=[r1], scope=TEST_AGENT_SCOPE)
    assert holds_fold_basis(hist, basis_summary="  Prior facts \n", basis_runs=[r1], scope=TEST_AGENT_SCOPE)
    assert holds_fold_basis(hist[1:], basis_summary=None, basis_runs=[r1], scope=TEST_AGENT_SCOPE)

    url_msg = Message(role="user", content="look", images=[Image(url="https://example.com/a.png")])
    url_run = replace(r1, messages=[url_msg, r1.messages[1]])
    edited = [smsg, replace(r1.messages[0], content="redacted", from_history=True), hist[2]]
    for bad_h, bad_s, bad_r in [
        (hist, "Prior facts", [r1, r2]),
        (hist, "Prior facts", []),
        (hist, "Different", [r1]),
        (hist, None, [r1]),
        (hist[1:], "Prior facts", [r1]),
        ([smsg, smsg, *hist[1:]], "Prior facts", [r1]),
        (edited, "Prior facts", [r1]),
        ([replace(url_msg, from_history=True), hist[2]], None, [url_run]),
    ]:
        assert not holds_fold_basis(bad_h, basis_summary=bad_s, basis_runs=bad_r, scope=TEST_AGENT_SCOPE)


def test_fork_frame_lru_isolation_sanitization_and_eviction(tmp_path: Path) -> None:
    async def custom_on_send(_body: object) -> None:
        pass

    tpl = ModelRequest(
        messages=(_msg("user", "secret"),),
        tools=(_TOOL,),
        retry_budget=object(),
        on_send=custom_on_send,
        prompt_hold_pin=[{"h": 1}],
    )
    key_a = (tmp_path / "a.db", "agent:code", "sess-1")
    remember_fork_frame(key_a, tpl, _SYS)
    found = lookup_fork_frame(key_a)
    assert found is not None
    stored_tpl, stored_sys = found
    assert (stored_tpl.messages, stored_tpl.retry_budget, stored_tpl.prompt_hold_pin) == ((), None, None)
    assert stored_tpl.on_send is not custom_on_send
    assert (stored_tpl.tools, stored_sys) == (tpl.tools, _SYS)
    assert lookup_fork_frame((tmp_path / "b.db", "agent:code", "sess-1")) is None
    assert lookup_fork_frame((tmp_path / "a.db", "agent:other", "sess-1")) is None

    for idx in range(64):
        remember_fork_frame((tmp_path / "a.db", "agent:code", f"extra-{idx}"), tpl, _SYS)
    assert lookup_fork_frame(key_a) is None
    assert lookup_fork_frame((tmp_path / "a.db", "agent:code", "extra-0")) is not None
    remember_fork_frame((tmp_path / "a.db", "agent:code", "extra-64"), tpl, _SYS)
    assert lookup_fork_frame((tmp_path / "a.db", "agent:code", "extra-0")) is not None
    assert lookup_fork_frame((tmp_path / "a.db", "agent:code", "extra-1")) is None


def test_build_scope_fork_constructs_turnless_whole_history_request() -> None:
    run = _completed_run("r1", messages=[_msg("user", "hello"), _msg("assistant", "world")])
    summary = ScopeSummary(text="Earlier", updated_at=datetime.now(UTC))
    fork = build_scope_fork(
        sizer=RequestSizer(ScriptedModel([]), "openai"),
        template=ModelRequest(messages=()),
        system_messages=_SYS,
        context_window=100_000,
        reserve_tokens=4_096,
        scope=TEST_AGENT_SCOPE,
        summary=summary,
        runs=[run],
    )
    assert fork.mid_turn is False
    assert [m.role for m in fork.request.messages] == ["system", "user", "user", "assistant"]
    assert "Earlier" in str(fork.request.messages[1].content)
    assert (fork.request.messages[2].content, fork.request.messages[3].content) == ("hello", "world")


# ---------------------------------------------------------------------------
# Provider wire golden tests
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_wire_anthropic_direct_fork_preserves_prefix_tools_thinking_and_ladder(tmp_path: Path) -> None:
    bodies: list[dict[str, Any]] = []

    def respond(request: httpx.Request) -> httpx.Response:
        bodies.append(json.loads(request.content))
        if len(bodies) == 1:
            u = {"input_tokens": 1_200, "cache_creation_input_tokens": 800, "output_tokens": 20}
            return sse_reply([{"type": "text", "text": "Working."}], stop_reason="end_turn", usage=u)
        u = {"input_tokens": 50, "cache_read_input_tokens": 1_950, "output_tokens": 30}
        return sse_reply(
            [{"type": "text", "text": "## Goal\nForked Anthropic summary"}],
            stop_reason="end_turn",
            usage=u,
        )

    with isolated_provider_environment():
        extra = {
            "api_key": "test-key",
            "thinking": {"type": "adaptive", "display": "summarized"},
            "cache_system_prompt": True,
            "extended_cache_time": True,
        }
        client = httpx2.AsyncClient(transport=httpx2_transport(respond))
        model = pipeline_provider(
            provider_for({"provider": "anthropic", "id": "claude-sonnet-5", "extra_kwargs": extra}, tmp_path, client),
        )
        bg_params = {
            "type": "object",
            "properties": {"command": {"type": "string"}, "background_after": {"type": "integer"}},
            "required": ["command"],
        }
        bg_tool = ToolSpec(name="shell", description="Run shell.", parameters=bg_params, strict=None)
        live = ModelRequest(messages=(*_SYS, _msg("user", "Run tests.")), tools=(bg_tool,))
        turn = await complete(model, live)
        stamp_request_identity(turn.message, model, live)
        summary = await _wire_fork(model, "anthropic", replace(live, messages=(*live.messages, turn.message)))

    assert summary == "## Goal\nForked Anthropic summary"
    live_body, fork_body = bodies
    assert fork_body["tools"] == live_body["tools"]
    assert "background_after" in fork_body["tools"][0]["input_schema"]["properties"]
    assert fork_body["tools"][-1].get("cache_control") == {"type": "ephemeral", "ttl": "1h"}
    assert (fork_body["system"], fork_body["thinking"]) == (live_body["system"], live_body["thinking"])
    assert "tool_choice" not in fork_body
    assert all("cache_control" not in b for b in fork_body["messages"][-1]["content"])
    assert any(isinstance(b, dict) and "cache_control" in b for m in fork_body["messages"][:-1] for b in m["content"])


@pytest.mark.asyncio
async def test_wire_openrouter_claude_prompt_hold_fork_is_read_only(tmp_path: Path) -> None:
    bodies: list[dict[str, Any]] = []
    model_id = "anthropic/claude-opus-5.5"

    def respond(request: Any) -> httpx.Response:  # noqa: ANN401
        bodies.append(json.loads(request.content))
        n = len(bodies)
        text = "## Goal\nHeld fork summary" if n == 3 else f"reply-{n}"
        choice = {"index": 0, "message": {"role": "assistant", "content": text}, "finish_reason": "stop"}
        toks = {"prompt_tokens": 500, "completion_tokens": 20, "total_tokens": 520}
        return streamed_reply({"id": f"gen-{n}", "created": 1, "model": model_id, "choices": [choice], "usage": toks})

    extra = {"api_key": "test-key", "cache_system_prompt": True, "extended_cache_time": True}
    cfg = {"provider": "openrouter", "id": model_id, "extra_kwargs": extra}
    model = pipeline_provider(openai_family_adapter(cfg, tmp_path, respond))
    db_path = tmp_path / "data" / "sessions" / "agent.db"
    db_path.parent.mkdir(parents=True, exist_ok=True)
    hold_path = prompt_hold_path(db_path, "sess-1")
    u1, u2 = _msg("user", "First"), _msg("user", "Second")

    t1 = await complete(
        model,
        ModelRequest(messages=(_msg("system", "System v1."), u1), tools=(_TOOL,), prompt_hold=hold_path),
    )
    m2 = (_msg("system", "System v2."), u1, t1.message, u2)
    t2 = await complete(model, ModelRequest(messages=m2, tools=(_TOOL,), prompt_hold=hold_path))
    assert hold_path.exists()
    os.utime(hold_path, (time.time() - 10.0, time.time() - 10.0))
    before = (hold_path.read_bytes(), hold_path.stat().st_mtime_ns)

    summary = await _wire_fork(
        model,
        "openrouter",
        ModelRequest(messages=(*m2, t2.message), tools=(_TOOL,), prompt_hold=hold_path),
    )
    assert summary == "## Goal\nHeld fork summary"
    assert (hold_path.read_bytes(), hold_path.stat().st_mtime_ns) == before
    fork_body = bodies[2]
    assert fork_body["messages"][0]["content"][0]["text"] == "System v1."
    assert fork_body["messages"][0]["content"][0]["cache_control"] == {"type": "ephemeral", "ttl": "1h"}
    assert any(
        "MindRoom system prompt update." in json.dumps(m.get("content", "")) for m in fork_body["messages"][1:-1]
    )
    assert all("cache_control" not in p for p in fork_body["messages"][-1]["content"])
    assert fork_body["tools"][-1].get("cache_control") == {"type": "ephemeral", "ttl": "1h"}


@pytest.mark.asyncio
async def test_wire_codex_fork_preserves_prompt_cache_key_and_session_id(tmp_path: Path) -> None:
    placeholders, paths = write_cli_logins(tmp_path), runtime_paths_for(tmp_path)
    recorded: list[tuple[dict[str, str], dict[str, Any]]] = []
    unbound_bodies: list[dict[str, Any]] = []

    def respond(request: httpx.Request) -> httpx.Response:
        recorded.append((dict(request.headers), json.loads(request.content)))
        n = len(recorded)
        text = "Initial answer" if n == 1 else "## Goal\nCodex forked summary"
        return _responses_sse(n, text, input_tokens=900, cached_tokens=700)

    model_cfg = ModelConfig(provider="codex", id="gpt-5.4", extra_kwargs={"codex_home": placeholders["<codex_home>"]})

    def make_codex(
        exec_id: ToolExecutionIdentity | None,
        handler: Callable[[httpx.Request], httpx.Response],
    ) -> ModelProvider:
        rt = ProviderRuntime(
            runtime_paths=paths,
            execution_identity=exec_id,
            codex_default_instructions=CODEX_DEFAULT_INSTRUCTIONS,
            http_client=httpx2.AsyncClient(transport=httpx2_transport(handler)),
        )
        return pipeline_provider(build_provider(model_cfg, rt))

    exec_id = ToolExecutionIdentity(
        "matrix",
        "code",
        "@alice:example.org",
        "!room:example.org",
        "$t",
        "$t",
        "!room:example.org:$t",
    )
    active = make_codex(exec_id, respond)
    unbound = make_codex(None, lambda req: (unbound_bodies.append(json.loads(req.content)), respond(req))[1])
    live = ModelRequest(messages=(*_SYS, _msg("user", "Explain caching.")))
    turn = await complete(active, live)
    stamp_request_identity(turn.message, active, live)
    assert (turn.message.provider_data or {}).get(CACHE_ROUTING_KEY) == cache_routing_digest(active.model_params)

    fork = SummaryFork(
        RequestSizer(active, "codex"),
        replace(live, messages=(*live.messages, turn.message)),
        200_000,
        8_192,
        False,
    )
    summarizer = _summarizer(unbound, sid="sess-codex")
    summary, _ = await summarize_runs(
        summarizer,
        None,
        [_completed_run("r1", messages=[live.messages[1], turn.message])],
        fork=fork,
    )
    assert (summary, unbound_bodies) == ("## Goal\nCodex forked summary", [])
    (live_h, live_b), (fork_h, fork_b) = recorded
    assert fork_b["prompt_cache_key"] == live_b["prompt_cache_key"]
    assert fork_b["prompt_cache_key"]
    assert fork_h.get("session_id") == live_h.get("session_id")
    assert fork_h.get("session_id")


@pytest.mark.asyncio
async def test_wire_gemini_mid_turn_fork_preserves_tools_and_function_response_order(tmp_path: Path) -> None:
    bodies: list[dict[str, Any]] = []

    def respond(request: httpx.Request) -> httpx.Response:
        bodies.append(json.loads(request.content))
        cand = {
            "content": {"role": "model", "parts": [{"text": "## Goal\nGemini mid-turn summary"}]},
            "finishReason": "STOP",
        }
        meta = {
            "promptTokenCount": 800,
            "cachedContentTokenCount": 600,
            "candidatesTokenCount": 20,
            "totalTokenCount": 820,
        }
        return streamed_reply({"candidates": [cand], "usageMetadata": meta})

    cfg = {"provider": "google", "id": "gemini-3.1-pro-preview", "api_key": "test-key"}
    model = pipeline_provider(build_gemini_adapter(cfg, tmp_path, respond))
    w_params = {"type": "object", "properties": {"city": {"type": "string"}}, "required": ["city"]}
    weather_tool = ToolSpec(name="get_weather", description="Get weather", parameters=w_params, strict=None)
    prefix = ModelRequest(messages=(*_SYS, _msg("user", "Weather in Paris?")), tools=(weather_tool,))
    call_msg = Message(
        role="assistant",
        content=None,
        tool_calls=[tool_call("get_weather", "call_1", city="Paris")],
        created_at=int(time.time()),
        metrics=MessageMetrics(tokens=TokenCounts(input_tokens=600, output_tokens=15)),
        provider_data=model_provenance(model.identity.id, model.identity.provider),
    )
    stamp_request_identity(call_msg, model, prefix)
    tool_msg = Message(role="tool", content="Sunny 22C", tool_call_id="call_1", tool_name="get_weather")

    summary = await _wire_fork(
        model,
        "google",
        replace(prefix, messages=(*prefix.messages, call_msg, tool_msg)),
        mid_turn=True,
        reserve=8_192,
    )
    assert summary == "## Goal\nGemini mid-turn summary"
    [fork_body] = bodies
    assert fork_body["tools"][0]["functionDeclarations"][0]["name"] == "get_weather"
    assert (fork_body.get("generationConfig") or {}).get("responseMimeType") != "application/json"
    last = fork_body["contents"][-1]
    assert (last["role"], "text" in last["parts"][0], "functionResponse" in last["parts"][-1]) == ("user", True, True)


@pytest.mark.asyncio
async def test_wire_openai_responses_stored_pre_reply_fork_preserves_portable_replay(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    bodies: list[dict[str, Any]] = []

    def respond(request: httpx.Request) -> httpx.Response:
        bodies.append(json.loads(request.content))
        n = len(bodies)
        text = "Stored answer" if n == 1 else "## Goal\nResponses portable fork summary"
        return _responses_sse(n, text, input_tokens=6_000)

    model = pipeline_provider(
        openai_family_adapter(
            {
                "provider": "openai",
                "id": "gpt-5.4",
                "extra_kwargs": {"api_key": "test-key", "store": True},
            },
            tmp_path,
            respond,
        ),
    )
    live = ModelRequest(messages=(*_SYS, _msg("user", "Remember alpha.")), portable_replay=True)
    turn1 = await complete(model, live)
    stamp_request_identity(turn1.message, model, live)
    assert (turn1.message.provider_data or {}).get("response_id") == "resp_1"
    assert (turn1.message.provider_data or {}).get("mindroom_response_stored") is True

    comp = CompactionConfig(enabled=True, reserve_tokens=1_000, replay_window_tokens=10_000, threshold_tokens=5_000)
    models = {
        "default": ModelConfig(
            provider="openai",
            id="gpt-5.4",
            context_window=100_000,
            extra_kwargs={"store": True},
        ),
    }
    config, paths = _make_config(tmp_path / "cfg", defaults_compaction=comp, models=models)
    storage = create_session_storage("test_agent", config, paths, execution_identity=None)
    seed_session(
        storage,
        _session("sess-resp", runs=[_completed_run("r1", messages=[live.messages[1], turn1.message])]),
    )
    xml_calls = SummaryCalls()
    monkeypatch.setattr("mindroom.history.summarizer.generate_compaction_summary", xml_calls)
    install_model_providers(monkeypatch, {"default": ScriptedModel([], identity=model.identity)})
    frame = RequestFrame(ModelRequest(messages=()), _SYS, TurnMessages(prompt=_msg("user", "Turn 2")))
    prepared = await prepare_scope_history(
        sizer=RequestSizer(model, "openai"),
        frame=frame,
        resolved_inputs=_inputs(config),
        runtime_paths=paths,
        config=config,
        scope_context=_scope_ctx(storage, "sess-resp", config, paths),
        compaction_lifecycle=RecordingCompactionLifecycle(),
        pipeline_timing=None,
        allow_native_compaction=False,
    )

    assert (prepared.compaction_reply_outcome, xml_calls.inputs, len(bodies)) == ("success", [], 2)
    fork_body = bodies[1]
    assert "previous_response_id" not in fork_body
    serialized_input = json.dumps(fork_body["input"])
    assert "Remember alpha." in serialized_input
    assert "Stored answer" in serialized_input
    remembered = lookup_fork_frame((storage.path, TEST_AGENT_SCOPE.key, "sess-resp"))
    assert remembered is not None
    assert remembered[0].portable_replay is True


# ---------------------------------------------------------------------------
# Entrypoint integration tests: pre-reply auto, live /compact, mid-turn
# ---------------------------------------------------------------------------


async def _run_pre_reply(
    tmp_path: Path,
    mp: pytest.MonkeyPatch,
    active: ModelProvider,
    run: RunRecord,
    sid: str,
    xml_calls: SummaryCalls,
    comp_model: str | None = None,
) -> tuple[Any, Any, list[dict[str, Any]]]:
    config, paths, storage = _fork_config(
        tmp_path,
        reserve_tokens=1_000,
        replay_window_tokens=10_000,
        threshold_tokens=5_000,
    )
    mp.setattr("mindroom.history.summarizer.generate_compaction_summary", xml_calls)
    _install_default(mp, active, other=summary_model(model_id="other-model", provider="openai"))
    seed_session(storage, _session(sid, runs=[run]))
    frame = RequestFrame(ModelRequest(messages=(), tools=(_TOOL,)), _SYS, TurnMessages(prompt=_msg("user", "Turn 2")))
    with capture_logs() as logs:
        prepared = await prepare_scope_history(
            sizer=RequestSizer(active, "openai"),
            frame=frame,
            resolved_inputs=_inputs(config, compaction_model=comp_model),
            runtime_paths=paths,
            config=config,
            scope_context=_scope_ctx(storage, sid, config, paths),
            compaction_lifecycle=RecordingCompactionLifecycle(),
            pipeline_timing=None,
            allow_native_compaction=False,
        )
    return prepared, storage, logs


@pytest.mark.asyncio
async def test_pre_reply_auto_compaction_fork_hit(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    fork_usage = MessageMetrics(tokens=TokenCounts(input_tokens=6_200, output_tokens=40, cache_read_tokens=5_900))
    turn = summary_turn("## Goal\nPre-reply fork summary", usage=fork_usage)
    active, xml_calls = ScriptedModel([[UsageReported(fork_usage), turn]]), SummaryCalls()
    run = _warm_run(active, ModelRequest(messages=(), tools=(_TOOL,)), 6_000)
    prepared, storage, logs = await _run_pre_reply(tmp_path, monkeypatch, active, run, "sess-warm", xml_calls)
    assert (prepared.compaction_reply_outcome, xml_calls.inputs, len(active.requests)) == ("success", [], 1)
    assert active.requests[0].tools == (_TOOL,)
    assert all("Turn 2" not in str(m.content) for m in active.requests[0].messages)
    assert summary_rows_of(storage, TEST_AGENT_SCOPE.key, "sess-warm")[-1].summary == "## Goal\nPre-reply fork summary"
    assert any(entry.get("compaction_strategy") == "fork" and entry.get("cache_read_tokens") == 5_900 for entry in logs)


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("case", "expected"),
    [("stale", "cache_cold"), ("redacted", "prefix_not_anchored"), ("different_model", "different_model")],
)
async def test_pre_reply_auto_compaction_fork_fallbacks(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    case: str,
    expected: str,
) -> None:
    active, xml_calls = ScriptedModel([]), SummaryCalls(summary=f"xml-{case}")
    ts = int(time.time()) - 400 if case == "stale" else int(time.time())
    run = _warm_run(active, ModelRequest(messages=(), tools=(_TOOL,)), 6_000, ts, f"r-{case}")
    if case == "redacted":
        run = replace(run, messages=[replace(run.messages[0], content="edited " * 4_000), run.messages[1]])
    comp_model = "other" if case == "different_model" else None
    prepared, _, logs = await _run_pre_reply(tmp_path, monkeypatch, active, run, f"sess-{case}", xml_calls, comp_model)
    assert (prepared.compaction_reply_outcome, len(xml_calls.inputs), active.requests) == ("success", 1, [])
    assert any(entry.get("fork_skip_reason") == expected for entry in logs)


@pytest.mark.asyncio
async def test_pre_reply_auto_compaction_skips_fork_for_different_requester_routing_key(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    alice_model = ScriptedModel(
        [answer("Alice answer", metrics=usage(6_000, 20))],
        model_params={"prompt_cache_key": "cache-alice", "session_id": "sess-alice"},
    )
    bob_model = ScriptedModel([], model_params={"prompt_cache_key": "cache-bob", "session_id": "sess-bob"})
    search_tool = ToolFunction(
        name="search",
        description="Search",
        parameters={"type": "object"},
        entrypoint=lambda: "",
    )
    alice_spec = AgentSpec(
        id="test_agent",
        name="test_agent",
        model=alice_model,
        system_messages=_SYS,
        toolkits=(AdaptedToolkit(name="search_kit", functions=(search_tool,), instructions=None),),
    )
    turn = TurnMessages(prompt=_msg("user", "Alice prompt"))
    req = build_request(summary=None, history=[], turn=turn, media=MediaInputs())
    events = run_agent(
        alice_spec,
        req,
        ctx=RunContext(run_id=str(uuid4()), session=UnstoredSession("sess-shared-pre-reply")),
        persist=None,
        request_template=ModelRequest(messages=()),
    )
    finished = [event async for event in events][-1]
    assert isinstance(finished, RunFinished)
    assert isinstance(finished.record, RunRecord)
    alice_answer = finished.record.messages[-1]
    assert (alice_answer.provider_data or {}).get(CACHE_ROUTING_KEY) == cache_routing_digest(
        alice_model.model_params,
    )

    stored_run = _completed_run("r-alice", messages=[turn.prompt, alice_answer])
    xml_calls = SummaryCalls(summary="xml-for-bob-pre-reply")
    prepared, storage, logs = await _run_pre_reply(
        tmp_path,
        monkeypatch,
        bob_model,
        stored_run,
        "sess-shared-pre-reply",
        xml_calls,
    )
    assert (prepared.compaction_reply_outcome, len(xml_calls.inputs), bob_model.requests) == ("success", 1, [])
    assert any(entry.get("fork_skip_reason") == "routing_key_mismatch" for entry in logs)
    assert (
        summary_rows_of(storage, TEST_AGENT_SCOPE.key, "sess-shared-pre-reply")[-1].summary == "xml-for-bob-pre-reply"
    )


@pytest.mark.asyncio
async def test_live_compact_scope_now_warm_hit_and_cold_or_drifted_fallback(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    config, paths, storage = _fork_config(tmp_path, reserve_tokens=1_000, replay_window_tokens=20_000)
    template, inputs = ModelRequest(messages=()), _inputs(config)

    async def compact_live(
        sid: str,
        model: ScriptedModel,
        xml_calls: SummaryCalls,
        remembered: tuple[Message, ...] | None,
    ) -> bool:
        clear_fork_frames()
        monkeypatch.setattr("mindroom.history.summarizer.generate_compaction_summary", xml_calls)
        _install_default(monkeypatch, model)
        seed_session(storage, _session(sid, runs=[_warm_run(model, template, 4_000, run_id=f"r-{sid}")]))
        sess = storage.load_session(sid)
        assert sess is not None
        set_force_compaction(storage, session=sess, forced=True)
        if remembered is not None:
            remember_fork_frame((storage.path, TEST_AGENT_SCOPE.key, sid), template, remembered)
        ctx = _scope_ctx(storage, sid, config, paths)
        return await compact_scope_now(ctx, inputs, lambda: model, RecordingCompactionLifecycle(), config, paths)

    active, xml_calls = ScriptedModel([summary_steps("## Goal\nLive /compact fork summary")]), SummaryCalls()
    assert await compact_live("sess-live", active, xml_calls, _SYS) is False
    assert (xml_calls.inputs, len(active.requests)) == ([], 1)
    assert (
        summary_rows_of(storage, TEST_AGENT_SCOPE.key, "sess-live")[-1].summary == "## Goal\nLive /compact fork summary"
    )
    assert read_scope_state(storage.load_session("sess-live")).force_compact_before_next_run is False

    for mode, remembered in [("cold", None), ("drifted", (_msg("system", "Drifted."),))]:
        model, xml_calls = ScriptedModel([]), SummaryCalls(summary=f"xml-{mode}")
        await compact_live(f"sess-live-{mode}", model, xml_calls, remembered)
        assert (len(xml_calls.inputs), model.requests) == (1, [])


@pytest.mark.asyncio
async def test_live_compact_scope_now_skips_fork_for_different_requester_routing_key(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    config, paths, storage = _fork_config(tmp_path, reserve_tokens=1_000, replay_window_tokens=20_000)
    template, inputs = ModelRequest(messages=()), _inputs(config)
    alice_model = ScriptedModel([], model_params={"prompt_cache_key": "cache-alice", "session_id": "sess-alice"})
    bob_model = ScriptedModel([], model_params={"prompt_cache_key": "cache-bob", "session_id": "sess-bob"})

    # Alice's reply populates the session and remembered fork frame under Alice's cache routing key.
    seed_session(storage, _session("sess-shared", runs=[_warm_run(alice_model, template, 4_000, run_id="r-alice")]))
    sess = storage.load_session("sess-shared")
    assert sess is not None
    set_force_compaction(storage, session=sess, forced=True)
    remember_fork_frame((storage.path, TEST_AGENT_SCOPE.key, "sess-shared"), template, _SYS)

    # Bob triggers /compact with a different requester-derived (prompt_cache_key, session_id) -> falls back to XML.
    xml_calls = SummaryCalls(summary="xml-for-bob")
    monkeypatch.setattr("mindroom.history.summarizer.generate_compaction_summary", xml_calls)
    _install_default(monkeypatch, bob_model)
    ctx = _scope_ctx(storage, "sess-shared", config, paths)
    with capture_logs() as logs:
        empty = await compact_scope_now(
            ctx,
            inputs,
            lambda: bob_model,
            RecordingCompactionLifecycle(),
            config,
            paths,
        )
    assert (empty, len(xml_calls.inputs), bob_model.requests) == (False, 1, [])
    assert any(entry.get("fork_skip_reason") == "routing_key_mismatch" for entry in logs)
    assert summary_rows_of(storage, TEST_AGENT_SCOPE.key, "sess-shared")[-1].summary == "xml-for-bob"


@pytest.mark.asyncio
async def test_compact_scope_now_sizes_with_active_model_name_when_compaction_model_differs(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    config, paths, storage = _fork_config(tmp_path, reserve_tokens=1_000)
    template, inputs = ModelRequest(messages=()), _inputs(config, compaction_model="other")
    assert inputs.execution_plan.active_model_name == "default"
    assert inputs.execution_plan.compaction_model_name == "other"
    active = ScriptedModel([], identity=ModelIdentity(id="scripted-model", provider="openai"))
    summary_provider = ScriptedModel([], identity=ModelIdentity(id="other-model", provider="openai"))
    install_model_providers(monkeypatch, {"default": active, "other": summary_provider})
    monkeypatch.setattr("mindroom.history.summarizer.generate_compaction_summary", SummaryCalls(summary="xml"))
    sizer_model_names: list[str] = []

    def spy_sizer(model: Any, cfg: Any, model_name: str) -> RequestSizer:  # noqa: ANN401
        sizer_model_names.append(model_name)
        return request_sizer(model, cfg, model_name)

    monkeypatch.setattr("mindroom.history.runtime.request_sizer", spy_sizer)
    seed_session(storage, _session("sess-alias", runs=[_warm_run(active, template, 4_000, run_id="r-alias")]))
    sess = storage.load_session("sess-alias")
    assert sess is not None
    set_force_compaction(storage, session=sess, forced=True)
    remember_fork_frame((storage.path, TEST_AGENT_SCOPE.key, "sess-alias"), template, _SYS)
    await compact_scope_now(
        _scope_ctx(storage, "sess-alias", config, paths),
        inputs,
        lambda: active,
        RecordingCompactionLifecycle(),
        config,
        paths,
    )
    assert sizer_model_names == ["default"]


def _probe_tool() -> str:
    """Return one large tool result."""
    return TOOL_RESULT


async def _run_mid_turn(
    mp: pytest.MonkeyPatch,
    env: tuple[Any, Any, Any],
    model: ModelProvider,
    sid: str,
    *,
    scoped: bool,
    xml_calls: SummaryCalls,
    tools: list[Any] | None = None,
    media: MediaInputs | None = None,
) -> RunRecord:
    config, paths, store = env
    storage = store if scoped else None
    mp.setattr("mindroom.history.summarizer.generate_compaction_summary", xml_calls)
    _install_default(mp, model)
    session = (storage.load_session(sid) if storage else None) or new_scope_session(
        session_id=sid,
        scope=TEST_AGENT_SCOPE,
    )
    settings = config.resolve_scope("test_agent").history_settings
    history = (
        scope_history_messages(
            session.runs,
            scope=TEST_AGENT_SCOPE,
            num_history_runs=None,
            num_history_messages=None,
            max_tool_calls_from_history=None,
        )
        if scoped
        else []
    )
    turn = TurnMessages(prompt=_msg("user", "Do the work"))
    run_store = RunStorage(store=storage, session=session, scope=TEST_AGENT_SCOPE) if storage else None
    replay = configured_replay_plan(history_settings=settings, estimated_tokens=0) if scoped else None
    hook = mid_turn_compaction_hook(
        config=config,
        runtime_paths=paths,
        scope=TEST_AGENT_SCOPE,
        model_name="default",
        model=model,
        history_settings=settings,
        storage=run_store,
        replay_plan=replay,
        prompt_id=turn.prompt.id,
        lifecycle=RecordingCompactionLifecycle(),
        turn_recorder=None,
    )
    funcs = tuple(
        ToolFunction(
            name=fn.__name__,
            description=fn.__doc__,
            parameters={"type": "object", "properties": {}},
            entrypoint=fn,
        )
        for fn in (tools or [_probe_tool])
    )
    toolkit = AdaptedToolkit(name="probes", functions=funcs, instructions=None)
    spec = AgentSpec(id="test_agent", name="test_agent", model=model, system_messages=_SYS, toolkits=(toolkit,))
    req = build_request(
        summary=session.summary if scoped else None,
        history=history,
        turn=turn,
        media=media or MediaInputs(),
    )
    persist = persist_to(storage, session) if storage else None
    events = run_agent(
        spec,
        req,
        ctx=RunContext(run_id=str(uuid4()), session=UnstoredSession(sid)),
        hooks=LoopHooks(before_request=(hook,)),
        persist=persist,
        request_template=ModelRequest(messages=()),
    )
    finished = [event async for event in events][-1]
    assert isinstance(finished, RunFinished)
    assert isinstance(finished.record, RunRecord)
    return finished.record


@pytest.mark.asyncio
async def test_mid_turn_scoped_and_run_local_fork_compaction(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    env = _fork_config(tmp_path, reserve_tokens=500, replay_window_tokens=2_500)
    storage = env[2]

    # 1. Scoped mid-turn: Fold 1 and Fold 2 both exceed replay limit (2k) while fitting 100k -> both fork cache-warm.
    xml_calls = SummaryCalls()
    scoped_turns = [
        [*calls(tool_call("_probe_tool", "c1"), metrics=usage(1_800, 20))],
        summary_steps("## Goal\nFold 1 forked summary"),
        [*calls(tool_call("_probe_tool", "c2"), metrics=usage(1_800, 20))],
        summary_steps("## Goal\nFold 2 forked summary"),
        [*answer("All finished", metrics=usage(400, 10))],
    ]
    model = ScriptedModel(scoped_turns)
    record = await _run_mid_turn(monkeypatch, env, model, "sess-mid-scoped", scoped=True, xml_calls=xml_calls)
    assert (record.content, xml_calls.inputs, len(model.requests)) == ("All finished", [], 5)
    for idx in (1, 3):
        assert model.requests[idx].tools
        assert is_transient_context(model.requests[idx].messages[-1].content)
    rows = [r.summary for r in summary_rows_of(storage, TEST_AGENT_SCOPE.key, "sess-mid-scoped")]
    assert rows == ["## Goal\nFold 1 forked summary", "## Goal\nFold 2 forked summary"]
    assert lookup_fork_frame((storage.path, TEST_AGENT_SCOPE.key, "sess-mid-scoped")) is not None

    # 2. Scoped basis mismatch: concurrent run lands in storage during tool call -> skips fork, falls back to XML.
    xml_calls = SummaryCalls(summary="xml-after-mismatch")
    seed_session(storage, _session("sess-mismatch", runs=[_completed_run("r-old")]))
    mismatch = ScriptedModel(
        [
            [*calls(tool_call("probe_concurrent", "c3"), metrics=usage(1_800, 20))],
            [*answer("OK", metrics=usage(400, 10))],
        ],
    )

    def probe_concurrent() -> str:
        """Return a large result while another run lands in storage."""
        current = storage.load_session("sess-mismatch")
        assert current is not None
        storage.save_run(current, _completed_run("r-concurrent"), record_usage=False)
        return TOOL_RESULT

    with capture_logs() as logs:
        await _run_mid_turn(
            monkeypatch,
            env,
            mismatch,
            "sess-mismatch",
            scoped=True,
            xml_calls=xml_calls,
            tools=[probe_concurrent],
        )
    assert (len(xml_calls.inputs), len(mismatch.requests)) == (1, 2)
    assert any(entry.get("fork_skip_reason") == "fold_basis_mismatch" for entry in logs)

    # 3. Run-local mid-turn fork hit.
    xml_calls = SummaryCalls()
    local_turns = [
        [*calls(tool_call("_probe_tool", "c1"), metrics=usage(1_800, 20))],
        summary_steps("## Goal\nRun-local forked summary"),
        [*answer("Local done", metrics=usage(400, 10))],
    ]
    local_model = ScriptedModel(local_turns)
    local_rec = await _run_mid_turn(monkeypatch, env, local_model, "sess-local", scoped=False, xml_calls=xml_calls)
    assert (local_rec.content, xml_calls.inputs, len(local_model.requests)) == ("Local done", [], 3)
    assert is_transient_context(local_model.requests[1].messages[-1].content)
    assert any(
        is_compaction_summary(m) and "Run-local forked summary" in str(m.content)
        for m in local_model.requests[2].messages
    )


@pytest.mark.asyncio
async def test_mid_turn_fork_with_inline_media_does_not_retry_media_dropped_on_context_window_exceeded(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    env = _fork_config(tmp_path, reserve_tokens=500, replay_window_tokens=6_000)
    storage = env[2]
    xml_calls = SummaryCalls(summary="xml-after-media-cwe")
    inner = ScriptedModel(
        [
            [*calls(tool_call("_probe_tool", "c1"), metrics=usage(5_200, 20))],
            [_raise_step(ContextWindowExceeded("prompt too long", provider="Synthetic", status_code=400))],
            answer("Finished after XML fallback", metrics=usage(400, 10)),
        ],
    )
    active = pipeline_provider(inner)
    chart = Image(id="att_chart", content=b"PNG-chart-bytes", mime_type="image/png")
    record = await _run_mid_turn(
        monkeypatch,
        env,
        active,
        "sess-mid-media-cwe",
        scoped=True,
        xml_calls=xml_calls,
        media=MediaInputs(images=[chart]),
    )
    fork_requests = [req for req in inner.requests if req.overrides.transient_retries == "caller"]
    assert len(fork_requests) == 1
    assert any(message.images for message in fork_requests[0].messages)
    assert (len(inner.requests), len(xml_calls.inputs), record.content) == (3, 1, "Finished after XML fallback")
    assert summary_rows_of(storage, TEST_AGENT_SCOPE.key, "sess-mid-media-cwe")[-1].summary == "xml-after-media-cwe"


@pytest.mark.asyncio
async def test_mid_turn_run_local_oversized_fork_falls_back_to_xml(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    env = _fork_config(tmp_path, reserve_tokens=500, replay_window_tokens=2_500)
    xml_calls = SummaryCalls(summary="## Goal\nConcise XML summary")
    model = ScriptedModel(
        [
            [*calls(tool_call("_probe_tool", "c1"), metrics=usage(1_800, 20))],
            summary_steps("word " * 2_000),
            [*answer("Recovered concisely", metrics=usage(400, 10))],
        ],
    )
    with capture_logs() as logs:
        record = await _run_mid_turn(monkeypatch, env, model, "sess-local-oversized", scoped=False, xml_calls=xml_calls)

    assert (record.content, len(xml_calls.inputs), len(model.requests)) == ("Recovered concisely", 1, 3)
    assert any(entry.get("fork_fallback_reason") == "exceeds_max_summary_tokens" for entry in logs)
    assert any(is_compaction_summary(m) and "Concise XML summary" in str(m.content) for m in model.requests[2].messages)


# ---------------------------------------------------------------------------
# Fallback matrix, usage warning & fatal error propagation tests
# ---------------------------------------------------------------------------


def _fork_case(
    steps: list[Any],
    xml_primary: ScriptedModel,
    *,
    xml_fallback: ScriptedModel | None = None,
    on_usage: Any = None,  # noqa: ANN401
    timeout_s: float = 10.0,
) -> tuple[ScriptedModel, Summarizer, SummaryFork, list[RunRecord]]:
    fork_model = ScriptedModel([steps], identity=_ID)
    run = _warm_run(fork_model, ModelRequest(messages=()), 1_000)
    fork = SummaryFork(
        RequestSizer(fork_model, "openai"),
        ModelRequest(messages=(*_SYS, *run.messages)),
        100_000,
        4_096,
        False,
    )
    return (
        fork_model,
        _summarizer(xml_primary, fallback=xml_fallback, on_usage=on_usage, timeout_s=timeout_s),
        fork,
        [run],
    )


@pytest.mark.asyncio
async def test_fork_fallback_to_primary_xml() -> None:
    recorded: list[tuple[ModelIdentity, MessageMetrics]] = []

    async def capture_usage(model_id: ModelIdentity, metrics: MessageMetrics) -> None:
        recorded.append((model_id, metrics))

    tc = tool_call("search", "call_1")
    billed = MessageMetrics(tokens=TokenCounts(input_tokens=1_000, output_tokens=15, cache_read_tokens=900))
    tc_turn = TurnCompleted(
        message=Message(role="assistant", content="## Goal\nText", tool_calls=[tc], metrics=billed),
        stop_reason="end",
    )
    cases: list[tuple[list[Any], str, str, int | None, Any]] = [
        ([UsageReported(billed), ToolCallReady(tc), tc_turn], "xml-tool", "tool_call", None, capture_usage),
        (summary_steps(summary_turn("truncated", stop_reason="output_limit")), "xml-out", "output_limit", None, None),
        (
            [_raise_step(ContextWindowExceeded("too long", provider="openai"))],
            "xml-ctx",
            "context_window_exceeded",
            None,
            None,
        ),
        (summary_steps("word " * 500), "concise", "exceeds_max_summary_tokens", 80, None),
    ]
    for steps, expected_xml, expected_reason, max_tokens, on_usage in cases:
        xml_primary = summary_model(expected_xml, model_id=_ID.id, provider=_ID.provider)
        fork_model, summarizer, fork, runs = _fork_case(steps, xml_primary, on_usage=on_usage)
        with capture_logs() as logs:
            summary, _ = await summarize_runs(
                summarizer,
                None,
                runs,
                fork=fork,
                max_summary_tokens=max_tokens,
                model=fork_model,
            )
        assert (summary, len(xml_primary.requests)) == (expected_xml, 1)
        assert any(entry.get("fork_fallback_reason") == expected_reason for entry in logs)

    assert (len(recorded), recorded[0][1].tokens.cache_read_tokens) == (1, 900)


@pytest.mark.asyncio
async def test_fork_fallback_refusal_and_timeout_budget() -> None:
    refusal = ModelSafeguardRefusal(
        provider="anthropic",
        usage=MessageMetrics(tokens=TokenCounts(input_tokens=1_000, output_tokens=5)),
    )

    # 1. ModelSafeguardRefusal WITH fallback -> jumps straight to fallback XML (never calls primary XML).
    xml_primary = summary_model("unused", model_id=_ID.id, provider=_ID.provider)
    xml_fallback = summary_model("xml-from-fallback", model_id="fallback-model", provider="openai")
    _, summarizer, fork, runs = _fork_case([_raise_step(refusal)], xml_primary, xml_fallback=xml_fallback)
    with capture_logs() as logs:
        summary, served = await summarize_runs(summarizer, None, runs, fork=fork)
    assert (summary, served.name, xml_primary.requests, len(xml_fallback.requests)) == (
        "xml-from-fallback",
        "fallback",
        [],
        1,
    )
    assert any(entry.get("fork_fallback_reason") == "safeguard_refusal" for entry in logs)

    # 2. ModelSafeguardRefusal WITHOUT fallback and fork timeout both bound XML fallback to max_attempts=1.
    for steps, timeout_s, expected_reason in [
        ([_raise_step(refusal)], 10.0, "safeguard_refusal"),
        ([Await(lambda: asyncio.sleep(0.05)), *summary_steps("late")], 0.01, "timeout"),
    ]:
        err = ProviderUnavailable("transient", provider="openai")
        xml_primary = summary_model(err, "no-retry", model_id=_ID.id, provider=_ID.provider)
        _, summarizer, fork, runs = _fork_case(steps, xml_primary, timeout_s=timeout_s)
        with capture_logs() as logs, pytest.raises(ProviderUnavailable):
            await summarize_runs(summarizer, None, runs, fork=fork)
        assert len(xml_primary.requests) == 1
        assert any(entry.get("fork_fallback_reason") == expected_reason for entry in logs)


@pytest.mark.asyncio
async def test_fork_usage_save_failure_warns_and_fatal_errors_propagate() -> None:
    metrics = MessageMetrics(tokens=TokenCounts(input_tokens=1_000, output_tokens=20, cache_read_tokens=800))
    xml_primary = summary_model("never", model_id=_ID.id, provider=_ID.provider)
    _, summarizer, fork, runs = _fork_case(
        summary_steps(summary_turn("fork-ok", usage=metrics)),
        xml_primary,
        on_usage=_broken_usage,
    )
    with capture_logs() as logs:
        summary, _ = await summarize_runs(summarizer, None, runs, fork=fork)
    assert (summary, xml_primary.requests) == ("fork-ok", [])
    assert any(entry.get("event") == "compaction_summary_usage_save_failed" for entry in logs)
    assert any(entry.get("compaction_strategy") == "fork" and entry.get("cache_read_tokens") == 800 for entry in logs)

    xml_primary = summary_model("never", model_id=_ID.id, provider=_ID.provider)
    _, summarizer, fork, runs = _fork_case(
        [_raise_step(AuthenticationFailed("bad key", provider="openai"))],
        xml_primary,
    )
    with pytest.raises(AuthenticationFailed):
        await summarize_runs(summarizer, None, runs, fork=fork)
    assert xml_primary.requests == []

    started = asyncio.Event()
    xml_primary = summary_model("never", model_id=_ID.id, provider=_ID.provider)
    _, summarizer, fork, runs = _fork_case([_wait_step(started)], xml_primary)
    task = asyncio.create_task(summarize_runs(summarizer, None, runs, fork=fork))
    await started.wait()
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    assert xml_primary.requests == []


@pytest.mark.asyncio
async def test_fork_concurrent_conversation_change_aborts_before_persist(tmp_path: Path) -> None:
    _, _, storage = _fork_config(tmp_path)
    template, concurrent_fork = ModelRequest(messages=()), ScriptedModel([])
    seed_session(storage, _session("sess-race", runs=[_warm_run(concurrent_fork, template, 1_000)]))
    loaded = storage.load_session("sess-race")
    assert loaded is not None

    async def mutate_store() -> None:
        delete_runs(storage, ["r1"], session_id="sess-race")

    concurrent_fork.turns.append([Await(mutate_store), *summary_steps("stale fork summary")])
    xml_primary = summary_model("never", model_id=_ID.id, provider=_ID.provider)
    fork = build_scope_fork(
        sizer=RequestSizer(concurrent_fork, "openai"),
        template=template,
        system_messages=_SYS,
        context_window=100_000,
        reserve_tokens=4_096,
        scope=TEST_AGENT_SCOPE,
        summary=loaded.summary,
        runs=loaded.runs,
    )
    summarizer = _summarizer(xml_primary, sid="sess-race")

    async def complete_outcome(outcome: Any) -> Any:  # noqa: ANN401
        return outcome

    with pytest.raises(RuntimeError, match="changed during compaction"):
        await compact_scope_history(
            storage=storage,
            session=loaded,
            mode="auto",
            summarizer=summarizer,
            available_history_budget=50_000,
            max_summary_tokens=50_000,
            before_tokens=1_000,
            replay_window_tokens=100_000,
            threshold_tokens=80_000,
            progress=None,
            complete=complete_outcome,
            model=concurrent_fork,
            fork=fork,
        )
    assert (xml_primary.requests, summary_rows_of(storage, TEST_AGENT_SCOPE.key, "sess-race")) == ([], [])


@pytest.mark.asyncio
async def test_passing_warm_fork_summary_is_tokenized_only_once(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A warm fork summary passing max_summary_tokens is validated once in summarizer, not re-tokenized by callers."""
    from mindroom.history.replay import estimate_summary_text_tokens  # noqa: PLC0415

    _, _, storage = _fork_config(tmp_path)
    template = ModelRequest(messages=())
    fork_model = ScriptedModel([summary_steps("## Goal\nConcise fork summary")], identity=_ID)
    seed_session(storage, _session("sess-once", runs=[_warm_run(fork_model, template, 1_000)]))
    loaded = storage.load_session("sess-once")
    assert loaded is not None
    xml_primary = summary_model("never", model_id=_ID.id, provider=_ID.provider)
    fork = build_scope_fork(
        sizer=RequestSizer(fork_model, "openai"),
        template=template,
        system_messages=_SYS,
        context_window=100_000,
        reserve_tokens=4_096,
        scope=TEST_AGENT_SCOPE,
        summary=loaded.summary,
        runs=loaded.runs,
    )
    calls_count = 0

    def counted_estimate(*args: Any, **kwargs: Any) -> int:  # noqa: ANN401
        nonlocal calls_count
        calls_count += 1
        return estimate_summary_text_tokens(*args, **kwargs)

    monkeypatch.setattr("mindroom.history.summarizer.estimate_summary_text_tokens", counted_estimate)

    async def complete_outcome(outcome: Any) -> Any:  # noqa: ANN401
        return outcome

    outcome = await compact_scope_history(
        storage=storage,
        session=loaded,
        mode="auto",
        summarizer=_summarizer(xml_primary, sid="sess-once"),
        available_history_budget=50_000,
        max_summary_tokens=50_000,
        before_tokens=1_000,
        replay_window_tokens=100_000,
        threshold_tokens=80_000,
        progress=None,
        complete=complete_outcome,
        model=fork_model,
        fork=fork,
    )
    assert outcome is not None
    assert calls_count == 1
    assert xml_primary.requests == []
