"""Start the ``claude_code`` route against the scripted ``fake_claude.py`` and read what that process saw."""

from __future__ import annotations

import json
import sys
from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING, Any

from mindroom.config.models import ModelConfig
from mindroom.constants import resolve_runtime_paths
from mindroom.llm.messages import Message
from mindroom.llm.provider import ToolSpec
from mindroom.providers.claude_code.adapter import ClaudeCodeProvider, build_provider

if TYPE_CHECKING:
    from mindroom.tool_system.worker_routing import ToolExecutionIdentity

_FAKE = Path(__file__).with_name("fake_claude.py")
LOOKUP = ToolSpec(
    name="lookup",
    description="Look up a marker by key.",
    parameters={"type": "object", "properties": {"key": {"type": "string"}}, "required": ["key"]},
    strict=None,
)


@dataclass(frozen=True)
class FakeCli:
    """A scripted ``claude`` executable and the log of what each process it started received."""

    path: Path
    log: Path
    storage_root: Path

    def events(self, event: str) -> list[dict[str, Any]]:
        """Return the logged entries of one kind, in order."""
        if not self.log.exists():
            return []
        entries = [json.loads(line) for line in self.log.read_text().splitlines()]
        return [entry for entry in entries if entry["event"] == event]

    def provider(
        self,
        *,
        execution_identity: ToolExecutionIdentity | None = None,
        **extra_kwargs: object,
    ) -> ClaudeCodeProvider:
        """Return the route's bare adapter, running this executable."""
        model_config = ModelConfig(
            provider="claude_code",
            id="claude-invented-1",
            extra_kwargs={"cli_path": str(self.path), **extra_kwargs},
        )
        runtime_paths = resolve_runtime_paths(
            config_path=self.storage_root.parent / "config.yaml",
            storage_path=self.storage_root,
        )
        return build_provider(model_config, runtime_paths, execution_identity=execution_identity)


def fake_cli(tmp_path: Path, *processes: dict[str, Any]) -> FakeCli:
    """Write a ``claude`` executable that plays ``processes``, one per start."""
    scenario = tmp_path / "scenario.json"
    scenario.write_text(json.dumps({"processes": list(processes)}))
    log = tmp_path / "claude.jsonl"
    path = tmp_path / "claude"
    path.write_text(f'#!/bin/sh\nexec "{sys.executable}" "{_FAKE}" "{scenario}" "{log}" "$@"\n')
    path.chmod(0o755)
    return FakeCli(path=path, log=log, storage_root=tmp_path / "mindroom_data")


def text_turn(text: str, **step: object) -> list[dict[str, Any]]:
    """Return a scripted turn that answers with ``text``."""
    return [{"text": text, **step}]


def call(call_id: str, key: str) -> dict[str, Any]:
    """Return a scripted ``lookup`` call."""
    return {"id": call_id, "name": "lookup", "input": {"key": key}}


def user(text: str) -> Message:
    """Return a user message."""
    return Message(role="user", content=text)


def result(call_id: str, text: str) -> Message:
    """Return the ``lookup`` result that answers ``call_id``."""
    return Message(role="tool", content=text, tool_call_id=call_id, tool_name="lookup")
