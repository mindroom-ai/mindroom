# Stopped and failed turns keep their own record

A turn that a Stop or a failure cut short is stored as the record its run's loop ended with, and later turns replay that record's messages (#3108).
Before, the loop stored a `CANCELLED` or `ERROR` record that history never replays, and the turn driver then saved a `COMPLETED` row over the same run id, holding the partial text, a status line, and redacted previews of the finished calls, flagged `mindroom_replay_state: interrupted`.

## Decisions

- The turn driver decides, where it already decided before, that the turn ended interrupted, and then makes the run its loop stored the turn's record: it reads the stored run back, marks it, and writes it over its row.
  Reading the stored run, not the attempt's in-memory record, keeps the facts patched onto the row after the run ended, such as delegation state.
  A run the loop left paused, which nothing resumes now, ends first as a cut-short run does: its waits and the context only its resume needed leave.
- A marked run is `COMPLETED` with `mindroom_interrupted` in its metadata, holding the number of its calls that had not finished; a failure's text goes in its `error`.
  An earlier release ignores the key, so after a rollback it loads the row and replays it as the finished turn its messages show.
  Background readers such as memory auto-flush and graph ingestion read completed turns, so a stopped turn's user message keeps reaching them, and the marker keeps its cut-short reply out of what the agent said, as the old flag did for the rendered summary.
- `CANCELLED` and `ERROR` keep meaning a run whose turn is still owned elsewhere and that history skips: a run a successor runtime takes over, a delegated child that delegation recovery settles, a minimal-mode checkpoint, a mid-turn fold's row of a running run, and a stopped approval resume.
  The storage rules for pending runs (loaded wherever they sit, never folded, overwritten by the run's own later save, moved to the log's end when they become visible) keep applying to them, and a marked run, once completed, moves to the log's end as any pending run that becomes visible does.
- The loop's record holds what the turn did: its messages, the text a cut-off request streamed as one assistant message, and each finished call's result, which the loop appends when a stop lands while it reports the batch.
  The calls of a batch that a stop cut off all count as unfinished, because the loop records a batch's results only once every call in it has ended.
- Marking drops each call no stored result answers from its assistant message, as the tool-call window drops a call whose result it leaves out, so every stored call has its answer and any release's request is provider-valid.
  A message left with no text and no call stays stored, because it holds its request's usage, and replay leaves it out.
- A message MindRoom edits, by dropping a call or the text a voice call never spoke, also drops the replay state its provider stored, keeping only MindRoom's model provenance and usage marks, and every provider formats it from its canonical fields, as it formats another provider's reply.
  That state describes the reply as it arrived: Claude's blocks and Gemini's parts replay its text, a stored Responses id and digest let the next request continue a server response that still waits for the dropped call's output, and a native checkpoint's items replay the dropped call directly.
  One rule for every edited message covers every route, the earlier releases included, which read the same state; messages the mark leaves whole keep their state, so their signed reasoning and prompt cache stay.
  A request after a dropped checkpoint replays the history the checkpoint stood for once, from an earlier checkpoint or in full, and the next response the provider compacts records a new one.
- The replay ends with a note derived from the stored record, `(turn stopped before completion)`, or `(turn failed before completion)` when the record holds a failure's text, with the number of calls that did not finish, so no synthetic text is stored and the wording can change without a migration.
  The note is an assistant message of its own: Claude and Gemini replay a reply's stored blocks rather than its text, so a note joined to that text never reached them.
- The model now sees a stopped turn's finished tool results in full, as it does a completed turn's, instead of redacted previews capped at 32,000 characters.
- Owners that end a run themselves rewrite that run's record instead of building another: a voice call keeps of its answer only what playout spoke, and CLI approval recovery ends the paused run it does not resume.
- The rendered summaries earlier releases stored stay `COMPLETED` rows that replay as stored; only background readers check their flag, to leave the summary out of what the agent said.

## Rejected alternatives

- A new `INTERRUPTED` status: a release before it fails to load any conversation holding such a run, and background readers, which read completed and paused turns only, lost the stopped turn's user message.
- Storing the stopped turn as `CANCELLED` or `ERROR` and making those statuses replay needed a new status for every pending row above, a layout step marking every `CANCELLED` and `ERROR` row stored so far as pending, and changed the meaning of the stop endings the storage oracle's recordings hold; `RUNNING` could not serve, because the rows Agno stored as `RUNNING` replay today.
- Marking the run with the loop's own save kept one write per run but needed the loop to know whether its owner ends the turn, or the stored status to differ from the record's, which readers of the record such as the team runtime and the OpenAI-compatible API check.
- Dropping unfinished calls only at replay kept the stored row as the loop wrote it, but an earlier release would replay those calls without results, which providers reject.
- Editing each provider's stored state to match an edited message (pruning Claude's blocks, a checkpoint's items, Gemini's parts) needs one rewrite per provider format, and still leaves a server-side response that waits for the dropped call.
- Storing the note in the record would have tied model-visible wording to stored data.

## Limitations

- A turn stopped or failed before its first run stored anything leaves no record; a Matrix reply's next turn still reads the message as unseen thread context, and a voice call stores the transcript in an interrupted record of its own.
- A team turn's record is its leader's run: a member's partial work in a delegation that was still running when the turn stopped stays in the member's own run, which no later turn replays.
- After a rollback, an earlier release replays a marked run as a finished turn without the note, and its background readers take the run's cut-short reply as the agent's words.
