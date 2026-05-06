# Summary

Top duplication candidate: `_image_artifacts_from_blocks` and `_audio_artifacts_from_blocks` in `src/mindroom/mcp/results.py` repeat the same MCP media-block extraction pattern with only the block class and Agno media constructor changed.
No meaningful cross-module duplication was found for MCP result text/resource summarization or MCP `CallToolResult` to Agno `ToolResult` conversion.
Nearby JSON preview and media conversion helpers are related, but they intentionally use different inputs, encodings, or fallback behavior.

# Coverage TSV

```coverage-tsv
qualname	kind	lines	status	search_terms	candidates_checked
_summarize_embedded_resource	function	lines 28-35	none-found	EmbeddedResource TextResourceContents BlobResourceContents "Embedded resource" mimeType	src/mindroom/mcp/results.py:28; tests/test_mcp_results.py:33; tests/test_mcp_results.py:80
_summarize_resource_link	function	lines 38-46	none-found	ResourceLink "Resource link" title description mimeType	src/mindroom/mcp/results.py:38; tests/test_mcp_results.py:41
_compact_structured_content	function	lines 49-52	related-only	structuredContent json.dumps sort_keys ensure_ascii compact structured	src/mindroom/mcp/results.py:49; src/mindroom/tool_system/events.py:52; src/mindroom/approval_manager.py:106; src/mindroom/tool_system/output_files.py:381
_text_lines_from_blocks	function	lines 55-64	none-found	TextContent EmbeddedResource ResourceLink content_blocks text lines	src/mindroom/mcp/results.py:55; tests/test_mcp_results.py:30; tests/test_mcp_results.py:67
_image_artifacts_from_blocks	function	lines 67-77	duplicate-found	ImageContent base64.b64decode Image(content mime_type) image artifacts	src/mindroom/mcp/results.py:67; src/mindroom/mcp/results.py:80; src/mindroom/matrix/image_handler.py:18; src/mindroom/attachment_media.py:20
_audio_artifacts_from_blocks	function	lines 80-90	duplicate-found	AudioContent base64.b64decode Audio(content mime_type) audio artifacts	src/mindroom/mcp/results.py:80; src/mindroom/mcp/results.py:67; src/mindroom/voice_handler.py:324; src/mindroom/attachment_media.py:20
raise_for_mcp_call_error	function	lines 93-102	none-found	isError MCPToolCallError "MCP tool call failed" structuredContent	src/mindroom/mcp/results.py:93; src/mindroom/mcp/manager.py:160; src/mindroom/custom_tools/scheduler.py:17
tool_result_from_call_result	function	lines 105-115	none-found	tool_result_from_call_result CallToolResult ToolResult images audios content	src/mindroom/mcp/results.py:105; src/mindroom/mcp/manager.py:215; src/mindroom/mcp/toolkit.py:94; src/mindroom/tool_system/output_files.py:369
```

# Findings

## 1. MCP image and audio artifact extraction repeat the same behavior

- `src/mindroom/mcp/results.py:67` iterates over content blocks, filters by `ImageContent`, base64-decodes `block.data`, falls back to UTF-8 bytes on decode failure, and appends `Image(content=..., mime_type=block.mimeType)`.
- `src/mindroom/mcp/results.py:80` performs the same sequence for `AudioContent` and `Audio(content=..., mime_type=block.mimeType)`.

The shared behavior is "extract MCP inline binary media blocks into Agno media artifacts."
The only differences to preserve are the MCP block type and the Agno media constructor.
This is active duplication, but it is local and small.

## Related but not duplicate

- `src/mindroom/matrix/image_handler.py:18` and `src/mindroom/voice_handler.py:324` also produce Agno media objects, but their inputs are downloaded Matrix event bytes, not MCP inline base64 content blocks.
- `src/mindroom/attachment_media.py:20` also maps media records to Agno media objects, but it uses file paths from persisted attachments and covers audio, image, file, and video records.
- `src/mindroom/tool_system/events.py:52`, `src/mindroom/approval_manager.py:106`, and `src/mindroom/tool_system/output_files.py:381` compact JSON-like values for display or serialization, but their behavior differs from `_compact_structured_content`: they use `ensure_ascii=False`, sometimes indent output, and sometimes fall back to `str()` or raise on non-normalizable values.
- `src/mindroom/custom_tools/scheduler.py:17` raises typed errors from scheduler response text, but it does not inspect MCP `isError`, MCP content blocks, or `structuredContent`.

# Proposed Generalization

No cross-module refactor recommended.
If this file is touched for MCP result work later, a minimal local helper could remove the media extraction duplication:

1. Add a private helper in `src/mindroom/mcp/results.py` that decodes MCP block `data` with the current base64 fallback.
2. Add a private generic extractor parameterized by block type and media constructor, or keep two loops and only share the decode helper.
3. Update `_image_artifacts_from_blocks` and `_audio_artifacts_from_blocks` to call the helper.
4. Keep the existing public functions and return shapes unchanged.
5. Run `tests/test_mcp_results.py`.

# Risk/tests

Risk is low if only the shared decode helper is extracted.
The main behavior to preserve is the current permissive fallback from invalid base64 to UTF-8 bytes.
Relevant tests are `tests/test_mcp_results.py`, especially text/resource conversion, error conversion, binary resource summaries, and audio artifact conversion.
Adding one invalid-base64 image or audio test would cover the fallback if the local helper is introduced.
