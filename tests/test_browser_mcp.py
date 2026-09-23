"""Native browser catalog and transport contracts."""

import asyncio
import base64
import json
import os
import subprocess
from pathlib import Path

import pytest
from agno.media import Image
from agno.models.openai.chat import OpenAIChat
from agno.tools.function import Function, ToolResult
from mcp.types import CallToolResult, TextContent, Tool

from mindroom.custom_tools.browser_mcp import BrowserMCPTools
from mindroom.tool_system import media_transport
from mindroom.tool_system.media_transport import decode_media_result, encode_media_result
from mindroom.tool_system.output_files import ToolOutputFilePolicy, wrap_function_for_output_files
from mindroom.worker_computer.mcp_catalog import browser_mcp_catalog, verify_browser_mcp_catalog
from mindroom.worker_computer.mcp_provider import WorkerBrowserMCP


def test_native_catalog_has_fixed_safe_schemas() -> None:
    """Materializing native functions preserves the fixed trusted schemas."""
    toolkit = BrowserMCPTools()
    tools = browser_mcp_catalog()
    assert "browser_run_code_unsafe" not in tools
    assert "browser_install" not in tools
    assert "browser_route" not in tools
    assert "browser_evaluate" in tools
    assert "browser_pdf_save" in tools
    assert toolkit.get_async_functions().keys() == tools.keys()
    for name, tool in tools.items():
        assert toolkit.get_async_functions()[name].parameters == tool["inputSchema"]


def test_native_screenshot_wire_keeps_optional_fields_optional() -> None:
    """OpenAI formatting cannot promote omitted native screenshot fields to required empty values."""
    function = BrowserMCPTools().get_async_functions()["browser_take_screenshot"].model_copy(deep=True)
    effective_strict = True if function.strict is None else function.strict
    function.process_entrypoint(strict=effective_strict)

    formatted = OpenAIChat(id="gpt-6-astra", api_key="sk-test")._format_tools([function])[0]["function"]

    assert formatted["strict"] is False
    assert formatted["parameters"]["required"] == ["type", "scale"]


def test_primary_materialization_has_no_process_or_workspace_effects(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Observe real launch seams and the configured runtime/workspace directories."""
    # Bootstrap the optional tool registry only for this materialization test.
    import mindroom.tools  # noqa: PLC0415, F401 - normal registry bootstrap
    from mindroom.constants import resolve_primary_runtime_paths  # noqa: PLC0415
    from mindroom.tool_system.metadata import get_tool_by_name  # noqa: PLC0415
    from mindroom.tool_system.worker_routing import (  # noqa: PLC0415
        ToolExecutionIdentity,
        build_agent_toolkit_worker_target,
        tool_execution_identity,
        worker_root_path,
    )

    paths = resolve_primary_runtime_paths(
        config_path=tmp_path / "config.yaml",
        storage_path=tmp_path / "runtime",
        process_env={"MINDROOM_WORKER_COMPUTER_ENABLED": "1", "MINDROOM_WORKER_BACKEND": "docker"},
    )
    workspace = paths.storage_root / "agents" / "writer" / "workspace"
    workspace.mkdir(parents=True)
    marker = workspace / "existing.txt"
    marker.write_text("preserve")
    before = {str(path.relative_to(tmp_path)): path.read_bytes() for path in tmp_path.rglob("*") if path.is_file()}
    before_directories = {str(path.relative_to(tmp_path)) for path in tmp_path.rglob("*") if path.is_dir()}

    def forbidden_launch(*_args: object, **_kwargs: object) -> None:
        pytest.fail("Primary metadata materialization launched a process")

    monkeypatch.setattr(subprocess, "Popen", forbidden_launch)
    monkeypatch.setattr(asyncio, "create_subprocess_exec", forbidden_launch)
    monkeypatch.setattr(asyncio, "create_subprocess_shell", forbidden_launch)
    identity = ToolExecutionIdentity(
        channel="matrix",
        agent_name="writer",
        requester_id="@viewer:fixture",
        room_id="!room:fixture",
        thread_id=None,
        resolved_thread_id=None,
        session_id="fixture",
    )
    target = build_agent_toolkit_worker_target(
        "user_agent",
        "writer",
        is_private=False,
        execution_identity=identity,
        runtime_paths=paths,
    )
    with tool_execution_identity(identity):
        toolkit = get_tool_by_name(
            "browser_mcp",
            paths,
            worker_target=target,
            worker_tools_override=["browser_mcp"],
            tool_output_workspace_root=workspace,
        )
    assert toolkit.get_async_functions().keys() == browser_mcp_catalog().keys()
    assert {
        str(path.relative_to(tmp_path)): path.read_bytes() for path in tmp_path.rglob("*") if path.is_file()
    } == before
    # Generic primary materialization initializes credential directories for every
    # toolkit; it must not create a browser profile, output directory or workspace.
    worker_root = worker_root_path(paths.storage_root, target.worker_key)
    allowed = {
        paths.storage_root / "credentials",
        worker_root.parent,
        worker_root,
        worker_root / "credentials",
        worker_root / ".shared_credentials",
    }
    assert {str(path.relative_to(tmp_path)) for path in tmp_path.rglob("*") if path.is_dir()} == (
        before_directories | {str(path.relative_to(tmp_path)) for path in allowed}
    )


@pytest.mark.asyncio
async def test_native_calls_require_worker_binding() -> None:
    """Primary-side materialization never authorizes local execution."""
    toolkit = BrowserMCPTools()
    function = toolkit.get_async_functions()["browser_snapshot"]
    with pytest.raises(RuntimeError, match="dedicated computer worker"):
        await function.entrypoint()


def test_catalog_drift_fails_closed() -> None:
    """A different native schema cannot silently widen the local surface."""
    tools = list(browser_mcp_catalog().values())
    verify_browser_mcp_catalog(tools)
    changed = json.loads(json.dumps(tools))
    changed[0]["inputSchema"]["properties"]["extra"] = {"type": "string"}
    with pytest.raises(RuntimeError, match="catalog"):
        verify_browser_mcp_catalog(changed)
    with pytest.raises(RuntimeError, match="catalog"):
        verify_browser_mcp_catalog(tools[:-1])


def test_image_result_roundtrip() -> None:
    """JSON transport preserves real Agno inline image bytes."""
    result = ToolResult(content="screen", images=[Image(content=b"png", mime_type="image/png")])
    decoded = decode_media_result(json.loads(json.dumps(encode_media_result(result))))
    assert isinstance(decoded, ToolResult)
    assert decoded.content == "screen"
    assert decoded.images[0].content == b"png"
    assert decoded.images[0].mime_type == "image/png"


def test_json_receipt_is_opaque() -> None:
    """Nested marker-shaped ordinary results are never recursively interpreted."""
    value = {"mindroom_tool_result": {"version": 777}}
    assert decode_media_result(encode_media_result(value)) == value


@pytest.mark.parametrize(
    "payload",
    [None, "old worker", {}, {"mindroom_tool_result": {"version": True, "kind": "json", "value": 1}}],
)
def test_malformed_envelopes_fail(payload: object) -> None:
    """Missing and malformed wire envelopes produce bounded protocol errors."""
    with pytest.raises(ValueError, match="worker tool result"):
        decode_media_result(payload)


@pytest.mark.parametrize(
    ("mime", "data"),
    [("text/html", "eA=="), ("image/png", "!!!"), ("image/png", ""), ("image/png", "eB==")],
)
def test_invalid_images_fail(mime: str, data: str) -> None:
    """Only strict canonical inline screenshot bytes cross the boundary."""
    payload = {
        "mindroom_tool_result": {
            "version": 1,
            "kind": "tool_result",
            "audios": [],
            "videos": [],
            "files": [],
            "content": "",
            "images": [{"mime_type": mime, "data_base64": data}],
        },
    }
    with pytest.raises(ValueError, match="worker tool result"):
        decode_media_result(payload)


@pytest.mark.asyncio
@pytest.mark.parametrize(("media", "path"), [(False, None), (False, "result.txt"), (True, None), (True, "result.txt")])
async def test_output_wrapper_precedes_wire_encoding(tmp_path: Path, media: bool, path: str | None) -> None:
    """Text receipts and explicit media redirect errors keep established workspace semantics."""

    async def result() -> ToolResult:
        return ToolResult(
            content="native text",
            images=[Image(content=b"image", mime_type="image/png")] if media else None,
        )

    function = Function(name="browser_snapshot", entrypoint=result)
    wrap_function_for_output_files(function, ToolOutputFilePolicy(workspace_root=tmp_path, auto_save_threshold_bytes=5))
    output = await function.entrypoint(mindroom_output_path=path)
    decoded = decode_media_result(encode_media_result(output))
    if media and path is None:
        assert isinstance(decoded, ToolResult)
        assert decoded.images[0].content == b"image"
        assert not list(tmp_path.iterdir())
    else:
        assert decoded == output
        assert isinstance(decoded, dict)
        receipt = decoded["mindroom_tool_output"]
        assert receipt["status"] == ("error" if media else "saved_to_file")
        if not media:
            assert (tmp_path / receipt["path"]).read_text() == "native text"


def test_worker_launch_has_fixed_sandbox_and_workspace(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """Launch cannot expand capabilities or disable Chromium's sandbox."""
    monkeypatch.setenv("BROWSER_EXECUTABLE_PATH", "/untrusted/browser")
    provider = WorkerBrowserMCP(display=":77", workspace=tmp_path / "workspace", storage_root=tmp_path / "storage")
    parameters = provider._server_parameters()
    assert parameters.args[0] == "/opt/mindroom-browser-mcp/node_modules/@playwright/mcp/cli.js"
    assert parameters.args[parameters.args.index("--executable-path") + 1] == "/opt/mindroom-browser-mcp/chromium"
    assert "--sandbox" in parameters.args
    assert "--headless" not in parameters.args
    assert "--no-sandbox" not in parameters.args
    assert "--block-service-workers" in parameters.args
    assert parameters.args[parameters.args.index("--proxy-server") + 1] == provider._proxy.endpoint
    assert parameters.args[parameters.args.index("--proxy-bypass") + 1] == "<-loopback>"
    assert parameters.args[parameters.args.index("--caps") + 1] == "vision,pdf"
    assert parameters.cwd == str(tmp_path / "workspace")
    assert parameters.env["DISPLAY"] == ":77"
    assert not list(tmp_path.iterdir())


@pytest.mark.asyncio
@pytest.mark.parametrize("drift", [False, True])
async def test_worker_bootstraps_guard_and_closes_on_drift(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    drift: bool,
) -> None:
    """Native requests cannot precede discovery and initial guard installation."""
    calls = []

    class Session:
        running = True

        def __init__(self, _parameters: object) -> None:
            pass

        async def list_tools(self) -> tuple[Tool, ...]:
            calls.append("catalog")
            return tuple(Tool.model_validate(tool) for tool in browser_mcp_catalog().values())[1 if drift else 0 :]

        async def call_tool(self, name: str, arguments: dict[str, object]) -> CallToolResult:
            calls.append((name, arguments))
            return CallToolResult(content=[TextContent(type="text", text=name)])

        async def close(self) -> None:
            calls.append("closed")

    monkeypatch.setattr("mindroom.worker_computer.mcp_provider.PlaywrightMCPSession", Session)
    provider = WorkerBrowserMCP(display=":77", workspace=tmp_path / "workspace", storage_root=tmp_path / "storage")
    try:
        if drift:
            with pytest.raises(RuntimeError, match="catalog"):
                await provider.execute("browser_snapshot", {})
            assert calls == ["catalog", "closed"]
            return
        await provider.execute("browser_snapshot", {})
        await provider.execute("browser_tabs", {"action": "list"})
        assert calls == [
            "catalog",
            ("browser_tabs", {"action": "list"}),
            ("browser_snapshot", {}),
            ("browser_tabs", {"action": "list"}),
        ]
    finally:
        await provider.close()
        assert provider._proxy._server is None
        assert not provider._proxy._connections
    assert calls[-2:] == [("browser_close", {}), "closed"]


@pytest.mark.asyncio
@pytest.mark.parametrize("owner", ["dead", "live", "unparseable"])
async def test_worker_recovers_profile_only_before_session_start(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    owner: str,
) -> None:
    """Native startup removes only proven dead locks before MCP can open the profile."""
    profile = tmp_path / "storage" / "browser-profiles" / "native-mcp"
    profile.mkdir(parents=True)
    cookies = profile / "Default" / "Cookies"
    cookies.parent.mkdir()
    cookies.write_bytes(b"saved-login")
    lock = profile / "SingletonLock"
    target = {"dead": "old-worker-999999999", "live": f"worker-{os.getpid()}", "unparseable": "unknown-owner"}[owner]
    lock.symlink_to(target)
    socket = profile / "SingletonSocket"
    socket.symlink_to("/missing/chromium/SingletonSocket")
    cookie = profile / "SingletonCookie"
    cookie.symlink_to("123456789")

    class Session:
        running = True

        def __init__(self, _parameters: object) -> None:
            assert lock.is_symlink() is (owner != "dead")
            assert cookies.read_bytes() == b"saved-login"
            assert socket.readlink() == Path("/missing/chromium/SingletonSocket")
            assert cookie.readlink() == Path("123456789")

        async def list_tools(self) -> tuple[Tool, ...]:
            return tuple(Tool.model_validate(tool) for tool in browser_mcp_catalog().values())

        async def call_tool(self, name: str, _arguments: dict[str, object]) -> CallToolResult:
            return CallToolResult(content=[TextContent(type="text", text=name)])

        async def close(self) -> None:
            pass

    monkeypatch.setattr("mindroom.worker_computer.mcp_provider.PlaywrightMCPSession", Session)
    provider = WorkerBrowserMCP(display=":77", workspace=tmp_path / "workspace", storage_root=tmp_path / "storage")
    try:
        assert (await provider.execute("browser_snapshot", {})).content == "browser_snapshot"
        if owner == "dead":
            # A ready session must not inspect or remove locks on subsequent calls.
            lock.symlink_to(target)
        assert (await provider.execute("browser_snapshot", {})).content == "browser_snapshot"
        assert lock.readlink() == Path(target)
        assert cookies.read_bytes() == b"saved-login"
    finally:
        await provider.close()


def test_codec_bounds_before_image_decode(monkeypatch: pytest.MonkeyPatch) -> None:
    """Oversized payloads fail before base64 allocates decoded bytes."""
    monkeypatch.setattr(media_transport, "_MAX_ENCODED_BYTES", 4)
    payload = {
        "mindroom_tool_result": {
            "version": 1,
            "kind": "tool_result",
            "audios": [],
            "videos": [],
            "files": [],
            "content": "",
            "images": [{"mime_type": "image/png", "data_base64": "aW1hZ2U="}],
        },
    }
    with pytest.raises(ValueError, match="worker tool result"):
        decode_media_result(payload)


@pytest.fixture
def small_image_limits(monkeypatch: pytest.MonkeyPatch) -> None:
    """Exercise padding and byte limits with tiny independent image payloads."""
    monkeypatch.setattr(media_transport, "MAX_MEDIA_BYTES", 4)
    monkeypatch.setattr(media_transport, "MAX_TOTAL_MEDIA_BYTES", 8)
    monkeypatch.setattr(media_transport, "_MAX_ENCODED_BYTES", 8)


@pytest.mark.usefixtures("small_image_limits")
@pytest.mark.parametrize("contents", [[b"abcd", b"efgh"], [b"x"] * 8, [b"abc", b"def", b"gh"]])
def test_independently_padded_images_roundtrip_at_aggregate_limit(contents: list[bytes]) -> None:
    """All eight allowed raw bytes survive transport despite independent padding."""
    result = ToolResult(content="screens", images=[Image(content=data, mime_type="image/png") for data in contents])
    decoded = decode_media_result(json.loads(json.dumps(encode_media_result(result))))
    assert isinstance(decoded, ToolResult)
    assert decoded.content == "screens"
    assert [image.content for image in decoded.images] == contents
    assert all(image.mime_type == "image/png" for image in decoded.images)


@pytest.mark.usefixtures("small_image_limits")
@pytest.mark.parametrize("contents", [[b"abcd", b"efgh", b"i"], [b"abcde"], [b"x"] * 9, [b""]])
@pytest.mark.parametrize("operation", ["encode", "decode"])
def test_image_limits_reject_oversized_or_empty_images(contents: list[bytes], operation: str) -> None:
    """Padding allowance never widens raw aggregate, per-image or count limits."""
    result = ToolResult(content="screens", images=[Image(content=data, mime_type="image/png") for data in contents])
    if operation == "encode":
        with pytest.raises(ValueError, match="worker tool result"):
            encode_media_result(result)
    else:
        payload = {
            "mindroom_tool_result": {
                "version": 1,
                "kind": "tool_result",
                "audios": [],
                "videos": [],
                "files": [],
                "content": "screens",
                "images": [
                    {"mime_type": "image/png", "data_base64": base64.b64encode(data).decode("ascii")}
                    for data in contents
                ],
            },
        }
        with pytest.raises(ValueError, match="worker tool result"):
            decode_media_result(payload)
