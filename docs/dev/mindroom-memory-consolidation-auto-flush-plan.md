# MindRoom Memory Consolidation and Auto-Flush Plan

Last updated: 2026-02-27

## Goals

- Keep memory simple and portable by making files the source of truth.
- Preserve `mem0` as an alternative backend.
- Avoid user-managed schedules for memory maintenance.
- Keep cross-thread memory fresh for the same agent.
- Use Agno session/storage APIs, not raw SQL.

## Core Decisions

1. **Primary backend:** file-based memory (`MEMORY.md` + daily files).
2. **Alternative backend:** `mem0` remains supported as an explicit option.
3. **Indexing model:** semantic index is secondary/cached and rebuildable; files are canonical.
4. **Flush visibility:** memory flush runs silently; `NO_REPLY` means no Matrix output.

## File Model

Per memory scope (agent/room/team), keep:

- `MEMORY.md` for curated long-term memory.
- `daily/YYYY-MM-DD.md` for short-term append logs.
- Optional topic files (for manual organization).

## State Model (Disk-Backed)

Use a JSON state file as durable flush control state:

- Path: `mindroom_data/memory_flush_state.json`
- Purpose: source of truth for dirty sessions and flush watermarks across restarts.
- Keys per `(agent_name, session_id)`:
  - `dirty`: bool
  - `last_session_updated_at`: int | null
  - `last_flushed_at`: int | null
  - `last_flushed_session_updated_at`: int | null
  - `room_id`: str | null
  - `thread_id`: str | null
  - `last_seen_at`: int
  - `in_flight`: bool (ephemeral safety; can be reset on boot)

## Triggering Strategy

### 1) On Turn Complete

When an agent finishes a turn, mark that session dirty in the state JSON.

### 2) Periodic Background Flush

Run a lightweight loop every `flush_interval_seconds`:

- load dirty sessions from state JSON,
- flush only sessions that are idle and have new Agno updates since last flush.

### 3) Opportunistic Cross-Session Flush (Same Agent)

When a new prompt arrives for agent `A` in session `S`:

- enqueue **other dirty sessions** for `A` (not `S`) at higher priority,
- so facts from parallel conversations are flushed sooner.

This is the desired behavior for "I mention something from another thread that just happened."

## Execution Model: Background-Only Queue

All flush work runs in background workers. The reply path never waits for memory flush.

- No pre-reply waiting window.
- No synchronous extraction in user request handling.
- New prompts can reprioritize queued jobs for the same agent.

This keeps cross-thread freshness while avoiding latency/timeout complexity in the response path.

## Eligibility Rules (Before Flush)

A session is flush-eligible only if all conditions hold:

1. session is marked dirty;
2. not currently in-flight;
3. session idle for at least `idle_seconds`;
4. Agno session exists via `create_session_storage(...).get_session(..., SessionType.AGENT)`;
5. `AgentSession.updated_at` is newer than `last_flushed_session_updated_at`.

## Flush Execution (Agno API Only)

For each eligible session:

1. Read session via Agno DB API (`SqliteDb.get_session`), deserialize to `AgentSession`.
2. Pull recent conversation context from `AgentSession.get_chat_history(...)` using a bounded extraction window.
   - Use a session watermark/cursor so each flush handles only new turns.
   - If backlog exceeds one window, process in multiple incremental chunks across cycles.
3. Run a silent extraction turn (same model family) with instructions:
   - write durable memory only,
   - append to `daily/YYYY-MM-DD.md`,
   - return `NO_REPLY` if nothing worth storing.
   - include a small bounded context from existing memory files (recent daily tail + relevant `MEMORY.md` snippets) for dedupe quality.
4. If result is `NO_REPLY` (or empty), write nothing.
5. If non-empty, append to daily file (and optionally curate `MEMORY.md` in a separate curation pass).
6. Update state JSON watermark fields and clear `dirty`.

No message is sent to Matrix for this maintenance flow.

## Cross-Session Freshness Guards

To avoid runaway work and duplication:

- cap opportunistic reprioritization per request (`max_cross_session_reprioritize`, e.g. 5),
- dedupe queue jobs per `(agent_name, session_id)`,
- optional per-agent concurrency limit (default 1),
- enforce per-session lock.

## Eventual Consistency Tradeoff

Cross-thread memory is eventually consistent:

- often available by the next turn,
- sometimes available in the same turn if background flush completes quickly,
- never delays the user reply.

## Config Surface (Proposed)

```yaml
memory:
  backend: file # file | mem0
  auto_flush:
    enabled: true
    flush_interval_seconds: 180
    idle_seconds: 120
    max_dirty_age_seconds: 600
    stale_ttl_seconds: 86400
    max_cross_session_reprioritize: 5
    batch:
      max_sessions_per_cycle: 10
      max_sessions_per_agent_per_cycle: 3
    extractor:
      no_reply_token: "NO_REPLY"
      max_messages_per_flush: 20
      max_chars_per_flush: 12000
      max_extraction_seconds: 30
      max_retries: 3
      include_memory_context:
        daily_tail_lines: 80
        memory_snippets: 5
        snippet_max_chars: 400
    curation:
      enabled: false
      max_lines_per_pass: 20
      max_passes_per_day: 1
      append_only: true
```

## Why This Design

- No manual user schedule needed.
- Preserves portability and auditability with file-first memory.
- Keeps semantic indexing optional and disposable.
- Handles parallel conversations for the same agent with a simple priority queue.
- Uses existing Agno abstractions, minimizing low-level coupling.
- Makes extraction-window and worker throughput explicit/tunable via config.

## Implementation Phases

1. Add JSON state manager (`memory_flush_state.json`) and mark-dirty hooks on turn completion.
2. Add background queue + worker using Agno session APIs and silent `NO_REPLY` extraction.
3. Add same-agent cross-session job reprioritization on new prompt (background-only).
4. Add stale cleanup and max-dirty-age fallback logic.
5. Add optional append-only curation pass (`daily` -> `MEMORY.md`) with strict caps.
6. Keep `mem0` as alternative backend path (no dual-write by default).
