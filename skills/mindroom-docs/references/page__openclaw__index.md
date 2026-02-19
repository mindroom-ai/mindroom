# OpenClaw Workspace Import

MindRoom supports a practical OpenClaw-compatible workflow focused on workspace portability:

- Reuse your OpenClaw markdown files (`SOUL.md`, `AGENTS.md`, `USER.md`, `MEMORY.md`, etc.)
- Keep daily memory logs in `memory/YYYY-MM-DD.md`
- Use `openclaw_compat` tool names where supported
- Add semantic recall over historical memory via knowledge bases
- Apply context budgeting with pre-trim memory flush for long threads

## What this is (and is not)

MindRoom is compatible with OpenClaw workspace patterns, not a full OpenClaw gateway clone.

Works well:

- File-based identity and memory documents
- OpenClaw-inspired behavior and instructions
- `sessions_*`, `message`, `subagents`, `web_*`, `exec/process`, `cron` compatibility surface

Not included:

- OpenClaw gateway control plane (`gateway` returns `not_configured`)
- Device nodes, canvas, and browser platform tools
- `tts` and `image` tool aliases (use MindRoom's native TTS/image tools directly)
- Heartbeat runtime — schedule heartbeats via `cron`/`scheduler` instead

## The `openclaw_compat` toolkit

The `openclaw_compat` tool provides OpenClaw-named aliases so prompts and skills written for OpenClaw work without rewriting tool calls:

| OpenClaw tool                | MindRoom backend                  |
| ---------------------------- | --------------------------------- |
| `exec`, `process`            | `ShellTools`                      |
| `web_search`, `web_fetch`    | `DuckDuckGoTools`, `WebsiteTools` |
| `cron`                       | `SchedulerTools`                  |
| `message`, `sessions_*`      | Matrix client calls               |
| `subagents`, `agents_list`   | Agent registry lookup             |
| `gateway`, `nodes`, `canvas` | Stubs (`not_configured`)          |

## Drop-in config

Use this as a starting point for importing an OpenClaw workspace:

```
agents:
  openclaw:
    display_name: OpenClawAgent
    include_default_tools: false
    learning: false
    model: opus
    role: OpenClaw-style personal assistant with persistent file-based identity and memory.
    rooms: [personal]

    instructions:
      - You wake up fresh each session with no memory of previous conversations. Your context files are already loaded into your system prompt.
      - Today's and yesterday's daily memory files from `memory_dir` are pre-loaded. For older history, use `search_knowledge_base`.
      - IMPORTANT: If you want to remember something, write it to `./openclaw_data/memory/YYYY-MM-DD.md` (append, never overwrite).
      - Curate long-term memory in `MEMORY.md` by distilling important points from recent daily files.
      - Ask before external/public actions and destructive operations.
      - Before answering prior-history questions, search memory files first with `search_knowledge_base`.

    context_files:
      - ./openclaw_data/SOUL.md
      - ./openclaw_data/AGENTS.md
      - ./openclaw_data/USER.md
      - ./openclaw_data/IDENTITY.md
      - ./openclaw_data/MEMORY.md
      - ./openclaw_data/TOOLS.md
      - ./openclaw_data/HEARTBEAT.md

    memory_dir: ./openclaw_data/memory
    knowledge_bases: [openclaw_memory]

    tools:
      - file
      - shell
      - scheduler
      - duckduckgo
      - website
      - openclaw_compat
      - python
      - calculator

    skills:
      - transcribe

knowledge_bases:
  openclaw_memory:
    path: ./openclaw_data/memory
    watch: true

defaults:
  max_thread_messages: 50
  max_preload_chars: 50000
  memory_flush:
    enabled: true
    threshold_percent: 80
    timeout_seconds: 30

models:
  opus:
    provider: anthropic
    id: claude-opus-4-6-latest
    context_window: 200000  # Optional: used for budgeting and flush thresholds
```

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

## Memory behavior

`memory_dir` is document-based context loading, not Mem0:

- Loads `MEMORY.md` (uppercase, OpenClaw standard) plus yesterday/today dated files from the directory
- Injects those files into role context at agent creation/reload
- Does not perform semantic search by itself

**Case sensitivity note:** `memory_dir` lookup is case-sensitive and expects `MEMORY.md`. On case-sensitive filesystems (typical Linux), lowercase `memory.md` will not be loaded.

For older history, use `knowledge_bases` on your memory folder — this provides semantic search across all files regardless of date.

## Context Management

MindRoom now includes built-in context controls for OpenClaw-style agents:

- Thread history is capped to the most recent messages (`defaults.max_thread_messages`, overridable per agent with `agents.<name>.max_thread_messages`)
- Preloaded role context from `context_files` + `memory_dir` is hard-capped by `defaults.max_preload_chars`
- If preload exceeds the cap, truncation priority is: daily files -> `MEMORY.md` -> personality files (`SOUL.md`, etc.), with a truncation marker appended
- Prompt budgeting estimates tokens from role + instructions + prompt and trims oldest thread lines when near the context window threshold
- For agents with `memory_dir`, a silent pre-trim memory flush turn runs at `defaults.memory_flush.threshold_percent` before trimming (response hidden from users; file writes persist)

## Known limitations

**Threading model:** MindRoom responds in Matrix threads by default. OpenClaw uses continuous room-level conversations. On mobile or via bridges (Telegram, Signal, WhatsApp), this means you must navigate into threads to continue a conversation. See [#169](https://github.com/mindroom-ai/mindroom/issues/169) for the planned `thread_mode: room` option.

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
