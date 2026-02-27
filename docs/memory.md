---
icon: lucide/brain
---

# Memory System

MindRoom supports two memory backends:

- `mem0`: vector memory (semantic retrieval + extraction via Mem0)
- `file`: markdown memory files (`MEMORY.md` plus optional dated notes)

You can switch backends with `memory.backend`.

## Memory Scopes

| Scope | User ID Format | Description |
|---|---|---|
| Agent | `agent_<name>` | Agent preferences and durable user context |
| Room | `room_<safe_room_id>` | Shared room/project context |
| Team | `team_<agent1>+<agent2>+...` | Shared team conversation memory |

Notes:
- Room IDs are sanitized (`:` -> `_`, `!` removed).
- Team IDs are sorted agent names joined by `+`.

## Backend: `mem0`

`mem0` keeps the existing behavior:
- semantic retrieval before response
- automatic extraction after turns
- storage in Chroma-backed Mem0 collections

Example:

```yaml
memory:
  backend: mem0
  embedder:
    provider: openai
    config:
      model: text-embedding-3-small
```

## Backend: `file`

`file` keeps memory in markdown files and treats files as source-of-truth.

Example:

```yaml
memory:
  backend: file
  file:
    path: ./mindroom_data/memory_files
    entrypoint_file: MEMORY.md
    max_entrypoint_lines: 200
```

### File layout

Under `memory.file.path` (or `<storage_path>/memory_files` by default), MindRoom stores per-scope folders such as:

- `agent_<name>/MEMORY.md`
- `room_<safe_room_id>/MEMORY.md`
- `team_<sorted_members>/MEMORY.md`

## File Auto-Flush Worker

When `memory.backend: file`, you can enable background auto-flush:

```yaml
memory:
  backend: file
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
      no_reply_token: NO_REPLY
      max_messages_per_flush: 20
      max_chars_per_flush: 12000
      max_extraction_seconds: 30
```

High-level behavior:

1. Turns mark sessions dirty.
2. Background worker picks eligible dirty sessions in bounded batches.
3. Worker runs a model-driven extraction (not keyword heuristics) to produce durable memories.
4. If extractor returns `NO_REPLY`, nothing is written.
5. Successful writes append to memory files via normal memory APIs.

## UI Configuration

The Dashboard **Memory** page supports:
- backend selection (`mem0` vs `file`)
- embedder provider/model/host
- file backend settings (`path`, `entrypoint_file`, `max_entrypoint_lines`)
- auto-flush settings (intervals, idle/age thresholds, retries)
- batch sizing
- extractor settings (`no_reply_token`, message/char/time limits, memory-context bounds)

Save from the Memory page to persist changes to `config.yaml`.

## Optional Memory Tool

For explicit agent-controlled memory operations, add the `memory` tool:

```yaml
agents:
  assistant:
    tools: [memory]
```

This exposes `add_memory`, `search_memory`, `get_all_memories`, and `delete_all_memories`.
