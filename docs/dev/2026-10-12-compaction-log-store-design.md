# Compaction log store

Design note, October 12, 2026, written before any code and trimmed in PR A (#2856) once the implementation existed, so it keeps only what the code cannot show.
Four design-review rounds (GPT-6 Astra and Claude Opus 5.5) found gaps in the transaction contract (stale writes, fencing, retirement, team members), not in the log model, so the design was patched rather than replaced; both approved it in round 4.
It covers two PRs: PR A replaces `main`'s compaction archive with a log store and keeps `main`'s behavior, and a rework of #2798 then expresses mid-turn compaction on that store.

## Why

`main`'s archive moved compacted runs out of Agno's runs table into a second table, and a redaction rollback moved them back.
#2798 added summaries that cover part of a running turn while that turn's runs still live in the other table.
After #2798 switched to positioned generations on 2026-10-11, at least 13 `fix(history)` commits fixed consistency gaps between the two stores, and review rounds 14 to 18 each found new majors in run ordering across both tables, rollback boundaries, crash leftovers, and filtering folded tool calls on four paths.
Each extra rule existed because rows moved between two stores or because a summary pointed at a run that was not stored yet; #2798 added about 1,940 production lines over `main`, most of them in that bookkeeping.

The user's direction: store each scope's history as one ordered, append-only log of turns and summary rows, keep #2798's user-visible behavior, and find the simpler implementation in the storage shape.

## Decision

- Agno's runs table, unchanged in schema, is the log of turns, and compaction never deletes or moves a run.
- A MindRoom table beside it holds summary rows, each positioned right after the newest run it covers; replay is the latest summary plus every run after it.
- A mid-turn compaction (the rework) stores what it folded out of the running turn on its summary row, because the two are written together and a cut must delete them together.
- Every lifecycle event is an append, a load, or a cut (delete one element and everything after it); one insertion rule moves a run that becomes visible to the end.

## Rejected alternatives

- **The runs table as a segment log** (the handoff's Option 1 as written): a top-level segment row counts as a run in `num_history_runs` and `get_messages`, and a child segment is adopted as a team member run by Agno's `_iter_member_runs_for_team_run` and its cancellation re-link in `team/_run.py`.
- **Merging run saves by message identity**: MindRoom replaces stored runs on purpose (interrupted records, queued-notice finalization that removes duplicate notices), and a merge would bring removed messages back.
- **Assembling a run from segments on read**: after a fold nothing needs a run's folded part except provenance, completeness, and the interrupted-record filter; replay, approval resume, and later folds all want only the rest.
- **Agno's runs table plus a separate MindRoom log for replay** (Option 2): two stores of the same turns are the problem being removed.
- **Overriding Agno's private run queries instead of owning the session read**: they would need a side channel to carry the summary out and would still read the session row outside one snapshot.
- **A gapped or fractional position column**: positions are only ever "after an existing run", so an existing run's index plus creation order is enough, and nothing renumbers when rows land between runs.
- **Separate ledger rows for folded requests' usage**: the run's own row already carries the cumulative totals, so a second row would double-count them or split one run's request breakdown from its totals.

## Why each insertion and append rule exists

The invariant: every visible run between a scope's latest barrier (a row without text) and its latest summary row is covered by that row's text, and every run behind the latest barrier is retired, neither replaying nor counting as seen.

- New runs take an index past both the highest run index and the highest summary position, because `main` allocated from run rows only, so deleting the newest runs would let a new run reuse an index a summary sorts after, which hides it.
- A non-visible run saved visible (an approval or CLI-approval continuation completing, an interrupted record replacing a cancelled run) moves to the end, because the model produced its answer after the turns that happened while it waited; this is the only reordering, and `main` reached the same replay order by keeping the paused run live while newer turns were archived.
- Every summary append is a compare-and-append against the session as the response loaded it, because a fold and another response's pre-reply compaction can overlap once folds happen mid-turn, and whichever commits second would otherwise build on a stale summary; a redaction or retirement that lands before the chunk reads its basis must void it too, or redacted or retired text is summarized again (found in PR A review).
- A row never sorts behind the latest summary row, because a racing compaction can leave identical summary text, and a smaller stale chunk would otherwise return folded runs to replay (found in PR A review).
- A visible run behind the latest summary row, or a member of one, is never rewritten, because a stale paused or completed copy would replay folded content or hide provenance from redaction; non-visible rows behind the summary stay writable so skipped approvals still complete.
  Together with the visibility move this replaces `main`'s resurrection guard.
- A save of a run a cut removed writes no row but still records its usage, because the provider calls behind it were made; `main` guarded only archived runs this way, and guarding every cut is simpler and stronger.
- Barrier rows survive cuts, because the runs and summaries behind them were retired and are no longer checked for redaction, so they must never replay again; `main` reached the same end by nulling every generation's text.

## Evidence and assumptions

- SQLAlchemy's pysqlite driver opens a SQLite transaction only before the first write, so consecutive reads inside one SQLAlchemy transaction do not share a snapshot (verified on this stack: a row committed by another connection between two `SELECT`s in one `Session.begin()` is visible to the second), and Agno's own `get_session` reads the session row and its runs without one snapshot.
  The loader therefore reads inside an explicit `BEGIN`, and every operation that reads before it writes starts with `BEGIN IMMEDIATE`.
- Non-visible runs load wherever they sit because approval and CLI-approval continuations find their run in the loaded session (Agno's `_acontinue_run` with `run_id`), and member runs load with their parent because Agno's team code finds members in `session.runs` by `parent_run_id`.
- Each scope has its own conversation database, so a session holds one top-level scope in practice; rows stay keyed by scope anyway.
- Summary rows keep their own event sets because reading folded runs' metadata on every response would scan the whole conversation; `main`'s archive kept per-run copies for the same reason.
- Retries and regenerations get fresh run ids, so the removed-run fence never blocks them.
- Discarding an empty completed run is not a cut: its summary rows stay, as after a crash, so the next prompt still has the work its fold summarized.

## Migration reasoning (PR A)

- Stored and blob copies of archived and tombstoned runs are dropped, because `main`'s reconcile pruned them only before a reply, so a released database can hold a stale whole-run save written after a rollback, or blob residue a swallowed Agno scrub error left, which would otherwise become ordinary visible runs.
- Archived runs come first, in archive order, then the runs a read returned, because each chunk folded the oldest visible runs, so archived runs were always a prefix of the conversation's order; `main` allocated indexes from live rows only, so archived and live indexes collide and every session is renumbered.
- Agno 2 blob runs and rows without a position become positioned rows, because positions need every run to be a row and the loader has no blob merge.
- Tombstones are not carried over: their stored copies are already dropped, and no in-memory copy survives the restart that runs the migration.
- Seen ids match `main` after reconcile except one case: metadata seen ids that `main`'s reconcile dropped when the cached summary differed from the newest generation, as after an interrupted summary write, are kept; that only withholds those messages from unseen thread context, and the design drops that repair at runtime anyway.
- #2798's own schema additions were never released, so they need no migration once PR A lands first.

## Legacy summaries

Summaries written when compaction deleted runs cannot be split by run, so `main`'s rules stay:

- While one is in force, redacting an event it may contain cuts at it; it sits before every run, so the cut removes every run and summary row of the scope.
- A redaction that removes a loaded run holding the event while one is in force retires the scope's summaries with a barrier, whether the cut started at that run or earlier, because `main` decided from whether a live run holding the event went before it consulted the archive.
- Edit cuts never consult legacy state, as `main`'s edit path never did.

## Deliberate behavior changes in PR A

- A resumed approval or CLI approval replays after the turns that happened while it waited, instead of at its original place.
  Redacting or editing one of those turns therefore also removes the approval's completed record, which is consistent because its continuation saw that turn, and redacting the approval's own source removes only it and the runs after its new place.
- A redaction that hits compacted history keeps the runs before the cut, including an older pending approval; `main` deleted every live run on an archive hit.
- Redacting an event that a non-visible run before a summary read or answered cuts from that run, removing the summary and the runs after it; it deletes more but never less than `main`, and the next turn reads such an event as unseen thread context anyway.
- Edits reach runs a summary covers; on `main` an edit of a message whose run a later compaction folded left that summary in place, so the regenerated reply replayed a summary of the reply it replaced.
- A cut affects one scope; redaction ignores rows behind a barrier; a save of any run a cut deleted is dropped; Agno 2 blob runs become rows.
- Compaction writes back only the message edits it made, onto the run as stored now, so an approval that completes or a reply event linked during a summary call is kept; `main` could write its stale working copy over them.

## Accepted limits

- Folded requests' usage lives in the run's metadata, so it is lost when the run never saves, as a crashed run's usage is.
- Stored history grows until the conversation is deleted, as with the archive.

## Mid-turn compaction on the log (the #2798 rework)

Implemented in #2798 on top of PR A; the summary rows' `segment_run_id` and `segment` columns, created by PR A, hold each fold's segment.


- Each fold reads, in its own transaction, the scope's latest summary row and the visible stored runs after it, excluding the running run, and seeds the summary from that row's text; with no stored run and no summary row, its row sits before every run.
- A first-turn fold creates the session row the summaries table cascades from, as `save_independent_usage(initial_session=...)` does, since Agno writes it only when the run ends; Agno's live session keeps every run it loaded at the start of the turn, so reading it again would summarize stored runs a first fold of the same turn already covered.
- The fold builds its segment (the folded messages, the tool executions they hold, and the member responses attached so far, from the live tree because Agno writes member rows only when the team run ends) from the live request messages, because `RunOutput.messages` is not current inside Agno's model loop.
- Its row sorts after the newest stored run it covered, or at the latest row's position when it covered none, so a run saved concurrently after the read sorts after the row and stays replayed.
- The commit is a compare-and-append that also refuses a removed segment owner, and it moves the running run's stored row to the end; this covers a resumed approval and a CLI checkpoint, which saves the running run mid-turn as cancelled.
- A run that owns segments loads without the messages its segments hold, matched by Agno message id, on a per-read copy after Agno's run-object cache (which keys on the stored `run_data` text alone), so every stored copy of the run stays consistent with its summary rows whichever copy was saved last.
- A cut deletes a run whole: when a deleted owner owns a segment on an earlier row (a run that folded, paused, and folded again after newer turns), the cut moves to that row and repeats; a cut also records the owner of every deleted segment as removed, which covers a running turn with no stored row yet.
- Retiring summaries also deletes every segment owner of the scope with its descendants, because such a run's stored part starts mid-turn and would replay without what it built on.
- The fold's in-memory effects (the recorder's folded set, the live `session.summary`, the usage carry-over, the request rewrite) complete together with the commit even when the turn is cancelled during it.
- A continuation interrupted after its fold does not resume from its paused row: an approved run cut short releases its continuation and the reply replays as a new attempt, and CLI recovery restarts from the checkpoint row, which loads without the folded messages.
- Interrupted records keep one filter, not zero: resetting accumulators does not work, because the recorder's tool lists are replaced wholesale from the streaming tool tracker on every update (`ai_turn_state.py`), team interruption resynchronizes from the presentation tracker (`teams.py`), CLI recovery seeds its presentation from the paused trace (`agent_cli/cli_approval_recovery.py`), and Agno merges `tool_executions` back into `run_response.tools` (`agent/_response.py`); `run_response.tools` stays untouched because the final reply, the paused-approval message, and team traces render from it, and #2798 promises the visible reply lists every tool call.
  `build_interrupted_replay_snapshot` leaves out every call the run's segments folded (tool-call ids in their messages, their tool executions, the tools of the member responses they hold, recursively, and entries whose `parent_bash_call_id` is folded); the recorder keeps the folded set each fold's commit returns, voice calls take `interrupted_snapshot()`, and CLI approval recovery rebuilds the set from the run's summary rows so it holds after a restart.
- Deleted by the rework: positioned generations (`through_run_id`, `covered_run_ids`, generation event ids), the applied, unapplied, and orphaned settlement, materialization, `mindroom_mid_turn_generation`, archived `run_index`, resurrection guards, reconciling in the builder before continuations, and four of the five folded-tool filters.
- Kept by the rework: the summary as the first history message, the request hook (`agno_compat_model_hooks.install_request_preparation`), run-local compaction for unscoped targets, the content-free request usage carry-over in run metadata (`mindroom_compacted_requests`), the removal of Vertex AI Claude per-request trimming, and the `LEGACY_COMPAT` handling of runs paused with an embedded summary block.

## A crash after a mid-turn compaction

In the log, the segment and its summary row are stored when the fold commits, so after a crash they survive, and the restarted attempt (restarts continue in place, #2639) replays the summary of the work before the fold; #2798 before the rework deleted such a summary because the crashed run never recorded it.

- **Kept** (implemented, and reversible on request): no code; the restarted attempt knows which tool calls ran and their results, beyond the visible account its prompt carries, so it does not redo them; an edit or redaction of the turn's message still cuts the row through its segment.
- **Rejected, discard**: needs a liveness rule telling a crashed turn from one still running, which is the unapplied and orphaned machinery this design removes.
