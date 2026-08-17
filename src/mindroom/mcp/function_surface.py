"""Pure collision policy for immutable MCP function surfaces.

No provider surface may contain the same function name twice.
Static discovery invalidates conflicting catalogs, dynamic loading rejects the newly requested surface, and final agent
construction hides any collision that appears between those validation boundaries.
"""

from collections import Counter
from collections.abc import Collection
from dataclasses import dataclass

__all__ = [
    "MCPFunctionCollisionReport",
    "MCPFunctionSurfaceSnapshot",
    "analyze_mcp_function_collisions",
    "local_mcp_function_name_collisions",
]


@dataclass(frozen=True, slots=True)
class MCPFunctionSurfaceSnapshot:
    """One agent and requester surface prepared by the MCP manager."""

    agent_name: str
    requester_surface: tuple[str, str] | None
    local_function_names: frozenset[str]
    server_function_sources: tuple[tuple[str, tuple[frozenset[str], ...]], ...]


@dataclass(frozen=True, slots=True)
class MCPFunctionCollisionReport:
    """Collisions owned by one server on one agent and requester surface."""

    agent_name: str
    requester_surface: tuple[str, str] | None
    server_id: str
    function_name_collisions: tuple[tuple[str, str], ...]


def local_mcp_function_name_collisions(
    local_function_names: Collection[str],
    mcp_function_names: Collection[str],
) -> frozenset[str]:
    """Return function names that violate the shared local/MCP uniqueness policy."""
    return frozenset(local_function_names) & frozenset(mcp_function_names)


def analyze_mcp_function_collisions(
    snapshots: tuple[MCPFunctionSurfaceSnapshot, ...],
) -> tuple[MCPFunctionCollisionReport, ...]:
    """Return deterministic collision reports without reading or mutating runtime state."""
    reports: list[MCPFunctionCollisionReport] = []
    for snapshot in snapshots:
        server_ids_by_function_name: dict[str, set[str]] = {}
        collisions_by_server: dict[str, set[tuple[str, str]]] = {}
        for server_id, function_sources in snapshot.server_function_sources:
            function_name_counts = Counter(name for source in function_sources for name in source)
            duplicate_function_names = {name for name, count in function_name_counts.items() if count > 1}
            for function_name in duplicate_function_names:
                message = f"MCP function name '{function_name}' collides within server '{server_id}'"
                collisions_by_server.setdefault(server_id, set()).add(
                    (function_name, message),
                )
            for function_name in function_name_counts:
                server_ids_by_function_name.setdefault(function_name, set()).add(server_id)

        local_collision_names = local_mcp_function_name_collisions(
            snapshot.local_function_names,
            server_ids_by_function_name,
        )
        for function_name, server_ids in server_ids_by_function_name.items():
            messages: list[str] = []
            if function_name in local_collision_names:
                messages.append(
                    f"MCP function name '{function_name}' collides with an existing MindRoom tool function",
                )
            if len(server_ids) > 1:
                server_list = ", ".join(sorted(server_ids))
                messages.append(f"MCP function name '{function_name}' collides across servers: {server_list}")
            if not messages:
                continue
            for server_id in server_ids:
                collisions_by_server.setdefault(server_id, set()).update(
                    (function_name, message) for message in messages
                )

        reports.extend(
            MCPFunctionCollisionReport(
                agent_name=snapshot.agent_name,
                requester_surface=snapshot.requester_surface,
                server_id=server_id,
                function_name_collisions=tuple(sorted(collisions)),
            )
            for server_id, collisions in sorted(collisions_by_server.items())
        )
    return tuple(reports)
