# ISSUE-183 — FINAL PLAN

**Goal:** Surface Kubernetes worker cold-start (~2 min `wait_for_ready` block) as a live, in-stream status update on the agent's reply, so the user sees forward motion instead of dead air.

**Synthesis source:** `PLAN-CODEX.md` (commit `41cbee5` on `issue-183-plan-codex`) and `PLAN-CLAUDE.md` (commit `8cfc885` on `issue-183-plan-claude`), reconciled against their cross-critiques (`CRITIQUE-CODEX.md`, `CRITIQUE-CLAUDE.md`). Both planners cross-voted for the other's plan, but each conceded the other's strongest mechanism. This synthesis takes Codex's **user-facing surface** and Claude's **thread-safety primitives**.

---

## 1. Architecture (one paragraph)

A new optional `progress_sink: ProgressSink` keyword is added to `WorkerBackend.ensure_worker(...)`. The Kubernetes backend wraps `wait_for_ready()` with a daemon thread that fires a `WorkerReadyProgress(phase, worker_key, backend_name, elapsed_seconds)` event after a **1.5 s grace window** (so warm pods stay silent), then on a 5 s refresh cadence, then once on `ready` or `failed`. The `sandbox_proxy._call_proxy_sync` (which already runs in `asyncio.to_thread`) installs a per-tool-call sink that uses `loop.call_soon_threadsafe(queue.put_nowait, evt)` to push events into an `asyncio.Queue` registered on a `ContextVar` set by `streaming.py:send_streaming_response`. The `StreamingResponse` keeps an active-warmups dict **as side-band state**: warmup text **never enters `accumulated_text`, `io.mindroom.tool_trace`, `run_metadata`, or persisted Agno history**, and is rendered only inside `_send_or_edit_message()` as a suffix appended to the body sent to Matrix. On `ready`/`failed`/`finalize`/cancel, the entry is removed and the next edit cycle drops the suffix.

## 2. Decisions resolved by debate

| # | Question | Decision | Why |
|---|---|---|---|
| D1 | Where to instrument the wait | **Worker backend layer** (`KubernetesWorkerBackend.wait_for_ready`) | Only layer with cold/warm timing |
| D2 | How to deliver to the user | **Side-band on `StreamingResponse`** rendered in `_send_or_edit_message` | Avoids 4 strip-everywhere seams (Claude conceded), avoids `_merge_tool_trace` `==`-equality hazards (Claude conceded) |
| D3 | Use `_append_queued_notice_if_needed`? | **No** | That path injects to model prompt history, not user-visible reply (both planners agree) |
| D4 | Extend `io.mindroom.tool_trace` `ToolTraceEntry`? | **No** | `_merge_tool_trace`/`_longest_common_prefix_len` (`streaming.py:127-157`) `==` on `@dataclass(slots=True)` would mutate prefix walk; `delivery_gateway.deliver_final` list-of-dataclass equality triggers spurious second edit (Claude conceded) |
| D5 | Grace window before first event | **1.5 s** | Warm pods ready in ~200-500 ms; `_READY_POLL_INTERVAL_SECONDS=1.0` (`kubernetes_resources.py:405-424`) means 1.0 s too twitchy, 3.0 s too slow (Codex conceded) |
| D6 | Refresh cadence after first event | **5 s** | Visible motion without edit spam |
| D7 | Notice text content | **Static lead + elapsed-only update**, no fake ETA | "remaining" derived from `ready_timeout_seconds` is timeout-deadline math, not an estimate; both planners agreed it's a lie |
| D8 | Multi-call correlation | **Coalesce by `worker_key`** | Parallel `shell` calls share one `wait_for_ready`; tool-name matching (Claude's V1) doesn't (Claude conceded) |
| D9 | Thread-to-loop bridge | **`loop.call_soon_threadsafe(queue.put_nowait, evt)` + `asyncio.Queue` + `pump.shutdown: threading.Event` + `loop.is_closed()` guard** | ContextVar alone is insufficient (Codex conceded); shutdown race must be defended (Claude's primitive) |
| D10 | ContextVar install location | **`streaming.py:send_streaming_response`** | Skips OpenAI-compat endpoint naturally; PEP 567 task context propagates to nested delegate agents |
| D11 | Cancel safety | Clear active warmups in `StreamingResponse.finalize()` BEFORE composing cancelled body. No strip needed in `clean_partial_reply_text` or `complete_pending_tool_block` because suffix never lives in `accumulated_text` | Side-band guarantees this (D2) |
| D12 | Backends without real cold-start | `static_runner.py`, `local.py`: accept `progress_sink` kwarg, ignore it | Backend-neutral contract |

## 3. Notice text templates

Backend-neutral. `{tool_name.function_name}` is rendered using the same convention as the existing tool-marker line (e.g. ``shell.run``, ``python.execute``).

```
Initial (after 1.5 s grace, single tool):
  ⏳ Preparing isolated worker for `{tool_name.function_name}`… first cold start can take up to 2 minutes.

Updated (every 5 s after initial):
  ⏳ Preparing isolated worker for `{tool_name.function_name}`… {elapsed}s elapsed.

Multiple tools sharing one warming worker (coalesced by worker_key):
  ⏳ Preparing isolated worker for `{tool_a}`, `{tool_b}`… {elapsed}s elapsed.

Multiple distinct workers warming concurrently (one suffix per worker_key):
  ⏳ Preparing isolated worker for `{tool_a}`… {elapsed_a}s elapsed.
  ⏳ Preparing isolated worker for `{tool_b}`… {elapsed_b}s elapsed.

On failure (replaces, then cleared on next stream chunk or finalize):
  ⚠️ Worker startup failed for `{tool_name.function_name}`: {error_short}.

On ready: suffix removed, no transition text. The actual tool execution
output flows through the normal pipeline immediately.
```

The suffix is appended to the **already-rendered streaming body**, separated by a blank line, so it sits below whatever the model has emitted so far without disturbing prior text.

## 4. New types

```python
# src/mindroom/workers/models.py
from typing import Literal, Callable
from dataclasses import dataclass

WorkerReadyPhase = Literal["cold_start", "waiting", "ready", "failed"]

@dataclass(frozen=True, slots=True)
class WorkerReadyProgress:
    phase: WorkerReadyPhase
    worker_key: str           # e.g. "kubernetes:shell-pool-0"
    backend_name: str         # "kubernetes" | "local" | ...
    elapsed_seconds: float
    error: str | None = None  # only set when phase == "failed"

ProgressSink = Callable[[WorkerReadyProgress], None]
```

```python
# src/mindroom/tool_system/runtime_context.py
@dataclass
class WorkerProgressPump:
    loop: asyncio.AbstractEventLoop
    queue: asyncio.Queue[WorkerProgressEvent]
    shutdown: threading.Event   # Claude's primitive

_WORKER_PROGRESS_PUMP: ContextVar[WorkerProgressPump | None] = ContextVar(
    "_WORKER_PROGRESS_PUMP", default=None
)

@contextmanager
def worker_progress_pump_scope(loop, queue) -> Iterator[WorkerProgressPump]:
    pump = WorkerProgressPump(loop=loop, queue=queue, shutdown=threading.Event())
    token = _WORKER_PROGRESS_PUMP.set(pump)
    try:
        yield pump
    finally:
        pump.shutdown.set()
        _WORKER_PROGRESS_PUMP.reset(token)
```

```python
# src/mindroom/streaming.py — side-band warmup state on StreamingResponse
@dataclass
class _ActiveWarmup:
    worker_key: str
    backend_name: str
    tool_labels: list[str]    # representative tool names for coalesced calls
    started_monotonic: float
    last_event: WorkerReadyProgress

# Field on StreamingResponse:
self._active_warmups: dict[str, _ActiveWarmup] = {}   # keyed by worker_key
```

The sink (synchronous, runs on the worker daemon thread) is built per-tool-call by `sandbox_proxy._call_proxy_sync`:

```python
def _make_progress_sink(pump: WorkerProgressPump, tool_name: str, function_name: str):
    def sink(progress: WorkerReadyProgress) -> None:
        if pump.shutdown.is_set() or pump.loop.is_closed():
            return
        evt = WorkerProgressEvent(
            tool_name=tool_name, function_name=function_name, progress=progress,
        )
        try:
            pump.loop.call_soon_threadsafe(pump.queue.put_nowait, evt)
        except RuntimeError:
            # loop closed between is_closed() check and call — drop event silently
            pass
    return sink
```

## 5. Concrete file list

| File | Lines | Change |
|---|---|---|
| `src/mindroom/workers/models.py` | EOF | Add `WorkerReadyPhase`, `WorkerReadyProgress`, `ProgressSink` |
| `src/mindroom/workers/backend.py` | 15-46 | `WorkerBackend.ensure_worker(..., progress_sink: ProgressSink \| None = None)` |
| `src/mindroom/workers/manager.py` | 29-31 | Forward `progress_sink` kwarg |
| `src/mindroom/workers/backends/static_runner.py` | 60-90 | Accept and ignore `progress_sink` |
| `src/mindroom/workers/backends/local.py` | 168-197 | Accept and ignore `progress_sink` |
| `src/mindroom/workers/backends/kubernetes.py` | 96-149 | Cold-start detection (`_deployment_ready(existing) is False`); start daemon thread on cold path; emit `cold_start` after 1.5 s, `waiting` every 5 s thereafter, `ready`/`failed` once in `finally`; pass thread reporter into `wait_for_ready` |
| `src/mindroom/workers/backends/kubernetes_resources.py` | 405-425 | Optional `on_poll_tick: Callable[[float], None] \| None` parameter passed to the poll loop |
| `src/mindroom/tool_system/runtime_context.py` | EOF | `WorkerProgressPump`, `_WORKER_PROGRESS_PUMP` ContextVar, `worker_progress_pump_scope` ctxmgr |
| `src/mindroom/tool_system/sandbox_proxy.py` | 268-340, 498-578 | Build per-call `progress_sink` from current `_WORKER_PROGRESS_PUMP`; pass to `ensure_worker` and to `_call_proxy_sync` |
| `src/mindroom/streaming.py` | 160-190 | Add `_active_warmups` dict to `StreamingResponse` |
| `src/mindroom/streaming.py` | 248-369 | In `_send_or_edit_message`: append rendered warmup suffix to body; reuse existing `event_id` if present; force initial send if first warmup arrives before any text |
| `src/mindroom/streaming.py` | 275-318 | In `finalize()`: clear `_active_warmups` BEFORE composing final body so cancel/error/complete paths never carry a suffix |
| `src/mindroom/streaming.py` | 486-658 | In `send_streaming_response`: enter `worker_progress_pump_scope`; spawn task that drains the queue and applies events to `StreamingResponse._active_warmups`; teardown on exit |

**Estimated diff:** ~250-300 LOC source + ~150 LOC tests. **No changes to** `worker_routing.py`, `ai.py`, `delivery_gateway.py`, `tool_system/events.py`, `clean_partial_reply_text`, `complete_pending_tool_block` — the side-band design buys us out of all four.

## 6. Test plan

### Unit
1. `tests/test_kubernetes_worker_backend.py`
   - cold-start path emits `cold_start` after grace + `ready` exactly once
   - warm-pod path emits ZERO events
   - 30 s simulated wait emits `cold_start` + ~6 `waiting` events
   - failure path emits `cold_start` + `failed` with error
   - sink absent (`progress_sink=None`) is a no-op
2. `tests/test_worker_progress_routing.py` (NEW)
   - `loop.call_soon_threadsafe` after `pump.shutdown.set()` is silently dropped
   - `loop.is_closed()` between check and call (race) caught by try/except RuntimeError
3. `tests/test_streaming_behavior.py`
   - warmup suffix appears on next `_send_or_edit_message`, disappears on `ready`
   - warmup never appears in `accumulated_text` returned by `send_streaming_response`
   - cancel during warmup → `finalize()` produces clean cancelled body, no suffix leak
   - error during warmup → ditto
   - parallel calls on same `worker_key` coalesce into one suffix line with both labels
   - parallel calls on different `worker_key`s → two suffix lines

### Integration
4. `tests/test_streaming_e2e.py` — fake backend with 3 s simulated warmup; assert ordered Matrix `room_send`/`m.replace` payload sequence: placeholder → first warmup edit → updated warmup edit → first content edit (no warmup) → finalize.

### Live test (mindroom-lab)
5. **Cold start verification**
   - Prime: scale down current shell-pool deployment via `kubectl scale deployment/<shell-pool> --replicas=0` OR `POST /api/workers/cleanup`
   - In `mindroom-lab` Cinny, send: `@code please run shell echo hello` (or whichever bound agent triggers k8s)
   - Expected: within ~2 s the message body shows `⏳ Preparing isolated worker…`; updates every 5 s with elapsed seconds; suffix disappears when output starts arriving
   - Capture: 3 screenshots (initial, mid-wait, post-ready) + `event_cache.db` audit of the message edit chain (`io.mindroom.stream_status` lifecycle clean) + screenshot inspection per the `chrome_devtools_take_screenshot` PNG-color verification protocol
6. **Warm path stays silent**
   - Re-run the same prompt immediately
   - Expected: NO warmup suffix at any point
7. **Cancel mid-warmup**
   - Trigger cold start, then `!stop` while warmup is showing
   - Expected: final body shows the standard cancelled note, no warmup suffix residue
8. **Multi-tool parallel** (if available)
   - Trigger two tools that route to different warming workers in one turn
   - Expected: two suffix lines, each with its own elapsed counter, both disappear independently

Evidence directory: `/tmp/ISSUE-183-evidence/` with screenshots, audit JSON, and a short markdown summary.

## 7. Out of scope (per report)

- Reducing actual k8s cold-start time (separate optimization issue if Bas wants it)
- Cinny rendering changes (this fix works in plain Element/Cinny because suffix flows through Matrix body text)
- Persisted warmup metrics across MindRoom restarts

## 8. Phase log

- 2026-04-20 PHASE 0: Filed by Bas. Living report `skills/mindroom-dev/references/reports/ISSUE-183.md`.
- 2026-04-20 PHASE 1: PLAN-CODEX.md (`41cbee5`) + PLAN-CLAUDE.md (`8cfc885`) committed on respective plan branches.
- 2026-04-20 PHASE 1.5: CRITIQUE-CODEX.md (`5937000`) + CRITIQUE-CLAUDE.md (`b6028f0`) committed; cross-vote — Codex preferred Claude, Claude preferred Codex; convergence on hybrid (Codex's user-facing surface + Claude's thread-safety primitives).
- 2026-04-20 PHASE 2: FINAL-PLAN.md committed as first commit on `issue-183` (this file). Implementer next.