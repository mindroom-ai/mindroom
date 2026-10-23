---
icon: lucide/user-pen
---

# Renaming an Agent's Matrix Account

`mindroom agents rename-account` moves MindRoom's state over to a new Matrix ID after your homeserver renamed an agent's account in place.
The agent's name in `config.yaml` does not change.
Only its Matrix ID does, for example from `@mindroom_atlas:example.org` to `@mindroom_nova:example.org`.

MindRoom keeps credentials, workers, journal rows, and other state under the Matrix ID.
After a server-side rename it would look for all of them under the new ID and find nothing.
The command moves them, rewrites what holds the old ID, and tells you what it left alone.

## When to use it

Run it after the homeserver renamed the account, and before MindRoom starts for the first time under the new ID.

- The MindRoom fork of Tuwunel renames a local user in place with the admin command `query user-id-rewrite` ([mindroom-tuwunel#109](https://github.com/mindroom-ai/mindroom-tuwunel/pull/109)).
  Its operator guide is `docs/user-id-rewrite.md` in that repository.
  The account keeps its password, rooms, and history.
- Synapse has no equivalent admin command, so this page does not apply to a Synapse server.
- The new ID must be on the same server as the old one.
  A rename in place keeps the server, and the command refuses another one.
- An in-place rename is only safe for an account whose rooms are not shared with other servers.
  The rewritten events no longer match their hashes and signatures, so a federated server that already holds them would reject them.

MindRoom takes an agent's Matrix ID from the `username` stored in `matrix_state.yaml`.
If you already changed that username to the new localpart by hand, see [After a manual username change](#after-a-manual-username-change).
If MindRoom then started under the new ID, it created fresh state there.
Add `--discard-new-principal` to drop that state when it holds no replies or credentials.
[New-ID state](#new-id-state) explains what it deletes and when it still refuses.

The command supports the SQLite event journal and the `static_runner` or `docker` worker backend.
Any other setup is refused with `not supported yet`.

## Procedure

1. Rename the account on the homeserver and confirm the new ID can log in with the agent's old password.
2. Stop MindRoom, and keep it from starting by itself until step 6.
   Disable its systemd unit (`systemctl disable --now <unit>`) or set its container's restart policy to `no` (`docker update --restart=no <container>`).
   If the unit comes from a NixOS configuration, also pause automatic deployments such as comin, which would start it again, and do not reboot.
   A MindRoom that starts after a killed run finds the agent half-renamed, see [When a step fails](#when-a-step-fails).
3. Take a snapshot or copy of the storage directory, the config directory, and the homeserver's database.
   Include the directories that `MINDROOM_CONTROL_STATE_PATH` and `MINDROOM_SESSION_STORAGE_PATH` point to when they are outside the storage directory, because the command changes files there too.
   The command keeps its own backups, but a snapshot is the only rollback that also covers the homeserver.
4. Run a dry run as the OS user the service runs as, with the service's environment and the same `--config` and `--storage-path`.
   Variables such as `MATRIX_HOMESERVER`, `MINDROOM_STORAGE_PATH`, `MINDROOM_CONFIG_PATH`, `MINDROOM_WORKER_BACKEND`, `MINDROOM_CONTROL_STATE_PATH`, and `MINDROOM_SESSION_STORAGE_PATH` decide which server the login check asks, which files the command reads, and whether it retires Docker workers.
   For a systemd service, `systemctl show <unit> -p User -p Environment -p EnvironmentFiles` lists them.
   A file the command rewrites keeps its mode but becomes owned by whoever ran the command, so a run as root can leave files the service can no longer write.
   The dry run prints the plan and changes nothing.
   The one exception is a config that still uses a retired access field: `access_model` or `matrix_room_access` at the top level, or `global_users`, `agent_reply_permissions`, `default_room_access`, or `room_permissions` under `authorization`.
   Loading it migrates it on disk, as every MindRoom command does, and writes the original to `config.yaml.pre-membership-access` beside it (see [Membership Access Migration](upgrades.md#membership-access-migration)).
   Configs without those fields are not touched.

   ```bash
   mindroom agents rename-account nova @mindroom_nova:example.org \
     --config config.yaml --storage-path mindroom_data
   ```

5. Read the plan, then apply it.
   The command asks `Apply this plan?` unless you pass `--yes`.

   ```bash
   mindroom agents rename-account nova @mindroom_nova:example.org \
     --config config.yaml --storage-path mindroom_data --apply
   ```

6. Turn auto-start back on and start MindRoom.
   The agent logs in with its stored password on a new device and syncs under the new ID.

A dry run neither contacts the homeserver nor checks that MindRoom is stopped.
`--apply` takes the locks a running MindRoom holds, so it refuses to run while MindRoom is up and blocks MindRoom from starting until it exits.
It then checks the login (see below), plans again under the locks, prints the plan, and asks for confirmation.

Neither plan can see collisions in the event journal.
Those in tables other than `room_member_joins` are found when the journal rewrite runs, which is the first step of `--apply` and comes after you confirmed.
The rewrite rolls back and the command refuses, naming `table.column`.
Nothing changes, except that the backup directory remains.

Running the same command again after a successful apply prints `Already adopted; nothing to do.` and exits 0.
Before it says so, it checks the event journal: when `matrix_state.yaml` names the new ID but the journal still has rows for `<name>@<another ID>`, it refuses and prints the command to run with `--from`.

### After a manual username change

If you set the agent's `username` in `matrix_state.yaml` to the new localpart yourself, the stored account names the new ID, but the event journal, OAuth stores, workers, and config are still under the old one.
On its next start MindRoom syncs as the new ID and fails on the old journal rows with `IngestionBatchIntegrityError`.
The command does not treat the username alone as proof of an adoption.
When the journal still has rows for `<name>@<old ID>` with an old ID on the new ID's server, it refuses, names them, and prints the command to run, with `--from <old ID>`:

```bash
mindroom agents rename-account nova @mindroom_nova:example.org \
  --from @mindroom_atlas:example.org --config config.yaml --storage-path mindroom_data
```

With `--from`, the old ID comes from the option instead of `matrix_state.yaml`.
The journal and files move from it and the config values are replaced, as in a normal run.
`matrix_state.yaml` keeps the device and access token MindRoom logged in with under the new ID; a `report:` line in the plan says so.
If you changed only the username and left the old `device_id` and `access_token`, they still belong to the old ID, and MindRoom refuses to start with them under the new one.
When that device's crypto store is still the old ID's under `encryption_keys/`, the command drops them, as a normal run does, and the next start logs in on a fresh device.
Without that store the command keeps them, so clear both fields by hand.
If MindRoom started after your edit, add `--discard-new-principal` for the state it wrote under the new ID.

`--from` must be a Matrix user ID on the new ID's server, differ from the new ID, and not be another stored account.
While `matrix_state.yaml` still names the old ID, `--from` may only repeat that ID.
Otherwise it must also be a former Matrix ID of this agent, because the command moves whatever is stored under it: a person's ID would hand their OAuth credentials and journal mentions to the agent.
The command accepts it when the event journal has rows for `<name>@<old ID>`, or when it is the ID MindRoom generates for the agent on that server, `@mindroom_<name>:<server>`, or `@mindroom_<name>_<namespace>:<server>` when `MINDROOM_NAMESPACE` is set.
Anything else is refused before anything changes, dry run included, with `--from <old ID> must be a former Matrix ID of agent <name>`.

Once a `--from` run has moved the journal, the journal no longer has rows for the old ID.
Running the same command again is then refused, unless the old ID is the generated one; in that case the rerun moves what is left, or prints `Nothing left to move from <old ID>; nothing was changed.` and writes nothing, not even a backup directory.

The "Already adopted" check only looks at the journal too.
If the journal holds no rows for the old ID, for example because you fixed it by hand, the command prints `Already adopted; nothing to do.` even when OAuth stores or workers are still under the old ID.
When the old ID is the one MindRoom generates for the agent, run the command with `--from <old ID>` to move what is left.
For any other old ID, `--from` needs `<name>@<old ID>` rows in the journal.
Restore the journal from before your fix to get them back only if MindRoom has not run since: a restored journal loses every row written after it, such as the record of replies MindRoom sent.
Otherwise move the remaining OAuth stores and workers by hand, as listed in [What changes](#what-changes).

## What changes

| Where | Change |
| --- | --- |
| `matrix_state.yaml`, account `agent_<name>` | The `username` becomes the new localpart. The old `device_id` and `access_token` are cleared so the next start logs in on a fresh device. The password stays. An account whose `username` already is the new localpart keeps its device unless that device still belongs to the old ID (see [After a manual username change](#after-a-manual-username-change)). |
| Event journal, `tracking/event_journal.db` | In one transaction: the principal `<name>@<old ID>` becomes `<name>@<new ID>`, the old ID is replaced in every other text column, the principal's approval grants are deleted, and leftover old-ID `room_member_joins` rows are deleted. The sync stream state of `<name>@<old ID>`, its `matrix_sync_consumers` row and its `matrix_ingestion_membership` rows, is deleted too: it belongs to the old device's Matrix sync stream, which no device continues, and a new stream reports every room from membership epoch 0, which kept rows would refuse. When `<name>@<old ID>` has no rows, there is nothing to move: only text cells that still hold the old ID are replaced, nothing of `<name>@<new ID>` is deleted, and a journal without such cells is left as it is. |
| `private_oauth/` | The directory of the old ID moves to the directory of the new ID. The worker key inside each OAuth credential store is rewritten. |
| `workers/` | Each worker whose key names the old ID as requester moves to the directory of its new key. Its `metadata/worker.json` and OAuth stores are rewritten. Its `venv/` is moved into the backup, because the scripts in it name the old path; MindRoom builds a new one on the worker's next use, and packages installed into the old one must be installed again. Docker control records and config projections of the old key are removed. |
| `private_instances/` and `tracking/private_instances/` | Private scopes owned by the old ID move to the scope of the new ID, and their owner record is rewritten. When [`MINDROOM_SESSION_STORAGE_PATH`](../configuration/index.md#environment-variables) is set, their session databases below it move the same way. Links to a moved scope are moved into the backup, because they would no longer lead anywhere. |
| `tracking/agents/*/personal_rooms/*.json` | The old ID is replaced in every record. A record owned by the old ID moves to the file name of the new ID. |
| `matrix_message_sends/` in the control state directory | Send intents of the old ID are removed from these files, because MindRoom refuses to replay an intent as another sender. Intents of other senders, including the new ID, stay. The directory is `control_state/` under the storage directory unless [`MINDROOM_CONTROL_STATE_PATH`](../configuration/index.md#environment-variables) points elsewhere. |
| `config.yaml` and the YAML files it includes | Every string value equal to the old ID, such as entries in user lists, `bot_accounts`, or `authorization.aliases`, becomes the new ID. A matching value inside a literal or folded block scalar makes the command refuse with `edit it by hand`. |

The event journal rewrite, the personal-room records, and the leftover scan match whole user IDs the way Tuwunel's `query user-id-rewrite` matches them.
`@nova:example.org` is found in `ask @nova:example.org.`, in `@nova:example.org:DEVICEID`, and in `@nova:example.org:12AB`, whose device ID starts with digits.
It is not found in `@nova:example.org.uk`, `@nova:example.org:8448`, or `@nova:example.org:1234`, which name other users or servers.
Digits after a `:` count as a port unless a letter follows them, so an all-digit suffix is never matched.
Worker keys and private-instance owners are compared as whole IDs too.
Config values are different: only a value that equals the old ID exactly is replaced.

The config edit is made on the text, so comments, quoting, and indentation survive.
The command parses the edited files again and refuses unless they differ from the originals only in those values.
It never edits mapping keys, `.env`, or other files.

Anything that goes wrong while rewriting the journal rolls the whole transaction back.
A row that would collide with a row already holding the new ID, in any table but `room_member_joins`, is found when the rewrite runs at the start of `--apply`, and makes the command refuse and name the table and column.

The old Docker worker container is removed last, after every other step succeeded.
If that fails, the command prints a warning and you remove the container by hand.

### What stays

These stay on purpose, and the plan lists them as `report:` lines.

- The old crypto store stays.
  It is the directory under `encryption_keys/` named after the old ID without the `@` and with each `:` replaced by `_`, for example `encryption_keys/mindroom_atlas_example.org/`.
  Only that store decrypts the old encrypted history, and the new device starts its own store.
  It cannot read encrypted history from before the rename.
- A desktop bridge pinned to the old ID stays pinned.
  Pair it again.
- Records of past tool jobs and scheduled runs outside the event journal keep the old ID as history.
- A worker directory that looks like it belonged to the old ID, but whose worker key cannot be found, stays in place and is reported with its path.

### New-ID state

When MindRoom ran under the new ID before its state was moved, it already wrote state there.
For the journal that means rows under both `<name>@<old ID>` and `<name>@<new ID>`.
Without `--discard-new-principal` the command refuses and names it, either both principals' tables with their row counts or the path that already exists.

With `--discard-new-principal`:

- The journal rows under `<name>@<new ID>` are deleted first, then the rows of `<name>@<old ID>` move in their place.
  When `matrix_state.yaml` already names the new ID and keeps its device (a `--from` run after a manual username change), the sync stream state of `<name>@<new ID>`, its `matrix_sync_consumers` row and `matrix_ingestion_membership` rows, is not deleted: that account keeps the device MindRoom already uses under the new ID, and the device continues that stream.
  Otherwise the next start logs in on a fresh device, and that state is deleted with the rest.
- A destination path that holds only OAuth stores without credentials, lock files, and empty directories is moved into the backup directory, then the old path moves in.

The flag only deletes journal rows while `<name>@<old ID>` still has rows.
Once it has none, the rows under `<name>@<new ID>` are the agent's history, moved there by an earlier run, so they are kept with or without the flag.
This is what makes a rerun after a killed run safe for the journal.
A `--from` without rows in the journal is refused before anything changes, unless it is the generated ID (see [After a manual username change](#after-a-manual-username-change)).

It still refuses, and changes nothing, when the new principal has rows in `reply_messages` or `matrix_delivery_outbox`, because those are replies MindRoom already sent under the new ID.
It also refuses when a destination holds credentials or any other file.
Handle those by hand, because deleting them could lose track of messages already in your rooms.
When the new-ID state holds nothing you need, move that path away yourself and run the command again.

## Checks before it changes anything

Every refusal exits 1 with the reason and leaves your files as they were.
Event journal collisions are the one refusal that only shows up while `--apply` is rewriting, as described in [Procedure](#procedure).

- The config exists and loads, `AGENT` is an agent in it, and `matrix_state.yaml` holds the account `agent_<name>`.
- The new ID is a valid Matrix user ID on the old ID's server, and is not another stored account.
- With `--from`, the old ID passes the same checks, differs from the new ID, and matches the stored account unless that already names the new ID.
- The journal backend and worker backend are supported.
- MindRoom is stopped: `Stop MindRoom first: another process holds <lock file>`.
- The login check passes (`--apply` only).
  The command logs in at `/_matrix/client/v3/login` on the configured homeserver as the new ID with the stored password, then logs out.
  A rejected login means the server rename has not happened, and the command refuses with `The server does not accept <new ID> with the stored password`.
- Event journal rows and paths under the new ID are dealt with as described in [New-ID state](#new-id-state).

The login check sends its requests straight to the configured homeserver, with the TLS settings MindRoom is configured to use, instead of going through MindRoom's Matrix client.
That keeps it from creating a crypto store for the new ID.
It creates a device named `mindroom rename check`.
If logging that device out fails, the command warns and you remove the device from the account by hand.
Pass `--skip-login-check` when the homeserver is unreachable from where you run the command, or when `matrix_state.yaml` holds no password for the agent.

## What it reports

With `--apply` the command prints, in order:

1. The plan: one line per step, then the `report:` lines.
2. `Adopted: agent <name> now uses <new ID>.`
3. `Backups:` and the path of the backup directory.
4. A `Warning:` line for each problem that does not undo the rename, such as an old Docker worker that could not be removed.
5. A leftover scan of the storage directory.

The leftover scan looks for the old ID in file and directory names, in JSON and YAML files, and in every text cell of the event journal.
It skips `encryption_keys`, the backup directory, and the package and cache directories of workers.
It lists what it found in two groups:

- `Reported above as staying` holds places the plan said it would leave, such as the desktop bridge pin.
- `Not reported by the plan, or not scanned` holds everything else, including files too large or unreadable to scan.

Read the second group with care.
It is where state the command did not know about shows up.

| Exit code | Meaning |
| --- | --- |
| `0` | Applied, nothing to do, or a dry run. |
| `1` | Refused, failed with every step undone, or you declined the confirmation. |
| `2` | A step failed and undoing the earlier steps failed too. |

## When a step fails

If a step fails, the command undoes the completed steps in reverse order.
It prints `Not applied:` with the failing step and exits 1.
The disk is back as it was, apart from the backup directory.

If an undo fails as well, the command prints `Undo failed:` and exits 2.
Do not start MindRoom.
Restore the remaining steps by hand from the manifest it names, as described below, or restore your snapshot.
Ctrl-C is ignored while the command undoes steps, because restoring a large journal takes a while.

If the command itself is killed during `--apply`, for example by `kill -9`, an out-of-memory kill, or a power loss, nothing is undone.
`matrix_state.yaml` is written after every other reversible step, so after a kill it still names the old ID and a rerun does not report `Already adopted`.
Do not start MindRoom until a rerun has finished or you have restored from the manifest or your snapshot.
Until then, part of the agent's state is under the new ID while `matrix_state.yaml` still names the old ID, which the homeserver no longer knows.
Restoring from the manifest as described below, or restoring your snapshot, and then running the command again is the safe way back.
Running the same command again directly also keeps the journal: once `<name>@<old ID>` has no rows, the rerun leaves the rows under `<name>@<new ID>` alone, with or without `--discard-new-principal`, and moves what the killed run had not moved yet.
It does not redo a rewrite inside a directory the killed run had already moved, such as the worker key of a moved OAuth store, so restore from the manifest if the kill may have come between a move and the rewrite after it.
After a killed `--from` run, the rerun with `--from` is refused unless the old ID is the generated one, because the journal no longer shows the old ID as the agent's; restore from the manifest then.

## Restoring from `agent-rename-backups`

Every apply writes a backup directory, even when it succeeds:

```text
<storage>/agent-rename-backups/<UTC timestamp>-rename-account/
```

For example `mindroom_data/agent-rename-backups/20261019T214558Z-rename-account/`.
The command never deletes it.
It can hold copies of credentials and the config, so it is readable by its owner only.
Delete it once you are sure of the result.

`manifest.json` lists every planned step in plan order, including steps that never ran, and is updated as steps run.
Each entry has a `type`, a `description`, a `source`, a `destination`, and a `backup` directory.
The `backup` directories are numbered `000`, `001`, and so on, and hold the originals.
Plain moves keep nothing there, because a move is only a rename.
The number is the entry's position in the manifest, counting from `000`, and the directory is `<backup dir>/NNN`.

The command rewrites the manifest after a step returns.
It is current after `Not applied:` and `Undo failed:`.
After a kill it lags by the step that was running, and that entry can say `backup: null` although the step took effect.
So decide by the numbered directory on disk, not by the `backup` field.

To restore by hand, stop MindRoom and walk the manifest from its last entry to its first.
Skip the entries that have nothing to restore:

- A `FinalAction` entry.
  It is only used with the Docker worker backend, to retire the old workers, and is never undone.
- A `SqliteEdit`, `FileRewrite`, or `DropPath` entry whose numbered directory does not exist, because that step never started.
- A `DropPath` entry whose numbered directory holds no item, because the drop never ran or was already undone.
- A `MovePath` entry, unless its `destination` exists and its `source` does not.
  Otherwise the move never happened or was already undone.
  With `--discard-new-principal`, the `destination` of a move that never ran can still exist, because the `DropPath` before it, which would have removed it, never ran either.
  Moving that `destination` back would put it inside `source`.

Restoring a `SqliteEdit` or `FileRewrite` entry that was already undone does no harm.
Neither does restoring one that never took effect, because `source` still holds the original.

One entry needs a check first: the last one with a numbered directory after a kill or a failed step.
The command may have stopped while it copied the original into that directory, for example because the disk was full.
A copy cut short must not replace a good `source`.
Check that the copy is whole before you use it.
For a database, `sqlite3 <copy> 'PRAGMA integrity_check'` must print `ok`, and `sqlite3 <copy> .tables` must list the tables, because an empty file also passes the first check.
Open a copied file and make sure it is complete.
If you cannot tell, restore your snapshot instead.

This matters most after `Undo failed:`, where the manifest includes steps the command never reached.
Restore each remaining entry like this:

| `type` | Restore |
| --- | --- |
| `MovePath` | Move `destination` back to `source`. |
| `SqliteEdit` | Delete `<source>-wal` and `<source>-shm` if they exist, then copy the database from `backup` over `source`. |
| `FileRewrite` | Copy the file from `backup` over `source`. |
| `DropPath` | Move the item from `backup` back to `source`. |

A `backup` directory holds the file under its original name.

Two things the backup does not cover.
A removed Docker worker container is not restored, and MindRoom creates a new one when the agent needs it.
The homeserver rename is not undone either, so after restoring the files MindRoom is back on the old ID while the server still knows the new one.
Restore the homeserver's snapshot too, or run the command again once the files and the server agree.

See also [Data Storage & Journal](storage.md) for the files this command touches and [CLI Reference](../cli.md#agents-rename-account) for its options.
