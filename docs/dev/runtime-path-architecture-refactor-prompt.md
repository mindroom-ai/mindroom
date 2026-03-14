# Runtime Path Architecture Refactor Prompt

Use this prompt when delegating the config-path and storage-path architecture cleanup to an agent.

```text
You are doing an architecture refactor, not a bug fix.

Goal:
Make runtime config access in MindRoom have one authoritative contract, so future work does not require touching many unrelated call sites.
Treat this as a runtime-context propagation refactor, not a path-only cleanup.
Path resolution, env loading, and import-time runtime snapshots must be unified together.
Do this as a merge-and-forget refactor.
Do not preserve old path behavior when it conflicts with the new contract.
Do not add compatibility wrappers whose only purpose is to keep old tests passing.

Context:
This repo keeps regressing on:
- config file path resolution
- storage root resolution
- config-adjacent .env loading and precedence
- config-relative paths vs agent-owned paths
- canonical durable agent state vs worker-local runtime state
- validators accepting values that runtime later interprets differently
- import-time snapshots of mutable runtime state
- alternate config loads leaking active runtime state into each other

Design target:
Use a small number of explicit runtime contracts and make them the only source of truth.

Required architecture:
1. There must be one process/runtime context object, `RuntimePaths` or a direct successor, that is the only authority for:
   - active config path
   - config dir
   - env path
   - shared storage root
   - any runtime-derived setting that is allowed to vary by runtime context, either directly on the object or through runtime-aware accessors

2. There must be a pure "resolve runtime context" path and a separate "activate runtime context" path.
   Resolving a runtime context must compute paths, env inputs, and derived settings without mutating process-global state.
   Activating a runtime context is the only place allowed to sync that resolved context into process-global state, if any global sync remains at all.

3. Bootstrap must load runtime env once when activating a runtime context, with one documented precedence order.
   Use explicit runtime arguments first.
   Then use true shell-exported process env vars, not process env previously mutated by runtime activation.
   Then use the config-adjacent `.env`.
   Then use code defaults.
   `Config.from_yaml()` must be pure with respect to process env and must not mutate `os.environ`.
   Either runtime activation must not write path authority back into `os.environ`, or runtime-synced env values must carry separate provenance and must not be treated as ordinary exported process env for foreign runtime resolution.

4. Resolving a temporary or explicit runtime context must be isolated from the active runtime except for explicitly passed overrides.
   Previously active `MINDROOM_CONFIG_PATH`, `MINDROOM_STORAGE_PATH`, or other active runtime state must not supply path or env authority for another config load.

5. There must be one explicit resolver per path domain:
   - config-relative paths
   - canonical durable agent-owned paths
   - canonical durable agent state roots
   - worker runtime roots
   Define exactly which domains accept placeholders.
   `config-relative` paths may allow `${MINDROOM_STORAGE_PATH}` and `${MINDROOM_CONFIG_PATH}` only if that is part of the intended contract.
   `agent-owned` paths must be workspace-relative only.
   `agent-owned` paths must reject absolute paths and all env-var placeholder forms.
   `worker runtime` paths must be backend-controlled only and not user-configurable through agent-owned path fields.

6. Validation and runtime resolution must use the same logic.
   Validators should call the same resolver functions used at runtime, or the exact same normalization pipeline, rather than reimplementing equivalent checks.
   If a value would resolve incorrectly at runtime, validation must reject it.
   No "validator accepts, runtime interprets differently" situations.

7. Durable agent state must be completely independent from worker reuse mode.
   Worker scope may affect runtime reuse, not the authoritative filesystem location of agent state.

8. No runtime code may depend on import-time snapshots of mutable runtime path or env state.
   This includes `CONFIG_PATH`, `STORAGE_PATH_OBJ`, `MATRIX_HOMESERVER`, `MATRIX_SSL_VERIFY`, `MINDROOM_NAMESPACE`, `ENABLE_AI_CACHE`, `MINDROOM_CONFIG_TEMPLATE`, and similar env-derived module constants when their values are allowed to vary by runtime context.
   Such values must either live on `RuntimePaths` or be resolved through runtime-aware accessors.
   Bootstrap-only compatibility shims are allowed only in narrowly approved startup code.
   Pass `RuntimePaths` explicitly across runtime boundaries instead.

9. No non-bootstrap runtime code may read runtime-varying keys directly via `os.getenv()` or `os.environ`.
   Runtime-varying keys must be read through the central runtime resolver or runtime-aware accessors.
   The only exceptions are explicitly approved bootstrap or subprocess-entry modules, enforced by a small explicit allowlist.

10. Eliminate duplicate/local runtime resolution logic.
   If a module reconstructs config/storage/agent path behavior itself, delete that logic and route through the central resolver.
   If a module loads or caches runtime env state itself, delete that logic and route through the central runtime context.

11. Fail closed.
   Reject absolute paths, env-var placeholders, or ambiguous forms where they are not part of the allowed domain contract.
   Do not silently reinterpret them.

Implementation requirements:
- First do a full grep sweep for:
  - direct imports or uses of `CONFIG_PATH`, `STORAGE_PATH_OBJ`, and other runtime globals
  - direct imports or uses of env-derived constants in non-bootstrap runtime modules
  - direct `os.getenv()` / `os.environ[...]` reads of runtime-varying keys in non-bootstrap runtime modules
  - local reconstruction of config dir, `.env`, `mindroom_data`, `agents/`, `workers/`, or similar runtime paths
  - any validator or loader that normalizes paths separately from the main runtime resolvers
- Then classify each usage into one of the runtime/path domains above.
- Then refactor to the minimal correct abstraction.
- Prefer fewer primitives and clearer ownership over more helper layers.
- Remove obsolete helpers after consolidation.
- Do not leave old and new systems both active.

Guardrails to add:
- Tests that changing config path and storage path only requires changing `RuntimePaths` creation, not module-specific logic.
- Tests that validation and runtime resolution are identical for agent-owned paths.
- Tests that placeholder acceptance and rejection is enforced per path domain.
- Tests for rejection of invalid forms such as `$MINDROOM_STORAGE_PATH/...` when workspace-relative agent-owned paths are required.
- Tests for worker-scoped execution still using canonical durable agent state.
- A two-workspace regression test that activates workspace A, then loads workspace B, and proves B resolves against B's own config path, sibling `.env`, and storage root.
- A test that modules imported before `set_runtime_paths()` still behave correctly after the runtime context changes.
- A test proving an explicitly exported shell env var is not overwritten by config-adjacent `.env` loading.
- A regression test for config seeding from a sibling `.env` `MINDROOM_CONFIG_TEMPLATE`.
- A regression test or lint-style test that prevents new direct runtime use of import-time runtime globals outside approved bootstrap modules.
- A regression test or lint-style test that prevents new direct `os.getenv()` / `os.environ` reads of runtime-varying keys outside approved bootstrap or subprocess-entry modules.
- Define the approved-module allowlist explicitly in the test/lint helper and keep it small.
- The allowlist should be concrete, for example startup-only modules such as `constants.py`, CLI startup, orchestrator bootstrap, and dedicated subprocess-entry code, rather than an open-ended concept of "bootstrap".

Deliverables:
1. Implement the refactor.
2. Add/adjust tests.
3. Add a short architecture note in the relevant code comments or docs describing the runtime context model, env precedence, path domains, and source of truth.
4. Run the relevant test suite.
5. In the final summary, include:
   - the final runtime context model
   - the final env precedence model
   - the final path-domain model
   - what duplicate logic was removed
   - what stale import-time runtime access was removed
   - what guardrails now prevent future regressions

Important constraints:
- Smallest correct abstraction, not a giant framework.
- No defensive wrapper layers.
- No backwards-compatibility branches for removed path semantics.
- If the clean solution requires touching many call sites, do it now rather than leaving mixed contracts in place.
- Do not stop after introducing the new abstraction.
- Finish the refactor by removing stale consumers of the old runtime contract.
```
