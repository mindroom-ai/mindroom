# mindroom

**Your AI is trapped in apps. We set it free.**

AI agents that learn who you are shouldn't forget everything when you switch apps. MindRoom agents follow you everywhere—Slack, Telegram, Discord, WhatsApp—with persistent memory intact.

Deploy once on Matrix. Your agents now work in any chat platform via bridges. They can even visit your client's workspace or join your friend's group chat.

Self-host for complete control or use our encrypted service. Either way, your agents remember you and can collaborate across organizations.

## The Problem

Every AI app is a prison:
- ChatGPT knows your coding style... but can't join your team's Slack
- Claude understands your writing... but can't access your email
- GitHub Copilot helps with code... but can't see your project specs
- You teach each AI from scratch, over and over

Meanwhile, your human team collaborates across Slack, Discord, Telegram, and email daily. Why can't your AI?

## The Solution

MindRoom agents:
- **Live in Matrix** - A federated protocol like email
- **Work everywhere** - Via bridges to Slack, Telegram, Discord, WhatsApp, IRC, email
- **Remember everything** - Persistent memory across all platforms
- **Collaborate naturally** - Multiple agents working together in threads
- **Respect boundaries** - You control which agent sees what data

## Built on Proven Infrastructure

MindRoom leverages the Matrix protocol, a decade-old open standard with significant real-world adoption:

**Foundation**
- **10+ years** of development by the Matrix.org Foundation
- **€10M+** invested in protocol development
- **100+ developers** contributing to the core ecosystem
- **35+ million users** globally

**Enterprise Validation**
- **German Healthcare**: 150,000+ organizations using Ti-Messenger
- **French Government**: 5.5 million civil servants on Tchap
- **Military Adoption**: NATO, U.S. Space Force, and other defense organizations
- **GDPR Compliant**: Built for European privacy standards

**What This Means For You**

By building on Matrix, MindRoom inherits:
- Production-tested federation across organizations
- Military-grade E2E encryption (Olm/Megolm)
- Professional clients (Element, FluffyChat, Cinny)
- 50+ maintained bridges to other platforms
- Proven scale and reliability

This foundation allows MindRoom to focus entirely on agent orchestration and intelligence, rather than reimplementing communication infrastructure.

## See It In Action

```
Monday, in your Matrix room:
You: @assistant Remember our project uses Python 3.11 and FastAPI

Tuesday, in your team's Slack (via bridge):
Colleague: What Python version are we using?
You: @assistant can you help?
Assistant: [Joins from Matrix] We're using Python 3.11 with FastAPI

Wednesday, in client's Telegram (via bridge):
Client: Can your AI review our API spec?
You: @assistant please analyze this
Assistant: [Travels from your server] I'll review this against our FastAPI patterns...
```

One agent. Every platform. Continuous memory.

## The Magic Moment - Cross-Organization Collaboration

```
Thursday, your client asks in their Discord:
Client: Can our architect AI review this with your team?
You: Sure! @assistant please collaborate with them

Your Assistant: [Joins from your Matrix server]
Client's Architect AI: [Joins from their server]
Together: [They review architecture, sharing context from both organizations]
```

**Two AI agents from different companies collaborating.**
This is impossible with ChatGPT, Claude, or any other platform.

## But It Gets Better - Your Agents Work as a Team

```
Friday, planning next sprint:
You: @research @analyst @writer Create a competitive analysis report
Research: I'll gather data on our top 5 competitors...
Analyst: I'll identify strategic patterns and opportunities...
Writer: I'll compile everything into an executive summary...
[They work together, transparently, delivering a comprehensive report]
```

## Key Features

### 🧠 Dual Memory System
- **Agent Memory**: Each agent remembers conversations, preferences, and patterns across all platforms
- **Room Memory**: Contextual knowledge that stays within specific rooms (work projects, personal notes)
- Tags for sharing memories across related threads

### 🤝 Multi-Agent Collaboration
```
You: @research @analyst @email Create weekly competitor analysis reports
Research: I'll gather competitor updates
Analyst: I'll identify strategic patterns
Email: I'll compile and send every Friday
[They work together, automatically, every week]
```

### 💬 Direct Messages (DMs)
- Agents respond naturally in 1:1 DMs without needing mentions
- Add more agents to existing DM rooms for collaborative private work
- Complete privacy separate from configured public rooms

### 🔐 Intelligent Trust Boundaries
- Route sensitive data to local Ollama models on your hardware
- Use GPT-5 for complex reasoning
- Send general queries to cost-effective cloud models
- You decide which AI sees what

### 🔌 80+ Integrations
Gmail, GitHub, Spotify, Home Assistant, Google Drive, Reddit, weather services, news APIs, financial data, and many more. Your agents can interact with all your tools.

### 📅 Automation & Scheduling
- Daily check-ins from your mindfulness agent
- Scheduled reports and summaries
- Event-based triggers (coming soon)
- Background tasks with human escalation

## Who This Is For

- **Teams using Matrix/Element** - Add AI to your existing secure infrastructure without migration
- **Open Source Projects** - Agents that remember all decisions and can visit contributor chats
- **Consultants & Agencies** - Your AI can securely join client workspaces
- **Privacy-Focused Organizations** - Self-host everything, own your data completely
- **Developers** - Build on our platform, contribute agents, extend functionality

## Quick Start

### Prerequisites
- Python 3.12+
- [uv](https://github.com/astral-sh/uv) for Python package management
- Node.js 20+ and [bun](https://bun.sh/) (optional, for web UI)

### Installation and starting

```bash
# Clone and install
git clone https://github.com/mindroom-ai/mindroom
cd mindroom
uv sync --all-extras
```

```bash
# Terminal 1: Start backend (agents + API)
./run-backend.sh

# Terminal 2: Start frontend (optional, for web UI)
./run-frontend.sh
```

The web interface will be available at http://localhost:3003

### First Steps

In any Matrix client (Element, FluffyChat, etc):
```
You: @mindroom_assistant What can you do?
Assistant: I can coordinate our team of specialized agents...

You: @mindroom_research @mindroom_analyst What are the latest AI breakthroughs?
[Agents collaborate to research and analyze]
```

## How Agents Work

### Agent Response Rules
Agents ONLY respond in threads (not main room). Within threads:

1. **Mentioned agents always respond** - Tag them to get their attention
2. **Single agent continues** - One agent in thread? It keeps responding
3. **Multiple agents collaborate** - They work together, not compete
4. **Smart routing** - System picks the best agent for new threads

### Available Commands

<!-- CODE:START -->
<!-- import sys -->
<!-- sys.path.insert(0, 'src') -->
<!-- from mindroom.commands import _get_command_entries -->
<!-- for entry in _get_command_entries(format_code=True): -->
<!--     print(entry) -->
<!-- CODE:END -->
<!-- OUTPUT:START -->
<!-- ⚠️ This content is auto-generated by `markdown-code-runner`. -->
- `!help [topic]` - Get help
- `!schedule <task>` - Schedule a task
- `!list_schedules` - List scheduled tasks
- `!cancel_schedule <id>` - Cancel a scheduled task
- `!widget [url]` - Add configuration widget
- `!config <operation>` - Manage configuration
- `!hi` - Show welcome message

<!-- OUTPUT:END -->

## Note for Self-Hosters

This repository contains everything you need to self-host MindRoom. The `saas-platform/` directory contains infrastructure and code specific to running MindRoom as a hosted service and can be safely ignored by self-hosters.

## Configuration

### Basic Setup

1. Copy the example configuration:
```bash
cp config.example.yaml config.yaml
```

2. Edit `config.yaml` to add your API keys (optional):
```yaml
models:
  - provider: openai
    api_key: your-key-here  # Or use environment variable
  - provider: anthropic
    api_key: your-key-here
  - provider: ollama
    host: http://localhost:11434  # For local models
```

3. Configure your Matrix homeserver (optional, defaults to localhost:8008):
```bash
export MATRIX_HOMESERVER=https://your-matrix.server
```

## Deployment Options

### 🏠 Self-Hosted
Complete control on your infrastructure:
```bash
# Using your existing Matrix server
MATRIX_HOMESERVER=https://your-matrix.server uv run mindroom run

# Or let MindRoom handle everything locally
uv run mindroom run
```

### ☁️ Our Hosted Service (Coming Soon)
Zero setup, enterprise security:
- End-to-end encrypted (we can't read your data)
- Automatic updates and scaling
- 99.9% uptime SLA
- Start free, scale as needed

### 🔀 Hybrid
Mix and match:
- Sensitive rooms on your server
- General rooms on our cloud
- Agents collaborate seamlessly across both

## Architecture

### Technical Stack
- **Matrix**: Conduit server (only 20-30MB RAM!)
- **Agents**: Python with matrix-nio
- **AI Models**: OpenAI, Anthropic, Ollama, or any provider
- **Memory**: Conversation-based (no separate database)
- **UI**: Web widget + any Matrix client

## Philosophy

We believe AI should be:

1. **Persistent**: Your AI should remember and learn from every interaction
2. **Ubiquitous**: Available wherever you communicate
3. **Collaborative**: Multiple specialists working together
4. **Private**: You control where your data lives
5. **Natural**: Just chat—no complex interfaces

## Status

- ✅ **Production ready** with 1000+ commits
- ✅ **80+ integrations** working today
- ✅ **Multi-agent collaboration** with persistent memory
- ✅ **Federation** across organizations and platforms
- ✅ **Self-hosted & cloud** options available
- 🚧 Voice interaction (STT/TTS) coming soon
- 🚧 Mobile apps in development
- 🚧 Agent marketplace planned


## Contributing

We welcome contributions! See [CONTRIBUTING.md](CONTRIBUTING.md) for guidelines.

From the developer of 10+ successful open source projects with thousands of users. MindRoom represents 1000+ commits of production-ready code, not a weekend experiment.

## License

MIT License - see [LICENSE](LICENSE) for details.

## Acknowledgments

Built with:
- [Matrix](https://matrix.org/) - The federated communication protocol
- [Agno](https://agno.dev/) - AI agent framework
- [matrix-nio](https://github.com/poljar/matrix-nio) - Python Matrix client

---

**mindroom** - AI that follows you everywhere, remembers everything, and stays under your control.
