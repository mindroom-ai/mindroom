# Background tool jobs: living implementation plan

> Agentic workers: use the test-driven development and subagent-driven development skills for the implementation tasks below.
> This document is the shared scope and progress record for PR #2113.
> Update checkboxes, decisions, and verification evidence as work lands; an unchecked item is not implemented or verified.

**Goal:** Let tools keep working while the conversation moves forward, with predictable waiting and quiet result handling.

**Architecture:** Each accepted tool call has one execution owner for its entire lifetime.
The caller's wait is independent of that execution, and the existing serialized conversation runner owns result consumption and visible responses.
Stored job outcomes provide recovery and discovery without replaying interrupted side effects.

**Tech stack:** Python 3.13, asyncio, Agno, the existing MindRoom response lifecycle and durable stores, Matrix, pytest, and repository pre-commit hooks.

**Spec:** The agreed behavior and invariants in this document are the specification for the tasks below.

## Agreed behavior

- Every managed application tool call exposes an optional `wait_timeout`, expressed in seconds.
- Omitted or null waits until completion or a human follow-up.
- Zero returns a job handle immediately; a positive finite value bounds waiting without cancelling the execution.
- A human follow-up releases the foreground wait so the assistant can respond while the original work continues.
- Human messages never automatically pause background jobs, including subagents.
- Explicit cancellation requests that work stop; the runtime reports completion of cancellation only after execution and cleanup settle.
- Native approval-required actions still wait for approval through the existing approval machinery.
- A single `job` management tool provides list, inspect, wait, and cancel; generic resume is removed.
- Once the agent finishes independent work, it waits for outstanding jobs automatically, visibly, and interruptibly.
- Results completing during text streaming are queued until a safe response boundary and never start a concurrent response in that conversation.
- An idle conversation can be continued for an unconsumed result without posting a synthetic completion message to Matrix.
- Already-consumed success, failure, and acknowledged cancellation cause no redundant completion response.
- Runtime completion input is distinguished from a new human request while retaining the original requester's authorization scope.
- Rich results, artifacts, approval continuations, and requester isolation remain supported.
- A durable result envelope is limited to 64 MiB of default JSON UTF-8 encoding, including base64 expansion and cumulative container/scalar overhead; oversized output becomes a failed job with a size-limit error.
- Active jobs remain discoverable after a new turn or compaction loses a handle.
- A restart preserves stored outcomes and marks abandoned local execution interrupted; it never reruns the tool automatically.

## Scope boundaries

- Nested tools remain owned by the accepted outer job.
  The shared timeout is not advertised for nested application calls that cannot independently detach; an explicit non-null value there is rejected.
- Keep reusable subagent sessions and follow-ups after the preceding child turn finishes.
- Sending instructions into an already-running subagent is a separate future capability, not part of this revision.
- Keep Agno result and resource-lifetime compatibility where required by existing tools.
- Do not replace rich results with a text-only contract, weaken approval or access checks, or promise that arbitrary Python threads and remote side effects are forcibly stoppable.
- Do not introduce a fixed global timeout or require the model to poll every job repeatedly.
- Keep feature logic in focused owners; bot and orchestrator changes are limited to wiring.
- Keep development artifacts and live evidence in persistent worktree storage; do not commit credentials or machine-specific paths.
- Use ordinary follow-up commits on the existing PR branch; do not merge the PR as part of implementation.

## Progress

- [x] Restore the original feature unchanged on current main and open replacement PR #2113.
- [x] Verify the restored baseline: 21,241 tests passed, 12 skipped; pre-commit and Tach passed.
- [x] Agree that human follow-ups release waits without automatically pausing execution.
- [x] Record the design and scope in this living document.
- [x] Task 1: simplify execution control and implement interruptible unlimited waits.
- [x] Task 2: expose the shared timeout at the SDK and delegation boundaries and remove generic resume.
- [x] Task 3: replace Matrix completion notices with serialized internal completion handling and automatic joining.
- [x] Task 4: verify real tool behavior, fix confirmed compatibility/lifecycle defects, and update documentation.
- [ ] Task 5: run complete checks, review the final diff, update the PR, and address valid review findings.

## Task 1: execution lifetime and waiter lifetime

**Files:** `src/mindroom/tool_jobs/runtime.py`, `src/mindroom/tool_jobs/control.py`, and runtime/control tests in `tests/test_tool_jobs.py` and `tests/test_background_subagents.py`.

**Interface:** `ToolJobRuntime.wait(job_id, *, owner, depth, timeout: float | None = None, reserved_token=None)` retains its existing result-claim return contract.
Keep `job_checkpoint()` as a cancellation checkpoint, without human-pause state or cross-loop asyncio waiters.
Human signals wake foreground waiters only; pending input cannot be lost between checking and subscribing.

- [x] Add a failing test in which a human signal releases a waiter while the operation crosses a subsequent checkpoint and completes without resume.
- [x] Add failing coverage for unlimited waiting, zero/positive budgets, invalid durations, repeated human messages, cancelled waiters, and exactly one execution.
- [x] Remove human-pause fields, pause watchers, and resume transitions; retain explicit cancellation ownership and native approval state.
- [x] Run the focused runtime/control tests and review the changes before the next dependent task.

Core regression shape using the existing test owner helper:

```python
started, proceed, finished = asyncio.Event(), asyncio.Event(), asyncio.Event()

async def operation():
    started.set()
    await proceed.wait()
    await job_checkpoint()
    finished.set()
    return BackgroundOutcome("completed", "done")

job = await runtime.start(JobSpec("followup", "tool", 0), owner=_owner(), operation=operation, human_signal=signal)
waiter = asyncio.create_task(runtime.wait(job.job_id, owner=_owner(), depth=0))
await started.wait()
signal.notify()
assert (await asyncio.wait_for(waiter, 1)).job.status == "running"
proceed.set()
await asyncio.wait_for(finished.wait(), 1)
```

## Task 2: shared timeout, job controls, and delegation

**Files:** `src/mindroom/tool_jobs/agno_compat_execution.py`, `src/mindroom/tool_jobs/agno_execution.py`, `src/mindroom/custom_tools/job.py`, the delegation toolkit/driver, agent construction where needed, and their focused tests.

**Interface:** `wait_timeout` is framework metadata, never an unexpected keyword passed to an application tool.
The same value must survive native approval admission and delegation execution.
`job(action="wait", job_id=..., wait_timeout=None)` uses the same waiter contract; list/inspect/cancel remain scoped to the original owner.

- [x] Add failing schema-and-execution coverage with real Agno Function objects, including a tool whose callable accepts no timeout keyword.
- [x] Cover omitted/null, zero, positive, invalid, batch, fallback, and native approval cases.
- [x] Expose and extract the reserved timeout at one shared SDK boundary and use it for native subagent waits.
- [x] Remove the generic resume action, pause instructions, and fixed ten-second waiting policy.
- [x] Preserve original arguments for application hooks/caches and approval exact-call matching; treat wait metadata consistently in durable receipts.
- [x] Verify tuple identity and rich streamed metadata/media preservation, fixing the existing codec/replay defects at their owners.
- [x] Run SDK, job-tool, delegation, and approval regression tests.

Public call examples:

```python
read_file(path="notes.md", wait_timeout=None)
run_subagent(task="Research options", agent_name="research", wait_timeout=0)
job(action="wait", job_id="stored-handle", wait_timeout=5)
```

## Task 3: quiet completion and automatic joining

**Files:** `src/mindroom/orchestration/tool_job_runtime.py`, `src/mindroom/tool_jobs/runtime.py`, completion/consumption owners, `src/mindroom/response_turn.py`, `src/mindroom/response_runner.py`, and focused response-lifecycle tests.

**Interface:** Job completion persists a pending outcome and wakes the conversation's serialized owner.
Foreground wait consumption, turn-end joining, and idle completion handling share durable result-consumption evidence.
The runtime waits without spending model calls, then supplies one internal continuation directing retrieval of ready outcomes through the existing native job tool and approval pipeline.
If the model does not retrieve an outcome, retain it for discovery without an unbounded continuation loop.
No completion path sends a Matrix notice back through ingress, claims a human follow-up, or starts a second concurrent model response.
Retain existing response-source and approval recovery guarantees when binding an internal completion to the response runner.

- [x] Write failing tests for consumed success/error, explicit cancellation, completion during streaming, a newer active turn, and idle completion.
- [x] Replace transport-specific delivery claims with internal pending completion ownership, using stable job/generation identities.
- [x] Consume ready results at response boundaries and wait for outstanding work after independent work finishes.
- [x] Release automatic waits promptly on human input and surface native approval requirements instead of waiting indefinitely on approval.
- [x] Persist consumption before suppressing future completion work; preserve pending results if a response is cancelled or cannot be saved.
- [x] Remove obsolete Matrix completion parsing, notifications, retry state, and tests that only assert the removed transport.
- [x] Run response, completion, approval, cancellation, and recovery regressions.

Required ordering for the streaming case:

```text
tool starts -> caller detaches -> assistant streams -> job stores result
-> current stream reaches its boundary -> conversation owner consumes result
-> assistant continues with the result -> no duplicate completion turn
```

## Task 4: real execution and retained guarantees

- [x] Attempt reproduction with actual read_file, ls, grep, write_file, and shell calls on the revised, restored-feature, and pre-feature trees.
  All three passed the same five-call Matrix scenario; the historical event-loop report remains unreproduced, so no causal fix is claimed.
- [x] Verify runtime-scoped execution-authorizer registration so independent runtimes cannot replace or remove each other's checks.
- [x] Verify cleanup failure cannot strand cancellation or prevent shutdown from settling other jobs.
- [x] Verify the delegation early-exception path settles while exact child ownership is retained.
- [x] Run isolated local Matrix/backend checks with real file, shell, and delegation execution; use controlled delays for deterministic concurrency.
- [x] Cover repeated interruptions, a newer turn, a long text stream, approval allow/deny, cancellation, restart, and large output.
- [x] Inspect visible Matrix messages and stored job/session outcomes for duplicate notices and duplicate execution.
- [x] Update user docs, architecture docs, Agno compatibility inventory/comments, tool descriptions, and this document to match the final behavior.

## Verification record

| Check | Status | Evidence |
| --- | --- | --- |
| Restored baseline non-Matrix suite | Passed before revision | 21,241 passed, 12 skipped, 28 warnings |
| Restored baseline pre-commit and Tach | Passed before revision | Recorded in PR #2113 |
| Revised runtime unit tests | Passed | 72 runtime/control tests after transport retirement; initial Task 1 independently reviewed |
| Revised SDK/delegation/approval tests | Passed | 313 passed, 2 pre-existing optional MCP skips; fresh task review approved with 79 passed and the same 2 skips |
| Revised completion and streaming tests | Passed; scoped review approved | Initial 260 owned tests, later 161 regressions and 10 native approval/ordinary-team cases after review fixes |
| Import graph contracts | Passed | 22 tests |
| Live encoded-result limit | Passed | A real 50 MiB native file artifact exceeded the encoded limit; one execution, a 1,550-byte failed receipt, a 65-character model error, no extra completion turn |
| Configured large-output policy | Passed | Full 330,021-byte result saved; 8,503-character model preview |
| Real Matrix coding and shell checks | Passed | Five actual calls on revised, restored-feature, and pre-feature trees; revised jobs completed and acknowledged; no completion notice or cross-loop error |
| Real Matrix concurrency/delegation/approval/restart checks | Passed | 18 controlled-model checks with actual Matrix/backend/tools; strict restart: one execution, one retrieval; no completion notices |
| Additional approval/blocking live regressions | Passed after fixes | Direct root approval waits visibly, natural completion uses the same response, human input releases waiting, and non-streaming STOP preserves interrupted replay while the tool continues |
| Fresh integrated live rerun | Passed | 16 approval, interruption, streaming, cancellation, and strict-restart checks; 316 Matrix events audited with zero synthetic completion notices |
| Revised full suite | Passed after final authorization fixes | 21,420 passed, 12 skipped, 41 warnings in 335 seconds |
| Revised pre-commit and Tach | Passed | All-files hooks and dependency/interface checks passed after formatting and generated-reference updates |
| Final whole-branch review | Approved after one fix wave | Null exclusions and empty implicit inclusion filters match real toolkit construction; native MCP conventions retained |
| Final PR review | Pending | Validate comments against the pushed code |

## Open findings and live fixes

- **Fixed: crash recovery duplicated execution and result reporting.** Accepted jobs now retain their exact original source.
  A pending original request retrieves its stored outcomes while the internal completion defers to that owner.
  The strict live restart test passed with one leaf execution, one interrupted-result retrieval, the original source settled, and a previously completed result still readable.
- **Fixed: independent text remained buffered.** An explicit background-wait stream event now flushes generated text before waiting.
  The repeated live run passed zero/positive budgets, repeated interruptions, child completion, long streaming, newer turns, and synchronous/asynchronous cancellation.

- **Fixed: non-streaming cancellation during automatic waiting.** The blocking driver now records completion after automatic joining has settled and preserves partial evidence while waiting.
  Real turn-recorder regressions cover cancellation during the wait and cancellation or failure during the retrieval attempt.
  The isolated recorder reproduction failed before the fix; the full Matrix STOP test still preserves interrupted replay through the outer lifecycle, so no end-to-end loss is claimed from that test.

- **Fixed: approved ordinary tools bypassed automatic joining.** A zero-timeout call resumed through native approval previously returned its independent text while its job was still running.
  Shared visible, interruptible joining now covers those continuation owners.
  All eight focused Agent/Team budget-and-interruption cases passed, and direct root-tool approval passed two live cases separately from delegated child approval.

- **Fixed: ordinary team results lacked persisted consumption evidence.** Explicit public session-state arguments now preserve the exact result receipt through both blocking and streaming team runs.
  Both real SDK/SQLite regressions passed after reproducing unconsumed outcomes.

- **Review fixes passed:** SDK installation locking, storage closure, primary delegation errors, result bounds, delegate metadata, and durable cancellation retries passed scoped review.
  Cancellation retries retain approval generation/receipt identity after failed admission and propagate persistence failures before returning terminal success.
  The first batch passed 151 tests with two optional skips; the follow-up runtime suite passed all 44 tests.

- **Final review fixes passed:** nullable exclusions and empty implicit toolkit inclusion lists now match the effective toolkit surface.
  Native MCP assignment/server conventions and local OAuth helpers retain their existing behavior.
  Sixteen authorization cases and 172 tool-job cases passed, with two optional skips; scoped re-review approved both fixes.

## Decision log

- 2026-09-16: Human input releases waits but never automatically pauses background execution.
- 2026-09-16: Keep native approvals, rich tool compatibility, durable result discovery, and explicit cancellation.
- 2026-09-16: Remove chat-based completion notices and serialize result handling through the conversation owner.
- 2026-09-16: Internal completions admit a nonprojected event-journal source so the existing response and approval owners retain durable recovery.
- 2026-09-16: Automatic joining waits in the runtime, then uses one internal ready-result continuation; it does not require a new private SDK tool-call injection API.
- 2026-09-16: Preserve one accepted outer job for nested execution; hide unsupported nested wait budgets instead of accepting and ignoring them.
- 2026-09-16: A still-pending original request owns recovery of its accepted jobs; an internal completion must not create a competing response for that source.
- 2026-09-16: Preserve existing retained job history and native identity; a separate cache deletion would not free child objects still captured by cancellation closures, so do not add a partial eviction mechanism.
- 2026-09-16: Bound each encoded result envelope at 64 MiB before artifact reads or base64 copies; retain the existing codec instead of introducing separate artifact storage.
- 2026-09-16: Match authorization to each toolkit's effective filter semantics, including native MCP assignment and server lists whose empty value means unrestricted.
- 2026-09-16: Active-subagent instruction delivery is outside this revision; completed-turn follow-ups stay supported.
- 2026-09-16: The prior 600–1,200-line incremental estimate predates this simplification; report measured final additions and deletions instead of treating the estimate as a target.

## Task 5: completion gate

- [x] Integrate current main, including the related approval-follow-up fix, before final validation.
- [x] Run `uv run pytest -m 'not requires_matrix' -n 10 --no-cov` after the focused tests pass.
- [x] Run `uv run pre-commit run --all-files` and `uv run tach check --dependencies --interfaces`.
- [x] Review the complete feature diff and the revision diff against the restored baseline separately.
- [x] Resolve verified review findings, preserve evidence for rejected claims, and recheck fixes.
- [ ] Update PR #2113 with the actual UX, measured diff, validation results, and any unresolved limitation.
- [ ] Confirm the pushed branch matches the reviewed local head and the worktree has no unintended changes.
