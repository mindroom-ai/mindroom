---
icon: lucide/brain
---

# Memory System

MindRoom includes a Mem0-inspired dual memory system that provides persistent memory across conversations.

## Memory Scopes

### Agent Memory

Each agent maintains its own memory of user preferences, coding style, and past interactions:

```
agent_<name>
├── User preferences
├── Coding style
├── Past tasks
└── Learned patterns
```

### Room Memory

Room-specific knowledge that persists across all agents:

```
room_<id>
├── Project context
├── Technical decisions
├── Important information
└── Shared knowledge
```

### Team Memory

Shared context for team collaboration:

```
team_<name>
├── Team decisions
├── Shared findings
└── Collaboration notes
```

## Enabling Memory

Memory is enabled per-agent:

```yaml
agents:
  assistant:
    display_name: Assistant
    role: A helpful assistant
    model: sonnet
    memory: true  # Enable memory
```

## Memory Configuration

Configure the memory system in your config:

```yaml
memory:
  # Embedding model for semantic search
  embedder:
    provider: openai
    model: text-embedding-3-small

  # Vector store backend
  vector_store:
    provider: chroma
    path: mindroom_data/memory

  # Memory options
  options:
    top_k: 5  # Number of memories to retrieve
```

## How Memory Works

1. **Storage**: When important information is shared, agents store it in the appropriate memory scope
2. **Retrieval**: Before responding, agents retrieve relevant memories using semantic search
3. **Context**: Retrieved memories are included in the agent's context for more personalized responses

## Memory Commands

Agents can interact with memory through natural language:

- "Remember that I prefer TypeScript over JavaScript"
- "What do you know about this project?"
- "Forget the database credentials I mentioned earlier"
