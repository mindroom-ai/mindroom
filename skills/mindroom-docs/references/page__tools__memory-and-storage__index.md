# Memory & Storage

Use these tools to explicitly manage MindRoom memory or connect an agent to external memory providers.

## What This Page Covers

This page documents the built-in tools in the `memory-and-storage` group. Use these tools when you need explicit memory CRUD operations, direct provider-specific memory access, or a clear separation between MindRoom memory and third-party memory services.

## Tools On This Page

- \[`memory`\] - Explicitly add, search, list, read, update, and delete MindRoom memories visible to the current agent.
- \[`mem0`\] - Direct Mem0 toolkit for user-scoped persistent memory outside MindRoom's built-in memory API.
- \[`zep`\] - Direct Zep Cloud toolkit for session memory and user-graph search.

## Common Setup Notes

`memory` is MindRoom-native and has no tool-specific configuration fields. It operates on the same MindRoom memory backend configured through `memory.backend` or `agents.<name>.memory_backend`, so it follows the built-in `mem0` or `file` backend for that agent. Use [Memory System](https://docs.mindroom.chat/memory/index.md) for the canonical docs on backend selection, automatic extraction, file-backed memory, Agno Learning, and storage layout. `mem0` and `zep` are separate upstream Agno toolkits that talk to external memory providers directly. Enabling `mem0` or `zep` does not change MindRoom's own memory backend, automatic memory extraction, or the behavior of the `memory` tool. `mem0` can work with a hosted Mem0 API key or with local/default upstream Mem0 configuration. `zep` requires a Zep API key, either through stored credentials or the `ZEP_API_KEY` environment variable. If optional dependencies for these tools are missing, MindRoom can auto-install them at first use unless `MINDROOM_NO_AUTO_INSTALL_TOOLS=1` is set. This page does not document conversation-scoped file attachments even though they are storage-like. Use [Matrix & Attachments](https://docs.mindroom.chat/tools/matrix-and-attachments/index.md) and [Attachments](https://docs.mindroom.chat/attachments/index.md) for attachment IDs, retention, and Matrix media flow.

## \[`memory`\]

`memory` gives an agent explicit control over the MindRoom memories available in its current scope.

### What It Does

`memory` exposes `add_memory()`, `search_memories()`, `list_memories()`, `get_memory()`, `update_memory()`, and `delete_memory()`. It complements MindRoom's automatic post-response memory extraction by letting the agent deliberately remember or inspect something on demand. The tool always uses the current agent's configured MindRoom memory backend, so the same calls work whether that agent uses built-in `mem0` storage or file-backed memory. Search and list results include memory IDs, and those IDs are then used with `get_memory()`, `update_memory()`, and `delete_memory()`. The tool is bound to the current agent's MindRoom scope and can reach any agent or team memories that MindRoom makes visible to that agent.

### Configuration

This tool has no tool-specific inline configuration fields.

### Example

```
memory:
  backend: file

agents:
  assistant:
    tools:
      - memory
```

```
add_memory("The user prefers terse release notes.")
search_memories("release notes", limit=3)
list_memories(limit=20)
get_memory("abc123")
update_memory("abc123", "The user prefers terse release notes with dates.")
delete_memory("abc123")
```

### Notes

- The tool uses whichever MindRoom backend is active for the agent, so enable and tune that backend through [Memory System](https://docs.mindroom.chat/memory/index.md), not through tool-local options.
- This is the right tool when you want explicit control over MindRoom's built-in durable memory rather than a separate provider account.
- The tool returns user-facing error strings on failures instead of raising raw exceptions into the conversation.

## \[`mem0`\]

`mem0` connects an agent directly to the upstream Mem0 toolkit.

### What It Does

`mem0` exposes `add_memory()`, `search_memory()`, `get_all_memories()`, and `delete_all_memories()`. It uses the upstream `mem0ai` client directly rather than MindRoom's built-in memory API. If `api_key` is set, or `MEM0_API_KEY` is present in the environment, the toolkit connects to Mem0's hosted platform client. If no API key is present but `config` is supplied, the toolkit initializes upstream Mem0 from that local config object. If neither `api_key` nor `config` is supplied, the toolkit falls back to upstream Mem0 defaults. Operations need a `user_id`, either from tool config or from the run context, and they return an error string when no user ID can be resolved.

### Configuration

| Option                       | Type       | Required | Default | Notes                                                                                                                    |
| ---------------------------- | ---------- | -------- | ------- | ------------------------------------------------------------------------------------------------------------------------ |
| `config`                     | `text`     | `no`     | `null`  | Advanced upstream Mem0 config passed through to `Memory.from_config()`, typically used for local or self-managed setups. |
| `api_key`                    | `password` | `no`     | `null`  | Optional Mem0 Platform API key, also read from `MEM0_API_KEY`.                                                           |
| `user_id`                    | `text`     | `no`     | `null`  | Fixed user scope for all calls when you do not want to rely on runtime user context.                                     |
| `org_id`                     | `text`     | `no`     | `null`  | Optional Mem0 organization ID for platform usage, also read from `MEM0_ORG_ID`.                                          |
| `project_id`                 | `text`     | `no`     | `null`  | Optional Mem0 project ID for platform usage, also read from `MEM0_PROJECT_ID`.                                           |
| `infer`                      | `boolean`  | `no`     | `true`  | Let Mem0 infer facts from added content.                                                                                 |
| `enable_add_memory`          | `boolean`  | `no`     | `true`  | Enable `add_memory()`.                                                                                                   |
| `enable_search_memory`       | `boolean`  | `no`     | `true`  | Enable `search_memory()`.                                                                                                |
| `enable_get_all_memories`    | `boolean`  | `no`     | `true`  | Enable `get_all_memories()`.                                                                                             |
| `enable_delete_all_memories` | `boolean`  | `no`     | `true`  | Enable `delete_all_memories()`.                                                                                          |
| `all`                        | `boolean`  | `no`     | `false` | Enable all currently exposed Mem0 methods regardless of the individual flags.                                            |

### Example

```
agents:
  assistant:
    tools:
      - mem0:
          user_id: assistant-memory
          infer: true
```

```
add_memory("The user prefers terse release notes.")
search_memory("release notes")
get_all_memories()
delete_all_memories()
```

### Notes

- `api_key` is optional because the toolkit can use local/default upstream Mem0 initialization instead of the hosted Mem0 platform.
- This toolkit is separate from MindRoom's `memory.backend: mem0` setting, so enabling `mem0` here does not configure or replace MindRoom's built-in memory backend.
- If you want MindRoom's automatic memory extraction and built-in memory retrieval to use Mem0, configure that in [Memory System](https://docs.mindroom.chat/memory/index.md) instead of relying on this toolkit alone.
- Store API keys outside authored YAML even when the current metadata marks them as optional.

## \[`zep`\]

`zep` connects an agent directly to Zep Cloud for conversational memory and user-graph search.

### What It Does

`zep` exposes `add_zep_message()`, `get_zep_memory()`, and `search_zep_memory()`. The toolkit requires a Zep API key at initialization time and raises an error when neither `api_key` nor `ZEP_API_KEY` is available. If `session_id` is omitted, the toolkit generates a new session ID automatically. If `user_id` is omitted, the toolkit generates a new Zep user and creates it in the remote account. If `user_id` is provided but the user does not exist yet, the toolkit attempts to create it before use. `get_zep_memory(memory_type="context")` returns either session context or raw message history. `search_zep_memory(query, search_scope="edges")` searches the Zep user graph by facts or nodes. `ignore_assistant_messages` skips assistant-role content when messages are added to Zep.

### Configuration

| Option                      | Type       | Required | Default | Notes                                                                        |
| --------------------------- | ---------- | -------- | ------- | ---------------------------------------------------------------------------- |
| `session_id`                | `text`     | `no`     | `null`  | Optional stable Zep thread ID, otherwise a new UUID is generated.            |
| `user_id`                   | `text`     | `no`     | `null`  | Optional stable Zep user ID, otherwise a new user is generated and created.  |
| `api_key`                   | `password` | `yes`    | `null`  | Required in practice unless it is provided through `ZEP_API_KEY`.            |
| `ignore_assistant_messages` | `boolean`  | `no`     | `false` | Ignore assistant-role messages when adding session messages.                 |
| `enable_add_zep_message`    | `boolean`  | `no`     | `true`  | Enable `add_zep_message()`.                                                  |
| `enable_get_zep_memory`     | `boolean`  | `no`     | `true`  | Enable `get_zep_memory()`.                                                   |
| `enable_search_zep_memory`  | `boolean`  | `no`     | `true`  | Enable `search_zep_memory()`.                                                |
| `instructions`              | `text`     | `no`     | `null`  | Override the default tool instructions injected for the model.               |
| `add_instructions`          | `boolean`  | `no`     | `false` | Add the tool instructions to the model prompt.                               |
| `all`                       | `boolean`  | `no`     | `false` | Enable all currently exposed Zep methods regardless of the individual flags. |

### Example

```
agents:
  assistant:
    tools:
      - zep:
          user_id: assistant-user
          session_id: release-review
```

```
add_zep_message(role="user", content="The user prefers terse release notes.")
get_zep_memory(memory_type="context")
search_zep_memory(query="release notes", search_scope="edges")
```

### Notes

- `zep` is an external provider toolkit and does not change MindRoom's built-in memory backend or the behavior of the `memory` tool.
- Use explicit `user_id` and `session_id` values when you want continuity across runs instead of a fresh generated identity.
- `search_scope="edges"` returns fact-style results, while `search_scope="nodes"` returns node summaries.
- Store the API key through credentials or `ZEP_API_KEY` rather than inline YAML.

## Related Docs

- [Tools Overview](https://docs.mindroom.chat/tools/index.md)
- [Memory System](https://docs.mindroom.chat/memory/index.md)
- [Matrix & Attachments](https://docs.mindroom.chat/tools/matrix-and-attachments/index.md)
- [Attachments](https://docs.mindroom.chat/attachments/index.md)
