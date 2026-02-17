# OpenClaw Workspace Import

MindRoom supports a practical OpenClaw-compatible workflow focused on workspace portability:

- Reuse your OpenClaw markdown files (`SOUL.md`, `AGENTS.md`, `USER.md`, `MEMORY.md`, etc.)
- Keep daily memory logs in `memory/YYYY-MM-DD.md`
- Use `openclaw_compat` tool names where supported
- Add semantic recall over historical memory via knowledge bases

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
      - Load identity and behavior from context files before acting.
      - Persist new context to daily memory files and curate long-term memory.
      - Use knowledge search for older history beyond recent daily files.
      - Ask before external/public actions and destructive shell operations.

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

## Known limitations

**Threading model:** MindRoom responds in Matrix threads by default. OpenClaw uses continuous room-level conversations. On mobile or via bridges (Telegram, Signal, WhatsApp), this means you must navigate into threads to continue a conversation. See [#169](https://github.com/mindroom-ai/mindroom/issues/169) for the planned `thread_mode: room` option.

**Context length:** Long conversations have no automatic truncation or compaction. If a thread exceeds the model's context window, the request will fail. See [#170](https://github.com/mindroom-ai/mindroom/issues/170) for the planned context management feature.

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
