# Conversation Storage Rows

This record explains why conversation databases move from Agno's run layout to storage v2, where each run, stored message, and summary is a row of its own, what was rejected on the way, and what the change accepts.
It is the design behind #3095; paths are relative to `src/mindroom/` unless they start with `tests/` or `docs/`.

## Why reverse the 2026-10-20 rejection

The [runtime replacement](2026-10-20-replace-agno-design.md) rejected a new storage schema with a one-time run rewrite, because rewriting every stored run risked replay and approval regressions for no user-visible gain.
That held for the replacement, which changed no stored byte so that a regression would point at the runtime alone.
Storage v2 ships in a release of its own after the replacement has run for one, so a regression points at the format alone.
The gain is still not user-visible, but the Agno layout was the root cause of a recurring class of storage bugs, and most of the rules that kept it consistent existed only because of that layout.
The maintainer's bar was to do it only if the code became significantly cleaner and easier to maintain, ideally shorter, not counting the conversion module.
The checkpoint taken once the new store worked beside the old one measured the format change itself at about 30 more runtime lines, against an estimate of 160 fewer, and the maintainer continued on maintainability alone.
The regression risk is carried by the oracle and the backups instead: the old store's behavior was recorded before any new code existed, and every converted database keeps a byte-for-byte copy.

## Bug classes

From 2026-09-10 to 2026-10-24, five PRs that shipped on `main` had the storage format as their root cause and four more as a contributing cause.
Inside #2798, 31 of its 64 review fixes traced to the format, and on the replacement's branch five more did, plus seven in part.
They fall into four classes:

- Two stores of one conversation, which #2856 removed with the [log store](2026-10-12-compaction-log-store-design.md).
- Whole-run blobs written from stale or live in-memory copies: the frozen-run refusal and its parent chain, reconciliation of the loaded session after every save, the compaction write-back merge, and whole-run rewrites that add one fact, such as a reply link or delegation state.
- Folded content held outside its run: mid-turn fold segments on summary rows, their strip at every load, and the folded member ids kept in memory.
- JSON state in the session row: `session_state` and the metadata seen ids.

Rows per run, per message, and per summary remove the second and third classes: a full save is refused once its run is final, a single fact is patched onto the stored row, and a fold flags the message rows it took.
The fourth class stays, and so do the folded requests' usage in run metadata, because run-local folds write no rows, and the summary placement mark, because it is message content.

## Merging by message identity

The [log store record](2026-10-12-compaction-log-store-design.md#rejected-alternatives) rejected merging run saves by message identity, because MindRoom replaces stored runs on purpose, as interrupted records and queued-notice finalization do, and a merge would bring removed messages back.
Storage v2 keys message rows by message id but still does not merge: a full save makes the run's replay view exactly the saved list and deletes the unfolded rows the list no longer holds, so a removed message never returns.
Only rows a fold took are left alone, and they never replay.
A saved list that holds one id twice raises instead of keeping one copy, and the conversion gives a repeated id in an old run a derived one.

## The oracle

The PR's first commit recorded what the Agno-layout store did, before any new code existed, as fixtures under `tests/fixtures/storage_oracle/`.
A deterministic driver in `tests/storage_oracle/`, with seeded ids and clocks, applied actions from one vocabulary: the compaction fuzz operations plus message content, CLI checkpoints, delegation saves, notice finalization, reply links, interrupted records, discards, stale saves, crashes after a fold, edits, redactions, and legacy rows.
It recorded a digest of the observables after every action and the full observables at the end and after every cut, for generated seeds, for named sequences covering the cases the design reviews found, and for every storage golden and legacy fixture.
The observables are what callers see: loaded runs and their replay messages, the replay summary, the request built for a probe prompt, resume requests, seen ids, redaction candidates, folded tool calls, save and append outcomes, usage, and the runs background readers get.
Three comparisons run against the recordings: the v2 store driven by the same actions (`test_native.py`), each recorded input converted (`test_migrated.py`), and converted and native end states driven by further random actions (`test_migrated_vs_native.py`).
Each deliberate difference is a named mask that asserts the new behavior (`masks.py`); a difference no mask names was fixed at its root, never masked.
The recordings cannot be regenerated, because the Agno-layout store is gone, so a new action adds seeds against the v2 store only.
The oracle exists because each review round of the earlier storage designs found the next inconsistency by reading code, while the oracle checks the old store's whole action vocabulary at once.

## Rejected alternatives

- **Clean up in place.**
  Dropping Agno's dead columns, unused indexes, and duplicated facts saves lines at low risk, but keeps the frozen-run refusal, segments, reconciliation, and whole-run rewrites, which are the rules behind the bug classes.
- **An entry log.**
  The first draft appended every change as an entry and projected runs from their entries, but projections need patch ordering, replacement entries, and a derived first visible status, which the reviewers costed at about 50 more lines than rows for the same bug classes removed.
- **Keeping the Agno tables for a release, with triggers and reconversion.**
  Triggers would flag a write by an older release and a re-upgrade would convert again, but one database holds many conversations and the flag cannot name the ones the older release wrote, reconverting discards every turn written under v2, and a conversation v2 created would lose its whole history to the first run an older release stored.
  Tracking writes per conversation would be migration code for a downgrade nobody supports.
- **A database both releases can read.**
  Writing both layouts keeps two stores of the same turns, the class #2856 removed.
  The conversion instead drops the Agno tables and the five sessions columns no current code uses, so an older release started on a converted file archives its sessions directory loudly instead of writing rows a re-upgrade would ignore.
- **SQLite's online backup API for the backup.**
  It opens the destination by path, and worker code can write session directories, so the backup copies the file through descriptors of the pinned directory under the conversion's write lock instead.
- **A run's tool executions in one column of its run row.**
  SQLite rewrites a row with all its overflow pages whenever the row's size changes, so every save of a running run, such as the CLI checkpoint after each tool call, rewrote every earlier result: a 200-call turn with 20 KB results wrote about 4 MB on each late save and about 400 MB over the turn.
  Tool executions are rows of their own, keyed by their place in the run's list, which only grows while the run runs, so such a save writes its new call alone; requirements stay a column, since only a paused run holds them and they do not grow per call.
- **Keeping a tool result only in its tool message.**
  A tool message's content is not always its execution's result, since a job notice is appended to the message alone, and a fold hides the message from the replay view, so rebuilding the result would need a record of every difference, and the run row would still grow with every call's arguments.
- **Keying tool rows by call id.**
  A call stored without an id reads with one derived from what it stores, which two such calls share, and the conversion keeps only text ids, so ids need not be unique within a run, while places are.
- **Converting the background readers' cursors.**
  Memory auto-flush and the knowledge graph keep `(created_at, run_id)` cursors outside the databases, which the backups do not cover, so their approval wait stays and a follow-up can convert those cursors on their own.

## Assumptions

- Nothing writes a conversation database during the upgrade: the previous release is stopped, and the startup sweep runs after workers that mounted whole state roots are stopped.
- MindRoom never sets write-ahead-log mode, so leaving it before the backup concerns only a file another tool changed.
- The store already needs SQLite 3.35 for `RETURNING`, the release that added `DROP COLUMN`, so the conversion checks no version.
- Retries and continuations take fresh run ids, so treating a `RUNNING` row an older release left as final blocks no later save.

## Deliberate behavior changes

- A full save of a run whose stored status is visible is refused, not only one behind the latest summary, because a stale paused or cancelled save after a completion made the run pending again.
- An interrupted record may replace a covered completed run, so readers by id see what the user saw or heard; replay changes only once a cut uncovers the run.
- A member run is final once its team run is visible, which replaces the frozen parent chain, because members persist when they end, before their team run's terminal save.
- A paused team run's members are the member runs its requirements name, so a member that finished before its team run's save makes the resume fail instead of re-running that member from a stale embedded copy.
- A reply link reaches any stored run, and a covered run's new reply id joins the covering summary's event sets, so redacting that reply cuts the summary.
- Stored runs never hold history copies, which CLI checkpoint rows held and every reader skipped.
- A fold stores its running run as a cancelled row when the run has none, which a crash leaves behind as a run that never replays, as a crashed CLI checkpoint does.
- A fold's folded tool calls go with its run's rows, because after a discard or a retirement no stored record needs them.
- Delegation recovery reads children by id, so it finds a completed child that a later summary covers and a folded child's crash-left cancelled row.
- A minimal-mode delegation patches its state onto the turn's checkpoint row instead of replacing that row with the live running record, which made it visible and empty and could let another response's compaction freeze the turn's answer out of storage (reasoned from the code, not reproduced).
- A delegation save of a record that is not paused, or of a final run, patches only the delegation key.
- Background readers and skill review read legacy tool calls in the repaired shape replay already showed, because the conversion stores the repair.
- An Agno 2 blob run stored without `session_id` reads with its row's.
- A compaction that clears the manual compaction flag no longer triggers another compaction at the next reply, because the session-state copy of the request outlived the flag and asked again.
- A stale turn's save no longer overwrites another writer's newer session state, because the pending request's key in session state made such a save rewrite the whole state.

## Accepted limitations

- An approval resume loads a fresh session, which can replay turns stored after its run's mid-turn fold, so a cut of such a turn during the resume leaves the resuming run unfenced; a run paused before that turn already had the same race, and both are listed in the [compaction limitations](../architecture/compaction.md#limitations).
- A cut whose point precedes a fold's cancelled row removes that row like any later run, so a voice-call reply that folded, the only kind of turn that runs outside the conversation's lifecycle lock, is fenced and its answer is not stored.
  Keeping such a row would cost about six lines in the cut, for a race that needs a folding voice turn and a concurrent edit or redaction of an earlier turn.
- A downgrade restores the backups and loses every write made since the upgrade; [Upgrade Notes](../deployment/upgrades.md#upgrading-to-conversation-storage-v2) holds the operator procedure.
- A converted file keeps the pages its Agno tables used until later writes reuse them when the compaction after the conversion is skipped, because those pages are under a quarter of the file or the directory lacks twice the live data free, or when its `VACUUM` fails; [Upgrade Notes](../deployment/upgrades.md#upgrading-to-conversation-storage-v2) says how to reclaim them offline.
- Python's `sqlite3` cannot open a database without following links, so a sessions directory swapped for a link inside SQLite's own open still steers that one open, which writes the v2 tables at the link's target only when the file there is missing or empty; the conversion's checks before and after connecting refuse every earlier swap.
- Not verified: how often released databases hold the odd shapes the conversion handles, such as members of members, a repeated message id in one run, `RUNNING` rows, workflow rows, and runs that do not decode; whether any deployment runs in write-ahead-log mode; and how long the largest real databases take to convert.

## Follow-ups

- The session totals in `session_data` repeat what the usage ledger records, but retiring them needs a per-session baseline in the ledger for usage from before it existed and changes the usage reports, so it is a later change.
- Team seen ids in session metadata stay, because removing them changes what a discarded empty team turn marks as seen.
- The background readers' approval wait stays until their cursors get a conversion of their own.
