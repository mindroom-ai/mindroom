"""Tests for public report publishing tools and storage."""

from __future__ import annotations

import json
from dataclasses import replace
from typing import TYPE_CHECKING, Any, Literal
from unittest.mock import AsyncMock

import pytest

import mindroom.tools  # noqa: F401
from mindroom.config.agent import AgentConfig
from mindroom.config.main import Config
from mindroom.config.models import ModelConfig
from mindroom.custom_tools.dynamic_workflow import DynamicWorkflowTools
from mindroom.custom_tools.dynamic_workflow_context import dynamic_workflow_store_and_owner
from mindroom.custom_tools.report_publishing import ReportPublishingTools
from mindroom.dynamic_workflows.service import DynamicWorkflowService
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
        ),
        runtime_paths,
    )
    return ToolRuntimeContext(
        agent_name="general",
        room_id="!room:localhost",
        thread_id="$thread:localhost",
        resolved_thread_id="$thread:localhost",
        requester_id="@user:localhost",
        client=AsyncMock(),
        config=config,
        runtime_paths=runtime_paths_for(config),
        conversation_cache=AsyncMock(),
        event_cache=make_event_cache_mock(),
        room=None,
        reply_to_event_id="$event:localhost",
        storage_path=None,
    )


def _tool_payload(result: str) -> dict[str, Any]:
    return json.loads(result)


def test_report_publishing_tool_registered() -> None:
    """Report publishing should be its own reusable tool surface."""
    metadata = TOOL_METADATA["report_publishing"]

    assert metadata.display_name == "Report Publishing"
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
    loaded = store.get_public_report(report.slug)
    html_path = store.public_report_html_path(report.slug)
    revoked = store.revoke_public_report(report.slug, revoked_by="@alice:localhost")

    assert report.slug.startswith("pub_")
    assert report.public_url == f"https://acme.mindroom.chat/reports/public/{report.slug}"
    assert loaded.source_type == "test_report"
    assert loaded.source == {"id": "example"}
    assert html_path == report_path
    assert revoked.revoked_at is not None
    with pytest.raises(ReportPublishingError, match="revoked"):
        store.public_report_html_path(report.slug)


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
        store.public_report_html_path(report.slug)


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

    index_path = store.public_report_asset_path(report.slug)
    assert index_path.name == "index.html"
    assert index_path.read_text(encoding="utf-8") == "<!doctype html><h1>Single Page</h1>"


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
        store.public_report_asset_path(report.slug, "../index.html")


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
    assert published["public_url"] == f"https://acme.mindroom.chat/mindroom/reports/public/{published['slug']}"
    assert published["public_path"] == f"/mindroom/reports/public/{published['slug']}"
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
    assert published["public_url"] == f"https://mindroom.lab.mindroom.chat/reports/public/{published['slug']}/"
    assert published["public_path"] == f"/reports/public/{published['slug']}/"


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
    assert published["public_url"] == f"https://mindroom.lab.mindroom.chat/reports/public/{published['slug']}/"


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
    assert "not available to the current requester" in revoked["message"]
