"""Check evaluation isolation and scoring through the real reviewer and skill loader."""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from typing import TYPE_CHECKING

import pytest
from agno.models.message import Message, MessageMetrics
from agno.models.response import ModelResponse

from mindroom import model_loading
from mindroom.config.models import ModelConfig
from mindroom.synthetic_model import SyntheticModel
from mindroom.tool_system import skills as skills_module
from scripts.testing import evaluate_skill_learning as evaluation

if TYPE_CHECKING:
    from pathlib import Path

SKILL = (
    "---\nname: sensor-export\ndescription: Use for sensor exports\n"
    "metadata: {mindroom: {learned: true}}\n---\n"
    "Return CSV with sensor,fahrenheit header. Convert Celsius to Fahrenheit, "
    "one decimal place, sort sensor names, no prose or fences.\n"
)


@dataclass
class _Provider(SyntheticModel):
    """Fixed provider replies; real Agno executes all requested skill tools."""

    id: str = "test"
    steps: list[ModelResponse] = field(default_factory=list)
    requests: list[list[Message]] = field(default_factory=list)

    async def ainvoke(self, messages: list[Message], **_kwargs: object) -> ModelResponse:
        self.requests.append([message.model_copy(deep=True) for message in messages])
        response = self.steps.pop(0)
        response.response_usage = MessageMetrics(input_tokens=10, output_tokens=2, total_tokens=12)
        return response


def _tool(name: str, arguments: dict[str, object]) -> ModelResponse:
    return ModelResponse(
        tool_calls=[
            {
                "id": "call-1",
                "type": "function",
                "function": {"name": name, "arguments": json.dumps(arguments)},
            },
        ],
    )


@pytest.mark.asyncio
async def test_evaluation_transfers_only_skills_and_counts_review_cost(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Held-out tasks never enter review; trials share only the intended treatment."""
    for key in (
        "MINDROOM_STORAGE_PATH",
        "MINDROOM_CONTROL_STATE_PATH",
        "MINDROOM_CONFIG_PATH",
        "MINDROOM_SESSION_STORAGE_PATH",
        "MINDROOM_SHARED_CREDENTIALS_PATH",
    ):
        monkeypatch.setenv(key, str(tmp_path / "live" / key))
    installed = tmp_path / "installed-skills/sensor-export/SKILL.md"
    installed.parent.mkdir(parents=True)
    installed.write_text(SKILL)
    monkeypatch.setattr(skills_module, "get_user_skills_dir", lambda: installed.parent.parent)
    providers = [
        _Provider(
            steps=[
                _tool("skill_manage", {"action": "create", "name": "sensor-export", "content": SKILL}),
                ModelResponse(content="Saved."),
            ],
        ),
    ]
    # The benchmark alternates arm order between cases to reduce ordering bias.
    expected = {
        "cold": "sensor,fahrenheit\nbirch,-40.0\ncedar,98.6",
        "warm": "sensor,fahrenheit\nash,14.0\nmaple,77.0",
        "control": "42",
    }
    for index, case in enumerate(evaluation.CASES):
        for arm in evaluation.ARMS[index % 3 :] + evaluation.ARMS[: index % 3]:
            steps = []
            if arm == "learned_skills":
                steps.append(_tool("get_skill_instructions", {"skill_name": "sensor-export"}))
            answer = "Wrong" if arm == "no_memory" and case.name != "control" else expected[case.name]
            providers.append(_Provider(steps=[*steps, ModelResponse(content=answer)]))
    pending = iter(providers)
    monkeypatch.setattr(model_loading, "get_model_instance", lambda *_args, **_kwargs: next(pending))

    report = await evaluation.evaluate(
        output_dir=tmp_path / "run",
        model_config=ModelConfig(provider="openai", id="test"),
        repeats=1,
    )

    assert report.review_tokens["total_tokens"] == 24
    assert report.learned_skills == ["sensor-export"], providers[0].requests[-1][-1].get_content_string()
    assert len(report.trials) == 9
    assert sum(trial.passed for trial in report.trials) == 7
    assert sum(trial.metrics["total_tokens"] for trial in report.trials) == 144
    reviewer_input = "\n".join(message.get_content_string() for message in providers[0].requests[0])
    assert evaluation.CORRECTION in reviewer_input, reviewer_input[:2000]
    assert all(case.prompt not in reviewer_input for case in evaluation.CASES)
    for provider, trial in zip(providers[1:], report.trials, strict=True):
        first = provider.requests[0]
        user_messages = [message.get_content_string() for message in first if message.role == "user"]
        assert user_messages == [next(case.prompt for case in evaluation.CASES if case.name == trial.case)]
        contents = "\n".join(message.get_content_string() for message in first)
        assert (evaluation.CORRECTION in contents) == (trial.arm == "raw_correction")
        assert "oak: 0" not in contents
        if trial.arm == "learned_skills":
            assert trial.tool_calls == ["get_skill_instructions"]
            loaded = json.loads(provider.requests[-1][-1].get_content_string())
            assert loaded["instructions"] == (
                "Return CSV with sensor,fahrenheit header. Convert Celsius to Fahrenheit, "
                "one decimal place, sort sensor names, no prose or fences."
            )
            assert trial.metrics["total_tokens"] == 24
        else:
            assert trial.tool_calls == []
            assert trial.metrics["total_tokens"] == 12
    saved = json.loads((tmp_path / "run/report.json").read_text())
    assert saved["summary"]["no_memory"]["passed"] == 1
    assert saved["summary"]["learned_skills"]["passed"] == 3
    assert (tmp_path / "run/learning/workspace/skills/sensor-export/SKILL.md").is_file()
    assert not (tmp_path / "live").exists()
    assert installed.read_text() == SKILL


@pytest.mark.asyncio
async def test_empty_review_is_reported_without_inventing_learning(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A no-op reviewer still gets evaluated, with an explicitly empty skill treatment."""
    monkeypatch.setattr(
        model_loading,
        "get_model_instance",
        lambda *_args, **_kwargs: _Provider(steps=[ModelResponse(content="42")]),
    )
    report = await evaluation.evaluate(
        output_dir=tmp_path / "run",
        model_config=ModelConfig(provider="openai", id="test"),
        repeats=2,
    )
    assert report.learned_skills == []
    assert report.review_tokens["total_tokens"] == 12
    assert len(report.trials) == 18
    assert sum(trial.passed for trial in report.trials) == 6
    assert all(not trial.tool_calls for trial in report.trials)
    assert json.loads((tmp_path / "run/report.json").read_text())["completed"] is True


@pytest.mark.asyncio
async def test_failed_trial_keeps_partial_report(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """A provider failure leaves completed evidence clearly marked as incomplete."""
    calls = 0

    def model(*_args: object, **_kwargs: object) -> _Provider:
        nonlocal calls
        calls += 1
        if calls == 3:
            msg = "provider unavailable"
            raise RuntimeError(msg)
        return _Provider(steps=[ModelResponse(content="42")])

    monkeypatch.setattr(model_loading, "get_model_instance", model)
    with pytest.raises(RuntimeError, match="provider unavailable"):
        await evaluation.evaluate(output_dir=tmp_path / "run", model_config=ModelConfig(provider="openai", id="test"))
    saved = json.loads((tmp_path / "run/report.json").read_text())
    assert saved["completed"] is False
    assert len(saved["trials"]) == 1
    assert saved["review_tokens"]["total_tokens"] == 12


@pytest.mark.parametrize(
    ("output", "passed"),
    [
        ("sensor,fahrenheit\nbirch,-40.0\ncedar,98.6\n", True),
        ("sensor,fahrenheit\r\nbirch,-40.0\r\ncedar,98.6", True),
        ("sensor,fahrenheit\ncedar,98.6\nbirch,-40.0", False),
        ("sensor,fahrenheit\nbirch,-40\ncedar,98.6", False),
        ("sensor,fahrenheit\nbirch,-40.0\ncedar,98.5", False),
        ("```csv\nsensor,fahrenheit\nbirch,-40.0\ncedar,98.6\n```", False),
        ("Here you go:\nsensor,fahrenheit\nbirch,-40.0\ncedar,98.6", False),
    ],
)
def test_scoring_rejects_wrong_values_order_precision_and_wrappers(output: str, passed: bool) -> None:
    """Only harmless line endings vary; formatting is part of the learned contract."""
    assert evaluation.CASES[0].accepts(output) is passed


@pytest.mark.asyncio
async def test_existing_output_is_not_reused(tmp_path: Path) -> None:
    """A previous run must never contaminate a comparison or lose its evidence."""
    sentinel = tmp_path / "keep.txt"
    sentinel.write_text("keep")
    with pytest.raises(FileExistsError):
        await evaluation.evaluate(output_dir=tmp_path, model_config=ModelConfig(provider="openai", id="test"))
    assert sentinel.read_text() == "keep"
