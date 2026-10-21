"""The Agno-backed runtime still writes, loads, and reads exactly what the storage, history, and event goldens hold.

Task B8 deletes this test with Agno; the owned storage, history, and codec tests keep reading the fixtures.
"""

from __future__ import annotations

import json
import os
import sqlite3
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

from mindroom.constants import MATRIX_RESPONSE_EVENT_ID_METADATA_KEY
from tests.agno_parity.capture_storage import (
    FIXTURES,
    STORES,
    StoreSpec,
    derive_goldens,
    owned_goldens,
    restore_store,
    run_row,
)

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


def _committed_store(label: str, storage_root: Path) -> tuple[StoreSpec, Path]:
    spec = next(spec for spec in STORES if spec.label == label)
    return spec, restore_store(spec, (FIXTURES / "storage" / f"{label}.sql").read_text(encoding="utf-8"), storage_root)


def _run_statuses(db_file: Path, runs_table: str) -> dict[str, str]:
    connection = sqlite3.connect(db_file)
    try:
        return dict(connection.execute(f'SELECT run_id, status FROM "{runs_table}"'))  # noqa: S608
    finally:
        connection.close()


def test_pre_resume_dumps_hold_the_runs_the_stores_hold_resumed(tmp_path: Path) -> None:
    """Each dump taken before an approval resume holds paused exactly the runs its committed store holds completed."""
    dumps = sorted((FIXTURES / "storage").glob("*__before_*.sql"))
    assert [path.stem for path in dumps] == [
        "agent_code__before_run-approved",
        "agent_code__before_run-delegation-approved",
        "agent_code__before_run-rejected",
        "agent_helper__before_run-delegation-approved",
        "team_squad__before_team-approved",
    ]
    for path in dumps:
        label = path.stem.partition("__before_")[0]
        spec, resumed_db = _committed_store(label, tmp_path / "resumed" / path.stem)
        paused_db = restore_store(spec, path.read_text(encoding="utf-8"), tmp_path / "paused" / path.stem)
        paused = _run_statuses(paused_db, f"{spec.session_table}_runs")
        resumed = _run_statuses(resumed_db, f"{spec.session_table}_runs")
        assert set(paused.values()) == {"PAUSED"}, path.name
        assert {resumed[run_id] for run_id in paused} == {"COMPLETED"}, path.name


def test_response_linkage_resaves_the_stored_row_as_agno_rereads_it(tmp_path: Path) -> None:
    """A delivered run is re-saved as Agno re-reads it, plus the reply's event id and a new update time."""
    cases = json.loads((FIXTURES / "storage" / "response_linkage.json").read_text(encoding="utf-8"))
    assert sorted(cases) == ["agent_code:run-media", "team_squad:team-completed"]
    for key, case in cases.items():
        label, _, run_id = key.partition(":")
        spec, db_file = _committed_store(label, tmp_path / label)
        before = case["before"]
        assert run_row(spec, db_file, run_id) == before
        kind = "run_output" if "agent_id" in before["run_data"] else "team_run_output"
        expected = _expected_agno_reread(kind, before["run_data"])
        expected["metadata"] = {
            **expected["metadata"],
            MATRIX_RESPONSE_EVENT_ID_METADATA_KEY: case["response_event_id"],
        }
        assert {**case["after"], "updated_at": None} == {**before, "run_data": expected, "updated_at": None}
        assert case["after"]["updated_at"] > before["updated_at"]
