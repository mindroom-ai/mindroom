# Private storage upgrades

A requester-key upgrade can change the canonical path of existing private agent state.
MindRoom refuses startup when a verified legacy owner needs migration or a storage-upgrade transaction is incomplete.
The offline utility migrates only roots with an exact historical owner record matching the old key and directory hash.
It retains current requester isolation and refuses recordless populated roots, invalid records, existing destinations, and changed source data.

## Prepare an offline recovery point

Hold ingress closed and stop every primary process, API, dedicated worker, script, export watcher, and worker controller that can write either volume.
Old binaries and workers without access to the shared participation markers cannot honor the new startup fence.
The command's `--writers-stopped` flag is an operator assertion; verify the actual processes and controllers are stopped separately.

Back up all participating state and session volumes together and verify that the recovery point can be restored.
Mount every configured volume before planning and keep the same mounts throughout apply, verification, and recovery.
Pass any separately configured control-state root so active script handles can be checked.
Close database writers and checkpoint their WAL files before planning; the utility refuses nonempty WAL or rollback journals and never checkpoints or edits source databases itself.

## Plan and apply

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
Worker credentials, worker lifecycle metadata, process handles, and existing worker resources are not migrated; recover credentials through their normal authenticated flow and start fresh workers.

## Recover an interrupted operation

Keep ingress closed and all writers stopped.
Use the original protected manifest to resume or reverse the recorded transaction.

```bash
mindroom storage-upgrade resume ./private-upgrade.json --writers-stopped --backup-verified
mindroom storage-upgrade rollback ./private-upgrade.json --writers-stopped
```

Receipts record each participating volume and every durable transition; there is no atomic transaction spanning volumes.
A missing volume, conflicting directory, changed owner record, or changed private content blocks recovery rather than overwriting data.
Rollback is intended for unchanged pre-traffic state and restores original owner-record bytes.
After new traffic writes data, stop writers and review a coordinated recovery plan instead of restoring stale paths or starting an old image.

Session verification requires the installed Agno session schema and validates any existing matching run table, including application names such as `writer_sessions` and `writer_sessions_runs`.
An absent run table is valid because Agno creates it lazily.
Missing session schemas or required columns in present session/run tables require explicit offline recovery before relocation; verification never creates or migrates database schemas.
Relative links must remain inside their moved scope throughout resolution; links that traverse through the old scope name are refused.
Owner replacement uses a receipt-derived temporary filename inside the same scope, and recovery removes only that exact operation's recognizable partial owner write.
Unknown temporary files are preserved and block recovery.
Hot runtime fences cache only complete receipts by file identity, size, ownership, and nanosecond modification/change timestamps, checking marker identities and participant agreement on every call.
Startup and independent destructive export preflights reread full receipts and scan legacy owners.
