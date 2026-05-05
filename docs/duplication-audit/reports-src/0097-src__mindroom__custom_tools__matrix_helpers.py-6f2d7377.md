# Summary

Top duplication candidate: `MatrixApiTools._check_rate_limit` in `src/mindroom/custom_tools/matrix_api.py` duplicates the sliding-window rate limiter implemented by `check_rate_limit`.
`MatrixMessageTools._check_rate_limit` and `MatrixRoomTools._check_rate_limit` are wrapper reuse sites, not duplicate implementations.
`src/mindroom/matrix/large_messages.py` has related rate-throttle logic, but it gates oversized streaming edits by last-send timestamp rather than enforcing a weighted per-agent/requester/room action budget.

# Coverage TSV

```coverage-tsv
qualname	kind	lines	status	search_terms	candidates_checked
check_rate_limit	function	lines 15-54	duplicate-found	rate_limit, _check_rate_limit, recent_actions, recent_write_units, sliding window, monotonic, stale_keys	src/mindroom/custom_tools/matrix_api.py:514, src/mindroom/custom_tools/matrix_message.py:113, src/mindroom/custom_tools/matrix_room.py:94, src/mindroom/matrix/large_messages.py:155
```

# Findings

## Duplicate Sliding-Window Matrix Tool Rate Limiter

- Primary behavior: `src/mindroom/custom_tools/matrix_helpers.py:15` builds a `(agent_name, requester_id, room_id)` key, uses `time.monotonic()`, prunes expired deque entries under a lock, rejects when `len(history) + weight > max_actions`, appends one timestamp per weight unit, and deletes stale keys from the shared history map.
- Duplicate implementation: `src/mindroom/custom_tools/matrix_api.py:514` performs the same keyed sliding-window algorithm against `_recent_write_units`, including the same monotonic cutoff, lock-protected deque pruning, weighted append, stale-key cleanup, and string error return.
- Differences to preserve: `matrix_api` maps an action to `_WRITE_ACTION_WEIGHTS[action]`, uses a 60 second / 8 unit window, stores history in `_recent_write_units`, and returns the message `Rate limit exceeded for matrix_api writes (8 units per 60s).`
- Existing reuse: `src/mindroom/custom_tools/matrix_message.py:113` and `src/mindroom/custom_tools/matrix_room.py:94` already delegate to `check_rate_limit` with tool-specific locks, history maps, windows, max actions, and optional weight.
- Related only: `src/mindroom/matrix/large_messages.py:155` prunes a timestamp map and suppresses repeated oversized non-terminal streaming edits by `(room_id, original_event_id)`.
  It is a minimum-interval throttle with content-size checks, not the same per-agent/requester/room action-window limiter.

# Proposed Generalization

Replace the body of `MatrixApiTools._check_rate_limit` with a call to `mindroom.custom_tools.matrix_helpers.check_rate_limit`, passing:

- `lock=cls._rate_limit_lock`
- `recent_actions=cls._recent_write_units`
- `window_seconds=cls._RATE_LIMIT_WINDOW_SECONDS`
- `max_actions=cls._RATE_LIMIT_MAX_UNITS`
- `tool_name="matrix_api writes"`
- `context=context`
- `room_id=room_id`
- `weight=cls._WRITE_ACTION_WEIGHTS[action]`

This preserves the current rejection text shape because `tool_name="matrix_api writes"` yields `Rate limit exceeded for matrix_api writes actions (...)`, which is close but not identical.
If exact text compatibility matters, the helper would need either a message label parameter or `matrix_api` would keep its local message formatting.
Given the repository has no end-users yet, the simplest production refactor would accept the minor wording change.

# Risk/tests

Risk is low because the duplicated algorithm is already centralized and used by two sibling Matrix tool modules.
The main behavior to test is that `MatrixApiTools._check_rate_limit` still enforces action weights, window expiration, per-agent/requester/room separation, and stale-key cleanup.
Focused tests should cover `send_event` weight 1, `put_state`/`redact` weight 2, boundary behavior at the 8-unit limit, and recovery after the 60-second window.
