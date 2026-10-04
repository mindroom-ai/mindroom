# Memory System

Memory lets agents remember facts, preferences, and context across conversations.
This page covers choosing a memory backend, configuring embeddings and extraction, file-based memory, the `memory` tool, and Agno Learning.

| Backend | What it does | Use it when |
|---|---|---|
| `mem0` (default) | Vector memory: relevant memories are retrieved before each reply and new ones are extracted automatically after each turn | You want hands-off semantic memory |
| `file` | Markdown files (`MEMORY.md` plus dated notes in `memory/`) that are the source of truth | You want memory you can read and edit, or an OpenClaw-style workspace |
| `none` | No built-in memory | The agent should keep no memories ([Agno Learning](#agno-learning) stays on unless disabled) |

<video controls playsinline preload="metadata" aria-label="The agent remembers each person's diet and plans the week's dinners" style="width: 100%">
  <source src="https://github.com/user-attachments/assets/75a32c85-7528-4b72-b268-0de8ec17f794#t=0.1" type="video/mp4" media="(prefers-color-scheme: dark)">
  <source src="https://github.com/user-attachments/assets/c4b6f6e3-03a9-40fd-808b-0d18abdaf263#t=0.1" type="video/mp4">
</video>

## Choosing a Backend

Set the global backend with `memory.backend` (`mem0`, `file`, or `none`; default `mem0`), and override it for one agent with `agents.<name>.memory_backend`.
`memory: none` is shorthand for `memory: {backend: none}`.

```yaml
memory:
  backend: mem0

agents:
  coder:
    display_name: Coder
    role: Write and review code
    memory_backend: file
  scratch:
    display_name: Scratch
    role: Stateless scratchpad agent
    memory_backend: none
```

With `none`, the agent gets no memories in its prompt, stores none after its turns, and does not receive the `memory` tool.
`none` does not turn off [Agno Learning](#agno-learning); set `learning: false` as well to disable all built-in memory and learning.
Conversation history is separate and stays in the session; see [History & Compaction](https://docs.mindroom.chat/configuration/history/).
OpenClaw-compatible agents use this same backend selection.

## Memory Scopes

| Scope | ID | Contents |
|---|---|---|
| Agent | `agent_<name>` | The agent's preferences and durable user context |
| Team | `team_<agent1>+<agent2>+...` (member names sorted) | Shared memory from team conversations |

An agent's memory reads cover its own scope and the scopes of the configured teams it belongs to, searched through the agent's own backend.
A team reads its own team scope; set `memory.team_reads_member_memory: true` (default `false`) to let it also read its members' agent scopes.

Team memory picks one backend from the members' effective backends:

- All members use `file`: the team uses file memory.
- All members use `none`: team memory is disabled.
- Any other combination, including `file` plus `none`, uses Mem0 with the configured `memory.embedder` and `memory.llm`.

A member's `none` setting disables only its own memory, so team conversations it takes part in can still be remembered in the team scope.
A `file` member of a team that uses Mem0 does not see that team's memory in its own conversations.

## Backend: `mem0`

Before each reply, Mem0 retrieves memories relevant to the message and adds them to the prompt.
After each turn, the [memory LLM](#memory-llm) extracts durable facts into a Chroma vector store under the agent's storage directory.

### Embedder

`memory.embedder` sets the embedding model used by Mem0, semantic file-memory search, and [knowledge bases](https://docs.mindroom.chat/knowledge/#embedder-configuration).
Mem0 supports the `openai` (default), `ollama`, `huggingface`, and `sentence_transformers` providers; semantic file memory and knowledge bases support the [narrower set](https://docs.mindroom.chat/knowledge/#embedder-configuration).

| Field | Default | Description |
|---|---|---|
| `provider` | `openai` | Embedding provider |
| `config.model` | `text-embedding-3-small` | Embedding model name |
| `config.host` | `null` | Base URL for OpenAI-compatible endpoints, or the Ollama host; for `ollama`, a configured `OLLAMA_HOST` takes precedence, and the fallback is `http://localhost:11434` |
| `config.dimensions` | `null` | Embedding dimension override (>= 1) for OpenAI-compatible providers |
| `config.api_key` | `null` | Explicit API key for the `openai` provider |
| `config.credentials_service` | `null` | Credential service that supplies the `openai` provider's key |

```yaml
memory:
  backend: mem0
  embedder:
    provider: openai
    config:
      model: text-embedding-3-small
      credentials_service: embedder # Optional strict credential binding
      dimensions: null              # Optional, for example 256
```

Fully local embedders:

```yaml
memory:
  embedder:
    provider: sentence_transformers
    config:
      model: sentence-transformers/all-MiniLM-L6-v2
```

```yaml
memory:
  embedder:
    provider: ollama
    config:
      model: nomic-embed-text
      host: http://localhost:11434
```

MindRoom installs the optional `sentence_transformers` extra the first time that provider is used.
Changing the embedder provider, model, host, or dimensions switches Mem0 to a separate collection, so memories stored with the previous embedder are no longer retrieved.

The `openai` embedder takes its API key from the first of these that is set:

1. `memory.embedder.config.api_key`.
2. The service named by `memory.embedder.config.credentials_service`; when it has no key, MindRoom does not fall back to another key.
3. When no service is named, the dedicated `embedder` credential (seeded from `EMBEDDER_API_KEY` or `EMBEDDER_API_KEY_FILE`), then the shared `openai` provider key.

Use `credentials_service` when the embedding endpoint needs a different key than chat, voice, or other OpenAI-compatible models.
With no key at all, keyless local OpenAI-compatible endpoints still work, while a keyed endpoint fails with an authentication error.
When the embedder rejects its credential, semantic memory search reports the cause in `memory` tool output and in the agent's prompt instead of returning empty results, and `/api/health` includes an `embedder` block until a request succeeds again.

### Memory LLM

`memory.llm` sets the model Mem0 uses to extract memories.
Supported providers are `ollama`, `openai`, `openrouter`, and `anthropic`.
When `memory.llm` is omitted, MindRoom uses `gemma4` on the Ollama server set by `OLLAMA_HOST` (default `http://localhost:11434`).
With `provider: ollama`, `config.host` points extraction at a different Ollama server, but a configured `OLLAMA_HOST` takes precedence; other providers ignore `host`.
With `provider: openai`, use `gpt-6-luna`; MindRoom drops the `temperature` and `top_p` values Mem0 sends, which GPT-6 models reject.

```yaml
memory:
  llm:
    provider: ollama
    config:
      model: gemma4
```

OpenRouter-only example, where both the memory LLM and the embeddings use the `openrouter` key:

```yaml
memory:
  embedder:
    provider: openai
    config:
      model: openai/text-embedding-3-small
      host: https://openrouter.ai/api/v1
      credentials_service: openrouter
      dimensions: 1536
  llm:
    provider: openrouter
    config:
      model: openai/gpt-6-luna
```

The LLM uses `memory.llm.config.api_key` when set and not blank, and otherwise the provider's credential (for example `openrouter`, or a dashboard credential stored as `OPENROUTER_API_KEY`).
When `OPENROUTER_API_KEY` is set in the process environment, the `openai` and `openrouter` memory LLMs send that key to OpenRouter, overriding `memory.llm.config.api_key`.

## Backend: `file`

File memory keeps `MEMORY.md` and dated notes under `memory/` in the agent's workspace, and those files are the source of truth.

```yaml
memory:
  backend: file
  file:
    max_entrypoint_lines: 200
  search:
    mode: keyword
    include:
      - memory/**/*.md
    include_entrypoint: false
  auto_flush:
    enabled: true
```

For a single agent, the file backend writes memory only when the agent uses the [`memory` tool](#memory) or edits the files, or when [auto-flush](#file-auto-flush-worker) is enabled; it does not extract memories after every turn like `mem0`.
A team whose members all use `file` appends each message it answers to the team's `MEMORY.md`, even without auto-flush.
Before each reply, MindRoom inlines the start of `MEMORY.md` into the prompt under a header with its path, so the agent does not need to re-read it, and adds memories that match the message.
`memory.file.max_entrypoint_lines` (default `200`, minimum `1`) caps the inlined lines; when it truncates, a marker reports the included and total line counts and the path to read for the rest.
`memory.file.path` (default unset, relative to the config directory) is a fallback root for team file memory and never moves agent file memory out of the workspace.

### File layout

Agent file memory lives in the agent workspace, `agents/<agent>/workspace/` in the storage directory:

- `MEMORY.md`, where `add_memory` writes
- `memory/YYYY-MM-DD.md`, where auto-flush writes

Team file memory is mirrored under each member's storage directory:

- `agents/<agent>/memory_files/team_<sorted_members>/MEMORY.md`
- `agents/<agent>/memory_files/team_<sorted_members>/memory/YYYY-MM-DD.md`

Memory files must be regular files; symlinks, FIFOs, and device nodes are ignored when reading and rejected when writing.
Each file is read up to 1 MiB, cut at its last complete line, and updating a truncated or non-UTF-8 file fails so the rest of it is preserved.

### Searching file memory

`memory.search` controls how `search_memories` and per-turn retrieval search file memory, and `agents.<name>.memory_search` overrides it per agent, with omitted fields inherited.

| Field | Default | Description |
|---|---|---|
| `mode` | `keyword` | `keyword` or `semantic` |
| `include` | `[memory/**/*.md]` | Glob patterns relative to the file-memory root that semantic search indexes; must be non-empty |
| `include_entrypoint` | `false` | Also index `MEMORY.md` for semantic search |

In `keyword` mode, `search_memories` matches structured entries with IDs in `MEMORY.md`, plus entries and plain-text lines in `memory/**/*.md`.
Ordinary prose in `MEMORY.md` reaches the agent through the prompt preload, not keyword search; to reach lines beyond the preload cap, read the file directly or use semantic search with `include_entrypoint: true`.

In `semantic` mode, MindRoom builds a vector index of the `include` files with `memory.embedder` on first use and stores it under `<storage-root>/knowledge_db/` (see [Knowledge storage](https://docs.mindroom.chat/knowledge/#storage)).
Until the index is ready, or when embeddings fail, search falls back to keyword results.
Writes through the `memory` tool and auto-flush refresh the index, but a ready index does not notice direct file edits until a later such write refreshes it.
Semantic mode covers the agent's own memory; team file memory is always keyword searched.

```yaml
agents:
  coder:
    display_name: Coder
    role: Write and review code
    memory_backend: file
    memory_search:
      mode: semantic
```

Once its semantic index is ready, an agent's file memory also appears as a read-only source in `search_knowledge_base`.
That source is semantic-only, without keyword fallback, team memory, or memory IDs, so use `search_memories` when you need those.

### Per-requester file memory

To give each requester their own file memory from one agent definition, combine `memory_backend: file` with [`private`](https://docs.mindroom.chat/configuration/agents/#private-instances); the requester's private root then becomes the file-memory root.

## File Auto-Flush Worker

Auto-flush periodically turns recent conversation in file-backed agents into durable memories.
It applies only to agents whose effective backend is `file`.

1. Each turn marks its session as having unsaved conversation.
2. A background worker picks eligible sessions in bounded batches.
3. The agent's own model reads the new messages, plus existing memories to avoid duplicates, and writes durable facts.
4. When the model answers with the `no_reply_token`, nothing is written.
5. Results are appended to `memory/YYYY-MM-DD.md`.

```yaml
memory:
  backend: file
  auto_flush:
    enabled: true
    flush_interval_seconds: 1800
    idle_seconds: 120
    max_dirty_age_seconds: 600
    batch:
      max_sessions_per_cycle: 10
    extractor:
      max_messages_per_flush: 20
```

| Field | Default | Description |
|---|---|---|
| `enabled` | `false` | Turn on auto-flush |
| `flush_interval_seconds` | `1800` (min 5) | Time between worker cycles |
| `idle_seconds` | `120` (min 0) | Session idle time before it becomes eligible |
| `max_dirty_age_seconds` | `600` (min 1) | Make a session eligible after this long with unsaved conversation, even if still active |
| `stale_ttl_seconds` | `86400` (min 60) | Forget pending sessions older than this |
| `max_cross_session_reprioritize` | `5` (min 0) | Other pending sessions of the same agent moved up the queue when a new message arrives |
| `retry_cooldown_seconds` | `30` (min 1) | Wait before retrying a failed extraction |
| `max_retry_cooldown_seconds` | `300` (min 1) | Upper bound for the growing retry wait |
| `batch.max_sessions_per_cycle` | `10` (min 1) | Sessions processed per cycle |
| `batch.max_sessions_per_agent_per_cycle` | `3` (min 1) | Sessions per agent processed per cycle |
| `extractor.no_reply_token` | `NO_REPLY` | Reply meaning nothing should be saved |
| `extractor.max_messages_per_flush` | `20` (min 1) | Messages considered per extraction |
| `extractor.max_chars_per_flush` | `12000` (min 1) | Message characters considered per extraction |
| `extractor.max_extraction_seconds` | `30` (min 1) | Timeout for one extraction, retried in a later cycle |
| `extractor.include_memory_context.memory_snippets` | `5` (min 0) | Existing memories shown to the model to avoid duplicates |
| `extractor.include_memory_context.snippet_max_chars` | `400` (min 1) | Characters per existing memory shown |

## [`memory`]

The `memory` tool lets an agent deliberately remember, look up, correct, or forget something, alongside automatic memory.
It has no tool-specific configuration; it always uses the agent's effective backend and the `memory.search` settings, and agents with `memory_backend: none` do not receive it.

```yaml
agents:
  assistant:
    tools: [memory]
```

```python
add_memory("The user prefers terse release notes.")
search_memories("release notes", limit=3)   # default limit 5
list_memories(limit=20)                     # default limit 50
get_memory("abc123")
update_memory("abc123", "The user prefers terse release notes with dates.")
delete_memory("abc123")
```

The tool reaches every agent and team scope visible to the agent, as described in [Memory Scopes](#memory-scopes).
Search and list results include an ID: Mem0 memory IDs and file-backed `file:<path>:<line>` IDs work with `get_memory`, `update_memory`, and `delete_memory`.
Semantic file-memory matches use read-only `semantic:<source_file>:<rank>` locators that these functions do not accept.
Result metadata includes `search_mode` (`keyword` or `semantic`), showing which search produced each result.
Failures are returned as readable error messages rather than raised into the conversation.

## UI Configuration

The Dashboard **Memory** page edits the `memory` section: backend, `team_reads_member_memory`, embedder provider, model, credential service, and host, file settings (`path`, `max_entrypoint_lines`), search settings, and all auto-flush settings.
The remaining fields, such as `llm`, are under **More settings**.
Save from the Memory page to write the changes to `config.yaml`.

## Agno Learning

Agno Learning builds a persistent profile of user preferences from conversations, separately from the memory backends above.
It is on by default for every agent, and `memory_backend: none` does not disable it.

```yaml
defaults:
  learning: true          # default
  learning_mode: always   # learn after every turn; agentic lets the agent decide via a tool

agents:
  assistant:
    learning: false       # disable for one agent
  research:
    learning_mode: agentic
```

See [Agent fields](https://docs.mindroom.chat/configuration/agents/#defaults) for how `learning` and `learning_mode` inherit from `defaults`.
Disabled agents create and update no learning data.
Learning data is stored in `learning/<agent>.db` under the agent's state root (`agents/<agent>/` in the storage directory for shared agents), so it survives container restarts when the storage directory is mounted.
