# ISSUE-247 final implementation plan

## Outcome

This plan is the minimum self-contained change that satisfies all three fixed requirements.

1. `[R1]` Every real Matrix message placed in current or replayed conversation context carries its event ID on `<msg>`, including newly persisted normal and interrupted assistant replies.
2. `[R2]` The Matrix room and optional thread target appear once in stable system context, with no changing reply event in that context.
3. `[R3]` Full location detail is current-turn-only, while persisted history contains one short location line only when the last known location marker changes.

No config option, feature flag, compatibility shim, session migration, generic enrichment API, or new production module is permitted.

## Architecture map and root cause

| Requirement | Current location | Verified behavior |
| --- | --- | --- |
| `[R1]` | `src/mindroom/prompt_message_tags.py:13-27` | `render_msg_tag` already accepts `event_id`, so no new renderer is needed. |
| `[R1]` | `src/mindroom/coalescing_batch.py:141-151` and `:189-210` | Coalesced and queued messages already pass each source event ID and require no production change. |
| `[R1]` | `src/mindroom/execution_preparation.py:120-138` and `:367-395` | A direct current Matrix message receives sender and timestamp but not event ID. |
| `[R1]` | `src/mindroom/execution_preparation.py:236-312` | Matrix fallback and unseen history have `ResolvedVisibleMessage.event_id` but render user content as `sender: body` and assistant content as bare text. |
| `[R1]` | `src/mindroom/response_turn.py:142-190`, `src/mindroom/ai.py:1452-1477`, and `src/mindroom/teams.py:1969-2023` | Dynamic-tool continuations reuse the original `ResponseTurnContext`, so blindly reading `ctx.reply_to_event_id` would mislabel a synthetic continuation with the source event. |
| `[R1]` | `src/mindroom/conversation_state_writer.py:110-136` and `src/mindroom/post_response_effects.py:230-243` | A normal assistant response event ID becomes authoritative only after Matrix delivery and is currently written to run metadata only. |
| `[R1]` | `src/mindroom/history/interrupted_replay.py:202-254` | Canonical interrupted runs persist plain user and assistant messages even when source and visible response event IDs exist in run metadata. |
| `[R2]` | `src/mindroom/response_runner.py:191-212`, `:1551-1559`, and `:2054-2062` | The footer is appended to each model prompt, including per-turn `reply_to_event_id`. |
| `[R2]` | `src/mindroom/hooks/enrichment.py:38-47`, `src/mindroom/ai.py:1179-1184`, and `src/mindroom/teams.py:1847-1852` | The existing system-enrichment channel provides deterministic stable-before-volatile ordering for both agents and teams. |
| `[R3]` | `src/mindroom/turn_policy.py:153-195` and `src/mindroom/hooks/enrichment.py:30-35` | The location plugin's `key="location"` item is flattened into a terminal `<mindroom_message_context>` block appended to `model_prompt`. |
| `[R1][R2][R3]` | `src/mindroom/ai.py:131-158` and `:1186-1217` | The model-prompt tail is folded into the memory-bearing current user message, so both footer and full location block are stored verbatim. |
| `[R3]` | `src/mindroom/execution_preparation.py:403-411`, `src/mindroom/teams.py:204-207`, and `:2024-2058` | The team path flattens every prepared message into one string before `Team.arun`, so an `add_to_agent_memory=False` message inserted in the common path would still be persisted. |
| `[R1][R2][R3]` | `src/mindroom/history/runtime.py:610-720` and `:1282-1290` | MindRoom selects replay limits but does not recompose stored messages. |
| `[R1][R2][R3]` | `.venv/lib/python3.13/site-packages/agno/session/agent.py:115-236` | Agno returns stored run messages verbatim for replay. |
| `[R3]` | `.venv/lib/python3.13/site-packages/agno/agent/_response.py:676-679` and `agno/team/_response.py:460-463` | Agno persists only messages whose `add_to_agent_memory` flag is true before the team-specific flattening described above. |
| `[R3]` | `src/mindroom/history/compaction.py:850-868`, `:906-925`, and `:1135-1184` | Compaction consumes the same stored messages, so the persisted representation must already be compact. |

The footer and location block are therefore stored verbatim rather than recomposed on replay.

The fix must change new current-turn storage and post-delivery assistant storage, not add a replay transformer.

## R3 mechanism decision

1. `[R3]` Choose the core-side location split; the corrected plan is approximately +140 net production lines and touches no external repository.
2. `[R3]` The `replay_text` route is estimated at roughly +145–155 lines once its three carrier fields, backward dedup, team fix, docs/API contract, and companion plugin edit are included.
3. `[R3]` The core split removes four carrier-path files and the deploy-order dependency, at the cost of one two-call-site parser in `execution_preparation.py`.
4. `[R3]` Parse location text as order-independent `key: value` fields, so plugin line reordering is harmless.
5. `[R3]` If the plugin renames fields, fail closed by keeping full detail transient and omitting only the unavailable short marker; never persist the full GPS block.
6. `[R3]` With `replay_text`, deploying core before the external plugin silently leaves the reported full-GPS persistence bug active.
7. `[R3]` The current plugin already emits the stable `location` key and field names at `/home/basnijholt/.mindroom-chat/plugins/location-enrich/hooks.py:244-296`.
8. `[R3]` Reject `replay_text` explicitly because its marginal extensibility is not requested and its out-of-repo rollout failure is worse than the core parser's fail-closed behavior.

## Per-file implementation plan

### `src/mindroom/execution_preparation.py`

1. `[R1]` Add `current_event_id: str | None = None` alongside the existing current sender and timestamp arguments in `_build_matrix_prompt_with_history`, `_messages_with_current_prompt`, `_messages_with_capped_context`, `_build_unseen_context_messages`, `_build_thread_history_messages`, and `_prepare_execution_context_common`.
2. `[R1]` Pass `event_id=current_event_id` to the existing direct `render_msg_tag` call.
3. `[R1]` Preserve `current_prompt_is_structured=True` behavior so coalesced and queued prompts retain their existing per-child event IDs without an outer duplicate wrapper.
4. `[R1]` In `_context_message_from_visible_message`, wrap every real `ResolvedVisibleMessage` with its speaker and `message.event_id`, while retaining the existing Agno role, media, attachment annotation, length cap, relay label, tool-marker stripping, and partial-reply guidance.
5. `[R1]` Remove the redundant `sender: ` text prefix only for content that is now inside `<msg from="...">`.
6. `[R1]` For an in-progress partial whose sender was replaced by a `You (...)` guidance label at `src/mindroom/execution_preparation.py:643-648`, use `response_sender_id` for `<msg from>` and keep the guidance label in the CDATA body so the real event remains addressable without losing partial-state semantics.
7. `[R1]` Do not add or move timestamps in fallback history, and do not change existing timestamp-prefix behavior.
8. `[R3]` Add one private location split function called by both `prepare_agent_execution_context` and `_prepare_bound_team_execution_context`, satisfying the two-call-site helper constraint.
9. `[R3]` Inspect only the terminal core-rendered `<mindroom_message_context>` block and only its `key="location"` item, leaving every other enrichment item byte-for-byte in the persisted prompt.
10. `[R3]` Remove the full location item from the persisted prompt even when no short marker can be derived, so parser degradation cannot reintroduce the original leak.
11. `[R3]` Decode the location item's escaped text into an order-independent field map and derive one fixed short marker in this order: `📍 Home` when `at_home: true`, `📍 <nearby_place>` when the place is not empty or `unknown`, otherwise `📍 <latitude>, <longitude>` when both coordinates exist.
12. `[R3]` Scan stored scope runs backward for the most recent surviving generated `📍 ...` marker rather than checking only the immediately previous run.
13. `[R3]` Append the marker as exactly one plain line to the persisted current prompt only when there is no prior marker or its value differs.
14. `[R3]` Treat missing location data as no update and do not reset the last known marker.
15. `[R3]` For agents, deliver the extracted full location-only block immediately before the current user message through PR #1596's existing `Message(role="user", add_to_agent_memory=False)` path.
16. `[R3]` For teams, do not insert that transient message into the common message list because `render_prepared_team_messages_text` would flatten it into persisted input.
17. `[R3]` Instead, append the full `<mindroom_message_context>` block to the freshly materialized team and members' current `additional_context` after stable system enrichment and before `Team.arun`.

### `src/mindroom/response_turn.py`

1. `[R1]` Add `active_current_event_id: str | None` to the existing `DynamicContinuationRunState`.
2. `[R1]` Accept and preserve the value in `initial()`.
3. `[R1]` Clear the value in `advance()` together with the current timestamp and structured-input flag so a synthetic continuation cannot claim the original Matrix event.

### `src/mindroom/ai.py`

1. `[R1]` Seed the initial agent continuation with `ctx.reply_to_event_id`.
2. `[R1]` Pass `continuation_state.active_current_event_id` through agent preparation in both blocking and streaming attempt paths.
3. `[R1]` Do not synchronize or otherwise rewrite `TurnRecorder.user_message`.

### `src/mindroom/teams.py`

1. `[R1]` Seed the initial team continuation with `ctx.reply_to_event_id`.
2. `[R1]` Pass `continuation_state.active_current_event_id` through team preparation in both blocking and streaming attempt paths.
3. `[R3]` Keep stable system enrichment applied before the execution-preparation call so the current full location block is appended only as its volatile tail.

### `src/mindroom/response_runner.py`

1. `[R2]` Delete `_append_matrix_prompt_context`, its duplicate-detection string check, and both prompt-mutating call sites.
2. `[R2]` Add one small module-level builder, used by the existing agent and team sites, that returns `EnrichmentItem(key="matrix_message_target", cache_policy="stable", text=...)` only when the effective tool surface contains `matrix_message`.
3. `[R2]` For a thread, render one authoritative statement containing `room_id`, `thread_id`, and a concise instruction to use the current or selected `<msg event_id>` as `reply_to_event_id`.
4. `[R2]` For room-level context, render one authoritative statement containing `room_id` and an explicit instruction to call `matrix_message` without `thread_id`.
5. `[R2]` Never include a source, reply, or response event ID value in system context.
6. `[R2]` Append the stable item to the existing `system_enrichment_items` at `_agent_turn_context` and the team `ResponseTurnContext` construction, retaining the current agent tool gate and team any-member rule.
7. `[R1]` Pass `self.deps.matrix_full_id` to `ConversationStateWriter.persist_response_event_id_in_session_run` through the existing response-event callback without changing the callback type used by post-response effects.
8. `[R1]` Seed `requester_id` in `_build_turn_recorder`'s existing Matrix run metadata so a canonical interrupted user snapshot has both its source sender and source event ID without synchronizing prepared prompt text.
9. `[R1]` Pass `self.deps.matrix_full_id` to `persist_interrupted_replay_snapshot` for visible interrupted assistant responses.

### `src/mindroom/conversation_state_writer.py`

1. `[R1]` Extend `persist_response_event_id_in_session_run` with `response_sender_id`.
2. `[R1]` In the same session upsert that writes `MATRIX_RESPONSE_EVENT_ID_METADATA_KEY`, locate the final model-visible assistant message and replace its content with `render_msg_tag(sender=response_sender_id, body=run.content, event_id=response_event_id)`.
3. `[R1]` Use canonical string `run.content` rather than the possibly already wrapped message content so a changed or repeated callback cannot nest tags.
4. `[R1]` If the run has no string content or no final assistant message, persist its response metadata without fabricating message content.
5. `[R1]` Retain the existing one-upsert and same-event early-return behavior.

### `src/mindroom/history/interrupted_replay.py`

1. `[R1]` Import the existing source and response event metadata keys and `render_msg_tag`.
2. `[R1]` When `snapshot.user_message`, `requester_id`, and `MATRIX_EVENT_ID_METADATA_KEY` are present, wrap the canonical interrupted user message with its real sender and source event ID.
3. `[R1]` When `response_sender_id` and `MATRIX_RESPONSE_EVENT_ID_METADATA_KEY` are present, wrap the canonical interrupted assistant content with its real sender and visible response event ID.
4. `[R1]` Add an optional `response_sender_id` only to the existing interrupted-persistence function chain, leaving unspoken or non-Matrix interrupted snapshots bare when no visible event exists.
5. `[R1]` Do not add timestamps or a new interrupted-replay type.

### `src/mindroom/prompts.py`

1. `[R1]` Add one concise sentence to the existing `<msg>` guidance explaining that `event_id` identifies the Matrix event for reactions, edits, and `matrix_message.reply_to_event_id`.
2. `[R1]` Do not document or require a new attribute order.

### `tach.toml`

1. `[R1]` Allow `mindroom.conversation_state_writer` and `mindroom.history.interrupted_replay` to depend on `mindroom.prompt_message_tags`.
2. `[R1]` Add both consumers to `mindroom.prompt_message_tags` visibility.
3. `[R1]` Do not add a timestamp-formatting dependency because this plan deliberately omits the timestamp refactor.

## Edge cases

1. `[R2]` Room-level context has one `room_id` statement and tells the model to omit `thread_id`; it never serializes a fake thread ID.
2. `[R3]` A turn without a location item leaves the prompt unchanged and does not reset the last known marker.
3. `[R3]` A first valid location persists one baseline marker, and later turns without location persist nothing.
4. `[R3]` Repeated Home turns persist one `📍 Home`, including a Home, no-location, Home sequence.
5. `[R3]` A place or coordinate change persists one new marker while the full live snapshot remains available on every located current turn.
6. `[R3]` Reordered location lines derive the same marker because parsing is field-based rather than positional.
7. `[R3]` Renamed or malformed location fields remove the full item from persistence, keep it visible for the current turn, and omit only the unavailable marker.
8. `[R3]` If compaction removes the last marker-bearing run, the next valid location establishes one fresh baseline without a compaction-specific branch.
9. `[R3]` Agent full detail uses `add_to_agent_memory=False`, while team full detail uses the current volatile `additional_context` tail because the team input is flattened.
10. `[R1]` A direct Matrix turn receives its source event ID, while scheduled, detached, and OpenAI-compatible turns with no real Matrix event omit the attribute.
11. `[R1]` Structured coalesced and queued turns keep their existing per-message event IDs and are not double-wrapped.
12. `[R1]` Media retries of the same input retain the active event ID, while a dynamic-tool continuation clears it.
13. `[R1]` Normal assistant messages gain event IDs only after successful visible delivery.
14. `[R1]` Canonical interrupted user or assistant messages are wrapped only when their corresponding source or visible response IDs and senders are known.
15. `[R1]` In-progress partial projections retain their guidance label and real response event ID.
16. `[R2][R3]` The stable Matrix target remains before volatile team location context, preserving the within-thread prompt-cache prefix.

## Test plan

### Focused execution preparation

1. `[R1]` Extend `tests/test_execution_preparation.py` to assert direct current event ID presence, `None` omission, and no double wrapper for structured coalesced input.
2. `[R1]` Cover fallback human, completed assistant, relayed human, and in-progress partial messages, asserting roles, media, speaker semantics, and event IDs remain correct.
3. `[R3]` Cover Home, named place, coordinates, reordered fields, malformed fields, no location, non-location enrichment, and first-marker behavior.
4. `[R3]` Cover backward dedup across Home, no-location, Home and changed Home-to-Office sequences.
5. `[R3]` Assert agent full detail is in a transient `add_to_agent_memory=False` message and absent from the persisted current message.
6. `[R3]` Assert team full detail is in current `additional_context` and absent from `render_prepared_team_messages_text`.

### Continuation and response persistence

1. `[R1]` Extend `tests/test_response_turn.py` to prove initial and same-input retry state retain the event ID and `advance()` clears it.
2. `[R1]` Extend `tests/test_conversation_state_writer.py` for agent and team sessions, final-assistant wrapping, idempotency, and metadata-only runs.
3. `[R1]` Extend `tests/test_interrupted_replay.py` for wrapped source user and visible assistant messages, plus absent-ID cases.
4. `[R1]` Add one response-lifecycle assertion that the existing post-delivery callback passes `matrix_full_id` and persists the new assistant wrapper.

### Stable Matrix target

1. `[R2]` Replace footer assertions in `tests/test_response_runner_agent.py`, `tests/test_response_runner_team_streaming.py`, and `tests/test_response_runner_session_lifecycle.py`.
2. `[R2]` Assert the user/model prompt contains no `[Matrix metadata for tool calls]` block.
3. `[R2]` Assert blocking and streaming agent and team contexts receive one stable item only when `matrix_message` is available.
4. `[R2]` Cover thread and room-level wording, including the explicit no-`thread_id` instruction.
5. `[R2]` Assert two turns with different source event IDs produce byte-identical Matrix target system text.

### Persistence and compaction integration

1. `[R3]` Extend `tests/test_history_prepare_integration.py` with Home, Home, Office and assert replay contains exactly one `📍 Home`, one `📍 Office`, and no full GPS fields.
2. `[R3]` Assert a no-location gap does not cause the same marker to be emitted again.
3. `[R3]` Assert compaction input contains only short markers and never the full location item.
4. `[R1][R2]` Assert persisted replay contains new message event IDs and no Matrix footer.

### Validation

1. `[R1][R2][R3]` Run the focused test files above.
2. `[R1][R2][R3]` Run `uv run pytest`.
3. `[R1]` Run `uv run tach check --dependencies --interfaces`.
4. `[R1][R2][R3]` Run `uv run pre-commit run --all-files`.

## Existing-history policy

1. `[R1][R2][R3]` Do not migrate, rewrite, parse, or lazily normalize any pre-change run.
2. `[R1][R2][R3]` Old footers, full GPS blocks, and unwrapped replies remain until ordinary configured retention, compaction, or session reset removes them.
3. `[R3]` Ignore old full location blocks when finding the last compact marker, so the first valid post-upgrade location writes one baseline.
4. `[R1][R2][R3]` New runs use only the new representation; no compatibility branch remains in the hot path.

## Out of scope

1. `[R3]` Do not add `EnrichmentItem.replay_text`, `message_enrichment_items`, an external location-plugin edit, or a general enrichment persistence policy.
2. `[R1]` Do not reorder `<msg>` attributes, refactor fallback timestamps, remove `_timestamp_thread_history_user_turns`, or add assistant timestamps.
3. `[R1]` Do not synchronize `TurnRecorder.user_message` with prepared prompt content.
4. `[R1][R2][R3]` Do not add config, feature flags, migrations, compatibility shims, database fields, or session-state keys.
5. `[R3]` Do not change GPS fetching, movement classification, geofencing, place resolution, or location thresholds.
6. `[R3]` Do not redesign team execution to pass provider-native message lists.
7. `[R3]` Do not add a compaction branch or summary schema for location.
8. `[R2]` Do not change `matrix_message` tool arguments or Matrix room/thread resolution.
9. `[R1][R2][R3]` Do not address ISSUE-239, ISSUE-240, retrieved-memory behavior already fixed by PR #1596, or unrelated prompt slimming.
10. `[R1][R2][R3]` Do not update documentation or mirrored skill references in this minimum implementation.

## Scope budget

The estimated net production delta is approximately **+140 lines excluding tests**.

1. `[R1][R3]` `src/mindroom/execution_preparation.py`: approximately +85 net lines for event plumbing, fallback wrapping, the two-call-site location split, backward marker lookup, and agent/team current-only placement.
2. `[R1]` `src/mindroom/response_turn.py`: approximately +6 net lines for active event identity.
3. `[R1]` `src/mindroom/ai.py`: approximately +5 net lines for initial and active event propagation.
4. `[R1][R3]` `src/mindroom/teams.py`: approximately +6 net lines for active event propagation and ordering confirmation.
5. `[R1][R2]` `src/mindroom/response_runner.py`: approximately +5 net lines after deleting the footer and adding the stable target plus existing persistence plumbing.
6. `[R1]` `src/mindroom/conversation_state_writer.py`: approximately +12 net lines for normal assistant wrapping.
7. `[R1]` `src/mindroom/history/interrupted_replay.py`: approximately +14 net lines for source and response wrappers.
8. `[R1]` `src/mindroom/prompts.py`: approximately +2 net lines of event-ID guidance.
9. `[R1]` `tach.toml`: approximately +5 net boundary lines.

No other production, documentation, plugin, config, or schema file is in scope.
