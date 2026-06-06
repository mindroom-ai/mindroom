# Matrix Ingestion Limits Report

## Scope

Validated MATRIX-MATRIX-1, MATRIX-MATRIX-2, MATRIX-MATRIX-4/5, and voice audio ingestion paths.
Focused write scope was Matrix sidecar text, media download/decrypt, attachment persistence, and voice audio handoff.

## Findings

MATRIX-MATRIX-1 was valid.
Large-message MXC sidecars were downloaded, decrypted, UTF-8 decoded, JSON parsed, and cached without byte caps.
The in-memory MXC text cache was bounded by entry count only.

MATRIX-MATRIX-2 was valid.
Shared Matrix media download returned full encrypted and decrypted payload bytes without size checks.
Image, file, video, attachment, and voice paths inherit that helper.

MATRIX-MATRIX-4/5 had enough validation in this pass.
No new quota layer was added because this area has cleanup/retention patterns but no existing local total-quota enforcement pattern for incoming Matrix media.

## Fixes

Added a 2 MiB hard cap for sidecar text bytes before decrypt, UTF-8 decode, JSON parse, in-memory cache insert, durable cache insert, and model-visible content hydration.
Added total-byte LRU eviction for the in-memory MXC text cache, with a 16 MiB total cap and existing entry-count and TTL behavior retained.
Added a 64 MiB hard cap for Matrix media bytes after download and again after decrypt.
Added an attachment persistence guard so direct media-byte registration cannot write payloads over the Matrix media cap.
Voice audio now inherits the shared media cap through `download_media_bytes`.

## Tests

Added focused regression tests for oversized plaintext sidecars, oversized encrypted sidecars before decrypt, oversized decrypted sidecars before decode, and MXC cache byte-budget eviction.
Added focused regression tests for oversized unencrypted media, encrypted media before decrypt, and decrypted media before handler/model handoff.
Added focused regression tests for direct attachment persistence over the media cap, voice audio cap inheritance, and strict typed voice download responses.

## Verification

Red run failed on the new limit tests before implementation.
Green run passed after implementation.
Latest focused verification command:

```bash
.venv/bin/python -m pytest tests/test_message_content.py::TestDownloadMxcText tests/test_image_handler.py::TestDownloadImage tests/test_attachments.py::test_register_media_attachment_rejects_payload_over_limit tests/test_voice_handler.py::TestVoiceHandler::test_download_audio_returns_none_when_media_cap_rejects_payload tests/test_voice_handler_thread.py::test_voice_handler_returns_transcription -q -n 0 --no-cov
```

Latest focused result: 27 passed.

Latest ruff command:

```bash
.venv/bin/python -m ruff check src/mindroom/matrix/message_content.py src/mindroom/matrix/media.py src/mindroom/attachments.py tests/test_message_content.py tests/test_image_handler.py tests/test_attachments.py tests/test_voice_handler.py tests/test_voice_handler_thread.py
```

Latest ruff result: all checks passed.
