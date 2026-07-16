---
icon: lucide/bell-ring
---

# Agent Callbacks

Agent callbacks let a MindRoom agent hand a background process a script that wakes the originating agent and thread when the work finishes.

The intended flow is simple:

1. The MindRoom agent starts a background Codex session or another long-running task.
2. It calls `mint_callback` with a short label for that task.
3. It includes the returned instruction in the background agent's prompt.
4. The background agent runs the script with a short result summary when it finishes.

## Configuration

Enable the tool on agents that launch background work:

```yaml
agents:
  orchestrator:
    role: Launch and supervise coding agents.
    tools:
      - callback_manager
```

No callback-specific configuration is required.

## Tool Result

`mint_callback(label)` returns a script path and an instruction like this:

```text
When finished, run: bash /path/to/cb_1234.sh "<short result summary>"
```

The script needs only Bash and curl.

It posts the summary back to the room and thread where the callback was minted, waking the same MindRoom agent.

After successful delivery, both the callback record and script are deleted.

If delivery fails, the script remains so the background agent can retry it.

Callbacks expire after seven days and can deliver only once.

## Network Access

Generated scripts call `http://127.0.0.1:8765` by default.

Set `MINDROOM_URL` when the background process needs another address to reach the MindRoom API.

## Security

Each script contains a random bearer token whose hash is stored in MindRoom control state.

The token can only wake the agent, room, and thread captured when it was minted.

Missing or incorrect tokens receive the same not-found response.

Use [External Triggers](external-triggers.md) for long-lived integrations that need stable signed identities or replay handling.
