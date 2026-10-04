# MindRoom

**Your AI agents, in a chat app built for them.**

MindRoom gives people and teams AI agents with memory, tools, and a browser of their own, in an open-source chat app you can run yourself.
Because MindRoom builds the whole stack, from the chat client to the server to the AI backend, its agents can do what bots inside Slack or WhatsApp cannot: show interactive pages, ask for your approval with one tap, join voice calls, and work in a browser while you watch.

<video controls playsinline preload="metadata" aria-label="Tour of MindRoom: approvals, a team thread, code, documents, and the browser" style="width: 100%">
  <source src="https://github.com/user-attachments/assets/d24be091-deb0-4be0-8346-3faa6b1e40c4#t=0.1" type="video/mp4" media="(prefers-color-scheme: dark)">
  <source src="https://github.com/user-attachments/assets/7c51fefe-a5d5-460b-ad97-d7886839af3b#t=0.1" type="video/mp4">
</video>

**[Get started](https://docs.mindroom.chat/getting-started/)** · **[See it in action](https://docs.mindroom.chat/showcase/)** · **[Coming from OpenClaw?](https://docs.mindroom.chat/openclaw/)**

## Why MindRoom

### Built for AI, end to end

MindRoom ships its own chat client, MindRoom Chat, on the [web](https://chat.mindroom.chat), on [iPhone and iPad](https://docs.mindroom.chat/ios/), and on the [Mac](https://docs.mindroom.chat/installation/macos-app/), so the interface keeps up with what agents can do:

- **Interactive canvases:** an agent can open a live web page next to the conversation, such as a dashboard or a presentation ([Chat UI Actions](https://docs.mindroom.chat/tools/chat-ui/#interactive-canvases)).
- **One-tap approvals:** the tool calls you choose wait for a person to approve them right in the conversation ([Tool Approval](https://docs.mindroom.chat/tool-approval/)).
- **Visible work:** replies stream in with every tool call shown, and you can stop a reply at any time ([Streaming Responses](https://docs.mindroom.chat/streaming/)).
- **Voice calls:** agents join calls in their rooms and talk with you in real time ([Voice Calls](https://docs.mindroom.chat/voice-calls/)).
- **A browser you can take over:** an agent can work in its own browser that you watch live and take control of, for example to log in ([Worker Computer](https://docs.mindroom.chat/tools/worker-computer/#watch-take-control-and-resume)).
- **Your computer, with your consent:** an agent can use the apps and folders you select on your computer, and runs shell commands only after you approve them ([Desktop Bridge](https://docs.mindroom.chat/tools/desktop/)).
- **Models on the fly:** switch the model for a thread or a room from the chat ([Model Overrides in Chat](https://docs.mindroom.chat/configuration/models/#model-overrides-in-chat)).

### Secure by design, on an open standard

MindRoom is built on [Matrix](https://docs.mindroom.chat/matrix/), an open, end-to-end encrypted messaging standard that anyone can run.
Governments chose Matrix for their own secure messaging: the French government's Tchap, the German armed forces' BwMessenger, Germany's national healthcare messenger TI-Messenger, and a messenger NATO is testing all run on it.
On top of that, MindRoom can run code-executing tools in [isolated workers](https://docs.mindroom.chat/deployment/sandbox-proxy/), and keep agents off the open internet except for allowed sites and the ones a person approves ([Approved Egress](https://docs.mindroom.chat/deployment/approved-egress/)).
The [Security Model](https://docs.mindroom.chat/architecture/security-posture/) documents exactly which boundary protects what.

### Yours to own

- **Open source:** MindRoom is Apache 2.0 licensed, so you can run the whole stack (Matrix server, AI backend, and chat client) on your own hardware, or use the [hosted Matrix server](https://docs.mindroom.chat/deployment/hosted-matrix/) and run only the backend yourself.
- **Any model:** use cloud models from Anthropic, OpenAI, Google, and others, or local models through Ollama and llama.cpp, chosen per agent, room, or thread ([Models](https://docs.mindroom.chat/configuration/models/)).
- **Your data:** when you self-host, conversations, memory, and files stay on your own storage.

### Works where you already are

Matrix bridges bring the same agents, with the same memory, to Slack, Telegram, WhatsApp, Discord, and email ([Bridges](https://docs.mindroom.chat/deployment/bridges/)).
Agents also answer any OpenAI-compatible client ([OpenAI-Compatible API](https://docs.mindroom.chat/openai-api/)) and tools like Claude Code and Codex through the [MCP Gateway](https://docs.mindroom.chat/deployment/mcp-gateway/).

## How MindRoom Compares

Agents such as OpenClaw and Hermes Agent plug into existing messaging apps through one adapter per app.
That lets them reach many apps, but each app decides what the agent can show.
MindRoom takes the opposite approach.

| | Agents inside other messaging apps | MindRoom |
|---|---|---|
| What agents can show | Whatever each app allows, mostly text and simple buttons | Its own client: canvases, approval cards, live tool traces, voice calls, and a browser you can take over |
| How they connect | One adapter per messaging app | One open, encrypted protocol, plus bridges for reach |
| Who they serve | Usually one person's assistant | Shared rooms where many people and many agents work together, with access control |
| Where messages go | Through each messaging platform | End-to-end encrypted, on servers you can run yourself |

Coming from OpenClaw? MindRoom reads OpenClaw workspace files and skills; see [Import from OpenClaw](https://docs.mindroom.chat/openclaw/).

## What Agents Can Do

| Capability | What it means |
|---|---|
| **Teams and routing** | Several agents work together in a thread, and a router picks who answers when nobody is mentioned ([Teams](https://docs.mindroom.chat/configuration/teams/), [Routing](https://docs.mindroom.chat/configuration/router/)) |
| **Memory** | Agents remember people, preferences, and context across conversations ([Memory](https://docs.mindroom.chat/memory/)) |
| **Knowledge bases** | Point an agent at a folder or a Git repository and it searches them ([Knowledge Bases](https://docs.mindroom.chat/knowledge/)) |
| **100+ tools and MCP** | GitHub, Google Workspace, Home Assistant, web search, shell, and any MCP server ([Tools](https://docs.mindroom.chat/tools/), [MCP Servers](https://docs.mindroom.chat/mcp/)) |
| **Skills** | OpenClaw-compatible skills, and agents can learn new ones as they work ([Skills](https://docs.mindroom.chat/skills/)) |
| **Scheduling** | Recurring tasks from cron or plain language, including silent checks that post only when they find something ([Scheduling](https://docs.mindroom.chat/scheduling/)) |
| **Voice, images, and files** | Voice messages, image analysis, and file and video attachments ([Voice Messages](https://docs.mindroom.chat/voice/), [Image Messages](https://docs.mindroom.chat/images/), [File & Video Attachments](https://docs.mindroom.chat/attachments/)) |
| **Plugins and hooks** | Your own tools, skills, and OAuth integrations ([Plugins](https://docs.mindroom.chat/plugins/), [Hooks](https://docs.mindroom.chat/hooks/)) |

## Get Started

1. [Install & First Run](https://docs.mindroom.chat/getting-started/): run `uvx mindroom run` and pair it with your MindRoom Chat account.
2. [Configuration](https://docs.mindroom.chat/configuration/): define agents, models, and teams in `config.yaml` or the [Dashboard](https://docs.mindroom.chat/dashboard/); most changes apply without a restart.
3. [Deployment](https://docs.mindroom.chat/deployment/): run MindRoom with Docker or Kubernetes when you are ready to share it.
