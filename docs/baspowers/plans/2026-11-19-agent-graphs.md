# Agent Graphs (Workflows as Python Scripts) Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: implement this plan task-by-task with the skill named in **Execution**. Steps use checkbox (`- [ ]`) syntax.

**Execution:** baspowers:subagent-driven-development, because 21 tasks across 8 pull requests share interfaces, later tasks would run on a compacted context, and the engine touches approvals, delegation and restart recovery where a shipped mistake is costly.

**Goal:** An agent starts a Python workflow that runs many agents in the background, survives restarts by replaying finished calls, pauses for people, and shows as a live graph in MindRoom Chat.

**Architecture:** A workflow run is a durable record with a node per SDK call; each attempt is one script run of the existing script runtime, and the stdlib SDK `mindroom.workflow` talks to a `WorkflowEngine` through new routes on the script gateway.
Agent nodes run as delegation jobs in `ToolJobRuntime`, the run itself is one job of kind `workflow`, and graph frames ride the live-frames transport next to the Sub-agents feed.
MindRoom Chat adds a frame store, two message cards, a jobs-bar row and a lazy graph panel.

**Tech Stack:** Python 3.13, FastAPI, SQLite, `jsonschema`, pytest; TypeScript, React, vanilla-extract, vitest, Playwright.

**Spec:** `docs/baspowers/specs/2026-11-19-agent-graphs-design.md`

## Global Constraints

- Toolkit `workflow` with exactly `start_workflow`, `get_workflow`, `cancel_workflow`, `resume_workflow`, `list_workflows`, `save_workflow`; SDK module `mindroom.workflow`; backend package `mindroom.workflow_runs`; run IDs `wf-<32 hex>`.
- The `start_workflow` description, the five one-line descriptions, the SDK signatures and docstrings, and the error messages are copied verbatim from the spec; a pull request adds only the lines for functions it ships.
- Defaults: `max_concurrent` 16, `max_concurrent_runs` 8, `max_runtime_hours` 72, `max_tool_calls_per_minute` 600, `prefix_stagger_seconds` 5, `large_run_notice_usd` 10 and 25 agents, schema attempts 5, automatic resumes without progress 5, long-poll 25 s, `read_peer_messages` wait cap 3600 s.
- Sizes: node request 1 MiB, node result 8 MiB, source 128 KiB, frame 60,000 bytes, card snapshot 40 KB, message 64 KiB, state value 256 KiB, label 80 characters.
- Room state carries IDs only (`workflow`, `job_id`); titles, labels, prompts and results never enter unencrypted state.
- The script never runs inside the primary process; `mindroom.workflow` imports only the standard library and `mindroom.script_runs.launch_contract`.
- No change to `dynamic_workflow` code; no new third-party dependency in either repository; no graph library in Chat.
- Backend: `ruff format` only changed files, new internal imports get a `tach.toml` entry (`uv run tach check --dependencies --interfaces`), tests use `@pytest_asyncio.fixture`.
- Docs: one sentence per line, no em dashes, only sentences that answer a user question; `docs/tools/workflows.md` grows in the pull request that ships the behavior.
- Every commit ends with `--trailer "Assisted-by: MindRoom <display name>:<model id> <https://github.com/mindroom-ai/mindroom>"`.
- Fixtures, tests and screenshots use invented placeholder data only.
- `PYTEST <file>` below means `NIX_PATH=nixpkgs=flake:nixpkgs nix-shell --run 'uv run pytest <file> -x -n 0 --no-cov'`; run each new test file once with default addopts before pushing.

## Review Focus

1. A script with no `main`, or one that calls `main()` itself: the run fails with the spec's message and starts no agent twice (Task 2).
2. A `foreach` over 1,000 items: at most 256 script threads, at most `max_concurrent` running nodes, frames at most 60,000 bytes, jobs state at most 27,000 bytes, and a Chat panel that renders 24 chips for a group of 300 (Tasks 7, 10, 11, 21).
3. A restart while a question is open, with the answer arriving before the new attempt reaches that call: the answer is returned and the question is not posted twice (Task 13).
4. An answer, approval or resume from an account that is not the run's requester: refused, and the run keeps waiting (Tasks 11, 13).
5. A resumed `foreach` whose list was reordered, grew, or contains the same value twice: finished items are reused, and equal items get distinct lanes (Tasks 7, 8, 9).

## Pull requests and branches

| PR | Branch (from fresh `origin/main`) | Tasks | Net source estimate |
|---|---|---|---|
| B1 | `feat/workflow-runs-core` | 1 to 4 | +650 |
| B2 | `feat/workflow-agents` | 5 to 7 | +450 |
| B3 | `feat/workflow-resume` | 8, 9 | +350 |
| B4 | `feat/workflow-graph-frames` | 10, 11 | +350 |
| B5 | `feat/workflow-pauses` | 12 to 14 | +400 |
| B6 | `feat/workflow-saved-sessions-peers` | 15 to 17 | +500 |
| C1 | mindroom-chat `feat/workflow-run-card` | 18, 19 | +450 |
| C2 | mindroom-chat `feat/workflow-graph-panel` | 20, 21 | +500 |

Each pull request is reviewed by Sol and Astra before it opens, opens as Bas, and is linked to the thread at once.
B1 to B4 merge in order; C1 needs B4; B5 and B6 are independent of each other after B3; C2 needs C1 and B6 (message edges).
A pull request that passes its estimate by more than 40% stops for the `stopping-runaway-prs` check.

## File structure

Backend, new:

| File | Responsibility |
|---|---|
| `src/mindroom/workflow.py` | The SDK that runs inside the script process: lanes and keys, gateway client, public functions, `_run_main`. |
| `src/mindroom/workflow_runs/models.py` | `WorkflowRun`, `WorkflowNode`, states, `NodeRequest`, `NodeReceipt`, ID validation. |
| `src/mindroom/workflow_runs/store.py` | `WorkflowRunStore`: SQLite tables and every read and write of them. |
| `src/mindroom/workflow_runs/replay.py` | Pure replay rules: digest, claim, dirty lanes. |
| `src/mindroom/workflow_runs/engine.py` | `WorkflowEngine`: start, attempts, node dispatch, concurrency, waiting, finish, cancel, resume. |
| `src/mindroom/workflow_runs/agent_nodes.py` | One agent or turn node as a delegation job; prefix stagger. |
| `src/mindroom/workflow_runs/structured.py` | Schema check, prompt suffix, JSON extraction, validation. |
| `src/mindroom/workflow_runs/questions.py` | `ask` nodes: card, answer, `!answer`. |
| `src/mindroom/workflow_runs/saved.py` | Saved workflow files: parse, list, save. |
| `src/mindroom/workflow_runs/graph.py` | Pure graph snapshot, delta, and fitting to a byte limit. |
| `src/mindroom/live_frames/workflow.py` | `io.mindroom.workflow` frame publisher. |
| `src/mindroom/api/workflow_gateway.py` | Routes under `/api/script-gateway/workflow/{script_run_id}/`. |
| `src/mindroom/custom_tools/workflow.py`, `src/mindroom/tools/workflow.py` | The `workflow` toolkit and its registration. |
| `src/mindroom/custom_tools/workflow_peers.py` | The run-scoped toolkit every workflow agent gets. |
| `src/mindroom/orchestration/workflow_runtime.py` | Builds the engine, wires it to the script runtime, job runtime and API, runs startup recovery. |

Backend, modified: `script_runs/models.py`, `store.py`, `manager.py`, `policy.py`, `broker.py` (workflow attempts), `tool_jobs/execution.py`, `publication.py`, `client_cancel.py`, `orchestration/tool_job_runtime.py` (job kind), `delegation/lifecycle.py`, `targets.py` (grant), `matrix/journal_ingress.py`, `matrix/event_types.py` (events), `config/main.py` (validation), `orchestrator.py`, `api/main.py`, `tach.toml`, `zensical.toml`, `docs/tools/workflows.md`.

Chat, new: `src/app/matrix/protocol/workflowFrame.ts`, `workflowRunContent.ts`, `cancelBackgroundJob.ts` (moved from `background-jobs/`), `src/app/messages/live/workflowRunStore.ts`, `bindWorkflowFrames.ts`, `src/app/messages/workflow-run/WorkflowRunCard.tsx`, `WorkflowQuestionCard.tsx`, `src/app/navigation/workflowPanel.ts`, `src/app/subagents/workflow/workflowGraphModel.ts`, `WorkflowPanel.tsx`, `WorkflowPanelPart.tsx`, `workflowPanel.css.ts`.
Chat, modified: `messages/content/renderAgentMessageContent.tsx`, `background-jobs/backgroundJobsPresentation.ts`, `BackgroundJobRow.tsx`, `matrix/protocol/backgroundJobsContract.ts`, `shell/useBindAtoms.ts`, `room/Room.tsx`, `docs/architecture.md`, the 17 locale overlays, `e2e/fixtures/`, `e2e/background-jobs/`, `e2e/workflow-run/`.

---

## PR B1: runs, SDK core, toolkit

### Task 1: Run store

**Files:**
- Create: `src/mindroom/workflow_runs/__init__.py`, `src/mindroom/workflow_runs/models.py`, `src/mindroom/workflow_runs/store.py`
- Modify: `tach.toml`
- Test: `tests/test_workflow_run_store.py`

**Interfaces:**
- Produces:
  - `WorkflowRunState(StrEnum)`: `STARTING, RUNNING, WAITING, COMPLETED, FAILED, CANCELLED, INTERRUPTED`; `TERMINAL_RUN_STATES`.
  - `WorkflowNodeState(StrEnum)`: `QUEUED, RUNNING, WAITING, COMPLETED, FAILED, CANCELLED`.
  - `WorkflowRun` (frozen dataclass): `run_id, agent_name, owner_user_id, room_id, thread_root_event_id, execution_identity: dict, title: str | None, source: str, source_digest: str, args: object, max_concurrent: int, max_cost_usd: float | None, deadline_at: str, depth: int = 0, parent_run_id: str | None = None, parent_node_key: str | None = None, state = STARTING, attempt: int = 0, idle_resumes: int = 0, previous_source_digest: str | None = None, script_run_id: str | None = None, job_id: str | None = None, card_event_id: str | None = None, result: object | None = None, error: str | None = None, created_at: str, finished_at: str | None = None`.
  - `WorkflowNode` (frozen dataclass): `run_id, key, seq: int, kind: str, lane: str, idx: int, digest: str, state, waiting_on: str | None, label: str, name: str | None, phase: str | None, group_key: str | None, session_key: str | None, attempt: int, cached: bool, started_at: str | None, ended_at: str | None, usage: dict, result: object | None, error: dict | None, links: dict[str, str]` (link names: `delegation_id`, `subagent_id`, `job_id`, `event_id`, `child_run_id`).
  - `NodeRequest(key: str, kind: str, args: dict, meta: dict)`, `NodeReceipt(id: str, state: str, cached: bool, result: object | None, error: dict | None)`; a node's public ID is `f"n{seq}"`.
  - `new_workflow_run_id() -> str`, `validated_workflow_run_id(run_id) -> str` (pattern `wf-[0-9a-f]{32}`).
  - `WorkflowRunStore(path: Path)` with `create_run(run)`, `get_run(run_id)` (raises `WorkflowRunNotFoundError`), `update_run(run_id, **changes) -> WorkflowRun`, `list_runs(*, agent_name=None, owner_user_id=None, room_id=None, thread_root_event_id=None, include_finished=True)`, `run_for_script(script_run_id) -> WorkflowRun | None`, `put_node(node) -> WorkflowNode` (assigns the next `seq` to a new key, keeps it for an existing one), `get_node(run_id, key)`, `node_by_seq(run_id, seq)`, `nodes(run_id)`, `drop_nodes(run_id, keys)`, `append_log(run_id, text)`, `log(run_id, limit=50)`, `prune(finished_before: str) -> int`.

- [ ] **Step 1: Write the failing tests**

```python
def test_put_node_assigns_stable_sequence(tmp_path):
    store = WorkflowRunStore(tmp_path / "w.sqlite3")
    store.create_run(_run("wf-" + "a" * 32))
    first = store.put_node(_node(key="@0"))
    second = store.put_node(_node(key="@1"))
    again = store.put_node(replace(first, state=WorkflowNodeState.COMPLETED, result={"big": "x" * 200_000}))
    assert (first.seq, second.seq, again.seq) == (1, 2, 1)
    assert store.get_node(first.run_id, "@0").result == {"big": "x" * 200_000}

def test_drop_nodes_and_run_for_script(tmp_path):
    store = WorkflowRunStore(tmp_path / "w.sqlite3")
    run = _run("wf-" + "b" * 32)
    store.create_run(run)
    store.update_run(run.run_id, script_run_id="script-" + "1" * 32, attempt=1)
    store.put_node(_node(run_id=run.run_id, key="@0"))
    store.drop_nodes(run.run_id, ["@0"])
    assert store.nodes(run.run_id) == []
    assert store.run_for_script("script-" + "1" * 32).attempt == 1

def test_prune_removes_only_old_finished_runs(tmp_path): ...  # a running run and a run finished after the cutoff both stay
```

- [ ] **Step 2: Run** `PYTEST tests/test_workflow_run_store.py`. Expected: FAIL, `ModuleNotFoundError: mindroom.workflow_runs`.
- [ ] **Step 3: Implement the models and the store.** Follow `script_runs/store.py` for connection handling and durable writes; JSON columns for `execution_identity`, `args`, `usage`, `result`, `error`, `links`; tables `workflow_runs`, `workflow_nodes` (primary key `run_id, key`, unique `run_id, seq`), `workflow_log`.
- [ ] **Step 4: Run** the same command. Expected: PASS. Run `uv run tach check --dependencies --interfaces`. Expected: no errors.
- [ ] **Step 5: Commit** `feat(workflow): durable run and node store`.

### Task 2: SDK core

**Files:**
- Create: `src/mindroom/workflow.py`, `tests/workflow_helpers.py`
- Modify: `tach.toml`
- Test: `tests/test_workflow_sdk.py`

**Interfaces:**
- Consumes: `launch_contract.GATEWAY_URL_ENV, RUN_ID_ENV, TOKEN_PATH_ENV`; routes of Task 3.
- Produces (this task): `tool`, `shell`, `ShellResult(ok: bool, exit_code: int, output: str)`, `phase`, `log`, `WorkflowError(message, *, kind)`, `AgentError`, `ToolError`, `_run_main(namespace: dict) -> None`, and the private pieces later tasks extend:
  - `_Lane` (thread-local): `path: str`, `next_index: int`, `phase: str | None`; `_enter_root_lane()` gives the calling thread the root lane `""`; `_next_key() -> str` returns `f"{lane.path}@{index}"` and raises the spec's raw-thread error when the thread has no lane.
  - `_Gateway`: `run_info() -> dict`, `submit(key, kind, args, meta) -> dict`, `wait(node_id) -> dict` (long-poll with `?wait=25` until the state is `completed`, `failed` or `cancelled`), `log(text)`, `finish(*, result=None, error=None)`; base URL `f"{gateway}/workflow/{script_run_id}"`; retries retryable transport and 5xx errors without limit; exits the process with code 3 on `409`.
  - `_call(kind: str, args: dict, *, label: str | None = None, name: str | None = None) -> object`: next key, submit, wait, return `result` or raise by `error["kind"]`.
  - `tests/workflow_helpers.py`: `FakeWorkflowGateway` (a threaded `http.server` that records requests and answers node requests from a `respond(request) -> receipt` callable) and `sdk_env(monkeypatch, gateway)`.
- `shell(command, *, timeout=None)` calls `tool("shell", "run_shell_command", args=command + '; printf "\\n__MINDROOM_EXIT__=%s\\n" "$?"', timeout=timeout)` and parses and removes the marker line; a missing marker gives `exit_code=-1`.
- `_run_main`: start the parent watchdog thread (poll `os.getppid()` every second; when it differs from the value at start, `os.killpg(os.getpgrp(), signal.SIGKILL)`), refuse when a node was already submitted (the spec's "Remove your own main() call" message), require a callable `main` (the spec's "Define def main(args)" message), call `main(run_info["args"])`, post `finish(result=...)`; on an exception print the traceback, post `finish(error=<last 4096 bytes of the traceback>)` and exit 1.

- [ ] **Step 1: Write the failing tests**

```python
def test_keys_count_calls_in_the_root_lane(gateway, sdk):
    sdk._enter_root_lane()
    sdk.tool("website", "read_url", url="https://example.org")
    sdk.phase("Check")
    sdk.tool("website", "read_url", url="https://example.org")
    assert [(r["key"], r["kind"], r["meta"]["phase"]) for r in gateway.nodes] == [("@0", "tool", None), ("@1", "tool", "Check")]

def test_shell_reports_exit_code_and_strips_the_marker(gateway, sdk):
    gateway.respond = lambda request: completed("2 failed\n\n__MINDROOM_EXIT__=1\n")
    result = sdk.shell("pytest -x")
    assert (result.ok, result.exit_code, result.output) == (False, 1, "2 failed\n")

def test_failed_tool_raises_tool_error_with_kind(gateway, sdk):
    gateway.respond = lambda request: failed({"kind": "indeterminate", "message": "may have run"})
    with pytest.raises(sdk.ToolError) as error:
        sdk.tool("gmail", "send_email", to="a@example.org")
    assert error.value.kind == "indeterminate"

def test_run_main_without_main_reports_what_to_do(gateway, sdk):
    with pytest.raises(SystemExit) as exit_info:
        sdk._run_main({})
    assert exit_info.value.code == 1
    assert "Define def main(args): and return the report. MindRoom calls it." in gateway.finished["error"]

def test_run_main_refuses_a_script_that_already_called_main(gateway, sdk):
    sdk._enter_root_lane(); sdk.tool("t", "f")
    with pytest.raises(SystemExit):
        sdk._run_main({"main": lambda args: "twice"})
    assert "Remove your own main() call." in gateway.finished["error"]
    assert len(gateway.nodes) == 1

def test_call_from_a_raw_thread_says_to_use_foreach(gateway, sdk):
    caught = []
    def work():
        try:
            sdk.tool("t", "f")
        except sdk.WorkflowError as error:
            caught.append(str(error))
    thread = threading.Thread(target=work); thread.start(); thread.join()
    assert "Call agent() from main or inside foreach()/parallel()." in caught[0]

def test_superseded_attempt_exits_with_code_3(gateway, sdk): ...   # gateway answers 409; SystemExit code 3
def test_sdk_imports_only_stdlib_and_launch_contract(): ...        # parse src/mindroom/workflow.py with ast; assert the import set
```

- [ ] **Step 2: Run** `PYTEST tests/test_workflow_sdk.py`. Expected: FAIL, `ModuleNotFoundError: mindroom.workflow`.
- [ ] **Step 3: Implement `src/mindroom/workflow.py`** with the interfaces above; reuse `script_sdk`'s request and error classification by copying its 30-line `_request` shape, not by importing `MindRoomTools`.
- [ ] **Step 4: Run** the same command. Expected: PASS.
- [ ] **Step 5: Commit** `feat(workflow): SDK core with tool, shell, phase, log`.

### Task 3: Engine core, gateway routes, workflow attempts

**Files:**
- Create: `src/mindroom/workflow_runs/engine.py`, `src/mindroom/api/workflow_gateway.py`, `src/mindroom/orchestration/workflow_runtime.py`
- Modify: `src/mindroom/script_runs/models.py`, `store.py`, `manager.py`, `policy.py`, `broker.py`, `src/mindroom/orchestrator.py`, `src/mindroom/api/main.py`, `tach.toml`
- Test: `tests/test_workflow_engine.py`, `tests/test_workflow_gateway.py`, `tests/test_workflow_launch.py`, `tests/test_workflow_local_integration.py`

**Interfaces:**
- Consumes: Task 1 store and models; `ScriptRunManager`, `ScriptToolBroker`, `ScriptRunRecord`.
- Produces:
  - `ScriptRunRecord.workflow_run_id: str | None = None`, stored in a new nullable column.
  - `ScriptRunManager.run(..., workflow_run_id: str | None = None)`: with it set, the run is local whenever no worker backend is configured (no `MINDROOM_SANDBOX_EXECUTION_MODE` needed), is allowed on `darwin`, has `preapprove_launch_grants=True`, and takes its grants from `resolve_script_launch_grants(context, workflow=True)`.
  - `policy._WORKFLOW_RESTRICTED_TOOLKITS = frozenset({"compact_context", "dynamic_tools", "slack"})`, used instead of `_SCRIPT_RESTRICTED_TOOLKITS` when `workflow=True`; `controls_run` functions stay excluded.
  - `ScriptToolBroker.execute_for_workflow(run: ScriptRunRecord, *, call_id: str, grant: ScriptToolGrant, arguments: dict) -> ScriptCallRecord`: claims the call and awaits its settled receipt.
  - `WORKFLOW_TRAILER = "\n\n# Added by MindRoom: calls main(args) and reports what it returns.\nfrom mindroom.workflow import _run_main as _mindroom_run_main\n_mindroom_run_main(globals())\n"`; the attempt launches `run.source + WORKFLOW_TRAILER`.
  - `WorkflowLimits(max_concurrent_runs: int = 8, max_runtime_hours: float = 72, max_tool_calls_per_minute: int = 600, max_concurrent_agents: int | None = None)`.
  - `WorkflowEngine(store, scripts, broker, clock)` with:
    - `async start(context: ToolRuntimeContext, *, source: str, args: object, title: str | None, max_concurrent: int, max_cost_usd: float | None, limits: WorkflowLimits) -> WorkflowRun`
    - `async cancel(run_id: str, *, reason: str) -> WorkflowRun`, `async wait(run_id: str) -> WorkflowRun` (returns once terminal), `status(run_id) -> tuple[WorkflowRun, list[WorkflowNode]]`
    - `async reconcile() -> None`: maps ended attempts to run states; called from the script runtime's maintenance loop.
    - Gateway side, where `attempt` is the authenticated `ScriptRunRecord`: `run_info(attempt) -> dict` (`run_id`, `attempt`, `args`), `async submit_node(attempt, request: NodeRequest) -> NodeReceipt`, `async node_receipt(attempt, node_id: str, *, wait: float) -> NodeReceipt`, `append_log(attempt, text)`, `async finish(attempt, *, result, error)`.
    - `SupersededAttemptError`, raised when `attempt.run_id != run.script_run_id`.
  - Routes under `/api/script-gateway/workflow/{script_run_id}`: `GET /run`, `POST /nodes`, `GET /nodes/{node_id}?wait=`, `POST /log`, `POST /finish`; bearer checked with `broker.authenticate`; `404` for an unknown or unauthorized run, `409` for a superseded attempt, `413` above 1 MiB.
  - `WorkflowRuntimeLifecycle` (in `orchestration/workflow_runtime.py`): `engine`, `async start()`, `async shutdown()`; built in `orchestrator.py` next to `build_script_runtime` and exposed as `orchestrator.workflow_runtime`.
- Run state from the attempt: `exited` after `finish(result=...)` is `completed`; `finish(error=...)` or a `failed` exit is `failed` with that error or the last 4 KiB of output; `exited` with no `finish` is `failed` with `The script ended without returning from main(args).`; `cancelled` and `interrupted` map to themselves.
- A `tool` node's args are `{"toolkit", "function", "arguments"}`; its label is `"<toolkit>.<function>"`; its broker call ID is `uuid5(NAMESPACE_OID, f"{attempt.run_id}:{key}").hex`.

- [ ] **Step 1: Write the failing tests**

```python
async def test_tool_node_runs_through_broker_and_saves_result(engine, attempt, broker):
    broker.results[("website", "read_url")] = "ok"
    receipt = await engine.submit_node(attempt, NodeRequest("@0", "tool", {"toolkit": "website", "function": "read_url", "arguments": {"url": "https://example.org"}}, {"phase": None}))
    final = await engine.node_receipt(attempt, receipt.id, wait=5)
    assert (final.state, final.result, final.cached) == ("completed", "ok", False)
    assert engine.status(attempt.workflow_run_id)[1][0].label == "website.read_url"

async def test_finish_then_exit_completes_run_and_wakes_waiters(engine, attempt, scripts):
    waiter = asyncio.create_task(engine.wait(attempt.workflow_run_id))
    await engine.finish(attempt, result="report", error=None)
    scripts.end(attempt.run_id, ScriptRunState.EXITED)
    await engine.reconcile()
    run = await waiter
    assert (run.state, run.result) == (WorkflowRunState.COMPLETED, "report")

async def test_exit_without_finish_fails_and_says_why(engine, attempt, scripts):
    scripts.end(attempt.run_id, ScriptRunState.EXITED)
    await engine.reconcile()
    assert engine.status(attempt.workflow_run_id)[0].error == "The script ended without returning from main(args)."

async def test_cancel_revokes_attempt_and_keeps_completed_nodes(engine, attempt, scripts): ...
    # scripts.cancelled == [attempt.run_id]; run.state is CANCELLED; the completed node is still in store.nodes(run_id)

async def test_ninth_active_run_is_refused(engine, context): ...
    # pytest.raises(WorkflowError, match="8 workflow runs are already active for you and this agent. Cancel one or wait.")

def test_node_request_over_one_mebibyte_is_413(client, token): ...
def test_unknown_token_is_404(client): ...
def test_long_poll_returns_when_the_node_changes(client, token): ...

async def test_workflow_attempt_runs_locally_without_worker_or_flag(manager, context):
    run = await manager.run(context, source="x = 1", workflow_run_id="wf-" + "a" * 32)
    assert (run.local_unsafe, run.workflow_run_id, run.preapprove_launch_grants) == (True, "wf-" + "a" * 32, True)

async def test_plain_script_still_needs_explicit_local_mode(manager, context):
    with pytest.raises(ScriptRunManagerError, match="Background scripts require a worker or an explicitly disabled sandbox."):
        await manager.run(context, source="x = 1")

def test_workflow_grants_include_memory_and_delegate_but_not_compact_context(context):
    names = {grant.toolkit_name for grant in resolve_script_launch_grants(context, workflow=True)}
    assert {"memory", "delegate"} <= names and not names & {"compact_context", "dynamic_tools", "slack"}

async def test_real_local_script_returns_its_report(local_stack):   # harness of tests/test_script_recovery_integration.py
    run = await local_stack.start("from mindroom.workflow import log\n\ndef main(args):\n    log('hi')\n    return {'n': args['n'] + 1}\n", args={"n": 1})
    done = await local_stack.engine.wait(run.run_id)
    assert (done.state, done.result, local_stack.store.log(run.run_id)) == (WorkflowRunState.COMPLETED, {"n": 2}, ["hi"])
```

- [ ] **Step 2: Run** `PYTEST tests/test_workflow_engine.py tests/test_workflow_gateway.py tests/test_workflow_launch.py tests/test_workflow_local_integration.py`. Expected: FAIL on missing modules and the unknown `workflow_run_id` argument.
- [ ] **Step 3: Implement** the interfaces above. The engine keeps one `asyncio.Condition` per run for long-polls and `wait`; node execution tasks are owned by the engine and cancelled by `cancel`. Register the router in `api/main.py` beside `script_gateway_router` and on the gateway-only listener.
- [ ] **Step 4: Run** the same command, then `PYTEST tests/test_script_run_manager.py tests/test_script_tool_broker.py tests/test_script_run_policy.py`. Expected: PASS, including the unchanged script tests.
- [ ] **Step 5: Commit** `feat(workflow): engine, gateway routes and workflow attempts`.

### Task 4: The `workflow` toolkit and job kind

**Files:**
- Create: `src/mindroom/custom_tools/workflow.py`, `src/mindroom/tools/workflow.py`, `docs/tools/workflows.md`
- Modify: `src/mindroom/tool_jobs/execution.py`, `src/mindroom/tool_jobs/publication.py`, `src/mindroom/orchestration/tool_job_runtime.py`, `src/mindroom/workflow_runs/engine.py`, `src/mindroom/config/main.py`, `docs/architecture/security-posture.md`, `zensical.toml`, `tach.toml`
- Test: `tests/test_workflow_tool.py`

**Interfaces:**
- Consumes: `WorkflowEngine` (Task 3), `JobSpec`, `BackgroundOutcome`, `ToolJobRuntime`.
- Produces:
  - `WorkflowTools(Toolkit)` with config fields `max_concurrent_runs=8`, `max_runtime_hours=72`, `max_tool_calls_per_minute=600`, `max_concurrent_agents=None`, `large_run_notice_usd=10`, `prefix_stagger_seconds=5`, registered as `workflow` (`executes_code=True`, `requires_room_context=True`, primary runtime).
  - In this pull request: `start_workflow(source=None, path=None, args=None, title=None, max_concurrent=16) -> str`, `get_workflow(run_id, nodes="active") -> str`, `cancel_workflow(run_id) -> str`.
  - `WorkflowTools.managed_job(function_name, arguments, *, job_id, runtime) -> JobSpec | str | None`: for `start_workflow` it starts the run and returns `JobSpec(tool_name="workflow", kind="workflow", adapter={"workflow": run_id}, operation=..., cancel=...)`; `None` for the other functions.
  - `tool_jobs/execution.py`: `_call_job` asks any toolkit that has `managed_job` (a `ManagedJobToolkit` protocol), not only `DelegateTools`; `_default_wait` is `0.0` for `start_workflow`, so the handle returns at once unless `background_after` is given.
  - `workflow_job_outcome(run: WorkflowRun) -> BackgroundOutcome`: `completed` with the report (a string as is, any other value as indented JSON), `failed` with `run.error`, `cancelled`, `interrupted`.
  - `orchestration/tool_job_runtime.py`: `continues` is true for kinds `delegation` and `workflow`; cancelling a `workflow` job calls `engine.cancel`.
  - `tool_jobs/publication.py`: a `workflow` job's state is `{job_id, thread_id, kind: "workflow", tool: "workflow", status, started_at, workflow}`.
  - Engine: at a terminal state it writes `.mindroom/workflow_runs/<run_id>/source.py`, `report.md` or `result.json`, and `nodes.jsonl` into the owner's workspace and never reads them back.
  - Config: an agent with both `workflow` and `dynamic_workflow` fails validation with `Remove dynamic_workflow: the workflow tool replaces it and both define list_workflows.`
- `get_workflow` returns `{run_id, title, status, attempt, counts, usage, waiting: [...], nodes: [{id, kind, label, status, error}]}`.

- [ ] **Step 1: Write the failing tests**

```python
async def test_start_workflow_returns_a_handle_and_its_job_delivers_the_report(tools, engine, runtime, managed_call):
    handle = json.loads(await managed_call(tools, "start_workflow", source=SCRIPT, args={"n": 1}, title="Count things"))
    assert handle["status"] == "running" and handle["run_id"].startswith("wf-") and handle["title"] == "Count things"
    engine.end(handle["run_id"], WorkflowRunState.COMPLETED, result="done")
    job = await runtime.wait_terminal(handle["job_id"])
    assert (job.kind, job.status, job.adapter["workflow"]) == ("workflow", "completed", handle["run_id"])
    assert await runtime.result_text(job) == "done"

def test_jobs_state_names_the_run_and_carries_no_title(workflow_job):
    assert _job_state(workflow_job) == {"job_id": workflow_job.job_id, "thread_id": "$thread", "kind": "workflow", "tool": "workflow",
                                        "status": "running", "started_at": workflow_job.created_at, "workflow": "wf-" + "a" * 32}

async def test_start_workflow_needs_exactly_one_source(tools):
    assert "Give exactly one of source or path." in await tools.start_workflow()

async def test_cancelling_the_job_cancels_the_run(tools, engine, runtime): ...   # engine.cancelled == [run_id]; job.status == "cancelled"
async def test_restart_keeps_a_workflow_job_running(runtime_after_restart, workflow_job): ...   # recovered job.status == "running"
async def test_failed_run_delivers_the_error_tail(tools, engine, runtime): ...   # job.status == "failed"; result text ends with the traceback tail
def test_finished_run_is_exported_to_the_workspace(engine, workspace): ...   # source.py, report.md and nodes.jsonl exist under .mindroom/workflow_runs/<run_id>/
def test_config_refuses_workflow_with_dynamic_workflow(): ...   # ValidationError matching the message above
```

- [ ] **Step 2: Run** `PYTEST tests/test_workflow_tool.py`. Expected: FAIL, `ModuleNotFoundError: mindroom.custom_tools.workflow`.
- [ ] **Step 3: Implement.** The `start_workflow` docstring is the spec's block with only the lines for `tool`, `shell`, `phase`, `log` and the example reduced to those calls; later tasks add their lines. Write `docs/tools/workflows.md` (enable the tool, options table, where the script runs, what the script can call) and add it to `zensical.toml`; add local workflow scripts and the workspace export to `security-posture.md`.
- [ ] **Step 4: Run** `PYTEST tests/test_workflow_tool.py tests/test_job_tools.py tests/test_background_delegation.py tests/test_event_journal_tool_jobs.py`. Expected: PASS.
- [ ] **Step 5: Commit** `feat(workflow): workflow toolkit and background job kind`. Then run the full suite on gce-dev, review with Sol and Astra, open PR B1.

---

## PR B2: agents in parallel

### Task 5: The `workflow` delegation grant

**Files:**
- Modify: `src/mindroom/delegation/lifecycle.py`, `src/mindroom/delegation/targets.py`, `src/mindroom/delegation/state.py`
- Test: `tests/test_workflow_targets.py`

**Interfaces:**
- Produces: `_DelegationGrant = Literal["delegate", "dynamic_workflow", "workflow"]`; `DelegationChild.workflow_run_id: str | None = None`; `delegation_grant(child)` returns `"workflow"` when it is set.
- With grant `workflow`, `authorize_delegation` and `resolve_fresh_target` allow the caller itself (plain or authored), every agent in the caller's `delegate_to`, and every agent for which `is_sender_allowed_for_agent_reply_in_room(requester, agent, config, room_id, runtime_paths, memberships)` holds; depth, private-agent, model and requester checks are unchanged.

- [ ] **Step 1: Write the failing tests**

```python
def test_workflow_grant_allows_self_without_delegate_to(matrix_context, config):
    assert isinstance(authorize_delegation("dev", "dev", "task", config=config, runtime_paths=paths, execution_identity=identity, depth=0, grant="workflow"), Config)

def test_workflow_grant_allows_a_room_agent_the_requester_may_address(matrix_context, config): ...   # "researcher" is in the room, not in delegate_to: Config
def test_workflow_grant_refuses_an_agent_outside_room_and_delegate_to(matrix_context, config): ...   # str containing "Cannot delegate to 'stranger'"
def test_workflow_grant_keeps_the_depth_limit(matrix_context, config): ...   # depth=3: "Cannot delegate: the maximum delegation depth was reached."
def test_delegate_grant_is_unchanged(matrix_context, config): ...   # self without delegate_to is still refused
```

- [ ] **Step 2: Run** `PYTEST tests/test_workflow_targets.py`. Expected: FAIL (grant `workflow` is unknown).
- [ ] **Step 3: Implement** the grant in `caller_grants_child`, `_target_refusal` and `delegation_grant`.
- [ ] **Step 4: Run** `PYTEST tests/test_workflow_targets.py tests/test_delegation_targets.py tests/test_delegate_tools.py`. Expected: PASS.
- [ ] **Step 5: Commit** `feat(workflow): delegation grant for workflow agents`.

### Task 6: Agent nodes as delegation jobs

**Files:**
- Create: `src/mindroom/workflow_runs/agent_nodes.py`
- Modify: `src/mindroom/workflow_runs/engine.py`, `src/mindroom/workflow.py`, `src/mindroom/tool_jobs/publication.py`, `src/mindroom/custom_tools/workflow.py`, `docs/tools/workflows.md`
- Test: `tests/test_workflow_agent_nodes.py`, `tests/test_workflow_sdk.py`

**Interfaces:**
- Consumes: `resolve_fresh_target`, `prepare_child_turn`, `delegation_call_job`, `run_delegated_child_response`, `ToolJobRuntime.start/wait/cancel`, Task 5 grant.
- Produces:
  - `AgentCall(prompt: str, agent: str | None, model: str | None, system: str | None, tools: tuple[str, ...] | None, skills: tuple[str, ...] | None, knowledge: tuple[str, ...] | None, profile: str | None, minimal: bool, timeout: float | None, schema: dict | None, name: str | None)` and `AgentCall.from_args(args: dict)`.
  - `AgentResult(text: str, usage: dict, cost_usd: float, delegation_id: str, subagent_id: str, job_id: str)`.
  - `AgentNodeError(message, *, kind)` with kind `failed | cancelled | denied | timeout | refused`.
  - `agent_job_id(owner: ToolExecutionIdentity, run_id: str, key: str) -> str`: SHA-256 of the sorted JSON `{"owner", "workflow", "node"}`.
  - `async run_agent_node(context, run, node, call, *, jobs: ToolJobRuntime, on_waiting: Callable[[str | None], None], previous: DelegationChild | None = None) -> AgentResult`.
  - SDK: `agent(prompt, *, schema=None, name=None, agent=None, model=None, system=None, tools=None, skills=None, knowledge=None, profile=None, minimal=False, timeout=None)` calling `_call("agent", {...})`; until Task 12 a `schema` raises `WorkflowError("schema is not available yet in this MindRoom version.")`.
- The child is prepared at depth `run.depth + 1` with `workflow_run_id=run.run_id`, mode `standard` unless `minimal`, and runs as `jobs.start(job_id, kind="delegation", adapter={**job.adapter, "workflow": run.run_id, "node": node.key}, reattach=True, ...)`; the node waits with `approval_timeout=None` and `timeout=call.timeout`.
- The subagent job's room state gains `"workflow": run_id` and nothing else.
- The engine sets the node `waiting` with `waiting_on="approval"` while the job is `awaiting_approval`, and the run `waiting` when nothing runs or is queued.

- [ ] **Step 1: Write the failing tests** (model turns come from `tests/scripted_agent_helpers.py`)

```python
async def test_agent_node_runs_a_child_job_and_saves_text_and_cost(stack):
    receipt = await stack.submit_agent("@0", prompt="Say hi")
    final = await stack.engine.node_receipt(stack.attempt, receipt.id, wait=10)
    node = stack.store.get_node(stack.run_id, "@0")
    assert final.result == "hi" and node.usage["cost_usd"] >= 0 and node.links.keys() >= {"job_id", "subagent_id", "delegation_id"}
    assert stack.jobs.job(node.links["job_id"]).adapter["workflow"] == stack.run_id

async def test_child_that_needs_approval_waits_and_resumes(stack):
    receipt = await stack.submit_agent("@0", prompt="Send the mail", script=[needs_approval("send_email"), says("sent")])
    await stack.until(lambda: stack.store.get_node(stack.run_id, "@0").waiting_on == "approval")
    assert stack.engine.status(stack.run_id)[0].state is WorkflowRunState.WAITING
    await stack.approve_open_card()
    assert (await stack.engine.node_receipt(stack.attempt, receipt.id, wait=10)).result == "sent"

async def test_denied_approval_fails_the_node_with_kind_denied(stack): ...
async def test_cancelling_one_node_job_fails_only_that_node(stack): ...   # error kind "cancelled"; the run stays RUNNING
async def test_timeout_cancels_the_child_and_reports_timeout(stack): ...
async def test_plain_copy_inherits_delegate_memory_and_workflow(stack): ...   # child toolkit names >= {"delegate", "memory", "workflow"}
async def test_unknown_model_is_refused_with_the_model_list(stack): ...   # kind "refused"; message starts "Cannot delegate: Unknown model 'opus5'. Available models:"
def test_subagent_job_state_carries_only_the_workflow_id(node_job): ...   # _job_state(job)["workflow"] == run_id; no "node", no label
def test_sdk_agent_raises_agent_error_with_kind(gateway, sdk): ...
```

- [ ] **Step 2: Run** `PYTEST tests/test_workflow_agent_nodes.py`. Expected: FAIL, `ModuleNotFoundError: mindroom.workflow_runs.agent_nodes`.
- [ ] **Step 3: Implement** `run_agent_node` and the engine's `agent` kind; add the `agent(...)` lines to the `start_workflow` docstring and the docs page.
- [ ] **Step 4: Run** `PYTEST tests/test_workflow_agent_nodes.py tests/test_workflow_sdk.py tests/test_background_delegation.py tests/test_delegation_approval_ownership.py`. Expected: PASS.
- [ ] **Step 5: Commit** `feat(workflow): agent nodes run as delegation jobs`.

### Task 7: `foreach`, `parallel`, the run limit and the prefix stagger

**Files:**
- Modify: `src/mindroom/workflow.py`, `src/mindroom/workflow_runs/engine.py`, `src/mindroom/workflow_runs/agent_nodes.py`, `src/mindroom/script_runs/broker.py`, `docs/tools/workflows.md`
- Test: `tests/test_workflow_sdk.py`, `tests/test_workflow_concurrency.py`

**Interfaces:**
- Produces:
  - SDK `foreach(items, fn, *, max_concurrent=None) -> list` and `parallel(fns, *, max_concurrent=None) -> list`.
    Each first submits a `group` node (`_call("group", {"op": "foreach"})` or `"parallel"`, with `meta["total"]`) at index i, then runs every function on a pool of `min(len, max_concurrent or 256)` threads, each in the child lane `f"{lane}/{i}:{item_key}"` with the parent's phase.
  - `_item_key(item, seen: dict[str, int]) -> str`: the first 12 hex characters of the SHA-256 of `json.dumps(item, sort_keys=True)`, with `#k` appended for the k-th equal item (k from 2), or `p<position>` when the item is not JSON; `parallel` uses `p<position>`.
  - A function that raises gives `None`, and the SDK logs `foreach item 3 failed: <error>`.
  - Engine: a per-run limit of `run.max_concurrent` on nodes in `RUNNING` of kinds `agent`, `turn`, `tool`; others wait in `QUEUED`; a node in `WAITING` does not count; `cancel` starts no queued node.
  - `prefix_key(call: AgentCall, target: str) -> str` and `PrefixGate(stagger_seconds: float, clock)` with `async enter(prefix: str) -> None` and `first_frame(prefix: str) -> None`.
  - Broker: the per-run lock becomes a per-run `asyncio.Semaphore` of size 1, or of the run's `max_concurrent` when `run.workflow_run_id` is set.

- [ ] **Step 1: Write the failing tests**

```python
def test_foreach_lanes_are_keyed_by_item_value(gateway, sdk):
    sdk._enter_root_lane()
    sdk.foreach(["b", "a", "b"], lambda item: sdk.tool("t", "f", x=item))
    digest = lambda value: hashlib.sha256(json.dumps(value, sort_keys=True).encode()).hexdigest()[:12]
    keys = sorted(request["key"] for request in gateway.nodes if request["kind"] == "tool")
    assert keys == sorted([f"/0:{digest('a')}@0", f"/0:{digest('b')}@0", f"/0:{digest('b')}#2@0"])

def test_foreach_keeps_order_and_gives_none_for_a_failed_item(gateway, sdk):
    gateway.respond = lambda request: (failed({"kind": "failed", "message": "boom"}) if request["args"].get("prompt") == "b"
                                       else completed(request["args"].get("prompt", "").upper()))
    sdk._enter_root_lane()
    assert sdk.foreach(["a", "b", "c"], lambda item: sdk.agent(item)) == ["A", None, "C"]

def test_parallel_given_results_says_to_pass_functions(sdk):
    sdk._enter_root_lane()
    with pytest.raises(sdk.WorkflowError, match=r"parallel\(\) takes functions\."):
        sdk.parallel(["already", "called"])

def test_foreach_of_1000_items_opens_at_most_256_requests(gateway, sdk): ...   # gateway.peak_open_requests <= 256; all 1000 results returned

async def test_engine_runs_at_most_max_concurrent_nodes(stack): ...   # max_concurrent=2, 6 agent nodes: peak RUNNING == 2, the rest QUEUED
async def test_waiting_node_frees_its_slot(stack): ...
async def test_cancel_starts_no_queued_node(stack): ...   # after cancel, child_runner.started has only the first 2 keys
async def test_stagger_holds_siblings_until_the_first_frame(clock): ...   # 3 equal prefixes: 1 enters; first_frame releases the other 2 together
async def test_stagger_releases_after_five_seconds_without_a_frame(clock): ...
async def test_different_prefixes_are_not_held(clock): ...
async def test_plain_script_calls_stay_serialized(broker): ...   # peak concurrent executions == 1
async def test_workflow_tool_calls_run_in_parallel(broker): ...   # peak == 3 with max_concurrent=3
```

- [ ] **Step 2: Run** `PYTEST tests/test_workflow_sdk.py tests/test_workflow_concurrency.py`. Expected: FAIL (`foreach` is missing).
- [ ] **Step 3: Implement.** `foreach` feeds items to the pool through an iterator so 1,000 items never create more than 256 threads; the first live frame of a child's Sub-agents feed calls `PrefixGate.first_frame`.
- [ ] **Step 4: Run** the same command plus `PYTEST tests/test_script_tool_broker.py`. Expected: PASS.
- [ ] **Step 5: Commit** `feat(workflow): foreach, parallel, run limit and prompt-cache stagger`. Then full suite, a live run of spec example 1 on a throwaway stack (`just live up`), Sol and Astra review, open PR B2.

---

## PR B3: replay and resume

### Task 8: Replay rules

**Files:**
- Create: `src/mindroom/workflow_runs/replay.py`
- Modify: `src/mindroom/workflow_runs/engine.py`
- Test: `tests/test_workflow_replay.py`

**Interfaces:**
- Produces:
  - `node_digest(kind: str, args: dict) -> str`: SHA-256 of the canonical JSON of `{"kind", "args"}`; the SDK never puts `name`, `timeout`, `label` or a group's `total` in `args`.
  - `split_key(key: str) -> tuple[str, int]`: `("/0:ab12", 3)` for `"/0:ab12@3"`.
  - `later_keys(keys: Iterable[str], key: str) -> tuple[str, ...]` (sorted): keys in the same lane with a larger index, and keys in lanes whose path starts with `f"{lane}/{j}:"` for `j >= index`.
  - `Claim(action: Literal["hit", "attach", "run"], dropped: tuple[str, ...])` and `claim(nodes: Mapping[str, WorkflowNode], key: str, digest: str) -> Claim`: completed with the same digest is `hit`; queued, running or waiting with the same digest is `attach`; anything else is `run` with `dropped=later_keys(nodes, key)`.
- Engine `submit_node` applies `claim`: a hit returns `NodeReceipt(cached=True)` and stamps the node with the current attempt; a run drops `claim.dropped` and replaces the node.

- [ ] **Step 1: Write the failing tests**

```python
def test_completed_node_with_same_digest_hits():
    assert claim({"@0": done("@0", "d1")}, "@0", "d1") == Claim("hit", ())

def test_live_node_with_same_digest_attaches():
    assert claim({"@0": waiting("@0", "d1")}, "@0", "d1").action == "attach"

def test_changed_call_drops_later_calls_and_lanes_opened_after_it():
    nodes = {key: done(key, "d") for key in ["@0", "@1", "@2", "/0:x@0", "/2:y@0", "/2:y/0:z@0"]}
    assert claim(nodes, "@1", "new") == Claim("run", ("/2:y/0:z@0", "/2:y@0", "@2"))

def test_sibling_lanes_are_untouched():
    nodes = {key: done(key, "d") for key in ["/0:a@0", "/0:a@1", "/0:b@0"]}
    assert claim(nodes, "/0:a@0", "new").dropped == ("/0:a@1",)

def test_failed_node_with_same_digest_runs_again():
    assert claim({"@0": failed_node("@0", "d1"), "@1": done("@1", "d")}, "@0", "d1") == Claim("run", ("@1",))

def test_digest_ignores_key_order_and_differs_by_kind():
    assert node_digest("agent", {"a": 1, "b": 2}) == node_digest("agent", {"b": 2, "a": 1}) != node_digest("turn", {"a": 1, "b": 2})
```

- [ ] **Step 2: Run** `PYTEST tests/test_workflow_replay.py`. Expected: FAIL, `ModuleNotFoundError`.
- [ ] **Step 3: Implement** `replay.py` as pure functions and call `claim` in `submit_node`; the gateway computes the digest from the request and never trusts one from the script.
- [ ] **Step 4: Run** `PYTEST tests/test_workflow_replay.py tests/test_workflow_engine.py`. Expected: PASS.
- [ ] **Step 5: Commit** `feat(workflow): replay rules for saved nodes`.

### Task 9: Resume, automatic resume, `now` and `random`

**Files:**
- Modify: `src/mindroom/workflow_runs/engine.py`, `src/mindroom/custom_tools/workflow.py`, `src/mindroom/orchestration/workflow_runtime.py`, `src/mindroom/orchestration/tool_job_runtime.py`, `src/mindroom/workflow.py`, `docs/tools/workflows.md`
- Test: `tests/test_workflow_resume.py`, `tests/test_workflow_sdk.py`

**Interfaces:**
- Produces:
  - `WorkflowEngine.resume(context, run_id, *, source: str | None = None, automatic: bool = False) -> WorkflowRun`: refuses a run with a live attempt (`Run <id> is still running. Cancel it first.`); stores `previous_source_digest`, the new source when given, `attempt + 1` and a new deadline; launches the next attempt; nodes left `queued` or `running` whose job is gone become `failed` with kind `interrupted`; open `ask` nodes stay.
  - `AUTO_RESUME_REASONS = frozenset({RUNTIME_RESTARTED, RUNTIME_SHUTDOWN, PLUGIN_TOOLS_CHANGED, WORKER_CONFIGURATION_CHANGED, AGENT_ISOLATION_CHANGED, SUPERVISOR_UNAVAILABLE})` (from `script_runs/reasons.py`).
  - `WorkflowEngine.recover(context_for: Callable[[WorkflowRun], ToolRuntimeContext | None]) -> list[str]`: at startup and in `reconcile`, resumes every run whose attempt ended `interrupted` for one of those reasons and whose requester is still authorized; `idle_resumes` counts automatic resumes since the last completed node, and at 5 the run ends `interrupted` with `Stopped resuming after 5 attempts that finished nothing. Fix the script and call resume_workflow.`
  - Startup order in `WorkflowRuntimeLifecycle.start`: script runtime recovery, then `engine.recover`, then the job runtime continues each stopped `workflow` job with a `JobSpec` whose operation waits for that run, or ends it when the run is terminal.
  - `resume_workflow(run_id, source=None, path=None) -> str`: the same handle shape as `start_workflow`; when the run's job has ended it registers a new job for the run.
  - SDK `now() -> datetime` and `random() -> float` as `value` nodes (`{"op": "now"}`, `{"op": "random"}`); the gateway creates the value (UTC ISO string, `secrets.SystemRandom().random()`).
  - The notice of the spec's Determinism paragraph, written with `store.append_log` when a completed node is replaced, `run.source_digest == run.previous_source_digest`, and no earlier node of that lane ran in this attempt.

- [ ] **Step 1: Write the failing tests**

```python
async def test_resume_returns_saved_results_and_runs_only_what_failed(stack):
    await stack.complete_agent("@0", prompt="a", text="A")
    await stack.fail_agent("@1", prompt="b")
    await stack.end_attempt(ScriptRunState.FAILED)
    await stack.engine.resume(stack.context, stack.run_id)
    first = await stack.submit_agent("@0", prompt="a")
    second = await stack.submit_agent("@1", prompt="b")
    assert (first.cached, first.result, second.cached) == (True, "A", False)
    assert stack.child_runner.started == ["@0", "@1", "@1"]

async def test_old_attempt_is_refused_after_resume(stack):
    old = stack.attempt
    await stack.end_attempt(ScriptRunState.INTERRUPTED)
    await stack.engine.resume(stack.context, stack.run_id)
    with pytest.raises(SupersededAttemptError):
        await stack.engine.submit_node(old, agent_request("@0", prompt="a"))

async def test_reordered_foreach_reuses_finished_items_and_runs_the_new_one(stack): ...
    # attempt 1 completes lanes /0:<a> and /0:<b>; attempt 2 submits /0:<b>, /0:<c>, /0:<a>: cached == [True, False, True]
async def test_restart_resumes_the_run_and_keeps_its_job_running(stack): ...   # recover() == [run_id]; job.status == "running"; attempt == 2
async def test_cancelled_run_is_not_resumed_at_startup(stack): ...
async def test_run_of_a_requester_without_access_is_not_resumed(stack): ...
async def test_automatic_resume_stops_after_five_idle_attempts(stack): ...   # state INTERRUPTED and the message above
async def test_now_and_random_replay_their_values(stack): ...   # equal results in attempts 1 and 2, cached True
async def test_changed_call_without_a_script_edit_logs_the_notice(stack): ...   # store.log(run_id)[-1].startswith("Call n2 changed between attempts without a script edit.")
async def test_resume_with_edited_source_launches_the_new_source(stack): ...   # scripts.launched[-1].source == NEW + WORKFLOW_TRAILER
```

- [ ] **Step 2: Run** `PYTEST tests/test_workflow_resume.py`. Expected: FAIL (`resume` is missing).
- [ ] **Step 3: Implement** the interfaces above and add `resume_workflow`, `now` and `random` to the docstring and the docs page.
- [ ] **Step 4: Run** `PYTEST tests/test_workflow_resume.py tests/test_workflow_sdk.py tests/test_script_recovery_integration.py tests/test_script_runtime_lifecycle.py`. Expected: PASS.
- [ ] **Step 5: Commit** `feat(workflow): resume by replay, also after a restart`. Then full suite, a live restart test (start spec example 1, restart the backend mid-run, the report still arrives), Sol and Astra review, open PR B3.

---

## PR B4: graph frames and the run card

### Task 10: Graph snapshot (pure)

**Files:**
- Create: `src/mindroom/workflow_runs/graph.py`
- Test: `tests/test_workflow_graph.py`

**Interfaces:**
- Produces:
  - `node_label(kind: str, args: dict, name: str | None) -> str`: the name, else the first line of the prompt or message, through `redact_sensitive_text(max_length=80)`; the engine stores it on the node when the node is created.
  - `projected_cost_usd(nodes) -> float`: spent plus the average cost of completed `agent` and `turn` nodes times the count of running and queued ones.
  - `graph_meta(run, nodes, *, large: bool) -> dict` and `graph_snapshot(run, nodes, *, edges, log, max_bytes: int) -> dict` with exactly the spec's frame fields; a snapshot that does not fit keeps every group, every node that is not completed, the newest completed nodes that fit, and `"more_done": <count left out>`; a group row's `total`, `done` and `failed` count all of its nodes, also the ones left out, and a node row's `error` is the error's message string.
  - `graph_delta(before: dict, after: dict) -> dict | None`: changed and new node and group rows, the full `edges` list when it changed, and `log_append`; `None` when nothing changed.

- [ ] **Step 1: Write the failing tests**

```python
def test_label_is_name_or_first_prompt_line_redacted_and_cut():
    assert node_label("agent", {"prompt": "Audit auth/session.py\nDetails"}, None) == "Audit auth/session.py"
    assert node_label("agent", {"prompt": "x"}, "critic") == "critic"
    assert len(node_label("agent", {"prompt": "a" * 500}, None)) <= 80
    assert "hunter2" not in node_label("agent", {"prompt": "Fetch https://bob:hunter2@example.org/x"}, None)

def test_snapshot_of_1000_nodes_fits_and_keeps_groups_and_unfinished_nodes():
    snapshot = graph_snapshot(RUN, nodes_in_one_group(completed=980, running=15, failed=5), edges=[], log=[], max_bytes=60_000)
    shown_done = sum(node["status"] == "completed" for node in snapshot["nodes"])
    assert len(json.dumps(snapshot).encode()) <= 60_000 and len(snapshot["groups"]) == 1
    assert sum(node["status"] != "completed" for node in snapshot["nodes"]) == 20 and snapshot["more_done"] == 980 - shown_done

def test_projection_adds_the_average_finished_cost_per_open_agent():
    assert projected_cost_usd(agents(completed=[1.0, 2.0, 3.0], running=2, queued=1)) == pytest.approx(12.0)

def test_delta_has_only_changed_rows_and_new_log_lines(): ...
def test_phases_keep_first_appearance_order(): ...
def test_card_snapshot_fits_40_kilobytes(): ...
```

- [ ] **Step 2: Run** `PYTEST tests/test_workflow_graph.py`. Expected: FAIL, `ModuleNotFoundError`.
- [ ] **Step 3: Implement** `graph.py` with no I/O.
- [ ] **Step 4: Run** the same command. Expected: PASS.
- [ ] **Step 5: Commit** `feat(workflow): graph snapshot model`.

### Task 11: Frames, card message, control event

**Files:**
- Create: `src/mindroom/live_frames/workflow.py`
- Modify: `src/mindroom/workflow_runs/engine.py`, `src/mindroom/workflow_runs/agent_nodes.py`, `src/mindroom/custom_tools/workflow.py`, `src/mindroom/matrix/event_types.py`, `src/mindroom/matrix/journal_ingress.py`, `src/mindroom/orchestration/workflow_runtime.py`, `docs/streaming.md`, `docs/tools/workflows.md`
- Test: `tests/test_workflow_frames.py`, `tests/test_workflow_control.py`

**Interfaces:**
- Consumes: Task 10; the live-frames transport and pacing used by `live_frames/subagents.py`.
- Produces:
  - `WORKFLOW_FRAME_EVENT_TYPE = "io.mindroom.workflow"`, `WORKFLOW_RUN_CONTENT_KEY = "io.mindroom.workflow_run"`, `WORKFLOW_CONTROL_EVENT_TYPE = "io.mindroom.workflow_control"`.
  - `WorkflowFramePublisher.update(run_id: str, *, room_id: str, meta: dict, snapshot: dict) -> None` and `close(run_id)`: frames `{"v": 1, "run", "sid", "seq", "meta", "snapshot" | "delta"}` (`sid` from `new_publisher_id()`, one per run and process, as subagent streams have), a full snapshot first and every 10 s while the run is not terminal, deltas between, at most one frame per 125 ms per account and room, encrypted as subagent frames are.
  - Engine: every node or run change marks the run dirty; a running node's live usage comes from its child's feed through `run_agent_node(..., on_usage: Callable[[dict], None])`.
  - Card: `start_workflow` posts one agent message with `io.mindroom.workflow_run` (`{run, job_id, requester, title, status, counts, usage, phases, attempt, started_at, ended_at}`, `requester` being `run.owner_user_id`) and the fallback body `Workflow "<title>" <status>: <n> done, <n> running, <n> waiting. Stop it with !cancel_job <job id>.`; its event ID is saved as `card_event_id`.
    The engine edits it when the run starts or stops waiting, at most once per 60 s while it runs, and at the end, when the content also carries `graph` (the last snapshot fitted to 40 KB).
  - `EventKind.WORKFLOW_CONTROL` in `journal_ingress.py` and `async handle_workflow_control(event, *, engine, context_for) -> bool`: `{run, action: "resume"}` from `run.owner_user_id` resumes; anything else returns `False` and changes nothing.

- [ ] **Step 1: Write the failing tests**

```python
async def test_first_frame_is_a_snapshot_then_deltas_then_a_snapshot_every_ten_seconds(publisher, client, clock): ...
    # frames[0] has "snapshot"; a node change gives a frame with "delta" naming only that node; after clock.advance(10) a "snapshot"
async def test_frames_are_wrapped_in_encrypted_rooms(publisher, client): ...   # content == {"v": 1, "encrypted": ANY}
async def test_running_node_usage_is_live(stack): ...   # the frame's node usage follows on_usage before the node settles
async def test_card_is_edited_on_waiting_and_on_end_and_at_most_once_a_minute(stack, clock): ...
async def test_final_card_edit_carries_a_fitted_graph(stack): ...   # len(json.dumps(content["io.mindroom.workflow_run"]["graph"])) <= 40_000
def test_card_content_and_room_state_never_meet(stack): ...   # the jobs state has no title, label or phase; the card has no token or source
async def test_resume_control_from_the_requester_resumes_the_run(stack): ...
async def test_resume_control_from_another_user_changes_nothing(stack): ...   # returns False; attempt unchanged
```

- [ ] **Step 2: Run** `PYTEST tests/test_workflow_frames.py tests/test_workflow_control.py`. Expected: FAIL, `ModuleNotFoundError: mindroom.live_frames.workflow`.
- [ ] **Step 3: Implement.** Share the stream pacing with `live_frames/subagents.py` instead of copying it; if that needs a small extraction, do it in this task with the subagent tests green.
- [ ] **Step 4: Run** the same command plus `PYTEST tests/live_frames`. Expected: PASS.
- [ ] **Step 5: Commit** `feat(workflow): live graph frames, run card and resume control`. Then full suite, live test with 200 synthetic agents (frame size, jobs-state size under 27,000 bytes, no homeserver rate limit), Sol and Astra review, open PR B4.

---

## PR B5: structured output, questions, cost

### Task 12: Structured output

**Files:**
- Create: `src/mindroom/workflow_runs/structured.py`
- Modify: `src/mindroom/workflow_runs/agent_nodes.py`, `src/mindroom/workflow.py`, `docs/tools/workflows.md`
- Test: `tests/test_workflow_structured.py`

**Interfaces:**
- Produces:
  - `SCHEMA_ATTEMPTS = 5`; `SchemaError(ValueError)`.
  - `check_schema(schema: object) -> None`: raises `SchemaError("schema is not valid JSON Schema: <path>: <reason>.")` using `jsonschema.Draft202012Validator.check_schema`.
  - `schema_prompt(prompt: str, schema: dict) -> str`: the prompt, a blank line, `End your answer with one fenced json block that matches this JSON Schema:`, and the schema as indented JSON.
  - `extract_json(text: str) -> object`: the last fenced `json` block, else the whole text; raises `SchemaError("no JSON value found")`.
  - `validation_error(value: object, schema: dict) -> str | None`: the first error as `<path>: <message>`.
- `run_agent_node` with a schema: check it first (kind `schema`, no child starts), run the child on `schema_prompt`, and on a failure continue the same child with `Your answer did not match the schema: <error>. Reply with only the corrected fenced json block.` until attempt 5, then raise `AgentNodeError(kind="schema")` with the spec's message.
- The node result is the parsed value; usage and cost sum all attempts.

- [ ] **Step 1: Write the failing tests**

```python
async def test_invalid_schema_fails_before_any_agent_starts(stack):
    error = await stack.agent_error("@0", prompt="x", schema={"type": "objekt"})
    assert error["kind"] == "schema" and error["message"].startswith("schema is not valid JSON Schema:") and stack.child_runner.started == []

async def test_second_attempt_fixes_the_answer_in_the_same_session(stack):
    result = await stack.agent_result("@0", prompt="x", schema=OK_SCHEMA, script=[says("no json here"), says('```json\n{"ok": true}\n```')])
    assert result == {"ok": True} and len(stack.child_sessions()) == 1 and stack.child_turns() == 2

async def test_five_bad_answers_fail_with_the_last_error(stack): ...
    # message == "Agent 'agent-1' did not return JSON matching the schema after 5 attempts. Last error: ok: 'yes' is not of type 'boolean'"
def test_extract_takes_the_last_fenced_block(): ...
def test_validation_error_names_the_path(): ...
def test_schema_is_part_of_the_digest(): ...   # node_digest differs with and without schema
```

- [ ] **Step 2: Run** `PYTEST tests/test_workflow_structured.py`. Expected: FAIL, `ModuleNotFoundError`.
- [ ] **Step 3: Implement**; remove the Task 6 refusal of `schema` in the SDK.
- [ ] **Step 4: Run** `PYTEST tests/test_workflow_structured.py tests/test_workflow_agent_nodes.py`. Expected: PASS.
- [ ] **Step 5: Commit** `feat(workflow): structured agent output with retries`.

### Task 13: `ask_user`

**Files:**
- Create: `src/mindroom/workflow_runs/questions.py`
- Modify: `src/mindroom/workflow_runs/engine.py`, `src/mindroom/workflow.py`, `src/mindroom/matrix/event_types.py`, `src/mindroom/matrix/journal_ingress.py`, `src/mindroom/tool_jobs/client_cancel.py`, `docs/tools/workflows.md`
- Test: `tests/test_workflow_questions.py`

**Interfaces:**
- Produces:
  - `WORKFLOW_QUESTION_CONTENT_KEY = "io.mindroom.workflow_question"` (`{run, node, requester, text, options, free_text, status: "open" | "answered" | "cancelled", answer}`), `WORKFLOW_ANSWER_EVENT_TYPE = "io.mindroom.workflow_answer"` (`{run, node, option | text}`), `EventKind.WORKFLOW_ANSWER`.
  - `async post_question(context, run, node, *, text: str, options: list[str] | None) -> str`: sends the card with the fallback body (the question, numbered options, `Answer with !answer <text>.`) and returns its event ID, saved in `node.links["event_id"]`.
  - `async answer_question(engine, *, run_id: str, node_id: str, sender: str, option: str | None, text: str | None) -> bool`: `False` unless the sender is `run.owner_user_id`, the node waits on `user`, and the option is one of its options; on success the node completes with the answer and the card is edited to status `answered` with `answer` and the body `Answered: <answer>`.
  - `async answer_command(engine, *, room_id, thread_id, sender, text) -> str` for `!answer`: answers the oldest open question of the thread, a bare number picks that option, and with none open it returns `No open question in this thread.`
  - Engine kind `ask`: the node is `waiting` with `waiting_on="user"` and does not count against the run limit; an answer with no live attempt is saved; `cancel` edits open cards to status `cancelled` and the body `Cancelled`.
  - SDK `ask_user(question, options=None)`: `_call("ask", {"question", "options"})`; options that are not a list of non-empty strings raise `WorkflowError("options must be a list of strings, such as ['Yes', 'No'].")`.

- [ ] **Step 1: Write the failing tests**

```python
async def test_ask_waits_then_returns_the_chosen_option(stack):
    receipt = await stack.submit_ask("@0", question="Deploy?", options=["Yes", "No"])
    assert stack.engine.status(stack.run_id)[0].state is WorkflowRunState.WAITING
    assert await answer_question(stack.engine, run_id=stack.run_id, node_id=receipt.id, sender=stack.requester, option="Yes", text=None)
    assert (await stack.engine.node_receipt(stack.attempt, receipt.id, wait=5)).result == "Yes"

async def test_answer_from_another_user_is_refused(stack):
    receipt = await stack.submit_ask("@0", question="Deploy?", options=["Yes", "No"])
    assert not await answer_question(stack.engine, run_id=stack.run_id, node_id=receipt.id, sender="@mallory:example.org", option="Yes", text=None)
    assert stack.store.get_node(stack.run_id, "@0").state is WorkflowNodeState.WAITING

async def test_answer_during_a_restart_is_returned_without_asking_twice(stack):
    receipt = await stack.submit_ask("@0", question="Deploy?", options=None)
    await stack.end_attempt(ScriptRunState.INTERRUPTED)
    assert await answer_question(stack.engine, run_id=stack.run_id, node_id=receipt.id, sender=stack.requester, option=None, text="after lunch")
    await stack.engine.resume(stack.context, stack.run_id)
    again = await stack.submit_ask("@0", question="Deploy?", options=None)
    assert (again.cached, again.result, len(stack.sent_questions)) == (True, "after lunch", 1)

async def test_answer_command_answers_the_oldest_open_question(stack): ...
async def test_answer_command_number_picks_the_option(stack): ...
async def test_unknown_option_is_refused(stack): ...
async def test_cancel_closes_open_question_cards(stack): ...
def test_sdk_refuses_options_that_are_not_strings(sdk): ...
```

- [ ] **Step 2: Run** `PYTEST tests/test_workflow_questions.py`. Expected: FAIL, `ModuleNotFoundError`.
- [ ] **Step 3: Implement**; route `io.mindroom.workflow_answer` like `io.mindroom.tool_approval_response`, and `!answer` beside `!cancel_job`.
- [ ] **Step 4: Run** the same command. Expected: PASS.
- [ ] **Step 5: Commit** `feat(workflow): ask_user pauses the run for an answer`.

### Task 14: Cost notice and budget pause

**Files:**
- Modify: `src/mindroom/workflow_runs/engine.py`, `src/mindroom/custom_tools/workflow.py`, `docs/tools/workflows.md`
- Test: `tests/test_workflow_cost.py`

**Interfaces:**
- Produces:
  - `start_workflow(..., max_cost_usd=None)`.
  - The run is marked `large` once, when it has more than 25 `agent` or `turn` nodes or `projected_cost_usd` passes `large_run_notice_usd` (0 turns the cost test off); the engine logs `Large run: <n> agents, about $<projected> projected.` and edits the card.
  - With `max_cost_usd`, when spent reaches it the engine starts no queued node and creates one system `ask` node with key `$budget:<n>`, text `This run has spent $<spent> of its $<limit> limit. Continue?` and options `["Continue with $<limit> more", "Stop"]`; Continue adds the original amount to the limit, Stop cancels the run.

- [ ] **Step 1: Write the failing tests**

```python
async def test_large_notice_fires_once_at_the_26th_agent(stack): ...   # exactly one log line starting "Large run: 26 agents"
async def test_large_notice_fires_when_the_projection_passes_ten_dollars(stack): ...
async def test_budget_pause_holds_queued_nodes_and_asks(stack): ...
    # limit 1.00, node costs 0.60 each, max_concurrent 1: after node 2 the run is WAITING on "budget" and child_runner.started has 2 keys
async def test_continue_adds_the_limit_and_releases_queued_nodes(stack): ...   # run.max_cost_usd == 2.00; node 3 starts
async def test_stop_cancels_the_run(stack): ...
async def test_without_a_limit_nothing_pauses(stack): ...
```

- [ ] **Step 2: Run** `PYTEST tests/test_workflow_cost.py`. Expected: FAIL.
- [ ] **Step 3: Implement** in the engine's settle path, reusing Task 13's `ask` node for the question.
- [ ] **Step 4: Run** the same command. Expected: PASS.
- [ ] **Step 5: Commit** `feat(workflow): large-run notice and max_cost_usd pause`. Then full suite, Sol and Astra review, open PR B5.

---

## PR B6: saved workflows, sessions, peers

### Task 15: Saved workflows

**Files:**
- Create: `src/mindroom/workflow_runs/saved.py`
- Modify: `src/mindroom/custom_tools/workflow.py`, `src/mindroom/workflow.py`, `src/mindroom/tool_jobs/client_cancel.py`, `docs/tools/workflows.md`, `docs/tools/agent-orchestration.md`
- Test: `tests/test_workflow_saved.py`

**Interfaces:**
- Produces:
  - `SavedWorkflow(name: str, description: str, args: dict[str, str], path: Path)`.
  - `saved_workflow_path(workspace: Path, name: str) -> Path`: the name matches `[a-z0-9][a-z0-9_-]{0,63}`, the path is `workspace/workflows/<name>.py`, and symlinks are refused.
  - `read_saved(path) -> SavedWorkflow` and `list_saved(workspace) -> list[SavedWorkflow]`, using `ast` only: the description is the first docstring line, `args` is a literal `ARGS` dict or `{}`.
  - `save_source(workspace, name, source, description) -> Path`: adds the docstring when the source has none; raises `SavedWorkflowExistsError("workflows/<name>.py already exists with different content. Pick another name or edit the file.")`.
  - Toolkit: `start_workflow(name=...)` (exactly one of `source`, `path`, `name`), `list_workflows()` returning `{saved: [...], runs: [...]}` for this conversation, `save_workflow(run_id, name, description=None)`; `!workflows` prints the same listing.
  - SDK `run(name, args=None)`: reads `workflows/<name>.py` under `MINDROOM_SCRIPT_WORKSPACE_ROOT`, submits a `group` node `{"op": "run", "name", "source_digest"}`, loads the file with `runpy.run_path(..., run_name="__workflow__")` in the child lane `f"{lane}/{i}:run"`, and returns its `main(args)`.
- `docs/tools/agent-orchestration.md` marks Dynamic Workflows as superseded by `workflow`, in one sentence with a link.

- [ ] **Step 1: Write the failing tests**

```python
def test_read_saved_parses_without_running_the_file(tmp_path):
    path = tmp_path / "workflows" / "review-branch.py"
    path.parent.mkdir()
    path.write_text('"""Review every changed file."""\nARGS = {"base": "Branch to compare against"}\nraise SystemExit("must not run")\n')
    assert read_saved(path) == SavedWorkflow("review-branch", "Review every changed file.", {"base": "Branch to compare against"}, path)

def test_save_refuses_to_replace_a_different_file(tmp_path): ...   # SavedWorkflowExistsError with the message above; an identical file is fine
def test_bad_names_and_symlinks_are_refused(tmp_path): ...   # "../x", "Has Space", a symlinked workflows directory
async def test_start_by_name_snapshots_the_file(tools, workspace): ...   # editing the file after start does not change run.source
async def test_save_workflow_writes_the_run_source_with_a_docstring(tools, workspace): ...
async def test_list_workflows_has_saved_and_this_conversations_runs(tools): ...
def test_sdk_run_calls_main_in_a_child_lane(gateway, sdk, workspace): ...   # node keys of the inner calls start with "/0:run@"
```

- [ ] **Step 2: Run** `PYTEST tests/test_workflow_saved.py`. Expected: FAIL, `ModuleNotFoundError`.
- [ ] **Step 3: Implement.**
- [ ] **Step 4: Run** `PYTEST tests/test_workflow_saved.py tests/test_workflow_tool.py`. Expected: PASS.
- [ ] **Step 5: Commit** `feat(workflow): saved workflows run by name`.

### Task 16: Sessions

**Files:**
- Modify: `src/mindroom/workflow.py`, `src/mindroom/workflow_runs/engine.py`, `src/mindroom/workflow_runs/agent_nodes.py`, `src/mindroom/workflow_runs/graph.py`, `docs/tools/workflows.md`
- Test: `tests/test_workflow_sessions.py`

**Interfaces:**
- Produces:
  - SDK `spawn(name, **options) -> Session` (a `session` node whose args are the options) and `Session.ask(message, *, schema=None, timeout=None)` (a `turn` node with args `{"session": <session node key>, "turn": <n from 1>, "message", "schema"}`).
  - Engine kind `turn`: turn 1 runs a fresh child named after the session; later turns continue it through `resolve_follow_up_target` with `previous=<the child of the last turn>`; `session_key` is set on every turn node and the session node's `links["subagent_id"]` names the child.
  - A second `ask` on a session that is in a turn fails with `Session '<name>' is already in a turn. Wait for it or spawn another agent.`
  - Replay: a turn whose digest equals the saved turn hits; the turn after the last saved one continues the saved child; a turn that differs earlier starts a fresh child whose prompt is `Earlier conversation:` plus the saved messages and answers of the turns before it, a blank line, and the new message.
  - Graph: turn nodes carry `session`; the session's row reports `turns`.

- [ ] **Step 1: Write the failing tests**

```python
async def test_second_turn_continues_the_same_child(stack):
    session = await stack.submit_session("@0", name="dev")
    await stack.turn_result("@1", session, 1, "Implement it", script=[says("done")])
    await stack.turn_result("@2", session, 2, "Now fix the test", script=[says("fixed")])
    assert len(stack.child_sessions()) == 1 and stack.child_prompts() == ["Implement it", "Now fix the test"]

async def test_resume_appends_to_the_saved_session(stack): ...   # after resume, turns 1 and 2 hit and turn 3 continues the same subagent_id
async def test_changed_middle_turn_starts_a_fresh_child_with_the_transcript(stack): ...
    # the new child's first prompt starts with "Earlier conversation:" and contains turn 1's message and answer
async def test_two_asks_at_once_on_one_session_are_refused(stack): ...
def test_sdk_turn_numbers_count_per_session(gateway, sdk): ...
```

- [ ] **Step 2: Run** `PYTEST tests/test_workflow_sessions.py`. Expected: FAIL (`spawn` is missing).
- [ ] **Step 3: Implement.**
- [ ] **Step 4: Run** `PYTEST tests/test_workflow_sessions.py tests/test_workflow_resume.py tests/test_delegation_sessions.py`. Expected: PASS.
- [ ] **Step 5: Commit** `feat(workflow): sessions that keep an agent's conversation`.

### Task 17: Peer messages and shared state

**Files:**
- Create: `src/mindroom/custom_tools/workflow_peers.py`
- Modify: `src/mindroom/workflow_runs/store.py`, `engine.py`, `agent_nodes.py`, `graph.py`, `src/mindroom/api/workflow_gateway.py`, `src/mindroom/workflow.py`, `src/mindroom/delegation/state.py`, `src/mindroom/agents.py`, `tach.toml`, `docs/tools/workflows.md`
- Test: `tests/test_workflow_peers.py`

**Interfaces:**
- Produces:
  - Store tables `workflow_messages(run_id, seq, sender, recipient, text, created_at)`, `workflow_cursors(run_id, reader, seq)`, `workflow_state(run_id, key, value_json, updated_by, updated_at)` with `add_message`, `unread(run_id, reader: str, names: Collection[str]) -> list[dict]`, `advance_cursor`, `get_state(run_id, key=None)`, `set_state(run_id, key, value, updated_by)`, `message_edges(run_id) -> list[dict]`.
  - Engine `async send(run_id, *, sender: str, to: str, text: str) -> int` (recipients reached; raises `PeerUnreachableError` with the spec's message when the name has no queued, running, waiting or session node and is not `script` or `*`) and `async read(run_id, *, reader: str, wait_seconds: float) -> list[dict]` (waits on the run's condition, at most 3600 s).
  - `DelegationChild.workflow_node_key: str | None = None`; every child with `workflow_run_id` gets `WorkflowPeerTools` (toolkit `workflow_peers`): `send_peer_message(to, text)`, `read_peer_messages(wait_seconds=0)`, `list_peers()`, `get_shared_state(key=None)`, `set_shared_state(key, value)`, `ask_user(question, options=None)`.
  - Messages queued for a session between turns are put in front of its next message as `Messages received since your last turn:` and one `<sender>: <text>` line each.
  - Gateway routes `POST /messages`, `GET /messages?wait=`, `GET /state`, `PUT /state/{key}`; SDK `send(to, text)`, `inbox(wait_seconds=0)`, `state.get(key, default=None)`, `state.set(key, value)`; the script's reader name is `script`.
  - Limits: text 64 KiB and state value 256 KiB, refused with `Too large (<n> bytes, limit <m>). Write it to a workspace file and send the path.`
  - Graph `edges`: `{"from", "to", "kind": "message", "count"}` from `message_edges`.

- [ ] **Step 1: Write the failing tests**

```python
async def test_message_sent_before_the_recipient_starts_is_delivered(stack):
    await stack.start_agent("@0", name="backend")
    await stack.queue_agent("@1", name="frontend")   # submitted, held by the run limit
    assert await stack.engine.send(stack.run_id, sender="backend", to="frontend", text="contract v1") == 1
    await stack.release("@1")
    assert [m["text"] for m in await stack.engine.read(stack.run_id, reader=stack.node_id("@1"), wait_seconds=0)] == ["contract v1"]

async def test_read_waits_and_wakes_on_the_first_message(stack): ...   # read(wait_seconds=30) returns within 1 s of send
async def test_each_message_is_read_once_per_recipient(stack): ...
async def test_two_agents_sharing_a_name_each_get_a_copy(stack): ...
async def test_message_to_a_finished_agent_is_refused_with_the_hint(stack): ...   # "'critic' has finished and will not read this. Use spawn() to keep an agent you want to talk to."
async def test_session_gets_queued_messages_in_front_of_its_next_turn(stack): ...
async def test_shared_state_is_last_write_wins_and_survives_resume(stack): ...
async def test_script_inbox_receives_messages_to_script(gateway_client, stack): ...
async def test_workflow_children_get_the_peer_tools_and_other_children_do_not(stack): ...
def test_message_edges_count_per_pair(store): ...
async def test_oversized_text_is_refused_with_the_file_hint(stack): ...
```

- [ ] **Step 2: Run** `PYTEST tests/test_workflow_peers.py`. Expected: FAIL, `ModuleNotFoundError: mindroom.custom_tools.workflow_peers`.
- [ ] **Step 3: Implement.** A message addressed to a name whose node is still queued counts as reachable.
- [ ] **Step 4: Run** `PYTEST tests/test_workflow_peers.py tests/test_workflow_sessions.py tests/test_workflow_graph.py`. Expected: PASS.
- [ ] **Step 5: Commit** `feat(workflow): peer messages and shared state between agents`. Then full suite, live runs of spec examples 3 and 6, Sol and Astra review, open PR B6.

---

## Chat pull requests: shared rules

Both Chat pull requests are built in a worktree of `mindroom-chat` from fresh `origin/main`, inside `nix develop`.
`VITEST <files>` means `just test <files>`; before each push run `just typecheck`, `just strict`, `NODE_OPTIONS=--max-old-space-size=6144 just lint`, `npx vite build` and `just bundle-report`.
Names use `workflowRun` in identifiers, because "workflow" already names the thread tabs (`threads/overview/WorkflowTabs.tsx`); labels say "Workflow".
Every string is a `mindroomUi.workflowRun.*` key in `src/app/i18n/locales/additions/en.json` and in the other 16 overlays with the same keys (`localeAdditions.test.ts` checks it).
Layers: `matrix` 3, `navigation` 4, `messages` 7, `subagents` 8, `background-jobs` 10, `room` 11; a file imports lower layers only.
Nothing new enters the entry chunk except the parsers, the store and the two cards; the bundle budget has about 1.8 KB of headroom, so a needed raise is a commit of its own that names the bytes.
Fixture data is invented: agent `panels`, models `Fable 5.2` and `Sol · GPT-6.2`, titles such as `Audit the sample module`.

## PR C1: run card, question card, jobs-bar row

### Task 18: Frame parser, message contents, run store

**Files:**
- Create: `src/app/matrix/protocol/workflowFrame.ts`, `src/app/matrix/protocol/workflowRunContent.ts`, `src/app/messages/live/workflowRunStore.ts`, `src/app/messages/live/bindWorkflowFrames.ts`, `src/app/messages/live/__fixtures__/workflow-frames-v1.json`
- Modify: `src/app/shell/useBindAtoms.ts`, `src/app/matrix/protocol/backgroundJobsContract.ts`, `docs/architecture.md` ("MindRoom event formats": rows for the frame, the two message contents and the two events)
- Move: `src/app/background-jobs/cancelBackgroundJob.ts` and its test to `src/app/matrix/protocol/` (imports updated, no behavior change), so the card (layer 7) and the panel (layer 8) can send a cancel
- Test: `workflowFrame.test.ts`, `workflowRunContent.test.ts`, `workflowRunStore.test.ts`, `bindWorkflowFrames.test.ts`, `backgroundJobsContract.test.ts`

**Interfaces:**
- Consumes: the backend frame and contents of Tasks 10, 11 and 13 (snake case on the wire, camel case in the client); `LiveOrder`, `compareLiveOrder` (`matrix/protocol/liveFrame.ts`), `bindLiveFrames`, `SYNTHETIC_EVENT_ID_PREFIX` (`messages/live/bindLiveReplyFrames.ts`).
- Produces, in `workflowFrame.ts`:
  - `WORKFLOW_FRAME_EVENT_TYPE = 'io.mindroom.workflow'`.
  - `WorkflowRunStatus = 'starting' | 'running' | 'waiting' | 'completed' | 'failed' | 'cancelled' | 'interrupted'`, `WorkflowNodeStatus = 'queued' | 'running' | 'waiting' | 'completed' | 'failed' | 'cancelled'`, `isLiveWorkflowRunStatus(status)` (true for the first three).
  - `WorkflowRunUsage = { inputTokens; outputTokens; toolCalls; costUsd?; projectedCostUsd? }`, `WorkflowRunCounts = { queued; running; waiting; completed; failed; cached }`.
  - `WorkflowRunMeta = { agent; threadId: string | null; title: string | null; status; attempt; startedAt; endedAt: number | null; maxConcurrent; maxCostUsd: number | null; large: boolean; counts; usage }`.
  - `WorkflowRunNode = { id; kind; label; name: string | null; phase: number | null; group: string | null; status; waitingOn: 'approval' | 'user' | 'budget' | null; agent?; model?; run?; subagent?; job?; session: string | null; turns; startedAt: number | null; endedAt: number | null; cached; error: string | null; usage? }`.
  - `WorkflowRunGroup = { id; kind: string; phase: number | null; parent: string | null; total; done; failed }` (kinds today: `foreach`, `parallel`, `run`), `WorkflowRunEdge = { from; to; count }`.
  - `WorkflowRunGraph = { phases: string[]; groups; nodes; edges; log: string[]; moreDone: number }`.
  - `WorkflowRunDelta = { nodes?; groups?; edges?; logAppend? }`, `WorkflowFrame = { run; meta } & LiveOrder & ({ snapshot: WorkflowRunGraph } | { delta: WorkflowRunDelta })`.
  - `parseWorkflowFrame(content: unknown): WorkflowFrame | null`, `parseWorkflowRunGraph(value: unknown): WorkflowRunGraph | null`, `applyWorkflowRunDelta(graph, delta): WorkflowRunGraph` (rows replace by `id`, new rows append, `edges` replaces, the log keeps its last 50 lines).
  - Parsing rule: a frame without a valid `run` (`wf-` and 32 hex), `sid`, `seq` or `meta` is dropped; a malformed node, group or edge row is dropped and the frame is kept; an unknown `kind` or extra field is kept or ignored, never an error.
- Produces, in `workflowRunContent.ts`:
  - `WORKFLOW_RUN_CONTENT_KEY = 'io.mindroom.workflow_run'`, `WORKFLOW_QUESTION_CONTENT_KEY = 'io.mindroom.workflow_question'`, `WORKFLOW_ANSWER_EVENT_TYPE = 'io.mindroom.workflow_answer'`, `WORKFLOW_CONTROL_EVENT_TYPE = 'io.mindroom.workflow_control'`.
  - `WorkflowRunCard = { run; jobId: string | null; requester: string | null; title: string | null; status; counts; usage; phases: string[]; attempt; startedAt: number | null; endedAt: number | null; graph?: WorkflowRunGraph }` and `parseWorkflowRunCard(content): WorkflowRunCard | null`.
  - `WorkflowRunQuestion = { run; node; requester: string | null; text; options: string[]; freeText: boolean; status: 'open' | 'answered' | 'cancelled'; answer: string | null }` and `parseWorkflowRunQuestion(content): WorkflowRunQuestion | null`.
  - `sendWorkflowRunAnswer(mx, room, threadId, cardEventId, question, answer: { option: string } | { text: string }): Promise<void>` and `sendWorkflowRunResume(mx, room, threadId, run): Promise<void>`; both add the thread relation as `cancelBackgroundJob` does and discard a failed local echo.
- Produces, in `workflowRunStore.ts`:
  - `WorkflowRunEntry = { roomId; sender; run; sid; seq; meta: WorkflowRunMeta; graph: WorkflowRunGraph; lastFrameAt: number; stale: boolean }`.
  - `WorkflowRunStore` with `ingest(roomId, sender, frame, receivedAt)`, `showCard(roomId, sender, card, receivedAt)` (adds a finished run's graph from its card when no newer entry exists), `get(roomId, run)`, `subscribe(roomId, callback)`, `clear()`; `getWorkflowRunStore(mx)`, `useRoomWorkflowRuns(roomId)`, `useWorkflowRun(roomId, run)`.
  - Ordering as `nextLiveEntry`: a frame from another sender or not newer by `compareLiveOrder` is ignored; a snapshot replaces the graph; a delta applies only when `sid` is equal and `seq` is the held `seq + 1`, else the entry turns `stale` until the next snapshot.
  - Kept: a live run while its last frame is younger than `WORKFLOW_RUN_FRESHNESS_MS = 30_000`, then it reads `stale`; an ended run for `WORKFLOW_RUN_KEPT_MS = 30 * 60 * 1000`.
- Produces, in `bindWorkflowFrames.ts`: `bindWorkflowFrames(mx, store?)` and `useBindWorkflowFrames(mx)`, a copy of `bindSubagentFrames.ts` with the ID prefix `` `${SYNTHETIC_EVENT_ID_PREFIX}workflow.` ``; `useBindAtoms` calls the hook after `useBindSubagentFrames`.
- `BackgroundJob` gains `workflow?: string`, parsed like the other join keys (malformed drops the key, not the job).

- [ ] **Step 1: Write the fixture and the failing tests.** The fixture holds four frames of one invented run as the backend sends them: a snapshot, a delta that completes one node, a delta that adds a log line, and a snapshot with `more_done`.

```ts
it('parses a snapshot frame into camel case', () => {
  const frame = parseWorkflowFrame(frames[0]);
  expect(frame?.meta).toMatchObject({ title: 'Audit the sample module', status: 'running', maxConcurrent: 16 });
  expect('snapshot' in frame! && frame.snapshot.nodes[0]).toMatchObject({ id: 'n2', waitingOn: null, phase: 0, group: 'n1' });
});
it('keeps the frame and drops a malformed node row', () => {
  const frame = parseWorkflowFrame(withNodes(frames[0], [{ id: 7 }, validNode]));
  expect('snapshot' in frame! && frame.snapshot.nodes).toHaveLength(1);
});
it.each([{}, { ...frames[0], run: 'x' }, { ...frames[0], meta: null }, { ...frames[0], sid: 5 }])('drops %j', (content) =>
  expect(parseWorkflowFrame(content)).toBeNull());

it('applies a delta only right after the frame before it', () => {
  const store = new WorkflowRunStore();
  store.ingest(ROOM, AGENT, parse(frames[0]), 0);          // seq 1
  store.ingest(ROOM, AGENT, parse(frames[2]), 1);          // seq 3: a gap
  expect(store.get(ROOM, RUN)).toMatchObject({ seq: 1, stale: true });
  store.ingest(ROOM, AGENT, parse(frames[3]), 2);          // snapshot
  expect(store.get(ROOM, RUN)).toMatchObject({ stale: false, graph: { moreDone: 12 } });
});
it('follows a new stream after a restart', () => { /* sid "b", seq 1 replaces sid "a", seq 40 */ });
it('ignores frames of another sender for a held run', () => { /* ... */ });
it('shows a finished card graph only when it holds nothing newer', () => { /* showCard after a live terminal frame changes nothing */ });
it('reads the workflow join key and drops a malformed one', () => {
  expect(selectJobs([{ ...job, kind: 'subagent', workflow: RUN }])[0].workflow).toBe(RUN);
  expect(selectJobs([{ ...job, workflow: 5 }])[0]).not.toHaveProperty('workflow');
});
it('reads a question and its answered edit', () => { /* status 'answered', answer 'Yes'; a content without options gives [] and freeText true */ });
it('sends an answer related to the card in its thread', async () => { /* content {run, node, option: 'Yes', 'm.relates_to': {rel_type: 'm.thread', event_id: THREAD, ...}} */ });
```

- [ ] **Step 2: Run** `VITEST src/app/matrix/protocol/workflowFrame.test.ts src/app/matrix/protocol/workflowRunContent.test.ts src/app/messages/live/workflowRunStore.test.ts`. Expected: FAIL, the modules do not exist.
- [ ] **Step 3: Implement.** Check the fixture against a frame captured from PR B4's live test before pushing, and update whichever side is wrong.
- [ ] **Step 4: Run** the same command plus `src/app/messages/live/bindWorkflowFrames.test.ts src/app/matrix/protocol/backgroundJobsContract.test.ts src/app/matrix/protocol/cancelBackgroundJob.test.ts src/test/layering.test.ts`. Expected: PASS.
- [ ] **Step 5: Commit** `feat(workflow-run): frame parser, message contents and run store`.

### Task 19: Run card, question card, jobs-bar row

**Files:**
- Create: `src/app/messages/workflow-run/WorkflowRunCard.tsx`, `WorkflowRunCard.css.ts`, `WorkflowQuestionCard.tsx`, `workflowRunLabels.ts`, `e2e/fixtures/workflow-run.tsx`, `e2e/fixtures/workflow-run.html`, `e2e/workflow-run/workflow-run-card.spec.ts`
- Modify: `src/app/messages/content/renderAgentMessageContent.tsx`, `src/app/background-jobs/backgroundJobsPresentation.ts`, `BackgroundJobRow.tsx`, `BackgroundJobsBar.tsx`, `useJobLookups.ts`, the 17 overlays in `src/app/i18n/locales/additions/`, `e2e/background-jobs/jobs-bar-rich-rows.spec.ts`
- Test: `WorkflowRunCard.test.tsx`, `WorkflowQuestionCard.test.tsx`, `backgroundJobsPresentation.test.ts`, `BackgroundJobsBar.test.tsx`

**Interfaces:**
- Consumes: Task 18; `cancelBackgroundJob` (now in `matrix/protocol/`); the option-button and fold styles of `messages/question/QuestionCard.tsx`; the usage cells and `JobElapsed` of `BackgroundJobRow.tsx`.
- Produces:
  - `workflowRunLabels.ts`: `workflowRunStatusLabel(status, t)`, `workflowRunCountsLine(counts, t)` and `workflowRunCostLine(usage, t)`, shared by the card, the row and the panel.
  - `WorkflowRunCard({ card, roomId, threadId, eventId, sender, readOnly })`: shows the live entry of `useWorkflowRun(roomId, card.run)` when it is newer than the card (its `meta` wins over the card's fields), else the card.
    Content: title (or "Workflow"), status pill, phases as a row of names, the counts line, the cost line, live elapsed time, "Large run" when `meta.large`.
    Stop shows while the status is live, the viewer may cancel jobs, and `card.jobId` is set; it sends `cancelBackgroundJob(mx, room, threadId, { jobId })` and reads "Stopping…" until the status changes or the send fails.
    Resume shows for `failed`, `cancelled` and `interrupted` when the viewer is `card.requester`; it calls `sendWorkflowRunResume`.
    The card has no Graph button yet; Task 21 adds it with the panel.
  - `WorkflowQuestionCard({ question, roomId, threadId, eventId, readOnly })`: one button per option, a text field with Send when `freeText`, all disabled with the line "Only {{name}} can answer." for anyone but `question.requester`; `answered` shows "Answered: {{answer}}" and `cancelled` shows "Cancelled", both without controls.
    A sent answer disables the controls until the edit arrives; a failed send enables them again and shows the room's send-failure text.
  - `renderAgentMessageContent`: from a MindRoom agent only, a content with `io.mindroom.workflow_run` renders `WorkflowRunCard` and one with `io.mindroom.workflow_question` renders `WorkflowQuestionCard`, placed before the secret-request branch; from anyone else the body renders as text.
  - Jobs bar: `BackgroundJobsBarItem` gains `workflowRun?: WorkflowRunEntry`, and `BackgroundJobLookups` gains `workflowRuns: ReadonlyMap<string, WorkflowRunEntry>` filled by `useJobLookups` from `useRoomWorkflowRuns`.
    A job of `kind: 'workflow'` is a row of the new first group `workflow` ("Workflows · {{number}}"): headline the run's title, detail the counts line, the usage cells from `meta.usage`, elapsed time, and Stop.
    A subagent job whose `workflow` names a run that has a listed `workflow` job in the view is left out of `items`, `groups` and the summary; with no such listed job it stays an ordinary sub-agent row.
    The row's status is `waiting` when the run's `meta.status` is `waiting`.
- English strings (`mindroomUi.workflowRun`): `title` "Workflow"; `status.starting` "Starting", `status.running` "Running", `status.waiting` "Waiting for you", `status.completed` "Done", `status.failed` "Failed", `status.cancelled` "Cancelled", `status.interrupted` "Interrupted"; `counts` "{{done}} done, {{running}} running, {{waiting}} waiting"; `reused` "Reused: {{count}}"; `cost` "{{spent}} so far, about {{projected}} in total"; `largeRun` "Large run"; `attempt` "Attempt {{number}}"; `stop` "Stop"; `stopping` "Stopping…"; `resume` "Resume"; `question.placeholder` "Type your answer"; `question.send` "Send"; `question.answered` "Answered: {{answer}}"; `question.cancelled` "Cancelled"; `question.onlyRequester` "Only {{name}} can answer."; and `mindroomUi.backgroundJobs.groupWorkflows` "Workflows · {{number}}".

- [ ] **Step 1: Write the failing tests**

```tsx
it('prefers the live run over the card it was posted with', () => {
  store.ingest(ROOM, AGENT, snapshot({ status: 'waiting', counts: { completed: 12, running: 8, waiting: 1 } }), now);
  render(<WorkflowRunCard card={card({ status: 'running' })} {...props} />);
  expect(screen.getByText('Waiting for you')).toBeTruthy();
  expect(screen.getByText('12 done, 8 running, 1 waiting')).toBeTruthy();
});
it('stops through the job cancel event and reads Stopping', async () => { /* sendEvent called with io.mindroom.background_job_cancel {job_id: JOB} */ });
it('offers Resume to the requester of an interrupted run only', () => { /* viewer != requester: no Resume button */ });
it('shows no Stop to a viewer who may not cancel jobs, and none on a finished run', () => { /* ... */ });

it('lets only the requester answer', () => {
  render(<WorkflowQuestionCard question={question({ requester: '@other:example.org', options: ['Yes', 'No'] })} {...props} />);
  expect(screen.getByRole<HTMLButtonElement>('button', { name: 'Yes' }).disabled).toBe(true);
});
it('sends the chosen option and waits for the edit', async () => { /* io.mindroom.workflow_answer {run, node, option: 'Yes'}; buttons disabled after */ });
it('sends free text', async () => { /* {text: 'after lunch'} */ });
it('shows an answered question without controls', () => { /* "Answered: Yes", no buttons */ });

it('lists one row per workflow run and hides its agents', () => {
  const model = presentBackgroundJobs([workflowJob(RUN), subagentJob({ workflow: RUN }), subagentJob({ workflow: RUN })], no, t, 3, lookups);
  expect(model?.items.map((item) => item.jobId)).toEqual([workflowJob(RUN).jobId]);
  expect(model?.groups[0]).toMatchObject({ kind: 'workflow' });
});
it('keeps a workflow agent listed while its run has no listed job', () => { /* only subagentJob({workflow: RUN}): one sub-agent row */ });
it('does not render a card for workflow content from a person', () => { /* fromMindroomAgent false: plain body */ });
```

- [ ] **Step 2: Run** `VITEST src/app/messages/workflow-run src/app/background-jobs/backgroundJobsPresentation.test.ts`. Expected: FAIL, the components and the `workflow` group do not exist.
- [ ] **Step 3: Implement.** Build the cards on the existing card primitives (the surfaces, pills and buttons `SecretRequestCard` and `QuestionCard` use), so focus order, right-to-left layout and contrast on every palette come with them.
- [ ] **Step 4: Run** the same command plus `src/app/background-jobs src/app/i18n/localeAdditions.test.ts src/app/messages/question`. Expected: PASS.
- [ ] **Step 5: End-to-end and screenshots.** The fixture page renders the run card (running, waiting, done, interrupted), both question cards, and a jobs bar with one workflow row, all from invented data.
  `workflow-run-card.spec.ts` opens it, checks the texts above and that Stop and an option button are reachable by keyboard, and saves the screenshots for the pull request (wide and phone width).
  Extend `jobs-bar-rich-rows.spec.ts`: PUT a `workflow` job with two workflow sub-agent jobs and `io.mindroom.workflow` frames to the test homeserver, and expect one row with the run's title.
- [ ] **Step 6: Commit** `feat(workflow-run): run card, question card and jobs-bar row`. Then the checks of "shared rules", Sol and Astra review, open PR C1 with a `## Screenshots` section.

---

## PR C2: graph panel

### Task 20: Graph layout model (pure)

**Files:**
- Create: `src/app/subagents/workflow/workflowGraphModel.ts`
- Test: `src/app/subagents/workflow/workflowGraphModel.test.ts`

**Interfaces:**
- Consumes: `WorkflowRunGraph`, `WorkflowRunNode`, `WorkflowRunGroup`, `WorkflowRunEdge` (Task 18). No React, no Matrix, no DOM.
- Produces:
  - `WorkflowGraphItem = { type: 'node'; node: WorkflowRunNode } | { type: 'group'; group: WorkflowRunGroup; items: WorkflowGraphItem[] }`.
  - `WorkflowGraphBand = { phase: string | null; items: WorkflowGraphItem[] }` and `WorkflowGraphLayout = { bands: WorkflowGraphBand[]; moreDone: number }`.
  - `layoutWorkflowRunGraph(graph: WorkflowRunGraph): WorkflowGraphLayout`.
    Start order is the number in the ID (`n17` is 17), and every list is in start order.
    A node sits in the box of its `group`, and a group in the box of its `parent`; one whose container is not in the graph sits at the top level of its band, never dropped.
    A top-level item belongs to the band of its `phase`; items with no phase form a first band with `phase: null`; the other bands follow `graph.phases`, and a phase with no item yet keeps its empty band.
    A parent chain that leads back to itself is cut at the repeat, so the function always returns.
  - `foldGroupItems(items, limit = 24): { shown: WorkflowGraphItem[]; more: number }`: up to the limit everything shows in start order; above it waiting, failed, running and queued items come first, then the newest completed ones, and the rest count as `more`.
  - `shownEdges(edges, shownNodeIds: ReadonlySet<string>): WorkflowRunEdge[]`: the edges whose two ends are shown chips; an end such as `script` has no chip, so that edge is left out.
  - `edgePath(from: Box, to: Box): string` with `Box = { x; y; width; height }`: one SVG cubic curve from the bottom centre of `from` to the top centre of `to`, or between the facing sides when `to` does not lie below `from`.
  - `nodeResultPreview(node, run: SubagentRun | undefined): string | undefined`: the run's `meta.result`, else the last non-empty line of `run.docs.md`, cut to 300 characters.

- [ ] **Step 1: Write the failing tests**

```ts
it('puts nodes in their group and groups in their phase band, in start order', () => {
  const layout = layoutWorkflowRunGraph(graph({
    phases: ['Audit', 'Verify'],
    groups: [group('n3', { phase: 0 })],
    nodes: [node('n9', { phase: 1 }), node('n5', { group: 'n3', phase: 0 }), node('n4', { group: 'n3', phase: 0 }), node('n1')],
  }));
  expect(layout.bands.map((band) => band.phase)).toEqual([null, 'Audit', 'Verify']);
  expect(ids(layout.bands[1].items)).toEqual(['n3']);
  expect(ids(childrenOf(layout.bands[1].items[0]))).toEqual(['n4', 'n5']);
});
it('keeps a node whose group the frame left out', () =>
  expect(ids(layoutWorkflowRunGraph(graph({ nodes: [node('n4', { group: 'n3' })] })).bands[0].items)).toEqual(['n4']));
it('returns for groups that name each other as parent', () => { /* n1.parent n2, n2.parent n1: both shown once */ });
it('folds a group of 300 to its unfinished items and the newest done ones', () => {
  const { shown, more } = foldGroupItems(items({ completed: 280, running: 15, failed: 5 }));
  expect(shown).toHaveLength(24);
  expect(shown.slice(0, 20).every((item) => statusOf(item) !== 'completed')).toBe(true);
  expect(more).toBe(276);
});
it('draws only edges between shown chips', () =>
  expect(shownEdges([edge('n1', 'n2'), edge('script', 'n2'), edge('n1', 'n8')], new Set(['n1', 'n2']))).toEqual([edge('n1', 'n2')]));
it('curves from the bottom of one chip to the top of the one below', () =>
  expect(edgePath({ x: 0, y: 0, width: 100, height: 40 }, { x: 200, y: 120, width: 100, height: 40 })).toMatch(/^M 50 40 C .* 250 120$/));
it('previews the result, else the last line the agent wrote', () => { /* ... */ });
```

- [ ] **Step 2: Run** `VITEST src/app/subagents/workflow/workflowGraphModel.test.ts`. Expected: FAIL, the module does not exist.
- [ ] **Step 3: Implement.**
- [ ] **Step 4: Run** the same command. Expected: PASS.
- [ ] **Step 5: Commit** `feat(workflow-run): graph layout model`.

### Task 21: Graph panel

**Files:**
- Create: `src/app/navigation/workflowPanel.ts`, `src/app/subagents/workflow/WorkflowPanel.tsx`, `WorkflowPanelPart.tsx`, `workflowPanel.css.ts`, `e2e/workflow-run/workflow-graph-panel.spec.ts`
- Modify: `src/app/room/Room.tsx`, `src/app/messages/workflow-run/WorkflowRunCard.tsx`, `src/app/background-jobs/BackgroundJobsBar.tsx`, `BackgroundJobRow.tsx`, `e2e/fixtures/workflow-run.tsx`, the 17 overlays
- Test: `src/app/navigation/workflowPanel.test.ts`, `src/app/subagents/workflow/WorkflowPanel.test.tsx`, `WorkflowRunCard.test.tsx`

**Interfaces:**
- Consumes: Tasks 18 to 20; `openSubagentsPanel` (`navigation/subagentsPanel.ts`); `useRoomSubagentRuns` (`messages/live/subagentRunStore.ts`); `useRoomBackgroundJobs` (`matrix/hooks/`); `cancelBackgroundJob`; `lazyPart`, `PartUnavailable`, `ResizablePanel`; the avatar and compact model label `SubagentRunCard` uses.
- Produces:
  - `navigation/workflowPanel.ts`: `workflowRunPanelAtom` (`{ roomId; run; threadId?: string | null } | undefined`, a new value on every opening), `openWorkflowRunPanel(store, roomId, run, threadId?)`, `closeWorkflowRunPanel(store, roomId)`, `useWorkflowRunPanel(roomId): { open: boolean; run?: string; close(): void }`.
  - `WorkflowPanel({ room, run, threadId, onClose })`, lazy through `WorkflowPanelPart` (the unavailable state with Retry and Close, as `SubagentsPanelPart`).
    Header: title, status pill, the counts and cost lines, elapsed time, Stop for a live run (the job is the room's `kind: 'workflow'` job whose `workflow` is this run) and Resume under the card's rule.
    Body: one `section` per band with the phase name as its heading; a group box with its kind ("For each", "Parallel", "Saved workflow"), "{{done}} of {{total}}", a progress bar and "Failed: {{count}}"; chips folded by `foldGroupItems` behind "Show {{count}} more"; then "and {{count}} more done" when `moreDone` is not 0; then the log.
    Chip: a button with a status dot, the label, the agent's name, the model (the compact label of the joined sub-agent run, else the node's `model` name), tokens, cost and elapsed time; "Reused from the last run" for `cached`, "Queued", or the reason it waits.
    Details of the selected chip: label, status, error, `nodeResultPreview`, its message counts as text ("Messages to {{name}}: {{count}}"), Open (`openSubagentsPanel(store, roomId, threadId, node.run)`, when `node.run` is set) and Stop agent (`cancelBackgroundJob` with `node.job`, while the node is live).
    Edges: one `aria-hidden` SVG over the body with a path per `shownEdges` entry; chip boxes are measured in a layout effect and by one `ResizeObserver`, and redrawn at most once per animation frame.
    With no entry for the run it shows "Waiting for the run's next update…"; with a `stale` entry it shows "Not updating. Last update {{time}}." above the last graph.
    Chips are memoized on their node row, which `applyWorkflowRunDelta` keeps for untouched rows, so a delta re-renders only the chips it names.
  - `Room.tsx`: a `PanelEntry` `id: 'workflow'` after `subagents` (`ResizablePanel` with `testId="resizable-workflow-panel"` and its own width key, `covers: mobile`), `store.sub(workflowRunPanelAtom, ...)` that requests the panel and shows the run's conversation as the subagents subscription does, and the panel in the "any panel open" expression.
    There is no header toggle: a room can have several runs, so the panel opens from a card or a jobs-bar row.
  - `WorkflowRunCard`: a Graph button when the store holds the run or the card carries `graph`; it calls `store.showCard(...)` for a card with `graph`, then `openWorkflowRunPanel`.
  - Jobs bar: a workflow row gets the existing `OpenButton`, which calls `openWorkflowRunPanel(store, roomId, run, item.threadId)`.
- English strings added under `mindroomUi.workflowRun`: `graph` "Graph"; `panel.close` "Close"; `panel.resize` "Resize workflow panel"; `panel.hide` "Hide workflow"; `panel.waitingForUpdate` "Waiting for the run's next update…"; `panel.stale` "Not updating. Last update {{time}}."; `panel.log` "Log"; `panel.showMore` "Show {{count}} more"; `panel.moreDone_one` and `panel.moreDone_other` "and {{count}} more done"; `group.foreach` "For each"; `group.parallel` "Parallel"; `group.run` "Saved workflow"; `group.progress` "{{done}} of {{total}}"; `group.failed` "Failed: {{count}}"; `node.reused` "Reused from the last run"; `node.queued` "Queued"; `node.waitingOn.user` "Waiting for your answer"; `node.waitingOn.approval` "Waiting for approval"; `node.waitingOn.budget` "Waiting for the cost decision"; `node.result` "Result"; `node.error` "Error"; `node.messagesTo` "Messages to {{name}}: {{count}}"; `node.open` "Open"; `node.stop` "Stop agent".

- [ ] **Step 1: Write the failing tests**

```tsx
it('opens for a run and closes only in its own room', () => {
  openWorkflowRunPanel(store, ROOM, RUN, THREAD);
  closeWorkflowRunPanel(store, OTHER_ROOM);
  expect(store.get(workflowRunPanelAtom)).toEqual({ roomId: ROOM, run: RUN, threadId: THREAD });
});

it('shows bands, a group with its progress, and chips with their status', () => {
  runs.ingest(ROOM, AGENT, snapshot(fixtureGraph), now);
  subagentRuns.ingest(ROOM, AGENT, subagentSnapshot({ run: 'delegation-1', model: FABLE }), now);   // joins node n4
  render(<WorkflowPanel room={room} run={RUN} threadId={null} onClose={noop} />);
  expect(screen.getByRole('heading', { name: 'Audit' })).toBeTruthy();
  expect(screen.getByText('28 of 40')).toBeTruthy();
  expect(within(chip('Audit auth/session.py')).getByText('Fable 5.2')).toBeTruthy();
});
it('renders 24 chips and Show more for a group of 300', () => { /* 24 chip buttons; "Show 276 more"; "and 700 more done" */ });
it('re-renders only the chip a delta names', () => { /* render counter on two chips; a delta for n4 leaves n5 at one render */ });
it('opens the Sub-agents panel on the chip\'s run', async () => { /* subagentsPanelAtom == {roomId, threadId: null, run: 'delegation-1'} */ });
it('stops one agent by its own job', async () => { /* cancel event {job_id: node.job}, not the run's job */ });
it('says why a chip waits', () => { /* waitingOn 'user': "Waiting for your answer" */ });
it('shows a finished run from its card after the live entry is gone', () => { /* store.showCard(...); the bands render */ });
it('says it waits for an update when it holds nothing', () => { /* "Waiting for the run's next update…" */ });
it('shows Graph on the card only when there is a graph to show', () => { /* no entry and no card.graph: no button */ });
```

- [ ] **Step 2: Run** `VITEST src/app/navigation/workflowPanel.test.ts src/app/subagents/workflow src/app/messages/workflow-run`. Expected: FAIL, the modules do not exist.
- [ ] **Step 3: Implement.** Use no graph or layout library; the layout is CSS (bands stack, a box wraps its chips in a grid) and the only computed geometry is `edgePath`.
- [ ] **Step 4: Run** the same command plus `src/app/background-jobs src/app/i18n/localeAdditions.test.ts src/test/layering.test.ts src/app/threads/__tests__/featureOwnership.architecture.test.ts`, then `npx vite build` and `just bundle-report`. Expected: PASS, and `WorkflowPanel` is its own chunk outside the initial JS.
- [ ] **Step 5: End-to-end and screenshots.** `workflow-graph-panel.spec.ts` posts a run card as the room's agent and PUTs `io.mindroom.workflow` frames to the homeserver's ephemeral endpoint, as `jobs-bar-rich-rows.spec.ts` does for subagent frames (if the endpoint refuses the new type, extend the test homeserver first and say so in the pull request).
  It checks: Graph opens the panel; a delta turns one chip to Done without a reload; a chip's Open shows the Sub-agents panel on that run; at phone width the panel covers the conversation and Close returns; the panel frame passes `checkPanelFrame`.
  Screenshots from the fixture page with invented data: the panel wide, at phone width, with a selected chip, and with message edges.
- [ ] **Step 6: Commit** `feat(workflow-run): live graph panel`. Then the checks of "shared rules", Sol and Astra review, open PR C2 with a `## Screenshots` section.
- [ ] **Step 7: Live test of the whole feature** on a fresh throwaway deployment (backend at B6, Chat at this branch): spec example 2 with a restart in the middle, an approval pause, an `ask_user` answered in Chat, a cancel, and a resume with an edited script; then 200 parallel synthetic agents with the panel open, recording frame sizes and long tasks (target: no long task over 50 ms from a frame). Tear the deployment down and post the numbers on PR C2.
