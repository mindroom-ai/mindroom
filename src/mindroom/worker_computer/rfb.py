"""Bounded RFB 3.8 client parser; unparsed bytes never reach the display."""

import struct

_MAX_BUFFER = 1024 * 1024


class RfbProtocolError(ValueError):
    """Unsupported or malformed client protocol message."""


class RfbClientFilter:
    """Filter client input after an authenticated RFB 3.8 None-security handshake."""

    def __init__(self) -> None:
        self._buffer = bytearray()
        self._phase = 0

    def feed(self, data: bytes, *, allow_input: bool) -> bytes:  # noqa: C901 - incremental protocol phases
        """Forward known complete messages, dropping input for watchers."""
        if len(self._buffer) + len(data) > _MAX_BUFFER:
            msg = "RFB client buffer limit exceeded."
            raise RfbProtocolError(msg)
        self._buffer.extend(data)
        output = bytearray()
        while self._buffer:
            if self._phase < 3:
                length = 12 if self._phase == 0 else 1
                if len(self._buffer) < length:
                    break
                packet = bytes(self._buffer[:length])
                valid = packet == b"RFB 003.008\n" if self._phase == 0 else packet == b"\x01"
                if self._phase == 2:
                    valid = packet in (b"\x00", b"\x01")
                if not valid:
                    msg = "Only RFB 3.8 with None security is supported."
                    raise RfbProtocolError(msg)
                self._phase += 1
                is_input = False
            else:
                result = self._message_length()
                if result is None:
                    break
                length, is_input = result
                if length > _MAX_BUFFER:
                    msg = "RFB client message limit exceeded."
                    raise RfbProtocolError(msg)
                if len(self._buffer) < length:
                    break
                packet = bytes(self._buffer[:length])
            del self._buffer[:length]
            if not is_input or allow_input:
                output.extend(packet)
        return bytes(output)

    def _message_length(self) -> tuple[int, bool] | None:  # noqa: C901, PLR0911, PLR0912 - protocol message table
        kind = self._buffer[0]
        fixed = {0: (20, False), 3: (10, False), 4: (8, True), 5: (6, True)}
        if kind in fixed:
            return fixed[kind]
        if kind == 2:  # SetEncodings
            if len(self._buffer) < 4:
                return None
            return 4 + 4 * struct.unpack_from("!H", self._buffer, 2)[0], False
        if kind == 6:  # ClientCutText, including bounded extended clipboard negotiation.
            if len(self._buffer) < 8:
                return None
            length = struct.unpack_from("!i", self._buffer, 4)[0]
            if -4 < length < 0:
                msg = "Invalid extended clipboard payload."
                raise RfbProtocolError(msg)
            return 8 + abs(length), True
        if kind == 150:  # EnableContinuousUpdates
            return 10, False
        if kind == 248:  # ClientFence
            if len(self._buffer) < 9:
                return None
            length = self._buffer[8]
            if length > 64:
                msg = "Invalid RFB fence payload."
                raise RfbProtocolError(msg)
            return 9 + length, False
        if kind == 251:  # SetDesktopSize
            if len(self._buffer) < 8:
                return None
            return 8 + 16 * self._buffer[6], True
        if kind == 255:  # QEMU ExtendedKeyEvent
            if len(self._buffer) < 2:
                return None
            if self._buffer[1] == 0:
                return 12, True
        msg = "Unsupported RFB client message."
        raise RfbProtocolError(msg)
