# Bot Runtime Simplification Roadmap

Why the inbound-turn runtime is divided the way it is, the invariants its seams protect, and the simplifications still planned.
The AI reply lifecycle belongs to [Reply messages](reply-messages.md), and the event journal's decisions and the homeserver behaviour behind them belong to the [event journal contracts](matrix-event-journal-contracts.md); this page does not repeat them.

## Why

The runtime abstractions should be concrete, honest, and easy to trace.
Turn handling applies the general [architecture principles](../dev/2026-04-15-mindroom-architecture-boundaries.md#principles) with three runtime-specific rules:

- One inbound turn can be traced without jumping between multiple coordinators.
- The durable turn truth is singular.
- A change that moves ownership deletes the old owner instead of adding a second one.

## Target Boundaries

- `AgentBot` owns Matrix lifecycle only.
- `TurnController` owns one inbound turn from ingress to recorded outcome.
- `TurnPolicy` owns pure decision logic only: it sends nothing, runs no AI, and writes no persistence state.
- `ResponseRunner` owns response execution and lifecycle only.
- `TurnStore` owns durable turn truth.
- `DeliveryGateway` owns Matrix transport only.

The ingress-to-execution seam is one-way: ingress hands the runner immutable values, and the runner assembles the final payload itself after it takes the lifecycle lock.
A callback that runs payload building back inside `TurnController` after the lock was rejected, because data must cross the seam as values, not closures.

Edit regeneration runs on a runner-owned task and does not wait for its claim, so another reply holding the conversation never holds up the room's event lane.

## Ordering Identities

Requester identity is split into several identities because each protects a different invariant.

- **Physical sender and effective requester** differ when trusted-relay promotion attributes a turn to someone other than the account that sent it.
- **Acting requester**: an agent's or team's reply names the human or configured bot account it answered, and an entity that reply mentions authorizes and executes as that requester, while receipt lanes, coalescing, the chatter gate, self-echo drops, and speaker labels stay on the replying entity.
  A reply's final text arrives as a completed `m.replace` of its placeholder, so that edit is the journal source and the placeholder its discovery alias, which keeps later completed edits of the same reply from dispatching again.
- **Batching owner**: a batch holds only one owner's messages and ends where the effective requester or the acting author changes, because a turn runs under exactly one requester's identity and takes its origin from its latest event.

## Turn Ownership

The crash guarantee is durable callback acceptance with retryable callback execution, then durable turn ownership and terminal deduplication once the turn record exists.

A physical source event ID is an exclusive turn claim, while a discovery alias is only an advisory settlement key.
Aliases stay separate from canonical source identity so recovery can index every triggering event, such as an interactive selection or the human sources of a coalesced router relay, and later edits and redactions reach the owned prompt, without making one message look coalesced.

Approval replies and reactions can have several semantic consumers, so a durable claim selects exactly one before any side effect, and recovery routes only to the claimed consumer instead of rediscovering intent from mutable runtime state.
Each consumer owns the replay semantics of its own side effects; generic reaction hooks, for example, are at-least-once.

Interactive selections:

- Reaction and numeric-answer admission snapshot the prompt revision the projection exposes, so later edits cannot reinterpret a stored selection; unrelated Matrix origin clocks are never treated as causal order.
- Active prompts are derived by joining those immutable revisions to the visible projection, so history recovery can restore an unconsumed revision without resurrecting an answered one.
- The selecting source, not the reusable question event, is the turn's execution and deduplication identity.
- The selection's history ends at the selecting source while the question stays the reply target, so clarification in between remains available.
- An interactive reaction stays pending while its detached response owns the selection, so a restart can replay it until delivery is durable.

When the current event is in thread history, its turn's history is bounded at that event, so later entries cannot enter a backlog turn's context or displace its earlier history from the budget.

An interrupted turn stays pending, and its replay continues the reply in place.
An interrupted edit regeneration also stays pending but starts over, because a newer edit may have replaced the prompt its stopped attempt answered; a regeneration's approved resume that was cut short continues below what it showed, as an interrupted turn does.

Potentially mutating chat commands are at-most-once: the start of execution is recorded before the handler runs, and the exact result before visible delivery.
Recovery re-delivers a recorded result, while an interrupted attempt without one is not rerun and instead tells the requester its outcome is uncertain and to inspect state before retrying.
`!config set` keeps its own Matrix-backed preview, decision, execution, and result journal, because its mutation begins only after the requester reacts to the preview.

## Durable Dispatch Boundary

Nio owns receive cursors, prepared batches, provenance, encryption recipients, and room-key sharing; the [event journal contracts](matrix-event-journal-contracts.md) record why MindRoom trusts nio for these instead of re-deriving them.

### Retention and deduplication

Classic Sync tokens are opaque and may be invalidated, forcing a sync without `since` whose limited timeline backfill can redeliver an older event.
No checkpoint-relative pruning frontier can therefore preserve exact deduplication, so settled rows are kept: settling clears a row's replay payload in place, and the row goes on proving that its event already produced its one turn.

### Failure, refusal, and ignored traffic

A callback that raises or is cancelled stays pending at the head of its room lane and retries with capped backoff for as long as it fails, so a homeserver, provider, or network outage delays events instead of dropping them.
Only a callback that recognizes a deterministic refusal settles its event, by returning an outcome instead of raising.
A message or media event whose relation target the homeserver refuses to serve can never be placed in a conversation, so it settles as intentionally ignored.
A successful but empty readiness result settles as intentionally ignored instead of repeating download or transcription work forever.
Pure-policy ignores, such as unmentioned managed senders, blocked deep synthetic relays, and commands another entity owns, settle their journal rows without recording a turn, which keeps high-volume ignored traffic out of the handled-turn ledger without weakening deduplication.

### Room lanes

A global scan only discovers rooms; each room's lane runs its pending callbacks in receipt order, and a failure pauses only that room.
Revisiting a receipt at or before the room's latest admitted receipt also backs off, because immediate downstream handoffs would otherwise trap recovery in a retry loop.
A periodic liveness sweep exists because a downstream owner can vanish without a completion notification.

### Shutdown, replacement, and crash

Orderly shutdown, entity replacement, and a crash leave the same pending state, so one replay path recovers all three, and the interrupted reply stays visibly streaming until replay adopts it.
Interrupted callbacks stay pending for exact replay, including edits whose revision has not reached a final response, and an approved run cut short releases its continuation without settling its sources, unless a FINAL is already owed, so replay adopts its reply too.
Shutdown closes callback admission before withdrawing runtime capabilities, and its quiescence request stays latched across supervisor retries and late startup completion, so transport lifecycle notifications cannot reopen admission.
A cleanup that outlasts its budget keeps its owners, and the Matrix client and journal stay open until it finishes.
Shutdown invalidates membership readiness only after owners finish, so readiness loss cannot settle an accepted source as revoked authorization.
Replay does not wait for a later sync token or Sliding Sync position: recovered callbacks may rely on the room ID, but cached membership and state are best-effort.
Recovery intent travels with queued ingress, so lane and coalescing workers cannot turn a recovered turn whose router target is temporarily unavailable into a terminal fallback response.

### Delivery and membership

Only the durable `INITIAL` outbox row may put a placeholder in the room, so a send whose outcome Matrix never confirmed is resolved by resending that row under its own transaction ID, not by a second, unowned send.
Every outbox delivery freezes the room membership epoch that authorized it, except an approval card whose delivery stages atomically transfer their already-decided terminal cleanup to a successor membership.

### Invites

Invites have no stable event ID for a journal row, so the pending room and inviter are persisted before background handling starts.
That record only wakes unfinished work and never grants inviter authority: routers and agents re-read Nio's current inviter after the join fence is durable and immediately before requesting the join.

### Best-effort inputs

- An event that never reached either durable owner and later falls outside Matrix replay is lost; this is the explicit pre-admission loss boundary.
- Live `room-member-joined` hooks are at-least-once, because they are emitted before durable settlement.
- Call-manager membership and unknown-event callbacks only wake reconciliation, because their standalone payloads cannot replay the current room call state.
- Auxiliary room callbacks are gated by the provenance of their own event, so one live event cannot license unrelated historical call-state mutations.
- To-device call inputs and desktop pairing receivers are best-effort and have no journal ownership, because they lack a stable replayable timeline-event identity.

### Inspecting and repairing records

The journal is `tracking/event_journal.db` under the storage directory, or the configured PostgreSQL database.
`journal_events` grows by one row per admitted event and is never pruned, for the deduplication reason above.
Inspect its growth with `SELECT state, COUNT(*) FROM journal_events GROUP BY state;`.
Do not delete settled rows unless duplicate callback execution after a later Matrix redelivery is acceptable.
Recovery logs and skips a pending row it cannot decode and keeps it pending for repair, so other rows continue.
A row that decodes but whose payload is corrupt or does not match its stored kind is logged as corrupt and settled, which clears its replay payload.
To repair a retained unreadable pending row, stop MindRoom, back up the affected database, and restore a known-good copy before restarting.
Deleting such a row is a last resort that accepts losing that callback.
Repair cannot recover payloads that settlement already cleared.

## Voice Readiness

A voice message's batching scope and its echo thread are distinct: a root audio message can join the room's batch while its echo starts a thread rooted at it, and the final coalesced reply target follows the current dispatch policy.

## Handled-Turn Ledger

- One runtime process owns each agent's ledger and its semantic ordering; nothing defines cross-process precedence, and the database does not coordinate independent runtimes pointed at the same storage path.
- Retention keeps replayable incomplete turns, every group an unsettled journal row references, and every turn record carrying a redaction tombstone, because history cleanup derives from those tombstones once a departure dropped the journal's tombstone or for history older than the journal.

## Redaction

The user-visible history behavior is on [Conversation history](../configuration/history.md#compacted-history-and-redaction).

- Nothing durable records that history cleanup is owed: every response derives it from the tombstones, so a crash between tombstone and cleanup, or a redacted event no turn owns, needs no recovery path.
- Approval creation and source-redaction admission serialize, so an approval cannot acquire a source that a deletion already settled.
- Redacted replay can stay in local session storage until its history scope next serves a response, because approval continuations, manual compaction, and voice-call turns do not run the check.
- Semantic memory backends such as Mem0 keep their own lifecycle and are not changed by this cleanup.

## Approval Event Client Contract

Clients consume approval events under these rules, which this repository's code cannot enforce:

- Clients associate approval history with the paused response through `response_event_id`, never by tool names or argument similarity.
- Clients render compact history from the original event plus its latest same-sender terminal edit, taking complete argument evidence from the original, inline or as its Matrix attachment, because terminal edits omit it.
- Only the originating grant card carries mutable auto-approval controls, and revocation or expiry never changes the historical authorization of calls the grant covered.
- After a device change, a missing terminal receipt may be republished because it offers no action, so clients deduplicate receipts by exact approval and tool-call identity, scoped to the event sender.

## Simplifications Still Planned

- `TurnController` is the only normal-turn owner but is still too large: interactive-selection execution and response-action assembly should move out of it.
- `ResponseRunner` still owns too much execution detail: its team turn driver, interrupted persistence, inbox tracking, and enrichment helpers should become their own modules.
- `IngressHookRunner` is a thin hook adapter with a vague name; it may stay a helper but must not grow into another top-level orchestration object.
- Only after those steps, revisit `MessageTarget`, which combines conversation identity and delivery placement, and split them only if the runtime still needs it.
