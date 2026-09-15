# Agno compatibility boundaries

The goal is to make Agno weaknesses actionable upstream and remove local workarounds as fixes ship.
For each weakness, connect the observed behavior, an upstream issue or PR, local regression coverage, and the condition that allows removal.
Focused `agno_compat_<subject>.py` modules help isolate substantive workarounds; small adaptations can stay beside their owners when that makes the upstream gap clearer.
Each workaround documents its reason, verified upstream issue/PR or explicit tracking gap, removal condition, and behavioral tests in source comments.
The maintenance policy lives in `AGENTS.md` through its `CLAUDE.md` target.

## Upstream work

### Existing tracking

Issue and PR states below were checked on September 15, 2026. Recheck upstream before starting a contribution or removing a workaround.
The source modules listed here contain the exact regression-test references and removal conditions.
Paths are relative to `src/mindroom/`.

| Agno weakness | Upstream issue / PR | Local workaround and remaining scope |
| --- | --- | --- |
| Deleted runs survive in legacy blobs or deletion spans separate transactions. | [Issue #9934](https://github.com/agno-agi/agno/issues/9934), [PR #9939](https://github.com/agno-agi/agno/pull/9939), both open. | `agno_compat_sqlite.py`; preserve owner descendant deletion when the atomic upstream fix ships. |
| New runs can sort before surviving runs after deletion. | [Issue #9936](https://github.com/agno-agi/agno/issues/9936), [PR #9938](https://github.com/agno-agi/agno/pull/9938), both open. | `agno_compat_sqlite.py`; verify insertion after existing indexes with the local override disabled. |
| Team input flattens roleful messages. | [Issue #9942](https://github.com/agno-agi/agno/issues/9942), [PR #9943](https://github.com/agno-agi/agno/pull/9943), both open. | `history/agno_compat_message_builder.py`; historical-media filtering needs a separate extension point. |
| Responses streams accept incomplete EOFs and publish continuation IDs too early. | [PR #10135](https://github.com/agno-agi/agno/pull/10135), open. | `agno_compat_openai_responses.py`; retry safety after retained output remains a separate gap. |
| Responses continuation depends on a hard-coded model-name predicate. | [PR #10075](https://github.com/agno-agi/agno/pull/10075), open. | `agno_compat_openai_responses.py`; preserve explicit storage and replay choices. |
| Responses reasoning is lost during explicit tool-call replay. | [Issue #9960](https://github.com/agno-agi/agno/issues/9960), [PR #9968](https://github.com/agno-agi/agno/pull/9968), both open. | `agno_compat_openai_responses_items.py`; hosted tool-search output needs separate coverage and tracking. |
| Unreliable streamed tool-call indexes create malformed or merged calls. | [Issue #8879](https://github.com/agno-agi/agno/issues/8879), [PR #8880](https://github.com/agno-agi/agno/pull/8880), both open. | `agno_compat_openai_chat.py`; local filtering only removes empty slots. |
| Claude requests include sampling controls rejected in supported modes. | [Issue #9931](https://github.com/agno-agi/agno/issues/9931), [PR #9933](https://github.com/agno-agi/agno/pull/9933), both open. | `agno_compat_claude.py`; check top-level parameters and `extra_body` for the same model generations. |
| Vertex Claude tool definitions contain rejected provider-level `strict` fields. | [Issue #6599](https://github.com/agno-agi/agno/issues/6599), open; [PR #6923](https://github.com/agno-agi/agno/pull/6923), closed without merge. | `agno_compat_vertex_claude_tools.py`; preserve schema properties named `strict`. |
| Knowledge search failures become plausible empty results. | [Issue #10150](https://github.com/agno-agi/agno/issues/10150), [PR #10152](https://github.com/agno-agi/agno/pull/10152), both open. | `agno_compat_knowledge.py`; insertion failures and validation need separate work. |
| Prepared requests and effective tools require private Agent/Team APIs. | [Issue #7806](https://github.com/agno-agi/agno/issues/7806), [PR #7807](https://github.com/agno-agi/agno/pull/7807), both open. | `agno_compat_prepared_tools.py`; an inspection API must also support executable run-context/media bindings to replace the RTC path. |
| Async runs lack supported persistence hooks for synchronous storage owners. | [Issue #10149](https://github.com/agno-agi/agno/issues/10149), open; [PR #10148](https://github.com/agno-agi/agno/pull/10148), open and partial. | `agno_compat_session_persistence.py`; the PR only routes Agent startup through the awaitable read path. Agent/Team writes and owner dispatch remain. |

[PR #9814](https://github.com/agno-agi/agno/pull/9814) is merged, and its typed embedding errors are present in the pinned Agno 3.0.9 embedder.
The remaining embedder work concerns request/validation hooks and owner-controlled batch failure handling.

### Tracking gaps

These are unfinished contribution candidates identified by the local audit.
Before filing an issue, check current Agno behavior and existing discussions, and build a minimal Agno-only reproducer or a concrete extension-point proposal.
Missing extension points support intentional MindRoom behavior; establish the general upstream use case before proposing an API.
Related gaps are grouped below for navigation; separate independent fixes and removal conditions when contributing.

| Observed gap or required extension point | Next upstream work | Local evidence |
| --- | --- | --- |
| Parsed provider responses omit terminal stop metadata. | Preserve OpenAI `finish_reason` and Claude `stop_reason` through a stable parsed-response interface. | `agno_compat_openai_chat.py`, `agno_compat_claude.py`. |
| Hosted Responses tool-search items are omitted from replay data. | Reproduce lost hosted-search output and preserve ordered call/output items. | `agno_compat_openai_responses_items.py`. |
| Retrying a stream can reuse partial assistant or tool state. | Establish retry ownership and test that partial output cannot be duplicated. | `agno_compat_openai_responses.py`. |
| SQLite journaling is forced, and cache statistics require private access. | Propose configurable pragmas; track public cache counters separately. | `agno_compat_sqlite.py`. |
| Cancelled runs persist in detached tasks without an awaited drain boundary. | Expose completion for the exact cancelled run before caller-owned history writes. | `agno_compat_session_persistence.py`. |
| Knowledge insertion lacks caller-controlled error propagation and validation. | Reproduce swallowed vector failures and propose validation before publishing content status. | `agno_compat_knowledge.py`. |
| Embedder requests are built separately across sync, async, and batch paths. | Add shared request and response hooks while retaining typed provider failures. | `agno_compat_openai_embedder.py`. |
| Denying a persisted tool call requires a live Function and private continuation interception. | Support denial after tool removal and an owner callback before resumed-tool lookup. | `agno_compat_approval.py`. |
| Message projection and historical-media policy require private message interception. | Propose supported message-preparation hooks with explicit history-mutation ownership. | `agno_compat_model_hooks.py`, `history/agno_compat_message_builder.py`. |
| Post-tool callbacks lack mutable messages/results at the required stage. | Extend the callback surface after result formatting and media insertion. | `agno_compat_model_hooks.py`. |
| Invocation, retry-cycle, scoped request, and final SDK payload hooks are missing. | Define each lifecycle stage separately, including wrapper order, cache behavior, and cancellation cleanup. | `agno_compat_model_hooks.py`. |
| Tool-hook chains cannot express the required deferred-result and synchronous execution ownership. | Propose an execution hook with regression coverage for cache hits, argument rewrites, and cancellation. | `tool_system/agno_compat_tool_hooks.py`. |
| Function schema postprocessors are lost or misbound during copying/preparation. | Add a supported postprocessor that survives copies and runs after schema preparation. | `tool_system/agno_compat_function_schema.py`. |
| Google authentication requires private resolver and Function entrypoint replacement. | Propose injectable credential resolution and an entrypoint middleware hook. | `oauth/agno_compat_google_auth.py`. |
| WebsiteReader does not expose fetch, redirect, and crawl-admission hooks. | Propose a replaceable fetch path and explicit crawl-policy callbacks. | `custom_tools/agno_compat_website_reader.py`. |
| GithubTools serializes provider failures and logs provider detail through fixed message formats. | Expose typed errors; separately provide structured logging or a redaction hook. | `custom_tools/agno_compat_github_errors.py`. |
| Adapter media capabilities must be inferred from a private module-name table. | Expose accurate supported-input capabilities independently of provider error learning. | The small documented table in `provider_media_fallback.py`. |

Module extraction does not close a tracking gap.
The contribution is complete when the upstream behavior is available, the local regression passes without its workaround, and the obsolete adaptation is removed.

## Boundary inventory

Paths below are relative to `src/mindroom/`.
Related workarounds can share one module, but keep separate removal conditions when an upstream fix covers only part of it.

| Compatibility module | Agno adaptation | Policy and lifecycle owner |
| --- | --- | --- |
| `agno_compat_session_persistence.py` | Async Agent/Team persistence bindings for the owned synchronous store and exact-run cancellation drainage. | `agent_storage.py` and storage lifecycle callers; `ai.py` retains canonical history ownership. |
| `agno_compat_sqlite.py` | Private pragma listener removal, monotonic run insertion, atomic run/legacy deletion transaction, and private cache counters. | `agent_storage.py` retains journaling choice, prompt sanitization, descendant selection, diagnostics, and legacy scrub policy. |
| `agno_compat_knowledge.py` | Search error propagation and private insertion/status plumbing with owner validation. | `strict_knowledge.py` retains shared-index scope and complete-embedding requirements; knowledge managers retain publication/lifecycle ownership. |
| `agno_compat_openai_embedder.py` | Copied sync/async/batch request paths with explicit request/error/validation hooks. | `openai_embedder.py` retains input and dimensions policy, safe errors, response validation, health reporting, and the product sync-batch API. |
| `agno_compat_approval.py` | Private rejected-tool result creation and temporary continuation tool-lookup interception. | `approval_tools.py` retains exact call/run identity, authorization, and denial state. |
| `agno_compat_model_hooks.py` | Model message projection, post-tool formatting/media callbacks, permanent and scoped invocation/retry bindings, answer-cache bypass, and provider client factories. | Approval receipts, queued notices, request logging, media fallback, Claude retries, prompt caching, and participation decisions remain in their owning modules. |
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
Attempt-scoped bindings restore the previous methods and answer-cache settings when the attempt exits.
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
