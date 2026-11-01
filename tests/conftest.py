"""Test configuration and fixtures for MindRoom tests."""

import asyncio
import gc
import ipaddress
import json
import os
import re
import shutil
import sqlite3
import subprocess
import sys
import tempfile
import threading
import time
import uuid
import warnings
from collections.abc import (
    AsyncGenerator,
    AsyncIterator,
    Awaitable,
    Callable,
    Collection,
    Generator,
    Iterable,
    Iterator,
    Mapping,
    MutableMapping,
    Sequence,
)
from concurrent.futures import ThreadPoolExecutor
from contextlib import ExitStack, contextmanager, suppress
from dataclasses import asdict, dataclass, field, replace
from functools import partial
from itertools import count
from pathlib import Path
from types import ModuleType, SimpleNamespace
from typing import TYPE_CHECKING, Any, NoReturn, cast
from unittest.mock import DEFAULT, AsyncMock, MagicMock, patch
from urllib.parse import unquote, urlsplit

import httpcore
import httpcore2
import httpx
import httpx2
import nio
import pytest
import pytest_asyncio
import structlog
import yaml
from agno.utils import log as agno_log
from aioresponses import aioresponses
from openai import AsyncOpenAI, OpenAI
from structlog.testing import ReturnLoggerFactory
from structlog.typing import BindableLogger, Context, Processor, WrappedLogger

import mindroom.api.sandbox_exec as sandbox_exec_module
import mindroom.approval.continuation_runner as continuation_runner_module
import mindroom.approval.manager as approval_manager_module
import mindroom.bot  # noqa: F401
import mindroom.cli.config as cli_config_module
import mindroom.cli.desktop as cli_desktop_module
import mindroom.cli.service as cli_service_module
import mindroom.custom_tools.todo as todo_tool_module
import mindroom.knowledge.refresh_scheduler as knowledge_refresh_scheduler_module
import mindroom.managed_avatars as managed_avatars_module
import mindroom.matrix.client_room_admin as client_room_admin_module
import mindroom.matrix.rooms as matrix_rooms_module
import mindroom.memory._entrypoint_snapshots as memory_entrypoint_snapshots_module
import mindroom.reply_turn.locked_turn as locked_turn_module
import mindroom.reply_turn.request as reply_request_module
import mindroom.response_runner as response_runner_module
import mindroom.streaming as streaming_module
import mindroom.tool_jobs.instances as tool_jobs_instances
import mindroom.tool_system.sandbox_proxy as sandbox_proxy_module
from mindroom import ai as mindroom_ai
from mindroom.agent_cli.turn import LiveTurnTools
from mindroom.agent_reply_membership import AgentReplyMembershipIndex
from mindroom.approval.manager import (
    ApprovalActionResult,
    ApprovalManager,
    ToolApprovalTransportError,
    initialize_approval_store,
)
from mindroom.authorization import _ReplyAuthorizationDecision
from mindroom.bot import AgentBot
from mindroom.coalescing.batch import PendingEvent
from mindroom.coalescing.gate import CoalescingDrainResult
from mindroom.command_turn_executor import CommandTurnExecutor
from mindroom.config.load_attempt import ConfigLoad
from mindroom.config.main import Config, load_config
from mindroom.config.plugin import PluginEntryConfig
from mindroom.config.resolution import entity_model_name
from mindroom.constants import RuntimePaths, resolve_runtime_paths, safe_replace
from mindroom.delivery_gateway import DeliveryGateway, EditTextRequest, FinalDeliveryRequest, SendTextRequest
from mindroom.dispatch_handoff import (
    MediaDispatchEvent,
    PreparedIngress,
    prepare_media_ingress,
)
from mindroom.dispatch_source import MESSAGE_SOURCE_KIND, ScheduledHistoryBudget
from mindroom.edit_regenerator import EditRegenerator
from mindroom.embeddings.openai_embedder import OpenAIEmbedder
from mindroom.event_journal import (
    AdmissionResult,
    ConversationPage,
    DeliveryAcknowledgement,
    DeliveryStage,
    EventClass,
    EventKind,
    FinalAnswerEnqueue,
    InboundEvent,
    MatrixDelivery,
    MatrixDeliveryView,
    PermanentDeliveryFailure,
    PrincipalStore,
    ProjectedEvent,
    RelationView,
    VisibleMessage,
    turn_rows,
)
from mindroom.event_journal import reads as journal_reads
from mindroom.event_journal.outbox import matrix_delivery_payload
from mindroom.execution_preparation import _build_unseen_context_messages
from mindroom.final_delivery import Completed, Failed, FailureCause, FinalDeliveryOutcome, TurnEnding
from mindroom.history.prompt_tokens import build_static_token_estimator
from mindroom.history.runtime import (
    finalize_history_preparation,
    prepare_scope_history,
    resolve_entity_preparation_inputs,
)
from mindroom.history.session_context import (
    ScopeSessionContext,
    load_scope_session,
    resolve_bound_team_scope,
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
from mindroom.interactive_models import InteractivePrompt, interactive_prompt_content
from mindroom.matrix.client_delivery import DeliveredMatrixEvent, build_edit_event_content
from mindroom.matrix.client_room_admin import RoomJoinOutcome
from mindroom.matrix.client_session import MindRoomAsyncClient, matrix_client_config, olm_store_dir
from mindroom.matrix.client_visible_messages import ResolvedVisibleMessage
from mindroom.matrix.conversation_reads import ConversationReader
from mindroom.matrix.identity import MatrixID
from mindroom.matrix.large_messages import _oversized_nonterminal_streaming_edit_next_allowed_at
from mindroom.matrix.media import is_matrix_media_dispatch_event
from mindroom.matrix.relation_lookup import RelationLookup
from mindroom.matrix_rtc.call_manager import CallManager
from mindroom.mcp import toolkit as mcp_toolkit_module
from mindroom.message_target import MessageTarget
from mindroom.model_defaults import OLLAMA_HOST_DEFAULT
from mindroom.orchestration.config_lifecycle import ConfigReloadLifecycle, load_published_config, load_startup_config
from mindroom.personal_room_lifecycle import PersonalRoomLifecycle
from mindroom.providers import registry as provider_registry
from mindroom.providers.pipeline import media as media_pipeline
from mindroom.reaction_dispatch import ReactionDispatcher
from mindroom.reply_turn.request import ResponseRequest
from mindroom.response_admission import ResponseAdmissionGate
from mindroom.response_lifecycle import QueuedHumanNoticeReservation
from mindroom.response_payload_preparation import (
    DispatchPayloadInputs,
    ResponsePayloadPreparation,
    ResponsePayloadPreparer,
)
from mindroom.response_runner import ResponseRunner
from mindroom.response_sources import Owned, ResponseSources, SourceFate, Unclaimed
from mindroom.response_turn import CompletedAttempt, ResponseTurnContext
from mindroom.session_storage.agent_storage import create_state_storage
from mindroom.session_storage.legacy_usage_totals import add_pre_ledger_baselines
from mindroom.session_storage.schema import quote_identifier
from mindroom.session_storage.store import (
    ConversationStore,
    _session_state,
    _write_session_state,
    delete_runs_in_transaction,
    ensure_session_row,
    persist_to,
    runs_without,
)
from mindroom.thread_summary import schedule as thread_summary_schedule
from mindroom.thread_summary.schedule import _ThreadSummaryBaselines
from mindroom.thread_utils import decide_agent_response
from mindroom.token_counting.families import load_o200k
from mindroom.tool_system import plugin_imports
from mindroom.tool_system import plugins as plugins_module
from mindroom.tool_system import registry as tool_registry_module
from mindroom.tool_system.declarations import ToolMetadata
from mindroom.turn_origin import TurnOrigin, classify_turn_origin
from mindroom.turn_policy import PreparedDispatch, TurnPolicy
from mindroom.turn_store import _CLAIM_REGISTRIES, TurnClaim, TurnStore, _ClaimRegistry
from mindroom.turns.dispatch import TurnDispatcher
from mindroom.turns.facts import IngressFacts
from mindroom.turns.ingress import TurnIngress
from mindroom.turns.interactive_selection import InteractiveSelections
from mindroom.user_stop_reconciliation import UserStopReconciler
from mindroom.visible_response_reconciliation import VisibleResponseReconciler
from mindroom.voice.ingress import VoiceIngress
from mindroom.voice.visible_voice_echo import VisibleVoiceEchoLifecycle, _reset_visible_voice_echo_barriers
from mindroom.workers import runtime as workers_runtime_module
from mindroom.workers.backends import static_runner as static_runner_module
from mindroom.workers.compatibility import worker_health_payload
from tests.fakes.scripted_model import estimate_only_model
from tests.identity_helpers import persist_entity_accounts
from tests.test_helpers import make_test_ca_pem

if TYPE_CHECKING:
    from _typeshed import DataclassInstance
    from xdist.workermanage import WorkerController

    from mindroom.agent_cli.session import CliTurnOwner
    from mindroom.approval.recovery import ApprovalRecovery
    from mindroom.dispatch_handoff import DispatchEvent
    from mindroom.event_journal import ApprovalDeliveryView, EventJournalStore
    from mindroom.event_journal.backend import Backend, Operation
    from mindroom.history.request_messages import TurnMessages
    from mindroom.llm.provider import ModelProvider, ModelRequest
    from mindroom.llm.runs import AnyRunRecord
    from mindroom.orchestrator import _MultiAgentOrchestrator
    from mindroom.session_storage.types import ConversationSession
    from mindroom.streaming import StreamingResponse
    from mindroom.tool_system.agent_tool_calls import PreparedAgentToolCatalog
    from mindroom.tool_system.tool_access import ToolKey
    from mindroom.tool_system.worker_routing import ToolExecutionIdentity
    from tests.team_turn_helpers import ProbeToolkit


_STRUCTLOG_CONFIGURE = structlog.configure
_REAL_BUILD_MODEL_PROVIDER = provider_registry.build_model_provider
_POSTGRES_STARTUP_TIMEOUT_SECONDS = 30

# The event journal orders text in SQL, in SQLite, and in Python, and those
# three must agree. An Alpine image cannot show a disagreement: every musl
# locale sorts like C, so a missing `COLLATE "C"` looks correct there and
# breaks against a glibc server. The journal's server is therefore glibc.
_POSTGRES_JOURNAL_RUN_ID_STASH_KEY = pytest.StashKey[str]()
_POSTGRES_JOURNAL_CONTAINER_PREFIX = "mindroom-postgres-journal-test-"
_POSTGRES_JOURNAL_IMAGE = "mirror.gcr.io/library/postgres:16"
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
# each worker retains test schemas for the full suite, including their indexes.
# Docker's default of half of host RAM is left behind on purpose -- an uncapped
# runaway on a shared machine is the same class of failure as the disk leak.
_POSTGRES_JOURNAL_TMPFS_SIZE = "4g"

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


async def activate_interactive_prompt(
    store: PrincipalStore,
    *,
    question_event_id: str,
    room_id: str,
    sender: str,
    creator_agent: str,
    thread_id: str | None = None,
    revision_event_id: str | None = None,
    question_text: str = "Choose",
    options: Mapping[str, str] | None = None,
    option_labels: Mapping[str, str] | None = None,
    source_event_id: str | None = None,
) -> AdmissionResult:
    """Admit and settle one self-authored Matrix revision carrying a prompt."""
    prompt_source_event_id = source_event_id or f"{question_event_id}-source"
    await store.admit(
        InboundEvent(
            event_id=prompt_source_event_id,
            room_id=room_id,
            thread_id=thread_id,
            kind=EventKind.MESSAGE,
            event_class=EventClass.CONTEXT_ONLY,
            sender="@user:localhost",
            origin_server_ts=500,
            source={},
        ),
    )
    prompt = InteractivePrompt(
        creator_agent=creator_agent,
        question_text=question_text,
        options=dict(options or {"1": "one"}),
        option_labels=dict(option_labels or {"1": "One"}),
        source_event_id=prompt_source_event_id,
    )
    installed_content: dict[str, object] = {
        "msgtype": "m.text",
        "body": question_text,
        **interactive_prompt_content(prompt),
    }
    event_id = revision_event_id or question_event_id
    content = installed_content
    if revision_event_id is not None:
        content = {
            "msgtype": "m.text",
            "body": f"* {question_text}",
            "m.new_content": installed_content,
            "m.relates_to": {"rel_type": "m.replace", "event_id": question_event_id},
            **interactive_prompt_content(prompt),
        }
    inbound = InboundEvent(
        event_id=event_id,
        room_id=room_id,
        thread_id=thread_id,
        kind=EventKind.MESSAGE,
        event_class=EventClass.ACTIONABLE,
        sender=sender,
        origin_server_ts=1_000,
        source={"event_id": event_id, "content": content},
    )
    result = await store.admit(
        inbound,
        ProjectedEvent(
            event_id=event_id,
            room_id=room_id,
            thread_id=thread_id,
            sender=sender,
            origin_server_ts=1_000,
            content=content,
            replaces_event_id=question_event_id if revision_event_id is not None else None,
            redacts_event_id=None,
        ),
    )
    await store.settle(event_id)
    return result


async def membership_epoch_is_active(store: PrincipalStore, room_id: str, epoch: int) -> bool:
    """Probe the journal's row-locked active-membership predicate."""
    # This is deliberately white-box: production callers use higher-level store guards.
    return await store._backend.write(
        lambda transaction: journal_reads.claim_membership_epoch(
            transaction,
            store._principal_id,
            room_id=room_id,
            expected_membership_epoch=epoch,
        ),
    )


_configure_quiet_structlog()


__all__ = [
    "TEST_ACCESS_TOKEN",
    "TEST_PASSWORD",
    "FakeApprovalTransport",
    "FakeCredentialsManager",
    "FakeMediaResponse",
    "activate_interactive_prompt",
    "agent_response_should_respond",
    "aioresponse",
    "approval_manager_for_test",
    "bind_runtime_paths",
    "build_private_template_dir",
    "bypass_authorization",
    "create_mock_room",
    "delivered_matrix_event",
    "delivered_matrix_side_effect",
    "drain_coalescing",
    "enforce_turn_authorization",
    "handle_card_response_for_test",
    "initialize_approval_store_for_test",
    "install_edit_message_mock",
    "install_edit_regeneration_mock",
    "install_generate_response_mock",
    "install_personal_room_shutdown_mock",
    "install_runtime_journal_support",
    "install_send_response_mock",
    "install_shutdown_drain_mocks",
    "load_config_yaml",
    "make_matrix_client_mock",
    "make_visible_message",
    "membership_epoch_is_active",
    "message_origin",
    "normalize_console_output",
    "orchestrator_runtime_paths",
    "patch_response_runner_module",
    "prepare_history_for_run_for_test",
    "prepare_payload_via_seam",
    "replace_delivery_gateway_deps",
    "replace_edit_regenerator_deps",
    "replace_response_runner_deps",
    "replace_turn_deps",
    "replace_turn_policy_deps",
    "replace_turn_store_deps",
    "request_envelope",
    "requires_linux",
    "runtime_paths_for",
    "serve_media_download",
    "serve_media_from_download",
    "start_call_manager",
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
RuntimeBot = AgentBot
TestFunction = Callable[..., object]


async def prepare_agent_and_prompt_for_test(
    ctx: ResponseTurnContext,
    *,
    prompt: str,
    runtime_paths: RuntimePaths,
    config: Config,
    scope_context: ScopeSessionContext | None = None,
    current_event_id: str | None = None,
    **turn_inputs: Any,  # noqa: ANN401
) -> Any:  # noqa: ANN401
    """Prepare one agent turn through ``ai._prepare_agent_and_prompt`` with a plain top-level turn's inputs.

    ``turn_inputs`` names the ``ResponseTurnContext`` fields a test sets beyond ``prompt``; every other one keeps
    ``ctx``'s value.
    """
    return await mindroom_ai._prepare_agent_and_prompt(
        replace(ctx, prompt=prompt, **turn_inputs),
        runtime_paths=runtime_paths,
        config=config,
        scope_context=scope_context,
        current_event_id=current_event_id,
    )


def build_unseen_context_messages_for_test(
    prompt: str,
    thread_history: Sequence[ResolvedVisibleMessage],
    **options: Any,  # noqa: ANN401
) -> "tuple[TurnMessages, list[str]]":
    """Build one turn's unseen thread context through ``_build_unseen_context_messages`` with a plain reply's options.

    ``options`` names the options a test sets; the others take the values a thread reply without a sender, display
    names, timestamp, transient context, or attachments passes, in UTC.
    """
    turn_options: dict[str, Any] = {
        "transient_context_messages": (),
        "current_sender_id": None,
        "member_display_names": {},
        "current_timestamp_ms": None,
        "prompt_event_id": None,
        "current_prompt_is_structured": False,
        "timezone": "UTC",
        "attachment_context": None,
    }
    return _build_unseen_context_messages(prompt, thread_history, **(turn_options | options))


def call_proxy_sync_for_test(**call: Any) -> object:  # noqa: ANN401
    """Send one tool call through ``sandbox_proxy._call_proxy_sync`` with the options a test names.

    The others take a plain call's values: an entrypoint that leaves the call's agent alone, no overrides,
    execution env, extra passthrough, or resolved worker target, and the built-in tools.
    """
    plain: dict[str, Any] = {
        "function_entrypoint": lambda *_args, **_kwargs: None,
        "tool_config_overrides": None,
        "tool_init_overrides": None,
        "execution_env": None,
        "extra_env_passthrough": None,
        "worker_target": None,
        "runtime_config": None,
        "tools": tool_registry_module.builtin_tool_registry(),
    }
    return sandbox_proxy_module._call_proxy_sync(**(plain | call))


@contextmanager
def bound_worker_launch_config(config: Config) -> Iterator[None]:
    """Bind ``config`` as the applied config dedicated workers launch from, as `mindroom run` does."""
    with workers_runtime_module.bind_worker_launch_config_source(lambda: config):
        yield


def live_turn_tools_for_test(
    owner: "CliTurnOwner",
    *,
    catalog: "PreparedAgentToolCatalog",
    authorize: "Callable[[ToolKey, dict[str, object]], Awaitable[None]]",
    **options: Any,  # noqa: ANN401
) -> LiveTurnTools:
    """Build one CLI turn's live tools with the options a test names, which include its output-file policy.

    The others take a top-level turn's values: no context documents or knowledge refresh.
    """
    turn_options: dict[str, Any] = {
        "context": {},
        "delegation_depth": 0,
        "refresh_scheduler": None,
    }
    return LiveTurnTools(owner, catalog=catalog, authorize=authorize, **(turn_options | options))


async def _no_approval_event(_room_id: str, _thread_id: str | None, _content: dict[str, Any]) -> dict[str, Any] | None:
    return None


async def _no_approval_delivery(_delivery: MatrixDelivery) -> str:
    msg = "This test sends no approval delivery"
    raise ToolApprovalTransportError(msg)


async def _no_delivered_event(*_target: object) -> str | None:
    return None


async def _no_sources_to_wake(_entity_name: str, _room_id: str, _source_event_ids: tuple[str, ...]) -> None:
    return None


@dataclass
class FakeApprovalTransport:
    """The approval transport an approval manager uses, with the methods a test sets.

    The others prepare and send nothing, find no earlier delivery, know no sender, device, or responder principal,
    wake nothing, and find the router in every room.
    """

    prepare_approval_event: Callable[[str, str | None, dict[str, Any]], Awaitable[dict[str, Any] | None]] = (
        _no_approval_event
    )
    send_approval_delivery: Callable[[MatrixDelivery], Awaitable[str]] = _no_approval_delivery
    resolve_approval_delivery: Callable[[MatrixDelivery], Awaitable[str | None]] = _no_delivered_event
    resolve_approval_action_delivery: Callable[[str, str], Awaitable[str | None]] = _no_delivered_event
    transport_sender_id: Callable[[], str | None] = lambda: None
    transport_device_id: Callable[[], str | None] = lambda: None
    wake_continuation_sources: Callable[[str, str, tuple[str, ...]], Awaitable[None]] = _no_sources_to_wake
    responder_principal_id: Callable[[str], str | None] = lambda _entity_name: None
    router_missing: Callable[[str], bool] = lambda _room_id: False


def approval_manager_for_test(
    runtime_paths: RuntimePaths,
    *,
    cards: "ApprovalDeliveryView | None" = None,
    **transport: Any,  # noqa: ANN401
) -> ApprovalManager:
    """Build an approval manager over the transport methods a test names, as ``FakeApprovalTransport`` fields."""
    return ApprovalManager(runtime_paths, transport=FakeApprovalTransport(**transport), cards=cards)


def initialize_approval_store_for_test(
    runtime_paths: RuntimePaths,
    *,
    cards: "ApprovalDeliveryView | None" = None,
    recovery: "ApprovalRecovery | None" = None,
    **transport: Any,  # noqa: ANN401
) -> ApprovalManager:
    """Initialize the approval store over the transport methods a test names, as ``FakeApprovalTransport`` fields."""
    return initialize_approval_store(
        runtime_paths,
        transport=FakeApprovalTransport(**transport),
        cards=cards,
        recovery=recovery,
    )


async def handle_card_response_for_test(manager: ApprovalManager, **response: Any) -> ApprovalActionResult:  # noqa: ANN401
    """Answer one approval card through ``manager.handle_card_response`` as a plain card response does.

    ``response`` names the card, its responder, the decision, and whatever else a test sets; the reason, consume
    callback, timed approval, and binding and schedule scopes otherwise stay unset.
    """
    unset = dict.fromkeys(("reason", "before_consume", "auto_approve_seconds", "current_binding", "scheduled_scope"))
    return await manager.handle_card_response(**(unset | response))


async def prepare_history_for_run_for_test(
    *,
    agent_name: str,
    full_prompt: str,
    session_id: str | None,
    runtime_paths: RuntimePaths,
    config: Config,
    execution_identity: "ToolExecutionIdentity | None",
    model: "ModelProvider | None" = None,
    storage: "ConversationStore | None" = None,
    session: "ConversationSession | None" = None,
    history_settings: ResolvedHistorySettings | None = None,
    active_model_name: str | None = None,
    active_context_window: int | None = None,
    static_prompt_tokens: int | None = None,
    available_history_budget: int | None = None,
    scope: HistoryScope | None = None,
    execution_plan: ResolvedHistoryExecutionPlan | None = None,
    compaction_lifecycle: CompactionLifecycle | None = None,
) -> PreparedHistoryState:
    """Compose the production history-preparation seams for one test run of an agent without instructions or tools.

    ``model`` is the provider the run's requests go to; without one, history is sized by estimate alone.
    """
    resolved_scope = scope or HistoryScope(kind="agent", scope_id=agent_name)
    model = model or estimate_only_model()
    static_token_estimator = build_static_token_estimator(model.token_counter, (), (), frozenset())
    if static_prompt_tokens is None:
        static_prompt_tokens = static_token_estimator.estimate(full_prompt)
    resolved_inputs = resolve_entity_preparation_inputs(
        config=config,
        runtime_paths=runtime_paths,
        history_settings=history_settings or config.resolve_scope(agent_name).history_settings,
        active_model_name=active_model_name or entity_model_name(config, agent_name),
        active_context_window=active_context_window,
        static_prompt_tokens=static_prompt_tokens,
    )
    if execution_plan is not None:
        resolved_inputs = replace(resolved_inputs, execution_plan=execution_plan)
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
        "model": model,
        "resolved_inputs": resolved_inputs,
        "non_prompt_tokens": static_token_estimator.non_prompt_tokens,
        "runtime_paths": runtime_paths,
        "config": config,
        "compaction_lifecycle": compaction_lifecycle,
        "pipeline_timing": None,
        "allow_native_compaction": True,
    }
    if storage is not None and session_id is not None:
        persisted_session = session
        if persisted_session is None:
            persisted_session = storage.load_session(session_id)
        scope_context = ScopeSessionContext(
            scope=resolved_scope,
            storage=storage,
            session=persisted_session,
            session_id=session_id,
            storage_factory=lambda: storage,
        )
        prepared_scope_history = await prepare_scope_history(scope_context=scope_context, **scope_history_kwargs)
    else:
        scope_context = (
            load_scope_session(
                session_id=session_id,
                runtime_paths=runtime_paths,
                config=config,
                execution_identity=execution_identity,
                scope=resolved_scope,
            )
            if session_id is not None
            else None
        )
        prepared_scope_history = await prepare_scope_history(scope_context=scope_context, **scope_history_kwargs)
    return finalize_history_preparation(
        prepared_scope_history=prepared_scope_history,
        config=config,
        static_prompt_tokens=static_prompt_tokens,
        pipeline_timing=None,
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
        AgentReplyMembershipIndex(),
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


def live_turn_claim(*source_event_ids: str) -> TurnClaim:
    """Return a live claim on these sources, alone in its own registry, for gate and lane cleanup tests."""
    return _ClaimRegistry().register(frozenset(source_event_ids), frozenset())


def turn_claim_is_live(claim: TurnClaim) -> bool:
    """Return whether a claim from ``live_turn_claim`` is still held."""
    return any(claim._registry.owns(event_id) for event_id in claim.source_event_ids)


def make_ingress_facts(
    sender: str,
    *,
    source_kind: str = MESSAGE_SOURCE_KIND,
    requester_user_id: str | None = None,
    acting_requester_id: str | None = None,
    original_sender: str | None = None,
    original_sender_is_human: bool = False,
    hook_source: str | None = None,
    message_received_depth: int = 0,
    discovery_event_id: str | None = None,
    sender_entity_name: str | None = None,
    requester_entity_name: str | None = None,
) -> IngressFacts:
    """Build the provenance ingress classifies for one event; the lane requester defaults to its sender."""
    return IngressFacts(
        physical_sender=sender,
        source_kind=source_kind,
        lane_requester_id=requester_user_id or sender,
        original_sender=original_sender,
        original_sender_is_human=original_sender_is_human,
        acting_requester_id=acting_requester_id,
        hook_source=hook_source,
        message_received_depth=message_received_depth,
        discovery_event_id=discovery_event_id,
        sender_entity_name=sender_entity_name,
        requester_entity_name=requester_entity_name,
    )


def make_pending_event(
    event: PreparedIngress | nio.RoomMessageFormatted | MediaDispatchEvent,
    room: nio.MatrixRoom,
    *,
    source_kind: str,
    active_follow_up: bool = False,
    hook_source: str | None = None,
    message_received_depth: int = 0,
    payload_trusted: bool = False,
    requester_user_id: str | None = None,
    discovery_event_id: str | None = None,
    turn_dispatch_recovery: bool = False,
    enqueue_time: float | None = None,
    notice: QueuedHumanNoticeReservation | None = None,
    facts: IngressFacts | None = None,
) -> PendingEvent:
    """Build one PendingEvent with its admission facts beside its PreparedIngress.

    Raw nio text events are normalized into PreparedIngress form and raw nio
    media events are wrapped with their caption and retained protocol reference.
    """
    if isinstance(event, PreparedIngress):
        prepared = event
    elif is_matrix_media_dispatch_event(event):
        prepared = prepare_media_ingress(event)
    elif isinstance(event, nio.RoomMessageFormatted):
        prepared = PreparedIngress(
            sender=event.sender,
            event_id=event.event_id,
            body=event.body,
            source=event.source,
            server_timestamp=event.server_timestamp,
        )
    else:
        msg = f"Unsupported pending event type: {type(event).__name__}"
        raise TypeError(msg)
    prepared = replace(
        prepared,
        active_follow_up=active_follow_up or prepared.active_follow_up,
        payload_trusted=payload_trusted or prepared.payload_trusted,
        turn_dispatch_recovery=turn_dispatch_recovery or prepared.turn_dispatch_recovery,
    )
    facts = facts or make_ingress_facts(
        prepared.sender,
        source_kind=source_kind,
        requester_user_id=requester_user_id,
        hook_source=hook_source,
        message_received_depth=message_received_depth,
        discovery_event_id=discovery_event_id,
    )
    if enqueue_time is None:
        return PendingEvent(event=prepared, room=room, facts=facts, notice=notice)
    return PendingEvent(event=prepared, room=room, facts=facts, enqueue_time=enqueue_time, notice=notice)


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
                runner.inbox.drain_inbox_responses(),
            )
            if not runner.inbox._inbox_response_tasks and not bot._coalescing_gate._gates:
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


# The controller creates the pipe and every worker mounts it, so they must agree
# on its directory. Captured at import, before pytest-shm points each process's
# temp root into that process's own pytest base directory at session start.
_OWNER_PIPE_ROOT = Path(tempfile.gettempdir())


def _owner_pipe_dir(run_id: str) -> Path:
    """Return the host directory holding one run's owner pipe."""
    return _OWNER_PIPE_ROOT / f"mindroom-pytest-owner-{run_id}"


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
) -> AsyncGenerator[Callable[[], "EventJournalStore"]]:
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


async def _empty_async_iterator() -> AsyncGenerator[object]:
    """Yield nothing while preserving async-iterator semantics for nio relations APIs."""
    if False:
        yield None


def _make_room_get_event_response(
    event_id: str,
    *,
    sender: str = "@user:localhost",
) -> nio.RoomGetEventResponse:
    """Return a minimal RoomGetEventResponse containing one visible text event."""
    event = MagicMock(spec=nio.RoomMessageText)
    event.event_id = event_id
    event.sender = sender
    event.body = event_id
    event.server_timestamp = 0
    event.transaction_id = None
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
    ending: TurnEnding,
    event_id: str | None = None,
    is_visible_response: bool = False,
    final_visible_body: str | None = None,
    delivery_kind: str | None = None,
    tool_trace: tuple[object, ...] = (),
    extra_content: Mapping[str, object] | None = None,
    option_map: dict[str, str] | None = None,
    options_list: tuple[dict[str, str], ...] | None = None,
) -> FinalDeliveryOutcome:
    """Build one compact terminal outcome for tests."""
    return FinalDeliveryOutcome(
        ending=ending,
        event_id=event_id,
        is_visible_response=is_visible_response,
        final_visible_body=final_visible_body,
        delivery_kind=delivery_kind,
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


@dataclass
class FakeMediaResponse:
    """The part of an aiohttp response that a streamed Matrix media download reads."""

    body: bytes
    status: int = 200
    headers: dict[str, str] = field(default_factory=dict)
    streamed_bytes: int = 0
    released: bool = False

    @property
    def content(self) -> "FakeMediaResponse":
        """Stand in for the response's payload stream."""
        return self

    async def iter_chunked(self, size: int) -> AsyncIterator[bytes]:
        """Yield the body in chunks, counting what the reader actually took."""
        for start in range(0, len(self.body), size):
            chunk = self.body[start : start + size]
            self.streamed_bytes += len(chunk)
            yield chunk

    def release(self) -> None:
        """Record that the reader gave the connection back."""
        self.released = True


async def serve_media_download(download: Callable[..., Awaitable[object]], path: str) -> FakeMediaResponse:
    """Answer one streamed media download request from a test's ``download(mxc=...)`` fake."""
    server_name, media_id = urlsplit(path).path.split("/")[-2:]
    response = await download(mxc=f"mxc://{unquote(server_name)}/{unquote(media_id)}")
    if isinstance(response, nio.DownloadResponse):
        return FakeMediaResponse(response.body)
    return FakeMediaResponse(b'{"errcode": "M_NOT_FOUND"}', status=404)


def serve_media_from_download(client: AsyncMock) -> None:
    """Answer streamed media requests on ``client.send`` from ``client.download`` as it is when each is sent."""

    async def send(_method: str, path: str, *_args: object, **_kwargs: object) -> object:
        if "/media/download/" not in path:
            return DEFAULT
        return await serve_media_download(client.download, path)

    client.access_token = TEST_ACCESS_TOKEN
    client.send = AsyncMock(side_effect=send)


async def push_stream_chunk(streaming: "StreamingResponse", chunk: str, client: object) -> None:
    """Apply one visible chunk to a stream and let its throttle decide whether to deliver it."""
    prior_delta_at = streaming.last_delta_at
    streaming._update(chunk)
    await streaming._throttled_send(client, prior_delta_at=prior_delta_at)


def legacy_default_store_client(
    homeserver: str,
    user_id: str,
    device_id: str,
    access_token: str,
    runtime_paths: RuntimePaths,
) -> MindRoomAsyncClient:
    """Open a client on the ordinary nio DefaultStore that MindRoom wrote before owned sessions existed."""
    store_dir = olm_store_dir(user_id, runtime_paths)
    store_dir.mkdir(parents=True, exist_ok=True)
    client = MindRoomAsyncClient(homeserver, user_id, store_path=str(store_dir), config=matrix_client_config())
    client.restore_login(user_id, device_id, access_token)
    return client


def make_matrix_client_mock(*, user_id: str = "@mindroom_test:example.com") -> AsyncMock:
    """Return an AsyncClient-shaped mock with safe defaults for sync nio APIs."""
    client = AsyncMock(spec=nio.AsyncClient)
    client.user_id = user_id
    client.homeserver = "https://localhost"
    # A logged-in client always has one, and delivery records it on every claim
    # so a resend can tell whether its frozen transaction ID still deduplicates.
    client.device_id = "TESTDEVICE"
    client.olm = None
    client.rooms = _AutoRoomCache(user_id)
    client.invited_rooms = {}
    client.next_batch = "s_test_token"
    client.loaded_sync_token = ""
    presence_response = MagicMock()
    presence_response.presence = "offline"
    presence_response.last_active_ago = 3_600_000
    room_messages_response = nio.RoomMessagesResponse(room_id="!test:localhost", chunk=[], start="", end=None)
    client.add_event_callback = MagicMock()
    client.add_response_callback = MagicMock()
    client.get_presence = AsyncMock(return_value=presence_response)
    client.room_get_event = AsyncMock(
        side_effect=lambda _room_id, event_id: _make_room_get_event_response(event_id, sender=user_id),
    )
    client.room_get_event_relations = MagicMock(return_value=_empty_async_iterator())
    client.room_messages = AsyncMock(return_value=room_messages_response)
    client.joined_rooms = AsyncMock(return_value=nio.JoinedRoomsResponse(rooms=[]))
    serve_media_from_download(client)

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


def summarized_lines(request: "ModelRequest") -> list[str]:
    """Return the thread lines one summary request asked the model to summarize."""
    prompt = request.messages[-1].get_content_string()
    start = prompt.index("<thread_messages>\n") + len("<thread_messages>\n")
    return prompt[start : prompt.index("\n</thread_messages>")].splitlines()


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
                created_ts=message.timestamp,
                revision_event_id=message.latest_event_id,
                revision_ts=message.edited_timestamp if message.edited_timestamp is not None else message.timestamp,
                content=dict(message.content),
            )
            for message in messages
        ),
        refresh_pending=(),
        next_cursor=None,
    )
    reader.read.return_value = page
    reader.read_strict.return_value = page


class _NoReplyRecords:
    """Reply records of an outbox that never writes a reply row."""

    async def with_pending_work(self) -> tuple[object, ...]:
        return ()

    async def for_event(self, _event_id: str) -> None:
        return None

    async def for_sources(self, _event_ids: tuple[str, ...]) -> None:
        return None

    async def load(self, _reply_id: str) -> None:
        return None


class FakeOutbox:
    """An in-memory outbox with the real claim-before-send semantics.

    A plain mock would let a test pass while the delivery never went through
    the outbox at all. This keeps the two rules that matter: a claimed row's
    payload is frozen, and an acknowledged row replays its recorded event ID
    instead of sending again.
    """

    def __init__(self) -> None:
        self.rows: dict[tuple[str, str], MatrixDelivery] = {}
        self.acknowledged_projections: list[tuple[ProjectedEvent, ...]] = []
        # Turns whose membership has ended, as the journal would report it.
        self.ended_membership_turn_ids: set[str] = set()
        # The journal sources each FINAL enqueue handed over, in order.
        self.handed_over: list[tuple[str, ...]] = []
        self.room_membership_epochs: dict[str, int] = {}

    @property
    def principal_id(self) -> str:
        """Return the principal this in-memory delivery store represents."""
        return "agent@alice"

    @property
    def replies(self) -> _NoReplyRecords:
        """Return the reply records this outbox never writes."""
        return _NoReplyRecords()

    async def membership_epoch(self, room_id: str) -> int:
        """Return the fake room's current membership epoch."""
        return self.room_membership_epochs.get(room_id, 0)

    async def turn_membership_is_current(self, *, turn_id: str, room_id: str) -> bool:
        """Return whether a turn still speaks for the room's current membership."""
        del room_id
        return turn_id not in self.ended_membership_turn_ids

    async def enqueue_matrix_delivery(
        self,
        *,
        delivery_id: str,
        stage: DeliveryStage,
        room_id: str,
        thread_id: str | None,
        payload: Mapping[str, object],
        result: Mapping[str, object] | None = None,
        event_type: str = "m.room.message",
        edits_event_id: str | None = None,
        permanent_failure_reason: str | None = None,
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

        """
        membership_epoch = await self.membership_epoch(room_id)
        existing_owner = next(
            (
                delivery
                for (existing_delivery_id, _stage), delivery in self.rows.items()
                if existing_delivery_id == delivery_id
            ),
            None,
        )
        if existing_owner is not None and (
            existing_owner.retired
            or existing_owner.room_id != room_id
            or existing_owner.membership_epoch != membership_epoch
        ):
            return None
        key = (delivery_id, stage.value)
        existing = self.rows.get(key)
        if existing is not None:
            if existing.attempted or existing.permanently_failed:
                return existing.transaction_id
            self.rows[key] = replace(
                existing,
                room_id=room_id,
                thread_id=thread_id,
                payload=matrix_delivery_payload(self.principal_id, delivery_id, stage, payload),
                result=dict(result) if result is not None else None,
                event_type=event_type,
                edits_event_id=edits_event_id,
                permanent_failure_reason=permanent_failure_reason,
            )
            return existing.transaction_id
        if delivery_id in self.ended_membership_turn_ids:
            return None
        transaction_id = f"tx-{delivery_id}-{stage.value}"
        self.rows[key] = MatrixDelivery(
            delivery_id=delivery_id,
            stage=stage,
            event_type=event_type,
            room_id=room_id,
            membership_epoch=membership_epoch,
            thread_id=thread_id,
            transaction_id=transaction_id,
            payload=matrix_delivery_payload(self.principal_id, delivery_id, stage, payload),
            result=dict(result) if result is not None else None,
            edits_event_id=edits_event_id,
            acknowledged_event_id=None,
            created_at_ns=len(self.rows),
            permanent_failure_reason=permanent_failure_reason,
        )
        return transaction_id

    async def claim_matrix_delivery(
        self,
        *,
        delivery_id: str,
        stage: DeliveryStage,
        sending_device_id: str | None = None,
    ) -> MatrixDelivery | None:
        """Freeze one delivery before any network call, returning its prior state.

        The prior attempted state comes back while the first device intent is
        committed atomically, exactly as the real outbox does.
        """
        key = (delivery_id, stage.value)
        row = self.rows.get(key)
        if row is None or row.retired or row.permanently_failed:
            return None
        if (
            stage is DeliveryStage.INITIAL
            and not row.attempted
            and (delivery_id, DeliveryStage.FINAL.value) in self.rows
        ):
            return None
        initial = self.rows.get((delivery_id, DeliveryStage.INITIAL.value))
        if (
            stage is DeliveryStage.FINAL
            and row.edits_event_id is None
            and initial is not None
            and initial.attempted
            and initial.acknowledged_event_id is None
        ):
            return None
        claimed = replace(
            row,
            attempted=True,
            sending_device_id=sending_device_id if not row.attempted else row.sending_device_id,
        )
        self.rows[key] = claimed
        return replace(claimed, attempted=row.attempted)

    async def record_matrix_delivery_device(
        self,
        *,
        delivery_id: str,
        stage: DeliveryStage,
        device_id: str | None,
    ) -> None:
        """Record the device namespace this delivery is about to send under."""
        key = (delivery_id, stage.value)
        if key in self.rows:
            self.rows[key] = replace(self.rows[key], sending_device_id=device_id)

    async def load_matrix_delivery(self, *, delivery_id: str, stage: DeliveryStage) -> MatrixDelivery | None:
        """Return one delivery without claiming it."""
        return self.rows.get((delivery_id, stage.value))

    async def record_permanent_matrix_delivery_failure(
        self,
        *,
        delivery_id: str,
        stage: DeliveryStage,
        reason: str,
    ) -> PermanentDeliveryFailure:
        """Stop retrying one definitively refused immutable payload, or report its ACK."""
        if not reason:
            msg = "A permanent Matrix delivery failure requires a reason"
            raise ValueError(msg)
        key = (delivery_id, stage.value)
        row = self.rows.get(key)
        if row is None or row.acknowledged_event_id is not None:
            return PermanentDeliveryFailure(acknowledged_event_id=None if row is None else row.acknowledged_event_id)
        if not row.retired and not row.permanently_failed:
            self.rows[key] = replace(row, permanent_failure_reason=reason)
        return PermanentDeliveryFailure()

    async def retire_matrix_delivery(
        self,
        *,
        delivery_id: str,
        stage: DeliveryStage,
        room_id: str,
        membership_epoch: int,
    ) -> str | None:
        """Retain one obsolete row as an in-memory identity tombstone."""
        key = (delivery_id, stage.value)
        row = self.rows.get(key)
        if row is None:
            return None
        assert (row.room_id, row.membership_epoch) == (room_id, membership_epoch)
        if row.acknowledged_event_id is not None:
            return row.acknowledged_event_id
        self.rows[key] = replace(row, retired=True)
        return None

    async def enqueue_final_answer(
        self,
        *,
        delivery_id: str,
        room_id: str,
        thread_id: str | None,
        payload: Mapping[str, object],
        turn_key: str | None,
        result: Mapping[str, object] | None = None,
        event_type: str = "m.room.message",
        edits_event_id: str | None = None,
        permanent_failure_reason: str | None = None,
    ) -> FinalAnswerEnqueue | None:
        """Record a FINAL answer and hand over its delivery's own source.

        There is no turn store here to bind, so only the delivery id is handed
        over and kept, because the handoff is a durable effect of this call in
        production and a double that swallowed it would let a lost handoff pass
        unnoticed; what a bound turn settles is pinned against the real backends.
        """
        del turn_key
        transaction_id = await self.enqueue_matrix_delivery(
            delivery_id=delivery_id,
            stage=DeliveryStage.FINAL,
            room_id=room_id,
            thread_id=thread_id,
            payload=payload,
            result=result,
            event_type=event_type,
            edits_event_id=edits_event_id,
            permanent_failure_reason=permanent_failure_reason,
        )
        if transaction_id is None:
            return None
        self.handed_over.append((delivery_id,))
        return FinalAnswerEnqueue(transaction_id=transaction_id, settled_event_ids=(delivery_id,))

    async def acknowledge_matrix_delivery(
        self,
        *,
        delivery_id: str,
        stage: DeliveryStage,
        event_id: str,
        delivered_projections: tuple[ProjectedEvent, ...],
        terminal_response_event_id: str | None = None,  # noqa: ARG002 - this fake keeps no turns
    ) -> DeliveryAcknowledgement:
        """Record the Matrix event one claimed delivery produced."""
        key = (delivery_id, stage.value)
        already = self.rows[key].acknowledged_event_id
        if already is not None:
            # First-writer-wins, like the real store: a loser is told the event
            # the row already names rather than its own, and told it bound
            # nothing -- which stays true even when the two events are equal.
            return DeliveryAcknowledgement(settled_event_id=already)
        self.rows[key] = replace(
            self.rows[key],
            acknowledged_event_id=event_id,
            permanent_failure_reason=None,
        )
        self.acknowledged_projections.append(delivered_projections)
        return DeliveryAcknowledgement(settled_event_id=event_id)

    async def unacknowledged_matrix_deliveries(
        self,
        *,
        event_type: str = "m.room.message",
        limit: int = 256,
        after: tuple[int, str, str] | None = None,
    ) -> tuple[MatrixDelivery, ...]:
        """Return deliveries whose Matrix outcome is unknown, oldest first.

        The cursor is honoured, because recovery relies on it to make
        progress: a row it visits but does not acknowledge -- a failure, or a
        placeholder its answer overtook -- must not come back on the next
        page, or the scan never ends.
        """
        pending = sorted(
            (
                row
                for row in self.rows.values()
                if (
                    row.event_type == event_type
                    and row.acknowledged_event_id is None
                    and not row.retired
                    and not row.permanently_failed
                )
            ),
            key=lambda row: (row.created_at_ns, row.delivery_id, row.stage.value),
        )
        if after is not None:
            pending = [row for row in pending if (row.created_at_ns, row.delivery_id, row.stage.value) > after]
        return tuple(pending[:limit])


def make_outbox_mock() -> MatrixDeliveryView:
    """Return an outbox a delivery test can actually send through."""
    return cast("MatrixDeliveryView", FakeOutbox())


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

    inner: MatrixDeliveryView

    @property
    def principal_id(self) -> str:
        """Return the wrapped delivery principal."""
        return self.inner.principal_id

    async def membership_epoch(self, room_id: str) -> int:
        """Return the wrapped room membership epoch."""
        return await self.inner.membership_epoch(room_id)

    async def enqueue_matrix_delivery(
        self,
        *,
        delivery_id: str,
        stage: DeliveryStage,
        room_id: str,
        thread_id: str | None,
        payload: Mapping[str, object],
        result: Mapping[str, object] | None = None,
        event_type: str = "m.room.message",
        edits_event_id: str | None = None,
        permanent_failure_reason: str | None = None,
    ) -> str | None:
        """Record delivery intent."""
        return await self.inner.enqueue_matrix_delivery(
            delivery_id=delivery_id,
            stage=stage,
            room_id=room_id,
            thread_id=thread_id,
            payload=payload,
            result=result,
            event_type=event_type,
            edits_event_id=edits_event_id,
            permanent_failure_reason=permanent_failure_reason,
        )

    async def enqueue_final_answer(
        self,
        *,
        delivery_id: str,
        room_id: str,
        thread_id: str | None,
        payload: Mapping[str, object],
        turn_key: str | None,
        result: Mapping[str, object] | None = None,
        event_type: str = "m.room.message",
        edits_event_id: str | None = None,
        permanent_failure_reason: str | None = None,
    ) -> FinalAnswerEnqueue | None:
        """Record a FINAL answer and its handoff."""
        return await self.inner.enqueue_final_answer(
            delivery_id=delivery_id,
            room_id=room_id,
            thread_id=thread_id,
            payload=payload,
            turn_key=turn_key,
            result=result,
            event_type=event_type,
            edits_event_id=edits_event_id,
            permanent_failure_reason=permanent_failure_reason,
        )

    async def turn_membership_is_current(self, *, turn_id: str, room_id: str) -> bool:
        """Return whether a turn still speaks for the room's current membership."""
        return await self.inner.turn_membership_is_current(turn_id=turn_id, room_id=room_id)

    async def claim_matrix_delivery(
        self,
        *,
        delivery_id: str,
        stage: DeliveryStage,
        sending_device_id: str | None = None,
    ) -> MatrixDelivery | None:
        """Freeze one delivery before network I/O and return what to send."""
        return await self.inner.claim_matrix_delivery(
            delivery_id=delivery_id,
            stage=stage,
            sending_device_id=sending_device_id,
        )

    async def record_matrix_delivery_device(
        self,
        *,
        delivery_id: str,
        stage: DeliveryStage,
        device_id: str | None,
    ) -> None:
        """Record the device namespace this delivery is about to send under."""
        await self.inner.record_matrix_delivery_device(
            delivery_id=delivery_id,
            stage=stage,
            device_id=device_id,
        )

    async def load_matrix_delivery(self, *, delivery_id: str, stage: DeliveryStage) -> MatrixDelivery | None:
        """Return one delivery without claiming it."""
        return await self.inner.load_matrix_delivery(delivery_id=delivery_id, stage=stage)

    async def record_permanent_matrix_delivery_failure(
        self,
        *,
        delivery_id: str,
        stage: DeliveryStage,
        reason: str,
    ) -> PermanentDeliveryFailure:
        """Stop retrying one definitively refused immutable payload, or report its ACK."""
        return await self.inner.record_permanent_matrix_delivery_failure(
            delivery_id=delivery_id,
            stage=stage,
            reason=reason,
        )

    async def retire_matrix_delivery(
        self,
        *,
        delivery_id: str,
        stage: DeliveryStage,
        room_id: str,
        membership_epoch: int,
    ) -> str | None:
        """Retain an obsolete delivery as an identity tombstone."""
        return await self.inner.retire_matrix_delivery(
            delivery_id=delivery_id,
            stage=stage,
            room_id=room_id,
            membership_epoch=membership_epoch,
        )

    async def acknowledge_matrix_delivery(
        self,
        *,
        delivery_id: str,
        stage: DeliveryStage,
        event_id: str,
        delivered_projections: tuple[ProjectedEvent, ...],
        terminal_response_event_id: str | None = None,
    ) -> DeliveryAcknowledgement:
        """Record the Matrix outcome, then die before anything else can run."""
        await self.inner.acknowledge_matrix_delivery(
            delivery_id=delivery_id,
            stage=stage,
            event_id=event_id,
            delivered_projections=delivered_projections,
            terminal_response_event_id=terminal_response_event_id,
        )
        msg = "crashed the instant the outcome was recorded"
        raise CrashError(msg)

    async def unacknowledged_matrix_deliveries(
        self,
        *,
        event_type: str = "m.room.message",
        limit: int = 256,
        after: tuple[int, str, str] | None = None,
    ) -> tuple[MatrixDelivery, ...]:
        """Return deliveries whose Matrix outcome is unknown, oldest first."""
        return await self.inner.unacknowledged_matrix_deliveries(
            event_type=event_type,
            limit=limit,
            after=after,
        )


def ignore_final_delivery_handoff(_event_ids: tuple[str, ...]) -> None:
    """Drop the outbox-enqueue journal handoff, for tests that are not about it.

    Named so a reader can see that the handoff was deliberately stubbed here,
    not forgotten. Tests that care what the handoff settles use a real journal
    store instead.
    """


async def ignore_delivered_projection(
    _delivery: MatrixDelivery,
    _event_id: str,
) -> tuple[ProjectedEvent, ...]:
    """Return no projection for outbox-only tests."""
    return ()


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
    failure: Exception | None = None,
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
        store=cast("RelationView", FakeRelationStore(threads=dict(threads or {}), failure=failure)),
        runtime=resolved_client,  # type: ignore[arg-type]
    )


def event_serving_client(
    event_id: str,
    *,
    content: dict[str, object],
    event_type: str = "m.room.message",
) -> MagicMock:
    """Return a homeserver that serves one event for every point lookup."""
    client = MagicMock()
    client.room_get_event = AsyncMock(
        return_value=nio.RoomGetEventResponse.from_dict(
            {
                "event_id": event_id,
                "sender": "@user:localhost",
                "origin_server_ts": 1,
                "room_id": "!room:localhost",
                "type": event_type,
                "content": content,
            },
        ),
    )
    return client


def thread_reply_client(reply_event_id: str, thread_root_id: str) -> MagicMock:
    """Return a homeserver that answers every event lookup with one reply inside a thread."""
    content = {
        "msgtype": "m.text",
        "body": "reply",
        "m.relates_to": {"rel_type": "m.thread", "event_id": thread_root_id},
    }
    return event_serving_client(reply_event_id, content=content)


def make_thread_reply_lookup(reply_event_id: str, thread_root_id: str) -> RelationLookup:
    """Return a relation lookup whose homeserver answers every event lookup with one reply inside a thread."""
    return make_relation_lookup(client=thread_reply_client(reply_event_id, thread_root_id))


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
            # A walk on the server again that read the whole conversation.
            hydrator=SimpleNamespace(rewalk=AsyncMock(return_value=True)),
        ),
    )


def make_membership_stub() -> PrincipalStore:
    """Return the narrow membership view used by runtime test collaborators."""
    return cast(
        "PrincipalStore",
        SimpleNamespace(
            membership_epoch=AsyncMock(return_value=0),
            interactive_prompt_is_current=AsyncMock(return_value=True),
        ),
    )


def install_runtime_journal_support(bot: RuntimeBot) -> RuntimeBot:
    """Install the durable-runtime stand-ins used by lightweight bot tests.

    These tests deliberately do not open an owned ingestion session. Route
    their membership work through the mocked Matrix transports while keeping
    production's durable gateway fail-closed when no session is attached.
    """

    async def change_membership(
        room_id: str,
        target_membership: str,
        *,
        is_authorized: Callable[[], bool] | None = None,
    ) -> bool:
        client = bot.client
        if client is None:
            msg = "Matrix client is not ready for test room membership work"
            raise RuntimeError(msg)
        if target_membership == "join":
            position = await bot.journal_principal().ingestion_membership_position(room_id)
            if is_authorized is not None and not is_authorized():
                return False
            rooms = client.rooms
            if (
                isinstance(rooms, Mapping)
                and any(joined_room_id == room_id for joined_room_id in rooms)
                and (position is None or position.membership == "join")
            ):
                return True
            outcome = await client_room_admin_module.join_room(client, room_id)
            joined = outcome is RoomJoinOutcome.JOINED or outcome is True
            if joined:
                client.rooms[room_id] = nio.MatrixRoom(room_id, client.user_id)
                if isinstance(client.invited_rooms, dict):
                    client.invited_rooms.pop(room_id, None)
            return joined
        if target_membership == "leave":
            return await matrix_rooms_module.leave_room(client, room_id)
        msg = f"Unsupported test room membership target: {target_membership}"
        raise ValueError(msg)

    bot._room_lifecycle.deps = replace(
        bot._room_lifecycle.deps,
        change_membership=change_membership,
    )
    bot.change_local_membership = change_membership  # type: ignore[method-assign]
    bot.personal_rooms.change_membership = change_membership
    sync_bot_runtime_state(bot)
    return bot


async def start_call_manager(bot: RuntimeBot) -> MagicMock:
    """Start ``bot``'s call wiring as startup does, with a spec'd call-manager fake built for its config; return it."""
    call_manager = MagicMock(spec=CallManager)
    with patch("mindroom.bot.maybe_build_call_manager", new=AsyncMock(return_value=call_manager)):
        await bot._register_call_manager_callbacks(MagicMock(spec=nio.AsyncClient))
    return call_manager


def normalize_console_output(text: str) -> str:
    """Collapse wrapped console output for stable substring assertions."""
    return " ".join(_SOFT_WRAP_RE.sub("", _ANSI_RE.sub("", text)).split())


def fresh_thread_summary_baselines(monkeypatch: pytest.MonkeyPatch) -> _ThreadSummaryBaselines:
    """Install fresh process-wide thread-summary baselines and locks, as a restart starts with."""
    fresh = _ThreadSummaryBaselines()
    monkeypatch.setattr(thread_summary_schedule, "THREAD_SUMMARY_BASELINES", fresh)
    return fresh


@pytest.fixture
def thread_summary_baselines(monkeypatch: pytest.MonkeyPatch) -> _ThreadSummaryBaselines:
    """Give the test its own process-wide thread-summary baselines and locks."""
    return fresh_thread_summary_baselines(monkeypatch)


@pytest.fixture
def wide_cli_consoles(monkeypatch: pytest.MonkeyPatch) -> None:
    """Render the CLI's consoles at a width no output line with a temporary path reaches.

    `normalize_console_output` rejoins a line wrapped at a space without that space, so a temporary path in the middle
    of a line breaks the assertion at some path lengths. The CLI builds its consoles at import, which fixes a `COLUMNS`
    exported when pytest starts, and with `FORCE_COLOR` set the root conftest's dumb terminal makes Rich use 80 columns.
    Rich takes a pinned width and height before either.
    """
    for console in (
        cli_config_module.console,
        cli_config_module._err_console,
        cli_desktop_module._console,
        cli_desktop_module._error_console,
        cli_service_module._console,
        cli_service_module._err_console,
    ):
        monkeypatch.setattr(console, "_width", 1000)
        monkeypatch.setattr(console, "_height", 25)


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

    def for_primary_runtime_agent_scope(self, agent_name: str) -> "FakeCredentialsManager":
        """Return an empty primary agent-scoped store."""
        return FakeCredentialsManager({}, storage_root=self.storage_root / "primary" / agent_name)

    def for_primary_runtime_scope(self, requester_id: str, agent_name: str | None) -> "FakeCredentialsManager":
        """Return an empty primary requester-scoped store."""
        return FakeCredentialsManager(
            {},
            storage_root=self.storage_root / "primary" / requester_id / (agent_name or ""),
        )


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


def install_bots(orchestrator: "_MultiAgentOrchestrator", bots: Mapping[str, object]) -> None:
    """Register test bots with a real orchestrator's entity fleet, which owns every managed bot."""
    for entity_name, bot in bots.items():
        orchestrator.fleet.add(entity_name, cast("AgentBot", bot))


def swap_fleet_bot(orchestrator: "_MultiAgentOrchestrator", entity_name: str, bot: object | None) -> None:
    """Swap or drop one fleet bot in place, as a finished replacement or removal leaves it, without its lifecycle."""
    slots = orchestrator.fleet._slots
    if bot is None:
        del slots[entity_name]
    else:
        slots[entity_name].bot = cast("AgentBot", bot)


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


def published_config_load(config: Config) -> ConfigLoad:
    """Return a successful load of ``config`` that no newer API commit superseded."""
    return ConfigLoad(
        config=config,
        source_fingerprint=config.source_fingerprint,
        source_files=config.source_files,
        uses_includes=config.uses_includes,
    )


async def initialize_from_config_file(runtime_paths: RuntimePaths) -> "_MultiAgentOrchestrator":
    """Load the config file the way `mindroom run` startup does, then build and initialize the runtime from that load."""
    from mindroom.orchestrator import _MultiAgentOrchestrator  # noqa: PLC0415

    config = await load_startup_config(runtime_paths, api_enabled=True)
    orchestrator = _MultiAgentOrchestrator(runtime_paths=runtime_paths, config=config)
    await orchestrator.initialize()
    return orchestrator


def make_config_reload_lifecycle(
    runtime_paths: RuntimePaths,
    *,
    current_config: Config | None = None,
    agent_bots: Mapping[str, AgentBot] | None = None,
    response_admission_gate: ResponseAdmissionGate | None = None,
    load_config: Callable[[], ConfigLoad] | None = None,
) -> ConfigReloadLifecycle:
    """Return a running reload lifecycle with stubbed runtime hooks; it loads the config file unless told otherwise."""
    runtime_config = current_config if current_config is not None else Config()
    return ConfigReloadLifecycle(
        runtime_paths=runtime_paths,
        is_running=lambda: True,
        current_config=lambda: runtime_config,
        agent_bots=lambda: agent_bots if agent_bots is not None else {},
        load_config=load_config or partial(load_published_config, runtime_paths, api_enabled=False),
        apply_update_plan=AsyncMock(return_value=True),
        response_admission_gate=response_admission_gate or ResponseAdmissionGate(),
        before_runtime_replacement=AsyncMock(),
        rebind_published_config_consumers=MagicMock(),
        finish_reload=AsyncMock(),
    )


def write_concurrent_edit_while_validating(monkeypatch: pytest.MonkeyPatch, config_path: Path, agent_name: str) -> str:
    """Rename ``agent_name`` on disk, as a dashboard save would, while a candidate carrying "Agent change" validates.

    Returns the concurrently written source.
    """
    concurrent = yaml.safe_load(config_path.read_text(encoding="utf-8"))
    concurrent["agents"][agent_name]["display_name"] = "Saved from the dashboard"
    concurrent_source = yaml.safe_dump(concurrent, sort_keys=True)
    validate = Config.validate_with_runtime

    def validate_after_concurrent_edit(data: object, runtime_paths: RuntimePaths, **kwargs: object) -> Config:
        if (
            "Agent change" in json.dumps(data, default=str)
            and config_path.read_text(encoding="utf-8") != concurrent_source
        ):
            config_path.write_text(concurrent_source, encoding="utf-8")
        return validate(data, runtime_paths, **kwargs)

    monkeypatch.setattr(Config, "validate_with_runtime", staticmethod(validate_after_concurrent_edit))
    return concurrent_source


def assert_refused_for_concurrent_edit(result: str, config_path: Path, concurrent_source: str) -> None:
    """Assert a change was refused because the file changed, keeping the concurrent edit on disk."""
    assert "configuration file changed" in result
    assert "NOT applied" in result
    assert "Invalid configuration" not in result
    assert config_path.read_text(encoding="utf-8") == concurrent_source


def write_demo_tool_plugin(plugin_root: Path, tool_name: str) -> None:
    """Write a plugin at ``plugin_root`` whose tools module registers one tool named ``tool_name``."""
    plugin_root.mkdir(parents=True)
    (plugin_root / "mindroom.plugin.json").write_text(
        json.dumps({"name": "demo_plugin", "tools_module": "tools.py", "skills": []}),
        encoding="utf-8",
    )
    (plugin_root / "tools.py").write_text(
        "from agno.tools import Toolkit\n"
        "from mindroom.tool_system.declarations import ToolCategory, ToolFileAccess\n"
        "from mindroom.tool_system.registration import register_tool_with_metadata\n"
        "\n"
        "class DemoTool(Toolkit):\n"
        "    def __init__(self) -> None:\n"
        "        super().__init__(name='demo', tools=[])\n"
        "\n"
        "@register_tool_with_metadata(\n"
        f"    name='{tool_name}',\n"
        "    file_access=ToolFileAccess.NONE,\n"
        "    display_name='Plugin Tool',\n"
        "    description='Plugin-defined tool',\n"
        "    category=ToolCategory.DEVELOPMENT,\n"
        ")\n"
        "def demo_plugin_tools():\n"
        "    return DemoTool\n",
        encoding="utf-8",
    )


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


def plant_workspace_entry(path: Path, kind: str, victim: Path | None = None) -> None:
    """Put what worker code could plant at ``path``: a ``link`` to ``victim`` or a ``fifo``."""
    if path.is_symlink() or path.is_file():
        path.unlink()
    path.parent.mkdir(parents=True, exist_ok=True)
    if kind == "fifo":
        os.mkfifo(path)
    else:
        assert victim is not None
        path.symlink_to(victim, target_is_directory=victim.is_dir())


def bind_runtime_paths(
    config: Config,
    runtime_paths: RuntimePaths,
) -> Config:
    """Return a runtime-bound copy of a test config.

    Unless the test config authors them, a reply waits no coalescing debounce and starts no automatic thread summary.
    A summary builds a model by name in the background, usually the reply's own ``default``, so it would take a turn
    that the test scripted for the reply. A config that authors no models gets the ``default`` model its entities name.
    """
    data = config.authored_model_dump()
    if not data.get("models"):
        data["models"] = {"default": {"provider": "openai", "id": "test-model"}}
    bound = Config.validate_with_runtime(data, runtime_paths)
    persist_entity_accounts(bound, runtime_paths)
    authored_coalescing = config.defaults.coalescing
    if "debounce_ms" not in authored_coalescing.model_fields_set:
        bound.defaults.coalescing.debounce_ms = 0
    if "thread_summary_first_threshold" not in config.defaults.model_fields_set:
        # No test thread reaches this many messages.
        bound.defaults.thread_summary_first_threshold = sys.maxsize
    _TEST_RUNTIME_PATHS_BY_CONFIG_ID[id(bound)] = runtime_paths
    return bound


def runtime_paths_for(config: Config) -> RuntimePaths:
    """Return the explicit runtime context previously bound to a test config."""
    runtime_paths = _TEST_RUNTIME_PATHS_BY_CONFIG_ID.get(id(config))
    if runtime_paths is None:
        msg = "Test config is missing bound RuntimePaths"
        raise KeyError(msg)
    return runtime_paths


def cached_plugin_entry(module_path: Path) -> plugin_imports._ModuleCacheEntry:
    """Return the one plugin module cache entry for a file, whatever other plugins earlier tests left cached."""
    (entry,) = (
        cached for cached in plugin_imports._MODULE_IMPORT_CACHE.values() if cached.module.__file__ == str(module_path)
    )
    return entry


def cached_plugin_module(module_path: Path) -> ModuleType:
    """Return the one cached plugin module imported from a file."""
    return cached_plugin_entry(module_path).module


def register_test_tool(monkeypatch: pytest.MonkeyPatch, metadata: ToolMetadata) -> None:
    """Register ``metadata`` as one built-in tool for the rest of the test.

    Built-in tools never change in a running process, so views composed before the change are dropped.
    """
    tool_registry_module.builtin_tool_registry()
    monkeypatch.setitem(tool_registry_module._BUILTIN_TOOLS, metadata.name, metadata)
    plugins_module._CONFIG_PLUGINS.clear()


def replace_test_tool_factory(monkeypatch: pytest.MonkeyPatch, tool_name: str, factory: Callable[[], type]) -> None:
    """Build the built-in tool ``tool_name`` from ``factory`` for the rest of the test."""
    metadata = tool_registry_module.builtin_tool_registry().entries[tool_name]
    register_test_tool(monkeypatch, replace(metadata, factory=factory))


def publish_test_plugins(config: Config, runtime_paths: RuntimePaths) -> plugins_module.ConfigPlugins:
    """Publish ``config``'s plugins as startup does and return its view, failing on a plugin that does not load.

    The autouse restore of the plugin state undoes the publication after the test.
    """
    generation = plugins_module.prepare_plugins(config, runtime_paths)
    if generation.skipped:
        plugins_module.dispose_plugins(generation)
        raise generation.skipped[0].error
    plugins_module.publish_plugins(generation)
    return plugins_module.config_plugins(config, runtime_paths)


# Plugin code a test writes reaches the test's own objects through probe modules under this prefix; the autouse
# plugin-state restore removes them after the test.
_TEST_PROBE_PREFIX = "mindroom_test_probe_"


def write_hook_plugin(plugin_root: Path, name: str, callbacks: Sequence[object]) -> None:
    """Write plugin ``name`` at ``plugin_root`` whose hooks module offers the test's ``@hook`` functions ``callbacks``.

    The hooks module takes them from a probe module, so a test observes its own hooks through a real plugin publication.
    """
    probe = ModuleType(f"{_TEST_PROBE_PREFIX}{uuid.uuid4().hex}")
    probe.callbacks = tuple(callbacks)
    sys.modules[probe.__name__] = probe
    plugin_root.mkdir(parents=True, exist_ok=True)
    (plugin_root / "mindroom.plugin.json").write_text(
        json.dumps({"name": name, "hooks_module": "hooks.py", "skills": []}),
        encoding="utf-8",
    )
    exports = "".join(f"hook_{index} = _probe.callbacks[{index}]\n" for index in range(len(callbacks)))
    (plugin_root / "hooks.py").write_text(
        f"import sys\n\n_probe = sys.modules[{probe.__name__!r}]\n{exports}",
        encoding="utf-8",
    )


def publish_hook_plugin(
    config: Config,
    runtime_paths: RuntimePaths,
    name: str,
    callbacks: Sequence[object],
) -> plugins_module.ConfigPlugins:
    """Publish plugin ``name``, whose hooks are ``callbacks``, as one of ``config``'s plugins and return its view.

    The view's hooks are the registry the orchestrator activates for ``config`` and every later turn's context
    captures. Publishing ``name`` again replaces its hooks.
    """
    write_hook_plugin(runtime_paths.config_dir / "plugins" / name, name, callbacks)
    entry = PluginEntryConfig(path=f"./plugins/{name}")
    if entry not in config.plugins:
        config.plugins.append(entry)
    return publish_test_plugins(config, runtime_paths)


GENERATION_PROBE_TOOL = "generation_probe"
_GENERATION_PROBE_MODULE = f"{_TEST_PROBE_PREFIX}generation"


def write_generation_probe_plugin(plugin_root: Path, generation: int) -> list[tuple[str, int]]:
    """Write plugin ``generation-probe`` at ``plugin_root`` as of ``generation`` and return what its calls report.

    Its tool ``generation_probe`` offers ``which_generation``, whose body appends ``("tool", generation)`` and whose
    before-call hook appends ``("hook", generation)`` to the returned list, so a test tells which publication each
    half of one call came from. Rewriting the plugin with another ``generation`` keeps the list.
    """
    probe = sys.modules.get(_GENERATION_PROBE_MODULE)
    if probe is None:
        probe = ModuleType(_GENERATION_PROBE_MODULE)
        probe.seen = []
        sys.modules[_GENERATION_PROBE_MODULE] = probe
    plugin_root.mkdir(parents=True, exist_ok=True)
    (plugin_root / "mindroom.plugin.json").write_text(
        json.dumps(
            {"name": "generation-probe", "tools_module": "tools.py", "hooks_module": "hooks.py", "skills": []},
        ),
        encoding="utf-8",
    )
    header = f"import sys\n\nGENERATION = {generation}\n_seen = sys.modules[{_GENERATION_PROBE_MODULE!r}].seen\n\n"
    (plugin_root / "tools.py").write_text(
        header + "from agno.tools import Toolkit\n"
        "\n"
        "from mindroom.plugin_api import ToolCategory, ToolFileAccess, register_tool_with_metadata\n"
        "\n"
        "class GenerationProbeTools(Toolkit):\n"
        "    def __init__(self) -> None:\n"
        f"        super().__init__(name={GENERATION_PROBE_TOOL!r}, tools=[self.which_generation])\n"
        "\n"
        "    async def which_generation(self) -> str:\n"
        '        """Return the plugin generation this tool came from."""\n'
        "        _seen.append(('tool', GENERATION))\n"
        "        return str(GENERATION)\n"
        "\n"
        "@register_tool_with_metadata(\n"
        f"    name={GENERATION_PROBE_TOOL!r},\n"
        "    file_access=ToolFileAccess.NONE,\n"
        "    display_name='Generation Probe',\n"
        "    description='Report the plugin generation',\n"
        "    category=ToolCategory.DEVELOPMENT,\n"
        ")\n"
        "def generation_probe_tools():\n"
        "    return GenerationProbeTools\n",
        encoding="utf-8",
    )
    (plugin_root / "hooks.py").write_text(
        header + "from mindroom.plugin_api import hook\n"
        "\n"
        "@hook('tool:before_call')\n"
        "async def record_generation(ctx):\n"
        "    if ctx.tool_name == 'which_generation':\n"
        "        _seen.append(('hook', GENERATION))\n",
        encoding="utf-8",
    )
    return probe.seen


def snapshot_builtin_tools() -> dict[str, ToolMetadata]:
    """Return a copy of the built-in tools, for `restore_builtin_tools`."""
    return dict(tool_registry_module.builtin_tool_registry().entries)


def restore_builtin_tools(snapshot: Mapping[str, ToolMetadata]) -> None:
    """Make the built-in tools the ones ``snapshot`` copied, dropping every tool registered since."""
    tool_registry_module._BUILTIN_TOOLS.clear()
    tool_registry_module._BUILTIN_TOOLS.update(snapshot)


@contextmanager
def preserved_builtin_tools() -> Iterator[None]:
    """Restore the built-in tools a block registers or replaces.

    Built-in tools never change in a running process, so config views composed before the change are dropped.
    """
    snapshot = snapshot_builtin_tools()
    plugins_module._CONFIG_PLUGINS.clear()
    try:
        yield
    finally:
        restore_builtin_tools(snapshot)
        plugins_module._CONFIG_PLUGINS.clear()


@pytest.fixture
def restored_builtin_tools() -> Iterator[None]:
    """Restore the built-in tools the test registers or replaces when it ends."""
    with preserved_builtin_tools():
        yield


@pytest.fixture(autouse=True)
def _restore_published_plugins() -> Iterator[None]:
    """Restore the published plugin generation and the installed plugin modules a test loads or publishes.

    The probe modules a test's plugins read go too.
    """
    published = plugins_module._PUBLISHED
    module_cache = dict(plugin_imports._MODULE_IMPORT_CACHE)
    manifest_cache = dict(plugin_imports._PLUGIN_CACHE)
    modules = {
        name: module
        for name, module in sys.modules.items()
        if name.startswith(tool_registry_module.PLUGIN_MODULE_PREFIX)
    }
    yield
    plugins_module._PUBLISHED = published
    plugin_imports._MODULE_IMPORT_CACHE.clear()
    plugin_imports._MODULE_IMPORT_CACHE.update(module_cache)
    plugin_imports._PLUGIN_CACHE.clear()
    plugin_imports._PLUGIN_CACHE.update(manifest_cache)
    for name in [
        name
        for name in sys.modules
        if name.startswith(
            (
                tool_registry_module.PLUGIN_MODULE_PREFIX,
                tool_registry_module.CANDIDATE_PLUGIN_MODULE_PREFIX,
                _TEST_PROBE_PREFIX,
            ),
        )
    ]:
        if name not in modules:
            del sys.modules[name]
    sys.modules.update(modules)


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
        user_ids = [f"@mindroom_{agent}:{domain}" for agent in agents]
        room.users = {user_id: nio.MatrixUser(user_id) for user_id in user_ids}
    else:
        room.users = {}
    return room


def completing(respond: Callable[..., Awaitable[str]]) -> Callable[..., Awaitable[mindroom_ai.TurnResult]]:
    """Return an ``ai_response`` stand-in that answers as ``respond`` does, in a turn that completed.

    A blocking caller reads a turn that holds no completed attempt as failed, so a bare ``TurnResult`` is a failed turn.
    """

    async def complete(*args: object, **kwargs: object) -> mindroom_ai.TurnResult:
        text = await respond(*args, **kwargs)
        return mindroom_ai.TurnResult(
            text=text,
            completed=CompletedAttempt(replayable_text=text, has_visible_content=bool(text)),
        )

    return complete


def completed_ai_response_mock(**kwargs: object) -> AsyncMock:
    """Return an ``AsyncMock`` ``ai_response`` whose turn completes with its ``return_value``."""
    mock = AsyncMock(**kwargs)

    async def respond(*_args: object, **_kwargs: object) -> str:
        return mock.return_value

    mock.side_effect = completing(respond)
    return mock


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
    runner.locked.deps = replace(runner.deps, request_preparer=preparer)


async def prepare_payload_via_seam(bot: RuntimeBot, execute_args: tuple[object, ...]) -> None:
    """Drive the execution-side payload preparation from captured dispatch args."""
    event = cast("DispatchEvent", execute_args[1])
    dispatch = cast("PreparedDispatch", execute_args[2])
    payload_inputs = cast("DispatchPayloadInputs", execute_args[4])
    await bot._request_payload_preparer.prepare(
        ResponseRequest(
            sources=ResponseSources(
                pending_event_ids=(dispatch.envelope.source_event_id,),
                logical_source_event_ids=(dispatch.envelope.source_event_id,),
            ),
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
    """Give a client a test swapped in the bot's user id, as a logged-in client has."""
    client = bot.client
    if client is not None and getattr(client, "user_id", None) is None:
        client.user_id = bot.matrix_id.full_id


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
    owner_field_names = _turn_owner_field_names(bot)
    owner_changes = {name: value for name, value in changes.items() if name in owner_field_names}
    if policy_changes:
        owner_changes["turn_policy"] = bot._turn_policy
    if store_changes:
        owner_changes["turn_store"] = bot._turn_store
    if owner_changes:
        replace_turn_deps(bot, **owner_changes)
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
    replace_turn_deps(bot, delivery_gateway=bot._delivery_gateway)
    replace_response_runner_deps(bot, delivery_gateway=bot._delivery_gateway)
    return rebuilt


def replace_response_runner_deps(bot: RuntimeBot, **changes: object) -> ResponseRunner:
    """Rebuild the response runner after swapping captured collaborators."""
    sync_bot_runtime_state(bot)
    coordinator = unwrap_extracted_collaborator(bot._response_runner)
    bot._build_reply_turn_owners(replace(coordinator.deps, **changes))
    rebuilt = bot._response_runner
    wrap_extracted_collaborators(bot, "_response_runner")
    replace_turn_deps(bot, response_runner=bot._response_runner)
    return rebuilt


async def finish_edit_regenerations(bot: RuntimeBot) -> None:
    """Wait for the regenerations edits started off the room's lane, as a test checks their effects."""
    runner = unwrap_extracted_collaborator(bot._response_runner)
    await asyncio.gather(*tuple(runner.inbox._inbox_response_tasks), return_exceptions=True)


def replace_edit_regenerator_deps(bot: RuntimeBot, **changes: object) -> EditRegenerator:
    """Rebuild the edit regenerator after swapping captured collaborators."""
    install_runtime_journal_support(bot)
    regenerator = unwrap_extracted_collaborator(bot._edit_regenerator)
    regenerator_field_names = set(regenerator.deps.__dataclass_fields__)
    store_field_names = set(unwrap_extracted_collaborator(bot._turn_store).deps.__dataclass_fields__)
    unknown = set(changes) - regenerator_field_names - store_field_names
    assert not unknown, f"not an edit regenerator or turn store collaborator: {sorted(unknown)}"
    rebuilt_changes = {name: value for name, value in changes.items() if name in regenerator_field_names}
    if "receipt_order" not in rebuilt_changes:
        receipt_orders = count(1)

        async def next_receipt_order() -> int:
            return next(receipt_orders)

        rebuilt_changes["receipt_order"] = next_receipt_order
    store_changes = {name: value for name, value in changes.items() if name in store_field_names}
    if store_changes:
        replace_turn_store_deps(bot, **store_changes)
        rebuilt_changes["turn_store"] = bot._turn_store
    rebuilt = EditRegenerator(replace(regenerator.deps, **rebuilt_changes))
    bot._edit_regenerator = rebuilt
    wrap_extracted_collaborators(bot, "_edit_regenerator")
    replace_turn_deps(bot, edit_regenerator=bot._edit_regenerator)
    return rebuilt


def install_edit_regeneration_mock(
    bot: RuntimeBot,
    generate_response: Callable[[ResponseRequest], Awaitable[object]],
) -> EditRegenerator:
    """Answer the bot's edit regenerations of an agent's turns through ``generate_response``, as its runner would."""
    runner = _ExtractedCollaboratorProxy(unwrap_extracted_collaborator(bot._response_runner))
    runner.generate_response = generate_response
    return replace_edit_regenerator_deps(bot, response_runner=runner)


def replace_reaction_dispatcher_deps(bot: RuntimeBot, **changes: object) -> ReactionDispatcher:
    """Rebuild reaction dispatch after swapping collaborators captured at construction."""
    rebuilt = ReactionDispatcher(replace(bot._reaction_dispatcher.deps, **changes))
    bot._reaction_dispatcher = rebuilt
    return rebuilt


def replace_interactive_selection_handlers(
    bot: RuntimeBot,
    *,
    handle: Callable[..., Awaitable[bool]] | None = None,
    start: Callable[..., Awaitable[None]] | None = None,
) -> None:
    """Replace the interactive-selection handlers for one test bot."""
    selections = bot._interactive_selections
    if handle is not None:
        selections._handle_interactive_selection = handle  # type: ignore[method-assign]
    if start is not None:
        selections._start_interactive_selection = start  # type: ignore[method-assign]


def _changes_for(deps: "DataclassInstance", changes: Mapping[str, object]) -> dict[str, object]:
    """Return the changes that name one owner's deps fields."""
    return {name: value for name, value in changes.items() if name in deps.__dataclass_fields__}


_VOICE_READINESS_CHANGES = {
    "normalizer": "normalizer",
    "turn_store": "turn_store",
    "visible_voice_echo": "visible_echo",
    "logger": "logger",
}


def _turn_owners(bot: RuntimeBot) -> tuple[TurnIngress, TurnDispatcher, InteractiveSelections, VoiceIngress]:
    """Return the bot's turn owners: ingress, dispatch, interactive selections, and voice ingress."""
    return bot._turn_ingress, bot._turn_dispatcher, bot._interactive_selections, bot._voice_ingress


def _turn_owner_field_names(bot: RuntimeBot) -> set[str]:
    """Return every deps field some turn owner has."""
    return set().union(*(owner.deps.__dataclass_fields__ for owner in _turn_owners(bot)))


def _held_turn_collaborator(bot: RuntimeBot, field_name: str) -> object:
    """Return the collaborator the turn owners hold for one deps field."""
    return next(
        getattr(owner.deps, field_name) for owner in _turn_owners(bot) if field_name in owner.deps.__dataclass_fields__
    )


def update_turn_owner_deps(bot: RuntimeBot, **changes: object) -> None:
    """Swap collaborators in place on every turn owner whose deps name them, voice readiness included.

    In place, because the gate and the reaction dispatcher keep the owners they were built with.
    """
    owners = _turn_owners(bot)
    known = set(_VOICE_READINESS_CHANGES) | _turn_owner_field_names(bot)
    unknown = set(changes) - known
    assert not unknown, f"not a turn owner collaborator: {sorted(unknown)}"
    for owner in owners:
        if owner_changes := _changes_for(owner.deps, changes):
            owner.deps = replace(owner.deps, **owner_changes)
    readiness_changes = {field: changes[name] for name, field in _VOICE_READINESS_CHANGES.items() if name in changes}
    if readiness_changes:
        voice = bot._voice_ingress
        voice.deps = replace(voice.deps, readiness=replace(voice.deps.readiness, **readiness_changes))


def replace_turn_deps(bot: RuntimeBot, **changes: object) -> None:
    """Swap turn collaborators captured at construction, rebuilding the collaborators that capture them too."""
    sync_bot_runtime_state(bot)
    owner_field_names = _turn_owner_field_names(bot)
    rebuilt_changes = {name: value for name, value in changes.items() if name in owner_field_names}
    default_collaborators = {
        "resolver": "_conversation_resolver",
        "normalizer": "_inbound_turn_normalizer",
        "command_executor": "_command_turn_executor",
        "turn_policy": "_turn_policy",
        "ingress_hook_runner": "_ingress_hook_runner",
        "response_runner": "_response_runner",
        "delivery_gateway": "_delivery_gateway",
        "turn_store": "_turn_store",
        "edit_regenerator": "_edit_regenerator",
        "visible_responses": "_visible_responses",
    }
    # The owners are updated in place, so a collaborator a test passed in stays until
    # the test passes another; only a bot attribute swapped since the last call replaces it.
    propagated = bot.__dict__.setdefault("_propagated_turn_collaborators", {})
    for field_name, attr_name in default_collaborators.items():
        held = _held_turn_collaborator(bot, field_name)
        propagated.setdefault(attr_name, held)
        if field_name not in rebuilt_changes:
            current = getattr(bot, attr_name)
            rebuilt_changes[field_name] = current if current is not propagated[attr_name] else held
    store_field_names = set(unwrap_extracted_collaborator(bot._turn_store).deps.__dataclass_fields__)
    store_changes = {name: value for name, value in changes.items() if name in store_field_names}
    if store_changes:
        replace_turn_store_deps(bot, **store_changes)
        rebuilt_changes["turn_store"] = bot._turn_store
    if "edit_regenerator" not in rebuilt_changes:
        rebuilt_changes["edit_regenerator"] = bot._edit_regenerator
    if "ingress" not in rebuilt_changes:
        validator_deps = bot._ingress_validator.deps
        rebuilt_changes["ingress"] = IngressValidator(
            replace(
                validator_deps,
                turn_store=rebuilt_changes["turn_store"],
                turn_policy=rebuilt_changes["turn_policy"],
                **{name: changes[name] for name in ("settle_ignored_sources",) if name in changes},
            ),
        )
    bot._ingress_validator = rebuilt_changes["ingress"]
    visible_voice_echo = unwrap_extracted_collaborator(bot._visible_voice_echo)
    bot._visible_voice_echo = VisibleVoiceEchoLifecycle(
        replace(
            visible_voice_echo.deps,
            runtime=rebuilt_changes.get("runtime", _held_turn_collaborator(bot, "runtime")),
            logger=rebuilt_changes.get("logger", _held_turn_collaborator(bot, "logger")),
            agent_name=rebuilt_changes.get("agent_name", _held_turn_collaborator(bot, "agent_name")),
            delivery_gateway=rebuilt_changes["delivery_gateway"],
            turn_store=rebuilt_changes["turn_store"],
            ingress=rebuilt_changes["ingress"],
        ),
    )
    wrap_extracted_collaborators(bot, "_visible_voice_echo")
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
            runtime=rebuilt_changes.get("runtime", _held_turn_collaborator(bot, "runtime")),
            logger=rebuilt_changes.get("logger", _held_turn_collaborator(bot, "logger")),
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
            runtime=rebuilt_changes.get("runtime", _held_turn_collaborator(bot, "runtime")),
            logger=rebuilt_changes.get("logger", _held_turn_collaborator(bot, "logger")),
            runtime_paths=rebuilt_changes.get("runtime_paths", _held_turn_collaborator(bot, "runtime_paths")),
            agent_name=rebuilt_changes.get("agent_name", _held_turn_collaborator(bot, "agent_name")),
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
            delivery_gateway=rebuilt_changes["delivery_gateway"],
        ),
    )
    wrap_extracted_collaborators(bot, "_user_stop_reconciler")
    update_turn_owner_deps(bot, **rebuilt_changes, visible_voice_echo=bot._visible_voice_echo)
    ingress = bot._turn_ingress
    reaction_dispatcher = bot._reaction_dispatcher
    replace_reaction_dispatcher_deps(
        bot,
        runtime=ingress.deps.runtime,
        logger=ingress.deps.logger,
        runtime_paths=ingress.deps.runtime_paths,
        agent_name=ingress.deps.agent_name,
        user_stop_reconciler=bot._user_stop_reconciler,
        ingress=ingress.deps.ingress,
        reserve_prompt_ingress_order=ingress.reserve_prompt_ingress_order,
        enqueue_interactive_selection=bot._interactive_selections.enqueue_interactive_selection,
        config_confirmation=replace(
            reaction_dispatcher.deps.config_confirmation,
            runtime=ingress.deps.runtime,
            runtime_paths=ingress.deps.runtime_paths,
            build_message_target=ingress.deps.resolver.build_message_target,
            delivery_gateway=ingress.deps.delivery_gateway,
        ),
    )
    edit_changes = {
        name: value
        for name, value in changes.items()
        if name in unwrap_extracted_collaborator(bot._edit_regenerator).deps.__dataclass_fields__
    }
    if edit_changes:
        replace_edit_regenerator_deps(bot, **edit_changes)
    propagated.update({attr_name: getattr(bot, attr_name) for attr_name in default_collaborators.values()})


@contextmanager
def patch_response_runner_module(**changes: object) -> Generator[None]:
    """Patch module-level response runner seams on the real current owners.

    A seam is patched in every reply module that imports it, so a resumed approval sees the replacement too.
    """
    with ExitStack() as stack:
        for name, replacement in changes.items():
            if name == "apply_post_response_effects":
                module_names = ["mindroom.response_lifecycle"]
            else:
                module_names = [
                    module.__name__
                    for module in (
                        response_runner_module,
                        reply_request_module,
                        locked_turn_module,
                        continuation_runner_module,
                    )
                    if name in vars(module)
                ]
            for module_name in module_names:
                stack.enter_context(patch(f"{module_name}.{name}", new=replacement))
        yield


def install_personal_room_shutdown_mock(bot: AgentBot) -> None:
    """Install lifecycle cancellation for partial shutdown fixtures through one seam."""
    bot._personal_room_lifecycle = MagicMock(spec=PersonalRoomLifecycle)


def install_shutdown_drain_mocks(
    bot: RuntimeBot,
    *,
    coalescing_drain_result: CoalescingDrainResult,
    responses_drained: bool,
    response_recovery_complete: bool,
) -> None:
    """Install exact shutdown drain outcomes through stable collaborator seams."""
    wrap_extracted_collaborators(bot, "_coalescing_gate")
    bot._coalescing_gate.drain_all = AsyncMock(
        return_value=coalescing_drain_result,
    )
    bot._response_runner.inbox.drain_inbox_responses = AsyncMock(return_value=responses_drained)
    bot._response_runner.inbox._incomplete_inbox_responses_recoverable = response_recovery_complete


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
                    Failed(FailureCause.DELIVERY_FAILED),
                    event_id=request.existing_event_id,
                    is_visible_response=True,
                    final_visible_body=request.response_text,
                    extra_content=request.extra_content,
                )
            return _outcome(Failed(FailureCause.DELIVERY_FAILED))
        return _outcome(
            Completed(),
            event_id=event_id,
            is_visible_response=True,
            final_visible_body=request.response_text,
            delivery_kind=delivery_kind,
            extra_content=request.extra_content,
        )

    bot._delivery_gateway.deliver_final = AsyncMock(side_effect=_deliver_final)
    replace_turn_deps(bot, delivery_gateway=bot._delivery_gateway)
    replace_response_runner_deps(bot, delivery_gateway=bot._delivery_gateway)


async def record_turn_answered(bot: RuntimeBot, request: ResponseRequest) -> None:
    """Settle a faked response's sources and record its turn answered, as a real reply's records do."""
    await bot._journal_dispatcher.store.settle_many(request.sources.pending_event_ids)
    turn_store = unwrap_extracted_collaborator(bot._turn_store)
    await turn_store.deps.turn_records.write(turn_rows.complete_turn_of, request.sources.logical_source_event_ids)


def install_generate_response_mock(bot: RuntimeBot, generate_response: AsyncMock) -> None:
    """Route response execution through one envelope-explicit generate-response mock."""
    wrap_extracted_collaborators(bot, "_response_runner")

    def _resolved_event_id_from_test_result(
        result: FinalDeliveryOutcome | str | None,
    ) -> str | None:
        if isinstance(result, FinalDeliveryOutcome):
            return result.final_visible_event_id
        return result

    async def _generate(request: ResponseRequest) -> SourceFate:
        if request.payload_preparation is not None:
            try:
                request = await bot._request_payload_preparer.prepare(request)
            except Exception:
                # A real reply's records would show the failure and settle its sources.
                return Owned()
        attachment_ids = list(request.attachment_ids) if request.attachment_ids is not None else None
        result = await generate_response(
            prompt=request.prompt,
            thread_history=request.thread_history,
            existing_event_id=request.existing_event_id,
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
        if isinstance(result, Owned | Unclaimed):
            # The mock says what its response did with the sources.
            return result
        event_id = _resolved_event_id_from_test_result(result)
        if event_id is not None:
            await record_turn_answered(bot, request)
        # A faked answer's sources are settled above; a faked silence leaves them pending, as a released reply does.
        return Owned()

    bot._response_runner.generate_response = AsyncMock(side_effect=_generate)
    replace_turn_deps(bot, response_runner=bot._response_runner)


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
    replace_turn_deps(bot, delivery_gateway=bot._delivery_gateway)
    replace_response_runner_deps(bot, delivery_gateway=bot._delivery_gateway)


def install_model_providers(monkeypatch: pytest.MonkeyPatch, providers: Mapping[str, "ModelProvider"]) -> None:
    """Build the models ``providers`` names as the providers it maps them to, and every other model as before.

    An earlier install builds the other models; a model no install maps raises ``LookupError`` instead of building
    a real provider. ``providers`` is read at every build, so a test may replace a provider between two turns.
    """
    earlier = provider_registry.build_model_provider

    def build(
        config: Config,
        paths: RuntimePaths,
        model_name: str = "default",
        *,
        execution_identity: "ToolExecutionIdentity | None" = None,
    ) -> "ModelProvider":
        if model_name in providers:
            return providers[model_name]
        if earlier is _REAL_BUILD_MODEL_PROVIDER:
            msg = f"no provider installed for model {model_name!r}"
            raise LookupError(msg)
        return earlier(config, paths, model_name, execution_identity=execution_identity)

    monkeypatch.setattr(provider_registry, "build_model_provider", build)


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


@pytest.fixture
def test_ca_pem() -> str:
    """Return a valid test CA certificate in PEM format."""
    return make_test_ca_pem()


@pytest_asyncio.fixture
async def aioresponse() -> AsyncGenerator[aioresponses]:
    """Async fixture for mocking HTTP responses in tests."""
    # Based on https://github.com/matrix-nio/matrix-nio/blob/main/tests/conftest_async.py
    with aioresponses() as m:
        yield m


# Deadline for a test wait that should finish. A passing wait returns as soon as its work is done,
# so the deadline only bounds a hang; it is generous because parallel runs stall background work.
SETTLE_TIMEOUT_SECONDS = 30.0


def loop_runs_while_blocked(loop: asyncio.AbstractEventLoop) -> bool:
    """From inside a blocking call, return whether ``loop`` runs a callback meanwhile, as it can only when the call runs off the loop.

    A call on the loop's own thread gets False at once, so a test whose calls moved onto the loop fails without waiting.
    """
    with suppress(RuntimeError):
        if asyncio.get_running_loop() is loop:
            return False
    loop_ran = threading.Event()
    loop.call_soon_threadsafe(loop_ran.set)
    return loop_ran.wait(timeout=SETTLE_TIMEOUT_SECONDS)


@pytest.fixture(autouse=True)
def _isolate_structlog_configuration(
    monkeypatch: pytest.MonkeyPatch,
    request: pytest.FixtureRequest,
) -> Generator[None]:
    """Silence incidental logs and prevent global logging configuration leaks."""
    _configure_quiet_structlog()
    if request.node.path.name != "test_logging_config.py":
        monkeypatch.setattr(structlog, "configure", _configure_uncached_structlog)
    # `setup_logging` reroutes Agno's loggers; give later tests on this worker Agno's own setup back.
    agno_loggers = [
        (agno_logger, agno_logger.handlers[:], agno_logger.filters[:], agno_logger.level, agno_logger.propagate)
        for agno_logger in (agno_log.agent_logger, agno_log.team_logger, agno_log.workflow_logger)
    ]
    yield
    _configure_quiet_structlog()
    for agno_logger, handlers, filters, level, propagate in agno_loggers:
        agno_logger.handlers[:] = handlers
        agno_logger.filters[:] = filters
        agno_logger.setLevel(level)
        agno_logger.propagate = propagate


@pytest.fixture(autouse=True)
def _pin_matrix_homeserver(monkeypatch: pytest.MonkeyPatch) -> None:
    """Keep test runtime defaults isolated from shell-level runtime overrides.

    Tests use ':localhost' Matrix IDs and non-namespaced localparts unless they
    explicitly opt into a different runtime context.
    """
    monkeypatch.setenv("MATRIX_HOMESERVER", "http://localhost:8008")
    monkeypatch.delenv("MATRIX_SERVER_NAME", raising=False)
    monkeypatch.delenv("MINDROOM_NAMESPACE", raising=False)
    monkeypatch.delenv("MINDROOM_CONFIG_PATH", raising=False)
    monkeypatch.delenv("MINDROOM_STORAGE_PATH", raising=False)


@pytest.fixture(autouse=True)
def _homeserver_without_live_frames(monkeypatch: pytest.MonkeyPatch) -> None:
    """Keep streams from asking a homeserver whether it relays live frames; a test that needs frames opts in."""

    async def no_live_frames(_homeserver_url: str) -> bool:
        return False

    monkeypatch.setattr(streaming_module, "homeserver_relays_live_frames", no_live_frames)


@pytest.fixture(autouse=True)
def _reset_memory_entrypoint_snapshots(monkeypatch: pytest.MonkeyPatch) -> None:
    """Keep a conversation's kept MEMORY.md copy from leaking into later tests."""
    monkeypatch.setattr(memory_entrypoint_snapshots_module, "_snapshots", {})


@pytest.fixture(autouse=True, scope="session")
def _loaded_o200k() -> None:
    """Load the token encoder once, so no test's estimates depend on how far its background load has come."""
    load_o200k()


@pytest.fixture(autouse=True)
def _withdraw_embedded_runtime_from_the_shared_api_app() -> Generator[None]:
    """Withdraw a runtime a test lent the shared dashboard app, so each test binds the runtime it needs."""
    yield
    api_main = sys.modules.get("mindroom.api.main")
    if api_main is not None:
        api_main.unbind_embedded_runtime(api_main.app)


@pytest.fixture(autouse=True)
def _reset_active_mcp_server_manager(monkeypatch: pytest.MonkeyPatch) -> None:
    """Keep an orchestrator's process-wide MCP manager from leaking into later tests."""
    monkeypatch.setattr(mcp_toolkit_module, "_ACTIVE_MCP_SERVER_MANAGER", None)


@pytest.fixture(autouse=True)
def _reset_runtime_refresh_scheduler(monkeypatch: pytest.MonkeyPatch) -> None:
    """Keep a test's process-wide knowledge refresh scheduler, bound to its event loop, from leaking into later tests."""
    monkeypatch.setattr(knowledge_refresh_scheduler_module, "_runtime_scheduler", None)


@pytest.fixture(autouse=True)
def _restore_gil_switch_interval() -> Generator[None]:
    """Keep a primary started by one test from shortening the GIL switch interval for later tests."""
    interval = sys.getswitchinterval()
    yield
    sys.setswitchinterval(interval)


@pytest.fixture(autouse=True)
def _restore_gc_thresholds() -> Generator[None]:
    """Keep a runtime refreeze in one test from raising the full-collection threshold for later tests."""
    thresholds = gc.get_threshold()
    yield
    gc.set_threshold(*thresholds)


@pytest.fixture(autouse=True)
def _reopen_primary_worker_manager() -> Generator[None]:
    """Reopen the process-wide primary worker manager after a test runs a real shutdown."""
    yield
    if not workers_runtime_module._PRIMARY_WORKER_MANAGER_ACCEPTING_BUILDS:
        workers_runtime_module.reset_primary_worker_manager()


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


def _refuse_connection(request: httpx.Request) -> NoReturn:
    message = "network disabled in tests"
    raise httpx.ConnectError(message, request=request)


def _refuse_model_connection(request: httpx2.Request) -> NoReturn:
    """Refuse a model SDK's request as its httpx2 transport refuses an unreachable host."""
    message = "network disabled in tests"
    raise httpx2.ConnectError(message, request=request)


@pytest.fixture(autouse=True)
def _never_download_stock_avatars(monkeypatch: pytest.MonkeyPatch) -> None:
    """Keep bot startup and room creation from fetching stock avatars over the network.

    Tests behave like an offline machine, which leaves entities without a stock avatar.
    `tests/test_managed_avatars.py` installs its own downloader to cover the real behavior.
    """

    async def offline(url: str) -> bytes:
        _refuse_connection(httpx.Request("GET", url))

    monkeypatch.setattr(managed_avatars_module, "_download_stock_avatar", offline)


@pytest.fixture
def current_static_runner(monkeypatch: pytest.MonkeyPatch) -> None:
    """Answer the static sandbox runner's health probe as a runner of this release, for tests that fake no runner."""

    def current(runner_root: str) -> httpx.Response:
        return httpx.Response(
            200,
            json=asdict(worker_health_payload(mindroom_version="test")),
            request=httpx.Request("GET", f"{runner_root}/healthz"),
        )

    monkeypatch.setattr(static_runner_module, "_read_runner_health", current)


# Captured before any test patches them, so the offline clients below still start from the production ones.
_PRODUCTION_EMBEDDER_CLIENT = OpenAIEmbedder._sdk_client
_PRODUCTION_EMBEDDER_ACLIENT = OpenAIEmbedder._async_sdk_client


def _offline_sync_client(embedder: OpenAIEmbedder) -> OpenAI:
    if embedder.client is None:
        # Without retries, because the SDK would back off for seconds before reporting the refusal.
        embedder.client = _PRODUCTION_EMBEDDER_CLIENT(embedder).with_options(
            http_client=httpx2.Client(transport=httpx2.MockTransport(_refuse_model_connection)),
            max_retries=0,
        )
    return embedder.client


def _offline_async_client(embedder: OpenAIEmbedder) -> AsyncOpenAI:
    if embedder.async_client is None:
        embedder.async_client = _PRODUCTION_EMBEDDER_ACLIENT(embedder).with_options(
            http_client=httpx2.AsyncClient(transport=httpx2.MockTransport(_refuse_model_connection)),
            max_retries=0,
        )
    return embedder.async_client


@pytest.fixture
def real_embedder_clients() -> None:
    """Keep the OpenAI embedder SDK clients on their real transport, for tests that serve embeddings locally."""


@pytest.fixture(autouse=True)
def _offline_openai_embedders(request: pytest.FixtureRequest, monkeypatch: pytest.MonkeyPatch) -> None:
    """Keep OpenAI-compatible embedders from sending real embedding requests.

    The default config pairs mem0 memory with the OpenAI embedder, so any test that searches memory
    or probes embedder health builds one. Its requests would go to `OPENAI_BASE_URL` or
    api.openai.com with the keyless placeholder key. Here an embedder without an injected SDK client
    builds its clients the production way, then moves them onto a transport that fails every request
    the way an unreachable endpoint does. Those failures never reach httpcore, so the network guard
    does not report them: a test that uses such an embedder sees a down endpoint, as it would in
    production. Tests that need vectors inject a client or their own embedder.
    """
    if "real_embedder_clients" in request.fixturenames:
        return
    monkeypatch.setattr(OpenAIEmbedder, "_sdk_client", _offline_sync_client)
    monkeypatch.setattr(OpenAIEmbedder, "_async_sdk_client", _offline_async_client)


_OLLAMA_DEFAULT_PORT = urlsplit(OLLAMA_HOST_DEFAULT).port


def _dial_leaves_the_test(host: str, port: int) -> bool:
    """Return whether to refuse dialing ``host:port``.

    Refused: any host except localhost, ``*.localhost``, or a loopback or unspecified IP literal, plus port 11434,
    where a locally running Ollama listens, on every host. Hostnames are never resolved, so every other name is
    refused, and so are this machine's non-loopback addresses.
    """
    if host == "localhost" or host.endswith(".localhost"):
        local = True
    else:
        try:
            address = ipaddress.ip_address(host.strip("[]"))
        except ValueError:
            local = False
        else:
            local = address.is_loopback or address.is_unspecified
    # Test servers on other local ports stay reachable.
    return not local or port == _OLLAMA_DEFAULT_PORT


@dataclass
class _RefusedDial:
    """One refused dial, the thread that made it, and whether a test failure has named it."""

    address: str
    thread: str
    reported: bool = False


def _refuse_dial_off_the_test(
    host: str,
    port: int,
    connect_error: type[Exception],
    dials: list[_RefusedDial],
) -> None:
    if _dial_leaves_the_test(host, port):
        # httpcore passes IPv6 hosts unbracketed.
        address = f"[{host.strip('[]')}]:{port}" if ":" in host else f"{host}:{port}"
        dials.append(_RefusedDial(address, threading.current_thread().name))
        message = f"network disabled in tests: {address}"
        raise connect_error(message)


def _describe_dials(dials: Iterable[_RefusedDial]) -> str:
    return ", ".join(sorted({f"{dial.address} (thread {dial.thread})" for dial in dials}))


def _report_test_dials(dials: list[_RefusedDial], start: int, test_thread: str) -> str | None:
    """Mark the dials recorded from index ``start`` on as reported, and return the failure for their test."""
    made_during_test = dials[start:]
    if not made_during_test:
        return None
    for dial in made_during_test:
        dial.reported = True
    message = f"test tried to reach the network: {_describe_dials(made_during_test)}"
    if any(dial.thread != test_thread for dial in made_during_test):
        message += (
            f"; a dial from a thread other than the test's own ({test_thread})"
            " may come from an earlier test's background work"
        )
    return message


def _unreported_dials_failure(dials: list[_RefusedDial]) -> str | None:
    """Return the failure for dials no test reported, which were made between tests or after the last one."""
    unreported = [dial for dial in dials if not dial.reported]
    if not unreported:
        return None
    return (
        "background work tried to reach the network between tests or after the last test: "
        f"{_describe_dials(unreported)}"
    )


def _refusing_sync_connect[R](
    connect_tcp: Callable[..., R],
    connect_error: type[Exception],
    dials: list[_RefusedDial],
) -> Callable[..., R]:
    def refusing_connect_tcp(backend: object, host: str, port: int, *args: object, **kwargs: object) -> R:
        _refuse_dial_off_the_test(host, port, connect_error, dials)
        return connect_tcp(backend, host, port, *args, **kwargs)

    return refusing_connect_tcp


def _refusing_async_connect[R](
    connect_tcp: Callable[..., Awaitable[R]],
    connect_error: type[Exception],
    dials: list[_RefusedDial],
) -> Callable[..., Awaitable[R]]:
    async def refusing_connect_tcp(backend: object, host: str, port: int, *args: object, **kwargs: object) -> R:
        _refuse_dial_off_the_test(host, port, connect_error, dials)
        return await connect_tcp(backend, host, port, *args, **kwargs)

    return refusing_connect_tcp


@pytest.fixture(autouse=True, scope="session")
def _refused_dials() -> Generator[list[_RefusedDial]]:
    """Refuse every HTTP client dial that ``_dial_leaves_the_test`` rejects, for the whole session.

    The patch outlives each test so that a background thread or task finishing after its test is still refused.
    A dial no test reported, made between tests or after the last one, fails the session at teardown.
    """
    dials: list[_RefusedDial] = []
    with pytest.MonkeyPatch.context() as patcher:
        for core in (httpcore, httpcore2):
            patcher.setattr(
                core.SyncBackend,
                "connect_tcp",
                _refusing_sync_connect(core.SyncBackend.connect_tcp, core.ConnectError, dials),
            )
            patcher.setattr(
                core.AnyIOBackend,
                "connect_tcp",
                _refusing_async_connect(core.AnyIOBackend.connect_tcp, core.ConnectError, dials),
            )
        yield dials
    failure = _unreported_dials_failure(dials)
    if failure is not None:
        pytest.fail(failure, pytrace=False)


@pytest.fixture(autouse=True)
def _fail_tests_that_reach_the_network(_refused_dials: list[_RefusedDial]) -> Generator[None]:
    """Fail a test whose HTTP client made a dial that ``_dial_leaves_the_test`` rejects.

    The refusal surfaces as a connect error, which the code under test may swallow, so the attempt
    is recorded and checked here. This covers clients built on httpx and httpx2, which include the
    OpenAI, Anthropic, and Ollama SDKs. Raw sockets, aiohttp, and requests are not covered: google-genai's
    async path uses aiohttp, and tiktoken downloads its encodings through requests when its cache is cold.
    """
    start = len(_refused_dials)
    yield
    failure = _report_test_dials(_refused_dials, start, threading.current_thread().name)
    if failure is not None:
        pytest.fail(failure, pytrace=False)


def pytest_configure(config: pytest.Config) -> None:
    """Keep Google client libraries and Git from acting as the developer.

    With no credentials named, google-auth reads gcloud's application default credentials file and asks the GCE
    metadata server, and it refreshes tokens through ``requests``, which the network guard does not cover. An
    empty gcloud config directory, ``NO_GCE_CHECK``, and no inherited ``GOOGLE_APPLICATION_CREDENTIALS`` make a
    test that builds a Google client without credentials fail instead. The helper below also drops the inherited
    Google keys that MindRoom and google-genai read.
    This is a configure hook, not a fixture, because google-auth reads ``NO_GCE_CHECK`` once, when collection first
    imports ``google.auth.compute_engine``.
    Git also ignores the developer's global and system config, so commits in test repositories never sign or run
    template hooks.
    """
    gcloud_config = tempfile.mkdtemp(prefix="mindroom-test-gcloud-")
    config.add_cleanup(lambda: shutil.rmtree(gcloud_config, ignore_errors=True))
    hide_developer_google_credentials(os.environ, gcloud_config)
    os.environ["GIT_CONFIG_GLOBAL"] = os.devnull
    os.environ["GIT_CONFIG_NOSYSTEM"] = "1"


def hide_developer_google_credentials(environ: MutableMapping[str, str], gcloud_config: str) -> None:
    """Point google-auth at an empty gcloud config, skip the GCE metadata server, and drop inherited Google keys.

    google-genai reads ``GOOGLE_API_KEY`` and ``GEMINI_API_KEY`` itself, and its async client uses aiohttp, which the
    network guard does not see either; MindRoom also reads ``GOOGLE_API_KEY_FILE``, and its Google Workspace tools sign
    in with ``GOOGLE_SERVICE_ACCOUNT_FILE`` and ``GOOGLE_DELEGATED_USER``.
    """
    environ["CLOUDSDK_CONFIG"] = gcloud_config
    environ["NO_GCE_CHECK"] = "true"
    for name in (
        "GOOGLE_APPLICATION_CREDENTIALS",
        "GOOGLE_API_KEY",
        "GOOGLE_API_KEY_FILE",
        "GEMINI_API_KEY",
        "GOOGLE_SERVICE_ACCOUNT_FILE",
        "GOOGLE_DELEGATED_USER",
    ):
        environ.pop(name, None)


@pytest.fixture(autouse=True)
def _never_freeze_the_test_heap(monkeypatch: pytest.MonkeyPatch) -> None:
    """Keep orchestrator initialization from freezing the pytest worker's heap.

    Cycles among frozen objects are never collected, so every test that
    initializes an orchestrator would pin its own fixtures, mocks, and
    orchestrator for the rest of the worker's life.
    `tests/test_heap_probe.py` covers the real freeze against a fake `gc`.
    """
    monkeypatch.setattr(gc, "freeze", lambda: None)


@pytest.fixture(scope="session")
def _worker_tmpdir_link_root() -> Generator[Path]:
    """Hold this session's worker TMPDIR links in one short directory removed at the end."""
    path = Path(tempfile.mkdtemp(prefix="mr-links-", dir="/tmp"))
    yield path
    shutil.rmtree(path, ignore_errors=True)


@pytest.fixture(autouse=True)
def _isolate_worker_tmpdir_links(monkeypatch: pytest.MonkeyPatch, _worker_tmpdir_link_root: Path) -> None:
    """Keep the worker TMPDIR links that tool subprocess envs create out of the machine's shared /tmp."""
    monkeypatch.setattr(sandbox_exec_module, "_TMPDIR_LINK_ROOT", _worker_tmpdir_link_root)


@pytest.fixture(autouse=True)
def _reset_runtime_paths() -> Generator[None]:
    """Restore process env and bound test runtime mappings after each test."""
    original_env = os.environ.copy()
    original_bound_configs = dict(_TEST_RUNTIME_PATHS_BY_CONFIG_ID)
    yield
    os.environ.clear()
    os.environ.update(original_env)
    _TEST_RUNTIME_PATHS_BY_CONFIG_ID.clear()
    _TEST_RUNTIME_PATHS_BY_CONFIG_ID.update(original_bound_configs)


@pytest.fixture(autouse=True)
def _isolate_tool_job_registrations(monkeypatch: pytest.MonkeyPatch) -> None:
    """Separate each test's pinned instances; tests still own shutdown of live resources."""
    monkeypatch.setattr(tool_jobs_instances, "_instances", {})


@pytest.fixture(autouse=True)
def _reset_model_media_capabilities(monkeypatch: pytest.MonkeyPatch) -> None:
    """Keep process-local learned media support isolated per test."""
    monkeypatch.setattr(media_pipeline, "_LEARNED_UNSUPPORTED", {})


@pytest.fixture(autouse=True)
def _reset_oversized_nonterminal_streaming_edit_rate_limit() -> Generator[None]:
    """Keep the process-global oversized streaming-edit cadence isolated per test, even when one fails."""
    _oversized_nonterminal_streaming_edit_next_allowed_at.clear()
    yield
    _oversized_nonterminal_streaming_edit_next_allowed_at.clear()


@pytest.fixture(autouse=True)
def _reset_turn_claims() -> Generator[None]:
    """Give every test an empty claim registry: it is process-global, shared by every turn store of an agent."""
    _CLAIM_REGISTRIES.clear()
    yield
    _CLAIM_REGISTRIES.clear()


@pytest.fixture(autouse=True)
def _reset_approval_manager_runtime() -> Generator[None]:
    """Give every test a cold approval manager.

    The manager is module-global and outlives the test that initialized it. It
    holds Matrix transport hooks, including the approval-card store every
    inbound message consults to recover a click on a card this process has
    forgotten. A test that builds a bot without starting its runtime never sets
    those hooks, so it inherits whichever ones the previous test on this
    worker left behind -- and awaiting a previous test's mock raises inside a
    message callback nothing is watching, which surfaces as an unrelated test
    hanging until its timeout rather than as a failure here.
    """
    approval_manager_module._MANAGER = None
    yield
    approval_manager_module._MANAGER = None


@pytest.fixture(autouse=True)
def _reset_voice_echo_barriers() -> Generator[None]:
    """Keep cross-bot voice echo ordering state, and its loop bindings, per test."""
    _reset_visible_voice_echo_barriers()
    yield
    _reset_visible_voice_echo_barriers()


@pytest.fixture(autouse=True)
def bypass_authorization(request: pytest.FixtureRequest) -> Generator[None]:
    """Bypass authorization checks in tests by default.

    This allows test users like @user:example.com to interact with agents
    without needing to be in the authorized_users list.

    Tests in test_authorization.py are excluded since they test authorization itself.
    """

    # These defaults never need call tracking. Plain stubs avoid creating six
    # MagicMocks (and their reference cycles) for every test in the suite.
    def allow_reply(*_args: object, **_kwargs: object) -> _ReplyAuthorizationDecision:
        return _ReplyAuthorizationDecision.ALLOWED

    def allow_sender(*_args: object, **_kwargs: object) -> bool:
        return True

    async def allow_target_room(*_args: object, **_kwargs: object) -> bool:
        return True

    # Don't bypass authorization for tests that are specifically testing it
    if "test_authorization" in request.node.parent.name:
        yield
    else:
        with ExitStack() as stack:
            if "enforce_turn_authorization" not in request.fixturenames:
                stack.enter_context(
                    patch(
                        "mindroom.authorization._responder_reply_authorization",
                        new=allow_reply,
                    ),
                )
                stack.enter_context(patch("mindroom.authorization.is_sender_allowed_for_responder", new=allow_sender))
                stack.enter_context(
                    patch(
                        "mindroom.turn_policy.TurnPolicy.can_reply_to_sender_in_room",
                        new=allow_sender,
                    ),
                )
                stack.enter_context(
                    patch("mindroom.approval.inbound.is_sender_allowed_for_responder", new=allow_sender),
                )
                stack.enter_context(
                    patch(
                        "mindroom.custom_tools.attachment_helpers.is_sender_allowed_for_responder",
                        new=allow_sender,
                    ),
                )
                stack.enter_context(
                    patch(
                        "mindroom.custom_tools.attachment_helpers.requester_joined_target_room",
                        new=allow_target_room,
                    ),
                )
                stack.enter_context(
                    patch(
                        "mindroom.custom_tools.matrix_message_idempotency.requester_joined_target_room",
                        new=allow_target_room,
                    ),
                )
                stack.enter_context(
                    patch("mindroom.delegation.lifecycle.is_sender_allowed_for_responder", new=allow_sender),
                )
                # The module is imported at collection, so it binds the real check before this bypass starts.
                stack.enter_context(
                    patch.object(todo_tool_module, "is_sender_allowed_for_responder", new=allow_sender),
                )
            yield


@pytest.fixture
def enforce_turn_authorization() -> None:
    """Keep final TurnPolicy authorization active for tests that exercise it."""


@pytest.fixture
def squad_probe() -> Iterator[type["ProbeToolkit"]]:
    """Register the probe toolkit the team suites' ``squad`` members offer, with cleared records, for the test."""
    from tests.team_turn_helpers import registered_probe  # noqa: PLC0415 - team_turn_helpers imports this module

    with registered_probe() as toolkit:
        yield toolkit


def unstored_team_scope_context(
    config: Config,
    member_names: Sequence[str],
    *,
    team_name: str | None,
) -> ScopeSessionContext:
    """Return the scope context of a team with no stored session, whose store keeps nothing, for building a team."""
    storage = MagicMock()
    return ScopeSessionContext(
        scope=resolve_bound_team_scope(member_names=member_names, config=config, team_name=team_name),
        storage=storage,
        session=None,
        session_id="session-1",
        storage_factory=lambda: storage,
    )


@pytest.fixture
def store(tmp_path: Path) -> "ConversationStore":
    """Open the conversation database ``code`` under ``tmp_path`` as production opens one, with every table."""
    return create_state_storage("code", tmp_path, HistoryScope(kind="agent", scope_id="code"))


@pytest.fixture
def team_store(tmp_path: Path) -> "ConversationStore":
    """Open team ``crew``'s conversation database, named ``code`` as ``store``'s is, so tests query the same tables."""
    return create_state_storage("code", tmp_path / "team", HistoryScope(kind="team", scope_id="crew"))


def record_store_threads(monkeypatch: pytest.MonkeyPatch, *method_names: str) -> dict[str, list[str]]:
    """Record the name of the thread each call of the named ``ConversationStore`` methods runs on, by method.

    A database's lane runs its operations on a thread named ``conversation-store``.
    """
    threads: dict[str, list[str]] = {name: [] for name in method_names}

    def recording(real: Callable[..., Any], calls: list[str]) -> Callable[..., Any]:
        def recorded(self: "ConversationStore", *args: object, **kwargs: object) -> object:
            calls.append(threading.current_thread().name)
            return real(self, *args, **kwargs)

        return recorded

    for name in method_names:
        monkeypatch.setattr(ConversationStore, name, recording(getattr(ConversationStore, name), threads[name]))
    return threads


def seed_session_row(storage: "ConversationStore", session: "ConversationSession") -> None:
    """Create the session's row from its fields unless one exists, as the store's first write of a session does."""
    with storage.transaction(write=True) as connection:
        created = ensure_session_row(connection, storage.names, session)
    if created is not None:
        session.stored_session_state = created


def seed_session_state(storage: "ConversationStore", session: "ConversationSession", state: "dict[str, Any]") -> None:
    """Store ``state`` as the session row's ``session_state``, creating the row if needed, and leave the rest alone.

    No store method writes the state alone: a run save carries it. Tests stand in for another run's save with this.
    ``session.stored_session_state`` then records it as the row's, as the store's writes do, so ``state`` must not
    change afterwards.
    """
    with storage.transaction(write=True) as connection:
        ensure_session_row(connection, storage.names, session)
        _write_session_state(connection, storage.names, session.session_id, state)
    session.stored_session_state = state


def stored_session_state(storage: "ConversationStore", session_id: str) -> "dict[str, Any]":
    """Return the ``session_state`` the row of session ``session_id`` holds, as a load reads it, without its runs."""
    with storage.transaction(write=False) as connection:
        (session_state,) = connection.execute(
            f"SELECT session_state FROM {quote_identifier(storage.names.sessions)} WHERE session_id = ?",  # noqa: S608
            (session_id,),
        ).fetchone()
    return _session_state(session_state)


def seed_session(storage: "ConversationStore", session: "ConversationSession") -> "ConversationSession":
    """Create the session's row from its fields, then save every run it holds with ``save_run``, and return the session.

    Production code saves runs one at a time as they finish; tests seed whole histories with this instead. The runs
    are saved as conversation writes, so the usage ledger and session totals stay empty; ``persist_session`` seeds
    those too. A row that already exists keeps its fields; change them through the store's field writers.
    """
    seed_session_row(storage, session)
    for run in _in_save_order(session.runs):
        storage.save_run(session, run, record_usage=False)
    return session


def persist_session(storage: "ConversationStore", session: "ConversationSession") -> "ConversationSession":
    """Seed the session as ``seed_session`` does, but save each run as its turn's terminal record, usage included."""
    seed_session_row(storage, session)
    persist_runs(storage, session, *_in_save_order(session.runs))
    return session


def _in_save_order(runs: "Sequence[AnyRunRecord]") -> "list[AnyRunRecord]":
    """Return ``runs`` with each team run's members moved right before it, the order a team turn saves them in.

    Members finish before their team run, and a member saved after its finished team run is final, so it writes
    nothing. A member whose team run is not among ``runs`` keeps its place.
    """
    top_level_ids = {run.run_id for run in runs if run.parent_run_id is None}
    ordered: list[AnyRunRecord] = []
    for run in runs:
        if run.parent_run_id is None:
            ordered += [*(member for member in runs if member.parent_run_id == run.run_id), run]
        elif run.parent_run_id not in top_level_ids:
            ordered.append(run)
    return ordered


def persist_runs(storage: "ConversationStore", session: "ConversationSession", *runs: "AnyRunRecord") -> None:
    """Hand each run to ``persist_to`` in order, as a turn hands over its terminal record, recording its usage too.

    Only that save writes the usage ledger. The saves run on an event loop of their own, so sync and async tests
    alike can call this.
    """
    persist = persist_to(storage, session)

    async def persist_all() -> None:
        for run in runs:
            await persist(run)

    with ThreadPoolExecutor(max_workers=1) as pool:
        pool.submit(asyncio.run, persist_all()).result()


def delete_runs(
    storage: "ConversationStore",
    run_ids: Collection[str],
    *,
    session_id: str,
    session: "ConversationSession | None" = None,
) -> set[str]:
    """Delete the session's runs ``run_ids`` with their member runs and messages, as another writer's delete does.

    No summary row goes and no removed run is recorded, as when a run that holds a fold's row ends as no turn.
    ``session``, when given, then holds none of the runs; returns the deleted ids.
    """
    with storage.transaction(write=True) as connection:
        deleted = delete_runs_in_transaction(connection, storage.names, run_ids, session_id=session_id)
    if session is not None:
        session.runs = runs_without(session.runs, run_ids)
    return deleted


def seed_session_metrics(storage: "ConversationStore", session_id: str, totals: "Mapping[str, object]") -> None:
    """Write ``totals`` as the session row's stored usage totals, as an older release's run saves left them.

    The step to layout version 7 then keeps what they count beyond the session's run rows, as opening the database
    at this release does.
    """
    with storage.transaction(write=True) as connection:
        (raw,) = connection.execute(
            f"SELECT session_data FROM {quote_identifier(storage.names.sessions)} WHERE session_id = ?",  # noqa: S608
            (session_id,),
        ).fetchone()
        session_data = json.loads(raw) if raw else {}
        session_data["session_metrics"] = dict(totals)
        connection.execute(
            f"UPDATE {quote_identifier(storage.names.sessions)} SET session_data = ? WHERE session_id = ?",  # noqa: S608
            (json.dumps(session_data), session_id),
        )
        add_pre_ledger_baselines(connection, storage.names)


def create_agno_2_sessions_db(path: "Path") -> "Path":
    """Write the agno 2.6.12 ``code_sessions`` fixture database to ``path`` and return it."""
    path.parent.mkdir(parents=True, exist_ok=True)
    fixture = Path(__file__).parent / "fixtures" / "agno_2_6_12_code_sessions.sql"
    connection = sqlite3.connect(path)
    try:
        connection.executescript(fixture.read_text(encoding="utf-8"))
    finally:
        connection.close()
    return path
