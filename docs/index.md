---
icon: lucide/bot
---

# MindRoom

AI agents that live in Matrix and work everywhere via bridges.

<video controls playsinline preload="metadata" aria-label="Tour of MindRoom: approvals, a team thread, code, documents, and the browser" style="width: 100%">
  <source src="https://github.com/user-attachments/assets/d24be091-deb0-4be0-8346-3faa6b1e40c4#t=0.1" type="video/mp4" media="(prefers-color-scheme: dark)">
  <source src="https://github.com/user-attachments/assets/7c51fefe-a5d5-460b-ad97-d7886839af3b#t=0.1" type="video/mp4">
</video>

See the [Showcase](showcase.md) for more recordings.

## What is MindRoom?

MindRoom runs a set of AI agents, teams of agents, and a built-in router, each signed into a Matrix homeserver as its own user.
You talk to them in Matrix rooms from any Matrix client, and Matrix bridges carry the same conversations to Discord, Slack, Telegram, and other platforms.
Everything is defined in one `config.yaml`, and most edits to it apply while MindRoom keeps running; see [Configuration](configuration/index.md) for the exceptions.

To get started, follow [Install & First Run](getting-started.md#recommended-hosted-matrix-local-mindroom-uvx-only).

## Features

| Feature | Description |
|---------|-------------|
| **Agents** | Single-specialty actors with their own model, tools, and instructions |
| **Teams** | Bundles of agents that work in coordinate or collaborate mode |
| **Router** | Built-in responder that picks the right agent or team when nobody is mentioned |
| **Memory** | Mem0 vector memory or Markdown memory files, scoped to an agent or team |
| **Knowledge Bases** | Semantic search or files-only access over documents, assigned per agent |
| **Tools** | 100+ integrations such as GitHub, Slack, and Gmail |
| **Skills** | OpenClaw-compatible skills that extend what agents can do |
| **Scheduling** | Run agent tasks at set times with cron expressions or natural language |
| **Voice** | Speech-to-text transcription of voice messages |
| **Images** | Analysis of user-sent images with vision-capable models |
| **File & Video Attachments** | Files and videos that agents can read and send within a conversation |
| **Interactive Q&A** | Clickable multiple-choice questions answered with Matrix reactions |
| **Matrix Desktop Bridge** | Observe or locally lease control of a computer, read selected folders, and run locally approved shell commands without opening inbound ports |
| **Authorization** | Control which users may use which agents and rooms |
| **OpenAI-Compatible API** | Use agents from LibreChat, Open WebUI, or any OpenAI client |
| **Streaming** | Responses appear progressively as message edits, with tool-call markers |
| **Chat Commands** | `!` commands for schedules, models, modes, encryption, the desktop bridge, and help |
| **Hot Reload** | Config changes restart only the affected agents |

## Documentation

- [Install & First Run](getting-started.md) - Installation and first steps
- [Hosted Matrix Deployment](deployment/hosted-matrix.md) - Run only `uvx mindroom` locally against hosted Matrix
- [Configuration](configuration/index.md) - Config file, environment, agents, models, teams, and routing
- [Dashboard](dashboard.md) - Web UI for configuration
- [Tools](tools/index.md) - Enable and configure tool integrations
- [Matrix Desktop Bridge](tools/desktop.md) - Control a desktop and signed-in browser over Matrix
- [MCP](mcp.md) - Connect MCP servers and expose their tools to agents
- [Skills](skills.md) - OpenClaw-compatible skills
- [OpenClaw Import](openclaw.md) - Reuse OpenClaw workspace files in MindRoom
- [Memory System](memory.md) - How agent memory works
- [Knowledge Bases](knowledge.md) - Semantic indexing or files-only knowledge access
- [Scheduling](scheduling.md) - Schedule tasks with cron or natural language
- [Chat Commands](chat-commands.md) - Every `!` command and its options
- [Streaming Responses](streaming.md) - Progressive message edits
- [Voice Messages](voice.md), [Image Messages](images.md), and [File & Video Attachments](attachments.md) - Media handling
- [Interactive Q&A](interactive.md) - Multiple-choice questions via Matrix reactions
- [Rooms & Spaces](rooms.md) - Managed rooms and the optional root Matrix Space
- [Bridges](deployment/bridges/index.md) - Connect Telegram, Slack, and other platforms to Matrix
- [Authorization](authorization.md) - User and room access control
- [Plugins](plugins.md) - Extend with custom tools, OAuth providers, and skills
- [OAuth Framework](oauth-framework.md) - Build scoped OAuth-backed tool integrations
- [External Triggers](external-triggers.md) - Wake agents from signed watcher events and one-shot sub-agent callbacks
- [OpenAI-Compatible API](openai-api.md) - Use agents from any OpenAI-compatible client
- [Deployment](deployment/index.md) - Docker and Kubernetes deployment
- [Sandbox Proxy](deployment/sandbox-proxy.md) - Isolate code-execution tools in a worker
- [Google Services OAuth](deployment/google-services-oauth.md) - Custom admin OAuth setup for Gmail, Calendar, Drive, Docs, Sheets, and Tasks
- [Google Services OAuth (Local Install)](deployment/google-services-user-oauth.md) - Connect Google locally without Cloud setup
- [Architecture](architecture/index.md) - How it works under the hood
- [CLI Reference](cli.md) - Command-line interface
- [Support](support.md) - Contact and troubleshooting help
- [Privacy Policy](privacy.md) and [Terms of Service](terms.md)

## License

- **Repository (except `saas-platform/`)**: Apache License 2.0
- **SaaS Platform** (`saas-platform/`): Business Source License 1.1 (converts to Apache 2.0 on 2030-02-06)
