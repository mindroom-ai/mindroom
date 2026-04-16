# Narrow Tach Rollout Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Add Tach as a narrow, enforced boundary tool for the Matrix cache / conversation / runtime-support slice without turning it into a noisy repo-wide adoption.

**Architecture:** Keep the architecture doc broad and living, but make the first enforcement slice deliberately small. Encode only the Matrix cache boundary cluster in Tach, use the real public seams that already exist, and leave the rest of the repo effectively unchecked for now.

**Expanded pilot scope:** Once the first cache boundary is green, widen the same PR only to direct production consumers that already import through the public cache or conversation seams.
That second ring currently includes `mindroom.bot_runtime_view`, `mindroom.matrix.client`, `mindroom.conversation_resolver`, `mindroom.commands.handler`, `mindroom.scheduling`, `mindroom.tool_system.runtime_context`, `mindroom.thread_summary`, `mindroom.streaming`, `mindroom.post_response_effects`, `mindroom.hooks.sender`, and `mindroom.turn_controller`.

**Explicitly out of scope for this pilot:** Do not try to enforce the `thread_membership` / `thread_bookkeeping` / `stale_stream_cleanup` cluster yet.
That area still carries cyclic structure around cache and client behavior and should stay advisory until a later cleanup pass makes the ownership calmer.

**Tech Stack:** Python, uv, Tach, GitHub Actions, existing MindRoom Matrix cache/conversation modules.

---

## File map

- Modify: `pyproject.toml`
- Create: `tach.toml`
- Modify: `.github/workflows/pytest.yml` or create a dedicated narrow Tach workflow if that ends up cleaner
- Modify: `src/mindroom/matrix/cache/__init__.py`
- Modify: `src/mindroom/runtime_support.py`
- Modify: selected tests under `tests/` that currently import private cache implementation types outside cache-focused unit tests
- Possibly modify: `docs/dev/2026-04-15-mindroom-architecture-boundaries.md` only if a small clarification becomes necessary during rollout

## Task 1: Establish the narrow Tach baseline

**Files:**
- Modify: `pyproject.toml`
- Create: `tach.toml`

- [ ] **Step 1: Reproduce the current no-Tach baseline**

Run:

```bash
cd /home/basnijholt/Work/dev/mindroom-tach-cache-boundary-pilot
uvx tach check --dependencies --interfaces
```

Expected:
- The command does not provide a useful project check yet because the repo has no Tach config.

- [ ] **Step 2: Add Tach as a dev dependency**

Modify `pyproject.toml` to add `tach>=0.34.1` to `[dependency-groups].dev`.

- [ ] **Step 3: Write the initial narrow `tach.toml`**

Create `tach.toml` with:
- `source_roots = ["src"]`
- repo-appropriate excludes
- only the small first module slice:
  - `mindroom.matrix.cache`
  - `mindroom.matrix.conversation_cache`
  - `mindroom.runtime_support`

Use dependency rules only for the first commit if that is the cleanest path to green.

Do not attempt to model the whole repo.

- [ ] **Step 4: Verify Tach reads the config**

Run:

```bash
cd /home/basnijholt/Work/dev/mindroom-tach-cache-boundary-pilot
uv sync --group dev
uv run tach check --dependencies
```

Expected:
- Tach reads the config.
- The output now reflects only the configured narrow slice.

- [ ] **Step 5: Commit the baseline**

```bash
git add pyproject.toml tach.toml
git commit -m "chore: add narrow tach baseline"
```

## Task 2: Encode the real Matrix boundary instead of an imagined one

**Files:**
- Modify: `tach.toml`
- Modify: `src/mindroom/matrix/cache/__init__.py`
- Modify: `src/mindroom/runtime_support.py`
- Test/reference: `src/mindroom/matrix/conversation_cache.py`, `src/mindroom/matrix/client.py`

- [ ] **Step 1: Map the current production imports in the enforced slice**

Run:

```bash
cd /home/basnijholt/Work/dev/mindroom-tach-cache-boundary-pilot
rg -n "mindroom\\.matrix\\.cache|ConversationEventCache|_EventCache|_EventCacheWriteCoordinator" src/mindroom -S
```

Expected:
- `runtime_support.py` is the main production constructor of `_EventCache` and `_EventCacheWriteCoordinator`.
- most higher-level consumers already type against `ConversationEventCache` or `MatrixConversationCache`.

- [ ] **Step 2: Decide the first allowed boundary explicitly**

Encode these rules in `tach.toml`:
- `mindroom.matrix.conversation_cache` may depend on `mindroom.matrix.cache`
- `mindroom.runtime_support` may depend on `mindroom.matrix.cache`
- direct production consumers may be added only when they already import through the package-level cache boundary or `mindroom.matrix.conversation_cache`
- nothing in the first slice should require broad repo-wide rules

Do not overfit the config to tests yet.

- [ ] **Step 3: Add a package-level public surface for the cache slice where needed**

If Tach interface checks require a calmer public import path, use `src/mindroom/matrix/cache/__init__.py` as the package-level surface for names that should be imported from above the cache package.

Keep this tiny.

Do not export internals just to satisfy the tool.

- [ ] **Step 4: Keep `_EventCache` as an intentional internal implementation**

Do not promote `_EventCache` to a public API just to make Tach easier.

If necessary, document in `tach.toml` and code changes that `runtime_support.py` is the composition-root exception for constructing the private concrete cache.

- [ ] **Step 5: Run the narrow check again**

```bash
cd /home/basnijholt/Work/dev/mindroom-tach-cache-boundary-pilot
uv run tach check --dependencies
```

Expected:
- dependency checks reflect the intended narrow architecture
- no violations remain in the enforced production slice

- [ ] **Step 6: Commit the dependency-boundary pass**

```bash
git add tach.toml src/mindroom/matrix/cache/__init__.py src/mindroom/runtime_support.py
git commit -m "refactor: encode narrow matrix cache boundaries"
```

## Task 3: Add interface enforcement only where it improves the boundary

**Files:**
- Modify: `tach.toml`
- Possibly modify: `src/mindroom/matrix/cache/__init__.py`
- Possibly modify: imports in `src/mindroom/matrix/conversation_cache.py` and `src/mindroom/matrix/client.py`

- [ ] **Step 1: Add the smallest useful interface definition**

Use Tach interfaces to protect only the public cache surface that should be reachable from above the cache package.

Do not define interfaces for every internal module.

Prefer one package-level interface over a pile of deep per-module interfaces.

- [ ] **Step 2: Run interface checks and observe the first real failures**

```bash
cd /home/basnijholt/Work/dev/mindroom-tach-cache-boundary-pilot
uv run tach check --interfaces
```

Expected:
- either the first interface pass is already green
- or it identifies a small number of import-path leaks in the enforced slice

- [ ] **Step 3: Fix only genuine boundary leaks**

If interface errors appear:
- move callers to the package-level cache boundary where appropriate
- keep `runtime_support.py` as the allowed concrete constructor if that still matches the architecture
- do not widen the interface just to silence the tool unless the widened boundary is genuinely intended

- [ ] **Step 4: Run the combined narrow check**

```bash
cd /home/basnijholt/Work/dev/mindroom-tach-cache-boundary-pilot
uv run tach check --dependencies --interfaces
```

Expected:
- the configured narrow slice is fully green

- [ ] **Step 5: Commit the interface pass**

```bash
git add tach.toml src/mindroom/matrix/cache/__init__.py src/mindroom/matrix/conversation_cache.py src/mindroom/matrix/client.py
git commit -m "refactor: enforce cache public interface with tach"
```

## Task 4: Wire the narrow Tach check into CI without going repo-wide

**Files:**
- Modify: `.github/workflows/pytest.yml` or create a dedicated workflow like `.github/workflows/tach.yml`

- [ ] **Step 1: Decide whether to extend `pytest.yml` or add a dedicated workflow**

Prefer the smaller, clearer option.

If a dedicated workflow keeps the signal clearer, use that.

If one extra step in `pytest.yml` is cleaner, do that instead.

- [ ] **Step 2: Add the narrow Tach check command**

Use the repo environment plus the dev dependency group, then run:

```bash
uv run tach check --dependencies --interfaces
```

Do not make CI depend on repo-wide Tach adoption.

- [ ] **Step 3: Run the equivalent command locally**

```bash
cd /home/basnijholt/Work/dev/mindroom-tach-cache-boundary-pilot
uv sync --group dev
uv run tach check --dependencies --interfaces
```

Expected:
- local CI-equivalent command is green

- [ ] **Step 4: Commit the CI wiring**

```bash
git add .github/workflows/pytest.yml .github/workflows/tach.yml
git commit -m "ci: enforce narrow tach boundary checks"
```

Only add whichever workflow file you actually changed.

## Task 5: Verify, review, and open the PR

**Files:**
- Review all changed files

- [ ] **Step 1: Run the narrow Tach check**

```bash
cd /home/basnijholt/Work/dev/mindroom-tach-cache-boundary-pilot
uv run tach check --dependencies --interfaces
```

- [ ] **Step 2: Run targeted regression checks for the touched Matrix slice**

```bash
cd /home/basnijholt/Work/dev/mindroom-tach-cache-boundary-pilot
uv run pytest tests/test_event_cache.py tests/test_thread_history.py tests/test_threading_error.py -x -n 0 --no-cov -v
```

- [ ] **Step 3: Run pre-commit on touched files**

```bash
cd /home/basnijholt/Work/dev/mindroom-tach-cache-boundary-pilot
uv run pre-commit run --files pyproject.toml tach.toml .github/workflows/pytest.yml src/mindroom/matrix/cache/__init__.py src/mindroom/runtime_support.py src/mindroom/matrix/conversation_cache.py src/mindroom/matrix/client.py
```

Adjust the file list to match the actual diff.

- [ ] **Step 4: Review against the architecture spec**

Confirm that the PR:
- enforces only the intended narrow slice
- does not accidentally widen `_EventCache` into a broader public API
- does not turn Tach into a giant advisory report generator

- [ ] **Step 5: Commit any final fixups**

```bash
git add <exact files>
git commit -m "fix: tighten narrow tach rollout"
```

Only if needed.

- [ ] **Step 6: Push and open the PR**

```bash
git push -u origin tach-cache-boundary-pilot
gh pr create --base main --head tach-cache-boundary-pilot --title "chore: add narrow tach boundary enforcement" --body "<summary>"
```
