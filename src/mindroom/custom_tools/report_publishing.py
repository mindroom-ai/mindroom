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
from mindroom.dynamic_workflows.validation import DynamicWorkflowError
from mindroom.entity_resolution import (
    DuplicateManagedEntityIdentityError,
    MissingManagedEntityAccountError,
    entity_identity_registry,
)
from mindroom.report_access_policy import ReportAccessPolicy
from mindroom.report_publishing.store import (
    ARTIFACT_KIND_STATIC_SITE,
    PublishableReport,
    PublishedReport,
    ReportPublishingError,
    ReportPublishingStore,
    report_route_path,
)
from mindroom.report_viewer_auth import report_viewer_auth_configuration_error
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
        "Publish an authorized report source through a revocable public or origin-room link. "
        "Public links require confirm_public=true. Supports source_type dynamic_workflow_run and static_site."
    ),
    "revoke_public_report": "Revoke a previously published report link under either access policy.",
}


_TOOL_PARAMETERS: dict[str, dict[str, object]] = {
    "publish_report": {
        "type": "object",
        "properties": {
            "source_type": {"type": "string"},
            "source": JSON_OBJECT_SCHEMA,
            "confirm_public": {"type": "boolean"},
            "access_policy": {
                "type": ["string", "null"],
                "enum": [ReportAccessPolicy.PUBLIC.value, ReportAccessPolicy.ORIGIN_ROOM.value, None],
            },
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
    """Tools that publish authorized report artifacts through revocable links."""

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
        access_policy: str | None = None,
    ) -> str:
        """Publish an authorized report artifact through a revocable link."""
        context = get_tool_runtime_context()
        if context is None:
            return self._context_error()
        try:
            resolved_access_policy = _resolve_access_policy(context, access_policy)
        except ReportPublishingError as exc:
            return self._payload("error", source_type=source_type, message=str(exc))
        policy_error = _publication_policy_error(context, resolved_access_policy, confirm_public=confirm_public)
        if policy_error is not None:
            return self._payload(
                "error",
                source_type=source_type,
                message=policy_error,
            )
        try:
            origin_metadata = _origin_room_metadata(context, resolved_access_policy)
            publishable = _resolve_publishable_source(context, source_type, source)
            report = ReportPublishingStore(context.runtime_paths.storage_root).publish_report(
                source=publishable,
                published_by=context.requester_id,
                base_url=context.runtime_paths.env_value("MINDROOM_PUBLIC_URL"),
                access_policy=resolved_access_policy,
                **origin_metadata,
            )
        except (
            DuplicateManagedEntityIdentityError,
            DynamicWorkflowError,
            MissingManagedEntityAccountError,
            ReportPublishingError,
        ) as exc:
            return self._payload("error", source_type=source_type, message=str(exc))
        access_message = (
            "Anyone who possesses this public bearer link can view the report."
            if report.access_policy is ReportAccessPolicy.PUBLIC
            else "Access is limited to authenticated Matrix users currently joined to the origin room."
        )
        report_path = _report_path_for_report(report)
        legacy_public_fields = (
            {
                "public_url": report.public_url,
                "public_path": report_path,
            }
            if report.access_policy is ReportAccessPolicy.PUBLIC
            else {}
        )
        return self._payload(
            "ok",
            source_type=report.source_type,
            source=report.source,
            slug=report.slug,
            access_policy=report.access_policy.value,
            report_url=report.public_url,
            report_path=report_path,
            message=access_message,
            published_at=report.published_at,
            **legacy_public_fields,
        )

    def revoke_public_report(self, slug: str) -> str:
        """Revoke a previously published report link under either access policy."""
        context = get_tool_runtime_context()
        if context is None:
            return self._context_error()
        try:
            store = ReportPublishingStore(context.runtime_paths.storage_root)
            report = store.get_report(slug, include_revoked=True)
            _authorize_report_for_context(context, report)
            revoked = store.revoke_report(slug, revoked_by=context.requester_id)
        except ReportPublishingError as exc:
            return self._payload("error", slug=slug, message=str(exc))
        return self._payload(
            "ok",
            slug=revoked.slug,
            source_type=revoked.source_type,
            source=revoked.source,
            access_policy=revoked.access_policy.value,
            revoked_at=revoked.revoked_at,
        )

    async def apublish_report(
        self,
        source_type: str,
        source: dict[str, Any],
        confirm_public: bool,
        access_policy: str | None = None,
    ) -> str:
        """Publish an authorized report artifact through a revocable link."""
        return self.publish_report(source_type, source, confirm_public, access_policy)

    async def arevoke_public_report(self, slug: str) -> str:
        """Revoke a previously published report link under either access policy."""
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
        execution_identity=build_execution_identity_from_runtime_context(context),
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
        artifact_kind=ARTIFACT_KIND_STATIC_SITE,
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


def _authorize_report_for_context(context: ToolRuntimeContext, report: PublishedReport) -> None:
    if context.requester_id in {report.requested_by, report.published_by}:
        return
    msg = "Report is not available to the current requester."
    raise ReportPublishingError(msg)


def _resolve_access_policy(context: ToolRuntimeContext, access_policy: str | None) -> ReportAccessPolicy:
    configured_default = context.config.report_publishing.default_access_policy
    if access_policy is None:
        return configured_default
    try:
        return ReportAccessPolicy(access_policy.strip())
    except (AttributeError, ValueError) as exc:
        msg = f"Unsupported report access_policy '{access_policy}'."
        raise ReportPublishingError(msg) from exc


def _publication_policy_error(
    context: ToolRuntimeContext,
    access_policy: ReportAccessPolicy,
    *,
    confirm_public: bool,
) -> str | None:
    if access_policy is ReportAccessPolicy.PUBLIC:
        if not context.config.report_publishing.allow_public:
            return "Public report publication is disabled by report_publishing.allow_public."
        if not confirm_public:
            return (
                "Set confirm_public to true to publish this bearer link; anyone who possesses it can view the report."
            )
        return None
    auth_error = report_viewer_auth_configuration_error(context.runtime_paths)
    if auth_error is not None:
        return (
            "Origin-room report publication requires trusted browser authentication with verified Matrix identity: "
            f"{auth_error}."
        )
    return None


def _origin_room_metadata(
    context: ToolRuntimeContext,
    access_policy: ReportAccessPolicy,
) -> dict[str, str | None]:
    if access_policy is ReportAccessPolicy.PUBLIC:
        return {
            "origin_room_id": None,
            "publisher_entity_name": None,
            "publisher_matrix_user_id": None,
        }
    room_id = context.room_id.strip()
    if not room_id:
        msg = "Origin-room report publication requires a canonical Matrix room ID in trusted tool context."
        raise ReportPublishingError(msg)
    publisher_entity_name = context.agent_name.strip()
    if not publisher_entity_name:
        msg = "Origin-room report publication requires publisher identity in trusted tool context."
        raise ReportPublishingError(msg)
    try:
        publisher_matrix_user_id = (
            entity_identity_registry(
                context.config,
                context.runtime_paths,
            )
            .current_id(publisher_entity_name)
            .full_id
        )
    except KeyError as exc:
        msg = "Origin-room report publication requires a configured publisher identity."
        raise ReportPublishingError(msg) from exc
    return {
        "origin_room_id": room_id,
        "publisher_entity_name": publisher_entity_name,
        "publisher_matrix_user_id": publisher_matrix_user_id,
    }


def _report_path_for_report(report: PublishedReport) -> str:
    if report.public_url is not None:
        public_path = urlsplit(report.public_url).path
        if public_path:
            return public_path
    return report_route_path(
        report.slug,
        access_policy=report.access_policy,
        trailing_slash=report.is_static_site,
    )


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
