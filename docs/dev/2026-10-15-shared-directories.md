# Shared Directories Between Agents

Design record for letting an agent create a directory, invite other agents, and have them read it from their own dedicated workers.

## Goal

An agent on dedicated Docker or Kubernetes workers creates a directory, writes into it from its worker, and invites other agents, who read it from their own workers.
The worker boundary between agents must not weaken for anything else.

## Decisions and assumptions

- Invited agents only read; the owner is the single writer.
  The request asked for invitees that can read, and one writer avoids concurrent-edit semantics.
- A shared directory belongs to an agent, not to a requester, matching how a non-private agent's workspace is already shared by all of its requesters.
- Private agents can neither own nor join a directory, because their data is per requester while a member's worker may serve every requester.
- An invitation needs the requester's access to the invited agent, matching the rule that a mentioned entity acts for the requesting human.
- Membership changes recreate the affected workers, because neither Docker nor Kubernetes adds a mount to a running container.
- The tool reports the path the agent's code tools see, which differs between the primary and a dedicated worker because workers mount storage at their own mount path.

## Approaches considered

### Per-directory mounts from a primary-owned ledger (chosen)

Data lives at `shared_directories/<owner>/<name>/` and membership in a ledger below `tracking/`, and each dedicated worker mounts every directory its agents own (read-write) or joined (read-only), like read-only knowledge mounts.
Each directory is its own mount whose parent directories no dedicated worker can write, so per-directory access holds against worker code, and existing drift detection recreates workers when their mounts change.

### Directories declared in `config.yaml`

A `shared_directories:` section edited through the validated config write path would reuse hot reload and be operator-visible.
Rejected because agent config writes require a platform administrator and a human confirmation, so ordinary users could not share, and because runtime collaboration state does not belong in authored config.

### One common volume for all participating agents

A single mounted `shared/` directory avoids restarts after the first mount.
Rejected because it has no per-directory access, so an invitation would mean nothing.

### Rejected variants

- **Directory inside the owner's workspace.**
  Worker code writes that workspace, so it could swap the folder for a link between the primary's check and the container runtime's mount; Kubernetes `subPath` resolution and Docker bind sources both follow links within reach, which would mount credentials or another agent's workspace into a member.
- **Owner mounts all of `shared_directories/<owner>/` writable to avoid a restart on create.**
  The same link swap at `<name>` before a member's worker starts.
- **Mount propagation into running workers.**
  Kubernetes supports it only for `hostPath` volumes and Docker only with host-side mounting, neither of which the worker backends use.
- **Mount targets nested inside each member's workspace.**
  Primary-process tools would see an empty placeholder at that path instead of the files, and placeholders would accumulate in workspaces.
- **Teaching every `file` and `coding` helper about several roots.**
  A path inside a shared directory is instead handed to a copy of the toolkit rooted there, so the existing single-root confinement applies unchanged.

## Constraints

- The `static_runner` sidecar can write `shared_directories/`, so it mounts every directory and keeps none apart, consistent with it already seeing every agent's state; the charts never combine it with dedicated workers, whose mount sources it could otherwise redirect.
- In a worker, the share roots `file` and `coding` accept are the directories mounted there, because the worker cannot read the primary-only ledger.

## Limitations and open questions

- A removed member's running worker still reads the directory until its next tool call recreates it.
- Directories of an agent removed from config stay on disk and in the ledger, and nothing cleans them up.
- Write access for members, teams as members, private agents, quotas, and an acceptance step for invitations are not implemented.
- Primary-process path tools such as `attachments` cannot read shared directories, so agents copy files into their workspace first.
