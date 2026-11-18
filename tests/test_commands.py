"""Tests for command parsing."""

from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace
from typing import TYPE_CHECKING, Any, cast
from unittest.mock import AsyncMock, MagicMock, patch

import nio
import pytest

from mindroom.authorization import (
    ensure_room_membership_synced,
    responder_candidate_entities_from_cached_room,
)
from mindroom.commands.handler import generate_welcome_message_for_room, handle_command
from mindroom.commands.parsing import (
    _COMMAND_DOCS,
    Command,
    CommandType,
    command_parser,
    get_command_help,
    get_compact_command_entries,
)
from mindroom.config.access import ResponderAccessConfig
from mindroom.config.agent import AgentConfig, TeamConfig
from mindroom.config.main import Config, load_config
from mindroom.config.models import RouterConfig
from mindroom.constants import ROUTER_AGENT_NAME, RuntimePaths, resolve_runtime_paths
from mindroom.matrix.identity import MatrixID
from mindroom.matrix.mentions import format_message_with_mentions
from mindroom.message_target import MessageTarget
from mindroom.room_reply_modes import set_room_reply_mode_override
from tests.authorization_helpers import (
    isolated_membership_index,
    make_test_command_handler_context,
)
from tests.conftest import (
    bind_runtime_paths,
    make_conversation_reader_mock,
    make_visible_message,
    runtime_paths_for,
    test_runtime_paths,
)
from tests.identity_helpers import persist_entity_accounts
from tests.test_plugins import _write_broken_tool_plugin
from tests.test_turn_policy import (
    _EVENT_ID,
    _ROOM_ID,
    _SENDER,
    _context,
    _dispatch,
    _entity_id,
    _policy_for,
    _room_with_members,
)
from tests.test_turn_policy import config as config  # noqa: PLC0414

if TYPE_CHECKING:
    from mindroom.conversation_resolver import MessageContext
    from mindroom.event_journal import EventJournalStore

WELCOME_QUICK_COMMAND_LINES = [
    "\u2022 `/hi` - Show this welcome message again",
    "\u2022 `/schedule <time> <message>` - Schedule tasks and reminders",
    "\u2022 `/help [topic]` - Get detailed help",
]


def _test_runtime_paths(tmp_path: Path) -> RuntimePaths:
    return RuntimePaths(
        config_path=tmp_path / "config.yaml",
        config_dir=tmp_path,
        env_path=tmp_path / ".env",
        storage_root=tmp_path / "mindroom_data",
        process_env={"MATRIX_HOMESERVER": "http://localhost:8008"},
    )


def _parse(text: str) -> Command | None:
    """Parse ``text`` and check that its ``/`` form parses identically, or as conversation when unknown."""
    command = command_parser.parse(text)
    if "!" in text:
        slash = command_parser.parse(text.replace("!", "/", 1))
        if command is None or command.type is CommandType.UNKNOWN:
            assert slash is None
        else:
            assert slash is not None
            assert (slash.type, slash.args) == (command.type, command.args)
    return command


def test_help_command() -> None:
    """Test help command parsing."""
    # Basic help
    command = _parse("!help")
    assert command is not None
    assert command.type == CommandType.HELP
    assert command.args["topic"] is None

    # Help with topic
    command = _parse("!help invite")
    assert command is not None
    assert command.type == CommandType.HELP
    assert command.args["topic"] == "invite"


def test_hi_command() -> None:
    """Test hi command parsing."""
    # Basic hi command
    command = _parse("!hi")
    assert command is not None
    assert command.type == CommandType.HI
    assert command.args == {}

    # Case insensitive
    command = _parse("!HI")
    assert command is not None
    assert command.type == CommandType.HI

    # With trailing space (should still work)
    command = _parse("!hi ")
    assert command is not None
    assert command.type == CommandType.HI


def test_invalid_commands() -> None:
    """Test that invalid commands are handled correctly."""
    # Commands that should return UNKNOWN
    unknown_commands = [
        "!invalid",
        "!unknowncmd",
        "!test123",
        "!notacommand",
    ]

    for cmd_text in unknown_commands:
        command = _parse(cmd_text)
        assert command is not None
        assert command.type == CommandType.UNKNOWN

    # Non-commands that should return None
    non_commands = [
        "invite calculator",  # Missing exclamation
        "just a regular message",
        "",
    ]

    for cmd_text in non_commands:
        command = _parse(cmd_text)
        assert command is None


def test_reload_plugins_command() -> None:
    """Test reload-plugins command parsing."""
    for cmd_text in ("!reload-plugins", "!reload_plugins", "!RELOAD-PLUGINS"):
        command = _parse(cmd_text)
        assert command is not None
        assert command.type == CommandType.RELOAD_PLUGINS
        assert command.args == {}


def test_schedule_command() -> None:
    """Test schedule command parsing."""
    # Basic schedule with time and message
    command = _parse("!schedule in 5 minutes Check the deployment")
    assert command is not None
    assert command.type == CommandType.SCHEDULE
    assert command.args["full_text"] == "in 5 minutes Check the deployment"

    # Schedule with just time expression
    command = _parse("!schedule tomorrow")
    assert command is not None
    assert command.type == CommandType.SCHEDULE
    assert command.args["full_text"] == "tomorrow"

    # Schedule with complex expression
    command = _parse("!schedule tomorrow at 3pm Send the weekly report")
    assert command is not None
    assert command.type == CommandType.SCHEDULE
    assert command.args["full_text"] == "tomorrow at 3pm Send the weekly report"


def test_list_schedules_command() -> None:
    """Test list schedules command parsing."""
    variations = [
        "!list_schedules",
        "!listschedules",
        "!list-schedules",
        "!list_schedule",  # singular with underscore
        "!listschedule",  # singular without separator
        "!list-schedule",  # singular with dash
        "!inspect_schedules",
        "!inspectschedules",
        "!inspect-schedules",
        "!inspect_schedule",
        "!inspectschedule",
        "!inspect-schedule",
        "!LIST_SCHEDULES",  # case insensitive
    ]

    for cmd_text in variations:
        command = _parse(cmd_text)
        assert command is not None
        assert command.type == CommandType.LIST_SCHEDULES
        assert command.args == {}


def test_removed_skill_command_is_unknown() -> None:
    """Test that the removed skill command is parsed as unknown."""
    command = _parse("!skill repo-quick-audit")
    assert command is not None
    assert command.type == CommandType.UNKNOWN
    assert command.args["raw_command"] == "!skill repo-quick-audit"

    command = _parse("!skill summarize Release notes")
    assert command is not None
    assert command.type == CommandType.UNKNOWN
    assert command.args["raw_command"] == "!skill summarize Release notes"

    command = _parse("!skill   ")
    assert command is not None
    assert command.type == CommandType.UNKNOWN
    assert command.args["raw_command"] == "!skill"


def test_all_commands_have_documentation() -> None:
    """Test that all CommandType values have documentation."""
    # Check that all commands have documentation (except UNKNOWN which is special)
    commands_needing_docs = set(CommandType) - {CommandType.UNKNOWN}
    missing_docs = commands_needing_docs - set(_COMMAND_DOCS.keys())
    assert not missing_docs, f"Missing documentation for commands: {missing_docs}"

    # Check that there are no extra documentation entries
    extra_docs = set(_COMMAND_DOCS.keys()) - set(CommandType)
    assert not extra_docs, f"Documentation for non-existent commands: {extra_docs}"

    # Check that all documentation entries are properly formatted
    for cmd_type, (syntax, description) in _COMMAND_DOCS.items():
        assert syntax.startswith("/"), f"{cmd_type} syntax should start with '/'"
        assert len(description) > 0, f"{cmd_type} should have a description"


def test_background_job_commands() -> None:
    """`!jobs` lists the conversation's background jobs; `!cancel_job` cancels one or all of them."""
    command = _parse("!jobs")
    assert command is not None
    assert command.type == CommandType.JOBS
    command = _parse("!cancel_job abc123")
    assert command is not None
    assert command.type == CommandType.CANCEL_JOB
    assert command.args == {"job_id": "abc123"}
    command = _parse("!cancel-job all")
    assert command is not None
    assert command.type == CommandType.CANCEL_JOB
    assert command.args == {"job_id": None}


def test_cancel_schedule_command() -> None:
    """Test cancel schedule command parsing."""
    # Basic cancel
    command = _parse("!cancel_schedule abc123")
    assert command is not None
    assert command.type == CommandType.CANCEL_SCHEDULE
    assert command.args["task_id"] == "abc123"
    assert command.args["cancel_all"] is False

    # With hyphen
    command = _parse("!cancel-schedule xyz789")
    assert command is not None
    assert command.type == CommandType.CANCEL_SCHEDULE
    assert command.args["task_id"] == "xyz789"
    assert command.args["cancel_all"] is False

    # Case insensitive
    command = _parse("!CANCEL_SCHEDULE task456")
    assert command is not None
    assert command.type == CommandType.CANCEL_SCHEDULE
    assert command.args["cancel_all"] is False

    # Cancel all tasks
    command = _parse("!cancel_schedule all")
    assert command is not None
    assert command.type == CommandType.CANCEL_SCHEDULE
    assert command.args["task_id"] == "all"
    assert command.args["cancel_all"] is True

    # Cancel all with different case
    command = _parse("!cancel_schedule ALL")
    assert command is not None
    assert command.type == CommandType.CANCEL_SCHEDULE
    assert command.args["task_id"] == "ALL"
    assert command.args["cancel_all"] is True


def test_edit_schedule_command() -> None:
    """Test edit schedule command parsing."""
    command = _parse("!edit_schedule abc123 in 10 minutes check deployment")
    assert command is not None
    assert command.type == CommandType.EDIT_SCHEDULE
    assert command.args["task_id"] == "abc123"
    assert command.args["full_text"] == "in 10 minutes check deployment"

    command = _parse("!edit-schedule task42 tomorrow at 9am @finance market update")
    assert command is not None
    assert command.type == CommandType.EDIT_SCHEDULE
    assert command.args["task_id"] == "task42"
    assert command.args["full_text"] == "tomorrow at 9am @finance market update"

    command = _parse("!EDIT_SCHEDULE id999 every weekday at 8am check alerts")
    assert command is not None
    assert command.type == CommandType.EDIT_SCHEDULE
    assert command.args["task_id"] == "id999"


@pytest.mark.parametrize(
    "text",
    [
        "!config show",
        "!config",
        "!desktop setup",
        "!mode helper minimal",
        "!model opus",
        "!model",
        "!room_model reset",
        "!room-model",
        "!thread_mode room",
        "!reply_mode mentions",
        "!auto_approve 2h",
        "!encrypt confirm",
        "!e2ee",
        "!jobs",
        "!cancel_job all",
        "!help schedule",
        "!schedule in 5 minutes\nmultiline task",
        "🎤 !schedule in 5 minutes check",
        "  !hi  ",
    ],
)
def test_slash_prefix_parses_like_bang(text: str) -> None:
    """Every known command parses the same with ``/`` as with ``!``."""
    command = _parse(text)
    assert command is not None
    assert command.type is not CommandType.UNKNOWN


@pytest.mark.parametrize(
    "text",
    ["/home/user/file.txt", "/s", "/unknown", "//schedule in 5 min x", "/schedule", "/ help", "/", "🎤 /foo"],
)
def test_unknown_slash_text_is_conversation(text: str) -> None:
    """A ``/`` message that matches no command stays conversation."""
    assert command_parser.parse(text) is None


def test_unknown_bang_command_stays_unknown() -> None:
    """``!unknown`` still parses as an unknown command."""
    command = command_parser.parse("!unknown")
    assert command is not None
    assert command.type == CommandType.UNKNOWN


def test_slash_command_keeps_original_raw_text() -> None:
    """``raw_text`` carries the text as typed, after voice-prefix removal."""
    command = command_parser.parse("🎤 /schedule tomorrow check")
    assert command is not None
    assert command.raw_text == "/schedule tomorrow check"


def test_get_command_help() -> None:
    """Test help text generation."""
    # General help
    help_text = get_command_help()
    assert "Available Commands" in help_text
    assert "/schedule" in help_text
    assert "/help" in help_text
    assert "/schedule" in help_text
    assert "/list_schedules" in help_text
    assert "/cancel_schedule" in help_text
    assert "/edit_schedule" in help_text
    assert "/skill" not in help_text

    # Specific command help
    schedule_help = get_command_help("schedule")
    assert "Schedule Command" in schedule_help
    assert "Usage:" in schedule_help
    assert "Reminders" in schedule_help or "Workflows" in schedule_help

    # Schedule command help
    schedule_help = get_command_help("schedule")
    assert "Schedule Command" in schedule_help
    assert "Simple Reminders:" in schedule_help
    assert "Agent and Team Workflows:" in schedule_help
    assert "in 5 minutes" in schedule_help
    assert "Requires an explicit recurring polling cadence" in schedule_help
    assert "Automatically converts to smart polling" not in schedule_help
    assert "silently" in schedule_help
    assert "quietly" in schedule_help
    assert "hide their trigger" in schedule_help
    assert "NO_REPLY" in schedule_help
    assert "visible by default" in schedule_help

    list_schedules_help = get_command_help("list_schedules")
    assert "List Schedules Command" in list_schedules_help

    cancel_help = get_command_help("cancel_schedule")
    assert "Cancel Schedule Command" in cancel_help
    assert "cancel_schedule" in cancel_help

    edit_help = get_command_help("edit_schedule")
    assert "Edit Schedule Command" in edit_help
    assert "edit_schedule" in edit_help
    assert "make this schedule silent" in edit_help
    assert "make this schedule visible" in edit_help
    assert "silent-delivery mode" in edit_help

    reload_help = get_command_help("reload-plugins")
    assert "Reload Plugins Command" in reload_help
    assert "/reload-plugins" in reload_help
    assert "Admin only" in reload_help

    reload_help_alias = get_command_help("reload_plugins")
    assert reload_help_alias == reload_help

    skill_help = get_command_help("skill")
    assert "Available Commands" in skill_help
    assert "/skill" not in skill_help


def test_compact_command_entries_characterize_welcome_subset() -> None:
    """Compact command docs preserve the existing welcome quick-command wording."""
    assert (
        get_compact_command_entries(
            (CommandType.HI, CommandType.SCHEDULE, CommandType.HELP),
            format_code=True,
        )
        == WELCOME_QUICK_COMMAND_LINES
    )


@pytest.mark.asyncio
async def test_welcome_message_uses_compact_command_docs(tmp_path: Path) -> None:
    """The welcome quick commands should match the parser-owned compact docs."""
    room = nio.MatrixRoom(room_id="!room:localhost", own_user_id="@mindroom_router:localhost")
    runtime_paths = _test_runtime_paths(tmp_path)
    config = Config()
    persist_entity_accounts(config, runtime_paths, usernames={"router": "mindroom_router_oldns"})
    welcome_message = generate_welcome_message_for_room(room.room_id, config, runtime_paths)

    quick_command_block = "\u26a1 **Quick commands:**\n" + "\n".join(WELCOME_QUICK_COMMAND_LINES)
    assert quick_command_block in welcome_message
    assert "using its configured alias" in welcome_message
    assert "@mindroom_assistant" not in welcome_message


@pytest.mark.asyncio
async def test_welcome_message_lists_configured_teams(tmp_path: Path) -> None:
    """Rooms configured through teams should advertise those team mention targets."""
    room = nio.MatrixRoom(room_id="!room:localhost", own_user_id="@mindroom_router:localhost")
    config = Config(
        agents={"calculator": AgentConfig(display_name="Calculator")},
        teams={
            "ops": TeamConfig(
                display_name="Ops Team",
                role="Operations escalation team",
                agents=["calculator"],
                rooms=["!room:localhost"],
            ),
        },
    )
    runtime_paths = _test_runtime_paths(tmp_path)
    persist_entity_accounts(
        config,
        runtime_paths,
        usernames={
            "router": "mindroom_router_oldns",
            "calculator": "mindroom_calculator_oldns",
            "ops": "mindroom_ops_oldns",
        },
    )
    welcome_message = generate_welcome_message_for_room(room.room_id, config, runtime_paths)

    assert "\U0001f9e0 **Available agents and teams in this room:**" in welcome_message
    assert "\u2022 **Ops Team** (alias `ops`): Operations escalation team (Team of 1 agent)" in welcome_message
    assert format_message_with_mentions(config, runtime_paths, welcome_message)["body"] == welcome_message


@pytest.mark.asyncio
async def test_senderless_welcome_lists_configured_room_responders(tmp_path: Path) -> None:
    """Senderless configured-room welcomes should advertise static room responders."""
    room = nio.MatrixRoom(room_id="!room:localhost", own_user_id="@mindroom_router:localhost")
    room.add_member("@mindroom_research_oldns:localhost", "Research", None)
    room.members_synced = True
    config = Config(
        agents={
            "code": AgentConfig(
                display_name="Code",
                role="Writes code",
                rooms=["!room:localhost"],
            ),
            "research": AgentConfig(
                display_name="Research",
                role="Finds sources",
            ),
        },
        teams={
            "ops": TeamConfig(
                display_name="Ops Team",
                role="Operations escalation team",
                agents=["code"],
                rooms=["!room:localhost"],
            ),
        },
    )
    runtime_paths = _test_runtime_paths(tmp_path)
    persist_entity_accounts(
        config,
        runtime_paths,
        usernames={
            "router": "mindroom_router_oldns",
            "code": "mindroom_code_oldns",
            "research": "mindroom_research_oldns",
            "ops": "mindroom_ops_oldns",
        },
    )

    welcome_message = generate_welcome_message_for_room(room.room_id, config, runtime_paths)

    assert "\U0001f9e0 **Available agents and teams in this room:**" in welcome_message
    assert "\u2022 **Code** (alias `code`): Writes code" in welcome_message
    assert "\u2022 **Ops Team** (alias `ops`): Operations escalation team (Team of 1 agent)" in welcome_message
    assert "\u2022 **Research**" not in welcome_message


@pytest.mark.asyncio
async def test_hi_command_lists_ad_hoc_present_responder(tmp_path: Path) -> None:
    """Ad-hoc room welcomes should advertise the same live responders routing can target."""
    config = Config(
        agents={
            "code": AgentConfig(
                display_name="Code",
                role="Writes code",
            ),
        },
    )
    runtime_paths = _test_runtime_paths(tmp_path)
    persist_entity_accounts(
        config,
        runtime_paths,
        usernames={"router": "mindroom_router_oldns", "code": "mindroom_code_oldns"},
    )
    room = nio.MatrixRoom(room_id="!adhoc:localhost", own_user_id="@mindroom_router:localhost")
    room.add_member("@mindroom_code_oldns:localhost", "Code", None)
    room.members_synced = True
    send_response = AsyncMock(return_value="$welcome")
    command = Command(type=CommandType.HI, args={}, raw_text="!hi")
    event = SimpleNamespace(
        sender="@alice:localhost",
        event_id="$event",
        body="!hi",
        source={"content": {"body": "!hi"}},
    )
    context = make_test_command_handler_context(
        client=AsyncMock(),
        config=config,
        runtime_paths=runtime_paths,
        logger=MagicMock(),
        conversation_reader=make_conversation_reader_mock(),
        stable_target=MessageTarget.resolve("!adhoc:localhost", None, "$event"),
        record_handled_turn=AsyncMock(),
        record_command_result=AsyncMock(),
        send_response=send_response,
    )

    await handle_command(
        context=context,
        room=room,
        event=event,
        command=command,
        requester_user_id="@alice:localhost",
    )

    response_text = send_response.await_args.args[0]
    assert "\U0001f9e0 **Available agents and teams in this room:**" in response_text
    assert "\u2022 **Code** (alias `code`): Writes code" in response_text
    context.client.joined_members.assert_not_awaited()


@pytest.mark.asyncio
async def test_hi_command_uses_live_responder_candidates_when_available(tmp_path: Path) -> None:
    """The live command path should mirror routing's responder candidate boundary."""
    config = Config(
        agents={
            "code": AgentConfig(
                display_name="Code",
                role="Writes code",
            ),
            "research": AgentConfig(
                display_name="Research",
                role="Researches topics",
            ),
        },
    )
    runtime_paths = _test_runtime_paths(tmp_path)
    persist_entity_accounts(
        config,
        runtime_paths,
        usernames={
            "router": "mindroom_router",
            "code": "mindroom_code",
            "research": "mindroom_research",
        },
    )
    room = nio.MatrixRoom(room_id="!room:localhost", own_user_id="@mindroom_router:localhost")
    send_response = AsyncMock(return_value="$welcome")
    candidate_resolver = AsyncMock(return_value=[MatrixID.parse("@mindroom_code:localhost")])
    context = make_test_command_handler_context(
        client=AsyncMock(),
        config=config,
        runtime_paths=runtime_paths,
        logger=MagicMock(),
        conversation_reader=make_conversation_reader_mock(),
        stable_target=MessageTarget.resolve("!room:localhost", None, "$event"),
        record_handled_turn=AsyncMock(),
        record_command_result=AsyncMock(),
        send_response=send_response,
        responder_candidates_for_room=candidate_resolver,
    )

    await handle_command(
        context=context,
        room=room,
        event=SimpleNamespace(
            sender="@alice:localhost",
            event_id="$event",
            body="!hi",
            source={"content": {"body": "!hi"}},
        ),
        command=Command(type=CommandType.HI, args={}, raw_text="!hi"),
        requester_user_id="@alice:localhost",
    )

    candidate_resolver.assert_awaited_once_with(room, "@alice:localhost")
    response_text = send_response.await_args.args[0]
    assert "\u2022 **Code** (alias `code`): Writes code" in response_text
    assert "\u2022 **Research**" not in response_text


@pytest.mark.asyncio
async def test_schedule_command_reuses_failed_boundary_membership_snapshot(tmp_path: Path) -> None:
    """Live scheduling must not retry a failed membership refresh from the same turn."""
    config = Config()
    runtime_paths = _test_runtime_paths(tmp_path)
    persist_entity_accounts(config, runtime_paths, usernames={"router": "mindroom_router"})
    membership_index = isolated_membership_index()
    room = nio.MatrixRoom(room_id="!adhoc:localhost", own_user_id="@mindroom_router:localhost")
    room.add_member("@mindroom_router:localhost", "Router", None)
    room.add_member("@alice:localhost", "Alice", None)
    client = AsyncMock()
    client.joined_members.side_effect = TimeoutError("membership lookup timed out")
    send_response = AsyncMock(return_value="$schedule-response")
    command = Command(
        type=CommandType.SCHEDULE,
        args={"full_text": "in 5 minutes check logs"},
        raw_text="!schedule in 5 minutes check logs",
    )
    event = SimpleNamespace(
        sender="@alice:localhost",
        event_id="$schedule",
        body=command.raw_text,
        source={"content": {"body": command.raw_text}},
    )

    async def cached_responder_candidates(candidate_room: nio.MatrixRoom, sender_id: str) -> list[MatrixID]:
        return responder_candidate_entities_from_cached_room(
            candidate_room,
            sender_id,
            config,
            runtime_paths,
            membership_index,
        )

    context = make_test_command_handler_context(
        client=client,
        config=config,
        runtime_paths=runtime_paths,
        logger=MagicMock(),
        conversation_reader=make_conversation_reader_mock(),
        stable_target=MessageTarget.resolve(room.room_id, None, event.event_id),
        record_handled_turn=AsyncMock(),
        record_command_result=AsyncMock(),
        send_response=send_response,
        responder_candidates_for_room=cached_responder_candidates,
        agent_reply_memberships=membership_index,
    )

    assert not await ensure_room_membership_synced(client, room, sender_id=event.sender)

    await handle_command(
        context=context,
        room=room,
        event=event,
        command=command,
        requester_user_id=event.sender,
    )

    assert client.joined_members.await_count == 1
    assert "No agents or teams" in send_response.await_args.args[0]


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("room_shape", "sync_mode", "expected_reply"),
    [
        ("private", "classic", None),
        ("extra_member", "classic", "private room"),
        ("extra_agent", "classic", "private room"),
        ("failed_refresh", "classic", "try again"),
        ("unseen_invite", "classic", "private room"),
        # Sliding sync loads members lazily, so a refresh without the server's counts proves nothing about invites.
        ("private", "sliding", "private room"),
        ("counted", "sliding", None),
    ],
)
async def test_desktop_command_resolves_exact_agent_from_room_candidates(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    room_shape: str,
    sync_mode: str,
    expected_reply: str | None,
) -> None:
    """`/desktop` requires the sole eligible agent and a complete membership with no party but the owner account."""
    runtime_paths = _test_runtime_paths(tmp_path)
    config = Config.validate_with_runtime(
        {
            "models": {"default": {"provider": "openai", "id": "test-model"}},
            "matrix_sync": {"mode": sync_mode},
            "defaults": {"tools": []},
            "agents": {
                "code": {
                    "display_name": "Code",
                    "role": "Writes code",
                    "tools": ["desktop"],
                },
                "notes": {"display_name": "Notes", "role": "Takes notes"},
            },
        },
        runtime_paths,
    )
    persist_entity_accounts(
        config,
        runtime_paths,
        usernames={"router": "mindroom_router", "code": "mindroom_code", "notes": "mindroom_notes"},
    )
    # In a room with a second agent, that agent may be the one answering.
    own_user_id = "@mindroom_notes:localhost" if room_shape == "extra_agent" else "@mindroom_code:localhost"
    room = nio.MatrixRoom(room_id="!room:localhost", own_user_id=own_user_id)
    joined_user_ids = ["@mindroom_router:localhost", "@mindroom_code:localhost", "@alice:localhost"]
    if room_shape == "extra_member":
        joined_user_ids.append("@bob:localhost")
    if room_shape == "extra_agent":
        joined_user_ids.append("@mindroom_notes:localhost")
    for user_id in joined_user_ids:
        room.add_member(user_id, None, None)
    if room_shape == "unseen_invite":
        # The server still counts an invite the projection never saw.
        room.update_summary(nio.RoomSummary(invited_member_count=1, joined_member_count=3))
    if room_shape == "counted":
        room.update_summary(nio.RoomSummary(invited_member_count=0, joined_member_count=3))
    client = AsyncMock()
    if room_shape == "failed_refresh":
        client.joined_members.side_effect = TimeoutError("membership lookup timed out")
    else:
        client.joined_members.return_value = nio.JoinedMembersResponse(
            members=[nio.RoomMember(user_id, None, None) for user_id in joined_user_ids],
            room_id=room.room_id,
        )
    assert await ensure_room_membership_synced(client, room, sender_id="@alice:localhost") is (
        room_shape != "failed_refresh"
    )
    send_response = AsyncMock(return_value="$desktop")
    candidate_resolver = AsyncMock(return_value=[MatrixID.parse(user_id) for user_id in joined_user_ids[1:]])
    controller_identity = MagicMock()
    desktop_handler = MagicMock(return_value="desktop status")
    monkeypatch.setattr("mindroom.commands.handler.handle_desktop_command", desktop_handler)
    context = make_test_command_handler_context(
        client=client,
        config=config,
        runtime_paths=runtime_paths,
        logger=MagicMock(),
        conversation_reader=make_conversation_reader_mock(),
        stable_target=MessageTarget.resolve("!room:localhost", None, "$event"),
        record_handled_turn=AsyncMock(),
        record_command_result=AsyncMock(),
        send_response=send_response,
        responder_candidates_for_room=candidate_resolver,
        controller_identity=controller_identity,
    )

    await handle_command(
        context=context,
        room=room,
        event=SimpleNamespace(
            sender="@alice:localhost",
            event_id="$event",
            body="!desktop status",
            source={"content": {"body": "!desktop status"}},
        ),
        command=Command(
            type=CommandType.DESKTOP,
            args={"args_text": "status"},
            raw_text="!desktop status",
        ),
        requester_user_id="@alice:localhost",
    )

    if expected_reply is None:
        assert desktop_handler.call_args.kwargs["scope"].agent_name == "code"
        assert desktop_handler.call_args.kwargs["scope"].requester_id == "@alice:localhost"
        assert desktop_handler.call_args.kwargs["scope"].controller_identity is controller_identity
    else:
        desktop_handler.assert_not_called()
        assert expected_reply in send_response.await_args.args[0]


def test_chat_commands_page_lists_all_supported_commands() -> None:
    """The chat commands docs page should document every supported command syntax."""
    chat_commands = Path(__file__).resolve().parents[1] / "docs" / "chat-commands.md"
    # Table cells escape literal pipes in command syntax as "\|".
    contents = chat_commands.read_text(encoding="utf-8").replace("\\|", "|")

    for syntax, _description in _COMMAND_DOCS.values():
        assert syntax in contents


@pytest.mark.asyncio
async def test_welcome_lists_only_the_tools_an_agent_can_use_here(tmp_path: Path) -> None:
    """A tool whose plugin failed to load is not advertised, while the agent's other tools are."""
    _write_broken_tool_plugin(tmp_path / "plugins" / "broken")
    config_path = tmp_path / "config.yaml"
    config_path.write_text(
        "models:\n"
        "  default:\n"
        "    provider: openai\n"
        "    id: gpt-6-astra\n"
        "agents:\n"
        "  code:\n"
        "    display_name: Code\n"
        "    role: Writes code\n"
        "    rooms: ['!room:localhost']\n"
        "    include_default_tools: false\n"
        "    tools:\n"
        "      - broken_plugin_tool\n"
        "      - calculator\n"
        "plugins:\n"
        "  - ./plugins/broken\n",
        encoding="utf-8",
    )
    runtime_paths = resolve_runtime_paths(
        config_path=config_path,
        storage_path=tmp_path / "mindroom_data",
        process_env={"MATRIX_HOMESERVER": "http://localhost:8008", "MINDROOM_NAMESPACE": ""},
    )
    room = nio.MatrixRoom(room_id="!room:localhost", own_user_id="@mindroom_router:localhost")

    config = load_config(runtime_paths, tolerate_plugin_load_errors=True)
    persist_entity_accounts(config, runtime_paths, usernames={"router": "mindroom_router", "code": "mindroom_code"})
    welcome_message = generate_welcome_message_for_room(room.room_id, config, runtime_paths)

    assert "• **Code** (alias `code`): Writes code (\U0001f527 `calculator`)" in welcome_message
    assert "broken_plugin_tool" not in welcome_message


async def _command_owners(
    config: Config,
    text: str,
    rooms: dict[str, nio.MatrixRoom],
    context: MessageContext | None = None,
    journal_store: EventJournalStore | None = None,
) -> list[str]:
    """Return the entities whose own policy says they run this command, each from its own copy of the room."""
    command = command_parser.parse(text)
    assert command is not None
    owners = []
    for name, room in rooms.items():
        policy = _policy_for(config, name)
        policy.deps.runtime.orchestrator.journal_store = journal_store or _claims()
        dispatch = _dispatch(context or _context(), agent_name=name)
        if await policy.owns_command(room, dispatch, command, source_event_id=_EVENT_ID):
            owners.append(name)
    return owners


def _claims() -> EventJournalStore:
    """Return a claim store that saves the first owner of each room event's purpose."""
    saved: dict[tuple[str, ...], str] = {}

    async def claim_room_event(*key_and_owner: str) -> str:
        return saved.setdefault(key_and_owner[:3], key_and_owner[3])

    return cast("EventJournalStore", SimpleNamespace(claim_room_event=claim_room_event))


def _shared_room(config: Config, *entity_names: str) -> dict[str, nio.MatrixRoom]:
    """Return one room holding the person and these entities, as every MindRoom account sees it."""
    room = _room_with_members(_SENDER, *(_entity_id(config, name).full_id for name in entity_names))
    return dict.fromkeys(("general", "research", ROUTER_AGENT_NAME), room)


@pytest.mark.asyncio
async def test_mentioned_agent_handles_command(config: Config) -> None:
    """A command that mentions an agent is run by that agent alone, though another has the lower Matrix ID."""
    context = _context(mentioned=[_entity_id(config, "research")])

    assert command_parser.parse("@research !model").type is CommandType.MODEL
    assert await _command_owners(config, "@research !model", _shared_room(config, "general", "research"), context) == [
        "research",
    ]


@pytest.mark.asyncio
async def test_thread_agent_handles_command_in_its_thread(config: Config) -> None:
    """A command in a thread with one agent is run by that agent."""
    history = [
        make_visible_message(sender=_SENDER, body="please help"),
        make_visible_message(sender=_entity_id(config, "research").full_id, body="sure thing"),
    ]
    context = _context(thread_id="$thread:localhost", thread_history=history)

    assert await _command_owners(config, "!help", _shared_room(config, "general", "research"), context) == ["research"]


@pytest.mark.asyncio
async def test_untargeted_command_goes_to_lowest_id_candidate_without_model_call(config: Config) -> None:
    """A command aimed at nobody is run by the candidate with the lowest Matrix ID, and nothing is routed."""
    with patch("mindroom.router.relay.suggest_responder_for_message", new=AsyncMock()) as select:
        owners = await _command_owners(config, "!help", _shared_room(config, "general", "research"))

    assert owners == ["general"]
    select.assert_not_awaited()


@pytest.mark.asyncio
async def test_untargeted_command_answers_in_mentions_reply_mode(config: Config) -> None:
    """A command addresses MindRoom itself, so a room that answers only mentions still answers it."""
    set_room_reply_mode_override(runtime_paths_for(config), room_id=_ROOM_ID, mode="mentions")

    assert await _command_owners(config, "!help", _shared_room(config, "general", "research")) == ["general"]


@pytest.mark.asyncio
async def test_command_in_one_agent_room_is_handled_by_that_agent(config: Config) -> None:
    """A room with one agent and no owner account runs commands."""
    assert await _command_owners(config, "!help", _shared_room(config, "research")) == ["research"]


@pytest.mark.asyncio
async def test_command_owner_comes_from_the_joined_pool(tmp_path: Path) -> None:
    """An agent configured for a room it has left owns no command there: the joined candidates decide."""
    agents = {name: AgentConfig(display_name=name.title(), rooms=[_ROOM_ID]) for name in ("general", "research")}
    config = bind_runtime_paths(Config(agents=agents), test_runtime_paths(tmp_path))

    assert await _command_owners(config, "!help", _shared_room(config, "research")) == ["research"]


@pytest.mark.asyncio
async def test_saved_command_owner_is_followed_on_replay(config: Config) -> None:
    """Bots that disagree on a command's owner follow the first one saved, and its replay still runs it."""
    rooms = {name: _room_with_members(_SENDER, _entity_id(config, name).full_id) for name in ("research", "general")}
    claims = _claims()

    assert await _command_owners(config, "!help", rooms, journal_store=claims) == ["research"]
    assert await _command_owners(config, "!help", rooms, journal_store=claims) == ["research"]


def _router_access_denies_everyone(tmp_path: Path, **general: Any) -> Config:  # noqa: ANN401
    """Return a two-agent config whose agents admit the person and whose `router.access` does not."""
    access = ResponderAccessConfig(users=[_SENDER])
    return bind_runtime_paths(
        Config(
            agents={
                "general": AgentConfig(display_name="General", access=access, **general),
                "research": AgentConfig(display_name="Research", access=access),
            },
            router=RouterConfig(access=ResponderAccessConfig(users=["@owner:localhost"], current_room_members=False)),
        ),
        test_runtime_paths(tmp_path),
    )


@pytest.mark.asyncio
@pytest.mark.usefixtures("enforce_turn_authorization")
@pytest.mark.parametrize("owner_account_joined", [False, True])
async def test_desktop_rule_unchanged(tmp_path: Path, owner_account_joined: bool) -> None:
    """A Desktop agent alone with the requester runs `/desktop` whatever else decides commands.

    The owner account answers nobody, so it is no party to the room.
    """
    config = _router_access_denies_everyone(tmp_path, tools=["desktop"])
    entities = ("general", ROUTER_AGENT_NAME) if owner_account_joined else ("general",)
    room = _room_with_members(_SENDER, *(_entity_id(config, name).full_id for name in entities))
    client = AsyncMock()
    client.joined_members.return_value = nio.JoinedMembersResponse(
        members=[nio.RoomMember(user_id, None, None) for user_id in room.users],
        room_id=room.room_id,
    )
    assert await ensure_room_membership_synced(client, room, sender_id=_SENDER)
    room.update_summary(nio.RoomSummary(invited_member_count=0, joined_member_count=len(room.users)))

    assert await _command_owners(config, "!desktop status", {"general": room}) == ["general"]
    assert await _command_owners(config, "!help", {"general": room}) == []


@pytest.mark.asyncio
@pytest.mark.usefixtures("enforce_turn_authorization")
async def test_router_access_denies_untargeted_command(tmp_path: Path) -> None:
    """`router.access` gates a command aimed at nobody as it gates routing, and no command aimed at an agent."""
    config = _router_access_denies_everyone(tmp_path)
    rooms = _shared_room(config, "general", "research")
    mention = _context(mentioned=[_entity_id(config, "research")])

    assert await _command_owners(config, "!help", rooms) == []
    assert await _command_owners(config, "!help @research", rooms, mention) == ["research"]
