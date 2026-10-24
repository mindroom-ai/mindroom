# Portable Deferred Tools Design

This record explains why deferred tools on models without native tool search use a fixed search-and-call wrapper instead of loading tools into the request at runtime.
Native tool search on Claude and OpenAI Responses models is unchanged.

## Problem

Runtime loading gave each agent `list_tools`, `load_tool`, `unload_tool`, and `tool_search`, and `load_tool` added the toolkit to the provider `tools` array and its instructions to the system prompt.
Every provider caches a byte-identical prompt prefix, and the tool definitions and system prompt sit at the front of that prefix: Anthropic orders tools, system, then messages, and open-model chat templates render tools inside the leading system block.
Each load or unload therefore missed the cache for the whole conversation, and the same-turn continuation that rebuilt the agent after a load sent that uncached request immediately.
A line naming the currently loaded tools at the end of the system prompt also changed with every load.

## Decision

On every model without native tool search, an agent with deferred tools sends one fixed tool list for the whole session: its eager tools plus `tool_search` and `call_tool`.
`tool_search` returns the full definitions of matching functions, with the instructions they and their toolkits would render, as an ordinary tool result, so discovery only appends to the end of the conversation.
The model calls a discovered function through `call_tool(name, arguments)`, the shape of MCP's `tools/call`.
MindRoom rewrites that call to the real function before Agno resolves it, and presents every stored call to a deferred function as a `call_tool` call when it sends history to the provider.
Stored history, approvals, tool hooks, worker routing, background jobs, and the chat display therefore see only real function names, exactly as on the native path.
Deferred toolkits are built when the agent is built, as the native path already does, so no per-session loaded-tool state exists.
Agno registers only the first function of each name, so a deferred function whose name an earlier toolkit already provides is neither hidden nor offered by search.

The rewrite happens at the model boundary rather than inside a dispatching tool because approval, hooks, worker routing, and job execution attach to the real Agno function.
A dispatcher that invoked the real entrypoint from inside `call_tool` would bypass all of them.

The model calls discovered functions through the wrapper instead of by their real names because those names are absent from the declared tool list.
Inference servers that constrain tool names to the declared list (vLLM and llama.cpp grammars) cannot emit an undeclared name, and models are trained to call only declared tools.

## Rejected Alternatives

- **Placeholder declarations.**
  Declaring every deferred function up front with a one-line description and permissive parameters keeps real names and needs no rewrite.
  It costs roughly 20 to 40 tokens per function on every request and keeps a large catalog in front of the model, which deferral exists to avoid; MCP-heavy agents reach hundreds of functions.
- **Append-only runtime loading.**
  Appending revealed definitions to the end of the `tools` array keeps only the prefix before the new definition cached.
  Because templates render tools before the conversation, the conversation itself still misses the cache.
  OpenRouter's client-side deferral on endpoints without native support works this way.
- **OpenRouter `deferred_tools` (portable profile).**
  It implements the same search-and-call wrapper on OpenRouter's side for any model, but only for OpenRouter routes.
  Its state travels in encrypted reasoning that must be replayed byte-for-byte, so compaction, redaction rollback, interrupted-reply replay, model switches, and catalog changes after a config reload fail with `deferred_tools_history_changed` or `deferred_tools_catalog_changed`.
  It was also a beta that restricts `tool_choice` and was still rolling out per account in October 2026.
  Its model-facing wrapper names and schemas are not published, so there was nothing exact to copy.
- **Anthropic mid-conversation `tool_addition`.**
  It adds tool definitions inline without touching the cache, but only on recent Claude models, which already use native tool search.

## Assumptions and Constraints

- Building every deferred toolkit when the agent is built is acceptable, because the native path has done so since #1417.
- Providers accept a `call_tool` call whose `arguments` is a nested JSON object; weaker models sometimes send it as a JSON string, which the rewrite accepts.
- Deferred function arguments lose provider-side `strict` schema enforcement; Agno's existing argument parsing and validation return errors to the model so it can retry.
- Discovered definitions live in the conversation, so after compaction summarizes them away the model searches again.
- The rewrite bound to Agno internals with no public extension point, so each binding carried an `AGNO_COMPAT` record, until the agent loop's `rewrite_tool_calls` hook and the provider pipeline replaced the bindings and their markers when MindRoom [stopped running Agno's agent loop](2026-10-20-replace-agno-design.md).
- Minimal mode keeps calling deferred toolkits through `mindroom-agent`, which never changed the provider tool list.

## Evidence

A live three-turn conversation on `gemini-3.8-flash` (provider `google`) on October 19, 2026 computed with the deferred `calculator` toolkit twice, then called the deferred `sleep` toolkit, once on `main` and once on this change:

| | `main` | this change |
| --- | --- | --- |
| Correct replies | 3 of 3 | 3 of 3 |
| Distinct tool lists sent | 3 (4, 12, then 13 tools) | 1 (`call_tool`, `tool_search`) |
| Provider requests | 8 | 9 |
| Input tokens on the last request | 3011 | 2624 |

Gemini accepted the free-form `arguments` object of `call_tool`, which its schema conversion sends as an object without properties, and every deferred call went through `call_tool`.
Neither run reported cached tokens, because Gemini's implicit cache did not engage at these prompt sizes.

The same conversation on `gpt-6-luna` through OpenAI Chat Completions (`api: chat_completions`, which takes the portable path), on October 20, 2026, measured the cache directly:

| | `main` | this change |
| --- | --- | --- |
| Correct replies | 3 of 3 | 3 of 3 |
| Provider requests | 8 | 9 |
| Input tokens over all requests | 16,326 | 20,677 |
| Cached input tokens | 10,410 (64%) | 17,852 (86%) |
| Uncached input tokens | 5,916 | 2,825 |
| Requests after the first that read nothing from the cache | 2, each right after a `load_tool` | 0 |

Every request on this change read the whole previous request from the cache, while on `main` the request after each `load_tool` read none of it.
This change sends more input tokens in total, because search results carry full definitions and the model searched again on each turn, but less than half as many uncached ones.
Rerunning the identical conversation reuses the earlier run's cache, so only each side's first run is a clean measurement.
OpenAI rejects function tools on Chat Completions while reasoning is enabled, so both runs set `reasoning_effort: none`.

The stock `mindroom config init --provider openrouter` Mind agent, with its 15 deferred tools, ran three thread turns (tag the thread, remove one of the tags, search the web) on DeepSeek V4.1 Flash, GLM-5.3, and Qwen3.8-27B through OpenRouter on October 20, 2026, once on `main` and once on this change:

| | `main` | this change |
| --- | --- | --- |
| Completed turns | 9 of 9 | 9 of 9 |
| Distinct tool lists per run | 3 | 1 |
| Distinct system prompts per run | 3 | 1 |
| GLM-5.3 uncached input tokens | 95,785 | 50,610 |

GLM-5.3 is the clean cache comparison, because OpenRouter served both of its runs from the same upstream.
OpenRouter spread the DeepSeek and Qwen runs over two to four upstreams with separate caches, so their token counts measure routing rather than this change.
On this change the cached prefix broke only at turn boundaries, where the per-turn transient context that history replay drops differs; `main` breaks there too ([#3002](https://github.com/mindroom-ai/mindroom/issues/3002)).
