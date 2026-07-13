# ISSUE-239 Findings

## Root cause

The reported turn reached the Matrix callback at 05:55:21.210 but did not reach the response decision until 05:55:30.568, which accounts for the first approximately 9.3 seconds of the 19.7-second delay.

The latency-sensitive dispatch path awaited DM detection before asking `TurnPolicy` whether to respond at `src/mindroom/text_ingress_dispatch.py:133-151`.

On a cache miss, DM detection performed an unbounded `m.direct` account-data request at `src/mindroom/matrix/rooms.py:610-635` and could then perform an unbounded room-state request at `src/mindroom/matrix/rooms.py:667-716`.

The two nio response-validation warnings during the otherwise unlogged first gap are consistent with the room-state fallback, although the old logs did not identify which Matrix request consumed the time precisely enough to prove the individual subrequest after the fact.

The thread-history cache was not the cause because the incident recorded cache hits of only 7.4 ms and 20.6 ms.

After the response decision, `ResponseRunner` refreshed history and awaited payload preparation before the existing response attempt could create its placeholder at `src/mindroom/response_runner.py:868-883`.

Payload preparation serially built attachment-aware payload state and ran message and system enrichment hooks at `src/mindroom/response_payload_preparation.py:78-107`.

Attachment hydration can resolve thread attachment IDs, register historical Matrix media, resolve records, and materialize current media at `src/mindroom/inbound_turn_normalizer.py:351-418`, so old threads with large media histories can add work in this phase.

The incident's Dawarich enrichment request completed immediately before the 9.4-second payload-hydration phase ended, which is direct evidence that enrichment was on the pre-placeholder critical path for the second gap.

The old timing did not isolate memory, embedding, or knowledge-base work well enough to attribute part of the reported delay to those components.

No evidence implicated a knowledge-base or Chroma lookup in the measured gaps, while automatically extracted memory work may still add later startup latency but is now also protected by the earlier placeholder ordering.

The root cause was therefore two serial latency hazards before first visibility: unbounded network-backed DM detection before the response decision and potentially slow history, attachment, and hook hydration after the decision but before placeholder delivery.

## Fix

The normal text dispatch path now bounds DM lookup to one second at `src/mindroom/text_ingress_dispatch.py:52-54` and `src/mindroom/text_ingress_dispatch.py:133-138`.

`is_dm_room` now supports a caller-selected timeout and falls back to nio's in-memory two-member-room signal without caching the uncertain result at `src/mindroom/matrix/rooms.py:667-729`.

Room-cleanup callers retain the original unbounded behavior by omitting the optional timeout, so a transient lookup delay cannot cause them to leave a DM incorrectly.

Once the response lifecycle lock is acquired and the locked retry guard has approved the turn, agent and team responses now send a pending-stream placeholder before thread refresh and payload hydration at `src/mindroom/response_runner.py:957-1004`.

The ordering still avoids placeholders for empty prompts, stale restart retries, existing response events, and queued forced-compaction turns.

The early event ID is adopted by the existing streaming and delivery lifecycle, propagated into dispatch-failure finalization at `src/mindroom/turn_controller.py:1755-1763`, and converted to a terminal cancellation note if hydration is cancelled at `src/mindroom/response_runner.py:1005-1021`.

The fix adds structured phase timings for dispatch context at `src/mindroom/text_ingress_dispatch.py:155-164`, response payload building and hooks at `src/mindroom/response_payload_preparation.py:114-122`, and attachment hydration at `src/mindroom/inbound_turn_normalizer.py:419-430`.

Pipeline summaries now measure first visibility directly from lock acquisition and retain thread refresh as a diagnostic span at `src/mindroom/timing.py:63-89`.

## Validation

The fresh targeted command was `uv run pytest tests/test_dm_detection.py tests/test_queued_message_notify.py tests/test_response_payload_preparation.py tests/test_sync_restart_retry.py tests/test_timing.py tests/test_turn_dispatch_pipeline.py -x -n 0 --no-cov -v` inside `nix-shell shell.nix`.

The targeted run collected 163 tests and passed all 163 in 6.78 seconds on Python 3.13.13.

`tests/test_dm_detection.py:213-256` proves that latency-sensitive DM lookup times out to the in-memory fallback while the default cleanup behavior remains unbounded.

`tests/test_queued_message_notify.py:1597-1647` blocks payload preparation deliberately and proves that the placeholder is already visible while hydration remains pending.

`tests/test_queued_message_notify.py:1650-1763` verifies that early placeholders are preserved across setup failures and finalized on cancellation.

`tests/test_response_payload_preparation.py:206-220` verifies emission of response payload phase timing, and `tests/test_timing.py:445-507` verifies the revised additive pipeline segments and thread-refresh diagnostic.

The full suite was intentionally skipped at the user's direction because repeated attempts to access the Nix daemon from the sandbox had stalled or terminated prior sessions.

The user reported that targeted and adjacent regression suites had also passed in the prior sessions, but the 163-test run above is the validation rerun captured directly in this session.

## Confidence and limitations

Confidence is high that the new ordering removes slow payload, enrichment, memory, and model preparation from the pre-placeholder path because the ordering test holds hydration indefinitely after observing the sent placeholder.

Confidence is high that unbounded DM detection was a real dispatch-path latency defect and medium-high that it consumed the entire first incident gap because the historical logs lacked the new per-phase fields.

No hosted live replay of the original event was performed, so a future recurrence should be checked against the new `Dispatch context hydration phases`, `matrix_dm_detection_timed_out`, `Response payload hydration phases`, and `Attachment hydration phases` events for exact production attribution.
