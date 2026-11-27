# MindRoom Shell Architecture Redesign Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: implement this plan task-by-task with the skill named in **Execution**. Steps use checkbox (`- [ ]`) syntax.

**Execution:** `baspowers:subagent-driven-development` (via `run_subagent` on `gemini4_neon@xhigh`) — 3 substantial milestone commits with clear interface seams on one branch/PR (`refactor/shell-architecture`), reviewed by Opus + Astra at milestones and on the full diff.

**Goal:** Replace the 1,879-LOC `src/mindroom/shell/execution.py` god module and its split-brain ownership with focused, Tach-enforced modules centered on `ManagedProcess`, `SpoolCapture`, `run_foreground`, and `ProcessRegistry`.

**Architecture:** Three sequential milestone commits on `refactor/shell-architecture`: (1) extract `process.py` (`ManagedProcess` + launcher), `streaming.py` (`workers run`), and `policy.py` (arg/timeout helpers); (2) extract `output.py` (pure buffers + `SpoolCapture` + `BackgroundLogFile`), `runner.py` (`run_foreground`), and `registry.py` (`ProcessRegistry`), invert `background_scope.py` to break the cycle, and delete `execution.py`; (3) migrate `supervisor.py`/`script_runs`, `desktop/shell.py`, `desktop/login_environment.py`, and `api/sandbox_runner.py` to clean contracts and lock down `tach.toml`.

**Tech Stack:** Python 3.13+, `asyncio`, `pytest`, `ruff`, `ty`, `vulture`, `privata`, `tach`.

**Spec:** `docs/baspowers/specs/2026-11-27-shell-architecture-design.md`

## Global Constraints
- Behavior-preserving for all user-facing shell semantics: `nohup cmd > out.log 2>&1 &` survival on clean exit, GNU `timeout` / `setpgid` session scan and verified systemd scope `cgroup.kill` (#4454), `OOMPolicy=kill` and non-cached probe timeouts (#4450), Windows desktop import safety (`getattr(os, "O_DIRECTORY", 0)` / `O_NOFOLLOW`, Linux-gated `os.getsid`), and fast `python -m mindroom.shell.supervisor` startup (no `tool_jobs`/`background_scope` imports).
- No permanent `execution.py` compatibility shim after Milestone 2/3.
- All checks (`pytest` with default `addopts`, `ruff check`, `ruff format --check`, `ty check`, `vulture`, `privata`, `tach check --dependencies --interfaces`) must pass at every milestone commit.
- Never run `env`, `printenv`, `set`, or `env | grep`; never search `/`; set explicit `timeout` on every shell command; `unset VIRTUAL_ENV` in nix-shell commands.

## Review Focus
1. **Clean-exit `nohup` vs explicit `kill_group_after_exit=True`:** Switching `_monitor_process` from `log_file is None` guessing to explicit `BackgroundProcess.kill_group_after_exit` must preserve both: agent auto-background (`kill_group_after_exit=False`) preserves `nohup` on natural exit, while `DesktopShell` (`kill_group_after_exit=True`) and failed/timed-out commands kill the tree.
2. **Handoff & disconnect race during `run_or_background`:** Cancellation or `registry.close()` arriving during the `await asyncio.sleep(0)` handoff window must still kill the process tree and leave zero leaked records or open log files.
3. **In-process `sandbox_runner.py` concurrent turn isolation:** Replacing shared `_inprocess_shell_records` + `kill_namespace_records` with `ProcessRegistry.close_namespace(namespace)` must gracefully stop ONLY the closing turn's namespace without touching concurrent turns.
4. **Desktop spool handover & `DesktopShell` cancellation/timeout:** Replacing `DesktopShellOutput(ShellOutputCapture)` with `SpoolCapture` must preserve byte-offset `pread` paging after instant exit and after background completion, and clean up the spool file on `hand_over` or `close()`.
5. **Supervisor wire compatibility & `signal_record` delivery semantics:** Structured JSON `status`/`signal` responses between `supervisor.py` server and client must preserve exact `ProcessStatus` (`running`/`exited`/`unknown`/`error`) and `SignalResult` (`signalled`/`finished`/`unknown`/`error`) classification, and `signal_record` must still report `False` when `os.killpg` raises `ProcessLookupError` on a vanished group.

---

### Task 1 (Milestone 1): Extract `process.py` (`ManagedProcess`), `streaming.py`, and `policy.py`

**Files:**
- Modify: `src/mindroom/shell/_process_target.py` (export `is_safe_slice_name`)
- Create: `src/mindroom/shell/process.py` (`ManagedProcess`, `LaunchFailure`, idle/systemd probe & launch, Bash 126/127 classification)
- Create: `src/mindroom/shell/streaming.py` (`CommandStdin` with `attach`/`detach`, `_PipeReaders`, `stream_command`)
- Create: `src/mindroom/shell/policy.py` (`default_run_timeout_seconds`, `shell_call_timeout`, `normalize_shell_args`, `shell_args_to_command`, `shell_job_wait_seconds`, `shell_request_budget_seconds`)
- Modify: `src/mindroom/shell/execution.py` (delete `_PROCESS_SCOPES`, delegate spawn/teardown to `ManagedProcess`, remove moved streaming/policy/launcher code)
- Modify: callers of policy/streaming (`src/mindroom/api/sandbox_runner.py`, `src/mindroom/tools/shell.py`, `src/mindroom/tools/desktop.py`, `src/mindroom/custom_tools/desktop_shell.py`, `src/mindroom/tool_jobs/execution.py`, `src/mindroom/tool_system/sandbox_proxy.py`, `src/mindroom/desktop/shell.py`)
- Modify: affected tests (retarget imports/monkeypatches for moved symbols)

**Interfaces:**
- `src/mindroom/shell/process.py` produces:
  - `class ManagedProcess`: `spawn(argv, env, cwd, *, stdin, stderr) -> ManagedProcess`, `.pid: int`, `.returncode: int | None`, `.stdin`/`.stdout`/`.stderr`, `.wait_exit(timeout_seconds, on_poll)`, `.signal(sig) -> bool`, `.kill_tree()`, `.terminate_tree(*, grace_period=0.5, whole_group=False) -> bool`, `.processes_exist() -> bool`.
  - `classify_launch_failure(argv, env, cwd, exc) -> tuple[int, str] | None`.
- `src/mindroom/shell/streaming.py` produces: `CommandStdin`, `stream_command(...)`.
- `src/mindroom/shell/policy.py` produces: `default_run_timeout_seconds`, `shell_call_timeout`, `normalize_shell_args`, `shell_args_to_command`, `shell_job_wait_seconds`, `shell_request_budget_seconds`.

- [ ] **Step 1:** Export `is_safe_slice_name` in `_process_target.py` and create `process.py` with `ManagedProcess` owning `(process, scope)` directly (deleting `_PROCESS_SCOPES` WeakKeyDictionary and all manual `scope=` reconstructions).
- [ ] **Step 2:** Extract `streaming.py` (`stream_command`, `CommandStdin`, `_PipeReaders`) using `ManagedProcess` and `CommandStdin.attach()`/`detach()`, and update `api/sandbox_runner.py`.
- [ ] **Step 3:** Extract `policy.py` (arg parsing + timeout/budget math) and update `tools/shell.py`, `tools/desktop.py`, `custom_tools/desktop_shell.py`, `tool_jobs/execution.py`, `tool_system/sandbox_proxy.py`, `api/sandbox_runner.py`.
- [ ] **Step 4:** Update `execution.py` (`ProcessRecord.managed: ManagedProcess` replacing raw `process` + `_PROCESS_SCOPES`), retarget affected tests, and verify:
  - `pytest` (default `addopts`, `-n 4`) on all shell/desktop/worker/tool_jobs tests
  - `ruff check`, `ruff format --check`, `ty check`, `vulture`, `privata`, `tach check`
  - Simulated Windows import test (`mindroom.desktop.shell` without `O_DIRECTORY`/`O_NOFOLLOW`/`getsid`).
- [ ] **Step 5:** Commit Milestone 1 (`refactor(shell): extract ManagedProcess, streaming, and policy modules`).

---

### Task 2 (Milestone 2): Extract `output.py` (`SpoolCapture`), `runner.py`, and `registry.py`; Invert `background_scope.py`; Delete `execution.py`

**Files:**
- Create: `src/mindroom/shell/output.py` (`ShellOutputBuffer`, `ShellOutputProgress`, `SpoolCapture`, `SpoolStream`, `BackgroundLogPolicy`, `BackgroundLogFile`, `open_background_log`, `format_shell_completion`, `format_shell_timeout`, `format_shell_success`, `format_auto_background_message`, `format_log_trailer`, `drain_text_stream`)
- Modify: `src/mindroom/shell/output_capture.py` (`ShellOutputDestination`, `ShellOutputCapture` composing `SpoolCapture` + lazy `output_files` publish)
- Create: `src/mindroom/shell/runner.py` (`CommandSpec`, `ActiveCommand`, `ShellRunResult`, `run_foreground`)
- Create: `src/mindroom/shell/registry.py` (`ProcessRegistry`, `BackgroundProcess`, `ProcessStatus`, `SignalResult`, `MAX_BACKGROUNDED`)
- Modify: `src/mindroom/shell/background_scope.py` (`ShellBackgroundScope` owns a `ProcessRegistry` + remote endpoint tracking; removes lazy circular import!)
- Delete: `src/mindroom/shell/execution.py`
- Modify: `src/mindroom/tools/shell.py`, `src/mindroom/shell/supervisor.py`, `src/mindroom/api/sandbox_runner.py`, `src/mindroom/desktop/shell.py`, `src/mindroom/desktop/login_environment.py`, `src/mindroom/custom_tools/desktop_shell.py`, and affected tests.

**Interfaces:**
- `src/mindroom/shell/output.py` produces:
  - `ShellOutputBuffer` (pure ring buffer; NO `_log_file` or `_attach_log_file`).
  - `SpoolCapture` (bounded tempfile spool: `.stdout`, `.stderr`, `.incomplete`, `.interruption`, `.close()`, independent of workspace paths).
  - `drain_text_stream(stream, sinks)` supporting dynamic sink addition via a list/callback when a foreground run transitions to a background log file.
- `src/mindroom/shell/runner.py` produces:
  - `run_foreground(argv, env, cwd, *, tail, timeout, on_progress, output_destination, spool, output_capture, stdin, stderr, kill_group_after_exit, launch_failure_exits) -> ShellRunResult`.
  - `ActiveCommand` shared helper for `run_foreground` and `ProcessRegistry.run_or_background`.
- `src/mindroom/shell/registry.py` produces:
  - `ProcessRegistry`: `spawn_background(...)`, `run_or_background(...)`, `status(namespace, handle) -> ProcessStatus`, `signal(namespace, handle, *, force=False) -> SignalResult`, `discard(handle)`, `close_namespace(namespace)` (async graceful), `close()` (async graceful), `kill_all()` (sync shutdown).
  - `BackgroundProcess` with explicit `kill_group_after_exit: bool`, no `output_receipt`.

- [ ] **Step 1:** Build `output.py` and slim `output_capture.py` to compose `SpoolCapture`. Keep all existing output truncation/formatting/log-sweep invariants intact.
- [ ] **Step 2:** Build `runner.py` (`ActiveCommand` + `run_foreground`) and `registry.py` (`ProcessRegistry`, explicit `kill_group_after_exit`, `self.closed` gating with no `current_shell_background_scope()` reach-back, no dead `output_receipt`).
- [ ] **Step 3:** Update `background_scope.py` so `ShellBackgroundScope` owns `registry: ProcessRegistry` and `close()` awaits `self.registry.close()` directly (eliminating the runtime circular import).
- [ ] **Step 4:** Migrate `tools/shell.py`, `supervisor.py`, `api/sandbox_runner.py` (fixing in-process teardown to `await _inprocess_shell_registry.close_namespace(namespace)` instead of sync SIGKILL + removing the aliased `_inprocess_shell_records` scope hack), `desktop/login_environment.py` (using `run_foreground` directly), `desktop/shell.py`, and `custom_tools/desktop_shell.py`. Delete `src/mindroom/shell/execution.py`.
- [ ] **Step 5:** Update affected tests, add/verify tests for all 5 Review Focus items (including `kill_group_after_exit` explicit flag, handoff cancel/close race, in-process concurrent namespace isolation, desktop spool handover). Run full targeted pytest + `ruff` + `ty` + `vulture` + `privata` + `tach`.
- [ ] **Step 6:** Commit Milestone 2 (`refactor(shell): separate output, runner, and ProcessRegistry; break background_scope cycle`).

---

### Task 3 (Milestone 3): Clean Caller Contracts (`supervisor`/`script_runs` structured ops, `DesktopShell` `SpoolCapture`) & Lock Down `tach.toml`

**Files:**
- Modify: `src/mindroom/shell/supervisor.py`, `src/mindroom/script_runs/supervised.py`, `src/mindroom/script_runs/manager.py`, `src/mindroom/api/sandbox_runner_scripts.py` (use explicit `op="spawn_background"` or structured `status`/`signal` JSON replies; delete `_FINISHED_STATUS_RE` English string regex parsing and `timeout=0` spawn hack)
- Modify: `src/mindroom/desktop/shell.py` (replace `DesktopShellOutput(ShellOutputCapture)` subclass with `SpoolCapture`; drop `register_finished` and `_prune()` reconciliation)
- Modify: `tach.toml` (declare every `mindroom.shell.*` module and `mindroom.tools.shell` with explicit `depends_on` and `visibility` matching the acyclic DAG in Section 2.1 of the spec)
- Modify: tests (`tests/test_shell_supervisor.py`, `tests/test_desktop_shell.py`, `tests/test_script_run*.py`, `tests/test_tach_visibility.py`, `tests/test_import_graph.py`)

- [ ] **Step 1:** Upgrade `supervisor.py` + `script_runs/supervised.py` to use explicit background start (instead of `timeout=0`) and structured `status`/`signal` payloads from `ProcessRegistry.status()`/`signal()`, removing English-string regex parsing (`_FINISHED_STATUS_RE`). Keep supervisor startup slim (verify `python -m mindroom.shell.supervisor` does not import `mindroom.tool_jobs` or `mindroom.shell.background_scope`).
- [ ] **Step 2:** Replace `DesktopShellOutput(ShellOutputCapture)` in `desktop/shell.py` with `SpoolCapture` directly, removing fake `path=""`, `close()`-override hack, `register_finished`, and `_prune()`.
- [ ] **Step 3:** Add full `[[modules]]` entries for `mindroom.shell.*` and `mindroom.tools.shell` to `tach.toml` (enforcing exact DAG + visibility), and run `uv run --with tach tach check --dependencies --interfaces` + `pytest tests/test_tach_visibility.py tests/test_import_graph.py`.
- [ ] **Step 4:** Run full verification across all shell, supervisor, script_runs, desktop, worker, tool_jobs, and import/tach tests + `ruff check` + `ruff format --check` + `ty check` + `vulture` + `privata` + simulated Windows import check.
- [ ] **Step 5:** Commit Milestone 3 (`refactor(shell): clean supervisor/desktop contracts and enforce shell Tach boundaries`).
