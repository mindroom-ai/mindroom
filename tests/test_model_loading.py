"""Tests for model provider construction."""

from __future__ import annotations

from typing import TYPE_CHECKING

from mindroom.config.main import Config
from mindroom.config.models import ModelConfig
from mindroom.model_loading import get_model_instance
from tests.conftest import bind_runtime_paths, runtime_paths_for, test_runtime_paths

if TYPE_CHECKING:
    from pathlib import Path


def test_vertexai_claude_preserves_pre_agno_2_6_timeout_for_large_outputs(tmp_path: Path) -> None:
    """Vertex Claude keeps explicit timeout so large max_tokens can run non-streaming."""
    config = bind_runtime_paths(
        Config(
            models={
                "opus": ModelConfig(
                    provider="vertexai_claude",
                    id="claude-opus-4-8",
                    extra_kwargs={
                        "project_id": "dummy-project",
                        "region": "us-east1",
                        "max_tokens": 32768,
                    },
                ),
            },
        ),
        test_runtime_paths(tmp_path),
    )

    model = get_model_instance(config, runtime_paths_for(config), "opus")

    assert model.timeout == 60.0


def test_anthropic_timeout_override_is_preserved(tmp_path: Path) -> None:
    """Explicit Claude timeout config wins over compatibility default."""
    config = bind_runtime_paths(
        Config(
            models={
                "claude": ModelConfig(
                    provider="anthropic",
                    id="claude-opus-4-8",
                    extra_kwargs={
                        "api_key": "dummy-key",
                        "timeout": 120.0,
                    },
                ),
            },
        ),
        test_runtime_paths(tmp_path),
    )

    model = get_model_instance(config, runtime_paths_for(config), "claude")

    assert model.timeout == 120.0
