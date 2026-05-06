Summary: cancellation provenance handling is the only meaningful duplication candidate for `src/mindroom/response_attempt.py`.
`ResponseAttemptRunner.run` and the non-streaming/streaming paths in `src/mindroom/response_runner.py` all classify `CancelledError`, map it to a failure reason, and coordinate terminal cancellation behavior.
The placeholder-send and stop-button behavior is related to other modules, but it is not duplicated in another active implementation.

```coverage-tsv
qualname	kind	lines	status	search_terms	candidates_checked
ResponseAttemptDeps	class	lines 31-41	not-a-behavior-symbol	ResponseAttemptDeps constructor and fields; dependency bundle usage	src/mindroom/response_runner.py:1420
ResponseAttemptRequest	class	lines 45-55	not-a-behavior-symbol	ResponseAttemptRequest fields thinking_message existing_event_id on_cancelled; request object usage	src/mindroom/response_runner.py:1434
log_cancelled_response	function	lines 58-78	related-only	log_cancelled_response classify_cancel_source CancelledError provenance restart user_stop interrupted	src/mindroom/response_runner.py:1185; src/mindroom/response_runner.py:1809; src/mindroom/response_runner.py:2012; src/mindroom/orchestration/runtime.py:63; src/mindroom/streaming.py:1122
ResponseAttemptRunner	class	lines 82-179	duplicate-found	ResponseAttemptRunner visible response attempt cancellation tracking stop button cleanup placeholder delivery	src/mindroom/response_runner.py:1184; src/mindroom/response_runner.py:1807; src/mindroom/response_runner.py:2011; src/mindroom/stop.py:236
ResponseAttemptRunner._send_thinking_message	async_method	lines 87-98	related-only	SendTextRequest STREAM_STATUS_PENDING placeholder_sent mark_first_visible_reply send_text	src/mindroom/delivery_gateway.py:814; src/mindroom/turn_controller.py:1112; src/mindroom/turn_controller.py:1164; src/mindroom/response_runner.py:1245
ResponseAttemptRunner._should_show_stop_button	async_method	lines 100-115	related-only	is_user_online show_stop_button Stop button decision streaming decision presence check	src/mindroom/matrix/presence.py:172; src/mindroom/stop.py:350
ResponseAttemptRunner.run	async_method	lines 117-179	duplicate-found	asyncio.create_task stop_manager set_current clear_message CancelledError failure_reason visible response cleanup	src/mindroom/response_runner.py:1184; src/mindroom/response_runner.py:1807; src/mindroom/response_runner.py:2011; src/mindroom/stop.py:236
```

Findings:

1. Cancellation provenance and terminal handling are still repeated around the response-attempt abstraction.
`ResponseAttemptRunner.run` classifies `CancelledError`, derives `cancel_failure_reason`, invokes an optional cancellation callback, logs via `log_cancelled_response`, and then clears stop tracking in `src/mindroom/response_attempt.py:152`.
The team non-streaming path in `src/mindroom/response_runner.py:1184` repeats classification and logging, then either edits the visible response into a cancelled note or builds a cancelled `FinalDeliveryOutcome`.
The bot non-streaming path in `src/mindroom/response_runner.py:1807` repeats classification and logging before either delivering a cancelled visible note or returning a cancelled outcome.
The streaming path in `src/mindroom/response_runner.py:2011` repeats the same cancellation logging entry point before re-raising for stream cleanup.
Differences to preserve: team and bot non-streaming paths own delivery finalization, streaming must re-raise for stream transport cleanup, and `ResponseAttemptRunner.run` owns stop-manager cleanup and the `on_cancelled` callback.

2. Placeholder message sending is related but not a refactor target.
`ResponseAttemptRunner._send_thinking_message` sends a pending placeholder with `{STREAM_STATUS_KEY: STREAM_STATUS_PENDING}` and marks `placeholder_sent` / first-visible timing in `src/mindroom/response_attempt.py:87`.
Other `send_text` call sites in `src/mindroom/delivery_gateway.py:814`, `src/mindroom/turn_controller.py:1112`, and `src/mindroom/turn_controller.py:1164` send visible messages with extra content, but they are final delivery, routed echoes, or terminal failure notices rather than pending placeholders.
The comparable timing marks in `src/mindroom/response_runner.py:1245` mark final visible replies, not placeholders.

3. Stop-button decision logic is related to presence and stop tracking, but not duplicated.
`ResponseAttemptRunner._should_show_stop_button` combines the configured stop-button flag with `is_user_online` and logs the decision in `src/mindroom/response_attempt.py:100`.
`src/mindroom/matrix/presence.py:172` has a similar presence-gated decision for streaming, but the behavior controls a different feature and has different defaults when no requester is available.
`src/mindroom/stop.py:350` owns the Matrix reaction write, so it is a collaborator rather than a duplicate implementation.

Proposed generalization:

A small helper could live next to `log_cancelled_response` in `src/mindroom/response_attempt.py` or in a focused cancellation-response module, returning both `cancel_source` and `failure_reason` after logging.
That would remove the repeated `classify_cancel_source` / `cancel_failure_reason` pairing across `ResponseAttemptRunner.run` and `response_runner.py` without absorbing delivery-specific behavior.
No broader refactor is recommended from this file alone.

Risk/tests:

The main risk is changing cancellation semantics for user stop versus sync restart.
Tests should cover `ResponseAttemptRunner.run` cancellation callbacks and stop cleanup in `tests/test_response_attempt.py`, plus the team, bot non-streaming, and streaming cancellation paths that currently call `log_cancelled_response`.
Any helper should preserve the existing log messages because they distinguish sync restart, user stop, and unexpected interruption.
