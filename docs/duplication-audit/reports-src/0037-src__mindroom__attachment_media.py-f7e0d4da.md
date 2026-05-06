# Summary

No meaningful duplication found.
`src/mindroom/attachment_media.py` owns a narrow bridge from persisted `AttachmentRecord` metadata to Agno media objects and the one higher-level resolver that adds context filtering and timing.
Other modules contain related media conversion or attachment resolution behavior, but they operate on different input shapes or expose different payloads.

# Coverage TSV

```coverage-tsv
qualname	kind	lines	status	search_terms	candidates_checked
_attachment_records_to_media	function	lines 20-69	related-only	AttachmentRecord to Agno media; record.kind; Audio/Image/File/Video constructors; filepath mime_type filename	src/mindroom/mcp/results.py:67; src/mindroom/mcp/results.py:80; src/mindroom/matrix/image_handler.py:18; src/mindroom/voice_handler.py:324; src/mindroom/attachments.py:866
resolve_attachment_media	function	lines 72-119	related-only	resolve_attachment_media; resolve_attachments plus filter_attachments_for_context; attachment_ids resolved IDs media timing	src/mindroom/inbound_turn_normalizer.py:330; src/mindroom/attachments.py:627; src/mindroom/attachments.py:642; src/mindroom/attachments.py:793; src/mindroom/custom_tools/attachments.py:85
```

# Findings

No real duplication found.

Related behavior checked:

- `src/mindroom/mcp/results.py:67` and `src/mindroom/mcp/results.py:80` also convert external artifacts into Agno `Image` and `Audio` objects.
  This is not a duplicate of `_attachment_records_to_media` because MCP results decode base64 content blocks and construct in-memory `content=` media, while attachment media uses persisted `AttachmentRecord.local_path` and produces `filepath=` media across audio, image, file, and video types.
- `src/mindroom/matrix/image_handler.py:18` and `src/mindroom/voice_handler.py:324` also construct Agno media objects after Matrix downloads.
  These are live Matrix download paths using bytes and Matrix MIME helpers, not persisted attachment-record conversion.
- `src/mindroom/attachments.py:866` renders `AttachmentRecord` objects into tool JSON payloads and checks `record.local_path.is_file()`.
  It shares the same source record type and availability check but produces metadata dictionaries rather than model media inputs.
- `src/mindroom/attachments.py:627`, `src/mindroom/attachments.py:642`, and `src/mindroom/attachments.py:793` provide lower-level attachment ID resolution, context filtering, and thread-root attachment discovery.
  `resolve_attachment_media` composes these helpers rather than duplicating their internals.
- `src/mindroom/custom_tools/attachments.py:85` resolves attachment IDs for tool responses.
  It intentionally returns tool-facing payload dictionaries via `attachments_for_tool_payload`, not Agno media objects, so the behavior is related but distinct.

# Proposed Generalization

No refactor recommended.
The existing split is appropriate: `attachments.py` owns attachment metadata resolution and context filtering, while `attachment_media.py` owns conversion into model media inputs.
Extracting a shared helper for Agno media constructors would likely add indirection without removing meaningful duplicated behavior.

# Risk/Tests

No production code changes were made.
If this module is refactored later, tests should continue to cover:

- Missing local files are skipped during media conversion.
- File attachments preserve `filename` and tolerate Agno MIME allow-list `ValueError`.
- Context filtering rejects cross-room and cross-thread attachment IDs before media construction.
- Timing metadata from `resolve_attachment_media` preserves requested, resolved, rejected, and per-media counts.
