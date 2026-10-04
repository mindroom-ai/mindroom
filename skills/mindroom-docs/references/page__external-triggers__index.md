# External Triggers

External triggers let a process outside MindRoom wake an agent or team with one HTTP request, without keeping an agent turn alive while it waits.

Use them for watchers that detect a meaningful change, such as a campground site opening or a Git branch moving, and for background tasks that should report back when they finish.

A watcher sends a signed event to `POST /api/triggers/<trigger_id>`, and MindRoom posts a Matrix message that mentions the target agent or team in the target room.
The message contains the event's `title` and `message`, followed by its `data` as a JSON block.
MindRoom does not run watcher code or poll external systems; the watcher decides when to send.

There are two kinds of triggers:

- Reusable signed triggers, created with the `external_trigger_manager` tool, authenticate each request with an Ed25519 signature.
- Single-use [agent callbacks](#agent-callbacks), created with the `callback_manager` tool, give a background task a Bash/curl script that wakes the originating agent and thread once.

Triggers are created and managed by tool calls in a live Matrix conversation, not in `config.yaml`.
Trigger records are stored in the primary runtime's control state, `MINDROOM_CONTROL_STATE_PATH` when set and `control_state/` under the storage root otherwise.
Agent workers and sandbox runners do not receive that path.

## Configuration

Add `external_trigger_manager` to agents that may create triggers.

```yaml
agents:
  ops:
    display_name: Ops
    role: Watch external systems and report actionable changes.
    model: default
    rooms: [lobby]
    tools:
      - external_trigger_manager

external_trigger_policy:
  enabled: true
  max_triggers_per_owner: 20
  admin_users:
    - "@admin:example.org"
```

`external_trigger_policy` is optional; every field has a default.

| Field | Type | Default | Valid values | Meaning |
| --- | --- | --- | --- | --- |
| `enabled` | bool | `true` | | When `false`, every trigger endpoint, including callbacks, answers not found, and `callback_manager` refuses to mint callbacks. |
| `default_replay_window_seconds` | int | `300` | 30 to 3600 | Signature age accepted by new signed triggers that do not set their own window. |
| `max_replay_window_seconds` | int | `3600` | 30 to 3600 | Cap on any trigger's replay window. |
| `default_max_body_bytes` | int | `65536` | 1024 to 262144 | Request body limit for new triggers that do not set their own. |
| `max_body_bytes` | int | `262144` | 1024 to 262144 | Cap on any trigger's body limit. |
| `max_triggers_per_owner` | int | `20` | 1 to 1000 | Trigger records one owner may hold, including unused callbacks. |
| `admin_users` | list of Matrix IDs | `[]` | | Trigger-only administrators. |

Each default must not exceed its matching cap.
Lowering a cap also limits existing triggers.

Top-level `administrators` and `admin_users` can list, enable, disable, rotate, and delete any owner's triggers, and can target other agents, teams, and rooms.
`admin_users` grants only this trigger authority, not wider administrator rights.
Both lists match canonical identities after `authorization.aliases` resolution.

To stop users from creating triggers without review, add [tool approval](https://docs.mindroom.chat/tool-approval/) rules for `create_trigger`, `rotate_trigger_key`, `disable_trigger`, and `delete_trigger`.

## Signed Trigger Setup Flow

1. Generate a signing key where the watcher runs.

   ```bash
   mindroom trigger keygen --private-key-file /etc/mindroom/triggers/campground.key
   ```

   The command prints `private_key`, `public_key`, and `public_key_fingerprint`.
   Keep the private key only in the watcher runtime.

2. In a Matrix conversation with the agent, give it the printed `public_key` and ask it to call `external_trigger_manager.create_trigger`.

   ```json
   {
     "trigger_id": "campground",
     "public_key": "BASE64_PUBLIC_KEY_FROM_KEYGEN",
     "key_id": "campground-main",
     "description": "Campground availability watcher",
     "allowed_kinds": ["campground.availability"],
     "replay_window_seconds": 300,
     "max_body_bytes": 65536
   }
   ```

   The tool returns the endpoint path `/api/triggers/<trigger_id>` and the public key fingerprint, never key material.

3. Have the watcher send events with `mindroom trigger send` and the same `key_id`, as shown in [Sending Events](#sending-events).

### `create_trigger` arguments

| Argument | Default | Meaning |
| --- | --- | --- |
| `trigger_id` | required | Endpoint name; ASCII letters, digits, `_`, and `-` only. |
| `public_key` | required | Ed25519 public key as raw base64, an OpenSSH `ssh-ed25519 ...` line, or PEM. |
| `key_id` | `default` | Key ID the sender must present. |
| `description` | `""` | Human-readable purpose. |
| `target_agent` | current agent | Agent or team to wake; only administrators can choose another. |
| `target_room_id` | current room | Room to post in; only administrators can choose another. |
| `target_thread_id` | none | Post every delivery into this existing thread. |
| `new_thread` | `false` | Post each delivery as a new room-level message so the agent answers in a fresh thread with a fresh session. |
| `allowed_kinds` | any | Accepted `kind` values; others are rejected. |
| `replay_window_seconds` | policy default | Maximum signature age, capped by policy. |
| `max_body_bytes` | policy default | Request body limit, capped by policy. |

`target_thread_id` and `new_thread` are mutually exclusive.
Without either, deliveries post to the room's main timeline.

The requester who asks for the trigger becomes its owner and must be a human user, not a bot account.
An administrator who creates a trigger for another agent or room still owns it.
Shared agents and teams can only be targeted in rooms already configured for them.
A [private agent](https://docs.mindroom.chat/configuration/agents/#private-instances) creating its own trigger may target its current room even when that room is not listed in `rooms`.
Triggers never create rooms or make agents join rooms.

### Managing triggers

The `external_trigger_manager` tool also provides these functions, available to the trigger owner and administrators:

- `list_triggers` lists the requester's triggers, or every trigger for administrators.
- `disable_trigger(trigger_id, enabled=False)` disables a trigger; pass `enabled=True` to re-enable it.
- `rotate_trigger_key(trigger_id, public_key, key_id)` replaces the signing key.
- `delete_trigger(trigger_id)` removes the trigger.

## Sending Events

Send a signed event when the watcher detects a real change.

```bash
mindroom trigger send campground \
  --url http://127.0.0.1:8765 \
  --key-file /etc/mindroom/triggers/campground.key \
  --key-id campground-main \
  --kind campground.availability \
  --event-id reserveamerica:yosemite:site-42:2026-07-04 \
  --title "Campground site opened" \
  --message "Site 42 is available for July 4." \
  --data-json '{"campground":"Yosemite","site":"42","date":"2026-07-04"}'
```

`--url` defaults to `MINDROOM_URL`, then `http://127.0.0.1:8765`.
See [`mindroom trigger send`](https://docs.mindroom.chat/cli/#trigger-send) for every option.
Use `--no-verify-tls` only for local development against a trusted endpoint.

The JSON request body has a required non-empty `kind` and `message`, and optional `event_id`, `title`, `thread_key`, and `data` object; other fields are rejected.

### Signing requests without the CLI

A watcher can sign requests itself with the Ed25519 private key.
Sign the UTF-8 string below, joined with `\n`, where `<path>` is the request path such as `/api/triggers/campground`, `<timestamp>` is Unix seconds, `<nonce>` is a fresh random string, and the last line is the lowercase hex SHA-256 of the exact body bytes.

```text
MINDROOM-TRIGGER-V1
POST
<path>
<timestamp>
<nonce>
<sha256-hex-of-body>
```

Send the base64 signature with the headers `X-MindRoom-Trigger-Key-Id`, `X-MindRoom-Trigger-Timestamp`, `X-MindRoom-Trigger-Nonce`, and `X-MindRoom-Trigger-Signature`.
The timestamp must not be in the future and must be within the trigger's replay window.

## Grouping Deliveries Into One Thread

On a `new_thread` trigger, every delivery opens a new thread.
That suits independent events but is noisy for a conversation that arrives as many events, such as replies in one support ticket.

Pass the same `thread_key` to group deliveries: the first delivery with a key opens a thread, and later deliveries with that key post into it, so the agent keeps one session for the whole conversation.

```bash
mindroom trigger send support-inbox \
  --key-file /etc/mindroom/triggers/support.key \
  --kind support.message \
  --event-id ticket-812:msg-3 \
  --thread-key ticket-812 \
  --message "Customer replied on ticket 812."
```

Choose a key that names the upstream conversation, not the message, such as a ticket ID, an upstream thread timestamp, or a direct-message channel ID.
A key stays bound to its thread for 7 days after its most recent delivery; after that, or after `rotate_trigger_key`, the next delivery with the key opens a new thread.
If two deliveries with a new key arrive together, one may receive `409 External trigger thread is being opened by another delivery`; retry it as a fresh signed request with the same `event_id` and it joins the thread.
`thread_key` has no effect on triggers with a fixed `target_thread_id` or without `new_thread`.

## Reusable Trigger Idempotency

Give the same external event the same `--event-id`, such as a reservation ID, Git commit SHA, release tag, webhook delivery ID, or a hash of the changed state.
After a successful delivery, a request with the same `event_id` is answered as a duplicate and posts nothing for the next 24 hours.
If `--event-id` is omitted, the CLI generates a random one, so repeated sends are not deduplicated.

To retry, send a fresh signed request with the same `event_id`; replaying identical request bytes and headers is rejected because each nonce is single-use.

`event_id`, `thread_key`, and the signature nonce are each limited to 256 bytes, so use short ASCII identifiers.
Each trigger holds at most 10,000 recent nonces, 10,000 recent `event_id` values, and 10,000 active `thread_key` values; beyond that, requests receive `429` until older entries expire.
Rotating a trigger's key or deleting the trigger forgets its delivered `event_id` values.

Deduplication requires every API process that accepts triggers to share one control-state filesystem.

## Delivery Requirements

A request is delivered only when all of these hold:

- The trigger exists, is enabled, and `external_trigger_policy.enabled` is true.
- The owner is still allowed to talk to the target agent in the target room.
- The owner, or one of the owner's `authorization.aliases`, is currently joined to the target room; bot accounts do not count.
- The router and the target agent or team are running and joined to the target room.

The triggered turn runs on behalf of the trigger owner, not the router.
For a private agent (`private.per`), the trigger wakes the owner's private instance, so a trigger created by `@alice:example.org` uses Alice's private state.
An administrator's trigger for a private agent wakes the administrator's private state.

## Troubleshooting Responses

A successful request returns `202` with `accepted: true`, the `event_id`, and the posted Matrix event ID, or `duplicate: true` when the `event_id` was already delivered.

| Status | Detail | Cause and fix |
| --- | --- | --- |
| `401` | `Invalid external trigger signature` | Wrong private key, `key_id` mismatch, missing signature headers, or a timestamp outside the replay window; check the watcher's clock. |
| `403` | `External trigger owner is not authorized for this target room` | The owner lost access to the target agent in that room. |
| `403` | `External trigger owner is not joined to the target room` | The owner left the room; rejoin it. |
| `404` | `External trigger not found` | Unknown, disabled, deleted, or consumed trigger, triggers disabled in policy, or a wrong callback token. |
| `409` | `External trigger nonce has already been used` | The same signed request was sent twice; sign a new one. |
| `409` | `External trigger event is already in progress` | Another request with this `event_id` is still being delivered. |
| `413` | `External trigger body exceeds configured limit` | Shrink the body or raise `max_body_bytes`. |
| `422` | `External trigger kind is not allowed` | `kind` is not in the trigger's `allowed_kinds`; other `422` responses list invalid body fields. |
| `429` | `External trigger replay limit reached` | Too many recent events for one trigger; wait for older entries to expire. |
| `503` | `External trigger target runtime is not available` | The router or target bot is not running or not joined to the room yet; retry later. |

## Watcher Behavior

A polling watcher should store the last observed state and compare before sending.
A webhook watcher should deduplicate webhook delivery IDs before calling MindRoom.

## Security Modes

### Kubernetes Hardened Mode

Keep the trigger private key out of the agent's reach.
Run always-on watchers as a sidecar of the MindRoom runtime pod, a CronJob, or a separate deployment, and mount the private key only into that watcher container, never into `sandbox-runner` or the agent workspace.
A watcher script may live in the agent's workspace as long as the key does not.
A worker pod can send triggers when the watcher is meant to live only as long as that worker.
Set `MINDROOM_URL` to the MindRoom service URL or pass `--url`.
If the watcher calls `mindroom trigger send`, its image needs the `mindroom` CLI.

### Personal VM Or Unsandboxed Mode

On a personal VM, a cron job running as the MindRoom user can call `http://127.0.0.1:8765`.
This does not hide the private key from an agent with unsandboxed shell access as that same user.
To hide the key from agent code, run the watcher as a separate OS user or keep the agent's code tools in a sandbox.

## Agent Callbacks

Agent callbacks let an agent hand a background task a script that wakes the same agent in the same thread when the task finishes.

1. The agent starts a background Codex session or another long-running task.
2. It calls `mint_callback` with a short label for that task.
3. It includes the returned instruction in the background task's prompt.
4. The background task runs the script with a short result summary when it finishes.

### Configuration

Enable the tool on agents that launch background work:

```yaml
agents:
  orchestrator:
    role: Launch and supervise coding agents.
    tools:
      - callback_manager
```

No callback-specific configuration exists.
Callbacks follow `external_trigger_policy` and count toward `max_triggers_per_owner` until used or deleted.

### Tool Result

`mint_callback(label)` returns a script path and an instruction like this:

```text
When finished, run: bash /path/to/callback_1234.sh "<short result summary>"
```

The script needs only Bash and curl.
It posts the summary to the room and thread where the callback was minted, which wakes the agent.
After a successful delivery the callback is used up and the script deletes itself; if delivery fails, the script remains so it can be run again.
Unused callbacks never expire, so delete abandoned ones with `external_trigger_manager.delete_trigger`.

### Network Access

The script calls the address of the MindRoom API server that was running when the callback was minted, including a non-default `--api-port`, or `http://127.0.0.1:8765` when none was running.
When the background process must reach MindRoom at another address, set `MINDROOM_URL` in MindRoom's own environment before the agent mints the callback, because the address is written into the script.
Point `MINDROOM_URL` only at a trusted MindRoom endpoint because the script sends its token there.

### Security

Each script contains a random bearer token that can only wake the agent, room, and thread captured when it was minted.
A missing or wrong token gets the same not-found response as an unknown trigger.
Callback deliveries pass the same [delivery requirements](#delivery-requirements) as signed triggers.
Use signed triggers for reusable integrations that need a stable identity.
