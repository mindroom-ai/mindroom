# Post-Merge Refactor Priorities

This document lists the next refactors that should happen soon after the current hook, plugin, and config-hardening branch lands.

It is intentionally short and prioritized.

The goal is to preserve the improvements from this branch and reduce the chance that the same bad patterns reappear.

## Completed In This PR

These items were the pre-merge stabilization plan for this branch.

They are now implemented in the current branch.

### 1. Backend API Snapshot And Generation Refactor

Files:

- `src/mindroom/api/main.py`
- `src/mindroom/api/config_lifecycle.py`
- `tests/api/test_api.py`

What landed:

- Most recurring review findings have been caused by runtime paths, config data, config load result, and auth state being published separately.
- The API now publishes one snapshot object under one stable app state holder.
- Reads, writes, and reloads now bind to one current snapshot after locking and reject stale generations.

Outcome:

- Runtime swaps now publish one coherent snapshot instead of mutating multiple fields in place.
- Late loads and writes now become generation mismatches instead of stale partial commits.
- Auth-protected dashboard routes now bind one request snapshot and stay on that snapshot for protected reads and writes.
- Runtime refresh operations now reject stale requests before mutating the losing runtime.
- Focused tests now cover read-during-swap, write-during-swap, stale load completion, and stale write attempts after failure publication.

### 2. Plugin Validation Isolation From Live Registry State

Files:

- `src/mindroom/tool_system/metadata.py`
- `src/mindroom/tool_system/plugins.py`
- `src/mindroom/config/main.py`
- `tests/test_plugins.py`

What landed:

- Plugin validation and runtime loading are still the other major source of stale-global-state bugs.
- Validation now resolves plugin tool metadata through a separate registration sink instead of mutating the live registry.
- Runtime activation still applies the committed plugin overlay transactionally.

Outcome:

- Validation no longer snapshots and restores the live tool registry.
- Runtime activation still commits one explicit plugin overlay.
- Tests now cover add/remove/re-add, manifest rename, export rename, duplicate names, collisions, and rollback paths.

### 3. Frontend Loaded Config / Draft Config Split

Files:

- `frontend/src/store/configStore.ts`
- `frontend/src/services/configService.ts`
- `frontend/src/App.tsx`
- `frontend/src/components/**`
- `frontend/src/store/configStore.test.ts`

What landed:

- The frontend previously had the same bug-generating shape as the backend did: one implicit state object standing in for loaded config, draft config, save status, and invalid-load state.
- The store now separates `loadedConfig` from the editable draft and versions draft mutations explicitly.
- `saveConfig()` now returns an explicit result contract.
- Invalid-load recovery is now an explicit raw-config mode instead of a synthetic replacement draft.

Outcome:

- Overlapping loads, saves, and policy refreshes are now request-versioned.
- Save callers such as Knowledge and Voice now use the explicit save result contract.
- Draft versioning now covers tool overrides and other persisted sidecar draft state.
- Focused tests now cover overlapping saves, edits during save, failed save contracts, and invalid-load recovery behavior.

## Why These Items

This branch fixed recurring regressions in four areas.

- Plugin tool registration and reload behavior.
- User-facing config-load error handling.
- Frontend stale-state handling during async config refresh.
- Hook ingress and runtime-context plumbing.
- API runtime rebinding for long-lived background tasks.

The fixes are now materially better than `origin/main`.

The remaining work should focus on reducing the number of legal ways to bypass those good patterns.

## Priority 1: Consolidate Request Snapshot Helpers

Files:

- `src/mindroom/api/main.py`
- `src/mindroom/api/config_lifecycle.py`
- `src/mindroom/api/credentials.py`
- `src/mindroom/api/knowledge.py`
- `src/mindroom/api/schedules.py`
- `src/mindroom/api/workers.py`

Current state:

- The helper layer now has a coherent snapshot model, and protected dashboard routes bind one auth-bearing request snapshot before reading or writing committed state.
- The old auth-under-one-snapshot and execute-under-another bug class is now covered directly in request-level tests.
- The remaining work here is cleanup, not correctness: there are still multiple similarly named app-scoped and request-scoped helpers across `main.py` and `config_lifecycle.py`.

Why this is next:

- The current model is correct, but the helper surface is still easy to misuse.
- Future route work is safer if request code has one obvious helper family and app-scoped variants are harder to call by accident.

Refactor target:

- Keep one obvious request-scoped helper path for route code that needs authenticated user plus committed runtime/config state together.
- Reduce or clearly internalize app-scoped helper variants that should not be used from request handlers.
- Keep request-time tool metadata resolution non-mutating and runtime-scoped.

Acceptance criteria:

- Request handlers use one obvious request-scoped helper family for committed config and runtime reads/writes.
- App-scoped helpers are only used from app wiring, background tasks, or explicit non-request code paths.
- Request-time helpers do not re-read runtime or auth state when a bound request snapshot is already available.

## Priority 2: Simplify Plugin Tool Loading

Files:

- `src/mindroom/tool_system/plugins.py`
- `src/mindroom/tool_system/metadata.py`

Current state:

- Plugin tool state is now transactional and explicit enough to be correct.
- The code still depends on import-time decorator registration to discover plugin tools.
- There are still several moving parts, including module caches, manifest caches, per-module tool metadata, and the committed live overlay.

Why this is next:

- This is still the most complex seam touched by the branch.
- It was the source of the largest cluster of regressions during review.
- Future changes here are still more likely than average to reintroduce stale-state bugs.
- The current lock-based validation safety is correct enough for merge, but it is not the end-state design.

Refactor target:

- Make plugin loading build an explicit candidate tool-registration map before commit.
- Make validation resolve tool metadata without mutating the live process-global registry at all.
- Reduce reliance on ambient module side effects as the durable source of truth.
- Keep built-in tools as one base layer and plugin tools as one committed overlay.
- Keep collision checks centralized and unconditional.

Acceptance criteria:

- One active plugin load path builds candidate registrations first and commits once.
- Validation reads stay fully isolated from the live registry.
- Plugin add, remove, re-add, manifest rename, and export rename all work from the same model.
- Tool-name collisions fail at one clear boundary.
- Tests cover whole-plugin removal, intra-plugin duplicate names, cross-plugin collisions, manifest-only rename, and failed multi-plugin load rollback.

## Priority 3: Reduce Frontend Async Store Duplication

Files:

- `frontend/src/store/configStore.ts`
- `frontend/src/services/configService.ts`
- `frontend/src/App.tsx`

Current state:

- The store now has `loadedConfig`, a mutable draft, explicit recovery mode, and request sequencing for load/save/policy refresh.
- The sequencing and diagnostic-retention patterns are still duplicated across a large store file.
- Raw recovery editing now exists, but it still lives inline inside the main config store and app shell.

Why this is next:

- This exact stale-state class has already recurred more than once.
- The frontend behavior is now correct enough, but it is still more implicit and duplicated than it should be.

Refactor target:

- Extract one small internal helper for request-versioned async actions.
- Make recovery mode one explicit sub-state instead of logic distributed across `configStore.ts` and `App.tsx`.
- Keep save-result handling explicit so components do not guess from store side effects.

Acceptance criteria:

- `loadConfig()`, `refreshAgentPolicies()`, and `saveConfig()` share one clear sequencing pattern.
- Overlapping success, validation failure, and generic failure cases are all covered in store tests.
- A future async store action should have one obvious way to avoid stale commits.

## Priority 4: Move API Background Runtime Lifecycles Out Of `main.py`

Files:

- `src/mindroom/api/main.py`
- `src/mindroom/api/config_lifecycle.py`

Current state:

- The config watcher and worker cleanup loop now read current runtime state instead of closing over startup `RuntimePaths`.
- The lifecycle policy still lives in `api.main`.
- Config cache commits are now generation-checked and runtime-mismatch loads are discarded, but the sequencing still spans multiple helpers in `config_lifecycle.py`.

Why this is next:

- The rebinding bug showed that long-lived API tasks and request-time runtime state need one owner.
- Keeping the lifecycle policy in `main.py` makes it easier for future runtime-refresh changes to miss a sibling background task.
- The config lifecycle code now has the right locking semantics, but the atomic commit model is still not obvious at a glance.

Refactor target:

- Move app-bound config watching and runtime-bound background loop helpers into a small lifecycle module next to `config_lifecycle.py`.
- Keep `main.py` focused on route assembly and app wiring.
- Collapse config-load staging and commit into one clearly named helper so late results cannot drift back into ad hoc state writes.

Acceptance criteria:

- Request-time runtime refresh and background loops share the same app-bound runtime source of truth.
- Long-lived tasks do not capture startup runtime paths directly.
- Tests cover runtime rebinding for watcher and cleanup loops at the module boundary.

## Priority 5: Continue Shrinking `bot.py`

Files:

- `src/mindroom/bot.py`
- `src/mindroom/hooks/ingress.py`
- `src/mindroom/tool_system/runtime_context.py`
- `src/mindroom/commands/handler.py`

Current state:

- Hook ingress policy moved out of `bot.py`, which was the right first extraction.
- `bot.py` is still a large integration file with multiple responsibilities.

Why this is next:

- Large integration files make it easier for narrowly correct fixes to miss sibling paths.
- The branch already showed that runtime-context and message-normalization seams drift when they live half in `bot.py` and half elsewhere.

Refactor target:

- Move the next pure helper layer out of `bot.py`, not the orchestration itself.
- Good candidates are response-runtime assembly or message normalization helpers that are already conceptually shared.
- Avoid extracting wrappers that only add indirection.

Acceptance criteria:

- The extracted code owns a real policy or builder concern.
- `bot.py` loses branching and duplicated adapter logic, not just line count.
- Tests stay at the behavior boundary, not the helper boundary.

## What Not To Do

- Do not start a broad plugin-system rewrite unrelated to current pain points.
- Do not add more wrappers whose only job is to adapt one call signature to another.
- Do not keep both safe and unsafe helper variants public if only one should be used in normal code.
- Do not mix new feature work into these cleanup PRs.

## Testing Expectations For These Refactors

- Prefer invariant-style regression tests over single-endpoint spot tests.
- Add one test for the whole bug class when possible, not one more test for one missed sibling.
- Keep the current stale-state and invalid-config regressions green while refactoring.

## Recommended Order

1. Centralize request snapshot consumption.
2. Simplify plugin tool loading.
3. Reduce frontend async store duplication.
4. Move API background runtime lifecycles out of `main.py`.
5. Continue shrinking `bot.py` with one clearly justified extraction at a time.
