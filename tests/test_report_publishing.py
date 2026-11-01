"""Tests for public report publishing tools and storage."""

from __future__ import annotations

import asyncio
import json
import os
import threading
from dataclasses import replace
from typing import TYPE_CHECKING, Any, Literal
from unittest.mock import AsyncMock, patch

import pytest
from pydantic import ValidationError

import mindroom.tools  # noqa: F401
from mindroom.config.agent import AgentConfig
from mindroom.config.main import Config
from mindroom.config.models import ModelConfig
from mindroom.custom_tools.dynamic_workflow import DynamicWorkflowTools
from mindroom.custom_tools.dynamic_workflow_context import dynamic_workflow_store_and_owner
from mindroom.custom_tools.report_publishing import ReportPublishingTools
from mindroom.dynamic_workflows.service import DynamicWorkflowService
from mindroom.dynamic_workflows.spec import parse_workflow_spec
from mindroom.dynamic_workflows.store import DynamicWorkflowStore
from mindroom.message_target import MessageTarget
from mindroom.report_publishing.store import PublishableReport, ReportPublishingError, ReportPublishingStore
from mindroom.tool_system.registry import builtin_tool_registry
from mindroom.tool_system.runtime_context import ToolRuntimeContext, tool_runtime_context
from tests.agent_tool_helpers import toolkit_offers
from tests.authorization_helpers import (
    make_test_tool_runtime_context,
)
from tests.conftest import (
    bind_runtime_paths,
    make_conversation_reader_mock,
    make_relation_lookup,
    runtime_paths_for,
    test_runtime_paths,
)
from tests.test_dynamic_workflows import _RESEARCH_BRIEF_STEPS, file_work_waiting_for_the_loop
from tests.test_dynamic_workflows import _workflow_spec as dynamic_workflow_spec

if TYPE_CHECKING:
    from pathlib import Path


def _workflow_spec() -> dict[str, object]:
    return dynamic_workflow_spec(**_RESEARCH_BRIEF_STEPS)


def _make_context(
    tmp_path: Path,
    *,
    public_url: str = "https://acme.mindroom.chat",
    agent_memory_backend: Literal["file"] | None = None,
) -> ToolRuntimeContext:
    runtime_paths = test_runtime_paths(tmp_path)
    runtime_paths = replace(
        runtime_paths,
        # The test's paths keep no control state root.
        control_state_root=None,
        process_env={**runtime_paths.process_env, "MINDROOM_PUBLIC_URL": public_url},
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
            models={"default": ModelConfig(provider="anthropic", id="claude-sonnet-5")},
        ),
        runtime_paths,
    )
    return make_test_tool_runtime_context(
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
        relations=make_relation_lookup(),
        conversation_reader=make_conversation_reader_mock(),
        room=None,
        storage_path=None,
    )


def _tool_payload(result: str) -> dict[str, Any]:
    return json.loads(result)


def test_report_publishing_tool_registered() -> None:
    """Report publishing should be its own reusable tool surface."""
    metadata = builtin_tool_registry().entries["report_publishing"]

    assert metadata.display_name == "Report Publishing"
    assert metadata.consumes_workspace_paths is True
    assert metadata.function_names == (
        "publish_report",
        "revoke_public_report",
    )


@pytest.mark.parametrize("toolkit_class", [DynamicWorkflowTools, ReportPublishingTools])
def test_each_offered_function_runs_the_toolkit_method_of_its_name(
    toolkit_class: type[DynamicWorkflowTools | ReportPublishingTools],
) -> None:
    """Each tool is one method under its own name, the one entrypoint the toolkit offers for it."""
    toolkit = toolkit_class()

    assert toolkit.functions == {name: getattr(toolkit, name) for name in toolkit.functions}


@pytest.mark.asyncio
async def test_tool_file_work_leaves_the_event_loop_free(tmp_path: Path) -> None:
    """While a tool's file work, a static-site copy included, is under way, other coroutines on the loop keep running."""
    dynamic_workflow_tool = DynamicWorkflowTools()
    report_tool = ReportPublishingTools()
    context = _make_context(tmp_path, agent_memory_backend="file")
    site_dir = context.runtime_paths.storage_root / "agents" / "general" / "workspace" / "public-demo"
    site_dir.mkdir(parents=True)
    (site_dir / "index.html").write_text("<!doctype html><h1>Demo</h1>", encoding="utf-8")
    store_methods: dict[type, tuple[str, ...]] = {
        DynamicWorkflowStore: (
            "create_workflow",
            "update_workflow",
            "list_workflows",
            "list_workflow_revisions",
            "start_workflow_run",
            "load_workflow_revision",
            "complete_workflow_run",
            "get_workflow_run",
        ),
        ReportPublishingStore: ("publish_report", "get_public_report", "revoke_public_report"),
    }
    workflow_id = "competitor-research-report"

    with file_work_waiting_for_the_loop(store_methods) as freedom, tool_runtime_context(context):
        created = _tool_payload(await dynamic_workflow_tool.create_workflow(_workflow_spec(), reason="initial design"))
        updated = _tool_payload(
            await dynamic_workflow_tool.update_workflow(workflow_id, {"description": "Updated."}, "Clarify"),
        )
        listed = _tool_payload(await dynamic_workflow_tool.list_workflows())
        revisions = _tool_payload(await dynamic_workflow_tool.list_workflow_revisions(workflow_id))
        run = _tool_payload(await dynamic_workflow_tool.run_workflow(workflow_id, {"topic": "pricing"}))
        loaded_run = _tool_payload(await dynamic_workflow_tool.get_workflow_run(workflow_id, run["run_id"]))
        run_report = _tool_payload(
            await report_tool.publish_report(
                source_type="dynamic_workflow_run",
                source={"workflow_id": workflow_id, "run_id": run["run_id"]},
                confirm_public=True,
            ),
        )
        site = _tool_payload(
            await report_tool.publish_report(
                source_type="static_site",
                source={"path": "public-demo", "title": "Public Demo"},
                confirm_public=True,
            ),
        )
        revoked = _tool_payload(await report_tool.revoke_public_report(site["slug"]))

    assert freedom.blocked == []
    assert set(freedom.called) == {name for names in store_methods.values() for name in names}
    assert [created["status"], updated["status"], listed["status"], revisions["status"]] == ["ok"] * 4
    assert [run["status"], loaded_run["status"]] == ["completed"] * 2
    assert [run_report["status"], site["status"], revoked["status"]] == ["ok"] * 3


@pytest.mark.asyncio
async def test_a_publish_cancelled_while_it_runs_leaves_no_public_link(tmp_path: Path) -> None:
    """A publication whose call is cancelled is revoked off the loop, since its requester never sees the link."""
    report_tool = ReportPublishingTools()
    context = _make_context(tmp_path, agent_memory_backend="file")
    site_dir = context.runtime_paths.storage_root / "agents" / "general" / "workspace" / "public-demo"
    site_dir.mkdir(parents=True)
    (site_dir / "index.html").write_text("<!doctype html><h1>Demo</h1>", encoding="utf-8")
    loop = asyncio.get_running_loop()
    publishing_waits = asyncio.Event()
    release_publishing = threading.Event()
    publish_report = ReportPublishingStore.publish_report

    def waiting(*args: object, **kwargs: object) -> object:
        loop.call_soon_threadsafe(publishing_waits.set)
        release_publishing.wait(timeout=5)
        return publish_report(*args, **kwargs)

    with (
        patch.object(ReportPublishingStore, "publish_report", waiting),
        file_work_waiting_for_the_loop({ReportPublishingStore: ("revoke_public_report",)}) as freedom,
        tool_runtime_context(context),
    ):
        publishing = asyncio.create_task(
            report_tool.publish_report(
                source_type="static_site",
                source={"path": "public-demo", "title": "Public Demo"},
                confirm_public=True,
            ),
        )
        await publishing_waits.wait()
        publishing.cancel()
        release_publishing.set()
        with pytest.raises(asyncio.CancelledError):
            await publishing

    store = ReportPublishingStore(context.runtime_paths.storage_root)
    [record] = (context.runtime_paths.storage_root / "report_publishing" / "public_reports").glob("*.json")
    report = store.get_public_report(record.stem, include_revoked=True)
    assert (report.revoked_at is not None, report.revoked_by) == (True, "@user:localhost")
    with pytest.raises(ReportPublishingError, match="revoked"):
        store.get_public_report(record.stem)
    assert (freedom.called, freedom.blocked) == (["revoke_public_report"], [])


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
    html_path = store.report_asset_path(store.get_public_report(report.slug))
    revoked = store.revoke_public_report(report.slug, revoked_by="@alice:localhost")

    assert report.slug.startswith("pub_")
    assert report.public_url == f"https://acme.mindroom.chat/reports/public/{report.slug}"
    assert loaded.source_type == "test_report"
    assert loaded.source == {"id": "example"}
    assert html_path == report_path
    assert revoked.revoked_at is not None
    with pytest.raises(ReportPublishingError, match="revoked"):
        store.report_asset_path(store.get_public_report(report.slug))


def test_report_publishing_store_upgrades_legacy_html_record_on_revoke(tmp_path: Path) -> None:
    """A pre-artifact-kind record serves its file and rewrites without losing publication metadata."""
    storage_root = tmp_path / "mindroom_data"
    artifact_path = storage_root / "reports" / "legacy.html"
    artifact_path.parent.mkdir(parents=True)
    artifact_path.write_bytes(b"<!doctype html><h1>Legacy report</h1>\n")
    slug = "pub_00000000000000000000000000000000"
    record_path = storage_root / "report_publishing" / "public_reports" / f"{slug}.json"
    record_path.parent.mkdir(parents=True)
    record_path.write_text(
        json.dumps(
            {
                "slug": slug,
                "source_type": "dynamic_workflow_run",
                "source": {"workflow_id": "weekly-report", "run_id": "run-7"},
                "artifact_path": "reports/legacy.html",
                "title": "Weekly report",
                "requested_by": "@alice:localhost",
                "published_by": "@publisher:localhost",
                "published_at": "2026-06-20T12:00:00+00:00",
                "public_url": "https://example.test/reports/public/" + slug,
                "revoked_at": None,
                "revoked_by": None,
            },
        ),
        encoding="utf-8",
    )
    store = ReportPublishingStore(storage_root)

    loaded = store.get_public_report(slug)
    served_path = store.report_asset_path(loaded)
    revoked = store.revoke_public_report(slug, revoked_by="@admin:localhost")

    assert loaded.artifact_kind == "html_file"
    assert served_path == artifact_path
    assert served_path.read_bytes() == b"<!doctype html><h1>Legacy report</h1>\n"
    assert revoked.source_type == "dynamic_workflow_run"
    assert revoked.source == {"workflow_id": "weekly-report", "run_id": "run-7"}
    assert revoked.title == "Weekly report"
    assert revoked.requested_by == "@alice:localhost"
    assert revoked.published_by == "@publisher:localhost"
    assert revoked.published_at == "2026-06-20T12:00:00+00:00"
    assert revoked.public_url == "https://example.test/reports/public/" + slug

    persisted = json.loads(record_path.read_text(encoding="utf-8"))
    assert persisted["artifact_kind"] == "html_file"
    reopened = ReportPublishingStore(storage_root)
    reloaded = reopened.get_public_report(slug, include_revoked=True)
    assert reloaded == revoked
    assert reopened.report_asset_path(reloaded).read_bytes() == b"<!doctype html><h1>Legacy report</h1>\n"


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
        store.report_asset_path(store.get_public_report(report.slug))


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
            artifact_root=source_dir.parent,
        ),
        published_by="@alice:localhost",
        base_url="https://mindroom.lab.mindroom.chat",
    )
    (source_dir / "index.html").write_text("<!doctype html>changed", encoding="utf-8")

    index_path = store.report_asset_path(store.get_public_report(report.slug))
    script_path = store.report_asset_path(store.get_public_report(report.slug), "app.js")

    assert report.artifact_kind == "static_site"
    assert report.public_url == f"https://mindroom.lab.mindroom.chat/reports/public/{report.slug}/"
    assert index_path.read_text(encoding="utf-8").startswith("<!doctype html><script")
    assert script_path.read_text(encoding="utf-8") == "document.body.dataset.ready = 'true';"
    assert index_path.parent.is_relative_to(storage_root / "report_publishing" / "artifacts")


def test_report_publishing_store_copies_nested_static_site_directories(tmp_path: Path) -> None:
    """Nested site directories should be recreated with their files under the snapshot root."""
    storage_root = tmp_path / "mindroom_data"
    source_dir = tmp_path / "workspace" / "site"
    (source_dir / "assets" / "vendor").mkdir(parents=True)
    (source_dir / "empty").mkdir()
    (source_dir / "index.html").write_text("<!doctype html>Site", encoding="utf-8")
    (source_dir / "assets" / "app.js").write_text("document.body.dataset.ready = 'true';", encoding="utf-8")
    (source_dir / "assets" / "vendor" / "lib.js").write_text("export const lib = 1;", encoding="utf-8")
    store = ReportPublishingStore(storage_root)

    report = store.publish_report(
        source=PublishableReport(
            source_type="static_site",
            source={"path": "site"},
            artifact_path=source_dir,
            title="Nested Site",
            requested_by="@alice:localhost",
            artifact_kind="static_site",
            artifact_root=source_dir.parent,
        ),
        published_by="@alice:localhost",
        base_url="https://mindroom.lab.mindroom.chat",
    )

    loaded = store.get_public_report(report.slug)
    site_root = store.report_asset_path(loaded).parent
    assert store.report_asset_path(loaded, "assets/app.js").read_text(encoding="utf-8").startswith("document.body")
    assert (
        store.report_asset_path(loaded, "assets/vendor/lib.js").read_text(encoding="utf-8") == "export const lib = 1;"
    )
    assert (site_root / "empty").is_dir()
    assert sorted(path.relative_to(site_root).as_posix() for path in site_root.rglob("*")) == [
        "assets",
        "assets/app.js",
        "assets/vendor",
        "assets/vendor/lib.js",
        "empty",
        "index.html",
    ]


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
            artifact_root=page_path.parent,
        ),
        published_by="@alice:localhost",
        base_url="https://mindroom.lab.mindroom.chat",
    )

    index_path = store.report_asset_path(store.get_public_report(report.slug))
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
        patch("mindroom.report_publishing.static_site._copy_regular_file", side_effect=OSError("disk full")),
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
                artifact_root=page_path.parent,
            ),
            published_by="@alice:localhost",
            base_url="https://mindroom.lab.mindroom.chat",
        )

    artifacts_root = storage_root / "report_publishing" / "artifacts"
    assert not artifacts_root.exists() or list(artifacts_root.iterdir()) == []


@pytest.mark.asyncio
async def test_report_publishing_tool_reports_static_site_copy_failure(tmp_path: Path) -> None:
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
        patch("mindroom.report_publishing.static_site._copy_regular_file", side_effect=OSError("disk full")),
        tool_runtime_context(context),
    ):
        published = _tool_payload(
            await report_tool.publish_report(
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
                artifact_root=page_path.parent,
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
                artifact_root=source_dir.parent,
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
                artifact_root=source_dir.parent,
            ),
            published_by="@alice:localhost",
            base_url="https://mindroom.lab.mindroom.chat",
        )


def test_report_publishing_store_rejects_static_site_symlink_swapped_after_listing(tmp_path: Path) -> None:
    """An entry swapped to a symlink after the directory listing must never be copied."""
    storage_root = tmp_path / "mindroom_data"
    source_dir = tmp_path / "workspace" / "site"
    secret_file = tmp_path / "secret.txt"
    source_dir.mkdir(parents=True)
    secret_file.write_text("matrix-access-token", encoding="utf-8")
    (source_dir / "index.html").write_text("<!doctype html>Site", encoding="utf-8")
    (source_dir / "zzz.js").write_text("console.log('ok')", encoding="utf-8")
    store = ReportPublishingStore(storage_root)
    real_listdir = os.listdir
    swapped: list[bool] = []

    def swapping_listdir(directory_fd: int) -> list[str]:
        names = real_listdir(directory_fd)
        if not swapped:
            swapped.append(True)
            (source_dir / "zzz.js").unlink()
            (source_dir / "zzz.js").symlink_to(secret_file)
        return names

    with (
        patch("mindroom.report_publishing.static_site.os.listdir", swapping_listdir),
        pytest.raises(ReportPublishingError, match="symlink"),
    ):
        store.publish_report(
            source=PublishableReport(
                source_type="static_site",
                source={"path": "site"},
                artifact_path=source_dir,
                title="Raced Site",
                requested_by="@alice:localhost",
                artifact_kind="static_site",
                artifact_root=source_dir.parent,
            ),
            published_by="@alice:localhost",
            base_url="https://mindroom.lab.mindroom.chat",
        )

    assert swapped == [True]
    artifacts_root = storage_root / "report_publishing" / "artifacts"
    assert not artifacts_root.exists() or list(artifacts_root.iterdir()) == []
    assert not (storage_root / "report_publishing" / "public_reports").exists()


def test_report_publishing_store_rejects_single_page_symlink(tmp_path: Path) -> None:
    """A single-page source that is a symlink must fail instead of copying its target."""
    storage_root = tmp_path / "mindroom_data"
    workspace_root = tmp_path / "workspace"
    secret_file = tmp_path / "secret.txt"
    workspace_root.mkdir(parents=True)
    secret_file.write_text("matrix-access-token", encoding="utf-8")
    (workspace_root / "report.html").symlink_to(secret_file)
    store = ReportPublishingStore(storage_root)

    with pytest.raises(ReportPublishingError, match="symlink"):
        store.publish_report(
            source=PublishableReport(
                source_type="static_site",
                source={"path": "report.html"},
                artifact_path=workspace_root / "report.html",
                title="Linked Page",
                requested_by="@alice:localhost",
                artifact_kind="static_site",
                artifact_root=workspace_root,
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
            artifact_root=source_dir.parent,
        ),
        published_by="@alice:localhost",
        base_url="https://mindroom.lab.mindroom.chat",
    )

    with pytest.raises(ReportPublishingError, match="asset path is invalid"):
        store.report_asset_path(store.get_public_report(report.slug), "../index.html")


@pytest.mark.asyncio
@pytest.mark.parametrize("confirm_public", [False, "no", "0", "false"])
async def test_a_model_sending_a_false_like_confirmation_does_not_publish(
    tmp_path: Path,
    confirm_public: bool | str,
) -> None:
    """The model's arguments are validated as their declared types, so a string "no" means False."""
    dynamic_workflow_tool = DynamicWorkflowTools()
    publish = toolkit_offers(ReportPublishingTools(), "publish_report").entrypoint
    context = _make_context(tmp_path)

    with tool_runtime_context(context):
        _tool_payload(await dynamic_workflow_tool.create_workflow(_workflow_spec(), reason="initial design"))
        run = _tool_payload(
            await dynamic_workflow_tool.run_workflow("competitor-research-report", {"topic": "Agno factories"}),
        )
        result = _tool_payload(
            await publish(
                source_type="dynamic_workflow_run",
                source={"workflow_id": "competitor-research-report", "run_id": run["run_id"]},
                confirm_public=confirm_public,
            ),
        )

    assert result["status"] == "error"
    assert "confirm_public" in result["message"]
    assert list((context.runtime_paths.storage_root / "report_publishing").rglob("*")) == []


@pytest.mark.asyncio
async def test_a_model_sending_a_non_string_slug_is_refused_before_the_revoke_runs() -> None:
    """Revoking validates its argument too, so a wrong type fails as a validation error."""
    revoke = toolkit_offers(ReportPublishingTools(), "revoke_public_report").entrypoint

    with pytest.raises(ValidationError, match="slug"):
        await revoke(slug=123)


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("public_url", "link_base"),
    [
        ("https://acme.mindroom.chat/mindroom", "https://acme.mindroom.chat/mindroom"),
        (" https://acme.mindroom.chat/mindroom/ ", "https://acme.mindroom.chat/mindroom"),
        ("", None),
    ],
    ids=["public-url", "padded-public-url", "no-public-url"],
)
async def test_report_publishing_tool_publishes_dynamic_workflow_run_report(
    tmp_path: Path,
    public_url: str,
    link_base: str | None,
) -> None:
    """Report Publishing should expose Dynamic Workflow reports through a source reference."""
    dynamic_workflow_tool = DynamicWorkflowTools()
    report_tool = ReportPublishingTools()
    context = _make_context(tmp_path, public_url=public_url)

    with tool_runtime_context(context):
        _tool_payload(await dynamic_workflow_tool.create_workflow(_workflow_spec(), reason="initial design"))
        run = _tool_payload(
            await dynamic_workflow_tool.run_workflow("competitor-research-report", {"topic": "Agno factories"}),
        )
        with_scope = _tool_payload(
            await report_tool.publish_report(
                source_type="dynamic_workflow_run",
                source={"workflow_id": "competitor-research-report", "run_id": run["run_id"], "scope": "agent"},
                confirm_public=True,
            ),
        )
        published = _tool_payload(
            await report_tool.publish_report(
                source_type="dynamic_workflow_run",
                source={"workflow_id": "competitor-research-report", "run_id": run["run_id"]},
                confirm_public=True,
            ),
        )
        revoked = _tool_payload(await report_tool.revoke_public_report(published["slug"]))

    assert with_scope["message"] == "dynamic_workflow_run source contains unsupported field 'scope'."
    assert published["status"] == "ok"
    assert published["source_type"] == "dynamic_workflow_run"
    assert published["source"] == {
        "workflow_id": "competitor-research-report",
        "run_id": run["run_id"],
    }
    report_path = f"/reports/private/agent/general/competitor-research-report/{run['run_id']}"
    assert run["report_url"] == (None if link_base is None else f"{link_base}{report_path}")
    assert published["public_url"] == (None if link_base is None else f"{link_base}/reports/public/{published['slug']}")
    assert published["public_path"] == (
        f"/reports/public/{published['slug']}" if link_base is None else f"/mindroom/reports/public/{published['slug']}"
    )
    assert revoked["status"] == "ok"
    assert revoked["revoked_at"] is not None


@pytest.mark.asyncio
async def test_report_publishing_tool_publishes_workspace_static_site(tmp_path: Path) -> None:
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
            await report_tool.publish_report(
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


@pytest.mark.asyncio
async def test_report_publishing_tool_publishes_workspace_single_html_page(tmp_path: Path) -> None:
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
            await report_tool.publish_report(
                source_type="static_site",
                source={"path": "report.html", "title": "Single Page"},
                confirm_public=True,
            ),
        )

    assert published["status"] == "ok"
    assert published["source"] == {"path": "report.html"}
    assert published["public_url"] == f"https://mindroom.lab.mindroom.chat/reports/public/{published['slug']}/"


@pytest.mark.asyncio
async def test_report_publishing_tool_requires_workspace_for_static_site(tmp_path: Path) -> None:
    """Static site publishing should require an agent workspace."""
    report_tool = ReportPublishingTools()
    context = _make_context(tmp_path)

    with tool_runtime_context(context):
        published = _tool_payload(
            await report_tool.publish_report(
                source_type="static_site",
                source={"path": "public-demo", "title": "Public Demo"},
                confirm_public=True,
            ),
        )

    assert published["status"] == "error"
    assert "agent workspace" in published["message"]


@pytest.mark.asyncio
async def test_report_publishing_tool_rejects_static_site_path_escape(tmp_path: Path) -> None:
    """Static site path input should stay workspace-relative."""
    report_tool = ReportPublishingTools()
    context = _make_context(tmp_path, agent_memory_backend="file")
    workspace_root = context.runtime_paths.storage_root / "agents" / "general" / "workspace"
    workspace_root.mkdir(parents=True)

    with tool_runtime_context(context):
        escaped = _tool_payload(
            await report_tool.publish_report(
                source_type="static_site",
                source={"path": "../outside", "title": "Escape"},
                confirm_public=True,
            ),
        )

    assert escaped["status"] == "error"
    assert "workspace root" in escaped["message"]


@pytest.mark.asyncio
async def test_report_publishing_tool_rejects_arbitrary_sources(tmp_path: Path) -> None:
    """Report Publishing should publish only registered authorized source types."""
    report_tool = ReportPublishingTools()
    context = _make_context(tmp_path)

    with tool_runtime_context(context):
        unsupported = _tool_payload(
            await report_tool.publish_report(
                source_type="file_path",
                source={"path": "unregistered/report.html"},
                confirm_public=True,
            ),
        )
        extra_field = _tool_payload(
            await report_tool.publish_report(
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


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("source", "message"),
    [
        ({"title": "Demo"}, "static_site source field 'path' is missing."),
        ({"path": "   ", "title": "Demo"}, "static_site source field 'path' must be a non-empty string."),
        ({"path": "demo", "title": 7}, "static_site source field 'title' must be a non-empty string."),
    ],
)
async def test_report_publishing_tool_requires_nonblank_source_text(
    tmp_path: Path,
    source: dict[str, object],
    message: str,
) -> None:
    """Each required source field must be present and hold nonblank text."""
    report_tool = ReportPublishingTools()
    with tool_runtime_context(_make_context(tmp_path)):
        result = _tool_payload(
            await report_tool.publish_report(source_type="static_site", source=source, confirm_public=True),
        )

    assert result["status"] == "error"
    assert result["message"] == message


@pytest.mark.asyncio
async def test_report_publishing_tool_rejects_failed_dynamic_workflow_runs(tmp_path: Path) -> None:
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
        store, owner_id = dynamic_workflow_store_and_owner(context)
        store.create_workflow(
            spec=parse_workflow_spec(spec),
            scope="agent",
            owner_id=owner_id,
            created_by=context.agent_name or "general",
            reason="initial design",
        )
        run = await DynamicWorkflowService(store).run_workflow(
            workflow_id="competitor-research-report",
            scope="agent",
            owner_id=owner_id,
            input_data={"topic": "Agno factories"},
            requested_by=context.requester_id,
        )
        rejected = _tool_payload(
            await report_tool.publish_report(
                source_type="dynamic_workflow_run",
                source={"workflow_id": "competitor-research-report", "run_id": run.run_id},
                confirm_public=True,
            ),
        )

    assert run.status == "failed"
    assert rejected["status"] == "error"
    assert "Only completed" in rejected["message"]


@pytest.mark.asyncio
async def test_report_publishing_tool_denies_revoke_for_different_requester(tmp_path: Path) -> None:
    """Public report revocation should stay limited to the source requester or publisher."""
    dynamic_workflow_tool = DynamicWorkflowTools()
    report_tool = ReportPublishingTools()
    alice_context = _make_context(tmp_path)
    bob_context = replace(alice_context, requester_id="@bob:localhost")

    with tool_runtime_context(alice_context):
        _tool_payload(await dynamic_workflow_tool.create_workflow(_workflow_spec(), reason="initial design"))
        run = _tool_payload(
            await dynamic_workflow_tool.run_workflow("competitor-research-report", {"topic": "Agno factories"}),
        )
        published = _tool_payload(
            await report_tool.publish_report(
                source_type="dynamic_workflow_run",
                source={"workflow_id": "competitor-research-report", "run_id": run["run_id"]},
                confirm_public=True,
            ),
        )
    with tool_runtime_context(bob_context):
        revoked = _tool_payload(await report_tool.revoke_public_report(published["slug"]))

    assert revoked["status"] == "error"
    assert "not available to the current requester" in revoked["message"]
