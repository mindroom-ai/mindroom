# Agno parity goldens

These fixtures pin what MindRoom sends and stores while Agno 3.0.9 still runs it, so the owned runtime can prove it changes no persisted data and no model-visible bytes (`docs/dev/2026-10-20-replace-agno-design.md`).
The capture scripts and `*_current.py` tests under `tests/agno_parity/` are deleted with Agno's runtime; the fixtures stay and the owned code's parity tests read them.
A deliberate change updates the golden in the same commit.

## Provider wire goldens (`providers/`)

`providers/<family>/<scenario>.json` holds one provider request and its parsed reply.
`tests/agno_parity/capture_providers.py` writes them and `tests/agno_parity/test_provider_goldens_current.py` proves the current code reproduces each one.
Regenerate with `.venv/bin/python -m tests.agno_parity.capture_providers [family ...]`; a rerun without code changes rewrites identical files.

### How a golden is captured

- The model is built by `get_model_instance` from `input.model_config`, so every hook production installs is active: request logging, the Claude prompt-cache ladder and history repairs, stream retry, and media fallback.
- The run-message patches that storage installs (`session_storage/agno_compat_run_messages.py`) are active, which is why every reply carries `provider_data.mindroom_model`.
- `input.deferred_tool_search` installs the native or portable deferred tool search exactly as `agents.py` does, and `input.portable_replay` calls `configure_portable_replay` as history replay planning does.
- `input.native_compaction` calls `configure_native_compaction` with its threshold and history generation, as history planning does when it enables native compaction.
- `input.tool_jobs` installs `install_tool_job_execution` as agent construction does, and the harness answers the per-turn job checks (a background job runtime, execution resources, and which functions are managed calls), because `Function.from_dict` cannot carry the assembled toolkit those checks read.
- `input.provider_tools_disabled` sends the request inside `without_provider_tools()`, as participation decisions and judgments do.
- Streamed goldens call Agno's `aresponse_stream`, the run loop's entry point, and stop after the first request completes; goldens with `input.stream: false` call `aresponse`, the non-streaming path of structured output, compaction summaries, and decision requests.
- Every SDK request is answered at the httpx transport layer with `response_raw`, so the SDK clients production builds (MindRoom's pooled client, Agno's shared client, SDK-built clients) are unchanged.
- Each golden must produce exactly one provider request.

### Format

| Key | Content |
| --- | --- |
| `family`, `scenario` | The provider route and scenario names. |
| `input.model_config` | Authored `ModelConfig` fields (`provider`, `id`, `api`, `host`, `api_key`, `extra_kwargs`). |
| `input.messages` | Stored-message dicts in `Message.to_dict()` shape, oldest first; earlier turns carry `from_history: true` as replayed history does. |
| `input.tools` | `Function.to_dict()` dicts, which Agno's `_format_tools` turns into request tools. |
| `input.tool_choice` | `null`, `"none"`, or a forced call in the shape the route's Agno adapter forwards: `{"type": "function", "function": {"name": ...}}` on Chat routes, `{"type": "function", "name": ...}` on Responses routes, and `"any"` on Gemini. |
| `input.response_format` | `null`, `{"type": "dict", "value": ...}`, or `{"type": "pydantic", "model": "module:Class", "json_schema": ...}`; it is the value Agno's agent resolves for the model's structured-output support, and replay fails when the class's JSON schema no longer equals `json_schema`. |
| `input.deferred_tool_search` | `null` or `{"mode": "native" \| "portable", "tool_names": [...]}`. |
| `input.portable_replay` | `null`, or the `configure_portable_replay` setting for Responses models. |
| `input.stream` | `true` for `aresponse_stream`, `false` for `aresponse`. |
| `input.provider_tools_disabled` | Present only as `true`, for a request sent inside `without_provider_tools()`. |
| `input.tool_jobs` | Present only as `{"managed": [...]}`, the functions a turn that can start tool jobs offers as managed calls. |
| `input.native_compaction` | Present only as `{"threshold", "history_generation"}`, the arguments of `configure_native_compaction`. |
| `request` | `method`, `url`, the stable `headers`, and the JSON `body` of the SDK request. |
| `response_raw` | `status`, `headers`, and one of `sse` (events as `{"event"?, "data"}`, where `data` is a JSON object or the literal `[DONE]`), `ndjson` (one object per line), or `json` (the body). |
| `parsed.message` | The assistant message's `Message.to_dict()` once the request completes, before the run loop resolves or runs its tool calls, plus `citations`, which `to_dict()` omits. |
| `parsed.metrics` | That message's `MessageMetrics.to_dict()`. |
| `parsed.stop_reason` | The first of `provider_data` `stop_reason`, `finish_reason`, and `response_status`, or `null` when the route records none. |

The harness writes each `sse` event as its `event:` and `data:` lines plus a blank line, and each `ndjson` entry as one JSON line; SDKs parse both, so the byte layout is not part of the contract.

### Normalization

- Files are pretty-printed with sorted keys, and capture runs from that same key-sorted input, so dict key order is not pinned; list order is.
- Request headers are dropped when they carry credentials (`authorization`, `x-api-key`, `api-key`, `x-goog-api-key`, AWS `x-amz-*` signing headers) or SDK, platform, and transport details (`x-stainless-*`, `amz-sdk-*`, `user-agent`, `x-goog-api-client`, `host`, `content-length`, `accept-encoding`, `connection`); every other header, such as `anthropic-beta`, `anthropic-version`, `chatgpt-account-id`, and the Codex session headers, is kept.
- `id` and `created_at` are dropped from `parsed.message`, and the wall-clock `duration` and `time_to_first_token` from `parsed.metrics`.
- uuid4 values Agno generates for tool calls sent without ids (every Ollama call, and the Gemini `tool_definitions` reply) become `<uuid4>`; a follow-up golden replays such a call with the id `parity-generated-call-<n>`.
- The Cerebras SDK's `/v1/tcp_warming` request, sent when a client is built, is answered and not recorded.

### Harness-supplied state

- Provider environment variables (`OPENAI_*`, `ANTHROPIC_*`, `GOOGLE_*`, `AWS_*`, `AZURE_*`, and the other provider prefixes) are hidden from SDK fallbacks during each capture.
- `<codex_home>` and `<kimi_home>` in `extra_kwargs` become directories holding fixed Codex (`auth.json` with an unexpiring token, account `parity-account`, `installation_id`) and Kimi (`credentials/kimi-code.json`) CLI credentials.
- `vertexai_claude` gets `client_params.credentials` and Gemini with `vertexai: true` gets `credentials`, both a static Google OAuth token, because JSON cannot carry credential objects.
- google-genai is switched from aiohttp to its httpx client for the call; the request body is the same.
- All credentials are synthetic; the goldens never reach a provider.

### Scenarios

| Scenario | Pins |
| --- | --- |
| `plain_turn` | System prompt and one user message, text reply. |
| `system_history` | `plain_turn` replayed as history, then a new prompt. |
| `tool_definitions` | Two tool schemas, one tool-call reply. |
| `complex_tool_definitions` | A toolkit function whose typed signature yields nested objects, an array of objects, nullable and defaulted parameters, an enum, and a dict, plus a raw MCP input schema with `$defs`/`$ref`, nullable `anyOf` and type lists, formats, bounds, and defaults; the reply calls the MCP tool with nested arguments. |
| `tool_results_history` | `tool_definitions` replayed with its tool result. |
| `parallel_tool_calls`, `parallel_tool_results` | Two calls in one reply, then both results replayed. |
| `foreign_tool_history` | A tool turn stored by another provider (Claude, or OpenAI Chat for Claude routes) replayed after a model switch. |
| `image_input`, `file_input`, `audio_input` | A 1x1 PNG, a tiny PDF, or an empty WAV on the prompt; routes that omit a kind pin the media-fallback note instead. |
| `reasoning`, `reasoning_replay` | A reasoning or thinking reply, then that turn replayed. |
| `reasoning_tool_call`, `reasoning_tool_results` | A reasoning reply that calls a tool, then the tool loop continued with its result, so signed thinking, reasoning content, and thought signatures replay inside one turn. |
| `claude_citations`, `claude_citations_replay` | A text document with citations, then the cited turn replayed. |
| `claude_server_tool_blocks`, `claude_server_tool_blocks_replay` | Native tool-search server blocks with a deferred tool call, then the turn replayed with its result. |
| `responses_continuation` | A stored response continued with `previous_response_id`. |
| `responses_explicit_replay` | On Responses routes, `reasoning_tool_call` runs under portable replay and this golden replays its items and result explicitly. |
| `responses_store_false_turn`, `responses_store_false` | The same reasoning tool turn and explicit replay with `store: false`. |
| `structured_output` | `mindroom.routing._RoutingSuggestion` as native schema, JSON schema, or JSON mode, per route. |
| `structured_output_nested` | `mindroom.scheduling.tasks.ScheduledWorkflow`, whose schema has `$defs`/`$ref`, nullable `anyOf`, defaults, a format, and a bound, the same way. |
| `prompt_cache` | A system prompt with the session-context boundary plus the route's cache settings. |
| `prompt_cache_disabled` | Claude with `cache_system_prompt` and `extended_cache_time` off. |
| `deferred_tool_search`, `deferred_tool_search_replay` | Deferred tools offered natively or behind the production `dynamic_tools` functions `tool_search`/`call_tool`, then a deferred call replayed. |
| `output_limit_stop` | A reply the output limit cut off mid-sentence, sent without streaming as a compaction summary is: Claude `max_tokens`, Chat and Ollama `length`, Gemini `MAX_TOKENS`, or an incomplete Responses response. |
| `output_limit_stop_tool_call` | A streamed reply the output limit cut off inside a tool call's arguments JSON. |
| `approval_gated_tools` | A function the approval policy gates (`requires_confirmation`, `approval_type` `mindroom_policy`) and a `run_subagent` stand-in marked for external execution, both through `apply_tool_approval_capability`, beside an ungated function; the reply calls the gated one. |
| `tool_job_wait_parameter` | A turn whose calls can become tool jobs: the managed function gains the optional `background_after` parameter, the other function keeps its schema, and the reply passes `background_after`. |
| `native_compaction`, `native_compaction_replay` | Native compaction with a 60,000-token trigger: the request's `context_management`, a reply that carries a compaction checkpoint, then the checkpoint and its tail replayed in place of the turns before it. |
| `tool_choice_forced` | A call of `get_weather` forced through `tool_choice`. |
| `tool_choice_none` | A decision request: `tool_choice` `"none"` with provider tools disabled and the tools still defined, sent without streaming. |
| `search_grounding` | Google Search grounding (`search: true`) on `gemini-3.1-pro-preview`, on Vertex AI at the `global` location: the search tool goes out in place of the two offered function tools, and the streamed reply carries grounding metadata, which the parsed message keeps only in `citations`, plus tool-use prompt tokens in its usage. |

Skipped where the route has nothing to pin:

- `reasoning`, `reasoning_replay`, `reasoning_tool_call`, `reasoning_tool_results`: `openai_chat` and `azure` (Chat Completions returns no reasoning text), `ollama`, `groq`, and `cerebras` (Agno parses no streamed reasoning on these routes); Responses routes have no `reasoning_tool_results` and use `responses_explicit_replay` instead.
- `prompt_cache`: `openai_chat`, `azure`, `deepseek`, `zai`, `llama_cpp`, `gemini`, `gemini_vertexai`, `ollama`, `groq`, and `cerebras`, which have no MindRoom or Agno prompt-cache setting.
- Claude-only scenarios run on `anthropic`, `vertexai_claude`, and `bedrock_claude`; the server tool-search blocks only on the first two, since Bedrock uses portable tool search.
- `deferred_tool_search_replay`: `anthropic` and `vertexai_claude`, covered by `claude_server_tool_blocks_replay`.
- Responses scenarios run on `openai_responses`; Codex always uses `store: false`, so it has `responses_explicit_replay` and no continuation or `store: false` variant.
- `output_limit_stop`: `codex`, whose streamed Responses reply raises `IncompleteResponsesStreamError` at `response.incomplete`, so there is no parsed reply.
- `output_limit_stop_tool_call`: Gemini and Ollama, which send each call whole, and the Responses routes, whose cut-off calls the harness does not reproduce.
- `approval_gated_tools`: Claude routes, Gemini, Ollama, and Cerebras, whose request tool definitions omit Agno's approval flags (see their `tool_definitions` goldens).
- `tool_choice_forced`: Claude routes, Ollama, and Cerebras, whose Agno adapters drop a caller's `tool_choice`.
- `native_compaction`, `native_compaction_replay`: run only on `anthropic`, `vertexai_claude`, `openai_responses`, and `codex`, the routes with native compaction.
- `search_grounding`: runs only on `gemini` and `gemini_vertexai`, the routes with Google Search grounding.

## Prompt goldens (`prompts/`)

`prompts/<scenario>.json` holds every model request one scenario sends, as the provider adapter receives it from the request loop.
`tests/agno_parity/prompt_scenarios.py` builds each scenario through the production builders (`ai.stream_agent_response`, `teams.team_response_stream`, `matrix_rtc.call_tools.build_call_tools`, and each helper's call site) and `tests/agno_parity/capture_prompts.py` writes the files.
`tests/agent_loop/test_prompt_parity.py` compares the helper requests and the first request of every Matrix reply with the owned path (the agent `create_agent` builds and the request the loop sends first), `tests/team_runtime/test_prompt_parity.py` the team goldens, and names the step that will compare each remaining golden.
Requests MindRoom sends through its own provider layer are recorded where the provider adapter receives them, below the request pipeline; requests the Agno team runtime still sends are recorded at the Agno model call.
Rewrite the fixtures with `.venv/bin/python -m tests.agno_parity.capture_prompts [scenario ...]` from the repository root.

Each file has these keys:

- `scenario` and `description` name what the file pins.
- `input` records what the scenario ran: the authored `config.yaml` mapping, workspace files, the turn's prompt and thread history, and the entry point.
- `requests` lists each request with the model `id` and `provider` string, whether it was a streaming call, its `messages`, its `tools`, its `tool_choice`, its `response_format`, and its `deferred_tool_names`.
- `exported` appears only where MindRoom hands model-visible values to a model outside its own loop: the realtime voice model's instructions and tool schemas, and the Live voice model's system prompt.

The capture replaces the provider call, so it sees every request hook MindRoom wraps around that call, such as portable tool search, but not the provider's own wire conversion, which the `providers/` goldens pin.
Messages are `Message.to_dict()` without the volatile `id`, `created_at`, and `metrics` keys, plus `"add_to_agent_memory": false` on transient context messages that are never stored.
`tools` holds the formatted tool definitions in the order the request loop passed them to the provider call.
`deferred_tool_names` lists, sorted, the tools Claude's wire hook sends with `defer_loading`; below the capture, that hook also adds the server-side tool search entry and moves the deferred tools after the others, so on that path `tools` is not the wire order.
It is empty when no tool is natively deferred, including on models whose deferred tools use portable tool search, which appears in `tools` itself.
A pydantic `response_format` is recorded as `{"pydantic_model": <class name>, "json_schema": <model_json_schema()>}`; a dict format is recorded as passed.

Normalization:

- The capture's temporary root becomes `<TMP>`, which appears in preloaded context-file headings.
- Today's date in the date context, such as `Monday, October 19, 2026`, becomes `<DATE>`; the timezone is pinned to `UTC`, and message timestamps and the schedule parser's current time are fixed inputs.
- A delegation's tool result names its record by a random 32-digit hex id under the day the delegation started; the ids become `<ID-1>`, `<ID-2>`, and so on in first-appearance order, and the day becomes `<DATE>`.
- Requests are grouped by the model instance that received them, in the order MindRoom built those models, and each model's requests stay in call order.
  A team builds its members before its leader, so member requests come first; members run concurrently, so their relative call order is not part of the contract.
- Collaborate-mode members all answer `ok`, because Agno's streaming broadcast result concatenates member output in arrival order, which is not deterministic.

Scenarios use Claude (`anthropic`, `claude-sonnet-5-5`) unless named otherwise.
Structured-output helpers are captured on each of Agno's three structured-output paths: on `anthropic`, which receives the schema as `response_format`; on `vertexai_claude`, which Agno asks for JSON in the system message with `{"type": "json_object"}`; and on `cerebras`, which receives `{"type": "json_schema", "json_schema": {"name", "schema"}}` and no JSON prompt.
Agent scenarios set `learning: false`, because Agno Learning is retired with Agno and its prompt block and tools have no owned equivalent.

## Tool goldens (`tools/`)

`.venv/bin/python -m tests.agno_parity.capture_tools` writes `tools/schemas.json`, `tools/schemas_all_enabled.json`, and `tools/arguments.json`, and `tests/agno_parity/test_tool_goldens_current.py` checks the current code against all three.
All three are pretty JSON.
The schema files list tools in sorted order and keep every other key in the order providers receive it, such as `parameters` as `type`, `properties`, `required`, with properties in signature order; the test compares serialized bytes, so a reordered key fails parity.
`arguments.json` has sorted keys and is compared by value.

### `tools/schemas.json`

The file maps each built-in tool registration name to `{"instructions", "add_instructions", "functions"}`.
`instructions` and `add_instructions` are the toolkit's own values; Agno adds `instructions` to the system prompt only when `add_instructions` is true, and no function adds instructions of its own.
`functions` lists the functions in the order Agno prepares them for an async run: sync functions in registration order, each replaced in place by an async variant of the same name, then async-only functions.
Each function records `name`, `description`, `parameters`, `strict`, `requires_confirmation`, `external_execution`, `approval_type`, `stop_after_tool_call`, and `show_result` after Agno's per-run preparation (`parse_tools`), which runs `process_entrypoint` and MindRoom's schema postprocessors.
The provider payload `Function.to_dict()` is the same entry, in the same key order, without `stop_after_tool_call`, `show_result`, and null values.
Only Agno's non-strict preparation is captured, because Agno makes functions strict only for a run with an output schema on a model with native structured outputs, and no MindRoom run that has tools sets an output schema.

Each tool is built by `build_agent_toolkit` with its default configuration, for one agent of a config shaped like `mindroom config init` output.
File memory gives that agent a workspace, so output-file wrapping adds `mindroom_output_path`, and the agent delegates and uses the knowledge graph, so the `delegate` and `knowledge_graph` tools exist.
`dynamic_tools` is built beside a deferred `calculator`, because tool search exists only with a deferred toolkit.
Tools that need settings to construct get dummy credentials (`DUMMY_CREDENTIALS`, `DUMMY_ENV`), and SDK clients that contact their service during construction are replaced by mocks (`OFFLINE_CLIENTS`).
The capture hides the host environment and refuses network connections, so the `update_awareness` instructions report an unknown latest release.

Entries leave out policy that agent construction applies after a tool is built: approval capability (which pauses, hides, or job-gates functions per `tool_approval`, and marks `delegate`'s `run_subagent` and `continue_subagent` as external execution), persona and function filters, hook bridges, and the per-turn `background_after` parameter that tool jobs add to managed calls.

Normalization: the running MindRoom version in the `update_awareness` instructions is replaced by `<MINDROOM_VERSION>`.

Not captured:

- `apify`, because its functions come from Apify Actor builds, which only the service returns.
- `composio`, because its functions come from the Composio action catalog, which only the service returns.

Agno behavior the golden pins as it is:

- `gemini`'s `generate_image` has no description and exposes only `mindroom_output_path`, because Agno cannot resolve its `Agent` annotation and falls back to an empty schema.
- `openclaw_compat` builds a toolkit without functions.

### `tools/schemas_all_enabled.json`

The file maps each captured tool that has function switches to `{"overrides", "instructions", "add_instructions", "functions"}`, built as for `tools/schemas.json` but with `overrides` as the agent's authored tool config.
`overrides` sets every boolean `all` and `enable_*` field to true, plus the extra settings in `EXTRA_ENABLING_OVERRIDES` that some functions need (`agentql_query`, `google_drive`'s `download_file`, `usage_stats`'s `admin_scope`); the dummy Slack credentials include a user token so `search_messages` registers.
Some switches replace a default function instead of adding one, such as `tavily`'s `enable_search_context`, so a tool's functions are the union of both files.

Every name in a tool's `ToolMetadata.function_names` appears in one of the two files, except the names in `NEVER_REGISTERED_FUNCTIONS`: async method names that Agno registers under their sync name, helper methods, and `wikipedia`'s knowledge-base function, which needs a knowledge base object that config cannot supply.

### `tools/arguments.json`

The file maps a case id to `{"function", "raw_arguments", "arguments", "kwargs"}` or `{"function", "raw_arguments", "arguments", "error_text"}`.
Each case sends one tool call, whose arguments string is `raw_arguments`, to a probe in `tests/agno_parity/tool_argument_probes.py` through Agno's `Model.get_function_calls_to_run` and `Model.arun_function_calls`.
`kwargs` is what the probe body received, each value encoded by `encode_kwargs` as `{"type": <Python type name>, "value": ...}`, so coercion such as `"3"` to `3` stays visible.
An injected framework object, such as the calling agent, is encoded as `{"type": "injected", "value": null}`, so the owned loop may inject its own stand-in.
`error_text` is the tool message content the model reads when the call fails.
`arguments` is the tool message's `tool_args`, which history keeps: the decoded arguments after Agno turns top-level `"true"`, `"false"`, `"none"`, and `"null"` strings into booleans and null, and drops values the model sent for injected parameters such as `agent`.
`arguments` is null when the model sent `[]`, and absent when Agno rejects the call before running it.
The probes import no Agno code, so owned implementations keep using them after Agno is removed.

Normalization: the pydantic version in error links (`https://errors.pydantic.dev/2.13/v/...`) is replaced by `<PYDANTIC_VERSION>`.
Decoding errors embed the message of Python's `ast.literal_eval`, and probes that skip validation fail with Python's own `TypeError` text, so both depend on the interpreter version (Python 3.13).

## Storage, history, and event goldens (`storage/`, `runs/`, `history/`, `events/`)

`tests/agno_parity/capture_storage.py` writes `storage/`, `runs/`, `history/`, and `events/`.
Every scenario runs MindRoom's real code path with real Agno agents and teams: `ai_response`, `stream_agent_response`, `team_response`, `team_response_stream`, the approval resume (`AgentApprovalExecution.continue_run` and `continue_paused_team_run`, called with the decisions, receipt, and continuation `ResponseRunner` builds), the OpenAI-compatible API, and MindRoom's storage, compaction, and reply-linkage functions.
Only the provider is replaced, by the synthetic provider answering from a script.
Regenerate with `.venv/bin/python -m tests.agno_parity.capture_storage`, which re-runs itself with `PYTHONHASHSEED=0`; never edit these files by hand.
`tests/session_storage/test_store_goldens.py` proves the owned store reads and writes every `storage/` file as Agno did.

### Scenarios

| Store | Session | Runs |
| --- | --- | --- |
| `agent_code` | `!room:example.org:$conversation` | `run-text` (blocking, cache and reasoning counters), `run-tools` (streamed, a tool call, then two parallel calls, then reasoning, `redacted_reasoning_content`, and `provider_data`), `run-media` (prompt with an image, audio, a video, a PDF, a UTF-8 text file, and a file path), `run-view` (`view_file` puts a viewed image on the tool-media follow-up message), `run-error` and `run-cancelled` (MindRoom's interrupted-turn records, stored `COMPLETED`), `run-paused-approval` (`PAUSED` on an approval-gated `multiply`, system message kept) |
| `agent_code` | `!room:example.org:$delegation` | `run-paused-delegation`: `PAUSED` on `run_subagent` external execution, with `metadata.mindroom_delegation` projecting the child's pending approval |
| `agent_code` | `!room:example.org:$approved` | `run-approved`: paused on `multiply` like `run-paused-approval`, then approved and continued, so the run is `COMPLETED` with its requirement resolved (`confirmation` true), the tool's result, and the follow-up answer; Agno records no metrics for a continued tool call |
| `agent_code` | `!room:example.org:$rejected` | `run-rejected`: paused the same way, then denied with a reason, so the requirement's `confirmation` and the tool's `confirmed` are false, both carry the reason as `confirmation_note`, and the tool message holds the reason |
| `agent_code` | `!room:example.org:$delegation-approved` | `run-delegation-approved`: paused like `run-paused-delegation`, then the child's call was approved, so the child and then the parent continued, and the `run_subagent` requirement holds the child's answer and delegation record as its external result |
| `agent_code` | `!room:example.org:$native` | `run-native-error` (`ERROR`) and `run-native-cancelled` (`CANCELLED`), as Agno saves them when no MindRoom turn record replaces them |
| `agent_code` | `!room:example.org:$compacted` | `run-c1` to `run-c5`; a compaction summary row covers `run-c1` and `run-c2`, an edit cut removed `run-c4` (a `_removed_runs` row), and a mid-turn fold row holds a segment of `run-c5`'s tool exchange, so `run-c5` loads without those messages; a `compaction_summary` usage row |
| `agent_helper` | `delegate:code:helper:<id>` | The delegated child runs: `PAUSED` on its approval for `run-paused-delegation`, and continued to `COMPLETED` for `run-delegation-approved` |
| `team_squad` | `!room:example.org:$squad` | Coordinate team runs `team-completed` (member used a tool), `team-member-error` (member row `ERROR`), and `team-paused` (member approval; the team row embeds the paused member) |
| `team_squad` | `!room:example.org:$squad-approved` | `team-approved`: paused like `team-paused`, then the member's call was approved and the team continued, so the member and team rows are `COMPLETED` |
| `team_panel` | `!room:example.org:$panel` | Streamed collaborate run `panel-completed` with two member rows |
| `agno2_migrated` | `session-1` | `tests/fixtures/agno_2_6_12_code_sessions.sql` after MindRoom's open-time migration (runs moved out of the `runs` blob, `session_data` still double-encoded) |

Every run with a provider request also has its usage row in `<table>_usage`.

### Files

- `storage/<store>.sql`: `iterdump` of each database; restore it with `sqlite3` `executescript`, as `tests.conftest.create_agno_2_sessions_db` does.
- `storage/stores.json`: each store's database path under the storage root, its sessions table, and its sessions with their stored session type.
  Open a restored store with `create_state_storage(name, <storage root>/<path without sessions/<name>.db>, subdir="sessions", session_table=...)`.
- `storage/loaded_sessions.json`: `AgentSession.to_dict()` or `TeamSession.to_dict()` of every session as `get_agent_session` or `get_team_session` loads it: runs after the scope's latest summary row plus every non-visible run and its descendants, folded messages removed, and the summary taken from the latest summary row.
- `runs/<type>.json`: `{case: dict}` for every value of that type nested in the stored `run_data` columns, summary-row segments, and `session_data`, exactly as stored.
  A case key is `<store>:<run id or segment:<row id>>` followed by its path, such as `agent_code:run-tools.messages[3].metrics`.
  The types are `run_output` (`RunOutput`), `team_run_output` (`TeamRunOutput`), `message` (`Message`), `image`, `audio`, `video`, and `file` (media dicts, read through `Message.from_dict` because Agno media has no `from_dict`), `tool_execution` (`ToolExecution`), `tool_call_metrics` (`ToolCallMetrics`), `run_requirement` (`RunRequirement`), `message_metrics` (`MessageMetrics`), `run_metrics` (`RunMetrics`), `model_metrics` (`ModelMetrics`), and `session_metrics` (`SessionMetrics`).
- `history/<store>__<session>.json`: `get_messages` results for the loaded session under each selection's keyword arguments, and `filter_tool_calls` applied to `copy_history_message` copies of a selection, as Agno's message builder does.
  Agno's builder passes `num_history_runs` as `last_n_runs`, `num_history_messages` as `limit`, `[system_message_role]` as `skip_roles` only for a role other than `user`, `assistant`, or `tool`, and the member's id as `agent_id` only for a team member.
- `events/ai_run.json`: the `run_metadata_collector` content, the `io.mindroom.ai_run` Matrix metadata, of every run that produced one; a turn stopped by cancelling its task and a turn paused for approval produce none, and a resumed run's entry is the metadata its resume produced.
- `events/openai_compat.json`: each `/v1/chat/completions` request with its status and body, parsed JSON when blocking and the raw lines of the SSE stream when streaming.
- `events/ai_run_cancelled.json`: the same content for a streamed and a blocking turn of `code` that Agno's run cancellation (`acancel_run`, which Stop also requests) ended after a completed tool call, in the sessions `$agno-cancelled` and `$agno-cancelled-blocking`.
  These turns run after the stores are dumped, so no store golden holds their rows.
  No golden holds a paused payload: MindRoom publishes one only for a pause it cannot turn into an approval, and every pause a MindRoom tool causes becomes an approval.
- `storage/<store>__before_<run id>.sql`: the sessions one approval resume reads, dumped just before it ran: the paused run's session and, for a delegation, its child's session in the child's store, with only those sessions' rows in every table keyed by session.
  Restore one like its store; the store golden holds the same sessions after the resume, so the two form a paused and resumed pair.
- `storage/response_linkage.json`: `agent_code:run-media` and `team_squad:team-completed` re-saved with `matrix_response_event_id` through `ConversationStateWriter.apersist_response_event_id_in_session_run`, as MindRoom does after delivering a reply, with each runs-table row before and after and its `run_data` decoded.
  The re-save runs after the stores are dumped, so the store goldens keep the rows as the turns saved them.
- `history/scoped_selections.json`: for every `<store>:<session>`, `get_messages` with the scope filter history replay passes (`agent_id` in agent stores, `team_id` in team stores), its run and message limits, and the prompt roles it skips, plus one selection by another scope of the same kind, which owns no run in the store.

Agno's own readers are not lossless.
Re-serializing a stored run adds `"events": []` to it and to every member response, drops an empty `tools` list, and drops `Audio.format`, so a row written back after Agno read it carries those changes.
The resumed runs are such rows, since Agno's continuation reads the paused run from storage before saving it, and so is any loaded run MindRoom edits and saves with `save_runs`.
The current-behavior test pins exactly these differences.
The store goldens carry no `matrix_response_event_id` linkage, which MindRoom adds with `save_runs` after delivering a reply; `storage/response_linkage.json` pins that re-save.

The `run-error` record holds the previous turn's answer followed by `(turn failed before completion)`, because the Agno-era interrupted-turn text fell back to replayed history when the failed request produced no assistant message; the golden keeps that stored legacy data, while the owned loop keeps only the failed reply's own text (`tests/test_agent_turn_runtime.py::test_an_error_record_never_repeats_an_earlier_answer`).

### Normalization

- The wall clock is frozen, and every turn starts at the next minute after 2027-01-15T08:00:00Z, so Unix timestamps run `1800000060`, `1800000120`, and so on.
- `time.time`, `datetime.now` in every `agno` and `mindroom` module, Agno's message, run, and tool timestamps, summary rows, requirement `created_at`, and the system prompt's date follow that clock, and local time is UTC.
- Agno's timers report every timed span as 0.5 seconds and every `time_to_first_token` as 0.25 seconds, since parallel tool calls run concurrently and would otherwise time in whatever order they ran.
- Random ids become sequential ones in first-appearance order across the dumps, the events, and then the pre-resume dumps, the linkage rows, and the cancelled-turn events: UUIDs become `00000000-0000-4000-8000-<n>`, 32-digit hex ids such as delegation, child run, correlation, and viewed-image ids become `<n>` padded to 32 hex digits, and completion ids become `chatcmpl-<n>` padded to 12 hex digits.
- The capture's temporary directory becomes `/mindroom-capture`.
- Dumps sort their `CREATE INDEX` statements, because Agno creates indexes in a different order on every run, and strip the trailing spaces SQLAlchemy leaves in table definitions.
- The capture runs with `PYTHONHASHSEED=0`, because Agno's `Model.to_dict` orders `agent_data.model` and `team_data.model` by iterating a set.

## Knowledge goldens (`knowledge/`)

`.venv/bin/python -m tests.agno_parity.capture_knowledge` writes `knowledge/embedders.json`, `knowledge/rows.json`, and `knowledge/search.json` from the Agno-backed knowledge path, and the owned knowledge code's parity tests in `tests/test_knowledge_goldens.py` read them.
The owned knowledge code replaced the path the capture drives, so the capture runs only on the commit that added it.
All three are pretty JSON with sorted keys and are compared by value.

### `knowledge/embedders.json`

The file maps an embedder case to `{"embedder", "calls"}`, where `embedder` is the authored `memory.embedder` block and each call names the Agno-era method (`agno_method`), the purpose that method implied (`query` or `document`), and its `texts`.
OpenAI-compatible and Ollama calls record each HTTP request's `method`, `url`, and JSON `body`; sentence-transformers calls record each call to the local model (`encode_query` or `encode_document`, its input, and its keyword arguments).
Each embedder is built by `create_configured_embedder`; mock transports answer every request, and a recording model stands in for the sentence-transformers weights.

### `knowledge/rows.json`

`input` holds the authored config, every source file, the fixed mtime they get, and the embedding rule: each chunk's vector is `tests.agno_parity.knowledge_vectors.parity_vector` of its text.
Each base is refreshed through the production refresh path into a real Chroma store, and `bases.<base_id>` records the published collection's `metadata`, its `configuration_json`, and every row's id, document, metadata, and embedding.
Rows are sorted by source path, page, row number, chunk, and document.

Normalization:

- Row ids, which hash a per-read uuid4 with the content hash, become `<md5>`.
- `content_hash`, which hashes the source metadata with a temporary snapshot path, becomes `<sha256>`, and `content_id`, its uuid5, becomes `<uuid5>`.

### `knowledge/search.json`

Each search sends one query through Agno's `search_knowledge_base` tool for an agent of `input.config`: `librarian` searches one base, `researcher` two, and `archivist` an empty one; the last search fails its query embedding.
A search records `documents`, the tool's JSON result parsed, or `text` for `No documents found` and the error text.
Agno renders each document from an unordered set and Chroma returns metadata in hash order, so the result's key order varies between processes; its values and its layout (`indent=2`, `ensure_ascii=False`) do not.
`content_hash` in each document's `meta_data` becomes `<sha256>`.
