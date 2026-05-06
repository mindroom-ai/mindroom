Summary: The strongest duplication candidate is Matrix media downloading and encrypted attachment decryption, which is implemented for text sidecars in `message_content.py` and for image/file/video/audio media in `media.py`.
There is also repeated string-key normalization of Matrix event/content mappings across nearby Matrix modules, but it is small and often localized to type narrowing.
No broad refactor is recommended from this file alone.

```coverage-tsv
qualname	kind	lines	status	search_terms	candidates_checked
_extract_large_message_v2_content	function	lines 29-37	related-only	"json.loads sidecar content dict string keys matrix event content"	src/mindroom/matrix/large_messages.py:414, src/mindroom/matrix/large_messages.py:420, src/mindroom/matrix/cache/sqlite_event_cache_events.py:86, src/mindroom/matrix/cache/postgres_event_cache_events.py:124
_normalized_content_dict	function	lines 40-44	duplicate-found	"if isinstance(content, dict) string-key dict comprehension event source content"	src/mindroom/matrix/visible_body.py:41, src/mindroom/matrix/visible_body.py:85, src/mindroom/matrix/media.py:78, src/mindroom/matrix/client_visible_messages.py:206, src/mindroom/matrix/client_thread_history.py:242, src/mindroom/dispatch_handoff.py:108, src/mindroom/coalescing_batch.py:62
is_v2_sidecar_text_preview	function	lines 47-58	none-found	"io.mindroom.long_text version 2 matrix_event_content_json msgtype m.file"	src/mindroom/matrix/large_messages.py:118, src/mindroom/inbound_turn_normalizer.py:212, src/mindroom/dispatch_handoff.py:170, src/mindroom/turn_controller.py:575
_sidecar_content_for_resolution	function	lines 61-70	related-only	"m.new_content io.mindroom.long_text sidecar metadata edit content"	src/mindroom/matrix/large_messages.py:453, src/mindroom/matrix/visible_body.py:41, src/mindroom/matrix/client_visible_messages.py:335
_sidecar_mxc_url	function	lines 73-84	duplicate-found	"content url file url mxc encrypted unencrypted file info"	src/mindroom/matrix/media.py:193, src/mindroom/matrix/large_messages.py:102, src/mindroom/matrix/large_messages.py:113, src/mindroom/matrix/large_messages.py:380
_download_mxc_text	async_function	lines 87-189	duplicate-found	"client.download decrypt_attachment DownloadResponse DownloadError utf-8 mxc cache"	src/mindroom/matrix/media.py:167, src/mindroom/matrix/media.py:187, src/mindroom/matrix/large_messages.py:293, src/mindroom/matrix/cache/sqlite_event_cache.py:586, src/mindroom/matrix/cache/postgres_event_cache.py:962
extract_and_resolve_message	async_function	lines 192-252	related-only	"extract visible message resolve canonical content visible_body_from_content msgtype"	src/mindroom/matrix/client_visible_messages.py:154, src/mindroom/matrix/client_visible_messages.py:116, src/mindroom/matrix/client_thread_history.py:170
extract_edit_body	async_function	lines 255-282	related-only	"extract edit body m.new_content visible body m.replace"	src/mindroom/matrix/client_visible_messages.py:174, src/mindroom/matrix/client_visible_messages.py:401, src/mindroom/matrix/conversation_cache.py:249
resolve_event_source_content	async_function	lines 285-305	related-only	"resolve event source content hydrate sidecar content"	src/mindroom/matrix/client_visible_messages.py:194, src/mindroom/conversation_resolver.py:531, src/mindroom/conversation_resolver.py:603, src/mindroom/matrix/client_thread_history.py:741
_resolve_canonical_content	async_function	lines 308-341	none-found	"hydrate canonical v2 sidecar download mxc json content"	src/mindroom/matrix/large_messages.py:414, src/mindroom/matrix/client_visible_messages.py:268, src/mindroom/matrix/client_thread_history.py:256
_clean_expired_cache	function	lines 344-359	related-only	"expired cache ttl OrderedDict popitem last false clear test cache"	src/mindroom/matrix/large_messages.py:155, src/mindroom/matrix/large_messages.py:160
_clear_mxc_cache	function	lines 362-364	not-a-behavior-symbol	"clear mxc cache test helper"	none
```

Findings:

1. Matrix MXC download and encrypted attachment decryption are duplicated.
   `src/mindroom/matrix/message_content.py:87` downloads MXC content through `client.download(mxc=mxc_url)`, validates the response, decrypts encrypted file payloads with `crypto.attachments.decrypt_attachment`, decodes UTF-8 text, and caches the result.
   `src/mindroom/matrix/media.py:167` and `src/mindroom/matrix/media.py:187` perform the same core media transport steps for event-backed media: `client.download(event.url)`, response validation, and `crypto.attachments.decrypt_attachment`.
   The duplicated behavior is the Matrix media byte retrieval and optional E2EE decryption path.
   Differences to preserve: sidecar text accepts raw content/file metadata rather than a parsed `nio.RoomEncryptedMedia`, validates `mxc://` structure, decodes UTF-8, and has in-memory plus durable text caching.

2. String-key dict normalization for Matrix payloads is repeated.
   `src/mindroom/matrix/message_content.py:40` returns `{key: value for key, value in content.items() if isinstance(key, str)}` for untyped content.
   Similar comprehensions or content-dict guards appear in `src/mindroom/matrix/media.py:78`, `src/mindroom/matrix/client_visible_messages.py:206`, `src/mindroom/matrix/client_thread_history.py:242`, `src/mindroom/matrix/visible_body.py:41`, `src/mindroom/dispatch_handoff.py:108`, and `src/mindroom/coalescing_batch.py:62`.
   The shared behavior is narrowing untrusted Matrix event/content mappings to string-keyed dictionaries before passing them into typed helpers.
   Differences to preserve: some call sites accept `Mapping[str, Any]`, some accept raw `object`, and `visible_body.visible_content_from_content` intentionally unwraps `m.new_content`.

Proposed generalization:

1. No immediate production refactor is required for this audit.
2. If touching MXC retrieval later, extract a small byte-level helper in `src/mindroom/matrix/media.py`, for example `download_mxc_bytes(client, mxc_url, file_info=None) -> bytes | None`.
3. Keep sidecar-specific concerns in `message_content.py`: durable text cache, UTF-8 decoding, JSON sidecar parsing, and canonical content resolution.
4. If string-key normalization keeps spreading, expose a tiny `string_keyed_dict(value: object) -> dict[str, Any]` helper in a Matrix utility module and migrate only active call sites being edited.

Risk/tests:

- MXC download refactoring would need tests for unencrypted sidecar download, encrypted sidecar download, invalid MXC URL rejection, non-download responses, UTF-8 decode failures, in-memory cache hits, durable cache hits, and durable cache persistence failures.
- A shared normalization helper is low risk but can subtly change object identity behavior; `resolve_event_source_content` currently returns the original event source when resolved content is the same object.
- Existing sidecar hydration and visible message tests should cover `extract_and_resolve_message`, `extract_edit_body`, `resolve_event_source_content`, and thread-history callers after any future refactor.
