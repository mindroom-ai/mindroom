# Named workers

A shell call that sets `worker="<name>"` runs in a worker of its own beside the agent's normal worker, so one reply or one shell script spreads commands over several pods without a model turn for each.

## Problem

The only ways to get another pod were a subagent with `dedicated_worker=True`, which costs a model turn for each pod, and a background script, which is Python only, takes no arguments, and outlives the reply that started it.
Neither lets an agent's own shell run a command on a pod it chooses, which is what running several isolated end-to-end stacks or the shards of a test suite needs.

## Decisions

- **A named worker is a dedicated subagent worker without the subagent.**
  Its key inserts one `named=<id>` part where a subagent's worker has `subagent=<id>`, with the normal worker as its state scope, so mounts, credentials, per-requester resources, runner tokens, NetworkPolicy, and the runner's own resolution of a pinned key apply unchanged.
  The key helpers treat both kinds as one, so a third kind is a new literal, not new branches.
- **The argument is on `run_shell_command`, not a new tool.**
  The shell is already what `mindroom-agent` calls from inside a command, so one argument serves a model step, a background job, and a shell script, under the approval rules, hooks, call limits, and audit trail every shell call has.
  `file` and `python` take no `worker`: every worker of an agent mounts the same workspace, and a named worker runs Python through its shell.
- **A reply owns its named workers, and nothing about them is stored.**
  The fleet is found by the turn ID the reply's tool context carries, which its own calls, its background jobs, and its CLI calls share while a subagent's turn has another, and it is released the way the reply's CLI grant is: at the end of the turn, or when the last background job the turn started settles.
  After a crash the only truth is what the cluster runs, which cleanup reads, so a durable fleet record could only disagree with it.
- **The key holds the fleet's digest of the name, from a nonce no restart keeps.**
  The model's string never reaches a Kubernetes name, label, or path.
  A worker that outlives its fleet can never be reached again, only removed: a resumed turn that reattached to a worker with stale processes and bound ports would fail in ways that are hard to see.
- **Two layers remove a named worker.**
  The fleet removes its workers when it is released, and cleanup removes any named worker in which no call ran for its idle timeout or that reached its maximum age.
  The second layer needs no memory of who started what, so it also covers a primary that restarted, a failed removal, and a start that failed.
  The maximum age is the only bound on a command that hangs inside a job without a `timeout`.
- **A stopped named worker is removed, never restarted.**
  The next call with its name starts an empty one with its full lifetime, where a restart would inherit the old creation time and be stopped at once.
  A subagent's worker keeps its restart, because a follow-up continues the same child.
- **Limits count every named Deployment that is not being deleted, plus the starts in flight.**
  A worker counts from its start until it is removed, whatever its status, because a failed or idle one still holds a pod or its state until cleanup removes it.
  Counting from the cluster makes a worker a crashed primary left behind use up its requester's limit, which is what bounds the pods a restart loop could accumulate.
  A deployed worker its reply already owns needs only a check of its own Deployment. New or missing workers use the full count.
  The starts in flight are read before the Deployments are listed, so a start that finishes in between is counted in the listing.
  A deployment can set its limit to zero to disable named workers entirely.
  A call past a limit fails with the count instead of waiting, because a queue needs an owner, an order, and a cancellation path that a refused call does not.
- **Admission decides a worker's size under the lock that starts it.**
  The first admitted call records the size, and every call's start applies the recorded size, so two calls that name a new worker at the same time cannot apply two pod templates and restart the pod under a running command.
- **Only agents whose workers are per requester and agent run named workers.**
  The per-requester limit is counted by the normal worker's key, which names the tenant, requester, and agent only on the `user_agent` scope.
  On `shared` and `user` scopes the limit would need the requester and agent carried beside the key, so those scopes are refused until someone needs them.
- **A primary removes the named workers it did not start on its first maintenance pass.**
  No fleet survives a restart, so a named worker created before the process started can never be reached again, and waiting for its idle timeout only holds its requester's limit.
- **A failure is recorded against the Deployment that failed.**
  A handle carries the Deployment's UID, and a failure from a different UID or recorded startup count is ignored.
  The `started` marker uses that same identity to report a new or replaced Deployment. It does not report Kubernetes replacing a pod or restarting a container within an unchanged Deployment.
  A late failure from an earlier pod in the same Deployment can still stop its replacement.
- **`workers run` wraps `tools call` with minimal control-plane support on `/calls/{id}`.**
  `GET /api/agent-cli/calls/{id}?wait=N&after_progress=R` (up to 25 s, at most 64 concurrent waiters per owner; a read over the cap answers at once) long-polls without consuming an active-operation slot and returns only the latest `progress` message (no history buffer; intermediate updates between reads may be skipped, while `progress_revision` guarantees the latest is seen).
  The CLI leaves at least 0.2 s between two reads, which bounds its reads of a server that ignores `?wait=`.
  `POST /api/agent-cli/calls/{id}/cancel` is scoped to the turn owner's bearer (not the originating Bash window, so another window of the same turn can cancel a known call ID), answers `401` for an unknown ID, leaves a settled call's receipt unchanged, and cancels the admitted task at most once across concurrent requests: only the request that cancels it waits for it to settle, and a repeat answers with the current receipt at once.
  Both routes validate the bearer again after they waited, so a grant that expired or was revoked meanwhile gets `401`, not a receipt.
  Cancel lives in `LiveTurnTools.cancel_call` alone and does not touch the approval layer: while a call waits in its approval handler, a cancel only marks it, and the call ends `cancelled` without running when the handler returns.
  A delegate call whose child waits for approval is deferred the same way, and ends `cancelled` as interrupted, since its child may have run.
  The CLI holds a signal from the moment its handlers are installed until the submit request returns, so its cancel never overtakes its submit; a signal held through a refused submit ends the CLI as that signal and cancels nothing.
  A second signal restores the default handler and ends the CLI at once; during the submit that is before the CLI can send a cancel or print a call ID, so an admitted command runs on to its `--timeout`.
  It exits with the command's exit code, using `124` for timeout, `125` only for pre-submit failures, any `4xx` answer to the submit (the call was never admitted; this includes a `408` or `429` from a proxy in front of MindRoom), and named-worker refusals (a `NamedWorkerError`, or a `worker` or `resource_profile` argument the agent's shell does not offer), `255` for every other post-submit failure or unknown outcome (don't blindly retry), `128 + signal` when the CLI is signalled while it submits or waits, and `2` for usage errors, with all CLI messages on stderr, or dropped when stderr is closed or fails a write, which changes neither stdout nor the exit code.
  The result and the cancel notice are printed after the CLI's signal handlers are removed, so a signal during a blocked write stops the CLI as it does on any program.
  A delegated child's worker progress and refusals are not bound to the delegate call's receipt.
  It reads the exit code and the stop at the time limit from the words the shell leads its result with, which tests build with the shell's own formatters; a receipt field for the exit status would be a second contract through the runner, the proxy, and the receipt for the same fact.
  A launch failure is a failed tool call, not command output. When the runner saves the output, the worker header and the shell’s exit line are in the preview.
  It submits once and retries only receipt reads, even when the submission response is lost, because a second submission could run the command twice.
- **Maintenance retries a failed removal from the worker's files.**
  A removal deletes the Deployment before the files, because the pod must be gone first, so a failure in between left files no pass found.
  A worker directory that no Deployment names for a grace period, and whose startup manifest names a pinned key that maps to that directory, is removed under the worker's lock.
  A removal uses a marker beside the directory that names the worker's key, because deleting the files can stop after the manifest is gone; a later pass, also of a restarted primary, finishes a marked removal without the manifest. If space or quota prevents writing a new marker, removal still goes ahead.
  Pinned workers record their startup identity before provisioning credentials, so maintenance can find even an early failed start.
  Retirement markers are read and written relative to the validated workers directory without following symlinks.
  A manifest that cannot be read is read again after the grace period.
  An orphan sweep only trusts manifests and markers for subagent and named workers. A script worker’s run retries its own removal.
  After a subagent worker's Deployment disappears for any reason (including operator deletion), its directory is removed once the sweep has seen it absent for five minutes and no dependent ReplicaSet or non-terminal pod remains; that costs a cold start and caches, not workspace data.
- **The worker protocol moves to 9 with an unchanged wire schema**, as 8 did, because a runner must map a `named=` key to its normal key.
- **A Kubernetes worker prepares itself behind its health check, and the call waits for that separately.**
  A production profile of `workers run` found 90 to 95 % of a warm call in the worker's per-call child, which imported every plugin again, and a cold start that reported ready at 20 s but ran its command at 72 s, after a lazy venv (12 s), shell supervisor (2 s), and forkserver template (26 s).
  Doing that work before the health check answers would put it inside `readyTimeoutSeconds`, which already covers scheduling, the image pull, and the pod start: 32 to 49 s in a burst of 32 workers, where the preparation itself takes about three times as long, so workers would be marked failed on a busy node.
  The worker therefore answers as on a lazy start and reports the setup's state in the same answer, and the primary waits for it with its own budget and reports `preparing`, so "ready" still means a command runs at once.
  Every failure of the eager setup falls back to the lazy path a first request always had, which also reports the real error.
  The health answer gained an optional field and the startup manifest an optional key, both ignored by the other release, so the worker protocol stays at 9.
- **A forkserver template imports only plugins that opt in, once, and is never changed afterwards.**
  Importing in the template runs a plugin before the call's working directory and environment apply, and shares its threads, open files, and state with every call, which is only correct for a plugin that promises it, so `worker_preload` in the plugin manifest defaults to off.
  The primary lists the enabled plugins in the worker's startup manifest, because a worker pod has no config file and would otherwise learn its plugins only from the first request.
  A request whose opt-in plugins or their files differ from what the template imported drops the inherited plugins in its own child and imports them there, so no request replaces a template another request is about to fork from.
  The preload reaches the template through its environment, and the template has no control channel, because tool code runs under the same user and could otherwise change what later calls inherit.

## Rejected alternatives

- Kubernetes Jobs: `ttlSecondsAfterFinished` and `activeDeadlineSeconds` would clean up while the primary is down, but a Job runs no sandbox runner, so cancellation, progress, the call's environment, credential leases, and the egress identity would each need a second path, with `batch` permissions the worker manager's Role does not have.
- One Deployment with several replicas: a worker is one key, one pod, one endpoint, one token, and one state directory in the ready cache, in-flight tracking, and removal.
- Workers kept for the thread instead of the reply: stacks would stay warm between messages, but every fan-out would leave its pods idle until a timeout, and a name would reattach to a pod with stale processes.
- Starting many background scripts: a script is detached from the reply, so a forgotten one holds a pod for up to a day.
- A queue for calls past a limit; see the limits decision.
- A `workers` argument on `request_network_access`; see the first limit.

## Limits

- Approved-egress grants do not reach a named worker, because the egress proxy matches a pod by its own worker key and reads a shared agent's name from the fourth key part.
  Teaching the proxy to match a pinned key's normal key would carry grants over to named and subagent workers alike, and is the place to fix it, so MindRoom mints no extra grants meanwhile.
- While the primary is down nothing is removed; commands stop because their connections drop, and the workers go on the first maintenance pass once the primary is back.
- A turn that resumes after its fleet was released, such as after an approval that outlived the reply and its jobs, gets new workers, so an agent whose shell commands need approval gets a new worker when the paused turn no longer owns the original fleet. This also applies to CLI approval recovery.
- Agents whose workers are shared or per user run no named workers.
- Cancelling `workers run` via `SIGINT`/`SIGTERM`/`SIGHUP` requests remote cancellation (8-second client budget over the server's 5-second settle wait); `SIGKILL` still cannot be caught, leaving the command's own `--timeout` to stop it.
- Cancelling a command that waits for approval leaves its approval card in place until it is decided or the reply ends; approving the card runs nothing.
  If the reply pauses for that approval, before or right after it is decided (such as a grant expiring during the approval check), the approved command runs when the reply resumes, as without the cancel.
  Withdrawing the card on cancel is left to a follow-up change.
- A closed stdout pipe is not handled by `workers run` yet; that comes with the change to how output travels.
- A subagent owns the named workers its own turn starts, not its caller's, and only when its own agent sets `named_workers`.
- Background scripts, OpenAI-compatible API requests, and other calls without a reply's turn cannot name a worker.
- Each named worker starts with empty package caches and costs a cold start, and nodes the autoscaler adds can take longer than `readyTimeoutSeconds`.
- A worker whose opt-in plugin changed on disk imports it in every call until the worker restarts, and a package upgraded in the worker's venv is not picked up by plugins the template already imported.
- The runner process still imports a request's plugins itself on its first request, and admission still lists the worker Deployments once per new worker under one lock.
- With a `ReadWriteOnce` storage claim every worker runs on the runtime's node, so named workers add isolation and their own resource limits there, not capacity.
- Dedicated subagent workers are still not counted against any limit.
- The Docker backend runs no named workers and does not retry a failed removal from the worker's files.
- The warning `subagent_worker_retire_failed` is now `pinned_worker_retire_failed` and covers named workers too; a retirement that cannot allocate its marker on a full volume logs `worker_retirement_marker_allocation_failed` and removes the files without it. Neither is a stable event in `docs/deployment/operational-log-events.md`.
