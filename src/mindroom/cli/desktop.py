"""CLI for the lightweight Matrix-attached desktop bridge."""

from __future__ import annotations

import asyncio
import os
import time
from pathlib import Path  # noqa: TC003 - Typer evaluates command annotations at runtime.
from typing import TYPE_CHECKING

import typer
from rich.console import Console

if TYPE_CHECKING:
    import nio

    from mindroom.constants import RuntimePaths
    from mindroom.desktop.session import DesktopMatrixSession

_console = Console()
_error_console = Console(stderr=True)

desktop_app = typer.Typer(
    name="desktop",
    help="Connect allowlisted local applications to cloud MindRoom over Matrix E2EE.",
    no_args_is_help=True,
)


@desktop_app.command("controller")
def desktop_controller(
    entity: str = typer.Option(..., "--entity", help="Cloud agent whose Matrix device will send commands."),
    config_path: Path | None = typer.Option(  # noqa: B008
        None,
        "--config",
        "-c",
        help="Cloud MindRoom config path.",
    ),
    storage_path: Path | None = typer.Option(  # noqa: B008
        None,
        "--storage-path",
        "-s",
        help="Cloud MindRoom state directory.",
    ),
) -> None:
    """Print the cloud controller identity that the local bridge must pin."""
    from mindroom.cli.config import activate_cli_runtime  # noqa: PLC0415
    from mindroom.desktop.identity import controller_identity_for_entity  # noqa: PLC0415

    runtime_paths = activate_cli_runtime(config_path, storage_path=storage_path)
    try:
        identity = controller_identity_for_entity(entity, runtime_paths=runtime_paths)
    except Exception as exc:
        _error_console.print(f"[red]Controller identity lookup failed:[/red] {exc}")
        raise typer.Exit(1) from None
    _console.print("[green]Cloud Matrix controller:[/green]")
    _console.print(f"  Entity: {identity.entity_name}")
    _console.print(f"  User: {identity.user_id}")
    _console.print(f"  Device: {identity.device_id}")
    _console.print(f"  Ed25519: {identity.ed25519}")
    _console.print("\nPass these exact values to 'mindroom desktop run' on the local computer.")


@desktop_app.command("login")
def desktop_login(
    user_id: str = typer.Option(..., "--user-id", help="Dedicated Matrix user ID for this desktop device."),
    homeserver: str | None = typer.Option(
        None,
        "--homeserver",
        help="Matrix homeserver URL; defaults to the configured MindRoom homeserver.",
    ),
    replace: bool = typer.Option(False, "--replace", help="Replace the saved session with a fresh Matrix device."),
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
    from mindroom.cli.config import activate_cli_runtime  # noqa: PLC0415
    from mindroom.constants import runtime_matrix_homeserver  # noqa: PLC0415
    from mindroom.desktop.session import desktop_session_path  # noqa: PLC0415

    runtime_paths = activate_cli_runtime(config_path, storage_path=storage_path)
    session_path = desktop_session_path(runtime_paths)
    if session_path.exists() and not replace:
        _error_console.print(f"[red]Error:[/red] Session already exists at {session_path}. Use --replace explicitly.")
        raise typer.Exit(1)
    password = os.environ.get("MINDROOM_DESKTOP_MATRIX_PASSWORD")
    if password is None:
        password = typer.prompt("Matrix password", hide_input=True, confirmation_prompt=False)
    try:
        asyncio.run(
            _login_and_save(
                runtime_paths=runtime_paths,
                homeserver=homeserver or runtime_matrix_homeserver(runtime_paths),
                user_id=user_id,
                password=password,
                session_path=session_path,
            ),
        )
    except Exception as exc:
        _error_console.print(f"[red]Desktop login failed:[/red] {exc}")
        raise typer.Exit(1) from None


async def _login_and_save(
    *,
    runtime_paths: RuntimePaths,
    homeserver: str,
    user_id: str,
    password: str,
    session_path: Path,
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
        runtime_paths=runtime_paths,
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
    _console.print("\nPin these exact values in the cloud agent's desktop tool configuration.")


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
    log_level: str = typer.Option("INFO", "--log-level", "-l"),
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
    from mindroom.cli.config import activate_cli_runtime  # noqa: PLC0415
    from mindroom.desktop.session import desktop_session_path, load_desktop_session  # noqa: PLC0415
    from mindroom.logging_config import setup_logging  # noqa: PLC0415

    runtime_paths = activate_cli_runtime(config_path, storage_path=storage_path)
    setup_logging(level=log_level.upper(), runtime_paths=runtime_paths)
    try:
        session = load_desktop_session(desktop_session_path(runtime_paths))
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
            ),
        )
    except KeyboardInterrupt:
        _console.print("\n[yellow]Desktop bridge stopped.[/yellow]")
    except Exception as exc:
        _error_console.print(f"[red]Desktop bridge failed:[/red] {exc}")
        raise typer.Exit(1) from None


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
) -> None:
    from mindroom.desktop.bridge import DesktopBridge, DesktopBridgePolicy  # noqa: PLC0415
    from mindroom.desktop.provider import PyAutoGuiDesktopProvider  # noqa: PLC0415
    from mindroom.desktop.session import (  # noqa: PLC0415
        open_desktop_client,
        prepare_desktop_client,
    )
    from mindroom.matrix.olm_to_device import PinnedMatrixDevice, resolve_pinned_device  # noqa: PLC0415
    from mindroom.matrix.to_device import AuthenticatedToDeviceEvent  # noqa: PLC0415

    controller = PinnedMatrixDevice(
        user_id=controller_user_id,
        device_id=controller_device_id,
        ed25519=controller_ed25519,
    )
    client = await open_desktop_client(session, runtime_paths=runtime_paths)
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
            ),
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
        await prepare_desktop_client(client)
        await resolve_pinned_device(client, controller)

        mode = f"control enabled for {lease_minutes} minute(s)" if allow_control else "observe-only"
        _console.print(f"[green]Desktop bridge online:[/green] {mode}")
        _console.print(f"Allowed requesters: {', '.join(sorted(allow_requester))}")
        _console.print(f"Allowed agents: {', '.join(sorted(allow_agent))}")
        _console.print(f"Allowed applications: {', '.join(sorted(allow_app))}")
        _console.print("Move the pointer to the upper-left corner to trigger PyAutoGUI's emergency stop.")
        await _sync_desktop_client(client)
    finally:
        client.stop_sync_forever()
        for task in tasks:
            task.cancel()
        if tasks:
            await asyncio.gather(*tasks, return_exceptions=True)
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


__all__ = ["desktop_app", "desktop_controller", "desktop_login", "desktop_run"]
