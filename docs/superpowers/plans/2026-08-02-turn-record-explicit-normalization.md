# Explicit Turn Record Normalization Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Remove hidden `TurnRecord` post-construction mutation and make canonicalization and invariant-sensitive updates explicit without changing durable behavior.

**Architecture:** A focused `turn_record.py` module will own the immutable canonical value and pure normalization functions. `handled_turns.py` will retain ledger persistence and codecs, while callers will use explicit canonical construction for invariant-sensitive transitions instead of relying on `dataclasses.replace()` to rerun `__post_init__`.

**Tech Stack:** Python 3.13, frozen dataclasses, pytest, Ruff, mypy, uv

---

## File Map

- Create `src/mindroom/turn_record.py` for `SourceEventMetadata`, `TurnRecord`, grouped canonicalization helpers, and explicit canonical record updates.
- Modify `src/mindroom/handled_turns.py` to consume and re-export the focused model while retaining codec and ledger responsibilities.
- Modify `src/mindroom/turn_store.py`, `src/mindroom/edit_regenerator.py`, `src/mindroom/text_ingress_dispatch.py`, `src/mindroom/visible_response_reconciliation.py`, and `src/mindroom/command_turn_executor.py` where updates currently depend on implicit re-normalization.
- Modify `tests/test_handled_turns.py` for focused construction, immutability, normalization, and transition coverage.
- Modify affected integration tests only when they directly construct noncanonical `TurnRecord` values.

### Task 1: Pin Explicit Canonicalization Behavior

**Files:**
- Modify: `tests/test_handled_turns.py`

- [ ] **Step 1: Write failing tests for a post-init-free canonical model**

Add tests that require explicit creation to normalize coupled values and require the dataclass itself to have no post-init hook.

```python
def test_turn_record_has_no_post_init_normalization_hook() -> None:
    assert "__post_init__" not in TurnRecord.__dict__


def test_turn_record_create_normalizes_coupled_source_state() -> None:
    record = TurnRecord.create(
        ["source", "source", ""],
        discovery_event_ids=["source", "edit", "edit"],
        redacted_source_event_ids=["missing", "edit"],
        pending_redaction_cleanup_event_ids=["source", "edit"],
        source_event_prompts={"source": "prompt"},
        source_event_revisions={"edit": [4, "edit-event"]},
    )

    assert record.source_event_ids == ("source",)
    assert record.discovery_event_ids == ("edit",)
    assert record.redacted_source_event_ids == ("edit",)
    assert record.pending_redaction_cleanup_event_ids == ("edit",)
    assert record.source_event_prompts == {"source": "prompt"}
    assert record.source_event_revisions is None
```

- [ ] **Step 2: Run the focused tests and verify the new structural test fails**

Run: `uv run pytest tests/test_handled_turns.py -k 'no_post_init or normalizes_coupled_source_state' -v`

Expected: the post-init test fails because `TurnRecord.__dict__` still contains `__post_init__`, while the behavior test passes against the existing implementation.

- [ ] **Step 3: Commit the characterization tests**

Run:

```bash
git add tests/test_handled_turns.py
git commit -m "Test explicit turn record canonicalization"
```

### Task 2: Extract the Canonical Turn Record Model

**Files:**
- Create: `src/mindroom/turn_record.py`
- Modify: `src/mindroom/handled_turns.py`
- Test: `tests/test_handled_turns.py`

- [ ] **Step 1: Add cohesive normalized value groups**

Create private frozen dataclasses `_CanonicalSourceState`, `_CanonicalDeliveryState`, `_CanonicalDispatchState`, `_CanonicalCommandState`, and `_CanonicalContextState` in `turn_record.py`.
Add pure functions that return those values from the permissive arguments currently accepted by `TurnRecord.create(...)`.
The source-state function must deduplicate identities, remove source IDs from discovery aliases, restrict redactions to indexed IDs, restrict pending cleanup to redacted IDs, prune redacted metadata and prompts, and normalize revisions.
The delivery-state function must normalize response and visible-echo IDs and clear `visible_echo_is_fallback` without an echo ID.
The dispatch-state function must accept positive non-boolean receipt orders and clear a settled STOP order that exceeds or lacks its admitted STOP order.
The command-state function must normalize result text and imply that execution started when a result exists.
The context-state function must normalize optional strings and retain only typed `HistoryScope` and `MessageTarget` values.

- [ ] **Step 2: Make `TurnRecord.create(...)` construct canonical fields directly**

Define `TurnRecord` as a frozen dataclass without `__post_init__`.
Keep the current public fields and defaults so consumers and persisted projections remain stable.
Have `create(...)` call the grouped pure functions and then invoke the generated constructor exactly once with canonical values.
Keep mapping values immutable through `MappingProxyType`.

- [ ] **Step 3: Move source metadata and record query behavior with the model**

Move `SourceEventMetadata`, `SourceEventRevision`, `_prompt_source_event_id`, `same_turn_identity`, `merge_edit_facts`, and the record query properties and methods into `turn_record.py`.
Keep compatibility imports in `handled_turns.py` so existing import sites continue to resolve while the implementation boundary becomes focused.

- [ ] **Step 4: Run the focused tests**

Run: `uv run pytest tests/test_handled_turns.py -k 'post_init or canonical or normalize or metadata or revision or redaction' -v`

Expected: PASS, including the new assertion that `TurnRecord` has no `__post_init__`.

- [ ] **Step 5: Commit the model extraction**

Run:

```bash
git add src/mindroom/turn_record.py src/mindroom/handled_turns.py tests/test_handled_turns.py
git commit -m "Extract canonical turn record model"
```

### Task 3: Make Invariant-Sensitive Updates Explicit

**Files:**
- Modify: `src/mindroom/turn_record.py`
- Modify: `src/mindroom/handled_turns.py`
- Modify: `src/mindroom/turn_store.py`
- Modify: `src/mindroom/edit_regenerator.py`
- Modify: `src/mindroom/text_ingress_dispatch.py`
- Modify: `src/mindroom/visible_response_reconciliation.py`
- Modify: `src/mindroom/command_turn_executor.py`
- Modify: `tests/test_handled_turns.py`
- Test: `tests/test_turn_store.py`
- Test: `tests/test_edit_regenerator.py`

- [ ] **Step 1: Write failing transition tests**

Add focused tests for a named `canonicalize_turn_record(...)` update boundary.

```python
def test_canonicalize_turn_record_prunes_new_redactions() -> None:
    record = TurnRecord.create(
        ["first", "second"],
        source_event_prompts={"first": "one", "second": "two"},
    )

    updated = canonicalize_turn_record(
        record,
        redacted_source_event_ids=("first",),
        pending_redaction_cleanup_event_ids=("first", "second"),
    )

    assert updated.redacted_source_event_ids == ("first",)
    assert updated.pending_redaction_cleanup_event_ids == ("first",)
    assert updated.source_event_prompts == {"second": "two"}


def test_canonicalize_turn_record_links_command_result_to_started_checkpoint() -> None:
    record = TurnRecord.create(["source"], command_execution_started=False)

    updated = canonicalize_turn_record(record, command_result_text="done")

    assert updated.command_execution_started is True
    assert updated.command_result_text == "done"
```

- [ ] **Step 2: Run the transition tests and verify they fail**

Run: `uv run pytest tests/test_handled_turns.py -k canonicalize_turn_record -v`

Expected: collection or import failure because `canonicalize_turn_record` does not exist.

- [ ] **Step 3: Implement an explicit typed update boundary**

Add `canonicalize_turn_record(record, **changes)` using a private sentinel for omitted fields.
The function must pass the complete effective field set through `TurnRecord.create(...)`, making re-normalization explicit at each call site.
Annotate supported keyword values with overloads or a typed parameter object so mypy rejects unknown fields and incompatible values.

- [ ] **Step 4: Replace invariant-sensitive record replacements**

Use `canonicalize_turn_record(...)` for replacements that change source IDs, discovery IDs, redaction IDs, prompts, revisions, source metadata, visible-echo linkage, receipt orders, or command checkpoint fields.
Keep `replace(...)` only for independent canonical fields such as a validated response ID, completion flag, timestamp, response owner, history scope, and conversation target.
Remove any `replace(...)` call whose correctness previously depended on `__post_init__` repairing another field.

- [ ] **Step 5: Run focused domain suites**

Run:

```bash
uv run pytest tests/test_handled_turns.py tests/test_turn_store.py tests/test_edit_regenerator.py tests/test_cancelled_response_hook.py tests/test_dispatch_obligations.py -q
```

Expected: PASS.

- [ ] **Step 6: Commit explicit transitions**

Run:

```bash
git add src/mindroom/turn_record.py src/mindroom/handled_turns.py src/mindroom/turn_store.py src/mindroom/edit_regenerator.py src/mindroom/text_ingress_dispatch.py src/mindroom/visible_response_reconciliation.py src/mindroom/command_turn_executor.py tests/test_handled_turns.py
git commit -m "Make turn record transitions explicit"
```

### Task 4: Verify Compatibility and Repository Quality

**Files:**
- Modify only files required by failures attributable to this refactor.

- [ ] **Step 1: Run all directly affected tests**

Run:

```bash
uv run pytest tests/test_handled_turns.py tests/test_turn_store.py tests/test_edit_regenerator.py tests/test_edit_response_regeneration.py tests/test_cancelled_response_hook.py tests/test_bot_reactions_approvals.py tests/test_dispatch_obligations.py tests/test_matrix_sync_tokens.py tests/test_turn_dispatch_pipeline.py tests/test_voice_command_processing.py -q
```

Expected: PASS.

- [ ] **Step 2: Run static checks on changed Python files**

Run:

```bash
uv run ruff check src/mindroom/turn_record.py src/mindroom/handled_turns.py src/mindroom/turn_store.py src/mindroom/edit_regenerator.py src/mindroom/text_ingress_dispatch.py src/mindroom/visible_response_reconciliation.py src/mindroom/command_turn_executor.py tests/test_handled_turns.py
uv run mypy src/mindroom/turn_record.py src/mindroom/handled_turns.py src/mindroom/turn_store.py
```

Expected: both commands exit successfully.

- [ ] **Step 3: Run import-boundary and full pre-commit verification**

Run:

```bash
uv run pytest tests/test_import_graph.py -q
uv run pre-commit run --all-files
```

Expected: PASS with no hook modifications left unstaged.

- [ ] **Step 4: Verify the final diff and structural success criteria**

Run:

```bash
git diff --check
git grep -n 'object.__setattr__' -- src/mindroom/turn_record.py src/mindroom/handled_turns.py
git status --short
```

Expected: `git diff --check` succeeds, the grep produces no `TurnRecord` construction assignments, and status contains only intentional changes plus the pre-existing untracked `.claude` artifacts.

- [ ] **Step 5: Commit final verification fixes**

Run:

```bash
git add src/mindroom tests docs/superpowers
git commit -m "Polish explicit turn record normalization"
```

Skip this commit when verification required no changes.

### Task 5: Publish the Pull Request and Validate Automation

**Files:**
- No planned source changes unless CI or AI review identifies a verified defect.

- [ ] **Step 1: Push the feature branch**

Run: `git push -u origin turn-record-explicit-normalization`

Expected: the remote branch is created successfully.

- [ ] **Step 2: Open a ready-for-review pull request**

Create a non-draft PR with a concise summary of the explicit normalization boundary, invariant-sensitive transition audit, schema compatibility, and verification commands.

- [ ] **Step 3: Wait for CI and AI review**

Poll the PR checks and review threads until required automation reaches a terminal state.

- [ ] **Step 4: Verify and address actionable findings**

Reproduce each reported defect against the code before changing it.
Fix only correct in-scope findings, rerun the proportional tests, commit, push, and wait for the replacement checks and AI review.

- [ ] **Step 5: Report the final PR URL and status**

Report the ready-for-review PR link, commits, verification evidence, and any intentionally rejected review findings.
