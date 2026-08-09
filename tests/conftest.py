"""Test configuration and fixtures for MindRoom tests."""

import asyncio
import os
import re
import shutil
import subprocess
import sys
import tempfile
import time
import uuid
import warnings
from collections.abc import (
    AsyncGenerator,
    AsyncIterator,
    Awaitable,
    Callable,
    Generator,
    Iterable,
    Iterator,
    Mapping,
    MutableMapping,
    Sequence,
)
from contextlib import ExitStack, contextmanager
from dataclasses import dataclass, field, replace
from itertools import count
from pathlib import Path
from types import SimpleNamespace
from typing import TYPE_CHECKING, Any, cast
from unittest.mock import AsyncMock, MagicMock, patch

import nio
import pytest
import pytest_asyncio
import structlog
import yaml
from agno.models.base import Model
from agno.models.response import ModelResponse
from aioresponses import aioresponses
from structlog.testing import ReturnLoggerFactory
from structlog.typing import BindableLogger, Context, Processor, WrappedLogger

import mindroom.approval_manager as approval_manager_module
import mindroom.bot  # noqa: F401
import mindroom.handled_turns as handled_turns_module
from mindroom.agent_storage import get_agent_session, get_team_session
from mindroom.ai import ResponseTurnContext
from mindroom.bot import AgentBot, TeamBot
from mindroom.coalescing import CoalescingDrainResult
from mindroom.command_turn_executor import CommandTurnExecutor
from mindroom.config.main import Config, load_config
from mindroom.constants import RuntimePaths, resolve_runtime_paths, safe_replace
from mindroom.conversation_resolver import DispatchContextResult, MessageContext
from mindroom.delivery_gateway import DeliveryGateway, EditTextRequest, FinalDeliveryRequest, SendTextRequest
from mindroom.dispatch_source import ScheduledHistoryBudget
from mindroom.edit_regenerator import EditRegenerator
from mindroom.event_journal import (
    ConversationPage,
    DeliveryAcknowledgement,
    DeliveryStage,
    JournalEvent,
    OutboxDelivery,
    OutboxView,
    PendingTurnView,
    RelationView,
    TerminalTurnWrite,
    VisibleMessage,
)
from mindroom.final_delivery import FinalDeliveryOutcome
from mindroom.handled_turns import _reset_handled_turn_ledger_runtime
from mindroom.history.runtime import (
    ScopeSessionContext,
    _resolve_history_scope,
    finalize_history_preparation,
    open_scope_session_context,
    prepare_scope_history,
    resolve_agent_preparation_inputs,
)
from mindroom.history.types import (
    CompactionLifecycle,
    HistoryScope,
    PreparedHistoryState,
    ResolvedHistoryExecutionPlan,
    ResolvedHistorySettings,
)
from mindroom.hooks import EnrichmentItem, MessageEnvelope
from mindroom.ingress_validation import IngressValidator
from mindroom.interactive import InteractiveMetadata
from mindroom.matrix.client import DeliveredMatrixEvent, ResolvedVisibleMessage
from mindroom.matrix.client_delivery import build_edit_event_content
from mindroom.matrix.conversation_reads import ConversationReader
from mindroom.matrix.identity import MatrixID
from mindroom.matrix.relation_lookup import RelationLookup
from mindroom.matrix.thread_diagnostics import is_thread_history_degraded
from mindroom.media_fallback import reset_model_media_capability_cache
from mindroom.message_target import MessageTarget
from mindroom.reaction_dispatch import ReactionDispatcher
from mindroom.response_delivery import TurnHandoff
from mindroom.response_payload_preparation import (
    DispatchPayloadInputs,
    ResponsePayloadPreparation,
    ResponsePayloadPreparer,
)
from mindroom.response_runner import PostLockRequestPreparationError, ResponseRequest, ResponseRunner
from mindroom.thread_utils import decide_agent_response
from mindroom.turn_controller import TurnController, _DispatchPreparation, _ReplayGuardContext
from mindroom.turn_origin import TurnOrigin, classify_turn_origin
from mindroom.turn_policy import PreparedDispatch, TurnPolicy
from mindroom.turn_store import TurnStore
from mindroom.user_stop_reconciliation import UserStopReconciler
from mindroom.visible_response_reconciliation import VisibleResponseReconciler
from mindroom.visible_voice_echo import VisibleVoiceEchoLifecycle, _reset_visible_voice_echo_barriers
from tests.identity_helpers import persist_entity_accounts

if TYPE_CHECKING:
    from agno.agent import Agent
    from agno.db.base import BaseDb
    from agno.session.agent import AgentSession
    from agno.session.team import TeamSession
    from xdist.workermanage import WorkerController

    from mindroom.config.models import CompactionConfig
    from mindroom.dispatch_handoff import DispatchEvent
    from mindroom.event_journal import EventJournalStore
    from mindroom.event_journal.backend import Backend, Operation
    from mindroom.matrix_rtc.call_manager import CallManager
    from mindroom.tool_system.worker_routing import ToolExecutionIdentity


_STRUCTLOG_CONFIGURE = structlog.configure
_POSTGRES_STARTUP_TIMEOUT_SECONDS = 30

# The event journal orders text in SQL, in SQLite, and in Python, and those
# three must agree. An Alpine image cannot show a disagreement: every musl
# locale sorts like C, so a missing `COLLATE "C"` looks correct there and
# breaks against a glibc server. The journal's server is therefore glibc.
_POSTGRES_JOURNAL_RUN_ID_STASH_KEY = pytest.StashKey[str]()
_POSTGRES_JOURNAL_CONTAINER_PREFIX = "mindroom-postgres-journal-test-"
_POSTGRES_JOURNAL_IMAGE = "postgres:16"
_POSTGRES_JOURNAL_LOCALE = "en_US.utf8"

# `postgres:16` declares `VOLUME /var/lib/postgresql/data`, so every container
# started from it is handed an anonymous volume. That volume does not die with
# the container: `docker rm -f` without `-v` removes the container and orphans
# the volume, and only a container that exits on its own reaches `--rm`'s
# volume-removing path. A suite that starts a server per run therefore left one
# dangling ~50 MB data directory behind per run forever -- observed here at 742
# volumes and 169 GB, which filled the root filesystem.
#
# Mounting a tmpfs over the data directory means no volume is ever created, so
# there is nothing to leak down any exit path: clean teardown, `rm -f`, SIGKILL,
# or a reboot. The database is thrown away at the end of the run either way, so
# nothing of value was on disk.
#
# If this ever regresses, know that it is deliberately hard to see: the volumes
# live under Docker's storage root, which is unreadable without privileges, so
# `du -x /` under-reports by the entire leak while `df` counts it. Trust `df`
# and `docker system df -v`, not `du`.
_POSTGRES_JOURNAL_DATA_DIR = "/var/lib/postgresql/data"
# A cap, not an allocation: tmpfs pages are only backed as they are written, and
# a fresh cluster plus one database per xdist worker measures well under 1 GB.
# Docker's default of half of host RAM is left behind on purpose -- an uncapped
# runaway on a shared machine is the same class of failure as the disk leak.
_POSTGRES_JOURNAL_TMPFS_SIZE = "2g"

# A killed run reaches no teardown, so its container outlives it forever. The
# tmpfs above means such a container no longer strands storage, but it still
# holds memory and a published port, and nothing ever collects it.
#
# Nothing outside the run may collect it either. Up to seventeen worktrees run
# this suite at once on this host, so a sweeper that matched on the name prefix,
# the image, or the label would destroy a live run's server. Recording the
# owning PID does not fix that -- PIDs are recycled -- and a lifetime cap just
# picks a different run to kill. Every one of those needs a guess about whether
# an owner is alive, and a wrong guess kills someone's suite.
#
# So the container watches its owner instead, and no guess is needed. The owner
# holds the write end of a FIFO open for the whole session and never sends
# anything through it. The kernel closes that end when the owning process dies,
# however it dies, and the container's read then returns EOF. The container
# shuts itself down, which is also the only exit path `--rm` collects, so a
# killed run's container removes itself with no sweeper in sight.
#
# Ordering is what makes the fact exact. The owner opens its end *before*
# `docker run`, so from the container's first instant "no writer" can only mean
# a dead owner, never one that has not arrived yet. That is why the watcher
# opens with O_NONBLOCK, which on a FIFO succeeds whether or not a writer is
# present, and only then reads blocking: an owner that died during container
# startup reports EOF immediately instead of blocking the open forever.
_POSTGRES_OWNER_MOUNT_DIR = "/mindroom-run-owner"
_POSTGRES_OWNER_PIPE_NAME = "alive"
# A watcher that cannot open the pipe exits without signalling anything, so its
# `&&` never fires. Losing the reaper leaks one container; signalling on
# anything other than the owner's death would kill a live server, and that is
# the outcome worth spending a leak to avoid.
_POSTGRES_OWNER_WATCH_COMMAND = (
    "perl -e '"
    "use Fcntl;"
    " sysopen(my $pipe, $ARGV[0], O_RDONLY | O_NONBLOCK) or exit 1;"
    " fcntl($pipe, F_SETFL, 0) or exit 1;"
    " 1 while sysread($pipe, my $ignored, 4096) > 0;"
    f"' {_POSTGRES_OWNER_MOUNT_DIR}/{_POSTGRES_OWNER_PIPE_NAME}"
)
# The server deliberately does not run as PID 1, and that is the whole point of
# this shell. Orphaned processes are reparented to PID 1, so a postmaster in
# that slot ends up reaping this watcher -- and `CleanupBackend` reads any child
# exiting with a status other than 0 or 1 as a crashed backend. Measured against
# `postgres:16`: an unrelated child exiting 2 produced "server process (PID 7)
# exited with exit code 2", then "terminating any other active server processes"
# and "all server processes terminated; reinitializing". Every live connection
# died. A watcher SIGKILLed under memory pressure would do that to a healthy
# run, which is exactly the harm this whole mechanism exists to prevent. Keeping
# a shell at PID 1 puts the watcher outside the postmaster's sight completely.
#
# The shell waits only on the server, so a server that exits on its own still
# ends the container exactly as it did before.
#
# SIGINT alone is not enough, and this was measured rather than reasoned about.
# Once the postmaster is serving, SIGINT is its fast shutdown and the image's
# own STOPSIGNAL, and the container exits within a second of the owner dying.
# During bootstrap the signal goes to `docker-entrypoint.sh` instead, which is
# running `initdb` and a temporary server, and it survives: an owner killed in
# that window left the container running, still up ninety seconds later and
# serving happily, which is precisely the leak this mechanism exists to stop.
# A pytest run killed in its first couple of seconds is not a rare shape.
#
# So the watcher escalates. SIGINT first, so a live cluster still gets its
# clean fast shutdown, then SIGKILL if the process is still there. SIGKILL
# cannot be refused by any bootstrap phase, `wait` then returns, PID 1 exits,
# and `--rm` collects the container. Losing a fast shutdown costs nothing here:
# the data directory is a throwaway tmpfs that the container is about to drop.
_POSTGRES_OWNER_SHUTDOWN_GRACE_SECONDS = 5

# One shared server serves every xdist worker, and `postgres:16` ships
# `max_connections = 100`. `PostgresBackend.open` takes five connections per
# store -- one serialized writer plus a four-connection reader pool -- so the
# default is exhausted by twenty concurrent stores. `-n auto` on this host is
# thirty-two workers, which is a hundred and sixty.
#
# Measured, with only `-n` differing over the same 348 tests: `-n 32` produced
# 137 errors and 1 failure, every one `FATAL: sorry, too many clients already`,
# and `-n 16` produced none. That is a property of the worker count and not of
# any test, which is why it has been read as flakiness so many times.
#
# The cap is a slot count, not an allocation: an unused slot costs a few hundred
# bytes of shared memory, while a backend process only exists once something
# connects. Five hundred leaves room for a bigger `-n auto` and for several
# stores per worker without ever being the reason a run goes red.
_POSTGRES_JOURNAL_MAX_CONNECTIONS = 500
_POSTGRES_JOURNAL_ENTRYPOINT_SCRIPT = f"""\
docker-entrypoint.sh postgres -c max_connections={_POSTGRES_JOURNAL_MAX_CONNECTIONS} &
server=$!
({_POSTGRES_OWNER_WATCH_COMMAND} && {{
    kill -INT "$server" 2>/dev/null
    sleep {_POSTGRES_OWNER_SHUTDOWN_GRACE_SECONDS}
    kill -KILL "$server" 2>/dev/null
}}) &
wait "$server"
"""


def _configure_quiet_structlog() -> None:
    """Keep incidental test logging cheap and silent."""
    _STRUCTLOG_CONFIGURE(
        processors=[],
        context_class=dict,
        logger_factory=ReturnLoggerFactory(),
        wrapper_class=structlog.stdlib.BoundLogger,
        cache_logger_on_first_use=False,
    )


def _configure_uncached_structlog(
    processors: Iterable[Processor] | None = None,
    wrapper_class: type[BindableLogger] | None = None,
    context_class: type[Context] | None = None,
    logger_factory: Callable[..., WrappedLogger] | None = None,
    cache_logger_on_first_use: bool | None = None,
) -> None:
    """Prevent logging tests from leaving cached production renderers behind."""
    # Cached proxies outlive one test, so the suite intentionally overrides this request.
    _ = cache_logger_on_first_use
    _STRUCTLOG_CONFIGURE(
        processors=processors,
        wrapper_class=wrapper_class,
        context_class=context_class,
        logger_factory=logger_factory,
        cache_logger_on_first_use=False,
    )


_configure_quiet_structlog()


__all__ = [
    "TEST_ACCESS_TOKEN",
    "TEST_PASSWORD",
    "FakeCredentialsManager",
    "agent_response_should_respond",
    "aioresponse",
    "bind_mock_config_event_journal",
    "bind_runtime_paths",
    "build_private_template_dir",
    "bypass_authorization",
    "create_mock_room",
    "delivered_matrix_event",
    "delivered_matrix_side_effect",
    "dispatch_context_result",
    "drain_coalescing",
    "install_call_manager_mock",
    "install_edit_message_mock",
    "install_generate_response_mock",
    "install_runtime_journal_support",
    "install_send_response_mock",
    "install_shutdown_drain_mocks",
    "load_config_yaml",
    "make_matrix_client_mock",
    "make_visible_message",
    "message_origin",
    "normalize_console_output",
    "orchestrator_runtime_paths",
    "patch_response_runner_module",
    "prepare_history_for_run_for_test",
    "prepare_payload_via_seam",
    "prepared_dispatch_result",
    "replace_delivery_gateway_deps",
    "replace_edit_regenerator_deps",
    "replace_response_runner_deps",
    "replace_turn_controller_deps",
    "replace_turn_policy_deps",
    "replace_turn_store_deps",
    "request_envelope",
    "requires_linux",
    "runtime_paths_for",
    "sync_bot_runtime_state",
    "test_runtime_paths",
    "unwrap_extracted_collaborator",
    "wrap_extracted_collaborators",
    "write_config_yaml",
]

_TEST_RUNTIME_PATHS_BY_CONFIG_ID: dict[int, RuntimePaths] = {}
_VISIBLE_MESSAGE_IDS = count(1)
_ANSI_RE = re.compile(r"\x1b\[[0-9;]*m")
_SOFT_WRAP_RE = re.compile(r"(?<=\S)\n(?=\S)")
RuntimeBot = AgentBot | TeamBot
TestFunction = Callable[..., object]


async def prepare_history_for_run_for_test(
    *,
    agent: "Agent",
    agent_name: str,
    full_prompt: str,
    session_id: str | None,
    runtime_paths: RuntimePaths,
    config: Config,
    execution_identity: "ToolExecutionIdentity | None",
    storage: "BaseDb | None" = None,
    session: "AgentSession | TeamSession | None" = None,
    history_settings: ResolvedHistorySettings | None = None,
    compaction_config: "CompactionConfig | None" = None,
    has_authored_compaction_config: bool | None = None,
    active_model_name: str | None = None,
    active_context_window: int | None = None,
    static_prompt_tokens: int | None = None,
    available_history_budget: int | None = None,
    scope: HistoryScope | None = None,
    execution_plan: ResolvedHistoryExecutionPlan | None = None,
    compaction_lifecycle: CompactionLifecycle | None = None,
) -> PreparedHistoryState:
    """Compose the production history-preparation seams for one test run."""
    resolved_scope = scope or _resolve_history_scope(agent)
    resolved_inputs = resolve_agent_preparation_inputs(
        agent=agent,
        agent_name=agent_name,
        full_prompt=full_prompt,
        config=config,
        history_settings=history_settings,
        compaction_config=compaction_config,
        has_authored_compaction_config=has_authored_compaction_config,
        active_model_name=active_model_name,
        active_context_window=active_context_window,
        static_prompt_tokens=static_prompt_tokens,
        execution_plan=execution_plan,
    )
    if available_history_budget is not None:
        # prepare_scope_history reads its trigger/hard budgets from the execution
        # plan, so express the test budget override through the plan itself.
        resolved_inputs = replace(
            resolved_inputs,
            execution_plan=replace(
                resolved_inputs.execution_plan,
                replay_budget_tokens=available_history_budget,
                hard_replay_budget_tokens=available_history_budget,
            ),
        )
    scope_history_kwargs = {
        "agent": agent,
        "agent_name": agent_name,
        "resolved_inputs": resolved_inputs,
        "runtime_paths": runtime_paths,
        "config": config,
        "scope": resolved_scope,
        "compaction_lifecycle": compaction_lifecycle,
    }
    if storage is not None and resolved_scope is not None and session_id is not None:
        persisted_session = session
        if persisted_session is None:
            persisted_session = (
                get_team_session(storage, session_id)
                if resolved_scope.kind == "team"
                else get_agent_session(storage, session_id)
            )
        scope_context = ScopeSessionContext(
            scope=resolved_scope,
            storage=storage,
            session=persisted_session,
            session_id=session_id,
        )
        prepared_scope_history = await prepare_scope_history(scope_context=scope_context, **scope_history_kwargs)
    else:
        with open_scope_session_context(
            agent=agent,
            agent_name=agent_name,
            session_id=session_id,
            runtime_paths=runtime_paths,
            config=config,
            execution_identity=execution_identity,
            scope=resolved_scope,
        ) as scope_context:
            prepared_scope_history = await prepare_scope_history(scope_context=scope_context, **scope_history_kwargs)
    return finalize_history_preparation(
        prepared_scope_history=prepared_scope_history,
        config=config,
        static_prompt_tokens=static_prompt_tokens,
        available_history_budget=available_history_budget,
    )


def dispatch_context_result(context: MessageContext) -> DispatchContextResult:
    """Wrap a stable message context in the dispatch extraction result shape."""
    return DispatchContextResult(context=context, thread_context=None)


def prepared_dispatch_result(dispatch: PreparedDispatch) -> _DispatchPreparation:
    """Wrap a prepared dispatch in the private turn-controller preparation result shape."""
    return _DispatchPreparation(
        dispatch=dispatch,
        replay_guard=_ReplayGuardContext(
            history=dispatch.context.replay_guard_history,
            degraded=is_thread_history_degraded(dispatch.context.replay_guard_history),
            thread_id=dispatch.target.resolved_thread_id,
        ),
    )


def agent_response_should_respond(
    agent_name: str,
    am_i_mentioned: bool,
    is_thread: bool,
    room: nio.MatrixRoom,
    thread_history: Sequence[ResolvedVisibleMessage],
    config: Config,
    runtime_paths: RuntimePaths,
    mentioned_agents: list[MatrixID] | None = None,
    has_non_agent_mentions: bool = False,
    *,
    sender_id: str,
    available_responders_in_room: list[MatrixID] | None = None,
    agents_in_thread: Sequence[MatrixID] | None = None,
) -> bool:
    """Return the boolean projection of the agent response decision for tests."""
    return decide_agent_response(
        agent_name,
        am_i_mentioned,
        is_thread,
        room,
        thread_history,
        config,
        runtime_paths,
        mentioned_agents,
        has_non_agent_mentions,
        sender_id=sender_id,
        available_responders_in_room=available_responders_in_room,
        agents_in_thread=agents_in_thread,
    ).should_respond


def message_origin(
    *,
    sender_id: str = "@user:localhost",
    requester_id: str | None = None,
    sender_entity_name: str | None = None,
    requester_entity_name: str | None = None,
    source_kind: str = "message",
    original_sender: str | None = None,
    trusted_user_relay: bool = False,
) -> TurnOrigin:
    """Build canonical origin metadata for manually constructed test envelopes."""
    return classify_turn_origin(
        transport_sender_id=sender_id,
        requester_id=requester_id or sender_id,
        sender_entity_name=sender_entity_name,
        requester_entity_name=requester_entity_name,
        source_kind=source_kind,
        original_sender=original_sender,
        trusted_user_relay=trusted_user_relay,
    )


def request_envelope(
    *,
    room_id: str = "!test:localhost",
    reply_to_event_id: str = "$event",
    thread_id: str | None = None,
    prompt: str = "Hello",
    user_id: str | None = "@user:localhost",
    target: MessageTarget | None = None,
    agent_name: str = "test_agent",
    source_kind: str = "message",
    attachment_ids: tuple[str, ...] = (),
) -> MessageEnvelope:
    """Build a canonical response envelope for direct ResponseRequest tests."""
    resolved_user_id = user_id or "@user:localhost"
    resolved_target = target or MessageTarget.resolve(room_id, thread_id, reply_to_event_id)
    return MessageEnvelope(
        source_event_id=reply_to_event_id,
        target=resolved_target,
        body=prompt,
        attachment_ids=attachment_ids,
        mentioned_agents=(),
        agent_name=agent_name,
        origin=message_origin(sender_id=resolved_user_id, requester_id=resolved_user_id, source_kind=source_kind),
    )


def requires_linux(
    *,
    reason: str = "requires Linux",
    timeout: float | None = None,
) -> Callable[[TestFunction], TestFunction]:
    """Return a decorator for tests that only run on Linux."""

    def decorator(test_func: TestFunction) -> TestFunction:
        marked = pytest.mark.skipif(sys.platform != "linux", reason=reason)(test_func)
        if timeout is not None:
            marked = pytest.mark.timeout(timeout)(marked)
        return marked

    return decorator


async def drain_coalescing(*bots: RuntimeBot) -> None:
    """Drain gate batches and detached responses until both are quiescent.

    A detached response settling during the runner drain can release its
    lifecycle lock and flush a busy-conversation backlog into the gate, so a
    single gate-then-runner pass is not a reliable barrier.
    """
    for bot in bots:
        runner = unwrap_extracted_collaborator(bot._response_runner)
        while True:
            # Concurrently: the gate drain may hold a busy conversation's
            # backlog until its detached response goes idle, which only the
            # runner drain settles.
            await asyncio.gather(
                bot._coalescing_gate.drain_all(),
                runner.drain_inbox_responses(),
            )
            if not runner._inbox_response_tasks and not bot._coalescing_gate._gates:
                break


def _wait_for_postgres_container(database_url: str) -> None:
    import psycopg  # noqa: PLC0415

    deadline = time.monotonic() + _POSTGRES_STARTUP_TIMEOUT_SECONDS
    last_error: Exception | None = None
    while time.monotonic() < deadline:
        try:
            with psycopg.connect(database_url, connect_timeout=1):
                return
        except psycopg.OperationalError as exc:
            last_error = exc
            time.sleep(0.25)
    msg = "Postgres test container did not become ready"
    raise RuntimeError(msg) from last_error


def _create_postgres_worker_database(database_url: str, worker_id: str) -> str:
    """Create an isolated database for one worker on the shared Postgres server."""
    import psycopg  # noqa: PLC0415
    from psycopg import sql  # noqa: PLC0415

    database_name = f"mindroom_{worker_id}"
    with psycopg.connect(database_url, autocommit=True) as db:
        db.execute(sql.SQL("CREATE DATABASE {}").format(sql.Identifier(database_name)))
    return f"{database_url.rsplit('/', 1)[0]}/{database_name}"


def _wait_for_postgres_container_port(docker: str, container_name: str) -> str:
    """Wait for Docker to publish a shared container's random host port."""
    deadline = time.monotonic() + _POSTGRES_STARTUP_TIMEOUT_SECONDS
    last_error = ""
    while time.monotonic() < deadline:
        result = subprocess.run(
            [docker, "port", container_name, "5432/tcp"],
            check=False,
            capture_output=True,
            text=True,
        )
        if result.returncode == 0 and result.stdout.strip():
            mapped_port = result.stdout.strip().splitlines()[-1]
            _, port = mapped_port.rsplit(":", 1)
            return f"postgresql://cache:test@127.0.0.1:{port}/mindroom"
        last_error = result.stderr.strip()
        time.sleep(0.05)
    msg = f"Postgres test container did not publish a port: {last_error}"
    raise RuntimeError(msg)


def _wait_for_conflicting_postgres_container(docker: str, container_name: str) -> str:
    """Return the state of the container that already holds ``container_name``.

    Docker reserves a container's name when ``create`` starts and registers the
    container itself only when ``create`` finishes. Every worker that loses the
    race to create the shared server therefore sees ``Conflict`` from ``run``
    and ``no such object`` from ``inspect`` until the winner's create completes
    -- a window that widens under load, which is precisely when the whole suite
    is running. One inspect samples that window and reads a race as a broken
    machine. The name is known to be taken, so wait for the container behind it.
    """
    deadline = time.monotonic() + _POSTGRES_STARTUP_TIMEOUT_SECONDS
    last_error = ""
    while time.monotonic() < deadline:
        result = subprocess.run(
            [docker, "inspect", "--format", "{{.State.Status}}", container_name],
            check=False,
            capture_output=True,
            text=True,
        )
        if result.returncode == 0:
            return result.stdout.strip()
        last_error = result.stderr.strip()
        time.sleep(0.05)
    msg = f"Postgres journal container {container_name} never became inspectable: {last_error}"
    raise RuntimeError(msg)


def _postgres_container_name(run_id: str, prefix: str) -> str:
    """Return the deterministic disposable Postgres container name for one test run."""
    return f"{prefix}{run_id}"


def _owner_pipe_dir(run_id: str) -> Path:
    """Return the host directory holding one run's owner pipe."""
    return Path(tempfile.gettempdir()) / f"mindroom-pytest-owner-{run_id}"


def _hold_owner_pipe(run_id: str) -> None:
    """Open this run's owner pipe for writing, for as long as this process lives.

    The descriptor is deliberately never closed. The kernel closing it when this
    process dies -- including under the SIGKILL that leaves no teardown to run,
    which is the whole case being fixed -- is the signal the container waits on,
    so an explicit close would only add a way to send it early.

    Opening read-write is what keeps this from blocking: opening a FIFO
    write-only waits for a reader, and the reader is a container that cannot be
    started until this returns.
    """
    directory = _owner_pipe_dir(run_id)
    directory.mkdir(parents=True, exist_ok=True)
    pipe = directory / _POSTGRES_OWNER_PIPE_NAME
    os.mkfifo(pipe)
    os.open(pipe, os.O_RDWR)


def _postgres_run_command(docker: str, container_name: str, run_id: str) -> list[str]:
    """Return the argv that starts one disposable Postgres journal server."""
    return [
        docker, "run", "--rm", "-d",
        "--name", container_name,
        "--label", f"mindroom.pytest.run={run_id}",
        # Keeps the image's declared anonymous volume from ever existing.
        "--tmpfs", f"{_POSTGRES_JOURNAL_DATA_DIR}:size={_POSTGRES_JOURNAL_TMPFS_SIZE}",
        # Read-only: the container only ever reads the owner pipe, and the run
        # that owns it is the only thing allowed to say when it ends.
        "-v", f"{_owner_pipe_dir(run_id)}:{_POSTGRES_OWNER_MOUNT_DIR}:ro",
        "-e", "POSTGRES_USER=cache",
        "-e", "POSTGRES_PASSWORD=test",
        "-e", "POSTGRES_DB=mindroom",
        "-e", f"LANG={_POSTGRES_JOURNAL_LOCALE}",
        "-e", f"POSTGRES_INITDB_ARGS=--locale={_POSTGRES_JOURNAL_LOCALE}",
        "-p", "127.0.0.1::5432",
        "--entrypoint", "bash",
        _POSTGRES_JOURNAL_IMAGE,
        "-c", _POSTGRES_JOURNAL_ENTRYPOINT_SCRIPT,
    ]  # fmt: skip


def _remove_postgres_container(docker: str, container_name: str) -> None:
    """Remove one disposable Postgres container, and anything it owns, if it exists."""
    # `-v` because a bare `rm -f` orphans a container's anonymous volumes. The
    # data directory is a tmpfs so today there are none, but a removal that
    # takes the container's storage with it is what the caller means, and it
    # keeps a future mount from quietly reopening the leak this replaced.
    result = subprocess.run(
        [docker, "rm", "-f", "-v", container_name],
        check=False,
        capture_output=True,
        text=True,
    )
    if result.returncode == 0 or "No such container" in result.stderr:
        return
    msg = f"Could not remove Postgres test container {container_name}: {result.stderr.strip()}"
    raise RuntimeError(msg)


def pytest_configure_node(node: "WorkerController") -> None:
    """Own the shared Postgres server's run from the xdist controller.

    The controller holds the owner pipe rather than a worker because it is the
    only participant that outlives all of them: a worker that runs out of work
    shuts down while the others are still querying the server, and the worker
    that happened to create the container is not special in that respect.

    Nodes are configured before they run tests, so the pipe exists before any
    worker can start the container -- the ordering the watcher depends on.
    """
    if _POSTGRES_JOURNAL_RUN_ID_STASH_KEY in node.config.stash:
        return
    run_id = node.workerinput["testrunuid"]
    node.config.stash[_POSTGRES_JOURNAL_RUN_ID_STASH_KEY] = run_id
    _hold_owner_pipe(run_id)


def pytest_sessionfinish(session: pytest.Session) -> None:
    """Release the shared Postgres server once every xdist worker has finished."""
    if hasattr(session.config, "workerinput"):
        return
    run_id = session.config.stash.get(_POSTGRES_JOURNAL_RUN_ID_STASH_KEY, None)
    if run_id is None:
        return
    shutil.rmtree(_owner_pipe_dir(run_id), ignore_errors=True)
    docker = shutil.which("docker")
    if docker is None:
        return
    if subprocess.run([docker, "info"], check=False, capture_output=True).returncode != 0:
        # The fixture skipped on this same condition, so nothing was ever created.
        return
    try:
        _remove_postgres_container(
            docker,
            _postgres_container_name(run_id, _POSTGRES_JOURNAL_CONTAINER_PREFIX),
        )
    except RuntimeError as exc:
        warnings.warn(pytest.PytestWarning(str(exc)), stacklevel=1)
        session.exitstatus = pytest.ExitCode.TESTS_FAILED


@pytest.fixture(scope="session")
def postgres_journal_url(worker_id: str, testrun_uid: str) -> Iterator[str]:
    """Start or reuse one glibc Postgres server for event-journal parity tests.

    Only a machine that cannot run Docker at all skips. Every other failure
    raises, because a skip here silently deletes the PostgreSQL half of a suite
    whose entire claim is that its rules hold on both backends, and a suite that
    proves half of what it says it proves still exits 0.
    """
    docker = shutil.which("docker")
    if docker is None:
        pytest.skip("Docker is required for Postgres event-journal parity tests")
    if subprocess.run([docker, "info"], check=False, capture_output=True).returncode != 0:
        pytest.skip("Docker daemon is unavailable for Postgres event-journal parity tests")

    shared_across_workers = worker_id != "master"
    run_id = testrun_uid if shared_across_workers else uuid.uuid4().hex
    container_name = _postgres_container_name(run_id, _POSTGRES_JOURNAL_CONTAINER_PREFIX)
    if not shared_across_workers:
        # This process is the whole run, so it is its own owner. Under xdist the
        # controller has already done this, before any worker got to run.
        _hold_owner_pipe(run_id)
    run_result = subprocess.run(
        _postgres_run_command(docker, container_name, run_id),
        check=False,
        capture_output=True,
        text=True,
    )
    created_container = run_result.returncode == 0
    if not created_container:
        if "is already in use by container" not in run_result.stderr:
            msg = f"Could not start Postgres journal container: {run_result.stderr.strip()}"
            raise RuntimeError(msg)
        status = _wait_for_conflicting_postgres_container(docker, container_name)
        if status in {"dead", "exited"}:
            msg = f"Shared Postgres journal container is {status}"
            raise RuntimeError(msg)

    try:
        database_url = _wait_for_postgres_container_port(docker, container_name)
        _wait_for_postgres_container(database_url)
        if shared_across_workers:
            database_url = _create_postgres_worker_database(database_url, worker_id)
        yield database_url
    finally:
        if not shared_across_workers:
            # Only the run that created the container may destroy it.
            if created_container:
                _remove_postgres_container(docker, container_name)
            shutil.rmtree(_owner_pipe_dir(run_id), ignore_errors=True)


def postgres_journal_schema_url(database_url: str) -> str:
    """Return a DSN onto one fresh, empty schema of ``database_url``.

    The Postgres server is shared by every test in a worker, so isolation has
    to come from somewhere. A private schema pinned into the DSN gives it
    without a private server, and because the pin travels with the DSN rather
    than with a connection, two stores opened from the same string see the
    same tables -- which is what a test about two connections racing needs.
    """
    import psycopg  # noqa: PLC0415
    from psycopg import sql  # noqa: PLC0415

    schema = f"journal_{uuid.uuid4().hex}"
    with psycopg.connect(database_url, autocommit=True) as db:
        db.execute(sql.SQL("CREATE SCHEMA {}").format(sql.Identifier(schema)))
    separator = "&" if "?" in database_url else "?"
    return f"{database_url}{separator}options=-csearch_path%3D{schema}"


@pytest_asyncio.fixture(params=("sqlite", "postgres"), ids=("sqlite", "postgres"))
async def journal_database(
    request: pytest.FixtureRequest,
    tmp_path: Path,
) -> AsyncGenerator[Callable[[], "EventJournalStore"], None]:
    """Return an opener onto one empty database, per supported backend.

    Parametrized rather than duplicated so a rule can only be proven for one
    backend by also proving it for the other.

    An opener rather than a store, because a store is one process's connections
    and not the database itself. Whether two bots sharing a database can step on
    each other is a question only a second store over the same database can ask,
    and every store this hands out is closed when the test ends.
    """
    from mindroom.event_journal import EventJournalStore  # noqa: PLC0415

    backend = str(request.param)
    if backend == "sqlite":
        database_path = tmp_path / "event_journal.db"

        def connect() -> EventJournalStore:
            return EventJournalStore.open_sqlite(database_path)
    else:
        scoped_url = postgres_journal_schema_url(request.getfixturevalue("postgres_journal_url"))

        def connect() -> EventJournalStore:
            return EventJournalStore.open_postgres(scoped_url)

    opened: list[EventJournalStore] = []

    def opener() -> EventJournalStore:
        store = connect()
        opened.append(store)
        return store

    try:
        yield opener
    finally:
        for store in opened:
            await store.close()


@pytest.fixture
def journal_store(journal_database: Callable[[], "EventJournalStore"]) -> "EventJournalStore":
    """Return one open event-journal store per supported backend.

    Opening is synchronous and closing belongs to ``journal_database``, so this
    is a plain fixture. The store is still parametrized over both backends,
    because that comes from the fixture it asks for.
    """
    return journal_database()


async def _empty_async_iterator() -> AsyncGenerator[object, None]:
    """Yield nothing while preserving async-iterator semantics for nio relations APIs."""
    if False:
        yield None


def _make_room_get_event_response(event_id: str) -> nio.RoomGetEventResponse:
    """Return a minimal RoomGetEventResponse containing one visible text event."""
    event = MagicMock(spec=nio.RoomMessageText)
    event.event_id = event_id
    event.sender = "@user:localhost"
    event.body = event_id
    event.server_timestamp = 0
    event.source = {
        "type": "m.room.message",
        "content": {
            "msgtype": "m.text",
            "body": event_id,
        },
    }
    response = nio.RoomGetEventResponse()
    response.event = event
    return response


def _outcome(
    terminal_status: str,
    event_id: str | None = None,
    is_visible_response: bool = False,
    final_visible_body: str | None = None,
    delivery_kind: str | None = None,
    failure_reason: str | None = None,
    suppressed: bool = False,
    tool_trace: tuple[object, ...] = (),
    extra_content: Mapping[str, object] | None = None,
    option_map: dict[str, str] | None = None,
    options_list: tuple[dict[str, str], ...] | None = None,
) -> FinalDeliveryOutcome:
    """Build one compact terminal outcome for tests."""
    resolved_suppressed = suppressed or (failure_reason == "suppressed_by_hook" and not is_visible_response)
    return FinalDeliveryOutcome(
        terminal_status=terminal_status,
        event_id=event_id,
        is_visible_response=is_visible_response,
        final_visible_body=final_visible_body,
        delivery_kind=delivery_kind,
        failure_reason=failure_reason,
        suppressed=resolved_suppressed,
        tool_trace=tool_trace,
        extra_content=dict(extra_content or {}),
        interactive_metadata=InteractiveMetadata._from_parts(option_map, options_list),
    )


class _AutoRoomCache(MutableMapping[str, nio.MatrixRoom]):
    """Mutable test room cache that lazily vends joined unencrypted rooms."""

    def __init__(self, own_user_id: str) -> None:
        self._own_user_id = own_user_id
        self._rooms: dict[str, nio.MatrixRoom] = {}

    def __getitem__(self, room_id: str) -> nio.MatrixRoom:
        room = self._rooms.get(room_id)
        if room is not None:
            return room
        if not room_id.startswith("!"):
            raise KeyError(room_id)
        room = nio.MatrixRoom(room_id, self._own_user_id)
        self._rooms[room_id] = room
        return room

    def __setitem__(self, room_id: str, room: nio.MatrixRoom) -> None:
        self._rooms[room_id] = room

    def __delitem__(self, room_id: str) -> None:
        del self._rooms[room_id]

    def __iter__(self) -> Iterator[str]:
        yield from self._rooms

    def __len__(self) -> int:
        return len(self._rooms)


def make_matrix_client_mock(*, user_id: str = "@mindroom_test:example.com") -> AsyncMock:
    """Return an AsyncClient-shaped mock with safe defaults for sync nio APIs."""
    client = AsyncMock(spec=nio.AsyncClient)
    client.user_id = user_id
    # A logged-in client always has one, and delivery records it on every claim
    # so a resend can tell whether its frozen transaction ID still deduplicates.
    client.device_id = "TESTDEVICE"
    client.rooms = _AutoRoomCache(user_id)
    client.next_batch = "s_test_token"
    client.loaded_sync_token = ""
    client.has_uncommitted_classic_sync_state = False
    presence_response = MagicMock()
    presence_response.presence = "offline"
    presence_response.last_active_ago = 3_600_000
    room_messages_response = nio.RoomMessagesResponse(room_id="!test:localhost", chunk=[], start="", end=None)
    client.add_event_callback = MagicMock()
    client.add_response_callback = MagicMock()
    client.get_presence = AsyncMock(return_value=presence_response)
    client.room_get_event = AsyncMock(side_effect=lambda _room_id, event_id: _make_room_get_event_response(event_id))
    client.room_get_event_relations = MagicMock(return_value=_empty_async_iterator())
    client.room_messages = AsyncMock(return_value=room_messages_response)
    client.joined_rooms = AsyncMock(return_value=nio.JoinedRoomsResponse(rooms=[]))

    async def reset_classic_sync_state() -> None:
        client.next_batch = ""
        client.loaded_sync_token = ""
        client.rooms.clear()
        client.has_uncommitted_classic_sync_state = False

    def acknowledge_classic_sync(_next_batch: str) -> None:
        client.has_uncommitted_classic_sync_state = False

    client.clear_persisted_sync_recovery = MagicMock()
    client.acknowledge_classic_sync = MagicMock(side_effect=acknowledge_classic_sync)
    client.reset_classic_sync_state.side_effect = reset_classic_sync_state
    return client


def delivered_matrix_event(
    event_id: str,
    content: Mapping[str, object] | None = None,
) -> DeliveredMatrixEvent:
    """Return one delivered Matrix event using the exact content payload seen by the helper."""
    return DeliveredMatrixEvent(
        event_id=event_id,
        content_sent={} if content is None else dict(content),
    )


def delivered_matrix_side_effect(event_id: str) -> Callable[..., Awaitable[DeliveredMatrixEvent]]:
    """Build one async mock side effect that mirrors send/edit helpers returning delivered events."""

    async def _deliver(*args: object, **kwargs: object) -> DeliveredMatrixEvent:
        if "content" in kwargs:
            content = kwargs["content"]
            content_mapping = content if isinstance(content, Mapping) else None
            return delivered_matrix_event(event_id, content_mapping)
        if "new_content" in kwargs:
            new_content = kwargs["new_content"]
            new_text = kwargs.get("new_text")
            if isinstance(new_content, Mapping) and isinstance(new_text, str):
                return delivered_matrix_event(
                    event_id,
                    build_edit_event_content(
                        event_id=str(args[2]) if len(args) > 2 else "",
                        new_content=dict(new_content),
                        new_text=new_text,
                    ),
                )
            content = new_content
            content_mapping = content if isinstance(content, Mapping) else None
            return delivered_matrix_event(event_id, content_mapping)
        if len(args) > 4 and isinstance(args[3], Mapping) and isinstance(args[4], str):
            return delivered_matrix_event(
                event_id,
                build_edit_event_content(
                    event_id=str(args[2]),
                    new_content=dict(args[3]),
                    new_text=args[4],
                ),
            )
        content_index = 2 if len(args) <= 3 else 3
        content = args[content_index] if len(args) > content_index else None
        content_mapping = content if isinstance(content, Mapping) else None
        return delivered_matrix_event(event_id, content_mapping)

    return _deliver


def serve_conversation_reader(
    reader: ConversationReader,
    messages: Sequence[ResolvedVisibleMessage],
    *,
    room_id: str = "!test:localhost",
    thread_id: str | None = None,
) -> None:
    """Point one stub reader at these messages, as the projection would serve them."""
    page = ConversationPage(
        messages=tuple(
            VisibleMessage(
                logical_event_id=message.event_id,
                room_id=room_id,
                thread_id=thread_id,
                sender=message.sender,
                # `or ordinal` would rewrite a real timestamp of 0.
                created_ts=ordinal if message.timestamp is None else message.timestamp,
                revision_event_id=message.event_id,
                revision_ts=ordinal if message.timestamp is None else message.timestamp,
                content=dict(message.content),
            )
            for ordinal, message in enumerate(messages, start=1)
        ),
        refresh_pending=(),
        next_cursor=None,
    )
    reader.read.return_value = page
    reader.read_strict.return_value = page


class FakeOutbox:
    """An in-memory outbox with the real claim-before-send semantics.

    A plain mock would let a test pass while the delivery never went through
    the outbox at all. This keeps the two rules that matter: a claimed row's
    payload is frozen, and an acknowledged row replays its recorded event ID
    instead of sending again.
    """

    def __init__(self) -> None:
        self.rows: dict[tuple[str, str], OutboxDelivery] = {}
        # What each acknowledgement carried alongside it, so a test can
        # assert the terminal record and the acknowledgement are one write.
        self.acknowledged_terminal_turns: list[tuple[str, TerminalTurnWrite | None]] = []
        self.attempted: set[tuple[str, str]] = set()
        # Turns whose membership has ended, as the journal would report it.
        self.ended_membership_turn_ids: set[str] = set()
        # The journal sources each FINAL enqueue handed over, in order.
        self.handed_over: list[tuple[str, ...]] = []

    async def turn_membership_is_current(self, *, turn_id: str, room_id: str) -> bool:
        """Return whether a turn still speaks for the room's current membership."""
        del room_id
        return turn_id not in self.ended_membership_turn_ids

    async def enqueue_delivery(
        self,
        *,
        turn_id: str,
        stage: DeliveryStage,
        room_id: str,
        thread_id: str | None,
        payload: Mapping[str, object],
        edits_event_id: str | None = None,
        settle_source_event_ids: tuple[str, ...] = (),
    ) -> str | None:
        """Record intent, leaving an already-attempted row's payload alone.

        An unattempted row is rewritten, exactly as the real `ON CONFLICT DO
        UPDATE ... WHERE attempted = 0` does. Refusing that too would make this
        double stricter than production and hide a same-turn re-enqueue -- a
        continuation replacing the answer it has not sent yet -- behind a stale
        payload no real deployment would serve.

        An attempted row is exempt from the membership check for the same
        reason production exempts it: its outcome is unknown, so the retry has
        to go out under the frozen transaction rather than strand whatever it
        may already have made visible.

        The handed-over sources are kept rather than dropped, because they are
        a durable effect of this call in production and a double that swallowed
        them would let a lost handoff pass unnoticed. There is no journal here
        to settle them in; whether the settlement really shares this
        transaction is pinned against the real backends.
        """
        if settle_source_event_ids:
            self.handed_over.append(settle_source_event_ids)
        key = (turn_id, stage.value)
        existing = self.rows.get(key)
        if existing is not None:
            if key in self.attempted:
                return existing.transaction_id
            self.rows[key] = replace(
                existing,
                room_id=room_id,
                thread_id=thread_id,
                payload=dict(payload),
                edits_event_id=edits_event_id,
            )
            return existing.transaction_id
        if turn_id in self.ended_membership_turn_ids:
            return None
        transaction_id = f"tx-{turn_id}-{stage.value}"
        self.rows[key] = OutboxDelivery(
            turn_id=turn_id,
            stage=stage,
            room_id=room_id,
            thread_id=thread_id,
            transaction_id=transaction_id,
            payload=dict(payload),
            edits_event_id=edits_event_id,
            acknowledged_event_id=None,
            created_at_ns=len(self.rows),
        )
        return transaction_id

    async def claim_delivery(self, *, turn_id: str, stage: DeliveryStage) -> OutboxDelivery | None:
        """Freeze one delivery before any network call, returning its prior state.

        The pre-claim row is what comes back, exactly as the real outbox does:
        a caller has to be able to see whether *someone else* attempted this
        and from which device, and reading after the mark would report this
        attempt back to itself. The device is not written here -- claiming does
        not mean this device is going to send.
        """
        key = (turn_id, stage.value)
        row = self.rows.get(key)
        if row is None:
            return None
        if stage is DeliveryStage.INITIAL and not row.attempted and (turn_id, DeliveryStage.FINAL.value) in self.rows:
            return None
        initial = self.rows.get((turn_id, DeliveryStage.INITIAL.value))
        if (
            stage is DeliveryStage.FINAL
            and row.edits_event_id is None
            and initial is not None
            and initial.attempted
            and initial.acknowledged_event_id is None
        ):
            return None
        self.attempted.add(key)
        self.rows[key] = replace(row, attempted=True)
        return row

    async def record_sending_device(
        self,
        *,
        turn_id: str,
        stage: DeliveryStage,
        device_id: str | None,
    ) -> None:
        """Record the device namespace this delivery is about to send under."""
        key = (turn_id, stage.value)
        if key in self.rows:
            self.rows[key] = replace(self.rows[key], sending_device_id=device_id)

    async def load_delivery(self, *, turn_id: str, stage: DeliveryStage) -> OutboxDelivery | None:
        """Return one delivery without claiming it."""
        return self.rows.get((turn_id, stage.value))

    async def acknowledge_delivery(
        self,
        *,
        turn_id: str,
        stage: DeliveryStage,
        event_id: str,
        terminal_turn: TerminalTurnWrite | None = None,
    ) -> DeliveryAcknowledgement:
        """Record the Matrix event one claimed delivery produced, and the turn it completes.

        The terminal record is kept rather than discarded so a test can assert
        it travelled *with* the acknowledgement. Dropping it here would let the
        two drift apart again without anything noticing.
        """
        key = (turn_id, stage.value)
        already = self.rows[key].acknowledged_event_id
        if already is not None:
            # First-writer-wins, like the real store: a loser is told the event
            # the row already names rather than its own, and told it bound
            # nothing -- which stays true even when the two events are equal.
            return DeliveryAcknowledgement(settled_event_id=already, bound=False)
        self.rows[key] = replace(self.rows[key], acknowledged_event_id=event_id)
        self.acknowledged_terminal_turns.append((turn_id, terminal_turn))
        return DeliveryAcknowledgement(settled_event_id=event_id, bound=True)

    async def unacknowledged_deliveries(
        self,
        *,
        limit: int = 256,
        after: tuple[int, str, str] | None = None,
    ) -> tuple[OutboxDelivery, ...]:
        """Return deliveries whose Matrix outcome is unknown, oldest first.

        The cursor is honoured, because recovery relies on it to make
        progress: a row it visits but does not acknowledge -- a failure, or a
        placeholder its answer overtook -- must not come back on the next
        page, or the scan never ends.
        """
        pending = sorted(
            (row for row in self.rows.values() if row.acknowledged_event_id is None),
            key=lambda row: (row.created_at_ns, row.turn_id, row.stage.value),
        )
        if after is not None:
            pending = [row for row in pending if (row.created_at_ns, row.turn_id, row.stage.value) > after]
        return tuple(pending[:limit])


def make_outbox_mock() -> OutboxView:
    """Return an outbox a delivery test can actually send through."""
    return cast("OutboxView", FakeOutbox())


class CrashError(RuntimeError):
    """The process died here."""


@dataclass
class DiesAfterNextWriteCommit:
    """An event-journal backend whose process ends as its next write commits.

    The window a two-commit handoff opens is *between* commits, so a probe for
    it has to sit at the commit boundary. A wrapper one layer up can only stop
    between whole store calls, and would step straight over a store method that
    quietly runs two write transactions of its own -- passing while the very
    ordering it was written to forbid is back in production.
    """

    inner: "Backend"
    armed: bool = False
    # How many write transactions have committed through this view, so a test
    # can say which side of a commit something else happened on.
    commits: int = 0

    async def write[T](self, operation: "Operation[T]") -> T:
        """Commit one write transaction, then die if this was the armed one."""
        result = await self.inner.write(operation)
        self.commits += 1
        if self.armed:
            self.armed = False
            msg = "crashed the instant a write committed"
            raise CrashError(msg)
        return result

    async def read[T](self, operation: "Operation[T]") -> T:
        """Run one read transaction."""
        return await self.inner.read(operation)

    async def close(self) -> None:
        """Do nothing: the wrapped backend outlives this view of it."""


@dataclass
class DiesAfterAcknowledgement:
    """One principal's outbox, whose process ends as an outcome is recorded.

    Only useful for boundaries that are one store call and one write, which
    acknowledgement is. A crash between two writes inside a single store call
    is invisible from here, so probing for that needs
    ``DiesAfterNextWriteCommit`` instead.
    """

    inner: OutboxView

    async def enqueue_delivery(
        self,
        *,
        turn_id: str,
        stage: DeliveryStage,
        room_id: str,
        thread_id: str | None,
        payload: Mapping[str, object],
        edits_event_id: str | None = None,
        settle_source_event_ids: tuple[str, ...] = (),
    ) -> str | None:
        """Record delivery intent."""
        return await self.inner.enqueue_delivery(
            turn_id=turn_id,
            stage=stage,
            room_id=room_id,
            thread_id=thread_id,
            payload=payload,
            edits_event_id=edits_event_id,
            settle_source_event_ids=settle_source_event_ids,
        )

    async def turn_membership_is_current(self, *, turn_id: str, room_id: str) -> bool:
        """Return whether a turn still speaks for the room's current membership."""
        return await self.inner.turn_membership_is_current(turn_id=turn_id, room_id=room_id)

    async def claim_delivery(self, *, turn_id: str, stage: DeliveryStage) -> OutboxDelivery | None:
        """Freeze one delivery before network I/O and return what to send."""
        return await self.inner.claim_delivery(turn_id=turn_id, stage=stage)

    async def record_sending_device(
        self,
        *,
        turn_id: str,
        stage: DeliveryStage,
        device_id: str | None,
    ) -> None:
        """Record the device namespace this delivery is about to send under."""
        await self.inner.record_sending_device(turn_id=turn_id, stage=stage, device_id=device_id)

    async def load_delivery(self, *, turn_id: str, stage: DeliveryStage) -> OutboxDelivery | None:
        """Return one delivery without claiming it."""
        return await self.inner.load_delivery(turn_id=turn_id, stage=stage)

    async def acknowledge_delivery(
        self,
        *,
        turn_id: str,
        stage: DeliveryStage,
        event_id: str,
        terminal_turn: TerminalTurnWrite | None = None,
    ) -> DeliveryAcknowledgement:
        """Record the Matrix outcome, then die before anything else can run."""
        await self.inner.acknowledge_delivery(
            turn_id=turn_id,
            stage=stage,
            event_id=event_id,
            terminal_turn=terminal_turn,
        )
        msg = "crashed the instant the outcome was recorded"
        raise CrashError(msg)

    async def unacknowledged_deliveries(
        self,
        *,
        limit: int = 256,
        after: tuple[int, str, str] | None = None,
    ) -> tuple[OutboxDelivery, ...]:
        """Return deliveries whose Matrix outcome is unknown, oldest first."""
        return await self.inner.unacknowledged_deliveries(limit=limit, after=after)


# Drops contract 2's journal handoff, for tests that are not about it. Named
# rather than a bare lambda so a reader can see that the handoff was
# deliberately stubbed here, not forgotten. Tests that care what the handoff
# settles use a real journal store instead.
ignore_final_delivery_handoff = TurnHandoff(
    sources_for_turn=lambda _turn_id: (),
    released=lambda _event_ids: None,
)


@dataclass
class FakePendingTurnStore:
    """A journal holding no unfinished work, for tests that are not about the replay guard.

    The empty answer is the one that changes nothing: the degraded replay
    guard acts only on positive proof, so a store with nothing pending never
    suppresses a turn. Tests that are about the guard admit into a real journal
    instead, because the filtering they exercise happens in SQL.
    """

    async def pending_thread_events_after(
        self,
        *,
        room_id: str,  # noqa: ARG002 - part of the view's shape
        thread_id: str,  # noqa: ARG002 - part of the view's shape
        after_origin_server_ts: int,  # noqa: ARG002 - part of the view's shape
        excluding_event_id: str,  # noqa: ARG002 - part of the view's shape
        limit: int = 256,  # noqa: ARG002 - part of the view's shape
    ) -> tuple[JournalEvent, ...]:
        """Return no unsettled events in this thread."""
        return ()


def make_pending_turn_view() -> PendingTurnView:
    """Return a journal view that reports no unfinished work in any conversation."""
    return cast("PendingTurnView", FakePendingTurnStore())


@dataclass
class FakeRelationStore:
    """The journal's answer about which events it admitted, and their threads."""

    threads: dict[str, str | None] = field(default_factory=dict)
    asked: list[tuple[str, str]] = field(default_factory=list)
    failure: Exception | None = None

    async def admitted_thread_id(self, *, room_id: str, event_id: str) -> tuple[bool, str | None]:
        """Return whether one event was admitted, and the thread it belongs to."""
        self.asked.append((room_id, event_id))
        if self.failure is not None:
            raise self.failure
        if event_id not in self.threads:
            return False, None
        return True, self.threads[event_id]


def install_relation_lookup(
    bot: object,
    *,
    threads: dict[str, str | None] | None = None,
    client: object | None = None,
    failure: Exception | None = None,
) -> FakeRelationStore:
    """Point one bot's resolver at a relation lookup a test controls.

    Returns the journal stand-in so a test can assert what was asked of it.
    `RelationLookup` is frozen, so the whole collaborator is replaced rather
    than patched.
    """
    store = FakeRelationStore(threads=dict(threads or {}), failure=failure)
    # The bot's own client unless a test says otherwise, so mocks a test already
    # placed on it keep answering and keep being observable.
    resolved_client = SimpleNamespace(client=client if client is not None else bot.client)  # type: ignore[attr-defined]
    lookup = RelationLookup(
        store=cast("RelationView", store),
        runtime=resolved_client,  # type: ignore[arg-type]
    )
    bot._relations = lookup  # type: ignore[attr-defined]
    # Tests wrap collaborators in a proxy, and setting `deps` on the wrapper
    # would leave the resolver that actually runs holding its old ones.
    resolver = unwrap_extracted_collaborator(bot._conversation_resolver)  # type: ignore[attr-defined]
    resolver.deps = replace(resolver.deps, relations=lookup)
    return store


def make_relation_lookup(
    *,
    threads: dict[str, str | None] | None = None,
    client: object | None = None,
) -> RelationLookup:
    """Return a real relation lookup over an in-memory set of admitted events.

    The real object rather than a mock, so a caller that starts relying on the
    journal-before-homeserver order, or on the per-turn memo, is tested against
    the behaviour it will actually get. Without an explicit ``client`` the
    homeserver serves a plain text event for any ID, which is what the old
    cache double did.
    """
    resolved_client = SimpleNamespace(client=client) if client is not None else _serving_client()
    return RelationLookup(
        store=cast("RelationView", FakeRelationStore(threads=dict(threads or {}))),
        runtime=resolved_client,  # type: ignore[arg-type]
    )


def _serving_client() -> SimpleNamespace:
    """Return a runtime whose homeserver answers any point lookup."""
    client = MagicMock()
    client.room_get_event = AsyncMock(side_effect=lambda _room_id, event_id: _make_room_get_event_response(event_id))
    return SimpleNamespace(client=client)


def make_latest_thread_event_id_mock(projected: str | None = None) -> AsyncMock:
    """Return a reply-fallback stand-in that follows the real precedence.

    A double that answered one fixed value would let a caller stop passing
    ``known_latest_thread_event_id`` -- or start passing one where a deliberate
    reply target already decided the answer -- and still pass. ``projected`` is
    what the projection would report; without it, an empty thread answers with
    its own root, exactly as the real reader does.
    """

    async def _answer(
        *,
        room_id: str,  # noqa: ARG001 - part of the signature under test
        thread_id: str | None,
        reply_to_event_id: str | None = None,
        existing_event_id: str | None = None,
        known_latest_thread_event_id: str | None = None,
    ) -> str | None:
        if thread_id is None or existing_event_id is not None or reply_to_event_id is not None:
            return None
        return known_latest_thread_event_id or projected or thread_id

    return AsyncMock(side_effect=_answer)


def make_conversation_reader_mock() -> ConversationReader:
    """Return a reader shaped like the projection one, serving an empty conversation."""
    page = ConversationPage(messages=(), refresh_pending=(), next_cursor=None)
    return cast(
        "ConversationReader",
        SimpleNamespace(
            may_have_unread_history=AsyncMock(return_value=False),
            hydration_was_truncated=AsyncMock(return_value=False),
            read=AsyncMock(return_value=page),
            read_strict=AsyncMock(return_value=page),
            latest_thread_event_id=AsyncMock(return_value=None),
        ),
    )


def install_runtime_journal_support(bot: RuntimeBot) -> RuntimeBot:
    """Pin the journal identity a test bot certifies its sync checkpoints against.

    The real generation is a fresh UUID per database, so a test that saves a
    checkpoint and restarts would exercise the first-open mint rejecting it
    rather than the token logic it means to test.
    """
    bot._sync_checkpoint_trust.store_generation = "test-store-generation"
    sync_bot_runtime_state(bot)
    return bot


def install_call_manager_mock(bot: RuntimeBot, call_manager: object | None) -> None:
    """Install a call-manager fake through the shared test seam."""
    bot._call_manager = cast("CallManager | None", call_manager)


def normalize_console_output(text: str) -> str:
    """Collapse wrapped console output for stable substring assertions."""
    return " ".join(_SOFT_WRAP_RE.sub("", _ANSI_RE.sub("", text)).split())


class _ExtractedCollaboratorProxy[CollaboratorT]:
    """Mutable proxy that keeps real collaborator attributes visible to tests."""

    def __init__(self, wrapped: CollaboratorT) -> None:
        object.__setattr__(self, "_wrapped", wrapped)

    def __getattr__(self, name: str) -> object:
        proxy_dict = object.__getattribute__(self, "__dict__")
        if name in proxy_dict:
            return proxy_dict[name]
        return getattr(object.__getattribute__(self, "_wrapped"), name)

    def __setattr__(self, name: str, value: object) -> None:
        object.__getattribute__(self, "__dict__")[name] = value

    def __delattr__(self, name: str) -> None:
        proxy_dict = object.__getattribute__(self, "__dict__")
        if name in proxy_dict:
            del proxy_dict[name]
            return
        msg = f"{type(self).__name__!s} has no attribute {name!r}"
        raise AttributeError(msg)


class FakeModel(Model):
    """Minimal model returning one canned response, for deterministic agent tests."""

    def invoke(self, *_args: object, **_kwargs: object) -> ModelResponse:
        """Return one successful fake response."""
        return ModelResponse(content="ok")

    async def ainvoke(self, *_args: object, **_kwargs: object) -> ModelResponse:
        """Return one successful fake async response."""
        return ModelResponse(content="ok")

    def invoke_stream(self, *_args: object, **_kwargs: object) -> Iterator[ModelResponse]:
        """Yield one successful fake streaming response."""
        yield ModelResponse(content="ok")

    async def ainvoke_stream(self, *_args: object, **_kwargs: object) -> AsyncIterator[ModelResponse]:
        """Yield one successful fake async streaming response."""
        yield ModelResponse(content="ok")

    def _parse_provider_response(self, response: ModelResponse, *_args: object, **_kwargs: object) -> ModelResponse:
        return response

    def _parse_provider_response_delta(
        self,
        response: ModelResponse,
        *_args: object,
        **_kwargs: object,
    ) -> ModelResponse:
        return response


class FakeCredentialsManager:
    """Stub credentials manager for tests that need credential lookup."""

    def __init__(
        self,
        credentials_by_service: dict[str, dict[str, object]],
        worker_managers: dict[str, "FakeCredentialsManager"] | None = None,
        *,
        storage_root: Path | None = None,
        current_worker_key: str | None = None,
        current_worker_root: Path | None = None,
    ) -> None:
        self._credentials_by_service = credentials_by_service
        self._worker_managers = worker_managers or {}
        self.storage_root = storage_root or Path("/var/empty/mindroom-fake-storage")
        self.base_path = self.storage_root / "credentials"
        self.shared_base_path = self.base_path
        self.current_worker_key = current_worker_key
        self.current_worker_root = current_worker_root

    def load_credentials(self, service: str) -> dict[str, object]:
        """Return stored credentials for *service*, or empty dict."""
        return self._credentials_by_service.get(service, {})

    def for_worker(self, worker_key: str) -> "FakeCredentialsManager":
        """Return a worker-scoped credentials manager."""
        return self._worker_managers.get(
            worker_key,
            FakeCredentialsManager(
                {},
                storage_root=self.storage_root / "workers" / worker_key,
                current_worker_key=worker_key,
                current_worker_root=self.storage_root / "workers" / worker_key,
            ),
        )

    def shared_manager(self) -> "FakeCredentialsManager":
        """Return the shared credential layer for this fake manager."""
        return self


def pytest_collection_modifyitems(items: list[pytest.Item]) -> None:
    """Skip tests marked with requires_matrix unless MATRIX_SERVER_URL is set."""
    if os.environ.get("MATRIX_SERVER_URL"):
        # Matrix server available, don't skip
        return

    skip_marker = pytest.mark.skip(reason="requires_matrix: no MATRIX_SERVER_URL set")
    for item in items:
        if "requires_matrix" in item.keywords:
            item.add_marker(skip_marker)


# Test credentials constants - not real credentials, safe for testing
TEST_PASSWORD = "mock_test_password"  # noqa: S105
TEST_ACCESS_TOKEN = "mock_test_token"  # noqa: S105


def test_runtime_paths(tmp_root: Path) -> RuntimePaths:
    """Create an isolated runtime context for one test config."""
    tmp_root.mkdir(parents=True, exist_ok=True)
    config_path = tmp_root / "config.yaml"
    config_path.write_text("router:\n  model: default\n", encoding="utf-8")
    return resolve_runtime_paths(
        config_path=config_path,
        storage_path=tmp_root / "mindroom_data",
        process_env={
            "MATRIX_HOMESERVER": "http://localhost:8008",
            "MINDROOM_NAMESPACE": "",
        },
    )


test_runtime_paths.__test__ = False


def orchestrator_runtime_paths(
    storage_path: Path,
    *,
    config_path: Path | None = None,
) -> RuntimePaths:
    """Build an explicit runtime context for orchestrator tests.

    Default the config path to an isolated file under the provided test root so
    callers never fall back to the tracked repo-root config.yaml.
    """
    if config_path is None:
        config_path = storage_path / "config.yaml"
    return resolve_runtime_paths(
        config_path=config_path,
        storage_path=storage_path,
        process_env={
            "MATRIX_HOMESERVER": "http://localhost:8008",
            "MINDROOM_NAMESPACE": "",
        },
    )


def load_config_yaml(config_path: Path) -> Config:
    """Load a config YAML file through the production runtime-aware loader."""
    return load_config(resolve_runtime_paths(config_path=Path(config_path).expanduser().resolve()))


def write_config_yaml(config: Config, config_path: Path) -> None:
    """Write a test config using the authored YAML representation."""
    path = Path(config_path)
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp_path = path.with_suffix(path.suffix + ".tmp")
    with tmp_path.open("w", encoding="utf-8") as f:
        yaml.dump(
            config.authored_model_dump(),
            f,
            default_flow_style=False,
            sort_keys=True,
            allow_unicode=True,
            width=120,
        )
    safe_replace(tmp_path, path)


def bind_runtime_paths(
    config: Config,
    runtime_paths: RuntimePaths,
) -> Config:
    """Return a runtime-bound copy of a test config."""
    bound = Config.validate_with_runtime(config.authored_model_dump(), runtime_paths)
    _persist_bound_entity_accounts(bound, runtime_paths)
    authored_coalescing = config.defaults.coalescing
    if "debounce_ms" not in authored_coalescing.model_fields_set:
        bound.defaults.coalescing.debounce_ms = 0
    _TEST_RUNTIME_PATHS_BY_CONFIG_ID[id(bound)] = runtime_paths
    return bound


def _persist_bound_entity_accounts(config: Config, runtime_paths: RuntimePaths) -> None:
    """Prepare managed Matrix accounts for tests that bind runtime config."""
    persist_entity_accounts(config, runtime_paths)


def bind_mock_config_event_journal(mock_config: MagicMock) -> None:
    """Give a config mock the durable-store contract the bot runtime reads."""
    mock_config.event_journal.backend = "sqlite"


def runtime_paths_for(config: Config) -> RuntimePaths:
    """Return the explicit runtime context previously bound to a test config."""
    runtime_paths = _TEST_RUNTIME_PATHS_BY_CONFIG_ID.get(id(config))
    if runtime_paths is None:
        msg = "Test config is missing bound RuntimePaths"
        raise KeyError(msg)
    return runtime_paths


def create_mock_room(
    room_id: str = "!test:localhost",
    agents: list[str] | None = None,
    config: Config | None = None,
) -> MagicMock:
    """Create a mock room with specified agents."""
    room = MagicMock()
    room.room_id = room_id
    if agents:
        domain = config.get_domain(runtime_paths_for(config)) if config is not None else "localhost"
        room.users = {f"@mindroom_{agent}:{domain}": None for agent in agents}
    else:
        room.users = {}
    return room


def make_turn_context(
    entity_label: str = "test_agent",
    *,
    session_id: str | None = "test_session",
    run_id: str | None = None,
    correlation_id: str = "corr-test",
    reply_to_event_id: str | None = None,
    room_id: str | None = None,
    thread_id: str | None = None,
    requester_id: str | None = None,
    matrix_run_metadata: dict[str, Any] | None = None,
    active_event_ids: frozenset[str] = frozenset(),
    transient_enrichment_items: tuple[EnrichmentItem, ...] = (),
    system_enrichment_items: tuple[EnrichmentItem, ...] = (),
    scheduled_history_budget: ScheduledHistoryBudget | None = None,
) -> ResponseTurnContext:
    """Build one response-turn context with test defaults."""
    return ResponseTurnContext(
        entity_label=entity_label,
        session_id=session_id,
        run_id=run_id,
        correlation_id=correlation_id,
        reply_to_event_id=reply_to_event_id,
        room_id=room_id,
        thread_id=thread_id,
        requester_id=requester_id,
        matrix_run_metadata=matrix_run_metadata,
        active_event_ids=active_event_ids,
        transient_enrichment_items=transient_enrichment_items,
        system_enrichment_items=system_enrichment_items,
        scheduled_history_budget=scheduled_history_budget,
    )


def make_visible_message(
    *,
    sender: str = "@user:localhost",
    body: str = "",
    event_id: str | None = None,
    timestamp: int = 0,
    content: dict[str, object] | None = None,
    thread_id: str | None = None,
) -> ResolvedVisibleMessage:
    """Build one typed visible message for thread/history tests."""
    resolved_content = dict(content) if isinstance(content, dict) else {}
    if "body" not in resolved_content and body:
        resolved_content["body"] = body
    return ResolvedVisibleMessage.synthetic(
        sender=sender,
        body=body,
        event_id=event_id or f"$visible-{next(_VISIBLE_MESSAGE_IDS)}",
        timestamp=timestamp,
        content=resolved_content or None,
        thread_id=thread_id,
    )


def unwrap_extracted_collaborator[T](collaborator: T) -> T:
    """Return the real extracted collaborator behind one test wrapper."""
    if isinstance(collaborator, MagicMock):
        wrapped = collaborator._mock_wraps
        if wrapped is not None:
            return wrapped
    wrapped = getattr(collaborator, "_wrapped", None)
    if wrapped is not None:
        return wrapped
    return collaborator


def wrap_extracted_collaborators(bot: RuntimeBot, *names: str) -> RuntimeBot:
    """Wrap frozen extracted collaborators so tests can patch their methods."""
    sync_bot_runtime_state(bot)
    collaborator_names = names or (
        "_turn_policy",
        "_delivery_gateway",
        "_response_runner",
        "_turn_store",
        "_visible_voice_echo",
        "_edit_regenerator",
        "_inbound_turn_normalizer",
        "_conversation_resolver",
        "_conversation_state_writer",
    )
    for name in collaborator_names:
        collaborator = getattr(bot, name)
        if isinstance(collaborator, MagicMock | _ExtractedCollaboratorProxy):
            continue
        setattr(bot, name, _ExtractedCollaboratorProxy(collaborator))
    _sync_request_payload_preparer(bot)
    return bot


def _sync_request_payload_preparer(bot: RuntimeBot) -> None:
    """Repoint the response runner's payload preparer at the current collaborators.

    The preparer captures the normalizer and ingress hook runner; tests swap
    those for proxies after construction, so rebuild the preparer to track them.
    """
    runner = unwrap_extracted_collaborator(bot._response_runner)
    preparer = ResponsePayloadPreparer(
        normalizer=bot._inbound_turn_normalizer,
        ingress_hook_runner=bot._ingress_hook_runner,
        agent_name=runner.deps.agent_name,
        logger=runner.deps.logger,
    )
    bot._request_payload_preparer = preparer
    runner.deps = replace(runner.deps, request_preparer=preparer)


async def prepare_payload_via_seam(bot: RuntimeBot, execute_args: tuple[object, ...]) -> None:
    """Drive the execution-side payload preparation from captured dispatch args."""
    event = cast("DispatchEvent", execute_args[1])
    dispatch = cast("PreparedDispatch", execute_args[2])
    payload_inputs = cast("DispatchPayloadInputs", execute_args[4])
    await bot._request_payload_preparer.prepare(
        ResponseRequest(
            thread_history=dispatch.context.thread_history,
            prompt=event.body,
            response_envelope=dispatch.envelope,
            payload_preparation=ResponsePayloadPreparation(
                dispatch=dispatch,
                prompt=event.body,
                action_kind="individual",
                payload_inputs=payload_inputs,
                target_member_names=None,
                dispatch_started_at=0.0,
                context_ready_monotonic=0.0,
            ),
        ),
    )


def sync_bot_runtime_state(bot: RuntimeBot) -> None:
    """Update the extracted runtime state after tests mutate bot internals."""
    runtime = bot._runtime_view
    client = bot.client
    if client is not None and getattr(client, "user_id", None) is None:
        client.user_id = bot.matrix_id.full_id
    runtime.client = bot.client
    runtime.config = bot.config
    runtime.enable_streaming = bot.enable_streaming
    runtime.orchestrator = bot.orchestrator


def replace_turn_policy_deps(bot: RuntimeBot, **changes: object) -> TurnPolicy:
    """Rebuild the turn policy after swapping collaborators captured at construction."""
    sync_bot_runtime_state(bot)
    policy = unwrap_extracted_collaborator(bot._turn_policy)
    policy_field_names = set(policy.deps.__dataclass_fields__)
    policy_changes = {name: value for name, value in changes.items() if name in policy_field_names}
    rebuilt = TurnPolicy(replace(policy.deps, **policy_changes)) if policy_changes else policy
    bot._turn_policy = rebuilt
    wrap_extracted_collaborators(bot, "_turn_policy")
    store_field_names = set(unwrap_extracted_collaborator(bot._turn_store).deps.__dataclass_fields__)
    store_changes = {name: value for name, value in changes.items() if name in store_field_names}
    if store_changes:
        replace_turn_store_deps(bot, **store_changes)
    controller = unwrap_extracted_collaborator(bot._turn_controller)
    controller_field_names = set(controller.deps.__dataclass_fields__)
    controller_changes = {name: value for name, value in changes.items() if name in controller_field_names}
    if policy_changes:
        controller_changes["turn_policy"] = bot._turn_policy
    if store_changes:
        controller_changes["turn_store"] = bot._turn_store
    if controller_changes:
        replace_turn_controller_deps(bot, **controller_changes)
    return rebuilt


def replace_turn_store_deps(bot: RuntimeBot, **changes: object) -> TurnStore:
    """Rebuild the turn store after swapping collaborators captured at construction."""
    sync_bot_runtime_state(bot)
    store = unwrap_extracted_collaborator(bot._turn_store)
    rebuilt = TurnStore(replace(store.deps, **changes))
    bot._turn_store = rebuilt
    wrap_extracted_collaborators(bot, "_turn_store")
    return rebuilt


def replace_delivery_gateway_deps(bot: RuntimeBot, **changes: object) -> DeliveryGateway:
    """Rebuild the delivery gateway after swapping captured collaborators."""
    sync_bot_runtime_state(bot)
    gateway = unwrap_extracted_collaborator(bot._delivery_gateway)
    rebuilt = DeliveryGateway(replace(gateway.deps, **changes))
    bot._delivery_gateway = rebuilt
    wrap_extracted_collaborators(bot, "_delivery_gateway")
    replace_turn_controller_deps(bot, delivery_gateway=bot._delivery_gateway)
    replace_response_runner_deps(bot, delivery_gateway=bot._delivery_gateway)
    return rebuilt


def replace_response_runner_deps(bot: RuntimeBot, **changes: object) -> ResponseRunner:
    """Rebuild the response runner after swapping captured collaborators."""
    sync_bot_runtime_state(bot)
    coordinator = unwrap_extracted_collaborator(bot._response_runner)
    rebuilt = ResponseRunner(replace(coordinator.deps, **changes))
    bot._response_runner = rebuilt
    wrap_extracted_collaborators(bot, "_response_runner")
    replace_turn_controller_deps(bot, response_runner=bot._response_runner)
    return rebuilt


def replace_edit_regenerator_deps(bot: RuntimeBot, **changes: object) -> EditRegenerator:
    """Rebuild the edit regenerator after swapping captured collaborators."""
    install_runtime_journal_support(bot)
    regenerator = unwrap_extracted_collaborator(bot._edit_regenerator)
    regenerator_field_names = set(regenerator.deps.__dataclass_fields__)
    rebuilt_changes = {
        name: value for name, value in changes.items() if name in regenerator_field_names or name == "logger"
    }
    if "logger" in rebuilt_changes:
        logger = rebuilt_changes.pop("logger")
        rebuilt_changes["get_logger"] = lambda logger=logger: logger
    if "receipt_order" not in rebuilt_changes:
        receipt_orders = count(1)

        async def next_receipt_order() -> int:
            return next(receipt_orders)

        rebuilt_changes["receipt_order"] = next_receipt_order
    store_field_names = set(unwrap_extracted_collaborator(bot._turn_store).deps.__dataclass_fields__)
    store_changes = {name: value for name, value in changes.items() if name in store_field_names}
    if store_changes:
        replace_turn_store_deps(bot, **store_changes)
        rebuilt_changes["turn_store"] = bot._turn_store
    rebuilt = EditRegenerator(replace(regenerator.deps, **rebuilt_changes))
    bot._edit_regenerator = rebuilt
    wrap_extracted_collaborators(bot, "_edit_regenerator")
    replace_turn_controller_deps(bot, edit_regenerator=bot._edit_regenerator)
    return rebuilt


def replace_reaction_dispatcher_deps(bot: RuntimeBot, **changes: object) -> ReactionDispatcher:
    """Rebuild reaction dispatch after swapping collaborators captured at construction."""
    rebuilt = ReactionDispatcher(replace(bot._reaction_dispatcher.deps, **changes))
    bot._reaction_dispatcher = rebuilt
    return rebuilt


def replace_turn_controller_deps(bot: RuntimeBot, **changes: object) -> TurnController:
    """Rebuild the turn controller after swapping collaborators captured at construction."""
    sync_bot_runtime_state(bot)
    controller = unwrap_extracted_collaborator(bot._turn_controller)
    controller_field_names = set(controller.deps.__dataclass_fields__)
    rebuilt_changes = {name: value for name, value in changes.items() if name in controller_field_names}
    default_collaborators = {
        "resolver": "_conversation_resolver",
        "normalizer": "_inbound_turn_normalizer",
        "command_executor": "_command_turn_executor",
        "turn_policy": "_turn_policy",
        "ingress_hook_runner": "_ingress_hook_runner",
        "response_runner": "_response_runner",
        "delivery_gateway": "_delivery_gateway",
        "tool_runtime": "_tool_runtime_support",
        "turn_store": "_turn_store",
        "edit_regenerator": "_edit_regenerator",
        "visible_responses": "_visible_responses",
    }
    for field_name, attr_name in default_collaborators.items():
        if field_name in rebuilt_changes:
            continue
        rebuilt_changes[field_name] = getattr(bot, attr_name)
    store_field_names = set(unwrap_extracted_collaborator(bot._turn_store).deps.__dataclass_fields__)
    store_changes = {name: value for name, value in changes.items() if name in store_field_names}
    if store_changes:
        replace_turn_store_deps(bot, **store_changes)
        rebuilt_changes["turn_store"] = bot._turn_store
    if "edit_regenerator" not in rebuilt_changes:
        rebuilt_changes["edit_regenerator"] = bot._edit_regenerator
    if "ingress" not in rebuilt_changes:
        rebuilt_changes["ingress"] = IngressValidator(
            replace(
                bot._ingress_validator.deps,
                turn_store=rebuilt_changes["turn_store"],
                turn_policy=rebuilt_changes["turn_policy"],
            ),
        )
    bot._ingress_validator = rebuilt_changes["ingress"]
    visible_voice_echo = unwrap_extracted_collaborator(bot._visible_voice_echo)
    bot._visible_voice_echo = VisibleVoiceEchoLifecycle(
        replace(
            visible_voice_echo.deps,
            runtime=rebuilt_changes.get("runtime", controller.deps.runtime),
            logger=rebuilt_changes.get("logger", controller.deps.logger),
            agent_name=rebuilt_changes.get("agent_name", controller.deps.agent_name),
            delivery_gateway=rebuilt_changes["delivery_gateway"],
            turn_store=rebuilt_changes["turn_store"],
            ingress=rebuilt_changes["ingress"],
        ),
    )
    wrap_extracted_collaborators(bot, "_visible_voice_echo")
    rebuilt_changes["visible_voice_echo"] = bot._visible_voice_echo
    visible_responses = unwrap_extracted_collaborator(bot._visible_responses)
    visible_response_changes = {
        name: value
        for name, value in changes.items()
        if name in visible_responses.deps.__dataclass_fields__
        and name not in {"runtime", "logger", "turn_store", "delivery_gateway"}
    }
    bot._visible_responses = VisibleResponseReconciler(
        replace(
            visible_responses.deps,
            runtime=rebuilt_changes.get("runtime", controller.deps.runtime),
            logger=rebuilt_changes.get("logger", controller.deps.logger),
            turn_store=rebuilt_changes["turn_store"],
            delivery_gateway=rebuilt_changes["delivery_gateway"],
            **visible_response_changes,
        ),
    )
    wrap_extracted_collaborators(bot, "_visible_responses")
    rebuilt_changes["visible_responses"] = bot._visible_responses
    command_executor = unwrap_extracted_collaborator(bot._command_turn_executor)
    command_changes = {
        name: value
        for name, value in changes.items()
        if name in command_executor.deps.__dataclass_fields__
        and name
        not in {
            "runtime",
            "logger",
            "runtime_paths",
            "agent_name",
            "normalizer",
            "turn_policy",
            "turn_store",
            "visible_responses",
        }
    }
    bot._command_turn_executor = CommandTurnExecutor(
        replace(
            command_executor.deps,
            runtime=rebuilt_changes.get("runtime", controller.deps.runtime),
            logger=rebuilt_changes.get("logger", controller.deps.logger),
            runtime_paths=rebuilt_changes.get("runtime_paths", controller.deps.runtime_paths),
            agent_name=rebuilt_changes.get("agent_name", controller.deps.agent_name),
            normalizer=rebuilt_changes["normalizer"],
            turn_policy=rebuilt_changes["turn_policy"],
            turn_store=rebuilt_changes["turn_store"],
            visible_responses=rebuilt_changes["visible_responses"],
            **command_changes,
        ),
    )
    wrap_extracted_collaborators(bot, "_command_turn_executor")
    rebuilt_changes["command_executor"] = bot._command_turn_executor
    user_stop_reconciler = unwrap_extracted_collaborator(bot._user_stop_reconciler)
    bot._user_stop_reconciler = UserStopReconciler(
        replace(
            user_stop_reconciler.deps,
            turn_store=rebuilt_changes["turn_store"],
            response_runner=rebuilt_changes["response_runner"],
            delivery_gateway=rebuilt_changes["delivery_gateway"],
        ),
    )
    wrap_extracted_collaborators(bot, "_user_stop_reconciler")
    rebuilt = TurnController(replace(controller.deps, **rebuilt_changes))
    bot._turn_controller = rebuilt
    reaction_dispatcher = bot._reaction_dispatcher
    replace_reaction_dispatcher_deps(
        bot,
        runtime=rebuilt.deps.runtime,
        logger=rebuilt.deps.logger,
        runtime_paths=rebuilt.deps.runtime_paths,
        agent_name=rebuilt.deps.agent_name,
        turn_policy=rebuilt.deps.turn_policy,
        turn_store=rebuilt.deps.turn_store,
        user_stop_reconciler=bot._user_stop_reconciler,
        ingress=rebuilt.deps.ingress,
        stop_manager=bot.stop_manager,
        reserve_prompt_ingress_order=rebuilt.reserve_prompt_ingress_order,
        handle_interactive_selection=rebuilt.handle_interactive_selection,
        config_confirmation=replace(
            reaction_dispatcher.deps.config_confirmation,
            runtime=rebuilt.deps.runtime,
            runtime_paths=rebuilt.deps.runtime_paths,
            build_message_target=rebuilt.deps.resolver.build_message_target,
            delivery_gateway=rebuilt.deps.delivery_gateway,
        ),
    )
    edit_changes = {
        name: value
        for name, value in changes.items()
        if name in unwrap_extracted_collaborator(bot._edit_regenerator).deps.__dataclass_fields__
    }
    if edit_changes:
        replace_edit_regenerator_deps(bot, **edit_changes)
    return rebuilt


@contextmanager
def patch_response_runner_module(**changes: object) -> Generator[None, None, None]:
    """Patch module-level response coordinator seams on the real current owner."""
    with ExitStack() as stack:
        for name, replacement in changes.items():
            module_name = (
                "mindroom.response_lifecycle" if name == "apply_post_response_effects" else "mindroom.response_runner"
            )
            stack.enter_context(patch(f"{module_name}.{name}", new=replacement))
        yield


def install_shutdown_drain_mocks(
    bot: RuntimeBot,
    *,
    coalescing_drain_result: CoalescingDrainResult,
    responses_drained: bool,
    response_recovery_complete: bool,
) -> None:
    """Install exact shutdown drain outcomes through stable collaborator seams."""
    wrap_extracted_collaborators(bot, "_coalescing_gate", "_response_runner")
    bot._coalescing_gate.drain_all = AsyncMock(
        return_value=coalescing_drain_result,
    )
    bot._response_runner.drain_inbox_responses = AsyncMock(return_value=responses_drained)
    unwrap_extracted_collaborator(
        bot._response_runner,
    )._incomplete_inbox_responses_recoverable = response_recovery_complete


def install_send_response_mock(bot: RuntimeBot, send_response: AsyncMock) -> None:
    """Route visible delivery through one target-explicit send-response mock."""
    wrap_extracted_collaborators(bot, "_delivery_gateway")

    async def _send_text(request: SendTextRequest) -> str | None:
        return await send_response(
            target=request.target,
            response_text=request.response_text,
            skip_mentions=request.skip_mentions,
            tool_trace=request.tool_trace,
            extra_content=request.extra_content,
        )

    bot._delivery_gateway.send_text = AsyncMock(side_effect=_send_text)

    async def _deliver_final(request: FinalDeliveryRequest) -> FinalDeliveryOutcome:
        event_id = await send_response(
            target=request.target,
            response_text=request.response_text,
            skip_mentions=request.skip_mentions,
            tool_trace=request.tool_trace,
            extra_content=request.extra_content,
        )
        delivery_kind = "edited" if request.existing_event_id is not None else "sent"
        if event_id is None:
            if request.existing_event_id is not None:
                return _outcome(
                    terminal_status="error",
                    event_id=request.existing_event_id,
                    is_visible_response=True,
                    final_visible_body=request.response_text,
                    failure_reason="test_mock_no_visible_response",
                    extra_content=request.extra_content,
                )
            return _outcome(
                terminal_status="error",
                failure_reason="test_mock_no_visible_response",
            )
        return _outcome(
            terminal_status="completed",
            event_id=event_id,
            is_visible_response=True,
            final_visible_body=request.response_text,
            delivery_kind=delivery_kind,
            extra_content=request.extra_content,
        )

    bot._delivery_gateway.deliver_final = AsyncMock(side_effect=_deliver_final)
    replace_turn_controller_deps(bot, delivery_gateway=bot._delivery_gateway)
    replace_response_runner_deps(bot, delivery_gateway=bot._delivery_gateway)


def install_generate_response_mock(bot: RuntimeBot, generate_response: AsyncMock) -> None:
    """Route response execution through one envelope-explicit generate-response mock."""
    wrap_extracted_collaborators(bot, "_response_runner")

    def _resolved_event_id_from_test_result(
        result: FinalDeliveryOutcome | str | None,
    ) -> str | None:
        if isinstance(result, FinalDeliveryOutcome):
            return result.final_visible_event_id
        return result

    async def _generate(request: ResponseRequest) -> str | None:
        if request.payload_preparation is not None:
            try:
                request = await bot._request_payload_preparer.prepare(request)
            except Exception as exc:
                raise PostLockRequestPreparationError from exc
        attachment_ids = list(request.attachment_ids) if request.attachment_ids is not None else None
        result = await generate_response(
            prompt=request.prompt,
            thread_history=request.thread_history,
            existing_event_id=request.existing_event_id,
            existing_event_is_placeholder=request.existing_event_is_placeholder,
            user_id=request.user_id,
            media=request.media,
            attachment_ids=attachment_ids,
            model_prompt=request.model_prompt,
            transient_enrichment_items=request.transient_enrichment_items,
            system_enrichment_items=request.system_enrichment_items,
            response_envelope=request.response_envelope,
            correlation_id=request.correlation_id,
            matrix_run_metadata=request.matrix_run_metadata,
        )
        return _resolved_event_id_from_test_result(result)

    bot._response_runner.generate_response = AsyncMock(side_effect=_generate)
    replace_turn_controller_deps(bot, response_runner=bot._response_runner)


def install_edit_message_mock(bot: RuntimeBot, edit_message: AsyncMock) -> None:
    """Route Matrix edits through one argument-expanded edit-message mock."""
    wrap_extracted_collaborators(bot, "_delivery_gateway")

    async def _edit_text(request: EditTextRequest) -> bool:
        return await edit_message(
            request.target.room_id,
            request.event_id,
            request.new_text,
            request.target.resolved_thread_id,
            tool_trace=request.tool_trace,
            extra_content=request.extra_content,
        )

    bot._delivery_gateway.edit_text = AsyncMock(side_effect=_edit_text)
    replace_turn_controller_deps(bot, delivery_gateway=bot._delivery_gateway)
    replace_response_runner_deps(bot, delivery_gateway=bot._delivery_gateway)


@pytest.fixture
def build_private_template_dir(tmp_path: Path) -> Callable[..., Path]:
    """Return a helper that creates a local private-instance template directory."""

    def _build(
        name: str = "private_template",
        *,
        files: dict[str, str] | None = None,
    ) -> Path:
        template_dir = tmp_path / name
        template_dir.mkdir(parents=True, exist_ok=True)
        template_files = files or {
            "SOUL.md": "Template soul.\n",
            "USER.md": "Template user.\n",
            "MEMORY.md": "# Memory\n",
            "memory/notes.md": "Private note.\n",
        }
        for relative_path, content in template_files.items():
            destination = template_dir / relative_path
            destination.parent.mkdir(parents=True, exist_ok=True)
            destination.write_text(content, encoding="utf-8")
        return template_dir

    return _build


@pytest_asyncio.fixture
async def aioresponse() -> AsyncGenerator[aioresponses, None]:
    """Async fixture for mocking HTTP responses in tests."""
    # Based on https://github.com/matrix-nio/matrix-nio/blob/main/tests/conftest_async.py
    with aioresponses() as m:
        yield m


@pytest.fixture(autouse=True)
def _isolate_structlog_configuration(
    monkeypatch: pytest.MonkeyPatch,
    request: pytest.FixtureRequest,
) -> Generator[None, None, None]:
    """Silence incidental logs and prevent global logging configuration leaks."""
    _configure_quiet_structlog()
    if request.node.path.name != "test_logging_config.py":
        monkeypatch.setattr(structlog, "configure", _configure_uncached_structlog)
    yield
    _configure_quiet_structlog()


@pytest.fixture(autouse=True)
def _pin_matrix_homeserver(monkeypatch: pytest.MonkeyPatch) -> None:
    """Keep test runtime defaults isolated from shell-level runtime overrides.

    Tests use ':localhost' Matrix IDs and non-namespaced localparts unless they
    explicitly opt into a different runtime context.
    """
    monkeypatch.delenv("MATRIX_HOMESERVER", raising=False)
    monkeypatch.delenv("MATRIX_SERVER_NAME", raising=False)
    monkeypatch.delenv("MINDROOM_NAMESPACE", raising=False)
    monkeypatch.delenv("MINDROOM_CONFIG_PATH", raising=False)
    monkeypatch.delenv("MINDROOM_STORAGE_PATH", raising=False)


@pytest.fixture(autouse=True)
def _never_build_the_dashboard(monkeypatch: pytest.MonkeyPatch) -> None:
    """Keep `mindroom run` from shelling out to a real frontend build.

    `ensure_frontend_dist_dir` builds `frontend/dist` with `bun install`, `tsc`
    and `vite build` whenever a source checkout has no dashboard assets, and
    every CLI test that invokes `run` reaches it because only
    `orchestrator.main` is mocked. In a checkout without `frontend/dist` the
    first such test in each xdist worker therefore starts a real multi-minute
    build and blows the suite's 60 s per-test timeout, while the rest of the
    workers race it into the same directory.

    The flag is production's own opt-out, and `RuntimePaths.env_value` reads
    only a captured `process_env`, so tests that pass their own snapshot --
    including the ones that cover the auto-build itself -- are unaffected.
    """
    monkeypatch.setenv("MINDROOM_AUTO_BUILD_FRONTEND", "0")


@pytest.fixture(autouse=True)
def _reset_runtime_paths() -> Generator[None, None, None]:
    """Restore process env and bound test runtime mappings after each test."""
    original_env = os.environ.copy()
    original_bound_configs = dict(_TEST_RUNTIME_PATHS_BY_CONFIG_ID)
    yield
    os.environ.clear()
    os.environ.update(original_env)
    _TEST_RUNTIME_PATHS_BY_CONFIG_ID.clear()
    _TEST_RUNTIME_PATHS_BY_CONFIG_ID.update(original_bound_configs)


@pytest.fixture(autouse=True)
def _reset_model_media_capabilities() -> Generator[None, None, None]:
    """Keep process-local learned media support isolated per test."""
    reset_model_media_capability_cache()
    yield
    reset_model_media_capability_cache()


_LEDGER_LOADING_TEST_MODULES = frozenset(
    {
        "test_handled_turns.py",
        "test_turn_store.py",
        "test_user_stop_convergence.py",
    },
)


@pytest.fixture(autouse=True)
def _reset_handled_turn_ledger_state(
    monkeypatch: pytest.MonkeyPatch,
    request: pytest.FixtureRequest,
) -> Generator[None, None, None]:
    """Give every test a cold handled-turn map, pre-warmed where startup is skipped.

    The map is process-global and ``load`` fills it once per process. Left over
    from a previous test it would satisfy that check, so a ledger opened onto a
    fresh database would answer from the previous test's records and never read
    its own.

    Production warms the ledger during startup and every read refuses until it
    has. Most tests build a bot and drive its callbacks directly without ever
    reaching startup, and their database is empty, so starting the map loaded
    is exactly the state warming it would produce.

    A test whose database is *not* empty must opt out, with the
    ``ledger_loads_from_disk`` marker or by living in one of the modules
    below, and warm for real -- pre-warming would leave it answering from an
    empty map while the rows it cares about sit unread.
    """
    _reset_handled_turn_ledger_runtime()
    loads_from_disk = (
        request.node.path.name in _LEDGER_LOADING_TEST_MODULES
        or request.node.get_closest_marker("ledger_loads_from_disk") is not None
    )
    if not loads_from_disk:
        shared_ledger_state = handled_turns_module._shared_ledger_state

        def pre_warmed_ledger_state(*state_key: str) -> handled_turns_module._LedgerState:
            state = shared_ledger_state(*state_key)
            state.loaded = True
            return state

        monkeypatch.setattr(handled_turns_module, "_shared_ledger_state", pre_warmed_ledger_state)
    yield
    _reset_handled_turn_ledger_runtime()


@pytest.fixture(autouse=True)
def _reset_approval_manager_runtime() -> Generator[None, None, None]:
    """Give every test a cold approval manager.

    The manager is module-global and outlives the test that initialized it. It
    holds Matrix transport hooks, including the approval-card store every
    inbound message consults to recover a click on a card this process has
    forgotten. A test that builds a bot without an orchestrator never sets
    those hooks, so it inherits whichever ones the previous test on this
    worker left behind -- and awaiting a previous test's mock raises inside a
    message callback nothing is watching, which surfaces as an unrelated test
    hanging until its timeout rather than as a failure here.
    """
    approval_manager_module._MANAGER = None
    yield
    approval_manager_module._MANAGER = None


@pytest.fixture(autouse=True)
def _reset_voice_echo_barriers() -> Generator[None, None, None]:
    """Keep cross-bot voice echo ordering state, and its loop bindings, per test."""
    _reset_visible_voice_echo_barriers()
    yield
    _reset_visible_voice_echo_barriers()


@pytest.fixture(autouse=True)
def bypass_authorization(request: pytest.FixtureRequest) -> Generator[None, None, None]:
    """Bypass authorization checks in tests by default.

    This allows test users like @user:example.com to interact with agents
    without needing to be in the authorized_users list.

    Tests in test_authorization.py are excluded since they test authorization itself.
    """
    # Don't bypass authorization for tests that are specifically testing it
    if "test_authorization" in request.node.parent.name:
        yield
    else:
        with (
            patch("mindroom.bot.is_authorized_sender", return_value=True),
            patch("mindroom.ingress_validation.is_authorized_sender", return_value=True),
            patch("mindroom.reaction_dispatch.is_authorized_sender", return_value=True),
        ):
            yield
