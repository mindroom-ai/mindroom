# Config Bundles

A config bundle is a complete configuration directory: `config.yaml`, its included YAML and text files, prompts, `.env`, and any other assets.
The bundle commands deploy such a directory as a unit, validate it before activation, refuse to overwrite hand edits, keep the replaced tree for rollback, and confirm that the running MindRoom applied it.

| Task | Command |
| --- | --- |
| Hot-apply a YAML/include change to a running MindRoom, wait for the result, and optionally roll back | [`config apply-bundle`](#config-apply-bundle) |
| Install any change, including `.env`, plugins, or other assets, without waiting for the runtime | [`config install-bundle`](#config-install-bundle) |
| Seed or update the config directory when MindRoom starts | [`mindroom run --bootstrap-config-bundle`](#install-at-startup) |
| Check whether a change can be applied by config hot reload | [`config classify-change`](#config-classify-change) |
| Confirm the runtime applied a config | [`config fingerprint` and `config check-applied`](#config-fingerprint-and-config-check-applied) |

Keep runtime state and storage outside the bundle directory, and leave `.mindroom-bundle.json` inside it to the installer.

## config install-bundle

Validate and install a complete configuration directory:

```bash
mindroom config install-bundle ./candidate --target ./active --json
mindroom config install-bundle ./candidate --target ./active --initialize-only
mindroom config install-bundle ./candidate --target ./active --revision deploy-2
mindroom config check-applied --path ./active/config.yaml --fingerprint <receipt-fingerprint> --wait 300
# Roll back, pinned to the digest of the tree the installation moved aside:
mindroom config install-bundle ./active.previous --target ./active --expected-digest <receipt-previous-digest> --json
```

`--target` is the directory to install into, `--config` selects the config file relative to the bundle root (default `config.yaml`), and `--json` prints the receipt as JSON.

The candidate is validated as MindRoom would load it, including includes and environment, before anything is activated.
Includes must stay inside the config file's directory, as described in [Splitting the Configuration](https://docs.mindroom.chat/configuration/#splitting-the-configuration-into-multiple-files).
Run the installer with the runtime's environment and filesystem access; external resources the config references are validated but not part of the bundle.
Automatic config migrations rewrite the installed copy, never the source bundle.

An invalid candidate leaves the active and previous trees untouched.
Bundles containing symlinks or special files are rejected, and so are mounts at or inside the target and its recovery paths, even with `--force`.
The target must be a directory below a writable mount, not the mount point itself.

### Protection against hand edits

An identical candidate returns `unchanged` and changes nothing.
A changed candidate replaces the active tree only if every file name, mode, and content still matches the last installation, including `.env` and files outside the YAML include graph.
A target that was edited or never installed by this command fails with `Target contains authored edits or is unmanaged; use install-bundle --force to replace it explicitly.`
`--force` adopts or resets such a target but never bypasses validation; it cannot be combined with `--initialize-only`.

`--source-only` refuses a changed candidate unless every difference lies within the YAML/include sources of `--config`, as reported by [`config classify-change`](#config-classify-change); a refused candidate is never installed.
`--source-only` also requires an existing target, so omit it for the initial installation.

### Rolling back

Every replacement keeps the complete former tree at `TARGET.previous`; keep it until the runtime confirms the new config.
To roll back, install `TARGET.previous` with the receipt's `previous_digest` as `--expected-digest`.
The digest covers the whole tree, including `.env` and other assets that the config fingerprint does not cover, and `--expected-digest` can pin any candidate source this way.
After the rollback, the rejected tree becomes the new `TARGET.previous`, so repeating a pinned rollback fails instead of switching back to the rejected tree.
If the active tree was edited since the installation, rollback also requires `--force`.
`install-bundle` never rolls back on its own when the runtime rejects a config; use `apply-bundle --rollback-on-failure` for that.

### Install at startup

`mindroom run --bootstrap-config-bundle SOURCE --config TARGET/config.yaml` installs the bundle before MindRoom loads its environment.
The target directory is the selected config file's directory, and the source must contain the same config filename at its root.
Validation reads the bundle's `.env`, with exported process variables and an explicit `--storage-path` taking precedence, and startup then uses the installed tree.

Without a revision, startup installs the bundle only when the target directory is absent and otherwise keeps it unchanged and unvalidated, returning `initialized` with no digest or fingerprint.
Add `--bootstrap-config-bundle-revision REVISION` (which requires `--bootstrap-config-bundle`) to roll out a new bundle through restarts:

- A target whose recorded revision matches is kept, including later hot installs and rollbacks.
- A target with a different or no recorded revision gets the candidate after full validation, but only if the target is unedited and was installed by these commands, even when the content is identical; adopt or reset it once with `install-bundle --force`.
- When only the revision differs, only the recorded revision changes and the files stay untouched.
- A failed installation keeps the previous revision.

These are the same rules as `install-bundle --initialize-only` with and without `--revision`.
A revision is a nonblank string of at most 128 UTF-8 bytes without control characters.
Installs without `--revision` keep the active revision and ignore any revision recorded in the incoming bundle.

### Receipts and exit codes

Exit `0` means the tree was installed or kept; exit `2` reports an installation error, printed as `{"status": "failed", "detail": ...}` with `--json`.
The JSON receipt contains these fields:

| Field | Meaning |
| --- | --- |
| `status` | `installed`, `unchanged`, or `initialized` |
| `config_path` | Installed config file |
| `digest` | Whole-tree digest of file names, modes, and contents, excluding installer metadata; `null` for `initialized` |
| `fingerprint` | [Config source fingerprint](#config-fingerprint-and-config-check-applied) to pass to `check-applied`; `null` for `initialized` |
| `recovery_pending` | `true` when the new tree is active but moving the old tree to `TARGET.previous` needs a retry |
| `previous_digest` | Whole-tree digest of the tree moved to `TARGET.previous`, or `null` when nothing was replaced |

A changed installation triggers the running MindRoom's [config hot reload](https://docs.mindroom.chat/configuration/#configuration-file); confirm the result with `config check-applied`.
Hot reload does not reread `.env` or other assets, so environment changes can need a restart.

### Interrupted installs

Readers can briefly find the target missing during replacement but never see a partly copied tree.
Installation is not durable against power loss and does not coordinate with other programs writing to the tree.
After an interrupted install, rerun the command to finish recovery before doing anything else.
Do not modify the reserved sibling paths `.TARGET.pending`, `.TARGET.retired`, `.TARGET.transaction`, and `.TARGET.lock`.
If recovery reports changed previous or recovery trees, inspect them manually.
A process killed while copying can leave an unused `.TARGET.stage-*` directory that you can delete.

## config classify-change

Check whether a candidate tree differs from the current tree only in the YAML/include sources that config hot reload rereads:

```bash
mindroom config classify-change ./active ./candidate --json
mindroom config classify-change ./old-tree ./new-tree --config prod/config.yaml --config staging/config.yaml
```

Sources are the files the YAML loader reads from each `--config` entrypoint (default `config.yaml`, repeatable) in either tree, including transitively included YAML and text files, plus directories that only appear or disappear around them.
Every entrypoint must load in both trees.
Every other difference is reported as `other`, including `.env`, plugins, scripts, unreferenced files, directory mode changes, and include paths that switch between file and directory.
Installer metadata in `.mindroom-bundle.json` is ignored.
Hot reload does not reread `other` paths, so they may need a restart or another mechanism such as [plugin hot reload](https://docs.mindroom.chat/plugins/#live-development-hot-reload).
A source-only result does not prove that the runtime applies every changed setting; confirm with `config check-applied`.
The command only reads both trees, rejects symlinks and special files, and lists paths but never file contents.

JSON output contains `status` (`source_only` or `non_source`), `sources`, and `other`.
Exit codes are `0` when every difference is a source change (including no difference), `1` for other changes, and `2` when either tree cannot be classified.

## config apply-bundle

Hot-apply a source-only change to the directory a running MindRoom loads, wait for the runtime result, and optionally restore the previous tree when the runtime rejects it:

```bash
mindroom config apply-bundle ./candidate --target ./active --rollback-on-failure --wait 300 --json
```

The command first checks that the runtime reports a reload status, using the same `--url`, `--timeout`, `MINDROOM_API_KEY`, and HTTPS rules as [`check-applied`](#config-fingerprint-and-config-check-applied), and installs nothing until it does.
It then installs the candidate like `install-bundle --source-only` without `--force`, honoring `--config` and `--expected-digest`, and waits up to `--wait` seconds (default 300) for the runtime result.
Changes to `.env`, plugins, or other files that hot reload does not reread are refused before anything is installed; install those with `install-bundle` instead.
With `--rollback-on-failure`, a runtime rejection of a changed installation restores `TARGET.previous`, pinned to the receipt's `previous_digest`, and waits again for that tree.
Pending, unreadable, or restart-required results never roll back, and neither does a failure when a reload of the same or an unknown config was already pending or failed before installation.

| Status | Exit code | Meaning |
| --- | --- | --- |
| `applied` | `0` | The runtime applied the candidate. |
| `pending` | `1` | The candidate is installed, but the runtime had not settled it within `--wait`; confirm later with `check-applied`. |
| `failed` | `2` | Nothing was installed, or the runtime rejected the candidate and no rollback was requested or possible, so the candidate stays installed. |
| `rolled_back` | `3` | The runtime rejected the candidate, and the restored previous tree was applied. |
| `unconfirmed` | `4` | The runtime result could not be read or proven, or restoring or confirming the previous tree failed; inspect before retrying. |
| `restart_required` | `5` | The runtime adopted the candidate, but part of it needs a restart. |

The JSON receipt contains `status`, `detail`, the candidate `install` receipt and its `runtime_status`, and the `rollback` receipt and its `rollback_runtime_status`.
`install` is `null` when nothing was installed, and `rollback` is `null` unless the previous tree was restored.

## config fingerprint and config check-applied

Confirm that the running MindRoom finished applying a particular config:

```bash
mindroom config fingerprint --path ./config.yaml
mindroom config check-applied --path ./config.yaml --wait 300
mindroom config check-applied --fingerprint <sha256> --url https://example.org --json
```

`fingerprint` prints a SHA-256 over the config file and all transitively included YAML and text files; for a config without includes it equals the file's SHA-256.
Moving the tree to another directory keeps its fingerprint.
Comment and formatting changes alter the fingerprint, while unrelated files and environment variables do not.

When reading a config file, both commands refuse legacy access settings, exiting `2`, because the runtime would rewrite those source files when migrating them.
Run `mindroom config migrate --path <config-path>` first.
`check-applied --fingerprint` does not read the local config, so it needs no local migration, but the fingerprint must be that of the migrated config.

`check-applied` reads the authenticated `GET /api/config/reload-status` endpoint from `--url` (default `MINDROOM_URL`, then `http://127.0.0.1:8765`).
It requires `MINDROOM_API_KEY`, and HTTPS for remote URLs.
Without `--wait`, it checks once; `--wait` bounds the polling period and `--timeout` (default 10 seconds) bounds each request.
Neither command changes config or triggers a reload.

| Exit code | Meaning |
| --- | --- |
| `0` | The runtime applied the expected fingerprint. |
| `1` | Pending, or the runtime reports a different fingerprint, including when `--wait` expires. |
| `2` | The runtime failed to apply the expected fingerprint, needs a restart, or its status is unavailable. |

JSON output shows the expected and observed fingerprints.
The endpoint reports only the latest reload, with status `pending`, `applied`, `failed`, `restart_required`, or `unavailable` (HTTP 503, until MindRoom is ready).

The runtime reports `applied` once the reload finished, including changes that need no agent restart, but this does not prove that every bot or external service is healthy.
A change to the [event journal](https://docs.mindroom.chat/deployment/storage/#event-journal) database reports `restart_required`.
If the YAML fails to parse, the failure has no fingerprint, so a wait for a specific fingerprint reports pending until it expires.
