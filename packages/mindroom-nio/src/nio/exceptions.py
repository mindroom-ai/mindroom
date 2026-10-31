# Copyright © 2018 Damir Jelić <poljar@termina.org.uk>
#
# Permission to use, copy, modify, and/or distribute this software for
# any purpose with or without fee is hereby granted, provided that the
# above copyright notice and this permission notice appear in all copies.
#
# THE SOFTWARE IS PROVIDED "AS IS" AND THE AUTHOR DISCLAIMS ALL WARRANTIES
# WITH REGARD TO THIS SOFTWARE INCLUDING ALL IMPLIED WARRANTIES OF
# MERCHANTABILITY AND FITNESS. IN NO EVENT SHALL THE AUTHOR BE LIABLE FOR ANY
# SPECIAL, DIRECT, INDIRECT, OR CONSEQUENTIAL DAMAGES OR ANY DAMAGES WHATSOEVER
# RESULTING FROM LOSS OF USE, DATA OR PROFITS, WHETHER IN AN ACTION OF
# CONTRACT, NEGLIGENCE OR OTHER TORTIOUS ACTION, ARISING OUT OF OR IN
# CONNECTION WITH THE USE OR PERFORMANCE OF THIS SOFTWARE.


class ProtocolError(Exception):
    pass


class LocalProtocolError(ProtocolError):
    pass


class MembersSyncError(LocalProtocolError):
    pass


class SendRetryError(LocalProtocolError):
    pass


class RemoteProtocolError(ProtocolError):
    pass


class InsufficientRecursionDepthError(RemoteProtocolError):
    """Signal that a recursive relations page did not meet the required depth.

    Raised by ``AsyncClient.room_get_event_relations()`` when a caller passes
    ``minimum_recursion_depth`` and the server either omits ``recursion_depth``
    or reports a value below the requirement. The page's events are not
    yielded, because a caller that needs a guaranteed traversal depth cannot
    distinguish a shallow page from a complete one.
    """

    def __init__(self, required: int, reported: int | None) -> None:
        self.required = required
        self.reported = reported
        super().__init__(
            f"Server reported recursion depth {reported!r}, "
            f"but at least {required} is required"
        )


class LocalTransportError(ProtocolError):
    pass


class RemoteTransportError(ProtocolError):
    pass


class OlmTrustError(Exception):
    pass


class OlmUnverifiedDeviceError(OlmTrustError):
    def __init__(self, unverified_device, *args):
        super().__init__(*args)
        self.device = unverified_device


class VerificationError(Exception):
    pass


class EncryptionError(Exception):
    pass


class GroupEncryptionError(Exception):
    pass


class TransferCancelledError(Exception):
    pass
