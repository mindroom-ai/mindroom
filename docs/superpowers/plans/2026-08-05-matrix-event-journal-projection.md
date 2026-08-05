# Matrix Event Journal and Conversation Projection Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Replace MindRoom's overlapping Matrix cache, callback-obligation, history-repair, and visible-delivery state machines with one durable ingress journal, one latest-visible-message projection, and one idempotent terminal-delivery outbox while preserving zero-loss recovery and fast conversation reconstruction.

**Architecture:** Nio remains the transport authority for `LIVE`, `RECOVERED`, and `HISTORY`, while MindRoom collapses provenance at durable admission into `ACTIONABLE` or `CONTEXT_ONLY`, projects each logical message into one current row, resumes actionable work from the journal after crashes, and retries initial and final Matrix delivery with deterministic transaction IDs.

**Tech Stack:** Python 3.13, mindroom-nio 0.36 or newer, asyncio, SQLite with WAL, PostgreSQL through psycopg, pytest, Hypothesis, Tuwunel, Synapse, and Matrix Client-Server APIs.

## Global Constraints

- The MindRoom work is one implementation branch and one implementation PR, even though it is split into reviewable commits below.
- The small recursive-relations prerequisite lands first as its own mindroom-nio PR because repositories cannot share an atomic commit.
- Do not merge an intermediate commit that contains both the old and new production paths.
- Do not add a feature flag, shadow writer, compatibility facade, dual-read fallback, or runtime switch between the old and new designs.
- New code may exist unused during an early commit on the unmerged branch, but the cutover commit must delete the replaced production path in the same commit.
- The final implementation diff must contain fewer production source lines than `origin/main` by at least 8,000 lines across the files in the removal ledger below.
- The target is a reduction of at least 10,000 production source lines, and missing that target requires a written explanation in the implementation PR.
- If the final reduction is below 8,000 lines, stop and redesign instead of asking reviewers to merge an architectural transition.
- If any third review round still finds a new correctness class, stop patching and reconsider the design before continuing.
- Nio is the only owner of transport provenance, limited-timeline recovery, room baselines, pagination safety, and callback redelivery.
- MindRoom must not infer recovery from pagination cursors, repeated membership events, timestamps, `limited`, `prev_batch`, or server-specific response shapes.
- MindRoom must map `LIVE` and `RECOVERED` to `ACTIONABLE` and must map `HISTORY` to `CONTEXT_ONLY` exactly once at admission.
- Code after durable admission must not branch on raw nio provenance.
- The no-loss guarantee begins after the bot completes its first successful baseline response and publishes readiness.
- Before readiness, historical events may populate context but must never create turns.
- After readiness, an actionable event must either become durably pending before nio accepts it or cause `nio.CallbackNotAcceptedError` so nio and the Matrix cursor remain retryable.
- A persistence failure must never advance the application-owned Classic Sync checkpoint.
- An unrecovered room must keep the bot unready and must not be converted into best-effort history.
- Conversation reads must not scan a room after that conversation has been hydrated once.
- Thread hydration must use one recursive event-relations traversal rooted at the thread event rather than room-wide `/messages` scans.
- Room-scoped conversations may perform one serialized initial `/messages` pagination, but the result must be installed atomically and all later reads must use the projection.
- A server that cannot provide Matrix v1.10 recursive relations must fail the strict thread-hydration contract rather than trigger a room-scan repair subsystem.
- Intermediate AI edit bodies and edit chains are not durable product data.
- A logical message has one projection row whose latest visible content is replaced in place.
- An out-of-order older edit must not overwrite a newer projected edit.
- An edit whose original has not arrived may occupy one latest-unresolved row for that target, but a second edit must replace that row instead of extending a chain.
- Redacting the current edit must trigger a point hydration of that logical message because the previous edit body is intentionally not retained.
- A strict conversation read must wait for that point hydration instead of serving content known to be stale.
- SQLite must have exactly one writer task per store, while reads may use separate read connections.
- PostgreSQL must implement the same contract and pass the same backend tests before the cutover commit is considered complete.
- The hot path must not create one SQLite connection per callback or per cache concern.
- Initial response sends and terminal response sends or edits must use a durable outbox and deterministic Matrix transaction IDs.
- Intermediate streaming edits may be coalesced in memory and may be lost during a crash because the next terminal update restores the latest visible state.
- An outbox payload becomes immutable when its first send attempt begins, because the homeserver may have accepted that transaction ID even if the client did not observe the response.
- The final response body must always be durably enqueued before delivery.
- Existing source redaction, authorization, E2EE metadata, media transcript, large-message sidecar, reaction, command, and room-membership semantics remain in scope.
- Backward-compatible cache schemas are out of scope, but the existing `SyncContinuityStore` checkpoint format remains readable so a stopped deployment can resume without dropping messages during cutover.
- Do not add production code to `bot.py` beyond construction, registration, lifecycle calls, and routing into focused collaborators.
- Use dataclasses and typed protocols for boundaries, and do not use `getattr()` or `hasattr()` to weaken nio or storage types.
- Every task below begins with a failing test or measurable failing check and ends with a focused commit.

---

## Why This Plan Is Executable

The current overlapping subsystem contains 20,248 production lines across `src/mindroom/matrix/cache/`, `client_thread_history.py`, `conversation_cache.py`, `dispatch_obligations/`, `handled_turns.py`, `turn_store.py`, the cold-history fence, and sync cache-trust modules.

The implementation is not complete merely when the new tables and interfaces work.

It is complete only when the old owners named in the removal ledger are gone, the final source budget is met, and the same crash and load tests prove the replacement.

The work stays on one unmerged branch so temporary scaffolding cannot become a second permanent architecture.

The implementation PR must contain this table with actual measured values before review.

| Gate | Required result |
| --- | --- |
| Actionable event durability | Zero lost or duplicate turns across every enumerated crash point. |
| Historical admission | `HISTORY` populates context and never starts a turn. |
| Restart recovery | The realistic `messages([], None)` restart case replies exactly once. |
| Edit storage | 10,000 edits of 100 logical messages leave 100 visible rows and at most 100 unresolved-edit rows. |
| Conversation query | `EXPLAIN` shows the room/thread/order index and no room-wide scan. |
| SQLite contention | Zero `database is locked` errors in the 50-thread stress run. |
| Delivery idempotency | Crashing after Matrix accepts an initial or final send creates one visible response. |
| Production source size | At least 8,000 net lines removed, with 10,000 lines as the target. |
| Competing paths | No old cache, dispatch-obligation, history-repair, or non-idempotent terminal-send fallback remains. |

If a gate fails, fix the design that owns the failure rather than adding a fallback around it.

## State Model

The storage package exposes four durable concepts and no generic cache API.

```python
class EventActionability(StrEnum):
    ACTIONABLE = "actionable"
    CONTEXT_ONLY = "context_only"


class JournalOutcome(StrEnum):
    PENDING = "pending"
    COMPLETED = "completed"
    INTENTIONALLY_IGNORED = "intentionally_ignored"


@dataclass(frozen=True, slots=True)
class MatrixEventEnvelope:
    principal_id: str
    room_id: str
    event_id: str
    event_kind: MatrixEventKind
    actionability: EventActionability
    event_source: dict[str, object]
    membership_epoch: int
    provenance: nio.TimelineEventProvenance


@dataclass(frozen=True, slots=True)
class ConversationKey:
    principal_id: str
    room_id: str
    thread_id: str | None
```

The `provenance` field is retained only in the journal's diagnostic data until the event settles.

The pending worker receives `actionability`, not raw provenance.

The public store contract is deliberately small.

```python
class MatrixEventStore(Protocol):
    async def admit(self, envelope: MatrixEventEnvelope) -> JournalAdmission: ...
    async def pending(self, *, limit: int) -> tuple[JournalEvent, ...]: ...
    async def settle(self, event_id: str, outcome: JournalOutcome) -> None: ...
    async def load_conversation(self, key: ConversationKey) -> ConversationProjection: ...
    async def install_hydration(self, hydration: ConversationHydration) -> None: ...
    async def enqueue_delivery(self, delivery: OutboxDelivery) -> None: ...
    async def pending_deliveries(self, *, limit: int) -> tuple[OutboxDelivery, ...]: ...
    async def mark_delivered(self, key: OutboxKey, event_id: str) -> None: ...
    async def mark_room_joined(self, room_id: str) -> int: ...
    async def mark_room_departed(self, room_id: str) -> int: ...
```

`JournalAdmission` returns the durable receipt order, whether the event was newly inserted, and whether actionable work remains pending.

It does not expose storage-specific rows or cache trust.

## SQL Shape

SQLite and PostgreSQL use equivalent constraints and indexes with backend-appropriate identity syntax.

```sql
CREATE TABLE matrix_event_journal (
    principal_id TEXT NOT NULL,
    event_id TEXT NOT NULL,
    room_id TEXT NOT NULL,
    event_kind TEXT NOT NULL,
    actionability TEXT NOT NULL,
    event_json TEXT,
    provenance TEXT,
    membership_epoch INTEGER NOT NULL,
    receipt_order INTEGER NOT NULL,
    outcome TEXT NOT NULL,
    retry_count INTEGER NOT NULL DEFAULT 0,
    PRIMARY KEY (principal_id, event_id)
);

CREATE INDEX matrix_event_journal_pending
ON matrix_event_journal(principal_id, outcome, receipt_order);

CREATE TABLE visible_messages (
    principal_id TEXT NOT NULL,
    room_id TEXT NOT NULL,
    logical_event_id TEXT NOT NULL,
    thread_id TEXT,
    sender TEXT NOT NULL,
    created_ts INTEGER NOT NULL,
    latest_event_id TEXT NOT NULL,
    latest_event_ts INTEGER NOT NULL,
    visible_json TEXT NOT NULL,
    needs_refresh INTEGER NOT NULL DEFAULT 0,
    redacted INTEGER NOT NULL DEFAULT 0,
    PRIMARY KEY (principal_id, room_id, logical_event_id)
);

CREATE INDEX visible_messages_conversation
ON visible_messages(principal_id, room_id, thread_id, created_ts, logical_event_id);

CREATE TABLE pending_message_edits (
    principal_id TEXT NOT NULL,
    room_id TEXT NOT NULL,
    logical_event_id TEXT NOT NULL,
    sender TEXT NOT NULL,
    edit_event_id TEXT NOT NULL,
    edit_ts INTEGER NOT NULL,
    edit_json TEXT NOT NULL,
    PRIMARY KEY (principal_id, room_id, logical_event_id)
);

CREATE TABLE conversation_hydration (
    principal_id TEXT NOT NULL,
    room_id TEXT NOT NULL,
    thread_id TEXT,
    membership_epoch INTEGER NOT NULL,
    complete INTEGER NOT NULL,
    PRIMARY KEY (principal_id, room_id, thread_id)
);

CREATE TABLE response_outbox (
    principal_id TEXT NOT NULL,
    turn_id TEXT NOT NULL,
    stage TEXT NOT NULL,
    transaction_id TEXT NOT NULL,
    room_id TEXT NOT NULL,
    target_event_id TEXT,
    content_json TEXT NOT NULL,
    attempted INTEGER NOT NULL DEFAULT 0,
    delivered_event_id TEXT,
    PRIMARY KEY (principal_id, turn_id, stage),
    UNIQUE (principal_id, transaction_id)
);
```

SQLite must represent an unthreaded conversation with a canonical empty storage key rather than depending on nullable primary-key equality.

The typed API continues to expose `thread_id: str | None` and performs the storage conversion in one helper.

## Projection Rules

- A non-edit message inserts one `visible_messages` row keyed by its own event ID.
- A valid same-sender `m.replace` event updates the target row only when `(origin_server_ts, event_id)` is newer than `(latest_event_ts, latest_event_id)`.
- A replacement never changes `created_ts`, `logical_event_id`, room, thread, or sender.
- An edit without an original upserts one `pending_message_edits` row using the same ordering comparison.
- Arrival of the original atomically applies and deletes the unresolved row when the sender matches.
- A redaction of the original tombstones the visible row.
- A redaction of the latest replacement marks the row `needs_refresh=1` and schedules a point hydration.
- A redaction of an already superseded replacement changes no visible state.
- Media transcription, large-message hydration, and normalized visible content update the same `visible_json` row.
- Once a context-only event is projected, its journal payload is deleted because replaying the projection is idempotent.
- Once an ignored own echo or intermediate AI edit is projected, its journal row is deleted because it can never create a turn.
- A terminal actionable source retains only its compact identity and outcome for deduplication, not its edit body or full event JSON.
- A pending actionable source retains the exact replay event JSON and E2EE security metadata until settlement.

## Deterministic Delivery Rules

Use UUIDv5 with a fixed MindRoom namespace and the string `principal_id:turn_id:stage` to derive the Matrix transaction ID.

The `stage` values are `initial` and `final`.

The initial row represents either the first substantive send or the placeholder send.

The final row represents the terminal send when no visible event exists or the terminal `m.replace` when one does.

The outbox row must be committed before calling `room_send`.

Retrying a row must pass its stored transaction ID through `send_message_result` or `edit_message_result`.

The row's content and target event ID become immutable when `attempted` changes to true.

Intermediate stream updates remain in the existing in-memory `StreamingResponse` coalescer and do not create outbox rows.

## Removal Ledger

The final implementation must delete or replace the following production owners.

| Current owner | Required final state |
| --- | --- |
| `src/mindroom/matrix/cache/` | Delete the 29-file cache package after moving only normalized projection behavior into `matrix/event_store/`. |
| `src/mindroom/matrix/client_thread_history.py` | Delete room-scan, refill, gap, snapshot, and repair logic after focused hydration exists. |
| `src/mindroom/matrix/conversation_cache.py` | Replace with a small `conversation_projection.py` reader and hydrator. |
| `src/mindroom/dispatch_obligations/` | Delete after journal admission and pending-worker replay own exact callbacks. |
| `src/mindroom/dispatch_admission.py` | Delete after actionability becomes part of journal admission. |
| `src/mindroom/cold_history_fence.py` | Delete after the provenance mapping happens in the admission adapter. |
| `src/mindroom/matrix/sync_cache_trust.py` | Delete cache-generation trust and retain only direct checkpoint publication. |
| `src/mindroom/matrix/sync_certification.py` | Delete cache certification and replace it with a response-accepted predicate. |
| `src/mindroom/turn_settlement_retry.py` | Delete after journal settlement is the pending worker's responsibility. |
| `src/mindroom/matrix/cache/write_coordinator.py` | Delete with the cache package and use one store writer. |
| Advisory `notify_outbound_*` paths | Delete after acknowledged sends update the projection through the event store. |
| Duplicated handled-source state | Remove from `handled_turns.py` and `turn_store.py` after the journal is the source identity owner. |

The implementation may retain AI-run metadata in `TurnStore`, but it must not retain a second pending-source, response-idempotency, or callback-replay state machine.

---

## Task 1: Freeze the Contract and the Deletion Budget

**Files:**

- Create: `tests/test_matrix_event_store_contract.py`
- Create: `tests/test_matrix_event_pipeline_crashes.py`
- Create: `tests/test_matrix_event_pipeline_performance.py`
- Modify: `tests/test_matrix_sync_continuity.py`
- Modify: `tests/test_streaming_e2e.py`

- [ ] **Step 1: Record the source baseline.**

Run:

```bash
wc -l \
  src/mindroom/matrix/cache/*.py \
  src/mindroom/matrix/client_thread_history.py \
  src/mindroom/matrix/conversation_cache.py \
  src/mindroom/dispatch_obligations/*.py \
  src/mindroom/handled_turns.py \
  src/mindroom/turn_store.py \
  src/mindroom/cold_history_fence.py \
  src/mindroom/dispatch_admission.py \
  src/mindroom/matrix/sync_cache_trust.py \
  src/mindroom/matrix/sync_certification.py \
  src/mindroom/matrix/sync_continuity.py
```

Expected: the current baseline totals 20,248 lines on commit `b639b6ef3`.

- [ ] **Step 2: Add table-driven crash-point tests.**

Cover these crash points for one actionable source.

1. Crash before journal commit.
2. Crash after journal commit but before nio records admission.
3. Crash after nio records admission but before the worker starts.
4. Crash after the worker records a pending turn but before model execution.
5. Crash after model execution but before final outbox commit.
6. Crash after final outbox commit but before Matrix accepts the transaction.
7. Crash after Matrix accepts the transaction but before the outbox records the event ID.
8. Crash after outbox acknowledgement but before journal settlement.

Assert that cases one through three replay the event, cases four through eight resume durable work, and every case produces one terminal turn and one visible response.

- [ ] **Step 3: Change the realistic restart regression to bounded exhaustion with no `end`.**

Keep the production sequence in `tests/test_matrix_sync_continuity.py` with a persisted baseline, a fresh `prev_batch`, exact unchanged own membership, and `messages([], None)`.

Assert that the initial-window message is `RECOVERED`, reaches durable actionable admission, and receives exactly one reply.

Keep negative tests for no baseline, missing or mismatched membership, pagination failure, repeated cursor, cap abandonment, and cold history.

- [ ] **Step 4: Add the edit compaction contract.**

Generate 10,000 ordered and shuffled edits across 100 logical messages.

Assert that the projection contains 100 visible rows, retains the newest body per message, stores no settled intermediate edit JSON, and uses at most 100 unresolved rows when originals are deliberately withheld.

- [ ] **Step 5: Add the indexed conversation-read contract.**

Create 500 visible messages in one thread and 10,000 messages in unrelated rooms and threads.

Assert exact chronological output and assert through `EXPLAIN QUERY PLAN` that SQLite uses `visible_messages_conversation`.

Add the PostgreSQL equivalent with `EXPLAIN (FORMAT JSON)` and assert an index scan over the conversation index.

- [ ] **Step 6: Add the outbox idempotency contract.**

Make the fake Matrix server accept the deterministic transaction and then raise a connection error before returning the event ID.

Restart the worker and assert that retrying the same transaction produces the same event ID and one visible event.

- [ ] **Step 7: Run the new tests and confirm they fail for missing interfaces.**

Run:

```bash
uv run pytest -q \
  tests/test_matrix_event_store_contract.py \
  tests/test_matrix_event_pipeline_crashes.py \
  tests/test_matrix_event_pipeline_performance.py \
  tests/test_matrix_sync_continuity.py \
  tests/test_streaming_e2e.py
```

Expected: the new store and pipeline tests fail because `mindroom.matrix.event_store` does not exist yet, while existing tests still pass outside the selected new cases.

- [ ] **Step 8: Commit the red contract.**

```bash
git add tests/test_matrix_event_store_contract.py \
  tests/test_matrix_event_pipeline_crashes.py \
  tests/test_matrix_event_pipeline_performance.py \
  tests/test_matrix_sync_continuity.py \
  tests/test_streaming_e2e.py
git commit -m "test: define durable Matrix event pipeline contract"
```

## Task 2: Implement the Storage-Neutral Domain and Projection Reducer

**Files:**

- Create: `src/mindroom/matrix/event_store/__init__.py`
- Create: `src/mindroom/matrix/event_store/types.py`
- Create: `src/mindroom/matrix/event_store/protocol.py`
- Create: `src/mindroom/matrix/event_store/projection.py`
- Create: `tests/test_matrix_event_projection.py`

- [ ] **Step 1: Write reducer tests before implementation.**

Cover original arrival, latest edit replacement, older delayed edit rejection, cross-sender edit rejection, edit-before-original, original arrival after edit, original redaction, current-edit redaction, superseded-edit redaction, media transcript update, and thread-order stability.

- [ ] **Step 2: Implement the typed domain.**

Add `EventActionability`, `MatrixEventKind`, `JournalOutcome`, `MatrixEventEnvelope`, `JournalAdmission`, `JournalEvent`, `ConversationKey`, `VisibleMessage`, `ConversationProjection`, `ConversationHydration`, `OutboxKey`, and `OutboxDelivery` as frozen slotted dataclasses or string enums.

Keep storage rows private to each backend.

- [ ] **Step 3: Implement pure projection decisions.**

Make `projection.py` a side-effect-free reducer that returns typed insert, replace, ignore, tombstone, unresolved-edit, or refresh-required decisions.

Do not import SQLite, PostgreSQL, nio clients, bot runtime objects, or Matrix network helpers into this module.

- [ ] **Step 4: Run the focused reducer tests.**

```bash
uv run pytest -q tests/test_matrix_event_projection.py
```

Expected: all projection reducer tests pass.

- [ ] **Step 5: Commit the domain and reducer.**

```bash
git add src/mindroom/matrix/event_store tests/test_matrix_event_projection.py
git commit -m "feat: define Matrix event journal and projection domain"
```

## Task 3: Implement One-Writer SQLite and PostgreSQL Stores

**Files:**

- Create: `src/mindroom/matrix/event_store/schema.py`
- Create: `src/mindroom/matrix/event_store/sqlite.py`
- Create: `src/mindroom/matrix/event_store/postgres.py`
- Create: `src/mindroom/matrix/event_store/writer.py`
- Modify: `tests/test_matrix_event_store_contract.py`
- Modify: `tests/conftest.py`

- [ ] **Step 1: Parameterize one backend contract.**

Run every journal, projection, hydration, membership, compaction, and outbox behavior against SQLite and PostgreSQL through the `MatrixEventStore` protocol.

Skip PostgreSQL only when the repository's standard PostgreSQL test fixture is unavailable.

- [ ] **Step 2: Implement the SQLite schema and one writer.**

Open one WAL-mode writer connection for the store lifetime and route typed write commands through one bounded asyncio queue.

Batch commands that are already concurrently waiting, but complete each caller only after the transaction containing its command commits.

Use separate short-lived or pooled read connections without creating additional writers.

- [ ] **Step 3: Implement atomic admission and projection.**

One admission transaction must verify the room membership epoch, insert or deduplicate the journal event, apply the projection reducer, compact context-only payloads when safe, and return `JournalAdmission`.

A failed statement must roll back the whole admission.

- [ ] **Step 4: Implement the PostgreSQL backend with identical semantics.**

Use a transaction and row constraints equivalent to SQLite.

Do not add PostgreSQL-only recovery behavior or a second protocol.

- [ ] **Step 5: Prove bounded edit storage and indexed reads.**

Run:

```bash
uv run pytest -q \
  tests/test_matrix_event_store_contract.py \
  tests/test_matrix_event_projection.py \
  tests/test_matrix_event_pipeline_performance.py
```

Expected: backend contracts, edit compaction, and query-plan assertions pass for every available backend.

- [ ] **Step 6: Commit the stores.**

```bash
git add src/mindroom/matrix/event_store tests/test_matrix_event_store_contract.py tests/conftest.py
git commit -m "feat: persist Matrix journal and visible projection"
```

## Task 4: Cut Ingress Over to the Journal and Delete Dispatch Obligations

**Files:**

- Create: `src/mindroom/matrix/event_ingress.py`
- Create: `src/mindroom/matrix/event_worker.py`
- Create: `tests/test_matrix_event_ingress.py`
- Modify: `src/mindroom/runtime_support.py`
- Modify: `src/mindroom/bot.py`
- Modify: `src/mindroom/turn_controller.py`
- Modify: `src/mindroom/dispatch_handoff.py`
- Modify: `src/mindroom/dispatch_replay_guard.py`
- Delete: `src/mindroom/dispatch_obligations/`
- Delete: `src/mindroom/dispatch_admission.py`
- Delete: `src/mindroom/cold_history_fence.py`
- Delete: `src/mindroom/turn_settlement_retry.py`
- Rewrite: `tests/test_dispatch_obligations.py` as journal and worker tests, then rename or delete it.

- [ ] **Step 1: Add exact provenance-mapping tests.**

Assert `LIVE -> ACTIONABLE`, `RECOVERED -> ACTIONABLE`, and `HISTORY -> CONTEXT_ONLY` for every supported message, media, reaction, approval, room-lifecycle, redaction, and decryption-failure event kind.

Assert that downstream handler inputs contain actionability but not raw provenance.

- [ ] **Step 2: Add the nio admission adapter.**

Register exactly one `AsyncClient.add_event_admission_callback` that builds `MatrixEventEnvelope`, awaits `store.admit`, and raises `nio.CallbackNotAcceptedError` on any durability or membership-epoch rejection.

The callback must not execute commands, turns, hooks, reactions, or visible delivery.

- [ ] **Step 3: Add the pending worker.**

Load pending rows by receipt order, claim one row only in memory, dispatch it through one typed event-kind mapping, settle it after the semantic owner commits, and leave it pending on cancellation or failure.

Startup must wake the worker before readiness can report healthy actionable processing.

No durable `running` state is needed because a crash must leave the row pending.

- [ ] **Step 4: Preserve typed replay inputs.**

Move only the exact event serialization and E2EE security metadata needed from `dispatch_obligations/events.py` into `event_ingress.py`.

Reject corrupt replay sources explicitly and do not invent fallback events.

- [ ] **Step 5: Wire the composition root.**

Make `runtime_support.py` construct one shared backend and one principal-bound view.

Make `bot.py` register the admission adapter, start and stop the pending worker, and route existing semantic handlers through its public methods.

Do not move classification, retries, or storage SQL into `bot.py`.

- [ ] **Step 6: Remove the old path in the same cutover commit.**

Delete dispatch obligations, the cold-history fence, dispatch admission, settlement retry, callback-kind retry scheduling, semantic-consumer claims, and their composition-root fields.

Remove tests that assert the deleted implementation rather than retained behavior.

- [ ] **Step 7: Run ingress, crash, and sync-continuity tests.**

```bash
uv run pytest -q \
  tests/test_matrix_event_ingress.py \
  tests/test_matrix_event_pipeline_crashes.py \
  tests/test_matrix_sync_continuity.py \
  tests/test_cold_history_fence.py \
  tests/test_dispatch_obligations.py
```

Expected: the new ingress and crash contracts pass, and deleted implementation tests are removed or rewritten rather than preserved through adapters.

- [ ] **Step 8: Commit the ingress cutover and deletion together.**

```bash
git add -A src/mindroom tests
git commit -m "refactor: replace dispatch obligations with event journal"
```

## Task 5: Replace Cache Repair with Direct Projection Reads and One-Time Hydration

**Files:**

- Create: `src/mindroom/matrix/conversation_projection.py`
- Create: `src/mindroom/matrix/conversation_hydration.py`
- Create: `tests/test_conversation_projection.py`
- Modify: `src/mindroom/conversation_resolver.py`
- Modify: `src/mindroom/matrix/reply_chain.py`
- Modify: `src/mindroom/reaction_dispatch.py`
- Modify: `src/mindroom/matrix/stale_stream_cleanup.py`
- Modify: `src/mindroom/hooks/sender.py`
- Modify: `src/mindroom/streaming.py`
- Modify: `src/mindroom/runtime_support.py`
- Modify: `src/mindroom/bot.py`
- Delete: `src/mindroom/matrix/cache/`
- Delete: `src/mindroom/matrix/client_thread_history.py`
- Delete: `src/mindroom/matrix/conversation_cache.py`
- Delete or rewrite: `tests/test_event_cache*.py`
- Delete or rewrite: `tests/test_thread_history.py`
- Delete or rewrite: `tests/test_bot_sync_event_cache.py`
- Delete or rewrite: `tests/test_matrix_event_cache*.py`

- [ ] **Step 1: Add conversation-reader behavior tests.**

Cover a hydrated thread, an unhydrated thread, concurrent first readers, an unthreaded room conversation, redacted content, media transcript content, large-message content, a current-edit redaction requiring point refresh, a leave/rejoin epoch, and a backend outage.

- [ ] **Step 2: Implement the projection reader.**

`ConversationProjectionReader.get_conversation` must perform one indexed store call and return the existing canonical resolved-message type expected by prompt assembly.

It must not know about gaps, snapshots, trust generations, refills, stale fallbacks, or write coordinators.

- [ ] **Step 3: Land the recursive-relations prerequisite in mindroom-nio.**

In `mindroom-ai/mindroom-nio`, add a typed `recurse: bool = False` parameter to `Api.room_get_event_relations` and `AsyncClient.room_get_event_relations`, encode `recurse=true` in the query only when requested, and cover pagination in `tests/async_client_test.py`.

Release the change, bump MindRoom's nio dependency, and do not add any batch-admission or application-storage behavior to nio.

The expected nio change is less than 50 production lines because the server and response iterator already implement recursive relation responses.

- [ ] **Step 4: Implement one-time hydration.**

For threads, fetch the root with `room_get_event`, paginate `room_get_event_relations` with `recurse=True` and no relation-type filter, reduce the returned thread replies and nested replacements into latest visible messages, and install the entire hydration in one membership-epoch-checked transaction.

The recursive response may contain a historical edit chain, but MindRoom must retain only the reducer's latest visible row and at most one unresolved edit per target.

For room-scoped conversations, perform one serialized `/messages` pagination and install it through the same transaction.

Use one in-flight task per `ConversationKey` so concurrent first readers share the network work.

Mark hydration complete only after pagination ends successfully and the installation commits.

- [ ] **Step 5: Define the homeserver contract.**

At startup, read `/versions` and require Matrix v1.10 or a successful recursive-relations feature probe for strict historical thread hydration.

If recursive relations are absent, report an actionable readiness failure instead of restoring full-room rescans or durable local edit chains.

Add live contract coverage for recursive relation results in Task 10.

- [ ] **Step 6: Replace production consumers.**

Change conversation resolution, reply-chain lookup, reaction lookup, stale-stream cleanup, hooks, and streaming thread targeting to use the projection reader's narrow methods.

Preserve one per-turn point-lookup memo only if a measured caller performs duplicate event lookups inside a turn.

- [ ] **Step 7: Delete the cache and repair path in the same cutover commit.**

Remove the 29-file cache package, room-scan history, snapshot replacement, gap markers, stale cache fallback, cache generations, advisory outbound writes, startup prewarm, and `EventCacheWriteCoordinator`.

Do not keep old tests green by recreating old methods on the new reader.

- [ ] **Step 8: Run projection and retained behavior tests.**

```bash
uv run pytest -q \
  tests/test_conversation_projection.py \
  tests/test_matrix_event_store_contract.py \
  tests/test_thread_history.py \
  tests/test_matrix_sync_continuity.py \
  tests/test_streaming.py \
  tests/test_bot_reactions_approvals.py
```

Expected: retained behavior passes through projection tests, while implementation-specific cache tests are deleted or reduced to backend contract tests.

- [ ] **Step 9: Check the first deletion gate.**

```bash
git diff --numstat origin/main...HEAD -- src/mindroom
find src/mindroom/matrix/event_store -name '*.py' -print0 | xargs -0 wc -l
```

Expected: production source is already net negative and no deleted cache module is imported.

- [ ] **Step 10: Commit the projection cutover and deletion together.**

```bash
git add -A src/mindroom tests
git commit -m "refactor: replace Matrix cache repair with visible projection"
```

## Task 6: Reduce Sync Continuity to Durable Admission and Checkpoint Publication

**Files:**

- Create: `src/mindroom/matrix/sync_checkpoint.py`
- Modify: `src/mindroom/matrix/sync_continuity.py`
- Modify: `src/mindroom/bot.py`
- Modify: `tests/test_matrix_sync_continuity.py`
- Delete: `src/mindroom/matrix/sync_cache_trust.py`
- Delete: `src/mindroom/matrix/sync_certification.py`

- [ ] **Step 1: Add a three-state checkpoint contract.**

Test cold baseline, established continuation, and failed response.

The cold baseline may persist context and then publish readiness, the established continuation may advance only after every admission commits, and a failed response must retain the prior checkpoint and reset nio's transient Classic state.

- [ ] **Step 2: Implement direct checkpoint ownership.**

Keep `SyncContinuityStore` as the crash-atomic file owner for the existing checkpoint and join-fence format.

Replace cache certification with a small `SyncCheckpointOwner` that receives response success, `unrecovered_room_ids`, and the highest admission receipt for diagnostics.

Do not attach a cache generation because the journal itself is durable and idempotent.

- [ ] **Step 3: Fail closed on unrecovered responses.**

If nio reports any unrecovered room after readiness, leave the prior checkpoint unchanged, reset the transient Classic sync state, keep the bot unready, and surface the exact room IDs in health diagnostics.

Do not reclassify those events in MindRoom.

- [ ] **Step 4: Delete cache trust and certification.**

Remove generation comparison, cache pending-write diagnostics, cache availability certification, cold-cache cleanup, and cache-driven checkpoint invalidation.

- [ ] **Step 5: Run continuity tests.**

```bash
uv run pytest -q tests/test_matrix_sync_continuity.py
```

Expected: baseline, restart recovery, unknown-position reset, persistence failure, cancellation, membership, and realistic `messages([], None)` tests pass without cache trust.

- [ ] **Step 6: Commit the checkpoint simplification.**

```bash
git add -A src/mindroom/matrix src/mindroom/bot.py tests/test_matrix_sync_continuity.py
git commit -m "refactor: gate Matrix checkpoints on durable admission"
```

## Task 7: Add the Durable Terminal Outbox and Coalesce Intermediate Edits

**Files:**

- Create: `src/mindroom/matrix/response_outbox.py`
- Create: `tests/test_response_outbox.py`
- Modify: `src/mindroom/matrix/client_delivery.py`
- Modify: `src/mindroom/delivery_gateway.py`
- Modify: `src/mindroom/streaming.py`
- Modify: `src/mindroom/response_attempt.py`
- Modify: `src/mindroom/visible_response_reconciliation.py`
- Modify: `tests/test_matrix_delivery.py`
- Modify: `tests/test_streaming_e2e.py`
- Modify: `tests/test_streaming_edits.py`

- [ ] **Step 1: Add deterministic transaction tests.**

Assert stable transaction IDs across processes, different IDs for initial and final stages, immutable payloads after first attempt, and one homeserver event after an accepted-but-unacknowledged retry.

- [ ] **Step 2: Thread transaction IDs through low-level delivery.**

Add a required transaction ID to the outbox-owned paths in `send_message_result` and `edit_message_result` while leaving explicitly non-outbox utilities typed and deliberate.

Ensure encrypted and cache-bypass sends use the supplied transaction ID instead of generating `uuid4()`.

- [ ] **Step 3: Enqueue before terminal delivery.**

Make the delivery gateway persist initial and final `OutboxDelivery` rows before network I/O, retry pending rows on startup, and mark them delivered with the returned event ID.

The pending worker may settle the source only after the final outbox row is durably acknowledged or deliberately terminal without a visible response.

- [ ] **Step 4: Keep only latest unsent intermediate content.**

Retain one in-memory pending streaming body and replace it when newer model output arrives before the next edit begins.

Once an edit attempt starts, freeze that payload until the attempt returns, then send only the newest accumulated body if it changed.

Do not insert intermediate edit bodies into the outbox or journal.

- [ ] **Step 5: Collapse visible response reconciliation.**

Replace duplicate pending-visible and retry state with outbox queries keyed by turn and stage.

Delete `visible_response_reconciliation.py` if no unique non-outbox behavior remains, or reduce it to a stateless adoption helper.

- [ ] **Step 6: Run delivery and streaming tests.**

```bash
uv run pytest -q \
  tests/test_response_outbox.py \
  tests/test_matrix_delivery.py \
  tests/test_streaming_e2e.py \
  tests/test_streaming_edits.py \
  tests/test_matrix_event_pipeline_crashes.py
```

Expected: duplicate-delivery crash cases pass, final content survives restart, and intermediate edits are coalesced without durable edit history.

- [ ] **Step 7: Commit the outbox cutover.**

```bash
git add -A src/mindroom tests
git commit -m "feat: deliver terminal Matrix responses through durable outbox"
```

## Task 8: Remove Duplicate Turn and Source State

**Files:**

- Modify: `src/mindroom/turn_store.py`
- Modify: `src/mindroom/handled_turns.py`
- Modify: `src/mindroom/dispatch_replay_guard.py`
- Modify: `src/mindroom/visible_response_reconciliation.py`
- Modify: `src/mindroom/sync_restart_retry.py`
- Modify: `tests/test_turn_store.py`
- Modify: `tests/test_handled_turns.py`
- Modify: `tests/test_turn_controller_focused.py`
- Modify: `tests/test_voice_command_processing.py`

- [ ] **Step 1: Inventory every remaining durable source identity.**

Run:

```bash
rg -n "source_event_id|pending.*turn|response_event_id|retry.*source|handled" \
  src/mindroom/turn_store.py \
  src/mindroom/handled_turns.py \
  src/mindroom/dispatch_replay_guard.py \
  src/mindroom/visible_response_reconciliation.py \
  src/mindroom/sync_restart_retry.py
```

Classify each field as AI-run metadata, journal-owned ingress state, or outbox-owned delivery state.

- [ ] **Step 2: Add ownership tests.**

Assert that the journal is the only durable pending-source owner, the outbox is the only durable delivery-intent owner, and `TurnStore` retains only model-execution and terminal business metadata.

- [ ] **Step 3: Remove duplicate owners.**

Delete handled-source tombstones, pending-visible intent, retry-source journals, and response-delivery reconciliation from `HandledTurnLedger` and `TurnStore` when the journal or outbox owns the same fact.

Delete entire modules when their last unique responsibility disappears.

- [ ] **Step 4: Preserve only necessary turn recovery.**

Keep the prompt, selected entity, model-run result, cancellation or redaction business outcome, and any facts required to avoid rerunning an already completed model call.

Reference journal receipt order and outbox keys rather than copying their state.

- [ ] **Step 5: Run turn recovery tests.**

```bash
uv run pytest -q \
  tests/test_turn_store.py \
  tests/test_handled_turns.py \
  tests/test_turn_controller_focused.py \
  tests/test_voice_command_processing.py \
  tests/test_matrix_event_pipeline_crashes.py
```

Expected: model execution and redaction behavior remain correct without a second source or delivery state machine.

- [ ] **Step 6: Commit the ownership cleanup.**

```bash
git add -A src/mindroom tests
git commit -m "refactor: remove duplicate Matrix turn durability state"
```

## Task 9: Enforce the Complexity and Performance Gates

**Files:**

- Modify: `tests/test_matrix_event_pipeline_performance.py`
- Create: `tests/manual/matrix_event_pipeline_stress.py`
- Modify: `docs/architecture/bot-runtime.md`
- Create: `docs/architecture/matrix-event-pipeline.md`

- [ ] **Step 1: Add deterministic operation-count assertions.**

Assert one indexed projection query per hydrated conversation read, one projection-row update per edit, no room-wide read after hydration, and no more than one unresolved row per logical message.

Prefer operation counts and query plans over timing thresholds in CI.

- [ ] **Step 2: Add the 50-thread stress harness.**

Drive 50 concurrent threads, 500 logical messages, 10,000 streaming edits, recovered events, media hydration, redactions, and periodic restarts through SQLite.

Record admission latency, writer queue latency, conversation-read latency, SQLite lock errors, duplicate turns, duplicate visible responses, unresolved journal rows, and database size.

- [ ] **Step 3: Define local acceptance thresholds.**

Require zero lost turns, zero duplicate turns, zero duplicate terminal responses, zero SQLite lock errors, zero room scans after hydration, p95 durable admission below 50 milliseconds, p95 hydrated conversation reads below 50 milliseconds, and p95 writer queue wait below 100 milliseconds on the standard development host.

Treat timing values as manual release gates and keep deterministic structural assertions in CI.

- [ ] **Step 4: Measure the source budget.**

Run:

```bash
git diff --numstat origin/main...HEAD -- src/mindroom | \
  awk '{ added += $1; deleted += $2 } END { print "added=" added, "deleted=" deleted, "net=" added-deleted }'
```

Expected: `net` is at most `-8000`, with `-10000` or lower as the target.

- [ ] **Step 5: Prove deleted owners are absent.**

Run:

```bash
test ! -d src/mindroom/matrix/cache
test ! -d src/mindroom/dispatch_obligations
test ! -e src/mindroom/matrix/client_thread_history.py
test ! -e src/mindroom/matrix/conversation_cache.py
test ! -e src/mindroom/cold_history_fence.py
test ! -e src/mindroom/matrix/sync_cache_trust.py
test ! -e src/mindroom/matrix/sync_certification.py
rg -n "EventCacheWriteCoordinator|ThreadCacheGap|DispatchObligation" src/mindroom && exit 1 || true
```

Expected: every deletion check passes and the symbol search is empty.

- [ ] **Step 6: Document the final ownership flow.**

Update the runtime architecture to show `nio -> journal/projection -> pending worker -> outbox -> Matrix` and state that intermediate edits are not retained.

Remove documentation for cache trust, gap repair, dispatch obligations, and advisory outbound cache updates.

- [ ] **Step 7: Run the manual stress harness.**

```bash
uv run python tests/manual/matrix_event_pipeline_stress.py --threads 50 --edits 10000 --restarts 5
```

Expected: every correctness and local performance threshold passes.

- [ ] **Step 8: Commit the gates and documentation.**

```bash
git add tests/test_matrix_event_pipeline_performance.py \
  tests/manual/matrix_event_pipeline_stress.py \
  docs/architecture/bot-runtime.md \
  docs/architecture/matrix-event-pipeline.md
git commit -m "test: enforce Matrix pipeline simplicity and performance"
```

## Task 10: Validate Against Real Tuwunel and Synapse

**Files:**

- Create: `tests/live/test_matrix_event_pipeline_live.py`
- Modify: `tests/manual/matrix_event_pipeline_stress.py`
- Modify: `docs/architecture/matrix-event-pipeline.md`

- [ ] **Step 1: Run the realistic restart recovery case against Tuwunel.**

Persist baseline `w1`, stop MindRoom, send a message, restart with `pos=None`, return the missed message and unchanged own-membership event in the initial snapshot with `prev_batch=w2`, and make `/messages?from=w1&to=w2&dir=f` return an empty chunk with no `end`.

Assert provenance is `RECOVERED`, journal actionability is `ACTIONABLE`, the turn completes once, and the response is visible once.

- [ ] **Step 2: Run the same case against Synapse.**

Use the MindRoom Synapse compact-edit configuration and require the same observable result.

- [ ] **Step 3: Validate cold history.**

Start with no persisted baseline and assert that initial history hydrates the projection, creates no turn, establishes the baseline, and publishes readiness only after the journal commits.

- [ ] **Step 4: Validate edit-heavy projection.**

Stream an AI response with updates every 500 milliseconds, restart during the stream, allow the terminal response to finish, and assert one logical projected message with final content and no durable intermediate edit bodies.

- [ ] **Step 5: Validate homeserver recursive hydration.**

Create a thread with many edits, hydrate it through recursive relations on both servers, and assert that MindRoom reduces the relation tree to latest visible content without a room scan or retained edit chain.

- [ ] **Step 6: Validate deployment cutover.**

Stop the old runtime only after its current callbacks drain, preserve the existing continuity checkpoint, start the new runtime on that checkpoint, send a message during downtime, and assert that Matrix recovery admits and answers it once.

- [ ] **Step 7: Run the live suite.**

```bash
uv run pytest -q tests/live/test_matrix_event_pipeline_live.py
```

Expected: Tuwunel and Synapse pass the same no-loss, history, edit-compaction, hydration, and idempotent-delivery contract.

- [ ] **Step 8: Commit live coverage.**

```bash
git add tests/live/test_matrix_event_pipeline_live.py \
  tests/manual/matrix_event_pipeline_stress.py \
  docs/architecture/matrix-event-pipeline.md
git commit -m "test: verify Matrix event pipeline on deployed servers"
```

## Task 11: Final Verification and Review Stop Gate

**Files:**

- Modify only files required by verified failures.

- [ ] **Step 1: Run formatting, linting, typing, and focused tests.**

```bash
uv run pre-commit run --all-files
uv run pytest -q \
  tests/test_matrix_event_store_contract.py \
  tests/test_matrix_event_projection.py \
  tests/test_matrix_event_ingress.py \
  tests/test_matrix_event_pipeline_crashes.py \
  tests/test_matrix_event_pipeline_performance.py \
  tests/test_conversation_projection.py \
  tests/test_response_outbox.py \
  tests/test_matrix_sync_continuity.py \
  tests/test_streaming_e2e.py \
  tests/test_turn_store.py
```

Expected: every command exits successfully.

- [ ] **Step 2: Run the full test suite.**

```bash
uv run pytest -q
```

Expected: the full suite passes.

- [ ] **Step 3: Re-run live and stress gates.**

Run Task 9's 50-thread stress command and Task 10's live Tuwunel and Synapse suite from the final commit.

Expected: every threshold and real-server contract still passes.

- [ ] **Step 4: Audit for two owners of one fact.**

Search journal state, turn state, delivery state, edit content, hydration completeness, checkpoint state, membership epoch, and retry ownership.

For each fact, record exactly one production owner in the implementation PR description.

- [ ] **Step 5: Recalculate the final deletion budget.**

Run Task 9's source-budget command from the final commit.

Expected: at least 8,000 net production lines are removed.

- [ ] **Step 6: Apply the stop gate.**

Do not request merge if a compatibility path remains, the deletion gate fails, a crash case duplicates or loses a turn, a live server needs a MindRoom provenance guess, or an intermediate edit body remains durable.

- [ ] **Step 7: Commit only verified cleanup if needed.**

```bash
git add -A
git commit -m "chore: finish Matrix event pipeline simplification"
```

## Nio Scope Decision

The core implementation requires one narrow mindroom-nio change to expose the Matrix v1.10 `recurse` query parameter on the existing event-relations iterator.

Nio 0.36 already supplies provenance, persisted baselines, limited-timeline recovery, admission rejection, and Classic response acknowledgement, so none of those areas should change.

The recursive-relations change should remain below 50 production lines plus focused tests and must land as its own nio PR before Task 5 cuts over MindRoom history reads.

Do not add a nio batch-admission API during this implementation.

First remove room scans, repeated repair, competing SQLite writers, and durable intermediate edits, then measure the remaining admission cost.

Open a separate mindroom-nio proposal only if the final 50-thread and 200-event recovery profiles show that per-event durable callback commits consume more than 20 percent of recovery wall time or violate the 50-millisecond admission p95.

That proposal must preserve per-event room-state callback semantics and cannot be assumed to fit the earlier 250-to-500-line estimate until a red nio test demonstrates a safe batch boundary.

This condition prevents a speculative transport API from becoming another permanent layer before the MindRoom simplification proves it is needed.

## Expected Final Shape

The intended production flow is:

```text
nio timeline recovery and provenance
  -> MatrixEventIngress durable admission
      -> matrix_event_journal pending source
      -> visible_messages latest-state projection
  -> MatrixEventWorker turn execution
  -> response_outbox deterministic initial/final delivery
  -> Matrix
```

Conversation reconstruction is one indexed projection read after at most one conversation hydration.

Intermediate AI edits exist only in Matrix transport and the in-memory stream coalescer.

The journal owns pending input, the projection owns visible context, `TurnStore` owns model-run facts, the outbox owns terminal delivery intent, nio owns recovery provenance, and `SyncContinuityStore` owns the last accepted application checkpoint.

No other module may own a second copy of those facts.
