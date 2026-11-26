---
icon: lucide/archive
---

# Memory & Storage

The `mem0` and `zep` tools connect an agent directly to the external Mem0 and Zep Cloud memory services.
They are separate from MindRoom's own memory: enabling them does not change `memory.backend`, automatic memory extraction, or the `memory` tool.
To make MindRoom's built-in memory use Mem0, see [Memory System](../memory.md); to let an agent explicitly add, search, or delete MindRoom memories, see the [`memory` tool](../memory.md#memory).
For conversation file attachments, see [Attachments](../attachments.md).

API keys are `password` fields, so set them in the dashboard or through the environment variables below, never inline in `config.yaml`; see [Security Restrictions](index.md#security-restrictions).

## [`mem0`]

`mem0` gives the agent `mem0_read()` (`search`, `get_all`), `mem0_add()`, and `mem0_delete_all()` backed by the `mem0ai` client.
With an API key, it uses the hosted Mem0 Platform; without one, it uses Mem0's default setup, which stores memories locally but calls OpenAI for embeddings and memory extraction.
That default setup requires `OPENAI_API_KEY` in the process environment, not the `.env`, even when the agent itself uses another provider; without it, the `mem0` tool fails to load.
Local memories live outside MindRoom's storage directory, in `/tmp/qdrant` with history in `history.db` in Mem0's directory (`~/.mem0/`, or `MEM0_DIR` when set), so persist and back up those paths separately or the memories can be lost on reboot or container recreation.
A custom local Mem0 config object cannot be set through MindRoom.

Memories are stored per user.
Set `user_id` to share one memory store across all users; without it, each call uses the Matrix user who sent the request, and calls without a resolvable user return an error.

| Option | Type | Default | Notes |
| --- | --- | --- | --- |
| `api_key` | `password` | `null` | Mem0 Platform API key; also read from `MEM0_API_KEY`. |
| `user_id` | `text` | `null` | Fixed user scope for every call. |
| `org_id` | `text` | `null` | Mem0 Platform organization ID; also read from `MEM0_ORG_ID`. |
| `project_id` | `text` | `null` | Mem0 Platform project ID; also read from `MEM0_PROJECT_ID`. |
| `infer` | `boolean` | `true` | Let Mem0 infer facts from added content. |
| `enable_add_memory` | `boolean` | `true` | Enable `mem0_add()`. |
| `enable_search_memory` | `boolean` | `true` | Enable `mem0_read(action="search")`. |
| `enable_get_all_memories` | `boolean` | `true` | Enable `mem0_read(action="get_all")`. |
| `enable_delete_all_memories` | `boolean` | `true` | Enable `mem0_delete_all()`. |
| `all` | `boolean` | `false` | Enable every function regardless of the individual flags. |

```yaml
agents:
  assistant:
    tools:
      - mem0:
          user_id: assistant-memory
          enable_delete_all_memories: false
```

## [`zep`]

`zep` connects the agent to Zep Cloud for conversation memory and user-graph search.
It requires a Zep API key from `api_key` or `ZEP_API_KEY`; without one, the agent runs without the `zep` tool and logs `Could not load tool for agent construction`.

- `zep_add_message(role, content)` adds a message to the Zep thread.
- `zep_read(action="context" | "messages")` returns the thread's context summary or raw message history.
- `zep_read(action="search", query=..., search_scope="edges" | "nodes")` searches the user graph for facts or node summaries.

Set `user_id` and `session_id` to keep using the same Zep user and thread across conversations.
Without them, the tool generates a new Zep user and thread, so it does not find earlier memory.
A `user_id` that does not exist yet in Zep is created automatically.
Custom instructions you set with `instructions` are always included in the agent's prompt.

| Option | Type | Default | Notes |
| --- | --- | --- | --- |
| `session_id` | `text` | `null` | Stable Zep thread ID; a new one is generated when unset. |
| `user_id` | `text` | `null` | Stable Zep user ID; a new user is generated and created when unset. |
| `api_key` | `password` | `null` | Zep API key; also read from `ZEP_API_KEY`. |
| `ignore_assistant_messages` | `boolean` | `false` | Exclude assistant-role messages from graph-memory extraction; they are still stored in the Zep thread. |
| `enable_add_zep_message` | `boolean` | `true` | Enable `zep_add_message()`. |
| `enable_get_zep_memory` | `boolean` | `true` | Enable `zep_read(action="context" | "messages")`. |
| `enable_search_zep_memory` | `boolean` | `true` | Enable `zep_read(action="search")`. |
| `instructions` | `text` | `null` | Additional Zep usage instructions. |
| `all` | `boolean` | `false` | Enable every function regardless of the individual flags. |

```yaml
agents:
  assistant:
    tools:
      - zep:
          user_id: assistant-user
          session_id: release-review
```
