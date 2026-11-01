# ruff: noqa: D100
from __future__ import annotations

import asyncio
import threading
from contextlib import AbstractAsyncContextManager, asynccontextmanager, suppress
from dataclasses import asdict, dataclass
from functools import partial
from typing import TYPE_CHECKING, Annotated, Any, Literal

from fastapi import Depends, FastAPI, Header, Request
from fastapi.exceptions import RequestValidationError
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse, Response
from pydantic import BaseModel, ConfigDict

from mindroom import constants
from mindroom.agent_policy import PolicyInputs, build_agent_policy_seeds, resolve_agent_policy_index
from mindroom.api import config_lifecycle
from mindroom.api.agent_cli import bind_agent_cli_registry
from mindroom.api.agent_cli import router as agent_cli_router
from mindroom.api.app_state import EmbeddedRuntime, ExternalTriggerRuntime, ensure_app_state
from mindroom.api.auth import authenticate_user, dashboard_cors_origins, verify_user
from mindroom.api.auth import router as auth_router
from mindroom.api.auth_user import AuthUser  # noqa: TC001 - FastAPI resolves route annotations.
from mindroom.api.budgets import router as budgets_router
from mindroom.api.computers import active_computer_worker_keys, withdraw_computer_runtime
from mindroom.api.computers import router as computers_router
from mindroom.api.config_lifecycle import ApiSnapshot, ApiState
from mindroom.api.config_reload import router as config_reload_router
from mindroom.api.config_schema import router as config_schema_router
from mindroom.api.connections import router as connections_router
from mindroom.api.connections_session import router as connections_session_router

# Import routers
from mindroom.api.credentials import router as credentials_router
from mindroom.api.dynamic_workflows import router as dynamic_workflows_router
from mindroom.api.egress_broker import router as egress_broker_router
from mindroom.api.egress_credentials import router as egress_credentials_router
from mindroom.api.external_triggers import router as external_triggers_router
from mindroom.api.frontend import router as frontend_router
from mindroom.api.homeassistant_integration import router as homeassistant_router
from mindroom.api.knowledge import router as knowledge_router
from mindroom.api.matrix_operations import router as matrix_router
from mindroom.api.mcp_gateway import gateway_cors_origins, gateway_lifespan, install_gateway_routes
from mindroom.api.oauth import router as oauth_router
from mindroom.api.openai_compat import router as openai_compat_router
from mindroom.api.provider_setup import router as provider_setup_router
from mindroom.api.report_publishing import public_router as report_publishing_public_router
from mindroom.api.request_body_limit import RequestBodyLimitMiddleware
from mindroom.api.response_activity import router as response_activity_router
from mindroom.api.schedules import router as schedules_router
from mindroom.api.script_gateway import bind_script_tool_broker
from mindroom.api.script_gateway import router as script_gateway_router
from mindroom.api.skills import router as skills_router
from mindroom.api.thread_exports import router as thread_exports_router
from mindroom.api.tools import router as tools_router
from mindroom.api.usage import router as usage_router
from mindroom.api.usage_export import close_usage_export_runner
from mindroom.api.validation_errors import validation_error_response
from mindroom.api.workers import maintain_leased_workers
from mindroom.api.workers import router as workers_router
from mindroom.config.commit import ConfigCommit
from mindroom.config.personal_agents import personal_agent_data
from mindroom.embeddings.embedder_health import get_embedder_failure
from mindroom.integrations import integration_routers
from mindroom.knowledge_graph.store import knowledge_graph_failure
from mindroom.logging_config import get_logger
from mindroom.matrix.decrypt_failure import e2ee_stats
from mindroom.matrix.health import get_matrix_sync_health_snapshot
from mindroom.orchestration.runtime import matrix_ingestion_grace_seconds, matrix_sync_startup_timeout_seconds
from mindroom.origins import allowed_client_origins
from mindroom.personal_agents import read_personal_agent_user_ids
from mindroom.runtime_env_policy import COMPUTER_ALLOWED_ORIGINS_ENV
from mindroom.runtime_state import get_runtime_state
from mindroom.workers.runtime import (
    lease_configured_primary_worker_manager,
)

if TYPE_CHECKING:
    from collections.abc import AsyncIterator, Awaitable, Callable

    from starlette.types import ASGIApp, Message, Receive, Scope, Send

    from mindroom.config.main import Config
    from mindroom.external_triggers.store import TriggerDeliverySnapshot
    from mindroom.workers.backend import WorkerBackend

logger = get_logger(__name__)
_WORKER_CLEANUP_INTERVAL_ENV = "MINDROOM_WORKER_CLEANUP_INTERVAL_SECONDS"
_DASHBOARD_CORS_EXPOSE_HEADERS = (
    config_lifecycle.CONFIG_GENERATION_HEADER,
    config_lifecycle.CONFIG_USES_INCLUDES_HEADER,
    config_lifecycle.CONFIG_PENDING_RESTART_HEADER,
)


@dataclass(frozen=True)
class _DashboardCorsSettings:
    """Dashboard CORS settings derived from the runtime environment."""

    allow_origins: tuple[str, ...]
    allow_credentials: bool
    expose_headers: tuple[str, ...] = _DASHBOARD_CORS_EXPOSE_HEADERS


class _RuntimeDashboardCorsMiddleware:
    """Apply dashboard CORS settings from the app's current runtime context."""

    def __init__(
        self,
        app: ASGIApp,
        *,
        api_app: FastAPI,
        fallback_runtime_paths: constants.RuntimePaths,
    ) -> None:
        self.app = app
        self.api_app = api_app
        self.fallback_runtime_paths = fallback_runtime_paths
        self._middleware_by_settings: dict[_DashboardCorsSettings, CORSMiddleware] = {}

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        middleware = self._middleware_for_current_runtime(scope.get("path", ""))
        if scope.get("path", "").startswith("/api/computers"):

            async def no_store_send(message: Message) -> None:
                if message["type"] == "http.response.start":
                    message["headers"] = [
                        (key, value) for key, value in message.get("headers", []) if key.lower() != b"cache-control"
                    ]
                    message["headers"].append((b"cache-control", b"no-store"))
                await send(message)

            await middleware(scope, receive, no_store_send)
        else:
            await middleware(scope, receive, send)

    def _middleware_for_current_runtime(self, path: str) -> CORSMiddleware:
        paths = self._current_runtime_paths()
        origins = (
            allowed_client_origins(paths, COMPUTER_ALLOWED_ORIGINS_ENV)
            if path.startswith("/api/computers")
            else gateway_cors_origins(paths, path)
        )
        settings = (
            _DashboardCorsSettings(origins, allow_credentials=False, expose_headers=("WWW-Authenticate",))
            if origins is not None
            else _dashboard_cors_settings(paths)
        )
        middleware = self._middleware_by_settings.get(settings)
        if middleware is None:
            middleware = CORSMiddleware(
                self.app,
                allow_origins=list(settings.allow_origins),
                allow_credentials=settings.allow_credentials,
                allow_methods=["*"],
                allow_headers=["*"],
                expose_headers=list(settings.expose_headers),
            )
            self._middleware_by_settings[settings] = middleware
        return middleware

    def _current_runtime_paths(self) -> constants.RuntimePaths:
        try:
            return _app_runtime_paths(self.api_app)
        except TypeError:
            return self.fallback_runtime_paths


class DraftAgentPolicyDefaultsRequest(BaseModel):
    """Subset of config defaults required to preview derived agent policy."""

    model_config = ConfigDict(extra="ignore")

    worker_scope: Literal["shared", "user", "user_agent"] | None = None


class AgentPoliciesRequest(BaseModel):
    """Payload for deriving draft agent policies from the current editor state."""

    model_config = ConfigDict(extra="ignore")

    defaults: DraftAgentPolicyDefaultsRequest | None = None
    agents: dict[str, PolicyInputs]


class RawConfigSourceRequest(BaseModel):
    """Payload for raw config source recovery edits."""

    source: str


class PersonalAgentResponse(BaseModel):
    """One roster entry's generated agent, which the authored config the dashboard edits leaves out."""

    template: str
    user_id: str
    agent_name: str
    matrix_user_id: str
    display_name: str | None
    pending: bool
    template_exists: bool


def _worker_cleanup_interval_seconds(runtime_paths: constants.RuntimePaths) -> float:
    """Return the configured background idle-worker cleanup interval."""
    raw = runtime_paths.env_value(_WORKER_CLEANUP_INTERVAL_ENV, default="0")
    try:
        interval = float(raw)
    except ValueError:
        return 0.0
    return max(0.0, interval)


def _cleanup_workers_once(
    runtime_paths: constants.RuntimePaths,
    *,
    touch_live_workers: Callable[[WorkerBackend], None],
    computer_worker_keys: frozenset[str] = frozenset(),
) -> int:
    """Run one worker maintenance pass when a backend is configured; return how many idle workers it cleaned."""
    worker_lease = lease_configured_primary_worker_manager(runtime_paths)
    if worker_lease is None:
        return 0
    with worker_lease as worker_manager:
        maintenance = maintain_leased_workers(
            worker_manager,
            touch_live_workers=touch_live_workers,
            computer_worker_keys=computer_worker_keys,
        )
    if maintenance.cleaned:
        logger.info(
            "Cleaned idle workers",
            count=len(maintenance.cleaned),
            backend=worker_manager.backend_name,
        )
    if maintenance.reconciled:
        logger.info(
            "Reconciled drifted worker pod templates",
            count=len(maintenance.reconciled),
            backend=worker_manager.backend_name,
        )
    return len(maintenance.cleaned)


async def _worker_cleanup_loop(
    stop_event: asyncio.Event,
    api_app: FastAPI,
    *,
    touch_live_workers: Callable[[WorkerBackend], None],
    idle_poll_interval_seconds: float = 1.0,
) -> None:
    """Periodically clean idle workers using the app's current runtime paths."""
    while not stop_event.is_set():
        runtime_paths = _app_runtime_paths(api_app)
        interval_seconds = _worker_cleanup_interval_seconds(runtime_paths)
        if interval_seconds <= 0:
            try:
                await asyncio.wait_for(stop_event.wait(), timeout=idle_poll_interval_seconds)
                break
            except TimeoutError:
                continue
        try:
            await asyncio.wait_for(stop_event.wait(), timeout=interval_seconds)
            break
        except TimeoutError:
            try:
                await asyncio.to_thread(
                    _cleanup_workers_once,
                    _app_runtime_paths(api_app),
                    touch_live_workers=touch_live_workers,
                    computer_worker_keys=active_computer_worker_keys(api_app),
                )
            except Exception:
                logger.exception("Background worker cleanup failed")


def _app_context(api_app: FastAPI) -> ApiSnapshot:
    """Return the committed API snapshot for one app instance."""
    return config_lifecycle.require_api_state(api_app).snapshot


def _app_runtime_paths(api_app: FastAPI) -> constants.RuntimePaths:
    """Return the committed runtime paths for one API app instance."""
    return _app_context(api_app).runtime_paths


def initialize_api_app(api_app: FastAPI, runtime_paths: constants.RuntimePaths) -> None:
    """Bind one API app to a runtime's paths; per-generation bindings made for other paths end with them."""
    app_state = ensure_app_state(api_app)
    if app_state.api_state is None:
        app_state.api_state = ApiState(
            config_lock=threading.Lock(),
            snapshot=ApiSnapshot(generation=0, runtime_paths=runtime_paths),
        )
    elif config_lifecycle.publish_runtime_paths(app_state.api_state, runtime_paths):
        app_state.external_trigger_runtime = None
        withdraw_computer_runtime(api_app)
    config_lifecycle.register_api_app(api_app)


def bind_embedded_runtime(api_app: FastAPI, runtime: EmbeddedRuntime) -> None:
    """Lend the dashboard API what `mindroom run` shares with it, until `unbind_embedded_runtime`."""
    ensure_app_state(api_app).embedded = runtime
    bind_agent_cli_registry(api_app, runtime.agent_cli_registry)
    bind_script_tool_broker(api_app, runtime.script_broker)


def unbind_embedded_runtime(api_app: FastAPI) -> None:
    """Withdraw what the runtime lent, once its embedded server stopped serving."""
    bind_script_tool_broker(api_app, None)
    bind_agent_cli_registry(api_app, None)
    ensure_app_state(api_app).embedded = None


@asynccontextmanager
async def _lifespan(_app: FastAPI) -> AsyncIterator[None]:
    """Run the dashboard API's own background work while `mindroom run` serves it.

    The runtime has migrated storage, synced credentials, and published its config load before it serves this app,
    and it publishes every later load, so this app neither prepares storage nor loads or watches config itself.
    """
    app_state = config_lifecycle.app_state(_app)
    runtime = app_state.runtime
    logger.info(
        "Published knowledge index refresh is scheduled by Git polling, filesystem watch, on access, or explicit API actions",
    )

    stop_event = asyncio.Event()
    worker_cleanup = asyncio.create_task(
        _worker_cleanup_loop(stop_event, _app, touch_live_workers=runtime.script_worker_keepalive),
    )

    async with gateway_lifespan(_app):
        yield

    withdraw_computer_runtime(_app)
    stop_event.set()
    worker_cleanup.cancel()
    with suppress(asyncio.CancelledError):
        await worker_cleanup
    close_usage_export_runner(_app)


def bind_external_trigger_runtime(
    api_app: FastAPI,
    client: object,
    conversation_reader: object,
    *,
    runtime_config: Config,
    is_trigger_snapshot_ready: Callable[[TriggerDeliverySnapshot], Awaitable[bool]],
    admission_fence: Callable[[], AbstractAsyncContextManager[None]],
) -> None:
    """Attach router Matrix delivery runtime to one API app while it serves the config the bots run.

    The trigger endpoint authorizes against the API snapshot and the receiving
    bot enforces its own config, so while a dashboard save the runtime has not
    applied yet is published, delivery stays unavailable; the reload that
    applies the save binds again.
    """
    snapshot = config_lifecycle.require_api_state(api_app).snapshot
    app_state = config_lifecycle.app_state(api_app)
    if snapshot.runtime_config is None or snapshot.runtime_config.model_dump() != runtime_config.model_dump():
        app_state.external_trigger_runtime = None
        return
    app_state.external_trigger_runtime = ExternalTriggerRuntime(
        client=client,
        conversation_reader=conversation_reader,
        config_generation=snapshot.generation,
        is_trigger_snapshot_ready=is_trigger_snapshot_ready,
        admission_fence=admission_fence,
    )


def unbind_external_trigger_runtime(api_app: FastAPI) -> None:
    """Clear router Matrix delivery runtime from one API app."""
    config_lifecycle.app_state(api_app).external_trigger_runtime = None


def _api_docs_kwargs(runtime_paths: constants.RuntimePaths) -> dict[str, str | None]:
    """Return generated-docs routes for this runtime."""
    docs_enabled = runtime_paths.env_flag(
        "MINDROOM_ENABLE_API_DOCS",
        default=not bool(runtime_paths.env_value("MINDROOM_PLATFORM_LOGIN_URL")),
    )
    if not docs_enabled:
        return {"docs_url": None, "redoc_url": None, "openapi_url": None}
    return {"docs_url": "/docs", "redoc_url": "/redoc", "openapi_url": "/openapi.json"}


def _dashboard_cors_settings(runtime_paths: constants.RuntimePaths) -> _DashboardCorsSettings:
    """Return dashboard CORS settings for one runtime context; credentials are never allowed with `*`."""
    origins = dashboard_cors_origins(runtime_paths)
    return _DashboardCorsSettings(allow_origins=origins, allow_credentials="*" not in origins)


def _add_dashboard_cors_middleware(api_app: FastAPI, runtime_paths: constants.RuntimePaths) -> None:
    """Add dashboard CORS middleware without wildcard credential defaults."""
    api_app.add_middleware(
        _RuntimeDashboardCorsMiddleware,
        api_app=api_app,
        fallback_runtime_paths=runtime_paths,
    )


_runtime_paths = constants.resolve_primary_runtime_paths()
_api_docs = _api_docs_kwargs(_runtime_paths)
app = FastAPI(
    title="MindRoom Dashboard API",
    lifespan=_lifespan,
    docs_url=_api_docs["docs_url"],
    redoc_url=_api_docs["redoc_url"],
    openapi_url=_api_docs["openapi_url"],
    exception_handlers={RequestValidationError: validation_error_response},
)
initialize_api_app(app, _runtime_paths)
# Added before CORS, so a 413 still carries the dashboard's CORS headers.
app.add_middleware(RequestBodyLimitMiddleware)
_add_dashboard_cors_middleware(app, _runtime_paths)


def _set_config_generation_header(response: Response, generation: int) -> None:
    """Attach the committed config generation to one API response."""
    response.headers[config_lifecycle.CONFIG_GENERATION_HEADER] = str(generation)


async def _commit_dashboard_save(
    request: Request,
    response: Response,
    build: Callable[[constants.RuntimePaths], ConfigCommit],
    expected_generation: int | None,
) -> dict[str, bool]:
    """Commit one dashboard save off the event loop, which validation would block, and attach its generation."""
    published = await asyncio.to_thread(
        config_lifecycle.commit_dashboard_config,
        request,
        build,
        expected_generation=expected_generation,
    )
    _set_config_generation_header(response, published.generation)
    return {"success": True}


# Include routers
app.include_router(auth_router)
app.include_router(connections_session_router)
app.include_router(connections_router)
app.include_router(egress_credentials_router)
install_gateway_routes(app)
app.include_router(credentials_router, dependencies=[Depends(verify_user)])
app.include_router(egress_broker_router, dependencies=[Depends(verify_user)])
app.include_router(provider_setup_router, dependencies=[Depends(verify_user)])
app.include_router(homeassistant_router, dependencies=[Depends(verify_user)])
app.include_router(matrix_router, dependencies=[Depends(verify_user)])
app.include_router(thread_exports_router, dependencies=[Depends(verify_user)])
app.include_router(response_activity_router)  # Aggregate operational probe, like health/readiness.
app.include_router(config_reload_router)  # Requires its own operator bearer key.
app.include_router(config_schema_router, dependencies=[Depends(verify_user)])
app.include_router(oauth_router)
app.include_router(schedules_router, dependencies=[Depends(verify_user)])
app.include_router(knowledge_router, dependencies=[Depends(verify_user)])
app.include_router(skills_router, dependencies=[Depends(verify_user)])
app.include_router(tools_router, dependencies=[Depends(verify_user)])
app.include_router(usage_router)  # Routes require dashboard, signed personal, or dedicated service authentication.
app.include_router(budgets_router)  # Routes require dashboard or dedicated budget-service authentication.
app.include_router(workers_router, dependencies=[Depends(verify_user)])
app.include_router(openai_compat_router)  # Uses its own bearer auth, not verify_user
app.include_router(report_publishing_public_router)
app.include_router(external_triggers_router)
for integration_router in integration_routers():
    app.include_router(integration_router)  # Each integration authenticates its own requests.
app.include_router(computers_router)
app.include_router(script_gateway_router)
app.include_router(agent_cli_router)
# Each report admits the user who requested its run, administrator or not.
app.include_router(dynamic_workflows_router, dependencies=[Depends(authenticate_user)])


@app.get("/api/health")
async def health_check(request: Request) -> JSONResponse:
    """Health check endpoint with Matrix sync-loop liveness."""
    runtime_state = get_runtime_state()
    runtime_paths = config_lifecycle.api_runtime_paths(request)
    sync_health = get_matrix_sync_health_snapshot(
        startup_grace_seconds=matrix_sync_startup_timeout_seconds(runtime_paths),
        ingestion_grace_seconds=matrix_ingestion_grace_seconds(runtime_paths),
    )

    response: dict[str, object] = {
        "status": "healthy",
        "last_sync_time": sync_health.last_sync_time.isoformat() if sync_health.last_sync_time is not None else None,
        "e2ee": e2ee_stats().as_dict(),
    }
    if sync_health.stale_entities:
        response["stale_sync_entities"] = list(sync_health.stale_entities)

    embedder_failure = get_embedder_failure()
    if embedder_failure is not None:
        # Additive diagnostic only: a broken embedder degrades semantic search
        # but must not flip liveness and restart an otherwise usable runtime.
        response["embedder"] = {"status": "failing", "detail": embedder_failure}
    if (knowledge_graph_failure_detail := knowledge_graph_failure()) is not None:
        # Additive diagnostic only, like the embedder block: a failing graph must not flip liveness.
        response["knowledge_graph"] = {"status": "failing", "detail": knowledge_graph_failure_detail}

    if runtime_state.phase == "ready" and not sync_health.is_healthy:
        response["status"] = "unhealthy"
        return JSONResponse(status_code=503, content=response)

    return JSONResponse(content=response)


@app.get("/api/ready")
async def readiness_check() -> JSONResponse:
    """Readiness endpoint tied to successful orchestrator startup."""
    state = get_runtime_state()
    if state.phase == "ready":
        return JSONResponse({"status": "ready"})
    return JSONResponse(
        status_code=503,
        content={"status": state.phase, "detail": state.detail or "MindRoom is not ready"},
    )


@app.post("/api/config/load")
async def load_config(
    request: Request,
    response: Response,
    _user: Annotated[AuthUser, Depends(verify_user)],
) -> dict[str, Any]:
    """Return the committed configuration as authored, with its generation and state headers."""
    generation = config_lifecycle.committed_generation(request)
    config, _ = config_lifecycle.read_committed_runtime_config(request)
    _set_config_generation_header(response, generation)
    # The payload is the config itself, so the includes flag rides in a header
    # like the generation does, and so does the notice that the saved
    # event_journal is not the one this process has open.
    response.headers[config_lifecycle.CONFIG_USES_INCLUDES_HEADER] = (
        "true" if config_lifecycle.config_uses_includes(request) else "false"
    )
    response.headers[config_lifecycle.CONFIG_PENDING_RESTART_HEADER] = (
        "true" if config_lifecycle.config_pending_restart(request) else "false"
    )
    return config.authored_model_dump()


@app.put("/api/config/save")
async def save_config(
    request: Request,
    response: Response,
    new_config: dict[str, Any],
    _user: Annotated[AuthUser, Depends(verify_user)],
    x_mindroom_config_generation: Annotated[int | None, Header()] = None,
) -> dict[str, bool]:
    """Save configuration to file."""
    build = partial(ConfigCommit.from_payload, new_config)
    return await _commit_dashboard_save(request, response, build, x_mindroom_config_generation)


@app.get("/api/config/raw")
async def get_raw_config_source(
    request: Request,
    response: Response,
    _user: Annotated[AuthUser, Depends(verify_user)],
) -> dict[str, Any]:
    """Return the raw config source text for recovery editing."""
    generation = config_lifecycle.committed_generation(request)
    payload: dict[str, Any] = {
        "source": config_lifecycle.read_raw_config_source(request),
        "uses_includes": config_lifecycle.config_uses_includes(request),
    }
    _set_config_generation_header(response, generation)
    return payload


@app.put("/api/config/raw")
async def save_raw_config_source(
    request: Request,
    response: Response,
    payload: RawConfigSourceRequest,
    _user: Annotated[AuthUser, Depends(verify_user)],
    x_mindroom_config_generation: Annotated[int | None, Header()] = None,
) -> dict[str, bool]:
    """Replace the raw config source text after validating it against the active runtime."""
    build = partial(ConfigCommit.from_source, payload.source)
    return await _commit_dashboard_save(request, response, build, x_mindroom_config_generation)


@app.post("/api/config/agent-policies")
async def get_agent_policies(
    payload: AgentPoliciesRequest,
    _user: Annotated[AuthUser, Depends(verify_user)],
) -> dict[str, dict[str, dict[str, Any]]]:
    """Return backend-derived policies for the current draft agent config."""
    default_worker_scope = payload.defaults.worker_scope if payload.defaults is not None else None
    policy_index = resolve_agent_policy_index(
        build_agent_policy_seeds(payload.agents, default_worker_scope=default_worker_scope),
    )
    return {
        "agent_policies": {
            agent_name: {
                **asdict(policy),
                "team_eligibility_reason": policy_index.team_eligibility_reasons[agent_name],
            }
            for agent_name, policy in policy_index.policies.items()
        },
    }


@app.get("/api/config/personal-agents")
async def get_personal_agents(
    request: Request,
    _user: Annotated[AuthUser, Depends(verify_user)],
) -> list[PersonalAgentResponse]:
    """List every generated personal agent, including pending ones and those whose template was removed."""
    config, runtime_paths = config_lifecycle.read_committed_runtime_config(request)
    roster = await asyncio.to_thread(read_personal_agent_user_ids, runtime_paths)
    # The roster can be newer than the loaded config, so names render from the template, not config.agents.
    templates = {key: template.model_dump() for key, template in config.personal_agents.items()}
    return [
        PersonalAgentResponse(
            template=entry.template,
            user_id=entry.user_id,
            agent_name=entry.agent_name,
            matrix_user_id=matrix_user_id,
            display_name=(entry.edits.display_name if entry.edits is not None else None)
            or (
                personal_agent_data(templates[entry.template], entry).get("display_name")
                if entry.template in templates
                else None
            ),
            pending=entry.pending,
            template_exists=entry.template in templates,
        )
        for entry, matrix_user_id in roster
    ]


app.include_router(frontend_router)
