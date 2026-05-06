Summary: top duplication candidates are Matrix media download/decryption behavior shared with sidecar text download in `matrix/message_content.py`, MIME normalization duplicated with attachment extension selection, and filename/body media metadata extraction that overlaps caption and attachment filename handling.

## Coverage TSV

```coverage-tsv
qualname	kind	lines	status	search_terms	candidates_checked
_ImageMimeResolution	class	lines 35-41	not-a-behavior-symbol	"_ImageMimeResolution resolve_image_mime_type MIME resolution dataclass"	src/mindroom/matrix/image_handler.py:38; src/mindroom/attachments.py:534; tests/test_image_handler.py:318
is_image_message_event	function	lines 44-46	related-only	"is_image_message_event RoomMessageImage RoomEncryptedImage isinstance image event"	src/mindroom/attachments.py:715; src/mindroom/dispatch_handoff.py:127; src/mindroom/inbound_turn_normalizer.py:289; src/mindroom/conversation_resolver.py:159; src/mindroom/turn_controller.py:525
is_file_message_event	function	lines 49-51	related-only	"is_file_message_event RoomMessageFile RoomEncryptedFile isinstance file event"	src/mindroom/dispatch_handoff.py:131; src/mindroom/matrix/media.py:61
is_video_message_event	function	lines 54-56	related-only	"is_video_message_event RoomMessageVideo RoomEncryptedVideo isinstance video event"	src/mindroom/attachments.py:509; src/mindroom/dispatch_handoff.py:129; src/mindroom/matrix/media.py:61
is_file_or_video_message_event	function	lines 59-61	related-only	"is_file_or_video_message_event file or video media dispatch"	src/mindroom/attachments.py:724; src/mindroom/matrix/media.py:71
is_audio_message_event	function	lines 64-66	related-only	"is_audio_message_event RoomMessageAudio RoomEncryptedAudio isinstance audio event"	src/mindroom/attachments.py:836; src/mindroom/conversation_resolver.py:157; src/mindroom/dispatch_handoff.py:124; src/mindroom/turn_controller.py:1890
is_matrix_media_dispatch_event	function	lines 69-71	related-only	"is_matrix_media_dispatch_event image file video dispatch event"	src/mindroom/attachments.py:836; src/mindroom/attachments.py:858; src/mindroom/dispatch_handoff.py:117; src/mindroom/inbound_turn_normalizer.py:238; src/mindroom/turn_controller.py:1087
parse_matrix_media_dispatch_event_source	function	lines 74-88	related-only	"parse_matrix_media_dispatch_event_source parse_event parse_decrypted_event RoomMessage Event.parse_event"	src/mindroom/attachments.py:688; src/mindroom/matrix/client_thread_history.py:199; src/mindroom/matrix/client_thread_history.py:288
_event_id_for_log	function	lines 91-93	related-only	"event_id_for_log event.event_id isinstance str logger event_id"	src/mindroom/matrix/media.py:177; src/mindroom/matrix/media.py:183; src/mindroom/matrix/media.py:199; src/mindroom/matrix/media.py:202
media_mime_type	function	lines 96-107	related-only	"media_mime_type mimetype info mimetype encrypted media event.mimetype"	src/mindroom/matrix/image_handler.py:38; src/mindroom/voice_handler.py:333; src/mindroom/attachments.py:514; src/mindroom/attachments.py:534
_sniff_image_mime_type	function	lines 110-127	none-found	"image/png image/jpeg image/gif image/webp image/bmp image/tiff startswith bytes signatures"	src/mindroom/matrix/avatar.py:14; tests/test_image_handler.py:303
_normalize_mime_type	function	lines 130-134	duplicate-found	"split semicolon strip lower MIME normalize mimetype guess_extension"	src/mindroom/attachments.py:167
resolve_image_mime_type	function	lines 137-149	related-only	"resolve_image_mime_type detected declared mismatch effective mime"	src/mindroom/matrix/image_handler.py:38; src/mindroom/attachments.py:534; tests/test_image_handler.py:318
extract_media_caption	function	lines 152-164	duplicate-found	"extract_media_caption filename body Matrix media caption MSC2530 filename_for_media_event"	src/mindroom/attachments.py:455; src/mindroom/dispatch_handoff.py:127; src/mindroom/voice_handler.py:218; tests/test_image_handler.py:32
_decrypt_encrypted_media_bytes	function	lines 167-184	duplicate-found	"decrypt_attachment content file key hashes sha256 iv encrypted media"	src/mindroom/matrix/message_content.py:149
download_media_bytes	async_function	lines 187-207	duplicate-found	"client.download DownloadError response.body bytes decrypt media download mxc"	src/mindroom/matrix/message_content.py:131; src/mindroom/matrix/image_handler.py:35; src/mindroom/voice_handler.py:329; src/mindroom/attachments.py:508
```

## Findings

1. `download_media_bytes` / `_decrypt_encrypted_media_bytes` duplicate the raw Matrix download and encrypted attachment decryption flow in `src/mindroom/matrix/message_content.py:131`.
   `media.py` downloads event media via `client.download(event.url)`, validates error/body shape, and decrypts encrypted Matrix media from `event.source["content"]["file"]`.
   `_download_mxc_text` in `message_content.py` separately validates and downloads an MXC URL at `src/mindroom/matrix/message_content.py:131`, checks for download failure at `src/mindroom/matrix/message_content.py:145`, then decrypts sidecar payloads with `crypto.attachments.decrypt_attachment` at `src/mindroom/matrix/message_content.py:149`.
   The behavior is not identical because sidecar text validates MXC URL syntax, uses durable/in-memory text caches, accepts `file_info` instead of a nio media event, and decodes UTF-8 text.
   The duplicated core is the transport step: download MXC bytes, handle `nio.DownloadError`, decrypt with Matrix attachment key/hash/iv fields, and return `None` on failure.

2. `_normalize_mime_type` duplicates MIME normalization in `src/mindroom/attachments.py:167`.
   Both split at the first semicolon, strip whitespace, and lowercase before using the MIME value.
   `media.py` returns `None` for invalid/empty values, while `attachments.py` only calls the logic after a truthy string guard and then maps the normalized value to an extension.
   This is a small but real duplicate because MIME parameters such as `image/png; charset=utf-8` must be canonicalized consistently for image MIME resolution and attachment file extension selection.

3. `extract_media_caption` overlaps with `_filename_for_media_event` in `src/mindroom/attachments.py:455`.
   Both inspect `event.source["content"]["filename"]` and `event.body` to interpret Matrix media metadata.
   The behavior differs: `extract_media_caption` returns `body` only when `filename` is present and differs from `body`, otherwise it returns a caller-provided default; `_filename_for_media_event` returns `filename` first and falls back to `body`.
   This is duplicated metadata extraction rather than duplicated output logic.
   A shared low-level helper could reduce repeated fragile source/content access while preserving the distinct caption and filename policies.

## Proposed Generalization

1. Add a focused byte transport helper in `src/mindroom/matrix/media.py`, for example `download_mxc_bytes(client, mxc_url, *, encrypted_file_info=None, event_id=None) -> bytes | None`.
   Keep `_download_mxc_text` responsible for caching, URL validation, and UTF-8 decoding, but delegate the download/decrypt bytes portion.
   Keep `download_media_bytes` as the event-shaped wrapper that passes `event.url`, event decryption metadata, and `event_id`.

2. Move MIME normalization into a tiny shared helper, likely `src/mindroom/mime_types.py` or a public helper in `src/mindroom/matrix/media.py` if it should remain Matrix-scoped.
   Use it from `_normalize_mime_type` or replace that private function, and from `attachments._extension_from_mime_type`.

3. Add a small Matrix media metadata helper, for example `media_content_fields(event) -> tuple[content, filename, body]` or narrower `media_filename(event)`.
   Keep `extract_media_caption` and `_filename_for_media_event` as separate policy functions so caption defaults and filename fallback behavior do not drift.

No broad refactor is recommended.

## Risk/tests

Transport deduplication has the highest risk because sidecar text downloads currently validate MXC URL shape and use cache layers before download, while event media downloads rely on nio event URLs.
Tests should cover `tests/test_message_content.py` sidecar download/decryption paths, `tests/test_image_handler.py` image download/MIME behavior, voice download tests in `tests/test_voice_handler.py`, and attachment registration paths that call `download_media_bytes`.

MIME normalization extraction is low risk but should keep existing `None` handling and parameter stripping semantics.
Tests around `tests/test_image_handler.py::TestImageMimeResolution` and attachment extension behavior should be updated or added if this is refactored.

Caption/filename helper extraction is low to medium risk because Matrix MSC2530 semantics depend on preserving the difference between a filename-only `body` and a real user caption.
Existing `tests/test_image_handler.py` caption cases and media attachment filename tests should be retained.
