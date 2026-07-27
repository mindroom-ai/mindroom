# Pre-Cache-Campaign Experiment Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:executing-plans to implement this plan task-by-task.

**Goal:** Produce a runnable experimental MindRoom branch and multi-architecture image with Matrix cache behavior restored to the parent of PR #1586.

**Architecture:** Restore the cache implementation and its owning thread-history facade from the fixed baseline while retaining unrelated current-main code.
Resolve resulting integration failures only at the boundary between current runtime code and the restored cache.
Publish one isolated image tag without invoking the release workflow or changing `latest`.

**Tech Stack:** Python 3.13, uv, pytest, Tach, pre-commit, Docker Buildx, GitHub Container Registry.

## Global Constraints

Never create a branch whose name begins with `codex/`.

Do not commit anything under `docs/superpowers/`.

Do not open a pull request.

Do not modify `latest`, release tags, or minimal-image tags.

Use fresh isolated cache storage for runtime experiments.

Preserve unrelated current-main behavior.

Use the fixed rollback baseline `34fae3688cbd182bc6221e48767ed2ba2c3fafc3^`.

---

### Task 1: Verify the Current-Main Starting Point

**Files:**

- Inspect: `src/mindroom/matrix/cache/`
- Inspect: `src/mindroom/matrix/client_thread_history.py`
- Inspect: `src/mindroom/matrix/conversation_cache.py`
- Inspect: `src/mindroom/matrix/sync_cache_trust.py`
- Inspect: `src/mindroom/matrix/thread_resolution_reuse.py`

**Interfaces:**

- Consumes: current branch `experiment/pre-cache-campaign`
- Produces: clean verified starting point at main commit `17bf577081703b9db39b7432707dbfddaa85bc44`

- [ ] **Step 1: Verify branch, worktree isolation, and clean state**

Run:

```bash
git branch --show-current
git status --short
git merge-base --is-ancestor 17bf577081703b9db39b7432707dbfddaa85bc44 HEAD
```

Expected: branch is `experiment/pre-cache-campaign`, status is clean, and ancestry command exits zero.

- [ ] **Step 2: Verify dependency setup and import provenance**

Run:

```bash
uv sync --all-extras
PYTHONPATH=src uv run python -c 'import mindroom; print(mindroom.__file__)'
```

Expected: import path points into this worktree.

- [ ] **Step 3: Run the current cache-focused baseline**

Run:

```bash
PYTHONPATH=src uv run pytest \
  tests/test_event_cache.py \
  tests/test_event_cache_backends.py \
  tests/test_event_cache_contract.py \
  tests/test_event_cache_semantics.py \
  tests/test_event_cache_write_coordination.py \
  tests/test_thread_cache_mutations.py \
  tests/test_thread_history.py \
  -q -x -n auto --no-cov
```

Expected: exit zero before rollback.

---

### Task 2: Restore the Cache Island

**Files:**

- Restore: `src/mindroom/matrix/cache/`
- Restore: `src/mindroom/matrix/client_thread_history.py`
- Restore: `src/mindroom/matrix/conversation_cache.py`
- Delete: `src/mindroom/matrix/sync_cache_trust.py`
- Delete: `src/mindroom/matrix/thread_resolution_reuse.py`
- Restore or delete by baseline presence: `tests/event_cache_test_support.py`
- Restore or delete by baseline presence: `tests/test_event_cache.py`
- Restore or delete by baseline presence: `tests/test_event_cache_backends.py`
- Restore or delete by baseline presence: `tests/test_event_cache_contract.py`
- Restore or delete by baseline presence: `tests/test_event_cache_semantics.py`
- Restore or delete by baseline presence: `tests/test_event_cache_storage_maintenance.py`
- Restore or delete by baseline presence: `tests/test_event_cache_write_coordination.py`
- Restore or delete by baseline presence: `tests/test_matrix_cache_interaction_contract.py`
- Restore or delete by baseline presence: `tests/test_matrix_event_cache_fuzz.py`
- Restore or delete by baseline presence: `tests/test_matrix_event_cache_live_audit.py`
- Restore or delete by baseline presence: `tests/test_matrix_event_cache_security.py`
- Restore or delete by baseline presence: `tests/test_sync_cache_trust.py`
- Restore or delete by baseline presence: `tests/test_thread_cache_mutations.py`
- Restore or delete by baseline presence: `tests/test_thread_history.py`
- Restore or delete by baseline presence: `tests/test_thread_mutation_atomicity.py`
- Restore or delete by baseline presence: `tests/test_thread_read_guards.py`
- Restore or delete by baseline presence: `tests/test_thread_repair.py`
- Restore or delete by baseline presence: `tests/test_thread_repair_bounding.py`
- Restore or delete by baseline presence: `tests/test_thread_resolution_reuse.py`
- Delete: `scripts/testing/benchmark_thread_history_reuse.py`
- Delete: `scripts/testing/fuzz_matrix_event_cache.py`

**Interfaces:**

- Consumes: baseline tree at `34fae3688cbd182bc6221e48767ed2ba2c3fafc3^`
- Produces: baseline `ConversationEventCache`, `EventCacheWriteCoordinator`, `MatrixConversationCache`, and thread-history implementation

- [ ] **Step 1: Restore every baseline-owned file**

Use the baseline tree to restore all files listed above that existed before PR #1586.

- [ ] **Step 2: Remove campaign-created files**

Remove only listed files that have no object at the baseline revision.

- [ ] **Step 3: Prove exact cache-island equality**

Run:

```bash
git diff --exit-code 34fae3688^ -- \
  src/mindroom/matrix/cache \
  src/mindroom/matrix/client_thread_history.py \
  src/mindroom/matrix/conversation_cache.py
```

Expected: exit zero.

- [ ] **Step 4: Commit the mechanical rollback**

Stage only the files listed in this task and commit:

```bash
git commit -m "Restore pre-campaign Matrix cache internals"
```

---

### Task 3: Reconnect Current Runtime Code to the Restored Cache

**Files:**

- Modify: `src/mindroom/runtime_support.py`
- Modify: `src/mindroom/bot.py`
- Modify: `src/mindroom/orchestrator.py`
- Modify: `src/mindroom/bot_runtime_view.py`
- Modify: `src/mindroom/approval_transport.py`
- Modify: `src/mindroom/matrix/message_content.py`
- Modify: `src/mindroom/matrix/stale_stream_cleanup.py`
- Modify: `src/mindroom/streaming.py`
- Modify: `src/mindroom/thread_export/service.py`
- Test: `tests/test_import_graph.py`
- Test: `tests/test_bot_sync_event_cache.py`
- Test: `tests/test_matrix_client_session.py`
- Test: `tests/test_orchestrator_runtime.py`
- Test: `tests/test_streaming.py`

**Interfaces:**

- Consumes: baseline `ConversationEventCache` without principal views, membership epochs, revisions, retained repair deltas, or atomic append outcomes
- Produces: current runtime wired to one baseline shared cache service with no compatibility implementation inside `matrix/cache/`

- [ ] **Step 1: Record deterministic import failures**

Run:

```bash
PYTHONPATH=src uv run pytest tests/test_import_graph.py -q -x --no-cov
PYTHONPATH=src uv run python -c 'import mindroom.bot; import mindroom.orchestrator'
```

Expected: failures identify current callers of removed cache interfaces.

- [ ] **Step 2: Restore shared-cache runtime typing**

Change `OwnedRuntimeSupport.event_cache` and `_build_event_cache` back from `SharedConversationEventCache` to `ConversationEventCache`.

Change startup prewarm claims back to room-only keys because baseline cache storage is not principal-bound.

- [ ] **Step 3: Remove principal-view binding**

Bind bots, router approvals, and thread exports directly to the shared runtime cache.

Remove current `for_principal(...)` and `principal_id` calls.

- [ ] **Step 4: Restore baseline sync-cache trust ownership**

Move classic sync-checkpoint certification back into `AgentBot` using the existing baseline functions in `matrix.sync_certification` and `matrix.sync_tokens`.

Keep current sliding-sync transport behavior, but do not let sliding sync claim that the baseline cache is certified.

Remove calls to cache-generation, principal purge, room membership epochs, and joined-room cache reopening.

- [ ] **Step 5: Restore old thread and sidecar call contracts**

Remove membership-epoch arguments from point and batch writes.

Use baseline single-item MXC text reads and writes.

Use baseline thread replacement boolean outcomes.

Use baseline `append_event` followed by `revalidate_thread_after_incremental_update`.

- [ ] **Step 6: Run focused integration tests**

Run:

```bash
PYTHONPATH=src uv run pytest \
  tests/test_import_graph.py \
  tests/test_bot_sync_event_cache.py \
  tests/test_matrix_client_session.py \
  tests/test_orchestrator_runtime.py \
  tests/test_streaming.py \
  -q -x -n auto --no-cov
```

Expected: exit zero.

- [ ] **Step 7: Commit integration repairs**

Stage each modified file explicitly and commit:

```bash
git commit -m "Reconnect current runtime to legacy cache"
```

---

### Task 4: Validate the Rollback

**Files:**

- Validate: all changed production and test files
- Validate: `tach.toml`

**Interfaces:**

- Consumes: runnable current runtime plus restored cache
- Produces: evidence that imports, cache behavior, dependency boundaries, and complete tests pass

- [ ] **Step 1: Run restored cache tests**

Run:

```bash
PYTHONPATH=src uv run pytest \
  tests/test_event_cache.py \
  tests/test_event_cache_backends.py \
  tests/test_event_cache_contract.py \
  tests/test_event_cache_semantics.py \
  tests/test_event_cache_write_coordination.py \
  tests/test_thread_cache_mutations.py \
  tests/test_thread_history.py \
  -q -x -n auto --no-cov
```

Expected: exit zero.

- [ ] **Step 2: Run Tach**

Run:

```bash
uv run tach check --dependencies --interfaces
```

Expected: exit zero.

- [ ] **Step 3: Run complete pytest**

Run:

```bash
PYTHONPATH=src uv run pytest -q -n auto --no-cov
```

Expected: exit zero.

- [ ] **Step 4: Run pre-commit**

Run:

```bash
uv run pre-commit run --all-files
```

Expected: exit zero without unrelated rewrites.

- [ ] **Step 5: Verify diff integrity**

Run:

```bash
git diff --check
git status --short
git diff --stat 17bf577081703b9db39b7432707dbfddaa85bc44..HEAD
```

Expected: no unstaged changes and a diff limited to experiment docs, cache rollback, tests, and required integration repairs.

---

### Task 5: Build, Publish, and Smoke-Test the Image

**Files:**

- Build: `local/instances/deploy/Dockerfile.mindroom`

**Interfaces:**

- Consumes: validated branch head
- Produces: `ghcr.io/mindroom-ai/mindroom:experiment-pre-cache-campaign`

- [ ] **Step 1: Build the production image locally**

Run:

```bash
docker build \
  --file local/instances/deploy/Dockerfile.mindroom \
  --tag mindroom:pre-cache-campaign-local \
  .
```

Expected: exit zero.

- [ ] **Step 2: Smoke-test the local image**

Run:

```bash
docker run --rm mindroom:pre-cache-campaign-local mindroom version
```

Expected: exit zero and a MindRoom version string.

- [ ] **Step 3: Push the experiment branch**

Run:

```bash
git push --set-upstream origin experiment/pre-cache-campaign
```

Expected: remote branch created without opening a pull request.

- [ ] **Step 4: Authenticate Docker to GHCR**

Run:

```bash
gh auth token | docker login ghcr.io --username basnijholt --password-stdin
```

Expected: `Login Succeeded`.

- [ ] **Step 5: Publish the multi-architecture image**

Run:

```bash
docker buildx build \
  --platform linux/amd64,linux/arm64 \
  --file local/instances/deploy/Dockerfile.mindroom \
  --tag ghcr.io/mindroom-ai/mindroom:experiment-pre-cache-campaign \
  --provenance=false \
  --sbom=false \
  --push \
  .
```

Expected: exit zero and one multi-architecture manifest under the unique experiment tag.

- [ ] **Step 6: Verify the remote manifest**

Run:

```bash
docker buildx imagetools inspect ghcr.io/mindroom-ai/mindroom:experiment-pre-cache-campaign
```

Expected: manifest contains Linux AMD64 and Linux ARM64 entries.

- [ ] **Step 7: Smoke-test the published image**

Run:

```bash
docker run --rm ghcr.io/mindroom-ai/mindroom:experiment-pre-cache-campaign mindroom version
```

Expected: exit zero and a MindRoom version string.
