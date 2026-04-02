# Thread Dispatch Single Source Of Truth Notes

Last updated: 2026-04-02
Owner: MindRoom backend
Scope: Maintenance guidance for dispatch targeting, thread history hydration, and response delivery semantics.

## Objective

Keep one source of truth per concern.
Do not let target derivation, thread history hydration, and response outcome semantics drift apart again.

## Current Design

`derive_conversation_target()` resolves canonical conversation identity without reconstructing thread bodies.
`_extract_dispatch_context()` may return `thread_history=[]` with `requires_full_thread_history=True` when dispatch still needs canonical history.
`_hydrate_dispatch_context()` is the only production step that loads canonical thread history for router, team selection, and `should_agent_respond()`.
`fetch_thread_history()` is the production thread-history API.
`_latest_thread_event_id()` is a thin MSC3440 helper and must follow the same visible-state policy as `fetch_thread_history()`.
`_ResponseDispatchResult` is the single source of truth for final delivery outcome.
`_ResponseTarget` is the single source of truth for resolved thread identity, delivery thread identity, and session scope.

## Invariants

1. Target derivation must not download sidecars or prefetch thread bodies.
2. Router, team selection, and `should_agent_respond()` may only consume hydrated canonical thread history.
3. If one inbound event needs a visible thread reconstruction, later steps must reuse that work instead of rebuilding it independently.
4. Suppressed responses must not preserve a final response event ID.
5. A source event is marked responded only when a real terminal event exists.
6. Placeholder cleanup and post-placeholder failure handling must use the same `delivery_thread_id` as the original placeholder send.
7. Threaded edits without a genuine reply target must use the latest visible thread event for MSC3440 fallback.
8. Reply-chain target derivation and reply-chain history hydration are different phases and must stay separate.

## Smells To Watch For

Duplicated visible-thread reconstruction logic is an architectural smell.
Recomputing response target identity in multiple delivery paths is an architectural smell.
Keeping an unused snapshot wrapper or metadata type is not architecture.
That is just stale code and should be removed.

## Maintenance Checklist

When changing reply-chain targeting, inspect `derive_conversation_target()`, `derive_conversation_context()`, and `_latest_thread_event_id()`.
When changing response delivery, inspect `_prepare_response_target()`, `_deliver_generated_response()`, `_resolve_response_event_id()`, `_execute_dispatch_action()`, and `_finalize_dispatch_failure()`.
When changing threaded edits, inspect `build_threaded_edit_content()` and confirm MSC3440 fallback still points at the latest visible thread event.
When changing placeholder handling, confirm suppression, failed edits, and post-placeholder failures all leave retry semantics correct.

## Suggested Validation

Run `uv run pytest -x -n 0 --no-cov -q`.
Run `uv run pre-commit run --all-files`.
For targeted thread-lifecycle work, at minimum run `tests/test_thread_history.py`, `tests/test_multi_agent_bot.py`, `tests/test_streaming_behavior.py`, `tests/test_edit_response_regeneration.py`, and `tests/test_threading_error.py`.
