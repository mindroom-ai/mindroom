# History compaction

This page records the assumptions and decisions behind portable history compaction, its summary rows, and seen-message tracking.
User-facing behavior is in [Context Window](../configuration/history.md#context-window), Agno workarounds are in the [Agno compatibility inventory](agno-compatibility.md), and provider request workarounds are in `history/summary_provider_compat.py` and `history/claude_replay_compat.py`.
When upgrading Agno or a provider SDK, run those modules' contract tests before removing a workaround.

## Assumptions

Session rows and runs are saved whole from in-memory snapshots, so a stale save can arrive after a compaction or a redaction has moved on.
The summary rows are therefore authoritative: `session.summary` is derived from the owner scope's latest row on every read, and run writes refuse a stale copy of a folded run or of a run a cut removed.
A compaction, a redaction, and an approval's completion can overlap on one scope, so each summary chunk checks, under the write lock, that the history it summarized is still the stored history before it appends.

## Decisions

- Compaction keeps every run in Agno's runs table and appends a summary row positioned after the newest run it covers, instead of moving runs into a second store, because keeping two stores consistent produced repeated bugs once summaries had to cover part of a running turn (see `docs/dev/2026-10-12-compaction-log-store-design.md`).
- Edits and redactions cut the log from the earliest element holding the event; rows that only end earlier summaries (barriers) survive cuts, so retired summaries never replay again.
  Summaries written when compaction deleted runs cannot be split by run, so while such a summary is in force, redacting an event it may contain cuts at it, removing every run and summary row of the scope.
- Seen ids are derived from the loaded runs and the summary rows after the latest barrier rather than stored, so they follow the remaining history after compaction or redaction.
  Every redaction change drops the scope's metadata seen ids, because dropping ids can only re-offer messages, never hide them.
- A completed approval moves to the end of the log, after the turns that happened while it waited, because its continuation saw them.
- Persisted replay fitting and the Vertex full-request guard stay separate because they measure different inputs at different boundaries.

## Limitations

Metadata seen ids, such as team consumption, have no summary-row counterpart, so a stale whole-row session write can restore ids that a redaction dropped; that only withholds those messages from unseen thread context.
Stored history grows until the conversation is deleted.
