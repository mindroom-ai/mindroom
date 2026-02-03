---
icon: lucide/message-square
---

# Matrix Integration

MindRoom uses the Matrix protocol for all agent communication.

## Why Matrix?

- **Federated** - Connect to any Matrix homeserver
- **Bridgeable** - Bridge to Discord, Slack, Telegram, and more
- **Open** - Open standard and open-source implementations
- **End-to-End Encryption** - Secure communication (optional)

## Matrix Client

MindRoom uses `matrix-nio` for Matrix communication:

```python
from nio import AsyncClient

client = AsyncClient(homeserver, user_id)
await client.login(access_token)
await client.sync_forever()
```

## Agent Users

Each agent gets its own Matrix user:

```
@mindroom_assistant:example.com
@mindroom_code:example.com
@mindroom_research:example.com
```

Users are automatically created and managed by the orchestrator.

## Room Management

Agents can:

- Join existing rooms
- Create new rooms (from config)
- Respond to invites
- Leave rooms

## Threading

Agents respond in threads when available, keeping conversations organized:

```
├── User: @assistant help with this code
│   ├── Assistant: I can help! Let me look at it...
│   ├── User: It should return a list
│   └── Assistant: Here's the updated version...
```

## Presence

Agents update their Matrix presence status:

- **Online** - Agent is running and ready
- **Unavailable** - Agent is processing
- **Offline** - Agent is stopped
