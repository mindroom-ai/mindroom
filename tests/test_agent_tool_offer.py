"""Which toolkits one agent build offers, for each config rule and per-turn input that decides it."""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

import pytest

from mindroom.agents import create_agent
from mindroom.config.main import Config
from mindroom.constants import RuntimePaths, resolve_runtime_paths
from mindroom.tool_system.worker_routing import ToolExecutionIdentity
from tests.agent_tool_helpers import offered_function, source_toolkit
from tests.identity_helpers import persist_entity_accounts

if TYPE_CHECKING:
    from pathlib import Path


def _runtime_paths(tmp_path: Path) -> RuntimePaths:
    config_path = tmp_path / "config.yaml"
    config_path.write_text("agents: {}\n", encoding="utf-8")
    return resolve_runtime_paths(
        config_path=config_path,
        storage_path=tmp_path / "mindroom_data",
        process_env={"MATRIX_HOMESERVER": "http://localhost:8008", "MINDROOM_NAMESPACE": ""},
    )


def _identity(*, room: bool) -> ToolExecutionIdentity:
    return ToolExecutionIdentity(
        channel="matrix",
        agent_name="code",
        requester_id="@user:example.org",
        room_id="!room:example.org" if room else None,
        thread_id=None,
        resolved_thread_id=None,
        session_id="session",
    )


def _offered(tmp_path: Path, agent: dict[str, Any], *, room: bool = True, depth: int = 0, **raw: object) -> list[str]:
    runtime_paths = _runtime_paths(tmp_path)
    data: dict[str, Any] = {
        "defaults": {"tools": [], "allow_self_config": False},
        "agents": {"code": {"display_name": "Code", "role": "Write code", **agent}, "helper": {"display_name": "H"}},
        "models": {"default": {"provider": "openai", "id": "gpt-4o-mini"}},
        **raw,
    }
    config = Config.validate_with_runtime(data, runtime_paths)
    persist_entity_accounts(config, runtime_paths)
    build = create_agent(
        "code",
        config,
        runtime_paths,
        execution_identity=_identity(room=room),
        knowledge=None,
        active_model_name=None,
        delegation_depth=depth,
    )
    return sorted(toolkit.name for toolkit in build.spec.toolkits)


@pytest.mark.parametrize(
    ("agent", "kwargs", "expected"),
    [
        pytest.param({"tools": ["calculator"]}, {}, ["calculator"], id="plain"),
        pytest.param(
            {"tools": ["calculator", "memory"], "memory_backend": "none"},
            {},
            ["calculator"],
            id="memory-none",
        ),
        pytest.param(
            {"tools": ["calculator", "memory"], "memory_backend": "mem0"},
            {},
            ["calculator", "memory"],
            id="memory-on",
        ),
        pytest.param({"tools": ["calculator", "knowledge_graph"]}, {}, ["calculator"], id="graph-off-drops-listed"),
        pytest.param({"tools": ["delegate"]}, {}, [], id="delegate-without-targets"),
        pytest.param({"delegate_to": ["helper"]}, {}, ["delegate"], id="delegate-injected"),
        pytest.param({"delegate_to": ["helper"]}, {"depth": 3}, [], id="delegate-at-max-depth"),
        pytest.param({"allow_self_config": True}, {}, ["self_config"], id="self-config"),
        pytest.param({"skill_learning": {"enabled": True}}, {}, ["skill_manage"], id="skill-learning"),
        pytest.param(
            {"tools": ["matrix_message"]},
            {},
            ["attachments", "matrix_message", "matrix_room"],
            id="implied-in-room",
        ),
        pytest.param(
            {"tools": ["matrix_message"], "delegate_to": ["helper"]},
            {"room": False},
            ["delegate"],
            id="room-tools-need-a-room",
        ),
        pytest.param(
            {"tools": ["sleep", {"calculator": {"defer": True}}]},
            {},
            ["calculator", "dynamic_tools", "sleep"],
            id="deferred",
        ),
        pytest.param(
            {"tools": ["sleep", {"calculator": {"defer": True, "initial": True}}]},
            {},
            ["calculator", "sleep"],
            id="deferred-initial",
        ),
        pytest.param({"include_default_tools": True}, {}, ["sleep"], id="defaults-merged"),
        pytest.param({"include_default_tools": False}, {}, [], id="defaults-skipped"),
    ],
)
def test_one_build_offers_the_toolkits_config_and_turn_decide(
    tmp_path: Path,
    agent: dict[str, Any],
    kwargs: dict[str, Any],
    expected: list[str],
) -> None:
    """Each config rule and per-turn input adds or drops exactly the toolkits it governs."""
    raw = {"defaults": {"tools": ["sleep"], "allow_self_config": False}} if "include_default_tools" in agent else {}
    # A Matrix room turn always offers the room runtime tools.
    in_room = ["invite_router"] if kwargs.get("room", True) else []
    assert _offered(tmp_path, agent, **kwargs, **raw) == sorted({*expected, *in_room})


@pytest.mark.parametrize("authored", [["/opt/a", "/opt/b"], "/opt/a, /opt/b"])
def test_an_agent_only_tool_setting_reaches_the_toolkit_once_normalized(tmp_path: Path, authored: object) -> None:
    """A per-agent list override reaches the shell toolkit as the comma-joined text its constructor takes."""
    runtime_paths = _runtime_paths(tmp_path)
    config = Config.validate_with_runtime(
        {
            "defaults": {"tools": [], "allow_self_config": False},
            "agents": {"code": {"display_name": "Code", "tools": [{"shell": {"shell_path_prepend": authored}}]}},
            "models": {"default": {"provider": "openai", "id": "gpt-4o-mini"}},
        },
        runtime_paths,
    )
    persist_entity_accounts(config, runtime_paths)
    build = create_agent(
        "code",
        config,
        runtime_paths,
        execution_identity=_identity(room=True),
        knowledge=None,
        active_model_name=None,
    )
    shell = source_toolkit(offered_function(build, "run_shell_command"))
    assert shell._shell_path_prepend == "/opt/a, /opt/b"
