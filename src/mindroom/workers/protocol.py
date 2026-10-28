"""The requests and responses the primary and its sandbox runners exchange, shared by both sides.

Every model refuses fields it does not declare, and every request names the protocol it speaks, so a
primary and a worker from different releases fail with an error instead of silently dropping what one
of them added.
"""

from __future__ import annotations

from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field

from mindroom.tool_system.worker_routing import WorkerScope  # noqa: TC001 - pydantic resolves it at runtime

# 2: workers mount only agent workspaces and never write private-instance identity records.
# 3: /execute streams progress and keep-alive lines before its result line, and job calls have no time limit.
# 4: a shell call's set and removed variables apply over the worker's own environment and workspace script.
# 5: a shell call's PATH directories go first on the command's PATH.
# 6: requests and responses are these typed models, which refuse unknown fields, and each request names its
#    protocol, so mixed releases fail instead of dropping fields; a request names its workspace in `base_dir`.
WORKER_PROTOCOL_VERSION = 6

FailureKind = Literal["tool", "worker"]


class _WireModel(BaseModel):
    model_config = ConfigDict(extra="forbid")


class WorkerRoute(_WireModel):
    """Which worker a request reaches and on whose behalf, carried by every routed request.

    ``execution_identity`` is written by ``serialize_tool_execution_identity`` and read back by
    ``parse_tool_execution_identity_payload``. ``config_snapshot`` holds the live config fields runners resolve,
    for runners that mount no config. ``base_dir`` is the calling agent's tool workspace, relative to the shared
    storage root when the request names a worker, which the worker confines before acting in it.
    """

    protocol: int
    worker_key: str | None = None
    worker_scope: WorkerScope | None = None
    routing_agent_name: str | None = None
    execution_identity: dict[str, Any] | None = None
    private_agent_names: list[str] | None = None
    config_snapshot: dict[str, Any] | None = None
    base_dir: str | None = None


class CallEnvironment(_WireModel):
    """How one shell call's own environment changes the worker's, beyond the values sent in ``execution_env``.

    ``set_names`` are the ``execution_env`` variables the call set itself, from plugin hooks or a CLI grant, and
    ``unset_names`` the variables its hooks removed; both apply over the worker's own environment and the
    workspace's ``.mindroom/worker-env.sh``, except where the worker's own composition changed the name (its
    protected names, HOME, its CA bundle and its Agent Vault settings). ``path_prepend`` holds the directories the
    call puts first on the command's ``PATH``.
    """

    set_names: list[str] = Field(default_factory=list)
    unset_names: list[str] = Field(default_factory=list)
    path_prepend: list[str] = Field(default_factory=list)


class ExecuteRequest(WorkerRoute):
    """One tool call, with credentials by lease and the environment execution tools receive."""

    tool_name: str
    function_name: str
    args: list[Any] = Field(default_factory=list)
    kwargs: dict[str, Any] = Field(default_factory=dict)
    lease_id: str | None = None
    tool_config_overrides: dict[str, Any] = Field(default_factory=dict)
    tool_init_overrides: dict[str, Any] = Field(default_factory=dict)
    execution_env: dict[str, str] = Field(default_factory=dict)
    environment: CallEnvironment = Field(default_factory=CallEnvironment)
    extra_env_passthrough: str | None = None
    # Lets the primary stop this call through /execute/cancel when it stops waiting for it.
    request_id: str | None = Field(default=None, max_length=64)
    # A background job's call runs as long as its tool does, and its tools know they run as part of a job.
    run_as_job: bool = False


class SaveAttachmentRequest(WorkerRoute):
    """Attachment bytes to write at one path inside the request's workspace."""

    attachment_id: str
    mindroom_output_path: str
    bytes_b64: str
    sha256: str
    size_bytes: int


class ViewFileRequest(WorkerRoute):
    """One image path inside the request's workspace."""

    path: str


class LeaseRequest(_WireModel):
    """Credentials for one upcoming tool call, held by the worker until the call consumes them."""

    protocol: int
    tool_name: str
    function_name: str
    credential_overrides: dict[str, Any] = Field(default_factory=dict)
    ttl_seconds: int
    max_uses: int = 1


class CancelRequest(_WireModel):
    """Stop one execute request the primary no longer waits for."""

    protocol: int
    request_id: str = Field(max_length=64)


class WorkerFailure(_WireModel):
    """A request the worker refused or could not finish, and whether the worker itself is at fault."""

    ok: Literal[False] = False
    error: str
    failure_kind: FailureKind


class ExecuteResponse(_WireModel):
    """The result of one tool call, or why it failed."""

    ok: bool
    result: Any | None = None
    error: str | None = None
    failure_kind: FailureKind | None = None


class SaveAttachmentResponse(_WireModel):
    """Where the bytes landed and the receipt the primary checks against what it sent."""

    ok: bool
    worker_path: str | None = None
    size_bytes: int | None = None
    sha256: str | None = None
    error: str | None = None
    failure_kind: FailureKind | None = None


class ViewFileResponse(_WireModel):
    """The bounded image a worker read, as an encoded media result."""

    ok: bool
    result: Any | None = None
    error: str | None = None
    failure_kind: FailureKind | None = None


class LeaseResponse(_WireModel):
    """The lease a later tool call names to receive its credentials."""

    lease_id: str
    expires_at: float
    max_uses: int


class CancelResponse(_WireModel):
    """Whether the request was running when its cancel arrived."""

    cancelled: bool
