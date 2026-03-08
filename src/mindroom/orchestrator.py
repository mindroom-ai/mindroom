"""Multi-agent orchestration facade."""

from __future__ import annotations

import asyncio
import time
from contextlib import suppress
from dataclasses import dataclass, field
from pathlib import Path
from typing import TYPE_CHECKING, Any

import uvicorn

from mindroom.agents import get_rooms_for_entity
from mindroom.authorization import is_authorized_sender
from mindroom.matrix.client import get_joined_rooms, get_room_members, invite_to_room
from mindroom.matrix.rooms import (
    ensure_all_rooms_exist,
    ensure_root_space,
    ensure_user_in_rooms,
    load_rooms,
    resolve_room_aliases,
)
from mindroom.matrix.state import MatrixState
from mindroom.matrix.users import create_agent_user
from mindroom.runtime_state import reset_runtime_state, set_runtime_starting
from mindroom.tool_system.plugins import load_plugins
from mindroom.tool_system.skills import clear_skill_cache, get_skill_snapshot

from .bot import AgentBot, TeamBot, create_bot_for_entity
from .config.main import Config
from .constants import CONFIG_PATH, MATRIX_HOMESERVER
from .credentials_sync import sync_env_to_credentials
from .file_watcher import watch_file
from .logging_config import get_logger, setup_logging
from .orchestration.config_updates import (
    _get_changed_agents,
    _identify_entities_to_restart,
)
from .orchestration.config_updates import (
    update_config as update_config_method,
)
from .orchestration.rooms import (
    _ensure_room_invitations as _ensure_room_invitations_method,
)
from .orchestration.rooms import (
    _ensure_rooms_exist as _ensure_rooms_exist_method,
)
from .orchestration.rooms import (
    _ensure_root_space as _ensure_root_space_method,
)
from .orchestration.rooms import (
    _setup_rooms_and_memberships as _setup_rooms_and_memberships_method,
)
from .orchestration.runtime import (
    _STARTUP_RETRY_INITIAL_DELAY_SECONDS,
    _STARTUP_RETRY_MAX_DELAY_SECONDS,
    _cancel_sync_task,
    _create_temp_user,
    _matrix_homeserver_startup_timeout_seconds_from_env,
    _retry_delay_seconds,
    _run_with_retry,
    _stop_entities,
    _sync_forever_with_restart,
    _wait_for_matrix_homeserver,
)
from .orchestration.runtime import (
    _bots_to_setup_after_background_start as _bots_to_setup_after_background_start_method,
)
from .orchestration.runtime import (
    _cancel_bot_start_task as _cancel_bot_start_task_method,
)
from .orchestration.runtime import (
    _cancel_bot_start_tasks as _cancel_bot_start_tasks_method,
)
from .orchestration.runtime import (
    _cancel_knowledge_refresh_task as _cancel_knowledge_refresh_task_method,
)
from .orchestration.runtime import (
    _configure_knowledge as _configure_knowledge_method,
)
from .orchestration.runtime import (
    _configured_entity_names as _configured_entity_names_method,
)
from .orchestration.runtime import (
    _create_and_start_entities as _create_and_start_entities_method,
)
from .orchestration.runtime import (
    _create_managed_bot as _create_managed_bot_method,
)
from .orchestration.runtime import (
    _ensure_user_account as _ensure_user_account_method,
)
from .orchestration.runtime import (
    _log_degraded_startup as _log_degraded_startup_method,
)
from .orchestration.runtime import (
    _prepare_user_account as _prepare_user_account_method,
)
from .orchestration.runtime import (
    _refresh_knowledge_for_runtime as _refresh_knowledge_for_runtime_method,
)
from .orchestration.runtime import (
    _require_config as _require_config_method,
)
from .orchestration.runtime import (
    _run_bot_start_retry as _run_bot_start_retry_method,
)
from .orchestration.runtime import (
    _run_knowledge_refresh as _run_knowledge_refresh_method,
)
from .orchestration.runtime import (
    _running_bots_for_entities as _running_bots_for_entities_method,
)
from .orchestration.runtime import (
    _schedule_bot_start_retry as _schedule_bot_start_retry_method,
)
from .orchestration.runtime import (
    _schedule_knowledge_refresh as _schedule_knowledge_refresh_method,
)
from .orchestration.runtime import (
    _start_entities_once as _start_entities_once_method,
)
from .orchestration.runtime import (
    _start_router_bot as _start_router_bot_method,
)
from .orchestration.runtime import (
    _start_runtime as _start_runtime_method,
)
from .orchestration.runtime import (
    _start_sync_task as _start_sync_task_method,
)
from .orchestration.runtime import (
    _stop_memory_auto_flush_worker as _stop_memory_auto_flush_worker_method,
)
from .orchestration.runtime import (
    _sync_memory_auto_flush_worker as _sync_memory_auto_flush_worker_method,
)
from .orchestration.runtime import (
    _sync_runtime_support_services as _sync_runtime_support_services_method,
)
from .orchestration.runtime import (
    _try_start_bot_once as _try_start_bot_once_method,
)
from .orchestration.runtime import (
    initialize as initialize_method,
)
from .orchestration.runtime import (
    start as start_method,
)
from .orchestration.runtime import (
    stop as stop_method,
)

if TYPE_CHECKING:
    from collections.abc import Awaitable, Callable

    from .knowledge.manager import KnowledgeManager

logger = get_logger(__name__)
type _OrchestratorCollaborator = Any

_AUXILIARY_TASK_RESTART_INITIAL_DELAY_SECONDS = 1.0
_AUXILIARY_TASK_RESTART_MAX_DELAY_SECONDS = 30.0

__all__ = [
    "_STARTUP_RETRY_INITIAL_DELAY_SECONDS",
    "_STARTUP_RETRY_MAX_DELAY_SECONDS",
    "Config",
    "MultiAgentOrchestrator",
    "_cancel_sync_task",
    "_create_temp_user",
    "_get_changed_agents",
    "_identify_entities_to_restart",
    "_matrix_homeserver_startup_timeout_seconds_from_env",
    "_run_with_retry",
    "_stop_entities",
    "_sync_forever_with_restart",
    "_wait_for_matrix_homeserver",
    "create_bot_for_entity",
    "main",
]


@dataclass
class MultiAgentOrchestrator:
    """Orchestrates multiple agent bots."""

    storage_path: Path
    agent_bots: dict[str, AgentBot | TeamBot] = field(default_factory=dict, init=False)
    running: bool = field(default=False, init=False)
    config: Config | None = field(default=None, init=False)
    _sync_tasks: dict[str, asyncio.Task] = field(default_factory=dict, init=False)
    _bot_start_tasks: dict[str, asyncio.Task] = field(default_factory=dict, init=False)
    knowledge_managers: dict[str, KnowledgeManager] = field(default_factory=dict, init=False)
    _memory_auto_flush_worker: Any = field(default=None, init=False)
    _memory_auto_flush_task: asyncio.Task | None = field(default=None, init=False)
    _knowledge_refresh_task: asyncio.Task | None = field(default=None, init=False)

    def __post_init__(self) -> None:
        """Store a canonical absolute storage path to survive runtime cwd changes."""
        self.storage_path = self.storage_path.expanduser().resolve()

    _stop_memory_auto_flush_worker = _stop_memory_auto_flush_worker_method
    _sync_memory_auto_flush_worker = _sync_memory_auto_flush_worker_method
    _ensure_user_account = _ensure_user_account_method
    _require_config = _require_config_method
    _prepare_user_account = _prepare_user_account_method
    _configure_knowledge = _configure_knowledge_method
    _cancel_knowledge_refresh_task = _cancel_knowledge_refresh_task_method
    _cancel_bot_start_task = _cancel_bot_start_task_method
    _cancel_bot_start_tasks = _cancel_bot_start_tasks_method
    _start_sync_task = _start_sync_task_method
    _bots_to_setup_after_background_start = _bots_to_setup_after_background_start_method
    _running_bots_for_entities = _running_bots_for_entities_method
    _try_start_bot_once = _try_start_bot_once_method
    _run_bot_start_retry = _run_bot_start_retry_method
    _schedule_bot_start_retry = _schedule_bot_start_retry_method
    _run_knowledge_refresh = _run_knowledge_refresh_method
    _schedule_knowledge_refresh = _schedule_knowledge_refresh_method
    _refresh_knowledge_for_runtime = _refresh_knowledge_for_runtime_method
    _sync_runtime_support_services = _sync_runtime_support_services_method
    _configured_entity_names = staticmethod(_configured_entity_names_method)
    _create_managed_bot = _create_managed_bot_method
    _start_entities_once = _start_entities_once_method
    _create_and_start_entities = _create_and_start_entities_method
    initialize = initialize_method
    start = start_method
    _start_router_bot = _start_router_bot_method
    _log_degraded_startup = _log_degraded_startup_method
    _start_runtime = _start_runtime_method
    update_config = update_config_method
    stop = stop_method

    _setup_rooms_and_memberships = _setup_rooms_and_memberships_method
    _ensure_rooms_exist = _ensure_rooms_exist_method
    _ensure_root_space = _ensure_root_space_method
    _ensure_room_invitations = _ensure_room_invitations_method

    @property
    def matrix_homeserver(self) -> str:
        """Return the current Matrix homeserver URL."""
        return MATRIX_HOMESERVER

    @property
    def load_plugins(self) -> _OrchestratorCollaborator:
        """Return the current plugin loader."""
        return load_plugins

    @property
    def bot_factory(self) -> _OrchestratorCollaborator:
        """Return the current bot factory."""
        return create_bot_for_entity

    @property
    def create_agent_user(self) -> _OrchestratorCollaborator:
        """Return the current Matrix user creation helper."""
        return create_agent_user

    @property
    def identify_entities_to_restart(self) -> _OrchestratorCollaborator:
        """Return the current config-diff planner."""
        return _identify_entities_to_restart

    @property
    def wait_for_matrix_homeserver(self) -> _OrchestratorCollaborator:
        """Return the current Matrix homeserver wait helper."""
        return _wait_for_matrix_homeserver

    @property
    def sync_forever_with_restart(self) -> _OrchestratorCollaborator:
        """Return the current sync-loop supervisor."""
        return _sync_forever_with_restart

    @property
    def cancel_sync_task(self) -> _OrchestratorCollaborator:
        """Return the current sync-task cancellation helper."""
        return _cancel_sync_task

    @property
    def stop_entities(self) -> _OrchestratorCollaborator:
        """Return the current entity-stop helper."""
        return _stop_entities

    @property
    def get_rooms_for_entity(self) -> _OrchestratorCollaborator:
        """Return the current room lookup helper."""
        return get_rooms_for_entity

    @property
    def resolve_room_aliases(self) -> _OrchestratorCollaborator:
        """Return the current room alias resolver."""
        return resolve_room_aliases

    @property
    def load_rooms(self) -> _OrchestratorCollaborator:
        """Return the current room state loader."""
        return load_rooms

    @property
    def ensure_all_rooms_exist(self) -> _OrchestratorCollaborator:
        """Return the current room creation/reconciliation helper."""
        return ensure_all_rooms_exist

    @property
    def ensure_root_space(self) -> _OrchestratorCollaborator:
        """Return the current root-space reconciliation helper."""
        return ensure_root_space

    @property
    def ensure_user_in_rooms(self) -> _OrchestratorCollaborator:
        """Return the current internal-user membership helper."""
        return ensure_user_in_rooms

    @property
    def get_joined_rooms(self) -> _OrchestratorCollaborator:
        """Return the current joined-room fetcher."""
        return get_joined_rooms

    @property
    def get_room_members(self) -> _OrchestratorCollaborator:
        """Return the current room-members fetcher."""
        return get_room_members

    @property
    def invite_to_room(self) -> _OrchestratorCollaborator:
        """Return the current room invite helper."""
        return invite_to_room

    @property
    def matrix_state_cls(self) -> _OrchestratorCollaborator:
        """Return the current Matrix state storage class."""
        return MatrixState

    @property
    def is_authorized_sender(self) -> _OrchestratorCollaborator:
        """Return the current authorization predicate."""
        return is_authorized_sender


async def _handle_config_change(orchestrator: MultiAgentOrchestrator) -> None:
    """Handle configuration file changes."""
    logger.info("Configuration file changed, checking for updates...")
    if orchestrator.running:
        updated = await orchestrator.update_config()
        if updated:
            logger.info("Configuration update applied to affected agents")
        else:
            logger.info("No agent changes detected in configuration update")
        return
    logger.info("Ignoring config change while startup is still in progress")


async def _watch_config_task(config_path: Path, orchestrator: MultiAgentOrchestrator) -> None:
    """Watch config file for changes."""

    async def on_config_change() -> None:
        await _handle_config_change(orchestrator)

    await watch_file(config_path, on_config_change)


async def _watch_skills_task(orchestrator: MultiAgentOrchestrator) -> None:
    """Watch skill roots for changes and clear cached skills."""
    while not orchestrator.running:  # noqa: ASYNC110
        await asyncio.sleep(0.1)
    last_snapshot = get_skill_snapshot()
    while orchestrator.running:
        await asyncio.sleep(1.0)
        snapshot = get_skill_snapshot()
        if snapshot != last_snapshot:
            last_snapshot = snapshot
            clear_skill_cache()
            logger.info("Skills changed; cache cleared")


async def _run_api_server(host: str, port: int, log_level: str) -> None:
    """Run the bundled dashboard/API server as an asyncio task."""
    from mindroom.api.main import app as api_app  # noqa: PLC0415

    config = uvicorn.Config(api_app, host=host, port=port, log_level=log_level.lower())
    server = uvicorn.Server(config)
    await server.serve()


async def _run_auxiliary_task_forever(
    task_name: str,
    operation: Callable[[], Awaitable[None]],
) -> None:
    """Restart a non-critical background task whenever it exits or crashes."""
    restart_count = 0
    while True:
        started_at = time.monotonic()
        try:
            await operation()
            logger.warning("Auxiliary task exited; restarting", task_name=task_name)
        except asyncio.CancelledError:
            raise
        except Exception:
            logger.exception("Auxiliary task crashed; restarting", task_name=task_name)
        if time.monotonic() - started_at >= _AUXILIARY_TASK_RESTART_MAX_DELAY_SECONDS:
            restart_count = 0
        restart_count += 1
        await asyncio.sleep(
            _retry_delay_seconds(
                restart_count,
                initial_delay_seconds=_AUXILIARY_TASK_RESTART_INITIAL_DELAY_SECONDS,
                max_delay_seconds=_AUXILIARY_TASK_RESTART_MAX_DELAY_SECONDS,
            ),
        )


async def main(
    log_level: str,
    storage_path: Path,
    *,
    api: bool = True,
    api_port: int = 8765,
    api_host: str = "0.0.0.0",  # noqa: S104
) -> None:
    """Main entry point for the multi-agent bot system."""
    setup_logging(level=log_level)

    storage_path = storage_path.expanduser().resolve()

    logger.info("Syncing API keys from environment to CredentialsManager...")
    sync_env_to_credentials()

    storage_path.mkdir(parents=True, exist_ok=True)

    config_path = Path(CONFIG_PATH)

    logger.info("Starting orchestrator...")
    orchestrator = MultiAgentOrchestrator(storage_path=storage_path)
    set_runtime_starting()
    auxiliary_tasks: list[asyncio.Task] = []

    try:
        auxiliary_specs = [
            ("config watcher", lambda: _watch_config_task(config_path, orchestrator), "config_watcher_supervisor"),
            ("skills watcher", lambda: _watch_skills_task(orchestrator), "skills_watcher_supervisor"),
        ]

        if api:
            logger.info("Starting bundled dashboard/API server on %s:%d", api_host, api_port)
            auxiliary_specs.append(
                ("bundled API server", lambda: _run_api_server(api_host, api_port, log_level), "api_server_supervisor"),
            )

        for task_name, operation, supervisor_name in auxiliary_specs:
            auxiliary_tasks.append(
                asyncio.create_task(
                    _run_auxiliary_task_forever(task_name, operation),
                    name=supervisor_name,
                ),
            )

        await orchestrator.start()

    except KeyboardInterrupt:
        logger.info("Multi-agent bot system stopped by user")
    except Exception:
        logger.exception("Error in orchestrator")
        raise
    finally:
        for task in auxiliary_tasks:
            task.cancel()
        for task in auxiliary_tasks:
            with suppress(asyncio.CancelledError):
                await task
        await orchestrator.stop()
        reset_runtime_state()
