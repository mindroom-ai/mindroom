# One turn driver, one turn record, one reply ending

Decisions behind the refactor that made every reply turn run on one streaming driver, gave the interrupted-turn record one writer, merged the agent and team locked turns, and left every non-success reply ending to the reply span's exit.
The code shows what each owner does; this record keeps why, what was rejected, and what stays open.

## Why

One reply turn was written four times: agent or team, each blocking or streaming.
Every provider request streams since #3148, so the blocking drivers only re-derived, from the finished run record, what the stream had already produced, and settled by different rules: an empty streamed reply published `error` while a blocking one kept its record's status, and a blocking cancel was recorded from the recorder's mirrored state while a streamed one was recorded from the attempt's partial output.
The interrupted-turn record had three writers (the driver's `AITurnState` mirror, the runner's twelve save sites, and a recorder-less standalone path) guarded by a once-only flag, and a Stop recorded itself as `cancelled` or as `error` depending on whether it landed in the provider await or in the streamer's Matrix write.
A non-success reply ending was decided by the streamer (a terminal row with the note inside the answer text), again by the gateway (about twelve branches turning a transport outcome into a hand-built outcome), and again by the runner (ending whatever span was left), and a non-Stop cancellation followed one rule when streamed and another when delivered whole.

## Decisions

- Blocking callers collect the streaming driver; Matrix delivery that waits for the whole reply stays a delivery choice ("collect, then deliver the final message"), not a second driver.
  A collected reply is therefore the streamed text: it keeps narration around hidden tool calls (with the same paragraph gap the streamer adds) and the narration of a turn that continued after a model switch, and a collected team reply is the streamed team document.
- The silent-schedule rule for teams (a leader `NO_REPLY` with no member finding delivers and records the bare token) moved into the one team settlement before the blocking branch was deleted; it was semantic behavior, not rendering.
- The driver is the only writer of the turn record and stores an interrupted turn once, into the scope its runs are stored in, so the row kind comes from the scope and `is_team` disappeared.
  It settles the record before it yields the text that ends a turn, because a consumer may close the stream as soon as it reads that text.
  A consumer that stops reading (a Stop in the streamer, a delivery failure) leaves the turn recorded as stopped; process shutdown records nothing, because the successor replays the turn.
  A whole delivery collects the turn to its end before its delivery hooks run, so a Stop in `message:before_response` leaves the completed run in history while the reply ends stopped; recording it again after delivery would make the runner a second writer of the record.
  Its run-only effects (memory, skill review) still skip it, because the turn's generation never returned a delivery.
- The streamer now closes the response stream in its consumer task, and forwards the supervising task's shutdown tag to that task.
  The driver waits for a started interrupted-record store through any further cancellation, such as the one the streamer sends when its stream task has not ended 0.5 s after a Stop, because the reply settles, `session:started` checks for the session's first row, and the conversation's next turn reads history only after the turn's task ends.
  Before, a Stop that landed while the streamer awaited Matrix left the driver suspended until garbage collection closed it in a different task, which carried no shutdown tag.
- A turn rejected before any attempt ran (a team member that is not running or that the requester may not use, a team name sent to the agent path) records nothing; its message and the rejection reach the next turn through the thread as unseen events.
- The cancel source has one owner, the cancellation's message; the per-task source registry nobody read is gone, and with it the promise to keep the first source, which `Task.cancel` never kept.
- Agent and team turns run one locked sequence; what differs is decided in one preparation step per kind, which returns the values the shared generation and settlement read.
  A team Stop therefore takes the agent's path and is no longer logged as an error.
- Tool-call visibility is read once per turn, from the reply the turn claimed, because reading live configuration after later awaits let a reload split one turn between two visibilities.
- The streamer writes only a stream that ran to its end; a cancelled or failed stream raises, and the span exit writes the note as a note segment below what the reply's records say it shows, through `interrupted_end`, for both delivery modes.
  A stopped stream's final message therefore keeps what it last showed plus the note; text generated after its last progress edit stays in history, not in the message.
- A whole delivery with nothing to show ends as a stream that showed only its placeholder does, instead of the placeholder text standing as a completed answer.
- Approval-resumed replies keep `message:before_response` on their final text, though their progress can stream first, because their final answer is delivered as a whole delivery; only a stream the streamer finishes runs `message:final_response_transform`.
- An ordinary answer delivered whole whose final write is recorded but not yet acknowledged reports `suspended`, and its run-only effects (finished notification, memory, skill review) apply at once, because its run finished and nothing applies them when the outbox later delivers it.
  Effects and hooks that need a delivered completed answer (`message:after_response`, buttons, thread summary) still skip it; an approval's owed final keeps running everything after acknowledgement, so it reports its run unfinished until then.
  A failed or cancelled effect is only logged there, because the outbox owns the answer by then and settling it again would fire `message:cancelled` and release its sources for an answer still on its way.

## Rejected alternatives

- Deriving every hook-facing outcome from the reply records (`outcome_of`).
  Review found fields the records cannot give today without new recorded facts: `delivery_kind` (the placeholder is sent after the claim), the rendered body of an interactive answer, interactive option values (rendering drops them), an owed FINAL's status (streamed and whole deliveries report it differently), and a cancellation the records show as CANCELLED without a Stop (source deletion).
  The success outcomes stay built where they are delivered.
- A typed `TurnFailed` stream ending.
  It needed every consumer (Matrix streamer, collector, API first-event status, approval collection, voice, children) and changes error replies from answers to cancellations; it stays a follow-up of #3119.
- Turning a final-only `RunFinished` body into a delta, which would move final-only replies from `before_response` to `final_response_transform`.
- Routing the OpenAI-compatible team endpoints through the Matrix team driver, which would add document rendering the API strips again.
- Turning `response_runner.py` into a package while the approval-records refactor moves its approval methods out of the same file.

## Open

- An interrupted turn's record still comes from the running attempt's partial output and is stored as an interrupted replay row, not as an "interrupted" run status (#3108).
