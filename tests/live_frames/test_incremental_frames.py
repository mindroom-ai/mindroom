"""Incremental live-frame tick guarantees and byte-for-byte oracle equivalence."""

from __future__ import annotations

import json
import random
from bisect import bisect_left
from dataclasses import replace
from types import SimpleNamespace
from typing import TYPE_CHECKING, Any, Literal, cast

import pytest
from nio.api import Api

import mindroom.live_frames.frames as fm
import mindroom.live_frames.publisher as pm
import mindroom.live_frames.transport as tm
import mindroom.streaming as sm
import mindroom.tool_system.events as ev
from mindroom.config.main import Config
from mindroom.history.reply_compactions import ReplyCompactions
from mindroom.history.types import CompactionLifecycleStart
from mindroom.llm.events import ReportedCall
from mindroom.matrix.client_delivery import DeliveredMatrixEvent
from mindroom.matrix.mentions import visible_markdown
from mindroom.message_target import MessageTarget
from mindroom.tool_system.runtime_context import WorkerProgressEvent
from mindroom.workers.models import WorkerReadyProgress
from tests.conftest import bind_runtime_paths, make_matrix_client_mock, runtime_paths_for, test_runtime_paths
from tests.streaming_helpers import FakeReplyRecords

if TYPE_CHECKING:
    from collections.abc import Callable
    from pathlib import Path

    import nio

_ROOM, _KINDS = "!room:localhost", ("completed", "started", "minimal_job", "long_job", "no_preview")
type _Phase = Literal["cold_start", "failed", "ready"]
type _S = sm.StreamingResponse
type _O = tm.LiveSendOutcome
type _F = dict[str, object]
type _Tr = ev.StreamingToolTracker


def _client(*, enc: bool = False) -> nio.AsyncClient:
    c = make_matrix_client_mock(user_id="@mindroom_code:localhost")
    c.rooms = {_ROOM: SimpleNamespace(encrypted=enc)}
    return c


def _streaming(tmp: Path, *, enc: bool = False, cls: type[_S] = sm.StreamingResponse) -> _S:
    async def _noop(*_a: object, **_kw: object) -> DeliveredMatrixEvent:
        return DeliveredMatrixEvent(event_id="$reply", content_sent={})

    cfg = bind_runtime_paths(Config(), test_runtime_paths(tmp))
    target, paths = MessageTarget.resolve(_ROOM, None, "$q"), runtime_paths_for(cfg)
    comp, hooks = ReplyCompactions(), FakeReplyRecords(send=_noop, edit=_noop).hooks()
    s = cls(target, cfg, paths, event_id="$reply", compactions=comp, **hooks)
    s._shows_stream, s.live = True, pm.LivePublisher(client=_client(enc=enc), room_id=_ROOM)
    comp.bind(s)
    return s


def _entry(i: int, kind: str = "completed") -> ev.ToolTraceEntry:
    cid = f"call-{i}"
    if kind == "started":
        return ev.ToolTraceEntry("tool_call_started", "run_shell", f"cmd={i} " + "a" * 260, tool_call_id=cid)
    if kind == "minimal_job":
        res = json.dumps({"job_id": f"j{i}", "status": "waiting", "tool": "worker"})
        return ev.ToolTraceEntry("tool_call_completed", "run_subagent", result_preview=res, tool_call_id=cid)
    if kind == "long_job":
        res = json.dumps({"job_id": f"j{i}", "tool": "worker", "status": "running", "summary": "s" * 260})
        return ev.ToolTraceEntry("tool_call_completed", "run_subagent", "x" * 240, res, tool_call_id=cid)
    if kind == "no_preview":
        return ev.ToolTraceEntry("tool_call_started", "ping", tool_call_id=cid)
    args, out = f"cmd={i} " + "a" * 280, f"out={i} " + "r" * 280
    return ev.ToolTraceEntry("tool_call_completed", "run_shell", args, out, i % 3 == 0, cid, is_error=i % 5 == 0)


def _commit(s: _S) -> None:
    snap = s._delivery_snapshot(is_final=False, allow_empty_progress=True)
    assert snap is not None
    s._mark_delivery_committed(sm._prepare_delivery_from_snapshot(snap).committed_state)


def _start(s: _S, tr: _Tr, cid: str, cmd: str = "x", *, marker: bool = False) -> None:
    m, e = tr.start(ReportedCall(cid, "run_shell", {"cmd": cmd}), tool_index=len(s.tool_trace) + 1)
    assert e is not None
    s.append_tool_trace(e)
    if marker:
        s._append_tool_marker(m)


def _complete(s: _S, tr: _Tr, cid: str, res: str = "ok", *, err: bool = False, text: bool = False) -> None:
    pending, done = tr.complete(ReportedCall(cid, "run_shell", {"cmd": "x"}, result=res, is_error=err))
    assert pending is not None
    if text and pending.visible_tool_index is not None:
        s.accumulated_text = ev.complete_pending_tool_block(s.accumulated_text, "run_shell", pending.visible_tool_index)
    assert s.complete_tool_trace(tr, pending, done)


def _count(mp: pytest.MonkeyPatch, target: object, attr: str) -> list[int]:
    n, orig = [0], getattr(target, attr)

    def _wrapped(*args: Any, **kwargs: Any) -> Any:  # noqa: ANN401
        n[0] += 1
        return orig(*args, **kwargs)

    mp.setattr(target, attr, _wrapped)
    return n


class _Harness:
    def __init__(self, mp: pytest.MonkeyPatch, start: float = 100.0) -> None:
        self.now, self.sent, self.outcome = start, cast("list[_F]", []), tm.LiveSendOutcome.SENT
        mp.setattr(pm.time, "monotonic", lambda: self.now)

        async def _send(_c: object, _r: str, frame: _F, *, transaction_id: str) -> _O:
            del transaction_id
            self.sent.append(frame)
            return self.outcome

        mp.setattr(pm, "send_live_frame", _send)

    async def tick(self, s: _S, dt: float = 0.1) -> _O | None:
        self.now += dt
        assert s.live is not None
        state = s.live_state()
        assert state is not None
        return await s.live._send_next(*state)

    async def check(self, s: _S, *, expect: bool) -> _F | None:
        before = len(self.sent)
        await self.tick(s)
        assert len(self.sent) == before + int(expect)
        return self.sent[-1] if expect else None


def _oracle_docs(s: _S) -> tuple[str, fm.LiveDocs]:
    assert s.event_id is not None
    disp, _ = sm._visible_text(s.accumulated_text, in_progress=True)
    md = "" if disp == sm._PROGRESS_PLACEHOLDER else visible_markdown(s.config, s.runtime_paths, disp)
    tools = ev.serialize_tool_trace(s.tool_trace) if s.show_tool_calls else ()
    if tools and s.tool_progress:
        prog = s.tool_progress
        tools = tuple(
            {**e, "progress": prog[t.tool_call_id]} if t.tool_call_id in prog else e
            for t, e in zip(s.tool_trace, tools, strict=True)
        )
    act = "\n".join(line.text for line in s._warmup_state.render_lines(show_tool_calls=s.show_tool_calls))
    segs = tuple({"start": g["start"], "after_tool": g["after_tool"]} for g in s._reasoning_segments)
    comps = tuple(s.compactions.content() or ()) if s.compactions is not None else ()
    st = s._resolve_stream_status(is_final=False)
    return s.event_id, fm.LiveDocs(md, s.reasoning_text, tools, st, act, segs, comps)


def _ref_fit(tools: tuple[_F, ...], *, bare: int) -> tuple[_F, ...]:
    out: list[_F] = []
    for i, e in enumerate(tools):
        shown = fm._fitted_entry(e, bare=False)
        if i < bare:
            s = fm._fitted_entry(e, bare=True)
            if len(json.dumps(s, separators=(",", ":"))) < len(json.dumps(shown, separators=(",", ":"))):
                shown = s
        out.append(shown)
    return tuple(out)


class _Oracle:
    """Uncached reference implementation of LivePublisher for exact byte-for-byte comparison."""

    def __init__(self, pub: pm.LivePublisher) -> None:
        self.client, self.room_id, self.sid = pub.client, pub.room_id, pub.publisher_id
        self.seq, self.last_snap, self.bare, self.last_sent = -1, 0.0, 0, cast("fm.LiveDocs | None", None)
        self.sent: list[_F] = []

    def step(self, now: float, s: _S, outcome: _O) -> None:
        rid, docs = _oracle_docs(s)
        fits = lambda f: tm.frame_fits(self.client, self.room_id, f, event_type=fm.LIVE_EVENT_TYPE)  # noqa: E731
        lf = lambda **kw: fm.live_frame(reply_event_id=rid, publisher_id=self.sid, seq=self.seq, **kw)  # noqa: E731
        built = lambda b: (lf(snapshot=(sh := replace(docs, tools=_ref_fit(docs.tools, bare=b)))), sh)  # noqa: E731

        snap_due = self.last_sent is None or now - self.last_snap >= pm._SNAPSHOT_INTERVAL_SECONDS
        shown = replace(docs, tools=_ref_fit(docs.tools, bare=self.bare))
        if not snap_due and shown == self.last_sent:
            return
        self.seq += 1
        frame = None if snap_due or self.last_sent is None else lf(delta=fm.live_delta(self.last_sent, shown))
        if frame is None or not fits(frame):
            prev_bare, count = self.bare, len(docs.tools)
            self.bare = min(self.bare, count)
            if not fits(built(self.bare)[0]):
                idx = bisect_left(range(count + 1), True, lo=self.bare + 1, key=lambda b: fits(built(b)[0]))
                self.bare = min(idx, count)
            snap, fitted = built(self.bare)
            if frame is None or fits(snap):
                snap_due, frame, shown = True, snap, fitted
            else:
                self.bare = prev_bare
        self.sent.append(frame)
        if outcome is tm.LiveSendOutcome.SENT:
            self.last_sent, self.last_snap = shown, (now if snap_due else self.last_snap)
        elif outcome is tm.LiveSendOutcome.RETRY:
            self.last_sent = None


@pytest.mark.asyncio
async def test_idle_ticks_do_zero_work(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """Unchanged ticks do zero tool serialization, zero entry fitting, and zero trace matching."""
    s, h = _streaming(tmp_path), _Harness(monkeypatch)
    s.accumulated_text, s.tool_trace = (
        "Working",
        [_entry(i, "long_job" if i % 7 == 0 else "completed") for i in range(40)],
    )
    _commit(s)
    await h.tick(s, dt=0.0)
    ser = _count(monkeypatch, sm, "serialize_tool_trace_entry")
    fit, match = _count(monkeypatch, fm, "_fitted_entry"), _count(monkeypatch, sm, "_tool_traces_match")
    for _ in range(20):
        assert not s.has_uncommitted_presentation()
        assert await h.tick(s) is None
    assert len(h.sent) == 1
    assert (ser[0], fit[0], match[0]) == (0, 0, 0)


@pytest.mark.asyncio
async def test_single_entry_changes_touch_one_entry(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """Single progress change, clear, append, and completion each re-serialize and re-fit only 1 entry."""
    s, tr, h = _streaming(tmp_path), ev.StreamingToolTracker(), _Harness(monkeypatch)
    for i in range(50):
        _start(s, tr, f"call-{i}", f"echo {i}")
    ok = tm.LiveSendOutcome.SENT
    assert await h.tick(s, dt=0.0) is ok
    ser, fit = _count(monkeypatch, sm, "serialize_tool_trace_entry"), _count(monkeypatch, fm, "_fitted_entry")
    cases: list[tuple[Callable[[], Any], _O | None, tuple[int, int]]] = [
        (lambda: s.set_tool_progress("call-49", "50%"), ok, (1, 1)),
        (lambda: s.set_tool_progress("call-49", "50%"), None, (0, 0)),
        (lambda: s.clear_tool_progress("call-49"), ok, (1, 1)),
        (lambda: _start(s, tr, "call-50"), ok, (1, 1)),
        (lambda: _complete(s, tr, "call-49"), ok, (1, 1)),
    ]
    for mutate, expected, counts in cases:
        ser[0] = fit[0] = 0
        mutate()
        assert await h.tick(s) is expected
        assert (ser[0], fit[0]) == counts


@pytest.mark.asyncio
@pytest.mark.parametrize("enc", [False, True], ids=["plaintext", "encrypted"])
async def test_snapshot_fitting_serializes_once_and_steady_reuses_cache(
    enc: bool,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Fitting a 200-tool snapshot serializes once; steady-state snapshots do zero entry fitting."""
    c, h = _client(enc=enc), _Harness(monkeypatch)
    pub = pm.LivePublisher(client=c, room_id=_ROOM)
    entries = [
        _entry(i, "minimal_job" if i % 19 == 0 else ("no_preview" if i % 23 == 0 else "completed")) for i in range(200)
    ]
    docs = fm.LiveDocs("summary", "", ev.serialize_tool_trace(entries), "streaming", "")
    ser_size, fit = _count(monkeypatch, tm, "_serialized_size"), _count(monkeypatch, fm, "_fitted_entry")

    await pub._send_next("$reply", docs)
    assert pub.bare > 0
    assert ser_size[0] == 1
    assert tm.frame_fits(c, _ROOM, h.sent[-1], event_type=fm.LIVE_EVENT_TYPE)
    snap = cast("_F", h.sent[-1]["snapshot"])
    fewer = {**h.sent[-1], "snapshot": {**snap, "tools": list(_ref_fit(docs.tools, bare=pub.bare - 1))}}
    assert not tm.frame_fits(c, _ROOM, fewer, event_type=fm.LIVE_EVENT_TYPE)

    h.now += 6.0
    ser_size[0] = fit[0] = 0
    await pub._send_next("$reply", docs)
    assert len(h.sent) == 2
    assert "snapshot" in h.sent[-1]
    assert (fit[0], ser_size[0]) == (0, 1)


def _rand_tool(rng: random.Random, s: _S, tr: _Tr, seq: list[int], step: int) -> None:
    op = rng.randrange(7)
    if op == 0:
        seq[0] += 1
        kind = rng.choice(_KINDS)
        _start(s, tr, f"c-{seq[0]}", "a" * 240, marker=True) if kind == "started" else s.append_tool_trace(
            _entry(seq[0], kind),
        )
    elif op == 1 and tr.pending_tools:
        cid = tr.pending_tools[0].tool_call_id or ""
        s.clear_tool_progress(cid)
        _complete(s, tr, cid, "r" * rng.randint(5, 260), err=rng.random() < 0.2, text=True)
    elif op == 2 and s.tool_trace and (chosen := rng.choice(s.tool_trace).tool_call_id) is not None:
        s.set_tool_progress(chosen, f"p={rng.randint(0, 100)}")
    elif op == 3 and s.tool_progress:
        s.clear_tool_progress(rng.choice(list(s.tool_progress)))
    elif op == 4 and s.tool_trace:
        s.replace_tool_progress(
            {e.tool_call_id: f"b-{step}" for e in s.tool_trace[-3:] if e.tool_call_id and rng.random() < 0.6},
        )
    elif op == 5:
        seq[0] += 1
        s.apply_tool_trace_snapshot([*s.tool_trace, _entry(seq[0], rng.choice(_KINDS))])
    elif op == 6:
        tr.pending_tools.clear()
        n = rng.randint(0, min(len(s.tool_trace) + 2, 25))
        s.replace_tool_trace([_entry(seq[0] + j, rng.choice(_KINDS)) for j in range(n)])
        seq[0] += n


def _rand_sideband(rng: random.Random, s: _S, cids: list[str], step: int) -> None:
    assert s.compactions is not None
    if rng.random() < 0.5:
        w = rng.randrange(5)
        if w < 2:
            (s._warmup_state.clear_terminal_failures if w == 0 else s._warmup_state.clear_for_terminal_transition)()
        else:
            ph = cast("_Phase", ("cold_start", "failed", "ready")[w - 2])
            s._warmup_state.apply_event(
                WorkerProgressEvent("shell", "run", WorkerReadyProgress(ph, "w1", "docker", float(step))),
            )
    elif not cids or rng.random() < 0.4:
        cids.append(s.compactions.start(CompactionLifecycleStart("auto", "s", "t", "m", 12_000, 16_000, 8)))
    else:
        s.compactions.update(cids[-1], status=rng.choice(["running", "success"]), after_tokens=step * 10)


def _rand_meta(rng: random.Random, s: _S, tr: _Tr, cids: list[str], step: int) -> None:
    op = rng.randrange(6)
    if op == 0:
        s._append_incremental_text(rng.choice(["hello ", "world\n", "🚀 ", "@code "]))
    elif op == 1:
        s.accumulated_text = f"rep-{step} " + "x" * rng.randint(0, 60)
        s._mark_nonadditive_text_mutation()
    elif op == 2:
        s.add_reasoning(f"think-{step} ")
        if rng.random() < 0.5:
            s.end_reasoning_segment()
    elif op == 3:
        if rng.random() < 0.5:
            _commit(s)
        else:
            tr.pending_tools.clear()
            s.restore_last_delivered_state()
    elif op == 4:
        _rand_sideband(rng, s, cids, step)
    else:
        s.show_tool_calls = not s.show_tool_calls


@pytest.mark.asyncio
@pytest.mark.parametrize("enc", [False, True], ids=["plaintext", "encrypted"])
async def test_randomized_stream_matches_oracle_byte_for_byte(
    tmp_path: Path,
    enc: bool,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """120 randomized steps emit byte-for-byte identical frames to the uncached reference oracle."""
    rng = random.Random(42 if enc else 7)  # noqa: S311
    monkeypatch.setattr(tm, "_MAX_FRAME_BYTES", 7_500 if enc else 5_500)
    s, h = _streaming(tmp_path, enc=enc), _Harness(monkeypatch, start=1_000.0)
    assert s.live is not None
    oracle, tr, seq, cids = _Oracle(s.live), ev.StreamingToolTracker(), [0], cast("list[str]", [])

    for step in range(120):
        for _ in range(0 if rng.random() < 0.25 else rng.randint(1, 3)):
            _rand_tool(rng, s, tr, seq, step) if rng.random() < 0.5 else _rand_meta(rng, s, tr, cids, step)
        h.outcome = tm.LiveSendOutcome.RETRY if rng.random() < 0.08 else tm.LiveSendOutcome.SENT
        await h.tick(s, dt=5.5 if rng.random() < 0.14 else 0.1)
        oracle.step(h.now, s, h.outcome)
        assert len(h.sent) == len(oracle.sent)
        assert (s.live.bare, s.live.seq) == (oracle.bare, oracle.seq)
        if h.sent:
            assert Api.to_json(h.sent[-1]) == Api.to_json(oracle.sent[-1])
        norm, ident = sm._normalize_stream_accumulated_text, sm._tool_trace_identity
        exp = (
            norm(s.accumulated_text) != norm(s._last_delivered_text)
            or tuple(map(ident, s.tool_trace)) != tuple(map(ident, s._last_delivered_tool_trace))
            or s.presentation_state != s._last_delivered_presentation_state
        )
        assert s.has_uncommitted_presentation() is exp
    assert len(h.sent) > 40


@pytest.mark.asyncio
async def test_every_mutation_path_updates_live_frame(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """Every streaming mutation path invalidates caches and emits an updated live frame."""
    h = _Harness(monkeypatch)
    monkeypatch.setattr(pm, "_SNAPSHOT_INTERVAL_SECONDS", 9999.0)
    repl = _streaming(tmp_path, cls=sm.ReplacementStreamingResponse)
    await h.check(repl, expect=True)
    repl._update("replaced body")
    await h.check(repl, expect=True)
    await h.check(repl, expect=False)

    s, tr, cid_box = _streaming(tmp_path), ev.StreamingToolTracker(), cast("list[str]", [])
    comp = s.compactions
    assert comp is not None
    await h.check(s, expect=True)
    await h.check(s, expect=False)

    def _nonadditive() -> None:
        s.accumulated_text = ev.complete_pending_tool_block(s.accumulated_text, "run_shell", tool_index=1)
        s._mark_nonadditive_text_mutation()

    def _commit_replace() -> None:
        _commit(s)
        s.replace_tool_trace([_entry(3)])

    w_ev = lambda k, ph: WorkerProgressEvent("shell", "run", WorkerReadyProgress(ph, k, "docker", 1.5))  # noqa: E731
    mutations: list[Callable[[], Any]] = [
        lambda: s._append_incremental_text("hello"),
        lambda: s.add_reasoning("thinking"),
        lambda: _start(s, tr, "c1", "ls", marker=True),
        lambda: s.set_tool_progress("c1", "running 1"),
        lambda: s.replace_tool_progress({"c1": "running 2"}),
        lambda: s.clear_tool_progress("c1"),
        _nonadditive,
        lambda: _complete(s, tr, "c1"),
        lambda: s.apply_tool_trace_snapshot([*s.tool_trace, _entry(2)]),
        _commit_replace,
        s.restore_last_delivered_state,
        lambda: s._warmup_state.apply_event(w_ev("w1", "failed")),
        s._warmup_state.clear_terminal_failures,
        lambda: s._warmup_state.apply_event(w_ev("w2", "cold_start")),
        s._warmup_state.clear_for_terminal_transition,
        lambda: cid_box.append(comp.start(CompactionLifecycleStart("auto", "s", "t", "m", 12_000, 16_000, 8))),
        lambda: comp.update(cid_box[0], status="success", after_tokens=3_000),
        lambda: setattr(s, "show_tool_calls", False),
        lambda: setattr(s, "show_tool_calls", True),
    ]
    for mutate in mutations:
        mutate()
        await h.check(s, expect=True)
        await h.check(s, expect=False)

    s.event_id = "$reply-2"
    state = s.live_state()
    assert state is not None
    assert state[0] == "$reply-2"
    s._append_incremental_text("!")
    frame = await h.check(s, expect=True)
    assert frame is not None
    assert frame["reply"] == "$reply-2"


def test_uncommitted_presentation_fast_path_and_mutations(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """Committed unchanged streams skip _tool_traces_match while every mutation is detected."""
    s, tr = _streaming(tmp_path), ev.StreamingToolTracker()
    for i in range(25):
        _start(s, tr, f"c-{i}", f"x {i}")
    _commit(s)
    match_calls = _count(monkeypatch, sm, "_tool_traces_match")
    for _ in range(10):
        assert not s.has_uncommitted_presentation()
    s.accumulated_text = "  \n"
    assert not s.has_uncommitted_presentation()
    assert match_calls[0] == 0

    mutations: list[Callable[[], Any]] = [
        lambda: setattr(s, "accumulated_text", "hello"),
        lambda: setattr(s, "presentation_state", {"mode": "updated"}),
        lambda: s.append_tool_trace(_entry(99)),
        lambda: _complete(s, tr, "c-0"),
        lambda: s.apply_tool_trace_snapshot([*s.tool_trace, _entry(100)]),
        lambda: s.replace_tool_trace([_entry(101)]),
        lambda: s.tool_trace.append(_entry(102)),
    ]
    for mutate in mutations:
        mutate()
        assert s.has_uncommitted_presentation()
        s.restore_last_delivered_state()
        assert not s.has_uncommitted_presentation()
