# Worker Tool Validation Snapshot Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Split tool validation from tool executability so the primary runtime owns config validity and workers only own local execution.

**Architecture:** Introduce a validation-only tool snapshot derived from the primary runtime's resolved tool surface, make config validation depend on that snapshot instead of executable runtime metadata, and pass the snapshot to workers at startup instead of ad hoc allowed-name lists. This removes the worker-side placeholder metadata path and restores a single source of truth for authored tool validation.

**Tech Stack:** Python, Pydantic, FastAPI sandbox runner, Kubernetes worker backend, pytest

---

### Task 1: Capture the new worker contract in tests

**Files:**
- Modify: `tests/api/test_sandbox_runner_api.py`
- Modify: `tests/test_kubernetes_worker_backend.py`
- Modify: `tests/test_tools_metadata.py`

- [ ] **Step 1: Write the failing tests**

Add tests that assert:
- worker startup accepts tools validated upstream without fabricating `ToolMetadata`
- unknown tools are still rejected from the worker runtime
- Kubernetes worker env contains a validation snapshot payload instead of `MINDROOM_SANDBOX_ALLOWED_TOOL_NAMES_JSON`
- the validation snapshot preserves authored override schema and MCP-specific validation behavior

- [ ] **Step 2: Run the focused tests to verify they fail**

Run:
```bash
PYTHONPATH=/home/basnijholt/Work/dev/mindroom/.worktrees/pr-594-review/src \
/home/basnijholt/Work/dev/mindroom/.venv/bin/pytest \
tests/api/test_sandbox_runner_api.py \
tests/test_kubernetes_worker_backend.py \
tests/test_tools_metadata.py \
-k 'validation_snapshot or worker_runtime or mcp' -x -n 0 --no-cov -v
```

Expected:
- failures referring to missing validation snapshot helpers or old allowed-tool-name env wiring

### Task 2: Add a validation-only tool snapshot

**Files:**
- Modify: `src/mindroom/tool_system/metadata.py`
- Modify: `src/mindroom/mcp/registry.py` if needed for explicit MCP validation markers
- Test: `tests/test_tools_metadata.py`

- [ ] **Step 1: Add the failing metadata test if Task 1 did not already cover it**

Add a test that builds a runtime validation snapshot and asserts it contains:
- known tool names
- config-field schema used by authored overrides
- explicit MCP override-validation semantics without depending on runtime factories

- [ ] **Step 2: Implement the minimal metadata changes**

Add:
- a validation-only dataclass for one tool's authored-config validation surface
- a runtime snapshot builder that returns validation data separate from executable registry state
- serialization helpers for worker startup payloads

Keep `ToolMetadata` for runtime/UI concerns only.

- [ ] **Step 3: Run metadata-focused tests**

Run:
```bash
PYTHONPATH=/home/basnijholt/Work/dev/mindroom/.worktrees/pr-594-review/src \
/home/basnijholt/Work/dev/mindroom/.venv/bin/pytest \
tests/test_tools_metadata.py -x -n 0 --no-cov -v
```

Expected:
- PASS

### Task 3: Make config validation depend on the snapshot

**Files:**
- Modify: `src/mindroom/config/main.py`
- Modify: `src/mindroom/tool_system/metadata.py`
- Test: `tests/api/test_sandbox_runner_api.py`

- [ ] **Step 1: Write or extend the failing config-validation tests**

Assert that authored tool validation succeeds when the upstream snapshot says a missing worker plugin tool is valid, and fails when the tool is not present in that snapshot.

- [ ] **Step 2: Implement the minimal config-validation changes**

Change config validation to consume the validation snapshot instead of executable registry plus `ToolMetadata`.

That includes:
- replacing `_validate_authored_tool_entries_with_state(...)` with a snapshot-based entrypoint
- removing MCP validation's dependence on runtime factory markers during config validation

- [ ] **Step 3: Run the sandbox-runner validation tests**

Run:
```bash
PYTHONPATH=/home/basnijholt/Work/dev/mindroom/.worktrees/pr-594-review/src \
/home/basnijholt/Work/dev/mindroom/.venv/bin/pytest \
tests/api/test_sandbox_runner_api.py \
-k 'skips_unavailable_plugins_for_worker_runtime or rejects_invalid_tools' \
-x -n 0 --no-cov -v
```

Expected:
- PASS

### Task 4: Replace the worker startup side channel

**Files:**
- Modify: `src/mindroom/api/sandbox_runner.py`
- Modify: `src/mindroom/workers/backends/kubernetes_resources.py`
- Test: `tests/test_kubernetes_worker_backend.py`
- Test: `tests/api/test_sandbox_runner_api.py`

- [ ] **Step 1: Write or extend the failing worker payload tests**

Assert that:
- Kubernetes sends the serialized validation snapshot into worker env
- sandbox runner reads that snapshot from env
- worker startup no longer uses placeholder tool factories or placeholder metadata

- [ ] **Step 2: Implement the minimal runtime changes**

Replace:
- `MINDROOM_SANDBOX_ALLOWED_TOOL_NAMES_JSON`
- `_validation_placeholder_tool_factory()`
- `_placeholder_tool_metadata()`
- `_tool_state_with_upstream_allowed_names()`

With:
- one serialized validation snapshot env payload
- one env loader in the sandbox runner
- snapshot-based config validation during worker startup

- [ ] **Step 3: Run focused worker tests**

Run:
```bash
PYTHONPATH=/home/basnijholt/Work/dev/mindroom/.worktrees/pr-594-review/src \
/home/basnijholt/Work/dev/mindroom/.venv/bin/pytest \
tests/api/test_sandbox_runner_api.py \
tests/test_kubernetes_worker_backend.py \
-x -n 0 --no-cov -v
```

Expected:
- PASS

### Task 5: Final cleanup, verification, and push

**Files:**
- Modify only the files touched above

- [ ] **Step 1: Remove obsolete helpers and dead imports**

Delete the old allowed-name plumbing and any no-longer-needed compatibility helpers.

- [ ] **Step 2: Run full focused verification**

Run:
```bash
PYTHONPATH=/home/basnijholt/Work/dev/mindroom/.worktrees/pr-594-review/src \
/home/basnijholt/Work/dev/mindroom/.venv/bin/pytest \
tests/api/test_sandbox_runner_api.py \
tests/test_kubernetes_worker_backend.py \
tests/test_tools_metadata.py \
-x -n 0 --no-cov -v
```

Expected:
- PASS

- [ ] **Step 3: Commit and push**

Run:
```bash
git add docs/superpowers/plans/2026-04-15-worker-tool-validation-snapshot.md
git add tests/api/test_sandbox_runner_api.py tests/test_kubernetes_worker_backend.py tests/test_tools_metadata.py
git add src/mindroom/tool_system/metadata.py src/mindroom/config/main.py src/mindroom/api/sandbox_runner.py src/mindroom/workers/backends/kubernetes_resources.py
git commit -m "refactor: split worker tool validation from execution"
git push
```
