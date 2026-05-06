# Summary

Top duplication candidates in `src/mindroom/interactive.py`:

1. Matrix reaction sending is repeated across interactive buttons, config confirmations, stop buttons, and the Matrix conversation tool.
2. Compact text preview formatting duplicates `matrix.client_visible_messages.message_preview`.

The interactive question persistence, response parsing, and selection handling are specialized enough that I do not recommend extracting them based on current source duplication alone.

# Coverage TSV

```coverage-tsv
qualname	kind	lines	status	search_terms	candidates_checked
TextResponseEvent	class	lines 32-37	not-a-behavior-symbol	"TextResponseEvent Protocol sender body source"	none
_InteractiveQuestion	class	lines 41-48	related-only	"room_id thread_id options creator_agent created_at pending confirmation dataclass"	src/mindroom/commands/config_confirmation.py:27
InteractiveMetadata	class	lines 52-74	related-only	"InteractiveMetadata option_map options_list interactive_metadata"	src/mindroom/final_delivery.py:9; src/mindroom/post_response_effects.py:57; src/mindroom/delivery_gateway.py:365; src/mindroom/streaming.py:210
InteractiveMetadata.from_parts	method	lines 59-70	none-found	"from_parts option_map options_list tuple dict copy"	none
InteractiveMetadata.options_as_list	method	lines 72-74	none-found	"options_as_list options_list mutable copy interactive_metadata"	src/mindroom/post_response_effects.py:131; src/mindroom/custom_tools/matrix_conversation_operations.py:692
_InteractiveResponse	class	lines 78-96	related-only	"_InteractiveResponse formatted_text interactive_metadata parse_and_format_interactive"	src/mindroom/delivery_gateway.py:366
_InteractiveResponse.option_map	method	lines 85-89	none-found	"option_map property return dict interactive_metadata.option_map"	none
_InteractiveResponse.options_list	method	lines 92-96	none-found	"options_list property options_as_list interactive response"	none
InteractiveSelection	class	lines 100-106	related-only	"InteractiveSelection question_event_id selection_key selected_value thread_id"	src/mindroom/turn_controller.py:930
_serialize_active_questions	function	lines 135-137	related-only	"asdict serialize dataclass persistence payload to_dict"	src/mindroom/commands/config_confirmation.py:44
_load_active_questions	function	lines 140-169	related-only	"deserialize persisted questions from dict from_dict created_at options"	src/mindroom/commands/config_confirmation.py:57; src/mindroom/commands/config_confirmation.py:216
_load_persisted_questions	function	lines 172-177	related-only	"read_text json.loads persisted questions empty file"	src/mindroom/custom_tools/subagents.py:105; src/mindroom/matrix/cache/sqlite_event_cache_events.py:86
_write_active_questions_atomically_locked	function	lines 180-209	related-only	"mkstemp json.dump fsync Path.replace safe_replace tmp_path.replace"	src/mindroom/constants.py:1088; src/mindroom/custom_tools/subagents.py:155; src/mindroom/config/main.py:1746; src/mindroom/api/skills.py:97
_replace_active_questions_locked	function	lines 212-217	none-found	"replace active questions clear dirty deleted ids"	none
_set_active_questions_locked	function	lines 220-223	none-found	"set active questions snapshot global"	none
_store_active_question_locked	function	lines 226-230	related-only	"store active pending question dirty deleted discard register_pending_change"	src/mindroom/commands/config_confirmation.py:77
_remove_active_question_locked	function	lines 233-240	related-only	"remove active question pop pending change deleted dirty"	src/mindroom/commands/config_confirmation.py:127
_apply_local_changes_locked	function	lines 243-256	none-found	"apply local changes dirty deleted ids merge persisted snapshot"	none
_refresh_active_questions_locked	function	lines 259-279	none-found	"flock LOCK_SH refresh persisted questions local changes"	none
_save_active_questions_locked	function	lines 282-313	none-found	"flock LOCK_EX save active questions merge persisted warning"	none
init_persistence	function	lines 316-352	related-only	"init persistence tracking interactive_questions lock restore pending changes"	src/mindroom/commands/config_confirmation.py:216
_preview_text	function	lines 355-360	duplicate-found	"compact preview join split max_length rstrip ellipsis message_preview"	src/mindroom/matrix/client_visible_messages.py:220
_find_interactive_match	function	lines 363-365	none-found	"interactive regex pattern re.search interactive code block"	none
_normalize_interactive_marker	function	lines 368-370	none-found	"normalize interactive marker strip lower split join"	none
_is_interactive_marker	function	lines 373-375	none-found	"interactive marker allowed markers normalized"	none
_is_inline_interactive_json	function	lines 378-387	none-found	"inline interactive json startswith marker brace bracket"	none
_should_warn_unparsed_interactive	function	lines 390-417	none-found	"malformed interactive fence warn inline json marker"	none
should_create_interactive_question	function	lines 420-430	none-found	"should_create_interactive_question find interactive match"	src/mindroom/custom_tools/matrix_conversation_operations.py:118
handle_reaction	async_function	lines 433-504	related-only	"ReactionEvent reacts_to event.key creator_agent ignore own agent remove active question"	src/mindroom/commands/config_confirmation.py:350; src/mindroom/stop.py:298; src/mindroom/bot.py:1406
handle_text_response	async_function	lines 507-546	none-found	"numeric text response active interactive questions resolved_thread_id"	src/mindroom/turn_controller.py:1564; src/mindroom/turn_controller.py:2000
_handle_text_response_locked	function	lines 549-583	none-found	"numeric reply active question room thread creator selected_value remove"	none
parse_and_format_interactive	function	lines 586-665	related-only	"parse interactive json format question options option_map"	src/mindroom/delivery_gateway.py:371; src/mindroom/delivery_gateway.py:768; src/mindroom/streaming.py:585; src/mindroom/streaming.py:814; src/mindroom/custom_tools/matrix_conversation_operations.py:647
register_interactive_question	function	lines 668-697	related-only	"register interactive question event_id room thread option_map agent pending change"	src/mindroom/commands/config_confirmation.py:77; src/mindroom/post_response_effects.py:120; src/mindroom/custom_tools/matrix_conversation_operations.py:681
clear_interactive_question	function	lines 700-711	none-found	"clear interactive question remove active save log"	src/mindroom/custom_tools/matrix_conversation_operations.py:646
add_reaction_buttons	async_function	lines 714-747	duplicate-found	"room_send m.reaction m.annotation event_id key ignore_unverified_devices"	src/mindroom/commands/config_confirmation.py:300; src/mindroom/stop.py:350; src/mindroom/custom_tools/matrix_conversation_operations.py:330
_cleanup	function	lines 750-758	related-only	"cleanup clear module state pending changes"	src/mindroom/commands/config_confirmation.py:295
```

# Findings

## 1. Matrix reaction send payload construction is duplicated

`interactive.add_reaction_buttons` sends one `m.reaction` event per option with:

- `message_type="m.reaction"`
- `content["m.relates_to"]["rel_type"] == "m.annotation"`
- target `event_id`
- reaction `key`
- `ignore_unverified_devices_for_config(config)`
- response type check against `nio.RoomSendResponse`

The same payload shape and delivery policy appear in:

- `src/mindroom/commands/config_confirmation.py:300` for confirm/cancel reactions.
- `src/mindroom/stop.py:350` for the stop reaction.
- `src/mindroom/custom_tools/matrix_conversation_operations.py:330` for arbitrary tool reactions.

Why this is duplicated: each call site manually constructs the same Matrix annotation reaction envelope and calls `client.room_send` with the same Matrix event type and encryption-device policy.

Differences to preserve:

- `interactive.add_reaction_buttons` accepts multiple option dictionaries and logs per-option failures.
- `config_confirmation.add_confirmation_reactions` uses fixed confirm/cancel keys and distinct warning labels.
- `stop.StopManager.add_stop_button` needs the returned reaction event ID and an outbound cache notification payload.
- `matrix_conversation_operations._message_react` defaults an empty reaction to `👍` and returns a tool result instead of logging only.

## 2. Compact preview text formatting is duplicated

`interactive._preview_text` at `src/mindroom/interactive.py:355` compacts whitespace with `" ".join(text.split())`, returns the compact body when short enough, and otherwise truncates with `...`.

`src/mindroom/matrix/client_visible_messages.py:220` implements the same behavior as `message_preview`, with the only meaningful differences that it accepts `object`, returns `""` for non-strings, and defaults to length `120` instead of `160`.

Why this is duplicated: both helpers produce log/UI-safe one-line text previews using the same whitespace compaction and truncation algorithm.

Differences to preserve:

- Interactive warnings currently pass only `str` and use `max_length=160`.
- Client-visible message previews accept non-string inputs and use `max_length=120`.

# Proposed Generalization

1. Add a small Matrix reaction helper, likely in `src/mindroom/matrix/message_content.py` or a new focused `src/mindroom/matrix/reactions.py`, with a pure payload builder such as `build_reaction_content(event_id: str, key: str) -> dict[str, object]`.
2. Optionally add a thin async sender helper only if the call sites can still preserve their return/logging needs without hiding important behavior.
3. Replace `interactive.add_reaction_buttons`, `config_confirmation.add_confirmation_reactions`, `StopManager.add_stop_button`, and `MatrixConversationOperations._message_react` payload literals with the shared builder.
4. Replace `interactive._preview_text` with `message_preview(text, max_length=160)` or move the shared compact/truncate helper to a neutral text utility if importing Matrix visible-message code from `interactive.py` is considered the wrong dependency direction.

# Risk/tests

Reaction helper risk is low if limited to payload construction, but tests should cover exact `m.reaction` content for interactive buttons, config confirmations, stop buttons, and Matrix tool reactions.

An async reaction sender helper would be higher risk because current call sites differ in logging, return values, and cache notification behavior; I do not recommend that as the first refactor.

Preview helper risk is low, but tests should assert malformed interactive warning previews still truncate at 160 characters and preserve current whitespace compaction.
