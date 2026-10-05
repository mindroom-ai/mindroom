"""Decide when an agent's prompt files are curated, and run each pass in the background.

The completed-response memory handoff calls ``maybe_start`` after each successful agent response.
It only starts a background task, so a reply is never delayed.
The task measures the curatable files and, when a pass is due, runs the agent's own model with the pass's file
tools on a staged copy of the workspace, validates the result in code, and publishes it.
A pass has no Matrix client, so it posts nothing that people or the router could see; its log lines carry
``caller_label=prompt_curation``.
Attempts, the hysteresis flag, and failure backoff persist in ``prompt_curation_state.json`` in the storage root,
so a restart neither forgets a cooldown nor retries a failing pass in a loop.
"""

from __future__ import annotations

import asyncio
import contextvars
import json
import threading
from dataclasses import asdict, dataclass, field, replace
from datetime import UTC, datetime
from functools import partial
from typing import TYPE_CHECKING, Literal, cast, get_args
from uuid import uuid4

from agno.agent import Agent
from agno.run.base import RunStatus

from mindroom import model_loading
from mindroom.agent_storage import create_session_storage
from mindroom.background_tasks import create_background_task
from mindroom.helper_usage import HelperUsageOwner, record_helper_usage
from mindroom.history.storage import new_scope_session
from mindroom.llm_request_logging import bind_llm_request_log_context
from mindroom.logging_config import bound_log_context, get_logger
from mindroom.memory import schedule_agent_memory_refresh
from mindroom.path_confinement import write_file_within_root
from mindroom.prompt_curation.policy import (
    MEMORY_DIR_PREFIX,
    max_content_loss_tokens,
    next_active,
    plan_pass,
    validate_pass,
)
from mindroom.prompt_curation.staging import StagedWorkspace, curatable_tokens
from mindroom.prompt_curation.tools import PromptCurationTools
from mindroom.runtime_resolution import resolve_agent_execution, resolve_agent_runtime
from mindroom.token_budget import estimate_text_tokens
from mindroom.tool_call_budget import install_model_call_cap

if TYPE_CHECKING:
    from pathlib import Path

    from agno.run.agent import RunOutput

    from mindroom.config.main import Config
    from mindroom.config.prompt_curation import PromptCurationConfig
    from mindroom.constants import RuntimePaths
    from mindroom.prompt_curation.policy import PassBounds, PassMeasurement
    from mindroom.tool_system.worker_routing import ToolExecutionIdentity

logger = get_logger(__name__)

_STATE_FILENAME = "prompt_curation_state.json"
_STATE_LOCK = threading.Lock()
# Each failed pass doubles the cooldown, up to 8x.
_MAX_BACKOFF_DOUBLINGS = 3
# Moving one section takes two calls, so a pass can move ten sections; with install_model_call_cap the pass ends
# after 22 model requests, each of which resends the curated files.
_TOOL_CALL_LIMIT = 20
_MEMORY_LISTING_LIMIT = 200

type _PassOutcome = Literal["accepted", "rejected", "conflict", "failed", "timeout"]
_OUTCOMES = frozenset(get_args(_PassOutcome.__value__))


@dataclass(frozen=True)
class _ScopeState:
    """Persisted curation state of one agent workspace."""

    active: bool = False
    last_attempt_at: int | None = None
    consecutive_failures: int = 0
    last_outcome: _PassOutcome | None = None

    @classmethod
    def from_payload(cls, payload: object) -> _ScopeState:
        """Read one persisted entry, ignoring fields of the wrong type."""
        if not isinstance(payload, dict):
            return cls()
        entry = cast("dict[str, object]", payload)
        active = entry.get("active")
        last_attempt_at = entry.get("last_attempt_at")
        failures = entry.get("consecutive_failures")
        outcome = entry.get("last_outcome")
        return cls(
            active=active is True,
            last_attempt_at=last_attempt_at if isinstance(last_attempt_at, int) else None,
            consecutive_failures=failures if isinstance(failures, int) and failures > 0 else 0,
            last_outcome=cast("_PassOutcome", outcome) if outcome in _OUTCOMES else None,
        )


@dataclass(frozen=True)
class _CurationScope:
    """One agent workspace and the response whose completion checked it."""

    agent_name: str
    session_id: str
    identity: ToolExecutionIdentity | None
    key: str


def _now() -> int:
    return int(datetime.now(UTC).timestamp())


def _scope_key(config: Config, agent_name: str, identity: ToolExecutionIdentity | None) -> str:
    """Return the key of the agent workspace a response used: one per private worker scope."""
    execution = resolve_agent_execution(agent_name, config, execution_identity=identity)
    return f"{agent_name}:{execution.worker_key}" if execution.is_private else agent_name


def _read_states(storage_root: Path) -> dict[str, object]:
    try:
        data = json.loads((storage_root / _STATE_FILENAME).read_text(encoding="utf-8"))
    except FileNotFoundError:
        return {}
    except (OSError, ValueError):
        logger.warning("Invalid prompt curation state; starting fresh")
        return {}
    scopes = data.get("scopes") if isinstance(data, dict) else None
    return cast("dict[str, object]", scopes) if isinstance(scopes, dict) else {}


def _load_scope_state(storage_root: Path, key: str) -> _ScopeState:
    with _STATE_LOCK:
        return _ScopeState.from_payload(_read_states(storage_root).get(key))


def _save_scope_state(storage_root: Path, key: str, state: _ScopeState) -> None:
    with _STATE_LOCK:
        states = _read_states(storage_root)
        states[key] = asdict(state)
        payload = json.dumps({"version": 1, "scopes": states}, indent=2, sort_keys=True)
        write_file_within_root(storage_root, _STATE_FILENAME, f"{payload}\n".encode())


def _cooldown_seconds(settings: PromptCurationConfig, failures: int) -> int:
    return round(settings.cooldown_hours * 3600 * 2 ** min(failures, _MAX_BACKOFF_DOUBLINGS))


def _due_at(state: _ScopeState, settings: PromptCurationConfig) -> int:
    if state.last_attempt_at is None:
        return 0
    return state.last_attempt_at + _cooldown_seconds(settings, state.consecutive_failures)


@dataclass(frozen=True)
class _DuePass:
    """A pass that is due: its staged workspace, starting sizes, and band."""

    staged: StagedWorkspace
    before: PassMeasurement
    bounds: PassBounds
    context_window: int | None


def _due_pass(
    config: Config,
    runtime_paths: RuntimePaths,
    scope: _CurationScope,
    settings: PromptCurationConfig,
    *,
    active: bool,
) -> _DuePass | None:
    """Return the pass that is due for a workspace, reading its large memory/ only when one is."""
    root = resolve_agent_runtime(
        scope.agent_name,
        config,
        runtime_paths,
        execution_identity=scope.identity,
    ).file_memory_root
    context_window = config.get_model_context_window(config.resolve_entity(scope.agent_name).model_name)
    if root is None or plan_pass(curatable_tokens(root, settings), settings, context_window, active=active) is None:
        return None
    staged = StagedWorkspace.load(root, settings)
    before = staged.measurement()
    bounds = plan_pass(before.curated_tokens, settings, context_window, active=active)
    return None if bounds is None else _DuePass(staged, before, bounds, context_window)


def _curation_prompt(
    config: Config,
    agent_name: str,
    staged: StagedWorkspace,
    before: PassMeasurement,
    bounds: PassBounds,
    settings: PromptCurationConfig,
) -> str:
    memory_paths = sorted(path for path in staged.baseline if path.startswith(MEMORY_DIR_PREFIX))
    listing = [
        f"- {path} ({estimate_text_tokens(staged.read(path))} tokens)" for path in memory_paths[:_MEMORY_LISTING_LIMIT]
    ]
    if len(memory_paths) > _MEMORY_LISTING_LIMIT:
        listing.append(f"- ...and {len(memory_paths) - _MEMORY_LISTING_LIMIT} more")
    curated_files = "\n\n".join(
        f'<file path="{path}" tokens="{before.curated[path]}">\n{staged.read(path)}\n</file>' for path in staged.curated
    )
    return config.render_prompt(
        "PROMPT_CURATION_PROMPT_TEMPLATE",
        agent_name=agent_name,
        measured_tokens=bounds.measured_tokens,
        upper_tokens=bounds.upper_tokens,
        floor_tokens=bounds.floor_tokens,
        max_file_shrink_percent=round(100 * settings.max_file_shrink),
        max_loss_tokens=max_content_loss_tokens(before, settings),
        memory_files="\n".join(listing) or "(none)",
        curated_files=curated_files,
    )


@dataclass
class PromptCurationRunner:
    """Own the process's curation passes, at most one running per agent workspace."""

    runtime_paths: RuntimePaths
    _tasks: dict[str, asyncio.Task[_PassOutcome | None]] = field(default_factory=dict, init=False)
    # Workspaces known to be in cooldown, so a busy agent does not re-read its state after every turn.
    _not_before: dict[str, int] = field(default_factory=dict, init=False)
    _stopped: bool = field(default=False, init=False)

    def maybe_start(
        self,
        config: Config,
        *,
        agent_name: str,
        session_id: str,
        identity: ToolExecutionIdentity | None,
    ) -> asyncio.Task[_PassOutcome | None] | None:
        """Check a file-memory agent's workspace in the background after a completed response.

        Returns the started task, or None when curation is off, already running, or cooling down.
        """
        if self._stopped or agent_name not in config.agents:
            return None
        entity = config.resolve_entity(agent_name)
        if entity.memory_backend != "file" or not entity.prompt_curation.enabled:
            return None
        scope = _CurationScope(agent_name, session_id, identity, _scope_key(config, agent_name, identity))
        running = self._tasks.get(scope.key)
        if (running is not None and not running.done()) or self._not_before.get(scope.key, 0) > _now():
            return None
        task = create_background_task(
            self._run(config, scope),
            name=f"prompt_curation:{agent_name}",
            # The response's context carries its log, usage, and tool bindings, which must not label the pass.
            context=contextvars.Context(),
        )
        self._tasks[scope.key] = task
        task.add_done_callback(lambda done: self._tasks.pop(scope.key) if self._tasks.get(scope.key) is done else None)
        return task

    async def stop(self) -> None:
        """Cancel every running pass; a cancelled pass publishes nothing."""
        self._stopped = True
        tasks = list(self._tasks.values())
        for task in tasks:
            task.cancel()
        await asyncio.gather(*tasks, return_exceptions=True)

    async def _run(self, config: Config, scope: _CurationScope) -> _PassOutcome | None:
        identity = scope.identity
        log_context = {
            "agent_id": scope.agent_name,
            "session_id": scope.session_id,
            "requester_id": identity.requester_id if identity is not None else None,
            "caller_label": "prompt_curation",
        }
        log_context = {key: value for key, value in log_context.items() if value is not None}
        with bound_log_context(**log_context), bind_llm_request_log_context(**log_context):
            return await self._check(config, scope)

    async def _check(self, config: Config, scope: _CurationScope) -> _PassOutcome | None:
        settings = config.resolve_entity(scope.agent_name).prompt_curation
        storage_root = self.runtime_paths.storage_root
        now = _now()
        state = await asyncio.to_thread(_load_scope_state, storage_root, scope.key)
        if (due_at := _due_at(state, settings)) > now:
            self._not_before[scope.key] = due_at
            return None
        active = state.active
        try:
            due = await asyncio.to_thread(_due_pass, config, self.runtime_paths, scope, settings, active=active)
        except (OSError, ValueError) as exc:
            logger.warning("Prompt curation skipped: curatable files are unreadable", error=str(exc))
            self._not_before[scope.key] = now + _cooldown_seconds(settings, 0)
            return None
        if due is None:
            if active:
                await asyncio.to_thread(_save_scope_state, storage_root, scope.key, replace(state, active=False))
            return None
        # The attempt is recorded before the pass, so a crash or restart mid-pass still waits out the cooldown.
        await asyncio.to_thread(
            _save_scope_state,
            storage_root,
            scope.key,
            replace(state, active=True, last_attempt_at=now),
        )
        outcome, tokens_after = await self._curate(config, scope, due, settings)
        failures = state.consecutive_failures
        failures = 0 if outcome == "accepted" else failures if outcome == "conflict" else failures + 1
        await asyncio.to_thread(
            _save_scope_state,
            storage_root,
            scope.key,
            _ScopeState(
                active=next_active(tokens_after, settings, due.context_window, active=True),
                last_attempt_at=now,
                consecutive_failures=failures,
                last_outcome=outcome,
            ),
        )
        self._not_before[scope.key] = now + _cooldown_seconds(settings, failures)
        return outcome

    async def _curate(
        self,
        config: Config,
        scope: _CurationScope,
        due: _DuePass,
        settings: PromptCurationConfig,
    ) -> tuple[_PassOutcome, int]:
        """Run one pass and publish it when valid; return the outcome and the curated files' size afterwards."""
        staged, before, bounds = due.staged, due.before, due.bounds
        logger.info(
            "Prompt curation started",
            tokens=before.curated_tokens,
            target_tokens=bounds.upper_tokens,
            floor_tokens=bounds.floor_tokens,
        )
        try:
            response = await asyncio.wait_for(
                self._run_model(config, scope, staged, before, bounds, settings),
                timeout=settings.timeout_seconds,
            )
        except TimeoutError:
            logger.warning("Prompt curation timed out", timeout_seconds=settings.timeout_seconds)
            return "timeout", before.curated_tokens
        except Exception:
            logger.exception("Prompt curation model run failed")
            return "failed", before.curated_tokens
        if response.status == RunStatus.error:
            logger.warning("Prompt curation model run failed", error=str(response.content))
            return "failed", before.curated_tokens
        after = staged.measurement()
        if violations := validate_pass(before, after, bounds, settings):
            logger.warning(
                "Prompt curation rejected",
                violations=violations,
                tokens_before=before.curated_tokens,
                tokens_after=after.curated_tokens,
            )
            return "rejected", before.curated_tokens
        if await asyncio.to_thread(staged.publish) == "conflict":
            logger.info("Prompt curation discarded: a curated file was rewritten during the pass")
            return "conflict", before.curated_tokens
        schedule_agent_memory_refresh(
            scope.agent_name,
            staged.root,
            config,
            self.runtime_paths,
            execution_identity=scope.identity,
        )
        logger.info(
            "Prompt curation accepted",
            tokens_before=before.curated_tokens,
            tokens_after=after.curated_tokens,
            changed_paths=sorted(after.changed_paths),
        )
        return "accepted", after.curated_tokens

    async def _run_model(
        self,
        config: Config,
        scope: _CurationScope,
        staged: StagedWorkspace,
        before: PassMeasurement,
        bounds: PassBounds,
        settings: PromptCurationConfig,
    ) -> RunOutput:
        model_name = config.resolve_entity(scope.agent_name).model_name
        model = model_loading.get_model_instance(
            config,
            self.runtime_paths,
            model_name,
            execution_identity=scope.identity,
        )
        install_model_call_cap(model, entity_name=scope.agent_name)
        curator = Agent(
            name="PromptCurator",
            model=model,
            tools=[PromptCurationTools(staged, bounds, settings, before)],
            tool_call_limit=_TOOL_CALL_LIMIT,
            telemetry=False,
        )
        invocation_id = uuid4().hex
        response = await curator.arun(
            _curation_prompt(config, scope.agent_name, staged, before, bounds, settings),
            run_id=invocation_id,
            session_id=f"prompt_curation:{scope.key}",
        )
        await record_helper_usage(
            response,
            owner=HelperUsageOwner(
                storage_factory=partial(
                    create_session_storage,
                    scope.agent_name,
                    config,
                    self.runtime_paths,
                    execution_identity=scope.identity,
                ),
                session_id=scope.session_id,
                # Insert-or-ignore, for a triggering conversation whose session row is not stored yet.
                initial_session=new_scope_session(
                    session_id=scope.session_id,
                    scope_id=scope.agent_name,
                    is_team=False,
                ),
            ),
            invocation_id=invocation_id,
            kind="prompt_curation",
            requester_id=scope.identity.requester_id if scope.identity is not None else None,
        )
        return response
