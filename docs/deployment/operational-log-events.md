---
icon: lucide/activity
---

# Operational Log Events

MindRoom names every structured log record with an `event` string.
Most event names are internal diagnostics and can change in any release.
The events on this page are stable operator signals for log-based metrics and alerts.
Their names and fields are pinned by `tests/test_operational_log_events.py`, which also checks the field tables on this page, so a rename or field change cannot land without updating this page in the same change.

See also [Routing & Responder Selection](../configuration/router.md), [Threads, Replies & Participation](../configuration/threads.md), and [Access Control](../authorization.md).

## Health & Readiness

| Method | Endpoint | Description |
|--------|----------|-------------|
| GET | `/api/health` | Liveness endpoint that returns `503` with `{"status": "unhealthy", "stale_sync_entities": [...]}` for stale Matrix sync after startup is ready, while `/api/ready` owns startup state before readiness. |
| GET | `/api/ready` | Returns `{"status": "ready"}` when the orchestrator has finished startup. Returns `503` with `{"status": "<phase>", "detail": "..."}` otherwise |

MindRoom tracks runtime phases internally:

| Phase | Meaning |
|-------|---------|
| `idle` | Process not started |
| `starting` | Startup in progress (detail message available) |
| `ready` | Orchestrator booted, serving requests |
| `failed` | Startup or runtime failure (detail message available) |

When the semantic-search embedder is failing, the health payload additionally carries `"embedder": {"status": "failing", "detail": ...}` with the classified cause; this block is diagnostic only and never flips liveness.
Use `/api/health` for liveness probes and `/api/ready` for readiness probes in container orchestrators.
While `mindroom run` waits for hosted pairing approval, only these two endpoints answer: `/api/health` returns `200` and `/api/ready` returns `503` with phase `starting` and detail `Waiting for local pairing approval`.
Ordinary Matrix transport silence still reaches the watchdog after 120 seconds and makes `/api/health` return `503` after 180 seconds without a successful sync.
While Nio commits durable ingestion progress, the watchdog and `/api/health` consume the same monotonic progress snapshot and defer for up to `MINDROOM_MATRIX_INGESTION_GRACE_SECONDS` (default 600).
Both stop deferring when that grace expires or progress stops advancing for their respective silence timeout.
Successful sync completion refreshes both liveness clocks and clears the ingestion grace window.
After 90 seconds without sync or durable ingestion progress, `matrix_sync_stall_diagnostics` logs bounded await chains for that agent's sync, ingestion runner, ingestion pump, and delivery recovery tasks, including during first-sync and restarted-sync startup grace.
Reports repeat at most every 90 seconds and include sync age, time without progress, and ingestion generation without changing watchdog or readiness behavior.
Snapshots contain at most four tasks and 32 code locations per task, with strings capped at 240 characters; they exclude locals, message content, and source text, and stop at opaque Future or Task await boundaries.
[Operational Log Events](operational-log-events.md) lists this report's stable fields and a suggested alert condition.
Configure liveness probe `failureThreshold` to allow sufficient time for watchdog self-healing.

## Collecting the events

Set `MINDROOM_LOG_FORMAT=json` so each record is written as one JSON object per line to stderr and to the runtime log file under `<storage>/logs/`.
Every record carries `event`, `level`, `logger`, and `timestamp` next to the event fields below.
Container log collectors that parse JSON lines can then filter on `event` and extract numeric fields.

```json
{"sample_count": 1200, "p50_ms": 0.412, "p95_ms": 1.87, "p99_ms": 4.02, "max_ms": 11.3, "gc_collections_total": [5120, 466, 31], "max_lag_scheduled_at": "2026-01-01T12:00:41.250+00:00", "max_lag_observed_at": "2026-01-01T12:00:41.261+00:00", "event": "event_loop_scheduler_lag_summary", "logger": "mindroom.event_loop_stall", "level": "info", "timestamp": "2026-01-01T12:01:00.012345Z"}
```

`event_loop_scheduler_lag_summary` is an `info` record, so keep `LOG_LEVEL` at `INFO` or lower, or raise only that logger with `MINDROOM_LOGGER_LEVELS=mindroom.event_loop_stall:INFO`.
The other events on this page are `warning` records.
Setting `MINDROOM_EVENT_LOOP_STALL_THRESHOLD_SECONDS` to `0` or a negative value disables the event-loop stall detector and with it the scheduler-lag summary.
See [Environment Variables](../configuration/index.md#environment-variables) for the logging controls.

## Events

Each section lists the event's fields, a pseudo-query for its suggested alert, and where to look when the alert fires.
The alert thresholds are starting points; tune them against your own baseline.
The queries are generic pseudo-queries, so translate them into your log platform's metric and alert syntax.

### `event_loop_scheduler_lag_summary`

The runtime runs every agent, team, and router bot on one asyncio event loop.
The event-loop stall detector schedules a heartbeat callback every 50 ms and records how late each callback runs.
A native watcher thread, not a task on the loop, logs one summary for every completed 60-second window that collected samples.

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
A sustained high median means the loop is saturated and replies slow down, which can happen while `/api/health` still reports healthy, because liveness tracks Matrix sync and durable ingestion progress rather than loop latency.

```text
metric  = distribution of p50_ms
          from logs where event = "event_loop_scheduler_lag_summary"
alert if  median(metric) over 10 minutes > 20
          for 30 minutes
```

When it fires, look for `event_loop_stall_detected` and `event_loop_stall_ongoing` records, which include the loop thread's stack when one stall exceeds the stall threshold (5 seconds by default) unless `stack_capture_suppressed` is true.
Compare the frequency of `event_loop_gc_collection` records, which report garbage collections slower than 50 ms.
Repeated `large_streaming_edit_preview_prepared` records with a growing `original_size_bytes` in one room point at one long streaming response.

### `matrix_sync_stall_diagnostics`

Each agent, team, and router bot owns its own Matrix receive loop.
When one of them goes 90 seconds without sync or durable ingestion progress, this record logs bounded await chains for that entity's tasks, and it repeats at most every 90 seconds while the stall lasts.
It does not change watchdog, liveness, or readiness behavior, as described in [Health & Readiness](#health-readiness).

| Field | Meaning |
|-------|---------|
| `agent` | Name of the entity whose receive loop made no progress |
| `no_progress_seconds` | Seconds since the last sync or durable ingestion progress |
| `sync_age` | Seconds since the last sync activity, or `null` before the first sync result |
| `generation` | Durable ingestion progress generation, or `null` when none is reported |
| `snapshots` | Up to four task snapshots for the entity's sync, ingestion runner, ingestion pump, and delivery recovery tasks, each with `task_name`, `await_chain`, `await_boundary`, and `truncated` |

A homeserver interruption that lasts a little over 90 seconds without a receive-loop restart logs one report per entity.
Each restart starts a fresh 90-second timer, so with `MINDROOM_MATRIX_SYNC_STARTUP_TIMEOUT_SECONDS` below 90, first syncs that keep timing out can restart the loop before it ever logs this report.
Each report covers its own 90 seconds without sync or durable ingestion progress, so five reports for one entity within 15 minutes mean at least seven and a half minutes, not necessarily continuous, without that progress.

```text
metric  = count of logs where event = "matrix_sync_stall_diagnostics"
          grouped by agent
alert if  sum(metric) over 15 minutes >= 5 for any agent
```

Read the `await_chain` snapshots to see where each task is waiting.
Reports for every entity at once usually point at the homeserver or at a saturated event loop, so check `event_loop_scheduler_lag_summary` as well.

### `tool_call_limit_reached`

Each agent or team run has a tool-call budget, normally the entity's `max_tool_calls_per_turn`.
Calls past the budget return a tool error, and a run that keeps requesting tools ends after its budget plus two model requests.
The refused model request never reaches the provider, the turn completes with the text produced so far, and this record is logged once for that run.

| Field | Meaning |
|-------|---------|
| `entity` | Agent or team name; dynamic workflow participants appear as `dynamic_workflow_<participant id>` |
| `budget` | The run's tool-call budget |
| `model_requests` | Model requests the run made before the next one was refused |

```text
alert on  each log where event = "tool_call_limit_reached"
notify    at most once per hour
```

Check whether the run was a runaway loop, such as the same tool call repeated or calls to unknown tools, or legitimate long work.
Skill reviews also log this record under the reviewed agent's name, with a fixed `budget` of 16 that `max_tool_calls_per_turn` does not change.
For legitimate long work in an agent or team turn, raise that entity's `max_tool_calls_per_turn`, as described in [Agents](../configuration/agents.md).

## Debug Logging

Every completed model request logs an `LLM usage` event with provider and model identifiers and, when the provider reports usage, input tokens, uncached input tokens, and a cache-read ratio, without prompt content.

Set `debug.log_llm_requests: true` to log provider requests as JSONL under `debug.llm_request_log_dir` (default `mindroom_data/logs/llm_requests`).
Those records include prompts, messages, the tools sent to the provider, model parameters, and requester and source Matrix event metadata.
The same flag records successful tool calls with timing in `mindroom_data/tracking/tool_calls.jsonl`; tool failures are always recorded there.
Credential-bearing fields such as tokens, cookies, passwords, API keys, and authorization headers are redacted, but these files can still contain sensitive prompt, argument, and result data, so leave the flag disabled unless you are actively debugging.

Export `MINDROOM_TIMING=1` before startup to log timing for tool calls and one INFO-level `Dispatch pipeline timing` summary per turn, including `time_to_model_request_ms` and context, queue, payload, agent-build, and model spans.

```bash
LOG_LEVEL=DEBUG MINDROOM_LOG_FORMAT=json MINDROOM_TIMING=1 mindroom run
```

To find what grows the primary process's memory, set `MINDROOM_HEAP_PROBE_INTERVAL_SECONDS` in the process environment or the config-adjacent `.env`.
The primary then logs one `heap_type_probe` event per interval with the 25 most common object types and their counts, resident memory (`rss_bytes`), allocator totals under `malloc` (`arena_bytes`, `arena_in_use_bytes`, `arena_free_bytes`, `mmap_bytes`), and `python_allocated_blocks`.
Resident memory that grows while `arena_in_use_bytes` plus `mmap_bytes` and `python_allocated_blocks` stay flat suggests freed memory the allocator keeps, and growth in those suggests live objects, which the type histogram may not show because it counts only garbage-collected container objects, not strings, bytes, or numbers.
Each probe briefly pauses the event loop, so prefer intervals of several minutes on large processes.
Unset or `0` disables the probe (the default), and any other value below `60` fails startup.

See [Operational Log Events](../deployment/operational-log-events.md) for stable log events to alert on.
