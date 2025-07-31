# mindroom

**A universal interface for AI agents with persistent memory, where every conversation has a home.**

## Vision

mindroom reimagines how we interact with AI. Instead of isolated chat sessions that forget everything, mindroom creates a living ecosystem where AI agents have genuine memory, live in dedicated spaces, and can collaborate intelligently.

Think of it as a **chat-native operating system for AI** - where the chat interface isn't just a UI choice, but the fundamental paradigm for human-AI interaction.

## Core Concepts

### 🧠 **Persistent Memory Everywhere**
- Every agent maintains long-term memory across all conversations
- Every room has its own persistent memory and context
- Memories are ratable - users can mark quality and relevance
- Tag-based memory sharing allows knowledge clustering across topics

### 🏠 **Rooms as Contexts**
- Each Matrix room represents a dedicated context (Private Life, Open Source, Research, etc.)
- Rooms maintain their own knowledge base and conversation history
- Agents can be "native" to specific rooms or visit as guests
- Room memory provides domain-specific context automatically

### 🤖 **Multi-Agent Collaboration**
- Multiple specialized agents can work together in a single conversation
- Agents see each other's responses and coordinate intelligently
- Router agent automatically suggests relevant specialists
- Agents can invite other agents if they need help

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

#### Tag-Based Memory Sharing
- Tag threads with keywords/topics
- All threads with the same tag share memory
- Creates topic-specific knowledge clusters
- Enables cross-thread learning

### Real-Time Features

#### Progress Widget
- See all agents currently processing
- Monitor long-running research tasks
- Real-time progress indicators
- Cancel operations mid-flight

#### Scheduled Interactions
- Agents can run scheduled tasks (daily check-ins, reminders)
- Example: Mindfulness agent asking "What are you grateful for today?"
- Configurable per-room schedules
- Cron-based automation

### Advanced Capabilities

#### Thread Management
- **Branching**: Fork conversations to explore alternatives
- **Linking**: Connect related threads for context expansion
- **Editing**: Modify AI responses for better context
- **Context Control**: Fine-grained memory permissions

#### Slash Commands
- `/link [thread-id]` - Link another thread's context
- `/agents` - List available agents
- `/context` - Show token usage
- `/tag [name]` - Tag thread for memory sharing
- `/branch` - Fork the conversation
- `/schedule` - Manage agent automation
- And many more...

## Quick Start

### Installation

```bash
git clone https://github.com/yourusername/mindroom
cd mindroom
uv sync --all-extras
source .venv/bin/activate
```

### Configuration

1. **Configure your agents** in `agents.yaml` (already included with defaults)

2. **Create a `.env` file** (optional - for custom AI providers):

```env
# Matrix configuration (optional - defaults to localhost:8008)
MATRIX_HOMESERVER=http://localhost:8008

# AI configuration
AGNO_MODEL=openai:gpt-4  # or anthropic:claude-3-opus, ollama:llama3.2, etc.

# Optional API keys
OPENAI_API_KEY=your-key-here
ANTHROPIC_API_KEY=your-key-here
OLLAMA_HOST=http://localhost:11434  # for local models
```

### Running Mindroom

Just one command:

```bash
mindroom run
```

This automatically:
- ✅ Creates your Matrix user account
- ✅ Creates accounts for all agents in `agents.yaml`
- ✅ Creates all rooms defined in `agents.yaml`
- ✅ Invites agents to their configured rooms
- ✅ Starts the multi-agent system

### Basic Usage

In your Matrix client (Element, etc.):
- **Direct mention**: `@mindroom_calculator What is 15% of 200?`
- **Multiple agents**: `@mindroom_research @mindroom_analyst What are the latest AI trends?`
- **In threads**: Agents respond to all messages automatically

## Usage Examples

### Multi-Agent Collaboration
```
You: @mindroom_research:localhost @mindroom_analyst:localhost What are the latest trends in renewable energy?
ResearchAgent: I'll gather recent data on renewable energy trends...
AnalystAgent: Based on the research, here's my analysis of the key patterns...
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
