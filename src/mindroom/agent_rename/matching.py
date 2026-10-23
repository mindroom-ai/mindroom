"""Find a Matrix user ID in text the way the homeserver's in-place account rename does.

An occurrence counts only when nothing after it continues the server name:
a letter or digit, a ``.`` or ``-`` before one, or a ``:`` before a port.
A port is one or more digits that end the text or precede anything but an ASCII letter;
digits followed by a letter start a device ID, so ``:12AB`` ends the user ID while ``:8448`` and ``:1234`` do not.
So ``@a:example.org`` is found in ``ask @a:example.org.``, ``"@a:example.org:DEVICEID"``, and ``@a:example.org:12AB``,
but not in ``@a:example.org.uk``, ``@a:example.org:8448``, or ``@a:example.org-gateway:x``, which name other users.
This mirrors ``find_user_id`` and ``continues_server_name`` in Tuwunel's ``query user-id-rewrite``
(mindroom-tuwunel#109 as of commit 875f00923), so local state ends up holding exactly the IDs the server now sends.
"""

from __future__ import annotations

import re

# A port's digits are followed by neither a digit nor a letter, so backtracking into them cannot make a port.
_CONTINUES_SERVER_NAME = r"(?![A-Za-z0-9]|[.-][A-Za-z0-9]|:[0-9]+(?![0-9A-Za-z]))"
_NEVER = re.compile(r"(?!)")


def _user_id_pattern(user_id: str) -> re.Pattern[str]:
    """Return a pattern matching ``user_id`` wherever nothing after it continues its server name.

    An empty ID matches nothing.
    """
    if not user_id:
        return _NEVER
    return re.compile(re.escape(user_id) + _CONTINUES_SERVER_NAME)


def contains_user_id(text: str, user_id: str) -> bool:
    """Whether ``text`` holds ``user_id`` with nothing after it continuing its server name."""
    return _user_id_pattern(user_id).search(text) is not None


def replace_user_id(text: str, old_user_id: str, new_user_id: str) -> str:
    """Replace every bounded occurrence of ``old_user_id`` in ``text`` with ``new_user_id``, taken literally."""
    return _user_id_pattern(old_user_id).sub(lambda _: new_user_id, text)
