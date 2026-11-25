"""Incremental live-frame tick guarantees and byte-for-byte oracle equivalence."""

from __future__ import annotations

import json
import random
from bisect import bisect_left
from dataclasses import replace
from types import SimpleNamespace
from typing import TYPE_CHECKING, Any, Literal

import pytest
from nio.api import Api

import mindroom.live_frames.frames as frames_module
import mindroom.live_frames.publisher as publisher_module
import mindroom.live_frames.transport as transport_module
import mindroom.streaming as streaming_module
import mindroom.tool_system.events as events_module
from mindroom.config.main import Config
from mindroom.history.reply_compactions import ReplyCompactions
from mindroom.history.types import CompactionLifecycleStart
from mindroom.live_frames.frames import LIVE_EVENT_TYPE, LiveDocs, live_delta, live_frame
from mindroom.live_frames.publisher import LivePublisher
from mindroom.live_frames.transport import LiveSendOutcome
from mindroom.llm.events import ReportedCall
from mindroom.matrix.client_delivery import DeliveredMatrixEvent
from mindroom.matrix.mentions import visible_markdown
from mindroom.message_target import MessageTarget
from mindroom.streaming import ReplacementStreamingResponse, StreamingResponse
from mindroom.tool_system.events import StreamingToolTracker, ToolTraceEntry
from mindroom.tool_system.runtime_context import WorkerProgressEvent
from mindroom.workers.models import WorkerReadyProgress
from tests.conftest import bind_runtime_paths, make_matrix_client_mock, runtime_paths_for, test_runtime_paths
from tests.streaming_helpers import FakeReplyRecords

if TYPE_CHECKING:
    from collections.abc import Callable
    from pathlib import Path

    import nio

_ROOM = "!room:localhost"
_ENTRY_KINDS = ("completed", "started", "minimal_job", "long_job", "no_preview")
_WARMUP_PHASES: tuple[Literal["cold_start", "failed", "ready"], ...] = ("cold_start", "failed", "ready")


def _client(*, encrypted: bool = False) -> nio.AsyncClient:
    client = make_matrix_client_mock(user_id="@mindroom_code:localhost")
    client.rooms = {_ROOM: SimpleNamespace(encrypted=encrypted)}
    return client


def _streaming(
    tmp_path: Path,
    *,
    encrypted: bool = False,
    cls: type[StreamingResponse] = StreamingResponse,
) -> StreamingResponse:
    async def _noop(*_args: object, **_kwargs: object) -> DeliveredMatrixEvent:
        return DeliveredMatrixEvent(event_id="$reply", content_sent={})

    config = bind_runtime_paths(Config(), test_runtime_paths(tmp_path))
    target = MessageTarget.resolve(_ROOM, None, "$q")
    runtime_paths = runtime_paths_for(config)
    compactions = ReplyCompactions()
    hooks = FakeReplyRecords(send=_noop, edit=_noop).hooks()
    streaming = cls(target, config, runtime_paths, event_id="$reply", compactions=compactions, **hooks)
    streaming._shows_stream = True
    streaming.live = LivePublisher(client=_client(encrypted=encrypted), room_id=_ROOM)
    compactions.bind(streaming)
    return streaming


def _entry(index: int, kind: str = "completed") -> ToolTraceEntry:
    tool_call_id = f"call-{index}"
    if kind == "started":
        return ToolTraceEntry("tool_call_started", "run_shell", f"cmd={index} " + "a" * 260, tool_call_id=tool_call_id)
    if kind == "minimal_job":
        result = json.dumps({"job_id": f"j{index}", "status": "waiting", "tool": "worker"})
        return ToolTraceEntry("tool_call_completed", "run_subagent", result_preview=result, tool_call_id=tool_call_id)
    if kind == "long_job":
        result = json.dumps({"job_id": f"j{index}", "tool": "worker", "status": "running", "summary": "s" * 260})
        return ToolTraceEntry("tool_call_completed", "run_subagent", "x" * 240, result, tool_call_id=tool_call_id)
    if kind == "no_preview":
        return ToolTraceEntry("tool_call_started", "ping", tool_call_id=tool_call_id)
    args = f"cmd={index} " + "a" * 280
    result = f"out={index} " + "r" * 280
    return ToolTraceEntry(
        "tool_call_completed",
        "run_shell",
        args,
        result,
        index % 3 == 0,
        tool_call_id,
        is_error=index % 5 == 0,
    )


def _commit(streaming: StreamingResponse) -> None:
    snapshot = streaming._delivery_snapshot(is_final=False, allow_empty_progress=True)
    assert snapshot is not None
    streaming._mark_delivery_committed(streaming_module._prepare_delivery_from_snapshot(snapshot).committed_state)


def _start(
    streaming: StreamingResponse,
    tracker: StreamingToolTracker,
    tool_call_id: str,
    cmd: str = "x",
    *,
    marker: bool = False,
) -> None:
    text_chunk, entry = tracker.start(
        ReportedCall(tool_call_id, "run_shell", {"cmd": cmd}),
        tool_index=len(streaming.tool_trace) + 1,
    )
    assert entry is not None
    streaming.append_tool_trace(entry)
    if marker:
        streaming._append_tool_marker(text_chunk)


def _complete(
    streaming: StreamingResponse,
    tracker: StreamingToolTracker,
    tool_call_id: str,
    result: str = "ok",
    *,
    is_error: bool = False,
    update_text: bool = False,
) -> None:
    pending, done = tracker.complete(
        ReportedCall(tool_call_id, "run_shell", {"cmd": "x"}, result=result, is_error=is_error),
    )
    assert pending is not None
    if update_text and pending.visible_tool_index is not None:
        streaming.accumulated_text = events_module.complete_pending_tool_block(
            streaming.accumulated_text,
            "run_shell",
            pending.visible_tool_index,
        )
    assert tracker.update_visible_trace_entry(streaming.tool_trace, pending, done)
    streaming.note_tool_trace_changed()


def _count(monkeypatch: pytest.MonkeyPatch, target: object, attr: str) -> list[int]:
    calls = [0]
    original = getattr(target, attr)

    def _wrapped(*args: Any, **kwargs: Any) -> Any:  # noqa: ANN401
        calls[0] += 1
        return original(*args, **kwargs)

    monkeypatch.setattr(target, attr, _wrapped)
    return calls


class _Harness:
    def __init__(self, monkeypatch: pytest.MonkeyPatch, start: float = 100.0) -> None:
        self.now = start
        self.sent: list[dict[str, object]] = []
        self.outcome = LiveSendOutcome.SENT
        monkeypatch.setattr(publisher_module.time, "monotonic", lambda: self.now)

        async def _send(
            _client: object,
            _room_id: str,
            frame: dict[str, object],
            *,
            transaction_id: str,
        ) -> LiveSendOutcome:
            del transaction_id
            self.sent.append(frame)
            return self.outcome

        monkeypatch.setattr(publisher_module, "send_live_frame", _send)

    async def tick(self, streaming: StreamingResponse, dt: float = 0.1) -> LiveSendOutcome | None:
        self.now += dt
        assert streaming.live is not None
        state = streaming.live_state()
        assert state is not None
        return await streaming.live._send_next(*state)

    async def check(self, streaming: StreamingResponse, *, expect: bool) -> dict[str, object] | None:
        before = len(self.sent)
        await self.tick(streaming)
        assert len(self.sent) == before + int(expect)
        return self.sent[-1] if expect else None


def _oracle_docs(streaming: StreamingResponse) -> tuple[str, LiveDocs]:
    assert streaming.event_id is not None
    display, _ = streaming_module._visible_text(streaming.accumulated_text, in_progress=True)
    md = (
        ""
        if display == streaming_module._PROGRESS_PLACEHOLDER
        else visible_markdown(streaming.config, streaming.runtime_paths, display)
    )
    tools = events_module.serialize_tool_trace(streaming.tool_trace) if streaming.show_tool_calls else ()
    if tools and streaming.tool_progress:
        progress = streaming.tool_progress
        tools = tuple(
            {**entry, "progress": progress[trace.tool_call_id]} if trace.tool_call_id in progress else entry
            for trace, entry in zip(streaming.tool_trace, tools, strict=True)
        )
    activity = "\n".join(
        line.text for line in streaming._warmup_state.render_lines(show_tool_calls=streaming.show_tool_calls)
    )
    segments = tuple({"start": seg["start"], "after_tool": seg["after_tool"]} for seg in streaming._reasoning_segments)
    compactions = tuple(streaming.compactions.content() or ()) if streaming.compactions is not None else ()
    status = streaming._resolve_stream_status(is_final=False)
    return streaming.event_id, LiveDocs(md, streaming.reasoning_text, tools, status, activity, segments, compactions)


def _ref_fit(tools: tuple[dict[str, object], ...], *, bare: int) -> tuple[dict[str, object], ...]:
    fitted: list[dict[str, object]] = []
    for index, entry in enumerate(tools):
        shown = frames_module._fitted_entry(entry, bare=False)
        if index < bare:
            stripped = frames_module._fitted_entry(entry, bare=True)
            if len(json.dumps(stripped, separators=(",", ":"))) < len(json.dumps(shown, separators=(",", ":"))):
                shown = stripped
        fitted.append(shown)
    return tuple(fitted)


class _Oracle:
    """Uncached reference implementation of LivePublisher for exact byte-for-byte comparison."""

    def __init__(self, publisher: LivePublisher) -> None:
        self.client = publisher.client
        self.room_id = publisher.room_id
        self.publisher_id = publisher.publisher_id
        self.seq = -1
        self.last_snapshot_at = 0.0
        self.bare = 0
        self.last_sent: LiveDocs | None = None
        self.sent: list[dict[str, object]] = []

    def step(self, now: float, streaming: StreamingResponse, outcome: LiveSendOutcome) -> None:
        reply_event_id, docs = _oracle_docs(streaming)

        def fits(frame: dict[str, object]) -> bool:
            return transport_module.frame_fits(self.client, self.room_id, frame, event_type=LIVE_EVENT_TYPE)

        def built(bare: int) -> tuple[dict[str, object], LiveDocs]:
            shown = replace(docs, tools=_ref_fit(docs.tools, bare=bare))
            frame = live_frame(
                reply_event_id=reply_event_id,
                publisher_id=self.publisher_id,
                seq=self.seq,
                snapshot=shown,
            )
            return frame, shown

        snapshot_due = (
            self.last_sent is None or now - self.last_snapshot_at >= publisher_module._SNAPSHOT_INTERVAL_SECONDS
        )
        shown = replace(docs, tools=_ref_fit(docs.tools, bare=self.bare))
        if not snapshot_due and shown == self.last_sent:
            return
        self.seq += 1
        frame = (
            None
            if snapshot_due or self.last_sent is None
            else live_frame(
                reply_event_id=reply_event_id,
                publisher_id=self.publisher_id,
                seq=self.seq,
                delta=live_delta(self.last_sent, shown),
            )
        )
        if frame is None or not fits(frame):
            prev_bare, count = self.bare, len(docs.tools)
            self.bare = min(self.bare, count)
            if not fits(built(self.bare)[0]):
                idx = bisect_left(range(count + 1), True, lo=self.bare + 1, key=lambda bare: fits(built(bare)[0]))
                self.bare = min(idx, count)
            snapshot, fitted = built(self.bare)
            if frame is None or fits(snapshot):
                snapshot_due, frame, shown = True, snapshot, fitted
            else:
                self.bare = prev_bare
        self.sent.append(frame)
        if outcome is LiveSendOutcome.SENT:
            self.last_sent = shown
            if snapshot_due:
                self.last_snapshot_at = now
        elif outcome is LiveSendOutcome.RETRY:
            self.last_sent = None


@pytest.mark.asyncio
async def test_idle_ticks_do_zero_work(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """Unchanged ticks do zero tool serialization, zero entry fitting, and zero trace matching."""
    streaming, harness = _streaming(tmp_path), _Harness(monkeypatch)
    streaming.accumulated_text = "Working"
    streaming.tool_trace = [_entry(i, "long_job" if i % 7 == 0 else "completed") for i in range(40)]
    _commit(streaming)
    await harness.tick(streaming, dt=0.0)
    serialize_calls = _count(monkeypatch, streaming_module, "serialize_tool_trace_entry")
    fit_calls = _count(monkeypatch, frames_module, "_fitted_entry")
    match_calls = _count(monkeypatch, streaming_module, "_tool_traces_match")
    for _ in range(20):
        assert not streaming.has_uncommitted_presentation()
        assert await harness.tick(streaming) is None
    assert len(harness.sent) == 1
    assert (serialize_calls[0], fit_calls[0], match_calls[0]) == (0, 0, 0)


@pytest.mark.asyncio
async def test_single_entry_changes_touch_one_entry(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """Single progress change, clear, append, and completion each re-serialize and re-fit only 1 entry."""
    streaming, tracker, harness = _streaming(tmp_path), StreamingToolTracker(), _Harness(monkeypatch)
    for i in range(50):
        _start(streaming, tracker, f"call-{i}", f"echo {i}")
    assert await harness.tick(streaming, dt=0.0) is LiveSendOutcome.SENT
    serialize_calls = _count(monkeypatch, streaming_module, "serialize_tool_trace_entry")
    fit_calls = _count(monkeypatch, frames_module, "_fitted_entry")
    cases: list[tuple[Callable[[], Any], LiveSendOutcome | None, tuple[int, int]]] = [
        (lambda: streaming.set_tool_progress("call-49", "50%"), LiveSendOutcome.SENT, (1, 1)),
        (lambda: streaming.set_tool_progress("call-49", "50%"), None, (0, 0)),
        (lambda: streaming.clear_tool_progress("call-49"), LiveSendOutcome.SENT, (1, 1)),
        (lambda: _start(streaming, tracker, "call-50"), LiveSendOutcome.SENT, (1, 1)),
        (lambda: _complete(streaming, tracker, "call-49"), LiveSendOutcome.SENT, (1, 1)),
    ]
    for mutate, expected, counts in cases:
        serialize_calls[0] = fit_calls[0] = 0
        mutate()
        assert await harness.tick(streaming) is expected
        assert (serialize_calls[0], fit_calls[0]) == counts


@pytest.mark.asyncio
@pytest.mark.parametrize("encrypted", [False, True], ids=["plaintext", "encrypted"])
async def test_snapshot_fitting_serializes_once_and_steady_reuses_cache(
    encrypted: bool,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Fitting a 200-tool snapshot serializes once; steady-state snapshots do zero entry fitting."""
    client, harness = _client(encrypted=encrypted), _Harness(monkeypatch)
    publisher = LivePublisher(client=client, room_id=_ROOM)
    entries = [
        _entry(i, "minimal_job" if i % 19 == 0 else ("no_preview" if i % 23 == 0 else "completed")) for i in range(200)
    ]
    docs = LiveDocs("summary", "", events_module.serialize_tool_trace(entries), "streaming", "")
    serialize_size_calls = _count(monkeypatch, transport_module, "_serialized_size")
    fit_calls = _count(monkeypatch, frames_module, "_fitted_entry")

    await publisher._send_next("$reply", docs)
    assert publisher.bare > 0
    assert serialize_size_calls[0] == 1
    assert transport_module.frame_fits(client, _ROOM, harness.sent[-1], event_type=LIVE_EVENT_TYPE)
    snapshot = harness.sent[-1]["snapshot"]
    assert isinstance(snapshot, dict)
    fewer = {**harness.sent[-1], "snapshot": {**snapshot, "tools": list(_ref_fit(docs.tools, bare=publisher.bare - 1))}}
    assert not transport_module.frame_fits(client, _ROOM, fewer, event_type=LIVE_EVENT_TYPE)

    harness.now += 6.0
    serialize_size_calls[0] = fit_calls[0] = 0
    await publisher._send_next("$reply", docs)
    assert len(harness.sent) == 2
    assert "snapshot" in harness.sent[-1]
    assert (fit_calls[0], serialize_size_calls[0]) == (0, 1)


@pytest.mark.asyncio
async def test_class_level_fits_monkeypatch_uses_bisect_fallback(monkeypatch: pytest.MonkeyPatch) -> None:
    """Class-level LivePublisher._fits overrides bypass the fast size path and use bisect."""
    harness = _Harness(monkeypatch)
    publisher = LivePublisher(client=_client(), room_id=_ROOM)
    docs = LiveDocs(
        "summary",
        "",
        events_module.serialize_tool_trace([_entry(i) for i in range(12)]),
        "streaming",
        "",
    )
    fast_path_calls = _count(monkeypatch, publisher_module, "frame_plaintext_size")
    fits_calls = 0

    def custom_fits(_self: LivePublisher, frame: dict[str, object]) -> bool:
        nonlocal fits_calls
        fits_calls += 1
        snapshot = frame.get("snapshot")
        if not isinstance(snapshot, dict):
            return True
        tools = snapshot["tools"]
        assert isinstance(tools, list)
        return sum(1 for entry in tools if "args_preview" in entry) <= 5

    monkeypatch.setattr(LivePublisher, "_fits", custom_fits)
    assert await publisher._send_next("$reply", docs) is LiveSendOutcome.SENT
    assert fast_path_calls[0] == 0
    assert fits_calls > 1
    assert publisher.bare == 7
    assert len(harness.sent) == 1


def _rand_tool(
    rng: random.Random,
    streaming: StreamingResponse,
    tracker: StreamingToolTracker,
    seq: list[int],
    step: int,
) -> None:
    op = rng.randrange(7)
    if op == 0:
        seq[0] += 1
        kind = rng.choice(_ENTRY_KINDS)
        if kind == "started":
            _start(streaming, tracker, f"c-{seq[0]}", "a" * 240, marker=True)
        else:
            streaming.append_tool_trace(_entry(seq[0], kind))
    elif op == 1 and tracker.pending_tools:
        tool_call_id = tracker.pending_tools[0].tool_call_id or ""
        streaming.clear_tool_progress(tool_call_id)
        _complete(
            streaming,
            tracker,
            tool_call_id,
            "r" * rng.randint(5, 260),
            is_error=rng.random() < 0.2,
            update_text=True,
        )
    elif op == 2 and streaming.tool_trace and (chosen := rng.choice(streaming.tool_trace).tool_call_id) is not None:
        streaming.set_tool_progress(chosen, f"p={rng.randint(0, 100)}")
    elif op == 3 and streaming.tool_progress:
        streaming.clear_tool_progress(rng.choice(list(streaming.tool_progress)))
    elif op == 4 and streaming.tool_trace:
        streaming.replace_tool_progress(
            {
                entry.tool_call_id: f"b-{step}"
                for entry in streaming.tool_trace[-3:]
                if entry.tool_call_id and rng.random() < 0.6
            },
        )
    elif op == 5:
        seq[0] += 1
        streaming.apply_tool_trace_snapshot([*streaming.tool_trace, _entry(seq[0], rng.choice(_ENTRY_KINDS))])
    elif op == 6:
        tracker.pending_tools.clear()
        count = rng.randint(0, min(len(streaming.tool_trace) + 2, 25))
        streaming.replace_tool_trace([_entry(seq[0] + j, rng.choice(_ENTRY_KINDS)) for j in range(count)])
        seq[0] += count


def _rand_sideband(rng: random.Random, streaming: StreamingResponse, compaction_ids: list[str], step: int) -> None:
    assert streaming.compactions is not None
    if rng.random() < 0.5:
        choice = rng.randrange(5)
        if choice == 0:
            streaming._warmup_state.clear_terminal_failures()
        elif choice == 1:
            streaming._warmup_state.clear_for_terminal_transition()
        else:
            phase = _WARMUP_PHASES[choice - 2]
            streaming._warmup_state.apply_event(
                WorkerProgressEvent("shell", "run", WorkerReadyProgress(phase, "w1", "docker", float(step))),
            )
    elif not compaction_ids or rng.random() < 0.4:
        compaction_ids.append(
            streaming.compactions.start(CompactionLifecycleStart("auto", "s", "t", "m", 12_000, 16_000, 8)),
        )
    else:
        streaming.compactions.update(
            compaction_ids[-1],
            status=rng.choice(["running", "success"]),
            after_tokens=step * 10,
        )


def _rand_meta(
    rng: random.Random,
    streaming: StreamingResponse,
    tracker: StreamingToolTracker,
    compaction_ids: list[str],
    step: int,
) -> None:
    op = rng.randrange(6)
    if op == 0:
        streaming._append_incremental_text(rng.choice(["hello ", "world\n", "🚀 ", "@code "]))
    elif op == 1:
        streaming.accumulated_text = f"rep-{step} " + "x" * rng.randint(0, 60)
        streaming._mark_nonadditive_text_mutation()
    elif op == 2:
        streaming.add_reasoning(f"think-{step} ")
        if rng.random() < 0.5:
            streaming.end_reasoning_segment()
    elif op == 3:
        if rng.random() < 0.5:
            _commit(streaming)
        else:
            tracker.pending_tools.clear()
            streaming.restore_last_delivered_state()
    elif op == 4:
        _rand_sideband(rng, streaming, compaction_ids, step)
    else:
        streaming.show_tool_calls = not streaming.show_tool_calls


@pytest.mark.asyncio
@pytest.mark.parametrize("encrypted", [False, True], ids=["plaintext", "encrypted"])
async def test_randomized_stream_matches_oracle_byte_for_byte(
    tmp_path: Path,
    encrypted: bool,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """120 randomized steps emit byte-for-byte identical frames to the uncached reference oracle."""
    rng = random.Random(42 if encrypted else 7)  # noqa: S311
    monkeypatch.setattr(transport_module, "_MAX_FRAME_BYTES", 7_500 if encrypted else 5_500)
    streaming, harness = _streaming(tmp_path, encrypted=encrypted), _Harness(monkeypatch, start=1_000.0)
    assert streaming.live is not None
    oracle = _Oracle(streaming.live)
    tracker = StreamingToolTracker()
    seq = [0]
    compaction_ids: list[str] = []

    for step in range(120):
        for _ in range(0 if rng.random() < 0.25 else rng.randint(1, 3)):
            if rng.random() < 0.5:
                _rand_tool(rng, streaming, tracker, seq, step)
            else:
                _rand_meta(rng, streaming, tracker, compaction_ids, step)
        harness.outcome = LiveSendOutcome.RETRY if rng.random() < 0.08 else LiveSendOutcome.SENT
        await harness.tick(streaming, dt=5.5 if rng.random() < 0.14 else 0.1)
        oracle.step(harness.now, streaming, harness.outcome)
        assert len(harness.sent) == len(oracle.sent)
        assert (streaming.live.bare, streaming.live.seq) == (oracle.bare, oracle.seq)
        if harness.sent:
            assert Api.to_json(harness.sent[-1]) == Api.to_json(oracle.sent[-1])
        normalize = streaming_module._normalize_stream_accumulated_text
        identity = streaming_module._tool_trace_identity
        expected_uncommitted = (
            normalize(streaming.accumulated_text) != normalize(streaming._last_delivered_text)
            or tuple(map(identity, streaming.tool_trace)) != tuple(map(identity, streaming._last_delivered_tool_trace))
            or streaming.presentation_state != streaming._last_delivered_presentation_state
        )
        assert streaming.has_uncommitted_presentation() is expected_uncommitted
    assert len(harness.sent) > 40


@pytest.mark.asyncio
async def test_every_mutation_path_updates_live_frame(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """Every streaming mutation path invalidates caches and emits an updated live frame."""
    harness = _Harness(monkeypatch)
    monkeypatch.setattr(publisher_module, "_SNAPSHOT_INTERVAL_SECONDS", 9999.0)
    replacement = _streaming(tmp_path, cls=ReplacementStreamingResponse)
    await harness.check(replacement, expect=True)
    replacement._update("replaced body")
    await harness.check(replacement, expect=True)
    await harness.check(replacement, expect=False)

    streaming = _streaming(tmp_path)
    tracker = StreamingToolTracker()
    compaction_ids: list[str] = []
    compactions = streaming.compactions
    assert compactions is not None
    await harness.check(streaming, expect=True)
    await harness.check(streaming, expect=False)

    def _nonadditive() -> None:
        streaming.accumulated_text = events_module.complete_pending_tool_block(
            streaming.accumulated_text,
            "run_shell",
            tool_index=1,
        )
        streaming._mark_nonadditive_text_mutation()

    def _commit_replace() -> None:
        _commit(streaming)
        streaming.replace_tool_trace([_entry(3)])

    def _warmup_event(worker_key: str, phase: Literal["cold_start", "failed", "ready"]) -> WorkerProgressEvent:
        return WorkerProgressEvent("shell", "run", WorkerReadyProgress(phase, worker_key, "docker", 1.5))

    mutations: list[Callable[[], Any]] = [
        lambda: streaming._append_incremental_text("hello"),
        lambda: streaming.add_reasoning("thinking"),
        lambda: _start(streaming, tracker, "c1", "ls", marker=True),
        lambda: streaming.set_tool_progress("c1", "running 1"),
        lambda: streaming.replace_tool_progress({"c1": "running 2"}),
        lambda: streaming.clear_tool_progress("c1"),
        _nonadditive,
        lambda: _complete(streaming, tracker, "c1"),
        lambda: streaming.apply_tool_trace_snapshot([*streaming.tool_trace, _entry(2)]),
        _commit_replace,
        streaming.restore_last_delivered_state,
        lambda: streaming._warmup_state.apply_event(_warmup_event("w1", "failed")),
        streaming._warmup_state.clear_terminal_failures,
        lambda: streaming._warmup_state.apply_event(_warmup_event("w2", "cold_start")),
        streaming._warmup_state.clear_for_terminal_transition,
        lambda: compaction_ids.append(
            compactions.start(CompactionLifecycleStart("auto", "s", "t", "m", 12_000, 16_000, 8)),
        ),
        lambda: compactions.update(compaction_ids[0], status="success", after_tokens=3_000),
        lambda: setattr(streaming, "show_tool_calls", False),
        lambda: setattr(streaming, "show_tool_calls", True),
    ]
    for mutate in mutations:
        mutate()
        await harness.check(streaming, expect=True)
        await harness.check(streaming, expect=False)

    streaming.event_id = "$reply-2"
    state = streaming.live_state()
    assert state is not None
    assert state[0] == "$reply-2"
    streaming._append_incremental_text("!")
    frame = await harness.check(streaming, expect=True)
    assert frame is not None
    assert frame["reply"] == "$reply-2"


@pytest.mark.asyncio
async def test_tool_trace_snapshot_owns_entries_and_guards_equal_reapply(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """In-place caller mutations do not leak into tool_trace, and equal snapshots preserve O(1) idle checks."""
    streaming, harness = _streaming(tmp_path), _Harness(monkeypatch)
    entry = _entry(0, "started")
    streaming.apply_tool_trace_snapshot([entry])
    _commit(streaming)
    assert await harness.tick(streaming, dt=0.0) is LiveSendOutcome.SENT

    entry.type = "tool_call_completed"
    entry.result_preview = "finished"
    assert streaming.tool_trace[0] is not entry
    assert streaming.tool_trace[0].type == "tool_call_started"
    assert not streaming.has_uncommitted_presentation()
    state = streaming.live_state()
    assert state is not None
    assert state[1].tools[0]["type"] == "tool_call_started"
    assert await harness.tick(streaming) is None

    streaming.apply_tool_trace_snapshot([entry])
    assert streaming.has_uncommitted_presentation()
    state = streaming.live_state()
    assert state is not None
    assert state[1].tools[0]["type"] == "tool_call_completed"
    assert state[1].tools[0]["result_preview"] == "finished"
    assert await harness.tick(streaming) is LiveSendOutcome.SENT

    _commit(streaming)
    streaming.apply_tool_trace_snapshot([entry])
    match_calls = _count(monkeypatch, streaming_module, "_tool_traces_match")
    assert not streaming.has_uncommitted_presentation()
    assert match_calls[0] == 0

    replaced = _entry(1, "started")
    streaming.replace_tool_trace([replaced])
    _commit(streaming)
    replaced.type = "tool_call_completed"
    replaced.result_preview = "mutated-after-replace"
    assert streaming.tool_trace[0] is not replaced
    assert streaming.tool_trace[0].type == "tool_call_started"
    assert not streaming.has_uncommitted_presentation()
    streaming.replace_tool_trace([replaced])
    assert streaming.has_uncommitted_presentation()
    assert streaming.tool_trace[0].type == "tool_call_completed"
    _commit(streaming)
    streaming.replace_tool_trace([replaced])
    match_calls[0] = 0
    assert not streaming.has_uncommitted_presentation()
    assert match_calls[0] == 0


def test_uncommitted_presentation_fast_path_and_mutations(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """Committed unchanged streams skip _tool_traces_match while every mutation is detected."""
    streaming, tracker = _streaming(tmp_path), StreamingToolTracker()
    for i in range(25):
        _start(streaming, tracker, f"c-{i}", f"x {i}")
    _commit(streaming)
    match_calls = _count(monkeypatch, streaming_module, "_tool_traces_match")
    for _ in range(10):
        assert not streaming.has_uncommitted_presentation()
    streaming.accumulated_text = "  \n"
    assert not streaming.has_uncommitted_presentation()
    assert match_calls[0] == 0

    mutations: list[Callable[[], Any]] = [
        lambda: setattr(streaming, "accumulated_text", "hello"),
        lambda: setattr(streaming, "presentation_state", {"mode": "updated"}),
        lambda: streaming.append_tool_trace(_entry(99)),
        lambda: _complete(streaming, tracker, "c-0"),
        lambda: streaming.apply_tool_trace_snapshot([*streaming.tool_trace, _entry(100)]),
        lambda: streaming.replace_tool_trace([_entry(101)]),
        lambda: streaming.tool_trace.append(_entry(102)),
    ]
    for mutate in mutations:
        mutate()
        assert streaming.has_uncommitted_presentation()
        streaming.restore_last_delivered_state()
        assert not streaming.has_uncommitted_presentation()


def test_tool_trace_snapshot_copies_only_changed_entries(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """Equal 200-entry snapshots do zero replace() copies, and 1-entry updates copy only that entry."""
    streaming = _streaming(tmp_path)
    caller_entries = [_entry(i, "started") for i in range(200)]
    streaming.apply_tool_trace_snapshot(caller_entries)
    owned_before = list(streaming.tool_trace)
    replace_calls = _count(monkeypatch, streaming_module, "replace")

    streaming.apply_tool_trace_snapshot(caller_entries)
    assert replace_calls[0] == 0
    assert all(current is previous for current, previous in zip(streaming.tool_trace, owned_before, strict=True))

    caller_entries[123].type = "tool_call_completed"
    caller_entries[123].result_preview = "updated"
    streaming.apply_tool_trace_snapshot(caller_entries)
    assert replace_calls[0] == 1
    assert streaming.tool_trace[123] is not owned_before[123]
    assert streaming.tool_trace[123] is not caller_entries[123]
    assert streaming.tool_trace[123].result_preview == "updated"
    assert all(streaming.tool_trace[i] is owned_before[i] for i in range(200) if i != 123)

    owned_after_snapshot = list(streaming.tool_trace)
    replace_calls[0] = 0
    streaming.replace_tool_trace(caller_entries)
    assert replace_calls[0] == 0
    assert all(
        current is previous for current, previous in zip(streaming.tool_trace, owned_after_snapshot, strict=True)
    )

    caller_entries[7].result_preview = "replaced-7"
    streaming.replace_tool_trace(caller_entries)
    assert replace_calls[0] == 1
    assert streaming.tool_trace[7] is not caller_entries[7]
    assert streaming.tool_trace[7].result_preview == "replaced-7"
    assert all(streaming.tool_trace[i] is owned_after_snapshot[i] for i in range(200) if i != 7)


def test_uncommitted_presentation_caches_matched_presentation_state(tmp_path: Path) -> None:
    """Committed nested presentation_state dicts avoid repeated deep equality across idle ticks."""
    eq_calls = 0

    class _EqProbe:
        def __init__(self, value: str) -> None:
            self.value = value

        def __hash__(self) -> int:
            return hash(self.value)

        def __eq__(self, other: object) -> bool:
            nonlocal eq_calls
            eq_calls += 1
            return isinstance(other, _EqProbe) and self.value == other.value

        def __deepcopy__(self, _memo: dict[int, object]) -> _EqProbe:
            return _EqProbe(self.value)

    streaming = _streaming(tmp_path)
    streaming.presentation_state = {
        "kind": "team",
        "members": [{"id": "member_a", "content": _EqProbe("slot-a")}],
    }
    _commit(streaming)
    eq_calls = 0

    assert not streaming.has_uncommitted_presentation()
    assert eq_calls == 1
    for _ in range(20):
        assert not streaming.has_uncommitted_presentation()
    assert eq_calls == 1

    streaming.presentation_state = {
        "kind": "team",
        "members": [{"id": "member_a", "content": _EqProbe("slot-a-v2")}],
    }
    assert streaming.has_uncommitted_presentation()
    assert eq_calls == 2

    streaming.restore_last_delivered_state()
    assert not streaming.has_uncommitted_presentation()
    assert eq_calls == 3
    for _ in range(10):
        assert not streaming.has_uncommitted_presentation()
    assert eq_calls == 3
