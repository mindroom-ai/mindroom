---
icon: lucide/activity
---

# Operational Log Events

This page covers how to monitor a running MindRoom: the health and readiness endpoints, the structured log events that are stable enough for metrics and alerts, and the debug logging you can turn on while troubleshooting.
Most other log event names are internal diagnostics and can change in any release.

See also [Routing & Responder Selection](../configuration/router.md), [Threads, Replies & Participation](../configuration/threads.md), and [Access Control](../authorization.md).

## Health & Readiness

| Method | Endpoint | Description |
|--------|----------|-------------|
| GET | `/api/health` | Liveness. Returns `200` with `{"status": "healthy", ...}`, or `503` with `"status": "unhealthy"` once startup is ready and an agent, team, or router has stopped syncing with Matrix |
| GET | `/api/ready` | Readiness. Returns `{"status": "ready"}` when startup has finished, otherwise `503` with `{"status": "<phase>", "detail": "..."}` |

Use `/api/health` for liveness probes and `/api/ready` for readiness probes in container orchestrators.

The `/api/health` body also contains:

- `last_sync_time`: the oldest last successful Matrix sync among running entities.
- `stale_sync_entities`: the names of entities whose Matrix sync is stale, when there are any.
- `e2ee`: decryption-failure counters (see [Troubleshooting Undecryptable Messages](../matrix.md#troubleshooting-undecryptable-messages)).
- `embedder`: `{"status": "failing", "detail": ...}` with the cause while the semantic-search embedder is failing; this never makes the endpoint unhealthy.

The `/api/ready` phases are:

| Phase | Meaning |
|-------|---------|
| `idle` | Process not started |
| `starting` | Startup in progress; `detail` says what it is waiting for |
| `ready` | Startup finished and requests are being served |
| `failed` | Startup or runtime failure; `detail` gives the cause |

While `mindroom run` waits for hosted pairing approval, only these two endpoints answer: `/api/health` returns `200` and `/api/ready` returns `503` with phase `starting` and detail `Waiting for local pairing approval`.

When an agent, team, or router receives no Matrix sync for 120 seconds, MindRoom restarts that entity's sync loop on its own.
After 180 seconds without a sync, `/api/health` returns `503` and lists the entity in `stale_sync_entities`.
While an entity is still making progress on a large backlog of received events, both wait up to [`MINDROOM_MATRIX_INGESTION_GRACE_SECONDS`](../configuration/index.md#matrix).
Set the liveness probe's `failureThreshold` high enough that the sync-loop restart can recover before the container is restarted.

## Collecting the Events

Set `MINDROOM_LOG_FORMAT=json` so each record is written as one JSON object per line to stderr and to the runtime log file under `<storage>/logs/`.
Every record carries `event`, `level`, `logger`, and `timestamp` next to its own fields, so log collectors that parse JSON lines can filter on `event` and extract numeric fields.

```json
{"sample_count": 1200, "p50_ms": 0.412, "p95_ms": 1.87, "p99_ms": 4.02, "max_ms": 11.3, "gc_collections_total": [5120, 466, 31], "max_lag_scheduled_at": "2026-01-01T12:00:41.250+00:00", "max_lag_observed_at": "2026-01-01T12:00:41.261+00:00", "event": "event_loop_scheduler_lag_summary", "logger": "mindroom.event_loop_stall", "level": "info", "timestamp": "2026-01-01T12:01:00.012345Z"}
```

`event_loop_scheduler_lag_summary` is an `info` record, so keep `LOG_LEVEL` at `INFO` or lower, or raise only that logger with `MINDROOM_LOGGER_LEVELS=mindroom.event_loop_stall:INFO`.
The other events on this page are `warning` records.
See [Environment Variables](../configuration/index.md#environment-variables) for the logging controls.

## Events

The event names and fields below are stable operator signals.
Each section lists the fields, a suggested alert, and where to look when it fires.
The thresholds are starting points to tune against your own baseline, and the queries are generic pseudo-queries to translate into your log platform's syntax.

### `event_loop_scheduler_lag_summary`

All agents, teams, and the router share one event loop.
Every 60 seconds MindRoom logs how late a heartbeat scheduled every 50 ms actually ran.

| Field | Meaning |
|-------|---------|
| `sample_count` | Heartbeat samples in the window |
| `p50_ms` | Median heartbeat lateness in milliseconds |
| `p95_ms` | 95th percentile heartbeat lateness in milliseconds |
| `p99_ms` | 99th percentile heartbeat lateness in milliseconds |
| `max_ms` | Worst heartbeat lateness in milliseconds |
| `max_lag_scheduled_at` | UTC time at which the worst heartbeat was due |
| `max_lag_observed_at` | UTC time at which the worst heartbeat ran |
| `gc_collections_total` | Cumulative garbage collections per generation since process start |

An idle or lightly loaded loop reports a `p50_ms` of a few milliseconds or less.
A sustained high median means the loop is saturated and replies slow down, even while `/api/health` reports healthy, because liveness tracks Matrix sync rather than loop latency.

```text
metric  = distribution of p50_ms
          from logs where event = "event_loop_scheduler_lag_summary"
alert if  median(metric) over 10 minutes > 20
          for 30 minutes
```

When it fires, look for `event_loop_stall_detected` and `event_loop_stall_ongoing` records, which include the blocking stack when one stall exceeds `MINDROOM_EVENT_LOOP_STALL_THRESHOLD_SECONDS` (default `5`).
Compare the frequency of `event_loop_gc_collection` records, which report garbage collections slower than 50 ms.
Repeated `large_streaming_edit_preview_prepared` records with a growing `original_size_bytes` in one room point at one long streaming response.
Setting `MINDROOM_EVENT_LOOP_STALL_THRESHOLD_SECONDS` to `0` or a negative value disables stall detection and this summary.

### `matrix_sync_stall_diagnostics`

Logged when one agent, team, or router goes 90 seconds without Matrix sync progress, and repeated at most every 90 seconds while the stall lasts.
It shows where that entity's sync and event-processing tasks are waiting, without message content, and does not affect health or readiness.

| Field | Meaning |
|-------|---------|
| `agent` | Name of the entity that made no progress |
| `no_progress_seconds` | Seconds since the last sync or event-processing progress |
| `sync_age` | Seconds since the last sync activity, or `null` before the first sync result |
| `generation` | Event-processing progress counter, or `null` when none is reported |
| `snapshots` | Up to four task snapshots, each with `task_name`, `await_chain`, `await_boundary`, and `truncated` |

Each report covers its own 90 seconds without progress, so five reports for one entity within 15 minutes mean at least seven and a half minutes, not necessarily continuous, without progress.

```text
metric  = count of logs where event = "matrix_sync_stall_diagnostics"
          grouped by agent
alert if  sum(metric) over 15 minutes >= 5 for any agent
```

Read the `await_chain` snapshots to see where each task is waiting.
Reports for every entity at once usually point at the homeserver or at a saturated event loop, so check `event_loop_scheduler_lag_summary` as well.

### `tool_call_limit_reached`

Each agent or team run has a tool-call budget, normally the entity's [`max_tool_calls_per_turn`](../configuration/agents.md#configuration-options).
Calls past the budget fail with a tool error, and this event is logged once when a run keeps requesting tools and is stopped, ending the turn with the text produced so far.

| Field | Meaning |
|-------|---------|
| `entity` | Agent or team name; dynamic workflow participants appear as `dynamic_workflow_<participant id>` |
| `budget` | The run's tool-call budget |
| `model_requests` | Model requests the run made before it was stopped |

```text
alert on  each log where event = "tool_call_limit_reached"
notify    at most once per hour
```

Check whether the run was a runaway loop, such as the same tool call repeated or calls to unknown tools, or legitimate long work.
For legitimate long work, raise that entity's `max_tool_calls_per_turn`.
Skill reviews and [prompt curation](../memory.md#prompt-curation) passes also log this record under the agent's name, with fixed budgets of 16 and 20 that `max_tool_calls_per_turn` does not change.

## Debug Logging

Every completed model request logs an `LLM usage` event with provider and model identifiers and, when the provider reports usage, input tokens, uncached input tokens, and a cache-read ratio, without prompt content.

To record full provider requests, enable `debug.log_llm_requests`:

```yaml
debug:
  log_llm_requests: true
  llm_request_log_dir: null
```

- `log_llm_requests` (bool, default `false`): write provider requests as JSONL, and record successful tool calls with timing in `<storage>/tracking/tool_calls.jsonl`, where `<storage>` is `MINDROOM_STORAGE_PATH` or the default `mindroom_data` (tool failures are always recorded there).
- `llm_request_log_dir` (string, default `null`, meaning `<storage>/logs/llm_requests`): directory for the request logs.

Request logs include prompts, messages, the tools sent to the provider, model parameters, and requester and source Matrix event metadata.
Credentials such as tokens, cookies, passwords, API keys, and authorization headers are redacted, but these files can still contain sensitive prompt, argument, and result data, so leave the flag disabled unless you are actively debugging.

Export `MINDROOM_TIMING=1` before startup to log timing for tool calls and one INFO-level `Dispatch pipeline timing` summary per turn, including `time_to_model_request_ms` and context, queue, payload, agent-build, and model spans.

```bash
LOG_LEVEL=DEBUG MINDROOM_LOG_FORMAT=json MINDROOM_TIMING=1 mindroom run
```

### Memory Growth

To find what grows the primary process's memory, set `MINDROOM_HEAP_PROBE_INTERVAL_SECONDS` in the process environment or the `.env` file next to `config.yaml`.
MindRoom then logs one `heap_type_probe` event per interval with the 25 most common object types and their counts, resident memory (`rss_bytes`), allocator totals (`arena_bytes`, `arena_in_use_bytes`, `arena_free_bytes`, `mmap_bytes`), and `python_allocated_blocks`.
Resident memory that grows while `arena_in_use_bytes` plus `mmap_bytes` and `python_allocated_blocks` stay flat suggests freed memory the allocator keeps.
Growth in those suggests live objects, which the type histogram may not show because it does not count strings, bytes, or numbers.
Each probe briefly pauses the event loop, so prefer intervals of several minutes on large processes.
Unset or `0` disables the probe (the default), and any other value below `60` fails startup.
