# Mindroom Demo Scripts

This directory contains several demo scripts to test and showcase mindroom functionality.

## Prerequisites

1. Ensure mindroom is installed and configured:
   ```bash
   uv sync --all-extras
   source .venv/bin/activate
   ```

2. Create a user account if you haven't already:
   ```bash
   mindroom user create
   ```

3. Start the mindroom agents in another terminal:
   ```bash
   mindroom run --log-level DEBUG
   ```

## Available Scripts

### 1. `demo_real_messages.py` - Basic Message Demo

Sends various types of messages to test agent responses.

```bash
./demo_real_messages.py
```

Features:
- Direct mentions to specific agents
- Thread creation and replies
- Multi-agent scenarios
- Tests response deduplication

### 2. `demo_comprehensive.py` - Full Feature Demo

A comprehensive demo with room creation and real-time monitoring.

```bash
./demo_comprehensive.py
```

Features:
- Creates a new demo room
- Automatically invites all agents
- Sends test messages with live monitoring
- Shows responses in a formatted table

### 3. `monitor_room.py` - Room Monitor

Monitor all messages in a specific room.

```bash
./monitor_room.py !roomid:localhost
```

Features:
- Real-time message monitoring
- Color-coded output by sender type
- Thread detection
- Useful for debugging

## Usage Tips

1. **Watch the logs**: Run mindroom with `--log-level DEBUG` to see detailed agent behavior:
   ```bash
   mindroom run --log-level DEBUG
   ```

2. **Test specific scenarios**:
   - Direct mentions: `@calculator: What is 2+2?`
   - Thread conversations: Start a thread and watch single-agent responses
   - Multi-agent threads: Mention multiple agents to see them collaborate

3. **Room IDs**: You can find room IDs in Element/Matrix client or by using:
   ```bash
   mindroom room list
   ```

## Example Workflow

1. Terminal 1 - Start agents:
   ```bash
   mindroom run --log-level DEBUG
   ```

2. Terminal 2 - Run demo:
   ```bash
   ./demo_comprehensive.py
   ```

3. Terminal 3 (optional) - Monitor room:
   ```bash
   ./monitor_room.py !roomid:localhost
   ```

## Troubleshooting

- **Login failures**: Ensure your Matrix server is running
- **No responses**: Check that agents are running and have joined the room
- **Duplicate responses**: The ResponseTracker should prevent this - check logs

## What to Look For

When running demos, observe:

1. **Response Tracking**: Each event should only be responded to once
2. **Thread Behavior**: Only one agent responds in threads (unless mentioned)
3. **Mention Handling**: Mentioned agents always respond
4. **Agent Emojis**: Each agent has a unique emoji in logs
5. **Timing**: Responses should appear within a few seconds
