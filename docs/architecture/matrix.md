---
icon: lucide/message-square
---

# Matrix Integration

MindRoom uses the Matrix protocol for all agent communication. The Matrix integration is implemented in `src/mindroom/matrix/` and consists of several specialized modules.

## Why Matrix?

- **Federated** - Connect to any Matrix homeserver
- **Bridgeable** - Bridge to Discord, Slack, Telegram, and more
- **Open** - Open standard and open-source implementations
- **End-to-End Encryption** - Secure communication (optional, with encrypted room support)

## Architecture Overview

The Matrix integration is split into focused modules:

| Module | Purpose |
|--------|---------|
| `client.py` | Core client operations (login, send, edit, rooms) |
| `rooms.py` | Room management and state persistence |
| `users.py` | Agent user account creation and management |
| `presence.py` | Presence status and streaming decisions |
| `state.py` | Persistent state (accounts, rooms) via YAML |
| `identity.py` | Matrix ID parsing and agent identification |
| `event_info.py` | Event relation analysis (threads, edits, replies) |
| `message_builder.py` | MSC3440-compliant message construction |
| `mentions.py` | @mention parsing and formatting |
| `typing.py` | Typing indicator management |
| `large_messages.py` | Handling messages exceeding 64KB limit |
| `message_content.py` | Message extraction with MXC attachment support |

## Matrix Client

MindRoom uses `matrix-nio` for Matrix communication. The client is created with SSL context handling and encryption key storage:

```python
from mindroom.matrix.client import create_matrix_client, login

# Create client with optional SSL verification (for dev environments)
client = create_matrix_client(
    homeserver="https://matrix.example.com",
    user_id="@mindroom_agent:example.com",
    store_path="/path/to/encryption/keys"  # Optional, defaults to mindroom_data/encryption_keys/<sanitized_user_id>
)

# Login with password
client = await login(homeserver, user_id, password)

# Or use context manager for automatic cleanup
from mindroom.matrix.client import matrix_client

async with matrix_client(homeserver, user_id, access_token) as client:
    # Use client
    pass
```

### Environment Variables

- `MATRIX_HOMESERVER` - Matrix homeserver URL (default: `http://localhost:8008`)
- `MATRIX_SERVER_NAME` - Federation server name (if different from homeserver hostname)
- `MATRIX_SSL_VERIFY` - Set to `"false"` to disable SSL verification (default: `"true"`, dev only)
- `MINDROOM_ENABLE_STREAMING` - Set to `"false"` to disable message streaming/editing (default: `"true"`)

## Agent Users

Each agent gets its own Matrix user with the `mindroom_` prefix:

```
@mindroom_assistant:example.com
@mindroom_code:example.com
@mindroom_research:example.com
@mindroom_router:example.com  (built-in routing agent)
```

Users are automatically created during orchestrator startup:

```python
from mindroom.matrix.users import create_agent_user

agent_user = await create_agent_user(
    homeserver="https://matrix.example.com",
    agent_name="assistant",
    agent_display_name="Assistant Agent"
)
# Returns AgentMatrixUser with user_id, password, etc.
```

Credentials are persisted in `mindroom_data/matrix_state.yaml` for reuse across restarts.

## State Management

Matrix state (accounts and rooms) is persisted using Pydantic models:

```python
from mindroom.matrix.state import MatrixState

# Load existing state
state = MatrixState.load()

# Access accounts
account = state.get_account("agent_assistant")

# Access rooms
room = state.get_room("lobby")
room_aliases = state.get_room_aliases()  # {key: room_id}

# Add/update and save
state.add_room("dev", "!abc:example.com", "#dev:example.com", "Development")
state.save()
```

State file location: `mindroom_data/matrix_state.yaml`

## Room Management

Agents can:

- Join existing rooms by alias or ID
- Create new rooms with AI-generated topics
- Respond to invites automatically
- Leave rooms when no longer configured
- Set room avatars

```python
from mindroom.matrix.rooms import ensure_room_exists, ensure_all_rooms_exist

# Ensure a single room exists (creates if needed)
room_id = await ensure_room_exists(
    client=client,
    room_key="lobby",
    config=config,
    power_users=["@mindroom_assistant:example.com"]
)

# Ensure all configured rooms exist
room_ids = await ensure_all_rooms_exist(client, config)
```

### DM Room Detection

```python
from mindroom.matrix.rooms import is_dm_room

if await is_dm_room(client, room_id):
    # Handle DM-specific behavior
    pass
```

## Threading (MSC3440)

Agents respond in threads following the [MSC3440](https://github.com/matrix-org/matrix-spec-proposals/blob/main/proposals/3440-threading-via-relations.md) specification. Thread messages use `m.relates_to` with `rel_type: m.thread`.

```
├── User: @assistant help with this code
│   ├── Assistant: I can help! Let me look at it...
│   ├── User: It should return a list
│   └── Assistant: Here's the updated version...
```

### Building Thread Messages

```python
from mindroom.matrix.message_builder import build_message_content

content = build_message_content(
    body="Here's the solution...",
    thread_event_id="$original_message_id",
    latest_thread_event_id="$latest_in_thread",  # For MSC3440 fallback
    mentioned_user_ids=["@user:example.com"]
)
```

### Event Analysis

The `EventInfo` class analyzes all Matrix event relations:

```python
from mindroom.matrix.event_info import EventInfo

info = EventInfo.from_event(event.source)

if info.is_thread:
    thread_root = info.thread_id

if info.is_edit:
    original = info.original_event_id

if info.is_reply:
    reply_target = info.reply_to_event_id

if info.can_be_thread_root:
    # Safe to start a new thread from this message
    pass
```

## Message Flow

### Sync Loop

Each agent bot runs its own sync loop:

```python
# From bot.py
SYNC_TIMEOUT_MS = 30000

async def sync_forever(self):
    await self.client.sync_forever(timeout=SYNC_TIMEOUT_MS, full_state=True)
```

Events are processed in background tasks to prevent blocking:

1. **Sync receives event** - Matrix server sends event via long-polling
2. **Event callback triggered** - Appropriate handler based on event type
3. **Background task created** - Processing happens asynchronously
4. **Response sent** - Agent responds in thread with streaming or full message

### Streaming Responses

Agents can stream responses by editing messages progressively:

```python
from mindroom.matrix.presence import should_use_streaming

# Only stream if user is online (saves API calls for offline users)
if await should_use_streaming(client, room_id, requester_user_id):
    # Use streaming (message edits)
else:
    # Send complete message at once
```

## Presence

Agents set their Matrix presence with status messages containing model and role information:

```python
from mindroom.matrix.presence import set_presence_status, build_agent_status_message

# Build status message
status = build_agent_status_message(agent_name, config)
# Example: "🤖 Model: anthropic/claude-sonnet-4-latest | 💼 Code assistant | 🔧 5 tools available"

await set_presence_status(client, status, presence="online")
```

### Presence States

- **online** - Agent is running and ready (includes model info in status message)
- **unavailable** - Agent is idle but still connected (client open)
- **offline** - Agent is stopped or disconnected

Presence is checked to decide whether to use streaming:

```python
from mindroom.matrix.presence import is_user_online

if await is_user_online(client, user_id):
    # User is online, streaming will be visible
    pass
```

While processing, agents use **typing indicators** instead of changing presence state, providing real-time feedback that they're working on a response.

## Typing Indicators

Show typing indicator while processing:

```python
from mindroom.matrix.typing import typing_indicator

async with typing_indicator(client, room_id):
    # Typing indicator shown, auto-refreshed
    response = await generate_response()
# Typing indicator automatically stopped
```

The typing indicator is automatically refreshed at the minimum of half the timeout interval or 15 seconds to remain visible during long operations.

## Mentions

Parse and format @mentions in messages:

```python
from mindroom.matrix.mentions import format_message_with_mentions

# Parse @agent mentions and create proper Matrix content
content = format_message_with_mentions(
    config=config,
    text="Hey @assistant can you help?",
    sender_domain="example.com",
    thread_event_id="$thread_root"
)
# Returns content dict with m.mentions, formatted_body with links
```

Mention formats supported:
- `@calculator` - Short agent name
- `@mindroom_calculator` - Full username
- `@mindroom_calculator:localhost` - Full Matrix ID

## Large Messages

Messages exceeding the 64KB Matrix event limit are automatically handled:

```python
from mindroom.matrix.large_messages import prepare_large_message

# Automatically uploads large content as MXC attachment
content = await prepare_large_message(client, room_id, content)
```

- Messages > 55KB: Uploaded as `message.txt` attachment
- Edits > 27KB: Lower threshold due to edit structure doubling size
- Preview text included in message body
- Custom metadata (`io.mindroom.long_text`) for reconstruction
- Supports encrypted rooms (content encrypted before upload)

## Identity Management

The `MatrixID` class handles Matrix user ID parsing:

```python
from mindroom.matrix.identity import MatrixID, extract_agent_name

# Parse Matrix ID
mid = MatrixID.parse("@mindroom_assistant:example.com")
print(mid.username)  # "mindroom_assistant"
print(mid.domain)    # "example.com"
print(mid.full_id)   # "@mindroom_assistant:example.com"

# Create from agent name
mid = MatrixID.from_agent("assistant", "example.com")

# Extract agent name from Matrix ID
agent_name = extract_agent_name("@mindroom_code:localhost", config)
# Returns "code" if configured, None otherwise
```

## Configuration

Matrix settings are derived from `config.yaml`:

```yaml
# Rooms are auto-created if they don't exist
agents:
  assistant:
    rooms: [lobby, dev]  # Room aliases (not full IDs)

# Teams also get Matrix users
teams:
  research_team:
    rooms: [research]
```

Room aliases are resolved to room IDs automatically. Full room IDs (starting with `!`) can also be used.

### Room Creation

When a room alias doesn't exist on the server:

1. Room is created with the configured alias
2. AI-generated topic is set based on room name and configured agents
3. Power users (agents configured for the room) are invited
4. Room avatar is set if available in `avatars/rooms/{room_key}.png`
5. State is saved to `matrix_state.yaml`
