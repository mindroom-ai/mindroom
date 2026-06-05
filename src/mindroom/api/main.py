# ruff: noqa: D100
from __future__ import annotations

import asyncio
import threading
from contextlib import asynccontextmanager, suppress
from dataclasses import asdict, dataclass
from typing import TYPE_CHECKING, Annotated, Any, Literal
from urllib.parse import urlsplit

from fastapi import Depends, FastAPI, Header, HTTPException, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse, Response
from pydantic import BaseModel, ConfigDict, Field

from mindroom import constants
from mindroom.agent_policy import build_agent_policy_seeds, resolve_agent_policy_index
from mindroom.api import config_lifecycle
from mindroom.api.auth import ApiAuthState, verify_user  # noqa: F401
from mindroom.api.auth import router as auth_router
from mindroom.api.config_lifecycle import ApiSnapshot, ApiState, ConfigLoadResult  # noqa: F401

# Import routers
from mindroom.api.credentials import router as credentials_router
from mindroom.api.frontend import router as frontend_router
from mindroom.api.homeassistant_integration import router as homeassistant_router
from mindroom.api.integrations import router as integrations_router
from mindroom.api.knowledge import router as knowledge_router
from mindroom.api.matrix_operations import router as matrix_router
from mindroom.api.oauth import router as oauth_router
from mindroom.api.openai_compat import router as openai_compat_router
from mindroom.api.schedules import router as schedules_router
from mindroom.api.skills import router as skills_router
from mindroom.api.tools import router as tools_router
from mindroom.api.workers import router as workers_router
from mindroom.credentials_sync import sync_env_to_credentials
from mindroom.knowledge import KnowledgeRefreshScheduler, reconcile_knowledge_mode_transition_states
from mindroom.knowledge.watch import KnowledgeSourceWatcher
from mindroom.logging_config import get_logger
from mindroom.matrix.health import get_matrix_sync_health_snapshot
from mindroom.orchestration.runtime import matrix_sync_startup_timeout_seconds
from mindroom.runtime_state import get_runtime_state
from mindroom.tool_system.sandbox_proxy import sandbox_proxy_config
from mindroom.workers.runtime import (
    get_primary_worker_manager,
    primary_worker_backend_available,
    primary_worker_backend_name,
    serialized_kubernetes_worker_validation_snapshot,
)

if TYPE_CHECKING:
    from collections.abc import AsyncIterator
    from pathlib import Path

    from starlette.types import ASGIApp, Receive, Scope, Send

    from mindroom.config.main import Config

logger = get_logger(__name__)
_WORKER_CLEANUP_INTERVAL_ENV = "MINDROOM_WORKER_CLEANUP_INTERVAL_SECONDS"
_DASHBOARD_CORS_ALLOWED_ORIGINS_ENV = "MINDROOM_DASHBOARD_CORS_ALLOWED_ORIGINS"
_DASHBOARD_CORS_ALLOW_ALL_ORIGINS_ENV = "MINDROOM_DASHBOARD_CORS_ALLOW_ALL_ORIGINS"
_DASHBOARD_CORS_EXPOSE_HEADERS = (config_lifecycle.CONFIG_GENERATION_HEADER,)
_DEFAULT_DASHBOARD_CORS_ALLOWED_ORIGINS = (
    "http://localhost:3003",
    "http://localhost:5173",
    "http://127.0.0.1:3003",
    "http://127.0.0.1:5173",
)


@dataclass(frozen=True)
class _DashboardCorsSettings:
    """Dashboard CORS settings derived from the runtime environment."""

    allow_origins: tuple[str, ...]
    allow_credentials: bool


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
        middleware = self._middleware_for_current_runtime()
        await middleware(scope, receive, send)

    def _middleware_for_current_runtime(self) -> CORSMiddleware:
        settings = _dashboard_cors_settings(self._current_runtime_paths())
        middleware = self._middleware_by_settings.get(settings)
        if middleware is None:
            middleware = CORSMiddleware(
                self.app,
                allow_origins=list(settings.allow_origins),
                allow_credentials=settings.allow_credentials,
                allow_methods=["*"],
                allow_headers=["*"],
                expose_headers=list(_DASHBOARD_CORS_EXPOSE_HEADERS),
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


class DraftAgentPolicyKnowledgeRequest(BaseModel):
    """Subset of private knowledge config required to preview derived policy."""

    model_config = ConfigDict(extra="ignore")

    enabled: bool | None = None
    path: str | None = None


class DraftAgentPolicyPrivateRequest(BaseModel):
    """Subset of private config required to preview derived policy."""

    model_config = ConfigDict(extra="ignore")

    per: Literal["user", "user_agent"] | None = None
    knowledge: DraftAgentPolicyKnowledgeRequest | None = None


class DraftAgentPolicyAgentRequest(BaseModel):
    """Subset of agent config required to preview derived policy."""

    model_config = ConfigDict(extra="ignore")

    worker_scope: Literal["shared", "user", "user_agent"] | None = None
    private: DraftAgentPolicyPrivateRequest | None = None
    delegate_to: list[str] = Field(default_factory=list)


class AgentPoliciesRequest(BaseModel):
    """Payload for deriving draft agent policies from the current editor state."""

    model_config = ConfigDict(extra="ignore")

    defaults: DraftAgentPolicyDefaultsRequest | None = None
    agents: dict[str, DraftAgentPolicyAgentRequest]


class RawConfigSourceRequest(BaseModel):
    """Payload for raw config source recovery edits."""

    source: str


def _worker_cleanup_interval_seconds(runtime_paths: constants.RuntimePaths) -> float:
    """Return the configured background idle-worker cleanup interval."""
    raw = (runtime_paths.env_value(_WORKER_CLEANUP_INTERVAL_ENV, default="0") or "0").strip()
    try:
        interval = float(raw)
    except ValueError:
        return 0.0
    return max(0.0, interval)


def _cleanup_workers_once(
    runtime_paths: constants.RuntimePaths,
    *,
    runtime_config: Config | None = None,
    worker_grantable_credentials: frozenset[str] | None = None,
) -> int:
    """Run one idle-worker cleanup pass when a backend is configured."""
    proxy_config = sandbox_proxy_config(runtime_paths)
    if not primary_worker_backend_available(
        runtime_paths,
        proxy_url=proxy_config.proxy_url,
        proxy_token=proxy_config.proxy_token,
    ):
        return 0

    if runtime_config is None and primary_worker_backend_name(runtime_paths) == "kubernetes":
        return 0

    kubernetes_tool_validation_snapshot: dict[str, dict[str, object]] | None = None
    if runtime_config is not None and primary_worker_backend_name(runtime_paths) == "kubernetes":
        kubernetes_tool_validation_snapshot = serialized_kubernetes_worker_validation_snapshot(
            runtime_paths,
            runtime_config=runtime_config,
        )
        if worker_grantable_credentials is None:
            worker_grantable_credentials = runtime_config.get_worker_grantable_credentials()
    worker_manager = get_primary_worker_manager(
        runtime_paths,
        proxy_url=proxy_config.proxy_url,
        proxy_token=proxy_config.proxy_token,
        storage_root=runtime_paths.storage_root,
        kubernetes_tool_validation_snapshot=kubernetes_tool_validation_snapshot,
        worker_grantable_credentials=worker_grantable_credentials,
    )
    cleaned_workers = worker_manager.cleanup_idle_workers()
    if cleaned_workers:
        logger.info(
            "Cleaned idle workers",
            count=len(cleaned_workers),
            backend=worker_manager.backend_name,
        )
    return len(cleaned_workers)


async def _worker_cleanup_loop(
    stop_event: asyncio.Event,
    api_app: FastAPI,
    *,
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
                try:
                    runtime_config, runtime_paths = config_lifecycle.read_app_committed_runtime_config(api_app)
                except HTTPException:
                    runtime_config = None
                    runtime_paths = _app_runtime_paths(api_app)
                await asyncio.to_thread(
                    _cleanup_workers_once,
                    runtime_paths,
                    runtime_config=runtime_config,
                    worker_grantable_credentials=(
                        runtime_config.get_worker_grantable_credentials()
                        if runtime_config is not None
                        else constants.DEFAULT_WORKER_GRANTABLE_CREDENTIALS
                    ),
                )
            except Exception:
                logger.exception("Background worker cleanup failed")


def _api_runtime_paths(request: Request) -> constants.RuntimePaths:
    """Return the API request's committed runtime paths."""
    return config_lifecycle.api_runtime_paths(request)


def _app_context(api_app: FastAPI) -> ApiSnapshot:
    """Return the committed API snapshot for one app instance."""
    return config_lifecycle.require_api_state(api_app).snapshot


def _app_runtime_paths(api_app: FastAPI) -> constants.RuntimePaths:
    """Return the committed runtime paths for one API app instance."""
    return _app_context(api_app).runtime_paths


def initialize_api_app(api_app: FastAPI, runtime_paths: constants.RuntimePaths) -> None:
    """Initialize one API app instance with explicit runtime-bound state."""
    app_state = config_lifecycle.ensure_app_state(api_app)
    app_state.api_auth_account_id = runtime_paths.env_value("ACCOUNT_ID")
    previous_state = app_state.api_state
    if previous_state is None:
        app_state.api_state = ApiState(
            config_lock=threading.Lock(),
            snapshot=ApiSnapshot(
                generation=0,
                runtime_paths=runtime_paths,
                config_data={},
                runtime_config=None,
                config_load_result=None,
                auth_state=None,
            ),
        )
        config_lifecycle.register_api_app(api_app)
        return

    with previous_state.config_lock:
        current_snapshot = previous_state.snapshot
        auth_state = current_snapshot.auth_state if current_snapshot.runtime_paths == runtime_paths else None
        config_data = current_snapshot.config_data if current_snapshot.runtime_paths == runtime_paths else {}
        runtime_config = current_snapshot.runtime_config if current_snapshot.runtime_paths == runtime_paths else None
        config_load_result = (
            current_snapshot.config_load_result if current_snapshot.runtime_paths == runtime_paths else None
        )
        source_fingerprint = (
            current_snapshot.source_fingerprint if current_snapshot.runtime_paths == runtime_paths else None
        )
        previous_state.snapshot = config_lifecycle._published_snapshot(
            current_snapshot,
            runtime_paths=runtime_paths,
            config_data=config_data,
            runtime_config=runtime_config,
            auth_state=auth_state,
            config_load_result=config_load_result,
            source_fingerprint=source_fingerprint,
        )
    config_lifecycle.register_api_app(api_app)


async def _sync_standalone_knowledge_watchers(api_app: FastAPI) -> None:
    """Align API-owned knowledge filesystem watchers with the committed config."""
    source_watcher = config_lifecycle.app_state(api_app).knowledge_source_watcher
    if source_watcher is None:
        return
    snapshot = _app_context(api_app)
    await source_watcher.sync(config=snapshot.runtime_config, runtime_paths=snapshot.runtime_paths)


def _read_request_runtime_config_or_none(request: Request) -> tuple[Config, constants.RuntimePaths] | None:
    try:
        return config_lifecycle.read_committed_runtime_config(request)
    except HTTPException:
        return None


def _read_app_runtime_config_or_none(api_app: FastAPI) -> tuple[Config, constants.RuntimePaths] | None:
    try:
        return config_lifecycle.read_app_committed_runtime_config(api_app)
    except HTTPException:
        return None


def _reconcile_knowledge_mode_transitions(
    previous: tuple[Config, constants.RuntimePaths] | None,
    api_app: FastAPI,
) -> None:
    if previous is None:
        return
    previous_config, previous_runtime_paths = previous
    current_config, current_runtime_paths = config_lifecycle.read_app_committed_runtime_config(api_app)
    if current_runtime_paths != previous_runtime_paths:
        return
    reconcile_knowledge_mode_transition_states(previous_config, current_config, current_runtime_paths)


async def _reload_config_after_file_change(
    api_app: FastAPI,
    runtime_paths: constants.RuntimePaths,
) -> None:
    previous = _read_app_runtime_config_or_none(api_app)
    loaded = config_lifecycle.load_config_into_app(runtime_paths, api_app)
    if loaded:
        _reconcile_knowledge_mode_transitions(previous, api_app)
    await _sync_standalone_knowledge_watchers(api_app)


async def _watch_config(
    stop_event: asyncio.Event,
    api_app: FastAPI,
    *,
    poll_interval_seconds: float = 1.0,
) -> None:
    """Watch the current config file, rebinding automatically when runtime paths change."""
    watched_config_path: Path | None = None
    last_mtime = 0.0

    while not stop_event.is_set():
        runtime_paths = _app_runtime_paths(api_app)
        config_path = runtime_paths.config_path
        if config_path != watched_config_path:
            watched_config_path = config_path
            try:
                last_mtime = config_path.stat().st_mtime if config_path.exists() else 0.0
            except (OSError, PermissionError):
                last_mtime = 0.0

        try:
            await asyncio.wait_for(stop_event.wait(), timeout=poll_interval_seconds)
            break
        except TimeoutError:
            pass

        try:
            runtime_paths = _app_runtime_paths(api_app)
            config_path = runtime_paths.config_path
            if config_path != watched_config_path:
                watched_config_path = config_path
                try:
                    last_mtime = config_path.stat().st_mtime if config_path.exists() else 0.0
                except (OSError, PermissionError):
                    last_mtime = 0.0

            current_mtime = config_path.stat().st_mtime if config_path.exists() else 0.0
            if current_mtime != last_mtime:
                last_mtime = current_mtime
                logger.info("Config file changed", path=str(config_path))
                await _reload_config_after_file_change(api_app, runtime_paths)
        except (OSError, PermissionError):
            last_mtime = 0.0
        except Exception:
            logger.exception("Exception during file watcher callback - continuing to watch")


@asynccontextmanager
async def _lifespan(_app: FastAPI) -> AsyncIterator[None]:
    """Manage application startup and shutdown."""
    runtime_paths = _app_runtime_paths(_app)
    constants.ensure_writable_config_path(create_minimal=True, runtime_paths=runtime_paths)
    config_lifecycle.load_config_into_app(runtime_paths, _app)
    logger.info(
        "Initialized API runtime config",
        config_path=str(runtime_paths.config_path),
        config_exists=runtime_paths.config_path.exists(),
    )

    # Sync API keys from environment to CredentialsManager
    logger.info("Syncing API credentials from runtime env")
    sync_env_to_credentials(runtime_paths=runtime_paths)

    app_state = config_lifecycle.app_state(_app)
    api_owned_knowledge_refresh_scheduler: KnowledgeRefreshScheduler | None = None
    standalone_knowledge_source_watcher: KnowledgeSourceWatcher | None = None
    knowledge_refresh_scheduler = app_state.orchestrator_knowledge_refresh_scheduler
    if knowledge_refresh_scheduler is None:
        api_owned_knowledge_refresh_scheduler = KnowledgeRefreshScheduler()
        knowledge_refresh_scheduler = api_owned_knowledge_refresh_scheduler
        standalone_knowledge_source_watcher = KnowledgeSourceWatcher(knowledge_refresh_scheduler)
        app_state.knowledge_source_watcher = standalone_knowledge_source_watcher
    app_state.knowledge_refresh_scheduler = knowledge_refresh_scheduler
    await _sync_standalone_knowledge_watchers(_app)
    logger.info(
        "Published knowledge index refresh is scheduled by Git polling, filesystem watch, on access, or explicit API actions",
    )

    stop_event = asyncio.Event()
    watch_task = asyncio.create_task(_watch_config(stop_event, _app))
    worker_cleanup_task = asyncio.create_task(_worker_cleanup_loop(stop_event, _app))

    yield

    stop_event.set()
    watch_task.cancel()
    worker_cleanup_task.cancel()
    with suppress(asyncio.CancelledError):
        await watch_task
    with suppress(asyncio.CancelledError):
        await worker_cleanup_task
    if standalone_knowledge_source_watcher is not None:
        await standalone_knowledge_source_watcher.shutdown()
    if api_owned_knowledge_refresh_scheduler is not None:
        await api_owned_knowledge_refresh_scheduler.shutdown()


def bind_orchestrator_knowledge_refresh_scheduler(
    api_app: FastAPI,
    scheduler: KnowledgeRefreshScheduler,
) -> None:
    """Attach the orchestrator-owned background refresh scheduler to the bundled API app."""
    config_lifecycle.app_state(api_app).orchestrator_knowledge_refresh_scheduler = scheduler


def _api_docs_kwargs(runtime_paths: constants.RuntimePaths) -> dict[str, str | None]:
    """Return generated-docs routes for this runtime."""
    docs_enabled = runtime_paths.env_flag(
        "MINDROOM_ENABLE_API_DOCS",
        default=not bool(runtime_paths.env_value("MINDROOM_PLATFORM_LOGIN_URL")),
    )
    if not docs_enabled:
        return {"docs_url": None, "redoc_url": None, "openapi_url": None}
    return {"docs_url": "/docs", "redoc_url": "/redoc", "openapi_url": "/openapi.json"}


def _origin_from_url(value: str | None) -> str | None:
    if not value:
        return None
    parsed = urlsplit(value.strip())
    if not parsed.scheme or not parsed.netloc:
        return None
    return f"{parsed.scheme}://{parsed.netloc}"


def _api_cors_origins(runtime_paths: constants.RuntimePaths) -> list[str]:
    """Return hosted browser origins allowed to make credentialed API calls."""
    return list(
        dict.fromkeys(
            origin
            for origin in (
                _origin_from_url(runtime_paths.env_value("MINDROOM_PUBLIC_URL")),
                _origin_from_url(runtime_paths.env_value("MINDROOM_PLATFORM_LOGIN_URL")),
            )
            if origin is not None
        ),
    )


def _dashboard_cors_settings(runtime_paths: constants.RuntimePaths) -> _DashboardCorsSettings:
    """Return dashboard CORS settings for one runtime context."""
    if runtime_paths.env_flag(_DASHBOARD_CORS_ALLOW_ALL_ORIGINS_ENV):
        return _DashboardCorsSettings(allow_origins=("*",), allow_credentials=False)

    configured_origins = runtime_paths.env_value(_DASHBOARD_CORS_ALLOWED_ORIGINS_ENV)
    if configured_origins is None:
        hosted_origins = tuple(_api_cors_origins(runtime_paths))
        if hosted_origins:
            return _DashboardCorsSettings(allow_origins=hosted_origins, allow_credentials=True)

    origins = _parse_dashboard_cors_allowed_origins(configured_origins)
    return _DashboardCorsSettings(
        allow_origins=origins,
        allow_credentials="*" not in origins,
    )


def _parse_dashboard_cors_allowed_origins(configured_origins: str | None) -> tuple[str, ...]:
    """Parse a comma-separated dashboard CORS origin list."""
    if configured_origins is None:
        return _DEFAULT_DASHBOARD_CORS_ALLOWED_ORIGINS
    origins = tuple(origin for origin in (part.strip() for part in configured_origins.split(",")) if origin)
    return origins or _DEFAULT_DASHBOARD_CORS_ALLOWED_ORIGINS


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
)
initialize_api_app(app, _runtime_paths)
_add_dashboard_cors_middleware(app, _runtime_paths)


def _sanitize_entity_payload(entity_data: dict[str, Any]) -> dict[str, Any]:
    """Return a copy of entity data without API-only ID fields."""
    payload = entity_data.copy()
    payload.pop("id", None)
    return payload


def _resolve_unique_entity_id(base_id: str, entities: dict[str, Any]) -> str:
    """Return a unique ID, appending a numeric suffix when needed."""
    if base_id not in entities:
        return base_id
    counter = 1
    while f"{base_id}_{counter}" in entities:
        counter += 1
    return f"{base_id}_{counter}"


def _list_entities(config_data: dict[str, Any], section: str) -> list[dict[str, Any]]:
    return [{"id": entity_id, **entity_data} for entity_id, entity_data in config_data.get(section, {}).items()]


def _upsert_section_entity(
    candidate_config: dict[str, Any],
    section: str,
    entity_id: str,
    entity_data: dict[str, Any],
) -> None:
    candidate_config.setdefault(section, {})[entity_id] = _sanitize_entity_payload(entity_data)


def _create_section_entity(
    candidate_config: dict[str, Any],
    section: str,
    default_entity_id: str,
    entity_data: dict[str, Any],
) -> str:
    base_entity_id = entity_data.get("display_name", default_entity_id).lower().replace(" ", "_")
    entity_id = _resolve_unique_entity_id(base_entity_id, candidate_config.setdefault(section, {}))
    _upsert_section_entity(candidate_config, section, entity_id, entity_data)
    return entity_id


def _delete_section_entity(
    candidate_config: dict[str, Any],
    section: str,
    entity_id: str,
    not_found_detail: str,
) -> None:
    if section not in candidate_config or entity_id not in candidate_config[section]:
        raise HTTPException(status_code=404, detail=not_found_detail)
    del candidate_config[section][entity_id]


def _set_config_generation_header(response: Response, generation: int) -> None:
    """Attach the committed config generation to one API response."""
    response.headers[config_lifecycle.CONFIG_GENERATION_HEADER] = str(generation)


# Include routers
app.include_router(auth_router)
app.include_router(credentials_router, dependencies=[Depends(verify_user)])
app.include_router(homeassistant_router, dependencies=[Depends(verify_user)])
app.include_router(integrations_router, dependencies=[Depends(verify_user)])
app.include_router(matrix_router, dependencies=[Depends(verify_user)])
app.include_router(oauth_router)
app.include_router(schedules_router, dependencies=[Depends(verify_user)])
app.include_router(knowledge_router, dependencies=[Depends(verify_user)])
app.include_router(skills_router, dependencies=[Depends(verify_user)])
app.include_router(tools_router, dependencies=[Depends(verify_user)])
app.include_router(workers_router, dependencies=[Depends(verify_user)])
app.include_router(openai_compat_router)  # Uses its own bearer auth, not verify_user


@app.get("/api/health")
async def health_check(request: Request) -> JSONResponse:
    """Health check endpoint with Matrix sync-loop liveness."""
    runtime_state = get_runtime_state()
    runtime_paths = _api_runtime_paths(request)
    sync_health = get_matrix_sync_health_snapshot(
        startup_grace_seconds=matrix_sync_startup_timeout_seconds(runtime_paths),
    )

    response: dict[str, object] = {
        "status": "healthy",
        "last_sync_time": sync_health.last_sync_time.isoformat() if sync_health.last_sync_time is not None else None,
    }
    if sync_health.stale_entities:
        response["stale_sync_entities"] = list(sync_health.stale_entities)

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
    _user: Annotated[dict, Depends(verify_user)],
) -> dict[str, Any]:
    """Load configuration from file."""
    generation = config_lifecycle.committed_generation(request)
    payload = config_lifecycle.read_committed_config(request, lambda config_data: dict(config_data))
    _set_config_generation_header(response, generation)
    return payload


@app.put("/api/config/save")
async def save_config(
    request: Request,
    response: Response,
    new_config: dict[str, Any],
    _user: Annotated[dict, Depends(verify_user)],
    x_mindroom_config_generation: Annotated[int | None, Header()] = None,
) -> dict[str, bool]:
    """Save configuration to file."""
    previous = _read_request_runtime_config_or_none(request)
    generation = config_lifecycle.replace_committed_config(
        request,
        new_config,
        error_prefix="Failed to save configuration",
        expected_generation=x_mindroom_config_generation,
    )
    _reconcile_knowledge_mode_transitions(previous, request.app)
    _set_config_generation_header(response, generation)
    return {"success": True}


@app.get("/api/config/raw")
async def get_raw_config_source(
    request: Request,
    response: Response,
    _user: Annotated[dict, Depends(verify_user)],
) -> dict[str, str]:
    """Return the raw config source text for recovery editing."""
    generation = config_lifecycle.committed_generation(request)
    payload = {"source": config_lifecycle.read_raw_config_source(request)}
    _set_config_generation_header(response, generation)
    return payload


@app.put("/api/config/raw")
async def save_raw_config_source(
    request: Request,
    response: Response,
    payload: RawConfigSourceRequest,
    _user: Annotated[dict, Depends(verify_user)],
    x_mindroom_config_generation: Annotated[int | None, Header()] = None,
) -> dict[str, bool]:
    """Replace the raw config source text after validating it against the active runtime."""
    previous = _read_request_runtime_config_or_none(request)
    generation = config_lifecycle.replace_raw_config_source(
        request,
        payload.source,
        error_prefix="Failed to save raw configuration",
        expected_generation=x_mindroom_config_generation,
    )
    _reconcile_knowledge_mode_transitions(previous, request.app)
    _set_config_generation_header(response, generation)
    return {"success": True}


@app.post("/api/config/agent-policies")
async def get_agent_policies(
    payload: AgentPoliciesRequest,
    _user: Annotated[dict, Depends(verify_user)],
) -> dict[str, dict[str, dict[str, Any]]]:
    """Return backend-derived policies for the current draft agent config."""
    default_worker_scope = payload.defaults.worker_scope if payload.defaults is not None else None
    agent_payload = {
        agent_name: agent_config.model_dump(exclude_none=True) for agent_name, agent_config in payload.agents.items()
    }
    policy_index = resolve_agent_policy_index(
        build_agent_policy_seeds(
            agent_payload,
            default_worker_scope=default_worker_scope,
        ),
    )
    return {
        "agent_policies": {agent_name: asdict(policy) for agent_name, policy in policy_index.policies.items()},
    }


@app.get("/api/config/agents")
async def get_agents(request: Request, _user: Annotated[dict, Depends(verify_user)]) -> list[dict[str, Any]]:
    """Get all agents."""
    return config_lifecycle.read_committed_config(request, lambda config_data: _list_entities(config_data, "agents"))


@app.put("/api/config/agents/{agent_id}")
async def update_agent(
    request: Request,
    agent_id: str,
    agent_data: dict[str, Any],
    _user: Annotated[dict, Depends(verify_user)],
) -> dict[str, bool]:
    """Update a specific agent."""

    def mutate(candidate_config: dict[str, Any]) -> None:
        _upsert_section_entity(candidate_config, "agents", agent_id, agent_data)

    config_lifecycle.write_committed_config(
        request,
        mutate,
        error_prefix="Failed to save agent",
    )
    return {"success": True}


@app.post("/api/config/agents")
async def create_agent(
    request: Request,
    agent_data: dict[str, Any],
    _user: Annotated[dict, Depends(verify_user)],
) -> dict[str, Any]:
    """Create a new agent."""

    def mutate(candidate_config: dict[str, Any]) -> str:
        return _create_section_entity(candidate_config, "agents", "new_agent", agent_data)

    agent_id = config_lifecycle.write_committed_config(
        request,
        mutate,
        error_prefix="Failed to create agent",
    )
    return {"id": agent_id, "success": True}


@app.delete("/api/config/agents/{agent_id}")
async def delete_agent(
    request: Request,
    agent_id: str,
    _user: Annotated[dict, Depends(verify_user)],
) -> dict[str, bool]:
    """Delete an agent."""

    def mutate(candidate_config: dict[str, Any]) -> None:
        _delete_section_entity(candidate_config, "agents", agent_id, "Agent not found")

    config_lifecycle.write_committed_config(
        request,
        mutate,
        error_prefix="Failed to delete agent",
    )
    return {"success": True}


@app.get("/api/config/teams")
async def get_teams(request: Request, _user: Annotated[dict, Depends(verify_user)]) -> list[dict[str, Any]]:
    """Get all teams."""
    return config_lifecycle.read_committed_config(request, lambda config_data: _list_entities(config_data, "teams"))


@app.put("/api/config/teams/{team_id}")
async def update_team(
    request: Request,
    team_id: str,
    team_data: dict[str, Any],
    _user: Annotated[dict, Depends(verify_user)],
) -> dict[str, bool]:
    """Update a specific team."""

    def mutate(candidate_config: dict[str, Any]) -> None:
        _upsert_section_entity(candidate_config, "teams", team_id, team_data)

    config_lifecycle.write_committed_config(
        request,
        mutate,
        error_prefix="Failed to save team",
    )
    return {"success": True}


@app.post("/api/config/teams")
async def create_team(
    request: Request,
    team_data: dict[str, Any],
    _user: Annotated[dict, Depends(verify_user)],
) -> dict[str, Any]:
    """Create a new team."""

    def mutate(candidate_config: dict[str, Any]) -> str:
        return _create_section_entity(candidate_config, "teams", "new_team", team_data)

    team_id = config_lifecycle.write_committed_config(
        request,
        mutate,
        error_prefix="Failed to create team",
    )
    return {"id": team_id, "success": True}


@app.delete("/api/config/teams/{team_id}")
async def delete_team(
    request: Request,
    team_id: str,
    _user: Annotated[dict, Depends(verify_user)],
) -> dict[str, bool]:
    """Delete a team."""

    def mutate(candidate_config: dict[str, Any]) -> None:
        _delete_section_entity(candidate_config, "teams", team_id, "Team not found")

    config_lifecycle.write_committed_config(
        request,
        mutate,
        error_prefix="Failed to delete team",
    )
    return {"success": True}


@app.get("/api/config/models")
async def get_models(request: Request, _user: Annotated[dict, Depends(verify_user)]) -> dict[str, Any]:
    """Get all model configurations."""
    return config_lifecycle.read_committed_config(
        request,
        lambda config_data: dict(config_data.get("models", {})) if config_data.get("models") else {},
    )


@app.put("/api/config/models/{model_id}")
async def update_model(
    request: Request,
    model_id: str,
    model_data: dict[str, Any],
    _user: Annotated[dict, Depends(verify_user)],
) -> dict[str, bool]:
    """Update a model configuration."""

    def mutate(candidate_config: dict[str, Any]) -> None:
        if "models" not in candidate_config:
            candidate_config["models"] = {}
        candidate_config["models"][model_id] = model_data

    config_lifecycle.write_committed_config(
        request,
        mutate,
        error_prefix="Failed to save model",
    )
    return {"success": True}


@app.get("/api/config/room-models")
async def get_room_models(request: Request, _user: Annotated[dict, Depends(verify_user)]) -> dict[str, Any]:
    """Get room-specific model overrides."""
    return config_lifecycle.read_committed_config(
        request,
        lambda config_data: dict(config_data.get("room_models", {})) if config_data.get("room_models") else {},
    )


@app.put("/api/config/room-models")
async def update_room_models(
    request: Request,
    room_models: dict[str, str],
    _user: Annotated[dict, Depends(verify_user)],
) -> dict[str, bool]:
    """Update room-specific model overrides."""

    def mutate(candidate_config: dict[str, Any]) -> None:
        candidate_config["room_models"] = room_models

    config_lifecycle.write_committed_config(
        request,
        mutate,
        error_prefix="Failed to save room models",
    )
    return {"success": True}


@app.get("/api/rooms")
async def get_available_rooms(request: Request, _user: Annotated[dict, Depends(verify_user)]) -> list[str]:
    """Get list of available rooms."""

    def read_rooms(config_data: dict[str, Any]) -> list[str]:
        rooms: set[str] = set()
        rooms.update(config_data.get("rooms", {}))
        for agent_data in config_data.get("agents", {}).values():
            agent_rooms = agent_data.get("rooms", [])
            rooms.update(agent_rooms)
        for team_data in config_data.get("teams", {}).values():
            team_rooms = team_data.get("rooms", [])
            rooms.update(team_rooms)
        return sorted(rooms)

    return config_lifecycle.read_committed_config(request, read_rooms)


app.include_router(frontend_router)


if __name__ == "__main__":
    import uvicorn

    uvicorn.run(app, host="0.0.0.0", port=8765)  # noqa: S104
