Summary: No meaningful duplication found.

`TurnRecorder` is a narrow mutable accumulator for one live top-level turn.
Nearby modules contain related immutable state carriers, streaming attempt state, and interrupted replay persistence helpers, but they do not duplicate the recorder's live lifecycle role.
The only overlapping behavior is very small value-copying and event-id normalization logic, which is too local and low-value to generalize.

```coverage-tsv
qualname	kind	lines	status	search_terms	candidates_checked
TurnRecorder	class	lines 17-121	related-only	TurnRecorder record turn recorder interrupted replay assistant_text completed_tools interrupted_tools outcome	src/mindroom/response_runner.py:425; src/mindroom/ai.py:216; src/mindroom/handled_turns.py:62; src/mindroom/turn_store.py:40; src/mindroom/history/interrupted_replay.py:55
TurnRecorder.set_run_metadata	method	lines 30-32	related-only	set_run_metadata run_metadata dict metadata materialize matrix run metadata	src/mindroom/response_runner.py:163; src/mindroom/ai_run_metadata.py:213; src/mindroom/teams.py:1139
TurnRecorder.set_run_id	method	lines 34-36	none-found	set_run_id run_id or None attempt_run_id recorder.run_id	none
TurnRecorder.set_response_event_id	method	lines 38-40	related-only	set_response_event_id response_event_id normalized_response_event_id normalized_event_id response_id	src/mindroom/commands/handler.py:175; src/mindroom/handled_turns.py:144; src/mindroom/handled_turns.py:1054; src/mindroom/turn_store.py:285
TurnRecorder.set_assistant_text	method	lines 42-44	related-only	set_assistant_text assistant_text StreamingAttemptState assistant text	src/mindroom/ai.py:216; src/mindroom/response_runner.py:523
TurnRecorder.set_completed_tools	method	lines 46-48	related-only	set_completed_tools completed_tools list completed trace	src/mindroom/ai.py:216; src/mindroom/ai.py:500; src/mindroom/teams.py:2101
TurnRecorder.set_interrupted_tools	method	lines 50-52	related-only	set_interrupted_tools interrupted_tools pending_tools trace_entry	src/mindroom/ai.py:1728; src/mindroom/teams.py:2101; src/mindroom/history/interrupted_replay.py:99
TurnRecorder.sync_partial_state	method	lines 54-66	related-only	sync_partial_state partial state assistant_text completed_tools interrupted_tools run_metadata	src/mindroom/teams.py:2101; src/mindroom/response_runner.py:523
TurnRecorder.record_completed	method	lines 68-80	related-only	record_completed mark_completed completed_tools assistant_text run_metadata	src/mindroom/teams.py:1801; src/mindroom/teams.py:2340; src/mindroom/teams.py:2446
TurnRecorder.record_interrupted	method	lines 82-95	related-only	record_interrupted mark_interrupted interrupted_tools completed_tools cancelled run	src/mindroom/ai.py:1728; src/mindroom/teams.py:2298; src/mindroom/teams.py:2464; src/mindroom/response_runner.py:523
TurnRecorder.mark_completed	method	lines 97-99	none-found	mark_completed outcome completed	none
TurnRecorder.mark_interrupted	method	lines 101-103	related-only	mark_interrupted outcome interrupted ensure_recorder_interrupted	src/mindroom/response_runner.py:473
TurnRecorder.interrupted_snapshot	method	lines 105-114	related-only	interrupted_snapshot build_interrupted_replay_snapshot user_message partial_text response_event_id	src/mindroom/history/interrupted_replay.py:211; src/mindroom/history/interrupted_replay.py:286; src/mindroom/response_runner.py:443
TurnRecorder.claim_interrupted_persistence	method	lines 116-121	none-found	claim_interrupted_persistence interrupted_persisted persist once interrupted recorder	none
```

Findings:

No real duplication requiring refactor was found.

Related-only candidates checked:

- `src/mindroom/response_runner.py:425` builds and owns a `TurnRecorder`, and `src/mindroom/response_runner.py:443` persists its interrupted snapshot exactly once.
  This is recorder orchestration, not a duplicate recorder implementation.
- `src/mindroom/ai.py:216` has `_StreamingAttemptState`, which also carries `assistant_text` and tool trace lists.
  It is lower-level streaming parser state with provider metrics, pending tool matching, retry flags, and exceptions.
  It feeds `TurnRecorder` on cancellation at `src/mindroom/ai.py:1728`, so extracting a shared state object would mix different lifecycle layers.
- `src/mindroom/history/interrupted_replay.py:211` builds immutable `InterruptedReplaySnapshot` instances, and `src/mindroom/history/interrupted_replay.py:286` offers a stateless persistence wrapper.
  `TurnRecorder.interrupted_snapshot` delegates to this canonical builder instead of duplicating snapshot normalization.
- `src/mindroom/handled_turns.py:62` and `src/mindroom/turn_store.py:40` are durable or immutable turn metadata carriers.
  They overlap in names such as `response_event_id`, but they model handled source-event ledger state and edit regeneration metadata, not live assistant response accumulation.
- `src/mindroom/response_runner.py:163`, `src/mindroom/commands/handler.py:175`, and `src/mindroom/handled_turns.py:1054` contain small metadata/event-id normalization helpers related to `set_run_metadata` and `set_response_event_id`.
  The logic is similar in shape but intentionally scoped to different input types and legacy handling needs.

Proposed generalization: No refactor recommended.

The possible shared helpers would only wrap `dict(x) if x is not None else None`, `list(x)`, or `value or None`.
Those extra abstractions would add indirection without reducing meaningful duplicated behavior.
The existing delegation from `TurnRecorder.interrupted_snapshot` to `build_interrupted_replay_snapshot` is already the important generalization.

Risk/tests:

- No production code was changed.
- If a future refactor touches this area, focus tests on interrupted streaming persistence, cancellation during team responses, delivery-error replay snapshots, and response event ID propagation.
- Existing relevant coverage should include recorder unit tests if present plus integration tests around `ResponseRunner._persist_interrupted_turn`, `ai.stream_agent_response` cancellation, and team cancellation paths.
