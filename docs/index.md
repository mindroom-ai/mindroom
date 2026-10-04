---
icon: lucide/bot
---

# MindRoom

**AI agents that know you and your work, in a chat app anyone can use.**

MindRoom gives you a personal AI agent for everything from family trips to your homelab, and gives teams shared agents connected to their email, chat, calendar, documents, tickets, and code.
They find what you need, write what you ask for, run recurring jobs on their own, and learn from every conversation, so they get better the more they are used.
It is open source, works with any model, and you can run all of it yourself: the chat app, the server, and the AI backend.

<video controls playsinline preload="metadata" aria-label="Tour of MindRoom: approvals, a team thread, code, documents, and the browser" style="width: 100%">
  <source src="https://github.com/user-attachments/assets/d24be091-deb0-4be0-8346-3faa6b1e40c4#t=0.1" type="video/mp4" media="(prefers-color-scheme: dark)">
  <source src="https://github.com/user-attachments/assets/7c51fefe-a5d5-460b-ad97-d7886839af3b#t=0.1" type="video/mp4">
</video>

**[Get started](getting-started.md)** · **[See it in action](showcase.md)** · **[Coming from OpenClaw?](openclaw.md)**

## What People Use It For

=== "Personal"

    - Plan a family trip: flights, places to stay, a day-by-day itinerary, and a packing list.
    - Keep your calendar, reminders, and to-do lists in order, by voice from your phone.
    - Look after your home network and homelab: check services, read logs, and change configs from the chat, with approval rules for anything risky.
    - Build quick tools and scripts in the agent's own workspace.
    - Keep notes and memories you can still find months later.
    - Follow the topics you care about, with a digest that only arrives when there is something new.
    - Control your smart home through Home Assistant.

=== "Work"

    - Find anything across email, chat, documents, tickets, and code, and see where each answer came from.
    - Get a morning briefing, or a summary of your week before every one-on-one.
    - Write status updates from what actually happened in chat and the tracker, not from what people remembered to report.
    - Turn a question into a report or an interactive presentation built from your own documents.
    - Triage the inbox, prepare for meetings, and draft emails, then approve each one before it is sent.
    - Investigate a technical question across specifications, code, and records, with every claim traced to its source.
    - Join a new team and ask its agent how the work fits together and whom to ask.

## Why MindRoom

### Agents that know you and your work

Agents connect to 100+ tools, including Gmail, Google Drive, Slack, Jira, Confluence, GitHub, and any MCP server ([Tools](tools/index.md), [MCP Servers](mcp.md)), and search knowledge bases built from your folders and Git repositories ([Knowledge Bases](knowledge.md)).
Each person can get a private room with their own agent ([Personal Rooms](personal-rooms.md)), each team can share agents that answer only its members ([Access Control](authorization.md)), and agents can work as a team or hand tasks to each other ([Teams](configuration/teams.md), [Agent Orchestration](tools/agent-orchestration.md)).

### They remember, and keep improving

Agents keep what is worth remembering from every conversation without being asked, and look it up again when it becomes relevant ([Memory](memory.md)).
A shared agent gets better as more people use it: what it learns while helping one person helps the next.
With skill learning on, an agent also turns work it repeats into reusable skills, following the self-improvement loop of Hermes Agent ([Automatic Skill Learning](skills.md#automatic-skill-learning)).
You can teach it directly too: tell it a preference or a correction, and it is remembered.

### They work while you don't

Schedule recurring jobs in plain language, such as a daily briefing or a weekly report, including silent checks that post only when something changed ([Scheduling](scheduling.md)).
Send a voice message from your phone ([Voice Messages](voice.md)), or call an agent and talk while it hands longer tasks to background agents ([Voice Calls](voice-calls.md)).

### Safe to give real access

- Sending or changing something can wait for your one-tap approval, which shows exactly what will be sent and to whom ([Tool Approval](tool-approval.md)).
- Code runs in isolated workers ([Workers & Sandboxing](deployment/sandbox-proxy.md)), and agents can be kept off the open internet except for sites a person approves ([Approved Egress](deployment/approved-egress.md)).
- Each agent answers only the people and rooms you allow ([Access Control](authorization.md)).
- It all runs on [Matrix](matrix.md), an open, end-to-end encrypted messaging standard that governments chose for their own secure messaging: France's Tchap, the German armed forces' BwMessenger, Germany's national healthcare messenger TI-Messenger, and a messenger NATO is testing all run on it.

The [Security Model](architecture/security-posture.md) documents exactly which boundary protects what.

### A chat app built for agents

MindRoom builds its own client, MindRoom Chat, on the [web](https://chat.mindroom.chat), on [iPhone and iPad](ios.md), and on the [Mac](installation/macos-app.md), along with the server and the AI backend.
So agents are not limited to what Slack or WhatsApp can display:

- Replies stream in with every tool call shown, and you can stop them at any time ([Streaming Responses](streaming.md)).
- Approval cards and clickable questions sit right in the conversation ([Interactive Questions](interactive.md)).
- An agent can work in its own browser that you watch live and take over, for example to log in ([Worker Computer](tools/worker-computer.md#watch-take-control-and-resume)).
- An agent can use the apps and folders you select on your own computer, and runs shell commands only after you approve them ([Desktop Bridge](tools/desktop.md)).
- An agent can open an interactive page next to the conversation, such as a dashboard or a presentation ([Chat UI Actions](tools/chat-ui.md#interactive-canvases)).
- You can switch the model for a thread or a room from the chat ([Model Overrides in Chat](configuration/models.md#model-overrides-in-chat)).

### Yours to own

- **Open source:** MindRoom is Apache 2.0 licensed, so you can run the whole stack on your own hardware, or use the [hosted Matrix server](deployment/hosted-matrix.md) and run only the backend yourself.
- **Any model:** use cloud models from Anthropic, OpenAI, Google, and others, or local models through Ollama and llama.cpp, chosen per agent, room, or thread, and switch the day a better model comes out ([Models](configuration/models.md)).
- **Federated like email:** separate MindRoom deployments, such as groups with different data rules, can still share rooms where that is allowed.
- **Extensible:** add your own tools, skills, and event hooks as plugins, which reload while MindRoom keeps running ([Plugins](plugins.md), [Hooks](hooks.md)).

### Works where you already are

Matrix bridges bring the same agents, with the same memory, to Slack, Telegram, WhatsApp, Discord, and email ([Bridges](deployment/bridges/index.md)).
Agents also answer any OpenAI-compatible client ([OpenAI-Compatible API](openai-api.md)), and tools like Claude Code and Codex can use them through the [MCP Gateway](deployment/mcp-gateway.md).

## How MindRoom Compares

Agents such as OpenClaw and Hermes Agent plug into existing messaging apps through one adapter per app.
That lets them reach many apps, but each app decides what the agent can show.
MindRoom takes the opposite approach.

| | Agents inside other messaging apps | MindRoom |
|---|---|---|
| Who they serve | Usually one person's assistant | Personal agents for everyone and shared agents for teams, in rooms where people and agents work together, with access control |
| What agents can show | Whatever each app allows, mostly text and simple buttons | Its own client: approval cards, live tool traces, voice calls, a browser you can take over, and interactive pages |
| How they connect | One adapter per messaging app | One open, encrypted protocol, plus bridges for reach |
| Where messages go | Through each messaging platform | End-to-end encrypted, on servers you can run yourself |

Coming from OpenClaw? MindRoom reads OpenClaw workspace files and skills; see [Import from OpenClaw](openclaw.md).

## Get Started

1. [Install & First Run](getting-started.md): run `uvx mindroom run` and pair it with your MindRoom Chat account.
2. [Configuration](configuration/index.md): define agents, models, and teams in `config.yaml` or the [Dashboard](dashboard.md); most changes apply without a restart.
3. [Deployment](deployment/index.md): run MindRoom with Docker or Kubernetes when you are ready to share it.
