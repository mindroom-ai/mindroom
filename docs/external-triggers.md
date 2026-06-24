---
icon: lucide/radio-tower
---

# External Triggers

External triggers let a watcher wake MindRoom without keeping an agent turn alive.

A watcher process runs outside the agent loop, detects a meaningful change, and sends one signed HTTP event to MindRoom.

MindRoom verifies the signature, checks replay and size limits, checks that the owner can still talk to the target agent in the target room, then posts a Matrix message with the target agent or team mention.

MindRoom does not run watcher code and does not poll external systems from the agent turn loop.

## Use Cases

- A campground cancellation watcher checks a booking site and sends an event only when a matching site opens.
- A Git repo watcher tracks a branch, tag, or webhook payload and sends an event only when the observed commit or digest changes.

## Model

Triggers are managed by the `external_trigger_manager` tool, not by authored per-trigger YAML.

`config.yaml` contains only global policy for the feature.

Trigger records live in primary-runtime control state under `MINDROOM_CONTROL_STATE_PATH` or under `mindroom_data/control_state` by default.

Workers, sandbox runners, and public runtime environments do not receive `MINDROOM_CONTROL_STATE_PATH`.

The tool accepts only public key material.

The watcher keeps the private key.

Tool output includes the endpoint path and public key fingerprint, but never includes the private key or raw public key.

The public API endpoint is `POST /api/triggers/<trigger_id>`.

## Configuration

Add the manager tool to agents that should be allowed to request triggers.

Use tool approval rules to gate `create_trigger`, `rotate_trigger_key`, `disable_trigger`, and `delete_trigger` when users should not self-provision triggers without approval.

```yaml
agents:
  ops:
    display_name: Ops
    role: Watch external systems and report actionable changes.
    model: default
    rooms: [lobby]
    tools:
      - external_trigger_manager

models:
  default:
    provider: openai
    id: gpt-5.5

external_trigger_policy:
  enabled: true
  default_replay_window_seconds: 300
  max_replay_window_seconds: 3600
  default_max_body_bytes: 65536
  max_body_bytes: 262144
  max_triggers_per_owner: 20
  admin_users:
    - "@admin:example.org"
```

`enabled: false` makes trigger endpoints return not found.

`admin_users` can create, list, rotate, enable, disable, or delete triggers across owners.

Non-admin callers can create triggers only for the current agent and current room in the live Matrix tool context.

The target room must already be configured for the target agent or team.

Triggers do not widen static room membership.

No new Matrix room is created for a trigger.

## Setup Flow

Generate a watcher signing key.

```bash
mindroom trigger keygen --private-key-file /etc/mindroom/triggers/campground.key
```

Give the printed `public_key` to the agent and ask it to call `external_trigger_manager.create_trigger`.

Keep the printed `private_key` or the `--private-key-file` output only in the watcher runtime.

Example tool arguments:

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

For a non-admin caller, the target is the current agent and current room.

For an admin caller, `target_agent`, `target_room_id`, and `target_thread_id` can be provided.

The tool returns `/api/triggers/<trigger_id>` when creation succeeds.

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

The request body contains `kind`, `message`, optional `event_id`, optional `title`, and optional `data`.

`kind` must match `allowed_kinds` when the trigger record has an allowlist.

Use `--no-verify-tls` only for local development against a trusted endpoint.

## Runtime Checks

Each request uses one immutable trigger snapshot.

That snapshot includes the record version, auth epoch, target, public key, policy-capped replay window, policy-capped body size, and current API config generation.

The API authenticates the signature, parses the body, checks current owner authorization, checks target runtime readiness, checks live owner membership in the target room, claims replay state, then dispatches.

Target runtime readiness requires both the router and target bot to be running and joined to the resolved target room.

The delivered Matrix message stamps the original Matrix requester as trusted trigger owner metadata.

That metadata lets private work agents treat the trigger as a turn from the owner, not from the router bot.

## Idempotency

Use a stable `--event-id` for the same external event.

For example, use a reservation ID, Git commit SHA, release tag, webhook delivery ID, or deterministic hash of the changed state.

If delivery succeeds, a later signed request with the same `event_id` is treated as a duplicate and does not post another Matrix message while the replay record is retained.

Retries must create a fresh signed request with the same `--event-id`.

Each nonce-bearing HTTP request is single-use.

Do not reuse the same HTTP request body and headers as a retry strategy.

Replay state lives in the primary control-state JSON store and uses an advisory file lock.

Deploy trigger ingress with a single shared control-state filesystem, or keep one API writer until replay storage moves to a distributed atomic backend.

If `--event-id` is omitted, the CLI generates a random event ID, so repeated sends are not idempotent.

## Watcher Behavior

Watcher code should call `mindroom trigger send` only when something meaningful changes.

For a polling watcher, store the last observed state and compare before sending.

For a webhook watcher, deduplicate webhook delivery IDs before calling MindRoom.

MindRoom receives trigger events.

MindRoom does not host watcher loops, schedule watcher polls, or keep an agent turn alive while waiting for external state.

## Security Modes

### Kubernetes Hardened Mode

Keep the trigger private key outside the agent sandbox.

Do not mount the private key into the agent sandbox.

The agent can see the watcher script if it is in its workspace, but it should not see the private key or control-state path.

Run always-on polling watchers as a top-level runtime sidecar, a CronJob, or an external deployment.

Worker pods can call MindRoom when a watcher is intentionally scoped to that worker lifecycle.

Mount the trigger private key only into the watcher container that needs it.

Do not mount the trigger private key into `sandbox-runner` or the agent workspace.

Use your deployed MindRoom service URL for `MINDROOM_URL`.

The watcher image must contain the watcher code and the `mindroom` CLI if the watcher shells out to `mindroom trigger send`.

### Personal VM Or Unsandboxed Mode

A cron job can run as the same user as MindRoom and call `http://127.0.0.1:8765`.

This is convenient for a personal VM.

This is not a secret boundary if the agent has unsandboxed shell access as that same user.

In that mode, the agent can usually read the same user's files or invoke the same local tools.

Use a separate OS user, sandbox, Kubernetes sidecar, or CronJob isolation when the private key must be hidden from agent code.
