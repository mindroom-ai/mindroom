# Summary

Top duplication candidates for `src/mindroom/api/sandbox_exec.py`:

- Subprocess environment construction overlaps with shell tool environment construction in `src/mindroom/tools/shell.py:35` and `src/mindroom/tools/shell.py:147`, especially passthrough keys, worker/runtime cache keys, PATH handling, and vendor telemetry injection.
- Process-group lifecycle handling is repeated between workspace env hook sourcing in `src/mindroom/api/sandbox_exec.py:393` and shell command execution in `src/mindroom/tools/shell.py:406`, including `start_new_session=True`, `os.killpg`, timeout handling, and stream draining.
- Workspace-home and worker-owned env contracts are related across `src/mindroom/api/sandbox_exec.py:249`, `src/mindroom/api/sandbox_runner.py:672`, and `src/mindroom/tools/shell.py:130`, but the behavior is intentionally split between worker process setup, request overlay protection, and shell subprocess inheritance.

```coverage-tsv
qualname	kind	lines	status	search_terms	candidates_checked
runner_execution_mode	function	lines 67-69	related-only	MINDROOM_SANDBOX_RUNNER_EXECUTION_MODE runner_execution_mode env_value	src/mindroom/api/sandbox_worker_prep.py:152, src/mindroom/workspaces.py:305
runner_uses_subprocess	function	lines 72-74	none-found	runner_uses_subprocess subprocess execution mode	none
runner_subprocess_timeout_seconds	function	lines 77-87	related-only	timeout env float default max runtime_paths.env_value	src/mindroom/orchestration/runtime.py:42, src/mindroom/constants.py:787
runner_dedicated_worker_key	function	lines 90-93	related-only	MINDROOM_SANDBOX_DEDICATED_WORKER_KEY dedicated worker key	src/mindroom/api/sandbox_worker_prep.py:152, src/mindroom/api/sandbox_worker_prep.py:187, src/mindroom/workspaces.py:305
runner_dedicated_worker_root	function	lines 96-101	related-only	MINDROOM_SANDBOX_DEDICATED_WORKER_ROOT storage_root resolve	src/mindroom/api/sandbox_worker_prep.py:157, src/mindroom/workers/backends/local.py
shared_root_from_dedicated_worker_root	function	lines 104-121	none-found	worker_dir_name storage_subpath_prefix parent reverse parts	none
runner_shared_storage_root	function	lines 124-146	related-only	MINDROOM_SANDBOX_SHARED_STORAGE_ROOT kubernetes worker storage subpath	src/mindroom/api/sandbox_worker_prep.py:286, src/mindroom/tool_system/worker_routing.py
runner_storage_root	function	lines 149-153	related-only	shared storage root fallback storage_root	src/mindroom/api/sandbox_worker_prep.py:286
runner_uses_dedicated_worker	function	lines 156-158	related-only	runner_uses_dedicated_worker record_failure dedicated worker	src/mindroom/api/sandbox_worker_prep.py:329
request_execution_env	function	lines 161-182	related-only	sandbox execution runtime env shell python tool_name	src/mindroom/constants.py:701, src/mindroom/constants.py:719, src/mindroom/constants.py:759
runtime_paths_with_execution_env	function	lines 185-209	related-only	RuntimePaths process_env env_file_values overlay MappingProxyType	src/mindroom/constants.py:342, src/mindroom/constants.py:729
project_src_path	function	lines 212-214	related-only	Path __file__ parents src root	src/mindroom/tool_system/dependencies.py:24
current_runtime_site_packages	function	lines 217-230	none-found	site.getsitepackages getusersitepackages dedupe resolved paths	none
subprocess_passthrough_env	function	lines 233-235	duplicate-found	SUBPROCESS_ENV_PASSTHROUGH_KEYS os.environ passthrough env	src/mindroom/tools/shell.py:35, src/mindroom/tools/shell.py:154, src/mindroom/constants.py:127
generic_subprocess_env	function	lines 238-246	duplicate-found	generic subprocess env HOME PATH PYTHONPATH VIRTUAL_ENV vendor telemetry	src/mindroom/tools/shell.py:147, src/mindroom/tools/shell.py:169
worker_subprocess_env	function	lines 249-267	duplicate-found	worker subprocess env XDG_CACHE_HOME PIP_CACHE_DIR UV_CACHE_DIR VIRTUAL_ENV PYTHONPATH	src/mindroom/api/sandbox_runner.py:689, src/mindroom/tools/shell.py:130
resolve_subprocess_worker_context	function	lines 270-281	related-only	python executable env cwd worker context paths.venv_dir workspace	src/mindroom/workers/backends/local.py, src/mindroom/api/sandbox_worker_prep.py:281
subprocess_env_for_request	function	lines 284-297	related-only	overlay execution_env base_env vendor telemetry	src/mindroom/api/sandbox_runner.py:778, src/mindroom/tools/shell.py:147
subprocess_worker_command	function	lines 300-306	none-found	python -m mindroom.api.sandbox_runner command	none
WorkspaceEnvHookError	class	lines 309-310	not-a-behavior-symbol	error class	none
_WorkspaceEnvHookOutputLimitError	class	lines 313-319	not-a-behavior-symbol	error class output limit	none
_WorkspaceEnvHookOutputLimitError.__init__	method	lines 316-319	not-a-behavior-symbol	error init stream_name size	none
resolve_workspace_env_hook_path	function	lines 322-363	related-only	.mindroom worker-env.sh resolve symlink size base_dir	src/mindroom/api/sandbox_runner.py:624, src/mindroom/api/sandbox_worker_prep.py:197
source_workspace_env_hook	function	lines 366-430	duplicate-found	source hook bash Popen start_new_session timeout output marker	src/mindroom/tools/shell.py:406, src/mindroom/tools/shell.py:645
_kill_workspace_env_hook_process_group	function	lines 433-435	duplicate-found	killpg getpgid SIGKILL process group	src/mindroom/tools/shell.py:431, src/mindroom/tools/shell.py:541, src/mindroom/tools/shell.py:654
_capture_workspace_env_hook_output	function	lines 438-469	duplicate-found	capture subprocess stdout stderr timeout drain output limit selectors	src/mindroom/tools/shell.py:567, src/mindroom/tools/shell.py:592, src/mindroom/tools/shell.py:624
_read_workspace_env_hook_event	function	lines 472-487	related-only	os.read nonblocking selector buffer output limit	src/mindroom/tools/shell.py:567
_resolve_bash	function	lines 490-497	related-only	shutil.which bash PATH /bin/bash	src/mindroom/tools/shell.py:173, src/mindroom/tools/shell.py:216
_parse_workspace_env_hook_output	function	lines 500-527	related-only	NUL env block marker split overlay bytes	src/mindroom/constants.py:184, src/mindroom/api/sandbox_runner.py:730
_accept_overlay_chunk	function	lines 530-548	related-only	env chunk key value filter allowed changed max bytes	src/mindroom/constants.py:184, src/mindroom/api/sandbox_runner.py:717
_is_valid_env_name	function	lines 551-554	related-only	env name alnum underscore digit validation	src/mindroom/api/skills.py:17, src/mindroom/thread_tags.py:20
```

# Findings

## 1. Subprocess env construction repeats shell subprocess env policy

`sandbox_exec.subprocess_passthrough_env`, `generic_subprocess_env`, and `worker_subprocess_env` build subprocess environments from a small allowlist plus runtime cache and Python path values in `src/mindroom/api/sandbox_exec.py:233`, `src/mindroom/api/sandbox_exec.py:238`, and `src/mindroom/api/sandbox_exec.py:249`.
The shell tool has a parallel allowlist and builder in `src/mindroom/tools/shell.py:35` and `src/mindroom/tools/shell.py:147`.
Both paths copy host/system env values, preserve interpreter/runtime variables, add worker cache env values, and inject `vendor_telemetry_env_values()`.

Differences to preserve:

- `sandbox_exec.SUBPROCESS_ENV_PASSTHROUGH_KEYS` includes `NIX_LD`, `NIX_LD_LIBRARY_PATH`, and omits shell UX values such as `SHELL`, `TERM`, and `USER`.
- The shell tool layers `runtime_env`, optional `base_process_env`, workspace-home contract values, and configured PATH prepends.
- Worker subprocess env intentionally forces `HOME` to the worker root and prepends the worker venv bin path.

## 2. Process-group subprocess lifecycle is duplicated

Workspace hook sourcing uses `subprocess.Popen(..., start_new_session=True)` in `src/mindroom/api/sandbox_exec.py:393`, kills the child process group on timeout/output overflow in `src/mindroom/api/sandbox_exec.py:433`, and manually captures stdout/stderr with deadlines in `src/mindroom/api/sandbox_exec.py:438`.
The shell tool uses `asyncio.create_subprocess_exec(..., start_new_session=True)` in `src/mindroom/tools/shell.py:406`, kills process groups in `src/mindroom/tools/shell.py:431`, `src/mindroom/tools/shell.py:541`, and `src/mindroom/tools/shell.py:645`, and has separate stream-drain and timeout helpers in `src/mindroom/tools/shell.py:567`, `src/mindroom/tools/shell.py:592`, and `src/mindroom/tools/shell.py:624`.

This is functionally related behavior: start a process in a new process group, enforce bounded foreground waiting, consume stdout/stderr without blocking forever, and terminate descendants when needed.
It is not literal duplicate code because one path is synchronous, byte-capped, and must parse a NUL-delimited env snapshot, while the shell tool is asynchronous, line-oriented, and can background long-running processes.

Differences to preserve:

- Workspace env hook must fail closed on timeout, missing marker, non-zero exit, or oversized byte output.
- Shell command execution intentionally backgrounds timed-out commands and keeps process records for polling.
- Shell stream handling is line-oriented and tolerant of oversized line reads; hook stream handling is byte-oriented and cap-enforced.

## 3. Workspace and worker-owned env contracts are related but not redundant

`worker_subprocess_env` sets worker-owned cache and venv variables in `src/mindroom/api/sandbox_exec.py:249`.
`sandbox_runner._worker_owned_env` builds the same worker-owned names from a prepared request in `src/mindroom/api/sandbox_runner.py:689`, while `_workspace_home_contract_env` builds workspace HOME/XDG values and merges worker-owned values in `src/mindroom/api/sandbox_runner.py:672`.
The shell tool recognizes an already-present workspace-home contract in `src/mindroom/tools/shell.py:130` to preserve MindRoom-owned values when launching shell subprocesses.

This is repeated knowledge of the same env contract, but each call site has a different source object and security role.
The constants in `src/mindroom/constants.py:163`, `src/mindroom/constants.py:172`, and `src/mindroom/constants.py:181` already centralize the names, which keeps the duplication mostly structural rather than semantic.

# Proposed Generalization

A small helper module could reduce the active duplication without changing behavior:

- Add `src/mindroom/subprocess_env.py` or a focused section in `src/mindroom/constants.py` for pure helpers that build worker runtime env fragments from explicit path strings and filter host env values by a caller-provided allowlist.
- Keep shell-specific PATH prepending, backgrounding, and line buffering in `tools/shell.py`.
- Keep workspace hook parsing and byte caps in `api/sandbox_exec.py`.
- Optionally add one shared `kill_process_group(pid, sig)` helper if another process-group caller appears; with only sync/async divergence today, a broader process runner abstraction is not recommended.

# Risk/tests

Behavior risk is highest around environment filtering because small allowlist differences can expose secrets, break local Nix/SSL/proxy behavior, or change PATH/PYTHONPATH precedence.
Any refactor should preserve exact key sets and precedence order with tests for `generic_subprocess_env`, `worker_subprocess_env`, `_shell_subprocess_env`, and workspace-home contract preservation.

Process lifecycle refactoring is riskier than the duplication warrants right now.
Tests would need to cover child process-group cleanup, timeout behavior, output caps, stderr error excerpts, background shell polling, and cancellation.

No production-code refactor is recommended from this audit unless the env allowlists continue to diverge.
