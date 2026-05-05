Summary: The primary active duplication is the toolkit request validation shared by `load_tools` and `unload_tools`: both build identical `unknown` and `not_allowed` responses from the same `config.toolkits`, `allowed_toolkits`, and `loaded_toolkits` inputs.
There is also a cross-tool JSON response envelope pattern repeated by several custom tools, but it is broad and low-risk to leave alone unless custom tool payloads are standardized separately.

```coverage-tsv
qualname	kind	lines	status	search_terms	candidates_checked
DynamicToolsToolkit	class	lines 21-248	related-only	DynamicToolsToolkit dynamic_tools Toolkit list_toolkits load_tools unload_tools	src/mindroom/agents.py:555; src/mindroom/tool_system/dynamic_toolkits.py:119; src/mindroom/mcp/manager.py:582; src/mindroom/tools/dynamic_tools.py:10
DynamicToolsToolkit.__init__	method	lines 24-42	related-only	Toolkit name instructions tools list_toolkits load_tools unload_tools	src/mindroom/custom_tools/matrix_room.py:40; src/mindroom/custom_tools/thread_tags.py:42; src/mindroom/custom_tools/matrix_message.py:23; src/mindroom/agents.py:570
DynamicToolsToolkit._payload	method	lines 45-48	duplicate-found	_payload status tool json.dumps sort_keys custom_tools	src/mindroom/custom_tools/matrix_api.py:144; src/mindroom/custom_tools/matrix_room.py:57; src/mindroom/custom_tools/thread_tags.py:74; src/mindroom/custom_tools/matrix_message.py:43; src/mindroom/custom_tools/subagents.py:43; src/mindroom/custom_tools/attachments.py:60
DynamicToolsToolkit._loaded_toolkits	method	lines 50-55	related-only	get_loaded_toolkits_for_session loaded_toolkits session_id	src/mindroom/tool_system/dynamic_toolkits.py:149; src/mindroom/tool_system/dynamic_toolkits.py:279; src/mindroom/agents.py:1150; tests/test_dynamic_toolkits.py:356
DynamicToolsToolkit._allowed_toolkits	method	lines 57-58	related-only	allowed_toolkits get_agent agent_config	src/mindroom/agents.py:684; src/mindroom/tool_system/dynamic_toolkits.py:126; src/mindroom/tool_system/dynamic_toolkits.py:143; src/mindroom/mcp/manager.py:551
DynamicToolsToolkit._initial_toolkits	method	lines 60-61	related-only	initial_toolkits sticky _initial_loaded_toolkits	src/mindroom/tool_system/dynamic_toolkits.py:71; src/mindroom/agents.py:697; src/mindroom/mcp/manager.py:551; tests/test_dynamic_toolkits.py:725
DynamicToolsToolkit._scope_incompatible_tools	method	lines 63-64	related-only	get_toolkit_scope_incompatible_tools scope_incompatible	src/mindroom/config/main.py:1125; src/mindroom/tool_system/dynamic_toolkits.py:104; src/mindroom/config/main.py:1138; tests/test_dynamic_toolkits.py:700
DynamicToolsToolkit._toolkit_entry	method	lines 66-74	related-only	toolkit entry description tool_names loaded sticky get_toolkit_tool_configs	src/mindroom/agents.py:688; src/mindroom/mcp/manager.py:551; src/mindroom/tool_system/dynamic_toolkits.py:224; src/mindroom/config/main.py:1430
DynamicToolsToolkit._session_error	method	lines 76-84	related-only	session_error stable session_id loaded_toolkits payload	src/mindroom/agents.py:564; src/mindroom/custom_tools/claude_agent.py:330; src/mindroom/tool_system/dynamic_toolkits.py:156
DynamicToolsToolkit.list_toolkits	method	lines 86-97	related-only	list_toolkits loaded_toolkits allowed_toolkits toolkit_entry	src/mindroom/agents.py:706; src/mindroom/mcp/manager.py:582; tests/test_dynamic_toolkits.py:608
DynamicToolsToolkit._load_tools_precheck	method	lines 99-146	duplicate-found	Unknown toolkit not_allowed scope_incompatible already_loaded session_id load_tools unload_tools	src/mindroom/custom_tools/dynamic_tools.py:199; src/mindroom/custom_tools/dynamic_tools.py:208; src/mindroom/tool_system/dynamic_toolkits.py:92; src/mindroom/config/main.py:493
DynamicToolsToolkit.load_tools	method	lines 148-189	duplicate-found	load_tools save_loaded_toolkits merge_runtime_tool_configs conflict next_request	src/mindroom/custom_tools/dynamic_tools.py:197; src/mindroom/custom_tools/dynamic_tools.py:237; src/mindroom/tool_system/dynamic_toolkits.py:186; src/mindroom/tool_system/dynamic_toolkits.py:203
DynamicToolsToolkit.unload_tools	method	lines 191-248	duplicate-found	unload_tools Unknown toolkit not_allowed sticky not_loaded save_loaded_toolkits	src/mindroom/custom_tools/dynamic_tools.py:99; src/mindroom/custom_tools/dynamic_tools.py:178; src/mindroom/tool_system/dynamic_toolkits.py:186; tests/test_dynamic_toolkits.py:725
```

Findings:

1. Duplicated dynamic-toolkit request validation responses in `load_tools` and `unload_tools`.
   `DynamicToolsToolkit._load_tools_precheck` validates unknown toolkit names and per-agent allow-list membership at [src/mindroom/custom_tools/dynamic_tools.py:101](../../../../src/mindroom/custom_tools/dynamic_tools.py:101) and [src/mindroom/custom_tools/dynamic_tools.py:110](../../../../src/mindroom/custom_tools/dynamic_tools.py:110).
   `DynamicToolsToolkit.unload_tools` repeats the same checks and response payload fields at [src/mindroom/custom_tools/dynamic_tools.py:199](../../../../src/mindroom/custom_tools/dynamic_tools.py:199) and [src/mindroom/custom_tools/dynamic_tools.py:208](../../../../src/mindroom/custom_tools/dynamic_tools.py:208).
   The behavior is functionally the same: both operations reject names outside `config.toolkits`, then reject configured toolkits absent from the agent's `allowed_toolkits`, and return `toolkit`, `loaded_toolkits`, `message`, and `allowed_toolkits`.
   Differences to preserve: `load_tools` has additional checks for scope incompatibility, already-loaded state, and missing `session_id`; `unload_tools` has sticky initial-toolkit and not-loaded checks.

2. Duplicated custom-tool JSON payload envelope.
   `DynamicToolsToolkit._payload` builds `{"status": status, "tool": "dynamic_tools", ...}` and returns `json.dumps(..., sort_keys=True)` at [src/mindroom/custom_tools/dynamic_tools.py:45](../../../../src/mindroom/custom_tools/dynamic_tools.py:45).
   The same behavior appears in custom Matrix/thread toolkits at [src/mindroom/custom_tools/matrix_api.py:144](../../../../src/mindroom/custom_tools/matrix_api.py:144), [src/mindroom/custom_tools/matrix_room.py:57](../../../../src/mindroom/custom_tools/matrix_room.py:57), [src/mindroom/custom_tools/thread_tags.py:74](../../../../src/mindroom/custom_tools/thread_tags.py:74), and [src/mindroom/custom_tools/matrix_message.py:43](../../../../src/mindroom/custom_tools/matrix_message.py:43).
   This is functionally the same response envelope and serialization rule with only the `tool` value varying.
   Differences to preserve: `attachments` uses `ensure_ascii=False` and a fixed tool name helper, while Matrix-related toolkits may add classmethod error helpers on top.

Proposed generalization:

1. Add a small private helper in `DynamicToolsToolkit`, such as `_toolkit_access_error(toolkit, loaded_toolkits, allowed_toolkits) -> str | None`, covering only the shared unknown/not-allowed branches.
2. Have `_load_tools_precheck` call that helper before its load-only checks.
3. Have `unload_tools` call the same helper before its unload-only checks.
4. Do not extract the JSON `_payload` pattern now unless a separate cleanup standardizes custom tool envelopes across several tool modules.

Risk/tests: Low behavioral risk if the validation helper returns byte-for-byte equivalent JSON payloads.
The focused tests are in `tests/test_dynamic_toolkits.py`, especially manager load/unload status cases around lines 608-745 and the scope-incompatible case around line 700.
No production code was edited for this audit.
