# ruff: noqa: INP001
"""Helpers for CLI `connect` command and local onboarding config updates."""

from __future__ import annotations

import re
from dataclasses import dataclass
from hashlib import sha256
from pathlib import Path
from typing import TYPE_CHECKING

import httpx

from mindroom.constants import CONFIG_PATH, OWNER_MATRIX_USER_ID_PLACEHOLDER

if TYPE_CHECKING:
    from collections.abc import Callable

_PAIR_CODE_RE = re.compile(r"^[A-Z0-9]{4}-[A-Z0-9]{4}$")
_MATRIX_USER_ID_RE = re.compile(r"^@[^:\s]+:[^\s]+$")
_NAMESPACE_RE = re.compile(r"^[a-z0-9]{4,32}$")
_LEGACY_OWNER_PLACEHOLDER = "__PLACEHOLDER__"


@dataclass(frozen=True)
class PairCompleteResult:
    """Credentials returned by the provisioning pair-complete endpoint."""

    client_id: str
    client_secret: str
    namespace: str
    owner_user_id: str | None = None
    namespace_invalid: bool = False
    owner_user_id_invalid: bool = False


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
) -> PairCompleteResult:
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

    raw_owner_user_id = data.get("owner_user_id")
    parsed_owner_user_id = _parse_owner_user_id(raw_owner_user_id)
    owner_user_id_invalid = (
        isinstance(raw_owner_user_id, str) and bool(raw_owner_user_id.strip()) and parsed_owner_user_id is None
    )
    client_id = _required_non_empty_string(data, "client_id")
    raw_namespace = data.get("namespace")
    parsed_namespace = _parse_namespace(raw_namespace)
    namespace_invalid = isinstance(raw_namespace, str) and bool(raw_namespace.strip()) and parsed_namespace is None
    if parsed_namespace is None:
        parsed_namespace = _derive_namespace(client_id)

    return PairCompleteResult(
        client_id=client_id,
        client_secret=_required_non_empty_string(data, "client_secret"),
        namespace=parsed_namespace,
        owner_user_id=parsed_owner_user_id,
        namespace_invalid=namespace_invalid,
        owner_user_id_invalid=owner_user_id_invalid,
    )


def persist_local_provisioning_env(
    *,
    provisioning_url: str,
    client_id: str,
    client_secret: str,
    namespace: str,
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
        "MINDROOM_NAMESPACE": namespace,
    }
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


def _derive_namespace(seed: str) -> str:
    """Derive a deterministic namespace from an identifier."""
    return sha256(seed.encode("utf-8")).hexdigest()[:8]


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
