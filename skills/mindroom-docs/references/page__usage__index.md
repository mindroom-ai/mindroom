# Usage Tracking

MindRoom records content-free token usage for agent and team replies and the [helper and internal AI work](#token-usage) listed below, plus call duration for GPT-Live voice calls.
View it in the dashboard [Usage tab](#dashboard-usage-tab), fetch it over [HTTP](#usage-report-api) for reporting or cost tools, or let agents read it with the [`usage_stats`](#usage_stats) tool.
Reports show retained usage, not estimated spend or a billing ledger.

## Dashboard Usage Tab

The **Usage** tab shows organization-wide token usage from the [usage report](#usage-report-api) and uses normal dashboard authentication.

- All-time recorded totals for input, output, and cache tokens
- Daily activity with a UTC date range and an expandable data table
- Searchable agent/team, model, and requester breakdowns, sorted by the selected token counter
- Agent/team detail with requesters, cumulative models, and daily activity
- Requester detail across agents, models, and daily activity, including usage of shared agents
- Model detail across agents, requesters, and daily activity, keeping providers separate

The token selector also offers reasoning and audio counters when the provider reports them.
Select a row in any breakdown to open its detail panel.
Date ranges apply only to daily activity; agent and model totals stay cumulative.
Daily and requester detail can be lower than cumulative totals when older attribution is missing, and undated runs appear only in totals.
A run that used several models counts once in each model's run count, so do not add model run counts together.
Cache tokens may already be included in input counts, depending on the provider.

The tab waits automatically while a report is prepared.
**Refresh** requests the latest report; completed reports can be up to one minute old.

## Token Usage

This section describes what every usage view counts.

**Counters.** Each total has nine counters: `input_tokens`, `output_tokens`, `total_tokens`, `cache_read_tokens`, `cache_write_tokens`, `reasoning_tokens`, `audio_input_tokens`, `audio_output_tokens`, and `audio_total_tokens`.
Missing counters are reported as zero, so a zero does not prove a provider used none of that category.
Models are grouped by provider and model; usage that cannot be attributed to a model appears under model `unknown`.

**Runs.** `run_count` counts retained top-level AI replies with usable token metrics, not messages or conversations.
Team member tokens count toward model, requester, daily, and request detail without adding to `run_count`, and a team's session totals already include its members.
Helper work adds tokens without adding runs, including retries and rejected outputs:

| Request `kind` | Work |
| --- | --- |
| `run` | Ordinary agent or team reply |
| `compaction_summary` | Conversation compaction summaries |
| `memory_auto_flush` | Background memory auto-flush extraction |
| `dynamic_workflow` | Embedded Dynamic Workflow participants |
| `skill_learning` | Automatic [skill reviews](https://docs.mindroom.chat/skills/) |
| `prompt_curation` | Background [prompt curation](https://docs.mindroom.chat/memory/#prompt-curation) passes |
| `routing`, `room_topic`, `schedule_parse`, `thread_summary`, `voice_normalization`, `voice_transcription` | Internal AI work, reported under the entity `system:internal` |

Helpers are attributed to the conversation and requester that triggered them, and stay unattributed when no requester is known.
`system:internal` usage has no human requester, appears only in organization reports, and transcription is counted only when the provider reports tokens.
Embedding calls and calls to separately configured judgment backends, such as participation or mid-turn judges, are not counted.

**Requesters.** Usage is grouped by canonical Matrix user ID, with known aliases combined; `user_id: null` holds unattributed usage.
Shared and private instances of the same agent are combined in entity rows; `private_agent_breakdown` shows the private part separately.

**Retention.** Recorded usage survives compaction, message edits, and regeneration, and a regenerated reply counts as a separate run.
Erasing a whole session removes its usage; deleted sessions are unavailable.
Reports cover only agents in the current configuration, so removing an agent drops its usage from reports even though its sessions stay stored; removed and ad hoc teams stay included.
Usage from session history that predates usage tracking is imported once at startup; history already lost cannot be reconstructed, and old usage is never given invented dates or requesters.

**Coverage.** Each breakdown has a matching coverage object (`coverage`, `model_coverage`, `user_coverage`, `cumulative_model_coverage`, `daily_coverage`, `request_coverage`, `voice_coverage`, `private_agent_coverage`) with `scanned_sources`, `unavailable_sources`, and an explanatory `note`.
`unavailable_sources` counts sources with missing, unreadable, or incomplete data; it is not a missing-token percentage.
Because of these gaps, model, user, and daily rows do not necessarily sum to the top-level totals.
Totals support cost estimates, but provider-specific charging rules and calls outside retained storage are not captured.

## Usage Report API

`GET /api/usage` returns the organization-wide report under standard dashboard authentication, like other administrator APIs.
A collector service can fetch the same report from `GET /api/usage/export` with its own credential; see [Usage Export Service](#usage-export-service).

Both routes accept two optional query parameters, each defaulting to `false`:

| Parameter | Adds |
| --- | --- |
| `include_daily=true` | `daily_breakdown` and `daily_coverage`, plus a `daily_breakdown` inside every requester and private-agent row |
| `include_requests=true` | `request_breakdown` and `request_coverage` |

### Polling for a Report

The first request for a report returns `202` with `{"status":"pending"}` and `Retry-After: 5`.
Poll the same URL with the same parameters after that delay; once ready, the route returns `200` with the report.
Completed reports are cached for 60 seconds, and configuration changes discard cached reports.
A `503` means the report could not be prepared, for example because the committed configuration is unavailable; retry after a few seconds.
All responses use `Cache-Control: no-store` and contain no conversation content.

### Report Fields

Every completed report includes `schema_version: 1` and `generated_at`, the UTC ISO 8601 time the scan finished; cached responses keep that time.

| Field | Contents |
| --- | --- |
| `totals`, `session_count` | All retained usage |
| `breakdown` | One row per agent or team |
| `model_breakdown` | Retained runs by provider and model, with `run_count` |
| `cumulative_model_breakdown` | Session totals by provider and model, with `session_count` |
| `user_breakdown` | Per requester: `user_id`, `totals`, `run_count`, and its own `model_breakdown` |
| `private_agent_breakdown` | Private-agent usage per owner and agent; see [Private-Agent Rows](#private-agent-rows) |
| `voice_breakdown` | GPT-Live call duration; see [Voice Call Duration](#voice-call-duration) |

Each `breakdown` row has session `totals`, `session_count`, and `cumulative_model_breakdown`, plus `retained_run_totals`, `run_count`, and a `user_breakdown` showing which requesters used that agent or team.
Requester totals sum to `retained_run_totals`, which can differ from the cumulative session `totals`.

`cumulative_model_breakdown` comes from per-model totals stored with each session, so it includes compacted history still in retained sessions but has no dates or requesters.
A session using several models counts once in each model's `session_count`.

### Daily Rows

Each daily row has a UTC `date` (`YYYY-MM-DD`), `totals`, `run_count`, and a `model_breakdown`, sorted oldest first.
Dates come from individual request timestamps, falling back to the run's creation date when request detail is missing, which can shift usage across days.
Each run counts once on its first request date, so a later day can show tokens with `run_count: 0`.
Usage with no usable date is left out of daily rows but stays in all-time totals, so a user with only undated runs has an empty `daily_breakdown`.

### Request Rows

`include_requests=true` is available only on the two organization routes.
Each request row has `entity`, `user_id`, `provider`, `model`, `kind`, epoch-seconds `created_at`, and all nine counters in `totals`, sorted by time.
Rows keep individual provider calls apart so a consumer can apply per-request pricing, such as context-length tiers.
Rows contain no prices, prompts, responses, session IDs, or run IDs.
Only requests whose counters match their run's recorded totals are exported; missing detail marks `request_coverage` incomplete while aggregate totals stay available.

### Voice Call Duration

Each `voice_breakdown` row is one GPT-Live provider session, with `entity`, `user_id`, `provider`, `model`, epoch-seconds `created_at`, `duration_seconds`, and `finalized`.
Reconnecting starts a new row, even within the same call, and duration is not split across UTC days.
`finalized: false` means the duration is the last running total the provider reported, for example after a dropped connection.
Sum `duration_seconds` to group voice use by caller, agent, or model, and price it separately from the tokens of agents the call delegated to.
Voice duration does not add to token totals, request rows, or run counts, and rows contain no audio, transcripts, or session IDs.

### Private-Agent Rows

Each `private_agent_breakdown` row has `agent_name`, `user_id`, session `totals`, `session_count`, `cumulative_model_breakdown`, `retained_run_totals`, `run_count`, and `model_breakdown`.
Session totals are attributed to the private instance's owner, while retained runs keep the user who made the request.
Session totals can include older compacted history that has no model or daily detail.

To attribute usage by ownership instead of by requester, compute this per user and counter over the union of users in both breakdowns, summing a user's private-agent rows first and treating missing rows as zero:

```text
user_breakdown.totals - private_agent_breakdown.retained_run_totals + private_agent_breakdown.totals
```

For example, 60 retained tokens including 20 private-agent tokens, plus a private-agent session total of 50, gives `60 - 20 + 50 = 90`.
This view assigns private-instance usage to its owner: if Bob requested 20 tokens from Alice's private instance with 100 session tokens, Alice gets 100 and Bob gets none.
Keep `user_breakdown` unchanged when reporting who made requests.

### Consuming Reports

Tolerate additional response fields, and keep session totals and retained-run breakdowns distinct.
Treat each report as a snapshot; do not add successive all-time totals together.
Counter values can change between reports as coverage improves.

### Personal Private-Agent Usage

`GET /api/usage/me/private-agents` returns the signed-in user's usage across their own private agents, with the same rows as `get_my_private_usage()` but without `user_id` or `user_breakdown`.
The only accepted query parameter is `include_daily` (default `false`); any other parameter returns `400` `Usage target overrides are not accepted`.

The endpoint requires [strict JWT mode](https://docs.mindroom.chat/deployment/trusted-upstream-auth/#strict-jwt-mode) with a verified Matrix identity, like the [Connections portal](https://docs.mindroom.chat/deployment/trusted-upstream-auth/#connections-portal).
Without it, requests fail with `403` `Personal APIs require trusted signed authentication` or `Personal APIs require a verified Matrix identity`; an API key alone cannot select a user.
Any signed-in user can call it, and it grants no administrator or export access.

## Usage Export Service

Strict JWT deployments can let a collector service call `GET /api/usage/export` without giving it a browser or administrator identity.
The route returns the same report, parameters, and polling behavior as [`GET /api/usage`](#usage-report-api).

Configure [trusted-upstream strict JWT mode](https://docs.mindroom.chat/deployment/trusted-upstream-auth/#strict-jwt-mode), including `MINDROOM_TRUSTED_UPSTREAM_USER_ID_HEADER`, then add a dedicated service audience and client ID:

```bash
MINDROOM_USAGE_SERVICE_JWT_AUDIENCE=mindroom-usage-export
MINDROOM_USAGE_SERVICE_CLIENT_ID=usage-export-client.example.org
```

The service sends its assertion in the trusted-upstream JWT header on every request, including polls.
The assertion must be signed with `RS256` by the trusted-upstream issuer and JWKS, and contain valid `exp`, `iat`, `iss`, and `aud` claims, where `aud` is `MINDROOM_USAGE_SERVICE_JWT_AUDIENCE` rather than the browser audience.
It must also contain `type=app`, `common_name` exactly equal to `MINDROOM_USAGE_SERVICE_CLIENT_ID`, and an empty `sub`.

| Status | Meaning |
| --- | --- |
| `503` `Usage export service authentication is not configured` | Trusted-upstream auth, a strict-JWT setting, or a service setting is missing |
| `401` `Invalid usage service JWT` | Missing or invalid assertion |

A service assertion authorizes only `/api/usage/export`, not `/api/usage`, personal usage, configuration, or other administrator routes.

## `usage_stats`

The `usage_stats` tool lets an agent answer usage questions in chat; it is read-only.

| Function | Reports | Availability |
| --- | --- | --- |
| `get_my_usage()` | The caller's usage of the current agent; for a private agent, the caller's own instance | Always |
| `get_my_private_usage()` | Usage across all the caller's currently configured private agents | Always |
| `get_all_usage()` | Organization-wide usage of currently configured agents, all stored teams, and `system:internal` | Requires `admin_scope: true` and a caller listed in `administrators` |

Give an agent the self-service functions by adding the tool:

```yaml
agents:
  usage_assistant:
    display_name: Usage Assistant
    role: Report the caller's retained MindRoom usage
    tools:
      - usage_stats
```

Enable `get_all_usage()` with the per-agent `admin_scope` option (boolean, default `false`), which cannot be set in `defaults.tools`:

```yaml
administrators:
  - "@usage-admin:example.com"

agents:
  usage_admin:
    display_name: Usage Administrator
    role: Report retained MindRoom usage across configured entities
    tools:
      - usage_stats:
          admin_scope: true
```

All three functions accept `include_daily` (default `false`), which adds [daily rows](#daily-rows) and `daily_coverage`.

### Tool Responses

Responses are JSON with `status` and `tool` fields, plus `scope`, `totals`, `session_count`, `breakdown`, `coverage`, `model_breakdown`, and `model_coverage`.
`get_all_usage()` returns the organization report fields described in [Report Fields](#report-fields), except request rows.
Self reports leave `breakdown` empty.
`get_my_private_usage()` adds `cumulative_model_breakdown` and `private_agent_breakdown` without user IDs.
`get_my_usage()` omits cumulative model and private-agent fields and excludes the in-progress tool call.
On a shared agent it counts only the caller's retained runs, so it can miss usage after compaction.
On a private agent it reports the whole session totals, which include compacted usage and can exceed the retained model or daily detail.

Errors use the same envelope with a `code` and `message`:

| Code | Cause |
| --- | --- |
| `authorization_error` | `get_all_usage()` called without `admin_scope`, from another agent, or by a non-administrator |
| `context_unavailable` | No active requester or agent context, such as outside a conversation |
