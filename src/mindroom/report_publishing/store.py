"""Disk-backed public report publishing store."""

from __future__ import annotations

import json
import re
from dataclasses import dataclass, replace
from datetime import UTC, datetime
from pathlib import Path
from typing import TYPE_CHECKING, cast
from uuid import uuid4

if TYPE_CHECKING:
    from collections.abc import Mapping

_PUBLIC_REPORT_SLUG_RE = re.compile(r"^pub_[a-f0-9]{32}$")
_REQUIRED_PUBLISHED_REPORT_FIELDS = frozenset(
    {
        "slug",
        "source_type",
        "artifact_path",
        "title",
        "requested_by",
        "published_by",
        "published_at",
    },
)


class ReportPublishingError(ValueError):
    """Raised when a public report publishing operation is invalid."""


@dataclass(frozen=True)
class PublishableReport:
    """Authorized report artifact ready to publish."""

    source_type: str
    source: dict[str, object]
    artifact_path: Path
    title: str
    requested_by: str


@dataclass(frozen=True)
class PublishedReport:
    """Persistent public link record for one report artifact."""

    slug: str
    source_type: str
    source: dict[str, object]
    artifact_path: str
    title: str
    requested_by: str
    published_by: str
    published_at: str
    public_url: str | None
    revoked_at: str | None = None
    revoked_by: str | None = None


class ReportPublishingStore:
    """Persist revocable public report links under one storage root."""

    def __init__(self, storage_root: Path) -> None:
        self._storage_root = storage_root
        self._report_publishing_root = storage_root / "report_publishing"

    def publish_report(
        self,
        *,
        source: PublishableReport,
        published_by: str,
        base_url: str | None = None,
    ) -> PublishedReport:
        """Create a public link record for one authorized report artifact."""
        if not source.artifact_path.is_file():
            msg = "Report artifact was not found."
            raise ReportPublishingError(msg)
        slug = f"pub_{uuid4().hex}"
        report = PublishedReport(
            slug=slug,
            source_type=source.source_type,
            source=dict(source.source),
            artifact_path=_relative_artifact_path(source.artifact_path, self._storage_root),
            title=source.title,
            requested_by=source.requested_by,
            published_by=published_by,
            published_at=_utc_now(),
            public_url=_public_report_url(base_url, slug),
        )
        _atomic_write_json(self._public_report_path(slug), _published_report_to_json(report))
        return report

    def get_public_report(self, slug: str, *, include_revoked: bool = False) -> PublishedReport:
        """Load one public report record."""
        report = _published_report_from_json(_load_json_mapping(self._public_report_path(slug)))
        if report.revoked_at is not None and not include_revoked:
            msg = f"Public report '{slug}' was revoked."
            raise ReportPublishingError(msg)
        return report

    def public_report_html_path(self, slug: str) -> Path:
        """Return the public HTML report path for one active public link."""
        report = self.get_public_report(slug)
        report_path = self._artifact_path_from_relative(report.artifact_path)
        if not report_path.is_file():
            msg = f"Public report '{slug}' artifact was not found."
            raise ReportPublishingError(msg)
        return report_path

    def revoke_public_report(self, slug: str, *, revoked_by: str) -> PublishedReport:
        """Revoke one public report link without deleting its underlying artifact."""
        report = self.get_public_report(slug, include_revoked=True)
        if report.revoked_at is not None:
            return report
        revoked = replace(report, revoked_at=_utc_now(), revoked_by=revoked_by)
        _atomic_write_json(self._public_report_path(slug), _published_report_to_json(revoked))
        return revoked

    def _public_report_path(self, slug: str) -> Path:
        _validate_public_report_slug(slug)
        return self._report_publishing_root / "public_reports" / f"{slug}.json"

    def _artifact_path_from_relative(self, artifact_path: str) -> Path:
        relative_path = Path(artifact_path)
        if relative_path.is_absolute() or ".." in relative_path.parts:
            msg = "Published report artifact path is invalid."
            raise ReportPublishingError(msg)
        report_path = self._storage_root / relative_path
        if not report_path.resolve().is_relative_to(self._storage_root.resolve()):
            msg = "Published report artifact path is invalid."
            raise ReportPublishingError(msg)
        return report_path


def _published_report_to_json(report: PublishedReport) -> dict[str, object]:
    return {
        "slug": report.slug,
        "source_type": report.source_type,
        "source": report.source,
        "artifact_path": report.artifact_path,
        "title": report.title,
        "requested_by": report.requested_by,
        "published_by": report.published_by,
        "published_at": report.published_at,
        "public_url": report.public_url,
        "revoked_at": report.revoked_at,
        "revoked_by": report.revoked_by,
    }


def _published_report_from_json(data: dict[str, object]) -> PublishedReport:
    missing_fields = sorted(_REQUIRED_PUBLISHED_REPORT_FIELDS - data.keys())
    if missing_fields:
        msg = f"Published report record is missing field '{missing_fields[0]}'."
        raise ReportPublishingError(msg)
    source = data.get("source", {})
    return PublishedReport(
        slug=str(data["slug"]),
        source_type=str(data["source_type"]),
        source=_object_mapping(cast("Mapping[object, object]", source)) if isinstance(source, dict) else {},
        artifact_path=str(data["artifact_path"]),
        title=str(data["title"]),
        requested_by=str(data["requested_by"]),
        published_by=str(data["published_by"]),
        published_at=str(data["published_at"]),
        public_url=str(data["public_url"]) if data.get("public_url") is not None else None,
        revoked_at=str(data["revoked_at"]) if data.get("revoked_at") is not None else None,
        revoked_by=str(data["revoked_by"]) if data.get("revoked_by") is not None else None,
    )


def _validate_public_report_slug(value: str) -> None:
    if not _PUBLIC_REPORT_SLUG_RE.fullmatch(value):
        msg = f"public report slug must match {_PUBLIC_REPORT_SLUG_RE.pattern}."
        raise ReportPublishingError(msg)


def _public_report_url(base_url: str | None, slug: str) -> str | None:
    if base_url is None or not base_url.strip():
        return None
    return f"{base_url.rstrip('/')}/reports/public/{slug}"


def _relative_artifact_path(artifact_path: Path, storage_root: Path) -> str:
    try:
        return artifact_path.resolve().relative_to(storage_root.resolve()).as_posix()
    except ValueError as exc:
        msg = "Report artifact must live under the MindRoom storage root."
        raise ReportPublishingError(msg) from exc


def _object_mapping(data: Mapping[object, object]) -> dict[str, object]:
    return {str(key): value for key, value in data.items()}


def _utc_now() -> str:
    return datetime.now(UTC).isoformat().replace("+00:00", "Z")


def _load_json_mapping(path: Path) -> dict[str, object]:
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except FileNotFoundError as exc:
        msg = "JSON mapping was not found."
        raise ReportPublishingError(msg) from exc
    except json.JSONDecodeError as exc:
        msg = f"Failed to parse JSON mapping: {exc}"
        raise ReportPublishingError(msg) from exc
    if not isinstance(data, dict):
        msg = "Expected JSON mapping."
        raise ReportPublishingError(msg)
    return data


def _atomic_write_json(path: Path, data: dict[str, object]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp_path = path.with_name(f".{path.name}.{uuid4().hex}.tmp")
    tmp_path.write_text(json.dumps(data, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    tmp_path.replace(path)
