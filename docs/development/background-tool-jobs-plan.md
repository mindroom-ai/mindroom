# Background tool jobs: living implementation plan

> Agentic workers: use the test-driven development and subagent-driven development skills for the implementation tasks below.
> This document is the shared scope and progress record for PR #2113.
> Update checkboxes, decisions, and verification evidence as work lands; an unchecked item is not implemented or verified.

**Current status (2026-09-19):** Implemented restart-only, instance-wide `background_tool_jobs` settings, disabled by default, with toolkit exclusions defaulting to `[shell]`.
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

- Generic background tool jobs require `background_tool_jobs.enabled: true`; the default is false, and changing it requires a restart.
- `background_tool_jobs.exclude_toolkits` excludes complete registered toolkits, including plugin/custom toolkits; an explicit list replaces the `[shell]` default.
- Both settings are pinned at startup. Saved approvals and jobs retain their accepted owner across exclusion changes.
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

## Task 8: integration coverage for risky lifecycle boundaries

**Status:** Complete; six integration cases verified locally.
Add repeatable CI coverage for multi-step scenarios previously exercised mainly through isolated boundary tests or live checks.
Keep the agreed behavior and production architecture unchanged unless a new regression demonstrates a concrete bug.

**Files:** Focused integration tests under `tests/test_tool_job_turn_integration.py` and `tests/test_tool_job_restart_integration.py`, reusing existing typed fixtures where practical.
Use real Agno Agent or Team execution, job controls, durable job snapshots, and SQLite session storage.
Only the model provider and external transport need deterministic substitutes.
Use event barriers to control ordering, finite failure deadlines, and explicit task/resource cleanup.

- [x] Release a foreground wait through human input, let the original tool continue into a newer turn, rediscover it with `job(action="list")`, and retrieve its result without repeating the side effect.
- [x] Complete a detached tool during an active stream and verify that retrieval waits for the response boundary, persists the exact consumption receipt, and leaves no redundant completion work.
- [x] Interrupt a response after a tool result exists but before durable consumption is confirmed, then reconstruct the runtime and retrieve the retained result without replaying the tool.
- [x] Produce a real native approval pause, restart disabled with the saved run and journal continuation, verify it stays parked without executing, and re-enable to approve or deny the exact call once.
- [x] Exercise streaming and blocking entry points, success and tool failure where relevant, and a shared conversation whose saved session-row requester differs from the approval requester.
- [x] Prove representative assertions detect broken guarantees with temporary fault injection or isolated mutation checks; preserve the production tree after each check.
- [x] Run focused integration regressions, repository hooks, and independent review; record the results here for the existing PR.

Tests must assert real side effects, persisted outcomes, model-visible tool results, and pending-work state rather than mocked callback counts.
Do not add production test hooks, a new scheduling abstraction, or a reusable test framework for these scenarios.

The six cases cover a human-released job crossing into a newer turn, success and failure during active streaming, interrupted consumption followed by runtime reconstruction, and approved or denied native continuations across disabled and enabled startup.
The approval cases retain a shared session row owned by a different requester from the exact paused run.
Assertions check actual side effects, provider-visible tool results, exact durable receipts, and pending outcomes.
Both approval cases exercise `ResponseRunner` and `AgentApprovalExecution` through real final-delivery acknowledgment, consumption finalization, and terminal source and continuation settlement.

Final verification: `uv run pytest -m 'not requires_matrix' -n 10 --no-cov` passed 21,504 tests with 12 skips and 27 warnings in 340.77 seconds.
All repository pre-commit hooks passed, including type checks and module boundaries.
Independent review approved the tests after adding exact provider-message assertions, guaranteed response-task cleanup, and full approval lifecycle coverage.
Six subprocess-local mutations failed as intended when human release, response joining, receipt confirmation, parked-approval indexing, provider-visible result delivery, or terminal approval settlement was broken.

These tests use real SDK execution, durable job snapshots, SQLite sessions, and approval journals with deterministic model responses and local tool side effects.
They reconstruct runtimes in one process and do not exercise Matrix transport or simulate a process kill.
The earlier 48 live Matrix scenarios remain the transport evidence for the unchanged production code.
No new production defect was demonstrated, so this extension changes only tests and this plan.

## Architecture audit: preserve the agreed behavior

**Status:** Assessment completed on 2026-09-17; the three approved refactors are implemented and verified locally.
The original assessment below records the reasoning; implementation measurements and verification follow it.
The user confirmed that automatic result delivery across turns and durable recovery are essential.
Keep those requirements, generic tool support, native approvals, rich results, requester isolation, interruption, quiet delivery, and the default-off configuration gate.

The reviewed production Python diff against the integrated base is 57 files, +4,786/-125 lines, or net +4,661.
It introduces 22 Python modules and changes 35 existing modules; the separate generated tool metadata file brings the production file count to 58.
The focused `tool_jobs/` package accounts for 3,043 added lines.
These measurements cover the entire PR, not just the latest integration-test commits.

### Recommended changes, in priority order

1. **Persist whether an approval requires background jobs when the approval is created.**
   Before this refactor, `tool_jobs/disabled.py::_approval_uses_jobs` reconstructed this fact during disabled startup by locating a saved SDK run, trying canonical private storage layouts, and inspecting its tool arguments and delegation state.
   Its dedicated `history/session_context.py::read_scope_session_run` reader had no other production caller.
   Record a narrow, explicit feature-ownership marker in the approval continuation while the exact paused run is available, and use it to park the continuation after restart.
   Preserve the current distinction between ordinary approvals and feature-dependent approvals, including explicit null wait metadata, delegated/member calls, and approvals created before any job record exists.
   The original classifier, reconstruction, and dedicated reader occupied 97 lines across three functions; replacement metadata plumbing reduces the possible net saving.
   This recommendation removes inference and storage-layout coupling from the normal disabled startup path.
   Do not add migration handling for this PR's unreleased background-job records or development snapshots; update the new representation and feature-specific fixtures directly.
   Existing ordinary approval records can default the new marker to false without database reconstruction or a migration pass.

2. **Give tool filters one shared policy definition used by construction and retained-job authorization.**
   `tool_jobs/authorization.py::_configured_tool_allowed` repeats inclusion/exclusion rules also implemented by `tool_system/metadata.py::_apply_implicit_toolkit_filters` and the MCP catalog/dispatch paths.
   The intent is the same: determine whether a configured tool name belongs to the allowed surface.
   Keep a small pure filter predicate beside the existing tool-policy code, with explicit normalization for each toolkit convention; use it in the active construction and authorization paths.
   Preserve the semantic difference: an empty implicit toolkit include list allows no functions, while an empty MCP assignment/server include list is unrestricted.
   Preserve inherited null exclusions, remote-name matching, and local OAuth helper exceptions.
   Leave exact owner, factory provenance, membership, and current-configuration checks at their existing boundaries.
   The main benefit is maintaining one definition of filter behavior; net line savings may be small or zero.

3. **Consolidate delegation's shared approval/result postprocessing while retaining distinct execution ownership.**
   `delegation/execution.py::drive_delegations` grew from 376 to 522 lines.
   Its foreground and background paths repeat resolved-child event emission, approval projection, result formatting, and parent persistence.
   In particular, the resolved-tool loops around lines 1034 and 1098 perform the same lookup and event emission; terminal result formatting also appears in `_background_child_outcome` and the foreground path.
   Share the matching presentation/formatting operations and make the parent path express the three relevant outcomes clearly: still running, awaiting approval, or terminal.
   Keep native child execution, liveness locks, reusable sessions, and approval continuation in the delegation adapter.
   Preserve the order of durable parent persistence, result acknowledgement, and claim release; cancellation of a parent wait must still leave accepted child work owned.
   Avoid generalizing the exception paths together: background cleanup preserves primary errors and retained execution ownership differently from foreground cleanup.
   This is primarily a readability and duplicated-policy reduction; extracting a large block into another file alone is not a reduction in lifecycle complexity.

### Machinery that should remain

- The runtime's separation of execution ownership from individual waiters is required for interruption and later retrieval.
- Exact persisted consumption evidence is required before suppressing a future completion response.
  Removing readback or acknowledging at result creation would weaken recovery.
- Resource leases and task-affine MCP connection cleanup are required when a tool outlives its originating SDK run.
- Native delegation's approval bridge is required because a child can reach a new approval boundary after the parent has detached.
- Internal completion admission already uses the ordinary journal and serialized response owner.
  There is no independent Matrix delivery pipeline to delete.
- Cancellation admission, execution drain, and durable settlement represent different boundaries.
  Similar-looking flags are not sufficient evidence that these states can be merged safely.
- Rich-result serialization preserves media, artifacts, and control outcomes across restarts.
  Replacing it with plain text would change the agreed feature.

### Approach and expected impact

Prefer the targeted changes above over a rewrite of persistence, approvals, or the response lifecycle.
A new unified store would still need to coordinate SDK session persistence and existing journal ownership, so a large reduction is not established.
File-only reorganization can improve navigation but leaves the state transitions and interactions intact.

The evidence supports modest deletion opportunities and a more useful reduction in duplicated decisions and implicit ownership.
It does not support promising that thousands of production lines can be removed while keeping all agreed guarantees.
The approval marker is the strongest simplification candidate; filter policy sharing is the clearest prevention of future drift; delegation cleanup is the main local readability opportunity.

Suggested sequence:

1. Specify marker creation and the false default for ordinary approvals, update feature-specific fixtures, and replace normal startup inference.
2. Share filter policy with explicit tests for the differing empty/null conventions.
3. Consolidate only equivalent delegation postprocessing, preserving the persistence/acknowledgement boundary.
4. Run the affected integration suites and live interruption, streaming, approval, and restart scenarios before publishing any implementation.

### Audit verification

The existing tests in `test_background_tool_jobs_config.py`, `test_tool_job_authorization.py`, `test_background_delegation.py`, `test_tool_job_internal_completion.py`, and `test_tool_job_restart_integration.py` all passed during this audit.
They establish the current behavior around the proposed boundaries; they do not validate refactors that have not been implemented.
The audit itself changed only this living document; implementation verification is recorded below.

### Implemented simplifications

Approval continuations now record `requires_background_tool_jobs` when the exact pause is saved.
Disabled startup reads that marker and saved job sources without opening SDK session databases.
The marker remains true across later approval generations.
Ordinary approvals default to false, with no migration pass for this unreleased feature.
Wait metadata is interpreted as feature ownership only when the startup-pinned option is enabled; native job and internal completion ownership remain explicit.

One small `tool_name_allowed` predicate now serves toolkit construction, MCP catalog/dispatch paths, and retained-job authorization.
Callers preserve their distinct empty-list conventions through normalization.
Delegation now shares resolved-child completion emission and terminal result/receipt formatting, with persistence, acknowledgement, approval, and execution ownership still explicit at their existing boundaries.

| Refactor | Production additions | Production deletions | Net lines |
| --- | ---: | ---: | ---: |
| Persist approval ownership; remove SDK startup reconstruction | 66 | 122 | -56 |
| Share tool filter policy | 51 | 22 | +29 |
| Share delegation completion postprocessing | 51 | 28 | +23 |
| Total | 168 | 172 | -4 |

These changes affect 15 production Python files and leave production size effectively unchanged.
The benefit comes from removing reconstruction dependencies and duplicated rules.
Before integrating the newer main changes, the whole PR changed 63 production Python files with +4,814/-157 lines, or net +4,657.
The additional touched files include the existing MCP filter owners and approval journal forwarding path; the shared filter predicate is the only new Python module in this pass.

### Refactor verification

- Approval-focused suites, writer/generation cases, completion tests, repository hooks, and Tach passed; independent task review found no issues.
- Filter construction/authorization/MCP suites passed 213 tests before and after extraction.
  Review identified missing cross-owner exclusion coverage; two real manager/toolkit/authorization cases were added, both detected a deliberately removed exclusion clause, and the 185-test follow-up suite passed.
  Scoped re-review accepted the fix.
- Delegation-focused suites passed 85 tests before and after extraction, with targeted hooks passing and independent task review finding no issues.
- The final live run exercised six scenarios and passed 21 assertions through real Matrix, backend, and tool execution.
  These cover default blocking, completion during visible streaming, human follow-up into a newer turn, native approval across enabled/disabled/enabled restarts, delegated approval, and background delegation.
  Assertions verify execution counts, visible partial Matrix edits, and exact tool/child results reaching the model and parent agent.
- All repository pre-commit hooks and Tach dependencies/interfaces passed on the combined implementation.
- The full non-Matrix suite passed 21,470 tests with 12 skips across two nonoverlapping groups: 21,467 tests in parallel, followed by three timing-sensitive cases sequentially.
  The parallel group completed in 318.20 seconds with 26 warnings; the sequential group completed in 3.18 seconds.
- Final independent review approved the combined implementation with no remaining findings.

Two initial parallel suite attempts exposed timing failures in unchanged shutdown, shell PID-file readiness, and native knowledge-reader deadline tests.
The failing tests and their relevant execution paths were checked against the original PR base; no feature change was implicated.
All three passed unchanged in the final sequential group, and no tests were omitted from final verification.
No unrelated production or test changes were added for these timing failures.

Tests caught two implementation errors before the approval refactor was committed: filtering a streaming pause down to pending tools lost earlier wait metadata, and unconditional interpretation of a native `wait_timeout` argument falsely marked an ordinary disabled-mode approval.
Both now have regression coverage.

### Integration with current main

Current main shares normal and approved-agent execution through the response lifecycle.
The feature now uses that shared driver, removing its obsolete agent-specific result-join loop while retaining the team continuation owner.
Persisted background-job ownership is combined with main's runtime-model and continuation-budget fields.
Execution/resource ownership, interruptible waiting, durable consumption, and disabled startup behavior remain intact.

Against the integrated main revision, the PR changes 63 production Python files with +4,784/-156 lines, or net +4,628.
Reusing the newer approved-agent driver reduces the feature diff by another 29 net lines beyond the three original simplifications.

The focused integration suite passed 1,488 tests with two optional skips.
All repository pre-commit hooks and Tach dependencies/interfaces passed.
A fresh live Matrix/backend run passed the same six scenarios and all 21 assertions, including exact result delivery and approval parking across restarts.
Independent review approved the merge's conflict resolutions and lifecycle integration with no findings.
Final full non-Matrix verification passed 22,267 tests with 13 skips across nonoverlapping groups: 22,240 cases using xdist load scheduling, followed by 27 cases sequentially.
The parallel group finished in 324.78 seconds with 27 warnings; the sequential group finished in 5.01 seconds.
The sequential group contains the 24-case storage suite and the three previously identified timing-sensitive cases; no tests were omitted.

An earlier full attempt stalled near completion and exposed an order-sensitive cache-diagnostics assertion in unchanged code.
The assertion counts all process-local adapters, so an unrelated retained empty adapter reproduces its failure; closing that adapter makes the test pass.
The storage suite passes alone, and the new approval suites followed by the diagnostic test leave no surviving adapters.
The original adapter owner and scheduler stall were not established; the interrupted run is not completion evidence.
The final load-scheduled run and isolated group passed without production or test changes for those issues.

### Deployed restart and synchronous-tool regressions

Two deployed failures were reproduced after the integration above:

- Plain synchronous tools could execute successfully and then be recorded as failed during cleanup on Python 3.14.
  The SDK runs their hook bridge on a worker-local event loop; the job's main-loop cleanup tracker incorrectly retained tasks from that loop.
  The job now owns the complete offloaded dispatch without attaching the outer tracker to the worker loop.
  Async calls retain their existing tracked cancellation drain.
- Recovery reused the earlier Matrix response event but started its presentation empty, replacing already-streamed prose and tool metadata.
  Recovery now reads the latest trusted visible edit and carries its text and trace into agent and team generation.
  Earlier text remains a fixed prefix, tool indices continue after its trace, and the recovery prompt tells the model not to repeat it.
  An unreadable earlier response leaves recovery retryable instead of replacing unknown content.

Review also reproduced and corrected terminal-only provider output being hidden by that prefix, setup failures treating recovered prose as a disposable placeholder, blocking cancellation replacing the prefix, and approval completion reconciliation rewriting an older same-name tool slot.
Regression tests cover agent and team responses, streaming and blocking delivery, approval snapshots, and exact trace preservation.
Blocking teams apply the saved prefix once after the shared driver returns, including continuation-limit notices.
If tool calls are hidden by the current config, recovery keeps the earlier prose while removing matching tool markers and trace metadata together.

- [x] Real Matrix/backend restart checks: 27 assertions covering repeated restarts, shutdown-interrupted tools, blocking recovery, and approval recovery; prior prose and trace survive on the same event and original side effects execute once.
- [x] Real Python 3.14 Matrix/tool checks: 37 assertions covering registered coding and todo tools, normal waits, zero waits with retrieval, saved outcomes, and a restart with the feature disabled.
- [x] Add Python 3.14 CI coverage for registered execution, wait budgets, and tool hooks alongside the full Python 3.13 suite.
- [x] Preserve newly published blocking wait progress and its trace through cancellation: eight further live assertions pass after a real Matrix Stop reaction.
  Wait updates carry a complete presentation; cancellation re-reads the latest owned Matrix edit and leaves it untouched if that read fails.
  This reuses the existing response owner without adding a separate mutable presentation cache.
- [x] Combined verification: 22,318 tests passed with 13 skips across nonoverlapping groups (22,290 in parallel and 28 isolated cases); repository hooks, full type checking, and Tach passed.
  The final parallel group completed in 322.45 seconds and the isolated group in 4.83 seconds.
  The broader live lifecycle suite also passed 21 assertions for ordinary execution, active streaming, human follow-ups, approval parking across restarts, and delegation.
- [x] Independent review accepted the corrections with no remaining findings.

The broader Python 3.14 suite is not claimed to pass: the pinned SDK has nullable-schema differences, and a live-test helper explicitly launches Python 3.13 into an inherited test environment.
The focused CI lane avoids those unrelated paths while covering the deployed cross-loop failure.

The previous head's sole CI failure was a two-second deadline in the restart integration test while an empty consumption finalizer had already finished.
A controlled 2.1-second finalization delay reproduced the failure without an execution-ownership defect.
The test now awaits the task after its existing start barrier and human signal; the suite's 60-second timeout still bounds deadlocks.
The same latency probe and both restart integration cases pass, with no production change for this test timing issue.

### Exploratory testing through a real agent

The live-test workflow now requires exploratory model testing for risky agent-facing changes, alongside deterministic protocol checks.
Give the model a goal through Matrix, let it choose additional cases, and independently verify its tool calls and filesystem, process, and durable-state claims.
Record the build, model route, bounded test scope, findings, and untested cases in persistent evidence storage.

On `5f33fa647`, a real `codex` / `gpt-6-astra` agent completed 11 exploratory cases using the normal coding, shell, and job tools in an isolated MindRoom/Tuwunel instance.
It exercised default, zero, and bounded waits; handle rediscovery; shell errors; cancellation; repeated result retrieval; Unicode; and output-file capture.
Nine independent lifecycle checks passed, including admission of a human follow-up while the original shell command remained active, exactly one original side effect, and preservation of streamed prose on the same event after a backend restart without replaying the interrupted tool.
A Matrix Stop reaction cancelled the visible reply; this does not claim cancellation of independently owned jobs.
The completed exploration thread, including its cancelled job, received no additional visible reply across the restart; only its internal thread-summary metadata was added.

The real agent exposed the existing shell-output capture limitation: 552,000 generated bytes became a 44,366-byte saved tool result with a truncation notice.
An independent registered-tool reproduction likewise saved only 2,223 bytes from 2.5 MB of generated output.
The affected shell and output-file implementation is unchanged from `main`; its correction is tracked separately in [PR #2130](https://github.com/mindroom-ai/mindroom/pull/2130).
The reported `tail=2` problem did not reproduce for stdout: the independent probe returned exactly two data lines plus the cwd and truncation notices.
Unknown job IDs receiving the scoped unavailable-job message and shell error text being returned by a completed tool invocation do not establish additional job-runtime bugs.

This exploratory run did not cover 24 hours of elapsed retention, other providers, worker-container routing, media, or approvals.
Earlier deterministic approval and Python 3.14 coverage remains separate evidence; the real-model run does not replace it.

### Rebase and fresh review after the shell fix

PR #2130 was squash-merged, and this branch was rebased onto `main` at `d697d2a7b`.
The rebase retains the new subagent model selection alongside managed wait budgets.
A real-model Matrix run verified all 2,500,000 generated shell-output bytes, the requested child model in durable session state, human follow-ups, Stop, and recovery of the same visible message without replaying its interrupted command.
The rebased baseline passed 22,549 tests with 13 skips across parallel and isolated groups.

Fresh independent review found four additional gaps, now covered by regression tests:

- Skill access must retain its concrete source through the output-file wrapper. Configured skill grants are rechecked; workspace script execution remains blocked by the existing skill policy.
- Ad hoc teams use an ordinary agent as their Matrix transport. Managed execution and durable discovery accept that runtime-owned actor/transport pair while retaining requester, configured-team membership, and native delegation checks.
- Fresh blocking replies can publish progress while waiting. Stop and restart cancellation preserve the latest owned Matrix presentation, just as recovery does, and leave it intact if it cannot be read.
- Restart and shutdown flushes preserve unchanged job timestamps so recent-result ordering survives recovery.

These corrections reuse the existing ownership and presentation boundaries; they do not introduce another team roster or mutable presentation cache.
They passed 348 focused tests with two optional skips and ten further real-model Matrix checks for skill access, workspace script policy, fresh blocking Stop preservation, and ad hoc member tools.

A second independent review identified a cross-turn ad hoc team gap: an idle completion resumed only the transport agent, which could not retrieve another member's exact-owned result.
Completion now reconstructs the currently authorized owners of outstanding jobs from their existing records.
Ordinary turns join only their own jobs; team turns and approval continuations also join their materialized members' jobs.
No separate persistent team roster is added.

Real-model testing exposed the other half of that path: member job controls had been constructed with the transport agent's identity.
They now bind the actual member while retaining the original requester, transport, and conversation.
The regression test uses normal agent construction and actual SDK result consumption, and was observed failing before that binding correction.
A fresh Matrix run passed six checks: a human follow-up ended the original team wait, its single-agent reply finished while the other member's command was still running, and a later team continuation consumed and delivered the exact result without repeating the command.

A further boundary review found that replaying a generator's original chunks discarded the state-conflict notice added during result consumption.
Replay now retains that appended notice; SDK-dispatch regressions cover plain and rich streams, newer parent state, events, and media.
The same review checked cleanup failures against the baseline SDK: Agno already logs and suppresses toolkit disconnect errors.
The documentation now distinguishes that existing best-effort policy from cleanup exceptions reported to the job runtime, which become failed outcomes.
This preserves the existing teardown contract instead of adding a separate toolkit cleanup policy.

Review also exposed a delivery-policy gap: replacing a silent schedule's origin with a generic completion origin enabled visible progress after recovery.
The design keeps completion intent runtime-owned while preserving source delivery policy independently.
Accepted generic and delegated jobs retain their source kind in the existing durable adapter metadata; no new store or recovery process is introduced.
Automatic result joins stay within the same quiet/ordinary delivery policy so newer ordinary turns cannot publish quiet schedule results, and quiet continuations cannot suppress ordinary results.

The seven new regression cases were observed failing before correction and now pass in a focused run of 377 tests, with two optional skips.
The quiet delivery contract retains the existing model-controlled final-report policy: `NO_REPLY` is suppressed, while findings or an unfinished-work report may still be delivered.

A later final-document review found that ordinary team joins replaced prose already shown while waiting.
The shared turn state now retains completed semantic text, while blocking responses retain their delivered segments and team streams seed fresh trackers from the prior document.
Terminal SDK summaries keep the structured live document and its trace.
Regression assertions inspect the final document and canonical recorder, including repeated joins, recovered prefixes, cancellation, and quiet control tokens; approved continuations retain their existing collector.
Quiet continuations preserve substantive findings without concatenating `NO_REPLY` into a visible report.

Subsequent review found that cancellation could finish one resource close but abandon the rest of its captured batch.
The resource owner and SDK teardown adapter now drain each complete release batch before propagating caller cancellation.
Agent and team regressions cancel both successive closes in immediate and detached cleanup paths.

Blocking agent approval pauses also retain the completed background-join presentation through the existing pause builder, including hidden tool identities and visible marker numbering.
The agent stream adapter applies terminal-only content fallback to every attempt, so a normal background continuation retains its answer even when the provider emits no text delta.
Regression coverage exercises actual SDK approval pauses and both collected and Matrix streaming delivery.

The next review checkpoint identified two policy boundaries that should reuse existing rules.
Calls marked `stop_after_tool_call` control the next model step and must return their actual result before that decision; they remain inline and omit the shared waiting option.
Returning a job handle instead would lose model-switch timing or dynamic-tool continuation semantics, so extending detached result handling for those controls would add unnecessary state.
Application authorization still runs before inline control execution, and unsupported numeric waits fail before side effects.
Registered agent and team model-switch regressions cover both timing choices, human follow-ups, immediate waits, and finite waits; dynamic loader schemas use the same stop-after flag.

Native job access now applies the same frozen storage-binding comparison as native retrieval.
Tests change either the caller or child worker scope and require discovery, controls, automatic joining, and completion lookup to withdraw access.
The job runtime does not gain another storage policy or migration layer.

Native delegation output-file policy now follows accepted child execution instead of the foreground wait.
The early handle remains visible, and the operation applies the shared finalizer once to its completed result.
Only the accepted relative output path is added to the existing native adapter snapshot; approval recovery resolves and revalidates the caller's current output policy before resuming.
Completed result retrieval reads the saved receipt without validating or rewriting the old destination.
Integration coverage includes explicit and automatic output, blocking and detached execution, approval recovery after runtime reconstruction, invalid resumed paths, and retrieval after the completed file is moved.
The unused session-root forwarding helper and its exports were removed, restoring direct use of the existing storage resolver.

Current-grant checks now include the authored constructor settings captured by each concrete non-MCP toolkit.
The existing immutable construction snapshot retains their canonical value; changing these settings invalidates retained calls and outcomes until the settings match again.
This closes constructor-controlled grants such as file and shell enable flags without enumerating tool-specific options or rebuilding remote clients during authorization.
Include/exclude filters retain their existing per-function checks.
Real SDK integration tests mutate eager and deferred file/shell settings while an outer job is active, verify that the side effect never occurs after revocation, and exercise discovery and result access before and after restoring the original settings.

### Tools with their own background execution

The shell's native timeout can finish a tool invocation while its process is still running.
Wrapping that invocation in a generic job produced a completed job containing a second background handle, so generic cancellation no longer owned the process.
This was reproduced with a real process through both agent and team SDK dispatch.

The initial correction used registered toolkit/function pairs for schema projection and execution admission, using existing construction identity so presets retained the same behavior.
The configurable toolkit-level policy below supersedes that list.
Shell run, check, and kill functions keep their existing arguments, process owner, and shell handles without a generic job or added `wait_timeout`.
The normal application authorization boundary still applies, including current constructor grants inside a retained outer job.
Human follow-ups do not detach excluded calls through the generic runtime; the shell's native timeout bounds their wait.
Shell handles are outside generic discovery and automatic completion delivery.
Other functions, including an unrelated function with the same name, retain managed execution.

Regression tests exercise actual process completion and termination, schema projection, rejection of a stale generic waiting argument before any side effect, and a human follow-up while a native shell wait is active.
The existing shell-output fix from PR #2130 remains responsible for complete output-file capture; no second output truncation policy or retired-job tombstone store is added here.
A real-model Matrix run passed ten independent checks across native completion, cancellation, and a human follow-up, alongside a generic file job.
The redirected output retained all 262,144 payload bytes and its exact sentinels and working-directory header; shell controls created no generic job records.

Independent review found that an integer wait budget beyond floating-point range raised an unhandled overflow and aborted the response.
The shared validator now checks the representable nonnegative range before conversion, retaining the existing recoverable tool-error contract.
Real SDK batch regressions cover streaming and blocking correction after rejection, with no invalid side effect and successful sibling execution.

### Configurable toolkit exclusions (2026-09-19)

The user-approved configuration replaces the root boolean with `background_tool_jobs.enabled` and `background_tool_jobs.exclude_toolkits`.
There is no scalar compatibility shim for this unreleased option.
The startup snapshot deep-copies both settings so in-place list edits cannot change active execution.
The exclusion list uses registered toolkit names and covers all functions, including custom/plugin toolkits and preset-expanded tools; no plugin-specific opt-out API is needed.
Explicit lists replace the default `[shell]`, including an empty list.

Native delegation applies the same policy before accepting fresh subagent and follow-up turns.
Saved approval continuations retain their accepted execution owner in either direction when exclusions change.
The already-recovered job index distinguishes accepted background work from foreground continuations; authorized lookup still controls access.
Native delegation reuses its existing persisted owners without a migration.

Integration coverage exercises real plugin loading and SDK dispatch for agents and teams, native argument preservation, startup pinning, config-command restart notices, excluded delegation, stale wait rejection, and approval recovery after exclusions change.
Verification passed: 212 focused tests; the full Python 3.13 suite passed 22,784 tests with 25 optional/environment skips, including 27 process/storage cases run separately.
A real-model Matrix run passed 14 independent checks across native shell completion/cancellation, exact output capture, a human follow-up, both excluded plugin functions, native timeout argument preservation, and ordinary generic jobs alongside them.
Repository hooks, type checks, Tach, and module privacy checks passed.
Independent review status is tracked on the PR.

Independent review identified three approval/lifetime gaps in the first configurable version.
Excluded plugins could have a native argument named `wait_timeout` mistaken for managed ownership, and a config change could reinterpret a saved generic approval's arguments.
The dispatch adapter now captures each call's wait mode in the existing SDK run metadata before approval, keyed by exact run and call identity.
Continuation restores that metadata through the existing saved-run path; later calls use the current policy.
The SDK tool-preparation adapter binds the exact stored run's wait metadata into its active context, including team members, and publishes later call captures back into the saved run.
Pause ownership comes from the captured mode instead of argument-name inference.
This introduces no new store or database migration.

The third gap allowed a delegated child's generic tool to detach when excluded delegation supplied no outer job.
Nested tools now keep the native child owner even after a human follow-up.
Regression coverage executes agent/team approvals, streaming and blocking, changes exclusions in both directions, verifies later calls and new runs, checks disabled restart parking, and interrupts a real slow child tool.

The next independent review found three additional gaps outside the exclusion parser.
Direct toolkit construction now captures authored options just like registry construction, so configured Dynamic Workflow tools pass unchanged grants and lose access after those options change.
Terminal-only text fallback now belongs to the response attempt, preventing a nested tool completion from causing the parent's streamed prose to repeat, including when background jobs are disabled.

Synchronous completion ownership now belongs to each SDK call inside an accepted job, including embedded agents that construct their own models.
This moves the existing tracker and dispatch drain to the shared SDK boundary instead of sharing one tracker across an entire async workflow.
Async calls receive independent leaf trackers; sync calls retain their complete worker dispatch, including hook cleanup on a worker-local event loop.
The adapter is gated by accepted execution ownership, so foreground execution keeps the SDK's normal behavior.
Real workflow regressions execute multiple calculator calls sequentially and in parallel.
Cancellation regressions verify that all started nested threads finish before job settlement and resource cleanup, with both sync and async hooks.

Delegation's own policy approval can pause before any child or job exists.
That gate now restores the exact call's saved wait mode from its owning SDK run, including member runs, instead of recomputing it from the current exclusion list.
The driver records a mode when it owns a call without an SDK capture, using the same metadata rather than another persistence format.
Restart regressions cover both exclusion transitions and deliberately reuse a call ID in the team and member runs to verify that their policies stay separate.

Automatic SDK learning reuses the agent's model but invokes internal extraction functions without an agent/team run owner.
Those calls now keep SDK execution and native schemas; nested extraction retains and rechecks its outer tool's authority instead of inventing another job owner.
The same boundary covers other internal model calls without adding a learning-specific allowlist or copying models.
Real SDK regressions verify automatic and agent-requested learning with both shared and requester-scoped storage, including revocation immediately before the actual memory write.
