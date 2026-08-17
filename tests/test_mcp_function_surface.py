"""Pure MCP function-surface collision analysis tests."""

from mindroom.mcp.function_surface import (
    MCPFunctionCollisionReport,
    MCPFunctionSurfaceSnapshot,
    analyze_mcp_function_collisions,
)


def _snapshot(
    *,
    agent_name: str = "code",
    requester_surface: tuple[str, str] | None = None,
    local_function_names: tuple[str, ...] = (),
    server_function_sources: tuple[tuple[str, tuple[tuple[str, ...], ...]], ...],
) -> MCPFunctionSurfaceSnapshot:
    return MCPFunctionSurfaceSnapshot(
        agent_name=agent_name,
        requester_surface=requester_surface,
        local_function_names=frozenset(local_function_names),
        server_function_sources=tuple(
            (server_id, tuple(frozenset(source) for source in sources))
            for server_id, sources in server_function_sources
        ),
    )


def test_reports_local_function_collision_for_owning_server() -> None:
    """A local function collision implicates its MCP server and exact function."""
    snapshot = _snapshot(
        local_function_names=("run_shell_command",),
        server_function_sources=(("demo", (("run_shell_command", "safe"),)),),
    )

    reports = analyze_mcp_function_collisions((snapshot,))

    assert reports == (
        MCPFunctionCollisionReport(
            agent_name="code",
            requester_surface=None,
            server_id="demo",
            function_name_collisions=(
                (
                    "run_shell_command",
                    "MCP function name 'run_shell_command' collides with an existing MindRoom tool function",
                ),
            ),
        ),
    )


def test_reports_every_server_owning_cross_server_collision() -> None:
    """A cross-server collision reports every server that owns the function."""
    snapshot = _snapshot(
        server_function_sources=(
            ("alpha", (("shared", "alpha_only"),)),
            ("beta", (("shared", "beta_only"),)),
        ),
    )

    reports = analyze_mcp_function_collisions((snapshot,))

    assert {report.server_id for report in reports} == {"alpha", "beta"}
    assert {report.function_name_collisions for report in reports} == {
        (("shared", "MCP function name 'shared' collides across servers: alpha, beta"),),
    }


def test_same_function_on_distinct_requester_surfaces_does_not_collide() -> None:
    """Function ownership on distinct requester surfaces remains isolated."""
    alice = _snapshot(
        requester_surface=("user", "@alice:example.test"),
        server_function_sources=(("alpha", (("shared",),)),),
    )
    bob = _snapshot(
        requester_surface=("user", "@bob:example.test"),
        server_function_sources=(("beta", (("shared",),)),),
    )

    assert analyze_mcp_function_collisions((alice, bob)) == ()


def test_reports_duplicate_function_across_same_server_catalogs() -> None:
    """Duplicate names from two same-requester catalogs implicate that server."""
    snapshot = _snapshot(
        requester_surface=("user", "@alice:example.test"),
        server_function_sources=(("demo", (("echo", "first_only"), ("echo", "second_only"))),),
    )

    reports = analyze_mcp_function_collisions((snapshot,))

    assert reports == (
        MCPFunctionCollisionReport(
            agent_name="code",
            requester_surface=("user", "@alice:example.test"),
            server_id="demo",
            function_name_collisions=(("echo", "MCP function name 'echo' collides within server 'demo'"),),
        ),
    )


def test_unrelated_agent_surface_receives_no_report() -> None:
    """Collisions do not leak onto an unrelated agent's function surface."""
    code = _snapshot(
        local_function_names=("echo",),
        server_function_sources=(("demo", (("echo",),)),),
    )
    research = _snapshot(
        agent_name="research",
        server_function_sources=(("other", (("echo",),)),),
    )

    reports = analyze_mcp_function_collisions((code, research))

    assert {report.agent_name for report in reports} == {"code"}
    assert {report.server_id for report in reports} == {"demo"}
    assert {report.function_name_collisions for report in reports} == {
        (("echo", "MCP function name 'echo' collides with an existing MindRoom tool function"),),
    }


def test_empty_surface_receives_no_report() -> None:
    """An agent without MCP functions has no collision report."""
    snapshot = _snapshot(server_function_sources=())

    assert analyze_mcp_function_collisions((snapshot,)) == ()


def test_combines_local_and_cross_server_messages_for_same_function() -> None:
    """Each owner receives every reason that makes one function ambiguous."""
    snapshot = _snapshot(
        local_function_names=("echo",),
        server_function_sources=(
            ("alpha", (("echo",),)),
            ("beta", (("echo",),)),
        ),
    )

    reports = analyze_mcp_function_collisions((snapshot,))

    expected_collisions = (
        ("echo", "MCP function name 'echo' collides across servers: alpha, beta"),
        ("echo", "MCP function name 'echo' collides with an existing MindRoom tool function"),
    )
    assert {report.server_id for report in reports} == {"alpha", "beta"}
    assert all(report.function_name_collisions == expected_collisions for report in reports)
