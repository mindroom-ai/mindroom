# Upgrade Notes

Use this page when upgrading an existing deployment across a release that needs operator action.
Each section names the change, what to do before or after the upgrade, and what does not carry over.
Always back up configuration and persistent storage before replacing the running version, and never run two backend versions against the same storage at once.

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
The first primary startup of a release with this migration moves every verified private scope to its current path before accepting traffic, in both the orchestrator and the standalone primary API.
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
- **Boolean tool settings**: stored tool config values for boolean fields, including credential seeds read from environment variables or files, must be JSON booleans, JSON `null`, or exactly `true` or `false`; any other value makes the tool fail to load with an error naming the field.
- **Restart resume setting**: `defaults.auto_resume_after_restart` is removed and fails validation, so delete it from `config.yaml`; a reply interrupted by a restart always continues in the same message.
- **Delegation records**: a delegation still running during the upgrade finishes normally, but its workspace `run.json`, `events.jsonl`, and `transcript.md` stop updating.

### Compaction Archive

Downgrading to a release without the [compaction archive](https://docs.mindroom.chat/configuration/history/#agent-compaction-settings) is unsupported: older releases neither maintain nor redact the archive, and compaction state they write is never adopted again.

When upgrading from v2026.9.314 or earlier, runs that those releases deleted during compaction are not recoverable.
Summaries compacted by those releases record no per-run provenance, so redacting an event they may contain still clears that conversation's summaries, the runs archived after them, and its live runs.

### Workspace-only worker mounts

From v2026.9.327, dedicated Docker and Kubernetes workers mount only agent workspaces.
Drain worker activity before upgrading, because the upgraded primary stops every older worker before serving, ending its tool calls, shells, background scripts, and CLI sessions.
Until those workers are gone, startup fails and the primary restarts, so the Docker daemon or Kubernetes API must be reachable.
Upgrade worker images together with the primary: Docker refuses worker images from older releases, and Kubernetes workers run the configured worker image, which must come from the same release.
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
