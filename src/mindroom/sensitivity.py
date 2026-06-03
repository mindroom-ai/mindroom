"""Single source of truth for secret-name classification across worker isolation.

Several worker-isolation paths must decide whether a *name* denotes a secret:

- projected-config redaction strips sensitive ``config.yaml`` keys from the
  worker-visible snapshot (:mod:`mindroom.workers.backends.docker_projection`);
- public worker startup-env filtering keeps secret env vars out of worker
  manifests (:mod:`mindroom.runtime_env_policy`);
- file-secret handling routes ``*_FILE`` env vars that point at secret files
  (:mod:`mindroom.workers.backends._dedicated_worker_common`).

They share the same core secret-name *stems*, so a new secret pattern only has to
be added here. Each call site layers its own extras on top of the shared core
(``runtime_env_policy`` also treats ``_API_KEYS`` / ``_DATABASE_URL`` as secret;
the file-secret path also recognises credential and service-account files).
"""

from __future__ import annotations

import re

# Core secret-name stems shared by every sensitive-name check.
_SECRET_NAME_STEMS: tuple[str, ...] = ("api_key", "password", "secret", "token")

# Config keys whose value is always a secret regardless of suffix.
_SENSITIVE_CONFIG_KEYS = frozenset(
    {
        "access_token",
        "api_key",
        "client_secret",
        "long_lived_token",
        "password",
        "private_key",
        "refresh_token",
        "secret",
        "token",
    },
)

# Keys that end in a secret stem but are not actually secrets.
_NON_SECRET_CONFIG_KEY_EXCEPTIONS = frozenset({"no_reply_token", "token_uri"})

# HTTP header names whose value always carries a credential.
_SENSITIVE_HEADER_KEYS = frozenset({"authorization", "proxy_authorization"})


def normalize_config_key(raw_key: str) -> str:
    """Normalize a config key to lowercase-underscore form for classification."""
    return re.sub(r"[^a-z0-9]+", "_", raw_key.strip().lower()).strip("_")


def secret_name_suffixes(
    *,
    stems: tuple[str, ...] = _SECRET_NAME_STEMS,
    upper: bool = False,
    file: bool = False,
) -> tuple[str, ...]:
    """Return ``_<stem>`` secret suffixes, optionally upper-cased and ``_FILE``-suffixed.

    Call sites derive their concrete suffix tuples from the shared stems so the
    common core (``api_key`` / ``password`` / ``secret`` / ``token``) cannot drift
    between the redaction, env-filtering, and file-secret paths.
    """
    suffixes: list[str] = []
    for stem in stems:
        token = stem.upper() if upper else stem
        suffixes.append(f"_{token}_FILE" if file else f"_{token}")
    return tuple(suffixes)


def is_sensitive_config_key(raw_key: str) -> bool:
    """Return whether a config key's value should be redacted from worker snapshots."""
    normalized_key = normalize_config_key(raw_key)
    if normalized_key in _NON_SECRET_CONFIG_KEY_EXCEPTIONS:
        return False
    if normalized_key in _SENSITIVE_CONFIG_KEYS:
        return True
    return normalized_key.endswith(secret_name_suffixes())


def is_sensitive_header_key(raw_key: str) -> bool:
    """Return whether an HTTP header name carries a credential to be redacted."""
    return normalize_config_key(raw_key) in _SENSITIVE_HEADER_KEYS or is_sensitive_config_key(raw_key)
