# MindRoom Shell Module Architecture Redesign

**Date:** 2026-11-27
**Status:** Approved for implementation (single branch/PR `refactor/shell-architecture`, 3 milestone commits)
**Scope:** `src/mindroom/shell/`, its callers (`tools/shell.py`, `desktop/shell.py`, `desktop/login_environment.py`, `api/sandbox_runner.py`, `api/sandbox_runner_scripts.py`, `script_runs/`, `tool_jobs/`, `tool_system/sandbox_proxy.py`, `custom_tools/desktop_shell.py`), and `tach.toml`.

## 1. Problem Statement

`src/mindroom/shell/` is 3,486 LOC, with `execution.py` alone at 1,879 LOC. The issue is not line count alone — five ownership and split-brain defects make every shell change fragile:

1. **Process & isolation scope are detached:** `_start_command` returns a bare `asyncio.subprocess.Process` and stashes `ProcessScope` into a global `_PROCESS_SCOPES` `WeakKeyDictionary`. Seven teardown sites manually reconstruct `(pid, scope)`.
2. **Five callers collide in a 20-parameter `run_command` + naked `dict[str, ProcessRecord]`:**
   - `script_runs/supervised.py` encodes "start in background" as `timeout=0` and `supervisor.py` regex-parses English status/kill strings back into structured states (`_FINISHED_STATUS_RE`).
   - `desktop/login_environment.py` allocates a throwaway `registry={}` and relies on `finally: kill_all_records(registry)` for a foreground probe.
   - `desktop/shell.py` maintains a shadow `_handles` dict beside `_records`, passes `register_finished=True`, and reconciles silent 10-minute evictions via `_prune()`.
   - `api/sandbox_runner.py` aliases one shared `_inprocess_shell_records` dict into per-request `ShellBackgroundScope` and tears it down via sync immediate `SIGKILL` (`kill_namespace_records`), diverging from async graceful `SIGTERM` (`close_background_records`).
3. **Completion & descendant policy are guessed by convention:** `_monitor_process` infers whether to kill descendants on natural exit from `log_file is None`; `_background_rejection` detects closed owners via `active_scope.records is registry` (ContextVar reach-back); three functions mutate `ProcessRecord` terminal state; `ProcessRecord.output_receipt` has zero production readers.
4. **Output sinks split control and violate Liskov:** `ShellOutputBuffer` secretly tees to an attached `_log_file` while `_read_stream` feeds `capture` explicitly; `DesktopShellOutput(ShellOutputCapture)` passes fake `path=""` and overrides `close()` to keep completed spools alive.
5. **Circular dependency & zero Tach boundaries:** `execution.py <-> background_scope.py` has a runtime circular import; policy callers (`tool_jobs`, `sandbox_proxy`, `tools/desktop`) import the 1,879-LOC engine for ~100 LOC of arg/timeout helpers; `mindroom.shell.*` has no `tach.toml` declarations.

## 2. Target Architecture & Ownership Invariants

### 2.1 Acyclic Module DAG

```
_process_target (Linux /proc session & cgroup v2 verification; stdlib only)
parent_death_exec (standalone Linux PR_SET_PDEATHSIG wrapper; stdlib only)
       │
       ▼
process (ManagedProcess, idle/systemd launcher probe & argv wrap, Bash 126/127 classify)
       │
       ├──────────────────────────┐
       ▼                          ▼
streaming (workers run:      runner (CommandSpec, _RunningCommand,
 CommandStdin, _PipeReaders,  run_foreground, ShellRunResult)
 stream_command)                  ▲
                                  │
output (ShellOutputBuffer,        │
 ShellOutputProgress,             ├─── output_capture (ShellOutputDestination,
 SpoolCapture, BackgroundLogFile, │     ShellOutputCapture composing SpoolCapture
 formatters, drain_text_stream) ──┘     + lazy tool_system.output_files publish)
                                  │
                                  ▼
                             registry (ProcessRegistry, BackgroundProcess,
                              ProcessStatus, SignalResult, spawn_background,
                              run_or_background, status, signal, close_namespace, close)
                                  │
                    ┌─────────────┴─────────────┐
                    ▼                           ▼
            background_scope               supervisor (worker Unix-socket IPC
       (ShellBackgroundScope owning         + _ShellSupervisorManager; structured
        ProcessRegistry + remote leases)    status/signal JSON; no background_scope
                    │                       or tool_jobs imports)
                    ▼
                 policy (normalize_shell_args, shell_args_to_command,
                  shell_call_timeout, shell_job_wait_seconds,
                  shell_request_budget_seconds)
```

### 2.2 Core Invariants

1. **Single OS process owner (`ManagedProcess` in `process.py`):**
   - Owns `(process: asyncio.subprocess.Process, scope: ProcessScope | None)` from spawn to death.
   - Exposes `ManagedProcess.spawn(...)`, `.pid`, `.returncode`, `.stdin`/`.stdout`/`.stderr`, `.wait(timeout, on_poll)`, `.signal(sig)`, `.kill_tree()`, `.terminate_tree(grace_period, whole_group)`, `.processes_exist()`.
   - Eliminates `_PROCESS_SCOPES` WeakKeyDictionary and every caller-threaded `scope=` parameter.
2. **Three explicit execution modes, one owner before and after handoff:**
   - `run_foreground` (`runner.py`): registry-free bounded foreground run; **zero** background records or live processes survive return or cancellation (except intentionally detached `nohup` children when `kill_group_after_exit=False` and the command exited naturally).
   - `ProcessRegistry` (`registry.py`): owns reservations, active/finished `BackgroundProcess` entries, limits (`MAX_BACKGROUNDED`, `_MAX_TOTAL_BACKGROUNDED`), stale sweeping, and `closed: bool`.
     - Explicit `spawn_background(...)` for immediate tracked runs (`script_runs`, `DesktopShell` handle creation) — eliminates `timeout=0`.
     - Explicit `run_or_background(...)` for foreground-wait-then-adopt (`tools/shell.py` auto-background, `DesktopShell` inline-wait-then-handle, `supervisor` background mode). Until adoption succeeds, the caller owns teardown; after adoption, the registry owns it. `self.closed` rejects late adoption without touching `current_shell_background_scope()`.
     - Explicit `kill_group_after_exit: bool` stored on `BackgroundProcess` — never inferred from `log_file is None`.
     - Structured `status(namespace, handle) -> ProcessStatus` and `signal(namespace, handle, force=...) -> SignalResult` — eliminates `check_command`/`kill_command` English-string regex round-trips in `supervisor.py`.
     - Unified `async close_namespace(namespace)` and `async close()` — graceful SIGTERM (0.5s grace) → SIGKILL → drain monitors → flush log trailers everywhere (including `sandbox_runner.py` in-process mode).
     - Dead `output_receipt` removed.
   - `stream_command` (`streaming.py`): raw byte streaming for `workers run`, sharing only `ManagedProcess`.
3. **Pure output primitives (`output.py` + `output_capture.py`):**
   - `ShellOutputBuffer` is a pure rolling tail buffer (no `_log_file` field or `_attach_log_file()`).
   - `drain_text_stream(stream, sinks)` fans out decoded chunks explicitly to registered sinks (buffer, spool, live log).
   - `SpoolCapture` (`output.py`) owns caller-managed bounded tempfile spooling (`append`, `read`, `pread`, `tail`, `close`); `ShellOutputCapture` (`output_capture.py`) composes `SpoolCapture` + `ShellOutputDestination` + lazy `output_files` publication.
   - `DesktopShell` uses `SpoolCapture` directly (removes `DesktopShellOutput` Liskov subclass, fake `path=""`, `register_finished`, and `_prune()`).
4. **Strict acyclic Tach boundaries (`tach.toml`):**
   - Every `mindroom.shell.*` module and `mindroom.tools.shell` declared in `tach.toml` with explicit `depends_on` and `visibility`.
   - Zero runtime circular imports (`background_scope.py` imports `registry.py`; nothing below `background_scope` imports `background_scope` or `tool_jobs`).
   - `supervisor.py` never imports `background_scope` or `tool_jobs` (preserves fast supervisor startup).

### 2.3 Non-Goals (Do Not Rewrite)
- `_process_target.py` Linux `/proc` & cgroup v2 verification logic (keep intact; only export `is_safe_slice_name` to eliminate duplication).
- Linux `chrt`/`ionice`/`systemd-run` probe/cache logic and `#4450` (`OOMPolicy=kill`, non-cached probe timeouts).
- `parent_death_exec.py` (52 LOC, standalone).
- Supervisor Unix-socket transport architecture and `_ShellSupervisorManager` lifecycle.
- `tools/shell.py` env construction and login-bash handling.
- `DesktopShell` approval/lease/input-settling state machine (only drop `DesktopShellOutput`, `register_finished`, and `_prune()`).
- `execution.py` shim: remove `execution.py` completely once all callers and tests migrate to the new focused modules (no permanent compatibility shim).
