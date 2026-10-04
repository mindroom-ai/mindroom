# MindRoom

**AI agents that know your work, in a chat app anyone can use.**

MindRoom gives everyone a personal AI agent, and every team shared agents connected to its email, chat, calendar, documents, tickets, and code.
They find what you need, write what you ask for, run recurring jobs on their own, and learn from every conversation, so they get better the more your team uses them.
It is open source, works with any model, and you can run all of it yourself: the chat app, the server, and the AI backend.

<video controls playsinline preload="metadata" aria-label="Tour of MindRoom: approvals, a team thread, code, documents, and the browser" style="width: 100%">
  <source src="https://github.com/user-attachments/assets/d24be091-deb0-4be0-8346-3faa6b1e40c4#t=0.1" type="video/mp4" media="(prefers-color-scheme: dark)">
  <source src="https://github.com/user-attachments/assets/7c51fefe-a5d5-460b-ad97-d7886839af3b#t=0.1" type="video/mp4">
</video>

**[Get started](https://docs.mindroom.chat/getting-started/)** · **[See it in action](https://docs.mindroom.chat/showcase/)** · **[Coming from OpenClaw?](https://docs.mindroom.chat/openclaw/)**

## What People Use It For

- Find anything across email, chat, documents, tickets, and code, and see where each answer came from.
- Get a morning briefing, or a summary of your week before every one-on-one.
- Write status updates from what actually happened in chat and the tracker, not from what people remembered to report.
- Turn a question into a report or an interactive presentation built from your own documents.
- Triage the inbox, prepare for meetings, and draft emails, then approve each one before it is sent.
- Investigate a technical question across specifications, code, and records, with every claim traced to its source.
- Join a new team and ask its agent how the work fits together and whom to ask.

## Why MindRoom

### Agents that know your work

Agents connect to 100+ tools, including Gmail, Google Drive, Slack, Jira, Confluence, GitHub, and any MCP server ([Tools](https://docs.mindroom.chat/tools/), [MCP Servers](https://docs.mindroom.chat/mcp/)), and search knowledge bases built from your folders and Git repositories ([Knowledge Bases](https://docs.mindroom.chat/knowledge/)).
Each person can get a private room with their own agent ([Personal Rooms](https://docs.mindroom.chat/personal-rooms/)), each team can share agents that answer only its members ([Access Control](https://docs.mindroom.chat/authorization/)), and agents can work as a team or hand tasks to each other ([Teams](https://docs.mindroom.chat/configuration/teams/), [Agent Orchestration](https://docs.mindroom.chat/tools/agent-orchestration/)).

### They remember, and keep improving

Agents keep what is worth remembering from every conversation without being asked, and look it up again when it becomes relevant ([Memory](https://docs.mindroom.chat/memory/)).
A shared agent gets better as more people use it: what it learns while helping one person helps the next.
With skill learning on, an agent also turns work it repeats into reusable skills, following the self-improvement loop of Hermes Agent ([Automatic Skill Learning](https://docs.mindroom.chat/skills/#automatic-skill-learning)).
You can teach it directly too: tell it a preference or a correction, and it is remembered.

### They work while you don't

Schedule recurring jobs in plain language, such as a daily briefing or a weekly report, including silent checks that post only when something changed ([Scheduling](https://docs.mindroom.chat/scheduling/)).
Send a voice message from your phone ([Voice Messages](https://docs.mindroom.chat/voice/)), or call an agent and talk while it hands longer tasks to background agents ([Voice Calls](https://docs.mindroom.chat/voice-calls/)).

### Safe to give real access

- Sending or changing something can wait for your one-tap approval, which shows exactly what will be sent and to whom ([Tool Approval](https://docs.mindroom.chat/tool-approval/)).
- Code runs in isolated workers ([Workers & Sandboxing](https://docs.mindroom.chat/deployment/sandbox-proxy/)), and agents can be kept off the open internet except for sites a person approves ([Approved Egress](https://docs.mindroom.chat/deployment/approved-egress/)).
- Each agent answers only the people and rooms you allow ([Access Control](https://docs.mindroom.chat/authorization/)).
- It all runs on [Matrix](https://docs.mindroom.chat/matrix/), an open, end-to-end encrypted messaging standard that governments chose for their own secure messaging: France's Tchap, the German armed forces' BwMessenger, Germany's national healthcare messenger TI-Messenger, and a messenger NATO is testing all run on it.

The [Security Model](https://docs.mindroom.chat/architecture/security-posture/) documents exactly which boundary protects what.

### A chat app built for agents

MindRoom builds its own client, MindRoom Chat, on the [web](https://chat.mindroom.chat), on [iPhone and iPad](https://docs.mindroom.chat/ios/), and on the [Mac](https://docs.mindroom.chat/installation/macos-app/), along with the server and the AI backend.
So agents are not limited to what Slack or WhatsApp can display:

- Replies stream in with every tool call shown, and you can stop them at any time ([Streaming Responses](https://docs.mindroom.chat/streaming/)).
- Approval cards and clickable questions sit right in the conversation ([Interactive Questions](https://docs.mindroom.chat/interactive/)).
- An agent can work in its own browser that you watch live and take over, for example to log in ([Worker Computer](https://docs.mindroom.chat/tools/worker-computer/#watch-take-control-and-resume)).
- An agent can use the apps and folders you select on your own computer, and runs shell commands only after you approve them ([Desktop Bridge](https://docs.mindroom.chat/tools/desktop/)).
- An agent can open an interactive page next to the conversation, such as a dashboard or a presentation ([Chat UI Actions](https://docs.mindroom.chat/tools/chat-ui/#interactive-canvases)).
- You can switch the model for a thread or a room from the chat ([Model Overrides in Chat](https://docs.mindroom.chat/configuration/models/#model-overrides-in-chat)).

### Yours to own

- **Open source:** MindRoom is Apache 2.0 licensed, so you can run the whole stack on your own hardware, or use the [hosted Matrix server](https://docs.mindroom.chat/deployment/hosted-matrix/) and run only the backend yourself.
- **Any model:** use cloud models from Anthropic, OpenAI, Google, and others, or local models through Ollama and llama.cpp, chosen per agent, room, or thread, and switch the day a better model comes out ([Models](https://docs.mindroom.chat/configuration/models/)).
- **Federated like email:** separate MindRoom deployments, such as groups with different data rules, can still share rooms where that is allowed.
- **Extensible:** add your own tools, skills, and event hooks as plugins, which reload while MindRoom keeps running ([Plugins](https://docs.mindroom.chat/plugins/), [Hooks](https://docs.mindroom.chat/hooks/)).

### Works where you already are

Matrix bridges bring the same agents, with the same memory, to Slack, Telegram, WhatsApp, Discord, and email ([Bridges](https://docs.mindroom.chat/deployment/bridges/)).
Agents also answer any OpenAI-compatible client ([OpenAI-Compatible API](https://docs.mindroom.chat/openai-api/)), and tools like Claude Code and Codex can use them through the [MCP Gateway](https://docs.mindroom.chat/deployment/mcp-gateway/).

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

Coming from OpenClaw? MindRoom reads OpenClaw workspace files and skills; see [Import from OpenClaw](https://docs.mindroom.chat/openclaw/).

## Get Started

1. [Install & First Run](https://docs.mindroom.chat/getting-started/): run `uvx mindroom run` and pair it with your MindRoom Chat account.
2. [Configuration](https://docs.mindroom.chat/configuration/): define agents, models, and teams in `config.yaml` or the [Dashboard](https://docs.mindroom.chat/dashboard/); most changes apply without a restart.
3. [Deployment](https://docs.mindroom.chat/deployment/): run MindRoom with Docker or Kubernetes when you are ready to share it.
