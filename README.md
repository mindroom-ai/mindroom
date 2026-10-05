<div align="center">

<picture>
  <source media="(prefers-reduced-motion: no-preference)" srcset="https://raw.githubusercontent.com/mindroom-ai/mindroom/main/assets/logo/logo-mark-animated.svg" />
  <img src="https://raw.githubusercontent.com/mindroom-ai/mindroom/main/assets/logo/logo-mark.svg" alt="MindRoom logo" width="128" />
</picture>

# MindRoom

**AI agents that know you and your work, in a chat app anyone can use.**

Open source under Apache 2.0 · Any model, local or cloud · Self-host the whole stack

[Website](https://mindroom.chat) · [Docs](https://docs.mindroom.chat) · [Showcase](https://docs.mindroom.chat/showcase/) · [MindRoom Chat](https://chat.mindroom.chat) · [Quick start](#quick-start)

[![PyPI](https://img.shields.io/pypi/v/mindroom)](https://pypi.org/project/mindroom/)
[![Tests](https://img.shields.io/github/actions/workflow/status/mindroom-ai/mindroom/pytest.yml?label=tests)](https://github.com/mindroom-ai/mindroom/actions/workflows/pytest.yml)
[![Docs](https://img.shields.io/badge/docs-mindroom.chat-blue)](https://docs.mindroom.chat)
[![License](https://img.shields.io/github/license/mindroom-ai/mindroom)](https://github.com/mindroom-ai/mindroom/blob/main/LICENSE)
[![Downloads](https://img.shields.io/pypi/dm/mindroom)](https://pypi.org/project/mindroom/)

</div>

https://github.com/user-attachments/assets/665d730b-83b3-42b6-aa98-7dde35f16244

MindRoom gives you a personal agent for your calendar, notes, trips, and homelab, and shared agents for your team's email, documents, and code.
Every agent is a real user on [Matrix](https://matrix.org/), the open chat standard, so you talk to it in [MindRoom Chat](https://github.com/mindroom-ai/mindroom-chat), in any other Matrix client, or in Slack, Telegram, WhatsApp, and Discord through bridges.
Pick a [local model](docs/configuration/models.md) for your private life or a frontier model for hard problems, and self-host the whole stack or run only the agents on your own computer.

<table>
<tr>
<th width="50%">Any computer</th>
<th width="50%">macOS app</th>
</tr>
<tr>
<td valign="top">

```bash
uvx mindroom run
```

Installs and starts MindRoom with a starter agent, then pairs it with your [MindRoom Chat](https://chat.mindroom.chat) account.

</td>
<td valign="top">

```bash
brew install --cask mindroom-ai/tap/mindroom
```

A native app that runs your agents on your Mac in the background.
Needs an Apple silicon Mac with macOS 14 or later.

</td>
</tr>
</table>

Chat with your agents on the [web](https://chat.mindroom.chat), on the [Mac](docs/installation/macos-app.md), on [iPhone and iPad](https://apps.apple.com/us/app/mindroom-ai/id6760272172), and on Android (in beta).
See [Quick Start](#quick-start) for every way to run it.

## See it in action

<table>
<tr>
<td width="50%" valign="top">
<a href="https://docs.mindroom.chat/showcase/#ask-approve-done"><picture><source media="(prefers-color-scheme: dark)" srcset="https://github.com/user-attachments/assets/5e389c59-c81a-4f70-9c5d-de7406ff95f4" /><img src="https://github.com/user-attachments/assets/bc6194b1-9398-47f5-b649-2ea17dbb6b9f" alt="A review dialog where the user approves an agent's calendar booking" /></picture></a>
<p><b>Approve before it acts</b><br />Risky actions wait for your OK, with the exact arguments in view.</p>
</td>
<td width="50%" valign="top">
<a href="https://docs.mindroom.chat/showcase/#one-thread-the-whole-team"><picture><source media="(prefers-color-scheme: dark)" srcset="https://github.com/user-attachments/assets/39a572a8-74d7-4349-ad68-2ee1b0c16a21" /><img src="https://github.com/user-attachments/assets/1efa0323-6502-4d7c-82c9-6c39f906ce89" alt="Three colleagues see the same agent thread side by side" /></picture></a>
<p><b>The whole team, one thread</b><br />Colleagues share an agent in a thread and see every answer stream in live.</p>
</td>
</tr>
<tr>
<td width="50%" valign="top">
<a href="https://docs.mindroom.chat/showcase/#canvases"><picture><source media="(prefers-color-scheme: dark)" srcset="https://github.com/user-attachments/assets/7fc2bc01-1ec5-40d1-971f-5e6f33ba0f18" /><img src="https://github.com/user-attachments/assets/8eb3e6d8-8e9f-4ebb-a51b-bbb5913536e3" alt="Two agent canvases: a week grid of free meeting slots and a weekend trip planner with a budget" /></picture></a>
<p><b>Canvases you can click</b><br />An agent lays out free slots or a trip budget beside the chat and acts on what you pick.</p>
</td>
<td width="50%" valign="top">
<a href="https://docs.mindroom.chat/showcase/#canvases"><picture><source media="(prefers-color-scheme: dark)" srcset="https://github.com/user-attachments/assets/1392c1f2-146f-4131-a966-360f7d83f14a" /><img src="https://github.com/user-attachments/assets/bd3107c6-907d-4df3-922e-73f0da14055c" alt="A thread next to a data canvas with a fitted curve and its parameters" /></picture></a>
<p><b>Data you can explore</b><br />Leave a point out of a fit, then send the new result back to the agent.</p>
</td>
</tr>
<tr>
<td width="50%" valign="top">
<a href="https://docs.mindroom.chat/showcase/#it-drives-you-take-the-wheel"><picture><source media="(prefers-color-scheme: dark)" srcset="https://github.com/user-attachments/assets/ca52372b-8fc5-442a-8f2b-90d7a3d16d8e" /><img src="https://github.com/user-attachments/assets/1884d9d2-1c4b-4279-ba55-d6cbdc4d43ba" alt="An agent's browser waits on an order page while the user can take control" /></picture></a>
<p><b>It drives, you take the wheel</b><br />Watch an agent work in its own browser and take over when it needs your passkey.</p>
</td>
<td width="50%" valign="top">
<a href="https://docs.mindroom.chat/showcase/#connect-once-then-just-ask"><picture><source media="(prefers-color-scheme: dark)" srcset="https://github.com/user-attachments/assets/69f219b2-6fef-419c-9d3b-513aaa340b01" /><img src="https://github.com/user-attachments/assets/4f18f100-5ce8-46c4-a4bf-18be84fccb4f" alt="A thread answered from a wiki next to the connections page where the wiki account is connected" /></picture></a>
<p><b>Connect once, then just ask</b><br />Connect an account once, and the agent answers from it from then on.</p>
</td>
</tr>
<tr>
<td width="50%" valign="top">
<a href="https://docs.mindroom.chat/showcase/#lock-up-on-the-way-out"><picture><source media="(prefers-color-scheme: dark)" srcset="https://github.com/user-attachments/assets/b35e6de5-09f6-4ae1-b8df-e9ad3e81987e" /><img src="https://github.com/user-attachments/assets/38b5676b-dedf-4bde-a6ba-fad3378d3c12" alt="Two phone screens: a voice note locks up the house, and another sets a reminder" /></picture></a>
<p><b>From your phone</b><br />One voice note locks up the house; another becomes a reminder.</p>
</td>
<td width="50%" valign="top">
<a href="https://docs.mindroom.chat/showcase/#your-morning-already-handled"><picture><source media="(prefers-color-scheme: dark)" srcset="https://github.com/user-attachments/assets/d37616c8-3dd4-4e35-a065-988249227c81" /><img src="https://github.com/user-attachments/assets/03a931bc-6654-44b4-a15e-696921305fd3" alt="A scheduled morning brief with today's meetings and what needs attention" /></picture></a>
<p><b>Your morning, already handled</b><br />A scheduled brief with today's meetings and what needs you.</p>
</td>
</tr>
</table>

These are stills from the [showcase](https://docs.mindroom.chat/showcase/) recordings: MindRoom Chat and a real MindRoom backend, with a fictional company and scripted model responses.

## What people use it for

<table>
<tr>
<th width="50%">Personal</th>
<th width="50%">Work</th>
</tr>
<tr>
<td valign="top">

- Plan a family trip, from flights to a packing list.
- Keep your calendar, reminders, and to-do lists in order, by voice.
- Keep notes, a journal, and memories you can find months later.
- Follow the topics you care about, with a digest only when something is new.
- Look after your homelab and smart home, with approval for anything risky.
- Build quick tools and scripts in the agent's own workspace.

</td>
<td valign="top">

- Find anything across email, chat, documents, tickets, and code, with sources.
- Get a morning briefing, or a summary of your week before a one-on-one.
- Write status updates from what actually happened in chat and the tracker.
- Turn a question into a report or a presentation built from your own documents.
- Triage the inbox and draft emails, approving each one before it is sent.
- Join a new team and ask its agent how the work fits together.

</td>
</tr>
</table>

## Why MindRoom

<table>
<tr>
<td width="50%" valign="top">

**🔌 [Connected to your tools and documents](https://docs.mindroom.chat/#agents-that-know-you-and-your-work)**<br />
Personal agents and shared team agents connect to 100+ tools, including email, calendar, Slack, Jira, GitHub, and any MCP server, search your own documents, and can use any computer you pair, even from a server across the world.

</td>
<td width="50%" valign="top">

**🔒 [Private where it matters](https://docs.mindroom.chat/#private-where-it-matters)**<br />
Pick a model per agent: a local one for your most personal data, a frontier one for coding. With local memory and your own server, nothing that agent sees leaves your home.

</td>
</tr>
<tr>
<td valign="top">

**🧠 [Memory that keeps improving](https://docs.mindroom.chat/#they-remember-and-keep-improving)**<br />
Agents keep what matters from every conversation, get better as more people use them, and can turn work they repeat into reusable skills.

</td>
<td valign="top">

**🛡️ [Safe to give real access](https://docs.mindroom.chat/#safe-to-give-real-access)**<br />
One-tap approval for anything risky, sandboxed code execution, and end-to-end encryption on Matrix, the open standard governments use for secure messaging.

</td>
</tr>
<tr>
<td valign="top">

**💬 [A chat app built for agents](https://docs.mindroom.chat/#a-chat-app-built-for-agents)**<br />
MindRoom builds its own client for the web, Mac, iPhone, iPad, and Android (in beta), so agents can show live tool traces, ask for approval, open interactive canvases, join voice calls, and work in a real browser you can take over.

</td>
<td valign="top">

**🌉 [Works where you already are](https://docs.mindroom.chat/#works-where-you-already-are)**<br />
Bridges bring the same agents to Slack, Telegram, WhatsApp, and Discord, and an MCP gateway brings them to Claude Code and Codex.

</td>
</tr>
</table>

<details>
<summary><b>Full feature list</b></summary>

**Agents and teams**

- **Multi-agent orchestration** — define specialist agents and teams in `config.yaml` or the dashboard; a built-in router picks the responder when you don't @-mention one, mentioning several agents makes them collaborate in a thread, and agents can hand tasks to each other or run reusable workflows ([Teams](docs/configuration/teams.md), [Agent Orchestration](docs/tools/agent-orchestration.md)).
- **Adaptive participation** — in a thread with several people, agents you opt in that already replied there decide for themselves when an untagged message is theirs to answer, and a judge can decide whether a new message should interrupt the current task or wait ([Threads](docs/configuration/threads.md#adaptive-participation)).
- **Personal rooms and access control** — each person can get a private room with their own agent, and each agent answers only the people and rooms you allow ([Personal Rooms](docs/personal-rooms.md), [Access Control](docs/authorization.md)).

**Memory and learning**

- **Persistent memory** — agents remember people, preferences, and context across conversations and platforms (Mem0 + ChromaDB, stored on your disk).
- **Automatic skill learning** — opt-in background reviews turn work an agent repeats into a small library of reusable skills, following the self-improvement loop of Hermes Agent ([Skills](docs/skills.md#automatic-skill-learning)).
- **Knowledge bases (RAG)** — point an agent at a folder or Git repository; MindRoom indexes it and can watch it for changes ([Knowledge Bases](docs/knowledge.md)).

**Tools and computers**

- **100+ tool integrations** — Gmail, GitHub, Google Docs, Google Drive, Home Assistant, shell, Python, web search, any [MCP server](docs/mcp.md), and more, plus native Matrix tools and a per-thread `todo` planner.
- **A real browser you can take over** — an agent works in its own persistent Chromium browser that you watch live in MindRoom Chat, take control of for a login or passkey, and hand back ([Worker Computer](docs/tools/worker-computer.md)).
- **Reach any computer you pair** — your agents can run on a server across the world and still use your laptop: turn on the desktop bridge on any machine, and the agent can use the apps and folders you allow and run the commands you approve, over end-to-end encrypted Matrix with no open ports or VPN ([Desktop Bridge](docs/tools/desktop.md)).
- **Lean prompts** — minimal mode gives an agent one Bash tool that calls its other tools, and dynamic tools load rarely used tools only when needed, so each request carries fewer tokens ([Minimal Mode](docs/tools/agent-cli.md), [Dynamic Tools](docs/tools/dynamic-tools.md)).

**In the chat**

- **Interactive canvases** — an agent can open a page it wrote, such as a dashboard, slide deck, form, or picker, beside the conversation, and what you pick there comes back as your next message ([Interactive Canvases](docs/canvases.md)).
- **Approvals and questions** — one-tap approval cards show exactly what will be sent and to whom, a scheduled send can be approved when it is scheduled, and agents can ask multiple-choice questions you answer with a click ([Tool Approval](docs/tool-approval.md), [Interactive Questions](docs/interactive.md)).
- **Streaming responses** — agents type into the room with progressive edits, visible tool traces, and cancellation.
- **Voice** — voice messages are transcribed, agents can join Element Call voice calls and talk in real time, and text-to-speech tools use OpenAI, Groq, ElevenLabs, and Cartesia ([Voice Messages](docs/voice.md), [Voice Calls](docs/voice-calls.md)).
- **Model routing** — a different model per agent, room, or thread (`!model` and `!room_model`); route sensitive rooms to local Ollama and everything else to a cloud model.

**Automation**

- **Scheduling & automation** — cron or natural-language scheduled tasks (`!schedule`), including silent checks that post only when they find something, plus [supervised background Python watchers](docs/tools/background-scripts.md) that can call governed agent tools and wake the agent only when something changes, and [external triggers](docs/external-triggers.md) that let any process wake an agent with one HTTP request.

**Safety and privacy**

- **Isolated execution** — shell, Python, and coding tools can run in container workers with no access to the primary process's secrets, with per-tool [approval rules](docs/tool-approval.md) and [approved egress](docs/deployment/approved-egress.md) for locked-down environments ([Workers & Sandboxing](docs/deployment/sandbox-proxy.md)).
- **End-to-end encryption** — rooms can be end-to-end encrypted with Matrix's Olm/Megolm ([End-to-End Encryption](docs/matrix.md#end-to-end-encryption)).

**Run it anywhere**

- **Works with other apps** — bridges bring the same agents to Slack, Telegram, WhatsApp, Discord, and email, an [OpenAI-compatible API](docs/openai-api.md) serves them to LibreChat, Open WebUI, and other clients, and an [MCP gateway](docs/deployment/mcp-gateway.md) shares their tools with Claude Code and Codex ([Bridges](docs/deployment/bridges/index.md)).
- **Native macOS app** — runs your agents on your Mac in the background, with a menu bar companion and one-click local model setup ([macOS App](docs/installation/macos-app.md)).
- **Web dashboard** — create and configure agents, teams, models, tools, credentials, and knowledge bases by clicking instead of editing YAML; chat stays in your Matrix client.
- **Plugins & hooks** — drop-in [plugins](docs/plugins.md) add custom tools, skills, and OAuth providers, and a typed [event-hook system](docs/hooks.md) (per-hook timeouts, fault isolation) lets them observe and transform messages; reload plugins at runtime with `!reload-plugins`.
- **Hot reload & restart-safe** — `config.yaml` and plugin changes apply live without bringing down the stack, and conversations resume seamlessly after a restart: session history and turn state are durable on disk, so agents pick up where they left off without double-replying.
- **Enterprise deployment** — the same runtime scales from a laptop to multi-tenant Kubernetes with Helm charts.

</details>

<details>
<summary><b>Why we built this</b></summary>

AI apps can connect to your email, calendar, and documents now, but each one ties those connections to one company.
Connect your inbox to ChatGPT, and OpenAI's servers process it for OpenAI's models only.
Want Claude for writing or Gemini for research? Connect everything again, hand your data to another company, and teach it about you from scratch.

MindRoom turns that around:

- **Connect once, use any model.** Your accounts, memory, and documents live in MindRoom, which can run on your own machine, and every agent uses them with the model that fits: Claude, GPT, Gemini, or a local model.
- **You decide who sees what.** A cloud model sees only what its agent sends it, and an agent on a local model, with local memory and your own server, keeps everything at home.
- **One place, everywhere you chat.** Agents live in shared rooms on Matrix and follow you to Slack, Telegram, WhatsApp, and Discord through Matrix bridges, with their memory intact.

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
- **Sandboxing with real secrets isolation.** Execution tools (shell, Python, coding) can run in isolated container workers with no access to the primary process's secrets — your agent uses credentialed tools (Gmail, GitHub, ...) while the code it executes can never read those credentials. Per-tool [approval rules](docs/tool-approval.md) and [egress approval](docs/deployment/approved-egress.md) add human-in-the-loop control.
- **Batteries included.** 100+ built-in tool integrations with typed configuration, OAuth flows, and automatic dependency installation — plus OpenClaw-compatible skills on top.

Coming from OpenClaw? MindRoom [imports OpenClaw workspaces](docs/openclaw.md) (`SOUL.md`, `MEMORY.md`, skills) and ships an `openclaw_compat` tool preset.

## Quick Start

### Hosted Matrix + local MindRoom (fastest)

MindRoom runs on your machine; Matrix is hosted at `mindroom.chat` and the chat UI at [chat.mindroom.chat](https://chat.mindroom.chat).
The only prerequisite is [uv](https://github.com/astral-sh/uv), which installs Python automatically if needed.

```bash
uvx mindroom run
# First run: choose a model provider; anthropic, openai, and openrouter also ask for an API key (Enter skips).
# MindRoom writes ~/.mindroom/config.yaml and ~/.mindroom/.env with hosted defaults,
# then prints a link and QR code: approve it with your MindRoom Chat account,
# or enter the code in MindRoom Chat -> Settings -> Local MindRoom.
# Finally, Enter keeps MindRoom running in the background as a login service (systemd or launchd); n runs it here
```

Coding agents and scripts can answer every question with flags instead: `OPENAI_API_KEY=sk-... uvx mindroom run --provider openai --service`.
To create or review the files without starting, run `uvx mindroom config init` (optionally with `--provider codex` or another preset), edit `~/.mindroom/.env`, and then run `uvx mindroom run`.
See the [hosted Matrix deployment guide](docs/deployment/hosted-matrix.md) for full details.

### Self-hosted, from source

Requires Python 3.12+ and [uv](https://github.com/astral-sh/uv).
For the dashboard in a fresh source checkout, install Node.js 24 and [Bun](https://bun.sh/) so the first run can build missing assets, or set `MINDROOM_FRONTEND_DIST` to a prebuilt dashboard directory.
The repository dev shell provides Node.js 24 and Bun.

```bash
git clone https://github.com/mindroom-ai/mindroom
cd mindroom
uv sync

# Create a starter config with an Anthropic model and self-hosted Matrix settings.
uv run mindroom config init --path ./config.local.yaml --matrix-server self-hosted --provider anthropic

# Set MATRIX_HOMESERVER, ANTHROPIC_API_KEY, and any required registration credentials.
$EDITOR .env
# Replace each owner placeholder with your quoted Matrix user ID.
$EDITOR config.local.yaml

# Optional: bootstrap this checkout's local Synapse + MindRoom Chat stack.
# MINDROOM_CONFIG_PATH=./config.local.yaml uv run mindroom local-stack-setup --synapse-dir local/matrix

# Start MindRoom with the generated config.
uv run mindroom run --config ./config.local.yaml
```

The checked-in `config.yaml` is a development setup with local model endpoints.
In `config.local.yaml`, replace every `__MINDROOM_OWNER_USER_ID_FROM_PAIRING__` with your quoted Matrix user ID, such as `"@alice:matrix.example.com"`.
See the [manual configuration and account provisioning instructions](docs/getting-started.md#configuration) for homeserver setup.
The starter creates the `Mind` agent in the `personal` room.

With dashboard assets available, open http://localhost:8765.
Without assets and Bun, the API is available at http://localhost:8765/api but the dashboard is unavailable.
Matrix E2EE support is installed by default.

### macOS app

The macOS app provides a native window and menu bar companion for local agents and computer access.
It bundles `uv`, uses `~/.mindroom` for config and state, and manages the `mindroom service` launchd service.
The signed app requires an Apple silicon Mac.

```bash
brew install --cask mindroom-ai/tap/mindroom
```

Open **MindRoom** from `/Applications` to set up local agents, manage computer access, or open chat and the configuration dashboard.
**Run AI on this Mac** offers one-click local model setup; see the [hardware recommendations](docs/installation/macos-app.md#local-ai-models) for Qwen3.8 27B and smaller models matched to your Mac's memory.
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
2. **Single responder continues** - In a thread with one person, the agent or team that answered keeps responding without a tag
3. **Multiple agents collaborate** - Mention multiple agents when you want an ad-hoc collaboration
4. **Smart routing** - The router picks the best agent or team for a new thread when you don't tag one
5. **Group threads stay human-first** - Once two or more people talk in a thread, agents answer only when tagged, so they don't interrupt
6. **Adaptive participation** - An agent with [`participation`](docs/configuration/threads.md#adaptive-participation) enabled that has already replied in a group thread decides for each untagged message whether it can help, so nobody has to tag it
7. **DMs need no mentions** - Agents respond naturally in 1:1 rooms, and you can add more agents to a DM for private collaboration

<details>
<summary><b>Chat commands</b></summary>

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
- `!desktop [setup|status|confirm|rotate|disconnect]` - Manage your Desktop target
- `!mode <agent> minimal|standard|show|reset` - Switch one agent between standard tools and a Bash-only interface
- `!model [name|list|reset]` - Show or switch the model used in the current thread
- `!room_model [name|list|reset]` - Show the room model default or switch it (set/reset require a room admin)
- `!thread_mode [room|thread|reset|show]` - Show or switch the thread mode used in the current room (room admin only)
- `!encrypt [confirm]` - Enable end-to-end encryption for this room (irreversible, room admin only)
- `!e2ee` - Show encryption diagnostics for this room
- `!hi` - Show welcome message

<!-- OUTPUT:END -->

</details>

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
    accept_invites: true  # Accept all, none, or matching inviter ID patterns
    knowledge_bases: [engineering_docs]

models:
  default:
    provider: anthropic
    id: claude-sonnet-5-5

knowledge_bases:
  engineering_docs:
    path: ./knowledge_docs
    watch: true

voice:
  enabled: true
  stt:
    provider: openai
    model: gpt-transcribe

mindroom_user:
  username: mindroom_user  # Immutable once the account is created on first run
  display_name: MindRoomUser

administrators: ["@alice:example.com"]
room_defaults:
  invite_users: ["@alice:example.com"]

authorization:
  config_command_enabled: false
```

Environment variables go in `.env` next to the selected config file (`~/.mindroom/.env` for the hosted path):

```bash
MATRIX_HOMESERVER=https://your-matrix.server
ANTHROPIC_API_KEY=your-key-here
# Dashboard API key; generate one with `openssl rand -hex 32`.
# Set it empty (MINDROOM_API_KEY=) only with `--api-host 127.0.0.1`; `mindroom run` listens on every interface by default.
MINDROOM_API_KEY=replace-with-a-long-random-secret
```

Select another config with `mindroom run --config /path/to/config.yaml` or `export MINDROOM_CONFIG_PATH=/path/to/config.yaml` before running the CLI.
MindRoom selects the config before loading the `.env` beside it.

Teams, per-room models, context compaction, history controls, and memory backends are covered in the [configuration docs](docs/configuration/index.md) and at [docs.mindroom.chat](https://docs.mindroom.chat).

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
See [AGENTS.md](AGENTS.md) for the current development workflow and quality checks.

## License

- **Repository (except `saas-platform/`)**: [Apache License 2.0](LICENSE)
- **SaaS Platform** (`saas-platform/`): [Business Source License 1.1](saas-platform/LICENSE) (converts to Apache 2.0 on 2030-02-06)

## Acknowledgments

Built with:
- [Matrix](https://matrix.org/) - The federated communication protocol
- [Agno](https://agno.dev/) - AI agent framework
- [mindroom-nio](https://github.com/mindroom-ai/mindroom-nio) - Python Matrix client
