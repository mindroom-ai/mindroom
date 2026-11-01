# History compaction

This page records the assumptions and decisions behind portable history compaction before and during a reply, its summary rows, and seen-message tracking.
User-facing behavior is in [Context Window](../configuration/history.md#context-window).

## Assumptions

Runs are saved whole from in-memory snapshots, so a stale save can arrive after a compaction or a redaction has moved on.
The summary rows are therefore authoritative: `session.summary` is derived from the owner scope's latest row on every read, and a full save of a run is refused once it, or a member's team run, has completed, or once a cut has removed it.
Only an interrupted record, which holds what the user saw or heard, replaces a completed run, and facts added after a run ends, such as its reply event or delegation state, are patched onto its stored row.
A compaction, a redaction, and an approval's completion can overlap on one scope, so each summary chunk checks, under the write lock, that the history it summarized is still the stored history before it appends.

## Decisions

- A summary request goes out with the provider's own retries and prompt cache off, and on the Claude routes without the model's authored thinking setting, because the summary retry policy owns retries, thinking spends the output budget the summary needs, and a one-off summary never reuses its prefix; other routes keep the authored setting.
  Leaving it out turns thinking off only on the Claude models before Claude 5 other than Mythos Preview; every Claude 5 and 5.x model and Mythos Preview runs adaptive thinking when a request sets none, so their summaries still think.
  The request sends no `disabled` or `between_tools` setting, which only some of those models accept; Fable 5.1, Fable 5, Mythos 5.1, Mythos 5, Mythos Preview, and Opus 5.5 cannot turn thinking off at all.
  It keeps the route's transport timeout instead of overriding it with `timeout_seconds`, because a shorter authored provider timeout must still apply; the summary call's own deadline cancels a slower request.
- A summary counts as complete only when the provider reports that it ended on its own; a summary cut off by the output limit, a content filter, or a tool call is never stored.
  A route that reports no stop reason cannot show a cut-off, so its summaries count as complete.
- Compaction keeps every run in the runs table and appends a summary row positioned after the newest run it covers, instead of moving runs into a second store, because keeping two stores consistent produced repeated bugs once summaries had to cover part of a running turn (see `docs/dev/2026-10-12-compaction-log-store-design.md`).
- Edits and redactions cut the log from the earliest element holding the event; rows that only end earlier summaries (barriers) survive cuts, so retired summaries never replay again.
  Summaries written when compaction deleted runs cannot be split by run, so while such a summary is in force, redacting an event it may contain cuts at it, removing every run and summary row of the scope.
- Seen ids are derived from the loaded runs and the summary rows after the latest barrier rather than stored, so they follow the remaining history after compaction, an edit, or a redaction.
  A team turn records the events it consumed only in its stored run, as an agent turn does, because a second copy in session metadata would outlive the runs an edit removes and hide their messages from the regenerated turn.
  For the same reason, seen ids that earlier releases copied into session metadata are not read: every event that history still represents is also held by its stored run or summary row.
- A manual compaction request is one flag that the compacting reply clears, not a request id, because no reply of the scope can run alongside the one compacting; the [Limitations](#limitations) name the one writer that can.
- A completed approval moves to the end of the log, after the turns that happened while it waited, because its continuation saw them.
- The summary replays as the first history message instead of inside the system prompt, so a compaction never changes the cached prompt prefix, and minimal agents with a string system message see it too.
  After a mid-turn compaction it follows that turn's prompt instead, in the rewritten request, its resume, and later turns while that run replays first, because a fast model that reads the prompt last restarts the request even when the summary records it finished (evidence in `docs/dev/2026-10-10-compaction-unification-design.md`).
  It is marked as history, so it is never stored in a run and an approval resume rebuilds it with the rest of its history.
- Persisted replay fitting runs once before a reply; the mid-turn hook sizes every model request of the reply from the latest response's reported usage when the loop received one, the first request with the history size the reply's preparation found plus estimates of the rest of the request, and by estimate after a response that created a native checkpoint, which is billed for the transcript the checkpoint replaced.
  The rest includes today's system prompt, the tools, and the prompt's attachments, which preparation did not size; sizing the whole first request by estimate instead would fold Claude and Gemini conversations that the reported size shows to fit, since their estimates carry the family margin.
- The compaction check before a reply, and the replay plan after it, size history from the request size the provider reported for the scope's latest answer, plus that answer, minus the system prompt and tools that request was sized with, which its run publishes; without a usable reported size they use the history estimate ([token counting](token-counting.md)).
  Today's system prompt and tools are taken off instead when they are smaller, so an estimate's margin cannot undercount the history; subtracting today's when they have grown, such as after more tools or longer context files, would show history that shrank and overflow the window.
  The reported size wins even when the estimate is larger, because the estimate carries its family margin, which counts most text high (Claude's by up to half again, by the ratios in [token counting](token-counting.md#evidence)), and taking the larger count would compact conversations early on every reply.
  When that size is over the final budget, the plan trims by estimates against the budget scaled by the estimate's ratio to the reported size, since only estimates can size a candidate subset of runs.
  The history estimate keeps its margin because no fixed tokenizer fits every provider: on one Qwen thread, tool results counted 1.49 times their characters / 4, tool call arguments 1.44 times, and assistant text 1.20 times, `o200k_base` still undercounted by about 10%, and a history estimated under the trigger went out as a 126,847-token request in a 128K window.
  The reported size is ignored once the history holds a native checkpoint, under replay limits, when that request did not replay the whole history (its plan was cut to its budget, a scheduled task's `history_limit`, or an authored replay limit since removed), so its run published no system prompt and tools, after another model answered, when the latest run carried media, and when a summary was written after that request started or in the same second, because each makes it describe content the reply does not replay, or count it with another tokenizer or billing basis.
  A native checkpoint stands in for the transcript before it only on its own route, and the response that created it was billed for that transcript; before any checkpoint, a native request sends the same history as any other.
  It can still overcount by reasoning resent inside the last tool loop, which can cause an early compaction, including on consecutive turns if that overhead recurs.
- A mid-turn compaction folds everything but the system prompt, the current prompt, the current turn's transient messages, and queued notices, summarizing the scope's stored runs after the latest summary row together with the running turn's exchanges.
  Earlier turns' transient messages (turn context such as memory results and the approval receipt) are stored with their runs and replay where they were sent, so a later turn's request can extend the previous one whole, which Codex's prompt cache needs; a summary leaves them out because they are context, not conversation.
  It reads those runs from storage, not from the running loop's session, because that session still holds every run loaded when the turn started, so a second fold of the same turn would summarize them again.
  The request keeps each tool batch whole, but the summarizer reads every folded message as its own unit, because a model can read several large files in one parallel batch, and a batch larger than one summary input would be cut short and lose the later results.
  Each tool result names its own tool and arguments in the summary input, so it reads correctly apart from its call message, including for providers whose calls carry no ids.
- A fold appends one summary row and, in the same transaction, stores the messages it took and the member runs that had ended as rows of its running run, flagged with that row, because a run is stored only when it ends; a running run without a row gets a cancelled one.
  A flagged message never replays again, whichever copy of the run is saved later, and the running run sorts after the row (a stored paused or checkpointed copy moves there).
  The row keeps the event ids the running run had answered and read at the fold, so a cut finds what the fold summarized even after that run's row is gone.
- A cut removes a run whole: when a removed run folded into an earlier summary row, the cut starts at that row, and the running run of every removed fold row is fenced, so a running turn whose row the cut removed stores nothing more.
  Retiring summaries also removes every run that folded mid-turn, because its stored part starts mid-turn and would replay without what it built on.
- The fold's in-memory effects (the request rewrite, the recorder's folded tool calls, the folded requests' usage in run metadata, and the live session summary) and its success notice happen together with its commit, even when the turn is cancelled meanwhile, so a stopped reply never reports a stored fold as failed.
  An interrupted record therefore leaves out exactly the calls the stored summary holds; its write also reads that set from the run's folded rows, so paths without a turn recorder and a restarted CLI approval recovery leave them out too.
- A fold survives a crash of its turn: the restarted attempt replays its summary and knows which tool calls already ran and what they returned, and an edit or redaction of the turn's message still removes it through the fold's row.
- Text compaction starts from the latest readable native checkpoint, because the provider already summarized everything its request replayed, and re-summarizing that history after a model switch took a local model over an hour.
  The checkpoint's text also covers its own run's prompt, so the summary built on it always folds that run; an edit or redaction of the prompt then removes that summary with the run.
  Each checkpoint records the summary and run ids its request replayed, and stands in only while those still precede its run, because a completed approval sorts after the turns it waited for and retiring a legacy summary keeps its runs; checkpoints recorded without this record are not reused.
  A native request that replays from a checkpoint not covering the runs before it records an empty basis, since its own checkpoints miss those runs too; a checkpoint of unknown history, such as one recorded before this record existed, counts as covering, as native replay assumes, and an approval continuation records what its paused request replayed.
  A checkpoint too large to leave room for its own run in the summary input is not used, so compaction still progresses.
- A summary input that shrank after a failure keeps that size for the compaction's later chunks, because they would otherwise repeat the failure, and a slow model spends its whole timeout on each one; a chunk still grows to hold its first run whole.
- A request without persisted replay (team members, workflow participants, scheduled tasks with `history_limit: 0`) compacts run-locally: its summary is an ordinary message of its own run and is stored with it.

## Limitations

A `compact_context` call running as a background tool job can request compaction while a later reply of the same scope compacts on an earlier request; that reply's clear drops the new request, which its compaction covers unless it fails.
Stored history grows until the conversation is deleted.
Folded requests' usage lives in the run's metadata, so it is lost when the run never saves, as a crashed run's usage is.
An approval resume loads a fresh session, which can replay turns stored after its run's mid-turn fold, so an edit or redaction of such a turn during the resume leaves the resuming run unfenced, because the run sorts before that turn, and its completion can store content derived from the cut turn.
A run paused before that turn has the same race (`tests/test_summary_rows.py::test_a_pending_approval_before_the_cut_survives`); both need an edit or redaction of a recent turn while an approval of an older turn is resuming.
A voice-call reply runs outside the conversation's lifecycle lock, so an edit or redaction of an earlier turn that lands after the reply folded mid-turn removes the reply's run row and fences it, and its answer is not stored.
