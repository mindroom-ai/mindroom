---
icon: lucide/shield
---

# Authorization

This page explains who may talk to agents, teams, and the router, who may administer MindRoom and manage credentials, and who may open the dashboard.
Use it to give people access to an agent, to work out why an agent ignored someone, or to map bridged identities to one person.

<video controls playsinline preload="metadata" aria-label="New colleagues are invited to a room, and the agent answers everyone in it" style="width: 100%" poster="https://github.com/user-attachments/assets/83b9f414-40f7-4fe5-8f1d-62304b641b87" data-poster-light="https://github.com/user-attachments/assets/83b9f414-40f7-4fe5-8f1d-62304b641b87" data-poster-dark="https://github.com/user-attachments/assets/d4b50091-0d28-4185-8ecb-3f2615542e74">
  <source src="https://github.com/user-attachments/assets/83081f1a-9caf-476a-b147-3c5f93762471" type="video/mp4" media="(prefers-color-scheme: dark)">
  <source src="https://github.com/user-attachments/assets/5eb23908-4555-42ca-8b38-a2636097339b" type="video/mp4">
</video>

See also [Rooms & Spaces](rooms.md), [Routing & Responder Selection](configuration/router.md), and [Threads, Replies & Participation](configuration/threads.md).

## Authority at a glance

Each kind of authority has its own setting, and no setting grants another kind.

| Question | Decided by |
| --- | --- |
| Who may make the router, an agent, or a team join a room? | That entity's `accept_invites` ([agents](configuration/agents.md), [teams](configuration/teams.md), [router](configuration/router.md)) |
| Who receives automatic invitations and Matrix room admin power? | `room_defaults` and `rooms.<key>.invite_users` / `admins` ([Room policy](rooms.md#room-policy)) |
| Who may converse with a responder? | That responder's [`access`](#responder-access) |
| Who may run administrative commands and manage any agent's credentials? | `administrators` |
| Who may manage one agent's shared credentials and OAuth connections? | `administrators` and `agents.<name>.credential_managers` |
| Who may open and change the dashboard? | [Deployment authentication](#dashboard-configuration) |
| May a tool action run? | Tool availability plus any [tool approval](tool-approval.md) rule |

Joining a room grants no conversation access, and conversation access grants no credential management.
Administrators are not invited automatically and get no Matrix room power.
Room admins are not platform administrators or credential managers.
Credential managers get no conversation access.

## Configuration

```yaml
administrators:
  - "@owner:example.com"

rooms:
  engineering:
    display_name: Engineering
    invite_users:
      - "@engineer:example.com"

agents:
  code:
    display_name: Code
    rooms: [engineering]
    accept_invites:
      - "@owner:example.com"
    access:
      current_room_members: false
      members_of_rooms: [engineering]
      users:
        - "@contractor:example.com"
    credential_managers:
      - "@credential-owner:example.com"

router:
  access:
    current_room_members: true

authorization:
  config_command_enabled: false
  aliases:
    "@owner:example.com":
      - "@telegram_owner:example.com"
```

| Field | Type | Default | Description |
| --- | --- | --- | --- |
| `administrators` | list of Matrix user IDs | `[]` | Platform administrators; concrete IDs only, no wildcards |
| `agents.<name>.credential_managers` | list of Matrix user IDs | `[]` | Users who may manage that agent's shared credentials and OAuth connections; concrete IDs only, no wildcards |
| `<responder>.access` | object or null | `null` | Conversation access for an agent, team, or the router; see [Responder access](#responder-access) |
| `authorization.config_command_enabled` | bool | `false` | Enables [`!config`](chat-commands.md#config) for administrators |
| `authorization.aliases` | map of Matrix user ID to list of IDs | `{}` | Bridge identities treated as one person; see [Bridge aliases](#bridge-aliases) |
| `bot_accounts` | list of Matrix user IDs | `[]` | Non-MindRoom bots; see [Bot accounts](#bot-accounts) |

## Responder access

`access` on an agent, team, or the router has three clauses, and a requester is allowed when any one matches.

- `current_room_members` (bool): allow joined members of the room the message is in.
- `members_of_rooms` (list of managed room keys): allow joined members of any listed managed room.
- `users` (list): allow these Matrix user IDs or glob patterns.

Defaults:

- For agents and teams, omitting `access` or its `members_of_rooms` grants members of the responder's own managed `rooms`.
- An explicit `members_of_rooms: []` turns that inferred grant off.
- Only managed room keys grant membership; raw Matrix room IDs or aliases in `rooms` grant nothing, and `members_of_rooms` entries that are not managed room keys fail validation.
- The router defaults to `current_room_members: true`.

Platform administrators and MindRoom's own internal identities pass every responder's `access` check.
[Bridge aliases](#bridge-aliases) are resolved before matching.
The same check covers every way of reaching a responder: messages, media, calls, reactions, approvals, external triggers, background scripts, delegation, attachments, and scheduled resumes.

A team's `access` covers requests to the team as a whole, so someone the team admits reaches every member agent through the team, even members whose own `access` would not admit them directly.

### Room membership grants

Only joined members count: a pending invitation does not, and leaving, being kicked, or being banned removes the grant.
When MindRoom cannot confirm membership of a referenced room, the grant does not apply.
The router tracks membership, so it must be joined to a room before `current_room_members` can allow anyone there.
In an ad-hoc room an agent joined first, have the agent use its `invite_router` tool (see [When the Router Is Missing](tool-approval.md#when-the-router-is-missing)) and retry after the router joins.

### Invitations

`accept_invites` decides only whether an entity joins a room it is invited to.
Accepting an invitation never grants permission to interact; every later message still goes through `access`.
An entity that accepts an invitation to a configured room it is not listed for in `rooms` does not answer there; when mentioned, it replies that it is not configured for this room.

### Agents mentioning other agents

When an agent or team reply, including a message an agent sends with `matrix_api`, mentions another agent or team, the mentioned entity acts for the person or configured bot account who requested the reply.
It responds only when its own `access` admits that requester, and it uses the requester's credentials, private instances, memory, learning, and approvals.
A mention wakes the mentioned entity only after the reply finishes (not when it was stopped or failed) and only while the requester is a joined member of the room.
Agents stop waking each other once a conversation has [`defaults.max_consecutive_agent_replies`](configuration/agents.md#defaults) consecutive agent or team messages since a person last wrote there, and resume after the next message from a person.
In a room-level conversation, the count covers the room's messages outside threads.
Commands in an agent's reply are limited as described in [Command Handling](chat-commands.md#command-handling), and a scheduled task's text never runs as a command.

## Requester identity and private state

MindRoom resolves [bridge aliases](#bridge-aliases) first, so a person reaching an agent through any configured alias gets the same conversations, private state, requester-scoped credentials, approvals, triggers, scripts, and usage.

An agent's `private` setting (see [Private Instances](configuration/agents.md#private-instances)) only decides whose state an interaction uses.
It does not grant access; MindRoom checks the agent's `access` before picking a requester's private instance.

## Bridge aliases

`authorization.aliases` maps bridge-created Matrix IDs to one canonical Matrix user before access, administrator, and credential checks.

```yaml
authorization:
  aliases:
    "@alice:example.com":
      - "@telegram_123:example.com"
      - "@signal_456:example.com"
```

Aliases apply only to people; managed agents, teams, the router, `bot_accounts`, and MindRoom's internal account are never remapped.
Each alias may appear only once, and a canonical ID cannot also be another user's alias.

Upgrading from the retired access fields is covered in [Membership Access Migration](deployment/upgrades.md#membership-access-migration).

## Bot accounts

`bot_accounts` lists non-MindRoom bots, such as bridge bots, that MindRoom treats like agents rather than people when deciding whether to respond, so they do not trigger [multi-human thread protection](configuration/threads.md#bot-accounts).
They still need `access` to talk to a responder.
A bot account's message that the router hands to an agent, or a task it scheduled, runs as that bot account, so the agent and every agent or team its reply mentions apply their own `access` to it.

```yaml
bot_accounts:
  - "@telegram_bot:example.com"
  - "@slack_bot:example.com"
```

## Dashboard configuration

Dashboard access follows deployment authentication, not the Matrix `administrators` list.

- Standalone: `MINDROOM_API_KEY` protects the dashboard and its API; without it, access is unauthenticated, so set it before exposing an instance beyond a trusted local network.
- Hosted (Supabase): the user's platform sign-in is validated, and the instance owner account is enforced when configured.
- [Trusted upstream auth](deployment/trusted-upstream-auth.md): every gateway-authenticated user may change configuration unless Connections is enabled, which adds an `administrators` check; see its [Security Boundary](deployment/trusted-upstream-auth.md#security-boundary).

Standalone deployments should set `MINDROOM_OWNER_USER_ID` so dashboard requests made with the API key act as the owner's Matrix identity for credential management.
Browser changes made with a dashboard sign-in or trusted upstream identity must come from the dashboard's own origin; see [Browser Mutation Protection](deployment/trusted-upstream-auth.md#browser-mutation-protection).

### Unauthenticated dashboard host names

Without a credential, the dashboard and the `/v1` API answer only requests addressed to `localhost`, a `*.localhost` name, an IP address, the hosts of `MINDROOM_PUBLIC_URL`, `MINDROOM_BASE_URL`, `MINDROOM_URL`, or `MINDROOM_SCRIPT_GATEWAY_URL`, or a host listed in `MINDROOM_DASHBOARD_ALLOWED_HOSTS`.
This blocks DNS-rebinding attacks from other web pages.
Any other name, such as `myserver.local`, fails with `Host '<host>' is not allowed without a credential; add it to MINDROOM_DASHBOARD_ALLOWED_HOSTS or configure authentication`.
Browser pages may call it only when served from the same host, from a `localhost` name or loopback address, or from a host set through the URL settings above or `MINDROOM_DASHBOARD_ALLOWED_HOSTS`.
Pages from any other origin fail with `Origin '<origin>' is not allowed without a credential`, and changes requested from another site fail with `Cross-site browser requests are not allowed without a credential`.

A reverse proxy in front of an unauthenticated dashboard or `/v1` API must pass the browser's `Host` header through unchanged, for example nginx `proxy_set_header Host $host;` or Apache `ProxyPreserveHost On`.
A proxy that rewrites `Host` to its upstream address, as nginx `proxy_pass` and Apache `mod_proxy` do by default, makes every request look addressed to an IP address and disables this protection; in that case set `MINDROOM_API_KEY`, and `OPENAI_COMPAT_API_KEYS` for `/v1`.

These host checks do not apply to requests authenticated by an API key, a platform session, or trusted upstream auth, or to routes with their own authorization, such as health probes, webhooks, computer sessions, and `/v1` with `OPENAI_COMPAT_API_KEYS`.

## Platform and credential authority

Platform administrators may:

- Run `!reload-plugins`, and `!config` when `authorization.config_command_enabled` is `true` (see [Chat Commands](chat-commands.md#config)).
- Manage any agent's credentials and the deployment-wide OAuth client configuration.
- Talk to every responder regardless of its `access`.

A credential manager may manage only the named agent's shared credentials and OAuth connections.
Requesters manage their own personal OAuth connections without being listed anywhere; see [Who Can Connect An Account](oauth-framework.md#who-can-connect-an-account).
Reading or changing an agent's shared credentials returns HTTP 403 to anyone who is neither an administrator nor that agent's credential manager.
Connections may show users with agent access whether a shared connection exists, without revealing the connected account or granting management.

## Tool approval and resource ownership

Conversation access lets a requester ask a responder to act, but the responder must still have the tool, and any required approval must still succeed.
An approval belongs to the person who started the action, and MindRoom rechecks that person's current access when it is approved.

Schedules belong to their room, while external triggers, background scripts, private workers, and requester-scoped credentials belong to the requester.
Attachments are scoped to their room and thread, so an authorized response may read attachments other participants uploaded in that conversation.
None of these ownership rules grants responder access.
