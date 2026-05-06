Summary: No meaningful duplicate implementation of the full edited-message regeneration workflow was found.
The closest active duplication is a small coalesced-turn prompt-map update pattern shared with normal dispatch prompt refresh, plus several intentionally shared lifecycle surfaces around response generation, turn persistence, and visible edit extraction.

```coverage-tsv
qualname	kind	lines	status	search_terms	candidates_checked
_GenerateResponse	class	lines 32-52	related-only	generate_response protocol callable existing_event_id matrix_run_metadata on_lifecycle_lock_acquired	src/mindroom/bot.py:1570; src/mindroom/bot.py:1819; src/mindroom/response_runner.py:2081
_GenerateResponse.__call__	async_method	lines 35-52	related-only	generate_response ResponseRequest existing_event_id target matrix_run_metadata	src/mindroom/bot.py:1619; src/mindroom/bot.py:1841; src/mindroom/bot.py:1938; src/mindroom/response_runner.py:2081
EditRegeneratorDeps	class	lines 56-66	related-only	Deps dataclass runtime resolver turn_store ingress_hook_runner generate_response	src/mindroom/bot.py:455; src/mindroom/turn_store.py:76; src/mindroom/conversation_resolver.py:105
EditRegenerator	class	lines 70-322	related-only	message edit regeneration handle_message_edit original_event_id load_turn extract_visible_edit_body	src/mindroom/turn_controller.py:1539; src/mindroom/turn_store.py:200; src/mindroom/matrix/conversation_cache.py:228
EditRegenerator._logger	method	lines 75-76	not-a-behavior-symbol	get_logger logger property deps logger	none
EditRegenerator._client	method	lines 78-83	related-only	client none Matrix client is not ready runtime client	src/mindroom/conversation_resolver.py:123; src/mindroom/bot.py:497
EditRegenerator._record_turn_record	method	lines 85-87	related-only	record_turn_record record_handled_turn_record exact handled-turn record	src/mindroom/turn_store.py:109; src/mindroom/handled_turns.py:296
EditRegenerator.edit_regeneration_context	async_method	lines 89-116	related-only	fetch_thread_history MessageContext replay_guard_history resolved_thread_id	src/mindroom/conversation_resolver.py:573; src/mindroom/conversation_resolver.py:592
EditRegenerator.handle_message_edit	async_method	lines 118-322	duplicate-found	handle_message_edit coalesced source_event_prompts edited_content coalesced_prompt build_run_metadata ingress hooks	src/mindroom/turn_controller.py:1539; src/mindroom/turn_controller.py:1647; src/mindroom/coalescing_batch.py:166; src/mindroom/coalescing_batch.py:183; src/mindroom/turn_controller.py:1779; src/mindroom/matrix/conversation_cache.py:228; src/mindroom/matrix/client_visible_messages.py:174
```

Findings:

1. Coalesced prompt-map update is duplicated in the normal dispatch and edit-regeneration paths.

- `src/mindroom/edit_regenerator.py:224` copies persisted `source_event_prompts`, overwrites the edited source event with the newly visible body, rebuilds prompt parts in `source_event_ids` order, and calls `coalesced_prompt()`.
- `src/mindroom/turn_controller.py:1647` copies `handled_turn.source_event_prompts`, overwrites the normalized event body for the same source event, and writes it back with `with_source_event_prompts()`.
- `src/mindroom/coalescing_batch.py:166` and `src/mindroom/coalescing_batch.py:183` are the original source of the prompt map and combined prompt construction.
- The behavior is functionally the same at the prompt-map mutation layer: preserve all known source prompts, update one source event body, and keep the coalesced turn's prompt metadata consistent.
- Differences to preserve: edit regeneration must fail closed when persisted prompts are missing or incomplete, and it must rebuild the combined prompt in recorded source-event order; normal dispatch only refreshes metadata when normalization changes a live event already present in the handled turn.

2. Edit visible-body resolution is centralized already, but regeneration duplicates the high-level "resolve edit then project visible content" decision shape used by cached conversation reads.

- `src/mindroom/edit_regenerator.py:193` calls `extract_visible_edit_body()` and returns early if no visible edited body can be resolved.
- `src/mindroom/matrix/conversation_cache.py:245` retrieves the latest edit, calls `extract_edit_body()`, returns early when body/content are unavailable, then projects the resolved content onto the original event.
- `src/mindroom/matrix/client_visible_messages.py:174` is already the runtime-config-aware wrapper around `extract_edit_body()`.
- This is related-only rather than a refactor target because regeneration only needs the edited prompt body for a single incoming edit event, while the cache path merges latest edit content into an original event source.

3. Turn context and run metadata reconstruction are related lifecycle plumbing rather than duplicated behavior.

- `src/mindroom/edit_regenerator.py:165` backfills `conversation_target`, `history_scope`, and `response_owner` for an old handled-turn record before regeneration.
- `src/mindroom/turn_controller.py:1779` attaches the same response context during normal dispatch before building run metadata at `src/mindroom/turn_controller.py:1784`.
- `src/mindroom/turn_store.py:145` and `src/mindroom/turn_store.py:159` already centralize this behavior for normal response paths.
- This is not a duplicate implementation because edit regeneration starts from a `HandledTurnRecord` and may need to preserve an explicit anchor event; normal dispatch starts from `HandledTurnState`.

Proposed generalization:

Introduce one focused helper only if this area is edited again: a pure function in `src/mindroom/coalescing_batch.py`, for example `update_coalesced_prompt_map(source_event_ids, source_event_prompts, source_event_id, prompt) -> tuple[dict[str, str], str] | None`.
It should return `None` for missing or incomplete maps so edit regeneration can keep its current fail-closed logging, and normal dispatch can continue using the map-update half without changing behavior.
No immediate refactor is recommended from this audit alone because the duplicate is small and the edit path has correctness-sensitive early exits.

Risk/tests:

- Any helper around coalesced prompt updates must preserve source-event ordering; otherwise regenerated prompts for multi-message turns can change meaning.
- Tests should cover editing the first, middle, and anchor event of a coalesced turn, missing persisted prompt maps, incomplete maps, and non-coalesced edits.
- Existing edit regeneration tests should also assert that stale runs are removed only after the lifecycle lock is acquired and that suppressed regeneration still backfills old turn records when required.
