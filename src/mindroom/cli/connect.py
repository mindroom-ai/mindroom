"""Helpers for CLI `connect` command and local onboarding config updates."""

from __future__ import annotations

import hashlib
import io
import re
import socket
import time
import webbrowser
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from functools import partial
from typing import TYPE_CHECKING, Literal
from urllib.parse import urlparse

import yaml
from rich.console import Group
from rich.markup import escape
from rich.style import Style
from rich.table import Table
from rich.text import Text

from mindroom import constants
from mindroom.cli.owner import parse_owner_matrix_user_id, replace_owner_placeholders_in_text
from mindroom.config.yaml_includes import load_yaml_config_source
from mindroom.constants import OWNER_MATRIX_USER_ID_ENV
from mindroom.http_error_detail import error_detail_from_response
from mindroom.matrix.provisioning_env import provisioning_url_from_env

from .env_file import env_path_for_config, upsert_env_values
from .interactive import section

if TYPE_CHECKING:
    from collections.abc import Callable, Iterator, Mapping
    from pathlib import Path

    import httpx
    from rich.console import Console, RenderableType

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
    "self_hosted_pairing_error",
]

_API_PATH = "/v1/local-mindroom/pair/device"
_NAMESPACE_RE = re.compile(r"^[a-z0-9]{4,32}$")
# Issued client credentials are written unquoted to .env and sent as headers, so only plain token characters pass.
_CLIENT_CREDENTIAL_RE = re.compile(r"[A-Za-z0-9._~+/-]{1,512}={0,2}")
_DEFAULT_POLL_INTERVAL_SECONDS = 3
_MAX_BACKOFF_SECONDS = 30
# An approval near expiry may still be handed out, so outages get this grace before timing out locally.
# Kept at least APPROVED_CLAIM_GRACE_SECONDS in scripts/local_mindroom_provisioning_service.py by a contract test.
_EXPIRY_GRACE_SECONDS = 60
# Kept in sync with scripts/local_mindroom_provisioning_service.py by a contract test.
_PAIR_SESSION_ALREADY_CLAIMED_DETAIL = "Pair session already claimed"
_LOCAL_MINDROOM_SETTINGS = "MindRoom Chat → Settings → Local MindRoom"
# Terminals narrower than the QR code plus this gap and details width stack the details below the code.
# A pairing link's QR code is 37 columns wide, so 80-column terminals fit the details beside it.
_PAIRING_DETAILS_GAP = 3
_PAIRING_DETAILS_MIN_WIDTH = 36
_LOST_APPROVAL_WARNING = (
    "The previous approval could not be received; starting a new pairing. "
    f"You can revoke the unused entry in {_LOCAL_MINDROOM_SETTINGS}."
)


@dataclass(frozen=True)
class PairCompleteResult:
    """Credentials returned once by the provisioning device-pairing poll endpoint."""

    client_id: str
    client_secret: str
    namespace: str
    owner_user_id: str | None = None
    namespace_invalid: bool = False
    # The owner the service named when it is not a current-grammar Matrix user ID; it is shown, never saved.
    rejected_owner_user_id: str | None = None


@dataclass(frozen=True)
class DevicePairSession:
    """A pending device pairing the user approves in MindRoom Chat."""

    pair_code: str
    device_secret: str
    approve_url: str
    poll_interval_seconds: int
    expires_at: datetime


class _ServiceError(ValueError):
    """A provisioning request failed; status code 0 means the service was unreachable."""

    def __init__(self, status_code: int, detail: str) -> None:
        if status_code == 0:
            message = f"Could not reach provisioning service: {detail}"
        else:
            message = f"Pairing failed ({status_code}): {detail}"
        super().__init__(message)
        self.status_code = status_code
        self.detail = detail

    @property
    def transient(self) -> bool:
        """Whether retrying later may succeed: unreachable, rate limited, or a server error."""
        return self.status_code in {0, 429} or self.status_code >= 500


def _httpx_post(url: str, *, json: Mapping[str, object], timeout: float) -> httpx.Response:
    """Call httpx.post without importing httpx during CLI help rendering."""
    import httpx  # noqa: PLC0415

    # The service names this install's owner, so TLS is verified whatever MATRIX_SSL_VERIFY says.
    return httpx.post(url, json=json, timeout=timeout)


def _post_json(
    post_request: Callable[..., httpx.Response],
    url: str,
    payload: Mapping[str, object],
) -> dict[str, object]:
    """POST to the provisioning service and return its JSON object, raising _ServiceError for failed requests."""
    import httpx  # noqa: PLC0415

    try:
        response = post_request(url, json=payload, timeout=10)
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
    rejected_owner_user_id = None
    if parsed_owner_user_id is None and raw_owner_user_id is not None:
        named_owner = raw_owner_user_id.strip() if isinstance(raw_owner_user_id, str) else str(raw_owner_user_id)
        rejected_owner_user_id = named_owner or None
    client_id = _required_client_credential(data, "client_id")
    raw_namespace = data.get("namespace")
    parsed_namespace = _parse_namespace(raw_namespace)
    namespace_invalid = isinstance(raw_namespace, str) and bool(raw_namespace.strip()) and parsed_namespace is None
    if parsed_namespace is None:
        parsed_namespace = ""

    return PairCompleteResult(
        client_id=client_id,
        client_secret=_required_client_credential(data, "client_secret"),
        namespace=parsed_namespace,
        owner_user_id=parsed_owner_user_id,
        namespace_invalid=namespace_invalid,
        rejected_owner_user_id=rejected_owner_user_id,
    )


def _validate_poll_interval(raw_value: object) -> int:
    """Return a positive poll interval or the default as fallback."""
    if isinstance(raw_value, bool) or not isinstance(raw_value, int):
        return _DEFAULT_POLL_INTERVAL_SECONDS
    if raw_value <= 0:
        return _DEFAULT_POLL_INTERVAL_SECONDS
    return raw_value


def _parse_expires_at(raw_value: str) -> datetime:
    """Return the session expiry, which the service sends as a timezone-aware ISO timestamp."""
    try:
        return datetime.fromisoformat(raw_value)
    except ValueError:
        msg = "Pairing response has invalid expires_at."
        raise ValueError(msg) from None


def _utc_now() -> datetime:
    return datetime.now(UTC)


def _start_session(
    post_request: Callable[..., httpx.Response],
    base_url: str,
    *,
    client_name: str,
    client_fingerprint: str,
) -> DevicePairSession:
    """Start a device pairing session with the provisioning service."""
    started = _post_json(
        post_request,
        f"{base_url}/start",
        {"client_name": client_name.strip(), "client_pubkey_or_fingerprint": client_fingerprint},
    )
    return DevicePairSession(
        pair_code=_required_non_empty_string(started, "pair_code"),
        device_secret=_required_non_empty_string(started, "device_secret"),
        approve_url=_parse_approve_url(_required_non_empty_string(started, "approve_url")),
        poll_interval_seconds=_validate_poll_interval(started.get("poll_interval_seconds")),
        expires_at=_parse_expires_at(_required_non_empty_string(started, "expires_at")),
    )


def _start_session_with_retry(
    post_request: Callable[..., httpx.Response],
    base_url: str,
    *,
    client_name: str,
    client_fingerprint: str,
    sleep: Callable[[float], None],
    warn: Callable[[str], None] | None,
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
            )
        except _ServiceError as exc:
            if not exc.transient:
                raise
            if warn is not None:
                warn(f"{exc} (retrying in {delay}s)")
        sleep(delay)
        if stopped():
            return None
        delay = min(delay * 2, _MAX_BACKOFF_SECONDS)


def _wait_for_approval(
    post_request: Callable[..., httpx.Response],
    base_url: str,
    session: DevicePairSession,
    *,
    sleep: Callable[[float], None],
    stopped: Callable[[], bool],
    now: Callable[[], datetime],
) -> PairCompleteResult | Literal["expired", "claimed", "stopped"]:
    """Poll until the session is approved, has expired, or stopped() reports that waiting is pointless.

    Returns "claimed" when the service already handed out this session's credentials in a response that never arrived.
    """
    interval = session.poll_interval_seconds
    delay = interval
    while True:
        sleep(delay)
        if stopped():
            return "stopped"
        try:
            polled = _post_json(
                post_request,
                f"{base_url}/poll",
                {"device_secret": session.device_secret},
            )
        except _ServiceError as exc:
            # The service prunes expired sessions, so an unknown device secret means expired.
            if exc.status_code == 404:
                return "expired"
            if exc.status_code == 410 and exc.detail == _PAIR_SESSION_ALREADY_CLAIMED_DETAIL:
                return "claimed"
            if not exc.transient:
                raise
            # The service decides expiry while it answers; this local deadline only covers outages.
            grace = timedelta(seconds=max(interval, _EXPIRY_GRACE_SECONDS))
            if now() >= session.expires_at + grace:
                return "expired"
            # Installs behind one NAT share the service's per-address poll budget, so back off while limited.
            delay = max(interval, min(delay * 2, _MAX_BACKOFF_SECONDS)) if exc.status_code == 429 else interval
            continue

        delay = interval
        outcome = _parse_poll(polled)
        if outcome != "pending":
            return outcome


def _parse_poll(polled: dict[str, object]) -> PairCompleteResult | Literal["expired", "pending"]:
    """Read one successful poll response."""
    status = polled.get("status")
    if status == "connected":
        return _parse_pair_complete(polled)
    if status == "expired":
        return "expired"
    if status == "pending":
        return "pending"
    if status is None:
        msg = "Poll response missing status."
        raise ValueError(msg)
    msg = f"Unexpected poll status: {status}"
    raise ValueError(msg)


def _raise_or_warn_before_renewal(
    outcome: Literal["expired", "claimed"],
    *,
    renew_expired: bool,
    warn: Callable[[str], None] | None,
) -> None:
    """Explain an unapproved session to an interactive caller, or warn an unattended run that it starts over."""
    if renew_expired:
        if outcome == "claimed" and warn is not None:
            warn(_LOST_APPROVAL_WARNING)
        return
    if outcome == "claimed":
        msg = (
            "The approval could not be received: its credentials were issued but never arrived, "
            f"so that connection is unusable and you can revoke the unused entry in {_LOCAL_MINDROOM_SETTINGS}. "
            "Run the command again to get a new link."
        )
        raise ValueError(msg)
    msg = "Approval timed out. Run the command again to get a new link."
    raise ValueError(msg)


def run_device_pairing(
    *,
    provisioning_url: str,
    client_name: str,
    client_fingerprint: str,
    announce: Callable[[DevicePairSession], None],
    post_request: Callable[..., httpx.Response] | None = None,
    sleep: Callable[[float], None] | None = None,
    renew_expired: bool = True,
    stop_waiting: Callable[[], bool] | None = None,
    warn: Callable[[str], None] | None = None,
    now: Callable[[], datetime] = _utc_now,
) -> PairCompleteResult | None:
    """Wait until a signed-in user approves this machine.

    When renew_expired is True, expired sessions and approvals whose credentials never arrived start a new code and announce it again, and transient failures to start a session are retried with backoff.
    When False, these outcomes and start failures raise ValueError (prevents indefinite waiting in interactive flows).
    Returns None when stop_waiting, checked before each session start, start retry, and poll, reports that pairing is no longer needed.
    The callback may instead raise to abort its caller, as the CLI does for local cancellation.
    """
    post = post_request or _httpx_post
    sleep = sleep or time.sleep
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
                sleep=sleep,
                warn=warn,
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
            )
        announce(session)
        outcome = _wait_for_approval(
            post,
            base_url,
            session,
            sleep=sleep,
            stopped=stopped,
            now=now,
        )
        if outcome == "stopped":
            return None
        if isinstance(outcome, PairCompleteResult):
            return outcome
        _raise_or_warn_before_renewal(outcome, renew_expired=renew_expired, warn=warn)


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
    sleep: Callable[[float], None] | None = None,
    renew_expired: bool = True,
    stop_waiting: Callable[[], bool] | None = None,
    confirm_approver: Callable[[], bool] | None = None,
) -> PairCompleteResult | None:
    """Pair this install with the hosted provisioning service and save its credentials.

    The approving account is always printed; confirm_approver, given only when a person can answer, asks whether it is theirs before anything is saved.
    Returns None without saving anything when stop_waiting reports that another process paired this machine.
    Raises ValueError without saving when the approver is declined.
    Raises ValueError after printing the credentials when they cannot be saved, because the service issues them only once.
    """
    resolved_url = (
        provisioning_url or runtime_paths.env_value("MINDROOM_PROVISIONING_URL") or "https://mindroom.chat"
    ).strip()
    name = (client_name or "").strip() or socket.gethostname()

    with _pairing_display(console) as show_pair_code:

        def announce(session: DevicePairSession) -> None:
            show_pair_code(session)
            if open_browser:
                webbrowser.open(session.approve_url)

        result = run_device_pairing(
            provisioning_url=resolved_url,
            client_name=name,
            client_fingerprint=local_client_fingerprint(config_path=runtime_paths.config_path),
            announce=announce,
            post_request=post_request,
            sleep=sleep,
            renew_expired=renew_expired,
            stop_waiting=stop_waiting,
            warn=lambda message: console.print(f"[yellow]Warning:[/yellow] {escape(message)}"),
        )
    if result is None:
        console.print("This machine was paired by another MindRoom process; continuing.")
        return None
    if result.rejected_owner_user_id is not None:
        console.print(
            "[yellow]Warning:[/yellow] Pairing response included malformed owner_user_id; skipping config owner autofill.",
        )
    if result.namespace_invalid:
        console.print(
            "[yellow]Warning:[/yellow] Pairing response included malformed namespace; leaving MINDROOM_NAMESPACE empty.",
        )
    _confirm_approver_or_raise(console, result, confirm_approver)
    console.print("[green]Connected.[/green]")
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


@contextmanager
def _pairing_display(console: Console) -> Iterator[Callable[[DevicePairSession], None]]:
    """Yield the callback that shows each pair code, spinning in a terminal until pairing ends.

    Anywhere else it prints plain lines, which logs keep and the macOS app reads the link from.
    `FORCE_COLOR` makes Rich treat pipes as terminals, so the output must also be a TTY, and not a dumb one.
    A dumb TTY, such as an Emacs shell buffer, cannot draw the spinner but still gets the QR code.
    """
    tty = console.is_terminal and console.file.isatty()
    if not (tty and console.is_interactive):
        yield partial(_print_plain_pairing_request, console, with_qr=tty)
        return
    console.print(section("Connect to MindRoom Chat"))
    with console.status("Requesting a pair code…") as waiting:

        def show_pair_code(session: DevicePairSession) -> None:
            # Printing while the spinner runs keeps the spinner below; restarting it would erase lines above it.
            console.print(_terminal_pairing_request(session, width=console.width))
            expires_at = session.expires_at.astimezone().strftime("%H:%M")
            waiting.update(f"Waiting for approval · the code expires at {expires_at} · Ctrl+C to cancel")

        yield show_pair_code


def _print_plain_pairing_request(console: Console, session: DevicePairSession, *, with_qr: bool) -> None:
    """Print the approval link on its own line with the pair code, for logs and the macOS app."""
    console.print("\nConnect this machine to MindRoom:")
    console.print(f"  {session.approve_url}", markup=False, soft_wrap=True)
    console.print(f"  or enter code {session.pair_code} in {_LOCAL_MINDROOM_SETTINGS}", markup=False)
    console.print(f"  Approve only if the page shows code {session.pair_code}", markup=False)
    if with_qr:
        console.print(render_qr(session.approve_url), markup=False, highlight=False)
    console.print("Waiting for approval (Ctrl+C to cancel)…")


def _terminal_pairing_request(session: DevicePairSession, *, width: int) -> Group:
    """Return the approval link on its own unwrapped line, then the QR code beside the pair code to check."""
    # The QR code's full blocks are its light modules, so fixed colors keep it scannable in light themes too.
    # Its odd module count leaves a last line of upper half blocks, whose lower half lies outside the code.
    *qr_rows, qr_last_row = render_qr(session.approve_url).rstrip("\n").splitlines()
    qr_code = Text.assemble(("\n".join(qr_rows), "#ffffff on #000000"), "\n", (qr_last_row, "#ffffff"), no_wrap=True)
    qr_width = len(qr_last_row)
    details = Text.assemble(
        ("Pair code  ", "dim"),
        (session.pair_code, "bold cyan"),
        "\n\n",
        f"Approve only if the page shows\ncode {session.pair_code}.\n\n",
        ("Or enter the code in MindRoom Chat\n→ Settings → Local MindRoom.", "dim"),
    )
    if width < qr_width + _PAIRING_DETAILS_GAP + _PAIRING_DETAILS_MIN_WIDTH:
        codes: RenderableType = Group(qr_code, details)
    else:
        codes = Table.grid(padding=(0, _PAIRING_DETAILS_GAP))
        codes.add_column(no_wrap=True, vertical="middle")
        codes.add_column(vertical="middle")
        codes.add_row(qr_code, details)
    link = Text.assemble(
        "  ",
        (session.approve_url, Style(color="bright_blue", underline=True, link=session.approve_url)),
        no_wrap=True,
        overflow="ignore",
    )
    return Group("Open this link or scan the QR code, then approve this machine in MindRoom Chat:", link, codes)


def _confirm_approver_or_raise(
    console: Console,
    result: PairCompleteResult,
    confirm_approver: Callable[[], bool] | None,
) -> None:
    """Show who approved this machine and, when someone can answer, discard the credentials unless it was them.

    A pair code or link can be approved by whoever sees it, so the approving account is the one agents will trust.
    """
    approver = result.owner_user_id
    description = approver or "an account the provisioning service did not identify"
    if result.rejected_owner_user_id is not None:
        # A named account stays named even when it cannot be saved; repr keeps control characters visible.
        approver = repr(result.rejected_owner_user_id)
        description = f"{approver}, which is not a valid Matrix user ID"
    console.print(f"\n[bold]Approved by {escape(description)}.[/bold]")
    # Nobody can recognize an unnamed account, so it gets the same revoke hint as an unattended run.
    if confirm_approver is None or approver is None:
        console.print(f"If this is not your account, revoke this connection in {_LOCAL_MINDROOM_SETTINGS}.")
        return
    if not confirm_approver():
        msg = (
            "Credentials discarded; nothing was saved. "
            f"The connection approved by {approver} is unusable; revoke it in {_LOCAL_MINDROOM_SETTINGS}."
        )
        raise ValueError(msg)


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
    elif _should_note_missing_administrator(config_path, owner_user_id):
        # A re-pair with another account finds no placeholder left, so the earlier account stays in charge.
        console.print(
            f"  Note: {owner_user_id} is not listed in administrators in {config_path}. "
            "If this account should manage MindRoom, add it to administrators, "
            "room_defaults.invite_users, and room_defaults.admins.",
            markup=False,
        )


def _should_note_missing_administrator(config_path: Path, owner_user_id: str) -> bool:
    """Return True when the config's administrators list omits the user.

    Return False when the config cannot be read, does not load as a mapping, or has a non-list administrators value.
    A missing or null administrators value counts as an empty list.
    """
    try:
        data, _ = load_yaml_config_source(config_path)
    except (OSError, yaml.YAMLError, UnicodeError):
        return False
    if not isinstance(data, dict):
        return False
    administrators = data.get("administrators")
    if administrators is None:
        return True
    return isinstance(administrators, list) and owner_user_id not in administrators


def self_hosted_pairing_error(runtime_paths: RuntimePaths) -> str | None:
    """Explain why pairing does not apply to a self-hosted homeserver with no provisioning service configured."""
    if provisioning_url_from_env(runtime_paths) is not None:
        return None
    homeserver = constants.runtime_matrix_homeserver(runtime_paths).strip()
    if constants.is_hosted_matrix_homeserver(homeserver):
        return None
    hosted_hostname = urlparse(constants.DEFAULT_MATRIX_HOMESERVER).hostname
    return (
        f"The Matrix homeserver URL is {homeserver} (MATRIX_HOMESERVER); "
        f"hosted {hosted_hostname} pairing requires {constants.DEFAULT_MATRIX_HOMESERVER}. "
        "Self-hosted servers register agents with "
        "MATRIX_REGISTRATION_TOKEN or MATRIX_REGISTRATION_SHARED_SECRET instead. "
        "For hosted defaults, run `mindroom config init --matrix-server mindroom.chat`; "
        "to pair with your own provisioning service, pass --provisioning-url."
    )


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


def _required_client_credential(data: dict[str, object], key: str) -> str:
    """Read an issued client credential, refusing anything but a plain token."""
    value = _required_non_empty_string(data, key)
    if _CLIENT_CREDENTIAL_RE.fullmatch(value) is None:
        msg = f"Provisioning response has invalid {key}."
        raise ValueError(msg)
    return value


def _parse_approve_url(raw_value: str) -> str:
    """Return an approval link a browser may open: a plain http(s) URL that every URL parser reads alike."""
    msg = "Pairing response has invalid approve_url."
    if not raw_value.isprintable() or " " in raw_value or "\\" in raw_value:
        raise ValueError(msg)
    try:
        parsed = urlparse(raw_value)
    except ValueError:
        raise ValueError(msg) from None
    if parsed.scheme not in {"http", "https"} or not parsed.hostname or parsed.username is not None:
        raise ValueError(msg)
    return raw_value


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
