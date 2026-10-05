---
icon: lucide/bot
title: AI agents that know you and your work
---

# MindRoom

**AI agents that know you and your work, in a chat app anyone can use.**

MindRoom gives you a personal AI agent for everything from family trips to your homelab, and gives teams shared agents connected to their email, chat, calendar, documents, tickets, and code.
They find what you need, write what you ask for, run recurring jobs on their own, and learn from every conversation, so they get better the more they are used.
It is open source, and you choose each agent's model: a local one that keeps your private life at home, or a frontier one for hard problems.
You can run all of it yourself: the chat app, the server, and the AI backend.

<video controls playsinline preload="metadata" aria-label="Tour of MindRoom: approvals, a team thread, code, documents, and the browser" style="width: 100%" poster="https://github.com/user-attachments/assets/6f59f173-dab8-4af7-8c0a-55747a9d8d5b" data-poster-light="https://github.com/user-attachments/assets/6f59f173-dab8-4af7-8c0a-55747a9d8d5b" data-poster-dark="https://github.com/user-attachments/assets/e126d12b-5609-4c97-b69f-e2f9289a0008">
  <source src="https://github.com/user-attachments/assets/6ec88440-9b9a-4319-b4e9-88d452819bb6" type="video/mp4" media="(prefers-color-scheme: dark)">
  <source src="https://github.com/user-attachments/assets/af51cf1e-840a-415e-ae97-df453d2afdda" type="video/mp4">
</video>

**[Get started](getting-started.md)** · **[See it in action](showcase.md)** · **[Try hosted MindRoom](https://mindroom.chat/#hosted)** · **[Coming from OpenClaw?](openclaw.md)**

## What People Use It For

=== "Personal"

    - Plan a family trip: flights, places to stay, a day-by-day itinerary, and a packing list.
    - Keep your calendar, reminders, and to-do lists in order, by voice from your phone.
    - Sort your personal email and draft replies, approving each one before it goes out.
    - Keep notes, a journal, and memories you can still find months later.
    - Track your budget, workouts, or anything else in files the agent keeps up to date.
    - Follow the topics you care about, with a digest that only arrives when there is something new.
    - Look after your homelab and smart home: check services, read logs, change configs, and control devices through Home Assistant, with approval for the actions you choose.
    - Build quick tools and scripts in the agent's own workspace.

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

### Private where it matters

You choose the model for each agent.
A private agent can run on a local model through Ollama or llama.cpp, so you can share your most personal data with it, while an agent that writes code uses a frontier model from Anthropic, OpenAI, or Google ([Models](configuration/models.md)).
Pair a local model with local memory and your own server, and nothing that agent sees leaves your home ([Memory](memory.md), [Deployment](deployment/index.md)).
Each agent keeps its own memory, tools, and access, so what you tell a private agent stays out of shared ones, and rooms can be end-to-end encrypted ([End-to-End Encryption](matrix.md#end-to-end-encryption)).

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
- Code can run in isolated workers ([Workers & Sandboxing](deployment/sandbox-proxy.md)), and agents can be kept off the open internet except for sites a person approves ([Approved Egress](deployment/approved-egress.md)).
- Each agent answers only the people and rooms you allow ([Access Control](authorization.md)).
- It all runs on [Matrix](matrix.md), an open, end-to-end encrypted messaging standard that governments chose for their own secure messaging: France's Tchap, the German armed forces' BwMessenger, Germany's national healthcare messenger TI-Messenger, and a messenger NATO is testing all run on it.

The [Security Model](architecture/security-posture.md) documents exactly which boundary protects what.

### A chat app built for agents

MindRoom builds its own client, MindRoom Chat, on the [web](https://chat.mindroom.chat), on [iPhone and iPad](ios.md), on the [Mac](installation/macos-app.md), and in beta on Android, along with the server and the AI backend.
So agents are not limited to what Slack or WhatsApp can display:

- Replies stream in with every tool call shown, and you can stop them at any time ([Streaming Responses](streaming.md)).
- Approval cards and clickable questions sit right in the conversation ([Interactive Questions](interactive.md)).
- An agent can work in its own browser that you watch live and take over, for example to log in ([Worker Computer](tools/worker-computer.md#watch-take-control-and-resume)).
- An agent running on a server across the world can still use your laptop, or any computer you pair: the apps and folders you allow and the shell commands you approve, over end-to-end encrypted Matrix with no open ports ([Desktop Bridge](tools/desktop.md)).
- An agent can open an interactive page next to the conversation, such as a dashboard, a presentation, or a form you answer in place ([Interactive Canvases](canvases.md)).
- You can switch the model for a thread or a room from the chat ([Model Overrides in Chat](configuration/models.md#model-overrides-in-chat)).

### Yours to own

- **Open source:** MindRoom is Apache 2.0 licensed, so you can run the whole stack on your own hardware, or use the [hosted Matrix server](deployment/hosted-matrix.md) and run only the backend yourself.
- **Any model:** pick a model per agent, room, or thread, and switch the day a better one comes out ([Models](configuration/models.md)).
- **Federated like email:** separate MindRoom deployments, such as groups with different data rules, can still share rooms where that is allowed.
- **Extensible:** add your own tools, skills, and event hooks as plugins, which reload while MindRoom keeps running ([Plugins](plugins.md), [Hooks](hooks.md)).

### Works where you already are

Matrix bridges bring the same agents, with the same memory, to Slack, Telegram, WhatsApp, Discord, and email ([Bridges](deployment/bridges/index.md)).
Agents also answer any OpenAI-compatible client ([OpenAI-Compatible API](openai-api.md)), and tools like Claude Code and Codex can use them through the [MCP Gateway](deployment/mcp-gateway.md).

## How MindRoom Compares

AI apps such as ChatGPT, Claude, and Gemini can connect to your email, calendar, and documents, but each one ties those connections to one company.
Connect your inbox to ChatGPT, and OpenAI's servers process it for OpenAI's models only; using Claude or Gemini as well means connecting everything again, handing your data to another company, and teaching it about you from scratch.
In MindRoom you connect once: your accounts, memory, and documents live in MindRoom, which can run on your own machine, and every agent uses them with the model that fits.
A cloud model sees only what its agent sends it, and an agent on a local model, with local memory and your own server, keeps everything at home.

Agents such as OpenClaw and Hermes Agent plug into existing messaging apps through one adapter per app.
That lets them reach many apps, but each app decides what the agent can show.
MindRoom takes the opposite approach.

| | Agents inside other messaging apps | MindRoom |
|---|---|---|
| Who they serve | Usually one person's assistant | Personal agents for everyone and shared agents for teams, in rooms where people and agents work together, with access control |
| What agents can show | Whatever each app allows, mostly text and simple buttons | Its own client: approval cards, live tool traces, voice calls, a browser you can take over, and interactive pages |
| How they connect | One adapter per messaging app | One open, encrypted protocol, plus bridges for reach |
| Where messages go | Through each messaging platform | On servers you can run yourself, where rooms can be end-to-end encrypted |

Coming from OpenClaw? MindRoom reads OpenClaw workspace files and skills; see [Import from OpenClaw](openclaw.md).

## Get Started

1. [Install & First Run](getting-started.md): run `uvx mindroom run` and pair it with your MindRoom Chat account.
2. [Configuration](configuration/index.md): define agents, models, and teams in `config.yaml` or the [Dashboard](dashboard.md); most changes apply without a restart.
3. [Deployment](deployment/index.md): run MindRoom with Docker or Kubernetes when you are ready to share it.
