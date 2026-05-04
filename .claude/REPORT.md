# Matrix Delivery Device Trust Opt-In

## Summary of Changes

Added `matrix_delivery.ignore_unverified_devices`, defaulting to `false`, as an explicit Matrix delivery policy option.
Wired the option through outgoing Matrix delivery paths that call `room_send`, including normal messages, edits, file messages, hooks, scheduled sends, interactive controls, approval cards, thread summaries, streaming updates, and related tool flows.
Updated generated config templates and deployment-neutral documentation to describe the option and its security tradeoff.
Added regression tests proving the default remains `false` and that an opt-in value reaches the Matrix client send call.
Updated affected tests and generated docs skill references for the new delivery keyword.

## Tests Run and Results

`uv run pytest tests/test_matrix_delivery.py -q -n 0 --no-cov` passed.
`uv run pytest tests/test_send_file_message.py tests/test_large_messages_integration.py -q -n 0 --no-cov` passed.
`uv run pytest tests/test_matrix_delivery.py tests/test_thread_summary.py tests/test_interactive.py tests/test_stop_emoji_reuse.py tests/test_response_attempt.py tests/test_cli_config.py tests/test_tool_approval.py tests/test_matrix_api_tool.py tests/test_matrix_message_tool.py tests/test_tool_hooks.py tests/test_stale_stream_cleanup.py -q -n 0 --no-cov` passed.
`uv run pytest tests/test_streaming_behavior.py tests/test_streaming_finalize.py tests/test_streaming_e2e.py tests/test_unknown_command_response.py tests/test_multi_agent_bot.py::TestMultiAgentOrchestrator::test_shutdown_expires_in_flight_approval_send_after_event_id_arrives tests/test_multi_agent_bot.py::TestMultiAgentOrchestrator::test_update_config_keeps_router_owned_approvals_pending_when_requesting_bot_is_removed tests/test_multi_agent_bot.py::TestMultiAgentOrchestrator::test_requesting_bot_room_reconcile_keeps_router_owned_approval_pending tests/test_skip_mentions.py::test_delivery_gateway_edit_text_preserves_plain_reply_relation_in_room_mode -q -n 0 --no-cov` passed.
`uv run pytest --lf -q -n 0 --no-cov` passed after updating affected test doubles.
`uv run ruff format src/mindroom tests docs && uv run ruff check src/mindroom tests docs` passed.
`uv run pytest --no-cov` passed with 6250 passed and 56 skipped.
`uv run pre-commit run --all-files` passed.

## Remaining Risks or Questions

No known remaining implementation risks.
The option intentionally weakens Matrix E2EE device verification behavior only when operators opt in, so documentation calls out the tradeoff and the default remains the current safe behavior.

## Suggested PR Title

Add Matrix delivery opt-in for ignoring unverified devices

## Suggested PR Body

## Summary

- Add `matrix_delivery.ignore_unverified_devices`, defaulting to `false`.
- Pass the setting to outgoing Matrix `room_send` calls so operators can explicitly opt in to delivery that ignores unverified devices.
- Document the security tradeoff and update generated config templates.

## Tests

- `uv run pytest --no-cov`
- `uv run pre-commit run --all-files`
