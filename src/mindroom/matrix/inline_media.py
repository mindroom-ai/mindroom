"""Bounded registry and wire-attachment helpers for encrypted inline Matrix images."""

from __future__ import annotations

import copy
import html
import re
from collections import OrderedDict
from collections.abc import Mapping
from threading import Lock
from typing import Any

from mindroom.constants import INLINE_MEDIA_CONTENT_KEY

_MAX_ENTRIES = 512
_LOCK = Lock()
_REGISTRY: OrderedDict[tuple[str, str], dict[str, Any]] = OrderedDict()
_IMG_SRC_PATTERN = re.compile(r"""<img\b[^>]*?(?<![\w-])src=(?:"(mxc://[^"]+)"|'(mxc://[^']+)')""", re.IGNORECASE)


def register_inline_media(room_id: str, mxc_uri: str, encrypted_file: Mapping[str, Any]) -> None:
    """Record one encrypted inline image descriptor scoped to ``(room_id, mxc_uri)``."""
    if not room_id or not mxc_uri.startswith("mxc://") or encrypted_file.get("url") != mxc_uri:
        msg = "Inline media registration requires a room ID and an EncryptedFile matching its mxc:// URI."
        raise ValueError(msg)
    key = (room_id, mxc_uri)
    with _LOCK:
        _REGISTRY[key] = copy.deepcopy(dict(encrypted_file))
        _REGISTRY.move_to_end(key)
        while len(_REGISTRY) > _MAX_ENTRIES:
            _REGISTRY.popitem(last=False)


def _img_mxc_uris(formatted_body: object) -> list[str]:
    if not isinstance(formatted_body, str) or "mxc://" not in formatted_body:
        return []
    return list(
        dict.fromkeys(html.unescape(m.group(1) or m.group(2)) for m in _IMG_SRC_PATTERN.finditer(formatted_body)),
    )


def strip_inline_media_from_content(content: dict[str, Any]) -> dict[str, Any]:
    """Return ``content`` without ``io.mindroom.inline_media`` keys, copying only if present."""
    new_content = content.get("m.new_content")
    inner_has_key = isinstance(new_content, Mapping) and INLINE_MEDIA_CONTENT_KEY in new_content
    if INLINE_MEDIA_CONTENT_KEY not in content and not inner_has_key:
        return content
    cleaned = {k: v for k, v in content.items() if k != INLINE_MEDIA_CONTENT_KEY}
    if isinstance(new_content, Mapping) and INLINE_MEDIA_CONTENT_KEY in new_content:
        cleaned["m.new_content"] = {k: v for k, v in new_content.items() if k != INLINE_MEDIA_CONTENT_KEY}
    return cleaned


def filter_inline_media_for_html(source: Mapping[str, Any], formatted_body: str) -> dict[str, dict[str, Any]] | None:
    """Filter ``source``'s inline media map down to entries referenced by ``formatted_body``."""
    existing = source.get(INLINE_MEDIA_CONTENT_KEY)
    if not isinstance(existing, Mapping) or not existing:
        return None
    filtered = {
        uri: copy.deepcopy(dict(entry))
        for uri in _img_mxc_uris(formatted_body)
        if isinstance(entry := existing.get(uri), Mapping) and entry.get("url") == uri
    }
    return filtered or None


def copy_referenced_inline_media(source: Mapping[str, Any] | None, target: dict[str, Any]) -> None:
    """Copy onto ``target`` only the inline media keys from ``source`` referenced by ``target``'s HTML."""
    target.pop(INLINE_MEDIA_CONTENT_KEY, None)
    if source is None:
        return
    formatted_body = target.get("formatted_body")
    if isinstance(formatted_body, str) and (inline_media := filter_inline_media_for_html(source, formatted_body)):
        target[INLINE_MEDIA_CONTENT_KEY] = inline_media


def attach_inline_media(content: dict[str, Any], room_id: str, *, room_encrypted: bool) -> dict[str, Any]:
    """Attach same-room encrypted inline image keys for ``<img src>`` URIs in ``content``."""
    if room_encrypted is not True:
        return strip_inline_media_from_content(content)
    relates = content.get("m.relates_to")
    is_edit = (
        isinstance(content.get("m.new_content"), dict)
        and isinstance(relates, dict)
        and relates.get("rel_type") == "m.replace"
    )
    visible = content["m.new_content"] if is_edit else content
    formatted_body = visible.get("formatted_body")
    uris = _img_mxc_uris(formatted_body)
    inline_media: dict[str, dict[str, Any]] = (
        filter_inline_media_for_html(visible, formatted_body) or {} if isinstance(formatted_body, str) and uris else {}
    )
    if uris:
        with _LOCK:
            for uri in uris:
                if (entry := _REGISTRY.get((room_id, uri))) is not None:
                    _REGISTRY.move_to_end((room_id, uri))
                    inline_media[uri] = copy.deepcopy(entry)
    stripped = strip_inline_media_from_content(content)
    if not inline_media:
        return stripped
    if is_edit:
        return {**stripped, "m.new_content": {**stripped["m.new_content"], INLINE_MEDIA_CONTENT_KEY: inline_media}}
    return {**stripped, INLINE_MEDIA_CONTENT_KEY: inline_media}
