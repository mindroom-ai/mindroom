"""What an automation's check receives, and the two step types it returns to the runner."""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import PurePosixPath
from typing import TYPE_CHECKING, Any

from mindroom.tool_system.worker_routing import build_tool_execution_identity

if TYPE_CHECKING:
    from collections.abc import Callable, Mapping
    from pathlib import Path

    from mindroom.config.automations import Automation
    from mindroom.config.main import Config
    from mindroom.constants import RuntimePaths
    from mindroom.tool_system.worker_routing import ToolExecutionIdentity


def automation_execution_identity(
    runtime_paths: RuntimePaths,
    agent_name: str,
    requester_id: str | None,
) -> ToolExecutionIdentity | None:
    """Return the identity a requester's turns resolve their private instance of the agent with, or None for a shared agent."""
    if requester_id is None:
        return None
    return build_tool_execution_identity(
        channel="matrix",
        agent_name=agent_name,
        runtime_paths=runtime_paths,
        requester_id=requester_id,
        room_id=None,
        thread_id=None,
        resolved_thread_id=None,
        session_id=None,
    )


def workspace_context_files(config: Config, agent_name: str) -> list[str]:
    """Return the always-loaded context files in the workspace an automation maintains, as workspace-relative paths.

    A private agent's are its ``private.context_files``; its agent-level ``context_files`` load from the shared agent
    workspace that every requester reads, which no requester's run maintains.
    """
    agent = config.get_agent(agent_name)
    paths = (agent.private.context_files or []) if agent.private is not None else agent.context_files
    return [PurePosixPath(path).as_posix() for path in paths]


@dataclass(frozen=True)
class AutomationContext:
    """Everything an automation's check gets when its cron fires for one agent, or one requester's private instance.

    ``config`` is the config at fire time; later steps receive the current one.
    ``entry`` is the agent's entry for this automation, ``options`` its ``options`` (empty for a built-in), and
    ``settings`` the plugin's own ``settings`` (empty for a built-in).
    ``options`` and ``settings`` are read-only.
    ``requester_id`` is the requester whose private instance of a private agent this run maintains, or None for a shared
    agent.
    ``workspace`` is the workspace root, which may not exist yet: a shared agent's, or None for a shared agent without
    file memory, which has no workspace, and for a private agent the requester's private root, and
    ``state_dir`` is the automation state directory of the agent or private instance in primary storage, outside the
    workspace, which the automation creates when it first writes there.
    """

    agent_name: str
    config: Config
    runtime_paths: RuntimePaths
    entry: Automation
    options: Mapping[str, Any]
    settings: Mapping[str, Any]
    requester_id: str | None
    workspace: Path | None
    state_dir: Path

    @property
    def execution_identity(self) -> ToolExecutionIdentity | None:
        """Return the identity memory and knowledge helpers resolve this run's private instance with, or None."""
        return automation_execution_identity(self.runtime_paths, self.agent_name, self.requester_id)


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
