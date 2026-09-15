# Agno compatibility boundaries

MindRoom isolates private Agno bindings, copied SDK paths, and upstream repairs in `agno_compat_<subject>.py` modules beside their owners.
Each workaround documents its reason, verified upstream issue/PR or explicit tracking gap, removal condition, and behavioral tests in source comments.
The maintenance policy lives in `AGENTS.md` through its `CLAUDE.md` target.

## Boundary inventory

Paths below are relative to `src/mindroom/`.
Related workarounds can share one module, but keep separate removal conditions when an upstream fix covers only part of it.

| Compatibility module | Agno adaptation | Policy and lifecycle owner |
| --- | --- | --- |
| `agno_compat_session_persistence.py` | Async Agent/Team persistence bindings for the owned synchronous store. | `agent_storage.py` and storage lifecycle callers. |
| `agno_compat_sqlite.py` | Private pragma listener removal, monotonic run insertion, atomic run/legacy deletion transaction, and private cache counters. | `agent_storage.py` retains journaling choice, prompt sanitization, descendant selection, diagnostics, and legacy scrub policy. |
| `agno_compat_knowledge.py` | Search error propagation and private insertion/status plumbing with owner validation. | `strict_knowledge.py` retains shared-index scope and complete-embedding requirements; knowledge managers retain publication/lifecycle ownership. |
| `agno_compat_openai_embedder.py` | Copied sync/async/batch request paths with explicit request/error/validation hooks. | `openai_embedder.py` retains input and dimensions policy, safe errors, response validation, health reporting, and the product sync-batch API. |
| `agno_compat_approval.py` | Private rejected-tool result creation and temporary continuation tool-lookup interception. | `approval_tools.py` retains exact call/run identity, authorization, and denial state. |
| `agno_compat_model_hooks.py` | Model message projection, post-tool formatting/media callbacks, invocation/retry method binding, and provider client factories. | Approval receipts, queued notices, request logging, media fallback, Claude retries, and prompt caching remain in their owning modules. |
| `agno_compat_prepared_tools.py` | Private Agent/Team tool preparation and temporary Team instruction restoration. | `history/prompt_tokens.py` owns estimation/cache policy; `matrix_rtc/call_tools.py` owns execution context, authorization, and LiveKit conversion. |
| `history/agno_compat_message_builder.py` | Roleful Team input and history-media message-builder bindings. | Agent/Team construction installs the patch; history owners retain replay policy. |
| `tool_system/agno_compat_tool_hooks.py` | Private FunctionCall sync/async hook-chain adaptation. | `tool_system/tool_hooks.py` owns deferred-result handling, synchronous execution, approvals, dispatch, and cancellation. |
| `tool_system/agno_compat_function_schema.py` | Per-Function schema processor binding and rebinding after copies. | `tool_system/output_files.py` retains schema transformation, workspace/path rules, execution, and receipts. |
| `agno_compat_openai_chat.py` | OpenAI Chat parser repairs and retained terminal metadata. | `openai_models.py` retains provider classes and canonical history/replay policy. |
| `agno_compat_openai_responses.py` | Responses stream completion, response-ID publication, retry protection, and continuation adaptation. | `openai_models.py` retains request configuration and native/portable replay policy. |
| `agno_compat_openai_responses_items.py` | Capture of provider output items lost by Agno parsing. | `openai_response_replay.py` retains canonical filtering, output ordering, and replay selection. |
| `agno_compat_claude.py` | Claude sampling and terminal metadata adaptation. | `claude_compat.py` retains typed refusal and native-compaction behavior. |
| `agno_compat_vertex_claude_tools.py` | Non-mutating removal of unsupported provider-level strict flags before Agno tool formatting. | `vertex_claude_compat.py` retains context fitting, token counting, and native checkpoint policy. |
| `custom_tools/agno_compat_website_reader.py` | Private crawl queue, visited state and copied loop with owner callbacks. | `custom_tools/website.py` retains server-fetch and redirect validation, exact-host rules, extraction, budgets, results, and sanitized logging. |
| `custom_tools/agno_compat_github_errors.py` | Capture of typed failures across serialized Agno results and prefix/logger binding. | `custom_tools/github.py` retains credential ownership and refresh, PyGithub requester policy, OAuth recovery, and sanitized output/log messages. |
| `oauth/agno_compat_google_auth.py` | Private credential resolver, original resolver binding, and registered Function entrypoint adaptation. | `oauth/client.py` and Google tool owners retain credential state, refresh, service-account fallback, locking, scopes, and user prompts. |

## Installation and ownership

Installers are explicit and idempotent, and preserve the existing order of model wrappers.
The bindings call owner-supplied callbacks for behavior such as message projection, retry classification, and schema transformation.
They do not choose approval outcomes, move storage ownership, change retry limits, or alter provider configuration.
SQLite descendant discovery and legacy scrubbing remain inside the same deletion transaction.
Provider modules preserve optional imports and provider-specific dataclass defaults.

Tiny adapters may stay beside a cohesive owner when another file would add only indirection, with the same provenance and removal comments.
The adapter-media capability table beside media fallback is one such case.
Application request-log context, canonical replay, OAuth policy, and ordinary public Agno construction remain application code.

## Upgrading Agno

1. Read each affected source comment and the linked upstream changes.
2. Inspect the code in the pinned distribution to verify the behavior actually shipped.
3. Run the listed behavioral tests with the candidate workaround disabled.
4. Remove only the replaced adaptation, retain required product behavior and tests, and update Tach boundaries and this inventory.

A merged PR does not by itself prove that the pinned SDK supplies the complete replacement.
For example, Agno's typed embedding errors do not replace MindRoom's request/validation hooks, and its existing `after_tool_results` callback does not expose the mutable message surface used by queued notices.
The Responses completion fix also has a separate removal condition from protection against retrying retained partial output.
