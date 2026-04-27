# Clean Architecture Boundaries Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task.
> Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Remove wrong-direction imports around API, config, Matrix, and worker boundaries, then encode those boundaries in `tach.toml`.

**Architecture:** Keep large composition roots where systems legitimately meet, especially `bot.py`, `orchestrator.py`, `agents.py`, and `response_runner.py`.
Move derived runtime behavior out of low-level packages so config stays authored-data focused and Matrix helpers stay Matrix focused.
Every phase removes one concrete dependency leak and adds a Tach rule that prevents it from returning.

**Tech Stack:** Python, FastAPI, Pydantic, Matrix/Nio, Tach, pytest, uv.

---

## File Structure

- Modify `src/mindroom/api/main.py` to keep FastAPI app assembly and route registration only.
- Modify `src/mindroom/api/config_lifecycle.py` to own API runtime config reload helpers.
- Modify `src/mindroom/api/google_integration.py` to call config lifecycle helpers instead of importing from `api.main`.
- Modify `tach.toml` after each boundary move so the fixed dependency cannot return.
- Modify or add focused tests under `tests/api/` for API reload behavior.
- Later phases will modify `src/mindroom/config/main.py`, `src/mindroom/runtime_resolution.py`, `src/mindroom/matrix/rooms.py`, `src/mindroom/matrix/mentions.py`, and worker API modules.

---

## Accountability Gates

Each phase is complete only when:

- [x] Task 1: the targeted wrong-direction import is removed from source.
- [x] Task 1: `tach.toml` forbids the removed dependency direction.
- [x] Task 1: `uv run tach check --dependencies --interfaces` passes.
- [x] Task 1: targeted tests for the changed area pass.
- [x] Task 1: a focused `rg` check confirms the removed import did not move somewhere else.
- [x] Task 1: the diff contains no unrelated cleanup.

---

### Task 1: Remove API Route To App Entrypoint Cycle

**Files:**
- Modify: `src/mindroom/api/main.py`
- Modify: `src/mindroom/api/config_lifecycle.py`
- Modify: `src/mindroom/api/google_integration.py`
- Modify: `tach.toml`
- Test: existing API config/google tests, plus a focused test if no existing coverage catches the contract

- [x] **Step 1: Write or identify the failing boundary test**

  Add a focused import-boundary test if no existing test checks that route modules do not import `mindroom.api.main`.

- [x] **Step 2: Run the focused test and confirm it fails**

  Run the selected test command.
  Expected: failure while `google_integration` imports reload behavior from `api.main`, or Tach still permits the forbidden edge.

- [x] **Step 3: Move reload helper into config lifecycle**

  Move `_reload_api_runtime_config` from `api.main` into `api.config_lifecycle`.
  Keep behavior unchanged.
  Preserve existing route behavior by updating imports and call sites.

- [x] **Step 4: Remove `api.main` from `google_integration` dependencies**

  Update `google_integration` to import and call the helper from `api.config_lifecycle`.
  Remove the local lazy imports from `api.main`.

- [x] **Step 5: Tighten Tach**

  Remove `mindroom.api.main` from the `mindroom.api.google_integration` dependency list.
  Update the architecture debt comment so the previous exception is gone.

- [x] **Step 6: Verify Task 1**

  Run:

  ```bash
  rg -n "from mindroom\\.api\\.main|import mindroom\\.api\\.main|mindroom\\.api\\.main" src/mindroom/api/google_integration.py tach.toml
  uv run tach check --dependencies --interfaces
  uv run pytest tests/api/test_api.py tests/test_google_tool_wrappers.py -n 0 --no-cov
  ```

  Expected: no source import from `api.main`, Tach passes, targeted tests pass.

---

### Task 2: Move Runtime-Derived Config Resolution Out Of Config Models

**Files:**
- Modify: `src/mindroom/config/main.py`
- Modify: `src/mindroom/runtime_resolution.py` or create `src/mindroom/entity_resolution.py`
- Modify: callers of moved methods
- Modify: `tach.toml`
- Test: config/runtime resolution tests

- [ ] **Step 1: Write resolver tests for current behavior**
- [ ] **Step 2: Move Matrix-dependent config methods into the resolver**
- [ ] **Step 3: Update callers to use resolver functions**
- [ ] **Step 4: Tighten Tach so `mindroom.config.*` cannot import `mindroom.matrix.*`**
- [ ] **Step 5: Verify with targeted tests and Tach**

**Progress:**

- [x] Slice A: moved configured room bot username resolution from `Config` to `mindroom.entity_resolution`.
- [x] Slice A: updated orchestrator and room cleanup callers.
- [x] Slice A: added focused resolver coverage and reran room cleanup tests.

---

### Task 3: Split Matrix Room Primitives From Managed Room Provisioning

**Files:**
- Modify: `src/mindroom/matrix/rooms.py`
- Create or modify: `src/mindroom/managed_rooms.py` or `src/mindroom/orchestration/managed_rooms.py`
- Modify: callers that currently use managed provisioning from `matrix.rooms`
- Modify: `tach.toml`
- Test: Matrix room provisioning tests

- [ ] **Step 1: Write tests around managed room provisioning behavior**
- [ ] **Step 2: Move MindRoom-specific provisioning out of `matrix.rooms`**
- [ ] **Step 3: Keep raw Matrix helpers in `matrix.rooms`**
- [ ] **Step 4: Tighten Tach so `matrix.rooms` cannot import agents or topic generation**
- [ ] **Step 5: Verify with targeted tests and Tach**

---

### Task 4: Replace Config Dependency In Matrix Mentions

**Files:**
- Modify: `src/mindroom/matrix/mentions.py`
- Add or modify a small mention directory type near runtime or Matrix delivery
- Modify: delivery callers that format mentions
- Modify: `tach.toml`
- Test: mention formatting tests

- [ ] **Step 1: Write tests for mention resolution through a small directory object**
- [ ] **Step 2: Add the typed mention directory**
- [ ] **Step 3: Update Matrix mention formatting to consume the directory instead of `Config`**
- [ ] **Step 4: Tighten Tach so `matrix.mentions` cannot import `config.main`**
- [ ] **Step 5: Verify with targeted tests and Tach**

---

### Task 5: Hide Local Worker Internals Behind A Worker Facade

**Files:**
- Modify: `src/mindroom/api/sandbox_worker_prep.py`
- Modify: `src/mindroom/api/sandbox_runner.py`
- Add or modify a public worker facade
- Modify: `tach.toml`
- Test: sandbox runner API tests

- [ ] **Step 1: Write tests for the API-facing worker behavior**
- [ ] **Step 2: Add a public worker facade for the required operations**
- [ ] **Step 3: Update API sandbox modules to use the facade**
- [ ] **Step 4: Tighten Tach so API sandbox modules cannot import `workers.backends.local`**
- [ ] **Step 5: Verify with targeted tests and Tach**
