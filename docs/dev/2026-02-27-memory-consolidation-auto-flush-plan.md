# MindRoom Memory Consolidation and Auto-Flush Plan

This record explains why file memory is the primary backend and why auto-flush extracts memories in the background, and it lists the parts of the plan that are not built.

## Goals

- Keep memory simple and portable by making files the source of truth.
- Preserve `mem0` as an alternative backend.
- Avoid user-managed schedules for memory maintenance.
- Keep cross-thread memory fresh for the same agent.
- Read sessions through Agno session and storage APIs instead of raw SQL, to minimize low-level coupling to Agno.

## Decisions

- Files are canonical because they keep memory portable and auditable; any semantic index over them is a secondary cache that can be discarded and rebuilt.
- When a new prompt arrives for an agent, that agent's other dirty sessions move up the flush queue, so a fact from a parallel conversation that just happened is saved sooner; a simple priority queue is enough for this.

## Rejected alternatives

- A pre-reply waiting window for pending flushes, and synchronous extraction while handling a request: both would put latency and timeout handling into the response path, so all flush work runs in the background and the reply never waits for it.

## Accepted limitations

- Cross-thread memory is eventually consistent: a fact is often available by the next turn, and sometimes within the same turn if the flush finishes quickly.
- A session's change is detected only by its last-update time, with no per-message cursor, so a backlog larger than one extraction window is not processed incrementally across cycles; each changed session re-extracts only its newest bounded window.
- Boot does not reset a persisted in-flight mark, so a session whose flush was interrupted by a crash is skipped until stale cleanup drops its entry.

## Not implemented

- Cursor-based backlog chunking, which would remove the extraction-window limitation above.
- The optional curation pass: auto-flush only appends to dated daily notes, and this pass would promote durable facts from them into the curated `MEMORY.md`, append-only and under strict caps.
