# Memory System

MindRoom supports two memory backends:

- `mem0`: vector memory (semantic retrieval + extraction via Mem0)
- `file`: markdown memory files (`MEMORY.md` plus optional dated notes)

Set the global default backend with `memory.backend`. Override the backend per agent with `agents.<name>.memory_backend`. When an agent uses `memory_backend: file`, its file memory lives in its canonical workspace root. Use `agents.<name>.private` when one shared agent definition should keep file memory inside a requester-local private root. `private` changes where private files live. It does not switch the memory backend by itself.

OpenClaw compatibility uses this same backend selection; there is no separate OpenClaw-only memory engine.

Optional:

- `memory.team_reads_member_memory: true` allows team-context memory reads to include member agent scopes.

## Memory Scopes

| Scope | User ID Format               | Description                                |
| ----- | ---------------------------- | ------------------------------------------ |
| Agent | `agent_<name>`               | Agent preferences and durable user context |
| Team  | `team_<agent1>+<agent2>+...` | Shared team conversation memory            |

Notes:

- Team IDs are sorted agent names joined by `+`.

## Backend: `mem0`

`mem0` keeps the existing behavior:

- semantic retrieval before response
- automatic extraction after turns
- storage in Chroma-backed Mem0 collections

Example:

```
memory:
  backend: mem0
  embedder:
    provider: openai
    config:
      model: text-embedding-3-small
      dimensions: null             # Optional: embedding dimension override (e.g., 256)
```

Fully local embedder example:

```
memory:
  backend: mem0
  embedder:
    provider: sentence_transformers
    config:
      model: sentence-transformers/all-MiniLM-L6-v2
```

MindRoom auto-installs the optional `sentence_transformers` extra the first time this provider is used.

Ollama embedder example:

```
memory:
  backend: mem0
  embedder:
    provider: ollama
    config:
      model: nomic-embed-text
      host: http://localhost:11434
```

Supported embedder providers: `openai`, `ollama`, `huggingface`, `sentence_transformers`.

### Memory LLM

The memory system uses an LLM for extraction. Configure it with `memory.llm`:

```
memory:
  llm:
    provider: ollama    # ollama, openai, or anthropic
    config:
      model: llama3.2
```

Supported LLM providers: `ollama` (default), `openai`, `anthropic`.

## Backend: `file`

`file` keeps memory in markdown files and treats files as source-of-truth.

Example:

```
memory:
  backend: file
  file:
    max_entrypoint_lines: 200
```

`memory.file.path` is an optional fallback root for file-memory paths. It does not relocate canonical agent file memory (which always lives under the agent's workspace root). It can affect team file memory when the resolution determines the configured path should be used.

Per-agent override example:

```
memory:
  backend: mem0

agents:
  coder:
    display_name: Coder
    role: Write and review code
    memory_backend: file
```

For shared agents, file memory now lives directly under `agents/<name>/workspace/`. For requester-private agents, file memory lives directly under the effective private root. Use `private` when you need per-requester file-memory isolation.

Private instance example:

```
agents:
  mind:
    display_name: Mind
    role: A persistent personal AI companion
    memory_backend: file
    private:
      per: user
      root: mind_data
      template_dir: ./mind_template
```

In this setup, each requester gets their own private `mind_data/` root inside a canonical private-instance state root in shared storage. When `memory_backend: file` is enabled, that private root becomes the agent's effective file-memory root. If `./mind_template/` contains `MEMORY.md` and `memory/`, those files are copied into each private root on first use and then remain editable per requester. Later runs backfill newly added scaffold files without overwriting requester edits. MindRoom does not invent `MEMORY.md` or `memory/` for private agents. Put those files in your template directory if you want them scaffolded into each private root. If `memory_backend` is not `file`, `private` still creates private files and directories, but it does not make file memory active. Use `private` for requester-isolated workspaces.

### File layout

Agent file memory is stored under each agent's canonical workspace root:

- `agents/<agent>/workspace/MEMORY.md`
- `agents/<agent>/workspace/memory/YYYY-MM-DD.md`

Team file memory is mirrored under each participating agent's storage directory:

- `agents/<agent>/memory_files/team_<sorted_members>/MEMORY.md`
- `agents/<agent>/memory_files/team_<sorted_members>/memory/YYYY-MM-DD.md`

## File Auto-Flush Worker

When the effective backend is `file` for at least one agent, you can enable background auto-flush:

```
memory:
  backend: file
  auto_flush:
    enabled: true
    flush_interval_seconds: 1800
    idle_seconds: 120
    max_dirty_age_seconds: 600
    stale_ttl_seconds: 86400
    max_cross_session_reprioritize: 5
    retry_cooldown_seconds: 30       # Cooldown before retrying a failed extraction
    max_retry_cooldown_seconds: 300   # Upper bound for retry cooldown backoff
    batch:
      max_sessions_per_cycle: 10
      max_sessions_per_agent_per_cycle: 3
    extractor:
      no_reply_token: NO_REPLY
      max_messages_per_flush: 20
      max_chars_per_flush: 12000
      max_extraction_seconds: 30
      include_memory_context:
        memory_snippets: 5
        snippet_max_chars: 400
```

High-level behavior:

1. Turns mark sessions dirty.
1. Background worker picks eligible dirty sessions in bounded batches.
1. Worker runs a model-driven extraction (not keyword heuristics) to produce durable memories.
1. If extractor returns `NO_REPLY`, nothing is written.
1. Successful writes append to memory files via normal memory APIs.

## UI Configuration

The Dashboard **Memory** page supports:

- backend selection (`mem0` vs `file`)
- team/member read toggle (`team_reads_member_memory`)
- embedder provider/model/host
- file backend settings (`path`, `max_entrypoint_lines`)
- auto-flush settings (intervals, idle/age thresholds, retries)
- batch sizing
- extractor settings (`no_reply_token`, message/char/time limits, `include_memory_context` dedupe bounds)

Save from the Memory page to persist changes to `config.yaml`. Use the Dashboard **Agents** page to set an agent-specific **Memory Backend** override.

## Optional Memory Tool

For explicit agent-controlled memory operations, add the `memory` tool:

```
agents:
  assistant:
    tools: [memory]
```

This exposes `add_memory`, `search_memories`, `list_memories`, `get_memory`, `update_memory`, and `delete_memory`.

## Agno Learning

MindRoom integrates Agno's built-in Learning system, which lets agents learn and adapt from conversations. Learning is separate from the memory backends above — it uses Agno's own SQLite-backed storage in each agent's state root (`learning/`).

### Configuration

```
defaults:
  learning: true          # Enable learning for all agents (default: true)
  learning_mode: always   # "always" (extract after every turn) or "agentic" (agent decides via tool)
```

Per-agent override:

```
agents:
  assistant:
    learning: false       # Disable learning for this agent
  research:
    learning_mode: agentic  # Agent controls when to learn
```

| Field           | Type   | Default  | Description                                                                                      |
| --------------- | ------ | -------- | ------------------------------------------------------------------------------------------------ |
| `learning`      | bool   | `true`   | Enable Agno Learning for the agent                                                               |
| `learning_mode` | string | `always` | `always`: automatic extraction after every turn. `agentic`: agent decides via tool when to learn |

Agents inherit `learning` and `learning_mode` from `defaults` unless explicitly overridden. Disabled agents do not create or update learning state. Learning data persists in `agents/<name>/learning/<agent>.db` within the agent's state root.
