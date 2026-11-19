"""The file and coding tool names select the filesystem toolkit wherever a tool name is authored or stored."""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

import pytest
from pydantic import ValidationError

from mindroom.config.approval import ApprovalRuleConfig
from mindroom.config.contacts import ContactsConfig
from mindroom.config.listening import ListenConfig
from mindroom.config.main import Config, ConfigRuntimeValidationError
from mindroom.config.models import BackgroundToolJobsConfig, DefaultsConfig
from mindroom.config.personal_agents import GeneratedAgentEdits
from mindroom.constants import resolve_runtime_paths
from mindroom.contacts.store import ContactSide
from mindroom.delegation.personas import inline_persona

if TYPE_CHECKING:
    from pathlib import Path


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


def test_entries_naming_one_toolkit_merge_at_the_first_position() -> None:
    """Two spellings merge into the first one's place; defer holds only if both defer, initial if either has it."""
    assert _entries(["shell", "file", "python", {"coding": {"defer": True, "initial": True}}]) == [
        "shell",
        "filesystem",
        "python",
    ]
    merged = _entries([{"file": {"defer": True}}, {"coding": {"defer": True, "initial": True}}])
    assert merged == [{"filesystem": {"defer": True, "initial": True}}]
    assert _entries([{"coding": {"defer": True}}, "filesystem"]) == ["filesystem"]


def test_merged_entries_union_their_settings() -> None:
    """Settings of both entries apply, so a switched-off function stays off."""
    merged = _entries([{"file": {"enable_save_file": False}}, {"coding": {"enable_delete_file": True}}])
    assert merged == [{"filesystem": {"exclude_tools": ["write_file"], "enable_delete_file": True}}]


def test_merged_entries_with_different_values_fail_naming_both() -> None:
    """Two spellings that set one setting differently have no single reading."""
    with pytest.raises(ValidationError, match="'file' and 'coding' both select filesystem but set exclude_tools"):
        _config([{"file": {"exclude_tools": ["grep"]}}, {"coding": {"exclude_tools": ["ls"]}}])


def test_one_spelling_twice_is_still_a_duplicate() -> None:
    """Only different spellings merge; the same name twice keeps failing as before."""
    with pytest.raises(ValidationError, match="Duplicate"):
        _config(["coding", "coding"])


def test_defaults_tools_and_agent_tools_layer_across_spellings() -> None:
    """A defaults entry and an agent entry under other spellings layer like one tool's entries."""
    config = Config.model_validate(
        {
            "models": {"default": {"provider": "openai", "id": "x"}},
            "defaults": {"tools": [{"file": {"enable_save_file": False}}]},
            "agents": {"a": {"display_name": "A", "tools": ["coding"]}},
        },
    )
    assert [entry.model_dump() for entry in config.defaults.tools] == [{"filesystem": {"exclude_tools": ["write_file"]}}]
    assert [entry.name for entry in config.agents["a"].tools] == ["filesystem"]


@pytest.mark.parametrize(
    ("switch", "excluded"),
    [
        ("enable_save_file", "write_file"),
        ("enable_replace_file_chunk", "edit_file"),
        ("enable_list_files", "ls"),
        ("enable_search_files", "find_files"),
        ("enable_search_content", "grep"),
    ],
)
def test_legacy_file_switches_map_to_exclude_tools(switch: str, excluded: str) -> None:
    """A function a file entry switched off is excluded from filesystem."""
    assert _entries([{"file": {switch: False}}]) == [{"filesystem": {"exclude_tools": [excluded]}}]
    assert _entries([{"file": {switch: True}}]) == ["filesystem"]


def test_legacy_file_read_switches_exclude_read_file_only_together() -> None:
    """read_file goes only when both of file's reads were off, since either one read the file."""
    assert _entries([{"file": {"enable_read_file": False}}]) == ["filesystem"]
    assert _entries([{"file": {"enable_read_file_chunk": False}}]) == ["filesystem"]
    both = [{"file": {"enable_read_file": False, "enable_read_file_chunk": False}}]
    assert _entries(both) == [{"filesystem": {"exclude_tools": ["read_file"]}}]


def test_legacy_file_filters_are_renamed_and_unmatched_settings_dropped() -> None:
    """Retired function names in the filters become their replacements; settings with no counterpart go."""
    entry = {
        "file": {
            "exclude_tools": "save_file, search_content",
            "enable_list_files": False,
            "max_file_length": 10,
            "max_file_lines": 10,
            "line_separator": "\n",
            "exclude_patterns": [],
            "all": True,
            "expose_base_directory": True,
            "enable_delete_file": True,
        },
    }
    assert _entries([entry]) == [{"filesystem": {"exclude_tools": ["write_file", "grep", "ls"], "enable_delete_file": True}}]
    assert _entries([{"file": {"include_tools": ["read_file_chunk", "list_files"]}}]) == [
        {"filesystem": {"include_tools": ["read_file", "ls"]}},
    ]


def test_mapped_file_settings_load_with_the_runtime_and_coding_never_had_them(tmp_path: Path) -> None:
    """A mapped file entry passes the registry's checks; Agno's settings on coding, which never took them, fail."""
    runtime_paths = resolve_runtime_paths(config_path=tmp_path / "config.yaml", storage_path=tmp_path / "storage")

    def load(entry: object) -> Config:
        data = {"models": {"default": {"provider": "openai", "id": "x"}}, "agents": {"a": {"display_name": "A"}}}
        data["agents"]["a"]["tools"] = [entry]
        return Config.validate_with_runtime(data, runtime_paths)

    legacy = {"file": {"enable_save_file": False, "include_tools": "read_file_chunk, save_file, list_files"}}
    assert load(legacy).agents["a"].tools[0].overrides == {
        "include_tools": ["read_file", "write_file", "ls"],
        "exclude_tools": ["write_file"],
    }
    with pytest.raises(ConfigRuntimeValidationError, match="enable_save_file"):
        load({"coding": {"enable_save_file": False}})


def test_tool_name_lists_take_every_spelling() -> None:
    """worker_tools, contacts, listening, background-job exclusions, edits, and grants read file and coding."""
    config = _config(["filesystem", "shell"], worker_tools=["shell", "file", "python", "coding"])
    assert config.agents["a"].worker_tools == ["shell", "filesystem", "python"]
    assert DefaultsConfig(worker_tools=["file", "coding"]).worker_tools == ["filesystem"]
    assert ContactsConfig(tools=["coding"]).tools == ["filesystem"]
    assert ListenConfig(rooms=["lobby"], tools=["memory", "coding.read_file"]).tools == ["memory", "filesystem.read_file"]
    assert BackgroundToolJobsConfig(exclude_toolkits=["file"]).exclude_toolkits == ["filesystem"]
    assert GeneratedAgentEdits(tools=["file", "shell"]).tools == ["filesystem", "shell"]
    assert ContactSide.model_validate({"tools": ["coding", "file"]}).tools == ["filesystem"]


def test_subagent_tools_take_every_spelling() -> None:
    """run_subagent's and a profile's tools name the caller's filesystem toolkit by any spelling."""
    persona = inline_persona("You review.", ["coding", "file.read_file", "shell"], None, None)
    assert persona.tools == ("filesystem", "filesystem.read_file", "shell")


def test_openclaw_compat_brings_filesystem() -> None:
    """The OpenClaw preset expands to the filesystem toolkit."""
    assert "filesystem" in Config.expand_tool_names(["openclaw_compat"])


@pytest.mark.parametrize("match", ["save_file", "search_*", "replace_file_chunk", "list_files"])
def test_approval_rules_naming_only_retired_file_functions_fail(match: str) -> None:
    """A rule that would gate nothing after the rename fails, naming the replacement."""
    with pytest.raises(ValidationError, match="retired file tool functions .* is now "):
        ApprovalRuleConfig(match=match, action="require_approval")


@pytest.mark.parametrize("match", ["*_file", "write_file", "*", "grep"])
def test_approval_rules_matching_a_current_function_still_load(match: str) -> None:
    """A rule that also matches a current function keeps loading."""
    assert ApprovalRuleConfig(match=match, action="require_approval").match == match
