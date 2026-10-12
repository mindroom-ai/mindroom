# Compaction log store

Design note, October 12, 2026, written before any code.
Revised the same day after design review round 1 (GPT-6 Astra: 2 blockers, 5 majors; Claude Opus 5.5: 3 majors).
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
- A mid-turn compaction stores what it folded out of the running turn on its summary row; the turn's run row is what Agno writes, read without the folded messages.
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
  | `summary`, `summary_model` | Cumulative summary text; `NULL` text makes the row a barrier (see Legacy summaries). |
  | `event_ids` | Matrix event ids the row's own folded content read, answered, or wrote; redaction matches these. |
  | `seen_event_ids` | Event ids the row's own folded content counts as seen. |
  | `legacy` | A content-free summary written before the archive existed. |
  | `segment_run_id`, `segment` | Mid-turn only: the running run's id and what the fold took out of it (the folded messages, the tool executions they hold, and the member responses attached so far), as Agno run JSON. `NULL` in PR A. |
  | `created_at` | Creation time. |

  Its key is `(after_run_index, id)`, so it sorts after the run at `after_run_index` and after earlier summary rows at the same position.
- Event sets are incremental: a row holds only what its own chunk or segment folded, and readers take the union over rows.
- `<session_table>_removed_runs` (`session_id`, `run_id`) records every run a cut deleted, without content, so a later save of that run is dropped (see Cut).
- The schema includes the segment columns from the start, so the #2798 rework adds no second schema change.
  Both tables are created whenever a conversation database opens, so fresh files and databases that never compacted have them.

### The invariant

**I1**: every history-visible top-level run of a scope whose key precedes the scope's latest summary row is covered by that row's text.
History-visible means `is_model_history_visible_run`: top-level and not paused, cancelled, or errored.

These rules keep I1:

1. Compaction folds a prefix of the visible runs after the latest summary and appends one summary row per chunk, positioned after the chunk's last run.
2. A new run's `run_index` is one past the larger of the session's highest `run_index` and highest `after_run_index`, computed inside the inserting statement.
   `main` allocates from run rows only, so deleting the newest runs would let a new run reuse an index a summary row sorts after, which hides it.
3. A save that makes a stored non-visible row visible (an approval or CLI-approval continuation completing, an interrupted record replacing a cancelled run) moves that row to the end, with a fresh index from rule 2.
   This is the only reordering, and it puts the run where the model produced its answer: after the turns that happened while it waited.
4. Every summary append is a compare-and-append: in one write transaction it checks that the scope's latest summary row is still the one its input was built on and resolves `after_run_index` from the folded run's `run_id`, and otherwise writes nothing.
   A pre-reply chunk that loses the race ends that compaction as a failed chunk does; a mid-turn fold continues without compacting that request.
5. A cut deletes a suffix of the log, so the remaining rows keep I1.

Rule 3 is needed even for `main`'s behavior: an approval paused before newer turns and a compaction, then resumed, must replay after the summary, and on `main` it does because it stayed live while the newer turns were archived.
A save that keeps a row's visibility never moves it, so a stale save of a folded run keeps its position and stays folded; that is why `main`'s resurrection guard is no longer needed.
Rule 4 replaces nothing on `main`, where two compactions of one scope are not expected to overlap; with mid-turn folds, a fold and another response's pre-reply compaction can overlap, and whichever commits second would otherwise build on a stale summary.

### Transactions

SQLAlchemy's pysqlite driver opens a SQLite transaction only before the first write, so consecutive reads inside one SQLAlchemy transaction do not share a snapshot (verified on this stack: a row committed by another connection between two `SELECT`s in one `Session.begin()` is visible to the second).
Agno's own `get_session` therefore reads the session row and its runs without one snapshot.

- The loader reads the boundary, the summary text, and the runs either in one statement or inside an explicit `BEGIN`, so a concurrent append or cut can never pair a new summary with old runs or the reverse.
- Operations that read before they write (append, cut, retirement) start with `BEGIN IMMEDIATE`, as `history/legacy_compaction_state.py` does today.

### Loading

`get_session` returns exactly the runs `main` keeps live today:

- the visible top-level runs whose key follows their scope's latest summary row,
- every non-visible top-level run (paused, cancelled, errored), wherever it sits, because approval and CLI-approval continuations find their run in the loaded session (`approval/execution.py`, `teams.py`, Agno's `_acontinue_run` with `run_id`),
- and every descendant of those runs, because Agno's team code finds member runs in `session.runs` by `parent_run_id`.

A run that owns segments loads without the messages its segments hold, matched by Agno message id, which `Message.to_dict` stores.
That makes every stored copy of the run consistent with its summary rows, whichever copy was saved last: a paused or CLI-checkpointed row saved before the fold, a cancellation save of an older snapshot, or the post-fold record Agno writes at the end.
Only runs with segments pay for this, and only on load.

The filter runs in SQL, so long conversations do not read their folded runs' `run_data` on every load.
`session.summary` is set on every read from the owner scope's latest summary row, so it is no longer a cache that a stale session-row write can leave behind.
Agno's `get_messages`, `num_history_runs`, and `num_history_messages`, and MindRoom's replay estimators and compaction selection, all see only these runs, as they see only live runs today.
`get_run` still reads one row directly; its callers (`delegation/recovery.py`, `skill_learning/runner.py`) look runs up by id, so seeing a folded run there is harmless.

### Ordering key

Rows record the run they sort after (`after_run_index`) and break ties by `id`.
A gapped or fractional position column was rejected: positions are only ever "after an existing run", so an integer of an existing run plus creation order is enough, and nothing has to renumber when rows are inserted between runs.

### Scopes

Each scope has its own conversation database: an agent scope uses the agent's database and a team scope its own `team_<name>_<digest>` database (`history/session_context.py`), and one `session.summary` field serves one owner scope.
A session therefore holds one top-level scope in practice; summary rows stay keyed by `scope_key` and the loader compares each run with its own scope's latest row, so a second scope in one session is still handled.

### Seen ids and redaction candidates

- Seen ids are the union of the `seen_event_ids` of summary rows after the scope's latest barrier, the loaded visible runs' own seen ids, and the scope metadata ids no stored run carries, as on `main`.
- Redaction candidates are the union of the `event_ids` of summary rows after the latest barrier and every loaded top-level run's seen and source ids, as on `main`; these are checked against Matrix on every response.
- Rows keep these sets because reading folded runs' metadata from `run_data` on every response would scan the whole conversation; `main`'s archive keeps the same per-run copies for the same reason.

### Cut

`cut(key)` deletes, in one transaction, every top-level run of the scope with a key at or after `key` together with its descendants, and every summary row of the scope with a key at or after `key`, and records the deleted run ids in `<session_table>_removed_runs`.
Deleting a summary row "restores" the runs it covered without moving anything: they follow the new latest summary, so they load and replay again, which is what `main`'s `roll_back_to` does by moving rows back.

A run is deleted whole: when a deleted run owns a segment on a row before `key` (a run that folded, paused, and folded again after newer turns), the cut moves to that row and repeats until no deleted run owns an earlier segment.
In PR A no row has a segment, so the closure never applies.

A run insert whose `run_id` is in `<session_table>_removed_runs` is dropped inside the inserting statement.
`main` guards only archived runs this way (tombstones plus reconcile), and its fuzz test already covers compact, redact, then a stale save of the redacted run; guarding every cut is the simpler and stronger rule.
Regenerating an edited message creates a new run, so the guard never blocks it.

The cut point for a redacted event is the earliest element that holds it:

- the first loaded top-level run whose seen or source ids hold the event, and
- the earliest summary row after the latest barrier whose `event_ids` hold it; within that row's range (after the previous summary row, up to the row) the first run whose metadata holds the event is the cut point, or the row itself when only its segment holds it.

The range scan reads folded runs only when an event behind a summary is actually redacted, which is rare.
The existing loop that repeats until a pass removes nothing new stays, because a cut can bring older runs back into the candidate set.
After a cut the scope's metadata seen ids are dropped, as on `main`.

An edit cuts at the first loaded run that answered the edited event, as `remove_run_by_event_id(remove_following_runs=True)` does today; in the rework it also matches a summary row whose segment answered it.
Every existing caller passes `remove_following_runs=True`, so the flag goes.

Discarding an empty completed run (`ai_runtime.discard_empty_completed_run`) is not a cut: the run's summary rows stay, as after a crash, so the next prompt still has the work its fold summarized.

## Agno mapping

### Rejected: the runs table as a segment log (handoff Option 1 as written)

- Segment rows as extra rows: a top-level segment counts as a run in `num_history_runs` and `get_messages`, and a child segment is adopted as a team member run by Agno's `_iter_member_runs_for_team_run` and its cancellation re-link in `team/_run.py`.
- Merging saves by message identity: MindRoom replaces stored runs on purpose (interrupted records in `history/interrupted_replay.py`, queued-notice finalization in `ai_runtime.py`, which removes duplicate notices), and a merge would bring removed messages back.
- Assembling a run from segments on read: nothing needs the folded part of a run after the fold except provenance, completeness, and the interrupted-record filter; replay, approval resume, and later folds all want only the rest.

### Rejected: Agno's runs table plus a separate MindRoom log for replay (Option 2)

Two stores of the same turns are the problem being removed.

### Chosen: whole runs in Agno's table, summary rows beside it

Agno keeps reading and writing whole runs; MindRoom adds a filter and a message projection on read, three rules on run insertion (index allocation, the visibility move, the removed-run guard), and its own tables.
The folded part of a running turn lives on the summary row because the two are written together, and the cut closure deletes them together with the run.

### Agno seams

| Seam | Change | Marker |
| --- | --- | --- |
| `SqliteDb._get_session_run_rows` and `_get_session_runs_data` (private, Agno 3.0.9), or a MindRoom-owned `get_session` | The loading filter, the segment projection, and the summary text, read consistently (see Transactions). | New `AGNO_COMPAT`: session reads load every stored run, with no extension point to load the runs after a boundary (`runs_limit` only takes the last N), and they read the session row and runs without one snapshot. |
| `agno_compat_sqlite.insert_run_row` (copied Agno UPSERT) | Rules 2 and 3 and the removed-run guard. | These are MindRoom policy, so no new marker; the existing marker's removal condition gains "Agno lets the owner allocate the index and veto an insert inside its UPSERT". |
| `agno_compat_sqlite.run_deletion_transaction` | Reused for `cut`, deleting run subtrees and summary rows in one transaction. | Existing marker; the legacy-blob part of its reason goes (see Migration). |

The table layout lives in a schema-only module beside the summaries owner, as `history/archive_schema.py` does today, so `session_storage` can use it without importing the history runtime; `tach.toml` follows.

## Behavior mapping

| Behavior | In the log |
| --- | --- |
| Pre-reply compaction folds every visible run chunk by chunk; a failed later chunk keeps the earlier chunks (`main`, #2798) | Each chunk appends a summary row after its last run; nothing is deleted; newer runs stay after it. |
| Mid-turn compaction of a stored conversation: one summary covers the scope's stored history and the turn so far (#2798) | One transaction appends a summary row holding what the fold took out of the running turn, positioned after the newest stored run it covered, and moves the running run's stored row, if any, to the end. |
| An old paused approval resumed after newer turns, compacting mid-turn | The paused row stays where it is until the fold moves it after the new summary row; completion moves it to the end again; its stored copies load without the folded messages. |
| A pending approval older than a summary can still resume | It loads as a non-visible run wherever it sits; a cut after it leaves it, as #2798 intends; `main` deleted it on an archive hit. |
| Editing the latest message regenerates its reply and removes compaction that depends on it | `cut` at the first element that answered the message: its run in PR A; in the rework also a mid-turn summary row whose segment answered it. |
| Redaction removes the first run that read, answered, or wrote the message, every later run, and compaction back to just before it | `cut` at the earliest element holding the event, with the whole-run closure (see Cut). |
| Stopped or failed reply is saved without the tool calls a fold already summarized (#2798) | One filter in `build_interrupted_replay_snapshot`, fed by the folded calls of the run's segments (see below). |
| Folded requests' usage still counts (#2798) | The content-free request carry-over in run metadata (`mindroom_compacted_requests`) stays, for scoped and run-local folds alike. |
| Seen event ids | Union of summary rows' sets and loaded runs' ids, as today, from one log. |
| Stale saves of old runs | A save keeps the stored position unless it makes the row visible, so a folded run stays folded; a save of a cut run is dropped; `reconcile_compaction_state` goes. |
| Team member runs | Children of their team run: loaded with it, deleted with it, positioned by it. |
| Content-free legacy summaries | A `legacy` row at `after_run_index = -1`; its event sets are the seen ids captured when it was adopted. Unchanged rules, see Legacy summaries. |
| Summary replayed first, not in the system prompt (#2798) | Unchanged by storage; the rework keeps it. |

### Interrupted records after a fold

The four filters of #2798 become one, not zero.
Resetting accumulators does not work: the recorder's tool lists are replaced wholesale from the streaming tool tracker on every update (`ai_turn_state.py`), team interruption resynchronizes from the presentation tracker (`teams.py`), CLI recovery seeds its presentation from the paused trace (`agent_cli/cli_approval_recovery.py`), and Agno merges `tool_executions` back into `run_response.tools` (`agent/_response.py`).
`run_response.tools` also stays untouched, because the final reply, the paused-approval message, and team traces render from it (`ai.py`, `teams.py`), and #2798 promises the visible reply lists every tool call.

- `build_interrupted_replay_snapshot` leaves out every call the run's segments folded: tool-call ids in the segments' messages, the segments' tool executions, the tools of their member responses recursively, and entries whose `parent_bash_call_id` is folded.
- The recorder keeps the folded set each fold's commit returns; voice calls take `interrupted_snapshot()`, as #2798 does.
- CLI approval recovery rebuilds the set from the run's summary rows, so it also holds after a restart.
- The `mindroom_mid_turn_generation` metadata key goes; the folded set comes from storage or the commit.

## Mid-turn compaction on the log (the #2798 rework)

- Each fold reads, in its own transaction, the scope's latest summary row and the visible stored runs after it, excluding the running run, and seeds the summary from that row's text.
  Agno's live session keeps every run it loaded at the start of the turn, so reading it again would summarize stored runs a first fold of the same turn already covered.
- The fold builds its segment from the live request messages, because `RunOutput.messages` is not current inside Agno's model loop.
- Its row's `after_run_index` is the newest stored run it covered, or the latest summary row's position when it covered none, so a run saved concurrently after the read sorts after the row and stays replayed.
- The commit is a compare-and-append (rule 4), and it moves the running run's stored row to the end; this covers a resumed approval and a CLI checkpoint (`agent_cli/agno_compat_cli_checkpoint.py` saves the running run mid-turn as cancelled).
- The fold's in-memory effects (the recorder's folded set, the live `session.summary`, the usage carry-over, the request rewrite) complete together with the commit even when the turn is cancelled during it.
- Retiring summaries also deletes every run that owns a segment on a retired row, with its descendants, since such a run's stored part starts mid-turn and would replay without what it built on.
- The rework must confirm how a continuation interrupted after its fold restarts; whichever stored copy it starts from loads without the folded messages.
- Deleted: positioned generations (`through_run_id`, `covered_run_ids`, generation event ids), the applied, unapplied, and orphaned settlement, materialization, `mindroom_mid_turn_generation`, archived `run_index`, resurrection guards, reconciling in the builder before continuations, and four of the five folded-tool filters.
- Kept: the summary as the first history message, the request hook (`agno_compat_model_hooks.install_request_preparation`), run-local compaction for unscoped targets, the request usage carry-over, the removal of Vertex AI Claude per-request trimming, and the `LEGACY_COMPAT` handling of runs paused with an embedded summary block.
- The staged deletion of `docs/dev/2026-10-10-compaction-unification-plan.md` in the #2798 worktree lands with the rework.

## Crash after a mid-turn compaction: decision needed from the user

In the log, the segment and its summary row are stored when the fold commits, so after a crash they survive, and the restarted attempt (restarts continue in place, #2639) replays the summary of the work before the fold.
#2798 today deletes such a summary because the crashed run never recorded it.

- **Keep (recommended)**: no code; the restarted attempt knows which tool calls ran and their results, beyond the visible account its prompt carries, so it does not redo them; an edit or redaction of the turn's message still cuts the row through its segment.
- **Discard**: needs a liveness rule telling a crashed turn from one still running, which is the unapplied and orphaned machinery this design removes.

## Migration (PR A)

`migrate_compaction_database` runs once per conversation database when storage opens, before any response, under `BEGIN IMMEDIATE` with a re-check after taking the lock, as `history/legacy_compaction_state.py` does today; the summaries table's presence marks it done.
Per session it writes one run order and the summary rows:

1. Every live row and Agno 2 blob entry whose `run_id` appears in `_compacted_runs`, with or without `run_data`, is dropped with its descendants.
   `main`'s reconcile prunes such copies only before a reply, so a released database can hold a stale whole-run save written after a rollback, or blob residue a swallowed Agno scrub error left; after migration they would be ordinary visible runs.
2. Runs held only in an Agno 2 `runs` blob become run rows, and the blob is cleared; a database that never had a runs table gets one with Agno's schema, as `session_storage/preflight.py` uses `get_table_schema_definition`.
   Positions need every run to be a row; afterwards Agno's blob merge never triggers, and `scrub_legacy_run_blobs` goes, with the Agno 2 `LEGACY_COMPAT` rule moving to the migration beside `session_storage/legacy_session_storage.py`.
3. Archived runs with `run_data` come first, in archive order, then every run a read returns today, in that order (Agno 2 blob runs first, then rows by `run_index`, `created_at`, and `run_id`, which also places rows with a `NULL` index); every row gets a fresh `run_index` in that combined order.
   Archived runs are always a prefix of the order a read returned when they were folded, because each chunk folds the oldest visible runs, so this order is the one the conversation had; the non-visible runs compaction skipped load wherever they sit.
   `main` allocates indexes from live rows only, so archived and live indexes can collide and must be renumbered.
   Restored rows write no usage; their usage rows already exist.
4. Each generation becomes a summary row with its summary, model, and `created_at`, positioned after its last archived top-level run, or after the previous row when it archived none; its event sets are the union of its archived runs' columns, including generations whose summaries were retired but whose runs kept `run_data`.
   Legacy generations become `legacy` rows at `-1` with their captured seen ids.
   Tombstones themselves are not carried over: step 1 already removed every stored copy, and no in-memory copy survives the restart that runs the migration.
5. A database that never got the archive (state from v2026.9.314 or earlier) adopts its destructive-compaction state directly as `legacy` rows, with the existing metadata cleanup and the same step 1 for its tombstones.
6. The archive tables are dropped, with `history/archive.py` and `history/archive_schema.py`.

Each format gets its own `LEGACY_COMPAT` marker; the archive's schema has one released shape, first tagged in v2026.9.315 (#2351, 2026-09-25), and its "Last legacy release" is the newest tag at merge time (today `v2026.10.268`), verified with `git tag --sort=-creatordate` before merging.
#2798's own schema additions were never released, so they need no migration once PR A lands first.
`docs/architecture/migrations.md` gains the archive row and updates the destructive-state and Agno 2 blob rows.

## Legacy summaries

Unchanged rules, expressed on the log:

- While a legacy row still has summary text, an event it may contain (its captured seen ids, or a source the legacy summary consumed per `summary_depends_on_source`) cuts at that row; since it sits at `-1`, that removes every run and summary row of the scope, as `clear_to_legacy` does.
- A cut that removes a loaded run for any other event while a legacy row still has summary text sets every summary row's text of the scope to `NULL`, as `retire_summaries` does: runs behind the rows stay stored but neither replay nor count as seen, and runs after them keep replaying.
  A cut that starts at a folded run or a summary row does not retire, as `main` retires only when a live run went.
- Readers take summary event sets only from rows after the latest barrier (a row with `NULL` text), which is `main`'s "cumulative until a generation without one".

## Deliberate behavior changes in PR A

- A resumed approval or CLI approval replays after the turns that happened while it waited, instead of at its original place.
  Redacting one of those turns therefore also removes the approval's completed record, and redacting the approval's own source removes only it and the runs after its new place, where `main` cut every live run after its original index.
- A redaction that hits compacted history keeps runs that precede the cut, including an older pending approval; `main` deleted every live run on an archive hit.
- Redacting the source of a non-visible run that precedes a summary cuts from that run, removing the summary and the runs after it; `main` removed only that run and the live runs after it.
  It deletes more but never less, and in practice the next turn reads such an event as unseen thread context, so the cut lands at about the same place on `main`.
- A save of any run a cut deleted is dropped; `main` drops it only for archived runs.
- Agno 2 blob runs become rows.
- The repair that dropped metadata seen ids when a stale session-row write had left an old cached summary goes with the cache; `main` already accepts that a stale whole-row write can restore metadata seen ids, which only withholds messages from unseen thread context.

## Not changed by PR A

- The summary stays in Agno's system prompt (`add_session_summary_to_context`), with no mid-turn compaction and Vertex AI Claude per-request trimming unchanged; those are #2798.
- Compaction selection, chunking, summary calls, retries, fallback, hooks, notices, and replay fitting.
- Edits still regenerate only the latest message.
- User docs change only where `docs/configuration/history.md` says compaction moves runs into an archive.

## Accepted limits

- Folded requests' usage lives in the run's metadata, so it is lost when the run never saves, as a crashed run's usage is today; writing it as separate ledger rows was rejected because the run's own row already carries the cumulative totals, so a second row would either double-count them or split one run's request breakdown from its totals.
- Stored history grows without bound, as the archive did; conversation deletion still removes everything.

## Testing

Failing tests first, at the owning seams: the summary-row module, `_ConversationSqliteDb` loading, `insert_run_row`, the redaction and edit cut, and the migration.

- `tests/test_compaction_fuzz.py` moves to the log, with invariants for I1 (the latest row's text covers every visible run and segment before it), "compaction deletes and moves no run row", "loaded runs are exactly `main`'s live set", "replay is the latest summary plus later visible runs", "a cut removes exactly the elements at or after it, with descendants and the whole-run closure", and "a cut run never returns", over generated histories with interrupted chunks, paused and resumed approvals, stale saves, edits, and redactions, keeping the existing compact, redact, stale-save example.
- Rule 2 after deleting the newest runs; rule 3 for an approval, a CLI approval, and an interrupted record; rule 4 when another append commits first or the folded run is gone.
- Loading under a concurrent append and cut committed between the reads (deterministic interleaving).
- Legacy: a cut starting at a folded run does not retire; clearing requires a legacy row with text.
- Migration of real-shaped `main` databases: legacy, ordinary, barrier, retired-with-`run_data`, and tombstone generations, colliding live and archived indexes, a resurrected live copy of an archived and of a tombstoned run, blob residue of an archived run, an interrupted multi-chunk compaction with visible blob runs left, member runs, `NULL` `run_index` rows, a blob-only database without a runs table, and a session with two scopes; then every reader works after reopening.
- In the rework: two folds in one run, a pre-reply compaction committing during a long mid-turn run, a fold followed by a crash and by a delayed pre-fold save, the two-fold approval redaction from #2798's `tests/test_compaction_redaction.py`, retirement after a fold, interrupted records on all four paths (CLI recovery seeded from the paused trace, team member calls, a non-streaming final tool list), and run-local folds' usage.
- The full suite, pre-commit, and `uv run tach check --dependencies --interfaces`, and a live compaction run on two providers, including a redaction of compacted content.
