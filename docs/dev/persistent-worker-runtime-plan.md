# Persistent Worker Runtime Plan

Last updated: 2026-03-11
Owner: MindRoom backend
Status: Phases 1-3 are complete in code.
Phase 4 is in progress.
The backend-neutral worker contract, lifecycle handling, and default routing policy are implemented.
The built-in Kubernetes provider is also implemented and deployment-wired.
What remains is production hardening, richer metrics, operator documentation, dedicated-worker production validation, and deferred product-boundary decisions around `/v1` identity and credential defaults.

## Objective

Implement persistent worker-scoped execution environments for MindRoom tools.
Keep the visible agent as the shared product abstraction users talk to.
Move mutable execution state into worker-owned storage selected per tool call by policy.
Support user-isolated, room-isolated, and shared collaboration modes without building one full MindRoom runtime per user.

## Why This Is Split Into Phases

Phase 1 proves the core architecture can route generic tool calls into persistent scoped workers.
Phase 2 makes all mutable state obey the same scope so the system becomes correct rather than merely demonstrable.
Phase 3 introduces the backend/provider abstraction, lifecycle model, policy surface, and observability once the state model is sound.
Phase 4 adds production provider implementations against that abstraction, including Kubernetes.
Doing the work in this order prevents expensive lifecycle and deployment work from being built on top of an unproven routing model.

## Current Status

Phase 1 is implemented and already useful for proving persistent tool execution.
The implementation provides generic worker routing for tool calls rather than a `shell`-only special case.
The implementation validates persistence with `shell`, `file`, and `python`.
Phase 2 has been smoke-validated in GKE on the shared provider shape of one shared MindRoom pod, one shared sandbox-runner sidecar, and one shared PVC.
That smoke validation confirmed same-worker-key persistence across turns, isolation across different worker keys, persistent Python environments, and survival of worker-owned state across pod replacement.
The implementation carries execution identity from Matrix and from the currently permitted `/v1` surface into worker-routing decisions.
The implementation persists worker workspace, cache, and Python packages inside worker-owned state.
The implementation aligns file-backed memory reads and writes with worker-owned state for worker-scoped agents.
For file-backed agents that set `memory_file_path`, local workspace-aware tools now derive their base directory from that same path.
Worker-routed scoped tools still execute against the resolved worker workspace, so the agent-level workspace hint does not bypass worker-owned state.
Sessions, learning, and most credentials are now worker-scope-aware.
Phase 3 is implemented in code.
The worker backend contract, worker manager facade, worker handle model, and handle-based routed execution are the active runtime architecture.
The primary runtime now ships built-in `static_runner` and `kubernetes` backends behind the same contract.
The primary runtime exposes backend-neutral worker observability and cleanup endpoints through `/api/workers`.
The primary runtime also runs optional background idle cleanup using the configured backend.
The built-in default worker-routing policy now comes from per-tool metadata plus agent/default `worker_tools` overrides rather than only from environment-driven sandbox selection.
Phase 4 has started.
The built-in Kubernetes backend now provisions dedicated worker Deployments and Services, mounts durable worker state from the shared PVC by worker subpath, waits for readiness, records failure metadata, and evicts idle workers by scaling them to zero while keeping state.
The current codebase and test suite therefore already include a production-provider implementation, even though some rollout and operator work remains.
This document does not claim a recent dedicated-worker GKE smoke or soak validation for the Kubernetes backend.
Google Services, Spotify, Home Assistant, and the Google-backed `gmail`, `google_calendar`, and `google_sheets` tools remain shared-only.
Those integrations are supported only for agents without worker routing or with `worker_scope=shared`.
Dashboard credential management is intentionally limited to unscoped agents and agents with `worker_scope=shared`.
The dashboard does not manage credentials for `user`, `user_agent`, or `room_thread` workers.
The `/v1` API remains intentionally restricted to unscoped agents and agents with `worker_scope=shared` until trusted requester identity is solved.

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
Current `/v1` behavior only allows unscoped agents and agents with `worker_scope=shared`.
That restriction is intentional and remains in place until trusted requester identity is solved.
User-scoped `/v1` workers do not currently ship as a supported product behavior.
Future `/v1` support should allow additional worker scopes only when a trusted authenticated principal is present.
One intended final rule is that `shared` and possibly session-derived `room_thread` can work without a trusted user principal, while `user` and `user_agent` require one.
That `room_thread` expansion is not implemented today and should not be treated as current `/v1` behavior.
The dashboard also has an authenticated user identity, but that identity is a dashboard principal rather than the Matrix requester identity used by runtime worker routing.
That means the dashboard must not read or write credentials for `user`, `user_agent`, or `room_thread` workers until there is a deliberate identity-linking model for those scopes.

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
- Support worker eviction while preserving worker-owned state by default.
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

What Phase 2 proved is worker-keyed state resolution plus runtime overrides that make mutable state land in the correct worker-owned locations.
MindRoom core should describe the ownership boundary in those terms rather than assume one blanket implementation such as "the worker process always runs with worker root as `MINDROOM_STORAGE_PATH`."
Concrete providers may realize the same contract through storage-path overrides, tool runtime overrides, mounted volumes, or other provider-specific mechanisms.
The worker workspace should remain at `<worker_root>/workspace`.
The persistent Python environment should remain at `<worker_root>/venv`.
Caches should stay inside `<worker_root>/cache`.
Non-secret tool init overrides such as `base_dir` are allowed only as a narrow transport mechanism for workspace-aware tools.
Those overrides must be explicitly whitelisted, type-validated before toolkit construction, and rejected with a client error when malformed.
For worker-routed execution, runtime worker workspace resolution remains authoritative over any forwarded tool init override.

## Memory Design

If worker-routed tools can edit memory, memory reads during prompt assembly must resolve against the same worker-owned storage.
The current implementation already does this for file-backed memory.
The full design should keep file-backed memory as the only worker-editable memory backend until another backend has a deliberate synchronization model.

The final memory rules are:

- Worker-scoped agents that allow worker-editable memory must use `memory.backend=file`.
- File memory for worker-scoped agents resolves relative to the worker storage root.
- Shared agent memory outside worker scope remains supported for non-worker-routed agents.
- Shell and file tools must not directly mutate non-file memory backends.
- Room memory should only be auto-written into shared scopes when that sharing is intentional.

## Sessions And Learning

Sessions and learning are already worker-scope-aware.
The storage resolver now chooses paths from the same worker-owned state boundary when a worker-scoped agent is active.
That remains a path-resolution concern rather than something duplicated across call sites.

The steady-state rules are:

- Session SQLite paths resolve into worker-owned state when the active agent has a worker scope.
- Learning SQLite paths resolve into worker-owned state when the active agent has a worker scope.
- Non-worker-scoped agents continue using the existing shared per-agent locations.
- Prompt assembly and tool execution must agree on the same session and learning scope.

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
Google Services, Spotify, Home Assistant, and the Google-backed `gmail`, `google_calendar`, and `google_sheets` tools are intentionally unsupported for `user`, `user_agent`, and `room_thread`.
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

MindRoom core owns worker scope semantics, execution identity resolution, worker key resolution, tool routing policy, and worker-owned state semantics.
MindRoom core must not leak provider-specific lifecycle details into the generic routing layer.
MindRoom core now ships the interface and the built-in `static_runner` and `kubernetes` backends needed for current development and hosted deployment shapes.
The local development model, the shared sandbox-runner deployment, and the dedicated Kubernetes-worker deployment are providers behind the same worker backend contract.
The current codebase therefore treats Kubernetes as a built-in provider, not only a future idea.
A future external provider or controller can still consume the same contract without changing core routing logic.

## Local Provider

The local provider should preserve the current Phase 2 behavior of one primary MindRoom runtime plus a shared sandbox-runner that realizes logical workers from worker keys.
The local provider may realize many logical workers inside one runtime process as long as worker-owned state remains isolated by worker key.
The local provider should support introspection of active workers and cleanup of idle workers for debugging.

## Kubernetes Provider

The Kubernetes provider is implemented against the worker backend contract introduced in Phase 3.
The current implementation creates dedicated worker Deployments and Services, mounts durable worker-owned storage from the shared PVC via worker-specific subpaths, propagates the shared sandbox token, and waits for readiness before returning a worker handle.
Idle cleanup currently scales workers to zero while preserving state and deletes the per-worker Service.
The long-term architecture may still move this behavior behind an external controller, but that is no longer a prerequisite for shipping the current provider model.
Each Kubernetes worker still needs durable worker-owned storage and an authenticated internal endpoint, and the current implementation satisfies that contract.

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

- Worker state must be isolated by mounted storage boundaries rather than only by logical path conventions.
- Worker endpoints must be internal-only and authenticated.
- Logs and metrics should prefer worker-key hashes or short IDs rather than raw user identifiers.
- Shared scopes must be explicit rather than accidental fallbacks.
- A requester must never be able to route into another requester's worker by supplying untrusted identity fields.
- Credentials should only be loaded into the worker scope that is allowed to use them.
- Worker-owned mutable state must never silently fall back to shared primary-runtime state.

## State Initialization Policy

Adopting worker scope is a clean break from old shared agent-scoped mutable state.
The system does not migrate or import old sessions, learning data, or file memory into worker-owned storage.

The initialization rules are:

- Agents without worker scope keep existing storage untouched.
- Agents that opt into any worker scope start with fresh worker-owned mutable state.
- `shared`, `user`, `user_agent`, and `room_thread` all use newly scoped storage rather than automatic migration.
- No explicit import flow is part of the current design.
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

The remaining work is no longer core routing architecture.
It is productionization, deferred policy decisions, and operator experience.

1. Trusted `/v1` identity for isolating worker scopes.
   Current `/v1` requests still build execution identity with `requester_id=None`, and the API intentionally only supports unscoped agents and `worker_scope=shared`.
   Remaining work is to decide the authenticated principal source for `/v1`, decide whether `room_thread` should key from conversation or session identity, and then safely enable additional scopes.

2. Finalize explicit credential policy defaults.
   Credentials are already scope-aware and leased to workers, but the long-term default policy by tool class is still not fully codified as a first-class model.
   The main remaining decision is when worker-routed tools should default to `user`, `shared`, or another explicit credential scope.

3. Add aggregate metrics and dashboards.
   We still need counters and histograms for worker creation and reuse, startup latency, idle eviction counts, execute requests by tool and scope, and credential lease issuance and expiry.
   The current `/api/workers` surfaces are useful for debugging but are not sufficient for fleet-level observability.

4. Do dedicated-worker production validation beyond unit and integration coverage.
   The Kubernetes backend is implemented and tested, but this document should still treat dedicated-worker GKE smoke and soak validation as remaining rollout work unless and until it is recorded explicitly.
   The most valuable checks are end-to-end persistence across worker recreation, failure injection, restart handling, and concurrency or capacity behavior under real cluster conditions.

5. Finish operator-facing documentation.
   We still need final docs for retention and cleanup behavior per provider, storage layout and PVC expectations, deployment mode selection, and incident handling for stuck or unhealthy workers.

6. Decide optional product affordances.
   Open decisions remain around user-facing worker reset commands, long-term retention controls, and whether any of that should surface in the dashboard or chat UX.

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

- Current local provider lifecycle and persistence behavior.
- Future provider realization and reattachment to state through the same worker contract.
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

Phase 2 implemented the core scope-aware state changes needed for worker routing correctness.
Phase 2 is the correctness phase.

Phase 2 delivered:

- Add worker-aware session storage resolvers.
- Add worker-aware learning storage resolvers.
- Refactor credentials storage and lookup to be scope-aware.
- Refactor credential lease issuance to target the resolved worker endpoint.
- Enforce file-backed memory for worker-editable agents.
- Block direct worker mutation of unsupported memory backends.
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

The next step is to finish the remaining productionization work rather than redesign the routing model again.
The highest-value targets are richer metrics, dedicated-worker GKE smoke and soak validation, provider and incident documentation, and the deferred `/v1` identity plus credential-policy decisions.

## File Map For Remaining Work

- `src/mindroom/tool_system/worker_routing.py` is the source of truth for execution identity, scope semantics, worker keys, and scoped path helpers.
- `src/mindroom/tool_system/metadata.py` is the source of truth for per-tool default execution targets and for the built-in default worker-routing policy.
- `src/mindroom/agents.py` now resolves session and learning storage through worker-aware paths and remains the place to keep agent construction aligned with scoped state.
- `src/mindroom/credentials.py` is now scope-aware and remains the place where runtime credential ownership rules should continue to consolidate.
- `src/mindroom/api/openai_compat.py` keeps enforcing conservative `/v1` scope eligibility and trusted requester identity rules.
- `src/mindroom/api/sandbox_runner.py` should remain an execution runtime component over the worker backend contract rather than a lifecycle owner.
- `src/mindroom/tool_system/sandbox_proxy.py` should resolve worker handles through the worker manager and stay free of provider-specific assumptions.
- `src/mindroom/workers/` is now the home of the backend-neutral worker contract plus the built-in `static_runner` and `kubernetes` backends that ship with core.
- `src/mindroom/api/workers.py` and the background cleanup loop in `src/mindroom/api/main.py` are the current backend-neutral observability and lifecycle surfaces.
- `cluster/k8s/instance/templates/deployment-mindroom.yaml` and related worker templates now encode both the shared-runner and dedicated Kubernetes-worker deployment shapes.

## Open Decisions

- Decide the exact authenticated identity source for `/v1` user-scoped workers.
- Decide the final credential scope defaults for each class of worker-routed tool, with `user` as the current leading default when a trusted requester identity exists.
- Decide whether `room_thread` on `/v1` should key from conversation ID, session ID, or a distinct thread identifier.
- Decide the long-term worker retention policy for hosted deployments.
- Decide whether explicit user-facing worker reset commands should exist in the product.
