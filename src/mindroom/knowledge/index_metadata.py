"""Published knowledge index metadata JSON helpers."""

from __future__ import annotations

import json
import os
import uuid
from dataclasses import dataclass
from typing import TYPE_CHECKING, cast

if TYPE_CHECKING:
    from collections.abc import Container, Mapping
    from pathlib import Path


@dataclass(frozen=True)
class IndexMetadataFields:
    """Shared fields persisted in knowledge index metadata."""

    settings: tuple[str, ...]
    status: str
    collection: str | None = None
    last_published_at: str | None = None
    published_revision: str | None = None
    indexed_count: int | None = None
    source_signature: str | None = None


def load_index_metadata_payload(metadata_path: Path) -> dict[str, object] | None:
    """Load a JSON object from an index metadata file."""
    if not metadata_path.exists():
        return None
    try:
        payload = json.loads(metadata_path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return None
    if not isinstance(payload, dict):
        return None
    return dict(payload)


def optional_metadata_str(value: object) -> str | None:
    """Return non-empty persisted metadata strings."""
    return value if isinstance(value, str) and value else None


def _coerce_nonnegative_metadata_int(value: object) -> int | None:
    """Coerce JSON metadata values into a nonnegative integer."""
    if isinstance(value, bool):
        return None
    if isinstance(value, int) and value >= 0:
        return value
    if isinstance(value, float) and value.is_integer() and value >= 0:
        return int(value)
    if isinstance(value, str) and value.strip().isdigit():
        return int(value.strip())
    return None


def parse_index_metadata_fields(
    payload: Mapping[str, object],
    *,
    allowed_statuses: Container[str],
    require_complete_fields_for_all_statuses: bool = False,
) -> IndexMetadataFields | None:
    """Parse the shared published-index fields from a JSON object."""
    raw_settings = payload.get("settings")
    raw_status = payload.get("status")
    if (
        not isinstance(raw_settings, list)
        or not all(isinstance(item, str) for item in raw_settings)
        or not isinstance(raw_status, str)
        or raw_status not in allowed_statuses
    ):
        return None

    collection = optional_metadata_str(payload.get("collection"))
    indexed_count = _coerce_nonnegative_metadata_int(payload.get("indexed_count"))
    source_signature = optional_metadata_str(payload.get("source_signature"))
    if (require_complete_fields_for_all_statuses or raw_status == "complete") and (
        collection is None or indexed_count is None or source_signature is None
    ):
        return None

    return IndexMetadataFields(
        settings=cast("tuple[str, ...]", tuple(raw_settings)),
        status=raw_status,
        collection=collection,
        last_published_at=optional_metadata_str(payload.get("last_published_at")),
        published_revision=optional_metadata_str(payload.get("published_revision")),
        indexed_count=indexed_count,
        source_signature=source_signature,
    )


def build_index_metadata_payload(
    fields: IndexMetadataFields,
    *,
    extra_fields: Mapping[str, object | None] | None = None,
) -> dict[str, object]:
    """Build a JSON payload with stable published-index field names."""
    payload: dict[str, object] = {
        "settings": list(fields.settings),
        "status": fields.status,
    }
    payload.update(
        {
            key: value
            for key, value in (
                ("collection", fields.collection),
                ("last_published_at", fields.last_published_at),
                ("published_revision", fields.published_revision),
                ("indexed_count", fields.indexed_count),
                ("source_signature", fields.source_signature),
            )
            if value is not None
        },
    )
    if extra_fields is not None:
        payload.update({key: value for key, value in extra_fields.items() if value is not None})
    return payload


def write_index_metadata_payload(metadata_path: Path, payload: Mapping[str, object]) -> None:
    """Atomically persist a published-index JSON payload."""
    metadata_path.parent.mkdir(parents=True, exist_ok=True)
    tmp_path = metadata_path.with_name(f".{metadata_path.name}.{os.getpid()}.{uuid.uuid4().hex}.tmp")
    try:
        tmp_path.write_text(json.dumps(dict(payload), sort_keys=True), encoding="utf-8")
        tmp_path.replace(metadata_path)
    except Exception:
        tmp_path.unlink(missing_ok=True)
        raise
