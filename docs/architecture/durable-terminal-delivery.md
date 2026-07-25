# Durable terminal delivery

MindRoom streams a response by sending a placeholder and editing it repeatedly.
The last edit publishes the terminal outcome: the final body plus a terminal stream status.
Matrix can reject that last edit while limited-sync timeline recovery is pending, while the room is rate limited, or during a transport blip.

`mindroom.matrix.client_delivery` retries such a send immediately inside a bounded window.
When that window is exhausted the response outcome is already committed but nothing is visible, so without further repair the user is left with a partial stream forever.

Durable terminal delivery closes that gap: once MindRoom commits a terminal outcome, transient transport failure delays visibility but never loses it.

## Components

| Module | Responsibility |
| --- | --- |
| `mindroom.terminal_delivery` | Durable record, state machine, precedence, and JSON-backed store |
| `mindroom.terminal_delivery_worker` | Scheduling: wakeups, backoff, bounded concurrency, per-room ordering |
| `mindroom.delivery_gateway` | Records committed intents and performs one classified attempt |
| `mindroom.bot` | Owns store warm-up, worker lifecycle, and the sync-response wakeup |

## State machine

```text
record ---> pending ---> attempting ---> delivered
              ^             |  |  |
              |             |  |  +---> dead_letter   (permanent failure, retry budget, capacity)
              |             |  +------> superseded    (newer terminal intent, redacted or missing target)
              +-- retry_wait <-+        (transient failure, exponential backoff with jitter)
```

`delivered`, `superseded`, and `dead_letter` are settled; nothing re-enters the queue from them.

Every transition applies to shared in-memory state and is durably written under one advisory file lock before it becomes observable, so any crash leaves exactly one valid recoverable state.

## Persistence

One JSON file per entity: `<storage>/tracking/<entity>_pending_terminal_deliveries.json`, schema-versioned, written through `write_json_file_durable` (fsynced temp file, atomic replace, directory fsync) under `advisory_file_lock`.
An unreadable or wrong-schema file is quarantined and the worker continues with an empty store.
Malformed individual rows are dropped with a count logged; valid rows in the same file survive.

Each record persists only what is needed to reconstruct the delivery: owning entity, serialized `MessageTarget`, target event ID (absent for a plain send), anchor and source event IDs, terminal outcome kind, the rendered visible body, tool trace, extra content, delivery revision, runtime generation, attempt count, timestamps, next attempt time, and a classified error code.
It never persists clients, access tokens, auth headers, or raw exception payloads.

## Identity, ordering, and precedence

`delivery_id` is `sha256(entity, room, target_event_id, anchor_event_id)`, so every terminal intent for one visible target maps to one row.
`transaction_id` is `mindroom-td-<delivery_id>-<revision>`, giving at-least-once transport with an exactly-once visible effect: an identical retry reuses the same Matrix transaction ID, and an edit carrying identical content is idempotent regardless.

Ordering never uses wall-clock time.

1. A new intent with a different response correlation ID is by construction a later response turn for the same target, so it always supersedes and bumps `revision`.
2. Within one response turn, only a strictly stronger outcome replaces an existing row (`completed` > `cancelled` > `error`).
3. A delayed error or fallback can therefore never overwrite a successful final response, pending or already delivered.
4. Recording the same intent twice is a no-op that keeps the existing row and its attempt count.
5. A redacted or missing target event settles the row as `superseded`; it is never recreated.
6. A source-event redaction settles every row owned by that source.

## Retry policy

The immediate bounded retry in `client_delivery` runs first and is unchanged.
After it is exhausted, `DeliveryGateway` records the intent and classifies every later attempt:

| Classification | Matrix signal | Action |
| --- | --- | --- |
| transient | `SendRetryError`, `M_LIMIT_EXCEEDED`, network, 5xx, unknown | exponential backoff with jitter, capped |
| local precondition | client absent, room cache unsynced, device trust | transient; the runtime is expected to recover |
| superseded | target event missing or redacted | settle, no further attempts |
| permanent | `M_FORBIDDEN`, not in room, `M_TOO_LARGE`, malformed event | dead letter with a loud log |

Backoff honours a server `retry_after_ms` hint, is bounded above, and the retry budget itself is bounded; exhausting it dead-letters the row rather than looping.

## Worker lifecycle

The worker starts during bot startup, right after the durable store is warmed, and stays idle until the bot is running with a live, synced client.
It wakes on every applied sync response — limited-sync recovery closes its gaps while a sync is applied, which makes that the practical recovery-ready signal — and otherwise scans on a bounded interval, so correctness never depends on a single notification.

Attempts run under a global concurrency cap and a per-room ordering lock, so one blocked room cannot starve others and one room never has two concurrent terminal writes.
Sends are bound to the current bot's gateway, which resolves the live client per attempt; a replaced client or a config reload is picked up on the next attempt rather than retained.

Shutdown cancels the loop and returns every leased row to `retry_wait`.
A hard crash leaves rows in `attempting`; the next `warm()` converts leaked or expired leases back into due `retry_wait` rows.

## Interaction with stale-stream cleanup

Startup stale-stream cleanup repairs partial streams left by a restart by writing an interruption note and optionally auto-resuming the turn.
That must not fight durable delivery, so cleanup skips any target event a durable row still owns: no interruption note is written over a committed final response, and no duplicate turn is auto-resumed for work that has already been answered.

## Observability

Structured events, all free of response bodies and transport secrets:
`terminal_delivery_pending_recorded`, `terminal_delivery_intent_superseded_on_record`, `terminal_delivery_leases_recovered`, `terminal_delivery_startup_recovery`, `terminal_delivery_worker_wake`, `terminal_delivery_deferred`, `terminal_delivery_recovered`, `terminal_delivery_superseded`, `terminal_delivery_dead_letter`, `terminal_delivery_backlog_capacity_exceeded`, `terminal_delivery_store_quarantined`, `terminal_delivery_records_quarantined`, and `terminal_delivery_drain_completed` (backlog size, dead-letter count, oldest unsettled age, max attempts).

## Retention

Settled rows are compacted after a retention window and above a settled-row cap.
Unsettled rows are never dropped by age; an unbounded backlog is instead capped, and the oldest overflow rows are dead-lettered loudly rather than silently discarded.
