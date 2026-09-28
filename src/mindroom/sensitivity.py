"""Shared secret-name stems for worker isolation.

Public worker startup-env filtering keeps secret env vars out of worker manifests
(:mod:`mindroom.runtime_env_policy`) by name, so a new secret pattern only has to
be added here. The call site layers its own extras on top of the shared core
(``runtime_env_policy`` also treats ``_API_KEYS`` / ``_DATABASE_URL`` as secret).
Worker-visible config is not filtered by name: runners and workers receive only
the allowlisted fields in :mod:`mindroom.config.worker_projection`.
"""

from __future__ import annotations

# Core secret-name stems shared by every sensitive-name check.
_SECRET_NAME_STEMS: tuple[str, ...] = ("api_key", "password", "secret", "token")


def secret_name_suffixes(
    *,
    stems: tuple[str, ...] = _SECRET_NAME_STEMS,
    upper: bool = False,
    file: bool = False,
) -> tuple[str, ...]:
    """Return ``_<stem>`` secret suffixes, optionally upper-cased and ``_FILE``-suffixed.

    Call sites derive their concrete suffix tuples from the shared stems so the
    common core (``api_key`` / ``password`` / ``secret`` / ``token``) cannot drift
    between them.
    """
    suffixes: list[str] = []
    for stem in stems:
        token = stem.upper() if upper else stem
        suffixes.append(f"_{token}_FILE" if file else f"_{token}")
    return tuple(suffixes)
