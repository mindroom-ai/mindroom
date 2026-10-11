"""RFB parser security boundaries use real fragmented protocol bytes."""

import pytest

from mindroom.worker_computer.rfb import RfbClientFilter, RfbProtocolError


def connected_parser() -> RfbClientFilter:
    """Connected parser."""
    parser = RfbClientFilter()
    assert parser.feed(b"RFB 003.008\n", allow_input=False) == b"RFB 003.008\n"
    assert parser.feed(b"\x01\x01", allow_input=False) == b"\x01\x01"
    return parser


def test_watcher_cannot_type_through_fragmented_rfb_messages() -> None:
    """Watcher cannot type through fragmented rfb messages."""
    parser = connected_parser()
    key = bytes.fromhex("0401000000000061")
    assert parser.feed(key[:3], allow_input=False) == b""
    assert parser.feed(key[3:], allow_input=False) == b""
    update = bytes.fromhex("03010000000005000320")
    assert parser.feed(update, allow_input=False) == update
    assert parser.feed(key, allow_input=True) == key
    assert parser.feed(key[:3], allow_input=True) == b""
    assert parser.feed(key[3:], allow_input=False) == b""


@pytest.mark.parametrize("payload", [b"\xfe", bytes.fromhex("060000007fffffff"), bytes.fromhex("06000000ffffffff")])
def test_unknown_or_oversized_messages_fail_closed(payload: bytes) -> None:
    """Unknown or oversized messages fail closed."""
    with pytest.raises(RfbProtocolError):
        connected_parser().feed(payload, allow_input=True)


@pytest.mark.parametrize("version", [b"RFB 003.003\n", b"garbage!!!!!"])
def test_invalid_handshake_is_rejected(version: bytes) -> None:
    """Invalid handshake is rejected."""
    with pytest.raises(RfbProtocolError):
        RfbClientFilter().feed(version, allow_input=True)


def test_watcher_negotiation_and_clipboard_suppression() -> None:
    """Watcher negotiation and clipboard suppression."""
    parser = connected_parser()
    encodings = bytes.fromhex("0200000100000000")
    clipboard = bytes.fromhex("0600000000000003") + b"abc"
    pointer = bytes.fromhex("050100010002")
    assert parser.feed(encodings + clipboard + pointer, allow_input=False) == encodings
    assert parser.feed(clipboard + pointer, allow_input=True) == clipboard + pointer


def test_extended_clipboard_negotiation_is_bounded_and_watcher_cannot_write() -> None:
    """Extended clipboard negotiation is bounded and watcher cannot write."""
    parser = connected_parser()
    # Negative length denotes extended clipboard; CAPS flags and one format size.
    caps = bytes.fromhex("06000000fffffff80100000100000000")
    assert parser.feed(caps[:10], allow_input=False) == b""
    assert parser.feed(caps[10:], allow_input=False) == b""
    assert parser.feed(caps, allow_input=True) == caps
    with pytest.raises(RfbProtocolError):
        parser.feed(bytes.fromhex("0600000080000000"), allow_input=True)
