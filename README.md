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
Each agent is a real Matrix user, so people, specialist agents, and teams collaborate in shared rooms and threads instead of being funneled through one assistant.
Agents can keep shared team context or requester-private memory, files, and knowledge, while execution workers can stay isolated from the primary process's secrets.
Use the [MindRoom chat client](https://github.com/mindroom-ai/mindroom-cinny), any other Matrix client, or bridges to Slack, Telegram, Discord, WhatsApp, IRC, and email.
Self-host the whole stack, or run only the MindRoom backend locally and pair it with hosted Matrix at [mindroom.chat](https://mindroom.chat).

https://github.com/user-attachments/assets/1f121c89-5418-4f42-bdfe-fb9de0fecd03

## Features

- **Matrix-native collaboration** — every agent and team has a Matrix identity; people and agents work together in rooms and threads, with built-in routing when nobody is mentioned and ad hoc collaboration when several agents are.
- **Durable, private state** — sessions and turn state survive restarts, while optional [private agents](docs/configuration/agents.md#private-instances) give each requester isolated files, memory, knowledge, and execution scope from one shared agent definition.
- **Tools, knowledge, and automation** — 100+ integrations cover Gmail, GitHub, Google Drive, Home Assistant, shell, Python, web search, and more; agents can also search watched folders, run scheduled tasks, and escalate background work to a person.
- **Cloud and local models** — choose a model per agent, room, or thread (`!model`), including local Ollama and OpenAI-compatible endpoints alongside hosted providers.
- **Security boundaries** — run code-execution tools in isolated container workers without the primary process's secrets, then add per-tool approvals, egress approval, requester-scoped credentials, and Matrix end-to-end encryption.
- **Live, restart-safe operation** — stream progressive responses and visible tool traces with cancellation; hot-reload config and plugins; resume interrupted conversations after a restart without double replies.
- **Open interfaces and extensions** — use any Matrix client, connect other chat systems through bridges, expose agents through an [OpenAI-compatible API](docs/openai-api.md), and add tools, skills, OAuth providers, or typed event hooks through plugins.
- **From laptop to Kubernetes** — configure agents through YAML or the web dashboard, self-host the full stack, or deploy isolated workers and multi-tenant instances with Helm.

What it looks like:

```text
Alice (from a bridged Slack room): @research @analyst Compare our top competitors
Research: [searches sources and streams findings into the thread]
Analyst: [uses those findings and posts a recommendation in the same thread]
Bob (from a Matrix client): @writer Turn this thread into an executive summary
Writer: [continues with the shared conversation context]
```

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

# Point at your Matrix homeserver, or bootstrap a local Synapse + Cinny stack:
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

Runtime configuration lives in `config.yaml`: agents, teams, models, rooms, knowledge bases, voice, memory, and authorization.
Large configurations can be split across files with [YAML includes](docs/configuration/index.md#splitting-the-configuration-into-multiple-files), while secrets stay in `.env` or the credential store.
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
    model: whisper-1

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

- **Own homeserver** — set `MATRIX_HOMESERVER` and run against a compatible Matrix homeserver.
- **Local stack** — `mindroom local-stack-setup` bootstraps a local Synapse + Cinny via Docker.
- **Hosted Matrix** — run only the backend locally against hosted Matrix at [mindroom.chat](https://mindroom.chat), pairing via [chat.mindroom.chat](https://chat.mindroom.chat) ([guide](docs/deployment/hosted-matrix.md)).
- **Docker** — single-container runtime ([guide](docs/deployment/docker.md)).
- **Kubernetes** — Helm charts for enterprise-scale, multi-tenant deployments ([guide](docs/deployment/kubernetes.md)).
- **NixOS LXC (Incus)** — the author's favorite for personal use: [mindroom-ai/lxc-nixos](https://github.com/mindroom-ai/lxc-nixos) provisions a persistent, agent-controlled NixOS container with the full stack, which the agent can rebuild and manage itself while the host controls what it sees.
- **Bridges** — connect Slack, Telegram, WhatsApp, and more via [docs/deployment/bridges](docs/deployment/bridges).

## Why Matrix?

Matrix is an open, federated messaging protocol with a decade of production use.
By building on it, MindRoom inherits instead of reimplements:

- End-to-end encryption (Olm/Megolm)
- Federation — your agent can join rooms on other homeservers, including other organizations'
- Mature clients on every platform (Element, Cinny, FluffyChat)
- Bridges to Slack, Telegram, Discord, WhatsApp, IRC, email, and more

## How It Compares to OpenClaw and Hermes

[OpenClaw](https://github.com/openclaw/openclaw) and [Hermes Agent](https://github.com/nousresearch/hermes-agent) are capable self-hosted agent runtimes with broad model, tool, and messaging support.
MindRoom makes a different architectural choice: Matrix rooms, threads, identities, and permissions are its native collaboration model rather than external delivery channels.

- **Agents are participants.** Every agent is a Matrix user that people and other agents can mention, invite, authorize, and collaborate with in shared threads.
- **Multi-user does not mean shared state.** A shared agent definition can materialize requester-private workspaces, memory, knowledge, and execution scope.
- **The protocol stays open.** Federation connects organizations, any Matrix client can be the interface, and bridges are additive rather than the foundation.
- **Execution and credentials are separate.** Isolated workers can use narrowly scoped credential leases without receiving the primary runtime's secrets, with tool and network approvals layered on top.

Coming from OpenClaw?
MindRoom [imports OpenClaw workspaces](docs/openclaw.md) (`SOUL.md`, `MEMORY.md`, skills) and ships an `openclaw_compat` tool preset.

## Stack and Interfaces

- **Matrix**: a compatible homeserver and any Matrix client, with the MindRoom client providing an AI-focused experience
- **Agents**: Python, built on [Agno](https://agno.dev/) and [mindroom-nio](https://github.com/mindroom-ai/mindroom-nio)
- **AI models**: nearly any cloud or local model, including OpenAI-compatible endpoints
- **Memory**: `mem0`, markdown files, or stateless operation, selectable globally or per agent
- **API**: an OpenAI-compatible endpoint that exposes MindRoom agents as selectable models
- **UI**: web dashboard for administration; the [MindRoom chat client](https://github.com/mindroom-ai/mindroom-cinny) (or any Matrix client) for chat

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
