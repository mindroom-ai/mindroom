"""Background prompt curation through real staging, persistence, and Agno tool loops with a scripted model."""

from __future__ import annotations

import ast
import asyncio
import json
from dataclasses import dataclass, field
from pathlib import Path
from typing import TYPE_CHECKING, Any
from unittest.mock import patch

import pytest
import structlog
from agno.models.message import Message, MessageMetrics
from agno.models.response import ModelResponse

import mindroom.prompt_curation as prompt_curation_package
from mindroom.config.agent import AgentConfig, AgentPrivateConfig
from mindroom.config.main import Config
from mindroom.config.models import ModelConfig
from mindroom.config.prompt_curation import AgentPromptCurationConfig, PromptCurationConfig
from mindroom.constants import resolve_runtime_paths
from mindroom.llm_request_logging import current_llm_request_log_context
from mindroom.prompt_curation import runner as runner_module
from mindroom.prompt_curation.runner import PromptCurationRunner
from mindroom.runtime_resolution import resolve_agent_runtime
from mindroom.synthetic_model import SyntheticModel
from mindroom.tool_system.worker_routing import ToolExecutionIdentity
from mindroom.usage_stats import collect_admin_usage

if TYPE_CHECKING:
    from collections.abc import Callable, Mapping, Sequence

    from mindroom.constants import RuntimePaths

_Call = tuple[str, dict[str, object]]

HEADER = "# Memory\n"
# Eight 642-character sections: MEMORY.md is 1,286 tokens, over a 1,000-token trigger.
SECTIONS = [f"## Topic {index}\n" + f"Detail {index} " * 70 + "\n" for index in range(8)]
MEMORY = HEADER + "".join(SECTIONS)
POINTER = "## Topic 3\nSee memory/topics.md\n"
FILLER = "Filler line. " * 23 + "\n"
EXTRA = "## Extra\n" + "Old trip notes. " * 15 + "\n"
ALICE = ToolExecutionIdentity(
    channel="matrix",
    agent_name="mind",
    requester_id="@alice:example.test",
    room_id="!room:example.test",
    thread_id="$thread",
    resolved_thread_id="$thread",
    session_id="session",
)


@dataclass
class _ScriptedModel(SyntheticModel):
    """Provider double that plays a fixed tool-call script, then answers."""

    script: list[list[_Call]] = field(default_factory=list)
    requests: list[list[str]] = field(default_factory=list)
    log_contexts: list[dict[str, object]] = field(default_factory=list)
    request_log_contexts: list[dict[str, object]] = field(default_factory=list)
    before_answer: Callable[[], None] | None = None
    failure: Exception | None = None
    # Requests after this many wait for ``release``, setting ``blocked`` first.
    released_requests: int | None = None
    release: asyncio.Event = field(default_factory=asyncio.Event)
    blocked: asyncio.Event = field(default_factory=asyncio.Event)

    async def ainvoke(
        self,
        messages: list[Message],
        *,
        tools: Sequence[Mapping[str, Any]] | None = None,  # noqa: ARG002
        **_kwargs: object,
    ) -> ModelResponse:
        self.requests.append([message.get_content_string() for message in messages])
        self.log_contexts.append(structlog.contextvars.get_contextvars())
        self.request_log_contexts.append(current_llm_request_log_context())
        if self.failure is not None:
            raise self.failure
        if self.released_requests is not None and len(self.requests) > self.released_requests:
            self.blocked.set()
            await self.release.wait()
        usage = MessageMetrics(input_tokens=100, output_tokens=5, total_tokens=105)
        if not self.script:
            if self.before_answer is not None:
                self.before_answer()
            return ModelResponse(content="Moved one section.", response_usage=usage)
        calls = [
            {
                "id": f"call-{len(self.requests)}-{index}",
                "type": "function",
                "function": {"name": name, "arguments": json.dumps(arguments)},
            }
            for index, (name, arguments) in enumerate(self.script.pop(0))
        ]
        return ModelResponse(content="", tool_calls=calls, response_usage=usage)


def _model(*script: list[_Call]) -> _ScriptedModel:
    return _ScriptedModel(id="scripted", name="scripted", provider="test", script=list(script))


def _setup(
    tmp_path: Path,
    *,
    memory: str = MEMORY,
    private: bool = False,
    enabled: bool = True,
    timeout_seconds: int | None = None,
) -> tuple[Config, RuntimePaths, Path]:
    agent = AgentConfig(
        display_name="Mind",
        memory_backend="file",
        prompt_curation=AgentPromptCurationConfig(
            trigger_tokens=1_000,
            enabled=enabled,
            timeout_seconds=timeout_seconds,
        ),
        private=AgentPrivateConfig(per="user") if private else None,
    )
    config = Config(agents={"mind": agent}, models={"default": ModelConfig(provider="openai", id="gpt-6-astra")})
    paths = resolve_runtime_paths(config_path=tmp_path / "config.yaml", storage_path=tmp_path)
    root = resolve_agent_runtime("mind", config, paths, ALICE if private else None, create=True).file_memory_root
    assert root is not None
    root.mkdir(parents=True, exist_ok=True)
    (root / "MEMORY.md").write_text(memory, encoding="utf-8")
    (root / "SOUL.md").write_text("Be kind.\n", encoding="utf-8")
    return config, paths, root


def _snapshot(root: Path) -> dict[str, bytes]:
    return {str(path.relative_to(root)): path.read_bytes() for path in sorted(root.rglob("*")) if path.is_file()}


def _state(paths: RuntimePaths) -> dict[str, Any]:
    return json.loads((paths.storage_root / "prompt_curation_state.json").read_text())["scopes"]


def _usage_kinds(config: Config, paths: RuntimePaths) -> set[str]:
    report = collect_admin_usage(config=config, runtime_paths=paths, include_requests=True)
    return {row.kind for row in report.request_breakdown or []}


async def _curate(
    runner: PromptCurationRunner,
    config: Config,
    identity: ToolExecutionIdentity | None = None,
) -> str | None:
    task = runner.maybe_start(config, agent_name="mind", session_id="session", identity=identity)
    return None if task is None else await task


MOVE = [
    ("append_file", {"path": "memory/topics.md", "content": SECTIONS[3]}),
    ("edit_file", {"path": "MEMORY.md", "old_text": SECTIONS[3], "new_text": POINTER}),
]


@pytest.mark.asyncio
async def test_moving_detail_into_memory_within_bounds_is_published(tmp_path: Path) -> None:
    """A pass that moves one section verbatim into memory/ and leaves a pointer lands in its band and is published."""
    config, paths, root = _setup(tmp_path)
    model = _model(MOVE)

    with patch("mindroom.model_loading.get_model_instance", return_value=model):
        outcome = await _curate(PromptCurationRunner(paths), config)

    assert outcome == "accepted"
    curated = (root / "MEMORY.md").read_text()
    assert curated == MEMORY.replace(SECTIONS[3], POINTER)
    assert (root / "memory" / "topics.md").read_text() == SECTIONS[3]
    assert (root / "SOUL.md").read_text() == "Be kind.\n"
    assert 0.85 * len(MEMORY) <= len(curated) <= 0.9 * len(MEMORY)
    assert _usage_kinds(config, paths) == {"prompt_curation"}
    assert _state(paths)["mind"] == {
        "active": True,
        "consecutive_failures": 0,
        "last_attempt_at": _state(paths)["mind"]["last_attempt_at"],
        "last_outcome": "accepted",
    }
    prompt = model.requests[0][-1]
    assert "total 1286 tokens" in prompt
    assert "at most 1157 tokens, but not below 1093" in prompt
    assert SECTIONS[7] in prompt
    assert (
        "Edited MEMORY.md. Curated files now 1133 tokens (target at most 1157, not below 1093); "
        "net memory content removed 0 tokens (at most 64)." in model.requests[1]
    )


@pytest.mark.asyncio
async def test_an_over_cut_is_refused_and_every_file_stays_byte_identical(tmp_path: Path) -> None:
    """Deleting 90% of MEMORY.md is refused at the tool, the pass is rejected, and no file changes."""
    config, paths, root = _setup(tmp_path)
    before = _snapshot(root)
    over_cut = "".join(SECTIONS[1:])
    model = _model([("edit_file", {"path": "MEMORY.md", "old_text": over_cut, "new_text": ""})])

    with patch("mindroom.model_loading.get_model_instance", return_value=model):
        outcome = await _curate(PromptCurationRunner(paths), config)

    assert outcome == "rejected"
    assert _snapshot(root) == before
    assert "Refused: MEMORY.md shrank 87%" in model.requests[1][-1]
    assert _state(paths)["mind"]["consecutive_failures"] == 1


@pytest.mark.asyncio
async def test_deleting_instead_of_moving_is_rejected_and_restored(tmp_path: Path) -> None:
    """A cut within the band that deletes detail instead of moving it is rejected, leaving the files untouched."""
    config, paths, root = _setup(tmp_path)
    before = _snapshot(root)
    model = _model([("edit_file", {"path": "MEMORY.md", "old_text": SECTIONS[3], "new_text": ""})])

    with patch("mindroom.model_loading.get_model_instance", return_value=model):
        outcome = await _curate(PromptCurationRunner(paths), config)

    assert outcome == "rejected"
    assert _snapshot(root) == before


@pytest.mark.asyncio
async def test_a_rewrite_during_the_pass_discards_it_without_counting_a_failure(tmp_path: Path) -> None:
    """A live turn's rewrite wins over the pass, which publishes nothing and is not held against the agent."""
    config, paths, root = _setup(tmp_path)
    rewritten = "# Memory\nRewritten by a live turn.\n"
    model = _model(MOVE)
    model.before_answer = lambda: (root / "MEMORY.md").write_text(rewritten, encoding="utf-8")

    with patch("mindroom.model_loading.get_model_instance", return_value=model):
        outcome = await _curate(PromptCurationRunner(paths), config)

    assert outcome == "conflict"
    assert (root / "MEMORY.md").read_text() == rewritten
    assert not (root / "memory" / "topics.md").exists()
    assert _state(paths)["mind"]["consecutive_failures"] == 0


@pytest.mark.asyncio
async def test_a_timed_out_pass_reports_its_usage_and_changes_nothing(tmp_path: Path) -> None:
    """A pass that runs out of time publishes nothing, counts as a failure, and still reports what it spent."""
    config, paths, root = _setup(tmp_path, timeout_seconds=1)
    before = _snapshot(root)
    model = _model(MOVE)
    model.released_requests = 1

    with patch("mindroom.model_loading.get_model_instance", return_value=model):
        outcome = await _curate(PromptCurationRunner(paths), config)

    assert outcome == "timeout"
    assert _snapshot(root) == before
    assert _usage_kinds(config, paths) == {"prompt_curation"}
    assert _state(paths)["mind"]["consecutive_failures"] == 1


@pytest.mark.asyncio
async def test_a_failing_model_run_counts_as_a_failure(tmp_path: Path) -> None:
    """A provider error ends the pass without publishing and backs off like a rejection."""
    config, paths, root = _setup(tmp_path)
    before = _snapshot(root)
    model = _model(MOVE)
    model.failure = RuntimeError("provider down")

    with patch("mindroom.model_loading.get_model_instance", return_value=model):
        outcome = await _curate(PromptCurationRunner(paths), config)

    assert outcome == "failed"
    assert _snapshot(root) == before
    assert _state(paths)["mind"] == {
        "active": True,
        "consecutive_failures": 1,
        "last_attempt_at": _state(paths)["mind"]["last_attempt_at"],
        "last_outcome": "failed",
    }


@pytest.mark.asyncio
async def test_one_pass_runs_per_workspace_and_stop_cancels_it_unpublished(tmp_path: Path) -> None:
    """A second reply does not start a second pass, and shutdown cancels the running one before it publishes."""
    config, paths, root = _setup(tmp_path)
    before = _snapshot(root)
    model = _model(MOVE)
    model.released_requests = 1
    runner = PromptCurationRunner(paths)

    with patch("mindroom.model_loading.get_model_instance", return_value=model):
        task = runner.maybe_start(config, agent_name="mind", session_id="session", identity=None)
        assert task is not None
        await model.blocked.wait()
        assert runner.maybe_start(config, agent_name="mind", session_id="other", identity=None) is None
        await runner.stop()

    assert task.cancelled()
    assert _snapshot(root) == before
    state = _state(paths)["mind"]
    assert state["active"] is True
    assert state["last_attempt_at"] is not None
    assert runner.maybe_start(config, agent_name="mind", session_id="session", identity=None) is None


@pytest.mark.asyncio
async def test_rereading_files_stops_at_the_input_budget(tmp_path: Path) -> None:
    """Every request resends the conversation, so a model that keeps re-reading files is stopped early."""
    config, paths, root = _setup(tmp_path)
    before = _snapshot(root)
    model = _model(*([("read_file", {"path": "MEMORY.md"})] for _ in range(15)))

    with patch("mindroom.model_loading.get_model_instance", return_value=model):
        outcome = await _curate(PromptCurationRunner(paths), config)

    assert outcome == "rejected"
    assert _snapshot(root) == before
    first_request_tokens = sum(len(content) for content in model.requests[0]) // 4
    sent_tokens = sum(sum(len(content) for content in request) // 4 for request in model.requests)
    assert len(model.requests) < 8
    assert sent_tokens <= 8 * first_request_tokens


@pytest.mark.asyncio
async def test_the_cooldown_survives_a_restart(tmp_path: Path) -> None:
    """The persisted attempt time keeps a new runner from curating again within the cooldown."""
    config, paths, _root = _setup(tmp_path)
    runner = PromptCurationRunner(paths)
    with patch("mindroom.model_loading.get_model_instance", return_value=_model(MOVE)):
        assert await _curate(runner, config) == "accepted"
    assert runner.maybe_start(config, agent_name="mind", session_id="session", identity=None) is None

    model = _model(MOVE)
    with patch("mindroom.model_loading.get_model_instance", return_value=model):
        assert await _curate(PromptCurationRunner(paths), config) is None

    assert model.requests == []


def test_failed_passes_back_off_up_to_eight_times_the_cooldown() -> None:
    """Each consecutive failure doubles the wait before the next pass, capped at eight times the cooldown."""
    hours = [runner_module._cooldown_seconds(PromptCurationConfig(), failures) / 3600 for failures in range(6)]

    assert hours == [24, 48, 96, 192, 192, 192]


@pytest.mark.asyncio
async def test_a_failed_pass_is_retried_only_after_its_backoff(tmp_path: Path) -> None:
    """After one rejected pass, the next waits two cooldowns instead of one."""
    config, paths, _root = _setup(tmp_path)
    over_cut = _model([("edit_file", {"path": "MEMORY.md", "old_text": SECTIONS[3], "new_text": ""})])
    with patch("mindroom.model_loading.get_model_instance", return_value=over_cut):
        assert await _curate(PromptCurationRunner(paths), config) == "rejected"
    state = _state(paths)["mind"]
    day = 24 * 3600

    for elapsed, expected in ((day + 1, None), (2 * day + 1, "accepted")):
        with (
            patch.object(runner_module, "_now", return_value=state["last_attempt_at"] + elapsed),
            patch("mindroom.model_loading.get_model_instance", return_value=_model(MOVE)),
        ):
            assert await _curate(PromptCurationRunner(paths), config) == expected


@pytest.mark.asyncio
async def test_files_under_the_trigger_start_no_pass(tmp_path: Path) -> None:
    """Small prompt files cost no model call and leave no state behind."""
    config, paths, _root = _setup(tmp_path, memory="# Memory\n- Prefers terse replies.\n")
    model = _model(MOVE)

    with patch("mindroom.model_loading.get_model_instance", return_value=model):
        assert await _curate(PromptCurationRunner(paths), config) is None

    assert model.requests == []
    assert not (paths.storage_root / "prompt_curation_state.json").exists()


@pytest.mark.asyncio
async def test_hysteresis_keeps_curating_under_the_trigger_until_the_target(tmp_path: Path) -> None:
    """Once curation started, it continues under the trigger until the files are below the stop target."""
    # 942 tokens: under the 1,000 trigger but over the 900 stop target.
    config, paths, root = _setup(tmp_path, memory=HEADER + "".join(SECTIONS[:5]) + FILLER + EXTRA)
    model = _model(
        [
            ("append_file", {"path": "memory/trips.md", "content": EXTRA}),
            ("edit_file", {"path": "MEMORY.md", "old_text": EXTRA, "new_text": ""}),
        ],
    )
    with patch("mindroom.model_loading.get_model_instance", return_value=model):
        assert await _curate(PromptCurationRunner(paths), config) is None
    runner_module._save_scope_state(paths.storage_root, "mind", runner_module._ScopeState(active=True))

    with patch("mindroom.model_loading.get_model_instance", return_value=model):
        assert await _curate(PromptCurationRunner(paths), config) == "accepted"

    assert len((root / "MEMORY.md").read_text()) // 4 <= 900
    assert _state(paths)["mind"]["active"] is False


@pytest.mark.asyncio
async def test_disabled_and_non_file_agents_are_never_checked(tmp_path: Path) -> None:
    """Disabled curation and agents without file memory never start a background check."""
    config, paths, _root = _setup(tmp_path, enabled=False)
    runner = PromptCurationRunner(paths)
    assert runner.maybe_start(config, agent_name="mind", session_id="session", identity=None) is None

    config.agents["mind"].prompt_curation = None
    config.agents["mind"].memory_backend = "mem0"
    assert runner.maybe_start(config, agent_name="mind", session_id="session", identity=None) is None


@pytest.mark.asyncio
async def test_private_workspaces_are_curated_and_tracked_per_worker_scope(tmp_path: Path) -> None:
    """A private agent's requester workspace is curated with that requester's identity and its own state."""
    config, paths, root = _setup(tmp_path, private=True)

    with patch("mindroom.model_loading.get_model_instance", return_value=_model(MOVE)):
        assert await _curate(PromptCurationRunner(paths), config, ALICE) == "accepted"

    assert (root / "memory" / "topics.md").read_text() == SECTIONS[3]
    (key,) = _state(paths)
    assert key.startswith("mind:")
    assert key != "mind:"


@pytest.mark.asyncio
async def test_pass_logs_carry_its_caller_label_not_the_responses_context(tmp_path: Path) -> None:
    """The pass's model calls are labelled prompt_curation and carry none of the triggering response's log fields."""
    config, paths, _root = _setup(tmp_path)
    model = _model(MOVE)

    with (
        structlog.contextvars.bound_contextvars(correlation_id="$response"),
        patch("mindroom.model_loading.get_model_instance", return_value=model),
    ):
        await _curate(PromptCurationRunner(paths), config)

    assert model.log_contexts[0]["caller_label"] == "prompt_curation"
    assert model.log_contexts[0]["agent_id"] == "mind"
    assert "correlation_id" not in model.log_contexts[0]
    assert model.request_log_contexts[0]["caller_label"] == "prompt_curation"


def test_curation_cannot_post_to_matrix() -> None:
    """The pass has no Matrix client: no prompt-curation module imports Matrix code."""
    package_dir = Path(prompt_curation_package.__file__).parent
    imported = {
        node.module if isinstance(node, ast.ImportFrom) else alias.name
        for path in package_dir.glob("*.py")
        for node in ast.walk(ast.parse(path.read_text()))
        if isinstance(node, ast.Import | ast.ImportFrom)
        for alias in node.names
    }
    assert not [name for name in imported if name and name.startswith(("mindroom.matrix", "nio"))]
