"""API tests for public and origin-room report publishing."""

from __future__ import annotations

from typing import TYPE_CHECKING
from unittest.mock import AsyncMock

import pytest

from mindroom.api import main
from mindroom.report_access_policy import ReportAccessPolicy
from mindroom.report_publishing.authorization import (
    ReportAuthorizationDecision,
    ReportAuthorizationReason,
)
from mindroom.report_publishing.store import PublishableReport, ReportPublishingStore
from tests.api.conftest import trusted_upstream_headers, use_trusted_upstream_runtime

if TYPE_CHECKING:
    from collections.abc import Generator

    from fastapi.testclient import TestClient


@pytest.fixture(autouse=True)
def _clear_report_authorization_runtime(test_client: TestClient) -> Generator[None, None, None]:
    """Prevent global API runtime callbacks from leaking between route tests."""
    main.unbind_report_authorization_runtime(test_client.app)
    yield
    main.unbind_report_authorization_runtime(test_client.app)


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


def _publish_origin_report(test_client: TestClient, *, static_site: bool = False) -> tuple[str, str]:
    runtime_paths = main._app_runtime_paths(test_client.app)
    if static_site:
        artifact_path = runtime_paths.storage_root / "workspace-fixtures" / "protected-site"
        artifact_path.mkdir(parents=True)
        (artifact_path / "index.html").write_text(
            "<!doctype html><script src='app.js'></script>",
            encoding="utf-8",
        )
        (artifact_path / "app.js").write_text("document.body.dataset.protected = 'true';", encoding="utf-8")
        artifact_kind = "static_site"
    else:
        artifact_path = runtime_paths.storage_root / "reports" / "protected.html"
        artifact_path.parent.mkdir(parents=True, exist_ok=True)
        artifact_path.write_text("<html><body>Protected report</body></html>", encoding="utf-8")
        artifact_kind = "html_file"
    report = ReportPublishingStore(runtime_paths.storage_root).publish_report(
        source=PublishableReport(
            source_type="test_report",
            source={"id": "protected"},
            artifact_path=artifact_path,
            title="Protected Report",
            requested_by="@alice:example.org",
            artifact_kind=artifact_kind,
        ),
        published_by="@alice:example.org",
        access_policy=ReportAccessPolicy.ORIGIN_ROOM,
        origin_room_id="!origin:example.org",
        publisher_entity_name="test_agent",
        publisher_matrix_user_id="@mindroom_test_agent:example.org",
    )
    return report.slug, str(runtime_paths.storage_root)


def _bind_authorization(
    test_client: TestClient,
    reason: ReportAuthorizationReason = ReportAuthorizationReason.AUTHORIZED,
) -> AsyncMock:
    authorize = AsyncMock(return_value=ReportAuthorizationDecision(reason))
    main.bind_report_authorization_runtime(test_client.app, authorize)
    return authorize


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
    ReportPublishingStore(runtime_paths.storage_root).revoke_report(slug, revoked_by="@alice:example.org")

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
    ReportPublishingStore(runtime_paths.storage_root).revoke_report(slug, revoked_by="@alice:example.org")

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


def test_origin_room_report_serves_root_head_and_assets_for_joined_viewer(test_client: TestClient) -> None:
    """Protected root and every asset should use browser identity and room authorization."""
    use_trusted_upstream_runtime(test_client.app)
    slug, _storage_root = _publish_origin_report(test_client, static_site=True)
    authorize = _bind_authorization(test_client)
    headers = trusted_upstream_headers()

    index_response = test_client.get(f"/reports/room/{slug}/", headers=headers)
    head_response = test_client.head(f"/reports/room/{slug}/app.js", headers=headers)
    asset_response = test_client.get(f"/reports/room/{slug}/app.js?cache=1", headers=headers)

    assert index_response.status_code == 200
    assert "sandbox allow-scripts" in index_response.headers["content-security-policy"]
    assert head_response.status_code == 200
    assert head_response.content == b""
    assert asset_response.status_code == 200
    assert "dataset.protected" in asset_response.text
    assert authorize.await_count == 3
    assert all(call.args[1] == "@alice:example.org" for call in authorize.await_args_list)


def test_origin_room_report_redirect_authorizes_before_trailing_slash(test_client: TestClient) -> None:
    """Static-site redirect should not bypass protected authorization."""
    use_trusted_upstream_runtime(test_client.app)
    slug, _storage_root = _publish_origin_report(test_client, static_site=True)
    authorize = _bind_authorization(test_client)

    response = test_client.get(
        f"/reports/room/{slug}",
        headers=trusted_upstream_headers(),
        follow_redirects=False,
    )

    assert response.status_code == 301
    assert response.headers["location"] == f"{slug}/"
    authorize.assert_awaited_once()


def test_origin_room_report_requires_authenticated_browser_principal(test_client: TestClient) -> None:
    """Missing trusted browser identity should return 401 without API-key requirements."""
    slug, _storage_root = _publish_origin_report(test_client)
    authorize = _bind_authorization(test_client)

    response = test_client.get(
        f"/reports/room/{slug}?matrix_user_id=@alice:example.org",
        headers={"X-Trusted-Matrix-User": "@alice:example.org"},
    )

    assert response.status_code == 401
    authorize.assert_not_awaited()


def test_origin_room_report_does_not_accept_dashboard_api_key(test_client: TestClient) -> None:
    """A dashboard API key is not a verified Matrix browser identity."""
    runtime_paths = main._app_runtime_paths(test_client.app)
    api_key_runtime_paths = runtime_paths.__class__(
        config_path=runtime_paths.config_path,
        config_dir=runtime_paths.config_dir,
        env_path=runtime_paths.env_path,
        storage_root=runtime_paths.storage_root,
        process_env={**dict(runtime_paths.process_env), "MINDROOM_API_KEY": "test-key"},
        env_file_values=runtime_paths.env_file_values,
    )
    main.initialize_api_app(test_client.app, api_key_runtime_paths)
    slug, _storage_root = _publish_origin_report(test_client)
    authorize = _bind_authorization(test_client)

    response = test_client.get(
        f"/reports/room/{slug}",
        headers={"Authorization": "Bearer test-key"},
    )

    assert response.status_code == 401
    authorize.assert_not_awaited()


def test_origin_room_report_denies_authenticated_principal_without_matrix_identity(test_client: TestClient) -> None:
    """Authenticated principal without verified Matrix mapping should get uniform 404."""
    use_trusted_upstream_runtime(test_client.app)
    slug, _storage_root = _publish_origin_report(test_client)
    authorize = _bind_authorization(test_client)
    headers = trusted_upstream_headers(matrix_user_id="")

    response = test_client.get(f"/reports/room/{slug}", headers=headers)

    assert response.status_code == 404
    assert response.json()["detail"] == "Report was not found."
    authorize.assert_not_awaited()


def test_origin_room_direct_asset_denial_is_uniform_not_found(test_client: TestClient) -> None:
    """Direct asset requests should not bypass denial or disclose membership state."""
    use_trusted_upstream_runtime(test_client.app)
    slug, storage_root = _publish_origin_report(test_client, static_site=True)
    authorize = _bind_authorization(test_client, ReportAuthorizationReason.VIEWER_NOT_JOINED)

    response = test_client.get(
        f"/reports/room/{slug}/app.js",
        headers=trusted_upstream_headers(),
    )

    assert response.status_code == 404
    assert response.json()["detail"] == "Report was not found."
    assert storage_root not in response.text
    authorize.assert_awaited_once()


def test_origin_room_report_rejects_encoded_asset_traversal(test_client: TestClient) -> None:
    """Encoded traversal must fail after authentication without escaping snapshot root."""
    use_trusted_upstream_runtime(test_client.app)
    slug, storage_root = _publish_origin_report(test_client, static_site=True)
    _bind_authorization(test_client)

    response = test_client.get(
        f"/reports/room/{slug}/%2e%2e%2fconfig.yaml",
        headers=trusted_upstream_headers(),
    )

    assert response.status_code == 404
    assert storage_root not in response.text


def test_origin_room_report_fails_closed_without_runtime_binding(test_client: TestClient) -> None:
    """Configured auth without live Matrix authorization should return 503."""
    use_trusted_upstream_runtime(test_client.app)
    slug, _storage_root = _publish_origin_report(test_client)

    response = test_client.get(f"/reports/room/{slug}", headers=trusted_upstream_headers())

    assert response.status_code == 503
    assert response.json()["detail"] == "Report authorization is temporarily unavailable."


def test_origin_room_report_fails_closed_on_backend_unavailable(test_client: TestClient) -> None:
    """Matrix lookup failure should stay distinct from membership denial."""
    use_trusted_upstream_runtime(test_client.app)
    slug, _storage_root = _publish_origin_report(test_client)
    _bind_authorization(test_client, ReportAuthorizationReason.AUTHORIZATION_BACKEND_UNAVAILABLE)

    response = test_client.get(f"/reports/room/{slug}", headers=trusted_upstream_headers())

    assert response.status_code == 503


def test_report_routes_never_cross_access_policies(test_client: TestClient) -> None:
    """Changing route shape must not reinterpret public or protected records."""
    use_trusted_upstream_runtime(test_client.app)
    public_slug, _storage_root = _publish_public_report(test_client)
    origin_slug, _storage_root = _publish_origin_report(test_client)
    authorize = _bind_authorization(test_client)
    headers = trusted_upstream_headers()

    public_through_room = test_client.get(f"/reports/room/{public_slug}", headers=headers)
    protected_through_public = test_client.get(f"/reports/public/{origin_slug}")

    assert public_through_room.status_code == 404
    assert protected_through_public.status_code == 404
    authorize.assert_not_awaited()


def test_origin_room_revocation_is_immediate_despite_cached_authorization(test_client: TestClient) -> None:
    """Every request should re-read revocation before using membership authority."""
    use_trusted_upstream_runtime(test_client.app)
    slug, _storage_root = _publish_origin_report(test_client, static_site=True)
    authorize = _bind_authorization(test_client)
    headers = trusted_upstream_headers()

    first = test_client.get(f"/reports/room/{slug}/app.js", headers=headers)
    runtime_paths = main._app_runtime_paths(test_client.app)
    ReportPublishingStore(runtime_paths.storage_root).revoke_report(slug, revoked_by="@alice:example.org")
    revoked = test_client.get(f"/reports/room/{slug}/app.js", headers=headers)

    assert first.status_code == 200
    assert revoked.status_code == 404
    authorize.assert_awaited_once()
