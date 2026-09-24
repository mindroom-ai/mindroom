"""Narrow primary-authenticated CLI worker launch and shell messages."""

from __future__ import annotations

from typing import Literal, get_args
from urllib.parse import urlsplit

from pydantic import BaseModel, ConfigDict, Field, SecretStr, field_validator

from mindroom.workers.compatibility import WORKER_PROTOCOL_VERSION

CLI_PRIVATE_ROOT_PATH = "/app/.mindroom-agent-cli"
_ShellOperationName = Literal["run_shell_command", "check_shell_command", "kill_shell_command"]
SHELL_OPERATION_NAMES: tuple[_ShellOperationName, ...] = get_args(_ShellOperationName)


def safe_origin(value: str) -> str:
    """Require a concrete HTTP origin, without credential, query or path injection."""
    parsed = urlsplit(value)
    if (
        parsed.scheme not in {"http", "https"}
        or not parsed.hostname
        or parsed.username is not None
        or parsed.password is not None
        or parsed.path not in {"", "/"}
        or parsed.query
        or parsed.fragment
    ):
        msg = "CLI network endpoints require safe HTTP(S) origins"
        raise ValueError(msg)
    _ = parsed.port
    return value.rstrip("/")


class CliShellSettings(BaseModel):
    """Trusted effective shell settings; no provider credential or generic env map."""

    model_config = ConfigDict(extra="forbid", frozen=True)
    workspace: str = Field(min_length=1, max_length=4096)
    shell_path_prepend: str | None = Field(max_length=4096)
    output_max_bytes: int = Field(gt=0)
    output_auto_save_threshold_bytes: int = Field(ge=0)


class CliWorkerLaunch(BaseModel):
    """Single-use grant installation on an already acquired physical worker."""

    model_config = ConfigDict(extra="forbid", frozen=True)
    protocol_version: int
    worker_key: str = Field(min_length=1, max_length=1024)
    state_scope_worker_key: str = Field(min_length=1, max_length=1024)
    private_agent_names: list[str] = Field(max_length=128)
    turn_id: str = Field(min_length=1, max_length=1024)
    generation: str = Field(min_length=1, max_length=1024)
    token: SecretStr = Field(min_length=16, max_length=4096)
    gateway_url: str = Field(max_length=2048)
    primary_url: str = Field(max_length=2048)
    control_urls: list[str] = Field(max_length=256)
    shell: CliShellSettings

    @field_validator("protocol_version")
    @classmethod
    def current_protocol(cls, value: int) -> int:
        """Reject launch attempts from a different worker generation."""
        if value != WORKER_PROTOCOL_VERSION:
            msg = "Incompatible CLI worker protocol"
            raise ValueError(msg)
        return value

    @field_validator("gateway_url", "primary_url")
    @classmethod
    def origin(cls, value: str) -> str:
        """Normalize only safe, fixed upstream origins."""
        return safe_origin(value)

    @field_validator("control_urls")
    @classmethod
    def control_origins(cls, values: list[str]) -> list[str]:
        """Normalize independently protected control origins."""
        return [safe_origin(value) for value in values]


class CliShellOperation(BaseModel):
    """Allow only canonical shell functions; their actual Functions validate arguments."""

    model_config = ConfigDict(extra="allow", frozen=True)
    function_name: _ShellOperationName


class CliShellRequest(BaseModel):
    """An owner-reserved supervisor handle and one canonical shell operation."""

    model_config = ConfigDict(extra="forbid", frozen=True)
    worker_key: str
    handle: str = Field(pattern=r"^shell:[0-9a-f]{32}$")
    operation: CliShellOperation
