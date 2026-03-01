# OpenClaw Workspace Import

MindRoom supports a practical OpenClaw-compatible workflow focused on workspace portability:

- Reuse your OpenClaw markdown files (`SOUL.md`, `AGENTS.md`, `USER.md`, `MEMORY.md`, etc.)
- Use `openclaw_compat` tool names where supported
- Use MindRoom's unified memory backend (`memory.backend`) for persistence
- Optionally add semantic recall over workspace files via knowledge bases

## What this is (and is not)

MindRoom is compatible with OpenClaw workspace patterns, not a full OpenClaw gateway clone.

Works well:

- File-based identity and memory documents
- OpenClaw-inspired behavior and instructions
- `sessions_send`, `sessions_spawn`, `list_sessions`, `message`, `agents_list`, `web_*`, `exec/process`, `cron`, `browser` compatibility surface

Not included:

- OpenClaw gateway control plane
- Device nodes and canvas platform tools
- `tts` and `image` tool aliases (use MindRoom's native TTS/image tools directly)
- Heartbeat runtime - schedule heartbeats via `cron`/`scheduler` instead

## The `openclaw_compat` toolkit

The `openclaw_compat` tool provides OpenClaw-named aliases so prompts and skills written for OpenClaw work without rewriting tool calls:

| OpenClaw tool                                                     | MindRoom backend                                            |
| ----------------------------------------------------------------- | ----------------------------------------------------------- |
| `exec`, `process`                                                 | `ShellTools`                                                |
| `web_search`, `web_fetch`                                         | `DuckDuckGoTools`, `WebsiteTools`                           |
| `cron`                                                            | `SchedulerTools`                                            |
| `message`                                                         | Matrix client calls                                         |
| `agents_list`, `sessions_send`, `sessions_spawn`, `list_sessions` | `SubAgentsTools` (also available standalone as `subagents`) |
| `browser`                                                         | `BrowserTools` (Playwright, host target only)               |

Memory is not a separate OpenClaw subsystem in MindRoom. It uses the normal MindRoom memory backend.

## Drop-in config

Use this as a starting point for importing an OpenClaw workspace:

```
agents:
  openclaw:
    display_name: OpenClawAgent
    include_default_tools: false
    learning: false
    memory_backend: file
    memory_file_path: ./openclaw_data
    model: opus
    role: OpenClaw-style personal assistant with persistent file-based identity and memory.
    rooms: [personal]

    instructions:
      - You wake up fresh each session with no memory of previous conversations. Your context files are already loaded into your system prompt.
      - Important long-term context is persisted by the configured MindRoom memory backend. If something must be preserved exactly, write/update the relevant file directly.
      - MEMORY.md is curated long-term memory; daily files are short-lived notes and logs.
      - Ask before external/public actions and destructive operations.
      - Before answering prior-history questions, search memory files first with `search_knowledge_base` when configured.

    context_files:
      - ./openclaw_data/SOUL.md
      - ./openclaw_data/AGENTS.md
      - ./openclaw_data/USER.md
      - ./openclaw_data/IDENTITY.md
      - ./openclaw_data/TOOLS.md
      - ./openclaw_data/HEARTBEAT.md

    knowledge_bases: [openclaw_memory]

    tools:
      - openclaw_compat
      - python

    skills:
      - transcribe

knowledge_bases:
  openclaw_memory:
    path: ./openclaw_data/memory
    watch: true

memory:
  file:
    max_entrypoint_lines: 200
  auto_flush:
    enabled: true
```

`memory_file_path` points the file-memory scope directly at the workspace root, so `MEMORY.md` is loaded automatically by the file backend as the entrypoint — no need to list it in `context_files`. `memory_file_path` is ignored unless the effective backend is `file`; if you switch this agent to `mem0`, re-add `MEMORY.md` to `context_files` when you still want it preloaded. The `openclaw_compat` toolkit already bundles shell, file operations, web search, web fetch, browser, and scheduler aliases, so listing those tools individually is not necessary.

## Recommended workspace layout

```
openclaw_data/
├── SOUL.md
├── AGENTS.md
├── USER.md
├── IDENTITY.md
├── MEMORY.md
├── TOOLS.md
├── HEARTBEAT.md
└── memory/
    ├── YYYY-MM-DD.md
    └── topic-notes.md
```

## Unified memory behavior

OpenClaw-compatible agents use the same memory system as every other MindRoom agent:

- `memory.backend: mem0` for vector memory (global default)
- `memory.backend: file` for file-first memory (global default)
- `memory_backend: file` on an individual agent to override the global default
- `memory_file_path: ./openclaw_data` to point the file-memory scope at an existing workspace directory instead of the default `<root>/agent_<name>/`
- Agents that use file memory without `memory_file_path` continue to use the global `memory.file.path` (or the default `<storage_path>/memory_files/`)
- optional `knowledge_bases` for semantic recall over arbitrary workspace folders

Recommended for OpenClaw-style setups: `memory_backend: file` with `memory_file_path` pointing at the workspace root and `memory.auto_flush.enabled: true`.

## Context Management

MindRoom includes built-in context controls for OpenClaw-style agents:

- **Conversation history** is managed by Agno's session system - previous turns (including tool calls and results) are automatically replayed. Control depth with `num_history_runs` or `num_history_messages` (see [Agents](https://docs.mindroom.chat/configuration/agents/index.md)).
- **Preloaded role context** from `context_files` is hard-capped by `defaults.max_preload_chars`.

## Known limitations

**Threading model:** MindRoom responds in Matrix threads by default. OpenClaw uses continuous room-level conversations. To match this behavior on mobile or via bridges (Telegram, Signal, WhatsApp), set `thread_mode: room` on the agent - this sends plain room messages with a single persistent session per room instead of creating threads.

## Privacy guidance

`context_files` apply to all rooms for that agent. If `MEMORY.md` is sensitive:

- Keep the agent in private rooms only, or
- Split into private/public agents and exclude sensitive files from the public agent

## Skills

Skills are loaded from `~/.mindroom/skills/<name>/`. To use an OpenClaw skill like `transcribe`, copy the skill directory from your OpenClaw workspace:

```
mkdir -p ~/.mindroom/skills
cp -r /path/to/openclaw-workspace/skills/transcribe ~/.mindroom/skills/
```

Set required environment variables (for example `WHISPER_URL`) as defined in the skill's `SKILL.md` frontmatter.
