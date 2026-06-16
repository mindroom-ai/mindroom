"""Runtime supervision for workspace-authored automations."""

from __future__ import annotations

import asyncio
import json
from collections.abc import Awaitable, Callable, Mapping, Sequence
from dataclasses import asdict, dataclass, field, replace
from datetime import UTC, datetime
from typing import TYPE_CHECKING, Any, Protocol

from croniter import croniter

from mindroom.constants import ROUTER_AGENT_NAME
from mindroom.hooks import build_hook_message_sender
from mindroom.logging_config import get_logger
from mindroom.workspace_automations.actions import WorkspaceAutomationActionResult, run_automation_action
from mindroom.workspace_automations.executor import ShellCheckResult, run_shell_check
from mindroom.workspace_automations.loader import load_workspace_automations
from mindroom.workspace_automations.models import (
    LoadedWorkspaceAutomation,
    WorkspaceAutomationLoadError,
    WorkspaceAutomationLoadResult,
    WorkspaceAutomationTrigger,
)
from mindroom.workspace_automations.targets import WorkspaceAutomationTarget, iter_workspace_automation_targets
from mindroom.workspace_automations.triggers import trigger_matches

if TYPE_CHECKING:
    from pathlib import Path

    import nio

    from mindroom.config.main import Config
    from mindroom.constants import RuntimePaths
    from mindroom.hooks import HookMessageSender, HookRegistry
    from mindroom.matrix.conversation_cache import ConversationCacheProtocol

_LOGGER = get_logger(__name__)
_DEFAULT_SCAN_INTERVAL_SECONDS = 30.0
_DEFAULT_MAX_SLEEP_SECONDS = 30.0


class _BotWithClient(Protocol):
    client: nio.AsyncClient | None


type _BotProvider = Callable[[str], _BotWithClient | None]
type _TargetLoader = Callable[["Config", "RuntimePaths"], Sequence[WorkspaceAutomationTarget]]
type _AutomationLoader = Callable[..., WorkspaceAutomationLoadResult]
type _CheckRunner = Callable[..., Awaitable[ShellCheckResult]]
type _TriggerMatcher = Callable[[WorkspaceAutomationTrigger | None, ShellCheckResult], bool]
type _ActionRunner = Callable[..., Awaitable[WorkspaceAutomationActionResult]]
type _Sleep = Callable[[float], Awaitable[None]]
type _Now = Callable[[], datetime]
type _TaskFactory = Callable[..., asyncio.Task[None]]
type _MessageSenderBuilder = Callable[..., "HookMessageSender"]


@dataclass(frozen=True, slots=True)
class AutomationKey:
    """Stable identifier for one loaded workspace automation."""

    agent_name: str
    automation_id: str
    workspace_root: str

    @classmethod
    def from_loaded(cls, automation: LoadedWorkspaceAutomation) -> AutomationKey:
        """Build a stable key from a normalized loaded automation."""
        return cls(
            agent_name=automation.agent_name,
            automation_id=automation.automation_id,
            workspace_root=str(automation.workspace_root),
        )

    def state_id(self) -> str:
        """Return a compact JSON object key for the persisted status map."""
        return f"{self.agent_name}:{self.automation_id}:{self.workspace_root}"


@dataclass(frozen=True, slots=True)
class WorkspaceAutomationLoadedStatus:
    """Public status summary for one loaded automation."""

    agent_name: str
    automation_id: str
    workspace_root: str
    schedule: str
    last_status: str | None = None
    last_run_at: str | None = None
    last_exit_code: int | None = None
    last_error: str | None = None
    last_event_id: str | None = None


@dataclass(frozen=True, slots=True)
class WorkspaceAutomationScanResult:
    """Summary of one automation scan."""

    loaded_count: int
    error_count: int
    errors: tuple[WorkspaceAutomationLoadError, ...] = ()


@dataclass(frozen=True, slots=True)
class _LoadedAutomationEntry:
    target: WorkspaceAutomationTarget
    automation: LoadedWorkspaceAutomation


@dataclass(frozen=True, slots=True)
class _RunStatus:
    agent_name: str
    automation_id: str
    workspace_root: str
    last_status: str
    last_run_at: str
    last_exit_code: int | None = None
    last_error: str | None = None
    last_event_id: str | None = None


@dataclass(frozen=True, slots=True)
class _ServiceContext:
    config: Config
    runtime_paths: RuntimePaths
    hook_registry: HookRegistry
    bot_provider: _BotProvider
    conversation_cache: ConversationCacheProtocol


@dataclass
class WorkspaceAutomationService:
    """Load, schedule, and supervise workspace automations."""

    target_loader: _TargetLoader = iter_workspace_automation_targets
    automation_loader: _AutomationLoader = load_workspace_automations
    check_runner: _CheckRunner = run_shell_check
    trigger_matcher: _TriggerMatcher = trigger_matches
    action_runner: _ActionRunner = run_automation_action
    message_sender_builder: _MessageSenderBuilder = build_hook_message_sender
    now: _Now = lambda: datetime.now(UTC)
    sleep: _Sleep = asyncio.sleep
    task_factory: _TaskFactory = asyncio.create_task
    scan_interval_seconds: float | None = _DEFAULT_SCAN_INTERVAL_SECONDS
    max_sleep_seconds: float = _DEFAULT_MAX_SLEEP_SECONDS
    _loaded: dict[AutomationKey, _LoadedAutomationEntry] = field(default_factory=dict, init=False, repr=False)
    _tasks: dict[AutomationKey, asyncio.Task[None]] = field(default_factory=dict, init=False, repr=False)
    _run_status: dict[AutomationKey, _RunStatus] = field(default_factory=dict, init=False, repr=False)
    _state_file_lock: asyncio.Lock = field(default_factory=asyncio.Lock, init=False, repr=False)
    _context: _ServiceContext | None = field(default=None, init=False, repr=False)
    _scan_task: asyncio.Task[None] | None = field(default=None, init=False, repr=False)
    _shutting_down: bool = field(default=False, init=False, repr=False)

    @property
    def is_started(self) -> bool:
        """Return whether the service currently owns live runtime context."""
        return self._context is not None and not self._shutting_down

    async def start(
        self,
        config: Config,
        runtime_paths: RuntimePaths,
        hook_registry: HookRegistry,
        bot_provider: _BotProvider,
        conversation_cache: ConversationCacheProtocol,
    ) -> WorkspaceAutomationScanResult:
        """Start the service and perform the initial automation scan."""
        if self._context is not None and not self._shutting_down:
            return await self.refresh(config, hook_registry, bot_provider, conversation_cache)
        self._shutting_down = False
        self._context = _ServiceContext(
            config=config,
            runtime_paths=runtime_paths,
            hook_registry=hook_registry,
            bot_provider=bot_provider,
            conversation_cache=conversation_cache,
        )
        result = await self.scan_now()
        if self.scan_interval_seconds is not None:
            self._scan_task = self._create_task(self._scan_loop(), name="workspace_automation_scan_loop")
        return result

    async def refresh(
        self,
        config: Config,
        hook_registry: HookRegistry,
        bot_provider: _BotProvider,
        conversation_cache: ConversationCacheProtocol,
    ) -> WorkspaceAutomationScanResult:
        """Refresh config-bound dependencies and reconcile loaded automations."""
        context = self._require_context()
        self._context = replace(
            context,
            config=config,
            hook_registry=hook_registry,
            bot_provider=bot_provider,
            conversation_cache=conversation_cache,
        )
        return await self.scan_now()

    async def shutdown(self) -> None:
        """Cancel all background work and clear loaded runtime state."""
        self._shutting_down = True
        scan_task = self._scan_task
        self._scan_task = None
        if scan_task is not None:
            scan_task.cancel()
            await asyncio.gather(scan_task, return_exceptions=True)

        tasks = tuple(self._tasks.values())
        self._tasks.clear()
        self._loaded.clear()
        self._run_status.clear()
        self._context = None
        for task in tasks:
            task.cancel()
        if tasks:
            await asyncio.gather(*tasks, return_exceptions=True)

    async def scan_now(self) -> WorkspaceAutomationScanResult:
        """Reload automation files and reconcile supervised cron loops."""
        context = self._require_context()
        loaded_entries: dict[AutomationKey, _LoadedAutomationEntry] = {}
        errors: list[WorkspaceAutomationLoadError] = []

        for target in self.target_loader(context.config, context.runtime_paths):
            result = self.automation_loader(
                agent_name=target.agent_name,
                workspace_root=target.workspace_root,
                agent_rooms=target.agent_configured_rooms,
                policy=target.policy,
            )
            errors.extend(result.errors)
            for automation in result.automations:
                key = AutomationKey.from_loaded(automation)
                loaded_entries[key] = _LoadedAutomationEntry(target=target, automation=automation)

        self._log_load_errors(errors)
        await self._reconcile_tasks(loaded_entries)
        return WorkspaceAutomationScanResult(
            loaded_count=len(loaded_entries),
            error_count=len(errors),
            errors=tuple(errors),
        )

    def list_loaded(self) -> tuple[WorkspaceAutomationLoadedStatus, ...]:
        """Return a stable snapshot of loaded automations and their latest statuses."""
        items: list[WorkspaceAutomationLoadedStatus] = []
        for key, entry in sorted(self._loaded.items(), key=lambda item: item[0].state_id()):
            status = self._run_status.get(key)
            items.append(
                WorkspaceAutomationLoadedStatus(
                    agent_name=key.agent_name,
                    automation_id=key.automation_id,
                    workspace_root=key.workspace_root,
                    schedule=entry.automation.schedule,
                    last_status=status.last_status if status is not None else None,
                    last_run_at=status.last_run_at if status is not None else None,
                    last_exit_code=status.last_exit_code if status is not None else None,
                    last_error=status.last_error if status is not None else None,
                    last_event_id=status.last_event_id if status is not None else None,
                ),
            )
        return tuple(items)

    def _require_context(self) -> _ServiceContext:
        context = self._context
        if context is None:
            msg = "WorkspaceAutomationService has not been started"
            raise RuntimeError(msg)
        return context

    async def _reconcile_tasks(self, loaded_entries: Mapping[AutomationKey, _LoadedAutomationEntry]) -> None:
        removed_keys = set(self._loaded) - set(loaded_entries)
        for key in sorted(removed_keys, key=AutomationKey.state_id):
            await self._cancel_task(key)
            self._run_status.pop(key, None)

        self._loaded = dict(loaded_entries)

        for key in sorted(self._loaded, key=AutomationKey.state_id):
            task = self._tasks.get(key)
            if task is None or task.done():
                self._tasks[key] = self._create_task(
                    self._automation_loop(key),
                    name=f"workspace_automation:{key.agent_name}:{key.automation_id}",
                )

    async def _cancel_task(self, key: AutomationKey) -> None:
        task = self._tasks.pop(key, None)
        if task is None:
            return
        task.cancel()
        await asyncio.gather(task, return_exceptions=True)

    async def _scan_loop(self) -> None:
        try:
            while not self._shutting_down:
                interval = self.scan_interval_seconds
                if interval is None:
                    return
                await self.sleep(interval)
                if self._shutting_down:
                    return
                await self.scan_now()
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            _LOGGER.exception("Workspace automation scan loop failed", error=str(exc))

    async def _automation_loop(self, key: AutomationKey) -> None:
        try:
            while not self._shutting_down:
                entry = self._loaded.get(key)
                if entry is None:
                    return
                next_fire = croniter(entry.automation.schedule, self.now()).get_next(datetime)
                if next_fire.tzinfo is None:
                    next_fire = next_fire.replace(tzinfo=UTC)
                await self._sleep_until(next_fire)
                if self._shutting_down:
                    return

                entry = self._loaded.get(key)
                if entry is None:
                    return
                await self._run_automation_once(key, entry)
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            _LOGGER.exception(
                "Workspace automation loop failed",
                agent_name=key.agent_name,
                automation_id=key.automation_id,
                workspace_root=key.workspace_root,
                error=str(exc),
            )

    async def _sleep_until(self, next_fire: datetime) -> None:
        while not self._shutting_down:
            delay_seconds = (next_fire - self.now()).total_seconds()
            if delay_seconds <= 0:
                return
            await self.sleep(min(delay_seconds, self.max_sleep_seconds))

    async def _run_automation_once(self, key: AutomationKey, entry: _LoadedAutomationEntry) -> None:
        context = self._require_context()
        automation = entry.automation
        check_result = await self.check_runner(
            config=context.config,
            runtime_paths=context.runtime_paths,
            target=entry.target,
            automation=automation,
        )
        matched = self.trigger_matcher(automation.trigger, check_result)
        if not matched:
            await self._record_status(
                key,
                _RunStatus(
                    agent_name=key.agent_name,
                    automation_id=key.automation_id,
                    workspace_root=key.workspace_root,
                    last_status="not_matched",
                    last_run_at=self._now_iso(),
                    last_exit_code=check_result.exit_code,
                    last_error=check_result.error,
                ),
            )
            return

        action_result = await self.action_runner(
            config=context.config,
            runtime_paths=context.runtime_paths,
            target=entry.target,
            automation=automation,
            check_result=check_result,
            hook_registry=context.hook_registry,
            message_sender=self._message_sender(context),
            trigger_payload=_trigger_payload(automation),
        )
        await self._record_status(
            key,
            _RunStatus(
                agent_name=key.agent_name,
                automation_id=key.automation_id,
                workspace_root=key.workspace_root,
                last_status="action_succeeded" if action_result.ok else "action_failed",
                last_run_at=self._now_iso(),
                last_exit_code=check_result.exit_code,
                last_error=check_result.error or action_result.failure_reason,
                last_event_id=action_result.event_id,
            ),
        )

    def _message_sender(self, context: _ServiceContext) -> HookMessageSender | None:
        router_bot = context.bot_provider(ROUTER_AGENT_NAME)
        if router_bot is None or router_bot.client is None:
            return None
        return self.message_sender_builder(
            router_bot.client,
            context.config,
            context.runtime_paths,
            conversation_cache=context.conversation_cache,
        )

    async def _record_status(self, key: AutomationKey, status: _RunStatus) -> None:
        self._run_status[key] = status
        await self._write_state_file()

    async def _write_state_file(self) -> None:
        context = self._require_context()
        state_path = context.runtime_paths.storage_root / "workspace_automations" / "state.json"
        payload = {
            "automations": {
                key.state_id(): _status_payload(status)
                for key, status in sorted(self._run_status.items(), key=lambda item: item[0].state_id())
            },
        }
        async with self._state_file_lock:
            await asyncio.to_thread(_write_json_file, state_path, payload)

    def _now_iso(self) -> str:
        now = self.now()
        if now.tzinfo is None:
            now = now.replace(tzinfo=UTC)
        return now.isoformat()

    def _create_task(self, awaitable: Awaitable[None], *, name: str) -> asyncio.Task[None]:
        return self.task_factory(awaitable, name=name)

    @staticmethod
    def _log_load_errors(errors: Sequence[WorkspaceAutomationLoadError]) -> None:
        for error in errors:
            _LOGGER.warning(
                "Workspace automation load error",
                file_path=str(error.file_path),
                automation_id=error.automation_id,
                field_path=".".join(str(part) for part in error.field_path),
                message=error.message,
            )


def _trigger_payload(automation: LoadedWorkspaceAutomation) -> dict[str, Any]:
    if automation.trigger is None:
        return {}
    return automation.trigger.model_dump(exclude_none=True)


def _status_payload(status: _RunStatus) -> dict[str, object]:
    return {key: value for key, value in asdict(status).items() if value is not None}


def _write_json_file(path: Path, payload: Mapping[str, object]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(f"{json.dumps(payload, indent=2, sort_keys=True)}\n", encoding="utf-8")


__all__ = [
    "AutomationKey",
    "WorkspaceAutomationLoadedStatus",
    "WorkspaceAutomationScanResult",
    "WorkspaceAutomationService",
]
