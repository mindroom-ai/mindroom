"""Router judgments preserve candidate scope and the legacy fallback."""

from __future__ import annotations

import json
from dataclasses import replace
from types import SimpleNamespace
from typing import TYPE_CHECKING

import httpx
import pytest
from agno.run.agent import RunOutput
from pydantic import ValidationError
from structlog.testing import capture_logs

from mindroom import model_loading, routing
from mindroom.config.main import Config
from mindroom.judgment.client import PINNED_MODEL, SystemOneClient
from mindroom.matrix.client_visible_messages import ResolvedVisibleMessage
from tests.conftest import test_runtime_paths

if TYPE_CHECKING:
    from pathlib import Path

    from mindroom.constants import RuntimePaths


def _config(judgment: dict | None = None) -> Config:
    return Config.model_validate(
        {
            "models": {"default": {"provider": "openai", "id": "test"}},
            "agents": {
                "code": {"display_name": "code", "role": "Write Python code"},
                "research": {"display_name": "research", "role": "Research sources"},
                "hidden": {"display_name": "hidden", "role": "Private unrelated agent"},
            },
            "teams": {"crew": {"display_name": "crew", "agents": ["code", "research"], "role": "Combined work"}},
            "router": {"judgment": judgment or {"provider": "typesafe"}},
        },
    )


def _providers(monkeypatch: pytest.MonkeyPatch) -> list[str]:
    monkeypatch.setattr(model_loading, "get_model_instance", lambda *_: SimpleNamespace(id="test"))
    prompts = []

    class Router:
        def __init__(self, **_kwargs: object) -> None:
            pass

        async def arun(self, prompt: str, **_kwargs: object) -> RunOutput:
            prompts.append(prompt)
            return RunOutput(content={"entity_name": "research", "reasoning": "fallback"})

    monkeypatch.setattr(routing, "Agent", Router)
    return prompts


def _paths(tmp_path: Path) -> RuntimePaths:
    return replace(test_runtime_paths(tmp_path), process_env={"TYPESAFE_API_KEY": "synthetic"})


def _choice_provider(monkeypatch: pytest.MonkeyPatch, choice: str | None) -> list[dict]:
    posted = []

    async def post(_self: SystemOneClient, body: bytes) -> bytes:
        payload = json.loads(body)
        posted.append(payload)
        return json.dumps(
            {
                "model": PINNED_MODEL,
                "answers": {
                    "responder_selection": {
                        "type": "choice",
                        "choice": choice,
                        "confidence": 1,
                        "probabilities": {
                            key: int(key == choice) for key in payload["questions"]["responder_selection"]["criteria"]
                        },
                    },
                },
                "usage": {"input_tokens": 42, "output_tokens": 1},
            },
        ).encode()

    monkeypatch.setattr(SystemOneClient, "_post", post)
    return posted


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("decision", "expected", "fallback"),
    [
        ("candidate_0", "code", False),
        ("candidate_2", "crew", False),
        ("no_fit", None, False),
        ("multiple", "research", True),
        (None, "research", True),
        ("hidden", "research", True),
    ],
)
async def test_router_maps_choice_and_only_falls_back_when_needed(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    decision: str | None,
    expected: str | None,
    fallback: bool,
) -> None:
    """Router maps choice and only falls back when needed."""
    legacy = _providers(monkeypatch)
    posted = _choice_provider(monkeypatch, decision)
    result = await routing.suggest_responder(
        "Fix my Python code",
        ["code", "research", "crew"],
        _config(),
        _paths(tmp_path),
    )
    assert result is not None
    assert result.entity_name == expected
    assert bool(legacy) is fallback
    assert len(posted) == 1
    payload = posted[0]
    assert set(payload["questions"]["responder_selection"]["criteria"]) == {
        "candidate_0",
        "candidate_1",
        "candidate_2",
        "no_fit",
        "multiple",
    }
    assert "Private unrelated agent" not in json.dumps(payload)


@pytest.mark.asyncio
async def test_router_judge_sees_complete_recent_text_and_aliased_speakers(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Router judge sees complete recent text and aliased speakers."""
    _providers(monkeypatch)
    posted = _choice_provider(monkeypatch, "candidate_0")
    history = [
        ResolvedVisibleMessage.synthetic(sender="@person:example.org", body=body, event_id=f"$msg{i}")
        for i, body in enumerate(["oldest", "context " * 30, "second", "third"])
    ]
    result = await routing.suggest_responder(
        "Current question",
        ["code", "research"],
        _config(),
        _paths(tmp_path),
        history,
    )
    assert result is not None
    assert result.entity_name == "code"
    payload = posted[0]
    state = json.dumps(payload["state"])
    assert "oldest" not in state
    assert "context " * 30 in state
    assert "Current question" in state
    assert "@person:example.org" not in state
    assert "speaker_1" in state


@pytest.mark.asyncio
@pytest.mark.parametrize("message", ["token=sk-secret", "x" * 16000], ids=["secret", "oversized"])
async def test_unsafe_judgment_context_uses_existing_router_without_judge_call(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    message: str,
) -> None:
    """Unsafe judgment context uses existing router without judge call."""
    legacy = _providers(monkeypatch)
    posted = _choice_provider(monkeypatch, "candidate_0")
    with capture_logs() as logs:
        result = await routing.suggest_responder(message, ["code", "research"], _config(), _paths(tmp_path))
    failures = [entry for entry in logs if entry.get("failure") == "incomplete_state"]
    assert len(failures) == 1
    assert failures[0]["incomplete_reason"] == (
        "essential_input_redacted" if message.startswith("token=") else "essential_input_too_large"
    )
    assert message not in json.dumps(failures)
    assert result is not None
    assert result.entity_name == "research"
    assert len(legacy) == 1
    assert posted == []


@pytest.mark.asyncio
async def test_disabled_judgment_keeps_existing_router(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """Disabled judgment keeps existing router."""
    legacy = _providers(monkeypatch)
    posted = _choice_provider(monkeypatch, "candidate_0")
    config = _config()
    config.router.judgment = None
    result = await routing.suggest_responder("Help", ["code", "research"], config, test_runtime_paths(tmp_path))
    assert result is not None
    assert result.entity_name == "research"
    assert len(legacy) == 1
    assert posted == []


@pytest.mark.parametrize("alias", ["default", "missing"])
def test_router_rejects_duplicate_llm_judgment_configuration(alias: str) -> None:
    """LLM routing has one configuration entry point: router.model."""
    with pytest.raises(ValidationError, match="typesafe"):
        _config({"provider": "llm", "model": alias})


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "scenario",
    ["select", "missing_key", "timeout", "drift", "low_confidence", "tie", "malformed"],
)
async def test_typesafe_routing_uses_shared_client_and_fallback(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    scenario: str,
) -> None:
    """Provider selection, credentials, thresholds and decoder failures reach the correct routing path."""
    legacy = _providers(monkeypatch)
    posted = []

    async def post(_self: SystemOneClient, body: bytes) -> bytes:
        posted.append(json.loads(body))
        if scenario == "timeout":
            error = "unavailable"
            raise httpx.ReadTimeout(error)
        return (
            json.dumps(
                {
                    "model": "wrong" if scenario == "drift" else PINNED_MODEL,
                    "answers": {
                        "responder_selection": {
                            "type": "choice",
                            "choice": "candidate_0",
                            "confidence": 0.1 if scenario == "low_confidence" else 0.95,
                            "probabilities": {"candidate_0": 0.5, "candidate_1": 0.5, "no_fit": 0, "multiple": 0}
                            if scenario == "tie"
                            else {"candidate_0": 0.9, "candidate_1": 0.1, "no_fit": 0, "multiple": 0},
                        },
                    },
                    "usage": {"input_tokens": 42, "output_tokens": 1},
                },
            ).encode()
            if scenario != "malformed"
            else b"bad json"
        )

    monkeypatch.setattr(SystemOneClient, "_post", post)
    paths = replace(
        _paths(tmp_path),
        process_env={"TYPESAFE_API_KEY": "" if scenario == "missing_key" else "synthetic"},
    )
    config = _config({"provider": "typesafe", "threshold": 0.0 if scenario == "tie" else 0.8})
    result = await routing.suggest_responder("Help with Python", ["code", "research"], config, paths)
    assert result is not None
    assert result.entity_name == ("code" if scenario == "select" else "research")
    assert bool(legacy) is (scenario != "select")
    assert len(posted) == (0 if scenario == "missing_key" else 1)
    if posted:
        assert posted[0]["questions"]["responder_selection"]["type"] == "choice"


@pytest.mark.asyncio
async def test_default_and_jev_fallback_use_same_llm_router(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """Opting into JEV retains the configured LLM prompt and model on fallback."""
    prompts = _providers(monkeypatch)
    aliases = []

    def load(_config: Config, _paths: RuntimePaths, alias: str) -> SimpleNamespace:
        aliases.append(alias)
        return SimpleNamespace(id="test")

    monkeypatch.setattr(model_loading, "get_model_instance", load)
    config = _config()
    config.models["cheap_router"] = config.models["default"]
    config.router.model = "cheap_router"
    paths = replace(test_runtime_paths(tmp_path), process_env={"TYPESAFE_API_KEY": ""})
    fallback = await routing.suggest_responder("Help", ["code", "research"], config, paths)
    config.router.judgment = None
    ordinary = await routing.suggest_responder("Help", ["code", "research"], config, paths)
    assert fallback == ordinary == routing.ResponderSelection("research")
    assert len(prompts) == 2
    assert prompts[0] == prompts[1]
    assert aliases == ["cheap_router", "cheap_router"]
