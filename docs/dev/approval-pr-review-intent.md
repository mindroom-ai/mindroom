# Approval PR Review Intent

This PR should make approval handling smaller, more explicit, and less recoverable.
The product invariant is that an approval belongs to the currently running agent turn, and once that turn is gone the approval is no longer actionable.

## Core Intent

Matrix approval cards are the only user-visible approval surface.
A live in-memory waiter is the only runtime state that can unblock a tool call.
Approving or denying an old card after the MindRoom process restarts must not affect any future tool call.
If an agent turn is cancelled, aborted, or lost during process restart, the approval should become terminal with the `expired` status.
The statuses should stay simple: `approved` means the user allowed the live tool call, `denied` means the user rejected the live tool call, and `expired` means the turn no longer exists.

## Design Goals

There should be one approval path for running tools: create a Matrix card, wait for a Matrix response, and continue only when the live waiter resolves to `approved`.
There should not be a second approval recovery path through local files, cached Matrix events, Matrix history scans, or direct event fetches.
Cached approval events may help startup cleanup find old cards to expire, but the cache must never make an approval actionable.
Startup cleanup is best effort and cosmetic from a correctness perspective.
If cleanup misses a card, that is acceptable because the old card has no live waiter and cannot approve anything.

## Cache Boundary

The cache is an index for cards and terminal edits that MindRoom itself already sent.
The cache is not an approval database.
Outbound write-through is acceptable because Matrix sync may not have cached a just-sent approval card or edit before a restart.
Fallbacks from cache lookup to homeserver history scans or ad hoc event fetches should be rejected unless there is a concrete correctness requirement that cannot be met any other way.

## Non-Goals

Do not preserve or mention legacy approval storage.
Do not rebuild pending approvals after restart.
Do not make old approval responses work after restart.
Do not add broad fallback code just to make cleanup more complete.
Do not add compatibility branches for behavior that never shipped.

## Review Checklist

Every path that continues a tool call should require a live approval waiter.
Every restart, shutdown, cancellation, or abort path should make live pending approvals terminal as `expired` when a Matrix card exists.
Every startup cleanup path should be allowed to expire cached cards, but not reconstruct pending approval state.
Every cache use should be explainable as indexing Matrix cards or edits, not as storing approval truth.
Every fallback should have a specific failure mode that makes it strictly necessary.
