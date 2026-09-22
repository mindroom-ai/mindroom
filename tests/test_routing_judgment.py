"""Router judgments preserve candidate scope and the legacy fallback."""

from __future__ import annotations

import json
from dataclasses import replace
from types import SimpleNamespace
from typing import TYPE_CHECKING

import httpx
import pytest
from agno.models.response import ModelResponse
from pydantic import ValidationError

from mindroom import model_loading, routing
from mindroom.config.main import Config
from mindroom.judgment.client import PINNED_MODEL, SystemOneClient
from mindroom.matrix.client_visible_messages import ResolvedVisibleMessage
from tests.conftest import test_runtime_paths
from tests.participation_helpers import ParticipationModel

if TYPE_CHECKING:
    from pathlib import Path


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
            "router": {"judgment": judgment or {"provider": "llm", "model": "default"}},
        },
    )


def _providers(monkeypatch: pytest.MonkeyPatch, content: str | BaseException) -> tuple[ParticipationModel, list[str]]:
    model = ParticipationModel(content if isinstance(content, BaseException) else ModelResponse(content=content))
    monkeypatch.setattr(model_loading, "get_model_instance", lambda *_: model)
    legacy_prompts = []

    class LegacyRouter:
        def __init__(self, **_kwargs: object) -> None:
            pass

        async def arun(self, prompt: str, **_kwargs: object) -> SimpleNamespace:
            legacy_prompts.append(prompt)
            return SimpleNamespace(content={"entity_name": "research", "reasoning": "fallback"})

    monkeypatch.setattr(routing, "Agent", LegacyRouter)
    return model, legacy_prompts


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
    model, legacy = _providers(monkeypatch, json.dumps({"decision": decision}))
    result = await routing.suggest_responder(
        "Fix my Python code",
        ["code", "research", "crew"],
        _config(),
        test_runtime_paths(tmp_path),
    )
    assert result == expected
    assert bool(legacy) is fallback
    assert len(model.requests) == 1
    payload = json.loads(model.requests[0]["messages"][1].content)
    assert set(payload["question"]["criteria"]) == {"candidate_0", "candidate_1", "candidate_2", "no_fit", "multiple"}
    assert "Private unrelated agent" not in json.dumps(payload)


@pytest.mark.asyncio
async def test_router_judge_sees_complete_recent_text_and_aliased_speakers(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Router judge sees complete recent text and aliased speakers."""
    model, _ = _providers(monkeypatch, '{"decision":"candidate_0"}')
    history = [
        ResolvedVisibleMessage.synthetic(sender="@person:example.org", body=body, event_id=f"$msg{i}")
        for i, body in enumerate(["oldest", "context " * 30, "second", "third"])
    ]
    result = await routing.suggest_responder(
        "Current question",
        ["code", "research"],
        _config(),
        test_runtime_paths(tmp_path),
        history,
    )
    assert result == "code"
    payload = json.loads(model.requests[0]["messages"][1].content)
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
    model, legacy = _providers(monkeypatch, '{"decision":"candidate_0"}')
    result = await routing.suggest_responder(message, ["code", "research"], _config(), test_runtime_paths(tmp_path))
    assert result == "research"
    assert len(legacy) == 1
    assert model.requests == []


@pytest.mark.asyncio
async def test_router_provider_failure_falls_back(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """Router provider failure falls back."""
    _, legacy = _providers(monkeypatch, RuntimeError("provider unavailable"))
    result = await routing.suggest_responder("Help", ["code", "research"], _config(), test_runtime_paths(tmp_path))
    assert result == "research"
    assert len(legacy) == 1


@pytest.mark.asyncio
async def test_disabled_judgment_keeps_existing_router(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """Disabled judgment keeps existing router."""
    model, legacy = _providers(monkeypatch, '{"decision":"candidate_0"}')
    config = _config()
    config.router.judgment = None
    result = await routing.suggest_responder("Help", ["code", "research"], config, test_runtime_paths(tmp_path))
    assert result == "research"
    assert len(legacy) == 1
    assert model.requests == []


def test_router_rejects_unknown_judgment_model_alias() -> None:
    """Router rejects unknown judgment model alias."""
    with pytest.raises(ValidationError, match="Unknown judgment model for router"):
        _config({"provider": "llm", "model": "missing"})


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
    model, legacy = _providers(monkeypatch, '{"decision":"candidate_0"}')
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
        test_runtime_paths(tmp_path),
        process_env={"TYPESAFE_API_KEY": "" if scenario == "missing_key" else "synthetic"},
    )
    config = _config({"provider": "typesafe", "threshold": 0.0 if scenario == "tie" else 0.8})
    result = await routing.suggest_responder("Help with Python", ["code", "research"], config, paths)
    assert result == ("code" if scenario == "select" else "research")
    assert bool(legacy) is (scenario != "select")
    assert model.requests == []
    assert len(posted) == (0 if scenario == "missing_key" else 1)
    if posted:
        assert posted[0]["questions"]["responder_selection"]["type"] == "choice"
