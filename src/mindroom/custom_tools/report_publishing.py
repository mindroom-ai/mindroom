"""Report publishing tools for MindRoom agents."""

from __future__ import annotations

from functools import partial
from typing import TYPE_CHECKING, Any
from urllib.parse import urlsplit

from mindroom.background_tasks import run_blocking_or_compensate, run_blocking_until_complete
from mindroom.custom_tools.dynamic_workflow_context import (
    authorize_dynamic_workflow_run,
    dynamic_workflow_store_and_owner,
)
from mindroom.custom_tools.tool_payloads import custom_tool_payload, tool_context_or_error
from mindroom.dynamic_workflows.spec import DynamicWorkflowError
from mindroom.dynamic_workflows.store import AGENT_SCOPE
from mindroom.origins import configured_public_url
from mindroom.report_publishing.store import (
    ARTIFACT_KIND_STATIC_SITE,
    PublishableReport,
    PublishedReport,
    ReportPublishingError,
    ReportPublishingStore,
)
from mindroom.tool_system.authoring import Toolkit, tool
from mindroom.workspaces import resolve_workspace_relative_path

if TYPE_CHECKING:
    from mindroom.tool_system.runtime_context import ToolRuntimeContext

_DYNAMIC_WORKFLOW_RUN_SOURCE_KEYS = frozenset({"workflow_id", "run_id"})
_STATIC_SITE_SOURCE_KEYS = frozenset({"path", "title"})


class ReportPublishingTools(Toolkit):
    """Tools that publish authorized report artifacts through revocable public links."""

    def __init__(self) -> None:
        super().__init__(name="report_publishing")

    @staticmethod
    def _payload(status: str, **fields: object) -> str:
        return custom_tool_payload("report_publishing", status, **fields)

    @tool
    async def publish_report(
        self,
        source_type: str,
        source: dict[str, Any],
        confirm_public: bool,
    ) -> str:
        """Publish an authorized report source through a revocable public link.

        Args:
            source_type: Either dynamic_workflow_run or static_site.
            source: For dynamic_workflow_run, workflow_id and run_id of a completed run; for static_site, path
                (workspace-relative, to a directory with index.html or to one HTML file) and title.
            confirm_public: Must be true; anyone with the link can open the report until it is revoked.

        """
        if isinstance(context := tool_context_or_error(self._payload, "Report Publishing"), str):
            return context
        if not confirm_public:
            return self._payload(
                "error",
                source_type=source_type,
                message="Set confirm_public to true to publish this report as a public link.",
            )
        try:
            publishable = await run_blocking_until_complete(_resolve_publishable_source, context, source_type, source)
            store = ReportPublishingStore(context.runtime_paths.storage_root)
            # A cancelled call never shows its link, so its requester could neither use nor revoke it.
            report = await run_blocking_or_compensate(
                partial(
                    store.publish_report,
                    source=publishable,
                    published_by=context.requester_id,
                    base_url=configured_public_url(context.runtime_paths),
                ),
                lambda published: store.revoke_public_report(published.slug, revoked_by=context.requester_id),
            )
        except (DynamicWorkflowError, ReportPublishingError) as exc:
            return self._payload("error", source_type=source_type, message=str(exc))
        return self._payload(
            "ok",
            source_type=report.source_type,
            source=report.source,
            slug=report.slug,
            public_url=report.public_url,
            public_path=_public_path_for_report(report),
            published_at=report.published_at,
        )

    @tool
    async def revoke_public_report(self, slug: str) -> str:
        """Revoke a previously published public report link."""
        if isinstance(context := tool_context_or_error(self._payload, "Report Publishing"), str):
            return context
        try:
            store = ReportPublishingStore(context.runtime_paths.storage_root)
            report = await run_blocking_until_complete(partial(store.get_public_report, slug, include_revoked=True))
            _authorize_public_report_for_context(context, report)
            revoked = await run_blocking_until_complete(
                partial(store.revoke_public_report, slug, revoked_by=context.requester_id),
            )
        except ReportPublishingError as exc:
            return self._payload("error", slug=slug, message=str(exc))
        return self._payload(
            "ok",
            slug=revoked.slug,
            source_type=revoked.source_type,
            source=revoked.source,
            revoked_at=revoked.revoked_at,
        )


def _resolve_publishable_source(
    context: ToolRuntimeContext,
    source_type: str,
    source: dict[str, Any],
) -> PublishableReport:
    normalized_source_type = source_type.strip()
    if normalized_source_type == "dynamic_workflow_run":
        return _resolve_dynamic_workflow_run_source(context, source)
    if normalized_source_type == "static_site":
        return _resolve_static_site_source(context, source)
    msg = f"Unsupported report source_type '{source_type}'."
    raise ReportPublishingError(msg)


def _resolve_static_site_source(
    context: ToolRuntimeContext,
    source: dict[str, Any],
) -> PublishableReport:
    _reject_unsupported_source_fields(source, _STATIC_SITE_SOURCE_KEYS, "static_site source")
    source_path = _required_source_text(source, "path", context="static_site source")
    title = _required_source_text(source, "title", context="static_site source")
    workspace_root = context.agent_workspace_root()
    if not workspace_root.is_dir():
        msg = "static_site publishing found no agent workspace yet; write the site into the workspace first."
        raise ReportPublishingError(msg)
    try:
        site_dir = resolve_workspace_relative_path(
            workspace_root,
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
        artifact_kind=ARTIFACT_KIND_STATIC_SITE,
        artifact_root=workspace_root,
    )


def _resolve_dynamic_workflow_run_source(
    context: ToolRuntimeContext,
    source: dict[str, Any],
) -> PublishableReport:
    _reject_unsupported_source_fields(source, _DYNAMIC_WORKFLOW_RUN_SOURCE_KEYS, "dynamic_workflow_run source")
    workflow_id = _required_source_text(source, "workflow_id", context="dynamic_workflow_run source")
    run_id = _required_source_text(source, "run_id", context="dynamic_workflow_run source")
    store, owner_id = dynamic_workflow_store_and_owner(context)
    run = store.get_workflow_run(
        workflow_id=workflow_id,
        scope=AGENT_SCOPE,
        owner_id=owner_id,
        run_id=run_id,
    )
    authorize_dynamic_workflow_run(context, run)
    if run.status != "completed":
        msg = "Only completed Dynamic Workflow runs can be published."
        raise ReportPublishingError(msg)
    return PublishableReport(
        source_type="dynamic_workflow_run",
        source={
            "workflow_id": workflow_id,
            "run_id": run_id,
        },
        artifact_path=store.run_report_html_artifact_path(run),
        title=store.run_report_title(run),
        requested_by=run.requested_by,
    )


def _authorize_public_report_for_context(context: ToolRuntimeContext, report: PublishedReport) -> None:
    if context.requester_id in {report.requested_by, report.published_by}:
        return
    msg = "Public report is not available to the current requester."
    raise ReportPublishingError(msg)


def _public_path_for_report(report: PublishedReport) -> str:
    if report.public_url is not None:
        public_path = urlsplit(report.public_url).path
        if public_path:
            return public_path
    suffix = "/" if report.is_static_site else ""
    return f"/reports/public/{report.slug}{suffix}"


def _reject_unsupported_source_fields(
    source: dict[str, object],
    allowed_fields: frozenset[str],
    context: str,
) -> None:
    unsupported_fields = sorted(set(source) - allowed_fields)
    if unsupported_fields:
        msg = f"{context} contains unsupported field '{unsupported_fields[0]}'."
        raise ReportPublishingError(msg)


def _required_source_text(source: dict[str, object], key: str, *, context: str) -> str:
    if key not in source:
        msg = f"{context} field '{key}' is missing."
        raise ReportPublishingError(msg)
    value = source[key]
    if not isinstance(value, str) or not value.strip():
        msg = f"{context} field '{key}' must be a non-empty string."
        raise ReportPublishingError(msg)
    return value.strip()
