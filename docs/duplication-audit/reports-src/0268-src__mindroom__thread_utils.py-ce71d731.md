Summary: top duplication candidates for `src/mindroom/thread_utils.py` are the visible Matrix content-layer selector and canonical room/thread session ID construction.
Mention extraction and agent/thread response gating have related call sites, but the checked candidates either produce outbound mentions, consume plain text, or compose the `thread_utils` primitives rather than duplicating the same behavior.

```coverage-tsv
qualname	kind	lines	status	search_terms	candidates_checked
_extract_mentioned_user_ids	function	lines 27-44	related-only	m.mentions user_ids formatted_body matrix.to pills parse mentions	src/mindroom/matrix/mentions.py:43; src/mindroom/conversation_resolver.py:78; src/mindroom/scheduling.py:1219
_visible_message_content	function	lines 47-52	duplicate-found	m.new_content visible content layer content visible_body	src/mindroom/matrix/visible_body.py:41; src/mindroom/matrix/visible_body.py:78; src/mindroom/conversation_resolver.py:52
_is_bot_or_agent	function	lines 55-57	related-only	extract_agent_name bot_accounts bot or agent sender	src/mindroom/thread_utils.py:176; src/mindroom/teams.py:737; src/mindroom/authorization.py
check_agent_mentioned	function	lines 60-80	related-only	check_agent_mentioned mentioned_agents has_non_agent_mentions	src/mindroom/conversation_resolver.py:91; src/mindroom/turn_controller.py:578; src/mindroom/conversation_resolver.py:30
create_session_id	function	lines 83-86	duplicate-found	create_session_id session_id room_id thread_id build_session_id	src/mindroom/message_target.py:33; src/mindroom/turn_store.py:347; src/mindroom/turn_store.py:399
get_agents_in_thread	function	lines 89-121	related-only	get_agents_in_thread extract_agent_name router seen_ids thread_history	src/mindroom/scheduling.py:1288; src/mindroom/teams.py:800; src/mindroom/turn_controller.py:594
_agents_from_user_ids	function	lines 124-135	related-only	MatrixID.parse agent_name mentioned user ids agents from text	src/mindroom/scheduling.py:1231; src/mindroom/teams.py:737; src/mindroom/matrix/mentions.py:193
has_user_responded_after_message	function	lines 138-163	none-found	has_user_responded_after_message target_event_id responded after sender	thread_utils.py internal search only; none
has_multiple_non_agent_users_in_thread	function	lines 166-183	related-only	multiple non agent users in thread bot_accounts extract_agent_name	src/mindroom/thread_utils.py:186; src/mindroom/thread_utils.py:321; src/mindroom/turn_controller.py:594
thread_requires_explicit_agent_targeting	function	lines 186-202	related-only	thread_requires_explicit_agent_targeting filter_agents_by_sender_permissions multiple users	src/mindroom/turn_controller.py:594; src/mindroom/thread_utils.py:269
get_configured_agents_for_room	function	lines 205-227	related-only	configured agents room resolve_room_aliases config.agents router	src/mindroom/turn_controller.py:1028; src/mindroom/authorization.py; src/mindroom/routing.py
_has_any_agent_mentions_in_thread	function	lines 230-241	related-only	any agent mentions in thread _extract_mentioned_user_ids _agents_from_user_ids	src/mindroom/thread_utils.py:244; src/mindroom/teams.py:791; src/mindroom/turn_controller.py:578
get_all_mentioned_agents_in_thread	function	lines 244-266	related-only	all mentioned agents in thread dedupe mentions seen_ids	src/mindroom/thread_utils.py:230; src/mindroom/teams.py:791; src/mindroom/scheduling.py:1231
should_agent_respond	function	lines 269-338	related-only	should_agent_respond is_sender_allowed_for_agent_reply available_agents thread participants	src/mindroom/turn_controller.py:578; src/mindroom/turn_controller.py:1028; src/mindroom/teams.py:641
```

Findings:

1. `thread_utils._visible_message_content` duplicates the visible-content layer selection in `matrix.visible_body.visible_content_from_content`.
   `src/mindroom/thread_utils.py:47` returns `content["m.new_content"]` when it is a dict, otherwise the original content.
   `src/mindroom/matrix/visible_body.py:41` does the same Matrix replacement-aware content selection for visible body resolution and additionally normalizes the mapping to string-keyed dict output.
   The behavior is functionally the same for mention detection because both helpers decide which Matrix content object represents the visible message after edits.
   Difference to preserve: `thread_utils` currently returns the original dict object and type is `dict[str, Any]`; `visible_content_from_content` accepts a `Mapping[str, object]` and returns a shallow copy with string keys.

2. `thread_utils.create_session_id` duplicates `MessageTarget._build_session_id`.
   `src/mindroom/thread_utils.py:83` returns `room_id` for room-level sessions and `f"{room_id}:{thread_id}"` for thread sessions.
   `src/mindroom/message_target.py:33` builds the same canonical persisted session ID for resolved message targets.
   `turn_store` calls `create_session_id` at `src/mindroom/turn_store.py:347` and `src/mindroom/turn_store.py:399`, while response delivery paths use `MessageTarget.session_id`, so two sources of truth encode the same room/thread session key.
   Difference to preserve: `create_session_id` accepts a source thread ID; `MessageTarget._build_session_id` is explicitly based on a resolved thread ID after room-mode and thread-start resolution.

Related-only checks:

- `_extract_mentioned_user_ids` consumes Matrix event content (`m.mentions.user_ids`, then Matrix HTML pills).
  `matrix.mentions.parse_mentions_in_text` at `src/mindroom/matrix/mentions.py:43` is the outbound/plain-text mention parser, and `conversation_resolver` only overlays trusted payload metadata at `src/mindroom/conversation_resolver.py:78`.
  These are related mention surfaces, but they are not the same behavior.
- `_agents_from_user_ids` and `scheduling._extract_mentioned_agents_from_text` both parse mentioned IDs into `MatrixID` objects and filter to configured agents, but scheduling first parses natural-language text through `parse_mentions_in_text`.
  The shared lower-level filtering is small and already local to `thread_utils`; no active cross-module duplicate is strong enough to justify moving it.
- `get_agents_in_thread`, `has_multiple_non_agent_users_in_thread`, `thread_requires_explicit_agent_targeting`, and `should_agent_respond` are policy composition functions used by turn routing, scheduling, and teams.
  The checked call sites call these helpers or perform adjacent team/request filtering, not independent copies of the same policy.

Proposed generalization:

1. Replace `_visible_message_content` with a direct import of `visible_content_from_content` from `mindroom.matrix.visible_body`, or delete `_visible_message_content` and call the shared helper from `check_agent_mentioned`.
2. Make `create_session_id` delegate to a public session-key helper in `message_target.py`, or expose a small module-level `build_session_id(room_id, resolved_thread_id)` and have both `MessageTarget` and `thread_utils` use it.
3. Keep mention extraction and thread response policy in `thread_utils`; no broader refactor is recommended.

Risk/tests:

- Visible-content consolidation needs tests for edited messages with `m.new_content`, malformed/non-string keys, and mention fallback through `formatted_body`.
- Session-id consolidation needs tests that room-level, threaded, and resolved-thread targets continue to match existing persisted Agno session IDs.
- No production code was edited for this audit.
