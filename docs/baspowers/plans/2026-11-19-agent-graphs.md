# Agent Graphs (Workflows as Python Scripts) Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: implement this plan task-by-task with the skill named in **Execution**. Steps use checkbox (`- [ ]`) syntax.

**Execution:** baspowers:subagent-driven-development, because 28 tasks across 15 pull requests share interfaces, later tasks would run on a compacted context, and the engine touches approvals, delegation and restart recovery where a shipped mistake is costly.

**Goal:** An agent starts a Python workflow that runs many agents in the background, survives restarts by replaying finished calls without repeating a write, pauses for people, and shows as a live graph in MindRoom Chat.

**Architecture:** A workflow run is a durable record with a node per SDK call; each attempt is one script run of the existing script runtime, and the stdlib SDK `mindroom.workflow` talks to a `WorkflowEngine` through new routes on the script gateway.
Every node saves a handle to its dispatch before the work starts, so a crash is repaired by reading the handle, never by repeating the work.
Agent turns run as delegation jobs in `ToolJobRuntime` (one job per turn, fenced by a node generation), the run itself is one admitted job of kind `workflow`, and graph frames ride the live-frames transport next to the Sub-agents feed.
MindRoom Chat adds the frame type to its sync filter, a frame store, two message cards, a jobs-bar row and a lazy graph panel.

**Tech Stack:** Python 3.13, FastAPI, SQLite, `jsonschema`, pytest; TypeScript, React, vanilla-extract, vitest, Playwright.

**Spec:** `docs/baspowers/specs/2026-11-19-agent-graphs-design.md` (revised after review rounds 1 and 2).

## Global Constraints

- Toolkit `workflow` with exactly `start_workflow`, `get_workflow`, `cancel_workflow`, `resume_workflow`, `list_workflow_runs`, `save_workflow`; SDK module `mindroom.workflow`; backend package `mindroom.workflow_runs`; run IDs `wf-<32 hex>`.
- The `start_workflow` description, the five one-line descriptions, the SDK signatures and docstrings, and the error messages are copied verbatim from the spec; a pull request adds only the lines for functions it ships, and the two last description lines (`Models:`, `Agents you can run with agent=:`) are built per agent as `custom_tools/delegate.py` builds them.
- A call that was dispatched and has no recorded end is `indeterminate` and is never issued again without `rerun`; no code path may turn it into a retry, and an `indeterminate` node blocks its key whatever the new digest or lane state.
- Run lookups by ID go through `owned_run` (Task 6), which checks agent and requester; nothing else reads a run for a caller.
- Defaults: `max_concurrent` 16, `max_concurrent_runs` 8 (top-level runs), `max_runtime_hours` 72 of working time, `max_tool_calls_per_minute` 600, `large_run_notice_usd` 10 and 25 agents, schema turns 5, automatic resumes without progress 5, node poll 25 s with a 40 s client timeout, `shell` timeout 3600 s and tail 2000.
- Sizes: request 1 MiB, result 8 MiB then spill to a file with a 64 KiB preview, source 128 KiB, label 80 characters, frame error text 200 characters; frames and cards are fitted with the transport's own size check, never with `len(json.dumps(...))`.
- Room state carries IDs only (`workflow`, `job_id`) and only the run's own job; node jobs are not listed there; titles, labels, prompts and results never enter unencrypted state, and peer message texts and shared state never enter a frame, a card or room state.
- The script never runs inside the primary process, and never on the host when a worker backend is configured; `mindroom.workflow` imports only the standard library and `mindroom.script_runs.launch_contract`.
- No change to `dynamic_workflow` code; no new third-party dependency in either repository; no graph library in Chat.
- Backend: `ruff format` only changed files, new internal imports get a `tach.toml` entry (`uv run tach check --dependencies --interfaces` in every task that adds an import; do not export engine internals to make it pass), tests use `@pytest_asyncio.fixture`.
- Docs: one sentence per line, no em dashes, only sentences that answer a user question; `docs/tools/workflows.md` grows in the pull request that ships the behavior.
- Every commit ends with `--trailer "Assisted-by: MindRoom <display name>:<model id> <https://github.com/mindroom-ai/mindroom>"`.
- Fixtures, tests and screenshots use invented placeholder data only.
- `PYTEST <file>` below means `NIX_PATH=nixpkgs=flake:nixpkgs nix-shell --run 'uv run pytest <file> -x -n 0 --no-cov'`; run each new test file once with default addopts before pushing.

## Test levels

Every test file states its level in its module docstring, and a task says which level each test uses.

1. **Pure:** functions with no I/O (`replay.py`, `graph.py`, `structured.py`) and the SDK against `FakeWorkflowGateway`.
2. **Engine harness:** `WorkflowStack` in `tests/workflow_helpers.py` (built in Task 5, extended in Tasks 6, 8 and 10).
   It holds the real `WorkflowRunStore`, the real `ScriptRunStore` and `ScriptToolBroker`, the real `ToolJobRuntime` on its real store, real delegation with the scripted model of `tests/scripted_agent_helpers.py`, a recording fake Matrix client, and a fake script manager: the test plays the script by calling the engine's gateway methods.
   Methods: `start(source="", args=None, **options) -> WorkflowRun`, `attempt` (the live `ScriptRunRecord`), `submit(key, kind, args, meta=None) -> NodeReceipt`, `result(key, kind, args, **wait) -> object` (submit, wait, return or raise), `end_attempt(state, reason=None)`, `restart() -> WorkflowStack` (closes the runtimes and reopens them on the same databases, as a primary restart does), `until(predicate)`, `approve_open_card()`, `deny_open_card()`, `sent_events(type)`, `child_turns()`, `jobs`, `store`, `engine`, `requester`, `recover()` (runs Task 8's startup steps), `finish_attempt(result=None, error=None)`, `deliveries(run_id)` (the reports the owner's conversation received), a `counting_tool` whose effect is one appended line, and the fault hooks `crash_engine_before_settle()`, `crash_broker_after_effect()` and `crash_engine_after_send()`, which make the next such step raise and leave the databases as a killed process would.
   A task that uses a helper not listed here adds it to `WorkflowStack` in that task.
   Faults that only show on the real job runtime (generations, claims, continuation, admission) are tested here, never with a fake child runner.
3. **Local integration:** `LocalWorkflowStack` in `tests/test_workflow_local_integration.py`: the real script runtime in local mode with a real child process (the construction of `tests/test_script_recovery_integration.py:170-230`, with `allow_shell_without_worker` on) and a counting tool.
4. **Live:** a fresh throwaway deployment made by `~/repos/dev/scripts/live.py` (the `just live up`, `just live ask` and `just live down` recipes of `~/repos/dev`, pointed at the worktree; this is not the journal gate in MindRoom's own `justfile`), never lab or prod, with the fault scripts of `tests/workflow_live/` (Task 8), torn down afterwards.

## Review Focus

1. A write that finished before a crash is not repeated, and a write whose end is unknown is not repeated either: stop the primary between a counting tool's effect and the node's settlement, in both receipt states, locally and on a worker (Tasks 5, 7, 8).
2. A node that runs again gets a new job and never the old job's result; a late result of a replaced generation changes nothing (Tasks 7, 10).
3. A refused admission of `start_workflow` starts no process, and a redelivered call starts one (Task 6).
4. Controls from the wrong person or place: another requester's run ID, an answer from another user, the right user in the wrong room, an answer to a card of an old generation, a target agent that is addressable but not in the room (Tasks 6, 9, 16, 18).
5. A `foreach` over 1,000 items: at most 256 script threads, at most `max_concurrent` running nodes, every frame passes the transport check with 1,000 unfinished nodes and long Unicode labels, and the counts stay true (Tasks 12, 13, 15, 16, 28).
6. A restart with an open approval or an open question: the earlier writes of that agent are not made again, and the question is not posted twice (Tasks 11, 11b, 18, 24).
7. Nothing a room member can read carries more than the owner chose to show: no peer message text or shared-state value in a frame, card or state event, and no run found through a provider call ID alone (Tasks 6, 14, 15, 23).

## Pull requests and branches

| PR | Branch (from fresh `origin/main`) | Tasks | Net source estimate |
|---|---|---|---|
| B1 | `feat/workflow-run-store` | 1, 2 | +680 |
| B2a | `feat/workflow-attempts` | 3, 4 | +410 |
| B2b | `feat/workflow-engine-core` | 5 | +430 |
| B3a | `feat/workflow-tool` | 6 | +410 |
| B3b | `feat/workflow-resume` | 7, 8 | +460 |
| B4 | `feat/workflow-agents` | 9 to 11 | +660 |
| B4b | `feat/workflow-approval-restore` | 11b | +250 |
| B5 | `feat/workflow-fanout` | 12 to 14 | +580 |
| B6 | `feat/workflow-graph-frames` | 15, 16 | +530 |
| B7 | `feat/workflow-pauses` | 17 to 19 | +560 |
| B8 | `feat/workflow-saved` | 20 | +250 |
| B9a | `feat/workflow-sessions` | 22 | +250 |
| B9b | `feat/workflow-peers` | 23, 24 | +500 |
| C1 | mindroom-chat `feat/workflow-run-card` | 25, 26 | +750 |
| C2 | mindroom-chat `feat/workflow-graph-panel` | 27, 28 | +700 |

Backend about +5,970, Chat about +1,450.
Task 21 was cut in review round 2 (see the spec's "v1 and later"); the other task numbers are unchanged.
Each pull request is reviewed by Sol and Astra before it opens, opens as Bas, and is linked to the thread at once.
Order: B1, B2a, B2b, B3a, B3b, B4, B4b, B5, B6 and B7 in sequence; B8 needs B6; B9a needs B7 and B9b needs B9a; C1 is built against the pinned contract of Tasks 15, 16 and 18 after B6 and merges after B7 and a check against captured frames; C2 needs C1 and B9b.
No pull request is planned above 700 lines; one that passes its estimate by more than 40% stops for the `stopping-runaway-prs` check.
B1, B2a, B2b and B3a ship no complete user-visible flow, so they merge without a live gate; from B3b on, each live gate uses only what has shipped (stated in the task that opens the pull request).
**Checkpoints.** After B3b and again after B5, stop and report to Bas the real net source size of every merged pull request against its estimate and the outcome of each crash test listed in the spec's "Known limits" that has run, before starting the next pull request.

## File structure

Backend, new:

| File | Responsibility |
|---|---|
| `src/mindroom/workflow.py` | The SDK that runs inside the script process: lanes and keys, gateway client, public functions, `_run_workflow`. |
| `src/mindroom/workflow_runs/models.py` | `WorkflowRun`, `WorkflowNode`, states, `DispatchHandle`, `NodeRequest`, `NodeReceipt`, ID validation. |
| `src/mindroom/workflow_runs/store.py` | `WorkflowRunStore`: SQLite tables, schema version and migrations, every read and write. |
| `src/mindroom/workflow_runs/replay.py` | Pure replay rules: digest, decision, dirty lanes, stale nodes. |
| `src/mindroom/workflow_runs/engine.py` | `WorkflowEngine`: reserve, attempts, node dispatch and settlement, concurrency, waiting, finish, cancel, resume. |
| `src/mindroom/workflow_runs/recovery.py` | Startup: retire inherited attempts, reconcile handles, start next attempts. |
| `src/mindroom/workflow_runs/faults.py` | `hold(name)`: a no-op unless `MINDROOM_WORKFLOW_FAULT_DIR` is set, for the barriers of the live crash tests. |
| `src/mindroom/workflow_runs/agent_nodes.py` | One agent turn as a delegation job; launch path. |
| `src/mindroom/workflow_runs/structured.py` | Schema check, prompt suffix, JSON extraction, validation. |
| `src/mindroom/workflow_runs/questions.py` | `ask` nodes: card, answer, rejection, `!answer`. |
| `src/mindroom/workflow_runs/peers.py` | Messages, cursors, shared state, exactly-once operations, waits. |
| `src/mindroom/workflow_runs/saved.py` | Saved workflow files: parse, list, confined save. |
| `src/mindroom/workflow_runs/graph.py` | Pure graph snapshot, delta, and fitting with a `fits` callback. |
| `src/mindroom/live_frames/workflow.py` | `io.mindroom.workflow` frame publisher. |
| `src/mindroom/api/workflow_gateway.py` | Routes under `/api/script-gateway/workflow/{script_run_id}/`, mounted on both listeners. |
| `src/mindroom/custom_tools/workflow.py`, `src/mindroom/tools/workflow.py` | The `workflow` toolkit and its registration. |
| `src/mindroom/custom_tools/workflow_peers.py` | The run-scoped toolkit every workflow agent gets. |
| `src/mindroom/orchestration/workflow_runtime.py` | Builds the engine, wires it to the script runtime, job runtime and API, runs startup recovery. |

Backend, modified: `script_runs/models.py`, `store.py`, `legacy_schema.py`, `manager.py`, `policy.py`, `broker.py`, `recovery.py`, `shim.py`, `wire.py`, `worker_client.py`, `supervised.py` (workflow attempts), `tools/shell.py` (exit status), `orchestration/script_runtime.py`, `tool_jobs/execution.py`, `runtime.py`, `publication.py`, `authorization.py`, `client_cancel.py`, `orchestration/tool_job_runtime.py` (job kind, admission, recovery), `delegation/lifecycle.py`, `targets.py`, `state.py`, `continuation.py`, `execution.py`, `recovery.py`, `job_approvals.py` (grant, fields, prompt lookup, restore), `matrix/journal_ingress.py`, `matrix/event_types.py`, `commands/parsing.py` (events and commands), `usage/storage.py`, `agents.py`, `tools/__init__.py`, `tools_metadata.json`, `pyproject.toml`, `vulture_whitelist.py`, `orchestrator.py`, `api/main.py`, `api/script_gateway.py`, `tach.toml`, `zensical.toml`, `docs/tools/workflows.md`, `docs/tools/agent-orchestration.md`, `docs/streaming.md`, `docs/architecture/security-posture.md`.

Chat, new: `src/app/matrix/protocol/workflowFrame.ts`, `workflowRunContent.ts`, `cancelBackgroundJob.ts` (moved from `background-jobs/`), `src/app/messages/live/workflowRunStore.ts`, `bindWorkflowFrames.ts`, `src/app/messages/workflow-run/WorkflowRunCard.tsx`, `WorkflowQuestionCard.tsx`, `src/app/navigation/workflowPanel.ts`, `src/app/subagents/workflow/workflowGraphModel.ts`, `WorkflowPanel.tsx`, `WorkflowPanelPart.tsx`, `workflowPanel.css.ts`.
Chat, modified: `matrix/client/clientSyncPolicy.ts`, `messages/content/renderAgentMessageContent.tsx`, `background-jobs/backgroundJobsPresentation.ts`, `BackgroundJobRow.tsx`, `matrix/protocol/backgroundJobsContract.ts`, `shell/useBindAtoms.ts`, `room/Room.tsx`, `docs/architecture.md`, the 17 locale overlays, `e2e/fixtures/`, `e2e/background-jobs/`, `e2e/workflow-run/`.

---

## PR B1: run store and replay rules

### Task 1: Run store with migrations

**Files:**
- Create: `src/mindroom/workflow_runs/__init__.py`, `src/mindroom/workflow_runs/models.py`, `src/mindroom/workflow_runs/store.py`
- Modify: `tach.toml`
- Test (pure): `tests/test_workflow_run_store.py`

**Interfaces:**
- Produces:
  - `WorkflowRunState(StrEnum)`: `STARTING, RUNNING, WAITING, COMPLETED, FAILED, CANCELLED, INTERRUPTED`; `TERMINAL_RUN_STATES`.
  - `WorkflowNodeState(StrEnum)`: `QUEUED, RUNNING, WAITING, COMPLETED, FAILED, CANCELLED, INDETERMINATE`.
  - `DispatchHandle(kind: Literal["broker", "job", "run"], ref: str, dispatched: bool)`: the broker call (`"<script run id>:<call id>"`), the job ID, or a child run ID; `dispatched` turns true in the same transaction that hands the work over.
  - `WorkflowRun` (frozen dataclass): `run_id, agent_name, owner_user_id, room_id, thread_root_event_id, execution_identity: dict, persona: dict | None, title: str | None, source: str, source_digest: str, args: object, max_concurrent: int, max_cost_usd: float | None, resource_profile: str | None, runtime_seconds_left: float | None (`None` is no limit), origin_job_id: str | None (the job ID, or the inline key of Task 6), parent_run_id: str | None = None, parent_node_key: str | None = None, state = STARTING, attempt: int = 0, idle_resumes: int = 0, previous_source_digest: str | None = None, script_run_id: str | None = None, job_id: str | None = None, card_event_id: str | None = None, result: object | None = None, error: str | None = None, created_at: str, finished_at: str | None = None`.
  - `WorkflowNode` (frozen dataclass): `run_id, key, seq: int, kind: str, lane: str, idx: int, digest: str, request: dict, generation: int, state, waiting_on: str | None, handle: DispatchHandle | None, attempt: int, stale: bool, label: str, name: str | None, phase: str | None, group_key: str | None, members: tuple[str, ...] | None, session_key: str | None, cached: bool, started_at: str | None, ended_at: str | None, result: object | None, result_file: str | None, error: dict | None, links: dict[str, str]` (link names: `delegation_id`, `subagent_id`, `job_id`, `event_id`, `child_run_id`).
  - `NodeRequest(key: str, kind: str, args: dict, meta: dict)`, `NodeReceipt(id: str, state: str, cached: bool, result: object | None, error: dict | None, result_ref: str | None = None)`; a node's public ID is `f"n{seq}"`.
  - `new_workflow_run_id() -> str`, `validated_workflow_run_id(run_id) -> str` (pattern `wf-[0-9a-f]{32}`).
  - `WorkflowRunStore(path: Path)`:
    - runs: `reserve_run(run) -> tuple[WorkflowRun, bool]` (one run per `origin_job_id`: a second reserve with the same key returns the first run and `False` when its owner, source digest and args are equal, and raises `WorkflowRunConflictError` otherwise), `delete_intent(run_id) -> bool` (only a `STARTING` run with attempt 0), `get_run(run_id)` (raises `WorkflowRunNotFoundError`), `update_run(run_id, **changes)`, `list_runs(*, agent_name=None, owner_user_id=None, room_id=None, thread_root_event_id=None, top_level_only=False, include_finished=True)`, `run_for_script(script_run_id)`, `child_runs(run_id)`.
    - nodes: `put_node(node)` (assigns the next `seq` to a new key, keeps it for an existing one), `get_node(run_id, key)`, `node_by_seq(run_id, seq)`, `nodes(run_id, *, include_stale=False)`, `settle_node(run_id, key, generation, *, state, result=None, error=None) -> bool` (false and no change when the generation is not the node's current one), `mark_stale(run_id, keys)`.
    - usage: `add_usage(run_id, key, generation, turn, attempt, usage: dict) -> bool` (unique on run, key, generation, turn; a repeat changes nothing and returns false), `usage_totals(run_id, *, attempt=None) -> dict` (`input_tokens, output_tokens, tool_calls, cost_usd, cost_unknown`), `node_usage(run_id) -> dict[str, dict]`.
    - `append_log(run_id, text)`, `log(run_id, limit=50)`, `prune(finished_before: str) -> int` (runs, nodes, usage, log, spilled result files, and the peer tables once Task 23 adds them).
  - `SCHEMA_VERSION = 1` in a `workflow_schema` table and `_MIGRATIONS: tuple[Callable[[sqlite3.Connection], None], ...]`, applied in order at open; a database newer than the code refuses to open with `Workflow store is from a newer MindRoom (schema <n>).`
  - A result whose JSON is over 8 MiB is written to `<store dir>/results/<run_id>/<seq>-<generation>.json`; the row keeps `result_file` and the first 64 KiB as `result`.

- [ ] **Step 1: Write the failing tests**

```python
def test_put_node_assigns_stable_sequence(tmp_path):
    store = WorkflowRunStore(tmp_path / "w.sqlite3")
    run, _ = store.reserve_run(_run("wf-" + "a" * 32, origin_job_id="job-1"))
    first, second = store.put_node(_node(key="@0")), store.put_node(_node(key="@1"))
    again = store.put_node(replace(first, state=WorkflowNodeState.COMPLETED, result={"big": "x" * 200_000}))
    assert (first.seq, second.seq, again.seq) == (1, 2, 1)
    assert store.get_node(run.run_id, "@0").request == first.request

def test_reserve_is_idempotent_per_origin_job(tmp_path):
    store = WorkflowRunStore(tmp_path / "w.sqlite3")
    first, created = store.reserve_run(_run("wf-" + "a" * 32, origin_job_id="job-1"))
    second, created_again = store.reserve_run(_run("wf-" + "b" * 32, origin_job_id="job-1"))
    assert (created, created_again, second.run_id) == (True, False, first.run_id)

def test_reserve_hit_with_another_owner_or_other_args_is_refused(tmp_path): ...   # WorkflowRunConflictError; the first run is unchanged

def test_settle_refuses_an_old_generation(tmp_path):
    store, node = _store_with_node(tmp_path, generation=2)
    assert not store.settle_node(node.run_id, node.key, 1, state=WorkflowNodeState.COMPLETED, result="late")
    assert store.get_node(node.run_id, node.key).state is WorkflowNodeState.RUNNING

def test_usage_is_append_only_and_deduplicated(tmp_path):
    store, node = _store_with_node(tmp_path)
    assert store.add_usage(node.run_id, node.key, 1, 1, 1, {"cost_usd": 0.5, "input_tokens": 10})
    assert not store.add_usage(node.run_id, node.key, 1, 1, 1, {"cost_usd": 0.5, "input_tokens": 10})
    store.add_usage(node.run_id, node.key, 2, 1, 2, {"cost_usd": 0.25})          # a rerun in attempt 2
    assert store.usage_totals(node.run_id)["cost_usd"] == 0.75
    assert store.usage_totals(node.run_id, attempt=2)["cost_usd"] == 0.25

def test_unknown_price_marks_totals_unknown(tmp_path): ...   # a usage entry without cost_usd: cost_unknown is True
def test_large_result_spills_to_a_file_and_prune_removes_it(tmp_path): ...   # 9 MiB result: result_file exists, preview is 64 KiB
def test_prune_removes_only_old_finished_runs(tmp_path): ...
def test_newer_schema_refuses_to_open(tmp_path): ...
def test_migrations_run_in_order_from_version_zero(tmp_path, monkeypatch): ...   # a fake second migration runs once and bumps the version
```

- [ ] **Step 2: Run** `PYTEST tests/test_workflow_run_store.py`. Expected: FAIL, `ModuleNotFoundError: mindroom.workflow_runs`.
- [ ] **Step 3: Implement the models and the store.** Follow `script_runs/store.py` for connection handling and durable writes; JSON columns for dicts; tables `workflow_runs` (unique `origin_job_id`), `workflow_nodes` (primary key `run_id, key`, unique `run_id, seq`), `workflow_usage`, `workflow_log`, `workflow_schema`.
- [ ] **Step 4: Run** the same command. Expected: PASS. Run `uv run tach check --dependencies --interfaces`. Expected: no errors.
- [ ] **Step 5: Commit** `feat(workflow): durable run, node and usage store`.

### Task 2: Replay rules (pure)

**Files:**
- Create: `src/mindroom/workflow_runs/replay.py`
- Test (pure): `tests/test_workflow_replay.py`

**Interfaces:**
- Produces:
  - `node_digest(kind: str, args: dict) -> str`: SHA-256 of the canonical JSON of `{"kind", "args"}`. The SDK puts `name` in `args` for `agent`, `session` and `turn` (it is a mailbox address) and never puts `timeout`, `label`, `cached` or a group's `total` there.
  - `split_key(key: str) -> tuple[str, int]`: `("/0:ab12", 3)` for `"/0:ab12@3"`; `parent_group(lane: str) -> str | None`: `"@0"` for lane `"/0:ab12"`, `"/0:ab12@2"` for `"/0:ab12/2:p0"`, `None` for the root.
  - `Decision(action, next_generation: bool)` with `action` one of `"return"` (the same attempt already has this key), `"hit"`, `"attach"`, `"reconcile"`, `"raise_indeterminate"`, `"run"`.
  - `decide(saved: WorkflowNode | None, digest: str, *, attempt: int, dirty: bool, rerun: bool, cached: bool, live: bool = False) -> Decision`, in the spec's order:
    1. `saved.attempt == attempt` and not stale: `return`.
    2. `saved.state` is `QUEUED` or `RUNNING` and not `live`: `reconcile`, whatever the digest, `dirty` or `rerun`. The caller settles the node from its handle and calls `decide` again, with `live=True` when the handle's work is still alive.
    3. `saved.state` is `INDETERMINATE` and not `rerun`: `raise_indeterminate`, whatever the digest or `dirty`.
    4. `rerun`, or no saved node, or another digest, or `dirty`: `run` (next generation when a node is saved).
    5. same digest, by state: `COMPLETED` is `hit` (or `run` when `cached` is false); `WAITING` or a live `RUNNING` is `attach`; `FAILED` or `CANCELLED` is `run`.
  - `DirtyLanes`: `mark(key: str)` records that the node at `key` ran in this attempt (lane dirty after its index, and the same for every enclosing group's key up to the root); `mark_after(group_key: str)` dirties only the parent lane after a group whose ordered member list changed; `is_dirty(key: str) -> bool`.
    A `cached=False` node whose new result equals the saved one does not call `mark`.
  - `stale_keys(nodes: Iterable[WorkflowNode], attempt: int) -> tuple[str, ...]`: keys whose node was not touched by a completed attempt.

- [ ] **Step 1: Write the failing tests**

```python
def test_same_attempt_returns_the_node_even_when_it_failed():
    assert decide(failed_node("@0", "d", attempt=2), "d", attempt=2, dirty=False, rerun=False, cached=True).action == "return"

def test_completed_same_digest_hits_and_live_attaches():
    assert decide(done("@0", "d", attempt=1), "d", attempt=2, dirty=False, rerun=False, cached=True) == Decision("hit", False)
    assert decide(waiting("@0", "d", attempt=1), "d", attempt=2, dirty=False, rerun=False, cached=True).action == "attach"

def test_running_node_of_a_dead_attempt_is_reconciled_not_run():
    assert decide(running("@0", "d", attempt=1), "d", attempt=2, dirty=False, rerun=False, cached=True).action == "reconcile"

def test_indeterminate_raises_until_rerun_names_it():
    node = indeterminate("@0", "d", attempt=1)
    assert decide(node, "d", attempt=2, dirty=False, rerun=False, cached=True).action == "raise_indeterminate"
    assert decide(node, "d", attempt=2, dirty=False, rerun=True, cached=True) == Decision("run", True)

def test_indeterminate_raises_with_a_changed_digest_and_in_a_dirty_lane(): ...   # only rerun=True runs it
def test_dead_running_node_is_reconciled_before_digest_dirty_or_rerun_are_looked_at(): ...
def test_changed_membership_dirties_the_parent_after_the_group_and_no_item_lane(): ...
def test_changed_digest_dirty_lane_and_failed_node_run_as_the_next_generation(): ...
def test_cached_false_runs_a_completed_node(): ...

def test_changed_item_dirties_what_follows_the_join_and_not_its_siblings():
    dirty = DirtyLanes()
    dirty.mark("/0:a@0")                       # item a of the foreach at @0 ran again
    assert dirty.is_dirty("/0:a@1") and dirty.is_dirty("@1")          # later in its lane, and after the join
    assert not dirty.is_dirty("/0:b@0") and not dirty.is_dirty("@0")  # sibling item, and the group itself

def test_nested_join_reaches_the_root(): ...   # mark("/1:x/0:p0@0") dirties "/1:x@1" and "@2", not "@1" or "@0"
def test_digest_covers_name_and_ignores_key_order(): ...
def test_stale_keys_are_those_a_completed_attempt_did_not_touch(): ...
```

- [ ] **Step 2: Run** `PYTEST tests/test_workflow_replay.py`. Expected: FAIL, `ModuleNotFoundError`.
- [ ] **Step 3: Implement** `replay.py` as pure functions.
- [ ] **Step 4: Run** the same command. Expected: PASS.
- [ ] **Step 5: Commit** `feat(workflow): replay rules`. Then the full suite on gce-dev, Sol and Astra review, open PR B1 ("No visible change").

---

## PR B2a: SDK core and attempts

### Task 3: SDK core

**Files:**
- Create: `src/mindroom/workflow.py`, `tests/workflow_helpers.py`
- Modify: `tach.toml`
- Test (pure): `tests/test_workflow_sdk.py`

**Interfaces:**
- Consumes: `launch_contract.GATEWAY_URL_ENV, RUN_ID_ENV, TOKEN_PATH_ENV`; routes of Task 5.
- Produces (this task): `tool`, `shell`, `ShellResult(ok: bool, exit_code: int, output: str)`, `phase`, `log`, `WorkflowError(message, *, kind)`, `AgentError`, `ToolError`, `_run_workflow(source_path: str) -> None`, and the private pieces later tasks extend:
  - `_Lane` (thread-local): `path: str`, `next_index: int`, `phase: str | None`; `_next_key() -> str` returns `f"{lane.path}@{index}"`; with no lane it raises the spec's module-level message when called on the main thread and the raw-thread message otherwise.
  - `_Gateway`: `run_info()`, `submit(key, kind, args, meta) -> dict`, `wait(node_id) -> dict` (repeats `GET nodes/{id}?wait=25` with a 40 s socket timeout until the state is `completed`, `failed`, `cancelled` or `indeterminate`), `log(text)`, `finish(*, result=None, error=None)`; base URL `f"{gateway}/workflow/{script_run_id}"`; retries transport and 5xx errors without limit; exits the process with code 3 on `409`; a `413` raises the spec's size message with the real byte count.
  - `_call(kind: str, args: dict, *, label=None, name=None, cached=True) -> object`: next key, submit, wait, return `result` or raise by `error["kind"]`; a receipt with `result_ref` makes `_Gateway.result(node_id)` read `GET results/{id}` and return the full typed value, never the preview.
  - `_check_options(function: str, given: dict, allowed: frozenset[str])`: an unknown option raises `WorkflowError("agent() has no option 'system_prompt'. Did you mean system?")` using `difflib.get_close_matches` plus the fixed aliases `system_prompt -> system`, `agent_name -> agent`, `task -> prompt`.
  - `tests/workflow_helpers.py`: `FakeWorkflowGateway` (a threaded `http.server` that records requests and answers node requests from a `respond(request) -> receipt` callable) and `sdk_env(monkeypatch, gateway)`.
- `tool(toolkit, function, /, *, cached=True, **arguments)` submits kind `tool` with args `{"toolkit", "function", "arguments"}` and meta `{"cached": cached}`.
- `shell(command, *, timeout=3600, tail=2000, cached=True)` submits `tool("shell", "run_shell_command", args=command, timeout=timeout, tail=tail)` unchanged and reads the receipt: `output` is `result["output"]`, `exit_code` is `result["exit_code"]` (Task 5 makes the receipt carry both), `ok` is `exit_code == 0`. Nothing is appended to the command.
- There is no separate entry file: the validated shim calls `mindroom.workflow._run_workflow(source_path)` (Task 4).
- `_run_workflow`: compile the source with its real filename, execute it in a fresh module named `__workflow__` registered in `sys.modules` (no lane exists yet, so any SDK call fails with the module-level message before a node is submitted), require a callable `main` (the spec's "Define def main(args)" message), enter the root lane, call `main(run_info["args"])`, post `finish(result=...)`; on an exception print the traceback, post `finish(error=<last 4096 bytes of the traceback>)` and exit 1.

- [ ] **Step 1: Write the failing tests**

```python
def test_keys_count_calls_in_the_root_lane(gateway, sdk, in_main): ...
    # two tool calls with phase("Check") between: [("@0", "tool", None), ("@1", "tool", "Check")]

def test_shell_reads_the_exit_status_from_the_receipt_and_sends_the_command_unchanged(gateway, sdk, in_main):
    gateway.respond = lambda request: completed({"output": "2 failed\n", "exit_code": 1})
    result = sdk.shell("pytest -x  # a trailing comment")
    assert (result.ok, result.exit_code, result.output) == (False, 1, "2 failed\n")
    assert gateway.nodes[0]["args"]["arguments"] == {"args": "pytest -x  # a trailing comment", "timeout": 3600, "tail": 2000}

def test_failed_tool_raises_tool_error_with_kind(gateway, sdk, in_main): ...   # kind "indeterminate"

def test_name_main_guard_is_harmless(gateway, run_source):
    run_source('def main(args):\n    return "ok"\n\nif __name__ == "__main__":\n    main()\n')
    assert gateway.finished == {"result": "ok"}

def test_module_level_call_fails_before_any_node(gateway, run_source):
    with pytest.raises(SystemExit):
        run_source('from mindroom.workflow import tool\ntool("t", "f")\ndef main(args): return 1\n')
    assert "Call agent() inside main(args). MindRoom calls main; remove your own call." in gateway.finished["error"]
    assert gateway.nodes == []

def test_self_call_of_main_fails_before_any_node(gateway, run_source): ...   # source ends with main({}): same message, no node
def test_missing_main_reports_what_to_do(gateway, run_source): ...
def test_traceback_keeps_the_users_line_numbers(gateway, run_source): ...   # 'File "wf.py", line 3' in finished["error"]
def test_call_from_a_raw_thread_says_to_use_foreach(gateway, sdk, in_main): ...
def test_unknown_option_suggests_the_right_name(sdk, in_main): ...   # check via _check_options("agent", {"system_prompt": "x"}, ...)
def test_oversized_request_says_to_use_a_file(gateway, sdk, in_main): ...   # gateway answers 413: "Prompt is 1,403,112 bytes, limit 1 MiB."
def test_poll_survives_a_gateway_restart(gateway, sdk, in_main): ...   # two connection errors, then the receipt
def test_superseded_attempt_exits_with_code_3(gateway, sdk, in_main): ...
def test_sdk_imports_only_stdlib_and_launch_contract(): ...   # ast over the file
def test_result_over_the_inline_limit_is_fetched_in_full_and_keeps_its_type(gateway, sdk, in_main): ...   # a 9 MiB list comes back as that list
```

- [ ] **Step 2: Run** `PYTEST tests/test_workflow_sdk.py`. Expected: FAIL, `ModuleNotFoundError: mindroom.workflow`.
- [ ] **Step 3: Implement**; copy `script_sdk`'s 30-line `_request` shape, do not import `MindRoomTools`.
- [ ] **Step 4: Run** the same command and the tach check. Expected: PASS.
- [ ] **Step 5: Commit** `feat(workflow): SDK core`.

### Task 4: Workflow attempts (placement, launch entry, policy, migration)

**Files:**
- Modify: `src/mindroom/script_runs/models.py`, `store.py`, `legacy_schema.py`, `manager.py`, `policy.py`, `broker.py`, `recovery.py`, `shim.py`, `wire.py`, `worker_client.py`, `supervised.py`, `src/mindroom/orchestration/script_runtime.py`, `tach.toml`
- Test (pure and local integration): `tests/test_workflow_launch.py`, `tests/test_script_legacy_schema.py` (extend), `tests/test_script_worker_client.py` and `tests/api/test_sandbox_runner_scripts.py` (extend)

**Interfaces:**
- Produces:
  - `ScriptRunRecord.workflow_run_id: str | None = None`, a nullable column added by `migrate_legacy_script_run_columns` with a `# LEGACY_COMPAT:` entry.
  - `ScriptRunManager.run(..., workflow_run_id: str | None = None, entry: Literal["script", "workflow"] = "script")`. For a workflow attempt:
    - placement is `workflow_placement(config, runtime_paths, agent_name) -> Literal["worker", "local"] | str`: `"worker"` whenever a Docker or Kubernetes worker backend or a sandbox proxy is configured for the agent (a backend that is down then fails the launch with its own error, never a local run; the shared static runner keeps the script runtime's refusal); `"local"` on Linux when none is configured and `defaults.allow_shell_without_worker` is true or the sandbox mode is explicitly local; otherwise the spec's refusal sentence.
    - **Launch entry.** The launched command stays the shim, with `PYTHONHASHSEED=0`. `entry` is part of the trusted launch description: `SandboxScriptRunRequest.entry: Literal["script", "workflow"] = "script"` in `wire.py` with the protocol version raised by one, sent by `worker_client.py` and passed by `supervised.py` to the shim. The shim validates the source digest and the token path exactly as today and only then, for `workflow`, calls `mindroom.workflow._run_workflow(source_path)` instead of `runpy.run_path`; the token removal stays in its `finally`. A worker on the older protocol refuses the request (`extra="forbid"`), and the launch fails with `This worker image is too old to run workflows. Update it.`
    - **Attempt limit.** `max_runtime_seconds` of a workflow attempt is the fixed `WORKFLOW_ATTEMPT_SECONDS = 30 * 24 * 3600`, whatever `max_runtime_hours` is; the manager, the supervisor and the worker enforce only that. The run's working time belongs to the engine (Task 7), and plain scripts keep their limit.
    - `recovery_signature` is stored as `None`, so no new primary adopts the process.
  - Policy: `resolve_script_launch_grants(context, *, workflow: bool = False)`: with `workflow=True` the excluded toolkits are `_WORKFLOW_RESTRICTED_TOOLKITS = frozenset({"compact_context", "dynamic_tools", "slack"})` and `controls_run` functions; the grants are resolved again from the owner's live tools at every call (`live_workflow_grant(context, toolkit, function) -> ScriptToolGrant | None`), so a tool added or removed after launch is honoured.
  - Approval: `ScriptToolBroker` uses the operator's unmodified `config.tool_approval` (default and rules) for a run with `workflow_run_id`; `build_automation_approval_config` is not applied to it. Plain scripts are unchanged.
  - `script_process_authority` and the removal tracking in `orchestration/script_runtime.py` accept an agent that has `script` or `workflow`; removing `workflow` revokes that agent's workflow attempts with reason `WORKFLOW_TOOL_REMOVED`.

- [ ] **Step 1: Write the failing tests**

```python
def test_no_backend_and_shell_allowed_runs_locally(config_without_worker):
    config_without_worker.defaults.allow_shell_without_worker = True
    assert workflow_placement(config_without_worker, paths, "dev") == "local"

def test_no_backend_and_shell_not_allowed_is_refused_with_the_setting_named(config_without_worker):
    assert workflow_placement(config_without_worker, paths, "dev") == (
        "Workflows run code. Route this agent to a worker, or set defaults.allow_shell_without_worker: true to run it on this machine.")

async def test_configured_worker_that_is_down_fails_and_never_runs_locally(manager_with_dead_backend, context):
    with pytest.raises(ScriptRunManagerError):
        await manager_with_dead_backend.run(context, source="x = 1", workflow_run_id=RUN, entry="workflow")
    assert manager_with_dead_backend.local_launches == []

async def test_workflow_attempt_has_no_recovery_signature_and_a_fixed_hash_seed(manager, context): ...
async def test_plain_script_placement_and_approval_are_unchanged(manager, context): ...

async def test_default_require_approval_asks_for_an_unlisted_function(broker, workflow_run):   # operator default require_approval, no rule
    assert (await broker.approval_for(workflow_run, "website", "read_url")).action == "require_approval"
async def test_default_auto_approve_runs_claude_agent_without_asking(broker, workflow_run): ...   # no always-ask list for workflows
async def test_deny_rule_added_during_a_run_is_honoured(broker, workflow_run): ...

def test_grants_include_memory_and_delegate_but_not_compact_context(context): ...
def test_tool_removed_after_launch_is_refused_at_the_call(context): ...   # live_workflow_grant(...) is None
def test_old_script_database_gains_the_column(tmp_path): ...   # a v-before database opens and reads workflow_run_id None
def test_removing_the_workflow_tool_revokes_its_attempts(script_runtime): ...
async def test_workflow_entry_crosses_the_worker_wire_and_the_shim_validates_before_it_dispatches(manager, context): ...
    # the worker request carries entry="workflow"; a wrong digest is refused before any user code; the token file is gone after the run
def test_older_protocol_worker_refuses_a_workflow_launch(): ...   # SandboxScriptRunRequest of the old version rejects `entry`
async def test_static_runner_and_macos_local_runs_are_refused(manager_with_static_runner, context): ...
async def test_attempt_limit_is_thirty_days_whatever_max_runtime_hours_is(manager, context): ...   # also with max_runtime_hours=0
```

- [ ] **Step 2: Run** `PYTEST tests/test_workflow_launch.py`. Expected: FAIL (`workflow_placement` and the argument are missing).
- [ ] **Step 3: Implement.**
- [ ] **Step 4: Run** the same command plus `PYTEST tests/test_script_run_manager.py tests/test_script_tool_broker.py tests/test_script_run_policy.py tests/test_script_recovery_integration.py`. Expected: PASS, the script tests unchanged.
- [ ] **Step 5: Commit** `feat(workflow): workflow attempts on the script runtime`. Then the full suite on gce-dev, Sol and Astra review, open PR B2a ("No visible change").

## PR B2b: engine core

### Task 5: Engine core, gateway routes, tool nodes with dispatch handles

**Files:**
- Create: `src/mindroom/workflow_runs/engine.py`, `src/mindroom/api/workflow_gateway.py`, `src/mindroom/orchestration/workflow_runtime.py`
- Modify: `src/mindroom/script_runs/models.py`, `store.py`, `broker.py`, `src/mindroom/tools/shell.py`, `src/mindroom/orchestrator.py`, `src/mindroom/api/main.py`, `src/mindroom/api/script_gateway.py`, `tests/workflow_helpers.py`, `tach.toml`
- Test (engine harness, local integration): `tests/test_workflow_engine.py`, `tests/test_workflow_gateway.py`, `tests/test_workflow_local_integration.py`

**Interfaces:**
- Consumes: Tasks 1 to 4.
- Produces:
  - `WorkflowLimits(max_concurrent_runs: int = 8, max_runtime_hours: float = 72, max_tool_calls_per_minute: int = 600, max_concurrent_agents: int | None = None)`.
  - `WorkflowEngine(store, scripts, broker, clock)`:
    - `async launch(run_id, context) -> WorkflowRun`: starts the next attempt of a reserved run (Task 6 calls it from the admitted job).
    - `async cancel(run_id, *, reason) -> WorkflowRun`, `async wait(run_id) -> WorkflowRun`, `status(run_id) -> tuple[WorkflowRun, list[WorkflowNode]]`, `async reconcile() -> None` (maps ended attempts to run states; called from the script runtime's maintenance loop).
    - Gateway side, where `attempt` is the authenticated `ScriptRunRecord`: `run_info(attempt)`, `async submit_node(attempt, request: NodeRequest) -> NodeReceipt`, `async node_receipt(attempt, node_id, *, wait: float) -> NodeReceipt`, `append_log(attempt, text)`, `async finish(attempt, *, result, error)`.
    - `SupersededAttemptError`, raised when `attempt.run_id != run.script_run_id`.
    - In this task `submit_node` knows only "new key" and "this attempt already has the key" (it returns that node's receipt); Task 7 adds the rest of `decide`.
  - **Tool node dispatch:** the call ID is `uuid5(NAMESPACE_OID, f"{run_id}:{key}:{generation}").hex`. In one store transaction the node is written `RUNNING` with `handle=DispatchHandle("broker", f"{attempt.run_id}:{call_id}", dispatched=False)`; `ScriptToolBroker.execute_for_workflow(run, *, call_id, grant, arguments) -> ScriptCallRecord` claims the call (the broker's own receipt row marks it dispatched) and awaits its settled receipt; the engine then calls `settle_node` with the receipt's result or error.
  - `ScriptCallRecord.effect_started_at: str | None` (new nullable column, migrated like Task 4's): the broker writes it durably after hooks and approval and immediately before it invokes the tool function, for every script call.
  - `settle_from_handle(node) -> NodeState`: reads the broker receipt the handle names. A completed receipt gives `COMPLETED` with its result; a refusal recorded before execution (grant revoked, denied, invalid arguments) gives `FAILED`; a pending receipt with no `effect_started_at` (claimed, still waiting for hooks or approval) gives `FAILED` with kind `interrupted`, which is safe to run again; a receipt that is pending with `effect_started_at`, orphaned, or "result too large or invalid" gives `INDETERMINATE`. No receipt row gives `FAILED` kind `interrupted` only when the attempt's script run record still exists (its calls were never claimed); when that record was retired or pruned, the node is `INDETERMINATE`.
  - `ScriptCallRecord.metadata: dict | None` (new nullable column, same migration): the broker stores a tool result's structured metadata there. `run_shell_command` returns its exit status as that metadata (`{"exit_code": <int>}`, from `ShellRunResult` and the finished `ProcessRecord.return_code`), and a `tool` node for it has the result `{"output": <text>, "exit_code": <int>}`.
  - Tool node results are saved in the workflow store through `settle_node`, so the broker's 64 KiB receipt cap does not bound them: `execute_for_workflow` hands the full result to the engine before the receipt is trimmed.
  - Routes under `/api/script-gateway/workflow/{script_run_id}`: `GET /run`, `POST /nodes`, `GET /nodes/{node_id}?wait=`, `GET /results/{node_id}` (streams the node's full result from the row or its spilled file), `POST /log`, `POST /finish`; bearer checked with `broker.authenticate`; `404` for an unknown or unauthorized run, `409` for a superseded attempt, `413` with `{"bytes", "limit"}` above 1 MiB (8 MiB for `/finish`). `GET /nodes/{id}` holds the request up to `wait` seconds on the run's condition.
    The router is included by `api/main.py` and, by name, in the gateway-only app of `api/script_gateway.py`.
  - Run state from the attempt: `exited` after `finish(result=...)` is `completed`; `finish(error=...)` or a `failed` exit is `failed` with that error or the last 4 KiB of output; `exited` with no `finish` is `failed` with `The script ended without returning from main(args).`; `cancelled` and `interrupted` map to themselves.
  - `WorkflowRuntimeLifecycle` (in `orchestration/workflow_runtime.py`): `engine`, `async start()`, `async shutdown()`; built in `orchestrator.py` next to `build_script_runtime`.
  - `WorkflowStack` and `LocalWorkflowStack` as described under "Test levels".

- [ ] **Step 1: Write the failing tests**

```python
async def test_tool_node_saves_its_handle_before_dispatch_and_its_result_after(stack):
    seen = []
    stack.counting_tool.before_effect = lambda: seen.append(stack.store.get_node(stack.run_id, "@0"))
    assert await stack.result("@0", "tool", tool_args("counter", "append", line="a")) == "appended"
    assert seen[0].handle.kind == "broker" and seen[0].state is WorkflowNodeState.RUNNING
    assert stack.counting_tool.lines == ["a"]

async def test_redelivered_request_in_the_same_attempt_returns_the_same_node_and_runs_nothing(stack):
    first = await stack.submit("@0", "tool", tool_args("counter", "fail"))
    await stack.engine.node_receipt(stack.attempt, first.id, wait=5)        # failed
    again = await stack.submit("@0", "tool", tool_args("counter", "fail"))
    assert (again.id, again.state, stack.counting_tool.calls) == (first.id, "failed", 1)

async def test_large_tool_result_is_returned_in_full(stack): ...   # a 300 KiB result round-trips; the old 64 KiB cap does not apply
async def test_result_over_eight_mebibytes_returns_a_reference_and_the_results_route_streams_all_of_it(stack): ...   # also after stack.restart()
async def test_shell_node_result_carries_the_real_exit_status(stack): ...   # command "exit 3": {"output": ..., "exit_code": 3}

def test_settle_from_handle_copies_a_completed_receipt(stack): ...        # COMPLETED with the receipt's result
def test_settle_from_handle_pending_receipt_whose_effect_started_is_indeterminate(stack): ...  # INDETERMINATE
def test_settle_from_handle_approval_wait_before_the_effect_is_safe_to_run_again(stack): ...   # pending, no effect_started_at: FAILED, kind "interrupted"
def test_settle_from_handle_with_pruned_call_records_is_indeterminate(stack): ...   # the attempt's script run record is gone
def test_settle_from_handle_unclaimed_call_is_safe_to_run_again(stack): ...   # FAILED, kind "interrupted"

async def test_finish_then_exit_completes_run_and_wakes_waiters(stack): ...
async def test_exit_without_finish_fails_and_says_why(stack): ...
async def test_cancel_revokes_attempt_and_keeps_completed_nodes(stack): ...

def test_node_request_over_one_mebibyte_is_413_with_the_byte_count(client, token): ...   # measured on the whole body
def test_unknown_token_is_404(client): ...
def test_poll_returns_when_the_node_changes_and_after_wait_otherwise(client, token): ...
def test_routes_are_served_by_the_gateway_only_listener(gateway_only_client, token): ...   # GET /run is 200 there

async def test_real_local_script_returns_its_report(local_stack):
    run = await local_stack.start("from mindroom.workflow import log, shell\n\ndef main(args):\n    log('hi')\n"
                                  "    return {'n': args['n'] + 1, 'code': shell('exit 3').exit_code}\n", args={"n": 1})
    done = await local_stack.engine.wait(run.run_id)
    assert (done.state, done.result) == (WorkflowRunState.COMPLETED, {"n": 2, "code": 3})
```

- [ ] **Step 2: Run** `PYTEST tests/test_workflow_engine.py tests/test_workflow_gateway.py tests/test_workflow_local_integration.py`. Expected: FAIL on missing modules.
- [ ] **Step 3: Implement.** The engine keeps one `asyncio.Condition` per run for polls and `wait`; node tasks are owned by the engine and cancelled by `cancel`.
- [ ] **Step 4: Run** the same command, then `PYTEST tests/test_script_tool_broker.py tests/test_shell_tool.py`. Expected: PASS.
- [ ] **Step 5: Commit** `feat(workflow): engine core, gateway routes and tool nodes`. Then the full suite, Sol and Astra review, open PR B2b ("No visible change"; no toolkit is registered yet).

---

## PR B3a: the toolkit

### Task 6: The `workflow` toolkit and the admitted job kind

**Files:**
- Create: `src/mindroom/custom_tools/workflow.py`, `src/mindroom/tools/workflow.py`, `docs/tools/workflows.md`
- Modify: `src/mindroom/tool_jobs/execution.py`, `runtime.py`, `publication.py`, `authorization.py`, `src/mindroom/orchestration/tool_job_runtime.py`, `src/mindroom/workflow_runs/engine.py`, `src/mindroom/agents.py`, `src/mindroom/tools/__init__.py`, `src/mindroom/tools_metadata.json`, `src/mindroom/usage/storage.py`, `pyproject.toml`, `vulture_whitelist.py`, `docs/architecture/security-posture.md`, `zensical.toml`, `tach.toml`, the generated `skills/mindroom-docs/references/`
- Test (engine harness): `tests/test_workflow_tool.py`, `tests/test_tools_metadata.py` (regenerated)

**Interfaces:**
- Consumes: Task 5 engine; `JobSpec`, `BackgroundOutcome`, `ToolJobRuntime`.
- Produces:
  - `WorkflowTools(Toolkit)` with config fields `max_concurrent_runs=8`, `max_runtime_hours=72`, `max_tool_calls_per_minute=600`, `max_concurrent_agents=None`, `large_run_notice_usd=10`, registered as `workflow` (`executes_code=True`, `requires_room_context=True`, primary runtime).
  - In this task: `start_workflow(source=None, path=None, args=None, title=None, max_concurrent=16, resource_profile=None) -> str`, `get_workflow(run_id, nodes="active") -> str`, `cancel_workflow(run_id) -> str`. `path` is read with the confined read script sources use.
  - The description is built in `__init__` from the spec's text plus `Models: ...` and `Agents you can run with agent=: ...` (the lists `custom_tools/delegate.py:143-179` computes; the agents line appears in Task 9).
  - `owned_run(store, run_id, *, agent_name, requester_id) -> WorkflowRun`: raises `WorkflowRunNotFoundError("No workflow run <id> in your runs.")` for an unknown ID and for another owner's ID alike. Every tool function and, later, every peer and control path uses it.
  - **Admission order.** `WorkflowTools.managed_job(function_name, arguments, *, job_id, runtime) -> JobSpec | str | None` validates the arguments and placement (a refusal string starts nothing), writes the intent with `store.reserve_run` (a `STARTING` run with attempt 0, a preassigned run ID, the source, args and options, and `origin_job_id=job_id`; no process), and returns `JobSpec(tool_name="workflow", kind="workflow", adapter={"workflow_job": job_id, "workflow": run_id}, operation=..., cancel=...)`. When `runtime.start` refuses, the caller deletes the intent (`delete_intent`). The operation, which runs only after admission, writes `run.job_id`, calls `engine.launch` when the run has no attempt yet, and then waits for the run. The adapter never changes afterwards, so a redelivered call reattaches, and the zero-wait handle already holds the run ID.
  - **Crash points.** After the intent and before admission: `recover_runs` (Task 8) deletes a `STARTING` attempt-0 run whose job the runtime does not know. After admission and before launch: the job's continuation (Task 8) runs the same operation, which launches. After the script record exists and before the run names it: `retire_inherited_attempts` kills that process and the operation launches a new attempt.
  - `tool_jobs/execution.py`: `_call_job` asks any toolkit that has `managed_job` (a `ManagedJobToolkit` protocol) only after the call passed the ordinary approval gate, never instead of it; the job is started with `reply_id=None` for kind `workflow`; `_default_wait` is `0.0` for `start_workflow`.
  - `tool_jobs/authorization.py` and `orchestration/tool_job_runtime.py`: kind `workflow` has its own grant check (the owner agent still has the `workflow` toolkit and the requester still has access) and its own recovery entry (Task 8); `continues` stays false for it in this task, so until Task 8 a restart ends the job `interrupted`.
  - The handle a detached call returns is `{job_id, run_id, status, title}`; `workflow_job_outcome(run) -> BackgroundOutcome` carries the whole report as the result payload (a string as is, any other value as indented JSON), not the 500-character summary; `failed` carries `run.error`.
  - The job is never set `awaiting_approval` by the engine.
  - **Inline path.** When the call cannot be a job (`delegation_depth > 0`, minimal mode, inline tools), `start_workflow` reserves the run, launches, waits, and returns the report as its result. Its `origin_job_id` is `"inline:" + sha256` of the sorted JSON of `{"owner": asdict(owner), "run_id": <the calling model run>, "tool_call_id"}`, the identity `tool_jobs/execution.py` hashes for job IDs; a bare provider call ID is never a key (a workflow agent's nested run gets its key in Task 14). A reservation hit whose owner, source digest or args differ raises `WorkflowRunConflictError` and returns nothing of the other run. With `background_tool_jobs.enabled: false`, or with `workflow` in that setting's excluded toolkits, a top-level call returns the spec's refusal sentence, naming the setting to change.
  - `tool_jobs/publication.py`: a `workflow` job's state is `{job_id, thread_id, kind: "workflow", tool: "workflow", status, started_at, workflow}` where `workflow` is the run ID from the job's adapter.
  - `get_workflow` returns `{run_id, title, status, attempt, counts, usage, waiting: [...], failed: [{id, label, error}], nodes: [{id, kind, label, status, error}]}` and honours `mindroom_output_path`.
  - `usage/storage.py` gains the usage kind `workflow`.

- [ ] **Step 1: Write the failing tests**

```python
async def test_start_returns_a_handle_and_its_job_delivers_the_whole_report(stack, managed_call):
    handle = json.loads(await managed_call("start_workflow", source=SCRIPT, title="Count things"))
    assert handle.keys() == {"job_id", "run_id", "status", "title"} and handle["run_id"].startswith("wf-")
    await stack.finish_attempt(result="x" * 20_000)
    job = await stack.jobs.wait_terminal(handle["job_id"])
    assert (job.kind, job.status) == ("workflow", "completed") and len(await stack.collect(job)) == 20_000

async def test_refused_admission_starts_no_process_and_reserves_no_run(stack, managed_call):
    stack.refuse_admission()                                    # the runtime's grant check says no
    with pytest.raises(JobAccessError):
        await managed_call("start_workflow", source=SCRIPT)
    assert stack.scripts.launched == [] and stack.store.list_runs() == []        # the intent was deleted

async def test_two_requesters_with_the_same_tool_call_id_get_their_own_runs_and_reports(stack, child_call): ...   # inline path, call ID "call-1" twice
async def test_zero_wait_handle_names_the_run_before_the_first_attempt_exists(stack, managed_call): ...

async def test_require_approval_rule_launches_only_after_the_user_approves(stack, managed_call): ...
    # operator rule: start_workflow requires approval. Before the card is approved scripts.launched == []; after it, one launch

async def test_redelivered_call_reattaches_and_launches_once(stack, managed_call):
    first = json.loads(await managed_call("start_workflow", source=SCRIPT, job_id="job-1"))
    second = json.loads(await managed_call("start_workflow", source=SCRIPT, job_id="job-1"))
    assert first["run_id"] == second["run_id"] and len(stack.scripts.launched) == 1

async def test_a_child_agent_gets_the_report_inline(stack, child_call): ...   # depth 1: the return value is the report, no job exists
async def test_background_jobs_off_is_refused_in_one_sentence(tools_without_jobs): ...
async def test_another_requesters_run_id_reads_as_unknown(stack, other_requester_tools): ...   # get/cancel: "No workflow run ... in your runs."
async def test_another_agents_run_id_reads_as_unknown(stack, other_agent_tools): ...
def test_jobs_state_names_the_run_and_carries_no_title(stack): ...
async def test_start_needs_exactly_one_source(tools): ...   # "Give exactly one of source or path."
async def test_cancelling_the_job_cancels_the_run(stack, managed_call): ...
async def test_failed_run_delivers_the_error_tail(stack, managed_call): ...
async def test_ninth_top_level_run_is_refused_and_nested_runs_do_not_count(stack, managed_call): ...
def test_description_lists_the_configured_models(tools): ...   # "Models: haiku, opus" from the test config
def test_no_function_name_is_shared_with_dynamic_workflow(): ...
```

- [ ] **Step 2: Run** `PYTEST tests/test_workflow_tool.py`. Expected: FAIL, `ModuleNotFoundError: mindroom.custom_tools.workflow`.
- [ ] **Step 3: Implement.** The docstring is the spec's block with only the lines for what has shipped (`tool`, `shell`, `phase`, `log`, the replay paragraph). Write `docs/tools/workflows.md` (enable the tool, options table, where the script runs and which setting allows it, what the script can call, effects belong in `shell()`/`tool()`), add it to `zensical.toml`, add workflow scripts to `security-posture.md`, and regenerate the tools metadata and the docs skill references.
- [ ] **Step 4: Run** `PYTEST tests/test_workflow_tool.py tests/test_job_tools.py tests/test_background_delegation.py tests/test_event_journal_tool_jobs.py tests/test_tools_metadata.py`. Expected: PASS.
- [ ] **Step 5: Commit** `feat(workflow): workflow toolkit and admitted job kind`. Then the full suite, Sol and Astra review, open PR B3a (the tool starts, reports and cancels a run; resume and restart recovery follow in B3b, and the docs page says so).

---

## PR B3b: resume and restart

### Task 7: Replay in the engine, `resume_workflow`, `now` and `random`

**Files:**
- Modify: `src/mindroom/workflow_runs/engine.py`, `src/mindroom/custom_tools/workflow.py`, `src/mindroom/workflow.py`, `docs/tools/workflows.md`
- Test (engine harness): `tests/test_workflow_resume.py`; (pure) `tests/test_workflow_sdk.py`

**Interfaces:**
- Consumes: `decide`, `DirtyLanes`, `stale_keys` (Task 2); `settle_from_handle` (Task 5).
- Produces:
  - `submit_node` applies `decide` with a per-attempt `DirtyLanes` and the run's `rerun` set:
    - `hit`: `NodeReceipt(cached=True)`, the node is stamped with the current attempt, no usage is added.
    - `attach`: the receipt of the live node; its handle keeps driving it.
    - `reconcile`: `settle_from_handle` first, then `decide` again on the settled node, so a copied result is a hit only when the digest matches and the lane is clean, and an `INDETERMINATE` outcome raises before any dirty-lane or digest rule can run the call.
    - a `group` node whose saved `members` differ from the submitted ones calls `DirtyLanes.mark_after(key)`.
    - `raise_indeterminate`: a failed receipt with `error.kind == "indeterminate"` and the message `This call may already have run (attempt <n>). Check its effect; to run it again call resume_workflow(run_id, rerun=["<id>"]).` Nothing is dispatched.
    - `run`: `cancel_node_work(old)` (cancels a live job or broker call, closes an open card), then the node is written with `generation + 1`, dispatched, and `DirtyLanes.mark(key)` is called.
  - `cancel_node_work(node)` is also called for every key in `stale_keys(...)` when an attempt completes, followed by `mark_stale`.
  - `WorkflowEngine.resume(context, run_id, *, source=None, rerun: Sequence[str] = (), automatic=False) -> WorkflowRun`: refuses a run with a live attempt (`Run <id> is still running. Cancel it first.`); stores `previous_source_digest`, the new source when given and `attempt + 1`; a person's resume sets a fresh `runtime_seconds_left`, an automatic one keeps what is left; unknown IDs in `rerun` are refused by name; launches the next attempt.
  - `resume_workflow(run_id, source=None, path=None, rerun=None) -> str`: through `managed_job`, so it registers a new job for the run (`origin_job_id` stays the first job; `run.job_id` becomes the new one) and returns the same handle shape. On a live run it returns the refusal sentence.
  - Working time: the engine is its only owner. `runtime_seconds_left` decreases only while the run is `RUNNING` and is saved at every state change and once a minute; at 0 the engine ends the attempt and the run `interrupted` with `The run used its <n> hours of working time. resume_workflow continues it.`; `max_runtime_hours=0` stores `None` and never ends a run. The attempt's own 30-day process limit (Task 4) is handled in Task 8.
  - SDK `now() -> datetime` and `random() -> float` as `value` nodes (`{"op": "now"}`, `{"op": "random"}`); the gateway creates the value.
  - The notice of the spec's Determinism paragraph, written with `append_log` when a completed node is replaced, `run.source_digest == run.previous_source_digest`, and the lane was not dirty.

- [ ] **Step 1: Write the failing tests**

```python
async def test_resume_reuses_saved_results_and_runs_only_what_failed(stack):
    assert await stack.result("@0", "tool", tool_args("counter", "append", line="a")) == "appended"
    with pytest.raises(NodeFailed):
        await stack.result("@1", "tool", tool_args("counter", "fail_once"))
    await stack.end_attempt(ScriptRunState.FAILED)
    await stack.engine.resume(stack.context, stack.run_id)
    first = await stack.submit("@0", "tool", tool_args("counter", "append", line="a"))
    assert first.cached and stack.counting_tool.lines == ["a"]                    # the write did not happen again
    assert await stack.result("@1", "tool", tool_args("counter", "fail_once")) == "ok"
    assert stack.store.get_node(stack.run_id, "@1").generation == 2

async def test_indeterminate_call_is_not_issued_again_until_rerun_names_it(stack):
    node = await stack.leave_indeterminate("@0", tool_args("counter", "append", line="a"))   # dispatched, no receipt end
    await stack.engine.resume(stack.context, stack.run_id)
    with pytest.raises(NodeFailed) as error:
        await stack.result("@0", "tool", tool_args("counter", "append", line="a"))
    assert error.value.kind == "indeterminate" and f'rerun=["n{node.seq}"]' in str(error.value)
    assert stack.counting_tool.calls == 1
    await stack.end_attempt(ScriptRunState.FAILED)
    await stack.engine.resume(stack.context, stack.run_id, rerun=[f"n{node.seq}"])
    await stack.result("@0", "tool", tool_args("counter", "append", line="a"))
    assert stack.counting_tool.calls == 2

async def test_changed_upstream_check_does_not_resend_an_ambiguous_write(stack): ...
    # attempt 1: @0 shell(cached=False) "v1"; @1 a write left dispatched with its effect started. attempt 2: @0 returns "v2" (lane dirty)
    # @1 raises indeterminate, counting_tool.calls == 1; the same with @1's args edited
async def test_membership_only_change_reruns_the_check_after_the_join(stack): ...   # item b removed, a cached: @1 runs again
async def test_changed_item_reruns_the_check_after_the_join(stack): ...
    # attempt 1: group @0 with items a, b; then @1 shell "test". attempt 2: item a's args change.
    # item b is cached; item a runs; @1 runs again (cached False) although its digest is unchanged
async def test_reordered_and_grown_foreach_reuses_finished_items(stack): ...
async def test_removed_item_is_marked_stale_and_its_live_work_cancelled(stack): ...
async def test_replaced_node_cancels_its_live_job_and_closes_its_card(stack): ...
async def test_cached_false_runs_again_and_an_equal_result_keeps_the_lane_clean(stack): ...
async def test_old_attempt_is_refused_after_resume(stack): ...   # SupersededAttemptError
async def test_now_and_random_replay_their_values(stack): ...
async def test_changed_call_without_a_script_edit_logs_the_notice(stack): ...
async def test_resume_registers_a_new_job_when_the_old_one_ended(stack, managed_call): ...
async def test_working_time_does_not_count_while_waiting(stack, clock): ...   # 80 h WAITING then RUNNING: not interrupted
async def test_working_time_limit_interrupts_and_resume_continues(stack, clock): ...
async def test_automatic_resume_keeps_the_used_time_and_zero_hours_never_interrupts(stack, clock): ...
```

- [ ] **Step 2: Run** `PYTEST tests/test_workflow_resume.py`. Expected: FAIL (`resume` is missing).
- [ ] **Step 3: Implement**; add `resume_workflow`, `now`, `random`, `cached=False` and the replay paragraph to the docstring and the docs page (including "what replay does not cover").
- [ ] **Step 4: Run** `PYTEST tests/test_workflow_resume.py tests/test_workflow_engine.py tests/test_workflow_sdk.py tests/test_workflow_tool.py`. Expected: PASS.
- [ ] **Step 5: Commit** `feat(workflow): resume by replay without repeating a write`.

### Task 8: Restart recovery and the first acceptance test

**Files:**
- Create: `src/mindroom/workflow_runs/recovery.py`, `tests/workflow_live/README.md`, `tests/workflow_live/kill_after_write.py`, `tests/workflow_live/restart_mid_run.py`
- Modify: `src/mindroom/workflow_runs/engine.py`, `src/mindroom/orchestration/workflow_runtime.py`, `src/mindroom/orchestration/tool_job_runtime.py`, `src/mindroom/orchestration/script_runtime.py`, `docs/tools/workflows.md`
- Test (engine harness, local integration): `tests/test_workflow_recovery.py`, `tests/test_workflow_local_integration.py`

**Interfaces:**
- Produces:
  - `AUTO_RESUME_REASONS = frozenset({RUNTIME_RESTARTED, RUNTIME_SHUTDOWN, PLUGIN_TOOLS_CHANGED, WORKER_CONFIGURATION_CHANGED, AGENT_ISOLATION_CHANGED, SUPERVISOR_UNAVAILABLE, MAX_RUNTIME_EXCEEDED})` (from `script_runs/reasons.py`). `MAX_RUNTIME_EXCEEDED` is the attempt's 30-day process limit; it resumes only while the run has working time left or no limit, and `reconcile` applies the same rule outside a restart.
  - `faults.hold(name)` is called at `"before_receipt"` (in `execute_for_workflow`, after the tool's effect and before the receipt commits) and at `"before_settle"` (after the commit, before `settle_node`). With `MINDROOM_WORKFLOW_FAULT_DIR` unset it returns at once; set, it writes `<dir>/<name>.reached` and waits for `<dir>/<name>.release`.
  - `async retire_inherited_attempts(store, scripts) -> None`: before the gateway serves any workflow route, every attempt of an unfinished run that this process did not start is revoked and its process killed through the owning backend; the routes answer `409` for it. Because workflow attempts have no recovery signature (Task 4), the script runtime adopts none.
  - `async recover_runs(engine, context_for: Callable[[WorkflowRun], ToolRuntimeContext | Pending | Revoked]) -> list[str]`, run after the job runtime's own recovery owns its jobs:
    - a `STARTING` run with attempt 0: deleted when the job runtime does not know its job; launched by its job's continuation otherwise.
    - a run whose `finish` was stored: its state is settled and its job ends with the stored report (one delivery).
    - any other unfinished run whose last attempt ended for an `AUTO_RESUME_REASONS` reason: `engine.resume(..., automatic=True)` when `context_for` returns a context; `Pending` (Matrix or the bot not ready) leaves it for the next `reconcile`; `Revoked` ends it `interrupted` with `The requester no longer has access.`
    - `idle_resumes` counts automatic resumes since the last newly completed node; at 5 the run ends `interrupted` with `Stopped resuming after 5 attempts that finished nothing. Fix the script and call resume_workflow.`
  - `orchestration/tool_job_runtime.py`: `continues` is true for kind `workflow`; its continuation returns a `JobSpec` whose operation waits for the run (found by `origin_job_id` or `run.job_id`), or ends the job when the run is terminal.
  - Startup order in `WorkflowRuntimeLifecycle.start`: script runtime recovery, `retire_inherited_attempts`, job runtime recovery, `recover_runs`, then the routes open.
  - `tests/workflow_live/`: `kill_after_write.py --barrier before-receipt|after-receipt` (starts a run through `just live ask` on a deployment with the fault directory set, waits for that barrier's `.reached` file, never for the counter file, kills the backend with `SIGKILL`, starts it without the barrier, and prints the file's line count, the node's state and the number of report deliveries, with no `rerun`) and `restart_mid_run.py` (the same with a graceful restart at a chosen node count); the README gives the exact commands for a local stack and for a stack with a Docker worker.

- [ ] **Step 1: Write the failing tests.** The first two are the acceptance test of the spec.

```python
async def test_crash_after_the_write_and_its_receipt_copies_the_result_and_writes_once(stack):
    stack.crash_engine_before_settle()                       # the broker receipt is saved; settle_node never runs
    await stack.submit("@0", "tool", tool_args("counter", "append", line="a"))
    await stack.until(lambda: stack.counting_tool.lines == ["a"])
    stack = await stack.restart()
    assert await stack.recover() == [stack.run_id]
    receipt = await stack.submit("@0", "tool", tool_args("counter", "append", line="a"))
    assert (receipt.state, receipt.result, stack.counting_tool.lines) == ("completed", "appended", ["a"])
    await stack.finish_attempt(result="report")
    assert stack.deliveries(stack.run_id) == ["report"]      # one report

async def test_crash_after_the_write_without_a_receipt_is_indeterminate_and_writes_once(stack):
    stack.crash_broker_after_effect()                        # the effect happened; the receipt stays pending
    await stack.submit("@0", "tool", tool_args("counter", "append", line="a"))
    await stack.until(lambda: stack.counting_tool.lines == ["a"])
    stack = await stack.restart()
    await stack.recover()
    receipt = await stack.submit("@0", "tool", tool_args("counter", "append", line="a"))
    assert (receipt.state, receipt.error["kind"], stack.counting_tool.lines) == ("failed", "indeterminate", ["a"])

async def test_crash_between_admission_and_launch_launches_once(stack, managed_call): ...   # the job is admitted, no attempt: one launch after restart
async def test_crash_between_launch_and_naming_the_attempt_retires_it_and_launches_once(stack, managed_call): ...
async def test_intent_without_a_job_is_deleted(stack): ...
async def test_inherited_attempt_is_retired_and_gets_409(stack): ...   # the old attempt's token: SupersededAttemptError; its process was killed
async def test_restart_keeps_the_job_running_and_delivers_one_report(stack): ...
async def test_restart_after_finish_was_stored_delivers_it_without_a_new_attempt(stack): ...
async def test_restart_before_matrix_is_ready_waits_and_resumes_later(stack): ...   # Pending, then a context: resumed once
async def test_run_of_a_requester_without_access_ends_interrupted(stack): ...
async def test_cancelled_run_is_not_resumed(stack): ...
async def test_automatic_resume_stops_after_five_idle_attempts(stack): ...

async def test_real_process_killed_mid_run_resumes_and_writes_once(local_stack):
    run = await local_stack.start(APPEND_TWICE_SCRIPT)       # tool append "a", then a slow tool, then append "b"
    await local_stack.until_lines(["a"])
    await local_stack.kill_primary_and_restart()
    done = await local_stack.engine.wait(run.run_id)
    assert done.state is WorkflowRunState.COMPLETED and local_stack.lines() == ["a", "b"]

async def test_attempt_that_hits_its_process_limit_is_resumed_while_working_time_is_left(local_stack):
    # real process, the attempt limit patched to 2 s, a script that sleeps in Python past it on its first attempt only
    ...   # ended MAX_RUNTIME_EXCEEDED, resumed once, "a" cached, the run completes, runtime_seconds_left did not reset
async def test_plain_script_runtime_limit_is_unchanged(local_stack): ...   # a plain script still ends interrupted at its own limit
```

- [ ] **Step 2: Run** `PYTEST tests/test_workflow_recovery.py`. Expected: FAIL, `ModuleNotFoundError: mindroom.workflow_runs.recovery`.
- [ ] **Step 3: Implement.**
- [ ] **Step 4: Run** `PYTEST tests/test_workflow_recovery.py tests/test_workflow_local_integration.py tests/test_script_recovery_integration.py tests/test_script_runtime_lifecycle.py`. Expected: PASS.
- [ ] **Step 5: Commit** `feat(workflow): restart recovery through replay`. Then the full suite on gce-dev, and the live gate with tools only: `tests/workflow_live/kill_after_write.py` on a local stack and on a Docker-worker stack (expected output: `lines=1 node=indeterminate reports=0` for `before-receipt` and `lines=1 node=cached reports=1` for `after-receipt`; on the Docker stack also a 9 MiB tool result that the script reads back whole), Sol and Astra review, open PR B3b. Then the first checkpoint: report sizes and these crash-test results to Bas before B4.

---

## PR B4: agents

### Task 9: The `workflow` delegation grant

**Files:**
- Modify: `src/mindroom/delegation/lifecycle.py`, `src/mindroom/delegation/targets.py`, `src/mindroom/delegation/state.py`, `src/mindroom/custom_tools/workflow.py`
- Test (pure with a fake room): `tests/test_workflow_targets.py`

**Interfaces:**
- Produces: `_DelegationGrant = Literal["delegate", "dynamic_workflow", "workflow"]`; `DelegationChild.workflow_run_id: str | None = None`, `workflow_node_key: str | None = None`, `workflow_generation: int | None = None` (absent in old snapshots, read as `None`); `delegation_grant(child)` returns `"workflow"` when the run ID is set.
- `caller_grants_child` has no requester or room, so for grant `workflow` it answers only "the caller has the `workflow` toolkit".
- The target rule lives in `authorize_delegation` and `_requester_access`, which see the requester and the room. With grant `workflow` a target is allowed when it is the caller (plain or authored), or in the caller's `delegate_to`, or when both hold: the target's bot is joined to the run's room (`memberships`), and `is_sender_allowed_for_agent_reply_in_room(requester, target, ...)`. Depth, private-agent, model and requester checks are unchanged, and the same function runs at admission, dispatch and every continuation.
- `WorkflowTools` appends `Agents you can run with agent=: ...` (delegate_to plus the joined room agents the requester may address) to its description, and the docs page says that enabling `workflow` lets the agent run the room's agents.

- [ ] **Step 1: Write the failing tests**

```python
def test_self_is_allowed_without_delegate_to(matrix_context, config): ...
def test_room_agent_joined_and_addressable_is_allowed(matrix_context, config): ...
def test_agent_the_requester_may_address_globally_but_not_in_this_room_is_refused(matrix_context, config):
    # "stranger" has a global user grant for the requester and is not joined to the room
    assert "Cannot delegate to 'stranger'" in authorize_delegation("dev", "stranger", "t", config=config, runtime_paths=paths,
                                                                    execution_identity=identity, depth=0, grant="workflow")
def test_joined_agent_the_requester_may_not_address_is_refused(matrix_context, config): ...
def test_private_agent_of_another_user_is_refused(matrix_context, config): ...
def test_depth_limit_is_kept(matrix_context, config): ...
def test_delegate_grant_is_unchanged(matrix_context, config): ...
def test_old_child_snapshot_reads_without_the_new_fields(): ...
```

- [ ] **Step 2: Run** `PYTEST tests/test_workflow_targets.py`. Expected: FAIL (grant `workflow` is unknown).
- [ ] **Step 3: Implement.**
- [ ] **Step 4: Run** `PYTEST tests/test_workflow_targets.py tests/test_delegation_targets.py tests/test_delegate_tools.py`. Expected: PASS.
- [ ] **Step 5: Commit** `feat(workflow): delegation grant for workflow agents`.

### Task 10: Agent nodes as delegation jobs

**Files:**
- Create: `src/mindroom/workflow_runs/agent_nodes.py`
- Modify: `src/mindroom/workflow_runs/engine.py`, `src/mindroom/workflow.py`, `src/mindroom/tool_jobs/publication.py`, `src/mindroom/custom_tools/workflow.py`, `tests/workflow_helpers.py`, `docs/tools/workflows.md`
- Test (engine harness, real `ToolJobRuntime` and scripted model): `tests/test_workflow_agent_nodes.py`; (pure) `tests/test_workflow_sdk.py`

**Interfaces:**
- Consumes: `resolve_fresh_target`, `prepare_child_turn`, `delegation_call_job`, `ToolJobRuntime.start/wait/cancel/acknowledge_wait`, Task 9.
- Produces:
  - `AgentCall(prompt, agent, model, system, tools, skills, knowledge, profile, minimal, timeout, schema, name, output_path, dedicated_worker, resource_profile)` and `AgentCall.from_args(args)`; the node's `request` keeps the resolved target (`{agent, persona, tools, model, depth, worker_scope}`) so recovery never re-resolves from current config.
  - `turn_job_id(owner, run_id, key, generation, turn) -> str`: SHA-256 of the sorted JSON of those five.
  - `async launch_child(context, run, node, call, *, turn: int, previous: DelegationChild | None, jobs) -> str` (the job ID): the one launch path. It prepares the child with the caller's real depth (`prepare_child_turn` adds one itself), `task=node.label`, `prompt=call.prompt`, `workflow_run_id`, `workflow_node_key`, `workflow_generation`, standard mode unless `minimal`; in one store transaction writes `handle=DispatchHandle("job", job_id, dispatched=False)`; calls `jobs.start(job_id, kind="delegation", depth=caller_depth, adapter={..., "workflow": run_id, "node": key, "generation": generation}, reattach=False, ...)` exactly once; marks the handle dispatched. The job's depth is the caller's, as for every delegation job: `continued_subagent_job` loads the child with `depth=job.depth` and `load_subagent` requires `child.depth == depth + 1`.
  - `async await_child(run, node, jobs, *, on_waiting) -> AgentResult | AgentNodeError`: waits on the saved job ID with the engine's claim and no approval timeout of its own, reads the child's raw final answer from the owner-checked child run (`read_child_run`; not the display string with its receipt, and not the audit record, which redacts and replaces large values), adds the turn's own usage with `add_usage(run, key, generation, turn, attempt, ...)`, calls `settle_node` (which refuses an old generation), then `jobs.acknowledge_wait(job_id, claim)`.
  - A child that ended `completed` because a config reload stopped it (the notice of `delegation/lifecycle.py:552-562`) is treated as interrupted: the node is `FAILED` kind `interrupted` when the child made no tool call, `INDETERMINATE` when it did.
  - Timeout: after `call.timeout` the engine calls `jobs.cancel(job_id)` and waits for the job to end before failing the node with kind `timeout`.
  - `settle_from_handle` for a `job` handle: an ended job is copied (completed, failed, cancelled, denied); a job still live is attached; a job ID the runtime never admitted is `FAILED` kind `interrupted`.
  - `is_workflow_node_job(job) -> bool` (the adapter carries `workflow` and the kind is not `workflow`), used in three places: `tool_jobs/publication.py` leaves such a job out of the room state list and of `total`; `_follow_up_candidates` in `orchestration/tool_job_runtime.py` skips it, so a node job that ended while the primary was down is never a follow-up turn or a held-jobs notice; the `job` tool's list skips it.
  - `AgentResult(text, delegation_id, subagent_id, job_id)`; `AgentNodeError(message, *, kind)` with kind `failed | cancelled | denied | timeout | refused | indeterminate`.
  - SDK `agent(...)` with the spec's signature, calling `_check_options` and `_call("agent", {...})`; until Task 17 a `schema` raises `WorkflowError("schema is not available yet in this MindRoom version.")`; a `name` equal to a configured agent with no `agent=` makes the gateway log the spec's hint.

- [ ] **Step 1: Write the failing tests**

```python
async def test_agent_node_runs_a_child_job_and_saves_the_raw_answer_and_usage(stack):
    assert await stack.result("@0", "agent", {"prompt": "Say hi"}, script=[says("hi")]) == "hi"      # no receipt text appended
    node = stack.store.get_node(stack.run_id, "@0")
    job = stack.jobs.job(node.links["job_id"])
    assert (job.adapter["workflow"], job.adapter["generation"], job.depth) == (stack.run_id, 1, 0)   # the caller's depth
    assert stack.store.usage_totals(stack.run_id)["input_tokens"] > 0

async def test_failed_node_reruns_on_resume_as_a_new_job(stack):
    with pytest.raises(NodeFailed):
        await stack.result("@0", "agent", {"prompt": "x"}, script=[fails()])
    first_job = stack.store.get_node(stack.run_id, "@0").links["job_id"]
    await stack.end_attempt(ScriptRunState.FAILED)
    await stack.engine.resume(stack.context, stack.run_id)
    assert await stack.result("@0", "agent", {"prompt": "x"}, script=[says("ok")]) == "ok"
    node = stack.store.get_node(stack.run_id, "@0")
    assert node.generation == 2 and node.links["job_id"] != first_job and stack.jobs.job(first_job).status == "failed"

async def test_edited_prompt_at_the_same_key_starts_a_new_job_and_fences_the_old_one(stack):
    old = await stack.submit("@0", "agent", {"prompt": "old"}, script=[hangs()])
    await stack.end_attempt(ScriptRunState.CANCELLED)
    await stack.engine.resume(stack.context, stack.run_id)
    assert await stack.result("@0", "agent", {"prompt": "new"}, script=[says("new answer")]) == "new answer"
    stack.release_hung_child(text="old answer")                                 # a late completion of generation 1
    assert stack.store.get_node(stack.run_id, "@0").result == "new answer" and stack.child_cancelled(old)

async def test_redelivered_request_attaches_and_starts_one_child(stack): ...   # same attempt, same key twice: one job
async def test_finished_node_jobs_are_not_follow_up_candidates(stack): ...   # _follow_up_candidates(stack.jobs.all()) has only the run's job
async def test_node_job_that_ended_while_the_primary_was_down_is_not_a_follow_up_and_not_in_the_job_list(stack): ...
async def test_large_structured_answer_is_saved_whole_before_the_job_is_acknowledged(stack): ...   # 200 KiB JSON answer: the node result equals it
async def test_node_jobs_are_not_listed_in_room_state(stack): ...   # 100 node jobs: published jobs == [the workflow job], total 1
async def test_child_that_needs_approval_waits_and_resumes(stack): ...   # waiting_on "approval", run WAITING, approve: "sent"
async def test_denied_and_expired_cards_fail_the_node_with_kind_denied(stack): ...
async def test_cancelling_one_node_job_fails_only_that_node(stack): ...
async def test_timeout_cancels_the_child_and_waits_for_it_to_end(stack): ...
async def test_child_stopped_by_a_config_reload_is_not_saved_as_a_result(stack): ...
async def test_the_job_snapshot_holds_the_label_not_the_prompt(stack): ...   # a 200 KiB prompt: the saved child task is <= 80 characters
async def test_plain_copy_inherits_delegate_memory_and_workflow(stack): ...
async def test_unknown_model_is_refused_with_the_model_list(stack): ...   # kind "refused"
async def test_output_path_dedicated_worker_and_resource_profile_reach_the_child(stack): ...
def test_sdk_agent_raises_agent_error_with_kind(gateway, sdk, in_main): ...
```

- [ ] **Step 2: Run** `PYTEST tests/test_workflow_agent_nodes.py`. Expected: FAIL, `ModuleNotFoundError: mindroom.workflow_runs.agent_nodes`.
- [ ] **Step 3: Implement**; add the `agent(...)` lines to the docstring and the docs page.
- [ ] **Step 4: Run** `PYTEST tests/test_workflow_agent_nodes.py tests/test_workflow_sdk.py tests/test_background_delegation.py tests/test_delegation_approval_ownership.py tests/test_job_tools.py`. Expected: PASS.
- [ ] **Step 5: Commit** `feat(workflow): agent nodes run as delegation jobs`.

### Task 11: Agents across a restart, and approval waits

**Files:**
- Modify: `src/mindroom/delegation/continuation.py`, `src/mindroom/delegation/job_approvals.py`, `src/mindroom/tool_jobs/runtime.py`, `src/mindroom/orchestration/tool_job_runtime.py`, `src/mindroom/orchestration/script_runtime.py`, `src/mindroom/workflow_runs/engine.py`, `agent_nodes.py`, `recovery.py`, `docs/tools/workflows.md`
- Test (engine harness with `restart()`): `tests/test_workflow_agent_restart.py`

**Interfaces:**
- Produces:
  - **Running jobs.** `continued_subagent_job` copies `workflow`, `node` and `generation` into the continuation's adapter and keeps the job's ID, as the runtime does today; the continuation is the same turn number for the usage ledger plus a `continuation` counter (so its usage entry is `turn=<n>.<c>`, unique).
  - **The original prompt.** `delegation/continuation.py` gains `set_original_prompt_lookup(lookup: Callable[[BackgroundJob], str | None])`, registered by `WorkflowRuntimeLifecycle` (delegation does not import `workflow_runs`). For a job that carries `workflow`, `_continuation_prompt` (today `job.adapter.get(_PROMPT_KEY, child.task)`) and the stopped-calls continuation in `delegation/execution.py` (today `original_prompt=child.task`) take the prompt from the lookup, which reads the node's saved request by run, key and generation; `child.task` stays the 80-character label. The engine does nothing at startup for such a node: the replayed script reaches it, `decide` says `reconcile`, `settle_from_handle` finds the live job, and the call attaches. A job whose continuation was refused (the limit, a lost session) is `FAILED` kind `interrupted` when the child's record shows no tool call, else `INDETERMINATE`.
  - **An approval wait at a restart (this pull request).** The job ends as today, and its node becomes `INDETERMINATE` when the child had made a tool call, `FAILED` kind `interrupted` otherwise. Task 11b replaces this with restore and the follow-up turn.
  - **Expiry and denial.** The card keeps the operator's expiry (`tool_approval.timeout_days`); an expired or denied card resumes the child with that call denied, as today. The engine adds no deadline.
  - **Script `tool()` approvals.** The broker's background-approval path (`orchestration/script_runtime.py:282-319`) calls `engine.node_waiting(run_id, key, on="approval")` before it waits and `engine.node_resumed(...)` after, so the node shows `waiting` and frees its slot. The broker has claimed such a call, and its receipt has no `effect_started_at` (Task 5): after a restart `settle_from_handle` reads it as safe, the replay runs the call as the next generation with a new call ID and a new card, and the old card is closed; a denial raises `ToolError(kind="denied")`.
  - Run state: `WAITING` when no node is `RUNNING` and no `QUEUED` node may start until a person acts.

- [ ] **Step 1: Write the failing tests**

```python
async def test_running_agent_is_continued_not_restarted_and_its_node_attaches(stack):
    await stack.submit("@0", "agent", {"prompt": "long task"}, script=[calls_tool("counter", "append", line="a"), hangs()])
    await stack.until(lambda: stack.counting_tool.lines == ["a"])
    stack = await stack.restart(script_after=[says("done")])
    await stack.recover()
    assert await stack.result("@0", "agent", {"prompt": "long task"}) == "done"
    node = stack.store.get_node(stack.run_id, "@0")
    assert node.generation == 1 and stack.counting_tool.lines == ["a"] and len(stack.child_sessions()) == 1
    assert stack.jobs.job(node.links["job_id"]).adapter["workflow"] == stack.run_id       # the continuation kept the fields

async def test_restart_before_the_first_tool_call_continues_with_the_full_prompt_at_the_same_depth(stack):
    prompt = "Summarize. " + "x" * 5_000                     # far longer than the 80-character label
    await stack.submit("@0", "agent", {"prompt": prompt}, script=[hangs()])
    stack = await stack.restart(script_after=[says("done")])
    await stack.recover()
    assert await stack.result("@0", "agent", {"prompt": prompt}) == "done"
    child = stack.child_sessions()[0]
    assert prompt in stack.last_prompt_sent(child) and child.depth == 1 and stack.store.get_node(stack.run_id, "@0").generation == 1

async def test_restart_during_an_approval_wait_never_repeats_the_agents_earlier_write(stack): ...   # node INDETERMINATE, counter lines == ["a"]

async def test_agent_that_cannot_be_continued_after_a_tool_call_is_indeterminate(stack): ...
async def test_agent_that_cannot_be_continued_before_any_tool_call_runs_again(stack): ...
async def test_continuation_usage_is_added_once(stack): ...   # totals equal the sum of both partial turns
async def test_expired_card_denies_the_call_and_the_child_goes_on(stack, clock): ...
async def test_script_tool_approval_shows_the_node_waiting_and_frees_its_slot(stack): ...
async def test_script_tool_approval_after_restart_asks_again_and_runs_once(stack): ...
async def test_denied_script_tool_raises_denied(stack): ...
```

- [ ] **Step 2: Run** `PYTEST tests/test_workflow_agent_restart.py`. Expected: FAIL (the continuation drops the fields and sends the label).
- [ ] **Step 3: Implement.**
- [ ] **Step 4: Run** the same command plus `PYTEST tests/test_subagent_continuation.py tests/test_background_delegation.py tests/test_delegation_approval_ownership.py tests/test_script_tool_broker.py`. Expected: PASS, with subagents outside workflows unchanged.
- [ ] **Step 5: Commit** `feat(workflow): agents and approvals survive a restart`. Then the full suite, the live gate with sequential text agents (a script of three `agent()` calls where the second writes through the counting tool; `restart_mid_run.py` during the second; expected: one line, one report, three agents in the Sub-agents panel, none of them twice), Sol and Astra review, open PR B4 with screenshots of the three agents in the Sub-agents panel.

---

## PR B4b: an approval wait across a restart

### Task 11b: Restore, follow-up turn, `indeterminate`

**Files:**
- Modify: `src/mindroom/tool_jobs/runtime.py`, `src/mindroom/delegation/recovery.py`, `continuation.py`, `job_approvals.py`, `src/mindroom/orchestration/tool_job_runtime.py`, `src/mindroom/workflow_runs/agent_nodes.py`, `docs/tools/workflows.md`
- Test (engine harness with `restart()` and a `kill()` that drops the runtimes as a killed process leaves them): `tests/test_workflow_approval_restart.py`

**Interfaces:**
- **Step 0, the spike, before any product change.** Write `test_paused_child_run_survives_a_graceful_restart_and_a_kill`: a child pauses on a gated call, the runtimes stop both ways, and `read_child_run` then returns the paused run with its pending calls and the decisions already given.
  Today a stopped job's cleanup runs before `_keeps` is asked and settles the child as interrupted (`ToolJobRuntime._settle_stopped`, `delegation/recovery.py`), and `_awaits_continuation` accepts only `running` jobs, so the graceful case is expected to fail until the paused run is preserved before cleanup. Report the result in the pull request before building further.
- Produces, tried in this order for a job that carries `workflow` and waited for approval:
  1. **Restore.** The paused run is preserved before cleanup; `_keeps` and `_awaits_continuation` keep the job; its continuation loads the paused run with `read_child_run`, shows the cards of the undecided calls again through `request_child_approvals` with their original expiry (the old cards are edited to `Superseded after a restart`), keeps the decisions already given on sibling cards, and resumes through `forward_resume`.
  2. **Follow-up turn,** when the paused run cannot be loaded: the gated call never ran, so the child is continued as a running job is, with the account of its finished calls; it makes the gated call again and a new card appears. The check in `delegation/continuation.py` that refuses a paused turn accepts it for such a job.
  3. **`INDETERMINATE`,** only when the child's record shows a call that started and has no recorded end.
- The job's adapter records `wait: "approval" | "user"`, so a child in its own `ask_user` (Task 24) is not taken for an approval wait.
- Subagents outside workflows are unchanged: every changed branch is behind `is_workflow_node_job`.

- [ ] **Step 1: Write the failing tests**

```python
async def test_paused_child_run_survives_a_graceful_restart_and_a_kill(stack): ...   # Step 0
async def test_crash_on_either_side_of_an_approval_decision_does_not_repeat_a_write(stack, stop, moment): ...
    # the child wrote "a", then asks approval for two calls; stop is graceful or kill; moment is before or after card 1 was approved
    # after recovery card 1 is asked at most once in total, card 2 is open with its first expiry; approve: "sent"; lines == ["a"], sent_mails == 1
async def test_unloadable_paused_run_continues_with_a_follow_up_turn_and_one_new_card(stack): ...   # lines == ["a"]
async def test_child_with_a_started_unrecorded_call_is_indeterminate(stack): ...
async def test_ordinary_subagent_approval_wait_at_a_restart_is_unchanged(stack): ...
```

- [ ] **Step 2: Run** `PYTEST tests/test_workflow_approval_restart.py`. Expected: FAIL.
- [ ] **Step 3: Implement** in the order above. If the paused run cannot be preserved without changing ordinary subagents, ship steps 2 and 3 only and say so in the pull request.
- [ ] **Step 4: Run** the same command plus `PYTEST tests/test_workflow_agent_restart.py tests/test_subagent_continuation.py tests/test_background_delegation.py tests/test_delegation_approval_ownership.py`. Expected: PASS.
- [ ] **Step 5: Commit** `feat(workflow): an approval wait survives a restart`. Then the full suite, the live gate (an agent writes through the counting tool and then needs an approval; a graceful restart and a `SIGKILL` while the card is open; expected: one line, one card left to decide, the approval accepted, one report), Sol and Astra review, open PR B4b.

---

## PR B5: fan-out

### Task 12: `foreach`, `parallel`, `PartialFailure`

**Files:**
- Modify: `src/mindroom/workflow.py`, `src/mindroom/workflow_runs/engine.py`, `src/mindroom/custom_tools/workflow.py`, `docs/tools/workflows.md`
- Test (pure): `tests/test_workflow_sdk.py`; (engine harness) `tests/test_workflow_fanout.py`

**Interfaces:**
- Produces:
  - SDK `foreach(items, fn, *, max_concurrent=None) -> list` and `parallel(fns, *, max_concurrent=None) -> list`.
    Each first submits a `group` node (`{"op": "foreach" | "parallel"}`, with `meta["total"]` and `meta["members"]`, the ordered lane names), which the gateway completes at submit, then runs every function on threads held to `min(len, max_concurrent or 256)` active lanes by a semaphore, each in the child lane `f"{lane}/{i}:{item_key}"` with the parent's phase.
    A lane whose poll returns a receipt in state `waiting` releases the semaphore until the node leaves that state, so the next lane starts.
  - `_item_key(item, seen) -> str`: the first 12 hex characters of the SHA-256 of `json.dumps(item, sort_keys=True)`, with `#k` for the k-th equal item (k from 2); an item that is not JSON gets `p<position>`, and the group's meta records `positional: true` so the graph and docs can say so; `parallel` uses `p<position>`.
  - `PartialFailure(WorkflowError)` with `ok: list`, `results: list`, `errors: dict[int, BaseException]`, `items: list | None`, kind `partial`, and the message `2 of 5 calls failed: [1] AgentError(failed): ...; [3] KeyError: 'x'`.
    Every sibling settles first. Any exception in a function is an error at its position, also a script bug; with no error the plain list returns, and `None` is then a real result.
    A run cancel (`409`, or `WorkflowError(kind="cancelled")` for the whole run) is not collected: it stops the pool and propagates.
  - `parallel` checks its functions before running: when two share `__code__` and the same closure cells, it raises the spec's loop-variable message; results that are not callables raise the "takes functions" message.
  - A Python exception inside a lane with no node of its own is recorded as an `error` node (`_call("error", {"type", "message"})`), so the graph shows where it happened.
  - `get_workflow` and the report delivery: when a completed run has failed or indeterminate nodes, the delivery's first line is the spec's `3 calls failed (...)` sentence.

- [ ] **Step 1: Write the failing tests**

```python
def test_foreach_lanes_are_keyed_by_item_value(gateway, sdk, in_main): ...   # ["b", "a", "b"]: keys /0:<a>@0, /0:<b>@0, /0:<b>#2@0

def test_one_failure_lets_siblings_finish_then_raises_with_partial_results(gateway, sdk, in_main):
    gateway.respond = by_prompt({"a": completed("A"), "b": failed({"kind": "failed", "message": "boom"}), "c": completed(None)})
    with pytest.raises(sdk.PartialFailure) as caught:
        sdk.foreach(["a", "b", "c"], lambda item: sdk.agent(item))
    error = caught.value
    assert (error.ok, error.results, list(error.errors), error.items) == (["A", None], ["A", None, None], [1], ["a", "b", "c"])
    assert len(gateway.nodes_of_kind("agent")) == 3

def test_a_legitimate_none_result_is_not_a_failure(gateway, sdk, in_main):
    gateway.respond = lambda request: completed(None)
    assert sdk.foreach(["a", "b"], lambda item: sdk.agent(item)) == [None, None]

def test_all_failed_raises_and_ok_is_empty(gateway, sdk, in_main): ...
def test_a_script_bug_in_the_callback_is_an_error_at_its_position_with_a_node(gateway, sdk, in_main): ...   # KeyError: errors[0] is KeyError; one "error" node
def test_run_cancel_is_not_collected(gateway, sdk, in_main): ...   # gateway answers 409 mid-fan-out: SystemExit(3), no PartialFailure

def test_parallel_of_lambdas_over_a_loop_variable_is_refused(sdk, in_main):
    prompts = ["a", "b"]
    with pytest.raises(sdk.WorkflowError, match="capture the same loop variable"):
        sdk.parallel([lambda: sdk.agent(p) for p in prompts])

def test_parallel_of_distinct_functions_and_of_default_bound_lambdas_is_fine(gateway, sdk, in_main): ...   # [lambda p=p: ...] passes
def test_parallel_given_results_says_to_pass_functions(sdk, in_main): ...
def test_foreach_of_1000_items_opens_at_most_256_requests(gateway, sdk, in_main): ...
def test_a_waiting_lane_lets_the_next_lane_start_at_max_concurrent_one(gateway, sdk, in_main): ...
def test_non_json_items_are_positional_and_marked(gateway, sdk, in_main): ...

async def test_completed_run_with_caught_failures_says_so_in_the_delivery(stack): ...   # first line "1 call failed (n3); get_workflow(..."
```

- [ ] **Step 2: Run** `PYTEST tests/test_workflow_sdk.py tests/test_workflow_fanout.py`. Expected: FAIL (`foreach` is missing).
- [ ] **Step 3: Implement.** `foreach` feeds items to the pool through an iterator so 1,000 items never create more than 256 threads. Add the fan-out lines and the `PartialFailure` catch pattern to the docstring and the docs page, and the spec's "Agents that edit the same repository" idiom (one git worktree per item, a merge step, `dedicated_worker`) to the docs page.
- [ ] **Step 4: Run** the same command plus `PYTEST tests/test_workflow_resume.py`. Expected: PASS.
- [ ] **Step 5: Commit** `feat(workflow): foreach and parallel with PartialFailure`.

### Task 13: The run limit and the broker semaphore

**Files:**
- Modify: `src/mindroom/workflow_runs/engine.py`, `agent_nodes.py`, `src/mindroom/script_runs/broker.py`
- Test (engine harness): `tests/test_workflow_concurrency.py`

**Interfaces:**
- Produces:
  - A per-run limit of `run.max_concurrent` on nodes in `RUNNING` of kinds `agent`, `turn`, `tool`; others wait in `QUEUED`; a node in `WAITING` (any reason) does not count, and takes a slot again before it goes on; `cancel` starts no queued node. A child run has its own limit.
  - Broker: the per-run lock becomes a per-run `asyncio.Semaphore` of size 1, or of the run's `max_concurrent` when `run.workflow_run_id` is set; `max_tool_calls_per_minute` delays calls above the rate, it does not fail them.

- [ ] **Step 1: Write the failing tests**

```python
async def test_engine_runs_at_most_max_concurrent_nodes(stack): ...   # max_concurrent=2, 6 agents: peak RUNNING == 2
async def test_waiting_node_frees_its_slot_and_takes_one_again(stack): ...
async def test_cancel_starts_no_queued_node(stack): ...
async def test_plain_script_calls_stay_serialized(broker): ...
async def test_workflow_tool_calls_run_in_parallel(stack): ...   # peak == 3 with max_concurrent=3
async def test_tool_rate_limit_delays_and_does_not_fail(stack, clock): ...
```

- [ ] **Step 2: Run** `PYTEST tests/test_workflow_concurrency.py`. Expected: FAIL.
- [ ] **Step 3: Implement.**
- [ ] **Step 4: Run** the same command plus `PYTEST tests/test_script_tool_broker.py`. Expected: PASS.
- [ ] **Step 5: Commit** `feat(workflow): run limit and parallel tool calls`.

### Task 14: Usage totals, nested runs, delegation through `tool()`

**Files:**
- Modify: `src/mindroom/workflow_runs/engine.py`, `agent_nodes.py`, `src/mindroom/custom_tools/workflow.py`, `docs/tools/workflows.md`
- Test (engine harness): `tests/test_workflow_cost.py`, `tests/test_workflow_nested.py`

**Interfaces:**
- Produces:
  - Usage: each entry is one model turn's own usage (the child record's own requests, not its rolled-up total that includes its subagents, `delegation/state.py:163-175`); a subagent a workflow agent starts with `run_subagent` reports its turns as entries under the parent node's key with `turn="sub:<delegation id>"`. `run_usage(run_id) -> {input_tokens, output_tokens, tool_calls, cost_usd, attempt_cost_usd, cost_unknown}` sums the run and its child runs once.
  - Nested run: an inline `start_workflow` inside a workflow agent (Task 6's inline path) reserves a run with `parent_run_id` and `parent_node_key`; the parent node gets a child `group` node with `handle=DispatchHandle("run", child_run_id, ...)`; cancelling the parent run or the agent's job cancels the child run; the child has its own `max_concurrent` and does not count toward `max_concurrent_runs`; its `origin_job_id` is `"nested:" + sha256(parent run, parent node key, generation, source digest, args digest)`, with no call ID in it, so the continued agent's repeated `start_workflow` call after a restart, made under a new call ID, finds the same child run: it waits for a live one and gets the stored report of a finished one.
  - Depth: `MAX_DELEGATION_DEPTH` is checked on the agent's real delegation depth when it starts a child; `run(name)` adds none.
  - Script `tool("delegate", "run_subagent", ...)` and `tool("delegate", "continue_subagent", ...)`: the engine maps the call to an `agent` or `turn` node through `launch_child`, so it has job admission and approval handling; `tool("workflow", "start_workflow", ...)` maps to a nested run. The broker does not execute these, but its steps before execution run first for that toolkit function: `live_workflow_grant`, the hooks, and the operator's approval rule (the node is `waiting` on `approval` while it asks). A refusal or denial fails the node and launches nothing.
  - Workflow agents keep `matrix_message`; a test pins that a message sent by a workflow agent lands in the originating thread.

- [ ] **Step 1: Write the failing tests**

```python
async def test_rerun_does_not_erase_what_the_first_generation_cost(stack):
    with pytest.raises(NodeFailed):
        await stack.result("@0", "agent", {"prompt": "x"}, script=[fails(cost=0.40)])
    await stack.end_attempt(ScriptRunState.FAILED)
    await stack.engine.resume(stack.context, stack.run_id)
    await stack.result("@0", "agent", {"prompt": "x"}, script=[says("ok", cost=0.10)])
    usage = stack.engine.run_usage(stack.run_id)
    assert (usage["cost_usd"], usage["attempt_cost_usd"]) == (pytest.approx(0.50), pytest.approx(0.10))

async def test_cache_hit_adds_nothing(stack): ...
async def test_subagent_of_a_workflow_agent_is_counted_once(stack): ...   # child 0.20 own + its subagent 0.30: total 0.50, not 0.80
async def test_unpriced_model_marks_the_total_unknown(stack): ...

async def test_agent_starts_a_nested_run_inline_and_gets_its_report(stack): ...   # parent max_concurrent=1: no deadlock; child run has parent_run_id
async def test_stop_of_the_parent_cancels_the_nested_run(stack): ...
async def test_nested_run_cost_is_in_the_parent_total_once(stack): ...
async def test_restart_during_a_nested_run_waits_for_the_same_child_run(stack): ...   # the repeated call has a new call ID: one child run, not two
async def test_denied_or_approval_required_script_tool_delegate_launches_nothing_until_approved(stack): ...   # operator default require_approval: zero jobs before the card is approved; a denial: zero jobs, ToolError(kind="denied")
async def test_script_tool_start_workflow_passes_the_same_gate(stack): ...
async def test_depth_limit_refuses_the_fourth_level(stack): ...
async def test_script_tool_delegate_becomes_an_agent_node_with_a_job(stack): ...   # tool("delegate", "run_subagent", task=...): node kind "agent", a job exists
async def test_workflow_agent_message_lands_in_the_originating_thread(stack): ...
```

- [ ] **Step 2: Run** `PYTEST tests/test_workflow_cost.py tests/test_workflow_nested.py`. Expected: FAIL.
- [ ] **Step 3: Implement.**
- [ ] **Step 4: Run** the same command plus `PYTEST tests/test_workflow_agent_nodes.py tests/test_workflow_tool.py`. Expected: PASS.
- [ ] **Step 5: Commit** `feat(workflow): usage ledger totals and nested runs`. Then the full suite, the live gate with a text-only diamond (spec example 1 without `schema`: the plan step returns lines the script splits; 6 research agents; `restart_mid_run.py` at 3 done; expected: the report arrives once, finished agents are not paid twice, `get_workflow` shows the attempt cost below the run cost), Sol and Astra review, open PR B5. Then the second checkpoint: report sizes and crash-test results to Bas before B6.

---

## PR B6: graph frames and the run card

### Task 15: Graph snapshot and fitting (pure)

**Files:**
- Create: `src/mindroom/workflow_runs/graph.py`
- Test (pure): `tests/test_workflow_graph.py`

**Interfaces:**
- Produces:
  - `node_label(kind, args, name) -> str`: the name, else the first line of the prompt or message, through `redact_sensitive_text(max_length=80)`; the engine stores it on the node at creation.
  - `projected_cost_usd(turn_costs: Sequence[float], open_agents: int, spent: float) -> float`: spent plus the average of all finished turns (failed ones and schema corrections included) times the running and queued agent nodes.
  - `graph_meta(run, nodes, usage, *, large) -> dict` with exactly the spec's `meta` fields (counts are always the true ones, including `indeterminate`).
  - `graph_rows(run, nodes, usage_by_node, edges, log) -> Graph` (`phases`, `groups`, `nodes`, `edges`, `log`; stale nodes left out; a node row's `error` is its message cut to 200 characters; a group row's `total`, `done`, `failed` count all its nodes).
  - `fit_snapshot(graph, *, fits: Callable[[dict], bool]) -> dict`: adds rows in the order waiting for a person, failed and indeterminate, running, queued, completed newest first, then edges by count, keeping the largest set for which `fits(candidate)` holds (binary search over the ordered rows, not one call per row); what is left out is counted in `omitted` per status and for edges; when the groups alone do not fit, the innermost groups fold into their parent's counts until they do.
    Every field is bounded in the projection, never in the run: the title and each phase name are cut to 80 characters, the log to its last 20 lines of 200 characters. When a candidate with no node row still does not fit, the log, then the group rows, then the phase names are dropped (their counts stay in `meta` and `omitted`); the last form, `meta` plus `omitted`, has a fixed size and always fits.
  - `graph_delta(before: dict, after: dict) -> dict | None`: changed and new rows and `log_append`; returns the marker `REPLACE` when a row disappeared, the phases changed or `omitted` changed, and the publisher then sends a snapshot.
  - `edges` rows are `{"from", "to", "kind": "message", "count"}` and nothing else; no function in this module takes a message text or a shared-state value.

- [ ] **Step 1: Write the failing tests**

```python
def test_label_is_name_or_first_prompt_line_redacted_and_cut(): ...

def test_1000_unfinished_nodes_with_long_unicode_labels_fit_and_counts_stay_true():
    graph = graph_rows(RUN, nodes(queued=900, running=60, failed=40, label="検査 " * 16), {}, [], [])
    snapshot = fit_snapshot(graph, fits=encrypted_fits)            # the real check of live_frames/transport.py on an encrypted envelope
    shown = Counter(node["status"] for node in snapshot["nodes"])
    assert encrypted_fits(snapshot) and shown["failed"] == 40      # actionable rows first
    assert shown["queued"] + snapshot["omitted"]["queued"] == 900 and shown["running"] + snapshot["omitted"]["running"] == 60

def test_dense_edges_are_counted_when_left_out(): ...               # 5,000 edges: omitted["edges"] + len(edges) == 5000
def test_groups_that_do_not_fit_fold_into_their_parent(): ...       # 2,000 nested groups: the frame fits; the top group's total is unchanged
def test_delta_has_only_changed_rows_and_new_log_lines(): ...
def test_removed_row_or_changed_phases_asks_for_a_snapshot(): ...   # graph_delta(...) is REPLACE
def test_huge_log_and_500_long_phase_names_with_no_nodes_fall_back_to_counts_and_fit(): ...   # encrypted_fits(snapshot); meta counts unchanged
def test_counts_only_form_fits_for_any_run(): ...   # 10,000 root groups: the frame is meta plus omitted
def test_projection_includes_failed_turns_in_the_average(): ...
def test_stale_nodes_are_not_in_the_graph(): ...
```

- [ ] **Step 2: Run** `PYTEST tests/test_workflow_graph.py`. Expected: FAIL, `ModuleNotFoundError`.
- [ ] **Step 3: Implement** `graph.py` with no I/O.
- [ ] **Step 4: Run** the same command. Expected: PASS.
- [ ] **Step 5: Commit** `feat(workflow): graph snapshot model with fitting`.

### Task 16: Frames, card message, control events

**Files:**
- Create: `src/mindroom/live_frames/workflow.py`
- Modify: `src/mindroom/workflow_runs/engine.py`, `agent_nodes.py`, `src/mindroom/custom_tools/workflow.py`, `src/mindroom/matrix/event_types.py`, `src/mindroom/matrix/journal_ingress.py`, `src/mindroom/orchestration/workflow_runtime.py`, `docs/streaming.md`, `docs/tools/workflows.md`
- Test (engine harness): `tests/test_workflow_frames.py`, `tests/test_workflow_control.py`

**Interfaces:**
- Consumes: Task 15; the transport, its size check and the pacing of `live_frames/subagents.py` (`fits` as at `subagents.py:217`).
- Produces:
  - `WORKFLOW_FRAME_EVENT_TYPE = "io.mindroom.workflow"`, `WORKFLOW_RUN_CONTENT_KEY = "io.mindroom.workflow_run"`, `WORKFLOW_CONTROL_EVENT_TYPE = "io.mindroom.workflow_control"`.
  - `WorkflowFramePublisher.update(run_id, *, room_id, meta, graph) -> None` and `close(run_id)`: frames `{"v": 1, "run", "sid", "seq", "meta", "snapshot" | "delta"}` (`sid` from `new_publisher_id()`), a fitted snapshot first, every 10 s while the run is not terminal, and whenever `graph_delta` returns `REPLACE`; deltas between; at most one frame per 125 ms per account and room; encrypted as subagent frames are; every frame passes the transport's check before it is sent, and a delta that does not fit is replaced by a snapshot.
  - Engine: every node or run change marks the run dirty; a running node's live usage comes from its child's feed.
  - Card: `start_workflow` posts one agent message with `io.mindroom.workflow_run` (`{run, job_id, requester, title, status, counts, usage, phases, attempt, started_at, ended_at, error}`, `error` being the first line of a failed run's error) and the fallback body `Workflow "<title>" <status>: <n> done, <n> running, <n> waiting. Stop it with !cancel_job <job id>.`; it is sent with the transaction ID `wfcard-<run id>` so a restart does not post it twice; its event ID is saved as `card_event_id`.
    The engine edits it when the status, the phase list or the waiting state changes, otherwise at most once per 10 minutes, and at the end with `graph`: the last snapshot fitted with a `fits` that measures the whole edit event (the content appears twice in an edit, plus encryption) against the event size limit.
  - `EventKind.WORKFLOW_CONTROL` in `journal_ingress.py` and `async handle_workflow_control(event, *, engine, context_for) -> bool`. The run is loaded by ID and the event must be in the run's room and thread.
    `{run, action: "resume"}` from `run.owner_user_id` resumes through the same path as `resume_workflow` (a new job is registered when the old one ended).
    Anything else returns `False` and changes nothing.
  - Stop keeps the existing room-scoped meaning of `io.mindroom.background_job_cancel`; for a node's job ID the cancel reads the job runtime, since node jobs are not in room state.

- [ ] **Step 1: Write the failing tests**

```python
async def test_first_frame_is_a_snapshot_then_deltas_then_a_snapshot_every_ten_seconds(stack, clock): ...
async def test_every_frame_passes_the_transport_check_in_an_encrypted_room_with_1000_queued_nodes(stack): ...
async def test_removed_node_sends_a_snapshot_not_a_delta(stack): ...
async def test_running_node_usage_is_live(stack): ...
async def test_card_is_posted_once_across_a_restart(stack): ...   # the same transaction ID; one card event
async def test_card_is_edited_on_status_phase_and_waiting_changes_and_else_every_ten_minutes(stack, clock): ...
    # a 72 h idle-running run: fewer than 450 edits
async def test_final_card_edit_is_receivable_in_an_encrypted_room(stack): ...   # the whole edit event passes the size limit; it carries "graph"
async def test_failed_run_card_carries_the_first_error_line(stack): ...
def test_room_state_has_one_job_for_a_run_of_200_agents(stack): ...   # the workflow job only; no title, label or phase in it
async def test_resume_control_from_the_requester_in_the_runs_thread_resumes(stack): ...
async def test_resume_control_from_another_user_changes_nothing(stack): ...
async def test_resume_control_from_the_requester_in_another_room_changes_nothing(stack): ...
async def test_unknown_control_action_changes_nothing_and_sends_nothing(stack): ...   # {action: "page"} from a room member: no event sent
```

- [ ] **Step 2: Run** `PYTEST tests/test_workflow_frames.py tests/test_workflow_control.py`. Expected: FAIL, `ModuleNotFoundError: mindroom.live_frames.workflow`.
- [ ] **Step 3: Implement.** Share the stream pacing with `live_frames/subagents.py` instead of copying it; if that needs a small extraction, do it in this task with the subagent tests green.
- [ ] **Step 4: Run** the same command plus `PYTEST tests/live_frames tests/test_tool_job_publication.py`. Expected: PASS.
- [ ] **Step 5: Commit** `feat(workflow): live graph frames, run card and control events`. Then the full suite, the live gate (200 synthetic agents in an encrypted room: every frame accepted by the homeserver, one job in room state, no rate-limit errors; capture three frames and the card content into `tests/workflow_live/captured/` for Chat's fixtures), Sol and Astra review, open PR B6.

---

## PR B7: structured output, questions, cost

### Task 17: Structured output

**Files:**
- Create: `src/mindroom/workflow_runs/structured.py`
- Modify: `src/mindroom/workflow_runs/agent_nodes.py`, `src/mindroom/workflow.py`, `src/mindroom/custom_tools/workflow.py`, `docs/tools/workflows.md`
- Test (pure): `tests/test_workflow_structured.py`; (engine harness) the three async tests

**Interfaces:**
- Produces:
  - `SCHEMA_TURNS = 5`; `SchemaError(ValueError)`.
  - `check_schema(schema) -> None`: raises `SchemaError("schema is not valid JSON Schema: <path>: <reason>.")` using `jsonschema.Draft202012Validator.check_schema`.
  - `schema_prompt(prompt, schema) -> str`: the prompt, a blank line, `End your answer with one fenced json block that matches this JSON Schema:`, and the schema as indented JSON.
  - `extract_json(text) -> object`: the last fenced `json` block, else the whole text, read from the child's raw answer; raises `SchemaError("no JSON value found")`.
  - `validation_error(value, schema) -> str | None`: the first error as `<path>: <message>`.
- With a schema, `launch_child` checks it first (kind `schema`, no child starts) and runs the child on `schema_prompt`. On a failure the engine continues the same child as turn `n + 1` of the same generation (its own job ID, its own usage entry) with `Your answer did not match the schema: <error>. Reply with only the corrected fenced json block.`, up to turn 5, then fails the node with kind `schema` and the spec's message.
- The node result is the parsed value; JSON `null` is a valid result.

- [ ] **Step 1: Write the failing tests**

```python
async def test_invalid_schema_fails_before_any_agent_starts(stack): ...   # kind "schema"; no job exists
async def test_second_turn_fixes_the_answer_in_the_same_session_with_its_own_job_and_usage(stack):
    result = await stack.result("@0", "agent", {"prompt": "x", "schema": OK_SCHEMA}, script=[says("no json here"), says('```json\n{"ok": true}\n```')])
    assert result == {"ok": True} and len(stack.child_sessions()) == 1 and stack.child_turns() == 2
    assert len(stack.jobs_of_node("@0")) == 2 and stack.usage_entries("@0") == 2
async def test_five_bad_answers_fail_with_the_last_error(stack): ...
def test_extract_takes_the_last_fenced_block(): ...
def test_validation_error_names_the_path(): ...
def test_null_is_a_valid_value_for_a_schema_that_allows_it(): ...
def test_schema_is_part_of_the_digest(): ...
```

- [ ] **Step 2: Run** `PYTEST tests/test_workflow_structured.py`. Expected: FAIL, `ModuleNotFoundError`.
- [ ] **Step 3: Implement**; remove the Task 10 refusal of `schema` in the SDK and add the schema lines to the docstring.
- [ ] **Step 4: Run** `PYTEST tests/test_workflow_structured.py tests/test_workflow_agent_nodes.py`. Expected: PASS.
- [ ] **Step 5: Commit** `feat(workflow): structured agent output with retries`.

### Task 18: `ask_user`

**Files:**
- Create: `src/mindroom/workflow_runs/questions.py`
- Modify: `src/mindroom/workflow_runs/engine.py`, `src/mindroom/workflow.py`, `src/mindroom/matrix/event_types.py`, `src/mindroom/matrix/journal_ingress.py`, `src/mindroom/commands/parsing.py` and the command registry and help text, `src/mindroom/tool_jobs/client_cancel.py`, `src/mindroom/custom_tools/workflow.py`, `docs/tools/workflows.md`
- Test (engine harness): `tests/test_workflow_questions.py`, `tests/test_commands.py` (extend)

**Interfaces:**
- Produces:
  - `WORKFLOW_QUESTION_CONTENT_KEY = "io.mindroom.workflow_question"` (`{run, node, question_id, requester, text, options, free_text, status: "open" | "answered" | "cancelled", answer}`), `WORKFLOW_ANSWER_EVENT_TYPE = "io.mindroom.workflow_answer"` (`{run, node, option | text}`, related to the card), `WORKFLOW_ANSWER_REJECTED_EVENT_TYPE = "io.mindroom.workflow_answer_rejected"` (`{run, node, reason}`, related to the answer event), `EventKind.WORKFLOW_ANSWER`.
  - `question_id` is `"q" + sha256(f"{run_id}:{seq}:{generation}").hexdigest()[:6]`, so two runs in one thread, and a question and its rerun, have different IDs.
  - `async post_question(context, run, node, *, text, options) -> str`: sends the card with the transaction ID `wfq-<run id>-<seq>-<generation>` and the fallback body `Question q3fa91c: <text> 1. Yes 2. No. Answer with !answer q3fa91c <number or text>.`; returns the event ID, saved in `node.links["event_id"]` in the same step that sets the handle dispatched; a replay or restart that finds the node `WAITING` with no event ID sends again with the same transaction ID, which the homeserver deduplicates.
  - `AnswerOutcome = Literal["accepted", "not_for_you", "wrong_place", "no_longer_open", "already_answered", "not_an_option"]` and `async answer_question(engine, *, run_id, node_id, sender, room_id, thread_id, card_event_id, option, text) -> AnswerOutcome`: accepted only when the sender is `run.owner_user_id`, the room and thread are the run's, `card_event_id` is the event of the node's current generation, the node waits on `user`, and the option is one of its options. On `accepted` the node completes with the answer and the card is edited to `answered`; every other outcome sends the rejection event and a short notice in the thread.
  - `async answer_command(engine, *, room_id, thread_id, sender, text) -> str` for `!answer`: `!answer q3fa91c 2` answers the open card of the sender in that thread whose ID it is, and an ID that matches no open card (an old run, an old generation) answers nothing and replies `no longer open`; without an ID it answers only when the sender has exactly one open question in the thread, else it replies `Several questions are open: q3fa91c (Deploy?), q07be21 (Which branch?). Use !answer <id> <answer>.`; with none open `No open question for you in this thread.` It is registered in `commands/parsing.py`, the command registry and `!help`.
  - Engine kind `ask`: the node is `WAITING` with `waiting_on="user"` and frees its slot; an answer with no live attempt is saved on the node; `cancel` and `cancel_node_work` edit open cards to `cancelled`.
  - SDK `ask_user(question, options=None)`; options that are not a list of non-empty strings raise `WorkflowError("options must be a list of strings, such as ['Yes', 'No'].")`.

- [ ] **Step 1: Write the failing tests**

```python
async def test_ask_waits_then_returns_the_chosen_option(stack): ...

async def test_restart_between_posting_and_saving_posts_one_card(stack):
    stack.crash_engine_after_send()                          # the card was sent; links["event_id"] was not saved
    await stack.submit("@0", "ask", {"question": "Deploy?", "options": ["Yes", "No"]})
    stack = await stack.restart()
    await stack.recover()
    await stack.submit("@0", "ask", {"question": "Deploy?", "options": ["Yes", "No"]})
    assert len(stack.homeserver_events(WORKFLOW_QUESTION_CONTENT_KEY)) == 1          # deduplicated by transaction ID

async def test_answer_during_a_restart_is_returned_without_asking_twice(stack): ...   # cached True, "after lunch", one card

@pytest.mark.parametrize(("change", "outcome"), [
    ({"sender": "@mallory:example.org"}, "not_for_you"),
    ({"room_id": "!other:example.org"}, "wrong_place"),
    ({"card_event_id": "$card-of-generation-1"}, "no_longer_open"),
    ({"option": "Maybe"}, "not_an_option"),
])
async def test_rejected_answers_change_nothing_and_say_why(stack, change, outcome):
    receipt = await stack.open_question("@0", options=["Yes", "No"], generation=2)
    assert await stack.answer(receipt, **{**stack.valid_answer(receipt, "Yes"), **change}) == outcome
    assert stack.store.get_node(stack.run_id, "@0").state is WorkflowNodeState.WAITING
    assert stack.sent_events(WORKFLOW_ANSWER_REJECTED_EVENT_TYPE)[-1]["reason"] == outcome

async def test_second_answer_is_already_answered(stack): ...
async def test_answer_command_with_an_id_answers_that_question(stack): ...
async def test_two_runs_in_one_thread_ask_under_different_ids_and_a_late_answer_to_a_replaced_question_answers_nothing(stack): ...
async def test_waiting_past_the_attempt_limit_then_an_answer_continues_the_run(local_stack): ...   # real process, limit patched to 2 s, the answer comes after it
async def test_answer_command_without_an_id_needs_exactly_one_open_question(stack): ...   # two open: the list reply, nothing answered
async def test_answer_command_is_parsed_and_listed_in_help(): ...
async def test_another_lane_keeps_running_while_a_question_is_open(stack): ...
async def test_cancel_and_a_replaced_node_close_open_cards(stack): ...
def test_sdk_refuses_options_that_are_not_strings(sdk, in_main): ...
```

- [ ] **Step 2: Run** `PYTEST tests/test_workflow_questions.py`. Expected: FAIL, `ModuleNotFoundError`.
- [ ] **Step 3: Implement**; route `io.mindroom.workflow_answer` like `io.mindroom.tool_approval_response`, and `!answer` through the command registry as `!cancel_job` is.
- [ ] **Step 4: Run** the same command plus `PYTEST tests/test_commands.py`. Expected: PASS.
- [ ] **Step 5: Commit** `feat(workflow): ask_user pauses a branch for an answer`.

### Task 19: Cost notice and budget pause

**Files:**
- Modify: `src/mindroom/workflow_runs/engine.py`, `src/mindroom/custom_tools/workflow.py`, `docs/tools/workflows.md`
- Test (engine harness): `tests/test_workflow_budget.py`

**Interfaces:**
- Consumes: `run_usage` (Task 14), `projected_cost_usd` (Task 15), the `ask` node (Task 18), the card (Task 16).
- Produces:
  - `start_workflow(..., max_cost_usd=None)`.
  - The run is marked `large` once, when it has more than 25 `agent` or `turn` nodes or the projection passes `large_run_notice_usd` (0 turns the cost test off); the engine logs `Large run: <n> agents, about $<projected> projected.` and edits the card.
  - With `max_cost_usd`, when the whole-run sum (`cost_usd`, all generations and nested runs) reaches it, the engine starts no queued node, lets running ones finish, and creates one system `ask` node with key `$budget:<n>`, text `This run has spent $<spent> of its $<limit> limit. Continue?` and options `["Continue with $<limit> more", "Stop"]`; Continue adds the original amount, Stop cancels the run.
  - Waiting: the run is `WAITING` once nothing is `RUNNING` and every `QUEUED` node is held by the budget question; while running nodes finish it is still `RUNNING`.

- [ ] **Step 1: Write the failing tests**

```python
async def test_large_notice_fires_once_at_the_26th_agent(stack): ...
async def test_large_notice_fires_when_the_projection_passes_ten_dollars(stack): ...
async def test_budget_holds_queued_nodes_and_asks_and_the_run_waits_once_nothing_runs(stack):
    # limit 1.00, turns cost 0.60, max_concurrent 2, four agents
    ...   # after two finish: spent 1.20, two QUEUED held, one "$budget:1" ask, run state WAITING; no third child started
async def test_budget_counts_failed_and_rerun_turns(stack): ...   # a failed 0.60 turn plus its 0.60 rerun reach a 1.00 limit
async def test_cached_nodes_on_resume_do_not_move_the_budget(stack): ...
async def test_continue_adds_the_limit_and_releases_queued_nodes(stack): ...
async def test_stop_cancels_the_run(stack): ...
async def test_without_a_limit_nothing_pauses(stack): ...
```

- [ ] **Step 2: Run** `PYTEST tests/test_workflow_budget.py`. Expected: FAIL.
- [ ] **Step 3: Implement** in the engine's settle path.
- [ ] **Step 4: Run** the same command. Expected: PASS.
- [ ] **Step 5: Commit** `feat(workflow): large-run notice and max_cost_usd pause`. Then the full suite, the live gate (spec examples 1 and 2 as written; a script that asks one question and makes one approval-gated call, with `restart_mid_run.py` while each is open; expected: one card each, the answer and the approval accepted after the restart, one report), Sol and Astra review, open PR B7.

---

## PR B8: saved workflows

### Task 20: Saved workflows

**Files:**
- Create: `src/mindroom/workflow_runs/saved.py`
- Modify: `src/mindroom/custom_tools/workflow.py`, `src/mindroom/workflow.py`, `src/mindroom/workflow_runs/engine.py`, `src/mindroom/api/workflow_gateway.py`, `src/mindroom/commands/parsing.py` and the command registry, `docs/tools/workflows.md`
- Test (pure and engine harness): `tests/test_workflow_saved.py`

**Interfaces:**
- Produces:
  - `SavedWorkflow(name, description, args: dict[str, str], path: Path)`.
  - `read_saved_source(workspace: Path, name: str) -> bytes` and `list_saved(workspace) -> list[SavedWorkflow]`: the name matches `[a-z0-9][a-z0-9_-]{0,63}`; the file is opened with the descriptor-relative confined read script sources use (`open_directory_within_root`), so a symlink swapped in during the read cannot escape; parsing uses `ast` only (first docstring line, literal `ARGS` dict or `{}`).
  - `save_source(workspace, name, source, description) -> Path`: adds the docstring when the source has none; writes with the confined, atomic, no-clobber write of `delegation/records.py:681-700`; an existing file with other content raises `SavedWorkflowExistsError("workflows/<name>.py already exists with different content. Pick another name or edit the file.")`; two simultaneous saves of different content leave one winner and one such error.
  - Toolkit: `start_workflow(name=...)` (exactly one of `source`, `path`, `name`; the bytes are snapshotted at start), `list_workflow_runs()` returning `{runs: [...], saved: [...]}` (runs through `owned_run`'s owner filter for this conversation), `save_workflow(run_id, name, description=None)` (the run through `owned_run`); `!workflows` prints the same listing and is registered with the command registry and `!help`.
  - Gateway `GET /sources/{name}`: returns the saved file's bytes and SHA-256, snapshotted once per attempt in the `workflow_sources` table (store migration 2; Task 23's is 3, and whichever lands second takes the higher number), so every `run(name)` in an attempt executes the same bytes.
  - SDK `run(name, args=None)`: fetches the source, submits a `group` node `{"op": "run", "name", "source_sha256"}`, executes those bytes as a fresh module in the child lane `f"{lane}/{i}:run"`, and returns its `main(args)`. It adds no delegation depth.
  - The workspace holds code only. No run result is written there by MindRoom; the docs say who can edit a saved workflow in a shared workspace.
  - Docs: `docs/tools/agent-orchestration.md` marks Dynamic Workflows as deprecated in favour of `workflow` in one sentence with a link, and `docs/tools/workflows.md` gets "Moving from Dynamic Workflows" (both toolkits can be enabled together; old runs stay readable; no YAML runs on the new engine). No code of `dynamic_workflows/` changes; `test_an_agent_may_have_both_toolkits` pins that config validation passes with both.

- [ ] **Step 1: Write the failing tests**

```python
def test_list_saved_parses_without_running_the_file(tmp_path): ...   # a file that raises SystemExit at import still lists
def test_save_refuses_to_replace_a_different_file_and_accepts_an_identical_one(tmp_path): ...
def test_two_simultaneous_saves_leave_one_winner(tmp_path): ...   # two threads: one path written, one SavedWorkflowExistsError
def test_bad_names_and_symlinks_are_refused(tmp_path): ...   # "../x", "Has Space", a symlinked workflows directory, a symlinked file
async def test_start_by_name_snapshots_the_file(stack): ...   # editing the file after start does not change run.source
async def test_save_workflow_refuses_another_owners_run(stack, other_requester_tools): ...
async def test_a_finished_run_writes_nothing_into_a_shared_workspace(stack): ...   # two requesters share the workspace: no file of the run appears there
async def test_list_workflow_runs_has_this_conversations_runs_and_the_saved_files(stack): ...
async def test_run_executes_the_bytes_snapshotted_for_the_attempt(stack): ...
    # the file changes between two run("x") calls of one attempt: both get the first bytes; the group's digest holds their hash
async def test_edited_saved_file_reruns_its_group_on_resume(stack): ...
def test_sdk_run_calls_main_in_a_child_lane(gateway, sdk, in_main): ...   # inner keys start with "/0:run@"
async def test_workflows_command_is_parsed_and_listed_in_help(): ...
def test_an_agent_may_have_both_toolkits(): ...   # config validation passes; tool names are disjoint
```

- [ ] **Step 2: Run** `PYTEST tests/test_workflow_saved.py`. Expected: FAIL, `ModuleNotFoundError`.
- [ ] **Step 3: Implement**; add `name=`, `run` and the two functions to the descriptions.
- [ ] **Step 4: Run** `PYTEST tests/test_workflow_saved.py tests/test_workflow_tool.py tests/test_commands.py`. Expected: PASS.
- [ ] **Step 5: Commit** `feat(workflow): saved workflows run by name`. Then the full suite, the live gate (save a run, start it by name with new args), Sol and Astra review, open PR B8.

### Task 21 (cut in review round 2)

The report-publishing source and the plugin SDK import are under "v1 and later" in the spec; nothing in `custom_tools/report_publishing.py`, `plugin_check.py` or `tool_system/plugin_imports.py` changes in this plan.

---

## PR B9a: sessions

### Task 22: Sessions

**Files:**
- Modify: `src/mindroom/workflow.py`, `src/mindroom/workflow_runs/engine.py`, `agent_nodes.py`, `graph.py`, `src/mindroom/custom_tools/workflow.py`, `docs/tools/workflows.md`
- Test (engine harness): `tests/test_workflow_sessions.py`

**Interfaces:**
- Produces:
  - SDK `spawn(name, **options) -> Session` (a `session` node whose args are the options and the name) and `Session.ask(message, *, schema=None, timeout=None)` (a `turn` node with args `{"session": <session node key>, "turn": <n from 1>, "message", "schema"}`). The gateway, not the SDK, adds the lineage to the digest: a SHA-256 over the digest and generation of each earlier turn node of that session as it stands in this attempt. So a turn that changed, or that ran again with the same message and gave another answer, changes all later turns of that session in any lane.
  - Engine kind `turn`: turn 1 runs a fresh child named after the session through `launch_child`; later turns continue it through `resolve_follow_up_target` with `previous=<the child of the last turn>`; each turn is its own job (`turn_job_id` with the session turn number) and usage entry; `session_key` is set on every turn node and the session node's `links["subagent_id"]` names the child.
  - A second `ask` on a session that is in a turn fails with `Session '<name>' is already in a turn. Wait for it or spawn another agent.`; two different sessions asked from `parallel` run at once.
  - Replay: a turn whose digest equals the saved turn hits; the turn after the last saved one continues the saved child; a turn that differs earlier starts a fresh child (a new generation of the session) whose prompt is `Earlier conversation:` plus the saved messages and answers of the turns before it, a blank line, and the new message.
  - Graph: turn nodes carry `session`; the session's row reports `turns`.

- [ ] **Step 1: Write the failing tests**

```python
async def test_second_turn_continues_the_same_child_as_its_own_job(stack): ...   # one child session, two jobs, prompts in order
async def test_resume_appends_to_the_saved_session(stack): ...
async def test_changed_middle_turn_starts_a_fresh_child_with_the_transcript(stack): ...
async def test_changed_turn_in_one_lane_reruns_the_sessions_later_turn_in_another_lane(stack):
    # turn 1 asked in lane /0:p0, turn 2 in lane /0:p1; attempt 2 changes turn 1's message
    ...   # turn 2 misses (its lineage changed) although its own message is unchanged
async def test_two_asks_at_once_on_one_session_are_refused_and_two_sessions_run_together(stack): ...
async def test_rerun_of_turn_one_with_the_same_message_reruns_turn_two(stack): ...   # turn 1 failed and ran again as generation 2: turn 2 misses
def test_sdk_turn_numbers_count_per_session(gateway, sdk, in_main): ...
```

- [ ] **Step 2: Run** `PYTEST tests/test_workflow_sessions.py`. Expected: FAIL (`spawn` is missing).
- [ ] **Step 3: Implement**; add the `spawn` lines and the `review_loop` example to the docstring.
- [ ] **Step 4: Run** `PYTEST tests/test_workflow_sessions.py tests/test_workflow_resume.py tests/test_delegation_sessions.py`. Expected: PASS.
- [ ] **Step 5: Commit** `feat(workflow): sessions that keep an agent's conversation`. Then the full suite, the live gate (spec example 3 on a throwaway stack, with a restart between two rounds), Sol and Astra review, open PR B9a.

---

## PR B9b: agents that talk

### Task 23: Peer messages and shared state, exactly once

**Files:**
- Create: `src/mindroom/workflow_runs/peers.py`, `src/mindroom/custom_tools/workflow_peers.py`
- Modify: `src/mindroom/workflow_runs/store.py` (migration 3), `engine.py`, `agent_nodes.py`, `graph.py`, `src/mindroom/workflow.py`, `src/mindroom/custom_tools/workflow.py`, `src/mindroom/agents.py`, `tach.toml`, `docs/tools/workflows.md`
- Test (engine harness): `tests/test_workflow_peers.py`

**Interfaces:**
- Produces:
  - Store migration 3: `workflow_messages(run_id, seq, sender_key, sender_generation, sender_name, recipient_name, text, withdrawn, created_at)`, `workflow_cursors(run_id, reader_key, reader_generation, seq)`, `workflow_state(run_id, key, value_json, updated_by, updated_at)`, `workflow_peer_calls(run_id, op_id, result_json)` (primary key `run_id, op_id`), all removed by `prune`.
  - `peers.py`, every function one transaction that first looks up `op_id` in `workflow_peer_calls` and returns the saved result when present, else performs the effect and saves the result:
    - `send(store, run_id, *, op_id, sender: NodeRef, to: str, text: str) -> dict` (`{delivered, held}`): `to` is a name, `*` or `script`; a name with no agent yet is `held: true`; a name whose agents have all finished raises `PeerUnreachableError` with the spec's message.
    - `read(store, run_id, *, op_id, reader: NodeRef, names) -> list[dict]`: unread, not withdrawn messages for the reader's names, and the cursor moves in the same transaction as the saved result.
    - `get_state(..., op_id, key=None)`, `set_state(..., op_id, key, value, updated_by)`.
  - `op_id`: for an agent, the SHA-256 of `(node key, generation, the child's model run ID, tool_call_id, operation name, canonical JSON of its arguments)`, so a provider retry is the same operation and a call ID reused in a continued turn or with other arguments is a new one; for the script, the node's key and generation, since `send`, `inbox`, `state.get` and `state.set` are submitted as nodes of kinds `send`, `inbox`, `state` through `_call` like every SDK call. A replayed script `send` is a cache hit and sends nothing.
  - Waiting: `engine.wait_for_messages(run_id, reader, wait_seconds) -> list[dict]` sets the node `WAITING` on `peer`, frees its slot, waits on the run's condition with no ceiling on `wait_seconds`, and takes a slot again before it returns. The script's `inbox(wait_seconds)` is the same wait behind a node poll.
  - Runnable report: the SDK keeps, under one lock, `runnable` (lanes not started yet plus lanes running Python between calls, the root lane included) and a counter that rises with every change; every submit and poll request carries both as `X-Workflow-Runnable: <counter>:<runnable>`, and the engine keeps the value with the highest counter per attempt.
  - Deadlock notice: when every unfinished agent node waits on `peer`, nothing is `QUEUED` or `RUNNING`, and the newest report says `runnable == 0`, every waiter returns `[]` with `note: "Every agent is waiting for a message and nobody is left to send one."` A lane that has not submitted yet is counted in `runnable`, so a sender held behind the SDK's own limit is never taken for a deadlock (Task 12 lets it start once the waiting lane frees its place).
  - Reruns: when a node starts a new generation, `withdraw_unread(run_id, sender_key, old_generation)` marks its unread messages withdrawn, and the new generation's cursor starts at 0.
  - `WorkflowPeerTools` (toolkit `workflow_peers`), given to every child whose `workflow_run_id` is set: `send_peer_message(to, text)`, `read_peer_messages(wait_seconds=0)`, `list_peers()`, `get_shared_state(key=None)`, `set_shared_state(key, value)`. Each resolves its run through the child's own `workflow_run_id` and node, never from an argument.
  - Messages queued for a session between turns are put in front of its next message as `Messages received since your last turn:` and one `<sender>: <text>` line each.
  - SDK `send(to, text)`, `inbox(wait_seconds=0)`, `state.get(key, default=None)`, `state.set(key, value)`; the script's name is `script`.
  - A message or value over the 1 MiB request limit is refused with `Too large (<n> bytes, limit 1 MiB). Write it to a workspace file and send the path.`
  - Graph `edges`: `{"from", "to", "kind": "message", "count"}` only. Message texts and state values leave the store through one function, `get_workflow(run_id, messages=True)` behind `owned_run`; the frame publisher, the card and room state have no code path to them.

- [ ] **Step 1: Write the failing tests**

```python
async def test_message_to_a_name_nobody_has_yet_is_held_and_delivered_when_it_starts(stack):
    backend = await stack.start_agent("/0:p0@0", name="backend")
    assert stack.peer_send(backend, to="frontend", text="contract v1") == {"delivered": 0, "held": True}
    frontend = await stack.start_agent("/0:p1@0", name="frontend")              # submitted after the send
    assert [m["text"] for m in stack.peer_read(frontend)] == ["contract v1"]

async def test_a_retried_tool_call_sends_once_and_reads_the_same_messages(stack):
    a, b = await stack.two_agents("a", "b")
    for _ in range(2):                                                           # the provider retried the same tool call
        stack.peer_send(a, to="b", text="hello", tool_call_id="call-1")
    assert len(stack.store_messages()) == 1
    first = stack.peer_read(b, tool_call_id="call-2")
    assert stack.peer_read(b, tool_call_id="call-2") == first and stack.peer_read(b, tool_call_id="call-3") == []

async def test_script_send_is_not_repeated_on_resume_and_inbox_replays(stack): ...   # attempt 2: "send" node cached; "inbox" returns the saved list
async def test_state_write_is_not_reapplied_on_resume_over_a_newer_value(stack): ...
async def test_waiting_reader_frees_the_only_slot_for_its_sender(stack):
    # max_concurrent=1: "frontend" waits in read_peer_messages(600); "backend" is queued
    ...   # backend starts, sends, frontend wakes within 1 s; both finish
async def test_everyone_waiting_returns_with_the_note_instead_of_hanging(stack): ...
async def test_rerun_agent_sees_the_messages_its_predecessor_read_and_its_old_unread_messages_are_withdrawn(stack): ...
async def test_two_agents_sharing_a_name_each_get_a_copy_and_star_reaches_all_unfinished(stack): ...
async def test_message_to_a_finished_agent_is_refused_with_the_hint(stack): ...
async def test_session_gets_queued_messages_in_front_of_its_next_turn(stack): ...
async def test_peer_tools_reach_only_the_childs_own_run(stack): ...   # a child of another run cannot read this run's state
async def test_workflow_children_get_the_peer_tools_and_other_children_do_not(stack): ...
async def test_message_edges_carry_counts_and_no_frame_card_or_state_event_carries_a_text(stack): ...   # the marker text is in no sent event; get_workflow(messages=True) of the owner has it, of another requester reads as unknown
async def test_reused_call_id_in_a_continued_turn_or_with_other_arguments_is_a_new_operation(stack): ...
async def test_sender_lane_not_yet_started_is_not_a_deadlock(local_stack): ...
    # real SDK: foreach([reader, sender], fn, max_concurrent=1); the reader waits in read_peer_messages(600); the sender lane has not submitted
    # no deadlock note is returned; the sender starts, sends, both finish
async def test_oversized_text_is_refused_with_the_file_hint(stack): ...
```

- [ ] **Step 2: Run** `PYTEST tests/test_workflow_peers.py`. Expected: FAIL, `ModuleNotFoundError: mindroom.workflow_runs.peers`.
- [ ] **Step 3: Implement**; add the peer lines to the docstring.
- [ ] **Step 4: Run** `PYTEST tests/test_workflow_peers.py tests/test_workflow_sessions.py tests/test_workflow_graph.py tests/test_workflow_run_store.py`. Expected: PASS.
- [ ] **Step 5: Commit** `feat(workflow): peer messages and shared state, exactly once`.

### Task 24: An agent's own `ask_user`, the examples, the blind authoring test

**Files:**
- Modify: `src/mindroom/custom_tools/workflow_peers.py`, `src/mindroom/workflow_runs/questions.py`, `engine.py`, `docs/tools/workflows.md`
- Create: `tests/workflow_examples/` (the spec's example scripts as files), `tests/workflow_live/blind_authoring.py`
- Test (engine harness, local integration): `tests/test_workflow_child_questions.py`, `tests/test_workflow_examples.py`

**Interfaces:**
- Produces:
  - `WorkflowPeerTools.ask_user(question, options=None)`: creates an `ask` node under the agent's node with key `f"{node key}?{sha256(question, options)[:12]}"`, posts the same card as Task 18, and waits inside `ToolJobRuntime.human_decision_wait(job_id)` with `wait: "user"` recorded on the job's adapter (Task 11b tells it from an approval wait by that), so the job shows `awaiting_approval` and the node `waiting` on `user`. A node of that key that is already answered returns its answer at once, so an agent continued after a restart that asks the same question again does not ask the user twice.
  - `tests/test_workflow_examples.py`: every example of the spec runs on `LocalWorkflowStack` with the scripted model, including a failing finder in example 5 (the run fails instead of reporting "no bugs"), the "Five more rounds" answer in example 3 (rounds 6 to 10 run), and the team example at `max_concurrent=1` (it finishes).
  - `tests/workflow_live/blind_authoring.py`: gives a fresh model only the built `start_workflow` description and three task sentences (a scatter and join, a bounded check loop, two named agents that negotiate), runs what it writes on the live stack, and prints pass or fail per task with the script. It is run with Haiku, Opus and Sol; a failure is fixed by changing the description text, then the spec.

- [ ] **Step 1: Write the failing tests**

```python
async def test_child_question_waits_while_another_lane_keeps_running(stack): ...
async def test_child_question_unanswered_across_a_restart_is_asked_once(stack):
    await stack.submit("@0", "agent", {"prompt": "x"}, script=[calls_tool("workflow_peers", "ask_user", question="Which branch?"), says("main")])
    await stack.until(stack.has_open_question)
    stack = await stack.restart(script_after=[calls_tool("workflow_peers", "ask_user", question="Which branch?"), says("main")])
    await stack.recover()
    await stack.answer_open_question(text="main")
    assert await stack.result("@0", "agent", {"prompt": "x"}) == "main"
    assert len(stack.homeserver_events(WORKFLOW_QUESTION_CONTENT_KEY)) == 1
async def test_answer_arriving_with_no_process_attached_is_returned_later(stack): ...

@pytest.mark.parametrize("example", sorted(Path("tests/workflow_examples").glob("*.py")), ids=lambda p: p.stem)
async def test_spec_example_runs(local_stack, example): ...
async def test_loop_until_dry_fails_when_a_finder_fails(local_stack): ...
async def test_review_loop_runs_five_more_rounds_when_asked(local_stack): ...
async def test_team_example_finishes_at_max_concurrent_one(local_stack): ...
def test_examples_in_the_spec_and_in_tests_are_the_same_text(): ...   # the fenced blocks of the spec equal the files
```

- [ ] **Step 2: Run** `PYTEST tests/test_workflow_child_questions.py tests/test_workflow_examples.py`. Expected: FAIL.
- [ ] **Step 3: Implement.**
- [ ] **Step 4: Run** the same command plus `PYTEST tests/test_workflow_questions.py tests/test_workflow_peers.py`. Expected: PASS.
- [ ] **Step 5: Commit** `feat(workflow): agents ask the user; examples run in tests`. Then the full suite, the live gate (spec example 6 on a throwaway stack; `blind_authoring.py` with three models; paste its output into the pull request), Sol and Astra review, open PR B9b.

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

### Task 25: Sync filter, frame parser, message contents, run store

**Files:**
- Create: `src/app/matrix/protocol/workflowFrame.ts`, `src/app/matrix/protocol/workflowRunContent.ts`, `src/app/messages/live/workflowRunStore.ts`, `src/app/messages/live/bindWorkflowFrames.ts`, `src/app/messages/live/__fixtures__/workflow-frames-v1.json`
- Modify: `src/app/matrix/client/clientSyncPolicy.ts`, `src/app/shell/useBindAtoms.ts`, `src/app/matrix/protocol/backgroundJobsContract.ts`, `docs/architecture.md` ("MindRoom event formats": rows for the frame, the two message contents and the three events)
- Move: `src/app/background-jobs/cancelBackgroundJob.ts` and its test to `src/app/matrix/protocol/` (imports updated, no behavior change), so the card (layer 7) and the panel (layer 8) can send a cancel
- Test: `clientSyncPolicy.test.ts`, `workflowFrame.test.ts`, `workflowRunContent.test.ts`, `workflowRunStore.test.ts`, `bindWorkflowFrames.test.ts`, `backgroundJobsContract.test.ts`, `e2e/workflow-run/workflow-frames-sync.spec.ts`

**Interfaces:**
- **Sync filter.** `SYNC_EPHEMERAL_TYPES` in `clientSyncPolicy.ts` gains `'io.mindroom.workflow'` (the list is exported for the test). Without it the homeserver never sends a frame to Chat, whatever the binder does.
- Consumes: the backend frame and contents of Tasks 15, 16 and 18, pinned by the files PR B6 captured in `tests/workflow_live/captured/` (snake case on the wire, camel case in the client); `LiveOrder`, `compareLiveOrder` (`matrix/protocol/liveFrame.ts`), `bindLiveFrames`, `SYNTHETIC_EVENT_ID_PREFIX` (`messages/live/bindLiveReplyFrames.ts`).
- Produces, in `workflowFrame.ts`:
  - `WORKFLOW_FRAME_EVENT_TYPE = 'io.mindroom.workflow'`.
  - `WorkflowRunStatus = 'starting' | 'running' | 'waiting' | 'completed' | 'failed' | 'cancelled' | 'interrupted'`, `WorkflowNodeStatus = 'queued' | 'running' | 'waiting' | 'completed' | 'failed' | 'cancelled' | 'indeterminate'`, `isLiveWorkflowRunStatus(status)` (true for the first three).
  - `WorkflowRunUsage = { inputTokens; outputTokens; toolCalls; costUsd?; attemptCostUsd?; projectedCostUsd?; costUnknown: boolean }`, `WorkflowRunCounts = { queued; running; waiting; completed; failed; indeterminate; cached }`.
  - `WorkflowRunMeta = { agent; threadId: string | null; title: string | null; status; attempt; startedAt; endedAt: number | null; maxConcurrent; maxCostUsd: number | null; large: boolean; counts; usage }`.
  - `WorkflowRunNode = { id; kind; label; name: string | null; phase: number | null; group: string | null; status; waitingOn: 'approval' | 'user' | 'budget' | 'peer' | null; agent?; model?; run?; subagent?; job?; session: string | null; turns; startedAt: number | null; endedAt: number | null; cached; error: string | null; usage? }`.
  - `WorkflowRunGroup = { id; kind: string; phase: number | null; parent: string | null; total; done; failed }` (kinds today: `foreach`, `parallel`, `run`), `WorkflowRunEdge = { from; to; count }`.
  - `WorkflowRunOmitted = { queued; running; waiting; completed; failed; edges }` and `WorkflowRunGraph = { phases: string[]; groups; nodes; edges; log: string[]; omitted: WorkflowRunOmitted }`.
  - `WorkflowRunDelta = { nodes?; groups?; edges?; logAppend? }`, `WorkflowFrame = { run; meta } & LiveOrder & ({ snapshot: WorkflowRunGraph } | { delta: WorkflowRunDelta })`.
  - `parseWorkflowFrame(content: unknown): WorkflowFrame | null`, `parseWorkflowRunGraph(value: unknown): WorkflowRunGraph | null`, `applyWorkflowRunDelta(graph, delta): WorkflowRunGraph` (rows replace by `id`, new rows append, `edges` replaces, the log keeps its last 50 lines).
  - Parsing rule: a frame without a valid `run` (`wf-` and 32 hex), `sid`, `seq` or `meta` is dropped; a snapshot with no rows at all (the counts-only form) is valid; a malformed node, group or edge row is dropped and the frame is kept; an unknown `kind` or extra field is kept or ignored, never an error.
- Produces, in `workflowRunContent.ts`:
  - `WORKFLOW_RUN_CONTENT_KEY = 'io.mindroom.workflow_run'`, `WORKFLOW_QUESTION_CONTENT_KEY = 'io.mindroom.workflow_question'`, `WORKFLOW_ANSWER_EVENT_TYPE = 'io.mindroom.workflow_answer'`, `WORKFLOW_ANSWER_REJECTED_EVENT_TYPE = 'io.mindroom.workflow_answer_rejected'`, `WORKFLOW_CONTROL_EVENT_TYPE = 'io.mindroom.workflow_control'`.
  - `WorkflowRunCard = { run; jobId: string | null; requester: string | null; title: string | null; status; counts; usage; phases: string[]; attempt; startedAt: number | null; endedAt: number | null; error: string | null; graph?: WorkflowRunGraph }` and `parseWorkflowRunCard(content): WorkflowRunCard | null`.
  - `WorkflowRunQuestion = { run; node; questionId: string; requester: string | null; text; options: string[]; freeText: boolean; status: 'open' | 'answered' | 'cancelled'; answer: string | null }` and `parseWorkflowRunQuestion(content): WorkflowRunQuestion | null`.
  - `sendWorkflowRunAnswer(mx, room, threadId, cardEventId, question, answer: { option: string } | { text: string }): Promise<void>` and `sendWorkflowRunResume(mx, room, threadId, run): Promise<void>`; both add the thread relation as `cancelBackgroundJob` does and discard a failed local echo. `sendWorkflowRunAnswer` resolves with the answer event's ID.
  - `parseWorkflowAnswerRejection(event): { answerEventId: string; reason: string } | null`, read only from a MindRoom agent sender.
- Produces, in `workflowRunStore.ts`:
  - `WorkflowRunEntry = { roomId; sender; run; sid; seq; meta: WorkflowRunMeta; graph: WorkflowRunGraph; lastFrameAt: number; stale: boolean }`.
  - `WorkflowRunStore` with `ingest(roomId, sender, frame, receivedAt)`, `showCard(roomId, sender, card, receivedAt)` (adds a finished run's graph from its card when no newer entry exists), `get(roomId, run)`, `subscribe(roomId, callback)`, `clear()`; `getWorkflowRunStore(mx)`, `useRoomWorkflowRuns(roomId)`, `useWorkflowRun(roomId, run)`.
  - Ordering as `nextLiveEntry`: a frame from another sender or not newer by `compareLiveOrder` is ignored; a snapshot replaces the graph; a delta applies only when `sid` is equal and `seq` is the held `seq + 1`, else the entry turns `stale` until the next snapshot.
  - Kept: a live run while its last frame is younger than `WORKFLOW_RUN_FRESHNESS_MS = 30_000`, then it reads `stale`; an ended run for `WORKFLOW_RUN_KEPT_MS = 30 * 60 * 1000`.
- Produces, in `bindWorkflowFrames.ts`: `bindWorkflowFrames(mx, store?)` and `useBindWorkflowFrames(mx)`, a copy of `bindSubagentFrames.ts` with the ID prefix `` `${SYNTHETIC_EVENT_ID_PREFIX}workflow.` `` that hands a content to `parseWorkflowFrame`; `useBindAtoms` calls the hook after `useBindSubagentFrames`.
- `BackgroundJob` gains `workflow?: string` (the run ID on the run's own job; node jobs are never in room state), parsed like the other join keys (malformed drops the key, not the job).

- [ ] **Step 1: Write the fixture and the failing tests.** The fixture holds four frames of one invented run as the backend sends them: a snapshot, a delta that completes one node, a delta that adds a log line, and a snapshot with `omitted` counts; they are copied from `tests/workflow_live/captured/` with the names replaced by invented ones.

```ts
it('asks the homeserver for workflow frames', () => {
  expect(SYNC_EPHEMERAL_TYPES).toContain('io.mindroom.workflow');
  expect(buildSyncFilter(mx).getDefinition().room?.ephemeral?.types).toContain('io.mindroom.workflow');
});
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
  expect(store.get(ROOM, RUN)).toMatchObject({ stale: false, graph: { omitted: { queued: 412, completed: 12 } } });
});
it('keeps a counts-only snapshot and its omitted counts', () => { /* no phases, groups or nodes: the entry holds meta.counts and omitted */ });
it('follows a new stream after a restart', () => { /* sid "b", seq 1 replaces sid "a", seq 40 */ });
it('ignores frames of another sender for a held run', () => { /* ... */ });
it('shows a finished card graph only when it holds nothing newer', () => { /* showCard after a live terminal frame changes nothing */ });
it('reads the workflow join key and drops a malformed one', () => {
  expect(selectJobs([{ ...job, kind: 'workflow', workflow: RUN }])[0].workflow).toBe(RUN);
  expect(selectJobs([{ ...job, workflow: 5 }])[0]).not.toHaveProperty('workflow');
});
it('reads a question and its answered edit', () => { /* status 'answered', answer 'Yes'; a content without options gives [] and freeText true */ });
it('sends an answer related to the card in its thread', async () => { /* content {run, node, option: 'Yes', 'm.relates_to': {rel_type: 'm.thread', event_id: THREAD, ...}} */ });
it('reads a rejection only from an agent', () => { /* a person's io.mindroom.workflow_answer_rejected is ignored */ });
```

`workflow-frames-sync.spec.ts` (Playwright, the test homeserver) proves delivery, which no store fixture can: the room's agent account PUTs a snapshot and a delta as `io.mindroom.workflow` ephemeral events, and the page's `getWorkflowRunStore` holds the run with the delta applied.
It runs three times: a fresh login; a returning client whose stored sync filter predates this change (seed the old filter definition in its storage first, then reload); and an encrypted room.
If the test homeserver refuses the event type, extend it first and say so in the pull request.

- [ ] **Step 2: Run** `VITEST src/app/matrix/protocol/workflowFrame.test.ts src/app/matrix/protocol/workflowRunContent.test.ts src/app/messages/live/workflowRunStore.test.ts`. Expected: FAIL, the modules do not exist.
- [ ] **Step 3: Implement.** When the returning-client case fails because the SDK reuses a stored filter ID, make the filter's name carry a version so a changed definition creates a new filter.
- [ ] **Step 4: Run** the same command plus `src/app/matrix/client/clientSyncPolicy.test.ts src/app/messages/live/bindWorkflowFrames.test.ts src/app/matrix/protocol/backgroundJobsContract.test.ts src/app/matrix/protocol/cancelBackgroundJob.test.ts src/test/layering.test.ts`. Expected: PASS. Then `just e2e-slot <n> e2e/workflow-run/workflow-frames-sync.spec.ts` on a free slot. Expected: 3 passed.
- [ ] **Step 5: Commit** `feat(workflow-run): sync filter, frame parser, message contents and run store`.

### Task 26: Run card, question card, jobs-bar row

**Files:**
- Create: `src/app/messages/workflow-run/WorkflowRunCard.tsx`, `WorkflowRunCard.css.ts`, `WorkflowQuestionCard.tsx`, `workflowRunLabels.ts`, `e2e/fixtures/workflow-run.tsx`, `e2e/fixtures/workflow-run.html`, `e2e/workflow-run/workflow-run-card.spec.ts`
- Modify: `src/app/messages/content/renderAgentMessageContent.tsx`, `src/app/background-jobs/backgroundJobsPresentation.ts`, `BackgroundJobRow.tsx`, `BackgroundJobsBar.tsx`, `useJobLookups.ts`, the 17 overlays in `src/app/i18n/locales/additions/`, `e2e/background-jobs/jobs-bar-rich-rows.spec.ts`
- Test: `WorkflowRunCard.test.tsx`, `WorkflowQuestionCard.test.tsx`, `backgroundJobsPresentation.test.ts`, `BackgroundJobsBar.test.tsx`

**Interfaces:**
- Consumes: Task 25; `cancelBackgroundJob` (now in `matrix/protocol/`); the option-button and fold styles of `messages/question/QuestionCard.tsx`; the usage cells and `JobElapsed` of `BackgroundJobRow.tsx`.
- Produces:
  - `workflowRunLabels.ts`: `workflowRunStatusLabel(status, t)`, `workflowRunCountsLine(counts, t)` and `workflowRunCostLine(usage, t)`, shared by the card, the row and the panel.
  - `WorkflowRunCard({ card, roomId, threadId, eventId, sender, readOnly })`: shows the live entry of `useWorkflowRun(roomId, card.run)` when it is newer than the card (its `meta` wins over the card's fields), else the card.
    Content: title (or "Workflow"), status pill, phases as a row of names, the counts line, the cost line ("at least" when `costUnknown`), live elapsed time, "Large run" when `meta.large`, a failed run's `error` line, and "{{count}} failed" on a finished run whose counts hold failed or indeterminate calls.
    Stop shows while the status is live, the viewer may cancel jobs, and `card.jobId` is set; it sends `cancelBackgroundJob(mx, room, threadId, { jobId })` and reads "Stopping…" until the status changes or the send fails.
    For the viewer who is `card.requester`, Resume shows for `failed`, `cancelled` and `interrupted`, and Run again for `completed`; both call `sendWorkflowRunResume` and read "Starting…" until a newer status arrives or 15 seconds pass.
    The card has no Graph button yet; Task 28 adds it with the panel.
  - `WorkflowQuestionCard({ question, roomId, threadId, eventId, readOnly })`: one button per option, a text field with Send when `freeText`, all disabled with the line "Only {{name}} can answer." for anyone but `question.requester`; `answered` shows "Answered: {{answer}}" and `cancelled` shows "Cancelled", both without controls.
    A sent answer disables the controls until the card's edit arrives. A rejection event for that answer (`parseWorkflowAnswerRejection`) enables them again and shows the reason ("Already answered.", "This question is no longer open.", "That is not one of the options.", "Only the person who started the run can answer."). With neither after 15 seconds they enable again with "No reply yet. Try again." A failed send enables them at once and shows the room's send-failure text.
  - `renderAgentMessageContent`: from a MindRoom agent only, a content with `io.mindroom.workflow_run` renders `WorkflowRunCard` and one with `io.mindroom.workflow_question` renders `WorkflowQuestionCard`, placed before the secret-request branch; from anyone else the body renders as text.
  - Jobs bar: `BackgroundJobsBarItem` gains `workflowRun?: WorkflowRunEntry`, and `BackgroundJobLookups` gains `workflowRuns: ReadonlyMap<string, WorkflowRunEntry>` filled by `useJobLookups` from `useRoomWorkflowRuns`.
    A job of `kind: 'workflow'` is a row of the new first group `workflow` ("Workflows · {{number}}"): headline the run's title, detail the counts line, the usage cells from `meta.usage`, elapsed time, and Stop.
    The run's agents are not rows: MindRoom does not list node jobs in room state, so the bar needs no hiding rule.
    The row's status is `waiting` when the run's `meta.status` is `waiting`.
- English strings (`mindroomUi.workflowRun`): `title` "Workflow"; `status.starting` "Starting", `status.running` "Running", `status.waiting` "Waiting for you", `status.completed` "Done", `status.failed` "Failed", `status.cancelled` "Cancelled", `status.interrupted` "Interrupted"; `counts` "{{done}} done, {{running}} running, {{waiting}} waiting"; `reused` "Reused: {{count}}"; `cost` "{{spent}} so far, about {{projected}} in total"; `largeRun` "Large run"; `attempt` "Attempt {{number}}"; `stop` "Stop"; `stopping` "Stopping…"; `resume` "Resume"; `runAgain` "Run again"; `starting` "Starting…"; `failedCount_one` "{{count}} failed" and `failedCount_other` "{{count}} failed"; `costAtLeast` "at least {{spent}} so far"; `question.rejected.alreadyAnswered` "Already answered."; `question.rejected.noLongerOpen` "This question is no longer open."; `question.rejected.notAnOption` "That is not one of the options."; `question.rejected.notForYou` "Only the person who started the run can answer."; `question.noReply` "No reply yet. Try again."; `question.placeholder` "Type your answer"; `question.send` "Send"; `question.answered` "Answered: {{answer}}"; `question.cancelled` "Cancelled"; `question.onlyRequester` "Only {{name}} can answer."; and `mindroomUi.backgroundJobs.groupWorkflows` "Workflows · {{number}}".

- [ ] **Step 1: Write the failing tests**

```tsx
it('prefers the live run over the card it was posted with', () => {
  store.ingest(ROOM, AGENT, snapshot({ status: 'waiting', counts: { completed: 12, running: 8, waiting: 1 } }), now);
  render(<WorkflowRunCard card={card({ status: 'running' })} {...props} />);
  expect(screen.getByText('Waiting for you')).toBeTruthy();
  expect(screen.getByText('12 done, 8 running, 1 waiting')).toBeTruthy();
});
it('stops through the job cancel event and reads Stopping', async () => { /* sendEvent called with io.mindroom.background_job_cancel {job_id: JOB} */ });
it('offers Resume to the requester of an interrupted run only, and Run again on a finished one', () => { /* viewer != requester: neither */ });
it('shows why a run failed and how many calls failed in a finished one', () => { /* card.error line; "3 failed" */ });
it('shows no Stop to a viewer who may not cancel jobs, and none on a finished run', () => { /* ... */ });

it('lets only the requester answer', () => {
  render(<WorkflowQuestionCard question={question({ requester: '@other:example.org', options: ['Yes', 'No'] })} {...props} />);
  expect(screen.getByRole<HTMLButtonElement>('button', { name: 'Yes' }).disabled).toBe(true);
});
it('sends the chosen option and waits for the edit', async () => { /* io.mindroom.workflow_answer {run, node, option: 'Yes'}; buttons disabled after */ });
it('sends free text', async () => { /* {text: 'after lunch'} */ });
it('shows an answered question without controls', () => { /* "Answered: Yes", no buttons */ });
it('enables the card again with the reason when the answer is rejected', async () => {
  await user.click(screen.getByRole('button', { name: 'Yes' }));
  emitRejection({ answerEventId: SENT_ID, reason: 'already_answered' });
  expect(screen.getByText('Already answered.')).toBeTruthy();
  expect(screen.getByRole<HTMLButtonElement>('button', { name: 'Yes' }).disabled).toBe(false);
});
it('enables the card again after 15 seconds with no reply', async () => { /* fake timers: "No reply yet. Try again." */ });

it('lists a workflow run as one row of the workflow group beside ordinary sub-agents', () => {
  const model = presentBackgroundJobs([workflowJob(RUN), subagentJob()], no, t, 2, lookups);
  expect(model?.groups.map((group) => group.kind)).toEqual(['workflow', 'subagent']);
  expect(model?.items[0]).toMatchObject({ jobId: workflowJob(RUN).jobId, name: 'Audit the sample module' });
});
it('does not render a card for workflow content from a person', () => { /* fromMindroomAgent false: plain body */ });
```

- [ ] **Step 2: Run** `VITEST src/app/messages/workflow-run src/app/background-jobs/backgroundJobsPresentation.test.ts`. Expected: FAIL, the components and the `workflow` group do not exist.
- [ ] **Step 3: Implement.** Build the cards on the existing card primitives (the surfaces, pills and buttons `SecretRequestCard` and `QuestionCard` use), so focus order, right-to-left layout and contrast on every palette come with them.
- [ ] **Step 4: Run** the same command plus `src/app/background-jobs src/app/i18n/localeAdditions.test.ts src/app/messages/question`. Expected: PASS.
- [ ] **Step 5: End-to-end and screenshots.** The fixture page renders the run card (running, waiting, done, interrupted), both question cards, and a jobs bar with one workflow row, all from invented data.
  `workflow-run-card.spec.ts` opens it, checks the texts above and that Stop and an option button are reachable by keyboard, and saves the screenshots for the pull request (wide and phone width).
  Extend `jobs-bar-rich-rows.spec.ts`: PUT a `workflow` job and `io.mindroom.workflow` frames to the test homeserver, and expect one row with the run's title whose numbers follow a delta.
- [ ] **Step 6: Commit** `feat(workflow-run): run card, question card and jobs-bar row`. Then the checks of "shared rules", and against a throwaway backend at B7: start a run that asks a question, answer it in Chat, answer a second time from another account (the rejection shows), stop a run and resume it from the card. Sol and Astra review, open PR C1 with a `## Screenshots` section.

---

## PR C2: graph panel

### Task 27: Graph layout model (pure)

**Files:**
- Create: `src/app/subagents/workflow/workflowGraphModel.ts`
- Test: `src/app/subagents/workflow/workflowGraphModel.test.ts`

**Interfaces:**
- Consumes: `WorkflowRunGraph`, `WorkflowRunNode`, `WorkflowRunGroup`, `WorkflowRunEdge`, `WorkflowRunOmitted` (Task 25). No React, no Matrix, no DOM.
- Produces:
  - `WorkflowGraphItem = { type: 'node'; node: WorkflowRunNode } | { type: 'group'; group: WorkflowRunGroup; items: WorkflowGraphItem[] }`.
  - `WorkflowGraphBand = { phase: string | null; items: WorkflowGraphItem[] }` and `WorkflowGraphLayout = { bands: WorkflowGraphBand[]; omitted: WorkflowRunOmitted }`; 
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

### Task 28: Graph panel

**Files:**
- Create: `src/app/navigation/workflowPanel.ts`, `src/app/subagents/workflow/WorkflowPanel.tsx`, `WorkflowPanelPart.tsx`, `workflowPanel.css.ts`, `e2e/workflow-run/workflow-graph-panel.spec.ts`
- Modify: `src/app/room/Room.tsx`, `src/app/messages/workflow-run/WorkflowRunCard.tsx`, `src/app/background-jobs/BackgroundJobsBar.tsx`, `BackgroundJobRow.tsx`, `e2e/fixtures/workflow-run.tsx`, the 17 overlays
- Test: `src/app/navigation/workflowPanel.test.ts`, `src/app/subagents/workflow/WorkflowPanel.test.tsx`, `WorkflowRunCard.test.tsx`

**Interfaces:**
- Consumes: Tasks 25 to 27; `openSubagentsPanel` (`navigation/subagentsPanel.ts`); `useRoomSubagentRuns` (`messages/live/subagentRunStore.ts`); `useRoomBackgroundJobs` (`matrix/hooks/`); `cancelBackgroundJob`; `lazyPart`, `PartUnavailable`, `ResizablePanel`; the avatar and compact model label `SubagentRunCard` uses.
- Produces:
  - `navigation/workflowPanel.ts`: `workflowRunPanelAtom` (`{ roomId; run; threadId?: string | null } | undefined`, a new value on every opening), `openWorkflowRunPanel(store, roomId, run, threadId?)`, `closeWorkflowRunPanel(store, roomId)`, `useWorkflowRunPanel(roomId): { open: boolean; run?: string; close(): void }`.
  - `WorkflowPanel({ room, run, threadId, onClose })`, lazy through `WorkflowPanelPart` (the unavailable state with Retry and Close, as `SubagentsPanelPart`).
    Header: title, status pill, the counts and cost lines, elapsed time, Stop for a live run (the job is the room's `kind: 'workflow'` job whose `workflow` is this run) and Resume or Run again under the card's rule.
    Body: one `section` per band with the phase name as its heading; a group box with its kind ("For each", "Parallel", "Saved workflow"), "{{done}} of {{total}}", a progress bar and "Failed: {{count}}"; chips folded by `foldGroupItems` behind "Show {{count}} more"; then one line per non-zero `omitted` status ("and {{count}} more queued", "... running", "... waiting", "... failed", "... done") and, under them, "Ask {{agent}} for the rest." (the rows a frame left out are not paged in v1); then the log.
    Chip: a button with a status dot, the label, the agent's name, the model (the compact label of the joined sub-agent run, else the node's `model` name), tokens, cost and elapsed time; "Reused from the last run" for `cached`, "Queued", "May have run" for `indeterminate`, or the reason it waits.
    Details of the selected chip: label, status, error, `nodeResultPreview`, its message counts as text ("Messages to {{name}}: {{count}}"; message texts never reach the client), Open (`openSubagentsPanel(store, roomId, threadId, node.run)`, when `node.run` is set) and Stop agent (`cancelBackgroundJob` with `node.job`, while the node is live).
    Edges: one `aria-hidden` SVG over the body with a path per `shownEdges` entry; chip boxes are measured in a layout effect and by one `ResizeObserver`, and redrawn at most once per animation frame.
    With no entry for the run it shows "Waiting for the run's next update…"; with a `stale` entry it shows "Not updating. Last update {{time}}." above the last graph.
    Chips are memoized on their node row, which `applyWorkflowRunDelta` keeps for untouched rows, so a delta re-renders only the chips it names.
  - `Room.tsx`: a `PanelEntry` `id: 'workflow'` after `subagents` (`ResizablePanel` with `testId="resizable-workflow-panel"` and its own width key, `covers: mobile`), `store.sub(workflowRunPanelAtom, ...)` that requests the panel and shows the run's conversation as the subagents subscription does, and the panel in the "any panel open" expression.
    There is no header toggle: a room can have several runs, so the panel opens from a card or a jobs-bar row.
  - `WorkflowRunCard`: a Graph button when the store holds the run or the card carries `graph`; it calls `store.showCard(...)` for a card with `graph`, then `openWorkflowRunPanel`.
  - Jobs bar: a workflow row gets the existing `OpenButton`, which calls `openWorkflowRunPanel(store, roomId, run, item.threadId)`.
- English strings added under `mindroomUi.workflowRun`: `graph` "Graph"; `panel.close` "Close"; `panel.resize` "Resize workflow panel"; `panel.hide` "Hide workflow"; `panel.waitingForUpdate` "Waiting for the run's next update…"; `panel.stale` "Not updating. Last update {{time}}."; `panel.log` "Log"; `panel.showMore` "Show {{count}} more"; `panel.omitted.queued`, `.running`, `.waiting`, `.failed`, `.done` "and {{count}} more queued" and so on (each with `_one` and `_other`); `panel.askAgent` "Ask {{agent}} for the rest."; `node.mayHaveRun` "May have run"; `node.waitingOn.peer` "Waiting for a message"; `node.messages` "Messages"; `group.foreach` "For each"; `group.parallel` "Parallel"; `group.run` "Saved workflow"; `group.progress` "{{done}} of {{total}}"; `group.failed` "Failed: {{count}}"; `node.reused` "Reused from the last run"; `node.queued` "Queued"; `node.waitingOn.user` "Waiting for your answer"; `node.waitingOn.approval` "Waiting for approval"; `node.waitingOn.budget` "Waiting for the cost decision"; `node.result` "Result"; `node.error` "Error"; `node.messagesTo` "Messages to {{name}}: {{count}}"; `node.open` "Open"; `node.stop` "Stop agent".

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
it('renders 24 chips and Show more for a group of 300, and says what the frame left out', () => {
  /* 24 chip buttons; "Show 276 more"; omitted {queued: 412, failed: 30}: "and 412 more queued", "and 30 more failed" */ });
it('renders a counts-only snapshot as the header numbers and the omitted lines', () => { /* no bands; "and 1,000 more queued"; "Ask panels for the rest." */ });
it('shows message counts of the selected chip as text', () => { /* "Messages to frontend: 2"; no button, no control event sent */ });
it('stays responsive on a snapshot of 300 rows', () => { /* render time under 100 ms in jsdom; chips are memoized */ });
it('re-renders only the chip a delta names', () => { /* render counter on two chips; a delta for n4 leaves n5 at one render */ });
it('opens the Sub-agents panel on the chip\'s run', async () => { /* subagentsPanelAtom == {roomId, threadId: null, run: 'delegation-1'} */ });
it('stops one agent by its own job', async () => { /* cancel event {job_id: node.job}, not the run's job */ });
it('says why a chip waits and that an indeterminate call may have run', () => { /* 'user': "Waiting for your answer"; 'peer': "Waiting for a message"; "May have run" */ });
it('shows a finished run from its card with no ephemeral history', () => { /* a fresh store; store.showCard(...); the bands render, failed chips first */ });
it('says it waits for an update when it holds nothing', () => { /* "Waiting for the run's next update…" */ });
it('shows Graph on the card only when there is a graph to show', () => { /* no entry and no card.graph: no button */ });
```

- [ ] **Step 2: Run** `VITEST src/app/navigation/workflowPanel.test.ts src/app/subagents/workflow src/app/messages/workflow-run`. Expected: FAIL, the modules do not exist.
- [ ] **Step 3: Implement.** Use no graph or layout library; the layout is CSS (bands stack, a box wraps its chips in a grid) and the only computed geometry is `edgePath`.
- [ ] **Step 4: Run** the same command plus `src/app/background-jobs src/app/i18n/localeAdditions.test.ts src/test/layering.test.ts src/app/threads/__tests__/featureOwnership.architecture.test.ts`, then `npx vite build` and `just bundle-report`. Expected: PASS, and `WorkflowPanel` is its own chunk outside the initial JS.
- [ ] **Step 5: End-to-end and screenshots.** `workflow-graph-panel.spec.ts` posts a run card as the room's agent and PUTs `io.mindroom.workflow` frames to the homeserver's ephemeral endpoint, as `jobs-bar-rich-rows.spec.ts` does for subagent frames (if the endpoint refuses the new type, extend the test homeserver first and say so in the pull request).
  It checks: Graph opens the panel; a delta turns one chip to Done without a reload; after a page reload (no ephemeral history) the finished run's card still opens its graph; a chip's Open shows the Sub-agents panel on that run; at phone width the panel covers the conversation and Close returns; the panel frame passes `checkPanelFrame`.
  Screenshots from the fixture page with invented data: the panel wide, at phone width, with a selected chip, and with message edges.
- [ ] **Step 6: Commit** `feat(workflow-run): live graph panel`. Then the checks of "shared rules", Sol and Astra review, open PR C2 with a `## Screenshots` section.
- [ ] **Step 7: Live test of the whole feature** on a fresh throwaway deployment (`~/repos/dev/scripts/live.py up` with the backend at B9b and Chat at this branch, in an encrypted room). One script covers the scenarios: a fan-out of 8 agents of which one writes through the counting tool, one makes an approval-gated call and one asks the user, then a `shell` check and a synthesis.
  Run `tests/workflow_live/restart_mid_run.py` while the approval and the question are open; answer and approve in Chat; cancel; resume with an edited synthesis prompt.
  Expected: one counter line, one card per question and approval, one report, the edited call and nothing before it paid again.
  Then 1,000 queued synthetic agents at `max_concurrent=200` with the panel open: record frame sizes and long tasks (target: every frame accepted, no long task over 50 ms from a frame, the omitted counts add up to 1,000).
  Tear the deployment down and post the numbers on PR C2.
