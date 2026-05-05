## Summary

No meaningful duplication found.
`src/mindroom/approval_inbound.py` is a focused adapter for inbound Matrix approval controls, and the nearby approval modules it overlaps with already split responsibilities along clear boundaries.
The closest related behavior is approval-card parsing in `src/mindroom/approval_events.py` and action dispatch in `src/mindroom/tool_approval.py`, but those operate on different event types or lower-level runtime state.

## Coverage TSV

```coverage-tsv
qualname	kind	lines	status	search_terms	candidates_checked
ApprovalResponsePayload	class	lines 30-36	related-only	ApprovalResponsePayload MatrixApprovalAction ApprovalActionResult approval payload dataclass	src/mindroom/tool_approval.py:84; src/mindroom/approval_manager.py:198; src/mindroom/approval_events.py:15
parse_approval_response_event	function	lines 39-63	related-only	tool_approval_response approval_id denial_reason status reply_to_event_id approval card parsing	src/mindroom/approval_events.py:35; src/mindroom/approval_events.py:88; src/mindroom/matrix/event_info.py:75; src/mindroom/matrix/visible_body.py:41; tests/test_multi_agent_bot.py:3954
handle_tool_approval_action	async_function	lines 66-109	related-only	handle_matrix_approval_action MatrixApprovalAction approval notice authorized sender approval reaction custom response	src/mindroom/bot.py:1393; src/mindroom/bot.py:1410; src/mindroom/tool_approval.py:286; src/mindroom/approval_manager.py:408; src/mindroom/approval_manager.py:422; tests/test_tool_approval.py:828
maybe_handle_tool_approval_reply	async_function	lines 112-137	none-found	approval reply denial rich reply fallback active approval card EventInfo reply_to_event_id	src/mindroom/bot.py:1362; src/mindroom/matrix/visible_body.py:30; src/mindroom/tool_approval.py:280; src/mindroom/matrix/client_visible_messages.py:335
```

## Findings

No real duplication requiring refactor was found.

`parse_approval_response_event` is related to `PendingApproval.from_card_event` in `src/mindroom/approval_events.py:35`, but it parses the custom inbound `io.mindroom.tool_approval_response` control event rather than an outbound approval card.
Both normalize Matrix content into typed approval-shaped data, yet they intentionally validate different schemas and have different failure behavior.
The inbound parser tolerates malformed or incomplete custom client events by returning `None` fields, while approval-card parsing raises for missing required fields.

`handle_tool_approval_action` is related to `handle_matrix_approval_action` in `src/mindroom/tool_approval.py:286` and the approval-manager entry points at `src/mindroom/approval_manager.py:408` and `src/mindroom/approval_manager.py:422`.
This is not duplicate behavior because `approval_inbound.py` owns Matrix ingress authorization, construction of `MatrixApprovalAction`, and user-facing notice emission for resolution errors.
The lower-level modules own live waiter lookup, reason sanitization, and terminal approval state changes.

`maybe_handle_tool_approval_reply` is the only reply-to-approval shortcut found.
It combines reply relation extraction via `EventInfo.from_event`, active-card filtering through `is_process_active_approval_card`, Matrix rich-reply fallback stripping, and conversion of a text reply into a denial.
No other source module appears to implement the same reply-as-denial behavior.

## Proposed Generalization

No refactor recommended.
The module already acts as the narrow shared inbound adapter used by text replies, reactions, and custom Matrix approval response events.
Extracting the related pieces further would mostly move single-purpose glue around and risk blurring the boundary between Matrix ingress authorization and lower-level approval state resolution.

## Risk/Tests

If this area is changed later, preserve these behavior differences:

- Custom approval response events may resolve by `approval_id` without a card reply relation.
- Reply-to-approval text denies only when the replied-to card is active in this process.
- Unauthorized senders must be rejected before approval state is mutated.
- Resolution errors from truncated or otherwise invalid approval decisions should emit a notice through the orchestrator when possible.

Relevant existing tests include `tests/test_multi_agent_bot.py:3954`, `tests/test_multi_agent_bot.py:4035`, `tests/test_multi_agent_bot.py:4136`, `tests/test_multi_agent_bot.py:4432`, and `tests/test_tool_approval.py:828`.
