"""Keep enabled agents' workspaces current with YAML thread exports.

Bots report room activity as the journal admits events; one always-running
task debounces those reports into single-flight export passes that read through
the live bots' clients and projection views, so no pass logs in or opens a
journal of its own. Storage I/O runs off the event loop inside
``export_threads_to_sources``, and target discovery runs off it here.
"""

from __future__ import annotations

import asyncio
from dataclasses import dataclass, replace
from typing import TYPE_CHECKING, Protocol

from mindroom.logging_config import get_logger
from mindroom.private_instance_identity import private_instances_for_agent
from mindroom.thread_export.models import ThreadExportRoom, ThreadExportSource, ThreadExportTarget
from mindroom.thread_export.projected_history import export_conversation_reader
from mindroom.thread_export.selection import export_rooms
from mindroom.thread_export.service import export_threads_to_sources
from mindroom.thread_export.storage import clear_thread_export_root
from mindroom.tool_system.worker_routing import agent_workspace_root_path
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
_DEBOUNCE_SECONDS = 2.0


class _ThreadExportBot(Protocol):
    """What the runner reads from a running bot."""

    running: bool
    client: nio.AsyncClient | None
    rooms: list[str]

    @property
    def matrix_id(self) -> MatrixID:
        """Return the bot's Matrix identity."""
        ...

    @property
    def approval_room_ids(self) -> frozenset[str]:
        """Return the bot's configured and durably invited room IDs."""
        ...

    def journal_principal(self) -> PrincipalStore:
        """Return the bot's principal-bound projection view."""
        ...


@dataclass(frozen=True)
class WorkspaceThreadExportDeps:
    """Runtime collaborators the workspace export runner reads through."""

    runtime_paths: RuntimePaths
    config_provider: Callable[[], Config | None]
    bot_provider: Callable[[str], _ThreadExportBot | None]
    debounce_seconds: float = _DEBOUNCE_SECONDS


class WorkspaceThreadExportRunner:
    """Debounce room activity into single-flight export passes.

    The runner exists for the whole orchestrator lifetime. A full pass with no
    agent enabling exports is the cleanup sweep for agents that used to, after
    which the loop idles on its event.
    """

    def __init__(self, deps: WorkspaceThreadExportDeps) -> None:
        self._deps = deps
        self._pending_room_ids: set[str] = set()
        self._full_pass_pending = False
        self._wakeup = asyncio.Event()
        self._task: asyncio.Task[None] | None = None

    def start(self) -> None:
        """Start the loop once; later calls are no-ops."""
        if self._task is None:
            self._task = asyncio.create_task(self._run(), name="thread_export_workspace_sync")

    async def stop(self) -> None:
        """Cancel the loop, abandoning a pass in flight; every write it makes is atomic."""
        task = self._task
        self._task = None
        if task is not None:
            task.cancel()
            await asyncio.gather(task, return_exceptions=True)

    def mark_room_activity(self, room_id: str) -> None:
        """Queue one room for re-export."""
        self._pending_room_ids.add(room_id)
        self._wakeup.set()

    def queue_full_pass(self) -> None:
        """Queue a pass over every room that also reconciles removed threads, rooms, and agents."""
        self._full_pass_pending = True
        self._wakeup.set()

    async def _run(self) -> None:
        while True:
            await self._wakeup.wait()
            self._wakeup.clear()
            if self._deps.debounce_seconds > 0:
                await asyncio.sleep(self._deps.debounce_seconds)
            await self._run_pass_once()

    async def _run_pass_once(self) -> None:
        """Consume the pending work and run one pass.

        Per-room Matrix and journal failures are caught inside the pass and
        recorded as target failures. An exception that escapes is a fault in
        this process or its disk, so the work is kept for the next trigger
        rather than retried on a timer: a reload, a restart, or the next
        message in a room all run it again.
        """
        full_pass, room_ids = self._full_pass_pending, frozenset(self._pending_room_ids)
        self._full_pass_pending = False
        self._pending_room_ids.clear()
        if not full_pass and not room_ids:
            return
        config = self._deps.config_provider()
        if config is None:
            return
        try:
            await self._run_pass(config, full_pass=full_pass, room_ids=room_ids)
        except Exception:
            logger.exception("Thread export pass crashed")
            self._full_pass_pending |= full_pass
            self._pending_room_ids |= room_ids

    async def _run_pass(self, config: Config, *, full_pass: bool, room_ids: frozenset[str]) -> None:
        """Export each agent's rooms through that agent into its own workspace."""
        runtime_paths = self._deps.runtime_paths
        enabled = {
            name: agent.thread_exports for name, agent in config.agents.items() if agent.thread_exports is not None
        }
        if full_pass:
            await asyncio.to_thread(_clear_disabled_agent_exports, config, runtime_paths, frozenset(enabled))
        active_bots = {
            agent_name: bot
            for agent_name in enabled
            if (bot := self._deps.bot_provider(agent_name)) is not None and bot.running and bot.client is not None
        }
        for agent_name in enabled.keys() - active_bots.keys():
            logger.warning("Skipping thread exports for agent without a running bot", agent_name=agent_name)
        target_groups = await asyncio.to_thread(
            _build_target_groups,
            config,
            runtime_paths,
            enabled,
            active_bots,
        )
        targets = tuple(target for group in target_groups.values() for target in group)
        if not targets:
            return
        state_rooms = await asyncio.to_thread(export_rooms, runtime_paths, None)
        sources: list[ThreadExportSource] = []
        for agent_name, agent_targets in target_groups.items():
            bot = active_bots[agent_name]
            rooms = await asyncio.to_thread(
                _select_agent_rooms,
                bot.rooms,
                bot.approval_room_ids,
                state_rooms,
            )
            if not full_pass:
                rooms = [room for room in rooms if room.room_id in room_ids]
            sources.append(_source_for_bot(bot, tuple(rooms), agent_targets, config))
        stats = await export_threads_to_sources(
            config=config,
            runtime_paths=runtime_paths,
            sources=sources,
            targets=targets,
            full_pass=full_pass,
        )
        _log_pass(stats, room_ids=None if full_pass else sorted(room_ids))


def _select_agent_rooms(
    configured_room_ids: Sequence[str],
    available_room_ids: frozenset[str],
    state_rooms: Sequence[ThreadExportRoom],
) -> list[ThreadExportRoom]:
    """Return configured and invited rooms available to one live agent account."""
    configured_ids = tuple(dict.fromkeys(configured_room_ids))
    configured_id_set = frozenset(configured_ids)
    state_rooms_by_id = {room.room_id: room for room in state_rooms}
    ordered_room_ids = (
        *(room_id for room_id in configured_ids if room_id in available_room_ids),
        *sorted(available_room_ids - configured_id_set),
    )
    return [
        replace(
            state_rooms_by_id.get(
                room_id,
                ThreadExportRoom(key=room_id, room_id=room_id, alias="", name=""),
            ),
            invited=room_id not in configured_id_set,
        )
        for room_id in ordered_room_ids
    ]


def _source_for_bot(
    bot: _ThreadExportBot,
    rooms: tuple[ThreadExportRoom, ...],
    targets: tuple[ThreadExportTarget, ...],
    config: Config,
) -> ThreadExportSource:
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
        target_output_dirs=tuple(target.output_dir for target in targets),
    )


def _log_pass(stats: Sequence[ThreadExportStats], *, room_ids: list[str] | None) -> None:
    """Summarize one pass, and name every target that recorded a failure."""
    logger.info(
        "Exported threads to agent workspaces",
        room_ids=room_ids,
        targets=len(stats),
        rooms_exported=sum(target_stats.rooms_exported for target_stats in stats),
        threads_exported=sum(target_stats.threads_exported for target_stats in stats),
        threads_unchanged=sum(target_stats.threads_unchanged for target_stats in stats),
        failures=sum(target_stats.failures for target_stats in stats),
    )
    for target_stats in stats:
        if target_stats.failures:
            logger.warning(
                "Thread export target recorded failures",
                output_dir=str(target_stats.output_dir),
                failures=[failure.error for failure in target_stats.failed_items],
            )


def _build_target_groups(
    config: Config,
    runtime_paths: RuntimePaths,
    enabled: dict[str, AgentThreadExportConfig],
    active_bots: dict[str, _ThreadExportBot],
) -> dict[str, tuple[ThreadExportTarget, ...]]:
    """Resolve each active agent's shared or private export targets."""
    groups: dict[str, tuple[ThreadExportTarget, ...]] = {}
    for agent_name, options in enabled.items():
        bot = active_bots.get(agent_name)
        if bot is None:
            continue
        agent_user_id = bot.matrix_id.full_id
        if config.agents[agent_name].private is None:
            targets = (_shared_target(runtime_paths, agent_name, agent_user_id, options),)
        else:
            targets = tuple(_private_targets(config, runtime_paths, agent_name, agent_user_id, options))
        if targets:
            groups[agent_name] = targets
    return groups


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
) -> list[ThreadExportTarget]:
    """Return one owner-scoped target per private instance whose core identity checks out.

    An instance without a valid owner gets its export tree cleared instead: nothing can
    run as that instance, so nothing should keep reading conversations there.
    """
    private = config.agents[agent_name].private
    assert private is not None
    targets: list[ThreadExportTarget] = []
    for instance in private_instances_for_agent(runtime_paths.storage_root, agent_name, private.per):
        output_dir = _private_export_dir(config, runtime_paths, agent_name, instance.state_root)
        if output_dir is None:
            logger.warning(
                "Skipping private instance without a resolvable workspace",
                agent_name=agent_name,
                instance_root=str(instance.state_root),
            )
            continue
        if instance.requester_id is None:
            logger.warning(
                "Clearing exports of private instance without valid core identity",
                agent_name=agent_name,
                instance_root=str(instance.state_root),
            )
            _clear_export_tree(runtime_paths, output_dir)
            continue
        required_member_user_ids = (instance.requester_id,)
        if options.private_room_scope == "owner_and_agent":
            required_member_user_ids += (agent_user_id,)
        targets.append(
            ThreadExportTarget(
                output_dir=output_dir,
                required_member_user_ids=required_member_user_ids,
                include_invited_rooms=options.invited_rooms,
                trusted_root=runtime_paths.storage_root,
            ),
        )
    return targets


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
        for instance in private_instances_for_agent(runtime_paths.storage_root, agent_name, agent_config.private.per):
            output_dir = _private_export_dir(config, runtime_paths, agent_name, instance.state_root)
            if output_dir is not None:
                _clear_export_tree(runtime_paths, output_dir)
