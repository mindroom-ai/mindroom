---
icon: lucide/bell-ring
---

# Agent Callbacks

Agent callbacks let a MindRoom agent hand a background process a script that wakes the originating agent and thread when the work finishes.

They are a small adapter over external triggers rather than a separate callback service.

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

Callbacks use the existing `external_trigger_policy`, which is enabled by default, and count toward `max_triggers_per_owner` until used or deleted.

## Tool Result

`mint_callback(label)` returns a script path and an instruction like this:

```text
When finished, run: bash /path/to/callback_1234.sh "<short result summary>"
```

The script needs only Bash and curl.

It posts the summary back to the room and thread where the callback was minted, waking the same MindRoom agent.

MindRoom stores the callback as a bearer-authenticated external trigger whose delivery mode is `single_use`.

After successful Matrix delivery, the external trigger is consumed and the script deletes itself.

If delivery fails, the script remains so the background agent can retry it.

Changing the request's `event_id` cannot make the trigger deliver more than once.

Unused callbacks do not expire automatically, so delete abandoned records with `external_trigger_manager` if they are no longer needed.

## Network Access

Generated scripts call `http://127.0.0.1:8765` by default.

Set `MINDROOM_URL` when the background process needs another address to reach the MindRoom API.

Point `MINDROOM_URL` only at a trusted MindRoom endpoint because the script sends its bearer token there.

## Security

Each script contains a random bearer token whose hash is stored in MindRoom control state.

The token can only wake the agent, room, and thread captured when it was minted.

Missing or incorrect tokens receive the same not-found response.

The request then follows the normal external-trigger readiness, authorization, room-membership, replay, Matrix-delivery, and failure-retry path.

Use [External Triggers](external-triggers.md) directly for reusable integrations that need stable signed identities.
