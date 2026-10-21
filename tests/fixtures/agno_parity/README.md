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
- Streamed goldens call Agno's `aresponse_stream`, the run loop's entry point, and stop after the first request completes; structured-output goldens call `aresponse`, the non-streaming path `Agent(output_schema=...)` takes.
- Every SDK request is answered at the httpx transport layer with `response_raw`, so the SDK clients production builds (MindRoom's pooled client, Agno's shared client, SDK-built clients) are unchanged.
- Each golden must produce exactly one provider request.

### Format

| Key | Content |
| --- | --- |
| `family`, `scenario` | The provider route and scenario names. |
| `input.model_config` | Authored `ModelConfig` fields (`provider`, `id`, `api`, `host`, `api_key`, `extra_kwargs`). |
| `input.messages` | Stored-message dicts in `Message.to_dict()` shape, oldest first; earlier turns carry `from_history: true` as replayed history does. |
| `input.tools` | `Function.to_dict()` dicts, which Agno's `_format_tools` turns into request tools. |
| `input.tool_choice` | Always `null`. |
| `input.response_format` | `null`, `{"type": "dict", "value": ...}`, or `{"type": "pydantic", "model": "module:Class", "json_schema": ...}`; it is the value Agno's agent resolves for the model's structured-output support, and replay fails when the class's JSON schema no longer equals `json_schema`. |
| `input.deferred_tool_search` | `null` or `{"mode": "native" \| "portable", "tool_names": [...]}`. |
| `input.portable_replay` | `null`, or the `configure_portable_replay` setting for Responses models. |
| `input.stream` | `true` for `aresponse_stream`, `false` for `aresponse`. |
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

Skipped where the route has nothing to pin:

- `reasoning`, `reasoning_replay`, `reasoning_tool_call`, `reasoning_tool_results`: `openai_chat` and `azure` (Chat Completions returns no reasoning text), `ollama`, `groq`, and `cerebras` (Agno parses no streamed reasoning on these routes); Responses routes have no `reasoning_tool_results` and use `responses_explicit_replay` instead.
- `prompt_cache`: `openai_chat`, `azure`, `deepseek`, `zai`, `llama_cpp`, `gemini`, `gemini_vertexai`, `ollama`, `groq`, and `cerebras`, which have no MindRoom or Agno prompt-cache setting.
- Claude-only scenarios run on `anthropic`, `vertexai_claude`, and `bedrock_claude`; the server tool-search blocks only on the first two, since Bedrock uses portable tool search.
- `deferred_tool_search_replay`: `anthropic` and `vertexai_claude`, covered by `claude_server_tool_blocks_replay`.
- Responses scenarios run on `openai_responses`; Codex always uses `store: false`, so it has `responses_explicit_replay` and no continuation or `store: false` variant.

## Prompt goldens (`prompts/`)

`prompts/<scenario>.json` holds every model request one scenario sends, as the provider adapter receives it from the request loop.
`tests/agno_parity/capture_prompts.py` builds each scenario through the production builders (`ai.stream_agent_response`, `teams.team_response_stream`, `matrix_rtc.call_tools.build_call_tools`, and each helper's call site), and `tests/agno_parity/test_prompt_goldens_current.py` proves the Agno-backed code still reproduces every file.
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
- Requests are grouped by the model instance that received them, in the order MindRoom built those models, and each model's requests stay in call order.
  A team builds its members before its leader, so member requests come first; members run concurrently, so their relative call order is not part of the contract.
- Collaborate-mode members all answer `ok`, because Agno's streaming broadcast result concatenates member output in arrival order, which is not deterministic.

Scenarios use Claude (`anthropic`, `claude-sonnet-5-5`) unless named otherwise.
Structured-output helpers are captured on each of Agno's three structured-output paths: on `anthropic`, which receives the schema as `response_format`; on `vertexai_claude`, which Agno asks for JSON in the system message with `{"type": "json_object"}`; and on `cerebras`, which receives `{"type": "json_schema", "json_schema": {"name", "schema"}}` and no JSON prompt.
Agent scenarios set `learning: false`, because Agno Learning is retired with Agno and its prompt block and tools have no owned equivalent.
