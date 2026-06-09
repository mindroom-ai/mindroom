"""API tests for public report publishing."""

from __future__ import annotations

from typing import TYPE_CHECKING

from mindroom.api import main
from mindroom.report_publishing.store import PublishableReport, ReportPublishingStore
from tests.api.conftest import use_trusted_upstream_runtime

if TYPE_CHECKING:
    from fastapi.testclient import TestClient


def _publish_public_report(test_client: TestClient) -> tuple[str, str]:
    runtime_paths = main._app_runtime_paths(test_client.app)
    report_path = runtime_paths.storage_root / "reports" / "example.html"
    report_path.parent.mkdir(parents=True)
    report_path.write_text("<html><body>Published report</body></html>", encoding="utf-8")
    store = ReportPublishingStore(runtime_paths.storage_root)
    report = store.publish_report(
        source=PublishableReport(
            source_type="test_report",
            source={"id": "example"},
            artifact_path=report_path,
            title="Example Report",
            requested_by="@alice:example.org",
        ),
        published_by="@alice:example.org",
        base_url="https://acme.mindroom.chat",
    )
    return report.slug, str(runtime_paths.storage_root)


def test_public_report_served_without_dashboard_auth(test_client: TestClient) -> None:
    """Public report URLs should serve published reports without dashboard credentials."""
    use_trusted_upstream_runtime(test_client.app)
    slug, _storage_root = _publish_public_report(test_client)

    response = test_client.get(f"/reports/public/{slug}")

    assert response.status_code == 200
    assert response.headers["content-type"].startswith("text/html")
    assert response.headers["content-security-policy"] == (
        "default-src 'none'; "
        "img-src 'self' data: https:; "
        "style-src 'unsafe-inline'; "
        "font-src 'self' data:; "
        "base-uri 'none'; "
        "frame-ancestors 'self'"
    )
    assert response.headers["cache-control"] == "no-store, max-age=0"
    assert response.headers["pragma"] == "no-cache"
    assert response.headers["expires"] == "0"
    assert "Published report" in response.text


def test_public_report_returns_404_after_revocation(test_client: TestClient) -> None:
    """Revoked public report URLs should stop serving the underlying artifact."""
    slug, storage_root = _publish_public_report(test_client)
    runtime_paths = main._app_runtime_paths(test_client.app)
    ReportPublishingStore(runtime_paths.storage_root).revoke_public_report(slug, revoked_by="@alice:example.org")

    response = test_client.get(f"/reports/public/{slug}")

    assert response.status_code == 404
    assert response.json()["detail"] == "Public report was not found."
    assert storage_root not in response.text


def test_public_report_returns_safe_404_for_invalid_slug(test_client: TestClient) -> None:
    """Invalid public report slugs should be indistinguishable from missing reports."""
    runtime_paths = use_trusted_upstream_runtime(test_client.app)

    response = test_client.get("/reports/public/not-a-slug")

    assert response.status_code == 404
    assert response.json()["detail"] == "Public report was not found."
    assert str(runtime_paths.storage_root) not in response.text


def test_public_report_returns_safe_404_for_corrupt_record(test_client: TestClient) -> None:
    """Corrupt public report records should not leak raw parser failures through the API."""
    runtime_paths = use_trusted_upstream_runtime(test_client.app)
    slug = "pub_" + ("a" * 32)
    report_path = runtime_paths.storage_root / "report_publishing" / "public_reports" / f"{slug}.json"
    report_path.parent.mkdir(parents=True)
    report_path.write_text('{"slug": "' + slug + '"}', encoding="utf-8")

    response = test_client.get(f"/reports/public/{slug}")

    assert response.status_code == 404
    assert response.json()["detail"] == "Public report was not found."
    assert str(runtime_paths.storage_root) not in response.text
