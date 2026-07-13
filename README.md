# mindroom

[![PyPI](https://img.shields.io/pypi/v/mindroom)](https://pypi.org/project/mindroom/)
[![Python](https://img.shields.io/pypi/pyversions/mindroom)](https://pypi.org/project/mindroom/)
[![Tests](https://img.shields.io/github/actions/workflow/status/mindroom-ai/mindroom/pytest.yml?label=tests)](https://github.com/mindroom-ai/mindroom/actions/workflows/pytest.yml)
[![Build](https://img.shields.io/github/actions/workflow/status/mindroom-ai/mindroom/build-mindroom.yml?label=build)](https://github.com/mindroom-ai/mindroom/actions/workflows/build-mindroom.yml)
[![Docs](https://img.shields.io/badge/docs-mindroom.chat-blue)](https://docs.mindroom.chat)
[![License](https://img.shields.io/github/license/mindroom-ai/mindroom)](https://github.com/mindroom-ai/mindroom/blob/main/LICENSE)
[![Downloads](https://img.shields.io/pypi/dm/mindroom)](https://pypi.org/project/mindroom/)
[![GitHub](https://img.shields.io/badge/github-mindroom--ai%2Fmindroom-blue?logo=github)](https://github.com/mindroom-ai/mindroom)

<img src="frontend/public/logo.png" alt="MindRoom Logo" align="right" width="150" />

**AI agents that live in your chat rooms.**

MindRoom is an open-source multi-agent runtime built on [Matrix](https://matrix.org/) that works with nearly any [cloud or local AI model](docs/configuration/models.md).
You define agents in a YAML file or in the web dashboard; MindRoom gives each one a Matrix account, and you talk to them in threads in [MindRoom Chat](https://github.com/mindroom-ai/mindroom-chat) — or any other Matrix client you already use.
Because Matrix bridges to other platforms, the same agents also work in Slack, Telegram, Discord, WhatsApp, IRC, and email — with the same persistent memory everywhere.
Self-host the whole stack, or run only the MindRoom backend locally and pair it with hosted Matrix at [mindroom.chat](https://mindroom.chat).

https://github.com/user-attachments/assets/1f121c89-5418-4f42-bdfe-fb9de0fecd03

## Features

- **Multi-agent orchestration** — define specialist agents and teams in `config.yaml`; a built-in router picks the responder when you don't @-mention one, and mentioning several agents makes them collaborate in a thread.
- **Persistent memory** — agents remember people, preferences, and context across conversations and platforms (Mem0 + ChromaDB, stored on your disk).
- **100+ tool integrations** — Gmail, GitHub, Google Drive, Home Assistant, shell, Python, web search, and more, plus native Matrix tools and a per-thread `todo` planner, with sandboxed execution and per-tool approval rules.
- **Knowledge bases (RAG)** — point an agent at a folder of files; MindRoom indexes it and can watch it for changes.
- **Scheduling & automation** — cron or natural-language scheduled tasks (`!schedule`), background work with human escalation.
- **Model routing** — a different model per agent, room, or thread (`!model`); route sensitive rooms to local Ollama and everything else to a cloud model.
- **Voice** — transcription of Matrix voice messages, and text-to-speech tools via OpenAI, Groq, ElevenLabs, and Cartesia.
- **Streaming responses** — agents type into the room with progressive edits, visible tool traces, and cancellation.
- **Plugins & hooks** — drop-in [plugins](docs/plugins.md) add custom tools, skills, and OAuth providers, and a typed [event-hook system](docs/hooks.md) (per-hook timeouts, fault isolation) lets them observe and transform messages; reload plugins at runtime with `!reload-plugins`.
- **Hot reload & restart-safe** — `config.yaml` and plugin changes apply live without bringing down the stack, and conversations resume seamlessly after a restart: session history and turn state are durable on disk, so agents pick up where they left off without double-replying.
- **Web dashboard** — create and configure agents, teams, models, tools, credentials, and knowledge bases by clicking instead of editing YAML; chat stays in your Matrix client.
- **Enterprise deployment** — the same runtime scales from a laptop to multi-tenant Kubernetes with Helm charts, isolated execution workers, and egress approval for locked-down environments.

What it looks like:

```text
You: @research @analyst @writer Create a competitive analysis report
Research: I'll gather data on our top 5 competitors...
Analyst: I'll identify strategic patterns and opportunities...
Writer: I'll compile everything into an executive summary...
```

<details>
<summary><b>Why we built this</b></summary>

Every AI app is a silo:

- ChatGPT knows your coding style... but can't join your team's Slack
- Claude understands your writing... but can't access your email
- GitHub Copilot helps with code... but can't see your project specs
- You teach each AI from scratch, over and over

Your human team collaborates across Slack, Discord, Telegram, and email every day — your AI should too.
MindRoom agents live in one place (Matrix) and follow you everywhere via bridges, with their memory intact.

Federation even lets agents cross organization boundaries:

```text
Your client asks in their Discord:
Client: Can our architect AI review this with your team?
You: Sure! @assistant please collaborate with them

Your Assistant: [Joins from your Matrix server]
Client's Architect AI: [Joins from their server]
Together: [They review architecture, sharing context from both organizations]
```

Two AI agents from different companies collaborating — impossible with app-bound assistants.

</details>

## How It Compares to OpenClaw and Hermes

[OpenClaw](https://github.com/openclaw/openclaw) and [Hermes Agent](https://github.com/nousresearch/hermes-agent) are self-hosted assistants that pipe an agent into chat apps you already use.
MindRoom plays in the same space but makes different architectural bets:

- **Multi-agent and multi-user by default.** Both are personal-first: one owner talking to their assistant. In MindRoom every agent is a real Matrix user, so you run a fleet of specialists and teams, share them with family, a project, or a whole company, and scope access per user and per room.
- **An AI-native interface on an open protocol.** With WhatsApp, Signal, or Telegram as the front end, you rent UX from platforms that were never designed for agents and can cut bots off at any time. MindRoom's home is Matrix, with [MindRoom Chat](https://github.com/mindroom-ai/mindroom-chat) tuned for AI: collapsible tool-call traces, model metadata on every response, streaming with in-place edits, response cancellation, and first-class threads. Bridges to those apps are additive, not the foundation.
- **Sandboxing with real secrets isolation.** Execution tools (shell, Python, coding) can run in isolated container workers with no access to the primary process's secrets — your agent uses credentialed tools (Gmail, GitHub, ...) while the code it executes can never read those credentials. Per-tool [approval rules](docs/configuration/index.md) and [egress approval](docs/deployment/approved-egress.md) add human-in-the-loop control.
- **Batteries included.** 100+ built-in tool integrations with typed configuration, OAuth flows, and automatic dependency installation — plus OpenClaw-compatible skills on top.

Coming from OpenClaw? MindRoom [imports OpenClaw workspaces](docs/openclaw.md) (`SOUL.md`, `MEMORY.md`, skills) and ships an `openclaw_compat` tool preset.

## Quick Start

### Hosted Matrix + local MindRoom (fastest)

MindRoom runs on your machine; Matrix is hosted at `mindroom.chat` and the chat UI at [chat.mindroom.chat](https://chat.mindroom.chat).
The only prerequisite is [uv](https://github.com/astral-sh/uv), which installs Python automatically if needed.
Watch the 2-minute setup video:

<a href="https://youtu.be/jR3xLUxyWhg"><img src="https://img.youtube.com/vi/jR3xLUxyWhg/maxresdefault.jpg" alt="MindRoom: installing and talking to my first AI agent in 2 minutes" width="480"></a>

```bash
# Create ~/.mindroom/config.yaml and ~/.mindroom/.env with hosted defaults
uvx mindroom config init

# Add model auth, or run `uvx mindroom config init --provider codex` and `codex login`
$EDITOR ~/.mindroom/.env

# Generate pair code in https://chat.mindroom.chat:
# Settings -> Local MindRoom -> Generate Pair Code
uvx mindroom connect --pair-code ABCD-EFGH

# Start MindRoom
uvx mindroom run
```

See the [hosted Matrix deployment guide](docs/deployment/hosted-matrix.md) for full details.

### Self-hosted, from source

Requires Python 3.12+ and [uv](https://github.com/astral-sh/uv); Node.js 20+ with [bun](https://bun.sh/) is optional for building the web dashboard.

```bash
git clone https://github.com/mindroom-ai/mindroom
cd mindroom
uv sync

# Point at your Matrix homeserver, or bootstrap a local Synapse + MindRoom Chat stack:
#   mindroom local-stack-setup --synapse-dir /path/to/mindroom-stack/local/matrix
export MATRIX_HOMESERVER=https://your-matrix.server
export ANTHROPIC_API_KEY=your-key-here

# Start MindRoom (agents + API + web dashboard)
uv run mindroom run
```

The web dashboard is available at http://localhost:8765.
Matrix E2EE support is installed by default.

### macOS menu bar app

The menu bar app runs the local MindRoom service without keeping a terminal open.
It bundles `uv`, uses `~/.mindroom` for config and state, and manages the `mindroom service` launchd service.
The signed universal app supports both Apple silicon and Intel Macs.

```bash
brew install --cask mindroom-ai/tap/mindroom
```

Open **MindRoom** from `/Applications` and use the menu bar item to install the runtime, pair with the hosted chat UI, and open the dashboard.
See the [macOS app guide](docs/installation/macos-app.md) for setup, updates, and uninstall instructions.

### First steps

In the MindRoom chat client (hosted at [chat.mindroom.chat](https://chat.mindroom.chat), or bundled with the local stack):

```text
You: @assistant What can you do?
Assistant: I can coordinate our team of specialized agents...

You: @research @analyst What are the latest AI breakthroughs?
[Agents collaborate to research and analyze]
```

## How Agents Respond

Agents and teams respond using Matrix thread relations to keep conversations organized.
If your client or bridge only sends plain replies, MindRoom keeps them in an existing thread when the reply chain eventually reaches a threaded ancestor or proven thread root.
Plain replies that never reach threaded context still stay plain replies.

1. **Mentioned agents and teams respond** - Tag them to get their attention
2. **Single responder continues** - One agent or team in a thread keeps responding
3. **Multiple agents collaborate** - Mention multiple agents when you want an ad-hoc collaboration
4. **Smart routing** - System picks the best agent or team for new threads
5. **DMs need no mentions** - Agents respond naturally in 1:1 rooms, and you can add more agents to a DM for private collaboration

### Chat Commands

<!-- CODE:START -->
<!-- import sys -->
<!-- sys.path.insert(0, 'src') -->
<!-- from mindroom.commands.parsing import _get_command_entries -->
<!-- for entry in _get_command_entries(format_code=True): -->
<!--     print(entry) -->
<!-- CODE:END -->
<!-- OUTPUT:START -->
<!-- ⚠️ This content is auto-generated by `markdown-code-runner`. -->
- `!help [topic]` - Get help
- `!reload-plugins` - Reload configured plugins (admin only)
- `!schedule <task>` - Schedule a task
- `!list_schedules` - List scheduled tasks
- `!cancel_schedule <id>` - Cancel a scheduled task
- `!edit_schedule <id> <task>` - Edit an existing scheduled task
- `!config <operation>` - Manage configuration
- `!model [name|list|reset]` - Show or switch the model used in the current thread
- `!thread_mode [room|thread|reset|show]` - Show or switch the thread mode used in the current room (room admin only)
- `!encrypt [confirm]` - Enable end-to-end encryption for this room (irreversible, room admin only)
- `!e2ee` - Show encryption diagnostics for this room
- `!hi` - Show welcome message

<!-- OUTPUT:END -->

## Configuration

Everything lives in `config.yaml`: agents, teams, models, rooms, knowledge bases, voice, memory, and authorization.
The web dashboard edits the same file, so you can point-and-click instead of writing YAML.
Either way, changes are hot-reloaded and take effect without a restart.

```yaml
agents:
  assistant:
    display_name: Assistant
    role: A helpful AI assistant
    model: default
    rooms: [lobby]
    tools: [matrix_message]
    accept_invites: true  # Optional: accept authorized ad-hoc room invites
    knowledge_bases: [engineering_docs]

models:
  default:
    provider: anthropic
    id: claude-sonnet-5

knowledge_bases:
  engineering_docs:
    path: ./knowledge_docs
    watch: true

voice:
  enabled: true
  stt:
    provider: openai
    model: gpt-4o-transcribe

mindroom_user:
  username: mindroom_user  # Immutable once the account is created on first run
  display_name: MindRoomUser

authorization:
  global_users: ["@alice:example.com"]
  default_room_access: false
```

Environment variables go in `.env` (or `~/.mindroom/.env` for the hosted path):

```bash
MATRIX_HOMESERVER=https://your-matrix.server
ANTHROPIC_API_KEY=your-key-here
# Optional: protect dashboard API endpoints (recommended for non-localhost)
# MINDROOM_API_KEY=your-secret-key
# Optional: use a non-default config location
# MINDROOM_CONFIG_PATH=/path/to/config.yaml
```

Teams, cultures, per-room models, context compaction, history controls, and memory backends are covered in the [configuration docs](docs/configuration/index.md) and at [docs.mindroom.chat](https://docs.mindroom.chat).

## Deployment

- **Own homeserver** — set `MATRIX_HOMESERVER` and run against any Synapse, Conduit, or Dendrite instance.
- **Local stack** — `mindroom local-stack-setup` bootstraps a local Synapse + MindRoom Chat via Docker.
- **Hosted Matrix** — run only the backend locally against hosted Matrix at [mindroom.chat](https://mindroom.chat), pairing via [chat.mindroom.chat](https://chat.mindroom.chat) ([guide](docs/deployment/hosted-matrix.md)).
- **Docker** — single-container runtime ([guide](docs/deployment/docker.md)).
- **Kubernetes** — Helm charts for enterprise-scale, multi-tenant deployments ([guide](docs/deployment/kubernetes.md)).
- **NixOS LXC (Incus)** — the author's favorite for personal use: [mindroom-ai/lxc-nixos](https://github.com/mindroom-ai/lxc-nixos) provisions a persistent, agent-controlled NixOS container with the full stack, which the agent can rebuild and manage itself while the host controls what it sees.
- **Bridges** — connect Slack, Telegram, WhatsApp, and more via [docs/deployment/bridges](docs/deployment/bridges).

## Why Matrix?

Matrix is an open, federated messaging protocol with a decade of production use, including large government and healthcare deployments.
By building on it, MindRoom inherits instead of reimplements:

- End-to-end encryption (Olm/Megolm)
- Federation — your agent can join rooms on other homeservers, including other organizations'
- Mature clients on every platform (Element, Cinny, FluffyChat)
- 50+ maintained bridges to Slack, Telegram, Discord, WhatsApp, IRC, email, and more

<details>
<summary><b>Matrix adoption at a glance</b></summary>

- **10+ years** of development by the Matrix.org Foundation, with **€10M+** invested and **100+ core contributors**
- **35+ million users** globally
- **German healthcare**: 150,000+ organizations on TI-Messenger
- **French government**: 5.5 million civil servants on Tchap
- **Defense**: NATO, U.S. Space Force, and other defense organizations
- Built for European privacy standards (GDPR)

</details>

## Architecture

- **Matrix**: any homeserver (Synapse, Conduit, Dendrite, ...)
- **Agents**: Python, built on [Agno](https://agno.dev/) and [mindroom-nio](https://github.com/mindroom-ai/mindroom-nio)
- **AI models**: Anthropic, OpenAI, Google, Ollama, Bedrock, or any OpenAI-compatible endpoint
- **Memory**: Mem0 + ChromaDB vector storage, persistent on disk
- **UI**: web dashboard for administration; [MindRoom Chat](https://github.com/mindroom-ai/mindroom-chat) (or any Matrix client) for chat

See [docs/architecture](docs/architecture) for internals.

## Note for Self-Hosters

This repository contains everything you need to self-host MindRoom.
The `saas-platform/` directory contains infrastructure specific to running MindRoom as a hosted service and can be safely ignored by self-hosters.

## Contributing

We welcome contributions!
See [CLAUDE.md](CLAUDE.md) for the current development workflow and quality checks.

## License

- **Repository (except `saas-platform/`)**: [Apache License 2.0](LICENSE)
- **SaaS Platform** (`saas-platform/`): [Business Source License 1.1](saas-platform/LICENSE) (converts to Apache 2.0 on 2030-02-06)

## Acknowledgments

Built with:
- [Matrix](https://matrix.org/) - The federated communication protocol
- [Agno](https://agno.dev/) - AI agent framework
- [mindroom-nio](https://github.com/mindroom-ai/mindroom-nio) - Python Matrix client
