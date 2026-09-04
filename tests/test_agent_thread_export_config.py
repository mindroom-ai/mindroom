"""Tests for the per-agent thread_exports setting."""

from __future__ import annotations

import pytest

from mindroom.config.agent import AgentConfig, AgentThreadExportConfig


def test_thread_exports_defaults_to_disabled() -> None:
    """Thread exports defaults to disabled."""
    assert AgentConfig(display_name="A").thread_exports is None


def test_thread_exports_true_enables_defaults() -> None:
    """Thread exports true enables defaults."""
    config = AgentConfig.model_validate({"display_name": "A", "thread_exports": True})
    assert config.thread_exports == AgentThreadExportConfig()
    assert config.thread_exports is not None
    assert config.thread_exports.invited_rooms is True
    assert config.thread_exports.private_room_scope == "owner_and_agent"


def test_thread_exports_false_disables() -> None:
    """Thread exports false disables."""
    config = AgentConfig.model_validate({"display_name": "A", "thread_exports": False})
    assert config.thread_exports is None


def test_thread_exports_mapping_sets_options() -> None:
    """Thread exports mapping sets options."""
    config = AgentConfig.model_validate(
        {"display_name": "A", "thread_exports": {"invited_rooms": False, "private_room_scope": "owner"}},
    )
    assert config.thread_exports == AgentThreadExportConfig(invited_rooms=False, private_room_scope="owner")


def test_thread_exports_rejects_unknown_scope() -> None:
    """Thread exports rejects unknown scope."""
    with pytest.raises(ValueError, match="private_room_scope"):
        AgentConfig.model_validate({"display_name": "A", "thread_exports": {"private_room_scope": "everyone"}})
