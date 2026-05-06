## Summary

No meaningful duplication found.

`src/mindroom/streaming_warmup.py` owns a small, cohesive state machine for worker warmup side-band lines.
The closest related behavior is stream error-note shortening in `src/mindroom/streaming.py`, visible tool marker rendering in `src/mindroom/tool_system/events.py`, warmup suffix stripping in `src/mindroom/matrix/visible_body.py`, and worker progress emission in `src/mindroom/workers/backends/kubernetes.py`.
Those candidates share formatting or progress vocabulary, but they do not duplicate the warmup module's active-worker state transitions.

## Coverage TSV

```coverage-tsv
qualname	kind	lines	status	search_terms	candidates_checked
_shorten_warmup_error	function	lines 14-19	related-only	"_shorten_warmup_error, split join normalize error, truncate error, len 180, warmup error"	src/mindroom/streaming.py:145; src/mindroom/tool_system/events.py:97; src/mindroom/tool_system/tool_calls.py:247
_ActiveWarmup	class	lines 23-29	none-found	"_ActiveWarmup, active_warmups, worker warmup state, WorkerReadyProgress storage"	src/mindroom/streaming.py:303; src/mindroom/streaming_delivery.py:234; src/mindroom/workers/models.py:41
RenderedWarmupLine	class	lines 33-37	none-found	"RenderedWarmupLine, rendered line text html, warmup line html"	src/mindroom/streaming.py:835; src/mindroom/matrix/message_content.py:1; src/mindroom/tool_system/events.py:297
_render_tool_labels	function	lines 40-44	related-only	"_render_tool_labels, code escape label, tool marker line, safe_tool_name, format tool label"	src/mindroom/tool_system/events.py:297; src/mindroom/tool_system/events.py:335; src/mindroom/streaming.py:831
_render_worker_status_line	function	lines 47-77	none-found	"Preparing isolated worker, Worker startup failed, cold_start waiting elapsed, warmup status line"	src/mindroom/workers/backends/kubernetes.py:72; src/mindroom/workers/backends/kubernetes.py:114; src/mindroom/streaming.py:804
WorkerWarmupState	class	lines 81-167	none-found	"WorkerWarmupState, active_warmups, needs_warmup_clear_edit, worker progress event state"	src/mindroom/streaming.py:303; src/mindroom/streaming_delivery.py:343; src/mindroom/tool_system/runtime_context.py:159
WorkerWarmupState.clear_for_terminal_transition	method	lines 88-92	related-only	"clear_for_terminal_transition, terminal transition clear warmup, finalize warmup clear"	src/mindroom/streaming.py:503; src/mindroom/streaming.py:513; src/mindroom/streaming.py:860
WorkerWarmupState.note_nonterminal_delivery	method	lines 94-97	related-only	"note_nonterminal_delivery, last_send_had_warmup_suffix, had_warmup_suffix, nonterminal delivery"	src/mindroom/streaming.py:258; src/mindroom/streaming.py:767; src/mindroom/streaming.py:860
WorkerWarmupState.clear_terminal_failures	method	lines 99-105	related-only	"clear_terminal_failures, failed warmups, phase failed active_warmups, terminal failures"	src/mindroom/streaming.py:468; src/mindroom/streaming_delivery.py:234; src/mindroom/streaming_delivery.py:238
WorkerWarmupState._clear_failed_retry_duplicates	method	lines 107-122	none-found	"clear failed retry duplicates, stale failed worker keys, same tool retry warmup"	src/mindroom/streaming_delivery.py:343; src/mindroom/tool_system/events.py:405; src/mindroom/workers/backends/kubernetes.py:314
WorkerWarmupState.render_lines	method	lines 124-132	related-only	"render_lines warmup suffix, render active notices, warmup_suffix_lines"	src/mindroom/streaming.py:804; src/mindroom/streaming.py:835; src/mindroom/matrix/visible_body.py:18
WorkerWarmupState.apply_event	method	lines 134-167	related-only	"apply_event WorkerProgressEvent, progress phase ready failed waiting, worker progress event routing"	src/mindroom/streaming.py:890; src/mindroom/streaming_delivery.py:343; src/mindroom/tool_system/sandbox_proxy.py:720; src/mindroom/workers/backends/kubernetes.py:72
```

## Findings

No real duplication found.

Related-only candidates:

- `src/mindroom/streaming.py:145` normalizes whitespace and truncates stream exception text, similar to `_shorten_warmup_error`.
  It is not a duplicate because it formats a terminal stream failure note with a different default, limit, and wrapper text.
- `src/mindroom/tool_system/events.py:297` renders visible inline tool markers with backtick escaping and mention neutralization, while `_render_tool_labels` renders warmup labels in both plain text and Matrix HTML.
  The shared concern is label display, but the call sites have different output contracts.
- `src/mindroom/workers/backends/kubernetes.py:72` and `src/mindroom/workers/backends/kubernetes.py:114` create worker progress events consumed by `WorkerWarmupState.apply_event`.
  They are producer-side progress scheduling, not duplicate consumer-side state management.
- `src/mindroom/streaming.py:804` appends rendered warmup lines to outbound Matrix content, and `src/mindroom/matrix/visible_body.py:18` strips those suffixes when reconstructing canonical visible text.
  These are paired read/write operations around the same metadata, not duplicate rendering or state logic.

## Proposed Generalization

No refactor recommended.

The warmup module already centralizes the active warmup state and rendering behavior.
Extracting a shared truncation helper for `_shorten_warmup_error` and `_format_stream_error_note` would add an abstraction for two call sites with different limits, defaults, and surrounding text.
Extracting a shared tool-label renderer would also risk mixing Matrix HTML warmup formatting with visible tool-marker formatting.

## Risk/tests

No production change was made, so no tests were run.

If a future refactor touches this area, focus tests on:

- Failed warmup messages preserve the 180-character limit and punctuation behavior.
- `show_tool_calls=False` hides tool labels in warmup suffixes.
- A `ready` event clears the final warmup suffix by setting `needs_warmup_clear_edit` only when the last sent edit had warmup lines.
- Retried tool calls clear stale failed warmups for the same tool label without clearing unrelated failed workers.
