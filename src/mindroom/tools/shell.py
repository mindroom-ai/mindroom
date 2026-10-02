"""Shell tool configuration with async subprocess execution and timeout-to-handle support."""

from __future__ import annotations

import json
import os
import re
import shlex
from dataclasses import dataclass
from pathlib import Path
from typing import Protocol, cast, runtime_checkable

from agno.tools.toolkit import Toolkit

from mindroom.constants import (
    WORKSPACE_HOME_CONTRACT_ENV_NAMES,
    RuntimePaths,
    shell_execution_runtime_env_values,
    subprocess_path_with_prepends,
    workspace_home_identity_env,
)
from mindroom.shell_execution import (
    DEFAULT_RUN_TIMEOUT_SECONDS,
    ProcessRecord,
    check_command,
    kill_command,
    kill_namespace_records,
    run_command,
)
from mindroom.shell_output_capture import ShellOutputDestination
from mindroom.shell_supervisor import (
    SHELL_SUPERVISOR_SOCKET_ENV,
    check_command_via_supervisor,
    kill_command_via_supervisor,
    run_command_via_supervisor,
)
from mindroom.tool_system.declarations import (
    ConfigField,
    SetupType,
    ToolCategory,
    ToolExecutionTarget,
    ToolFileAccess,
    ToolManagedInitArg,
    ToolStatus,
)
from mindroom.tool_system.output_files import ToolOutputFileHandled, current_tool_output_file_request
from mindroom.tool_system.registration import register_tool_with_metadata
from mindroom.vendor_telemetry import vendor_telemetry_env_values


@runtime_checkable
class ShellRuntimeSettings(Protocol):
    """Non-secret effective settings retained by the canonical shell toolkit."""

    @property
    def shell_path_prepend(self) -> str | None:
        """Return the resolved path entries from the normal configuration merge."""
        ...

    @property
    def extra_env_passthrough(self) -> str | None:
        """Return the resolved extra env passthrough patterns from the normal configuration merge."""
        ...


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
    "The command runs in your agent workspace as its working directory "
    "(echoed as a `[cwd: ...]` first line in the result). Always use relative paths or "
    "`$MINDROOM_AGENT_WORKSPACE` for workspace files instead of `~`: worker-routed execution maps `~` "
    "to the workspace, while local execution maps it to the host home."
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

# Module-level process registry shared across all MindRoomShellTools instances.
# This ensures handles survive toolkit re-creation for local execution; when a
# supervisor socket is advertised, the supervisor owns the registry instead.
_process_registry: dict[str, ProcessRecord] = {}


def retire_local_shell_namespace(namespace: str) -> None:
    """Stop this process's background commands in one namespace, as retiring a worker stops its own."""
    kill_namespace_records(_process_registry, namespace)


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
    base_process_env: dict[str, str] | None = None,
    shell_path_prepend: str | None = None,
    workspace_dir: Path | None = None,
) -> dict[str, str]:
    """Build the env passed to shell subprocesses."""
    env = {key: value for key, value in os.environ.items() if key in _LOCAL_SHELL_PASSTHROUGH_ENV_KEYS}
    if base_process_env is not None:
        env.update({key: value for key, value in base_process_env.items() if key in _LOCAL_SHELL_PASSTHROUGH_ENV_KEYS})
    env.update(runtime_env)
    workspace_home_contract = (
        _workspace_home_contract_env_from_process_env(base_process_env) if base_process_env is not None else {}
    )
    env.update(workspace_home_contract)
    if workspace_dir is not None and not workspace_home_contract:
        # Local (non-worker) execution keeps the host HOME, but the workspace
        # env var is still promised to agents, so export it from base_dir.
        env["MINDROOM_AGENT_WORKSPACE"] = str(workspace_dir.expanduser().resolve())

    path_value = subprocess_path_with_prepends(
        env.get("PATH"),
        prepend_entries=_shell_path_prepend_entries(shell_path_prepend),
    )
    if path_value is None:
        env.pop("PATH", None)
    else:
        env["PATH"] = path_value
    env.update(vendor_telemetry_env_values())
    return env


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


def _handle_namespace(*, runtime_paths: RuntimePaths, base_dir: Path | None) -> str:
    """Return the namespace that owns one shell handle registry record."""
    storage_root = str(runtime_paths.storage_root.resolve())
    resolved_base_dir = str(base_dir.expanduser().resolve()) if base_dir is not None else ""
    return f"{storage_root}::{resolved_base_dir}"


@dataclass(frozen=True, slots=True)
class AgentCliShellBinding:
    """Trusted minimal-mode shell binding, never provider tool arguments.

    A worker runs commands through its supervisor socket with an owner-reserved
    handle; the primary runs them in its own registry, so both are optional.
    """

    socket_path: str | None
    namespace: str
    handle: str | None
    api_url: str
    token_path: str


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
    managed_init_args=(ToolManagedInitArg.RUNTIME_PATHS,),
    dependencies=[],
    docs_url="https://docs.agno.com/tools/toolkits/local/shell",
    function_names=("check_shell_command", "kill_shell_command", "run_shell_command"),
)
def shell_tools() -> type[Toolkit]:  # noqa: C901
    """Return shell tools for command execution."""

    class MindRoomShellTools(Toolkit):
        """MindRoom shell toolkit with async execution and timeout-to-handle support."""

        @property
        def shell_path_prepend(self) -> str | None:
            """Expose the effective non-secret path setting after normal config merge."""
            return self._shell_path_prepend

        @property
        def extra_env_passthrough(self) -> str | None:
            """Expose the effective passthrough patterns after normal config merge."""
            return self._extra_env_passthrough

        def __init__(
            self,
            base_dir: Path | str | None = None,
            enable_run_shell_command: bool = True,
            all: bool = False,  # noqa: A002
            extra_env_passthrough: str | None = None,
            shell_path_prepend: str | None = None,
            *,
            runtime_paths: RuntimePaths,
            agent_cli_binding: AgentCliShellBinding | None = None,
            **kwargs: object,
        ) -> None:
            self.base_dir: Path | None = Path(base_dir) if isinstance(base_dir, str) else base_dir

            tools: list[object] = []
            if all or enable_run_shell_command:
                tools.extend([self.run_shell_command, self.check_shell_command, self.kill_shell_command])

            super().__init__(name="shell_tools", tools=tools, **kwargs)  # ty: ignore[invalid-argument-type]
            run_shell_command_function = self.async_functions.get("run_shell_command")
            if run_shell_command_function is not None:
                effective_strict = (
                    False if run_shell_command_function.strict is None else run_shell_command_function.strict
                )
                run_shell_command_function.process_entrypoint(strict=effective_strict)

            self._runtime_env = dict(
                shell_execution_runtime_env_values(
                    runtime_paths,
                    extra_env_passthrough=extra_env_passthrough,
                    process_env=runtime_paths.process_env,
                ),
            )
            self._base_process_env = dict(runtime_paths.process_env)
            self._extra_env_passthrough = extra_env_passthrough
            if run_shell_command_function is not None:
                notes = (
                    (_WORKSPACE_CWD_NOTE, _WORKING_METHOD_NOTE)
                    if self.base_dir is not None
                    else (_WORKING_METHOD_NOTE,)
                )
                run_shell_command_function.description = "\n\n".join(
                    (run_shell_command_function.description or "", *notes),
                ).strip()
            self._handle_namespace = _handle_namespace(runtime_paths=runtime_paths, base_dir=self.base_dir)
            self._shell_path_prepend = shell_path_prepend
            # Sandbox tool subprocesses delegate execution to the runner's
            # long-lived shell supervisor so background handles survive the
            # per-request process.
            self._supervisor_socket = os.environ.get(SHELL_SUPERVISOR_SOCKET_ENV) or None
            self._agent_cli_binding = agent_cli_binding
            if agent_cli_binding is not None:
                self._supervisor_socket = agent_cli_binding.socket_path
                self._handle_namespace = agent_cli_binding.namespace
                self._runtime_env.update(
                    {
                        "MINDROOM_AGENT_CLI_URL": agent_cli_binding.api_url,
                        "MINDROOM_AGENT_CLI_TOKEN_PATH": agent_cli_binding.token_path,
                    },
                )

        async def run_shell_command(
            self,
            args: list[str] | str,
            tail: int = 100,
            timeout: int = DEFAULT_RUN_TIMEOUT_SECONDS,  # noqa: ASYNC109
        ) -> str | ToolOutputFileHandled:
            """Runs a shell command and returns the output or error.

            If the command completes within ``timeout`` seconds the last ``tail``
            lines of stdout are returned. On non-zero exit, useful stdout is
            preserved together with stderr. When the timeout is exceeded, the
            process keeps running in the background
            and a handle string is returned that can be polled with
            ``check_shell_command`` or stopped with ``kill_shell_command``.

            With ``mindroom_output_path``, capture the complete supported output
            within the redirect byte limit instead of applying ``tail``. If the
            command backgrounds, it saves to that destination when it finishes;
            ``check_shell_command`` then returns the file receipt.

            Command strings use non-login Bash with the prepared execution environment.
            Use explicit ``["bash", "-lc", command]`` only when login startup is needed.

            Args:
                args: The command to run as a shell command string or a list of argv strings.
                tail: The number of lines to return from the output.
                timeout: Maximum seconds to wait before backgrounding the command.

            Returns:
                The command output, an error message, or a background handle.

            """
            try:
                command_args = _normalize_shell_args(args)
            except ValueError as exc:
                return f"Error: {exc}"
            subprocess_env = _shell_subprocess_env(
                self._runtime_env,
                base_process_env=self._base_process_env,
                shell_path_prepend=self._shell_path_prepend,
                workspace_dir=self.base_dir,
            )
            argv = _shell_subprocess_args(command_args, subprocess_env)
            cwd = str(self.base_dir) if self.base_dir else None
            output_request = current_tool_output_file_request()
            output_destination = (
                ShellOutputDestination(
                    workspace_root=str(output_request.policy.workspace_root),
                    path=output_request.path.requested_path,
                    max_bytes=output_request.policy.max_bytes,
                )
                if output_request is not None and output_request.path is not None
                else None
            )
            if self._supervisor_socket is not None:
                result = await run_command_via_supervisor(
                    self._supervisor_socket,
                    namespace=self._handle_namespace,
                    argv=argv,
                    env=subprocess_env,
                    cwd=cwd,
                    tail=tail,
                    timeout=timeout,
                    handle=self._agent_cli_binding.handle if self._agent_cli_binding is not None else None,
                    output_destination=output_destination,
                )
            else:
                result = await run_command(
                    _process_registry,
                    namespace=self._handle_namespace,
                    argv=argv,
                    env=subprocess_env,
                    cwd=cwd,
                    tail=tail,
                    timeout=timeout,
                    output_destination=output_destination,
                )
            message = result.message
            if result.output_file_handled:
                return ToolOutputFileHandled(message)
            if cwd is None:
                return message
            return f"[cwd: {cwd}]\n{message}"

        def check_shell_command(self, handle: str) -> str:
            """Poll the status of a backgrounded shell command.

            Safe to call multiple times — the record is kept until automatic
            cleanup (~10 min after finish). Use ``kill_shell_command`` to stop
            a running process.

            Args:
                handle: The ``shell:...`` identifier from the ``Handle:`` line returned by ``run_shell_command``.

            Returns:
                Output if the command finished, or a status summary if still running.

            """
            if self._supervisor_socket is not None:
                return check_command_via_supervisor(
                    self._supervisor_socket,
                    namespace=self._handle_namespace,
                    handle=handle,
                )
            return check_command(_process_registry, namespace=self._handle_namespace, handle=handle)

        def kill_shell_command(self, handle: str, force: bool = False) -> str:
            """Kill a backgrounded shell command.

            Args:
                handle: The ``shell:...`` identifier from the ``Handle:`` line returned by ``run_shell_command``.
                force: If True send SIGKILL immediately instead of SIGTERM.

            Returns:
                Confirmation message or error.

            """
            if self._supervisor_socket is not None:
                return kill_command_via_supervisor(
                    self._supervisor_socket,
                    namespace=self._handle_namespace,
                    handle=handle,
                    force=force,
                )
            return kill_command(_process_registry, namespace=self._handle_namespace, handle=handle, force=force)

    return MindRoomShellTools
