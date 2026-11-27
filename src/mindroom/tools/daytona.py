"""Daytona tool configuration and native implementation."""

from __future__ import annotations

import base64
import json
import posixpath
import shlex
from typing import Any, Literal

import httpx

from mindroom.logging_config import get_logger
from mindroom.tool_system.authoring import Toolkit
from mindroom.tool_system.declarations import (
    ConfigField,
    SetupType,
    ToolCategory,
    ToolFileAccess,
    ToolStatus,
    bool_config_field,
)
from mindroom.tool_system.registration import register_tool_with_metadata
from mindroom.tools.native_helpers import UNSET, prepare_python_code, require_non_empty, resolve_bool_toggle
from mindroom.tools.string_mapping import parse_string_mapping

logger = get_logger(__name__)

_DaytonaRunAction = Literal["code", "shell"]
_DaytonaInspectAction = Literal["read_file", "list_files"]
_DaytonaFilesAction = Literal["create_file", "delete_file", "change_directory"]
_VALID_LANGUAGES = frozenset({"python", "javascript", "typescript"})
_DEFAULT_API_URL = "https://app.daytona.io/api"
_DEFAULT_INSTRUCTIONS = (
    "You have a Daytona remote sandbox that keeps its working directory across calls in this reply. "
    "Execute code in the sandbox and report the actual output."
)


def _normalize_language(value: str | None) -> str | None:
    if value is None:
        return None
    normalized = str(value).strip().lower()
    if not normalized:
        return None
    if normalized not in _VALID_LANGUAGES:
        msg = f"Unsupported Daytona sandbox_language {value!r}; expected one of: {', '.join(sorted(_VALID_LANGUAGES))}"
        raise ValueError(msg)
    return normalized


def _error_json(message: str) -> str:
    return json.dumps({"status": "error", "message": message})


class _DaytonaTools(Toolkit):
    """Execute code, shell commands, and file operations in a remote Daytona sandbox."""

    def __init__(
        self,
        api_key: str | None = None,
        api_url: str | None = None,
        sandbox_id: str | None = None,
        sandbox_language: str | None = None,
        sandbox_target: str | None = None,
        sandbox_os: str | None = None,
        auto_stop_interval: int | None = 60,
        sandbox_os_user: str | None = None,
        sandbox_env_vars: dict[str, str] | str | None = None,
        sandbox_labels: dict[str, str] | str | None = None,
        sandbox_public: bool | None = None,
        organization_id: str | None = None,
        timeout: int = 300,
        auto_create_sandbox: bool | None = UNSET,
        verify_ssl: bool | None = True,
        persistent: bool | None = UNSET,
        instructions: str | None = None,
    ) -> None:
        if not isinstance(api_key, str) or not api_key.strip():
            msg = "Daytona API key is required."
            raise ValueError(msg)

        self.api_key = api_key.strip()
        self.api_url = api_url.strip() if isinstance(api_url, str) and api_url.strip() else None
        self.sandbox_id = sandbox_id.strip() if isinstance(sandbox_id, str) and sandbox_id.strip() else None
        self.sandbox_language = _normalize_language(sandbox_language)
        self.sandbox_target = (
            sandbox_target.strip() if isinstance(sandbox_target, str) and sandbox_target.strip() else None
        )
        self.sandbox_os = sandbox_os
        self.auto_stop_interval = auto_stop_interval
        self.sandbox_os_user = sandbox_os_user
        self.sandbox_env_vars = parse_string_mapping(sandbox_env_vars, field_name="sandbox_env_vars")
        self.sandbox_labels = parse_string_mapping(sandbox_labels, field_name="sandbox_labels")
        self.sandbox_public = sandbox_public
        self.organization_id = (
            organization_id.strip() if isinstance(organization_id, str) and organization_id.strip() else None
        )
        self.timeout = timeout
        self.auto_create_sandbox = resolve_bool_toggle(auto_create_sandbox, default=True)
        # Keep TLS certificate verification enabled unless explicitly disabled with False.
        self.verify_ssl = verify_ssl is not False
        self.persistent = resolve_bool_toggle(persistent, default=True)

        self._client: Any | None = None
        self._working_directory = "/home/daytona"
        self._persistent_sandbox_id: str | None = None

        resolved_instructions = (
            instructions.strip() if isinstance(instructions, str) and instructions.strip() else _DEFAULT_INSTRUCTIONS
        )
        super().__init__(
            name="daytona_tools",
            instructions=resolved_instructions,
            tools=[self.daytona_run, self.daytona_inspect, self.daytona_files],
        )

    def _reject_ambient_daytona_env(self) -> None:
        """Fail closed before Daytona() starts if ambient env/.env values would leak into unset fields."""
        from daytona._utils.env import DaytonaEnvReader

        env_reader = DaytonaEnvReader()
        leaked: list[str] = []
        if self.sandbox_target is None and env_reader.get("DAYTONA_TARGET"):
            leaked.append("DAYTONA_TARGET")
        if self.organization_id is None and env_reader.get("DAYTONA_ORGANIZATION_ID"):
            leaked.append("DAYTONA_ORGANIZATION_ID")
        if self.sandbox_target is None and env_reader.get("DAYTONA_JWT_TOKEN"):
            leaked.append("DAYTONA_JWT_TOKEN")
        if leaked:
            msg = (
                f"Ambient {', '.join(leaked)} detected while corresponding Daytona tool config is unset; "
                "configure the field explicitly or unset the ambient variable."
            )
            raise ValueError(msg)

    def _get_client(self) -> Any:  # noqa: ANN401 - optional daytona.Daytona client
        if self._client is None:
            from daytona import Daytona, DaytonaConfig

            self._reject_ambient_daytona_env()
            config = DaytonaConfig(
                api_key=self.api_key,
                api_url=self.api_url or _DEFAULT_API_URL,
                target=self.sandbox_target,
                organization_id=self.organization_id,
                use_deprecated_polling=False,
                otel_enabled=False,
            )
            client = Daytona(config)
            if not self.verify_ssl:
                self._disable_client_tls_verification(client)
            self._client = client
        return self._client

    @staticmethod
    def _disable_client_tls_verification(client: Any) -> None:  # noqa: ANN401 - optional daytona client
        """Disable TLS verification on this single Daytona client instance only."""
        from daytona_api_client.rest import RESTClientObject

        api_client = getattr(client, "_api_client", None)
        if api_client is not None:
            api_client.configuration.verify_ssl = False
            api_client.rest_client = RESTClientObject(api_client.configuration)
            if hasattr(client, "_clone_api_client_to_toolbox_api_client"):
                client._toolbox_api_client = client._clone_api_client_to_toolbox_api_client()
        old_http = getattr(client, "_http_client", None)
        if isinstance(old_http, httpx.Client):
            timeout = old_http.timeout
            old_http.close()
            client._http_client = httpx.Client(verify=False, timeout=timeout)  # noqa: S501 - explicit verify_ssl=False

    def _create_sandbox(self) -> Any:  # noqa: ANN401 - optional daytona.Sandbox
        from daytona import CodeLanguage, CreateSandboxFromSnapshotParams

        client = self._get_client()
        language = CodeLanguage(self.sandbox_language or "python")
        labels = dict(self.sandbox_labels or {})
        labels.setdefault("created_by", "mindroom_daytona_toolkit")
        labels.setdefault("language", str(language.value))
        if self.persistent:
            labels.setdefault("persistent", "true")
        params = CreateSandboxFromSnapshotParams(
            language=language,
            os_user=self.sandbox_os_user,
            env_vars=self.sandbox_env_vars,
            auto_stop_interval=self.auto_stop_interval,
            labels=labels,
            public=self.sandbox_public,
        )
        sandbox = client.create(params, timeout=self.timeout)
        if self.persistent:
            self._persistent_sandbox_id = sandbox.id
        return sandbox

    def _get_or_create_sandbox(self) -> Any:  # noqa: ANN401 - optional daytona.Sandbox
        client = self._get_client()
        try:
            sandbox = None
            target_id = self.sandbox_id or (self._persistent_sandbox_id if self.persistent else None)
            if target_id:
                try:
                    sandbox = client.get(target_id)
                except Exception as exc:
                    logger.debug("daytona_get_sandbox_failed", sandbox_id=target_id, error=str(exc))
                    sandbox = None
            if sandbox is None:
                sandbox = self._create_sandbox()
            if getattr(sandbox, "state", None) != "started":
                client.start(sandbox, timeout=self.timeout)
        except Exception:
            if self.auto_create_sandbox:
                return self._create_sandbox()
            raise
        else:
            return sandbox

    def _resolve_sandbox_path(self, raw_path: str) -> str:
        if raw_path.startswith("/"):
            return posixpath.normpath(raw_path)
        return posixpath.normpath(posixpath.join(self._working_directory, raw_path))

    def _change_dir(self, sandbox: Any, raw_dir: str) -> str:  # noqa: ANN401
        target = self._resolve_sandbox_path(raw_dir)
        check = sandbox.process.exec(
            f"test -d {shlex.quote(target)} && echo 'exists' || echo 'not found'",
            cwd="/",
        )
        if "exists" in str(check.result):
            self._working_directory = target
            return f"Changed directory to: {target}"
        return f"Error: Directory {target} not found"

    def daytona_run(
        self,
        action: _DaytonaRunAction,
        code: str | None = None,
        command: str | None = None,
    ) -> str:
        """Run code or a shell command in the remote Daytona sandbox.

        Actions:
        - `code`: execute `code` in the configured sandbox language (default Python).
        - `shell`: execute bash `command` in the sandbox's current working directory (standalone `cd <dir>` updates cwd).
        """
        try:
            sandbox = self._get_or_create_sandbox()
            if action == "code":
                source = require_non_empty(code, "code")
                if (self.sandbox_language or "python") == "python":
                    source = prepare_python_code(source)
                return str(sandbox.process.code_run(source).result)
            if action == "shell":
                cmd = require_non_empty(command, "command")
                if cmd.startswith("cd ") and "&&" not in cmd and ";" not in cmd and "|" not in cmd:
                    return self._change_dir(sandbox, cmd[3:].strip())
                return str(sandbox.process.exec(cmd, cwd=self._working_directory).result)
        except Exception as exc:
            return _error_json(f"Error in daytona_run ({action}): {exc}")
        return _error_json(f"Unsupported action: {action}")

    def daytona_inspect(
        self,
        action: _DaytonaInspectAction,
        path: str | None = None,
    ) -> str:
        """Read-only inspection of files and directories in the Daytona sandbox.

        Actions:
        - `read_file`: read contents of `path` (relative to working directory or absolute).
        - `list_files`: list entries in `path` (defaults to current working directory).
        """
        try:
            sandbox = self._get_or_create_sandbox()
            if action == "read_file":
                target = self._resolve_sandbox_path(require_non_empty(path, "path"))
                result = sandbox.process.exec(f"cat {shlex.quote(target)}")
                if result.exit_code != 0:
                    return _error_json(f"Error reading file: {result.result}")
                return str(result.result)
            if action == "list_files":
                target = self._resolve_sandbox_path(path) if path else self._working_directory
                result = sandbox.process.exec(f"ls -la {shlex.quote(target)}")
                if result.exit_code != 0:
                    return _error_json(f"Error listing directory: {result.result}")
                return f"Contents of {target}:\n{result.result}"
        except Exception as exc:
            return _error_json(f"Error in daytona_inspect ({action}): {exc}")
        return _error_json(f"Unsupported action: {action}")

    def daytona_files(  # noqa: PLR0911
        self,
        action: _DaytonaFilesAction,
        path: str,
        content: str | None = None,
    ) -> str:
        """Create/overwrite files, delete files/directories, or change working directory in the Daytona sandbox.

        Actions:
        - `create_file`: write `content` to `path` (creating parent directories as needed).
        - `delete_file`: remove file or directory at `path`.
        - `change_directory`: switch per-reply working directory to `path`.
        """
        try:
            sandbox = self._get_or_create_sandbox()
            raw_path = require_non_empty(path, "path")
            if action == "change_directory":
                return self._change_dir(sandbox, raw_path)
            target = self._resolve_sandbox_path(raw_path)
            if action == "create_file":
                if content is None:
                    return _error_json("Error creating file: content is required")
                parent = posixpath.dirname(target)
                if parent and parent != "/":
                    mkdir_res = sandbox.process.exec(f"mkdir -p {shlex.quote(parent)}")
                    if mkdir_res.exit_code != 0:
                        return _error_json(f"Failed to create directory: {mkdir_res.result}")
                encoded = base64.b64encode(content.encode("utf-8")).decode("ascii")
                write_res = sandbox.process.exec(
                    f"printf %s {shlex.quote(encoded)} | base64 -d > {shlex.quote(target)}",
                )
                if write_res.exit_code != 0:
                    return _error_json(f"Failed to create file: {write_res.result}")
                return f"File created/updated: {target}"
            if action == "delete_file":
                check = sandbox.process.exec(
                    f"test -d {shlex.quote(target)} && echo 'directory' || echo 'file'",
                )
                flag = "-rf" if "directory" in str(check.result) else "-f"
                rm_res = sandbox.process.exec(f"rm {flag} {shlex.quote(target)}")
                if rm_res.exit_code != 0:
                    return _error_json(f"Failed to delete: {rm_res.result}")
                return f"Deleted: {target}"
        except Exception as exc:
            return _error_json(f"Error in daytona_files ({action}): {exc}")
        return _error_json(f"Unsupported action: {action}")


@register_tool_with_metadata(
    name="daytona",
    file_access=ToolFileAccess.NONE,
    requires_primary_runtime=True,
    display_name="Daytona",
    description="Execute code in secure, remote sandbox environments",
    category=ToolCategory.DEVELOPMENT,
    status=ToolStatus.REQUIRES_CONFIG,
    setup_type=SetupType.API_KEY,
    icon="FaTerminal",
    icon_color="text-blue-600",
    config_fields=[
        ConfigField(
            name="api_key",
            env=("DAYTONA_API_KEY",),
            label="API Key",
            type="password",
            required=False,
            placeholder="dt_...",
            description="Daytona API key",
        ),
        ConfigField(
            name="api_url",
            env=("DAYTONA_API_URL",),
            label="API URL",
            type="url",
            required=False,
            placeholder="https://api.daytona.io",
            description="Daytona API URL",
        ),
        ConfigField(
            name="sandbox_id",
            label="Sandbox ID",
            type="text",
            required=False,
            placeholder="sandbox-123",
            description="Specific sandbox ID to use. If None, creates or uses persistent sandbox",
        ),
        ConfigField(
            name="sandbox_language",
            label="Sandbox Language",
            type="text",
            required=False,
            default="python",
            placeholder="python",
            description="Primary language for the sandbox (python, javascript, typescript)",
        ),
        ConfigField(
            name="sandbox_target",
            label="Sandbox Target",
            type="text",
            required=False,
            placeholder="target-config",
            description="Target configuration for the sandbox",
        ),
        ConfigField(
            name="sandbox_os",
            label="Sandbox OS",
            type="text",
            required=False,
            placeholder="ubuntu-20.04",
            description="Operating system for the sandbox",
        ),
        ConfigField(
            name="auto_stop_interval",
            label="Auto Stop Interval",
            type="number",
            required=False,
            default=60,
            description="Auto-stop interval in minutes (0 to disable)",
        ),
        ConfigField(
            name="sandbox_os_user",
            label="Sandbox OS User",
            type="text",
            required=False,
            placeholder="daytona",
            description="OS user for the sandbox",
        ),
        ConfigField(
            name="sandbox_env_vars",
            label="Sandbox Environment Variables",
            type="password",
            required=False,
            placeholder='{"ENV_VAR": "value"}',
            description="Environment variables for the sandbox (JSON format)",
        ),
        ConfigField(
            name="sandbox_labels",
            label="Sandbox Labels",
            type="text",
            required=False,
            default="{}",
            placeholder='{"label": "value"}',
            description="Labels for the sandbox (JSON format)",
        ),
        ConfigField(
            name="organization_id",
            label="Organization ID",
            type="text",
            required=False,
            placeholder="org-123",
            description="Organization ID for the sandbox",
        ),
        ConfigField(
            name="timeout",
            label="Timeout",
            type="number",
            required=False,
            default=300,
            description="Timeout for sandbox operations in seconds",
        ),
        bool_config_field(
            "auto_create_sandbox",
            "Auto Create Sandbox",
            description="Create a fallback sandbox after a sandbox-management error",
        ),
        bool_config_field("verify_ssl", "Verify SSL", description="Whether to verify SSL certificates"),
        bool_config_field(
            "persistent",
            "Persistent",
            description="Whether to reuse the same sandbox across calls within one reply",
        ),
        bool_config_field(
            "sandbox_public",
            "Sandbox Public",
            default=None,
            description="Whether the sandbox should be public",
        ),
        ConfigField(
            name="instructions",
            label="Instructions",
            type="text",
            required=False,
            placeholder="Custom guidelines for the toolkit",
            description="Custom instructions for the toolkit",
        ),
    ],
    dependencies=["daytona"],
    docs_url="https://docs.mindroom.chat/tools/automation-and-platforms/#daytona",
    function_names=("daytona_files", "daytona_inspect", "daytona_run"),
    action_scoped_grants=("daytona_files", "daytona_inspect", "daytona_run"),
)
def daytona_tools() -> type[Toolkit]:
    """Return Daytona tools for secure code execution in remote sandbox environments."""
    return _DaytonaTools
