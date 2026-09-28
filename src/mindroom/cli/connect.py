"""Helpers for CLI `connect` command and local onboarding config updates."""

from __future__ import annotations

import hashlib
import io
import re
import socket
import time
import webbrowser
from dataclasses import dataclass
from typing import TYPE_CHECKING, Literal

import yaml
from rich.markup import escape

from mindroom import constants
from mindroom.cli.owner import parse_owner_matrix_user_id, replace_owner_placeholders_in_text
from mindroom.config.yaml_includes import load_yaml_config_source
from mindroom.constants import OWNER_MATRIX_USER_ID_ENV
from mindroom.http_error_detail import error_detail_from_response

from .env_file import env_path_for_config, upsert_env_values

if TYPE_CHECKING:
    from collections.abc import Callable, Mapping
    from pathlib import Path

    import httpx
    from rich.console import Console

    from mindroom.constants import RuntimePaths

__all__ = [
    "DevicePairSession",
    "PairCompleteResult",
    "local_client_fingerprint",
    "pair_local_install",
    "persist_local_provisioning_env",
    "render_qr",
    "replace_owner_placeholders_in_config",
    "run_device_pairing",
]

_API_PATH = "/v1/local-mindroom/pair/device"
_NAMESPACE_RE = re.compile(r"^[a-z0-9]{4,32}$")
_DEFAULT_POLL_INTERVAL_SECONDS = 3
_MAX_START_RETRY_SECONDS = 30


@dataclass(frozen=True)
class PairCompleteResult:
    """Credentials returned by the provisioning pair-complete endpoint."""

    client_id: str
    client_secret: str
    namespace: str
    owner_user_id: str | None = None
    namespace_invalid: bool = False
    owner_user_id_invalid: bool = False


@dataclass(frozen=True)
class DevicePairSession:
    """A pending device pairing the user approves in MindRoom Chat."""

    pair_code: str
    device_secret: str
    approve_url: str
    poll_interval_seconds: int


class _ServiceError(ValueError):
    """A provisioning request failed; status code 0 means the service was unreachable."""

    def __init__(self, status_code: int, detail: str) -> None:
        if status_code == 0:
            message = f"Could not reach provisioning service: {detail}"
        else:
            message = f"Pairing failed ({status_code}): {detail}"
        super().__init__(message)
        self.status_code = status_code

    @property
    def transient(self) -> bool:
        """Whether retrying later may succeed: unreachable, rate limited, or a server error."""
        return self.status_code in {0, 429} or self.status_code >= 500


def _httpx_post(url: str, *, json: Mapping[str, object], timeout: float, verify: bool) -> httpx.Response:
    """Call httpx.post without importing httpx during CLI help rendering."""
    import httpx  # noqa: PLC0415

    return httpx.post(url, json=json, timeout=timeout, verify=verify)


def _post_json(
    post_request: Callable[..., httpx.Response],
    url: str,
    payload: Mapping[str, object],
    *,
    verify: bool,
) -> dict[str, object]:
    """POST to the provisioning service and return its JSON object, raising _ServiceError for failed requests."""
    import httpx  # noqa: PLC0415

    try:
        response = post_request(url, json=payload, timeout=10, verify=verify)
    except httpx.HTTPError as exc:
        raise _ServiceError(0, str(exc)) from exc
    if not response.is_success:
        raise _ServiceError(response.status_code, error_detail_from_response(response))
    try:
        data = response.json()
    except ValueError as exc:
        msg = "Provisioning service returned invalid JSON."
        raise ValueError(msg) from exc
    if not isinstance(data, dict):
        msg = "Provisioning service returned unexpected response."
        raise TypeError(msg)
    return data


def _parse_pair_complete(data: dict[str, object]) -> PairCompleteResult:
    """Read credentials from a connected poll response."""
    raw_owner_user_id = data.get("owner_user_id")
    parsed_owner_user_id = parse_owner_matrix_user_id(raw_owner_user_id)
    owner_user_id_invalid = (
        isinstance(raw_owner_user_id, str) and bool(raw_owner_user_id.strip()) and parsed_owner_user_id is None
    )
    client_id = _required_non_empty_string(data, "client_id")
    raw_namespace = data.get("namespace")
    parsed_namespace = _parse_namespace(raw_namespace)
    namespace_invalid = isinstance(raw_namespace, str) and bool(raw_namespace.strip()) and parsed_namespace is None
    if parsed_namespace is None:
        parsed_namespace = ""

    return PairCompleteResult(
        client_id=client_id,
        client_secret=_required_non_empty_string(data, "client_secret"),
        namespace=parsed_namespace,
        owner_user_id=parsed_owner_user_id,
        namespace_invalid=namespace_invalid,
        owner_user_id_invalid=owner_user_id_invalid,
    )


def _validate_poll_interval(raw_value: object) -> int:
    """Return a positive poll interval or the default as fallback."""
    if isinstance(raw_value, bool) or not isinstance(raw_value, int):
        return _DEFAULT_POLL_INTERVAL_SECONDS
    if raw_value <= 0:
        return _DEFAULT_POLL_INTERVAL_SECONDS
    return raw_value


def _start_session(
    post_request: Callable[..., httpx.Response],
    base_url: str,
    *,
    client_name: str,
    client_fingerprint: str,
    verify: bool,
) -> DevicePairSession:
    """Start a device pairing session with the provisioning service."""
    started = _post_json(
        post_request,
        f"{base_url}/start",
        {"client_name": client_name.strip(), "client_pubkey_or_fingerprint": client_fingerprint},
        verify=verify,
    )
    return DevicePairSession(
        pair_code=_required_non_empty_string(started, "pair_code"),
        device_secret=_required_non_empty_string(started, "device_secret"),
        approve_url=_required_non_empty_string(started, "approve_url"),
        poll_interval_seconds=_validate_poll_interval(started.get("poll_interval_seconds")),
    )


def _start_session_with_retry(
    post_request: Callable[..., httpx.Response],
    base_url: str,
    *,
    client_name: str,
    client_fingerprint: str,
    verify: bool,
    sleep: Callable[[float], None],
    report_retry: Callable[[str], None] | None,
    stopped: Callable[[], bool],
) -> DevicePairSession | None:
    """Start a session, retrying transient failures with doubling backoff so unattended services survive boot.

    Returns None when stopped() reports that pairing is no longer needed between retries.
    """
    delay = _DEFAULT_POLL_INTERVAL_SECONDS
    while True:
        try:
            return _start_session(
                post_request,
                base_url,
                client_name=client_name,
                client_fingerprint=client_fingerprint,
                verify=verify,
            )
        except _ServiceError as exc:
            if not exc.transient:
                raise
            if report_retry is not None:
                report_retry(f"{exc} (retrying in {delay}s)")
        sleep(delay)
        if stopped():
            return None
        delay = min(delay * 2, _MAX_START_RETRY_SECONDS)


def _wait_for_approval(
    post_request: Callable[..., httpx.Response],
    base_url: str,
    session: DevicePairSession,
    *,
    verify: bool,
    sleep: Callable[[float], None],
    stopped: Callable[[], bool],
) -> PairCompleteResult | Literal["expired", "stopped"]:
    """Poll until the session is approved, has expired, or stopped() reports that waiting is pointless."""
    while True:
        sleep(session.poll_interval_seconds)
        if stopped():
            return "stopped"
        try:
            polled = _post_json(
                post_request,
                f"{base_url}/poll",
                {"device_secret": session.device_secret},
                verify=verify,
            )
        except _ServiceError as exc:
            # The service prunes expired sessions, so an unknown device secret means expired.
            if exc.status_code == 404:
                return "expired"
            if exc.transient:
                continue
            raise

        status = polled.get("status")
        if status == "connected":
            return _parse_pair_complete(polled)
        if status == "expired":
            return "expired"
        if status == "pending":
            continue
        if status is None:
            msg = "Poll response missing status."
            raise ValueError(msg)
        msg = f"Unexpected poll status: {status}"
        raise ValueError(msg)


def run_device_pairing(
    *,
    provisioning_url: str,
    client_name: str,
    client_fingerprint: str,
    matrix_ssl_verify: bool,
    announce: Callable[[DevicePairSession], None],
    post_request: Callable[..., httpx.Response] | None = None,
    sleep: Callable[[float], None] = time.sleep,
    renew_expired: bool = True,
    stop_waiting: Callable[[], bool] | None = None,
    report_retry: Callable[[str], None] | None = None,
) -> PairCompleteResult | None:
    """Wait until a signed-in user approves this machine.

    When renew_expired is True, expired sessions start a new code and announce it again, and transient failures to start a session are retried with backoff.
    When False, expiry and start failures raise ValueError (prevents indefinite waiting in interactive flows).
    Returns None when stop_waiting, checked before each session start, start retry, and poll, reports that pairing is no longer needed.
    """
    post = post_request or _httpx_post
    base_url = f"{provisioning_url.rstrip('/')}{_API_PATH}"

    def stopped() -> bool:
        return stop_waiting is not None and stop_waiting()

    while True:
        if stopped():
            return None
        if renew_expired:
            session = _start_session_with_retry(
                post,
                base_url,
                client_name=client_name,
                client_fingerprint=client_fingerprint,
                verify=matrix_ssl_verify,
                sleep=sleep,
                report_retry=report_retry,
                stopped=stopped,
            )
            if session is None:
                return None
        else:
            session = _start_session(
                post,
                base_url,
                client_name=client_name,
                client_fingerprint=client_fingerprint,
                verify=matrix_ssl_verify,
            )
        announce(session)
        outcome = _wait_for_approval(
            post,
            base_url,
            session,
            verify=matrix_ssl_verify,
            sleep=sleep,
            stopped=stopped,
        )
        if outcome == "stopped":
            return None
        if isinstance(outcome, PairCompleteResult):
            return outcome
        if not renew_expired:
            msg = "Approval timed out. Run the command again to get a new link."
            raise ValueError(msg)


def render_qr(text: str) -> str:
    """Render text as a compact QR code made of block characters."""
    import segno  # noqa: PLC0415

    buffer = io.StringIO()
    segno.make(text, error="l").terminal(out=buffer, compact=True)
    return buffer.getvalue()


def local_client_fingerprint(*, config_path: Path) -> str:
    """Return a stable, non-secret local fingerprint."""
    raw = f"{socket.gethostname()}:{config_path.expanduser().resolve()}"
    return f"sha256:{hashlib.sha256(raw.encode('utf-8')).hexdigest()}"


def _print_exports(
    console: Console,
    provisioning_url: str,
    result: PairCompleteResult,
) -> None:
    """Print non-persisted exports for local provisioning credentials."""
    import shlex  # noqa: PLC0415

    console.print("\nExport these variables before running MindRoom:")
    console.print(
        f"  export MINDROOM_PROVISIONING_URL={shlex.quote(provisioning_url)}",
        markup=False,
        soft_wrap=True,
    )
    console.print(f"  export MINDROOM_LOCAL_CLIENT_ID={shlex.quote(result.client_id)}", markup=False, soft_wrap=True)
    console.print(
        f"  export MINDROOM_LOCAL_CLIENT_SECRET={shlex.quote(result.client_secret)}",
        markup=False,
        soft_wrap=True,
    )
    console.print(f"  export MINDROOM_NAMESPACE={shlex.quote(result.namespace)}", markup=False, soft_wrap=True)
    if result.owner_user_id:
        console.print(
            f"  export MINDROOM_OWNER_USER_ID={shlex.quote(result.owner_user_id)}",
            markup=False,
            soft_wrap=True,
        )
        console.print(
            f"\nOwner user ID from pairing: {result.owner_user_id} (not persisted in --no-persist-env mode).",
            markup=False,
        )
        console.print(
            "Update your config.yaml owner placeholder(s) manually if you rely on membership access settings.",
        )


def pair_local_install(
    runtime_paths: RuntimePaths,
    *,
    console: Console,
    provisioning_url: str | None = None,
    client_name: str | None = None,
    persist_env: bool = True,
    open_browser: bool = False,
    post_request: Callable[..., httpx.Response] | None = None,
    sleep: Callable[[float], None] = time.sleep,
    renew_expired: bool = True,
    stop_waiting: Callable[[], bool] | None = None,
) -> PairCompleteResult | None:
    """Pair this install with the hosted provisioning service and save its credentials.

    Returns None without saving anything when stop_waiting reports that another process paired this machine.
    Raises ValueError after printing the credentials when they cannot be saved, because the service issues them only once.
    """
    resolved_url = (
        provisioning_url or runtime_paths.env_value("MINDROOM_PROVISIONING_URL") or "https://mindroom.chat"
    ).strip()
    name = (client_name or "").strip() or socket.gethostname()

    def announce(session: DevicePairSession) -> None:
        console.print("\nConnect this machine to MindRoom:")
        console.print(f"  {session.approve_url}", markup=False, soft_wrap=True)
        console.print(f"  or enter code {session.pair_code} in MindRoom Chat → Settings → Local MindRoom", markup=False)
        if console.is_terminal:
            console.print(render_qr(session.approve_url), markup=False, highlight=False)
        console.print("Waiting for approval (Ctrl+C to cancel)…")
        if open_browser:
            webbrowser.open(session.approve_url)

    result = run_device_pairing(
        provisioning_url=resolved_url,
        client_name=name,
        client_fingerprint=local_client_fingerprint(config_path=runtime_paths.config_path),
        matrix_ssl_verify=constants.runtime_matrix_ssl_verify(runtime_paths=runtime_paths),
        announce=announce,
        post_request=post_request,
        sleep=sleep,
        renew_expired=renew_expired,
        stop_waiting=stop_waiting,
        report_retry=lambda message: console.print(f"[yellow]Warning:[/yellow] {escape(message)}"),
    )
    if result is None:
        console.print("This machine was paired by another MindRoom process; continuing.")
        return None
    if result.owner_user_id_invalid:
        console.print(
            "[yellow]Warning:[/yellow] Pairing response included malformed owner_user_id; skipping config owner autofill.",
        )
    if result.namespace_invalid:
        console.print(
            "[yellow]Warning:[/yellow] Pairing response included malformed namespace; leaving MINDROOM_NAMESPACE empty.",
        )
    owner_text = f" as {result.owner_user_id}" if result.owner_user_id else ""
    console.print(f"[green]Connected{owner_text}.[/green]")
    if persist_env:
        try:
            env_path = persist_local_provisioning_env(
                provisioning_url=resolved_url,
                client_id=result.client_id,
                client_secret=result.client_secret,
                namespace=result.namespace,
                owner_user_id=result.owner_user_id,
                config_path=runtime_paths.config_path,
            )
        except (OSError, ValueError) as exc:
            # The service hands these credentials out once, so show them before failing.
            # Under a service they land in its logs; the connection can be revoked in MindRoom Chat.
            _print_exports(console, resolved_url, result)
            msg = (
                f"Could not save credentials to {env_path_for_config(runtime_paths.config_path)}: {exc}. "
                "Save the exports above; they are not shown again."
            )
            raise ValueError(msg) from exc
        console.print(f"  Saved credentials to: {env_path}")
        if result.owner_user_id:
            _replace_owner_placeholders_or_warn(console, runtime_paths.config_path, result.owner_user_id)
    else:
        _print_exports(console, resolved_url, result)
    return result


def _replace_owner_placeholders_or_warn(console: Console, config_path: Path, owner_user_id: str) -> None:
    """Fill owner placeholders after credentials are saved; a failure here must not lose the pairing."""
    try:
        replaced = replace_owner_placeholders_in_config(config_path=config_path, owner_user_id=owner_user_id)
    except (OSError, ValueError) as exc:
        console.print(
            f"[yellow]Warning:[/yellow] Could not update owner placeholder(s) in {config_path}: {escape(str(exc))}",
        )
        console.print(f"  Replace them with {owner_user_id} manually.", markup=False)
        return
    if replaced:
        console.print(f"  Updated owner placeholder(s) in: {config_path}")


def persist_local_provisioning_env(
    *,
    provisioning_url: str,
    client_id: str,
    client_secret: str,
    namespace: str,
    owner_user_id: str | None = None,
    config_path: str | Path,
) -> Path:
    """Write local provisioning credentials to .env next to the active config file."""
    updates = {
        "MINDROOM_PROVISIONING_URL": provisioning_url.rstrip("/"),
        "MINDROOM_LOCAL_CLIENT_ID": client_id,
        "MINDROOM_LOCAL_CLIENT_SECRET": client_secret,
        "MINDROOM_NAMESPACE": namespace,
    }
    if parsed_owner_user_id := parse_owner_matrix_user_id(owner_user_id):
        updates[OWNER_MATRIX_USER_ID_ENV] = parsed_owner_user_id

    return upsert_env_values(env_path_for_config(config_path), updates)


def _owner_placeholder_config_files(config_path: Path) -> tuple[Path, ...]:
    """Return every config source file: the top-level file plus each !include target."""
    try:
        _, source_files = load_yaml_config_source(config_path)
    except (OSError, yaml.YAMLError, UnicodeError):
        # A config that fails to parse still gets top-level replacement.
        return (config_path,)
    return tuple(sorted(source_files))


def replace_owner_placeholders_in_config(*, config_path: Path, owner_user_id: str) -> bool:
    """Replace owner placeholder tokens in the config and its !include files."""
    if parse_owner_matrix_user_id(owner_user_id) is None:
        return False
    if not config_path.exists():
        return False

    replaced_any = False
    for source_file in _owner_placeholder_config_files(config_path):
        content = source_file.read_text(encoding="utf-8")
        replaced = replace_owner_placeholders_in_text(content, owner_user_id)
        if replaced != content:
            source_file.write_text(replaced, encoding="utf-8")
            replaced_any = True
    return replaced_any


def _required_non_empty_string(data: dict[str, object], key: str) -> str:
    """Read a required string field from a JSON dict."""
    raw_value = data.get(key)
    if isinstance(raw_value, str):
        value = raw_value.strip()
        if value:
            return value
    msg = f"Provisioning response missing {key}."
    raise ValueError(msg)


def _parse_namespace(raw_value: object) -> str | None:
    """Parse optional installation namespace from pairing response."""
    if not isinstance(raw_value, str):
        return None
    namespace = raw_value.strip().lower()
    if not namespace:
        return None
    if _NAMESPACE_RE.fullmatch(namespace):
        return namespace
    return None
