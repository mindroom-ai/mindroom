# Matrix Recovery Single-Checkpoint Design

## Status

This document defines an isolated cross-repository experiment approved and validated locally on 2026-08-03.

The experiment starts from MindRoom `a5a6a338b` and mindroom-nio `bac260148`.

It replaces the proposed PR 1783 restart coordinator rather than stacking another fix onto it.

The development integration branch declares the exact commit from `fix/single-checkpoint-startup-rewind` as a temporary direct dependency in both wheel metadata and the lockfile.

Production rollout still requires releasing the nio change first and then restoring MindRoom's exact package-version pin.

## Problem

MindRoom currently persists the last Matrix sync token whose timeline cache and pre-certification work completed.

mindroom-nio separately persists the transport token with recovery gaps and callback rows before MindRoom certifies the response.

A crash can therefore leave nio at `T2` while MindRoom still trusts only `C1`.

Current main resumes nio at `T2`, drains retained work, and then replays from `C1`.

PR 1783 expands that handoff with restart phases, private nio state sanitization, and more lifecycle coordination.

The expanded protocol still leaves completed-marker and member-baseline restart defects.

## Design Decision

MindRoom's cache-certified checkpoint is the sole startup authority.

At every Classic startup, MindRoom asks nio to atomically reset its transport cursor and shared recovery state against that checkpoint.

A non-null checkpoint clears recovery generations created above it because their generation and sequence metadata belongs to the superseded cursor history.

This full reset also applies when nio's stored Classic token is equal because MindRoom writes the checkpoint only after every Classic source callback reaches durable admission.

Sliding work created after that equal Classic token remains above the checkpoint and is returned by Classic replay, so its shared completion and window state must not survive the reset.

A tokenless target also clears the recovery lane because retained rows cannot be ordered safely against an initial timeline from an unrelated generation.

This chooses one cold server ordering authority and accepts that a pre-admission event omitted from the initial sync cannot be recovered.

Callbacks that crossed MindRoom's admission boundary remain recoverable from the exact dispatch-obligation store with their full event payload.

Unaccepted events from a discarded generation are regenerated when Matrix replay returns them.

The next Classic Sync request replays from that checkpoint through the existing nio recovery, admission, dispatch-obligation, batch-cache, and certification paths.

No startup path attempts to certify or finish an advanced nio token.

This rule deliberately exchanges a bounded replay for one durable owner and a smaller crash state space.

## Alternatives Considered

### Extend PR 1783's restart coordinator

This keeps two independently persisted positions and adds phases to reconcile them.

It is rejected because the protocol already grew by thousands of changed lines and still missed two restart cases.

### Cache every admitted event and trust nio's advanced token

This reuses nio's event WAL and initially looked like the smallest replacement.

It is rejected because the response batch still owns limited-gap markers and batch-wide relation resolution.

It is also unsafe for a shared token when one room has retryable retained work while another room abandoned recovery before the crash.

nio does not persist every non-timeline response side effect needed to certify that shared token either.

Making this safe requires another durable response-status and acknowledgement protocol, which recreates the complexity being removed.

### Add a MindRoom cache journal

This would duplicate nio's persisted event rows and add another settlement protocol.

It is rejected because it creates a third owner without eliminating either existing checkpoint.

## mindroom-nio Boundary

mindroom-nio will expose one typed startup operation that atomically aligns persisted recovery state to a supplied Classic Sync token.

The store transaction will replace or delete the stored sync token and clear every recovery gap, pending callback row, completed generation-zero marker, and invalidated Sliding Sync window token.

The same full reset applies to a different token, an equal token, and no token.

The client installs an empty recovery lane in memory after the transaction.

The client operation will clear the matching window state, install the supplied token as `loaded_sync_token`, and clear `next_batch` so the first equal-token response is still applied.

The operation will reject use after any sync request has started or while response execution or recovery dispatch tasks are active.

The operation is intentionally a startup boundary rather than a general callback-cancellation API.

## MindRoom Startup Flow

MindRoom first initializes the event cache and loads the checkpoint whose cache generation still matches.

If no checkpoint validates, the authoritative token is `None` and the principal cache is purged as on main.

In Classic Sync mode, MindRoom invokes nio's atomic startup reset before starting sync for every authoritative token value, including an equal token.

MindRoom runs that startup-only client mutation on the owning event-loop thread so cancellation cannot leave an off-loop rewind mutating nio or its Peewee-bound store after startup has exited.

The tokenless reset clears the rejected cursor and recovery lane so the initial server timeline defines one fresh ordering.

The equal-token reset clears the lane completely because a valid MindRoom checkpoint proves that Classic work through that cursor crossed durable admission, while any newer Sliding residue will be replayed from the Classic cursor.

MindRoom then starts Classic Sync from the authoritative token with the existing first-response `full_state` behavior.

The replayed response must pass the existing response-batch cache gate and pre-certification side effects before a new checkpoint is written.

An `M_UNKNOWN_POS` rejection continues to clear MindRoom trust and fall back to a tokenless rebuild.

## Development Branch Integration

The MindRoom recovery branch is stacked on the independent tokenless-membership baseline branch.

Its package metadata temporarily points `mindroom-nio` at the exact reviewed commit on the nio development branch.

The lockfile records the same commit so source installs, built wheels, tests, and reviewers use identical code even if the branch later advances.

No development branch is merged, tagged, or released as part of this validation.

## Room-Member Baseline

A tokenless first sync describes state at the start of its timeline even when a homeserver places a membership event only in the timeline.

The tokenless baseline will therefore record eligible membership events from both the state block and the timeline before live hooks are armed.

That baseline remains pending across response attempts that reset the cursor before certification.

A restored-token first sync remains a catch-up stream and may emit unseen timeline joins.

This distinction fixes the false onboarding case without a lifecycle phase machine.

## Crash Semantics

A crash before nio's rewind transaction leaves the old mismatch, so the next startup tries the same rewind again.

A crash after a strict rollback leaves nio durably aligned to the MindRoom checkpoint with an empty recovery lane, so the next startup reuses that checkpoint.

A crash after any reset leaves an empty recovery lane aligned to the supplied checkpoint or cold start.

A crash during the replay leaves nio advanced again while MindRoom still holds the old checkpoint, so the following startup clears that partial replay state and replays the window again.

Dispatch obligations, handled-turn records, hook markers, and event-cache upserts absorb duplicate observable work by source event identity.

The design does not claim exactly-once transport delivery.

It provides at-least-once replay with idempotent durable consumers.

## Tests

The first nio test will seed a token, real gap, accepted and unaccepted pending rows, a completed marker, and a Sliding Sync window token, perform a strict rollback, reopen the store, and assert that only the supplied token remains.

A sibling will prove that equal and tokenless resets also clear every callback row, completed marker, unfinished walk, and Sliding Sync window token.

A nio client test will prove the operation refuses use after any sync request and updates both durable and in-memory state when idle.

The first MindRoom integration test will create a real persisted nio gap at `T2` over a valid MindRoom checkpoint `C1`, retain the accepted later event in MindRoom's obligation store, restart, and assert that startup clears the old generation and replays events from `C1` in server order.

That test will also prove that re-admission of the retained event returns the existing MindRoom obligation instead of duplicating it.

A real Sliding-to-Classic restart test will prove that a completed Sliding state event at an equal Classic token cannot suppress the replayed Classic timeline event.

A tokenless overlap test will prove that the initial server timeline replaces stale completion and sequence metadata and applies events in server order.

The replay test will use a real nio client and real continuity store while mocking only the homeserver response.

A cold-cache sibling test will assert that the rewind target is `None` and that the principal cache is rebuilt from a tokenless sync.

A room-member test will put the baseline membership only in the tokenless first-sync timeline and assert that a later profile update does not emit a join hook.

A sibling will reject the first response and prove that its tokenless replay still records a timeline-only baseline.

Existing recovery, cache-certification, dispatch-obligation, membership-hook, import-boundary, type, lint, and full test suites remain required.

## Falsification Criteria

The design is rejected if nio cannot rewind the token and marker state in one store transaction.

The design is rejected if clearing nio recovery state loses callback work that already crossed MindRoom's durable admission boundary.

The design is rejected if stale recovery generations survive the rewind and can block or reorder replay from the certified token.

The design is rejected if replay re-admission duplicates an existing MindRoom dispatch obligation.

The design is rejected if clearing Sliding Sync window tokens on a Classic rewind creates an unsafe mixed-transport state.

The design is rejected if an active task can race the startup rewind despite the operation's guard.

The design is rejected if the production change approaches PR 1783's coordination surface instead of remaining a narrow store API plus startup wiring.

## Scope

The experiment does not remove main's in-process drain-then-replay behavior after a callback failure.

The experiment does not remove the response-batch cache gate.

The experiment leaves Sliding Sync startup behavior unchanged, although a Classic rewind invalidates stored Sliding Sync window tokens in the shared nio recovery lane.

The experiment does not add live-event cache double writes, a new MindRoom journal, or restart phases.

The experiment accepts that a pre-admission event omitted from a tokenless initial sync cannot be recovered without reintroducing a cross-generation merge protocol.

The experiment does not modify PR 1783's worktree.

If the experiment passes, a follow-up may use the same atomic nio operation to simplify in-process loop-exit recovery after proving active-dispatch cancellation semantics.
