Summary: The main duplication candidates are JSON-safe value normalization in `llm_request_logging.py`, atomic replace/write helpers in Matrix and handled-turn persistence, and bespoke `mindroom_output_path` schema/receipt handling in the attachments tool.
Most output-file behavior is already centralized in `src/mindroom/tool_system/output_files.py`, and related call sites generally call into it rather than reimplementing validation or writes.

```coverage-tsv
qualname	kind	lines	status	search_terms	candidates_checked
ToolOutputFilePolicy	class	lines 46-62	related-only	ToolOutputFilePolicy from_runtime runtime policy max bytes	src/mindroom/custom_tools/attachments.py:24,499; src/mindroom/api/sandbox_runner.py:1445
ToolOutputFilePolicy.from_runtime	method	lines 53-62	related-only	from_runtime runtime_paths max_bytes process_env env_file_values	src/mindroom/custom_tools/attachments.py:499; src/mindroom/api/sandbox_runner.py:1445; src/mindroom/tool_system/sandbox_proxy.py:634
_ValidatedOutputPath	class	lines 66-70	not-a-behavior-symbol	validated output path requested relative absolute overwritten	none
_SerializedToolOutput	class	lines 74-76	not-a-behavior-symbol	serialized tool output payload format	none
ToolOutputWriteResult	class	lines 80-86	not-a-behavior-symbol	write result receipt absolute byte_count overwritten	src/mindroom/custom_tools/attachments.py:611-615; src/mindroom/api/sandbox_runner.py:1450-1455
_output_redirect_max_bytes	function	lines 89-105	related-only	MINDROOM_TOOL_OUTPUT_REDIRECT_MAX_BYTES max bytes process_env env_file_values os.environ	src/mindroom/tool_system/sandbox_proxy.py:58,115,634-640; src/mindroom/constants.py:228-231
_success_receipt	function	lines 108-123	duplicate-found	mindroom_tool_output saved_to_file path bytes format overwritten sha256	src/mindroom/custom_tools/attachments.py:596-602; src/mindroom/custom_tools/attachments.py:641-644; src/mindroom/api/sandbox_runner.py:1454-1460
_error_receipt	function	lines 126-132	related-only	mindroom_tool_output error status error	src/mindroom/custom_tools/attachments.py:407-414,533,558,613; src/mindroom/api/sandbox_runner.py:1397-1408
_output_path_schema	function	lines 135-140	duplicate-found	mindroom_output_path schema anyOf null description OUTPUT_PATH_ARGUMENT_DESCRIPTION	src/mindroom/custom_tools/attachments.py:394-400
_has_output_path_argument	function	lines 143-147	none-found	inspect signature parameters function.parameters properties mindroom_output_path	none
ensure_output_path_schema_optional	function	lines 150-162	duplicate-found	properties mindroom_output_path remove required schema optional	src/mindroom/custom_tools/attachments.py:389-401
_normalize_output_path_argument	function	lines 165-170	related-only	normalize output path empty string none pop kwargs	src/mindroom/api/sandbox_runner.py:988-992
normalize_output_path_argument	function	lines 173-175	related-only	normalize_output_path_argument request kwargs OUTPUT_PATH_ARGUMENT	src/mindroom/api/sandbox_runner.py:988-992
_process_entrypoint_with_output_path_schema	function	lines 178-181	related-only	process_entrypoint strict False schema optional	src/mindroom/tools/shell.py:361-367; src/mindroom/history/compaction.py:831-834
_copy_function_model	function	lines 184-193	related-only	Function.model_copy update deep fallback object setattr	src/mindroom/tool_system/sandbox_proxy.py:944,982; src/mindroom/history/compaction.py:831
_model_copy_with_output_path_schema	function	lines 196-204	none-found	model_copy with output path schema postprocessor	none
_install_output_path_schema_postprocessor	function	lines 207-218	none-found	MethodType process_entrypoint model_copy Function postprocessor	none
_path_has_environment_expansion	function	lines 221-222	related-only	startswith tilde dollar percent env expansion path literal	src/mindroom/tool_system/worker_routing.py:647-651
_validate_raw_output_path	function	lines 225-249	related-only	workspace-relative string path NUL absolute dotdot expansion	src/mindroom/api/knowledge.py:76; src/mindroom/knowledge/manager.py:561-562; src/mindroom/tool_system/worker_routing.py:647-652; src/mindroom/custom_tools/attachments.py:481-485
_validate_output_path	function	lines 252-280	related-only	resolve_relative_path_within_root_preserving_leaf symlink directory overwritten	src/mindroom/workspaces.py:61-93; src/mindroom/api/sandbox_runner.py:1446; src/mindroom/custom_tools/attachments.py:513-516
validate_output_path	function	lines 283-286	related-only	validate_output_path local_policy output_path	src/mindroom/custom_tools/attachments.py:513-516; src/mindroom/api/sandbox_runner.py:1446-1448
validate_output_path_syntax	function	lines 289-292	related-only	validate_output_path_syntax remote worker syntax	src/mindroom/custom_tools/attachments.py:513-514; src/mindroom/api/sandbox_runner.py:1402-1404
_validate_parent_components	function	lines 295-305	related-only	parent components symlink existing file directory workspace	src/mindroom/workspaces.py:79-90; src/mindroom/tool_system/output_files.py:268
_existing_parent_component_error	function	lines 308-313	related-only	parent is_symlink exists not dir error	src/mindroom/workspaces.py:51-57,85-92
_ensure_parent_directory	function	lines 316-338	related-only	mkdir parent components symlink resolve is_relative_to	src/mindroom/matrix/state.py:185; src/mindroom/handled_turns.py:403-420
_normalize_json_value	function	lines 341-362	duplicate-found	JSON normalize BaseModel dataclass Path Enum Mapping set tuple bytes repr	src/mindroom/llm_request_logging.py:111-131; src/mindroom/history/compaction.py:1461-1474; src/mindroom/tool_system/events.py:52-58
_has_tool_result_media	function	lines 365-366	related-only	ToolResult images videos audios files media	src/mindroom/history/compaction.py:1422-1474; src/mindroom/ai_runtime.py:298-319
_serialize_tool_output	function	lines 369-390	related-only	serialize tool output ToolResult string binary generator json fallback	src/mindroom/tool_system/events.py:52-58; src/mindroom/mcp/results.py:52; src/mindroom/custom_tools/attachments.py:60-67
_write_atomic	function	lines 393-434	duplicate-found	NamedTemporaryFile fsync replace tmp atomic write directory	src/mindroom/matrix/state.py:183-205; src/mindroom/handled_turns.py:403-423; src/mindroom/interactive.py:189-203; src/mindroom/oauth/state.py:105-106
write_bytes_to_output_path	function	lines 437-474	related-only	validate output path max bytes write bytes receipt	src/mindroom/custom_tools/attachments.py:611-615; src/mindroom/api/sandbox_runner.py:1450-1455
_redirect_result_to_file	function	lines 477-507	related-only	serialize result max bytes write atomic receipt error	src/mindroom/custom_tools/attachments.py:519-645
_signature_with_output_path	function	lines 510-524	none-found	inspect Parameter KEYWORD_ONLY VAR_KEYWORD signature replace	none
_copy_annotations_with_output_path	function	lines 527-536	none-found	get_type_hints annotations wrapper output path	none
_docstring_with_output_path	function	lines 539-542	none-found	docstring Args mindroom_output_path description	none
_wrap_entrypoint	function	lines 545-583	related-only	wrap entrypoint async sync normalize validate redirect result signature annotations	src/mindroom/tool_system/sandbox_proxy.py:930-965; src/mindroom/tool_system/sandbox_proxy.py:968-1004
_wrap_entrypoint.<locals>.async_wrapper	nested_async_function	lines 552-560	related-only	async wrapper output path validate redirect await	src/mindroom/tool_system/sandbox_proxy.py:985-1003
_wrap_entrypoint.<locals>.sync_wrapper	nested_function	lines 565-573	related-only	sync wrapper output path validate redirect entrypoint	src/mindroom/tool_system/sandbox_proxy.py:947-964
wrap_function_for_output_files	function	lines 586-604	related-only	wrap Function entrypoint collision strict schema optional	src/mindroom/tool_system/sandbox_proxy.py:930-965; src/mindroom/tool_system/sandbox_proxy.py:968-1004
wrap_toolkit_for_output_files	function	lines 607-621	none-found	wrap toolkit functions async_functions seen ids output files	none
```

Findings:

1. `mindroom_output_path` schema optionalization is duplicated in the attachments toolkit.
   `output_files.ensure_output_path_schema_optional` builds the reserved argument schema, inserts it into `Function.parameters["properties"]`, and removes it from `required` at `src/mindroom/tool_system/output_files.py:150`.
   `AttachmentsToolkit._describe_get_attachment_schema` performs the same optional-argument surgery for `get_attachment` at `src/mindroom/custom_tools/attachments.py:389`, with one extra description for `attachment_id`.
   Difference to preserve: the attachments method also enriches `attachment_id`, so only the output-path portion is duplicated.

2. `mindroom_tool_output` receipt construction is partially duplicated in attachment save responses.
   `_success_receipt` returns the canonical `saved_to_file` receipt at `src/mindroom/tool_system/output_files.py:108`.
   The worker attachment path manually constructs the same status/path/bytes/format object at `src/mindroom/custom_tools/attachments.py:596`, and the local attachment path spreads the canonical receipt and adds `sha256` at `src/mindroom/custom_tools/attachments.py:641`.
   Difference to preserve: attachment save receipts include `sha256`, and worker receipts do not currently include `overwritten`.

3. JSON-safe normalization is duplicated at different strictness levels.
   `_normalize_json_value` handles `Path`, `Enum`, `BaseModel`, dataclasses, mappings, sequences, and sets before `json.dumps` at `src/mindroom/tool_system/output_files.py:341`.
   `_json_safe` in `src/mindroom/llm_request_logging.py:111` overlaps for `BaseModel`, dataclasses, mappings, sequences/sets, bytes, and `Path`, but it falls back to `repr` instead of raising.
   `history/compaction.py` also has focused media payload normalization at `src/mindroom/history/compaction.py:1461`.
   Difference to preserve: output-file serialization rejects or text-falls-back for unsupported values, while request logging is deliberately lossy and always JSON-safe.

4. Atomic write/replace flows are repeated across persistence modules.
   `_write_atomic` writes a temporary file in the destination directory, flushes/fsyncs, and replaces the target at `src/mindroom/tool_system/output_files.py:393`.
   Similar durable replace flows exist in Matrix state persistence at `src/mindroom/matrix/state.py:183` and handled-turn tracking at `src/mindroom/handled_turns.py:403`.
   Difference to preserve: Matrix and handled-turn writes fsync the containing directory after replacement; output-file writes validates workspace containment and parent symlink safety before writing.

Proposed generalization:

1. Add a tiny public helper in `output_files.py` such as `output_file_receipt(..., sha256: str | None = None, overwritten: bool | None = None)` only if more call sites need custom receipt decoration.
2. In `AttachmentsToolkit._describe_get_attachment_schema`, reuse `ensure_output_path_schema_optional(function)` after applying the attachment ID description, or extract just the schema object if preserving existing parameter fields is clearer.
3. Leave JSON normalization separate for now unless a caller explicitly needs the same strict/fallback semantics; the overlapping code has materially different failure behavior.
4. Leave atomic write flows separate until there is a shared persistence helper that supports text/binary serializers, directory fsync policy, and workspace containment as explicit options.

Risk/tests:

Changing output-path schema handling would need `tests/test_tool_output_files.py` and attachment-tool schema tests around `get_attachment`.
Changing receipt construction would need attachment save tests for local and worker paths, especially `sha256`, `overwritten`, and `worker_path`.
Any shared JSON normalizer would risk changing log payload shape or output-file fallback behavior; tests would need non-JSON values, dataclasses, Pydantic models, sets, bytes, and enums.
Any atomic-write helper would need filesystem tests covering symlink parents, parent directory creation, temp cleanup on failure, directory fsync behavior, and replacement of existing files.
