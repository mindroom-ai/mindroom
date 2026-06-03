"""Tests for the shared secret-name classification module.

These pin the classification rules and guard that the suffix tuples derived for
each call site stay set-equal to the historical hardcoded sets, so consolidating
the three sites onto the shared core did not change behavior.
"""

from __future__ import annotations

import pytest

from mindroom import sensitivity


@pytest.mark.parametrize(
    "key",
    [
        "api_key",
        "API-Key",
        "openai_api_key",
        "password",
        "PASSWORD",
        "secret",
        "client_secret",
        "access_token",
        "refresh_token",
        "long_lived_token",
        "private_key",
        "my_token",
        "service.password",
    ],
)
def test_is_sensitive_config_key_flags_secrets(key: str) -> None:
    """Secret-bearing config keys are classified sensitive across case/separators."""
    assert sensitivity.is_sensitive_config_key(key) is True


@pytest.mark.parametrize(
    "key",
    ["no_reply_token", "token_uri", "model", "id", "base_url", "username", "display_name"],
)
def test_is_sensitive_config_key_allows_non_secrets(key: str) -> None:
    """Non-secret keys, including the documented exceptions, are not redacted."""
    assert sensitivity.is_sensitive_config_key(key) is False


def test_config_secret_suffixes_match_historical_set() -> None:
    """The projected-config redaction suffixes must stay set-equal to the original."""
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
