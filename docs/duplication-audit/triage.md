# Source Duplication Audit Triage

This document is a second-pass synthesis of the per-module reports in `docs/duplication-audit/reports-src`.
It groups duplicate-functionality findings by code area and turns the raw reports into a reviewable refactor backlog.
The area passes were read-only and intentionally overlapped where duplication crossed subsystem boundaries.

## How To Use This

Do not work through the 430 reports linearly.
Start with one cluster from the PR queue below.
For each cluster, open the listed reports, verify the current source still matches the claim, add characterization tests, then make the smallest refactor that removes the duplicated behavior.
Prefer one small PR per cluster.
Avoid broad base classes or universal helpers unless two or more narrow extractions have already proven the shape.

## Living Status

| Cluster | Status | PR | Branch | Gate | Verification |
| --- | --- | --- | --- | --- | --- |
| STORE-009 / rank 5: Attachment output-path schema | Draft PR opened; accepted by line gate | [#892](https://github.com/mindroom-ai/mindroom/pull/892) | `refactor/attachment-output-path-schema` | Production net `-5` lines | `uv run pytest tests/test_tool_output_files.py tests/test_attachments_tool.py -q --no-cov -n 0`; `uv run pre-commit run --files ...` |
| FLOW-005 / rank 2: Approval datetime parser | Draft PR opened; accepted by line gate | [#893](https://github.com/mindroom-ai/mindroom/pull/893) | `refactor/approval-datetime-parser` | Production net `-3` lines | `uv run pytest tests/test_tool_approval.py -n 0 --no-cov`; `uv sync --all-extras`; `uv run pre-commit run --files ...` |
| FLOW-006 / rank 3: Matrix reaction content builder | Draft PR opened; accepted by line gate | [#894](https://github.com/mindroom-ai/mindroom/pull/894) | `refactor/matrix-reaction-content-builder` | Production net `-31` lines | Focused pytest: 5 passed; `uv run pre-commit run --files ...` |
| CFG-002 / rank 4: Config search-location rendering | Draft PR opened; accepted as roughly flat with duplicated rendering removed | [#895](https://github.com/mindroom-ai/mindroom/pull/895) | `refactor/config-search-location-rendering` | Production net `+6` lines | Focused CLI pytest: 14 passed; `uv run pre-commit run --files ...` |

## PR Queue

| Rank | Cluster | Area | Confidence | Risk | First PR |
| --- | --- | --- | --- | --- | --- |
| 1 | Worker API response DTOs and serializers | API, workers | High | Low | Extract `api/worker_responses.py` and share worker DTOs plus `serialize_worker_handle`. |
| 2 | Approval datetime parser | Approvals | High | Low | Fold duplicated approval-local ISO parser into one approval helper. |
| 3 | Matrix reaction content builder | Matrix, workflow | High | Low | Add pure `build_reaction_content(event_id, key)` and reuse it at reaction send sites. |
| 4 | Config search-location rendering | CLI, config | High | Low | Add a small CLI display helper for `config_search_locations(...)` output. |
| 5 | Attachment output-path schema | Attachments, tool system | High | Low | Reuse the existing output-path schema helper from attachment tool schema generation. |
| 6 | CLI `.env` mutation helpers | CLI | High | Medium | Add a line-preserving env-file helper and migrate `connect.py` plus `local_stack.py` first. |
| 7 | Config validation helpers | Config | High | Low-Medium | Extract ordered duplicate detection and history-limit mutual-exclusion helpers. |
| 8 | Google service-account fallback | Custom tools, OAuth | High | Medium | Extract only the shared fallback predicate for Gmail, Calendar, Drive, and Sheets wrappers. |
| 9 | Agent session coercion cleanup | Storage, memory | High | Low-Medium | Replace auto-flush local session coercion with `get_agent_session()`. |
| 10 | API snapshot publishing | API config lifecycle | High | Medium-High | Make `config_lifecycle._published_snapshot` canonical and replace two copies. |
| 11 | Knowledge published-index metadata codec | Knowledge | High | Medium | Extract common metadata read/write and atomic JSON helpers into `knowledge/index_metadata.py`. |
| 12 | Scheduled task restore parsing | Scheduling | High | Medium | Make `restore_scheduled_tasks` consume parsed `ScheduledTaskRecord`s. |
| 13 | Credentials worker-target resolver | API credentials, OAuth | High | Medium | Expose a narrow `worker_target_for_credentials_target()` helper and reuse it from OAuth. |
| 14 | Google OAuth provider factory skeleton | OAuth | High | Medium | Add a private Google provider factory while keeping public per-service factory functions. |
| 15 | Matrix thread and visible-event helpers | Matrix | High | Low-Medium | Extract cache lookup normalization, page-local child proof, and visible-content helper reuse. |
| 16 | Worker lifecycle pure helpers | Workers | High | Medium-High | Extract only effective idle status, filtering, sorting, and maybe worker-lock registry helpers. |
| 17 | Plugin import transaction cleanup | Tool system | High | High | Share package-chain install/restore and module execution scaffolding in `plugin_imports.py`. |
| 18 | Matrix cache backend policy | Matrix cache | High | Medium-High | Extract only agent-message snapshot policy or pure event batch grouping. |
| 19 | Streaming tool tracking | Core runtime | High | High | Extract a scoped streaming-tool tracker without owning Matrix delivery or team formatting. |
| 20 | Subprocess env and sandbox attachment protocol | Sandbox, workers | High | High | Write characterization tests first, then extract pure env or protocol helpers only. |

The first five items are cleanup-sized and should be easy to review.
Items 6 through 16 are normal refactor PRs with focused tests.
Items 17 through 20 have strong duplication evidence but should be delayed until characterization tests are in place.

## Area Backlog

### API And Web Backend

| ID | Cluster | Reports | Files | Confidence | Risk | Suggested first move |
| --- | --- | --- | --- | --- | --- | --- |
| API-001 | Snapshot publication and stale-write semantics | `0013`, `0019`, `0023` | `api/config_lifecycle.py`, `api/main.py`, `api/runtime_reload.py` | High | Medium-High | Canonicalize `_published_snapshot` in `config_lifecycle.py`. |
| API-002 | Worker observability DTOs and serializers | `0026`, `0032` | `api/workers.py`, `api/sandbox_runner.py` | High | Low | Extract shared DTOs and serializer while keeping manager lookup local. |
| API-003 | Credentials/OAuth worker-target resolution | `0014`, `0021`, `0031` | `api/credentials.py`, `api/oauth.py`, `api/tools.py` | High | Medium | Expose the worker-target conversion helper only. |
| API-004 | Agent/team config CRUD helpers | `0019` | `api/main.py` | High | Low | Add private section entity helpers for list, upsert, create, and delete. |
| API-005 | Scheduling API read/edit model | `0029` | `api/schedules.py`, `scheduling.py` | Medium-High | Medium | Add pure scheduling read-model helpers while keeping API DTOs local. |
| API-006 | OpenAI-compatible team preparation | `0022` | `api/openai_compat.py`, `teams.py` | Medium-High | High | Defer until stronger team execution tests exist. |
| API-007 | Sandbox subprocess env policy | `0024` | `api/sandbox_exec.py`, `tools/shell.py` | Medium | High | Extract only pure env fragments after exact precedence tests. |

Recommended API order is `API-002`, `API-001`, then `API-003`.

### Config, CLI, And Commands

| ID | Cluster | Reports | Files | Confidence | Risk | Suggested first move |
| --- | --- | --- | --- | --- | --- | --- |
| CFG-001 | CLI `.env` mutation helpers | `0048`, `0049`, `0051` | `cli/config.py`, `cli/connect.py`, `cli/local_stack.py` | High | Medium | Add `cli/env_file.py` and migrate the two near-identical upsert helpers first. |
| CFG-002 | Config search-location rendering | `0048`, `0052`, `0073` | `cli/config.py`, `cli/main.py`, `constants.py` | High | Low | Share only display formatting for config search locations. |
| CFG-003 | Config duplicate-list and validator helpers | `0063`, `0065`, `0066`, `0070`, `0071` | `config/*.py` | High | Low-Medium | Add `config/validation.py` with ordered duplicate and history-limit helpers. |
| CFG-004 | Runtime config mutation and YAML rendering | `0058`, `0086` | `commands/config_commands.py`, `custom_tools/config_manager.py`, `custom_tools/self_config.py` | Medium-High | Medium-High | Extract only validate-and-persist or YAML formatting, not full update flows. |
| CFG-005 | Provider/env-key resolution drift | `0050`, `0073` | `cli/doctor.py`, `cli/config.py`, `constants.py` | High | High | Start by deriving ordinary CLI required env keys from `env_key_for_provider`. |
| CFG-006 | Chat command help and welcome snippets | `0060`, `0061` | `commands/parsing.py`, `commands/handler.py` | High | Low | Make command docs the source for a compact welcome subset. |

Recommended config order is `CFG-002`, `CFG-001`, then `CFG-003`.

### Core Runtime And Orchestration

| ID | Cluster | Reports | Files | Confidence | Risk | Suggested first move |
| --- | --- | --- | --- | --- | --- | --- |
| CORE-001 | Streaming tool tracking | `0008`, `0261`, `0265` | `ai.py`, `teams.py`, `streaming_delivery.py` | High | High | Extract a scoped tracker that owns pending/completed trace state only. |
| CORE-002 | Matrix run and turn metadata normalization | `0414`, `0117`, `0008`, `0007`, `0075` | `turn_store.py`, `handled_turns.py`, `history/interrupted_replay.py`, `ai.py`, `agents.py` | High | High | Add pure parsing/serialization helpers, preserving caller return shapes. |
| CORE-003 | Response runner lifecycle setup | `0251`, `0250`, `0249`, `0252`, `0042` | `response_runner.py`, `response_terminal.py` | High | High | First replace manual terminal outcome construction with the existing builder. |
| CORE-004 | Agent/team execution micro-helpers | `0008`, `0010`, `0265`, `0113` | `ai.py`, `ai_runtime.py`, `teams.py`, `execution_preparation.py` | High | Medium | Move only tiny helpers such as retry run IDs and cancellation boilerplate detection. |
| CORE-005 | Turn ingress text-like flow | `0412`, `0413` | `turn_controller.py`, `turn_policy.py` | Medium-High | Medium | Add private `_dispatch_prepared_text_ingress(...)` only. |
| CORE-006 | Orchestrator root-space invite helpers | `0247`, `0043` | `orchestrator.py`, `bot_room_lifecycle.py` | Medium | Medium-High | Make `_ensure_root_space` reuse `_invite_user_if_missing` before adding new helpers. |

Core runtime has large payoff but higher blast radius.
Start only after a characterization-test pass for the selected cluster.

### Matrix Protocol, Media, Rooms, And Cache

| ID | Cluster | Reports | Files | Confidence | Risk | Suggested first move |
| --- | --- | --- | --- | --- | --- | --- |
| MAT-001 | Cache backend snapshot and event-cache policy | `0160`, `0161`, `0165`, `0166`, `0172`, `0173` | `matrix/cache/*.py` | High | Medium-High | Extract snapshot policy or pure event batch grouping only. |
| MAT-002 | MXC upload/download/media primitives | `0176`, `0187`, `0188`, `0191`, `0185`, `0155`, `0038` | `matrix/client_delivery.py`, `matrix/large_messages.py`, `matrix/avatar.py`, `matrix/media.py`, `matrix/message_content.py`, `matrix/image_handler.py`, `attachments.py` | High | Medium | Add byte-level download or upload-response normalization helper. |
| MAT-003 | Visible message and thread relation helpers | `0179`, `0180`, `0200`, `0201`, `0202`, `0203`, `0206`, `0182`, `0268` | `matrix/thread_*.py`, `visible_body.py`, `thread_utils.py`, `event_info.py` | High | Low-Medium | Move lookup normalization and page-local child proof into Matrix thread helpers. |
| MAT-004 | Room membership and leave filtering | `0020`, `0043`, `0195`, `0245` | `api/matrix_operations.py`, `bot_room_lifecycle.py`, `matrix/rooms.py`, `orchestration/rooms.py` | High | Medium | Add `filter_non_dm_rooms(client, room_ids)` and defer room-diff generalization. |
| MAT-005 | Matrix identity and managed-account resolution | `0184`, `0205`, `0207`, `0245` | `matrix/identity.py`, `matrix/users.py`, `matrix_identifiers.py`, `matrix/state.py`, `orchestration/rooms.py`, `cli/connect.py` | Medium-High | Medium | Centralize managed account key and user-id resolution. |
| MAT-006 | Matrix custom-tool presentation helpers | `0095`, `0096`, `0098`, `0099`, `0353`, `0354`, `0355` | `custom_tools/matrix_*.py`, `tools/matrix_*.py` | High | Low-Medium | Reuse `matrix_helpers.check_rate_limit` before extracting serializers. |
| MAT-007 | Streaming and stale-cleanup delivery shapes | `0196`, `0260`, `0261`, `0249` | `streaming.py`, `matrix/stale_stream_cleanup.py`, `delivery_gateway.py`, `response_attempt.py` | Medium-High | Medium | Share cancellation logging first and defer delivery abstraction. |

Matrix work should be split into many small PRs.
Do not introduce a SQLite/Postgres cache base class as the first step.

### Tool System And Generic Tool Wrappers

| ID | Cluster | Reports | Files | Confidence | Risk | Suggested first move |
| --- | --- | --- | --- | --- | --- | --- |
| TOOL-001 | Plugin import transaction duplication | `0278`, `0281`, `0282` | `tool_system/plugins.py`, `tool_system/metadata.py`, `tool_system/plugin_imports.py` | High | High | Share package-chain install/restore and module execution scaffolding. |
| TOOL-002 | Context-bound async stream wrappers | `0284`, `0290` | `tool_system/runtime_context.py`, `tool_system/worker_routing.py`, `llm_request_logging.py` | High | Medium-High | Add a generic wrapper for streams bound by a context-manager factory. |
| TOOL-003 | Output-file and sandbox attachment protocol | `0279`, `0285` | `tool_system/output_files.py`, `tool_system/sandbox_proxy.py`, `api/sandbox_runner.py` | High | High | Extract protocol dataclasses or encode/decode helpers after tests. |
| TOOL-004 | Dynamic registry reconciliation | `0283` | `tool_system/registry_state.py`, `mcp/registry.py` | Medium-High | Medium | Add a private reconciliation helper for owned dynamic tool names. |
| TOOL-005 | Optional dependency install and retry flow | `0275` | `tool_system/dependencies.py`, `tool_system/metadata.py`, `api/auth.py` | High | Medium | Extend `ensure_optional_deps` or add one importable-or-install helper. |
| TOOL-006 | File/coding path-safety helpers | `0310`, `0332` | `tools/file.py`, `tools/coding.py`, `custom_tools/coding.py` | High | High | Extract only pure path helpers and blocked-path message generation. |
| TOOL-007 | Shell subprocess env fragments | `0382` | `tools/shell.py`, sandbox execution paths | Medium | Medium | Extract PATH composition and workspace-home contract helpers only. |

Ignore the repeated lazy toolkit factory pattern for now.
It is explicit boilerplate with low payoff and high churn if generalized.

### Custom Tools, OAuth, Credentials, And Provider Integrations

| ID | Cluster | Reports | Files | Confidence | Risk | Suggested first move |
| --- | --- | --- | --- | --- | --- | --- |
| CRED-001 | Google custom-tool service-account fallback | `0089`, `0090`, `0091`, `0093`, `0232` | `custom_tools/gmail.py`, `google_calendar.py`, `google_drive.py`, `google_sheets.py`, `oauth/client.py` | High | Medium | Extract only the fallback predicate or mixin logic, not constructors. |
| CRED-002 | Google OAuth provider factory skeleton | `0234`, `0235`, `0236`, `0237`, `0233` | `oauth/google_*.py`, `oauth/google.py` | High | Medium | Add `_google_oauth_provider(...)` and keep public per-service factories. |
| CRED-003 | Dashboard credential routing and OAuth target resolution | `0014`, `0077`, `0021` | `api/credentials.py`, `credentials.py`, `api/oauth.py`, `oauth/service.py` | High | Medium-High | Reuse only the worker-target conversion helper first. |
| CRED-004 | OAuth-required payload serialization | `0232`, `0233` | `oauth/client.py`, `oauth/providers.py`, `api/sandbox_runner.py`, `tool_system/tool_hooks.py` | High | Medium | Add `oauth_connection_required_payload(exc)` and preserve dict/string variants. |
| CRED-005 | Google Drive numeric coercion | `0091` | `custom_tools/google_drive.py`, `tool_system/metadata.py` | High | Low | Share a low-level optional-number coercer with caller-owned error handling. |
| CRED-006 | Matrix custom-tool helpers | `0095`, `0096`, `0098`, `0099`, `0103`, `0104`, `0105` | `custom_tools/matrix_*.py`, `matrix_helpers.py` | High | Low-Medium | Replace only `MatrixApiTools._check_rate_limit` first. |

Provider wrapper constructors differ enough that helper extraction should start with predicates and payload builders, not constructor inheritance.

### Knowledge, Memory, Attachments, And History

| ID | Cluster | Reports | Files | Confidence | Risk | Suggested first move |
| --- | --- | --- | --- | --- | --- | --- |
| STORE-001 | Knowledge published-index metadata codec | `0143`, `0147`, `0145` | `knowledge/manager.py`, `knowledge/registry.py` | High | Medium | Add `knowledge/index_metadata.py` for shared field readers and atomic JSON writes. |
| STORE-002 | Memory backend scope traversal | `0220`, `0221`, `0227` | `memory/_file_backend.py`, `memory/_mem0_backend.py`, `memory/functions.py` | High | High | Extract only allowed-scope and storage-path traversal. |
| STORE-003 | Agent/team Agno session loading | `0006`, `0120`, `0123`, `0225` | `agent_storage.py`, `memory/auto_flush.py`, `history/runtime.py`, `history/interrupted_replay.py`, `conversation_state_writer.py`, `teams.py` | High | Low-Medium | Replace auto-flush local coercion with `get_agent_session()`. |
| STORE-004 | ToolExecutionIdentity JSON payloads | `0145`, `0225` | `knowledge/refresh_runner.py`, `memory/auto_flush.py`, `tool_system/worker_routing.py` | High | Medium | Add strict/lenient serializer and parser near the type definition. |
| STORE-005 | Attachment ID ordering and prompt wording | `0038`, `0037`, `0218` | `attachments.py`, `inbound_turn_normalizer.py`, `tool_system/runtime_context.py` | Medium-High | Low | Add `unique_attachment_ids()` and `format_attachment_ids_prompt()`. |
| STORE-006 | Credential-free Git repo URL normalization | `0144` | `knowledge/redaction.py`, `knowledge/manager.py`, `config/main.py` | High | Medium | Expose `credential_free_repo_url(...)` and migrate manager first. |
| STORE-007 | Memory/knowledge embedder provider mapping | `0226`, `0143` | `memory/config.py`, `knowledge/manager.py`, `credentials_sync.py`, `embeddings.py` | Medium-High | Medium | Reuse `get_ollama_host()` and `get_api_key_for_provider()` before a broader resolver. |
| STORE-008 | History force-compaction flag mutation | `0121`, `0123` | `history/manual.py`, `history/runtime.py`, `history/storage.py` | High | Low | Add `set_force_compaction_state()` next to `clear_force_compaction_state()`. |
| STORE-009 | Attachment tool output-path schema | `0081` | `custom_tools/attachments.py`, `tool_system/output_files.py` | High | Low | Use the existing output-path schema helper. |

Storage work should preserve backend-specific semantics.
Avoid a sweeping memory backend abstraction until small traversal helpers have tests.

### Workers, Sandbox, And Execution Environment

| ID | Cluster | Reports | Files | Confidence | Risk | Suggested first move |
| --- | --- | --- | --- | --- | --- | --- |
| WORK-001 | Worker API response DTOs and serializers | `0026`, `0032` | `api/workers.py`, `api/sandbox_runner.py` | High | Low | Extract shared DTOs and `serialize_worker_handle()`. |
| WORK-002 | Worker lifecycle presentation helpers | `0420`, `0422`, `0425`, `0426`, `0427` | `workers/backends/static_runner.py`, `workers/backends/local.py`, `workers/backends/kubernetes.py` | High | Medium-High | Extract effective idle status, filtering, sorting, and maybe lock registry only. |
| WORK-003 | Subprocess and workspace env contract | `0024`, `0382`, `0026`, `0424` | `api/sandbox_exec.py`, `api/sandbox_runner.py`, `tools/shell.py`, `workers/backends/kubernetes_resources.py` | High | High | Extract pure PATH and workspace-home helpers after exact env tests. |
| WORK-004 | Sandbox attachment save protocol symmetry | `0285`, `0026` | `tool_system/sandbox_proxy.py`, `api/sandbox_runner.py` | High | Medium-High | Extract protocol helpers or dataclasses near the API boundary. |
| WORK-005 | User-agent private visibility policy | `0028`, `0285`, `0424` | `api/sandbox_worker_prep.py`, `tool_system/sandbox_proxy.py`, `workers/backends/kubernetes_resources.py`, `tool_system/worker_routing.py` | High | Medium | Add a pure predicate in `worker_routing.py` and keep exceptions local. |
| WORK-006 | File/coding base-dir path safety | `0084`, `0310`, `0332` | `custom_tools/coding.py`, `tools/file.py` | High | Medium | Extract only path resolution, path formatting, and blocked-path message helpers. |

Worker and sandbox changes can affect credential exposure and workspace isolation.
Write characterization tests before extracting env or protocol helpers.

### Workflow Infrastructure

| ID | Cluster | Reports | Files | Confidence | Risk | Suggested first move |
| --- | --- | --- | --- | --- | --- | --- |
| FLOW-001 | Scheduled task state parsing drift | `0029`, scheduling report | `scheduling.py` | High | Medium | Make restore consume parsed `ScheduledTaskRecord`s while preserving overdue handling. |
| FLOW-002 | Matrix send and cache-tracking tail | hooks and scheduling reports | `hooks/sender.py`, `scheduling.py` | High | Medium-High | Add `send_and_track_message` for already-built content only. |
| FLOW-003 | Matrix room-state get/put adapters | hooks and scheduling reports | `hooks/state.py`, `scheduling.py` | High | Medium | Create a low-level Matrix state helper only where semantics align. |
| FLOW-004 | Hook package wire-format helpers | hooks reports | `hooks/context.py`, `hooks/execution.py`, `hooks/ingress.py` | High | Low | Add shared hook-source builder and envelope extractor. |
| FLOW-005 | Approval datetime parser | approval reports | `approval_events.py`, `approval_manager.py` | High | Low | Fold duplicated parser into one approval-local helper. |
| FLOW-006 | Matrix reaction payload builder | interactive report | `interactive.py`, config confirmation, stop, Matrix tools | High | Low | Add pure reaction content builder. |
| FLOW-007 | Tiny literal helpers | coalescing and interactive reports | `coalescing_batch.py`, `dispatch_handoff.py`, `interactive.py` | High | Low | Batch with nearby touched work rather than standalone. |

Workflow infrastructure has several low-risk pure helpers.
Do not generalize coalescing queues, background task ownership, or voice in-flight normalization yet.

## Defer Or Ignore Themes

Repeated lazy tool factories in `src/mindroom/tools/*.py` are not worth a standalone refactor.
Package facade files such as `knowledge/__init__.py`, `memory/__init__.py`, and `history/__init__.py` should stay as they are.
Schema parity between SQLite and Postgres is real, but the first Matrix cache PR should extract pure policy rather than introduce a shared backend class.
Agent and team response lifecycle overlap is real, but a unified response engine would be too risky before smaller helper extractions land.
Generic process-runner or env-runner abstractions should wait until exact env precedence tests pin the behavior.
Matrix state/event send helpers should stay narrow because event types, auditing, side effects, and user-facing text differ by caller.
Config-manager, self-config, and chat `!config` flows should share only neutral validate/persist helpers until output text and authorization behavior are pinned.
Lifecycle, queue, background-task, and lock-manager similarities should be treated as design notes unless a task touches multiple call sites.

## Review Checklist Per Refactor PR

Open every listed report before editing.
Confirm the source still matches the report because `main` may have changed after the audit branch was created.
Add characterization tests for each behavior difference named in the cluster.
Keep extraction ownership close to the domain module that already owns the concept.
Preserve user-facing strings, Matrix payload shapes, JSON field names, and env precedence unless the PR explicitly changes them.
Run the focused tests named in the cluster and then the full relevant suite before merging.
