"""Published knowledge index metadata JSON helpers."""

from __future__ import annotations

import json
import os
import uuid
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from collections.abc import Container, Mapping
    from pathlib import Path


_IndexMetadataFields = tuple[dict[str, str], str, str | None, str | None, str | None, int | None, str | None]


def load_index_metadata_payload(metadata_path: Path) -> dict[str, object] | None:  # noqa: D103
    try:
        payload = json.loads(metadata_path.read_text(encoding="utf-8")) if metadata_path.exists() else None
    except (OSError, json.JSONDecodeError):
        return None
    return dict(payload) if isinstance(payload, dict) else None


def optional_metadata_str(value: object) -> str | None:  # noqa: D103
    return value if isinstance(value, str) and value else None


def _coerce_nonnegative_metadata_int(value: object) -> int | None:
    match value:
        case bool():
            return None
        case int() if value >= 0:
            return value
        case float() if value.is_integer() and value >= 0:
            return int(value)
        case str() if value.strip().isdigit():
            return int(value.strip())
    return None


def parse_index_metadata_fields(
    payload: Mapping[str, object],
    *,
    allowed_statuses: Container[str],
    require_complete_fields_for_all_statuses: bool = False,
) -> _IndexMetadataFields | None:
    """Parse the shared published-index fields from a JSON object."""
    raw_settings = payload.get("settings")
    raw_status = payload.get("status")
    if not isinstance(raw_settings, dict) or not isinstance(raw_status, str) or raw_status not in allowed_statuses:
        return None
    settings: dict[str, str] = {}
    for key, value in raw_settings.items():
        if not isinstance(key, str) or not isinstance(value, str):
            return None
        settings[key] = value

    collection = optional_metadata_str(payload.get("collection"))
    indexed_count = _coerce_nonnegative_metadata_int(payload.get("indexed_count"))
    source_signature = optional_metadata_str(payload.get("source_signature"))
    settings_mode = optional_metadata_str(settings.get("mode"))
    require_complete_fields = require_complete_fields_for_all_statuses or raw_status == "complete"
    collection_required = require_complete_fields and not (raw_status == "complete" and settings_mode == "files")
    if require_complete_fields and (
        (collection_required and collection is None) or indexed_count is None or source_signature is None
    ):
        return None

    return (
        settings,
        raw_status,
        collection,
        optional_metadata_str(payload.get("last_published_at")),
        optional_metadata_str(payload.get("published_revision")),
        indexed_count,
        source_signature,
    )


def write_index_metadata_payload(  # noqa: D103
    metadata_path: Path,
    *,
    settings: Mapping[str, str],
    status: str,
    **fields: object | None,
) -> None:
    payload = {
        "settings": dict(settings),
        "status": status,
        **{key: value for key, value in fields.items() if value is not None},
    }
    metadata_path.parent.mkdir(parents=True, exist_ok=True)
    tmp_path = metadata_path.with_name(f".{metadata_path.name}.{os.getpid()}.{uuid.uuid4().hex}.tmp")
    try:
        tmp_path.write_text(json.dumps(payload, sort_keys=True), encoding="utf-8")
        tmp_path.replace(metadata_path)
    except Exception:
        tmp_path.unlink(missing_ok=True)
        raise
