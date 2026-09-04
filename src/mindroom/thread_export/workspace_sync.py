"""Keep enabled agents' workspaces current with YAML thread exports.

Bots report room activity as the journal admits events; one runner debounces
those reports and runs a single export pass at a time through the live bots'
clients and projection views, so no pass logs in or opens a journal of its
own. Storage I/O runs off the event loop inside ``export_threads_to_sources``,
and target discovery runs off it here.
"""

from __future__ import annotations

import asyncio
from dataclasses import dataclass
from typing import TYPE_CHECKING, Protocol

from mindroom.constants import ROUTER_AGENT_NAME
from mindroom.logging_config import get_logger
from mindroom.private_instance_identity import PrivateInstanceIdentityError, load_private_instance_identity
from mindroom.thread_export.models import ThreadExportRoom, ThreadExportSource, ThreadExportTarget
from mindroom.thread_export.projected_history import export_conversation_reader
from mindroom.thread_export.selection import export_rooms, invited_export_rooms
from mindroom.thread_export.service import export_threads_to_sources
from mindroom.thread_export.storage import clear_thread_export_root
from mindroom.tool_system.worker_routing import (
    agent_state_root_path,
    agent_workspace_root_path,
    private_instance_state_root_for_requester,
)
from mindroom.workspaces import resolve_agent_workspace_from_state_path

if TYPE_CHECKING:
    from collections.abc import Callable, Sequence
    from pathlib import Path

    import nio

    from mindroom.config.agent import AgentThreadExportConfig
    from mindroom.config.main import Config
    from mindroom.constants import RuntimePaths
    from mindroom.event_journal import PrincipalStore
    from mindroom.matrix.identity import MatrixID
    from mindroom.thread_export.models import ThreadExportStats

logger = get_logger(__name__)

_WORKSPACE_EXPORT_DIRNAME = "thread_exports"
_PRIVATE_INSTANCES_DIRNAME = "private_instances"
_DEBOUNCE_SECONDS = 2.0


class ThreadExportBot(Protocol):
    """What the runner reads from a running bot."""

    running: bool
    client: nio.AsyncClient | None

    @property
    def matrix_id(self) -> MatrixID:
        """Return the bot's Matrix identity."""
        ...

    def journal_principal(self) -> PrincipalStore:
        """Return the bot's principal-bound projection view."""
        ...


def enabled_thread_export_agents(config: Config) -> dict[str, AgentThreadExportConfig]:
    """Return the agents whose workspaces receive thread exports."""
    return {name: agent.thread_exports for name, agent in config.agents.items() if agent.thread_exports is not None}


@dataclass(frozen=True)
class WorkspaceThreadExportDeps:
    """Runtime collaborators the workspace export runner reads through."""

    runtime_paths: RuntimePaths
    config_provider: Callable[[], Config | None]
    bot_provider: Callable[[str], ThreadExportBot | None]
    debounce_seconds: float = _DEBOUNCE_SECONDS


@dataclass(frozen=True)
class _AgentTarget:
    """One export target and the agent whose workspace it lives in."""

    agent_name: str
    target: ThreadExportTarget


class WorkspaceThreadExportRunner:
    """Debounce room activity into single-flight export passes."""

    def __init__(self, deps: WorkspaceThreadExportDeps) -> None:
        self._deps = deps
        self._pending_room_ids: set[str] = set()
        self._full_pass_pending = False
        self._wakeup = asyncio.Event()
        self._stopped = False

    def mark_room_activity(self, room_id: str) -> None:
        """Queue one room for re-export."""
        self._pending_room_ids.add(room_id)
        self._wakeup.set()

    def queue_full_pass(self) -> None:
        """Queue a pass over every room that also reconciles removed threads and rooms."""
        self._full_pass_pending = True
        self._wakeup.set()

    def stop(self) -> None:
        """Let ``run`` return after the current pass."""
        self._stopped = True
        self._wakeup.set()

    async def run(self) -> None:
        """Drain triggers one debounced pass at a time until stopped."""
        while not self._stopped:
            await self._wakeup.wait()
            self._wakeup.clear()
            if self._stopped:
                return
            if self._deps.debounce_seconds > 0:
                await asyncio.sleep(self._deps.debounce_seconds)
            await self._run_pass_once()

    async def _run_pass_once(self) -> None:
        """Consume the pending work and run one pass; requeue it when nothing could be read yet."""
        full_pass, room_ids = self._full_pass_pending, frozenset(self._pending_room_ids)
        self._full_pass_pending = False
        self._pending_room_ids.clear()
        if not full_pass and not room_ids:
            return
        config = self._deps.config_provider()
        if config is None:
            return
        try:
            completed = await self._run_pass(config, full_pass=full_pass, room_ids=room_ids)
        except Exception:
            # Keep the work for the next trigger rather than retrying in a tight
            # loop on a fault that may not clear by itself.
            logger.exception("Thread export pass crashed")
            self._requeue(full_pass=full_pass, room_ids=room_ids)
            return
        if not completed:
            self._requeue(full_pass=full_pass, room_ids=room_ids)
            self._wakeup.set()

    def _requeue(self, *, full_pass: bool, room_ids: frozenset[str]) -> None:
        self._full_pass_pending |= full_pass
        self._pending_room_ids |= room_ids

    async def _run_pass(self, config: Config, *, full_pass: bool, room_ids: frozenset[str]) -> bool:
        """Export the dirty rooms, or everything, into every enabled agent's workspace."""
        router_bot = self._ready_bot(ROUTER_AGENT_NAME)
        if router_bot is None:
            logger.debug("Deferring thread export pass until the router bot is running")
            return False
        runtime_paths = self._deps.runtime_paths
        enabled = enabled_thread_export_agents(config)
        if full_pass:
            await asyncio.to_thread(_clear_disabled_agent_exports, config, runtime_paths, frozenset(enabled))
        agent_user_ids = {
            agent_name: bot.matrix_id.full_id
            for agent_name in enabled
            if (bot := self._deps.bot_provider(agent_name)) is not None
        }
        for agent_name in enabled.keys() - agent_user_ids.keys():
            logger.warning("Skipping thread exports for agent without a bot", agent_name=agent_name)
        agent_targets = await asyncio.to_thread(_build_targets, config, runtime_paths, enabled, agent_user_ids)
        if not agent_targets:
            return True
        targets = tuple(agent_target.target for agent_target in agent_targets)
        state_rooms, invited_groups = await asyncio.to_thread(_select_rooms, config, runtime_paths)
        if not full_pass:
            state_rooms = [room for room in state_rooms if room.room_id in room_ids]
            invited_groups = [
                (entity_name, selected)
                for entity_name, rooms in invited_groups
                if (selected := [room for room in rooms if room.room_id in room_ids])
            ]
        sources = [_source_for_bot(router_bot, tuple(state_rooms), config)] if state_rooms else []
        unreadable_rooms: list[tuple[Sequence[ThreadExportRoom], str]] = []
        for entity_name, rooms in invited_groups:
            bot = self._ready_bot(entity_name)
            if bot is None:
                unreadable_rooms.append((tuple(rooms), f"Bot '{entity_name}' is not running"))
                continue
            sources.append(_source_for_bot(bot, tuple(rooms), config))
        stats = await export_threads_to_sources(
            config=config,
            runtime_paths=runtime_paths,
            sources=sources,
            targets=targets,
            unreadable_rooms=unreadable_rooms,
            full_pass=full_pass,
        )
        _log_pass(agent_targets, stats, room_ids=room_ids, full_pass=full_pass)
        return True

    def _ready_bot(self, entity_name: str) -> ThreadExportBot | None:
        """Return the bot when it is running with a Matrix client."""
        bot = self._deps.bot_provider(entity_name)
        if bot is None or not bot.running or bot.client is None:
            return None
        return bot


def _select_rooms(
    config: Config,
    runtime_paths: RuntimePaths,
) -> tuple[list[ThreadExportRoom], list[tuple[str, list[ThreadExportRoom]]]]:
    """Read the persisted configured and invited rooms, off the event loop."""
    state_rooms = export_rooms(runtime_paths, None)
    invited_groups = invited_export_rooms(
        config,
        runtime_paths,
        None,
        known_room_ids={room.room_id for room in state_rooms},
    )
    return state_rooms, invited_groups


def _source_for_bot(bot: ThreadExportBot, rooms: tuple[ThreadExportRoom, ...], config: Config) -> ThreadExportSource:
    """Read ``rooms`` through one running bot's client and projection view."""
    client = bot.client
    assert client is not None
    return ThreadExportSource(
        client=client,
        reader=export_conversation_reader(
            client=client,
            config=config,
            store=bot.journal_principal(),
            self_sender=bot.matrix_id.full_id,
        ),
        rooms=rooms,
    )


def _log_pass(
    agent_targets: Sequence[_AgentTarget],
    stats: Sequence[ThreadExportStats],
    *,
    room_ids: frozenset[str],
    full_pass: bool,
) -> None:
    """Log one line per target that did something, and every target on a full pass."""
    for agent_target, target_stats in zip(agent_targets, stats, strict=True):
        if not full_pass and not any(
            (
                target_stats.rooms_exported,
                target_stats.threads_exported,
                target_stats.threads_unchanged,
                target_stats.failures,
            ),
        ):
            continue
        logger.info(
            "Exported threads to agent workspace",
            agent_name=agent_target.agent_name,
            room_ids=None if full_pass else sorted(room_ids),
            rooms_exported=target_stats.rooms_exported,
            threads_exported=target_stats.threads_exported,
            threads_unchanged=target_stats.threads_unchanged,
            failures=target_stats.failures,
        )


def _build_targets(
    config: Config,
    runtime_paths: RuntimePaths,
    enabled: dict[str, AgentThreadExportConfig],
    agent_user_ids: dict[str, str],
) -> tuple[_AgentTarget, ...]:
    """Resolve every shared and private export target for the enabled agents."""
    agent_targets: list[_AgentTarget] = []
    for agent_name, options in enabled.items():
        agent_user_id = agent_user_ids.get(agent_name)
        if agent_user_id is None:
            continue
        if config.agents[agent_name].private is None:
            targets: tuple[ThreadExportTarget, ...] = (
                _shared_target(runtime_paths, agent_name, agent_user_id, options),
            )
        else:
            targets = _private_targets(config, runtime_paths, agent_name, agent_user_id, options)
        agent_targets.extend(_AgentTarget(agent_name, target) for target in targets)
    return tuple(agent_targets)


def _shared_target(
    runtime_paths: RuntimePaths,
    agent_name: str,
    agent_user_id: str,
    options: AgentThreadExportConfig,
) -> ThreadExportTarget:
    """Return the membership-scoped target for one shared agent."""
    return ThreadExportTarget(
        output_dir=_shared_export_dir(runtime_paths, agent_name),
        required_member_user_ids=(agent_user_id,),
        include_invited_rooms=options.invited_rooms,
        trusted_root=runtime_paths.storage_root,
    )


def _shared_export_dir(runtime_paths: RuntimePaths, agent_name: str) -> Path:
    return agent_workspace_root_path(runtime_paths.storage_root, agent_name) / _WORKSPACE_EXPORT_DIRNAME


def _private_targets(
    config: Config,
    runtime_paths: RuntimePaths,
    agent_name: str,
    agent_user_id: str,
    options: AgentThreadExportConfig,
) -> tuple[ThreadExportTarget, ...]:
    """Return one owner-scoped target per private instance whose core identity checks out."""
    targets: list[ThreadExportTarget] = []
    for state_root in _private_instance_state_roots(runtime_paths.storage_root, agent_name):
        try:
            owner = _private_instance_owner(config, runtime_paths, agent_name, state_root)
            output_dir = _private_export_dir(config, runtime_paths, agent_name, state_root)
        except OSError:
            # One unreadable instance must not cost every other target its pass; its
            # files stay as they are until a later pass can read the record again.
            logger.exception(
                "Skipping private instance whose identity could not be read",
                agent_name=agent_name,
                instance_root=str(state_root),
            )
            continue
        if owner is None:
            logger.warning(
                "Skipping private instance without valid core identity",
                agent_name=agent_name,
                instance_root=str(state_root),
            )
            if output_dir is not None:
                _clear_export_tree(runtime_paths, output_dir)
            continue
        if output_dir is None:
            logger.warning(
                "Skipping private instance without a resolvable workspace",
                agent_name=agent_name,
                instance_root=str(state_root),
            )
            continue
        required_member_user_ids = (owner,) if options.private_room_scope == "owner" else (owner, agent_user_id)
        targets.append(
            ThreadExportTarget(
                output_dir=output_dir,
                required_member_user_ids=required_member_user_ids,
                include_invited_rooms=options.invited_rooms,
                trusted_root=runtime_paths.storage_root,
            ),
        )
    return tuple(targets)


def _private_instance_state_roots(storage_root: Path, agent_name: str) -> tuple[Path, ...]:
    """Return existing private-instance state roots for one private agent."""
    instances_root = storage_root / _PRIVATE_INSTANCES_DIRNAME
    if not instances_root.is_dir() or instances_root.is_symlink():
        return ()
    instance_dir_names = {agent_name, agent_state_root_path(storage_root, agent_name).name}
    try:
        return tuple(
            sorted(
                state_root
                for scope_dir in instances_root.iterdir()
                if scope_dir.is_dir() and not scope_dir.is_symlink()
                for state_root in scope_dir.iterdir()
                if state_root.is_dir() and not state_root.is_symlink() and state_root.name in instance_dir_names
            ),
        )
    except OSError:
        logger.exception("Skipping private instance discovery", agent_name=agent_name)
        return ()


def _private_instance_owner(
    config: Config,
    runtime_paths: RuntimePaths,
    agent_name: str,
    state_root: Path,
) -> str | None:
    """Return the requester the core identity record names, when it owns exactly this root."""
    private = config.agents[agent_name].private
    assert private is not None
    try:
        identity = load_private_instance_identity(runtime_paths.storage_root, state_root.parent)
    except PrivateInstanceIdentityError:
        return None
    if identity is None:
        return None
    expected_state_root = private_instance_state_root_for_requester(
        runtime_paths.storage_root,
        requester_id=identity.requester_id,
        agent_name=agent_name,
        worker_scope=private.per,
        runtime_paths=runtime_paths,
    )
    if expected_state_root is None or expected_state_root.resolve() != state_root.resolve():
        return None
    return identity.requester_id


def _private_export_dir(
    config: Config,
    runtime_paths: RuntimePaths,
    agent_name: str,
    state_root: Path,
) -> Path | None:
    """Resolve one private instance's export directory inside its workspace."""
    try:
        workspace = resolve_agent_workspace_from_state_path(
            agent_name,
            config,
            runtime_paths=runtime_paths,
            state_storage_path=state_root,
            use_state_storage_path=True,
        )
    except ValueError:
        return None
    if workspace is None:
        return None
    return workspace.lexical_root / _WORKSPACE_EXPORT_DIRNAME


def _clear_export_tree(runtime_paths: RuntimePaths, output_dir: Path) -> None:
    """Remove one exporter-owned tree; a root without the ownership marker is left alone."""
    try:
        clear_thread_export_root(output_dir, trusted_root=runtime_paths.storage_root)
    except (OSError, RuntimeError):
        logger.warning("Skipping unsafe thread export cleanup", output_dir=str(output_dir))


def clear_workspace_thread_exports(config: Config, runtime_paths: RuntimePaths) -> None:
    """Remove every configured agent's workspace exports, for when no agent enables them any more."""
    _clear_disabled_agent_exports(config, runtime_paths, frozenset())


def _clear_disabled_agent_exports(
    config: Config,
    runtime_paths: RuntimePaths,
    enabled_agent_names: frozenset[str],
) -> None:
    """Remove exports for configured agents that no longer enable them."""
    for agent_name, agent_config in config.agents.items():
        if agent_name in enabled_agent_names:
            continue
        if agent_config.private is None:
            _clear_export_tree(runtime_paths, _shared_export_dir(runtime_paths, agent_name))
            continue
        for state_root in _private_instance_state_roots(runtime_paths.storage_root, agent_name):
            output_dir = _private_export_dir(config, runtime_paths, agent_name, state_root)
            if output_dir is not None:
                _clear_export_tree(runtime_paths, output_dir)
