"""Drive agent replies with the scripted provider: install it, register a probe toolkit, and stop a turn mid-stream."""

from __future__ import annotations

import asyncio
from contextlib import contextmanager
from typing import TYPE_CHECKING, ClassVar

from agno.tools import Toolkit

from mindroom import agents
from mindroom.cancellation import request_task_cancel
from mindroom.custom_tools.tool_payloads import custom_tool_payload
from mindroom.llm.events import TextDelta
from mindroom.session_storage.agent_storage import create_session_storage
from mindroom.tool_system.declarations import ToolFileAccess
from mindroom.tool_system.metadata import ToolCategory
from mindroom.tool_system.registration import register_tool_with_metadata
from mindroom.tool_system.registry_state import capture_tool_registry_snapshot, restore_tool_registry_snapshot
from tests.fakes.scripted_model import Await

if TYPE_CHECKING:
    from collections.abc import Iterable, Iterator

    import pytest

    from mindroom.ai import AIStreamChunk
    from mindroom.cancellation import TaskCancelSource
    from mindroom.config.main import Config
    from mindroom.constants import RuntimePaths
    from mindroom.llm.provider import ModelProvider
    from mindroom.llm.runs import AnyRunRecord


def install_agent_model(monkeypatch: pytest.MonkeyPatch, model: ModelProvider) -> None:
    """Answer every agent request of the test with ``model``, whichever configured model the agent resolves."""
    monkeypatch.setattr(agents, "_build_agent_model_provider", lambda *_args, **_kwargs: model)


def stored_runs(
    config: Config,
    runtime_paths: RuntimePaths,
    session_id: str,
    *,
    agent_name: str = "general",
) -> list[AnyRunRecord]:
    """Return the runs the agent's replies stored in the session, none when the session does not exist."""
    storage = create_session_storage(agent_name, config, runtime_paths, execution_identity=None)
    session = storage.load_session(session_id, "agent")
    return [] if session is None else session.runs


def streamed_text(chunks: Iterable[AIStreamChunk]) -> list[str]:
    """Return the text deltas a reply streamed, in order."""
    return [chunk.text for chunk in chunks if isinstance(chunk, TextDelta)]


@contextmanager
def registered_toolkit(name: str, toolkit: type[Toolkit]) -> Iterator[None]:
    """Register ``toolkit`` as the tool ``name`` for one test and restore every tool registry afterwards.

    Registration also records the tool as a built-in, which later registry resolutions copy back, so the whole
    registry state is restored rather than only the active registry.
    """
    snapshot = capture_tool_registry_snapshot()

    @register_tool_with_metadata(
        name=name,
        file_access=ToolFileAccess.NONE,
        display_name=name,
        description=f"The {name} test toolkit.",
        category=ToolCategory.DEVELOPMENT,
    )
    def factory() -> type[Toolkit]:
        return toolkit

    try:
        yield
    finally:
        restore_tool_registry_snapshot(snapshot)


PROBE_TOOL = "agent_path_probe"


class ProbeTools(Toolkit):
    """Tools an agent-path reply calls: a shell that answers or keeps running, a save that keeps running, and more.

    A call that keeps running sets ``running`` and waits to be cancelled, so a test can stop the turn mid-call.
    ``switch_thread_model`` answers as the thread-model tool does and stops the run after it, as that tool does.
    """

    running: ClassVar[asyncio.Event]

    def __init__(self) -> None:
        super().__init__(
            name="ai_probe",
            tools=[
                self.run_shell_command,
                self.save_file,
                self.read_file,
                self.inspect,
                self.dangerous,
                self.switch_thread_model,
            ],
        )
        self.async_functions["switch_thread_model"].stop_after_tool_call = True

    @classmethod
    async def _keep_running(cls) -> str:
        cls.running.set()
        await asyncio.Event().wait()
        raise AssertionError

    async def run_shell_command(self, cmd: str) -> str:
        """Run one shell command; ``pwd`` answers at once and anything else keeps running.

        Args:
            cmd: The command.

        """
        return "/app" if cmd == "pwd" else await self._keep_running()

    async def save_file(self, file_name: str) -> str:
        """Save a file; the save keeps running.

        Args:
            file_name: The file.

        """
        del file_name
        return await self._keep_running()

    async def read_file(self, path: str) -> str:
        """Read a file.

        Args:
            path: The file.

        """
        return f"contents of {path}"

    async def inspect(self, path: str) -> str:
        """Inspect a file.

        Args:
            path: The file.

        """
        del path
        return "ready"

    async def dangerous(self, value: int) -> str:
        """Do something that needs approval.

        Args:
            value: The value.

        """
        return f"did {value}"

    async def switch_thread_model(self, model_name: str, when: str = "next-turn") -> str:
        """Switch the thread's model.

        Args:
            model_name: The configured model.
            when: When the switch takes effect.

        """
        if model_name not in {"default", "large"}:
            return custom_tool_payload(
                "thread_model",
                "error",
                action="switch",
                message=f"Unknown model '{model_name}'.",
            )
        return custom_tool_payload("thread_model", "ok", action="switch", model=model_name, when=when)


@contextmanager
def registered_probe_tools() -> Iterator[type[ProbeTools]]:
    """Register ``ProbeTools`` as ``PROBE_TOOL`` for one test, with a fresh ``running`` event."""
    ProbeTools.running = asyncio.Event()
    with registered_toolkit(PROBE_TOOL, ProbeTools):
        yield ProbeTools


def raising(error: BaseException) -> Await:
    """A scripted step that fails the request with ``error``, as a provider or a cancellation does mid-stream."""

    async def raise_it() -> None:
        raise error

    return Await(raise_it)


def stop_turn(cancel_source: TaskCancelSource | None = None) -> Await:
    """A scripted step that cancels the task running the model, as a stop or a restart does mid-stream."""

    async def stop() -> None:
        task = asyncio.current_task()
        assert task is not None
        request_task_cancel(task, cancel_source=cancel_source)
        await asyncio.sleep(0)

    return Await(stop)


__all__ = [
    "PROBE_TOOL",
    "ProbeTools",
    "install_agent_model",
    "raising",
    "registered_probe_tools",
    "registered_toolkit",
    "stop_turn",
    "stored_runs",
    "streamed_text",
]
