"""Tests for vendored plugin install and update."""

from __future__ import annotations

import io
import json
import tarfile
from typing import TYPE_CHECKING

import pytest
from typer.testing import CliRunner

from mindroom import plugin_install
from mindroom.cli.main import app
from mindroom.plugin_install import (
    find_locked_plugin_dirs,
    install_plugin,
    parse_plugin_spec,
    update_plugin,
)

if TYPE_CHECKING:
    from collections.abc import Iterator
    from pathlib import Path

runner = CliRunner()

_VALID_PLUGIN_FILES = {
    "mindroom.plugin.json": json.dumps({"name": "compat-demo", "hooks_module": "hooks.py"}),
    "hooks.py": (
        "from mindroom.hooks import hook\n"
        "@hook(event='message:received', name='compat-demo-hook')\n"
        "async def compat_demo_hook(ctx):\n"
        "    del ctx\n"
    ),
}

_BROKEN_PLUGIN_FILES = {
    "mindroom.plugin.json": json.dumps({"name": "broken", "hooks_module": "hooks.py"}),
    "hooks.py": "raise RuntimeError('plugin exploded')\n",
}


def _archive_bytes(files: dict[str, str], root: str = "demo-plugin-abc123") -> bytes:
    buffer = io.BytesIO()
    with tarfile.open(fileobj=buffer, mode="w:gz") as archive:
        for relative_path, content in files.items():
            data = content.encode("utf-8")
            info = tarfile.TarInfo(f"{root}/{relative_path}")
            info.size = len(data)
            archive.addfile(info, io.BytesIO(data))
    return buffer.getvalue()


def _fake_github(
    monkeypatch: pytest.MonkeyPatch,
    *,
    commit: str = "a" * 40,
    files: dict[str, str] = _VALID_PLUGIN_FILES,
) -> None:
    monkeypatch.setattr(plugin_install, "_resolve_commit", lambda _repository, _ref: commit)
    monkeypatch.setattr(
        plugin_install,
        "_download_archive",
        lambda _repository, resolved: _archive_bytes(files, root=f"demo-plugin-{resolved[:7]}"),
    )


@pytest.mark.parametrize(
    ("spec", "expected"),
    [
        ("ping-hook-plugin", plugin_install._PluginSpec("mindroom-ai/ping-hook-plugin", "HEAD", "ping-hook-plugin")),
        ("acme/demo-plugin", plugin_install._PluginSpec("acme/demo-plugin", "HEAD", "demo-plugin")),
        ("acme/demo-plugin@v1.2.3", plugin_install._PluginSpec("acme/demo-plugin", "v1.2.3", "demo-plugin")),
    ],
)
def test_parse_plugin_spec_accepts_supported_forms(spec: str, expected: plugin_install._PluginSpec) -> None:
    """Bare names, owner/repo, and @ref forms should all resolve to one target."""
    assert parse_plugin_spec(spec) == expected


@pytest.mark.parametrize("spec", ["", "@v1", "demo@", "a/b/c", "/demo", "acme/", "..", "acme/..", "acme/.", "de\\mo"])
def test_parse_plugin_spec_rejects_invalid_forms(spec: str) -> None:
    """Malformed and directory-unsafe specs should fail before any network access."""
    with pytest.raises(ValueError, match="Invalid plugin spec"):
        parse_plugin_spec(spec)


class _FakeResponse:
    def __init__(self, text: str) -> None:
        self.status_code = 200
        self.text = text


def test_resolve_commit_quotes_ref_and_sends_token(monkeypatch: pytest.MonkeyPatch) -> None:
    """Refs should be URL-quoted and GITHUB_TOKEN should authenticate the request."""
    captured: dict[str, object] = {}

    def fake_get(url: str, **kwargs: object) -> _FakeResponse:
        captured["url"] = url
        captured["headers"] = kwargs["headers"]
        return _FakeResponse("a" * 40)

    monkeypatch.setenv("GITHUB_TOKEN", "test-token")
    monkeypatch.setattr(plugin_install.httpx, "get", fake_get)

    commit = plugin_install._resolve_commit("acme/demo-plugin", "feature/x#1")

    assert commit == "a" * 40
    assert captured["url"] == "https://api.github.com/repos/acme/demo-plugin/commits/feature/x%231"
    assert captured["headers"] == {"Accept": "application/vnd.github.sha", "Authorization": "Bearer test-token"}


def test_resolve_commit_rejects_non_sha_response(monkeypatch: pytest.MonkeyPatch) -> None:
    """Anything but a 40-hex commit SHA should never reach the lock file."""
    monkeypatch.delenv("GITHUB_TOKEN", raising=False)
    monkeypatch.setattr(plugin_install.httpx, "get", lambda _url, **_kwargs: _FakeResponse("<html>error</html>"))

    with pytest.raises(ValueError, match="unexpected commit"):
        plugin_install._resolve_commit("acme/demo-plugin", "HEAD")


def test_download_archive_enforces_size_cap(monkeypatch: pytest.MonkeyPatch) -> None:
    """Oversized archives should abort the download instead of exhausting memory."""

    class _FakeStream:
        status_code = 200

        def __enter__(self) -> _FakeStream:
            return self

        def __exit__(self, *args: object) -> None:
            return None

        def iter_bytes(self) -> Iterator[bytes]:
            yield b"x" * 8
            yield b"x" * 8

    monkeypatch.setattr(plugin_install, "_MAX_ARCHIVE_BYTES", 10)
    monkeypatch.setattr(plugin_install.httpx, "stream", lambda _method, _url, **_kwargs: _FakeStream())

    with pytest.raises(ValueError, match="exceeds"):
        plugin_install._download_archive("acme/demo-plugin", "a" * 40)


def test_install_plugin_vendors_validated_archive(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """Install should unpack, strictly validate, and record exact provenance."""
    _fake_github(monkeypatch)

    result = install_plugin(parse_plugin_spec("acme/demo-plugin@main"), tmp_path / "plugins")

    assert result.name == "compat-demo"
    assert result.directory == tmp_path / "plugins" / "demo-plugin"
    assert (result.directory / "hooks.py").is_file()
    assert not result.has_pyproject
    lock = plugin_install._read_plugin_lock(result.directory)
    assert lock.repository == "acme/demo-plugin"
    assert lock.requested_ref == "main"
    assert lock.commit == "a" * 40
    assert lock.installed_at
    assert [path.name for path in (tmp_path / "plugins").iterdir()] == ["demo-plugin"]


def test_install_plugin_reports_pyproject_dependencies(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """Packaged plugins should surface that their dependencies are not installed."""
    _fake_github(monkeypatch, files={**_VALID_PLUGIN_FILES, "pyproject.toml": "[project]\nname='demo'\n"})

    result = install_plugin(parse_plugin_spec("acme/demo-plugin"), tmp_path / "plugins")

    assert result.has_pyproject


def test_install_plugin_refuses_existing_directory(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """Install should never overwrite an existing plugin directory."""
    _fake_github(monkeypatch)
    destination = tmp_path / "plugins" / "demo-plugin"
    destination.mkdir(parents=True)

    with pytest.raises(ValueError, match="already exists"):
        install_plugin(parse_plugin_spec("acme/demo-plugin"), tmp_path / "plugins")


def test_install_plugin_rejects_broken_plugin_and_cleans_staging(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A failed strict check should leave the plugins directory untouched."""
    _fake_github(monkeypatch, files=_BROKEN_PLUGIN_FILES)

    with pytest.raises(ValueError, match="plugin exploded"):
        install_plugin(parse_plugin_spec("acme/demo-plugin"), tmp_path / "plugins")

    assert list((tmp_path / "plugins").iterdir()) == []


def test_update_plugin_is_noop_at_same_commit(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """Updates at the pinned commit should not download or touch plugin files."""
    _fake_github(monkeypatch)
    installed = install_plugin(parse_plugin_spec("acme/demo-plugin"), tmp_path / "plugins")
    marker = installed.directory / "local-marker"
    marker.write_text("untouched", encoding="utf-8")

    update = update_plugin(installed.directory)

    assert update.installed is None
    assert update.previous_commit == "a" * 40
    assert marker.read_text(encoding="utf-8") == "untouched"


def test_update_plugin_swaps_to_new_commit(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """A new upstream commit should atomically replace the vendored directory."""
    _fake_github(monkeypatch)
    installed = install_plugin(parse_plugin_spec("acme/demo-plugin"), tmp_path / "plugins")
    (installed.directory / "stale-file").write_text("old", encoding="utf-8")
    new_files = {**_VALID_PLUGIN_FILES, "new-file": "new"}
    _fake_github(monkeypatch, commit="b" * 40, files=new_files)

    update = update_plugin(installed.directory)

    assert update.previous_commit == "a" * 40
    assert update.installed is not None
    assert update.installed.lock.commit == "b" * 40
    assert (installed.directory / "new-file").is_file()
    assert not (installed.directory / "stale-file").exists()
    assert [path.name for path in (tmp_path / "plugins").iterdir()] == ["demo-plugin"]


def test_update_plugin_keeps_previous_version_on_failed_check(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A broken upstream revision should leave the installed version fully intact."""
    _fake_github(monkeypatch)
    installed = install_plugin(parse_plugin_spec("acme/demo-plugin"), tmp_path / "plugins")
    _fake_github(monkeypatch, commit="b" * 40, files=_BROKEN_PLUGIN_FILES)

    with pytest.raises(ValueError, match="plugin exploded"):
        update_plugin(installed.directory)

    assert (installed.directory / "hooks.py").is_file()
    assert plugin_install._read_plugin_lock(installed.directory).commit == "a" * 40
    assert [path.name for path in (tmp_path / "plugins").iterdir()] == ["demo-plugin"]


def test_update_plugin_repins_reference(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """--ref should repin the recorded reference even without new commits."""
    _fake_github(monkeypatch)
    installed = install_plugin(parse_plugin_spec("acme/demo-plugin"), tmp_path / "plugins")

    update = update_plugin(installed.directory, ref="v1.0.0")

    assert update.installed is None
    assert plugin_install._read_plugin_lock(installed.directory).requested_ref == "v1.0.0"


def test_read_plugin_lock_requires_lock_file(tmp_path: Path) -> None:
    """Directories without provenance should be rejected with guidance."""
    with pytest.raises(ValueError, match="Not a vendored plugin"):
        plugin_install._read_plugin_lock(tmp_path)


def test_read_plugin_lock_ignores_unknown_fields_and_rejects_missing(tmp_path: Path) -> None:
    """Locks from other mindroom versions should load; corrupt locks should not."""
    lock_path = tmp_path / plugin_install._PLUGIN_LOCK_FILENAME
    lock_path.write_text(
        json.dumps(
            {
                "repository": "acme/demo-plugin",
                "requested_ref": "HEAD",
                "commit": "a" * 40,
                "installed_at": "2026-07-16T00:00:00+00:00",
                "future_field": "ignored",
            },
        ),
        encoding="utf-8",
    )

    assert plugin_install._read_plugin_lock(tmp_path).repository == "acme/demo-plugin"

    lock_path.write_text(json.dumps({"repository": "acme/demo-plugin"}), encoding="utf-8")
    with pytest.raises(ValueError, match="missing commit"):
        plugin_install._read_plugin_lock(tmp_path)


def test_find_locked_plugin_dirs_skips_unmanaged_entries(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """Only vendored plugin directories should participate in --all updates."""
    _fake_github(monkeypatch)
    installed = install_plugin(parse_plugin_spec("acme/demo-plugin"), tmp_path / "plugins")
    (tmp_path / "plugins" / "manual-clone").mkdir()
    (tmp_path / "plugins" / ".previous-demo-plugin").mkdir()

    assert find_locked_plugin_dirs(tmp_path / "plugins") == (installed.directory,)


def test_plugins_install_cli_reports_result_and_config_snippet(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """CLI install should report provenance and print the config entry to add."""
    _fake_github(monkeypatch)

    result = runner.invoke(
        app,
        ["plugins", "install", "acme/demo-plugin", "--plugins-dir", str(tmp_path / "plugins")],
    )

    assert result.exit_code == 0
    assert "Installed plugin: compat-demo" in result.stdout
    assert f"acme/demo-plugin@{'a' * 12}" in result.stdout
    assert "- path:" in result.stdout


def test_plugins_install_cli_honors_config_path_and_prints_relative_snippet(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """--path should target that config's plugins directory and yield a portable snippet."""
    _fake_github(monkeypatch)
    config_path = tmp_path / "config.yaml"
    config_path.write_text("agents: {}\n", encoding="utf-8")

    result = runner.invoke(app, ["plugins", "install", "acme/demo-plugin", "--path", str(config_path)])

    assert result.exit_code == 0
    assert (tmp_path / "plugins" / "demo-plugin" / "hooks.py").is_file()
    assert "- path: plugins/demo-plugin" in result.stdout


def test_plugins_install_cli_reports_failure_without_traceback(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """CLI install should surface plugin errors without a Typer traceback."""
    _fake_github(monkeypatch, files=_BROKEN_PLUGIN_FILES)

    result = runner.invoke(
        app,
        ["plugins", "install", "acme/demo-plugin", "--plugins-dir", str(tmp_path / "plugins")],
    )

    assert result.exit_code == 1
    assert "Plugin install failed:" in result.stdout
    assert "plugin exploded" in result.stdout
    assert "Traceback (most recent call last)" not in result.output


def test_plugins_update_cli_updates_all_vendored_plugins(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """CLI --all should update stale plugins and report already-current ones."""
    plugins_dir = tmp_path / "plugins"
    _fake_github(monkeypatch)
    install_plugin(parse_plugin_spec("acme/demo-plugin"), plugins_dir)
    _fake_github(monkeypatch, commit="b" * 40)

    result = runner.invoke(app, ["plugins", "update", "--all", "--plugins-dir", str(plugins_dir)])

    assert result.exit_code == 0
    assert f"{'a' * 12} -> {'b' * 12}" in result.stdout
    stale = runner.invoke(app, ["plugins", "update", "--all", "--plugins-dir", str(plugins_dir)])
    assert stale.exit_code == 0
    assert "Already up to date: demo-plugin" in stale.stdout


def test_plugins_update_cli_requires_exactly_one_target(tmp_path: Path) -> None:
    """CLI update should reject ambiguous target selection."""
    result = runner.invoke(app, ["plugins", "update", "--plugins-dir", str(tmp_path)])

    assert result.exit_code == 2
    assert "exactly one of NAME or --all" in result.output


@pytest.mark.parametrize("name", ["../outside", "..", "a/b", "a\\b", ""])
def test_plugins_update_cli_rejects_traversal_names(tmp_path: Path, name: str) -> None:
    """Update targets must stay inside the plugins directory."""
    result = runner.invoke(app, ["plugins", "update", name, "--plugins-dir", str(tmp_path)])

    assert result.exit_code == 2
    assert "Invalid plugin name" in result.output
