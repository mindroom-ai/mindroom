"""Canonical Matrix long-text sidecar content parsing."""

from __future__ import annotations

from collections.abc import Mapping
from typing import Any

_LONG_TEXT_METADATA_KEY = "io.mindroom.long_text"
_LONG_TEXT_UNAVAILABLE_KEY = "io.mindroom.long_text_unavailable"
_LONG_TEXT_UNAVAILABLE_NOTE = "[long message content unavailable]"


def _validated_mxc_url(value: object) -> str | None:
    """Return one structurally complete Matrix content URI."""
    if not isinstance(value, str) or not value.startswith("mxc://"):
        return None
    server_name, separator, media_id = value[len("mxc://") :].partition("/")
    return value if server_name and separator and media_id else None


def sidecar_mxc_url(content: Mapping[str, Any]) -> str | None:
    """Return the valid MXC URL for one supported v2 long-text sidecar."""
    metadata = content.get(_LONG_TEXT_METADATA_KEY)
    if not isinstance(metadata, dict) or metadata.get("version") != 2:
        return None
    if metadata.get("encoding") != "matrix_event_content_json":
        return None
    if (url := _validated_mxc_url(content.get("url"))) is not None:
        return url
    encrypted_file = content.get("file")
    if not isinstance(encrypted_file, dict):
        return None
    return _validated_mxc_url(encrypted_file.get("url"))


def sidecar_content_to_resolve(content: Mapping[str, Any]) -> Mapping[str, Any] | None:
    """Return the content dict that owns one unresolved long-text sidecar.

    An edit carries its sidecar inside ``m.new_content``, because the outer
    content of an edit is a fallback for clients that do not apply edits. Both
    shapes have to be recognised by anything deciding whether the text it holds
    is the whole message or a preview of it.
    """
    for candidate in (content, content.get("m.new_content")):
        if isinstance(candidate, Mapping) and sidecar_mxc_url(candidate) is not None:
            return candidate
    return None


def holds_unresolved_sidecar(content: Mapping[str, Any]) -> bool:
    """Return whether this content's text is a preview rather than the message.

    Resolved content is what the sidecar file itself holds, and that payload
    carries no sidecar metadata of its own, so resolving is what makes this
    false. Nothing has to remember to clear a flag.
    """
    return sidecar_content_to_resolve(content) is not None


def unavailable_sidecar_content(content: Mapping[str, Any]) -> dict[str, Any]:
    """Return content that settles a sidecar which can never be read, keeping its preview.

    The sidecar metadata is removed from whichever dict owned it, top-level content or an edit's
    ``m.new_content``, and replaced by an explicit marker. The preview body gains a note, so a model reading
    it knows the text is incomplete. The event's ``m.relates_to`` and every other field stay as sent.
    """
    placeholder = dict(content)
    owner = sidecar_content_to_resolve(content)
    if owner is None:
        return placeholder
    settled_owner = {key: value for key, value in owner.items() if key != _LONG_TEXT_METADATA_KEY}
    preview = owner.get("body")
    settled_owner["body"] = (
        f"{preview}\n\n{_LONG_TEXT_UNAVAILABLE_NOTE}"
        if isinstance(preview, str) and preview
        else _LONG_TEXT_UNAVAILABLE_NOTE
    )
    settled_owner[_LONG_TEXT_UNAVAILABLE_KEY] = True
    if owner is content:
        return settled_owner
    placeholder["m.new_content"] = settled_owner
    return placeholder
