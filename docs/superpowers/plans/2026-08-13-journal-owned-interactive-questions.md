# Projection-Owned Interactive Prompts Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task.
> Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Make the admitted Matrix-visible revision the sole authority for active interactive prompts while retaining durable source-owned selections.

**Architecture:** Outbound terminal messages and edits carry typed `io.mindroom.interactive` metadata before transport.
The existing projection orders revisions, and the same admission transaction reconciles the active prompt from the resulting visible row before any later reaction can be admitted.
Runtime code renders buttons but no longer registers, replaces, or forgets durable questions.

**Tech Stack:** Python 3.13, asyncio, dataclasses, Matrix message content, SQLite, PostgreSQL, pytest, Ruff, ty, Tach.

## Global Constraints

- Preserve detached interactive reaction execution and source-owned selection replay.
- Preserve membership fencing and exact selecting-source turn identity.
- Keep PR #1807's journal-owned approval continuation model mechanically and semantically compatible.
- Do not add a staging table, retry queue, runtime lease, or callback state machine.
- Remove the public question registration, replacement, and forget APIs.
- Use Matrix projection order rather than HTTP completion order.
- Write one sentence per Markdown line.
- Write and run each behavior test before its production implementation.
- Finish with materially fewer production lines than commit `728a980c1`.

---

## File Structure

- `src/mindroom/interactive_models.py` owns the dependency-free prompt metadata and source selection value objects plus Matrix-content encoding and decoding.
- `src/mindroom/event_journal/interactive_questions.py` reconciles active prompts from visible projection rows and owns durable selection claims.
- `src/mindroom/event_journal/store.py` composes projection reconciliation with admission and exposes only claim APIs to runtime consumers.
- `src/mindroom/event_journal/views.py` keeps only claim methods required by typed runtime collaborators.
- `src/mindroom/delivery_gateway.py` includes prompt metadata in terminal non-streaming content.
- `src/mindroom/streaming.py` includes prompt metadata only in terminal streamed content.
- `src/mindroom/custom_tools/matrix_conversation_operations.py` includes prompt metadata before direct sends and edits and only adds buttons after successful transport.
- `src/mindroom/post_response_effects.py` adds buttons after response delivery without writing journal state.
- `tests/test_event_journal_store.py` proves projection ordering, membership authorization, admission snapshots, and selection replay on SQLite and PostgreSQL.
- `tests/test_matrix_message_tool.py`, `tests/test_streaming_finalize.py`, and `tests/test_response_delivery_gateway.py` prove outbound metadata is present before transport.

### Task 1: Make Projection Admission Own Active Prompt State

**Files:**

- Modify: `src/mindroom/interactive_models.py`
- Modify: `src/mindroom/event_journal/interactive_questions.py`
- Modify: `src/mindroom/event_journal/store.py`
- Test: `tests/test_event_journal_store.py`

**Interfaces:**

- Produce: `InteractivePrompt(creator_agent, question_text, options, option_labels, source_event_id, membership_epoch)`.
- Produce: `interactive_prompt_content(prompt: InteractivePrompt) -> dict[str, object]`.
- Produce: `interactive_prompt_from_content(content: Mapping[str, object]) -> InteractivePrompt | None`.
- Produce: `reconcile_projected_prompt(transaction, principal_id, projected) -> None`.
- Remove: `register_if_current`, `replace_if_current`, and `forget` from durable runtime use.

- [ ] **Step 1: Write failing projection-order tests**

Add real-store tests that admit a self-authored original prompt and prove a reaction snapshots it.
Add a test that admits an interactive edit before any transport callback could run, then admits a reaction and proves the replacement prompt is selected.
Add a test that admits two edits in reverse callback order and proves `origin_server_ts` plus event-ID projection ordering selects the Matrix-visible prompt.
Add tests that a non-interactive self edit clears the prompt, another sender cannot forge one, and an old source or stale epoch cannot activate one.

```python
await admit(alice, "$turn")
await admit(alice, "$target", sender=ALICE, content=prompt_content("Old?", "old", source="$turn"))
await admit(alice, "$newer", sender=ALICE, ts=3_000, content=prompt_edit("$target", "New?", "new"))
await admit(alice, "$older", sender=ALICE, ts=2_000, content=prompt_edit("$target", "Stale?", "stale"))
await admit(alice, "$reaction", kind=EventKind.REACTION, content=reaction("$target", "1"))
selection = await alice.claim_interactive_reaction(
    source_event_id="$reaction",
    question_event_id="$target",
    selection_key="1",
    creator_agent="agent",
)
assert selection is not None
assert (selection.question_text, selection.selected_value) == ("New?", "new")
```

- [ ] **Step 2: Run the new store tests and verify RED**

Run: `uv run pytest -q -n 0 --no-cov tests/test_event_journal_store.py -k 'projected_prompt or visible_prompt_revision'`.
Expected: FAIL because prompt activation still requires the public post-delivery registration API.

- [ ] **Step 3: Implement typed Matrix prompt metadata**

Replace the ID-bearing `InteractiveQuestion` transport object with `InteractivePrompt` containing only immutable payload, creator identity, and membership proof.
Encode it under `io.mindroom.interactive` and decode only exact typed fields.
When both proof forms are present, use the admitted source event if it exists and use the captured epoch only when the source does not exist.

- [ ] **Step 4: Reconcile from the visible projection**

After `journal.admit` projects a newly admitted actionable message or edit, read the target's current `visible_messages` row.
Accept self authorship only when `principal_id == f"{prompt.creator_agent}@{visible_sender}"` for interactive content or when the principal suffix matches the visible sender for a clearing edit.
Validate the source or epoch through `reads.claim_membership_epoch` and then replace or delete the active row in the same backend transaction.
Always reconcile from the currently visible row rather than the incoming edit so a losing older revision cannot reactivate itself.
Delete any active row whose current self-authored visible revision has no valid prompt metadata.

- [ ] **Step 5: Remove the claim-time active-row fallback**

Require reaction admission to have created its immutable source-bound selection snapshot.
Do not reinterpret a source through a prompt revision that appeared after admission.

- [ ] **Step 6: Run the store tests and verify GREEN**

Run: `uv run pytest -q -n 0 --no-cov tests/test_event_journal_store.py -k 'InteractiveQuestion or projected_prompt or visible_prompt_revision'`.
Expected: PASS on SQLite and PostgreSQL.

- [ ] **Step 7: Commit the projection-owned store slice**

Run: `git add src/mindroom/interactive_models.py src/mindroom/event_journal/interactive_questions.py src/mindroom/event_journal/store.py tests/test_event_journal_store.py && git commit -m "Activate interactive prompts from Matrix projection"`.

### Task 2: Put Prompt Metadata on the Wire Before Delivery

**Files:**

- Modify: `src/mindroom/delivery_gateway.py`
- Modify: `src/mindroom/streaming.py`
- Modify: `src/mindroom/custom_tools/matrix_conversation_operations.py`
- Modify: `src/mindroom/post_response_effects.py`
- Test: `tests/test_matrix_message_tool.py`
- Test: `tests/test_streaming_finalize.py`
- Test: `tests/test_response_delivery_gateway.py`

**Interfaces:**

- Consume: `interactive_prompt_content` and `InteractivePrompt` from Task 1.
- Remove: runtime calls to `register_interactive_question_for_turn`, `register_interactive_question_for_epoch`, and `forget_interactive_question`.
- Retain: `add_reaction_buttons` after successful Matrix delivery.

- [ ] **Step 1: Write failing wire-content tests**

Assert a direct interactive send carries the prompt and captured membership proof in its event content before `send_message_result` resolves.
Assert an interactive edit carries the prompt inside `m.new_content` and the physical edit event becomes the revision identity at admission.
Assert a plain edit omits prompt metadata and therefore clears the active row when admitted.
Assert terminal blocking and streaming responses carry source-turn proof, while nonterminal streaming revisions never carry prompt metadata.

```python
sent_content = mock_send.await_args.args[2]
assert sent_content["io.mindroom.interactive"] == {
    "creator_agent": "general",
    "membership_epoch": 0,
    "option_labels": {"1": "Approve"},
    "options": {"1": "approve"},
    "question_text": "Continue?",
}
```

- [ ] **Step 2: Run the outbound tests and verify RED**

Run: `uv run pytest -q -n 0 --no-cov tests/test_matrix_message_tool.py tests/test_streaming_finalize.py tests/test_response_delivery_gateway.py -k 'interactive and (content or metadata or edit)'`.
Expected: FAIL because prompt metadata is still registered only after delivery.

- [ ] **Step 3: Add metadata to direct Matrix operations**

Parse direct text once with mapping enabled.
Capture the target membership epoch before transport only when the message is interactive.
Include the typed prompt metadata in send or edit content and add reaction buttons after a successful result.
Delete `_register_interactive`, `_maybe_add_interactive_question`, the post-send epoch registration branch, and the explicit forget branch.

- [ ] **Step 4: Add metadata to terminal response delivery**

For blocking delivery, merge prompt metadata into the `SendTextRequest` or `EditTextRequest` content using the response source event ID and delivery agent.
For streaming delivery, carry the creator agent and source event ID into the immutable delivery snapshot and add metadata only when the resolved stream status is terminal.
Keep progressive placeholders and edits free of prompt metadata.

- [ ] **Step 5: Reduce post-response effects to buttons**

Rename the dependency from registration language to button-rendering language.
Remove `PrincipalStore` and membership-turn plumbing from `PostResponseEffectsSupport` where it exists only for question registration.

- [ ] **Step 6: Run the outbound tests and verify GREEN**

Run: `uv run pytest -q -n 0 --no-cov tests/test_matrix_message_tool.py tests/test_streaming_finalize.py tests/test_response_delivery_gateway.py`.
Expected: PASS.

- [ ] **Step 7: Commit the wire-format slice**

Run: `git add src/mindroom/delivery_gateway.py src/mindroom/streaming.py src/mindroom/custom_tools/matrix_conversation_operations.py src/mindroom/post_response_effects.py tests && git commit -m "Carry interactive prompts in Matrix revisions"`.

### Task 3: Delete Registration Surface and Reconcile Documentation

**Files:**

- Modify: `src/mindroom/event_journal/store.py`
- Modify: `src/mindroom/event_journal/views.py`
- Modify: `src/mindroom/event_journal/__init__.py`
- Modify: `docs/architecture/bot-runtime.md`
- Modify: `docs/superpowers/specs/2026-08-13-journal-owned-interactive-questions-design.md`
- Test: affected runtime and membership suites.

**Interfaces:**

- Retain: `claim_interactive_reaction` and `claim_interactive_text`.
- Remove: every public registration, replacement, and forget method plus associated test fakes.

- [ ] **Step 1: Delete obsolete APIs and tests**

Remove the public store and view methods, registration helpers, `replace_existing` policy flag, and tests that exercise callback registration rather than visible-revision behavior.
Update typed fakes directly instead of retaining compatibility wrappers.

- [ ] **Step 2: Prove obsolete symbols are gone**

Run: `rg -n 'register_interactive_question|replace_existing|forget_interactive_question|_register_interactive|_maybe_add_interactive_question' src/mindroom`.
Expected: no output.

- [ ] **Step 3: Update architecture language**

Document that outbound metadata describes a candidate prompt but only journal admission activates it.
Document that projection ordering chooses the active revision and source settlement consumes only the immutable selection.
Remove every statement that transport callbacks register or replace active questions.

- [ ] **Step 4: Run affected suites**

Run: `uv run pytest -q -n 0 --no-cov tests/test_event_journal_store.py tests/test_interactive.py tests/test_journal_membership_fence.py tests/test_response_runner_focused.py tests/test_matrix_message_tool.py tests/test_bot_reactions_approvals.py tests/test_streaming_finalize.py tests/test_journal_ingress.py tests/test_queued_message_notify.py tests/test_turn_controller_focused.py tests/test_matrix_sync_continuity.py tests/test_sync_task_cancellation.py tests/test_ingress_lanes.py tests/test_response_delivery_gateway.py tests/test_locked_turn_delivery.py tests/test_bot_ready_hook.py`.
Expected: PASS on SQLite and PostgreSQL where parametrized.

- [ ] **Step 5: Measure net simplification**

Run: `git diff --numstat d89a7da9ce0474cdfde35777e92bf2f239bbf6ad...HEAD -- src/mindroom`.
Expected: materially fewer than the `+410/-126` production diff at `728a980c1`, with fewer public methods and no replacement policy branch.

- [ ] **Step 6: Commit the deletion and documentation slice**

Run: `git add src tests docs && git commit -m "Remove post-delivery prompt registration"`.

### Task 4: Verify Exact Head, PR #1807 Compatibility, and Merge Readiness

**Files:**

- No permanent files unless verification exposes a real defect.

**Interfaces:**

- Consume: the latest exact pushed head of PR #1807 resolved immediately before verification.
- Produce: a clean frozen PR head with independent correctness, simplicity, and compatibility approvals.

- [ ] **Step 1: Run static verification**

Run: `uv run pre-commit run --all-files`.
Run: `uv run tach check --dependencies --interfaces`.
Run: `git diff --check`.
Expected: every command exits zero.

- [ ] **Step 2: Run the complete non-Matrix suite**

Run: `uv run pytest -m 'not requires_matrix'`.
Expected: zero failures.

- [ ] **Step 3: Verify PR #1807 mechanically and semantically**

Resolve PR #1807's exact GitHub head with `gh pr view 1807 --repo mindroom-ai/mindroom --json headRefOid,baseRefOid`.
Create a disposable merge worktree and require a conflict-free merge tree or only additive conflicts eliminated in this PR.
Run the combined interactive, approval-continuation, schema, response-runner, and bot-reaction suites on SQLite and PostgreSQL.

- [ ] **Step 4: Push and run independent exact-head reviews**

Push only after local verification passes.
Freeze the worktree and dispatch independent correctness, simplification, and PR #1807 compatibility reviewers against the immutable GitHub head.
Address only verified in-scope findings and restart the exact-head round after any executable change.

- [ ] **Step 5: Merge only after all gates pass**

Require a clean worktree, local and GitHub SHA equality, zero unresolved review threads, all required CI green, two independent approvals, and PR #1807 compatibility approval.
Then squash merge PR #1828 and verify its merged state and merge commit.
