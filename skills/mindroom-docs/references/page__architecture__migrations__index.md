# Migration and compatibility boundaries

MindRoom keeps substantive historical schemas and representations in named legacy modules.
Historical-format production owners use `legacy_<subject>.py` beside their current owner, including one-time upgrades and recurring old-data readers.
The current storage or lifecycle owner keeps its transaction, locking, validation, authorization, retry, and current-format processing.
Small field defaults stay with their current model when extraction would add indirection without isolating a meaningful migration.
Dependency-owned migrations and authoritative SaaS data remain under their existing owners.

This page maps every item from the migration audit to its current owner and disposition.
It describes the current source rather than promising support for every earlier release.

Each legacy compatibility comment starts with `# LEGACY_COMPAT: <short description of the legacy format>`, followed by its release provenance, handling, and coverage.
The marker also applies to documented defaults and compatibility notes kept beside current owners.
Find all marked Python rules, including the SaaS backend, from the repository root:

```bash
rg -n -F 'LEGACY_COMPAT:' --glob '*.py'
```

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
The remaining rows include existing focused boundaries and later audit additions.

| Boundary | Trigger and current caller | Retained guarantees |
| --- | --- | --- |
| [`src/mindroom/mcp_gateway/legacy_schema.py`][mcp-legacy-schema] | `GatewayOAuthStore` calls `migrate_schema` when opening SQLite. | The store retains its writer transaction, base DDL, and live processing; ordered expiry, accounting, lifecycle, account, and token-cutoff upgrades stay together. |
| `src/mindroom/legacy_usage_storage.py` | Startup or direct storage opening finds a session database without an independent usage table. | Import only retained facts, publishing the table and seed atomically; current reporting never reads old run representations. |
| `src/mindroom/legacy_usage_storage.py` | A retained request snapshot has no model or provider fields; current single-model compaction snapshots also use this shape. | Inherit a single known run model only when every request counter reconciles with the run and per-model totals; ambiguous mixed-model history remains unavailable. |
| [`src/mindroom/legacy_session_storage.py`][legacy-session] | Run deletion or initial usage migration encounters an Agno 2 `runs` blob. | Current rows win by `run_id`; descendant deletion, transaction ownership, and diagnostics remain with current owners. |
| [`src/mindroom/legacy_openai_tool_replay.py`][legacy-openai] | OpenAI-family adapters encounter missing tool arguments, sparse placeholders, or Agno-only Responses spans without reusable ordered output. | Request-only repairs preserve call/result links while supplying empty arguments, removing placeholder pairs, and dropping unverifiable reasoning tails and provider item IDs; canonical content rendering stays in `openai_response_replay.py`. |
| `src/mindroom/legacy_tool_credentials.py` | Primary or standalone API startup finds no receipt in the primary credential directory. | Only a `verify_ssl: false` in a `daytona` document is dropped, from the primary and every existing worker store, through the credential store's no-follow reads, encryption policy, and atomic writes; a lock beside the receipt serializes starting processes, the receipt keeps a later deliberate `false`, and it is withheld while any stored document is unreadable. |
| `src/mindroom/legacy_tool_credentials.py` | Primary or standalone API startup finds no worker-copy receipt in the primary credential directory. | Documents of built-in tools that require the primary runtime or room context are deleted, unread, from every existing worker's own store through a no-follow directory descriptor; primary stores and worker shared-credential mirrors are kept, and the receipt is withheld while any worker store cannot be inspected or cleaned. |
| `src/mindroom/api/credentials_target.py` | A dashboard delete of a scoped tool's settings, which the primary now owns. | The worker copy an older dashboard saved is deleted along with the primary copy, through the same credential store delete. |
| [`src/mindroom/legacy_handled_turns.py`][legacy-handled] | `HandledTurnLedger` finds `tracking/<agent>_responded.json`. | Insert-only adoption protects newer rows, fills absent indexes, retries interrupted work, and renames only after adoption. |
| [`src/mindroom/event_journal/legacy_turn_records.py`][legacy-turn-records] | The handled-turn importer adopts missing journal indexes. | The journal transaction is retained and migration writes never use current upsert deletion semantics. |
| [`src/mindroom/event_journal/legacy_response_attempts.py`][legacy-response-attempts] | Backend startup finds released approval/outbox tables without `response_attempts`. | One schema transaction adopts stable source identity, preserves pending approvals and frozen wire payloads, and aborts corrupt required live ownership; a second open does not repeat adoption. |
| [`src/mindroom/legacy_delivery_payloads.py`][legacy-delivery] | Outbox reads or Matrix writes encounter inline FINAL results and the bounded marker. | Old inline outcomes keep rolling-writer precedence, current local results remain authoritative otherwise, and full recovery data stays off the wire. |
| [`src/mindroom/legacy_approval_payloads.py`][legacy-approval] | Approval claim or resume encounters missing historical context or the older card ID. | Current approval ownership, authorization, exact-call checks, transaction settlement, and failure handling remain with current owners. |
| [`src/mindroom/event_journal/legacy_approval_recovery.py`][legacy-approval-recovery] | Approval settlement encounters an INITIAL retired by historical deleted-response cleanup. | The helper proves exact source and response tombstones with no FINAL; current owners retain card expiration, failure fencing, retries, locking, and transactional settlement. |
| [`src/mindroom/matrix/legacy_sync_continuity.py`][legacy-sync] | `SyncContinuityStore` loads a valid v2 or v3 record. | The helper validates the old shape, discards its checkpoint, increments revision once, and lets the store rewrite v4 under lock. |
| [`src/mindroom/external_triggers/legacy_replay_store.py`][legacy-replay-store] | An external trigger replay call finds the shared `external_triggers/replay.json`, including one written before thread keys existed. | The helper splits the file by replay scope and supplies an empty `threads` section; `ExternalTriggerReplayStore` validates every record before writing one current file per scope, deletes the old file under its lock, and fails closed on a malformed one. |
| [`src/mindroom/script_runs/legacy_schema.py`][script-legacy-schema] | `ScriptRunStore` finds missing resource snapshot columns. | Schema creation and the transaction stay in the store; old rows receive the established null or empty-map values. |
| [`src/mindroom/knowledge/legacy_metadata.py`][knowledge-legacy] | Knowledge parsing sees absent or empty optional filter fields. | Current parsing still rejects unknown fields; missing corpus settings retain empty historical sentinels and rebuild only when the corresponding current corpus-compatibility value differs. |
| [`src/mindroom/matrix/legacy_state.py`][matrix-legacy-state] | Matrix state has accounts without a domain or a noncanonical serialized shape. | Runtime-domain resolution, parsing, caching, and atomic persistence stay in `matrix/state.py`; rewrites happen only when data differs. |
| [`src/mindroom/config/legacy_fields.py`][config-legacy] | Agent or defaults validation sees a retired field. | Pydantic remains the strict validation boundary and the helper provides directed replacement errors. |
| [`src/mindroom/tool_system/legacy_tool_overrides.py`][tool-legacy-overrides] | Authored tool override validation sees a retired per-tool field such as `restrict_to_base_dir`. | `tool_system/metadata.py` keeps authored-override validation and raises the directed replacement error the helper names. |
| [`src/mindroom/legacy_streaming.py`][legacy-streaming] | Streaming replay encounters body-only `[cancelled]` or `[error]` suffixes (each preceded by one space). | Current markers stay in `streaming.py`; `execution_preparation.py` gives recognized structured status precedence and delegates body fallback to the streaming reader. |
| [`src/mindroom/history/legacy_compaction_state.py`][legacy-compaction-state] | Opening a conversation database whose archive tables do not exist yet finds v2 state carrying `compacted_run_ids` or last-compaction audit fields, or a replayed summary. | `migrate_compaction_database` creates the archive and, in the same transaction, records one content-free legacy generation per such scope with its tombstones, plus the summary and its seen ids for the scope that owns the summary, then strips the retired keys; the legacy summary keeps replaying, `history/storage.py` prunes tombstoned runs by archive membership, and it owns legacy redaction invalidation. |
| [`src/mindroom/legacy_revision_replay.py`][legacy-revision-replay] | Turn-record merges and redaction cleanup encounter reconstructed revision provenance from pre-v2026.9.43 summaries. | Current revision facts win, storage mutation stays in `turn_store.py`, and source-only summary ownership applies only to labeled historical replay. |
| [`src/mindroom/event_journal/legacy_schema.py`][journal-legacy-schema] | A journal lacks Nio-owned `matrix_sync_consumers`, a current approval generation has executable calls without toolkit origins, or approval calls lack an argument digest column. | Schema setup preserves history and frozen deliveries while retiring obsolete work; unresumable approvals enter normal failure recovery before another card decision, and calls without a recorded argument digest never execute, except that CLI recovery of a generated `agent` function runs the arguments saved in the journal's own CLI payload. |
| [`src/mindroom/config/legacy_access.py`][access-legacy] | Config loading or `mindroom config migrate` finds retired access fields. | Complete-source validation, concrete grants, a backup, and atomic membership-schema publication are retained. |
| [`src/mindroom/legacy_private_storage.py`][private-legacy], [`legacy_private_storage_aliases.py`][private-legacy-aliases], and [`private_storage_paths.py`][private-paths] | Startup without the `tracking/private_storage_migrated.json` receipt finds a verified private scope with the historical requester spelling. | Intent records, owner and inode checks, worker quiescence, ordered renames, and verified aliases protect recovery and current callers. Sandbox runners can write `private_instances`, so an entry there whose evidence is invalid stays untouched with a warning instead of stopping startup, including an interrupted move whose session mirror is missing or whose configured roots changed; only unexpected entries in a separate session root and checks across all pending moves, such as a missing volume, still stop startup. The receipt, written once a start finishes every verified move, keeps later starts from scanning `private_instances` again, so an entry left untouched is retried only after the operator restores what it needs and deletes `tracking/private_storage_migrated.json`. |
| `src/mindroom/legacy_state_root_records.py` | Primary or standalone API startup without the `tracking/state_root_records_moved.json` receipt finds invited-room, pending-invite, personal-room, or conversation-mode records inside `agents/` or `private_instances/`. | Each old file is read without following links or blocking; a valid one is copied below `tracking/` at the same relative path unless a record already exists there and is then removed, anything else stays in place with a warning, and the receipt stops later starts from reading the worker-mounted locations again. An invalid old invited-room ledger is not adopted, so that entity leaves the rooms it kept on its first room pass after the upgrade. |
| `src/mindroom/private_instance_identity_store.py` | A requester's turn finds its private scope's owner record without the primary's copy below `tracking/`, because the scope was created or last used before that copy existed. | The matching record is copied below `tracking/` at the scope's relative path; until then the thread exporter gives that instance no target and clears its export tree, so an owner record planted in `private_instances` never gains a copy unless its requester uses the agent. |
| [`src/mindroom/session_storage_preflight.py`][session-preflight] | An owned session table lacks required Agno columns. | The recovery lock, SQLite rollback recovery, and whole-directory archive complete before current storage creation. |
| [`src/mindroom/oauth/legacy_credentials.py`][oauth-legacy-credentials] | The OAuth SQLite store normalizes a retired field or verifies a lossless requester binding. | The store retains schema, scope, revision, reset-receipt, transaction, and rollback ownership; old OAuth JSON is not adopted. |
| [`src/mindroom/matrix/legacy_crypto_upgrade.py`][crypto-upgrade] | Nio first takes durable ownership of a pre-durable crypto store. | Nio's file lease and account/device checks protect keys and trust while only retired recovery rows are cleared. |
| [`src/mindroom/legacy_attachments.py`][legacy-attachments] | `load_attachment` finds a record whose `local_path` is outside the current `incoming_media/` directory. | A no-follow walk from the filesystem root and the recorded SHA-256 gate a capped retained copy; the owner rewrites the record atomically and deletes a record whose source can never verify, so sweeps do not retry it. |
| [`src/mindroom/desktop/legacy_command_journal.py`][desktop-legacy-journal] | The desktop SQLite journal finds JSON v1 receipts during its one-time import. | Historical validation stays isolated; the journal retains file permissions, atomic import, replay tombstones, response delivery state, sequence maxima, and admission capacity. |
| `src/mindroom/desktop/native_config.py` | Loading the saved desktop setup finds MindRoom's own app or desktop helper among the allowed applications, which releases through v2026.9.378 let users choose. | Only those two IDs are dropped from what is loaded, so the rest of the setup stays usable and editable and the next save writes it without them; every edit and one-run `--allow-app` still refuses them. |
| [`src/mindroom/workers/backends/legacy_docker_worker_metadata.py`][legacy-docker-worker-metadata] | Docker worker backend startup finds a worker whose lifecycle record is still inside its bind-mounted state root. | Only the worker key is read, without following links, and only when its digest-bound directory name matches; the backend writes a fresh idle control record, never trusts other in-mount fields, and still addresses containers only by the key-derived name and ownership labels. |
| [`src/mindroom/workers/backends/legacy_state_root_mounts.py`][legacy-state-root-mounts] | Primary startup, before anything is served, finds a running Kubernetes worker Deployment whose template hash differs from the `mindroom.ai/workspace-template-hash` this release records, including a template an older release rewrote after a downgrade, or a Docker worker container without the `mindroom.ai/storage-layout` label, created when workers mounted whole agent state roots. | Kubernetes scales such Deployments to zero and waits up to 60 seconds for their pods to exit, and Docker removes such containers, addressed only by the runtime's ownership labels, so the next ensure recreates them with workspace-only mounts; any failure fails startup after the other workers were attempted, so the primary restarts until none remain. One warning asks the operator to check agent state roots for links those workers may have planted. |

## Python provenance and regression coverage

Each historical Python boundary carries a source block naming its legacy format, last native old writer and replacement, current handling, and meaningful regression coverage.
Those source blocks are authoritative for exact release details and test node IDs; this index groups boundaries that share a behavioral test surface.
`Last legacy release` means the final stable tagged release whose native writer or typed model emitted the old representation, not the final reader that accepted it.
Continued acceptance or reader removal belongs in `Handling`, separate from the writer cutoff and replacement release.
When no stable tag contained an old native writer, the block uses an honest unreleased, unversioned external-input, or no-tagged-model classification; schema-based recovery likewise states that it has no single release cutoff.

| Boundary owners | Behavioral evidence |
| --- | --- |
| [`mcp_gateway/legacy_schema.py`][mcp-legacy-schema] | [Gateway OAuth][gateway-oauth-tests], [capacity][gateway-capacity-tests], [lifecycle][gateway-lifecycle-tests], and [account][gateway-account-tests] tests exercise the staged schema upgrades and released token cutoff. |
| `legacy_usage_storage.py` | `tests/test_legacy_usage_storage.py` covers mixed schemas, current-row precedence, unknown dates, interruption rollback and retry, dormant stores, symlink isolation, and both startup entry points. |
| `legacy_state_root_records.py` | `tests/test_legacy_state_root_records.py` covers every moved record kind, the receipt, planted junk, links, FIFOs, and misbound records left behind, and both startup entry points. |
| `private_instance_identity_store.py` | `tests/test_private_instance_identity.py::test_private_instances_for_agent_ignores_a_record_the_primary_never_wrote` covers a planted owner record getting no owner and an older scope gaining its owner at the requester's next turn. |
| `legacy_tool_credentials.py` | `tests/test_legacy_tool_credentials.py` covers plain and encrypted primary, worker, and worker-mirror stores, unrelated services, verifying settings left byte-for-byte, the receipt keeping a later deliberate `false`, the receipt waiting for a document readable only under the right key, a concurrent start rechecking the receipt under the lock, worker copies of primary-only tool settings deleted once while other stores and services are kept, the worker-copy receipt waiting for a store that could not be inspected or cleaned, and both startup entry points. |
| `api/credentials_target.py` | `tests/test_credentials.py::test_dashboard_delete_also_removes_the_worker_copy_of_tool_settings` covers a dashboard delete removing both the primary settings and the older worker copy. |
| [`legacy_session_storage.py`][legacy-session] | [Run-storage tests][agent-runs-tests] use a frozen Agno 2 fixture for merge, deletion, descendant, malformed-data, and transaction behavior; [usage tests][usage-tests] cover import precedence and available usage. |
| [`legacy_openai_tool_replay.py`][legacy-openai] | [OpenAI model tests][openai-model-tests] cover missing arguments and placeholder pairs; [Responses replay tests][openai-replay-tests] and [history tests][native-history-tests] cover reasoning tails, filtered call/result links, bounded SQLite replay, and unchanged canonical state. |
| [`legacy_handled_turns.py`][legacy-handled] and [`event_journal/legacy_turn_records.py`][legacy-turn-records] | [Handled-turn tests][handled-turn-tests] cover released JSON shapes, the deliberate unversioned cutoff, interrupted adoption, occupied indexes, reopen behavior, and reconstructed replay facts. |
| [`event_journal/legacy_schema.py`][journal-legacy-schema] | [Journal upgrade tests][journal-upgrade-tests] cover released DDL, missing and already-added toolkit columns, a missing argument digest column, rejection of stale approval clicks, preserved current generations and frozen deliveries, and stable repeat opening on both database backends. |
| [`legacy_delivery_payloads.py`][legacy-delivery] and [`legacy_approval_payloads.py`][legacy-approval] | [Journal store][journal-store-tests], [response runner][response-runner-tests], and [approval][approval-tests] tests cover visible-result precedence, wire sanitation, frozen visibility, origin recovery, and sparse card identity. |
| [`event_journal/legacy_approval_recovery.py`][legacy-approval-recovery] | [Journal store][journal-store-tests] tests preserve incomplete deletion proof and FINAL debt; [response runner][response-runner-tests] tests recover every approval state only after card expiration, without editing deleted responses or resuming tools. |
| [`matrix/legacy_sync_continuity.py`][legacy-sync] and [`matrix/legacy_crypto_upgrade.py`][crypto-upgrade] | [Sync-continuity tests][sync-continuity-tests] cover complete v2/v3 conversion and retry, while [crypto upgrade tests][crypto-upgrade-tests] verify that identity, keys, and trust survive retirement of transport recovery. |
| [`script_runs/legacy_schema.py`][script-legacy-schema] | [Script-run tests][script-run-tests] rebuild the literal old table, preserve every old value, add empty resource snapshots, and verify a second open. |
| [`knowledge/legacy_metadata.py`][knowledge-legacy] | [Knowledge indexing tests][knowledge-indexing-tests] use independently written metadata from each field boundary and check preservation, nonmutation, repeated normalization, and corpus/query compatibility. |
| [`matrix/legacy_state.py`][matrix-legacy-state] and [`matrix/users.py`][matrix-users] | [Matrix identity][matrix-identity-tests] and [agent manager][matrix-agent-tests] tests preserve durable account state, verify stable reloads, and exercise the missing-request fallback without network registration. |
| [`config/legacy_access.py`][access-legacy] and [`config/legacy_fields.py`][config-legacy] | [Access migration tests][access-migration-tests] cover validated conversion, exact backup bytes, stable publication, and rejection paths; [configuration tests][agent-config-tests] cover every directed retired-field error. |
| [`tool_system/legacy_tool_overrides.py`][tool-legacy-overrides] | [Tool metadata tests][tool-metadata-tests] cover the directed `restrict_to_base_dir` rejection for the `file`, `coding`, and `python` tools. |
| [`legacy_private_storage.py`][private-legacy] and [`legacy_private_storage_aliases.py`][private-legacy-aliases] | [Private-storage tests][private-storage-tests] cover verified owner relocation, content preservation, historical aliases, tamper rejection, and planted entries left untouched without stopping startup. |
| [`oauth/legacy_credentials.py`][oauth-legacy-credentials] and [`oauth/credential_store.py`][oauth-store] | [OAuth store tests][oauth-store-tests] cover literal SQLite bindings, publication normalization, the removed JSON reader, reconnect disposition, and inert old files. |
| [`oauth/providers.py`][oauth-providers], [`api/oauth.py`][api-oauth], and [`oauth/discovery.py`][oauth-discovery] | [MCP OAuth tests][mcp-oauth-tests] cover unbound credential rejection and legacy registration binding; [OAuth API tests][oauth-api-tests] cover rejection of pending state without a bound endpoint. |
| [`memory/auto_flush.py`][auto-flush], [`report_publishing/store.py`][report-store], [`scheduling.py`][scheduling], [`custom_tools/todo_poke.py`][todo-poke], [`custom_tools/todo.py`][todo-tool], [`delegation/state.py`][delegation-state], and [`cli/owner.py`][cli-owner] | [Memory][memory-flush-tests], [report][report-tests], [scheduling][workflow-scheduling-tests], [todo poke][todo-poke-tests], [todo tool][todo-builtin-tests], [delegation mode][delegation-minimal-tests], and [pairing][cli-connect-tests] tests drive the retained defaults through their public read or mutation paths. |
| [`external_triggers/legacy_replay_store.py`][legacy-replay-store] | [Trigger replay tests][trigger-replay-tests] split a shared file with several scopes and one without a `threads` section, keep every claim deduplicated afterwards, and reject malformed records. |
| [`legacy_streaming.py`][legacy-streaming] and [`execution_preparation.py`][execution-preparation] | [Partial-reply][partial-reply-tests] and [streaming][streaming-tests] tests cover bounded historical suffixes, exact stripping order, current structured-status precedence, and interruption classification. |
| [`history/legacy_compaction_state.py`][legacy-compaction-state] | [Legacy compaction tests][legacy-compaction-state-tests] cover recognition, one-time migration, summary attribution, tombstone pruning, and malformed metadata; [compaction redaction tests][compaction-redaction-tests] cover legacy invalidation. |
| [`legacy_revision_replay.py`][legacy-revision-replay] | [Revision replay][legacy-revision-replay-tests], [turn-store][turn-store-tests], and [handled-turn][handled-turn-tests] tests cover reconstruction, monotonic preservation, historical and modern selection, and cold-reopen cleanup. |
| [`session_storage_preflight.py`][session-preflight] | [Session recovery tests][session-recovery-tests] cover schema-based archive, locks, rollback recovery, unrelated tables, current corruption, and byte preservation without inventing one release cutoff. |
| [`legacy_attachments.py`][legacy-attachments] | [Attachment tests][attachment-tests] cover verified adoption and record rewrite, independence from later source swaps, and rejection and one-time removal of records with planted leaf or ancestor links, missing sources, changed bytes, and missing digests, and retention of records after retained-copy write failures. |
| [`desktop/legacy_command_journal.py`][desktop-legacy-journal] | [Desktop journal tests][desktop-journal-tests] cover bodyless started receipts, retained sequence high-watermarks, deferred response replay, repeated opens, and independent current admission capacity. |
| `desktop/native_config.py` | `tests/test_desktop_native_config.py::test_saved_config_that_allows_mindroom_itself_loads_and_saves_without_it` covers a saved setup that allows both IDs loading and saving without them while an app edit naming one is still refused. |
| [`workers/backends/legacy_docker_worker_metadata.py`][legacy-docker-worker-metadata] | [Docker worker tests][docker-worker-tests] adopt a released in-mount record for listing, idle cleanup, and retirement while ignoring its container fields, and skip foreign-key, symlinked, and malformed records. |
| [`scripts/local_mindroom_provisioning_service.py`][provisioning-service] | [Provisioning service tests][provisioning-service-tests] load pair sessions persisted before device pairing as browser-initiated sessions without device fields. |
| [`scripts/local_mindroom_provisioning_service.py`][provisioning-service] | [Provisioning service tests][provisioning-service-tests] load device pair sessions persisted without a requester address and inspect them with no address. |
| [`scripts/local_mindroom_provisioning_service.py`][provisioning-service] | [Provisioning service tests][provisioning-service-tests] accept the deployed chat client's Matrix access-token headers on browser-initiated pairing and connection endpoints, prefer the OpenID token when both are sent, and reject access tokens on device inspect and approve. |
| [`scripts/local_mindroom_provisioning_service.py`][provisioning-service] | [Provisioning service tests][provisioning-service-tests] register an older client's own agent password unchanged and return no password for it. |
| [`workers/backends/kubernetes_resources.py`][kubernetes-resources] | [Kubernetes worker tests][kubernetes-worker-tests] remove a planted key from a per-worker Secret and from a shared worker-auth Secret when the worker is ensured, and null both entries when the worker is cleaned up. |
| [`scripts/local_mindroom_provisioning_service.py`][provisioning-service] | [Provisioning service tests][provisioning-service-tests] import a `state.json` written by earlier versions into the new SQLite state database on the first start that succeeds, import again after an interrupted first start, never read the file afterwards, and open the `.sqlite3` database beside a `.json` state path, importing that file the same way. |
| [`workers/backends/legacy_state_root_mounts.py`][legacy-state-root-mounts] | [State-root mount tests][legacy-state-root-mounts-tests] cover backend dispatch, Kubernetes configuration, API, and transport failures that fail retirement, and startup that fails before anything serves; [Docker worker tests][docker-worker-tests] remove only unlabeled containers, and [Kubernetes worker tests][kubernetes-worker-tests] stop real old-template and downgraded Deployments, wait for their live pods, and rebuild them from the current template. |
| [SSO cookie routes][sso] | [SSO endpoint tests][sso-cookie-tests] assert host-only token cookies and exact legacy shared-domain expiry cookies on both endpoints, and emit no domain cookie for localhost, IP addresses, and single-label hosts. |
| [Hosted instance lifecycle][instance-lifecycle] and [`legacy_instance_lifecycle.py`][legacy-instance-lifecycle] | [Lifecycle tests][instance-lifecycle-tests] mark an instance an older soft delete left `deprovisioned` while its deployment kept running as running again, then hold it or keep it running for an entitled subscription, leave one without a deployment alone, and look only during nightly runs and for accounts pending deletion. |

This index intentionally excludes current authoring shorthands, protocol adapters, recovery rules, and caches that tolerate unknown versions because those are active interfaces rather than evidence of a retired native writer.
Sparse publication, job, and failure fields in [`knowledge/index_metadata.py`][knowledge-index] remain a current writer contract: the writer still omits optional values and the reader accepts those sparse in-progress and failed records.
Dependency-owned schemas remain attributed to their dependency, and removed readers remain documented as removed rather than recreated only to obtain conversion coverage.
The coverage delivered here is limited to Python owners, including the SSO route and the hosted instance lifecycle; inventoried SQL migrations, browser cleanup, and infrastructure setup below remain outside this implementation and carry no new annotation or test claim.

The explicit response attempt schema replaces ownership inference used by the last verified native writer, v2026.9.137.
SQLite holds its startup writer transaction and PostgreSQL its schema advisory lock while the migration runs.
Literal released SQL and JSON in [response attempt migration tests][response-attempt-migration-tests] cover both backends, rollback, repeated opens, inline result precedence, ordinary completed turns, and retained delivery debt.
The migration assumes no active responses; current callers pass typed ownership independently of terminal recovery snapshots.

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
| J15 | Isolated | [`event_journal/legacy_approval_recovery.py`][legacy-approval-recovery] recognizes approvals stranded by historical INITIAL retirement; current owners retain consent, failure handling, and settlement. |
| J16 | Current behavior | [`turn_store.py`][turn-store] does not migrate ledger tombstones that carry no room, so one that v2026.10.30 or earlier wrote for a redaction delivered in another room still marks its event handled and blocks replies in threads that contain it until ledger retention drops it. |

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
| S10 | Current behavior | [`agent_storage.py`][agent-storage] and [`history/replay.py`][history-replay] own the current prompt persistence and replay boundary. |
| S11 | Removed/superseded | [`thread_export/storage.py`][thread-export] refuses populated markerless roots and marks only empty roots. |
| S12 | Tiny retained default | [`report_publishing/store.py`][report-store] treats missing `artifact_kind` as `html_file`. |
| S13 | Tiny retained default | [`scheduling.py`][scheduling] treats missing `history_limit` as the current `None` default. |
| S14 | Isolated | [`history/storage.py`][history-storage] reads current v2 compaction state and ignores v1; [`history/legacy_compaction_state.py`][legacy-compaction-state] migrates, once when the conversation database opens, state written before compacted runs were archived. |
| S15 | Current behavior | [`knowledge/candidate_checkpoint.py`][candidate-checkpoint] rebuilds unknown versions and retains current torn-tail recovery. |
| S16 | Isolated | [`external_triggers/store.py`][trigger-store] and [`external_triggers/replay_store.py`][replay-store] own current validation and replay deduplication; [`external_triggers/legacy_replay_store.py`][legacy-replay-store] splits the shared replay file into per-scope files and supplies an empty `threads` map when that section is absent. |
| S17 | Current behavior | [Receipts][scheduled-records], [todos][todo-state], [attachments][attachments], and workflows combine sparse fields with current identity and integrity checks. |
| S18 | Isolated | [`session_storage_preflight.py`][session-preflight] archives incompatible owned sessions; MindRoom does not invoke Agno's historical migration manager. |
| S19 | Isolated | [`legacy_session_storage.py`][legacy-session] owns Agno 2 blob scrub and double-JSON decoding; other Agno readers remain dependency-owned. |
| S20 | Dependency-owned | [`memory/config.py`][memory-config] leaves Mem0's history rewrite and default history path to Mem0. |
| S21 | Isolated | [`legacy_attachments.py`][legacy-attachments] adopts in-place attachment records into verified retained copies; [`attachments.py`][attachments] keeps copying, record publication, and retention cleanup. |
| S22 | Tiny retained default | [`custom_tools/todo_poke.py`][todo-poke] parses a todo item without `requester_id` but never pokes it, and [`custom_tools/todo.py`][todo-tool] records the current requester on the item's next write. |
| S23 | Isolated | [`knowledge/legacy_git_checkout.py`][knowledge-legacy-git] moves a checkout's in-tree `.git` aside and hard-links only its repository files into a fresh MindRoom-owned Git directory under a new config; [`knowledge/git_source.py`][knowledge-git-source] keeps initialization, sync, and the current layout. |
| S24 | Tiny retained default | [`delegation/state.py`][delegation-state] reads a retained subagent without `agent_mode` as standard, so delegations saved before minimal subagents continue unchanged. |

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
| C13 | Isolated | [`tool_system/legacy_tool_overrides.py`][tool-legacy-overrides] names the retired per-tool `restrict_to_base_dir` override and its replacement guidance; [`tool_system/metadata.py`][tool-metadata] raises the directed error during authored-override validation. |
| C14 | Tiny retained default | [`scripts/local_mindroom_provisioning_service.py`][provisioning-service] loads pair sessions persisted before device pairing, which lack device fields, as browser-initiated sessions. |
| C15 | Tiny retained default | [`scripts/local_mindroom_provisioning_service.py`][provisioning-service] still resolves Matrix access tokens through whoami on browser-initiated pairing and connection endpoints when the deployed chat client sends no OpenID token. |
| C16 | Tiny retained default | [`scripts/local_mindroom_provisioning_service.py`][provisioning-service] loads device pair sessions persisted before requester addresses were recorded with no address, which the approval page shows as unknown. |
| C17 | Tiny retained default | [`scripts/local_mindroom_provisioning_service.py`][provisioning-service] still registers the agent password that released clients send to register-agent, instead of generating a one-time password, until no paired installs run a release older than the first release containing #2428. |
| C18 | Tiny retained default | [`scripts/local_mindroom_provisioning_service.py`][provisioning-service] imports the single `state.json` that earlier versions rewrote on every change into its SQLite state database once, on the first start with a new database, and opens the database beside a state path that still names the `.json` file; the import stays in the script because the service deploys as one standalone file. |
| A1 | Current behavior | [`credentials.py`][credentials] uses JSON, including its encrypted envelope, for generic services. |
| A2 | Current behavior | [`credentials_sync.py`][credentials-sync] treats missing `_source` as manually owned instead of overwriting it from the environment. |
| A3 | Current behavior | [`credentials.py`][credentials] grants untagged shared credentials only through current allowlists and worker policy. |
| A4 | Current behavior | [`credentials_sync.py`][credentials-sync] supports current inline, named, embedder, and shared OpenAI credential sources. |
| A5 | Current behavior | [`credentials_sync.py`][credentials-sync] supports current provider aliases and `NAME` or `NAME_FILE` secrets. |
| A6 | Tiny retained default | [`workers/backends/kubernetes_resources.py`][kubernetes-resources] nulls the credential encryption key entries that earlier releases stored in Kubernetes worker auth Secrets whenever it applies or cleans up a worker's Secret entry. |

## OAuth, Matrix state, and tools

| ID | Status | Owner and reason |
| --- | --- | --- |
| O1 | Removed/superseded | [`oauth/credential_store.py`][oauth-store] no longer adopts OAuth JSON; JSON-only tokens require reconnection and files remain untouched. |
| O2 | Removed/superseded | [`oauth/credential_store.py`][oauth-store] no longer performs opaque or deferred old JSON adoption. |
| O3 | Isolated | [`oauth/legacy_credentials.py`][oauth-legacy-credentials] removes the old publication field; obsolete JSON and sidecar cleanup is gone. |
| O4 | Current behavior | [`oauth/credential_store.py`][oauth-store] owns schema, private-file, and scope-binding validation. |
| O5 | Current behavior | [`oauth/credential_lifecycle.py`][oauth-lifecycle] owns target resolution, generation checks, and reset receipts. |
| O6 | Current behavior | [`oauth/client.py`][oauth-client] and [`credential_lifecycle.py`][oauth-lifecycle] support active provider dialects and configured original authentication. |
| O7 | Removed/superseded | [`oauth/providers.py`][oauth-providers] rejects refreshes of credentials without a `token_uri` endpoint binding, which only unstamped plugin exchanger or parser output produced; the lifecycle deletes them and the user reconnects. |
| O8 | Removed/superseded | [`api/oauth.py`][api-oauth] rejects pending connect state without a bound `token_url`; the state lives 600 seconds, so only connects that straddle an upgrade must restart. |
| O9 | Tiny retained default | [`oauth/discovery.py`][oauth-discovery] binds a dynamic client registration without a recorded token endpoint to the endpoint resolved at first use, keeping its client and tokens; later endpoint changes re-register. |
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
| D4 | Current behavior | [The SaaS SQL files][saas-migrations] remain explicit migrations for authoritative account, subscription, instance, payment, usage, audit, and grant data; the first run of `005_account_deletion.sql` restarts the grace period of deletion requests older than 7 days, whose instances older releases left running, and records that run by adding `accounts.hard_delete_started_at`. |
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
[`execution_preparation.py`][execution-preparation] classifies structured stream status first and uses the old `[cancelled]` and `[error]` body suffixes (each preceded by one space) owned by [`legacy_streaming.py`][legacy-streaming] only through the streaming reader fallback.
Interrupted visible replies are excluded; eligible in-progress text is cleaned before it is included in model context.

A journal replacement must coordinate its generation binding with the next Nio baseline.
Agno sessions may still contain current handled-turn recovery facts and historical run blobs, while Matrix keeps visible messages and state independently of local storage.
Private storage moves require stopped primaries and absent managed workers, as described in [Private Storage Migration](https://docs.mindroom.chat/deployment/private-storage-upgrade/).
Usage discovery ignores verified historical primary and session aliases because their canonical directories are scanned separately; unverified symlinks still report incomplete coverage.
The Nio cutoff abandons pre-durable pending transport work while preserving crypto material, as described in [Nio 1.0 Upgrade](https://docs.mindroom.chat/deployment/nio-upgrade/).
Dependency migrations use their dependency's schema and locking contract, and SaaS databases are never treated as reconstructible caches.
Primary-process host browser profiles moved from `<storage>/browser-profiles` into each agent's state root, and the old directory is no longer read, because nothing records which agent or requester signed in to it; sign in again and delete it.
Default host browser screenshots, PDFs, and downloads likewise moved from `<storage>/browser` to `browser/` in each agent's state root, so `upload` no longer accepts files left in the old directory; move any still needed into the agent workspace, then delete it.
A `private.root` may no longer start with `browser` or `browser-profiles`, the directories the primary uses beside the private workspace.
Chromium in the host, headless worker, and Computer browsers and in `crawl4ai` no longer reads proxy settings from the environment; its only proxy is MindRoom's destination relay, which opens HTTP `CONNECT` tunnels through the configured egress proxy.
That proxy must now allow `CONNECT` to port 80 for plain-HTTP pages, which it previously received as ordinary proxied requests; the chart-managed approved egress proxy requires mindroom-egress-proxy v0.1.10 or later, so set `approvedEgress.image.tag: v0.1.10` before upgrading.
The primary's egress proxy must also allow `CONNECT` to IP addresses on ports 80 and 443, because the primary tunnels to the address it validated; egress proxies that allow only hostnames are no longer supported for the primary browser.
In the primary, a SOCKS proxy, a proxy URL with credentials or a path, `auto_proxy`, or `socks_server` is now ignored with a warning and those destinations are dialed directly, and `no_proxy` applies only to browsers with `allow_private_networks`.
A sandbox runner refuses to start a browser when its proxy variables name different proxies or one it cannot follow, so set them all, or only `all_proxy`, to the one HTTP(S) egress proxy.
Stored tool config values for boolean fields, such as credential seeds read from environment variables or files, must now be JSON booleans or exactly `true` or `false`; any other value makes that tool fail to load with an error naming the field.

### Workspace-only worker mounts

v2026.9.327 mounts only agent workspaces into dedicated Docker and Kubernetes workers.
Drain worker activity before upgrading, because primary startup stops every worker from an older release before it serves anything, which ends its tool calls, shells, background scripts, and CLI sessions.
Until every such worker is stopped, startup fails and the primary restarts, so the Docker daemon or Kubernetes API must be reachable when it starts.
Upgrade worker images in lockstep with the primary: the worker protocol is now 2 and the Docker backend refuses older images, while Kubernetes workers run the configured worker image, so that image must come from the same release.
Rolling back to v2026.9.326 is safe when the Docker and Kubernetes worker images roll back together with the primary; that release recreates workers with its state-root mounts on their next use, and upgrading again stops them at startup even though they keep this release's annotation, because their template hash no longer matches it.

Before upgrading, check two new limits:

- A `private.root` may no longer start with a name the primary writes beside the private workspace: `sessions`, `learning`, `chroma`, `knowledge_db`, `memory_files`, `calls`, `agent_modes.json`, `agent_modes.lock`, or `.sessions-recovery.lock`; such a configuration now fails validation, so rename the root first.
- Knowledge files above 64 MiB are left out of every knowledge base, including operator-managed ones, and the next refresh removes their existing vectors; find them with `find <knowledge folder> -type f -size +64M`.

Workers from older releases could write agent state roots, so after the upgrade check those roots for links they may have planted, running each command once with GNU find:

```bash
find "$STORAGE/agents" -mindepth 2 -type l -print -o -type d -regex "$STORAGE/agents/[^/]*/workspace" -prune
find "$STORAGE/private_instances" -mindepth 2 -type l -print -o -type d -regex "$STORAGE/private_instances/[^/]*/\([^/]*\)/\1_data" -prune
find "$STORAGE/agents" "$STORAGE/private_instances" -type f -links +1 -print
```

The second command skips private workspaces at the default `private.root` of `<agent>_data`; when some agents set another root, pair every private agent with its root, default ones included, in one alternation instead, such as `-regex "$STORAGE/private_instances/[^/]*/\(notes/notes_data\|mail/inbox\)"`, because a run per root would print the other agents' own workspace links, and roots listed without their agents would also skip a directory of that name under any other agent.
The first two commands skip links inside workspaces, which are the workers' own, and verified legacy aliases directly below `private_instances`; remove every link they print, including a workspace that is itself a link, and inspect hard-linked files for data copied out of another instance.

### Config-free runners and workers

MindRoom no longer mounts the primary's config into the `static_runner` sidecar or dedicated Kubernetes workers, which take agent settings only from the allowlisted snapshot each request carries.
The runtime chart's `workers.kubernetes.configMapName`, `configKey`, and `configPath` values and the matching `MINDROOM_KUBERNETES_WORKER_CONFIG_*` settings are removed.
Every script recovery signature written by an older release covers those removed worker settings, so background scripts still running on Kubernetes workers during the upgrade no longer verify and are interrupted once when the primary starts.
For the same reason the unversioned and pre-seccomp recovery-signature migrations were removed: no record they could adopt still verifies.
Kubernetes workers pick up their new pod template when they are next recreated.
Plugin directories beside a file-sourced config are no longer visible to the sidecar or Kubernetes workers, so install proxied plugins as Python packages in the runner image.

[access-legacy]: https://github.com/mindroom-ai/mindroom/blob/main/src/mindroom/config/legacy_access.py
[agent-storage]: https://github.com/mindroom-ai/mindroom/blob/main/src/mindroom/agent_storage.py
[agentql]: https://github.com/mindroom-ai/mindroom/blob/main/src/mindroom/tools/agentql.py
[ai-runtime]: https://github.com/mindroom-ai/mindroom/blob/main/src/mindroom/ai_runtime.py
[api-oauth]: https://github.com/mindroom-ai/mindroom/blob/main/src/mindroom/api/oauth.py
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
[desktop-legacy-journal]: https://github.com/mindroom-ai/mindroom/blob/main/src/mindroom/desktop/legacy_command_journal.py
[desktop-journal-tests]: https://github.com/mindroom-ai/mindroom/blob/main/tests/test_desktop_command_journal.py
[attachment-tests]: https://github.com/mindroom-ai/mindroom/blob/main/tests/test_attachments.py
[desktop-protocol]: https://github.com/mindroom-ai/mindroom/blob/main/src/mindroom/desktop/protocol.py
[docker-worker-tests]: https://github.com/mindroom-ai/mindroom/blob/main/tests/test_docker_worker_backend.py
[egress-policy]: https://github.com/mindroom-ai/mindroom/blob/main/src/mindroom/egress/policy.py
[event-info]: https://github.com/mindroom-ai/mindroom/blob/main/src/mindroom/matrix/event_info.py
[handled]: https://github.com/mindroom-ai/mindroom/blob/main/src/mindroom/handled_turns.py
[history-replay]: https://github.com/mindroom-ai/mindroom/blob/main/src/mindroom/history/replay.py
[history-storage]: https://github.com/mindroom-ai/mindroom/blob/main/src/mindroom/history/storage.py
[invited-rooms]: https://github.com/mindroom-ai/mindroom/blob/main/src/mindroom/matrix/invited_rooms_store.py
[journal-open]: https://github.com/mindroom-ai/mindroom/blob/main/src/mindroom/event_journal_open.py
[journal-legacy-schema]: https://github.com/mindroom-ai/mindroom/blob/main/src/mindroom/event_journal/legacy_schema.py
[knowledge-collections]: https://github.com/mindroom-ai/mindroom/blob/main/src/mindroom/knowledge/collections.py
[knowledge-git-source]: https://github.com/mindroom-ai/mindroom/blob/main/src/mindroom/knowledge/git_source.py
[knowledge-index]: https://github.com/mindroom-ai/mindroom/blob/main/src/mindroom/knowledge/index_metadata.py
[knowledge-legacy]: https://github.com/mindroom-ai/mindroom/blob/main/src/mindroom/knowledge/legacy_metadata.py
[knowledge-legacy-git]: https://github.com/mindroom-ai/mindroom/blob/main/src/mindroom/knowledge/legacy_git_checkout.py
[knowledge-settings]: https://github.com/mindroom-ai/mindroom/blob/main/src/mindroom/knowledge/indexing_config.py
[kubernetes-resources]: https://github.com/mindroom-ai/mindroom/blob/main/src/mindroom/workers/backends/kubernetes_resources.py
[kubernetes-worker-tests]: https://github.com/mindroom-ai/mindroom/blob/main/tests/test_kubernetes_worker_backend.py
[legacy-revision-replay]: https://github.com/mindroom-ai/mindroom/blob/main/src/mindroom/legacy_revision_replay.py
[legacy-compaction-state]: https://github.com/mindroom-ai/mindroom/blob/main/src/mindroom/history/legacy_compaction_state.py
[legacy-streaming]: https://github.com/mindroom-ai/mindroom/blob/main/src/mindroom/legacy_streaming.py
[legacy-approval]: https://github.com/mindroom-ai/mindroom/blob/main/src/mindroom/legacy_approval_payloads.py
[legacy-attachments]: https://github.com/mindroom-ai/mindroom/blob/main/src/mindroom/legacy_attachments.py
[legacy-approval-recovery]: https://github.com/mindroom-ai/mindroom/blob/main/src/mindroom/event_journal/legacy_approval_recovery.py
[legacy-docker-worker-metadata]: https://github.com/mindroom-ai/mindroom/blob/main/src/mindroom/workers/backends/legacy_docker_worker_metadata.py
[legacy-state-root-mounts]: https://github.com/mindroom-ai/mindroom/blob/main/src/mindroom/workers/backends/legacy_state_root_mounts.py
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
[oauth-discovery]: https://github.com/mindroom-ai/mindroom/blob/main/src/mindroom/oauth/discovery.py
[oauth-legacy-credentials]: https://github.com/mindroom-ai/mindroom/blob/main/src/mindroom/oauth/legacy_credentials.py
[oauth-lifecycle]: https://github.com/mindroom-ai/mindroom/blob/main/src/mindroom/oauth/credential_lifecycle.py
[oauth-store]: https://github.com/mindroom-ai/mindroom/blob/main/src/mindroom/oauth/credential_store.py
[oauth-providers]: https://github.com/mindroom-ai/mindroom/blob/main/src/mindroom/oauth/providers.py
[plugin-imports]: https://github.com/mindroom-ai/mindroom/blob/main/src/mindroom/tool_system/plugin_imports.py
[pre-journal-test]: https://github.com/mindroom-ai/mindroom/blob/main/tests/test_upgrade_from_pre_journal_storage.py
[private-legacy-aliases]: https://github.com/mindroom-ai/mindroom/blob/main/src/mindroom/legacy_private_storage_aliases.py
[private-legacy]: https://github.com/mindroom-ai/mindroom/blob/main/src/mindroom/legacy_private_storage.py
[private-paths]: https://github.com/mindroom-ai/mindroom/blob/main/src/mindroom/private_storage_paths.py
[provisioning-service]: https://github.com/mindroom-ai/mindroom/blob/main/scripts/local_mindroom_provisioning_service.py
[python-tools]: https://github.com/mindroom-ai/mindroom/blob/main/src/mindroom/tools/python.py
[replay-store]: https://github.com/mindroom-ai/mindroom/blob/main/src/mindroom/external_triggers/replay_store.py
[legacy-replay-store]: https://github.com/mindroom-ai/mindroom/blob/main/src/mindroom/external_triggers/legacy_replay_store.py
[report-store]: https://github.com/mindroom-ai/mindroom/blob/main/src/mindroom/report_publishing/store.py
[restart-retry]: https://github.com/mindroom-ai/mindroom/blob/main/src/mindroom/sync_restart_retry.py
[saas-migrations]: https://github.com/mindroom-ai/mindroom/tree/main/saas-platform/supabase/migrations
[scheduled-records]: https://github.com/mindroom-ai/mindroom/blob/main/src/mindroom/scheduled_run_records.py
[scheduling]: https://github.com/mindroom-ai/mindroom/blob/main/src/mindroom/scheduling.py
[delegation-state]: https://github.com/mindroom-ai/mindroom/blob/main/src/mindroom/delegation/state.py
[script-legacy-schema]: https://github.com/mindroom-ai/mindroom/blob/main/src/mindroom/script_runs/legacy_schema.py
[session-preflight]: https://github.com/mindroom-ai/mindroom/blob/main/src/mindroom/session_storage_preflight.py
[skills]: https://github.com/mindroom-ai/mindroom/blob/main/src/mindroom/tool_system/skills.py
[sso]: https://github.com/mindroom-ai/mindroom/blob/main/saas-platform/platform-backend/src/backend/routes/sso.py
[instance-lifecycle]: https://github.com/mindroom-ai/mindroom/blob/main/saas-platform/platform-backend/src/backend/services/instance_lifecycle.py
[legacy-instance-lifecycle]: https://github.com/mindroom-ai/mindroom/blob/main/saas-platform/platform-backend/src/backend/services/legacy_instance_lifecycle.py
[tag-vocabulary]: https://github.com/mindroom-ai/mindroom/blob/main/src/mindroom/thread_tag_vocabulary.py
[terraform-state]: https://github.com/mindroom-ai/mindroom/blob/main/cluster/scripts/setup-terraform-state.sh
[thread-export]: https://github.com/mindroom-ai/mindroom/blob/main/src/mindroom/thread_export/storage.py
[thread-models]: https://github.com/mindroom-ai/mindroom/blob/main/src/mindroom/thread_models.py
[thread-tags]: https://github.com/mindroom-ai/mindroom/blob/main/src/mindroom/thread_tags.py
[turn-store]: https://github.com/mindroom-ai/mindroom/blob/main/src/mindroom/turn_store.py
[todo-poke]: https://github.com/mindroom-ai/mindroom/blob/main/src/mindroom/custom_tools/todo_poke.py
[todo-state]: https://github.com/mindroom-ai/mindroom/blob/main/src/mindroom/custom_tools/todo_state.py
[todo-tool]: https://github.com/mindroom-ai/mindroom/blob/main/src/mindroom/custom_tools/todo.py
[tool-metadata]: https://github.com/mindroom-ai/mindroom/blob/main/src/mindroom/tool_system/metadata.py
[tool-legacy-overrides]: https://github.com/mindroom-ai/mindroom/blob/main/src/mindroom/tool_system/legacy_tool_overrides.py
[trigger-store]: https://github.com/mindroom-ai/mindroom/blob/main/src/mindroom/external_triggers/store.py
[usage]: https://github.com/mindroom-ai/mindroom/blob/main/src/mindroom/usage_stats_storage.py
[visible-recovery]: https://github.com/mindroom-ai/mindroom/blob/main/src/mindroom/visible_response_reconciliation.py
[worker-compat]: https://github.com/mindroom-ai/mindroom/blob/main/src/mindroom/workers/compatibility.py
[workflow-api]: https://github.com/mindroom-ai/mindroom/blob/main/src/mindroom/api/dynamic_workflows.py
[execution-preparation]: https://github.com/mindroom-ai/mindroom/blob/main/src/mindroom/execution_preparation.py
[streaming]: https://github.com/mindroom-ai/mindroom/blob/main/src/mindroom/streaming.py
[access-migration-tests]: https://github.com/mindroom-ai/mindroom/blob/main/tests/test_access_migration.py
[agent-config-tests]: https://github.com/mindroom-ai/mindroom/blob/main/tests/test_agents.py
[tool-metadata-tests]: https://github.com/mindroom-ai/mindroom/blob/main/tests/test_tools_metadata.py
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
[legacy-compaction-state-tests]: https://github.com/mindroom-ai/mindroom/blob/main/tests/test_legacy_compaction_state.py
[legacy-state-root-mounts-tests]: https://github.com/mindroom-ai/mindroom/blob/main/tests/test_legacy_state_root_mounts.py
[compaction-redaction-tests]: https://github.com/mindroom-ai/mindroom/blob/main/tests/test_compaction_redaction.py
[matrix-agent-tests]: https://github.com/mindroom-ai/mindroom/blob/main/tests/test_matrix_agent_manager.py
[matrix-identity-tests]: https://github.com/mindroom-ai/mindroom/blob/main/tests/test_matrix_identity.py
[mcp-oauth-tests]: https://github.com/mindroom-ai/mindroom/blob/main/tests/test_mcp_oauth.py
[memory-flush-tests]: https://github.com/mindroom-ai/mindroom/blob/main/tests/test_memory_auto_flush.py
[oauth-api-tests]: https://github.com/mindroom-ai/mindroom/blob/main/tests/api/test_oauth_api.py
[oauth-store-tests]: https://github.com/mindroom-ai/mindroom/blob/main/tests/test_oauth_credential_store.py
[openai-model-tests]: https://github.com/mindroom-ai/mindroom/blob/main/tests/test_openai_models.py
[openai-replay-tests]: https://github.com/mindroom-ai/mindroom/blob/main/tests/test_openai_native_compaction.py
[native-history-tests]: https://github.com/mindroom-ai/mindroom/blob/main/tests/test_native_compaction_history.py
[partial-reply-tests]: https://github.com/mindroom-ai/mindroom/blob/main/tests/test_partial_reply_context.py
[provisioning-service-tests]: https://github.com/mindroom-ai/mindroom/blob/main/tests/test_local_mindroom_provisioning_service.py
[private-storage-tests]: https://github.com/mindroom-ai/mindroom/blob/main/tests/test_private_storage_migration.py
[report-tests]: https://github.com/mindroom-ai/mindroom/blob/main/tests/test_report_publishing.py
[response-runner-tests]: https://github.com/mindroom-ai/mindroom/blob/main/tests/test_response_runner_focused.py
[script-run-tests]: https://github.com/mindroom-ai/mindroom/blob/main/tests/test_script_run_store.py
[session-recovery-tests]: https://github.com/mindroom-ai/mindroom/blob/main/tests/test_agent_session_storage_recovery.py
[sso-cookie-tests]: https://github.com/mindroom-ai/mindroom/blob/main/saas-platform/platform-backend/tests/test_sso_cookie_attrs.py
[instance-lifecycle-tests]: https://github.com/mindroom-ai/mindroom/blob/main/saas-platform/platform-backend/tests/test_instance_lifecycle.py
[streaming-tests]: https://github.com/mindroom-ai/mindroom/blob/main/tests/test_streaming_behavior.py
[sync-continuity-tests]: https://github.com/mindroom-ai/mindroom/blob/main/tests/test_sync_continuity_store.py
[todo-builtin-tests]: https://github.com/mindroom-ai/mindroom/blob/main/tests/test_todo_builtin.py
[todo-poke-tests]: https://github.com/mindroom-ai/mindroom/blob/main/tests/test_todo_poke.py
[trigger-replay-tests]: https://github.com/mindroom-ai/mindroom/blob/main/tests/test_external_trigger_replay_store.py
[turn-store-tests]: https://github.com/mindroom-ai/mindroom/blob/main/tests/test_turn_store.py
[usage-tests]: https://github.com/mindroom-ai/mindroom/blob/main/tests/test_usage_stats_storage.py
[workflow-scheduling-tests]: https://github.com/mindroom-ai/mindroom/blob/main/tests/test_workflow_scheduling.py
[delegation-minimal-tests]: https://github.com/mindroom-ai/mindroom/blob/main/tests/test_delegation_minimal_mode.py

[legacy-response-attempts]: https://github.com/mindroom-ai/mindroom/blob/main/src/mindroom/event_journal/legacy_response_attempts.py
[response-attempt-migration-tests]: https://github.com/mindroom-ai/mindroom/blob/main/tests/test_response_attempts_migration.py
