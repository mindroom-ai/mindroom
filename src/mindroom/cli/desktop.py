"""CLI for the lightweight Matrix-attached desktop bridge."""

# NativeConfigError takes a stable wire code before its user-facing message.
# ruff: noqa: EM101

from __future__ import annotations

import asyncio
import json
import os
import sys
import time
from dataclasses import replace
from pathlib import Path
from typing import TYPE_CHECKING

import typer
from rich.console import Console

from mindroom.desktop.command_journal import DesktopCommandJournalError, check_controller_binding
from mindroom.desktop.login_method import DesktopLoginMethod

if TYPE_CHECKING:
    from collections.abc import Mapping

    from mindroom.constants import RuntimePaths
    from mindroom.desktop.native_config import NativeDesktopConfig
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


@desktop_app.command("app")
def desktop_native_app(
    config_path: Path | None = typer.Option(  # noqa: B008
        None,
        "--config",
        help="Path to local MindRoom configuration.",
    ),
    storage_path: Path | None = typer.Option(  # noqa: B008
        None,
        "--storage-path",
        help="Path to local MindRoom storage.",
    ),
) -> None:
    """Run the native app's private structured helper over inherited standard I/O."""
    from mindroom import __version__  # noqa: PLC0415
    from mindroom.desktop.native_host import run_native_stdio  # noqa: PLC0415

    runtime_paths = _activate_desktop_runtime(config_path, storage_path=storage_path)
    asyncio.run(run_native_stdio(runtime_paths, helper_version=__version__))


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
        f"macOS has not applied {permission_names} {permission_label} to the terminal app running this command. "
        "macOS applies a grant only after that app restarts, even if it is already listed and enabled in "
        "System Settings > Privacy & Security. Enable it there if needed, quit the terminal app completely "
        "(Cmd-Q; closing its windows is not enough), reopen it, then run `mindroom desktop run` again."
    )
    if os.environ.get("TMUX"):
        msg += (
            " This command runs inside tmux, whose existing server does not pick up the new grant: after reopening "
            "the terminal app, also run `tmux kill-server`, or start the bridge outside tmux."
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

    owner, session = await login_desktop_client(
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
        fingerprint = client_ed25519_fingerprint(owner.client)
        _print_device_identity(session, fingerprint=fingerprint, session_path=session_path)
    finally:
        await owner.close()


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
    allow_agent: list[str] | None = typer.Option(  # noqa: B008
        None,
        "--allow-agent",
        help="Agent name from the setup message; prompts if omitted. Repeat as needed.",
    ),
    allow_app: list[str] | None = typer.Option(  # noqa: B008
        None,
        "--allow-app",
        help="Save allowed app IDs, or choose apps later in the macOS app.",
    ),
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
    """Pair and save the connection shared with the macOS app."""
    from mindroom.constants import runtime_matrix_homeserver  # noqa: PLC0415

    # Native configuration uses Unix file locks; CLI help must remain portable.
    from mindroom.desktop.native_config import (  # noqa: PLC0415
        NativeBrowserConfig,
        NativeCaptureConfig,
        NativeConfigError,
        NativeDesktopConfig,
        load_native_config,
        native_config_path,
        save_native_config,
    )
    from mindroom.desktop.session import (  # noqa: PLC0415
        DesktopSessionError,
        desktop_session_path,
        load_desktop_session,
        save_desktop_session,
    )
    from mindroom.matrix.olm_to_device import PinnedMatrixDevice  # noqa: PLC0415

    if allow_agent is None:
        allow_agent = [typer.prompt("Agent name from the setup message").strip()]
    runtime_paths = _activate_desktop_runtime(config_path, storage_path=storage_path)
    session_path = desktop_session_path(runtime_paths)
    if session_path.exists():
        _require_saved_session_matches(
            session_path,
            user_id=user_id,
            homeserver=homeserver or runtime_matrix_homeserver(runtime_paths),
        )
    else:
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
    try:
        path = native_config_path(runtime_paths.storage_root)
        try:
            previous = load_native_config(path)
        except NativeConfigError as exc:
            if exc.code != "configuration_missing":
                raise
            previous = None
        controller = PinnedMatrixDevice(controller_user_id, controller_device_id, controller_ed25519)
        check_controller_binding(
            runtime_paths.storage_root / "desktop_bridge" / "commands.sqlite3",
            json.dumps([controller.user_id, controller.device_id, controller.ed25519]),
        )
        session = load_desktop_session(session_path)
        matching = previous if previous is not None and previous.controller == controller else None
        config = NativeDesktopConfig(
            revision=previous.revision if previous else 0,
            enabled=True,
            controller=controller,
            allowed_requester_ids=(session.user_id,),
            allowed_agent_names=tuple(allow_agent),
            allowed_app_ids=tuple(allow_app)
            if allow_app is not None
            else (matching.allowed_app_ids if matching else ()),
            capture=matching.capture if matching else NativeCaptureConfig(),
            browser=matching.browser if matching else NativeBrowserConfig(),
        )
        config = NativeDesktopConfig.from_payload(config.to_payload(), validate_browser_paths=False)
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
        save_desktop_session(
            session_path,
            replace(session, cloudflare_access=cloudflare_access or session.cloudflare_access),
            expected_session=session,
        )
        save_native_config(path, config, expected_revision=config.revision)
    except (DesktopCommandJournalError, DesktopSessionError, ValueError) as exc:
        _error_console.print(f"[red]Desktop setup failed:[/red] {exc}")
        raise typer.Exit(1) from None
    _console.print("Setup saved for both the terminal and MindRoom app.")
    if not config.allowed_app_ids:
        _console.print("In MindRoom, open Computer access, choose apps, and save app access.")
    _console.print("After confirming in chat, start observation in the app or run `mindroom desktop run`.")


def _require_saved_session_matches(session_path: Path, *, user_id: str | None, homeserver: str) -> None:
    """Refuse to pair a saved session that belongs to another homeserver or user."""
    from mindroom.desktop.session import DesktopSessionError, load_desktop_session  # noqa: PLC0415

    try:
        session = load_desktop_session(session_path)
    except DesktopSessionError as exc:
        _error_console.print(f"[red]Desktop setup failed:[/red] {exc}")
        raise typer.Exit(1) from None
    homeserver_differs = homeserver.rstrip("/") != session.homeserver.rstrip("/")
    user_differs = user_id is not None and user_id != session.user_id
    if homeserver_differs or user_differs:
        _error_console.print(
            f"[red]Desktop setup failed:[/red] The saved session at {session_path} belongs to "
            f"{session.user_id} on {session.homeserver}, not {user_id or session.user_id} on "
            f"{homeserver}. Pass --storage-path for a separate setup, or run "
            "'mindroom desktop login --replace' to replace the saved session.",
        )
        raise typer.Exit(1)


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
    owner = await open_desktop_client(session, runtime_paths=runtime_paths, http_headers=http_headers)
    try:
        return await send_desktop_pairing_claim(
            owner,
            controller,
            code=code,
        )
    finally:
        await owner.close()


@desktop_app.command("run")
def desktop_run(
    controller_user_id: str | None = typer.Option(
        None,
        "--controller-user-id",
        help="Pinned cloud controller Matrix user.",
    ),
    controller_device_id: str | None = typer.Option(
        None,
        "--controller-device-id",
        help="Pinned cloud controller device.",
    ),
    controller_ed25519: str | None = typer.Option(None, "--controller-ed25519", help="Pinned controller fingerprint."),
    allow_requester: list[str] | None = typer.Option(  # noqa: B008
        None,
        "--allow-requester",
        help="Human Matrix requester allowed to operate this desktop; repeat as needed.",
    ),
    allow_agent: list[str] | None = typer.Option(  # noqa: B008
        None,
        "--allow-agent",
        help="MindRoom agent name allowed to operate this desktop; repeat as needed.",
    ),
    allow_app: list[str] | None = typer.Option(  # noqa: B008
        None,
        "--allow-app",
        help="Exact local application ID exposed to the agent; repeat as needed.",
    ),
    allow_control: bool = typer.Option(
        False,
        "--allow-control",
        help="Enable semantic and fallback input for a short local lease. Default is observe-only.",
    ),
    lease_minutes: int = typer.Option(15, "--lease-minutes", min=1, max=60, help="Local control lease duration."),
    max_screenshot_width: int | None = typer.Option(None, "--max-screenshot-width", min=320, max=3840),
    jpeg_quality: int | None = typer.Option(None, "--jpeg-quality", min=40, max=95),
    browser_extension: bool | None = typer.Option(
        None,
        "--browser-extension/--no-browser-extension",
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
    browser_timeout_seconds: int | None = typer.Option(
        None,
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
    """Observe using saved app/terminal setup; flags override this run only."""
    from mindroom.desktop.cloudflare_access import (  # noqa: PLC0415
        CloudflareAccessError,
        cloudflare_access_headers,
    )
    from mindroom.desktop.provider import DesktopProviderError  # noqa: PLC0415
    from mindroom.desktop.session import (  # noqa: PLC0415
        DesktopSessionError,
        desktop_session_path,
        load_desktop_http_headers,
        load_desktop_session,
    )
    from mindroom.logging_config import setup_logging  # noqa: PLC0415
    from mindroom.matrix.olm_to_device import OlmToDeviceError  # noqa: PLC0415

    runtime_paths = _activate_desktop_runtime(config_path, storage_path=storage_path)
    setup_logging(level=log_level.upper(), runtime_paths=runtime_paths)
    try:
        config = _resolve_run_config(
            runtime_paths.storage_root,
            controller_fields=(controller_user_id, controller_device_id, controller_ed25519),
            allow_requester=allow_requester,
            allow_agent=allow_agent,
            allow_app=allow_app,
            max_screenshot_width=max_screenshot_width,
            jpeg_quality=jpeg_quality,
            browser_extension=browser_extension,
            browser_executable=browser_executable,
            browser_user_data_dir=browser_user_data_dir,
            browser_timeout_seconds=browser_timeout_seconds,
        )
        _validate_browser_options(
            enabled=config.browser.enabled,
            executable_path=config.browser.executable_path if config.browser.enabled else browser_executable,
            user_data_dir=config.browser.user_data_dir if config.browser.enabled else browser_user_data_dir,
        )
        http_headers: Mapping[str, str] | None = load_desktop_http_headers(matrix_http_headers_file)
        session = load_desktop_session(desktop_session_path(runtime_paths))
        if cloudflare_access or session.cloudflare_access:
            http_headers = cloudflare_access_headers(session.homeserver, http_headers)
        _ensure_desktop_dependencies(runtime_paths)
        asyncio.run(
            _run_bridge(
                runtime_paths=runtime_paths,
                session=session,
                controller_user_id=config.controller.user_id,
                controller_device_id=config.controller.device_id,
                controller_ed25519=config.controller.ed25519,
                allow_requester=frozenset(config.allowed_requester_ids),
                allow_agent=frozenset(config.allowed_agent_names),
                allow_app=frozenset(config.allowed_app_ids),
                allow_control=allow_control,
                lease_minutes=lease_minutes,
                max_screenshot_width=config.capture.max_screenshot_width,
                jpeg_quality=config.capture.jpeg_quality,
                browser_extension=config.browser.enabled,
                browser_executable=config.browser.executable_path,
                browser_user_data_dir=config.browser.user_data_dir,
                browser_timeout_seconds=config.browser.timeout_seconds,
                http_headers=http_headers,
            ),
        )
    except KeyboardInterrupt:
        _console.print("\n[yellow]Desktop bridge stopped.[/yellow]")
    except (
        CloudflareAccessError,
        DesktopCommandJournalError,
        DesktopProviderError,
        ValueError,
        DesktopSessionError,
        OlmToDeviceError,
    ) as exc:
        _error_console.print(f"[red]Desktop bridge failed:[/red] {exc}")
        raise typer.Exit(1) from None


def _resolve_run_config(
    storage_root: Path,
    *,
    controller_fields: tuple[str | None, str | None, str | None],
    allow_requester: list[str] | None,
    allow_agent: list[str] | None,
    allow_app: list[str] | None,
    max_screenshot_width: int | None,
    jpeg_quality: int | None,
    browser_extension: bool | None,
    browser_executable: Path | None,
    browser_user_data_dir: Path | None,
    browser_timeout_seconds: int | None,
) -> NativeDesktopConfig:
    """Resolve one run without combining a new controller with saved authority."""
    # Native configuration uses Unix file locks; CLI help must remain portable.
    from mindroom.desktop.native_config import (  # noqa: PLC0415
        NativeBrowserConfig,
        NativeCaptureConfig,
        NativeConfigError,
        NativeDesktopConfig,
        load_native_config,
        native_config_path,
    )
    from mindroom.matrix.olm_to_device import PinnedMatrixDevice  # noqa: PLC0415

    try:
        config = load_native_config(native_config_path(storage_root))
    except NativeConfigError as exc:
        if exc.code != "configuration_missing":
            raise
        config = None
    if any(field is not None for field in controller_fields):
        user_id, device_id, fingerprint = controller_fields
        if user_id is None or device_id is None or fingerprint is None:
            msg = "Provide all three --controller options together, or omit them to use saved setup."
            raise NativeConfigError("invalid_request", msg)
        controller = PinnedMatrixDevice(user_id, device_id, fingerprint)
        if config is None or config.controller != controller:
            if not allow_requester or not allow_agent or not allow_app:
                msg = "A new controller requires --allow-requester, --allow-agent, and --allow-app."
                raise NativeConfigError("invalid_request", msg)
            config = NativeDesktopConfig(
                revision=0,
                enabled=True,
                controller=controller,
                allowed_requester_ids=tuple(allow_requester),
                allowed_agent_names=tuple(allow_agent),
                allowed_app_ids=tuple(allow_app),
                capture=NativeCaptureConfig(),
                browser=NativeBrowserConfig(),
            )
    if config is None:
        msg = "Complete setup in the MindRoom app or run the `mindroom desktop setup` command from your agent chat."
        raise NativeConfigError("configuration_missing", msg)
    if not config.enabled:
        msg = "Desktop access is disabled in the saved setup. Enable it in the MindRoom app before starting."
        raise NativeConfigError("configuration_missing", msg)
    config = replace(
        config,
        allowed_requester_ids=tuple(allow_requester) if allow_requester is not None else config.allowed_requester_ids,
        allowed_agent_names=tuple(allow_agent) if allow_agent is not None else config.allowed_agent_names,
        allowed_app_ids=tuple(allow_app) if allow_app is not None else config.allowed_app_ids,
        capture=replace(
            config.capture,
            max_screenshot_width=max_screenshot_width
            if max_screenshot_width is not None
            else config.capture.max_screenshot_width,
            jpeg_quality=jpeg_quality if jpeg_quality is not None else config.capture.jpeg_quality,
        ),
        browser=replace(
            config.browser,
            enabled=browser_extension if browser_extension is not None else config.browser.enabled,
            executable_path=browser_executable.expanduser().resolve()
            if browser_executable is not None
            else config.browser.executable_path,
            user_data_dir=browser_user_data_dir.expanduser().resolve()
            if browser_user_data_dir is not None
            else config.browser.user_data_dir,
            timeout_seconds=browser_timeout_seconds
            if browser_timeout_seconds is not None
            else config.browser.timeout_seconds,
        ),
    )
    if not config.allowed_app_ids:
        msg = "Choose and save apps in MindRoom > Computer access, or pass --allow-app APPLICATION_ID for this run."
        raise NativeConfigError("configuration_missing", msg)
    return NativeDesktopConfig.from_payload(config.to_payload(), validate_browser_paths=False)


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


async def _run_bridge(  # noqa: PLR0915
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
    from nio import AuthenticatedToDeviceEvent  # noqa: PLC0415

    from mindroom.desktop.bridge import DesktopBridge, DesktopBridgePolicy  # noqa: PLC0415
    from mindroom.desktop.playwright_mcp import PlaywrightMCPBrowserProvider  # noqa: PLC0415
    from mindroom.desktop.provider import PyAutoGuiDesktopProvider  # noqa: PLC0415
    from mindroom.desktop.session import (  # noqa: PLC0415
        open_desktop_client,
        prepare_desktop_client,
    )
    from mindroom.desktop.transport import DesktopTransport  # noqa: PLC0415
    from mindroom.matrix.olm_to_device import PinnedMatrixDevice, resolve_pinned_device  # noqa: PLC0415

    _request_required_desktop_permissions()
    controller = PinnedMatrixDevice(
        user_id=controller_user_id,
        device_id=controller_device_id,
        ed25519=controller_ed25519,
    )
    browser_provider = None
    owner = None
    bridge = None
    registration = None
    tasks: set[asyncio.Task[None]] = set()
    try:
        if browser_extension:
            browser_provider = PlaywrightMCPBrowserProvider(
                output_dir=runtime_paths.storage_root / "desktop-browser",
                executable_path=browser_executable,
                user_data_dir=browser_user_data_dir,
                call_timeout_seconds=browser_timeout_seconds,
                extension_token=runtime_paths.env_value("PLAYWRIGHT_MCP_EXTENSION_TOKEN"),
            )
        owner = await open_desktop_client(session, runtime_paths=runtime_paths, http_headers=http_headers)
        client = owner.client
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
            journal_path=runtime_paths.storage_root / "desktop_bridge" / "commands.sqlite3",
            legacy_journal_path=runtime_paths.storage_root / "desktop_bridge" / "command_journal.json",
        )
        client.add_to_device_callback(bridge.on_to_device_event, AuthenticatedToDeviceEvent)
        registration = client.to_device_callbacks[-1]
        await resolve_pinned_device(client, controller)
        await prepare_desktop_client(client)

        _announce_bridge(
            allow_control=allow_control,
            lease_minutes=lease_minutes,
            allow_requester=allow_requester,
            allow_agent=allow_agent,
            allow_app=allow_app,
            browser_extension=browser_extension,
        )
        transport = DesktopTransport(owner.source, wait_for_capacity=bridge.wait_for_capacity)
        tasks.update(
            (
                asyncio.create_task(bridge.run(), name="desktop_workers"),
                asyncio.create_task(transport.run(), name="desktop_transport"),
            ),
        )
        done, _ = await asyncio.wait(tasks, return_when=asyncio.FIRST_COMPLETED)
        for task in done:
            await task
    finally:
        # Native input runs in threads; cancelling its worker cannot stop the input.
        # Keep the journal and device lease until the active action has drained.
        if bridge is not None:
            await bridge.stop()
        for task in tasks:
            task.cancel()
        await asyncio.gather(*tasks, return_exceptions=True)
        if registration is not None and owner is not None:
            owner.client.to_device_callbacks.remove(registration)
        try:
            if bridge is not None:
                bridge.close()
        finally:
            try:
                if browser_provider is not None:
                    await browser_provider.close()
            finally:
                if owner is not None:
                    await owner.close()


def _announce_bridge(
    *,
    allow_control: bool,
    lease_minutes: int,
    allow_requester: frozenset[str],
    allow_agent: frozenset[str],
    allow_app: frozenset[str],
    browser_extension: bool,
) -> None:
    """Show the locally granted authority after the owned session is ready."""
    mode = f"control enabled for {lease_minutes} minute(s)" if allow_control else "observe-only"
    _console.print(f"[green]Desktop bridge online:[/green] {mode}")
    _console.print(f"Allowed requesters: {', '.join(sorted(allow_requester))}")
    _console.print(f"Allowed agents: {', '.join(sorted(allow_agent))}")
    _console.print(f"Allowed applications: {', '.join(sorted(allow_app))}")
    if browser_extension:
        _console.print("Playwright browser extension: enabled for the active installed browser profile")
    _console.print("Move the pointer to the upper-left corner to trigger PyAutoGUI's emergency stop.")


__all__ = ["desktop_app", "desktop_login", "desktop_native_app", "desktop_pair", "desktop_run", "desktop_setup"]
