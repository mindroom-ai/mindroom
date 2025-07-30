# Matrix AI Examples

This directory contains example scripts demonstrating how to use the Matrix AI bot and its agent system.

## Examples

### 1. `demo.py` - Agent System Demo
A standalone demo showing how to use different agents programmatically:
- Lists available agents
- Demonstrates calculator agent for math
- Shows code generation with code agent
- Text summarization with summary agent
- General conversation with general agent

Run with:
```bash
python examples/demo.py
```

### 2. `matrix_bot_with_agents.py` - Full Matrix Bot
Shows how to run the complete Matrix bot with all agents:
- Explains available agents and their capabilities
- Shows usage examples for different scenarios
- Demonstrates thread conversations
- Includes configuration instructions

Run with:
```bash
python examples/matrix_bot_with_agents.py
```

## Configuration

Before running the examples, create a `.env` file with:

```env
# Matrix configuration
MATRIX_HOMESERVER=https://your-homeserver.com
MATRIX_USER_ID=@your-bot:your-homeserver.com
MATRIX_PASSWORD=your-bot-password

# AI model configuration
AGNO_MODEL=openai:gpt-4  # or anthropic:claude-3-opus, ollama:llama3.2, etc.

# Optional: API keys for specific providers
OPENAI_API_KEY=your-key-here
ANTHROPIC_API_KEY=your-key-here
```

## Available Agents

### Core Agents (No Extra Dependencies)
- **@general** - General conversation and assistance
- **@calculator** - Mathematical calculations
- **@code** - Code generation and file operations
- **@shell** - Shell command execution
- **@summary** - Text summarization

### Optional Agents (Require Additional Dependencies)
- **@research** - Web research with DuckDuckGo, Wikipedia, Arxiv
- **@finance** - Stock market data and financial calculations
- **@news** - Current events and news summaries
- **@analyst** - Data analysis with CSV/JSON files

To use optional agents, install their dependencies:
```bash
pip install agno[ddg,arxiv,wikipedia]  # For research agent
pip install agno[yfinance]  # For finance agent
pip install agno[newspaper]  # For news agent
```

## Usage in Matrix

1. **Direct mention**: `@bot: How can you help me?`
2. **Agent-specific**: `@calculator: What is 15% of 200?`
3. **In threads**: All messages in a thread are treated as mentions
4. **Multi-agent threads**: Use different agents in the same conversation

## Tips

- Agents maintain separate memory/context
- Thread conversations preserve full history
- Use specific agents for better results
- Combine agents for complex tasks
