"""Tests for published-report access-policy configuration."""

from __future__ import annotations

import pytest
import yaml

from mindroom.config.main import Config
from mindroom.report_access_policy import ReportAccessPolicy
from tests.conftest import test_runtime_paths


def _base_config() -> dict[str, object]:
    return {
        "models": {
            "default": {
                "provider": "openai",
                "id": "gpt-5.6",
            },
        },
        "router": {"model": "default"},
    }


def test_report_publishing_config_preserves_public_defaults() -> None:
    """Existing deployments should keep public publication behavior."""
    config = Config.model_validate(_base_config())

    assert config.report_publishing.default_access_policy is ReportAccessPolicy.PUBLIC
    assert config.report_publishing.allow_public is True


def test_report_publishing_config_accepts_origin_room_and_public_disable() -> None:
    """Deployments may default to protected reports and disable new public links."""
    config = Config.model_validate(
        {
            **_base_config(),
            "report_publishing": {
                "default_access_policy": "origin_room",
                "allow_public": False,
            },
        },
    )

    assert config.report_publishing.default_access_policy is ReportAccessPolicy.ORIGIN_ROOM
    assert config.report_publishing.allow_public is False
    loaded = yaml.safe_load(yaml.dump(config.authored_model_dump()))
    assert loaded["report_publishing"] == {
        "allow_public": False,
        "default_access_policy": "origin_room",
    }


def test_report_publishing_config_rejects_unknown_policy() -> None:
    """Unsupported access policies should fail during schema loading."""
    with pytest.raises(ValueError, match="default_access_policy"):
        Config.model_validate(
            {
                **_base_config(),
                "report_publishing": {"default_access_policy": "shared_room"},
            },
        )


def test_origin_room_default_requires_trusted_browser_auth_at_runtime(tmp_path) -> None:  # noqa: ANN001
    """Runtime config loading should reject a protected default without viewer auth."""
    runtime_paths = test_runtime_paths(tmp_path)

    with pytest.raises(ValueError, match="MINDROOM_TRUSTED_UPSTREAM_AUTH_ENABLED"):
        Config.validate_with_runtime(
            {
                **_base_config(),
                "report_publishing": {"default_access_policy": "origin_room"},
            },
            runtime_paths,
        )


def test_origin_room_default_accepts_trusted_browser_auth_runtime(tmp_path) -> None:  # noqa: ANN001
    """Protected defaults should load when trusted browser identity is enabled."""
    runtime_paths = test_runtime_paths(tmp_path)
    trusted_runtime_paths = runtime_paths.__class__(
        config_path=runtime_paths.config_path,
        config_dir=runtime_paths.config_dir,
        env_path=runtime_paths.env_path,
        storage_root=runtime_paths.storage_root,
        process_env={
            **dict(runtime_paths.process_env),
            "MINDROOM_TRUSTED_UPSTREAM_AUTH_ENABLED": "true",
            "MINDROOM_TRUSTED_UPSTREAM_USER_ID_HEADER": "X-Trusted-User",
            "MINDROOM_TRUSTED_UPSTREAM_MATRIX_USER_ID_HEADER": "X-Trusted-Matrix-User",
        },
        env_file_values=runtime_paths.env_file_values,
    )

    config = Config.validate_with_runtime(
        {
            **_base_config(),
            "report_publishing": {"default_access_policy": "origin_room"},
        },
        trusted_runtime_paths,
    )

    assert config.report_publishing.default_access_policy is ReportAccessPolicy.ORIGIN_ROOM
