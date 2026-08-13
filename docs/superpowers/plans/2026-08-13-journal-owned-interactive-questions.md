# Journal-Owned Interactive Questions Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task.
> Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Replace JSON and process-global interactive-question ownership with one event-journal transaction boundary for registration, selection, settlement, and membership invalidation.

**Architecture:** A focused `event_journal.interactive_questions` module owns one narrow table and returns immutable selection dataclasses.
The event journal binds a question to its admitted source, consumes it during source settlement, and deletes it during membership fencing, while runtime code only executes the durable selection.

**Tech Stack:** Python 3.13, asyncio, dataclasses, SQLite, PostgreSQL, pytest, Ruff, ty, Tach.

## Global Constraints

- Keep all required behavior in PR #1825.
- Preserve detached interactive reaction execution so room dispatch remains non-blocking.
- Keep PR #1807's journal-owned approval continuation model compatible.
- Implement the smallest correct change and remove obsolete machinery aggressively.
- Do not add durable runtime execution leases.
- Do not preserve `interactive_questions.json` compatibility.
- Write one sentence per Markdown line.
- Write and run each behavior test before its production implementation.

---

## File Structure

- `src/mindroom/event_journal/interactive_questions.py` owns question serialization and transactional registration and claim operations.
- `src/mindroom/event_journal/schema.py` owns the `interactive_questions` table and its active-question lookup index.
- `src/mindroom/event_journal/store.py` exposes typed async `PrincipalStore` methods and composes turn membership proof with registration.
- `src/mindroom/event_journal/views.py` exposes only the methods required by typed runtime collaborators.
- `src/mindroom/event_journal/journal.py` composes question consumption with source settlement and question deletion with membership invalidation.
- `src/mindroom/interactive.py` retains parsing, prompt building, sender policy, and Matrix button I/O only.
- `src/mindroom/reaction_dispatch.py` asks the journal for one durable reaction selection.
- `src/mindroom/turn_controller.py` asks the journal for text selections and executes durable selections without restore or commit callbacks.
- `src/mindroom/post_response_effects.py` registers delivered response questions through `PrincipalStore`.
- `src/mindroom/custom_tools/matrix_conversation_operations.py` registers and clears direct-tool questions through `PrincipalStore`.
- `src/mindroom/event_journal/membership.py` translates membership evidence without external cleanup callbacks.
- `src/mindroom/membership_models.py` keeps one reported departure's room, durable identity, and rejoin evidence in one immutable record.
- `src/mindroom/bot.py` wires the journal methods and no longer initializes interactive JSON persistence.
- `tests/test_event_journal_store.py` proves storage invariants against SQLite and PostgreSQL.
- `tests/test_bot_reactions_approvals.py`, `tests/test_turn_controller_focused.py`, and `tests/test_matrix_message_tool.py` prove runtime integration.
- `tests/test_journal_membership_fence.py` proves atomic membership deletion.
- `tests/test_interactive.py` retains pure parsing, prompt, policy, and Matrix I/O tests and drops persistence-only cases.

### Task 1: Add the Durable Question Record and Registration APIs

**Files:**

- Create: `src/mindroom/event_journal/interactive_questions.py`
- Modify: `src/mindroom/event_journal/schema.py`
- Modify: `src/mindroom/event_journal/store.py`
- Modify: `src/mindroom/event_journal/views.py`
- Modify: `src/mindroom/event_journal/__init__.py`
- Test: `tests/test_event_journal_store.py`

**Interfaces:**

- Produce: `InteractiveQuestion(question_event_id, room_id, thread_id, creator_agent, question_text, options, option_labels)`.
- Produce: `InteractiveSelection(question_event_id, question_text, selection_key, selected_label, selected_value, thread_id)`.
- Produce: `PrincipalStore.register_interactive_question_for_turn(turn_id: str, question: InteractiveQuestion) -> bool`.
- Produce: `PrincipalStore.register_interactive_question_for_epoch(expected_membership_epoch: int, question: InteractiveQuestion) -> bool`.
- Produce: `PrincipalStore.forget_interactive_question(question_event_id: str) -> None`.

- [ ] **Step 1: Write failing registration tests**

Add parametrized real-store tests that admit a source at epoch zero, register one question, and assert a duplicate registration preserves the original immutable payload.
Add tests that reject a stale turn, a stale captured epoch, and every registration while `departure_fenced` is true.
Add a test that `forget_interactive_question` removes only the addressed question.

```python
question = InteractiveQuestion(
    question_event_id="$question",
    room_id="!room:test",
    thread_id="$thread",
    creator_agent="agent",
    question_text="Choose",
    options={"1": "one", "👍": "one"},
    option_labels={"1": "One", "👍": "One"},
)
assert await principal.register_interactive_question_for_turn("$source", question)
assert not await principal.register_interactive_question_for_epoch(0, replace(question, question_event_id="$stale"))
```

- [ ] **Step 2: Run the registration tests and verify RED**

Run: `uv run pytest -q -n 0 --no-cov tests/test_event_journal_store.py -k 'interactive_question_registration or interactive_question_forget'`
Expected: FAIL because the table, dataclasses, and store methods do not exist.

- [ ] **Step 3: Implement the narrow schema and registration module**

Create the table from the approved design with a unique `(principal_id, claimed_source_event_id)` constraint.
Add an index on `(principal_id, room_id, thread_id, creator_agent, created_at_ns, question_event_id)` limited to unclaimed rows where the backend supports the shared predicate.
Serialize only `question_text`, `options`, and `option_labels` into deterministic JSON.
Use `reads.claim_membership_epoch` before inserting so epoch matching, fenced rejection, and the insert share one transaction.
Use `ON CONFLICT (principal_id, question_event_id) DO NOTHING` so replay never rewrites the question users saw.

- [ ] **Step 4: Run the registration tests and verify GREEN**

Run: `uv run pytest -q -n 0 --no-cov tests/test_event_journal_store.py -k 'interactive_question_registration or interactive_question_forget'`
Expected: PASS on SQLite and PostgreSQL.

- [ ] **Step 5: Commit the durable registration slice**

Run: `git add src/mindroom/event_journal tests/test_event_journal_store.py && git commit -m "Store interactive questions in the event journal"`

### Task 2: Make Selection Claims Durable and Replayable

**Files:**

- Modify: `src/mindroom/event_journal/interactive_questions.py`
- Modify: `src/mindroom/event_journal/store.py`
- Modify: `src/mindroom/event_journal/views.py`
- Test: `tests/test_event_journal_store.py`

**Interfaces:**

- Consume: `InteractiveQuestion` and `InteractiveSelection` from Task 1.
- Produce: `PrincipalStore.claim_interactive_reaction(source_event_id: str, question_event_id: str, selection_key: str, creator_agent: str) -> InteractiveSelection | None`.
- Produce: `PrincipalStore.claim_interactive_text(source_event_id: str, selection_key: str, creator_agent: str) -> InteractiveSelection | None`.

- [ ] **Step 1: Write failing atomic-claim tests**

Add tests proving a pending reaction and active question become `INTERACTIVE_REACTION` plus one durable source binding in one call.
Assert replay by the same reaction returns the same literal selection.
Assert a second reaction cannot steal the question and does not gain the interactive semantic consumer.
Assert a reaction in another room, a terminal reaction, an old-membership reaction, an invalid option, and another agent all return `None` without mutation.
Add text tests proving the oldest eligible question is selected deterministically and same-message replay returns it.

```python
selection = await principal.claim_interactive_reaction(
    source_event_id="$reaction",
    question_event_id="$question",
    selection_key="👍",
    creator_agent="agent",
)
assert selection == InteractiveSelection(
    question_event_id="$question",
    question_text="Choose",
    selection_key="👍",
    selected_label="One",
    selected_value="one",
    thread_id="$thread",
)
assert await principal.claim_interactive_reaction(...) == selection
```

- [ ] **Step 2: Run the claim tests and verify RED**

Run: `uv run pytest -q -n 0 --no-cov tests/test_event_journal_store.py -k 'interactive_reaction_claim or interactive_text_claim'`
Expected: FAIL because claim methods do not exist.

- [ ] **Step 3: Implement row-locked claim transitions**

Lock the source and question rows with portable no-op `UPDATE ... RETURNING` statements before validating them.
Require a pending source, current unfenced membership, matching room and membership epoch, matching creator agent, and an available option.
Set `semantic_consumer` and `claimed_source_event_id` only after all facts are locked and validated.
For text selection, first return a row already bound to the same source, otherwise claim the oldest matching active row by `created_at_ns` and byte-ordered event ID.

- [ ] **Step 4: Run the claim tests and verify GREEN**

Run: `uv run pytest -q -n 0 --no-cov tests/test_event_journal_store.py -k 'interactive_reaction_claim or interactive_text_claim'`
Expected: PASS on SQLite and PostgreSQL.

- [ ] **Step 5: Commit the durable claim slice**

Run: `git add src/mindroom/event_journal tests/test_event_journal_store.py && git commit -m "Claim interactive selections transactionally"`

### Task 3: Couple Consumption and Membership Invalidation to Journal Transactions

**Files:**

- Modify: `src/mindroom/event_journal/interactive_questions.py`
- Modify: `src/mindroom/event_journal/journal.py`
- Test: `tests/test_event_journal_store.py`
- Test: `tests/test_journal_membership_fence.py`

**Interfaces:**

- Produce: `interactive_questions.consume_for_sources(transaction, principal_id, source_event_ids) -> None`.
- Produce: `interactive_questions.delete_for_room(transaction, principal_id, room_id) -> None`.
- Modify: `journal.settle_many` to consume claimed questions before terminalizing sources.
- Modify: `_advance_membership_epoch` to delete room questions before settling stale work.

- [ ] **Step 1: Write failing transaction-boundary tests**

Register and claim a question, call the real `settle_many` path, and assert both the source and question are terminal in the committed state.
Raise after `consume_for_sources` inside a backend write and assert rollback preserves both the pending source and claimed question.
Fence a room and assert active and claimed questions from that room disappear while another room and another principal survive.
Raise during the fence transaction and assert its question row survives rollback.

- [ ] **Step 2: Run the boundary tests and verify RED**

Run: `uv run pytest -q -n 0 --no-cov tests/test_event_journal_store.py tests/test_journal_membership_fence.py -k 'interactive_question and (settle or fence or rollback)'`
Expected: FAIL because settlement and membership invalidation do not touch the new table.

- [ ] **Step 3: Implement transactional deletion**

Call `consume_for_sources` from the sole `settle_many` implementation before compacting source rows.
Call `delete_for_room` from `_advance_membership_epoch` beside existing approval-card and derived-state deletion.
Do not introduce callbacks, retry tables, or cleanup state.

- [ ] **Step 4: Run the boundary tests and verify GREEN**

Run: `uv run pytest -q -n 0 --no-cov tests/test_event_journal_store.py tests/test_journal_membership_fence.py -k 'interactive_question and (settle or fence or rollback)'`
Expected: PASS on SQLite and PostgreSQL.

- [ ] **Step 5: Commit the transactional boundary slice**

Run: `git add src/mindroom/event_journal tests/test_event_journal_store.py tests/test_journal_membership_fence.py && git commit -m "Consume interactive questions with journal truth"`

### Task 4: Route Registration and Selection Through the Journal

**Files:**

- Modify: `src/mindroom/post_response_effects.py`
- Modify: `src/mindroom/custom_tools/matrix_conversation_operations.py`
- Modify: `src/mindroom/reaction_dispatch.py`
- Modify: `src/mindroom/journal_dispatch.py`
- Modify: `src/mindroom/turn_controller.py`
- Modify: `src/mindroom/bot.py`
- Test: `tests/test_bot_reactions_approvals.py`
- Test: `tests/test_matrix_message_tool.py`
- Test: `tests/test_turn_controller_focused.py`

**Interfaces:**

- Consume: Task 1 registration APIs.
- Consume: Task 2 reaction and text claim APIs.
- Produce: Journal dispatcher methods that bind the current admitted source ID to store claims without exposing transaction details.

- [ ] **Step 1: Write failing runtime-boundary tests**

Add a real-store reaction test that registers a question, dispatches a reaction, and observes a deferred selection without touching interactive module globals.
Add a replay test that recreates the bot-facing collaborators over the same database and receives the same claimed selection.
Add post-response and direct-tool tests that verify buttons are added only after the journal registration succeeds.
Add a text-response test that claims through the admitted message source and returns the literal oldest selection.

- [ ] **Step 2: Run the runtime-boundary tests and verify RED**

Run: `uv run pytest -q -n 0 --no-cov tests/test_bot_reactions_approvals.py tests/test_matrix_message_tool.py tests/test_turn_controller_focused.py -k 'journal_owned_interactive or durable_interactive'`
Expected: FAIL because runtime callers still use `interactive.py` persistence.

- [ ] **Step 3: Replace runtime registration and claim calls**

Build `InteractiveQuestion` at the delivery boundary and call the explicit turn or epoch registration method.
Make `ReactionDispatcher` perform sender policy checks and obtain its selection through the journal dispatcher.
Make the text ingress path call `claim_interactive_text` with the admitted message event ID.
Keep Matrix reaction-button sends outside the transaction and only after successful registration.
Keep response parsing and `build_selection_prompt` in `interactive.py`.

- [ ] **Step 4: Run the runtime-boundary tests and verify GREEN**

Run: `uv run pytest -q -n 0 --no-cov tests/test_bot_reactions_approvals.py tests/test_matrix_message_tool.py tests/test_turn_controller_focused.py -k 'journal_owned_interactive or durable_interactive'`
Expected: PASS.

- [ ] **Step 5: Commit the runtime routing slice**

Run: `git add src/mindroom tests/test_bot_reactions_approvals.py tests/test_matrix_message_tool.py tests/test_turn_controller_focused.py && git commit -m "Route interactive state through the journal"`

### Task 5: Remove Restore and Commit Reconciliation

**Files:**

- Modify: `src/mindroom/turn_controller.py`
- Modify: `src/mindroom/reaction_dispatch.py`
- Modify: `src/mindroom/response_runner.py`
- Test: `tests/test_bot_reactions_approvals.py`
- Test: `tests/test_turn_controller_focused.py`
- Test: `tests/test_response_runner_focused.py`

**Interfaces:**

- Consume: durable same-source replay from Task 2.
- Consume: terminal consumption from Task 3.
- Remove: `interactive.commit_selection` and `interactive.restore_selection` call sites.
- Remove: selection-specific `dispatch_source_is_terminal` reconciliation.

- [ ] **Step 1: Write failing failure-and-cancellation tests**

Cause selection execution to fail before FINAL enqueue and assert the source remains pending and replay returns the same selection without any restore callback.
Cancel before task start and after lifecycle acquisition and assert normal source retry preserves the durable claim.
Reach FINAL handoff, then raise, and assert the source is terminal and the question is already absent without a terminal probe.

- [ ] **Step 2: Run the lifecycle tests and verify RED**

Run: `uv run pytest -q -n 0 --no-cov tests/test_bot_reactions_approvals.py tests/test_turn_controller_focused.py tests/test_response_runner_focused.py -k 'durable_selection and (failure or cancellation or terminal)'`
Expected: FAIL because current runtime callbacks still restore or commit process-local state.

- [ ] **Step 3: Simplify the detached handoff**

Remove restore callbacks from `_InteractiveSelectionDispatch` and `PendingDispatchMetadata`.
Remove commit and restore branches from `handle_interactive_selection`.
On prestart or execution failure, leave the question bound to the pending source and release that source through the existing retry path.
Preserve `CancelledError` propagation.
Keep the restart-only wait for live source-owned tasks because task execution ownership is not durable.

- [ ] **Step 4: Run the lifecycle tests and verify GREEN**

Run: `uv run pytest -q -n 0 --no-cov tests/test_bot_reactions_approvals.py tests/test_turn_controller_focused.py tests/test_response_runner_focused.py -k 'durable_selection and (failure or cancellation or terminal)'`
Expected: PASS.

- [ ] **Step 5: Commit the lifecycle simplification**

Run: `git add src/mindroom/turn_controller.py src/mindroom/reaction_dispatch.py src/mindroom/response_runner.py tests && git commit -m "Replay durable interactive selections"`

### Task 6: Remove JSON Persistence and External Membership Cleanup

**Files:**

- Modify: `src/mindroom/interactive.py`
- Modify: `src/mindroom/event_journal/membership.py`
- Modify: `src/mindroom/event_journal/store.py`
- Modify: `src/mindroom/event_journal/journal.py`
- Modify: `src/mindroom/bot.py`
- Modify: `tests/test_interactive.py`
- Modify: `tests/test_journal_membership_fence.py`
- Modify: `tests/bot_helpers.py`

**Interfaces:**

- Remove: `init_persistence`, `_active_questions`, `_claimed_questions`, dirty and deleted overlays, JSON loaders and writers, and room cleanup functions.
- Remove: membership cleanup callbacks and `cleanup_fenced_departure` APIs.
- Retain: parsing, formatting, prompt construction, sender policy, and reaction-button I/O.

- [ ] **Step 1: Add or retain pure interactive tests before deletion**

Ensure parsing, prompt payload, managed-agent rejection, and reaction-button rendering have direct behavior tests that do not initialize persistence.
Run those tests before deleting persistence code so each retained behavior has a passing baseline.

- [ ] **Step 2: Delete persistence and callback machinery**

Remove JSON, tempfile, threading, filesystem-lock, and time imports that only supported question persistence.
Remove `AgentBot.start` persistence initialization.
Make membership fence and rejoin methods direct journal transitions with no `Callable[[], None]` parameters.
Delete tests whose only behavior is JSON corruption, flock interleaving, dirty overlay merging, or cross-process dictionary reconciliation.

- [ ] **Step 3: Run focused suites and verify GREEN**

Run: `uv run pytest -q -n 0 --no-cov tests/test_interactive.py tests/test_journal_membership_fence.py tests/test_bot_reactions_approvals.py tests/test_turn_controller_focused.py tests/test_matrix_message_tool.py`
Expected: PASS on both configured database backends where parametrized.

- [ ] **Step 4: Prove obsolete symbols are gone**

Run: `rg -n 'interactive_questions\.json|_active_questions|_claimed_questions|commit_selection|restore_selection|clear_interactive_questions_for_room|cleanup_fenced_departure' src/mindroom`
Expected: no output.

- [ ] **Step 5: Commit the deletion slice**

Run: `git add src/mindroom tests && git commit -m "Remove split interactive persistence ownership"`

### Task 7: Reconcile Documentation, Architecture, and Full Verification

**Files:**

- Modify: `docs/architecture/bot-runtime.md`
- Modify: PR description if the final scope changes materially.
- Test: all affected suites.

**Interfaces:**

- Consume: all prior tasks.
- Produce: final merge-and-forget documentation and verified source tree.

- [ ] **Step 1: Update architecture documentation**

Document that interactive questions and their selecting source share the journal transaction boundary.
Document that detached execution remains runtime-owned and that restart quiescence prevents overlapping execution.
Remove descriptions of JSON question recovery or external departure cleanup.

- [ ] **Step 2: Run focused affected suites**

Run: `uv run pytest -q -n 0 --no-cov tests/test_event_journal_store.py tests/test_interactive.py tests/test_journal_membership_fence.py tests/test_response_runner_focused.py tests/test_matrix_message_tool.py tests/test_bot_reactions_approvals.py tests/test_streaming_finalize.py tests/test_journal_ingress.py tests/test_queued_message_notify.py tests/test_turn_controller_focused.py tests/test_matrix_sync_continuity.py tests/test_sync_task_cancellation.py`
Expected: PASS.

- [ ] **Step 3: Run static and dependency verification**

Run: `pre-commit run --all-files`.
Run: `uv run tach check --dependencies --interfaces`.
Run: `git diff --check`.
Expected: every command exits zero.

- [ ] **Step 4: Run the complete non-Matrix test suite**

Run: `uv run pytest -m 'not requires_matrix'`.
Expected: zero failures.

- [ ] **Step 5: Compare complexity against the prior PR head**

Run: `git diff --stat 192b5c8efa0cde0be45a527e5641d357892a8ea5..HEAD -- src/mindroom`.
Run: `git diff --numstat fb10e4127489755da8202ed56c5712932c36f7aa...HEAD -- src/mindroom`.
Expected: the redesign deletes the JSON and callback machinery and materially reduces the production growth from the previous head's `+634/-80` production diff.

- [ ] **Step 6: Commit final documentation and cleanup**

Run: `git add docs src tests && git commit -m "Document journal-owned interactive recovery"`.

### Task 8: Verify PR #1807 Compatibility and Push

**Files:**

- No permanent production files unless conflict reconciliation exposes a real integration issue.
- Temporary worktree created with `mktemp -d` and removed with `git worktree remove` after verification.

**Interfaces:**

- Consume: PR #1807 pushed head `c9bb299ccc40a812c95407d9b871efe3046c3af3` or a newer exact GitHub head resolved immediately before verification.
- Produce: evidence that the two journal-owned workflows compose after mechanical conflict resolution.

- [ ] **Step 1: Resolve the current #1807 exact head**

Run: `gh pr view 1807 --repo mindroom-ai/mindroom --json headRefOid,baseRefOid,state,mergeable`.
Record the returned head and use that immutable object for the compatibility test.

- [ ] **Step 2: Create a temporary integration branch and merge tree**

Create a temporary worktree from the redesigned #1825 head.
Merge the exact #1807 head without committing.
Resolve only mechanical shared-file conflicts by retaining both independent table modules, both membership deletions, both `PrincipalStore` API sets, #1807 runtime-generation filtering, and #1825 worker stop-generation checks.

- [ ] **Step 3: Run combined focused verification**

Run the event-journal, approval continuation, interactive reaction, response runner, pending worker, membership fence, and sync cancellation suites in the temporary integration worktree.
Expected: zero failures on SQLite and PostgreSQL parametrizations.

- [ ] **Step 4: Remove the temporary integration worktree**

Abort the temporary merge if it remains in progress.
Remove the explicit validated temporary path with `git worktree remove`.
Do not modify PR #1807's existing dirty worktree.

- [ ] **Step 5: Verify and push PR #1825**

Run: `git status --short`, `git log --oneline -8`, and `git diff --check`.
Push `fix/pr-1825-followup` to `origin/fix/interactive-reaction-cancel-before-start` only after every required verification succeeds.

### Task 9: Final Boundary Simplification

**Files:**

- Create: `src/mindroom/membership_models.py`
- Modify: `src/mindroom/matrix/sync_loop.py`
- Modify: `src/mindroom/event_journal/membership.py`
- Modify: `src/mindroom/event_journal/interactive_questions.py`
- Test: `tests/test_sync_task_cancellation.py`
- Test: `tests/test_journal_membership_fence.py`
- Test: `tests/test_event_journal_store.py`

**Interfaces:**

- Replace the three parallel departure tuples with `tuple[ReportedDeparture, ...]`.
- Give `ReactionDispatcher` one interactive-selection handoff instead of round-tripping two controller methods through its dependencies.
- Preserve the membership, source, and question row-lock order.
- Remove only local wrappers and duplicated SQL that do not own a distinct invariant.

- [x] **Step 1: Prove the record boundary with a failing parser test**

- [x] **Step 2: Consolidate reported-departure transport into one immutable record**

- [x] **Step 3: Simplify question decoding and binding without changing transaction order**

- [x] **Step 4: Collapse the circular reaction-to-controller callback seam**

- [ ] **Step 5: Run the complete affected suite, static checks, and exact PR #1807 integration**
