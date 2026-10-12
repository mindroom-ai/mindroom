# Runtime Path Architecture Refactor Prompt

> **Archived prompt:** This artifact is retained for historical context and must not be used as current implementation guidance.

This record keeps the goals and constraints of the prompt used to delegate the config-path and storage-path architecture cleanup to an agent; the resulting runtime contract lives in the code.

## Why

MindRoom had recurring regressions in:

- config file path resolution;
- storage root resolution;
- config-adjacent `.env` loading and precedence;
- config-relative paths versus agent-owned paths;
- canonical durable agent state versus worker-local runtime state;
- validators accepting values that runtime later interpreted differently;
- import-time snapshots of mutable runtime state;
- alternate config loads leaking active runtime state into each other.

## Goal

Runtime config access gets one authoritative contract, so future work does not require touching many unrelated call sites.
The work was a runtime-context propagation refactor, not a path-only cleanup: path resolution, env loading, and import-time runtime snapshots had to be unified together.

## Contract rules

Each rule answers one of the regression classes above.

- Resolving a runtime context must not mutate process-global state and must be isolated from the active runtime except for explicitly passed overrides, so one config load cannot take path or env authority from another.
- Runtime env loads once with one documented precedence, where explicit runtime arguments come first and, below them, only values the shell truly exported may outrank the config-adjacent `.env`; runtime activation must not write path authority back into the process env where another resolution would mistake it for exported env.
- Each path domain (config-relative paths, agent-owned paths, durable agent state roots, worker runtime roots) has one resolver that fails closed: forms outside a domain's contract are rejected, never silently reinterpreted.
- Validation uses the same resolvers as runtime, so no value passes validation and then resolves differently.
- Durable agent state is independent of worker reuse mode: worker scope may change runtime reuse, never the authoritative location of agent state.
- Runtime code never depends on import-time snapshots of runtime-varying values; the runtime context is passed explicitly across boundaries, and direct env reads of runtime-varying keys stay confined to a small allowlist of named bootstrap and subprocess-entry modules.
- Removed tool-constructor env fallback is not part of the runtime contract.

## Constraints

- Merge and forget: do not preserve old path behavior that conflicts with the new contract, and add no compatibility branches for removed path semantics.
- Add no compatibility wrappers whose only purpose is to keep old tests passing, and no defensive wrapper layers.
- Use the smallest correct abstraction, with fewer primitives and clearer ownership rather than more helper layers or a framework.
- If the clean solution touches many call sites, touch them now rather than leaving mixed contracts in place.
- Do not stop after introducing the new abstraction: finish by removing duplicate resolution logic, obsolete helpers, and stale consumers of the old contract, so old and new systems are never both active.
