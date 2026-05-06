## Summary

Top duplication candidate: `download_image` in `src/mindroom/matrix/image_handler.py` and `register_image_attachment` in `src/mindroom/attachments.py` repeat the same Matrix image MIME resolution and mismatch logging flow after obtaining image bytes.
The Matrix download/decrypt behavior itself is already shared through `download_media_bytes`, so the duplicated area is limited to image MIME handling and warning emission.

## Coverage TSV

```coverage-tsv
qualname	kind	lines	status	search_terms	candidates_checked
download_image	async_function	lines 18-46	duplicate-found	download_image, download_media_bytes, resolve_image_mime_type, Image MIME mismatch, register_image_attachment	src/mindroom/attachments.py:523, src/mindroom/matrix/media.py:137, src/mindroom/voice_handler.py:329, src/mindroom/attachment_media.py:39, src/mindroom/mcp/results.py:67
```

## Findings

### Duplicate image MIME resolution and mismatch warning

`src/mindroom/matrix/image_handler.py:35` downloads Matrix image bytes, calls `resolve_image_mime_type(image_bytes, media_mime_type(event))`, logs a mismatch warning with `event_id`, `declared_mime_type`, and `detected_mime_type`, then returns an Agno `Image` with `mime_resolution.effective_mime_type`.

`src/mindroom/attachments.py:533` performs the same image-specific MIME resolution and mismatch warning after either receiving supplied image bytes or downloading them through `download_media_bytes`.
It then persists the media using the same effective MIME type.

This is real behavioral duplication because both call sites are responsible for reconciling Matrix-declared image metadata with byte-signature detection and emitting a warning when the two disagree.
The differences to preserve are the warning message text (`Image MIME mismatch...` versus `Image attachment MIME mismatch...`) and the final consumer: `download_image` builds an in-memory Agno `Image`, while `register_image_attachment` persists bytes and attachment metadata.

### Related-only media object construction

`src/mindroom/attachment_media.py:39` constructs Agno `Image` objects from persisted attachment paths, and `src/mindroom/mcp/results.py:67` constructs Agno `Image` objects from MCP image blocks.
These are related media conversions, but they do not duplicate Matrix download/decrypt or Matrix MIME reconciliation.

### Shared download/decrypt behavior already centralized

`src/mindroom/matrix/media.py:187` provides `download_media_bytes`, including download error handling and encrypted media decryption.
`download_image` uses this helper directly, and `register_image_attachment` also uses it when bytes are not supplied.
No additional refactor is recommended for download/decrypt behavior.

`src/mindroom/voice_handler.py:329` has the analogous audio flow of downloading media bytes and returning an Agno `Audio`, but it does not perform image MIME sniffing or mismatch handling.
It is related-only.

## Proposed Generalization

A minimal helper could live in `src/mindroom/matrix/media.py`, for example `resolve_matrix_image_media(event, image_bytes, *, warning_message: str)`, returning the existing MIME resolution after emitting the mismatch warning.
Both `download_image` and `register_image_attachment` could call it while preserving their different warning messages and downstream behavior.

No broader media abstraction is recommended.
Only two active call sites duplicate the image-specific MIME warning flow, and the existing `download_media_bytes` helper already covers the larger shared IO/decryption path.

## Risk/tests

Behavior risks are limited to preserving warning message text and structured log fields, preserving `None` behavior when media download fails, and ensuring supplied `image_bytes` in `register_image_attachment` avoids a second download.

Tests that would need attention for a refactor are `tests/test_image_handler.py` for `download_image` download/decrypt/MIME behavior and `tests/test_attachments.py` for `register_image_attachment` detected MIME persistence.
