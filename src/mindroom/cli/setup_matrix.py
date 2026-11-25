"""Interactive Matrix homeserver discovery, probing, and selection for starter setup."""

from __future__ import annotations

from dataclasses import dataclass
from typing import TYPE_CHECKING, Literal
from urllib.parse import urlsplit

from rich.markup import escape

from mindroom import constants
from mindroom.cli.env_file import format_env_assignment

if TYPE_CHECKING:
    from collections.abc import Mapping
    from pathlib import Path

    from mindroom.cli.setup_wizard import WizardIO
    from mindroom.constants import RuntimePaths

MatrixServerPreset = Literal["self-hosted", "mindroom.chat"]
_MatrixAuthMethod = Literal["token", "shared_secret", "open", "provisioning"]
_MenuChoice = Literal["mindroom.chat", "custom"]
_AuthChoice = Literal["token", "shared_secret", "open", "provisioning_url"]

_SELF_HOSTED_PLACEHOLDER = "https://matrix.example.com"
_PROVISIONING_CONNECTIONS_PATH = "/v1/local-mindroom/connections"
# Kept in sync with scripts/local_mindroom_provisioning_service.py (`_verify_openid_user`).
_PROVISIONING_MISSING_OPENID_DETAIL = "Missing Matrix OpenID token"
_PROBE_TIMEOUT_SECONDS = 4.0
_REG_TOKEN = "MATRIX_REGISTRATION_TOKEN"  # noqa: S105
_REG_SECRET = "MATRIX_REGISTRATION_SHARED_SECRET"  # noqa: S105
_REG_SECRET_FILE = "MATRIX_REGISTRATION_SHARED_SECRET_FILE"  # noqa: S105
_PAIRING_CRED_KEYS = ("MINDROOM_LOCAL_CLIENT_ID", "MINDROOM_LOCAL_CLIENT_SECRET")
_PROV_KEYS = ("MINDROOM_PROVISIONING_URL", *_PAIRING_CRED_KEYS)
_AUTH_SECRETS = {
    "token": ("Matrix registration token", _REG_TOKEN),
    "shared_secret": ("Matrix registration shared secret", _REG_SECRET),
}
_CLEAR_ON_AUTH: dict[_MatrixAuthMethod, tuple[str, ...]] = {
    "provisioning": (_REG_TOKEN, _REG_SECRET, _REG_SECRET_FILE),
    "token": (*_PROV_KEYS, _REG_SECRET, _REG_SECRET_FILE),
    "shared_secret": (*_PROV_KEYS, _REG_TOKEN),
    "open": (*_PROV_KEYS, _REG_TOKEN, _REG_SECRET, _REG_SECRET_FILE),
}
_HOSTED_ENV_BLOCK = (
    "# Matrix homeserver (must allow open registration for agent accounts)\n"
    f"MATRIX_HOMESERVER={constants.DEFAULT_MATRIX_HOMESERVER}\n# MATRIX_SSL_VERIFY=false\n"
    "# Matrix server_name override (needed when federation hostname differs)\nMATRIX_SERVER_NAME=mindroom.chat\n\n"
    "# Hosted provisioning API; lets `mindroom run` pair with your MindRoom Chat account\n"
    f"MINDROOM_PROVISIONING_URL={constants.DEFAULT_MATRIX_HOMESERVER}\n\n"
    "# Optional hosted installation namespace. Leave blank for existing unnamespaced rooms.\nMINDROOM_NAMESPACE=\n\n"
    "# Leave MATRIX_REGISTRATION_TOKEN empty on mindroom.chat; pairing replaces it.\n"
    "# Setting any token skips pairing. Keep real values secret; do not commit them.\nMATRIX_REGISTRATION_TOKEN="
)
_UNCONFIGURED_ENV_BLOCK = (
    "# Matrix homeserver (must allow open registration for agent accounts)\n"
    f"MATRIX_HOMESERVER={_SELF_HOSTED_PLACEHOLDER}\n# MATRIX_SSL_VERIFY=false\n"
    "# Matrix registration token (only needed if your homeserver requires it)\n# MATRIX_REGISTRATION_TOKEN="
)


@dataclass(frozen=True, slots=True)
class _ParsedMatrixServerInput:
    """Normalized user input for a Matrix server domain or URL."""

    url: str
    server_name: str
    is_bare_domain: bool


@dataclass(frozen=True, slots=True)
class _MatrixServerProbeResult:
    """Discovery, reachability, and provisioning probe outcome for a custom Matrix server."""

    homeserver: str
    discovered_via_well_known: bool = False
    reachable: bool = False
    provisioning_detected: bool = False


@dataclass(frozen=True, slots=True)
class StarterMatrixSelection:
    """Resolved Matrix homeserver and account-registration setup for starter files."""

    preset: MatrixServerPreset
    homeserver: str
    server_name: str | None = None
    provisioning_url: str | None = None
    registration_token: str | None = None
    registration_shared_secret: str | None = None
    owner_user_id: str | None = None
    auth_method: _MatrixAuthMethod | None = None
    configured: bool = True

    @property
    def is_hosted(self) -> bool:
        """Whether this selection targets hosted MindRoom Chat (`mindroom.chat`)."""
        return self.preset == "mindroom.chat"

    @property
    def uses_provisioning(self) -> bool:
        """Whether agent accounts are registered through a local-MindRoom provisioning service."""
        return self.provisioning_url is not None

    @property
    def summary_label(self) -> str:
        """Human-readable label for setup summaries."""
        if self.is_hosted:
            return "MindRoom Chat (mindroom.chat)"
        if self.server_name and self.server_name != _homeserver_host(self.homeserver):
            return f"{self.homeserver} (server name: {self.server_name})"
        return self.homeserver


def hosted_matrix_selection() -> StarterMatrixSelection:
    """Return the default hosted MindRoom Chat (`mindroom.chat`) selection."""
    url = constants.DEFAULT_MATRIX_HOMESERVER
    return StarterMatrixSelection("mindroom.chat", url, "mindroom.chat", url, auth_method="provisioning")


def unconfigured_self_hosted_matrix_selection() -> StarterMatrixSelection:
    """Return the placeholder self-hosted selection used when running non-interactively."""
    return StarterMatrixSelection("self-hosted", _SELF_HOSTED_PLACEHOLDER, configured=False)


def _homeserver_host(homeserver_url: str) -> str:
    host = (urlsplit(homeserver_url.strip()).hostname or "").lower()
    return f"[{host}]" if ":" in host else (host or homeserver_url.strip())


def _parse_matrix_server_input(raw: str) -> _ParsedMatrixServerInput | None:
    """Normalize a bare server name (`example.com` or `example.org:8448`) or explicit `http(s)://` URL."""
    from mindroom.matrix.identity import valid_matrix_server_name  # noqa: PLC0415  # lazy: keeps CLI startup slim

    cleaned = raw.strip()
    is_bare, domain = "://" not in cleaned, cleaned.rstrip("/")
    bad_chars = any(c.isspace() for c in cleaned) or any(b in cleaned for b in ("${", "@", "\\"))
    bad_bare = is_bare and (not domain or any(c in domain for c in "/?#"))
    if not cleaned or not cleaned.isprintable() or bad_chars or bad_bare:
        return None
    try:
        parsed = urlsplit(f"https://{domain}" if is_bare else cleaned)
        port = parsed.port
    except ValueError:
        return None

    scheme, host = parsed.scheme.lower(), (parsed.hostname or "").lower()
    host_lit = f"[{host}]" if ":" in host else host
    server_name = f"{host_lit}:{port}" if is_bare and port is not None else host_lit
    bad_host = ":" not in host and any(lbl.startswith("-") or lbl.endswith("-") for lbl in host.split("."))
    bad_port = port is not None and not (1 <= port <= 65535)
    if scheme not in {"http", "https"} or not host or parsed.query or parsed.fragment or bad_port or bad_host:
        return None
    if not valid_matrix_server_name(server_name):
        return None

    port_part = f":{port}" if port is not None and port != (443 if scheme == "https" else 80) else ""
    path = "" if is_bare else parsed.path.rstrip("/")
    return _ParsedMatrixServerInput(f"{scheme}://{host_lit}{port_part}{path}", server_name, is_bare)


def _normalize_server_url(raw: str | None) -> str | None:
    cleaned = (raw or "").strip()
    if not cleaned:
        return None
    parsed = _parse_matrix_server_input(cleaned)
    return parsed.url if parsed is not None else cleaned.rstrip("/")


def is_self_hosted_placeholder(raw: str | None) -> bool:
    """Return whether `raw` is the unconfigured self-hosted starter placeholder URL."""
    return _normalize_server_url(raw) == _SELF_HOSTED_PLACEHOLDER


def _http_get_json(url: str, *, timeout_seconds: float) -> tuple[int | None, object]:
    import httpx  # noqa: PLC0415  # lazy: keeps CLI startup slim

    try:
        with httpx.Client(timeout=timeout_seconds, follow_redirects=False) as client:
            response = client.get(url)
    except (httpx.HTTPError, ImportError, OSError):
        return None, None
    try:
        return response.status_code, response.json()
    except ValueError:
        return response.status_code, None


def _probe_provisioning_service(base_url: str, *, timeout_seconds: float = _PROBE_TIMEOUT_SECONDS) -> bool:
    """Return whether `base_url` exposes MindRoom's local provisioning service (fail-open)."""
    url = f"{base_url.rstrip('/')}{_PROVISIONING_CONNECTIONS_PATH}"
    status, payload = _http_get_json(url, timeout_seconds=timeout_seconds)
    return status == 401 and isinstance(payload, dict) and payload.get("detail") == _PROVISIONING_MISSING_OPENID_DETAIL


def _probe_matrix_server(
    parsed: _ParsedMatrixServerInput,
    *,
    timeout_seconds: float = _PROBE_TIMEOUT_SECONDS,
) -> _MatrixServerProbeResult:
    """Discover `.well-known/matrix/client` (blocking HTTPS->HTTP downgrade), verify versions, and probe provisioning."""
    homeserver, discovered, require_https = parsed.url, False, parsed.url.startswith("https://")
    if not urlsplit(parsed.url).path:
        wk_url = f"{parsed.url}/.well-known/matrix/client"
        wk_status, wk_payload = _http_get_json(wk_url, timeout_seconds=timeout_seconds)
        if wk_status == 200 and isinstance(wk_payload, dict):
            m_hs = wk_payload.get("m.homeserver")
            raw_base = m_hs.get("base_url") if isinstance(m_hs, dict) else None
            resolved = _parse_matrix_server_input(raw_base) if isinstance(raw_base, str) else None
            https_ok = resolved is not None and (not require_https or resolved.url.startswith("https://"))
            if resolved is not None and not resolved.is_bare_domain and https_ok:
                homeserver, discovered = resolved.url, True

    ver_status, ver_payload = _http_get_json(f"{homeserver}/_matrix/client/versions", timeout_seconds=timeout_seconds)
    if ver_status is None:
        return _MatrixServerProbeResult(homeserver, discovered_via_well_known=discovered)
    reachable = ver_status == 200 and isinstance(ver_payload, dict) and isinstance(ver_payload.get("versions"), list)
    prov = _probe_provisioning_service(homeserver, timeout_seconds=timeout_seconds)
    return _MatrixServerProbeResult(homeserver, discovered, reachable, prov)


def ensure_kept_env_compatible(selection: StarterMatrixSelection, env_path: Path) -> None:
    """Refuse merging into an existing `.env` that targets a different Matrix server."""
    if not env_path.exists():
        return
    from dotenv import dotenv_values  # noqa: PLC0415  # lazy: keeps CLI startup slim

    existing = dotenv_values(env_path, encoding="utf-8-sig")
    raw_hs = _normalize_server_url(existing.get("MATRIX_HOMESERVER"))
    existing_hs = None if raw_hs == _SELF_HOSTED_PLACEHOLDER else raw_hs
    existing_prov = _normalize_server_url(existing.get("MINDROOM_PROVISIONING_URL"))
    hosted_prov = existing_prov == constants.DEFAULT_MATRIX_HOMESERVER
    has_pairing = any((existing.get(k) or "").strip() for k in _PAIRING_CRED_KEYS)
    if existing_hs is None and (hosted_prov or (existing_prov is None and has_pairing)):
        existing_hs = constants.DEFAULT_MATRIX_HOMESERVER

    target_hs = _normalize_server_url(selection.homeserver) or selection.homeserver.strip().rstrip("/")
    target_prov = _normalize_server_url(selection.provisioning_url)
    hs_diff = existing_hs is not None and existing_hs != target_hs
    prov_diff = existing_prov is not None and (
        (target_prov is not None and existing_prov != target_prov) or (target_prov is None and existing_hs is None)
    )
    if hs_diff or prov_diff:
        msg = (
            f"Existing {env_path} targets another Matrix server. "
            "Overwrite .env (e.g. `mindroom config init --force`) or use a fresh --path."
        )
        raise ValueError(msg)


def _prompt_matrix_url(message: str, *, default: str = "", io: WizardIO) -> _ParsedMatrixServerInput:
    current = default
    while True:
        if (parsed := _parse_matrix_server_input(io.text(message, default=current).strip())) is not None:
            return parsed
        io.console.print(
            "[red]✗[/red] Enter a valid domain or http(s) URL (e.g. example.com or http://localhost:8008).",
        )
        current = ""


def _prompt_server_name(default_name: str, *, io: WizardIO) -> str:
    from mindroom.matrix.identity import valid_matrix_server_name  # noqa: PLC0415  # lazy: keeps CLI startup slim

    while True:
        candidate = io.text("Matrix server name (domain in @user:domain)", default=default_name).strip() or default_name
        if "${" not in candidate and valid_matrix_server_name(candidate):
            return candidate
        io.console.print("[red]✗[/red] Invalid Matrix server name. Enter a domain such as example.com or localhost.")


def _prompt_env_secret(label: str, env_key: str, *, io: WizardIO) -> str | None:
    while True:
        if not (value := io.secret(f"{label} (input hidden, press Enter to skip)").strip()):
            return None
        try:
            format_env_assignment(env_key, value)
        except ValueError as exc:
            io.console.print(f"[red]✗[/red] {escape(str(exc))}. Try again, or press Enter to skip.")
            continue
        return value


def _prompt_owner_user_id(domain: str, *, io: WizardIO) -> str | None:
    from mindroom.cli.owner import parse_owner_matrix_user_id  # noqa: PLC0415  # lazy: keeps CLI startup slim

    while True:
        if not (raw := io.text(f"Your Matrix user ID (e.g. @alice:{domain}, press Enter to skip)").strip()):
            return None
        if (parsed := parse_owner_matrix_user_id(raw)) is not None:
            return parsed
        io.console.print(f"[red]✗[/red] Invalid Matrix user ID. Use @user:{escape(domain)}, or press Enter to skip.")


def _prompt_custom_server_auth(*, homeserver: str, server_name: str, io: WizardIO) -> StarterMatrixSelection:
    w = len("Registration shared secret") + 2
    auth_choice: _AuthChoice = io.select(
        "How should MindRoom register agent accounts on this server?",
        [
            ("token", f"{'Registration token':<{w}}MATRIX_REGISTRATION_TOKEN"),
            ("shared_secret", f"{'Registration shared secret':<{w}}MATRIX_REGISTRATION_SHARED_SECRET"),
            ("open", f"{'Open registration':<{w}}no token needed"),
            ("provisioning_url", f"{'Provisioning service URL':<{w}}MINDROOM_PROVISIONING_URL on another host"),
        ],
    )
    if auth_choice == "provisioning_url":
        prov = _prompt_matrix_url("Provisioning service URL (e.g. https://provision.example.com)", io=io)
        with io.console.status("Checking provisioning service..."):
            ok = _probe_provisioning_service(prov.url)
        if ok:
            io.print_literal("[green]✓[/green] Local pairing supported on ", prov.url)
        else:
            io.print_literal(
                "[yellow]Warning:[/yellow] Could not verify provisioning at ",
                prov.url,
                "; saving anyway.",
            )
        return StarterMatrixSelection("self-hosted", homeserver, server_name, prov.url, auth_method="provisioning")

    val = _prompt_env_secret(*_AUTH_SECRETS[auth_choice], io=io) if auth_choice in _AUTH_SECRETS else None
    tok, sec = (
        (val, None) if auth_choice == "token" else (None, val) if auth_choice == "shared_secret" else (None, None)
    )
    owner = _prompt_owner_user_id(server_name, io=io)
    return StarterMatrixSelection("self-hosted", homeserver, server_name, None, tok, sec, owner, auth_choice)


def _resolve_server_name(
    parsed: _ParsedMatrixServerInput,
    probe: _MatrixServerProbeResult,
    existing_parsed: _ParsedMatrixServerInput | None,
    runtime_paths: RuntimePaths,
    *,
    io: WizardIO,
) -> str:
    if probe.discovered_via_well_known and parsed.server_name != _homeserver_host(probe.homeserver):
        return parsed.server_name
    from mindroom.matrix.identity import valid_matrix_server_name  # noqa: PLC0415  # lazy: keeps CLI startup slim

    existing_sn = (runtime_paths.env_value("MATRIX_SERVER_NAME") or "").strip()
    reuse = existing_parsed is not None and parsed.url == existing_parsed.url and valid_matrix_server_name(existing_sn)
    return _prompt_server_name(existing_sn if reuse else parsed.server_name, io=io)


def _select_custom_matrix_server(runtime_paths: RuntimePaths, *, io: WizardIO) -> StarterMatrixSelection:
    existing_hs = (runtime_paths.env_value("MATRIX_HOMESERVER") or "").strip()
    skip = {None, constants.DEFAULT_MATRIX_HOMESERVER, _SELF_HOSTED_PLACEHOLDER}
    existing_parsed = (
        _parse_matrix_server_input(existing_hs) if _normalize_server_url(existing_hs) not in skip else None
    )
    prompt = "Matrix server domain or URL (e.g. example.com or https://matrix.example.com)"
    parsed = _prompt_matrix_url(prompt, default=existing_hs if existing_parsed else "", io=io)
    if constants.is_hosted_matrix_homeserver(parsed.url):
        return hosted_matrix_selection()

    with io.console.status("Checking Matrix server..."):
        probe = _probe_matrix_server(parsed)
    hs, delegated = probe.homeserver, probe.discovered_via_well_known and probe.homeserver != parsed.url
    if delegated:
        io.print_literal("[green]✓[/green] Discovered homeserver ", hs, f" (server name: {escape(parsed.server_name)})")
    if probe.reachable and not delegated:
        io.print_literal("[green]✓[/green] Reachable Matrix homeserver: ", hs)
    elif not probe.reachable:
        io.print_literal("[yellow]Warning:[/yellow] Could not reach Matrix homeserver at ", hs, "; saving anyway.")

    server_name = _resolve_server_name(parsed, probe, existing_parsed, runtime_paths, io=io)
    if probe.provisioning_detected:
        io.print_literal("[green]✓[/green] Local pairing supported on ", hs)
        return StarterMatrixSelection("self-hosted", hs, server_name, hs, auth_method="provisioning")
    return _prompt_custom_server_auth(homeserver=hs, server_name=server_name, io=io)


def resolve_interactive_matrix_server(
    runtime_paths: RuntimePaths,
    *,
    io: WizardIO,
    explicit_preset: MatrixServerPreset | None = None,
) -> StarterMatrixSelection:
    """Run the interactive Matrix server menu and optional custom server discovery prompts."""
    w = len("MindRoom Chat (mindroom.chat)") + 2
    choices: list[tuple[_MenuChoice, str]] = [
        ("mindroom.chat", f"{'MindRoom Chat (mindroom.chat)':<{w}}hosted Matrix + QR pairing"),
        ("custom", f"{'Custom Matrix server':<{w}}connect to your own or company homeserver"),
    ]
    menu_prompt = "Which Matrix server should your agents connect to?"
    if explicit_preset is None and io.select(menu_prompt, choices) == "mindroom.chat":
        selection = hosted_matrix_selection()
    else:
        selection = _select_custom_matrix_server(runtime_paths, io=io)

    exported = _normalize_server_url(runtime_paths.process_env.get("MATRIX_HOMESERVER"))
    target = _normalize_server_url(selection.homeserver) or selection.homeserver.strip().rstrip("/")
    if exported and exported != target:
        msg = (
            "[yellow]Warning:[/yellow] MATRIX_HOMESERVER is exported in your shell environment and will override .env."
        )
        io.console.print(msg)
    return selection


def _infer_auth_method(s: StarterMatrixSelection) -> _MatrixAuthMethod:
    if s.auth_method:
        return s.auth_method
    pairs: tuple[tuple[_MatrixAuthMethod, object], ...] = (
        ("provisioning", s.uses_provisioning),
        ("token", s.registration_token),
        ("shared_secret", s.registration_shared_secret),
    )
    return next((m for m, val in pairs if val), "open")


def custom_matrix_env_updates(
    selection: StarterMatrixSelection,
    *,
    existing_env: Mapping[str, str | None] | None = None,
) -> dict[str, str]:
    """Return same-server `.env` updates for a custom server, preserving skipped secrets and owner."""
    existing, auth = existing_env or {}, _infer_auth_method(selection)
    updates: dict[str, str] = {
        "MATRIX_HOMESERVER": selection.homeserver,
        "MATRIX_SERVER_NAME": selection.server_name or _homeserver_host(selection.homeserver),
    }
    to_clear = list(_CLEAR_ON_AUTH[auth])
    if auth == "provisioning":
        updates["MINDROOM_PROVISIONING_URL"] = selection.provisioning_url or ""
        existing_prov = _normalize_server_url(existing.get("MINDROOM_PROVISIONING_URL"))
        if not (existing_prov and existing_prov == _normalize_server_url(selection.provisioning_url)):
            to_clear.extend((*_PAIRING_CRED_KEYS, "MINDROOM_NAMESPACE"))
    elif auth == "token" and selection.registration_token is not None:
        updates[_REG_TOKEN] = selection.registration_token
    elif auth == "shared_secret" and selection.registration_shared_secret is not None:
        updates[_REG_SECRET] = selection.registration_shared_secret
        to_clear.append(_REG_SECRET_FILE)

    for key in to_clear:
        if (existing.get(key) or "").strip():
            updates[key] = ""
    if selection.owner_user_id:
        updates[constants.OWNER_MATRIX_USER_ID_ENV] = selection.owner_user_id
    return updates


def render_matrix_env_block(selection: StarterMatrixSelection) -> str:
    """Render the Matrix section of a starter `.env` file."""
    if selection.is_hosted:
        return _HOSTED_ENV_BLOCK
    if not selection.configured:
        return _UNCONFIGURED_ENV_BLOCK

    lines = [
        "# Matrix homeserver",
        format_env_assignment("MATRIX_HOMESERVER", selection.homeserver),
        "# MATRIX_SSL_VERIFY=false",
        "# Matrix server_name (domain in @user:domain)",
        format_env_assignment("MATRIX_SERVER_NAME", selection.server_name or _homeserver_host(selection.homeserver)),
    ]
    if selection.provisioning_url:
        lines += [
            "",
            "# Local provisioning API; lets `mindroom run` pair with your Matrix account",
            format_env_assignment("MINDROOM_PROVISIONING_URL", selection.provisioning_url),
            "MINDROOM_NAMESPACE=",
            "MATRIX_REGISTRATION_TOKEN=",
        ]
    elif selection.registration_token:
        lines.append(format_env_assignment(_REG_TOKEN, selection.registration_token))
    elif selection.registration_shared_secret:
        lines.append(format_env_assignment(_REG_SECRET, selection.registration_shared_secret))
    else:
        lines.append("# MATRIX_REGISTRATION_TOKEN=")
    if selection.owner_user_id:
        lines.append(format_env_assignment(constants.OWNER_MATRIX_USER_ID_ENV, selection.owner_user_id))
    return "\n".join(lines)
