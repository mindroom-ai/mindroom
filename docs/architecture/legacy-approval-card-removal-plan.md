# Legacy Approval Card Removal Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task.

**Goal:** Delete the verified-empty compatibility path for pre-continuation approval cards while preserving all native approval durability guarantees.

**Architecture:** Approval-card storage accepts only complete native continuation identity, and the approval manager therefore has one native resolution and recovery path.
Existing databases are not rewritten; strict application writes protect their already-upgraded nullable physical columns, while fresh schemas use non-null columns.

**Tech Stack:** Python 3.13, asyncio, SQLite, PostgreSQL, pytest, Matrix nio, and the existing event journal.

## Global Constraints

Keep Agno's persisted pause as the only execution approval boundary.

Do not alter continuation, source-journal, response-outbox, STOP, restart, or reload semantics.

Use `-n auto` for pytest.

Do not add compatibility fallbacks for deleted legacy behavior.

---

### Task 1: Make Native Card Identity Mandatory

**Files:**

- Modify: `tests/test_event_journal_store.py`
- Modify: `src/mindroom/event_journal/approvals.py`
- Modify: `src/mindroom/event_journal/schema.py`
- Modify: `src/mindroom/event_journal/sqlite_backend.py`
- Modify: `src/mindroom/event_journal/postgres_backend.py`
- Modify: `src/mindroom/event_journal/store.py`
- Modify: `src/mindroom/event_journal/views.py`

**Interfaces:**

- Consumes: current-format Matrix card content containing `continuation_id`, `continuation_generation`, and `tool_call_id`.
- Produces: `StoredApprovalCard` with required native identity and a claim operation that rejects incomplete identity.

- [ ] **Step 1: Write the failing storage test**

Add a backend-parameterized test that calls `PrincipalStore.claim_approval_card` with otherwise valid card content lacking the three native identity fields and asserts `ValueError`.

The production mutation this catches is accepting a card that no exact paused call owns.

Update `TestApprovalCards.card` so its normal fixture contains literal native identity:

```python
"continuation_id": f"continuation-{event_id.lstrip('$')}",
"continuation_generation": 0,
"tool_call_id": event_id.lstrip("$"),
```

Then add this negative case:

```python
async def test_approval_card_requires_native_identity(self, alice: PrincipalStore) -> None:
    card = self.card("$card")
    content = cast("dict[str, object]", card["content"])
    content.pop("tool_call_id")

    with pytest.raises(ValueError, match="native continuation identity"):
        await alice.claim_approval_card(room_id=ROOM, transaction_id="txn", card=card)
```

- [ ] **Step 2: Verify the test fails for the expected reason**

Run `uv run pytest tests/test_event_journal_store.py -k approval_card_requires_native_identity -q -n auto --no-cov`.

Expected result: the call succeeds instead of raising `ValueError`.

- [ ] **Step 3: Enforce complete native identity**

Make card identity extraction return a required typed tuple or raise `ValueError`.

Make `StoredApprovalCard` identity fields non-optional and reject rows whose stored identity is incomplete or disagrees with their card content.

Declare the three columns `NOT NULL` in the current table DDL.

- [ ] **Step 4: Remove the obsolete schema upgrade path**

Delete `approval_card_upgrade_statements`, its backend calls, its column-probe code, and the tests that construct pre-upgrade schemas.

- [ ] **Step 5: Verify storage behavior**

Run `uv run pytest tests/test_event_journal_store.py -q -n auto --no-cov --disable-warnings --maxfail=1`.

- [ ] **Step 6: Commit the storage boundary**

Commit with message `refactor: require native approval card identity`.

### Task 2: Delete Matrix-Only Resolution and Recovery

**Files:**

- Modify: `src/mindroom/approval_manager.py`
- Modify: `src/mindroom/event_journal/approvals.py`
- Modify: `src/mindroom/event_journal/store.py`
- Modify: `src/mindroom/event_journal/views.py`
- Modify: `tests/test_tool_approval.py`
- Modify: `tests/test_event_journal_store.py`

**Interfaces:**

- Consumes: only `StoredApprovalCard` values with complete native identity.
- Produces: one atomic continuation-resolution path for clicks, expiry, and startup recovery.

- [ ] **Step 1: Pin the native startup and action behavior**

Retain focused tests proving that startup redelivers a recorded native decision, unanswered native cards remain expiry-owned, and a human action commits the exact call.

Run those tests before deletion to establish the green characterization baseline.

Convert `test_a_restart_redelivers_a_decision_instead_of_expiring_it` to use complete native identity and `resolve_continuation_approval_card`.

Retain `test_a_restart_retires_a_card_whose_send_never_came_back`, `test_detached_card_uses_the_continuations_absolute_deadline`, `TestApprovalContinuations.test_card_decision_atomically_readies_the_exact_call`, `TestApprovalContinuations.test_late_approval_atomically_expires_the_call_and_card`, and `TestApprovalContinuations.test_duplicate_card_decision_preserves_the_first_winner`.

- [ ] **Step 2: Delete the generic decision API**

Remove `approvals.resolve`, `PrincipalStore.resolve_approval_card`, `ApprovalView.resolve_approval_card`, `_ApprovalManager._record_resolution`, and `_ApprovalManager._emit_resolution`.

- [ ] **Step 3: Collapse runtime branches to native cards**

Remove `_discard_matrix_only_card` and every branch selected by missing continuation identity.

Keep recorded-resolution redelivery because native decisions still need it after a failed Matrix edit.

Keep native expiry and atomic `resolve_continuation_approval_card` behavior unchanged.

- [ ] **Step 4: Delete legacy-only tests**

Remove tests for generic card-only decisions, malformed legacy pagination, cross-router Matrix-only settlement, and startup expiry of pre-continuation cards.

Do not weaken native crash-window, duplicate-decision, expiry, or restart assertions.

- [ ] **Step 5: Verify manager and store behavior**

Run `uv run pytest tests/test_tool_approval.py tests/test_event_journal_store.py -q -n auto --no-cov --disable-warnings --maxfail=1`.

- [ ] **Step 6: Commit runtime deletion**

Commit with message `refactor: delete legacy approval card settlement`.

### Task 3: Remove Legacy Card Payload Fields and Finish the PR

**Files:**

- Modify: `src/mindroom/approval_events.py`
- Modify: `src/mindroom/approval_manager.py`
- Modify: `tests/test_tool_approval.py`
- Modify: `docs/architecture/legacy-approval-card-removal.md`

**Interfaces:**

- Consumes: current native approval cards emitted by `create_detached_approval`.
- Produces: a smaller card parser and renderer without Dynamic Workflow waiter-era fields.

- [ ] **Step 1: Remove unused legacy payload fields**

Delete `workflow_id` and `participant_id` from `PendingApproval`, parsing, pending/resolved content builders, and event-body formatting.

Delete tests that only exercise those removed fields.

- [ ] **Step 2: Run focused approval verification**

Run `uv run pytest tests/test_event_journal_store.py tests/test_tool_approval.py tests/test_bot_reactions_approvals.py -q -n auto --no-cov --disable-warnings --maxfail=1`.

- [ ] **Step 3: Run complete verification**

Run `uv run pytest -q -n auto --disable-warnings --maxfail=1`.

Run `uv run pre-commit run --all-files`.

Run `git diff --check origin/main...HEAD`.

- [ ] **Step 4: Record the measured deletion**

Update the design document with exact production additions and deletions relative to `origin/main`.

- [ ] **Step 5: Commit and publish**

Commit remaining changes, push `refactor/remove-legacy-approval-cards`, and open a pull request that closes issue #1830.
