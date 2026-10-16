# History compaction

This page records the assumptions and decisions behind portable history compaction, its archive of compacted runs, and seen-message tracking.
User-facing behavior is in [Context Window](../configuration/history.md#context-window), Agno workarounds are in the [Agno compatibility inventory](agno-compatibility.md), and provider request workarounds are in `history/summary_provider_compat.py` and `history/claude_replay_compat.py`.
When upgrading Agno or a provider SDK, run those modules' contract tests before removing a workaround.

## Assumptions

Session rows are saved whole from in-memory snapshots, so a stale save can restore live runs that compaction archived, an older replayed summary, or seen ids that a redaction dropped.
The archive is therefore authoritative: `session.summary` is only a cache of the latest generation's summary, and history is reconciled against the archive before each run.

## Decisions

- Compaction archives the runs it folds instead of deleting them, so nothing compaction touches is lost and a redaction can roll back to the generation that first folded the redacted event.
  Summaries written when compaction deleted runs cannot be split by run, so while such a summary still replays, redacting an event it may contain clears it together with every later generation and all live runs.
- Seen ids are derived from live runs and the archive rather than stored, so they follow the remaining history after compaction or redaction.
  Archived runs keep the seen ids they counted while live, apart from the wider set that redaction matches, so compaction never changes which messages count as seen.
  Reconciliation that refreshes a stale cached summary, and every redaction change, drop the scope's metadata seen ids, because dropping ids can only re-offer messages, never hide them.
- Persisted replay fitting and the Vertex full-request guard stay separate because they measure different inputs at different boundaries.

## Limitations

Metadata seen ids, such as team consumption, have no archive counterpart, so a stale whole-row session write can restore ids that a redaction dropped; that only withholds those messages from unseen thread context.
