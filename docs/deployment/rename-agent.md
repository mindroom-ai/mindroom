---
icon: lucide/tag
---

# Renaming an Agent

`mindroom agents rename OLD NEW` renames an agent's key in `config.yaml` and carries over everything MindRoom keeps under that name.
The agent keeps its Matrix account, its Matrix ID, its device, and its rooms.
Only the name MindRoom uses for it changes, for example from `atlas` to `nova`.

MindRoom keeps sessions, memory, workers, credentials, tracking records, and journal rows under the agent's name.
If you only edit the key in `config.yaml`, MindRoom looks for all of them under the new name and finds nothing.
The agent would start without its history, memory, or workspace.
The command moves them, rewrites what holds the old name, and tells you what it left alone.

## When to use it

Use it when you want a different agent key and the same Matrix account.

Use [`rename-account`](rename-agent-account.md) when the Matrix ID changes and the key stays.

| You want to change | Run |
| --- | --- |
| The agent's key, keeping its Matrix ID | `mindroom agents rename OLD NEW` |
| The agent's Matrix ID after the homeserver renamed the account, keeping its key | `mindroom agents rename-account` ([guide](rename-agent-account.md)) |
| Both | Rename the account on the homeserver, run `rename-account` with the old key, then run `rename`. |

The command keeps whatever Matrix ID `matrix_state.yaml` holds for the agent.
That is why a Matrix rename goes first: the new ID then travels with the agent to its new key.

The command renames a configured agent.
It refuses the router, teams, and private agents, see [Refusals and limits](#refusals-and-limits).
It supports the SQLite event journal and the `static_runner` or `docker` worker backend.
Any other setup is refused: Postgres and Kubernetes with `not supported yet`, and an unknown `MINDROOM_WORKER_BACKEND` with `Unsupported worker backend: <value>`.

`NEW` must follow the rules for agent names in the config:

- It uses letters, digits, and `_` only.
- It is not `router`, `user`, or `_shared`.
- It is not the name of another agent or team.
- It does not collide after normalization.
  Worker keys write a name without leading and trailing `_`.
  Agno ids lowercase a name, turn `_` into `-`, and split camel case, so `MyAgent` and `my_agent` both become `my-agent`.
  The worker key check includes the agent's own old name, so `nova` cannot become `_nova`.
  The Agno id check skips the agent being renamed, so it only refuses a new name whose id another agent already has.
  With agents `nova` and `crew`, renaming `crew` to `Nova` is refused.
  Colliding names would share workers or confuse team runs.

## Procedure

1. Stop MindRoom, and keep it from starting by itself until step 5.
   Disable its systemd unit (`systemctl disable --now <unit>`) or set its container's restart policy to `no` (`docker update --restart=no <container>`).
   If the unit comes from a NixOS configuration, also pause automatic deployments such as comin, which would start it again, and do not reboot.
   A MindRoom that starts after a killed run finds the agent half-renamed, see [Killed runs and reruns](#killed-runs-and-reruns).
2. Take a snapshot or copy of the storage directory and the config directory.
   Include `MINDROOM_SESSION_STORAGE_PATH` and `MINDROOM_CONTROL_STATE_PATH` when you set them to paths outside the storage directory.
   The command keeps its own backups, but a snapshot is the simplest rollback.
3. Run a dry run as the OS user the service runs as, with the service's environment and the same `--config` and `--storage-path`.
   Variables such as `MINDROOM_STORAGE_PATH`, `MINDROOM_CONFIG_PATH`, `MINDROOM_SESSION_STORAGE_PATH`, `MINDROOM_CONTROL_STATE_PATH`, and `MINDROOM_WORKER_BACKEND` decide which files the command reads and whether it retires a Docker worker.
   For a systemd service, `systemctl show <unit> -p User -p Environment -p EnvironmentFiles` lists them.
   A file the command rewrites keeps its mode but becomes owned by whoever ran the command.
   A memory store it edits is copied, so the copy is owned by that user too.
   A run as root can leave files the service can no longer write.
   The dry run prints the plan and changes nothing.
   The one exception is a config that still uses a retired access field.
   Loading it migrates it on disk, as every MindRoom command does, see the [account guide](rename-agent-account.md#procedure).

   ```bash
   mindroom agents rename atlas nova \
     --config config.yaml --storage-path mindroom_data
   ```

4. Read the plan, then apply it.
   The command asks `Apply this plan?` unless you pass `--yes`.

   ```bash
   mindroom agents rename atlas nova \
     --config config.yaml --storage-path mindroom_data --apply
   ```

5. Turn auto-start back on and start MindRoom.
   The agent syncs with the same device and access token as before.
   Ask it something in a room it was in, and check that it remembers an earlier conversation.

A dry run takes no locks and does not check that MindRoom is stopped.
`--apply` takes the locks a running MindRoom holds, so it refuses to run while MindRoom is up and blocks MindRoom from starting until it exits.
It then plans again under the locks, prints the plan, and asks for confirmation.

Both the dry run and the apply read the event journal, so both refuse a journal that already holds rows for `NEW`.
The apply checks again inside its transaction.

The plan starts with `Plan to rename agent OLD to NEW; it keeps its Matrix account <ID> and device:`.
It then lists one line per step, followed by `report:` lines for what the command leaves alone.
An agent that never logged in has no stored account, and the header says so.

After `--apply` the command prints, in order:

1. The plan.
2. `Renamed: agent OLD is now NEW.`
3. `Backups:` and the path of the backup directory.
4. A `Warning:` line for each problem that does not undo the rename, such as an old Docker worker that could not be removed.
5. A leftover scan of the storage directory, see [Leftover scan](#leftover-scan).

Steps run in this order: the event journal, the state directory with its session and learning databases, the symlinks into it, memory, teams, tracking and side stores, credentials, workers, derived indexes, `matrix_state.yaml`, `.env`, and `config.yaml` last.
Every move runs before the edits to what it moved.
The config goes last on purpose, because a run that stops early then still has the old name configured, see [Killed runs and reruns](#killed-runs-and-reruns).

| Exit code | Meaning |
| --- | --- |
| `0` | Applied or a dry run. |
| `1` | Refused, failed with every step undone, or you declined the confirmation. |
| `2` | A step failed and undoing the earlier steps failed too. |

Running the same command again after it succeeded exits 1 with `OLD is not a configured agent`, because the config names the agent `NEW` now.

## What changes

### Config and `.env`

- In `config.yaml` and every YAML file it includes, the command renames these:
  - The key `agents.<OLD>`.
  - The key `calls.agents.<OLD>`.
  - Items equal to `<OLD>` in `teams.*.agents` and `agents.*.delegate_to`.
  - The value of `personal_rooms.agent`.
- A string value that is a path into the agent's state directory, `<storage>/agents/<OLD>`, gets that one path component renamed.
  The path may be absolute, start with `~`, be relative to the config directory, or start with `${MINDROOM_STORAGE_PATH}`.
- The edit is made on the text, so comments, quoting, and indentation survive.
  The command parses the edited files again and refuses unless they differ from the originals only in those values.
- `MINDROOM_CONNECTIONS_AGENT` in the `.env` next to the config changes when its value is exactly `OLD`.
  Every other line stays.
- Prose is never edited.
  Other agents' instructions that mention the old name stay as they are, and the plan reports them.

### `matrix_state.yaml`

- The account key `agent_<OLD>` becomes `agent_<NEW>` and keeps its place among the other accounts.
- Its username, password, device ID, and access token stay, so the agent keeps its Matrix ID and device.
- An agent without a stored account has nothing to move.

### State directory, sessions, learning, and memory

- `agents/<OLD>/` moves to `agents/<NEW>/` as a whole.
  That holds the workspace, file-backed memory, the memory store, and the learning and session databases.
  With `MINDROOM_SESSION_STORAGE_PATH` set, the agent's directory in that root moves too.
  An agent that never ran has no directory and gets no step.
- Symlinks in the storage directory or directly in the config directory that point into `agents/<OLD>`, such as `agents/other -> <OLD>` for an agent sharing the directory, are retargeted to `agents/<NEW>`, keeping relative links relative.
- `sessions/<OLD>.db` becomes `<NEW>.db`.
  In one transaction the command renames the tables `<OLD>_sessions` and `<OLD>_sessions_*` and recreates their indexes under the new names.
  It updates the schema version rows, sets the agent ID of every session and run to the new name, and renames the history summary scope `agent:<OLD>`.
  MindRoom only loads runs whose agent ID is the agent's name, so this is what keeps the old history visible.
- `learning/<OLD>.db` becomes `<NEW>.db` the same way, including the table `<OLD>_learning_sessions` and the agent ID in `agno_learnings`.
- Memories tagged `agent_<OLD>` in the Chroma store get the tag `agent_<NEW>`.
  Team memories tagged `team_<members>` with the agent among the members get the member list sorted again with the new name.
  A team's memory sits in the store of every member, so the command edits the store of every configured agent.
  It edits through the Chroma client, which keeps Chroma's full-text index in step, and also renames the agent's own `agent` and `team_members` fields in those memories.
  Chroma stores are copied twice while they are edited, so leave free disk space for that.
- File-backed memory scope directories `memory_files/agent_<OLD>` and the team scope directories with the agent in them get the new name.

### Teams

- A configured team with the agent as a member keeps its name and its store.
  The runs the agent wrote there as a member, and the requirements it raised, take the new name.
- An ad-hoc team scope lists its members, `team_<members sorted, joined by +>`, and adds a requester suffix when a member is private.
  The store of a scope that lists the agent moves to the scope ID of the new member list, sorted again, with the suffix kept.
  Its tables, team IDs, history scope keys, and member runs are renamed too.

### Tracking and side stores

These stores spell the name as a path component, a mapping key, or a field value.
The command matches the exact name only, so renaming `code` leaves `code_team` alone.

- **Moved:**
  - `tracking/agents/<OLD>` and `tracking/automations/<OLD>`.
  - `sync_continuity/<OLD>.json` and its lock file.
  - `calls/<OLD>`, the call transcripts of an agent without file memory.
  - The agent's dynamic workflows.
  - The agent's avatar.
  - `shared_directories/<OLD>`.
- **Rewritten in place:**
  - Conversation modes, thread model choices (`model:<OLD>`), and memory auto-flush progress.
  - Shared directory records, as owner and as member.
  - Todos assigned to the agent and the todo poke history.
  - Dynamic workflow summaries, revisions, runs, and the public report records that point at them.
  - Delegation records and the delegation exports in workspaces.
  - External triggers the agent created or targets.
  - Desktop pairings and background script runs, in their SQLite databases.
  - The MCP gateway's agent selections.
- **Dropped into the backup, because they are bound to the old name:**
  - Subagent handles that name the agent. Start a new subagent.
  - Matrix send intents of the agent.
  - Pending OAuth sign-in states of the agent. The flow starts over.

### Workers and credentials

- Every worker whose key names the agent moves to the directory of its new key.
  That covers `shared`, `unscoped`, and `user_agent` keys.
  The command finds them in worker metadata, in Docker control records, and in the OAuth stores bound to them.
- Inside a moved worker, the metadata and the worker's OAuth stores get the new key.
  A Docker worker's mount point `agents/<OLD>` moves to the new name.
- The worker's `venv/` moves into the backup, because the scripts in it name the old path.
  MindRoom builds a new one on the worker's next use, and packages installed into the old one must be installed again.
- A Docker worker's startup manifest, `.runtime/startup_manifest.json`, names the old key, so it moves into the backup.
  MindRoom writes a new one when it creates the worker's container.
- Docker control records and config projections of the old key are removed.
  With the Docker worker backend the old container is removed last, after every other step succeeded.
  If that fails, the command prints a warning and you remove the container by hand.
- The agent's OAuth credentials in `private_oauth/_agents/` and in every requester's directory under `private_oauth/` move to the new name.
  Each OAuth store gets the new worker key and the new agent name in its binding.

### Event journal

All of this happens in one transaction in `tracking/event_journal.db`.

- The principal `<OLD>@<Matrix ID>` becomes `<NEW>@<Matrix ID>` in every table that has one.
  That includes the sync stream state, `matrix_sync_consumers` and `matrix_ingestion_membership`.
  `rename-account` deletes that state, because it gives the agent a new device.
  Here the device stays, so its stream continues and the state is kept.
- Turn records move to the new name, so a turn handled under the old name still counts as handled.
- The columns that hold the agent's name are renamed, such as the entity of a reply and the agent of a scheduled or paused call.
- The JSON fields that hold the name are rewritten.
  That includes the documents of paused approvals and background jobs and the team documents of replies the agent took part in.
  An ad-hoc team scope that lists the agent gets the scope ID of the new member list.
  Configured teams keep their names.
- The agent's approval grants are deleted.
  A grant's scope key hashes the agent's name, so it could never match again, and the agent asks for approval again.

The command refuses when the journal already has rows for `NEW` under any Matrix ID, or turn records for `NEW`, or an ad-hoc team scope that lists both names.
Anything that goes wrong while rewriting the journal rolls the whole transaction back.

### Knowledge Git checkouts and the handled-turn ledger

- A Git-backed knowledge base whose checkout sits inside the agent's state directory has a Git directory under `knowledge_git/`.
  Its name derives from the checkout's absolute path, so the move would orphan it.
  The command moves it to the name of the new path.
  Without that, Git sync would refuse to clone into the moved checkout, because it is not empty.
- `tracking/<OLD>_responded.json` is the handled-turn ledger from before the event journal.
  When MindRoom has not imported it yet, the command moves it to `<NEW>_responded.json`, the name the startup import reads.
  An imported ledger stays and is reported as a legacy file.

## What is dropped and rebuilt

Some state can be derived again, so the command drops it into the backup instead of renaming it.

| What | Why | After the rename |
| --- | --- | --- |
| The file-memory search index of the agent, `knowledge_db/file_memory_agent_<OLD>_*` | It carries the old name, so nothing reads it again. | MindRoom rebuilds it under the new name on next use. |
| The `venv/` of each moved worker | Its scripts name the old path. | MindRoom builds a new one on the worker's next use. |
| Docker records of the old worker keys, and the startup manifest of each moved Docker worker | They belong to the old key. | MindRoom creates new ones. |
| Approval grants, subagent handles, Matrix send intents, and pending OAuth states of the agent | They are bound to the old name. | The agent asks again, or the flow starts over. |

## What it reports and does not change

The plan prints `report:` lines for state it leaves alone.
Read them before you start MindRoom.

- **Knowledge graph.**
  A graph is named after its agent's state directory, so `NEW` starts a new graph.
  The old one stays in the graph store unused.
  The new graph begins with the conversations active within `knowledge_graph.ingest.backfill_days` of its first start.
  To cover older conversations, raise `backfill_days` before starting MindRoom.
  Raise it later together with `knowledge_graph.generation`, which starts every graph over.
- **Plugin state.**
  Paths under `plugins/` named after the agent stay, because each plugin owns its own format.
  Fix them by hand or with the plugin.
- **Scheduled tasks.**
  They are Matrix room state, which the command never reads or changes.
  A task whose message mentions `@OLD` keeps that text, so edit such tasks.
  The plan always prints this line, because listing the tasks would need Matrix access.
- **Other agents' instructions.**
  Config lines that mention the old name stay, with the file and the line numbers.
  Edit them by hand.
- **Paused delegations.**
  A delegation that had not finished keeps the old name in its saved run, so it cannot resume.
  The plan lists the sessions, and you ask again there.
- **Legacy files.**
  Files that older MindRoom versions kept under the name stay: dispatch obligations, session databases directly under the storage directory, the old memory search index, and an imported handled-turn ledger.
- **The mem0 history database.**
  It is outside the storage directory, at `~/.mem0/history.db` or in `MEM0_DIR`.
  It keeps recent messages under the old memory scope, so memory extraction for the new name starts without their context.
- **Private leftovers.**
  The state of a former private agent under `private_instances/` stays.
- **A few more.**
  The plan reports links of dynamic workflow reports already sent, which stop working.
  It lists background script runs that had not finished, which you start again.
  It lists config values that point inside the agent's shared directories.
  It lists delegation exports in private workspaces, which MindRoom never rewrites.
  It lists worker directories that look like the agent's but whose key nothing records.
  It notes that memories which cite call transcripts keep citing the old path.

### Leftover scan

The scan looks for the old name in file and directory names, in the strings and keys of JSON and YAML files, and in the text cells of the event journal.
It matches the whole name and its known spellings, such as `agent_<OLD>`, so it never counts `code_team` as `code`.
It skips `encryption_keys`, the backup directory, and the package and cache directories of workers.
It also lists dangling symlinks that point into `agents/<OLD>`, because MindRoom would create that directory again through them.
It lists what it found in two groups:

- `Reported above as staying` holds places the plan said it would leave.
- `Not reported by the plan, or not scanned` holds everything else, including files too large or unreadable to scan.

Read the second group with care.
It is where state the command did not know about shows up.

## Refusals and limits

Every refusal exits 1 with the reason, unless undoing the completed steps fails too.
Then the command exits 2 with `Undo failed:`, see [When a step fails](#when-a-step-fails).
A refusal in a dry run, or before `--apply` takes its locks, changes nothing.
A refusal after that leaves the lock files in `tracking/`, and one raised while a step runs also leaves the backup directory.
The completed steps are undone first.

- **Not supported.**
  - Teams: `<name> is a team; renaming teams is not supported yet.`
  - Private agents, those with `agents.<OLD>.private` set: `renaming private agents is not supported yet`.
  - The router: `router is MindRoom's router, which cannot be renamed.`
  - An event journal on Postgres: `The event_journal.backend 'postgres' is not supported yet`.
  - The Kubernetes worker backend: `The worker backend 'kubernetes' is not supported yet`.
  - An unknown `MINDROOM_WORKER_BACKEND`: `Unsupported worker backend: <value>`.
- **A bad `OLD`.** It must be a configured agent in the config.
- **A bad `NEW`.** It must follow the rules in [When to use it](#when-to-use-it).
  The command also refuses when `matrix_state.yaml` already holds an account `agent_<NEW>` or when the journal already has rows for `NEW`.
  It refuses when any path or record it must create under the new name already exists, and names it.
- **MindRoom is running.** The message reads `Stop MindRoom first: another process holds <lock file>`.
- **A file it cannot write.**
  The command edits `matrix_state.yaml`, `.env` and the config files after every store has moved, so it checks first that you can write each of them and its directory.
  A failure there would undo the whole rename.
  The message reads `Cannot write <file>, which the rename edits.` or `Cannot write in <directory>, where the rename replaces <file>.`
  A config that is a symlink into a read-only store, such as the Nix store, is refused this way.
- **A config edit it cannot make safely.**
  The message tells you to change it by hand, or says the edit could not be verified.
  - A value to change is a block scalar, an alias or include shares it with a place that does not name the agent, or an alias uses it as both a key and a value: `The value <value> in <file> (line N) ...; edit it by hand.`
  - The value of `MINDROOM_CONNECTIONS_AGENT` in `.env` uses escapes: `... uses escapes; edit it by hand.`
  - A file read with `!include_dir_named` is named after the agent: `The agent <OLD> is named by the file <path>; rename the file and the agent by hand.`
  - A YAML file the config includes cannot be placed in the config: `Cannot tell which part of the config <file> holds; rename the agent <OLD> in it by hand.`
  - The edited text does not parse back to the original apart from the changed values: `Config edit could not be verified in <path>`.
- **A store it cannot read.** The message names the file and the error.
- **A journal with the agent's principal but no stored account.**
  Restore the account `agent_<OLD>` in `matrix_state.yaml` first.

Three limits are not refusals:

- A config value that points into the agent's state directory is renamed only when no whitespace follows `agents/<OLD>`.
  A path such as `agents/atlas/my notes` stays as it is.
  The config-line report lists it, so fix it by hand.
- The command does not edit the text of workspace files, prompts, or memories.
  It only rewrites the delegation exports in workspaces and, in memories, the scope tags and the `agent` and `team_members` fields.
  Text that mentions the old name stays.
- The state directory `agents/<OLD>/` moves as a whole, so whatever inside it records its own absolute path still points at the old one.
  Examples are a virtual environment the agent created in its workspace, whose scripts name the old path, and Git worktrees, whose `.git` files and the main repository's `worktrees/*/gitdir` files hold absolute paths.
  To find them after the rename, run `grep -rlI -- '/agents/<OLD>/' <storage>/agents/<NEW>` for files that name the old path.
  Create a virtual environment again, and run `git worktree repair <worktree>...` in the main repository with the new path of each worktree.
  Symlinks into the directory are retargeted only in the storage directory and at the top of the config directory; point any others at the new path by hand.

## When a step fails

If a step fails, the command undoes the completed steps in reverse order.
It prints `Not applied:` with the failing step and exits 1.
The disk is back as it was, apart from the backup directory.

If an undo fails as well, the command prints `Undo failed:` and exits 2.
Do not start MindRoom.
Restore the remaining steps by hand from the manifest it names, as described below, or restore your snapshot.
Ctrl-C is ignored while the command undoes steps.

## Killed runs and reruns

If the command itself is killed during `--apply`, for example by `kill -9`, an out-of-memory kill, or a power loss, nothing is undone.
The config is edited last, so a run killed before that edit still has `OLD` configured.

Do not start MindRoom until you have restored from the manifest or your snapshot.
A half-renamed install still has `OLD` configured, while its journal principal, sync state, and turn records may already be `NEW`'s.
MindRoom then starts `OLD` on a fresh sync stream and can answer messages it already handled.
Once `matrix_state.yaml` was edited, it holds no account for `OLD`.
If the agent's Matrix ID is not the one MindRoom generates for `OLD`, MindRoom takes the agent's account for a retired bot and kicks it from its rooms.

Running the same rename again after a kill refuses.
It finds state under the new name that the killed run left, and it prints `Agent OLD is still configured, but ... so an earlier rename to NEW stopped before it finished.`
It names what it found and the manifest of that run.
The same applies when a destination of a move already exists.
When the killed run left nothing under the new name, the rerun plans as usual.

A kill after the config edit leaves only the removal of the old Docker worker undone.
`OLD` is no longer configured then, so the command has nothing left to do, and you remove the old container by hand.

The command only points at the manifest of the same rename, from `OLD` to `NEW`.
Backups of other renames are not offered, because restoring one would undo that rename.
Without a manifest of this rename, the state under the new name belongs to someone else, and the usual refusals name it.

To go on, restore from the manifest as described below, or restore your snapshot, and run the command again.
The command does not try to finish a half-done rename, because that could mix the old and new names in one store.

## Restoring from `agent-rename-backups`

Every apply writes a backup directory, even when it succeeds:

```text
<storage>/agent-rename-backups/<UTC timestamp>-rename/
```

For example `mindroom_data/agent-rename-backups/20261020T101500Z-rename/`.
The command never deletes it.
It can hold copies of credentials and the config, so it is readable by its owner only.
Delete it once you are sure of the result.

`manifest.json` lists every planned step in plan order, including steps that never ran, and is updated as steps run.
Each entry has a `type`, a `description`, a `source`, a `destination`, and a `backup` directory.
A `RetargetLink` entry also has `old_target` and `new_target`, the link's text before and after.
The `backup` directories are numbered `000`, `001`, and so on, and hold the originals.
Plain moves and link retargets keep nothing there, because the manifest alone can reverse them.
The number is the entry's position in the manifest, counting from `000`, and the directory is `<backup dir>/NNN`.
For an entry that edits a file after an earlier move, the `source` is where the file sits after that move.

The command rewrites the manifest after a step returns.
It is current after `Not applied:` and `Undo failed:`.
After a kill it lags by the step that was running, and that entry can say `backup: null` although the step took effect.
So decide by the numbered directory on disk, not by the `backup` field.

To restore by hand, stop MindRoom and walk the manifest from its last entry to its first.
Skip the entries that have nothing to restore:

- A `FinalAction` entry.
  It retires the old Docker worker and is never undone.
- A `SqliteEdit`, `FileRewrite`, `DirectoryEdit`, or `DropPath` entry whose numbered directory does not exist, because that step never started.
- A `DropPath` entry whose numbered directory holds no item, because the drop never ran or was already undone.
- A `MovePath` entry, unless its `destination` exists and its `source` does not.
  Otherwise the move never happened or was already undone.
- A `RetargetLink` entry, unless the link at `source` points at `new_target` (check with `readlink <source>`).
  Otherwise the retarget never happened or was already undone.

Restoring a `SqliteEdit`, `FileRewrite`, or `DirectoryEdit` entry that was already undone does no harm.
Neither does restoring one that never took effect, because `source` still holds the original.

One entry needs a check first: the last one with a numbered directory after a kill or a failed step.
The command may have stopped while it copied the original into that directory, for example because the disk was full.
A copy cut short must not replace a good `source`.
Check that the copy is whole before you use it.
For a database, `sqlite3 <copy> 'PRAGMA integrity_check'` must print `ok`, and `sqlite3 <copy> .tables` must list the tables, because an empty file also passes the first check.
For a text file, open the copy and check that it ends where a whole one would, for example that JSON or YAML still parses.
For a directory, such as the memory store a `DirectoryEdit` copies, compare the copy's file list with the directory at `source`, for example with `diff <(cd <copy> && find . | sort) <(cd <source> && find . | sort)`.
A file missing from the copy means the copy was cut short.
The database in it must pass the database check too, for a memory store `<copy>/chroma.sqlite3`.
If you cannot tell, restore your snapshot instead.

Restore each remaining entry like this:

| `type` | Restore |
| --- | --- |
| `MovePath` | Move `destination` back to `source`. |
| `SqliteEdit` | Delete `<source>-wal` and `<source>-shm` if they exist, then copy the database from `backup` over `source`. |
| `FileRewrite` | Copy the file from `backup` over `source`. |
| `DirectoryEdit` | Replace the directory at `source` with the copy in `backup`. |
| `DropPath` | Move the item from `backup` back to `source`. |
| `RetargetLink` | Replace the link at `source` with one pointing at `old_target`: `ln -sfn <old_target> <source>`. |

A `backup` directory holds the file or directory under its original name.

The backup does not cover a removed Docker worker container.
MindRoom creates a new one when the agent needs it.

See also [Data Storage & Journal](storage.md) for the files this command touches, [Renaming an Agent's Matrix Account](rename-agent-account.md) for changing the Matrix ID, and [CLI Reference](../cli.md#agents-rename) for its options.
