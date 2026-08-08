# Delivery Recovery Cliff Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task.
> Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Keep Matrix receive progress independent of durable delivery recovery and prove the production-shaped 100-stream workflow in a dedicated live profile.

**Architecture:** `AgentBot` turns each sync response into a transient wake for one owner-scoped recovery task while the response outbox remains the durable source of truth.
The live harness adds a fixed `recovery-cliff` profile with a managed sender, a built-in synthetic responder, exact reply and terminal-state correlation, and strict post-load health and drain gates.

**Tech Stack:** Python 3.13, asyncio, pytest, mindroom-nio, Matrix Client-Server API, SQLite journal probes, and the existing disposable Tuwunel harness.

## Global Constraints

The worktree was rebased at the user's request onto current PR tip `6a994538f684fac01fa5dec26fd440a479de6aad` after the live run.

The intervening PR delta changed only test isolation in `tests/conftest.py`, `tests/test_callback_manager_tool.py`, and `tests/test_turn_dispatch_pipeline.py`.

The response outbox remains the only durable delivery-recovery authority.

Every new behavior follows a red-green-refactor cycle.

The deterministic regression uses causal events rather than load-sensitive short sleeps.

The live profile launches exactly 100 roots before waiting for a response.

The managed `load_sender` account authors every root and explicitly mentions managed responder `general`.

The responder uses `provider: synthetic`, 80 characters per second, 4,000 to 4,800 response characters, 40-character chunks, and a 0.2 tool-call probability.

The profile uses Sliding Sync with `sliding_timeline_limit: 100`.

The live service-level deadline is fixed and never extended because partial progress continues.

No private deployment name, bridge name, credential, access token, or database URL enters code, tests, docs, output, or commit messages.

---

### Task 1: Detach and coalesce response delivery recovery

**Files:**

- Modify: `tests/test_sync_task_cancellation.py`
- Modify: `src/mindroom/bot.py`

**Interfaces:**

- Consumes: `DeliveryGateway.recover_deliveries() -> RecoveryOutcome` and owner-scoped `create_background_task` and `wait_for_background_tasks`.
- Produces: `AgentBot._schedule_delivery_recovery() -> None`, `AgentBot._run_scheduled_delivery_recovery() -> None`, and `AgentBot._recover_unacknowledged_deliveries() -> bool`.

- [x] **Step 1: Add the causal receive-loop regression**

Add a test whose recovery coroutine waits for an event set only after the first real `_on_sync_response()` returns and the second response begins.

```python
@pytest.mark.asyncio
async def test_sync_response_returns_while_delivery_recovery_waits_for_the_next_response(
    tmp_path: Path,
) -> None:
    bot = _sliding_response_bot(tmp_path)
    recovery_started = asyncio.Event()
    next_response_started = asyncio.Event()
    first_response_returned = asyncio.Event()

    async def recover_deliveries() -> RecoveryOutcome:
        recovery_started.set()
        await next_response_started.wait()
        return RecoveryOutcome(recovered=0, failed=0)

    async def drive_responses() -> None:
        await bot._on_sync_response(nio.SlidingSyncResponse("pos-one"))
        first_response_returned.set()
        next_response_started.set()
        await bot._on_sync_response(nio.SlidingSyncResponse("pos-two"))

    with patch.object(
        bot,
        "_delivery_gateway",
        new=SimpleNamespace(recover_deliveries=recover_deliveries),
    ):
        driver = asyncio.create_task(drive_responses())
        try:
            await recovery_started.wait()
            await asyncio.sleep(0)
            assert first_response_returned.is_set()
        finally:
            next_response_started.set()
            await driver
            assert await wait_for_background_tasks(timeout=1, owner=bot._runtime_view)
```

- [x] **Step 2: Run the causal test and verify RED**

Run:

```bash
uv run pytest -q tests/test_sync_task_cancellation.py::test_sync_response_returns_while_delivery_recovery_waits_for_the_next_response -n 0 --no-cov
```

Expected: the assertion that the first response returned fails because the callback still awaits recovery.

- [x] **Step 3: Add coalescing and shutdown ownership regressions**

Add a parked first pass followed by two completed sync callbacks and require exactly two total passes with a maximum active count of one.

Add a shutdown test that forces the existing owner drain to expire immediately and requires cancellation to reach the parked recovery coroutine.

Update `test_delivery_recovery_asks_the_outbox_on_every_sync_response` to drain owner-scoped tasks after each response so its exception, incomplete, success, and later-new-debt outcomes remain causal after detachment.

- [x] **Step 4: Implement the minimal owner-scoped recovery task**

Initialize one event and one task reference in `AgentBot.__init__`.

```python
self._delivery_recovery_wake = asyncio.Event()
self._delivery_recovery_task: asyncio.Task[None] | None = None
```

Make the existing pass report completion without swallowing cancellation.

```python
async def _recover_unacknowledged_deliveries(self) -> bool:
    try:
        outcome = await self._delivery_gateway.recover_deliveries()
    except Exception:
        self.logger.exception("Delivery recovery failed")
        return False
    if outcome.recovered:
        self.logger.info("Resent unacknowledged deliveries", deliveries=outcome.recovered)
    if not outcome.complete:
        self.logger.warning("Deliveries still unsent after recovery", deliveries=outcome.failed)
    return outcome.complete
```

Schedule one task and make new sync progress interrupt retry backoff.

```python
def _schedule_delivery_recovery(self) -> None:
    if self._sync_shutting_down:
        return
    self._delivery_recovery_wake.set()
    task = self._delivery_recovery_task
    if task is not None and not task.done():
        return
    self._delivery_recovery_task = create_background_task(
        self._run_scheduled_delivery_recovery(),
        name=f"delivery_recovery_{self.agent_name}",
        owner=self._runtime_view,
    )
```

The runner clears the wake before each pass, retries incomplete work with capped backoff, and clears only its own task reference in `finally`.

Replace the direct recovery await in `_run_sync_response_side_effects` with `_schedule_delivery_recovery()`.

Set the wake immediately after `_sync_shutting_down = True` so a backoff wait exits and the existing owner drain can finish or cancel the task.

- [x] **Step 5: Run the focused recovery tests and verify GREEN**

Run:

```bash
uv run pytest -q tests/test_sync_task_cancellation.py -k 'delivery_recovery or sync_shutdown_cancels_the_owned_delivery' -n 0 --no-cov
```

Expected: every selected test passes with no warnings.

- [x] **Step 6: Run the complete sync lifecycle file and commit**

Run:

```bash
uv run pytest -q tests/test_sync_task_cancellation.py -n 0 --no-cov
```

Commit:

```bash
git add src/mindroom/bot.py tests/test_sync_task_cancellation.py
git commit -m "fix(matrix): keep delivery recovery off sync callbacks"
```

### Task 2: Separate short-stream correctness from the recovery cliff

**Files:**

- Modify: `scripts/testing/fuzz_live_matrix.py`
- Modify: `tests/test_live_matrix_fuzz.py`
- Modify: `scripts/README.md`

**Interfaces:**

- Consumes: `LiveFuzzScenario`, `ManagedTuwunelStack`, `LiveMatrixClient`, and the existing exact-response relation index.
- Produces: `short_stream_correctness_scenario() -> LiveFuzzScenario`, `recovery_cliff_scenario() -> LiveFuzzScenario`, and profile-specific managed config.

- [x] **Step 1: Write profile and config tests**

Rename the old saturation test and require the fixed short-stream shape under profile `short-stream-correctness`.

Add a fixed recovery profile test with 100 threads and no trace batches.

Add a config test that parses the written YAML and asserts these exact literals.

```python
assert config["matrix_sync"] == {
    "mode": "sliding",
    "sliding_timeline_limit": 100,
}
assert config["models"]["synthetic"]["provider"] == "synthetic"
assert config["models"]["synthetic"]["extra_kwargs"] == {
    "seed": 1,
    "min_response_chars": 4000,
    "max_response_chars": 4800,
    "chunk_chars": 40,
    "chars_per_second": 80,
    "tool_call_probability": 0.2,
}
assert config["agents"]["general"]["model"] == "synthetic"
assert config["agents"]["load_sender"]["rooms"] == ["lobby"]
```

Add a credential fixture containing both `agent_general` and `agent_load_sender`, and require lookup for `load_sender` to return only its token and device.

- [x] **Step 2: Run the new profile tests and verify RED**

Run:

```bash
uv run pytest -q tests/test_live_matrix_fuzz.py -k 'short_stream or recovery_cliff or managed_agent_credentials' -n 0 --no-cov
```

Expected: imports or assertions fail because the fixed profile, renamed profile, and managed-sender config do not exist.

- [x] **Step 3: Implement fixed profile selection and managed configuration**

Allow exactly `fuzz`, `restart-regression`, `short-stream-correctness`, and `recovery-cliff` profiles.

Keep the existing 13-thread short-stream scenario unchanged except for its truthful name.

Represent recovery cliff as `LiveFuzzScenario(thread_count=100, batches=(), profile="recovery-cliff")` and reject any altered trace shape.

Pass the selected profile into `ManagedTuwunelStack` so `_write_config` adds `load_sender`, switches `general` to the built-in synthetic model, and selects Sliding Sync only for recovery cliff.

Generalize `agent_matrix_credentials()` to accept `agent_name: str = AGENT_NAME` and read `accounts[f"agent_{agent_name}"]`.

Update CLI choices, default deadlines, result labels, and `scripts/README.md` so short-stream correctness cannot be presented as a capacity result.

- [x] **Step 4: Run the profile tests and verify GREEN**

Run:

```bash
uv run pytest -q tests/test_live_matrix_fuzz.py -k 'short_stream or recovery_cliff or managed_agent_credentials or scenario' -n 0 --no-cov
```

Expected: every selected test passes.

- [x] **Step 5: Commit profile configuration**

```bash
git add scripts/testing/fuzz_live_matrix.py tests/test_live_matrix_fuzz.py scripts/README.md
git commit -m "test(matrix): define the delivery recovery cliff profile"
```

### Task 3: Enforce the live recovery-cliff acceptance contract

**Files:**

- Modify: `scripts/testing/fuzz_live_matrix.py`
- Modify: `tests/test_live_matrix_fuzz.py`

**Interfaces:**

- Consumes: the fixed `recovery-cliff` scenario and managed `load_sender` credentials from Task 2.
- Produces: concurrent root release, terminal stream audit, post-load drain audit, health and sync-progress audit, and clean bounded shutdown.

- [x] **Step 1: Write the 100-root launch barrier test**

Use a fake sender whose every `send_event()` waits until all 100 calls have entered.

Run the root-release helper and require all calls to complete, which fails if the implementation awaits any send before launching the rest.

Require every payload to contain one distinct `run=<id> thread=<0..99>` marker and an explicit `m.mentions.user_ids` entry for `general`.

- [x] **Step 2: Write pure terminal and drain audit tests**

Build literal Matrix originals and `m.replace` edits with `io.mindroom.stream_status` values.

Require the audit to accept one original with exactly one effective `completed` terminal state per expected source.

Require it to reject a missing original, duplicate originals, a nonterminal latest edit, a response to an unknown source, absent recovery markers, a pending journal row, an unacknowledged outbox row, unhealthy health evidence, a watchdog stall, absent post-load sync progress, or unclean shutdown.

Seed SQLite with both terminal and live rows and prove that the drain query counts only `journal_events.state = 'pending'` and `response_outbox.acknowledged_event_id IS NULL`.

- [x] **Step 3: Run the acceptance tests and verify RED**

Run:

```bash
uv run pytest -q tests/test_live_matrix_fuzz.py -k 'recovery_cliff' -n 0 --no-cov
```

Expected: the launch helper and acceptance evaluator are absent or fail their new assertions.

- [x] **Step 4: Implement the fixed live runner**

Authenticate a `LiveMatrixClient` with the persisted `load_sender` access token without registering another Matrix user.

Warm one sender-to-responder turn, then release all 100 roots in one `asyncio.gather()` before entering response observation.

Use the existing thread relation index to correlate responder originals to exact source event IDs.

For originals and edits, read `io.mindroom.stream_status` from `m.new_content` when present and otherwise from `content`.

Poll one strict observer cursor, `/api/health`, runtime liveness, exact replies, terminal statuses, and the fixed service-level deadline together.

For every positioned observer sync, enumerate the complete cursor interval forward through `/messages` and publish its raw events plus the new cursor only after bounded completion.

Require an exact-room `Waiting to retry Matrix delivery after sync recovery` post-warm log delta, at least one attempted and unacknowledged workload FINAL outbox row, a post-warm `general` detached-worker resend marker, and no room recovery-abandonment marker so a non-exercising run cannot pass.

After replies finish, wait within one fixed drain deadline for no pending journal rows and no unacknowledged outbox rows.

Send one post-load reaction and wait until the exact responder journal row is settled to prove that receive progress resumed.

Compare health `last_sync_time` before and after the post-load fence, require zero `matrix_sync_watchdog_stalled`, and call `stop_mindroom()` within its existing bound.

Return a machine-readable result with the root count, canonical response count, recovery marker counts, maximum active stream duration, drain counts, and `status: PASS`.

- [x] **Step 5: Run acceptance unit tests and the complete harness test file**

Run:

```bash
uv run pytest -q tests/test_live_matrix_fuzz.py -k 'recovery_cliff' -n 0 --no-cov
uv run pytest -q tests/test_live_matrix_fuzz.py -n 0 --no-cov
```

Expected: both commands pass without warnings.

- [x] **Step 6: Commit the live runner**

```bash
git add scripts/testing/fuzz_live_matrix.py tests/test_live_matrix_fuzz.py
git commit -m "test(matrix): exercise 100 sustained managed streams"
```

### Task 4: Verify the integrated branch

**Files:**

- Verify: `src/mindroom/bot.py`
- Verify: `scripts/testing/fuzz_live_matrix.py`
- Verify: `tests/test_sync_task_cancellation.py`
- Verify: `tests/test_live_matrix_fuzz.py`

**Interfaces:**

- Consumes: the runtime and harness changes from Tasks 1 through 3.
- Produces: evidence that the focused behavior, broader lifecycle, formatting, boundaries, and live workflow are all valid.

- [x] **Step 1: Run focused and adjacent tests**

```bash
uv run pytest -q \
  tests/test_sync_task_cancellation.py \
  tests/test_bot_ready_hook.py \
  tests/test_response_delivery_gateway.py \
  tests/test_send_file_message.py \
  tests/test_live_matrix_fuzz.py \
  tests/test_synthetic_model.py \
  -n 0 --no-cov
```

- [x] **Step 2: Run static checks**

```bash
uv run ruff check src/mindroom/bot.py scripts/testing/fuzz_live_matrix.py tests/test_sync_task_cancellation.py tests/test_live_matrix_fuzz.py
uv run ruff format --check src/mindroom/bot.py scripts/testing/fuzz_live_matrix.py tests/test_sync_task_cancellation.py tests/test_live_matrix_fuzz.py
uv run tach check --dependencies --interfaces
git diff --check e4cdd2f32733..HEAD
```

- [x] **Step 3: Run the live acceptance profile**

```bash
uv run python scripts/testing/fuzz_live_matrix.py \
  --profile recovery-cliff \
  --reply-timeout 180 \
  --failure-log /tmp/mindroom-recovery-cliff.log
```

Expected: exactly 100 canonical completed replies, at least one exact-room delivery-retry marker, observed attempted and unacknowledged workload FINAL debt, a `general` detached-worker resend marker, no recovery abandonment, zero pending journal rows, zero unacknowledged outbox rows, healthy advancing sync, zero watchdog stalls, clean shutdown, and `status: PASS`.

- [x] **Step 4: Review the diff and record verification**

Inspect every production branch added to `bot.py`, confirm no durable debt flag or extra lifecycle owner exists, and confirm the harness never prints credentials or database URLs.

Update `CODEX_REVIEW.md` in the shared review checkout with the tested commit and exact command results without copying private deployment details into the branch.
