Summary: Top duplication candidates are Matrix thread relation construction repeated in file-message delivery, and Matrix edit wrapper construction repeated in large-message preview paths. Markdown rendering and HTML sanitization behavior appears centralized in `src/mindroom/matrix/message_builder.py`; searches did not find another source module implementing the same sanitizer, markdown-it renderer, fenced-code transform, URL/style allowlist, or math/highlight behavior.

## Coverage TSV

```coverage-tsv
qualname	kind	lines	status	search_terms	candidates_checked
_count_repeated_characters	function	lines 138-142	none-found	count repeated characters; fence closing; repeated character loop	src/mindroom/interactive.py:369; src/mindroom/custom_tools/coding.py:53
_is_fence_closing_line	function	lines 145-152	none-found	fence closing line; fence marker; backtick tilde closing	src/mindroom/interactive.py:369; src/mindroom/interactive.py:391
_needs_block_html_boundary_after_line	function	lines 155-164	none-found	block html boundary; hr block tag; supported block html	src/mindroom/matrix/large_messages.py:214; src/mindroom/matrix/stale_stream_cleanup.py:1400
_transform_markdown_outside_fenced_code	function	lines 167-205	none-found	transform outside fenced code; splitlines keepends fence	src/mindroom/tool_system/events.py:319; src/mindroom/custom_tools/coding.py:132
_escape_html_block_openers_in_text	function	lines 208-222	none-found	escape html block openers; raw html tag line start; unsupported tags	src/mindroom/matrix/mentions.py:394; src/mindroom/delivery_gateway.py:923
_escape_html_block_openers	function	lines 225-226	none-found	escape html block openers; markdown raw html	src/mindroom/matrix/mentions.py:394; src/mindroom/matrix/stale_stream_cleanup.py:1400
_sanitize_url_attribute	function	lines 229-243	none-found	urlsplit href src allowed schemes; sanitize url attribute	src/mindroom/matrix/cache/postgres_redaction.py:30; src/mindroom/api/frontend.py:10
_sanitize_style_attribute	function	lines 246-263	none-found	sanitize style attribute; style split semicolon; expression url style	none
_normalize_input_line_endings	function	lines 266-267	none-found	normalize line endings; replace CRLF CR	none
_normalize_supported_block_html_boundaries_in_text	function	lines 270-288	none-found	normalize block html boundaries; CommonMark blank lines	src/mindroom/matrix/large_messages.py:214; src/mindroom/matrix/mentions.py:394
_normalize_supported_block_html_boundaries	function	lines 291-292	none-found	normalize supported block html boundaries; markdown fenced transform	src/mindroom/matrix/stale_stream_cleanup.py:1400; src/mindroom/matrix/mentions.py:394
_escape_unterminated_html_fragments	function	lines 295-297	none-found	escape unterminated html fragments; HTMLParser preserves text	none
_format_sanitized_attributes	function	lines 300-324	none-found	format sanitized attributes; allowed formatted_body attributes	src/mindroom/conversation_resolver.py:82; src/mindroom/dispatch_handoff.py:285
_FormattedBodyHtmlSanitizer	class	lines 327-385	none-found	HTMLParser sanitizer; handle_starttag; convert_charrefs false	none
_FormattedBodyHtmlSanitizer.__init__	method	lines 330-332	not-a-behavior-symbol	HTMLParser init parts convert_charrefs	none
_FormattedBodyHtmlSanitizer.get_html	method	lines 334-335	not-a-behavior-symbol	get html join parts	none
_FormattedBodyHtmlSanitizer.handle_starttag	method	lines 337-343	none-found	handle_starttag formatted_body allowed tags	none
_FormattedBodyHtmlSanitizer.handle_startendtag	method	lines 345-356	none-found	handle_startendtag void formatted_body tags	none
_FormattedBodyHtmlSanitizer.handle_endtag	method	lines 358-363	none-found	handle_endtag escape unsupported tags	none
_FormattedBodyHtmlSanitizer.handle_data	method	lines 365-366	none-found	handle_data escape html parser data	none
_FormattedBodyHtmlSanitizer.handle_entityref	method	lines 368-369	none-found	handle_entityref preserve entity refs	none
_FormattedBodyHtmlSanitizer.handle_charref	method	lines 371-372	none-found	handle_charref preserve char refs	none
_FormattedBodyHtmlSanitizer.handle_comment	method	lines 374-375	none-found	handle_comment escape comments	none
_FormattedBodyHtmlSanitizer.handle_decl	method	lines 377-378	none-found	handle_decl escape declarations	none
_FormattedBodyHtmlSanitizer.handle_pi	method	lines 380-381	none-found	handle_pi escape processing instructions	none
_FormattedBodyHtmlSanitizer.unknown_decl	method	lines 383-385	none-found	unknown_decl CDATA HTMLParser	none
_sanitize_formatted_body_html	function	lines 388-392	none-found	sanitize formatted_body html; sanitizer feed close	get checked with src/mindroom/conversation_resolver.py:82; src/mindroom/dispatch_handoff.py:285
_highlight	function	lines 398-406	none-found	pygments highlight get_lexer_by_name ClassNotFound	none
_render_preserved_math_inline	function	lines 409-416	none-found	math_inline dollarmath preserve dollars	none
_render_preserved_math_block	function	lines 419-427	none-found	math_block dollarmath preserve dollars div	none
_build_markdown_renderer	function	lines 430-442	none-found	MarkdownIt commonmark breaks table strikethrough dollarmath	none
markdown_to_html	function	lines 448-460	related-only	markdown_to_html calls; org.matrix.custom.html formatted_body	src/mindroom/matrix/large_messages.py:214; src/mindroom/matrix/stale_stream_cleanup.py:1400; src/mindroom/matrix/mentions.py:394
build_thread_relation	function	lines 463-495	duplicate-found	m.thread is_falling_back m.in_reply_to thread relation	src/mindroom/matrix/client_delivery.py:399; src/mindroom/voice_handler.py:247; src/mindroom/approval_transport.py:148
build_matrix_edit_content	function	lines 498-505	duplicate-found	m.new_content m.replace edit envelope	src/mindroom/matrix/large_messages.py:229; src/mindroom/matrix/large_messages.py:510; src/mindroom/matrix/client_delivery.py:448
build_message_content	function	lines 508-564	related-only	build message content msgtype body formatted_body mentions thread extra_content	src/mindroom/matrix/mentions.py:408; src/mindroom/delivery_gateway.py:923; src/mindroom/scheduling.py:783; src/mindroom/thread_summary.py:409
```

## Findings

1. `build_thread_relation` duplicates the fallback thread relation shape still built inline in `src/mindroom/matrix/client_delivery.py:399`.
   Both construct an MSC3440 `m.thread` relation with `event_id`, `is_falling_back: True`, and an `m.in_reply_to` fallback event.
   The inline file-message delivery branch raises `ValueError` when `latest_thread_event_id` is absent, while `build_thread_relation` currently uses an `assert`.
   `src/mindroom/voice_handler.py:247` also builds a simpler thread relation inline, but it omits fallback and reply metadata, so it is related rather than an exact duplicate.

2. `build_matrix_edit_content` duplicates edit-envelope construction in `src/mindroom/matrix/large_messages.py:229` and `src/mindroom/matrix/large_messages.py:510`.
   These paths manually build wrapper content with `m.new_content` and copy an existing `m.relates_to` relation for edit preview or sidecar payloads.
   The behavior differs from `build_matrix_edit_content` because the existing relation is already the outer edit relation and because large-message code also copies streaming metadata.
   Still, the envelope-building intent is the same and could use a small helper that accepts an already-resolved outer relation.

## Proposed Generalization

1. Replace the inline fallback relation in `send_file_message` with `build_thread_relation(thread_event_id=thread_id, latest_thread_event_id=latest_thread_event_id)` after preserving the current explicit `ValueError`.
2. Consider a narrowly named helper in `src/mindroom/matrix/message_builder.py`, such as `build_matrix_edit_preview_content(new_content, edit_relation, *, body=None, formatted_body=None)`, only if more large-message edit-preview call sites appear.
3. Do not refactor the markdown renderer or sanitizer internals; they are already centralized and the related call sites call `markdown_to_html` rather than duplicating its implementation.

## Risk/tests

For the thread relation dedupe, tests should cover file-message sends with `thread_id` and a missing `latest_thread_event_id` so the current `ValueError` behavior is preserved.
For any edit-envelope helper, large-message tests should verify nonterminal streaming previews, sidecar edit previews, `m.new_content`, copied metadata, and retained outer `m.replace` relation.
No production code was edited for this audit.
