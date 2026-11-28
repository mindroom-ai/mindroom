"""Construct concrete toolkits from the tool registry and resolved settings."""

from __future__ import annotations

from typing import TYPE_CHECKING, Any, cast

from mindroom.config.tool_aliases import canonical_tool_name
from mindroom.constants import DEFAULT_TOOL_OUTPUT_AUTO_SAVE_THRESHOLD_BYTES
from mindroom.file_access import agent_file_access
from mindroom.logging_config import get_logger
from mindroom.shared_directories.paths import shared_directory_roots
from mindroom.shared_directories.tool_roots import SharedDirectoryRoot
from mindroom.tool_system.agent_tools import resolve_agent_tools
from mindroom.tool_system.construction import ToolConstruction, tool_config_signature
from mindroom.tool_system.declarations import ToolManagedInitArg
from mindroom.tool_system.dependencies import (
    auto_install_optional_extra_for_import_retry,
    ensure_tool_deps,
    optional_dependency_environment,
)
from mindroom.tool_system.placement import tool_placement
from mindroom.tool_system.registry import ToolMetadataValidationError, factory_origin
from mindroom.tool_system.sandbox_proxy import worker_stand_ins
from mindroom.tool_system.settings import (
    load_toolkit_class,
    offered_function_names,
    pop_implicit_toolkit_filters,
    resolve_tool_settings,
)
from mindroom.tool_system.worker_routing import (
    supports_tool_name_for_worker_scope,
    unsupported_shared_only_integration_message,
)
from mindroom.workers.named import named_workers_refusal

if TYPE_CHECKING:
    from collections.abc import Callable
    from pathlib import Path

    from mindroom.config.main import Config
    from mindroom.config.models import FileAccess
    from mindroom.constants import RuntimePaths
    from mindroom.credentials.manager import CredentialsManager
    from mindroom.tool_system.authoring import Toolkit
    from mindroom.tool_system.declarations import ToolMetadata
    from mindroom.tool_system.registry import ToolRegistry
    from mindroom.tool_system.worker_routing import ResolvedWorkerTarget

logger = get_logger(__name__)


def _build_managed_tool_init_kwargs(
    metadata: ToolMetadata,
    *,
    runtime_paths: RuntimePaths,
    tools: ToolRegistry,
    credentials_manager: CredentialsManager | None,
    worker_target: ResolvedWorkerTarget | None,
    runtime_config: Config | None,
    tool_output_workspace_root: Path | None,
    saved_output_dir: Path | None,
    workspace_in_worker: bool | None,
    agent_state_root: Path | None,
    in_sandbox_request: bool,
) -> dict[str, object]:
    """Build declared MindRoom-managed constructor kwargs for one tool.

    Path tools working in a workdir can read ``saved_output_dir``, the saved outputs whose receipts name files there.
    """
    execution_identity = worker_target.execution_identity if worker_target is not None else None
    agent_name = worker_target.routing_agent_name if worker_target is not None else None
    saved_outputs = (
        (SharedDirectoryRoot(path=saved_output_dir, writable=False, label="the saved tool outputs directory"),)
        if saved_output_dir
        else ()
    )

    def path_tool_roots() -> tuple[SharedDirectoryRoot, ...]:
        # Shared directories change during a response, so path tools resolve them on each call.
        return (*shared_directory_roots(runtime_config, runtime_paths, agent_name), *saved_outputs)

    managed_values: dict[ToolManagedInitArg, Callable[[], object]] = {
        ToolManagedInitArg.RUNTIME_PATHS: lambda: runtime_paths,
        ToolManagedInitArg.CREDENTIALS_MANAGER: lambda: credentials_manager,
        ToolManagedInitArg.WORKER_TARGET: lambda: worker_target,
        ToolManagedInitArg.RUNTIME_CONFIG: lambda: runtime_config,
        ToolManagedInitArg.TOOL_OUTPUT_WORKSPACE_ROOT: lambda: tool_output_workspace_root,
        ToolManagedInitArg.WORKSPACE_IN_WORKER: lambda: (
            _sandbox_settings_route_workspace_tools(runtime_paths, tools)
            if workspace_in_worker is None
            else workspace_in_worker
        ),
        ToolManagedInitArg.CURRENT_ROOM_ID: lambda: (
            execution_identity.room_id if execution_identity is not None else None
        ),
        ToolManagedInitArg.AGENT_NAME: lambda: agent_name,
        ToolManagedInitArg.FILE_ACCESS: lambda: _managed_file_access(runtime_config, worker_target),
        ToolManagedInitArg.AGENT_STATE_ROOT: lambda: agent_state_root,
        ToolManagedInitArg.SHARED_DIRECTORY_ROOTS: lambda: path_tool_roots,
        ToolManagedInitArg.IN_SANDBOX_REQUEST: lambda: in_sandbox_request,
    }
    return {init_arg.value: managed_values[init_arg]() for init_arg in metadata.managed_init_args}


def _managed_file_access(runtime_config: Config | None, worker_target: ResolvedWorkerTarget | None) -> FileAccess:
    """Resolve the constructing agent's file_access."""
    return agent_file_access(runtime_config, worker_target.routing_agent_name if worker_target is not None else None)


def _constructing_agent_runs_in_worker(
    tool_name: str,
    runtime_paths: RuntimePaths,
    tools: ToolRegistry,
    runtime_config: Config | None,
    worker_target: ResolvedWorkerTarget | None,
) -> bool:
    """Place a toolkit a tool builds for its agent, such as a plugin's sub-toolkit.

    It runs in a worker when the sandbox settings route it there, or when the agent's own toolkit of that name would:
    an agent's explicit ``worker_tools`` can move it into a worker, never out of one, since the agent may not have
    that toolkit itself.
    """
    placement = tool_placement(runtime_paths, tools)
    if placement.runs_in_worker(tool_name, None):
        return True
    agent_name = worker_target.routing_agent_name if worker_target is not None else None
    return (
        runtime_config is not None
        and agent_name in runtime_config.agents
        and resolve_agent_tools(runtime_config, agent_name, tools).runs_in_worker(tool_name, placement)
    )


def _sandbox_settings_route_workspace_tools(runtime_paths: RuntimePaths, tools: ToolRegistry) -> bool:
    """Return whether the sandbox settings route any tool that works on workspace paths to a worker."""
    placement = tool_placement(runtime_paths, tools)
    return any(
        placement.runs_in_worker(name, None)
        for name, metadata in tools.entries.items()
        if metadata.consumes_workspace_paths
    )


def get_tool_by_name(
    tool_name: str,
    runtime_paths: RuntimePaths,
    *,
    tools: ToolRegistry,
    runs_in_worker: bool | None = None,
    credential_overrides: dict[str, object] | None = None,
    credentials_manager: CredentialsManager | None = None,
    runtime_config: Config | None = None,
    tool_config_overrides: dict[str, object] | None = None,
    tool_init_overrides: dict[str, object] | None = None,
    runtime_overrides: dict[str, object] | None = None,
    allowed_shared_services: frozenset[str] | None = None,
    tool_output_workspace_root: Path | None = None,
    agent_state_root: Path | None = None,
    tool_output_auto_save_threshold_bytes: int = DEFAULT_TOOL_OUTPUT_AUTO_SAVE_THRESHOLD_BYTES,
    workspace_in_worker: bool | None = None,
    in_sandbox_request: bool = False,
    worker_target: ResolvedWorkerTarget | None,
) -> Toolkit:
    """Get a tool instance by its name in ``tools``, the registry of the config the caller acts for.

    The factory, metadata, placement, and credential ownership come from ``tools``; ``runtime_config`` supplies
    configuration only.
    ``runs_in_worker`` says whether the toolkit's calls go to a worker; None routes them there when the sandbox
    settings would, or when the worker tools of the agent ``worker_target`` names would.
    ``agent_state_root`` is the constructing agent's resolved state root in the primary runtime;
    worker runtimes leave it unset because their own storage root is the state they own.
    ``workspace_in_worker`` says whether the agent's workspace-path tools run in a worker, where attachments are saved;
    None follows the sandbox settings for those tools.
    ``in_sandbox_request`` is set only by the sandbox runner building a toolkit for the request it executes.
    """
    tool_name = canonical_tool_name(tool_name)
    factory = tools.factory(tool_name)
    metadata = tools.entries[tool_name]
    routed = (
        _constructing_agent_runs_in_worker(tool_name, runtime_paths, tools, runtime_config, worker_target)
        if runs_in_worker is None
        else runs_in_worker
    )

    def build() -> Toolkit:
        worker_scope = worker_target.worker_scope if worker_target is not None else None
        routing_agent_name = worker_target.routing_agent_name if worker_target is not None else None
        if not supports_tool_name_for_worker_scope(tool_name, worker_scope):
            msg = unsupported_shared_only_integration_message(
                tool_name,
                worker_scope,
                agent_name=routing_agent_name,
                subject="Tool",
            )
            raise ToolMetadataValidationError(msg)

        # Imported on first tool construction so importing this module stays free
        # of schema-generation imports (#1436).
        from mindroom.tool_system.output_files import ToolOutputFilePolicy  # noqa: PLC0415
        from mindroom.tool_system.toolkits import (  # noqa: PLC0415
            configure_toolkit_functions,
            toolkit_function_names,
        )

        output_file_policy = ToolOutputFilePolicy.for_current_tools(
            tool_output_workspace_root,
            runtime_paths,
            auto_save_threshold_bytes=tool_output_auto_save_threshold_bytes,
        )
        settings = resolve_tool_settings(
            tool_name,
            runtime_paths,
            tools=tools,
            worker_target=worker_target,
            credentials_manager=credentials_manager,
            allowed_shared_services=allowed_shared_services,
            credential_overrides=credential_overrides,
            tool_config_overrides=tool_config_overrides,
            tool_init_overrides=tool_init_overrides,
            runtime_overrides=runtime_overrides,
        )
        construction = ToolConstruction(
            tool_name,
            factory_origin(factory),
            tool_config_signature(settings.tool_config_overrides),
        )
        tool_class = load_toolkit_class(factory)
        init_kwargs = dict(settings.values)
        extra_env_passthrough = init_kwargs.get("extra_env_passthrough")
        proxy_tool_init_overrides = dict(settings.tool_init_overrides or {})
        if tool_name == "shell":
            shell_path_prepend = init_kwargs.get("shell_path_prepend")
            if isinstance(shell_path_prepend, str):
                proxy_tool_init_overrides["shell_path_prepend"] = shell_path_prepend
            init_kwargs["allow_without_worker"] = (
                runtime_config is not None and runtime_config.defaults.allow_shell_without_worker
            )
            init_kwargs["named_workers"] = (
                routed and named_workers_refusal(routing_agent_name, runtime_config, runtime_paths) is None
            )
        init_kwargs.update(
            _build_managed_tool_init_kwargs(
                metadata,
                runtime_paths=runtime_paths,
                tools=tools,
                credentials_manager=settings.credentials_manager,
                worker_target=worker_target,
                runtime_config=runtime_config,
                tool_output_workspace_root=tool_output_workspace_root,
                saved_output_dir=(
                    output_file_policy.saved_output_dir
                    if output_file_policy is not None and output_file_policy.tools_use_workdir
                    else None
                ),
                workspace_in_worker=workspace_in_worker,
                agent_state_root=agent_state_root,
                in_sandbox_request=in_sandbox_request,
            ),
        )
        include_tools, exclude_tools = pop_implicit_toolkit_filters(metadata, init_kwargs)
        with optional_dependency_environment():
            toolkit = cast("Any", tool_class)(**init_kwargs)
        available = toolkit_function_names(toolkit)
        declared = frozenset(metadata.function_names or ())
        unknown_aliases = sorted(set(metadata.approval_aliases) - set(available) - declared)
        if unknown_aliases:
            msg = f"Tool '{tool_name}' declares approval aliases for functions it does not have: {', '.join(unknown_aliases)}"
            raise ToolMetadataValidationError(msg)
        offered = offered_function_names(
            available,
            include_tools=include_tools,
            exclude_tools=exclude_tools,
            declared_functions=declared,
        )
        configure_toolkit_functions(
            toolkit,
            offered=offered,
            output_files=output_file_policy,
            to_worker=worker_stand_ins(
                tool_name,
                toolkit,
                runtime_paths=runtime_paths,
                credentials_manager=settings.credentials_manager,
                tool_init_overrides=proxy_tool_init_overrides or None,
                tool_config_overrides=settings.tool_config_overrides,
                extra_env_passthrough=extra_env_passthrough if isinstance(extra_env_passthrough, str) else None,
                worker_target=worker_target,
                runtime_config=runtime_config,
                tools=tools,
            )
            if routed
            else None,
        )
        toolkit.tool_construction = construction
        return toolkit

    return _build_with_tool_dependencies(tool_name, metadata, runtime_paths, build)


def _build_with_tool_dependencies(
    tool_name: str,
    metadata: ToolMetadata,
    runtime_paths: RuntimePaths,
    build: Callable[[], Toolkit],
) -> Toolkit:
    """Run ``build`` once the tool's declared dependencies are present.

    When ``build`` raises ImportError and the tool's optional extra installs, ``build`` runs once more.
    """
    extra = metadata.dependency_extra or tool_name
    # Pre-check dependencies using find_spec (no side effects) before importing
    deps = metadata.dependencies or []
    if deps:
        missing = ", ".join(deps)
        try:
            installed = ensure_tool_deps(
                deps,
                extra,
                runtime_paths,
                missing_message=f"Missing dependencies for tool '{tool_name}': {missing}",
            )
        except ImportError:
            logger.warning("tool_dependencies_missing", tool_name=tool_name, missing_dependencies=missing)
            logger.warning("tool_dependency_install_required", tool_name=tool_name)
            raise
        if installed:
            logger.info("tool_optional_dependencies_auto_installed", tool_name=tool_name)

    try:
        return build()
    except ImportError as first_error:
        if not auto_install_optional_extra_for_import_retry(extra, runtime_paths):
            logger.warning("tool_import_failed", tool_name=tool_name, error=str(first_error))
            logger.warning("tool_dependency_install_required", tool_name=tool_name)
            raise

        logger.info("auto_installing_tool_optional_dependencies", tool_name=tool_name)
        try:
            return build()
        except ImportError as second_error:
            logger.warning("tool_auto_install_failed", tool_name=tool_name, error=str(second_error))
            raise second_error from first_error
