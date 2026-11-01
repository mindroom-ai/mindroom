# Upgrade Notes

Use this page when upgrading an existing deployment across a release that needs operator action.
Each section names the change, what to do before or after the upgrade, and what does not carry over.
Always back up configuration and persistent storage before replacing the running version, and never run two backend versions against the same storage at once.

## Upgrading from Agent Vault

MindRoom no longer supports Agent Vault; [Agent Keys](https://docs.mindroom.chat/deployment/egress-broker/) injects credentials into worker egress instead, and secrets stored in Agent Vault do not carry over.
Before upgrading, remove `agent_vault_access` from agents' `tools:` lists and `defaults.tools`, because a config that still lists it fails to load, and give agents that need brokered credentials the [`egress_credentials` tool](https://docs.mindroom.chat/deployment/egress-broker/#agent-tool).
In runtime chart values, enable the [egress broker](https://github.com/mindroom-ai/mindroom/blob/main/cluster/k8s/runtime/README.md#egress-broker) and delete `workers.kubernetes.agentVault`, `approvedEgress.parentProxy.enabled`, `approvedEgress.parentProxy.host`, and `approvedEgress.parentProxy.port`, which the chart now ignores; `approvedEgress.parentProxy.bypassDomains` still applies to the broker chain.
A `helm upgrade` of a release that ran the chart-managed Agent Vault server deletes that server, its Jobs, and its data claim, so first back up the volume and its bootstrap Secret if you still need the stored secrets.
Helm leaves behind the CA ConfigMap (`workers.kubernetes.agentVault.workerCaConfigMapName`) and the admin token Secret (default `agent-vault-access-admin`) that the bootstrap Job created, so delete them yourself, and remove the archived `agent-vault-bridge` plugin from `plugins:` if you use it.

## Upgrading to shared team memory

Earlier releases kept a copy of each team's memory in every member's storage directory; the first start after this upgrade merges those copies into team memory under `team_memory/` before any agent comes online, and removes each copy it merged from its member; the exceptions below stay where they are.
Each member had extracted every team turn on its own, so each turn keeps the member's extraction that holds the most facts, as a team turn now writes one; a turn only one member stored stays, and file team memory keeps every `MEMORY.md` entry once.
Mem0 copies merge from the agents the config lists with Mem0 memory, the only stores they were read from; to keep the team memories of an agent you switched to `file` or `none` for now, switch it back to `mem0` before upgrading.
A member's team file copy holding anything MindRoom did not write, such as a hand-written line or another file, stays where it is, no longer read, and startup logs a warning starting with `Left` with its path; to keep a note from it, add it to the team's `MEMORY.md` as a `- [id=<unique id>] <note>` line, since team memory finds only such entries.
A member's Mem0 store holding a link or special file, or one Chroma cannot read, is not merged either, and its warning also starts with `Left`; its team memories stay there, no longer read.
Only Mem0 team memories of the configured embedder merge; those an earlier embedder wrote, which the runtime no longer read, stay with a `Left` warning, as does a memory whose embedding size the configured embedder never gives.
A storage error while writing team memory stops startup before any member's Mem0 copy is removed, and the next start merges the rest.
The first start installs Chroma for this when any agent the config lists has a Mem0 store; with `MINDROOM_NO_AUTO_INSTALL_TOOLS=1` and no `chromadb` extra, the Mem0 copies stay until the first start with Chroma.
With any embedder other than OpenAI's own API, such as a local or Ollama embedder or an OpenAI-compatible server set as `host` or through `OPENAI_BASE_URL`, the first start asks the embedder once for the size of its embeddings, and the Mem0 copies wait for a start whose embedder answers; after fixing the embedder, restart MindRoom to merge them.
An older release does not read `team_memory/`; after going back and upgrading again, delete `tracking/team_memory_merged.json` before the start so the copies that release wrote merge too.
That start also checks again every copy a `Left` warning named, so first remove any of them whose memories you do not want back.

## Upgrading to progress kept in conversation order

This upgrade needs no action: conversation databases convert at the first start without a backup, and memory auto-flush, knowledge-graph ingestion, and the `exploration` automation convert their progress when they next read each conversation.

### Going back from progress kept in conversation order

The release before this one reads the converted databases, but its memory auto-flush and knowledge-graph ingestion then fail on every pass for each agent or graph whose progress converted, logging `Memory auto-flush failed for an agent` or `Knowledge graph ingestion failed for an agent`.
To go back, stop MindRoom, then restore `tracking/memory_auto_flush.json` and each `knowledge_graph/ingestion/<graph>.json` under the storage root from a backup taken before the upgrade, or delete them.
Without those files, auto-flush starts again with the turns after the next start, and ingestion reads the conversations active within `knowledge_graph.ingest.backfill_days` again, which pays for that extraction again.
That release's usage reports also leave out the usage recorded while this release ran.

## Upgrading to one owner per conversation database

The first start after this upgrade converts every conversation database once more before any agent comes online, as the [conversation storage v2](#upgrading-to-conversation-storage-v2) upgrade did, so that each records the agent, team, or internal usage store it belongs to.
A team's database that holds no conversation yet converts when that team next uses it instead.
Each converted database logs one `conversation_converted` record (see [Events](https://docs.mindroom.chat/deployment/operational-log-events/#conversation_converted)).
Conversation history, compaction summaries, session state, pending tool approvals, and usage records carry over.

### Before upgrading to one owner per database

Make sure each disk that holds conversation databases has free space of about twice the total size of their `.db` files.
A database still in the Agno layout, which the [conversation storage v2](#upgrading-to-conversation-storage-v2) upgrade never converted, converts straight to this layout and needs the free space that section gives instead.
A database whose directory lacks the space it needs is not converted: startup logs `conversation_conversion_skipped_store` for it, and every use of it fails with an error naming the database and the bytes it needs until you free the space.
A deploy that rolls back on its own, such as `helm upgrade --atomic`, starts the older release on converted databases, which then moves them aside (see the next section).

### Going back to an older release

Before converting a database, MindRoom copies it byte for byte beside it and never deletes that copy: to `<name>.db.pre-storage-v4` for a database the conversation storage v2 upgrade already converted, and to `<name>.db.pre-storage-v2` for one still in the Agno layout.
A crash or restart during the upgrade leaves the database being converted unchanged, and the next start converts it.
The only way back is to stop MindRoom, replace each `<name>.db` with the copy this upgrade made of it, delete any `<name>.db-journal` beside it, and start the older release.
When a database has both backups, the copy this upgrade made is the newer file.
Everything written since the upgrade is lost: replies, summaries, session state, approvals requested since then, and usage records for that period.
An older release started on converted databases without restoring the backups archives each agent's or team's `sessions/` directory, backups included, as [Incompatible session databases](https://docs.mindroom.chat/deployment/storage/#incompatible-session-databases) describes; move the backups back out of that archive and restore them as above.
Going back below conversation storage v2 from a database that has both restores its `.pre-storage-v2` backup instead, as [Downgrading](#downgrading) describes.
Once you are sure you will not downgrade, delete these backups.

## Upgrading to checked model references and per-agent workspaces

A config whose agent, team, router, room, thread-summary, or enabled voice model names no model under `models` now fails to load instead of failing at the first reply, so run `mindroom config validate` before upgrading and add or rename the missing models; a config with agents and no `router.model` needs a model named `default`.
Every agent now has its own workspace, and a shared agent whose `worker_scope` is `user` or `user_agent` works in it instead of each worker's own scratch directory, so files those workers kept in their scratch directories are no longer the agent's working directory.
Make such an agent [`private`](https://docs.mindroom.chat/configuration/agents/#private-instances) to keep each requester's files apart.

## Upgrading from `MCPTools` plugin toolkits

MindRoom no longer runs plugin toolkits that subclass Agno `MCPTools` (see [MCP servers](https://docs.mindroom.chat/plugins/#mcp-servers)).
Before upgrading, configure each such server under `mcp_servers:` in `config.yaml` as [MCP](https://docs.mindroom.chat/mcp/) describes, and replace the plugin tool's name with `mcp_<server_id>` in agents' `tools:` lists and in `defaults.tools`.
The [community plugins](https://docs.mindroom.chat/plugins/#community-plugins) are not affected.

## Upgrading to conversation storage v2

The first start after this upgrade converts the conversation database of every agent and team, private instances included, and the internal usage database under `system/`, to a new layout before any agent comes online.
Each converted database logs one `conversation_converted` record (see [Events](https://docs.mindroom.chat/deployment/operational-log-events/#conversation_converted)).
The process stays live but becomes ready only after the conversion and the rest of startup finish; [Health & Readiness](https://docs.mindroom.chat/deployment/operational-log-events/#health-readiness) says what each endpoint answers meanwhile.
The conversion is one-way, and downgrading afterwards is not supported.

### Before upgrading

Check that every startup probe asks `/api/health`, as the bundled Helm charts do and [Health & Readiness](https://docs.mindroom.chat/deployment/operational-log-events/#health-readiness) requires.
A deploy that rolls back when readiness times out, such as `helm upgrade --atomic` with its default 5-minute timeout, can roll back mid-conversion and start the older release on converted databases (see [Downgrading](#downgrading)), so raise the timeout or drop `--atomic` for this upgrade.
Make sure each disk that holds conversation databases, under the storage directory or `MINDROOM_SESSION_STORAGE_PATH`, has free space of about two and a half times the total size of their `.db` files.
A database that no release since v2026.10.341 has opened can still hold Agno 2 run blobs or the compaction archive, which the conversion copies first, so it needs up to about three and a half times its size.
A database whose directory lacks the space it needs is not converted: startup logs `conversation_conversion_skipped_store` for it, and every use of it fails with an error naming the database and the bytes it needs until you free the space, after which its next use converts it.

### What carries over

Conversation history, compaction summaries, pending tool approvals, and usage records carry over.
Before converting a database, MindRoom copies it byte for byte to `<name>.db.pre-storage-v2` beside it and never deletes that copy.
A stored run whose data cannot be read is left out of the converted database, logged as `conversation_run_left_out` with its run ID, and kept in the backup.
A crash or restart during the upgrade leaves the database being converted unchanged, and the next start converts it.
Right after converting a database, MindRoom compacts it to return the space its old tables used, when its directory has free space of about twice the converted data.
Without that space the file keeps it until later writes reuse it; with MindRoom stopped, `sqlite3 <name>.db VACUUM` returns it.

### Downgrading

The only way back is to stop MindRoom, replace each `<name>.db` with its `<name>.db.pre-storage-v2` backup, delete any `<name>.db-journal` beside it, and start the older release.
Everything written since the upgrade is lost: replies, summaries, session state, approvals requested since then, and usage records for that period.
The older release drops messages redacted since the upgrade from the restored history before it next replies in that conversation, but edits made since the upgrade are not applied again, so the earlier reply to an edited message can return to the agent's history.
An older release started on converted databases without restoring the backups archives each agent's or team's `sessions/` directory, backups included, as [Incompatible session databases](https://docs.mindroom.chat/deployment/storage/#incompatible-session-databases) describes; move the backups back out of that archive and restore them as above.
Upgrading again converts each restored database again and writes a new backup.

Once you are sure you will not downgrade, delete the `.pre-storage-v2` backups.

## Upgrading to OpenAI SDK 3

OpenAI-compatible and Claude model requests, and the other OpenAI SDK requests [Model Configuration](https://docs.mindroom.chat/configuration/models/#configuration-examples) lists, now verify certificates against the operating system's trust store instead of certifi's bundle.
The published images include system CA certificates.
On a host without them, install them (for example the `ca-certificates` package) before upgrading, or point `SSL_CERT_FILE` at a complete CA bundle, such as certifi's `cacert.pem` with any private CA appended.

## Upgrading to durable reply records

Upgrade while no agent is replying.
Stop the previous backend once in-progress replies have finished, and decide pending tool approvals first: the upgrade cancels every approval of a paused reply still waiting or not yet resumed, while approvals of scheduled calls are kept.
A cancelled approval's card may stay visible, but clicking it does nothing, its reply keeps what it showed, and the request is not answered, so resend it if needed.
A reply that was still streaming when the previous backend stopped may be answered again after the upgrade, sometimes in a new message, and a tool it had already run can run again, so check the room before resending that request.

## Upgrading to Nio 1.0

Existing deployments upgrade automatically during normal startup.
Stop the previous backend, install the new release, and start it with the same configuration and storage root.
No journal archival, database reset, or `mindroom journal adopt` command is required.
If you customized `MINDROOM_MATRIX_SYNC_CACHE_WRITE_GRACE_SECONDS`, replace it with `MINDROOM_MATRIX_INGESTION_GRACE_SECONDS`.

### What the automatic migration preserves

The event journal is upgraded in place on both SQLite and PostgreSQL, keeping message history, redactions, and handled-turn records, so restarts do not produce duplicate replies.
Configuration, credentials, workspaces, memories, knowledge stores, and agent sessions are untouched.
Matrix accounts, device IDs, encryption keys, and device trust remain intact.

### What does not continue

Replies and tool approvals left unfinished before the upgrade are abandoned and do not resume.
An old approval card may stay visible but cannot be approved.
A tool action that already ran is not undone, so check its result before retrying it manually.

On first startup each room's existing history is loaded as context only and does not trigger replies or commands, including encrypted messages whose keys arrive later.
Messages sent during the downtime can therefore stay unanswered; resend any request you still want handled once startup completes.
Later messages are handled normally.
The old `tracking/room_member_joins.json` file is no longer read, and existing room members are not onboarded again.

### Device recovery after the upgrade

Normal restarts reuse the existing Matrix device.
If the homeserver reports a soft logout, MindRoom renews credentials for the same device with the configured authentication method, without losing pending work or encryption keys.

MindRoom never replaces a device automatically.
If the device store is missing, restore the deployment's matching storage backup before restarting.
A hard logout, a deleted server device, a changed returned identity, corrupt storage, or an unknown storage format requires operator recovery.
Clearing the journal or the crypto directory is not a supported repair, because a Nio 1.0 device session is bound to its journal.
Preserve the failed deployment's state while recovering it, because pending input and attempted deliveries may still need reconciliation.

## Private storage migration

Private storage created by v2026.9.32 or earlier uses older requester directory names.
The first primary startup of a release with this migration moves every verified private scope to its current path before accepting traffic.
Current and empty storage need no migration and trigger no worker checks.

### Before upgrading

1. Pause new requests and scheduled work, then drain active responses, tool calls, shell commands, and background processes.
   Stop every writer through the deployment lifecycle, including the previous primary, independent controllers, supervised processes, managed Docker or Kubernetes workers, and external runners.
   Disable worker creation, restart policies, and external reconciliation until the migration finishes, because MindRoom cannot fence older binaries or the control plane.
2. Remove managed Docker worker containers, including stopped ones, while keeping their host state and credential directories.
   For Kubernetes, delete every worker Deployment and ReplicaSet and wait until every worker Pod, including terminating Pods, is gone; scaled-down controllers still block migration.
   The check covers every resource labeled `mindroom.ai/worker-id` in the configured worker namespace, whatever primary owns it, so deployments sharing that namespace must coordinate this quiet window.
3. Back up primary storage, session storage, and worker state together while writers are stopped.
4. Mount the original volumes at the same configured primary and session paths.
   When `MINDROOM_SESSION_STORAGE_PATH` is set, make sure that volume is available before starting.
5. Start the new primary with worker creation still disabled externally.
   When a move is needed, startup checks that no workers exist before changing anything; it never stops or deletes workers itself.
   A worker API failure, missing permissions, or remaining workers stops startup.
6. Resume admission, scheduling, and worker creation only after startup succeeds.

Kubernetes primary service accounts need `list` on `deployments` and `replicasets` in the `apps` API group and on `pods` in the core API group, within the configured worker namespace.
The runtime chart's worker-manager Role includes these permissions, so apply the updated Role before starting the upgraded primary; externally managed RBAC must grant the same.

Static external sandbox runners must be stopped separately.
For the migration startup, remove `MINDROOM_SANDBOX_PROXY_URL` from the primary configuration after stopping the runner, because a configured external runner blocks the migration.
Restore the setting and restart the runner after the migration finishes.

### What changes

Files, databases, credentials, workspaces, and histories keep their contents.
Each old directory name stays behind as a relative symlink to its new directory, so absolute workspace paths and script shebangs keep working.
Keep these links when backing up and restoring the volumes; they are not recreated if lost.
Worker credential directories are not moved.

Private scopes without an owner record, which older releases could create, are left in place with the warning `Preserving private scope without an owner record; not migrating`.
They are not attached to the requester's new private scope.
Recover that history manually only after independently verifying its owner, with all writers stopped.

While a move is pending, its scope contains `.mindroom-private-storage-migration.json`, which holds private owner information; never delete or copy it.

### Interrupted or rejected startup

Restart the primary with the same mounted data and configured paths to resume an interrupted migration; remounts that keep the same directories are fine.
Do not create destination directories or start other writers during recovery.

An entry under `private_instances` whose ownership cannot be verified is left untouched with the warning `Leaving an invalid private storage entry untouched; not migrating`, and startup continues.
Startup stops with an error starting with `Private storage migration:` for conflicts it cannot leave aside, such as a missing or overlapping volume, two scopes claiming the same owner, session-only data without a primary scope, or nested mounts.
Correct the reported conflict with all writers stopped, then restart.

Once every verified move finishes, startup writes `tracking/private_storage_migrated.json` and later starts skip the migration.
To migrate an entry that was left untouched, fix it with all writers stopped, delete that file, and restart through the quiet window above.

There is no migration CLI or automatic rollback.
To return to an older release, stop all writers and restore the coordinated backups together before starting it.

## Membership Access Migration

See [Access Control](https://docs.mindroom.chat/authorization/#configuration) for the membership access schema.

Loading a single-file configuration that still uses retired access fields converts it automatically.
MindRoom validates the result, replaces `config.yaml`, removes the retired fields, keeps explicit new-schema values, and saves the original once as `config.yaml.pre-membership-access`.
The rewritten YAML loses comments and hand formatting; the backup keeps both.
When `config.yaml` is a single-file Docker bind mount that cannot be replaced, loading stops and asks you to run `mindroom config migrate --path <host-config.yaml>` on the host.

If the migration is rejected, replace non-concrete identity grants with concrete Matrix user IDs, replace unresolved room IDs or aliases with managed room keys, and remove `authorization.agent_reply_permissions` entries for agents or teams that no longer exist, then retry.

Configurations that use `!include` are not migrated.
When retired access fields and `!include` appear together, loading fails with `Automatic access migration does not support !include configurations` and changes no files.
Remove the includes or migrate the combined configuration by hand before retrying.

## Upgrade and reset limits

Smaller changes that need operator action:

- **Host browser files**: primary-process browser profiles live in each agent's state root, and `<storage>/browser-profiles` is not read; sign in again, then delete that directory.
  Default host browser screenshots, PDFs, and downloads live in `browser/` in each agent's state root, and `upload` does not accept files left in `<storage>/browser`; move any you still need into the agent workspace, then delete it.
- **Browser egress proxy**: Chromium in the host, headless worker, and Computer browsers and in `crawl4ai` tunnels every connection, plain HTTP included, through the configured egress proxy with HTTP `CONNECT`.
  The proxy must allow `CONNECT` to ports 80 and 443, and for the primary also to IP addresses; the chart-managed approved egress proxy needs the [minimum proxy image version](https://docs.mindroom.chat/deployment/approved-egress/#enable-with-the-runtime-chart).
  In a sandbox runner, set every proxy variable, or only `all_proxy`, to the same HTTP(S) proxy, or the browser does not start.
  See [Egress Proxy for Browsers](https://docs.mindroom.chat/tools/web-scraping-and-browser/#egress-proxy-for-browsers) for the full rules.
- **Mem0 history file**: Mem0 memory keeps its history in `mem0/` in each agent's state root, and stored memories carry over; MindRoom does not read the `history.db` in Mem0's own directory (`~/.mem0/`, or `MEM0_DIR` when set), which can hold the text of deleted memories, so delete that file unless an agent uses the [`mem0` tool](https://docs.mindroom.chat/tools/memory-and-storage/#mem0) without an API key, which keeps its history there.
  `mem0` is now a reserved name, so an agent whose `private.root` starts with `mem0` fails config load until you rename it.
- **Boolean tool settings**: stored tool config values for boolean fields, including credential seeds read from environment variables or files, must be JSON booleans, JSON `null`, or exactly `true` or `false`; any other value makes the tool fail to load with an error naming the field.
- **Retired Agno settings and unsupported model keys**: when you upgrade, delete `learning` and `learning_mode`, `compress_tool_results`, and the `website` tool's `knowledge` option from `config.yaml`, since each fails config load.
  Configs created by older `mindroom config init` runs set `learning: false` on the `mind` agent.
  The `learning/<agent>.db` databases that Agno Learning left under agent state roots are unused, and you can delete them.
  Also delete every `extra_kwargs` key a model's provider does not accept, such as the retired Agno setting `retries` or a misspelled key, on every model, including models no agent uses; [Extra Kwargs](https://docs.mindroom.chat/configuration/models/#extra-kwargs) lists the accepted keys.
- **Tool settings that took Python objects**: delete these settings, which no config value could supply, from every tool entry in `config.yaml`, since each fails config load: `db_engine` and `tables` of `sql`, `connection` of `postgres`, `connection` and `config` of `duckdb`, `duckdb_connection` and `duckdb_kwargs` of `csv`, `obb` of `openbb`, `safe_globals` and `safe_locals` of `python`, `reddit_instance` of `reddit`, `knowledge` of `wikipedia`, `sandbox_options` of `e2b`, and `workspace_config`, `connected_account_ids`, `metadata`, `processors`, and `logging_level` of `composio`.
  Values saved for them in the dashboard are ignored.
- **Unknown config keys**: every section of `config.yaml` except free-form maps, such as a plugin's `settings` and `memory.llm.config`, rejects keys MindRoom does not define, so a misspelled or leftover key, such as one under `memory`, `models`, `knowledge_bases`, `voice`, `router`, `plugins`, `authorization`, or an agent's `private` or `compaction`, fails config load with `Unknown key` even when nothing uses it; before upgrading, run the new release's `mindroom config validate` and delete or correct each key it names.
- **Sandbox runner release**: a `static_runner` sandbox runner from a release with another worker protocol, such as a Compose `sandbox-runner` service that was not pulled again, is refused and the calls routed to it fail, so upgrade it together with the primary (see [Keeping worker versions matched](https://docs.mindroom.chat/deployment/sandbox-proxy/#keeping-worker-versions-matched)).
- **Model providers**: a model whose `provider` is not one of the [supported providers](https://docs.mindroom.chat/configuration/models/#supported-providers) fails config load, even when no agent uses it, so `mindroom run` and the dashboard do not start until you fix or delete that model in `config.yaml`; this includes models saved earlier with Together, Mistral, Perplexity, Cohere, or xAI.
- **Model names**: a model key under `models` may contain only letters, digits, `:`, `_`, and `-`, so a key such as `gpt-6.1` fails config load even when no agent uses it, and `mindroom run` and the dashboard do not start until you rename it, as [Model Config Fields](https://docs.mindroom.chat/configuration/models/#model-config-fields) describes, and update every reference to it.
- **Restart resume setting**: `defaults.auto_resume_after_restart` is removed and fails validation, so delete it from `config.yaml`; a reply interrupted by a restart always continues in the same message.
- **Delegation records**: a delegation still running during the upgrade finishes normally, but its workspace `run.json`, `events.jsonl`, and `transcript.md` stop updating.
- **Pending tool approvals after going back**: once this release has opened a conversation, an earlier release cannot resume a reply in it that waits for a tool approval, and approving it there fails; answer pending approvals before going back, or ask again afterwards.
- **Knowledge index state**: the first start converts each semantic knowledge base's index in place, and older releases cannot read the converted index, so going back to one rebuilds every semantic base from scratch, embedding every file again, and its search is unavailable until that rebuild finishes.
- **MCP gateway and shared agents**: the [MCP gateway](https://docs.mindroom.chat/deployment/mcp-gateway/#agent-eligibility) no longer offers agents whose worker scope is `shared` or unset, so their users lose gateway access to them and saved selections of them stop working.
  To restore an agent, set `mcp_gateway_shared_credentials: true` on it or give it `worker_scope: user` or `user_agent`.
- **Retired prompt overrides**: a [`prompts`](https://docs.mindroom.chat/configuration/#built-in-prompt-overrides) entry for `DEFAULT_UNSEEN_MESSAGES_HEADER`, `INTERRUPTED_PARTIAL_REPLY_HEADER`, `MIXED_PARTIAL_REPLY_HEADER`, or `PREVIOUS_CONVERSATION_THREAD_HEADER` fails validation as an unknown prompt, so delete it from `config.yaml`; no prompt rendered these headers, so removing them changes no text a model reads.
- **Dedicated workers**: the upgrade replaces every Docker and Kubernetes worker once on its next use, which ends background shell commands running in it.
  Kubernetes background scripts still running during the upgrade are interrupted when MindRoom starts again instead of resuming; start them again.
- **Credentials from the environment**: the first start, or `mindroom doctor`, deletes the credential files earlier releases copied from provider-key variables, `OLLAMA_HOST`, `GITHUB_TOKEN`, `EMBEDDER_API_KEY`, `GOOGLE_APPLICATION_CREDENTIALS`, and credential seeds; MindRoom now reads those values from the environment at each start (see [Credentials from the Environment](https://docs.mindroom.chat/oauth-framework/#credentials-from-the-environment)).
  Before upgrading, set again any such variable you removed after MindRoom imported it, or save that credential in the dashboard, or the key stops working; startup logs each removed service whose variable is no longer set.
- **Docker worker config**: Docker workers get the config MindRoom has applied, and `MINDROOM_DOCKER_WORKER_HOST_CONFIG_PATH` only says where that config's directory is on the Docker host; a separate file at that path is no longer read, so move any worker-only settings into `config.yaml`.
- **Public address setting**: `MINDROOM_BASE_URL` is removed and `mindroom run` refuses to start while it is set, so before upgrading, move its value to `MINDROOM_PUBLIC_URL` unless that is already set, and delete it.
- **Room references**: `rooms`, `room_thread_modes`, an automation's `room`, the keys of `room_models`, and the keys of `room_thread_summary_models` other than agent and team names must name a room MindRoom can find, so a `#` alias of a room MindRoom did not create, or a room key no agent, team, or top-level `rooms` lists, now fails config load and `mindroom run` refuses to start.
  Run the new release's `mindroom config validate` and replace each reference it names with a room key or the room's `!` room ID, as [Rooms](https://docs.mindroom.chat/rooms/#configuration) describes.

### Compaction History

Downgrading below the release that keeps [compacted runs](https://docs.mindroom.chat/configuration/history/#agent-compaction-settings) in the conversation database beside their summaries is unsupported: older releases replay every compacted run again, and compaction state they write is never adopted again.

When upgrading from v2026.9.314 or earlier, runs that those releases deleted during compaction are not recoverable.
Summaries compacted by those releases record no per-run provenance, so redacting an event they may contain still clears that conversation's summaries and all of its runs.

### Workspace-only worker mounts

From v2026.9.327, dedicated Docker and Kubernetes workers mount only agent workspaces.
Drain worker activity before upgrading, because the upgraded primary stops every older worker before serving, ending its tool calls, shells, background scripts, and CLI sessions.
Until those workers are gone, startup fails and the primary restarts, so the Docker daemon or Kubernetes API must be reachable.
Upgrade worker images together with the primary, because Docker and Kubernetes refuse worker images from older releases.
Rolling back to v2026.9.326 is safe when the Docker and Kubernetes worker images roll back with the primary; upgrading again stops those workers at startup.

Before upgrading, check two limits:

- `private.root` cannot start with a name the primary writes beside the private workspace, listed under [Private Fields](https://docs.mindroom.chat/configuration/agents/#private-fields); such a configuration fails validation, so rename the root first.
- Knowledge files above 64 MiB are left out of every knowledge base, including operator-managed ones, and the next refresh removes their existing vectors; find them with `find <knowledge folder> -type f -size +64M`.

Older workers could write agent state roots, so after the upgrade check those roots for links they may have planted, running each command once with GNU find:

```bash
find "$STORAGE/agents" -mindepth 2 -type l -print -o -type d -regex "$STORAGE/agents/[^/]*/workspace" -prune
find "$STORAGE/private_instances" -mindepth 2 -type l -print -o -type d -regex "$STORAGE/private_instances/[^/]*/\([^/]*\)/\1_data" -prune
find "$STORAGE/agents" "$STORAGE/private_instances" -type f -links +1 -print
```

The first two commands skip links inside workspaces, which belong to the workers, and the verified old-name aliases directly below `private_instances`.
Remove every link they print, including a workspace that is itself a link, and inspect hard-linked files for data copied out of another instance.
The second command assumes the default `private.root` of `<agent>_data`.
When some agents set another root, pair every private agent with its root, default ones included, in one alternation such as `-regex "$STORAGE/private_instances/[^/]*/\(notes/notes_data\|mail/inbox\)"`; separate runs per root would print other agents' workspace links, and roots listed without their agents would skip same-named directories under other agents.

### Config-free runners and workers

The `static_runner` sidecar and dedicated Kubernetes workers do not mount the primary's config; they receive the agent settings they need with each request.
The runtime chart's `workers.kubernetes.configMapName`, `configKey`, and `configPath` values and the matching `MINDROOM_KUBERNETES_WORKER_CONFIG_*` settings are removed.
Background scripts still running on Kubernetes workers during the upgrade are interrupted once when the primary starts.
Kubernetes workers pick up the new pod template when they are next recreated.
Plugin directories beside a file-sourced config are not visible to the sidecar or Kubernetes workers, so install proxied plugins as Python packages in the runner image.

### Requester-scoped worker keys

v2026.9.33 changed the key of every `user` and `user_agent` worker, and workers from earlier releases are not reused or migrated.
After upgrading from an earlier release, reprovision every existing `user` and `user_agent` worker.
OAuth connections made for those scopes by ordinary Matrix IDs such as `@alice:example.org` keep working; reconnect any other integration connected for those scopes.
Shared and unscoped workers are unaffected.
