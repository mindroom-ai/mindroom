## Summary

Top duplication candidate: `src/mindroom/matrix/stale_stream_cleanup.py` manually builds a Matrix mention pill, visible body, and `m.mentions` metadata for one auto-resume relay instead of using the mention formatting path centralized in `src/mindroom/matrix/mentions.py`.
Most other mention-related code is adjacent but not duplicate: `thread_utils.py` parses incoming `m.mentions`/HTML pills, `scheduling.py` reuses `parse_mentions_in_text`, and `message_builder.py` is the lower-level content builder used by this module.

## Coverage TSV

```coverage-tsv
qualname	kind	lines	status	search_terms	candidates_checked
_MentionToken	class	lines 22-27	none-found	"_MentionToken start end localpart explicit_user_id mention token dataclass"	none
_MentionResolution	class	lines 31-34	none-found	"_MentionResolution plain_text markdown_text user_id matrix.to"	none
_MentionReplacement	class	lines 38-40	none-found	"_MentionReplacement replacement start end markdown_text"	none
parse_mentions_in_text	function	lines 43-78	related-only	"parse_mentions_in_text extract mentioned agents from text m.mentions"	src/mindroom/scheduling.py:1219, src/mindroom/thread_utils.py:27
_scan_mention_tokens	function	lines 81-90	none-found	"scan mention tokens explicit matrix id agent alias overlap"	none
_scan_explicit_matrix_id_tokens	function	lines 93-110	none-found	"explicit matrix id tokens @\\S+ parse_current_matrix_user_id"	none
_scan_agent_alias_tokens	function	lines 113-131	related-only	"agent alias mention regex @(mindroom_)? word voice mention pattern"	src/mindroom/voice_handler.py:35, src/mindroom/voice_handler.py:520
_mention_localpart	function	lines 134-136	related-only	"mention localpart split colon room_alias_localpart MatrixID.parse username"	src/mindroom/matrix_identifiers.py:68, src/mindroom/matrix/identity.py:43
_resolve_mention_tokens	function	lines 139-166	none-found	"resolve mention tokens replacements user_id markdown_text"	none
_resolve_mention_token	function	lines 169-190	none-found	"resolve mention token explicit_user_id agent alias"	none
_resolve_explicit_matrix_id_token	function	lines 193-215	related-only	"explicit Matrix ID token literal user resolution agent_name MatrixID.agent_name"	src/mindroom/thread_utils.py:124, src/mindroom/scheduling.py:1233
_resolve_agent_alias_token	function	lines 218-236	related-only	"resolve agent alias localpart has_server_name find matching agent"	src/mindroom/voice_handler.py:520
_agent_mention_resolution	function	lines 239-253	duplicate-found	"matrix.to display_name mentioned_user_ids build_message_content auto resume mention"	src/mindroom/matrix/stale_stream_cleanup.py:1391
_literal_user_resolution	function	lines 256-262	none-found	"literal user resolution matrix.to user_id markdown link"	none
_extract_longest_valid_matrix_user_id	function	lines 265-271	none-found	"longest valid matrix user id prefix parse_current_matrix_user_id trailing punctuation"	none
_is_valid_explicit_matrix_user_id	function	lines 274-280	related-only	"parse_current_matrix_user_id ValueError bool valid matrix user id"	src/mindroom/matrix/identity.py:144
_find_matching_agent_name_for_localpart	function	lines 283-294	related-only	"configured agent name localpart case-insensitive agent_name config.agents"	src/mindroom/matrix/identity.py:67, src/mindroom/thread_utils.py:124
_localpart_candidate_names	function	lines 297-326	related-only	"mindroom namespace suffix agent_username_localpart managed namespace localpart"	src/mindroom/matrix_identifiers.py:26, src/mindroom/matrix_identifiers.py:31
_range_overlaps_existing	function	lines 329-331	none-found	"range overlaps existing start end spans replacements"	none
_apply_replacements	function	lines 334-351	none-found	"apply replacements text spans last_end join markdown plain"	none
format_message_with_mentions	function	lines 354-416	duplicate-found	"format_message_with_mentions build_message_content markdown_to_html matrix.to m.mentions inherited mentions"	src/mindroom/matrix/stale_stream_cleanup.py:1391, src/mindroom/matrix/message_builder.py:508, src/mindroom/matrix/client_delivery.py:420, src/mindroom/streaming.py:823
```

## Findings

### 1. Auto-resume relay manually duplicates outgoing mention formatting

`src/mindroom/matrix/mentions.py:239` resolves an agent mention into:

- plain text body containing the target MXID.
- Markdown link text using `@{display_name}` and `https://matrix.to/#/{target_user_id}`.
- `m.mentions.user_ids` through `format_message_with_mentions` and `build_message_content`.

`src/mindroom/matrix/stale_stream_cleanup.py:1391` repeats the same behavior manually for `_build_auto_resume_relay_content`: it gets an agent Matrix ID, gets a display name, builds a visible `@{display_name}` body, creates a `matrix.to` formatted body through `markdown_to_html`, sets `mentioned_user_ids`, and then calls `build_message_content`.

Why this is duplicated: both paths are creating a Matrix event that visibly mentions a configured MindRoom agent and carries the corresponding `m.mentions` metadata.
The difference to preserve is visible body text: `format_message_with_mentions("@agent ...")` would currently produce a plain body with the full MXID, while `_build_auto_resume_relay_content` uses `@DisplayName`.
That means this is real duplication, but not a drop-in replacement without deciding which visible body is canonical for auto-resume notices.

### 2. Incoming mention extraction is related, not duplicate

`src/mindroom/thread_utils.py:27` extracts mentioned user IDs from received Matrix content by reading `m.mentions.user_ids`, with a fallback to Matrix HTML pills in `formatted_body`.
This overlaps conceptually with `format_message_with_mentions` producing `m.mentions` and `formatted_body`, but the direction is opposite: one produces outbound Matrix content, the other interprets inbound event content from clients and bridges.
No refactor is recommended between these paths.

### 3. Scheduling mention extraction already reuses the primary parser

`src/mindroom/scheduling.py:1219` extracts scheduled-task agent mentions by calling `parse_mentions_in_text`, parsing each returned user ID, and filtering to configured agents.
This is related to `_resolve_explicit_matrix_id_token`, `_resolve_agent_alias_token`, and `_find_matching_agent_name_for_localpart`, but it is not duplicate because it correctly delegates token scanning and mention resolution to the primary module.

### 4. Voice mention sanitization is related but intentionally different

`src/mindroom/voice_handler.py:520` uses a separate voice mention pattern to strip `@` from unavailable configured entities after AI transcription normalization.
This overlaps with agent alias recognition in `_scan_agent_alias_tokens`, but the behavior is deliberately different: it handles both agents and teams, preserves unmatched or allowed tokens, and does not resolve Matrix IDs or build mention metadata.
No refactor is recommended.

## Proposed Generalization

Minimal refactor candidate: add a small public helper in `src/mindroom/matrix/mentions.py` only if production edits are later requested, for example `format_direct_matrix_mention(display_text: str, target_user_id: str) -> tuple[str, str, list[str]]` or a dataclass returning visible body, formatted HTML, and mentioned user IDs.
Then `_agent_mention_resolution` and `_build_auto_resume_relay_content` could share the Matrix pill construction while preserving their different visible body choices.

No broader parser abstraction is recommended.
The existing split between outbound formatting, inbound mention detection, scheduling extraction, and voice sanitization is behaviorally justified.

## Risk/tests

The auto-resume duplication touches user-visible Matrix message content, so any refactor must preserve:

- `body` text used for previews and fallback clients.
- `formatted_body` Matrix pill rendering.
- `m.mentions.user_ids` order and de-duplication.
- thread and reply relation fields from `build_message_content`.
- `ORIGINAL_SENDER_KEY` extra content in auto-resume relay events.

Tests to update or add if this is refactored:

- focused mention formatting tests in `tests/test_mentions.py`.
- stale stream cleanup auto-resume content tests, especially assertions around `body`, `formatted_body`, and `m.mentions`.
- one regression test covering display-name body preservation if `_build_auto_resume_relay_content` keeps `@DisplayName` instead of full MXID.
