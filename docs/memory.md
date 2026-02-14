---
icon: lucide/brain
---

# Memory System

MindRoom uses Mem0 for persistent memory across conversations. Memory is automatically enabled for all agents and uses ChromaDB for semantic search.

## Memory Scopes

| Scope | User ID Format | Description |
|-------|----------------|-------------|
| Agent | `agent_<name>` | User preferences, past conversations, learned context |
| Room | `room_<safe_room_id>` | Project context, technical decisions, shared knowledge |
| Team | `team_<agent1>+<agent2>+...` | Shared team conversations and findings |

**Notes:**
- Room IDs are sanitized (`:` → `_`, `!` removed) for storage compatibility
- Team IDs use sorted agent names joined with `+`
- When searching agent memories, team memories are automatically included and deduplicated

## Configuration

Configure memory in your `config.yaml`:

```yaml
memory:
  embedder:
    provider: openai  # or "ollama"
    config:
      model: text-embedding-3-small
      host: null  # Optional: for self-hosted models

  llm:  # Optional: for memory extraction
    provider: openai  # or "ollama", "anthropic"
    config:
      model: gpt-5-mini
      temperature: 0.1
      host: null  # For Ollama
```

The `host` field is converted internally to `openai_base_url` or `ollama_base_url` depending on the provider.

## How Memory Works

1. **Semantic Retrieval**: Before responding, agents search for relevant memories (limit: 3 per scope)
2. **Context Enhancement**: Retrieved memories are prepended to the prompt with a note that they may not be relevant
3. **Automatic Storage**: After each conversation, Mem0 extracts and stores relevant information

## Storage

Memories are stored in ChromaDB at `<storage_path>/chroma/` with collection name `mindroom_memories`.

## Mem0 Tool (Optional)

For explicit memory control, add the `mem0` tool to an agent:

```yaml
agents:
  assistant:
    tools: [mem0]
```

This provides functions: `add_memory`, `search_memory`, `get_all_memories`, `delete_all_memories`.
