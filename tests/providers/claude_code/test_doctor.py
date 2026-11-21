"""``mindroom doctor`` on the ``claude_code`` route, against a scripted Claude Code process."""
# ruff: noqa: D103

from __future__ import annotations

from typing import TYPE_CHECKING, Any

import pytest

from mindroom.cli.doctor import _check_single_provider
from mindroom.config.main import Config
from mindroom.tool_system import dependencies
from tests.providers.claude_code.support import fake_cli, left_behind, text_turn

if TYPE_CHECKING:
    from pathlib import Path

    from tests.providers.claude_code.support import FakeCli


def _check(cli: FakeCli, *names: str, **settings: Any) -> tuple[int, int, int]:  # noqa: ANN401
    model: dict[str, Any] = {
        "provider": "claude_code",
        "id": "claude-invented-1",
        "extra_kwargs": {"cli_path": str(cli.path)},
    }
    models = dict.fromkeys(names or ("default",), model)
    if settings:
        models["wrong"] = {**model, "extra_kwargs": settings}
    config = Config.model_validate({"models": models})
    return _check_single_provider("claude_code", config, cli.runtime_paths)


def test_doctor_passes_a_signed_in_claude_code_without_sending_it_a_prompt(
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    cli = fake_cli(tmp_path, {"turns": [text_turn("never")]})

    assert _check(cli, "default", "fast") == (1, 0, 0)

    assert "claude_code: Claude Code is signed in to a Claude subscription" in capsys.readouterr().out
    # Both models run one executable, which is started once, asked nothing, and closed.
    assert len(cli.events("spawn")) == len(cli.events("exit")) == 1
    assert cli.events("user") == []
    assert left_behind() == []


def test_doctor_fails_a_claude_code_that_is_not_signed_in(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    cli = fake_cli(tmp_path, {"account": {"tokenSource": "none", "apiProvider": "firstParty"}})

    assert _check(cli) == (0, 1, 0)

    assert "claude auth login" in capsys.readouterr().out.replace("\n", " ")


def test_doctor_fails_an_environment_that_replaces_the_claude_sign_in(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    monkeypatch.setenv("ANTHROPIC_BASE_URL", "https://gateway.invented.example")
    cli = fake_cli(tmp_path, {})

    assert _check(cli) == (0, 1, 0)

    assert "ANTHROPIC_BASE_URL" in capsys.readouterr().out
    assert cli.events("spawn") == []


def test_doctor_only_warns_when_claude_code_could_not_be_started(
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    cli = fake_cli(tmp_path, {})
    cli.path.write_text("#!/bin/sh\nexit 3\n")

    assert _check(cli) == (0, 0, 1)

    assert "could not check Claude Code" in capsys.readouterr().out


def test_doctor_only_warns_when_claude_code_rejects_a_question_about_its_state(
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    cli = fake_cli(tmp_path, {"status_error": "No such request"})

    assert _check(cli) == (0, 0, 1)

    assert "could not check Claude Code" in capsys.readouterr().out
    assert len(cli.events("exit")) == 1


def test_doctor_fails_a_model_with_a_wrong_setting_and_goes_on_to_the_next(
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    cli = fake_cli(tmp_path, {})

    assert _check(cli, "default", effort="ultra") == (1, 1, 0)

    assert "effort must be one of" in capsys.readouterr().out.replace("\n", " ")


def test_doctor_reports_a_missing_claude_agent_sdk_and_installs_nothing(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    installed: list[object] = []
    monkeypatch.delenv("MINDROOM_NO_AUTO_INSTALL_TOOLS", raising=False)
    monkeypatch.setattr(dependencies, "check_deps_installed", lambda _dependencies: False)
    monkeypatch.setattr(dependencies, "_install_optional_extras", lambda extras: installed.append(extras) or True)

    assert _check(fake_cli(tmp_path, {})) == (0, 1, 0)

    assert installed == []
    assert "mindroom[claude_agent]" in capsys.readouterr().out.replace("\n", " ")


def test_optional_dependency_check_only_never_enters_the_installer(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(dependencies, "check_deps_installed", lambda _dependencies: False)

    def install(*_args: object) -> bool:
        raise AssertionError

    monkeypatch.setattr(dependencies, "auto_install_optional_extra_for_import_retry", install)
    with pytest.raises(ImportError, match=r"mindroom\[claude_agent\]"):
        dependencies.ensure_optional_deps(
            ["claude-agent-sdk"],
            "claude_agent",
            fake_cli(tmp_path, {}).runtime_paths,
            auto_install=False,
        )
