"""Tests for generated Vertex Claude endpoints.

No authenticated provider requests are sent.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Literal

import pytest
import yaml

from mindroom.cli.config import _model_template_block, _write_env_file
from mindroom.config.main import Config
from mindroom.constants import resolve_runtime_paths
from mindroom.model_loading import get_model_instance
from mindroom.vertex_claude_compat import MindroomVertexAIClaude

if TYPE_CHECKING:
    from pathlib import Path


@pytest.fixture(autouse=True)
def _clear_vertex_endpoint_override(monkeypatch: pytest.MonkeyPatch) -> None:
    """Keep ambient SDK endpoint overrides outside these generated-default checks."""
    monkeypatch.delenv("ANTHROPIC_VERTEX_BASE_URL", raising=False)


@pytest.mark.parametrize(
    ("matrix_server", "authored_region", "replace_existing", "expected_region", "expected_endpoint"),
    [
        ("mindroom.chat", None, False, "global", "https://aiplatform.googleapis.com/v1"),
        ("self-hosted", None, False, "global", "https://aiplatform.googleapis.com/v1"),
        ("mindroom.chat", "eu", True, "global", "https://aiplatform.googleapis.com/v1"),
        ("self-hosted", "us", True, "global", "https://aiplatform.googleapis.com/v1"),
        ("mindroom.chat", "eu", False, "eu", "https://aiplatform.eu.rep.googleapis.com/v1"),
        ("self-hosted", "us", False, "us", "https://aiplatform.us.rep.googleapis.com/v1"),
    ],
)
def test_generated_vertex_environment_reaches_runtime_endpoint(
    tmp_path: Path,
    matrix_server: Literal["mindroom.chat", "self-hosted"],
    authored_region: str | None,
    replace_existing: bool,
    expected_region: str,
    expected_endpoint: str,
) -> None:
    """Fresh and replaced files use global; preserved files retain their region."""
    env_path = tmp_path / ".env"
    if authored_region is not None:
        env_path.write_text(
            f"ANTHROPIC_VERTEX_PROJECT_ID=authored-project\nCLOUD_ML_REGION={authored_region}\n",
            encoding="utf-8",
        )
    _write_env_file(
        env_path,
        matrix_server,
        "vertexai_claude",
        storage_root=tmp_path / "state",
        replace_existing=replace_existing,
    )
    runtime_paths = resolve_runtime_paths(config_path=tmp_path / "config.yaml", process_env={})
    config = Config(models={"default": yaml.safe_load(_model_template_block("vertexai_claude"))})

    model = get_model_instance(config, runtime_paths)
    assert isinstance(model, MindroomVertexAIClaude)
    assert model.id == "claude-sonnet-5"
    assert model.region == expected_region
    expected_project = (
        "authored-project" if authored_region is not None and not replace_existing else "your-gcp-project-id"
    )
    assert model.native_compaction_endpoint() == f"{expected_endpoint}|{expected_project}|{expected_region}"


@pytest.mark.parametrize(
    ("process_region", "model_region", "expected_region"),
    [(None, None, "global"), ("us", None, "us"), ("us", "eu", "eu")],
)
def test_vertex_generated_environment_keeps_override_precedence(
    tmp_path: Path,
    process_region: str | None,
    model_region: str | None,
    expected_region: str,
) -> None:
    """Process and model choices must still override the generated global endpoint."""
    _write_env_file(
        tmp_path / ".env",
        "mindroom.chat",
        "vertexai_claude",
        storage_root=tmp_path / "state",
        replace_existing=False,
    )
    process_env = {"CLOUD_ML_REGION": process_region} if process_region is not None else {}
    runtime_paths = resolve_runtime_paths(config_path=tmp_path / "config.yaml", process_env=process_env)
    config = Config(models={"default": yaml.safe_load(_model_template_block("vertexai_claude"))})
    if model_region is not None:
        config.models["default"].extra_kwargs = {"region": model_region}

    model = get_model_instance(config, runtime_paths)
    assert isinstance(model, MindroomVertexAIClaude)
    assert model.region == expected_region
