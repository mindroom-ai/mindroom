# Migration and compatibility boundaries

MindRoom keeps substantive historical schemas and representations in named legacy modules.
Historical-format production owners use `legacy_<subject>.py` beside their current owner, including one-time upgrades and recurring old-data readers.
The current storage or lifecycle owner keeps its transaction, locking, validation, authorization, retry, and current-format processing.
Small field defaults stay with their current model when extraction would add indirection without isolating a meaningful migration.
Dependency-owned migrations and authoritative SaaS data remain under their existing owners.

This page maps every item from the migration audit to its current owner and disposition.
It describes the current source rather than promising support for every earlier release.

## Status terms

| Status | Meaning |
| --- | --- |
| Isolated | Historical input remains supported through a named boundary. |
| Removed/superseded | The earlier reader is gone or a newer cutoff replaces it. |
| Dependency-owned | An installed library owns the migration. |
| Current behavior | The code serves a current format, protocol, safety rule, or authoring interface. |
| Tiny retained default | A small default stays with its current owner because extraction would add complexity. |

## Named boundaries

The first fourteen rows were created or renamed by the migration-boundary isolation work.
The remaining rows were already focused boundaries and complete the current map.

| Boundary | Trigger and current caller | Retained guarantees |
| --- | --- | --- |
| [`src/mindroom/mcp_gateway/legacy_schema.py`][mcp-legacy-schema] | `GatewayOAuthStore` calls `migrate_schema` when opening SQLite. | The store retains its writer transaction, base DDL, and live processing; ordered expiry, accounting, lifecycle, account, and token-cutoff upgrades stay together. |
| [`src/mindroom/legacy_session_storage.py`][legacy-session] | Run deletion and usage diagnostics encounter an Agno 2 `runs` blob. | Current rows win by `run_id`; descendant deletion, transaction ownership, diagnostics, and byte accounting remain with current owners. |
| [`src/mindroom/legacy_openai_tool_replay.py`][legacy-openai] | OpenAI-family adapters replay histories written before empty tool arguments were preserved. | Repair copies changed messages, supplies empty arguments, and drops placeholder calls with their associated orphan tool results without mutating stored history. |
| [`src/mindroom/legacy_handled_turns.py`][legacy-handled] | `HandledTurnLedger` finds `tracking/<agent>_responded.json`. | Insert-only adoption protects newer rows, fills absent indexes, retries interrupted work, and renames only after adoption. |
| [`src/mindroom/event_journal/legacy_turn_records.py`][legacy-turn-records] | The handled-turn importer adopts missing journal indexes. | The journal transaction is retained and migration writes never use current upsert deletion semantics. |
| [`src/mindroom/legacy_delivery_payloads.py`][legacy-delivery] | Outbox reads or Matrix writes encounter inline FINAL results and the bounded marker. | Old inline outcomes keep rolling-writer precedence, current local results remain authoritative otherwise, and full recovery data stays off the wire. |
| [`src/mindroom/legacy_approval_payloads.py`][legacy-approval] | Approval claim or resume encounters missing historical context or the older card ID. | Current approval ownership, authorization, exact-call checks, transaction settlement, and failure handling remain with current owners. |
| [`src/mindroom/matrix/legacy_sync_continuity.py`][legacy-sync] | `SyncContinuityStore` loads a valid v2 or v3 record. | The helper validates the old shape, discards its checkpoint, increments revision once, and lets the store rewrite v4 under lock. |
| [`src/mindroom/script_runs/legacy_schema.py`][script-legacy-schema] | `ScriptRunStore` finds missing resource snapshot columns. | Schema creation and the transaction stay in the store; old rows receive the established null or empty-map values. |
| [`src/mindroom/knowledge/legacy_metadata.py`][knowledge-legacy] | Knowledge parsing sees absent or empty optional filter fields. | Current parsing still rejects unknown fields; missing corpus settings retain empty historical sentinels and rebuild only when the corresponding current corpus-compatibility value differs. |
| [`src/mindroom/matrix/legacy_state.py`][matrix-legacy-state] | Matrix state has accounts without a domain or a noncanonical serialized shape. | Runtime-domain resolution, parsing, caching, and atomic persistence stay in `matrix/state.py`; rewrites happen only when data differs. |
| [`src/mindroom/config/legacy_fields.py`][config-legacy] | Agent or defaults validation sees a retired field. | Pydantic remains the strict validation boundary and the helper provides directed replacement errors. |
| [`src/mindroom/legacy_streaming.py`][legacy-streaming] | Streaming replay encounters a body-only ` [cancelled]` or ` [error]` suffix. | Current markers stay in `streaming.py`; `execution_preparation.py` gives recognized structured status precedence and delegates body fallback to the streaming reader. |
| [`src/mindroom/legacy_revision_replay.py`][legacy-revision-replay] | Turn-record merges and redaction cleanup encounter reconstructed revision provenance from pre-v2026.9.43 summaries. | Current revision facts win, storage mutation stays in `turn_store.py`, and source-only summary ownership applies only to labeled historical replay. |
| [`src/mindroom/event_journal/legacy_schema.py`][journal-legacy-schema] | A journal has `journal_events` but lacks Nio-owned `matrix_sync_consumers`. | One schema transaction preserves history, handled turns, generation, and visible projection while retiring obsolete pending execution. |
| [`src/mindroom/config/legacy_access.py`][access-legacy] | Config loading or `mindroom config migrate` finds retired access fields. | Complete-source validation, concrete grants, a backup, and atomic membership-schema publication are retained. |
| [`src/mindroom/legacy_private_storage.py`][private-legacy], [`legacy_private_storage_aliases.py`][private-legacy-aliases], and [`private_storage_paths.py`][private-paths] | Startup finds a verified private scope with the historical requester spelling. | Intent records, owner and inode checks, worker quiescence, ordered renames, and verified aliases protect recovery and current callers. |
| [`src/mindroom/session_storage_preflight.py`][session-preflight] | An owned session table lacks required Agno columns. | The recovery lock, SQLite rollback recovery, and whole-directory archive complete before current storage creation. |
| [`src/mindroom/oauth/legacy_credentials.py`][oauth-legacy-credentials] | The OAuth SQLite store normalizes a retired field or verifies a lossless requester binding. | The store retains schema, scope, revision, reset-receipt, transaction, and rollback ownership; old OAuth JSON is not adopted. |
| [`src/mindroom/matrix/legacy_crypto_upgrade.py`][crypto-upgrade] | Nio first takes durable ownership of a pre-durable crypto store. | Nio's file lease and account/device checks protect keys and trust while only retired recovery rows are cleared. |

## Python provenance and regression coverage

Each historical Python boundary carries a source block naming its legacy format, last native old writer and replacement, current handling, and meaningful regression coverage.
Those source blocks are authoritative for exact release details and test node IDs; this index groups boundaries that share a behavioral test surface.
`Last legacy release` means the final stable tagged release whose native writer or typed model emitted the old representation, not the final reader that accepted it.
Continued acceptance or reader removal belongs in `Handling`, separate from the writer cutoff and replacement release.
When no stable tag contained an old native writer, the block uses an honest unreleased, unversioned external-input, or no-tagged-model classification; schema-based recovery likewise states that it has no single release cutoff.

| Boundary owners | Behavioral evidence |
| --- | --- |
| [`mcp_gateway/legacy_schema.py`][mcp-legacy-schema] | [Gateway OAuth][gateway-oauth-tests], [capacity][gateway-capacity-tests], [lifecycle][gateway-lifecycle-tests], and [account][gateway-account-tests] tests exercise the staged schema upgrades and released token cutoff. |
| [`legacy_session_storage.py`][legacy-session] | [Run-storage tests][agent-runs-tests] use a frozen Agno 2 fixture for merge, deletion, descendant, malformed-data, and transaction behavior; [usage tests][usage-tests] cover precedence and retained duplicates. |
| [`legacy_openai_tool_replay.py`][legacy-openai] | [OpenAI model tests][openai-model-tests] cover missing arguments, streamed placeholders, matching orphan results, copying, and input nonmutation. |
| [`legacy_handled_turns.py`][legacy-handled] and [`event_journal/legacy_turn_records.py`][legacy-turn-records] | [Handled-turn tests][handled-turn-tests] cover released JSON shapes, the deliberate unversioned cutoff, interrupted adoption, occupied indexes, reopen behavior, and reconstructed replay facts. |
| [`event_journal/legacy_schema.py`][journal-legacy-schema] | [Journal upgrade tests][journal-upgrade-tests] start from literal pre-Nio DDL and verify retained history and terminal facts, retired unfinished work, and stable repeat opening. |
| [`legacy_delivery_payloads.py`][legacy-delivery] and [`legacy_approval_payloads.py`][legacy-approval] | [Journal store][journal-store-tests], [response runner][response-runner-tests], and [approval][approval-tests] tests cover visible-result precedence, wire sanitation, frozen visibility, origin recovery, and sparse card identity. |
| [`matrix/legacy_sync_continuity.py`][legacy-sync] and [`matrix/legacy_crypto_upgrade.py`][crypto-upgrade] | [Sync-continuity tests][sync-continuity-tests] cover complete v2/v3 conversion and retry, while [crypto upgrade tests][crypto-upgrade-tests] verify that identity, keys, and trust survive retirement of transport recovery. |
| [`script_runs/legacy_schema.py`][script-legacy-schema] | [Script-run tests][script-run-tests] rebuild the literal old table, preserve every old value, add empty resource snapshots, and verify a second open. |
| [`knowledge/legacy_metadata.py`][knowledge-legacy] | [Knowledge indexing tests][knowledge-indexing-tests] use independently written metadata from each field boundary and check preservation, nonmutation, repeated normalization, and corpus/query compatibility. |
| [`matrix/legacy_state.py`][matrix-legacy-state] and [`matrix/users.py`][matrix-users] | [Matrix identity][matrix-identity-tests] and [agent manager][matrix-agent-tests] tests preserve durable account state, verify stable reloads, and exercise the missing-request fallback without network registration. |
| [`config/legacy_access.py`][access-legacy] and [`config/legacy_fields.py`][config-legacy] | [Access migration tests][access-migration-tests] cover validated conversion, exact backup bytes, stable publication, and rejection paths; [configuration tests][agent-config-tests] cover every directed retired-field error. |
| [`legacy_private_storage.py`][private-legacy] and [`legacy_private_storage_aliases.py`][private-legacy-aliases] | [Private-storage tests][private-storage-tests] cover verified owner relocation, content preservation, historical aliases, and tamper rejection. |
| [`oauth/legacy_credentials.py`][oauth-legacy-credentials] and [`oauth/credential_store.py`][oauth-store] | [OAuth store tests][oauth-store-tests] cover literal SQLite bindings, publication normalization, the removed JSON reader, reconnect disposition, and inert old files. |
| [`memory/auto_flush.py`][auto-flush], [`report_publishing/store.py`][report-store], [`scheduling.py`][scheduling], [`external_triggers/replay_store.py`][replay-store], and [`cli/owner.py`][cli-owner] | [Memory][memory-flush-tests], [report][report-tests], [scheduling][workflow-scheduling-tests], [trigger replay][trigger-replay-tests], and [pairing][cli-connect-tests] tests drive the retained defaults through their public read or mutation paths. |
| [`legacy_streaming.py`][legacy-streaming] and [`execution_preparation.py`][execution-preparation] | [Partial-reply][partial-reply-tests] and [streaming][streaming-tests] tests cover bounded historical suffixes, exact stripping order, current structured-status precedence, and interruption classification. |
| [`legacy_revision_replay.py`][legacy-revision-replay] | [Revision replay][legacy-revision-replay-tests], [turn-store][turn-store-tests], and [handled-turn][handled-turn-tests] tests cover reconstruction, monotonic preservation, historical and modern selection, and cold-reopen cleanup. |
| [`session_storage_preflight.py`][session-preflight] | [Session recovery tests][session-recovery-tests] cover schema-based archive, locks, rollback recovery, unrelated tables, current corruption, and byte preservation without inventing one release cutoff. |
| [SSO cookie routes][sso] | [SSO endpoint tests][sso-cookie-tests] assert exact shared-domain and host-only expiry cookies on both endpoints and retain current host-only behavior for localhost, IP addresses, and single-label hosts. |

This index intentionally excludes current authoring shorthands, protocol adapters, recovery rules, and caches that tolerate unknown versions because those are active interfaces rather than evidence of a retired native writer.
Sparse publication, job, and failure fields in [`knowledge/index_metadata.py`][knowledge-index] remain a current writer contract: the writer still omits optional values and the reader accepts those sparse in-progress and failed records.
Dependency-owned schemas remain attributed to their dependency, and removed readers remain documented as removed rather than recreated only to obtain conversion coverage.
The coverage delivered here is limited to Python owners, including the SSO route; inventoried SQL migrations, browser cleanup, and infrastructure setup below remain outside this implementation and carry no new annotation or test claim.

## Journal, delivery, approvals, and sync

Journal IDs use `J` to avoid colliding with credential IDs.

| ID | Status | Owner and reason |
| --- | --- | --- |
| J1 | Isolated | [`legacy_handled_turns.py`][legacy-handled] adopts pre-journal JSON once and renames it. |
| J2 | Isolated | [`handled_turns.py`][handled] keeps current sparse fields, [`turn_store.py`][turn-store] owns Agno run recovery and cleanup, [`legacy_handled_turns.py`][legacy-handled] reconstructs absent historical revision facts, and [`legacy_revision_replay.py`][legacy-revision-replay] owns their preservation and source-only summary decisions. |
| J3 | Removed/superseded | [`event_journal/legacy_schema.py`][journal-legacy-schema] replaces the old additive conversion framework with the Nio ownership cutoff. |
| J4 | Removed/superseded | [`event_journal/legacy_schema.py`][journal-legacy-schema] drops old interactive tables instead of archiving or translating them. |
| J5 | Removed/superseded | [`event_journal/legacy_schema.py`][journal-legacy-schema] retires the old response outbox rather than converting delivery debt. |
| J6 | Removed/superseded | [`event_journal/legacy_schema.py`][journal-legacy-schema] retires all pre-cutoff delivery tables, replacing the earlier unfenced-outbox guard. |
| J7 | Removed/superseded | [`event_journal/legacy_schema.py`][journal-legacy-schema] drops old approval transport and continuations rather than converting or tombstoning them. |
| J8 | Isolated | [`legacy_delivery_payloads.py`][legacy-delivery] owns inline FINAL results, precedence, markers, and wire sanitation. |
| J9 | Isolated | [`legacy_approval_payloads.py`][legacy-approval] rebuilds historical optional context; [`approval_execution.py`][approval-execution] keeps current exact execution gates. |
| J10 | Isolated | [`legacy_approval_payloads.py`][legacy-approval] owns the old card ID alias; [`approval_manager.py`][approval-manager] keeps current authentication, retries, tombstones, and fail-closed behavior. |
| J11 | Isolated | [`matrix/legacy_sync_continuity.py`][legacy-sync] converts valid v2/v3 join fences to current v4 and discards the obsolete checkpoint. |
| J12 | Removed/superseded | [The upgrade fixture][pre-journal-test] confirms old event-cache and dispatch-obligation files have no runtime reader and remain untouched. |
| J13 | Current behavior | [`event_journal_open.py`][journal-open] owns binding, generation, adoption, and database ownership guards. |
| J14 | Current behavior | [`sync_restart_retry.py`][restart-retry] and [`visible_response_reconciliation.py`][visible-recovery] keep current replay and visible-response safety. |

## Agent state, history, memory, and knowledge

| ID | Status | Owner and reason |
| --- | --- | --- |
| S1 | Isolated | [`script_runs/legacy_schema.py`][script-legacy-schema] adds old missing resource columns inside the current store transaction. |
| S2 | Isolated | [`knowledge/legacy_metadata.py`][knowledge-legacy] normalizes absent or empty optional filters. |
| S3 | Isolated | [`knowledge/legacy_metadata.py`][knowledge-legacy] retains empty historical sentinels for missing corpus settings, rebuilding only when the corresponding current corpus-compatibility value differs. |
| S4 | Current behavior | [`knowledge/index_metadata.py`][knowledge-index] deliberately writes and reads sparse publication, job, and failure lifecycle fields. |
| S5 | Current behavior | [`knowledge/collections.py`][knowledge-collections] protects current default and live collections as well as older published layouts. |
| S6 | Tiny retained default | [`memory/auto_flush.py`][auto-flush] discards two retired location fields while current worker identity sanitation and queue defaults remain. |
| S7 | Removed/superseded | [`memory/functions.py`][memory-functions] no longer contains or exports the monolithic memory-prompt wrapper. |
| S8 | Current behavior | [`ai_runtime.py`][ai-runtime] supports string input and deep-copies canonical message sequences for retries. |
| S9 | Isolated | [`legacy_openai_tool_replay.py`][legacy-openai] repairs old stored calls; current sparse-stream filtering stays in the adapters. |
| S10 | Current behavior | [`agent_storage.py`][agent-storage] and [`history/compaction.py`][history-compaction] own the current prompt persistence and replay boundary. |
| S11 | Removed/superseded | [`thread_export/storage.py`][thread-export] refuses populated markerless roots and marks only empty roots. |
| S12 | Tiny retained default | [`report_publishing/store.py`][report-store] treats missing `artifact_kind` as `html_file`. |
| S13 | Tiny retained default | [`scheduling.py`][scheduling] treats missing `history_limit` as the current `None` default. |
| S14 | Current behavior | [`history/storage.py`][history-storage] reads current v2 compaction state and ignores v1. |
| S15 | Current behavior | [`knowledge/candidate_checkpoint.py`][candidate-checkpoint] rebuilds unknown versions and retains current torn-tail recovery. |
| S16 | Current behavior | [`external_triggers/store.py`][trigger-store] and [`external_triggers/replay_store.py`][replay-store] own current validation and replay deduplication; the only retained historical default supplies an empty `threads` map when that section is absent. |
| S17 | Current behavior | [Receipts][scheduled-records], [todos][todo-state], [attachments][attachments], and workflows combine sparse fields with current identity and integrity checks. |
| S18 | Isolated | [`session_storage_preflight.py`][session-preflight] archives incompatible owned sessions; MindRoom does not invoke Agno's historical migration manager. |
| S19 | Isolated | [`legacy_session_storage.py`][legacy-session] owns Agno 2 blob merge, scrub, and double-JSON decoding; other Agno readers remain dependency-owned. |
| S20 | Dependency-owned | [`memory/config.py`][memory-config] leaves Mem0's history rewrite and default history path to Mem0. |

## Configuration and credentials

| ID | Status | Owner and reason |
| --- | --- | --- |
| C1 | Isolated | [`config/legacy_access.py`][access-legacy] converts retired reply-permission lists into membership access. |
| C2 | Current behavior | [`config/main.py`][config-main] accepts YAML null for optional root sections as a supported authoring form. |
| C3 | Current behavior | [`config/main.py`][config-main] normalizes supported string plugin entries to objects. |
| C4 | Current behavior | [`tool_system/plugin_imports.py`][plugin-imports] supports bare packages beside paths and explicit Python specs. |
| C5 | Current behavior | [`config/models.py`][config-models] reads and emits current string, compact, and explicit tool entries. |
| C6 | Current behavior | [`tool_system/metadata.py`][tool-metadata] keeps current text/list override interoperability. |
| C7 | Isolated | [`config/legacy_fields.py`][config-legacy] rejects retired fields with directed errors. |
| C8 | Current behavior | [`config/memory.py`][config-memory] supports `memory: none`. |
| C9 | Removed/superseded | [`cli/migrate.py`][cli-migrate] now applies access migration; the exact-text starter-memory converter is gone. |
| C10 | Tiny retained default | [`cli/owner.py`][cli-owner] replaces both old and current owner placeholders during pairing. |
| C11 | Current behavior | [`constants.py`][constants] owns current config, environment, and path selection without relocating data. |
| C12 | Current behavior | [`cli/local_stack.py`][local-stack] retains existing local-chat flags and container names. |
| A1 | Current behavior | [`credentials.py`][credentials] uses JSON, including its encrypted envelope, for generic services. |
| A2 | Current behavior | [`credentials_sync.py`][credentials-sync] treats missing `_source` as manually owned instead of overwriting it from the environment. |
| A3 | Current behavior | [`credentials.py`][credentials] grants untagged shared credentials only through current allowlists and worker policy. |
| A4 | Current behavior | [`credentials_sync.py`][credentials-sync] supports current inline, named, embedder, and shared OpenAI credential sources. |
| A5 | Current behavior | [`credentials_sync.py`][credentials-sync] supports current provider aliases and `NAME` or `NAME_FILE` secrets. |

## OAuth, Matrix state, and tools

| ID | Status | Owner and reason |
| --- | --- | --- |
| O1 | Removed/superseded | [`oauth/credential_store.py`][oauth-store] no longer adopts OAuth JSON; JSON-only tokens require reconnection and files remain untouched. |
| O2 | Removed/superseded | [`oauth/credential_store.py`][oauth-store] no longer performs opaque or deferred old JSON adoption. |
| O3 | Isolated | [`oauth/legacy_credentials.py`][oauth-legacy-credentials] removes the old publication field; obsolete JSON and sidecar cleanup is gone. |
| O4 | Current behavior | [`oauth/credential_store.py`][oauth-store] owns schema, private-file, and scope-binding validation. |
| O5 | Current behavior | [`oauth/credential_lifecycle.py`][oauth-lifecycle] owns target resolution, generation checks, and reset receipts. |
| O6 | Current behavior | [`oauth/client.py`][oauth-client] and [`credential_lifecycle.py`][oauth-lifecycle] support active provider dialects and configured original authentication. |
| M1 | Isolated | [`matrix/legacy_state.py`][matrix-legacy-state] backfills domains and asks the current state owner to rewrite noncanonical data. |
| M2 | Tiny retained default | [`matrix/users.py`][matrix-users] falls back from missing `requested_username` to persisted actual username. |
| M3 | Removed/superseded | [`thread_tags.py`][thread-tags] reads only one state event per thread-tag pair; the old thread-wide overlay is gone. |
| T1 | Current behavior | [`tools/python.py`][python-tools] publishes both installer names over one implementation. |
| T2 | Current behavior | [`tools/agentql.py`][agentql] adapts the currently installed AgentQL and browser-stealth combination. |
| T3 | Current behavior | [`tools/brandfetch.py`][brandfetch] and [`custom_tools/coding.py`][coding-tools] retain public option and tool naming. |
| T4 | Current behavior | [`api/dynamic_workflows.py`][workflow-api] deliberately returns 404 for the unscoped private-report route. |
| T5 | Current behavior | [`egress/policy.py`][egress-policy] retains the older allowlist path environment name as a deny-safe alias. |
| T6 | Current behavior | [Provider adapters][claude-compat] serve current request, replay, sampling, and tool-schema APIs. |
| T7 | Current behavior | [`tool_system/skills.py`][skills] supports OpenClaw metadata eligibility and its active tool preset. |

## Dependencies, SaaS, and deployment

These rows are checked manually because the original inventory used section headings rather than IDs.

| ID | Status | Owner and reason |
| --- | --- | --- |
| D1 | Dependency-owned | [`matrix/legacy_crypto_upgrade.py`][crypto-upgrade] isolates the pre-durable cutoff, while Nio owns its current SQLite schema and preserves crypto and trust records. |
| D2 | Dependency-owned | [`knowledge/indexing_config.py`][knowledge-settings] owns corpus compatibility, while Chroma owns storage-engine migrations. |
| D3 | Dependency-owned | [`memory/config.py`][memory-config] leaves Mem0's history-table rewrite and possibly external default history path to Mem0. |
| D4 | Current behavior | [The three SaaS SQL files][saas-migrations] remain explicit migrations for authoritative account, subscription, instance, payment, usage, audit, and grant data. |
| D5 | Current behavior | [SSO cookie cleanup][sso], [Terraform relocation][terraform-state], and [root service-worker cleanup][client-chart] remain deployment-owned; logger aliases and UI preferences are current state. |
| D6 | Current behavior | [Worker protocol checks][worker-compat] and [desktop protocol checks][desktop-protocol] protect current execution, identity reuse, metadata recovery, and replay gates. |
| D7 | Current behavior | [Provider][claude-compat], [Matrix protocol][event-info], dependency, and [cancellation][cancellation] adapters remain necessary after database reset. |
| D8 | Current behavior | [`session_storage_preflight.py`][session-preflight] is the concrete owned-session archive boundary; reset remains an explicit owner policy, not a generic exception fallback. |
| D9 | Current behavior | [Usage diagnostics][usage], [model overrides][thread-models], [invited rooms][invited-rooms], and [vocabulary cache][tag-vocabulary] deliberately use weak retention without an old conversion chain. |

## Upgrade and reset limits

An incompatible format is different from a locked database, permission failure, missing key, full disk, or current-schema corruption.
Migration owners reject those failures rather than converting them into deletion.
The OAuth credential and sync-continuity stores reject unsupported versions; other owners retain their existing version policies.
Several sparse readers deliberately ignore unknown fields or drop malformed reconstructible records.

Additional small compatibility branches stay with current readers.
[`execution_preparation.py`][execution-preparation] classifies structured stream status first and uses the old ` [cancelled]` and ` [error]` body suffixes owned by [`legacy_streaming.py`][legacy-streaming] only through the streaming reader fallback.
Interrupted visible replies are excluded; eligible in-progress text is cleaned before it is included in model context.
[`external_triggers/replay_store.py`][replay-store] supplies an empty `threads` map for replay stores written before thread keys existed.

A journal replacement must coordinate its generation binding with the next Nio baseline.
Agno sessions may still contain current handled-turn recovery facts and historical run blobs, while Matrix keeps visible messages and state independently of local storage.
Private storage moves require stopped primaries and absent managed workers, as described in [Private Storage Migration](https://docs.mindroom.chat/deployment/private-storage-upgrade/).
The Nio cutoff abandons pre-durable pending transport work while preserving crypto material, as described in [Nio 1.0 Upgrade](https://docs.mindroom.chat/deployment/nio-upgrade/).
Dependency migrations use their dependency's schema and locking contract, and SaaS databases are never treated as reconstructible caches.

[access-legacy]: https://github.com/mindroom-ai/mindroom/blob/main/src/mindroom/config/legacy_access.py
[agent-storage]: https://github.com/mindroom-ai/mindroom/blob/main/src/mindroom/agent_storage.py
[agentql]: https://github.com/mindroom-ai/mindroom/blob/main/src/mindroom/tools/agentql.py
[ai-runtime]: https://github.com/mindroom-ai/mindroom/blob/main/src/mindroom/ai_runtime.py
[approval-execution]: https://github.com/mindroom-ai/mindroom/blob/main/src/mindroom/approval_execution.py
[approval-manager]: https://github.com/mindroom-ai/mindroom/blob/main/src/mindroom/approval_manager.py
[attachments]: https://github.com/mindroom-ai/mindroom/blob/main/src/mindroom/attachments.py
[auto-flush]: https://github.com/mindroom-ai/mindroom/blob/main/src/mindroom/memory/auto_flush.py
[brandfetch]: https://github.com/mindroom-ai/mindroom/blob/main/src/mindroom/tools/brandfetch.py
[candidate-checkpoint]: https://github.com/mindroom-ai/mindroom/blob/main/src/mindroom/knowledge/candidate_checkpoint.py
[cancellation]: https://github.com/mindroom-ai/mindroom/blob/main/src/mindroom/cancellation.py
[claude-compat]: https://github.com/mindroom-ai/mindroom/blob/main/src/mindroom/claude_compat.py
[cli-migrate]: https://github.com/mindroom-ai/mindroom/blob/main/src/mindroom/cli/migrate.py
[cli-owner]: https://github.com/mindroom-ai/mindroom/blob/main/src/mindroom/cli/owner.py
[client-chart]: https://github.com/mindroom-ai/mindroom/tree/main/cluster/k8s/client
[coding-tools]: https://github.com/mindroom-ai/mindroom/blob/main/src/mindroom/custom_tools/coding.py
[config-legacy]: https://github.com/mindroom-ai/mindroom/blob/main/src/mindroom/config/legacy_fields.py
[config-main]: https://github.com/mindroom-ai/mindroom/blob/main/src/mindroom/config/main.py
[config-memory]: https://github.com/mindroom-ai/mindroom/blob/main/src/mindroom/config/memory.py
[config-models]: https://github.com/mindroom-ai/mindroom/blob/main/src/mindroom/config/models.py
[constants]: https://github.com/mindroom-ai/mindroom/blob/main/src/mindroom/constants.py
[credentials]: https://github.com/mindroom-ai/mindroom/blob/main/src/mindroom/credentials.py
[credentials-sync]: https://github.com/mindroom-ai/mindroom/blob/main/src/mindroom/credentials_sync.py
[crypto-upgrade]: https://github.com/mindroom-ai/mindroom/blob/main/src/mindroom/matrix/legacy_crypto_upgrade.py
[desktop-protocol]: https://github.com/mindroom-ai/mindroom/blob/main/src/mindroom/desktop/protocol.py
[egress-policy]: https://github.com/mindroom-ai/mindroom/blob/main/src/mindroom/egress/policy.py
[event-info]: https://github.com/mindroom-ai/mindroom/blob/main/src/mindroom/matrix/event_info.py
[handled]: https://github.com/mindroom-ai/mindroom/blob/main/src/mindroom/handled_turns.py
[history-compaction]: https://github.com/mindroom-ai/mindroom/blob/main/src/mindroom/history/compaction.py
[history-storage]: https://github.com/mindroom-ai/mindroom/blob/main/src/mindroom/history/storage.py
[invited-rooms]: https://github.com/mindroom-ai/mindroom/blob/main/src/mindroom/matrix/invited_rooms_store.py
[journal-open]: https://github.com/mindroom-ai/mindroom/blob/main/src/mindroom/event_journal_open.py
[journal-legacy-schema]: https://github.com/mindroom-ai/mindroom/blob/main/src/mindroom/event_journal/legacy_schema.py
[knowledge-collections]: https://github.com/mindroom-ai/mindroom/blob/main/src/mindroom/knowledge/collections.py
[knowledge-index]: https://github.com/mindroom-ai/mindroom/blob/main/src/mindroom/knowledge/index_metadata.py
[knowledge-legacy]: https://github.com/mindroom-ai/mindroom/blob/main/src/mindroom/knowledge/legacy_metadata.py
[knowledge-settings]: https://github.com/mindroom-ai/mindroom/blob/main/src/mindroom/knowledge/indexing_config.py
[legacy-revision-replay]: https://github.com/mindroom-ai/mindroom/blob/main/src/mindroom/legacy_revision_replay.py
[legacy-streaming]: https://github.com/mindroom-ai/mindroom/blob/main/src/mindroom/legacy_streaming.py
[legacy-approval]: https://github.com/mindroom-ai/mindroom/blob/main/src/mindroom/legacy_approval_payloads.py
[legacy-delivery]: https://github.com/mindroom-ai/mindroom/blob/main/src/mindroom/legacy_delivery_payloads.py
[legacy-handled]: https://github.com/mindroom-ai/mindroom/blob/main/src/mindroom/legacy_handled_turns.py
[legacy-openai]: https://github.com/mindroom-ai/mindroom/blob/main/src/mindroom/legacy_openai_tool_replay.py
[legacy-session]: https://github.com/mindroom-ai/mindroom/blob/main/src/mindroom/legacy_session_storage.py
[legacy-sync]: https://github.com/mindroom-ai/mindroom/blob/main/src/mindroom/matrix/legacy_sync_continuity.py
[legacy-turn-records]: https://github.com/mindroom-ai/mindroom/blob/main/src/mindroom/event_journal/legacy_turn_records.py
[local-stack]: https://github.com/mindroom-ai/mindroom/blob/main/src/mindroom/cli/local_stack.py
[matrix-legacy-state]: https://github.com/mindroom-ai/mindroom/blob/main/src/mindroom/matrix/legacy_state.py
[matrix-users]: https://github.com/mindroom-ai/mindroom/blob/main/src/mindroom/matrix/users.py
[mcp-legacy-schema]: https://github.com/mindroom-ai/mindroom/blob/main/src/mindroom/mcp_gateway/legacy_schema.py
[memory-config]: https://github.com/mindroom-ai/mindroom/blob/main/src/mindroom/memory/config.py
[memory-functions]: https://github.com/mindroom-ai/mindroom/blob/main/src/mindroom/memory/functions.py
[oauth-client]: https://github.com/mindroom-ai/mindroom/blob/main/src/mindroom/oauth/client.py
[oauth-legacy-credentials]: https://github.com/mindroom-ai/mindroom/blob/main/src/mindroom/oauth/legacy_credentials.py
[oauth-lifecycle]: https://github.com/mindroom-ai/mindroom/blob/main/src/mindroom/oauth/credential_lifecycle.py
[oauth-store]: https://github.com/mindroom-ai/mindroom/blob/main/src/mindroom/oauth/credential_store.py
[plugin-imports]: https://github.com/mindroom-ai/mindroom/blob/main/src/mindroom/tool_system/plugin_imports.py
[pre-journal-test]: https://github.com/mindroom-ai/mindroom/blob/main/tests/test_upgrade_from_pre_journal_storage.py
[private-legacy-aliases]: https://github.com/mindroom-ai/mindroom/blob/main/src/mindroom/legacy_private_storage_aliases.py
[private-legacy]: https://github.com/mindroom-ai/mindroom/blob/main/src/mindroom/legacy_private_storage.py
[private-paths]: https://github.com/mindroom-ai/mindroom/blob/main/src/mindroom/private_storage_paths.py
[python-tools]: https://github.com/mindroom-ai/mindroom/blob/main/src/mindroom/tools/python.py
[replay-store]: https://github.com/mindroom-ai/mindroom/blob/main/src/mindroom/external_triggers/replay_store.py
[report-store]: https://github.com/mindroom-ai/mindroom/blob/main/src/mindroom/report_publishing/store.py
[restart-retry]: https://github.com/mindroom-ai/mindroom/blob/main/src/mindroom/sync_restart_retry.py
[saas-migrations]: https://github.com/mindroom-ai/mindroom/tree/main/saas-platform/supabase/migrations
[scheduled-records]: https://github.com/mindroom-ai/mindroom/blob/main/src/mindroom/scheduled_run_records.py
[scheduling]: https://github.com/mindroom-ai/mindroom/blob/main/src/mindroom/scheduling.py
[script-legacy-schema]: https://github.com/mindroom-ai/mindroom/blob/main/src/mindroom/script_runs/legacy_schema.py
[session-preflight]: https://github.com/mindroom-ai/mindroom/blob/main/src/mindroom/session_storage_preflight.py
[skills]: https://github.com/mindroom-ai/mindroom/blob/main/src/mindroom/tool_system/skills.py
[sso]: https://github.com/mindroom-ai/mindroom/blob/main/saas-platform/platform-backend/src/backend/routes/sso.py
[tag-vocabulary]: https://github.com/mindroom-ai/mindroom/blob/main/src/mindroom/thread_tag_vocabulary.py
[terraform-state]: https://github.com/mindroom-ai/mindroom/blob/main/cluster/scripts/setup-terraform-state.sh
[thread-export]: https://github.com/mindroom-ai/mindroom/blob/main/src/mindroom/thread_export/storage.py
[thread-models]: https://github.com/mindroom-ai/mindroom/blob/main/src/mindroom/thread_models.py
[thread-tags]: https://github.com/mindroom-ai/mindroom/blob/main/src/mindroom/thread_tags.py
[turn-store]: https://github.com/mindroom-ai/mindroom/blob/main/src/mindroom/turn_store.py
[todo-state]: https://github.com/mindroom-ai/mindroom/blob/main/src/mindroom/custom_tools/todo_state.py
[tool-metadata]: https://github.com/mindroom-ai/mindroom/blob/main/src/mindroom/tool_system/metadata.py
[trigger-store]: https://github.com/mindroom-ai/mindroom/blob/main/src/mindroom/external_triggers/store.py
[usage]: https://github.com/mindroom-ai/mindroom/blob/main/src/mindroom/usage_stats_storage.py
[visible-recovery]: https://github.com/mindroom-ai/mindroom/blob/main/src/mindroom/visible_response_reconciliation.py
[worker-compat]: https://github.com/mindroom-ai/mindroom/blob/main/src/mindroom/workers/compatibility.py
[workflow-api]: https://github.com/mindroom-ai/mindroom/blob/main/src/mindroom/api/dynamic_workflows.py
[execution-preparation]: https://github.com/mindroom-ai/mindroom/blob/main/src/mindroom/execution_preparation.py
[streaming]: https://github.com/mindroom-ai/mindroom/blob/main/src/mindroom/streaming.py
[access-migration-tests]: https://github.com/mindroom-ai/mindroom/blob/main/tests/test_access_migration.py
[agent-config-tests]: https://github.com/mindroom-ai/mindroom/blob/main/tests/test_agents.py
[agent-runs-tests]: https://github.com/mindroom-ai/mindroom/blob/main/tests/test_agent_storage_runs.py
[approval-tests]: https://github.com/mindroom-ai/mindroom/blob/main/tests/test_tool_approval.py
[cli-connect-tests]: https://github.com/mindroom-ai/mindroom/blob/main/tests/test_cli_connect.py
[crypto-upgrade-tests]: https://github.com/mindroom-ai/mindroom/blob/main/tests/test_legacy_crypto_upgrade.py
[gateway-account-tests]: https://github.com/mindroom-ai/mindroom/blob/main/tests/test_mcp_gateway_accounts.py
[gateway-capacity-tests]: https://github.com/mindroom-ai/mindroom/blob/main/tests/test_mcp_gateway_oauth_capacity.py
[gateway-lifecycle-tests]: https://github.com/mindroom-ai/mindroom/blob/main/tests/test_mcp_gateway_lifecycle.py
[gateway-oauth-tests]: https://github.com/mindroom-ai/mindroom/blob/main/tests/test_mcp_gateway_oauth.py
[handled-turn-tests]: https://github.com/mindroom-ai/mindroom/blob/main/tests/test_handled_turns.py
[journal-store-tests]: https://github.com/mindroom-ai/mindroom/blob/main/tests/test_event_journal_store.py
[journal-upgrade-tests]: https://github.com/mindroom-ai/mindroom/blob/main/tests/test_journal_upgrade_boundary.py
[knowledge-indexing-tests]: https://github.com/mindroom-ai/mindroom/blob/main/tests/test_knowledge_indexing_config.py
[legacy-revision-replay-tests]: https://github.com/mindroom-ai/mindroom/blob/main/tests/test_legacy_revision_replay.py
[matrix-agent-tests]: https://github.com/mindroom-ai/mindroom/blob/main/tests/test_matrix_agent_manager.py
[matrix-identity-tests]: https://github.com/mindroom-ai/mindroom/blob/main/tests/test_matrix_identity.py
[memory-flush-tests]: https://github.com/mindroom-ai/mindroom/blob/main/tests/test_memory_auto_flush.py
[oauth-store-tests]: https://github.com/mindroom-ai/mindroom/blob/main/tests/test_oauth_credential_store.py
[openai-model-tests]: https://github.com/mindroom-ai/mindroom/blob/main/tests/test_openai_models.py
[partial-reply-tests]: https://github.com/mindroom-ai/mindroom/blob/main/tests/test_partial_reply_context.py
[private-storage-tests]: https://github.com/mindroom-ai/mindroom/blob/main/tests/test_private_storage_migration.py
[report-tests]: https://github.com/mindroom-ai/mindroom/blob/main/tests/test_report_publishing.py
[response-runner-tests]: https://github.com/mindroom-ai/mindroom/blob/main/tests/test_response_runner_focused.py
[script-run-tests]: https://github.com/mindroom-ai/mindroom/blob/main/tests/test_script_run_store.py
[session-recovery-tests]: https://github.com/mindroom-ai/mindroom/blob/main/tests/test_agent_session_storage_recovery.py
[sso-cookie-tests]: https://github.com/mindroom-ai/mindroom/blob/main/saas-platform/platform-backend/tests/test_sso_cookie_attrs.py
[streaming-tests]: https://github.com/mindroom-ai/mindroom/blob/main/tests/test_streaming_behavior.py
[sync-continuity-tests]: https://github.com/mindroom-ai/mindroom/blob/main/tests/test_sync_continuity_store.py
[trigger-replay-tests]: https://github.com/mindroom-ai/mindroom/blob/main/tests/test_external_trigger_replay_store.py
[turn-store-tests]: https://github.com/mindroom-ai/mindroom/blob/main/tests/test_turn_store.py
[usage-tests]: https://github.com/mindroom-ai/mindroom/blob/main/tests/test_usage_stats_storage.py
[workflow-scheduling-tests]: https://github.com/mindroom-ai/mindroom/blob/main/tests/test_workflow_scheduling.py
