# Named Worker Sandboxing Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: implement this plan task-by-task with the skill named in **Execution**. Steps use checkbox (`- [ ]`) syntax.

**Execution:** `baspowers:subagent-driven-development` — delegate implementation to `gemini4_neon@xhigh` (`minimal=True`) in one clean branch with frequent commits, then review with `opus` and `astra`.

**Goal:** Add sandbox isolation (`sandbox`, `mount_dir`, `mount_read_only`) to Kubernetes named workers across `run_shell_command`, `mindroom-agent workers run`, Kubernetes pod provisioning, primary request proxying, and the sandbox runner.

**Architecture:** Model sandbox isolation as `WorkerSandboxConfig(mount_dir, mount_read_only)` on `WorkerSpec`, locked on first admission in `workers/named.py` alongside `resource_profile`. Kubernetes provisioning strips workspace (or mounts one validated `subPath`), shared directories, knowledge bases, and mirrored credentials, and stamps `mindroom.ai/worker-sandbox=true`. Primary proxying and protocol 11 (`ExecuteRequest.sandbox`) strip call env, CLI grant, Agent Keys, leases, and `.mindroom/worker-env.sh`, while pointing `HOME` to the writable worker root.

**Tech Stack:** Python 3.13+, Pydantic v2, Kubernetes worker backend, pytest, ruff, tach.

**Spec:** `docs/baspowers/specs/2026-11-23-named-worker-sandbox-design.md`

## Global Constraints

- Only Kubernetes named workers (`named_workers >= 1`, `user_agent` scope, idle cleanup running); non-sandboxed worker pod templates and hashes must remain unchanged.
- `mount_dir` always implies `sandbox=True`; never allow narrowed mounts to retain credentials, Agent Keys, env secrets, or `MINDROOM_AGENT_CLI_TOKEN`.
- Never auto-create missing `mount_dir` via kubelet; validate existence and `O_NOFOLLOW` with `open_directory_within_root(..., create=False)`.
- Reuse existing primitives (`agent_workspace_relative_path`, `open_directory_within_root`, `WorkerSpec.mirror_grantable_credentials`, `ScopedWorkspaceMount`).
- Target lean, clean code (~250–400 LOC prod, ~1:1 test:prod covering real user flows and invariants).

## Review Focus

- Follow-up call omitting `sandbox`/`mount_dir` to an already-started sandboxed named worker MUST still strip `MINDROOM_AGENT_CLI_TOKEN`, Agent Keys, env secrets, and resolve the locked `base_dir` (not silently leak env on call 2).
- Empty-scratch sandbox (`sandbox=True, mount_dir=None`) MUST NOT trigger `resolve_agent_runtime(..., create=True)` in `_route_workspace` (which would crash with EROFS on `readOnlyRootFilesystem: true`).
- `sandbox=True` with `execution_env={}` MUST NOT fall back to `sandbox_exec.request_execution_env`'s `else` branch (`build_execution_tool_env`).
- Read-only `mount_dir` (`mount_read_only=True`) MUST keep `$HOME`/`$XDG_*_HOME` writable (worker state root), refuse explicit `mindroom_output_path`, and skip auto-save when `route.workspace` is not writable.
- `sandbox=False` / `mount_read_only=False` passed with `worker=None` by a model filling defaults MUST continue to work as an ordinary shell call.

---

### Task 1: Models, Protocol 11, and Named-Worker Admission

**Files:**
- Modify: `src/mindroom/workers/models.py`
- Modify: `src/mindroom/workers/protocol.py`
- Modify: `src/mindroom/workers/named.py`
- Test: `tests/test_worker_protocol_schema.py`, `tests/test_named_workers.py`

**Interfaces:**
- Produces: `LABEL_WORKER_SANDBOX`, `WorkerSandboxConfig(mount_dir: str | None = None, mount_read_only: bool = False)`, `WorkerSpec.sandbox: WorkerSandboxConfig | None = None`, `ExecuteRequest.sandbox: bool = False` (`WORKER_PROTOCOL_VERSION = 11`), `named_worker_call(..., sandbox=None, mount_dir=None, mount_read_only=None)`, `ensure_named_worker(...) -> tuple[WorkerHandle, WorkerSpec]` (or returns handle + admitted `WorkerSpec` via updated signature/callers).

- [ ] **Step 1:** Add `LABEL_WORKER_SANDBOX = "mindroom.ai/worker-sandbox"`, `WorkerSandboxConfig`, and `WorkerSpec.sandbox: WorkerSandboxConfig | None = None` in `src/mindroom/workers/models.py`.
- [ ] **Step 2:** Bump `WORKER_PROTOCOL_VERSION = 11` and add `sandbox: bool = False` to `ExecuteRequest` in `src/mindroom/workers/protocol.py`; update `_SCHEMA_DIGESTS[11]` in `tests/test_worker_protocol_schema.py`.
- [ ] **Step 3:** Extend `src/mindroom/workers/named.py` (`_NamedWorker`, `NamedWorkerCall`, `_NamedWorkerFleet.admit`, `named_worker_call`, `ensure_named_worker`) with validation, `mount_dir` normalization (`agent_workspace_relative_path`, reject `\x00`), first-call locking of `sandbox`, follow-up reuse/conflict refusal, `mirror_grantable_credentials=False` when sandboxed, and returning the admitted `WorkerSpec` alongside `WorkerHandle`.
- [ ] **Step 4:** Add unit tests in `tests/test_named_workers.py` and run `pytest tests/test_worker_protocol_schema.py tests/test_named_workers.py -x -n 0 --no-cov`.
- [ ] **Step 5:** Commit Task 1.

---

### Task 2: Kubernetes Mounts, Labels, Annotations, and Cache

**Files:**
- Modify: `src/mindroom/workers/backends/_dedicated_worker_common.py`
- Modify: `src/mindroom/workers/backends/kubernetes_resources.py`
- Modify: `src/mindroom/workers/backends/kubernetes.py`
- Test: `tests/test_kubernetes_worker_backend.py`

**Interfaces:**
- Consumes: `LABEL_WORKER_SANDBOX`, `WorkerSandboxConfig`, `WorkerSpec.sandbox`.
- Produces: `ScopedWorkspaceMount.read_only: bool = False`, `plan_scoped_workspace_mounts(..., sandbox=None)`, `_ANNOTATION_SANDBOX` + `parse_sandbox_annotation`.

- [ ] **Step 1:** Add `read_only: bool = False` to `ScopedWorkspaceMount` and `sandbox: WorkerSandboxConfig | None = None` to `plan_scoped_workspace_mounts` in `_dedicated_worker_common.py`: empty tuple when `sandbox.mount_dir is None`; single `open_directory_within_root(..., create=False)`-validated canonical mount (no legacy private aliases, `read_only=sandbox.mount_read_only`) when `sandbox.mount_dir is not None`.
- [ ] **Step 2:** Thread `sandbox: WorkerSandboxConfig | None = None` through `KubernetesResourceManager` (`apply_deployment`, `deployment_template_drifted`, `_deployment_manifest`, `_pod_template`, `_volume_mounts`, `_scoped_storage_mounts`, `_workspace_storage_mounts`): stamp `LABEL_WORKER_SANDBOX="true"` on pod template labels, omit shared directory & knowledge mounts, add `readOnly: True` for RO workspace mounts, persist/parse `_ANNOTATION_SANDBOX`.
- [ ] **Step 3:** Thread `spec.sandbox` in `KubernetesWorkerBackend` (`_provision_worker_locked`, `_plan_workspace_mounts`, `_plan_shared_directory_mounts`, `_is_reconcile_candidate`, `_reconcile_worker_deployment`).
- [ ] **Step 4:** Add tests in `tests/test_kubernetes_worker_backend.py` covering empty scratch mounts, subdir RW/RO mounts, missing/symlink `mount_dir` refusal, omitted shared/knowledge mounts, zero mirrored credentials, `mindroom.ai/worker-sandbox=true` label, and unchanged non-sandboxed template hash.
- [ ] **Step 5:** Commit Task 2.

---

### Task 3: Primary Proxy Stripping, Runner Isolation, Shell Tool, CLI & Docs

**Files:**
- Modify: `src/mindroom/tool_system/sandbox_proxy.py`
- Modify: `src/mindroom/api/sandbox_env_assembly.py`
- Modify: `src/mindroom/api/sandbox_runner.py`
- Modify: `src/mindroom/tools/shell.py`
- Modify: `src/mindroom/tool_system/agent_tool_calls.py`
- Modify: `src/mindroom/agent_cli/main.py`
- Modify: `src/mindroom/agent_cli/context.py`
- Modify: `docs/tools/execution-and-coding.md`, `docs/tools/agent-cli.md`, `docs/dev/2026-11-19-named-workers.md`
- Test: `tests/test_named_workers.py`, `tests/test_agent_cli_main.py`, `tests/test_sandbox_runner.py` (or appropriate runner test file)

**Interfaces:**
- Consumes: Tasks 1–2.
- Produces: End-to-end `run_shell_command(..., sandbox, mount_dir, mount_read_only)` and `mindroom-agent workers run [--sandbox] [--mount-dir DIR] [--mount-read-only]`.

- [ ] **Step 1:** Update `src/mindroom/tools/shell.py` (`run_shell_command` signature, hidden params when `not self._named_workers`, `_refusal`) and `src/mindroom/tool_system/agent_tool_calls.py` (`_invalid_arguments_reason`).
- [ ] **Step 2:** Update `src/mindroom/tool_system/sandbox_proxy.py`:
  - Pass `sandbox`, `mount_dir`, `mount_read_only` into `named_worker_call`.
  - Pre-validate `mount_dir` existence/no-symlink against the agent workspace before admission (raising `NamedWorkerError`).
  - Use the **admitted** `spec.sandbox` returned by `ensure_named_worker` to compute sandbox `base_dir`, refuse `mindroom_output_path` on `mount_read_only`, strip `call_environment` / `execution_env` / `extra_env_passthrough` / Agent Keys / credential leases, and set `request.sandbox = True`.
- [ ] **Step 3:** Update `src/mindroom/api/sandbox_env_assembly.py` and `src/mindroom/api/sandbox_runner.py`:
  - `_route_workspace`: support `ExecuteRequest.sandbox` with `base_dir=None` using `worker.base_dir` (ephemeral scratch) and skip `mkdir` when `workspace.is_dir()`.
  - `_prepare_execute_request` & `_resolve_entrypoint`: when `request.sandbox` is True, force empty env/overrides/call_environment, `allowed_shared_services=frozenset()`, skip `.mindroom/worker-env.sh`, keep `HOME`/`XDG_*_HOME` at `prepared.paths.root` while `MINDROOM_AGENT_WORKSPACE` stays `request_workspace`, and set `tool_output_workspace_root=None` when `route.workspace` is not writable. Verify streamed execution (`stream_io=True`) inherits the exact same stripping.
- [ ] **Step 4:** Update `src/mindroom/agent_cli/main.py` (`--sandbox`, `--mount-dir`, `--mount-read-only`), `src/mindroom/agent_cli/context.py`, and docs (`docs/tools/execution-and-coding.md`, `docs/tools/agent-cli.md`, `docs/dev/2026-11-19-named-workers.md`).
- [ ] **Step 5:** Add end-to-end tests covering all 5 Review Focus items, CLI flags, streamed execution, and runner isolation; run `ruff check`, `ruff format --check`, `uv run --with tach tach check --dependencies --interfaces`, and full touched pytest files with default addopts.
- [ ] **Step 6:** Commit Task 3.
