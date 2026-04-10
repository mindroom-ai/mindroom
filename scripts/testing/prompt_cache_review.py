"""Inspect MindRoom's Vertex Claude prompt-cache request shape.

This harness exercises the production memory-prompt assembly path and the
Agno/Vertex request formatter without making a network call.
"""

from __future__ import annotations

import asyncio
import importlib.util
import sys
import types
from contextlib import contextmanager
from dataclasses import dataclass
from pathlib import Path
from tempfile import TemporaryDirectory
from types import SimpleNamespace
from typing import Any, Iterator

from agno.agent import Agent
from agno.agent._messages import get_run_messages
from agno.models.vertexai.claude import Claude as VertexAIClaude
from agno.run import RunContext
from agno.run.agent import RunOutput
from agno.session.agent import AgentSession
from agno.utils.models.claude import format_messages

from mindroom.constants import RuntimePaths, resolve_runtime_paths

_REPO_ROOT = Path(__file__).resolve().parents[2]
_SRC_ROOT = _REPO_ROOT / "src"
_PROBE_MEMORY_PACKAGE = "mindroom.memory"


@dataclass(frozen=True)
class ProbeModules:
    """Loaded production source modules used by the harness."""

    agent_config_type: type[object]
    functions: types.ModuleType
    file_backend: types.ModuleType
    policy: types.ModuleType
    prompting: types.ModuleType


@dataclass(frozen=True)
class RequestArtifacts:
    """One constructed Vertex request plus the prompt text that produced it."""

    prompt: str
    system_prompt: str
    full_prompt: str
    payload: dict[str, Any]

    @property
    def user_blocks(self) -> list[dict[str, Any]]:
        return list(self.payload["messages"][0]["content"])

    @property
    def first_user_text(self) -> str:
        return str(self.user_blocks[0]["text"])


@dataclass(frozen=True)
class BlockPrefixSummary:
    """Stable prefix summary using Anthropic content-block boundaries."""

    stable_system_blocks: int
    stable_system_text_chars: int
    stable_message_blocks: int
    stable_message_text_chars: int
    first_diverging_message_index: int | None
    first_diverging_content_index: int | None


@dataclass
class _FakeConfig:
    """Minimal config surface needed by the production memory path."""

    agent_config_type: type[object]

    def __post_init__(self) -> None:
        self.agents = {"general": self.agent_config_type(display_name="General Agent")}
        self.teams: dict[str, object] = {}
        self.defaults = SimpleNamespace(worker_scope=None)
        self.memory = SimpleNamespace(
            backend="file",
            team_reads_member_memory=False,
            file=SimpleNamespace(path=None, max_entrypoint_lines=200),
        )
        self.PRIVATE_KNOWLEDGE_BASE_ID_PREFIX = "__agent_private__:"

    def get_agent(self, agent_name: str) -> object:
        return self.agents[agent_name]

    def get_agent_memory_backend(self, agent_name: str) -> str:
        del agent_name
        return "file"

    def assert_team_agents_supported(self, agent_names: list[str], team_name: str | None = None) -> None:
        del agent_names, team_name


class PromptCacheHarness:
    """Build request payloads for the current and split-entrypoint variants."""

    def __init__(
        self,
        *,
        modules: ProbeModules,
        runtime_paths: RuntimePaths,
        config: _FakeConfig,
        system_prompt: str,
    ) -> None:
        self.modules = modules
        self.runtime_paths = runtime_paths
        self.config = config
        self.system_prompt = system_prompt
        self.storage_path = runtime_paths.storage_root
        self.agent_name = "general"

    def write_memory_markdown(self, markdown: str) -> None:
        resolution = self.modules.policy.resolve_file_memory_resolution(
            self.storage_path,
            self.config,
            self.runtime_paths,
            agent_name=self.agent_name,
        )
        scope_path = self.modules.file_backend._scope_dir(  # noqa: SLF001
            self.modules.policy.agent_scope_user_id(self.agent_name),
            resolution,
            self.config,
            create=True,
        )
        (scope_path / "MEMORY.md").write_text(markdown, encoding="utf-8")

    def current_request(self, prompt: str) -> RequestArtifacts:
        full_prompt = asyncio.run(
            self.modules.functions.build_memory_enhanced_prompt(
                prompt,
                self.agent_name,
                self.storage_path,
                self.config,
                self.runtime_paths,
            )
        )
        return RequestArtifacts(
            prompt=prompt,
            system_prompt=self.system_prompt,
            full_prompt=full_prompt,
            payload=_build_vertex_payload(
                system_prompt=self.system_prompt,
                user_prompt=full_prompt,
            ),
        )

    def split_entrypoint_request(self, prompt: str) -> RequestArtifacts:
        memories = asyncio.run(
            self.modules.functions.search_agent_memories(
                prompt,
                self.agent_name,
                self.storage_path,
                self.config,
                self.runtime_paths,
            )
        )
        resolution = self.modules.policy.resolve_file_memory_resolution(
            self.storage_path,
            self.config,
            self.runtime_paths,
            agent_name=self.agent_name,
        )
        entrypoint = self.modules.file_backend.load_scope_entrypoint_context(
            self.modules.policy.agent_scope_user_id(self.agent_name),
            resolution,
            self.config,
        )
        user_chunks: list[str] = []
        if memories:
            user_chunks.append(self.modules.prompting._format_memories_as_context(memories, "agent file"))  # noqa: SLF001
        user_chunks.append(prompt)
        user_prompt = "\n\n".join(chunk for chunk in user_chunks if chunk)
        system_prompt = self.system_prompt
        if entrypoint:
            system_prompt = f"{system_prompt}\n\n[File memory entrypoint (agent)]\n{entrypoint}"
        return RequestArtifacts(
            prompt=prompt,
            system_prompt=system_prompt,
            full_prompt=user_prompt,
            payload=_build_vertex_payload(
                system_prompt=system_prompt,
                user_prompt=user_prompt,
            ),
        )


def summarize_block_prefix(first: RequestArtifacts, second: RequestArtifacts) -> BlockPrefixSummary:
    """Return the stable prompt prefix using Anthropic content-block boundaries."""

    stable_system_blocks = 0
    stable_system_text_chars = 0
    for first_block, second_block in zip(first.payload.get("system", ()), second.payload.get("system", ()), strict=False):
        if first_block != second_block:
            break
        stable_system_blocks += 1
        stable_system_text_chars += len(str(first_block.get("text", "")))

    stable_message_blocks = 0
    stable_message_text_chars = 0
    first_diverging_message_index: int | None = None
    first_diverging_content_index: int | None = None
    first_messages = list(first.payload["messages"])
    second_messages = list(second.payload["messages"])
    for message_index, (first_message, second_message) in enumerate(
        zip(first_messages, second_messages, strict=False),
    ):
        if first_message["role"] != second_message["role"]:
            first_diverging_message_index = message_index
            first_diverging_content_index = 0
            break
        first_blocks = list(first_message["content"])
        second_blocks = list(second_message["content"])
        for content_index, (first_block, second_block) in enumerate(zip(first_blocks, second_blocks, strict=False)):
            if first_block != second_block:
                first_diverging_message_index = message_index
                first_diverging_content_index = content_index
                return BlockPrefixSummary(
                    stable_system_blocks=stable_system_blocks,
                    stable_system_text_chars=stable_system_text_chars,
                    stable_message_blocks=stable_message_blocks,
                    stable_message_text_chars=stable_message_text_chars,
                    first_diverging_message_index=first_diverging_message_index,
                    first_diverging_content_index=first_diverging_content_index,
                )
            stable_message_blocks += 1
            stable_message_text_chars += len(str(first_block.get("text", "")))
        if len(first_blocks) != len(second_blocks):
            first_diverging_message_index = message_index
            first_diverging_content_index = min(len(first_blocks), len(second_blocks))
            break
    else:
        if len(first_messages) != len(second_messages):
            first_diverging_message_index = min(len(first_messages), len(second_messages))
            first_diverging_content_index = 0

    return BlockPrefixSummary(
        stable_system_blocks=stable_system_blocks,
        stable_system_text_chars=stable_system_text_chars,
        stable_message_blocks=stable_message_blocks,
        stable_message_text_chars=stable_message_text_chars,
        first_diverging_message_index=first_diverging_message_index,
        first_diverging_content_index=first_diverging_content_index,
    )


@contextmanager
def prompt_cache_harness(
    *,
    system_prompt: str = "SYSTEM",
) -> Iterator[PromptCacheHarness]:
    """Yield a harness rooted in one isolated runtime directory."""

    modules = _load_probe_modules()
    config = _FakeConfig(agent_config_type=modules.agent_config_type)
    with TemporaryDirectory() as tmp_dir:
        tmp_root = Path(tmp_dir)
        runtime_paths = resolve_runtime_paths(
            config_path=tmp_root / "config.yaml",
            storage_path=tmp_root / "mindroom_data",
            process_env={
                "MATRIX_HOMESERVER": "http://localhost:8008",
                "MINDROOM_NAMESPACE": "",
            },
        )
        yield PromptCacheHarness(
            modules=modules,
            runtime_paths=runtime_paths,
            config=config,
            system_prompt=system_prompt,
        )


def main() -> None:
    """Print one concise review of the prompt-cache hypothesis."""

    memory_markdown = (
        "# Memory\n\n"
        "Stable workspace context.\n"
        "- [id=python] Python backend uses FastAPI and pydantic.\n"
        "- [id=javascript] JavaScript frontend uses Next.js and React.\n"
    )
    with prompt_cache_harness() as harness:
        harness.write_memory_markdown(memory_markdown)
        current_python = harness.current_request("How does the Python backend work?")
        current_js = harness.current_request("How does the JavaScript frontend work?")
        split_python = harness.split_entrypoint_request("How does the Python backend work?")
        split_js = harness.split_entrypoint_request("How does the JavaScript frontend work?")

        current_summary = summarize_block_prefix(current_python, current_js)
        split_summary = summarize_block_prefix(split_python, split_js)

        print("Current production request shape")
        print(f"- system blocks: {len(current_python.payload.get('system', []))}")
        print(f"- user blocks in first message: {len(current_python.user_blocks)}")
        print(
            "- first user block contains entrypoint / searched memories / prompt: "
            f"{'[File memory entrypoint (agent)]' in current_python.first_user_text} / "
            f"{'[Automatically extracted agent file memories' in current_python.first_user_text} / "
            f"{current_python.prompt in current_python.first_user_text}"
        )
        print()
        print("Stable block prefix across two different prompts")
        print(
            "- current production path: "
            f"system blocks={current_summary.stable_system_blocks}, "
            f"user blocks={current_summary.stable_message_blocks}, "
            f"stable system chars={current_summary.stable_system_text_chars}"
        )
        print(
            "- split-entrypoint variant: "
            f"system blocks={split_summary.stable_system_blocks}, "
            f"user blocks={split_summary.stable_message_blocks}, "
            f"stable system chars={split_summary.stable_system_text_chars}"
        )


def _build_vertex_payload(*, system_prompt: str, user_prompt: str) -> dict[str, Any]:
    """Build the actual Anthropic/Vertex request body right before network I/O."""

    model = VertexAIClaude(
        id="claude-sonnet-4-6",
        cache_system_prompt=True,
    )
    agent = Agent(
        model=model,
        system_message=system_prompt,
    )
    run_messages = get_run_messages(
        agent,
        run_response=RunOutput(run_id="prompt-cache-review"),
        run_context=RunContext(
            run_id="prompt-cache-review",
            session_id="prompt-cache-review-session",
        ),
        input=user_prompt,
        session=AgentSession(session_id="prompt-cache-review-session"),
        user_id="user-1",
        add_history_to_context=True,
        add_dependencies_to_context=False,
        add_session_state_to_context=False,
        tools=[],
    )
    chat_messages, system_message = format_messages(run_messages.messages)
    request_kwargs = model._prepare_request_kwargs(
        system_message,
        tools=[],
        messages=run_messages.messages,
    )
    return {
        "model": model.id,
        "messages": chat_messages,
        **request_kwargs,
    }


def _load_probe_modules() -> ProbeModules:
    """Load the production memory source files under an isolated probe package."""

    if str(_SRC_ROOT) not in sys.path:
        sys.path.insert(0, str(_SRC_ROOT))

    if (loaded := _existing_probe_modules()) is not None:
        return loaded

    config_agent_module = types.ModuleType("mindroom.config.agent")

    class AgentConfig:
        def __init__(self) -> None:
            self.private = None
            self.delegate_to: list[str] = []
            self.worker_scope = None

    class CultureConfig:  # noqa: D401
        """Placeholder type used only to satisfy imports."""

    class TeamConfig:  # noqa: D401
        """Placeholder type used only to satisfy imports."""

    config_agent_module.AgentConfig = AgentConfig
    config_agent_module.CultureConfig = CultureConfig
    config_agent_module.TeamConfig = TeamConfig
    sys.modules["mindroom.config.agent"] = config_agent_module

    probe_memory_package = types.ModuleType(_PROBE_MEMORY_PACKAGE)
    probe_memory_package.__path__ = [str(_SRC_ROOT / "mindroom" / "memory")]
    sys.modules[_PROBE_MEMORY_PACKAGE] = probe_memory_package

    config_stub = types.ModuleType(f"{_PROBE_MEMORY_PACKAGE}.config")

    async def create_memory_instance(*args: object, **kwargs: object) -> None:
        raise AssertionError("Mem0 path should not be used in this file-memory harness")

    config_stub.create_memory_instance = create_memory_instance
    sys.modules[f"{_PROBE_MEMORY_PACKAGE}.config"] = config_stub

    mem0_stub = types.ModuleType(f"{_PROBE_MEMORY_PACKAGE}._mem0_backend")

    async def _unused_mem0(*args: object, **kwargs: object) -> None:
        raise AssertionError("Mem0 path should not be used in this file-memory harness")

    for name in (
        "add_mem0_agent_memory",
        "delete_mem0_agent_memory",
        "get_mem0_agent_memory",
        "list_mem0_agent_memories",
        "search_mem0_agent_memories",
        "store_mem0_conversation_memory",
        "update_mem0_agent_memory",
    ):
        setattr(mem0_stub, name, _unused_mem0)
    sys.modules[f"{_PROBE_MEMORY_PACKAGE}._mem0_backend"] = mem0_stub

    _load_probe_source_module(f"{_PROBE_MEMORY_PACKAGE}._shared", _SRC_ROOT / "mindroom" / "memory" / "_shared.py")
    policy = _load_probe_source_module(
        f"{_PROBE_MEMORY_PACKAGE}._policy",
        _SRC_ROOT / "mindroom" / "memory" / "_policy.py",
    )
    file_backend = _load_probe_source_module(
        f"{_PROBE_MEMORY_PACKAGE}._file_backend",
        _SRC_ROOT / "mindroom" / "memory" / "_file_backend.py",
    )
    prompting = _load_probe_source_module(
        f"{_PROBE_MEMORY_PACKAGE}._prompting",
        _SRC_ROOT / "mindroom" / "memory" / "_prompting.py",
    )
    functions = _load_probe_source_module(
        f"{_PROBE_MEMORY_PACKAGE}.functions",
        _SRC_ROOT / "mindroom" / "memory" / "functions.py",
    )
    return ProbeModules(
        agent_config_type=AgentConfig,
        functions=functions,
        file_backend=file_backend,
        policy=policy,
        prompting=prompting,
    )


def _existing_probe_modules() -> ProbeModules | None:
    """Return cached probe modules if they were already loaded."""

    functions = sys.modules.get(f"{_PROBE_MEMORY_PACKAGE}.functions")
    file_backend = sys.modules.get(f"{_PROBE_MEMORY_PACKAGE}._file_backend")
    policy = sys.modules.get(f"{_PROBE_MEMORY_PACKAGE}._policy")
    prompting = sys.modules.get(f"{_PROBE_MEMORY_PACKAGE}._prompting")
    config_agent_module = sys.modules.get("mindroom.config.agent")
    if not all((functions, file_backend, policy, prompting, config_agent_module)):
        return None
    return ProbeModules(
        agent_config_type=config_agent_module.AgentConfig,
        functions=functions,
        file_backend=file_backend,
        policy=policy,
        prompting=prompting,
    )


def _load_probe_source_module(module_name: str, source_path: Path) -> types.ModuleType:
    spec = importlib.util.spec_from_file_location(module_name, source_path)
    if spec is None or spec.loader is None:
        msg = f"Could not load probe module from {source_path}"
        raise RuntimeError(msg)
    module = importlib.util.module_from_spec(spec)
    sys.modules[module_name] = module
    spec.loader.exec_module(module)
    return module


if __name__ == "__main__":
    main()
