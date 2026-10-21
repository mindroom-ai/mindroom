"""Drive agent replies with the scripted provider: install it, register a probe toolkit, and stop a turn mid-stream."""

from __future__ import annotations

import asyncio
from contextlib import contextmanager
from typing import TYPE_CHECKING

from mindroom import agents
from mindroom.cancellation import request_task_cancel
from mindroom.tool_system.declarations import ToolFileAccess
from mindroom.tool_system.metadata import TOOL_METADATA, TOOL_REGISTRY, ToolCategory
from mindroom.tool_system.registration import register_tool_with_metadata
from tests.fakes.scripted_model import Await

if TYPE_CHECKING:
    from collections.abc import Iterator

    import pytest
    from agno.tools import Toolkit

    from mindroom.cancellation import TaskCancelSource
    from mindroom.llm.provider import ModelProvider


def install_agent_model(monkeypatch: pytest.MonkeyPatch, model: ModelProvider) -> None:
    """Answer every agent request of the test with ``model``, whichever configured model the agent resolves."""
    monkeypatch.setattr(agents, "_build_agent_model_provider", lambda *_args, **_kwargs: model)


@contextmanager
def registered_toolkit(name: str, toolkit: type[Toolkit]) -> Iterator[None]:
    """Register ``toolkit`` as the tool ``name`` for one test and restore the registries afterwards."""
    original_registry = TOOL_REGISTRY.copy()
    original_metadata = TOOL_METADATA.copy()

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
        TOOL_REGISTRY.clear()
        TOOL_REGISTRY.update(original_registry)
        TOOL_METADATA.clear()
        TOOL_METADATA.update(original_metadata)


def stop_turn(cancel_source: TaskCancelSource | None = None) -> Await:
    """A scripted step that cancels the task running the model, as a stop or a restart does mid-stream."""

    async def stop() -> None:
        task = asyncio.current_task()
        assert task is not None
        request_task_cancel(task, cancel_source=cancel_source)
        await asyncio.sleep(0)

    return Await(stop)


__all__ = ["install_agent_model", "registered_toolkit", "stop_turn"]
