"""The note telling an agent which other threads of its room had activity since its last reply in this thread."""

from __future__ import annotations

import asyncio
from contextlib import contextmanager
from dataclasses import dataclass, replace
from typing import TYPE_CHECKING, cast
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from structlog.testing import capture_logs

from mindroom.ai import ai_response
from mindroom.config.agent import AgentConfig
from mindroom.config.resolution import room_activity
from mindroom.config.room_activity import AgentRoomActivityConfig, RoomActivityConfig
from mindroom.hooks.enrichment import is_transient_context
from mindroom.llm.messages import stable_serialize
from mindroom.matrix.conversation_reads import ConversationReader
from mindroom.memory import MemoryPromptParts
from mindroom.room_activity.note import (
    _ROOM_ACTIVITY_ENRICHMENT_KEY,
    _room_activity_item,
    _room_activity_note,
    with_room_activity_note,
)
from tests.conftest import make_turn_context, make_visible_message
from tests.fakes.scripted_model import ScriptedModel, answer
from tests.history_helpers import _make_config
from tests.test_event_journal_store import OTHER_ROOM, ROOM, admit, summary_notice, text

if TYPE_CHECKING:
    from collections.abc import Iterator
    from pathlib import Path

    from mindroom.agent_modes import AgentMode
    from mindroom.bot_runtime_view import BotRuntimeView
    from mindroom.config.main import Config
    from mindroom.constants import RuntimePaths
    from mindroom.event_journal import EventJournalStore
    from mindroom.event_journal.store import PrincipalStore
    from mindroom.hooks import EnrichmentItem
    from mindroom.llm.provider import ModelProvider
    from mindroom.matrix.client_visible_messages import ResolvedVisibleMessage

AGENT = "@helper:localhost"
MAX = "@max:localhost"
DANIEL = "@daniel:localhost"
KINSEY = "@kinsey:localhost"
NAMES = {MAX: "Max", DANIEL: "Daniel", KINSEY: "Kinsey", AGENT: "Helper"}
MINUTE = 60_000
NOW = 1_800_000_000_000
FOOTER = 'Read one with matrix_message(action="read", thread_id="...") if it bears on this request.'


def _ago(minutes: int) -> int:
    return NOW - minutes * MINUTE


@pytest.fixture
def principal(journal_store: EventJournalStore) -> PrincipalStore:
    """Return the agent's own view of the projection."""
    return journal_store.principal(f"helper@{AGENT}")


def _reader(store: PrincipalStore) -> ConversationReader:
    return ConversationReader(store=store, hydrator=MagicMock())


def _history(*replies: tuple[str, int]) -> list[ResolvedVisibleMessage]:
    """Return the current thread's history: its root, then each ``(sender, minutes ago)`` message."""
    messages = [make_visible_message(sender=MAX, body="Current question", event_id="$current", timestamp=_ago(300))]
    messages += [
        make_visible_message(sender=sender, body="message", event_id=f"$current-{index}", timestamp=_ago(minutes))
        for index, (sender, minutes) in enumerate(replies)
    ]
    return messages


async def _note(
    store: PrincipalStore,
    history: list[ResolvedVisibleMessage],
    settings: RoomActivityConfig | None = None,
    names: dict[str, str] = NAMES,
) -> str | None:
    return await _room_activity_note(
        _reader(store),
        settings or RoomActivityConfig(enabled=True),
        room_id=ROOM,
        thread_id="$current",
        agent_user_id=AGENT,
        thread_history=history,
        member_display_names=names,
        trusted_sender_ids=frozenset({AGENT}),
        authorize_human=lambda _sender: False,
        now_ms=NOW,
    )


async def _busy_room(store: PrincipalStore) -> None:
    """Two threads with activity since the agent's last reply here, 30 minutes ago, and noise around them."""
    await admit(store, "$game", ts=_ago(200), sender=MAX, content=text("Let's build the game\nwith sprites"))
    await admit(
        store,
        "$game-title",
        ts=_ago(100),
        sender=AGENT,
        thread_id="$game",
        content=summary_notice("Game prototype"),
    )
    await admit(store, "$game-old", ts=_ago(40), sender=MAX, thread_id="$game")
    await admit(store, "$game-1", ts=_ago(25), sender=DANIEL, thread_id="$game")
    await admit(store, "$game-2", ts=_ago(10), sender=AGENT, thread_id="$game")
    await admit(store, "$game-3", ts=_ago(3), sender=MAX, thread_id="$game")
    await admit(store, "$design", ts=_ago(60), sender=KINSEY, content=text("Design assets\nWe need icons"))
    await admit(store, "$design-1", ts=_ago(20), sender=KINSEY, thread_id="$design")
    await admit(store, "$stale-1", ts=_ago(35), sender=DANIEL, thread_id="$stale")
    await admit(store, "$current-new", ts=_ago(2), sender=DANIEL, thread_id="$current")
    await admit(store, "$room-level", ts=_ago(2), sender=DANIEL)
    await admit(store, "$elsewhere-1", ts=_ago(1), sender=DANIEL, room_id=OTHER_ROOM, thread_id="$elsewhere")


@pytest.mark.asyncio
async def test_lists_other_threads_with_activity_since_the_last_reply_here(principal: PrincipalStore) -> None:
    """Each line names the thread, who wrote, how many new messages and when, and the agent's own reply there."""
    await _busy_room(principal)

    note = await _note(principal, _history((AGENT, 30)))

    assert note is not None
    assert note.splitlines() == [
        "Other threads in this room with activity since your last reply here:",
        '- "Game prototype" (thread $game): Max, Daniel; 3 new messages, last 3 min ago; you replied there 10 min ago',
        '- "Design assets" (thread $design): Kinsey; 1 new message, last 20 min ago',
        FOOTER,
    ]


@pytest.mark.asyncio
async def test_lists_at_most_max_threads_newest_first(principal: PrincipalStore) -> None:
    """A busy room lists only its most recently active threads."""
    await _busy_room(principal)

    note = await _note(principal, _history((AGENT, 30)), RoomActivityConfig(enabled=True, max_threads=1))

    assert note is not None
    assert [line for line in note.splitlines() if line.startswith("- ")] == [
        '- "Game prototype" (thread $game): Max, Daniel; 3 new messages, last 3 min ago; you replied there 10 min ago',
    ]


@pytest.mark.asyncio
async def test_a_first_reply_looks_back_one_window(principal: PrincipalStore) -> None:
    """With no earlier reply here, the note covers the window, and nothing older."""
    await _busy_room(principal)
    await admit(principal, "$ancient-1", ts=_ago(130), sender=KINSEY, thread_id="$ancient")

    note = await _note(principal, _history((MAX, 30)), RoomActivityConfig(enabled=True, window_minutes=120))

    assert note is not None
    lines = note.splitlines()
    assert lines[0] == "Other threads in this room with activity in the last 120 minutes:"
    assert (
        '- "Game prototype" (thread $game): Max, Daniel; 4 new messages, last 3 min ago; you replied there 10 min ago'
        in lines
    )
    assert "- (thread $stale): Daniel; 1 new message, last 35 min ago" in lines
    assert not any("$ancient" in line for line in lines)


@pytest.mark.asyncio
async def test_no_note_when_nothing_changed(principal: PrincipalStore) -> None:
    """A quiet room, or one whose activity is all before the last reply here, adds nothing."""
    assert await _note(principal, _history((AGENT, 30))) is None
    await admit(principal, "$design-1", ts=_ago(40), sender=KINSEY, thread_id="$design")
    await admit(principal, "$current-new", ts=_ago(2), sender=DANIEL, thread_id="$current")

    assert await _note(principal, _history((AGENT, 30))) is None


@pytest.mark.asyncio
async def test_another_room_and_the_current_thread_never_appear(principal: PrincipalStore) -> None:
    """Only the reply's own room counts, and never the thread the agent is replying in."""
    await admit(principal, "$current-new", ts=_ago(2), sender=DANIEL, thread_id="$current")
    await admit(principal, "$elsewhere-1", ts=_ago(1), sender=DANIEL, room_id=OTHER_ROOM, thread_id="$elsewhere")

    assert await _note(principal, _history((AGENT, 30))) is None


@pytest.mark.asyncio
async def test_a_summary_notice_is_neither_a_new_message_nor_a_reply(principal: PrincipalStore) -> None:
    """A thread whose only new message is its summary had no activity, and a long title is cut short."""
    await admit(principal, "$t", ts=_ago(200), sender=MAX, content=text("x" * 300))
    await admit(principal, "$t-title", ts=_ago(5), sender=AGENT, thread_id="$t", content=summary_notice("Fresh title"))

    assert await _note(principal, _history((AGENT, 30))) is None

    await admit(principal, "$t-1", ts=_ago(4), sender=MAX, thread_id="$t")
    note = await _note(principal, _history((AGENT, 30)))
    assert note is not None
    assert '- "Fresh title" (thread $t): Max; 1 new message, last 4 min ago' in note.splitlines()


@pytest.mark.asyncio
async def test_a_long_root_and_many_writers_stay_short(principal: PrincipalStore) -> None:
    """A title is one cut line of the root, and a thread names at most three writers."""
    await admit(principal, "$t", ts=_ago(200), sender=MAX, content=text("word " * 100))
    for index in range(5):
        await admit(principal, f"$t-{index}", ts=_ago(10 - index), sender=f"@person{index}:localhost", thread_id="$t")

    note = await _note(principal, _history((AGENT, 30)))

    assert note is not None
    (line,) = [line for line in note.splitlines() if line.startswith("- ")]
    title = line.split('"')[1]
    assert len(title) <= 80
    assert title.endswith("...")
    assert ": person4, person3, person2 and 2 others; 5 new messages, last 6 min ago" in line


@pytest.mark.asyncio
async def test_the_agent_s_own_summary_notice_is_not_its_last_reply(principal: PrincipalStore) -> None:
    """A title the agent posts in this thread after replying does not hide activity elsewhere since that reply."""
    await admit(principal, "$other-1", ts=_ago(20), sender=MAX, thread_id="$other")
    history = _history((AGENT, 30))
    history.append(
        make_visible_message(
            sender=AGENT,
            body="Current title",
            event_id="$current-title",
            timestamp=_ago(10),
            content=summary_notice("Current title"),
        ),
    )

    note = await _note(principal, history)

    assert note is not None
    assert "- (thread $other): Max; 1 new message, last 20 min ago" in note.splitlines()


@pytest.mark.asyncio
async def test_a_long_display_name_is_cut(principal: PrincipalStore) -> None:
    """A writer's display name is shortened, so one line stays short."""
    await admit(principal, "$other-1", ts=_ago(5), sender="@long:localhost", thread_id="$other")

    note = await _note(principal, _history((AGENT, 30)), names={"@long:localhost": "L" * 300})

    assert note is not None
    line = next(line for line in note.splitlines() if line.startswith("- "))
    assert len(line) < 120


@dataclass
class _Runtime:
    config: Config
    client: object
    agent_reply_memberships: object


def _config(
    tmp_path: Path,
    agent_settings: AgentRoomActivityConfig | None,
    *,
    tools: list[str],
) -> tuple[Config, RuntimePaths]:
    config, runtime_paths = _make_config(tmp_path)
    config.agents["test_agent"] = AgentConfig(display_name="Helper", tools=tools, room_activity=agent_settings)
    return config, runtime_paths


async def _turn_items(
    principal: PrincipalStore,
    config: Config,
    runtime_paths: RuntimePaths,
    *,
    thread_id: str | None = "$current",
    agent_mode: AgentMode = "standard",
    reader: ConversationReader | None = None,
) -> tuple[EnrichmentItem, ...]:
    ctx = replace(
        make_turn_context("test_agent", room_id=ROOM, thread_id=thread_id),
        thread_history=_history((AGENT, 30)),
        member_display_names=NAMES,
        agent_mode=agent_mode,
    )
    with patch("mindroom.room_activity.note.time.time", return_value=NOW / 1000):
        return await with_room_activity_note(
            ctx,
            runtime=cast("BotRuntimeView", _Runtime(config, MagicMock(), MagicMock())),
            runtime_paths=runtime_paths,
            reader=reader or _reader(principal),
            agent_user_id=AGENT,
        )


@pytest.mark.asyncio
async def test_the_turn_gets_the_note_only_when_the_setting_is_on(tmp_path: Path, principal: PrincipalStore) -> None:
    """The note is opt-in, needs ``matrix_message`` to read a thread, and only a standard thread reply gets one."""
    await _busy_room(principal)

    config, runtime_paths = _config(tmp_path, None, tools=["matrix_message"])
    assert await _turn_items(principal, config, runtime_paths) == ()

    config, runtime_paths = _config(tmp_path, AgentRoomActivityConfig(enabled=True), tools=["matrix_message"])
    (item,) = await _turn_items(principal, config, runtime_paths)
    assert item.key == _ROOM_ACTIVITY_ENRICHMENT_KEY
    assert item.persist is False
    assert item.cache_policy == "volatile"
    assert item.text.startswith("Other threads in this room with activity since your last reply here:")
    assert await _turn_items(principal, config, runtime_paths, thread_id=None) == ()
    assert await _turn_items(principal, config, runtime_paths, agent_mode="minimal") == ()

    config, runtime_paths = _config(tmp_path, AgentRoomActivityConfig(enabled=True), tools=[])
    assert await _turn_items(principal, config, runtime_paths) == ()


@pytest.mark.asyncio
async def test_a_failed_read_costs_the_reply_only_its_note(tmp_path: Path, principal: PrincipalStore) -> None:
    """When the note cannot be built, the reply goes on without it and one warning names the room; a stop still stops."""
    config, runtime_paths = _config(tmp_path, AgentRoomActivityConfig(enabled=True), tools=["matrix_message"])
    failing = MagicMock(spec=ConversationReader)
    failing.room_thread_activity = AsyncMock(side_effect=TimeoutError("journal busy"))

    with capture_logs() as logs:
        assert await _turn_items(principal, config, runtime_paths, reader=failing) == ()

    assert [(log["log_level"], log["error_type"], log["room_id"]) for log in logs] == [
        ("warning", "TimeoutError", ROOM),
    ]
    failing.room_thread_activity = AsyncMock(side_effect=asyncio.CancelledError)
    with pytest.raises(asyncio.CancelledError):
        await _turn_items(principal, config, runtime_paths, reader=failing)


def test_an_agent_overrides_the_defaults_field_by_field(tmp_path: Path) -> None:
    """Defaults are off with a 120-minute window and five threads; an agent's authored fields replace them."""
    config, _runtime_paths = _config(tmp_path, AgentRoomActivityConfig(enabled=True), tools=[])
    assert config.defaults.room_activity == RoomActivityConfig(enabled=False, window_minutes=120, max_threads=5)
    config.defaults.room_activity = RoomActivityConfig(window_minutes=30)

    assert room_activity(config, "test_agent") == RoomActivityConfig(enabled=True, window_minutes=30, max_threads=5)
    assert room_activity(config, "missing") == RoomActivityConfig(window_minutes=30)
    with pytest.raises(ValueError, match="max_threads"):
        RoomActivityConfig(max_threads=0)


class _Provider:
    def __init__(self) -> None:
        self.model = ScriptedModel([answer("ok") for _ in range(3)])

    def build(self, *_args: object, **_kwargs: object) -> ModelProvider:
        return self.model


@contextmanager
def _scripted_provider(monkeypatch: pytest.MonkeyPatch) -> Iterator[_Provider]:
    built = _Provider()
    monkeypatch.setattr("mindroom.providers.registry.build_model_provider", built.build)
    with patch("mindroom.ai.build_memory_prompt_parts", new=AsyncMock(return_value=MemoryPromptParts())):
        yield built


@pytest.mark.asyncio
async def test_the_note_follows_the_prompt_and_every_request_extends_the_previous_one(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The note is turn context after the prompt, never a cache breakpoint, and stays where it was sent."""
    config, runtime_paths = _make_config(tmp_path, num_history_runs=10)
    notes = {"First prompt": "note one", "Second prompt": None, "Third prompt": "note three"}
    requests: list[list[str]] = []
    with _scripted_provider(monkeypatch) as provider:
        for prompt, note in notes.items():
            items = () if note is None else (_room_activity_item(note),)
            ctx = replace(
                make_turn_context("test_agent", session_id="session-1"),
                prompt=prompt,
                transient_enrichment_items=items,
            )
            await ai_response(ctx, runtime_paths, config)
            # What a request sends of each message; ids and creation times stay local.
            requests.append(
                [
                    stable_serialize({"role": message.role, "content": message.content})
                    for message in provider.model.requests[-1].messages
                ],
            )
        last = provider.model.requests[-1].messages

    first, second, third = requests
    assert second[: len(first)] == first
    assert third[: len(second)] == second
    assert [message.role for message in last[-2:]] == ["user", "user"]
    assert last[-2].content == "Third prompt"
    assert is_transient_context(last[-1].content)
    assert "note three" in str(last[-1].content)
    assert last[-1].add_to_agent_memory is False
    assert sum("note one" in str(message.content) for message in last) == 1
