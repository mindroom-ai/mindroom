"""Start the ``claude_code`` route against the scripted ``fake_claude.py`` and read what that process saw."""

from __future__ import annotations

import base64
import json
import re
import sys
from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING, Any

from mindroom.config.models import ModelConfig
from mindroom.constants import resolve_runtime_paths
from mindroom.llm.messages import Message
from mindroom.llm.provider import ToolSpec
from mindroom.providers.claude_code.builder import build_provider
from mindroom.tool_system.worker_routing import ToolExecutionIdentity

if TYPE_CHECKING:
    from mindroom.constants import RuntimePaths
    from mindroom.providers.claude_code.adapter import ClaudeCodeProvider

_FAKE = Path(__file__).with_name("fake_claude.py")
PNG = base64.b64decode(
    "iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAYAAAAfFcSJAAAADUlEQVR42mP8z8BQDwAEhQGAhKmMIQAAAABJRU5ErkJggg==",
)
MARKERS = {"a": "LILAC-HERON", "b": "LEMON-IBIS", "c": "COPPER-OTTER"}
LOOKUP = ToolSpec(
    name="lookup",
    description="Look up a marker by key.",
    parameters={"type": "object", "properties": {"key": {"type": "string"}}, "required": ["key"]},
    strict=None,
)
# One invented conversation, whose requests may continue one another's idle process.
CONVERSATION = ToolExecutionIdentity(
    channel="matrix",
    agent_name="code",
    requester_id="@alice:invented.example",
    room_id="!room:invented.example",
    thread_id="$thread",
    resolved_thread_id="$thread",
    session_id="session-1",
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

    @property
    def runtime_paths(self) -> RuntimePaths:
        """The runtime whose storage this executable's processes start under."""
        return resolve_runtime_paths(
            config_path=self.storage_root.parent / "config.yaml",
            storage_path=self.storage_root,
        )

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
        return build_provider(model_config, self.runtime_paths, execution_identity=execution_identity)


def left_behind(cli: FakeCli) -> list[Path]:
    """Return files the route left in its private directory, apart from the reusable launcher."""
    home = cli.storage_root / "claude_code"
    return sorted(path for path in home.rglob("*") if path.is_file() and path != home / "exec-claude")


def fake_cli(tmp_path: Path, *processes: dict[str, Any]) -> FakeCli:
    """Write a ``claude`` executable that plays ``processes``, one per start."""
    tmp_path.mkdir(parents=True, exist_ok=True)
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


def lookup(key: str) -> str:
    """Look up a marker by key.

    Args:
        key: The marker's key.

    """
    return MARKERS[key]


def tagged_json(text: str, tag: str) -> list[dict[str, Any]]:
    """Decode the JSON payloads enclosed in the renderer's matching history tags."""
    return [json.loads(body) for body in re.findall(rf"<{tag}>\n(.*?)\n</{tag}>", text, flags=re.DOTALL)]


def user(text: str) -> Message:
    """Return a user message."""
    return Message(role="user", content=text)


def result(call_id: str, text: str) -> Message:
    """Return the ``lookup`` result that answers ``call_id``."""
    return Message(role="tool", content=text, tool_call_id=call_id, tool_name="lookup")
