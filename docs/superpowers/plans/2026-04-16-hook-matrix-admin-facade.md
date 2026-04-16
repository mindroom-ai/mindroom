# Hook Matrix Admin Facade Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Add a minimal hook-facing `matrix_admin` facade that lets plugins create rooms, invite users, inspect members, resolve aliases, and link rooms to a Space by reusing existing Matrix helpers.

**Architecture:** Add one small hook helper module that wraps existing Matrix client helpers behind a narrow protocol. Thread that facade through `HookContextSupport` and the orchestrator so hooks get router-backed admin capability without exposing raw `nio.AsyncClient`.

**Tech Stack:** Python 3.12, `matrix-nio`, MindRoom hook system, pytest

---

### Task 1: Plan Baseline Verification

**Files:**
- Test: `tests/test_hook_room_state.py`
- Test: `tests/test_bot_ready_hook.py`
- Test: `tests/test_hook_sender.py`

- [ ] **Step 1: Run the current hook/Matrix baseline tests**

Run: `uv run pytest tests/test_hook_room_state.py tests/test_bot_ready_hook.py tests/test_hook_sender.py -x -n 0 --no-cov -v`
Expected: PASS on the clean worktree before edits


### Task 2: Add Matrix Admin Hook Tests

**Files:**
- Create: `tests/test_hook_matrix_admin.py`
- Modify: `tests/test_bot_ready_hook.py`

- [ ] **Step 1: Write failing unit tests for the new hook matrix admin builder**

Cover:
- `resolve_alias()` returns the resolved room ID on `RoomResolveAliasResponse`
- `resolve_alias()` returns `None` on Matrix error responses
- `create_room()` delegates to `mindroom.matrix.client.create_room`
- `invite_user()` delegates to `mindroom.matrix.client.invite_to_room`
- `get_room_members()` delegates to `mindroom.matrix.client.get_room_members`
- `add_room_to_space()` delegates to `mindroom.matrix.client.add_room_to_space`

- [ ] **Step 2: Write failing context-plumbing tests**

Cover:
- `HookContext` stores and exposes `matrix_admin`
- router hook support uses the current router client for `matrix_admin`
- non-router hook support falls back to the orchestrator router client for `matrix_admin`
- non-router hook support returns `None` when no router client is available

- [ ] **Step 3: Run the new tests to verify they fail for the expected reason**

Run: `uv run pytest tests/test_hook_matrix_admin.py tests/test_bot_ready_hook.py -x -n 0 --no-cov -v`
Expected: FAIL with missing `matrix_admin` plumbing and missing hook matrix admin builder/export


### Task 3: Implement the Minimal Facade

**Files:**
- Create: `src/mindroom/hooks/matrix_admin.py`
- Modify: `src/mindroom/hooks/types.py`
- Modify: `src/mindroom/hooks/context.py`
- Modify: `src/mindroom/hooks/__init__.py`
- Modify: `src/mindroom/orchestrator.py`

- [ ] **Step 1: Add the hook-facing protocol types**

Add a `HookMatrixAdmin` protocol to `src/mindroom/hooks/types.py` with only:
- `resolve_alias`
- `create_room`
- `invite_user`
- `get_room_members`
- `add_room_to_space`

- [ ] **Step 2: Add the concrete builder in `src/mindroom/hooks/matrix_admin.py`**

Implement `build_hook_matrix_admin(client, runtime_paths)` as a thin wrapper that reuses:
- `invite_to_room`
- `create_room`
- `get_room_members`
- `add_room_to_space`

Use `client.room_resolve_alias()` directly for alias resolution.
Compute the Space-link server name with existing Matrix identity helpers instead of duplicating parsing logic.

- [ ] **Step 3: Thread the facade into hook contexts**

Update `HookContextSupport` and `HookContext` so:
- `HookContextSupport.matrix_admin()` returns router-backed admin capability
- router hooks use the live router client
- non-router hooks fall back to the orchestrator router client
- `base_kwargs()` includes `matrix_admin`

- [ ] **Step 4: Export the new public hook API**

Re-export `HookMatrixAdmin` and `build_hook_matrix_admin` from `src/mindroom/hooks/__init__.py`.

- [ ] **Step 5: Add orchestrator fallback**

Add a small `_hook_matrix_admin()` helper on the orchestrator next to the existing hook sender and room-state fallback helpers.


### Task 4: Verify and Refine

**Files:**
- Test: `tests/test_hook_matrix_admin.py`
- Test: `tests/test_bot_ready_hook.py`
- Test: `tests/test_hook_room_state.py`
- Test: `tests/test_hook_sender.py`

- [ ] **Step 1: Run the focused hook/Matrix test set**

Run: `uv run pytest tests/test_hook_matrix_admin.py tests/test_bot_ready_hook.py tests/test_hook_room_state.py tests/test_hook_sender.py -x -n 0 --no-cov -v`
Expected: PASS

- [ ] **Step 2: Run formatting/linting for touched files if needed**

Run: `uv run ruff check src/mindroom/hooks/matrix_admin.py src/mindroom/hooks/context.py src/mindroom/hooks/types.py src/mindroom/hooks/__init__.py src/mindroom/orchestrator.py tests/test_hook_matrix_admin.py tests/test_bot_ready_hook.py`
Expected: PASS

- [ ] **Step 3: Review the diff for accidental API expansion**

Run: `git --no-pager diff -- src/mindroom/hooks src/mindroom/orchestrator.py tests/test_hook_matrix_admin.py tests/test_bot_ready_hook.py`
Expected: only the minimal facade and tests, with no raw-client exposure

- [ ] **Step 4: Commit**

```bash
git add docs/superpowers/plans/2026-04-16-hook-matrix-admin-facade.md \
  src/mindroom/hooks/matrix_admin.py \
  src/mindroom/hooks/types.py \
  src/mindroom/hooks/context.py \
  src/mindroom/hooks/__init__.py \
  src/mindroom/orchestrator.py \
  tests/test_hook_matrix_admin.py \
  tests/test_bot_ready_hook.py
git commit -m "feat: add hook matrix admin facade"
```
