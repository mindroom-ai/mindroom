"""Desktop local model setup preserves authored settings and failures preserve the file."""

from pathlib import Path

import pytest
import yaml
from click.testing import Result
from typer.testing import CliRunner

from mindroom.cli.main import app


def _configure(path: Path, *extra: str) -> Result:
    return CliRunner().invoke(
        app,
        [
            "config",
            "use-local-model",
            "--path",
            str(path),
            "--model",
            "local-test",
            "--base-url",
            "http://127.0.0.1:11435/v1",
            *extra,
        ],
    )


def test_local_model_keeps_agents_credentials_and_original_backup(tmp_path: Path) -> None:
    """Preserve authored data while applying only the local default."""
    path = tmp_path / "config.yaml"
    initial = {
        "agents": {"writer": {"display_name": "Writer", "role": "Write", "model": "remote"}},
        "models": {
            "default": {"provider": "openai", "id": "remote-test"},
            "remote": {"provider": "openai", "id": "remote-test"},
        },
    }
    original = yaml.safe_dump(initial).encode()
    path.write_bytes(original)
    path.chmod(0o600)
    env = tmp_path / ".env"
    env.write_text("OPENAI_API_KEY=existing-secret\n")
    result = _configure(path)
    assert result.exit_code == 0, result.output
    updated = yaml.safe_load(path.read_text())
    assert updated["agents"] == initial["agents"]
    assert updated["models"]["remote"] == initial["models"]["remote"]
    assert updated["models"]["default"]["provider"] == "llama_cpp"
    assert updated["models"]["default"]["extra_kwargs"]["base_url"] == "http://127.0.0.1:11435/v1"
    assert path.stat().st_mode & 0o777 == 0o600
    backup = path.with_name("config.yaml.before-local-model")
    assert backup.read_bytes() == original
    assert _configure(path, "--model", "another-local-test").exit_code == 0
    assert backup.read_bytes() == original
    assert env.read_text() == "OPENAI_API_KEY=existing-secret\n"


@pytest.mark.parametrize("source", ["models: !include models.yaml\n", "models: []\n", "agents: [invalid]\n"])
def test_invalid_or_composed_configuration_is_not_rewritten(tmp_path: Path, source: str) -> None:
    """An unsupported document never leaves a partial write or backup."""
    path = tmp_path / "config.yaml"
    path.write_text(source)
    result = _configure(path)
    assert result.exit_code == 1
    assert path.read_text() == source
    assert not path.with_name("config.yaml.before-local-model").exists()


@pytest.mark.parametrize("url", ["https://example.com/v1", "http://127.0.0.1:11435/v1?redirect=elsewhere"])
def test_local_model_requires_loopback_endpoint(tmp_path: Path, url: str) -> None:
    """Refuse external endpoints in the local setup command."""
    path = tmp_path / "config.yaml"
    path.write_text("models: {}\n")
    result = _configure(path, "--base-url", url)
    assert result.exit_code == 1
    assert path.read_text() == "models: {}\n"
