"""The plugin API exports exactly the supported plugin entry points, pinned together with `PLUGIN_API_VERSION`.

Plugins import these names (docs/plugins.md, docs/hooks.md, docs/tools/atlassian.md), so removing a name, an enum
member, a documented method, property, or exception attribute, or a manifest field, renaming, reordering, or changing
the kind of a parameter, or changing a constant's value breaks them; update this pin together with the plugin docs and the fleet,
and bump `PLUGIN_API_VERSION` as docs/plugins.md says.
"""

from __future__ import annotations

import enum
import hashlib
import inspect
import json
import pkgutil
from typing import TYPE_CHECKING

import pytest
import typing_extensions

from mindroom import plugin_api, plugin_http, plugin_testing
from mindroom.tool_system import plugin_imports

if TYPE_CHECKING:
    from pathlib import Path

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
    "EVENT_TURN_ENDED",
    "EVENT_TURN_PAUSED",
    "EVENT_TURN_RESUMED",
    "EVENT_TURN_STARTED",
    "PLUGIN_API_VERSION",
    "ROUTER_AGENT_NAME",
    "TRUSTED_INTERNAL_RELAY_SOURCE_KIND",
    "VOICE_PREFIX",
    "AfterResponseContext",
    "AgentLifecycleContext",
    "ApprovalAlias",
    "Ask",
    "AtlassianConnectionConfig",
    "AutomationContext",
    "BeforeResponseContext",
    "CancelledResponseContext",
    "CompactionHookContext",
    "Config",
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
    "HookContext",
    "HookMatrixAdmin",
    "HookMessageSender",
    "ManagedEntity",
    "MessageEnrichContext",
    "MessageEnvelope",
    "MessageReceivedContext",
    "MessageTarget",
    "ModelProvider",
    "ModelScope",
    "OAuthConnectionRequired",
    "OAuthConsentGroup",
    "OAuthDiscoveryConfig",
    "OAuthProvider",
    "OAuthProviderError",
    "PluginContext",
    "PluginLifecycle",
    "PublicFetchError",
    "PublicFetchResult",
    "ReactionReceivedContext",
    "ResolvedWorkerTarget",
    "ResponseDraft",
    "ResponseResult",
    "RoomMemberJoinedContext",
    "RoomMemberLeftContext",
    "RuntimePaths",
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
    "ToolTraceEntry",
    "ToolValue",
    "Toolkit",
    "TranscriptLine",
    "TurnIntent",
    "TurnLifecycleContext",
    "TurnOrigin",
    "TurnOutcome",
    "TurnTrust",
    "WorkerService",
    "WorkerServiceError",
    "WorkerServiceState",
    "atlassian_connection_oauth_provider",
    "automation",
    "automation_threads",
    "build_model_provider",
    "build_oauth_connect_instruction",
    "complete_prompt",
    "config_plugins",
    "current_call",
    "current_execution_identity",
    "current_plugin_context",
    "dump_worker_target",
    "emit_custom_event",
    "ensure_worker_service",
    "entity_directory",
    "fetch_public_url",
    "fetch_public_url_sync",
    "get_logger",
    "get_tool_by_name",
    "get_tool_runtime_context",
    "google_cloud_oauth_provider",
    "google_workspace_oauth_providers",
    "hook",
    "is_human_user",
    "is_platform_administrator",
    "load_worker_target",
    "oauth_connect_url",
    "oauth_runtime_bootstrapper",
    "open_directory_within_root",
    "read_regular_file_within_root",
    "redact_sensitive_text",
    "register_atlassian_connection_tools",
    "register_google_workspace_tools",
    "register_tool_with_metadata",
    "resolve_agent_file",
    "resolve_config_relative_path",
    "resolve_turn_model",
    "stop_worker_service",
    "tool",
    "toolkit_functions",
    "write_agent_file",
    "write_file_within_root",
]

# Every hook context puts the base parameters first and ends with the keyword-only ones, which start with the three
# `emit` fills for each hook. A tool hook context makes its event name and every field of its own keyword-only.
_HOOK = "event_name, config, runtime_paths, correlation_id"
_HOOK_HELPERS = (
    "plugin_name, settings, logger, runtime_started_at, message_sender, agent_message_snapshot_reader, matrix_admin, "
    "room_state_querier, room_state_putter, turn_id, parent_turn_id"
)
_TOOL_HOOK = f"config, runtime_paths, correlation_id, *, event_name, {_HOOK_HELPERS}"

# Each callable's parameter names and kinds, written as a signature without annotations or defaults.
# A class lists its constructor parameters; underscore-prefixed ones are MindRoom's own wiring and are left out.
_SIGNATURES = {
    "AfterResponseContext": f"({_HOOK}, result, *, {_HOOK_HELPERS})",
    "AgentLifecycleContext": (
        f"({_HOOK}, entity_name, entity_type, rooms, matrix_user_id, joined_room_ids, stop_reason, *, {_HOOK_HELPERS})"
    ),
    "ApprovalAlias": "(judged_as, judge)",
    "Ask": "(text, new_thread, then, summary)",
    "AutomationContext": (
        "(agent_name, config, runtime_paths, entry, options, settings, requester_id, execution_identity, workspace, "
        "state_dir)"
    ),
    "AtlassianConnectionConfig": "(*, name, display_name, site_url, cloud_id, products, write, requester_scoped, client_config_service)",
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
    "HookContext": f"({_HOOK}, *, {_HOOK_HELPERS})",
    "ManagedEntity": "(name, kind, display_name, tool_names)",
    "MessageEnrichContext": f"({_HOOK}, envelope, target_entity_name, target_member_names, *, {_HOOK_HELPERS})",
    "MessageEnvelope": (
        "(source_event_id, target, body, attachment_ids, mentioned_agents, agent_name, hook_source, "
        "message_received_depth, dispatch_policy_source_kind, *, origin)"
    ),
    "MessageReceivedContext": f"({_HOOK}, envelope, skip_plugin_names, suppress, *, {_HOOK_HELPERS})",
    "MessageTarget": "(room_id, source_thread_id, resolved_thread_id, reply_to_event_id, session_id)",
    "ModelScope": "(entity, room_id, thread_id, explicit, explicit_context_window)",
    "OAuthConsentGroup": "(key, display_name)",
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
        "legacy_credentials, consent_group)"
    ),
    "PluginContext": "(plugin_name, runtime_paths)",
    "PluginLifecycle": "(plugin_name, process_state)",
    "PublicFetchError": "(message, *, code, status_code)",
    "PublicFetchResult": "(url, content_type, content)",
    "ReactionReceivedContext": (
        f"({_HOOK}, room_id, event_id, sender_id, reaction_key, target_event_id, thread_id, *, {_HOOK_HELPERS})"
    ),
    "ResolvedWorkerTarget": (
        "(worker_scope, routing_agent_name, execution_identity, tenant_id, account_id, worker_key, private_agent_names, "
        "placement)"
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
        "error, blocked, duration_ms, message_received_depth, shown)"
    ),
    "ToolBeforeCallContext": (
        f"({_TOOL_HOOK}, tool_name, arguments, agent_name, room_id, thread_id, requester_id, session_id, "
        "message_received_depth, shown, declined, decline_reason)"
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
        "cli_approval_handler, hook_source, tool_output_paths, tool_calls_shown, turn_id, parent_turn_id)"
    ),
    "ToolValue": "(*, content, images, audios, videos, files)",
    "Toolkit": "(*, name, tools, instructions)",
    "TranscriptLine": "(role, sender, display_name, text)",
    "TurnLifecycleContext": f"({_HOOK}, envelope, outcome, *, {_HOOK_HELPERS})",
    "WorkerService": "(name, module, idle_timeout_seconds)",
    "WorkerServiceState": "(state, port, exit_code)",
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
    "current_plugin_context": "(plugin_name)",
    "dump_worker_target": "(target)",
    "emit_custom_event": "(plugin_name, event_name, payload)",
    "ensure_worker_service": "(context, service, target)",
    "entity_directory": "(config, runtime_paths)",
    "fetch_public_url": "(url, *, max_bytes, use_env_proxy, user_agent)",
    "fetch_public_url_sync": "(url, *, max_bytes, use_env_proxy, user_agent)",
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
    "is_human_user": "(user_id, config, runtime_paths)",
    "is_platform_administrator": "(user_id, config, runtime_paths)",
    "load_worker_target": "(data)",
    "oauth_connect_url": "(provider, runtime_paths, *, worker_target)",
    "oauth_runtime_bootstrapper": "(config)",
    "open_directory_within_root": "(root, relative_path, *, create, mode)",
    "read_regular_file_within_root": "(root, relative_path, *, max_bytes, truncate)",
    "redact_sensitive_text": "(value, *, max_length)",
    "register_atlassian_connection_tools": "(connection)",
    "register_google_workspace_tools": "(workspace)",
    "register_tool_with_metadata": (
        "(*, name, display_name, description, category, status, setup_type, default_execution_target, "
        "requires_primary_runtime, consumes_workspace_paths, file_access, executes_code, requires_room_context, "
        "requires_conversation, icon, icon_color, config_fields, agent_override_fields, authored_override_validator, "
        "dependencies, auth_provider, oauth_fallback_fields, docs_url, helper_text, function_names, workspace_writers, "
        "approval_aliases, worker_inert_agent_functions, managed_init_args, supports_toolkit_filters)"
    ),
    "resolve_agent_file": "(raw_path, *, workspace_root, file_access, field_name)",
    "resolve_config_relative_path": "(raw_path, runtime_paths)",
    "resolve_turn_model": "(config, runtime_paths, scope, budget)",
    "stop_worker_service": "(context, service, target)",
    "tool": "(function, /, *, name, description)",
    "toolkit_functions": "(toolkit)",
    "write_agent_file": "(raw_path, payload, *, workspace_root, file_access)",
    "write_file_within_root": "(root, relative_path, payload, *, file_mode, dir_mode, exclusive)",
}

# Classes plugins receive from MindRoom and only annotate with, never construct, so each is pinned by its name alone:
# pinning a constructor's parameters would make every new config, path, or trace field a plugin API change.
_ANNOTATION_TYPES = [
    "Config",
    "OAuthConnectionRequired",
    "OAuthProviderError",
    "RuntimePaths",
    "ToolTraceEntry",
    "WorkerServiceError",
]

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
    "TurnOutcome": ["COMPLETED", "CANCELLED", "FAILED"],
    "TurnTrust": ["EXTERNAL", "TRUSTED_INTERNAL", "TRUSTED_USER_RELAY"],
}

# The methods and properties docs/plugins.md and docs/hooks.md have plugins use, by owning class, each method's
# parameters written as `_SIGNATURES` writes them. `ScheduleFiredContext` overrides `send_message` with the same ones.
_SEND_MESSAGE = "(self, room_id, text, *, thread_id, extra_content, trigger_dispatch, mentions, wake_mentioned)"
_MEMBERS = {
    "mindroom.plugin_api.HookContext": {
        "state_root": "property",
        "send_message": _SEND_MESSAGE,
        "query_room_state": "(self, room_id, event_type, state_key)",
        "get_latest_agent_message_snapshot": "(self, room_id, sender, *, thread_id)",
        "put_room_state": "(self, room_id, event_type, state_key, content)",
    },
    "mindroom.plugin_api.ScheduleFiredContext": {"send_message": _SEND_MESSAGE},
    "mindroom.plugin_api.MessageEnrichContext": {"add_metadata": "(self, key, text, *, cache_policy, persist)"},
    "mindroom.plugin_api.SystemEnrichContext": {"add_instruction": "(self, key, text, *, cache_policy)"},
    "mindroom.plugin_api.ToolBeforeCallContext": {"chat_preview": "(self)", "decline": "(self, reason)"},
    "mindroom.plugin_api.ToolAfterCallContext": {"chat_preview": "(self)"},
    "mindroom.plugin_api.ToolEnvironmentContext": {
        "set_env": "(self, name, value)",
        "unset_env": "(self, name)",
        "pass_through": "(self, name)",
        "prepend_path": "(self, directory)",
        "decline": "(self, reason)",
    },
    "mindroom.plugin_api.ContactBeforeSendContext": {
        "rewrite": "(self, text)",
        "revise": "(self, feedback)",
        "deny": "(self, reason)",
    },
    "mindroom.plugin_api.MessageEnvelope": {
        "room_id": "property",
        "requester_id": "property",
        "sender_id": "property",
        "source_kind": "property",
    },
    "mindroom.plugin_api.TurnOrigin": {
        "may_dispatch_without_mention": "property",
        "may_answer_interactive_prompt": "property",
    },
    "mindroom.plugin_api.ToolRuntimeContext": {
        "room_id": "property",
        "thread_id": "property",
        "resolved_thread_id": "property",
        "execution_identity": "property",
        "resolve_worker_target": "(self)",
    },
    "mindroom.plugin_api.RuntimePaths": {"env_value": "(self, name, *, default)"},
    "mindroom.plugin_api.PluginContext": {
        "settings": "property",
        "config": "property",
        "state_root": "property",
        "is_active": "(self)",
    },
    "mindroom.plugin_api.PluginLifecycle": {"context": "(self)"},
    "mindroom.plugin_api.HookMessageSender": {
        "__call__": (
            "(self, room_id, body, thread_id, source_hook, extra_content, *, trigger_dispatch, mentions, wake_mentioned)"
        ),
    },
    "mindroom.plugin_http.RoomReader": {
        "power": "(self)",
        "is_direct": "(self)",
        "agents": "(self)",
        "agent": "(self, agent_user_id)",
        "modes": "(self)",
        "set_modes": "(self, *, thread, reply)",
        "history": "(self)",
        "terminals": "(self, workspace, *, create)",
    },
    "mindroom.plugin_http.WorkerTerminals": {
        "open": "(self, *, cols, rows, cwd)",
        "sessions": "(self)",
        "close": "(self, session_id)",
        "attach": "(self, session_id)",
    },
    "mindroom.terminal.client.TerminalStream": {"send_input": "(self, data, *, empty_line)"},
    "mindroom.plugin_http.RoomHistory": {"since": "(self, recorded_under)"},
    "mindroom.plugin_http.WorkerRelay": {
        "http": "(self, method, path, *, service, port, headers, body)",
        "websocket": "(self, path, *, service, port, protocols, headers)",
        "relay_messages": "(self, websocket, upstream)",
        "ports": "(self)",
    },
    "mindroom.plugin_http.RoomMessenger": {
        "notice": "(self, text, *, thread_id, content)",
        "relay": "(self, text, *, mentions, thread_id, content)",
    },
    "mindroom.plugin_http.RoomPower": {
        "level": "(self, user_id)",
        "is_admin": "(self, user_id)",
        "may_send": "(self, user_id, event_type)",
    },
    "mindroom.plugin_api.HookMatrixAdmin": {
        "get_joined_rooms": "(self)",
        "retain_room": "(self, room_id)",
        "resolve_alias": "(self, alias)",
        "create_room": "(self, *, name, alias_localpart, topic, power_user_ids)",
        "invite_user": "(self, room_id, user_id)",
        "force_join_user": "(self, room_id, user_id)",
        "kick_user": "(self, room_id, user_id, *, reason)",
        "get_room_members": "(self, room_id)",
        "get_profile_avatar": "(self, user_id)",
        "get_room_state_event": "(self, room_id, event_type, state_key)",
        "add_room_to_space": "(self, space_room_id, room_id)",
        "put_room_state": "(self, room_id, event_type, state_key, content)",
    },
    "mindroom.plugin_testing.LoadedPlugin": {
        "add_room": "(self, room_id, members, *, power_levels, direct, history_visibility, key)",
        "client": "(self, user_id)",
        "sign_out": "(self, user_id)",
        "call_tool": "(self, tool, function, arguments, *, agent, sender, room)",
        "fire": "(self, event, *, agent, sender, room, body, **fields)",
    },
    "mindroom.tool_system.invocation.CallResult": {"succeeded": "property", "failure_text": "property"},
    "mindroom.history.types.HistoryScope": {"key": "property"},
    "mindroom.custom_tools.google_service.GoogleCloudToolkit": {
        "_google_cloud_credentials": "(self)",
        "_google_cloud_client": "(self, name, factory)",
        "_google_cloud_error_result": "(self, service_name, operation, exc)",
    },
}

# The attributes docs/plugins.md has a route read on an exception it catches, by owning class; each instance sets them,
# so `_MEMBERS` cannot pin them.
_EXCEPTION_ATTRIBUTES = {"mindroom.plugin_api.OAuthConnectionRequired": ["connect_url"]}

# Plugins compare with these values, and docs/hooks.md writes the event names as literals in `@hook(...)`.
_CONSTANTS = {
    "ACTIVE_THREAD_FOLLOW_UP_SOURCE_KIND": "active_thread_follow_up",
    "EVENT_AGENT_STARTED": "agent:started",
    "EVENT_AGENT_STOPPED": "agent:stopped",
    "EVENT_BOT_READY": "bot:ready",
    "EVENT_COMPACTION_AFTER": "compaction:after",
    "EVENT_COMPACTION_BEFORE": "compaction:before",
    "EVENT_CONFIG_RELOADED": "config:reloaded",
    "EVENT_CONTACT_BEFORE_SEND": "contact:before_send",
    "EVENT_MESSAGE_AFTER_RESPONSE": "message:after_response",
    "EVENT_MESSAGE_BEFORE_RESPONSE": "message:before_response",
    "EVENT_MESSAGE_CANCELLED": "message:cancelled",
    "EVENT_MESSAGE_ENRICH": "message:enrich",
    "EVENT_MESSAGE_FINAL_RESPONSE_TRANSFORM": "message:final_response_transform",
    "EVENT_MESSAGE_RECEIVED": "message:received",
    "EVENT_REACTION_RECEIVED": "reaction:received",
    "EVENT_ROOM_MEMBER_JOINED": "room:member_joined",
    "EVENT_ROOM_MEMBER_LEFT": "room:member_left",
    "EVENT_SCHEDULE_FIRED": "schedule:fired",
    "EVENT_SESSION_STARTED": "session:started",
    "EVENT_SYSTEM_ENRICH": "system:enrich",
    "EVENT_TOOL_AFTER_CALL": "tool:after_call",
    "EVENT_TOOL_BEFORE_CALL": "tool:before_call",
    "EVENT_TOOL_ENVIRONMENT": "tool:environment",
    "EVENT_TURN_ENDED": "turn:ended",
    "EVENT_TURN_PAUSED": "turn:paused",
    "EVENT_TURN_RESUMED": "turn:resumed",
    "EVENT_TURN_STARTED": "turn:started",
    "ROUTER_AGENT_NAME": "router",
    "TRUSTED_INTERNAL_RELAY_SOURCE_KIND": "trusted_internal_relay",
    "VOICE_PREFIX": "🎤 ",
}


# Every manifest field docs/plugins.md documents, set as a plugin sets it.
_MANIFEST = {
    "name": "demo",
    "mindroom_plugin_api": "1.0",
    "tools_module": "tools.py",
    "oauth_module": "oauth.py",
    "hooks_module": "hooks.py",
    "routes_module": "routes.py",
    "lifecycle_module": "lifecycle.py",
    "origin_module": "origin.py",
    "skills": ["skills"],
    "reserved_namespaces": ["com.example.demo."],
}

# What docs/plugins.md has plugins and their tests import from other modules, which `mindroom.plugin_api` cannot re-export without
# loading FastAPI or Agno, with each one's parameters written as `_SIGNATURES` writes them.
_HTTP_EXPORTS = [
    "AgentWorkspace",
    "ClientUser",
    "RoomHistory",
    "RoomMessenger",
    "RoomModes",
    "RoomPower",
    "RoomReader",
    "TerminalError",
    "WorkerRelay",
    "WorkerTerminals",
    "client_origins",
    "client_rooms",
    "client_stream",
    "client_user_oauth_access_token",
    "plugin_context",
    "plugin_origin_url",
    "require_client_user",
    "require_platform_administrator",
    "require_room_member",
    "require_room_messenger",
    "require_room_reader",
    "stream_room_reader",
    "worker_relay",
]
_TESTING_EXPORTS = ["LoadedPlugin", "Room", "SentMessage", "TurnRefused", "load_plugin"]
_ELSEWHERE_SIGNATURES = {
    "mindroom.custom_tools.google_service.GoogleCloudToolkit": (
        "(*, name, tools, runtime_paths, credentials_manager, worker_target, runtime_config, **kwargs)"
    ),
    "mindroom.memory.refresh_agent_memory_search": "(agent_name, root, config, runtime_paths, execution_identity)",
    "mindroom.memory.write_scope_markdown_file": "(scope_path, relative_path, payload)",
    "mindroom.plugin_http.AgentWorkspace": (
        "(name, user_id, display_name, private, root, files, terminal, worker_target, config)"
    ),
    "mindroom.plugin_http.ClientUser": "(user_id)",
    "mindroom.plugin_http.RoomHistory": "(visibility)",
    "mindroom.plugin_http.RoomMessenger": "(user)",
    "mindroom.plugin_http.RoomModes": "(thread, reply, reply_in_effect)",
    "mindroom.plugin_http.RoomPower": "(power_levels, creators, encrypted)",
    "mindroom.plugin_http.RoomReader": "(user, members)",
    "mindroom.plugin_http.client_origins": "(connection)",
    "mindroom.plugin_http.client_rooms": "(request, candidates)",
    "mindroom.plugin_http.client_stream": "(websocket, room_id, *, admit)",
    "mindroom.plugin_http.client_user_oauth_access_token": "(request, user, provider_id, agent_name)",
    "mindroom.plugin_http.plugin_context": "(connection)",
    "mindroom.plugin_http.plugin_origin_url": "(connection, label)",
    "mindroom.plugin_http.require_client_user": "(request)",
    "mindroom.plugin_http.require_platform_administrator": "(request)",
    "mindroom.plugin_http.require_room_member": "(request, room_id)",
    "mindroom.plugin_http.require_room_messenger": "(request, room_id)",
    "mindroom.plugin_http.require_room_reader": "(request, room_id)",
    "mindroom.plugin_http.stream_room_reader": "(websocket, user, room_id)",
    "mindroom.plugin_http.TerminalError": "(status_code, detail)",
    "mindroom.terminal.session.TerminalInfo": "(id, cols, rows, alive, exit_code, created_at)",
    "mindroom.plugin_http.WorkerRelay": "()",
    "mindroom.plugin_http.worker_relay": "(connection, target)",
    "mindroom.plugin_testing.Room": "(room_id, members, power_levels, direct, history_visibility, key, sent)",
    "mindroom.plugin_testing.SentMessage": "(text, thread_id, source, content, mentions)",
    "mindroom.plugin_testing.load_plugin": "(path, tmp_dir, *, config, settings)",
    "mindroom.tool_system.invocation.CallResult": "(status, value, error)",
}

# The plugin API version these pins describe, and their digest.
_PINNED_VERSION = ((1, 30), "0a7b1fbbec98d32b5440476028736a3bbc5cd8cd2d56818f7bc0b3e9fd26d73c")


def _is_enum(obj: object) -> bool:
    return isinstance(obj, type) and issubclass(obj, enum.Enum)


def _member_shape(obj: object) -> str:
    return "property" if isinstance(obj, property) else _parameter_shape(obj)


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

    A protocol, such as `ModelProvider`, and each of `_ANNOTATION_TYPES` is a type plugins annotate with and never call.
    """
    exported = {name: getattr(plugin_api, name) for name in plugin_api.__all__}
    shapes = {
        name: _parameter_shape(obj)
        for name, obj in exported.items()
        if callable(obj)
        and not _is_enum(obj)
        and not typing_extensions.is_protocol(obj)
        and name not in _ANNOTATION_TYPES
    }
    assert shapes == _SIGNATURES
    assert all(isinstance(exported[name], type) for name in _ANNOTATION_TYPES)


def test_each_exported_enum_keeps_its_member_names() -> None:
    """Plugins write members such as `ToolCategory.DEVELOPMENT`, so renaming or removing one breaks them."""
    exported = {name: getattr(plugin_api, name) for name in plugin_api.__all__}
    members = {name: list(obj.__members__) for name, obj in exported.items() if _is_enum(obj)}
    assert members == _ENUM_MEMBERS


def test_each_documented_method_and_property_keeps_its_name_and_parameters() -> None:
    """Plugins call these methods and read these properties as the docs show, so each one stays put."""
    shapes = {
        owner: {name: _member_shape(getattr(pkgutil.resolve_name(owner), name)) for name in members}
        for owner, members in _MEMBERS.items()
    }
    assert shapes == _MEMBERS


def test_each_documented_exception_attribute_is_set() -> None:
    """A route reads these attributes of an exception it catches, as the docs show, so renaming one breaks it."""
    attributes = {
        owner: [name for name in names if name in vars(pkgutil.resolve_name(owner)("message"))]
        for owner, names in _EXCEPTION_ATTRIBUTES.items()
    }
    assert attributes == _EXCEPTION_ATTRIBUTES


def test_each_exported_constant_keeps_its_value() -> None:
    """A plugin that compares with a constant's value, or writes it as a literal, breaks when the value changes."""
    exported = {name: getattr(plugin_api, name) for name in plugin_api.__all__}
    assert {name: value for name, value in exported.items() if isinstance(value, str)} == _CONSTANTS


def test_each_documented_manifest_field_is_read(tmp_path: Path) -> None:
    """MindRoom ignores unknown manifest fields, so renaming a documented one would silently drop it from every plugin."""
    root = tmp_path.resolve()
    module_files = tuple(value for key, value in _MANIFEST.items() if key.endswith("_module"))
    for module in module_files:
        (root / module).touch()
    (root / "skills").mkdir()
    (root / "mindroom.plugin.json").write_text(json.dumps(_MANIFEST))
    (root / "later").mkdir()
    (root / "later" / "mindroom.plugin.json").write_text(json.dumps({**_MANIFEST, "mindroom_plugin_api": "1.999"}))

    plugin = plugin_imports._load_plugin_base(root)

    modules = (
        plugin.tools_module_path,
        plugin.oauth_module_path,
        plugin.hooks_module_path,
        plugin.routes_module_path,
        plugin.lifecycle_module_path,
        plugin.origin_module_path,
    )
    assert plugin.name == "demo"
    assert modules == tuple(root / module for module in module_files)
    assert (plugin.skill_dirs, plugin.reserved_namespaces) == ([root / "skills"], ("com.example.demo.",))
    with pytest.raises(plugin_imports.PluginValidationError, match=r"needs plugin API 1\.999"):
        plugin_imports._load_plugin_base(root / "later")


def test_the_entry_points_plugins_import_from_other_modules_keep_their_parameter_names_and_kinds() -> None:
    """`mindroom.plugin_http` and `mindroom.plugin_testing` export exactly their helpers, and each keeps its parameters."""
    shapes = {path: _parameter_shape(pkgutil.resolve_name(path)) for path in _ELSEWHERE_SIGNATURES}

    assert plugin_http.__all__ == _HTTP_EXPORTS
    assert plugin_testing.__all__ == _TESTING_EXPORTS
    assert shapes == _ELSEWHERE_SIGNATURES


def test_the_pins_record_the_plugin_api_version_they_describe() -> None:
    """A change to the pinned surface fails here until the version is bumped and pinned with the surface's digest."""
    pins = [
        _EXPORTS,
        _SIGNATURES,
        _ENUM_MEMBERS,
        _MEMBERS,
        _EXCEPTION_ATTRIBUTES,
        _CONSTANTS,
        _MANIFEST,
        _HTTP_EXPORTS,
        _TESTING_EXPORTS,
        _ELSEWHERE_SIGNATURES,
    ]
    digest = hashlib.sha256(json.dumps(pins, sort_keys=True).encode()).hexdigest()

    assert (plugin_api.PLUGIN_API_VERSION, digest) == _PINNED_VERSION, (
        "The plugin API changed: bump PLUGIN_API_VERSION in mindroom/tool_system/plugin_imports.py, the minor for an "
        f"addition and the major for a breaking change, and set _PINNED_VERSION to (the new version, {digest!r})."
    )
