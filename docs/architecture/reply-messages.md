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
| `stop.py` | `SpanRegistry`: the task and Agno run of each span this instance executes. |
| `delivery_gateway.py` | Rendering and sending reply rows, owed notes, Stop buttons, and redactions. |

## Ownership

Every fact about an AI reply has one owner and one writer; other stores hold only what is theirs.
`SpanHandle.reply` and the turn ledger's in-memory map are caches of their owners, never separately written state.

| Store | Owns |
|---|---|
| Reply records (`reply_messages`, `reply_spans`, `reply_span_sources`, `reply_tool_calls`) | Everything about an AI reply: its event, state, presentation and tool-call visibility, the Stop and whether it applied, each span's sources and their settlement, each tool call a span made, redactions including deleted sources, and the bot instance and outcome of the span that claims an approval. |
| Turn ledger (`turn_records`) | User-message and turn facts: sources, aliases, prompts and revisions (an edit's once its regeneration claims the reply), tombstones, requester, history scope, conversation target, voice and command checkpoints, `completed` ("this agent answered this message", agent-scoped across re-logins), and `response_event_id` for turns that are not AI replies, such as commands, rejections, router notices, and a dispatch failure's notice sent before any reply existed. |
| Journal | Whether each event is pending or settled: the work queue of one Matrix identity. |
| Agent history (Agno session runs) | The conversation the model sees, with each run's sources and the event that shows its answer; a regeneration prunes the run it replaces once it claims the reply. |
| Outbox (`matrix_delivery_outbox`) | Transport for every Matrix write: key, room, thread, membership epoch, transaction id, frozen payload, continuation segments, edit target, attempt, device, acknowledgement, permanent failure, fence, and for a reply row its owner (`reply_id`, `span_id`, `reply_sequence`), with no reply meaning. |
| Approval run (`approval_continuations`, calls, cards, grants) | Consent and the Agno payload: the approval id, the span whose pause created it (`span_id`), the span that claims it (`claim_span_id`), generation, calls and decisions, publication lease, `waiting`, `ready`, or `failing`, failure text, and the run snapshot. Its room, thread, event, entity, held sources, and visibility are read from that paused span and its reply, and the hold on that reply is read from it. |

## Records

A reply (`reply_messages`) is one visible message in one room, with its event id once Matrix created it.
Its state is `active`, `paused` (waiting for an approval decision), or terminal: `completed`, `cancelled`, `failed`, or `gone` (removed from the room, or never shown).
It holds the canonical presentation, the possibly-shown presentation of its latest write with that write's sequence (a final transform's reshaped display, when one reshaped the answer), the confirmed sequence Matrix acknowledged, the Stop it recorded and whether it was applied, the Stop button's event id, redactions it still owes, and a note it owes but has not enqueued.
The approval that holds it is read with it from the continuations and never written to it.

A span (`reply_spans`, `reply_span_sources`) is one execution that writes a reply: `turn`, `replay`, `regeneration`, or `approval_resume`.
It records its delivery id, the journal sources it answers, the bot generation that claimed it, the reply's write sequence at claim, a rollback snapshot for regenerations, and its outcome once it ends: `completed`, `cancelled`, `failed`, `paused`, `released`, `superseded`, `lost`, `suppressed`, or `restored` (a regeneration that put the old answer back).
At most one span is current per reply.

A tool call (`reply_tool_calls`) is recorded on the span the calling task runs, as started before the tool runs and as completed with its result once it returns, whether or not the reply shows tool calls.
The start is recorded only while that span runs as its reply's current span and no Stop waits for it; otherwise the tool does not run, so a Stop committed before the start stops even a synchronous tool whose worker thread the Stop's cancellation cannot reach.
A call cut short stays started, since it may have taken effect.

Reply rows are ordinary `matrix_delivery_outbox` rows with `reply_id`, `span_id`, and `reply_sequence`; the stage is `initial` (the create), `final` (the span's terminal write), or `edit`.
`pending_reply_stops` holds a Stop on an event no reply is bound to yet.
`reply_principal_generations` holds the bot instance that owns each principal's replies.
`reply_deletion_endings` holds the replies a source deletion ended, with the span it cancelled, until the bot stops that span and delivers what the reply owes.

## Rules and outcomes

A rule returns one outcome: `applied`, `stale` (the span is no longer current), `duplicate` (already true), `deferred` (earlier writes are unresolved), `recompute` (a Stop, deletion, or departure committed after the caller rendered), or `stopped`.
A rule that meets a state it does not model never raises: on a reply that has not ended it ends the reply `failed` with the error note owed, cancels its current span, and settles the sources or fails the approval that holds them; a reply that already ended keeps its end, a stray second create is redacted, and a refused row of an older span is stale; the store logs `reply_unmodeled` with the reason, and a claim reports such a refusal as nothing to run.
Effects run in the rule's transaction (`SettleSources`, which settles the span's journal sources and marks the turn they index answered when the reply answered them, and `FenceApproval`) or after it commits (`CancelSpan`, `WakeApproval`, the retry of claims that waited for an approval to end, and the turn ledger's cache learning the answered turn); post-commit effects are best effort because the records already say what must happen.

Callers render a payload from the reply's revision before the transaction; a rule that would choose different content returns `recompute`, writes nothing, and the caller renders again.

## Claims

A claim runs under the conversation lock after the turn's first source gate and finds the reply through the span's sources, its bound event, or an interactive selection's acknowledgement:

- No reply: create one in `active` with a `turn` span; a regeneration never creates one.
- A new edit of an unheld reply that is not `gone`: a `regeneration` span with a rollback snapshot of a finished answer, or the rollback of the regeneration it re-runs when that one never wrote; a reply an approval holds without a Stop or end recorded, or one that is `gone`, regenerates nothing (`duplicate`).
- An edit the last span already answered (a sync restart's retry): `duplicate`, nothing runs.
- A last span ended `released` or `lost`: a `replay`, or the same regeneration re-run.
- A last span ended `superseded`: a span of the same kind.
- Unresolved durable writes, an owed note, or an approval that still holds a reply after its Stop or end: `deferred`; their resolution, or the approval's end, retries the sources.

The edit regenerator decides which edits reach a claim: only an edit of the latest message of its conversation, with no later message from someone other than an agent, whose reply showed something and is not `gone`.
It records a Stop on that reply first when a span still runs for it or an approval holds it, and its claim then waits for the conversation lock that span holds and for the approval the Stop fenced to end.
It hands the edit to a regeneration on a runner-owned task without waiting for its claim, because the claim waits for the conversation, which another reply of the agent may hold for as long as that reply runs; once the claim succeeds the regeneration records the edit's text and revision in the turn ledger and prunes the history run it replaces, and a regeneration that ends without a span owning the edit settles the edit itself, unless its claim was deferred.
An edit of a message still waiting in its coalescing queue changes that message's text instead and never reaches the regenerator.

## Writes

The create, each pause, and every terminal update are durable rows, recorded with their reply transition before they are sent and sent in reply-sequence order by one sending owner per reply.
Each progress edit is a direct edit recorded first by `write_ahead`, which raises the confirmed sequence of the previous edit.
A note a rule decides without a payload (an ownerless Stop, a restart, a settlement without an answer) is an owed write that `settle_reply_debt` renders and enqueues.
Recovery renders from the possibly-shown presentation, so a restart continues below what the reply may already show.
A replay, or a regeneration retried after one, tells the model which tool calls the attempts it takes over recorded, those spans that ended `lost`, `released`, `paused`, or `superseded` since the reply's last answer, so it does not repeat a finished call.

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

A reply is held by the continuation that names one of its spans; the store derives the hold when it loads the reply, so no rule writes it.
While held, a Stop or an edit's Stop fences the approval, a deletion or sources that settle without an answer keep the reply, retention keeps it, and a span that runs for the approval leaves the reply's end to the approval's settlement.
A continuation finishes once a FINAL at its first source was acknowledged or refused for good.
Its finish, release, or discard applies the reply rule while the continuation still exists and deletes the continuation in the same transaction.
A release hands the run's sources back to replay, unless a Stop is recorded: the reply then ends cancelled instead of replaying what the user stopped.
A restart leaves a reply an approval holds to approval recovery, including a span approved in place that an older instance ran.

## Interrupted responses

A cancellation, or a failure the response settles as its outcome, that ends a response before its answer's own terminal write ends its span through one table, `interrupted_end` in `reply_scope.py`:

- A recorded Stop ends the reply `cancelled` with the cancel note, whatever stopped the response.
- An interruption of a reply that has no event yet leaves Matrix untouched, and its sources stay pending for a retry.
- Otherwise, before delivery starts, the reply shows its restart, interruption, or error note while its sources retry; once delivery started, it ends `failed` with that note.

A regeneration that wrote nothing keeps the answer it was replacing in every case.
Exceptions raised before the response settles take the exception path instead: a preparation failure after the placeholder ends the reply `failed` with the error note through `dispatch_failed`, a membership or revision change supersedes the span so its sources replay, and any other exception releases the span for a retry.

## Abandoned regenerations

A regeneration abandoned without an answer or a retry restores the finished answer it was replacing only when it recorded no write Matrix may show, counting unacknowledged writes.
Otherwise the exit's terminal row, an owed note, or an owed redaction brings Matrix to match the records; a suppressed answer keeps what it showed and owes the interrupted note, or the cancelled note after a Stop.
A terminal row Matrix refused for good, a reply's only message included, ends the reply `failed` with the delivery-failed note owed.

## Lifetime

Each bot instance writes a fresh generation for its principal at start, then ends what older instances left (`owner_lost`): orphaned spans end `lost`, replies whose sources settled fail with the restart note (or end `gone` when they never wrote anything), and replies whose sources are pending wait for their replay.
A membership departure ends the room's replies `gone` inside the departure fence and cancels their spans afterwards.
A replay that a newer message from the same requester supersedes settles its sources with its reply, unless the reply still owes Matrix a write.
A bot instance that another took over writes nothing more: its claims and every write its running spans make, approval resumes included, are refused against the principal's persisted generation; a resume it left stays open to the owner's approval recovery, which ends it.
A replay that ingress settles without a turn, such as one whose requester lost access, ends its reply in that commit with the interrupted note, or removes a reply that showed only its placeholder.
Deleting every logical source of a reply's current work ends it `gone` in the tombstone's commit, which records the reply and the span it cancelled; the bot then cancels exactly that span and redacts what the reply showed, while a reply an approval holds and a written answer are kept, including the finished answer an edit was regenerating before the regeneration showed anything.
An entity removed from the configuration has no bot: its open replies end `failed` without Matrix writes, and their sources settle unanswered; a reply an approval holds is left to that approval, whose discard ends it, or whose owner settles it on coming back.
A removed entity's reply keeps the notes and redactions it still owes Matrix, which its bot delivers if the entity comes back.
The handled-turn retention pass deletes finished replies that owe nothing, with their spans, 30 days after their last change, the age at which the ledger forgets their turns.

## Assumptions and accepted limitations

This section records what the reply model deliberately does not handle, so a review does not report it as a defect.
A reply finding is a defect only when it reaches one of these outcomes on a realistic path, meaning normal use, one crash or restart, or one external fault such as Matrix refusing a write:

1. MindRoom crashes, or a room's event lane or a conversation wedges.
2. A user message is never answered and the user is never told why.
3. One message gets two visible answers.
4. An answer lands on the wrong turn or in the wrong room.
5. A reply shows "Thinking…" or a partial answer forever after a restart, Stop, edit, or approval.
6. A tool runs without its approval, or MindRoom itself runs a tool call a second time.
7. A Stop is ignored: the agent starts new tool calls or keeps answering after the user stopped the reply.

Sequences that need two or more independent faults are accepted limitations, not defects.
A race is a defect when ordinary use can hit it, such as a Stop, an edit, and a restart close together; it is an accepted limitation only when it needs internal steps to land in a window ordinary use does not reach.
The behavior below follows from deliberate decisions; a change that would restore what they removed needs a new decision, not a fix.

### Decisions

- A restart continues an interrupted reply in place below what it showed, and a retried regeneration rewrites the same message; neither starts a second message, given an upgrade that runs while no reply is in flight.
- An approval pauses and holds its reply, and later messages in the conversation are answered while it waits; a CLI approval that waits in place keeps its conversation for the wait; no approval holds the room's event lane.
- An edit regenerates only the reply to the latest message of its conversation, and only when that reply showed something and it is not `gone`; any other edit changes no reply.
- An edit of a reply that still streams stops it, as a Stop reaction would, and regenerates it in place; a second edit during that regeneration does the same, so the newest edit wins.
- An edit of a reply that waits for an approval stops it, as a Stop reaction would, which cancels the approval and expires its cards, and regenerates it in place once the approval ended.
- A regeneration that wrote nothing keeps the finished answer it was replacing.
- A rule that meets a state it does not model never raises: the reply's current work ends `failed` with the error note, while a reply that already ended keeps its end and a stray second create is redacted with the first left bound.
- After a restart the model is told which tool calls finished and which were cut short and may have taken effect, to check before repeating them, and nothing blocks a repeated call: a fast model rarely repeated a finished call (up to 1 of 24 real-model runs) but repeated a cut-short call whenever its tools gave it no way to check, which the owner accepted, because blocking an identical call would also block a read-only call the model must run again when its shortened result is not enough.
- An upgrade from an earlier release cannot rule out an approval that release left pending, so it cancels every one: the approval's cards and records are deleted, its sources settle unanswered, and its reply gets no records.

### Accepted limitations

Edits:

- An edit of a message that no longer waits in the coalescing queue, made before its reply showed anything, changes no reply; the turn answers the original text. This includes a message still waiting for media or voice before it reaches the queue.
- Only a text message is edited in the coalescing queue: an edit of a queued media message's caption changes nothing.
- An edit applied to a message still in the coalescing queue lives in memory, so a crash before the flush answers the original text.
- A coalesced turn whose messages come from more than one requester, or carry a delegated speaker, never regenerates; an edit of an earlier message of the latest coalesced turn regenerates the whole turn.
- When two messages of one coalesced turn are edited before either regeneration claims the reply, only the newer edit regenerates, and the earlier message keeps its original text in that answer.
- A regeneration whose history was redacted meanwhile is suppressed instead of rebuilt.
- An edit a `message:received` hook suppresses still counts as its message's newest revision, so an older edit of that message arriving later regenerates nothing, nor does an earlier edit of another message of the same coalesced turn whose regeneration has not claimed the reply yet; a later edit of another message regenerates with the suppressed message's earlier text.
- A Stop on the old answer after the edit stopped it does nothing to the regeneration, because the stopped reply's exit applies it; the regeneration offers its own Stop button once it claims, when Stop buttons are enabled and deliverable.
- When the stopped reply's terminal row is still unresolved, or the approval its Stop cancelled has not ended yet, the regeneration's claim is deferred and the edit is dispatched again later; if someone wrote in the conversation meanwhile, the retried edit is ignored and the reply keeps its cancelled note.
- Each retry of a deferred edit runs the `message:received` hooks again, because the edit's revision is recorded only when its regeneration claims.
- While a deferred claim stays blocked by an unresolved row, each retry backs off that room's event lane for between 1 and 30 seconds until the row resolves.
- After a restart, a regeneration may be told about tool calls the attempt before the edit made, which errs toward not repeating a side effect.
- A regeneration that a journal failure stops before its claim settles its edit after the edit already stopped the streaming reply, so the reply keeps its partial answer with the cancelled note.

Tool calls and the restart account:

- A call cut short stays recorded as started, and the model is told to check whether it took effect before repeating it.
- In a team, only the leader reads the account.

Delivery and recovery:

- A failure before anything was delivered shows the error note while the turn keeps replaying, so one that recurs on every attempt keeps retrying, and each retry backs off that room's event lane; a regeneration keeps showing the answer it was replacing, without a note, while it retries.
- A reply row written while its create's outcome is unknown is sized as a plain message and wrapped as an edit only when claimed; after a homeserver outage, an answer near the event size limit can then be refused and end with the delivery-failed note.
- A reply still streaming when an upgrade from an earlier release stops the backend can keep its partial text, and its replay may answer in a new message.
- An approval the upgrade cancels leaves its Matrix message as it was, which can still show that it waits for approval, and a click on its card does nothing.
- Deleting every source of a reply an approval holds keeps the reply and its approval cards (see [Approvals](#approvals)).
- A room departure and a removed entity end their replies without writing to Matrix, so those messages keep what they last showed (see [Lifetime](#lifetime)).
- A note Matrix refused for good is not sent again (I15).

Journal writes:

- Every progress edit is a journal write recorded before the edit is sent; up to 64 writes queued together commit in one transaction, and how often edits are sent follows the stream throttle.
- An error that aborts the whole SQLite transaction, such as a full disk, fails every write in that batch; no caller is told a write landed that did not.
- The writer task runs with the context of the first write it served, so log lines a batch emits carry that caller's bound log context.

## Invariants

- I1. At most one current span; only span-authored events from it change the canonical answer.
- I2. Durable writes of a reply are sent in sequence by its one sending owner; an attempted write is resolved before a later one is sent.
- I3. A terminal reply's state and answer change only by a regeneration claim, a failed terminal write, a completed redaction, a late create bound for redaction, or the note it owes.
- I4. A recorded Stop is eventually applied, unless a terminal row was enqueued before it.
- I5. Every non-terminal reply has a durable path to progress: pending span sources, an approval continuation, or `owner_lost`.
- I6. A span's sources settle with its terminal transition, except spans that hand them to a continuation, a retry, or a replay.
- I7. Every reply write is recorded before it is sent.
- I8. A Stop button is redacted when its reply leaves `active`, except while its span waits in place.
- I-S1. Every reply fact in the ownership table is read from reply records or a cache of them; no other store writes it.
- I-S2. A reply settles its journal sources only through `SettleSources`, which marks the turn they index answered in the same transaction when the reply answered them, including a finished approval's paused span; deleted sources, sources that became terminal without an answer, and those of a removed entity or of an approval whose owner is gone settle unanswered.
  Sources settled outside a reply, by an ingress decision that drops a waiting replay or by a room departure, leave their turn unanswered and end the reply that waited on them.
- I-S3. The outbox commits nothing outside transport for a row with `reply_id`.
- I-S4. A continuation names its reply through its paused span and holds no reply or source fact of its own.
- I9. Every unfinished continuation has an owner that moves it: draining every owner leaves no continuation, owed write, or deferred claim, and every source settled.
- I10. Retention never forgets a reply whose spans a continuation names.
- I11. A regeneration that recorded a write Matrix may show never restores its rollback.
- I13. At most one continuation names a reply's spans.
- I15. Once a finished reply owes nothing, its newest write Matrix took ends it, unless Matrix refused a note, which is not resent, the bot left the room, or the entity was removed.
- I16. A reply waiting to replay its original source has not had its turn recorded answered, which would suppress that replay.
- I17. No source is answered twice, one turn has one reply, and a reply writes only for its current span or, for a note it owes, its last.

`tests/test_reply_lifecycle_fuzz.py` checks I1, I7, I9, I10, I13, I15, I16, I17, and that a paused reply is held, over random interleavings of claims, writes, acknowledgements, Stops, restarts, regenerations, approval decisions, resumes and recoveries, deletions, departures, entity removal, retention, supersessions, and dropped replays.
It keeps continuations as rows the hold is derived from, regenerates a running or held reply by stopping it first as the edit regenerator does, defers claims through the shared blocking predicate, and ends every run by draining every owner.
The unit tests cover stale and retired spans and the remaining rules.
