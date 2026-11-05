"""The ``listen:`` block of an agent: defaults, bounds, and what config validation rejects."""

from __future__ import annotations

from typing import TYPE_CHECKING

import pytest
from pydantic import ValidationError

from mindroom.config.agent import AgentConfig
from mindroom.config.listening import ListenConfig
from mindroom.config.main import Config
from tests.conftest import test_runtime_paths

if TYPE_CHECKING:
    from pathlib import Path

_MODELS = {"default": {"provider": "openai", "id": "gpt-4o"}, "cheap": {"provider": "openai", "id": "gpt-4o-mini"}}


def _config(listen: dict[str, object], tools: tuple[str, ...] = ("memory", "react")) -> dict[str, object]:
    return {
        "models": _MODELS,
        "agents": {"scribe": {"display_name": "Scribe", "rooms": ["lobby"], "tools": list(tools), "listen": listen}},
    }


def test_listen_defaults() -> None:
    """Only rooms is required; the rest take the documented defaults."""
    listen = ListenConfig(rooms=["lobby"])

    assert listen.quiet_seconds == 60
    assert listen.max_messages == 200
    assert listen.max_runs_per_hour == 6
    assert listen.tools == ["memory", "react"]
    assert listen.model is None


@pytest.mark.parametrize(
    ("field", "value"),
    [
        ("quiet_seconds", 9),
        ("max_messages", 0),
        ("max_messages", 501),
        ("max_runs_per_hour", 0),
        ("max_runs_per_hour", 61),
    ],
)
def test_listen_bounds(field: str, value: int) -> None:
    """Values outside the documented bounds are rejected, and the edges are accepted."""
    with pytest.raises(ValidationError):
        ListenConfig(rooms=["lobby"], **{field: value})
    edges = ListenConfig(rooms=["lobby"], quiet_seconds=10, max_messages=500, max_runs_per_hour=60)
    assert (edges.quiet_seconds, edges.max_messages, edges.max_runs_per_hour) == (10, 500, 60)


def test_listen_unknown_model_rejected() -> None:
    """A model alias that is not under ``models`` fails validation."""
    with pytest.raises(ValidationError, match="Unknown listen model for agent 'scribe': 'nope'"):
        Config.model_validate(_config({"rooms": ["lobby"], "model": "nope"}))

    config = Config.model_validate(_config({"rooms": ["lobby"], "model": "cheap"}))
    assert config.agents["scribe"].listen is not None
    assert config.agents["scribe"].listen.model == "cheap"


def test_listen_unknown_tool_rejected(tmp_path: Path) -> None:
    """An explicit tool that is no toolkit fails, as an unknown entry of ``tools:`` does."""
    runtime_paths = test_runtime_paths(tmp_path)
    with pytest.raises(ValueError, match=r"listen\.tools\[1\]\.not_a_tool: Unknown tool 'not_a_tool'"):
        Config.validate_with_runtime(
            _config({"rooms": ["lobby"], "tools": ["memory", "not_a_tool.run"]}),
            runtime_paths,
        )

    Config.validate_with_runtime(_config({"rooms": ["lobby"], "tools": ["memory"]}), runtime_paths)


def test_listen_unknown_room_rejected(tmp_path: Path) -> None:
    """A listened room resolves as ``room_thread_modes`` rooms do, so an outside alias fails."""
    with pytest.raises(ValueError, match=r"agents\.scribe\.listen\.rooms -> #elsewhere:example\.org"):
        Config.validate_with_runtime(_config({"rooms": ["#elsewhere:example.org"]}), test_runtime_paths(tmp_path))


def test_listen_rooms_required() -> None:
    """A listen block with no rooms, or an empty list, is rejected."""
    with pytest.raises(ValidationError):
        ListenConfig()  # ty: ignore[missing-argument]
    with pytest.raises(ValidationError):
        ListenConfig(rooms=[])


def test_listen_tools_must_name_one() -> None:
    """A silent turn with no tool could only spend tokens, so an empty ``listen.tools`` is rejected."""
    with pytest.raises(ValidationError, match="tools"):
        ListenConfig(rooms=["lobby"], tools=[])


def test_listen_matrix_message_rejected(tmp_path: Path) -> None:
    """``matrix_message`` posts to the room, so it is refused in ``listen.tools`` even when the agent has it."""
    with pytest.raises(
        ValueError,
        match=r"listen\.tools\[1\]\.matrix_message: Tool 'matrix_message' posts to the room, which a silent turn never does",
    ):
        Config.validate_with_runtime(
            _config(
                {"rooms": ["lobby"], "tools": ["react", "matrix_message.send_message"]},
                ("react", "matrix_message"),
            ),
            test_runtime_paths(tmp_path),
        )


def test_listen_on_a_private_agent_rejected() -> None:
    """A private agent runs one instance per requester, none of which could listen, so ``listen`` is refused."""
    authored = _config({"rooms": ["lobby"]})
    authored["agents"]["scribe"]["private"] = {"per": "user"}  # type: ignore[index]

    with pytest.raises(ValidationError, match="configure private or listen, not both"):
        Config.model_validate(authored)


def test_agent_without_listen_is_none() -> None:
    """Listening is opt-in."""
    assert AgentConfig(display_name="Scribe").listen is None


@pytest.mark.parametrize(
    ("listen", "tools", "rejected"),
    [
        ({"rooms": ["lobby"]}, ("memory",), r"listen\.tools\[1\]\.react: Tool 'react' is not one of the agent's tools"),
        (
            {"rooms": ["lobby"], "tools": ["memory.add_memory"]},
            ("react",),
            r"listen\.tools\[0\]\.memory: Tool 'memory' is not one of the agent's tools; add it to agents\.scribe\.tools",
        ),
    ],
    ids=["default-list", "function-entry"],
)
def test_listen_tool_outside_agent_tools_rejected(
    tmp_path: Path,
    listen: dict[str, object],
    tools: tuple[str, ...],
    rejected: str,
) -> None:
    """A silent turn only narrows the agent's own tools, so a listen tool the agent lacks fails, the default ones too."""
    with pytest.raises(ValueError, match=rejected):
        Config.validate_with_runtime(_config(listen, tools), test_runtime_paths(tmp_path))


def test_listen_default_tools_validate(tmp_path: Path) -> None:
    """The default ``memory`` and ``react`` validate for an agent that has both."""
    config = Config.validate_with_runtime(_config({"rooms": ["lobby"]}), test_runtime_paths(tmp_path))
    assert config.agents["scribe"].listen is not None
    assert config.agents["scribe"].listen.tools == ["memory", "react"]
