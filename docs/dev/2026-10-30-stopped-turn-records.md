# Stopped and failed turns keep their own record

A turn that a Stop or a failure cut short is stored as the record its run's loop ended with, marked `INTERRUPTED`, and later turns replay that record's messages (#3108).
Before, the loop stored a `CANCELLED` or `ERROR` record that history never replays, and the turn driver then saved a `COMPLETED` row over the same run id, holding the partial text, a status line, and redacted previews of the finished calls, flagged `mindroom_replay_state: interrupted`.

## Decisions

- The turn driver decides, where it already decided before, that the turn ended interrupted, and then makes the run its loop stored the turn's record: it reads the stored run back, sets its status to `INTERRUPTED` and, for a failure, its `error`, and writes it over its row.
  Reading the stored run, not the attempt's in-memory record, keeps the facts patched onto the row after the run ended, such as delegation state.
  A run the loop left paused, which nothing resumes now, ends first as a cut-short run does: its waits and the context only its resume needed leave.
- `CANCELLED` and `ERROR` keep meaning a run whose turn is still owned elsewhere and that history skips: a run a successor runtime takes over, a delegated child that delegation recovery settles, a minimal-mode checkpoint, a mid-turn fold's row of a running run, and a stopped approval resume.
  The storage rules for pending runs (loaded wherever they sit, never folded, overwritten by the run's own later save, moved to the log's end when they become visible) keep applying to all of them, and to the rows earlier releases stored, so no stored row changes meaning.
- `INTERRUPTED` is a visible status: history replays it, a summary may fold it, and only an interrupted rewrite writes over it, as for `COMPLETED`.
- The loop's record holds what the turn did: its messages, the text a cut-off request streamed as one assistant message, and each finished call's result, which the loop appends when a stop lands while it reports the batch.
  A call no stored result answers did not finish; the replay drops it from its assistant message, as the tool-call window drops a call whose result it leaves out, so every replayed call has its answer.
  The calls of a batch that a stop cut off all count as unfinished, because the loop records a batch's results only once every call in it has ended.
- The replay ends with a note derived from the stored record, `(turn stopped before completion)`, or `(turn failed before completion)` when the record holds a failure's text, with the number of calls that did not finish, so no synthetic text is stored and the wording can change without a migration.
- The model now sees a stopped turn's finished tool results in full, as it does a completed turn's, instead of redacted previews capped at 32,000 characters.
- Owners that end a run themselves rewrite that run's record instead of building another: a voice call keeps of its answer only what playout spoke, and CLI approval recovery ends the paused run it does not resume.
- The rendered summaries earlier releases stored stay `COMPLETED` rows that replay as stored; only background readers check their flag, to leave the summary out of what the agent said.

## Rejected alternatives

- Storing the stopped turn as `CANCELLED` or `ERROR` and making those statuses replay needed a new status for every pending row above, a layout step marking every `CANCELLED` and `ERROR` row stored so far as pending, and changed the meaning of the stop endings the storage oracle's recordings hold; `RUNNING` could not serve, because the rows Agno stored as `RUNNING` replay today.
- Writing `INTERRUPTED` with the loop's own save kept one write per run but needed the loop to know whether its owner ends the turn, or the stored status to differ from the record's, which readers of the record such as the team runtime and the OpenAI-compatible API check.
- Storing the note in the record would have tied model-visible wording to stored data.

## Limitations

- A turn stopped or failed before its first run stored anything leaves no record; a Matrix reply's next turn still reads the message as unseen thread context, and a voice call stores the transcript in an interrupted record of its own.
- A team turn's record is its leader's run: a member's partial work in a delegation that was still running when the turn stopped stays in the member's own run, which no later turn replays.
- A release before this one fails to load a conversation that holds an `INTERRUPTED` run.
