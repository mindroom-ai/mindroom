# History compaction

This page records the assumptions and decisions behind portable history compaction before and during a reply, its summary rows, and seen-message tracking.
User-facing behavior is in [Context Window](../configuration/history.md#context-window), Agno workarounds are in the [Agno compatibility inventory](agno-compatibility.md), and the Claude replay workaround is in `history/claude_replay_compat.py`.
When upgrading Agno or a provider SDK, run those modules' contract tests before removing a workaround.

## Assumptions

Session rows and runs are saved whole from in-memory snapshots, so a stale save can arrive after a compaction or a redaction has moved on.
The summary rows are therefore authoritative: `session.summary` is derived from the owner scope's latest row on every read, and run writes refuse a stale copy of a folded run or of a run a cut removed.
A compaction, a redaction, and an approval's completion can overlap on one scope, so each summary chunk checks, under the write lock, that the history it summarized is still the stored history before it appends.

## Decisions

- A summary request goes out without streaming and with the provider's own retries, thinking, and prompt cache off, because the summary retry policy owns retries, thinking spends the output budget the summary needs, and a one-off summary never reuses its prefix.
  It keeps the route's transport timeout instead of overriding it with `timeout_seconds`, because a shorter authored provider timeout must still apply; the summary call's own deadline cancels a slower request.
- A summary counts as complete only when the provider reports that it ended on its own; a summary cut off by the output limit, a content filter, or a tool call is never stored.
  A route that reports no stop reason cannot show a cut-off, so its summaries count as complete.
- Compaction keeps every run in Agno's runs table and appends a summary row positioned after the newest run it covers, instead of moving runs into a second store, because keeping two stores consistent produced repeated bugs once summaries had to cover part of a running turn (see `docs/dev/2026-10-12-compaction-log-store-design.md`).
- Edits and redactions cut the log from the earliest element holding the event; rows that only end earlier summaries (barriers) survive cuts, so retired summaries never replay again.
  Summaries written when compaction deleted runs cannot be split by run, so while such a summary is in force, redacting an event it may contain cuts at it, removing every run and summary row of the scope.
- Seen ids are derived from the loaded runs and the summary rows after the latest barrier rather than stored, so they follow the remaining history after compaction or redaction.
  Every redaction change drops the scope's metadata seen ids, because dropping ids can only re-offer messages, never hide them.
- A completed approval moves to the end of the log, after the turns that happened while it waited, because its continuation saw them.
- The summary replays as the first history message instead of inside Agno's system prompt, so a compaction never changes the cached prompt prefix, and minimal agents with a string system message see it too.
  After a mid-turn compaction it follows that turn's prompt instead, in the rewritten request, its resume, and later turns while that run replays first, because a fast model that reads the prompt last restarts the request even when the summary records it finished (evidence in `docs/dev/2026-10-10-compaction-unification-design.md`).
  It is marked as history, so Agno never stores it in a run and an approval resume rebuilds it with the rest of its history.
- Persisted replay fitting runs once before a reply; the mid-turn hook sizes every model request of the reply, from the latest response's reported usage when the loop received one, and by estimate on native routes, whose checkpoint responses are billed for the transcript they replaced.
- The compaction check before a reply takes the larger of the characters / 4 history estimate and the request size the provider reported for the scope's latest answer, plus that answer, minus the static estimate of the system prompt and tools.
  The estimate alone undercounts real tokenizers, and no fixed tokenizer fits every provider: on one Qwen thread, tool results counted 1.49 times their characters / 4, tool call arguments 1.44 times, and assistant text 1.20 times, `o200k_base` still undercounted by about 10%, and a history estimated under the trigger went out as a 126,847-token request in a 128K window.
  The reported size is ignored on native routes, under replay limits, after another model answered, when the latest run carried media, and when a summary was written after that request started or in the same second, because each makes it describe content the reply does not replay, or count it with another tokenizer or billing basis.
  It can still overcount by the previous turn's transient memory and enrichment context and by reasoning resent inside the last tool loop, which can cause an early compaction, including on consecutive turns if that overhead recurs.
- A mid-turn compaction folds everything but the system prompt, the current prompt, transient messages, and queued notices, summarizing the scope's stored runs after the latest summary row together with the running turn's exchanges.
  It reads those runs from storage, not from the running loop's session, because that session still holds every run loaded when the turn started, so a second fold of the same turn would summarize them again.
  The request keeps each tool batch whole, but the summarizer reads every folded message as its own unit, because a model can read several large files in one parallel batch, and a batch larger than one summary input would be cut short and lose the later results.
  Each tool result names its own tool and arguments in the summary input, so it reads correctly apart from its call message, including for providers whose calls carry no ids.
- A fold appends one summary row that also holds its segment: the folded messages, the tool executions they hold, and the member responses attached so far, taken from the live request because Agno writes the run only when it ends.
  The two are written together and a cut must remove them together; the running run sorts after the row (a stored paused or checkpointed copy moves there), and a copy saved before the fold loads without the segment's messages.
- A cut removes a run whole: when a removed run holds a segment on an earlier row, the cut starts at that row, and the run of every removed segment is fenced, which covers a running turn that has not stored a row yet.
  Retiring summaries also removes every run that folded mid-turn, because its stored part starts mid-turn and would replay without what it built on.
- The fold's in-memory effects (the request rewrite, the recorder's folded tool calls, the folded requests' usage in run metadata, and the live session summary) and its success notice happen together with its commit, even when the turn is cancelled meanwhile, so a stopped reply never reports a stored fold as failed.
  An interrupted record therefore leaves out exactly the calls the stored summary holds; its write also reads that set from the run's summary rows, so paths without a turn recorder and a restarted CLI approval recovery leave them out too.
- A fold survives a crash of its turn: the restarted attempt replays its summary and knows which tool calls already ran and what they returned, and an edit or redaction of the turn's message still removes it through its segment.
- A request without persisted replay (team members, workflow participants, scheduled tasks with `history_limit: 0`) compacts run-locally: its summary is an ordinary message of its own run and is stored with it.

## Limitations

Metadata seen ids, such as team consumption, have no summary-row counterpart, so a stale whole-row session write can restore ids that a redaction dropped; that only withholds those messages from unseen thread context.
Stored history grows until the conversation is deleted.
Folded requests' usage lives in the run's metadata, so it is lost when the run never saves, as a crashed run's usage is.
