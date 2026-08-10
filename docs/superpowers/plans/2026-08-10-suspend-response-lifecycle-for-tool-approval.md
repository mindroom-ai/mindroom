# Suspended Tool Approval Continuations Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Suspend an agent or team response at a Matrix tool-approval boundary, release all live response lifecycle state, and resume the exact persisted Agno tool call once through the normal conversation serializer.

**Architecture:** Use Agno's persisted paused-run representation as the execution continuation and extend the event journal's approval storage with a first-writer-wins decision plus compare-and-set execution claim. A focused continuation coordinator owns card creation, expiry, startup recovery, entity rematerialization, and serialized resume, while response drivers and the response runner only expose and settle a typed suspended outcome.

**Tech Stack:** Python 3.13, asyncio, Agno paused runs and `acontinue_run`, SQLite/PostgreSQL event-journal backends, Matrix approval events, pytest, Ruff, Pyright.

## Global Constraints

The original response event remains the sole visible response owned by the source turn.
Pending approval must retain no live Agno agent, team, Matrix client, response task, waiter future, lifecycle lock, typing indicator, or streaming generator.
Approval policy scripts remain argument-sensitive and execute before a Matrix card is emitted.
Tool before-call and after-call hooks continue to wrap actual tool execution.
Approved continuation execution is at-most-once across duplicate decisions, worker wake-ups, reload, and uncertain crash recovery.
Denied and expired continuations never invoke the tool body.
The smallest correct change is preferred, and `bot.py` and `orchestrator.py` remain lifecycle wiring shells.
Markdown uses one sentence per line.

---

## File Structure

- Create `src/mindroom/approval_continuation.py` for typed continuation records, state transitions, and the process-local worker.
- Modify `src/mindroom/event_journal/approvals.py`, `schema.py`, `backend.py`, `sqlite_backend.py`, `postgres_backend.py`, `views.py`, and `store.py` for durable continuation persistence and atomic transitions.
- Modify `src/mindroom/approval_manager.py` and `tool_approval.py` to split card creation from decision waiting and to notify continuation readiness.
- Modify `src/mindroom/agents.py` and `tool_system/tool_hooks.py` to route potentially gated calls through Agno pause rather than inline waiting.
- Modify `src/mindroom/response_turn.py`, `ai.py`, `teams.py`, `history/turn_recorder.py`, `final_delivery.py`, and `response_runner.py` to carry and settle suspended outcomes.
- Modify `src/mindroom/turn_store.py`, `text_ingress_dispatch.py`, and `journal_dispatch.py` for the durable source-to-continuation handoff.
- Modify `src/mindroom/bot.py` and approval runtime initialization only for dependency wiring and startup or shutdown calls.
- Add focused tests in `tests/test_approval_continuation_store.py` and `tests/test_approval_continuation.py`, then extend existing approval, response-runner, tool-hook, and journal-ingress suites.

### Task 1: Durable Continuation State Machine

**Files:**
- Create: `src/mindroom/approval_continuation.py`
- Modify: `src/mindroom/event_journal/schema.py`
- Modify: `src/mindroom/event_journal/approvals.py`
- Modify: `src/mindroom/event_journal/views.py`
- Modify: `src/mindroom/event_journal/store.py`
- Modify: `src/mindroom/event_journal/backend.py`
- Modify: `src/mindroom/event_journal/sqlite_backend.py`
- Modify: `src/mindroom/event_journal/postgres_backend.py`
- Test: `tests/test_approval_continuation_store.py`
- Test: `tests/test_event_journal_store.py`

**Interfaces:**
- Produces: `ApprovalContinuation`, `ApprovalContinuationDecision`, and `ApprovalContinuationState` dataclasses or enums.
- Produces: `ApprovalContinuationView.create_approval_continuation(record)`, `resolve_approval_continuation(approval_id, decision)`, `claim_approval_continuation(approval_id, claimant_id)`, `complete_approval_continuation(approval_id)`, `fail_approval_continuation(approval_id, reason)`, and `recoverable_approval_continuations(now)`.
- Guarantees: decisions are first-writer-wins, claims are compare-and-set, and a claimed uncertain continuation cannot return to executable readiness.

- [ ] **Step 1: Write failing state-machine tests**

```python
async def test_continuation_decision_and_claim_are_first_writer_wins(store):
    await store.create_approval_continuation(_continuation("approval-1"))
    assert await store.resolve_approval_continuation("approval-1", "approved") is True
    assert await store.resolve_approval_continuation("approval-1", "denied") is False
    assert await store.claim_approval_continuation("approval-1", "worker-a") is True
    assert await store.claim_approval_continuation("approval-1", "worker-b") is False
```

- [ ] **Step 2: Run the focused tests and verify the missing API failure**

Run: `uv run pytest tests/test_approval_continuation_store.py -q`
Expected: FAIL because the continuation types and store methods do not exist.

- [ ] **Step 3: Add schema and typed store methods**

```python
class ApprovalContinuationState(StrEnum):
    WAITING_FOR_DECISION = "waiting_for_decision"
    READY = "ready"
    CLAIMED = "claimed"
    COMPLETED = "completed"
    TERMINAL_FAILURE = "terminal_failure"

type ApprovalContinuationDecision = Literal["approved", "denied", "expired"]

@dataclass(frozen=True, slots=True)
class ApprovalContinuation:
    approval_id: str
    room_id: str
    thread_id: str | None
    response_event_id: str
    entity_kind: Literal["agent", "team"]
    entity_name: str
    session_id: str
    run_id: str
    tool_call_id: str
    tool_name: str
    arguments: dict[str, Any]
    requester_id: str
    execution_identity: ToolExecutionIdentity
    expires_at: datetime
    decision: ApprovalContinuationDecision | None = None
    state: ApprovalContinuationState = ApprovalContinuationState.WAITING_FOR_DECISION
```

Store exact arguments and execution identity as canonical JSON, use one row per approval ID, and perform decision and claim updates with state predicates in one database statement.

- [ ] **Step 4: Run SQLite and PostgreSQL rendering tests**

Run: `uv run pytest tests/test_approval_continuation_store.py tests/test_event_journal_store.py -q`
Expected: PASS for creation, duplicate decision, duplicate claim, completion, terminal failure, expiry selection, and backend SQL rendering.

- [ ] **Step 5: Commit the state machine**

```bash
git add src/mindroom/approval_continuation.py src/mindroom/event_journal tests/test_approval_continuation_store.py tests/test_event_journal_store.py
git commit -m "feat: persist tool approval continuations"
```

### Task 2: Detached Approval Card Creation and Resolution Notification

**Files:**
- Modify: `src/mindroom/approval_manager.py`
- Modify: `src/mindroom/tool_approval.py`
- Modify: `src/mindroom/approval_inbound.py`
- Test: `tests/test_tool_approval.py`

**Interfaces:**
- Consumes: durable continuation methods from Task 1.
- Produces: `create_tool_approval_for_call(call, continuation) -> PendingApproval | ApprovalDecision` without awaiting a decision.
- Produces: `_ApprovalManager.create_approval(...) -> PendingApproval | ApprovalDecision` and a `decision_ready: Callable[[str], None]` callback invoked after a committed valid decision.
- Preserves: existing authorization, card redaction, sidecar, transaction deduplication, and terminal edit behavior.

- [ ] **Step 1: Write failing detached-card tests**

```python
pending = await manager.create_approval(
    tool_name="write_file",
    arguments={"path": "notes.txt"},
    room_id="!room:localhost",
    requester_id="@alice:localhost",
    approver_user_id="@alice:localhost",
    timeout_seconds=60,
)
assert pending.approval_id
assert manager.has_live_work() is False
```

Add a second test proving duplicate approval actions invoke `decision_ready` once after the durable decision wins.

- [ ] **Step 2: Run tests and verify they fail on inline waiter behavior**

Run: `uv run pytest tests/test_tool_approval.py -k 'detached or decision_ready' -q`
Expected: FAIL because card creation currently awaits `_await_waiter` and owns a live future.

- [ ] **Step 3: Split send-and-bind from wait ownership**

Refactor the existing linear request method so detached creation durably claims and sends the card, binds its event identity, returns `PendingApproval`, and removes the in-memory waiter requirement.
Keep `request_approval` as a temporary compatibility wrapper for non-response callers by implementing its wait with the old behavior only where explicitly required.
Make response decisions resolve the durable continuation before editing the card, and invoke `decision_ready` only when the resolution update succeeds.

- [ ] **Step 4: Run the complete approval suite**

Run: `uv run pytest tests/test_tool_approval.py tests/test_bot_reactions_approvals.py -q`
Expected: PASS, including all existing card transport and authorization cases plus detached behavior.

- [ ] **Step 5: Commit detached card support**

```bash
git add src/mindroom/approval_manager.py src/mindroom/tool_approval.py src/mindroom/approval_inbound.py tests/test_tool_approval.py
git commit -m "refactor: detach approval cards from live waiters"
```

### Task 3: Route Approval Policy Through Agno Pause

**Files:**
- Modify: `src/mindroom/tool_approval.py`
- Modify: `src/mindroom/agents.py`
- Modify: `src/mindroom/tool_system/tool_hooks.py`
- Test: `tests/test_agents.py`
- Test: `tests/test_tool_hooks.py`
- Test: `tests/test_tool_approval.py`

**Interfaces:**
- Produces: `tool_may_require_approval(config, tool_name) -> bool`, conservative for script rules and exact for static rules.
- Produces: `_mark_toolkit_approval_functions(toolkit, config) -> Toolkit`, setting `Function.requires_confirmation = True` only for potentially gated Matrix-chat functions.
- Removes: inline `request_tool_approval_for_call` waiting from the ordinary hook bridge.
- Preserves: tool before-call, actual body, and tool after-call ordering on continuation.

- [ ] **Step 1: Write failing toolkit-marking tests**

```python
toolkit = Toolkit()
toolkit.register(write_file, name="write_file")
marked = _mark_toolkit_approval_functions(toolkit, config)
assert marked.functions["write_file"].requires_confirmation is True
```

Cover static auto-approve, static require-approval, script-backed rules, approval-exempt calls, and OpenAI-compatible pruning.

- [ ] **Step 2: Run the tests and verify marking is absent**

Run: `uv run pytest tests/test_agents.py tests/test_tool_hooks.py tests/test_tool_approval.py -k approval -q`
Expected: FAIL on the new `requires_confirmation` assertions.

- [ ] **Step 3: Implement conservative marking and remove inline wait**

Apply marking after final function pruning and before bridge installation in agent toolkit assembly.
Retain `evaluate_tool_approval` for exact post-pause evaluation.
Delete the normal `_maybe_block_for_tool_approval` bridge step and its approval timing wait so a confirmed Agno continuation reaches the body without another card.

- [ ] **Step 4: Run tool assembly and hook suites**

Run: `uv run pytest tests/test_agents.py tests/test_tool_hooks.py tests/test_tool_approval.py tests/test_google_drive_oauth_tool.py -q`
Expected: PASS with hook ordering unchanged around executed calls.

- [ ] **Step 5: Commit approval pause routing**

```bash
git add src/mindroom/tool_approval.py src/mindroom/agents.py src/mindroom/tool_system/tool_hooks.py tests/test_agents.py tests/test_tool_hooks.py tests/test_tool_approval.py
git commit -m "feat: pause approval-gated Agno tool calls"
```

### Task 4: Typed Suspended Response Outcome

**Files:**
- Modify: `src/mindroom/history/turn_recorder.py`
- Modify: `src/mindroom/response_turn.py`
- Modify: `src/mindroom/ai.py`
- Modify: `src/mindroom/teams.py`
- Modify: `src/mindroom/final_delivery.py`
- Test: `tests/test_response_turn.py`
- Test: `tests/test_ai_user_id.py`
- Test: `tests/test_team_collaboration.py`

**Interfaces:**
- Produces: `PausedToolCall` with exact `tool_call_id`, `tool_name`, and copied `arguments`.
- Produces: `SuspendedAttempt` carrying entity kind, session ID, run ID, partial text, tool traces, and paused tool calls.
- Produces: `TurnRecorder.outcome == "waiting_for_approval"` and `TurnRecorder.paused_tool_calls`.
- Produces: delivery status `waiting_for_approval` without marking the source turn terminal.

- [ ] **Step 1: Write failing agent and team pause propagation tests**

```python
assert recorder.outcome == "waiting_for_approval"
assert recorder.paused_tool_calls == [
    PausedToolCall("call-1", "write_file", {"path": "notes.txt"}),
]
```

Cover blocking and streaming Agno `RunStatus.paused` results and a paused team member propagated through a team run.

- [ ] **Step 2: Run focused tests and verify paused runs are still generic interruptions**

Run: `uv run pytest tests/test_response_turn.py tests/test_ai_user_id.py tests/test_team_collaboration.py -k paused -q`
Expected: FAIL because paused tool identity is discarded and the recorder says `interrupted`.

- [ ] **Step 3: Carry exact paused tools through response drivers**

```python
@dataclass(frozen=True, slots=True)
class PausedToolCall:
    tool_call_id: str
    tool_name: str
    arguments: dict[str, Any]
```

Build these values from Agno `ToolExecution` instances using strict non-empty identity validation, attach them to the suspended resolution, and record `waiting_for_approval` without persisting an interrupted replay.

- [ ] **Step 4: Run response-driver tests**

Run: `uv run pytest tests/test_response_turn.py tests/test_ai_user_id.py tests/test_team_collaboration.py -q`
Expected: PASS for blocking, streaming, agent, and team outcomes.

- [ ] **Step 5: Commit suspended response types**

```bash
git add src/mindroom/history/turn_recorder.py src/mindroom/response_turn.py src/mindroom/ai.py src/mindroom/teams.py src/mindroom/final_delivery.py tests/test_response_turn.py tests/test_ai_user_id.py tests/test_team_collaboration.py
git commit -m "feat: expose suspended approval response outcomes"
```

### Task 5: Suspend, Persist, and Release the Response Lifecycle

**Files:**
- Modify: `src/mindroom/response_runner.py`
- Modify: `src/mindroom/turn_store.py`
- Modify: `src/mindroom/text_ingress_dispatch.py`
- Modify: `src/mindroom/journal_dispatch.py`
- Test: `tests/test_approval_continuation.py`
- Test: `tests/test_response_runner_agent.py`
- Test: `tests/test_response_runner_team.py`
- Test: `tests/test_journal_ingress.py`

**Interfaces:**
- Consumes: detached approval creation from Task 2 and suspended recorder output from Task 4.
- Produces: `ResponseRequest.on_approval_suspended: Callable[[ApprovalContinuation], Awaitable[None]]` for atomic source handoff.
- Produces: response-runner suspension settlement that edits the original event with pending status and returns from `run_locked_response`.
- Guarantees: `has_active_response_for_target(target)` is false and typing is off after suspension.

- [ ] **Step 1: Write failing lifecycle-release tests**

```python
await approval_card_created.wait()
await response_task
assert runner.has_active_response_for_target(target) is False
assert typing_updates[-1] is False
```

Add journal coverage proving the suspended source is not re-dispatched and a later event in the room reaches its turn callback.

- [ ] **Step 2: Run tests and verify the current response remains active**

Run: `uv run pytest tests/test_approval_continuation.py tests/test_journal_ingress.py -k 'suspend or approval' -q`
Expected: FAIL because the current hook still retains the lifecycle or the paused outcome is terminalized.

- [ ] **Step 3: Implement suspension settlement and source handoff**

For each exact paused call, evaluate policy under the lock.
Immediately continue auto-approved script results without creating a continuation.
For required approval, persist the continuation before making the card actionable, edit the existing response with pending approval stream metadata, invoke the source handoff, and return without post-response terminal effects.
Reject unsupported multiple independently gated calls in one paused run with a visible fail-closed result unless Agno presents them as one approval batch with stable identities.

- [ ] **Step 4: Run lifecycle, runner, and journal tests**

Run: `uv run pytest tests/test_approval_continuation.py tests/test_response_runner_agent.py tests/test_response_runner_team.py tests/test_journal_ingress.py -q`
Expected: PASS for lock release, typing cleanup, later human dispatch, and original source non-replay.

- [ ] **Step 5: Commit lifecycle suspension**

```bash
git add src/mindroom/response_runner.py src/mindroom/turn_store.py src/mindroom/text_ingress_dispatch.py src/mindroom/journal_dispatch.py tests/test_approval_continuation.py tests/test_response_runner_agent.py tests/test_response_runner_team.py tests/test_journal_ingress.py
git commit -m "feat: release response lifecycle during approval"
```

### Task 6: Serialized Exact Continuation Execution

**Files:**
- Modify: `src/mindroom/approval_continuation.py`
- Modify: `src/mindroom/response_runner.py`
- Modify: `src/mindroom/ai.py`
- Modify: `src/mindroom/teams.py`
- Modify: `src/mindroom/delivery_gateway.py`
- Test: `tests/test_approval_continuation.py`
- Test: `tests/test_response_runner_agent.py`
- Test: `tests/test_response_runner_team.py`

**Interfaces:**
- Produces: `ApprovalContinuationCoordinator.wake(approval_id) -> None`, `start()`, and `stop()`.
- Produces: `ResponseRunner.resume_approval_continuation(record) -> None` using `run_locked_target_operation`.
- Produces: agent and team continuation adapters that load the stored paused run and call `acontinue_run` with one verified updated `ToolExecution`.

- [ ] **Step 1: Write failing continuation serialization and identity tests**

```python
await coordinator.wake("approval-1")
await coordinator.wake("approval-1")
assert executed_calls == [("call-1", {"path": "notes.txt"})]
assert lifecycle_order == ["later-turn", "approval-continuation"]
```

Add denial and expiry tests asserting zero body calls, and mismatch tests asserting visible terminal failure.

- [ ] **Step 2: Run tests and verify resume support is missing**

Run: `uv run pytest tests/test_approval_continuation.py -k 'resume or duplicate or denial or expiry' -q`
Expected: FAIL because no coordinator or continuation runner exists.

- [ ] **Step 3: Implement the coordinator and exact Agno continuation**

Claim before materialization, acquire the stored conversation target through `run_locked_target_operation`, rebuild the entity from current configuration under the stored execution identity, load the paused run by session and run ID, and compare the stored tool call against the persisted Agno `ToolExecution` before applying the decision.
Use `confirmed=True` only for the matching approved call, `confirmed=False` for denied or expired calls, and leave every unrelated paused tool unresolved.
Edit the original Matrix event through `DeliveryGateway.deliver_final`, apply ordinary terminal post-response effects, and mark the continuation completed only after settlement.

- [ ] **Step 4: Run continuation and response-runner tests**

Run: `uv run pytest tests/test_approval_continuation.py tests/test_response_runner_agent.py tests/test_response_runner_team.py -q`
Expected: PASS for exact identity, one claim, ordering, original-event editing, and missing-entity terminal settlement.

- [ ] **Step 5: Commit continuation execution**

```bash
git add src/mindroom/approval_continuation.py src/mindroom/response_runner.py src/mindroom/ai.py src/mindroom/teams.py src/mindroom/delivery_gateway.py tests/test_approval_continuation.py tests/test_response_runner_agent.py tests/test_response_runner_team.py
git commit -m "feat: resume approved tool calls serially"
```

### Task 7: Expiry, Reload, and Uncertain Claim Recovery

**Files:**
- Modify: `src/mindroom/approval_continuation.py`
- Modify: `src/mindroom/tool_approval.py`
- Modify: `src/mindroom/bot.py`
- Modify: `src/mindroom/orchestration/config_lifecycle.py`
- Test: `tests/test_approval_continuation.py`
- Test: `tests/test_tool_approval.py`
- Test: `tests/test_config_reload.py`

**Interfaces:**
- Consumes: recoverable continuation scan from Task 1 and worker from Task 6.
- Produces: startup scan that schedules unexpired waits, resolves expired waits, enqueues ready unclaimed work, and settles abandoned claims without tool execution.
- Changes: approval shutdown no longer expires unresolved continuation-backed cards merely because the runtime reloads.

- [ ] **Step 1: Write failing reload and recovery tests**

```python
await first_runtime.stop()
second_runtime = await start_runtime(storage_path)
assert await second_runtime.continuations.get("approval-1").state == "waiting_for_decision"
```

Add tests for decision committed before reload, expiry during downtime, missing entity, persisted completed Agno result, and uncertain claimed execution.

- [ ] **Step 2: Run tests and verify startup currently expires cards**

Run: `uv run pytest tests/test_approval_continuation.py tests/test_tool_approval.py tests/test_config_reload.py -k 'reload or startup or expiry or uncertain' -q`
Expected: FAIL because startup discard treats recovered cards as terminal cleanup only.

- [ ] **Step 3: Implement startup and shutdown ownership**

Start one continuation coordinator after the approval transport and response runner are ready.
Stop it before journal and Matrix client teardown without mutating waiting durable rows.
On startup, reconcile card identity, schedule expiry from persisted timestamps, wake ready rows, reuse proven completed Agno results, and visibly fail abandoned unproven claims without resetting them to ready.

- [ ] **Step 4: Run reload and approval suites**

Run: `uv run pytest tests/test_approval_continuation.py tests/test_tool_approval.py tests/test_config_reload.py -q`
Expected: PASS for pending preservation and at-most-once recovery.

- [ ] **Step 5: Commit recovery behavior**

```bash
git add src/mindroom/approval_continuation.py src/mindroom/tool_approval.py src/mindroom/bot.py src/mindroom/orchestration/config_lifecycle.py tests/test_approval_continuation.py tests/test_tool_approval.py tests/test_config_reload.py
git commit -m "feat: recover suspended approvals across reload"
```

### Task 8: Acceptance Verification and Documentation

**Files:**
- Modify: `docs/architecture/bot-runtime.md`
- Modify: `docs/configuration/index.md`
- Modify: relevant files from Tasks 1 through 7 only when verification exposes an in-scope defect.

**Interfaces:**
- Produces: documented suspended approval lifecycle and evidence for every issue acceptance criterion.

- [ ] **Step 1: Add concise architecture documentation**

Document the state sequence `response -> paused run -> durable continuation -> lock release -> serialized decision continuation -> original response edit` with one sentence per line.

- [ ] **Step 2: Run focused acceptance suites**

Run: `uv run pytest tests/test_approval_continuation_store.py tests/test_approval_continuation.py tests/test_tool_approval.py tests/test_tool_hooks.py tests/test_response_turn.py tests/test_response_runner_agent.py tests/test_response_runner_team.py tests/test_journal_ingress.py -q`
Expected: PASS.

- [ ] **Step 3: Run static checks on changed Python files**

Run: `uv run ruff check src/mindroom tests`
Expected: PASS.

Run: `uv run pyright`
Expected: PASS.

- [ ] **Step 4: Run the full test suite**

Run: `uv run pytest -q`
Expected: PASS.

- [ ] **Step 5: Run pre-commit after a full dependency sync**

Run: `uv sync --all-extras && uv run pre-commit run --all-files`
Expected: PASS.

- [ ] **Step 6: Commit final documentation or verification fixes**

```bash
git add docs/architecture/bot-runtime.md docs/configuration/index.md src/mindroom tests
git commit -m "docs: describe suspended tool approvals"
```

- [ ] **Step 7: Inspect the final diff and issue mapping**

Run: `git diff --check origin/main...HEAD && git status --short && git log --oneline origin/main..HEAD`
Expected: no whitespace errors, only the unrelated pre-existing `.claude/TASK-1786391648-885a.md` remains untracked, and commits map cleanly to the design tasks.
