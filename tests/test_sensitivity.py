"""Tests for the shared secret-name stems.

These guard that the suffix tuples derived from the shared stems stay set-equal
to the historical hardcoded sets.
"""

from __future__ import annotations

from mindroom import sensitivity


def test_default_secret_suffixes_match_historical_set() -> None:
    """The default lowercase suffixes must stay set-equal to the original."""
    assert set(sensitivity.secret_name_suffixes()) == {"_api_key", "_password", "_secret", "_token"}


def test_runtime_startup_secret_suffixes_match_historical_set() -> None:
    """runtime_env_policy startup-secret suffixes (shared core + `_API_KEYS`)."""
    derived = {*sensitivity.secret_name_suffixes(upper=True), "_API_KEYS"}
    assert derived == {"_API_KEY", "_API_KEYS", "_PASSWORD", "_SECRET", "_TOKEN"}


def test_file_secret_suffixes_match_historical_set() -> None:
    """File-secret `*_FILE` suffixes (shared core + credential/service-account files)."""
    derived = {
        *sensitivity.secret_name_suffixes(upper=True, file=True),
        "_CREDENTIAL_FILE",
        "_CREDENTIALS_FILE",
        "_SERVICE_ACCOUNT_FILE",
    }
    assert derived == {
        "_API_KEY_FILE",
        "_CREDENTIAL_FILE",
        "_CREDENTIALS_FILE",
        "_PASSWORD_FILE",
        "_SECRET_FILE",
        "_SERVICE_ACCOUNT_FILE",
        "_TOKEN_FILE",
    }
