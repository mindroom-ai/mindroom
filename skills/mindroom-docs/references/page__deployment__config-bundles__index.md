# Config Bundles

## config install-bundle

Install a complete configuration directory, including nested YAML includes, prompts, and its `.env`:

```bash
mindroom config install-bundle ./candidate --target ./active --json
mindroom config install-bundle ./candidate --target ./active --initialize-only
mindroom config install-bundle ./candidate --target ./active --revision deploy-2
mindroom config check-applied --path ./active/config.yaml --fingerprint <receipt-fingerprint> --wait 300
# Pin rollback to the digest of the tree the installation moved aside:
mindroom config install-bundle ./active.previous --target ./active --expected-digest <receipt-previous-digest> --json
```

To install, confirm, and roll back in one step, use [`config apply-bundle`](#config-apply-bundle).

`--config` selects a relative file inside the bundle (default `config.yaml`).
The installer copies into a sibling staging directory and uses the native loader for include, environment, and config validation before publication.
Native includes must remain inside the selected config file's directory.
Run the installer with the runtime's environment and filesystem access; external resources referenced by config are outside the bundle's drift protection.
Relative paths resolve within the staged candidate during validation.
Automatic config migrations affect the staged copy only.

`--initialize-only` without `--revision` preserves any existing target directory.
It does not validate that existing tree and returns `initialized` with no fingerprint.
With `--revision`, a matching revision stored in `.mindroom-bundle.json` preserves the active tree after recovery, including any later hot installs.
A different or missing stored revision validates the candidate.
When `--revision` is supplied and the stored revision is missing or differs, an existing active tree must be owned and unedited, even when the candidate has identical content.
Use ordinary installation with explicit `--force` to adopt an unmanaged tree or reset authored drift.
The revision is an opaque, nonblank string of at most 128 UTF-8 bytes with no control characters.
It changes only after successful validation and publication.
If candidate content is identical, only metadata changes atomically; authored files and previous trees stay untouched.
Ordinary installs without `--revision` carry the active revision forward and ignore revision metadata in the incoming bundle.
Without this flag, unchanged trees return `unchanged`.
Changed managed trees replace the active tree only if its file names, modes, and contents still match the last installation.
This covers `.env` and files outside the YAML include graph too.
An unmanaged or edited tree requires explicit `--force`; this never bypasses validation.
`--force` cannot be combined with `--initialize-only`.
`--source-only` refuses a changed candidate unless every difference from the existing active tree lies within the YAML/include sources of `--config`, as reported by [`config classify-change`](#config-classify-change).
The comparison uses the staged candidate under the installer lock, so a refused candidate is never published.

Successful replacement retains the complete former tree at `TARGET.previous`.
Invalid candidates and failed copies leave active and previous trees untouched.
Symlinks and special files are rejected.
Mounts at or inside active and recovery trees are rejected before rotation or cleanup, including with `--force`.
Reserve `.mindroom-bundle.json` inside the tree for installer metadata; keep runtime state and other frequently modified files outside the bundle.

Rollback uses the same validated installation operation with `TARGET.previous` as its source.
Pass the installation receipt's `previous_digest` as `--expected-digest`; the guard compares the staged whole-tree digest before replacing active files, including `.env` and unrelated assets.
The rejected active revision then becomes the new previous tree.
Repeating the pinned rollback after a lost receipt fails safely instead of toggling back to the rejected revision.
The native fingerprint alone cannot distinguish revisions that only change `.env` or unrelated assets.
The guard can pin any mutable candidate source.
If the active revision has since been authored, rollback also requires explicit `--force`.
A failed or missing runtime receipt does not automatically roll back filesystem state.

For startup, `mindroom run --bootstrap-config-bundle SOURCE --config TARGET/config.yaml` runs initialize-only installation before loading the runtime environment.
Add `--bootstrap-config-bundle-revision REVISION` to install a changed declared revision before startup; this option requires a bootstrap source.
The target directory is derived from the selected config path, and the source must contain the same config filename at its root.
Validation reads the staged `.env`, with exported process values and explicit `--storage-path` taking precedence.
Startup then resolves paths and `.env` again from the installed tree.
Without a declared revision, existing target directories are preserved on restart.
With a matching declared revision, restarts preserve hot updates and guarded rollbacks; a new revision uses native validation and drift protection.
Keep storage outside this dedicated config directory.

**Filesystem limits:** directory replacement uses two renames on the target filesystem.
Readers can briefly see a missing target between renames, but never a partly copied tree.
This is not a transaction across concurrent reads or external writers, nor a power-loss durability guarantee.
Installer calls serialize using a sibling lock.
A failed publication rename restores the former active tree.
Interrupted operations leave complete trees at `.TARGET.pending` and `.TARGET.retired`; `.TARGET.transaction` records whole-tree content digests and persists ownership of `TARGET.previous` between installations.
Journal updates use atomic replacement; partial writes preserve the prior record.
Recovery accepts copied or restored trees with identical file names, modes, and bytes, even on another mount; it rejects changed previous or recovery contents.
Reserved regular `.mindroom-bundle.json` metadata is excluded from these digests, but links and special files are always rejected.
This verifies preserved content, not filesystem object identity.
Hashing previous and recovery trees adds filesystem reads during installation and bootstrap.
Retry the command to recover before proceeding.
Do not manually modify these reserved recovery paths.
If retired cleanup deletes some files before failing, its content no longer matches the journal; retry fails closed and requires manual inspection.
A process killed while copying may leave an unused `.TARGET.stage-*` directory for manual cleanup.
The target must be a directory below a writable mount, not the mount point itself.

JSON receipts contain `status`, `config_path`, `digest`, `fingerprint`, `recovery_pending`, and `previous_digest`.
The whole-tree `digest` identifies file names, modes, and contents, excluding installer metadata; initialize-only preservation returns no digest or fingerprint.
`previous_digest` is the whole-tree digest of the tree this installation moved to `TARGET.previous`, or `null` when it replaced nothing.
Exit `0` confirms filesystem installation or preservation; exit `2` reports an installation error.
An `installed` receipt with `recovery_pending: true` means publication succeeded but previous-tree rotation or cleanup needs a retry.
A failure writing the receipt after publication does not undo activation; retrying an immutable source is idempotent.
Pin mutable rollback sources as above.
The fingerprint identifies native YAML/include sources, so use the existing `check-applied` command to confirm runtime application.
The installer advances the root config mtime on changed activation for the existing watcher.
Environment files and arbitrary bundle assets are not covered by that reload receipt; environment changes can require a runtime restart.
Preserve `TARGET.previous` until runtime confirmation succeeds.

## config classify-change

Decide whether a candidate tree differs from the current tree only in the YAML/include sources that a runtime config reload rereads:

```bash
mindroom config classify-change ./active ./candidate --json
mindroom config classify-change ./old-tree ./new-tree --config prod/config.yaml --config staging/config.yaml
```

Sources are the files the native YAML loader reads from each `--config` entrypoint in either tree, including transitively included YAML and text files.
Directories that only appear or disappear around those files also count as sources.
Every other difference is reported as `other`, including `.env`, plugins, scripts, files no entrypoint reads, mode changes on directories, and include paths that switch between file and directory.
Config reload does not reread `other` paths, so they may need a runtime restart or another reload mechanism such as [plugin hot reload](https://docs.mindroom.chat/plugins/#live-development-hot-reload).
A source-only result does not prove the runtime applies every changed setting; confirm with `config check-applied`.
Each `--config` entrypoint (default `config.yaml`) must load in both trees.
Installer metadata in `.mindroom-bundle.json` is ignored, and symlinks and special files are rejected as in installation.
The command only reads both trees, and its results list paths, never file contents.

JSON output contains `status` (`source_only` or `non_source`), `sources`, and `other`.
Exit codes are `0` when every difference is a source (including no difference), `1` for other changes, and `2` when either tree cannot be classified.

## config apply-bundle

Hot-apply a source-only change to the directory a running MindRoom loads, wait for the runtime to apply it, and optionally restore the previous tree when the runtime rejects it:

```bash
mindroom config apply-bundle ./candidate --target ./active --rollback-on-failure --wait 300 --json
```

The command first reads `GET /api/config/reload-status` and installs nothing unless the runtime reports a reload status, with the same `--url`, `--timeout`, `MINDROOM_API_KEY`, and HTTPS rules as `check-applied`.
It then runs an ordinary installation without `--force`, honoring `--config` and `--expected-digest`, and waits up to `--wait` seconds (default 300) for the runtime result of the installed fingerprint.
The installation always uses `--source-only`, because the runtime fingerprint covers only YAML/include sources; changes to `.env`, plugins, or other files that config reload does not reread are refused before anything is installed, so install those with `install-bundle` instead.
With `--rollback-on-failure`, only a `failed` runtime result for the exact fingerprint of a changed installation restores `TARGET.previous`, pinned to the receipt's `previous_digest`, and then waits again for that tree's fingerprint.
A reload already pending or failed before installation, for the same or a not-yet-known fingerprint, does not count, and pending, unreadable, or restart-required results never roll back.

| Status | Exit code | Meaning |
| --- | --- | --- |
| `applied` | `0` | The runtime applied the candidate fingerprint. |
| `pending` | `1` | The candidate is installed, but the runtime had not settled its fingerprint within `--wait`; confirm later with `check-applied`. |
| `failed` | `2` | Nothing was installed, or the runtime rejected the candidate and no rollback was requested or possible, so the candidate stays installed. |
| `rolled_back` | `3` | The runtime rejected the candidate, and the restored previous tree was applied. |
| `unconfirmed` | `4` | The runtime result could not be read or proven, or restoring or confirming the previous tree failed; inspect before retrying. |
| `restart_required` | `5` | The runtime adopted the candidate, but part of it needs a restart. |

The JSON receipt contains `status`, `detail`, the candidate `install` receipt and its `runtime_status`, and the `rollback` receipt and its `rollback_runtime_status`.
`install` is `null` when nothing was installed, and `rollback` is `null` unless the previous tree was restored.

## config fingerprint and config check-applied

Confirm that the running runtime finished applying a particular config source:

```bash
mindroom config fingerprint --path ./config.yaml
mindroom config check-applied --path ./config.yaml --wait 300
mindroom config check-applied --fingerprint <sha256> --url https://example.org --json
```

`fingerprint` hashes the source bytes captured by the YAML loader, including all transitively included YAML and text files.
A single file uses its plain SHA-256; multiple files combine their relative paths and content hashes.
Moving the same tree to another directory preserves its fingerprint.
Comments and formatting changes affect the fingerprint; unrelated files and environment variables do not.

Legacy access settings must be migrated with `mindroom config migrate --path <config-path>` before capturing a fingerprint.
When reading a config file, both commands reject legacy access settings with exit `2`: automatic migration would rewrite their source bytes during reload.
Explicit `--fingerprint` skips reading local config sources; the caller must supply a fingerprint of the migrated source.

`check-applied` captures the expected fingerprint once before polling the authenticated `GET /api/config/reload-status` endpoint.
It requires `MINDROOM_API_KEY` and HTTPS for remote endpoints.
Without `--wait`, it checks once.
`--timeout` bounds each HTTP request; `--wait` bounds the polling period.
Neither command changes config or triggers a reload.

Exit codes are `0` for matching completed application, `1` for pending or a different fingerprint (including wait expiration), and `2` for matching failure, restart required, or unavailable status.
JSON output identifies the expected and observed fingerprints.
Only the latest reload result is retained in memory.

The runtime acknowledges a fingerprint after its reload plan finishes, including changes that need no agent restart.
A loaded API config cache does not count as completion.
Known event-journal changes requiring a process restart return `restart_required`.
Completion does not guarantee that every bot or external service is healthy.
If parsing fails before the include tree is known, the failure has no fingerprint and cannot settle a wait for a particular fingerprint.
