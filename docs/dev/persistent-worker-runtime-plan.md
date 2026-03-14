# Persistent Worker Runtime Plan

Last updated: 2026-03-13
Owner: MindRoom backend
Status: Phase 1 is complete.
Phase 2 is partially complete.
Canonical per-agent state ownership is implemented, but per-agent filesystem visibility for agent-isolated scopes is not fully enforced yet.
Phase 3 is complete.
Phase 4 provider and operator hardening is still in progress.
The backend-neutral worker contract, lifecycle handling, default routing policy, and Kubernetes provider implementation all exist.
Worker runtimes remain non-authoritative execution environments for caches, temporary files, and provider metadata.

## Objective

Implement persistent worker-scoped execution environments for MindRoom tools.
Keep the visible agent as the shared product abstraction users talk to.
Each agent has one canonical state root.
That root is the source of truth for the agent's context files, workspace files, file-backed memory, mem0-backed state, session and history state, and learning state.
All worker scopes read and write that same agent state root.
`worker_scope` controls execution isolation and reuse, not state ownership.
Support requester-isolated and shared runtime execution without building one full MindRoom runtime per user.

## Non-Negotiable Invariants

- Each agent has one canonical state root.
- The files under that root are the real files, not copies, seeds, mirrors, or alternate authoritative locations.
- `context_files` and `memory_file_path` must resolve inside the agent's canonical workspace.
- `worker_scope` does not change which files are authoritative.
- `shared`, `user_agent`, and unscoped dedicated execution for agent A must only be able to see agent A's canonical state root plus their own worker runtime root.
- `user` is different.
- If `user` remains supported for filesystem-capable worker tools, it is explicitly a per-requester multi-agent workstation mode rather than an agent-isolated mode.
- `base_dir`, current working directory, and other tool init hints are convenience defaults rather than security boundaries.
- If a backend exposes the broader shared agent-state tree to a runtime that is supposed to be agent-isolated, that backend does not satisfy this plan.

## Why This Is Split Into Phases

Phase 1 proves the core architecture can route generic tool calls into persistent scoped workers.
Phase 2 makes all durable agent state obey the same authoritative root and makes agent-isolated scopes see only the agent roots they are allowed to touch.
Phase 3 introduces the backend/provider abstraction, lifecycle model, policy surface, and observability once the state model is sound.
Phase 4 adds production provider implementations against that abstraction, including Kubernetes.
Doing the work in this order prevents expensive lifecycle and deployment work from being built on top of an unproven routing model.

## Current Status

Phase 1 is complete.
Phase 2 is only partially complete.
The current codebase routes tool calls into persistent workers and resolves durable agent state through one canonical root per agent across `shared`, `user`, `user_agent`, and unscoped dedicated execution.
That fixes state ownership, but it does not yet fully fix filesystem visibility for agent-isolated scopes.
Dedicated Kubernetes workers now narrow mounts for `shared`, `user_agent`, and unscoped dedicated execution, but shared-runner and local worker paths still expose broader shared storage than this plan allows for agent-isolated scopes.
Phase 3 is complete because the worker backend contract, lifecycle model, default routing policy, and observability surfaces all exist.
Dedicated Kubernetes workers are provisioned today and rely on the same agent-owned state model while keeping worker-local runtime caches isolated by worker key.
Phase 4 remains in progress as provider hardening, operator guidance, metrics, and broader rollout validation continue.
Google Services, Spotify, Home Assistant, and the Google-backed `gmail`, `google_calendar`, and `google_sheets` tools remain shared-only.
Those integrations are supported only for agents without worker routing or with `worker_scope=shared`.
Dashboard credential management is intentionally limited to unscoped agents and agents with `worker_scope=shared`.
The dashboard does not manage credentials for `user` or `user_agent` workers.
The `/v1` API remains intentionally restricted to unscoped agents and agents with `worker_scope=shared` until trusted requester identity is solved.

## Product Boundary

- Agent means the visible MindRoom entity with prompt, model, tool config, Matrix identity, and public behavior.
- Agent state root means the one canonical storage root for an agent's authoritative files and durable state.
- Worker means the hidden execution runtime that runs proxied tools and may keep non-authoritative runtime caches.
- Worker scope means the policy that decides which runtime should execute a given tool call and when that runtime is reused.
- Primary runtime means the shared MindRoom process that handles ingress, orchestration, routing, prompt assembly, and tool dispatch.

## Core Design Decisions

- Tool routing must be generic and live in the common tool execution layer.
- The design must not depend on container writable layers for persistence.
- Agent-owned state must not be re-keyed by worker scope.
- `worker_scope` must only determine runtime execution isolation and reuse.
- Worker runtimes may keep local caches, temporary files, Python environments, and provider metadata, but those files are not authoritative agent state.
- Public shared agents must remain possible.
- User isolation must be possible without spawning one full MindRoom runtime per user.
- Worker scope must be configurable per agent rather than globally fixed.
- Tools that require host-only services such as the Matrix client must remain runnable in the primary runtime.
- Credentials must become scope-aware rather than globally loaded by service name.
- Concurrent access to canonical agent state is expected and must be handled deliberately.

## User-Facing Outcomes

- A user can install a package in one turn and use it in a later turn.
- A user can create files in one turn and read them in a later turn.
- A shared public agent can still exist in a public room.
- Two users talking to the same agent can land in different runtimes when the scope requires isolation.
- Those different runtimes still read and write the same canonical agent state root for that agent.
- A file edit made through one runtime is visible from every other runtime that executes the same agent.
- Context files, workspace files, file-backed memory, mem0-backed state, sessions, and learning stay consistent because they all come from the same canonical agent state root.

## Non-Goals

- Do not build one full MindRoom runtime per user.
- Do not create a fresh container per tool call.
- Do not special-case the architecture around only `shell`, `file`, or `python`.
- Do not support split-brain mutable state where one runtime edits one copy and another runtime reads a different authoritative copy.
- Do not promise durable background processes as a product contract until a dedicated supervisor exists.

## Scope Semantics

- `shared` means one runtime may be reused by many callers for the same agent, and all of them use that agent's canonical state root.
- `user` means one persistent runtime may be reused per requester across multiple agents, and that runtime still reads and writes the canonical state roots of the agents it executes.
- `user_agent` means runtimes are isolated per requester and agent, but every runtime for that agent still reads and writes the same canonical agent state root.
- Unscoped dedicated execution still uses the same canonical agent state root for the addressed agent.
- `shared`, `user`, `user_agent`, and unscoped dedicated execution differ in runtime isolation and reuse.
- They do not change which files are authoritative for the agent.
- `shared`, `user_agent`, and unscoped dedicated execution for agent A must only expose agent A's canonical state root plus the runtime's own worker root.
- `user` is therefore a trust-sharing mode rather than an agent-level filesystem isolation boundary for filesystem-capable worker tools.
- Multiple agents may run inside that runtime.
- Those agents may access each other's mounted files inside that runtime.
- Use `user_agent` when you need hard per-agent runtime reuse and per-agent filesystem visibility.

## Worker Key Resolution

Worker keys must be deterministic, stable, and derived entirely from trusted execution identity plus agent configuration.
Worker keys are an internal routing identifier rather than a user-facing concept.
The current canonical shape is versioned and string-based so it can evolve without data ambiguity.

- `shared` resolves to `v1:<tenant>:shared:<agent>`.
- `user` resolves to `v1:<tenant>:user:<requester>`.
- `user_agent` resolves to `v1:<tenant>:user_agent:<requester>:<agent>`.

## Execution Identity

Every tool call needs a serializable execution identity that contains only the information needed for routing, runtime selection, and credential policy.
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
Current `/v1` behavior only allows unscoped agents and agents with `worker_scope=shared`.
That restriction is intentional and remains in place until trusted requester identity is solved.
User-scoped `/v1` workers do not currently ship as a supported product behavior.
Future `/v1` support should allow additional worker scopes only when a trusted authenticated principal is present.
One intended final rule is that `shared` can work without a trusted user principal, while `user` and `user_agent` require one.
The dashboard also has an authenticated user identity, but that identity is a dashboard principal rather than the Matrix requester identity used by runtime worker routing.
That means the dashboard must not read or write credentials for `user` or `user_agent` workers until there is a deliberate identity-linking model for those scopes.

## Tool Execution Policy

Tool execution policy should become an explicit concept rather than a side effect of the old sandbox settings.
The minimum policy categories are local execution in the primary runtime and worker-routed execution in a scoped worker.
The source of truth is the combination of tool metadata, `worker_tools`, and `worker_scope`.

The current policy model already supports the first item below.
The remaining items are still design goals rather than fully explicit first-class config.

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

## Worker Backend Contract

The system needs a first-class worker backend and worker manager abstraction rather than hard-coding worker lifecycle inside the sandbox proxy or sandbox runner.
The worker manager is the runtime-level owner of worker lookup, creation, health, and cleanup through a backend-neutral contract.
The worker backend is the provider that realizes a worker handle for a worker key.

The worker manager and backend contract must provide these responsibilities.

- Find or create the worker for a key.
- Return a worker handle containing endpoint, status, and any optional provider-neutral debug metadata needed for observability.
- Touch or refresh worker liveness.
- Track liveness and startup state.
- Enforce idle timeout and cleanup policy.
- Support worker eviction while preserving canonical agent state and any separately retained worker runtime caches by policy.
- Expose worker metadata for observability and debugging.
- Work with both local and hosted providers without leaking infrastructure details into core routing code.

The minimum useful worker handle should contain these fields.

- `worker_id`
- `worker_key`
- `endpoint`
- `auth_token`
- `status`
- `backend_name`
- `last_used_at`
- `expires_at`
- `debug_metadata`

## Agent And Worker Storage Layout

Authoritative agent state and worker runtime state are different things.
Every agent gets one canonical agent state root.
Every runtime gets its own worker runtime root.
Only the agent state root is authoritative for agent files and durable agent state.

The recommended layout is:

```text
<base_storage>/
  agents/<agent_name>/
    workspace/
    context/
    memory/
    mem0/
    sessions/
    learning/
  workers/<worker_dir>/
    venv/
    cache/
    tmp/
    metadata/
```

Concrete providers may realize the same contract through mounts, path overrides, bind propagation, or other provider-specific mechanisms.
If a runtime needs direct filesystem access to agent state, it must mount or otherwise resolve the same canonical agent state root used by every other runtime for that agent.
Worker-local copies of agent files are not authoritative.
The worker runtime root should remain the home for caches, virtualenvs, scratch files, and provider metadata.
Non-secret tool init overrides such as `base_dir` are allowed only as a narrow transport mechanism for pointing tools at the canonical agent workspace.
Those overrides must be explicitly whitelisted, type-validated before toolkit construction, and rejected with a client error when malformed.
Mounting the entire shared `agents/` tree into an agent-isolated runtime is incorrect even if tools start in the right `base_dir`.
For `shared`, `user_agent`, and unscoped dedicated execution, the runtime must only see the addressed agent root.
For `user`, the runtime may intentionally see more than one agent root, but only because that mode is explicitly defined as a multi-agent workstation.

## Memory Design

All agent memory backends must resolve through the canonical agent state root.
File-backed memory, mem0-backed state, and any future worker-editable memory backends must not fork by worker scope.
Prompt assembly and tool execution must read the same memory view across `shared`, `user`, `user_agent`, and unscoped dedicated execution.
Logical scoping inside the memory data model may still exist, but storage authority remains the canonical agent state root.

## Sessions And Learning

Sessions, history databases, and learning databases are agent-owned durable state.
They should resolve from the canonical agent state root rather than from worker runtime roots.
Workers may cache handles or derived in-memory context, but the authoritative on-disk state is per-agent and shared across scopes.
If requester-, room-, or thread-level distinctions are needed, they should be encoded in record keys or table contents rather than in filesystem ownership.

## Concurrency And Consistency

Multiple runtimes may access the same canonical agent state root concurrently.
This is expected for `user` and `user_agent` scopes.
The design must make concurrent writes explicit rather than treating them as an edge case.
Simple workspace files may use last-write-wins semantics when that is acceptable.
Sensitive artifacts must use storage engines or locking strategies that tolerate concurrent writers.
This requirement applies to SQLite databases, memory entrypoint files, structured metadata files, and any other agent-owned file that can be mutated by more than one runtime.

## Credentials And Leases

Global credentials keyed only by service name are incompatible with strong user isolation.
The final design should default to requester-scoped credentials for worker-routed execution unless sharing is explicitly configured.

The target credentials model is:

- Credentials are stored under a scope-aware namespace rather than only `service_name`.
- The common credential scopes are `shared`, `user`, and `worker`.
- The current leading option is to default worker-routed tools to `user` when a requester identity exists, but the exact per-tool defaults are still an open policy decision.
- Shared credentials require explicit opt-in.
- Credential leases are created on the target worker and are short-lived and single-use by default.
- Leased credentials never become part of the model prompt or normal tool arguments.

OAuth-heavy dashboard integrations are an explicit exception to isolated worker scopes.
Google Services, Spotify, Home Assistant, and the Google-backed `gmail`, `google_calendar`, and `google_sheets` tools are intentionally unsupported for `user` and `user_agent`.
They only support unscoped agents and agents with `worker_scope=shared`.
The credential-backed `gmail`, `google_calendar`, `google_sheets`, and `homeassistant` tools also stay local even for `worker_scope=shared` rather than being routed through the sandbox runner.
This keeps the generic worker-routing model clean while the dashboard OAuth connect and callback model remains shared-scope only.
Dashboard credential management follows the same product boundary more generally.
The dashboard may only read, write, test, or disconnect credentials for unscoped agents and agents with `worker_scope=shared`.
Isolated worker scopes remain runtime-owned state rather than dashboard-managed state.

## Dashboard Credential Boundary

The runtime and the dashboard do not currently resolve requester identity from the same trust source.
Matrix worker routing resolves `user` and `user_agent` workers from the Matrix sender.
The dashboard resolves from its own authenticated dashboard principal.
Those identities are not interchangeable and are not guaranteed to map to the same worker namespace.
Because of that, the dashboard must not present itself as a management surface for isolated worker-scoped credentials.
The current product rule is:

- Unscoped agents can use the dashboard credential UI.
- Agents with `worker_scope=shared` can use the dashboard credential UI.
- Agents with `worker_scope=user` or `worker_scope=user_agent` must treat credentials as runtime-owned state.
- Shared-only integrations are hidden or disabled for unsupported worker scopes.
- `/api/tools` may still render a read-only view for unsupported scopes, but it must not imply that dashboard edits will affect the live runtime worker.

## Provider Model

MindRoom core owns worker scope semantics, execution identity resolution, worker key resolution, tool routing policy, and the separation between canonical agent state and worker runtime state.
MindRoom core must not leak provider-specific lifecycle details into the generic routing layer.
MindRoom core now ships the interface and the built-in `static_runner` and `kubernetes` backends needed for current development and hosted deployment shapes.
The local development model, the shared sandbox-runner deployment, and the dedicated Kubernetes-worker deployment are providers behind the same worker backend contract.
The current codebase therefore treats Kubernetes as a built-in provider, not only a future idea.
A future external provider or controller can still consume the same contract without changing core routing logic.

## Local Provider

The local provider should preserve the current behavior of one primary MindRoom runtime plus a shared sandbox-runner that realizes logical workers from worker keys.
The local provider may realize many logical workers inside one runtime process as long as runtime caches remain isolated by worker key and the canonical agent state root remains shared per agent.
The local provider should support introspection of active workers and cleanup of idle workers for debugging.

## Kubernetes Provider

The Kubernetes provider is implemented against the worker backend contract introduced in Phase 3.
The current implementation creates dedicated worker Deployments and Services, propagates the shared sandbox token, and waits for readiness before returning a worker handle.
The current implementation already provisions dedicated worker Deployments and Services and routes them through the canonical agent-state model.
For `shared`, `user_agent`, and unscoped dedicated execution, the Kubernetes backend now mounts only the addressed agent root plus the worker runtime root.
`user` intentionally remains broader and mounts the shared `agents/` tree as a multi-agent workstation mode.
Idle cleanup currently scales workers to zero while preserving state and deletes the per-worker Service.
The long-term architecture may still move this behavior behind an external controller, but that is no longer a prerequisite for shipping the current provider model.
Each Kubernetes worker still needs durable runtime storage for caches plus access to the canonical agent state roots it executes against, as well as an authenticated internal endpoint.

## Provider Interface Contract

The worker manager should hide provider-specific details from the tool wrapper.
The tool wrapper should only need a resolved worker handle and an execution endpoint.
This keeps local and hosted providers aligned and testable with the same routing logic.

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

- Agent-isolated workers must be isolated by mounted storage boundaries rather than only by logical path conventions.
- Worker endpoints must be internal-only and authenticated.
- Logs and metrics should prefer worker-key hashes or short IDs rather than raw user identifiers.
- Shared scopes must be explicit rather than accidental fallbacks.
- A requester must never be able to route into another requester's worker by supplying untrusted identity fields.
- Credentials should only be loaded into the worker scope that is allowed to use them.
- Authoritative agent state must never silently fork into worker-local copies.
- Mounting the full shared agent-state tree into a `shared`, `user_agent`, or unscoped dedicated worker is a bug, not an acceptable simplification.
- Setting `base_dir` or the current working directory to the correct agent workspace is not enough if the runtime can still traverse to other agent roots.

## State Initialization Policy

Adopting worker scope must not create a new authoritative copy of agent state.
The system should preserve one canonical agent state root regardless of which runtime scope executes the tools.

The initialization rules are:

- Switching an agent between `shared`, `user`, `user_agent`, and unscoped dedicated execution keeps the same canonical agent state root.
- Agent-owned files must be addressed directly inside the canonical agent workspace, with no bootstrap copies or alternate authoritative locations.
- Worker runtime roots may always be recreated from scratch.
- Existing shared agent storage should be the migration target whenever a scoped implementation previously forked state by worker key.
- No compatibility fallback remains between legacy sandbox config and `worker_tools`.

## Observability

The system already exposes backend-neutral worker listing and idle cleanup through the primary runtime and local worker listing and cleanup through the sandbox-runner runtime.
Worker handles already carry startup counts, failure counts, failure reasons, timestamps, and provider debug metadata.
What is still missing is richer aggregate telemetry that can be scraped or graphed over time.

At minimum we need:

- Worker creation and reuse counts.
- Worker startup latency.
- Worker idle eviction counts.
- Execute request counts by tool and scope.
- Credential lease creation and expiration counts.
- Storage path resolution traces for debugging.
- Health and failure reason visibility for worker startup errors.

## What Is Left Now

The next work item is to realign the implementation with the clarified filesystem-visibility invariant.
Productionization work still matters, but it should not continue on top of the wrong storage model.

1. Enforce per-agent filesystem visibility in every worker backend path.
   `shared`, `user_agent`, and unscoped dedicated execution for agent A must only see agent A's canonical state root plus their own worker runtime root.
   `user` may remain broader only if it is intentionally kept as a multi-agent workstation mode.

2. Keep canonical agent state separate from worker runtime state in every provider path.
   Providers may still provision isolated runtimes and caches per worker key.
   They must not satisfy agent isolation merely by setting `base_dir` inside a broader shared mount.

3. Decide and implement the concurrency model for shared agent state.
   Some files may tolerate last-write-wins behavior.
   Sensitive artifacts need locking or a storage engine that already handles concurrent writers.

4. Revisit trusted `/v1` identity for isolating worker scopes.
   Current `/v1` requests still build execution identity with `requester_id=None`, and the API intentionally only supports unscoped agents and `worker_scope=shared`.
   Remaining work is to decide the authenticated principal source for `/v1` and then safely enable additional scopes.

5. Add aggregate metrics and dashboards.
   We still need counters and histograms for worker creation and reuse, startup latency, idle eviction counts, execute requests by tool and scope, and credential lease issuance and expiry.
   The current `/api/workers` surfaces are useful for debugging but are not sufficient for fleet-level observability.

6. Finish operator-facing documentation and dedicated-worker production validation.
   We still need final docs for retention and cleanup behavior per provider, storage layout and PVC expectations, deployment mode selection, incident handling for stuck or unhealthy workers, per-agent mount expectations, and concurrency behavior under real cluster conditions.

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
- Mount selection and filesystem visibility rules per scope.

Integration tests should cover:

- Package persistence across turns.
- File persistence across turns.
- File memory persistence across turns and across runtime scopes for the same agent.
- Mem0-backed state persistence across runtime scopes for the same agent.
- Session persistence across runtime scopes for the same agent.
- Learning persistence across runtime scopes for the same agent.
- A file edit performed by one runtime becomes visible to another runtime executing the same agent.
- Shared collaboration in the `shared` scope.
- A `shared` or `user_agent` worker for agent A cannot read or modify agent B's files by path.
- A `user` worker may access multiple agent roots only when that behavior is explicitly configured and documented as trust-sharing mode.

System tests should cover:

- Current local provider lifecycle and persistence behavior.
- Future provider realization and reattachment to state through the same worker contract.
- Worker eviction and recreation with preserved state.
- `/v1` behavior with and without trusted requester identity.

## Acceptance Criteria

- A user can install a package in one turn and use it later.
- A user can create files in one turn and use them later.
- File-backed memory edited from tools is visible in later turns from every runtime scope that executes the same agent.
- Mem0-backed state is visible from every runtime scope that executes the same agent.
- Session and learning state follow the agent's canonical state root rather than the worker runtime root.
- Two users of the same agent can execute in different runtimes when the scope is isolating, but they still see the same agent-owned files and memory for that agent.
- A `shared` or `user_agent` runtime for agent A cannot inspect or modify agent B's files through the filesystem.
- Unscoped dedicated execution for agent A cannot inspect or modify agent B's files through the filesystem.
- `user` is either intentionally documented as a multi-agent workstation mode or removed for filesystem-capable worker tools.
- Collaborative modes remain possible for `shared`.
- Tools that require primary-runtime services continue working locally.
- The routing layer remains generic for any worker-routed tool.

## Phase Plan

### Phase 1: Generic Worker Routing Prototype

Phase 1 is complete.
Phase 1 introduced `worker_scope`, worker key resolution, execution identity propagation, and generic worker-routed tool dispatch.
Phase 1 validated persistence with `shell`, `file`, and `python`.
Phase 1 introduced the first path-alignment work for worker-routed memory, which later evolved into the canonical agent-state work delivered in Phase 2.

### Phase 2: Agent-Owned Canonical State And Filesystem Visibility

Phase 2 is partially complete.
Canonical agent-owned state is implemented.
Per-agent filesystem visibility for agent-isolated scopes is still missing in some backend paths.
Phase 2 remains the correctness phase.

Phase 2 already delivered:

- Canonical agent-state resolution that is independent of worker scope.
- Routing `context_files`, workspace files, file-backed memory, mem0-backed state, sessions, and learning through that canonical agent state root.
- Separation of canonical agent state from worker runtime caches, virtualenvs, scratch files, and provider metadata.

Phase 2 remaining work is:

- Ensure `shared`, `user_agent`, and unscoped dedicated execution only expose the addressed agent root plus the worker runtime root.
- Treat `user` as a deliberate multi-agent workstation mode if it remains supported for filesystem-capable worker tools.
- Add explicit concurrent-writer handling for sensitive agent-owned artifacts.
- Keep Google Services, Spotify, Home Assistant, and the Google-backed tools shared-only until there is a dedicated scoped OAuth binding model.
- Keep dashboard credential management limited to unscoped and `shared` agents until there is a trusted identity-linking model between dashboard users and runtime worker requesters.

### Phase 3: Backend Contract, Policy, And Observability

Phase 3 is complete.
Phase 3 was the abstraction and policy phase.

Phase 3 delivered:

- Introduce a first-class backend-neutral worker manager and worker backend contract.
- Refactor routed tool execution to resolve a worker handle through that contract.
- Treat the current shared sandbox-runner deployment as the first concrete provider so Phase 2 behavior remains unchanged.
- Implement idle cleanup and state retention rules.
- Tighten `/v1` scope eligibility based on trusted requester identity.
- Add backend-neutral observability surfaces for active workers and worker failures.
- Add per-tool default execution targets so built-in routing defaults are explicit.

### Phase 4: Production Provider Implementations

Phase 4 is in progress.
Phase 4 is the provider-implementation and productionization phase.

Phase 4 already delivered:

- Implement a Kubernetes-backed worker provider.
- Attach durable storage to provider-managed workers.
- Add provider health checks and readiness handling.

Phase 4 remaining work is:

- Add aggregate metrics and dashboards for provider operations.
- Record dedicated-worker production validation, especially on GKE.
- Document retention, cleanup, and storage strategy per provider.
- Document incident handling for stuck or unhealthy workers.

## Recommended Immediate Next Step

The next step is to enforce the filesystem visibility half of the model that is still missing in current backends.
The highest-value target is to make `shared`, `user_agent`, and unscoped dedicated execution expose only the addressed agent root plus the worker runtime root, while keeping `user` explicitly broader only if that mode is intentionally retained.

## File Map For Remaining Work

- `src/mindroom/tool_system/worker_routing.py` is the source of truth for execution identity, scope semantics, worker keys, and the worker-path plumbing that must stay aligned with canonical agent-owned state and per-agent visibility rules.
- `src/mindroom/tool_system/metadata.py` is the source of truth for per-tool default execution targets and for the built-in default worker-routing policy.
- `src/mindroom/agents.py` already targets canonical agent-owned state and should stay aligned with the no-copy, workspace-relative contract.
- `src/mindroom/credentials.py` is now scope-aware and remains the place where runtime credential ownership rules should continue to consolidate.
- `src/mindroom/api/openai_compat.py` keeps enforcing conservative `/v1` scope eligibility and trusted requester identity rules.
- `src/mindroom/api/sandbox_runner.py` is one of the main places that still needs tighter runtime filesystem visibility rules for agent-isolated scopes.
- `src/mindroom/tool_system/sandbox_proxy.py` should resolve worker handles through the worker manager and stay free of provider-specific assumptions.
- `src/mindroom/workers/` is now the home of the backend-neutral worker contract plus the built-in `static_runner` and `kubernetes` backends that ship with core.
- `src/mindroom/workers/backends/kubernetes_resources.py` is the main Kubernetes manifest layer that must stop exposing broader shared storage to agent-isolated workers.
- `src/mindroom/api/workers.py` and the background cleanup loop in `src/mindroom/api/main.py` are the current backend-neutral observability and lifecycle surfaces.
- `cluster/k8s/instance/templates/deployment-mindroom.yaml` and related worker templates encode the shared-runner and dedicated Kubernetes-worker deployment shapes and must be checked against the per-agent mount rules in this plan.

## Open Decisions

- Decide the exact authenticated identity source for `/v1` user-scoped workers.
- Decide the final credential scope defaults for each class of worker-routed tool, with `user` as the current leading default when a trusted requester identity exists.
- Decide the long-term worker retention policy for hosted deployments.
- Decide whether explicit user-facing worker reset commands should exist in the product.
