# Matrix Integration

MindRoom uses the Matrix protocol for all agent communication. The integration is implemented in `src/mindroom/matrix/`.

## Why Matrix?

- **Federated** - Connect to any Matrix homeserver
- **Bridgeable** - Bridge to Discord, Slack, Telegram, and more
- **Open** - Open standard and open-source implementations
- **End-to-End Encryption** - Secure communication with encrypted room support

## Matrix Client

MindRoom uses `matrix-nio` for Matrix communication with SSL context handling and encryption key storage.

### Environment Variables

| Variable                    | Default                 | Description                              |
| --------------------------- | ----------------------- | ---------------------------------------- |
| `MATRIX_HOMESERVER`         | `http://localhost:8008` | Matrix homeserver URL                    |
| `MATRIX_SERVER_NAME`        | (from homeserver)       | Federation server name                   |
| `MATRIX_SSL_VERIFY`         | `true`                  | Set to `false` for dev/self-signed certs |
| `MINDROOM_ENABLE_STREAMING` | `true`                  | Enable message streaming via edits       |

## Agent Users

Each agent gets its own Matrix user with the `mindroom_` prefix:

```
@mindroom_assistant:example.com
@mindroom_router:example.com  (built-in routing agent)
```

Users are automatically created during orchestrator startup and credentials are persisted in `{STORAGE_PATH}/matrix_state.yaml` (default: `mindroom_data/matrix_state.yaml`).

## Room Management

Agents can join existing rooms, create new rooms with AI-generated topics, respond to invites automatically, leave unconfigured rooms, and set room avatars.

Rooms are auto-created via `ensure_room_exists()` and `ensure_all_rooms_exist()`. DM rooms can be detected with `is_dm_room(client, room_id)`.

## Threading (MSC3440)

Agents respond in threads following [MSC3440](https://github.com/matrix-org/matrix-spec-proposals/blob/main/proposals/3440-threading-via-relations.md). Thread messages use `m.relates_to` with `rel_type: m.thread`.

```
├── User: @assistant help with this code
│   ├── Assistant: I can help! Let me look at it...
│   ├── User: It should return a list
│   └── Assistant: Here's the updated version...
```

Use `build_message_content()` from `message_builder.py` to construct thread-aware messages, and `EventInfo.from_event()` to analyze event relations (threads, edits, replies, reactions).

## Message Flow

### Sync Loop

Each agent bot runs its own sync loop with 30-second long-polling timeout. Sync loops are wrapped with `_sync_forever_with_restart()` for automatic restart on connection failures.

Events are processed in background tasks:

1. Sync receives event via long-polling
1. Event callback triggered (`_on_message`, `_on_invite`, etc.)
1. Background task created for async processing
1. Agent responds in thread

### Streaming Responses

Agents stream responses by progressively editing messages. Streaming is enabled only when the requesting user is online (checked via `should_use_streaming()`), saving API calls for offline users.

Tool call telemetry is emitted as structured collapsible blocks (`<tool>...</tool>`, `<validation>...</validation>`) and mirrored in `io.mindroom.tool_trace` metadata on the same message content.

## Presence

Agents set their Matrix presence with status messages containing model and role information (e.g., "🤖 Model: anthropic/claude-sonnet-4-5-latest | 💼 Code assistant | 🔧 5 tools available").

**Presence States:**

- **online** - Agent running and ready
- **unavailable** - Agent idle but connected (treated as online for streaming)
- **offline** - Agent stopped or disconnected

## Typing Indicators

Agents show typing indicators while processing via `typing_indicator()` context manager. The indicator auto-refreshes at `min(timeout/2, 15)` seconds to remain visible during long operations.

## Mentions

Mentions are parsed via `format_message_with_mentions()` which handles multiple formats:

- `@calculator` - Short agent name
- `@mindroom_calculator` - Full username
- `@mindroom_calculator:localhost` - Full Matrix ID

Returns content with `m.mentions` and `formatted_body` containing clickable links.

## Large Messages

Messages exceeding the 64KB Matrix event limit are automatically handled by `prepare_large_message()`:

- Messages > 55,000 bytes: Uploaded as `message.txt` attachment
- Edits > 27,000 bytes: Lower threshold since edit structure roughly doubles size
- Preview text included in message body (maximum that fits)
- Custom metadata (`io.mindroom.long_text`) for reconstruction
- Preserves essential metadata (for example mentions) while dropping bulky optional fields to stay within event limits
- Encrypted rooms: Content encrypted before upload as `message.txt.enc`

## Identity Management

The `MatrixID` class handles Matrix user ID parsing and agent identification:

```
mid = MatrixID.parse("@mindroom_assistant:example.com")
mid.username  # "mindroom_assistant"
mid.domain    # "example.com"
mid.full_id   # "@mindroom_assistant:example.com"

# Create from agent name
mid = MatrixID.from_agent("assistant", "example.com")

# Extract agent name (returns "code" if configured, None otherwise)
agent_name = extract_agent_name("@mindroom_code:localhost", config)
```

## Configuration

Matrix settings are derived from `config.yaml`:

```
agents:
  assistant:
    rooms: [lobby, dev]  # Room aliases (auto-created if needed)

teams:
  research_team:
    rooms: [research]
```

Room aliases are resolved to room IDs automatically. Full room IDs (starting with `!`) are also supported.

When a room doesn't exist, it's created with an AI-generated topic, power users are invited, and avatars are set from `avatars/rooms/{room_key}.png` if available.
