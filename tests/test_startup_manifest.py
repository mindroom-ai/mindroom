"""Startup manifest serialization tests."""

from __future__ import annotations

import json
from typing import TYPE_CHECKING

from mindroom.constants import resolve_runtime_paths, write_startup_manifest

if TYPE_CHECKING:
    from pathlib import Path


def test_startup_manifest_excludes_oauth_client_secrets(tmp_path: Path) -> None:
    """OAuth client secrets must not be persisted in worker-readable manifests."""
    config_path = tmp_path / "config.yaml"
    config_path.write_text("agents: {}\n", encoding="utf-8")
    config_path.with_name(".env").write_text(
        "MINDROOM_OAUTH_GOOGLE_DRIVE_CLIENT_SECRET=env-file-secret\n",
        encoding="utf-8",
    )
    runtime_paths = resolve_runtime_paths(
        config_path=config_path,
        storage_path=tmp_path / "storage",
        process_env={
            "GOOGLE_CLIENT_ID": "client-id",
            "GOOGLE_CLIENT_SECRET": "process-secret",
            "MINDROOM_OAUTH_GOOGLE_GMAIL_CLIENT_SECRET": "process-gmail-secret",
        },
    )

    manifest_path = write_startup_manifest(tmp_path / "manifest-root", runtime_paths)
    manifest_text = manifest_path.read_text(encoding="utf-8")
    manifest = json.loads(manifest_text)
    serialized_runtime = manifest["runtime_paths"]

    assert "process-secret" not in manifest_text
    assert "process-gmail-secret" not in manifest_text
    assert "env-file-secret" not in manifest_text
    assert serialized_runtime["process_env"]["GOOGLE_CLIENT_ID"] == "client-id"
    assert "GOOGLE_CLIENT_SECRET" not in serialized_runtime["process_env"]
    assert "MINDROOM_OAUTH_GOOGLE_GMAIL_CLIENT_SECRET" not in serialized_runtime["process_env"]
    assert "MINDROOM_OAUTH_GOOGLE_DRIVE_CLIENT_SECRET" not in serialized_runtime["env_file_values"]
