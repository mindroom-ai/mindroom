"""The owned OpenAI-family adapters send the requests and parse the replies the Agno-backed layer did."""

from __future__ import annotations

from typing import TYPE_CHECKING

import pytest

from tests.providers.openai_family_support import golden_paths, load_golden, replay_golden

if TYPE_CHECKING:
    from pathlib import Path

GOLDENS = golden_paths()


def test_every_openai_family_golden_is_replayed() -> None:
    """The parity suite covers every golden of the families the owned adapters serve."""
    families = {path.parent.name for path in GOLDENS}
    assert families == {
        "openai_chat",
        "openai_responses",
        "codex",
        "azure",
        "openrouter",
        "deepseek",
        "zai",
        "llama_cpp",
        "kimi",
        "groq",
        "cerebras",
    }
    assert len(GOLDENS) == 200


@pytest.mark.asyncio
@pytest.mark.parametrize("path", GOLDENS, ids=lambda path: f"{path.parent.name}/{path.stem}")
async def test_owned_adapter_reproduces_golden(path: Path, tmp_path: Path) -> None:
    """Request method, URL, kept headers, and body match, and the parsed reply matches field for field."""
    golden = load_golden(path)
    request, parsed, turn = await replay_golden(golden, tmp_path)
    assert request == golden["request"]
    assert parsed == golden["parsed"]
    expected_stop = "tool_use" if golden["parsed"]["message"].get("tool_calls") else "end"
    assert turn.stop_reason == expected_stop
