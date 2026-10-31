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

from __future__ import annotations

import logging
import time
from collections import UserDict
from typing import Any
from uuid import UUID, uuid4

logger = logging.getLogger(__name__)

USER_AGENT = "nio"
type _Http2Header = tuple[bytes | str, bytes | str]


class HeaderDict(UserDict):
    def __setitem__(self, key, value):
        super().__setitem__(key.lower(), value)

    def __getitem__(self, key):
        return super().__getitem__(key.lower())


class TransportResponse:
    def __init__(self, uuid: UUID | None = None, timeout: float = 0) -> None:
        self.headers: HeaderDict = HeaderDict()
        self.content: bytes = b""
        self.status_code: int | None = None
        self.uuid = uuid or uuid4()
        self.creation_time = time.time()
        self.timeout: float = timeout
        self.send_time: float | None = None
        self.receive_time: float | None = None
        self.request_info: Any | None = None

    @property
    def elapsed(self) -> float:
        if (self.receive_time is not None) and (self.send_time is not None):
            elapsed = self.receive_time - self.send_time

        elif self.send_time is not None:
            elapsed = time.time() - self.send_time

        else:
            elapsed = 0.0

        return max(0, elapsed - (self.timeout / 1000))

    @property
    def text(self):
        return self.content.decode("utf-8")


class Connection:
    def connect(self) -> bytes:
        return b""

    def disconnect(self) -> bytes:
        return b""
