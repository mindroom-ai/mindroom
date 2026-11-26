---
icon: lucide/radio-tower
---

# External Triggers

Triggers let MindRoom wake an agent or team automatically in response to events—either from Matrix activity or from external systems—without keeping an agent turn alive or running a polling loop per watch.

Use them for:

- **Reactive triggers**, created with `triggers(action=...)` (`watch_room`, `watch_thread`, `subscribe_event`) or `watch_pr(...)`, to watch Matrix rooms with optional cheap triage, react when a thread resolves or changes tags, watch GitHub PRs, or subscribe to shared external event sources such as GitHub or generic HMAC webhooks.
- **Direct signed triggers**, created with `triggers(action="create", ...)`, to give a dedicated watcher its own `POST /api/triggers/<trigger_id>` endpoint authenticated with an Ed25519 signature.
- **Single-use [agent callbacks](#agent-callbacks)**, created with the `callback_manager` tool, to give a background task a Bash/curl script that wakes the originating agent and thread once.

`triggers` is the canonical toolkit name and exposes `triggers(action=...)` and `watch_pr(...)`.
`external_trigger_manager` remains a backward-compatible alias in `config.yaml`.
Both `triggers: {defer: true}` and `external_trigger_manager: {defer: true}` are supported when using [dynamic tools](tools/dynamic-tools.md).

Triggers are created and managed by tool calls in a live Matrix conversation, not in `config.yaml` (except shared [external event sources](#shared-external-event-sources), which admins declare under `external_trigger_policy.event_sources`).
Trigger records are stored in the primary runtime's [control state](deployment/storage.md#data-persistence) (`reactive_triggers.db` for reactive triggers, alongside direct trigger records and replay claims).
Agent workers and sandbox runners do not receive the control state path.

## Configuration

Add `triggers` (or its alias `external_trigger_manager`) to agents that may create and manage triggers. You can defer it with `{defer: true}` to keep its schema out of context until needed.

```yaml
agents:
  ops:
    display_name: Ops
    role: Watch rooms and external systems and report actionable changes.
    model: default
    rooms: [lobby]
    tools:
      - triggers: {defer: true}

external_trigger_policy:
  enabled: true
  reactive_enabled: true
  max_triggers_per_owner: 20
  max_reactive_triggers_per_owner: 50
  max_fires_per_hour: 60
  admin_users:
    - "@admin:example.org"
  event_sources:
    github:
      kind: github
      secret_env: GITHUB_WEBHOOK_SECRET
      allowed_repos:
        - mindroom-ai/mindroom
      allowed_events:
        - pull_request
        - pull_request_review
        - check_suite
        - release
        - issues
        - push
      subscribers:
        - "@alice:example.org"
```

`external_trigger_policy` is optional; every field has a default.

| Field | Type | Default | Valid values | Meaning |
| --- | --- | --- | --- | --- |
| `enabled` | bool | `true` | | Master switch. When `false`, every direct trigger, callback, and shared event-source endpoint answers not found, `callback_manager` refuses to mint callbacks, and reactive trigger delivery is disabled. |
| `reactive_enabled` | bool | `true` | | Enables or disables reactive triggers (`watch_room`, `watch_thread`, `subscribe_event`) and `POST /api/event-sources/{source_id}` independently of direct signed triggers. |
| `default_replay_window_seconds` | int | `300` | 30 to 3600 | Signature age accepted by new direct signed triggers that do not set their own window. |
| `max_replay_window_seconds` | int | `3600` | 30 to 3600 | Cap on any direct trigger's replay window. |
| `default_max_body_bytes` | int | `65536` | 1024 to 262144 | Request body limit for new direct triggers that do not set their own. |
| `max_body_bytes` | int | `262144` | 1024 to 262144 | Cap on any direct trigger's body limit. |
| `max_triggers_per_owner` | int | `20` | 1 to 1000 | Direct trigger records one owner may hold, including unused callbacks. |
| `max_reactive_triggers_per_owner` | int | `50` | 1 to 1000 | Reactive trigger rules one owner may hold. |
| `max_fires_per_hour` | int | `60` | 1 to 600 | Cap on the per-rule `max_fires_per_hour` rate limit for reactive triggers. |
| `admin_users` | list of Matrix IDs | `[]` | | Trigger-only administrators. |
| `event_sources` | mapping | `{}` | Keys matching `^[A-Za-z0-9_-]{1,64}$` | Named [shared external event sources](#shared-external-event-sources) served at `POST /api/event-sources/{source_id}`. |

Each default must not exceed its matching cap.
Lowering a cap also limits existing triggers.

Top-level `administrators` and `admin_users` can list, enable, disable, rotate, and delete any owner's triggers, can subscribe to any configured event source, and can target other agents, teams, and rooms.
Only a trigger's owner can rotate the key of a direct trigger for a [personal agent](personal-agents.md), a created agent, or a team that includes one, because a new key signs events that run as that owner.
`admin_users` grants only this trigger authority, not wider administrator rights.
Both lists match canonical identities after `authorization.aliases` resolution.
Every trigger function needs a human requester, so agents and bot accounts get no trigger authority, even when listed.

To stop users from creating or modifying triggers without review, add a [tool approval](tool-approval.md) rule for `triggers`.

## Reactive Triggers

Reactive triggers run inside the primary runtime's unified pipeline:

1. **Source adapter** observes new Matrix room messages, `com.mindroom.thread.tags` state transitions, or incoming shared external events.
2. **Deterministic filter** checks room, thread, sender aliases, regex pattern, tag transition, or structured event fields with zero LLM cost.
3. **Optional tool-less triage** evaluates plain-English criteria using a cheap classifier or model before waking any agent.
4. **Atomic SQLite claim** enforces `UNIQUE(trigger_id, dedupe_key)` idempotency, cooldown, hourly rate limit, expiry, and `once=True` disablement inside `<control_state>/reactive_triggers.db`.
5. **Owner-scoped dispatch** posts a Matrix message mentioning the target agent with the owner's `prompt` and the event wrapped in `<untrusted_event_data>` boundaries.

A non-match or failed triage creates neither a Matrix message nor an agent turn, and never consumes a `once=True` trigger.

### Watching a room (`action="watch_room"`)

`triggers(action="watch_room", ...)` watches new visible messages arriving in a Matrix room.
At creation, MindRoom records the room's current journal head so older history is never replayed.
Messages stamped as trigger-originated (`reactive_trigger` or `external_trigger`) are always skipped to prevent loops.
By default (`include_bots=False`), bot and agent messages are skipped; pass `include_bots=True` to watch other configured MindRoom agents (or allowed external bots) while unconditionally blocking messages from the trigger's own target agent (or any member of a target team).

| Argument | Default | Meaning |
| --- | --- | --- |
| `prompt` | required | Instruction delivered to the woken agent when the rule fires (max 4,000 chars). |
| `room_id` | current room | Watched Matrix room ID or configured room alias; the owner must be joined to it. |
| `thread_id` | none | Restrict matching to messages inside this thread root. |
| `senders` | any | Optional list of Matrix sender IDs (matched with `authorization.aliases`). |
| `pattern` | none | Optional case-insensitive regex (max 256 chars), evaluated with a strict search timeout over the first 4,096 characters. |
| `include_bots` | `false` | When `true`, allow messages from other configured MindRoom agents (once the owner's source-room membership and authorization pass) and authorized external bots; messages from the target agent or target team members and trigger-generated messages are always skipped. Avoid creating mutual `include_bots=true` rules between two agents that reply to each other (`max_fires_per_hour` and `cooldown_seconds` cap runaway loops if both fire). |
| `triage_instructions` | none | Optional plain-English criteria for [cheap tool-less triage](#cheap-tool-less-triage) (max 1,000 chars). |
| `triage_model` | none | Configured model alias from `models`; required when `triage_instructions` is set and `triage_provider="llm"`. |
| `triage_provider` | `"llm"` | Triage backend: `"llm"`, `"typesafe"`, or `"openai_decisions"`. |
| `triage_threshold` | none (`0.8` for classifiers) | Confidence threshold (`0.0` to `1.0`) for `"typesafe"` or `"openai_decisions"`; omitted for `"llm"`. |
| `target_agent` | current agent | Agent or team to wake; non-admins may target only agents they are authorized to address in `target_room_id`. |
| `target_room_id` | current room | Room where the wake message is posted. |
| `target_thread_id` | `"origin"` in watched room, else timeline | Fixed thread root ID, `"origin"` to reply in the triggering message's thread/root, or omitted. When `new_thread=False` and `target_thread_id` is omitted, `watch_room` defaults to `"origin"` if targeting the watched room, or the room timeline if targeting another room. |
| `new_thread` | `false` | Open a fresh thread per firing (or per `thread_key`); mutually exclusive with `target_thread_id`. |
| `thread_key` | none | Optional grouping key (requires `new_thread=True`) so repeated firings reuse one thread. |
| `once` | `false` | Disable the trigger automatically after one firing. |
| `max_fires_per_hour` | `10` | Per-rule hourly cap, bounded by `external_trigger_policy.max_fires_per_hour`. |
| `cooldown_seconds` | `0` | Minimum seconds between consecutive firings. |
| `expires_in_hours` | none | Optional lifetime in hours (positive, up to `8760`). |
| `trigger_id` | auto (`tr_<12 hex>`) | Optional custom ID matching `^[A-Za-z0-9_-]{1,64}$`. |
| `description` | `""` | Human-readable purpose. |

### Watching a thread tag (`action="watch_thread"`)

`triggers(action="watch_thread", ...)` watches a thread's `com.mindroom.thread.tags` state event—for example, waking an agent in thread B as soon as thread A is marked `resolved`.

At creation, MindRoom reads the thread's current tag state into a checkpoint without firing.
If the tag is already in the requested state (`transition="added"` when already present, or `"removed"` when already absent), `watch_thread` succeeds, reports `already_in_state: true`, and fires only on a future transition.
Live tag updates fire immediately, and offline transitions that remain in the new state are reconciled on startup without double-firing.

| Argument | Default | Meaning |
| --- | --- | --- |
| `prompt` | required | Instruction delivered to the woken agent when the transition occurs. |
| `tag` | `"resolved"` | Tag name to watch in `com.mindroom.thread.tags`. |
| `transition` | `"added"` | `"added"` (`absent -> present`) or `"removed"` (`present -> absent`). |
| `room_id` | current room | Watched Matrix room containing the thread. |
| `thread_id` | current thread | Watched thread root event ID (required if called outside a thread). |
| `target_agent` | current agent | Agent or team to wake. |
| `target_room_id` | current room | Room where the wake message is posted. |
| `target_thread_id` | none | Fixed thread root ID to post into (mutually exclusive with `new_thread`). |
| `new_thread` | `false` | Open a fresh thread (or reuse `thread_key`). |
| `thread_key` | none | Optional grouping key (requires `new_thread=True`). |
| `once` | `true` | Disable automatically after one firing. |
| `max_fires_per_hour` | `10` | Per-rule hourly cap, bounded by `external_trigger_policy.max_fires_per_hour`. |
| `cooldown_seconds` | `0` | Minimum seconds between consecutive firings. |
| `expires_in_hours` | none | Optional lifetime in hours (positive, up to `8760`). |
| `trigger_id` | auto (`tr_<12 hex>`) | Optional custom ID matching `^[A-Za-z0-9_-]{1,64}$`. |
| `description` | `""` | Human-readable purpose. |

### Subscribing to shared event sources (`action="subscribe_event"`)

`triggers(action="subscribe_event", ...)` attaches a reactive trigger to a named source configured under `external_trigger_policy.event_sources` (such as GitHub PR merges, generic HMAC-SHA256 webhooks, or shared Ed25519 fan-out events). Call `triggers(action="list")` to discover which event sources you are authorized to subscribe to.
The requester must be listed in that source's `subscribers` (or be a trigger/platform administrator), and `kind` and `match` are validated against the source's `allowed_events`, `allowed_repos`, or `allowed_kinds`.

| Argument | Default | Meaning |
| --- | --- | --- |
| `source_id` | required | Key under `external_trigger_policy.event_sources`. |
| `kind` | required | Normalized event kind to match (for example `"pull_request.merged"`, `"push"`, or a custom Ed25519 `kind`). |
| `prompt` | required | Instruction delivered to the woken agent when the event matches. |
| `match` | none | Optional dictionary of exact equality checks (`str`, `int`, or `bool`) against structured `event.data` fields (for example `{"repo": "mindroom-ai/mindroom", "number": 4198}`). Supports dotted keys for nested objects. |
| `triage_instructions` | none | Optional plain-English criteria for [cheap tool-less triage](#cheap-tool-less-triage). |
| `triage_model` | none | Configured model alias from `models` when `triage_provider="llm"`. |
| `triage_provider` | `"llm"` | Triage backend: `"llm"`, `"typesafe"`, or `"openai_decisions"`. |
| `triage_threshold` | none (`0.8` for classifiers) | Confidence threshold for `"typesafe"` or `"openai_decisions"`. |
| `target_agent` | current agent | Agent or team to wake. |
| `target_room_id` | current room | Room where the wake message is posted. |
| `target_thread_id` | none | Fixed thread root ID to post into (mutually exclusive with `new_thread`). |
| `new_thread` | `false` | Open a fresh thread (or reuse `thread_key`). |
| `thread_key` | none | Optional grouping key (requires `new_thread=True`). |
| `once` | `true` | Disable automatically after one firing. |
| `max_fires_per_hour` | `10` | Per-rule hourly cap, bounded by `external_trigger_policy.max_fires_per_hour`. |
| `cooldown_seconds` | `0` | Minimum seconds between consecutive firings. |
| `expires_in_hours` | none | Optional lifetime in hours (positive, up to `8760`). |
| `trigger_id` | auto (`tr_<12 hex>`) | Optional custom ID matching `^[A-Za-z0-9_-]{1,64}$`. |
| `description` | `""` | Human-readable purpose. |

### Cheap tool-less triage

When `triage_instructions` is set on `watch_room` or `subscribe_event`, MindRoom evaluates the event through the judgment subsystem before claiming or firing the rule:

- **`llm`**: calls a configured model alias (`triage_model`) with no tools attached. `triage_threshold` is not used.
- **`typesafe`**: calls the TypeSafe classifier with `triage_threshold` (default `0.8`). `triage_model` must be omitted.
- **`openai_decisions`**: calls the OpenAI Decisions classifier with `triage_threshold` (default `0.8`). `triage_model` must be omitted.

Event content is placed inside explicit untrusted-data boundaries.
Triage is **strictly tool-less** and **fail-closed**: only a `True` decision proceeds to claim and dispatch.
A `False` decision, `None` result, timeout, missing API key, or provider error drops the candidate as `triaged_no` without incrementing `fire_count` or consuming `once=True`.

### Rate limits, cooldown, and expiry

- **`once`**: disables the rule atomically inside the SQLite claim transaction so retries or concurrent events cannot double-fire.
- **`max_fires_per_hour`**: counts `claimed` and `delivered` firings over the trailing hour; capped by `external_trigger_policy.max_fires_per_hour`.
- **`cooldown_seconds`**: minimum elapsed time since `last_fired_at` before the next claim succeeds.
- **`expires_in_hours`**: sets an absolute Unix expiry timestamp after which the rule stops matching and firing.
- Finished firing records older than 30 days are pruned periodically.

## Shared External Event Sources

Admins can configure shared inbound event sources under `external_trigger_policy.event_sources` so one webhook or signed emitter can fan out to multiple subscribed reactive triggers.

Every configured source is served at:

```text
POST /api/event-sources/{source_id}
```

When verification succeeds, MindRoom fans out the normalized event to all enabled, unexpired, authorized matching rules in SQLite and returns `202 Accepted`:

```json
{
  "accepted": true,
  "source_id": "github",
  "event_id": "delivery-123",
  "matched_rules": 2
}
```

Verification authenticates the producer only.
At both `subscribe_event` creation and event fan-out, MindRoom checks that each rule's owner is listed in `subscribers` (or is an administrator) and enforces the source's allowlists.

### GitHub webhooks (`kind: github`)

Configure a GitHub webhook source with an HMAC-SHA256 secret stored in an environment variable (`secret_env` holds only the variable name, never the secret itself):

```yaml
external_trigger_policy:
  event_sources:
    github:
      kind: github
      secret_env: GITHUB_WEBHOOK_SECRET
      allowed_repos:
        - mindroom-ai/mindroom
      allowed_events:
        - pull_request
        - pull_request_review
        - check_suite
        - release
        - issues
        - push
      subscribers:
        - "@alice:example.org"
      max_body_bytes: 262144
```

| Field | Default | Valid values | Meaning |
| --- | --- | --- | --- |
| `kind` | `"github"` | `"github"` | Selects GitHub HMAC-SHA256 webhook verification and normalization. |
| `secret_env` | required | `^[A-Z_][A-Z0-9_]*$` | Environment variable name holding the GitHub webhook secret. |
| `allowed_repos` | required | Non-empty list of `owner/repo` | Fail-closed allowlist of repositories accepted by this source. |
| `allowed_events` | `["pull_request", "pull_request_review", "pull_request_review_comment", "issue_comment", "check_run", "check_suite", "release", "issues", "push"]` | Non-empty GitHub `X-GitHub-Event` names | Allowlisted GitHub webhook event types. |
| `subscribers` | `[]` | Matrix user IDs | Non-admin users allowed to subscribe rules to this source. |
| `max_body_bytes` | `262144` | 1024 to 1048576 | Maximum accepted webhook body size in bytes. |

MindRoom verifies `X-Hub-Signature-256` with `hmac.compare_digest`, requires non-empty `X-GitHub-Delivery` (used as `event_id`) and `X-GitHub-Event` headers, checks `repository.full_name` case-insensitively against `allowed_repos`, and normalizes `event.data["repo"]` to the canonical spelling from `allowed_repos`.

Normalized GitHub events:

- **`pull_request`**: `kind = "pull_request.merged"` when `action == "closed"` and `pull_request.merged` is `true`, otherwise `pull_request.<action>`. Structured `data`: `repo`, `number`, `action`, `merged`, `branch`, `base_branch`, `author`, `sha`, `url`.
- **`pull_request_review`**, **`issues`**, **`release`**, **`check_suite`**: `kind = "<event>.<action>"` with structured `repo`, identifiers (`number`/`tag`/`id`), `action`, `author`, `state`/`conclusion`, and `url`.
- **`push`**: `kind = "push"` with structured `repo`, `ref`, `branch`, `after`, `pusher`, `url`.

Attacker-controlled free text (such as PR titles and bodies) is isolated in `title` and `summary` as untrusted data and is never placed into structured `match` keys.

!!! note "GitHub header-only replay properties"
    GitHub's webhook protocol computes `X-Hub-Signature-256` over the raw HTTP body only; it does not sign `X-GitHub-Delivery` or `X-GitHub-Event`, and it does not include a request timestamp or nonce.
    MindRoom uses `X-GitHub-Delivery` as each rule's `dedupe_key` (retained in SQLite until finished firings are pruned after 30 days), which deduplicates genuine GitHub redeliveries.
    Because headers and timestamps are unsigned by GitHub, keep the webhook endpoint behind TLS and keep the webhook secret private so an eavesdropper cannot replay a captured body under a fabricated `X-GitHub-Delivery` header.

### Generic HMAC-SHA256 webhooks (`kind: hmac`)

Configure a generic HMAC-SHA256 source for any webhook provider that signs the raw request body with a shared secret (such as Linear, custom services, or internal alert relays):

```yaml
external_trigger_policy:
  event_sources:
    linear:
      kind: hmac
      secret_env: LINEAR_WEBHOOK_SECRET
      signature_header: Linear-Signature
      signature_prefix: ""
      kind_field: type
      event_id_header: Linear-Delivery
      allowed_kinds:
        - Issue
        - Comment
      subscribers:
        - "@alice:example.org"
      max_body_bytes: 262144
```

| Field | Default | Valid values | Meaning |
| --- | --- | --- | --- |
| `kind` | `"hmac"` | `"hmac"` | Selects generic HMAC-SHA256 webhook verification and normalization. |
| `secret_env` | required | `^[A-Z_][A-Z0-9_]*$` | Environment variable name holding the shared HMAC secret. |
| `signature_header` | `"X-Webhook-Signature-256"` | Valid ASCII HTTP header name | Header carrying the HMAC-SHA256 digest (matched case-insensitively). |
| `signature_prefix` | `"sha256="` | Any string (`""` for bare hex) | Prefix stripped before comparing the lowercase hex digest. |
| `kind_header` | none | Valid ASCII HTTP header name | Optional header holding the event `kind` (takes precedence over `kind_field`). |
| `kind_field` | `"kind"` | Non-empty JSON key or dot-path | JSON string field used as `kind` when `kind_header` is unset. |
| `event_id_header` | none | Valid ASCII HTTP header name | Optional header used as deduplication `event_id`; falls back to payload `event_id`/`id` or `sha256:<body-hash>`. |
| `allowed_kinds` | `[]` (any) | Non-empty strings | Optional allowlist of accepted `kind` values. |
| `subscribers` | `[]` | Matrix user IDs | Non-admin users allowed to subscribe rules to this source. |
| `max_body_bytes` | `262144` | 1024 to 1048576 | Maximum accepted webhook body size in bytes. |

Verified HMAC webhooks populate `event.data` with the parsed JSON object and extract optional string `actor`, `title`, `summary`, and `text` fields when present.

!!! note "HMAC headers are unsigned"
    Generic HMAC-SHA256 verification signs only the raw HTTP request body, not HTTP headers such as `kind_header` or `event_id_header`.
    For replay-sensitive sources, prefer `kind_field` and a body-level `event_id` (or `id`) field so event classification and deduplication are bound to the signed payload, and keep the endpoint behind TLS with a private shared secret.

### Shared Ed25519 fan-out (`kind: ed25519`)

Configure a shared Ed25519 source when one trusted watcher emits structured events that multiple users or rules should subscribe to:

```yaml
external_trigger_policy:
  event_sources:
    ci:
      kind: ed25519
      public_key: "BASE64_OR_OPENSSH_ED25519_PUBLIC_KEY"
      key_id: "ci-main"
      allowed_kinds:
        - deploy.finished
        - build.failed
      subscribers:
        - "@alice:example.org"
      replay_window_seconds: 300
      max_body_bytes: 65536
```

| Field | Default | Valid values | Meaning |
| --- | --- | --- | --- |
| `kind` | `"ed25519"` | `"ed25519"` | Selects Ed25519 signature verification and nonce replay checks. |
| `public_key` | required | Raw base64, OpenSSH `ssh-ed25519`, or PEM | Ed25519 public key used to verify requests. |
| `key_id` | `"default"` | 1 to 128 chars | Expected `X-MindRoom-Trigger-Key-Id` value. |
| `allowed_kinds` | `[]` (any) | Event `kind` strings | Optional allowlist of accepted `kind` values. |
| `subscribers` | `[]` | Matrix user IDs | Non-admin users allowed to subscribe rules to this source. |
| `replay_window_seconds` | `300` | 30 to 3600 | Maximum accepted signature age in seconds. |
| `max_body_bytes` | `65536` | 1024 to 262144 | Maximum accepted request body size in bytes. |

Senders use the same `MINDROOM-TRIGGER-V1` signature format and JSON payload shape as [direct signed triggers](#sending-events), signing the request path `/api/event-sources/<source_id>`.

!!! tip "Always set `event_id` for Ed25519 fan-out"
    Include a deterministic `event_id` in every JSON payload sent to a shared Ed25519 event source (for example a CI job ID or upstream event ID).
    If `event_id` is omitted, MindRoom falls back to `X-MindRoom-Trigger-Nonce` as the rule `dedupe_key`.
    Because retries must be signed with a fresh nonce, omitting `event_id` means retried deliveries will not deduplicate across subscribed rules.

### Event source responses

| Status | Cause |
| --- | --- |
| `202` | Event verified, normalized, and fanned out (`matched_rules` may be `0` if no rule matched or all matching rules already saw `event_id`). |
| `400` | Malformed JSON, missing required webhook headers/fields, disallowed `kind` on `hmac`, or oversized `event_id`. |
| `401` | Invalid GitHub/`hmac` HMAC-SHA256 signature or invalid/expired Ed25519 signature. |
| `403` | GitHub event type or repository is not in `allowed_events` / `allowed_repos`. |
| `404` | Unknown `source_id`, or `external_trigger_policy.enabled` / `reactive_enabled` is `false`. |
| `409` | Reused Ed25519 signature nonce. |
| `413` | Request body exceeds the source's `max_body_bytes`. |
| `422` | Invalid Ed25519 JSON payload or `kind` not in `allowed_kinds`. |
| `503` | Webhook secret env var missing/empty, configuration unavailable, or replay/reactive trigger store unavailable. |

## Signed Trigger Setup Flow

For a dedicated single-target watcher that posts directly to `/api/triggers/<trigger_id>`:

1. Generate a signing key where the watcher runs.

   ```bash
   mindroom trigger keygen --private-key-file /etc/mindroom/triggers/campground.key
   ```

   The command prints `private_key`, `public_key`, and `public_key_fingerprint`.
   Keep the private key only in the watcher runtime.

2. In a Matrix conversation with the agent, give it the printed `public_key` and ask it to call `triggers(action="create", ...)`.

   ```json
   {
     "action": "create",
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

### `action="create"` arguments

| Argument | Default | Meaning |
| --- | --- | --- |
| `trigger_id` | required | Endpoint name; ASCII letters, digits, `_`, and `-` only. |
| `public_key` | required | Ed25519 public key as raw base64, an OpenSSH `ssh-ed25519 ...` line, or PEM. |
| `key_id` | `default` | Key ID the sender must present. |
| `description` | `""` | Human-readable purpose. |
| `target_agent` | current agent | Agent or team to wake; only administrators can choose another. |
| `target_room_id` | current room | Room to post in; only administrators can choose another. |
| `target_thread_id` | current thread (in current room) | Post every delivery into this existing thread, or pass `"room"` to explicitly post to the room's main timeline. |
| `new_thread` | `false` | Post each delivery as a new room-level message so the agent answers in a fresh thread with a fresh session. |
| `reopen_on_fire` | `false` | Automatically clear the `resolved` tag on a fixed target thread before delivering a trigger firing. |
| `allowed_kinds` | any | Accepted `kind` values; others are rejected. |
| `replay_window_seconds` | policy default | Maximum signature age, capped by policy. |
| `max_body_bytes` | policy default | Request body limit, capped by policy. |

`target_thread_id` and `new_thread` are mutually exclusive.
When `target_thread_id` is omitted and the target room is the current room, deliveries default to the current thread (or the room's main timeline when called outside a thread).
Pass `target_thread_id="room"` to force room-timeline delivery from inside a thread.

The requester who asks for the trigger becomes its owner and must be a human user, not a bot account.
An administrator who creates a trigger for another agent or room still owns it.
Shared agents and teams can only be targeted in rooms already configured for them.
A [private agent](configuration/agents.md#private-instances), [personal agent](personal-agents.md), or [created agent](personal-agents.md#created-agents-and-self-updates) creating its own trigger may also target its current room when no agent or team lists that room in `rooms`, such as a personal room.
Triggers never create rooms or make agents join rooms.

### Managing, inspecting, and testing triggers

The `triggers(action=...)` tool provides unified lifecycle, observability, and testing actions available to the trigger owner (and administrators where noted below; `update` is owner-only):

- `triggers(action="list", include_disabled=True)` lists the requester's direct and reactive triggers (or every trigger for administrators) along with `event_sources` metadata for configured sources the requester may subscribe to (never exposing secrets or keys).
- `triggers(action="update", trigger_id=..., ...)` is **owner-only** and updates a reactive trigger in place (`prompt`, `description`, `pattern`, `senders`, `include_bots`, `match`, `triage_*`, `cooldown_seconds`, `max_fires_per_hour`, `expires_in_hours`, `once`, `enabled`) while preserving `rule_uid`, `fire_count`, `last_fired_at`, room cursors, thread checkpoints, and thread-key bindings. Administrators can use `set_enabled` or `delete`, not edit another owner's rule. Pass `pattern=""` or `triage_instructions=""` to clear them, `senders=[]` to clear sender filtering, or omit (`None`) to keep existing values.
- `triggers(action="set_enabled", trigger_id=..., enabled=False)` disables a direct or reactive trigger; pass `enabled=True` to re-enable it.
- `triggers(action="delete", trigger_id=...)` removes a direct or reactive trigger.
- `triggers(action="rotate_key", trigger_id=..., public_key=..., key_id="default")` replaces the signing key on a direct Ed25519 trigger.
- `triggers(action="history", trigger_id=None, limit=20)` returns recent reactive firing rows (ordered newest-first, `limit` clamped to `1..100`) with operational metadata (`status`, `triage_reason`, `error_message`, `matrix_event_id`, timestamps) and a compact event preview (text truncated to 240 chars). For a non-owner administrator, `history` returns operational metadata with `event: null` (redacted).
- `triggers(action="test", trigger_id=..., text=None, actor=None, kind=None, sample_data=None)` dry-runs deterministic matching and optional live triage on a reactive trigger without writing to SQLite, consuming `once`/cooldown/rate-limit quotas, or posting to Matrix.

### Watching GitHub Pull Requests (`watch_pr`)

The `triggers` toolkit also exposes `watch_pr(repo, number, events=..., ...)` for native GitHub PR/CI watching on one open pull request (`events` defaults to `["activity", "ci", "merge", "close"]`, `reopen_on_fire=True`, `auto_disable_on_close=True`, and `expires_in_hours=720`).

- **Delivery modes (`delivery_mode`):**
  - **`"webhook"`:** Selected automatically at creation when an authorized shared `github` webhook event source covers `repo` **and** its `allowed_events` covers every requested category (`"pull_request"` always; `"issue_comment"`, `"pull_request_review"`, and `"pull_request_review_comment"` when `"activity"` is watched; at least one of `"check_run"` or `"check_suite"` when `"ci"` is watched). Webhook-backed watches do zero idle polling (`next_poll_at=None`) and wake the ETag REST poller immediately whenever a signed webhook arrives for `repo`. Later revocation fails closed without falling back to periodic polling.
  - **`"poll"`:** Selected at creation when no covering webhook source exists and `external_trigger_policy.github_pr_polling_enabled` is `true` (interval clamped to `min_github_pr_poll_interval_seconds..86400`, default `300s`).
- **Auth modes (`auth_mode`):** Uses the watch owner's connected GitHub credentials (`"app"` or `"token"`), falling back to `"anonymous"` only when no owner credentials exist. Unreadable owner credential stores fail closed and never downgrade to anonymous requests.
- **Auto-disable (`pr_closed`):** When `auto_disable_on_close=True` (default), closing or merging the PR disables the watch with `disabled_reason="pr_closed"` once any terminal firing settles (or immediately if close/merge is not subscribed). Retired `pr_closed` watches do not consume `max_reactive_triggers_per_owner` or `max_github_pr_watches_per_owner`, and re-enabling enforces both quotas and silently re-seeds the checkpoint.

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
See [`mindroom trigger send`](cli.md#trigger-send) for every option.
Use `--no-verify-tls` only for local development against a trusted endpoint.

The JSON request body has a required non-empty `kind` and `message`, and optional `event_id`, `title`, `thread_key`, and `data` object; other fields are rejected.

### Signing requests without the CLI

A watcher can sign requests itself with the Ed25519 private key.
Sign the UTF-8 string below, joined with `\n`, where `<path>` is the request path such as `/api/triggers/campground` (or `/api/event-sources/<source_id>` for a shared Ed25519 event source), `<timestamp>` is Unix seconds, `<nonce>` is a fresh random string, and the last line is the lowercase hex SHA-256 of the exact body bytes.

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
A key stays bound to its thread for 7 days after its most recent delivery; after that, or after `triggers(action="rotate_key", ...)`, the next delivery with the key opens a new thread.
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

A trigger delivers only when all of these hold:

- The trigger exists, is enabled, and `external_trigger_policy.enabled` is true (plus `external_trigger_policy.reactive_enabled` for reactive triggers).
- The owner is still allowed to talk to the target agent in the target room.
- The owner, or one of the owner's `authorization.aliases`, is currently joined to the target room; bot accounts do not count.
- The target agent or team is running and joined to the target room.
- For reactive `room_message` watches, the owner is also still joined to the watched source room and permitted to hear the sender.
- For reactive `external_event` subscriptions, the owner is still authorized in the source's `subscribers` (or is an administrator).

The triggered turn runs on behalf of the trigger owner.
For a private agent (`private.per`), the trigger wakes the owner's private instance, so a trigger created by `@alice:example.org` uses Alice's private state.
An administrator's trigger for a private agent wakes the administrator's private state.

## Troubleshooting Responses

For direct triggers (`POST /api/triggers/{trigger_id}`), a successful request returns `202` with `accepted: true`, the `event_id`, and the posted Matrix event ID, or `duplicate: true` when the `event_id` was already delivered.

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
| `503` | `External trigger target runtime is not available` | The target bot is not running or not joined to the room yet, or it changed while the request waited for a reload; retry later. |

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

The script lives in the agent's workspace, and the path is where the agent's shell finds it in MindRoom, in a dedicated worker, or in a `static_runner` sandbox that mounts the agent workspaces, as the Kubernetes sidecar does.
The [Docker Compose `static_runner` sandbox](deployment/sandbox-proxy.md#sandbox-proxy-isolation_1) cannot see agent workspaces, so a background task started from a shell there cannot run the script.
The script needs only Bash and curl.
It posts the summary to the room and thread where the callback was minted, which wakes the agent.
After a successful delivery the callback is used up and the script deletes itself; if delivery fails, the script remains so it can be run again.
Unused callbacks never expire, so delete abandoned ones with `triggers(action="delete", trigger_id=...)`.

### Network Access

The script calls the address of the MindRoom API server that was running when the callback was minted, including a non-default `--api-port`, or `http://127.0.0.1:8765` when none was running.
When the background process must reach MindRoom at another address, set `MINDROOM_URL` in MindRoom's own environment before the agent mints the callback, because the address is written into the script.
Point `MINDROOM_URL` only at a trusted MindRoom endpoint because the script sends its token there.

### Security

Each script contains a random bearer token that can only wake the agent, room, and thread captured when it was minted.
A missing or wrong token gets the same not-found response as an unknown trigger.
Callback deliveries pass the same [delivery requirements](#delivery-requirements) as signed triggers.
Use signed triggers for reusable integrations that need a stable identity.
