"""Tests for public report publishing tools and storage."""

from __future__ import annotations

import json
from dataclasses import replace
from typing import TYPE_CHECKING, Any, Literal
from unittest.mock import AsyncMock, patch

import pytest

import mindroom.tools  # noqa: F401
from mindroom.config.agent import AgentConfig
from mindroom.config.main import Config
from mindroom.config.models import ModelConfig
from mindroom.config.report_publishing import ReportPublishingConfig
from mindroom.custom_tools.dynamic_workflow import DynamicWorkflowTools
from mindroom.custom_tools.dynamic_workflow_context import dynamic_workflow_store_and_owner
from mindroom.custom_tools.report_publishing import ReportPublishingTools
from mindroom.dynamic_workflows.service import DynamicWorkflowService
from mindroom.entity_resolution import entity_identity_registry
from mindroom.message_target import MessageTarget
from mindroom.report_access_policy import ReportAccessPolicy
from mindroom.report_publishing.store import PublishableReport, ReportPublishingError, ReportPublishingStore
from mindroom.tool_system.metadata import TOOL_METADATA
from mindroom.tool_system.runtime_context import ToolRuntimeContext, tool_runtime_context
from tests.conftest import bind_runtime_paths, make_event_cache_mock, runtime_paths_for, test_runtime_paths

if TYPE_CHECKING:
    from pathlib import Path


def _workflow_spec() -> dict[str, object]:
    return {
        "schema_version": 1,
        "id": "competitor-research-report",
        "name": "Competitor Research Report",
        "description": "Create a cited HTML report about competitors.",
        "kind": "workflow",
        "inputs": {
            "type": "object",
            "required": ["topic"],
            "properties": {"topic": {"type": "string"}},
        },
        "participants": [
            {
                "id": "writer",
                "kind": "ephemeral_agent",
                "name": "Report Writer",
                "model": "claude-sonnet-4-6",
                "tools": [],
            },
        ],
        "workflow": [
            {
                "id": "research",
                "type": "transform_step",
                "template": "Research brief for {input.topic}.",
            },
        ],
        "outputs": [{"id": "brief", "type": "text", "from_step": "research"}],
        "permissions": {
            "max_runtime_seconds": 1800,
            "max_concurrent_agents": 4,
            "max_total_agents": 16,
            "models": ["claude-sonnet-4-6"],
            "tools": [],
            "data": {
                "matrix_history": "none",
                "attachments": "none",
                "knowledge_bases": [],
            },
        },
    }


def _make_context(
    tmp_path: Path,
    *,
    public_url: str = "https://acme.mindroom.chat",
    agent_memory_backend: Literal["file"] | None = None,
    trusted_auth: bool = False,
    trusted_auth_env: dict[str, str] | None = None,
    report_publishing: ReportPublishingConfig | None = None,
) -> ToolRuntimeContext:
    runtime_paths = test_runtime_paths(tmp_path)
    runtime_paths = runtime_paths.__class__(
        config_path=runtime_paths.config_path,
        config_dir=runtime_paths.config_dir,
        env_path=runtime_paths.env_path,
        storage_root=runtime_paths.storage_root,
        process_env={
            **dict(runtime_paths.process_env),
            "MINDROOM_PUBLIC_URL": public_url,
            **(
                trusted_auth_env
                or {
                    "MINDROOM_TRUSTED_UPSTREAM_AUTH_ENABLED": "true",
                    "MINDROOM_TRUSTED_UPSTREAM_USER_ID_HEADER": "X-Trusted-User",
                    "MINDROOM_TRUSTED_UPSTREAM_MATRIX_USER_ID_HEADER": "X-Trusted-Matrix-User",
                }
                if trusted_auth
                else {}
            ),
        },
        env_file_values=runtime_paths.env_file_values,
    )
    config = bind_runtime_paths(
        Config(
            agents={
                "general": AgentConfig(
                    display_name="General Agent",
                    tools=["dynamic_workflow", "report_publishing"],
                    memory_backend=agent_memory_backend,
                ),
            },
            models={"default": ModelConfig(provider="anthropic", id="claude-sonnet-4-6")},
            report_publishing=report_publishing or ReportPublishingConfig(),
        ),
        runtime_paths,
    )
    return ToolRuntimeContext(
        agent_name="general",
        target=MessageTarget.resolve(
            room_id="!room:localhost",
            thread_id="$thread:localhost",
            reply_to_event_id="$event:localhost",
        ),
        requester_id="@user:localhost",
        client=AsyncMock(),
        config=config,
        runtime_paths=runtime_paths_for(config),
        conversation_cache=AsyncMock(),
        event_cache=make_event_cache_mock(),
        room=None,
        storage_path=None,
    )


def _tool_payload(result: str) -> dict[str, Any]:
    return json.loads(result)


def test_report_publishing_tool_registered() -> None:
    """Report publishing should be its own reusable tool surface."""
    metadata = TOOL_METADATA["report_publishing"]

    assert metadata.display_name == "Report Publishing"
    assert metadata.consumes_workspace_paths is True
    assert metadata.function_names == (
        "publish_report",
        "revoke_public_report",
    )


def test_report_publishing_store_creates_revocable_public_link(tmp_path: Path) -> None:
    """Published report links should be stored separately from report producers."""
    storage_root = tmp_path / "mindroom_data"
    report_path = storage_root / "reports" / "example.html"
    report_path.parent.mkdir(parents=True)
    report_path.write_text("<html>Report</html>", encoding="utf-8")
    store = ReportPublishingStore(storage_root)

    report = store.publish_report(
        source=PublishableReport(
            source_type="test_report",
            source={"id": "example"},
            artifact_path=report_path,
            title="Example Report",
            requested_by="@alice:localhost",
        ),
        published_by="@alice:localhost",
        base_url="https://acme.mindroom.chat",
    )
    loaded = store.get_report(report.slug)
    html_path = store.report_asset_path(store.get_report(report.slug))
    revoked = store.revoke_report(report.slug, revoked_by="@alice:localhost")

    assert report.slug.startswith("pub_")
    assert report.public_url == f"https://acme.mindroom.chat/reports/public/{report.slug}"
    assert loaded.source_type == "test_report"
    assert loaded.source == {"id": "example"}
    assert html_path == report_path
    assert revoked.revoked_at is not None
    with pytest.raises(ReportPublishingError, match="revoked"):
        store.report_asset_path(store.get_report(report.slug))


def test_report_publishing_store_round_trips_origin_room_metadata(tmp_path: Path) -> None:
    """Protected publication should preserve exact room and publisher identities."""
    storage_root = tmp_path / "mindroom_data"
    report_path = storage_root / "reports" / "example.html"
    report_path.parent.mkdir(parents=True)
    report_path.write_text("<html>Report</html>", encoding="utf-8")
    store = ReportPublishingStore(storage_root)

    report = store.publish_report(
        source=PublishableReport(
            source_type="test_report",
            source={"id": "example"},
            artifact_path=report_path,
            title="Example Report",
            requested_by="@alice:localhost",
        ),
        published_by="@alice:localhost",
        base_url="https://acme.mindroom.chat",
        access_policy=ReportAccessPolicy.ORIGIN_ROOM,
        origin_room_id="!Nhcu5BS-UMnFX7hBVfVSoXiD7OgH6iRT-xyIuqDnpYQ",
        publisher_entity_name="general",
        publisher_matrix_user_id="@mindroom_general:localhost",
    )
    loaded = store.get_report(report.slug)

    assert loaded.access_policy is ReportAccessPolicy.ORIGIN_ROOM
    assert loaded.origin_room_id == "!Nhcu5BS-UMnFX7hBVfVSoXiD7OgH6iRT-xyIuqDnpYQ"
    assert loaded.publisher_entity_name == "general"
    assert loaded.publisher_matrix_user_id == "@mindroom_general:localhost"
    assert loaded.public_url == f"https://acme.mindroom.chat/reports/room/{report.slug}"


def test_report_publishing_store_loads_legacy_record_as_public(tmp_path: Path) -> None:
    """Records predating access_policy must retain public bearer semantics."""
    storage_root = tmp_path / "mindroom_data"
    report_path = storage_root / "reports" / "example.html"
    report_path.parent.mkdir(parents=True)
    report_path.write_text("<html>Report</html>", encoding="utf-8")
    store = ReportPublishingStore(storage_root)
    report = store.publish_report(
        source=PublishableReport(
            source_type="test_report",
            source={},
            artifact_path=report_path,
            title="Legacy",
            requested_by="@alice:localhost",
        ),
        published_by="@alice:localhost",
    )
    metadata_path = storage_root / "report_publishing" / "public_reports" / f"{report.slug}.json"
    payload = json.loads(metadata_path.read_text(encoding="utf-8"))
    for key in (
        "access_policy",
        "origin_room_id",
        "publisher_entity_name",
        "publisher_matrix_user_id",
    ):
        payload.pop(key)
    metadata_path.write_text(json.dumps(payload), encoding="utf-8")

    loaded = store.get_report(report.slug)

    assert loaded.access_policy is ReportAccessPolicy.PUBLIC
    assert loaded.origin_room_id is None


@pytest.mark.parametrize(
    "changes",
    [
        {"access_policy": "origin_room"},
        {"access_policy": "shared_room"},
        {"publisher_matrix_user_id": 123},
    ],
)
def test_report_publishing_store_rejects_malformed_policy_records(
    tmp_path: Path,
    changes: dict[str, object],
) -> None:
    """Malformed protected or unknown-policy records must fail closed."""
    storage_root = tmp_path / "mindroom_data"
    report_path = storage_root / "reports" / "example.html"
    report_path.parent.mkdir(parents=True)
    report_path.write_text("<html>Report</html>", encoding="utf-8")
    store = ReportPublishingStore(storage_root)
    report = store.publish_report(
        source=PublishableReport(
            source_type="test_report",
            source={},
            artifact_path=report_path,
            title="Example",
            requested_by="@alice:localhost",
        ),
        published_by="@alice:localhost",
    )
    metadata_path = storage_root / "report_publishing" / "public_reports" / f"{report.slug}.json"
    payload = json.loads(metadata_path.read_text(encoding="utf-8"))
    payload.update(changes)
    metadata_path.write_text(json.dumps(payload), encoding="utf-8")

    with pytest.raises(ReportPublishingError):
        store.get_report(report.slug)


def test_report_publishing_store_rejects_incidental_public_room_metadata(tmp_path: Path) -> None:
    """New public records must not carry misleading protected metadata."""
    storage_root = tmp_path / "mindroom_data"
    report_path = storage_root / "reports" / "example.html"
    report_path.parent.mkdir(parents=True)
    report_path.write_text("<html>Report</html>", encoding="utf-8")

    with pytest.raises(ReportPublishingError, match="must not contain"):
        ReportPublishingStore(storage_root).publish_report(
            source=PublishableReport(
                source_type="test_report",
                source={},
                artifact_path=report_path,
                title="Example",
                requested_by="@alice:localhost",
            ),
            published_by="@alice:localhost",
            origin_room_id="!origin:localhost",
        )


def test_report_publishing_store_rejects_artifacts_outside_storage_root(tmp_path: Path) -> None:
    """Public links should never publish arbitrary filesystem paths."""
    storage_root = tmp_path / "mindroom_data"
    outside_path = tmp_path / "outside.html"
    outside_path.write_text("<html>Outside</html>", encoding="utf-8")
    store = ReportPublishingStore(storage_root)

    with pytest.raises(ReportPublishingError, match="storage root"):
        store.publish_report(
            source=PublishableReport(
                source_type="test_report",
                source={"id": "outside"},
                artifact_path=outside_path,
                title="Outside Report",
                requested_by="@alice:localhost",
            ),
            published_by="@alice:localhost",
            base_url="https://acme.mindroom.chat",
        )


def test_report_publishing_store_rejects_serve_time_symlink_escape(tmp_path: Path) -> None:
    """Public links should not follow artifact symlinks that escape storage root."""
    storage_root = tmp_path / "mindroom_data"
    report_path = storage_root / "reports" / "example.html"
    report_path.parent.mkdir(parents=True)
    report_path.write_text("<html>Report</html>", encoding="utf-8")
    outside_path = tmp_path / "outside.html"
    outside_path.write_text("<html>Outside</html>", encoding="utf-8")
    store = ReportPublishingStore(storage_root)
    report = store.publish_report(
        source=PublishableReport(
            source_type="test_report",
            source={"id": "example"},
            artifact_path=report_path,
            title="Example Report",
            requested_by="@alice:localhost",
        ),
        published_by="@alice:localhost",
        base_url="https://acme.mindroom.chat",
    )
    report_path.unlink()
    report_path.symlink_to(outside_path)

    with pytest.raises(ReportPublishingError, match="artifact path is invalid"):
        store.report_asset_path(store.get_report(report.slug))


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

    index_path = store.report_asset_path(store.get_report(report.slug))
    script_path = store.report_asset_path(store.get_report(report.slug), "app.js")

    assert report.artifact_kind == "static_site"
    assert report.public_url == f"https://mindroom.lab.mindroom.chat/reports/public/{report.slug}/"
    assert index_path.read_text(encoding="utf-8").startswith("<!doctype html><script")
    assert script_path.read_text(encoding="utf-8") == "document.body.dataset.ready = 'true';"
    assert index_path.parent.is_relative_to(storage_root / "report_publishing" / "artifacts")


def test_report_publishing_store_creates_single_page_snapshot(tmp_path: Path) -> None:
    """A single workspace HTML page should publish as a static site index."""
    storage_root = tmp_path / "mindroom_data"
    page_path = tmp_path / "workspace" / "report.html"
    page_path.parent.mkdir(parents=True)
    page_path.write_text("<!doctype html><h1>Single Page</h1>", encoding="utf-8")
    store = ReportPublishingStore(storage_root)

    report = store.publish_report(
        source=PublishableReport(
            source_type="static_site",
            source={"path": "report.html"},
            artifact_path=page_path,
            title="Single Page",
            requested_by="@alice:localhost",
            artifact_kind="static_site",
        ),
        published_by="@alice:localhost",
        base_url="https://mindroom.lab.mindroom.chat",
    )

    index_path = store.report_asset_path(store.get_report(report.slug))
    assert index_path.name == "index.html"
    assert index_path.read_text(encoding="utf-8") == "<!doctype html><h1>Single Page</h1>"


def test_report_publishing_store_removes_single_page_snapshot_on_copy_failure(tmp_path: Path) -> None:
    """Failed single-page snapshots should return a publishing error and leave no orphaned artifact directory."""
    storage_root = tmp_path / "mindroom_data"
    page_path = tmp_path / "workspace" / "report.html"
    page_path.parent.mkdir(parents=True)
    page_path.write_text("<!doctype html><h1>Single Page</h1>", encoding="utf-8")
    store = ReportPublishingStore(storage_root)

    with (
        patch("mindroom.report_publishing.static_site.shutil.copy2", side_effect=OSError("disk full")),
        pytest.raises(ReportPublishingError, match="disk full"),
    ):
        store.publish_report(
            source=PublishableReport(
                source_type="static_site",
                source={"path": "report.html"},
                artifact_path=page_path,
                title="Single Page",
                requested_by="@alice:localhost",
                artifact_kind="static_site",
            ),
            published_by="@alice:localhost",
            base_url="https://mindroom.lab.mindroom.chat",
        )

    artifacts_root = storage_root / "report_publishing" / "artifacts"
    assert not artifacts_root.exists() or list(artifacts_root.iterdir()) == []


def test_report_publishing_tool_reports_static_site_copy_failure(tmp_path: Path) -> None:
    """Static site copy failures should return the normal tool JSON error payload."""
    report_tool = ReportPublishingTools()
    context = _make_context(
        tmp_path,
        public_url="https://mindroom.lab.mindroom.chat",
        agent_memory_backend="file",
    )
    workspace_root = context.runtime_paths.storage_root / "agents" / "general" / "workspace"
    workspace_root.mkdir(parents=True)
    (workspace_root / "report.html").write_text("<!doctype html><h1>Single Page</h1>", encoding="utf-8")

    with (
        patch("mindroom.report_publishing.static_site.shutil.copy2", side_effect=OSError("disk full")),
        tool_runtime_context(context),
    ):
        published = _tool_payload(
            report_tool.publish_report(
                source_type="static_site",
                source={"path": "report.html", "title": "Single Page"},
                confirm_public=True,
            ),
        )

    assert published["status"] == "error"
    assert published["source_type"] == "static_site"
    assert "disk full" in published["message"]


def test_report_publishing_store_rejects_single_page_without_html_suffix(tmp_path: Path) -> None:
    """Single-file static site sources should stay limited to HTML pages."""
    storage_root = tmp_path / "mindroom_data"
    page_path = tmp_path / "workspace" / "report.pdf"
    page_path.parent.mkdir(parents=True)
    page_path.write_bytes(b"%PDF-1.7")
    store = ReportPublishingStore(storage_root)

    with pytest.raises(ReportPublishingError, match="HTML page"):
        store.publish_report(
            source=PublishableReport(
                source_type="static_site",
                source={"path": "report.pdf"},
                artifact_path=page_path,
                title="Not A Page",
                requested_by="@alice:localhost",
                artifact_kind="static_site",
            ),
            published_by="@alice:localhost",
            base_url="https://mindroom.lab.mindroom.chat",
        )


def test_report_publishing_store_rejects_static_site_without_index(tmp_path: Path) -> None:
    """Static site publishing should require index.html."""
    storage_root = tmp_path / "mindroom_data"
    source_dir = tmp_path / "workspace" / "site"
    source_dir.mkdir(parents=True)
    (source_dir / "app.js").write_text("console.log('missing index')", encoding="utf-8")
    store = ReportPublishingStore(storage_root)

    with pytest.raises(ReportPublishingError, match=r"index\.html"):
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
        store.report_asset_path(store.get_report(report.slug), "../index.html")


def test_report_publishing_tool_publishes_dynamic_workflow_run_report(tmp_path: Path) -> None:
    """Report Publishing should expose Dynamic Workflow reports through a source reference."""
    dynamic_workflow_tool = DynamicWorkflowTools()
    report_tool = ReportPublishingTools()
    context = _make_context(tmp_path, public_url="https://acme.mindroom.chat/mindroom")

    with tool_runtime_context(context):
        _tool_payload(dynamic_workflow_tool.create_workflow(_workflow_spec(), reason="initial design"))
        run = _tool_payload(
            dynamic_workflow_tool.run_workflow("competitor-research-report", {"topic": "Agno factories"}),
        )
        missing_confirmation = _tool_payload(
            report_tool.publish_report(
                source_type="dynamic_workflow_run",
                source={"workflow_id": "competitor-research-report", "run_id": run["run_id"]},
                confirm_public=False,
            ),
        )
        published = _tool_payload(
            report_tool.publish_report(
                source_type="dynamic_workflow_run",
                source={"workflow_id": "competitor-research-report", "run_id": run["run_id"]},
                confirm_public=True,
            ),
        )
        revoked = _tool_payload(report_tool.revoke_public_report(published["slug"]))

    assert missing_confirmation["status"] == "error"
    assert "confirm_public" in missing_confirmation["message"]
    assert published["status"] == "ok"
    assert published["source_type"] == "dynamic_workflow_run"
    assert published["source"] == {
        "workflow_id": "competitor-research-report",
        "run_id": run["run_id"],
        "scope": "agent",
    }
    assert published["report_url"] == f"https://acme.mindroom.chat/mindroom/reports/public/{published['slug']}"
    assert published["report_path"] == f"/mindroom/reports/public/{published['slug']}"
    assert published["public_url"] == published["report_url"]
    assert published["public_path"] == published["report_path"]
    assert revoked["status"] == "ok"
    assert revoked["revoked_at"] is not None


def test_report_publishing_tool_publishes_workspace_static_site(tmp_path: Path) -> None:
    """Report Publishing should let agents publish copied static-site directories."""
    report_tool = ReportPublishingTools()
    context = _make_context(
        tmp_path,
        public_url="https://mindroom.lab.mindroom.chat",
        agent_memory_backend="file",
    )
    workspace_root = context.runtime_paths.storage_root / "agents" / "general" / "workspace"
    site_dir = workspace_root / "public-demo"
    site_dir.mkdir(parents=True)
    (site_dir / "index.html").write_text("<!doctype html><script src='app.js'></script>", encoding="utf-8")
    (site_dir / "app.js").write_text("document.body.dataset.ready = 'true';", encoding="utf-8")

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
    assert published["report_url"] == f"https://mindroom.lab.mindroom.chat/reports/public/{published['slug']}/"
    assert published["report_path"] == f"/reports/public/{published['slug']}/"


def test_report_publishing_tool_publishes_origin_room_from_trusted_context(tmp_path: Path) -> None:
    """Protected publication should derive room and publisher identity from runtime context."""
    report_tool = ReportPublishingTools()
    context = _make_context(
        tmp_path,
        public_url="https://mindroom.example",
        agent_memory_backend="file",
        trusted_auth=True,
    )
    workspace_root = context.runtime_paths.storage_root / "agents" / "general" / "workspace"
    workspace_root.mkdir(parents=True)
    (workspace_root / "report.html").write_text("<!doctype html><h1>Protected</h1>", encoding="utf-8")

    with tool_runtime_context(context):
        published = _tool_payload(
            report_tool.publish_report(
                source_type="static_site",
                source={"path": "report.html", "title": "Protected"},
                confirm_public=False,
                access_policy="origin_room",
            ),
        )

    report = ReportPublishingStore(context.runtime_paths.storage_root).get_report(published["slug"])
    expected_publisher_id = (
        entity_identity_registry(
            context.config,
            context.runtime_paths,
        )
        .current_id("general")
        .full_id
    )
    assert published["status"] == "ok"
    assert published["access_policy"] == "origin_room"
    assert published["report_url"] == f"https://mindroom.example/reports/room/{published['slug']}/"
    assert published["report_path"] == f"/reports/room/{published['slug']}/"
    assert "public_url" not in published
    assert "public_path" not in published
    assert "current" in published["message"]
    assert report.origin_room_id == "!room:localhost"
    assert report.publisher_entity_name == "general"
    assert report.publisher_matrix_user_id == expected_publisher_id
    assert report.requested_by == "@user:localhost"


def test_report_publishing_tool_uses_configured_origin_room_default(tmp_path: Path) -> None:
    """Omitted policy should use configured protected default without public confirmation."""
    report_tool = ReportPublishingTools()
    context = _make_context(
        tmp_path,
        agent_memory_backend="file",
        trusted_auth=True,
        report_publishing=ReportPublishingConfig(
            default_access_policy=ReportAccessPolicy.ORIGIN_ROOM,
        ),
    )
    workspace_root = context.runtime_paths.storage_root / "agents" / "general" / "workspace"
    workspace_root.mkdir(parents=True)
    (workspace_root / "report.html").write_text("<!doctype html>Protected", encoding="utf-8")

    with tool_runtime_context(context):
        published = _tool_payload(
            report_tool.publish_report(
                source_type="static_site",
                source={"path": "report.html", "title": "Protected"},
                confirm_public=False,
            ),
        )

    assert published["status"] == "ok"
    assert published["access_policy"] == "origin_room"


def test_report_publishing_tool_rejects_new_public_links_when_disabled(tmp_path: Path) -> None:
    """allow_public=false should reject explicit and default public creation only."""
    report_tool = ReportPublishingTools()
    context = _make_context(
        tmp_path,
        report_publishing=ReportPublishingConfig(allow_public=False),
    )

    with tool_runtime_context(context):
        rejected = _tool_payload(
            report_tool.publish_report(
                source_type="static_site",
                source={"path": "missing.html", "title": "Public"},
                confirm_public=True,
                access_policy="public",
            ),
        )

    assert rejected["status"] == "error"
    assert "allow_public" in rejected["message"]


def test_public_disable_does_not_block_existing_report_revocation(tmp_path: Path) -> None:
    """Creation policy must not strand already-published public records."""
    report_tool = ReportPublishingTools()
    context = _make_context(tmp_path, agent_memory_backend="file")
    workspace_root = context.runtime_paths.storage_root / "agents" / "general" / "workspace"
    workspace_root.mkdir(parents=True)
    (workspace_root / "report.html").write_text("<!doctype html>Public", encoding="utf-8")

    with tool_runtime_context(context):
        published = _tool_payload(
            report_tool.publish_report(
                source_type="static_site",
                source={"path": "report.html", "title": "Public"},
                confirm_public=True,
            ),
        )
    disabled_context = replace(
        context,
        config=context.config.model_copy(
            update={"report_publishing": ReportPublishingConfig(allow_public=False)},
        ),
    )
    with tool_runtime_context(disabled_context):
        revoked = _tool_payload(report_tool.revoke_public_report(published["slug"]))

    assert published["status"] == "ok"
    assert "possesses" in published["message"]
    assert revoked["status"] == "ok"
    assert revoked["access_policy"] == "public"


def test_report_publishing_tool_rejects_origin_room_without_browser_auth(tmp_path: Path) -> None:
    """Protected creation should fail before copying when viewer auth is unavailable."""
    report_tool = ReportPublishingTools()
    context = _make_context(tmp_path)

    with tool_runtime_context(context):
        rejected = _tool_payload(
            report_tool.publish_report(
                source_type="static_site",
                source={"path": "missing.html", "title": "Protected"},
                confirm_public=False,
                access_policy="origin_room",
            ),
        )

    assert rejected["status"] == "error"
    assert "trusted browser authentication" in rejected["message"]


def test_report_publishing_tool_rejects_origin_room_with_malformed_email_mapping(tmp_path: Path) -> None:
    """Protected creation should fail before copying when its Matrix mapping is invalid."""
    report_tool = ReportPublishingTools()
    context = _make_context(
        tmp_path,
        trusted_auth=True,
        trusted_auth_env={
            "MINDROOM_TRUSTED_UPSTREAM_AUTH_ENABLED": "true",
            "MINDROOM_TRUSTED_UPSTREAM_USER_ID_HEADER": "X-Trusted-User",
            "MINDROOM_TRUSTED_UPSTREAM_EMAIL_HEADER": "X-Trusted-Email",
            "MINDROOM_TRUSTED_UPSTREAM_EMAIL_TO_MATRIX_USER_ID_TEMPLATE": "@static:example.org",
        },
    )

    with tool_runtime_context(context):
        rejected = _tool_payload(
            report_tool.publish_report(
                source_type="static_site",
                source={"path": "missing.html", "title": "Protected"},
                confirm_public=False,
                access_policy="origin_room",
            ),
        )

    assert rejected["status"] == "error"
    assert "exactly one {localpart} placeholder" in rejected["message"]


def test_report_publishing_tool_rejects_unknown_policy_and_publisher(tmp_path: Path) -> None:
    """Unsupported policy or missing configured publisher identity should fail closed."""
    report_tool = ReportPublishingTools()
    context = _make_context(tmp_path, trusted_auth=True)

    with tool_runtime_context(context):
        unsupported = _tool_payload(
            report_tool.publish_report(
                source_type="static_site",
                source={"path": "missing.html", "title": "Protected"},
                confirm_public=False,
                access_policy="shared_room",
            ),
        )
    with tool_runtime_context(replace(context, agent_name="missing")):
        missing_publisher = _tool_payload(
            report_tool.publish_report(
                source_type="static_site",
                source={"path": "missing.html", "title": "Protected"},
                confirm_public=False,
                access_policy="origin_room",
            ),
        )

    assert unsupported["status"] == "error"
    assert "Unsupported report access_policy" in unsupported["message"]
    assert missing_publisher["status"] == "error"
    assert "configured publisher identity" in missing_publisher["message"]


@pytest.mark.parametrize(
    ("missing_field", "expected_message"),
    [
        ("room_id", "canonical Matrix room ID"),
        ("agent_name", "publisher identity"),
    ],
)
def test_report_publishing_tool_rejects_missing_protected_context(
    tmp_path: Path,
    missing_field: str,
    expected_message: str,
) -> None:
    """Non-room or unidentified invocations must not create protected reports."""
    report_tool = ReportPublishingTools()
    context = _make_context(tmp_path, trusted_auth=True)
    if missing_field == "room_id":
        object.__setattr__(context.target, "room_id", "")
    else:
        context = replace(context, agent_name="")

    with tool_runtime_context(context):
        rejected = _tool_payload(
            report_tool.publish_report(
                source_type="static_site",
                source={"path": "missing.html", "title": "Protected"},
                confirm_public=False,
                access_policy="origin_room",
            ),
        )

    assert rejected["status"] == "error"
    assert expected_message in rejected["message"]


def test_report_publishing_tool_schema_has_no_model_controlled_identity_fields() -> None:
    """Model arguments may choose policy but never room or publisher identities."""
    parameters = ReportPublishingTools().functions["publish_report"].parameters
    properties = parameters["properties"]

    assert "access_policy" in properties
    assert "origin_room_id" not in properties
    assert "publisher_entity_name" not in properties
    assert "publisher_matrix_user_id" not in properties


def test_report_publishing_tool_publishes_workspace_single_html_page(tmp_path: Path) -> None:
    """Report Publishing should let agents publish one workspace HTML page directly."""
    report_tool = ReportPublishingTools()
    context = _make_context(
        tmp_path,
        public_url="https://mindroom.lab.mindroom.chat",
        agent_memory_backend="file",
    )
    workspace_root = context.runtime_paths.storage_root / "agents" / "general" / "workspace"
    workspace_root.mkdir(parents=True)
    (workspace_root / "report.html").write_text("<!doctype html><h1>Single Page</h1>", encoding="utf-8")

    with tool_runtime_context(context):
        published = _tool_payload(
            report_tool.publish_report(
                source_type="static_site",
                source={"path": "report.html", "title": "Single Page"},
                confirm_public=True,
            ),
        )

    assert published["status"] == "ok"
    assert published["source"] == {"path": "report.html"}
    assert published["report_url"] == f"https://mindroom.lab.mindroom.chat/reports/public/{published['slug']}/"


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
    context = _make_context(tmp_path, agent_memory_backend="file")
    workspace_root = context.runtime_paths.storage_root / "agents" / "general" / "workspace"
    workspace_root.mkdir(parents=True)

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


def test_report_publishing_tool_rejects_arbitrary_sources(tmp_path: Path) -> None:
    """Report Publishing should publish only registered authorized source types."""
    report_tool = ReportPublishingTools()
    context = _make_context(tmp_path)

    with tool_runtime_context(context):
        unsupported = _tool_payload(
            report_tool.publish_report(
                source_type="file_path",
                source={"path": "unregistered/report.html"},
                confirm_public=True,
            ),
        )
        extra_field = _tool_payload(
            report_tool.publish_report(
                source_type="dynamic_workflow_run",
                source={
                    "workflow_id": "competitor-research-report",
                    "run_id": "run_missing",
                    "artifact_path": "unregistered/report.html",
                },
                confirm_public=True,
            ),
        )

    assert unsupported["status"] == "error"
    assert "Unsupported report source_type" in unsupported["message"]
    assert extra_field["status"] == "error"
    assert "artifact_path" in extra_field["message"]


def test_report_publishing_tool_rejects_failed_dynamic_workflow_runs(tmp_path: Path) -> None:
    """Only completed Dynamic Workflow runs should be exposed as public report links."""
    report_tool = ReportPublishingTools()
    context = _make_context(tmp_path)
    spec = _workflow_spec()
    spec["workflow"] = [
        {
            "id": "write",
            "type": "agent_step",
            "participant": "writer",
            "prompt": "Write about {input.topic}.",
        },
    ]
    spec["outputs"] = [{"id": "brief", "type": "text", "from_step": "write"}]

    with tool_runtime_context(context):
        store, owner_id = dynamic_workflow_store_and_owner(context, "agent")
        store.create_workflow(
            spec=spec,
            scope="agent",
            owner_id=owner_id,
            created_by=context.agent_name or "general",
            reason="initial design",
        )
        run = DynamicWorkflowService(store).run_workflow(
            workflow_id="competitor-research-report",
            scope="agent",
            owner_id=owner_id,
            input_data={"topic": "Agno factories"},
            requested_by=context.requester_id,
        )
        rejected = _tool_payload(
            report_tool.publish_report(
                source_type="dynamic_workflow_run",
                source={"workflow_id": "competitor-research-report", "run_id": run.run_id},
                confirm_public=True,
            ),
        )

    assert run.status == "failed"
    assert rejected["status"] == "error"
    assert "Only completed" in rejected["message"]


def test_report_publishing_tool_denies_revoke_for_different_requester(tmp_path: Path) -> None:
    """Public report revocation should stay limited to the source requester or publisher."""
    dynamic_workflow_tool = DynamicWorkflowTools()
    report_tool = ReportPublishingTools()
    alice_context = _make_context(tmp_path)
    bob_context = replace(alice_context, requester_id="@bob:localhost")

    with tool_runtime_context(alice_context):
        _tool_payload(dynamic_workflow_tool.create_workflow(_workflow_spec(), reason="initial design"))
        run = _tool_payload(
            dynamic_workflow_tool.run_workflow("competitor-research-report", {"topic": "Agno factories"}),
        )
        published = _tool_payload(
            report_tool.publish_report(
                source_type="dynamic_workflow_run",
                source={"workflow_id": "competitor-research-report", "run_id": run["run_id"]},
                confirm_public=True,
            ),
        )
    with tool_runtime_context(bob_context):
        revoked = _tool_payload(report_tool.revoke_public_report(published["slug"]))

    assert revoked["status"] == "error"
    assert revoked["message"] == "Report is not available to the current requester."
