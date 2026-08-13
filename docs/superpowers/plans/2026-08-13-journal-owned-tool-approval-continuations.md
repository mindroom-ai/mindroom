# Journal-Owned Tool Approval Continuations Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Replace PR #1807's separate seven-state continuation database and dispatcher with event-journal-owned paused-run state dispatched through the original pending source.

**Architecture:** The event journal stores normalized continuation, source, and exact-call rows beside the existing card and outbox ledgers. The original Matrix source remains pending, the journal query exposes it only when continuation work is runnable, and normal response lifecycle re-entry performs Agno continuation under the existing per-conversation serializer. Current-format card decisions and exact-call decisions commit in one transaction, while the existing legacy-card settlement remains for one deployment cycle.

**Tech Stack:** Python 3.13, asyncio, SQLite and PostgreSQL through `event_journal.backend.Transaction`, Matrix nio, Agno 2.6.12, pytest with xdist, Tach.

## Global Constraints

Agno's persisted paused run remains the only execution approval boundary.

No live future, response coroutine, typing indicator, or conversation lock waits for a human decision.

The event journal is the only MindRoom-owned continuation database.

The original source event remains the continuation work item.

The response outbox remains the durable Matrix delivery authority.

Legacy card rows remain readable and fail closed, but never resume a tool.

Non-resumable surfaces do not expose approval-gated tools.

Every production behavior change follows a witnessed red-green TDD cycle.

All pytest commands use `-n auto`.

The redesign removes at least 1,000 production lines from the current branch before acceptance.

---

### Task 1: Journal Continuation State and Exact Calls

**Files:**
- Create: `src/mindroom/event_journal/approval_continuations.py`
- Modify: `src/mindroom/event_journal/schema.py`
- Modify: `src/mindroom/event_journal/models.py`
- Modify: `src/mindroom/event_journal/store.py`
- Modify: `src/mindroom/event_journal/__init__.py`
- Modify: `tests/test_event_journal_store.py`

**Interfaces:**
- Produces: `ApprovalContinuation`, `ApprovalCall`, and `ApprovalDecision` immutable values.
- Produces: `PrincipalStore.create_approval_continuation(continuation) -> ApprovalContinuation | None`.
- Produces: `PrincipalStore.approval_continuation_for_source(event_id) -> ApprovalContinuation | None`.
- Produces: `PrincipalStore.claim_approval_continuation(approval_id, runtime_generation) -> ApprovalContinuation | None`.
- Produces: `PrincipalStore.advance_approval_continuation(approval_id, claimant_generation, ...) -> ApprovalContinuation | None`.
- Produces: `PrincipalStore.request_approval_failure(approval_id, reason, expected_state) -> ApprovalContinuation | None`.
- Produces: `PrincipalStore.finish_approval_continuation(approval_id) -> bool`, guarded on an acknowledged FINAL outbox row.

- [ ] **Step 1: Write the failing backend-parity tests**

Add contract tests that admit two source events, create one continuation, assert both sources resolve to it, assert only one `ready -> claimed` transition wins, assert a current-generation claim stays hidden, assert an old-generation claim becomes recoverable, and assert finish refuses before FINAL acknowledgement.

Use literal values and real backend stores rather than mocking SQL operations.

```python
continuation = ApprovalContinuation(
    approval_id="approval-1",
    run_id="run-1",
    session_id="session-1",
    entity_kind="agent",
    entity_name="agent",
    room_id=ROOM,
    thread_id=THREAD,
    requester_id=ALICE,
    response_event_id="$waiting",
    source_event_ids=("$source-1", "$source-2"),
    calls=(ApprovalCall("call-1", "shell", "agent", expires_at_ns=deadline),),
    snapshot={"request_body": "run it"},
    state="ready",
)
assert await store.create_approval_continuation(continuation) == continuation
assert (await store.approval_continuation_for_source("$source-2")).approval_id == "approval-1"
assert await store.claim_approval_continuation("approval-1", "runtime-a") is not None
assert await store.claim_approval_continuation("approval-1", "runtime-b") is None
```

- [ ] **Step 2: Run the tests and verify the missing journal APIs fail**

Run: `uv run pytest tests/test_event_journal_store.py -q -n auto --no-cov -k 'approval_continuation'`

Expected: FAIL because the new continuation values and store methods do not exist.

- [ ] **Step 3: Implement the smallest normalized schema and operations**

Create tables for continuations, source links, and calls with foreign keys and indexes on source lookup and runnable state.

Keep serialization in one module and use integer nanosecond deadlines.

Use `UPDATE ... RETURNING` for guarded transitions on both backends.

Do not add completed or failed states.

```python
type ApprovalContinuationState = Literal["waiting", "ready", "claimed", "failing"]

@dataclass(frozen=True, slots=True)
class ApprovalCall:
    tool_call_id: str
    tool_name: str
    invoking_agent: str
    expires_at_ns: int
    decision: ApprovalDecision | None = None
    reason: str | None = None

@dataclass(frozen=True, slots=True)
class ApprovalContinuation:
    approval_id: str
    run_id: str
    session_id: str
    entity_kind: Literal["agent", "team"]
    entity_name: str
    room_id: str
    thread_id: str | None
    requester_id: str
    response_event_id: str
    source_event_ids: tuple[str, ...]
    calls: tuple[ApprovalCall, ...]
    snapshot: Mapping[str, object]
    state: ApprovalContinuationState
    runtime_generation: str | None = None
    failure_reason: str | None = None
    generation: int = 0
```

- [ ] **Step 4: Run backend parity and existing journal tests**

Run: `uv run pytest tests/test_event_journal_store.py -q -n auto --no-cov`

Expected: PASS on SQLite and configured PostgreSQL contract cases.

- [ ] **Step 5: Commit the journal state slice**

```bash
git add src/mindroom/event_journal tests/test_event_journal_store.py
git commit -m "refactor: store approval continuations in event journal"
```

### Task 2: Atomic Card and Exact-Call Decisions

**Files:**
- Modify: `src/mindroom/event_journal/schema.py`
- Modify: `src/mindroom/event_journal/approvals.py`
- Modify: `src/mindroom/event_journal/store.py`
- Modify: `src/mindroom/approval_manager.py`
- Modify: `src/mindroom/approval_transport.py`
- Modify: `tests/test_event_journal_store.py`
- Modify: `tests/test_tool_approval.py`

**Interfaces:**
- Consumes: normalized continuation and call rows from Task 1.
- Produces: `PrincipalStore.resolve_continuation_approval_card(card_event_id, requested_status, reason, resolution) -> RecordedApprovalDecision`.
- Produces: one returned durable winning resolution whose `status` and `resolution_reason` already reflect deadline enforcement and first-decision-wins.

- [ ] **Step 1: Write failing atomic-decision tests**

Cover approval before deadline, approval at or after deadline becoming `expired`, duplicate denial after approval preserving approval, unknown call failing closed, card write failure leaving the call undecided, and the final undecided call changing the continuation to `ready` in the same transaction.

Assert both the card row and continuation call from real stores after each operation.

- [ ] **Step 2: Run the tests and witness the two-ledger gap**

Run: `uv run pytest tests/test_event_journal_store.py tests/test_tool_approval.py -q -n auto --no-cov -k 'atomic_continuation_decision or detached_card_displays'`

Expected: FAIL because card resolution and continuation decision are separate writes.

- [ ] **Step 3: Add current-format card identity and one transaction**

Add nullable `continuation_id` and `tool_call_id` card columns.

Populate them from claimed current-format card content without affecting legacy rows.

In one transaction, guard the card's first resolution, guard the exact call's first decision, enforce `expires_at_ns`, update the call, update the continuation to ready when no undecided calls remain, and persist the final resolution JSON.

```sql
UPDATE approval_continuation_calls
SET decision = ?, reason = ?
WHERE approval_id = ? AND generation = ? AND tool_call_id = ? AND decision IS NULL
RETURNING tool_call_id
```

- [ ] **Step 4: Route current-format manager decisions through the atomic operation**

Keep `_emit_resolution` for legacy cards.

For a card with continuation identity, commit once, wake the owning source when the transaction returns ready, and deliver the returned stored resolution without another decision write.

Delete `detached_decision_ready`, `acknowledge_call`, `decision_recorded`, and recorded-continuation restoration.

- [ ] **Step 5: Run focused card and manager tests**

Run: `uv run pytest tests/test_event_journal_store.py tests/test_tool_approval.py tests/test_bot_reactions_approvals.py -q -n auto --no-cov`

Expected: PASS.

- [ ] **Step 6: Commit the atomic decision slice**

```bash
git add src/mindroom/event_journal src/mindroom/approval_manager.py src/mindroom/approval_transport.py tests/test_event_journal_store.py tests/test_tool_approval.py tests/test_bot_reactions_approvals.py
git commit -m "refactor: commit approval decisions with continuation state"
```

### Task 3: Dispatch Ready Continuations Through Original Sources

**Files:**
- Modify: `src/mindroom/event_journal/journal.py`
- Modify: `src/mindroom/event_journal/store.py`
- Modify: `src/mindroom/journal_dispatch.py`
- Modify: `src/mindroom/pending_event_worker.py`
- Modify: `src/mindroom/response_runner.py`
- Modify: `src/mindroom/bot.py`
- Modify: `tests/test_event_journal_store.py`
- Modify: `tests/test_response_runner_focused.py`
- Modify: `tests/test_event_journal_binding.py`
- Modify: `tests/test_event_journal_crash_matrix.py`

**Interfaces:**
- Consumes: continuation source links and states from Task 1.
- Produces: journal pending pages that hide waiting/current claims and return only the primary source for ready, failing, or old-runtime claimed work.
- Produces: `ResponseRunner.retry_approval_sources(source_event_ids)`, wired to `JournalDispatcher.retry_turn_sources`.
- Produces: `_run_owned_or_locked_response` behavior that claims and resumes ready continuations instead of merely adopting them.

- [ ] **Step 1: Write failing pending-query and end-to-end wake tests**

Test a waiting coalesced continuation returns neither source, a ready continuation returns only its primary source, a current claim returns none, and an old-runtime claim returns only its primary source.

Add an integration test that records the last human decision, wakes the existing journal worker, enters the normal response lifecycle once, and never calls a transport continuation scheduler.

- [ ] **Step 2: Run the tests and witness the old dispatcher dependency**

Run: `uv run pytest tests/test_event_journal_store.py tests/test_event_journal_binding.py tests/test_event_journal_crash_matrix.py tests/test_response_runner_focused.py -q -n auto --no-cov -k 'approval_source or ready_approval or journal_worker'`

Expected: FAIL because pending reads do not consult continuation ownership and the runner still delegates scheduling to the orchestrator.

- [ ] **Step 3: Filter pending rows at the journal source of truth**

Change the pending SQL so secondary continuation sources are always excluded and the primary source is filtered by state and runtime generation.

Pass the current runtime generation to pending reads through the principal view rather than storing process state globally.

- [ ] **Step 4: Resume from the existing response lifecycle entry**

Replace `_run_owned_or_locked_response`'s adopt-only branch with state-specific handling.

`ready` claims and executes under the current admission and conversation locks.

`waiting` preserves the acknowledged wait.

An old claim recovers FINAL debt without invoking Agno.

`failing` runs visible terminal settlement only.

Remove runner-to-orchestrator schedule and failure request callbacks.

- [ ] **Step 5: Run response, dispatch, and source-ownership tests**

Run: `uv run pytest tests/test_event_journal_store.py tests/test_event_journal_binding.py tests/test_event_journal_crash_matrix.py tests/test_response_runner_focused.py tests/test_response_turn.py -q -n auto --no-cov`

Expected: PASS.

- [ ] **Step 6: Commit the source-worker slice**

```bash
git add src/mindroom/event_journal src/mindroom/journal_dispatch.py src/mindroom/pending_event_worker.py src/mindroom/response_runner.py src/mindroom/bot.py tests
git commit -m "refactor: resume approvals through pending source worker"
```

### Task 4: Simplify Suspension and Card Publication

**Files:**
- Modify: `src/mindroom/approval_response.py`
- Modify: `src/mindroom/response_runner.py`
- Modify: `src/mindroom/approval_manager.py`
- Modify: `tests/test_response_runner_focused.py`
- Modify: `tests/test_tool_approval.py`

**Interfaces:**
- Consumes: `PrincipalStore.create_approval_continuation` and atomic card identity.
- Produces: suspension ordering of acknowledged INITIAL wait, one continuation create, then claim-before-send cards.
- Produces: `ApprovalResponseCoordinator.publish_cards` without continuation card attachment writes.

- [ ] **Step 1: Write failing crash and cancellation tests for the new ordering**

Cover crash before continuation creation replaying safely, crash after creation retaining exact paused run ownership, cancellation after create moving the row to failing, partial card publication discoverable entirely from card rows, and a recovered unacknowledged card becoming terminal without a copied card event ID.

- [ ] **Step 2: Run the tests and witness publishing-state dependencies**

Run: `uv run pytest tests/test_response_runner_focused.py tests/test_tool_approval.py -q -n auto --no-cov -k 'suspension or publication or partial_approval_card'`

Expected: FAIL against create-before-wait and card attachment behavior.

- [ ] **Step 3: Deliver wait first and create a born-bound continuation**

Construct the exact snapshot and policy plan before delivery.

Deliver or edit the INITIAL waiting response through the existing outbox.

Create the continuation once with a non-null response event ID and initial `waiting` or `ready` state.

On pre-create failure, let normal lifecycle cancellation or failure settlement own the original source.

On post-create failure, request `failing`, release the source to the journal worker, and return a suspended outcome.

- [ ] **Step 4: Remove continuation card attachment duplication**

Publish cards with deterministic IDs and continuation metadata.

Use card rows as the only card-event mapping.

Delete attach callbacks, attach recovery, and `card_event_id` from continuation calls.

- [ ] **Step 5: Run suspension, card, streaming, and cancellation tests**

Run: `uv run pytest tests/test_response_runner_focused.py tests/test_tool_approval.py tests/test_streaming.py tests/test_sync_task_cancellation.py -q -n auto --no-cov`

Expected: PASS.

- [ ] **Step 6: Commit the suspension slice**

```bash
git add src/mindroom/approval_response.py src/mindroom/response_runner.py src/mindroom/approval_manager.py tests
git commit -m "refactor: simplify durable approval suspension handoff"
```

### Task 5: Final Outbox Handoff, Recovery, and Unavailable Owners

**Files:**
- Modify: `src/mindroom/response_delivery.py`
- Modify: `src/mindroom/delivery_gateway.py`
- Modify: `src/mindroom/approval_response.py`
- Modify: `src/mindroom/response_runner.py`
- Modify: `src/mindroom/approval_transport.py`
- Modify: `src/mindroom/orchestrator.py`
- Modify: `tests/test_turn_delivery_handoff.py`
- Modify: `tests/test_response_delivery_gateway.py`
- Modify: `tests/test_response_runner_focused.py`
- Modify: `tests/test_approval_continuation_store.py`
- Modify: `tests/test_orchestrator_runtime.py`

**Interfaces:**
- Produces: `FinalDeliveryRequest.defer_source_handoff: bool` for approval continuations only.
- Produces: terminal continuation finish that requires acknowledged FINAL and atomically settles sources plus deletes the continuation.
- Produces: bounded unavailable-owner scan and router terminal notice without Agno continuation or cross-sender edit.

- [ ] **Step 1: Write failing FINAL crash-boundary tests**

Cover FINAL enqueue followed by send failure, restart recovery sending the frozen success once, no second Agno call, completion only after acknowledgement, hook suppression leaving a terminal failure owed, and router fallback for a removed owner.

- [ ] **Step 2: Run the tests and witness current reconciliation requirements**

Run: `uv run pytest tests/test_turn_delivery_handoff.py tests/test_response_delivery_gateway.py tests/test_response_runner_focused.py tests/test_approval_continuation_store.py tests/test_orchestrator_runtime.py -q -n auto --no-cov -k 'approval and (final or unavailable or removed or frozen)'`

Expected: FAIL until source handoff is deferred and journal-owned recovery is wired.

- [ ] **Step 3: Defer approval source handoff until FINAL acknowledgement**

Allow approval final delivery to enqueue and freeze the FINAL row without settling the source.

After acknowledgement and normal lifecycle effects, call `finish_approval_continuation` to settle all sources and delete the row atomically.

On an old claim, recover the existing outbox row first and finish only if it is acknowledged.

Never enqueue a failure over an existing successful FINAL payload.

- [ ] **Step 4: Keep only the unavailable-owner fallback in transport**

At config removal, permanent start failure, and startup after runtime support is ready, scan continuations whose configured owner cannot run.

Move each exact row to failing with a guarded update and use the router to send a principal-separated terminal notice.

Do not keep per-approval execution dispatcher tasks, claim retry loops, generation backoff, or runner resume callbacks in transport.

- [ ] **Step 5: Run lifecycle, outbox, reload, and real Agno tests**

Run: `uv run pytest tests/test_turn_delivery_handoff.py tests/test_response_delivery_gateway.py tests/test_response_runner_focused.py tests/test_approval_continuation_store.py tests/test_orchestrator_runtime.py tests/test_dynamic_tool_continuation_delivery.py -q -n auto --no-cov`

Expected: PASS.

- [ ] **Step 6: Commit the terminal handoff slice**

```bash
git add src/mindroom/response_delivery.py src/mindroom/delivery_gateway.py src/mindroom/approval_response.py src/mindroom/response_runner.py src/mindroom/approval_transport.py src/mindroom/orchestrator.py tests
git commit -m "refactor: settle approval continuations through response outbox"
```

### Task 6: Delete the Old Store and Dispatcher

**Files:**
- Delete: `src/mindroom/approval_continuation.py`
- Modify: `src/mindroom/approval_transport.py`
- Modify: `src/mindroom/approval_response.py`
- Modify: `src/mindroom/response_runner.py`
- Modify: `src/mindroom/bot.py`
- Modify: `src/mindroom/orchestrator.py`
- Modify: `src/mindroom/runtime_protocols.py`
- Modify: `tach.toml`
- Modify: `CLAUDE.md`
- Modify: `tests/test_approval_continuation_store.py`
- Modify: `tests/response_runner_helpers.py`

**Interfaces:**
- Consumes: journal APIs and source-worker execution from Tasks 1 through 5.
- Produces: no Agno approval-table store, no transport execution dispatcher, no runner scheduling callbacks, and no dual store handle.

- [ ] **Step 1: Retarget behavioral tests and delete implementation-pinning tests**

Move store behavior tests to the event-journal contract.

Keep tests for user-visible and crash behavior.

Delete tests that only assert the removed states, private wrappers, retry task names, or to-thread calls.

- [ ] **Step 2: Delete production paths and search for remnants**

Remove `ApprovalContinuationStore`, `publishing`, `settling`, `completed`, `failed`, `claimant_id`, `decision_recorded`, `schedule_approval_continuation`, `request_approval_continuation_failure`, `_continuation_tasks`, and continuation store close ownership.

Run: `rg 'ApprovalContinuationStore|decision_recorded|schedule_approval_continuation|request_approval_continuation_failure|_continuation_tasks|state == "publishing"|state == "settling"' src tests`

Expected: no active-path matches.

- [ ] **Step 3: Update module boundaries and architecture docs**

Update Tach dependencies for the event-journal continuation module and remove obsolete approval store edges.

Update the `CLAUDE.md` module table and approval architecture text with one sentence per Markdown line.

- [ ] **Step 4: Run the complete focused suite**

Run:

```bash
uv run pytest \
  tests/test_event_journal_store.py \
  tests/test_approval_continuation_store.py \
  tests/test_approval_response.py \
  tests/test_tool_approval.py \
  tests/test_tool_hooks.py \
  tests/test_response_runner_agent.py \
  tests/test_response_runner_focused.py \
  tests/test_response_turn.py \
  tests/test_streaming.py \
  tests/test_ai_user_id.py \
  tests/test_team_media_fallback.py \
  tests/test_agents.py \
  tests/test_sync_task_cancellation.py \
  tests/test_orchestrator_runtime.py \
  tests/test_bot_reactions_approvals.py \
  tests/test_dynamic_tool_continuation_delivery.py \
  -q -n auto --no-cov --disable-warnings --maxfail=1
```

Expected: PASS.

- [ ] **Step 5: Check the production deletion budget before final verification**

Run: `git diff --numstat origin/main -- src | awk '{a+=$1; d+=$2} END {print "src additions", a, "deletions", d, "net", a-d}'`

Expected: at least 1,000 fewer net production lines than the pre-redesign `+3238` branch delta, with a target net between `+1800` and `+2200`.

If the reduction is smaller, inspect retained dispatcher, store wrapper, recovery, and compatibility code before proceeding.

- [ ] **Step 6: Commit the deletion slice**

```bash
git add -A src tests tach.toml CLAUDE.md
git commit -m "refactor: remove parallel approval continuation protocol"
```

### Task 7: Full Verification, PR Accounting, and Push

**Files:**
- Modify: PR #1807 body through `gh pr edit`.
- Remove before final push if still present: `PR-1807-LEGACY-REMOVAL-HANDOFF.md`.

**Interfaces:**
- Produces: verified pushed head, accurate PR diff accounting, and no merge action.

- [ ] **Step 1: Run the full suite twice with xdist**

Run: `uv run pytest -q -n auto --disable-warnings --maxfail=1`

Expected: PASS.

Run the same command a second time to expose load-sensitive lifecycle races.

Expected: PASS.

- [ ] **Step 2: Run repository checks**

Run: `uv run pre-commit run --all-files`

Run: `uv run tach check --dependencies --interfaces`

Run: `git diff --check`

Expected: all commands exit zero.

- [ ] **Step 3: Audit scope and production size**

Run: `git status --short`

Run: `git diff --stat origin/main...HEAD`

Run: `git diff --numstat origin/main...HEAD -- src | awk '{a+=$1; d+=$2} END {print a, d, a-d}'`

Confirm the two user-owned untracked review files remain untouched.

- [ ] **Step 4: Update the PR body with current architecture and exact counts**

State that the event journal owns continuation state, the original source worker dispatches configured owners, the router only settles unavailable owners, and legacy cards remain for one deployment cycle.

Remove stale line counts and stale independent-review claims.

- [ ] **Step 5: Commit final documentation or cleanup changes and push**

```bash
git add -A
git commit -m "docs: update native approval continuation architecture"
git push origin fix/1796-native-approval-continuation
```

Do not merge the PR.
