"""Bounded registry and wire-attachment helpers for encrypted inline Matrix images."""

from __future__ import annotations

import copy
from collections import OrderedDict
from collections.abc import Mapping
from html.parser import HTMLParser
from threading import Lock
from typing import Any

from mindroom.constants import INLINE_MEDIA_CONTENT_KEY

_MAX_INLINE_MEDIA_ENTRIES = 512
_LOCK = Lock()
_REGISTRY: OrderedDict[tuple[str, str], dict[str, Any]] = OrderedDict()


class _InlineImageParser(HTMLParser):
    """Collect ``mxc://`` values from real ``<img src>`` elements outside code blocks."""

    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self._code_depth = 0
        self.uris: list[str] = []

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        lower_tag = tag.lower()
        if lower_tag in {"code", "pre"}:
            self._code_depth += 1
            return
        if lower_tag != "img" or self._code_depth > 0:
            return
        for attr_name, attr_value in attrs:
            if (
                attr_name.lower() == "src"
                and isinstance(attr_value, str)
                and attr_value.startswith("mxc://")
                and attr_value not in self.uris
            ):
                self.uris.append(attr_value)
                return

    def handle_endtag(self, tag: str) -> None:
        if tag.lower() in {"code", "pre"} and self._code_depth > 0:
            self._code_depth -= 1


def clear_inline_media_registry() -> None:
    """Clear the process-local inline media key registry."""
    with _LOCK:
        _REGISTRY.clear()


def register_inline_media(room_id: str, mxc_uri: str, encrypted_file: Mapping[str, Any]) -> None:
    """Record one encrypted inline image descriptor scoped to ``(room_id, mxc_uri)``."""
    if not room_id or not mxc_uri.startswith("mxc://"):
        msg = "Inline media registration requires a room ID and mxc:// URI."
        raise ValueError(msg)
    if encrypted_file.get("url") != mxc_uri:
        msg = "Encrypted inline media descriptor URL must match its mxc:// map key."
        raise ValueError(msg)
    snapshot = copy.deepcopy(dict(encrypted_file))
    key = (room_id, mxc_uri)
    with _LOCK:
        _REGISTRY[key] = snapshot
        _REGISTRY.move_to_end(key)
        while len(_REGISTRY) > _MAX_INLINE_MEDIA_ENTRIES:
            _REGISTRY.popitem(last=False)


def extract_inline_img_mxc_uris(formatted_body: str) -> list[str]:
    """Return unique ``mxc://`` URIs referenced as ``<img src>`` in rendered HTML."""
    if "mxc://" not in formatted_body:
        return []
    parser = _InlineImageParser()
    parser.feed(formatted_body)
    parser.close()
    return parser.uris


def filter_inline_media_for_html(
    source: Mapping[str, Any],
    formatted_body: str,
) -> dict[str, dict[str, Any]] | None:
    """Filter ``source``'s inline media map down to entries referenced by ``formatted_body``."""
    existing = source.get(INLINE_MEDIA_CONTENT_KEY)
    if not isinstance(existing, Mapping) or not existing:
        return None
    filtered: dict[str, dict[str, Any]] = {}
    for uri in extract_inline_img_mxc_uris(formatted_body):
        entry = existing.get(uri)
        if isinstance(entry, Mapping) and entry.get("url") == uri:
            filtered[uri] = copy.deepcopy(dict(entry))
    return filtered or None


def _is_edit_content(content: Mapping[str, Any]) -> bool:
    relates_to = content.get("m.relates_to")
    return (
        isinstance(content.get("m.new_content"), dict)
        and isinstance(relates_to, dict)
        and relates_to.get("rel_type") == "m.replace"
    )


def _lookup_inline_media(room_id: str, uris: list[str], existing: object) -> dict[str, dict[str, Any]]:
    matched: dict[str, dict[str, Any]] = {}
    existing_map = existing if isinstance(existing, Mapping) else None
    with _LOCK:
        for uri in uris:
            key = (room_id, uri)
            entry = _REGISTRY.get(key)
            if entry is not None:
                _REGISTRY.move_to_end(key)
                matched[uri] = copy.deepcopy(entry)
                continue
            if existing_map is not None:
                prior = existing_map.get(uri)
                if isinstance(prior, Mapping) and prior.get("url") == uri:
                    matched[uri] = copy.deepcopy(dict(prior))
    return matched


def attach_inline_media(
    content: dict[str, Any],
    room_id: str,
    *,
    room_encrypted: bool,
) -> dict[str, Any]:
    """Attach same-room encrypted inline image keys for ``<img src>`` URIs in ``content``.

    Plaintext and unknown-encryption rooms are left untouched. On ``m.replace`` edits,
    the map lives only on ``m.new_content`` and never on the outer fallback.
    """
    if room_encrypted is not True:
        return content
    is_edit = _is_edit_content(content)
    visible = content["m.new_content"] if is_edit else content
    formatted_body = visible.get("formatted_body")
    uris = extract_inline_img_mxc_uris(formatted_body) if isinstance(formatted_body, str) else []
    inline_media = _lookup_inline_media(room_id, uris, visible.get(INLINE_MEDIA_CONTENT_KEY)) if uris else {}

    if not inline_media:
        if INLINE_MEDIA_CONTENT_KEY not in visible and (not is_edit or INLINE_MEDIA_CONTENT_KEY not in content):
            return content
        if is_edit:
            updated = {key: value for key, value in content.items() if key != INLINE_MEDIA_CONTENT_KEY}
            updated["m.new_content"] = {key: value for key, value in visible.items() if key != INLINE_MEDIA_CONTENT_KEY}
            return updated
        return {key: value for key, value in content.items() if key != INLINE_MEDIA_CONTENT_KEY}

    if is_edit:
        updated = {key: value for key, value in content.items() if key != INLINE_MEDIA_CONTENT_KEY}
        updated["m.new_content"] = {**visible, INLINE_MEDIA_CONTENT_KEY: inline_media}
        return updated
    return {**content, INLINE_MEDIA_CONTENT_KEY: inline_media}


__all__ = [
    "attach_inline_media",
    "clear_inline_media_registry",
    "extract_inline_img_mxc_uris",
    "filter_inline_media_for_html",
    "register_inline_media",
]
