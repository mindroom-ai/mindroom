"""Tests for per-thread model overrides (!model command and thread_model tool)."""

from __future__ import annotations

import json
import os
from dataclasses import replace
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import TYPE_CHECKING
from unittest.mock import AsyncMock, MagicMock

import nio
import pytest
from pydantic import ValidationError
from structlog.testing import capture_logs

import mindroom.tools  # noqa: F401
from mindroom import thread_models
from mindroom.ai_run_metadata import build_ai_run_metadata_content
from mindroom.coalescing.batch import requester_coalescing_key
from mindroom.commands.model_commands import handle_model_command
from mindroom.commands.parsing import CommandType, command_parser, get_command_help
from mindroom.config.access import ResponderAccessConfig
from mindroom.config.agent import AgentConfig, TeamConfig
from mindroom.config.budgets import BudgetsConfig
from mindroom.config.main import Config
from mindroom.config.models import ModelConfig, ModelPricing, ReasoningConfig
from mindroom.constants import AI_RUN_METADATA_KEY, FORK_CONTENT_KEY, ROUTE_EVENT_TYPE, ROUTER_AGENT_NAME
from mindroom.custom_tools.thread_model import ThreadModelTools
from mindroom.entity_resolution import entity_identity_registry
from mindroom.journal_dispatch import _route_as_message
from mindroom.llm.runs import RunStatus
from mindroom.matrix.users import AgentMatrixUser
from mindroom.message_target import MessageTarget
from mindroom.model_selection import THREAD_MODEL_CONTENT_KEY
from mindroom.thread_models import (
    _get_thread_model_override,
    _store_path,
    clear_thread_model_override,
    copy_thread_model_override,
    resolve_thread_model_override,
    set_thread_model_override,
)
from mindroom.tool_system.invocation import ToolCallContext, execute_tool_call
from mindroom.tool_system.metadata import get_tool_by_name
from mindroom.tool_system.output_files import OUTPUT_PATH_ARGUMENT
from mindroom.tool_system.registry import builtin_tool_registry
from mindroom.tool_system.runtime_context import ToolRuntimeContext, tool_runtime_context
from mindroom.tool_system.tool_function import ToolOutcome
from mindroom.tool_system.toolkits import adapt_toolkit
from mindroom.turn_model import ModelScope, resolve_turn_model
from mindroom.turn_record import TurnRecord
from tests.agent_tool_helpers import toolkit_offers
from tests.authorization_helpers import (
    isolated_membership_index,
    make_test_tool_runtime_context,
)
from tests.bot_helpers import _matrix_room, make_test_agent_bot, unique_room_send_responses
from tests.conftest import (
    TEST_PASSWORD,
    bind_runtime_paths,
    drain_coalescing,
    install_model_providers,
    make_conversation_reader_mock,
    make_matrix_client_mock,
    make_relation_lookup,
    make_visible_message,
    parsed_route,
    runtime_paths_for,
    test_runtime_paths,
)
from tests.fakes.scripted_model import ScriptedModel, answer
from tests.room_settings_helpers import set_room_model_override
from tests.runtime_fakes import ready_runtime
from tests.test_hook_sender import _agent_bot, _dispatch_context
from tests.test_model_selection_commands import OTHER, resolved_thread_models, restricted_config
from tests.test_model_selection_scope import ROOM, USER
from tests.threading_helpers import seed_hydrated_conversation
from tests.turn_dispatch_helpers import dispatch_test_turn, make_test_turn

if TYPE_CHECKING:
    from mindroom.bot import AgentBot
    from mindroom.constants import RuntimePaths
    from mindroom.tool_system.tool_function import ToolFunction
    from mindroom.turn_policy import PreparedDispatch

THREAD_ID = "$thread-root:localhost"
ROOM_ID = "!room:localhost"
# The router's own client, whose account and device a typed command's result names.
_ROUTER_CLIENT = MagicMock(spec=nio.AsyncClient, user_id="@mindroom_router:localhost", device_id="DEVICE")


def _stored_model(runtime_paths: RuntimePaths, thread_id: str | None) -> str | None:
    record = _get_thread_model_override(runtime_paths, thread_id)
    return None if record is None else record.get("model:test_agent") or None


def _config_with_models(tmp_path: Path) -> Config:
    return bind_runtime_paths(
        Config(
            agents={"test_agent": AgentConfig(display_name="Test Agent", model="default")},
            models={
                "default": ModelConfig(provider="openai", id="default-model", context_window=None),
                "large": ModelConfig(provider="openai", id="large-model", context_window=32_000),
            },
        ),
        test_runtime_paths(tmp_path),
    )


def test_store_roundtrip(tmp_path: Path) -> None:
    """Set, get, and clear should persist one override per thread root."""
    config = _config_with_models(tmp_path)
    runtime_paths = test_runtime_paths(tmp_path)

    assert _stored_model(runtime_paths, THREAD_ID) is None
    assert _stored_model(runtime_paths, None) is None

    set_thread_model_override(
        runtime_paths,
        thread_id=THREAD_ID,
        model_name="large",
        entity_names=("test_agent",),
        config=config,
    )
    assert _stored_model(runtime_paths, THREAD_ID) == "large"
    assert _stored_model(runtime_paths, "$other:localhost") is None

    assert clear_thread_model_override(runtime_paths, THREAD_ID, entity_names=("test_agent",), config=config) is True
    assert _stored_model(runtime_paths, THREAD_ID) is None
    assert clear_thread_model_override(runtime_paths, THREAD_ID, entity_names=("test_agent",), config=config) is False


@pytest.mark.parametrize(
    ("operations", "expected"),
    [
        (("apply", "apply-other"), {"test_agent": "large"}),
        (("apply", "reset", "apply"), {}),
        (("set", "apply"), {"test_agent": "default"}),
    ],
)
def test_root_thread_model_is_consumed_once(
    tmp_path: Path,
    operations: tuple[str, ...],
    expected: dict[str, str],
) -> None:
    """A replay cannot replace the first pick or undo a later command, even if reset found no selection."""
    config = _config_with_models(tmp_path)
    paths = runtime_paths_for(config)
    names = ("test_agent",)
    for operation in operations:
        if operation == "reset":
            clear_thread_model_override(paths, THREAD_ID, entity_names=names, config=config)
        elif operation == "set":
            set_thread_model_override(
                paths,
                thread_id=THREAD_ID,
                model_name="default",
                entity_names=names,
                config=config,
            )
        else:
            thread_models.apply_root_thread_model(
                paths,
                thread_id=THREAD_ID,
                root_timestamp_ms=datetime.now(UTC).timestamp() * 1000,
                model_name="large" if operation == "apply" else "default",
                entity_names=names,
            )
    assert resolve_thread_model_override(paths, THREAD_ID, config=config).active == expected


def test_opening_pick_preserves_an_existing_choice_for_one_responder(tmp_path: Path) -> None:
    """An existing choice stays in place; the pick applies to the other addressable responder."""
    config = _config_with_models(tmp_path)
    config.agents["other"] = AgentConfig(display_name="Other", model="default")
    paths = runtime_paths_for(config)
    set_thread_model_override(
        paths,
        thread_id=THREAD_ID,
        model_name="default",
        entity_names=("test_agent",),
        config=config,
    )
    thread_models.apply_root_thread_model(
        paths,
        thread_id=THREAD_ID,
        root_timestamp_ms=datetime.now(UTC).timestamp() * 1000,
        model_name="large",
        entity_names=("test_agent", "other"),
    )
    assert [
        resolve_turn_model(
            config,
            paths,
            ModelScope(entity=name, room_id=ROOM_ID, thread_id=THREAD_ID),
            None,
        ).model_name
        for name in ("test_agent", "other")
    ] == ["default", "large"]


@pytest.mark.parametrize("explicit_first", [False, True])
def test_opening_pick_cannot_expand_on_a_later_handling(tmp_path: Path, explicit_first: bool) -> None:
    """Freeze the first addressable set even if every entity already had an explicit choice."""
    config = _config_with_models(tmp_path)
    config.agents["other"] = AgentConfig(display_name="Other", model="default")
    paths = runtime_paths_for(config)
    if explicit_first:
        set_thread_model_override(
            paths,
            thread_id=THREAD_ID,
            model_name="default",
            entity_names=("test_agent",),
            config=config,
        )
    sent_at = datetime.now(UTC).timestamp() * 1000
    thread_models.apply_root_thread_model(
        paths,
        thread_id=THREAD_ID,
        root_timestamp_ms=sent_at,
        model_name="large",
        entity_names=("test_agent",),
    )
    assert resolve_thread_model_override(paths, THREAD_ID, config=config).active == {
        "test_agent": "default" if explicit_first else "large",
    }
    initial = _store_path(paths).read_bytes()
    thread_models.apply_root_thread_model(
        paths,
        thread_id=THREAD_ID,
        root_timestamp_ms=sent_at,
        model_name="large",
        entity_names=("test_agent", "other"),
    )
    assert _store_path(paths).read_bytes() == initial
    assert clear_thread_model_override(paths, THREAD_ID, entity_names=("test_agent",), config=config)
    thread_models.apply_root_thread_model(
        paths,
        thread_id=THREAD_ID,
        root_timestamp_ms=sent_at,
        model_name="large",
        entity_names=("test_agent", "other"),
    )
    assert resolve_thread_model_override(paths, THREAD_ID, config=config).active == {}


def test_empty_fork_snapshot_has_the_same_eviction_priority_as_other_thread_records(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A fresh snapshot with no inherited model must not be dropped just because the store is full."""
    config = _config_with_models(tmp_path)
    paths = runtime_paths_for(config)
    monkeypatch.setattr(thread_models, "_MAX_TRACKED_THREADS", 1)
    set_thread_model_override(
        paths,
        thread_id=THREAD_ID,
        model_name="large",
        entity_names=("test_agent",),
        config=config,
    )
    copy_thread_model_override(paths, "$empty-parent", "$empty-fork")
    assert _get_thread_model_override(paths, THREAD_ID) is None
    assert (_get_thread_model_override(paths, "$empty-fork") or {}).get("inherited_from") == "$empty-parent"


def test_old_opening_pick_cannot_reapply_after_eviction(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """The seven-day limit blocks old picks even after ordinary eviction forgets the marker."""
    config = _config_with_models(tmp_path)
    paths = runtime_paths_for(config)
    monkeypatch.setattr(thread_models, "_MAX_TRACKED_THREADS", 1)
    clock = MagicMock(wraps=datetime)
    now = datetime.now(UTC)
    clock.now.return_value = now
    monkeypatch.setattr(thread_models, "datetime", clock)
    names = ("test_agent",)

    def apply() -> None:
        thread_models.apply_root_thread_model(
            paths,
            thread_id=THREAD_ID,
            root_timestamp_ms=now.timestamp() * 1000,
            model_name="large",
            entity_names=names,
        )

    apply()
    assert clear_thread_model_override(paths, THREAD_ID, entity_names=names, config=config)
    clock.now.return_value = now + timedelta(days=7)
    set_thread_model_override(paths, thread_id="$newer", model_name="large", entity_names=names, config=config)
    assert _get_thread_model_override(paths, THREAD_ID) is None
    apply()
    assert _get_thread_model_override(paths, THREAD_ID) is None
    assert resolve_thread_model_override(paths, "$newer", config=config).active == {"test_agent": "large"}


def test_noop_store_clear_without_consuming_opening_does_not_create_a_record(tmp_path: Path) -> None:
    """A store-only clear does not reserve a slot or mark an opening consumed."""
    config = _config_with_models(tmp_path)
    paths = runtime_paths_for(config)
    assert not clear_thread_model_override(paths, THREAD_ID, entity_names=("test_agent",), config=config)
    assert not _store_path(paths).exists()


def test_copy_thread_model_override_copies_once_and_never_overwrites(tmp_path: Path) -> None:
    """A fork takes all its parent's selections once; its own selections and resets stay its own."""
    config = _config_with_models(tmp_path)
    runtime_paths = test_runtime_paths(tmp_path)
    fork_id = "$fork-root:localhost"

    def entity_models(thread_id: str) -> dict[str, str]:
        record = _get_thread_model_override(runtime_paths, thread_id) or {}
        return {field: value for field, value in record.items() if field.startswith("model:") and value}

    set_thread_model_override(
        runtime_paths,
        thread_id=THREAD_ID,
        model_name="large",
        entity_names=("test_agent", "router"),
        config=config,
    )
    copy_thread_model_override(runtime_paths, THREAD_ID, fork_id)
    assert entity_models(fork_id) == {"model:test_agent": "large", "model:router": "large"}

    set_thread_model_override(
        runtime_paths,
        thread_id=fork_id,
        model_name="default",
        entity_names=("test_agent",),
        config=config,
    )
    copy_thread_model_override(runtime_paths, THREAD_ID, fork_id)
    assert _stored_model(runtime_paths, fork_id) == "default"
    assert _stored_model(runtime_paths, THREAD_ID) == "large"

    clear_thread_model_override(runtime_paths, fork_id, entity_names=("test_agent", "router"), config=config)
    copy_thread_model_override(runtime_paths, THREAD_ID, fork_id)
    assert entity_models(fork_id) == {}

    # A marker-only reset before the fork opens must not count as an existing fork snapshot.
    reset_first = "$reset-first:localhost"
    assert not clear_thread_model_override(
        runtime_paths,
        reset_first,
        entity_names=("test_agent",),
        config=config,
        consume_opening=True,
    )
    copy_thread_model_override(runtime_paths, THREAD_ID, reset_first)
    assert entity_models(reset_first) == {"model:test_agent": "large", "model:router": "large"}


@pytest.mark.parametrize("mutation", ["set", "clear", "opening", "fork"])
def test_thread_model_writes_preserve_records_after_read_failure(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    mutation: str,
) -> None:
    """A read failure cannot poison the cache or authorize replacing other threads' records."""
    config = _config_with_models(tmp_path)
    paths = runtime_paths_for(config)
    names = ("test_agent",)
    set_thread_model_override(
        paths,
        thread_id=THREAD_ID,
        model_name="large",
        entity_names=names,
        config=config,
        consume_opening=True,
    )
    store = _store_path(paths)
    before = store.read_bytes()
    read_text = Path.read_text
    unreadable = True

    def read(path: Path, encoding: str | None = None, errors: str | None = None, newline: str | None = None) -> str:
        if path == store and unreadable:
            raise PermissionError
        return read_text(path, encoding=encoding, errors=errors, newline=newline)

    monkeypatch.setattr(Path, "read_text", read)
    assert _get_thread_model_override(paths, THREAD_ID) is None
    unreadable = False
    assert _stored_model(paths, THREAD_ID) == "large"  # Unchanged mtime; a failed read must not be cached.

    def change_other_thread() -> None:
        if mutation == "set":
            set_thread_model_override(paths, thread_id="$new", model_name="default", entity_names=names, config=config)
        elif mutation == "clear":
            clear_thread_model_override(paths, "$new", entity_names=names, config=config, consume_opening=True)
        elif mutation == "opening":
            thread_models.apply_root_thread_model(
                paths,
                thread_id="$new",
                root_timestamp_ms=datetime.now(UTC).timestamp() * 1000,
                model_name="default",
                entity_names=names,
            )
        else:
            copy_thread_model_override(paths, THREAD_ID, "$new")

    unreadable = True  # Even a warm cache cannot make the existing file safe to replace.
    with pytest.raises(OSError, match="Could not read the override store"):
        change_other_thread()
    assert store.read_bytes() == before
    unreadable = False
    change_other_thread()
    records = json.loads(store.read_bytes())
    assert records[THREAD_ID] == json.loads(before)[THREAD_ID]
    assert records[THREAD_ID]["opening_model_consumed"] == "1"
    assert "$new" in records


_FORK_ID = "$fork-root:localhost"


def _fork_bot(tmp_path: Path, agent_name: str = "code") -> AgentBot:
    """Return a bot whose config offers the ``large`` model a parent thread may select."""
    bot = _agent_bot(tmp_path, agent_name=agent_name)
    bot.config.models["large"] = ModelConfig(provider="openai", id="large-model")
    return bot


async def _dispatch_fork_turn(
    bot: AgentBot,
    source_event_ids: tuple[str, ...] = (_FORK_ID,),
    *,
    pick: str | None = None,
    source_room_id: str | None = None,
) -> PreparedDispatch | None:
    """Prepare one turn of the fork ``_FORK_ID`` of ``THREAD_ID`` whose sources are ``source_event_ids``."""
    room = nio.MatrixRoom(room_id=ROOM_ID, own_user_id="@mindroom_code:localhost")
    event = nio.RoomMessageText.from_dict(
        {
            "event_id": source_event_ids[-1],
            "sender": "@user:localhost",
            "origin_server_ts": int(datetime.now(UTC).timestamp() * 1000),
            "content": {"msgtype": "m.text", "body": "@mindroom_code:localhost carry on"},
        },
    )
    if pick is not None:
        _join_model_test_bot(bot, room)
        event.source["content"][THREAD_MODEL_CONTENT_KEY] = {
            "version": 1,
            "runtime_user_id": bot.matrix_id.full_id,
            "runtime_device_id": "TESTDEVICE",
            "model": pick,
        }
    fork_ref: dict[str, object] = {"thread_id": THREAD_ID, "event_id": "$parent-reply:localhost"}
    if source_room_id is not None:
        fork_ref["room_id"] = source_room_id
    fork_root = make_visible_message(
        event_id=_FORK_ID,
        content={"body": "carry on", FORK_CONTENT_KEY: fork_ref},
    )
    context = replace(_dispatch_context(bot), is_thread=True, thread_id=_FORK_ID, thread_history=[fork_root])
    bot._conversation_resolver.extract_dispatch_context = AsyncMock(return_value=context)
    turn = make_test_turn(
        room,
        event,
        "@user:localhost",
        coalescing_key=requester_coalescing_key(ROOM_ID, _FORK_ID, "@user:localhost"),
        handled_turn=TurnRecord.create(list(source_event_ids)),
    )
    return await bot._turn_dispatcher._prepare_dispatch(turn)


def _fork_model(bot: AgentBot) -> str:
    """Return the model the ``code`` agent's next turn in the fork resolves."""
    scope = ModelScope(entity="code", room_id=ROOM_ID, thread_id=_FORK_ID)
    return resolve_turn_model(bot.config, bot._turn_dispatcher.deps.runtime_paths, scope, None).model_name


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("source_event_ids", "pick", "inherited"),
    [
        ((_FORK_ID,), None, "large"),
        ((_FORK_ID, "$fork-next:localhost"), None, "large"),
        (("$fork-next:localhost",), None, "default"),
        ((_FORK_ID,), "default", "default"),
        ((_FORK_ID,), "unknown", "large"),
    ],
    ids=["first-message", "coalesced-first-messages", "later-message", "explicit-pick", "invalid-pick"],
)
@pytest.mark.usefixtures("enforce_turn_authorization")
async def test_a_forks_first_turn_inherits_the_parent_thread_model_before_dispatch(
    tmp_path: Path,
    source_event_ids: tuple[str, ...],
    pick: str | None,
    inherited: str,
) -> None:
    """A fork's first turn inherits the parent model unless it has a valid pick of its own."""
    bot = _fork_bot(tmp_path)
    bot.config.agents["code"].access = ResponderAccessConfig(users=["@user:localhost"])
    bot.config.router.access = ResponderAccessConfig(current_room_members=False)
    set_thread_model_override(
        bot._turn_dispatcher.deps.runtime_paths,
        thread_id=THREAD_ID,
        model_name="large",
        entity_names=("code", "router"),
        config=bot.config,
    )

    await _dispatch_fork_turn(bot, source_event_ids, pick=pick)

    assert _fork_model(bot) == inherited
    router = resolve_turn_model(
        bot.config,
        runtime_paths_for(bot.config),
        ModelScope(entity="router", room_id=ROOM_ID, thread_id=_FORK_ID),
        None,
    )
    assert router.model_name == ("large" if _FORK_ID in source_event_ids else "default")


@pytest.mark.asyncio
@pytest.mark.parametrize("pick", [None, "default"])
@pytest.mark.usefixtures("enforce_turn_authorization")
async def test_fork_model_is_ready_before_a_hook_suppresses_the_opener(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    pick: str | None,
) -> None:
    """A hook can suppress the reply without making parent inheritance beat an opening pick."""
    bot = _fork_bot(tmp_path)
    bot.config.agents["code"].access = ResponderAccessConfig(users=["@user:localhost"])
    paths = runtime_paths_for(bot.config)
    set_thread_model_override(paths, thread_id=THREAD_ID, model_name="large", entity_names=("code",), config=bot.config)
    expected = pick or "large"

    async def suppress(**_kwargs: object) -> bool:
        assert resolve_thread_model_override(paths, _FORK_ID, config=bot.config).active["code"] == expected
        return True

    hooks = AsyncMock(side_effect=suppress)
    monkeypatch.setattr(bot._turn_dispatcher.deps.ingress_hook_runner, "emit_message_received_hooks", hooks)
    assert await _dispatch_fork_turn(bot, pick=pick) is None
    hooks.assert_awaited_once()
    hooks.side_effect = None
    hooks.return_value = False
    child = await _dispatch_fork_turn(bot, ("$next:localhost",))
    assert child is not None
    assert child.target.resolved_thread_id == _FORK_ID
    assert _fork_model(bot) == expected


def _join_model_test_bot(bot: AgentBot, room: nio.MatrixRoom, device_id: str = "TESTDEVICE") -> AsyncMock:
    """Attach a logged-in client so the real room-poster election sees this bot."""
    client = make_matrix_client_mock(user_id=bot.matrix_id.full_id)
    client.device_id = device_id
    client.rooms[room.room_id] = room
    bot.client = client
    bot.running = True
    return client


def _conversation_event(bot: AgentBot, event_id: str, body: str, **content: object) -> nio.RoomMessageText:
    """Build one requester prompt aimed at the bot, with optional composer metadata."""
    return nio.RoomMessageText.from_dict(
        {
            "type": "m.room.message",
            "event_id": event_id,
            "sender": "@user:localhost",
            "origin_server_ts": int(datetime.now(UTC).timestamp() * 1000),
            "content": {
                "msgtype": "m.text",
                "body": body,
                "m.mentions": {"user_ids": [bot.matrix_id.full_id]},
                **content,
            },
        },
    )


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("commands", "expected"),
    [(("manual",), "manual"), (("reset",), "default"), (("manual", "reset"), "default")],
    ids=["set", "reset-before-any-choice", "set-then-reset"],
)
@pytest.mark.usefixtures("enforce_turn_authorization")
async def test_typed_model_command_consumes_a_delayed_opening_pick(
    tmp_path: Path,
    commands: tuple[str, ...],
    expected: str,
) -> None:
    """A handled set or reset prevents an older pick for the entire thread, not just the command's scope."""
    bot = _fork_bot(tmp_path)
    config = bot.config
    config.models["manual"] = ModelConfig(provider="openai", id="manual-model")
    opener, setter = "@user:localhost", "@setter:localhost"
    config.agents["code"].access = ResponderAccessConfig(users=[opener, setter])
    config.router.access = ResponderAccessConfig(users=[opener], current_room_members=False)
    deps = bot._turn_dispatcher.deps
    room = _matrix_room(ROOM_ID, bot.matrix_id.full_id, user_ids=(bot.matrix_id.full_id, opener, setter))
    client = _join_model_test_bot(bot, room)
    event = _conversation_event(
        bot,
        "$opening",
        "First prompt",
        **{
            THREAD_MODEL_CONTENT_KEY: {
                "version": 1,
                "runtime_user_id": client.user_id,
                "runtime_device_id": client.device_id,
                "model": "large",
            },
        },
    )
    for index, command in enumerate(commands):
        handle_model_command(
            command,
            client=client,
            config=config,
            runtime_paths=deps.runtime_paths,
            membership_index=deps.runtime.agent_reply_memberships,
            room_id=ROOM_ID,
            thread_id=event.event_id,
            requester_user_id=setter,
            command_event_id=f"$command-{index}",
        )

    prepared = await bot._turn_dispatcher._prepare_dispatch(make_test_turn(room, event, opener))
    assert prepared is not None
    for entity, model in (("code", expected), (ROUTER_AGENT_NAME, "default")):
        scope = ModelScope(entity=entity, room_id=ROOM_ID, thread_id=prepared.target.resolved_thread_id)
        assert resolve_turn_model(config, deps.runtime_paths, scope, None).model_name == model


@pytest.mark.asyncio
@pytest.mark.usefixtures("enforce_turn_authorization")
async def test_opening_pick_with_unavailable_storage_answers_on_the_inherited_model(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Failing to save a pick must not refuse the turn or skip ordinary model inheritance."""
    bot = _fork_bot(tmp_path)
    bot.config.agents["code"].access = ResponderAccessConfig(users=["@user:localhost"])
    bot.config.agents["code"].memory_backend = "none"
    room = _matrix_room(ROOM_ID, bot.matrix_id.full_id, user_ids=(bot.matrix_id.full_id, "@user:localhost"))
    client = _join_model_test_bot(bot, room)
    unique_room_send_responses(client)
    paths = runtime_paths_for(bot.config)
    bot.config.models["room_model"] = ModelConfig(provider="openai", id="room-model")
    set_room_model_override(paths, room_id=ROOM_ID, model_name="room_model")
    store = _store_path(paths)
    store.parent.mkdir(parents=True, exist_ok=True)
    store.write_text("not json", encoding="utf-8")
    inherited = ScriptedModel([answer("Inherited answer")])
    install_model_providers(monkeypatch, {"room_model": inherited})
    opening = _conversation_event(
        bot,
        "$opening",
        "First prompt",
        **{
            THREAD_MODEL_CONTENT_KEY: {
                "version": 1,
                "runtime_user_id": client.user_id,
                "runtime_device_id": client.device_id,
                "model": "large",
            },
        },
    )
    with capture_logs() as logs:
        await dispatch_test_turn(bot._turn_dispatcher, room, opening, opening.sender)
        await drain_coalescing(bot)

    assert len(inherited.requests) == 1
    bodies = [call.kwargs["content"].get("body", "") for call in client.room_send.await_args_list]
    assert any("Inherited answer" in body for body in bodies)
    assert not any("storage is unavailable" in body for body in bodies)
    assert [entry["log_level"] for entry in logs if entry["event"] == "Opening model storage failed"] == ["warning"]
    assert store.read_text(encoding="utf-8") == "not json"


@pytest.mark.asyncio
@pytest.mark.parametrize("is_bridge", [False, True], ids=["human", "permitted-bridge"])
@pytest.mark.usefixtures("enforce_turn_authorization")
async def test_opening_pick_drives_the_reply_model_and_stays_in_its_thread(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    is_bridge: bool,
) -> None:
    """Use the picked provider on the first reply and next; reset and new roots use the inherited provider."""
    bot = _fork_bot(tmp_path)
    if is_bridge:
        # A configured bridge with a reply grant can also issue the typed !model reset below.
        bot.config.bot_accounts = ["@user:localhost"]
    bot.config.agents["code"].access = ResponderAccessConfig(users=["@user:localhost"])
    bot.config.agents["code"].memory_backend = "none"
    bot.config.router.access = ResponderAccessConfig(users=["@someone_else:localhost"], current_room_members=False)
    paths = runtime_paths_for(bot.config)
    catalog_account = bot.matrix_id.full_id
    room = _matrix_room(ROOM_ID, catalog_account, user_ids=(catalog_account, "@user:localhost"))
    client = _join_model_test_bot(bot, room)
    unique_room_send_responses(client)
    picked = ScriptedModel([answer("Picked answer"), answer("Picked follow-up")])
    inherited = ScriptedModel([answer("Inherited answer"), answer("Inherited after reset")])
    bot.config.models["large"].reasoning = ReasoningConfig(levels=["low", "high"], default="low")
    install_model_providers(monkeypatch, {"default": inherited, "large@high": picked})
    root = _conversation_event(
        bot,
        "$opening",
        "First prompt",
        **{
            THREAD_MODEL_CONTENT_KEY: {
                "version": 1,
                "runtime_user_id": catalog_account,
                "runtime_device_id": "TESTDEVICE",
                "model": "large@high",
            },
        },
    )
    relation = {"m.relates_to": {"rel_type": "m.thread", "event_id": root.event_id}}
    await seed_hydrated_conversation(bot, room_id=ROOM_ID, thread_id=root.event_id)
    dispatcher = bot._turn_dispatcher

    async def send(event: nio.RoomMessageText) -> None:
        await dispatch_test_turn(dispatcher, room, event, event.sender)
        await drain_coalescing(bot)

    await send(root)
    assert (len(picked.requests), len(inherited.requests)) == (1, 0)
    assert resolve_thread_model_override(paths, root.event_id, config=bot.config).active == {"code": "large@high"}
    await send(_conversation_event(bot, "$follow-up", "More in this thread", **relation))
    assert (len(picked.requests), len(inherited.requests)) == (2, 0)
    await send(_conversation_event(bot, "$new-root", "New topic"))
    assert (len(picked.requests), len(inherited.requests)) == (2, 1)
    await send(_conversation_event(bot, "$reset", "!model reset", **relation))
    assert not resolve_thread_model_override(paths, root.event_id, config=bot.config).active
    # Simulate a delayed responder preparing the original root after the reset.
    await dispatcher._prepare_dispatch(make_test_turn(room, root, root.sender))
    await send(_conversation_event(bot, "$after-reset", "Follow-up after reset", **relation))
    assert (len(picked.requests), len(inherited.requests)) == (2, 2)


@pytest.mark.asyncio
@pytest.mark.parametrize(("target_name", "re_elect"), [("helper", False), ("crew", False), ("helper", True)])
@pytest.mark.usefixtures("enforce_turn_authorization")
async def test_route_and_first_reply_use_the_opening_model(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    target_name: str,
    re_elect: bool,
) -> None:
    """The steward routes on the pick even after poster re-election, and the agent or team answers on it."""
    sender = "@user:localhost"
    grant = ResponderAccessConfig(users=[sender])
    paths = test_runtime_paths(tmp_path)
    config = bind_runtime_paths(
        Config(
            agents={
                name: AgentConfig(display_name=name, access=grant, memory_backend="none") for name in ("helper", "docs")
            },
            teams={"crew": TeamConfig(display_name="Crew", role="Help", agents=["helper", "docs"], access=grant)},
            models={name: ModelConfig(provider="openai", id=name) for name in ("default", "large")},
        ),
        paths,
    )
    config.router.access = grant
    runtime = ready_runtime(config, paths)
    registry = entity_identity_registry(config, paths)
    user_ids = tuple(registry.current_id(name).full_id for name in (ROUTER_AGENT_NAME, "helper", "docs", "crew"))
    room = _matrix_room(ROOM_ID, user_ids[0], user_ids=(*user_ids, sender))

    def make_bot(name: str, device_id: str) -> tuple[AgentBot, AsyncMock]:
        bot = make_test_agent_bot(
            AgentMatrixUser(
                agent_name=name,
                display_name=name,
                user_id=registry.current_id(name).full_id,
                password=TEST_PASSWORD,
            ),
            orchestrator=runtime,
        )
        client = _join_model_test_bot(bot, room, device_id)
        unique_room_send_responses(client, prefix=f"${name}-sent")
        return bot, client

    responder, client = make_bot(target_name, "RESPONDER")
    assert runtime.room_poster(ROOM_ID) is responder
    router, router_client = make_bot(ROUTER_AGENT_NAME, "CATALOG")
    assert runtime.room_poster(ROOM_ID) is router
    catalog = client if re_elect else router_client
    selection = answer(json.dumps({"entity_name": target_name, "reasoning": "Matches the prompt"}))
    picked = ScriptedModel([selection, answer("Picked response")])
    inherited = ScriptedModel([selection, answer("Wrong response")])
    install_model_providers(monkeypatch, {"large": picked, "default": inherited})
    root = _conversation_event(
        router,
        "$opening:localhost",
        "Please advise",
        **{
            THREAD_MODEL_CONTENT_KEY: {
                "version": 1,
                "runtime_user_id": catalog.user_id,
                "runtime_device_id": catalog.device_id,
                "model": "large",
            },
        },
    )
    root.source["content"].pop("m.mentions")
    await dispatch_test_turn(router._turn_dispatcher, room, root, sender)
    await drain_coalescing(router)
    assert (len(picked.requests), len(inherited.requests)) == (1, 0)
    (route,) = [
        call.kwargs["content"]
        for call in router_client.room_send.await_args_list
        if call.kwargs["message_type"] == ROUTE_EVENT_TYPE
    ]
    assert route[ROUTE_EVENT_TYPE]["entity"] == client.user_id

    # Deliver only the routed handoff: the responder must not need its own opening-event dispatch.
    handoff = _route_as_message(
        parsed_route(
            route,
            sender=router_client.user_id,
            event_id="$routed:localhost",
            room_id=ROOM_ID,
            origin_server_ts=root.server_timestamp + 1,
        ),
    )
    await seed_hydrated_conversation(responder, room_id=ROOM_ID, thread_id=root.event_id)
    deps = responder._turn_dispatcher.deps
    await dispatch_test_turn(
        responder._turn_dispatcher,
        room,
        handoff,
        facts=deps.ingress.classify(handoff, room_id=ROOM_ID),
    )
    await drain_coalescing(responder)
    assert (len(picked.requests), len(inherited.requests)) == (2, 0)
    assert any("Picked response" in send.kwargs["content"].get("body", "") for send in client.room_send.await_args_list)


@pytest.mark.asyncio
async def test_opening_pick_names_one_of_two_devices_on_the_same_runtime_account(tmp_path: Path) -> None:
    """Only the selected device of the shared catalog account can write to its runtime's store."""
    stores = []
    for device_id in ("NOT_PICKED", "PICKED"):
        bot = _fork_bot(tmp_path / device_id)
        bot.config.agents["code"].access = ResponderAccessConfig(users=["@user:localhost"])
        room = _matrix_room(ROOM_ID, bot.matrix_id.full_id, user_ids=(bot.matrix_id.full_id, "@user:localhost"))
        client = _join_model_test_bot(bot, room, device_id)
        event = _conversation_event(
            bot,
            "$root",
            "A prompt",
            **{
                THREAD_MODEL_CONTENT_KEY: {
                    "version": 1,
                    "runtime_user_id": client.user_id,
                    "runtime_device_id": "PICKED",
                    "model": "large",
                },
            },
        )
        with capture_logs() as logs:
            await bot._turn_dispatcher._prepare_dispatch(make_test_turn(room, event, event.sender))
        ignored = [entry["log_level"] for entry in logs if entry.get("reason") == "other_runtime_device"]
        assert ignored == (["info"] if device_id == "NOT_PICKED" else [])
        stores.append(
            resolve_thread_model_override(runtime_paths_for(bot.config), event.event_id, config=bot.config).active,
        )
    assert stores == [{}, {"code": "large", "router": "large"}]


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "ignored",
    [
        "reply",
        "room-mode",
        "other-runtime",
        "unknown-model",
        "unknown-level",
        "managed-sender",
        "no-access",
        "command",
    ],
)
@pytest.mark.usefixtures("enforce_turn_authorization")
async def test_ineligible_opening_model_pick_does_not_write(tmp_path: Path, ignored: str) -> None:
    """Only new threads, valid choices, a runtime's own devices, and the sender's reply grants can seed the store."""
    bot = _fork_bot(tmp_path)
    bot.config.agents["code"].access = ResponderAccessConfig(
        users=["@someone_else:localhost" if ignored == "no-access" else "@user:localhost"],
    )
    room = _matrix_room(ROOM_ID, bot.matrix_id.full_id, user_ids=(bot.matrix_id.full_id, "@user:localhost"))
    if ignored == "room-mode":
        bot.config.agents["code"].thread_mode = "room"
    model = {"unknown-model": "unconfigured", "unknown-level": "large@high"}.get(ignored, "large")
    runtime = "@other_runtime:localhost" if ignored == "other-runtime" else bot.matrix_id.full_id
    _join_model_test_bot(bot, room)
    content = {
        THREAD_MODEL_CONTENT_KEY: {
            "version": 1,
            "runtime_user_id": runtime,
            "runtime_device_id": "TESTDEVICE",
            "model": model,
        },
    }
    if ignored == "reply":
        content["m.relates_to"] = {"rel_type": "m.thread", "event_id": "$existing"}
        await seed_hydrated_conversation(bot, room_id=ROOM_ID, thread_id="$existing")
    event = _conversation_event(bot, "$event", "!model" if ignored == "command" else "A prompt", **content)
    dispatcher = bot._turn_dispatcher
    deps = dispatcher.deps
    if ignored == "no-access":
        bot.config.router.access = ResponderAccessConfig(current_room_members=False)
    if ignored == "managed-sender":
        event.sender = "@mindroom_router:localhost"
    facts = deps.ingress.classify(event, room_id=ROOM_ID)
    await bot._turn_dispatcher._prepare_dispatch(make_test_turn(room, event, facts=facts))

    assert not _store_path(deps.runtime_paths).exists()


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("parent_has_override", "fork_has_override"),
    [(True, False), (False, False), (True, True)],
    ids=["fork-reset", "parent-changed-later", "fork-selected-first-then-reset"],
)
async def test_a_delayed_responder_never_copies_the_parent_model_again(
    tmp_path: Path,
    parent_has_override: bool,
    fork_has_override: bool,
) -> None:
    """The fork's snapshot is taken once: a second responder of its first turn neither undoes a reset nor takes a later parent change."""
    first, delayed = _fork_bot(tmp_path), _fork_bot(tmp_path, agent_name="router")
    runtime_paths = first._turn_dispatcher.deps.runtime_paths
    if fork_has_override:
        # The fork's own selection, made before its first responder runs, is not replaced by the parent's.
        set_thread_model_override(
            runtime_paths,
            thread_id=_FORK_ID,
            model_name="default",
            entity_names=("code",),
            config=first.config,
        )

    def select_large_in_parent() -> None:
        set_thread_model_override(
            runtime_paths,
            thread_id=THREAD_ID,
            model_name="large",
            entity_names=("code",),
            config=first.config,
        )

    if parent_has_override:
        select_large_in_parent()
    await _dispatch_fork_turn(first)
    if parent_has_override:
        assert _fork_model(first) == ("default" if fork_has_override else "large")
        assert clear_thread_model_override(runtime_paths, _FORK_ID, entity_names=("code",), config=first.config)
    else:
        select_large_in_parent()

    await _dispatch_fork_turn(delayed)

    assert _fork_model(first) == "default"


def test_corrupt_store_reads_as_empty_but_refuses_a_write(tmp_path: Path) -> None:
    """Broken JSON is not an empty store a mutation can replace."""
    config = _config_with_models(tmp_path)
    paths = runtime_paths_for(config)
    path = _store_path(paths)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("not json", encoding="utf-8")

    assert _stored_model(paths, THREAD_ID) is None
    with pytest.raises(OSError, match="Could not read the override store"):
        set_thread_model_override(
            paths,
            thread_id=THREAD_ID,
            model_name="large",
            entity_names=("test_agent",),
            config=config,
        )
    assert path.read_text(encoding="utf-8") == "not json"


def test_store_prunes_oldest_entries(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """The store should keep only the newest overrides past the cap."""
    config = _config_with_models(tmp_path)
    runtime_paths = test_runtime_paths(tmp_path)
    monkeypatch.setattr("mindroom.thread_models._MAX_TRACKED_THREADS", 3)

    for index in range(5):
        set_thread_model_override(
            runtime_paths,
            thread_id=f"$thread-{index}:localhost",
            model_name="large",
            entity_names=("test_agent",),
            config=config,
        )

    assert [_stored_model(runtime_paths, f"$thread-{index}:localhost") for index in range(5)] == [
        None,
        None,
        "large",
        "large",
        "large",
    ]


def test_store_invalidates_cached_records_when_mtime_changes(tmp_path: Path) -> None:
    """An external file replacement should invalidate the parsed-record cache."""
    config = _config_with_models(tmp_path)
    runtime_paths = test_runtime_paths(tmp_path)
    path = _store_path(runtime_paths)
    set_thread_model_override(
        runtime_paths,
        thread_id=THREAD_ID,
        model_name="large",
        entity_names=("test_agent",),
        config=config,
    )
    assert _stored_model(runtime_paths, THREAD_ID) == "large"

    path.write_text(
        json.dumps({THREAD_ID: {"model:test_agent": "default", "set_at": "2026-07-09T00:00:00+00:00"}}),
        encoding="utf-8",
    )
    stat = path.stat()
    os.utime(path, ns=(stat.st_atime_ns, stat.st_mtime_ns + 1_000_000))

    assert _stored_model(runtime_paths, THREAD_ID) == "default"


def test_resolve_turn_model_prefers_thread_override(tmp_path: Path) -> None:
    """A thread override should beat the agent's authored model."""
    config = _config_with_models(tmp_path)
    runtime_paths = runtime_paths_for(config)
    set_thread_model_override(
        runtime_paths,
        thread_id=THREAD_ID,
        model_name="large",
        entity_names=("test_agent",),
        config=config,
    )

    runtime_model = resolve_turn_model(
        config,
        runtime_paths,
        ModelScope(entity="test_agent", room_id=ROOM_ID, thread_id=THREAD_ID),
        None,
    )

    assert runtime_model.model_name == "large"
    assert runtime_model.context_window == 32_000


def test_resolve_turn_model_thread_override_beats_room_override(
    tmp_path: Path,
) -> None:
    """A thread override should beat the room_models override."""
    config = bind_runtime_paths(
        Config(
            agents={"test_agent": AgentConfig(display_name="Test Agent", model="default")},
            room_models={ROOM_ID: "default"},
            models={
                "default": ModelConfig(provider="openai", id="default-model", context_window=None),
                "large": ModelConfig(provider="openai", id="large-model", context_window=32_000),
            },
        ),
        test_runtime_paths(tmp_path),
    )
    runtime_paths = runtime_paths_for(config)
    set_thread_model_override(
        runtime_paths,
        thread_id=THREAD_ID,
        model_name="large",
        entity_names=("test_agent",),
        config=config,
    )

    runtime_model = resolve_turn_model(
        config,
        runtime_paths,
        ModelScope(entity="test_agent", room_id=ROOM_ID, thread_id=THREAD_ID),
        None,
    )

    assert runtime_model.model_name == "large"


def test_resolve_turn_model_active_model_beats_thread_override(tmp_path: Path) -> None:
    """An explicit active model should beat the thread override."""
    config = _config_with_models(tmp_path)
    runtime_paths = runtime_paths_for(config)
    set_thread_model_override(
        runtime_paths,
        thread_id=THREAD_ID,
        model_name="large",
        entity_names=("test_agent",),
        config=config,
    )

    runtime_model = resolve_turn_model(
        config,
        runtime_paths,
        ModelScope(entity="test_agent", room_id=ROOM_ID, thread_id=THREAD_ID, explicit="default"),
        None,
    )

    assert runtime_model.model_name == "default"


def test_resolve_turn_model_ignores_stale_thread_override(tmp_path: Path) -> None:
    """An override naming a removed model should fall back to authored config."""
    config = _config_with_models(tmp_path)
    runtime_paths = runtime_paths_for(config)
    set_thread_model_override(
        runtime_paths,
        thread_id=THREAD_ID,
        model_name="removed-model",
        entity_names=("test_agent",),
        config=config,
    )

    runtime_model = resolve_turn_model(
        config,
        runtime_paths,
        ModelScope(entity="test_agent", room_id=ROOM_ID, thread_id=THREAD_ID),
        None,
    )

    assert runtime_model.model_name == "default"


def test_resolve_turn_model_ignores_a_thread_override_at_a_level_the_model_no_longer_lists(tmp_path: Path) -> None:
    """A stored ``name@level`` whose level was removed is stale like a removed model; a listed one applies."""
    config = _config_with_models(tmp_path)
    config.models["default"] = config.models["default"].model_copy(
        update={"reasoning": ReasoningConfig(levels=["low", "high"], default="low")},
    )
    runtime_paths = runtime_paths_for(config)
    scope = ModelScope(entity="test_agent", room_id=ROOM_ID, thread_id=THREAD_ID)
    set_thread_model_override(
        runtime_paths,
        thread_id=THREAD_ID,
        model_name="default@high",
        entity_names=("test_agent",),
        config=config,
    )
    assert resolve_turn_model(config, runtime_paths, scope, None).model_name == "default@high"

    config.models["default"] = config.models["default"].model_copy(
        update={"reasoning": ReasoningConfig(levels=["low"], default="low")},
    )

    assert resolve_turn_model(config, runtime_paths, scope, None).model_name == "default"
    override = resolve_thread_model_override(runtime_paths, THREAD_ID, config=config)
    assert override.stale == {"test_agent": "default@high"}


def test_resolve_turn_model_without_thread_id_keeps_authored_model(tmp_path: Path) -> None:
    """Resolution without thread context should ignore thread overrides."""
    config = _config_with_models(tmp_path)
    runtime_paths = runtime_paths_for(config)
    set_thread_model_override(
        runtime_paths,
        thread_id=THREAD_ID,
        model_name="large",
        entity_names=("test_agent",),
        config=config,
    )

    runtime_model = resolve_turn_model(config, runtime_paths, ModelScope(entity="test_agent", room_id=ROOM_ID), None)

    assert runtime_model.model_name == "default"


def test_model_command_parsing() -> None:
    """The parser should recognize !model with and without arguments."""
    command = command_parser.parse("!model")
    assert command is not None
    assert command.type == CommandType.MODEL
    assert command.args["args_text"] == ""

    command = command_parser.parse("!model large")
    assert command is not None
    assert command.type == CommandType.MODEL
    assert command.args["args_text"] == "large"

    command = command_parser.parse("!model reset")
    assert command is not None
    assert command.type == CommandType.MODEL
    assert command.args["args_text"] == "reset"


def test_model_command_set_and_reset(tmp_path: Path) -> None:
    """!model <name> should persist and !model reset should clear the override."""
    config = _config_with_models(tmp_path)
    runtime_paths = runtime_paths_for(config)

    response, _ = handle_model_command(
        "large",
        config=config,
        runtime_paths=runtime_paths,
        membership_index=isolated_membership_index(),
        room_id=ROOM_ID,
        thread_id=THREAD_ID,
        requester_user_id="@user:localhost",
        client=_ROUTER_CLIENT,
        command_event_id="$command",
    )
    assert "✅" in response
    assert "`large`" in response
    assert "room-level model selection" in response
    assert _stored_model(runtime_paths, THREAD_ID) == "large"

    set_room_model_override(
        runtime_paths,
        room_id=ROOM_ID,
        model_name="large",
    )

    response, _ = handle_model_command(
        "reset",
        config=config,
        runtime_paths=runtime_paths,
        membership_index=isolated_membership_index(),
        room_id=ROOM_ID,
        thread_id=THREAD_ID,
        requester_user_id="@user:localhost",
        client=_ROUTER_CLIENT,
        command_event_id="$command",
    )
    assert "✅" in response
    assert "room-level model selection" in response
    assert _stored_model(runtime_paths, THREAD_ID) is None
    assert (
        resolve_turn_model(
            config,
            runtime_paths,
            ModelScope(entity="test_agent", room_id=ROOM_ID, thread_id=THREAD_ID),
            None,
        ).model_name
        == "large"
    )

    response, _ = handle_model_command(
        "reset",
        config=config,
        runtime_paths=runtime_paths,
        membership_index=isolated_membership_index(),
        room_id=ROOM_ID,
        thread_id=THREAD_ID,
        requester_user_id="@user:localhost",
        client=_ROUTER_CLIENT,
        command_event_id="$command",
    )
    assert "no model override" in response


def test_model_command_show(tmp_path: Path) -> None:
    """Bare !model should report the current override and available models."""
    config = _config_with_models(tmp_path)
    runtime_paths = runtime_paths_for(config)

    response, _ = handle_model_command(
        "",
        config=config,
        runtime_paths=runtime_paths,
        membership_index=isolated_membership_index(),
        room_id=ROOM_ID,
        thread_id=THREAD_ID,
        requester_user_id="@user:localhost",
        client=_ROUTER_CLIENT,
        command_event_id="$command",
    )
    assert "No thread model override" in response
    assert "room-level model selection" in response
    assert "`default`" in response
    assert "`large`" in response

    set_thread_model_override(
        runtime_paths,
        thread_id=THREAD_ID,
        model_name="large",
        entity_names=("test_agent",),
        config=config,
    )
    response, _ = handle_model_command(
        "",
        config=config,
        runtime_paths=runtime_paths,
        membership_index=isolated_membership_index(),
        room_id=ROOM_ID,
        thread_id=THREAD_ID,
        requester_user_id="@user:localhost",
        client=_ROUTER_CLIENT,
        command_event_id="$command",
    )
    assert "- `large` (openai large-model) for `test_agent`" in response


@pytest.mark.parametrize("write_args", ["reset", "default"])
def test_model_command_ignores_overrides_of_removed_entities(tmp_path: Path, write_args: str) -> None:
    """A removed agent's thread override is not reported, and the next reset or selection removes it.

    The agent added again under the same name then starts without the old override.
    """
    config = _config_with_models(tmp_path)
    runtime_paths = runtime_paths_for(config)
    readded_config = bind_runtime_paths(
        Config(
            agents={
                "test_agent": AgentConfig(display_name="Test Agent", model="default"),
                "removed_agent": AgentConfig(display_name="Removed Agent", model="default"),
            },
            models=config.models,
        ),
        runtime_paths,
    )
    set_thread_model_override(
        runtime_paths,
        thread_id=THREAD_ID,
        model_name="large",
        entity_names=("test_agent", "removed_agent"),
        config=readded_config,
    )

    def model_command(args_text: str, command_config: Config = config) -> str:
        response, _ = handle_model_command(
            args_text,
            config=command_config,
            runtime_paths=runtime_paths,
            membership_index=isolated_membership_index(),
            room_id=ROOM_ID,
            thread_id=THREAD_ID,
            requester_user_id="@user:localhost",
            client=_ROUTER_CLIENT,
            command_event_id="$command",
        )
        return response

    assert "`removed_agent`" not in model_command("")
    assert "✅" in model_command(write_args)
    assert "`removed_agent`" not in model_command("", readded_config)
    runtime_model = resolve_turn_model(
        readded_config,
        runtime_paths,
        ModelScope(entity="removed_agent", room_id=ROOM_ID, thread_id=THREAD_ID),
        None,
    )
    assert runtime_model.model_name == "default"


def test_model_help_describes_room_level_fallback() -> None:
    """Reset help must describe the room-level fallback that actually runs."""
    help_text = get_command_help("model")

    assert "room-level model selection" in help_text


def test_model_command_list_alias_shows_models(tmp_path: Path) -> None:
    """!model list must show the override state, even outside a thread."""
    config = _config_with_models(tmp_path)
    runtime_paths = runtime_paths_for(config)

    for thread_id in (THREAD_ID, None):
        response, _ = handle_model_command(
            "list",
            config=config,
            runtime_paths=runtime_paths,
            membership_index=isolated_membership_index(),
            room_id=ROOM_ID,
            thread_id=thread_id,
            requester_user_id="@user:localhost",
            client=_ROUTER_CLIENT,
            command_event_id="$command",
        )
        assert "No thread model override" in response
        assert "`default`" in response
        assert "`large`" in response


def test_model_command_rejects_unknown_model(tmp_path: Path) -> None:
    """!model should reject names missing from config.models."""
    config = _config_with_models(tmp_path)
    runtime_paths = runtime_paths_for(config)

    response, _ = handle_model_command(
        "nonexistent",
        config=config,
        runtime_paths=runtime_paths,
        membership_index=isolated_membership_index(),
        room_id=ROOM_ID,
        thread_id=THREAD_ID,
        requester_user_id="@user:localhost",
        client=_ROUTER_CLIENT,
        command_event_id="$command",
    )
    assert "Unknown model" in response
    assert _stored_model(runtime_paths, THREAD_ID) is None


def test_model_command_requires_thread_for_set(tmp_path: Path) -> None:
    """Setting an override outside a thread should fail clearly."""
    config = _config_with_models(tmp_path)
    runtime_paths = runtime_paths_for(config)

    response, _ = handle_model_command(
        "large",
        config=config,
        runtime_paths=runtime_paths,
        membership_index=isolated_membership_index(),
        room_id=ROOM_ID,
        thread_id=None,
        requester_user_id="@user:localhost",
        client=_ROUTER_CLIENT,
        command_event_id="$command",
    )
    assert "only work inside a thread" in response
    assert _stored_model(runtime_paths, THREAD_ID) is None


def test_model_command_default_sets_model_instead_of_resetting(tmp_path: Path) -> None:
    """A configured model named "default" must be settable, not treated as a reset alias."""
    config = _config_with_models(tmp_path)
    runtime_paths = runtime_paths_for(config)

    response, _ = handle_model_command(
        "default",
        config=config,
        runtime_paths=runtime_paths,
        membership_index=isolated_membership_index(),
        room_id=ROOM_ID,
        thread_id=THREAD_ID,
        requester_user_id="@user:localhost",
        client=_ROUTER_CLIENT,
        command_event_id="$command",
    )
    assert "✅" in response
    assert "`default`" in response
    assert _stored_model(runtime_paths, THREAD_ID) == "default"


def test_store_drops_records_with_corrupt_set_at(tmp_path: Path) -> None:
    """Records with a non-string set_at must be dropped so prune sorting cannot fail."""
    config = _config_with_models(tmp_path)
    runtime_paths = test_runtime_paths(tmp_path)
    path = _store_path(runtime_paths)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        json.dumps({"$bad:localhost": {"model:test_agent": "large", "set_at": 12345}}),
        encoding="utf-8",
    )

    assert _stored_model(runtime_paths, "$bad:localhost") is None

    set_thread_model_override(
        runtime_paths,
        thread_id=THREAD_ID,
        model_name="large",
        entity_names=("test_agent",),
        config=config,
    )
    assert _stored_model(runtime_paths, THREAD_ID) == "large"


def _make_tool_context(tmp_path: Path, *, thread_id: str | None = THREAD_ID) -> ToolRuntimeContext:
    config = _config_with_models(tmp_path)
    return make_test_tool_runtime_context(
        agent_name="test_agent",
        target=MessageTarget.resolve(
            room_id=ROOM_ID,
            thread_id=thread_id,
            reply_to_event_id=None,
        ),
        requester_id="@user:localhost",
        client=AsyncMock(),
        config=config,
        runtime_paths=runtime_paths_for(config),
        relations=make_relation_lookup(),
        conversation_reader=make_conversation_reader_mock(),
        room=None,
        storage_path=None,
    )


def test_thread_model_tool_registered(tmp_path: Path) -> None:
    """The thread_model tool should be available from the metadata registry."""
    config = _config_with_models(tmp_path)
    toolkit = get_tool_by_name(
        "thread_model",
        runtime_paths_for(config),
        worker_target=None,
        tools=builtin_tool_registry(),
    )

    assert "thread_model" in builtin_tool_registry()
    assert isinstance(toolkit, ThreadModelTools)
    assert "thread_model_read" in toolkit.functions
    assert builtin_tool_registry().entries["thread_model"].function_names == ("thread_model_read", "thread_model_write")


def test_thread_model_switch_may_stop_the_run_but_does_not_stop_every_call() -> None:
    """Only a switch for the current response stops the run, and every switch still runs inside the run that made it."""
    tool = ThreadModelTools()
    function = toolkit_offers(tool, "thread_model_write")

    assert (function.stop_after_call, function.may_stop_run, function.controls_run) == (False, True, True)
    assert function.parameters["properties"]["action"]["enum"] == ["switch", "reset"]
    assert {"type": "string"} in function.parameters["properties"]["model_name"]["anyOf"]
    assert function.parameters["properties"]["when"]["enum"] == ["after-toolcall", "next-turn"]
    assert function.parameters["required"] == ["action"]


def _configured_thread_model_functions(context: ToolRuntimeContext, workspace: Path) -> dict[str, ToolFunction]:
    """Return the thread-model functions as an agent with a workspace is offered them, auto-saving any output."""
    toolkit = get_tool_by_name(
        "thread_model",
        context.runtime_paths,
        worker_target=None,
        tools=builtin_tool_registry(),
        tool_output_workspace_root=workspace,
        tool_output_auto_save_threshold_bytes=1,
    )
    return {function.name: function for function in adapt_toolkit(toolkit, key="thread_model").functions}


def test_a_configured_switch_offers_no_output_path(tmp_path: Path) -> None:
    """A switch's result is for its run, so the model cannot ask to save it to a file, as it can for list_models."""
    functions = _configured_thread_model_functions(_make_tool_context(tmp_path), tmp_path / "workspace")

    assert OUTPUT_PATH_ARGUMENT not in functions["thread_model_write"].parameters["properties"]
    assert OUTPUT_PATH_ARGUMENT in functions["thread_model_read"].parameters["properties"]


@pytest.mark.asyncio
async def test_a_configured_switch_for_the_current_response_is_never_auto_saved(tmp_path: Path) -> None:
    """A saved result would answer with a receipt, which no longer stops the run or names the new model."""
    context = _make_tool_context(tmp_path)
    switch = _configured_thread_model_functions(context, tmp_path / "workspace")["thread_model_write"]

    with tool_runtime_context(context):
        outcome = await execute_tool_call(
            switch,
            {"action": "switch", "model_name": "large", "when": "after-toolcall"},
            ToolCallContext(run_id="run", session_id="session", call_id="call-switch", user_id=None),
            middleware=(),
        )

    assert outcome.stop_after_call is True
    assert json.loads(outcome.content)["model"] == "large"
    assert not (tmp_path / "workspace").exists()


@pytest.mark.asyncio
async def test_thread_model_tool_requires_runtime_context() -> None:
    """Tool calls should fail clearly outside Matrix runtime context."""
    payload = json.loads(await ThreadModelTools().thread_model_write("switch", "large"))

    assert payload["status"] == "error"
    assert payload["tool"] == "thread_model"
    assert "context" in payload["message"]


@pytest.mark.asyncio
async def test_thread_model_tool_lists_configured_models_without_thread(tmp_path: Path) -> None:
    """Removing explicit discovery would leave agents unable to choose a configured alias."""
    context = _make_tool_context(tmp_path, thread_id=None)

    with tool_runtime_context(context):
        payload = json.loads(await ThreadModelTools().thread_model_read("list"))

    assert payload == {
        "action": "list",
        "models": [
            {"model_id": "default-model", "name": "default", "provider": "openai"},
            {"model_id": "large-model", "name": "large", "provider": "openai"},
        ],
        "status": "ok",
        "tool": "thread_model",
    }


@pytest.mark.asyncio
async def test_thread_model_tool_requires_thread(tmp_path: Path) -> None:
    """Switching should fail clearly without an active thread."""
    context = _make_tool_context(tmp_path, thread_id=None)
    with tool_runtime_context(context):
        payload = json.loads(await ThreadModelTools().thread_model_write("switch", "large"))

    assert payload["status"] == "error"
    assert "thread" in payload["message"]


@pytest.mark.asyncio
async def test_thread_model_tool_switch_get_and_reset(tmp_path: Path) -> None:
    """Switch, get, and reset should round-trip through the store."""
    context = _make_tool_context(tmp_path)
    tool = ThreadModelTools()

    with tool_runtime_context(context):
        switch_payload = json.loads(await tool.thread_model_write("switch", "large"))
        get_payload = json.loads(await tool.thread_model_read("get"))
        reset_payload = json.loads(await tool.thread_model_write("reset"))

    assert switch_payload["status"] == "ok"
    assert switch_payload["model"] == "large"
    assert switch_payload["model_id"] == "large-model"
    assert get_payload["overrides"] == {"router": "large", "test_agent": "large"}
    assert reset_payload["status"] == "ok"
    assert reset_payload["cleared"] is True
    assert _stored_model(context.runtime_paths, THREAD_ID) is None


@pytest.mark.asyncio
@pytest.mark.parametrize("action", ["switch", "reset"])
async def test_thread_model_tool_reports_unavailable_storage(tmp_path: Path, action: str) -> None:
    """A failed mutation returns an error string, not an exception or a model switch."""
    context = _make_tool_context(tmp_path)
    store = _store_path(context.runtime_paths)
    store.parent.mkdir(parents=True, exist_ok=True)
    store.write_text("not json", encoding="utf-8")
    tool = ThreadModelTools()

    with tool_runtime_context(context):
        result = (
            await tool.thread_model_write("switch", "large", when="after-toolcall")
            if action == "switch"
            else await tool.thread_model_write("reset")
        )

    assert isinstance(result, str)
    assert json.loads(result) == {
        "tool": "thread_model",
        "status": "error",
        "action": action,
        "message": "Thread model storage is unavailable. Please try again.",
    }
    assert store.read_text(encoding="utf-8") == "not json"


@pytest.mark.asyncio
async def test_thread_model_tool_can_switch_after_current_tool_call(tmp_path: Path) -> None:
    """Ignoring the timing argument would leave the current response on the old model."""
    context = _make_tool_context(tmp_path)

    with tool_runtime_context(context):
        outcome = await ThreadModelTools().thread_model_write("switch", "large", when="after-toolcall")

    # The run stops after the call, so the turn continues the response on the new model.
    assert isinstance(outcome, ToolOutcome)
    assert outcome.stop_after_call is True
    payload = json.loads(outcome.content)
    assert payload["status"] == "ok"
    assert payload["model"] == "large"
    assert payload["when"] == "after-toolcall"
    assert payload["note"] == "The current response will continue with the new model after this tool call."
    assert _stored_model(context.runtime_paths, THREAD_ID) == "large"


@pytest.mark.asyncio
async def test_over_budget_switch_continues_this_response_on_the_fallback(tmp_path: Path) -> None:
    """A mid-response switch to a priced model must not bypass the requester's budget."""
    context = _make_tool_context(tmp_path)
    context.config.models["large"].pricing = ModelPricing(input=5, output=30)
    context.config.models["cheap"] = ModelConfig(provider="openai", id="cheap-model")
    context.config.budgets = BudgetsConfig(fallback_model="cheap", monthly_limit_usd=0)

    with tool_runtime_context(context):
        outcome = await ThreadModelTools().thread_model_write("switch", "large", when="after-toolcall")

    assert isinstance(outcome, ToolOutcome)
    assert outcome.stop_after_call is True
    payload = json.loads(outcome.content)
    assert payload["status"] == "ok"
    assert payload["model"] == "cheap"
    assert payload["requested_model"] == "large"
    assert "budget" in payload["note"]
    assert _stored_model(context.runtime_paths, THREAD_ID) == "large"


@pytest.mark.asyncio
async def test_over_budget_next_turn_switch_says_replies_use_the_fallback(tmp_path: Path) -> None:
    """The agent should not promise the requested model while the requester is over budget."""
    context = _make_tool_context(tmp_path)
    context.config.models["large"].pricing = ModelPricing(input=5, output=30)
    context.config.models["cheap"] = ModelConfig(provider="openai", id="cheap-model")
    context.config.budgets = BudgetsConfig(fallback_model="cheap", monthly_limit_usd=0)

    with tool_runtime_context(context):
        payload = json.loads(await ThreadModelTools().thread_model_write("switch", "large"))

    assert payload["model"] == "large"
    assert "budget" in payload["note"]
    assert "`cheap`" in payload["note"]


@pytest.mark.asyncio
async def test_thread_model_tool_rejects_unknown_switch_timing(tmp_path: Path) -> None:
    """Accepting an unknown timing value could persist a change with undefined execution semantics."""
    context = _make_tool_context(tmp_path)
    entrypoint = toolkit_offers(ThreadModelTools(), "thread_model_write").entrypoint

    with tool_runtime_context(context), pytest.raises(ValidationError):
        await entrypoint(action="switch", model_name="large", when="later")

    assert _stored_model(context.runtime_paths, THREAD_ID) is None


@pytest.mark.asyncio
async def test_thread_model_tool_rejects_unknown_model(tmp_path: Path) -> None:
    """Switching should reject names missing from config.models."""
    context = _make_tool_context(tmp_path)

    with tool_runtime_context(context):
        payload = json.loads(await ThreadModelTools().thread_model_write("switch", "nonexistent"))

    assert payload["status"] == "error"
    assert payload["available_models"] == ["default", "large"]
    assert _stored_model(context.runtime_paths, THREAD_ID) is None


@pytest.mark.asyncio
async def test_thread_model_tool_rejects_a_model_removed_since_the_turn_started(tmp_path: Path) -> None:
    """A model a live reload removed is refused, and the thread keeps the selection it had."""
    context = _make_tool_context(tmp_path)
    current = context.config.model_copy(deep=True)
    del current.models["large"]
    tool = ThreadModelTools()

    with tool_runtime_context(context):
        await tool.thread_model_write("switch", "default")
    with tool_runtime_context(replace(context, config_provider=lambda: current)):
        payload = json.loads(await tool.thread_model_write("switch", "large"))

    assert payload["status"] == "error"
    assert payload["available_models"] == ["default"]
    assert _stored_model(context.runtime_paths, THREAD_ID) == "default"


@pytest.mark.asyncio
async def test_a_next_turn_switch_reports_the_model_later_turns_will_run(tmp_path: Path) -> None:
    """A live reload that points a name at another model shows in what the switch confirms."""
    context = _make_tool_context(tmp_path)
    current = context.config.model_copy(deep=True)
    current.models["large"].id = "larger-model"

    with tool_runtime_context(replace(context, config_provider=lambda: current)):
        payload = json.loads(await ThreadModelTools().thread_model_write("switch", "large"))

    assert payload["status"] == "ok"
    assert payload["model_id"] == "larger-model"


@pytest.mark.asyncio
async def test_a_next_turn_switch_measures_the_budget_as_later_turns_will(tmp_path: Path) -> None:
    """A live reload that prices a model shows in whether the switch promises it to a requester over budget."""
    context = _make_tool_context(tmp_path)
    context.config.models["cheap"] = ModelConfig(provider="openai", id="cheap-model")
    context.config.budgets = BudgetsConfig(fallback_model="cheap", monthly_limit_usd=0)
    current = context.config.model_copy(deep=True)
    current.models["large"].pricing = ModelPricing(input=5, output=30)

    with tool_runtime_context(replace(context, config_provider=lambda: current)):
        payload = json.loads(await ThreadModelTools().thread_model_write("switch", "large"))

    assert payload["model"] == "large"
    assert "`cheap`" in payload["note"]


@pytest.mark.asyncio
async def test_thread_model_tool_switches_a_reasoning_level_and_rejects_an_unsupported_one(tmp_path: Path) -> None:
    """``switch_thread_model`` accepts ``name@level`` and names supported levels when the level is unsupported."""
    context = _make_tool_context(tmp_path)
    context.config.models["sol"] = ModelConfig(
        provider="openai",
        id="sol-model",
        reasoning={"levels": ["low", "high"], "default": "low"},
    )

    with tool_runtime_context(context):
        switched = json.loads(await ThreadModelTools().thread_model_write("switch", "sol@high"))
        rejected = json.loads(await ThreadModelTools().thread_model_write("switch", "sol@xhigh"))

    assert switched["status"] == "ok"
    assert switched["model"] == "sol@high"
    assert _stored_model(context.runtime_paths, THREAD_ID) == "sol@high"
    assert rejected["status"] == "error"
    assert rejected["message"] == "unsupported level 'xhigh' for sol; supported: low, high."


@pytest.mark.asyncio
async def test_thread_model_tool_reports_stale_override_as_inactive(tmp_path: Path) -> None:
    """An override naming a removed model must not be reported as active."""
    context = _make_tool_context(tmp_path)
    set_thread_model_override(
        context.runtime_paths,
        thread_id=THREAD_ID,
        model_name="removed-model",
        entity_names=("test_agent",),
        config=context.config,
    )
    set_room_model_override(
        context.runtime_paths,
        room_id=ROOM_ID,
        model_name="large",
    )

    with tool_runtime_context(context):
        payload = json.loads(await ThreadModelTools().thread_model_read("get"))

    assert payload["status"] == "ok"
    assert payload["overrides"] == {}
    assert payload["stale_overrides"] == {"test_agent": "removed-model"}
    assert "no longer configured" in payload["note"]
    assert "room-level model selection" in payload["note"]
    assert (
        resolve_turn_model(
            context.config,
            context.runtime_paths,
            ModelScope(entity="test_agent", room_id=ROOM_ID, thread_id=THREAD_ID),
            None,
        ).model_name
        == "large"
    )


def _restricted_tool_context(tmp_path: Path, requester_id: str) -> ToolRuntimeContext:
    config = restricted_config(tmp_path)
    return make_test_tool_runtime_context(
        agent_name="helper",
        target=MessageTarget.resolve(room_id=ROOM, thread_id="$root", reply_to_event_id=None),
        requester_id=requester_id,
        client=AsyncMock(),
        config=config,
        runtime_paths=runtime_paths_for(config),
        relations=make_relation_lookup(),
        conversation_reader=make_conversation_reader_mock(),
        room=None,
        storage_path=None,
    )


@pytest.mark.asyncio
@pytest.mark.usefixtures("enforce_turn_authorization")
async def test_thread_model_tool_switch_leaves_entities_the_requester_cannot_address(tmp_path: Path) -> None:
    """A tool switch for a requester excluded by an agent's access must leave that agent on its own model."""
    context = _restricted_tool_context(tmp_path, USER)

    with tool_runtime_context(context):
        payload = json.loads(await ThreadModelTools().thread_model_write("switch", "expensive"))

    assert payload["status"] == "ok"
    assert resolved_thread_models(context.config, context.runtime_paths) == {
        "helper": "expensive",
        "restricted": "default",
    }


@pytest.mark.asyncio
@pytest.mark.usefixtures("enforce_turn_authorization")
async def test_thread_model_tool_reports_and_resets_only_the_entities_each_override_governs(tmp_path: Path) -> None:
    """Get must name each override's entities, and reset must keep another setter's restricted selection."""
    other_context = _restricted_tool_context(tmp_path, OTHER)
    context = replace(other_context, requester_id=USER)
    tool = ThreadModelTools()

    with tool_runtime_context(other_context):
        await tool.thread_model_write("switch", "expensive")
    with tool_runtime_context(context):
        await tool.thread_model_write("switch", "cheap")
        get_payload = json.loads(await tool.thread_model_read("get"))
        reset_payload = json.loads(await tool.thread_model_write("reset"))

    assert get_payload["overrides"] == {"helper": "cheap", "restricted": "expensive"}
    assert reset_payload["cleared"] is True
    assert resolved_thread_models(context.config, context.runtime_paths) == {
        "helper": "default",
        "restricted": "expensive",
    }


@pytest.mark.parametrize("display_name", [None, "Default Model"])
def test_ai_run_metadata_uses_preparation_time_model(tmp_path: Path, display_name: str | None) -> None:
    """Run metadata must describe the model that produced the response, not the next override."""
    config = _config_with_models(tmp_path)
    config.models["default"].display_name = display_name
    config.models["large"].display_name = "Large Model"
    runtime_paths = runtime_paths_for(config)
    prepared_model_name = resolve_turn_model(
        config,
        runtime_paths,
        ModelScope(entity="test_agent", room_id=ROOM_ID, thread_id=THREAD_ID),
        None,
    ).model_name

    # A mid-run switch_thread_model call persists a new override before the
    # metadata for the current response is built.
    set_thread_model_override(
        runtime_paths,
        thread_id=THREAD_ID,
        model_name="large",
        entity_names=("test_agent",),
        config=config,
    )

    metadata = build_ai_run_metadata_content(
        prices={},
        config=config,
        model_name=prepared_model_name,
        run_id="run-1",
        session_id="session-1",
        status=RunStatus.completed,
        model="default-model",
        model_provider="openai",
        tool_count=0,
    )

    assert metadata[AI_RUN_METADATA_KEY]["model"] == {
        "config": "default",
        "id": "default-model",
        "provider": "openai",
        **({"display_name": display_name} if display_name else {}),
    }


@pytest.mark.parametrize(
    ("model_name", "display_name", "expected_name"),
    [
        ("default", "  Friendly Model  ", "Friendly Model"),
        ("default", None, None),
        ("default", "", None),
        ("default", "  ", None),
        ("removed-model", "Friendly Model", None),
    ],
)
def test_ai_run_metadata_optional_model_display_name(
    tmp_path: Path,
    model_name: str,
    display_name: str | None,
    expected_name: str | None,
) -> None:
    """Publish the configured display name when set while retaining the model alias."""
    config = _config_with_models(tmp_path)
    config.models["default"] = ModelConfig(provider="openai", id="default-model", display_name=display_name)

    metadata = build_ai_run_metadata_content(
        prices={},
        config=config,
        model_name=model_name,
        run_id="run-1",
        session_id="session-1",
        status=RunStatus.completed,
        model="default-model",
        model_provider="openai",
        tool_count=0,
    )

    model = metadata[AI_RUN_METADATA_KEY]["model"]
    assert model["config"] == model_name
    if expected_name is None:
        assert "display_name" not in model
    else:
        assert model["display_name"] == expected_name


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("pick", "expected_code", "expected_router"),
    [
        (None, "default", "default"),
        ("large", "large", "default"),
    ],
    ids=["no-pick-uses-target-default", "opening-pick-does-not-inherit-parent-router-model"],
)
@pytest.mark.usefixtures("enforce_turn_authorization")
async def test_cross_room_fork_skips_parent_thread_model_inheritance(
    tmp_path: Path,
    pick: str | None,
    expected_code: str,
    expected_router: str,
) -> None:
    """A cross-room fork ignores parent thread model overrides while still honoring an explicit opening pick."""
    bot = _fork_bot(tmp_path)
    bot.config.agents["code"].access = ResponderAccessConfig(users=["@user:localhost"])
    bot.config.router.access = ResponderAccessConfig(current_room_members=False)
    set_thread_model_override(
        bot._turn_dispatcher.deps.runtime_paths,
        thread_id=THREAD_ID,
        model_name="large",
        entity_names=("code", "router"),
        config=bot.config,
    )

    await _dispatch_fork_turn(bot, (_FORK_ID,), pick=pick, source_room_id="!other-room:localhost")

    assert _fork_model(bot) == expected_code
    router = resolve_turn_model(
        bot.config,
        runtime_paths_for(bot.config),
        ModelScope(entity="router", room_id=ROOM_ID, thread_id=_FORK_ID),
        None,
    )
    assert router.model_name == expected_router


@pytest.mark.asyncio
async def test_thread_model_validation_errors_and_get_without_thread(tmp_path: Path) -> None:
    """Validation rejects bad actions, missing model_name, and cross-action params on reset before side effects."""
    context = _make_tool_context(tmp_path, thread_id=None)
    tool = ThreadModelTools()

    with tool_runtime_context(context):
        list_ok = json.loads(await tool.thread_model_read("list"))
        get_no_thread = json.loads(await tool.thread_model_read("get"))
        bad_read = json.loads(await tool.thread_model_read("nope"))  # type: ignore[arg-type]
        bad_write = json.loads(await tool.thread_model_write("nope"))  # type: ignore[arg-type]
        missing_model = json.loads(await tool.thread_model_write("switch"))
        blank_model = json.loads(await tool.thread_model_write("switch", model_name="  "))
        reset_with_model = json.loads(await tool.thread_model_write("reset", model_name="large"))
        reset_with_when = json.loads(await tool.thread_model_write("reset", when="next-turn"))

    assert list_ok["status"] == "ok"
    assert get_no_thread["status"] == "error"
    assert "thread" in get_no_thread["message"]
    assert bad_read["status"] == "error"
    assert "Unsupported thread_model_read action" in bad_read["message"]
    assert bad_write["status"] == "error"
    assert "Unsupported thread_model_write action" in bad_write["message"]
    assert missing_model["status"] == "error"
    assert "model_name is required" in missing_model["message"]
    assert blank_model["status"] == "error"
    assert "model_name is required" in blank_model["message"]
    assert reset_with_model["status"] == "error"
    assert "'model_name'" in reset_with_model["message"]
    assert reset_with_when["status"] == "error"
    assert "'when'" in reset_with_when["message"]
