---
icon: lucide/bell-ring
---

# Agent Callbacks

Agent callbacks let an orchestrating agent hand a spawned sub-agent a one-shot wake-up handle with a single tool call.

A callback is an ephemeral bearer-token trigger bound at mint time to the current agent, room, and thread, materialized as a ready-to-run shell script.

The sub-agent needs only bash and curl: when it runs the script, the completion message lands in the minting thread and wakes the orchestrating agent.

If the sub-agent never fires the callback, an `on_expiry: notify` callback posts a timeout notice instead, so the orchestrator is woken exactly once either way.

## Model

Callbacks are managed by the `callback_manager` tool, not by authored per-callback YAML.

`config.yaml` contains only the global `callback_policy` for the feature.

Callback records live in primary-runtime control state under `<control-state>/callbacks/records.json`, next to the `external_triggers/` state.

Only a SHA-256 hash of the bearer token is stored server-side.

The raw token appears only in the generated script and in the `curl_snippet` returned by `mint_callback`.

The public API endpoint is `POST /api/callbacks/<callback_id>`.

## Configuration

Add the manager tool to agents that should be allowed to mint callbacks.

```yaml
agents:
  orchestrator:
    display_name: Orchestrator
    role: Spawn coding sub-agents and track their completion.
    model: default
    rooms: [lobby]
    tools:
      - callback_manager

models:
  default:
    provider: anthropic
    id: claude-sonnet-5

callback_policy:
  enabled: true
  default_ttl_seconds: 86400
  max_ttl_seconds: 604800
  max_uses_cap: 20
  max_active_per_owner: 20
  max_body_bytes: 65536
```

`enabled: false` makes callback endpoints return not found and makes `mint_callback` error cleanly.

Requested TTLs and use budgets are silently capped by `max_ttl_seconds` and `max_uses_cap`.

`max_active_per_owner` counts only live, unconsumed records for the requesting owner.

Admins for `list_callbacks` and `revoke_callback` across owners come from `external_trigger_policy.admin_users`, the shared manager-tool family admin list.

## Setup Flow

The orchestrating agent calls one tool in a live Matrix conversation:

```json
{
  "label": "issue-042 implementer",
  "ttl_seconds": 86400,
  "max_uses": 1,
  "on_expiry": "notify"
}
```

`mint_callback` binds the callback to the current agent, room, and thread; non-admin callers cannot target other rooms.

The result contains everything the orchestrator needs:

- `callback_id` - the record ID, also the endpoint path component.
- `script_path` - a ready-to-run script in the agent workspace at `.mindroom/callbacks/cb_<id>.sh` (mode 0700, in a gitignored directory).
- `curl_snippet` - a raw curl line for consumers without bash.
- `brief_snippet` - one sentence to paste into the sub-agent prompt.
- `expires_at` - the ISO expiry timestamp.

The whole consumer API surface is:

```bash
bash cb_a1b2c3.sh done "fix/issue-042: implemented, 89 tests pass"
bash cb_a1b2c3.sh failed "build broken on main, cannot proceed"
bash cb_a1b2c3.sh blocked "need decision: which API key to use"
bash cb_a1b2c3.sh progress "tests running, half done"
```

The script prints `OK: MindRoom notified (<label>)` on success and a clear `FAILED:` line with non-zero exit otherwise, so even weak agents can tell what happened.

## Endpoint

`POST /api/callbacks/<callback_id>` with `Authorization: Bearer <token>` and JSON body `{"status": "done|failed|blocked|progress", "message": "...", "data": {...}}`.

The token is compared against the stored hash in constant time.

A missing or wrong token answers 404, never revealing whether the callback exists.

A consumed or expired callback with a valid token answers 410 Gone with a clear JSON error.

Bodies above `callback_policy.max_body_bytes` answer 413.

On success the response is `{"accepted": true, "callback_id": "...", "uses_left": N, "matrix_event_id": "$..."}`.

Each accepted fire decrements the use budget; when it reaches zero the record becomes a consumed tombstone that keeps answering 410 until the expiry sweep removes it.

## Runtime Checks

Callback delivery reuses the external-trigger delivery path and gates.

The API checks the policy switch, the token hash, expiry and use budget, current owner authorization for the target room and agent, live router and target bot readiness in the resolved room, and live owner membership in the target room, then atomically claims a use and dispatches.

The delivered Matrix message is `🤖 <label> → **<status>**: <message>` with the target-agent mention, plus a JSON code block when `data` is present.

The message stamps the callback owner as trusted original sender, so private agents treat the wake as an owner turn exactly like external triggers.

## Expiry Semantics

Records store `expires_at` and a periodic sweep on the API maintenance tick collects expired records.

An expired callback that still has unfired uses and `on_expiry: notify` first posts `⏰ Callback '<label>' expired without firing (created <ts>)` into the target thread through the same delivery gates.

After the notice is delivered (or for `silent` and fully consumed records, immediately), the record and its generated script file are deleted.

If the delivery runtime is unavailable, the notice is retried on later sweep ticks for up to one day past expiry before the record is dropped silently.

## Security Notes

The callback URL plus bearer token is a capability: whoever holds the script can post up to `max_uses` messages into one bound thread before the TTL ends.

That blast radius is deliberate and small: the token grants no other API access, the target is fixed at mint time, and the server stores only the token hash, so a leaked records file does not leak live capabilities.

In unsandboxed mode the script sits in the agent workspace, which already implies this access level; in sandboxed or Kubernetes mode hand the sub-agent only the script, the same trust boundary as the task brief itself.

Revoke early with `revoke_callback(callback_id)`, which deletes the record and best-effort deletes the script.

## Callback or External Trigger?

| Concern | Callback | External trigger |
|---|---|---|
| Lifetime | One task, TTL-bounded, self-deleting | Long-lived watcher endpoint |
| Auth | Bearer token (hash stored) | Ed25519 request signing |
| Consumer needs | bash + curl only | `mindroom` CLI or signing code + private key |
| Target binding | Current room and thread at mint time | Configured room, thread, or new thread at creation |
| Replay control | Use counter (one-shot or N-shot) | Nonce and event-id replay windows |
| Timeout handling | Built-in expiry notice wakes the owner | None; pair with `schedule()` if needed |
| Best for | Spawned sub-agent completion, progress pings | Campground watchers, CI hooks, nightly research |

Use [External Triggers](external-triggers.md) for anything that outlives one task or needs stable signing identity.
