"""The one golden regeneration command reproduces the goldens it owns, through each family's support functions.

The parity tests compare each golden with what its family's support functions compute; these tests cover what the
command adds: writing a file, routing every provider family to its own adapter, and each family's file layout.
Rebuilding every registered tool takes too long for this module, so the tool schemas are left to
``tests/tool_system/test_agno_toolkits.py``, which compares every tool's entry and each schema file's text.
"""

from __future__ import annotations

import socket
from pathlib import Path
from typing import TYPE_CHECKING

import pytest

from tests.agno_parity import prompt_scenarios, regenerate_goldens
from tests.agno_parity.golden_files import render, write
from tests.providers.golden_support import (
    FIXTURE_ROOT,
    golden_id,
    golden_request,
    hide_provider_environment,
    load_golden,
    output_schema_from_golden,
)
from tests.tool_system.agno_parity_support import ARGUMENTS_PATH

if TYPE_CHECKING:
    from collections.abc import Callable


def test_writing_a_golden_reports_whether_its_text_changed(tmp_path: Path) -> None:
    """A golden is written as rendered, an unchanged one is left alone, and its key order is part of its text."""
    path = tmp_path / "golden.json"

    assert write(path, {"b": 1, "a": [1]})
    assert path.read_text(encoding="utf-8") == render({"b": 1, "a": [1]}) == '{\n  "b": 1,\n  "a": [\n    1\n  ]\n}\n'
    assert not write(path, {"b": 1, "a": [1]})
    assert write(path, {"a": [1], "b": 1})


@pytest.mark.asyncio
@pytest.mark.parametrize("path", sorted(FIXTURE_ROOT.glob("*/plain_turn.json")), ids=golden_id)
async def test_every_provider_family_regenerates_through_its_own_adapter(
    path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Each family's plain turn regenerates byte for byte, so the command replays it on that family's adapter."""
    hide_provider_environment(monkeypatch)

    assert render(await regenerate_goldens.regenerated_provider_golden(path)) == path.read_text(encoding="utf-8")


@pytest.mark.asyncio
async def test_a_prompt_scenario_regenerates_under_its_own_temporary_root() -> None:
    """A scenario that names its temporary root regenerates byte for byte, with the root normalized away."""
    path = prompt_scenarios.golden_path("agent_anthropic")
    assert "<TMP>" in path.read_text(encoding="utf-8")

    assert render(await regenerate_goldens.regenerated_prompt_golden("agent_anthropic")) == path.read_text(
        encoding="utf-8",
    )


def test_a_temporary_root_reached_through_a_symlink_normalizes_to_one_placeholder(tmp_path: Path) -> None:
    """A root whose real path contains its own path, as macOS's ``/var`` and ``/private/var`` do, becomes ``<TMP>``.

    Each root is normalized from both its path and its real path; twenty roots with different names make an order
    that depends on string hashing show up.
    """
    for index in range(20):
        root = tmp_path / f"root-{index}"
        real = Path(f"{tmp_path / 'private'}{root}")
        real.mkdir(parents=True)
        root.symlink_to(real)
        assert str(root) in str(root.resolve())

        normalized = prompt_scenarios.normalize({"real": f"{root.resolve()}/a", "linked": f"{root}/b"}, root)

        assert normalized == {"real": "<TMP>/a", "linked": "<TMP>/b"}


@pytest.mark.asyncio
async def test_the_argument_golden_regenerates_with_sorted_keys() -> None:
    """Every argument case runs again and the file keeps its sorted layout."""
    assert render(await regenerate_goldens.argument_golden()) == ARGUMENTS_PATH.read_text(encoding="utf-8")


@pytest.mark.parametrize(
    "family",
    [regenerate_goldens.knowledge_goldens, regenerate_goldens.event_goldens],
    ids=["knowledge", "events"],
)
def test_the_knowledge_and_event_goldens_regenerate_byte_for_byte(family: Callable[[], dict[Path, object]]) -> None:
    """Each file the family writes holds exactly what it regenerates."""
    for path, golden in family().items():
        assert render(golden) == path.read_text(encoding="utf-8"), path


def test_regenerating_writes_every_family_and_lists_the_files_it_changed(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Every family's goldens are written, and only the files whose text changed are listed."""
    unchanged = tmp_path / "unchanged.json"
    unchanged.write_text(render({"same": True}), encoding="utf-8")
    families = ("provider_goldens", "prompt_goldens", "tool_goldens", "knowledge_goldens", "event_goldens")
    for name in families:
        goldens = {tmp_path / f"{name}.json": {"family": name}, unchanged: {"same": True}}
        monkeypatch.setattr(regenerate_goldens, name, lambda goldens=goldens: goldens)

    changed = regenerate_goldens.regenerate()

    assert changed == [tmp_path / f"{name}.json" for name in families]
    assert [path.read_text(encoding="utf-8") for path in changed] == [render({"family": name}) for name in families]


def test_a_failing_family_leaves_every_golden_as_it_was(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """A family that fails, such as the tools without every extra installed, stops the run before anything is written."""
    written = tmp_path / "provider.json"
    monkeypatch.setattr(regenerate_goldens, "provider_goldens", lambda: {written: {"family": "provider"}})

    def missing_extra() -> dict[Path, object]:
        msg = "an extra is missing"
        raise SystemExit(msg)

    monkeypatch.setattr(regenerate_goldens, "prompt_goldens", missing_extra)

    with pytest.raises(SystemExit, match="an extra is missing"):
        regenerate_goldens.regenerate()

    assert not written.exists()


def test_nothing_connects_while_the_goldens_regenerate() -> None:
    """Every connection and name lookup is refused, so no provider or service is reached."""
    with regenerate_goldens.offline(), socket.socket() as connection:
        with pytest.raises(OSError, match="refuses network connections"):
            connection.connect(("127.0.0.1", 9))
        with pytest.raises(OSError, match="refuses network connections"):
            socket.getaddrinfo("example.org", 443)
        with pytest.raises(OSError, match="refuses network connections"):
            socket.gethostbyname("example.org")


def test_a_golden_input_message_with_a_storage_flag_is_refused() -> None:
    """Input messages are turns, so a storage-only key such as ``from_history`` fails before anything is sent."""
    golden = load_golden(FIXTURE_ROOT / "anthropic" / "system_history.json")
    golden["input"]["messages"][1]["from_history"] = True

    with pytest.raises(AssertionError, match="storage-only keys"):
        golden_request(golden["input"])


def test_an_output_schema_that_changed_since_its_golden_is_refused() -> None:
    """A golden's recorded JSON schema must still be its class's, so a changed schema fails instead of replaying."""
    with pytest.raises(AssertionError, match="no longer matches"):
        output_schema_from_golden({"model": "mindroom.routing:_RoutingSuggestion", "json_schema": {}})
