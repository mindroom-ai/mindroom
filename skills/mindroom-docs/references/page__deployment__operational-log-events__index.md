# Operational Log Events

This page covers how to monitor a running MindRoom: the health and readiness endpoints, the structured log events that are stable enough for metrics and alerts, the container metrics worth alerting on, and the debug logging you can turn on while troubleshooting.
Most other log event names are internal diagnostics and can change in any release.

See also [Routing & Responder Selection](https://docs.mindroom.chat/configuration/router/), [Threads, Replies & Participation](https://docs.mindroom.chat/configuration/threads/), and [Access Control](https://docs.mindroom.chat/authorization/).

## Health & Readiness

| Method | Endpoint | Description |
|--------|----------|-------------|
| GET | `/api/health` | Liveness. Returns `200` with `{"status": "healthy", ...}`, or `503` with `"status": "unhealthy"` once startup is ready and an agent, team, or router has stopped syncing with Matrix |
| GET | `/api/ready` | Readiness. Returns `{"status": "ready"}` when startup has finished, otherwise `503` with `{"status": "<phase>", "detail": "..."}` |

Use `/api/health` for startup and liveness probes and `/api/ready` for readiness probes in container orchestrators.
Never point a startup probe at `/api/ready`: the one-time conversion after [upgrading to conversation storage v2](https://docs.mindroom.chat/deployment/upgrades/#upgrading-to-conversation-storage-v2) keeps the process not ready for as long as it takes, a probe restart rolls back the database being converted, and a database larger than the probe allows never finishes converting.
A startup probe on `/api/health` passes as soon as the process answers, as the bundled Helm charts configure it.

The `/api/health` body also contains:

- `last_sync_time`: the oldest last successful Matrix sync among running entities.
- `stale_sync_entities`: the names of entities whose Matrix sync is stale, when there are any.
- `e2ee`: decryption-failure counters (see [Troubleshooting Undecryptable Messages](https://docs.mindroom.chat/matrix/#troubleshooting-undecryptable-messages)).
- `embedder`: `{"status": "failing", "detail": ...}` with the cause while the semantic-search embedder is failing; this never makes the endpoint unhealthy.

The `/api/ready` phases are:

| Phase | Meaning |
|-------|---------|
| `idle` | Process not started |
| `starting` | Startup in progress; `detail` says what it is waiting for |
| `ready` | Startup finished and requests are being served |
| `failed` | Startup or runtime failure; `detail` gives the cause |

While `mindroom run` waits for hosted pairing approval, or converts conversation storage on the first start after an upgrade, only these two endpoints answer: `/api/health` returns `200` and `/api/ready` returns `503` with phase `starting` and detail `Waiting for local pairing approval` or `Converting conversation storage`.

When an agent, team, or router receives no Matrix sync for 120 seconds, MindRoom restarts that entity's sync loop on its own.
After 180 seconds without a sync, `/api/health` returns `503` and lists the entity in `stale_sync_entities`.
While an entity is still making progress on a large backlog of received events, both wait up to [`MINDROOM_MATRIX_INGESTION_GRACE_SECONDS`](https://docs.mindroom.chat/configuration/#matrix).
Set the liveness probe's `failureThreshold` high enough that the sync-loop restart can recover before the container is restarted.

## Collecting the Events

Set `MINDROOM_LOG_FORMAT=json` so each record is written as one JSON object per line to stderr and to the runtime log file under `<storage>/logs/`.
Every record carries `event`, `level`, `logger`, and `timestamp` next to its own fields, so log collectors that parse JSON lines can filter on `event` and extract numeric fields.

```json
{"sample_count": 1200, "p50_ms": 0.412, "p95_ms": 1.87, "p99_ms": 4.02, "max_ms": 11.3, "gc_collections_total": [5120, 466, 31], "max_lag_scheduled_at": "2026-01-01T12:00:41.250+00:00", "max_lag_observed_at": "2026-01-01T12:00:41.261+00:00", "event": "event_loop_scheduler_lag_summary", "logger": "mindroom.event_loop_stall", "level": "info", "timestamp": "2026-01-01T12:01:00.012345Z"}
```

`event_loop_scheduler_lag_summary` is an `info` record, so keep `LOG_LEVEL` at `INFO` or lower, or raise only that logger with `MINDROOM_LOGGER_LEVELS=mindroom.event_loop_stall:INFO`.
`agent_error`, `event_loop_stall_detected`, and `event_loop_stall_ongoing` are `error` records, and the other stable events below are `warning` records.
See [Environment Variables](https://docs.mindroom.chat/configuration/#environment-variables) for the logging controls.

## Events

The event names and fields below are stable operator signals; a record can carry further diagnostic fields that are not listed and can change.
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

When it fires, look for [`event_loop_stall_detected`](#event_loop_stall_detected) records, which carry the blocking stack of long stalls.
Every stall above `MINDROOM_EVENT_LOOP_STALL_STACK_THRESHOLD_SECONDS` (default `0.25`, `0` or a negative value disables) also logs one `event_loop_stall_stack` record with the blocking stack and the stall's `lag_ms` once it ends, at most once every 10 seconds.
Compare the frequency of `event_loop_gc_collection` records, which report garbage collections slower than 50 ms.
Repeated `large_streaming_edit_preview_prepared` records with a growing `original_size_bytes` in one room point at one long streaming response.
Setting `MINDROOM_EVENT_LOOP_STALL_THRESHOLD_SECONDS` to `0` or a negative value disables stall detection and this summary.

Once agents are ready, and then every `MINDROOM_HEAP_REFREEZE_INTERVAL_SECONDS` (default `3600`, `0` disables, any other value below `60` fails startup), MindRoom collects garbage once and excludes the surviving objects from later collections, logging `runtime_heap_refrozen`.
This keeps routine collections short on large processes at the cost of one full collection per interval.
After the first refreeze, MindRoom also raises CPython's full-collection threshold and logs `gc_full_collection_threshold_raised`, so routine full collections run less often, by an amount that depends on the workload; unreachable reference cycles then wait longer to be freed.
Memory held by objects that become garbage while frozen, such as agents a config reload replaced, is reclaimed only at the next refreeze, so resident memory can stay higher for up to one interval.

### `event_loop_stall_detected`

Logged when the shared event loop has run no heartbeat for `MINDROOM_EVENT_LOOP_STALL_THRESHOLD_SECONDS` (default `5`), so every agent, team, and router is frozen until it ends.
While the stall lasts, an `event_loop_stall_ongoing` record with the same fields follows every 30 seconds.

| Field | Meaning |
|-------|---------|
| `stalled_for_seconds` | Seconds since the last heartbeat |
| `threshold_seconds` | The configured stall threshold |
| `stack_capture_suppressed` | `true` when a stack was already captured in the last 30 seconds; the record then lacks the four fields below |
| `stack` | Stack of the code blocking the event loop |
| `process_cpu_seconds_since_heartbeat` | CPU seconds the process used since the last heartbeat |
| `other_thread_stacks` | Stacks of up to eight other threads |
| `omitted_thread_stack_count` | Number of other threads whose stacks were left out |

Count the records and alert when stalls repeat, since each one delays every reply in the process.
The `stack` names the blocking code; a `process_cpu_seconds_since_heartbeat` close to `stalled_for_seconds` points at CPU-bound work, and one near `0` at a blocking call such as file or network I/O.

### `event_loop_stall_ended`

Logged when a stall that `event_loop_stall_detected` reported ends.

| Field | Meaning |
|-------|---------|
| `stall_duration_seconds` | Seconds the event loop went without a heartbeat |

Sum `stall_duration_seconds` over a window to see how long agents were frozen in it.

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

### `matrix_event_decryption_failed`

Logged when an agent cannot decrypt a new message, in a room it has joined, from a sender it may reply to.
Every agent in the room that receives the message logs it, and a burst of failures in one encryption session can log fewer records than messages, so count distinct `event_id` values to approximate affected messages.
[Troubleshooting Undecryptable Messages](https://docs.mindroom.chat/matrix/#troubleshooting-undecryptable-messages) explains the usual cause and fix.

| Field | Meaning |
|-------|---------|
| `room_id` | Room of the message |
| `event_id` | Event ID of the message |
| `sender` | Matrix user ID of the sender |
| `sender_device_id` | Device the sender encrypted the message on |
| `session_id` | Encryption session of the message |
| `agent` | Agent that could not decrypt it |
| `key_request_already_sent` | Whether a key request for that session was already pending |
| `hint` | Fixed explanation of the usual cause |

Count it grouped by `agent` and alert on a sustained rate.
Failures from one `sender_device_id` point at that client, while failures across many senders for one agent point at that agent's device.

### `conversation_converted`

Logged once for each conversation database the first start after [upgrading to conversation storage v2](https://docs.mindroom.chat/deployment/upgrades/#upgrading-to-conversation-storage-v2) converts, or that converts at its first use later, such as a restored backup.

| Field | Meaning |
|-------|---------|
| `path` | The converted database file |
| `runs` | Runs in the converted database |
| `messages` | Stored messages in the converted database |
| `compacted` | Whether the file was compacted to return the space its old tables used |
| `duration_seconds` | Time the conversion and compaction took |

`compacted` is `false` when compacting would return little or the directory lacked the space the [upgrade notes](https://docs.mindroom.chat/deployment/upgrades/#upgrading-to-conversation-storage-v2) give for it; a `conversation_compaction_failed` warning names the database when compacting failed, and the conversion still stands.
A database that could not convert at startup logs `conversation_conversion_skipped_store` instead.

### `tool_call_limit_reached`

Each agent or team run has a tool-call budget, normally the entity's [`max_tool_calls_per_turn`](https://docs.mindroom.chat/configuration/agents/#configuration-options).
Calls past the budget fail with a tool error, and this event is logged once when a run keeps requesting tools and is stopped, ending the turn with the text produced so far.

| Field | Meaning |
|-------|---------|
| `entity` | Agent or team name; dynamic workflow `subagent` participants appear under the calling agent's name and `room_agent` participants under their own |
| `budget` | The run's tool-call budget |
| `model_requests` | Model requests the run made before it was stopped |

```text
alert on  each log where event = "tool_call_limit_reached"
notify    at most once per hour
```

Check whether the run was a runaway loop, such as the same tool call repeated or calls to unknown tools, or legitimate long work.
Each call to a tool the run does not offer logs a `tool_not_offered` warning, and each call whose arguments cannot be decoded logs an `invalid_tool_arguments` warning with `reason` `decode` or `not_object`; both carry `entity` and `tool_name`, never the arguments.
For legitimate long work, raise that entity's `max_tool_calls_per_turn`.
Skill reviews also log this record under the reviewed agent's name, with a fixed `budget` of 16 that `max_tool_calls_per_turn` does not change.

### `tool_call_failed`

Logged each time a tool call raises an error.
A call stopped together with its turn logs `error_type` `CancelledError`.

| Field | Meaning |
|-------|---------|
| `tool_name` | Name of the tool function |
| `agent_name` | Agent that made the call, or `null` |
| `error_type` | Class name of the error the tool raised |
| `error_message` | The error's message, with credentials redacted |
| `duration_ms` | Milliseconds from the call's start until it failed |
| `correlation_id` | ID shared by the tool calls that answer one message |
| `channel` | `matrix`, `openai_compat`, `mcp`, or `null` |

Count it grouped by `tool_name` and alert when one tool fails far more often than usual.
Failures that share a `correlation_id` came from a single turn, and `error_type` and `error_message` give the cause.
For a tool that uses an OAuth connection, also check for [`oauth_credentials_refresh_failed`](#oauth_credentials_refresh_failed) records.

### `oauth_credentials_refresh_failed`

Logged when MindRoom cannot refresh the access token of a stored [OAuth connection](https://docs.mindroom.chat/oauth-framework/), for a tool or an MCP server.

| Field | Meaning |
|-------|---------|
| `provider_id` | OAuth provider ID |
| `credential_service` | Credential service the connection is stored under |
| `reason` | `refresh_rejected`, `token_endpoint_changed`, or `provider_refresh_failed`, described below |
| `error_type` | `OAuthRefreshRejectedError` for the first two reasons, `OAuthProviderError` for `provider_refresh_failed` |
| `oauth_error` | The provider's OAuth error code, such as `invalid_grant`, `unrecognized` for a nonstandard code, or `null` |
| `has_refresh_token` | Whether the stored connection had a refresh token |
| `expires_at` | Expiry of the stored access token as a Unix timestamp, or `null` |

- `refresh_rejected`: the provider rejected the refresh grant, for example because it was revoked or expired; MindRoom removes the connection, and the account must be connected again.
- `token_endpoint_changed`: the provider's token endpoint changed since the account was connected, as [Troubleshooting OAuth Connections](https://docs.mindroom.chat/oauth-framework/#troubleshooting-oauth-connections) describes.
- `provider_refresh_failed`: any other failure, such as a network error or a provider outage; the connection is kept, so a later refresh can succeed.

A failure caused by an HTTP or network error also carries `transport_error_category`, `transport_error_type`, and, for an HTTP error, `http_status_code`.
Count it grouped by `provider_id` and `reason`.
Each `refresh_rejected` record is one connection that must be connected again, while repeated `provider_refresh_failed` records for one provider point at that provider, the network, or the OAuth client settings, which `oauth_error` `invalid_client` names.

### `provider_request_retried`

Logged each time MindRoom sends a model request again: after a transient provider failure, such as a rate limit, an overload, a server error, or a dropped connection, or after a [stalled stream](https://docs.mindroom.chat/configuration/models/#stalled-streams).
Each retry delays the reply, and a reply whose request fails every retry logs [`agent_error`](#agent_error).

| Field | Meaning |
|-------|---------|
| `model_id` | Model ID the request was sent to |
| `reason` | `transient_error` for a failure the provider or the network reported, `stream_stalled` for a stream that went silent past its idle limit |
| `error_type` | `RateLimited` for a throttled request, otherwise `ProviderUnavailable` |
| `status_code` | HTTP status of the failure, `504` for a stalled stream, or `null` when there is none, as for a dropped connection |
| `error` | The failure's message, redacted and truncated |
| `attempt` | Number of this retry for its `reason`, starting at `1` |
| `max_retries` | Retries allowed for its `reason` |
| `delay_seconds` | Seconds waited before this retry: the provider's `Retry-After` or Azure's `retry-after-ms` when it asked for at most a minute, otherwise a backoff that doubles with each retry |

Count it grouped by `model_id` and alert when one model is retried far more often than usual.
`error_type`, `status_code`, and `error` tell throttling apart from an outage or an unresponsive endpoint.

### `agent_error`

Logged each time an agent's or team's reply fails and the user gets an error message instead.

| Field | Meaning |
|-------|---------|
| `agent` | Agent or team name |
| `error_type` | Class name of the error that ended the reply |
| `error` | The error's repr, with credentials redacted |

These `error_type` values are stable; any other value is the class name of an unexpected error and can change:

- `RateLimited`: the provider throttled the request, after any retries that were safe.
- `ProviderUnavailable`: the provider or the network failed, or the stream stalled, after any retries that were safe.
- `AuthenticationFailed`: the provider rejected the credentials, or none are set up.
- `ContextWindowExceeded`: the conversation no longer fits the model's context window, or one request is larger than the account's tokens-per-minute limit.
- `InvalidRequest`: the provider rejected the request, such as a malformed or oversized body or an unknown model.
- `ModelSafeguardRefusal`: the model's safeguards refused the request.
- `ModelOutputLimitError`: the model's responses kept stopping at the output limit before their tool calls were complete.
- `ProviderError`: any other provider rejection, such as an HTTP `403` or an account without credit or past its spend or usage limit.

Count it grouped by `error_type` and alert when one value is far more frequent than usual.
`RateLimited` and `ProviderUnavailable` from many agents mean a provider is failing, while `InvalidRequest` or `ContextWindowExceeded` from one agent is more likely one conversation the provider cannot accept.

## Container Metrics

With the [Kubernetes worker backend](https://docs.mindroom.chat/deployment/kubernetes/#dedicated-worker-mode), idle worker Deployments scale to zero replicas by design, so exclude Deployments labelled `mindroom.ai/component=worker` from alerts on Deployments without an available replica.
Judge container memory by resident memory (RSS) or working set, not by total usage: total usage also counts the page cache that session databases, knowledge files, and logs fill, which the kernel reclaims under memory pressure.
The [heap probe](#memory-growth) reports the primary process's own `rss_bytes`.

## Debug Logging

When a request to a model under `models` ends, completed or not, MindRoom logs an `LLM usage` event with provider and model identifiers, the `stop_reason` of a reply that finished (`output_limit` or `content_filter` for one the provider cut off), and, when the provider reports usage, input tokens, uncached input tokens, and a cache-read ratio, without prompt content.

To record full provider requests, enable `debug.log_llm_requests`:

```yaml
debug:
  log_llm_requests: true
  llm_request_log_dir: null
```

- `log_llm_requests` (bool, default `false`): write provider requests as JSONL, and record successful tool calls with timing in `<storage>/tracking/tool_calls.jsonl`, where `<storage>` is `MINDROOM_STORAGE_PATH` or the default `mindroom_data` (tool failures are always recorded there).
- `llm_request_log_dir` (string, default `null`, meaning `<storage>/logs/llm_requests`): directory for the request logs.

Request logs include prompts, messages, the tools sent to the provider, model parameters, and requester and source Matrix event metadata.
Credentials such as tokens, cookies, passwords, API keys, authorization headers, and PEM private keys are redacted, but these files can still contain sensitive prompt, argument, and result data, so leave the flag disabled unless you are actively debugging.

Export `MINDROOM_TIMING=1` before startup to log timing for tool calls and one INFO-level `Dispatch pipeline timing` summary per turn, including `time_to_model_request_ms` and context, queue, payload, agent-build, and model spans.

```bash
LOG_LEVEL=DEBUG MINDROOM_LOG_FORMAT=json MINDROOM_TIMING=1 mindroom run
```

### Memory Growth

To find what grows the primary process's memory, set `MINDROOM_HEAP_PROBE_INTERVAL_SECONDS` in the process environment or the `.env` file next to `config.yaml`.
MindRoom then logs one `heap_type_probe` event per interval with the 25 most common object types and their counts, resident memory (`rss_bytes`), allocator totals (`arena_bytes`, `arena_in_use_bytes`, `arena_free_bytes`, `mmap_bytes`), and `python_allocated_blocks`.
Resident memory that grows while `arena_in_use_bytes` plus `mmap_bytes` and `python_allocated_blocks` stay flat suggests freed memory the allocator keeps.
Growth in those suggests live objects, which the type histogram may not show because it does not count strings, bytes, or numbers.
The histogram leaves out frozen objects and reports only their count as `frozen_objects`, so it shows what the runtime created since the last heap refreeze; set `MINDROOM_HEAP_REFREEZE_INTERVAL_SECONDS=0` to see growth over the whole process lifetime.
Each probe briefly pauses the event loop, so prefer intervals of several minutes on large processes.
Unset or `0` disables the probe (the default), and any other value below `60` fails startup.
