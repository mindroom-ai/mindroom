"""Bundle CLI reports filesystem activation separately from runtime application."""

from __future__ import annotations

import hashlib
import json
import shutil
from typing import TYPE_CHECKING

import httpx
import pytest
import typer
from typer.testing import CliRunner

from mindroom.cli.config import activate_cli_runtime
from mindroom.cli.config_bundle import initialize_runtime_bundle
from mindroom.cli.main import app
from mindroom.config.main import load_config
from mindroom.config_bundle import install_config_bundle

if TYPE_CHECKING:
    from collections.abc import Callable
    from pathlib import Path

    from mindroom.constants import RuntimePaths

runner = CliRunner()


@pytest.mark.parametrize("override", [None, "process", "cli"])
def test_bootstrap_validates_against_the_startup_storage(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    override: str | None,
) -> None:
    """Staged environment state controls validation unless process or CLI explicitly overrides it."""
    monkeypatch.delenv("MINDROOM_STORAGE_PATH", raising=False)
    source = tmp_path / "source"
    source.mkdir()
    storage = tmp_path / "bundle-state"
    storage.mkdir()
    (storage / "matrix_state.yaml").write_text("accounts:\n  agent_router:\n    username: duplicate\n")
    (source / ".env").write_text(f"MINDROOM_STORAGE_PATH={storage}\n")
    (source / "config.yaml").write_text("agents: {}\nmindroom_user:\n  username: duplicate\n")
    target = tmp_path / "active"
    explicit_storage = tmp_path / "explicit-state"
    if override == "process":
        monkeypatch.setenv("MINDROOM_STORAGE_PATH", str(explicit_storage))
    cli_storage = explicit_storage if override == "cli" else None
    if override is None:
        with pytest.raises(typer.Exit) as exc:
            initialize_runtime_bundle(source, target / "config.yaml", cli_storage)
        assert exc.value.exit_code == 2
        assert not target.exists()
    else:
        initialize_runtime_bundle(source, target / "config.yaml", cli_storage)
        runtime = activate_cli_runtime(target / "config.yaml", storage_path=cli_storage)
        assert runtime.storage_root == explicit_storage
        assert load_config(runtime).mindroom_user.username == "duplicate"


def test_install_receipt_matches_existing_fingerprint_command(tmp_path: Path) -> None:
    """JSON must be clean and reusable by the existing reload confirmation CLI."""
    source = tmp_path / "source"
    source.mkdir()
    (source / "config.yaml").write_text("agents: {}\n")
    target = tmp_path / "active"
    result = runner.invoke(app, ["config", "install-bundle", str(source), "--target", str(target), "--json"])
    assert result.exit_code == 0, result.output
    receipt = json.loads(result.stdout)
    assert receipt["status"] == "installed"
    fingerprint = runner.invoke(app, ["config", "fingerprint", "--path", str(target / "config.yaml")])
    assert receipt["fingerprint"] == fingerprint.stdout.strip()
    assert receipt["config_path"] == str(target / "config.yaml")
    assert len(receipt["digest"]) == 64


def test_invalid_bundle_exits_nonzero_and_never_publishes(tmp_path: Path) -> None:
    """Native YAML failures must become a clear CLI error, preserving active files."""
    source = tmp_path / "source"
    source.mkdir()
    (source / "config.yaml").write_text("agents: !include absent.yaml\n")
    target = tmp_path / "active"
    result = runner.invoke(app, ["config", "install-bundle", str(source), "--target", str(target), "--json"])
    assert result.exit_code == 2
    assert json.loads(result.stdout)["status"] == "failed"
    assert not target.exists()


def test_invalid_options_return_json_error(tmp_path: Path) -> None:
    """Conflicting modes must be a user-facing error instead of an uncaught exception."""
    result = runner.invoke(
        app,
        [
            "config",
            "install-bundle",
            str(tmp_path / "source"),
            "--target",
            str(tmp_path / "active"),
            "--initialize-only",
            "--force",
            "--json",
        ],
    )
    assert result.exit_code == 2
    assert json.loads(result.stdout)["status"] == "failed"


def test_source_digest_guard_rejects_changed_input(tmp_path: Path) -> None:
    """A supplied source guard must reach native validation before publication."""
    source = tmp_path / "source"
    source.mkdir()
    (source / "config.yaml").write_text("agents: {}\n")
    target = tmp_path / "active"
    result = runner.invoke(
        app,
        [
            "config",
            "install-bundle",
            str(source),
            "--target",
            str(target),
            "--expected-digest",
            "0" * 64,
            "--json",
        ],
    )
    assert result.exit_code == 2
    assert "digest" in json.loads(result.stdout)["detail"]
    assert not target.exists()


def test_receipt_output_failure_can_be_retried_without_rotating_previous(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Broken output after publication must leave a retryable complete installation."""
    source = tmp_path / "source"
    source.mkdir()
    (source / "config.yaml").write_text("agents: {}\n")
    target = tmp_path / "active"
    args = ["config", "install-bundle", str(source), "--target", str(target), "--json"]

    def fail_output(*_args: object, **_kwargs: object) -> None:
        msg = "receipt output failed"
        raise BrokenPipeError(msg)

    with monkeypatch.context() as patch:
        patch.setattr(typer, "echo", fail_output)
        assert runner.invoke(app, args).exit_code != 0
    assert (target / "config.yaml").read_text() == "agents: {}\n"
    result = runner.invoke(app, args)
    assert result.exit_code == 0, result.output
    assert json.loads(result.stdout)["status"] == "unchanged"
    assert not (tmp_path / "active.previous").exists()


def test_run_bootstraps_once_and_loads_installed_environment(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """Runtime bootstrap must validate before startup and load the newly installed .env."""
    source = tmp_path / "source"
    source.mkdir()
    (source / "custom.yaml").write_text("agents: {}\n")
    (source / ".env").write_text("BUNDLE_VALUE=installed\n")
    target = tmp_path / "active"
    observed = []

    async def start_runtime(*, runtime_paths: RuntimePaths, **_kwargs: object) -> None:
        observed.append(runtime_paths.env_value("BUNDLE_VALUE"))

    monkeypatch.setattr("mindroom.orchestrator.main", start_runtime)
    monkeypatch.setattr("mindroom.cli.main.check_env_keys", lambda *_args, **_kwargs: None)
    args = [
        "run",
        "--no-api",
        "--config",
        str(target / "custom.yaml"),
        "--storage-path",
        str(tmp_path / "state"),
        "--bootstrap-config-bundle",
        str(source),
    ]
    result = runner.invoke(app, args)
    assert result.exit_code == 0, result.output
    assert observed == ["installed"]
    (target / ".env").write_text("BUNDLE_VALUE=authored\n")
    (source / "custom.yaml").write_text("bad: [")
    result = runner.invoke(app, args)
    assert result.exit_code == 0, result.output
    assert observed == ["installed", "authored"]
    assert not (tmp_path / "active.previous").exists()


def test_run_invalid_bootstrap_never_starts_runtime(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """An invalid initial candidate cannot become active or start the service."""
    source = tmp_path / "source"
    source.mkdir()
    (source / "config.yaml").write_text("agents: !include missing.yaml\n")
    monkeypatch.setattr("mindroom.orchestrator.main", lambda **_kwargs: pytest.fail("Runtime must not start"))
    target = tmp_path / "active"
    result = runner.invoke(
        app,
        ["run", "--no-api", "--config", str(target / "config.yaml"), "--bootstrap-config-bundle", str(source)],
    )
    assert result.exit_code == 2, result.output
    assert not target.exists()


def test_run_revision_requires_source(tmp_path: Path) -> None:
    """Startup cannot declare a revision without a candidate source."""
    result = runner.invoke(
        app,
        [
            "run",
            "--no-api",
            "--config",
            str(tmp_path / "active/config.yaml"),
            "--bootstrap-config-bundle-revision",
            "one",
        ],
    )
    assert result.exit_code == 2
    assert "--bootstrap-config-bundle" in result.output


def test_run_revision_replaces_changed_bundle_before_runtime(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """A changed declared revision installs before runtime path resolution."""
    source = tmp_path / "source"
    source.mkdir()
    (source / "config.yaml").write_text("agents: {}\n")
    target = tmp_path / "active"
    observed: list[str] = []

    async def start_runtime(*, runtime_paths: RuntimePaths, **_kwargs: object) -> None:
        observed.append(runtime_paths.env_value("BUNDLE_VALUE") or "missing")

    monkeypatch.setattr("mindroom.orchestrator.main", start_runtime)
    monkeypatch.setattr("mindroom.cli.main.check_env_keys", lambda *_args, **_kwargs: None)
    args = [
        "run",
        "--no-api",
        "--config",
        str(target / "config.yaml"),
        "--bootstrap-config-bundle",
        str(source),
        "--bootstrap-config-bundle-revision",
        "one",
    ]
    assert runner.invoke(app, args).exit_code == 0
    (source / ".env").write_text("BUNDLE_VALUE=updated\n")
    assert runner.invoke(app, [*args[:-1], "two"]).exit_code == 0
    assert observed == ["missing", "updated"]
    assert json.loads((target / ".mindroom-bundle.json").read_text())["revision"] == "two"


def test_install_bundle_cli_revision_is_stored(tmp_path: Path) -> None:
    """The native CLI passes an explicit revision to the installer."""
    source = tmp_path / "source"
    source.mkdir()
    (source / "config.yaml").write_text("agents: {}\n")
    target = tmp_path / "active"
    result = runner.invoke(
        app,
        ["config", "install-bundle", str(source), "--target", str(target), "--revision", "one", "--json"],
    )
    assert result.exit_code == 0, result.output
    assert json.loads((target / ".mindroom-bundle.json").read_text())["revision"] == "one"


def test_classify_change_reports_status_paths_and_exit_code(tmp_path: Path) -> None:
    """Exit codes separate YAML/include source edits, other changes, and unclassifiable trees."""
    old = tmp_path / "old"
    old.mkdir()
    (old / "config.yaml").write_text("agents: {}\n")
    new = tmp_path / "new"
    shutil.copytree(old, new)
    (new / "config.yaml").write_text("agents: {}\n# edited\n")
    args = ["config", "classify-change", str(old), str(new), "--json"]
    result = runner.invoke(app, args)
    assert result.exit_code == 0, result.output
    assert json.loads(result.stdout) == {"status": "source_only", "sources": ["config.yaml"], "other": []}
    (new / ".env").write_text("SECRET_VALUE=redacted\n")
    result = runner.invoke(app, args)
    assert result.exit_code == 1, result.output
    assert json.loads(result.stdout) == {"status": "non_source", "sources": ["config.yaml"], "other": [".env"]}
    assert "redacted" not in result.output
    result = runner.invoke(app, [*args, "--config", "missing.yaml"])
    assert result.exit_code == 2, result.output
    assert json.loads(result.stdout)["status"] == "failed"


def test_install_bundle_source_only_refuses_non_source_change(tmp_path: Path) -> None:
    """The CLI flag reaches the installer and leaves the active tree untouched."""
    source = tmp_path / "source"
    source.mkdir()
    (source / "config.yaml").write_text("agents: {}\n")
    target = tmp_path / "active"
    args = ["config", "install-bundle", str(source), "--target", str(target), "--json"]
    assert runner.invoke(app, args).exit_code == 0
    (source / "notes.txt").write_text("not a config source\n")
    result = runner.invoke(app, [*args, "--source-only"])
    assert result.exit_code == 2, result.output
    assert "notes.txt" in json.loads(result.stdout)["detail"]
    assert not (target / "notes.txt").exists()


@pytest.mark.parametrize("value", ["!!bool maybe", "!!timestamp later", '!!int ""'])
@pytest.mark.parametrize("json_output", [True, False])
def test_classify_change_reports_unconstructible_yaml_as_failure(
    tmp_path: Path,
    value: str,
    json_output: bool,
) -> None:
    """Tagged scalars the safe constructor cannot build exit 2 instead of crashing with the non-source exit code."""
    old = tmp_path / "old"
    old.mkdir()
    (old / "config.yaml").write_text("agents: {}\n")
    new = tmp_path / "new"
    new.mkdir()
    (new / "config.yaml").write_text(f"agents: {{}}\nunknown: {value}\n")
    args = ["config", "classify-change", str(old), str(new)]
    result = runner.invoke(app, [*args, "--json"] if json_output else args)
    assert result.exit_code == 2, result.output
    if json_output:
        assert json.loads(result.stdout)["status"] == "failed"
    else:
        assert "Change classification failed" in result.output
    assert value.split()[1] not in result.output


def _active_tree(tmp_path: Path, source_text: str = "agents: {}\n") -> tuple[Path, Path, dict[str, object]]:
    """Install a single-file tree whose runtime fingerprint is the plain config SHA-256."""
    source = tmp_path / "source"
    source.mkdir()
    (source / "config.yaml").write_text(source_text)
    target = tmp_path / "active"
    result = runner.invoke(app, ["config", "install-bundle", str(source), "--target", str(target), "--json"])
    assert result.exit_code == 0, result.output
    return source, target, json.loads(result.stdout)


def _serve_runtime(monkeypatch: pytest.MonkeyPatch, respond: Callable[[int], dict[str, object] | None]) -> None:
    """Answer reload-status requests in order; None simulates a lost connection."""
    monkeypatch.setenv("MINDROOM_API_KEY", "operator-key")
    calls = []

    def get(url: str, **_kwargs: object) -> httpx.Response:
        assert url.endswith("/api/config/reload-status")
        calls.append(url)
        payload = respond(len(calls))
        if payload is None:
            msg = "connection lost"
            raise httpx.ConnectError(msg)
        return httpx.Response(503 if payload["status"] == "unavailable" else 200, json=payload)

    monkeypatch.setattr(httpx, "get", get)


def _fingerprint_of(target: Path) -> str:
    return hashlib.sha256((target / "config.yaml").read_bytes()).hexdigest()


def _apply(source: Path, target: Path, *args: str) -> tuple[int, dict[str, object]]:
    result = runner.invoke(
        app,
        ["config", "apply-bundle", str(source), "--target", str(target), "--wait", "0", "--json", *args],
    )
    return result.exit_code, json.loads(result.stdout)


@pytest.mark.parametrize(
    ("runtime", "flags", "status", "exit_code", "active"),
    [
        ("applies", ["--rollback-on-failure"], "applied", 0, "candidate"),
        ("rejects", ["--rollback-on-failure"], "rolled_back", 3, "previous"),
        ("rejects", [], "failed", 2, "candidate"),
        ("rejects_both", ["--rollback-on-failure"], "unconfirmed", 4, "previous"),
        ("keeps_previous", ["--rollback-on-failure"], "pending", 1, "candidate"),
        ("needs_restart", ["--rollback-on-failure"], "restart_required", 5, "candidate"),
        ("disconnects", ["--rollback-on-failure"], "unconfirmed", 4, "candidate"),
    ],
)
def test_apply_bundle_settles_one_receipt_and_rolls_back_only_confirmed_failure(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    runtime: str,
    flags: list[str],
    status: str,
    exit_code: int,
    active: str,
) -> None:
    """Only an exact runtime failure of a changed tree restores the digest-pinned previous tree."""
    source, target, first = _active_tree(tmp_path)
    previous_fingerprint = _fingerprint_of(target)
    (source / "config.yaml").write_text("agents: {}\n# candidate\n")

    def respond(call: int) -> dict[str, object] | None:
        current = _fingerprint_of(target)
        if call == 1 or runtime == "keeps_previous":
            return {"status": "applied", "fingerprint": previous_fingerprint}
        if runtime == "disconnects":
            return None
        if runtime == "rejects_both" or (runtime == "rejects" and current != previous_fingerprint):
            return {"status": "failed", "fingerprint": current}
        return {"status": "restart_required" if runtime == "needs_restart" else "applied", "fingerprint": current}

    _serve_runtime(monkeypatch, respond)
    code, receipt = _apply(source, target, *flags)
    assert (code, receipt["status"]) == (exit_code, status), receipt
    assert receipt["install"]["status"] == "installed"
    assert receipt["install"]["previous_digest"] == first["digest"]
    expected = "agents: {}\n# candidate\n" if active == "candidate" else "agents: {}\n"
    assert (target / "config.yaml").read_text() == expected
    if active == "previous":
        assert receipt["rollback"]["digest"] == first["digest"]
        assert receipt["rollback_runtime_status"] == ("applied" if status == "rolled_back" else "failed")
        assert (tmp_path / "active.previous/config.yaml").read_text() == "agents: {}\n# candidate\n"
    else:
        assert receipt["rollback"] is None


@pytest.mark.parametrize("problem", ["missing_key", "runtime_unavailable", "non_source"])
def test_apply_bundle_installs_nothing_unless_it_can_confirm_a_source_change(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    problem: str,
) -> None:
    """Unreadable runtime results and changes a reload cannot apply are refused before the active tree changes."""
    source, target, _first = _active_tree(tmp_path)
    fingerprint = _fingerprint_of(target)
    status = "unavailable" if problem == "runtime_unavailable" else "applied"
    _serve_runtime(monkeypatch, lambda _call: {"status": status, "fingerprint": fingerprint})
    if problem == "missing_key":
        monkeypatch.delenv("MINDROOM_API_KEY")
    (source / "config.yaml").write_text("agents: {}\n# candidate\n")
    if problem == "non_source":
        (source / ".env").write_text("CHANGED=1\n")
    code, receipt = _apply(source, target, "--rollback-on-failure")
    assert (code, receipt["status"], receipt["install"]) == (2, "failed", None)
    assert (target / "config.yaml").read_text() == "agents: {}\n"


@pytest.mark.parametrize("case", ["unchanged", "stale_failure", "stale_pending", "stale_unknown", "previous_changed"])
def test_apply_bundle_never_rolls_back_an_unproven_failure(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    case: str,
) -> None:
    """Unchanged trees, reloads that began before install, and altered previous trees are left in place."""
    source, target, _first = _active_tree(tmp_path)
    previous_fingerprint = _fingerprint_of(target)
    if case != "unchanged":
        (source / "config.yaml").write_text("agents: {}\n# candidate\n")
    candidate = hashlib.sha256((source / "config.yaml").read_bytes()).hexdigest()

    def respond(call: int) -> dict[str, object]:
        if case in {"stale_pending", "stale_unknown"} and call == 1:
            return {"status": "pending", "fingerprint": candidate if case == "stale_pending" else None}
        if case == "previous_changed":
            if call == 1:
                return {"status": "applied", "fingerprint": previous_fingerprint}
            # A legitimate install rotates a tree this apply never moved aside into TARGET.previous.
            (source / "config.yaml").write_text("agents: {}\n# other\n")
            install_config_bundle(source, target, process_env={})
            (source / "config.yaml").write_text("agents: {}\n# candidate\n")
            install_config_bundle(source, target, process_env={})
        return {"status": "failed", "fingerprint": candidate}

    _serve_runtime(monkeypatch, respond)
    code, receipt = _apply(source, target, "--rollback-on-failure")
    expected = (2, "failed") if case == "unchanged" else (4, "unconfirmed")
    assert (code, receipt["status"]) == expected, receipt
    assert receipt["rollback"] is None
    assert ("restoring the previous tree failed" in receipt["detail"]) == (case == "previous_changed")
    assert _fingerprint_of(target) == candidate
