# ruff: noqa: D100
from __future__ import annotations

import asyncio
import threading
from contextlib import asynccontextmanager, suppress
from dataclasses import asdict
from typing import TYPE_CHECKING, Annotated, Any, Literal, cast

from fastapi import Depends, FastAPI, Header, HTTPException, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse, Response
from pydantic import BaseModel, ConfigDict, Field

from mindroom import constants
from mindroom.agent_policy import build_agent_policy_seeds, resolve_agent_policy_index
from mindroom.api import config_lifecycle
from mindroom.api.auth import ApiAuthState, verify_user
from mindroom.api.auth import router as auth_router
from mindroom.api.config_lifecycle import ApiSnapshot, ApiState, ConfigLoadResult

# Import routers
from mindroom.api.credentials import router as credentials_router
from mindroom.api.frontend import router as frontend_router
from mindroom.api.google_integration import router as google_router
from mindroom.api.homeassistant_integration import router as homeassistant_router
from mindroom.api.integrations import router as integrations_router
from mindroom.api.knowledge import router as knowledge_router
from mindroom.api.matrix_operations import router as matrix_router
from mindroom.api.openai_compat import router as openai_compat_router
from mindroom.api.schedules import router as schedules_router
from mindroom.api.skills import router as skills_router
from mindroom.api.tools import router as tools_router
from mindroom.api.workers import router as workers_router
from mindroom.credentials_sync import sync_env_to_credentials
from mindroom.knowledge import KnowledgeRefreshScheduler
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
    from collections.abc import AsyncIterator, Callable
    from pathlib import Path

    from mindroom.config.main import Config
logger = get_logger(__name__)
_UNSET = object()
_WORKER_CLEANUP_INTERVAL_ENV = "MINDROOM_WORKER_CLEANUP_INTERVAL_SECONDS"


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


def api_runtime_paths(request: Request) -> constants.RuntimePaths:
    """Return the API request's committed runtime paths."""
    return config_lifecycle.api_runtime_paths(request)


def _app_state(api_app: FastAPI) -> ApiState:
    """Return the committed API state holder for one app instance."""
    try:
        state = api_app.state.api_state
    except AttributeError:
        state = None
    if not isinstance(state, ApiState):
        msg = "API context is not initialized"
        raise TypeError(msg)
    return state


def _published_snapshot(
    snapshot: ApiSnapshot,
    *,
    increment_generation: bool = True,
    runtime_paths: constants.RuntimePaths | None = None,
    config_data: dict[str, Any] | None = None,
    runtime_config: Config | None | object = _UNSET,
    config_load_result: ConfigLoadResult | None | object = _UNSET,
    auth_state: ApiAuthState | None | object = _UNSET,
) -> ApiSnapshot:
    """Return one new published snapshot with an incremented generation."""
    updated_runtime_paths = snapshot.runtime_paths if runtime_paths is None else runtime_paths
    updated_config_data = snapshot.config_data if config_data is None else config_data
    updated_runtime_config = snapshot.runtime_config if runtime_config is _UNSET else runtime_config
    updated_config_load_result = (
        snapshot.config_load_result
        if config_load_result is _UNSET
        else cast("ConfigLoadResult | None", config_load_result)
    )
    updated_auth_state = snapshot.auth_state if auth_state is _UNSET else auth_state
    return ApiSnapshot(
        generation=snapshot.generation + 1 if increment_generation else snapshot.generation,
        runtime_paths=updated_runtime_paths,
        config_data=updated_config_data,
        runtime_config=cast("Config | None", updated_runtime_config),
        config_load_result=updated_config_load_result,
        auth_state=updated_auth_state,
    )


def _app_context(api_app: FastAPI) -> ApiSnapshot:
    """Return the committed API snapshot for one app instance."""
    return _app_state(api_app).snapshot


def _app_runtime_paths(api_app: FastAPI) -> constants.RuntimePaths:
    """Return the committed runtime paths for one API app instance."""
    return _app_context(api_app).runtime_paths


def _bind_api_auth_ingress_context(api_app: FastAPI, runtime_paths: constants.RuntimePaths) -> None:
    """Bind tenant/account auth context at the API ingress boundary."""
    api_app.state.api_auth_account_id = runtime_paths.env_value("ACCOUNT_ID")


def initialize_api_app(api_app: FastAPI, runtime_paths: constants.RuntimePaths) -> None:
    """Initialize one API app instance with explicit runtime-bound state."""
    _bind_api_auth_ingress_context(api_app, runtime_paths)
    try:
        previous_state = api_app.state.api_state
    except AttributeError:
        previous_state = None
    if not isinstance(previous_state, ApiState):
        api_app.state.api_state = ApiState(
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

    config_lock = previous_state.config_lock
    with config_lock:
        try:
            current_state = api_app.state.api_state
        except AttributeError:
            current_state = previous_state
        if not isinstance(current_state, ApiState):
            current_state = previous_state
        current_snapshot = current_state.snapshot
        auth_state = current_snapshot.auth_state if current_snapshot.runtime_paths == runtime_paths else None
        config_data = current_snapshot.config_data if current_snapshot.runtime_paths == runtime_paths else {}
        runtime_config = current_snapshot.runtime_config if current_snapshot.runtime_paths == runtime_paths else None
        config_load_result = (
            current_snapshot.config_load_result if current_snapshot.runtime_paths == runtime_paths else None
        )
        current_state.snapshot = _published_snapshot(
            current_snapshot,
            runtime_paths=runtime_paths,
            config_data=config_data,
            runtime_config=runtime_config,
            auth_state=auth_state,
            config_load_result=config_load_result,
        )
        api_app.state.api_state = current_state
    config_lifecycle.register_api_app(api_app)


def _orchestrator_knowledge_refresh_scheduler(api_app: FastAPI) -> KnowledgeRefreshScheduler | None:
    """Return an orchestrator-provided refresh scheduler when the bundled API is running."""
    try:
        refresh_scheduler = api_app.state.orchestrator_knowledge_refresh_scheduler
    except AttributeError:
        return None
    return cast("KnowledgeRefreshScheduler | None", refresh_scheduler)


def _standalone_knowledge_source_watcher(api_app: FastAPI) -> KnowledgeSourceWatcher | None:
    """Return the API-owned filesystem watcher, when running without the orchestrator."""
    try:
        source_watcher = api_app.state.knowledge_source_watcher
    except AttributeError:
        return None
    return cast("KnowledgeSourceWatcher | None", source_watcher)


async def _sync_standalone_knowledge_watchers(api_app: FastAPI) -> None:
    """Align API-owned knowledge filesystem watchers with the committed config."""
    source_watcher = _standalone_knowledge_source_watcher(api_app)
    if source_watcher is None:
        return
    snapshot = _app_context(api_app)
    await source_watcher.sync(config=snapshot.runtime_config, runtime_paths=snapshot.runtime_paths)


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
                config_lifecycle.load_config_into_app(runtime_paths, api_app)
                await _sync_standalone_knowledge_watchers(api_app)
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

    api_owned_knowledge_refresh_scheduler: KnowledgeRefreshScheduler | None = None
    standalone_knowledge_source_watcher: KnowledgeSourceWatcher | None = None
    knowledge_refresh_scheduler = _orchestrator_knowledge_refresh_scheduler(_app)
    if knowledge_refresh_scheduler is None:
        api_owned_knowledge_refresh_scheduler = KnowledgeRefreshScheduler()
        knowledge_refresh_scheduler = api_owned_knowledge_refresh_scheduler
        standalone_knowledge_source_watcher = KnowledgeSourceWatcher(knowledge_refresh_scheduler)
        _app.state.knowledge_source_watcher = standalone_knowledge_source_watcher
    _app.state.knowledge_refresh_scheduler = knowledge_refresh_scheduler
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


app = FastAPI(title="MindRoom Dashboard API", lifespan=_lifespan)
initialize_api_app(app, constants.resolve_primary_runtime_paths())

# Configure CORS for the standalone frontend dev server.
app.add_middleware(
    CORSMiddleware,  # ty: ignore[invalid-argument-type]
    allow_origins=[
        "http://localhost:3003",  # Frontend dev server alternative port
        "http://localhost:5173",  # Vite dev server default
        "http://127.0.0.1:3003",  # Alternative localhost
        "http://127.0.0.1:5173",
        "*",  # Allow all origins for development (remove in production)
    ],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
    expose_headers=["*"],
)


def _reload_api_runtime_config(
    api_app: FastAPI,
    runtime_paths: constants.RuntimePaths,
    *,
    expected_snapshot: ApiSnapshot | None = None,
    mutate_runtime: Callable[[constants.RuntimePaths], constants.RuntimePaths] | None = None,
) -> None:
    """Rebind the API app to one runtime and surface structured config reload failures."""
    app_state = _app_state(api_app)
    with app_state.config_lock:
        current_state = _app_state(api_app)
        current_snapshot = current_state.snapshot
        if expected_snapshot is not None and (
            current_snapshot.generation != expected_snapshot.generation
            or current_snapshot.runtime_paths != expected_snapshot.runtime_paths
        ):
            raise HTTPException(
                status_code=409,
                detail="Configuration changed while request was in progress. Retry the operation.",
            )
        target_runtime_paths = runtime_paths if mutate_runtime is None else mutate_runtime(runtime_paths)
        _bind_api_auth_ingress_context(api_app, target_runtime_paths)
        auth_state = current_snapshot.auth_state if current_snapshot.runtime_paths == target_runtime_paths else None
        config_data = current_snapshot.config_data if current_snapshot.runtime_paths == target_runtime_paths else {}
        runtime_config = (
            current_snapshot.runtime_config if current_snapshot.runtime_paths == target_runtime_paths else None
        )
        config_load_result = (
            current_snapshot.config_load_result if current_snapshot.runtime_paths == target_runtime_paths else None
        )
        refreshed_snapshot = _published_snapshot(
            current_snapshot,
            runtime_paths=target_runtime_paths,
            config_data=config_data,
            runtime_config=runtime_config,
            auth_state=auth_state,
            config_load_result=config_load_result,
        )
        current_state.snapshot = refreshed_snapshot
        result, validated_payload, loaded_runtime_config = config_lifecycle._load_config_result(target_runtime_paths)
        current_state.snapshot = _published_snapshot(
            refreshed_snapshot,
            config_data=validated_payload if validated_payload is not None else refreshed_snapshot.config_data,
            runtime_config=loaded_runtime_config
            if loaded_runtime_config is not None
            else refreshed_snapshot.runtime_config,
            config_load_result=result,
        )
    config_lifecycle.raise_for_config_load_result(result)


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


def _set_config_generation_header(response: Response, generation: int) -> None:
    """Attach the committed config generation to one API response."""
    response.headers[config_lifecycle.CONFIG_GENERATION_HEADER] = str(generation)


# Include routers
app.include_router(auth_router)
app.include_router(credentials_router, dependencies=[Depends(verify_user)])
app.include_router(google_router, dependencies=[Depends(verify_user)])
app.include_router(homeassistant_router, dependencies=[Depends(verify_user)])
app.include_router(integrations_router, dependencies=[Depends(verify_user)])
app.include_router(matrix_router, dependencies=[Depends(verify_user)])
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
    runtime_paths = api_runtime_paths(request)
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
    generation = config_lifecycle.replace_committed_config(
        request,
        new_config,
        error_prefix="Failed to save configuration",
        expected_generation=x_mindroom_config_generation,
    )
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
    generation = config_lifecycle.replace_raw_config_source(
        request,
        payload.source,
        error_prefix="Failed to save raw configuration",
        expected_generation=x_mindroom_config_generation,
    )
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

    def read_agents(config_data: dict[str, Any]) -> list[dict[str, Any]]:
        agents = config_data.get("agents", {})
        # Convert to list format with IDs
        agent_list = []
        for agent_id, agent_data in agents.items():
            agent = {"id": agent_id, **agent_data}
            agent_list.append(agent)
        return agent_list

    return config_lifecycle.read_committed_config(request, read_agents)


@app.put("/api/config/agents/{agent_id}")
async def update_agent(
    request: Request,
    agent_id: str,
    agent_data: dict[str, Any],
    _user: Annotated[dict, Depends(verify_user)],
) -> dict[str, bool]:
    """Update a specific agent."""

    def mutate(candidate_config: dict[str, Any]) -> None:
        if "agents" not in candidate_config:
            candidate_config["agents"] = {}
        candidate_config["agents"][agent_id] = _sanitize_entity_payload(agent_data)

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
    base_agent_id = agent_data.get("display_name", "new_agent").lower().replace(" ", "_")

    def mutate(candidate_config: dict[str, Any]) -> str:
        if "agents" not in candidate_config:
            candidate_config["agents"] = {}
        agent_id = _resolve_unique_entity_id(base_agent_id, candidate_config["agents"])
        candidate_config["agents"][agent_id] = _sanitize_entity_payload(agent_data)
        return agent_id

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
        if "agents" not in candidate_config or agent_id not in candidate_config["agents"]:
            raise HTTPException(status_code=404, detail="Agent not found")
        del candidate_config["agents"][agent_id]

    config_lifecycle.write_committed_config(
        request,
        mutate,
        error_prefix="Failed to delete agent",
    )
    return {"success": True}


@app.get("/api/config/teams")
async def get_teams(request: Request, _user: Annotated[dict, Depends(verify_user)]) -> list[dict[str, Any]]:
    """Get all teams."""

    def read_teams(config_data: dict[str, Any]) -> list[dict[str, Any]]:
        teams = config_data.get("teams", {})
        # Convert to list format with IDs
        team_list = []
        for team_id, team_data in teams.items():
            team = {"id": team_id, **team_data}
            team_list.append(team)
        return team_list

    return config_lifecycle.read_committed_config(request, read_teams)


@app.put("/api/config/teams/{team_id}")
async def update_team(
    request: Request,
    team_id: str,
    team_data: dict[str, Any],
    _user: Annotated[dict, Depends(verify_user)],
) -> dict[str, bool]:
    """Update a specific team."""

    def mutate(candidate_config: dict[str, Any]) -> None:
        if "teams" not in candidate_config:
            candidate_config["teams"] = {}
        candidate_config["teams"][team_id] = _sanitize_entity_payload(team_data)

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
    base_team_id = team_data.get("display_name", "new_team").lower().replace(" ", "_")

    def mutate(candidate_config: dict[str, Any]) -> str:
        if "teams" not in candidate_config:
            candidate_config["teams"] = {}
        team_id = _resolve_unique_entity_id(base_team_id, candidate_config["teams"])
        candidate_config["teams"][team_id] = _sanitize_entity_payload(team_data)
        return team_id

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
        if "teams" not in candidate_config or team_id not in candidate_config["teams"]:
            raise HTTPException(status_code=404, detail="Team not found")
        del candidate_config["teams"][team_id]

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
        for agent_data in config_data.get("agents", {}).values():
            agent_rooms = agent_data.get("rooms", [])
            rooms.update(agent_rooms)
        return sorted(rooms)

    return config_lifecycle.read_committed_config(request, read_rooms)


app.include_router(frontend_router)


if __name__ == "__main__":
    import uvicorn

    uvicorn.run(app, host="0.0.0.0", port=8765)  # noqa: S104
