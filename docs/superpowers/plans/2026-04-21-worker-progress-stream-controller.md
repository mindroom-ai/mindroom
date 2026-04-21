# Worker Progress Stream Controller Implementation Plan

> **For this plan:** Execute inline in this worktree on the current `issue-183` branch. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Fix worker warmup streaming so delivery failures follow the normal streaming error contract, hidden-tool mode does not leak tool identifiers, and Kubernetes progress reflects the real startup lifecycle.

**Architecture:** Keep the core invariant that one supervised path owns visible stream delivery.
Implement the smallest correct change first inside `streaming.py`.
Only split out `src/mindroom/streaming_controller.py` if the single-writer cleanup is still substantial after the minimal fix is in place.
Treat sync-tool offload as a hypothesis to prove with a behavioral test before changing wrappers.

**Tech Stack:** Python, asyncio, nio, existing MindRoom Matrix delivery helpers, sandbox proxy, Kubernetes worker backend, pytest, unittest.mock.

**Execution Mode:** Inline execution in this worktree on the current `issue-183` branch.
Build on the existing worker-progress implementation in this branch.
Do not restart from `origin/main` unless Task 1 proves the current `streaming.py` path cannot be repaired incrementally.

---

## File map

- Modify: `src/mindroom/streaming.py`
- Possibly create: `src/mindroom/streaming_controller.py` if the single-writer cleanup remains substantial after the minimal supervision fix
- Modify: `src/mindroom/tool_system/runtime_context.py` only if the worker-progress pump contract changes
- Modify: `src/mindroom/tool_system/sandbox_proxy.py` only if Task 4 proves there is still a blocking sync-tool path
- Modify: `src/mindroom/workers/backends/kubernetes.py`
- Modify: `src/mindroom/workers/backends/kubernetes_resources.py`
- Modify: `docs/streaming.md`
- Modify: `docs/configuration/agents.md`
- Modify: `docs/configuration/index.md`
- Modify: `docs/dev/agent_configuration.md`
- Review: `docs/openai-api.md`
- Modify: `tests/test_streaming_behavior.py`
- Modify: `tests/test_streaming_e2e.py`
- Modify: `tests/test_kubernetes_worker_backend.py`
- Modify: `tests/test_worker_progress_routing.py` only if the worker-progress pump contract changes
- Modify: `tests/test_sandbox_proxy.py` only if Task 4 proves there is still a blocking sync-tool path
- Possibly modify: `tach.toml` if a new module crosses an enforced Tach boundary

## Task 1: Fix the delivery-failure contract with the smallest correct change

**Files:**
- Modify: `src/mindroom/streaming.py`
- Test: `tests/test_streaming_behavior.py`

- [ ] **Step 1: Write the failing delivery-contract regression tests against the current model shape.**

Add focused tests to `tests/test_streaming_behavior.py` for these exact cases.

```python
@pytest.mark.asyncio
async def test_worker_progress_delivery_failure_raises_streaming_delivery_error() -> None:
    async def stream() -> AsyncIterator[str]:
        pump = get_worker_progress_pump()
        assert pump is not None
        pump.queue.put_nowait(
            WorkerProgressEvent(
                tool_name="shell",
                function_name="run",
                progress=WorkerReadyProgress(
                    phase="cold_start",
                    worker_key="worker-a",
                    backend_name="kubernetes",
                    elapsed_seconds=2.0,
                ),
            ),
        )
        await asyncio.sleep(0)
        yield "hello"

    with (
        patch("mindroom.streaming.edit_message_result", new=AsyncMock(side_effect=RuntimeError("edit blew up"))),
        pytest.raises(StreamingDeliveryError, match="edit blew up"),
    ):
        await send_streaming_response(
            ...,
            existing_event_id="$thinking_123",
            adopt_existing_placeholder=True,
            room_mode=True,
        )
```

```python
@pytest.mark.asyncio
async def test_worker_progress_delivery_failure_still_writes_terminal_status() -> None:
    terminal_statuses: list[str] = []
    ...
    assert terminal_statuses[-1] == STREAM_STATUS_ERROR
```

Do not use `timeout_seconds` in Task 1 tests because that model field does not exist yet.

- [ ] **Step 2: Run the new tests to verify they fail on the current implementation.**

Run:

```bash
cd /home/basnijholt/Work/dev/mindroom/.worktrees/pr-issue-183-github
uv run pytest tests/test_streaming_behavior.py -k "worker_progress_delivery_failure" -x -n 0 --no-cov -v
```

Expected:
- One test fails with raw `RuntimeError("edit blew up")`, or the terminal-status assertion fails because the stream is not finalized correctly.

- [ ] **Step 3: Fix the supervision bug inside `streaming.py` before doing any file split.**

Change `src/mindroom/streaming.py` so worker-progress delivery failures are normalized through the same finalize plus `StreamingDeliveryError` path as main-stream delivery failures.
Keep the current module layout for this step.
Do not create `streaming_controller.py` yet.

Use a shape like this.

```python
try:
    await _consume_streaming_chunks(...)
    await _await_progress_task(...)
except Exception as exc:
    await streaming.finalize(client, error=exc)
    raise StreamingDeliveryError(...) from exc
```

Make `_shutdown_worker_progress_drain()` surface or return progress-task delivery failures so `send_streaming_response()` can normalize them through the supervised finalize path.

- [ ] **Step 4: Re-run the targeted tests and make them pass.**

Run:

```bash
cd /home/basnijholt/Work/dev/mindroom/.worktrees/pr-issue-183-github
uv run pytest tests/test_streaming_behavior.py -k "worker_progress_delivery_failure" -x -n 0 --no-cov -v
```

Expected:
- The delivery-contract regressions pass.

- [ ] **Step 5: Decide whether a controller extraction is still justified.**

Read the updated `src/mindroom/streaming.py`.
If the single-writer cleanup is now simple and readable, stop here and keep the logic in `streaming.py`.
If the module still has tangled parallel delivery ownership, extract a dedicated controller into `src/mindroom/streaming_controller.py` and move the tests to patch the controller module’s import site or an injected delivery seam.

- [ ] **Step 6: If you extract a controller, introduce a stable delivery seam before moving tests.**

Use a tiny seam such as an injected delivery adapter or a controller-owned helper import.
Do not keep tests coupled to a patch target that disappears during the refactor.

Use a shape like this.

```python
@dataclass(slots=True)
class StreamDelivery:
    send: Callable[..., Awaitable[DeliveredMatrixEvent | None]]
    edit: Callable[..., Awaitable[DeliveredMatrixEvent | None]]
```

- [ ] **Step 7: Commit the delivery-contract fix.**

```bash
git add src/mindroom/streaming.py tests/test_streaming_behavior.py
if [ -f src/mindroom/streaming_controller.py ]; then
  git add src/mindroom/streaming_controller.py
fi
git commit -m "fix: normalize worker progress delivery failures"
```

## Task 2: Make warmup rendering truthful and visibility-aware

**Files:**
- Modify: `src/mindroom/streaming.py`
- Possibly modify: `src/mindroom/streaming_controller.py`
- Test: `tests/test_streaming_behavior.py`
- Test: `tests/test_streaming_e2e.py`

- [ ] **Step 1: Write the failing rendering-policy tests.**

Add tests for hidden-tool mode and non-hardcoded duration behavior.
Reuse the existing current-branch warmup tests instead of creating duplicate end-to-end coverage.

```python
@pytest.mark.asyncio
async def test_hidden_tool_mode_worker_warmup_uses_generic_copy() -> None:
    streaming = StreamingResponse(..., show_tool_calls=False)
    ...
    assert "Preparing isolated worker" in body
    assert "shell.run" not in body
    assert "python.execute" not in body
```

Update these existing tests in place:
- `tests/test_streaming_behavior.py::TestStreamingBehavior::test_worker_warmup_suffix_renders_outside_partial_markdown`
- `tests/test_streaming_e2e.py::test_streaming_e2e_worker_warmup_edit_sequence`

Make their assertions match the new copy instead of adding a duplicate end-to-end test.

- [ ] **Step 2: Run the rendering tests to verify they fail.**

Run:

```bash
cd /home/basnijholt/Work/dev/mindroom/.worktrees/pr-issue-183-github
uv run pytest \
  tests/test_streaming_behavior.py::TestStreamingBehavior::test_hidden_tool_mode_worker_warmup_uses_generic_copy \
  tests/test_streaming_behavior.py::TestStreamingBehavior::test_worker_warmup_suffix_renders_outside_partial_markdown \
  tests/test_streaming_e2e.py::test_streaming_e2e_worker_warmup_edit_sequence \
  -x -n 0 --no-cov -v
```

Expected:
- The tests fail because current warmup text includes tool names and the hardcoded `2 minutes` copy.

- [ ] **Step 3: Move warmup text generation behind one render policy.**

Render worker progress from stream state plus `show_tool_calls`.
Keep tool labels out of hidden-tool mode entirely.
Do not store user-visible copy in the backend.

Use a shape like this.

```python
def render_worker_status_line(warmup: WorkerWarmupView, *, show_tool_calls: bool) -> str:
    if warmup.phase == "failed":
        ...
    if show_tool_calls and warmup.tool_labels:
        return f"Preparing isolated worker for {', '.join(warmup.tool_labels)}..."
    return "Preparing isolated worker..."
```

- [ ] **Step 4: Replace the hardcoded duration promise with duration-free or timeout-aware copy.**

Use elapsed-only text for non-terminal waiting states.
Use timeout-aware text only when the model later carries a configured timeout.
Do not promise a fixed `2 minutes` from `streaming.py`.

Use copy in this shape.

```python
"Preparing isolated worker..."
"Preparing isolated worker... 17s elapsed."
"Worker startup failed: <error>"
```

- [ ] **Step 5: Re-run the rendering tests and make them pass.**

Run:

```bash
cd /home/basnijholt/Work/dev/mindroom/.worktrees/pr-issue-183-github
uv run pytest \
  tests/test_streaming_behavior.py::TestStreamingBehavior::test_hidden_tool_mode_worker_warmup_uses_generic_copy \
  tests/test_streaming_behavior.py::TestStreamingBehavior::test_worker_warmup_suffix_renders_outside_partial_markdown \
  tests/test_streaming_e2e.py::test_streaming_e2e_worker_warmup_edit_sequence \
  -x -n 0 --no-cov -v
```

Expected:
- The rendering tests pass.

- [ ] **Step 6: Commit the rendering-policy fix.**

```bash
git add src/mindroom/streaming.py tests/test_streaming_behavior.py tests/test_streaming_e2e.py
if [ -f src/mindroom/streaming_controller.py ]; then
  git add src/mindroom/streaming_controller.py
fi
git commit -m "fix: honor hidden tool visibility in warmup rendering"
```

## Task 3: Report the real Kubernetes startup lifecycle instead of the pre-apply guess

**Files:**
- Modify: `src/mindroom/workers/backends/kubernetes.py`
- Modify: `src/mindroom/workers/backends/kubernetes_resources.py`
- Test: `tests/test_kubernetes_worker_backend.py`

- [ ] **Step 1: Write the failing backend tests for recreate-driven cold starts and recreated-startup metadata.**

Add tests in `tests/test_kubernetes_worker_backend.py` for these cases.

```python
def test_kubernetes_backend_reports_progress_for_recreated_ready_deployment() -> None:
    ...
    assert [event.phase for event in events] == ["cold_start", "ready"]
```

```python
def test_kubernetes_backend_recreated_ready_deployment_refreshes_startup_metadata() -> None:
    ...
    assert handle.last_started_at == 11.0
    assert handle.startup_count == 2
```

- [ ] **Step 2: Run the new backend tests to verify they fail.**

Run:

```bash
cd /home/basnijholt/Work/dev/mindroom/.worktrees/pr-issue-183-github
uv run pytest \
  tests/test_kubernetes_worker_backend.py::test_kubernetes_backend_reports_progress_for_recreated_ready_deployment \
  tests/test_kubernetes_worker_backend.py::test_kubernetes_backend_recreated_ready_deployment_refreshes_startup_metadata \
  -x -n 0 --no-cov -v
```

Expected:
- The recreate test fails because progress is decided before `apply_deployment()`.
- The startup-metadata test fails because recreate-driven cold starts keep stale `last_started_at` and `startup_count`.

- [ ] **Step 3: Keep Task 3 scoped to progress plus startup lifecycle metadata.**

Do not widen `WorkerReadyProgress` or the backend interfaces with `timeout_seconds` in this plan.
Task 2 already chose duration-free render copy as the smallest correct change.
Keep this task focused on recreate-driven progress reporting and the stale startup annotations that share the same pre-apply decision bug.

- [ ] **Step 4: Make deployment apply return enough information to decide whether startup reporting is needed.**

Add a tiny apply result in `src/mindroom/workers/backends/kubernetes_resources.py`.

```python
@dataclass(frozen=True, slots=True)
class DeploymentApplyResult:
    recreated: bool
```

Have `apply_deployment()` return `DeploymentApplyResult`.
Use that result in `src/mindroom/workers/backends/kubernetes.py` to decide progress reporting after the real apply path, not before it.
Use the same result to refresh `last_started_at` and `startup_count` when a previously-ready deployment is recreated.

- [ ] **Step 5: Emit progress and lifecycle metadata from the actual startup lifecycle.**

Cover both fresh create and manifest-hash recreate paths.
Remove any dead warmup-only state that becomes unnecessary after the rendering cleanup.

- [ ] **Step 6: Re-run the backend tests and make them pass.**

Run:

```bash
cd /home/basnijholt/Work/dev/mindroom/.worktrees/pr-issue-183-github
uv run pytest \
  tests/test_kubernetes_worker_backend.py::test_kubernetes_backend_reports_progress_for_recreated_ready_deployment \
  tests/test_kubernetes_worker_backend.py::test_kubernetes_backend_recreated_ready_deployment_refreshes_startup_metadata \
  -x -n 0 --no-cov -v
```

Expected:
- Both backend tests pass.

- [ ] **Step 7: Commit the backend lifecycle fix.**

```bash
git add src/mindroom/workers/backends/kubernetes.py src/mindroom/workers/backends/kubernetes_resources.py tests/test_kubernetes_worker_backend.py
git commit -m "fix: report kubernetes worker progress from real startup lifecycle"
```

## Task 4: Prove or disprove the remaining sync-tool responsiveness risk before changing wrappers

**Files:**
- Test: `tests/test_streaming_e2e.py`
- Review: `.venv/lib/python3.12/site-packages/agno/agent/_tools.py`
- Review: `.venv/lib/python3.12/site-packages/agno/tools/toolkit.py`
- Review: `.venv/lib/python3.12/site-packages/agno/tools/function.py`
- Review: `src/mindroom/tool_system/sandbox_proxy.py`
- Review: `src/mindroom/tool_system/runtime_context.py`
- Modify: `src/mindroom/tool_system/sandbox_proxy.py` only if the behavioral test proves there is still a blocking path
- Modify: `src/mindroom/tool_system/runtime_context.py` only if the worker-progress pump contract changes
- Modify: `tests/test_sandbox_proxy.py` only if a wrapper change is actually needed
- Modify: `tests/test_worker_progress_routing.py` only if the worker-progress pump contract changes

- [ ] **Step 1: Write the behavioral test first.**

Add one end-to-end style test named `test_streaming_e2e_real_proxied_sync_tool_warmup_sequence` in `tests/test_streaming_e2e.py`.
Make it exercise a real proxied sync tool call and assert a warmup edit is visible before the tool result completes.
Use a real sync worker-routed tool such as `file` or `python`.
Do not assert only on `toolkit.async_functions` or other registry details.

- [ ] **Step 2: Run the behavioral test and inspect the current async execution path.**

Run:

```bash
cd /home/basnijholt/Work/dev/mindroom/.worktrees/pr-issue-183-github
uv run pytest tests/test_streaming_e2e.py::test_streaming_e2e_real_proxied_sync_tool_warmup_sequence -x -n 0 --no-cov -v
```

Then inspect the current runtime path in:
- `.venv/lib/python3.12/site-packages/agno/agent/_tools.py`
- `.venv/lib/python3.12/site-packages/agno/tools/toolkit.py`
- `.venv/lib/python3.12/site-packages/agno/tools/function.py`

Expected:
- Either the behavioral test already passes because the runtime is offloading correctly, or it proves there is still a real blocking path worth fixing.

- [ ] **Step 3: If the behavioral test already passes, stop here.**

Keep the new behavioral test as a guard.
Do not rewrite sandbox proxy wrappers just to match the earlier hypothesis.
Commit the test-only guard outcome.

```bash
git add tests/test_streaming_e2e.py
git commit -m "test: add sync tool worker progress guard"
```

- [ ] **Step 4: If the behavioral test fails, make the smallest change that restores responsiveness.**

Prefer the smallest real fix in `src/mindroom/tool_system/sandbox_proxy.py`.
Only change wrapper registration if the failing test proves that sync worker-routed calls still block live progress.
If you need a unit test, make it behavioral enough to prove the execution path actually offloads work or removes the blocking path.

- [ ] **Step 5: If Task 4 changed production code, update the focused proxy tests.**

Use `tests/test_sandbox_proxy.py` only for assertions that prove actual execution behavior.
Do not stop at `assert "read_file_chunk" in toolkit.async_functions`.
If the worker-progress pump contract changes, update `tests/test_worker_progress_routing.py` to keep the publisher-only guarantees explicit.

- [ ] **Step 6: If Task 4 changed production code, re-run the relevant tests and commit the full fix.**

Run:

```bash
cd /home/basnijholt/Work/dev/mindroom/.worktrees/pr-issue-183-github
uv run pytest tests/test_streaming_e2e.py tests/test_sandbox_proxy.py tests/test_worker_progress_routing.py -x -n 0 --no-cov -v
```

Expected:
- The behavioral sync-tool test passes.
- Any added unit tests also pass.

Only use this step when Task 4 changed production code.

```bash
git add src/mindroom/tool_system/sandbox_proxy.py src/mindroom/tool_system/runtime_context.py tests/test_streaming_e2e.py tests/test_sandbox_proxy.py tests/test_worker_progress_routing.py
git commit -m "fix: preserve live worker progress for sync tools"
```

## Task 5: Update docs and run the real verification set

**Files:**
- Modify: `docs/streaming.md`
- Modify: `docs/configuration/agents.md`
- Modify: `docs/configuration/index.md`
- Modify: `docs/dev/agent_configuration.md`
- Review: `docs/openai-api.md`
- Possibly modify: `tach.toml`
- Review: all files changed in Tasks 1 through 4

- [ ] **Step 1: Update the user-facing streaming docs to match the final contract.**

In `docs/streaming.md`:
- document that hidden-tool mode may still show generic worker progress text
- state that hidden-tool mode must not reveal tool identifiers or tool-trace metadata
- remove the hardcoded `2 minutes` promise

- [ ] **Step 2: Update the configuration docs wherever `show_tool_calls` behavior is described.**

Update:
- `docs/configuration/agents.md`
- `docs/configuration/index.md`
- `docs/dev/agent_configuration.md`

Keep the wording aligned with the actual rendering contract.

- [ ] **Step 3: Re-check `docs/openai-api.md` and change it only if the Matrix-only note became inaccurate.**

If the `/v1/chat/completions` limitation is still unchanged, leave the file alone.
If not, update the limitation note.

- [ ] **Step 4: Run the targeted regression suite.**

Run:

```bash
cd /home/basnijholt/Work/dev/mindroom/.worktrees/pr-issue-183-github
uv run pytest tests/test_streaming_behavior.py tests/test_streaming_e2e.py tests/test_kubernetes_worker_backend.py tests/test_worker_progress_routing.py tests/test_sandbox_proxy.py -x -n 0 --no-cov -v
```

Expected:
- All targeted tests pass.

- [ ] **Step 5: Run Tach only if a new module or boundary change requires it.**

Run:

```bash
cd /home/basnijholt/Work/dev/mindroom/.worktrees/pr-issue-183-github
uv run tach check --dependencies --interfaces
```

Expected:
- Tach passes if it was relevant to the implementation.

- [ ] **Step 6: Run one final diff review before merging.**

Run:

```bash
cd /home/basnijholt/Work/dev/mindroom/.worktrees/pr-issue-183-github
git status --short
git diff -- src/mindroom/streaming.py src/mindroom/streaming_controller.py src/mindroom/tool_system/runtime_context.py src/mindroom/tool_system/sandbox_proxy.py src/mindroom/workers/backends/kubernetes.py src/mindroom/workers/backends/kubernetes_resources.py docs/streaming.md docs/configuration/agents.md docs/configuration/index.md docs/dev/agent_configuration.md docs/openai-api.md tests/test_streaming_behavior.py tests/test_streaming_e2e.py tests/test_kubernetes_worker_backend.py tests/test_sandbox_proxy.py tests/test_worker_progress_routing.py tach.toml
```

Expected:
- Worker-progress delivery failures are normalized through the standard stream finalization path.
- Hidden-tool mode never renders tool identifiers.
- No hardcoded `2 minutes` copy remains.
- Kubernetes progress reporting covers recreate-driven cold starts.

- [ ] **Step 7: Commit the docs and verification pass.**

```bash
git add docs/streaming.md docs/configuration/agents.md docs/configuration/index.md docs/dev/agent_configuration.md docs/openai-api.md tach.toml
git commit -m "docs: align worker progress streaming contract"
```

Only add the files that actually changed.
