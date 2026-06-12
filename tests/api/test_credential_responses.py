"""Tests for credential response security filtering."""

from mindroom.api.credential_responses import filter_credentials_for_response, filter_oauth_client_config_for_response


def test_plain_credentials_drop_internal_keys_only() -> None:
    """Plain service responses keep fields but drop internal metadata keys."""
    filtered = filter_credentials_for_response(
        {"api_key": "sk-plain", "region": "us-east-1", "_source": "ui"},
        is_oauth_service=False,
    )
    assert filtered == {"api_key": "sk-plain", "region": "us-east-1"}


def test_oauth_service_credentials_never_expose_token_material() -> None:
    """OAuth service responses must never include token or secret material."""
    credentials = {
        "access_token": "secret-access-token",
        "refresh_token": "secret-refresh-token",
        "id_token": "secret-id-token",
        "client_id": "client-id",
        "client_secret": "secret-client-secret",
        "scope": "drive",
        "email": "alice@example.org",
        "_source": "oauth",
        "_oauth_provider": "google",
    }
    filtered = filter_credentials_for_response(credentials, is_oauth_service=True)

    for secret in ("secret-access-token", "secret-refresh-token", "secret-id-token", "secret-client-secret"):
        assert secret not in str(filtered)
    assert "access_token" not in filtered
    assert "refresh_token" not in filtered
    assert "client_secret" not in filtered
    assert not any(key.startswith("_") for key in filtered)


def test_oauth_looking_credentials_are_filtered_even_for_non_oauth_services() -> None:
    """OAuth-looking documents are filtered even when the service is not registered as OAuth."""
    credentials = {
        "access_token": "secret-access-token",
        "refresh_token": "secret-refresh-token",
        "_oauth_provider": "google",
        "email": "alice@example.org",
    }
    filtered = filter_credentials_for_response(credentials, is_oauth_service=False)

    assert "secret-access-token" not in str(filtered)
    assert "secret-refresh-token" not in str(filtered)
    assert filtered == {"email": "alice@example.org"}


def test_oauth_client_config_response_never_contains_client_secret() -> None:
    """Client config responses must never include the client secret."""
    filtered = filter_oauth_client_config_for_response(
        {
            "client_id": "client-id",
            "client_secret": "secret-client-secret",
            "redirect_uri": "https://example.org/callback",
            "_source": "ui",
        },
    )
    assert filtered == {"client_id": "client-id", "redirect_uri": "https://example.org/callback"}
    assert "secret-client-secret" not in str(filtered)
