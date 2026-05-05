## Summary

No meaningful duplication found.
`src/mindroom/matrix/typing.py` is the only source module that calls `nio.AsyncClient.room_typing`, handles `nio.RoomTypingError`, or implements a Matrix typing-indicator context manager.
There is related task lifecycle code elsewhere, but it is generic background-task cancellation rather than duplicated typing behavior.

## Coverage TSV

```coverage-tsv
qualname	kind	lines	status	search_terms	candidates_checked
_set_typing	async_function	lines 19-44	none-found	room_typing, RoomTypingError, set typing status, Failed to set typing	src/mindroom/matrix/typing.py:35; src/mindroom/response_runner.py:33; none outside primary file
typing_indicator	async_function	lines 48-91	none-found	typing_indicator, typing indicator, asynccontextmanager, room_typing, refresh typing	src/mindroom/response_runner.py:1034; src/mindroom/response_runner.py:1133; src/mindroom/response_runner.py:1600; src/mindroom/response_runner.py:1692; none outside primary file
typing_indicator.<locals>.refresh_typing	nested_async_function	lines 74-78	related-only	refresh_typing, create_task sleep cancel suppress CancelledError, periodic refresh loop	src/mindroom/api/main.py:377; src/mindroom/api/main.py:383; src/mindroom/orchestration/runtime.py:123; src/mindroom/knowledge/watch.py:149; src/mindroom/knowledge/watch.py:171
```

## Findings

No real duplication found.

`_set_typing` converts seconds to milliseconds, calls `client.room_typing`, and logs `nio.RoomTypingError` failures.
Searches for `room_typing`, `RoomTypingError`, and typing-status log text found no other source implementation outside `src/mindroom/matrix/typing.py`.

`typing_indicator` is reused by `src/mindroom/response_runner.py` at lines 1034, 1133, 1600, and 1692 instead of being reimplemented there.
Those call sites are consumers of the helper, not duplicates.

`refresh_typing` has generic similarities to other background loops that create a task, later cancel it, and suppress `asyncio.CancelledError`.
Examples include `src/mindroom/api/main.py:377-388`, `src/mindroom/orchestration/runtime.py:123-134`, and `src/mindroom/knowledge/watch.py:149-182`.
Those flows manage unrelated API, sync, and knowledge watcher tasks and do not duplicate Matrix typing refresh semantics.

## Proposed Generalization

No refactor recommended.
The Matrix typing behavior is already centralized in `src/mindroom/matrix/typing.py`.
Extracting the generic task-cancel pattern from this module would not reduce duplicated Matrix behavior and would couple a tiny context manager to broader lifecycle utilities without clear payoff.

## Risk/Tests

No production changes were made.
If this module were changed in the future, focused tests should cover timeout conversion to milliseconds, `RoomTypingError` logging without raising, refresh interval scheduling, refresh-task cancellation, and final typing stop on normal exit and cancellation.
