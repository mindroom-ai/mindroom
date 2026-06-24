"""Tests for shared dispatch source-kind policy predicates."""

import pytest

from mindroom.constants import VISIBLE_ROUTER_VOICE_ECHO_KEY
from mindroom.dispatch_source import (
    EXTERNAL_TRIGGER_SOURCE_KIND,
    HOOK_DISPATCH_SOURCE_KIND,
    HOOK_SOURCE_KIND,
    IMAGE_SOURCE_KIND,
    MEDIA_SOURCE_KIND,
    MESSAGE_SOURCE_KIND,
    SCHEDULED_SOURCE_KIND,
    TRUSTED_INTERNAL_RELAY_SOURCE_KIND,
    VOICE_SOURCE_KIND,
    is_visible_router_voice_echo_content,
    source_kind_allows_internal_relay_detection,
    source_kind_allows_self_authored_ingress,
    source_kind_allows_trusted_original_sender,
    source_kind_bypasses_coalescing,
)


@pytest.mark.parametrize(
    "source_kind",
    [
        HOOK_SOURCE_KIND,
        HOOK_DISPATCH_SOURCE_KIND,
        SCHEDULED_SOURCE_KIND,
        TRUSTED_INTERNAL_RELAY_SOURCE_KIND,
    ],
)
def test_source_kind_bypasses_coalescing_for_synthetic_and_relay_turns(source_kind: str) -> None:
    """Synthetic fires and trusted relay turns are FIFO barriers."""
    assert source_kind_bypasses_coalescing(source_kind)


@pytest.mark.parametrize(
    "source_kind",
    [
        MESSAGE_SOURCE_KIND,
        VOICE_SOURCE_KIND,
        IMAGE_SOURCE_KIND,
        MEDIA_SOURCE_KIND,
        None,
        "",
    ],
)
def test_source_kind_bypasses_coalescing_rejects_interactive_and_unknown_turns(source_kind: str | None) -> None:
    """Interactive user turns and unknown source kinds use normal coalescing."""
    assert not source_kind_bypasses_coalescing(source_kind)


@pytest.mark.parametrize(
    "source_kind",
    [
        HOOK_SOURCE_KIND,
        HOOK_DISPATCH_SOURCE_KIND,
        SCHEDULED_SOURCE_KIND,
        TRUSTED_INTERNAL_RELAY_SOURCE_KIND,
        VOICE_SOURCE_KIND,
    ],
)
def test_source_kind_allows_trusted_original_sender_for_internal_provenance(source_kind: str) -> None:
    """Only internally generated source kinds may promote original-sender metadata."""
    assert source_kind_allows_trusted_original_sender(source_kind)


@pytest.mark.parametrize(
    "source_kind",
    [
        MESSAGE_SOURCE_KIND,
        IMAGE_SOURCE_KIND,
        MEDIA_SOURCE_KIND,
        None,
        "",
    ],
)
def test_source_kind_allows_trusted_original_sender_rejects_plain_turns(source_kind: str | None) -> None:
    """Plain user-controlled source kinds must not promote original-sender metadata."""
    assert not source_kind_allows_trusted_original_sender(source_kind)


@pytest.mark.parametrize(
    "source_kind",
    [
        "",
        MESSAGE_SOURCE_KIND,
        TRUSTED_INTERNAL_RELAY_SOURCE_KIND,
    ],
)
def test_source_kind_allows_internal_relay_detection_for_plain_text_handoff_candidates(
    source_kind: str,
) -> None:
    """Only plain text and already-relay candidates are promoted after trusted metadata inspection."""
    assert source_kind_allows_internal_relay_detection(source_kind)


@pytest.mark.parametrize(
    "source_kind",
    [
        HOOK_SOURCE_KIND,
        HOOK_DISPATCH_SOURCE_KIND,
        SCHEDULED_SOURCE_KIND,
        VOICE_SOURCE_KIND,
        IMAGE_SOURCE_KIND,
        MEDIA_SOURCE_KIND,
        None,
    ],
)
def test_source_kind_allows_internal_relay_detection_rejects_specialized_turns(
    source_kind: str | None,
) -> None:
    """Specialized source kinds should not be reclassified as generic trusted relays."""
    assert not source_kind_allows_internal_relay_detection(source_kind)


@pytest.mark.parametrize(
    "source_kind",
    [
        HOOK_DISPATCH_SOURCE_KIND,
        EXTERNAL_TRIGGER_SOURCE_KIND,
    ],
)
def test_source_kind_allows_self_authored_ingress_for_dispatch_synthetic_turns(source_kind: str) -> None:
    """Only dispatch-origin synthetic turns may bypass self-authored ingress suppression."""
    assert source_kind_allows_self_authored_ingress(source_kind)


@pytest.mark.parametrize(
    "source_kind",
    [
        MESSAGE_SOURCE_KIND,
        SCHEDULED_SOURCE_KIND,
        HOOK_SOURCE_KIND,
        TRUSTED_INTERNAL_RELAY_SOURCE_KIND,
        None,
        "",
    ],
)
def test_source_kind_allows_self_authored_ingress_rejects_plain_and_non_dispatch_turns(
    source_kind: str | None,
) -> None:
    """Plain and non-dispatch source kinds should keep normal self-authored suppression."""
    assert not source_kind_allows_self_authored_ingress(source_kind)


@pytest.mark.parametrize("content", [None, [], "visible echo"])
def test_visible_router_voice_echo_content_rejects_non_mappings(content: object) -> None:
    """Only Matrix content mappings can carry the visible voice echo marker."""
    assert not is_visible_router_voice_echo_content(content)


@pytest.mark.parametrize(
    "content",
    [
        {},
        {VISIBLE_ROUTER_VOICE_ECHO_KEY: False},
        {VISIBLE_ROUTER_VOICE_ECHO_KEY: "true"},
    ],
)
def test_visible_router_voice_echo_content_rejects_missing_or_non_true_marker(
    content: dict[str, object],
) -> None:
    """Only the explicit True marker identifies display-only router voice echoes."""
    assert not is_visible_router_voice_echo_content(content)


def test_visible_router_voice_echo_content_accepts_true_marker() -> None:
    """The visible router voice echo marker is a strict boolean metadata flag."""
    assert is_visible_router_voice_echo_content({VISIBLE_ROUTER_VOICE_ECHO_KEY: True})
