# Private storage upgrades

A requester-key upgrade can change the canonical path of existing private agent state.
Primary startup automatically migrates verified legacy state before credentials, watchers, API services, Matrix ingestion, or worker launch.
The migration moves only roots with an exact historical owner record matching the old key and directory hash.
It retains current requester isolation and refuses recordless populated roots, invalid records, existing destinations, and changed source data.
Fresh installations and completed upgrades do not stop workers or repeat relocation.
The CLI is optional for inspection and exceptional offline recovery.

## Deployment prerequisites

Hold ingress closed and stop the previous primary, independent API processes, scripts, export watchers, worker controllers, and supervised local children before starting the upgraded primary.
Use an exclusive cutover, such as a singleton deployment with a replacement strategy that stops the previous primary first.
Primary startup then stops exactly owned Kubernetes worker Deployments or Docker worker containers and waits for their removal before inspecting private file contents or moving state.
It preserves worker files and credentials; Kubernetes Services and Secrets remain in place.
New worker runtimes start through the normal lifecycle after migration completes.
Allow enough startup-probe time for worker termination, complete file inventories, and SQLite validation across all private scopes, in addition to the directory moves.
The API remains unavailable during migration; an aggressive restart budget can repeatedly interrupt recovery.
Startup logs report worker stopping, inspection, application, and completion phases with counts instead of private owner mappings.
Local execution relies on the stopped-supervisor prerequisite.
A separately managed static runner has no automatic stop guarantee, so automatic migration refuses that configuration until an operator establishes an offline recovery boundary.
Old binaries and workers without access to the shared participation markers cannot honor the new startup fence.
The migration locks coordinate participating upgrade processes; they do not fence an old primary or an external controller that ignores them.

Back up all participating state and session volumes together and verify that the recovery point can be restored.
Backup readiness is the deployment's responsibility; startup does not claim to verify a backup.
The protected transaction receipt is not a full volume backup.
Mount every configured state, session, and control-state volume before startup and keep the same mounts throughout migration and recovery.
Startup uses `MINDROOM_SESSION_STORAGE_PATH` and `MINDROOM_CONTROL_STATE_PATH` when configured and refuses missing or mismatched participants for a required migration.
It does not create a fallback directory for a missing migration volume.
Close database writers and checkpoint their WAL files before cutover; the migration refuses nonempty WAL or rollback journals and never checkpoints or edits source databases itself.

## Optional offline inspection and recovery

For a manual cutover, stop every writer, including dedicated workers, before using these commands.
The command's `--writers-stopped` and `--backup-verified` flags are operator assertions; verify the actual conditions separately.
Substitute the actual mounted paths in these examples.
Keep the manifest in a protected persistent location; it contains private owner mappings and original owner records.
The manifest is created with mode `0600` and cannot overwrite an existing file.

```bash
mindroom storage-upgrade plan --storage ./state --sessions ./sessions --control-state ./control-state --manifest ./private-upgrade.json
mindroom storage-upgrade apply ./private-upgrade.json --writers-stopped --backup-verified
mindroom storage-upgrade verify ./private-upgrade.json
```

Omit `--sessions` when sessions share the main storage root.
Omit `--control-state` only when control state uses the default `state/control_state` location.
Private `user` scopes move once with all their agents; `user_agent` scopes keep their original agent association.
Matching session directories move within their own volume.
Verification reads SQLite integrity, schema, and actual session/run rows through immutable read-only connections without creating sidecars.

The transition uses POSIX directory rename while holding per-volume migration locks, rechecking source identity and destination absence immediately before each move, and flushing directory changes.
It never copies across filesystems, merges directories, or replaces an observed destination.
Destination absence and rename are separate operations, so this does not provide atomic no-replace protection against an uncooperative writer that ignores the locks.
Every other writer must remain stopped throughout apply or recovery.

Historical absolute path strings in notes, memory, and session history are preserved as original content.
They are not automatically rewritten or treated as active references.
Absolute symlinks and recognized worker runtime metadata require separate explicit recovery because their paths can be interpreted after restart.
Worker credentials, worker lifecycle metadata, and process handles are not relocated or used to infer private ownership; recover credentials through their normal authenticated flow.

## Recover an interrupted operation

After an interrupted forward migration, restart the primary under the same deployment prerequisites.
Startup discovers the original plan embedded in the participating volume receipts and resumes automatically without a separate manifest.
A complete receipt uses normal startup checks, so later application writes do not trigger comparison with stale migration fingerprints.
An interrupted explicit reversal is finished automatically, but startup stays stopped afterward.
A completed rollback also stays stopped until explicit recovery; restarting does not silently undo it.

For manual recovery, keep ingress closed and all writers stopped.
Use the original protected manifest or an automatically created `.mindroom-storage-upgrade.json` receipt from a participating storage root.

```bash
mindroom storage-upgrade resume ./private-upgrade.json --writers-stopped --backup-verified
mindroom storage-upgrade rollback ./private-upgrade.json --writers-stopped

# An automatic migration needs no separate manifest for manual recovery.
mindroom storage-upgrade rollback ./state/.mindroom-storage-upgrade.json --writers-stopped
```

Receipts persist the original owner mapping and transaction phase on every participating volume.
Recovery validates the exact filesystem state to determine which moves and owner replacements already completed.
Each rename and owner replacement is flushed before completion; there is no atomic transaction spanning volumes.
If initial participation was interrupted, rollback completes prepared receipts on all volumes before recording reversal intent.
A missing volume, conflicting directory, changed owner record, or changed private content blocks recovery rather than overwriting data.
Rollback is intended for unchanged pre-traffic state and restores original owner-record bytes.
After new traffic writes data, stop writers and review a coordinated recovery plan instead of restoring stale paths or starting an old image.

Session verification requires the installed Agno session schema and validates any existing matching run table, including application names such as `writer_sessions` and `writer_sessions_runs`.
An absent run table is valid because Agno creates it lazily.
Known `learning/<agent>.db` files receive the same sidecar, integrity, schema, and row checks.
Learning-only databases can contain Agno learning or memory tables without a session table.
Missing session schemas or required columns in present session/run tables require explicit offline recovery before relocation; verification never creates or migrates database schemas.
Relative links must remain inside their moved scope throughout resolution; links that traverse through the old scope name are refused.
Owner replacement uses a receipt-derived temporary filename inside the same scope, and recovery removes only that exact operation's recognizable partial owner write.
Unknown temporary files are preserved and block recovery.
Hot runtime fences cache only complete receipts by file identity, size, ownership, and nanosecond modification/change timestamps, checking marker identities and participant agreement on every call.
Startup and independent destructive export preflights reread full receipts and scan legacy owners.
Secondary session scopes must have a verified primary owner or belong to the exact recorded recovery operation; directory names alone never establish ownership.
Owner attribute names and value encodings are validated before manual application creates a lock or transaction marker.
