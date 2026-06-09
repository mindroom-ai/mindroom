"""Report publishing tools for MindRoom agents."""

from __future__ import annotations

from typing import Any
from urllib.parse import urlsplit

from agno.tools import Toolkit

from mindroom.custom_tools.dynamic_workflow_context import (
    authorize_dynamic_workflow_run,
    dynamic_workflow_store_and_owner,
)
from mindroom.custom_tools.tool_payloads import custom_tool_payload
from mindroom.custom_tools.toolkit_functions import JSON_OBJECT_SCHEMA, register_toolkit_functions
from mindroom.dynamic_workflows.store import DynamicWorkflowError
from mindroom.report_publishing.store import (
    PublishableReport,
    PublishedReport,
    ReportPublishingError,
    ReportPublishingStore,
)
from mindroom.runtime_resolution import resolve_agent_runtime
from mindroom.tool_system.runtime_context import (
    ToolRuntimeContext,
    build_execution_identity_from_runtime_context,
    get_tool_runtime_context,
)
from mindroom.workspaces import resolve_workspace_relative_path

_DYNAMIC_WORKFLOW_RUN_SOURCE_KEYS = frozenset({"workflow_id", "run_id", "scope"})
_STATIC_SITE_SOURCE_KEYS = frozenset({"path", "title"})


_TOOL_DESCRIPTIONS = {
    "publish_report": (
        "Publish an authorized report source through a revocable public link. "
        "Supports source_type dynamic_workflow_run and static_site."
    ),
    "revoke_public_report": "Revoke a previously published public report link.",
}


_TOOL_PARAMETERS: dict[str, dict[str, object]] = {
    "publish_report": {
        "type": "object",
        "properties": {
            "source_type": {"type": "string"},
            "source": JSON_OBJECT_SCHEMA,
            "confirm_public": {"type": "boolean"},
        },
        "required": ["source_type", "source", "confirm_public"],
    },
    "revoke_public_report": {
        "type": "object",
        "properties": {"slug": {"type": "string"}},
        "required": ["slug"],
    },
}


class ReportPublishingTools(Toolkit):
    """Tools that publish authorized report artifacts through revocable public links."""

    def __init__(self) -> None:
        super().__init__(name="report_publishing", tools=[])
        self._register_functions()

    def _register_functions(self) -> None:
        register_toolkit_functions(
            self,
            sync_entrypoints={
                "publish_report": self.publish_report,
                "revoke_public_report": self.revoke_public_report,
            },
            async_entrypoints={
                "publish_report": self.apublish_report,
                "revoke_public_report": self.arevoke_public_report,
            },
            descriptions=_TOOL_DESCRIPTIONS,
            parameters=_TOOL_PARAMETERS,
        )

    @staticmethod
    def _payload(status: str, **fields: object) -> str:
        return custom_tool_payload("report_publishing", status, **fields)

    @classmethod
    def _context_error(cls) -> str:
        return cls._payload(
            "error",
            message="Report Publishing tool context is unavailable in this runtime path.",
        )

    def publish_report(
        self,
        source_type: str,
        source: dict[str, Any],
        confirm_public: bool,
    ) -> str:
        """Publish an authorized report artifact through a revocable public link."""
        context = get_tool_runtime_context()
        if context is None:
            return self._context_error()
        if not confirm_public:
            return self._payload(
                "error",
                source_type=source_type,
                message="Set confirm_public to true to publish this report as a public link.",
            )
        try:
            publishable = _resolve_publishable_source(context, source_type, source)
            report = ReportPublishingStore(context.runtime_paths.storage_root).publish_report(
                source=publishable,
                published_by=context.requester_id,
                base_url=context.runtime_paths.env_value("MINDROOM_PUBLIC_URL"),
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

    def revoke_public_report(self, slug: str) -> str:
        """Revoke a previously published public report link."""
        context = get_tool_runtime_context()
        if context is None:
            return self._context_error()
        try:
            store = ReportPublishingStore(context.runtime_paths.storage_root)
            report = store.get_public_report(slug, include_revoked=True)
            _authorize_public_report_for_context(context, report)
            revoked = store.revoke_public_report(slug, revoked_by=context.requester_id)
        except ReportPublishingError as exc:
            return self._payload("error", slug=slug, message=str(exc))
        return self._payload(
            "ok",
            slug=revoked.slug,
            source_type=revoked.source_type,
            source=revoked.source,
            revoked_at=revoked.revoked_at,
        )

    async def apublish_report(
        self,
        source_type: str,
        source: dict[str, Any],
        confirm_public: bool,
    ) -> str:
        """Publish an authorized report artifact through a revocable public link."""
        return self.publish_report(source_type, source, confirm_public)

    async def arevoke_public_report(self, slug: str) -> str:
        """Revoke a previously published public report link."""
        return self.revoke_public_report(slug)


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
    workspace = resolve_agent_runtime(
        context.agent_name,
        context.config,
        context.runtime_paths,
        build_execution_identity_from_runtime_context(context),
    ).workspace
    if workspace is None:
        msg = "static_site publishing requires an agent workspace in this runtime path."
        raise ReportPublishingError(msg)
    try:
        site_dir = resolve_workspace_relative_path(
            workspace.root,
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


def _resolve_dynamic_workflow_run_source(
    context: ToolRuntimeContext,
    source: dict[str, Any],
) -> PublishableReport:
    _reject_unsupported_source_fields(source, _DYNAMIC_WORKFLOW_RUN_SOURCE_KEYS, "dynamic_workflow_run source")
    workflow_id = _required_source_text(source, "workflow_id", context="dynamic_workflow_run source")
    run_id = _required_source_text(source, "run_id", context="dynamic_workflow_run source")
    scope = _optional_source_text(source, "scope", default="agent", context="dynamic_workflow_run source")
    store, owner_id = dynamic_workflow_store_and_owner(context, scope)
    run = store.get_workflow_run(
        workflow_id=workflow_id,
        scope=scope,
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
            "scope": scope,
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
    suffix = "/" if report.artifact_kind == "static_site" else ""
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


def _optional_source_text(
    source: dict[str, object],
    key: str,
    *,
    default: str,
    context: str,
) -> str:
    value = source.get(key, default)
    if not isinstance(value, str) or not value.strip():
        msg = f"{context} field '{key}' must be a non-empty string."
        raise ReportPublishingError(msg)
    return value.strip()
