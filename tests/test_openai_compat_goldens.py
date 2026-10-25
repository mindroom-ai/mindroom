"""The OpenAI-compatible API sends the bodies of its stored wire golden, from the production path.

``tests/fixtures/agno_parity/events/openai_compat.json`` holds the status and body of four ``/v1/chat/completions``
requests: a blocking agent reply, a streamed agent reply with a tool call, and a blocking and a streamed coordinate
team reply. Each request runs here through the API, the agent and team runtimes, the tools, and the provider
registry's request pipeline; only each route's adapter is replaced, by the script the golden's capture answered with.
"""

from __future__ import annotations

import json
import re
from pathlib import Path
from typing import TYPE_CHECKING, Any
from unittest.mock import patch

from fastapi import FastAPI
from fastapi.testclient import TestClient

from mindroom.api import openai_compat
from mindroom.api.main import initialize_api_app
from mindroom.config.main import Config
from mindroom.constants import resolve_runtime_paths
from mindroom.llm.messages import Message
from mindroom.llm.provider import ModelIdentity, TextDelta, TurnCompleted, UsageReported
from tests.conftest import bind_runtime_paths
from tests.fakes.scripted_model import ScriptedModel, ScriptStep, answer, calls, tool_call, usage

if TYPE_CHECKING:
    from collections.abc import Sequence

    from mindroom.config.models import ModelConfig
    from mindroom.llm.usage import MessageMetrics

GOLDEN = Path(__file__).parent / "fixtures" / "agno_parity" / "events" / "openai_compat.json"

_CONFIG: dict[str, Any] = {
    "timezone": "UTC",
    "defaults": {"tools": []},
    "memory": {"backend": "none"},
    "agents": {
        "writer": {"display_name": "Writer", "role": "Writes answers.", "model": "writer", "tools": ["calculator"]},
        "checker": {"display_name": "Checker", "role": "Checks answers.", "model": "checker", "tools": ["calculator"]},
    },
    "teams": {
        "squad": {
            "display_name": "Squad",
            "role": "Answers with a writer and a checker.",
            "agents": ["writer", "checker"],
            "mode": "coordinate",
            "model": "leader",
        },
    },
    "models": {
        "default": {"provider": "synthetic", "id": "synthetic"},
        "leader": {"provider": "synthetic", "id": "leader-model", "context_window": 100000},
        "writer": {"provider": "synthetic", "id": "writer-model"},
        "checker": {"provider": "synthetic", "id": "checker-model"},
    },
}
_COMPLETION_ID = re.compile(r"chatcmpl-[0-9a-f]{12}")
# Also inside the SSE lines, where the key's quotes are escaped.
_CREATED = re.compile(r'(created\\?"\s*:\s*)\d+')


def _streamed(metrics: MessageMetrics, *chunks: str) -> list[ScriptStep]:
    """Return a turn that streams exactly ``chunks``, as the capture's provider did, and completes with their text."""
    text = "".join(chunks)
    return [
        *(TextDelta(chunk) for chunk in chunks),
        UsageReported(metrics),
        TurnCompleted(message=Message(role="assistant", content=text, metrics=metrics), stop_reason="end"),
    ]


# Each request's turns by model id, in the order the capture scripted them.
_REQUESTS: dict[str, dict[str, list[list[ScriptStep]]]] = {
    "agent_blocking": {"writer-model": [answer("Hello from the writer.", usage(70, 5))]},
    "agent_stream_with_tool": {
        "writer-model": [
            calls(tool_call("add", "call-api-add", a=2, b=3), metrics=usage(72, 3)),
            _streamed(usage(78, 4), "The sum ", "is 5."),
        ],
    },
    "team_blocking": {
        "leader-model": [
            calls(
                tool_call("delegate_task_to_member", "call-api-team", member_id="writer", task="Add 2 and 3"),
                metrics=usage(400, 12),
            ),
            answer("The writer found 5.", usage(450, 9)),
        ],
        "writer-model": [
            calls(tool_call("add", "call-api-writer-add", a=2, b=3), metrics=usage(80, 4)),
            answer("5", usage(95, 1)),
        ],
    },
    "team_stream": {
        "leader-model": [
            calls(
                tool_call("delegate_task_to_member", "call-api-check", member_id="checker", task="Check 5"),
                metrics=usage(401, 12),
            ),
            _streamed(usage(451, 9), "Checked: ", "5 is right."),
        ],
        "checker-model": [_streamed(usage(66, 2), "Looks ", "right.")],
    },
}


def _wire(cases: dict[str, Any]) -> dict[str, Any]:
    """Number the completion ids by first appearance and blank the clock, the only values that vary between runs."""
    text = json.dumps(cases)
    ids: dict[str, str] = {}
    text = _COMPLETION_ID.sub(lambda match: ids.setdefault(match.group(), f"chatcmpl-{len(ids) + 1:012x}"), text)
    return json.loads(_CREATED.sub(r"\g<1>0", text))


def _post_all(tmp_path: Path) -> dict[str, Any]:
    """Send the golden's requests in order and record each one's request, status, and body as the capture did."""
    runtime_paths = resolve_runtime_paths(
        config_path=tmp_path / "config.yaml",
        storage_path=tmp_path / "storage",
        process_env={"OPENAI_COMPAT_ALLOW_UNAUTHENTICATED": "true"},
    )
    config = bind_runtime_paths(Config.model_validate(_CONFIG), runtime_paths)
    turns: dict[str, list[Sequence[ScriptStep]]] = {}

    def scripted_route(model_config: ModelConfig, *_args: object, **_kwargs: object) -> ScriptedModel:
        # Every route of one model id answers from the same script, as the capture's scripted models did.
        identity = ModelIdentity(id=model_config.id, provider="Synthetic", provider_name="Synthetic")
        return ScriptedModel(turns.setdefault(model_config.id, []), identity=identity)

    app = FastAPI()
    app.include_router(openai_compat.router)
    initialize_api_app(app, runtime_paths)
    recorded: dict[str, Any] = {}
    with (
        patch("mindroom.api.openai_compat._load_config", return_value=(config, runtime_paths)),
        patch("mindroom.providers.registry._build_adapter", new=scripted_route),
        TestClient(app, base_url="http://localhost") as client,
    ):
        golden = json.loads(GOLDEN.read_text(encoding="utf-8"))
        for name, scripts in _REQUESTS.items():
            for model_id, model_turns in scripts.items():
                turns.setdefault(model_id, []).extend(model_turns)
            request = golden[name]["request"]
            response = client.post("/v1/chat/completions", headers=request["headers"], json=request["json"])
            recorded[name] = {
                "request": request,
                "status_code": response.status_code,
                "body": response.text.split("\n") if request["json"].get("stream") else response.json(),
            }
    assert all(not model_turns for model_turns in turns.values()), turns
    return recorded


def test_the_api_sends_the_golden_bodies(tmp_path: Path) -> None:
    """Blocking and streamed agent and team completions match the golden: text, tool blocks, member slots, usage."""
    recorded = _post_all(tmp_path)

    assert _wire(recorded) == _wire(json.loads(GOLDEN.read_text(encoding="utf-8")))
