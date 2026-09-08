# Private storage migration

Primary startup automatically relocates every verified historical private scope to its current collision-safe requester path before accepting traffic.
Both the orchestrator and standalone primary API run this step before credentials, background work, or worker launch.
Current and empty storage need no migration and do not trigger worker API calls or content scans.

## Before upgrading

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
The bundled worker-manager chart roles include these reads; apply the updated roles before starting the upgraded primary.
Externally managed RBAC must supply the same permissions; verification failure never becomes success.

Static external sandbox runners must be stopped separately through their deployment lifecycle.
For that migration startup, remove `MINDROOM_SANDBOX_PROXY_URL` from the primary configuration after stopping the external runner; a configured external runner blocks automatic migration because startup cannot verify its shutdown.
Restore the runner configuration and restart it only after migration finishes.

## What changes

Startup uses the exact saved requester owner record to verify each historical key and directory name.
It renames the matching session directory first, then the primary scope, and finally updates the primary owner record.
Database files, WAL companions, credentials, workspaces, and histories retain their contents.
Worker credential directories remain separate and are not relocated.

Each pending scope temporarily contains `.mindroom-private-storage-migration.json` with its exact owner, volume paths, and original directory inodes.
The intent moves with the primary scope and is removed after both locations and the current owner record are durable.
It contains private owner information and belongs on the protected storage volume.

## Interrupted or rejected startup

Restart the primary with the same mounted data and configured paths to resume an interrupted migration automatically.
Remounts that preserve the directory inodes are supported.
Do not remove or copy pending intent records, create destination directories, or start other writers during recovery.
Abrupt process death can leave partial temporary files from durable intent or owner writes.
These remain protected, untouched files and never authorize recovery; only the exact final intent and owner records do.

Startup rejects ambiguous owners, populated scopes without owner records, conflicting destinations, missing recorded session mirrors, unrelated recovery records, unsafe scope or record links, nested mounts, and links that would break after relocation.
Inspect and correct the reported conflict with all writers stopped, then restart.
There is no migration CLI or automatic rollback.
To return to an older deployment, stop all writers and restore the coordinated backups together before starting it.

## Future removal

Migration support can be removed only after defining an explicit supported upgrade floor that requires an intermediate release containing this migration.
A fixed number of releases is insufficient because installations can skip releases or restore older backups.
A later release must continue rejecting unsupported historical layouts and direct operators to the supported intermediate upgrade.
