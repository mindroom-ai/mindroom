# Thread Cache Repair Simplification Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Make PR #1656 repair only rejected or absent thread snapshots, isolate incompatible caller contracts, bound permanent failures, and remove obsolete repair scaffolding without weakening cache safety.

**Architecture:** `client_thread_history.py` keeps cache-read policy and calls an injected refill function only after a cache miss or rejection. `ConversationCache` owns coordinator scheduling, retained-delta replay, contract-specific flight keys, and caller-independent repair work. `ThreadRepairRegistry` owns capped exponential failure state and principal-room delta cleanup, while per-caller telemetry is emitted after each public read returns.

**Tech Stack:** Python 3.13, asyncio, matrix-nio, SQLite event cache, pytest, Ruff, ty, Tach, pre-commit, GitHub Actions.

## Global Constraints

Do not rebase, amend, or force-push the existing PR branch.
Merge current `origin/main` normally before implementation.
Do not run full pytest, PostgreSQL fanout, Docker, or live Matrix while the shared resource gate is occupied.
Use focused tests with `-n 0 --no-cov` and lightweight static checks during implementation.
Write each behavior regression first and observe its expected failure before production edits.
Preserve precise retained-delta retirement for stored, redacted, and concurrently appended events.
Preserve the two-attempt membership-epoch convergence loop.
Keep imports at module scope and do not add dynamic typed-interface fallbacks.
Add files individually with `git add`; never use `git add .`.
Keep Markdown at one sentence per line.

---

### Task 1: Synchronize Current Main and Rebaseline

**Files:**
- Merge: `origin/main` into `fix/thread-cache-repair-convergence`
- Inspect: every conflicted file
- Test: `tests/test_thread_repair.py`
- Test: `tests/test_event_cache.py`
- Test: `tests/test_thread_history.py`

**Interfaces:**
- Consumes: current PR head `07cf66abeed57d268912c17b2bda00c50474df7a`
- Produces: one normal merge commit whose first parent is the review head and whose second parent is current `origin/main`

- [ ] **Step 1: Fetch and verify exact branch state**

```bash
git fetch origin
git status --short
git rev-parse HEAD
git rev-parse origin/fix/thread-cache-repair-convergence
git rev-parse origin/main
```

Expected: worktree clean, local and remote PR heads equal, and `origin/main` newer than the PR merge base.

- [ ] **Step 2: Preview overlap**

```bash
git --no-pager log --oneline HEAD..origin/main
git --no-pager diff --name-status HEAD...origin/main
git merge-tree "$(git merge-base HEAD origin/main)" HEAD origin/main
```

Expected: every conflict is identified before mutation.

- [ ] **Step 3: Merge without rewriting review history**

```bash
git merge --no-edit origin/main
```

Expected: normal merge commit or explicit conflict markers requiring manual resolution.

- [ ] **Step 4: Resolve only overlapping semantics**

Read both sides of every conflict and preserve current-main behavior plus the PR's cache-repair invariants.

```bash
git diff --check
git status --short
```

Expected: no conflict markers and no unrelated modification.

- [ ] **Step 5: Run focused merge baseline**

```bash
uv run pytest tests/test_thread_repair.py tests/test_event_cache.py tests/test_thread_history.py -x -n 0 --no-cov -q
```

Expected: PASS, or a failure classified as merge regression before later tasks begin.

---

### Task 2: Add Typed Flight Keys, Exponential Backoff, and Room Cleanup

**Files:**
- Modify: `src/mindroom/matrix/cache/thread_repair.py`
- Modify: `src/mindroom/matrix/cache/write_coordinator.py`
- Modify: `tests/test_thread_repair.py`
- Modify: `tests/test_event_cache_write_coordination.py`
- Modify: `tach.toml` only if the exposed module interface changes

**Interfaces:**
- Consumes: principal, room, thread, hydration flag, and stale-fallback flag
- Produces: `_ThreadRepairFlightKey`, `_ThreadRepairDeltaKey`, `ThreadRepairRegistry.clear_room`, and `EventCacheWriteCoordinator.clear_thread_repair_room`
- Produces: `ThreadRepairRegistry.run(..., result_arms_backoff=...)`

- [ ] **Step 1: Write failing exponential-backoff regression**

Add this behavior to `tests/test_thread_repair.py` using the existing `_schedule` helper.

```python
@pytest.mark.asyncio
async def test_failure_backoff_doubles_to_cap_and_resets_after_success() -> None:
    now = 10.0
    registry = ThreadRepairRegistry(
        failure_backoff_seconds=1.0,
        max_failure_backoff_seconds=4.0,
        clock=lambda: now,
    )
    key = ("@agent:localhost", "!room:localhost", "$thread", True, False)
    repair = AsyncMock(
        side_effect=[
            ThreadCacheReplaceOutcome.HARD_FAILURE,
            ThreadCacheReplaceOutcome.HARD_FAILURE,
            ThreadCacheReplaceOutcome.HARD_FAILURE,
            ThreadCacheReplaceOutcome.STORED,
        ],
    )

    for expected_delay in (1.0, 2.0, 4.0):
        await registry.run(
            key,
            schedule=_schedule,
            repair=repair,
            result_arms_backoff=lambda result: not result.usable,
        )
        assert registry.retry_after_seconds(key) == expected_delay
        now += expected_delay

    await registry.run(
        key,
        schedule=_schedule,
        repair=repair,
        result_arms_backoff=lambda result: not result.usable,
    )

    assert registry.retry_after_seconds(key) == 0.0
```

- [ ] **Step 2: Write failing room-clear regression**

```python
def test_clear_room_drops_only_matching_retained_deltas() -> None:
    registry = ThreadRepairRegistry()
    first = ("@agent:localhost", "!departed:localhost", "$thread")
    second = ("@agent:localhost", "!kept:localhost", "$thread")
    registry.retain_delta(first, _event("$departed", 1000, thread_id="$thread"))
    registry.retain_delta(second, _event("$kept", 2000, thread_id="$thread"))

    registry.clear_room("@agent:localhost", "!departed:localhost")

    assert registry.pending_deltas(first) == ()
    assert [source["event_id"] for source in registry.pending_deltas(second)] == ["$kept"]
```

- [ ] **Step 3: Run RED tests**

```bash
uv run pytest tests/test_thread_repair.py::test_failure_backoff_doubles_to_cap_and_resets_after_success tests/test_thread_repair.py::test_clear_room_drops_only_matching_retained_deltas -n 0 --no-cov -v
```

Expected: FAIL because capped exponential state and room cleanup do not exist.

- [ ] **Step 4: Implement minimal registry state**

Use separate key types so caller contracts never fragment retained deltas.

```python
type _ThreadRepairFlightKey = tuple[str, str, str, bool, bool]
type _ThreadRepairDeltaKey = tuple[str, str, str]


@dataclass(frozen=True, slots=True)
class _RepairFailureBackoff:
    delay_seconds: float
    retry_after: float
```

Replace `_retry_after` with `_failure_backoffs`, compute `min(previous_delay * 2, max_failure_backoff_seconds)`, and remove a key only after a non-backoff result.
Catch `Exception` rather than `BaseException` so cancellation, `KeyboardInterrupt`, and `SystemExit` do not create ordinary failure state.
Keep expired state for one cap window so consecutive failures escalate, then prune it to prevent unbounded idle-key growth.
Remove `_RetainedDelta.order` and return retained sources in insertion order because reconstruction already applies canonical Matrix ordering.

- [ ] **Step 5: Implement coordinator contract keys and room cleanup**

Change `run_thread_repair` to require both contract booleans.

```python
async def run_thread_repair[T](
    self,
    room_id: str,
    thread_id: str,
    repair_coro_factory: Callable[[], Awaitable[T]],
    *,
    coordination_scope: str,
    hydrate_sidecars: bool,
    allow_stale_fallback: bool,
    result_arms_backoff: Callable[[T], bool],
) -> T:
```

Inline `(coordination_scope, room_id, thread_id)` in retained-delta methods and use `(coordination_scope, room_id, thread_id, hydrate_sidecars, allow_stale_fallback)` only for flight ownership.
Add `clear_thread_repair_room(room_id, coordination_scope=...)` as a direct registry delegation.

- [ ] **Step 6: Run GREEN tests and owning coordination tests**

```bash
uv run pytest tests/test_thread_repair.py tests/test_event_cache_write_coordination.py -x -n 0 --no-cov -q
```

Expected: PASS.

- [ ] **Step 7: Commit registry primitives**

```bash
git add src/mindroom/matrix/cache/thread_repair.py src/mindroom/matrix/cache/write_coordinator.py tests/test_thread_repair.py tests/test_event_cache_write_coordination.py
git add tach.toml
git commit -m "Simplify thread repair ownership state"
```

Add `tach.toml` only when it changed.

---

### Task 3: Move Single-Flight Ownership to the Refill Seam

**Files:**
- Modify: `src/mindroom/matrix/client_thread_history.py`
- Modify: `src/mindroom/matrix/conversation_cache.py`
- Modify: `tests/test_event_cache.py`
- Modify: `tests/test_thread_history.py`
- Modify: `tests/test_turn_dispatch_pipeline.py` only if the typed fetch signature changes its assertion
- Modify: `tach.toml` if imports change

**Interfaces:**
- Consumes: the three public thread-fetch policies and the coordinator contract-key API from Task 2
- Produces: one private cache-policy helper in `client_thread_history.py`
- Produces: an optional refill callback invoked only after cache rejection or absence

- [ ] **Step 1: Write failing healthy-cache regression**

Add a real SQLite cache-hit test to `tests/test_event_cache.py`.

```python
@pytest.mark.asyncio
async def test_healthy_cache_hit_does_not_enter_repair_lane(tmp_path: Path) -> None:
    event_cache = SqliteEventCache(tmp_path / "event_cache.db")
    await event_cache.initialize()
    await _seed_thread_cache(
        event_cache,
        room_id="!room:localhost",
        thread_id="$thread:localhost",
        events=[_clear_payload("$thread:localhost", body="root")],
    )
    conversation_cache = _conversation_cache_for_thread_reads(tmp_path, event_cache, client=MagicMock())
    coordinator = MagicMock()
    coordinator.wait_for_thread_idle = AsyncMock(return_value=None)
    coordinator.pending_thread_repair_deltas.return_value = ()
    conversation_cache.runtime.event_cache_write_coordinator = coordinator

    try:
        result = await conversation_cache.get_thread_history("!room:localhost", "$thread:localhost")
    finally:
        await event_cache.close()

    assert result
    coordinator.run_thread_repair.assert_not_awaited()
```

Use the existing real snapshot-seeding helper and its canonical room/thread IDs rather than introducing a second fixture.

- [ ] **Step 2: Write failing incompatible-owner regression**

Use a real `EventCacheWriteCoordinator`, two `asyncio.Event` barriers, a strict full owner whose Matrix fetch raises, and an advisory full caller with a valid stale snapshot.
Assert that the advisory caller uses its own contract and returns a result whose source is `stale_cache`.
Assert that strict and advisory calls create two repair ownership entries rather than sharing one exception.

- [ ] **Step 3: Run RED tests**

```bash
uv run pytest tests/test_event_cache.py -k "healthy_cache_hit_does_not_enter_repair_lane or advisory_joiner_keeps_stale_fallback_from_strict_owner_failure" -n 0 --no-cov -v
```

Expected: cache-hit test observes `run_thread_repair`, and advisory caller inherits the strict owner's exception.

- [ ] **Step 4: Consolidate duplicated cache policy**

Add one private helper in `client_thread_history.py`.

```python
type _ThreadHistoryRefill = Callable[
    [Mapping[str, str | int | float | bool] | None],
    Awaitable[ThreadHistoryResult],
]
```

The helper performs `_load_cached_thread_history_if_usable`, returns a healthy cache hit, invokes `refill(cache_reject_diagnostics)` when supplied, and otherwise calls `refresh_thread_history_from_source`.
Make `fetch_thread_history`, `fetch_dispatch_thread_history`, and `fetch_dispatch_thread_snapshot` thin policy selectors for hydration and stale fallback.
Remove `retained_event_sources` from those public fetcher signatures because only authoritative refill consumes retained deltas.

- [ ] **Step 5: Make `ConversationCache` schedule only refill work**

Before the cache probe, synchronously inspect pending delta IDs and run the existing fail-closed preparation only when pending deltas exist.
Create a refill closure that late-binds `RetainedThreadEventSourceProvider`, calls `refresh_thread_history_from_source`, acknowledges precise provided IDs, and schedules through `run_thread_repair` with the hydration and stale-fallback booleans.
Return the shared result directly because incompatible contracts now have distinct keys.
Delete the joined-result `while True` loop and its stale/hydration re-checks.

- [ ] **Step 6: Run GREEN tests and both owning files**

```bash
uv run pytest tests/test_event_cache.py tests/test_thread_history.py -x -n 0 --no-cov -q
```

Expected: PASS.

- [ ] **Step 7: Run import and boundary checks**

```bash
uv run ruff check src/mindroom/matrix/client_thread_history.py src/mindroom/matrix/conversation_cache.py tests/test_event_cache.py tests/test_thread_history.py
uv run ruff format --check src/mindroom/matrix/client_thread_history.py src/mindroom/matrix/conversation_cache.py tests/test_event_cache.py tests/test_thread_history.py
uv run ty check src/mindroom/matrix/client_thread_history.py src/mindroom/matrix/conversation_cache.py
uv run tach check --dependencies --interfaces
```

Expected: PASS.

- [ ] **Step 8: Commit refill-boundary refactor**

```bash
git add src/mindroom/matrix/client_thread_history.py src/mindroom/matrix/conversation_cache.py tests/test_event_cache.py tests/test_thread_history.py
git add tests/test_turn_dispatch_pipeline.py tach.toml
git commit -m "Own thread repairs only at the refill seam"
```

Add optional files only when changed.

---

### Task 4: Emit Completion Telemetry Per Caller

**Files:**
- Modify: `src/mindroom/matrix/client_thread_history.py`
- Modify: `tests/test_thread_history.py`
- Modify: `tests/test_event_cache.py`

**Interfaces:**
- Consumes: the cache-policy helper and contract-specific refill callback from Task 3
- Produces: exactly one `matrix_cache_thread_history_refreshed` event per public caller

- [ ] **Step 1: Write failing concurrent telemetry regression**

Start two same-contract callers that share one controlled refill.
Collect logger calls whose event name is `matrix_cache_thread_history_refreshed`.
Assert two events, one for each caller label, while the homeserver fetch runs once.

```python
assert fetch.await_count == 1
assert [call.kwargs["caller_label"] for call in refresh_logs] == [
    "first_reader",
    "second_reader",
]
```

- [ ] **Step 2: Run RED test**

```bash
uv run pytest tests/test_event_cache.py -k "shared_repair_logs_completion_for_each_caller" -n 0 --no-cov -v
```

Expected: FAIL with one owner-attributed log.

- [ ] **Step 3: Move logging to the public cache-policy return point**

Remove `_log_thread_history_refresh` calls from stale fallback and authoritative refresh internals.
After each public cache-policy read returns, derive the mode from existing diagnostics.

```python
def _thread_history_refresh_mode(result: ThreadHistoryResult, *, cache_hit: bool) -> str:
    if cache_hit:
        return "cache_hit"
    if (
        result.diagnostics.get(THREAD_HISTORY_SOURCE_DIAGNOSTIC) == THREAD_HISTORY_SOURCE_CACHE
        and result.diagnostics.get("cache_store_outcome") == ThreadCacheReplaceOutcome.EXISTING_USABLE.value
    ):
        return "cache_hit_after_repair_conflict"
    return "full_scan"
```

Call `_log_thread_history_refresh` once with the current caller's label and coordinator wait value.

- [ ] **Step 4: Run GREEN tests**

```bash
uv run pytest tests/test_thread_history.py tests/test_event_cache.py -x -n 0 --no-cov -q
```

Expected: PASS with no duplicate telemetry expectation.

- [ ] **Step 5: Commit telemetry ownership**

```bash
git add src/mindroom/matrix/client_thread_history.py tests/test_thread_history.py tests/test_event_cache.py
git commit -m "Attribute thread repair telemetry per caller"
```

---

### Task 5: Correct Failure Classification and Background Scheduling

**Files:**
- Modify: `src/mindroom/matrix/conversation_cache.py`
- Modify: `src/mindroom/matrix/cache/thread_repair.py` only if predicate naming needs refinement
- Modify: `tests/test_event_cache.py`
- Modify: `tests/test_thread_repair.py`

**Interfaces:**
- Consumes: `result_arms_backoff` from Task 2
- Produces: `_thread_repair_result_arms_backoff`
- Produces: background repair skip when `event_cache.durable_writes_available` is false

- [ ] **Step 1: Replace the incorrect `WRITES_UNAVAILABLE` expectation with RED behavior**

Change the parameterized registry test so `HARD_FAILURE` still arms backoff and add a separate `WRITES_UNAVAILABLE` test.

```python
@pytest.mark.asyncio
async def test_writes_unavailable_completion_does_not_arm_backoff() -> None:
    registry = ThreadRepairRegistry(failure_backoff_seconds=2.0)
    repair = AsyncMock(return_value=ThreadCacheReplaceOutcome.WRITES_UNAVAILABLE)
    key = ("@agent:localhost", "!room:localhost", "$thread", False, False)

    first = await registry.run(
        key,
        schedule=_schedule,
        repair=repair,
        result_arms_backoff=lambda result: result is not ThreadCacheReplaceOutcome.WRITES_UNAVAILABLE,
    )
    second = await registry.run(
        key,
        schedule=_schedule,
        repair=repair,
        result_arms_backoff=lambda result: result is not ThreadCacheReplaceOutcome.WRITES_UNAVAILABLE,
    )

    assert first is ThreadCacheReplaceOutcome.WRITES_UNAVAILABLE
    assert second is ThreadCacheReplaceOutcome.WRITES_UNAVAILABLE
    assert repair.await_count == 2
```

- [ ] **Step 2: Write failing background skip regression**

Configure a conversation cache whose event cache reports `durable_writes_available = False`.
Call `_schedule_missing_thread_repair`.
Assert no background task and no coordinator repair call.

- [ ] **Step 3: Run RED tests**

```bash
uv run pytest tests/test_thread_repair.py -k "writes_unavailable_completion_does_not_arm_backoff" -n 0 --no-cov -v
uv run pytest tests/test_event_cache.py -k "missing_thread_repair_skips_when_writes_unavailable" -n 0 --no-cov -v
```

Expected: old broad predicate arms backoff and background work starts.

- [ ] **Step 4: Implement narrow failure predicate and scheduling guard**

```python
@staticmethod
def _thread_repair_result_arms_backoff(result: ThreadHistoryResult) -> bool:
    source = result.diagnostics.get(THREAD_HISTORY_SOURCE_DIAGNOSTIC)
    if source == THREAD_HISTORY_SOURCE_CACHE or result.diagnostics.get("cache_repair_usable") is True:
        return False
    return result.diagnostics.get("cache_store_outcome") != ThreadCacheReplaceOutcome.WRITES_UNAVAILABLE.value
```

Guard background scheduling with both coordinator presence and durable-write availability.
Delete the redundant explicit `CancelledError` branch because `except Exception` does not catch cancellation.

- [ ] **Step 5: Run GREEN tests**

```bash
uv run pytest tests/test_thread_repair.py tests/test_event_cache.py -x -n 0 --no-cov -q
```

Expected: PASS.

- [ ] **Step 6: Commit failure policy**

```bash
git add src/mindroom/matrix/conversation_cache.py src/mindroom/matrix/cache/thread_repair.py tests/test_event_cache.py tests/test_thread_repair.py
git commit -m "Bound persistent thread repair failures"
```

---

### Task 6: Clear Retained Deltas at Membership Departure

**Files:**
- Modify: `src/mindroom/matrix/conversation_cache.py`
- Modify: `tests/test_event_cache.py`
- Modify: `docs/dev/matrix-event-cache-interaction-contract.md`

**Interfaces:**
- Consumes: `EventCacheWriteCoordinator.clear_thread_repair_room` from Task 2
- Produces: departure-time deletion of retained principal-room delta state

- [ ] **Step 1: Write failing departure/rejoin regression**

Retain a pre-departure event through the real coordinator.
Call `ConversationCache.purge_rooms`.
Mark the room joined again.
Assert `pending_thread_repair_deltas` is empty before the next repair.

- [ ] **Step 2: Run RED test**

```bash
uv run pytest tests/test_event_cache.py -k "departure_clears_retained_thread_repair_deltas" -n 0 --no-cov -v
```

Expected: FAIL because the pre-departure event remains for 60 seconds.

- [ ] **Step 3: Clear principal-room state at the synchronous fence**

In the first `purge_rooms` loop, clear repair state immediately after `mark_room_departed`.

```python
coordinator.clear_thread_repair_room(
    room_id,
    coordination_scope=self.runtime.event_cache.principal_id,
)
```

Keep the durable room purge queued and awaited exactly as before.

- [ ] **Step 4: Document membership-boundary behavior**

Update the interaction contract with one sentence stating that authoritative departure clears retained repair deltas before post-rejoin repair can begin.
Keep one sentence per Markdown line.

- [ ] **Step 5: Run GREEN tests**

```bash
uv run pytest tests/test_event_cache.py tests/test_thread_repair.py -x -n 0 --no-cov -q
```

Expected: PASS.

- [ ] **Step 6: Commit departure cleanup**

```bash
git add src/mindroom/matrix/conversation_cache.py tests/test_event_cache.py docs/dev/matrix-event-cache-interaction-contract.md
git commit -m "Drop retained thread deltas on departure"
```

---

### Task 7: Make Winner Read Fail Open Without a Second Scan

**Files:**
- Modify: `src/mindroom/matrix/client_thread_history.py`
- Modify: `tests/test_thread_history.py`

**Interfaces:**
- Consumes: `_ThreadCacheRefillAttempt`
- Produces: non-optional `replace_outcome` plus explicit `cache_repair_usable`
- Produces: one-scan fail-open behavior for an unreadable `EXISTING_USABLE` winner

- [ ] **Step 1: Change existing regression to the desired one-scan contract**

Rename `test_refresh_fails_open_when_existing_winner_load_keeps_failing` to `test_refresh_fails_open_without_rescanning_when_existing_winner_load_fails`.
Assert one homeserver fetch, one winner-load attempt, homeserver history returned, `cache_store_outcome == "existing_usable"`, and `cache_repair_usable is False`.

- [ ] **Step 2: Run RED test**

```bash
uv run pytest tests/test_thread_history.py -k "fails_open_without_rescanning_when_existing_winner_load_fails" -n 0 --no-cov -v
```

Expected: FAIL because current behavior performs two scans and reports `retryable_conflict`.

- [ ] **Step 3: Simplify refill-attempt state**

Use this shape.

```python
@dataclass(frozen=True, slots=True)
class _ThreadCacheRefillAttempt:
    replace_outcome: ThreadCacheReplaceOutcome
    cache_repair_usable: bool
    existing_history: ThreadHistoryResult | None = None

    @property
    def retryable(self) -> bool:
        return self.replace_outcome.retryable
```

Construct it directly and delete `_refill_attempt_for_outcome`.
On winner-load exception or an unexpectedly missing winner, log once and return `EXISTING_USABLE` with `cache_repair_usable=False`.
Use `replace_outcome.value` for diagnostics.

- [ ] **Step 4: Run GREEN test and owning file**

```bash
uv run pytest tests/test_thread_history.py -x -n 0 --no-cov -q
```

Expected: PASS.

- [ ] **Step 5: Commit winner fail-open behavior**

```bash
git add src/mindroom/matrix/client_thread_history.py tests/test_thread_history.py
git commit -m "Fail open when a repair winner cannot load"
```

---

### Task 8: Remove Unreachable and Stale Scaffolding

**Files:**
- Modify: `src/mindroom/matrix/client_thread_history.py`
- Modify: `src/mindroom/matrix/conversation_cache.py`
- Modify: `scripts/testing/benchmark_thread_history_reuse.py`
- Modify: `scripts/testing/fuzz_matrix_event_cache.py`
- Modify: `tests/test_thread_history.py`
- Modify: affected tests that import removed private helpers
- Modify: `docs/dev/matrix-event-cache-interaction-contract.md`

**Interfaces:**
- Consumes: all green behavior from Tasks 2 through 7
- Produces: explicit `STORED` script assertions and no unreachable guarded-store missing-root outcome

- [ ] **Step 1: Prove the guarded-store missing-root branch is unreachable**

```bash
rg -n "_cache_store_rejection_outcome|_thread_history_cache_rejection_reason|_store_repaired_thread_snapshot" src/mindroom/matrix/client_thread_history.py tests/test_thread_history.py
```

Expected: per-thread store has one caller after `_fetch_thread_repair_snapshot`, while existing-cache and bulk validation retain the shared missing-root classifier.

- [ ] **Step 2: Remove the mocked-impossible test and production branch**

Replace `_cache_store_rejection_outcome` with opaque-history rejection only.
Delete the `skipped_missing_thread_root` return path and the test that directly fabricates a fetched result without its root.
Keep existing-cache and bulk missing-root tests unchanged.

- [ ] **Step 3: Fix script assertions explicitly**

Import `ThreadCacheReplaceOutcome` from the leaf cache-state module in both scripts.

```python
assert replaced is ThreadCacheReplaceOutcome.STORED
```

Do not add a boolean compatibility shim.

- [ ] **Step 4: Remove remaining safe scaffolding**

Delete caller-specific `failure_message` from `_cached_thread_event_ids_for_repair` and use one stable warning.
Delete any tuple wrapper, redundant cancellation branch, dead import, and old joined-result test left after earlier tasks.
Retain `RetainedThreadEventSourceProvider` and precise acknowledgment unless an equivalent smaller implementation passes the stored-redaction regression.

- [ ] **Step 5: Correct interaction-contract wording**

Document that cache probes occur outside repair ownership, only refills enter single-flight, incompatible contracts use distinct flights, every caller logs completion, uncached `WRITES_UNAVAILABLE` completion does not arm backoff, and repeated failures use capped exponential delay.

- [ ] **Step 6: Run owning tests and static script checks**

```bash
uv run pytest tests/test_thread_history.py tests/test_event_cache.py tests/test_thread_repair.py -x -n 0 --no-cov -q
uv run ruff check scripts/testing/benchmark_thread_history_reuse.py scripts/testing/fuzz_matrix_event_cache.py src/mindroom/matrix/client_thread_history.py src/mindroom/matrix/conversation_cache.py
uv run ruff format --check scripts/testing/benchmark_thread_history_reuse.py scripts/testing/fuzz_matrix_event_cache.py src/mindroom/matrix/client_thread_history.py src/mindroom/matrix/conversation_cache.py
git diff --check
```

Expected: PASS.

- [ ] **Step 7: Commit safe simplification**

```bash
git add src/mindroom/matrix/client_thread_history.py src/mindroom/matrix/conversation_cache.py scripts/testing/benchmark_thread_history_reuse.py scripts/testing/fuzz_matrix_event_cache.py tests/test_thread_history.py tests/test_event_cache.py tests/test_thread_repair.py docs/dev/matrix-event-cache-interaction-contract.md
git commit -m "Remove obsolete thread repair scaffolding"
```

Add only files that changed.

---

### Task 9: Verify Exact Head, Push, and Re-Review

**Files:**
- Verify: all files changed since `07cf66abeed57d268912c17b2bda00c50474df7a`
- Update: PR #1656 body when behavior or validation wording changed

**Interfaces:**
- Consumes: all task commits
- Produces: pushed exact head with green focused validation, current-main merge, accurate PR description, and fresh reviews

- [ ] **Step 1: Run focused owning suites**

```bash
uv run pytest tests/test_thread_repair.py tests/test_event_cache.py tests/test_thread_history.py tests/test_event_cache_write_coordination.py tests/test_thread_read_guards.py tests/test_matrix_event_cache_security.py -x -n 0 --no-cov -q
```

Expected: PASS.

- [ ] **Step 2: Run lightweight static gates**

```bash
uv run ruff check src/mindroom/matrix/cache/thread_repair.py src/mindroom/matrix/cache/write_coordinator.py src/mindroom/matrix/client_thread_history.py src/mindroom/matrix/conversation_cache.py tests/test_thread_repair.py tests/test_event_cache.py tests/test_thread_history.py
uv run ruff format --check src/mindroom/matrix/cache/thread_repair.py src/mindroom/matrix/cache/write_coordinator.py src/mindroom/matrix/client_thread_history.py src/mindroom/matrix/conversation_cache.py tests/test_thread_repair.py tests/test_event_cache.py tests/test_thread_history.py
uv run ty check src/mindroom/matrix/cache/thread_repair.py src/mindroom/matrix/cache/write_coordinator.py src/mindroom/matrix/client_thread_history.py src/mindroom/matrix/conversation_cache.py
uv run tach check --dependencies --interfaces
uv run pre-commit run --all-files
git diff --check origin/main...HEAD
```

Expected: PASS, or exact resource-kill evidence recorded without claiming the gate passed.

- [ ] **Step 3: Check simplification and stale traces**

```bash
git --no-pager diff --stat origin/main...HEAD
rg -n "assert replaced$|_refill_attempt_for_outcome|_thread_repair_key|except BaseException|skipped_missing_thread_root" src tests scripts/testing docs
```

Expected: no stale truthiness, wrapper, broad catch, or impossible store outcome.

- [ ] **Step 4: Update PR description**

Use repo-relative paths only.
Describe cache-hit bypass, contract-specific refill ownership, caller telemetry, exponential backoff, departure cleanup, precise retained-delta preservation, one-scan winner fail-open, current-main merge, and exact validation limits.

- [ ] **Step 5: Push without rewriting history**

```bash
git status --short
git push origin fix/thread-cache-repair-convergence
```

Expected: remote branch advances by normal commits.

- [ ] **Step 6: Wait for exact-head GitHub gates**

```bash
gh pr view 1656 --json headRefOid,baseRefOid,mergeStateStatus,statusCheckRollup
```

Expected: PR head equals local head, merge state is clean, and every required check completes successfully.

- [ ] **Step 7: Run fresh independent review**

Review the complete current `origin/main...HEAD` diff with the exact-head PR-review workflow.
Treat every finding as untrusted, fix only reproducible blockers, and repeat focused validation after each accepted fix.

- [ ] **Step 8: Check unresolved threads and final cleanliness**

```bash
git status --short
git rev-parse HEAD
git rev-parse origin/fix/thread-cache-repair-convergence
git diff --check origin/main...HEAD
```

Use the GitHub GraphQL `reviewThreads` query and require zero unresolved threads.
Do not merge PR #1656.
