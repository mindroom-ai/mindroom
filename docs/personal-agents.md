---
icon: lucide/user-plus
---

# Personal Agents

A [`private`](configuration/agents.md#private-instances) agent keeps one copy of its state for each requester, but everyone still talks to a single Matrix account, so nobody can address "Bob's agent", add it to a team, or delegate to it.
The optional `personal_agents` section gives each person onboarded through [`personal_rooms`](personal-rooms.md) their own agent with its own Matrix account, all built from one template whose defaults you control.
Use `private` when one shared agent with separate state per requester is enough, and `personal_agents` when each person needs an agent others can mention or put in a team.
A generated agent is a personal agent in the sense of the [egress broker](deployment/egress-broker.md) and [MCP gateway](deployment/mcp-gateway.md) pages only when its worker scope, from the template's `agent` or `defaults.worker_scope`, is `user` or `user_agent`.

## Setup

```yaml
rooms:
  lobby: {}
personal_agents:
  mind:
    name: "{username}_mind"
    matrix_username: "{username}.mind"
    agent:
      display_name: "{user_display_name}'s Mind"
      role: Personal assistant for {user}.
      model: sonnet
      memory_backend: file
    users:
      "@alice:example.org":
        model: opus
personal_rooms:
  agent: mind
  onboarding_rooms: [lobby]
```

`mind` is the template key, never an agent itself, and `personal_rooms.agent: mind` gives each user's personal room that user's own agent.
Because `access` is omitted, only the owner and platform administrators may use each generated agent.

## Configuration

`personal_agents` maps template keys to objects.
A key follows the [agent naming rules](configuration/agents.md#naming-rules) and does not equal an agent or team key.

| Field | Type | Default | Meaning |
| --- | --- | --- | --- |
| `name` | string | `"<template key>_{username}"` | Agent key pattern; it contains `{username}` exactly once and renders to letters, digits, and underscores. |
| `matrix_username` | string | `mindroom_<agent key>` | Matrix localpart pattern; it contains `{username}` exactly once plus at least one other character, so no agent takes a person's bare localpart, and renders to `[a-z0-9._=/-]`. `_<MINDROOM_NAMESPACE>` is appended when a namespace is set, and the default account carries the same suffix. |
| `agent` | object | Required | Any [agent](configuration/agents.md#configuration-options) fields; the defaults for every user's agent. |
| `users` | object | `{}` | Per-user overrides keyed by concrete Matrix user ID, with no patterns. Each override is an agent field that replaces the template's value whole, so a list or object is not merged. |

Placeholders are replaced as exact tokens, so other braces in prompts stay literal and a replaced value is never replaced again.

| Placeholder | Value | Where |
| --- | --- | --- |
| `{user}` | The user's full Matrix ID. | `display_name`, `role`, and each `instructions` item |
| `{username}` | The localpart lowercased, every character outside `[a-z0-9_]` replaced by `_`, cut to 40 characters, or `user` when empty. | `display_name`, `role`, each `instructions` item, `name`, and `matrix_username`; in the last two it is the only placeholder |
| `{user_display_name}` | The user's Matrix display name at onboarding, without control characters, cut to 100 characters, or the localpart when unset. | `display_name`, `role`, and each `instructions` item |

If the rendered `name` or Matrix account is already taken, `{username}` becomes the slug plus `_` and the first 6 hex characters of the SHA-256 of the user ID, for example `alice_86f01a` for `@alice:example.org`.
Taken means an authored agent or team, including one saved but not yet reloaded, a template key, `router`, `user`, `_shared`, another generated agent, or an account MindRoom already manages.
A user on another server than MindRoom's always gets this hashed form, so nobody elsewhere can take a local person's plain name.
A second clash fails that user's onboarding.

Every generated agent also gets these settings, whatever the template says:

- The owner is added to `access.users` and `credential_managers` when missing.
- `access.members_of_rooms` is `[]` when missing or null, so membership of the agent's rooms grants nothing unless you list rooms yourself.
- `allow_self_config` is `false`, even when `defaults.allow_self_config` is true, because `self_config` edits `agents.<name>`, which a generated agent does not have.

Config validation rejects `private` and `allow_self_config: true` in the template's `agent` and in any `users` override, because a personal agent already belongs to one user.
A template or override that would produce an invalid agent fails validation as soon as you save or load it, even before anyone has onboarded, with `personal_agents would generate an invalid agent for a new user:` and the reason.

## Generated Agents

Generated agents are ordinary agents in the running config, and workers receive them too.
They never appear in the saved config file, and the dashboard and `config_manager` edit only what you authored.

- Editing the template changes every user's agent at the next reload.
  The name, Matrix account, and `{user_display_name}` stay fixed at what onboarding stored.
- An authored agent or team that uses a generated agent's name fails validation with an error such as `agents.mind_alice collides with the agent generated from personal_agents.mind for @alice:example.org`.
  The same error rejects a dashboard or `config_manager` save that edits a generated agent in place, so use `personal_agents.<key>.users` for per-user changes.
- A team member or `delegate_to` entry can name a generated agent only once it exists, because validation rejects an unknown agent name before then.
- Removing a template stops its agents but keeps their roster entries and Matrix accounts, and bringing the template back with the same key starts them again.

## Letting Agents Talk to Each Other

By default nobody except the owner and administrators can use a generated agent, so agents do not reach each other.
To let them, set `access` under the template's `agent:`, for example `members_of_rooms: [lobby]` or `current_room_members: true`.
With `members_of_rooms: [lobby]` and the lobby as the onboarding room, every onboarded user can use every other user's agent.

> [!WARNING]
> **Whoever `access` admits works with the owner's agent.**
> Anyone the template's `access` admits can work with the owner's agent, including its memory, its files, and its shared credentials.
> That includes the admitted person's own agent acting for them, because an agent mentioned in a reply acts for the person who requested that reply (see [Agents mentioning other agents](authorization.md#agents-mentioning-other-agents)).
> Admit only people you would trust with everything an owner gives their agent.

## Onboarding Behavior

The router handles a join, a `commands` entry, or a `backfill` hit in an onboarding room like any [personal-room](personal-rooms.md#onboarding-behavior) trigger, with these differences:

1. MindRoom registers the agent's Matrix account and starts the agent without a restart; a failed registration leaves no agent behind.
2. The usual personal-room flow runs with that user's own agent once the agent has finished its first sync.
   Until then the trigger is pending, which does not hold up the onboarding room for anyone else, and the router logs `Personal agent is starting`.
3. The user keeps the agent, its account, and its room when they leave the onboarding room or are banned, and MindRoom has no command that removes them.

A [bridge alias](authorization.md#bridge-aliases) onboards onto its person's agent, which gives the alias account its own personal room.
A Matrix ID containing a space, `*`, `?`, `[`, or `]` gets no agent, because access settings cannot name it exactly.

The roster of generated agents is `tracking/personal_agents.json` in the storage directory; back it up with the storage, because an agent's name and Matrix account never change once recorded there.
Stop MindRoom before editing it by hand; a file MindRoom cannot read fails config loading with `Personal agent roster <path> is invalid:` and the reason.

> [!WARNING]
> **Everyone who joins an onboarding room gets an agent on your model keys.**
> Every human who joins an onboarding room, sends one of its `commands`, or is picked up by `backfill` gets a Matrix account and a running agent, and nothing caps how many.
> The only gate is membership of the onboarding room, so control it through that room's [`join_policy`](rooms.md#room-policy) and invitations.
> An open or federated lobby gives an agent to anyone who joins it, and `backfill: true` creates agents for every current member at once.
> Bridged puppet accounts count as people unless you list them in [`bot_accounts`](authorization.md#bot-accounts).

MindRoom's own accounts and the internal `mindroom_user` never get an agent.

The dashboard's Settings page edits `personal_agents` next to Personal rooms.
Its `personal_rooms.agent` dropdown lists the configured agents and the `personal_agents` templates.

## Switching From a Single Personal-Room Agent

When `personal_rooms.agent` changes from an agent to a template, existing personal rooms stay with the old agent, and a user's new agent cannot take over a room the old agent created.
Set a new `personal_rooms.alias_prefix` at the same time so that each user gets a new personal room with their own agent; otherwise their onboarding fails with `Personal-room alias ... which this agent does not own`.
The old agent's `access` no longer limits who gets an agent: everyone in an onboarding room gets one.

## Troubleshooting Personal Agents

A failure affects only that user's onboarding.
Failed onboarding retries after 30 seconds, then at doubling intervals up to one hour, and a config reload retries it immediately.

| Log message | Meaning |
| --- | --- |
| `Personal agent is starting` | Normal: the user's agent was just created or has not finished its first sync, and the trigger is retried when it has. |
| `Personal-room onboarding trigger failed` | Onboarding one user failed. For a personal agent, `error` is usually the homeserver refusing the registration, `No free personal agent name for <user>` when the plain and the hashed names are both taken, or `<user> cannot own a personal agent` for an unsupported Matrix ID. |
| `Personal-room reconciliation failed` | A retry failed, with the attempt number and the delay before the next try. |
| `Personal agent roster unavailable` | The roster file cannot be read, and the pass is retried. |

- A refused registration usually means the localpart is already held by another account, or, with `MATRIX_MANAGED_ACCOUNT_AUTH=appservice`, that a custom `matrix_username` is outside the application service's exclusive user namespace (see [Agent Users](matrix.md#agent-users)).
  Make the namespace cover the rendered name, including the `_<MINDROOM_NAMESPACE>` suffix, or change the pattern.
- Each user adds a Matrix account, a sync loop, and an encryption device.
  That is fine for dozens of users and heavy for hundreds.
