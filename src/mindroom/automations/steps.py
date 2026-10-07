"""What an automation's check receives, and the two step types it returns to the runner."""

from __future__ import annotations

from dataclasses import dataclass
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from collections.abc import Callable, Mapping
    from pathlib import Path

    from mindroom.config.automations import Automation
    from mindroom.config.main import Config
    from mindroom.constants import RuntimePaths


@dataclass(frozen=True)
class AutomationContext:
    """Everything an automation's check gets when its cron fires for one agent.

    ``config`` is the config at fire time; later steps receive the current one.
    ``entry`` is the agent's entry for this automation, ``options`` its ``options`` (empty for a built-in), and
    ``settings`` the plugin's own ``settings`` (empty for a built-in).
    ``options`` and ``settings`` are read-only.
    ``workspace`` is the agent's workspace root, which may not exist yet, or None for an agent without file memory,
    which has no workspace, and
    ``state_dir`` is the agent's automation state directory in primary storage, outside the workspace, created when
    something first writes there.
    """

    agent_name: str
    config: Config
    runtime_paths: RuntimePaths
    entry: Automation
    options: Mapping[str, Any]
    settings: Mapping[str, Any]
    workspace: Path | None
    state_dir: Path


@dataclass(frozen=True)
class Ask:
    """Post ``text`` mentioning the agent so it answers with a normal visible run.

    ``new_thread`` starts a thread, and a session, of its own; otherwise the text goes to the previous prompt's thread.
    When the run's response is final, or after the fallback timeout, the runner calls ``then`` off the event loop
    with the current config, the thread the text was posted in, and whether the timeout fired.
    Without ``then``, the chain ends once the run does.
    """

    text: str
    new_thread: bool
    then: Callable[[Config, str, bool], Ask | Done] | None = None


@dataclass(frozen=True)
class Done:
    """End the chain: post ``notice`` in the last prompt's thread without mentioning the agent, and resolve threads."""

    notice: str
    resolve: tuple[str, ...] = ()
    # Called on the event loop, for work that must be scheduled there, such as a background re-index.
    on_loop: Callable[[], None] | None = None


# Every message the runner posts carries this hook source prefix, so the turns it starts are known as automation turns.
AUTOMATION_HOOK_PREFIX = "automation/"


def is_automation_hook_source(hook_source: str | None) -> bool:
    """Return whether a trusted message's hook source marks a turn an automation started."""
    return hook_source is not None and hook_source.startswith(AUTOMATION_HOOK_PREFIX)
