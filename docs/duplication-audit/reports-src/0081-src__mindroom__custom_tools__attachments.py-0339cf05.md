Summary: The primary duplication candidate is `AttachmentTools._describe_get_attachment_schema`, which manually repeats the reserved `mindroom_output_path` schema optionalization already centralized in `mindroom.tool_system.output_files.ensure_output_path_schema_optional`.
The send-target, room authorization, output-path validation, attachment registration, and worker-save flows are related to shared helpers elsewhere, but the primary module mostly composes those helpers rather than reimplementing them.

```coverage-tsv
qualname	kind	lines	status	search_terms	candidates_checked
_AttachmentSendResult	class	lines 50-57	related-only	AttachmentSendResult MatrixMessageOperationResult send_result attachment_event_ids	src/mindroom/custom_tools/matrix_conversation_operations.py:49; src/mindroom/custom_tools/matrix_conversation_operations.py:279
_attachment_tool_payload	function	lines 60-67	related-only	json dumps status tool payload matrix_message _payload	src/mindroom/custom_tools/matrix_message.py:42; src/mindroom/custom_tools/matrix_conversation_operations.py:65
_get_attachment_listing	function	lines 70-95	related-only	resolve_attachments attachments_for_tool_payload list_tool_runtime_attachment_ids load_attachment	src/mindroom/attachments.py:627; src/mindroom/attachments.py:866; src/mindroom/tool_system/runtime_context.py:300
_resolve_context_attachment_path	function	lines 98-106	none-found	resolve context attachment path local_path load_attachment is_file	none
_resolve_context_attachment_record	function	lines 109-124	related-only	load_attachment storage_path attachment_id_available file missing context attachment	src/mindroom/attachments.py:580; src/mindroom/attachment_media.py:72; src/mindroom/attachments.py:642
_attachment_bytes_for_save	function	lines 127-148	related-only	read bytes size limit attachment sha256 output path save	src/mindroom/tool_system/output_files.py:437; src/mindroom/tool_system/sandbox_proxy.py:634; src/mindroom/api/sandbox_runner.py:1361
_resolve_attachment_ids	function	lines 151-173	related-only	resolve attachment ids context att_ resolve_attachments	src/mindroom/attachments.py:627; src/mindroom/attachment_media.py:72
_register_attachment_file_path	function	lines 176-197	related-only	register_local_attachment append_tool_runtime_attachment_id file_path	src/mindroom/attachments.py:394; src/mindroom/attachments.py:464; src/mindroom/tool_system/runtime_context.py:321
_resolve_attachment_file_paths	function	lines 200-219	none-found	register file paths attachment_file_paths newly_registered_attachment_ids	none
_resolve_send_attachments	function	lines 222-245	related-only	resolve_send_attachments attachment_ids attachment_file_paths matrix_conversation_operations	src/mindroom/custom_tools/matrix_conversation_operations.py:204; src/mindroom/custom_tools/matrix_conversation_operations.py:279
_send_attachment_paths	async_function	lines 248-277	related-only	send_file_message latest_thread_event_id attachment paths	src/mindroom/custom_tools/matrix_conversation_operations.py:229; src/mindroom/matrix/client_delivery.py:355
send_context_attachments	async_function	lines 280-335	related-only	send_context_attachments MatrixMessageOperations attachment sending	src/mindroom/custom_tools/matrix_conversation_operations.py:279
_resolve_send_target	function	lines 338-356	related-only	room_access_allowed resolve_context_thread_id joined room inherit context thread	src/mindroom/custom_tools/attachment_helpers.py:31; src/mindroom/custom_tools/attachment_helpers.py:62; src/mindroom/custom_tools/matrix_message.py:132
AttachmentTools	class	lines 359-669	not-a-behavior-symbol	AttachmentTools Toolkit registration methods	none
AttachmentTools.__init__	method	lines 362-382	related-only	Toolkit name attachments tools list get register schema	src/mindroom/custom_tools/matrix_message.py:36; src/mindroom/tools/attachments.py:16
AttachmentTools._describe_get_attachment_schema	method	lines 384-401	duplicate-found	mindroom_output_path schema required optional description ensure_output_path_schema_optional	src/mindroom/tool_system/output_files.py:135; src/mindroom/tool_system/output_files.py:150
AttachmentTools.list_attachments	async_method	lines 403-421	related-only	get_tool_runtime_context context error list attachments payload	src/mindroom/custom_tools/matrix_message.py:245; src/mindroom/custom_tools/matrix_message.py:56
AttachmentTools.get_attachment	async_method	lines 423-473	related-only	get attachment listing output path save payload context error	src/mindroom/custom_tools/matrix_message.py:245; src/mindroom/tool_system/output_files.py:165
AttachmentTools._resolve_output_path_argument	method	lines 475-485	related-only	normalize output path argument mindroom_output_path string	src/mindroom/tool_system/output_files.py:165; src/mindroom/tool_system/output_files.py:173
AttachmentTools._save_destination	method	lines 487-503	related-only	attachment_save_uses_worker ToolOutputFilePolicy.from_runtime worker target	src/mindroom/tool_system/sandbox_proxy.py:627; src/mindroom/api/sandbox_runner.py:1431
AttachmentTools._validate_output_path_before_save	method	lines 505-517	related-only	validate_output_path_syntax validate_output_path worker local policy	src/mindroom/tool_system/output_files.py:283; src/mindroom/tool_system/output_files.py:289; src/mindroom/api/sandbox_runner.py:1402
AttachmentTools._save_attachment_to_output_path	async_method	lines 519-645	related-only	save attachment output path worker local write bytes receipt sha256	src/mindroom/tool_system/sandbox_proxy.py:615; src/mindroom/api/sandbox_runner.py:1377; src/mindroom/tool_system/output_files.py:437
AttachmentTools.register_attachment	async_method	lines 647-669	related-only	register attachment context file path payload	src/mindroom/attachments.py:394; src/mindroom/custom_tools/attachments.py:176
```

## Findings

1. `AttachmentTools._describe_get_attachment_schema` duplicates reserved output-path schema optionalization.

- Primary: `src/mindroom/custom_tools/attachments.py:384`.
- Existing shared helper: `src/mindroom/tool_system/output_files.py:150`.
- Both functions copy Agno function parameters, add or update the `mindroom_output_path` property with `OUTPUT_PATH_ARGUMENT_DESCRIPTION`, and remove `mindroom_output_path` from the required list.
- The attachment tool has one extra behavior to preserve: it also injects the `attachment_id` description.
- This is a real but low-risk duplication because the generic helper already owns the reserved argument schema and required-list behavior.

Related but not recommended as duplication:

- `_resolve_send_target` shares room authorization and context-thread fallback concepts with `room_access_allowed` and `resolve_context_thread_id` in `src/mindroom/custom_tools/attachment_helpers.py:31` and `src/mindroom/custom_tools/attachment_helpers.py:62`.
  It also validates joined-room membership and returns a destination error tuple, so a direct extraction would not reduce much code.
- `_save_attachment_to_output_path`, `save_attachment_to_worker`, and the sandbox runner `/save-attachment` endpoint form one two-sided protocol.
  They repeat some validation/write vocabulary, but the host side, proxy side, and worker side have distinct responsibilities and already converge on `validate_output_path*` plus `write_bytes_to_output_path`.
- `_resolve_context_attachment_record` is related to `resolve_attachments` and `filter_attachments_for_context`, but it adds runtime-context availability checks and disk-presence errors suitable for model-facing tool responses.

## Proposed Generalization

Use `ensure_output_path_schema_optional(function)` inside `AttachmentTools._describe_get_attachment_schema`, then keep only the attachment-specific `attachment_id` description patch in this method.
No broader refactor is recommended.

Minimal plan:

1. Import `ensure_output_path_schema_optional` from `mindroom.tool_system.output_files`.
2. In `_describe_get_attachment_schema`, call it after confirming the `get_attachment` function exists.
3. Re-read `function.parameters`, patch only the `attachment_id` property description, and assign the parameters back.
4. Keep existing tests around attachment tool schema and output-path optionality, or add one focused regression if none exists.

## Risk/tests

Risk is low if limited to schema construction, but Agno function schema mutation is sensitive to object-copy behavior.
Tests should verify `get_attachment` exposes `mindroom_output_path` with the standard description, does not require it, and still describes `attachment_id`.
No production code was edited for this audit.
