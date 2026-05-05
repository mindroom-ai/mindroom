## Summary

Top duplication candidates for `src/mindroom/hooks/ingress.py` are hook-source tag serialization/parsing and repeated automation-source gating.
`hook_ingress_policy` itself is the centralized source for synthetic hook ingress behavior, and I found related call sites rather than duplicated implementations of its depth policy.

## Coverage TSV

```coverage-tsv
qualname	kind	lines	status	search_terms	candidates_checked
HookIngressPolicy	class	lines 17-23	related-only	HookIngressPolicy policy rerun_message_received allow_full_dispatch bypass_unmentioned_agent_gate skip_message_received_plugin_names	src/mindroom/turn_policy.py:117; src/mindroom/turn_controller.py:396; tests/test_hook_ingress.py:43
split_hook_source	function	lines 26-33	duplicate-found	split_hook_source hook_source source_hook partition colon plugin_name event_name	src/mindroom/hooks/context.py:94; src/mindroom/hooks/types.py:184; src/mindroom/hooks/sender.py:74; tests/test_hook_ingress.py:33
hook_ingress_policy	function	lines 36-59	related-only	hook_ingress_policy message_received_depth hook hook_dispatch EVENT_MESSAGE_RECEIVED allow_full_dispatch	src/mindroom/turn_controller.py:389; src/mindroom/turn_policy.py:112; src/mindroom/hooks/context.py:781; tests/test_hook_ingress.py:43
should_handle_interactive_text_response	function	lines 62-64	duplicate-found	should_handle_interactive_text_response is_automation_source_kind source_kind human follow-up interactive	src/mindroom/response_lifecycle.py:157; src/mindroom/bot.py:1506; src/mindroom/turn_policy.py:589; src/mindroom/turn_controller.py:418; tests/test_hook_ingress.py:75
```

## Findings

### 1. Hook-source tag serialization and parsing are split across modules

`split_hook_source` parses a serialized hook source as `<plugin_name>:<event_name>` using `partition(":")`, preserving additional colons in the event name at `src/mindroom/hooks/ingress.py:26`.
The matching serialization happens inline with `f"{plugin_name}:{event_name}"` in `src/mindroom/hooks/context.py:94`, and the serialized value is stored as Matrix metadata in `src/mindroom/hooks/sender.py:74`.
`validate_event_name` separately reasons about colon-delimited hook event namespaces at `src/mindroom/hooks/types.py:184`.

This is duplicated behavior because the serialized hook-source shape is effectively a small wire format, but construction and parsing are independent.
The current split parser intentionally partitions only once so event names like `message:received` remain intact, as tested in `tests/test_hook_ingress.py:33`.

Differences to preserve:
- `split_hook_source(None)` and malformed values return `(None, None)`.
- Event names may contain colons.
- Plugin names must be non-empty for a valid parsed source.

### 2. Automation-source gating is repeated around interactive and human-follow-up behavior

`should_handle_interactive_text_response` is a thin wrapper around `not is_automation_source_kind(envelope.source_kind)` at `src/mindroom/hooks/ingress.py:62`.
The same source-kind predicate gates queued notices and follow-up dispatch in `src/mindroom/response_lifecycle.py:157`, `src/mindroom/bot.py:1506`, `src/mindroom/turn_policy.py:589`, and `src/mindroom/turn_controller.py:418`.

This is duplicated behavior at the predicate level: these call sites all distinguish synthetic automation from human-originated ingress before allowing human-response paths.
It is not a one-to-one duplicate of interactive handling because some call sites also check thread state, mentions, active responses, or agent senders.

Differences to preserve:
- Interactive text responses only need the automation-source check.
- Follow-up queues and coalescing bypasses also preserve target, thread, mention, active-response, and agent-sender checks.
- `dispatch_policy_source_kind` has additional semantics in `src/mindroom/turn_policy.py:595`.

## Proposed Generalization

For hook-source tags, add a tiny helper near hook types, for example `build_hook_source(plugin_name: str, event_name: str) -> str` in `src/mindroom/hooks/types.py` or `src/mindroom/hooks/ingress.py`, and use it in `src/mindroom/hooks/context.py`.
That would pair construction with `split_hook_source` and make the `<plugin>:<event>` wire format explicit.

For automation-source gating, no immediate refactor is recommended from this file alone.
The repeated `is_automation_source_kind` calls are readable, already centralized through `src/mindroom/dispatch_source.py:51`, and each caller layers different local behavior on top.
If this grows, a later helper should name the higher-level concept, such as `is_human_ingress_source_kind`, rather than duplicating interactive-specific naming.

## Risk/tests

Hook-source helper extraction risk is low but should be covered by `tests/test_hook_ingress.py::test_split_hook_source_parses_serialized_tag` plus hook sender/context tests that assert `com.mindroom.hook_source`, especially `tests/test_hook_sender.py` cases with event names containing colons.

Automation-source gating should be left unchanged unless a broader human-ingress helper is introduced.
Any such change would need tests around interactive prompts, queued human notices, active-thread follow-up coalescing, and hook/scheduled source suppression.

No production code was edited, and tests were not run for this report-only audit.
