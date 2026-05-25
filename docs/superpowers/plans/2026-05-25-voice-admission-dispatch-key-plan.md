# Voice Admission Dispatch Key Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Close the remaining voice coalescing blockers without adding a second gate or broad room-wide capture.

**Architecture:** Keep `CoalescingGate` as the single owner of receive-order waiting. Separate the key an event waits under from the key it dispatches under, and let the gate settle older voice target-key tasks before later same-room/same-requester work flushes. Voice STT remains deferred; only target-key resolution is allowed to affect coalescing boundaries.

**Tech Stack:** Python 3.13, asyncio, existing `CoalescingGate`, Matrix `nio`, pytest with `-n auto`.

---

## Scope

In scope:

- Fix slow plain-reply voice thread lookup so later root-scoped text cannot dispatch before earlier voice is visible to coalescing.
- Preserve a typed follow-up's original dispatch key when it is admitted into a pending voice lane.
- Keep unrelated threads from being merged into the wrong voice batch.
- Keep router echo display-only and raw audio canonical.

Out of scope:

- Deleting all general `CoalescingGate.retarget()` behavior.
- Routing edits, interactive selections, hooks, or scheduled tasks through receive-time admission.
- Rewriting the full coalescing state machine.

## Accountability Rules

- No production changes before a failing test for each blocker.
- No new gate type or new coalescing module.
- Source additions budget: target under 200 added lines for this fix.
- `turn_controller.py` may only wire voice admission and target readiness; batching logic stays in `coalescing.py`.
- `CoalescingGate` must store admission lane and dispatch key separately.
- Any temporary broad same-room waiting must split by resolved dispatch key before dispatch; unrelated threads may wait on older target-key resolution but must not share a response.
- After implementation, run focused tests, full `pytest -n auto --no-cov -q`, pre-commit, and four unbiased PR-review agents using the PR review skill.

## Task 1: Add Failing Regressions

**Files:**

- Modify: `tests/test_voice_bot_threading.py`
- Modify: `tests/test_live_message_coalescing.py`

- [x] Add a bot-level regression where plain-reply voice to `$thread_child` starts first, `conversation_cache.get_thread_id_for_event("$thread_child")` is blocked, then typed text in `$thread_root` arrives.
      Expected before fix: text dispatches alone while the cache lookup is blocked.
      Expected after fix: no dispatch until the voice target key settles, then one batch `["$voice", "$typed"]`.

- [x] Add a gate-level regression where room-level pending voice returns `None`, a typed reply to `$voice` was admitted into that voice lane, and the typed event still dispatches under its original `$voice` thread key.
      Expected before fix: batch key is `(room, None, user)`.
      Expected after fix: batch key is `(room, "$voice", user)` and the handoff keeps the thread relation.

- [x] Run:

```bash
uv run pytest tests/test_voice_bot_threading.py::test_plain_reply_voice_waits_before_slow_thread_cache_lookup tests/test_live_message_coalescing.py::test_reply_to_failed_pending_voice_keeps_original_dispatch_key -n auto --no-cov -q
```

Expected: both tests fail for the reviewed bugs.

## Task 2: Preserve Dispatch Key On Ready Events

**Files:**

- Modify: `src/mindroom/coalescing.py`

- [x] Update `CoalescingGate.enqueue()` so `_key_for_pending_voice_source_event()` chooses only the admission lane.
      Preserve the caller's canonical key as `ReadyPendingEvent.dispatch_key`.

Target shape:

```python
async def enqueue(self, key: CoalescingKey, pending_event: PendingEvent) -> None:
    dispatch_key = self._canonical_key(key)
    admission_key = self._key_for_pending_voice_source_event(dispatch_key)
    await self.admit(
        admission_key,
        received_at=pending_event.enqueue_time,
        source_event_id=pending_event.event.event_id,
        source_kind=pending_event.source_kind,
        ready_result=ReadyPendingEvent(dispatch_key=dispatch_key, pending_event=pending_event),
    )
```

- [x] Run the failed-voice dispatch-key regression.

Expected: it passes without changing dispatch handoff rules.

## Task 3: Add Target-Key Readiness Separate From STT Readiness

**Files:**

- Modify: `src/mindroom/coalescing.py`
- Modify: `src/mindroom/turn_controller.py`

- [x] Extend `_QueuedEvent` with optional target-key readiness.

Target shape:

```python
target_key_task: asyncio.Task[CoalescingKey | None] | None = None
target_key_result: CoalescingKey | None = None
target_key_settled: bool = False
```

- [x] Extend `CoalescingGate.admit()` with `target_key_task: asyncio.Task[CoalescingKey | None] | None = None`.
      This is optional and only voice should pass it.

- [x] Add gate logic that settles older target-key tasks for the same `(room_id, requester_user_id)` before a normal same-user gate claims and dispatches.
      It must await target-key tasks only, not STT ready tasks.
      When a target key resolves and differs from the admission key, call existing `retarget(admission_key, target_key)`.

- [x] Change raw voice admission so it never awaits a cache/thread lookup before `admit()`.
      Use a synchronous receive lane:

```python
thread_id = event_info.thread_id or event_info.reply_to_event_id
admission_key = CoalescingKey(room.room_id, thread_id, requester_user_id)
```

- [x] Create one voice target task before admission.
      The target task resolves the canonical `CoalescingKey`.
      The voice ready task awaits the same target task before STT payload normalization.

- [x] Remove the direct voice `coalescing_gate.retarget(admission_key, dispatch_key)` from `_resolve_ready_voice_target()`.
      The gate owns target-key settling and retargeting.

- [x] Run the slow-cache bot-level regression.

Expected: no text dispatch while older voice target-key resolution is blocked; one coalesced voice/text dispatch after release.

## Task 4: Verify Existing Boundaries

**Files:**

- No new production files.

- [x] Run focused suites:

```bash
uv run pytest tests/test_voice_bot_threading.py tests/test_live_message_coalescing.py::test_pending_thread_voice_does_not_capture_unrelated_thread_text tests/test_live_message_coalescing.py::test_voice_admissions_resolving_to_different_threads_do_not_coalesce tests/test_live_message_coalescing.py::test_failed_room_voice_does_not_coalesce_surviving_room_roots tests/test_live_message_coalescing.py::test_reply_to_failed_pending_voice_keeps_original_dispatch_key tests/test_coalescing.py -n auto --no-cov -q
```

- [x] Run stale-path scan:

```bash
rg "TurnIngressCoalescingGate|VoiceCoalescingGate|turn_ingress_coalescing|voice_coalescing|key_hint" src tests || true
```

Expected: no old two-gate paths or key-hint terminology.

- [x] Run source diff budget:

```bash
git --no-pager diff --stat origin/main -- src/mindroom
```

Expected: no new production file and no second gate module.

## Task 5: Final Verification And Review

- [x] Run full tests:

```bash
uv run pytest -n auto --no-cov -q
```

- [x] Run pre-commit:

```bash
uv run pre-commit run --all-files
```

- [ ] Commit only touched files with targeted `git add`.

- [ ] Push the branch.

- [ ] Spawn four unbiased PR-review agents.
      Each prompt must only say to review the current PR using `/Users/bas.nijholt/Work/dev/mindroom/.claude/skills/pr-review/SKILL.md`, without summarizing the suspected bugs or the implementation.

- [ ] Triage review results as real blocker, cleanup, out of scope, or stale.
