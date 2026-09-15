"""Tests for authored model presentation metadata."""

from __future__ import annotations

from typing import TYPE_CHECKING

import pytest
from pydantic import ValidationError

from mindroom.config.main import Config
from mindroom.config.models import ModelConfig
from mindroom.model_loading import get_model_instance
from mindroom.synthetic_model import SyntheticModel
from tests.conftest import bind_runtime_paths, runtime_paths_for, test_runtime_paths

if TYPE_CHECKING:
    from pathlib import Path


def test_metadata_survives_config_roundtrip() -> None:
    """Authored presentation metadata should be trimmed and serialized."""
    model = ModelConfig(
        provider="openai",
        id="test-model",
        display_name="  Quick helper  ",
        icon="  icons/helper.png  ",
    )

    assert model.model_dump()["display_name"] == "Quick helper"
    assert model.model_dump()["icon"] == "icons/helper.png"


def test_blank_metadata_uses_default_presentation() -> None:
    """Blank presentation metadata should normalize to an absent value."""
    model = ModelConfig(provider="openai", id="test-model", display_name="  ", icon=" ")

    assert model.display_name is None
    assert model.icon is None


@pytest.mark.parametrize(
    "icon",
    [
        "icons/helper.png",
        "images/models/helper.svg",
        "mxc://server/media-id",
        "mxc://server:8448/media-id",
        "mxc://192.0.2.1/media-id",
        "mxc://192.0.2.1:8448/media-id",
        "mxc://[2001:db8::1]/media-id",
        "mxc://[2001:db8::1]:8448/media-id",
    ],
)
def test_icon_accepts_supported_authored_locations(icon: str) -> None:
    """Config-relative paths and structurally valid Matrix URIs should pass."""
    model = ModelConfig(provider="openai", id="test-model", icon=icon)

    assert model.icon == icon


@pytest.mark.parametrize(
    "icon",
    [
        "/icons/helper.png",
        r"C:\icons\helper.png",
        r"\icons\helper.png",
        "https://example.com/helper.png",
        "http://example.com/helper.png",
        "file://icons/helper.png",
        "data:image/png;base64,AAAA",
        "mxc://server",
        "mxc:///media-id",
        "mxc://server/media-id/extra",
        "mxc://server/media-id?download=1",
        "mxc://user@server/media-id",
        "mxc://user:password@server/media-id",
        "mxc://server:bad/media-id",
        "mxc://server:70000/media-id",
        "mxc://server:/media-id",
        "mxc://server_name/media-id",
        "mxc://server%20name/media-id",
        r"mxc://server\name/media-id",
    ],
)
def test_icon_rejects_unsupported_authored_locations(icon: str) -> None:
    """Remote, rooted, and malformed authored icon locations should fail."""
    with pytest.raises(ValidationError):
        ModelConfig(provider="openai", id="test-model", icon=icon)


def test_icon_canonicalizes_mixed_case_matrix_scheme() -> None:
    """Accepted Matrix icon URIs should use a consistent lowercase scheme."""
    model = ModelConfig(provider="openai", id="test-model", icon="MXC://Server.Example/media-id")

    assert model.icon == "mxc://Server.Example/media-id"


def test_display_metadata_is_not_forwarded_to_model_provider(tmp_path: Path) -> None:
    """Presentation metadata should remain outside provider constructor kwargs."""
    config = bind_runtime_paths(
        Config(
            models={
                "presented": ModelConfig(
                    provider="synthetic",
                    id="synthetic-test",
                    display_name="Quick helper",
                    icon="icons/helper.png",
                    extra_kwargs={"seed": 7},
                ),
            },
        ),
        test_runtime_paths(tmp_path),
    )

    model = get_model_instance(config, runtime_paths_for(config), "presented")

    assert isinstance(model, SyntheticModel)
    assert model.id == "synthetic-test"
    assert model.seed == 7
