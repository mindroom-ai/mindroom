## Summary

Top duplication candidate: `FinalDeliveryOutcome.option_map` and `FinalDeliveryOutcome.options_list` repeat the copied interactive metadata accessors already present on `interactive._InteractiveResponse`.
The repeated outcome/event predicates are related terminal-delivery policy, but most call sites already consume the properties from `final_delivery.py` instead of duplicating the raw condition.

## Coverage TSV

```coverage-tsv
qualname	kind	lines	status	search_terms	candidates_checked
StreamTransportOutcome	class	lines 18-33	related-only	StreamTransportOutcome constructors terminal_status rendered_body visible_body_state last_physical_stream_event_id	src/mindroom/streaming.py:547, src/mindroom/streaming.py:559, src/mindroom/streaming.py:594, src/mindroom/streaming.py:632, src/mindroom/streaming.py:654, src/mindroom/streaming.py:674, src/mindroom/streaming.py:683, src/mindroom/response_terminal.py:46, src/mindroom/response_runner.py:2280
StreamTransportOutcome.has_any_physical_stream_event	method	lines 28-29	none-found	last_physical_stream_event_id is not None has_any_physical_stream_event	src/mindroom/final_delivery.py:29
StreamTransportOutcome.has_rendered_visible_body	method	lines 32-33	related-only	visible_body_state == "visible_body" has_rendered_visible_body _visible_stream_event_id	src/mindroom/delivery_gateway.py:354
FinalDeliveryOutcome	class	lines 37-84	related-only	FinalDeliveryOutcome constructors terminal_status event_id is_visible_response final_visible_body delivery_kind	src/mindroom/delivery_gateway.py:421, src/mindroom/delivery_gateway.py:782, src/mindroom/delivery_gateway.py:831, src/mindroom/delivery_gateway.py:859, src/mindroom/delivery_gateway.py:1195, src/mindroom/delivery_gateway.py:1279, src/mindroom/delivery_gateway.py:1477, src/mindroom/response_runner.py:1208, src/mindroom/response_runner.py:2298, src/mindroom/response_runner.py:2427
FinalDeliveryOutcome.__post_init__	method	lines 49-51	related-only	tuple(tool_trace or ()) extra_content dict(extra_content or {}) FinalDeliveryOutcome constructors	src/mindroom/delivery_gateway.py:426, src/mindroom/delivery_gateway.py:433, src/mindroom/delivery_gateway.py:673, src/mindroom/delivery_gateway.py:704, src/mindroom/delivery_gateway.py:788, src/mindroom/delivery_gateway.py:837
FinalDeliveryOutcome.final_visible_event_id	method	lines 54-55	none-found	final_visible_event_id event_id if is_visible_response response_event_id	src/mindroom/post_response_effects.py:200, src/mindroom/response_lifecycle.py:452, src/mindroom/response_lifecycle.py:508, src/mindroom/response_runner.py:828, src/mindroom/response_runner.py:2429
FinalDeliveryOutcome.mark_handled	method	lines 58-59	none-found	mark_handled event_id is not None is_visible_response not suppressed	src/mindroom/response_runner.py:828, src/mindroom/response_runner.py:1395, src/mindroom/response_runner.py:2444
FinalDeliveryOutcome.response_text	method	lines 62-63	none-found	final_visible_body or empty response_text property	src/mindroom/final_delivery.py:62
FinalDeliveryOutcome.option_map	method	lines 66-69	duplicate-found	dict(interactive_metadata.option_map) option_map property	src/mindroom/interactive.py:84
FinalDeliveryOutcome.options_list	method	lines 72-75	duplicate-found	tuple(dict(item) for item in options_list) options_as_list options_list property	src/mindroom/interactive.py:72, src/mindroom/interactive.py:91
FinalDeliveryOutcome.cancelled_for_empty_prompt	method	lines 78-84	none-found	cancelled_for_empty_prompt empty_prompt terminal_status cancelled event_id None	src/mindroom/response_runner.py:820
```

## Findings

1. `FinalDeliveryOutcome.option_map` duplicates copied interactive metadata projection from `interactive._InteractiveResponse.option_map`.

   `src/mindroom/final_delivery.py:66` returns `None` without metadata and otherwise returns `dict(self.interactive_metadata.option_map)`.
   `src/mindroom/interactive.py:84` has the same guard and copied dict behavior for `_InteractiveResponse`.
   The behavior is functionally the same: expose a mutable copy of an `InteractiveMetadata` mapping without leaking the stored dict.
   Difference to preserve: `FinalDeliveryOutcome` is a public terminal outcome type, while `_InteractiveResponse` is an internal parse result.

2. `FinalDeliveryOutcome.options_list` duplicates copied interactive metadata list/tuple projection.

   `src/mindroom/final_delivery.py:72` returns `None` without metadata and otherwise returns `tuple(dict(item) for item in self.interactive_metadata.options_list)`.
   `src/mindroom/interactive.py:72` exposes `InteractiveMetadata.options_as_list()` with the same per-item copied dicts, and `src/mindroom/interactive.py:91` wraps it for `_InteractiveResponse`.
   The behavior is nearly identical but the container type differs: `FinalDeliveryOutcome.options_list` returns a tuple, while `_InteractiveResponse.options_list` returns a list for Matrix reaction-button registration.

No other meaningful duplication was found in this primary file.
`StreamTransportOutcome` and `FinalDeliveryOutcome` are the canonical containers used by `streaming.py`, `delivery_gateway.py`, `response_runner.py`, `response_lifecycle.py`, and `post_response_effects.py`.
Repeated constructor calls in `delivery_gateway.py` are numerous, but they represent distinct terminal branches and preserve important differences in `terminal_status`, `is_visible_response`, `delivery_kind`, failure reason, placeholder cleanup, and visible body policy.

## Proposed Generalization

Move the copied metadata projections onto `InteractiveMetadata` itself:

- Add `InteractiveMetadata.option_map_copy() -> dict[str, str]`.
- Add `InteractiveMetadata.options_tuple() -> tuple[dict[str, str], ...]` or a more neutral copied sequence helper.
- Have `FinalDeliveryOutcome.option_map`, `FinalDeliveryOutcome.options_list`, and `_InteractiveResponse.option_map` delegate to those helpers.
- Keep `InteractiveMetadata.options_as_list()` if callers require a list for registration APIs.

This is a small helper-level refactor only.
No broad delivery-outcome abstraction is recommended for the repeated `FinalDeliveryOutcome(...)` construction sites because the branch-specific fields are part of the delivery policy.

## Risk/tests

Risk is low if the helper preserves copied dict semantics.
Tests should cover that mutating returned `option_map` or option entries does not mutate `InteractiveMetadata`, `FinalDeliveryOutcome`, or `_InteractiveResponse`.
Existing delivery tests should continue checking terminal outcomes through `final_visible_event_id`, `mark_handled`, and post-response interactive registration paths.
