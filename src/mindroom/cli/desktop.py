"""CLI for the lightweight Matrix-attached desktop bridge."""

from __future__ import annotations

import asyncio
import os
import sys
import time
from pathlib import Path
from typing import TYPE_CHECKING

import typer
from rich.console import Console

from mindroom.desktop.login_method import DesktopLoginMethod

if TYPE_CHECKING:
    from collections.abc import Mapping

    import nio

    from mindroom.constants import RuntimePaths
    from mindroom.desktop.session import DesktopMatrixSession

_console = Console()
_error_console = Console(stderr=True)
_DESKTOP_EXTRA = "desktop"
_DESKTOP_DEPENDENCIES = ["pyautogui"]
_MACOS_DESKTOP_DEPENDENCIES = [
    "pyobjc-framework-applicationservices",
    "pyobjc-framework-cocoa",
]

desktop_app = typer.Typer(
    name="desktop",
    help="Connect allowlisted local applications to cloud MindRoom over Matrix E2EE.",
    no_args_is_help=True,
)


def _activate_desktop_runtime(config_path: Path | None, *, storage_path: Path | None) -> RuntimePaths:
    """Resolve local Desktop state without making it depend on the working directory."""
    from mindroom import constants  # noqa: PLC0415
    from mindroom.cli.config import activate_cli_runtime  # noqa: PLC0415

    process_env = constants.exported_process_env()
    has_explicit_config = any(
        value is not None and str(value).strip()
        for value in (
            config_path,
            process_env.get("MINDROOM_CONFIG_PATH"),
        )
    )
    if not has_explicit_config:
        config_path = Path.home() / ".mindroom" / "config.yaml"
    return activate_cli_runtime(config_path, storage_path=storage_path)


def _ensure_desktop_dependencies(runtime_paths: RuntimePaths) -> None:
    """Install the optional desktop runtime before starting the bridge."""
    from mindroom.desktop.provider import DesktopProviderError  # noqa: PLC0415
    from mindroom.tool_system.dependencies import ensure_optional_deps  # noqa: PLC0415

    dependencies = [*_DESKTOP_DEPENDENCIES]
    if sys.platform == "darwin":
        dependencies.extend(_MACOS_DESKTOP_DEPENDENCIES)
    try:
        ensure_optional_deps(dependencies, _DESKTOP_EXTRA, runtime_paths)
    except ImportError as exc:
        raise DesktopProviderError(str(exc)) from exc


def _request_required_desktop_permissions() -> None:
    """Request required local permissions before connecting the Desktop bridge."""
    from mindroom.desktop.provider import (  # noqa: PLC0415
        DesktopProviderError,
        request_macos_desktop_permissions,
    )

    missing_permissions = request_macos_desktop_permissions()
    if not missing_permissions:
        return
    permission_names = " and ".join(missing_permissions)
    permission_label = "permission" if len(missing_permissions) == 1 else "permissions"
    msg = (
        f"macOS requested {permission_names} {permission_label}. Grant the requested access to the terminal app "
        "running this command in System Settings > Privacy & Security, fully quit and reopen that app, then run "
        "`mindroom desktop run` again."
    )
    raise DesktopProviderError(msg)


@desktop_app.command("login")
def desktop_login(
    user_id: str | None = typer.Option(
        None,
        "--user-id",
        help="Expected Matrix user ID; required for password login and optional for SSO.",
    ),
    homeserver: str | None = typer.Option(
        None,
        "--homeserver",
        help="Matrix homeserver URL; defaults to the configured MindRoom homeserver.",
    ),
    login_method: DesktopLoginMethod = typer.Option(  # noqa: B008
        DesktopLoginMethod.AUTO,
        "--login-method",
        case_sensitive=False,
        help="Matrix login method. Auto uses password when advertised, otherwise browser SSO.",
    ),
    sso_idp: str | None = typer.Option(
        None,
        "--sso-idp",
        help="Matrix SSO identity-provider ID. Selects SSO when login method is auto.",
    ),
    open_browser: bool = typer.Option(
        True,
        "--open-browser/--no-open-browser",
        help="Open Matrix SSO in the default browser; otherwise print the URL.",
    ),
    cloudflare_access: bool = typer.Option(
        False,
        "--cloudflare-access",
        envvar="MINDROOM_DESKTOP_CLOUDFLARE_ACCESS",
        help="Authenticate Matrix requests interactively with the local cloudflared CLI.",
    ),
    replace: bool = typer.Option(False, "--replace", help="Replace the saved session with a fresh Matrix device."),
    matrix_http_headers_file: Path | None = typer.Option(  # noqa: B008
        None,
        "--matrix-http-headers-file",
        envvar="MINDROOM_DESKTOP_MATRIX_HTTP_HEADERS_FILE",
        help="Owner-only JSON file of HTTP headers added to every Matrix request.",
    ),
    config_path: Path | None = typer.Option(  # noqa: B008
        None,
        "--config",
        "-c",
        help="MindRoom config path used for runtime env.",
    ),
    storage_path: Path | None = typer.Option(  # noqa: B008
        None,
        "--storage-path",
        "-s",
        help="Desktop bridge state directory.",
    ),
) -> None:
    """Log in once, create an Olm device, and save its access token privately."""
    from mindroom.constants import runtime_matrix_homeserver  # noqa: PLC0415
    from mindroom.desktop.cloudflare_access import (  # noqa: PLC0415
        CloudflareAccessError,
        cloudflare_access_headers,
    )
    from mindroom.desktop.session import (  # noqa: PLC0415
        DesktopSessionError,
        desktop_session_path,
        load_desktop_http_headers,
        resolve_desktop_login_method,
    )
    from mindroom.desktop.sso import DesktopSsoError, receive_sso_login_token  # noqa: PLC0415

    runtime_paths = _activate_desktop_runtime(config_path, storage_path=storage_path)
    session_path = desktop_session_path(runtime_paths)
    if session_path.exists() and not replace:
        _error_console.print(f"[red]Error:[/red] Session already exists at {session_path}. Use --replace explicitly.")
        raise typer.Exit(1)
    try:
        resolved_homeserver = homeserver or runtime_matrix_homeserver(runtime_paths)
        http_headers: Mapping[str, str] | None = load_desktop_http_headers(matrix_http_headers_file)
        if cloudflare_access:
            http_headers = cloudflare_access_headers(resolved_homeserver, http_headers)
        requested_login_method = _login_method_for_sso_idp(login_method, sso_idp=sso_idp)
        resolved_login_method = asyncio.run(
            resolve_desktop_login_method(
                requested_login_method,
                homeserver=resolved_homeserver,
                runtime_paths=runtime_paths,
                http_headers=http_headers,
            ),
        )
        password: str | None = None
        login_token: str | None = None
        if resolved_login_method is DesktopLoginMethod.PASSWORD:
            user_id = _require_password_user_id(user_id)
            password = os.environ.get("MINDROOM_DESKTOP_MATRIX_PASSWORD")
            if password is None:
                password = typer.prompt("Matrix password", hide_input=True, confirmation_prompt=False)
        else:
            login_token = receive_sso_login_token(
                resolved_homeserver,
                open_browser=open_browser,
                announce=lambda message: _console.print(message, markup=False),
                idp_id=sso_idp,
            )
        asyncio.run(
            _login_and_save(
                runtime_paths=runtime_paths,
                homeserver=resolved_homeserver,
                user_id=user_id,
                password=password,
                login_token=login_token,
                session_path=session_path,
                http_headers=http_headers,
                cloudflare_access=cloudflare_access,
            ),
        )
    except (CloudflareAccessError, DesktopSessionError, DesktopSsoError) as exc:
        _error_console.print(f"[red]Desktop login failed:[/red] {exc}")
        raise typer.Exit(1) from None


def _require_password_user_id(user_id: str | None) -> str:
    """Return a password-login identity or raise one friendly CLI error."""
    from mindroom.desktop.session import DesktopSessionError  # noqa: PLC0415

    if user_id is None:
        msg = "--user-id is required for Matrix password login."
        raise DesktopSessionError(msg)
    return user_id


def _login_method_for_sso_idp(
    login_method: DesktopLoginMethod,
    *,
    sso_idp: str | None,
) -> DesktopLoginMethod:
    """Make an explicit SSO provider select SSO without hiding conflicts."""
    if sso_idp is None:
        return login_method
    if login_method is DesktopLoginMethod.PASSWORD:
        from mindroom.desktop.session import DesktopSessionError  # noqa: PLC0415

        msg = "--sso-idp cannot be used with --login-method password."
        raise DesktopSessionError(msg)
    return DesktopLoginMethod.SSO


async def _login_and_save(
    *,
    runtime_paths: RuntimePaths,
    homeserver: str,
    user_id: str | None,
    password: str | None,
    login_token: str | None,
    session_path: Path,
    http_headers: Mapping[str, str] | None = None,
    cloudflare_access: bool = False,
) -> None:
    from mindroom.desktop.session import (  # noqa: PLC0415
        client_ed25519_fingerprint,
        login_desktop_client,
        save_desktop_session,
    )

    client, session = await login_desktop_client(
        homeserver=homeserver,
        user_id=user_id,
        password=password,
        login_token=login_token,
        runtime_paths=runtime_paths,
        http_headers=http_headers,
        cloudflare_access=cloudflare_access,
    )
    try:
        save_desktop_session(session_path, session)
        fingerprint = client_ed25519_fingerprint(client)
        _print_device_identity(session, fingerprint=fingerprint, session_path=session_path)
    finally:
        await client.close()


def _print_device_identity(
    session: DesktopMatrixSession,
    *,
    fingerprint: str,
    session_path: Path,
) -> None:
    _console.print("[green]Desktop Matrix device ready.[/green]")
    _console.print(f"  Session: {session_path}")
    _console.print(f"  User: {session.user_id}")
    _console.print(f"  Device: {session.device_id}")
    _console.print(f"  Ed25519: {fingerprint}")
    _console.print("\nUse the pairing command returned by `!desktop setup` in the direct agent chat.")


@desktop_app.command("pair")
def desktop_pair(
    code: str = typer.Option(..., "--code", help="Short-lived code returned by !desktop setup."),
    controller_user_id: str = typer.Option(..., "--controller-user-id", help="Pinned cloud controller Matrix user."),
    controller_device_id: str = typer.Option(..., "--controller-device-id", help="Pinned cloud controller device."),
    controller_ed25519: str = typer.Option(..., "--controller-ed25519", help="Pinned controller fingerprint."),
    cloudflare_access: bool = typer.Option(
        False,
        "--cloudflare-access",
        envvar="MINDROOM_DESKTOP_CLOUDFLARE_ACCESS",
        help="Authenticate Matrix requests interactively with the local cloudflared CLI.",
    ),
    matrix_http_headers_file: Path | None = typer.Option(  # noqa: B008
        None,
        "--matrix-http-headers-file",
        envvar="MINDROOM_DESKTOP_MATRIX_HTTP_HEADERS_FILE",
        help="Owner-only JSON file of HTTP headers added to every Matrix request.",
    ),
    config_path: Path | None = typer.Option(  # noqa: B008
        None,
        "--config",
        "-c",
        help="MindRoom config path used for runtime env.",
    ),
    storage_path: Path | None = typer.Option(  # noqa: B008
        None,
        "--storage-path",
        "-s",
        help="Desktop bridge state directory.",
    ),
) -> None:
    """Claim one requester-agent pairing through authenticated Matrix E2EE."""
    from mindroom.desktop.cloudflare_access import (  # noqa: PLC0415
        CloudflareAccessError,
        cloudflare_access_headers,
    )
    from mindroom.desktop.session import (  # noqa: PLC0415
        DesktopSessionError,
        desktop_session_path,
        load_desktop_http_headers,
        load_desktop_session,
    )
    from mindroom.matrix.olm_to_device import OlmToDeviceError  # noqa: PLC0415

    runtime_paths = _activate_desktop_runtime(config_path, storage_path=storage_path)
    try:
        http_headers: Mapping[str, str] | None = load_desktop_http_headers(matrix_http_headers_file)
        session = load_desktop_session(desktop_session_path(runtime_paths))
        if cloudflare_access or session.cloudflare_access:
            http_headers = cloudflare_access_headers(session.homeserver, http_headers)
        verification = asyncio.run(
            _pair_desktop(
                runtime_paths=runtime_paths,
                session=session,
                code=code,
                controller_user_id=controller_user_id,
                controller_device_id=controller_device_id,
                controller_ed25519=controller_ed25519,
                http_headers=http_headers,
            ),
        )
    except (CloudflareAccessError, DesktopSessionError, OlmToDeviceError, ValueError) as exc:
        _error_console.print(f"[red]Desktop pairing failed:[/red] {exc}")
        raise typer.Exit(1) from None
    _console.print("[green]Pairing claim accepted.[/green] Return to the chat and run:")
    _console.print(f"!desktop confirm {code} {verification}", markup=False)


@desktop_app.command("setup")
def desktop_setup(
    code: str = typer.Option(..., "--code", help="Short-lived code returned by !desktop setup."),
    controller_user_id: str = typer.Option(..., "--controller-user-id", help="Pinned cloud controller Matrix user."),
    controller_device_id: str = typer.Option(..., "--controller-device-id", help="Pinned cloud controller device."),
    controller_ed25519: str = typer.Option(..., "--controller-ed25519", help="Pinned controller fingerprint."),
    user_id: str | None = typer.Option(
        None,
        "--user-id",
        help="Expected Matrix user ID; required for password login and optional for SSO.",
    ),
    homeserver: str | None = typer.Option(
        None,
        "--homeserver",
        help="Matrix homeserver URL; defaults to the configured MindRoom homeserver.",
    ),
    cloudflare_access: bool = typer.Option(
        False,
        "--cloudflare-access",
        envvar="MINDROOM_DESKTOP_CLOUDFLARE_ACCESS",
        help="Authenticate Matrix requests interactively with the local cloudflared CLI.",
    ),
    matrix_http_headers_file: Path | None = typer.Option(  # noqa: B008
        None,
        "--matrix-http-headers-file",
        envvar="MINDROOM_DESKTOP_MATRIX_HTTP_HEADERS_FILE",
        help="Owner-only JSON file of HTTP headers added to every Matrix request.",
    ),
    config_path: Path | None = typer.Option(  # noqa: B008
        None,
        "--config",
        "-c",
        help="MindRoom config path used for runtime env.",
    ),
    storage_path: Path | None = typer.Option(  # noqa: B008
        None,
        "--storage-path",
        "-s",
        help="Desktop bridge state directory.",
    ),
) -> None:
    """Log in when needed, then claim one requester-agent pairing."""
    from mindroom.desktop.session import desktop_session_path  # noqa: PLC0415

    runtime_paths = _activate_desktop_runtime(config_path, storage_path=storage_path)
    if not desktop_session_path(runtime_paths).exists():
        desktop_login(
            user_id=user_id,
            homeserver=homeserver,
            login_method=DesktopLoginMethod.AUTO,
            sso_idp=None,
            open_browser=True,
            cloudflare_access=cloudflare_access,
            replace=False,
            matrix_http_headers_file=matrix_http_headers_file,
            config_path=config_path,
            storage_path=storage_path,
        )
    desktop_pair(
        code=code,
        controller_user_id=controller_user_id,
        controller_device_id=controller_device_id,
        controller_ed25519=controller_ed25519,
        cloudflare_access=cloudflare_access,
        matrix_http_headers_file=matrix_http_headers_file,
        config_path=config_path,
        storage_path=storage_path,
    )


async def _pair_desktop(
    *,
    runtime_paths: RuntimePaths,
    session: DesktopMatrixSession,
    code: str,
    controller_user_id: str,
    controller_device_id: str,
    controller_ed25519: str,
    http_headers: Mapping[str, str] | None = None,
) -> str:
    from mindroom.desktop.pairing_client import send_desktop_pairing_claim  # noqa: PLC0415
    from mindroom.desktop.session import open_desktop_client  # noqa: PLC0415
    from mindroom.matrix.olm_to_device import PinnedMatrixDevice  # noqa: PLC0415

    controller = PinnedMatrixDevice(
        user_id=controller_user_id,
        device_id=controller_device_id,
        ed25519=controller_ed25519,
    )
    client = await open_desktop_client(session, runtime_paths=runtime_paths, http_headers=http_headers)
    try:
        return await send_desktop_pairing_claim(
            client,
            controller,
            code=code,
        )
    finally:
        await client.close()


@desktop_app.command("run")
def desktop_run(
    controller_user_id: str = typer.Option(..., "--controller-user-id", help="Pinned cloud controller Matrix user."),
    controller_device_id: str = typer.Option(..., "--controller-device-id", help="Pinned cloud controller device."),
    controller_ed25519: str = typer.Option(..., "--controller-ed25519", help="Pinned controller fingerprint."),
    allow_requester: list[str] = typer.Option(  # noqa: B008
        ...,
        "--allow-requester",
        help="Human Matrix requester allowed to operate this desktop; repeat as needed.",
    ),
    allow_agent: list[str] = typer.Option(  # noqa: B008
        ...,
        "--allow-agent",
        help="MindRoom agent name allowed to operate this desktop; repeat as needed.",
    ),
    allow_app: list[str] = typer.Option(  # noqa: B008
        ...,
        "--allow-app",
        help="Exact local application ID exposed to the agent; repeat as needed.",
    ),
    allow_control: bool = typer.Option(
        False,
        "--allow-control",
        help="Enable semantic and fallback input for a short local lease. Default is observe-only.",
    ),
    lease_minutes: int = typer.Option(15, "--lease-minutes", min=1, max=60, help="Local control lease duration."),
    max_screenshot_width: int = typer.Option(1600, "--max-screenshot-width", min=320, max=3840),
    jpeg_quality: int = typer.Option(80, "--jpeg-quality", min=40, max=95),
    browser_extension: bool = typer.Option(
        False,
        "--browser-extension",
        help="Expose Playwright MCP control of an existing browser profile when its extension is installed.",
    ),
    browser_executable: Path | None = typer.Option(  # noqa: B008
        None,
        "--browser-executable",
        help="Chrome-family executable to open the Playwright extension connection page, including Brave.",
    ),
    browser_user_data_dir: Path | None = typer.Option(  # noqa: B008
        None,
        "--browser-user-data-dir",
        help="Existing browser user-data root containing the profile where the extension is installed.",
    ),
    browser_timeout_seconds: int = typer.Option(
        90,
        "--browser-timeout-seconds",
        min=1,
        max=120,
        help="Local Playwright MCP call timeout.",
    ),
    log_level: str = typer.Option("INFO", "--log-level", "-l"),
    cloudflare_access: bool = typer.Option(
        False,
        "--cloudflare-access",
        envvar="MINDROOM_DESKTOP_CLOUDFLARE_ACCESS",
        help="Authenticate Matrix requests interactively with the local cloudflared CLI.",
    ),
    matrix_http_headers_file: Path | None = typer.Option(  # noqa: B008
        None,
        "--matrix-http-headers-file",
        envvar="MINDROOM_DESKTOP_MATRIX_HTTP_HEADERS_FILE",
        help="Owner-only JSON file of HTTP headers added to every Matrix request.",
    ),
    config_path: Path | None = typer.Option(  # noqa: B008
        None,
        "--config",
        "-c",
        help="MindRoom config path used for runtime env.",
    ),
    storage_path: Path | None = typer.Option(  # noqa: B008
        None,
        "--storage-path",
        "-s",
        help="Desktop bridge state directory.",
    ),
) -> None:
    """Run the outbound-only Matrix sync loop and execute locally authorized commands."""
    from mindroom.desktop.cloudflare_access import (  # noqa: PLC0415
        CloudflareAccessError,
        cloudflare_access_headers,
    )
    from mindroom.desktop.command_journal import DesktopCommandJournalError  # noqa: PLC0415
    from mindroom.desktop.provider import DesktopProviderError  # noqa: PLC0415
    from mindroom.desktop.session import (  # noqa: PLC0415
        DesktopSessionError,
        desktop_session_path,
        load_desktop_http_headers,
        load_desktop_session,
    )
    from mindroom.logging_config import setup_logging  # noqa: PLC0415
    from mindroom.matrix.olm_to_device import OlmToDeviceError  # noqa: PLC0415

    _validate_browser_options(
        enabled=browser_extension,
        executable_path=browser_executable,
        user_data_dir=browser_user_data_dir,
    )
    runtime_paths = _activate_desktop_runtime(config_path, storage_path=storage_path)
    setup_logging(level=log_level.upper(), runtime_paths=runtime_paths)
    try:
        http_headers: Mapping[str, str] | None = load_desktop_http_headers(matrix_http_headers_file)
        session = load_desktop_session(desktop_session_path(runtime_paths))
        if cloudflare_access or session.cloudflare_access:
            http_headers = cloudflare_access_headers(session.homeserver, http_headers)
        _ensure_desktop_dependencies(runtime_paths)
        asyncio.run(
            _run_bridge(
                runtime_paths=runtime_paths,
                session=session,
                controller_user_id=controller_user_id,
                controller_device_id=controller_device_id,
                controller_ed25519=controller_ed25519,
                allow_requester=frozenset(allow_requester),
                allow_agent=frozenset(allow_agent),
                allow_app=frozenset(allow_app),
                allow_control=allow_control,
                lease_minutes=lease_minutes,
                max_screenshot_width=max_screenshot_width,
                jpeg_quality=jpeg_quality,
                browser_extension=browser_extension,
                browser_executable=browser_executable,
                browser_user_data_dir=browser_user_data_dir,
                browser_timeout_seconds=browser_timeout_seconds,
                http_headers=http_headers,
            ),
        )
    except KeyboardInterrupt:
        _console.print("\n[yellow]Desktop bridge stopped.[/yellow]")
    except (
        CloudflareAccessError,
        DesktopCommandJournalError,
        DesktopProviderError,
        DesktopSessionError,
        OlmToDeviceError,
    ) as exc:
        _error_console.print(f"[red]Desktop bridge failed:[/red] {exc}")
        raise typer.Exit(1) from None


def _validate_browser_options(
    *,
    enabled: bool,
    executable_path: Path | None,
    user_data_dir: Path | None,
) -> None:
    """Reject browser-extension options that cannot describe a usable local profile."""
    if not enabled and (executable_path is not None or user_data_dir is not None):
        _error_console.print(
            "[red]Error:[/red] --browser-executable and --browser-user-data-dir require --browser-extension.",
        )
        raise typer.Exit(2)
    if executable_path is not None and not executable_path.expanduser().is_file():
        _error_console.print(f"[red]Error:[/red] Browser executable does not exist: {executable_path}")
        raise typer.Exit(2)
    if user_data_dir is not None and not user_data_dir.expanduser().is_dir():
        _error_console.print(f"[red]Error:[/red] Browser user-data directory does not exist: {user_data_dir}")
        raise typer.Exit(2)


async def _run_bridge(
    *,
    runtime_paths: RuntimePaths,
    session: DesktopMatrixSession,
    controller_user_id: str,
    controller_device_id: str,
    controller_ed25519: str,
    allow_requester: frozenset[str],
    allow_agent: frozenset[str],
    allow_app: frozenset[str],
    allow_control: bool,
    lease_minutes: int,
    max_screenshot_width: int,
    jpeg_quality: int,
    browser_extension: bool = False,
    browser_executable: Path | None = None,
    browser_user_data_dir: Path | None = None,
    browser_timeout_seconds: int = 90,
    http_headers: Mapping[str, str] | None = None,
) -> None:
    from mindroom.desktop.bridge import DesktopBridge, DesktopBridgePolicy  # noqa: PLC0415
    from mindroom.desktop.playwright_mcp import PlaywrightMCPBrowserProvider  # noqa: PLC0415
    from mindroom.desktop.provider import PyAutoGuiDesktopProvider  # noqa: PLC0415
    from mindroom.desktop.session import (  # noqa: PLC0415
        open_desktop_client,
        prepare_desktop_client,
    )
    from mindroom.matrix.olm_to_device import PinnedMatrixDevice, resolve_pinned_device  # noqa: PLC0415
    from mindroom.matrix.to_device import AuthenticatedToDeviceEvent  # noqa: PLC0415

    _request_required_desktop_permissions()
    controller = PinnedMatrixDevice(
        user_id=controller_user_id,
        device_id=controller_device_id,
        ed25519=controller_ed25519,
    )
    browser_provider = (
        PlaywrightMCPBrowserProvider(
            output_dir=runtime_paths.storage_root / "desktop-browser",
            executable_path=browser_executable,
            user_data_dir=browser_user_data_dir,
            call_timeout_seconds=browser_timeout_seconds,
            extension_token=runtime_paths.env_value("PLAYWRIGHT_MCP_EXTENSION_TOKEN"),
        )
        if browser_extension
        else None
    )
    client = await open_desktop_client(session, runtime_paths=runtime_paths, http_headers=http_headers)
    tasks: set[asyncio.Task[None]] = set()
    try:
        provider = PyAutoGuiDesktopProvider(
            allowed_app_ids=allow_app,
            max_screenshot_width=max_screenshot_width,
            jpeg_quality=jpeg_quality,
        )
        lease_expiry = round((time.time() + lease_minutes * 60) * 1000) if allow_control else None
        bridge = DesktopBridge(
            client=client,
            provider=provider,
            policy=DesktopBridgePolicy(
                controller=controller,
                allowed_requester_ids=allow_requester,
                allowed_agent_names=allow_agent,
                allowed_app_ids=allow_app,
                allow_control=allow_control,
                control_lease_expires_at_ms=lease_expiry,
                browser_enabled=browser_extension,
            ),
            browser_provider=browser_provider,
            journal_path=runtime_paths.storage_root / "desktop_bridge" / "command_journal.json",
        )

        def schedule_event(event: nio.ToDeviceEvent) -> None:
            if not isinstance(event, AuthenticatedToDeviceEvent):
                return
            task = asyncio.create_task(bridge.on_to_device_event(event), name="desktop_command")
            tasks.add(task)
            task.add_done_callback(command_done)

        def command_done(task: asyncio.Task[None]) -> None:
            tasks.discard(task)
            if task.cancelled():
                return
            error = task.exception()
            if error is not None:
                _error_console.print(f"[red]Desktop command task failed:[/red] {error}")

        client.add_to_device_callback(schedule_event, AuthenticatedToDeviceEvent)
        await resolve_pinned_device(client, controller)
        await prepare_desktop_client(client)

        mode = f"control enabled for {lease_minutes} minute(s)" if allow_control else "observe-only"
        _console.print(f"[green]Desktop bridge online:[/green] {mode}")
        _console.print(f"Allowed requesters: {', '.join(sorted(allow_requester))}")
        _console.print(f"Allowed agents: {', '.join(sorted(allow_agent))}")
        _console.print(f"Allowed applications: {', '.join(sorted(allow_app))}")
        if browser_extension:
            _console.print("Playwright browser extension: enabled for the active installed browser profile")
        _console.print("Move the pointer to the upper-left corner to trigger PyAutoGUI's emergency stop.")
        await _sync_desktop_client(client)
    finally:
        client.stop_sync_forever()
        for task in tasks:
            task.cancel()
        if tasks:
            await asyncio.gather(*tasks, return_exceptions=True)
        try:
            if browser_provider is not None:
                await browser_provider.close()
        finally:
            await client.close()


async def _sync_desktop_client(client: nio.AsyncClient) -> None:
    """Run desktop sync and surface permanent Matrix authentication failures."""
    import nio  # noqa: PLC0415

    from mindroom.desktop.session import DesktopSessionError  # noqa: PLC0415

    permanent_sync_error: nio.SyncError | None = None

    async def stop_on_permanent_sync_error(response: nio.SyncError) -> None:
        nonlocal permanent_sync_error
        if response.status_code not in {"M_FORBIDDEN", "M_UNKNOWN_TOKEN", "M_USER_DEACTIVATED"}:
            return
        permanent_sync_error = response
        client.stop_sync_forever()

    client.add_response_callback(stop_on_permanent_sync_error, nio.SyncError)  # ty: ignore[invalid-argument-type]
    await client.sync_forever(timeout=30_000, full_state=False, set_presence="online")
    if permanent_sync_error is not None:
        msg = f"Desktop Matrix sync stopped after permanent authentication failure: {permanent_sync_error}"
        raise DesktopSessionError(msg)


__all__ = ["desktop_app", "desktop_login", "desktop_pair", "desktop_run", "desktop_setup"]
