---
icon: lucide/route
---

# Router Configuration

The router is a built-in system component that handles intelligent message routing and room management.
It decides which agent or team should respond when no specific agent or team is mentioned, sends welcome messages to new rooms, and manages various system-level tasks.

See also [Threads, Replies & Participation](threads.md), [Access Control](../authorization.md), and [Logs & Monitoring](../deployment/operational-log-events.md).

## Configuration

```yaml
router:
  # Model for routing decisions (defaults to "default")
  model: haiku

  # Accept all, none, or matching inviter ID patterns (default: true)
  accept_invites: true

```

The router supports these configuration options:

| Option | Type | Default | Description |
|--------|------|---------|-------------|
| `model` | string | `"default"` | Model to use for ordinary routing and judgment fallback |
| `judgment` | object or null | `null` | Optional JEV (TypeSafe) responder selection before the LLM router |
| `access` | object or null | `null` | Membership-based responder access policy |
| `accept_invites` | bool or list[string] | `true` | Accept all inbound Matrix room invites with `true`, none with `false` or `[]`, or only inviters matching an exact or wildcard Matrix user ID in the list. Accepted room IDs are persisted, rejoined after restart, and preserved during room cleanup |

Invitation patterns are matched after identity alias resolution and use the same case-sensitive wildcard semantics as responder `access.users`.
Invitation acceptance grants room membership only and remains independent from responder access.
The router applies its `access` policy to every interaction after joining.
Omitted router access resolves to `current_room_members: true` with empty room and user grants.
Setting only `access.users` preserves that current-room-member grant; set `current_room_members: false` explicitly to remove it.
See [Authorization](../authorization.md) for the policy fields.

## How Routing Works

When a message arrives in a room without a specific agent or team mention, MindRoom first builds the eligible responder candidate set for that sender and room.

1. If the thread requires explicit targeting, the router stays silent; eligible existing individual agents may still use the opt-in [adaptive participation](threads.md#adaptive-participation)
2. If exactly one eligible responder remains, that agent or team handles the message directly
3. If multiple eligible responders remain, the router analyzes the message content and any recent thread context (up to 3 previous messages)
4. Based on the candidate entities' roles, tools, and instructions, it selects the best match
5. The router posts a message mentioning the selected entity (e.g., "@agent could you help with this?")
6. The mentioned agent or team sees the mention and responds in the thread

For configured rooms, routing candidates come only from `agents.<name>.rooms` and `teams.<name>.rooms`, then are filtered by the sender's per-entity reply permissions.
For ad-hoc rooms accepted through invites, routing candidates come from the sender-visible MindRoom agents and teams currently joined to that room, then are filtered by the same sender permissions.

When multiple responders are eligible, the router uses a structured output schema to ensure consistent routing decisions, including the selected agent or team name and reasoning for the selection.

## Responder selection judgments

Set `router.judgment` to make one bounded choice among the already eligible agents and teams before ordinary routing.
This is optional; omitting it or setting it to `null` preserves existing behavior, even when a TypeSafe API key is present.
Explicit mentions, existing thread participation rules, authorization, and deterministic single-candidate routing are unchanged.

LLM routing uses the existing `router.model` setting. To use a cheap LLM, point it at a configured model alias.

```yaml
router:
  model: cheap_router  # Must exist under models
```

To try JEV (System One), set `TYPESAFE_API_KEY` in the instance environment or config-adjacent `.env` and enable the judgment.
There is one LLM routing implementation: it handles ordinary routing and fallback when JEV cannot decide.

```yaml
router:
  model: default
  judgment:
    provider: typesafe
    threshold: 0.8
    timeout_seconds: 1.5
```

JEV receives candidate descriptions: roles, available tools, delegation capabilities, and the brief instructions used by ordinary routing.
It sees the current request text and the last three complete visible message bodies, with sender fields replaced by speaker aliases.
This is a text-only view: no system prompt, private tool results, attachment contents, or full conversation history is added.
Messages are not cut off to fit: sensitive or oversized input uses ordinary routing instead.
The complete judgment request is limited to 16,000 UTF-8 bytes and at most 253 candidate responders (plus two special choices).

A selected candidate routes through the existing delivery path.
A confident `no_fit` produces the existing Matrix message asking the user to mention a responder or rephrase.
For OpenAI-compatible `model: auto` requests, it returns HTTP 400 with error code `no_suitable_responder` instead of selecting the first agent.
The `multiple` choice means no single candidate can cover the request; it falls back to ordinary single-responder routing and does not launch several agents.
Low confidence, abstention, invalid output, missing credentials, capacity exhaustion, or a timeout also falls back to ordinary routing using the existing runtime model resolution.
The LLM router retains its configured prompts, `router.model`, and room/thread model overrides.
Its existing context window includes up to three previous messages truncated to 100 characters each.

System One returns a distribution and confidence; both the selected option's probability and confidence must meet `threshold`, and tied winners abstain.
The dedicated rubric does not use the ordinary router prompt overrides; those still apply to fallback routing.

Judgments share the same process-wide capacity limits as participation and mid-turn checks: eight concurrent calls and one per instance/router owner, without a waiting queue.
There are no application-level retries; the configured deadline bounds the JEV request.
Logs record the backend, decision, probability when available, latency, token usage, and failure category without logging the request text.

## Router Responsibilities

The router is a special system agent that handles several important tasks beyond message routing:

See [Command Handling](../chat-commands.md#command-handling).

### Welcome Messages

After accepting an invite, the router sends a requester-scoped welcome message only when the room has no existing message history.

That welcome message lists:

- Available agents and teams visible to the inviter with their descriptions
- How to interact with agents and teams (mentions, commands)
- Quick command reference

Startup welcomes with no requester list configured room responders when the room is statically configured.
Startup does not send requester-less welcomes in persisted ad-hoc invite rooms because the original inviter cannot be re-authorized safely from persisted state.
The live invite callback sends the requester-scoped welcome when the inviter currently has router reply access.

Use `!hi` in any room to see the welcome message again.

The `!hi` welcome lists responders visible to the requester.

See [Room Management](../rooms.md#room-management_1), [Voice Message Processing](../voice.md#voice-message-processing), [Configuration Confirmations](../chat-commands.md#configuration-confirmations), and [Scheduled Task Restoration](../scheduling.md#scheduled-task-restoration).

## Routing Behavior Details

### Single Responder Optimization

When there is only one eligible responder for a room, the router skips AI routing entirely.
The single responder handles messages directly, which is faster and more efficient.

See [Multi-Human Thread Protection](threads.md#multi-human-thread-protection).

### Routing Fallback

If routing fails (model error, invalid suggestion, etc.), the router sends a helpful error message that asks the user to mention an agent or team directly or rephrase the request.

Users can mention eligible agents or teams directly with `@entity_name` to bypass routing, while configured-room allowlists and reply permissions still decide whether that entity may answer.

## Note on the Router Agent

The router is always present and cannot be disabled.
It automatically joins any room with configured agents or teams.
If no `router` section is configured, it uses the default model.

The router account is not a conversational AI agent to tag directly.
If a message mentions only the router and no other users, agents, or teams, the router replies with the rules of engagement instead of answering the prompt.
Mention a specific agent or team when you want that entity to answer.
Mention multiple agents when you want an ad-hoc collaboration, or mention a configured team directly for its team workflow.
When one human and one agent or team are already talking in a thread, continuing without an explicit tag is fine.
Explicit tags select the agents or teams you want next.
An untagged single-human thread can also continue an eligible ad-hoc team of previously mentioned or participating individual agents; named team IDs do not become ad-hoc members.
Multi-human threads use the explicit-targeting default and opt-in individual [participation](threads.md#adaptive-participation).
In a new untagged message, automatic routing can still choose an agent or team when that is appropriate.
