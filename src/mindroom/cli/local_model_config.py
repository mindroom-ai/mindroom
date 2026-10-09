"""Configure the desktop app's local model without changing other agent selections."""

from __future__ import annotations

from pathlib import Path  # noqa: TC003
from typing import TYPE_CHECKING
from urllib.parse import urlsplit

import typer
import yaml
from rich.markup import escape

from mindroom.atomic_file import atomic_write_bytes_at, existing_file_mode
from mindroom.cli.config import activate_cli_runtime, console
from mindroom.config.main import Config
from mindroom.path_confinement import open_directory_within_root, read_regular_file_within_root

if TYPE_CHECKING:
    from mindroom.constants import RuntimePaths


def _updated_source(original: bytes, runtime: RuntimePaths, model: str, base_url: str, api_key: str) -> bytes:
    data = yaml.safe_load(original)
    if not isinstance(data, dict) or not isinstance(data.get("models", {}), dict):
        msg = "Configuration must contain a models mapping."
        raise TypeError(msg)
    endpoint = urlsplit(base_url)
    if (
        not model.strip()
        or endpoint.scheme != "http"
        or endpoint.hostname != "127.0.0.1"
        or endpoint.port is None
        or endpoint.path != "/v1"
        or endpoint.username is not None
        or endpoint.query
        or endpoint.fragment
    ):
        msg = "Supply a model ID and a loopback URL such as http://127.0.0.1:11435/v1."
        raise ValueError(msg)
    data.setdefault("models", {})["default"] = {
        "provider": "llama_cpp",
        "id": model,
        "context_window": 8192,
        "extra_kwargs": {"base_url": base_url, "api_key": api_key},
    }
    Config.validate_with_runtime(data, runtime)
    return yaml.safe_dump(data, sort_keys=False, allow_unicode=True).encode("utf-8")


def _write_source(config_path: Path, original: bytes, source: bytes) -> None:
    with open_directory_within_root(config_path.parent) as directory_fd:
        if read_regular_file_within_root(directory_fd, config_path.name) != original:
            msg = "Configuration changed during setup. Retry with the latest file."
            raise ValueError(msg)
        mode = existing_file_mode(directory_fd, config_path.name)
        backup_name = f"{config_path.name}.before-local-model"
        # Retain the original config across subsequent model switches.
        if not (config_path.parent / backup_name).exists():
            atomic_write_bytes_at(directory_fd, backup_name, original, file_mode=mode)
        atomic_write_bytes_at(directory_fd, config_path.name, source, file_mode=mode)


def apply_local_model(*, model: str, base_url: str, api_key: str, path: Path | None) -> None:
    """Validate and atomically replace the default model, retaining an original backup."""
    runtime = activate_cli_runtime(path)
    try:
        original = runtime.config_path.read_bytes()
        source = _updated_source(original, runtime, model, base_url, api_key)
        _write_source(runtime.config_path, original, source)
    except (OSError, TypeError, ValueError, yaml.YAMLError) as exc:
        console.print(f"[red]Local model configuration failed:[/red] {escape(str(exc))}")
        raise typer.Exit(1) from None
    console.print("[green]Default model now runs locally.[/green] Explicitly selected agent models were kept.")
