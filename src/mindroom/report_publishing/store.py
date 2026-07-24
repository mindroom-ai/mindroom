"""Disk-backed report publishing store."""

from __future__ import annotations

import json
import re
from dataclasses import dataclass, replace
from datetime import UTC, datetime
from pathlib import Path
from typing import TYPE_CHECKING, cast
from uuid import uuid4

from mindroom.durable_write import write_json_file_durable
from mindroom.matrix.identity import try_parse_historical_matrix_user_id, try_parse_matrix_room_id
from mindroom.report_access_policy import ReportAccessPolicy
from mindroom.report_publishing.static_site import (
    StaticSiteSnapshotError,
    resolve_static_site_asset,
    snapshot_static_site,
)

if TYPE_CHECKING:
    from collections.abc import Mapping

_PUBLIC_REPORT_SLUG_RE = re.compile(r"^pub_[a-f0-9]{32}$")
_ARTIFACT_KIND_HTML_FILE = "html_file"
ARTIFACT_KIND_STATIC_SITE = "static_site"
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
    artifact_kind: str = _ARTIFACT_KIND_HTML_FILE


@dataclass(frozen=True)
class PublishedReport:
    """Persistent publication record for one report artifact."""

    slug: str
    source_type: str
    source: dict[str, object]
    artifact_kind: str
    artifact_path: str
    title: str
    requested_by: str
    published_by: str
    published_at: str
    public_url: str | None
    access_policy: ReportAccessPolicy = ReportAccessPolicy.PUBLIC
    origin_room_id: str | None = None
    publisher_entity_name: str | None = None
    publisher_matrix_user_id: str | None = None
    revoked_at: str | None = None
    revoked_by: str | None = None

    @property
    def is_static_site(self) -> bool:
        """Return whether this report serves a copied static site."""
        return self.artifact_kind == ARTIFACT_KIND_STATIC_SITE


class ReportPublishingStore:
    """Persist revocable report publications under one storage root."""

    def __init__(self, storage_root: Path) -> None:
        self._storage_root = storage_root
        self._report_publishing_root = storage_root / "report_publishing"

    def publish_report(
        self,
        *,
        source: PublishableReport,
        published_by: str,
        base_url: str | None = None,
        access_policy: ReportAccessPolicy = ReportAccessPolicy.PUBLIC,
        origin_room_id: str | None = None,
        publisher_entity_name: str | None = None,
        publisher_matrix_user_id: str | None = None,
    ) -> PublishedReport:
        """Create a publication record for one authorized report artifact."""
        _validate_publication_metadata(
            access_policy=access_policy,
            origin_room_id=origin_room_id,
            publisher_entity_name=publisher_entity_name,
            publisher_matrix_user_id=publisher_matrix_user_id,
        )
        slug = f"pub_{uuid4().hex}"
        artifact_path = self._publish_artifact(source, slug)
        report = PublishedReport(
            slug=slug,
            source_type=source.source_type,
            source=dict(source.source),
            artifact_kind=source.artifact_kind,
            artifact_path=artifact_path,
            title=source.title,
            requested_by=source.requested_by,
            published_by=published_by,
            published_at=_utc_now(),
            public_url=_report_url(
                base_url,
                slug,
                artifact_kind=source.artifact_kind,
                access_policy=access_policy,
            ),
            access_policy=access_policy,
            origin_room_id=origin_room_id,
            publisher_entity_name=publisher_entity_name,
            publisher_matrix_user_id=publisher_matrix_user_id,
        )
        report_path = self._public_report_path(slug)
        payload = _published_report_to_json(report)
        write_json_file_durable(report_path, payload, indent=2, sort_keys=True, trailing_newline=True)
        return report

    def get_report(self, slug: str, *, include_revoked: bool = False) -> PublishedReport:
        """Load one report publication."""
        report = _published_report_from_json(_load_json_mapping(self._public_report_path(slug)))
        if report.slug != slug:
            msg = "Published report record slug does not match its storage key."
            raise ReportPublishingError(msg)
        if report.revoked_at is not None and not include_revoked:
            msg = f"Published report '{slug}' was revoked."
            raise ReportPublishingError(msg)
        return report

    def report_asset_path(self, report: PublishedReport, asset_path: str | None = None) -> Path:
        """Return one served file or static-site asset path for a loaded public report."""
        if report.is_static_site:
            site_root = self._artifact_path_from_relative(report.artifact_path)
            try:
                return resolve_static_site_asset(site_root, asset_path)
            except StaticSiteSnapshotError as exc:
                raise ReportPublishingError(str(exc)) from exc
        if report.artifact_kind != _ARTIFACT_KIND_HTML_FILE:
            msg = f"Public report '{report.slug}' artifact kind is invalid."
            raise ReportPublishingError(msg)
        if asset_path not in (None, ""):
            msg = f"Public report '{report.slug}' does not contain static assets."
            raise ReportPublishingError(msg)
        report_path = self._artifact_path_from_relative(report.artifact_path)
        if not report_path.is_file():
            msg = f"Public report '{report.slug}' artifact was not found."
            raise ReportPublishingError(msg)
        return report_path

    def revoke_report(self, slug: str, *, revoked_by: str) -> PublishedReport:
        """Revoke one report without deleting its underlying artifact."""
        report = self.get_report(slug, include_revoked=True)
        if report.revoked_at is not None:
            return report
        revoked = replace(report, revoked_at=_utc_now(), revoked_by=revoked_by)
        report_path = self._public_report_path(slug)
        payload = _published_report_to_json(revoked)
        write_json_file_durable(report_path, payload, indent=2, sort_keys=True, trailing_newline=True)
        return revoked

    def _publish_artifact(self, source: PublishableReport, slug: str) -> str:
        if source.artifact_kind == _ARTIFACT_KIND_HTML_FILE:
            if not source.artifact_path.is_file():
                msg = "Report artifact was not found."
                raise ReportPublishingError(msg)
            return _relative_artifact_path(source.artifact_path, self._storage_root)
        if source.artifact_kind == ARTIFACT_KIND_STATIC_SITE:
            destination_dir = self._report_publishing_root / "artifacts" / slug
            try:
                snapshot_static_site(source.artifact_path, destination_dir)
            except (OSError, StaticSiteSnapshotError) as exc:
                raise ReportPublishingError(str(exc)) from exc
            return _relative_artifact_path(destination_dir, self._storage_root)
        msg = f"Unsupported report artifact_kind '{source.artifact_kind}'."
        raise ReportPublishingError(msg)

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
        "artifact_kind": report.artifact_kind,
        "artifact_path": report.artifact_path,
        "title": report.title,
        "requested_by": report.requested_by,
        "published_by": report.published_by,
        "published_at": report.published_at,
        "public_url": report.public_url,
        "access_policy": report.access_policy.value,
        "origin_room_id": report.origin_room_id,
        "publisher_entity_name": report.publisher_entity_name,
        "publisher_matrix_user_id": report.publisher_matrix_user_id,
        "revoked_at": report.revoked_at,
        "revoked_by": report.revoked_by,
    }


def _published_report_from_json(data: dict[str, object]) -> PublishedReport:
    missing_fields = sorted(_REQUIRED_PUBLISHED_REPORT_FIELDS - data.keys())
    if missing_fields:
        msg = f"Published report record is missing field '{missing_fields[0]}'."
        raise ReportPublishingError(msg)
    source = data.get("source", {})
    if not isinstance(source, dict):
        msg = "Published report record field 'source' must be an object."
        raise ReportPublishingError(msg)
    raw_access_policy = data.get("access_policy", ReportAccessPolicy.PUBLIC.value)
    try:
        access_policy = ReportAccessPolicy(raw_access_policy)
    except (TypeError, ValueError) as exc:
        msg = f"Published report record has unsupported access policy '{raw_access_policy}'."
        raise ReportPublishingError(msg) from exc
    report = PublishedReport(
        slug=_required_text(data, "slug"),
        source_type=_required_text(data, "source_type"),
        source=_object_mapping(cast("Mapping[object, object]", source)),
        artifact_kind=_optional_text(data, "artifact_kind") or _ARTIFACT_KIND_HTML_FILE,
        artifact_path=_required_text(data, "artifact_path"),
        title=_required_text(data, "title"),
        requested_by=_required_text(data, "requested_by"),
        published_by=_required_text(data, "published_by"),
        published_at=_required_text(data, "published_at"),
        public_url=_optional_text(data, "public_url"),
        access_policy=access_policy,
        origin_room_id=_optional_text(data, "origin_room_id"),
        publisher_entity_name=_optional_text(data, "publisher_entity_name"),
        publisher_matrix_user_id=_optional_text(data, "publisher_matrix_user_id"),
        revoked_at=_optional_text(data, "revoked_at"),
        revoked_by=_optional_text(data, "revoked_by"),
    )
    _validate_public_report_slug(report.slug)
    _validate_publication_metadata(
        access_policy=report.access_policy,
        origin_room_id=report.origin_room_id,
        publisher_entity_name=report.publisher_entity_name,
        publisher_matrix_user_id=report.publisher_matrix_user_id,
        allow_public_incidental_metadata=True,
    )
    return report


def _validate_public_report_slug(value: str) -> None:
    if not _PUBLIC_REPORT_SLUG_RE.fullmatch(value):
        msg = f"public report slug must match {_PUBLIC_REPORT_SLUG_RE.pattern}."
        raise ReportPublishingError(msg)


def _report_url(
    base_url: str | None,
    slug: str,
    *,
    artifact_kind: str,
    access_policy: ReportAccessPolicy,
) -> str | None:
    if base_url is None or not base_url.strip():
        return None
    route_path = report_route_path(
        slug,
        access_policy=access_policy,
        trailing_slash=artifact_kind == ARTIFACT_KIND_STATIC_SITE,
    )
    return f"{base_url.rstrip('/')}{route_path}"


def report_route_path(
    slug: str,
    *,
    access_policy: ReportAccessPolicy,
    trailing_slash: bool,
) -> str:
    """Return the browser route for one published report."""
    route = "public" if access_policy is ReportAccessPolicy.PUBLIC else "room"
    suffix = "/" if trailing_slash else ""
    return f"/reports/{route}/{slug}{suffix}"


def _validate_publication_metadata(
    *,
    access_policy: ReportAccessPolicy,
    origin_room_id: str | None,
    publisher_entity_name: str | None,
    publisher_matrix_user_id: str | None,
    allow_public_incidental_metadata: bool = False,
) -> None:
    metadata = (origin_room_id, publisher_entity_name, publisher_matrix_user_id)
    if access_policy is ReportAccessPolicy.PUBLIC:
        if not allow_public_incidental_metadata and any(value is not None for value in metadata):
            msg = "Public report records must not contain origin-room authorization metadata."
            raise ReportPublishingError(msg)
        return
    if any(value is None or not value.strip() for value in metadata):
        msg = "Origin-room report records require room and publisher identity metadata."
        raise ReportPublishingError(msg)
    assert origin_room_id is not None
    assert publisher_entity_name is not None
    assert publisher_matrix_user_id is not None
    if not _valid_matrix_room_id(origin_room_id):
        msg = "Origin-room report record contains an invalid Matrix room ID."
        raise ReportPublishingError(msg)
    if not _valid_matrix_user_id(publisher_matrix_user_id):
        msg = "Origin-room report record contains an invalid publisher Matrix user ID."
        raise ReportPublishingError(msg)


def _valid_matrix_room_id(value: str) -> bool:
    return try_parse_matrix_room_id(value) == value


def _valid_matrix_user_id(value: str) -> bool:
    return try_parse_historical_matrix_user_id(value) == value


def _required_text(data: dict[str, object], key: str) -> str:
    value = data.get(key)
    if not isinstance(value, str) or not value:
        msg = f"Published report record field '{key}' must be a non-empty string."
        raise ReportPublishingError(msg)
    return value


def _optional_text(data: dict[str, object], key: str) -> str | None:
    value = data.get(key)
    if value is None:
        return None
    if not isinstance(value, str) or not value:
        msg = f"Published report record field '{key}' must be a non-empty string or null."
        raise ReportPublishingError(msg)
    return value


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
