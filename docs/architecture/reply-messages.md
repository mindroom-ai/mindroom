# Reply Messages

## Purpose

Every AI reply MindRoom shows in Matrix is owned by a durable reply record.
The record, not the Matrix timeline and not the process that happens to be streaming, decides what the reply shows, which work runs for it, and how it ends after a Stop, a restart, an edit, an approval, a deletion, or a departure.
This page is for contributors: it names the records, the rules that change them, and the invariants tests pin.

## Modules

| Module | Owns |
|---|---|
| `reply_lifecycle.py` | Pure rules: every event that changes a reply, as a function from the current records to a `Transition` with an outcome, changed records, effects, and at most one durable row. |
| `reply_presentation.py` | The presentation model (segments, notes, team state), its JSON codec, `render_body` for the body and trace a reply shows, and `render`, which adds the terminal wire status of a terminal row or owed note. |
| `reply_scope.py` | `ReplyRuntime` (one per bot instance) and `SpanHandle`: claims, span exits, write-ahead, owed writes, and the span slot child tasks share. |
| `event_journal/reply_messages.py`, `event_journal/reply_spans.py` | Persistence of replies and spans. |
| `event_journal/replies.py` | `ReplyStore`: each rule applied inside one database transaction, with its in-transaction effects. |
| `event_journal/legacy_reply_messages.py`, `legacy_reply_messages.py` | One-time adoption of replies main left in flight, and the post-sync reads of what they showed. |
| `stop.py` | `SpanRegistry`: the task and Agno run of each span this instance executes. |
| `delivery_gateway.py` | Rendering and sending reply rows, owed notes, Stop buttons, and redactions. |

## Records

A reply (`reply_messages`) is one visible message in one room, with its event id once Matrix created it.
Its state is `active`, `paused` (waiting for an approval decision), or terminal: `completed`, `cancelled`, `failed`, or `gone` (removed from the room, or never shown).
It holds the canonical presentation, the possibly-shown presentation of its latest write with that write's sequence, the confirmed sequence Matrix acknowledged, a frozen display when a final transform reshaped the answer, the Stop it recorded and whether it was applied, the Stop button's event id, redactions it still owes, a note it owes but has not enqueued, and the approval that holds it.

A span (`reply_spans`, `reply_span_sources`) is one execution that writes a reply: `turn`, `replay`, `regeneration`, or `approval_resume`.
It records its delivery id, the journal sources it answers, the bot generation that claimed it, the reply's write sequence at claim, a rollback snapshot for regenerations, and its outcome once it ends: `completed`, `cancelled`, `failed`, `paused`, `released`, `superseded`, `lost`, `suppressed`, or `restored` (a regeneration that put the old answer back).
At most one span is current per reply.

Reply rows are ordinary `matrix_delivery_outbox` rows with `reply_id`, `span_id`, and `reply_sequence`; the stage is `initial` (the create), `final` (the span's terminal write), or `edit`.
`pending_reply_stops` holds a Stop on an event no reply is bound to yet.
`reply_principal_generations` holds the bot instance that owns each principal's replies.
`reply_legacy_classifications` marks principals whose earlier-release replies were adopted.

## Rules and outcomes

A rule returns one outcome: `applied`, `stale` (the span is no longer current), `duplicate` (already true), `deferred` (earlier writes are unresolved), `recompute` (a Stop, deletion, or departure committed after the caller rendered), or `stopped`.
A rule that can never apply raises `InvalidTransitionError`; callers treat it as a bug and settle the sources with a dispatch error instead of retrying.
Effects run in the rule's transaction (`SettleSources`, `FenceApproval`, and `TransferStop`, which writes the turn record's Stop) or after it commits (`CancelSpan`, `WakeApproval`, and the turn ledger's cache learning a transferred Stop); post-commit effects are best effort because the records already say what must happen.

Callers render a payload from the reply's revision before the transaction; a rule that would choose different content returns `recompute`, writes nothing, and the caller renders again.

## Claims

A claim runs under the conversation lock after the turn's first source gate and finds the reply through the span's sources, its bound event, or an interactive selection's acknowledgement:

- No reply: create one in `active` with a `turn` span, or a historical reply with a `regeneration` span when an edit names an answer the records never saw.
- An edit whose driving edit differs from the last span's: a `regeneration` span with a rollback snapshot; a paused reply's approval is fenced `superseded` and cleaned up outside the conversation lock.
- An edit the last span already answered (a sync restart's retry): `duplicate`, nothing runs.
- A last span ended `released`, `lost`, or `superseded`: a `replay`, or the same regeneration re-run.
- Unresolved durable writes, an owed note, or a pending legacy read: `deferred`; the resolution retries the sources.

## Writes

The create, each pause, and every terminal update are durable rows, recorded with their reply transition before they are sent and sent in reply-sequence order by one sending owner per reply.
Each progress edit is a direct edit recorded first by `write_ahead`, which raises the confirmed sequence of the previous edit.
A note a rule decides without a payload (an ownerless Stop, a restart, a settlement without an answer) is an owed write that `settle_reply_debt` renders and enqueues.
Recovery renders from the possibly-shown presentation, so a restart continues below what the reply may already show.

## Stop

A Stop on an event commits on the reply in the transaction that records it on the turn.
A current span this instance runs is cancelled through the `SpanRegistry`; a span nobody runs ends at once with the cancel note owed.
A Stop on an event whose create is still unacknowledged waits in `pending_reply_stops` and applies when the create is acknowledged.
A Stop after the terminal row is satisfied by it.
The Stop button belongs to the reply: it is recorded when sent and redacted when the reply leaves `active`, except while a span waits in place for an approval.

## Approvals

A pause, the continuation it creates or advances, and the pause row commit together.
A resume claims the continuation and an `approval_resume` span of the paused reply together, and continues the paused answer segment.
A failure or Stop fences the continuation; its settlement writes the note and finishes the reply in the continuation's finish.
A response-local CLI approval waits in place: its span stays current through the wait.

## Lifetime

Each bot instance writes a fresh generation for its principal at start, then adopts replies an earlier release left once, then ends what older instances left (`owner_lost`): orphaned spans end `lost`, replies whose sources settled fail with the restart note (or end `gone` when they never wrote anything), and replies whose sources are pending wait for their replay.
A membership departure ends the room's replies `gone` inside the departure fence and cancels their spans afterwards.
A replay that a newer message from the same requester supersedes settles its sources with its reply, unless the reply still owes Matrix a write.
A bot instance that another took over writes nothing more: its claims and every write its running spans make, approval resumes included, are refused against the principal's persisted generation; a resume it left stays open to the owner's approval recovery, which ends it.
A replay that ingress settles without a turn, such as one whose requester lost access, ends its reply in that commit with the interrupted note, or removes a reply that showed only its placeholder.
Deleting every logical source of a reply's current work ends it `gone` in the tombstone's commit; the bot then cancels its running span and redacts what it showed, while a reply an approval holds and a written answer are kept, including the answer an edit was regenerating before the regeneration showed anything.
An entity removed from the configuration has no bot: its open replies end `failed` without Matrix writes.
The handled-turn retention pass deletes finished replies that owe nothing, with their spans, 30 days after their last change, the age at which the ledger forgets their turns.

## Invariants

- I1. At most one current span; only span-authored events from it change the canonical answer.
- I2. Durable writes of a reply are sent in sequence by its one sending owner; an attempted write is resolved before a later one is sent.
- I3. A terminal reply changes only by a regeneration claim, a failed terminal write, a completed redaction, or a late create bound for redaction.
- I4. A recorded Stop with no newer edit is eventually applied, unless a terminal row was enqueued before it.
- I5. Every non-terminal reply has a durable path to progress: pending span sources, an approval continuation, or `owner_lost`.
- I6. A span's sources settle with its terminal transition, except spans that hand them to a continuation, a retry, or a replay.
- I7. Every reply write is recorded before it is sent.
- I8. A Stop button is redacted when its reply leaves `active`, except while its span waits in place.

`tests/test_reply_lifecycle_fuzz.py` checks these over random interleavings of claims, writes, acknowledgements, Stops, restarts, regenerations, approvals, deletions, departures, supersessions, and dropped replays.
