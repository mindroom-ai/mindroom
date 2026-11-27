"""Report publishing tools for MindRoom agents."""

from __future__ import annotations

import html
import json
from functools import partial
from typing import TYPE_CHECKING, Any
from urllib.parse import urlsplit

from mindroom.background_tasks import run_blocking_or_compensate, run_blocking_until_complete
from mindroom.custom_tools.tool_payloads import custom_tool_payload, tool_context_or_error
from mindroom.origins import configured_public_url, published_reports_require_sign_in
from mindroom.report_publishing.store import (
    ARTIFACT_KIND_STATIC_SITE,
    PublishableReport,
    PublishedReport,
    ReportPublishingError,
    ReportPublishingStore,
)
from mindroom.tool_system.authoring import Toolkit, tool
from mindroom.workflow_runs.models import WorkflowRunState
from mindroom.workflow_runs.store import (
    WorkflowRunNotFoundError,
    WorkflowRunStore,
    WorkflowRunStoreError,
    owned_run,
)
from mindroom.workspaces import resolve_workspace_relative_path

if TYPE_CHECKING:
    from mindroom.tool_system.runtime_context import ToolRuntimeContext

_WORKFLOW_RUN_SOURCE_KEYS = frozenset({"run_id", "title"})
_STATIC_SITE_SOURCE_KEYS = frozenset({"path", "title"})


class ReportPublishingTools(Toolkit):
    """Tools that publish authorized report artifacts through revocable public links."""

    offer_output_path = False

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
        """Publish an authorized report source through a revocable link.

        Args:
            source_type: Either workflow_run or static_site.
            source: For workflow_run, run_id of a completed run and optional title; for static_site, path
                (workspace-relative, to a directory with index.html or to one HTML file) and title.
            confirm_public: Must be true; until it is revoked, anyone with the link can open the report, or only
                people signed in to this deployment where it requires sign-in (the result's who_can_open says which).

        """
        if isinstance(context := tool_context_or_error(self._payload, "Report Publishing"), str):
            return context
        if not confirm_public:
            return self._payload(
                "error",
                source_type=source_type,
                message="Set confirm_public to true to publish this report as a public link.",
            )
        # Read before publishing, so a setting MindRoom does not know fails the call without leaving a link behind.
        who_can_open = (
            "people signed in to this deployment"
            if published_reports_require_sign_in(context.runtime_paths)
            else "anyone with the link"
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
        except ReportPublishingError as exc:
            return self._payload("error", source_type=source_type, message=str(exc))
        return self._payload(
            "ok",
            source_type=report.source_type,
            source=report.source,
            slug=report.slug,
            public_url=report.public_url,
            public_path=_public_path_for_report(report),
            published_at=report.published_at,
            who_can_open=who_can_open,
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
    if normalized_source_type == "workflow_run":
        return _resolve_workflow_run_source(context, source)
    if normalized_source_type == "dynamic_workflow_run":
        msg = (
            "Report source_type 'dynamic_workflow_run' was removed with Dynamic Workflows; "
            "use 'workflow_run' with a completed workflow run_id instead."
        )
        raise ReportPublishingError(msg)
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


def _resolve_workflow_run_source(
    context: ToolRuntimeContext,
    source: dict[str, Any],
) -> PublishableReport:
    _reject_unsupported_source_fields(source, _WORKFLOW_RUN_SOURCE_KEYS, "workflow_run source")
    run_id = _required_source_text(source, "run_id", context="workflow_run source")
    explicit_title = _optional_source_text(source, "title", context="workflow_run source")
    store = _workflow_run_store(context, run_id)
    try:
        run = owned_run(store, run_id, agent_name=context.agent_name, requester_id=context.requester_id)
    except WorkflowRunStoreError as exc:
        raise ReportPublishingError(str(exc)) from exc
    if run.state is not WorkflowRunState.COMPLETED:
        msg = "Only completed workflow runs can be published."
        raise ReportPublishingError(msg)
    if run.result is None:
        msg = "Workflow run completed with no result to publish."
        raise ReportPublishingError(msg)
    run_title = run.title.strip() if isinstance(run.title, str) and run.title.strip() else None
    title = explicit_title or run_title or f"Workflow Run {run.run_id}"
    body = (
        run.result
        if isinstance(run.result, str)
        else json.dumps(run.result, indent=2, sort_keys=True, ensure_ascii=False)
    )
    return PublishableReport(
        source_type="workflow_run",
        source={"run_id": run.run_id},
        title=title,
        requested_by=run.owner_user_id,
        html_content=_render_report_html(title=title, body=body),
    )


def _workflow_run_store(context: ToolRuntimeContext, run_id: str) -> WorkflowRunStore:
    if context.orchestrator is not None:
        return context.orchestrator.workflow_engine.store
    control_state_root = context.runtime_paths.control_state_root
    if control_state_root is None:
        msg = "Workflow runs are unavailable without a control state root."
        raise ReportPublishingError(msg)
    db_path = control_state_root / "workflow_runs" / "workflow_runs.sqlite3"
    if not db_path.is_file():
        raise ReportPublishingError(str(WorkflowRunNotFoundError(run_id)))
    return WorkflowRunStore(db_path)


def _render_report_html(*, title: str, body: str) -> str:
    """Render a self-contained HTML report page with escaped title and body."""
    escaped_title = html.escape(title)
    escaped_body = html.escape(body)
    return (
        "<!doctype html>\n"
        '<html lang="en">\n'
        "<head>\n"
        '<meta charset="utf-8">\n'
        '<meta name="viewport" content="width=device-width, initial-scale=1">\n'
        f"<title>{escaped_title}</title>\n"
        "<style>\n"
        "body{font-family:system-ui,-apple-system,BlinkMacSystemFont,'Segoe UI',sans-serif;"
        "max-width:840px;margin:0 auto;padding:32px;line-height:1.55;color:#202124;}\n"
        "main{border-top:1px solid #d8dee4;padding-top:24px;}\n"
        "pre{white-space:pre-wrap;background:#f6f8fa;padding:16px;border-radius:6px;}\n"
        "</style>\n"
        "</head>\n"
        "<body>\n"
        f"<h1>{escaped_title}</h1>\n"
        "<main>\n"
        f"<pre>{escaped_body}</pre>\n"
        "</main>\n"
        "</body>\n"
        "</html>\n"
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


def _optional_source_text(source: dict[str, object], key: str, *, context: str) -> str | None:
    if key not in source or source[key] is None:
        return None
    value = source[key]
    if not isinstance(value, str) or not value.strip():
        msg = f"{context} field '{key}' must be a non-empty string."
        raise ReportPublishingError(msg)
    return value.strip()
