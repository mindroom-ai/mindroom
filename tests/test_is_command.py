"""Tests for _is_command function."""

from __future__ import annotations

from mindroom.bot import _is_command


def test_is_command_direct() -> None:
    """Test direct command detection."""
    assert _is_command("!help")
    assert _is_command("!schedule in 5 minutes check")
    assert _is_command("  !invite assistant  ")  # with whitespace
    assert not _is_command("help")
    assert not _is_command("just a message")
    assert not _is_command("")


def test_is_command_with_emoji() -> None:
    """Test command detection with voice emoji prefixes."""
    # Microphone emoji
    assert _is_command("🎤 !help")
    assert _is_command("🎤!invite assistant")  # no space after emoji
    assert _is_command("🎤   !schedule tomorrow")  # multiple spaces

    # Other voice emojis
    assert _is_command("🎙️ !help")
    assert _is_command("🗣️ !list_invites")
    assert _is_command("🎵 !schedule in 5 minutes")

    # Emoji but no command
    assert not _is_command("🎤 just talking")
    assert not _is_command("🎤 invite someone")  # no ! prefix
    assert not _is_command("🎙️ regular message")


def test_is_command_edge_cases() -> None:
    """Test edge cases."""
    assert not _is_command("")
    assert not _is_command("   ")
    assert not _is_command("🎤")  # just emoji
    assert not _is_command("🎤 ")  # emoji with space
    assert _is_command("!")  # technically a command start
    assert not _is_command("test!command")  # ! in middle
