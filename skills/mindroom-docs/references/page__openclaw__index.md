# OpenClaw Workspace Import

This page covers moving an OpenClaw workspace into MindRoom: where to put the files, a starting agent config, the `openclaw_compat` tool preset, and what does not carry over.
MindRoom reuses OpenClaw's file-based workspace (`SOUL.md`, `AGENTS.md`, `USER.md`, `MEMORY.md`, daily notes, and skills); it is not a clone of the OpenClaw gateway.

## What carries over

Supported:

- File-based identity and instruction documents, loaded through `context_files`
- File-first memory (`MEMORY.md` plus `memory/` notes) through MindRoom's `file` memory backend
- OpenClaw skills, including `metadata.openclaw` eligibility gating
- A matching tool bundle through the [`openclaw_compat`](#openclaw_compat) preset, including Matrix messaging with `matrix_message`

Not included:

- The OpenClaw gateway control plane
- Device nodes and canvas platform tools
- OpenClaw tool aliases such as `exec`, `process`, `web_search`, `web_fetch`, `tts`, and `image`; use MindRoom's native tools instead
- The heartbeat runtime; schedule heartbeats with the `scheduler` tool instead (see [Scheduling](https://docs.mindroom.chat/scheduling/))

## Import a workspace

1. Copy or sync your OpenClaw files into the agent's workspace, `agents/<agent>/workspace/` in the storage directory (`mindroom_data/` by default):

    ```text
    mindroom_data/
    └── agents/
        └── openclaw/
            └── workspace/
                ├── SOUL.md
                ├── AGENTS.md
                ├── USER.md
                ├── IDENTITY.md
                ├── MEMORY.md
                ├── TOOLS.md
                ├── HEARTBEAT.md
                ├── memory/
                │   ├── YYYY-MM-DD.md
                │   └── topic-notes.md
                └── skills/
                    └── transcribe/
                        └── SKILL.md
    ```

2. Add an agent to `config.yaml`, starting from this example:

    ```yaml
    agents:
      openclaw:
        display_name: OpenClawAgent
        role: OpenClaw-style personal assistant with persistent file-based identity and memory.
        model: default
        rooms: [personal]
        include_default_tools: false
        learning: false
        memory_backend: file

        instructions:
          - You wake up fresh each session with no memory of previous conversations. Your context files are already loaded into your system prompt.
          - Important long-term context is persisted by the configured MindRoom memory backend. If something must be preserved exactly, write/update the relevant file directly.
          - MEMORY.md is curated long-term memory; daily files are short-lived notes and logs.
          - Ask before external/public actions and destructive operations.
          - Before answering prior-history questions, search memory files first with `search_memories`.

        context_files:
          - SOUL.md
          - AGENTS.md
          - USER.md
          - IDENTITY.md
          - TOOLS.md
          - HEARTBEAT.md

        tools:
          - openclaw_compat
          - memory
          - python

    memory:
      search:
        mode: semantic
      auto_flush:
        enabled: true
    ```

`context_files`, file memory, and `search_memories` all read this one workspace.
`context_files` paths are relative to it; see [File-Based Context Loading](https://docs.mindroom.chat/configuration/agents/#file-based-context-loading).

## [`openclaw_compat`]

`openclaw_compat` is a preset that belongs in an agent's `tools:` list and expands to `shell`, `coding`, `duckduckgo`, `website`, `browser`, `scheduler`, and `matrix_message`.
`matrix_message` also enables `attachments` and `matrix_room`, so the agent gets all nine tools.
The preset has no callable functions of its own, and listing it next to one of its member tools does not create duplicates.

The preset has no configuration fields and cannot be deferred.
Setting `defer` or `initial` on it fails with `'openclaw_compat' is a preset and cannot be deferred; defer/initial are only valid on individual tools.`
To configure or lazy-load a member tool, list that tool individually, and if you need only one or two of the tools, list them instead of the preset.

## Memory

OpenClaw-style agents use the same memory backends as every other agent; see [Memory System](https://docs.mindroom.chat/memory/) for backends, file layout, semantic search, and auto-flush.
The recommended setup is `memory_backend: file` with `memory.auto_flush.enabled: true`, as in the example above.

With the `file` backend, MindRoom loads `MEMORY.md` into every reply's prompt automatically, so leave it out of `context_files`.
If you switch the agent to `mem0` and still want `MEMORY.md` preloaded, add it to `context_files`.
Use [knowledge bases](https://docs.mindroom.chat/knowledge/) only for non-memory documents that should be searchable as external knowledge.

## Context and history

Preloaded `context_files` are capped by `defaults.max_preload_chars` (default `50000`); see [File-Based Context Loading](https://docs.mindroom.chat/configuration/agents/#file-based-context-loading) for what gets dropped when a workspace exceeds it.
How much conversation history each reply sees is set by `num_history_runs`, `num_history_messages`, and `compaction`; see [History & Compaction](https://docs.mindroom.chat/configuration/history/).

## Room conversations instead of threads

MindRoom replies in Matrix threads by default, while OpenClaw keeps one continuous conversation per room.
To match OpenClaw, especially on mobile or through bridges (Telegram, Signal, WhatsApp), set `thread_mode: room` on the agent; see [Threads, Replies & Participation](https://docs.mindroom.chat/configuration/threads/).

## Privacy

`context_files` and file memory load in every room the agent is in.
If `MEMORY.md` or another workspace file is sensitive, keep the agent in private rooms only, or split it into a private and a public agent and leave the sensitive files out of the public agent's workspace.

## Skills

Skills in `agents/<agent>/workspace/skills/<name>/` load for that agent without being listed in `skills:`, so copying the OpenClaw workspace's `skills/` folder keeps them portable.
To share a skill across agents, install it under `~/.mindroom/skills/<name>/` and add its name to each agent's `skills:` allowlist:

```bash
mkdir -p ~/.mindroom/skills
cp -r /path/to/openclaw-workspace/skills/transcribe ~/.mindroom/skills/
```

Set any environment variables the skill's `SKILL.md` requires, such as `WHISPER_URL`.
See [Skill locations and precedence](https://docs.mindroom.chat/skills/#skill-locations-and-precedence) and [`metadata.openclaw` eligibility gating](https://docs.mindroom.chat/skills/#eligibility-gating-openclaw-metadata) (`os`, `requires`, `always`).
