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
      display_name: "{user_first_name}'s Mind"
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

In the template's `delegate_to`, the template key names each user's own agent, so `delegate_to: [mind]` lets every user's agent run fresh copies of itself as [subagents](tools/agent-orchestration.md#agent-delegation).

Placeholders are replaced as exact tokens, so other braces in prompts stay literal and a replaced value is never replaced again.

| Placeholder | Value | Where |
| --- | --- | --- |
| `{user}` | The user's full Matrix ID. | `display_name`, `role`, and each `instructions` item |
| `{username}` | The localpart lowercased, every character outside `[a-z0-9_]` replaced by `_`, cut to 40 characters, or `user` when empty. | `display_name`, `role`, each `instructions` item, `name`, and `matrix_username`; in the last two it is the only placeholder |
| `{user_display_name}` | The user's Matrix display name at onboarding, without control characters, cut to 100 characters, or the localpart when unset. | `display_name`, `role`, and each `instructions` item |
| `{user_first_name}` | The first word of `{user_display_name}`, so `Bas Nijholt` gives `Bas`, or the localpart when the display name is blank. | `display_name`, `role`, and each `instructions` item |

If the rendered `name` or Matrix account is already taken, `{username}` becomes the slug plus `_` and the first 6 hex characters of the SHA-256 of the user ID, for example `alice_86f01a` for `@alice:example.org`.
Taken means an authored agent, team, or template key, including one saved but not yet reloaded, `router`, `user`, `_shared`, another generated agent, or an account MindRoom already manages.
A user on another server than MindRoom's always gets this hashed form, so nobody elsewhere can take a local person's plain name.
A second clash fails that user's onboarding.

Every generated agent also gets these settings, whatever the template says:

- The owner is added to `access.users` and `credential_managers` when missing.
- `access.members_of_rooms` is `[]` when missing or null, so membership of the agent's rooms grants nothing unless you list rooms yourself.
- `allow_self_config` is `false`, even when `defaults.allow_self_config` is true, because `self_config` edits `agents.<name>`, which a generated agent does not have, so owners use `update_agent` instead.

Config validation rejects `private` and `allow_self_config: true` in the template's `agent` and in any `users` override, because a personal agent already belongs to one user.
A template or override that would produce an invalid agent fails validation as soon as you save or load it, even before anyone has onboarded, with `personal_agents would generate an invalid agent for a new user:` and the reason.

## Generated Agents

Generated agents are ordinary agents in the running config, and workers receive them too.
They never appear in the saved config file, and the dashboard and `config_manager` edit only what you authored.

- Editing the template changes every user's agent at the next reload.
  The name, Matrix account, `{user_display_name}`, and `{user_first_name}` stay fixed at what onboarding stored.
  A field its owner changed with [`update_agent`](#updating-a-generated-agent) keeps the owner's value until the owner restores it.
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

## Voice Calls

Name the template key in [`calls.agents`](voice-calls.md#calls-configuration) to let every user call their own agent:

```yaml
calls:
  enabled: true
  profiles:
    openai-live:
      backend: live
      model: gpt-live-1
      credentials_service: openai-live
      voice: marin
  agents:
    mind: openai-live
```

Each generated agent then uses that profile in its owner's personal room, including agents of people onboarded after the config was loaded.
The saved config keeps `mind`, not the generated agent names.
A generated agent that `calls.agents` also names directly keeps the profile given there.
Neither the template's `agent` nor its `users` overrides may list `rooms`, because a room takes only one calls-enabled agent; config loading fails with `calls.agents names personal_agents.mind, whose agent lists rooms in the template or a user's override`.

## Onboarding Behavior

The router handles a join, a `commands` entry, or a `backfill` hit in an onboarding room like any [personal-room](personal-rooms.md#onboarding-behavior) trigger, with these differences:

1. MindRoom registers the agent's Matrix account and starts the agent without a restart; a failed registration leaves no agent behind.
2. The usual personal-room flow runs with that user's own agent once the agent has finished its first sync.
   Until then the trigger is pending, which does not hold up the onboarding room for anyone else, and the router logs `Personal agent is starting`.
3. The user keeps the agent, its account, and its room when they leave the onboarding room or are banned, and MindRoom has no command that removes them.

A [bridge alias](authorization.md#bridge-aliases) onboards onto its person's agent, which gives the alias account its own personal room.
A Matrix ID containing a space, `*`, `?`, `[`, or `]` gets no agent, because access settings cannot name it exactly.

The roster of generated agents is `tracking/personal_agents.json` in the storage directory; back it up with the storage, because an agent's name and Matrix account never change once recorded there.
It also holds the agents that agents [create](#created-agents-and-self-updates) and every owner's edits.
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
The Agents page lists every agent generated from a template, read-only, with its owner, Matrix ID, template, and whether it is active, pending, or stopped because its template was removed.
Agents that agents [create](#created-agents-and-self-updates) do not appear there.

## Switching From a Single Personal-Room Agent

When `personal_rooms.agent` changes from an agent to a template, existing personal rooms stay with the old agent, and a user's new agent cannot take over a room the old agent created.
Set a new `personal_rooms.alias_prefix` at the same time so that each user gets a new personal room with their own agent; otherwise their onboarding fails with `Personal-room alias ... which this agent does not own`.
The old agent's `access` no longer limits who gets an agent: everyone in an onboarding room gets one.

## Created Agents and Self-Updates

A template gives every person the same agent.
A person cannot narrow its tools or reword its role, and cannot get a second agent with a narrower job unless an administrator edits `config.yaml` for them.
The [`agent_manager`](tools/agent-orchestration.md#agent_manager) tool covers both, and MindRoom adds it by itself:

- Every generated agent, personal or created, can show and change its own `display_name`, `role`, `instructions`, and `tools`, and set its avatar, for its owners.
- An agent with `created_agents_per_owner` above 0 can create new agents for the people who ask it.

Every call that creates, changes, or deletes an agent waits for the person asking to approve it on a card; see [Approval Cards](#approval-cards).

### Updating a Generated Agent

Ask the agent to change itself, for example to answer more briefly or to drop a tool.
It calls `agent_settings` to read its current values and the tool names it may choose from, then `update_agent` with only the fields to change.

- Each value replaces the whole field, so send a changed `instructions` list in full.
- `null` restores the template's value, or for a created agent its creator's value.
- `tools` are toolkit names such as `calculator`, never function names, chosen from the `allowed_tools` that `agent_settings` returns, which are the tools of the template or creator.
  A chosen tool keeps the settings it has there, and so does each tool it brings along, such as `attachments` with `matrix_message`; any other tool that was not chosen is left out, even one from `defaults.tools`.
- A change that the next config load would reject is refused with `The agent would not load with this change, so nothing was stored: <reason>`, and so is a new agent from `create_agent`.
- A field with no edit follows its template or creator, so a later change to either reaches it.

To change the agent's picture, attach a PNG, JPEG, GIF, or WebP image in a conversation with the agent and ask it to use the image as its avatar.
It calls `set_avatar`, which crops the image to its center square, and the new picture applies without a reload.
An animated image keeps only its first frame.

Only the agent's owners may ask, and anyone else is refused with `Only <owners> may change <agent>`.
A personal agent has one owner, the person it was made for, and cannot be deleted.

### Letting an Agent Create Agents

Set `created_agents_per_owner` on the agent that people may ask for helpers.
It applies to that agent only, never through `defaults`, and under a template's `agent:` it lets each person's agent create agents.

```yaml
agents:
  mind:
    display_name: Mind
    role: Personal assistant
    model: sonnet
    tools: [calculator, file]
    created_agents_per_owner: 2
```

The person asks `mind` for a helper, and `mind` calls `create_agent` with a `name`, a `display_name`, a `role`, and optionally `instructions` and `tools`.

- The new agent's ID is the creator's name, an underscore, and the `name` lowercased with other characters turned into underscores, trimmed of leading and trailing underscores, and cut to 30 characters, so `Research Helper` from `mind` gives `mind_research_helper`.
  The tool replies with its Matrix ID.
- `tools` come from the creator's tools, with the creator's settings for each, and omitting `tools` gives the new agent all of them.
  Asking for a tool the creator lacks is refused with the list of names it may use.
- `instructions` replace the creator's, and omitting them keeps the creator's.
- Everything else is copied from the creator, including its model, memory backend, knowledge bases, skills, and worker settings.
- Only the person who asked, any co-owners they add, and platform administrators may use the agent, and only the owners may invite it.
  Its `credential_managers` stay the creator's, because an unscoped agent shares its creator's credentials and OAuth connections; with a `user` or `user_agent` worker scope, each owner still manages their own connections.
  It starts with no rooms and no delegation, takes its automations from `defaults`, gets no `self_config` from `allow_self_config`, and cannot create agents of its own.

The new agent joins no room by itself.
The owner invites its Matrix ID to a room to talk to it, and the agent accepts because the invitation comes from an owner.
Authored config cannot reference a created agent, so a `delegate_to` list or a team that names one fails to load.
A change to a created agent through `self_config` or `config_manager` is refused with `agents.<name> was created by another agent; its owners change it with update_agent`, because saving it to `config.yaml` would lose it.

> [!WARNING]
> **A created agent has its creator's tools, knowledge bases, skills, and model.**
> Anyone who may ask the creator can make copies of it up to the cap, and a copy can drop tools but never add one.
> Set `created_agents_per_owner` only on agents whose whole configuration you would hand to each person who may ask them.
> A copy's access is its owners, not the creator's `access`.
> An owner can add any Matrix ID as a co-owner, including someone on another server who may not ask the creator, and a copy keeps working for its owners after they lose access to the creator.
> To take one copy away, stop MindRoom and remove its entry from `created` in `tracking/personal_agents.json`; setting the cap to 0 or removing the creator stops every copy of that creator.

The cap counts each person's agents from one creator, including one still being created.
A person at the cap is refused with `<owner> already has <n> agents from <creator>, which may create <cap> per owner`, and deleting an agent frees a place.
A name that is already taken is refused with `<name> is taken; choose another name`.
A name stays taken after the agent is deleted, because its Matrix account stays.

### Owners and Co-Owners

The person whose request creates an agent is its owner, resolved through any [bridge alias](authorization.md#bridge-aliases) to the person.
Only a person can own an agent, so a MindRoom account is refused, and so is a Matrix ID with `[`, `]`, a space, `*`, or `?`.

An owner can share a created agent by passing `co_owners` to `update_agent`, and passing `null` clears them.
Co-owners may use the agent, invite it, change its `display_name`, `role`, `instructions`, and `tools`, and set its avatar.
Only the owner may change `co_owners` or delete the agent with `delete_agent`.

### Approval Cards

`create_agent`, `update_agent`, `set_avatar`, and `delete_agent` each show an [approval card](tool-approval.md#approving-and-denying) to the person asking, whatever `tool_approval` says, because the agent asking may have read text its owner did not write.
The card shows the exact arguments, and only that person can approve it.
The 5, 10, and 30 minute options on a card are never offered for these calls, but the person asking can skip their cards in a thread with [`!auto_approve`](tool-approval.md#auto-approve-everything).
`agent_settings` only reads and asks for no approval of its own, so it shows a card only when a `tool_approval` default or rule requires one.

In a room without the router the call is denied with `Tool approval needs the router in this room`.
The agent then calls `invite_router` and retries, as described in [When the Router Is Missing](tool-approval.md#when-the-router-is-missing).

### Changing the Creator Later

A created agent is built again from its creator at every config load, so changing the creator changes its agents.

- A tool removed from the creator leaves the created agents that chose it, and the load logs `generated_agent_tool_dropped`.
  The choice stays stored, so the tool returns if the creator gets it back.
- Lowering `created_agents_per_owner` keeps each person's oldest agents up to the new cap and stops the rest with `created_agent_skipped` and `reason=over_cap`.
  Setting it to 0 stops them all and removes `create_agent`.
- Removing the creator stops its agents with `reason=creator_missing`.
- Stopped agents keep their roster entries and Matrix accounts, so restoring the creator or raising the cap starts them again.
  A stopped agent leaves every room except its direct messages, so an owner must invite it back into those rooms.
- Changing the creator's `access` does not change who may use its created agents; see the warning in [Letting an Agent Create Agents](#letting-an-agent-create-agents).
- Saving an agent, team, or template under a created agent's name from the dashboard or `config_manager` is refused with `<section>.<name> collides with the agent <creator> created for <owner>`.
  One added to `config.yaml` by hand keeps the name, and the created agent stops with `reason=name_taken`.
  The authored agent then logs in with the created agent's Matrix account, so it joins that agent's rooms and uses its memory and workspace, and every later dashboard or `config_manager` save is refused until one of the two is renamed or the roster entry is removed.
- A created agent copies a personal creator as its template defines it, so the owner's edits to that creator do not carry over, and the tools it may give come from the template.

## Troubleshooting Personal Agents

A failure affects only that user's onboarding.
Failed onboarding retries after 30 seconds, then at doubling intervals up to one hour, and a config reload retries it immediately.

| Log message | Meaning |
| --- | --- |
| `Personal agent is starting` | Normal: the user's agent was just created or has not finished its first sync, and the trigger is retried when it has. |
| `Personal-room onboarding trigger failed` | Onboarding one user failed. For a personal agent, `error` is usually the homeserver refusing the registration, `No free personal agent name for <user>` when the plain and the hashed names are both taken, `<user> cannot own an agent: brackets in a Matrix ID are not supported` for an ID with `[` or `]`, `<user> cannot own a personal agent: <reason>` for another unsupported Matrix ID such as one with a space, `*`, or `?`, or `<user> is a MindRoom account and cannot own a personal agent`. |
| `Personal-room reconciliation failed` | A retry failed, with the attempt number and the delay before the next try. |
| `Personal agent roster unavailable` | The roster file cannot be read, and the pass is retried. |
| `created_agent_skipped` | A created agent did not load; `reason` is `pending` while it is still being created, or until the next `create_agent` removes one a crash interrupted, `creator_missing`, `over_cap`, or `name_taken`. |
| `generated_agent_tool_dropped` | A tool an owner chose is no longer among the tools of the template or creator, so the agent loads without it. |
| `generated_agents_invalid` | The config with the owners' edits and the created agents is invalid for the reason in `error`, so MindRoom ignores them all until it is fixed, and each personal agent runs from its template. |

- A refused registration usually means the localpart is already held by another account, or, with `MATRIX_MANAGED_ACCOUNT_AUTH=appservice`, that a custom `matrix_username` is outside the application service's exclusive user namespace (see [Agent Users](matrix.md#agent-users)).
  Make the namespace cover the rendered name, including the `_<MINDROOM_NAMESPACE>` suffix, or change the pattern.
- Each user adds a Matrix account, a sync loop, and an encryption device.
  That is fine for dozens of users and heavy for hundreds.
