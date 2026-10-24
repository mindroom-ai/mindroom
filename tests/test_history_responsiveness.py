"""History sizing must leave the loop available without changing replay counts."""

from __future__ import annotations

import asyncio
import threading
from copy import deepcopy
from functools import partial
from typing import TYPE_CHECKING, Any
from unittest.mock import AsyncMock

import pytest

from mindroom.execution_preparation import _finalize_prepared_history
from mindroom.history.compaction import SummaryModel, _summarize_chunk, compact_scope_history
from mindroom.history.runtime import (
    PreparedScopeHistory,
    _compact_before_reply,
    _run_scope_compaction_with_lifecycle,
    prepare_scope_history,
    resolve_entity_preparation_inputs,
)
from mindroom.history.session_context import ScopeSessionContext
from mindroom.history.storage import _with_scope_state, append_compaction_chunk, read_chunk_basis
from mindroom.history.summary_call import CompactionSummaryOutputLimitError
from mindroom.history.types import HistoryScope, HistoryScopeState
from mindroom.llm.errors import ModelSafeguardRefusal
from mindroom.llm.messages import Message
from mindroom.providers.openai.responses import ResponsesAdapter
from mindroom.session_storage.agent_storage import create_state_storage
from mindroom.token_budget import estimate_compaction_input_tokens
from tests.conftest import install_model_providers, seed_session
from tests.fakes.summary_model import summary_model
from tests.history_helpers import (
    RecordingCompactionLifecycle,
    _completed_run,
    _make_config,
    _reported,
    _session,
    _summarizer,
    completed_outcome,
)
from tests.providers.openai_family_support import responses_adapter

if TYPE_CHECKING:
    from pathlib import Path

    from mindroom.config.main import Config
    from mindroom.history.runtime import HistoryPreparationInputs


def _agent_inputs(config: Config) -> HistoryPreparationInputs:
    """Resolve the configured test agent's history inputs for a 100-token static prompt."""
    return resolve_entity_preparation_inputs(
        entity_name="test_agent",
        history_settings=config.resolve_entity("test_agent").history_settings,
        config=config,
        static_prompt_tokens=100,
        active_model_name=None,
        active_context_window=None,
    )


@pytest.mark.asyncio
@pytest.mark.parametrize("stage", ["prepare", "finalize", "compact", "summary"])
async def test_history_sizing_allows_loop_progress(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    stage: str,
) -> None:
    """A pending loop callback can run while the provider sizes saved history."""
    config, paths = _make_config(tmp_path)
    model = responses_adapter(tmp_path, store=True)
    session = _session("session", runs=[_completed_run("run", messages=[Message(role="user", content="Hello")])])
    original = deepcopy(session)
    storage = create_state_storage("test_agent", tmp_path)
    seed_session(storage, session)
    scope = HistoryScope(kind="agent", scope_id="test_agent")
    resolved = _agent_inputs(config)

    async def prepare() -> PreparedScopeHistory:
        return await prepare_scope_history(
            model=model,
            resolved_inputs=resolved,
            non_prompt_tokens=0,
            runtime_paths=paths,
            config=config,
            scope_context=ScopeSessionContext(scope, storage, session, "session"),
        )

    prepared = await prepare()
    loop = asyncio.get_running_loop()
    entered = asyncio.Event()
    released = threading.Event()
    loop_progress: list[bool] = []
    estimate = ResponsesAdapter.estimate_portable_replay_tokens

    def controlled_estimate(self: ResponsesAdapter, messages: list[Message]) -> int:
        loop.call_soon_threadsafe(entered.set)
        loop_progress.append(released.wait(timeout=1))
        return estimate(self, messages)

    async def release_from_loop() -> None:
        await entered.wait()
        released.set()

    def controlled_text_estimate(
        value: str,
        *,
        model_id: str | None = None,
        conservative_fallback: bool = False,
    ) -> int:
        loop.call_soon_threadsafe(entered.set)
        loop_progress.append(released.wait(timeout=1))
        return estimate_compaction_input_tokens(
            value,
            model_id=model_id,
            conservative_fallback=conservative_fallback,
        )

    if stage == "summary":
        monkeypatch.setattr("mindroom.history.compaction.estimate_compaction_input_tokens", controlled_text_estimate)
    else:
        monkeypatch.setattr(ResponsesAdapter, "estimate_portable_replay_tokens", controlled_estimate)
    heartbeat = asyncio.create_task(release_from_loop())
    try:
        if stage == "prepare":
            await prepare()
        elif stage == "finalize":
            await _finalize_prepared_history(
                prepared_scope_history=prepared,
                config=config,
                static_prompt_tokens=100,
            )
        else:
            monkeypatch.setattr(
                "mindroom.history.compaction.generate_compaction_summary",
                AsyncMock(return_value="Greeting received."),
            )
            outcome = await compact_scope_history(
                storage=storage,
                session=session,
                scope=scope,
                state=HistoryScopeState(force_compact_before_next_run=True),
                summarizer=_summarizer(summary_model(model_id="summary"), 1000, name="summary"),
                available_history_budget=1000,
                before_tokens=0,
                replay_window_tokens=2000,
                threshold_tokens=1000,
                progress=None,
                complete=_reported,
                replay_model=model,
            )
            assert outcome is not None
        await heartbeat
        assert loop_progress
        assert all(loop_progress)
        if stage in {"prepare", "finalize"}:
            assert session == original
    finally:
        released.set()
        heartbeat.cancel()


@pytest.mark.asyncio
@pytest.mark.parametrize("stage", ["basis", "append"])
async def test_history_storage_work_allows_loop_progress(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    stage: str,
) -> None:
    """A pending loop callback can run while compaction reads its basis or appends its summary row."""
    model = responses_adapter(tmp_path, store=True)
    session = _session("session", runs=[_completed_run("run", messages=[Message(role="user", content="Hello")])])
    storage = create_state_storage("test_agent", tmp_path)
    seed_session(storage, session)
    scope = HistoryScope(kind="agent", scope_id="test_agent")
    loop = asyncio.get_running_loop()
    entered = asyncio.Event()
    released = threading.Event()
    loop_progress: list[bool] = []
    target, operation = (
        ("mindroom.history.compaction.read_chunk_basis", read_chunk_basis)
        if stage == "basis"
        else ("mindroom.history.compaction.append_compaction_chunk", append_compaction_chunk)
    )

    def controlled(*args: Any, **kwargs: Any) -> Any:  # noqa: ANN401
        loop.call_soon_threadsafe(entered.set)
        loop_progress.append(released.wait(timeout=1))
        return operation(*args, **kwargs)

    async def release_from_loop() -> None:
        await entered.wait()
        released.set()

    monkeypatch.setattr(target, controlled)
    heartbeat = asyncio.create_task(release_from_loop())
    try:
        monkeypatch.setattr(
            "mindroom.history.compaction.generate_compaction_summary",
            AsyncMock(return_value="Greeting received."),
        )
        outcome = await compact_scope_history(
            storage=storage,
            session=session,
            scope=scope,
            state=HistoryScopeState(force_compact_before_next_run=True),
            summarizer=_summarizer(summary_model(model_id="summary"), 1000, name="summary"),
            available_history_budget=1000,
            before_tokens=0,
            replay_window_tokens=2000,
            threshold_tokens=1000,
            progress=None,
            complete=_reported,
            replay_model=model,
        )
        assert outcome is not None
        await heartbeat
        assert loop_progress == [True]
    finally:
        released.set()
        heartbeat.cancel()


@pytest.mark.asyncio
async def test_forced_compaction_reuses_canonical_history_count(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """One unchanged canonical history is tokenized once before rewriting it."""
    config, paths = _make_config(tmp_path)
    model = responses_adapter(tmp_path, store=True)
    session = _session("session", runs=[_completed_run("run", messages=[Message(role="user", content="Hello")])])
    scope = HistoryScope(kind="agent", scope_id="test_agent")
    session.metadata = _with_scope_state(session.metadata, scope, HistoryScopeState(force_compact_before_next_run=True))
    storage = create_state_storage("test_agent", tmp_path)
    seed_session(storage, session)
    resolved = _agent_inputs(config)
    counted_history: list[list[str]] = []
    estimate = ResponsesAdapter.estimate_portable_replay_tokens

    def record_estimate(self: ResponsesAdapter, messages: list[Message]) -> int:
        if messages:
            counted_history.append([str(message.content) for message in messages])
        return estimate(self, messages)

    monkeypatch.setattr(ResponsesAdapter, "estimate_portable_replay_tokens", record_estimate)
    install_model_providers(monkeypatch, {"default": summary_model(model_id="summary")})
    monkeypatch.setattr(
        "mindroom.history.compaction.generate_compaction_summary",
        AsyncMock(return_value="Greeting received."),
    )
    lifecycle = RecordingCompactionLifecycle()
    prepared = await prepare_scope_history(
        compaction_lifecycle=lifecycle,
        model=model,
        resolved_inputs=resolved,
        non_prompt_tokens=0,
        runtime_paths=paths,
        config=config,
        scope_context=ScopeSessionContext(scope, storage, session, "session"),
    )
    assert counted_history == [["Hello"]]
    assert prepared.compaction_reply_outcome == "success"
    assert completed_outcome(lifecycle).before_tokens == 11
    assert session.summary is not None
    assert session.summary.text == "Greeting received."


@pytest.mark.asyncio
@pytest.mark.parametrize("after_persist", [False, True])
async def test_cancelling_history_sizing_preserves_durable_state(  # noqa: PLR0915
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    *,
    after_persist: bool,
) -> None:
    """Committed compaction completes its hooks and lifecycle before cancellation."""
    config, paths = _make_config(tmp_path)
    model = responses_adapter(tmp_path, store=True)
    session = _session("session", runs=[_completed_run("run", messages=[Message(role="user", content="Hello")])])
    storage = create_state_storage("test_agent", tmp_path)
    seed_session(storage, session)
    original = deepcopy(session)
    resolved = resolve_entity_preparation_inputs(
        config=config,
        entity_name="test_agent",
        history_settings=config.resolve_entity("test_agent").history_settings,
        static_prompt_tokens=100,
        active_model_name=None,
        active_context_window=None,
    )
    loop = asyncio.get_running_loop()
    entered = asyncio.Event()
    release = threading.Event()
    estimate = ResponsesAdapter.estimate_portable_replay_tokens

    def controlled_estimate(self: ResponsesAdapter, messages: list[Message]) -> int:
        loop.call_soon_threadsafe(entered.set)
        assert release.wait(timeout=5)
        return estimate(self, messages)

    async def generate(**_kwargs: object) -> str:
        if not after_persist:
            entered.set()
            await asyncio.Event().wait()
        return "Greeting received."

    lifecycle = AsyncMock()
    lifecycle.start.return_value = "notice"
    hook = AsyncMock()
    monkeypatch.setattr(ResponsesAdapter, "estimate_portable_replay_tokens", controlled_estimate)
    monkeypatch.setattr("mindroom.history.compaction.generate_compaction_summary", generate)
    monkeypatch.setattr("mindroom.history.compaction._emit_compaction_hook", hook)
    install_model_providers(monkeypatch, {"default": summary_model(model_id="summary")})
    task = asyncio.create_task(
        _run_scope_compaction_with_lifecycle(
            mode="manual",
            session_id=session.session_id,
            scope=HistoryScope(kind="agent", scope_id="test_agent"),
            execution_plan=resolved.execution_plan,
            history_budget=1000,
            current_history_tokens=11,
            runs_before=1,
            compaction_lifecycle=lifecycle,
            compact=partial(
                _compact_before_reply,
                storage=storage,
                session=session,
                scope=HistoryScope(kind="agent", scope_id="test_agent"),
                state=HistoryScopeState(force_compact_before_next_run=True),
                resolved_inputs=resolved,
                history_budget=1000,
                before_tokens=11,
                replay_model=model,
                config=config,
                runtime_paths=paths,
            ),
        ),
    )
    try:
        await asyncio.wait_for(entered.wait(), timeout=5)
        task.cancel("original cancellation")
        if after_persist:
            await asyncio.sleep(0)
            task.cancel("repeated cancellation")
            await asyncio.sleep(0)
            assert not task.done()
            release.set()
        with pytest.raises(asyncio.CancelledError, match="original cancellation"):
            await task
        if after_persist:
            persisted = storage.load_session("session", "agent")
            assert persisted is not None
            assert persisted.summary is not None
            assert persisted.summary.text == "Greeting received."
            assert persisted.runs == []
            assert hook.await_args_list[-1].kwargs["event_name"] == "compaction:after"
            lifecycle.complete_success.assert_awaited_once()
            lifecycle.complete_failure.assert_not_awaited()
            outcome = lifecycle.complete_success.await_args.args[0]
            assert outcome.lifecycle_notice_event_id == "notice"
            assert outcome.duration_ms >= 0
        else:
            assert session == original
            lifecycle.complete_success.assert_not_awaited()
            lifecycle.complete_failure.assert_awaited_once()
    finally:
        release.set()


@pytest.mark.asyncio
async def test_cancelled_finalization_finishes_before_the_session_is_reused(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Cancelling planning must not hand the live session back while the sizing worker still reads it."""
    config, paths = _make_config(tmp_path)
    model = responses_adapter(tmp_path, store=False)
    session = _session("session", runs=[_completed_run("run", messages=[Message(role="user", content="Hello")])])
    storage = create_state_storage("test_agent", tmp_path)
    seed_session(storage, session)
    scope = HistoryScope(kind="agent", scope_id="test_agent")
    prepared = await prepare_scope_history(
        model=model,
        resolved_inputs=_agent_inputs(config),
        non_prompt_tokens=0,
        runtime_paths=paths,
        config=config,
        scope_context=ScopeSessionContext(scope, storage, session, "session"),
    )
    loop = asyncio.get_running_loop()
    entered = asyncio.Event()
    release = threading.Event()
    ownership = asyncio.Lock()
    reuse_attempted = asyncio.Event()
    reused = asyncio.Event()
    estimate = ResponsesAdapter.estimate_portable_replay_tokens

    def controlled_estimate(self: ResponsesAdapter, messages: list[Message]) -> int:
        loop.call_soon_threadsafe(entered.set)
        assert release.wait(timeout=5)
        return estimate(self, messages)

    async def finalize() -> None:
        async with ownership:
            await _finalize_prepared_history(
                prepared_scope_history=prepared,
                config=config,
                static_prompt_tokens=100_000,
            )

    async def reuse() -> None:
        reuse_attempted.set()
        async with ownership:
            session.runs.append(_completed_run("later"))
            reused.set()

    monkeypatch.setattr(ResponsesAdapter, "estimate_portable_replay_tokens", controlled_estimate)
    task = asyncio.create_task(finalize())
    reuse_task: asyncio.Task[None] | None = None
    try:
        await asyncio.wait_for(entered.wait(), timeout=5)
        task.cancel()
        reuse_task = asyncio.create_task(reuse())
        await reuse_attempted.wait()
        assert not reused.is_set()
        task.cancel()
        release.set()
        with pytest.raises(asyncio.CancelledError):
            await task
        await reuse_task
        assert [run.run_id for run in session.runs] == ["run", "later"]
    finally:
        release.set()
        await asyncio.gather(task, return_exceptions=True)
        if reuse_task is not None:
            await reuse_task


@pytest.mark.asyncio
@pytest.mark.parametrize("retry", ["none", "fallback", "shrink"])
async def test_summary_retry_sizing_allows_loop_progress(
    monkeypatch: pytest.MonkeyPatch,
    retry: str,
) -> None:
    """Every estimate lets queued loop work run, including after a model refusal."""
    loop = asyncio.get_running_loop()
    loop_progress: list[bool] = []

    def controlled_estimate(value: str, *, model_id: str | None = None, conservative_fallback: bool = False) -> int:
        released = threading.Event()
        loop.call_soon_threadsafe(released.set)
        loop_progress.append(released.wait(timeout=1))
        return estimate_compaction_input_tokens(
            value,
            model_id=model_id,
            conservative_fallback=conservative_fallback,
        )

    monkeypatch.setattr("mindroom.history.compaction.estimate_compaction_input_tokens", controlled_estimate)
    summary = "Greeting received."
    responses: list[object] = [summary]
    if retry == "fallback":
        responses.insert(0, ModelSafeguardRefusal(provider="fake"))
    elif retry == "shrink":
        responses.insert(0, CompactionSummaryOutputLimitError("Output limit reached"))
    generate = AsyncMock(side_effect=responses)
    monkeypatch.setattr("mindroom.history.compaction.generate_compaction_summary", generate)
    # A shrink retry needs an input large enough to halve.
    run = _completed_run(
        "run",
        messages=[Message(role="user", content="Hello " * 8000 if retry == "shrink" else "Hello")],
    )
    fallback = SummaryModel(summary_model(model_id="fallback"), "fallback", 1000)
    result = await _summarize_chunk(
        _summarizer(
            summary_model(model_id="primary"),
            10000,
            name="primary",
            fallback=fallback if retry == "fallback" else None,
            session_id="session",
        ),
        None,
        [run],
    )
    assert result.summary == summary
    assert generate.await_count == (1 if retry == "none" else 2)
    assert loop_progress
    assert all(loop_progress)


@pytest.mark.asyncio
async def test_cancelling_preparation_sizing_preserves_input(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Late sizing cannot mutate history or roll back an already persisted summary."""
    config, paths = _make_config(tmp_path)
    model = responses_adapter(tmp_path, store=True)
    session = _session("session", runs=[_completed_run("run", messages=[Message(role="user", content="Hello")])])
    storage = create_state_storage("test_agent", tmp_path)
    seed_session(storage, session)
    original = deepcopy(session)
    scope = HistoryScope(kind="agent", scope_id="test_agent")
    loop = asyncio.get_running_loop()
    entered = asyncio.Event()
    finished = asyncio.Event()
    release = threading.Event()
    estimate = ResponsesAdapter.estimate_portable_replay_tokens

    def controlled_estimate(self: ResponsesAdapter, messages: list[Message]) -> int:
        loop.call_soon_threadsafe(entered.set)
        try:
            assert release.wait(timeout=5)
            return estimate(self, messages)
        finally:
            loop.call_soon_threadsafe(finished.set)

    monkeypatch.setattr(ResponsesAdapter, "estimate_portable_replay_tokens", controlled_estimate)
    operation = prepare_scope_history(
        model=model,
        resolved_inputs=_agent_inputs(config),
        non_prompt_tokens=0,
        runtime_paths=paths,
        config=config,
        scope_context=ScopeSessionContext(scope, storage, session, "session"),
    )
    task = asyncio.create_task(operation)
    try:
        await asyncio.wait_for(entered.wait(), timeout=5)
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task
        assert not finished.is_set()
        release.set()
        await asyncio.wait_for(finished.wait(), timeout=5)
        assert session == original
    finally:
        release.set()
