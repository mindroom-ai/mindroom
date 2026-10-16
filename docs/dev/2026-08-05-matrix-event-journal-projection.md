# Matrix Event Pipeline Simplification Plan

Archived record of the cutover that made the event journal the single durable owner of Matrix events (PR #1800).
It keeps the reasons, rejected designs, refuted theories, external behaviour, and measurements from that work; several mechanisms it names were later replaced, so check the code before quoting any of them as current.
What the journal guarantees today, including the decisions that still hold and the homeserver behaviour it relies on, is in [`docs/architecture/matrix-event-journal-contracts.md`](../architecture/matrix-event-journal-contracts.md).

## Why

At baseline `b639b6ef3` the overlapping Matrix subsystem held 20,248 production lines, and the 29-file `matrix/cache/` package overlapped with conversation history, dispatch obligations, sync trust, source deduplication, and response reconciliation.
Correctness depended on several stores agreeing about the same event after restarts, limited timelines, edits, redactions, and delivery failures.
Sliding Sync exposed the recovery gaps, but most of the complexity came from MindRoom independently repairing, caching, certifying, and replaying state that nio or one durable application store should own.
The goal was one owner per durable fact.
The work was to proceed only if a prototype proved the replacement correct, fast enough, and materially smaller, and to stop if it needed a compatibility facade, a second writer, retained edit chains, or a second recovery classifier; it needed none of them.
No merged state could leave the old and replacement paths both active, because a half-migrated deduplication substrate answers users twice.

## Decisions and rejected alternatives

### Source ownership ends at outbox enqueue, not at turn-store adoption

The plan first ended journal ownership once the turn store durably adopted a turn, and that would have lost answers.
Replay is driven only by pending journal events, so nothing replays a turn that was adopted but not yet delivered: a crash after the turn record and before the model ran would leave no owner for the work.
Turn records also held nothing about attachments, so a coalesced caption-and-images batch could only have replayed from the turn store as text.
Turn records deliberately carry no attachment source: for an encrypted attachment that source holds the AES key JWK, the IV, and the content hashes, a turn record outlives its turn, and replay already rebuilds media from the journal's pending source.

### Turn records moved into the journal database instead of being bridged to it

The specified bridge for the JSON ledger, a persist drain that calls `run_coroutine_threadsafe` and blocks, deadlocks.
Both journal backends offload with `asyncio.to_thread`, the loop's default executor, so once every executor thread holds a turn blocked on its persist future, the store write that would release them cannot get a thread.
A standalone probe reproduced it: one concurrent turn succeeds, and at `min(32, cpu_count + 4)` concurrent durable turns, which a multi-room deployment reaches, none returns.
A dedicated executor breaks the cycle but keeps the write-behind queue, which cannot enlist in the caller's transaction, so the ledger write became awaitable instead.
Only writes were converted (28 call sites) and the hundred-odd reads stayed synchronous over an in-memory map, which is why production changed by only +332/−581.

### Outgoing messages are not seeded at send acknowledgement

Seeding the projection at send acknowledgement was built first, for turns that read the conversation after sending and for turns no room event triggered, and then deleted.
The ordering guarantee decided it, not the absence of such readers: the server orders the bot's own echo before any later message on the same timeline, so a later turn already sees the previous answer.
Seeding only matters for a read in the same execution as the send, such as the thread-summary pass or the Matrix conversation tools, and there it makes the message visible sooner without making the read ordered.
It also cost more than it gave.
A send response carries only `room_id` and `event_id`, not the `origin_server_ts` the projection orders by, so seeded rows carried provisional ordering, and the "Thinking..." placeholder entered the durable record.
A seed stamped with the local clock outranked genuine later edits: enumerating all twenty-four arrival orders of a seed edit, its echo, the original, and a later edit found sixteen that froze the answer at the earlier edit, after four fixes had each closed only the ordering that prompted it.
Deriving the seed's ordering key from the revision it replaced fixed the clock case and left the reverse one: a later edit that arrived first was replaced by the seed, whose echo then canonicalized to a timestamp below that edit, and a reduction keeps no losers to restore.
Those orderings were first dismissed as unreachable because a homeserver stamps a send after everything it has already delivered, which holds for one server only: `origin_server_ts` is set by the originating server, so in a federated room a remote edit stamped later can arrive before ours.

### Resolved sidecar text lives on the projected revision and is resolved lazily

Most agent messages exceed the Matrix event size and arrive as a preview plus an MXC sidecar, so a projection that stored content verbatim served most of an agent's own history as previews.
Resolving during admission was rejected because admission commits before nio accepts the event, so sync acceptance would depend on a media fetch that can fail or hang.
Moving projection out of the admission transaction so a worker could resolve first was rejected: context-only history is admitted already settled with its payload cleared and never reaches a worker, so recovering it would need a second obligation kind and a second fence, a recovery state machine bought to avoid one media fetch.
The same fact refuted review proposals for epoch-scoped projection watermarks and a durable admission-to-projection fence: projection commits inside admission, so there is no window to fence.
A worker that skips revisions already superseded was rejected because it cannot know a later edit is coming; resolving on read downloads only the revision that won, whatever order the edits arrived in.

### Approvals got their own durable store

Approvals needed their exact-call identity kept durably, but neither existing owner could hold it: `visible_messages` models conversation messages, and the journal clears a settled event's payload on purpose.
They got their own small durable projection instead of widening either.

### A changed sending device reconciles against the room

Recovery was first framed as a choice between refusing to resend after a device change, which loses the answer silently, and resending under a fresh transaction ID, which duplicates if the original landed and breaks the deterministic-ID invariant.
Keeping the deterministic ID and reading the room for an answer already there avoids both, because reusing the string on a new device is still deterministic and still deduplicates that device's later retries.

## First fixes that were themselves the bug

Several fixes over-corrected past the defect they targeted and produced something worse, each reasoned from the one ordering that had been demonstrated.

| Defect | First fix | Why it was wrong |
| --- | --- | --- |
| A rejoin could resend a stale answer | Bind the transaction ID to the membership epoch and delete attempted rows | Deduplication was the mechanism, not the failure, so this turned a suppressed duplicate into a visible one |
| An echo could freeze a streamed answer | Treat a matching revision ID as a canonicalizing echo | Identity has no direction, so a late seed overwrote authoritative data with a local clock |
| A device change could resend a duplicate | Record the sending device when the row is claimed | A room lookup that raised left the row stamped with the current device, so the next pass skipped the lookup and posted twice |
| A failed final edit sent its error notice twice | Send the notice directly, then adopt it as the row's outcome | Acknowledgement is first-writer-wins and recovery can resend and acknowledge in the gap, and a membership-fence refusal and a Matrix failure return the same false, so a refused enqueue sent an old error into a room the bot had left; the fix sends nothing, because either the fence refused or the row still owes the answer |
| A cancelled ledger write | Roll the in-memory record back on cancellation | The cancelled write could already have committed |
| A cancellation right after an approval card was sent orphaned the card | Shield the durable write | `asyncio.shield` lets the write land but delivers the cancellation first, so the expiry ran before the row existed and the cancelled approval was recorded as approved; a detached task that records and then expires the card is the fix |

Making hydration the only proof of freshness for every read (`return not await conversation_is_hydrated(...)`) was first tried and reverted.
Non-blocking reads never hydrate, so every dispatch read stayed degraded and the dispatch-safe read collapsed into a strict one; eleven tests failed, including one that asserts a command must not block on a strict read.
The predicate later shipped applied only to reads whose caller requires complete history.

Before committing a fix, state the mirror-image input, the same events reversed or the benign server response with the same shape, and say which way each choice fails; prefer the failure a later pass can still repair, because hydration and delivery run once per something.
For the rejoin and echo rows a twenty-line probe against a temporary SQLite store settled each case in under a minute and disagreed with the reasoning every time.
For the device, final-edit, and ledger rows the second attempt was simpler than the first: read the room, send nothing, wait for the write to report.

## Matrix, nio, and tooling behaviour found during the work

The homeserver facts the journal relies on, such as Tuwunel's edit purge, `recursion_depth` semantics, and device-scoped transaction deduplication, are on the contracts page.

### nio classified recovered messages as history on the classic sync path

Live fuzz at 45 concurrent conversations lost replies with nothing pending and no failed admission: the unanswered sources were journaled context-only because nio reported `HISTORY` for messages a user had just sent.
One cause was the harness, which sent its scenario before the agent's first sync completed, and everything in an initial sync timeline is history by design.
The other was in nio before 0.37.0: mid-session a timeline event is `LIVE`, so `HISTORY` arrives only through the gap backfill for a `limited` timeline, whose continuity is proven by reaching the target token or by bounded exhaustion.
On the classic path `plan_sync_response` passed a `cursor_token` but left `membership_bound` unset, so a backfill that ran to the start of visible history proved nothing and left every recovered event classified as history; the sliding path set it from the same cursor.
The fix, mindroom-nio commit `0f2c318` released in 0.37.0, bounds the classic gap below by the `since` of the sync that opened it.
The loss reproduced on `main`, so it was pre-existing rather than a cutover regression.
One nio test pinned the old behaviour and was changed deliberately, because the errors are not symmetric: classifying already-seen history as `RECOVERED` costs a duplicate admission the journal recognises, while classifying a missed message as `HISTORY` loses the reply.

### Other findings

- Moving the final-text transform into terminal delivery first failed as `assert None == '$stream_1'`: the module-level streaming entry point forwarded an explicit keyword list to `streaming_cls(...)` and rejected `final_text_transform`, and the delivery reported `FinalDeliveryOutcome(terminal_status='error', event_id=None)` instead of raising, so a `TypeError` read as a missing event ID.
  Blaming missing `response_hooks` on the test doubles was an inference and was wrong; read the traceback before re-deriving the wiring.
- nio attaches provenance only to timeline events; state-block member events carry none, so their actionability cannot come from nio's classification.
- An old message on an ordinary live timeline, as when a replacement bot joins a room and syncs normally, is `LIVE`, admitted actionable, and suppressed later by policy; context-only classification is reserved for events not received while a member, such as cold history, and for notices and unknown msgtypes.
  A test that asserts the context-only mechanism rather than the no-turn outcome reads zero on that path and looks like a regression.
- A departure reaches the bot twice, from the local leave and from its sync echo.
  A guard cleared by a rejoin let the echo fence a second time and delete the conversation hydrated under the new membership, and an in-process record also lost or misattributed departures on a failed fence, a restart between the fence and its echo, two leaves before one echo, and an echo that never arrived, so confirming a local membership change against its echo has to be durable and exactly once.
- A post-restart wedge with none of the sync-livelock signatures showed one event logging `coalescing_gate_message_enqueued` with no `coalescing_gate_flush_started`, and that event's callback starting twice 13 ms and 19 ms apart.
  The flush was blocked on a lane slot held by the duplicate callback, which had lost the turn claim and was waiting for the winner's turn to settle; load cannot break that cycle.
  In that run the duplicate came from recovery replay after a restart, but two router relays sharing a human alias contend the same way with no restart involved.
- The Docker builder stage has no `git` binary, since git is installed only in the runtime stage for knowledge-base cloning, so a git source in `[tool.uv.sources]` fails all four image build targets with "Git executable not found" while every Python CI job stays green.
- vulture cannot fail on a whitelist entry whose symbol no longer exists, so deleting modules leaves their suppressions behind: eight entries for deleted `matrix/cache/` modules survived the deletion, and the byte-identical whitelist was misread as proof that nothing had been suppressed.

## Evidence

Timing thresholds are manual release evidence rather than CI assertions.

### Prototype measurements

Measured on the prototype on `macOS-26.5.2-arm64` with Python 3.13.10; `tests/manual/event_journal_measurements.py` reproduces them.
Reads ran against a 100,000-message conversation walked to its start over 2,001 cursor pages, because an index that only helps the newest page looks fine on a small table.

| Measurement | Result | Target |
| --- | --- | --- |
| Durable admission, p95 | 0.14 ms | under 50 ms |
| Bounded conversation read, p95 | 0.16 ms | under 50 ms |
| Deepest cursor page, at 100,000 messages | 4.44 ms | no degradation with depth |
| Writer-queue wait, p95 | 7.4 ms | under 100 ms |
| SQLite lock failures, 50 concurrent conversations | 0 | zero |
| Concurrent admission throughput | ~9,600 per second | not set |
| Conversation read query plan | covering index `visible_messages_page` | indexed |
| Pending replay query plan | index `journal_events_pending` | indexed |
| Database size per message | 636 bytes | not set |
| Replacement source | 4,589 lines (an earlier 3,654 omitted `journal_dispatch.py` and `matrix/outbound_projection.py`) | smaller than replaced |
| Replaced owners | 15,870 lines | — |
| Projected net change | −12,216 lines projected, while the branch then stood at +2,624 | materially net negative once the old owners are deleted |

The nine-boundary crash matrix, from before the journal commit to after acknowledgement but before settlement, produced one terminal turn and at most one visible response at every boundary.

### Live homeserver proof

`tests/manual/event_journal_live_proof.py` creates a disposable stack with throwaway accounts and removes it afterwards, so it can run at any commit without setup.
It passed against Tuwunel, the upstream `matrixdotorg/synapse:latest` image, and the MindRoom Synapse fork `ghcr.io/mindroom-ai/synapse:latest` (digest `sha256:fc4d4b8e50f1172d973b53179064dfe93d90070435d9a4642f8824b11f9471ff`, revision `69c5aa228f6b5130529863d43a43a4409dbfab82`), with all 22 required checks passing on both Synapse builds and on the fork with `mindroom_compact_edits_enabled` both on and off.

| Property | What the live server did |
| --- | --- |
| Recursive relations report a depth | A non-empty recursive page reports `recursion_depth`, and an impossible required depth is refused rather than silently met |
| Redacting the visible edit | The newest edit is visible before redaction, the redacted revision is unreadable, the message is reported as owing a refetch, and the refetch installs a revision the server still holds |
| Streaming edit churn | 25 edits produced exactly 1 logical row, carrying the newest body |
| Sidecars stay unresolved until fetched | 0 rows served before resolution; resolving installed the whole 8,704-character body and no reader saw the preview |
| Retry after acceptance | A repeated transaction ID returned the same event ID, and the same user re-logged in as a second device created a new event with it |
| Cold history is context only | An exhausted walk completed hydration, populated the conversation, and started 0 pending events |

- The second server caught the harness asserting Tuwunel's answer, `recursion_depth < 3`, as a contract: it could never fail on the server it was written against and failed on Synapse, so it is reported as a measurement instead.
- Device scoping was first demonstrated with a second registered account, which yields a second event whether or not the server keys on the device; the proof re-logs in the same account and refuses to run if the server returns the device it already had.
- Compact-edit collapsing never reached the edit-churn check, which admits edits with `room_get_event`; collapsing applies to `/sync`, Sliding Sync, `/messages`, and `/context`, so it reaches the journal only through cold hydration.
  On the fork, 25 edits to one message return 25 `m.replace` events from `/messages` with collapsing off and 1 with it on, and hydration lands on one row carrying the newest body either way, because the projection already reduces an edit chain to one row.

### Live fuzz

`scripts/testing/fuzz_live_matrix.py` drives concurrent operations with restarts against the same disposable stack.
Its duplicate check is `_assert_no_wrong_replies`, which fails on any source with more than one reply and on any reply to a source nothing expected.
`canonical_agent_replies` is not that check: it counts the reply-expecting prompts the scenario issued and moves with the seed, the profile, and the restarts, so comparing it across runs measures the scenario; a branch run reporting 17 against `main`'s 16 was once misread as a duplicate answer.
`--restart-interval` defaults to 100, so a `--steps 200` run restarts only once while still reporting `PASS`; the green cutover run was `--seed 42 --steps 200 --threads 45 --restart-interval 10`, which produced 18 restarts across 44 batches because the generator drops the trailing restart, and `--profile restart-regression` ignores the flag.
With the nio fix, seed 42 at 45 conversations ran 200 operations, 45 roots, 27 batches, and 123 canonical agent replies across one restart with zero lost replies and zero event-loop stalls, the configuration that had failed on both the branch and `main`.
At 45 threads the harness is not a single-run gate: seven runs on the same code gave two spurious failures, one and four lost replies at live batch 0 with `coordinator_queue_wait_ms` and `thread_read_total_ms` in the seconds and no errors from the code under test, and both passed on re-run with identical counts, while 12-thread runs never flaked.
A real regression reproduces on the same seed and a flake does not.

### Size of the change

Measured at `fa4b3268c`, the merged branch tip, with `git diff --numstat origin/main...HEAD`.
The whole-tree figures include deleted tests and tooling; the production line answers whether anything was simplified.

| | added | deleted | net |
| --- | --- | --- | --- |
| `src/mindroom` (production) | 16,728 | 21,825 | **−5,097** |
| tests, docs and tooling | 42,222 | 46,949 | −4,727 |
| whole branch vs `main` | 58,950 | 68,774 | **−9,824** |

`matrix/cache/` was 12,441 lines in 29 files and `event_journal/` was 6,295 in 19 while doing strictly more, and the tree held 328 fewer functions than `main`.
Mid-branch figures were wrong in both directions, from +3,940 while replacements landed ahead of the deletions to a claimed −8,258 that was 3,161 lines off by the merge, so re-measure rather than quote a figure taken before the tip.

## How the work was verified

These process lessons kept catching real defects and are not visible in the diff.

- Every fix was pinned by reverting the production change and confirming the matching test failed.
  Several tests that looked like coverage pinned the defect instead: one blanked an outbox row's device without changing it, so no duplicate was reachable, and another injected the binder it claimed to exercise.
- A mutation result lied in four ways: a concurrent agent rewrote the file before the mutant ran (verify the patch by checksum first), a syntax error made pytest exit 4 with zero failures (read the exit code), a `^FAILED` grep missed colourised lines (grep unanchored), and the test could not reach the mutated seam (a property spanning a decision and its transport needs a mutation at each).
- Review findings were verified before acting, because some were wrong in ways that made things worse: commit-then-publish for the ledger would have traded a rare duplicate after a failed write plus restart for a common one during any in-flight write, and a publish-before-commit window was called a regression although `origin/main` had a larger one.
- Each phase had to delete its predecessor and leave the tree smaller, so a green, well-tested approval-card change that added 68 net lines was held back, and a write path with no producer had to be wired or deleted.
- Building against the real nio branch, not tracing consumers by hand, showed that `persist_recovery=True` is required: nio refuses an acknowledgement past an open gap unless the gap row outlives the process, so with it false every certified response raises `LocalProtocolError`.
- Verify with a clean working tree: a marker registered only in an uncommitted `pyproject.toml` made seven tests pass locally that would have failed in CI.
- Parallel agents collided in git, not code: `git commit` commits the whole index, so one agent's commit swept in other agents' files, and `pre-commit` stashing unstaged files reverted another agent's restore; separate worktrees, or explicit file ownership in a shared one, avoid both.
  Errors from reading a file another agent is mid-way through editing belong to nobody and must not be fixed outside one's own scope, and a worktree auto-created from `origin/main` looks plausible while containing none of the branch, so check its base.

## Limitations accepted at the cutover

An attempted outbox row is resent across a rejoin, so when its first send never arrived, the previous membership's answer reaches the new one late.
That was preferred to deriving a fresh transaction, which posts the answer twice if the first send landed, or refreshing the payload, which leaves the durable record and the room disagreeing about what was said.
PR #1836 later replaced the resend: an old-membership row is reconciled against the room, adopting an event already there or retiring the row, and is never sent; both rejected alternatives still stand.

A permanently failing send has no retry horizon and is retried after every sync response, deliberately: every available terminal state drops the answer, the membership fence already withdraws rows for rooms the bot has left, and recording the sending device before each send bounds the duplicate scan to once per device change.
