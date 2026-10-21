"""The Agno-backed runtime still writes, loads, and reads exactly what the storage, history, and event goldens hold.

Task B8 deletes this test with Agno; the owned storage, history, and codec tests keep reading the fixtures.
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
from copy import deepcopy
from pathlib import Path
from typing import Any

from agno.metrics import MessageMetrics, ModelMetrics, RunMetrics, SessionMetrics, ToolCallMetrics
from agno.models.message import Message
from agno.models.response import ToolExecution
from agno.run.agent import RunOutput
from agno.run.requirement import RunRequirement
from agno.run.team import TeamRunOutput

from tests.agno_parity.capture_storage import FIXTURES, STORES, derive_goldens, owned_goldens

_REPOSITORY = Path(__file__).resolve().parents[2]
_AGNO_TYPES: dict[str, Any] = {
    "run_output": RunOutput,
    "team_run_output": TeamRunOutput,
    "message": Message,
    "tool_execution": ToolExecution,
    "run_requirement": RunRequirement,
    "message_metrics": MessageMetrics,
    "run_metrics": RunMetrics,
    "model_metrics": ModelMetrics,
    "tool_call_metrics": ToolCallMetrics,
    "session_metrics": SessionMetrics,
}
_MEDIA_KEYS = {"image": "images", "audio": "audio", "video": "videos", "file": "files"}


def _changed(generated: dict[str, str], committed: dict[str, str]) -> list[str]:
    return [path for path in sorted(generated.keys() | committed.keys()) if generated.get(path) != committed.get(path)]


def test_capture_reproduces_every_committed_golden(tmp_path: Path) -> None:
    """Rerunning every scenario writes byte-identical stores, events, runs, and history."""
    output = tmp_path / "goldens"
    result = subprocess.run(
        [sys.executable, "-m", "tests.agno_parity.capture_storage", "--output", str(output)],
        cwd=_REPOSITORY,
        env={**os.environ, "PYTHONHASHSEED": "0"},
        capture_output=True,
        text=True,
        check=False,
    )
    assert result.returncode == 0, result.stderr[-4000:]
    assert _changed(owned_goldens(output), owned_goldens(FIXTURES)) == []


def test_loading_the_committed_stores_reproduces_the_derived_goldens(tmp_path: Path) -> None:
    """MindRoom's storage path loads the committed stores into the committed sessions, runs, and history selections."""
    dumps = {spec.label: (FIXTURES / "storage" / f"{spec.label}.sql").read_text(encoding="utf-8") for spec in STORES}
    derived = derive_goldens(dumps, tmp_path)
    committed = {path: text for path, text in owned_goldens(FIXTURES).items() if path in derived}
    assert sorted(derived) == sorted(committed)
    assert _changed(derived, committed) == []


def _without_audio_format(audio: dict[str, Any]) -> dict[str, Any]:
    return {key: value for key, value in audio.items() if key != "format"}


def _reread_message(message: dict[str, Any]) -> dict[str, Any]:
    if "audio" not in message:
        return message
    return {**message, "audio": [_without_audio_format(audio) for audio in message["audio"]]}


def _reread_run(run: dict[str, Any]) -> dict[str, Any]:
    reread = {key: value for key, value in run.items() if not (key == "tools" and value == [])}
    reread["events"] = run.get("events", [])
    if "messages" in run:
        reread["messages"] = [_reread_message(message) for message in run["messages"]]
    if "member_responses" in run:
        reread["member_responses"] = [_reread_run(member) for member in run["member_responses"]]
    return reread


def _expected_agno_reread(kind: str, value: dict[str, Any]) -> dict[str, Any]:
    """Return what Agno writes back after reading ``value``: everything, minus its three known losses."""
    if kind in {"run_output", "team_run_output"}:
        return _reread_run(value)
    if kind == "message":
        return _reread_message(value)
    if kind == "audio":
        return _without_audio_format(value)
    return value


def _agno_reread(kind: str, value: dict[str, Any]) -> dict[str, Any]:
    if kind in _MEDIA_KEYS:
        key = _MEDIA_KEYS[kind]
        return Message.from_dict({"role": "user", key: [deepcopy(value)]}).to_dict()[key][0]
    return _AGNO_TYPES[kind].from_dict(deepcopy(value)).to_dict()


def test_agno_rereads_every_run_fixture_with_only_its_known_losses() -> None:
    """Agno's readers keep every stored field except an empty tool list and audio formats, and add empty events."""
    files = sorted((FIXTURES / "runs").glob("*.json"))
    assert {path.stem for path in files} == _AGNO_TYPES.keys() | _MEDIA_KEYS.keys()
    mismatches = [
        f"{path.stem}/{case}"
        for path in files
        for case, value in json.loads(path.read_text(encoding="utf-8")).items()
        if _agno_reread(path.stem, value) != _expected_agno_reread(path.stem, value)
    ]
    assert mismatches == []
