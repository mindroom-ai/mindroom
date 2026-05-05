## Summary

Top duplication candidates:

- `generate_room_topic_ai` repeats the local "one-off structured Agno generation" flow also used by thread summaries and routing: define a tiny Pydantic response model, build an `Agent` with `output_schema`, run it, type-check `response.content`, and return a scalar field.
- `ensure_room_has_topic` repeats the Matrix "ensure state event" pattern used by room name, space child, join rules, and thread-tag power-level reconciliation, but the topic path has a generated desired value and different failure logging.

## Coverage TSV

```coverage-tsv
qualname	kind	lines	status	search_terms	candidates_checked
_RoomTopic	class	lines 22-25	related-only	output_schema BaseModel Field topic summary decision	src/mindroom/thread_summary.py:65; src/mindroom/routing.py:25; src/mindroom/teams.py:198; src/mindroom/scheduling.py:690
generate_room_topic_ai	async_function	lines 28-117	duplicate-found	Agent output_schema cached_agent_run model_loading get_model_instance structured topic summary	src/mindroom/thread_summary.py:322; src/mindroom/routing.py:32; src/mindroom/scheduling.py:688; src/mindroom/memory/auto_flush.py:532
ensure_room_has_topic	async_function	lines 120-171	duplicate-found	room_get_state_event room_put_state m.room.topic ensure_room_name state event reconcile	src/mindroom/matrix/client_room_admin.py:90; src/mindroom/matrix/client_room_admin.py:183; src/mindroom/matrix/client_room_admin.py:205; src/mindroom/matrix/client_room_admin.py:322; src/mindroom/matrix/client_room_admin.py:351; src/mindroom/matrix/avatar.py:180; src/mindroom/thread_tags.py:685
```

## Findings

### Structured one-field AI generation is repeated

`src/mindroom/topic_generator.py:22` defines `_RoomTopic`, a private Pydantic schema with one generated text field.
`src/mindroom/topic_generator.py:89` loads a model, constructs an `Agent` with `output_schema=_RoomTopic`, runs `cached_agent_run`, checks the content type, and returns `content.topic`.

The same behavior shape appears in `src/mindroom/thread_summary.py:65` and `src/mindroom/thread_summary.py:322`.
That path defines `_ThreadSummary`, constructs an `Agent` with `output_schema=_ThreadSummary`, runs `cached_agent_run`, checks the content type, and returns `content.summary`.

Related structured AI flows also exist in `src/mindroom/routing.py:25`, `src/mindroom/teams.py:198`, and `src/mindroom/scheduling.py:690`.
Those are less direct duplicates because they validate additional fields, use different execution APIs in some cases, and apply domain-specific post-validation.

Differences to preserve:

- Topic generation logs and returns `None` on `cached_agent_run` exceptions.
- Thread summary generation configures model temperature and normalizes length elsewhere in the call chain.
- Routing validates that the selected agent is in the available set.
- Scheduling mutates defaults for missing one-time or cron schedule fields and validates conditional workflows.

### Matrix state-event ensure/update flow is repeated

`src/mindroom/topic_generator.py:142` reads `m.room.topic`, treats a truthy `content["topic"]` as already configured, and otherwise writes `{"topic": topic}` through `room_put_state`.
This is functionally the same read-current-state, compare desired content, write-state, response-type-check, log, and return-bool flow used in `src/mindroom/matrix/client_room_admin.py:322` for `m.room.name`.

The same state reconciliation pattern also appears in `src/mindroom/matrix/client_room_admin.py:90` for thread-tag power levels, `src/mindroom/matrix/client_room_admin.py:183` and `src/mindroom/matrix/client_room_admin.py:205` for join rules, and `src/mindroom/matrix/client_room_admin.py:351` for `m.space.child`.
Those call sites differ in how they compute or compare desired content, but the Matrix response handling is repeated.

Differences to preserve:

- `ensure_room_has_topic` only writes after successful AI topic generation.
- `ensure_room_has_topic` treats any existing non-empty topic as acceptable, not as drift to overwrite.
- `ensure_room_name` enforces a specific desired value and updates drift.
- `ensure_thread_tags_power_level` validates that the current state content is a dict before deriving desired content.
- `add_room_to_space` uses a non-empty state key.

## Proposed Generalization

A minimal refactor could add a small Matrix state helper in `src/mindroom/matrix/client_room_admin.py`, for example `put_room_state_bool(client, room_id, event_type, content, *, state_key=None, success_log, failure_log)`.
This would centralize `room_put_state`, `RoomPutStateResponse` checking, response error formatting, and boolean return handling.
`ensure_room_has_topic`, `ensure_room_name`, `_set_room_join_rule`, and `add_room_to_space` could call it while preserving their own read/compare/generation logic.

A second, lower-priority helper could support scalar structured generation, for example a private utility near AI runtime that accepts an `Agent`, prompt, session ID, expected schema type, field extractor, and log labels.
Because only topic generation and thread summaries are close direct matches, this should remain very small if introduced at all.
Routing, team mode selection, and scheduling should not be forced into it because their validation and fallback behavior is more domain-specific.

## Risk/tests

No production code was changed.

If the Matrix state helper is later implemented, tests should cover existing-topic no-op behavior, generated-topic failure, successful topic write, room-name drift update, join-rule write failure, and state-key handling for `m.space.child`.
If the structured generation helper is later implemented, tests should cover schema content, unexpected content fallback, and exception-to-`None` behavior for topic generation.
