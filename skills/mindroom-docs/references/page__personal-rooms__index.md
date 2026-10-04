# Personal Rooms

The optional `personal_rooms` section gives each eligible human a private, unlisted room with one selected agent, created when they join an onboarding room such as a lobby or send a configured command there.

## Setup

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

The router watches the onboarding rooms, so the selected agent does not need to join them.
The human must still be allowed to use the agent under its access rules; listing the onboarding rooms in `access.members_of_rooms`, as above, grants that (see [Responder access](https://docs.mindroom.chat/authorization/#responder-access)).

## Configuration

`personal_rooms` is an object, or `null` (the default) to disable onboarding.

| Field | Type | Default | Meaning |
| --- | --- | --- | --- |
| `agent` | string | Required | Configured agent that owns and answers in personal rooms. |
| `onboarding_rooms` | list of strings | Required | Nonempty list of configured rooms whose human joins trigger onboarding. |
| `commands` | list of strings | `[]` | Exact self-onboarding commands accepted in onboarding rooms; each starts with `!` and contains no whitespace or arguments. |
| `alias_prefix` | string | `"personal"` | Room alias prefix matching `[a-z0-9_-]{1,40}`, followed by a hash of the user ID and the installation namespace. |
| `name` | string | `"Personal room for {user}"` | New room name template, up to 1,000 characters. |
| `topic` | string | `"Private conversation with {agent}."` | New room topic template, up to 1,000 characters. |
| `welcome` | string | `"Welcome {user}! This is your personal room with {agent}."` | Welcome message template, up to 10,000 characters; empty disables the welcome. |
| `welcome_dispatch` | boolean | `false` | Send the welcome to the agent as a trusted message on the human's behalf once the human joins, so the agent starts the conversation; otherwise post it as a notice right away. |
| `confirmation` | string | `""` | Optional one-time notice in the onboarding room where the human was onboarded, visible to everyone there, up to 10,000 characters. |
| `backfill` | boolean | `false` | Also onboard current eligible members of the onboarding rooms on startup and config reload. |
| `auto_join_requester` | boolean | `false` | Join the human into a newly created personal room instead of only inviting them. |
| `requester_admin` | boolean | `false` | Grant the human Matrix room admin; when `false`, no admin is granted and existing power levels, including admin granted earlier, stay unchanged. |
| `avatar` | string or `null` | `null` | Room avatar file, relative to the configuration file; takes priority over `avatar_from_requester`. |
| `avatar_from_requester` | boolean | `false` | Copy the human's profile avatar into the room when `avatar` is not set. |

Templates accept only `{user}` (full Matrix user ID), `{room}` (room alias), and `{agent}` (agent display name); any other placeholder fails config validation.
Avatars are set only on rooms that have none.
The welcome is sent once, and changing `welcome` or `welcome_dispatch` later does not change a welcome that is already pending.

`auto_join_requester: true` requires the router's Matrix account to be allowed to use the homeserver's Synapse-compatible admin join API.
It applies only when MindRoom creates the room, and never joins someone who already joined, left, or was banned.

## Onboarding Behavior

- Joining an onboarding room, or sending one of the `commands` there, onboards only that human.
- MindRoom's own router, agent, and team accounts, accounts listed in `bot_accounts`, and the internal `mindroom_user` account never get personal rooms.
- A human who leaves or is banned from their personal room is not re-added by restarts or backfill.
  Leaving and rejoining an onboarding room re-invites them to their existing personal room, except after a ban or for an adopted room.
- In a personal room MindRoom created, the owner may invite other MindRoom agents, and an invited agent sees only messages sent after its invite.
  Anyone else invited into such a room is removed with a notice explaining that the room is private.
  Adopted rooms instead allow only the participants listed in their record.
- Personal rooms survive restarts and room cleanup, even after `personal_rooms` is removed.
  Disabling the feature stops new onboarding but does not remove existing rooms or access.

## Troubleshooting Personal Rooms

An onboarding failure affects only that human, and the onboarding room keeps working for everyone else.
Failed onboarding is retried automatically, first after 30 seconds and then at doubling intervals up to one hour, and a config reload retries it immediately.
A join or command that arrives before the personal-room agent connects is handled once it does.
Revoking the human's access to the agent, or disabling `personal_rooms`, cancels a welcome that has not been sent yet.

| Log message | Meaning |
| --- | --- |
| `Personal-room validation failed for an onboarding trigger` | An existing personal room failed ownership or membership checks, for example because another account already holds the alias, or a guest the agent cannot remove was raised to the agent's power level. |
| `Personal-room imported roster has unattested members` | An adopted room contains members its record does not list; the warning names them, and MindRoom does not change the room. |
| `Personal-room onboarding trigger failed` | Any other failure, such as an invite refused by the human's homeserver. |
| `Personal-room reconciliation failed` | An automatic retry failed; the log shows the attempt number and the delay before the next try. |

## Adopting an Existing Room

Operators can turn an existing room into someone's personal room, keeping its room ID and history.

1. Stop the runtime.
2. Have the selected agent send an `org.mindroom.personal_room` state event in the room with an empty state key and exactly `{"user_id": "<requester>", "agent_user_id": "<agent>"}` as content.
3. Make sure the agent is joined with room admin power, the room is not published in the room directory, and its join rule is invite-only.
4. Write the record with `personal_room_record_path(runtime_paths, agent_name, user_id)` and `write_personal_room(path, record)` from `mindroom.matrix.personal_room_store`.

For a room with shared history and one existing guest, a record looks like this:

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

- `room_id` is required; an alias alone never adopts a room.
- `source_room_id` is the onboarding room, and the requester must still be a member of it and allowed to use the agent.
- `creator_user_id` must match the room's creator, and `agent_user_id` must match the selected agent's Matrix ID.
- `router_user_id` (optional) must be this installation's router account and lets it stay in the room, including during room cleanup after onboarding is disabled.
- `expected_history_visibility` is `invited` (default), `joined`, or `shared` and must match the room's current setting; `world_readable` rooms cannot be adopted.
- `additional_user_ids` (default `[]`) lists the only other participants allowed to stay joined, invited, or knocking; it does not invite anyone or grant power.
- Set `welcome_completed: true` to skip the welcome; otherwise MindRoom sends the configured welcome.

If the room's history setting or members stop matching the record, onboarding for that human fails with one of the log messages above until someone fixes the room.
MindRoom does not change an adopted room's name, topic, or history setting, and never removes anyone from it.
It invites the requester if they are not in the room yet and applies the configured `requester_admin`, avatar, and welcome settings.
