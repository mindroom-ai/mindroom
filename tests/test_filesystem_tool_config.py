"""The file and coding tool names select the filesystem toolkit wherever a tool name is authored or stored."""

from __future__ import annotations

import re
from typing import TYPE_CHECKING, Any

import pytest
from pydantic import ValidationError

from mindroom.config.contacts import ContactsConfig
from mindroom.config.listening import ListenConfig
from mindroom.config.main import Config, ConfigRuntimeValidationError
from mindroom.config.models import BackgroundToolJobsConfig, DefaultsConfig
from mindroom.config.personal_agents import GeneratedAgentEdits
from mindroom.config.resolution import authored_tool_entries
from mindroom.constants import resolve_runtime_paths
from mindroom.contacts.store import ContactSide
from mindroom.custom_tools.script import ScriptTools
from mindroom.delegation.personas import inline_persona
from mindroom.dynamic_workflows.spec import SubagentParticipant
from mindroom.workers.protocol import ExecuteRequest, LeaseRequest

if TYPE_CHECKING:
    from pathlib import Path

    from mindroom.constants import RuntimePaths


def _config(tools: list[Any], **agent: object) -> Config:
    return Config.model_validate(
        {
            "models": {"default": {"provider": "openai", "id": "x"}},
            "agents": {"a": {"display_name": "A", "tools": tools, **agent}},
        },
    )


def _entries(tools: list[Any]) -> list[object]:
    return [entry.model_dump() for entry in _config(tools).agents["a"].tools]


@pytest.mark.parametrize("name", ["file", "coding", "filesystem"])
def test_every_name_of_the_toolkit_loads_as_filesystem(name: str) -> None:
    """Each spelling is one filesystem entry, saved under its registered name."""
    assert _entries([name, "shell"]) == ["filesystem", "shell"]
    assert _entries([{name: {"defer": True, "initial": True}}]) == [{"filesystem": {"defer": True, "initial": True}}]


def _layered(default_tools: list[Any], agent_tools: list[Any], **agent: object) -> Config:
    return Config.model_validate(
        {
            "models": {"default": {"provider": "openai", "id": "x"}},
            "defaults": {"tools": default_tools},
            "agents": {"a": {"display_name": "A", "tools": agent_tools, **agent}},
        },
    )


def _effective(config: Config) -> list[tuple[str, dict[str, object], bool, bool]]:
    return [
        (entry.name, dict(entry.tool_config_overrides), entry.defer, entry.initial)
        for entry in authored_tool_entries(config, "a")
        if entry.name == "filesystem"
    ]


def test_plain_entries_naming_one_toolkit_keep_the_first_position() -> None:
    """Two spellings with no settings and the same lazy flags are one entry where the first one stood."""
    assert _entries(["shell", "file", "python", "coding"]) == ["shell", "filesystem", "python"]
    both_deferred = [{"file": {"defer": True}}, {"coding": {"defer": True}}]
    assert _entries(both_deferred) == [{"filesystem": {"defer": True}}]


@pytest.mark.parametrize(
    "tools",
    [
        ["file", {"coding": {"defer": True, "initial": True}}],
        [{"coding": {"defer": True}}, "filesystem"],
        [{"file": {"exclude_tools": ["write_file"]}}, "coding"],
        ["filesystem", {"coding": {"enable_delete_file": True}}],
    ],
)
def test_entries_naming_one_toolkit_with_settings_or_other_flags_fail_naming_both(tools: list[Any]) -> None:
    """Two spellings never merge settings or lazy flags: the config fails and names both entries."""
    first, second = (entry if isinstance(entry, str) else next(iter(entry)) for entry in tools)
    with pytest.raises(ValidationError, match=f"'{first}' and '{second}' both select filesystem"):
        _config(tools)


def test_one_spelling_twice_is_still_a_duplicate() -> None:
    """Only different spellings are one entry; the same name twice keeps failing as before."""
    with pytest.raises(ValidationError, match="Duplicate"):
        _config(["coding", "coding"])


def test_plain_spellings_across_defaults_and_agent_build_one_toolkit() -> None:
    """A plain defaults entry and a plain agent entry under other spellings are one filesystem toolkit."""
    assert _effective(_layered(["file"], ["coding", "shell"])) == [("filesystem", {}, False, False)]


@pytest.mark.parametrize(
    ("default_tools", "agent_tools"),
    [
        ([{"file": {"exclude_tools": ["write_file"]}}], ["coding"]),
        (["file"], [{"coding": {"exclude_tools": ["grep"]}}]),
        (["filesystem"], [{"coding": {"defer": True, "initial": True}}]),
    ],
)
def test_spellings_across_defaults_and_agent_with_settings_or_flags_fail(
    default_tools: list[Any],
    agent_tools: list[Any],
) -> None:
    """A read-only default never meets an agent entry under another spelling: the config fails naming both."""
    default_name, agent_name = (
        next(iter(tools[0])) if isinstance(tools[0], dict) else tools[0] for tools in (default_tools, agent_tools)
    )
    with pytest.raises(ValidationError, match=f"'{agent_name}' and '{default_name}' both select filesystem"):
        _layered(default_tools, agent_tools)
    without_defaults = _layered(default_tools, agent_tools, include_default_tools=False)
    assert _effective(without_defaults) == _effective(_layered([], agent_tools))


def test_one_spelling_across_defaults_and_agent_inherits_as_any_tool_does() -> None:
    """The same spelling in both layers keeps ordinary inheritance: default settings under the agent's own."""
    config = _layered([{"file": {"exclude_tools": ["write_file"]}}], [{"file": {"defer": True}}])
    assert _effective(config) == [("filesystem", {"exclude_tools": ["write_file"]}, True, False)]


def _load(runtime_paths: RuntimePaths, entry: object, *, layer: str) -> Config:
    data: dict[str, Any] = {
        "models": {"default": {"provider": "openai", "id": "x"}},
        "defaults": {"tools": [entry] if layer == "defaults" else []},
        "agents": {"a": {"display_name": "A", "tools": [entry] if layer == "agent" else []}},
    }
    return Config.validate_with_runtime(data, runtime_paths)


@pytest.mark.parametrize("layer", ["defaults", "agent"])
@pytest.mark.parametrize(
    ("overrides", "replacement"),
    [
        ({"enable_save_file": False}, "filesystem: {exclude_tools: [write_file]}"),
        ({"enable_replace_file_chunk": None}, "filesystem: {exclude_tools: [edit_file]}"),
        ({"enable_read_file": True}, "filesystem: {exclude_tools: [read_file]}"),
        ({"enable_read_file_chunk": False}, "filesystem: {exclude_tools: [read_file]}"),
        ({"enable_list_files": False}, "filesystem: {exclude_tools: [ls]}"),
        ({"enable_search_files": False}, "filesystem: {exclude_tools: [find_files]}"),
        ({"enable_search_content": False}, "filesystem: {exclude_tools: [grep]}"),
        ({"all": False}, "filesystem: {enable_delete_file: true}"),
        ({"max_file_length": 10}, "remove"),
        ({"max_file_lines": 10}, "remove"),
        ({"line_separator": "\n"}, "remove"),
        ({"exclude_patterns": []}, "remove"),
        ({"expose_base_directory": True}, "remove"),
        ({"exclude_tools": "read_file, save_file"}, "filesystem: {exclude_tools: [write_file]}"),
        ({"include_tools": ["list_files"]}, "filesystem: {include_tools: [ls]}"),
    ],
)
def test_an_old_agno_setting_on_a_file_entry_fails_config_load_naming_its_replacement(
    tmp_path: Path,
    layer: str,
    overrides: dict[str, object],
    replacement: str,
) -> None:
    """No old setting is translated: whatever its value, the entry fails and says what to write instead."""
    runtime_paths = resolve_runtime_paths(config_path=tmp_path / "config.yaml", storage_path=tmp_path / "storage")
    setting = next(iter(overrides))
    with pytest.raises(
        ConfigRuntimeValidationError,
        match=rf"filesystem\.{setting}.* was removed; .*{re.escape(replacement)}",
    ):
        _load(runtime_paths, {"file": overrides}, layer=layer)


def test_current_filesystem_settings_load_under_every_spelling(tmp_path: Path) -> None:
    """The toolkit's own settings load on file, coding, and filesystem entries alike."""
    runtime_paths = resolve_runtime_paths(config_path=tmp_path / "config.yaml", storage_path=tmp_path / "storage")
    overrides = {"exclude_tools": ["write_file"], "enable_delete_file": True}
    for name in ("file", "coding", "filesystem"):
        assert _load(runtime_paths, {name: overrides}, layer="agent").agents["a"].tools[0].overrides == overrides


def test_tool_name_lists_take_every_spelling() -> None:
    """worker_tools, listening, background-job exclusions, and personal agent edits read file and coding."""
    config = _config(["filesystem", "shell"], worker_tools=["shell", "file", "python", "coding"])
    assert config.agents["a"].worker_tools == ["shell", "filesystem", "python"]
    assert DefaultsConfig(worker_tools=["file", "coding"]).worker_tools == ["filesystem"]
    assert ListenConfig(rooms=["lobby"], tools=["memory", "coding.read_file"]).tools == [
        "memory",
        "filesystem.read_file",
    ]
    assert BackgroundToolJobsConfig(exclude_toolkits=["file"]).exclude_toolkits == ["filesystem"]
    assert GeneratedAgentEdits(tools=["file", "shell"]).tools == ["filesystem", "shell"]


def test_stored_and_configured_grants_under_an_old_name_are_kept_as_written() -> None:
    """A stored contact grant and a configured contact default keep their spelling, so they match no toolkit."""
    assert ContactSide.model_validate({"tools": ["coding", "file"]}).tools == ["coding", "file"]
    assert ContactsConfig(tools=["coding"]).tools == ["coding"]


def test_subagent_tools_take_every_spelling() -> None:
    """run_subagent's and a profile's tools name the caller's filesystem toolkit by any spelling."""
    persona = inline_persona("You review.", ["coding", "file.read_file", "shell"], None, None)
    assert persona.tools == ("filesystem", "filesystem.read_file", "shell")


def test_workflow_script_and_worker_tool_names_take_every_spelling() -> None:
    """A workflow participant, the script toolkit's grants, and a worker call name filesystem by any spelling."""
    participant = SubagentParticipant.model_validate({"id": "p", "system_prompt": "P", "tools": ["coding", "file"]})
    assert participant.tools == ["filesystem"]
    assert ScriptTools(allowed_tools=["file", " coding.read_file "]).limits.allowed_tools == (
        "filesystem",
        "filesystem.read_file",
    )
    request = ExecuteRequest(protocol=1, tool_name="coding", function_name="read_file")
    lease = LeaseRequest(protocol=1, tool_name="file", function_name="read_file", ttl_seconds=30)
    # A lease is consumed under the name of the call it serves, so both read an old primary's name alike.
    assert (request.tool_name, lease.tool_name) == ("filesystem", "filesystem")


def test_openclaw_compat_brings_filesystem() -> None:
    """The OpenClaw preset expands to the filesystem toolkit."""
    assert "filesystem" in Config.expand_tool_names(["openclaw_compat"])


def _load_rules(tmp_path: Path, *rules: str | tuple[str, str]) -> Config:
    """Load rules given as a match glob, which requires approval, or as a glob and its action."""
    runtime_paths = resolve_runtime_paths(config_path=tmp_path / "config.yaml", storage_path=tmp_path / "storage")
    pairs = [(rule, "require_approval") if isinstance(rule, str) else rule for rule in rules]
    data = {
        "models": {"default": {"provider": "openai", "id": "x"}},
        "agents": {"a": {"display_name": "A", "tools": ["python"]}},
        "tool_approval": {"rules": [{"match": match, "action": action} for match, action in pairs]},
    }
    return Config.validate_with_runtime(data, runtime_paths)


@pytest.mark.parametrize(
    ("match", "renamed"),
    [
        ("save_file", "save_file is now write_file"),
        ("search_*", "search_files is now find_files"),
        ("replace_file_chunk", "replace_file_chunk is now edit_file"),
        # It still matches read_file, but neither of the two writes it used to gate.
        ("*a*", "save_file is now write_file"),
        ("*_chunk", "replace_file_chunk is now edit_file"),
    ],
)
def test_an_approval_rule_that_stops_deciding_a_renamed_function_fails(
    tmp_path: Path,
    match: str,
    renamed: str,
) -> None:
    """A rule that decided a retired function and not its replacement fails, naming the rule and the new name."""
    expected = rf"tool_approval\.rules\[1\]\.match {re.escape(repr(match))} .*{re.escape(renamed)}"
    with pytest.raises(ConfigRuntimeValidationError, match=expected):
        _load_rules(tmp_path, "run_shell_command", match)


def test_an_earlier_approval_rule_that_takes_the_replacement_from_its_gate_fails(tmp_path: Path) -> None:
    """The first matching rule decides: write_* auto-approving write_file ahead of the *_file gate fails."""
    with pytest.raises(
        ConfigRuntimeValidationError, match=r"rules\[1\]\.match '\*_file' .*save_file is now write_file"
    ):
        _load_rules(tmp_path, ("write_*", "auto_approve"), ("*_file", "require_approval"))
    with pytest.raises(ConfigRuntimeValidationError, match=r"rules\[0\]\.match 'save_file'"):
        _load_rules(tmp_path, "save_file", "*")
    with pytest.raises(ConfigRuntimeValidationError, match=r"rules\[1\]\.match 'save_file'"):
        _load_rules(tmp_path, "write_file", "save_file")
    # In the other order the gate still decides both names.
    assert _load_rules(tmp_path, ("*_file", "require_approval"), ("write_*", "auto_approve"))


@pytest.mark.parametrize(
    "match",
    ["write_file", "edit_file", "grep", "list_files", "*", "*_file", "read_*", "read_file*", "*_files"],
)
def test_approval_rules_that_decide_the_new_names_as_they_did_the_old_load(tmp_path: Path, match: str) -> None:
    """Rules for the new names, python's live list_files, and globs matching both names of a rename load."""
    assert _load_rules(tmp_path, match).tool_approval.rules[0].match == match
