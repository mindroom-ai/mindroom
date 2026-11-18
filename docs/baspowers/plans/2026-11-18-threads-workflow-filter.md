# Thread Workflow Filter for `matrix_room` Threads Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: implement this plan task-by-task with the skill named in **Execution**. Steps use checkbox (`- [ ]`) syntax.

**Execution:** baspowers:subagent-driven-development (five small tasks in two repositories; Task 4 is a privacy boundary and Task 3 reads approvals, so each gets its own reviewer).

**Goal:** `matrix_room(action="threads")` labels every thread with its workflow group (needs you, working, later, done), filters by it with `workflow=...`, and covers every room the requester shares with the agent with `room_id="all"`.

**Architecture:** A new pure module `src/mindroom/thread_workflow.py` ports MindRoom Chat's classification and reads its inputs from one room-state response; `matrix_room.py` adds running replies (the bots' own record) and pending approvals (the approval journal) and filters rows before it builds their previews.
One JSON case file, byte-identical in `mindroom` and `mindroom-chat`, runs against both implementations.

**Tech Stack:** Python 3.13, asyncio, matrix-nio, pytest; TypeScript and vitest for the client test.

**Spec:** `docs/dev/2026-11-18-threads-workflow-filter-design.md` (decision numbers below refer to it).

## Global Constraints

- Backend worktree `/home/basnijholt/repos/worktrees/mindroom-threads-workflow`, branch `feat/threads-workflow-filter`; client worktree `/home/basnijholt/repos/worktrees/mindroom-chat-thread-workflow-cases`, branch `test/thread-workflow-shared-cases`. Tasks 1-4 are backend, Task 5 is the client.
- No new tool and no new config option; one new parameter `workflow: Literal["needs_you", "working", "later", "all"] = "all"`.
- Row fields, exactly: `workflow` in `needs_you | working | later | done`; `why` in `approval | question | your_turn` on needs-you rows only; `back_at` (int, milliseconds) on later rows only when known; `room_id` on rows of an `all` call only. Absent keys are omitted, never `null`.
- All timestamps are integer milliseconds since the epoch; "snoozed" means `until > now`, strictly.
- Never return a thread of a room the requester is not a joined member of: every room of an `all` call passes `room_access_allowed(context, room_id, contact_room_ok=True)` before anything is read from it.
- Budget: least new code. Estimated backend product lines: Task 1 +40, Task 2 +70, Task 3 +40, Task 4 +40 (about 190; the design records that this is above the 100-150 asked for). Do not add helpers, options or fields beyond the Interfaces blocks.
- Fixtures and tests use placeholder data only (`$open`, `@user:localhost`, "Pick one"); no real thread titles or message text.
- Backend test command: `cd /home/basnijholt/repos/worktrees/mindroom-threads-workflow && NIX_PATH=nixpkgs=flake:nixpkgs nix-shell --run 'uv run pytest tests/<file>.py -x -n 0 --no-cov'`.
- Before each backend commit: `uv run tach check --dependencies --interfaces` and `uv run pre-commit run --all-files` inside the same nix-shell; `tach.toml` has `exact = true`, so its edits are part of each task.
- Commits carry `--trailer "Assisted-by: MindRoom <display name>:<model id> <https://github.com/mindroom-ai/mindroom>"` (or `$MINDROOM_ASSISTED_BY` if set). No push, no `git stash`, no reset.

## Review Focus

- A thread the person resolved instead of answering: an approval card or question older than the resolve must leave the row `done` (Task 2 `test_thread_workflow_counts_only_what_was_asked_after_the_resolve`, Task 3 tool test).
- `room_id="all"` with a room the agent is in and the requester is not: nothing is read from it and none of its rows appear (Task 4 `test_threads_all_rooms_skips_rooms_the_requester_is_not_in`).
- Room state written by someone else: a jobs event whose sender is not its state key or not a bot of this runtime, a snooze with an invalid `until`, `jobs` that is not a list; each is ignored without an exception (Task 2 `test_room_thread_facts_ignores_state_it_cannot_trust`).
- A filtered page with no match while older threads exist: `count == 0`, `has_more` true, `next_token` kept, so the agent can go on (Task 2 `test_threads_filtered_page_can_be_empty_and_still_have_more`).
- An encrypted room or a homeserver without `io.mindroom.latest_reply`: no question, the row is `your_turn`, no error (Task 3 `test_latest_reply_question_ts` case "no bundle").

---

### Task 1: Shared case file and the classification port

Net lines: product +40 (`thread_workflow.py`), `tach.toml` +4, tests +25, fixture about 290.

**Files:**
- Create: `tests/fixtures/thread-workflow-cases.json`
- Create: `src/mindroom/thread_workflow.py`
- Create: `tests/test_thread_workflow.py`
- Modify: `tach.toml` (declare the module)

**Interfaces:**
- Consumes: nothing.
- Produces: `classify_thread_workflow(*, now: int, is_resolved: bool = False, snoozed_until_ts: int | None = None, is_streaming: bool = False, has_background_job: bool = False, has_background_approval: bool = False, scheduled_task_count: int = 0, next_scheduled_ts: int | None = None, pending_approval: bool = False, pending_question: bool = False) -> dict[str, object]`.
  The case file's `status` keys are exactly these keyword names, so a case calls it as `classify_thread_workflow(now=case["now"], **case["status"])`.

- [ ] **Step 1: Write the case file**

Shape: `{"version": 1, "cases": [{"name": str, "now": 1000, "status": {...}, "expected": {...}}]}`.
A `status` holds only the keys that differ from the defaults; `expected` holds `workflow`, plus `why` or `back_at` when the row has it.
Format it with `python -m json.tool --indent 2` and end it with one newline; Task 5 copies these bytes.
Every case has `"now": 1000`.

| name | status | expected |
|---|---|---|
| idle | (empty) | needs_you, why your_turn |
| resolved | is_resolved | done |
| resolved beats snooze, work and schedule | is_resolved, snoozed_until_ts 2000, is_streaming, has_background_job, scheduled_task_count 1 | done |
| resolved hides a background approval | is_resolved, has_background_approval | done |
| approval asked after the resolve | is_resolved, pending_approval | needs_you, why approval |
| question asked after the resolve | is_resolved, pending_question | needs_you, why question |
| active snooze | snoozed_until_ts 2000 | later, back_at 2000 |
| snooze ending now | snoozed_until_ts 1000 | needs_you, why your_turn |
| expired snooze | snoozed_until_ts 999 | needs_you, why your_turn |
| snooze beats work and schedule | snoozed_until_ts 2000, is_streaming, has_background_job, scheduled_task_count 1, next_scheduled_ts 1500 | later, back_at 2000 |
| background approval beats snooze | snoozed_until_ts 2000, has_background_approval | needs_you, why approval |
| question beats snooze | snoozed_until_ts 2000, pending_question | needs_you, why question |
| approval named before question | pending_approval, pending_question | needs_you, why approval |
| approval beats work | pending_approval, is_streaming | needs_you, why approval |
| background approval beats work and schedule | has_background_approval, is_streaming, has_background_job, scheduled_task_count 1 | needs_you, why approval |
| question beats work | pending_question, has_background_job | needs_you, why question |
| streaming reply | is_streaming | working |
| background job | has_background_job | working |
| work beats schedule | is_streaming, scheduled_task_count 1, next_scheduled_ts 5000 | working |
| scheduled with a known time | scheduled_task_count 1, next_scheduled_ts 5000 | later, back_at 5000 |
| scheduled without a known time | scheduled_task_count 2 | later |
| expired snooze with a schedule | snoozed_until_ts 999, scheduled_task_count 1, next_scheduled_ts 5000 | later, back_at 5000 |

Booleans in the table are `true`; `(empty)` is `{}`.

- [ ] **Step 2: Write the failing test**

```python
_CASES = json.loads(
    (Path(__file__).parent / "fixtures" / "thread-workflow-cases.json").read_text(encoding="utf-8"),
)["cases"]


@pytest.mark.parametrize("case", _CASES, ids=lambda case: case["name"])
def test_shared_workflow_case(case: dict[str, Any]) -> None:
    """Each shared case classifies as MindRoom Chat classifies it."""
    assert classify_thread_workflow(now=case["now"], **case["status"]) == case["expected"]
```

- [ ] **Step 3: Run it to verify it fails**

Run: `cd /home/basnijholt/repos/worktrees/mindroom-threads-workflow && NIX_PATH=nixpkgs=flake:nixpkgs nix-shell --run 'uv run pytest tests/test_thread_workflow.py -x -n 0 --no-cov'`
Expected: FAIL at import (`No module named 'mindroom.thread_workflow'`).

- [ ] **Step 4: Implement `classify_thread_workflow` in `src/mindroom/thread_workflow.py`**

First match wins, in this order (design decision 3; the client's `threadGroups.ts:38` then `threadWorkflow.ts:42-52`):

1. `pending_approval`, or `has_background_approval` and not `is_resolved`: `{"workflow": "needs_you", "why": "approval"}`.
2. `pending_question`: `{"workflow": "needs_you", "why": "question"}`.
3. `is_resolved`: `{"workflow": "done"}`.
4. `snoozed_until_ts` is set and `> now`: `{"workflow": "later", "back_at": snoozed_until_ts}`.
5. `is_streaming` or `has_background_job`: `{"workflow": "working"}`.
6. `scheduled_task_count > 0`: `{"workflow": "later"}`, plus `"back_at": next_scheduled_ts` when it is not `None`.
7. Otherwise `{"workflow": "needs_you", "why": "your_turn"}`.

The module docstring names the client files it ports and the case file both repositories run.

- [ ] **Step 5: Declare the module in `tach.toml`**

Add `[[modules]]` with `path = "mindroom.thread_workflow"` and `depends_on = []`.

- [ ] **Step 6: Run the test to verify it passes**

Run: the Step 3 command.
Expected: 22 passed.

- [ ] **Step 7: Commit**

```bash
git add tests/fixtures/thread-workflow-cases.json src/mindroom/thread_workflow.py tests/test_thread_workflow.py tach.toml
git commit -m "Classify a thread's workflow group as MindRoom Chat does"
```

---

### Task 2: Room-state inputs, running replies and the `workflow` parameter

Net lines: product +70 (`thread_workflow.py` +38, `bot.py` +4, `matrix_room.py` +28), `tach.toml` +4, docs +3, tests +150.

**Files:**
- Modify: `src/mindroom/thread_workflow.py`
- Modify: `src/mindroom/bot.py:1012-1014` (new method beside `has_active_response_for_target`)
- Modify: `src/mindroom/custom_tools/matrix_room.py:174-211` (`_dispatch_action`), `:315-370` (`_threads`), `:460-521` (`matrix_room` and its docstring)
- Modify: `tach.toml`, `docs/tools/matrix-and-attachments.md:44,52`
- Test: `tests/test_thread_workflow.py`, `tests/test_matrix_room_tool.py`

**Interfaces:**
- Consumes: `classify_thread_workflow` (Task 1); `task_records_with_writers(room_id, state_response, runtime_paths)` (`scheduling/tasks.py:531`); `parse_thread_tag_state_key`, `parse_thread_tag_record`, `snooze_until`, `THREAD_TAGS_EVENT_TYPE`, `RESOLVED_THREAD_TAG`, `SNOOZED_THREAD_TAG` (`thread_tags.py`); `BACKGROUND_JOBS_EVENT_TYPE` (`matrix/event_types.py`); `persisted_bot_user_ids(runtime_paths)` (`entity_resolution.py:398`); `ResponseRunner.active_thread_ids_for_room` (`response_runner.py:475`).
- Produces:
  - `room_thread_facts(room_id: str, state: nio.RoomGetStateResponse, runtime_paths: RuntimePaths, bot_ids: Collection[str], now: int) -> dict[str, dict[str, Any]]`: per thread root ID, only the keys that hold: `resolved_ts: int` (the resolved tag's `set_at`), `snoozed_until_ts: int`, `runs: list[int | None]` (one entry per counted scheduled task: its `execute_at`, `None` for a recurring one), `has_background_job: True`, `has_background_approval: True`.
  - `thread_workflow(facts: Mapping[str, Any], *, now: int, replying: bool, approval_ts: int | None = None, question_ts: int | None = None) -> dict[str, object]`: the label of one thread from its `room_thread_facts` entry (`{}` when it has none).
  - `AgentBot.active_thread_ids_for_room(self, room_id: str) -> frozenset[str | None]`.
  - `MatrixRoomTools._room_threads(self, context, *, room_id: str, limit: int, page_token: str | None, workflow: str) -> tuple[list[dict[str, object]], str | None]`: one room's labelled rows of the asked group and the next page token; raises `RoomThreadsPageError` for a failed `/threads` page or a state read the homeserver refuses, and lets `ClientError` and `TimeoutError` through.

- [ ] **Step 1: Write the failing unit tests in `tests/test_thread_workflow.py`**

Build the state with `nio.RoomGetStateResponse(events=[...], room_id="!test:server")`, the runtime with `schedule_runtime_paths(tmp_path)` and task events with `scheduled_task_state_event` (both in `tests/scheduling_helpers.py`; `SCHEDULE_WRITER_ID` is the trusted bot).
A task event's content is `{"workflow": ScheduledWorkflow(schedule_type="once", execute_at=..., message="m", description="d", created_by="@user:server", thread_id=...).model_dump_json(), "status": "pending"}`; a recurring one has `schedule_type="cron"` and `cron_schedule=CronSchedule(minute="0")`.
A tag event is `{"type": THREAD_TAGS_EVENT_TYPE, "state_key": json.dumps([thread_id, tag], separators=(",", ":")), "sender": "@user:server", "content": {"set_by": "@user:server", "set_at": iso, "data": {...}}}`.
A jobs event is `{"type": BACKGROUND_JOBS_EVENT_TYPE, "state_key": sender, "sender": sender, "content": {"jobs": [{"job_id": "j1", "thread_id": ..., "status": ...}], "total": 1}}`.

- `test_room_thread_facts_reads_tags_tasks_and_jobs`: with `now` = 2026-06-01 in ms, a `resolved` tag set at 2026-01-01 on `$resolved`; a `snoozed` tag until 2999-01-01 on `$snoozed`; on `$later` a pending once task at 2999-01-01 and a pending cron task; a jobs event from `SCHEDULE_WRITER_ID` listing `$job` as `running`, `$approval` as `awaiting_approval` and one job with `thread_id` `None`. Assert the result equals `{"$resolved": {"resolved_ts": <ms>}, "$snoozed": {"snoozed_until_ts": <ms>}, "$later": {"runs": [<ms>, None]}, "$job": {"has_background_job": True}, "$approval": {"has_background_approval": True}}`.
- `test_room_thread_facts_ignores_state_it_cannot_trust`: a tag whose content is `{}`; a `snoozed` tag with `"until": "soon"`; a once task at 2000-01-01; a pending task with `new_thread=True`; a task with `"status": "cancelled"`; a task event sent by `@mallory:server`; a jobs event sent by `@mallory:server`; a jobs event from the bot under another user's state key; a jobs event whose `jobs` is `"none"`. Assert the result is `{}`.
- `test_thread_workflow_counts_only_what_was_asked_after_the_resolve`, parametrized, `now=10_000`:
  - `{"resolved_ts": 5000}`, `approval_ts=4000` gives `{"workflow": "done"}`; `approval_ts=6000` gives needs_you with `approval`.
  - `{"resolved_ts": 5000}`, `question_ts=5000` gives done; `question_ts=6000` gives needs_you with `question`.
  - `{}`, `question_ts=10_000 - 7 * 24 * 3600 * 1000` gives needs_you with `your_turn`; one millisecond later gives `question`.
  - `{"runs": [20_000, 30_000]}` gives later with `back_at` 20_000; `{"runs": [20_000, None]}` gives `{"workflow": "later"}`.
  - `{"has_background_job": True}` and, separately, `replying=True` give working; `{"has_background_approval": True, "snoozed_until_ts": 20_000}` gives needs_you with `approval`.

- [ ] **Step 2: Write the failing tool tests in `tests/test_matrix_room_tool.py`**

In `_make_context`, serve an empty state by default: `client.room_get_state = AsyncMock(return_value=nio.RoomGetStateResponse(events=[], room_id=room_id))`, so the existing thread tests keep passing.
Roots come from `_thread_event`; give the orchestrator as `replace(ctx, orchestrator=MagicMock(agent_bots={"general": MagicMock(active_thread_ids_for_room=MagicMock(return_value=frozenset({"$busy"})))}))`.

- `test_threads_rows_carry_their_workflow_group`: roots `$open`, `$resolved` (resolved tag), `$snoozed` (until 2999-01-01), `$busy`; the rows are, in order, `workflow` needs_you with `why` your_turn, done, later with `back_at` equal to the snooze end in ms, working; `why` and `back_at` are absent from the rows that have none. `ctx.client.room_get_state` was awaited once, with `"!room:localhost"`.
- `test_threads_workflow_filter_keeps_only_the_asked_group`, parametrized over `needs_you -> ["$open"]`, `working -> ["$busy"]`, `later -> ["$snoozed"]`, `all -> all four`; with `thread_root_body_preview` patched, its await count equals the number of rows returned.
- `test_threads_filtered_page_can_be_empty_and_still_have_more`: every root resolved, the page's token `"tok"`, `workflow="needs_you"`: `count == 0`, `threads == []`, `next_token == "tok"`, `has_more is True`.
- `test_threads_state_read_failure_is_an_error`, parametrized: `room_get_state` returns `nio.RoomGetStateError(message="boom")`, or raises `ClientError("down")`; `status == "error"`, `action == "threads"`, and no `threads` key.
- `test_matrix_room_schema_exposes_workflow_values`: next to `test_matrix_room_schema_exposes_supported_actions` (`:122`), the `workflow` enum is `["needs_you", "working", "later", "all"]`; `workflow="done"` is rejected as the malformed-argument test (`:160`) rejects its cases.

- [ ] **Step 3: Run both files to verify the new tests fail**

Run: `cd /home/basnijholt/repos/worktrees/mindroom-threads-workflow && NIX_PATH=nixpkgs=flake:nixpkgs nix-shell --run 'uv run pytest tests/test_thread_workflow.py -x -n 0 --no-cov'`, then the same with `tests/test_matrix_room_tool.py`.
Expected: FAIL (`cannot import name 'room_thread_facts'`; unexpected keyword `workflow`).

- [ ] **Step 4: Implement `room_thread_facts` and `thread_workflow`**

`room_thread_facts`, one pass over `state.events` plus one call of `task_records_with_writers`:

- A thread-tag event counts for the tags `resolved` and `snoozed` only, when `parse_thread_tag_record` returns a record; `snoozed_until_ts` comes from `snooze_until(record)` and is left out when that is `None`.
- A scheduled task counts when `task.workflow.thread_id` is set, `new_thread` is false, and `execute_at` is `None` or later than `now` (the client's `threadScheduledStatus.ts:28-36`); `task_records_with_writers` already keeps only pending tasks written by this runtime's bots.
- A jobs event counts when `state_key == sender` and the sender is in `bot_ids`; each listed job with a string `thread_id` sets `has_background_approval` when its `status` is `"awaiting_approval"`, else `has_background_job`.
- Read every field with `.get` and type checks: a test state may lack fields the real response guarantees.

`thread_workflow` calls `classify_thread_workflow` with: `is_resolved` = `resolved_ts` present; `is_streaming` = `replying`; `scheduled_task_count` = `len(runs)`; `next_scheduled_ts` = `min(runs)` when `runs` is non-empty and holds no `None`; `pending_approval` = `approval_ts` is set and, when resolved, `> resolved_ts`; `pending_question` = the same test for `question_ts`, and `now - question_ts < 7 * 24 * 60 * 60 * 1000` (the client's `QUESTION_WAITS_MS`).

- [ ] **Step 5: Add `AgentBot.active_thread_ids_for_room`**

One delegating method beside `has_active_response_for_target`: return `self._response_runner.active_thread_ids_for_room(room_id)`.

- [ ] **Step 6: Wire `workflow` through `matrix_room.py`**

- `matrix_room` gains `workflow: Literal["needs_you", "working", "later", "all"] = "all"` after `include_summaries`, passed through `_dispatch_action` to `_threads`.
- Move the page fetch and the row loop of `_threads` into `_room_threads`. It reads the page, then `context.client.room_get_state(room_id)` once (a response that is not `nio.RoomGetStateResponse` raises `RoomThreadsPageError(response=f"Failed to fetch room state: {response}")`), then per root: label with `thread_workflow(facts.get(event.event_id, {}), now=now, replying=event.event_id in replying)`, skip the root unless `workflow` is `"all"` or equals the label's `workflow`, and only then call `_serialize_thread_root` and merge the label into the row.
- A root whose `source` is not a dict is labelled from `{}` facts and an empty bundle; `_serialize_thread_root` still drops it.
- `now = int(time.time() * 1000)`; `bot_ids = persisted_bot_user_ids(context.runtime_paths)`; `replying` is the union of `bot.active_thread_ids_for_room(room_id)` over `context.orchestrator.agent_bots.values()`, empty when `context.orchestrator` is `None`.
- `_threads` keeps its `RoomThreadsPageError` payload and adds the `(ClientError, TimeoutError)` payload the other actions use (`_transport_error_message`); summaries and the `ok` payload are unchanged.
- Docstring, at most four added lines: `workflow` filters threads to one group; each row has `workflow` (needs_you, working, later, done), needs_you rows `why` (approval or question: an agent waits for the requester; your_turn: open and idle), later rows `back_at` (ms) when known; a filtered page can hold fewer rows than `limit`.

- [ ] **Step 7: Update `tach.toml` and the tool page**

- `mindroom.thread_workflow`: `depends_on = ["mindroom.scheduling.tasks", "mindroom.thread_tags"]`.
- `mindroom.custom_tools.matrix_room` (`tach.toml:2675`): add `mindroom.entity_resolution` and `mindroom.thread_workflow`.
- `uv run tach check --dependencies --interfaces` is the authority; add what it names and nothing else.
- `docs/tools/matrix-and-attachments.md:44,52`: add `workflow="all"` to the signature and one sentence to the `threads` row on the groups, `why` and `back_at`.

- [ ] **Step 8: Run both test files to verify they pass**

Run: the two Step 3 commands.
Expected: PASS, the existing tests of both files included.

- [ ] **Step 9: Commit**

```bash
git add src/mindroom/thread_workflow.py src/mindroom/bot.py src/mindroom/custom_tools/matrix_room.py tach.toml docs/tools/matrix-and-attachments.md tests/test_thread_workflow.py tests/test_matrix_room_tool.py
git commit -m "Label and filter matrix_room threads by workflow group"
```

---

### Task 3: Pending approvals and pending questions

Net lines: product +40 (`approval/manager.py` +16, `thread_workflow.py` +15, `matrix_room.py` +9), `tach.toml` +5, tests +130.

**Files:**
- Modify: `src/mindroom/approval/manager.py:980-1005` (new method after `pending_card_event_ids`)
- Modify: `src/mindroom/thread_workflow.py`
- Modify: `src/mindroom/custom_tools/matrix_room.py` (`_room_threads`)
- Modify: `tach.toml`
- Test: `tests/test_auto_approve.py`, `tests/test_thread_workflow.py`, `tests/test_matrix_room_tool.py`

**Interfaces:**
- Consumes: `thread_workflow(..., approval_ts=, question_ts=)` and `_room_threads` (Task 2); `ApprovalManager._pending_cards(cards, room_id)` (`manager.py:961`); `get_approval_store()` (`manager.py:1221`); `equivalent_requester_ids(user_id, config, runtime_paths)` (`requester_identity.py:148`); `bundled_replacement_candidates(event_source)` (`matrix/client_visible_messages.py:263`); `interactive_prompt_from_content(content)` (`interactive_models.py:47`).
- Produces:
  - `ApprovalManager.pending_approval_threads(self, *, room_id: str, approver_user_ids: Collection[str]) -> dict[str | None, int]`: per conversation of the room (a thread root ID, `None` for the room timeline), the creation time in ms (`created_at_ns // 1_000_000`) of its newest card that waits for one of the approvers.
  - `latest_reply_question_ts(thread_metadata: Mapping[str, Any], bot_ids: Collection[str]) -> int | None`: when the thread's bundled newest reply asked a question, else `None`. `thread_metadata` is the root's `m.thread` bundle, as `MatrixRoomTools._thread_metadata` returns it.

- [ ] **Step 1: Write the failing manager test in `tests/test_auto_approve.py`**

Beside `test_pending_card_listing_keeps_only_the_approvers_open_cards_in_one_thread` (`:419`), with the same `_stored` helper, `cards` mock and `approval_manager_for_test`:

`test_pending_approval_threads_maps_each_conversation_to_its_newest_waiting_card`: one page of cards:
`_stored("$a1", created_at_ns=5_000_000)`, `_stored("$a2", created_at_ns=7_000_000)`, `_stored("$b", created_at_ns=6_000_000, thread_id="$other")`, `_stored("$scheduled", created_at_ns=8_000_000, thread_id="$sched", owner_kind="scheduled")`, `_stored("$deny-only", created_at_ns=9_000_000, thread_id="$large", approvable=False)`, `_stored("$alias", created_at_ns=10_000_000, thread_id="$alias-thread", approver="@alias:localhost")`, and, each on its own thread, cards that must not count: another approver, `decided=True`, `card_event_id` `None`, `owner_kind=None`, `expires_in=timedelta(seconds=-1)`.
Assert `await manager.pending_approval_threads(room_id=ROOM_ID, approver_user_ids={HUMAN, "@alias:localhost"})` equals `{THREAD_ID: 7, "$other": 6, "$sched": 8, "$large": 9, "$alias-thread": 10}`.

- [ ] **Step 2: Write the failing unit test in `tests/test_thread_workflow.py`**

`test_latest_reply_question_ts`, parametrized; `BOT = "@mindroom_general:localhost"`, `bot_ids = {BOT}`, the prompt is `interactive_prompt_content(InteractivePrompt(creator_agent="general", question_text="Pick one", options={"1": "a"}, option_labels={"1": "A"}, source_event_id="$src"))`:

- "question in the reply": `{"io.mindroom.latest_reply": {"sender": BOT, "origin_server_ts": 100, "content": {"body": "x", **prompt}}}` gives `100`.
- "question added by the newest edit": the reply's content has no prompt; `unsigned["m.relations"]["m.replace"]` is `{"sender": BOT, "origin_server_ts": 200, "content": {"m.new_content": {"body": "x", **prompt}}}`; gives `200`.
- "edit keeps the original's question": the reply's content has the prompt, the edit's `m.new_content` has none; gives the edit's `200`.
- "someone else's edit": the same edit with `"sender": "@mallory:localhost"` on a reply without a prompt gives `None`.
- "reply from a person": `"sender": "@user:localhost"` with the prompt gives `None`.
- "prompt without options": `{"io.mindroom.interactive": {"question_text": "Pick one"}}` gives `None`.
- "no bundle": `{"count": 3}` gives `None`.

- [ ] **Step 3: Write the failing tool tests in `tests/test_matrix_room_tool.py`**

Extend `_thread_event` with `latest_reply: dict[str, object] | None = None`, put under the `m.thread` bundle as `io.mindroom.latest_reply`.

- `test_threads_row_waits_on_an_approval`: `get_approval_store` patched at `mindroom.custom_tools.matrix_room.get_approval_store` to return a mock whose `pending_approval_threads` is `AsyncMock(return_value={"$t1": 5000})`; root `$t1` is needs_you with `approval`, also when a snooze until 2999 is in the state; with a `resolved` tag whose `set_at` is 2026-01-01 the row is `done`; the mock was awaited with `room_id="!room:localhost"` and `approver_user_ids` containing `"@user:localhost"`.
- `test_threads_row_waits_on_a_question_only_for_the_thread_starter`: a root sent by `@user:localhost` whose `latest_reply` is the agent's prompt, with `origin_server_ts` of now, is needs_you with `question`; the same bundle on a root sent by `@alice:localhost` is `your_turn`; persist the bot account so it is in `persisted_bot_user_ids` (`persist_schedule_writer(ctx.runtime_paths, "@mindroom_general:localhost", entity_name="general")`).
- `test_threads_without_an_approval_store_still_lists`: `get_approval_store` returns `None`; the rows are labelled and `status == "ok"`.

- [ ] **Step 4: Run the three files to verify the new tests fail**

Run: `cd /home/basnijholt/repos/worktrees/mindroom-threads-workflow && NIX_PATH=nixpkgs=flake:nixpkgs nix-shell --run 'uv run pytest tests/test_auto_approve.py -x -n 0 --no-cov'`, then the same with `tests/test_thread_workflow.py` and `tests/test_matrix_room_tool.py`.
Expected: FAIL (`has no attribute 'pending_approval_threads'`; `cannot import name 'latest_reply_question_ts'`; rows say `your_turn`).

- [ ] **Step 5: Implement `ApprovalManager.pending_approval_threads`**

Iterate `self._pending_cards(self.cards, room_id)` (return `{}` when `self.cards` is `None`).
A card counts when `card_event_id` is not `None`, it is not `decided`, `owner_kind` is not `None`, `approver_user_id` is in `approver_user_ids`, and `expires_at_ns` is set and later than `time.time_ns()`.
Unlike `pending_card_event_ids`, a `scheduled` card and a card that is not `approvable` count (design decision 7).
Cards arrive oldest first, so the last one of a thread sets its value.

- [ ] **Step 6: Implement `latest_reply_question_ts`**

- `reply = thread_metadata.get("io.mindroom.latest_reply")`; return `None` unless it is a dict whose `sender` is in `bot_ids`.
- The edit is the first of `bundled_replacement_candidates(reply)` whose `sender` equals the reply's; the shown content is its `content["m.new_content"]`, else the reply's `content`.
- It asks a question when `interactive_prompt_from_content` returns a prompt for the shown content or, failing that, for the reply's own content (the client's `readInteractiveQuestion(content, originalContent)`).
- Return the edit's `origin_server_ts` when there is an edit, else the reply's; `None` when that is not an `int`.

- [ ] **Step 7: Use both in `_room_threads`**

- `requester_ids = equivalent_requester_ids(context.requester_id, context.config, context.runtime_paths)`.
- `approvals = await manager.pending_approval_threads(room_id=room_id, approver_user_ids=requester_ids)` when `get_approval_store()` returns a manager, else `{}`; once per room, before the row loop.
- Per root: `approval_ts=approvals.get(event.event_id)`, and `question_ts=latest_reply_question_ts(self._thread_metadata(event.source), bot_ids)` only when `event.sender` is in `requester_ids` (design decision 8), else `None`.

- [ ] **Step 8: Update `tach.toml`**

- `mindroom.thread_workflow` `depends_on`: add `mindroom.interactive_models` and `mindroom.matrix.client_visible_messages`; add `mindroom.thread_workflow` to the `visibility` of both (`tach.toml:601`, `:4578`).
- Add `bundled_replacement_candidates` to the `expose` list of the `mindroom.matrix.client_visible_messages` interface (`tach.toml:7125-7137`).
- `mindroom.custom_tools.matrix_room` `depends_on`: add `mindroom.approval.manager`; add `mindroom.custom_tools.matrix_room` to that module's `visibility` (`tach.toml:1300`).
- Gate: `uv run tach check --dependencies --interfaces`, and `tests/test_tach_visibility.py` in the pre-commit run.

- [ ] **Step 9: Run the three files to verify they pass**

Run: the three Step 4 commands.
Expected: PASS.

- [ ] **Step 10: Commit**

```bash
git add src/mindroom/approval/manager.py src/mindroom/thread_workflow.py src/mindroom/custom_tools/matrix_room.py tach.toml tests/test_auto_approve.py tests/test_thread_workflow.py tests/test_matrix_room_tool.py
git commit -m "Count pending approvals and questions as threads that need the requester"
```

---

### Task 4: `room_id="all"`

Net lines: product +40 (`matrix_room.py`), docs +3, tests +140.

**Files:**
- Modify: `src/mindroom/custom_tools/matrix_room.py` (`matrix_room`, new `_all_rooms_threads`, docstring)
- Modify: `docs/tools/matrix-and-attachments.md:44-52`
- Test: `tests/test_matrix_room_tool.py`

**Interfaces:**
- Consumes: `_room_threads` (Tasks 2-3); `room_access_allowed(context, room_id, contact_room_ok=True)` (`attachment_helpers.py:46`); `_add_current_summaries`; `_check_rate_limit`; `_SUMMARY_READ_CONCURRENCY`.
- Produces: `MatrixRoomTools._all_rooms_threads(self, context, *, limit: int, include_summaries: bool, workflow: str) -> str`: the `threads` payload with `room_id="all"`.

- [ ] **Step 1: Write the failing tests**

Set `ctx.client.rooms = {"!room:localhost": MagicMock(), "!shared:localhost": MagicMock(), "!private:localhost": MagicMock()}` (the current room is `!room:localhost`).
Membership is real: `ctx.client.joined_members = AsyncMock(side_effect=...)` returns, per room, a `MagicMock(spec=nio.JoinedMembersResponse)` whose `members` hold `MagicMock(user_id=...)`; `!shared:localhost` has `@user:localhost`, `!private:localhost` has only the agent.
`get_room_threads_page` (`_MOCK_TARGET`) and `ctx.client.room_get_state` answer per room through `side_effect` functions keyed by the room ID argument.

- `test_threads_all_rooms_skips_rooms_the_requester_is_not_in`: each room has one root; the payload has `status == "ok"`, `room_id == "all"`, rows only from `!room:localhost` and `!shared:localhost`, each with its `room_id`; neither `get_room_threads_page` nor `room_get_state` was called with `!private:localhost`.
- `test_threads_all_rooms_treats_an_unreadable_membership_as_not_shared`: `joined_members` for `!shared:localhost` returns `nio.JoinedMembersError(message="boom")`; no row from it, and it is not in `failed_room_ids`.
- `test_threads_all_rooms_puts_waiting_rows_first_then_newest`: `!room:localhost` has an idle root with `latest_activity_ts` 9000, `!shared:localhost` an idle root at 5000 and a root with a pending approval at 1000 (approval store mock as in Task 3); `workflow="needs_you"` returns the approval row, then 9000, then 5000; with `limit=2` it returns the first two and `has_more is True`.
- `test_threads_all_rooms_reports_more_when_a_room_has_older_threads`: one room's page returns the token `"tok"`; `has_more is True` and `next_token is None`.
- `test_threads_all_rooms_names_a_room_it_could_not_read`: `get_room_threads_page` raises `RoomThreadsPageError(response="boom")` for `!shared:localhost`; `status == "ok"`, the other room's rows are there, `failed_room_ids == ["!shared:localhost"]`; with no failure the key is absent.
- `test_threads_all_rooms_refuses_a_page_token`: `page_token="tok"` gives `status == "error"` and a message that names `page_token`; no room was read.
- `test_threads_all_rooms_reads_summaries_only_for_returned_rows`: three matching rows, `limit=2`, `include_summaries=True`; `current_thread_summary` (patched at `mindroom.custom_tools.matrix_room.current_thread_summary`) was awaited twice, with the room ID of each returned row.
- `test_all_is_a_room_only_for_threads`: `matrix_room(action="members", room_id="all")` gives `status == "error"`.
- `test_threads_all_rooms_counts_one_rate_limited_action`: after `_RATE_LIMIT_MAX_ACTIONS` calls of `room_id="all"`, the next one is refused, as `test_matrix_room_rate_limit_guardrail` (`:210`) asserts for one room.

- [ ] **Step 2: Run the file to verify the new tests fail**

Run: `cd /home/basnijholt/repos/worktrees/mindroom-threads-workflow && NIX_PATH=nixpkgs=flake:nixpkgs nix-shell --run 'uv run pytest tests/test_matrix_room_tool.py -x -n 0 --no-cov'`
Expected: FAIL (`Unknown room 'all'`).

- [ ] **Step 3: Implement the `all` branch**

In `matrix_room`, before `resolve_requested_room_id`: when `action == "threads"` and `(room_id or "").strip() == "all"`, check the rate limit against `context.room_id`, refuse a non-empty `page_token` with an error payload (`"page_token cannot be used with room_id='all'; name one room to page through it."`), and return `await self._all_rooms_threads(...)`.
Every other action with `room_id="all"` falls through to the existing unknown-room error.

`_all_rooms_threads`:

- The candidates are `list(context.client.rooms)`.
- Per room, under `asyncio.Semaphore(self._SUMMARY_READ_CONCURRENCY)`: first `room_access_allowed(context, room_id, contact_room_ok=True)`; a room that fails it contributes nothing and is read no further. Then `_room_threads(context, room_id=room_id, limit=limit, page_token=None, workflow=workflow)`; add `room_id` to each row.
- A room whose access check or read raises `RoomThreadsPageError`, `ClientError` or `TimeoutError` is a failed room; the others are still returned.
- Merge the rows and sort by `(row.get("why") not in ("approval", "question"), -activity)`, where `activity` is `latest_activity_ts`, else `timestamp`; keep the first `limit`.
- With `include_summaries`, call `_add_current_summaries` once per room of the kept rows, with that room's kept rows.
- Payload: `action="threads"`, `room_id="all"`, `count`, `threads`, `next_token=None`, `has_more` true when rows were cut or any room returned a next token, and `failed_room_ids` only when not empty.

- [ ] **Step 4: Finish the docstring and the tool page**

Docstring, at most three added lines: `room_id="all"` (threads only) covers every room the requester shares with you; rows carry `room_id`; it reads one page per room, so name a room and use `page_token` to go further.
`docs/tools/matrix-and-attachments.md`: one sentence on `room_id="all"` in the `threads` row, saying that only rooms the requester is in are listed.

- [ ] **Step 5: Run the file to verify it passes**

Run: the Step 2 command.
Expected: PASS.

- [ ] **Step 6: Commit**

```bash
git add src/mindroom/custom_tools/matrix_room.py docs/tools/matrix-and-attachments.md tests/test_matrix_room_tool.py
git commit -m "List threads across every room the requester shares with the agent"
```

---

### Task 5: The same cases in MindRoom Chat (separate pull request)

Net lines: product 0, fixture about 290 (a copy), tests +45.
Runs in `/home/basnijholt/repos/worktrees/mindroom-chat-thread-workflow-cases` on `test/thread-workflow-shared-cases`; depends only on Task 1's case file.

**Files:**
- Create: `src/app/event-store/threads/fixtures/thread-workflow-cases.json` (a byte copy)
- Modify: `src/app/event-store/threads/threadWorkflow.test.ts`

**Interfaces:**
- Consumes: Task 1's `tests/fixtures/thread-workflow-cases.json`; `isWaitingOnYou`, `classifyThreadWorkflow`, `threadReturnTs`, `ThreadWorkflowStatus` (`src/app/event-store/threads/threadWorkflow.ts`).
- Produces: nothing.

- [ ] **Step 1: Copy the case file and check it is identical**

```bash
cp /home/basnijholt/repos/worktrees/mindroom-threads-workflow/tests/fixtures/thread-workflow-cases.json \
   /home/basnijholt/repos/worktrees/mindroom-chat-thread-workflow-cases/src/app/event-store/threads/fixtures/thread-workflow-cases.json
cmp /home/basnijholt/repos/worktrees/mindroom-threads-workflow/tests/fixtures/thread-workflow-cases.json \
    /home/basnijholt/repos/worktrees/mindroom-chat-thread-workflow-cases/src/app/event-store/threads/fixtures/thread-workflow-cases.json
```

Expected: `cmp` prints nothing. Do not reformat the copy.

- [ ] **Step 2: Write the test in `threadWorkflow.test.ts`**

Add `describe('the workflow cases shared with the backend', ...)` with `it.each(cases)('$name', ...)`, where `cases` is the fixture's `cases` typed as:

```ts
type SharedCase = {
  name: string;
  now: number;
  status: {
    is_resolved?: boolean;
    snoozed_until_ts?: number;
    is_streaming?: boolean;
    has_background_job?: boolean;
    has_background_approval?: boolean;
    scheduled_task_count?: number;
    next_scheduled_ts?: number;
    pending_approval?: boolean;
    pending_question?: boolean;
  };
  expected: { workflow: string; why?: string; back_at?: number };
};
```

For each case, build the client's `ThreadWorkflowStatus` from `status` (absent booleans are `false`, an absent count is `0`; set an optional key only when the case has it, as the strict flags require) and assert:

- `waits = isWaitingOnYou({ pendingApproval: status.pending_approval, pendingQuestion: status.pending_question }, { isResolved, threadRecord: { status: clientStatus } })`.
- `workflow` is `'needs_you'` when `waits`, else `classifyThreadWorkflow(clientStatus, now)` mapped by `{ needsYou: 'needs_you', working: 'working', later: 'later', done: 'done' }`; it equals `expected.workflow`. This is the combination of `threadGroups.ts:38-50`.
- `why`: `undefined` unless `workflow` is `'needs_you'`; then `'your_turn'` when not `waits`, `'approval'` when `pending_approval` or (`has_background_approval` and not `is_resolved`), else `'question'`; it equals `expected.why`.
- `back_at`: when `workflow` is `'later'`, `threadReturnTs({ snoozedUntilTs, nextScheduledTs }, now)` equals `expected.back_at ?? Infinity`; otherwise `expected.back_at` is `undefined`.

Also assert `cases.length` is 22, so a truncated copy fails.

- [ ] **Step 3: Run the test**

Run: `cd /home/basnijholt/repos/worktrees/mindroom-chat-thread-workflow-cases && nix develop --command just test src/app/event-store/threads/threadWorkflow.test.ts` (run `nix develop --command npm ci` first when `node_modules` is missing).
Expected: PASS, 22 shared cases beside the file's existing tests.
The test passes on first run because the client already implements the rules; prove it can fail by changing one `expected.workflow` in the copy, seeing the case fail, and restoring the file with the Step 1 `cp` and `cmp`.

- [ ] **Step 4: Check types and lint for the test**

Run: `nix develop --command just strict` and `nix develop --command just lint` in the client worktree.
Expected: 0 errors.

- [ ] **Step 5: Commit**

```bash
git add src/app/event-store/threads/fixtures/thread-workflow-cases.json src/app/event-store/threads/threadWorkflow.test.ts
git commit -m "Run the workflow cases shared with the backend"
```

The pull request description names the backend pull request and states that the fixture is a byte copy of its `tests/fixtures/thread-workflow-cases.json`.
