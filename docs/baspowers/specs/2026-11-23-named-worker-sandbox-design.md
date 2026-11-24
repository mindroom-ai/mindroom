# Named Worker Sandboxing Design

## Intent & Constraints

Let an agent run shell commands in a sandboxed Kubernetes named worker that sees either **no workspace** (ephemeral scratch only) or **only one workspace-relative directory** (read-write or read-only), without inheriting the normal worker's broader filesystem, credentials, Agent Keys, environment secrets, workspace env hook, or `mindroom-agent` CLI grant.

**Constraints:**
- Kubernetes named workers only (`named_workers >= 1`, `worker_scope: user_agent` or `private.per: user_agent`, idle cleanup running).
- Narrowing the mount (`mount_dir`) always implies full sandbox mode — never allow a narrowed directory mount to silently retain credentials, Agent Keys, or `MINDROOM_AGENT_CLI_TOKEN`.
- Sandbox options are locked on the first admitted call for a worker name (same rule as `resource_profile`); follow-up calls that omit sandbox options reuse the worker's locked sandbox settings and still strip all credentials/env/CLI grants.
- Stamp `mindroom.ai/worker-sandbox: "true"` on sandboxed worker pods and Deployments so Kubernetes `NetworkPolicy` can optionally deny outbound egress (no Helm NetworkPolicy change in v1).
- Never let kubelet auto-create a missing `subPath` directory as root; require `mount_dir` to already exist as a real directory (not a symlink) via `open_directory_within_root(..., create=False)`.

## API Surface

### `run_shell_command` (`src/mindroom/tools/shell.py`)
Add three optional parameters beside `worker` and `resource_profile` (hidden from schema whenever named workers are unavailable, and covered by `_invalid_arguments_reason` in `src/mindroom/tool_system/agent_tool_calls.py`):
- `sandbox: bool | None = None`: run the named worker in isolated sandbox mode (ephemeral scratch directory only unless `mount_dir` is set).
- `mount_dir: str | None = None`: workspace-relative subdirectory to mount inside the sandboxed named worker (e.g. `"scratch/job1"`; workspace root `"."` and dot-prefixed segments like `.mindroom`, `.config`, or `.git` are refused). Always enables sandbox mode. Note that a writable `mount_dir` still allows repo-local files like `.git/hooks` if the normal worker later runs `git` there.
- `mount_read_only: bool | None = None`: mount `mount_dir` read-only. Requires `mount_dir`.

### `mindroom-agent workers run` (`src/mindroom/agent_cli/main.py`)
Add matching CLI flags to `workers run`:
- `--sandbox`: pass `sandbox=True`.
- `--mount-dir DIR`: pass `mount_dir=DIR`.
- `--mount-read-only`: pass `mount_read_only=True`.

Unset flags are omitted from the submitted tool arguments so follow-up calls (`mindroom-agent workers run NAME -- CMD`) inherit the worker's locked sandbox settings.

Update minimal-mode prompt guidance (`src/mindroom/agent_cli/context.py`) briefly if helpful, and document the flags in `docs/tools/execution-and-coding.md`, `docs/tools/agent-cli.md`, and `docs/dev/2026-11-19-named-workers.md`.

## Validation & First-Call Admission (`src/mindroom/workers/named.py`, `src/mindroom/workers/models.py`)

Define in `src/mindroom/workers/models.py`:
- `LABEL_WORKER_SANDBOX = "mindroom.ai/worker-sandbox"`
- `WorkerSandboxConfig(frozen=True, slots=True)`:
  - `mount_dir: str | None = None` (normalized POSIX relative subdirectory path, e.g. `"scratch/job1"`, or `None` for empty scratch)
  - `mount_read_only: bool = False`
- Add `sandbox: WorkerSandboxConfig | None = None` to `WorkerSpec`.

Validation in `named_worker_call(...)`:
- Type-check `sandbox` (`bool | None`), `mount_dir` (`str | None`), `mount_read_only` (`bool | None`).
- When `worker is None`:
  - Treat `sandbox in (None, False)` and `mount_read_only in (None, False)` with `mount_dir is None` as no named-worker request (so models filling default `false` on optional booleans still run normally).
  - If `sandbox is True`, `mount_dir is not None`, or `mount_read_only is True`, raise `NamedWorkerError` requiring `worker`.
- Cross-field rules:
  - `sandbox is False and mount_dir is not None` -> raise `NamedWorkerError("mount_dir always runs in sandbox mode; omit sandbox or set sandbox=true.")`.
  - `mount_read_only is True and mount_dir is None` -> raise `NamedWorkerError("mount_read_only requires mount_dir.")`.
- `mount_dir` normalization:
  - Reject NUL bytes; validate with `agent_workspace_relative_path(mount_dir)` (rejects empty/blank, `$`, absolute, `..`); reject empty `relative.parts` (`"."` workspace root) and any dot-prefixed segment (`part.startswith(".")`); normalize with `relative.as_posix()`.
  - Fail fast when `mount_dir` is set and `workers.kubernetes.runtimeClassName` is configured (runtimes such as gVisor synthesize inode numbers incompatible with `st_ino` mount verification); empty-scratch `sandbox=true` remains supported with `runtimeClassName`.
- Track `sandbox_specified: bool` (True whenever any of `sandbox`, `mount_dir`, `mount_read_only` is not `None`) and `sandbox: WorkerSandboxConfig | None`.
- On `_NamedWorkerFleet.admit`:
  - First call for `worker_key` locks `(resource_profile, sandbox)`.
  - Later call with `sandbox_specified=False` reuses the locked `sandbox` (and `resource_profile` as today).
  - Later call with `sandbox_specified=True` must match the locked `sandbox` exactly; any mismatch (including requesting `sandbox=True` on an unsandboxed worker or `sandbox=False` on a sandboxed worker) raises `NamedWorkerError`.
- `ensure_named_worker` returns `(WorkerHandle, WorkerSpec)` (or exposes the admitted `WorkerSandboxConfig | None`) so callers always know the worker's effective sandbox config even when a follow-up call omitted the flags.

## Seven Trust Surfaces Stripped in Sandbox Mode

When a named worker's admitted `sandbox` is not `None`:

1. **Workspace mount** (`_dedicated_worker_common.py`, `kubernetes.py`, `kubernetes_resources.py`):
   - `mount_dir is None`: mount **no** workspace `subPath`.
   - `mount_dir is not None`: resolve the agent's single `user_agent` workspace root, verify `workspace` and `mount_dir` exist without following symlinks (`open_directory_within_root(..., create=False)`), and mount only `subPath: <workspace_rel>/<mount_dir>` at canonical `mountPath: <storage_mount_path>/<workspace_rel>/<mount_dir>`, with `readOnly: True` when `mount_read_only=True`. Skip private legacy alias mounts and omit ordinary-worker `extra_containers`/`extra_volumes` and primary manifest env/secrets.
   - Pre-validate `mount_dir` existence/no-symlink on the primary before admission/pod creation so typos or symlinks fail fast with `NamedWorkerError` (CLI exit `125`) without creating worker state directories or Deployments; keep the authoritative check in `plan_scoped_workspace_mounts`.
2. **Shared directories**: omit all `plan_shared_directory_mounts` (`kubernetes.py` + `kubernetes_resources.py`).
3. **Knowledge bases**: omit all `_knowledge_storage_mounts` (`kubernetes_resources.py`).
4. **Mirrored shared credentials**: `WorkerSpec.mirror_grantable_credentials = False` so `_sync_shared_credentials` mirrors `frozenset()` (leaving `.shared_credentials` empty and read-only). Primary also skips credential leases (`credentials_manager=None`, `worker_grantable_credentials=None`).
5. **Per-call env, `extra_env_passthrough`, `temporary_secrets`, Agent Keys**: in `sandbox_proxy.py` `_build_execute_request`, skip `current_tool_environment()`, `stand_ins.execution_env`, `extra_env_passthrough`, and `execution_env_for_worker(...)`; send `execution_env={}`, `extra_env_passthrough=None`, `environment=CallEnvironment()`.
6. **Agent CLI grant (`MINDROOM_AGENT_CLI_TOKEN`)**: stripped automatically with `current_tool_environment()`, preventing calls back to the primary / normal worker.
7. **Workspace env hook & pod label**:
   - Runner skips `.mindroom/worker-env.sh` when `request.sandbox` is `True`.
   - Kubernetes `_pod_template` stamps `LABEL_WORKER_SANDBOX: "true"` on pod template labels (and Deployment labels via template copy, never selector) and persists `_ANNOTATION_SANDBOX` on Deployment annotations for template-drift checks.

## Runner & Wire Protocol (Protocol 11)

- Bump `WORKER_PROTOCOL_VERSION = 11` in `src/mindroom/workers/protocol.py` and add `sandbox: bool = False` to `ExecuteRequest` (update `tests/test_worker_protocol_schema.py`).
- In `src/mindroom/tool_system/sandbox_proxy.py`:
  - When admitted `sandbox` is not `None`:
    - If `sandbox.mount_dir is None`: `route.base_dir = None` (worker runs in its own ephemeral `<state_subpath>/workspace`).
    - If `sandbox.mount_dir` is set: `route.base_dir` is `<workspace_rel>/<mount_dir>`.
    - If explicit `mindroom_output_path` was supplied on either empty scratch (`mount_dir is None`) or `mount_read_only=True`: raise `NamedWorkerError`.
    - For writable `mount_dir`, rewrite runner `saved_to_file` receipt paths to `<mount_dir>/<path>` so paths stay agent-workspace-relative.
    - Set `request.sandbox = True`.
- In `src/mindroom/api/sandbox_runner.py` & `src/mindroom/api/sandbox_env_assembly.py`:
  - `_route_workspace`: when `isinstance(route, ExecuteRequest) and route.sandbox` and `worker is not None` and `route.base_dir is None`, return `worker.base_dir` (`paths.workspace.resolve()`) instead of calling `resolve_agent_runtime(..., create=True)`. When `workspace.is_dir()` is already true, do not call `mkdir`.
  - `_prepare_execute_request`: when `request.sandbox` is `True`:
    - Force `execution_env = {}` (never fall back to `sandbox_exec.request_execution_env` / `build_execution_tool_env`), empty `call_environment`, empty `call_path_prepend`, `credential_overrides = {}`.
    - Call `build_request_execution_env(..., sandbox=True)` which skips `.mindroom/worker-env.sh`, points `HOME`, `XDG_CONFIG_HOME`, `XDG_DATA_HOME`, `XDG_STATE_HOME` to `prepared.paths.root` (writable worker state root so RO mounts work and mounted dirs aren't polluted), and keeps `MINDROOM_AGENT_WORKSPACE = str(request_workspace)`.
    - Set `tool_output_workspace_root = None` when `request.base_dir is None` (empty scratch) or `route.workspace` is not writable (`not os.access(route.workspace, os.W_OK)`), so large output never auto-saves to an unreachable ephemeral worker path or crashes on a read-only mount.
    - Pass `sandbox=True` on `PreparedSandboxRunnerExecuteRequest` so `_resolve_entrypoint` sets `allowed_shared_services=frozenset()`.
  - Streamed execution (`stream_io=True`, `_shell_launch`) shares `_prepare_execute_request`, so `mindroom-agent workers run --sandbox` gets identical isolation.

## Verification Strategy

- Unit & integration tests in `tests/test_named_workers.py`, `tests/test_kubernetes_worker_backend.py`, `tests/test_agent_cli_main.py`, `tests/test_sandbox_runner.py` (or dedicated sandbox runner test cases), and `tests/test_worker_protocol_schema.py`:
  - Validation errors, `mount_dir` implying `sandbox=True`, `mount_read_only` rules, first-call locking & follow-up reuse without repeating flags, refusal on conflicting sandbox options.
  - Kubernetes pod template mounts (empty scratch = 0 workspace/shared/knowledge mounts; `mount_dir` = single subpath mount RW or RO; missing dir / symlink refused; `mindroom.ai/worker-sandbox: "true"` label; `.shared_credentials` mirrors nothing; non-sandboxed worker template hash unchanged).
  - Primary request stripping (no `MINDROOM_AGENT_CLI_TOKEN`, no Agent Keys broker env, no `execution_env`/passthrough/temp secrets, no leases, even on follow-up calls omitting flags; `mindroom_output_path` refused on `mount_read_only`).
  - Runner execution (`sandbox=True` empty scratch uses ephemeral worker workspace without touching agent workspace; `.mindroom/worker-env.sh` is not sourced; `HOME` points to writable worker root while `cwd` & `MINDROOM_AGENT_WORKSPACE` point to mounted dir; RO mount handles large output without crashing; `stream_io=True` via `workers run --sandbox --mount-dir --mount-read-only`).
- `ruff check`, `ruff format --check`, `uv run --with tach tach check --dependencies --interfaces`, and pytest with default addopts.
