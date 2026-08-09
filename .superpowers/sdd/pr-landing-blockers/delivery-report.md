# Delivery recovery ordering implementation report

## RED evidence

After `uv sync --all-extras`, the exact initial RED command was `uv run pytest tests/test_event_journal_crash_matrix.py::TestInitialAndFinalStages::test_live_final_cannot_overtake_initial_recovery_before_matrix_acceptance tests/test_event_journal_crash_matrix.py::TestInitialAndFinalStages::test_failed_initial_without_a_final_remains_recoverable -n 0 --no-cov -q`.

It failed on SQLite and PostgreSQL because the FINAL task completed while recovery's INITIAL request was paused before Matrix acceptance, with `AssertionError: FINAL became visible while recovery's earlier INITIAL was paused before Matrix acceptance`.

The exact reverse-order RED command was `uv run pytest tests/test_event_journal_crash_matrix.py::TestInitialAndFinalStages::test_initial_cannot_become_visible_after_final_owns_delivery -n 0 --no-cov -q`.

It failed on SQLite and PostgreSQL because the stale INITIAL became `$sent1` after FINAL completed instead of returning `None`.

The exact callback-boundary RED command was `uv run pytest tests/test_response_delivery_gateway.py::TestTurnDeliverySerialization::test_terminal_callback_can_reenter_the_same_turn -n 0 --no-cov -q`.

It failed on SQLite and PostgreSQL with `Failed: terminal callback deadlocked on the turn's visible-delivery lock`.

## Design

`DeliveryGateway` owns a weakly held `asyncio.Lock` per turn and shares that map with every `ResponseDelivery` it constructs, so unrelated turn and room deliveries remain independent.

`ResponseDelivery.deliver()`, `flush()`, and each recovered row serialize enqueue or supersession check, claim, device recording, Matrix send, and acknowledgement for the same turn.

The FINAL-exists check now runs inside the locked flush path for every INITIAL entry point, so recovery, live delivery, and direct flush cannot make a stale placeholder newly visible once FINAL owns delivery.

A failed INITIAL whose Matrix request was never accepted releases the lock and keeps its unacknowledged outbox row recoverable when no FINAL exists.

Terminal-record publication runs after the visible-delivery lock is released, preserving acknowledgement ownership while avoiding callback re-entry deadlocks.

The durable outbox rows, frozen payloads, deterministic transaction IDs, device-adoption checks, membership fence, separate INITIAL and FINAL states, and first-writer-wins acknowledgement remain unchanged.

## Files changed

- `src/mindroom/delivery_gateway.py` owns and shares the weak per-turn lock map.
- `src/mindroom/response_delivery.py` serializes turn delivery, suppresses stale INITIAL sends under that serialization, and moves post-acknowledgement publication outside the lock.
- `tests/test_event_journal_crash_matrix.py` adds fake-Matrix adversarial ordering and failed-INITIAL recovery coverage on SQLite and PostgreSQL.
- `tests/test_response_delivery_gateway.py` covers two gateway-created delivery instances, cancellation release, and terminal callback re-entry on SQLite and PostgreSQL.

## Verification

- `uv sync --all-extras` completed successfully.
- `uv run pytest tests/test_event_journal_crash_matrix.py tests/test_response_delivery_gateway.py tests/test_locked_turn_delivery.py tests/test_recoverable_text_delivery.py tests/test_turn_delivery_handoff.py -n 0 --no-cov -q` passed all 149 focused delivery tests.
- `uv run ruff check src/mindroom/response_delivery.py src/mindroom/delivery_gateway.py tests/test_event_journal_crash_matrix.py tests/test_response_delivery_gateway.py` passed.
- `uv run ruff format --check src/mindroom/response_delivery.py src/mindroom/delivery_gateway.py tests/test_event_journal_crash_matrix.py tests/test_response_delivery_gateway.py` passed.
- `uv run ty check src/mindroom/response_delivery.py src/mindroom/delivery_gateway.py tests/test_event_journal_crash_matrix.py tests/test_response_delivery_gateway.py` passed.
- `uv run pre-commit run --all-files` passed every hook, including Tach boundary enforcement.
- `uv run pytest -n auto --no-cov -q` completed the full repository suite at 100% with exit code 0.
- `git diff --check` passed.

## LOC

Production code changed by 99 insertions and 44 deletions, for a net increase of 55 lines.

Test code changed by 242 insertions and no deletions.

## Remaining concerns

The serialization is process-local to the existing `DeliveryGateway`, so it assumes one active gateway owns a principal's live and recovery delivery in a process, while the existing durable transaction-ID and acknowledgement rules continue to arbitrate cross-process retries.

The full suite emitted existing third-party deprecation or syntax warnings and existing pytest marker warnings, but no test failures.
