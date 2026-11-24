"""Shell tool configuration with async subprocess execution that stops a command at its timeout."""

from __future__ import annotations

import inspect
import json
import os
import re
import shlex
import subprocess
from dataclasses import replace
from pathlib import Path
from typing import TYPE_CHECKING, cast

from mindroom.constants import (
    WORKSPACE_HOME_CONTRACT_ENV_NAMES,
    RuntimePaths,
    shell_execution_runtime_env_values,
    subprocess_path_with_prepends,
    workspace_home_identity_env,
)
from mindroom.path_confinement import resolve_path_within_root
from mindroom.runtime_env_policy import SANDBOX_RUNTIME_ENV_BY_KEY
from mindroom.shell.execution import run_command, shell_call_timeout
from mindroom.shell.output_capture import ShellOutputDestination
from mindroom.shell.supervisor import SHELL_SUPERVISOR_SOCKET_ENV, run_command_via_supervisor
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
)
from mindroom.tool_system.output_files import (
    ToolOutputFileHandled,
    current_tool_output_file_request,
    current_tool_workdir,
)
from mindroom.tool_system.progress import report_tool_progress
from mindroom.tool_system.registration import register_tool_with_metadata
from mindroom.tool_system.tool_environment import ToolEnvironment, current_tool_environment
from mindroom.tool_system.tool_schema import closed_parameters
from mindroom.vendor_telemetry import vendor_telemetry_env_values
from mindroom.workers.models import ScriptResourceProfileName  # noqa: TC001 - the tool schema reads it at runtime.

if TYPE_CHECKING:
    from collections.abc import Callable, Mapping

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
        "TERM",
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
_SHELL_ARGS_ERROR = (
    '\'args\' must be a shell command string or a flat list of strings. Send args like "ls -la" or ["git", "status"].'
)
_SHELL_COMMAND_LINE_CHARS = frozenset("$|&;<>*?~`!(){}[]\n\r")
_WORKSPACE_CWD_NOTE = (
    "The command runs in your agent workspace as its working directory. Always use relative paths or "
    "`$MINDROOM_AGENT_WORKSPACE` for workspace files instead of `~`: worker-routed execution maps `~` "
    "to the workspace, while local execution maps it to the host home."
)
_WORKDIR_CWD_NOTE = (
    "The command runs in the workdir as its working directory, so relative paths name files there. "
    "`$MINDROOM_AGENT_WORKSPACE` is your agent workspace, which holds your skills, subagent profiles, saved tool "
    "outputs, and delegation records; use it for files meant for the workspace."
)
# Working method distilled from RRSI harness-search runs on graded terminal tasks, where these habits
# removed the most common silent failures of a shell agent (wrong field matched, merged file boundaries,
# reserialized edits, unverified renames, truncated reads).
_WORKING_METHOD_NOTE = (
    "Working method: inspect inputs first, sampling large files or outputs with head, tail, grep, or wc instead "
    "of printing everything, but compute results over the full input. Match filters against the extracted field "
    "value, not the whole line, and check them on a few sample records. When combining the lines or words of "
    "several text files, do not concatenate them raw: a file may lack its final newline, which merges its last "
    "word or record with the next file's first, so process each file separately or add a separator, and test "
    "with files that lack a trailing newline (joining split chunks of one file byte for byte is different). When "
    "only one value must change, replace just that span and keep every other byte, including comments and "
    "spacing. Afterwards verify the result: read outputs back, search for leftover old names after a rename, run "
    "available tests, and recheck suspicious results such as a zero count."
)

# How a call uses the named workers its agent may run, which `workers.named` owns and removes.
_NAMED_WORKERS_NOTE = (
    "Set `worker` to a short name to run the command in a worker of its own instead of your normal worker: a "
    "separate pod with its own processes, ports, and /tmp that mounts the same workspace. The first call with a "
    "name starts that worker, and later calls of this reply with the same name reach it, so several isolated "
    "stacks or shards of one job can run side by side. Calls with `background_after` 0 start their workers at the "
    "same time. Where `mindroom-agent` is available, a shell script does the same with "
    "`mindroom-agent workers run NAME -- COMMAND`, which waits and exits with the command's exit code. A result "
    "that starts with `[worker: NAME, started]` marks a new Deployment or startup MindRoom recorded, not a "
    "pod or container restart within that Deployment. "
    "Named workers are removed when this reply and its background jobs end, and one in which no command has run "
    "for several minutes is removed sooner, so keep results in the workspace and run long work as the call's own "
    "command, not detached from it. Give each worker its own output directory, and its own git worktree when it "
    "changes a checkout. Build or install what every worker needs once in the workspace before fanning out, "
    "because package caches start empty in each worker. Network access granted with `request_network_access` "
    "does not reach named workers. Set `sandbox=true` (empty scratch) or `mount_dir` (one non-hidden "
    "workspace-relative subdirectory, optionally with `mount_read_only=true`) on the first call for a name to run "
    "that worker in isolated sandbox mode without shared directories, knowledge bases, credentials, env secrets, "
    "or CLI grants."
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


def _normalize_shell_command_line(command: str) -> list[str]:
    """Run natural shell command strings through bash."""
    stripped = command.strip()
    if not stripped:
        raise ValueError(_SHELL_ARGS_ERROR)
    return ["bash", "-c", command]


def _looks_like_shell_command_line(command: str) -> bool:
    """Return whether a single argv item is probably a shell command line."""
    if any(char.isspace() for char in command):
        return True
    return any(char in _SHELL_COMMAND_LINE_CHARS for char in command)


def _normalize_shell_args(args: object) -> list[str]:
    """Normalize natural shell command strings and explicit argv lists."""
    if isinstance(args, str):
        stripped = args.strip()
        if not stripped:
            raise ValueError(_SHELL_ARGS_ERROR)
        if stripped[0] not in "[{":
            return _normalize_shell_command_line(args)
        try:
            args = json.loads(args)
        except json.JSONDecodeError as exc:
            if any(char.isspace() for char in stripped):
                return _normalize_shell_command_line(args)
            raise ValueError(_SHELL_ARGS_ERROR) from exc

    if not isinstance(args, list):
        raise ValueError(_SHELL_ARGS_ERROR)  # noqa: TRY004

    if any(not isinstance(item, str) for item in args):
        raise ValueError(_SHELL_ARGS_ERROR)

    normalized_args = cast("list[str]", args)
    if len(normalized_args) == 1 and _looks_like_shell_command_line(normalized_args[0]):
        return _normalize_shell_command_line(normalized_args[0])
    return normalized_args


def shell_refuses_commands(runtime_paths: RuntimePaths, *, allow_without_worker: bool) -> bool:
    """Return whether a shell built for ``runtime_paths`` refuses its commands.

    A worker builds the toolkit in runner mode; anywhere else its commands would run in MindRoom's own process, which
    ``defaults.allow_shell_without_worker`` (``allow_without_worker``) must allow.
    """
    return not allow_without_worker and not runtime_paths.env_flag(SANDBOX_RUNTIME_ENV_BY_KEY["runner_mode"])


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
    runtime_env: dict[str, str],
    *,
    call_environment: ToolEnvironment,
    base_process_env: dict[str, str] | None = None,
    shell_path_prepend: str | None = None,
    workspace_dir: Path | None = None,
    process_environ: Mapping[str, str] | None = None,
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
    env.update(call_environment.applied(runtime_env, base_process_env or {}))
    for name in call_environment.removed:
        env.pop(name, None)
    workspace_home_contract = (
        _workspace_home_contract_env_from_process_env(base_process_env) if base_process_env is not None else {}
    )
    env.update(workspace_home_contract)
    if workspace_dir is not None and not workspace_home_contract:
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
        ConfigField(
            name="enable_run_shell_command",
            label="Enable Run Shell Command",
            type="boolean",
            required=False,
            default=True,
        ),
        ConfigField(
            name="all",
            label="All",
            type="boolean",
            required=False,
            default=False,
        ),
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
    managed_init_args=(ToolManagedInitArg.RUNTIME_PATHS, ToolManagedInitArg.TOOL_OUTPUT_WORKSPACE_ROOT),
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
        ) -> None:
            self.base_dir: Path | None = Path(base_dir) if isinstance(base_dir, str) else base_dir
            # Beside a workdir, ``base_dir`` is the workdir; ``$MINDROOM_AGENT_WORKSPACE`` stays the workspace.
            self._beside_workdir = current_tool_workdir() is not None
            self._workspace_dir = tool_output_workspace_root if self._beside_workdir else self.base_dir
            self._refuse_commands = shell_refuses_commands(runtime_paths, allow_without_worker=allow_without_worker)
            # Whether calls may name a worker of their own, which the primary then routes them to.
            self._named_workers = named_workers
            self._in_worker = runtime_paths.env_flag(SANDBOX_RUNTIME_ENV_BY_KEY["runner_mode"])

            tools: list[Callable[..., object]] = []
            if all or enable_run_shell_command:
                tools.append(self.run_shell_command)

            super().__init__(name="shell_tools", tools=tools)

            self._runtime_env = dict(
                shell_execution_runtime_env_values(
                    runtime_paths,
                    extra_env_passthrough=extra_env_passthrough,
                    process_env=runtime_paths.process_env,
                ),
            )
            self._base_process_env = dict(runtime_paths.process_env)
            self._shell_path_prepend = shell_path_prepend
            # Sandbox tool subprocesses delegate execution to the runner's long-lived shell supervisor,
            # which stops the command when the per-request process dies.
            self._supervisor_socket = os.environ.get(SHELL_SUPERVISOR_SOCKET_ENV) or None

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

        def _command_env(self, process_environ: Mapping[str, str] | None = None) -> dict[str, str]:
            """Return the environment a command runs with, from ``process_environ`` (by default this process's)."""
            return _shell_subprocess_env(
                self._runtime_env,
                # The call's own environment: plugin hooks' changes and the response's CLI grant.
                call_environment=current_tool_environment(),
                base_process_env=self._base_process_env,
                shell_path_prepend=self._shell_path_prepend,
                workspace_dir=self._workspace_dir,
                process_environ=process_environ,
            )

        def terminal_launch(
            self,
            cwd: str | None,
            *,
            process_environ: Mapping[str, str] | None,
        ) -> TerminalLaunch:
            """Return an interactive Bash in the environment and workspace this toolkit's commands get.

            Not a function the agent calls: the sandbox runner starts it in a pty beside the commands it runs. It
            reads no startup files, so it starts with exactly that environment, and no readline settings, so it
            starts with Bash's built-in key bindings. ``process_environ`` is the environment of the process a
            command runs from there, None for this one's.
            The shell marks each prompt, which ``terminal_run`` waits for (``mindroom.terminal.lines``).
            """
            env = self._command_env(process_environ)
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
            if timeout is not None and (not isinstance(timeout, int | float) or timeout <= 0):
                msg = "timeout must be a positive number of seconds; omit it for the default limit."
                raise ValueError(msg)
            env = self._command_env(process_environ)
            argv = _shell_subprocess_args(_normalize_shell_args(call["args"]), env)
            launch = TerminalLaunch(argv=tuple(argv), env=env, cwd=str(self.base_dir) if self.base_dir else None)
            return launch, shell_call_timeout(timeout)

        def _refusal(self, *, named_worker: bool) -> str | None:
            """Return why a call cannot run here, where ``named_worker`` says it names a worker of its own."""
            if self._refuse_commands:
                return _NO_WORKER_ERROR
            # The primary routes a call to the worker it names, so one that reaches a shell outside a worker has none.
            if named_worker and not self._in_worker:
                return _NO_NAMED_WORKERS_ERROR
            return None

        async def run_shell_command(
            self,
            args: list[str] | str,
            tail: int = 100,
            timeout: int | None = None,  # noqa: ASYNC109
            worker: str | None = None,
            resource_profile: ScriptResourceProfileName | None = None,
            sandbox: bool | None = None,
            mount_dir: str | None = None,
            mount_read_only: bool | None = None,
        ) -> str | ToolOutputFileHandled:
            """Runs a shell command and returns the output or error.

            The last ``tail`` lines of stdout, then of stderr, are returned; on
            non-zero exit, all of stderr is kept. A command still running after
            ``timeout`` seconds is stopped with the processes it started, and its
            output so far is returned. Without ``timeout``, a call that runs as part
            of a background job has no time limit; that includes every call that
            accepts ``background_after`` and, with background jobs on, a subagent's
            calls for its delegated task, whether or not its caller waits for it. A
            call that accepts ``background_after`` and leaves it unset returns a job
            handle after 120 seconds while its command runs on. Any other command
            stops after 120 seconds. Set ``timeout`` on a command that might not
            end on its own, such as a watcher or a test run that can hang. Start a
            server or anything else meant to keep running with
            ``nohup cmd > out.log 2>&1 &``, which keeps running after the command
            ends; in the foreground it would keep the call running until its job is
            cancelled. Wait for something inside one command, such as
            ``until <check>; do sleep 10; done`` with a ``timeout``, not with
            repeated ``sleep`` calls: every call is a model turn.

            With ``mindroom_output_path``, capture the complete supported output
            within the redirect byte limit instead of applying ``tail``; a stopped
            command saves no file.

            Command strings use non-login Bash with the prepared execution environment.
            Use explicit ``["bash", "-lc", command]`` only when login startup is needed.

            Args:
                args: The command to run as a shell command string or a list of argv strings.
                tail: The number of lines to return from the output.
                timeout: Seconds after which the command is stopped; null uses the default limit.
                worker: Name of a worker of its own to run the command in, 1 to 32 lowercase letters, digits, or
                    hyphens; null runs it in your normal worker.
                resource_profile: CPU and memory size of the named worker, small, standard, or large, set by the
                    first call that names it; null gives it the normal worker size.
                sandbox: Run the named worker in isolated sandbox mode (ephemeral scratch only unless mount_dir is
                    set, without credentials, Agent Keys, env secrets, or mindroom-agent CLI grants).
                mount_dir: Workspace-relative subdirectory to mount inside the sandboxed named worker (e.g.
                    "scratch/job1"; workspace root "." and hidden dot-prefixed segments like ".mindroom" or ".git" are
                    refused). Always enables sandbox mode. Writable mount_dir still allows modifying repo-local files
                    such as .git/hooks if your normal worker later runs git there.
                mount_read_only: Mount mount_dir read-only; requires mount_dir.

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
                return refusal
            try:
                command_args = _normalize_shell_args(args)
            except ValueError as exc:
                if worker is not None:
                    raise
                return f"Error: {exc}"
            if timeout is not None and timeout <= 0:
                # A model may mean "no limit" or "run in the background"; either way, stopping at once helps nobody.
                msg = "timeout must be a positive number of seconds; omit it for the default limit."
                if worker is not None:
                    raise ValueError(msg)
                return f"Error: {msg}"
            subprocess_env = self._command_env()
            argv = _shell_subprocess_args(command_args, subprocess_env)
            cwd = str(self.base_dir) if self.base_dir else None
            output_request = current_tool_output_file_request()
            output_destination = (
                ShellOutputDestination(
                    workspace_root=str(output_request.policy.workspace_root),
                    path=output_request.path.requested_path,
                    max_bytes=output_request.policy.max_bytes,
                    tools_use_workdir=output_request.policy.tools_use_workdir,
                )
                if output_request is not None and output_request.path is not None
                else None
            )
            stop_after = shell_call_timeout(timeout)
            if self._supervisor_socket is not None:
                result = await run_command_via_supervisor(
                    self._supervisor_socket,
                    namespace=_SHELL_NAMESPACE,
                    argv=argv,
                    env=subprocess_env,
                    cwd=cwd,
                    tail=tail,
                    timeout=stop_after,
                    on_timeout="kill",
                    on_progress=report_tool_progress,
                    output_destination=output_destination,
                    launch_failure_exits=worker is not None,
                )
            else:
                result = await run_command(
                    {},
                    namespace=_SHELL_NAMESPACE,
                    argv=argv,
                    env=subprocess_env,
                    cwd=cwd,
                    # Never the terminal MindRoom runs in, where a prompting command would wait forever.
                    stdin=subprocess.DEVNULL,
                    tail=tail,
                    timeout=stop_after,
                    on_timeout="kill",
                    on_progress=report_tool_progress,
                    output_destination=output_destination,
                    launch_failure_exits=worker is not None,
                )
            if worker is not None and result.execution_error:
                raise ValueError(result.message)
            return ToolOutputFileHandled(result.message) if result.output_file_handled else result.message

    return MindRoomShellTools
