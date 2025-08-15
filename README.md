# mindroom

**Why trust ten AI platforms when you can trust zero? One conversation. Every AI model. Open source. Encrypted. Your data. Your control.**

**A universal interface for AI agents with persistent memory, where every conversation has a home.**

## Vision

mindroom reimagines how we interact with AI. Instead of isolated chat sessions that forget everything, mindroom creates a living ecosystem where AI agents have genuine memory, live in dedicated spaces, and can collaborate intelligently.

Think of it as a **chat-native operating system for AI** - where the chat interface isn't just a UI choice, but the fundamental paradigm for human-AI interaction.

## Core Concepts

### 🧠 **Persistent Memory Everywhere**
- Every agent maintains long-term memory across all conversations
- Every room has its own persistent memory and context
- Memories are ratable - users can mark quality and relevance (planned)
- Tag-based memory sharing allows knowledge clustering across topics (planned)

### 🏠 **Rooms as Contexts**
- Each Matrix room represents a dedicated context (Private Life, Open Source, Research, etc.)
- Rooms maintain their own knowledge base and conversation history
- Agents can be "native" to specific rooms or visit as guests
- Room memory provides domain-specific context automatically

### 🤖 **Multi-Agent Collaboration**
- Multiple specialized agents can work together in a single conversation
- Agents see each other's responses and coordinate intelligently
- Router agent automatically suggests relevant specialists
- Users can invite agents to specific threads even if they're not native to the room
- Thread-specific invitations with optional time limits

### 💬 **Threads as Experiments**
- Each thread maintains its own context and token count
- Branch conversations at any point to explore alternatives
- Tag threads to share memories across related discussions
- Full history preservation with intelligent context management

## Key Features

### Agent System
- **Local agents**: Run on your hardware with full privacy for sensitive data
- **Cloud agents**: More powerful models for complex tasks
- **Specialized agents**: Each with unique tools and expertise
  - @mindroom_calculator - Mathematical computations
  - @mindroom_code - Programming and file operations
  - @mindroom_research - Web research and fact-checking
  - @mindroom_analyst - Data analysis and visualization
  - @mindroom_finance - Market data and financial analysis
  - And many more...

### Memory Architecture

#### Room Memory
- Shared context specific to each room
- Contains all conversations and decisions from that room
- Only accessible to agents native to that room
- Provides automatic domain-specific context

#### Agent Memory
- Personal memory that travels with the agent
- Contains agent-specific learnings and patterns
- Builds expertise over time
- Accessible across all rooms

#### Tag-Based Memory Sharing (planned)
- Tag threads with keywords/topics
- All threads with the same tag share memory
- Creates topic-specific knowledge clusters
- Enables cross-thread learning

### Real-Time Features

#### Progress Widget (planned)
- See all agents currently processing
- Monitor long-running research tasks
- Real-time progress indicators
- Cancel operations mid-flight

#### Scheduled Interactions (planned)
- Agents can run scheduled tasks (daily check-ins, reminders)
- Example: Mindfulness agent asking "What are you grateful for today?"
- Configurable per-room schedules
- Cron-based automation

### External Integrations

MindRoom connects with your favorite services through two modes:

#### Simple Mode (No Setup Required)
- Instant access to Amazon, Reddit, GitHub, weather, news, and more
- No API keys or authentication needed
- Perfect for quick searches and public information
- Available through the widget's "✨ Simple Mode" tab

#### Full Integrations (OAuth/API)
- **Google Services**: Gmail, Calendar, and Drive access
- **Spotify**: Control playback, access playlists
- **GitHub**: Access private repos, manage issues
- **Reddit, Facebook, Dropbox**: Full API access
- **Amazon, IMDb, Walmart**: Real-time data with API keys
- One-click OAuth connections through the widget

See [docs/gmail_setup.md](docs/gmail_setup.md) for Google Services setup guide.

### Advanced Capabilities

#### Thread Management
- **Branching**: Fork conversations to explore alternatives (planned)
- **Linking**: Connect related threads for context expansion (planned)
- **Editing**: Modify AI responses for better context (planned)
- **Context Control**: Fine-grained memory permissions (planned)

#### Commands
- `!invite <agent>` - Invite an agent to a thread (only works in threads)
- `!uninvite <agent>` - Remove an agent from a thread
- `!list_invites` - List all invited agents in current thread
- `!widget [url]` - Add MindRoom configuration widget to the room
- `!help [topic]` - Get help on available commands
- `!link [thread-id]` - Link another thread's context (planned)
- `!agents` - List available agents (planned)
- `!context` - Show token usage (planned)
- `!tag [name]` - Tag thread for memory sharing (planned)
- `!branch` - Fork the conversation (planned)
- `!schedule` - Manage agent automation (planned)
- And many more...

## Quick Start

### Installation

#### Prerequisites
- Python 3.11+
- [uv](https://github.com/astral-sh/uv) for Python package management
- [pnpm](https://pnpm.io/) for Node.js package management (if using the widget)
- [Zellij](https://zellij.dev/) terminal multiplexer (optional, for helper scripts)
- Node.js 20+ (if using the widget)

#### Install MindRoom
```bash
git clone https://github.com/yourusername/mindroom
cd mindroom
uv sync --all-extras
source .venv/bin/activate

# If using the widget, also install frontend dependencies
cd widget/frontend
pnpm install
cd ../..

### Configuration

1. **Configure your agents and models** in `config.yaml` (already included with defaults)

2. **Create a `.env` file** (optional - for API keys):

```env
# Matrix configuration (optional - defaults to localhost:8008)
MATRIX_HOMESERVER=http://localhost:8008

# Optional API keys (if using OpenAI/Anthropic models)
OPENAI_API_KEY=your-key-here
ANTHROPIC_API_KEY=your-key-here
OLLAMA_HOST=http://localhost:11434  # for local models
```

### Running Mindroom

#### Option 1: Simple Command
```bash
mindroom run
```

#### Option 2: With Widget Interface (Recommended)
```bash
# Start both MindRoom and the configuration widget in a single terminal session
scripts/start

# To stop everything
scripts/stop
```

The helper script (`scripts/start`) runs both MindRoom and the widget in a [Zellij](https://zellij.dev/) terminal multiplexer session, giving you:
- MindRoom agents running in one pane
- Configuration widget (backend + frontend) in another pane
- Easy session management with attach/detach capabilities

Both methods automatically do:
- ✅ Creates your Matrix user account
- ✅ Creates accounts for all agents in `config.yaml`
- ✅ Creates all rooms defined in `config.yaml`
- ✅ Invites agents to their configured rooms
- ✅ Starts the multi-agent system
- ✅ Provides visual configuration interface at http://localhost:3003

### Basic Usage

In your Matrix client (Element, etc.):
- **Direct mention**: `@mindroom_calculator What is 15% of 200?`
- **Multiple agents**: `@mindroom_research @mindroom_analyst What are the latest AI trends?`
- **In threads**: Agents follow smart response rules (see below)

### Agent Response Rules

Agents ONLY respond in threads - never in main room messages. Within threads, they follow these intelligent rules:

1. **Mentioned agents always respond** - If you @mention an agent in a thread, it will respond
2. **Single agent continues conversation** - If only one agent is in a thread, it continues responding without mentions
3. **Multiple agents collaborate** - When 2+ agents are in a thread, they form a team and provide coordinated responses
4. **Smart routing for new threads** - If no agents have participated in the thread, the system picks the most suitable one(s)
5. **Invited agents act like natives** - Agents invited via `!invite` follow the same rules as room natives

#### Team Collaboration Modes

When multiple agents work together, they can operate in different modes:

- **Coordinate Mode**: Team leader delegates different subtasks to members (sequential or parallel as needed)
- **Collaborate Mode**: All agents work on the SAME task simultaneously, providing diverse perspectives
- **Route Mode**: A lead agent delegates to the most appropriate specialist

These rules ensure:
- No agent response storms (agents coordinate instead of competing)
- Natural conversations (single agent threads flow smoothly)
- Richer responses (multiple perspectives when multiple agents are present)
- Intelligent routing (best agent or team selected for new questions)

## Usage Examples

### Multi-Agent Collaboration

#### Explicit Team Formation (Multiple Agents Tagged)
```
You: @mindroom_research:localhost @mindroom_analyst:localhost What are the latest trends in renewable energy?
[Team forms with Research and Analyst agents]
ResearchAgent: I'll gather recent data on renewable energy trends...
AnalystAgent: Based on the research, here's my analysis of the key patterns...
Team Response: Combining our findings, here are the three major trends in renewable energy...
```

#### Automatic Team Formation (Multiple Agents in Thread)
```
[Thread already has Code and Security agents participating]
You: How should we implement user authentication?
[Code and Security agents automatically form a team]
CodeAgent: From an implementation perspective, I recommend using JWT tokens with...
SecurityAgent: Adding to that, we need to ensure proper encryption and...
Team Response: Here's our unified recommendation for secure authentication...
```

### Memory Persistence
```
You: @mindroom_general:localhost Remember that my project deadline is next Friday
GeneralAgent: I've noted your project deadline for next Friday.
[Days later in a different conversation]
You: @mindroom_general:localhost What do you know about my schedule?
GeneralAgent: You have a project deadline this Friday (in 2 days).
```

### Room-Based Context
```
[In "dev" room]
You: @mindroom_code:localhost How should we structure the authentication module?
CodeAgent: Based on our previous discussions in this room about the FastAPI backend...
```

## CLI Commands

```bash
# Show help (also works with just 'mindroom' or 'mindroom -h')
mindroom --help

# Run the multi-agent system (auto-setup everything)
mindroom run

# Show current status (agents, rooms, etc.)
mindroom info

# Create a new room manually
mindroom create-room testing --room-name "Testing Room"

# Invite agents to an existing room
mindroom invite-agents !room_id:localhost
```

## Architecture

### Matrix Foundation
- Built on Matrix protocol for decentralized, secure communication
- Uses matrix-nio for Python integration
- Leverages existing Matrix clients (Element, etc.)
- No custom UI needed - works with your favorite Matrix client

### Agent Framework
- Powered by the Agno library for AI integration
- Native SDK approach for optimal performance
- Supports multiple LLM providers (OpenAI, Anthropic, Ollama, etc.)
- Modular agent system with specialized toolkits

### Storage Philosophy
- **Chat as Database**: All data lives in the conversation
- No separate database infrastructure required
- Matrix handles replication and federation
- Natural audit trail and version history

## Development

### Adding New Agents

Create a new agent in `agents/`:

```python
from agents.base import create_agent
from agno.tools import YourToolkit

def create_your_agent(model):
    return create_agent(
        agent_name="your_agent",
        display_name="YourAgent",
        role="Your agent's purpose",
        model=model,
        tools=[YourToolkit()],
        instructions=[
            "Specific instruction 1",
            "Specific instruction 2",
        ],
    )
```

### Testing

```bash
pytest
pre-commit run --all-files
```

## Current Status

### ✅ Implemented Features
- Multi-agent system with specialized agents (calculator, code, research, etc.)
- Basic memory system with room and agent contexts
- Thread-based conversations
- Cross-room agent invitations with time limits
- Agent response routing based on context
- Commands: `!invite`, `!uninvite`, `!list_invites`, `!widget`, `!help`
- Multiple AI model support (OpenAI, Anthropic, Ollama, etc.)
- End-to-end encryption via Matrix
- Team-based agent collaboration (using Agno Teams)

### 🚧 In Development
- Memory rating and quality feedback
- Thread tagging and memory sharing
- Progress widget for real-time monitoring
- Scheduled agent interactions
- Thread branching and linking
- Context management commands

## Roadmap

### Near Term
- [ ] Widget API for real-time progress monitoring
- [ ] Advanced scheduling system with YAML configuration
- [ ] Voice interaction (STT/TTS) support
- [ ] Enhanced memory rating and feedback system

### Medium Term
- [ ] Federation support for cross-server agent sharing
- [ ] Plugin system for custom agent development
- [ ] Advanced memory visualization tools
- [ ] Mobile-optimized experience

### Long Term
- [ ] Agent marketplace for sharing specialized agents
- [ ] Advanced agent training from conversation feedback
- [ ] Multi-modal agents (image, video understanding)
- [ ] Distributed agent execution across devices

## Philosophy

mindroom is built on the belief that AI should be:

1. **Persistent**: Your AI agents should remember and learn from every interaction
2. **Contextual**: Different aspects of your life deserve different AI contexts
3. **Collaborative**: Multiple specialized agents working together are more powerful than one generalist
4. **Private**: You should control where your data lives and which agents can access it
5. **Natural**: Chat is the native interface for human-AI interaction

## Contributing

We welcome contributions! See [CONTRIBUTING.md](CONTRIBUTING.md) for guidelines.

## License

MIT License - see [LICENSE](LICENSE) for details.

## Acknowledgments

Built with:
- [Matrix](https://matrix.org/) - Decentralized communication protocol
- [Agno](https://agno.dev/) - AI agent framework
- [matrix-nio](https://github.com/poljar/matrix-nio) - Python Matrix client library

---

**mindroom** - Where AI agents come to life, remember, and collaborate.
