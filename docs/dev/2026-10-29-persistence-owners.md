# Persistence owners: one owner for each stored fact readers used to copy

Status: in progress (2026-10-29).
Scope: background readers of conversation logs (memory auto-flush, knowledge-graph ingestion, exploration), usage totals, the event journal's view declarations, thread-summary state, and per-scope history metadata.
The design went through several critique rounds before implementation; this record keeps what the code cannot show.

## Problem

Consumers of stored data kept or re-derived facts that one owner already held.

- Background readers sorted a conversation's runs by `(created_at, run_id)` and kept markers in that order, while the store's `seq` is the log's real order: a run saved visible over its paused, cancelled, or errored row takes the next `seq` and keeps its `created_at`, and edits and redactions cut by `seq`.
  A run that completed after an approval therefore landed behind a reader's marker and was never flushed, ingested, or offered as an exploration lead.
- The readers also held later turns back while a turn waited for approval, deciding when an approval could no longer finish from the current `timeout_days`, although each card's expiry is fixed when it is posted; lowering the setting made them skip a still-valid paused turn.
- Session totals in `session_data.session_metrics` were moved by a difference on every run save beside the usage ledger's per-run snapshots, and reports cross-checked the two.
- Every journal operation was declared as a sync function, an async forwarder, and a `Protocol` copy that existed only to narrow what each collaborator may call.
- Thread history was scanned six ways for summary state under two trust rules, so an authorized human's pinned summary counted as a message and reached the summarizer once an agent released the pin.
- Session metadata kept the force-compaction flag and preserved seen ids in maps keyed by scope, although every conversation database has one owner and every reader read only that owner's entry.

## Decisions

### Readers follow `seq`, and storage publishes every first completion

Readers consume each turn's first completion: a completed top-level turn is read once its `seq` passes the reader's position.
In-place rewrites of a completed turn (interrupted records over a final run, metadata patches) keep their `seq` and are not read again, as the old markers ignored them too.
A paused turn no longer holds back the turns after it, because it takes the next `seq` when it finishes.
A save that places a visible run at the log's end moves the session's update time even without usage, so the readers' update-time check sees an interrupted turn's canonical record saved completed over its cancelled row; checkpoint saves of a running turn stay quiet.
Each reader keeps its own progress (flush one position, ingestion per-kind positions with pieces of split runs, exploration the positions a check read through, committed when its run ends); the follower shares only ordered reads, eligibility, the due scan, and backoff.

### Redaction eligibility comes from the store's cut rule

A reader keeps the completed turns placed before the first run a redaction of any redacted candidate would remove, computed by the same holder selection and backward expansion through fold owners that `cut_at_event` uses.
Holders of every status count, so a paused or cancelled turn that read a redacted message keeps the later turns out, and a run that folded, paused, and completed after a later run cuts back to its fold row.
Legacy summary rows from before v2026.9.315 are not consulted; the agent's next reply removes those turns from history.

### Progress files convert when a reader next reads the conversation

An entry written by the released readers converts lazily, with the conversation's logged runs in hand, using the same first-sight rule a new conversation uses with the old marker as the floor.
No turn the old reader had not read is lost.
A turn the old reader read is read once more when an unread turn precedes it in `seq` (after an approval, or between turns created in the same second, whose creation order follows random run ids while `seq` follows writes); a split run ingested part-way is rendered again from its start.
A turn the old bug already skipped is not guaranteed to be recovered, because the old marker cannot tell it from one read normally.
Flush notes carry the `seq` of their batch's last turn; a note in the released format covers only turns at or before its run in creation order.

### The usage ledger is the only usage owner

Stored session totals counted ordinary runs only, so layout version 7 writes one `pre_ledger` ledger row per session whose stored run totals exceed what its run snapshots count, holding the positive residual, with per-model residuals only when they are nonnegative and sum to the residual.
The report keeps its schema-1 shape, because an external consumer reads the export and `docs/usage.md` documents an ownership formula over its fields.
Named report changes: the ledger's model attribution wins over contradictory stored session attribution; a counter whose stored total was below the ledger's sum reports the ledger's count; dated admin totals report the dated ledger rows instead of 0.

### Journal views are concrete classes

Each view keeps its name and becomes a concrete leaf class whose methods declare each operation once, over a typed helper that picks the read or write lane where the operation is declared.
`PrincipalStore` inherits the leaf views, so every annotation keeps its static narrowing and no caller changes.
Methods that coordinate several transactions, such as hydration's fenced chunk writes, stay async coordinators.

### One summary classifier

Each thread-history message is classified once as a trusted summary notice, an authorized human pin, or an ordinary message; human pins still never supply scheduling fields.
Consumers render the ordinary messages themselves, since the summarizer samples long threads and voice-call briefs keep every message with their own speaker labels.

### History metadata is one owner value

Layout version 7 moves the owner's entries into one `mindroom_history` value on both routes a database reaches the current layout by, after the Agno conversion's tombstone adoption has read other scopes' entries.

## Rejected alternatives

- A persisted exclusion list of already-read turn ids in each reader cursor: exact for the one-time repeat case, but a field and a filter on every read for a rare upgrade-time repeat.
- Converting every progress-file entry at worker start: it needs the config to map scope keys and graph names back to stores and reads every listed conversation at once.
- Re-deriving approval expiry from stored card rows inside the readers: a second owner of approval lifecycle that `seq` makes unnecessary.
- Journal views as separate objects built by `PrincipalStore` properties: over a thousand call-site edits for the same static narrowing.
- A generic `view.read(sync_fn)` at call sites: the lane choice would move to every call site, and any holder could run a write function in a read lane.
- Collapsing the report's cumulative and retained fields: it would break the export's consumer for no change in what is stored.

## Limitations

- Exploration has no redaction filter; automation checks are synchronous plugin API and the gap is accepted in `docs/architecture/security-posture.md`.
- Old-format summary rows are not consulted by the readers' redaction filter.
