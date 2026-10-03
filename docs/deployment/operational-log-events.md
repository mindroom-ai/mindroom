---
icon: lucide/activity
---

# Operational Log Events

MindRoom names every structured log record with an `event` string.
Most event names are internal diagnostics and can change in any release.
The events on this page are stable operator signals for log-based metrics and alerts.
Their names and fields are pinned by `tests/test_operational_log_events.py`, which also checks the field tables on this page, so a rename or field change cannot land without updating this page in the same change.

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

## Suggested alerts

These thresholds are starting points; tune them against your own baseline.
The queries are generic pseudo-queries, so translate them into your log platform's metric and alert syntax.

| Event | Condition | Starting threshold |
|-------|-----------|--------------------|
| `event_loop_scheduler_lag_summary` | Median of `p50_ms` stays high | Above 20 ms for 30 minutes |
| `matrix_sync_stall_diagnostics` | Repeated reports for one `agent` | 5 or more for one `agent` within 15 minutes |
| `tool_call_limit_reached` | Any occurrence | Every record, with notifications rate-limited to about one per hour |

## Events

Each section lists the event's fields, a pseudo-query for its suggested alert, and where to look when the alert fires.

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
It does not change watchdog, liveness, or readiness behavior, as described in [Health & Readiness](../dashboard.md#health-readiness).

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
