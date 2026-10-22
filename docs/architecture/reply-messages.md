# Reply Messages

Every AI reply MindRoom shows in Matrix is owned by a durable reply record.
The record, not the Matrix timeline and not the process that happens to be streaming, decides what the reply shows, which work runs for it, and how it ends after a Stop, a restart, an edit, an approval, a deletion, or a departure.
This page records the outcomes that design rules out, the decisions behind it, its accepted limitations, and the invariants tests pin; the rules are in `src/mindroom/reply_lifecycle.py`.

## What counts as a defect

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
The [decisions](#decisions) and [accepted limitations](#accepted-limitations) below are deliberate; a change that would restore what they removed needs a new decision, not a fix.

## Assumptions

- An upgrade runs while no reply is in flight; the limitations below list what an upgrade during a reply leaves behind.

## Decisions

### Ownership

Every fact about an AI reply has one owner and one writer, so no two stores can disagree about a reply:

- I-S1. Everything about an AI reply, including its event, state, presentation, Stop, each span's sources and their settlement, its tool calls, its redactions, and which bot instance claims its approval, is read from the reply records or from an in-memory cache of them that is never written separately; no other store writes it.
  The turn ledger keeps user-message and turn facts, and a response event only for turns that are not AI replies, such as commands, rejections, and router notices; the journal keeps whether each event is pending or settled; agent history keeps the conversation the model sees.
- I-S2. A reply settles its journal sources in the same transaction that marks the turn they index answered when the reply answered them, so a crash cannot leave one without the other.
  Deleted sources, sources that became terminal without an answer, and those of a removed entity or of an approval whose owner is gone settle unanswered.
  Sources settled outside a reply, by an ingress decision that drops a waiting replay or by a room departure, leave their turn unanswered and end the reply that waited on them.
- I-S3. The outbox commits nothing outside transport for a reply's outbox row.
- I-S4. An approval continuation owns consent and the paused run payload, names its reply through its paused span, and holds no reply or source fact of its own.

### Claims

- An edit regenerates only the reply to the latest message of its conversation, when no one other than an agent wrote after it, that reply showed something, and it is not `gone`; any other edit changes no reply.
- An edit of a reply that still streams, or that an approval holds, stops it first as a Stop reaction would, which cancels the approval and expires its cards, and then regenerates it in place; a second edit during that regeneration does the same, so the newest edit wins.
- The edit hands the regeneration to its own task and does not wait for its claim, because the claim waits for the conversation lock, which another reply of the agent may hold for as long as that reply runs.

### Writes and restarts

- Recovery renders from what a reply may already show, counting writes Matrix has not acknowledged, so a restart continues an interrupted reply in place below what it showed and a retried regeneration rewrites the same message; neither starts a second message.
- A tool call is recorded as started before the tool runs and as completed when it returns, whether or not the reply shows tool calls, so a restarted reply can be told which calls already ran.
- The start is refused, and the tool does not run, once a Stop is committed or its span is no longer current, because a Stop's cancellation cannot reach a synchronous tool running on a worker thread.
- A call cut short stays recorded as started, since it may have taken effect.
- Nothing blocks a repeated call; the model is told which calls finished and which were cut short and may have taken effect, to check before repeating them.
  In real-model runs a fast model rarely repeated a finished call (up to 1 of 24 runs) but repeated a cut-short call whenever its tools gave it no way to check.
  The owner accepted this, because blocking an identical call would also block a read-only call the model must run again when its shortened result is not enough.
- Effects that run after a rule commits, such as cancelling a span or waking an approval, are best effort, because the records already say what must happen; only an approval's finish or release runs its effects to completion even when its caller is cancelled, so the conversation it held is always released.

### Approvals

- An approval pauses and holds its reply and its conversation: later messages to that agent in that conversation wait with a ⏳ reaction, as behind a running reply, until the approval ends; no approval holds the room's event lane.
- With background jobs on, a top-level agent's gated call that can become a managed job asks for its approval from that job instead of pausing its reply, so the approval holds neither the reply nor its conversation; after `approval_wait_timeout` the reply goes on and the job's outcome reaches the conversation like any other job's, and a restart interrupts the job, so that approval does not survive a restart as a paused reply's does.
- The hold is not the conversation lock, because the resume, the failure settlement, an edit's regeneration, and approval recovery must still take that lock.
- The hold is taken before the pause commits, so the approval's end can never come before it, and each bot start takes the holds of its principal's pending approvals before it replays the journal.
- A message waiting behind a hold rechecks every 30 seconds that its approval still exists, so a hold whose end did not reach this bot still ends.
- Deleting the message a held reply answers cancels the approval, as a Stop would, and removes the reply.
- A released approval hands its sources back to replay, unless a Stop is recorded: the reply then ends `cancelled` instead of replaying what the user stopped.

### Lifetime

- Each bot instance takes its principal's replies over at start, and an instance another took over writes nothing more: its claims and every write its running spans make, approval resumes included, are refused, so it cannot write beside its successor.

## Accepted limitations

Edits:

- An edit of a message that no longer waits in the coalescing queue, made before its reply showed anything, changes no reply, and the turn answers the original text; this includes a message still waiting for media or voice before it reaches the queue.
  An edit regenerates only a reply that showed something, and the message is still answered once.
- Only a text message is edited in the coalescing queue: an edit of a queued media message's caption changes nothing, and the message is answered with its original caption.
- An edit applied to a message still in the coalescing queue lives in memory, so a crash before the flush answers the original text.
- A coalesced turn whose messages come from more than one requester, or carry a delegated speaker, never regenerates, because a regeneration replays every source under the editor's identity and would run messages the editor did not write as theirs.
- An edit of an earlier message of the latest coalesced turn regenerates the whole turn, because the turn has one reply.
- When two messages of one coalesced turn are edited before either regeneration claims the reply, only the newer edit regenerates, and the earlier message keeps its original text in that answer.
- A regeneration whose history was redacted meanwhile is suppressed instead of rebuilt, and the reply keeps the answer it was replacing, with its cancelled note after a Stop.
- An edit a `message:received` hook suppresses still counts as its message's newest revision, because revisions only move forward.
  An older edit of that message arriving later regenerates nothing, nor does an earlier edit of another message of the same coalesced turn whose regeneration has not claimed the reply yet; a later edit of another message regenerates with the suppressed message's earlier text.
- A Stop on the old answer after the edit stopped it does nothing to the regeneration, because the stopped reply already applies it; the regeneration offers its own Stop button once it claims, when Stop buttons are enabled and deliverable.
- When the stopped reply's terminal write is still unresolved, or the approval that holds it has not ended, the regeneration's claim is deferred and the edit is dispatched again once that resolves, or within 30 seconds if that wake is lost.
  If someone wrote in the conversation meanwhile, the edit no longer targets the latest message, so it is ignored and the reply keeps what the Stop left: its cancelled note, or an approved answer that was already final.
- Each retry of a deferred edit runs the `message:received` hooks again, because the edit's revision is recorded only when its regeneration claims the reply.
- A regeneration that a journal failure stops before its claim settles its edit after the edit already stopped the streaming or held reply, so the reply keeps what the Stop left, such as its partial answer with the cancelled note.

Tool calls:

- In a team, only the leader is told which tool calls an interrupted attempt made.

Delivery and recovery:

- A reply write made while its create's outcome is unknown is sized as a plain message and wrapped as an edit only when claimed, so after a homeserver outage an answer near the event size limit can be refused and end with the delivery-failed note.
- A reply still streaming when an upgrade from an earlier release stops the backend can keep its partial text, and its replay may answer in a new message, because upgrades are assumed to run with no reply in flight.
- An upgrade from an earlier release cancels every approval of a paused reply that release left pending, because it cannot rule out that one is pending; approvals of scheduled calls are kept.
  A cancelled approval's Matrix message keeps what it showed, which can still say that it waits for approval, and a click on its card does nothing.
- A message that already waited for the conversation's lock when the reply before it paused for an approval is answered while that approval waits; it is still answered once.
- A 30-second recheck of the approvals holding a conversation that lands while a reply's pause is still committing can drop that approval's hold, so later messages are answered while it waits; this needs the recheck to land inside that commit.
- A room departure and a removed entity end their replies without writing to Matrix, so those messages keep what they last showed: the bot has left the room, or the entity has no bot; a removed entity's reply still delivers the notes and redactions it owes if the entity comes back.
- A note Matrix refused for good is not sent again (I15).
- An interactive selection whose acknowledgement Matrix refuses for good stays unanswered without a note: the acknowledgement is smaller than the question MindRoom already delivered, so only a room-wide refusal such as lost send permission can refuse it, and that would refuse the note too.

Journal writes:

- Every progress edit costs a journal write before it is sent (I7).
- An error that aborts the whole SQLite transaction, such as a full disk, fails every write in that batch; no caller is told a write landed that did not.
- The journal writer runs with the log context of the first write it served, so log lines a batch emits carry that caller's bound context.

## Invariants

The numbers in parentheses refer to the outcomes in [What counts as a defect](#what-counts-as-a-defect).

- I1. At most one span is current per reply, and only events the current span authored change the canonical answer, so a stale or superseded attempt never writes into it (3, 4).
- I2. A reply's durable writes are sent in sequence by its one sending owner, and an attempted write is resolved before a later one is sent, so Matrix ends on the reply's newest write.
- I3. A terminal reply's state and answer change only by a regeneration claim, a failed terminal write, a completed redaction, a late create bound for redaction, or the note it owes, so work that already ended cannot reopen or rewrite it.
- I4. A recorded Stop is eventually applied, unless a terminal write was enqueued before it (7).
- I5. Every non-terminal reply has a durable path to progress: pending span sources, an approval continuation, or the next bot instance's takeover (2, 5).
- I6. A span's sources settle with its terminal transition, except spans that hand them to a continuation, a retry, or a replay, so a crash can neither answer them again nor drop them (2, 3).
- I7. Every reply write is recorded before it is sent, so recovery knows what Matrix may already show.
- I8. A Stop button is redacted when its reply leaves `active`, except while its span waits in place for an approval, since that span still runs.
- I9. Every unfinished continuation has an owner that moves it: draining every owner leaves no continuation, owed write, or deferred claim, and every source settled; a stranded approval would hold its conversation and reply forever (1, 5).
- I10. Retention never forgets a reply whose spans a continuation names, because the continuation reads its room, thread, event, and sources from them (I-S4).
- I11. A regeneration that recorded a write Matrix may show never restores its rollback, because the records would then hold the old answer while Matrix may show the new write.
- I13. At most one continuation names a reply's spans, so one approval decides how a held reply ends.
- I15. Once a finished reply owes nothing, its newest write Matrix took ends it, unless Matrix refused a note, which is not resent, the bot left the room, or the entity was removed (5).
- I16. A reply waiting to replay its original source has not had its turn recorded answered, which would suppress that replay (2).
- I17. No source is answered twice, one turn has one reply, and a reply writes only for its current span or, for a note it owes, its last (3, 4).

`tests/test_reply_lifecycle_fuzz.py` checks invariants from this list over random interleavings of every reply event.
