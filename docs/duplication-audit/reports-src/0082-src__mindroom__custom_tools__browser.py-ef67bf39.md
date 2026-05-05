Summary: No meaningful duplication found for the browser profile/tab lifecycle, Playwright action routing, snapshot generation, dialog handling, or artifact capture flows in `src/mindroom/custom_tools/browser.py`.
The closest candidates are small repeated input-normalization and slug/path-building idioms, but they are generic local validation patterns rather than duplicated browser behavior.
`src/mindroom/tools/browser.py` is a metadata wrapper for this module, not a second implementation.

```coverage-tsv
qualname	kind	lines	status	search_terms	candidates_checked
_BrowserTabState	class	lines 162-169	none-found	BrowserTabState tab refs pending_dialog console dataclass	src/mindroom/custom_tools/browser.py:162; src/mindroom/tools/browser.py:46
_BrowserProfileState	class	lines 173-179	none-found	BrowserProfileState Playwright context tabs active_target_id	src/mindroom/custom_tools/browser.py:173; src/mindroom/tools/browser.py:46
_clean_str	function	lines 182-187	related-only	non-empty string strip normalization _validate_non_empty_string normalize output path	src/mindroom/custom_tools/matrix_api.py:239; src/mindroom/tool_system/output_files.py:165; src/mindroom/custom_tools/subagents.py:67
profile_dir	function	lines 190-197	related-only	slug sanitize profile dir browser-profiles chmod 0700 worker dir normalization	src/mindroom/tool_system/worker_routing.py:162; src/mindroom/tool_system/worker_routing.py:172; src/mindroom/custom_tools/browser.py:190
persistent_launch_kwargs	function	lines 200-224	none-found	launch_persistent_context user_data_dir BROWSER_EXECUTABLE_PATH chromium service_workers viewport	src/mindroom/custom_tools/browser.py:200; src/mindroom/custom_tools/browser.py:999; src/mindroom/tools/agentql.py:37
clear_stale_singleton_locks	function	lines 227-248	none-found	SingletonLock SingletonCookie SingletonSocket stale Chromium symlink os.kill	src/mindroom/custom_tools/browser.py:227
BrowserTools	class	lines 251-1102	related-only	browser tool browserbase web browser website playwright action routing	src/mindroom/tools/browser.py:20; src/mindroom/tools/browserbase.py:13; src/mindroom/tools/web_browser_tools.py:13; src/mindroom/tools/website.py:13
BrowserTools.__init__	method	lines 254-262	related-only	Toolkit init output_dir mkdir runtime_paths browser tools	src/mindroom/custom_tools/browser.py:254; src/mindroom/tools/browser.py:36; src/mindroom/tools/website.py:22
BrowserTools._close_profiles	async_method	lines 264-267	none-found	close profiles stop profile browser state cleanup	src/mindroom/custom_tools/browser.py:264; src/mindroom/api/sandbox_runner.py:492
BrowserTools.close	method	lines 269-276	related-only	Toolkit close running loop create_task asyncio.run close resources	src/mindroom/custom_tools/browser.py:269; src/mindroom/api/sandbox_runner.py:492; src/mindroom/api/openai_compat.py:1841
BrowserTools.browser	async_method	lines 278-474	related-only	action routing json dumps sort_keys validate action custom tool	src/mindroom/custom_tools/browser.py:278; src/mindroom/custom_tools/matrix_api.py:1437; src/mindroom/custom_tools/subagents.py:43
BrowserTools._validate_target	method	lines 477-487	none-found	host sandbox node target validation browser tool	src/mindroom/custom_tools/browser.py:477
BrowserTools._status_payload	async_method	lines 489-494	none-found	browser status payload running false tabs profile	src/mindroom/custom_tools/browser.py:489
BrowserTools._profiles_payload	async_method	lines 496-506	none-found	browser profiles payload running_profiles selected_profile chrome default	src/mindroom/custom_tools/browser.py:496
BrowserTools._profile_status	async_method	lines 508-518	none-found	profile status activeTargetId tabCount tabs running	src/mindroom/custom_tools/browser.py:508
BrowserTools._tab_list	async_method	lines 520-538	none-found	tab list page title url active stale remove closed	src/mindroom/custom_tools/browser.py:520
BrowserTools._tabs_payload	async_method	lines 540-548	none-found	tabs payload activeTargetId profile status tabs	src/mindroom/custom_tools/browser.py:540
BrowserTools._open_tab	async_method	lines 550-563	none-found	new_page register tab goto domcontentloaded title url targetId	src/mindroom/custom_tools/browser.py:550
BrowserTools._focus_tab	async_method	lines 565-579	none-found	focus tab active_target_id tab not found title url	src/mindroom/custom_tools/browser.py:565
BrowserTools._close_tab	async_method	lines 581-591	none-found	close tab resolve_tab remove_tab targetId	src/mindroom/custom_tools/browser.py:581
BrowserTools._navigate	async_method	lines 593-605	none-found	navigate goto domcontentloaded active target title url	src/mindroom/custom_tools/browser.py:593; src/mindroom/tools/browserbase.py:97
BrowserTools._console	async_method	lines 607-623	none-found	console entries level filter max console entries page on console	src/mindroom/custom_tools/browser.py:607; src/mindroom/custom_tools/browser.py:1049
BrowserTools._pdf	async_method	lines 625-636	none-found	page pdf output path browser artifact	src/mindroom/custom_tools/browser.py:625
BrowserTools._upload	async_method	lines 638-665	none-found	set_input_files inputRef upload paths selector	src/mindroom/custom_tools/browser.py:638
BrowserTools._dialog	async_method	lines 667-692	none-found	pending_dialog accept promptText timeout dialog armed	src/mindroom/custom_tools/browser.py:667
BrowserTools._screenshot	async_method	lines 694-722	related-only	screenshot output path png jpeg full page selector browserbase screenshot	src/mindroom/custom_tools/browser.py:694; src/mindroom/tools/browserbase.py:52; src/mindroom/tools/website.py:13
BrowserTools._snapshot	async_method	lines 724-827	none-found	browser snapshot aria ai refs evaluate selectors interactive	src/mindroom/custom_tools/browser.py:57; src/mindroom/custom_tools/browser.py:724
BrowserTools._resolve_max_chars	method	lines 830-835	related-only	max chars efficient mode truncate snapshot content length	src/mindroom/custom_tools/browser.py:830; src/mindroom/tools/browserbase.py:87; src/mindroom/history/compaction.py:1192
BrowserTools._act	async_method	lines 837-979	none-found	click type press hover drag select fill resize wait evaluate browser act	src/mindroom/custom_tools/browser.py:837
BrowserTools._act_result	method	lines 982-991	related-only	ok payload action kind profile targetId json payload	src/mindroom/custom_tools/browser.py:982; src/mindroom/custom_tools/subagents.py:43; src/mindroom/custom_tools/matrix_conversation_operations.py:66
BrowserTools._ensure_profile	async_method	lines 993-1015	none-found	ensure profile async_playwright launch persistent context register pages new_page	src/mindroom/custom_tools/browser.py:993
BrowserTools._stop_profile	async_method	lines 1017-1023	none-found	stop profile context close playwright stop lock	src/mindroom/custom_tools/browser.py:1017
BrowserTools._resolve_tab	async_method	lines 1025-1043	none-found	resolve tab active target closed new page fallback	src/mindroom/custom_tools/browser.py:1025
BrowserTools._register_tab	method	lines 1045-1052	none-found	register tab uuid console dialog close event handlers	src/mindroom/custom_tools/browser.py:1045
BrowserTools._record_console	method	lines 1055-1063	none-found	console message level location text ring buffer	src/mindroom/custom_tools/browser.py:1055
BrowserTools._handle_dialog	async_method	lines 1065-1074	none-found	dialog accept dismiss pending behavior promptText	src/mindroom/custom_tools/browser.py:1065
BrowserTools._resolve_selector	method	lines 1077-1080	none-found	resolve selector ref refs map selector	src/mindroom/custom_tools/browser.py:1077
BrowserTools._next_output_path	method	lines 1082-1083	related-only	uuid output path extension artifact path	src/mindroom/custom_tools/browser.py:1082; src/mindroom/attachments.py:418; src/mindroom/tools/shell.py:438
BrowserTools._resolve_output_dir	method	lines 1085-1096	related-only	get_tool_runtime_context storage_path output dir mkdir browser artifacts	src/mindroom/custom_tools/browser.py:1085; src/mindroom/custom_tools/subagents.py:60; src/mindroom/custom_tools/scheduler.py:48; src/mindroom/tool_system/output_files.py:225
BrowserTools._remove_tab	method	lines 1099-1102	none-found	remove tab active target next tab state	src/mindroom/custom_tools/browser.py:1099
```

Findings:

No real duplication requiring refactor was found.

Related-only candidates:

1. String normalization appears in several tool modules.
`_clean_str` in `src/mindroom/custom_tools/browser.py:182` returns a stripped non-empty string or `None`.
Similar local validation appears in `src/mindroom/custom_tools/matrix_api.py:239` and `src/mindroom/tool_system/output_files.py:165`, but those callers need field-specific errors or preserve non-string objects, so a shared helper would either be too narrow or weaken the typed interfaces.

2. Safe-ish filesystem name normalization appears in multiple places.
`profile_dir` in `src/mindroom/custom_tools/browser.py:190` slugifies a browser profile name and creates a private persistent directory.
`src/mindroom/tool_system/worker_routing.py:162` and `src/mindroom/tool_system/worker_routing.py:172` use similar regex-based slug generation for worker keys, but their allowed characters and fallbacks differ by runtime identity semantics.

3. Tool payload helpers are stylistically related, not duplicated behavior.
`BrowserTools.browser` and `_act_result` return sorted JSON/status payloads, and `src/mindroom/custom_tools/subagents.py:43` has a small `_payload` helper.
The browser module returns dictionaries from internal methods and serializes only at the public `browser` entry point, while subagents serializes every payload at helper level.
This is not enough shared behavior to justify a cross-tool abstraction.

4. Runtime-context artifact paths are related but domain-specific.
`BrowserTools._resolve_output_dir` in `src/mindroom/custom_tools/browser.py:1085` chooses a browser artifact directory from the tool runtime context or runtime storage root.
`src/mindroom/custom_tools/subagents.py:60` and `src/mindroom/tool_system/output_files.py:225` also use runtime context or output-path validation, but they write registries or user-requested workspace files rather than browser-generated artifacts.

Proposed generalization: No refactor recommended.
The module owns a single cohesive browser runtime implementation, and the repeated patterns found elsewhere are generic validation/path idioms with domain-specific differences.

Risk/tests: No production changes were made.
If a future refactor extracts helpers anyway, tests should cover browser profile slugging and permissions, `BROWSER_EXECUTABLE_PATH` resolution, stale Chromium lock cleanup, tab registration/removal, all public browser actions, snapshot refs, screenshot/PDF artifact paths, upload selector resolution, dialog accept/dismiss behavior, and the tool-runtime-context fallback path.
