## Summary

Top duplication candidates for `src/mindroom/timing.py` are repeated local elapsed-millisecond calculations and diagnostic timing event assembly in Matrix cache/history code.
`DispatchPipelineTiming` itself appears unique: no other source module keeps a named phase-mark map and emits a one-shot pipeline summary from configured span pairs.
The `timed` decorator's sync/async/async-generator wrapping behavior also appears centralized, with only unrelated decorator wrappers elsewhere.

## Coverage TSV

```coverage-tsv
qualname	kind	lines	status	search_terms	candidates_checked
_is_enabled	function	lines 31-32	related-only	MINDROOM_TIMING timing_enabled env gating	src/mindroom/matrix/cache/write_coordinator.py:495; src/mindroom/matrix/cache/write_coordinator.py:823; src/mindroom/matrix/cache/thread_writes.py:894
timing_enabled	function	lines 35-37	related-only	timing_enabled MINDROOM_TIMING callers	src/mindroom/matrix/cache/write_coordinator.py:495; src/mindroom/matrix/cache/write_coordinator.py:823; src/mindroom/matrix/cache/thread_writes.py:894; src/mindroom/matrix/cache/thread_writes.py:23
DispatchPipelineTiming	class	lines 80-131	none-found	DispatchPipelineTiming phase marks summary_emitted elapsed_ms	src/mindroom/ai.py:698; src/mindroom/turn_controller.py:433; src/mindroom/response_runner.py:325; src/mindroom/teams.py:1467; src/mindroom/history/runtime.py:78
DispatchPipelineTiming.mark	method	lines 89-92	none-found	marks label overwrite perf_counter phase boundary	src/mindroom/ai.py:698; src/mindroom/turn_controller.py:433; src/mindroom/response_lifecycle.py:214; src/mindroom/streaming.py:298
DispatchPipelineTiming.note	method	lines 94-98	related-only	metadata update skip None diagnostic metadata	src/mindroom/timing.py:185; src/mindroom/matrix/client_thread_history.py:389; src/mindroom/matrix/client_thread_history.py:525; src/mindroom/matrix/cache/write_coordinator.py:544
DispatchPipelineTiming.mark_first_visible_reply	method	lines 100-105	none-found	first_visible_reply first_visible_kind once mark	src/mindroom/streaming.py:298; src/mindroom/response_lifecycle.py:315; src/mindroom/response_runner.py:1409
DispatchPipelineTiming.elapsed_ms	method	lines 107-113	duplicate-found	elapsed_ms round perf_counter monotonic milliseconds	src/mindroom/history/runtime.py:87; src/mindroom/matrix/client_thread_history.py:331; src/mindroom/matrix/client_thread_history.py:390; src/mindroom/matrix/client_thread_history.py:526; src/mindroom/matrix/cache/thread_writes.py:767; src/mindroom/matrix/cache/write_coordinator.py:183
DispatchPipelineTiming.emit_summary	method	lines 115-131	related-only	duration_pairs summary logger.debug elapsed metrics	src/mindroom/matrix/cache/write_coordinator.py:551; src/mindroom/matrix/cache/thread_writes.py:807; src/mindroom/matrix/cache/thread_writes.py:843; src/mindroom/matrix/cache/thread_writes.py:864
create_dispatch_pipeline_timing	function	lines 134-140	none-found	create tracker mark message_received enabled	src/mindroom/turn_controller.py:87; src/mindroom/turn_controller.py:665; src/mindroom/timing.py:134
attach_dispatch_pipeline_timing	function	lines 143-152	related-only	store tracker in source dict internal key cast	src/mindroom/conversation_resolver.py:57; src/mindroom/dispatch_handoff.py:287; src/mindroom/response_attempt.py:92
get_dispatch_pipeline_timing	function	lines 155-163	related-only	read typed object from source dict key isinstance	src/mindroom/authorization.py:167; src/mindroom/conversation_resolver.py:49; src/mindroom/dispatch_handoff.py:212
event_timing_scope	function	lines 166-168	none-found	event_id slice 20 unknown timing scope	src/mindroom/coalescing.py:353; src/mindroom/turn_controller.py:665; src/mindroom/turn_controller.py:1415
emit_timing_event	function	lines 171-188	related-only	filter none timing_scope logger.debug event_data	src/mindroom/matrix/cache/thread_writes.py:272; src/mindroom/matrix/client_delivery.py:191; src/mindroom/tool_system/tool_hooks.py:398; src/mindroom/streaming_delivery.py:137
emit_elapsed_timing	function	lines 191-200	duplicate-found	emit elapsed timing label duration_ms monotonic round	src/mindroom/history/runtime.py:87; src/mindroom/matrix/cache/write_coordinator.py:179; src/mindroom/matrix/client_thread_history.py:390; src/mindroom/matrix/cache/thread_writes.py:814; src/mindroom/tool_system/tool_hooks.py:550
timed	function	lines 203-254	related-only	decorator sync async asyncgen elapsed timing wraps	src/mindroom/bot.py:130; src/mindroom/hooks/decorators.py:45; src/mindroom/tool_system/output_files.py:552; src/mindroom/tool_system/metadata.py:800
timed.<locals>.decorator	nested_function	lines 210-252	related-only	decorator returns wrapper when enabled functools wraps	src/mindroom/hooks/decorators.py:45; src/mindroom/tool_system/metadata.py:800; src/mindroom/tool_system/output_files.py:552
timed.<locals>.emit_timing	nested_function	lines 214-215	duplicate-found	inner emit elapsed label start kwargs timing_scope	src/mindroom/attachments.py:807; src/mindroom/attachment_media.py:85; src/mindroom/inbound_turn_normalizer.py:260; src/mindroom/turn_policy.py:142
timed.<locals>.async_generator_wrapper	nested_async_function	lines 220-227	none-found	inspect isasyncgenfunction async generator wrapper finally yield	src/mindroom/timing.py:217; src/mindroom/tool_system/output_files.py:552; src/mindroom/bot.py:142
timed.<locals>.async_wrapper	nested_async_function	lines 234-240	related-only	async wrapper finally await duration decorator	src/mindroom/tool_system/output_files.py:552; src/mindroom/bot.py:142
timed.<locals>.sync_wrapper	nested_function	lines 245-250	related-only	sync wrapper finally duration decorator	src/mindroom/tool_system/output_files.py:565; src/mindroom/tool_system/sandbox_proxy.py:947; src/mindroom/tool_system/sandbox_proxy.py:985
```

## Findings

### 1. Elapsed millisecond calculation is repeated outside `timing.py`

`DispatchPipelineTiming.elapsed_ms` centralizes `round((end - start) * 1000, 1)` for stored phase marks in `src/mindroom/timing.py:107`.
Similar local calculations are repeated in cache and thread-history paths:

- `src/mindroom/matrix/client_thread_history.py:331`, `src/mindroom/matrix/client_thread_history.py:390`, `src/mindroom/matrix/client_thread_history.py:526`, and `src/mindroom/matrix/client_thread_history.py:1010` use `time.perf_counter()` deltas rounded to one decimal for diagnostic `*_ms` fields.
- `src/mindroom/matrix/cache/write_coordinator.py:183`, `src/mindroom/matrix/cache/write_coordinator.py:537`, `src/mindroom/matrix/cache/write_coordinator.py:542`, and `src/mindroom/matrix/cache/write_coordinator.py:543` compute the same one-decimal millisecond values.
- `src/mindroom/matrix/cache/thread_writes.py:767`, `src/mindroom/matrix/cache/thread_writes.py:775`, `src/mindroom/matrix/cache/thread_writes.py:814`, and `src/mindroom/matrix/cache/thread_writes.py:815` repeat the same diagnostic conversion.
- `src/mindroom/history/runtime.py:87` has a local `_elapsed_ms` helper using `time.monotonic()` and integer milliseconds for compaction lifecycle notices.

The functional intent is the same: convert an elapsed monotonic/perf-counter interval to a `duration_ms` or `*_ms` diagnostic value.
Differences to preserve are rounding precision and whether the call receives a start timestamp or two timestamps.

### 2. One-off timing event assembly repeats the `emit_elapsed_timing` pattern

`emit_elapsed_timing` in `src/mindroom/timing.py:191` takes a start timestamp, computes rounded elapsed milliseconds, and sends a structured timing event through `emit_timing_event`.
Several call sites already use it, but lower-level cache paths still open-code the same pattern so they can emit several durations in one event:

- `src/mindroom/matrix/cache/write_coordinator.py:169` builds an idle-wait timing event with a local `wait_ms`.
- `src/mindroom/matrix/cache/write_coordinator.py:551` emits `"Event cache update timing"` with predecessor, run, and total durations.
- `src/mindroom/matrix/cache/thread_writes.py:807`, `src/mindroom/matrix/cache/thread_writes.py:843`, and `src/mindroom/matrix/cache/thread_writes.py:864` emit `"Live event cache append timing"` with locally computed duration fields.

These are related to `emit_elapsed_timing`, but not exact duplicates because each event contains multiple named intervals and additional outcome metadata.
The shared behavior is duration field calculation and structured debug emission behind `MINDROOM_TIMING`.

## Proposed Generalization

A small helper in `src/mindroom/timing.py` would be enough if this duplication is worth reducing:

1. Add `elapsed_ms_since(start: float, *, now: float | None = None, precision: int = 1) -> float` for the one-decimal diagnostic cases.
2. Optionally add `elapsed_ms_between(start: float, end: float, *, precision: int = 1) -> float` and have `DispatchPipelineTiming.elapsed_ms` call it.
3. Leave integer compaction lifecycle durations in `history/runtime.py` alone unless callers agree that `float` milliseconds are acceptable there.
4. Convert only cache/thread-history diagnostic fields that already use one-decimal floats.
5. Keep `emit_elapsed_timing` unchanged except to call the new helper internally.

No broader refactor is recommended.
There is no evidence that `DispatchPipelineTiming`, event-scope derivation, source-dict attachment, or the sync/async/async-generator decorator should be generalized further.

## Risk/tests

Risk is low for a helper-only refactor, but exact metric values can change if rounding precision or clock source changes.
Tests should cover `DispatchPipelineTiming.elapsed_ms`, `emit_elapsed_timing`, and at least one Matrix cache diagnostic path that emits multiple `*_ms` fields.
No production code was edited for this audit.
