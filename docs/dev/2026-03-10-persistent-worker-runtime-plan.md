# Persistent Worker Runtime Plan

This record explains why MindRoom runs tools in persistent, scoped worker runtimes behind the visible agent, and why those runtimes hold no authoritative agent state.
What each worker scope mounts and how to deploy each backend are in [Sandbox Proxy Isolation](../deployment/sandbox-proxy.md); the trust model is in [Security Posture](../architecture/security-posture.md).

## Why

Users expect an agent to keep its work across turns: a package installed in one turn is usable in a later turn, and files created in one turn can be read later.
Some deployments also need requesters isolated from each other while they talk to the same shared agent, without running one full MindRoom runtime per user.
Users talk to one visible agent with one prompt, model, tool config, and Matrix identity, so which runtime executes a tool call, and when it is reused, stays hidden behind that agent.

## Decisions

- Each non-private agent, and each realized private instance, has exactly one authoritative state root, and `worker_scope` decides only which runtime executes a call and when it is reused, never which files are authoritative.
  This lets two requesters of one non-private agent use different runtimes while sharing that agent's files, and lets an operator change an agent's scope without moving or forking its state.
- Worker runtimes hold only non-authoritative state, such as virtualenvs, caches, scratch files, and provider metadata, so a worker can be evicted, scaled to zero, or recreated from scratch at any time without losing agent state.
- Requester-, room-, or thread-level distinctions inside durable state belong in record keys or table contents, not in separate filesystem roots per scope.
- Several runtimes can write one agent's files at once, such as different requesters' `user_agent` workers for one agent.
  Simple workspace files may use last-write-wins when that is acceptable, but agent-owned files that more than one runtime mutates, such as SQLite databases, memory entrypoint files, and structured metadata files, need a storage engine or strategy that tolerates concurrent writers.
- Routing lives in the common tool execution layer and works for any worker-routed tool, so the architecture is not special-cased around `shell`, `file`, or `python`.
  The routing layer needs only a resolved worker handle and an endpoint, so local and hosted providers share the same routing logic and tests.
- Tools that need the live Matrix client, mutate agent configuration, schedule or orchestrate work globally, or delegate through primary-runtime orchestration stay in the primary runtime by design, not as temporary exceptions.
- Agent isolation comes from mount boundaries, not path conventions.
  `base_dir`, the working directory, and other tool init hints are conveniences, because code-execution tools can traverse anything their process can reach; a runtime that can see other agents' roots is not agent-isolated, whatever `base_dir` says.
- `user` is a per-requester multi-agent workstation that deliberately shares files among the agents it serves; `user_agent` is the scope for per-agent filesystem isolation.
- Worker keys derive only from trusted execution identity plus agent configuration, so a requester cannot route into another requester's worker by supplying identity fields.
- Credentials keyed only by service name are incompatible with requester isolation, so worker-routed credentials are scoped, and sharing them requires explicit opt-in.
- The Google tools use scoped per-provider OAuth credentials and stay in the primary runtime, so worker runtimes never need Google OAuth client secrets.
- The dashboard's authenticated principal is not the Matrix requester identity that resolves `user` and `user_agent` workers, and the two are not guaranteed to map to the same worker namespace.
  Generic dashboard credential management therefore stays limited to unscoped and `shared` agents; scoped OAuth connections are the exception, allowed only when the dashboard request is bound to a Matrix requester identity.
- The OpenAI-compatible `/v1` API never treats the request-body `user` field as a trust source.
  `shared` can work without a trusted principal, while `user` and `user_agent` require one.

## Rejected alternatives

- One full MindRoom runtime per user: requester isolation must not require it.
- A fresh container per tool call, which cannot keep installed packages and files between turns.
- Persisting state in container writable layers, because workers are recreated and state must outlive them.
- Keying agent state by worker scope, or giving runtimes bootstrap copies or alternate authoritative locations, because one runtime would then edit one copy while another reads a different one.
- Treating arbitrary backgrounded shell commands as durable product state; supported background Python goes through the supervised `script` tool.

## Assumptions

- Providers may realize the state model through mounts, path overrides, bind propagation, or other mechanisms, as long as every runtime that needs durable files reaches the same canonical files.
- A Kubernetes worker needs durable storage for its runtime caches, access to the canonical workspaces it executes against, and an authenticated internal-only endpoint.
- One process may realize many logical workers, as the shared runner does, provided runtime caches stay isolated by worker key.

## Limitations

- The shared `static_runner` runner isolates runtime caches only, not agents or requesters; dedicated Docker and Kubernetes workers are the providers that can isolate agents, as [Security Posture](../architecture/security-posture.md) records.
- `spotify` and `homeassistant` stay shared-only until they have a scoped credential binding model.

## Open gaps

- Enabling `user` and `user_agent` agents on `/v1`, which needs a trusted authenticated principal.
- Choosing the concurrent-writer-safe storage or strategy for agent-owned files that several runtimes mutate.
- Fleet-level metrics are missing: worker creation and reuse counts, startup latency, idle evictions, execute requests by tool and scope, and credential lease issuance and expiry.
  The workers API serves debugging, not fleet observability.
- Logs and metrics should identify workers by key hash or short ID rather than raw requester identifiers.
- Operator guidance for stuck or unhealthy dedicated workers is missing, and no production validation of dedicated Kubernetes workers is recorded.
- Worker state retention should be configurable per deployment, because local developer workflows and hosted SaaS expect different things; the hosted retention policy is undecided.
- Whether tool metadata should also declare a tool's credential scope, whether it needs persistent state, and which memory backends it allows, beyond its default execution target and need for the primary runtime.
- Whether explicit user-facing worker reset commands should exist.
- Whether Kubernetes worker lifecycle should move behind an external controller consuming the same backend contract.
