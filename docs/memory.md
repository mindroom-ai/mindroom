---
icon: lucide/brain
---

# Memory System

MindRoom includes a Mem0-inspired memory system that provides persistent memory across conversations. Memory is automatically enabled for all agents and stores information using semantic search with ChromaDB.

## Memory Scopes

### Agent Memory

Each agent maintains its own memory namespace, storing user preferences and past interactions:

```
agent_<name>
├── User preferences
├── Past conversations
└── Learned context
```

Agent memories are stored with `user_id=agent_<name>` and automatically retrieved when processing messages.

### Room Memory

Room-specific knowledge that persists across all agents in that room:

```
room_<safe_room_id>
├── Project context
├── Technical decisions
└── Shared knowledge
```

Room IDs are sanitized (`:` replaced with `_`, `!` removed) for storage compatibility.

### Team Memory

When agents work as a team, memories are stored under a shared namespace:

```
team_<agent1>+<agent2>+...
├── Team conversations
├── Shared findings
└── Collaboration context
```

Team IDs are created from sorted agent names joined with `+`. When searching agent memories, team memories are automatically included for agents that belong to teams.

## Memory Configuration

Configure the memory system at the top level of your `config.yaml`:

```yaml
memory:
  # Embedder configuration for semantic search
  embedder:
    provider: openai  # or "ollama"
    config:
      model: text-embedding-3-small
      host: null  # Optional: host URL for self-hosted models

  # LLM for memory extraction (optional)
  llm:
    provider: openai  # or "ollama", "anthropic"
    config:
      model: gpt-4o-mini
      temperature: 0.1
```

### Configuration Options

| Field | Description |
|-------|-------------|
| `embedder.provider` | Embedding provider: `openai`, `ollama` |
| `embedder.config.model` | Model name for embeddings |
| `embedder.config.host` | Host URL for self-hosted models (Ollama, OpenAI-compatible servers, etc.) |
| `llm.provider` | LLM provider for memory extraction: `openai`, `ollama`, `anthropic` |
| `llm.config.model` | Model name for the LLM |
| `llm.config.temperature` | Temperature for LLM responses (e.g., `0.1`) |
| `llm.config.ollama_base_url` | Host URL for Ollama LLM (when using `ollama` provider) |

### Example with Ollama

```yaml
memory:
  embedder:
    provider: ollama
    config:
      model: nomic-embed-text
      host: http://localhost:11434

  llm:
    provider: ollama
    config:
      model: llama3.2
      ollama_base_url: http://localhost:11434
      temperature: 0.1
```

## How Memory Works

1. **Automatic Storage**: After each conversation, relevant information is automatically extracted and stored in the appropriate memory scope (agent, room, or team)
2. **Semantic Retrieval**: Before responding, agents search for relevant memories using the current message as a query
3. **Context Enhancement**: Retrieved memories are prepended to the agent's context with a note that they may not be relevant
4. **Deduplication**: When merging agent and team memories, duplicate content is filtered out

### Memory Flow

```
User Message
    ↓
┌──────────────────────────────────────────┐
│ Search agent memories (default limit: 3) │
│ + team memories (if agent is in a team)  │
│ Search room memories (default limit: 3)  │
└──────────────────────────────────────────┘
    ↓
Enhanced prompt: [room context] + [agent context] + [original message]
    ↓
Agent response
    ↓
Store conversation in memory (agent/team + room scopes)
```

## Storage Location

Memories are stored in ChromaDB under `<storage_path>/chroma/`:

- Collection name: `mindroom_memories`
- Default path: `mindroom_data/chroma/`

## Mem0 Tool (Optional)

For explicit memory control, agents can use the `mem0` tool which provides functions to:

- `add_memory`: Add memories manually
- `search_memory`: Search memories with custom queries
- `get_all_memories`: Retrieve all stored memories
- `delete_all_memories`: Delete all memories for the agent

Add the tool to an agent:

```yaml
agents:
  assistant:
    tools: [mem0]  # Enables explicit memory commands
```

With this tool enabled, agents can respond to natural language requests like:

- "Remember that I prefer TypeScript over JavaScript"
- "What do you know about this project?"
- "Clear all my memories and start fresh"
