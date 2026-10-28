"""Build sandbox-runner execute calls for tests that drive the runner below its HTTP endpoints."""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

from mindroom.api import sandbox_runner
from mindroom.tool_system.worker_routing import parse_tool_execution_identity_payload
from mindroom.workers.protocol import WORKER_PROTOCOL_VERSION, ExecuteRequest, WorkerFailure

if TYPE_CHECKING:
    from mindroom.api.sandbox_worker_prep import PreparedWorkerRequest
    from mindroom.config.main import Config
    from mindroom.constants import RuntimePaths


def execute_call(
    runtime_paths: RuntimePaths,
    config: Config | None = None,
    *,
    prepared_worker: PreparedWorkerRequest | None = None,
    credential_overrides: dict[str, Any] | None = None,
    runner_token: str | None = None,
    **fields: Any,  # noqa: ANN401
) -> sandbox_runner._ExecuteCall:
    """Return one execute call whose route is prepared like the endpoint's, or bound to ``prepared_worker``."""
    config = config if config is not None else sandbox_runner._runtime_config_or_empty(runtime_paths)
    request = ExecuteRequest(protocol=WORKER_PROTOCOL_VERSION, **fields)
    if prepared_worker is None:
        route = sandbox_runner._prepare_route_with_config(
            request,
            runtime_paths=runtime_paths,
            config=config,
            runner_token=runner_token,
        )
        assert not isinstance(route, WorkerFailure), route
    else:
        route = sandbox_runner._PreparedRoute(
            runtime_paths=runtime_paths,
            config=config,
            worker_key=prepared_worker.handle.worker_key,
            identity=(
                parse_tool_execution_identity_payload(request.execution_identity)
                if request.execution_identity is not None
                else None
            ),
            worker=prepared_worker,
            workspace=prepared_worker.base_dir,
        )
    return sandbox_runner._ExecuteCall(request=request, route=route, credential_overrides=credential_overrides or {})
