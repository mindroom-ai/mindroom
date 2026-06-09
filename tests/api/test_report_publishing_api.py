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


def _publish_static_site(test_client: TestClient) -> tuple[str, str]:
    runtime_paths = main._app_runtime_paths(test_client.app)
    source_dir = runtime_paths.storage_root / "workspace-fixtures" / "site"
    source_dir.mkdir(parents=True)
    (source_dir / "index.html").write_text(
        "<!doctype html><script src='app.js'></script><img src='image.png'>",
        encoding="utf-8",
    )
    (source_dir / "app.js").write_text("document.body.dataset.ready = 'true';", encoding="utf-8")
    (source_dir / "image.png").write_bytes(b"\x89PNG\r\n\x1a\n")
    store = ReportPublishingStore(runtime_paths.storage_root)
    report = store.publish_report(
        source=PublishableReport(
            source_type="static_site",
            source={"path": "site"},
            artifact_path=source_dir,
            title="Demo Site",
            requested_by="@alice:example.org",
            artifact_kind="static_site",
        ),
        published_by="@alice:example.org",
        base_url="https://mindroom.lab.mindroom.chat",
    )
    return report.slug, str(runtime_paths.storage_root)


def test_public_static_site_serves_index_and_assets_without_dashboard_auth(test_client: TestClient) -> None:
    """Static site public URLs should serve copied index and assets without dashboard credentials."""
    use_trusted_upstream_runtime(test_client.app)
    slug, _storage_root = _publish_static_site(test_client)

    index_response = test_client.get(f"/reports/public/{slug}/")
    script_response = test_client.get(f"/reports/public/{slug}/app.js")
    image_response = test_client.get(f"/reports/public/{slug}/image.png")

    assert index_response.status_code == 200
    assert index_response.headers["content-type"].startswith("text/html")
    assert "sandbox allow-scripts" in index_response.headers["content-security-policy"]
    assert "allow-same-origin" not in index_response.headers["content-security-policy"]
    assert "connect-src 'none'" in index_response.headers["content-security-policy"]
    assert "form-action 'none'" in index_response.headers["content-security-policy"]
    assert script_response.status_code == 200
    assert script_response.headers["content-type"].startswith("text/javascript")
    assert "document.body.dataset.ready" in script_response.text
    assert image_response.status_code == 200
    assert image_response.headers["content-type"].startswith("image/png")


def test_public_static_site_redirects_root_without_trailing_slash(test_client: TestClient) -> None:
    """Static site roots without a trailing slash should redirect so relative assets resolve."""
    use_trusted_upstream_runtime(test_client.app)
    slug, _storage_root = _publish_static_site(test_client)

    response = test_client.get(f"/reports/public/{slug}", follow_redirects=False)

    assert response.status_code == 301
    assert response.headers["location"] == f"{slug}/"


def test_public_static_site_rejects_missing_and_traversal_assets(test_client: TestClient) -> None:
    """Static site asset lookup should fail closed with uniform 404s."""
    runtime_paths = use_trusted_upstream_runtime(test_client.app)
    slug, _storage_root = _publish_static_site(test_client)

    missing_response = test_client.get(f"/reports/public/{slug}/missing.js")
    traversal_response = test_client.get(f"/reports/public/{slug}/../config.yaml")

    assert missing_response.status_code == 404
    assert missing_response.json()["detail"] == "Public report was not found."
    assert str(runtime_paths.storage_root) not in missing_response.text
    assert traversal_response.status_code == 404
    assert traversal_response.json()["detail"] == "Public report was not found."
    assert str(runtime_paths.storage_root) not in traversal_response.text


def test_public_static_site_returns_404_after_revocation(test_client: TestClient) -> None:
    """Revoking a static site should disable the index and every asset."""
    slug, storage_root = _publish_static_site(test_client)
    runtime_paths = main._app_runtime_paths(test_client.app)
    ReportPublishingStore(runtime_paths.storage_root).revoke_public_report(slug, revoked_by="@alice:example.org")

    index_response = test_client.get(f"/reports/public/{slug}/")
    script_response = test_client.get(f"/reports/public/{slug}/app.js")

    assert index_response.status_code == 404
    assert index_response.json()["detail"] == "Public report was not found."
    assert script_response.status_code == 404
    assert script_response.json()["detail"] == "Public report was not found."
    assert storage_root not in index_response.text
    assert storage_root not in script_response.text


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
