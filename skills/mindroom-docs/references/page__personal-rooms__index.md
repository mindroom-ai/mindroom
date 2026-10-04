# Personal Rooms

The optional `personal_rooms` section creates one private, unlisted room for each eligible human who joins an onboarding room.
The router watches the onboarding rooms; the selected agent does not need to join them.

```yaml
rooms:
  lobby: {}
agents:
  helper:
    display_name: Helper
    rooms: []
    access:
      members_of_rooms: [lobby]
personal_rooms:
  agent: helper
  onboarding_rooms: [lobby]
  commands: ["!personal"]
  welcome_dispatch: true
```

`personal_rooms` is an object or `null` (the default, which disables onboarding).
When enabled, its fields are:

| Field | Type | Default | Meaning |
| --- | --- | --- | --- |
| `agent` | string | Required | Configured agent that owns and answers in personal rooms. |
| `onboarding_rooms` | list of strings | Required | Nonempty list of configured onboarding rooms observed by the router. |
| `commands` | list of strings | `[]` | Exact self-onboarding commands beginning with `!`, without whitespace or arguments. |
| `alias_prefix` | string | `"personal"` | Lowercase alias prefix, matching `[a-z0-9_-]{1,40}`. |
| `name` | string | `"Personal room for {user}"` | New room name template, up to 1,000 characters. |
| `topic` | string | `"Private conversation with {agent}."` | New room topic template, up to 1,000 characters. |
| `welcome` | string | `"Welcome {user}! This is your personal room with {agent}."` | Welcome template, up to 10,000 characters; empty disables new welcomes. |
| `welcome_dispatch` | boolean | `false` | Send the welcome to the selected agent as a trusted message on the human's behalf after the human joins, so the agent starts the conversation; otherwise send it as a notice. |
| `confirmation` | string | `""` | Optional once-only notice in the original onboarding room, visible to everyone there, up to 10,000 characters. |
| `backfill` | boolean | `false` | Also onboard current eligible onboarding-room members on startup and config reload. |
| `auto_join_requester` | boolean | `false` | Use the homeserver admin API to join the requester into a newly created personal room. |
| `requester_admin` | boolean | `false` | Grant the human Matrix room administration; otherwise the agent owns room state. |
| `avatar` | string or `null` | `null` | Room avatar file, relative to the configuration; takes priority over `avatar_from_requester`. |
| `avatar_from_requester` | boolean | `false` | Copy the human's profile avatar into the room when no avatar file is configured. |

Templates support `{user}` (full Matrix user ID), `{room}` (room alias), and `{agent}` (display name).
Aliases are built from `alias_prefix`, a hash of the user ID, and the installation namespace.
Room avatars are set only when the room has none.

## Onboarding Behavior

- A human joining an onboarding room, or sending one of the `commands` there, onboards only that human.
- Existing agent access rules still apply, and agent and service accounts are excluded.
- `auto_join_requester: true` requires a source Matrix account allowed to use the homeserver's Synapse-compatible admin join API.
  It never force-joins someone who already joined, left, or was banned, and does not apply to existing or imported rooms.
- A requester who left or was banned from their personal room is not re-added by restarts or backfill; a fresh join of the onboarding room may re-invite them to a newly created personal room, but never overrides a ban.
- The owner may invite other MindRoom agents into their personal room, and an invited agent sees only messages sent after its invite.
  Anyone else invited into a room MindRoom created is removed with a notice explaining why.
- The welcome is delivered once, and its text and dispatch mode are fixed when the room is created, so later template changes do not affect pending welcomes.
  Disabling `personal_rooms` or revoking the human's access prevents a pending welcome.
- Personal rooms are kept across restarts and room cleanup, including after the feature is disabled, and disabling onboarding does not revoke existing access.

## Troubleshooting Personal Rooms

Onboarding failures affect only that requester, and the onboarding room keeps serving everyone else.
Failed rooms are retried automatically after delays that start at 30 seconds and double up to one hour, and a configuration reload retries them at once.
A join or command that arrives before the personal-room agent connects waits until it does.

| Log message | Meaning |
| --- | --- |
| `Personal-room validation failed for an onboarding trigger` | An existing personal room failed ownership or membership checks, for example because another account already holds the alias, or a guest the agent cannot remove was raised to the agent's power level. |
| `Personal-room imported roster has unattested members` | An imported room contains members that its seed record does not list; the warning names them, and the room is not changed. |
| `Personal-room onboarding trigger failed` | Any other failure, such as an invite refused by the requester's server. |

## Operator-seeded Existing Rooms

Operators can adopt an existing room, keeping its room ID and history, by writing a trusted `PersonalRoomRecord` before enabling onboarding.

1. Stop the runtime.
2. In the room, have the agent author an `org.mindroom.personal_room` state event with an empty state key and exactly `{"user_id": "<requester>", "agent_user_id": "<agent>"}` as content.
3. Make sure the target agent is joined with room admin power, the room's directory visibility is private, and its join rule is invite-only.
4. Write the record with `personal_room_record_path(runtime_paths, agent_name, user_id)` and `write_personal_room(path, record)` from `mindroom.matrix.personal_room_store`.

For a private room with shared history and one already permitted guest, a record looks like this:

```json
{
  "user_id": "@alice:example.test",
  "alias": "#personal-alice:example.test",
  "source_room_id": "!lobby:example.test",
  "room_id": "!existing:example.test",
  "welcome_completed": true,
  "adoption": {
    "creator_user_id": "@router:example.test",
    "agent_user_id": "@helper:example.test",
    "router_user_id": "@router:example.test",
    "expected_history_visibility": "shared",
    "additional_user_ids": ["@guest:example.test"]
  }
}
```

- Adoption requires the exact room ID; an alias alone never authorizes adoption.
- `creator_user_id` must match the room's creator, and `agent_user_id` must match the selected agent's Matrix identity.
- `router_user_id` (optional) lets this installation's router account stay in the room, including during ordinary cleanup after onboarding is disabled.
- `expected_history_visibility` must exactly match the room's current `invited`, `joined`, or `shared` history setting and defaults to `invited`; `world_readable` is never accepted.
- `additional_user_ids` (default `[]`) lists existing participants who may be joined, invited, or knocking.
  It only permits them; it does not invite anyone or grant power.
  No other members besides the requester, agent, seeded router, and these users are allowed.
- Set `welcome_completed: true` only when onboarding is already complete, and omit pending welcome content from completed imports.

Normal authorization and current onboarding-room membership still apply.
Reconciliation fails if the room's history visibility or membership no longer matches the record, and MindRoom never rewrites the room's name, topic, or history.
