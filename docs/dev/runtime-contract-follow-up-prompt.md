# Runtime Contract Follow-Up Prompt

Use this prompt when delegating the next runtime-architecture follow-up PR after the runtime path refactor.

```text
You are doing a follow-up architecture refactor, not a bug fix.

Read first:
- CLAUDE.md
- docs/dev/runtime-path-architecture-refactor-prompt.md

Goal:
Collapse the remaining split between `Config` and `RuntimePaths`.
The next PR should make runtime dependencies explicit in the helper layers that still reach through `Config.require_runtime_paths()`.
Treat this as a contract cleanup.
Do not reopen already-fixed runtime precedence bugs unless the new refactor requires touching them.
Do not add wrappers or fallback branches just to preserve old tests.

Context:
The previous refactor fixed the concrete correctness bugs around:
- explicit `config_path` plus exported `MINDROOM_STORAGE_PATH`
- bundled API import-time runtime initialization
- runtime-aware config validation on write paths
- tests masking runtime bugs by auto-binding every `Config`

The remaining issue is architectural.
The code still has two internal concepts mixed together:
- plain parsed config
- runtime-bound behavior reached through `Config`

That makes the runtime contract harder to explain than it should be.

Design target:
`RuntimePaths` is the runtime contract.
`Config` is configuration data.
If behavior depends on runtime, the code should take `RuntimePaths` explicitly instead of reaching through `Config.require_runtime_paths()`.

Required architecture:
1. `Config` must become pure data in internal application code.
   Remove `_runtime_paths`, `runtime_paths`, and `require_runtime_paths()` if the clean solution allows it.
   If temporary compatibility is absolutely necessary, keep it only as a narrow transition layer and remove all non-bootstrap helper usage in this PR.

2. Runtime-sensitive helpers must take `RuntimePaths` explicitly.
   Do not make helper behavior depend on whether the caller happened to pass a runtime-bound `Config`.
   A function that needs runtime should say so in its signature.

3. Do not pass both `runtime_paths` and redundant fields already inside it.
   If `runtime_paths` is passed, do not also pass `config_path`, `storage_path`, or env-derived values that are already part of the runtime object.

4. Keep runtime creation at process boundaries only.
   CLI, API startup, orchestrator startup, and similar top-level entrypoints may create the primary runtime context.
   Deeper helpers must not recreate runtime from partial inputs.

5. Make the `Config` contract obvious in tests too.
   Plain `Config(...)` must stay runtime-free by default.
   Tests that need runtime-sensitive behavior must construct `RuntimePaths` explicitly and pass it.

6. Prefer direct runtime threading over helper indirection.
   If a helper only exists to pull runtime out of `Config` and call another function, delete that indirection and thread `RuntimePaths` directly.

Scope for this PR:
- Refactor the highest-value helper layers that still reach runtime through `Config`.
- The expected starting points include:
  - `src/mindroom/bot.py`
  - `src/mindroom/ai.py`
  - `src/mindroom/authorization.py`
  - `src/mindroom/matrix/rooms.py`
  - `src/mindroom/matrix/users.py`
  - `src/mindroom/matrix/mentions.py`
  - `src/mindroom/avatar_generation.py`
  - `src/mindroom/voice_handler.py`
  - `src/mindroom/thread_utils.py`
  - any directly related config helpers

Non-goals for this PR unless they become directly necessary:
- a full tool-construction redesign
- a full model-factory redesign
- deleting env-sync compatibility for subprocesses and third-party libraries

Those can be separate follow-up PRs.
This PR should focus on making the internal runtime contract singular and explicit in the core helper layers.

Implementation requirements:
- First grep for:
  - `require_runtime_paths()`
  - `.runtime_paths`
  - helper functions that receive `Config` only to derive runtime
  - functions that now need `RuntimePaths` in their signature
- Classify each usage as one of:
  - pure config
  - runtime-sensitive helper
  - process-boundary bootstrap
- For runtime-sensitive helpers, thread `RuntimePaths` explicitly.
- Remove dead compatibility code after the new call flow is in place.
- Keep signatures simple.
- Do not make `runtime_paths` keyword-only unless there is a real ambiguity problem.
- Do not use `getattr()` or `hasattr()` to paper over typed contract changes.
- Fix mocks and tests instead of weakening production types.

Guardrails to add:
- A regression test proving plain `Config(...)` is still runtime-free.
- Tests that runtime-sensitive helper flows fail clearly when runtime is not threaded.
- Tests that the updated helpers work when given explicit `RuntimePaths`.
- A grep-style or AST-style guardrail, if useful, that prevents new helper-layer uses of `Config.require_runtime_paths()` outside an explicitly small allowlist during the transition.
- Update existing tests to use explicit runtime binding rather than ambient behavior.

Deliverables:
1. Implement the refactor.
2. Update tests.
3. Add a short architecture note in code comments or docs describing the new split:
   - `Config` is pure config
   - `RuntimePaths` is runtime context
   - process boundaries create runtime
   - helper layers receive runtime explicitly
4. Run the relevant test suite.
5. In the final summary, include:
   - what `Config` no longer owns
   - which helpers now take `RuntimePaths`
   - what redundant or wrapper plumbing was removed
   - what guardrails now protect the contract

Important constraints:
- Smallest correct abstraction.
- No compatibility branches just for old call sites.
- No new ambient runtime access.
- No defensive wrapper layers.
- Finish the slice fully instead of leaving mixed contracts active in the touched area.
```
