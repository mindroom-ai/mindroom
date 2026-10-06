"""The automation runner: cron timing, the visible prompt it posts, and the verify step that follows the run."""

from __future__ import annotations

import threading
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from typing import TYPE_CHECKING, Any, cast
from unittest.mock import patch

import pytest

from mindroom.automations import runner as runner_module
from mindroom.automations.prompt_curation import CurationPlan, verify_curation
from mindroom.automations.runner import AutomationRunner
from mindroom.background_tasks import wait_for_background_tasks
from mindroom.config.agent import AgentConfig
from mindroom.config.automations import PromptCurationAutomation
from mindroom.config.main import Config
from mindroom.config.models import RouterConfig
from mindroom.constants import ORIGINAL_SENDER_KEY, resolve_runtime_paths
from mindroom.runtime_resolution import resolve_agent_runtime

if TYPE_CHECKING:
    from pathlib import Path

    from mindroom.bot import AgentBot
    from mindroom.constants import RuntimePaths

ROOM = "!room:example.test"
# Eight 642-character sections: 1,286 tokens, over a 1,000-token trigger.
MEMORY = "# Memory\n" + "".join(f"## Topic {index}\n" + f"Detail {index} " * 70 + "\n" for index in range(8))
# Noon UTC; the default 04:00 schedule in the config's timezone is next due within a day.
NOON = datetime(2026, 10, 5, 12, 0, tzinfo=UTC)
DAY_LATER = NOON + timedelta(days=1)


@dataclass
class _Bot:
    """The agent bot as the runner sees it, recording every hook message it sends."""

    sent: list[dict[str, Any]] = field(default_factory=list)

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
async def test_no_new_prompt_while_verify_is_still_running(tmp_path: Path) -> None:
    """The automation stays busy until verify ends, so a new pass never starts before the last one is reported."""
    config, _paths, runner, bot = _setup(tmp_path)
    config.agents["mind"].automations = [PromptCurationAutomation(trigger_tokens=1_000, cron="* * * * *")]
    await _tick(runner, NOON)
    await _tick(runner, NOON + timedelta(minutes=1))
    release = threading.Event()

    def slow_verify(plan: CurationPlan) -> object:
        release.wait(5)
        return verify_curation(plan)

    with patch.object(runner_module, "verify_curation", slow_verify):
        runner.response_finished(["$event1"])
        await runner._tick(NOON + timedelta(minutes=3))
        assert len(bot.sent) == 1
        release.set()
        assert await wait_for_background_tasks(5)

    await _tick(runner, NOON + timedelta(minutes=4))
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
