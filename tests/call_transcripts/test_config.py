"""Tests for the call transcripts settings."""

from __future__ import annotations

import pytest
from pydantic import ValidationError

from mindroom.config.calls import CallTranscriptsConfig
from mindroom.config.main import Config


def test_call_transcripts_are_off_by_default() -> None:
    """Without settings, calls are not transcribed and no notes are written."""
    transcripts = Config().calls.transcripts

    assert transcripts == CallTranscriptsConfig(enabled=False, notes_model=None, languages=[])


def test_call_transcripts_accept_a_configured_notes_model_and_language_hints() -> None:
    """The notes model names a top-level model and the language hints keep their order."""
    config = Config.model_validate(
        {
            "models": {"notes": {"provider": "openai", "id": "gpt-6.1-sol"}},
            "calls": {"transcripts": {"enabled": True, "notes_model": "notes", "languages": ["nl", "en"]}},
        },
    )

    assert config.calls.transcripts == CallTranscriptsConfig(enabled=True, notes_model="notes", languages=["nl", "en"])


def test_call_transcripts_refuse_an_unknown_notes_model() -> None:
    """A notes model that is not a top-level model fails when the config loads."""
    with pytest.raises(ValueError, match=r"calls\.transcripts\.notes_model references unknown model: missing"):
        Config.model_validate({"models": {}, "calls": {"transcripts": {"enabled": True, "notes_model": "missing"}}})


def test_call_transcripts_language_hints_are_trimmed_and_never_blank() -> None:
    """A language code is sent as written without surrounding spaces, and an empty one fails at load."""
    assert CallTranscriptsConfig(languages=[" nl "]).languages == ["nl"]
    for blank in ("", "  "):
        with pytest.raises(ValidationError, match=r"languages\.0\n  String should have at least 1 character"):
            CallTranscriptsConfig.model_validate({"languages": [blank, "en"]})


def test_call_transcripts_refuse_unknown_fields() -> None:
    """A misspelled setting fails instead of being ignored."""
    with pytest.raises(ValidationError, match="notes_models"):
        CallTranscriptsConfig.model_validate({"notes_models": "notes"})
