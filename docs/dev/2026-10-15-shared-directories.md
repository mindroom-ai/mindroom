# Shared Directories Between Agents

Design record for letting an agent create a directory, invite other agents, and have the invited agents read it from their own dedicated workers.

## Goal

An agent running on dedicated Docker or Kubernetes workers can call a tool to create a shared directory, write into it from its worker, and invite other agents.
Each invited agent can then read that directory from its own worker with `shell`, `python`, `file`, and `coding`, without the worker boundary between agents being weakened for anything else.

Success means:

- Agent A creates `report`, writes `report/summary.md` from its worker, and invites B.
- B reads `summary.md` from its own worker with `shell` and with `file`.
- B cannot write into the directory, and no agent that was not invited can see it.
- A can remove B again and delete the directory.

## Stated requirements and assumptions

Stated by the user:

- The feature targets Kubernetes and dedicated worker container deployments.
- The agent creates the directory and invites other agents through a tool call.
- Invited agents can read from the directory.

Assumed, and open to correction:

- Invited agents get read-only access; only the owning agent writes.
  The user asked for invitees that "can read", and read-only keeps one writer per directory.
- A shared directory belongs to an agent, not to a requester, matching how a non-private agent's workspace is already shared by all of its requesters.
- Private agents can neither own nor join a shared directory, because their data is per requester while a member's worker may serve every requester.
- Any requester who may use the owning agent may create directories, and an invitation additionally requires that the requester may use the invited agent.
- Membership changes may restart the affected workers, because no supported container runtime adds a mount to a running container.

## Approaches considered

### A. Per-directory mounts from a primary-owned ledger (chosen)

The primary stores each directory under `shared_directories/<owner>/<name>/` and its membership in a ledger below `tracking/`.
Dedicated worker backends add one mount per directory an addressed agent owns (read-write) or belongs to (read-only), the same way they already add read-only knowledge mounts.

Chosen because each directory is a separate mount whose parents no worker can write, so per-directory access holds even against worker code, and because it reuses the existing mount planning, drift detection, and recreation paths.

### B. Shared directories declared in `config.yaml`

A `shared_directories:` section, edited by a tool through the validated config write path, would reuse hot reload and be visible to operators.
Rejected because config writes from agents require a platform administrator and a human confirmation (`self_config`), so ordinary users could not share, and because runtime collaboration state does not belong in authored config.

### C. One common shared volume for all opted-in agents

Mounting a single `shared/` directory into every participating worker avoids restarts after the first mount.
Rejected because it has no per-directory access control, so "invite" would mean nothing.

### Rejected variants of A

- **Directory inside the owner's workspace, mounted into members.**
  Worker code writes the owner's workspace, so it could replace the shared folder with a link between the primary's check and the container runtime's mount; Kubernetes `subPath` and Docker bind sources both follow links, which would mount credentials or another agent's workspace into a member.
- **Owner mounts its whole `shared_directories/<owner>/` writable to avoid an owner restart on create.**
  Same link attack: the owner could swap `<name>` for a link before a member's worker starts.
- **Mount propagation to add mounts to running workers.**
  Kubernetes supports it only for `hostPath` volumes and Docker only with host-side mounting, neither of which the worker backends use.
- **Mount target nested inside each member's workspace.**
  Primary-process tools would see an empty placeholder at that path instead of the shared files, and placeholders would accumulate in workspaces.

## Design

### Storage

- Data: `<storage_root>/shared_directories/<owner>/<name>/`, created by the primary as a real directory without following links.
  `shared_directories/` and `shared_directories/<owner>/` are never mounted into any dedicated worker, so worker code cannot replace a directory a member mounts.
- Ledger: `<storage_root>/tracking/shared_directories.json`, one record per `<owner>/<name>` with `owner`, `name`, `members`, and `created_at`.
  It is written with the existing `durable_write` record helpers under a primary-process lock; `tracking/` is primary-only, so worker code cannot forge membership.
- Names match `^[a-z0-9][a-z0-9_-]{0,63}$`.

### Tool

A new primary-only toolkit `shared_directories` with file access class `none`:

| Function | Who | Effect |
|---|---|---|
| `create_shared_directory(name)` | Owner | Creates the directory and ledger record; returns the path the owner's code tools see |
| `invite_to_shared_directory(name, agent_name)` | Owner | Adds a read-only member |
| `remove_from_shared_directory(name, agent_name)` | Owner | Removes a member |
| `delete_shared_directory(name)` | Owner | Removes the record, then the directory tree without following links |
| `list_shared_directories()` | Any | Lists directories the agent owns or belongs to, with paths, owners, members, and access |

The owner is the calling agent from the tool runtime context.
`invite_to_shared_directory` refuses a target that is not a configured agent, is the owner, is private, or that the requester may not use; like `stop_reply`, it acts only on behalf of an access-checked requester.
Every mutating result tells the model which agents' workers restart on their next tool call.

### Worker mounts

A pure planner in `src/mindroom/shared_directories/` returns, for one worker key, the mounts of every directory owned by or shared with an agent that key addresses, using the same agent addressing as knowledge mounts.
Owned directories mount read-write and member directories read-only; a `user` worker addressing both the owner and a member mounts read-write.
Each source is opened with `open_directory_within_root` before planning, and missing or linked sources are skipped with a log line, as for workspaces.

The Docker backend adds the planned mounts to its storage mount specs, and the Kubernetes backend adds `subPath` mounts of the storage volume.
Both backends' ready-worker caches compare the current plan on reuse, so a ledger change takes effect on the next tool call by recreating the worker through the existing drift path.

The `static_runner` sidecar mounts `shared_directories` read-write so shares work there, without per-directory isolation, consistent with the sidecar already seeing every agent's state.
The local backend uses the primary filesystem directly.

### Path tools

Under `file_access: workspace`, `file` and `coding` also accept paths inside the agent's shared directories: owned ones read-write and member ones read-only.
In the primary the set comes from the ledger; a worker-routed call receives the same set from the primary, and the worker accepts only entries below its `shared_directories` mount that it can open as real directories.
Primary-process path tools such as `attachments` and `matrix_message` stay workspace-only, so an agent copies a shared file into its workspace before sending it.

### Discovery

The tool execution environment description lists each shared directory the agent can reach, with its path, owner, and access, so an invited agent learns about it without calling `list_shared_directories`.

### Lifecycle

- Create, invite, remove, and delete take effect for each affected agent on that agent's next tool call, which recreates its worker and ends its background shells, tmux sessions, and computer sessions.
- Until then, a removed member's running worker still sees the directory.
- Directories of an agent removed from config stay on disk and in the ledger, and no worker mounts them.

## Security analysis

Checked against `docs/architecture/security-posture.md`:

- **Worker to primary:** the primary never trusts share contents; path tools open them through the existing no-follow confinement, and deletion uses the no-follow tree removal from worker retirement.
- **Worker to other agents:** a member's mount is read-only, and links the owner plants inside a share resolve inside the member's own container.
  Mount sources sit below primary-only directories, so neither Docker bind sources nor Kubernetes `subPath` resolution can be redirected by worker code.
- **Requester to agent:** an invitation needs the requester's access to the invited agent, matching the rule that a mentioned entity acts for the requesting human.
  Sharing makes the owner's directory readable by the invited agent's other requesters, which the requester could already do by relaying contents in chat; this becomes a listed intentional behavior.
- A requester who may use both agents can restart the invited agent's worker by repeated invitations; this becomes a listed intentional behavior.
- A `user` worker mounts the shares of every agent it addresses, consistent with its existing shared-workstation semantics.

`security-posture.md` gains the new primary-written location, the sidecar mount, and the intentional behaviors above.

## Testing

- Ledger and authorization tests at the toolkit seam: owner-only mutation, invite refusals (unknown, self, private, requester without access, non-human requester), idempotent invite and remove, delete through a planted link.
- Planner tests per worker scope: unset, `shared`, `user` with owner and member in one worker, `user_agent`, private agents ignored, missing and linked sources skipped.
- Docker volume specs and Kubernetes pod template mounts with correct `ro`/`readOnly`, and ready-cache invalidation after a ledger change.
- `file` and `coding` confinement: member share readable, write refused, non-member share refused.
- Live test on Docker dedicated workers: A creates, writes, and invites B; B reads with `shell` and `file`; B's write fails; removal hides the directory after B's next call.

## Out of scope

- Write access for invited agents.
- Private agents and teams as owners or members.
- Invitation acceptance by the invited agent.
- Quotas on directory count, members, or size.
- Primary-process path tools reading shared directories.
- Cleanup of directories whose owner left the config.
