# Silent Scheduled Tasks Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use baspowers:subagent-driven-development (recommended) or baspowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Add durable scheduled runs that leave no visible trigger and suppress successful empty responses while preserving findings and failures.

**Architecture:** Persist a `silent` schedule flag and transport silent fires as a custom Matrix timeline event that clients do not render.
The event journal owns that custom event as turn-backed work, normalizes it into the existing text pipeline, and carries a distinct source kind into non-streaming empty-aware response delivery.

**Tech Stack:** Python 3.13, Pydantic, matrix-nio, asyncio, FastAPI, pytest, uv, and the existing SQLite/PostgreSQL event-journal harness.

**Spec:** `docs/dev/silent-scheduled-tasks-design.md`

## Global Constraints

Normal schedules must retain their current visible `m.room.message` behavior.
Previously persisted schedules must deserialize with `silent=False`.
Only successful whitespace-only silent responses may be suppressed, while failures and explicit tool-posted messages remain visible.
Silent `new_thread` findings become visible room-level roots because the hidden trigger cannot be a visible thread root.
No new runtime dependency is allowed.
Markdown documentation uses one sentence per line.
Before every public-repository write or commit, scan proposed content and generated cross-references for prohibited names or tokens, private repository names or URLs, and private identifiers.
Never stage with `git add .` or `git add -A`, never amend commits, and never commit `docs/baspowers/*`.

---

### Task 1: Persist and expose the schedule flag

**Files:**
- Modify: `src/mindroom/scheduling.py`
- Modify: `src/mindroom/custom_tools/scheduler.py`
- Modify: `src/mindroom/prompts.py`
- Modify: `src/mindroom/api/schedules.py`
- Test: `tests/test_workflow_scheduling.py`
- Test: `tests/test_scheduler_tool.py`
- Test: `tests/test_scheduling.py`
- Test: `tests/api/test_schedules_api.py`

**Interfaces:**
- Produces: `ScheduledWorkflow.silent: bool = False`.
- Produces: `ScheduledTaskReadModel.silent: bool` and `ScheduledTaskResponse.silent: bool`.
- Produces: `build_edited_scheduled_workflow(..., silent: bool | None = None) -> ScheduledWorkflow`.
- Produces: `schedule_task(..., silent: bool | None = None) -> tuple[str | None, str]`.
- Produces: `SchedulerTools.schedule(request: str, new_thread: bool, history_limit: int | None = None, silent: bool = False) -> str`.
- Produces: `SchedulerTools.edit_schedule(task_id: str, request: str, history_limit: int | None = None, silent: bool | None = None) -> str`.

- [ ] **Step 1: Write failing persistence and edit tests**

Add tests proving old JSON defaults to visible mode, `silent=True` round-trips, patch-style edits preserve silence when omitted, and explicit `True` or `False` changes it.

```python
workflow = ScheduledWorkflow.model_validate_json(old_payload_without_silent)
assert workflow.silent is False

edited = build_edited_scheduled_workflow(workflow, room_id="!room:test", silent=True)
assert edited.silent is True
assert build_edited_scheduled_workflow(edited, room_id="!room:test").silent is True
```

- [ ] **Step 2: Run the new workflow tests and confirm RED**

Run: `uv run pytest tests/test_workflow_scheduling.py -n 0 --no-cov -q`

Expected: failures report the missing `silent` field or keyword.

- [ ] **Step 3: Implement the model, parser, list, and confirmation fields**

Add `silent: bool = False` to `ScheduledWorkflow`, copy it through `ScheduledTaskReadModel`, `_existing_task_parse_context`, `build_edited_scheduled_workflow`, and `_scheduled_task_response_text`, and render `Mode: Silent` or `Mode: Visible` in schedule listings.
Extend the parse prompt with exact rules that new schedules default to `false`, explicit quiet or silent wording sets `true`, explicit visible wording sets `false`, and edits preserve the current value when omitted.

```python
class ScheduledWorkflow(BaseModel):
    # existing fields
    silent: bool = False

def build_edited_scheduled_workflow(..., silent: bool | None = None) -> ScheduledWorkflow:
    return ScheduledWorkflow(
        # existing copied fields
        silent=existing_workflow.silent if silent is None else silent,
    )
```

- [ ] **Step 4: Write failing scheduler-tool and API tests**

Assert the create tool forwards `silent=False` and `silent=True`, the edit tool forwards `None`, `True`, and `False`, API list responses include `silent`, and API updates can toggle it without changing unrelated fields.

- [ ] **Step 5: Implement tool and API forwarding**

Add the typed parameters to the tool methods, `schedule_task`, `UpdateScheduleRequest`, and `ScheduledTaskResponse`, and pass them into the existing scheduling helpers.

- [ ] **Step 6: Run the Task 1 tests and confirm GREEN**

Run: `uv run pytest tests/test_workflow_scheduling.py tests/test_scheduler_tool.py tests/test_scheduling.py tests/api/test_schedules_api.py -n 0 --no-cov -q`

Expected: all selected tests pass.

- [ ] **Step 7: Scan, stage exact files, verify, and commit**

Run `git status --short`, scan the exact diff for prohibited or private references, stage only the eight Task 1 files, inspect `git diff --cached --name-only` and `git diff --cached --check`, then commit with `feat: add silent schedule contract`.

---

### Task 2: Send silent fires as custom Matrix events

**Files:**
- Modify: `src/mindroom/constants.py`
- Modify: `src/mindroom/dispatch_source.py`
- Modify: `src/mindroom/matrix/client_delivery.py`
- Modify: `src/mindroom/hooks/sender.py`
- Modify: `src/mindroom/scheduling_executor.py`
- Test: `tests/test_matrix_delivery.py`
- Test: `tests/test_scheduling_executor.py`

**Interfaces:**
- Consumes: `ScheduledWorkflow.silent` from Task 1.
- Produces: `SILENT_SCHEDULE_EVENT_TYPE = "io.mindroom.scheduled.trigger"`.
- Produces: `SILENT_SCHEDULE_SOURCE_KIND = "silent_scheduled"` and includes it in the existing automation policy sets.
- Produces: `send_message_outcome(..., message_type: str = "m.room.message") -> MatrixSendOutcome`.
- Produces: `send_message_result(..., message_type: str = "m.room.message") -> DeliveredMatrixEvent | None`.
- Produces: `send_matrix_message(..., message_type: str = "m.room.message") -> DeliveredMatrixEvent | None`.

- [ ] **Step 1: Write failing delivery tests for an arbitrary encrypted event type**

Extend the delivery seam tests so the explicit message type reaches `_send_prepared_room_message`, while callers that omit it still send `m.room.message`.

```python
await send_message_result(client, room_id, content, message_type=SILENT_SCHEDULE_EVENT_TYPE)
assert client.room_send.await_args.kwargs["message_type"] == SILENT_SCHEDULE_EVENT_TYPE
```

- [ ] **Step 2: Run the delivery tests and confirm RED**

Run: `uv run pytest tests/test_matrix_delivery.py -n 0 --no-cov -q`

Expected: `send_message_result` rejects the new keyword.

- [ ] **Step 3: Thread the event type through the existing send path**

Add the defaulted `message_type` keyword through `send_message_outcome`, `send_message_result`, and the hook sender facade without changing default callers.
Keep payload preparation, encryption guards, retry behavior, and delivered-event typing unchanged.

- [ ] **Step 4: Write failing scheduler transport tests**

Add tests proving visible schedules use `m.room.message`, silent schedules use the custom event type, both carry requester and history metadata, hook-transformed text is transported, hook suppression sends nothing, and silent new-thread content omits per-fire visible-root ownership.

- [ ] **Step 5: Implement the silent transport branch**

Select the event type from `workflow.silent`, keep normal content byte-for-byte compatible, and annotate silent content with `SILENT_SCHEDULE_SOURCE_KIND`.
Do not add `PER_FIRE_THREAD_ROOT_KEY` to silent new-thread content.

- [ ] **Step 6: Run Task 2 tests and confirm GREEN**

Run: `uv run pytest tests/test_matrix_delivery.py tests/test_scheduling_executor.py -n 0 --no-cov -q`

Expected: all selected tests pass.

- [ ] **Step 7: Scan, stage exact files, verify, and commit**

Run `git status --short`, scan the exact diff, stage only the seven Task 2 files, inspect the cached file list and diff check, then commit with `feat: transport silent schedule fires`.

---

### Task 3: Admit and replay silent triggers through the journal

**Files:**
- Modify: `src/mindroom/event_journal/models.py`
- Modify: `src/mindroom/matrix/journal_ingress.py`
- Modify: `src/mindroom/journal_dispatch.py`
- Modify: `src/mindroom/conversation_resolver.py`
- Test: `tests/test_journal_ingress.py`
- Test: `tests/test_turn_controller_focused.py`
- Test: `tests/test_workflow_scheduling.py`

**Interfaces:**
- Consumes: `SILENT_SCHEDULE_EVENT_TYPE` from Task 2.
- Consumes: `SILENT_SCHEDULE_SOURCE_KIND` from Task 2.
- Produces: `EventKind.SCHEDULE_TRIGGER = "schedule_trigger"` as a member of `TURN_BACKED_KINDS`.
- Produces: `_scheduled_trigger_as_message(event: nio.UnknownEvent) -> nio.RoomMessageFormatted` inside journal dispatch.

- [ ] **Step 1: Write failing journal admission tests**

Build `nio.UnknownEvent` fixtures with the custom type and assert live and recovered events are admitted as actionable scheduled-trigger work, cold-history events settle without callbacks, and unrelated unknown events remain unadmitted.

```python
event = nio.Event.parse_event(custom_schedule_source)
await ingress._admit(room, event, nio.TimelineEventProvenance.LIVE)
stored = await store.load_event(event.event_id)
assert stored is not None
assert stored.kind is EventKind.SCHEDULE_TRIGGER
```

- [ ] **Step 2: Run the admission tests and confirm RED**

Run: `uv run pytest tests/test_journal_ingress.py -n 0 --no-cov -q -k 'schedule_trigger'`

Expected: no scheduled-trigger event kind exists or the event is ignored.

- [ ] **Step 3: Add the event and source-kind vocabulary**

Add the event predicate before the generic unknown-event fallthrough and add the turn-backed journal kind.

- [ ] **Step 4: Write failing dispatch and recovery tests**

Assert the dispatcher converts valid custom content into a formatted message with the same sender, event ID, timestamp, body, content metadata, decrypted state, verification state, sender key, and session ID.
Assert malformed bodies settle intentionally without invoking `on_message`, while valid deferred callbacks keep the source pending and recovered rows replay through the same conversion.

- [ ] **Step 5: Implement validated conversion and binding**

Add a scheduled-trigger binding that accepts only the exact custom event type, copies its source with `type="m.room.message"`, ensures `content.msgtype="m.text"` and a nonempty string body, parses a formatted event, restores security fields from the original event, and delegates to `callbacks.on_message`.
Catch only validation or journal-corruption errors at this binding, log event identity without body content, and return completion so a malformed row cannot poison recovery.

- [ ] **Step 6: Write and implement target-placement tests**

Assert a silent trigger related to an existing thread keeps that thread and a room-level silent trigger resolves to room mode with no reply or thread relation to the hidden source.
Implement the room-mode override only when trusted source metadata identifies a silent scheduled source and the event has no existing thread.

- [ ] **Step 7: Run Task 3 tests and confirm GREEN**

Run: `uv run pytest tests/test_journal_ingress.py tests/test_turn_controller_focused.py tests/test_workflow_scheduling.py -n 0 --no-cov -q`

Expected: all selected tests pass.

- [ ] **Step 8: Scan, stage exact files, verify, and commit**

Run `git status --short`, scan the exact diff, stage only the seven Task 3 files, inspect the cached file list and diff check, then commit with `feat: dispatch silent schedule triggers`.

---

### Task 4: Make silent turns non-streaming and empty-aware

**Files:**
- Modify: `src/mindroom/response_payload_preparation.py`
- Modify: `src/mindroom/response_turn.py`
- Modify: `src/mindroom/response_runner.py`
- Modify: `src/mindroom/delivery_gateway.py`
- Test: `tests/test_response_turn.py`
- Test: `tests/test_response_runner_agent.py`
- Test: `tests/test_response_runner_team.py`
- Test: `tests/test_response_delivery_gateway.py`

**Interfaces:**
- Consumes: `SILENT_SCHEDULE_SOURCE_KIND` from Task 2, preserved through Task 3 dispatch.
- Produces: `ResponseTurnContext.allow_empty_response: bool = False`.
- Produces: a non-persistent `EnrichmentItem` keyed `silent_schedule_delivery`.
- Produces: automatic final suppression only when the source kind is silent scheduled and transformed response text is whitespace-only.

- [ ] **Step 1: Write failing response-driver tests**

Add blocking and streaming unit tests showing `allow_empty_response=True` accepts the first empty completion, records an empty successful result, performs no discard or retry, and yields no notice.
Retain the existing tests proving ordinary empty runs retry once and end in `EMPTY_RESPONSE_NOTICE`.

- [ ] **Step 2: Run response-turn tests and confirm RED**

Run: `uv run pytest tests/test_response_turn.py -n 0 --no-cov -q -k 'empty'`

Expected: `ResponseTurnContext` lacks the new policy or still retries.

- [ ] **Step 3: Implement source-scoped empty acceptance**

Add the defaulted context field and branch before `_settle_empty_run` so only allowed empty attempts settle with empty recorded and response text.

```python
if resolution.is_empty and ctx.allow_empty_response:
    return _CompletionSettle(
        keep_going=False,
        continuation=continuation,
        recorded_text="",
        recorded_tools=(),
        response_text="",
    )
```

- [ ] **Step 4: Write failing payload and streaming-policy tests**

Assert silent scheduled requests add the system instruction, set `allow_empty_response=True` for agent and team contexts, never enter streaming generation even when room streaming is enabled, and leave ordinary scheduled requests unchanged.

- [ ] **Step 5: Implement quiet execution policy**

Append an `EnrichmentItem` with `persist=False` that tells the entity to return no text for routine no-finding outcomes and to report findings or failures normally.
Derive `allow_empty_response` from the immutable response envelope source kind.
Resolve `use_streaming=False` before both agent and team streaming branches when that source kind is present.

- [ ] **Step 6: Write failing final-delivery tests**

Assert empty and whitespace-only silent responses return a suppressed cancelled outcome with no Matrix send, nonempty silent responses use normal durable delivery, and ordinary empty responses are not auto-suppressed.
Assert a before-response hook that transforms empty text into a finding causes delivery, while explicit hook suppression still wins.

- [ ] **Step 7: Implement final suppression after hook transformation**

Run before-response hooks first, then set `draft.suppress=True` when the resulting text is whitespace-only and `draft.envelope.source_kind` is the silent scheduled source.
Reuse the existing suppressed-delivery cleanup and source-settlement path.

- [ ] **Step 8: Run Task 4 tests and confirm GREEN**

Run: `uv run pytest tests/test_response_turn.py tests/test_response_runner_agent.py tests/test_response_runner_team.py tests/test_response_delivery_gateway.py -n 0 --no-cov -q`

Expected: all selected tests pass.

- [ ] **Step 9: Scan, stage exact files, verify, and commit**

Run `git status --short`, scan the exact diff, stage only the eight Task 4 files, inspect the cached file list and diff check, then commit with `feat: suppress empty silent schedule replies`.

---

### Task 5: Verify compatibility and repository quality

**Files:**
- Modify only files required to fix failures caused by Tasks 1 through 4.
- Test: scheduler, journal, response, API, import-graph, and configuration suites.

**Interfaces:**
- Consumes: all prior task interfaces.
- Produces: a release-ready silent-schedule implementation with no unrelated changes.

- [ ] **Step 1: Run the integrated focused suite**

Run: `uv run pytest tests/test_scheduling_executor.py tests/test_workflow_scheduling.py tests/test_scheduler_tool.py tests/test_scheduling.py tests/api/test_schedules_api.py tests/test_matrix_delivery.py tests/test_journal_ingress.py tests/test_turn_controller_focused.py tests/test_response_turn.py tests/test_response_runner_agent.py tests/test_response_runner_team.py tests/test_response_delivery_gateway.py -n 0 --no-cov -q`

Expected: all selected tests pass.

- [ ] **Step 2: Run architecture and import checks**

Run: `uv run pytest tests/test_import_graph.py -n 0 --no-cov -q`.
Run: `uv run tach check --dependencies --interfaces`.

Expected: both commands pass without widening an import allowlist for convenience.

- [ ] **Step 3: Run the complete backend suite**

Run: `uv run pytest -n 0 --no-cov -q`.

Expected: the complete suite passes.

- [ ] **Step 4: Run repository hooks**

Run: `uv run pre-commit run --all-files`.

Expected: every hook passes, and any mechanical formatting change is reviewed and retested.

- [ ] **Step 5: Audit the final branch**

Run `git status --short --branch`, inspect `git diff origin/main...HEAD --stat`, scan every commit and diff for prohibited or private references, and confirm `docs/baspowers/` is absent from the changed-file list.

- [ ] **Step 6: Commit verification-only fixes when necessary**

If verification required source changes, stage only those exact files after another status and content scan, verify the cached diff, and create a new commit without amending earlier commits.
