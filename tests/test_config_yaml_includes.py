"""Tests for Home-Assistant-style !include tags in the YAML config loader."""

from __future__ import annotations

import asyncio
import os
import time
from pathlib import Path
from typing import Any

import pytest
import yaml
from typer.testing import CliRunner

from mindroom import file_watcher
from mindroom.cli.main import app
from mindroom.config.main import load_config
from mindroom.config.yaml_includes import ConfigIncludeError, load_yaml_config_source
from mindroom.constants import resolve_runtime_paths

runner = CliRunner()


@pytest.fixture(autouse=True)
def _clear_runtime_path_env(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("MINDROOM_CONFIG_PATH", raising=False)
    monkeypatch.delenv("MINDROOM_STORAGE_PATH", raising=False)


def _write(path: Path, content: str) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(content, encoding="utf-8")
    return path


MONOLITH_CONFIG: dict[str, Any] = {
    "models": {"default": {"provider": "ollama", "id": "test-model"}},
    "agents": {
        "code": {
            "display_name": "Code Agent",
            "role": "Writes code",
            "tools": ["calculator"],
            "instructions": ["Line one of a long prompt.\nLine two of a long prompt."],
            "rooms": ["dev"],
        },
        "research": {
            "display_name": "Research Agent",
            "role": "Researches topics",
            "tools": ["calculator"],
            "rooms": ["dev"],
        },
    },
    "defaults": {"markdown": True},
}


def _write_split_config(config_dir: Path) -> Path:
    """Write MONOLITH_CONFIG split across include files; return the top-level path."""
    config_path = _write(
        config_dir / "config.yaml",
        "agents: !include_dir_merge_named agents/\nmodels: !include models.yaml\ndefaults:\n  markdown: true\n",
    )
    _write(config_dir / "models.yaml", yaml.dump(MONOLITH_CONFIG["models"]))
    _write(
        config_dir / "agents" / "code.yaml",
        "code:\n"
        "  display_name: Code Agent\n"
        "  role: Writes code\n"
        "  tools: !include _shared/tools.yaml\n"
        "  instructions:\n"
        "    - !include_text ../prompts/code.md\n"
        "  rooms: [dev]\n",
    )
    _write(config_dir / "agents" / "_shared" / "tools.yaml", "- calculator\n")
    _write(config_dir / "prompts" / "code.md", "Line one of a long prompt.\nLine two of a long prompt.\n")
    _write(
        config_dir / "agents" / "research.yaml",
        "research:\n  display_name: Research Agent\n  role: Researches topics\n  tools: [calculator]\n  rooms: [dev]\n",
    )
    return config_path


class TestIncludeTags:
    """Happy-path semantics of each include tag."""

    def test_include_resolves_nested_files_relative_to_including_file(self, tmp_path: Path) -> None:
        """!include nests recursively and resolves relative to the including file."""
        _write(tmp_path / "config.yaml", "outer: !include sub/inner.yaml\n")
        _write(tmp_path / "sub" / "inner.yaml", "deep: !include deeper.yaml\n")
        _write(tmp_path / "sub" / "deeper.yaml", "value: 42\n")

        data, files = load_yaml_config_source(tmp_path / "config.yaml")

        assert data == {"outer": {"deep": {"value": 42}}}
        assert files == frozenset(
            {
                (tmp_path / "config.yaml").resolve(),
                (tmp_path / "sub" / "inner.yaml").resolve(),
                (tmp_path / "sub" / "deeper.yaml").resolve(),
            },
        )

    def test_include_text_strips_exactly_one_trailing_newline(self, tmp_path: Path) -> None:
        """!include_text returns raw text with one trailing newline removed."""
        _write(tmp_path / "config.yaml", "prompt: !include_text prompt.md\n")
        _write(tmp_path / "prompt.md", "first line\nsecond line\n\n")

        data, files = load_yaml_config_source(tmp_path / "config.yaml")

        assert data == {"prompt": "first line\nsecond line\n"}
        assert (tmp_path / "prompt.md").resolve() in files

    def test_include_dir_list_orders_by_relative_path_and_recurses(self, tmp_path: Path) -> None:
        """!include_dir_list yields one item per file in lexicographic relative-path order."""
        _write(tmp_path / "config.yaml", "items: !include_dir_list items/\n")
        _write(tmp_path / "items" / "10.yaml", "ten\n")
        _write(tmp_path / "items" / "2.yml", "two\n")
        _write(tmp_path / "items" / "nested" / "b.yaml", "nested-b\n")
        _write(tmp_path / "items" / "notes.txt", "ignored\n")

        data, _files = load_yaml_config_source(tmp_path / "config.yaml")

        assert data == {"items": ["ten", "two", "nested-b"]}

    def test_dir_includes_skip_dot_and_underscore_names(self, tmp_path: Path) -> None:
        """Directory includes skip files and directories starting with '.' or '_'."""
        _write(tmp_path / "config.yaml", "named: !include_dir_named entries/\n")
        _write(tmp_path / "entries" / "kept.yaml", "1\n")
        _write(tmp_path / "entries" / ".hidden.yaml", "2\n")
        _write(tmp_path / "entries" / "_shared" / "snippet.yaml", "3\n")
        _write(tmp_path / "entries" / "_draft.yaml", "4\n")

        data, files = load_yaml_config_source(tmp_path / "config.yaml")

        assert data == {"named": {"kept": 1}}
        assert (tmp_path / "entries" / "_shared" / "snippet.yaml").resolve() not in files

    def test_include_dir_named_maps_filename_stems(self, tmp_path: Path) -> None:
        """!include_dir_named maps filename-without-extension to parsed content."""
        _write(tmp_path / "config.yaml", "named: !include_dir_named entries/\n")
        _write(tmp_path / "entries" / "alpha.yaml", "value: 1\n")
        _write(tmp_path / "entries" / "beta.yml", "value: 2\n")

        data, _files = load_yaml_config_source(tmp_path / "config.yaml")

        assert data == {"named": {"alpha": {"value": 1}, "beta": {"value": 2}}}

    def test_include_dir_merge_list_concatenates_lists(self, tmp_path: Path) -> None:
        """!include_dir_merge_list concatenates the lists from each file in order."""
        _write(tmp_path / "config.yaml", "merged: !include_dir_merge_list lists/\n")
        _write(tmp_path / "lists" / "a.yaml", "- 1\n- 2\n")
        _write(tmp_path / "lists" / "b.yaml", "- 3\n")

        data, _files = load_yaml_config_source(tmp_path / "config.yaml")

        assert data == {"merged": [1, 2, 3]}

    def test_include_dir_merge_named_merges_mappings(self, tmp_path: Path) -> None:
        """!include_dir_merge_named merges the mappings from each file."""
        _write(tmp_path / "config.yaml", "merged: !include_dir_merge_named maps/\n")
        _write(tmp_path / "maps" / "a.yaml", "one: 1\n")
        _write(tmp_path / "maps" / "b.yaml", "two: 2\n")

        data, _files = load_yaml_config_source(tmp_path / "config.yaml")

        assert data == {"merged": {"one": 1, "two": 2}}

    def test_empty_included_files_are_tolerated(self, tmp_path: Path) -> None:
        """Empty files resolve to None under !include and contribute nothing to dir tags."""
        _write(tmp_path / "config.yaml", "scalar: !include empty.yaml\nmerged: !include_dir_merge_named maps/\n")
        _write(tmp_path / "empty.yaml", "")
        _write(tmp_path / "maps" / "empty.yaml", "")
        _write(tmp_path / "maps" / "full.yaml", "key: value\n")

        data, files = load_yaml_config_source(tmp_path / "config.yaml")

        assert data == {"scalar": None, "merged": {"key": "value"}}
        assert (tmp_path / "empty.yaml").resolve() in files

    def test_empty_top_level_config_resolves_to_empty_dict(self, tmp_path: Path) -> None:
        """An empty top-level config parses to an empty dict, matching yaml.safe_load or {}."""
        _write(tmp_path / "config.yaml", "")

        data, files = load_yaml_config_source(tmp_path / "config.yaml")

        assert data == {}
        assert files == frozenset({(tmp_path / "config.yaml").resolve()})

    def test_diamond_include_reads_the_shared_file_once(
        self,
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """A file reachable via two include paths is read once so digest and content stay coherent."""
        _write(tmp_path / "config.yaml", "a: !include a.yaml\nb: !include b.yaml\n")
        _write(tmp_path / "a.yaml", "tools: !include shared/tools.yaml\n")
        _write(tmp_path / "b.yaml", "tools: !include shared/tools.yaml\n")
        _write(tmp_path / "shared" / "tools.yaml", "- calculator\n")

        read_names: list[str] = []
        original_read_bytes = Path.read_bytes

        def _counting_read_bytes(self: Path) -> bytes:
            read_names.append(self.name)
            return original_read_bytes(self)

        monkeypatch.setattr(Path, "read_bytes", _counting_read_bytes)

        data, files = load_yaml_config_source(tmp_path / "config.yaml")

        assert data == {"a": {"tools": ["calculator"]}, "b": {"tools": ["calculator"]}}
        assert (tmp_path / "shared" / "tools.yaml").resolve() in files
        assert read_names.count("tools.yaml") == 1


class TestIncludeErrors:
    """Error reporting for broken include trees."""

    def test_missing_file_error_names_including_file_and_line(self, tmp_path: Path) -> None:
        """A missing include target reports the including file and tag line."""
        _write(tmp_path / "config.yaml", "top: value\nbroken: !include missing.yaml\n")

        with pytest.raises(ConfigIncludeError, match=r"missing\.yaml' does not exist \(in .*config\.yaml, line 2\)"):
            load_yaml_config_source(tmp_path / "config.yaml")

    def test_missing_directory_error_names_including_file(self, tmp_path: Path) -> None:
        """A missing include directory reports the including file and tag line."""
        _write(tmp_path / "config.yaml", "broken: !include_dir_list nowhere/\n")

        with pytest.raises(ConfigIncludeError, match=r"'nowhere' does not exist \(in .*config\.yaml, line 1\)"):
            load_yaml_config_source(tmp_path / "config.yaml")

    def test_absolute_path_is_rejected(self, tmp_path: Path) -> None:
        """Absolute include paths are rejected outright."""
        _write(tmp_path / "config.yaml", "broken: !include /etc/passwd\n")

        with pytest.raises(ConfigIncludeError, match="does not allow absolute paths"):
            load_yaml_config_source(tmp_path / "config.yaml")

    def test_parent_escape_is_rejected(self, tmp_path: Path) -> None:
        """Includes resolving outside the config directory are rejected."""
        config_dir = tmp_path / "conf"
        _write(config_dir / "config.yaml", "broken: !include ../outside.yaml\n")
        _write(tmp_path / "outside.yaml", "value: 1\n")

        with pytest.raises(ConfigIncludeError, match="resolves outside the configuration directory"):
            load_yaml_config_source(config_dir / "config.yaml")

    def test_symlink_escape_is_rejected(self, tmp_path: Path) -> None:
        """A symlink pointing outside the config directory is rejected after resolution."""
        config_dir = tmp_path / "conf"
        _write(config_dir / "config.yaml", "broken: !include link.yaml\n")
        _write(tmp_path / "outside.yaml", "value: 1\n")
        (config_dir / "link.yaml").symlink_to(tmp_path / "outside.yaml")

        with pytest.raises(ConfigIncludeError, match="resolves outside the configuration directory"):
            load_yaml_config_source(config_dir / "config.yaml")

    def test_symlinked_directory_escape_is_rejected(self, tmp_path: Path) -> None:
        """A symlinked directory pointing outside the config directory is rejected before traversal."""
        config_dir = tmp_path / "conf"
        _write(config_dir / "config.yaml", "agents: !include_dir_merge_named agents/\n")
        (config_dir / "agents").mkdir()
        _write(tmp_path / "outside" / "external.yaml", "key: value\n")
        (config_dir / "agents" / "ext").symlink_to(tmp_path / "outside", target_is_directory=True)

        with pytest.raises(ConfigIncludeError, match="'ext' resolves outside the configuration directory"):
            load_yaml_config_source(config_dir / "config.yaml")

    def test_cycle_error_shows_the_include_chain(self, tmp_path: Path) -> None:
        """An include cycle reports the full chain of files."""
        _write(tmp_path / "config.yaml", "a: !include agents/b.yaml\n")
        _write(tmp_path / "agents" / "b.yaml", "b: !include ../config.yaml\n")

        with pytest.raises(ConfigIncludeError, match=r"config\.yaml -> agents/b\.yaml -> config\.yaml"):
            load_yaml_config_source(tmp_path / "config.yaml")

    def test_duplicate_merge_named_key_names_both_files(self, tmp_path: Path) -> None:
        """Duplicate keys across merge_named files raise, naming the key and both files."""
        _write(tmp_path / "config.yaml", "merged: !include_dir_merge_named maps/\n")
        _write(tmp_path / "maps" / "a.yaml", "shared: 1\n")
        _write(tmp_path / "maps" / "b.yaml", "shared: 2\n")

        with pytest.raises(ConfigIncludeError, match=r"duplicate key 'shared'.*maps/a\.yaml.*maps/b\.yaml"):
            load_yaml_config_source(tmp_path / "config.yaml")

    def test_duplicate_dir_named_stem_names_both_files(self, tmp_path: Path) -> None:
        """Duplicate filename stems across dir_named subdirectories raise instead of overwriting."""
        _write(tmp_path / "config.yaml", "named: !include_dir_named entries/\n")
        _write(tmp_path / "entries" / "one" / "same.yaml", "1\n")
        _write(tmp_path / "entries" / "two" / "same.yaml", "2\n")

        with pytest.raises(ConfigIncludeError, match=r"duplicate name 'same'.*one/same\.yaml.*two/same\.yaml"):
            load_yaml_config_source(tmp_path / "config.yaml")

    def test_merge_list_rejects_non_list_content(self, tmp_path: Path) -> None:
        """merge_list files containing non-list YAML raise a typed error naming the file."""
        _write(tmp_path / "config.yaml", "merged: !include_dir_merge_list lists/\n")
        _write(tmp_path / "lists" / "bad.yaml", "key: value\n")

        with pytest.raises(ConfigIncludeError, match=r"'lists/bad\.yaml' must contain a YAML list, got dict"):
            load_yaml_config_source(tmp_path / "config.yaml")

    def test_merge_named_rejects_non_mapping_content(self, tmp_path: Path) -> None:
        """merge_named files containing non-mapping YAML raise a typed error naming the file."""
        _write(tmp_path / "config.yaml", "merged: !include_dir_merge_named maps/\n")
        _write(tmp_path / "maps" / "bad.yaml", "- 1\n")

        with pytest.raises(ConfigIncludeError, match=r"'maps/bad\.yaml' must contain a YAML mapping, got list"):
            load_yaml_config_source(tmp_path / "config.yaml")

    def test_parse_error_inside_included_file_names_that_file(self, tmp_path: Path) -> None:
        """A YAML syntax error inside an included file reports the included file's mark."""
        _write(tmp_path / "config.yaml", "broken: !include bad.yaml\n")
        _write(tmp_path / "bad.yaml", "key: [unclosed\n")

        with pytest.raises(yaml.YAMLError, match=r"bad\.yaml"):
            load_yaml_config_source(tmp_path / "config.yaml")


class TestRoundTripEquivalence:
    """A split config must load identically to the monolith it came from."""

    def test_split_config_loads_to_same_dict_as_monolith(self, tmp_path: Path) -> None:
        """The include-based fixture resolves to exactly the monolith dict."""
        monolith_path = _write(tmp_path / "monolith" / "config.yaml", yaml.dump(MONOLITH_CONFIG))
        split_path = _write_split_config(tmp_path / "split")

        monolith_data, monolith_files = load_yaml_config_source(monolith_path)
        split_data, split_files = load_yaml_config_source(split_path)

        assert split_data == monolith_data
        assert monolith_files == frozenset({monolith_path.resolve()})
        assert len(split_files) == 6

    def test_load_config_validates_split_config_and_reports_source_files(self, tmp_path: Path) -> None:
        """load_config resolves includes into a validated Config carrying the file set."""
        split_path = _write_split_config(tmp_path)
        runtime_paths = resolve_runtime_paths(config_path=split_path)

        config = load_config(runtime_paths)

        assert set(config.agents) == {"code", "research"}
        assert config.agents["code"].instructions == ["Line one of a long prompt.\nLine two of a long prompt."]
        assert split_path.resolve() in config.source_files
        assert len(config.source_files) == 6

    def test_cli_validate_supports_include_configs(self, tmp_path: Path) -> None:
        """`mindroom config validate --path` accepts an include-based config."""
        split_path = _write_split_config(tmp_path)

        result = runner.invoke(app, ["config", "validate", "--path", str(split_path)])

        assert result.exit_code == 0, result.output
        assert "Configuration is valid" in result.output

    def test_cli_resolve_prints_merged_yaml_matching_monolith(self, tmp_path: Path) -> None:
        """`mindroom config resolve` output parses back to the monolith dict."""
        split_path = _write_split_config(tmp_path)

        result = runner.invoke(app, ["config", "resolve", "--path", str(split_path)])

        assert result.exit_code == 0, result.output
        assert yaml.safe_load(result.stdout) == MONOLITH_CONFIG

    def test_cli_resolve_reports_include_errors(self, tmp_path: Path) -> None:
        """`mindroom config resolve` surfaces include errors as validation output."""
        config_path = _write(tmp_path / "config.yaml", "broken: !include missing.yaml\n")

        result = runner.invoke(app, ["config", "resolve", "--path", str(config_path)])

        assert result.exit_code == 1
        assert "missing.yaml" in result.output


class TestWatchPaths:
    """The dynamic multi-file watcher backing include-aware hot reload."""

    @pytest.mark.asyncio
    async def test_change_to_any_watched_file_triggers_callback(
        self,
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """Editing an included file fires the watch callback."""
        monkeypatch.setattr(file_watcher, "_WATCH_SCAN_INTERVAL_SECONDS", 0.01)
        top = _write(tmp_path / "config.yaml", "a: 1\n")
        included = _write(tmp_path / "agents.yaml", "b: 2\n")
        changed = asyncio.Event()
        stop_event = asyncio.Event()

        async def on_change() -> None:
            changed.set()

        watch_task = asyncio.create_task(
            file_watcher.watch_paths(lambda: (top, included), on_change, stop_event),
        )
        await asyncio.sleep(0.05)
        future_time = time.time() + 5
        included.write_text("b: 3\n", encoding="utf-8")
        os.utime(included, (future_time, future_time))

        await asyncio.wait_for(changed.wait(), timeout=2)
        stop_event.set()
        await watch_task

    @pytest.mark.asyncio
    async def test_paths_entering_the_set_are_baselined_silently(
        self,
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """Growing the watched set (a reload re-derived includes) does not fire a change."""
        monkeypatch.setattr(file_watcher, "_WATCH_SCAN_INTERVAL_SECONDS", 0.01)
        top = _write(tmp_path / "config.yaml", "a: 1\n")
        extra = _write(tmp_path / "extra.yaml", "b: 2\n")
        watched: list[Path] = [top]
        callbacks: list[bool] = []
        stop_event = asyncio.Event()

        async def on_change() -> None:
            callbacks.append(True)

        watch_task = asyncio.create_task(file_watcher.watch_paths(lambda: tuple(watched), on_change, stop_event))
        await asyncio.sleep(0.05)
        watched.append(extra)
        await asyncio.sleep(0.1)

        stop_event.set()
        await watch_task
        assert callbacks == []
