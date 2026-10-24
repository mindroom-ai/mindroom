"""Shared scripted model, shell, provider batch, and fake integration for minimal-agent tests."""

from __future__ import annotations

import asyncio
import json
import time
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Any

import pytest

from mindroom import agents
from mindroom.agent_cli.bash import minimal_bash_toolkit
from mindroom.agent_cli.shell_contract import current_agent_cli_shell_env
from mindroom.agent_cli.turn import LiveTurnTools
from mindroom.agent_loop.model_loop import EMPTY_REQUEST, LoopHooks, LoopState
from mindroom.agent_loop.run import run_agent
from mindroom.agent_loop.spec import AgentSpec
from mindroom.agent_loop.tools import PlannedCall, PreparedTools
from mindroom.llm.events import RunFinished
from mindroom.llm.messages import Message
from mindroom.llm.provider import Capabilities, ModelRequest
from mindroom.llm.runs import RunRecord, ToolExecution
from mindroom.runtime_state import clear_api_server_address, set_api_server_address
from tests.conftest import SETTLE_TIMEOUT_SECONDS, install_model_providers
from tests.fakes.scripted_model import SYNTHETIC, ScriptedModel, answer, calls, tool_call

if TYPE_CHECKING:
    from collections.abc import AsyncIterator, Awaitable, Callable, Iterator, Sequence

    from mindroom.agent_cli.session import TurnToolRegistry
    from mindroom.llm.provider import ModelIdentity, NativeHistoryRoute, PortableReplayEstimator, StreamEvent


def cli_window() -> str | None:
    """Return the CLI window of the shell command running here, as `mindroom-agent` sends it."""
    env = current_agent_cli_shell_env()
    return env.window if env is not None else None


@pytest.fixture
def agent_cli_api() -> Iterator[None]:
    """Run like `mindroom run`, whose API server minimal Bash's CLI calls back."""
    set_api_server_address("127.0.0.1", 8765)
    yield
    clear_api_server_address()


def install_scripted_shell(monkeypatch: pytest.MonkeyPatch, run_shell_command: Callable[..., Awaitable[str]]) -> None:
    """Keep each agent's real shell toolkit, but run ``run_shell_command`` in place of its subprocess."""
    original = agents.get_tool_by_name

    def build(name: str, *args: object, **kwargs: object) -> object:
        toolkit = original(name, *args, **kwargs)
        if name == "shell" and "run_shell_command" in toolkit.async_functions:
            toolkit.async_functions["run_shell_command"].entrypoint = run_shell_command
        return toolkit

    monkeypatch.setattr(agents, "get_tool_by_name", build)


def shell_cli_owner(registry: TurnToolRegistry) -> LiveTurnTools:
    """Resolve the response owner from the grant Bash exports, as the API does for `mindroom-agent`."""
    shell_env = current_agent_cli_shell_env()
    assert shell_env is not None
    owner = registry.resolve("Bearer " + shell_env.token, now_ns=time.time_ns())
    assert isinstance(owner, LiveTurnTools)
    return owner


async def settled_receipt(owner: LiveTurnTools, call_id: str) -> dict[str, object]:
    """Poll the call's receipt until it is no longer queued, running, or waiting, as `mindroom-agent` waits for it.

    A call that never settles fails the test after ``SETTLE_TIMEOUT_SECONDS`` with its last receipt.
    """
    receipt: dict[str, object] = {}
    try:
        async with asyncio.timeout(SETTLE_TIMEOUT_SECONDS):
            while (receipt := await owner.get_call(call_id))["status"] in {"queued", "running", "waiting"}:  # noqa: ASYNC110
                await asyncio.sleep(0.001)
    except TimeoutError:
        pytest.fail(f"Call {call_id} did not settle within {SETTLE_TIMEOUT_SECONDS} s; last receipt: {receipt}")
    return receipt


PLUGIN = """from hashlib import sha256
from agno.tools import Toolkit
from agno.tools.function import ToolResult
from agno.media import Image, File
from mindroom.tool_system.declarations import ConfigField, ToolCategory, ToolExecutionTarget, ToolFileAccess
from mindroom.tool_system.registration import register_tool_with_metadata

class ParityTools(Toolkit):
    def __init__(self, api_key: str):
        self.key = api_key
        super().__init__(name="parity", tools=[self.integration, self.approved, self.media])
        self.get_async_functions()["approved"].requires_confirmation = True

    def integration(self, digest: str) -> str:
        assert sha256(self.key.encode()).hexdigest() == digest
        return "primary credential accepted"

    def approved(self, value: str) -> str:
        return "approved:" + value

    def media(self) -> ToolResult:
        import base64
        png = base64.b64decode("iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAQAAAC1HAwCAAAAC0lEQVR42mP8/x8AAwMCAO+jRZkAAAAASUVORK5CYII=")
        return ToolResult(content="image and file", images=[Image(content=png, mime_type="image/png")],
                          files=[File(content=b"same-agent file", mime_type="text/plain", filename="report.txt")])

@register_tool_with_metadata(name="parity", display_name="Local parity", description="Fake local credential integration",
    category=ToolCategory.DEVELOPMENT, default_execution_target=ToolExecutionTarget.PRIMARY,
    file_access=ToolFileAccess.NONE,
    config_fields=[ConfigField(name="api_key", label="API key", type="password", required=True)])
def parity():
    return ParityTools
"""


async def capture_bash_batch(
    owner: LiveTurnTools,
    *bash_calls: tuple[str, dict[str, object]],
    history: Sequence[Message] = (),
) -> None:
    """Hand the owner's checkpoint one provider batch of Bash calls, as the loop does before it runs any of them."""
    bash = minimal_bash_toolkit().functions[0]
    assistant = Message(
        role="assistant",
        tool_calls=[
            {"id": call_id, "type": "function", "function": {"name": "bash", "arguments": json.dumps(arguments)}}
            for call_id, arguments in bash_calls
        ],
    )
    catalog = owner.catalog
    record = catalog.record or RunRecord(
        run_id=catalog.run.run_id,
        session_id=catalog.run.session_id,
        agent_id=catalog.agent_name,
    )
    planned = [
        PlannedCall(
            call_id=call_id,
            function=bash,
            arguments=arguments,
            execution=ToolExecution(tool_call_id=call_id, tool_name="bash", tool_args=arguments),
        )
        for call_id, arguments in bash_calls
    ]
    state = LoopState(
        messages=[*history, assistant],
        offered=PreparedTools(functions={}, instructions=()),
        tools=(),
        run=catalog.run,
        record=record,
        request_index=1,
        loop_start_ids=frozenset(),
        request_template=EMPTY_REQUEST,
    )
    await owner.checkpoint.capture(state, planned)


async def run_bash_batch(owner: LiveTurnTools, *commands: str) -> RunRecord:
    """Run one provider batch of Bash calls ``bash-0``, ``bash-1``, ... through the loop, as a minimal attempt does.

    The batch is checkpointed before its calls run; the run answers "done" unless a call stops it.
    """
    catalog = owner.catalog
    model = ScriptedModel(
        [
            calls(*(tool_call("bash", f"bash-{index}", command=command) for index, command in enumerate(commands))),
            answer("done"),
        ],
    )
    spec = AgentSpec(
        id=catalog.agent_name,
        name=catalog.agent_name,
        model=model,
        system_message=None,
        toolkits=(minimal_bash_toolkit(execute=owner.execute_bash),),
    )
    record: RunRecord | None = None
    events = run_agent(
        spec,
        [Message(role="user", content="run")],
        ctx=catalog.run,
        hooks=LoopHooks(before_dispatch=owner.checkpoint.capture),
    )
    async for event in events:
        if isinstance(event, RunFinished):
            assert isinstance(event.record, RunRecord)
            record = event.record
    assert record is not None
    return record


type Step = str | list[tuple[str, dict[str, object]]]


@dataclass
class ScriptedProvider:
    """Answer each request with the next scripted step, recording every request as the loop sent it.

    A step is the reply text, or the ``(function, arguments)`` tool calls of one assistant message; once the script
    runs out, every request is answered with "done".
    """

    steps: list[Step] = field(default_factory=list)
    requests: list[ModelRequest] = field(default_factory=list)
    identity: ModelIdentity = SYNTHETIC
    capabilities: Capabilities = field(default_factory=Capabilities)
    # A scripted route keeps no provider-side history, sizes replayed history canonically, and records no settings.
    native_history_route: NativeHistoryRoute | None = None
    replay_estimator: PortableReplayEstimator | None = None
    model_params: dict[str, Any] = field(default_factory=dict)

    async def stream(self, request: ModelRequest) -> AsyncIterator[StreamEvent]:
        """Yield the next step as a streamed turn."""
        self.requests.append(request)
        step = self.steps.pop(0) if self.steps else "done"
        turn = (
            answer(step)
            if isinstance(step, str)
            else calls(
                *(
                    tool_call(name, f"provider-{len(self.requests)}-{index}", **arguments)
                    for index, (name, arguments) in enumerate(step)
                ),
            )
        )
        async for event in ScriptedModel(turns=[turn]).stream(request):
            yield event

    def install(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """Serve this model as the configured ``default`` model."""
        install_model_providers(monkeypatch, {"default": self})


def offered_tools(request: ModelRequest) -> list[str]:
    """Return the names of the tools one request offered."""
    return [tool.name for tool in request.tools]
