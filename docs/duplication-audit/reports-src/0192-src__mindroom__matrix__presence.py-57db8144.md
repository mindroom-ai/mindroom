# Duplication Audit: `src/mindroom/matrix/presence.py`

## Summary

Top duplication candidate: `build_agent_status_message` overlaps with `describe_agent` in `src/mindroom/agent_descriptions.py` for router/team/agent role and tool-summary rendering, but the output surfaces differ enough that this is related formatting rather than a clear extraction target.

No meaningful duplication found for Matrix presence API wrapping, user-online resolution, or streaming decision behavior.

## Coverage TSV

```coverage-tsv
qualname	kind	lines	status	search_terms	candidates_checked
set_presence_status	async_function	lines 18-47	none-found	set_presence PresenceSetResponse presence_status_set	set_presence_status call site src/mindroom/bot.py:903; rg set_presence( only src/mindroom/matrix/presence.py:31
build_agent_status_message	function	lines 50-96	related-only	get_entity_model_name get_agent_tools router role team agents Model tools available	src/mindroom/agent_descriptions.py:17; src/mindroom/agents.py:1081; src/mindroom/custom_tools/config_manager.py:353; src/mindroom/custom_tools/config_manager.py:491; src/mindroom/custom_tools/config_manager.py:658
is_user_online	async_function	lines 99-169	related-only	get_presence PresenceGetError room.users cached_user.presence online unavailable last_active_ago	src/mindroom/response_attempt.py:100; src/mindroom/matrix/client_delivery.py:131; src/mindroom/matrix/client_delivery.py:166; src/mindroom/authorization.py:237; rg get_presence only src/mindroom/matrix/presence.py:137
should_use_streaming	async_function	lines 172-215	none-found	should_use_streaming enable_streaming defaulting to streaming Streaming decision use_streaming	src/mindroom/response_runner.py:905; src/mindroom/response_runner.py:2143; src/mindroom/response_attempt.py:100
```

## Findings

### Related Formatting Only: Agent/Team Descriptions

`build_agent_status_message` in `src/mindroom/matrix/presence.py:50` and `describe_agent` in `src/mindroom/agent_descriptions.py:17` both branch on router, teams, and agents, then render role and tool/team details from config.

The duplicated behavior is limited to deciding which entity bucket an `agent_name` belongs to and collecting human-readable role/team/tool facts.
The differences are important: presence status includes model provider/id, emoji-prefixed compact parts, tool count, and only the first five team members, while `describe_agent` emits multiline delegation/orchestration descriptions, full team membership, collaboration mode, delegate targets, and the first short instruction.

This is not a strong extraction candidate unless more surfaces start needing the same compact status summary.

### Related Presence Consumption Only: Stop Button Visibility

`is_user_online` in `src/mindroom/matrix/presence.py:99` centralizes cache-first Matrix presence resolution and is reused by `ResponseAttemptRunner._should_show_stop_button` in `src/mindroom/response_attempt.py:100`.

That call site repeats the higher-level policy pattern of "feature flag/user exists, then presence decides the UI behavior", similar to `should_use_streaming`, but it does not duplicate the presence lookup implementation.
The behavior differs because stop-button visibility returns the configured value when no user is present, while streaming defaults to enabled when no requester is available.

No duplicated `client.get_presence` or `PresenceGetError` handling exists elsewhere under `src/mindroom`.

## Proposed Generalization

No refactor recommended.

The only overlap is related formatting/policy shape, not repeated active implementation.
Extracting a shared helper now would either erase meaningful output differences or create a parameterized formatter with little immediate payoff.

## Risk/Tests

If refactoring is attempted later, tests should pin:

- Presence set success and failure logging around `nio.PresenceSetResponse`.
- Status message output for router, team, configured agent with tools, configured model, and unknown model.
- Cache-first `is_user_online` behavior for `online`, `unavailable`, `offline`, missing room/user, `PresenceGetError`, and client exceptions.
- Divergent default behavior between streaming decisions with no requester and stop-button decisions with no user.
