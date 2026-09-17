# Background tool jobs: living implementation plan

> Agentic workers: use the test-driven development and subagent-driven development skills for the implementation tasks below.
> This document is the shared scope and progress record for PR #2113.
> Update checkboxes, decisions, and verification evidence as work lands; an unchecked item is not implemented or verified.

**Current status (2026-09-17):** Implemented a restart-only, instance-wide `background_tool_jobs` option, disabled by default.
The earlier proposal to remove automatic joining is withdrawn because staggered completions could create excessive replies.
Keep the existing waiting and grouping behavior while making the feature opt-in.
PR #2113 remains open and unmerged.

**Goal:** Let tools keep working while the conversation moves forward, with predictable waiting and quiet result handling.

**Architecture:** Each accepted tool call has one execution owner for its entire lifetime.
The caller's wait is independent of that execution, and the existing serialized conversation runner owns result consumption and visible responses.
Stored job outcomes provide recovery and discovery without replaying interrupted side effects.

**Tech stack:** Python 3.13, asyncio, Agno, the existing MindRoom response lifecycle and durable stores, Matrix, pytest, and repository pre-commit hooks.

**Spec:** The agreed behavior and invariants in this document are the specification for the tasks below.

## Agreed behavior

- Generic background tool jobs require `background_tool_jobs: true`; the default is false, and changing it requires a restart.
- When disabled, ordinary tools use their existing execution path without the generic job schema, management tool, SDK adapters, or background execution resource ownership.
- Saved work from an earlier enabled process stays parked while disabled; disabling must never replay accepted side effects or erase saved outcomes.
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
- [x] Task 5: run complete checks, review the final diff, update the PR, and address valid review findings.

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
| Final PR review | Passed on implementation commit `844293f70` | Greptile reported no outstanding findings; all inline threads resolved; CodeRabbit confirmed result-limit/cancellation fixes and withdrew obsolete findings |

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
- [x] Update PR #2113 with the actual UX, measured diff, validation results, and any unresolved limitation.
- [x] Confirm the pushed branch matches the reviewed local head and the worktree has no unintended changes.

## Publication

Implementation commit `844293f70` is pushed to PR #2113, which remains open and unmerged.
The complete production diff against integrated main is 49 files, +4,388/−92 lines, or net +4,296.
That is 653 net production lines beyond the original feature.
The simpler waiter/execution contract removes automatic pause machinery, while durable completion, recovery, compatibility, and verified fixes retain a substantial implementation.

The final external review covered the implementation commit and found no outstanding issue.
Other review services were limited by temporary review capacity, subscription status, or diff size; these are not additional approvals.
Hosted CI was still running at publication, with no failures reported at that checkpoint.
The completed local full suite, hooks, task reviews, whole-branch review, and live evidence are recorded above.

## Architecture reassessment: 2026-09-17

**Status:** Audited implementation `7dd9a9359`; the following proposal was withdrawn after the reply-volume discussion.
It remains here as an audit record, not authorized implementation work.
The core question is whether we can remove competing completion paths while retaining the agreed tool-execution contract.
The existing verification record applies to the published implementation, not to this proposed reduction.

### Measured complexity

The production diff against integrated main `3bfed6570` is +4,388/−92 lines across 49 files, or net +4,296.
The following groups assign each changed production file once by its main responsibility; shared modules contain some work from other groups.
Counts include comments and blank lines and do not measure complexity by themselves.

| Code group | Files | Net added lines |
| --- | ---: | ---: |
| Runtime and job controls | 5 | 1,145 |
| SDK and resource ownership | 7 | 959 |
| Completion and lifecycle wiring | 20 | 948 |
| Native delegation integration | 7 | 473 |
| Result storage and consumption | 2 | 433 |
| Authorization and tool provenance | 8 | 338 |
| Total | 49 | 4,296 |

Removing automatic pause/resume simplified execution states but did not remove most of these responsibilities.
The previous revision added 653 net production lines beyond the original feature.
We should not describe it as a substantially smaller implementation.

### What overlaps, and what should stay separate

1. Foreground calls and explicit `job(wait)` acquire an exclusive result claim and return through the native tool path.
   This path is necessary for blocking calls, rich results, approvals, and explicit waiting.
2. `tool_jobs/completion.py::join_conversation_jobs` waits for the same outcomes after the model finishes, briefly acquires and releases native result claims, then prompts the model to acquire them again through `job(wait)`.
   Its separate `join_approval_jobs` loop repeats the continuation policy for reconstructed approval runs.
   `response_turn.py` implements blocking and streaming join integration; `approval_execution.py` and `teams.py` add native approval retrieval paths.
3. `orchestration/tool_job_runtime.py::deliver_pending` and `response_runner.py::_resume_tool_job_completion` already deliver unconsumed outcomes through a serialized internal journal source when the conversation becomes idle.
   Foreground retrieval, automatic joining, and idle completion therefore need coordination around one saved outcome.

The removable overlap is between automatic joining and idle completion, not between execution and waiting.
Moving all of these branches into `ResponseRunner` would change their location without proving that their states or dependencies disappear.

Two apparent duplications should remain for this reduction.
`ConsumptionOwner` confirms ordinary Agno tool-result persistence, while native delegation explicitly persists its external requirement and approval state before acknowledging a result.
Their persistence owners differ, so replacing both with a generic callback interface would add a new abstraction without a demonstrated saving.
The job store, Agno result receipt, and event journal respectively own execution outcome, model consumption, and response scheduling; they are not interchangeable copies of the same record.

### Alternatives

| Option | Behavior | Expected reduction | Assessment |
| --- | --- | --- | --- |
| Preserve automatic joining; simplify readiness observation | Same reply remains open automatically; all existing completion paths remain | Small; no substantial line reduction established | Safe fallback, but does not deliver the requested architectural reduction |
| Remove automatic joining; retain one detached-completion path | A detached call may finish in a later assistant reply; explicit `job(wait)` still supports same-turn waiting | About 250–350 net production lines | Recommended if the reply-timing change is accepted |
| Remove durable consumption and recovery, or reduce supported tool types | Lost or repeated reporting after crashes, or fewer compatible tools | Not estimated as an eligible change | Conflicts with retained requirements; do not implement implicitly |

The recommended option retains blocking by default, zero/positive wait budgets, interruption by human follow-up, list/inspect/wait/cancel, rich results, native approvals, current authorization, and stored outcome discovery after restart.
It removes the promise that the runtime keeps a finished model response open until outstanding jobs finish.
It also removes the runtime-generated waiting paragraph and the extra model continuations used solely for automatic joining.
The agent can still explicitly wait when it needs a result before answering.
A completion that arrives during streaming waits for the existing serialized owner to become available, then generates a useful later reply.
This can mean an additional visible assistant reply, even though synthetic completion notices remain absent.
Ready outcomes for the same authorized conversation should be collected together at the locked completion boundary; outcomes that become ready later may require later replies.

### Recommended reduction, pending the reply-timing decision

Keep this flow:

```text
application tool -> one accepted job -> foreground wait
    -> ready: ordinary native result and persisted receipt
    -> timeout or human follow-up: return handle; execution continues

ready detached outcome -> existing internal journal source
    -> serialized conversation owner -> native job result retrieval
    -> persisted receipt -> later scheduled copies become no-ops
```

The completion scheduler must only schedule work; it must not execute tools, acknowledge unread results, or start a competing response.
Collect authorized ready generations under the existing lifecycle lock, preserving each job's identity and receipt.
Continue using the existing native `job(wait)` path for rich results and approval projection; this proposal does not invent direct SDK message injection.
If the model declines retrieval, the outcome remains discoverable; do not add an unbounded retry loop.
Keep original-source recovery and its coordination with idle completion so the same accepted work does not acquire two response owners after a restart.
Keep cancellation settlement, synchronous-thread draining, and task-affine connection ownership.

| Removal area | Concrete code | Estimated gross deletion |
| --- | --- | ---: |
| Automatic join helpers and wait-notice plumbing | `tool_jobs/completion.py`: `join_conversation_jobs`, `_wait_for_job`, `_wait_for_ready_jobs`, `join_approval_jobs`, `_ReadyJobContinuation`, `background_wait_notice`, `report_background_wait` | 110–125 lines |
| Response-turn join branches | `response_turn.py`: attempted-outcome set, joined blocking settlement, streaming continuation branch | 60–75 lines |
| Reconstructed approval retrieval | `approval_execution.py::retrieve_results`, `teams.py::_retrieve_team_job_results`, and their join calls | 85–100 lines |
| Wait-only presentation plumbing | `response_runner.py` callbacks, `BackgroundWaitChunk`, and its streaming/API collectors | 45–65 lines |
| Total gross deletion | Before replacement queue batching and revised prompt policy | 300–365 lines |

Allow roughly 20–50 new production lines for collecting ready results at the existing completion boundary and expressing the revised waiting policy.
Use 250–350 net removed lines as a planning range, not a measured patch or acceptance target.
That leaves approximately 3,950–4,050 net production lines against the same main baseline.
A claim that this becomes a few-hundred-line feature is unsupported while generic resource lifetimes, native approvals, rich results, and recovery remain requirements.
The principal benefit is removing one completion mechanism and its approval/streaming branches, not shrinking the whole feature dramatically.

### Next work and acceptance criteria

The user raised the case of one hundred jobs finishing at different times.
Keep automatic waiting and existing result grouping; the following reduction checklist is superseded and must not be executed as part of the configuration gate.

- [ ] Record whether a detached result may arrive in a later assistant reply.
- [ ] Implement one completion-path change with tests covering ordinary Agent/Team calls and resumed approvals; remove obsolete join-only tests and keep all retained guarantees covered.
- [ ] Exercise simultaneous ready results, completion during a long stream, a newer human turn, and a result consumed before its queued completion acquires the lock.
- [ ] Verify explicit waiting still returns rich results and approval requirements, and that manual cancellation stays quiet after its result is consumed.
- [ ] Repeat strict restart checks for one execution, original-source recovery, retained outcomes, and no redundant completion response.
- [ ] Run focused regressions, the full non-Matrix suite, repository hooks, and the existing live Matrix scenarios adapted to the selected reply behavior.
- [ ] Report measured production additions and deletions separately from tests and generated documentation before claiming simplification.

A cross-model consultation was attempted once for the architectural alternatives, but the external model's OAuth session had expired and no advice was returned.
The recommendation is based on the repository inspection and existing regression/live evidence, not on an independent consultation approval.

## Task 6: opt-in configuration and safe disabled startup

**Status:** Implemented and reviewed; final branch verification is recorded under Task 7.

**Requirements and global constraints:**

- Add one root boolean, `background_tool_jobs: bool = False`, to `Config`.
- Pin its effective value at instance startup; config reload may apply unrelated changes but must report restart required for a changed value.
- Reuse existing restart-status reporting in the config lifecycle, API, and config command; do not build a generic flag framework.
- A fresh disabled instance must not create the tool-job runtime, install its SDK adapters, enter execution-resource/consumption owners, advertise `wait_timeout`, install `job`, or start completion scheduling.
- Ordinary Agent and Team tools, delegation, streaming, native approvals, and existing shell-specific background behavior must keep working while disabled.
- Prompts must describe the capabilities actually available in that process.
- Enabled behavior, including automatic joining and interruption behavior, remains unchanged.
- Disabling after restart parks existing feature-owned sources and internal completion events before they can resume tools or original ingress.
- Preserve outcomes and pending ownership for recovery on a later enabled restart; do not delete records, acknowledge unread results, or run cleanup that replays tools.
- Cover native approval continuations for generic job calls and calls carrying reserved wait metadata, including pending approvals that have no job snapshot yet.
- Unrelated ordinary approvals and new human messages remain usable while disabled.
- Prefer a small startup-only index of saved source ownership, using existing validated snapshot parsing, and a guard at the existing journal dispatch boundary before approval handoff.
- Avoid per-event scans, another persistence format, and another scheduler.
- Use existing current-authorization checks when re-enabled; do not expose stored results through the disabled guard.
- Keep changes to `bot.py` and `orchestrator.py` limited to lifecycle wiring.
- No merge, force push, amendment, new PR, or unrelated architecture reduction.

**Implementation surfaces:**

- `src/mindroom/config/main.py`: authored option.
- `src/mindroom/orchestration/tool_job_runtime.py` and a small leaf helper under `tool_jobs/` if needed: effective startup setting and passive recovery ownership.
- `src/mindroom/orchestration/config_lifecycle.py`, `src/mindroom/api/config_lifecycle.py`, and `src/mindroom/commands/config_commands.py`: restart-required reporting.
- `src/mindroom/agents.py`, `src/mindroom/teams.py`, and `src/mindroom/tool_jobs/execution_scope.py`: bypass adapter installation and resource ownership when disabled.
- `src/mindroom/journal_dispatch.py` and its existing bot callback wiring: park saved feature-owned work before approval or ingress dispatch, with stable deferral rather than retry errors.
- Existing approval owner code only where needed to identify and fence pending feature calls without a saved job.
- `src/mindroom/custom_tools/delegate.py` and `src/mindroom/prompts.py`: conditional generic-job instructions.
- Extend existing config, orchestrator, job, journal, and approval tests; use a focused flag test file if clearer.

**Verification sequence:**

1. Write and run focused failing tests against the current implementation before production edits.
2. Verify default-off startup and actual ordinary tool/delegation execution, both blocking and streaming, with no generic-job schema or resource owners.
3. Verify explicit opt-in preserves the existing tool-job behavior and schemas.
4. Verify reload in either direction leaves the effective mode unchanged and reports restart required; a restarted instance applies the change.
5. Recover saved ordinary, delegated, completion, and approval work with the flag disabled; assert no original side effect executes and no retry-error loop is started.
6. Re-enable against the same saved state and verify outcome discovery/recovery without replay.
7. Verify unrelated ordinary approval execution remains functional while disabled.
8. Run focused affected regressions and import-boundary tests; report any fixtures that needed explicit opt-in.

- [x] Focused red/green tests and minimal implementation.
- [x] Task review and verified fixes.

The first review found that removing an agent could let cleanup discard a saved pre-execution approval with reserved wait metadata.
The passive reader now checks the finite supported canonical session locations when current configuration cannot locate the exact saved run, including shared, private per-user, and private per-user-agent storage.
It preserves the exact requester, session, and run match, without scanning directories or reconstructing executable capabilities.
The scoped re-review approved this fix and the corrected startup test fixtures.
The configuration gate and final review fixes change 26 production files by +448/−83 lines, or net +365, including the passive recovery safeguards.
That exceeds the initial 100–200-line estimate because saved approvals and coalesced sources require protection even when no job runtime is created.
The complete production feature against integrated main `3bfed6570` is now 58 files, +4,787/−126 lines, or net +4,661.

## Task 7: live verification, documentation, and PR update

**Status:** Implementation, live checks, full verification, and independent branch review are complete.
The existing PR carries publication and hosted-review status.

- Update operator and tool documentation with the root option, default, restart requirement, and parked old-work behavior.
- Run isolated live Matrix/backend scenarios with real tool side effects for off, on, reload, and enabled-to-disabled-to-enabled restart transitions.
- Keep existing interruption, newer-turn, streaming, cancellation, delegation, and native approval coverage explicitly enabled.
- Verify fresh default-off and ordinary native approval behavior, not merely model schemas.
- Run the complete non-Matrix test suite, repository hooks, and Tach after focused checks pass.
- Obtain independent review of the final flag patch and its integration with the existing branch.
- Record measured additions/deletions separately for production, tests, and documentation.
- Update the existing PR, push ordinary commits, address valid review findings, and leave the PR unmerged.

- [x] Live cases and evidence.
- [x] Full checks and independent review.
- [x] Documentation, measured diff, and PR update prepared.

### Configuration gate verification

| Check | Result |
| --- | --- |
| Focused flag and startup regressions after review fixes | 316 passed, 5 skipped; all five initial full-suite fixture failures corrected |
| Full non-Matrix suite | 21,498 passed, 12 skipped, 27 warnings in 308.20 seconds |
| Final repository checks | All-files pre-commit passed after regenerating documentation references; Tach dependencies/interfaces passed |
| Independent review | Whole-branch review and scoped publication-fix review approved with no remaining findings |
| Default-off live calls | Actual synchronous, asynchronous, delegated, streaming, blocking, and approved tools execute without generic schemas, resource owners, or SDK resource bindings |
| Live config reload | Both directions preserve the running mode and expose the existing restart-required API status |
| Live disabled restart | Saved ordinary/delegated jobs and a pre-execution approval stay parked, including while a new message in the same thread executes |
| Live enabled restart | Preserved jobs recover without replay; a pending approved call executes exactly once after re-enabling |
| Removed-agent approval | Feature approval remains parked while disabled; ordinary unavailable-owner cleanup still runs; after restoration native membership revocation expires the stale card and a fresh approval executes once |
| Enabled lifecycle regression | Timeout budgets, repeated human follow-ups, child continuity, long streaming, newer turns, cancellation, direct/delegated approvals, STOP, and restart remain covered |
| Live invalid waits | Ten streaming/blocking scenarios return tool errors, accept corrected calls, and execute each tool once without extra jobs |
| Live shared-session approval | Two real requesters share a canonical conversation; the exact approval remains parked while disabled and executes once after re-enabling |
| Publication fix regressions | 363 passed, 2 skipped for execution boundaries; 224 passed for session/config boundaries |
| Combined live evidence | 48 passing scenarios, 545 Matrix events, zero synthetic completion notices |

Live checks use a real local Matrix server, backend, and application tools with a deterministic local model endpoint.
The full suite retains pre-existing dependency deprecations and mock-coroutine warnings; it is not warning-free.
Private local test evidence and exact run identifiers remain in persistent worktree storage.

Publication review identified four additional issues: invalid waits aborted model runs, scan failures stopped completion retries, one restart warning hid another, and shared-session row ownership could hide an exact saved approval run.
The fixes use existing SDK tool failures, worker retries, canonical session lookup, and combined config feedback.
They remove one net production line across seven files and passed scoped independent review.
