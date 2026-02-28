"""Provisioning helpers for hosted local-MindRoom registration flows."""

from __future__ import annotations

import os
import re
from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING, Literal

import httpx

from mindroom.constants import CONFIG_PATH, MATRIX_SSL_VERIFY, OWNER_MATRIX_USER_ID_PLACEHOLDER

if TYPE_CHECKING:
    from collections.abc import Callable

_PAIR_CODE_RE = re.compile(r"^[A-Z0-9]{4}-[A-Z0-9]{4}$")
_MATRIX_USER_ID_RE = re.compile(r"^@[^:\s]+:[^:\s]+$")
_LEGACY_OWNER_PLACEHOLDER = "__PLACEHOLDER__"


def provisioning_url_from_env() -> str | None:
    """Get hosted provisioning API base URL from environment if configured."""
    url = os.getenv("MINDROOM_PROVISIONING_URL", "").strip()
    return url.rstrip("/") or None


def registration_token_from_env() -> str | None:
    """Get MATRIX_REGISTRATION_TOKEN from environment if configured."""
    token = os.getenv("MATRIX_REGISTRATION_TOKEN", "").strip()
    return token or None


def local_provisioning_client_credentials_from_env() -> tuple[str, str] | None:
    """Get local provisioning client credentials from environment if configured."""
    client_id = os.getenv("MINDROOM_LOCAL_CLIENT_ID", "").strip()
    client_secret = os.getenv("MINDROOM_LOCAL_CLIENT_SECRET", "").strip()
    if not client_id and not client_secret:
        return None
    if not client_id or not client_secret:
        msg = (
            "Provisioning credentials are incomplete. "
            "Set both MINDROOM_LOCAL_CLIENT_ID and MINDROOM_LOCAL_CLIENT_SECRET, "
            "or run `mindroom connect --pair-code ...` again."
        )
        raise ValueError(msg)
    return client_id, client_secret


def required_local_provisioning_client_credentials_for_registration(
    *,
    provisioning_url: str | None,
    registration_token: str | None,
) -> tuple[str, str] | None:
    """Resolve required local provisioning credentials when using hosted registration."""
    if registration_token or not provisioning_url:
        return None

    creds = local_provisioning_client_credentials_from_env()
    if creds is None:
        msg = (
            "MINDROOM_PROVISIONING_URL is set but local client credentials are missing. "
            "Run `mindroom connect --pair-code ...` first."
        )
        raise ValueError(msg)
    return creds


@dataclass(frozen=True)
class ProvisioningRegisterResult:
    """Result returned by the provisioning register-agent endpoint."""

    status: Literal["created", "user_in_use"]
    user_id: str


@dataclass(frozen=True)
class ProvisioningPairCompleteResult:
    """Credentials returned by the provisioning pair-complete endpoint."""

    client_id: str
    client_secret: str
    owner_user_id: str | None = None


def is_valid_pair_code(pair_code: str) -> bool:
    """Return True if pair_code has the expected ABCD-EFGH form."""
    return bool(_PAIR_CODE_RE.fullmatch(pair_code))


def complete_local_pairing(
    *,
    provisioning_url: str,
    pair_code: str,
    client_name: str,
    client_fingerprint: str,
    matrix_ssl_verify: bool,
    post_request: Callable[..., httpx.Response] = httpx.post,
) -> ProvisioningPairCompleteResult:
    """Call the provisioning API and return local client credentials."""
    payload = {
        "pair_code": pair_code,
        "client_name": client_name.strip(),
        "client_pubkey_or_fingerprint": client_fingerprint,
    }
    endpoint = f"{provisioning_url.rstrip('/')}/v1/local-mindroom/pair/complete"

    try:
        response = post_request(endpoint, json=payload, timeout=10, verify=matrix_ssl_verify)
    except httpx.HTTPError as exc:
        msg = f"Could not reach provisioning service: {exc}"
        raise ValueError(msg) from exc

    if not response.is_success:
        detail = _extract_error_detail(response)
        msg = f"Pairing failed ({response.status_code}): {detail}"
        raise ValueError(msg)

    try:
        data = response.json()
    except ValueError as exc:
        msg = "Provisioning service returned invalid JSON."
        raise ValueError(msg) from exc
    if not isinstance(data, dict):
        msg = "Provisioning service returned unexpected response."
        raise TypeError(msg)

    return ProvisioningPairCompleteResult(
        client_id=_required_non_empty_string(data, "client_id"),
        client_secret=_required_non_empty_string(data, "client_secret"),
        owner_user_id=_parse_owner_user_id(data.get("owner_user_id")),
    )


def persist_local_provisioning_env(
    *,
    provisioning_url: str,
    client_id: str,
    client_secret: str,
    owner_user_id: str | None = None,
    config_path: str | Path = CONFIG_PATH,
) -> Path:
    """Write local provisioning credentials to .env next to the active config file."""
    env_path = Path(config_path).expanduser().resolve().parent / ".env"
    env_path.parent.mkdir(parents=True, exist_ok=True)
    lines = env_path.read_text(encoding="utf-8").splitlines() if env_path.exists() else []

    updates = {
        "MINDROOM_PROVISIONING_URL": provisioning_url.rstrip("/"),
        "MINDROOM_LOCAL_CLIENT_ID": client_id,
        "MINDROOM_LOCAL_CLIENT_SECRET": client_secret,
    }
    if owner_user_id:
        updates["MINDROOM_OWNER_USER_ID"] = owner_user_id
    for key, value in updates.items():
        lines = _upsert_env_var(lines, key, value)

    env_path.write_text(f"{'\n'.join(lines)}\n", encoding="utf-8")
    return env_path


def replace_owner_placeholders_in_config(*, config_path: Path, owner_user_id: str) -> bool:
    """Replace owner placeholder tokens in config.yaml if they are still present."""
    if not _MATRIX_USER_ID_RE.fullmatch(owner_user_id):
        return False
    if not config_path.exists():
        return False

    content = config_path.read_text(encoding="utf-8")
    replaced = content.replace(OWNER_MATRIX_USER_ID_PLACEHOLDER, owner_user_id).replace(
        _LEGACY_OWNER_PLACEHOLDER,
        owner_user_id,
    )
    if replaced == content:
        return False

    config_path.write_text(replaced, encoding="utf-8")
    return True


async def register_user_via_provisioning_service(
    *,
    provisioning_url: str,
    client_id: str,
    client_secret: str,
    homeserver: str,
    username: str,
    password: str,
    display_name: str,
) -> ProvisioningRegisterResult:
    """Register an agent account via provisioning service server-side flow."""
    url = f"{provisioning_url}/v1/local-mindroom/register-agent"
    headers = {
        "X-Local-MindRoom-Client-Id": client_id,
        "X-Local-MindRoom-Client-Secret": client_secret,
    }
    payload = {
        "homeserver": homeserver.rstrip("/"),
        "username": username,
        "password": password,
        "display_name": display_name,
    }
    try:
        async with httpx.AsyncClient(timeout=10, verify=MATRIX_SSL_VERIFY) as client:
            response = await client.post(url, json=payload, headers=headers)
    except httpx.HTTPError as exc:
        msg = f"Could not reach provisioning service ({provisioning_url}): {exc}"
        raise ValueError(msg) from exc

    if not response.is_success:
        detail = response.text.strip() or "unknown error"
        if response.status_code in {401, 403}:
            msg = "Provisioning credentials are invalid or revoked. Run `mindroom connect --pair-code ...` again."
            raise ValueError(msg)
        if response.status_code == 404:
            msg = (
                "Provisioning service does not support /register-agent yet. "
                "Deploy the latest local provisioning service."
            )
            raise ValueError(msg)
        msg = f"Provisioning service returned HTTP {response.status_code}: {detail}"
        raise ValueError(msg)

    try:
        body = response.json()
    except ValueError as exc:
        msg = "Provisioning service returned invalid JSON while registering agent."
        raise ValueError(msg) from exc

    if not isinstance(body, dict):
        msg = "Provisioning service returned invalid register-agent payload."
        raise TypeError(msg)

    status = body.get("status")
    user_id = body.get("user_id")
    if status not in {"created", "user_in_use"}:
        msg = "Provisioning service response missing valid status for register-agent."
        raise ValueError(msg)
    if not isinstance(user_id, str) or not user_id.strip():
        msg = "Provisioning service response missing user_id for register-agent."
        raise ValueError(msg)

    return ProvisioningRegisterResult(status=status, user_id=user_id.strip())


def _required_non_empty_string(data: dict[str, object], key: str) -> str:
    """Read a required string field from a JSON dict."""
    raw_value = data.get(key)
    if isinstance(raw_value, str):
        value = raw_value.strip()
        if value:
            return value
    msg = f"Provisioning response missing {key}."
    raise ValueError(msg)


def _parse_owner_user_id(raw_value: object) -> str | None:
    """Parse optional owner_user_id from pairing response."""
    if not isinstance(raw_value, str):
        return None
    candidate_owner_user_id = raw_value.strip()
    if not candidate_owner_user_id:
        return None
    if _MATRIX_USER_ID_RE.fullmatch(candidate_owner_user_id):
        return candidate_owner_user_id
    return None


def _extract_error_detail(response: httpx.Response) -> str:
    """Extract a compact error detail from JSON or plaintext responses."""
    try:
        body = response.json()
    except ValueError:
        text = response.text.strip()
        return text or "unknown error"

    if isinstance(body, dict):
        detail = body.get("detail")
        if isinstance(detail, str):
            return detail
        if detail is not None:
            return str(detail)
    return "unknown error"


def _upsert_env_var(lines: list[str], key: str, value: str) -> list[str]:
    """Upsert a single KEY=value entry while preserving unrelated lines."""
    pattern = re.compile(rf"^\s*(?:export\s+)?{re.escape(key)}\s*=")
    for idx, line in enumerate(lines):
        if pattern.match(line):
            lines[idx] = f"{key}={value}"
            return lines
    lines.append(f"{key}={value}")
    return lines
