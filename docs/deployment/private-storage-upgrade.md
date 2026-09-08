# Private storage migration

Primary startup automatically relocates every verified historical private scope to its current collision-safe requester path before accepting traffic.
Both the orchestrator and standalone primary API run this step before credentials, background work, or worker launch.
Current and empty storage need no migration and do not trigger worker shutdown or content scans.

## Before upgrading

1. Stop every writer through the deployment lifecycle, including the previous primary, independent controllers, supervised processes, managed Docker or Kubernetes workers, and external runners.
   The migration locks cannot fence older binaries.
2. Take coordinated backups of primary storage, optional session storage, and worker state while writers are stopped.
3. Preserve the configured primary and session volume paths and mount the original volumes.
   When `MINDROOM_SESSION_STORAGE_PATH` is configured, make sure that volume is available before starting.
4. Start the new primary.
   If migration is needed, startup locks the volumes and stops exactly owned Docker or Kubernetes worker runtimes without deleting their durable state.
   This startup check is additional enforcement; workers must already be stopped before taking the coordinated backups.
   It validates every affected scope and session mirror before moving any directory.

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
