# Post-Merge Refactor Priorities

This document lists the next refactors that should happen soon after the current hook, plugin, and config-hardening branch lands.

It is intentionally short and prioritized.

The goal is to preserve the improvements from this branch and reduce the chance that the same bad patterns reappear.

## Why These Items

This branch fixed recurring regressions in four areas.

- Plugin tool registration and reload behavior.
- User-facing config-load error handling.
- Frontend stale-state handling during async config refresh.
- Hook ingress and runtime-context plumbing.
- API runtime rebinding for long-lived background tasks.

The fixes are now materially better than `origin/main`.

The remaining work should focus on reducing the number of legal ways to bypass those good patterns.

## Priority 1: Simplify Plugin Tool Loading

Files:

- `src/mindroom/tool_system/plugins.py`
- `src/mindroom/tool_system/metadata.py`

Current state:

- Plugin tool state is now transactional and explicit enough to be correct.
- The code still depends on import-time decorator registration to discover plugin tools.
- Validation now serializes registry mutation with runtime loads, but it still does so by mutating and restoring process-global registries under a lock.
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
- Validation reads do not need to snapshot and restore global registry state.
- Plugin add, remove, re-add, manifest rename, and export rename all work from the same model.
- Tool-name collisions fail at one clear boundary.
- Tests cover whole-plugin removal, intra-plugin duplicate names, cross-plugin collisions, manifest-only rename, and failed multi-plugin load rollback.

## Priority 2: Finish Config-Load Error Consolidation

Files:

- `src/mindroom/config/main.py`
- `src/mindroom/api/config_lifecycle.py`
- `src/mindroom/commands/config_commands.py`
- `src/mindroom/custom_tools/config_manager.py`
- `src/mindroom/custom_tools/self_config.py`
- `src/mindroom/cli/config.py`

Current state:

- The main user-facing API, chat, tool, and CLI paths now mostly normalize invalid config the same way.
- The remaining risk is broad local `except Exception` handling in config-facing tools and commands.
- There are still a few internal raw `load_config(...)` helpers, but the bad user-facing bypass pattern is mostly gone.

Why this is next:

- Future edits can still weaken the shared invalid-config UX if local call sites start formatting errors themselves again.
- The code now has the right shape, but not every caller is as strict as it should be.

Refactor target:

- Move user-facing config-load failure classification and formatting into one small shared module if it is still split across `config.main` and local callers.
- Narrow broad `except Exception` blocks in config-facing commands and tools to expected operational failures.
- Keep raw `load_config(...)` for internal startup and non-user-facing paths only.

Acceptance criteria:

- User-facing config readers and writers either use the shared helper or explicitly preserve its semantics.
- Malformed YAML, invalid UTF-8, missing files, schema errors, and runtime validation errors all stay in the same user-facing error channel.
- New user-facing config surfaces do not invent their own load-error formatting.

## Priority 3: Reduce Frontend Async Store Duplication

Files:

- `frontend/src/store/configStore.ts`
- `frontend/src/services/configService.ts`

Current state:

- The store now guards `loadConfig()`, `refreshAgentPolicies()`, and `saveConfig()` against stale overlapping responses.
- The sequencing pattern is still duplicated across multiple async actions.
- Invalid current configs now stay in a read-only error state with detailed validation issues instead of silently falling back to stale data.
- The invalid-config and validation-diagnostic retention logic still live directly inside `configStore.ts`.

Why this is next:

- This exact stale-state class has already recurred more than once.
- Duplicated async sequencing logic is easy to copy incorrectly.
- Recovery-mode behavior is now correct, but it is still hand-built inside the main store rather than being one explicit sub-state.

Refactor target:

- Extract one small internal request-version helper or store utility for async actions that replace committed state.
- Keep the distinction between validation-failure clears, generic-load-error preservation, and save-result sequencing.
- Keep diagnostics behavior explicit.
- Decide explicitly whether dashboard invalid-config recovery should remain read-only or gain a raw full-config editor.
- Make invalid-load state one explicit mode instead of implicit coupling between `config`, `diagnostics`, and `syncStatus`.

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
- Config cache commits are now serialized and runtime-mismatch loads are discarded, but the sequencing still spans multiple helpers in `config_lifecycle.py`.

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

1. Simplify plugin tool loading.
2. Finish config-load error consolidation.
3. Reduce frontend async store duplication.
4. Move API background runtime lifecycles out of `main.py`.
5. Continue shrinking `bot.py` with one clearly justified extraction at a time.
