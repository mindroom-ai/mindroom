# Matrix Recovery Single-Checkpoint Design

## Status

This document defines an isolated cross-repository experiment approved and validated locally on 2026-08-03.

The experiment starts from MindRoom `a5a6a338b` and mindroom-nio `bac260148`.

It replaces the proposed PR 1783 restart coordinator rather than stacking another fix onto it.

The development integration branch resolves nio from `fix/single-checkpoint-startup-rewind` and locks its exact commit.

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

When the nio token differs from that checkpoint, or no checkpoint is trusted, MindRoom asks nio to atomically rewind its transport cursor to that checkpoint.

The rewind preserves every unsettled generation because a pending row may be the only durable copy of an event that has not crossed MindRoom's admission boundary.

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

The store transaction will replace or delete the stored sync token, clear completed generation-zero markers, and clear invalidated Sliding Sync window tokens.

It will retain all generation-positive recovery gaps and pending callback rows, including rows whose admission has not completed.

The client operation will reload that retained recovery state, clear the matching in-memory window state, install the supplied token as `loaded_sync_token`, and clear `next_batch` so the first equal-token response is still applied.

The operation will reject use after any sync request has started or while response execution or recovery dispatch tasks are active.

The operation is intentionally a startup boundary rather than a general callback-cancellation API.

## MindRoom Startup Flow

MindRoom first initializes the event cache and loads the checkpoint whose cache generation still matches.

If no checkpoint validates, the authoritative token is `None` and the principal cache is purged as on main.

In Classic Sync mode, MindRoom invokes nio's atomic startup rewind before registering callbacks or starting sync when the loaded nio token differs from the authoritative token or the authoritative token is absent.

MindRoom runs that startup-only client mutation on the owning event-loop thread so cancellation cannot leave an off-loop rewind mutating nio or its Peewee-bound store after startup has exited.

The tokenless rewind clears completed markers even when both stored tokens are absent, so a fresh baseline cannot inherit stale suppression state.

An equal non-null token is left untouched so nio can drain any retained admission work at or below the certified checkpoint.

MindRoom then starts Classic Sync from the authoritative token with the existing first-response `full_state` behavior.

The replayed response must pass the existing response-batch cache gate and pre-certification side effects before a new checkpoint is written.

An `M_UNKNOWN_POS` rejection continues to clear MindRoom trust and fall back to a tokenless rebuild.

## Development Branch Integration

The MindRoom recovery branch is stacked on the independent tokenless-membership baseline branch.

Its uv source configuration points `mindroom-nio` at the named nio development branch.

The lockfile records the resolved nio commit so tests and reviewers use immutable code even if the branch later advances.

No development branch is merged, tagged, or released as part of this validation.

## Room-Member Baseline

A tokenless first sync describes state at the start of its timeline even when a homeserver places a membership event only in the timeline.

The tokenless baseline will therefore record eligible membership events from both the state block and the timeline before live hooks are armed.

That baseline remains pending across response attempts that reset the cursor before certification.

A restored-token first sync remains a catch-up stream and may emit unseen timeline joins.

This distinction fixes the false onboarding case without a lifecycle phase machine.

## Crash Semantics

A crash before nio's rewind transaction leaves the old mismatch, so the next startup tries the same rewind again.

A crash after the rewind transaction leaves nio durably aligned to the MindRoom checkpoint while retaining unsettled callback work, so the next startup reuses that checkpoint.

A crash during the replay leaves nio advanced again while MindRoom still holds the old checkpoint, so the following startup rewinds the cursor and replays the window again while retaining unsettled rows.

Dispatch obligations, handled-turn records, hook markers, and event-cache upserts absorb duplicate observable work by source event identity.

The design does not claim exactly-once transport delivery.

It provides at-least-once replay with idempotent durable consumers.

## Tests

The first nio test will seed a token, real gap, accepted and unaccepted pending rows, a completed marker, and a Sliding Sync window token, perform the rewind, reopen the store, and assert that the supplied token and every generation-positive row remain while markers and window tokens are gone.

A nio client test will prove the operation refuses use after any sync request and updates both durable and in-memory state when idle.

The first MindRoom integration test will create a real persisted nio gap at `T2` over a valid MindRoom checkpoint `C1`, restart, and assert that startup retains the old generation but requests replay from `C1` without first requesting `T2`.

A sibling will prove that an unadmitted row at an equal certified token survives startup and is eventually admitted.

The replay test will use a real nio client and real continuity store while mocking only the homeserver response.

A cold-cache sibling test will assert that the rewind target is `None` and that the principal cache is rebuilt from a tokenless sync.

A room-member test will put the baseline membership only in the tokenless first-sync timeline and assert that a later profile update does not emit a join hook.

A sibling will reject the first response and prove that its tokenless replay still records a timeline-only baseline.

Existing recovery, cache-certification, dispatch-obligation, membership-hook, import-boundary, type, lint, and full test suites remain required.

## Falsification Criteria

The design is rejected if nio cannot rewind the token and marker state in one store transaction.

The design is rejected if replay from the certified token loses an event that nio's advanced window could have recovered.

The design is rejected if preserving an advanced recovery generation can block or corrupt replay from the certified token.

The design is rejected if any generation-positive pending row is deleted before its callback work completes.

The design is rejected if clearing Sliding Sync window tokens on a Classic rewind creates an unsafe mixed-transport state.

The design is rejected if an active task can race the startup rewind despite the operation's guard.

The design is rejected if the production change approaches PR 1783's coordination surface instead of remaining a narrow store API plus startup wiring.

## Scope

The experiment does not remove main's in-process drain-then-replay behavior after a callback failure.

The experiment does not remove the response-batch cache gate.

The experiment leaves Sliding Sync startup behavior unchanged, although a Classic rewind invalidates stored Sliding Sync window tokens in the shared nio recovery lane.

The experiment does not add live-event cache double writes, a new MindRoom journal, or restart phases.

The experiment does not modify PR 1783's worktree.

If the experiment passes, a follow-up may use the same atomic nio operation to simplify in-process loop-exit recovery after proving active-dispatch cancellation semantics.
