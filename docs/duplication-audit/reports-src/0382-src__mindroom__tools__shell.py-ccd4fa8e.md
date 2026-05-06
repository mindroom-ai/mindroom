Summary: The strongest duplication candidates are subprocess environment assembly shared with `src/mindroom/api/sandbox_exec.py` / `src/mindroom/api/sandbox_runner.py`, and process-group timeout cleanup shared with workspace env hook subprocess handling.
No literal duplicate shell command registry implementation was found elsewhere in `./src`.

```coverage-tsv
qualname	kind	lines	status	search_terms	candidates_checked
_normalize_shell_args	function	lines 80-94	none-found	BeforeValidator json.loads flat list[str] shell args	none
_shell_path_prepend_entries	function	lines 97-101	related-only	shell_path_prepend PATH prepend split regex	src/mindroom/constants.py:659; src/mindroom/api/sandbox_exec.py:259
_shell_subprocess_path	function	lines 104-127	duplicate-found	PATH prepend dedupe join os.pathsep worker_subprocess_env	src/mindroom/api/sandbox_exec.py:259; src/mindroom/constants.py:689
_workspace_home_contract_env_from_process_env	function	lines 130-144	duplicate-found	workspace home contract MINDROOM_AGENT_WORKSPACE XDG_CONFIG_HOME	src/mindroom/api/sandbox_runner.py:672; src/mindroom/api/sandbox_runner.py:702
_shell_subprocess_env	function	lines 147-170	duplicate-found	subprocess env passthrough shell execution runtime vendor telemetry	src/mindroom/api/sandbox_exec.py:233; src/mindroom/api/sandbox_exec.py:238; src/mindroom/api/sandbox_runner.py:902; src/mindroom/constants.py:689
_login_bash_command_index	function	lines 173-198	none-found	bash -lc login command index Path(args[0]).name	none
_restore_env_exports_for_login_shell	function	lines 201-206	none-found	export PATH shlex.quote login shell restore	none
_shell_subprocess_args	function	lines 209-223	none-found	login bash env restore adjusted args	none
_handle_namespace	function	lines 226-230	related-only	namespace storage_root base_dir resolve	src/mindroom/runtime_support.py:87; src/mindroom/tool_system/worker_routing.py:563; src/mindroom/matrix/mentions.py:312
_ProcessRecord	class	lines 234-249	none-found	backgrounded process record pid stdout stderr finished return_code	none
shell_tools	function	lines 332-561	related-only	registered Toolkit shell command tools run check kill	src/mindroom/tools/python.py:27; src/mindroom/tools/daytona.py:182; src/mindroom/tools/e2b.py:53
shell_tools.<locals>.__init__	nested_function	lines 338-373	related-only	Toolkit init tools runtime_paths execution env managed init arg	src/mindroom/tools/google_drive.py:65; src/mindroom/tools/google_sheets.py:73; src/mindroom/tools/config_manager.py:29
shell_tools.<locals>.run_shell_command	nested_async_function	lines 375-483	duplicate-found	create subprocess timeout background handle stdout stderr process group	src/mindroom/api/sandbox_exec.py:393; src/mindroom/api/sandbox_runner.py:1164; src/mindroom/cli/local_stack.py:307
shell_tools.<locals>.check_shell_command	nested_function	lines 485-518	none-found	check shell handle FINISHED RUNNING partial output	none
shell_tools.<locals>.kill_shell_command	nested_function	lines 520-548	related-only	kill process group SIGTERM SIGKILL handle	src/mindroom/api/sandbox_exec.py:433; src/mindroom/tools/e2b.py:53
shell_tools.<locals>._sweep_stale_records	nested_function	lines 550-559	none-found	stale finished records monotonic cleanup	none
_read_stream	async_function	lines 567-579	related-only	async stream readline decode buffer oversized line	src/mindroom/api/sandbox_exec.py:438; src/mindroom/api/sandbox_exec.py:472
_cancel_pending_tasks	async_function	lines 582-589	related-only	cancel pending asyncio tasks suppress CancelledError	src/mindroom/api/main.py:378; src/mindroom/custom_tools/browser.py:1050
_await_foreground_process_exit	async_function	lines 592-621	duplicate-found	process wait timeout polling without pipe EOF	src/mindroom/api/sandbox_exec.py:438; src/mindroom/api/sandbox_exec.py:465
_await_reader_tasks_with_grace	async_function	lines 624-642	related-only	asyncio wait reader tasks grace cancel pending	src/mindroom/tools/shell.py:671; src/mindroom/api/sandbox_exec.py:438
_terminate_process_group	async_function	lines 645-668	duplicate-found	process group terminate escalate SIGKILL wait timeout	src/mindroom/api/sandbox_exec.py:403; src/mindroom/api/sandbox_exec.py:433
_monitor_process	async_function	lines 671-688	none-found	background process monitor update record buffers returncode	none
```

Findings:

1. Shell subprocess env construction repeats sandbox subprocess env construction.
`src/mindroom/tools/shell.py:147` builds a filtered subprocess env from host/process env, overlays runtime env, adds workspace-home contract values, adjusts PATH, and adds vendor telemetry.
`src/mindroom/api/sandbox_exec.py:233`, `src/mindroom/api/sandbox_exec.py:238`, and `src/mindroom/api/sandbox_exec.py:249` build closely related filtered subprocess envs, add worker-owned HOME/cache/venv values, prepend the worker venv to PATH, and add vendor telemetry.
`src/mindroom/api/sandbox_runner.py:902` prepares shell execution env for prepared workers with `worker_subprocess_env()` plus `constants.shell_extra_env_values()`.
The behavior is not identical: shell.py supports user-configured PATH prefixes and login-shell PATH restoration, while sandbox worker env construction owns worker cache/venv variables and PYTHONPATH.
Still, the shared behavior is a subprocess environment merge pipeline with a filtered base, runtime/workspace overlays, PATH composition, and telemetry.

2. Workspace-home contract construction is duplicated in forward and reverse forms.
`src/mindroom/api/sandbox_runner.py:672` constructs the canonical HOME, `MINDROOM_AGENT_WORKSPACE`, and XDG identity values for a workspace.
`src/mindroom/tools/shell.py:130` recognizes the same contract in an existing process env and forwards only the allowed contract keys when the identity is complete.
Both encode the same invariant: HOME must equal `MINDROOM_AGENT_WORKSPACE`, and XDG config/data/state paths must be under that workspace.
The difference to preserve is direction: sandbox_runner creates authoritative values from a `Path`, while shell.py validates and extracts from an existing env snapshot.

3. Process-group timeout and cleanup behavior is repeated.
`src/mindroom/tools/shell.py:645` sends SIGTERM to a subprocess group, waits, escalates to SIGKILL, and waits again.
`src/mindroom/api/sandbox_exec.py:393` starts workspace env hook subprocesses with `start_new_session=True`; its timeout and output-limit paths call `_kill_workspace_env_hook_process_group()` at `src/mindroom/api/sandbox_exec.py:433`, then wait with suppressed timeout errors at `src/mindroom/api/sandbox_exec.py:405` and `src/mindroom/api/sandbox_exec.py:411`.
The shell version is async and TERM-first; the workspace hook version is sync and SIGKILL-only because hook execution is already in an error path.
The common behavior is process-group ownership and bounded cleanup after subprocess timeout.

4. Foreground subprocess waiting without depending on pipe EOF has a related duplicate in workspace env hook output capture.
`src/mindroom/tools/shell.py:592` polls `process.wait()` with a deadline so a command can be backgrounded while stream reader tasks keep draining.
`src/mindroom/api/sandbox_exec.py:438` uses nonblocking selectors and an explicit monotonic deadline before `process.wait(timeout=remaining)`.
The implementations differ because one is asyncio and one is synchronous selector-based IO, but both solve deadline-bound subprocess completion independent of ordinary `communicate()`.

Proposed generalization:

1. Move workspace-home contract creation and validation to a focused helper in `src/mindroom/constants.py` or a small runtime env module, returning constructed values and validating/extracting an existing env snapshot.
2. Add a small PATH composition helper that takes current PATH plus prepended entries and preserves insertion order while deduping.
3. Consider a narrow subprocess process-group cleanup helper only if another sync/async subprocess path is added; today the async/sync split makes immediate consolidation less valuable.
4. Do not generalize the shell handle registry, check, kill, or monitor code; no active duplicate implementation was found.

Risk/tests:

Any env helper extraction would need tests covering PATH prepend order/deduplication, missing PATH, workspace-home contract acceptance/rejection, worker-prepared env preservation, and vendor telemetry retention.
Process cleanup refactoring would need subprocess tests for timeout, cancellation, SIGTERM-to-SIGKILL escalation, process-group targeting, and reader task draining.
No production code was edited for this audit.
