---
icon: lucide/chart-column
---

# Usage Tracking

## Dashboard Usage Tab

The **Usage** tab shows token usage across the deployment using the existing [usage report](#token-usage) and normal dashboard authentication.

- All-time recorded totals for input, output, and cache tokens
- Daily activity with a UTC date range and an expandable data table
- Searchable agent/team, model, and requester breakdowns, sorted by the selected token counter
- Agent/team detail with requesters, cumulative models, and daily activity
- Requester detail across agents, models, and daily activity, including usage of shared agents
- Model detail across agents, requesters, and daily activity, keeping providers separate

The token selector also exposes reasoning and audio counters when reported by the provider.
Select a row in any breakdown to open its detail panel, where the same token selector is available.
Date ranges apply only to daily activity; agent and model totals remain cumulative.
Daily and requester detail can be lower than cumulative totals when older attribution is missing.
Agent activity combines its requesters' dated runs; undated runs remain in recorded totals.
A run using several models appears once in each model's run count, so model run counts should not be added together.
Cache tokens may already be included in input counts, depending on the provider.
This page shows recorded usage, not estimated spend or billing totals.

The page waits automatically while a report is prepared.
Use **Refresh** to request the latest available report; completed reports may be cached by the server for up to one minute.

## Token Usage

`GET /api/usage` returns organization-wide retained usage under the same standard dashboard authentication as other administrator APIs.
`GET /api/usage/export` exposes the same report to a collector through the dedicated signed service assertion described in [Trusted Upstream Authentication](#usage-export-service).
The two routes share report preparation and caching while authenticating every request through their own policy.
When a report needs preparation, either route returns `202` with `{"status":"pending"}` and `Retry-After: 5`; after preparation succeeds, an authenticated poll returns the completed report.
Every report-state response uses `Cache-Control: no-store`.
Completed HTTP reports include `schema_version: 1` and a UTC ISO 8601 `generated_at` timestamp set when the scan finishes.
Cached polls return the original timestamp for that completed scan.

The completed organization-wide HTTP JSON includes overall `totals`, an entity `breakdown`, a `model_breakdown`, a `cumulative_model_breakdown`, and `user_breakdown`.
Organization reports include retained configured and ad hoc team sessions, including teams no longer present in the current configuration.
Each user has a canonical `user_id`, token `totals`, `run_count`, and their own `model_breakdown`.
Counters include input, output, total, cache read/write, reasoning, and audio tokens.
Models include their provider.
Stored per-model details split runs that use several models; older runs fall back to their recorded model.
Malformed or inconsistent model details retain the run's tokens under `unknown` and mark model coverage as incomplete.
Requester aliases are combined; `user_id: null` holds unattributed usage.

Each entity in `breakdown` also has `retained_run_totals`, `run_count`, and a `user_breakdown` with the same requester/model structure.
These fields show which requesters used each agent or team, using the same deduplicated usage snapshots as the report, including saved team member runs.
Entity rows combine shared and private instances; `private_agent_breakdown` identifies the private contribution separately.
Run counts measure retained top-level runs with usable token metrics, not messages or conversations, and can undercount historical activity.
Team member tokens contribute to model, requester, daily, and request detail without adding replies to `run_count`; cumulative team session totals already include those members.
Requester totals sum to the entity's `retained_run_totals`, which can differ from its cumulative `totals`.
The report-level `model_coverage` and `user_coverage` also apply to entity retained detail.

Organization reports include internal AI work under the `system:internal` entity: routing, room topics, schedule interpretation, thread summaries, voice normalization, and provider-reported transcription tokens.
These counters contribute to model, daily, and request totals without increasing conversation or AI reply counts.
System overhead has no assumed human requester and is excluded from personal and private-agent reports.
Request `kind` identifies the operation as `routing`, `room_topic`, `schedule_parse`, `thread_summary`, `voice_normalization`, or `voice_transcription`.
Only newly recorded provider counters are available; historical internal calls are not reconstructed, and transcription duration is not converted into estimated tokens.
A usage storage failure is logged without discarding a successful internal response.

`cumulative_model_breakdown` uses per-model details stored with session aggregates and includes compacted usage still present in retained sessions.
The same rows appear within each entity in `breakdown`.
Each row contains all token counters and `session_count`; one multi-model session counts once for every model it used, and duplicate entries for the same provider and model are combined first.
All token counters must reconcile to the session aggregate.
Missing, malformed, negative, or inconsistent details preserve the full session under `unknown` and mark `cumulative_model_coverage` incomplete.
Session aggregates do not provide dates or requester attribution for these model counters, and deleted sessions remain unavailable.

Use `GET /api/usage?include_daily=true` or `GET /api/usage/export?include_daily=true` to also return `daily_breakdown` and `daily_coverage`.
Each daily row includes a UTC `date`, combined token `totals`, `run_count`, and a `model_breakdown` with input, output, total, cache-read, cache-write, reasoning, and audio counters.
With `include_daily=true`, each entry in `user_breakdown` also includes its own `daily_breakdown` with that same row structure.
This also applies to requesters inside each entity's `user_breakdown`.
User aliases are combined before daily grouping, and the `user_id: null` entry includes daily unattributed usage.
Daily rows are sorted oldest first and use individual request timestamps when every counter reconciles to the recorded run and its per-model totals.
Each run counts once on its earliest request date, so a later day can contain tokens with `run_count: 0`.
Each model counts that run once on the first date it was used.
Older or unreconciled request details fall back to the run creation date, which can shift usage across days and is not an exact provider billing date.
When neither request details nor the run creation timestamp can date the usage, daily rows omit it and daily coverage is incomplete.
Users with only undated retained runs have an empty `daily_breakdown`; their all-time totals still include those runs.
The report-level `daily_coverage` applies to overall, per-user, and per-entity requester daily breakdowns.
Omitting `include_daily` or setting it to `false` leaves out the daily fields.
The API and agent tools share storage reading, aggregation, and serialization.

Use `GET /api/usage?include_requests=true` or `GET /api/usage/export?include_requests=true` to add `request_breakdown` and `request_coverage`.
This option defaults to `false` and is available only on these organization HTTP routes, not agent tools or personal usage APIs.
Each flat request row contains `entity`, canonical `user_id` (or `null`), `provider`, `model`, `kind`, an epoch-seconds `created_at`, and all nine token counters in `totals`.
Rows preserve individual provider calls, including cache counters, so a consumer can apply context-length pricing without treating a multi-call run as one large request.
No prices, provider thresholds, prompts, responses, session IDs, or run IDs are exported.
New request records include the provider and model used for that call, including runs resumed with a different model.
Requests are exported only when every counter reconciles exactly with both the run total and its per-model totals.
Older request records without model attribution can inherit a single known run model; ambiguous mixed-model history is excluded.
Missing, malformed, or inconsistent request detail also marks `request_coverage` incomplete while aggregate totals remain available.
Existing usage ledgers are not backfilled; initial migration can import request counters still present in retained messages, but missing history cannot be reconstructed.
Request rows are sorted by timestamp, and both request fields are omitted unless `include_requests=true`.
Daily and request options are independent; all four combinations have separate cached reports and share one concurrent scan limit.

Organization reports also include `voice_breakdown` and `voice_coverage` for GPT-Live calls.
Each voice row contains `entity`, canonical `user_id` (or `null`), `provider`, `model`, epoch-seconds `created_at`, `duration_seconds`, and `finalized`.
Missing caller attribution retains duration with `user_id: null` and marks voice coverage incomplete.
Rows represent provider sessions, so reconnecting creates a separate row even within the same call.
Duration comes from the provider's cumulative usage reports; repeated updates replace the saved snapshot instead of adding the cumulative total again.
`finalized: true` means the provider's final usage event was received and saved; otherwise duration is the latest reported running total, including calls interrupted by connection loss or a process crash.
`created_at` records when the provider session was first observed, and duration is not split across UTC days.
Sum `duration_seconds` across these rows to group voice use by caller, agent, or model; apply duration pricing separately from delegated agent token pricing.
Voice duration does not increase token totals, request rows, or AI reply counts, and delegated agent tokens continue through normal usage accounting.
Only new recorded calls appear; older voice duration and usage never reported or saved cannot be reconstructed.
These rows contain no audio, transcripts, conversation IDs, or provider session IDs and are restricted to organization reports.

Portable compaction summaries, background memory auto-flush extraction, automatic skill reviews, and embedded Dynamic Workflow participants contribute their returned provider counters to token totals and model, user, and daily views, including retries and rejected outputs.
Their request rows use `kind: compaction_summary`, `kind: memory_auto_flush`, `kind: dynamic_workflow`, or `kind: skill_learning`; ordinary run requests use `kind: run`.
Helpers contribute zero to `run_count`, preserving its AI reply count meaning.
Embedded workflow usage belongs to the exact caller conversation scope bound by the response runtime, including its private or team store, rather than the participant's synthetic session.
Helpers use the current trusted requester and remain unattributed when unavailable.
Compaction timestamps record when usage was saved after the provider response; other helpers preserve returned run and message timestamps.
Individual helper requests are exported only when returned message counters reconcile with the full helper usage; absent or partial request detail stays aggregate-only and marks request coverage incomplete.
Usage from exceptions or cancellation before Agno returns a helper run output remains unavailable.
Historical helper costs were not retained and cannot be reconstructed.

User and model breakdowns cover stored usage snapshots, including each saved team member's own counters once.
Provider execution saves record content-free usage in the same database transaction; later provider saves replace that run's snapshot.
Conversation-only rewrites preserve existing usage snapshots.
Compaction, edits, and regeneration keep usage already incurred; a regenerated reply with a new run ID contributes separately.
Explicit whole-session erasure removes its usage too.
Startup imports available old run rows and session blobs once, without reconstructing missing history from logs or inventing dates or requester identity.
The usage table and imported records commit atomically; an interrupted import rolls back and retries on the next startup.
A session database the startup import cannot read, or that is locked, is skipped with a warning instead of stopping startup, and its import retries when its agent or team next opens it.
Historical conversion lives in `legacy_usage_storage.py`; reporting reads the current usage table only.
Breakdowns can still differ from session totals when history lost before migration or unrecorded member usage lacks detailed attribution.
Deleted sessions are unavailable.
The `coverage`, `model_coverage`, and `user_coverage` fields describe missing sources and these limits.
`scanned_sources` counts discovered database candidates, including absent configured databases.
`unavailable_sources` also includes discovery failures and sources with incomplete metrics or attribution, so it is not a missing-token percentage or necessarily a subset of `scanned_sources`.
Verified historical aliases are ignored because their canonical directories are scanned separately; unverified symlinks remain coverage warnings.
This is a retained-usage report, not a billing ledger.
Responses contain no conversation content and use `Cache-Control: no-store`.

The organization-wide response also contains `private_agent_breakdown`, with one row per canonical `user_id` and `agent_name`.
Rows separate stored session `totals`, `session_count`, and `cumulative_model_breakdown` from `retained_run_totals`, `run_count`, and `model_breakdown`.
They include `daily_breakdown` when `include_daily=true`, with the same input, output, cache-read, cache-write, reasoning, and audio counters.
Session totals use a validated private-instance owner or the recorded session requester; retained runs preserve their recorded requester, falling back to the validated owner when missing.
Unknown ownership remains `user_id: null`, and `private_agent_coverage` reports unavailable attribution or metrics.
History compacted before usage migration can contribute to session totals without recoverable model or daily detail.

For an ownership-based view, combine private-agent session usage attributed to owners with retained usage outside private-agent instances attributed to requesters.
Group private-agent rows by `user_id` and replace their retained-run contribution to `user_breakdown` with their session totals.
For each token counter and user, calculate `user_breakdown.totals - private_agent_breakdown.retained_run_totals + private_agent_breakdown.totals`, summing private-agent rows first and treating missing rows as zero.
Calculate over the union of users in both breakdowns; users with no private-agent row retain their full `user_breakdown.totals`.
Use values from the same response and retain `user_id: null` as unattributed usage.
For example, 60 retained tokens containing 20 private-agent tokens, plus a private-agent session total of 50, gives 90 tokens: `60 - 20 + 50`.
This includes private history whose detail was lost before usage migration without counting its stored usage twice.
It also replaces recorded-requester attribution for private runs with session ownership: if Bob requested 20 retained tokens from Alice's private instance with 100 session tokens, this view assigns those 100 tokens to Alice and none to Bob.
Keep `user_breakdown` unchanged when reporting who made the retained requests; the combined view answers a different ownership question.
Usage outside private-agent instances still relies on retained requester-attributed runs; a shared conversation's recorded requester is not the owner of every run.
This combined view does not recover historical dates or per-model splits for the additional private session totals, and incomplete coverage still applies.

Consumers should tolerate additional response fields and preserve the distinction between session totals and retained-run breakdowns.
Coverage corrections can change counter values without changing the response shape.
Treat each export as a snapshot, rather than adding successive all-time totals together.

### Personal Private-Agent Usage

`GET /api/usage/me/private-agents?include_daily=true` returns the authenticated requester's usage across their configured private agents.
It shares the collector used by `get_my_private_usage()` and returns the same private rows, without `user_id` or an organization-wide `user_breakdown`.
Known requester aliases share one history; other users' databases and shared-agent databases are excluded.
The optional `include_daily` parameter defaults to `false`.

This endpoint requires trusted upstream authentication with signed JWTs and a verified Matrix identity, using the same identity checks as the personal Connections API.
An instance API key alone cannot select a personal user.
The requester comes only from authenticated identity; user, agent, and storage-path query overrides are rejected.
Ordinary authenticated users can access this personal endpoint, but their signed identity does not grant administrator dashboard access or access to the service export.

## Usage Export Service

Strict JWT deployments can expose `GET /api/usage/export` to a service client without granting that client a browser or administrator identity.
The route prepares the same organization-wide report as the standard dashboard-authenticated `GET /api/usage` route and accepts the same optional `include_daily` and `include_requests` query parameters, both defaulting to `false`.
With `include_requests=true`, the report adds reconciled provider-request token facts and request coverage as described in [Token Usage](#token-usage); missing request detail remains unavailable.
Both routes share background preparation and cache state while authenticating every request independently.
It never accepts an assertion from a query parameter, and methods other than `GET` are unsupported.

Configure trusted-upstream strict JWT mode as above, then add a dedicated service audience and exact client ID:

```bash
MINDROOM_TRUSTED_UPSTREAM_AUTH_ENABLED=true
MINDROOM_TRUSTED_UPSTREAM_USER_ID_HEADER=Cf-Access-Authenticated-User-Email
MINDROOM_TRUSTED_UPSTREAM_REQUIRE_JWT=true
MINDROOM_TRUSTED_UPSTREAM_JWT_HEADER=Cf-Access-Jwt-Assertion
MINDROOM_TRUSTED_UPSTREAM_JWKS_URL=https://gateway.example.org/.well-known/jwks.json
MINDROOM_TRUSTED_UPSTREAM_JWT_AUDIENCE=mindroom-dashboard
MINDROOM_TRUSTED_UPSTREAM_JWT_ISSUER=https://gateway.example.org
MINDROOM_USAGE_SERVICE_JWT_AUDIENCE=mindroom-usage-export
MINDROOM_USAGE_SERVICE_CLIENT_ID=usage-export-client.example.org
```

The service assertion must use `RS256` and include valid `exp`, `iat`, `iss`, and `aud` claims.
It must also contain `type=app`, a `common_name` exactly equal to `MINDROOM_USAGE_SERVICE_CLIENT_ID`, and an empty `sub` claim.
The issuer, JWKS URL, and assertion header come from the trusted-upstream settings, while `MINDROOM_USAGE_SERVICE_JWT_AUDIENCE` is separate from the browser audience.

The export route returns `503` until trusted-upstream auth, strict JWT mode, every shared strict-JWT setting, and both service settings are configured.
Missing or invalid assertions return `401`.
A valid service assertion authorizes only `/api/usage/export`; it does not authorize `/api/usage`, configuration APIs, personal usage APIs, or any administrator route.

Every request, including polls and cache hits, must include the service assertion.
When a report needs preparation, the route promptly returns `202` with `{"status":"pending"}`, `Retry-After: 5`, and `Cache-Control: no-store`.
Poll the same URL with the same `include_daily` and `include_requests` values after the requested delay.
Once preparation succeeds, an authenticated poll returns `200`, `Cache-Control: no-store`, and the existing aggregate report schema.
The four daily/request option combinations are prepared and cached separately, successful results expire 60 seconds after completion, and only one retained-data scan runs at a time.
Configuration or runtime changes discard earlier results.
On either organization route, a failed scan or unavailable committed configuration returns a content-free `503` with `Cache-Control: no-store`; scan failures remain cached for five seconds before another request can start a retry.

## [`usage_stats`]

`usage_stats` returns aggregate retained usage for the caller without modifying session storage.
All operations are local and read-only.
`get_my_usage()` always scopes the result to the current agent and the canonical current requester.
For a private agent, `get_my_usage()` remains inside the current user's exact private instance.
`get_my_private_usage()` reports all configured private agents belonging to the current requester, including retained stores under known requester aliases.
It resolves only that requester's databases and does not scan other users' private instances.
Shared-agent self reports cover retained Agno runs, while private self reports use the isolated Agno session aggregate.
The result excludes the in-flight tool call, and shared-agent self reports can be incomplete after compaction.
Provider billing, embedding usage, and speech-to-text usage are outside version one.

### Self-Service Configuration

Configure a normal agent with `usage_stats` to make `get_my_usage()` and `get_my_private_usage()` available.

```yaml
agents:
  usage_assistant:
    display_name: Usage Assistant
    role: Report the caller's retained MindRoom usage
    tools:
      - usage_stats
```

### Admin Configuration

Configure an admin agent with the per-agent `admin_scope` override to make `get_all_usage()` available.
Admin access requires both `admin_scope: true` and platform-administrator authority.

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

All three functions accept an optional `include_daily` boolean, which defaults to `false`.
`get_my_usage()` reports requester-attributed direct runs for a shared agent or the isolated session aggregate for a private agent.
`get_all_usage()` reports retained Agno session aggregates across configured agents and all stored teams, including ad hoc teams and teams removed from configuration.

### Daily Token Usage

Call `get_my_usage(include_daily=True)`, `get_my_private_usage(include_daily=True)`, or `get_all_usage(include_daily=True)` to include token usage per day.
The response adds `daily_breakdown`, with one row per UTC calendar date containing `date` (`YYYY-MM-DD`), token `totals`, `run_count`, and a `model_breakdown` grouped by provider and model.
Dates use individual request timestamps when all counters reconcile to the recorded run and its per-model totals; older or unreconciled details fall back to run creation time.
New requests retain their own model attribution; older requests can inherit a single known run model, but ambiguous mixed-model history cannot be split.
Each top-level run counts once on its first request date, so tokens on later days or from saved team members do not add extra replies.
Each model counts that run once on the first date it was used.
Dates are sorted oldest first and omit days without usable retained usage.
The daily breakdown follows the same requester and administrator access rules as the rest of the report.
Each day's combined totals and each model's totals separately include `input_tokens`, `output_tokens`, `cache_read_tokens`, and `cache_write_tokens`, alongside total, reasoning, and audio tokens.
The report's overall totals and overall `model_breakdown` expose the same counters.
For `get_all_usage(include_daily=True)`, each `user_breakdown` entry also includes a `daily_breakdown` with the same daily totals, run counts, and per-model rows.
Requester aliases share one user's daily history, and `user_id: null` contains unattributed daily usage.

`daily_coverage` reports scanned sources, unavailable or partially readable sources, and the retained-history limitation.
Usage with neither valid request timestamps nor a usable run creation timestamp is excluded from daily rows and marks its source as partially unavailable, while its tokens remain eligible for the other totals.
The run-date fallback cannot provide exact provider billing dates for historical or incomplete request detail.
This coverage applies to both overall and per-user daily rows; a user with only undated runs has an empty daily breakdown.
Daily rows include saved team member usage, but may differ from session totals when historical usage lacks retained counters or dates.
Both daily fields are omitted unless `include_daily=True`.

### Response And Coverage

All three functions return a JSON custom-tool envelope with `status` and `tool` fields.
A successful response also includes `scope`, token `totals`, `session_count`, an entity `breakdown`, `coverage`, a `model_breakdown`, and `model_coverage`.
Token totals separately report input, output, cache-read, cache-write, reasoning, and audio dimensions.

The admin response groups session aggregates by configured agent or stored team ID.
Self reports leave the entity `breakdown` empty; they still include `model_breakdown` and `model_coverage`.
Admin breakdown rows include every configured agent and stored team with retained usage and are sorted by total tokens.
Each entity also includes `retained_run_totals`, `run_count`, and `user_breakdown`, grouping retained usage by canonical requester and model.
With `include_daily=True`, these requester rows include daily detail too; the report's model, user, and daily coverage applies to them.
Shared and private instances of the same entity are combined; `private_agent_breakdown` separately identifies the private contribution.
Run counts describe retained top-level runs with usable metrics, not message counts, and requester totals sum to `retained_run_totals` rather than cumulative session `totals`.
Both responses group stored usage snapshots by provider and model in `model_breakdown`.
Organization detail includes each saved team member's own counters once, without adding replies to `run_count` or adding its tokens again to cumulative team session totals.
When a run stores detailed metrics for several models, each model receives its own tokens; repeated uses of the same provider and model within a run are combined.
Older runs without detailed metrics use their recorded provider and model, with missing identities reported as `unknown`.
Malformed model details or details that do not account for the run's token totals put the run's tokens under `unknown` and mark model and daily coverage as partially unavailable.
Model rows include token totals and run counts and are sorted by total tokens.
Each model counts a top-level run once, while daily and user run counts count that run once across all its models.
Coverage reports scanned sources, unavailable or partially unreadable sources, and the retained-history limitation.
Model coverage is reported separately because history lost before usage migration and unrecorded member usage can contribute to report totals without model attribution.
Consequently, model rows do not necessarily sum to the top-level totals.
The tool does not change Agno persistence settings.
The HTTP usage routes add `schema_version: 1` and a UTC ISO 8601 `generated_at` timestamp when a scan finishes; cached responses retain that scan timestamp.

Admin and all-private self reports also include `cumulative_model_breakdown` and `cumulative_model_coverage`.
Cumulative model rows use per-model details stored with session aggregates, so they include compacted usage still present in retained sessions.
Each row contains provider, model, all token counters, and `session_count`; a session using several models counts once in each model row.
Duplicate entries for the same provider and model within one session are combined before that count is added.
The details must reconcile every token counter to the session total; absent, malformed, negative, or inconsistent details preserve the full session under `unknown` and mark cumulative model coverage incomplete.
Session aggregates do not retain dates or requester attribution for these counters, and deleted sessions remain unavailable.
Shared-agent self reports omit the cumulative fields because their totals are requester-filtered retained runs rather than whole session aggregates.

`get_all_usage()` also includes recorded internal AI work under `system:internal`, including routing, room topics, schedule interpretation, thread summaries, voice normalization, and provider-reported transcription tokens.
This overhead contributes tokens but adds no conversations or top-level replies, and no human requester is inferred for it.
It is excluded from personal and private-agent usage reports. Historical internal calls and transcription tokens not reported by the provider remain unavailable.

### Private-Agent Accounting

`private_agent_breakdown` separates usage by `agent_name` and, for admin reports, canonical `user_id`.
Personal reports omit user identifiers and contain only the requester's own private agents.
`get_my_private_usage()` combines those agents in the overall totals; `get_all_usage()` includes private rows alongside the instance-wide report.

Each private row contains session `totals`, `session_count`, and `cumulative_model_breakdown`, plus `retained_run_totals`, `run_count`, and `model_breakdown`.
With `include_daily=True`, it also contains `daily_breakdown` using the same UTC dates and model/token counters as the other daily views.
Session totals can include history compacted before usage migration that no longer has model or daily detail; the two totals are intentionally separate.

For admin reports, session ownership comes from a validated private-instance identity record, falling back to the session's recorded requester.
Retained runs keep their recorded requester, with the validated owner as fallback when requester metadata is missing.
Known aliases are combined; missing ownership remains `user_id: null`.
`private_agent_coverage` describes unavailable attribution or metrics.
All views share the same storage reader, run deduplication, token normalization, and daily grouping.
Admin team totals use Agno's member-inclusive session aggregate without reading nested response content.
Usage snapshots survive compaction, edits, and regeneration; explicit whole-session erasure removes them.
A one-time startup import in `legacy_usage_storage.py` reads available Agno 2.x session blobs and Agno 3 run rows, including partly migrated databases.
The usage table and imported records commit atomically; an interrupted import rolls back and retries on the next startup.
A session database the startup import cannot read, or that is locked, is skipped with a warning instead of stopping startup, and its import retries when its agent or team next opens it.
The importer retains unknown timestamps and attribution and reports malformed records as coverage gaps.
Reports and tools read the current usage table without migrating or scanning conversation payloads.
History lost before migration cannot be recovered by this report.
Missing counters are reported as zero, so zero does not prove that an older provider recorded that token category.
These counters support cost estimates, but do not guarantee exact billing: provider-specific charging rules and calls outside retained session storage are not captured.

Errors use the same envelope with a stable code.
Common codes are `authorization_error` and `context_unavailable`.
