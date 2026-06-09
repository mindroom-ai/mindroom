# Static Site Report Publishing Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task.
> Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Let an agent publish a copied static-site directory with `index.html`, assets, and JavaScript through the existing revocable public report link system.

**Architecture:** Add `source_type="static_site"` to `report_publishing.publish_report`.
The tool resolves a workspace-relative directory, snapshots it into `MINDROOM_STORAGE_PATH/report_publishing/artifacts/<slug>/`, and records the copied directory as a public report artifact.
The public API serves `/reports/public/<slug>/` and `/reports/public/<slug>/<asset_path>` with a same-domain sandbox CSP that allows scripts but prevents same-origin dashboard/API privileges.

**Tech Stack:** Python 3.13, FastAPI `FileResponse`, `pathlib`, `shutil`, pytest, existing MindRoom tool runtime context, existing `ReportPublishingStore`.

---

## File Structure

- Create `src/mindroom/report_publishing/static_site.py`.
  This module owns static-site snapshot validation, copy logic, asset path validation, limits, and MIME-safe path resolution.
- Modify `src/mindroom/report_publishing/store.py`.
  Add artifact kind tracking and public asset resolution while preserving current single HTML report behavior for `dynamic_workflow_run`.
- Modify `src/mindroom/api/report_publishing.py`.
  Add the nested asset route and route both `/reports/public/{slug}` and `/reports/public/{slug}/{asset_path:path}` through the same store lookup.
- Modify `src/mindroom/api/report_headers.py`.
  Add a sandboxed static-site CSP that allows JavaScript while blocking network connections, forms, object embeds, and same-origin privilege.
- Modify `src/mindroom/custom_tools/report_publishing.py`.
  Add the `static_site` source resolver and update tool schema description.
- Modify `tests/test_report_publishing.py`.
  Cover store snapshot behavior, symlink rejection, size/file limits, and tool source resolution from the agent workspace.
- Modify `tests/api/test_report_publishing_api.py`.
  Cover serving `index.html`, JS assets, missing assets, traversal attempts, revocation, and headers.
- Modify `docs/tools/agent-orchestration.md`.
  Document `source_type="static_site"`, same-domain sandboxed JS behavior, and proxy requirements.
- Regenerate `skills/mindroom-docs/references/llms-full.txt` and `skills/mindroom-docs/references/page__tools__agent-orchestration__index.md` through pre-commit.

---

## Behavioral Contract

`publish_report(source_type="static_site", source={"path": "relative/site-dir", "title": "Demo"}, confirm_public=True)` publishes a copied snapshot of one workspace-relative directory.
The source directory must contain `index.html`.
The source path must resolve under `ToolRuntimeContext.storage_path`.
The snapshot must reject symlinks, absolute paths, `..`, too many files, and too much total data.
The published URL is `/reports/public/<slug>/`.
Revoking the slug disables the whole static site.
JavaScript is allowed only inside a CSP sandbox without `allow-same-origin`.
The sandbox blocks `fetch`, forms, object embeds, and MindRoom dashboard/API same-origin privilege.

---

### Task 1: Add Failing Store Tests For Static Site Snapshots

**Files:**
- Modify: `tests/test_report_publishing.py`

- [ ] **Step 1: Add store tests for static-site publishing**

Insert these tests after `test_report_publishing_store_rejects_serve_time_symlink_escape`.

```python
def test_report_publishing_store_creates_static_site_snapshot(tmp_path: Path) -> None:
    """Static sites should be copied into report publishing storage before serving."""
    storage_root = tmp_path / "mindroom_data"
    source_dir = tmp_path / "workspace" / "site"
    source_dir.mkdir(parents=True)
    (source_dir / "index.html").write_text(
        "<!doctype html><script src='app.js'></script><img src='image.png'>",
        encoding="utf-8",
    )
    (source_dir / "app.js").write_text("document.body.dataset.ready = 'true';", encoding="utf-8")
    (source_dir / "image.png").write_bytes(b"\x89PNG\r\n\x1a\n")
    store = ReportPublishingStore(storage_root)

    report = store.publish_report(
        source=PublishableReport(
            source_type="static_site",
            source={"path": "site"},
            artifact_path=source_dir,
            title="Demo Site",
            requested_by="@alice:localhost",
            artifact_kind="static_site",
        ),
        published_by="@alice:localhost",
        base_url="https://mindroom.lab.mindroom.chat",
    )
    (source_dir / "index.html").write_text("<!doctype html>changed", encoding="utf-8")

    index_path = store.public_report_asset_path(report.slug)
    script_path = store.public_report_asset_path(report.slug, "app.js")

    assert report.artifact_kind == "static_site"
    assert report.public_url == f"https://mindroom.lab.mindroom.chat/reports/public/{report.slug}/"
    assert index_path.read_text(encoding="utf-8").startswith("<!doctype html><script")
    assert script_path.read_text(encoding="utf-8") == "document.body.dataset.ready = 'true';"
    assert index_path.parent.is_relative_to(storage_root / "report_publishing" / "artifacts")


def test_report_publishing_store_rejects_static_site_without_index(tmp_path: Path) -> None:
    """Static site publishing should require index.html."""
    storage_root = tmp_path / "mindroom_data"
    source_dir = tmp_path / "workspace" / "site"
    source_dir.mkdir(parents=True)
    (source_dir / "app.js").write_text("console.log('missing index')", encoding="utf-8")
    store = ReportPublishingStore(storage_root)

    with pytest.raises(ReportPublishingError, match="index.html"):
        store.publish_report(
            source=PublishableReport(
                source_type="static_site",
                source={"path": "site"},
                artifact_path=source_dir,
                title="Broken Site",
                requested_by="@alice:localhost",
                artifact_kind="static_site",
            ),
            published_by="@alice:localhost",
            base_url="https://mindroom.lab.mindroom.chat",
        )


def test_report_publishing_store_rejects_static_site_symlink(tmp_path: Path) -> None:
    """Static site snapshots should reject symlinks instead of copying or following them."""
    storage_root = tmp_path / "mindroom_data"
    source_dir = tmp_path / "workspace" / "site"
    outside_file = tmp_path / "secret.txt"
    source_dir.mkdir(parents=True)
    (source_dir / "index.html").write_text("<!doctype html>Site", encoding="utf-8")
    outside_file.write_text("secret", encoding="utf-8")
    (source_dir / "secret.txt").symlink_to(outside_file)
    store = ReportPublishingStore(storage_root)

    with pytest.raises(ReportPublishingError, match="symlink"):
        store.publish_report(
            source=PublishableReport(
                source_type="static_site",
                source={"path": "site"},
                artifact_path=source_dir,
                title="Unsafe Site",
                requested_by="@alice:localhost",
                artifact_kind="static_site",
            ),
            published_by="@alice:localhost",
            base_url="https://mindroom.lab.mindroom.chat",
        )


def test_report_publishing_store_rejects_static_site_asset_traversal(tmp_path: Path) -> None:
    """Static site asset lookup should reject traversal paths."""
    storage_root = tmp_path / "mindroom_data"
    source_dir = tmp_path / "workspace" / "site"
    source_dir.mkdir(parents=True)
    (source_dir / "index.html").write_text("<!doctype html>Site", encoding="utf-8")
    store = ReportPublishingStore(storage_root)
    report = store.publish_report(
        source=PublishableReport(
            source_type="static_site",
            source={"path": "site"},
            artifact_path=source_dir,
            title="Demo Site",
            requested_by="@alice:localhost",
            artifact_kind="static_site",
        ),
        published_by="@alice:localhost",
        base_url="https://mindroom.lab.mindroom.chat",
    )

    with pytest.raises(ReportPublishingError, match="asset path is invalid"):
        store.public_report_asset_path(report.slug, "../index.html")
```

- [ ] **Step 2: Run store tests and verify RED**

Run:

```bash
uv run pytest tests/test_report_publishing.py -q --no-cov
```

Expected: FAIL because `PublishableReport` does not accept `artifact_kind`, and `ReportPublishingStore` does not have `public_report_asset_path`.

- [ ] **Step 3: Commit nothing**

Leave the failing tests unstaged until Task 2 makes them pass.

---

### Task 2: Implement Static Site Snapshot Storage

**Files:**
- Create: `src/mindroom/report_publishing/static_site.py`
- Modify: `src/mindroom/report_publishing/store.py`
- Test: `tests/test_report_publishing.py`

- [ ] **Step 1: Create static-site snapshot helpers**

Create `src/mindroom/report_publishing/static_site.py` with this code.

```python
"""Static-site snapshot helpers for public report publishing."""

from __future__ import annotations

import shutil
from dataclasses import dataclass
from pathlib import Path

STATIC_SITE_MAX_FILES = 200
STATIC_SITE_MAX_BYTES = 10 * 1024 * 1024


class StaticSiteSnapshotError(ValueError):
    """Raised when a static-site snapshot is invalid."""


@dataclass(frozen=True)
class StaticSiteSnapshot:
    """Result of copying one static site into report publishing storage."""

    root: Path
    file_count: int
    total_bytes: int


def snapshot_static_site(source_dir: Path, destination_dir: Path) -> StaticSiteSnapshot:
    """Copy one static-site directory without symlinks or path escapes."""
    resolved_source = source_dir.resolve()
    if not resolved_source.is_dir():
        msg = "Static site source path must be a directory."
        raise StaticSiteSnapshotError(msg)
    if not (resolved_source / "index.html").is_file():
        msg = "Static site source must contain index.html."
        raise StaticSiteSnapshotError(msg)

    entries = _static_site_entries(resolved_source)
    file_count = sum(1 for source_path, _relative_path in entries if source_path.is_file())
    total_bytes = sum(source_path.stat().st_size for source_path, _relative_path in entries if source_path.is_file())
    if file_count > STATIC_SITE_MAX_FILES:
        msg = f"Static site contains too many files: {file_count} > {STATIC_SITE_MAX_FILES}."
        raise StaticSiteSnapshotError(msg)
    if total_bytes > STATIC_SITE_MAX_BYTES:
        msg = f"Static site is too large: {total_bytes} > {STATIC_SITE_MAX_BYTES} bytes."
        raise StaticSiteSnapshotError(msg)

    destination_dir.mkdir(parents=True, exist_ok=False)
    for source_path, relative_path in entries:
        target_path = destination_dir / relative_path
        if source_path.is_dir():
            target_path.mkdir(parents=True, exist_ok=True)
            continue
        target_path.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(source_path, target_path)
    return StaticSiteSnapshot(root=destination_dir, file_count=file_count, total_bytes=total_bytes)


def resolve_static_site_asset(site_root: Path, asset_path: str | None) -> Path:
    """Resolve one static-site asset path under a copied site root."""
    relative_asset = Path(asset_path.strip("/") if asset_path else "index.html")
    if relative_asset == Path() or relative_asset.is_absolute() or ".." in relative_asset.parts:
        msg = "Published report asset path is invalid."
        raise StaticSiteSnapshotError(msg)
    resolved_root = site_root.resolve()
    resolved_asset = (site_root / relative_asset).resolve()
    if not resolved_asset.is_relative_to(resolved_root):
        msg = "Published report asset path is invalid."
        raise StaticSiteSnapshotError(msg)
    if not resolved_asset.is_file():
        msg = "Published report asset was not found."
        raise StaticSiteSnapshotError(msg)
    return resolved_asset


def _static_site_entries(source_dir: Path) -> list[tuple[Path, Path]]:
    entries: list[tuple[Path, Path]] = []
    for source_path in sorted(source_dir.rglob("*")):
        if source_path.is_symlink():
            msg = f"Static site source must not contain symlinks: {source_path}"
            raise StaticSiteSnapshotError(msg)
        relative_path = source_path.relative_to(source_dir)
        if relative_path.is_absolute() or ".." in relative_path.parts:
            msg = "Static site source path is invalid."
            raise StaticSiteSnapshotError(msg)
        entries.append((source_path, relative_path))
    return entries
```

- [ ] **Step 2: Extend report dataclasses and store methods**

Modify `src/mindroom/report_publishing/store.py`.

Add imports:

```python
from mindroom.report_publishing.static_site import (
    StaticSiteSnapshotError,
    resolve_static_site_asset,
    snapshot_static_site,
)
```

Add constants near `_REQUIRED_PUBLISHED_REPORT_FIELDS`:

```python
_ARTIFACT_KIND_HTML_FILE = "html_file"
_ARTIFACT_KIND_STATIC_SITE = "static_site"
```

Add `artifact_kind` to `PublishableReport` with a default:

```python
    artifact_kind: str = _ARTIFACT_KIND_HTML_FILE
```

Add `artifact_kind` to `PublishedReport` before `artifact_path`:

```python
    artifact_kind: str
```

Replace the start of `publish_report` with:

```python
        slug = f"pub_{uuid4().hex}"
        artifact_path = self._publish_artifact(source, slug)
        public_url = _public_report_url(base_url, slug, artifact_kind=source.artifact_kind)
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
            public_url=public_url,
        )
```

Add these methods to `ReportPublishingStore`:

```python
    def public_report_asset_path(self, slug: str, asset_path: str | None = None) -> Path:
        """Return one active public report file or static-site asset path."""
        report = self.get_public_report(slug)
        if report.artifact_kind == _ARTIFACT_KIND_HTML_FILE:
            if asset_path not in (None, ""):
                msg = f"Public report '{slug}' does not contain static assets."
                raise ReportPublishingError(msg)
            return self.public_report_html_path(slug)
        if report.artifact_kind == _ARTIFACT_KIND_STATIC_SITE:
            site_root = self._artifact_path_from_relative(report.artifact_path)
            try:
                return resolve_static_site_asset(site_root, asset_path)
            except StaticSiteSnapshotError as exc:
                raise ReportPublishingError(str(exc)) from exc
        msg = f"Public report '{slug}' artifact kind is invalid."
        raise ReportPublishingError(msg)

    def _publish_artifact(self, source: PublishableReport, slug: str) -> str:
        if source.artifact_kind == _ARTIFACT_KIND_HTML_FILE:
            if not source.artifact_path.is_file():
                msg = "Report artifact was not found."
                raise ReportPublishingError(msg)
            return _relative_artifact_path(source.artifact_path, self._storage_root)
        if source.artifact_kind == _ARTIFACT_KIND_STATIC_SITE:
            destination_dir = self._report_publishing_root / "artifacts" / slug
            try:
                snapshot_static_site(source.artifact_path, destination_dir)
            except StaticSiteSnapshotError as exc:
                raise ReportPublishingError(str(exc)) from exc
            return _relative_artifact_path(destination_dir, self._storage_root)
        msg = f"Unsupported report artifact_kind '{source.artifact_kind}'."
        raise ReportPublishingError(msg)
```

Update `public_report_html_path` to call `public_report_asset_path` only for HTML files, or keep its current implementation and use the new method from the API.

Update `_published_report_to_json`:

```python
        "artifact_kind": report.artifact_kind,
```

Update `_published_report_from_json`:

```python
        artifact_kind=str(data.get("artifact_kind", _ARTIFACT_KIND_HTML_FILE)),
```

Change `_public_report_url` to include a trailing slash for static sites:

```python
def _public_report_url(base_url: str | None, slug: str, *, artifact_kind: str) -> str | None:
    if base_url is None or not base_url.strip():
        return None
    suffix = f"/reports/public/{slug}/" if artifact_kind == _ARTIFACT_KIND_STATIC_SITE else f"/reports/public/{slug}"
    return f"{base_url.rstrip('/')}{suffix}"
```

- [ ] **Step 3: Run store tests and verify GREEN**

Run:

```bash
uv run pytest tests/test_report_publishing.py -q --no-cov
```

Expected: PASS for the new static-site store tests and existing dynamic workflow publish tests.

- [ ] **Step 4: Commit Task 1 and Task 2**

Run:

```bash
git add src/mindroom/report_publishing/static_site.py src/mindroom/report_publishing/store.py tests/test_report_publishing.py
git commit -m "Add static site report snapshots"
```

---

### Task 3: Add Failing API Tests For Static Site Serving

**Files:**
- Modify: `tests/api/test_report_publishing_api.py`

- [ ] **Step 1: Add API tests for static-site routes and sandbox headers**

Add these helper functions and tests after `_publish_public_report`.

```python
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
```

- [ ] **Step 2: Run API tests and verify RED**

Run:

```bash
uv run pytest tests/api/test_report_publishing_api.py -q --no-cov
```

Expected: FAIL because nested asset routes and static-site CSP are not wired yet.

---

### Task 4: Implement Static Site API Routes And Headers

**Files:**
- Modify: `src/mindroom/api/report_publishing.py`
- Modify: `src/mindroom/api/report_headers.py`
- Test: `tests/api/test_report_publishing_api.py`

- [ ] **Step 1: Add report header modes**

Replace the header constants in `src/mindroom/api/report_headers.py` with:

```python
_REPORT_CSP = (
    "default-src 'none'; "
    "img-src 'self' data: https:; "
    "style-src 'unsafe-inline'; "
    "font-src 'self' data:; "
    "base-uri 'none'; "
    "frame-ancestors 'self'"
)
_STATIC_SITE_CSP = (
    "sandbox allow-scripts; "
    "default-src 'none'; "
    "script-src 'self' 'unsafe-inline'; "
    "img-src 'self' data: https:; "
    "style-src 'self' 'unsafe-inline'; "
    "font-src 'self' data:; "
    "connect-src 'none'; "
    "form-action 'none'; "
    "object-src 'none'; "
    "base-uri 'none'; "
    "frame-ancestors 'self'"
)
```

Change `set_report_headers` to:

```python
def set_report_headers(response: FileResponse, *, cache_control: str, sandboxed_static_site: bool = False) -> None:
    """Set browser headers for rendered report artifacts."""
    response.headers["Content-Security-Policy"] = _STATIC_SITE_CSP if sandboxed_static_site else _REPORT_CSP
    response.headers["Cache-Control"] = cache_control
    response.headers["Pragma"] = "no-cache"
    response.headers["Expires"] = "0"
```

- [ ] **Step 2: Add nested static-site routes**

Replace `src/mindroom/api/report_publishing.py` route logic with:

```python
@public_router.get("/reports/public/{slug}", include_in_schema=False)
async def public_report(request: Request, slug: str) -> FileResponse:
    """Serve one active public HTML report from runtime storage."""
    return await _public_report_asset(request, slug, None)


@public_router.get("/reports/public/{slug}/", include_in_schema=False)
async def public_report_index(request: Request, slug: str) -> FileResponse:
    """Serve one active public static-site index from runtime storage."""
    return await _public_report_asset(request, slug, None)


@public_router.get("/reports/public/{slug}/{asset_path:path}", include_in_schema=False)
async def public_report_asset(request: Request, slug: str, asset_path: str) -> FileResponse:
    """Serve one active public static-site asset from runtime storage."""
    return await _public_report_asset(request, slug, asset_path)


async def _public_report_asset(request: Request, slug: str, asset_path: str | None) -> FileResponse:
    runtime_paths = api_runtime_paths(request)
    store = ReportPublishingStore(runtime_paths.storage_root)
    try:
        report = store.get_public_report(slug)
        report_path = store.public_report_asset_path(slug, asset_path)
    except ReportPublishingError as exc:
        raise HTTPException(status_code=404, detail="Public report was not found.") from exc

    response = FileResponse(report_path)
    set_report_headers(
        response,
        cache_control="no-store, max-age=0",
        sandboxed_static_site=report.artifact_kind == "static_site",
    )
    return response
```

- [ ] **Step 3: Run API tests and verify GREEN**

Run:

```bash
uv run pytest tests/api/test_report_publishing_api.py -q --no-cov
```

Expected: PASS.

- [ ] **Step 4: Run combined report publishing tests**

Run:

```bash
uv run pytest tests/test_report_publishing.py tests/api/test_report_publishing_api.py -q --no-cov
```

Expected: PASS.

- [ ] **Step 5: Commit Task 3 and Task 4**

Run:

```bash
git add src/mindroom/api/report_headers.py src/mindroom/api/report_publishing.py tests/api/test_report_publishing_api.py
git commit -m "Serve static site public reports"
```

---

### Task 5: Add Failing Tool Tests For `source_type="static_site"`

**Files:**
- Modify: `tests/test_report_publishing.py`

- [ ] **Step 1: Add tool tests for workspace static-site source**

Add these tests before `test_report_publishing_tool_rejects_arbitrary_sources`.

```python
def test_report_publishing_tool_publishes_workspace_static_site(tmp_path: Path) -> None:
    """Report Publishing should let agents publish copied static-site directories."""
    report_tool = ReportPublishingTools()
    context = _make_context(tmp_path, public_url="https://mindroom.lab.mindroom.chat")
    assert context.storage_path is None
    workspace_root = context.runtime_paths.storage_root / "agents" / "general" / "workspace"
    workspace_root.mkdir(parents=True)
    site_dir = workspace_root / "public-demo"
    site_dir.mkdir()
    (site_dir / "index.html").write_text("<!doctype html><script src='app.js'></script>", encoding="utf-8")
    (site_dir / "app.js").write_text("document.body.dataset.ready = 'true';", encoding="utf-8")
    context = replace(context, storage_path=workspace_root)

    with tool_runtime_context(context):
        published = _tool_payload(
            report_tool.publish_report(
                source_type="static_site",
                source={"path": "public-demo", "title": "Public Demo"},
                confirm_public=True,
            ),
        )

    assert published["status"] == "ok"
    assert published["source_type"] == "static_site"
    assert published["source"] == {"path": "public-demo"}
    assert published["public_url"] == f"https://mindroom.lab.mindroom.chat/reports/public/{published['slug']}/"
    assert published["public_path"] == f"/reports/public/{published['slug']}/"


def test_report_publishing_tool_requires_workspace_for_static_site(tmp_path: Path) -> None:
    """Static site publishing should require an agent workspace."""
    report_tool = ReportPublishingTools()
    context = _make_context(tmp_path)

    with tool_runtime_context(context):
        published = _tool_payload(
            report_tool.publish_report(
                source_type="static_site",
                source={"path": "public-demo", "title": "Public Demo"},
                confirm_public=True,
            ),
        )

    assert published["status"] == "error"
    assert "agent workspace" in published["message"]


def test_report_publishing_tool_rejects_static_site_path_escape(tmp_path: Path) -> None:
    """Static site path input should stay workspace-relative."""
    report_tool = ReportPublishingTools()
    context = _make_context(tmp_path)
    workspace_root = context.runtime_paths.storage_root / "agents" / "general" / "workspace"
    workspace_root.mkdir(parents=True)
    context = replace(context, storage_path=workspace_root)

    with tool_runtime_context(context):
        escaped = _tool_payload(
            report_tool.publish_report(
                source_type="static_site",
                source={"path": "../outside", "title": "Escape"},
                confirm_public=True,
            ),
        )

    assert escaped["status"] == "error"
    assert "workspace root" in escaped["message"]
```

- [ ] **Step 2: Run tool tests and verify RED**

Run:

```bash
uv run pytest tests/test_report_publishing.py -q --no-cov
```

Expected: FAIL because `_resolve_publishable_source` rejects `source_type="static_site"`.

---

### Task 6: Implement `source_type="static_site"` In The Tool

**Files:**
- Modify: `src/mindroom/custom_tools/report_publishing.py`
- Test: `tests/test_report_publishing.py`

- [ ] **Step 1: Add imports and source keys**

Modify imports:

```python
from mindroom.workspaces import resolve_workspace_relative_path
```

Add constants:

```python
_STATIC_SITE_SOURCE_KEYS = frozenset({"path", "title"})
```

- [ ] **Step 2: Add the static-site resolver branch**

Modify `_resolve_publishable_source`:

```python
    if normalized_source_type == "static_site":
        return _resolve_static_site_source(context, source)
```

Add this function:

```python
def _resolve_static_site_source(
    context: ToolRuntimeContext,
    source: dict[str, Any],
) -> PublishableReport:
    _reject_unsupported_source_fields(source, _STATIC_SITE_SOURCE_KEYS, "static_site source")
    if context.storage_path is None:
        msg = "static_site publishing requires an agent workspace in this runtime path."
        raise ReportPublishingError(msg)
    source_path = _required_source_text(source, "path", context="static_site source")
    title = _required_source_text(source, "title", context="static_site source")
    try:
        site_dir = resolve_workspace_relative_path(
            context.storage_path,
            source_path,
            field_name="static_site source.path",
        )
    except ValueError as exc:
        raise ReportPublishingError(str(exc)) from exc
    return PublishableReport(
        source_type="static_site",
        source={"path": source_path},
        artifact_path=site_dir,
        title=title,
        requested_by=context.requester_id,
        artifact_kind="static_site",
    )
```

- [ ] **Step 3: Make `public_path` preserve static-site trailing slash**

Update `_public_path_for_report`:

```python
def _public_path_for_report(report: PublishedReport) -> str:
    if report.public_url is None:
        suffix = f"/reports/public/{report.slug}/" if report.artifact_kind == "static_site" else f"/reports/public/{report.slug}"
        return suffix
    public_path = urlsplit(report.public_url).path
    if public_path:
        return public_path
    return f"/reports/public/{report.slug}/" if report.artifact_kind == "static_site" else f"/reports/public/{report.slug}"
```

- [ ] **Step 4: Update schema descriptions**

Change `_TOOL_DESCRIPTIONS["publish_report"]` to:

```python
"Publish an authorized report source through a revocable public link. Supports source_type dynamic_workflow_run and static_site."
```

- [ ] **Step 5: Run tool tests and verify GREEN**

Run:

```bash
uv run pytest tests/test_report_publishing.py -q --no-cov
```

Expected: PASS.

- [ ] **Step 6: Run report publishing tests**

Run:

```bash
uv run pytest tests/test_report_publishing.py tests/api/test_report_publishing_api.py -q --no-cov
```

Expected: PASS.

- [ ] **Step 7: Commit Task 5 and Task 6**

Run:

```bash
git add src/mindroom/custom_tools/report_publishing.py tests/test_report_publishing.py
git commit -m "Add static site report publishing tool source"
```

---

### Task 7: Update Documentation And Generated References

**Files:**
- Modify: `docs/tools/agent-orchestration.md`
- Modify by pre-commit: `skills/mindroom-docs/references/llms-full.txt`
- Modify by pre-commit: `skills/mindroom-docs/references/page__tools__agent-orchestration__index.md`

- [ ] **Step 1: Update report publishing docs**

In `docs/tools/agent-orchestration.md`, replace the `report_publishing` source description with:

```markdown
The current source types are `dynamic_workflow_run` and `static_site`.
Use `dynamic_workflow_run` to publish a completed Dynamic Workflow HTML report.
Use `static_site` to publish a copied workspace directory that contains `index.html` and optional CSS, JavaScript, images, fonts, or JSON assets.
The static site source path is workspace-relative and the published copy is stored under `MINDROOM_STORAGE_PATH/report_publishing/artifacts/<slug>/`.
JavaScript is allowed for static sites, but the public route serves static sites with a sandbox CSP that omits `allow-same-origin` and sets `connect-src 'none'`.
That means scripts can drive local page interactivity, but they cannot act as logged-in MindRoom dashboard code or call MindRoom APIs.
```

Update the example:

```python
publish_report(
    source_type="static_site",
    source={"path": "public-demo", "title": "Public Demo"},
    confirm_public=True,
)
```

Add this proxy note:

```markdown
No extra proxy route is needed when `/reports/public/*` already reaches the MindRoom backend.
If the dashboard frontend and Python backend are split across upstreams, route `/reports/public/*` to the Python backend and do not put dashboard-login middleware on that path.
Set `MINDROOM_PUBLIC_URL` to the externally reachable dashboard origin, such as `https://mindroom.lab.mindroom.chat`, so publish payloads include clickable absolute URLs.
```

- [ ] **Step 2: Run pre-commit to regenerate references**

Run:

```bash
uv run pre-commit run --all-files
```

Expected: PASS and generated docs references updated.

- [ ] **Step 3: Commit docs**

Run:

```bash
git add docs/tools/agent-orchestration.md skills/mindroom-docs/references/llms-full.txt skills/mindroom-docs/references/page__tools__agent-orchestration__index.md
git commit -m "Document static site report publishing"
```

---

### Task 8: Final Verification, PR Update, And Live Smoke Test

**Files:**
- No new source files.
- May modify PR title/body through GitHub CLI.

- [ ] **Step 1: Run focused verification**

Run:

```bash
uv run pytest tests/test_report_publishing.py tests/api/test_report_publishing_api.py -q --no-cov
uv run ruff check src/mindroom/report_publishing src/mindroom/custom_tools/report_publishing.py src/mindroom/api/report_publishing.py tests/test_report_publishing.py tests/api/test_report_publishing_api.py
```

Expected: PASS.

- [ ] **Step 2: Run boundary and full pre-commit verification**

Run:

```bash
uv run tach check --dependencies --interfaces
uv run pre-commit run --all-files
```

Expected: PASS.

- [ ] **Step 3: Run full test suite**

Run:

```bash
MINDROOM_AUTO_BUILD_FRONTEND=0 uv run pytest -n auto --no-cov -q
```

Expected: PASS with no failures.

- [ ] **Step 4: Push branch**

Run:

```bash
git push origin feature/dynamic-workflow-public-reports
```

Expected: branch pushed.

- [ ] **Step 5: Update PR description**

Run:

```bash
gh pr edit 1200 --title "Add reusable public report publishing tool" --body-file /tmp/pr-1200-body.md
```

Use a body that says:

```markdown
## Summary
- add reusable `report_publishing` storage, tool, and `/reports/public/{slug}` serving for revocable public links
- support completed Dynamic Workflow reports through `source_type="dynamic_workflow_run"`
- support copied static-site directories through `source_type="static_site"` with sandboxed same-domain JavaScript
- keep public slug serving fail-closed with uniform 404s for invalid, missing, revoked, corrupt, missing-artifact, or traversal records
- update docs, generated skill refs, tool metadata, Tach boundaries, and tests

## Tests
- `uv run pytest tests/test_report_publishing.py tests/api/test_report_publishing_api.py -q --no-cov`
- `uv run tach check --dependencies --interfaces`
- `uv run pre-commit run --all-files`
- `MINDROOM_AUTO_BUILD_FRONTEND=0 uv run pytest -n auto --no-cov -q`

## Deployment
- Set `MINDROOM_PUBLIC_URL` to the externally reachable origin, for example `https://mindroom.lab.mindroom.chat`.
- Ensure `/reports/public/*` routes to the Python backend without dashboard auth middleware.
```

- [ ] **Step 6: Live smoke test on lab deployment**

In a live MindRoom room where an agent has `report_publishing`, ask the agent to create a workspace directory:

```text
public-demo/
  index.html
  app.js
```

Then call:

```python
publish_report(
    source_type="static_site",
    source={"path": "public-demo", "title": "Public Demo"},
    confirm_public=True,
)
```

Verify:

```bash
curl -i https://mindroom.lab.mindroom.chat/reports/public/<slug>/
curl -i https://mindroom.lab.mindroom.chat/reports/public/<slug>/app.js
```

Expected:
- `index.html` returns HTTP 200.
- `app.js` returns HTTP 200.
- Response headers include `Content-Security-Policy: sandbox allow-scripts`.
- The CSP does not include `allow-same-origin`.
- `revoke_public_report("<slug>")` makes both URLs return `{"detail":"Public report was not found."}` with HTTP 404.

---

## Self-Review

- Spec coverage: The plan covers directory source input, copied snapshots, same-domain JS, sandbox CSP, revocation, proxy notes, tests, docs, and live deployment verification.
- Placeholder scan: The plan contains no placeholder tasks and every implementation step includes concrete file paths, code snippets, commands, and expected results.
- Type consistency: `artifact_kind`, `static_site`, `public_report_asset_path`, `snapshot_static_site`, and `resolve_static_site_asset` are introduced before later tasks use them.
