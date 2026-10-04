# Upgrade Notes

## Upgrading to Nio 1.0

Existing deployments upgrade automatically during normal MindRoom startup.
Stop the previous backend, install the new release, and start it with the same configuration and storage root.
No journal archival, database reset, or `mindroom journal adopt` command is required.
As with any storage upgrade, keep a backup of configuration and persistent storage before replacing the running version.
Do not run old and new backend versions against the same storage concurrently.

### What the automatic migration preserves

MindRoom upgrades a pre-Nio-1 event journal in place on both SQLite and PostgreSQL.
The journal generation and installation binding remain unchanged, along with event identities, projected message history, redactions, and handled-turn records.
Configuration, credentials, workspaces, memories, knowledge stores, and separate agent sessions stay in their existing stores.
Matrix accounts, device IDs, encryption keys, and device trust remain intact.

The journal migration retires unfinished old requests, deliveries, approvals, and membership/hydration bookkeeping in the same transaction that creates the new ingestion consumer table.
An interrupted transaction rolls back; the next startup retries the upgrade.
After that transaction commits, subsequent startups preserve new pending work normally.
Recognized older `sync_continuity` records are converted atomically, retaining pending join/decrypt fences while discarding checkpoints now owned by Nio.
Before Nio first adopts an existing crypto store, MindRoom also retires obsolete transport recovery rows under the store's exclusive lease without changing crypto keys.
An already durable Nio store is never reset by this migration.

### What does not continue

Unfinished replies and tool approvals from before the upgrade are abandoned.
An old approval card may remain visible, but it cannot resume the retired continuation.
A tool action that already happened is not undone; check its result before manually retrying it.
Old interrupted responses do not auto-resume because startup recovery requires delivery ownership in the current journal and room membership.

Nio establishes a new room baseline without importing the previous sync checkpoint.
Initial historical messages are context only: they do not trigger replies or commands.
Encrypted events first seen as unreadable history remain context only when their keys arrive, including after a restart.
Messages sent during downtime or before the first room baseline can therefore remain unanswered; resend any request you still want handled after startup completes.
Later live messages become actionable normally, and subsequent restarts preserve pending work and duplicate protection.
Room-member onboarding markers now live in the event journal; the old `tracking/room_member_joins.json` file is ignored.
Initial historical membership baselines prevent later profile updates from onboarding existing members again.

If you customized `MINDROOM_MATRIX_SYNC_CACHE_WRITE_GRACE_SECONDS`, replace it with `MINDROOM_MATRIX_INGESTION_GRACE_SECONDS`; the watchdog now bounds Nio ingestion progress instead of MindRoom's retired callback cache writes.

### Device recovery after the upgrade

Normal restarts reuse the existing Matrix device and durable stream.
If the homeserver reports a soft logout, MindRoom renews credentials for that same device using the configured authentication method.
Pending transport input, application work, encryption keys, and delivery identities remain intact.

Automatic device replacement is unsupported.
If the device store is missing, restore the deployment's matching storage backup before restarting.
Hard logout, a deleted server device, a changed returned identity, corrupt storage, or an unknown storage format still requires operator recovery.
An existing Nio 1.0 session is bound to its journal consumer; clearing the journal or crypto directory is not a supported repair.
Preserve the failed deployment's state when recovering it, because retained input and attempted deliveries may still need reconciliation.

## Private storage migration

Primary startup automatically relocates every verified historical private scope to its current collision-safe requester path before accepting traffic.
Both the orchestrator and standalone primary API run this step before credentials, background work, or worker launch.
Current and empty storage need no migration and do not trigger worker API calls or content scans.

### Before upgrading

1. Pause new requests and scheduled work, then drain active responses, tool calls, shell commands, and background processes.
   Stop every writer through the deployment lifecycle, including the previous primary, independent controllers, supervised processes, managed Docker or Kubernetes workers, and external runners.
   Disable worker creation, restart policies, and external reconciliation throughout migration.
   The migration locks cannot fence older binaries or the control plane.
2. Remove managed Docker worker containers, including stopped containers, while preserving their host state and credential directories.
   For Kubernetes, remove every worker Deployment and ReplicaSet and wait for every worker Pod, including terminating Pods, to disappear.
   The check conservatively covers all resources carrying `mindroom.ai/worker-id` in the configured worker namespace, regardless of custom labels or owning primary.
   Deployments sharing that namespace must coordinate this quiet window.
3. Take coordinated backups of primary storage, optional session storage, and worker state while writers are stopped.
4. Preserve the configured primary and session volume paths and mount the original volumes.
   When `MINDROOM_SESSION_STORAGE_PATH` is configured, make sure that volume is available before starting.
5. Start the new primary with worker creation still disabled externally.
   If migration is needed, startup locks the volumes and performs a bounded, read-only worker absence check before writing migration intents or moving directories.
   It never stops, deletes, retires, or repairs workers.
   Docker checks the live runtime namespace rather than saved worker metadata, so missing or stale metadata cannot hide containers.
   Kubernetes checks worker Deployments, ReplicaSets, and Pods; even scaled-down controllers block migration.
   API failure, missing permissions, invalid inventory, or remaining workers blocks startup.
   This point-in-time check requires deployment enforcement to keep writers absent throughout migration.
   It validates every affected scope and session mirror before moving any directory.
6. Resume admission, scheduling, and worker creation only after startup migration succeeds.

Kubernetes primary service accounts need `list` access to `deployments` and `replicasets` in the `apps` API group and `pods` in the core API group within the configured worker namespace.
The runtime chart's worker-manager Role includes these reads; apply the updated Role before starting the upgraded primary.
Externally managed RBAC must supply the same permissions; verification failure never becomes success.

Static external sandbox runners must be stopped separately through their deployment lifecycle.
For that migration startup, remove `MINDROOM_SANDBOX_PROXY_URL` from the primary configuration after stopping the external runner; a configured external runner blocks automatic migration because startup cannot verify its shutdown.
Restore the runner configuration and restart it only after migration finishes.

### What changes

Startup uses the exact saved requester owner record to verify each historical key and directory name.
It renames the matching session directory first, then the primary scope, and finally updates the primary owner record.
Verified historical primary paths remain as relative symlinks to their canonical siblings, and existing separate session mirrors receive matching aliases.
These links preserve unchanged absolute workspace paths and executable shebangs.
Alias access requires the exact current owner, historical path spelling, and canonical sibling target.
Retain the links when backing up and restoring the volumes; they are compatibility state, and a current owner record alone does not recreate a missing historical alias.
Database files, WAL companions, credentials, workspaces, and histories retain their contents.
Worker credential directories remain separate and are not relocated.

Older versions can leave populated private scopes without an owner record.
Startup warns and leaves these scopes and their matching session mirrors in place, without moving data, assigning an owner, or granting historical alias access.
Directory names and session metadata are not used to infer ownership.
New requester scopes remain separate; the retained unclaimed history is not automatically attached to them.
Recover that history only after independently verifying its owner, with all writers stopped.

Each pending scope temporarily contains `.mindroom-private-storage-migration.json` with its exact owner, volume paths, and original directory inodes.
The intent moves with the primary scope and is removed after both locations and the current owner record are durable.
It contains private owner information and belongs on the protected storage volume.

### Interrupted or rejected startup

Restart the primary with the same mounted data and configured paths to resume an interrupted migration automatically.
Remounts that preserve the directory inodes are supported.
Do not remove or copy pending intent records, create destination directories, or start other writers during recovery.
Abrupt process death can leave partial temporary files from durable intent or owner writes.
These remain protected, untouched files and never authorize recovery; only the exact final intent and owner records do.

Startup rejects malformed or ambiguous owner records, conflicting destinations, missing recorded session mirrors, session-only data without a primary scope, unrelated recovery records, unsafe scope or record links, nested mounts, and links that would break after relocation.
Inspect and correct the reported conflict with all writers stopped, then restart.
There is no migration CLI or automatic rollback.
To return to an older deployment, stop all writers and restore the coordinated backups together before starting it.

### Future removal

Migration support can be removed only after defining an explicit supported upgrade floor that requires an intermediate release containing this migration.
A fixed number of releases is insufficient because installations can skip releases or restore older backups.
A later release must continue rejecting unsupported historical layouts and direct operators to the supported intermediate upgrade.

## Membership Access Migration

See [Access Control](https://docs.mindroom.chat/authorization/#configuration) for the membership access schema.

Loading a monolithic configuration with retired access fields automatically converts it to this schema.
MindRoom validates the converted configuration before replacing `config.yaml` atomically and saves the exact original bytes once as `config.yaml.pre-membership-access`.
When `config.yaml` is a single-file Docker bind mount that cannot be replaced atomically, migration stops and directs the operator to run `mindroom config migrate --path <host-config.yaml>` on the host.
The migration preserves explicit new-schema values and removes the retired fields.
The normalized YAML does not preserve comments or hand formatting; the exact backup preserves both for recovery.
Before retrying a rejected migration, replace non-concrete identity grants with concrete Matrix user IDs and unresolved room IDs or aliases with managed room keys.
Also remove `authorization.agent_reply_permissions` entries whose agent or team is no longer configured.

Access migration does not support configurations that use `!include`.
If retired access fields and any `!include` are present together, loading fails without changing the root file, changing included files, or creating a backup.
Remove the includes or migrate the combined configuration manually before retrying.

## Upgrade and reset limits

A journal replacement must coordinate its generation binding with the next Nio baseline.
Agno sessions may still contain current handled-turn recovery facts and historical run blobs, while Matrix keeps visible messages and state independently of local storage.
Private storage moves require stopped primaries and absent managed workers, as described in [Private Storage Migration](#private-storage-migration).
Usage discovery ignores verified historical primary and session aliases because their canonical directories are scanned separately; unverified symlinks still report incomplete coverage.
The Nio cutoff abandons pre-durable pending transport work while preserving crypto material, as described in [Nio 1.0 Upgrade](#upgrading-to-nio-10).
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

Delegation records now keep their working state below `tracking/` and write the workspace `run.json`, `events.jsonl`, and `transcript.md` only as exports.
A delegation still in flight at the upgrade, such as one awaiting approval, settles normally, but its workspace record keeps what it held before the upgrade and is not updated further.

### Compaction Archive

See [History & Compaction](https://docs.mindroom.chat/configuration/history/#agent-compaction-settings) for the compaction archive.

- Downgrading to a release without the archive is unsupported: older releases neither maintain nor redact the archive, and compaction state they write is never adopted again.

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

### Requester-scoped worker keys

v2026.9.33 changed the key of every `user` and `user_agent` worker, and workers or scoped integrations from earlier releases are not reused or migrated.
After upgrading from an earlier release, reprovision every existing `user` and `user_agent` worker and reconnect every integration connected for those scopes.
Shared and unscoped workers are unaffected.
