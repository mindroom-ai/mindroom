"""The automation runner: cron timing, the visible prompt it posts, and the verify step that follows the run."""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from typing import TYPE_CHECKING, Any, cast
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from mindroom.automations import runner as runner_module
from mindroom.automations.runner import AutomationRunner
from mindroom.background_tasks import wait_for_background_tasks
from mindroom.config.agent import AgentConfig
from mindroom.config.automations import PromptCurationAutomation
from mindroom.config.main import Config
from mindroom.config.models import RouterConfig
from mindroom.constants import ORIGINAL_SENDER_KEY, SCHEDULED_MODEL_KEY, resolve_runtime_paths
from mindroom.runtime_resolution import resolve_agent_runtime
from mindroom.thread_tags import ThreadTagsError

if TYPE_CHECKING:
    from pathlib import Path

    from mindroom.bot import AgentBot
    from mindroom.constants import RuntimePaths

ROOM = "!room:example.test"
# Eight 642-character sections: 1,286 tokens, over a 1,000-token trigger.
MEMORY = "# Memory\n" + "".join(f"## Topic {index}\n" + f"Detail {index} " * 70 + "\n" for index in range(8))
# MEMORY with its last section moved to memory/topics.md behind a pointer, within the bounds.
CONDENSED = MEMORY.split("## Topic 7\n")[0] + "## Topic 7\nSee memory/topics.md\n"
# Noon UTC; the default 04:00 schedule in the config's timezone is next due within a day.
NOON = datetime(2026, 10, 5, 12, 0, tzinfo=UTC)
DAY_LATER = NOON + timedelta(days=1)


@dataclass
class _Bot:
    """The agent bot as the runner sees it, recording every hook message it sends."""

    sent: list[dict[str, Any]] = field(default_factory=list)
    client: Any = field(default_factory=lambda: MagicMock(user_id="@mindroom_mind:example.test"))

    async def _hook_send_message(
        self,
        room_id: str,
        body: str,
        thread_id: str | None,
        source_hook: str,
        extra_content: dict[str, Any] | None = None,
        *,
        trigger_dispatch: bool = False,
    ) -> str:
        self.sent.append(
            {
                "room_id": room_id,
                "body": body,
                "thread_id": thread_id,
                "source_hook": source_hook,
                "extra_content": extra_content,
                "trigger_dispatch": trigger_dispatch,
            },
        )
        return f"$event{len(self.sent)}"


def _setup(
    tmp_path: Path,
    *,
    memory: str = MEMORY,
    rooms: list[str] | None = None,
) -> tuple[Config, RuntimePaths, AutomationRunner, _Bot]:
    agent = AgentConfig(
        display_name="Mind",
        memory_backend="file",
        rooms=[ROOM] if rooms is None else rooms,
        automations=[PromptCurationAutomation(trigger_tokens=1_000)],
    )
    config = Config(agents={"mind": agent}, router=RouterConfig(model="default"))
    paths = resolve_runtime_paths(config_path=tmp_path / "config.yaml", storage_path=tmp_path)
    root = resolve_agent_runtime("mind", config, paths, None, create=True).file_memory_root
    assert root is not None
    root.mkdir(parents=True, exist_ok=True)
    (root / "MEMORY.md").write_text(memory, encoding="utf-8")
    bot = _Bot()
    runner = AutomationRunner(
        runtime_paths=paths,
        config_provider=lambda: config,
        bot_provider=lambda name: cast("AgentBot", bot) if name == "mind" else None,
    )
    return config, paths, runner, bot


async def _tick(runner: AutomationRunner, now: datetime) -> None:
    await runner._tick(now)
    assert await wait_for_background_tasks(5)


@pytest.mark.asyncio
async def test_a_due_check_posts_a_visible_prompt_that_the_agent_answers(tmp_path: Path) -> None:
    """The first tick only schedules; at the due time the agent posts the prompt as a hook-dispatched message."""
    _config, _paths, runner, bot = _setup(tmp_path)

    await _tick(runner, NOON)
    assert bot.sent == []
    await _tick(runner, DAY_LATER)

    (prompt,) = bot.sent
    assert prompt["room_id"] == ROOM
    assert prompt["thread_id"] is None
    assert prompt["source_hook"] == "prompt_curation"
    assert prompt["trigger_dispatch"] is True
    assert prompt["body"].startswith("@mind 🧹 Prompt maintenance: the files loaded into every one of your prompts")
    # No internal user is provisioned in this runtime, so no original sender is attached.
    assert prompt["extra_content"] is None


@pytest.mark.asyncio
async def test_the_internal_user_is_the_prompts_requester(tmp_path: Path) -> None:
    """Like a todo poke without a human, the prompt runs as MindRoom's internal user."""
    _config, _paths, runner, bot = _setup(tmp_path)

    with patch("mindroom.automations.runner.mindroom_user_id", return_value="@mindroom_user:example.test"):
        await _tick(runner, NOON)
        await _tick(runner, DAY_LATER)

    assert bot.sent[0]["extra_content"] == {ORIGINAL_SENDER_KEY: "@mindroom_user:example.test"}


@pytest.mark.asyncio
async def test_a_configured_room_overrides_the_first_agent_room(tmp_path: Path) -> None:
    """`room` sends the prompt somewhere other than the agent's first room."""
    config, _paths, runner, bot = _setup(tmp_path)
    config.agents["mind"].automations = [PromptCurationAutomation(trigger_tokens=1_000, room="!other:example.test")]

    await _tick(runner, NOON)
    await _tick(runner, DAY_LATER)

    assert bot.sent[0]["room_id"] == "!other:example.test"


@pytest.mark.asyncio
async def test_a_changed_schedule_on_reload_moves_the_next_run(tmp_path: Path) -> None:
    """Editing the cron reschedules from now instead of keeping the old due time."""
    config, _paths, runner, bot = _setup(tmp_path)
    await _tick(runner, NOON)

    config.agents["mind"].automations = [PromptCurationAutomation(trigger_tokens=1_000, cron="0 0 1 1 *")]
    await _tick(runner, NOON + timedelta(minutes=1))
    await _tick(runner, DAY_LATER)

    assert bot.sent == []


@pytest.mark.asyncio
async def test_the_finished_response_triggers_verify_and_a_notice_in_the_prompt_thread(tmp_path: Path) -> None:
    """Verify runs once, when the run the prompt started is final, and reports in that thread."""
    _config, _paths, runner, bot = _setup(tmp_path)
    await _tick(runner, NOON)
    await _tick(runner, DAY_LATER)

    runner.response_finished(["$unrelated", "$event1"])
    assert await wait_for_background_tasks(5)
    runner.response_finished(["$event1"])
    assert await wait_for_background_tasks(5)

    assert bot.sent[1:] == [
        {
            "room_id": ROOM,
            "body": "Prompt maintenance changed nothing; the files stay at 1286 tokens.",
            "thread_id": "$event1",
            "source_hook": "prompt_curation",
            "extra_content": None,
            "trigger_dispatch": False,
        },
    ]


@pytest.mark.asyncio
async def test_a_run_outside_the_bounds_gets_one_recheck_the_agent_answers(tmp_path: Path) -> None:
    """Findings are posted as a mention in the prompt's thread, run as the internal user, and not verified again."""
    config, paths, runner, bot = _setup(tmp_path)
    await _tick(runner, NOON)
    with patch("mindroom.automations.runner.mindroom_user_id", return_value="@mindroom_user:example.test"):
        await _tick(runner, DAY_LATER)
        root = resolve_agent_runtime("mind", config, paths, None).file_memory_root
        assert root is not None
        (root / "MEMORY.md").write_text("# Memory\n", encoding="utf-8")
        runner.response_finished(["$event1"])
        assert await wait_for_background_tasks(5)
    runner.response_finished(["$event2"])
    assert await wait_for_background_tasks(5)

    assert len(bot.sent) == 2
    recheck = bot.sent[1]
    assert recheck["body"].startswith("@mind ⚠️ Prompt maintenance needs a re-check: MEMORY.md shrank 100%")
    assert recheck["thread_id"] == "$event1"
    assert recheck["trigger_dispatch"] is True
    assert recheck["extra_content"] == {ORIGINAL_SENDER_KEY: "@mindroom_user:example.test"}


async def _finish_run(runner: AutomationRunner, config: Config, paths: RuntimePaths, memory: str | None) -> None:
    """Post the prompt, let the run rewrite MEMORY.md (or leave it), and verify."""
    await _tick(runner, NOON)
    await _tick(runner, DAY_LATER)
    if memory is not None:
        root = resolve_agent_runtime("mind", config, paths, None).file_memory_root
        assert root is not None
        (root / "memory").mkdir(exist_ok=True)
        moved = MEMORY.split("## Topic 7\n")[1]
        (root / "memory" / "topics.md").write_text(moved, encoding="utf-8")
        (root / "MEMORY.md").write_text(memory, encoding="utf-8")
    runner.response_finished(["$event1"])
    assert await wait_for_background_tasks(5)


@pytest.mark.asyncio
async def test_a_run_within_the_bounds_resolves_its_thread(tmp_path: Path) -> None:
    """The bot tags the prompt's thread resolved after the plain notice."""
    config, paths, runner, bot = _setup(tmp_path)
    set_tag = AsyncMock()
    with patch.object(runner_module, "set_thread_tag", set_tag):
        await _finish_run(runner, config, paths, CONDENSED)

    assert bot.sent[1]["body"].startswith("✅ Prompt files condensed from 1286 to ")
    set_tag.assert_awaited_once_with(bot.client, ROOM, "$event1", "resolved", set_by="@mindroom_mind:example.test")


@pytest.mark.asyncio
@pytest.mark.parametrize("memory", [None, "# Memory\n"], ids=["unchanged", "recheck"])
async def test_an_unchanged_run_or_a_recheck_leaves_the_thread_open(tmp_path: Path, memory: str | None) -> None:
    """Only a run that changed the files within the bounds resolves its thread."""
    config, paths, runner, _bot = _setup(tmp_path)
    set_tag = AsyncMock()
    with patch.object(runner_module, "set_thread_tag", set_tag):
        await _finish_run(runner, config, paths, memory)

    set_tag.assert_not_awaited()


@pytest.mark.asyncio
async def test_a_thread_the_bot_cannot_tag_still_gets_its_notice(tmp_path: Path) -> None:
    """A missing power level for thread tags is logged, not raised."""
    config, paths, runner, bot = _setup(tmp_path)
    set_tag = AsyncMock(side_effect=ThreadTagsError("power too low"))
    with (
        patch.object(runner_module, "set_thread_tag", set_tag),
        patch.object(runner_module.logger, "warning") as warning,
    ):
        await _finish_run(runner, config, paths, CONDENSED)

    set_tag.assert_awaited_once()
    assert bot.sent[1]["body"].startswith("✅ ")
    warning.assert_called_once_with("Automation could not resolve its thread", agent="mind", error="power too low")


@pytest.mark.asyncio
async def test_a_configured_model_runs_the_prompt_and_its_recheck(tmp_path: Path) -> None:
    """The automation's model rides on both mentions as the trusted per-run model."""
    config, paths, runner, bot = _setup(tmp_path)
    config.agents["mind"].automations = [PromptCurationAutomation(trigger_tokens=1_000, model="large")]
    await _tick(runner, NOON)
    await _tick(runner, DAY_LATER)
    root = resolve_agent_runtime("mind", config, paths, None).file_memory_root
    assert root is not None
    (root / "MEMORY.md").write_text("# Memory\n", encoding="utf-8")
    runner.response_finished(["$event1"])
    assert await wait_for_background_tasks(5)

    assert [message["extra_content"] for message in bot.sent] == [{SCHEDULED_MODEL_KEY: "large"}] * 2
    assert [message["trigger_dispatch"] for message in bot.sent] == [True, True]


@pytest.mark.asyncio
async def test_a_run_that_never_reports_back_is_verified_after_an_hour(tmp_path: Path) -> None:
    """The fallback verifies a prompt whose response never became final."""
    _config, _paths, runner, bot = _setup(tmp_path)
    await _tick(runner, NOON)
    await _tick(runner, DAY_LATER)

    await _tick(runner, DAY_LATER + timedelta(minutes=59))
    assert len(bot.sent) == 1
    await _tick(runner, DAY_LATER + timedelta(hours=1))

    assert [message["thread_id"] for message in bot.sent] == [None, "$event1"]


@pytest.mark.asyncio
async def test_no_new_prompt_while_the_last_one_awaits_verify(tmp_path: Path) -> None:
    """A frequent schedule never starts a second run on the same files before the first is verified."""
    config, _paths, runner, bot = _setup(tmp_path)
    config.agents["mind"].automations = [PromptCurationAutomation(trigger_tokens=1_000, cron="* * * * *")]
    await _tick(runner, NOON)
    await _tick(runner, NOON + timedelta(minutes=1))
    await _tick(runner, NOON + timedelta(minutes=2))
    assert len(bot.sent) == 1

    runner.response_finished(["$event1"])
    assert await wait_for_background_tasks(5)
    await _tick(runner, NOON + timedelta(minutes=3))

    assert [message["thread_id"] for message in bot.sent] == [None, "$event1", None]


@pytest.mark.asyncio
async def test_files_under_the_trigger_post_nothing(tmp_path: Path) -> None:
    """The check runs in code, so a healthy agent costs no model call and sees no message."""
    _config, _paths, runner, bot = _setup(tmp_path, memory="# Memory\n- Prefers terse replies.\n")

    await _tick(runner, NOON)
    await _tick(runner, DAY_LATER)

    assert bot.sent == []


@pytest.mark.asyncio
async def test_an_agent_without_a_resolvable_room_posts_nothing(tmp_path: Path) -> None:
    """An unknown room alias skips the prompt instead of guessing a room."""
    _config, _paths, runner, bot = _setup(tmp_path, rooms=["not-a-known-alias"])

    await _tick(runner, NOON)
    await _tick(runner, DAY_LATER)

    assert bot.sent == []


@pytest.mark.asyncio
async def test_disabling_an_automation_on_reload_stops_it(tmp_path: Path) -> None:
    """The runner reads the live config, so removing an automation drops its schedule."""
    config, _paths, runner, bot = _setup(tmp_path)
    await _tick(runner, NOON)

    config.agents["mind"].automations = []
    await _tick(runner, DAY_LATER)

    assert bot.sent == []
