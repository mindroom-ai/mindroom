<div align="center">

<picture>
  <source media="(prefers-reduced-motion: no-preference)" srcset="https://raw.githubusercontent.com/mindroom-ai/mindroom/main/assets/logo/logo-mark-animated.svg" />
  <img src="https://raw.githubusercontent.com/mindroom-ai/mindroom/main/assets/logo/logo-mark.svg" alt="MindRoom logo" width="128" />
</picture>

# MindRoom

**AI agents that know you and your work, in a chat app anyone can use.**

Open source under Apache 2.0 · Any model, local or cloud · Self-host the whole stack

[Website](https://mindroom.chat) · [Docs](https://docs.mindroom.chat) · [Showcase](https://docs.mindroom.chat/showcase/) · [MindRoom Chat](https://chat.mindroom.chat) · [Hosted](https://mindroom.chat/#hosted) · [Quick start](#quick-start)

[![PyPI](https://img.shields.io/pypi/v/mindroom)](https://pypi.org/project/mindroom/)
[![Tests](https://img.shields.io/github/actions/workflow/status/mindroom-ai/mindroom/pytest.yml?label=tests)](https://github.com/mindroom-ai/mindroom/actions/workflows/pytest.yml)
[![Docs](https://img.shields.io/badge/docs-mindroom.chat-blue)](https://docs.mindroom.chat)
[![License](https://img.shields.io/github/license/mindroom-ai/mindroom)](https://github.com/mindroom-ai/mindroom/blob/main/LICENSE)
[![Downloads](https://img.shields.io/pypi/dm/mindroom)](https://pypi.org/project/mindroom/)

</div>

https://github.com/user-attachments/assets/f8325b3c-7ed0-4cd7-bc77-0c4cd74f226e

MindRoom gives you a personal agent for your calendar, notes, trips, and homelab, and shared agents for your team's email, documents, and code.
Every agent is a real user on [Matrix](https://matrix.org/), the open chat standard, so you talk to it in [MindRoom Chat](https://github.com/mindroom-ai/mindroom-chat), in any other Matrix client, or in Slack, Telegram, WhatsApp, and Discord through bridges.
Pick a [local model](https://docs.mindroom.chat/configuration/models/) for your private life or a frontier model for hard problems, and self-host the whole stack or run only the agents on your own computer.

**Run it on any computer**

```bash
uvx mindroom run
```

Needs [uv](https://docs.astral.sh/uv/getting-started/installation/) and a model: an API key, a subscription login such as Codex, or a local model.
It installs MindRoom with a starter agent, pairs it with your [MindRoom Chat](https://chat.mindroom.chat) account, and starts it.

**Or use the macOS app**

```bash
brew install --cask mindroom-ai/tap/mindroom
```

A native app that runs your agents on your Mac in the background, with one-click local models.
Needs an Apple silicon Mac with macOS 14 or later.

Chat with your agents on the [web](https://chat.mindroom.chat), on the [Mac](https://docs.mindroom.chat/installation/macos-app/), on [iPhone and iPad](https://apps.apple.com/us/app/mindroom-ai/id6760272172), and on Android (in beta).
Rather not run it yourself? [Try hosted MindRoom](https://mindroom.chat/#hosted).
See [Quick start](#quick-start) for every way to run it.

## See it in action

<table>
<tr>
<td width="50%" valign="top">
<a href="https://docs.mindroom.chat/showcase/#ask-approve-done"><picture><source media="(prefers-color-scheme: dark)" srcset="https://github.com/user-attachments/assets/5e389c59-c81a-4f70-9c5d-de7406ff95f4" /><img src="https://github.com/user-attachments/assets/bc6194b1-9398-47f5-b649-2ea17dbb6b9f" alt="A review dialog where the user approves an agent's calendar booking" /></picture></a>
<p><b>Approve before it acts</b><br />Actions you choose wait for your OK, with the exact arguments in view.</p>
</td>
<td width="50%" valign="top">
<a href="https://docs.mindroom.chat/showcase/#one-thread-the-whole-team"><picture><source media="(prefers-color-scheme: dark)" srcset="https://github.com/user-attachments/assets/635ce3ef-217d-494a-a8e5-01e3dd78454d" /><img src="https://github.com/user-attachments/assets/fbd1295d-025d-41d1-9ee8-0cc674706b98" alt="Three colleagues see the same agent thread side by side" /></picture></a>
<p><b>The whole team, one thread</b><br />Colleagues share an agent in a thread and see every answer stream in live.</p>
</td>
</tr>
<tr>
<td width="50%" valign="top">
<a href="https://docs.mindroom.chat/showcase/#canvases"><picture><source media="(prefers-color-scheme: dark)" srcset="https://github.com/user-attachments/assets/7fc2bc01-1ec5-40d1-971f-5e6f33ba0f18" /><img src="https://github.com/user-attachments/assets/8eb3e6d8-8e9f-4ebb-a51b-bbb5913536e3" alt="Two agent canvases: a week grid of free meeting slots and a weekend trip planner with a budget" /></picture></a>
<p><b>Canvases you can click</b><br />An agent lays out free slots or a trip budget beside the chat and acts on what you pick.</p>
</td>
<td width="50%" valign="top">
<a href="https://docs.mindroom.chat/showcase/#canvases"><picture><source media="(prefers-color-scheme: dark)" srcset="https://github.com/user-attachments/assets/6ccc9db8-6155-4d50-ae60-f4e011d39617" /><img src="https://github.com/user-attachments/assets/c8492e0c-4864-4753-ad1b-4a7d66e70c91" alt="A data canvas with a fitted curve, its parameters, and one point left out" /></picture></a>
<p><b>Data you can explore</b><br />Leave a point out of a fit, then send the new result back to the agent.</p>
</td>
</tr>
<tr>
<td width="50%" valign="top">
<a href="https://docs.mindroom.chat/showcase/#it-drives-you-take-the-wheel"><picture><source media="(prefers-color-scheme: dark)" srcset="https://github.com/user-attachments/assets/ddf9402f-a451-4f50-b070-1b50aab739c8" /><img src="https://github.com/user-attachments/assets/6b7a2673-d9b9-446a-b9a7-4e4240241489" alt="The agent hands over the passkey step next to its browser on the order page" /></picture></a>
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
<a href="https://docs.mindroom.chat/showcase/#your-morning-already-handled"><picture><source media="(prefers-color-scheme: dark)" srcset="https://github.com/user-attachments/assets/a78ee8fb-0828-45bc-9cc4-08d4181d017b" /><img src="https://github.com/user-attachments/assets/1ae37f4e-63b9-4cb0-aefd-e0c2431b997a" alt="A scheduled morning brief with today's meetings and what needs attention" /></picture></a>
<p><b>Your morning, already handled</b><br />A scheduled brief with today's meetings and what needs you.</p>
</td>
</tr>
</table>

These are stills from the [showcase](https://docs.mindroom.chat/showcase/) recordings: MindRoom Chat and a real MindRoom backend, with a fictional company and scripted model responses.

## What people use it for

### Personal

- Plan a family trip, from flights to a packing list.
- Keep your calendar, reminders, and to-do lists in order, by voice.
- Keep notes, a journal, and memories you can find months later.
- Follow the topics you care about, with a digest only when something is new.
- Look after your homelab and smart home, with approval for the actions you choose.
- Build quick tools and scripts in the agent's own workspace.

### Work

- Find anything across email, chat, documents, tickets, and code, with sources.
- Get a morning briefing, or a summary of your week before a one-on-one.
- Write status updates from what actually happened in chat and the tracker.
- Turn a question into a report or a presentation built from your own documents.
- Triage the inbox and draft emails, approving each one before it is sent.
- Join a new team and ask its agent how the work fits together.

## Why MindRoom

<table>
<tr>
<td width="50%" valign="top">

**🔌 [Connected to your tools and documents](https://docs.mindroom.chat/#agents-that-know-you-and-your-work)**<br />
Personal agents and shared team agents connect to 100+ tools, including email, calendar, Slack, Jira, GitHub, and any MCP server, and search your own documents.
Even from a server across the world, they can use a computer you pair.

</td>
<td width="50%" valign="top">

**🔒 [Private where it matters](https://docs.mindroom.chat/#private-where-it-matters)**<br />
Pick a model per agent: a local one for your most personal data, a frontier one for coding. With local memory and your own server, nothing that agent sees leaves your home, and what you tell a private agent stays out of shared ones.

</td>
</tr>
<tr>
<td valign="top">

**🧠 [Memory that keeps improving](https://docs.mindroom.chat/#they-remember-and-keep-improving)**<br />
Agents keep what matters from every conversation, get better as more people use them, and can turn work they repeat into reusable skills.

</td>
<td valign="top">

**🛡️ [Safe to give real access](https://docs.mindroom.chat/#safe-to-give-real-access)**<br />
One-tap approval for the actions you choose, sandboxed code execution (on by default when hosted), and opt-in end-to-end encryption on Matrix, the open standard governments use for secure messaging.

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

- **Multi-agent orchestration** — define specialist agents and teams in `config.yaml` or the dashboard; a built-in router picks the responder when you don't @-mention one, mentioning several agents makes them collaborate in a thread, and agents can hand tasks to each other or run reusable workflows ([Teams](https://docs.mindroom.chat/configuration/teams/), [Agent Orchestration](https://docs.mindroom.chat/tools/agent-orchestration/)).
- **Adaptive participation** — in a thread with several people, agents you opt in that already replied there decide for themselves when an untagged message is theirs to answer, and a judge can decide whether a new message should interrupt the current task or wait ([Threads](https://docs.mindroom.chat/configuration/threads/#adaptive-participation)).
- **Personal rooms and access control** — each person can get a private room with their own agent, and each agent answers only the people and rooms you allow ([Personal Rooms](https://docs.mindroom.chat/personal-rooms/), [Access Control](https://docs.mindroom.chat/authorization/)).

**Memory and learning**

- **Persistent memory** — agents remember people, preferences, and context across conversations and platforms (Mem0 + ChromaDB, stored on your disk).
- **Automatic skill learning** — opt-in background reviews turn work an agent repeats into a small library of reusable skills, following the self-improvement loop of Hermes Agent ([Skills](https://docs.mindroom.chat/skills/#automatic-skill-learning)).
- **Knowledge bases (RAG)** — point an agent at a folder or Git repository; MindRoom indexes it and can watch it for changes ([Knowledge Bases](https://docs.mindroom.chat/knowledge/)).

**Tools and computers**

- **100+ tool integrations** — Gmail, GitHub, Google Docs, Google Drive, Home Assistant, shell, Python, web search, any [MCP server](https://docs.mindroom.chat/mcp/), and more, plus native Matrix tools and a per-thread `todo` planner.
- **A real browser you can take over** — an agent works in its own persistent Chromium browser that you watch live in MindRoom Chat, take control of for a login or passkey, and hand back ([Worker Computer](https://docs.mindroom.chat/tools/worker-computer/)).
- **Reach any computer you pair** — your agents can run on a server across the world and still use your laptop: turn on the desktop bridge on any machine, and the agent can use the apps and folders you allow and run the commands you approve, over end-to-end encrypted Matrix with no open ports or VPN ([Desktop Bridge](https://docs.mindroom.chat/tools/desktop/)).
- **Lean prompts** — minimal mode gives an agent one Bash tool that calls its other tools, and dynamic tools load rarely used tools only when needed, so each request carries fewer tokens ([Minimal Mode](https://docs.mindroom.chat/tools/agent-cli/), [Dynamic Tools](https://docs.mindroom.chat/tools/dynamic-tools/)).

**In the chat**

- **Interactive canvases** — an agent can open a page it wrote, such as a dashboard, slide deck, form, or picker, beside the conversation, and what you pick there comes back as your next message ([Interactive Canvases](https://docs.mindroom.chat/canvases/)).
- **Approvals and questions** — one-tap approval cards show exactly what will be sent and to whom, a scheduled send can be approved when it is scheduled, and agents can ask multiple-choice questions you answer with a click ([Tool Approval](https://docs.mindroom.chat/tool-approval/), [Interactive Questions](https://docs.mindroom.chat/interactive/)).
- **Streaming responses** — agents type into the room with progressive edits, visible tool traces, and cancellation.
- **Voice** — voice messages are transcribed, agents can join Element Call voice calls and talk in real time, and text-to-speech tools use OpenAI, Groq, ElevenLabs, and Cartesia ([Voice Messages](https://docs.mindroom.chat/voice/), [Voice Calls](https://docs.mindroom.chat/voice-calls/)).
- **Model routing** — a different model per agent, room, or thread (`!model` and `!room_model`); route sensitive rooms to local Ollama and everything else to a cloud model.

**Automation**

- **Scheduling & automation** — cron or natural-language scheduled tasks (`!schedule`), including silent checks that post only when they find something, plus [supervised background Python watchers](https://docs.mindroom.chat/tools/background-scripts/) that can call governed agent tools and wake the agent only when something changes, and [external triggers](https://docs.mindroom.chat/external-triggers/) that let any process wake an agent with one HTTP request.

**Safety and privacy**

- **Isolated execution** — shell, Python, and coding tools can run in container workers with no access to the primary process's secrets, with per-tool [approval rules](https://docs.mindroom.chat/tool-approval/) and [approved egress](https://docs.mindroom.chat/deployment/approved-egress/) for locked-down environments ([Workers & Sandboxing](https://docs.mindroom.chat/deployment/sandbox-proxy/)).
- **End-to-end encryption** — rooms can be end-to-end encrypted with Matrix's Olm/Megolm ([End-to-End Encryption](https://docs.mindroom.chat/matrix/#end-to-end-encryption)).

**Run it anywhere**

- **Works with other apps** — bridges bring the same agents to Slack, Telegram, WhatsApp, Discord, and email, an [OpenAI-compatible API](https://docs.mindroom.chat/openai-api/) serves them to LibreChat, Open WebUI, and other clients, and an [MCP gateway](https://docs.mindroom.chat/deployment/mcp-gateway/) shares their tools with Claude Code and Codex ([Bridges](https://docs.mindroom.chat/deployment/bridges/)).
- **Native macOS app** — runs your agents on your Mac in the background, with a menu bar companion and one-click local model setup ([macOS App](https://docs.mindroom.chat/installation/macos-app/)).
- **Web dashboard** — create and configure agents, teams, models, tools, credentials, and knowledge bases by clicking instead of editing YAML; chat stays in your Matrix client.
- **Plugins & hooks** — drop-in [plugins](https://docs.mindroom.chat/plugins/) add custom tools, skills, and OAuth providers, and a typed [event-hook system](https://docs.mindroom.chat/hooks/) (per-hook timeouts, fault isolation) lets them observe and transform messages; reload plugins at runtime with `!reload-plugins`.
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

## How it compares to OpenClaw and Hermes

[OpenClaw](https://github.com/openclaw/openclaw) and [Hermes Agent](https://github.com/nousresearch/hermes-agent) are self-hosted assistants that pipe an agent into chat apps you already use.
MindRoom plays in the same space but makes different architectural bets:

- **Multi-agent and multi-user by default.** Both are personal-first: one owner talking to their assistant. In MindRoom every agent is a real Matrix user, so you run a fleet of specialists and teams, share them with family, a project, or a whole company, and scope access per user and per room.
- **An AI-native interface on an open protocol.** With WhatsApp, Signal, or Telegram as the front end, you rent UX from platforms that were never designed for agents and can cut bots off at any time. MindRoom's home is Matrix, with [MindRoom Chat](https://github.com/mindroom-ai/mindroom-chat) tuned for AI: collapsible tool-call traces, model metadata on every response, streaming with in-place edits, response cancellation, and first-class threads. Bridges to those apps are additive, not the foundation.
- **Sandboxing with real secrets isolation.** Execution tools (shell, Python, coding) can run in isolated container workers with no access to the primary process's secrets — your agent uses credentialed tools (Gmail, GitHub, ...) while code running in a worker cannot read those credentials. Per-tool [approval rules](https://docs.mindroom.chat/tool-approval/) and [egress approval](https://docs.mindroom.chat/deployment/approved-egress/) add human-in-the-loop control.
- **Batteries included.** 100+ built-in tool integrations with typed configuration, OAuth flows, and automatic dependency installation — plus OpenClaw-compatible skills on top.

Coming from OpenClaw? MindRoom [imports OpenClaw workspaces](https://docs.mindroom.chat/openclaw/) (`SOUL.md`, `MEMORY.md`, skills) and ships an `openclaw_compat` tool preset.

## Quick start

### Your computer + MindRoom Chat (fastest)

MindRoom runs on your computer; Matrix is hosted at `mindroom.chat` and the chat app at [chat.mindroom.chat](https://chat.mindroom.chat).
You need:

- [uv](https://docs.astral.sh/uv/getting-started/installation/), which installs Python when needed.
- A model: an API key, a subscription login such as Codex, or a local model.
- A MindRoom Chat account, which your first sign-in at [chat.mindroom.chat](https://chat.mindroom.chat) creates.

```bash
uvx mindroom run
# First run: choose a model provider; anthropic, openai, and openrouter
# also ask for an API key (Enter skips).
# MindRoom writes ~/.mindroom/config.yaml and ~/.mindroom/.env with hosted
# defaults, then prints a link and QR code: approve it with your MindRoom Chat
# account, or enter the code in MindRoom Chat -> Settings -> Local MindRoom.
# Finally, Enter keeps MindRoom running in the background as a login service
# (systemd or launchd); n runs it here instead.
```

Coding agents and scripts can answer every question with flags instead: `OPENAI_API_KEY=sk-... uvx mindroom run --provider openai --service`.
To create or review the files without starting, run `uvx mindroom config init` (optionally with `--provider codex` or another preset), edit `~/.mindroom/.env`, and then run `uvx mindroom run`.
See the [hosted Matrix deployment guide](https://docs.mindroom.chat/deployment/hosted-matrix/) for full details.

### macOS app

The macOS app provides a native window and menu bar companion for local agents and computer access.
It bundles `uv`, uses `~/.mindroom` for config and state, and manages the `mindroom service` launchd service.
The signed app requires an Apple silicon Mac.

```bash
brew install --cask mindroom-ai/tap/mindroom
```

Open **MindRoom** from `/Applications` to set up local agents, manage computer access, or open chat and the configuration dashboard.
**Run AI on this Mac** offers one-click local model setup; see the [hardware recommendations](https://docs.mindroom.chat/installation/macos-app/#local-ai-models) for Qwen3.8 27B and smaller models matched to your Mac's memory.
See the [macOS app guide](https://docs.mindroom.chat/installation/macos-app/) for setup, updates, and uninstall instructions.

### Full stack with Docker Compose

To self-host everything, including the Matrix homeserver and the chat app, use [mindroom-stack](https://github.com/mindroom-ai/mindroom-stack) with Docker, Docker Compose, and Python 3:

```bash
git clone https://github.com/mindroom-ai/mindroom-stack
cd mindroom-stack
cp .env.example .env
$EDITOR .env  # set ANTHROPIC_API_KEY for the default stack config
./scripts/quickstart.py
```

It serves the dashboard at http://localhost:8765, MindRoom Chat at http://localhost:8080, and Matrix at http://localhost:8008.
See [Full stack Docker Compose](https://docs.mindroom.chat/getting-started/#full-stack-docker-compose) for other providers and access from other devices, and [Deployment](#deployment) for Kubernetes.

### From source, for contributors

To work on MindRoom itself, run it from a checkout.

<details>
<summary><b>Run from source</b></summary>

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
See the [manual configuration and account provisioning instructions](https://docs.mindroom.chat/getting-started/#configuration) for homeserver setup.
The starter creates the `Mind` agent in the `personal` room.

With dashboard assets available, open http://localhost:8765.
Without assets and Bun, the API is available at http://localhost:8765/api but the dashboard is unavailable.
Matrix E2EE support is installed by default.

</details>

### First steps

Open the **Personal** room in MindRoom Chat ([chat.mindroom.chat](https://chat.mindroom.chat), or the chat app of your own stack) and say hello: the starter agent `Mind` is the room's only responder, so it answers without a mention.

Then add more agents and teams in the dashboard or `config.yaml` (see [Configuration](#configuration)), and mention several in one message to have them work together.

## How agents respond

Agents and teams respond using Matrix thread relations to keep conversations organized.
If your client or bridge only sends plain replies, MindRoom keeps them in an existing thread when the reply chain eventually reaches a threaded ancestor or proven thread root.
Plain replies that never reach threaded context still stay plain replies.

1. **Mentioned agents and teams respond** - Tag them to get their attention
2. **Single responder continues** - In a thread with one person, the agent or team that answered keeps responding without a tag
3. **Multiple agents collaborate** - Mention multiple agents when you want an ad-hoc collaboration
4. **Smart routing** - The router picks the best agent or team for a new thread when you don't tag one
5. **Group threads stay human-first** - Once two or more people talk in a thread, agents answer only when tagged, so they don't interrupt
6. **Adaptive participation** - An agent with [`participation`](https://docs.mindroom.chat/configuration/threads/#adaptive-participation) enabled that has already replied in a group thread decides for each untagged message whether it can help, so nobody has to tag it
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
Either way, changes are hot-reloaded, and most take effect without a restart.

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
# `mindroom run` listens on every network interface by default, so keep this key set;
# leave it empty (MINDROOM_API_KEY=) only together with `--api-host 127.0.0.1`.
MINDROOM_API_KEY=replace-with-a-long-random-secret
```

Select another config with `mindroom run --config /path/to/config.yaml` or `export MINDROOM_CONFIG_PATH=/path/to/config.yaml` before running the CLI.
MindRoom selects the config before loading the `.env` beside it.

Teams, per-room models, context compaction, history controls, and memory backends are covered in the [configuration docs](https://docs.mindroom.chat/configuration/) and at [docs.mindroom.chat](https://docs.mindroom.chat).

## Deployment

- **Own homeserver** — set `MATRIX_HOMESERVER` and run against any Synapse, Conduit, or Dendrite instance ([guide](https://docs.mindroom.chat/getting-started/#manual-install-with-your-own-matrix-homeserver)).
- **Local stack** — `mindroom local-stack-setup` bootstraps a local Synapse + MindRoom Chat via Docker.
- **Your computer + MindRoom Chat (hosted Matrix)** — run only the backend locally against hosted Matrix at [mindroom.chat](https://mindroom.chat), pairing via [chat.mindroom.chat](https://chat.mindroom.chat) ([guide](https://docs.mindroom.chat/deployment/hosted-matrix/)).
- **Docker** — single-container runtime ([guide](https://docs.mindroom.chat/deployment/docker/)).
- **Kubernetes** — Helm charts for enterprise-scale, multi-tenant deployments ([guide](https://docs.mindroom.chat/deployment/kubernetes/)).
- **Agent-managed NixOS container (advanced)** — the author's favorite for personal use: [mindroom-ai/lxc-nixos](https://github.com/mindroom-ai/lxc-nixos) provisions a persistent, agent-controlled NixOS container with the full stack, which the agent can rebuild and manage itself while the host controls what it sees.
- **Bridges** — connect Slack, Telegram, WhatsApp, and more via [Bridges](https://docs.mindroom.chat/deployment/bridges/).

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

See the [architecture docs](https://docs.mindroom.chat/architecture/) for internals.

## Note for self-hosters

This repository contains everything you need to self-host MindRoom.
The `saas-platform/` directory contains infrastructure specific to running MindRoom as a hosted service and can be safely ignored by self-hosters.

## Contributing

We welcome contributions!
See [AGENTS.md](https://github.com/mindroom-ai/mindroom/blob/main/AGENTS.md) for the current development workflow and quality checks.

## License

- **Repository (except `saas-platform/`)**: [Apache License 2.0](https://github.com/mindroom-ai/mindroom/blob/main/LICENSE)
- **SaaS Platform** (`saas-platform/`): [Business Source License 1.1](https://github.com/mindroom-ai/mindroom/blob/main/saas-platform/LICENSE) (converts to Apache 2.0 on 2030-02-06)

## Acknowledgments

Built with:
- [Matrix](https://matrix.org/) - The federated communication protocol
- [Agno](https://agno.dev/) - AI agent framework
- [mindroom-nio](https://github.com/mindroom-ai/mindroom-nio) - Python Matrix client
