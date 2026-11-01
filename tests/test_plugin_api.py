"""`mindroom.plugin_api` exports exactly the supported plugin entry points, and their parameters change only on purpose.

Plugins import these names (docs/plugins.md, docs/hooks.md, docs/tools/atlassian.md), so removing a name or an enum
member, or renaming, reordering, or changing the kind of a parameter, breaks them; update this pin together with the
plugin docs and the fleet.
"""

from __future__ import annotations

import enum
import inspect

import typing_extensions

from mindroom import plugin_api

_EXPORTS = [
    "ACTIVE_THREAD_FOLLOW_UP_SOURCE_KIND",
    "EVENT_AGENT_STARTED",
    "EVENT_AGENT_STOPPED",
    "EVENT_BOT_READY",
    "EVENT_COMPACTION_AFTER",
    "EVENT_COMPACTION_BEFORE",
    "EVENT_CONFIG_RELOADED",
    "EVENT_CONTACT_BEFORE_SEND",
    "EVENT_MESSAGE_AFTER_RESPONSE",
    "EVENT_MESSAGE_BEFORE_RESPONSE",
    "EVENT_MESSAGE_CANCELLED",
    "EVENT_MESSAGE_ENRICH",
    "EVENT_MESSAGE_FINAL_RESPONSE_TRANSFORM",
    "EVENT_MESSAGE_RECEIVED",
    "EVENT_REACTION_RECEIVED",
    "EVENT_ROOM_MEMBER_JOINED",
    "EVENT_ROOM_MEMBER_LEFT",
    "EVENT_SCHEDULE_FIRED",
    "EVENT_SESSION_STARTED",
    "EVENT_SYSTEM_ENRICH",
    "EVENT_TOOL_AFTER_CALL",
    "EVENT_TOOL_BEFORE_CALL",
    "EVENT_TOOL_ENVIRONMENT",
    "ROUTER_AGENT_NAME",
    "TRUSTED_INTERNAL_RELAY_SOURCE_KIND",
    "VOICE_PREFIX",
    "AfterResponseContext",
    "AgentLifecycleContext",
    "Ask",
    "AtlassianConnectionConfig",
    "AutomationContext",
    "BeforeResponseContext",
    "CancelledResponseContext",
    "CompactionHookContext",
    "ConfigField",
    "ConfigReloadedContext",
    "ContactBeforeSendContext",
    "CurrentCall",
    "CustomEventContext",
    "Done",
    "EnrichmentItem",
    "FinalResponseDraft",
    "FinalResponseTransformContext",
    "GoogleWorkspaceConfig",
    "HelperResult",
    "HookBindings",
    "MessageEnrichContext",
    "MessageEnvelope",
    "MessageReceivedContext",
    "MessageTarget",
    "ModelProvider",
    "ModelScope",
    "OAuthDiscoveryConfig",
    "OAuthProvider",
    "ReactionReceivedContext",
    "ResolvedWorkerTarget",
    "ResponseDraft",
    "ResponseResult",
    "RoomMemberJoinedContext",
    "RoomMemberLeftContext",
    "ScheduleFiredContext",
    "SenderKind",
    "SessionHookContext",
    "SetupType",
    "SystemEnrichContext",
    "ToolAfterCallContext",
    "ToolBeforeCallContext",
    "ToolCategory",
    "ToolEnvironmentContext",
    "ToolExecutionTarget",
    "ToolFileAccess",
    "ToolManagedInitArg",
    "ToolRuntimeContext",
    "ToolStatus",
    "ToolValue",
    "Toolkit",
    "TranscriptLine",
    "TurnIntent",
    "TurnOrigin",
    "TurnTrust",
    "atlassian_connection_oauth_provider",
    "automation",
    "automation_threads",
    "build_model_provider",
    "build_oauth_connect_instruction",
    "complete_prompt",
    "config_plugins",
    "current_call",
    "current_execution_identity",
    "emit_custom_event",
    "get_logger",
    "get_tool_by_name",
    "get_tool_runtime_context",
    "google_cloud_oauth_provider",
    "google_workspace_oauth_providers",
    "hook",
    "oauth_connect_url",
    "oauth_runtime_bootstrapper",
    "open_directory_within_root",
    "read_regular_file_within_root",
    "register_atlassian_connection_tools",
    "register_google_workspace_tools",
    "register_tool_with_metadata",
    "resolve_agent_file",
    "resolve_config_relative_path",
    "resolve_turn_model",
    "tool",
    "toolkit_functions",
    "write_file_within_root",
]

# Every hook context puts the base parameters first and ends with the keyword-only ones, which start with the three
# `emit` fills for each hook. A tool hook context makes its event name and every field of its own keyword-only.
_HOOK = "event_name, config, runtime_paths, correlation_id"
_HOOK_HELPERS = (
    "plugin_name, settings, logger, runtime_started_at, message_sender, agent_message_snapshot_reader, matrix_admin, "
    "room_state_querier, room_state_putter"
)
_TOOL_HOOK = f"config, runtime_paths, correlation_id, *, event_name, {_HOOK_HELPERS}"

# Each callable's parameter names and kinds, written as a signature without annotations or defaults.
# A class lists its constructor parameters; underscore-prefixed ones are MindRoom's own wiring and are left out.
_SIGNATURES = {
    "AfterResponseContext": f"({_HOOK}, result, *, {_HOOK_HELPERS})",
    "AgentLifecycleContext": (
        f"({_HOOK}, entity_name, entity_type, rooms, matrix_user_id, joined_room_ids, stop_reason, *, {_HOOK_HELPERS})"
    ),
    "Ask": "(text, new_thread, then)",
    "AutomationContext": (
        "(agent_name, config, runtime_paths, entry, options, settings, requester_id, execution_identity, workspace, "
        "state_dir)"
    ),
    "AtlassianConnectionConfig": "(*, name, display_name, site_url, cloud_id, products, write, client_config_service)",
    "BeforeResponseContext": f"({_HOOK}, draft, *, {_HOOK_HELPERS})",
    "CancelledResponseContext": f"({_HOOK}, info, *, {_HOOK_HELPERS})",
    "CompactionHookContext": (
        f"({_HOOK}, agent_name, scope, room_id, thread_id, transcript, session_id, token_count_before, "
        f"token_count_after, compaction_summary, *, {_HOOK_HELPERS})"
    ),
    "ConfigField": (
        "(name, label, type, required, default, placeholder, description, options, validation, authored_override, env)"
    ),
    "ConfigReloadedContext": (
        f"({_HOOK}, changed_entities, added_entities, removed_entities, plugin_changes, *, {_HOOK_HELPERS})"
    ),
    "ContactBeforeSendContext": (
        f"({_TOOL_HOOK}, agent_name, room_id, thread_id, requester_id, contact_id, counterparty_id, "
        "counterparty_name, rules, text, message_received_depth, verdict, feedback)"
    ),
    "CurrentCall": "(*, call_id, toolkit, session_id, session_state, history_scope, runtime)",
    "CustomEventContext": (
        f"({_HOOK}, payload, source_plugin, room_id, thread_id, sender_id, message_received_depth, *, {_HOOK_HELPERS})"
    ),
    "Done": "(notice, resolve, on_loop)",
    "EnrichmentItem": "(key, text, cache_policy, persist, minimal_required)",
    "FinalResponseDraft": "(response_text, response_kind, envelope)",
    "FinalResponseTransformContext": f"({_HOOK}, draft, *, {_HOOK_HELPERS})",
    "GoogleWorkspaceConfig": "(*, name, display_name, client_config_service, allowed_hosted_domains, services)",
    "HelperResult": "(*, content, usage)",
    "HookBindings": (
        "(registry, registry_state, message_sender, matrix_admin, room_state_querier, room_state_putter, "
        "agent_message_snapshot_reader, runtime_started_at)"
    ),
    "MessageEnrichContext": f"({_HOOK}, envelope, target_entity_name, target_member_names, *, {_HOOK_HELPERS})",
    "MessageEnvelope": (
        "(source_event_id, target, body, attachment_ids, mentioned_agents, agent_name, hook_source, "
        "message_received_depth, dispatch_policy_source_kind, *, origin)"
    ),
    "MessageReceivedContext": f"({_HOOK}, envelope, skip_plugin_names, suppress, *, {_HOOK_HELPERS})",
    "MessageTarget": "(room_id, source_thread_id, resolved_thread_id, reply_to_event_id, session_id)",
    "ModelScope": "(entity, room_id, thread_id, explicit, explicit_context_window)",
    "OAuthDiscoveryConfig": (
        "(resource, discovery, authorization_server, authorization_url, token_url, registration_url, "
        "dynamic_client_registration, token_endpoint_auth_method, pkce_code_challenge_method, allow_insecure_env, "
        "allow_private_env, error_label)"
    ),
    "OAuthProvider": (
        "(id, display_name, authorization_url, token_url, scopes, credential_service, tool_config_service, "
        "client_config_services, shared_client_config_services, default_redirect_path, extra_auth_params, "
        "extra_token_params, token_endpoint_auth_method, pkce_code_challenge_method, allow_empty_scopes, "
        "requester_scoped_credentials, tool_config_oauth_fallback_fields, allowed_email_domains, "
        "allowed_hosted_domains, allowed_email_domains_env, allowed_hosted_domains_env, status_capabilities, "
        "token_parser, token_exchanger, claim_validator, runtime_bootstrapper, account_claims_reader, "
        "legacy_credentials)"
    ),
    "ReactionReceivedContext": (
        f"({_HOOK}, room_id, event_id, sender_id, reaction_key, target_event_id, thread_id, *, {_HOOK_HELPERS})"
    ),
    "ResolvedWorkerTarget": (
        "(worker_scope, routing_agent_name, execution_identity, tenant_id, account_id, worker_key, private_agent_names)"
    ),
    "ResponseDraft": "(response_text, response_kind, tool_trace, extra_content, envelope, suppress)",
    "ResponseResult": "(response_text, response_event_id, delivery_kind, response_kind, envelope)",
    "RoomMemberJoinedContext": (
        f"({_HOOK}, agent_name, room_id, event_id, user_id, sender_id, display_name, avatar_url, membership, "
        f"prev_membership, *, {_HOOK_HELPERS})"
    ),
    "RoomMemberLeftContext": (
        f"({_HOOK}, agent_name, room_id, event_id, user_id, sender_id, display_name, avatar_url, membership, "
        f"prev_membership, *, {_HOOK_HELPERS})"
    ),
    "ScheduleFiredContext": (
        f"({_HOOK}, task_id, workflow, room_id, thread_id, created_by, message_text, suppress, *, {_HOOK_HELPERS})"
    ),
    "SessionHookContext": f"({_HOOK}, agent_name, scope, session_id, room_id, thread_id, *, {_HOOK_HELPERS})",
    "SystemEnrichContext": f"({_HOOK}, envelope, target_entity_name, target_member_names, *, {_HOOK_HELPERS})",
    "ToolAfterCallContext": (
        f"({_TOOL_HOOK}, tool_name, arguments, agent_name, room_id, thread_id, requester_id, session_id, result, "
        "error, blocked, duration_ms, message_received_depth)"
    ),
    "ToolBeforeCallContext": (
        f"({_TOOL_HOOK}, tool_name, arguments, agent_name, room_id, thread_id, requester_id, session_id, "
        "message_received_depth, declined, decline_reason)"
    ),
    "ToolEnvironmentContext": (
        f"({_TOOL_HOOK}, function_name, registered_toolkit, arguments, agent_name, room_id, requester_id, "
        "message_received_depth, declined, decline_reason)"
    ),
    "ToolRuntimeContext": (
        "(agent_name, target, requester_id, client, config, runtime_paths, plugins, conversation_reader, relations, "
        "agent_reply_memberships, transport_agent_name, active_model_name, room, storage_path, attachment_ids, "
        "runtime_attachment_ids, runtime_media_attachments, hooks, correlation_id, message_received_depth, "
        "orchestrator, tool_function_filter, persona, membership, membership_turn_id, source_kind, config_provider, "
        "cli_approval_handler, hook_source)"
    ),
    "ToolValue": "(*, content, images, audios, videos, files)",
    "Toolkit": "(*, name, tools, instructions)",
    "TranscriptLine": "(role, sender, display_name, text)",
    "TurnOrigin": (
        "(transport_sender_id, requester_id, sender_entity_name, requester_entity_name, sender_kind, requester_kind, "
        "intent, source_kind, trust, acts_for_requester)"
    ),
    "atlassian_connection_oauth_provider": "(connection)",
    "automation": "(name, *, requires_file_memory)",
    "automation_threads": "(runtime_paths)",
    "build_model_provider": "(config, runtime_paths, model_name, *, execution_identity)",
    "build_oauth_connect_instruction": "(provider, connect_url)",
    "complete_prompt": "(provider, prompt, *, instructions, schema, overrides, run_id)",
    "config_plugins": "(config, runtime_paths)",
    "current_call": "()",
    "current_execution_identity": "()",
    "emit_custom_event": "(plugin_name, event_name, payload)",
    "get_logger": "(name)",
    "get_tool_by_name": (
        "(tool_name, runtime_paths, *, tools, runs_in_worker, credential_overrides, credentials_manager, "
        "runtime_config, tool_config_overrides, tool_init_overrides, runtime_overrides, "
        "allowed_shared_services, tool_output_workspace_root, agent_state_root, "
        "tool_output_auto_save_threshold_bytes, workspace_in_worker, worker_target)"
    ),
    "get_tool_runtime_context": "()",
    "google_cloud_oauth_provider": "()",
    "google_workspace_oauth_providers": "(workspace)",
    "hook": "(event, *, name, priority, timeout_ms, agents, rooms, required)",
    "oauth_connect_url": "(provider, runtime_paths, *, worker_target)",
    "oauth_runtime_bootstrapper": "(config)",
    "open_directory_within_root": "(root, relative_path, *, create, mode)",
    "read_regular_file_within_root": "(root, relative_path, *, max_bytes, truncate)",
    "register_atlassian_connection_tools": "(connection)",
    "register_google_workspace_tools": "(workspace)",
    "register_tool_with_metadata": (
        "(*, name, display_name, description, category, status, setup_type, default_execution_target, "
        "requires_primary_runtime, consumes_workspace_paths, file_access, executes_code, requires_room_context, "
        "requires_conversation, icon, icon_color, config_fields, agent_override_fields, authored_override_validator, "
        "dependencies, auth_provider, oauth_fallback_fields, docs_url, helper_text, function_names, "
        "worker_inert_agent_functions, managed_init_args, supports_toolkit_filters)"
    ),
    "resolve_agent_file": "(raw_path, *, workspace_root, file_access, field_name)",
    "resolve_config_relative_path": "(raw_path, runtime_paths)",
    "resolve_turn_model": "(config, runtime_paths, scope, budget)",
    "tool": "(function, /, *, name, description)",
    "toolkit_functions": "(toolkit)",
    "write_file_within_root": "(root, relative_path, payload, *, file_mode, dir_mode, exclusive)",
}

# Plugins use the enums through their members, so each member name is pinned in definition order.
_ENUM_MEMBERS = {
    "SenderKind": ["USER", "MANAGED_ENTITY"],
    "SetupType": ["NONE", "API_KEY", "OAUTH", "SPECIAL"],
    "ToolCategory": [
        "EMAIL",
        "ENTERTAINMENT",
        "SOCIAL",
        "DEVELOPMENT",
        "RESEARCH",
        "INFORMATION",
        "PRODUCTIVITY",
        "COMMUNICATION",
        "INTEGRATIONS",
        "SMART_HOME",
    ],
    "ToolExecutionTarget": ["PRIMARY", "WORKER"],
    "ToolFileAccess": ["NONE", "AGENT", "UNCONFINED"],
    "ToolManagedInitArg": [
        "RUNTIME_PATHS",
        "CREDENTIALS_MANAGER",
        "WORKER_TARGET",
        "RUNTIME_CONFIG",
        "TOOL_OUTPUT_WORKSPACE_ROOT",
        "WORKSPACE_IN_WORKER",
        "CURRENT_ROOM_ID",
        "AGENT_NAME",
        "FILE_ACCESS",
        "AGENT_STATE_ROOT",
        "SHARED_DIRECTORY_ROOTS",
    ],
    "ToolStatus": ["AVAILABLE", "REQUIRES_CONFIG"],
    "TurnIntent": [
        "USER_MESSAGE",
        "MANAGED_MESSAGE",
        "ROUTER_HANDOFF",
        "ROUTER_NOTICE",
        "SCHEDULED_FIRE",
        "EXTERNAL_TRIGGER",
        "HOOK_MESSAGE",
        "HOOK_DISPATCH",
        "TRUSTED_INTERNAL_RELAY",
        "JOB_FOLLOW_UP",
    ],
    "TurnTrust": ["EXTERNAL", "TRUSTED_INTERNAL", "TRUSTED_USER_RELAY"],
}


def _is_enum(obj: object) -> bool:
    return isinstance(obj, type) and issubclass(obj, enum.Enum)


def _parameter_shape(obj: object) -> str:
    signature = inspect.signature(obj)
    parameters = [
        parameter.replace(annotation=inspect.Parameter.empty, default=inspect.Parameter.empty)
        for name, parameter in signature.parameters.items()
        if not name.startswith("_")
    ]
    return str(inspect.Signature(parameters))


def test_plugin_api_exports_exactly_the_supported_names() -> None:
    """Adding or removing a supported name is a deliberate change to this list."""
    assert plugin_api.__all__ == _EXPORTS
    assert all(hasattr(plugin_api, name) for name in plugin_api.__all__)


def test_each_exported_callable_keeps_its_parameter_names_and_kinds() -> None:
    """A plugin calls these by position or keyword as documented, so each parameter's name and kind stay put.

    A protocol, such as `ModelProvider`, is a type plugins annotate with and never call.
    """
    exported = {name: getattr(plugin_api, name) for name in plugin_api.__all__}
    shapes = {
        name: _parameter_shape(obj)
        for name, obj in exported.items()
        if callable(obj) and not _is_enum(obj) and not typing_extensions.is_protocol(obj)
    }
    assert shapes == _SIGNATURES


def test_each_exported_enum_keeps_its_member_names() -> None:
    """Plugins write members such as `ToolCategory.DEVELOPMENT`, so renaming or removing one breaks them."""
    exported = {name: getattr(plugin_api, name) for name in plugin_api.__all__}
    members = {name: list(obj.__members__) for name, obj in exported.items() if _is_enum(obj)}
    assert members == _ENUM_MEMBERS
