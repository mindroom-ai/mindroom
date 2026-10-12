# Compaction log store

Design note, October 12, 2026, written before any code and before design review.
Base: `origin/main` at `49d15f268`.
It covers two PRs: PR A replaces `main`'s compaction archive with a log store and keeps `main`'s behavior, and a rework of #2798 then expresses mid-turn compaction on that store.

## Why

`main`'s archive (`history/archive.py`) moves compacted runs out of Agno's runs table into `<session_table>_compacted_runs`, and a redaction rollback moves them back.
#2798 added summaries that cover part of a running turn while that turn's runs still live in the other table.
After #2798 switched to positioned generations on 2026-10-11, at least 13 `fix(history)` commits fixed consistency gaps between the two stores, and review rounds 14 to 18 each found new majors in run ordering across both tables, rollback boundaries, crash leftovers, and filtering folded tool calls on four paths.
Each extra rule exists because rows move between two stores or because a summary points at a run that is not stored yet.
#2798 adds about 1,940 production lines over `main`, most of them in that bookkeeping.

The user's direction: store each scope's history as one ordered, append-only log of turns and summary rows, keep #2798's user-visible behavior, and find the simpler implementation in the storage shape.

## Decision

- Agno's runs table, unchanged in schema, is the log of turns: a run is one row, ordered by `run_index`, and compaction never deletes or moves it.
- A new MindRoom table, `<session_table>_summaries`, holds summary rows, each positioned in that order right after the newest run it covers.
- A mid-turn compaction stores the part of the running turn written before the fold on its summary row; the turn's run row keeps only what follows the fold, which is what Agno already writes.
- Three operations cover every lifecycle event: append (a run or a summary row), load (the latest summary plus what comes after it), and cut (delete one element and everything after it); one insertion rule moves a run that becomes visible to the end.

## The log

### Elements and order

- A **run row** is a top-level row of Agno's runs table; its key is `(run_index, 0)`.
  Child rows (team member runs, `parent_run_id` set) have no key of their own: they are loaded and deleted with their parent, whatever their `run_index`.
- A **summary row** has these columns:

  | Column | Meaning |
  | --- | --- |
  | `id` | Creation order; breaks ties between rows at the same position. |
  | `session_id`, `scope_key` | Owner; cascades from the session row. |
  | `after_run_index` | The `run_index` of the newest run the row covers; `-1` sorts before every run. |
  | `summary`, `summary_model` | Cumulative summary text; `NULL` text makes the row a barrier (see Legacy). |
  | `event_ids` | Matrix event ids the row's own folded content read, answered, or wrote; redaction matches these. |
  | `seen_event_ids` | Event ids the row's own folded content counts as seen. |
  | `legacy` | A content-free summary written before the archive existed. |
  | `segment_run_id`, `segment` | Mid-turn only: the running run's id and its record before the fold, as Agno run JSON. `NULL` in PR A. |
  | `created_at` | Creation time. |

  Its key is `(after_run_index, id)`, so it sorts after the run at `after_run_index` and after earlier summary rows at the same position.
- Event sets are incremental: a row holds only what its own chunk or segment folded, and readers take the union over rows.
- The schema includes the segment columns from the start, so the #2798 rework adds no second schema change.

### The invariant

**I1**: every history-visible top-level run of a scope whose key precedes the scope's latest summary row is covered by that row or an earlier one.
History-visible means `is_model_history_visible_run`: top-level and not paused, cancelled, or errored.

These rules keep I1:

1. Compaction folds a prefix of the visible runs after the latest summary and appends one summary row per chunk, positioned after the chunk's last run.
2. A new run's `run_index` is one past the larger of the session's highest `run_index` and highest `after_run_index`, computed inside the inserting statement.
   `main` allocates from run rows only, so deleting the newest runs would let a new run reuse an index a summary row sorts after, which hides it.
3. A save that makes a stored non-visible row visible (an approval or CLI-approval continuation completing, an interrupted record replacing a cancelled run) moves that row to the end, with a fresh index from rule 2.
   This is the only reordering, and it puts the run where the model produced its answer: after the turns that happened while it waited.
4. A cut deletes a suffix of the log, so the remaining rows keep I1.

Rule 3 is needed even for `main`'s behavior: an approval paused before newer turns and a compaction, then resumed, must replay after the summary, and on `main` it does because it stayed live while the newer turns were archived.
A save that keeps a row's visibility never moves it, so a stale save of a folded run keeps its position and stays folded; that is why `main`'s resurrection guard is no longer needed.

### Loading

`get_session` returns exactly the runs `main` keeps live today:

- the visible top-level runs whose key follows their scope's latest summary row,
- every non-visible top-level run (paused, cancelled, errored), wherever it sits, because approval and CLI-approval continuations find their run in the loaded session (`approval/execution.py`, `teams.py`, Agno's `_acontinue_run` with `run_id`),
- and every descendant of those runs, because Agno's team code finds member runs in `session.runs` by `parent_run_id`.

The filter runs in SQL, so long conversations do not read their folded runs' `run_data` on every load.
`session.summary` is set on every read from the owner scope's latest summary row, so it is no longer a cache that a stale session-row write can leave behind.
Agno's `get_messages`, `num_history_runs`, and `num_history_messages`, and MindRoom's replay estimators and compaction selection, all see only these runs, as they see only live runs today.

### Ordering key

Rows record the run they sort after (`after_run_index`) and break ties by `id`.
A gapped or fractional position column was rejected: positions are only ever "after an existing run", so an integer of an existing run plus creation order is enough, and nothing has to renumber when rows are inserted between runs.

### Scopes

Each scope has its own conversation database: an agent scope uses the agent's database and a team scope its own `team_<name>_<digest>` database (`history/session_context.py`), and one `session.summary` field serves one owner scope.
A session therefore holds one top-level scope in practice; summary rows stay keyed by `scope_key` and the loader compares each run with its own scope's latest row, so a second scope in one session would still be handled.

### Seen ids and redaction candidates

- Seen ids are the union of the `seen_event_ids` of summary rows after the scope's latest barrier, the loaded visible runs' own seen ids, and the scope metadata ids no stored run carries, as on `main`.
- Redaction candidates are the union of the `event_ids` of summary rows after the latest barrier and every loaded top-level run's seen and source ids, as on `main`; these are checked against Matrix on every response.
- Rows keep these sets because reading folded runs' metadata from `run_data` on every response would scan the whole conversation; `main`'s archive keeps the same per-run copies for the same reason.

### Cut

`cut(key)` deletes, in one transaction, every top-level run of the scope with a key at or after `key` together with its descendants, and every summary row of the scope with a key at or after `key`.
Deleting a summary row "restores" the runs it covered without moving anything: they follow the new latest summary, so they load and replay again, which is what `main`'s `roll_back_to` does by moving rows back.

The cut point for a redacted event is the earliest element that holds it:

- the first loaded top-level run whose seen or source ids hold the event, and
- the earliest summary row whose `event_ids` hold it; within that row's range (after the previous summary row, up to the row) the first run whose metadata holds the event is the cut point, or the row itself when only its segment or legacy provenance holds it.

The range scan reads folded runs only when an event behind a summary is actually redacted, which is rare.
The existing loop that repeats until a pass removes nothing new stays, because a cut can bring older runs back into the candidate set.
After a cut the scope's metadata seen ids are dropped, as on `main`.

An edit cuts at the first loaded run that answered the edited event, as `remove_run_by_event_id(remove_following_runs=True)` does today; in the rework it also matches a summary row whose segment answered it.
Every existing caller passes `remove_following_runs=True`, so the flag goes.

## Agno mapping

### Rejected: the runs table as a segment log (handoff Option 1 as written)

- Segment rows as extra rows: a top-level segment counts as a run in `num_history_runs` and `get_messages`, and a child segment is adopted as a team member run by Agno's `_iter_member_runs_for_team_run` and its cancellation re-link in `team/_run.py`.
- Merging saves by message identity: MindRoom replaces stored runs on purpose (interrupted records in `history/interrupted_replay.py`, queued-notice finalization in `ai_runtime.py`, which removes duplicate notices), and a merge would bring removed messages back.
- Assembling a run from segments on read: nothing needs the pre-fold part after a fold except provenance, usage, and completeness; replay, approval resume, and later folds all want only the part after it.

### Rejected: Agno's runs table plus a separate MindRoom log for replay (Option 2)

Two stores of the same turns are the problem being removed.

### Chosen: whole runs in Agno's table, summary rows beside it

Agno keeps reading and writing whole runs; MindRoom adds a filter on read, two rules on run insertion, and its own table for summary rows.
The pre-fold segment lives on the summary row because the two are written together and always deleted together: a cut that reaches the segment reaches the row, and a cut that reaches the row reaches the run's post-fold part after it.

### Agno seams

| Seam | Change | Marker |
| --- | --- | --- |
| `SqliteDb._get_session_run_rows` and `_get_session_runs_data` (private, Agno 3.0.9) | `_ConversationSqliteDb` overrides both to add the loading filter; `get_session` keeps calling Agno. | New `AGNO_COMPAT`: session reads load every stored run, with no extension point to load the runs after a boundary; `runs_limit` only takes the last N. |
| `agno_compat_sqlite.insert_run_row` (copied Agno UPSERT) | Index allocation after every run and summary position (rule 2); a non-visible row saved visible moves to the end (rule 3). | Existing markers; their reasons and removal conditions change. |
| `agno_compat_sqlite.run_deletion_transaction` | Reused for `cut`, deleting run subtrees and summary rows in one transaction. | Existing marker; the legacy-blob part of its reason goes (see Migration). |
| `_ConversationSqliteDb.get_session` (public override) | Sets `session.summary` from the summaries table. | None; public method. |

## Behavior mapping

| Behavior | In the log |
| --- | --- |
| Pre-reply compaction folds every visible run chunk by chunk; a failed later chunk keeps the earlier chunks (`main`, #2798) | Each chunk appends a summary row after its last run; nothing is deleted; newer runs stay after it. |
| Mid-turn compaction of a stored conversation: one summary covers the scope's stored history and the turn so far (#2798) | One transaction appends a summary row holding the turn's pre-fold record as its segment, positioned after the newest run the fold read, and moves the running run's stored row, if any, to the end. The run row Agno writes at the end of the turn is the post-fold part. |
| An old paused approval resumed after newer turns, compacting mid-turn | The paused row stays where it is until its continuation saves it; the fold moves it after the new summary row, and completion moves it to the end. |
| A pending approval older than a summary can still resume | It loads as a non-visible run wherever it sits; a cut after it leaves it, as #2798 intends; `main` deleted it on an archive hit. |
| Editing the latest message regenerates its reply and removes compaction that depends on it | `cut` at the first element that answered the message: its run in PR A; in the rework also a mid-turn summary row whose segment answered it. |
| Redaction removes the first run that read, answered, or wrote the message, every later run, and compaction back to just before it | `cut` at the earliest element holding the event (see Cut). |
| Stopped or failed reply is saved without the tool calls a fold already summarized (#2798) | The fold makes the turn's recorded work so far the segment: the turn recorder starts a new segment and the run's tool trace keeps only unfinished calls, so every interrupted record is built from post-fold state. |
| Folded requests' usage still counts (#2798) | The run's usage snapshot, written in its save transaction, also takes the requests of its segments' records; run totals already come from Agno's cumulative run metrics; `mindroom_compacted_requests` goes. |
| Seen event ids | Union of summary rows' sets and loaded runs' ids, as today, from one log. |
| Stale saves of old runs | A save keeps the stored position unless it makes the row visible, so a folded run stays folded; `reconcile_compaction_state` and the archived-run guard go. |
| Team member runs | Children of their team run: loaded with it, deleted with it, positioned by it. |
| Content-free legacy summaries | A `legacy` row at `after_run_index = -1`; its event sets are the seen ids captured when it was adopted. Unchanged rules, see Legacy. |
| Summary replayed first, not in the system prompt (#2798) | Unchanged by storage; the rework keeps it. |

### Answers to the handoff's interrupted-record question

#2798 filters folded tool calls on four paths; in the log each one reads post-fold state once the fold resets it:

- Normal replies build interrupted records from `TurnRecorder.completed_tools`; the fold starts a new recorder segment.
- Voice calls (`matrix_rtc/call_tools.py` `_call_agent_run_state`) read the same recorder.
- CLI approval recovery (`agent_cli/cli_approval_recovery.py`) reads the persisted run's `tools`; the fold keeps only calls that have not finished, so the persisted trace holds none it summarized.
- Team member tools reach interrupted records through the team's recorder, so the recorder reset covers them; member runs stay their own child rows, which the fold does not touch.

The rework must check one consequence: whether any visible output renders a reply's tool list from `run_response.tools` after the fact, since the trace no longer holds pre-fold calls; live streaming already posted them.

## Mid-turn compaction on the log (the #2798 rework)

- The fold reads the scope's loaded visible runs, which are exactly the stored runs no summary covers; `_uncovered_runs` and `covered_run_ids` go.
- Its row's `after_run_index` is the newest run the fold covered, or the latest summary row's position when it covered none, so a run saved concurrently after the fold read the session sorts after the row and stays replayed.
- The commit and the fold's in-memory effects (recorder segment, tool-trace trim, live `session.summary`, request rewrite) complete together even when the turn is cancelled during the commit, so a stopped record never repeats the segment.
- The running run's stored row moves to the end in the commit transaction; this covers a resumed approval and a CLI checkpoint (`agent_cli/agno_compat_cli_checkpoint.py` saves the running run mid-turn as cancelled).
- Deleted: positioned generations (`through_run_id`, `covered_run_ids`, generation event ids), the applied, unapplied, and orphaned settlement, materialization, `mindroom_mid_turn_generation`, archived `run_index`, resurrection guards, reconciling in the builder before continuations, the usage carry-over metadata, and the four folded-tool filters.
- Kept: the summary as the first history message, the request hook (`agno_compat_model_hooks.install_request_preparation`), run-local compaction for unscoped targets, the removal of Vertex AI Claude per-request trimming, and the `LEGACY_COMPAT` handling of runs paused with an embedded summary block.
- The staged deletion of `docs/dev/2026-10-10-compaction-unification-plan.md` in the #2798 worktree lands with the rework.

## Crash after a mid-turn compaction: decision needed from the user

In the log, the segment and its summary row are stored when the fold commits, so after a crash they survive, and the restarted attempt (restarts continue in place, #2639) replays the summary of the work before the fold.
#2798 today deletes such a summary because the crashed run never recorded it.

- **Keep (recommended)**: no code; the restarted attempt knows which tool calls ran and their results, beyond the visible account its prompt carries, so it does not redo them; an edit or redaction of the turn's message still cuts the row through its segment.
- **Discard**: needs a liveness rule telling a crashed turn from one still running, which is the unapplied and orphaned machinery this design removes.

## Migration (PR A)

`migrate_compaction_database` runs once per conversation database when storage opens, before any response, under `BEGIN IMMEDIATE` with a re-check after taking the lock, as `history/legacy_compaction_state.py` does today; the summaries table's presence marks it done.
Per session it writes one run order and the summary rows:

1. Runs held only in an Agno 2 `runs` blob become run rows, in the order Agno's merge reads them, and the blob is cleared.
   Positions need every run to be a row; afterwards the blob merge never triggers, and `scrub_legacy_run_blobs` and its `LEGACY_COMPAT` rule go.
2. Archived runs with `run_data` return to the runs table in archive order, ahead of every run a read returns today, which keep the order that read returns them in (Agno 2 blob runs first); every row gets a fresh `run_index` in that combined order.
   Blob runs left after a compaction are non-visible, since compaction folded every visible one, so their place relative to archived runs changes no replay.
   `main` allocates indexes from live rows only, so archived and live indexes can collide and must be renumbered.
   A live copy of an archived run (a resurrected stale save that reconcile has not pruned yet) is dropped, as reconcile would.
   Restored rows write no usage; their usage rows already exist.
3. Each generation becomes a summary row with its summary, model, and `created_at`, positioned after its last archived top-level run, or after the previous row when it archived none; its event sets are the union of its archived runs' columns.
   Legacy generations become `legacy` rows at `-1` with their captured seen ids.
   Tombstones are dropped: they only pruned stale saves of removed runs, and no in-memory copy survives the restart that runs the migration.
4. A database that never got the archive (state from v2026.9.314 or earlier) adopts its destructive-compaction state directly as `legacy` rows, with the existing metadata cleanup.
5. The archive tables are dropped, with `history/archive.py` and `history/archive_schema.py`.

Each format gets its own `LEGACY_COMPAT` marker in `history/legacy_compaction_state.py`; the archive format's "Last legacy release" is the newest tag at merge time (today `v2026.10.268`; the archive shipped with #2351 on 2026-09-25), verified with `git tag --sort=-creatordate` before merging.
#2798's own schema additions were never released, so they need no migration once PR A lands first.
`docs/architecture/migrations.md` gains the archive row and updates the destructive-state and Agno 2 blob rows.

## Legacy summaries

Unchanged rules, expressed on the log:

- An event a legacy row may contain (its captured seen ids, or a source the legacy summary consumed per `summary_depends_on_source`) cuts at that row; since it sits at `-1`, that removes every run and summary row of the scope, as `clear_to_legacy` does.
- A cut that removes a run for any other event while a legacy row still has summary text sets every summary row's text of the scope to `NULL`, as `retire_summaries` does: runs behind the rows stay stored but neither replay nor count as seen, and runs after them keep replaying.
- Readers take summary event sets only from rows after the latest barrier (a row with `NULL` text), which is `main`'s "cumulative until a generation without one".

## Deliberate behavior changes in PR A

- A resumed approval or CLI approval replays after the turns that happened while it waited, instead of at its original place.
- A redaction that hits compacted history keeps runs that precede the cut, including an older pending approval; `main` deleted every live run on an archive hit.
- Redacting the source of a non-visible run that precedes a summary cuts from that run, removing the summary and the runs after it; `main` removed only that run and the live runs after it.
  It deletes more but never less, and in practice the next turn reads such an event as unseen thread context, so the cut lands at about the same place on `main`.
- Agno 2 blob runs become rows.
- The repair that dropped metadata seen ids when a stale session-row write had left an old cached summary goes with the cache; `main` already accepts that a stale whole-row write can restore metadata seen ids, which only withholds messages from unseen thread context.

## Not changed by PR A

- The summary stays in Agno's system prompt (`add_session_summary_to_context`), with no mid-turn compaction and Vertex AI Claude per-request trimming unchanged; those are #2798.
- Compaction selection, chunking, summary calls, retries, fallback, hooks, notices, and replay fitting.
- Edits still regenerate only the latest message.
- User docs change only where `docs/configuration/history.md` says compaction moves runs into an archive.

## Accepted limits

- A cut leaves no tombstones, so a save of a run loaded before a concurrent cut deleted it adds it back as a new run at the end, as a live-run deletion allows on `main`; `main` guarded only archived runs this way.
- The pre-fold segment's usage is lost when the run never saves, as a crashed run's usage is today.
- Stored history grows without bound, as the archive did; conversation deletion still removes everything.

## Testing

Failing tests first, at the owning seams: the summary-row module, `_ConversationSqliteDb` loading, `insert_run_row`, the redaction and edit cut, and the migration.

- `tests/test_compaction_fuzz.py` moves to the log, with invariants for I1, "compaction deletes and moves no run row", "loaded runs are exactly `main`'s live set", "replay is the latest summary plus later visible runs", and "a cut removes exactly the elements at or after it, with descendants", over generated histories with interrupted chunks, paused and resumed approvals, stale saves, edits, and redactions.
- Rule 2 after deleting the newest runs, and rule 3 for an approval, a CLI approval, and an interrupted record.
- Migration of a real-shaped `main` database: legacy, ordinary, barrier, and tombstone generations, colliding live and archived indexes, a resurrected live copy, member runs, and an Agno 2 blob, then every reader works after reopening.
- The full suite, pre-commit, and `uv run tach check --dependencies --interfaces`, and a live compaction run on two providers, including a redaction of compacted content.
