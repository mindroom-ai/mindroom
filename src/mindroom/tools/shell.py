"""Shell tool configuration with async subprocess execution that stops a command at its timeout."""

from __future__ import annotations

import functools
import inspect
import os
import re
import shlex
import subprocess
import tempfile
from dataclasses import replace
from pathlib import Path
from typing import TYPE_CHECKING

from mindroom.constants import (
    WORKSPACE_HOME_CONTRACT_ENV_NAMES,
    RuntimePaths,
    shell_execution_runtime_env_values,
    shell_extra_env_values,
    subprocess_path_with_prepends,
    workspace_home_identity_env,
)
from mindroom.path_confinement import resolve_path_within_root
from mindroom.runtime_env_policy import (
    SANDBOX_RUNTIME_ENV_BY_KEY,
    SHELL_NON_INTERACTIVE_ENV_DEFAULTS,
    strip_authenticated_proxy_env,
)
from mindroom.shell.background_log import BackgroundLogPolicy
from mindroom.shell.background_scope import current_shell_background_scope
from mindroom.shell.output_capture import ShellOutputDestination, ShellSpillPolicy
from mindroom.shell.policy import (
    default_run_timeout_seconds,
    normalize_shell_args,
    shell_call_timeout,
)
from mindroom.shell.runner import run_foreground
from mindroom.shell.supervisor import (
    SHELL_SUPERVISOR_SOCKET_ENV,
    current_shell_dispatch_context,
    run_command_via_supervisor,
)
from mindroom.terminal.lines import PROMPT_COMMAND
from mindroom.terminal.session import TerminalError, TerminalLaunch
from mindroom.tool_system.authoring import Toolkit
from mindroom.tool_system.declarations import (
    ConfigField,
    SetupType,
    ToolCategory,
    ToolExecutionTarget,
    ToolFileAccess,
    ToolManagedInitArg,
    ToolStatus,
    bool_config_field,
)
from mindroom.tool_system.output_files import (
    ToolOutputFileHandled,
    active_tool_output_file_request,
    current_tool_workdir,
)
from mindroom.tool_system.progress import report_tool_metadata, report_tool_progress, structured_result_requested
from mindroom.tool_system.registration import register_tool_with_metadata
from mindroom.tool_system.tool_environment import ToolEnvironment, current_tool_environment
from mindroom.tool_system.tool_function import ToolOutcome
from mindroom.tool_system.tool_schema import closed_parameters
from mindroom.vendor_telemetry import vendor_telemetry_env_values
from mindroom.workers.models import ScriptResourceProfileName  # noqa: TC001 - the tool schema reads it at runtime.

if TYPE_CHECKING:
    from collections.abc import Awaitable, Callable, Mapping

    from mindroom.shell.background_scope import ShellBackgroundScope
    from mindroom.shell.runner import ShellRunResult
    from mindroom.tool_system.tool_function import ToolFunction

_LOCAL_SHELL_PASSTHROUGH_ENV_KEYS = frozenset(
    {
        "CURL_CA_BUNDLE",
        "GIT_SSL_CAINFO",
        "HOME",
        "HTTP_PROXY",
        "HTTPS_PROXY",
        "LANG",
        "LC_ALL",
        "LC_CTYPE",
        "NODE_EXTRA_CA_CERTS",
        "NO_PROXY",
        "PATH",
        "PIP_CACHE_DIR",
        "PYTHONPATH",
        "PYTHONPYCACHEPREFIX",
        "REQUESTS_CA_BUNDLE",
        "SSL_CERT_DIR",
        "SSL_CERT_FILE",
        "SHELL",
        "TMPDIR",
        "UV_CACHE_DIR",
        "USER",
        "VIRTUAL_ENV",
        "XDG_CACHE_HOME",
        "XDG_CONFIG_HOME",
        "XDG_DATA_HOME",
        "XDG_STATE_HOME",
        "http_proxy",
        "https_proxy",
        "no_proxy",
    },
)
_WORKSPACE_CWD_NOTE = (
    "CWD is your agent workspace. Always use relative paths or `$MINDROOM_AGENT_WORKSPACE` instead of `~` "
    "(worker-routed execution maps `~` to the workspace; local execution maps it to the host home)."
)
_WORKDIR_CWD_NOTE = (
    "The command runs in the workdir as its working directory (relative paths resolve there). "
    "Use `$MINDROOM_AGENT_WORKSPACE` for workspace files (skills, subagent profiles, saved outputs, delegation records)."
)
# Working method distilled from RRSI harness-search runs on graded terminal tasks, where these habits
# removed the most common silent failures of a shell agent (wrong field matched, merged file boundaries,
# reserialized edits, unverified renames, truncated reads).
_WORKING_METHOD_NOTE = (
    "Working method: sample large inputs first (head/tail/grep/wc) rather than dumping them, but compute over full "
    "input. Filter on the extracted field value, not the whole line, and sanity-check samples. Never raw-concat text "
    "files (missing trailing newlines merge boundary tokens/records): process separately or insert a separator and "
    "test without trailing newlines (except byte-for-byte split chunks). For single-value edits, replace only that "
    "span preserving all other bytes/comments/spacing. Verify outputs back, grep for leftover old names after "
    "renames, run tests, and recheck suspicious results (e.g. zero counts)."
)

# How a call uses the named workers its agent may run, which `workers.named` owns and removes.
_NAMED_WORKERS_NOTE = (
    "Named workers: pass `worker` (short name) for an isolated pod (own processes, ports, `/tmp`; same workspace), "
    "started on first use and reused across calls in this reply (`background_after=0` fans out in parallel; scripts "
    "can call `mindroom-agent workers run NAME -- COMMAND`). `[worker: NAME, started]` marks MindRoom-recorded "
    "startup, not pod restarts. Removed when reply/jobs end or after several idle minutes—save outputs to workspace "
    "and keep long work in the call itself (not detached). Give each worker its own output dir and git worktree; "
    "pre-install shared deps in workspace (caches start empty). `request_network_access` does not apply to named "
    "workers. Pass `sandbox=true` (scratch only) or `mount_dir` (non-hidden workspace subdir; optional "
    "`mount_read_only=true`) on first call for sandboxed isolation (no shared dirs, KB, credentials, env secrets, "
    "or CLI grants)."
)
_NO_NAMED_WORKERS_ERROR = (
    "Error: `worker` and `resource_profile` need a shell that runs in a worker on the kubernetes worker backend, "
    "and this agent's shell runs in MindRoom's own process."
)

_NO_WORKER_ERROR = (
    "Error: Shell commands run only in a worker, and this agent's shell runs in MindRoom's own process. "
    "Route `shell` to a worker, or set `defaults.allow_shell_without_worker: true` in config.yaml to run them here."
)

# A stopped command registers no handle, so every call shares one namespace.
_SHELL_NAMESPACE = "shell"


def _not_started(refusal: str) -> ToolOutcome:
    """Answer a call refused before any command ran, and report that, so a caller that stores results can tell."""
    report_tool_metadata(not_started=True)
    return ToolOutcome(content=refusal, is_error=True)


def shell_refuses_commands(runtime_paths: RuntimePaths, *, allow_without_worker: bool) -> bool:
    """Return whether a shell built for ``runtime_paths`` refuses its commands.

    A worker builds the toolkit in runner mode; anywhere else its commands would run in MindRoom's own process, which
    ``defaults.allow_shell_without_worker`` (``allow_without_worker``) must allow.
    """
    return not allow_without_worker and not runtime_paths.env_flag(SANDBOX_RUNTIME_ENV_BY_KEY["runner_mode"])


def _validate_shell_timeout(timeout: object) -> None:
    """Reject a timeout that is not a positive number of seconds."""
    if timeout is not None and (not isinstance(timeout, int | float) or timeout <= 0):
        msg = "timeout must be a positive number of seconds; omit it for the default limit."
        raise ValueError(msg)


def _shell_path_prepend_entries(shell_path_prepend: str | None) -> tuple[str, ...]:
    """Parse configured shell PATH prefixes."""
    if shell_path_prepend is None:
        return ()
    return tuple(part.strip() for part in re.split(r"[\n,]+", shell_path_prepend) if part.strip())


def _workspace_home_contract_env_from_process_env(base_process_env: dict[str, str]) -> dict[str, str]:
    """Return MindRoom-owned workspace env only when the full workspace contract is present."""
    workspace = base_process_env.get("MINDROOM_AGENT_WORKSPACE")
    if not workspace or base_process_env.get("HOME") != workspace:
        return {}

    expected_identity = workspace_home_identity_env(workspace)
    if any(base_process_env.get(name) != value for name, value in expected_identity.items()):
        return {}

    return {key: value for key, value in base_process_env.items() if key in WORKSPACE_HOME_CONTRACT_ENV_NAMES}


def _shell_subprocess_env(
    runtime_env: Mapping[str, str],
    *,
    call_environment: ToolEnvironment,
    base_process_env: dict[str, str] | None = None,
    shell_path_prepend: str | None = None,
    workspace_dir: Path | None = None,
    resolved_workspace_dir: Path | None = None,
    process_environ: Mapping[str, str] | None = None,
    interactive: bool = False,
    in_sandbox_request: bool = False,
) -> dict[str, str]:
    """Build the env passed to shell subprocesses.

    ``call_environment`` is the call's own environment, applied over ``runtime_env``; the variables it removes stay
    away from the command even where the base environment would give them, while the workspace variables, ``PATH``
    entries, and telemetry settings MindRoom adds here still apply. ``process_environ`` is the environment of the
    process the command runs from, by default this one's.
    """
    environ = os.environ if process_environ is None else process_environ
    env = {key: value for key, value in environ.items() if key in _LOCAL_SHELL_PASSTHROUGH_ENV_KEYS}
    if base_process_env is not None:
        env.update({key: value for key, value in base_process_env.items() if key in _LOCAL_SHELL_PASSTHROUGH_ENV_KEYS})
    if not in_sandbox_request:
        # Scrub ambient proxies only: explicitly passed or injected proxies are part of the call's configuration.
        env = strip_authenticated_proxy_env(env)
    if not interactive:
        env.update(SHELL_NON_INTERACTIVE_ENV_DEFAULTS)
    env.update(call_environment.applied(runtime_env, base_process_env or {}))
    for name in call_environment.removed:
        env.pop(name, None)
    workspace_home_contract = (
        _workspace_home_contract_env_from_process_env(base_process_env) if base_process_env is not None else {}
    )
    env.update(workspace_home_contract)
    if not workspace_home_contract:
        if resolved_workspace_dir is not None:
            env["MINDROOM_AGENT_WORKSPACE"] = str(resolved_workspace_dir)
        elif workspace_dir is not None:
            # Local (non-worker) execution keeps the host HOME, but the workspace
            # env var is still promised to agents, so export it from workspace_dir.
            env["MINDROOM_AGENT_WORKSPACE"] = str(workspace_dir.expanduser().resolve())

    path_value = subprocess_path_with_prepends(
        env.get("PATH"),
        prepend_entries=(*call_environment.path_prepend, *_shell_path_prepend_entries(shell_path_prepend)),
    )
    if path_value is None:
        env.pop("PATH", None)
    else:
        env["PATH"] = path_value
    env.update(vendor_telemetry_env_values())
    return env


def _terminal_cwd(base_dir: Path | None, cwd: str | None) -> str | None:
    """Return the folder a terminal starts in: ``cwd`` inside the workspace, else the workspace itself."""
    if base_dir is None:
        if cwd:
            raise TerminalError(400, "This shell has no workspace to start a terminal in.")
        return None
    try:
        folder = resolve_path_within_root(base_dir, cwd or "", symlinks="internal", strict=True)
    except (OSError, ValueError):
        folder = None
    if folder is None or not folder.is_dir():
        raise TerminalError(400, "A terminal starts only in a folder inside the workspace.")
    return str(folder)


def _login_bash_command_index(args: list[str]) -> int | None:
    """Return the command index for `bash -lc ...` style invocations."""
    if not args or Path(args[0]).name != "bash":
        return None

    login_shell = False
    command_index: int | None = None
    for index, arg in enumerate(args[1:], start=1):
        if arg == "--":
            break
        if not arg.startswith("-"):
            break
        if arg in {"-l", "--login"}:
            login_shell = True
            continue
        if arg.startswith("--"):
            continue
        if "l" in arg:
            login_shell = True
        if "c" in arg:
            command_index = index + 1
            break

    if not login_shell or command_index is None or command_index >= len(args):
        return None
    return command_index


def _restore_env_exports_for_login_shell(env: dict[str, str]) -> str:
    """Return shell exports that restore env values commonly reset by login startup."""
    path_value = env.get("PATH")
    if path_value is None:
        return ""
    return f"export PATH={shlex.quote(path_value)}; "


def _shell_subprocess_args(args: list[str], env: dict[str, str]) -> list[str]:
    """Return argv adjusted for shell startup files that rewrite env overlays."""
    command_index = _login_bash_command_index(args)
    if command_index is None:
        return args

    env_restore = _restore_env_exports_for_login_shell(env)
    if not env_restore:
        return args

    adjusted_args = list(args)
    # Login bash can reset PATH in /etc/profile after subprocess env is applied.
    # Restore MindRoom's captured execution PATH without re-sourcing workspace hooks.
    adjusted_args[command_index] = env_restore + adjusted_args[command_index]
    return adjusted_args


def _shell_output_policies(*, allow_spill: bool) -> tuple[ShellOutputDestination | None, ShellSpillPolicy | None]:
    request = active_tool_output_file_request()
    if request is None:
        return None, None
    policy = request.policy
    root = str(policy.workspace_root)
    if request.path is not None:
        destination = ShellOutputDestination(
            root,
            request.path.requested_path,
            policy.max_bytes,
            policy.tools_use_workdir,
        )
        return destination, None
    if allow_spill and not structured_result_requested():
        return None, ShellSpillPolicy(root, policy.max_bytes, policy.tools_use_workdir)
    return None, None


@register_tool_with_metadata(
    name="shell",
    display_name="Shell Commands",
    description="Run terminal commands and scripts in the agent workspace",
    category=ToolCategory.DEVELOPMENT,
    file_access=ToolFileAccess.UNCONFINED,
    executes_code=True,
    status=ToolStatus.AVAILABLE,
    setup_type=SetupType.NONE,
    default_execution_target=ToolExecutionTarget.WORKER,
    consumes_workspace_paths=True,
    icon="Terminal",
    icon_color="text-green-500",
    config_fields=[
        ConfigField(
            name="base_dir",
            label="Base Dir",
            type="text",
            required=False,
            default=None,
            authored_override=False,
        ),
        bool_config_field("enable_run_shell_command", "Enable Run Shell Command"),
        bool_config_field("all", "All", default=False),
        ConfigField(
            name="extra_env_passthrough",
            label="Extra Env Passthrough",
            type="text",
            required=False,
            default=None,
            placeholder="MY_SERVICE_URL, MY_SERVICE_*",
            description=(
                "Comma or newline-separated env var names or glob patterns to expose to shell "
                "execution in addition to the committed runtime env."
            ),
        ),
        ConfigField(
            name="shell_path_prepend",
            label="Shell PATH Prepend",
            type="text",
            required=False,
            default=None,
            placeholder="/opt/custom/bin, /run/wrappers/bin",
            description="Comma or newline-separated path entries to prepend to PATH for shell execution.",
        ),
    ],
    agent_override_fields=[
        ConfigField(
            name="extra_env_passthrough",
            label="Env Passthrough",
            type="string[]",
            required=False,
            default=None,
            placeholder="GITEA_TOKEN",
            description="Extra env var names or glob patterns exposed to shell execution for this agent only.",
        ),
        ConfigField(
            name="shell_path_prepend",
            label="PATH Prepend",
            type="string[]",
            required=False,
            default=None,
            placeholder="/run/wrappers/bin",
            description="Path entries prepended to PATH for this agent's shell tool only.",
        ),
    ],
    managed_init_args=(
        ToolManagedInitArg.RUNTIME_PATHS,
        ToolManagedInitArg.TOOL_OUTPUT_WORKSPACE_ROOT,
        ToolManagedInitArg.IN_SANDBOX_REQUEST,
    ),
    dependencies=[],
    docs_url="https://docs.mindroom.chat/tools/execution-and-coding/#shell",
    function_names=("run_shell_command",),
)
def shell_tools() -> type[Toolkit]:  # noqa: C901 - one toolkit class with its functions and its terminal
    """Return shell tools for command execution."""

    class MindRoomShellTools(Toolkit):
        """MindRoom shell toolkit with async execution that stops a command at its timeout."""

        def __init__(
            self,
            base_dir: Path | str | None = None,
            enable_run_shell_command: bool = True,
            all: bool = False,  # noqa: A002
            extra_env_passthrough: str | None = None,
            shell_path_prepend: str | None = None,
            *,
            runtime_paths: RuntimePaths,
            tool_output_workspace_root: Path | None = None,
            allow_without_worker: bool = False,
            named_workers: bool = False,
            in_sandbox_request: bool = False,
        ) -> None:
            self.base_dir: Path | None = Path(base_dir) if isinstance(base_dir, str) else base_dir
            # Beside a workdir, ``base_dir`` is the workdir; ``$MINDROOM_AGENT_WORKSPACE`` stays the workspace.
            self._beside_workdir = current_tool_workdir() is not None
            workspace_dir = tool_output_workspace_root if self._beside_workdir else self.base_dir
            self._resolved_base_dir = self.base_dir.expanduser().resolve() if self.base_dir is not None else None
            self._resolved_workspace_dir = (
                self._resolved_base_dir
                if workspace_dir is self.base_dir
                else (workspace_dir.expanduser().resolve() if workspace_dir is not None else None)
            )
            self._refuse_commands = shell_refuses_commands(runtime_paths, allow_without_worker=allow_without_worker)
            # Whether calls may name a worker of their own, which the primary then routes them to.
            self._named_workers = named_workers
            self._in_worker = runtime_paths.env_flag(SANDBOX_RUNTIME_ENV_BY_KEY["runner_mode"])
            self._in_sandbox_request = in_sandbox_request

            tools: list[Callable[..., object]] = []
            if all or enable_run_shell_command:
                tools.append(self.run_shell_command)

            super().__init__(name="shell_tools", tools=tools)

            # Sandbox tool subprocesses (and direct runner-supervised shell calls) delegate execution
            # to the runner's long-lived shell supervisor, which stops the command on client disconnect.
            dispatch = current_shell_dispatch_context()
            self._supervisor_socket = (
                dispatch.socket_path if dispatch is not None else os.environ.get(SHELL_SUPERVISOR_SOCKET_ENV)
            ) or None
            self._process_environ = dispatch.process_environ if dispatch is not None else None
            if in_sandbox_request:
                # The runner's request paths include its authorized execution env and worker overlays.
                self._runtime_env = shell_execution_runtime_env_values(
                    runtime_paths,
                    extra_env_passthrough=extra_env_passthrough,
                    process_env=runtime_paths.process_env,
                    home_dir=self._process_environ.get("HOME") if self._process_environ else None,
                )
            else:
                self._runtime_env = shell_extra_env_values(
                    extra_env_passthrough=extra_env_passthrough,
                    process_env=runtime_paths.process_env,
                )
            self._base_process_env = dict(runtime_paths.process_env)
            self._shell_path_prepend = shell_path_prepend

        def offered_function(self, function: ToolFunction) -> ToolFunction:
            """Describe ``run_shell_command`` with where and how it runs, offering named workers only where it may."""
            cwd_note = _WORKDIR_CWD_NOTE if self._beside_workdir else _WORKSPACE_CWD_NOTE
            notes = (cwd_note, _WORKING_METHOD_NOTE) if self.base_dir is not None else (_WORKING_METHOD_NOTE,)
            hidden = (
                () if self._named_workers else ("worker", "resource_profile", "sandbox", "mount_dir", "mount_read_only")
            )
            properties = {
                name: value for name, value in function.parameters["properties"].items() if name not in hidden
            }
            return replace(
                function,
                description="\n\n".join(
                    (function.description or "", *notes, *((_NAMED_WORKERS_NOTE,) if self._named_workers else ())),
                ).strip(),
                parameters=closed_parameters(self.run_shell_command, {**function.parameters, "properties": properties}),
            )

        def _command_env(
            self,
            process_environ: Mapping[str, str] | None = None,
            *,
            interactive: bool = False,
        ) -> dict[str, str]:
            """Return the environment a command runs with, from ``process_environ`` (by default this process's)."""
            return _shell_subprocess_env(
                self._runtime_env,
                # The call's own environment: plugin hooks' changes and the response's CLI grant.
                call_environment=current_tool_environment(),
                base_process_env=self._base_process_env,
                shell_path_prepend=self._shell_path_prepend,
                resolved_workspace_dir=self._resolved_workspace_dir,
                process_environ=process_environ,
                interactive=interactive,
                in_sandbox_request=self._in_sandbox_request,
            )

        def _resolve_workdir(self, workdir: str | None, env: Mapping[str, str]) -> str | None:
            """Resolve a command's cwd without restricting it to the workspace."""
            if workdir is None:
                return str(self.base_dir) if self.base_dir else None
            if not workdir.strip() or "\0" in workdir:
                msg = "workdir must be a non-blank path without NUL bytes."
                raise ValueError(msg)
            try:
                if workdir == "~" or workdir.startswith("~/"):
                    home = env.get("HOME") or os.path.expanduser("~")  # noqa: PTH111 - no error if the host home is unknown.
                    # Only the leading tilde expands; extra slashes after it are not a new root.
                    path = Path(home) / workdir[2:].lstrip("/")
                else:
                    path = Path(workdir)
                if not path.is_absolute():
                    path = (self._resolved_base_dir or Path.cwd()) / path
                path = path.resolve(strict=True)
                if not path.is_dir():
                    msg = f"workdir {workdir!r} is not a directory."
                    raise ValueError(msg)
                if not os.access(path, os.X_OK):
                    msg = f"workdir {workdir!r} is not searchable."
                    raise ValueError(msg)
            except OSError as exc:
                msg = f"cannot use workdir {workdir!r}: {exc}"
                raise ValueError(msg) from exc
            return str(path)

        def terminal_launch(
            self,
            cwd: str | None,
            *,
            process_environ: Mapping[str, str] | None,
        ) -> TerminalLaunch:
            """Return an interactive Bash in the environment and workspace this toolkit's commands get.

            Not a function the agent calls: the sandbox runner starts it in a pty beside the commands it runs. It
            reads no startup files or readline settings and skips the non-interactive defaults. It starts with
            Bash's built-in key bindings. ``process_environ`` is the environment of the process a command runs from
            there, None for this one's.
            The shell marks each prompt, which ``terminal_write(action="run")`` waits for (``mindroom.terminal.lines``).
            """
            env = self._command_env(process_environ, interactive=True)
            env.update(TERM="xterm-256color", PROMPT_COMMAND=PROMPT_COMMAND, INPUTRC="/dev/null")
            return TerminalLaunch(
                argv=("bash", "--noprofile", "--norc", "-i"),
                env=env,
                cwd=_terminal_cwd(self.base_dir, cwd),
            )

        def command_launch(
            self,
            *args: object,
            process_environ: Mapping[str, str] | None,
            **kwargs: object,
        ) -> tuple[TerminalLaunch, float | None]:
            """Return how a ``run_shell_command`` call with these arguments starts its command, and its time limit.

            Not a function the agent calls: the sandbox runner starts the command of a call whose output it streams
            as raw bytes (`mindroom-agent workers run`) on pipes of its own. ``process_environ`` is as for
            ``terminal_launch``. Arguments the call cannot take raise ``ValueError`` or ``TypeError``.
            """
            call = inspect.signature(self.run_shell_command).bind(*args, **kwargs).arguments
            timeout = call.get("timeout")
            _validate_shell_timeout(timeout)
            workdir = call.get("workdir")
            if workdir is not None and not isinstance(workdir, str):
                msg = "workdir must be a string path."
                raise ValueError(msg)
            env = self._command_env(process_environ)
            argv = _shell_subprocess_args(normalize_shell_args(call["args"]), env)
            launch = TerminalLaunch(argv=tuple(argv), env=env, cwd=self._resolve_workdir(workdir, env))
            return launch, shell_call_timeout(timeout)

        def _refusal(self, *, named_worker: bool) -> str | None:
            """Return why a call cannot run here, where ``named_worker`` says it names a worker of its own."""
            if self._refuse_commands:
                return _NO_WORKER_ERROR
            # The primary routes a call to the worker it names, so one that reaches a shell outside a worker has none.
            if named_worker and not self._in_worker:
                return _NO_NAMED_WORKERS_ERROR
            return None

        def _background_log_policy(self) -> BackgroundLogPolicy:
            log_root = self._resolved_workspace_dir or self._resolved_base_dir
            if log_root is None:
                temp_root = (
                    self._process_environ.get("TMPDIR") if self._process_environ else None
                ) or tempfile.gettempdir()
                return BackgroundLogPolicy(
                    log_dir=str(Path(temp_root) / f"mindroom-shell-logs-{os.getuid()}"),
                )
            display_root = (
                str(self._resolved_base_dir)
                if self._resolved_base_dir is not None
                and not self._beside_workdir
                and log_root == self._resolved_base_dir
                else None
            )
            return BackgroundLogPolicy(
                log_dir=str(log_root / ".mindroom" / "shell"),
                display_root=display_root,
            )

        def _command_runner(
            self,
            *,
            bg_scope: ShellBackgroundScope | None,
            timeout: int | None,
        ) -> Callable[..., Awaitable[ShellRunResult]]:
            if self._supervisor_socket is not None:
                return functools.partial(
                    run_command_via_supervisor,
                    self._supervisor_socket,
                    namespace=bg_scope.namespace if bg_scope is not None else _SHELL_NAMESPACE,
                    on_timeout="background" if bg_scope is not None else "kill",
                    max_runtime_seconds=timeout if bg_scope is not None else None,
                    background_log=self._background_log_policy() if bg_scope is not None else None,
                )
            if bg_scope is not None:
                return functools.partial(
                    bg_scope.registry.run_or_background,
                    namespace=bg_scope.namespace,
                    # Never the terminal MindRoom runs in, where a prompting command would wait forever.
                    stdin=subprocess.DEVNULL,
                    max_runtime_seconds=timeout,
                    background_log=self._background_log_policy(),
                )
            return functools.partial(run_foreground, stdin=subprocess.DEVNULL)

        async def run_shell_command(
            self,
            args: str | list[str],
            tail: int = 100,
            timeout: int | None = None,  # noqa: ASYNC109
            workdir: str | None = None,
            worker: str | None = None,
            resource_profile: ScriptResourceProfileName | None = None,
            sandbox: bool | None = None,
            mount_dir: str | None = None,
            mount_read_only: bool | None = None,
        ) -> str | ToolOutcome | ToolOutputFileHandled:
            """Run a command via non-login Bash (prepared env; pass ``["bash", "-lc", cmd]`` only if login startup is needed).

            Returns last ``tail`` lines of stdout then stderr (all stderr on non-zero exit). Waits up to 120s in foreground:
            ``timeout`` <= 120 stops the process group inline and returns output so far; if still running at 120s, calls
            with ``background_after`` (unset) return a job handle (runs until done, ``timeout``, or cancel), while other
            calls (including subagents) background with PID/log/wait/stop commands and stop at ``timeout`` or turn end.
            Set ``timeout`` on commands that may hang; detach servers meant to outlive the turn via
            ``nohup cmd > out.log 2>&1 &``; poll in one command (``until <check>; do sleep 10; done`` + ``timeout``)
            instead of repeated ``sleep`` turns. With ``mindroom_output_path``, saves full output (up to byte limit)
            ignoring ``tail`` (managed top-level jobs save on completion; stopped/in-turn-backgrounded calls save none).

            Args:
                args: Shell command string (preferred; cd, &&, pipes, redirects), or argv list (no shell syntax).
                tail: Output lines to return.
                timeout: Stop command + process group after N seconds (<= 120 inline; null/> 120 backgrounds at 120s).
                workdir: Existing directory for this call (absolute or relative to default CWD; ~ uses command HOME).
                worker: Named worker (1-32 lowercase letters/digits/hyphens); null uses normal worker.
                resource_profile: Named worker size (small/standard/large; set on first call; null = default).
                sandbox: Run named worker in isolated sandbox (scratch only unless mount_dir set; no creds/secrets/CLI grants).
                mount_dir: Non-hidden workspace subdir to mount in sandbox (e.g. "scratch/job1"; "." and dot-segments refused; implies sandbox; writable mounts can still edit repo-local files like .git/hooks).
                mount_read_only: Mount mount_dir read-only (requires mount_dir).

            Returns:
                The command output or an error message.

            """
            refusal = self._refusal(
                named_worker=(
                    worker is not None
                    or resource_profile is not None
                    or sandbox is True
                    or mount_dir is not None
                    or mount_read_only is True
                ),
            )
            if refusal is not None:
                return _not_started(refusal)
            subprocess_env = self._command_env(self._process_environ)
            try:
                command_args = normalize_shell_args(args)
                _validate_shell_timeout(timeout)
                cwd = self._resolve_workdir(workdir, subprocess_env)
            except ValueError as exc:
                refused = _not_started(f"Error: {exc}")
                if worker is not None:
                    raise
                return refused
            argv = _shell_subprocess_args(command_args, subprocess_env)
            output_destination, spill_policy = _shell_output_policies(allow_spill=not mount_read_only)
            scope = current_shell_background_scope()
            wait_limit = default_run_timeout_seconds()
            bg_scope = scope if scope is not None and (timeout is None or timeout > wait_limit) else None
            result = await self._command_runner(bg_scope=bg_scope, timeout=timeout)(
                argv=argv,
                env=subprocess_env,
                cwd=cwd,
                tail=tail,
                timeout=wait_limit if bg_scope is not None else shell_call_timeout(timeout),
                on_progress=report_tool_progress,
                output_destination=output_destination,
                spill_policy=spill_policy,
                launch_failure_exits=worker is not None,
            )
            if bg_scope is not None and result.handle is not None:
                bg_scope.had_backgrounded = True
            if result.return_code is not None:
                report_tool_metadata(exit_code=result.return_code)
            if worker is not None and result.execution_error:
                raise ValueError(result.message)
            value: str | ToolOutcome = (
                ToolOutcome(
                    content=result.message,
                    is_error=True,
                    counts_as_tool_failure=result.execution_error or result.background_rejected,
                )
                if result.failed
                else result.message
            )
            return ToolOutputFileHandled(value) if result.output_file_handled else value

    return MindRoomShellTools
