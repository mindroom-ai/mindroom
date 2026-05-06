# Summary

Top duplication candidate: `visible_content_from_content` duplicates the edit-visible-content selection in `src/mindroom/thread_utils.py`, with a stricter string-key normalization in `visible_body.py`.
Related Matrix content selection exists in `src/mindroom/conversation_resolver.py` and `src/mindroom/matrix/message_content.py`, but those paths either inspect both wrapper and edit payload metadata or normalize generic content dictionaries rather than resolving user-visible text.
No meaningful duplication found for trusted visible-body metadata, warmup suffix stripping, bundled replacement preview, or rich-reply fallback stripping.

# Coverage TSV

```coverage-tsv
qualname	kind	lines	status	search_terms	candidates_checked
_sender_is_trusted	function	lines 13-15	related-only	trusted_sender_ids sender in trusted exact internal sender check	src/mindroom/matrix/message_content.py:233; src/mindroom/matrix/client_visible_messages.py:143; src/mindroom/matrix/stale_stream_cleanup.py:1047
_strip_explicit_warmup_suffix	function	lines 18-27	none-found	STREAM_WARMUP_SUFFIX_KEY warmup_suffix body endswith suffix	src/mindroom/streaming.py:836; src/mindroom/matrix/stale_stream_cleanup.py:1115; src/mindroom/matrix/large_messages.py:48
strip_matrix_rich_reply_fallback	function	lines 30-38	none-found	rich reply fallback quoted_line_count startswith "> " splitlines	src/mindroom/approval_inbound.py:136; src/mindroom/response_runner.py:136; src/mindroom/interactive.py:392
visible_content_from_content	function	lines 41-46	duplicate-found	m.new_content visible message content visible content layer string-keyed content	src/mindroom/thread_utils.py:47; src/mindroom/conversation_resolver.py:52; src/mindroom/matrix/message_content.py:40; src/mindroom/matrix/cache/sqlite_agent_message_snapshot.py:32; src/mindroom/matrix/cache/postgres_agent_message_snapshot.py:33
visible_body_from_content	function	lines 49-70	related-only	STREAM_VISIBLE_BODY_KEY body fallback trusted sender warmup suffix	src/mindroom/matrix/message_content.py:226; src/mindroom/matrix/message_content.py:272; src/mindroom/matrix/client_thread_history.py:182; src/mindroom/matrix/stale_stream_cleanup.py:1131; src/mindroom/streaming.py:834
has_trusted_stream_body_metadata	function	lines 73-75	related-only	STREAM_VISIBLE_BODY_KEY STREAM_WARMUP_SUFFIX_KEY metadata presence	src/mindroom/matrix/message_content.py:235; src/mindroom/matrix/stale_stream_cleanup.py:1115; src/mindroom/matrix/large_messages.py:48
visible_body_from_event_source	function	lines 78-93	related-only	event_source content sender visible body from event source	src/mindroom/matrix/client_visible_messages.py:213; src/mindroom/matrix/client_thread_history.py:182; src/mindroom/matrix/client_thread_history.py:752
_visible_preview_content	function	lines 96-106	none-found	bundled replacement preview sender content visible_content_from_content event_source dict	src/mindroom/matrix/client_visible_messages.py:230; src/mindroom/matrix/client_visible_messages.py:274
bundled_visible_body_preview	function	lines 109-127	none-found	bundled replacement body latest_event m.replace explicit body stream metadata	src/mindroom/matrix/client_visible_messages.py:255; src/mindroom/matrix/client_visible_messages.py:274
```

# Findings

1. `visible_content_from_content` duplicates `thread_utils._visible_message_content`.

- Primary behavior: `src/mindroom/matrix/visible_body.py:41` returns `content["m.new_content"]` for edit events, otherwise the original content, and normalizes returned keys to strings.
- Duplicate behavior: `src/mindroom/thread_utils.py:47` selects `content["m.new_content"]` for mention detection, otherwise the wrapper content.
- Why duplicated: both functions answer the same Matrix question: which content layer carries user-visible message fields after an edit.
- Differences to preserve: `thread_utils._visible_message_content` returns the `m.new_content` dict as-is, while `visible_content_from_content` filters non-string keys and accepts `Mapping[str, object]`.

2. Adjacent generic content normalization is related but not a direct duplicate.

- `src/mindroom/matrix/message_content.py:40` filters arbitrary content objects down to string-keyed dictionaries.
- `src/mindroom/matrix/visible_body.py:41` does that normalization only after choosing the visible edit payload.
- These are related primitives, but not the same behavior unless the codebase wants a single helper for both "normalize any dict" and "select the visible edit layer".

# Proposed Generalization

Use `visible_content_from_content` in `thread_utils._visible_message_content`, or remove the local helper and call the Matrix helper directly where mention detection needs the visible layer.
No broader refactor recommended.

# Risk/tests

Risk is low but not zero because `thread_utils` would start dropping non-string keys from `m.new_content`.
That should be acceptable for Matrix event content, but tests around mention detection in edited messages should verify `m.mentions` and `formatted_body` are still read from edited content.
No tests were run because this task requested an audit report only and no production code edits.
