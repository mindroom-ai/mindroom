# Dedicated subagent workers

A subagent started with `run_subagent(dedicated_worker=True)` runs its worker-routed tool calls in a worker of its own, so an agent that fans out work scales across pods instead of sharing one.

## Problem

A worker key names scope, requester, and agent, so every same-agent child (fresh copies, authored copies, profiles) and every child of a `worker_scope: user` agent shares one worker.
Background-job subagents run concurrently, so they compete for that one worker's CPU and memory, which default to `500m` and `1Gi` on Kubernetes.

## Decisions

- **The model opts in per call; the operator allows it per calling agent** (`dedicated_subagent_workers`), because each such worker costs cluster capacity the operator pays for.
- **Background-script machinery is reused.** A script run already gets a worker of its own whose state scope is its normal worker (`WorkerSpec.state_scope_worker_key`), with resource profiles and a structural key check (`resolve_state_scope_worker_key`), so a subagent's worker mounts exactly what its normal worker mounts.
- **The pin is a `subagent=<id>` part before the normal key's last part.** Requester parts are `~`-prefixed URL-quoted Matrix IDs that keep `:`, so `@a:subagent-<hex>` and `@a:script-<hex>` are valid requesters whose normal keys look pinned when parts are matched by prefix. `=` is escaped in requester parts and replaced in tenant and agent parts, so a part containing it can only be a pin, and the runner and the backends can find a pinned key's normal key without being told. Script keys keep `script-<hex>` and are only ever checked against a known run ID.
- **The pinned key keeps every scope**, including `shared`, `user`, and unscoped. Forcing `user_agent` as scripts do would change a `user`-scope child's mounts, need private-scope restrictions, and fail the runner's browser check that the key's scope equals the route's `worker_scope`.
- **The choice persists on `DelegationChild` and reaches calls through a registry keyed by the child's session ID**, bound for each attempt in `child_run_context`, which the direct, native, and job adapters share for fresh and resumed attempts. The child's session ID is in every identity its calls carry, including identities the Matrix tool context rebuilds from its fields and minimal-mode CLI calls that arrive as separate API requests.
- **The pinned worker serves its normal key from its own credential store**, where the primary mirrors shared credentials; otherwise it would open an empty store for the normal key below its own root.
- **Idle cleanup deletes a subagent's worker** instead of scaling it to zero, since nothing reuses it after its subagent is done.
  Dedicated workers are offered only while this process runs a cleanup loop with a positive interval: the loop lives in the API server, so `mindroom run --no-api` with a positive interval would otherwise accumulate them.
- **The worker protocol moves to 8 with an unchanged wire schema**, as 7 did, because runners must map pinned keys to their normal key; an older runner would resolve a private agent's workspace and credential store from the pinned key.
- **`DelegationChild` defaults the two new fields** instead of a conversation layout step, because run metadata is rewritten only by layout steps, and the defaults are how older children ran.

## Rejected alternatives

- More replicas of one worker behind its Service: tmux sessions, background shells, and browser sessions live in one pod, and a Service spreads calls across pods.
- A bigger single worker: vertical only, and already possible with `MINDROOM_KUBERNETES_WORKER_USER_RESOURCES_JSON`.
- Running the child's agent loop in the pod: the pod would need model keys and the credential store, which the trust model keeps in the primary.
- The pin on `ToolExecutionIdentity`: the Matrix tool context rebuilds identities from its fields and drops it, subagent ownership records compare serialized identities, and the identity crosses the worker wire.
- A context variable instead of the session-keyed registry: minimal-mode CLI calls are separate API requests.
- Scaling the worker down when the child's turn ends: `ensure_worker` returns before a call registers as in flight, and file requests never register, so a turn-end release can stop a worker under a call that just started; idle cleanup cannot, because `ensure_worker` refreshes the last use.

## Limits

- A dedicated worker starts empty: values saved inside the normal worker's own credential store and packages installed into its environment are not there.
- Each dedicated worker costs a cold start, and Kubernetes nodes the autoscaler adds can take longer than `readyTimeoutSeconds`.
- Nothing caps how many dedicated workers run at once beyond a namespace `ResourceQuota`.
- A worker stays for the idle timeout after its last call, holding its resource requests.
- Worker Computer opens the agent's normal worker; showing a child's worker would need an authorized choice of child, which a session lookup cannot give that endpoint.
- As for every worker, a call that has ensured its worker but not yet registered as in flight, or a file request, which never registers, can lose the worker to an idle pass; it needs an idle timeout shorter than a cold start, and closing it means tracking use from before `ensure_worker`.
