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
| `event_journal/legacy_reply_messages.py`, `legacy_reply_messages.py` | One-time adoption of replies an earlier release left in flight, and the post-sync reads of what they showed. |
| `stop.py` | `SpanRegistry`: the task and Agno run of each span this instance executes. |
| `delivery_gateway.py` | Rendering and sending reply rows, owed notes, Stop buttons, and redactions. |

## Ownership

Every fact about an AI reply has one owner and one writer; other stores hold only what is theirs.
`SpanHandle.reply` and the turn ledger's in-memory map are caches of their owners, never separately written state.

| Store | Owns |
|---|---|
| Reply records (`reply_messages`, `reply_spans`, `reply_span_sources`) | Everything about an AI reply: its event, state, presentation and tool-call visibility, the Stop and whether it applied, the edit order a regeneration answers, each span's sources and their settlement, redactions including deleted sources, the bot instance and outcome of the span that claims an approval, and a regeneration's selected edit. |
| Turn ledger (`turn_records`) | User-message and turn facts: sources, aliases, prompts, revisions, tombstones, requester, history scope, conversation target, voice and command checkpoints, `completed` ("this agent answered this message", agent-scoped across re-logins), and `response_event_id` for turns that are not AI replies, such as commands, rejections, router notices, and a dispatch failure's notice sent before any reply existed. |
| Journal | Whether each event is pending or settled: the work queue of one Matrix identity. |
| Agent history (Agno session runs) | The conversation the model sees, with each run's sources and the event that shows its answer, which outlives the reply records' retention: an edit to an older turn finds the answer it regenerates there and nowhere else. |
| Outbox (`matrix_delivery_outbox`) | Transport for every Matrix write: key, room, thread, membership epoch, transaction id, frozen payload, continuation segments, edit target, attempt, device, acknowledgement, permanent failure, fence, and for a reply row its owner (`reply_id`, `span_id`, `reply_sequence`), with no reply meaning. |
| Approval run (`approval_continuations`, calls, cards, grants) | Consent and the Agno payload: the approval id, the span whose pause created it (`span_id`), the span that claims it (`claim_span_id`), generation, calls and decisions, publication lease, `waiting`, `ready`, or `failing`, failure text, and the run snapshot. Its room, thread, event, entity, held sources, visibility, and selected edit are read from that paused span and its reply, and the hold on that reply is read from it. |

## Records

A reply (`reply_messages`) is one visible message in one room, with its event id once Matrix created it.
Its state is `active`, `paused` (waiting for an approval decision), or terminal: `completed`, `cancelled`, `failed`, or `gone` (removed from the room, or never shown).
It holds the canonical presentation, the possibly-shown presentation of its latest write with that write's sequence, the confirmed sequence Matrix acknowledged, a frozen display when a final transform reshaped the answer, the Stop it recorded and whether it was applied, the Stop button's event id, redactions it still owes, and a note it owes but has not enqueued.
The approval that holds it is read with it from the continuations and never written to it.

A span (`reply_spans`, `reply_span_sources`) is one execution that writes a reply: `turn`, `replay`, `regeneration`, or `approval_resume`.
It records its delivery id, the journal sources it answers, the bot generation that claimed it, the reply's write sequence at claim, a rollback snapshot for regenerations, and its outcome once it ends: `completed`, `cancelled`, `failed`, `paused`, `released`, `superseded`, `lost`, `suppressed`, or `restored` (a regeneration that put the old answer back).
At most one span is current per reply.

Reply rows are ordinary `matrix_delivery_outbox` rows with `reply_id`, `span_id`, and `reply_sequence`; the stage is `initial` (the create), `final` (the span's terminal write), or `edit`.
`pending_reply_stops` holds a Stop on an event no reply is bound to yet.
`reply_principal_generations` holds the bot instance that owns each principal's replies.
`reply_legacy_classifications` marks principals whose earlier-release replies were adopted.
`reply_deletion_endings` holds the replies a source deletion ended, with the span it cancelled, until the bot stops that span and delivers what the reply owes.

## Rules and outcomes

A rule returns one outcome: `applied`, `stale` (the span is no longer current), `duplicate` (already true), `deferred` (earlier writes are unresolved), `recompute` (a Stop, deletion, or departure committed after the caller rendered), or `stopped`.
A rule that can never apply raises `InvalidTransitionError`; callers treat it as a bug and settle the sources with a dispatch error instead of retrying.
Effects run in the rule's transaction (`SettleSources`, which settles the span's journal sources and marks the turn they index answered when the reply answered them, and `FenceApproval`) or after it commits (`CancelSpan`, `WakeApproval`, and the turn ledger's cache learning the answered turn); post-commit effects are best effort because the records already say what must happen.

Callers render a payload from the reply's revision before the transaction; a rule that would choose different content returns `recompute`, writes nothing, and the caller renders again.

## Claims

A claim runs under the conversation lock after the turn's first source gate and finds the reply through the span's sources, its bound event, or an interactive selection's acknowledgement:

- No reply: create one in `active` with a `turn` span; a regeneration always finds one, since the edit regenerator adopts an answer the records never saw as a `completed` reply before it prunes the history that names it.
- An edit whose driving edit differs from the last span's: a `regeneration` span with a rollback snapshot, or the rollback of the regeneration it replaces when that one never answered; an approval that paused the reply with no span running for it is fenced `superseded` and cleaned up outside the conversation lock.
- An edit the last span already answered (a sync restart's retry): `duplicate`, nothing runs.
- A last span ended `released`, `lost`, or `superseded`: a `replay`, or the same regeneration re-run.
- Unresolved durable writes, an owed note, a pending legacy read, or for an edit an approval that holds the reply while or after a span runs for it: `deferred`; the resolution, or that approval's finish or release, retries the sources.

## Writes

The create, each pause, and every terminal update are durable rows, recorded with their reply transition before they are sent and sent in reply-sequence order by one sending owner per reply.
Each progress edit is a direct edit recorded first by `write_ahead`, which raises the confirmed sequence of the previous edit.
A note a rule decides without a payload (an ownerless Stop, a restart, a settlement without an answer) is an owed write that `settle_reply_debt` renders and enqueues.
Recovery renders from the possibly-shown presentation, so a restart continues below what the reply may already show.

## Stop

A Stop on an event commits on the reply it names; the turn ledger does not hold Stops.
A current span this instance runs is cancelled through the `SpanRegistry`; a span nobody runs ends at once with the cancel note owed.
A Stop on an event whose create is still unacknowledged waits in `pending_reply_stops` and applies when the create is acknowledged.
A Stop after the terminal row is satisfied by it.
The Stop button belongs to the reply: it is recorded when sent and redacted when the reply leaves `active`, except while a span waits in place for an approval.

## Approvals

A pause, the continuation it creates or advances, and the pause row commit together; the continuation names the paused span and holds that span's pending sources.
A resume claims an `approval_resume` span of the paused reply and names it as the continuation's claim in one transaction, and continues the paused answer segment.
A claimed continuation stays `ready` and reads as claimed by the bot instance of the span its claim names; a further pause clears the claim.
A failure or Stop fences the continuation; its settlement writes the note and finishes the reply in the continuation's finish.
A response-local CLI approval waits in place: its span stays current through the wait, and once approved it runs for that approval as a resume does, so the continuation's finish or failure settles the sources and ends the reply.

A reply is held by the continuation that names one of its spans and is not fenced `superseded`; the store derives the hold when it loads the reply, so no rule writes it.
While held, a Stop fences the approval, a deletion keeps the reply, retention keeps it, and a span that runs for the approval leaves the reply's end to the approval's settlement.
A continuation finishes once a FINAL at its first source was acknowledged or refused for good, or once an edit superseded it.
Its finish, release, or discard applies the reply rule while the continuation still exists and deletes the continuation in the same transaction.

## Abandoned regenerations

A regeneration abandoned without an answer or a retry restores the answer it was replacing only when it recorded no write Matrix may show, counting unacknowledged writes.
A FINAL Matrix refused for good counts only the writes before it.
A Stop or a deletion restores only a finished answer, since restoring unfinished work would run its retry after them.
A restored paused answer ends `failed` with the interrupted note, since consent is never restored.
Otherwise the exit's terminal row, an owed note, or an owed redaction brings Matrix to match the records; a suppressed answer keeps what it showed and owes the interrupted note, or the cancelled note after a Stop.

## Lifetime

Each bot instance writes a fresh generation for its principal at start, then adopts replies an earlier release left once, then ends what older instances left (`owner_lost`): orphaned spans end `lost`, replies whose sources settled fail with the restart note (or end `gone` when they never wrote anything), and replies whose sources are pending wait for their replay.
A membership departure ends the room's replies `gone` inside the departure fence and cancels their spans afterwards.
A replay that a newer message from the same requester supersedes settles its sources with its reply, unless the reply still owes Matrix a write.
A bot instance that another took over writes nothing more: its claims and every write its running spans make, approval resumes included, are refused against the principal's persisted generation; a resume it left stays open to the owner's approval recovery, which ends it.
A replay that ingress settles without a turn, such as one whose requester lost access, ends its reply in that commit with the interrupted note, or removes a reply that showed only its placeholder.
Deleting every logical source of a reply's current work ends it `gone` in the tombstone's commit, which records the reply and the span it cancelled; the bot then cancels exactly that span and redacts what the reply showed, while a reply an approval holds and a written answer are kept, including the finished answer an edit was regenerating before the regeneration showed anything.
An entity removed from the configuration has no bot: its open replies end `failed` without Matrix writes, and their sources settle unanswered unless an approval holds them.
The handled-turn retention pass deletes finished replies that owe nothing, with their spans, 30 days after their last change, the age at which the ledger forgets their turns.

## Invariants

- I1. At most one current span; only span-authored events from it change the canonical answer.
- I2. Durable writes of a reply are sent in sequence by its one sending owner; an attempted write is resolved before a later one is sent.
- I3. A terminal reply's state and answer change only by a regeneration claim, a failed terminal write, a completed redaction, a late create bound for redaction, or the note it owes.
- I4. A recorded Stop with no newer edit is eventually applied, unless a terminal row was enqueued before it.
- I5. Every non-terminal reply has a durable path to progress: pending span sources, an approval continuation, or `owner_lost`.
- I6. A span's sources settle with its terminal transition, except spans that hand them to a continuation, a retry, or a replay.
- I7. Every reply write is recorded before it is sent.
- I8. A Stop button is redacted when its reply leaves `active`, except while its span waits in place.
- I-S1. Every reply fact in the ownership table is read from reply records or a cache of them; no other store writes it.
- I-S2. A reply settles its journal sources only through `SettleSources`, which marks the turn they index answered in the same transaction when the reply answered them, including a finished approval's paused span; deleted sources, sources that became terminal without an answer, and those of a removed entity or of an approval whose owner is gone settle unanswered.
  Sources settled outside a reply, by an ingress decision that drops a waiting replay or by a room departure, leave their turn unanswered and end the reply that waited on them.
- I-S3. The outbox commits nothing outside transport for a row with `reply_id`.
- I-S4. A continuation names its reply through its paused span and holds no reply or source fact of its own.
- I9. Every unfinished continuation has an owner that moves it: draining every owner leaves no continuation, owed write, or deferred claim.
- I10. Retention never forgets a reply whose spans a continuation names.
- I11. An abandoned regeneration that recorded no write Matrix may show restores its rollback, as the rules in Abandoned regenerations allow.
- I12. A regeneration that recorded a write Matrix may show never restores its rollback.
- I13. At most one continuation that is not superseded names a reply's spans.
- I14. An abandoned regeneration that recorded a write Matrix may show leaves a terminal row, an owed note, or an owed redaction; a create still in flight is redacted once acknowledged.

`tests/test_reply_lifecycle_fuzz.py` checks I1 and I4 through I14 over random interleavings of claims, writes, acknowledgements, Stops, restarts, regenerations, approval decisions, resumes and recoveries, deletions, departures, entity removal, retention, supersessions, and dropped replays.
It keeps continuations as rows the hold is derived from, defers edit claims through the shared blocking predicate, and ends every run by draining every owner.
The unit tests cover stale and retired spans and the remaining rules.
