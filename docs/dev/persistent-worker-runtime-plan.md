# Persistent Worker Runtime Plan

Last updated: 2026-03-10
Owner: MindRoom backend
Status: Phase 1 prototype implemented in commit `fa9b1418`

## Objective

Implement persistent worker-scoped execution environments for MindRoom tools.
Keep the visible agent as the shared product abstraction users talk to.
Move mutable execution state into worker-owned storage selected per tool call by policy.
Support user-isolated, room-isolated, and shared collaboration modes without building one full MindRoom runtime per user.

## Why This Is Split Into Phases

Phase 1 proves the core architecture can route generic tool calls into persistent scoped workers.
Phase 2 makes all mutable state obey the same scope so the system becomes correct rather than merely demonstrable.
Phase 3 expands policy and lifecycle behavior once the state model is sound.
Phase 4 makes the system production-ready in Kubernetes.
Doing the work in this order prevents expensive migration and deployment work from being built on top of an unproven routing model.

## Current Status

Phase 1 is implemented and already useful for proving persistent tool execution.
The current prototype provides generic worker routing for tool calls rather than a `shell`-only special case.
The current prototype validates persistence with `shell`, `file`, and `python`.
The current prototype carries execution identity from Matrix and OpenAI-compatible ingress into worker-routed tool calls.
The current prototype persists worker workspace, cache, and Python packages inside worker-owned state.
The current prototype aligns file-backed memory reads and writes with worker-owned state for worker-scoped agents.
The current prototype does not yet make sessions, learning, and credentials fully worker-scope-aware.

## Product Boundary

- Agent means the visible MindRoom entity with prompt, model, tool config, Matrix identity, and public behavior.
- Worker means the hidden persistent execution environment that owns mutable runtime state.
- Worker scope means the policy that decides which worker should execute a given tool call.
- Primary runtime means the shared MindRoom process that handles ingress, orchestration, routing, prompt assembly, and tool dispatch.

## Core Design Decisions

- Tool routing must be generic and live in the common tool execution layer.
- The design must not depend on container writable layers for persistence.
- Mutable state owned by tools must live in the same worker scope that executes those tools.
- Public shared agents must remain possible.
- User isolation must be possible without spawning one full MindRoom runtime per user.
- Worker scope must be configurable per agent rather than globally fixed.
- Tools that require host-only services such as the Matrix client must remain runnable in the primary runtime.
- Credentials must become scope-aware rather than globally loaded by service name.

## User-Facing Outcomes

- A user can install a package in one turn and use it in a later turn.
- A user can create files in one turn and read them in a later turn.
- A shared public agent can still exist in a public room.
- Two users talking to the same agent can land in different workers when the scope requires isolation.
- Collaborative behavior can still use shared mutable state when the scope is `room_thread` or `shared`.
- Editable file-backed memory stays consistent across turns because reads and writes come from the same worker-owned state.

## Non-Goals

- Do not build one full MindRoom runtime per user.
- Do not create a fresh container per tool call.
- Do not special-case the architecture around only `shell`, `file`, or `python`.
- Do not support split-brain mutable state where worker tools edit one copy and prompt assembly reads another.
- Do not promise durable background processes as a product contract until a dedicated supervisor exists.

## Scope Semantics

- `user` means the same requester gets the same worker across all agents that use the `user` scope in the same tenant.
- `user_agent` means the same requester gets a separate worker for each agent.
- `room_thread` means all tool execution within the same room thread shares one worker.
- `shared` means everyone using that agent shares one worker.
- `user` is the default isolation choice when users need a personal coding environment that survives across agents.
- `user_agent` is the default isolation choice when agents need separate package or filesystem state.
- `room_thread` is the default collaborative choice when a thread should accumulate shared runtime state.
- `shared` is the explicit opt-in mode for globally shared worker state.

## Worker Key Resolution

Worker keys must be deterministic, stable, and derived entirely from trusted execution identity plus agent configuration.
Worker keys are an internal routing identifier rather than a user-facing concept.
The current canonical shape is versioned and string-based so it can evolve without data ambiguity.

- `shared` resolves to `v1:<tenant>:shared:<agent>`.
- `user` resolves to `v1:<tenant>:user:<requester>`.
- `user_agent` resolves to `v1:<tenant>:user_agent:<requester>:<agent>`.
- `room_thread` resolves to `v1:<tenant>:room_thread:<room>:<thread>`.

## Execution Identity

Every tool call needs a serializable execution identity that contains only the information needed for routing and scoped state resolution.
Execution identity must not contain process-local objects such as the Matrix client, the whole config object, or other non-serializable services.

The execution identity should contain these fields.

- `channel`
- `tenant_id`
- `account_id`
- `agent_name`
- `requester_id`
- `room_id`
- `thread_id`
- `resolved_thread_id`
- `session_id`

## Trusted Identity Rules

Matrix already provides a trustworthy requester identity path through the sender and existing runtime context.
The OpenAI-compatible path must not use a request-body `user` field as a durable trust source.
The final design should allow user-scoped workers on `/v1` only when a trusted authenticated principal is present.
If `/v1` lacks a trusted requester identity, only scopes that do not require a requester identity should be allowed.
The practical rule is that `shared` and session-derived `room_thread` can work without a trusted user principal, while `user` and `user_agent` require one.

## Tool Execution Policy

Tool execution policy should become an explicit concept rather than a side effect of the old sandbox settings.
The minimum policy categories are local execution in the primary runtime and worker-routed execution in a scoped worker.
The initial compatibility path can continue honoring `sandbox_tools`, but the source of truth should move to `worker_tools` and `worker_scope`.

The final policy model should support these per-tool decisions.

- `execution_target` with values `primary` or `worker`
- `worker_scope`
- `credential_scope`
- `requires_persistent_state`
- `requires_matrix_runtime`
- `allowed_memory_backends`

## Routing Flow

1. A request enters the primary runtime through Matrix or the OpenAI-compatible API.
2. The primary runtime builds a serializable execution identity.
3. The agent selects a tool and the tool wrapper consults its execution policy.
4. Local-only tools execute directly in the primary runtime.
5. Worker-routed tools derive a worker key from the configured scope and execution identity.
6. The worker manager resolves or creates the worker for that key.
7. The primary runtime optionally creates short-lived credential leases on the target worker.
8. The primary runtime forwards the execute request to the target worker endpoint.
9. The worker executes the tool locally against its own mounted state.
10. The result returns to the primary runtime and then to the model or user.

## Worker Manager

The system needs a first-class worker manager abstraction rather than hard-coding worker lifecycle inside the sandbox proxy.
The worker manager is the runtime-level owner of worker lookup, creation, health, and cleanup.

The worker manager must provide these responsibilities.

- Resolve worker keys from execution identity and scope.
- Find or create the worker for a key.
- Return a worker handle containing endpoint, state location, and status.
- Track liveness and startup state.
- Enforce idle timeout and cleanup policy.
- Expose worker metadata for observability and debugging.
- Work with both local Docker and Kubernetes backends.

The minimum useful worker handle should contain these fields.

- `worker_key`
- `endpoint`
- `state_root`
- `status`
- `backend`
- `last_seen_at`

## Worker Storage Layout

All mutable worker state must live on mounted storage under a worker-owned root.
The exact on-disk layout can stay simple as long as ownership boundaries are clear.

The recommended layout is:

```text
<base_storage>/workers/<worker_dir>/
  workspace/
  venv/
  cache/
  sessions/
  learning/
  memory/
  credentials/
  metadata/
```

The current prototype already uses the worker root as `MINDROOM_STORAGE_PATH` for worker execution.
That keeps existing storage helper code reusable while still isolating worker-owned state.
The worker workspace should remain at `<worker_root>/workspace`.
The persistent Python environment should remain at `<worker_root>/venv`.
Caches should stay inside `<worker_root>/cache`.

## Memory Design

If worker-routed tools can edit memory, memory reads during prompt assembly must resolve against the same worker-owned storage.
The current prototype already does this for file-backed memory.
The full design should keep file-backed memory as the only worker-editable memory backend until another backend has a deliberate synchronization model.

The final memory rules are:

- Worker-scoped agents that allow worker-editable memory must use `memory.backend=file`.
- File memory for worker-scoped agents resolves relative to the worker storage root.
- Shared agent memory outside worker scope remains supported for non-worker-routed agents.
- Shell and file tools must not directly mutate non-file memory backends.
- Room memory should only be auto-written into shared scopes when that sharing is intentional.

## Sessions And Learning

Sessions and learning are currently per-agent and must become worker-scope-aware.
The storage resolver should choose paths from the same worker root when a worker-scoped agent is active.
This should be done through path resolvers rather than duplicating logic at every call site.

The target rules are:

- Session SQLite paths become worker-scoped when the active agent has a worker scope.
- Learning SQLite paths become worker-scoped when the active agent has a worker scope.
- Non-worker-scoped agents continue using the existing shared per-agent locations.
- Prompt assembly and tool execution must agree on the same session and learning scope.

## Credentials And Leases

Global credentials keyed only by service name are incompatible with strong user isolation.
The final design should default to requester-scoped credentials for worker-routed execution unless sharing is explicitly configured.

The target credentials model is:

- Credentials are stored under a scope-aware namespace rather than only `service_name`.
- The common credential scopes are `shared`, `user`, and `worker`.
- The default for worker-routed tools is `user` when a requester identity exists.
- Shared credentials require explicit opt-in.
- Credential leases are created on the target worker and are short-lived and single-use by default.
- Leased credentials never become part of the model prompt or normal tool arguments.

## Local Development Backend

The local backend should use one primary MindRoom runtime plus many persistent worker containers.
Each worker should map a host directory to its worker root and expose an internal sandbox-runner API.
The current prototype can continue using the existing sandbox runner with worker-keyed state while the dedicated worker manager is introduced.
The final local backend should allow introspection of active workers and cleanup of idle workers for debugging.

## Kubernetes Backend

The long-term Kubernetes model is not one static sidecar per tenant pod.
The long-term model is a control-plane runtime plus dynamically managed worker pods or containers.
Each worker needs a durable volume or durable directory mapping for its state root.
Workers should be discoverable only inside the cluster network.

The Kubernetes responsibilities are:

- Create worker pods on demand from worker keys.
- Attach persistent storage to each worker.
- Route execute and lease requests to the correct worker endpoint.
- Scale idle workers down without losing mounted state.
- Surface worker health and startup failures clearly.

## Local And Kubernetes Interface Contract

The worker manager should hide backend-specific details from the tool wrapper.
The tool wrapper should only need a resolved worker handle and an endpoint.
This keeps local Docker and Kubernetes behavior aligned and testable with the same routing logic.

## Tools That Must Stay Local

Some tools should continue executing in the primary runtime because they depend on process-local services or orchestrator authority.
These tools are part of the final design rather than temporary exceptions.

The default local-only set includes:

- Tools that require the live Matrix client.
- Tools that mutate agent configuration.
- Tools that schedule or orchestrate background work globally.
- Tools that delegate to sub-agents using primary runtime orchestration.

Concrete current examples include `scheduler`, `subagents`, and self-configuration flows.

## Background Processes

Background processes inside workers should not be treated as reliable product state until MindRoom has an explicit worker-local supervisor.
The safe contract is that worker processes may be terminated whenever a worker is evicted, restarted, or migrated.
If background execution is later supported, worker metadata should track process identity and cleanup state under the worker root.

## Security And Isolation Rules

- Worker state must be isolated by mounted storage boundaries rather than only by logical path conventions.
- Worker endpoints must be internal-only and authenticated.
- Logs and metrics should prefer worker-key hashes or short IDs rather than raw user identifiers.
- Shared scopes must be explicit rather than accidental fallbacks.
- A requester must never be able to route into another requester's worker by supplying untrusted identity fields.
- Credentials should only be loaded into the worker scope that is allowed to use them.
- Worker-owned mutable state must never silently fall back to shared primary-runtime state.

## Migration Strategy

Migration needs different behavior for shared and isolated scopes because current data is mostly agent-scoped and shared.
Not all existing data can be safely partitioned after the fact.

The migration rules should be:

- Agents without worker scope keep existing storage untouched.
- `shared` scope may migrate existing agent-scoped sessions, learning, and file memory into the shared worker root on first activation.
- `user`, `user_agent`, and `room_thread` should not automatically clone shared historical state into every new worker.
- Isolated scopes should start with empty mutable state unless an explicit import is requested.
- Compatibility fallback from legacy `sandbox_tools` to `worker_tools` should remain during the transition window.

## Observability

The full system should expose enough telemetry to answer whether worker routing is correct and whether isolation boundaries are holding.

At minimum we need:

- Worker creation and reuse counts.
- Worker startup latency.
- Worker idle eviction counts.
- Execute request counts by tool and scope.
- Credential lease creation and expiration counts.
- Storage path resolution traces for debugging.
- Health and failure reason visibility for worker startup errors.

## Operational Policies

The recommended idle policy is to keep workers alive for roughly 30 minutes after last use.
State should outlive the live worker process so a new worker can be recreated for the same key later.
Cleanup should remove only live workers on idle timeout by default and leave state retention to a separate policy.
State retention should be configurable per deployment because local developer workflows and hosted SaaS have different expectations.

## Testing Strategy

The full implementation should keep the same test pyramid across all phases.

Unit tests should cover:

- Worker key resolution.
- Scope-aware path resolution.
- Identity derivation.
- Credentials scope selection.
- Policy decisions for local versus worker-routed tools.

Integration tests should cover:

- Package persistence across turns.
- File persistence across turns.
- File memory persistence across turns.
- Session persistence in scoped workers.
- Learning persistence in scoped workers.
- Isolation between two users of the same public agent.
- Shared collaboration in `room_thread` and `shared` scopes.

System tests should cover:

- Local Docker worker lifecycle.
- Kubernetes worker creation and reattachment to state.
- Worker eviction and recreation with preserved state.
- `/v1` behavior with and without trusted requester identity.

## Acceptance Criteria

- A user can install a package in one turn and use it later.
- A user can create files in one turn and use them later.
- File-backed memory edited from tools is visible in later turns from the same worker scope.
- Session and learning state follow the same worker scope as the tool execution environment.
- Two users of the same shared agent do not see each other's workspace, credentials, or worker-owned memory when the scope is isolating.
- Collaborative modes remain possible for `room_thread` and `shared`.
- Tools that require primary-runtime services continue working locally.
- The routing layer remains generic for any worker-routed tool.

## Phase Plan

### Phase 1: Generic Worker Routing Prototype

Phase 1 is complete.
Phase 1 introduced `worker_scope`, worker key resolution, execution identity propagation, and generic worker-routed tool dispatch.
Phase 1 validated persistence with `shell`, `file`, and `python`.
Phase 1 aligned file-backed memory with worker-owned storage.

### Phase 2: Scope-Aware Mutable State

Phase 2 should make all remaining mutable state obey worker scope.
Phase 2 is the correctness phase.

Phase 2 work items are:

- Add worker-aware session storage resolvers.
- Add worker-aware learning storage resolvers.
- Refactor credentials storage and lookup to be scope-aware.
- Refactor credential lease issuance to target the resolved worker endpoint.
- Enforce file-backed memory for worker-editable agents.
- Block direct worker mutation of unsupported memory backends.
- Add migration behavior for `shared` scope and explicit-import behavior for isolated scopes.

### Phase 3: Policy Expansion And Lifecycle

Phase 3 should complete the behavior surface around scopes and worker management.
Phase 3 is the policy phase.

Phase 3 work items are:

- Introduce a first-class worker manager abstraction.
- Implement explicit local Docker worker management rather than relying on one static runner process.
- Implement idle cleanup and state retention rules.
- Tighten `/v1` scope eligibility based on trusted requester identity.
- Add observability surfaces for active workers and worker failures.
- Finalize defaults for which tools are local versus worker-routed.

### Phase 4: Production Kubernetes Runtime

Phase 4 should move the design from local correctness to production deployment.
Phase 4 is the operations phase.

Phase 4 work items are:

- Implement dynamic worker pod lifecycle in Kubernetes.
- Attach durable storage to workers.
- Add worker health checks and readiness handling.
- Add metrics and debugging endpoints.
- Document retention, cleanup, and storage-class strategy.
- Document incident handling for stuck or unhealthy workers.

## Recommended Immediate Next Step

The next implementation step should be Phase 2.
The first concrete target inside Phase 2 should be session and learning storage resolvers because they close the biggest remaining split-state gap after memory.

## File Map For Remaining Work

- `src/mindroom/tool_system/worker_routing.py` should remain the source of truth for execution identity, scope semantics, worker keys, and scoped path helpers.
- `src/mindroom/agents.py` should move session and learning path selection behind worker-aware resolvers.
- `src/mindroom/credentials.py` should become scope-aware and stop assuming global service-only keys.
- `src/mindroom/api/openai_compat.py` should enforce trusted requester identity rules for user-scoped workers.
- `src/mindroom/api/sandbox_runner.py` should evolve behind a worker manager rather than remaining the place where worker lifecycle is implicitly encoded.
- `cluster/k8s/instance/templates/deployment-mindroom.yaml` should eventually stop representing the final deployment model because many dynamic workers cannot be modeled as one static sidecar.

## Open Decisions

- Decide the exact authenticated identity source for `/v1` user-scoped workers.
- Decide the final credential scope defaults for each class of worker-routed tool.
- Decide whether `room_thread` on `/v1` should key from conversation ID, session ID, or a distinct thread identifier.
- Decide the long-term worker retention policy for hosted deployments.
- Decide whether explicit user-facing worker reset commands should exist in the product.

