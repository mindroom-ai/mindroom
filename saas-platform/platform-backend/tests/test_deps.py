"""Test dependency injection utilities."""

from datetime import UTC, datetime, timedelta
from hashlib import sha256
from unittest.mock import MagicMock, Mock, patch

import jwt
import pytest
from fastapi import HTTPException
from fastapi.routing import APIRoute

from backend import auth_monitor
from backend.deps import (
    _auth_cache,
    invalidate_account_auth_cache,
    verify_admin,
    verify_user,
    verify_user_allow_deleted,
)
from backend.metrics import get_admin_metric, reset_security_metrics
from main import app


def _jwt_with_exp(expires_at: datetime) -> str:
    return jwt.encode({"sub": "user_123", "exp": int(expires_at.timestamp())}, "secret", algorithm="HS256")


class TestDeps:
    """Test dependency injection functions."""

    @pytest.fixture
    def mock_supabase(self):
        """Mock Supabase client."""
        with patch("backend.deps.ensure_supabase") as mock:
            sb = MagicMock()
            mock.return_value = sb
            yield sb

    @pytest.fixture
    def mock_auth_client(self):
        """Mock auth client."""
        with patch("backend.deps._ensure_auth_client") as mock:
            ac = MagicMock()
            mock.return_value = ac
            yield ac

    @pytest.fixture
    def mock_time(self):
        """Mock time for constant-time operations."""
        with patch("backend.deps.time") as mock:
            # Need enough values for all perf_counter calls
            mock.perf_counter.side_effect = [0.0, 0.001, 0.002, 0.003, 0.004]
            yield mock

    @pytest.mark.asyncio
    async def test_verify_user_success(self, mock_supabase: MagicMock, mock_auth_client: MagicMock, mock_time: Mock):
        """Test successful user verification."""
        from backend.deps import verify_user

        # Setup mock user
        mock_user = Mock()
        mock_user.user.id = "user_123"
        mock_user.user.email = "test@example.com"
        mock_user.user.user_metadata = {"full_name": "Test User"}
        mock_auth_client.auth.get_user.return_value = mock_user

        # Setup mock account
        mock_supabase.table().select().eq().single().execute.return_value = Mock(
            data={"id": "user_123", "email": "test@example.com", "status": "active", "deleted_at": None}
        )

        # Test
        result = await verify_user("Bearer test-token")

        # Verify
        assert result["user_id"] == "user_123"
        assert result["account_id"] == "user_123"
        assert result["email"] == "test@example.com"

    @pytest.mark.asyncio
    async def test_verify_user_invalid_token(self, mock_auth_client: MagicMock, mock_time: Mock):
        """Test user verification with invalid token."""
        from backend.deps import verify_user

        # Setup invalid token
        mock_auth_client.auth.get_user.return_value = None

        # Test
        with pytest.raises(HTTPException) as exc_info:
            await verify_user("Bearer invalid-token")

        assert exc_info.value.status_code == 401
        assert exc_info.value.detail == "Invalid token"

    @pytest.mark.asyncio
    async def test_verify_user_creates_account(
        self, mock_supabase: MagicMock, mock_auth_client: MagicMock, mock_time: Mock
    ):
        """Test user verification creates account if not exists."""
        from backend.deps import _auth_cache, verify_user

        _auth_cache.clear()
        token = _jwt_with_exp(datetime.now(UTC) + timedelta(minutes=5))

        # Setup mock user
        mock_user = Mock()
        mock_user.user.id = "new_user_123"
        mock_user.user.email = "new@example.com"
        mock_user.user.user_metadata = {"full_name": "New User"}
        mock_auth_client.auth.get_user.return_value = mock_user

        # The select finds no account, so verify_user inserts one
        mock_supabase.table().select().eq().single().execute.side_effect = Exception("Not found")

        # Inserts return the created rows, including column defaults
        mock_supabase.table().insert().execute.return_value = Mock(
            data=[{"id": "new_user_123", "email": "new@example.com", "status": "active", "deleted_at": None}]
        )

        # Test
        result = await verify_user(f"Bearer {token}")

        # Verify
        assert result["user_id"] == "new_user_123"
        assert result["account_id"] == "new_user_123"
        assert result["account"]["status"] == "active"

        # Verify insert was called
        insert_call = mock_supabase.table().insert.call_args[0][0]
        assert insert_call["id"] == "new_user_123"
        assert insert_call["email"] == "new@example.com"
        assert sha256(token.encode()).hexdigest() in _auth_cache
        _auth_cache.clear()

    @pytest.mark.asyncio
    async def test_verify_user_cache_hit(self, mock_supabase: MagicMock, mock_auth_client: MagicMock):
        """Test user verification uses cache."""
        from backend.deps import AuthCacheEntry, _auth_cache, verify_user

        # Pre-populate cache
        token = _jwt_with_exp(datetime.now(UTC) + timedelta(minutes=5))
        _auth_cache[sha256(token.encode()).hexdigest()] = AuthCacheEntry(
            expires_at=datetime.now(UTC) + timedelta(minutes=5),
            account_id="cached_user",
            user_data={
                "user_id": "cached_user",
                "account_id": "cached_user",
                "email": "cached@example.com",
                "account": {"id": "cached_user", "status": "active", "deleted_at": None},
            },
        )

        # Test
        result = await verify_user(f"Bearer {token}")

        # Verify
        assert result["user_id"] == "cached_user"

        # Auth client should not be called for cached token
        mock_auth_client.auth.get_user.assert_not_called()

        # Clean up cache
        _auth_cache.clear()

    @pytest.mark.asyncio
    async def test_verify_user_cache_key_is_token_hash(self, mock_supabase: MagicMock, mock_auth_client: MagicMock):
        """Auth cache must not store raw bearer tokens as keys."""
        from backend.deps import _auth_cache, verify_user

        _auth_cache.clear()
        token = _jwt_with_exp(datetime.now(UTC) + timedelta(minutes=5))
        mock_user = Mock()
        mock_user.user.id = "user_123"
        mock_user.user.email = "test@example.com"
        mock_user.user.user_metadata = {"full_name": "Test User"}
        mock_auth_client.auth.get_user.return_value = mock_user
        mock_supabase.table().select().eq().single().execute.return_value = Mock(
            data={"id": "user_123", "email": "test@example.com", "status": "active", "deleted_at": None}
        )

        await verify_user(f"Bearer {token}")

        assert token not in _auth_cache
        assert sha256(token.encode()).hexdigest() in _auth_cache
        _auth_cache.clear()

    @pytest.mark.asyncio
    async def test_verify_user_cache_deadline_is_bounded_by_token_exp(
        self, mock_supabase: MagicMock, mock_auth_client: MagicMock
    ):
        """Auth cache entries must expire no later than the JWT exp claim."""
        from backend.deps import _auth_cache, verify_user

        _auth_cache.clear()
        expires_at = datetime.now(UTC) + timedelta(seconds=30)
        token = _jwt_with_exp(expires_at)
        mock_user = Mock()
        mock_user.user.id = "user_123"
        mock_user.user.email = "test@example.com"
        mock_user.user.user_metadata = {"full_name": "Test User"}
        mock_auth_client.auth.get_user.return_value = mock_user
        mock_supabase.table().select().eq().single().execute.return_value = Mock(
            data={"id": "user_123", "email": "test@example.com", "status": "active", "deleted_at": None}
        )

        await verify_user(f"Bearer {token}")

        entry = _auth_cache[sha256(token.encode()).hexdigest()]
        assert entry.expires_at <= expires_at
        assert entry.expires_at > datetime.now(UTC)
        _auth_cache.clear()

    @pytest.mark.asyncio
    async def test_verify_user_cache_does_not_share_mutable_user_data(
        self, mock_supabase: MagicMock, mock_auth_client: MagicMock
    ):
        """Request handlers must not be able to mutate cached auth data."""
        from backend.deps import _auth_cache, verify_user

        _auth_cache.clear()
        token = _jwt_with_exp(datetime.now(UTC) + timedelta(minutes=5))
        mock_user = Mock()
        mock_user.user.id = "user_123"
        mock_user.user.email = "test@example.com"
        mock_user.user.user_metadata = {"full_name": "Test User"}
        mock_auth_client.auth.get_user.return_value = mock_user
        mock_supabase.table().select().eq().single().execute.return_value = Mock(
            data={"id": "user_123", "email": "test@example.com", "status": "active", "deleted_at": None}
        )

        result = await verify_user(f"Bearer {token}")
        result["email"] = "mutated@example.com"
        result["account"]["email"] = "mutated@example.com"
        mock_auth_client.auth.get_user.reset_mock()

        cached_result = await verify_user(f"Bearer {token}")

        assert cached_result["email"] == "test@example.com"
        assert cached_result["account"]["email"] == "test@example.com"
        mock_auth_client.auth.get_user.assert_not_called()
        _auth_cache.clear()

    @pytest.mark.asyncio
    async def test_verify_user_does_not_accept_expired_cached_token(self, mock_auth_client: MagicMock):
        """Expired JWTs must not be accepted through a stale auth cache hit."""
        from backend.deps import AuthCacheEntry, _auth_cache, verify_user

        _auth_cache.clear()
        token = _jwt_with_exp(datetime.now(UTC) - timedelta(seconds=1))
        cache_key = sha256(token.encode()).hexdigest()
        _auth_cache[cache_key] = AuthCacheEntry(
            expires_at=datetime.now(UTC) - timedelta(seconds=1),
            account_id="cached_user",
            user_data={
                "user_id": "cached_user",
                "account_id": "cached_user",
                "email": "cached@example.com",
                "account": {"id": "cached_user", "status": "active", "deleted_at": None},
            },
        )
        mock_auth_client.auth.get_user.return_value = None

        with pytest.raises(HTTPException) as exc_info:
            await verify_user(f"Bearer {token}")

        assert exc_info.value.status_code == 401
        assert cache_key not in _auth_cache
        _auth_cache.clear()

    @pytest.mark.asyncio
    async def test_verify_user_missing_bearer(self):
        """Test user verification with missing Bearer prefix."""
        from backend.deps import verify_user

        with pytest.raises(HTTPException) as exc_info:
            await verify_user("invalid-format-token")

        assert exc_info.value.status_code == 401
        assert "Invalid authorization format" in exc_info.value.detail

    @pytest.mark.asyncio
    async def test_verify_user_auth_failures(self, mock_auth_client: MagicMock):
        """Test that auth failures are properly handled."""
        from backend.deps import verify_user

        # Setup invalid token
        mock_auth_client.auth.get_user.return_value = None

        # Test
        with pytest.raises(HTTPException) as exc_info:
            await verify_user("Bearer invalid")

        assert exc_info.value.status_code == 401
        assert exc_info.value.detail == "Invalid token"

    @pytest.mark.asyncio
    async def test_verify_admin_success(self, mock_supabase: MagicMock, mock_auth_client: MagicMock):
        """Test successful admin verification."""
        from backend.deps import verify_admin

        reset_security_metrics()

        # Setup mock admin user
        mock_user = Mock()
        mock_user.user.id = "admin_123"
        mock_user.user.email = "admin@example.com"
        mock_user.user.user_metadata = {"full_name": "Admin User"}
        mock_auth_client.auth.get_user.return_value = mock_user

        # Setup mock account with is_admin=True
        mock_supabase.table().select().eq().single().execute.return_value = Mock(
            data={
                "id": "admin_123",
                "email": "admin@example.com",
                "is_admin": True,
                "status": "active",
                "deleted_at": None,
            }
        )

        # Test
        result = await verify_admin("Bearer admin-token")

        # Verify
        assert result["user_id"] == "admin_123"
        assert result["email"] == "admin@example.com"
        assert get_admin_metric("success") == 1

    @pytest.mark.asyncio
    async def test_verify_admin_not_admin(self, mock_supabase: MagicMock, mock_auth_client: MagicMock):
        """Test admin verification fails for non-admin user."""
        from backend.deps import verify_admin

        reset_security_metrics()

        # Setup mock regular user
        mock_user = Mock()
        mock_user.user.id = "user_123"
        mock_user.user.email = "user@example.com"
        mock_user.user.user_metadata = {}
        mock_auth_client.auth.get_user.return_value = mock_user

        # Setup mock account with is_admin=False
        mock_supabase.table().select().eq().single().execute.return_value = Mock(
            data={"id": "user_123", "email": "user@example.com", "is_admin": False}
        )

        # Test
        with pytest.raises(HTTPException) as exc_info:
            await verify_admin("Bearer user-token")

        assert exc_info.value.status_code == 403
        assert exc_info.value.detail == "Admin access required"
        assert get_admin_metric("forbidden") == 1

    @pytest.mark.asyncio
    async def test_verify_admin_invalid_header(self):
        """Test admin verification with malformed authorization header."""
        from backend.deps import verify_admin

        reset_security_metrics()

        with pytest.raises(HTTPException) as exc_info:
            await verify_admin("invalid")

        assert exc_info.value.status_code == 401
        assert get_admin_metric("unauthorized") == 1

    def test_ensure_supabase(self):
        """Test ensure_supabase returns client."""
        from backend.deps import ensure_supabase

        # Should return the global supabase client
        with patch("backend.deps.supabase") as mock_sb:
            mock_sb.return_value = "test_client"
            result = ensure_supabase()
            assert result == mock_sb

    def test_ensure_supabase_raises_when_none(self):
        """Test ensure_supabase raises when client is None."""
        from backend.deps import ensure_supabase

        with patch("backend.deps.supabase", None):
            with pytest.raises(HTTPException) as exc_info:
                ensure_supabase()
            assert exc_info.value.status_code == 500
            assert "Supabase not configured" in exc_info.value.detail

    def test_limiter_instance(self):
        """Test limiter is properly initialized."""
        from backend.deps import limiter

        assert limiter is not None
        # Limiter should be a Limiter instance
        assert hasattr(limiter, "limit")


def _auth_user(user_id: str = "user_123", email: str = "user@example.test") -> Mock:
    auth_user = Mock()
    auth_user.user.id = user_id
    auth_user.user.email = email
    auth_user.user.user_metadata = {}
    return auth_user


def _account_row(**overrides: object) -> dict[str, object]:
    return {
        "id": "user_123",
        "email": "user@example.test",
        "is_admin": False,
        "status": "active",
        "deleted_at": None,
    } | overrides


@pytest.fixture
def auth_backend():
    """Patch the Supabase auth and database clients used by the auth dependencies."""
    _auth_cache.clear()
    with (
        patch.dict(auth_monitor.failed_attempts, clear=True),
        patch.dict(auth_monitor.blocked_ips, clear=True),
        patch("backend.deps._ensure_auth_client") as ensure_auth_client,
        patch("backend.deps.ensure_supabase") as ensure_supabase,
    ):
        auth_client = MagicMock()
        auth_client.auth.get_user.return_value = _auth_user()
        ensure_auth_client.return_value = auth_client
        sb = MagicMock()
        ensure_supabase.return_value = sb
        yield auth_client, sb.table().select().eq().single().execute
    _auth_cache.clear()


INACTIVE_ACCOUNTS = [
    pytest.param(_account_row(status="suspended"), id="suspended"),
    pytest.param(_account_row(status="pending_verification"), id="pending-verification"),
    pytest.param(_account_row(status="deleted", deleted_at="2026-09-01T00:00:00Z"), id="soft-deleted"),
    pytest.param(_account_row(deleted_at="2026-09-01T00:00:00Z"), id="active-with-deleted-at"),
    pytest.param(_account_row(status=None), id="missing-status"),
]


@pytest.mark.asyncio
@pytest.mark.parametrize("account", INACTIVE_ACCOUNTS)
async def test_verify_user_rejects_inactive_accounts(auth_backend, account: dict[str, object]) -> None:
    """Suspended, unverified, and deleted accounts must not pass the regular user gate."""
    _auth_client, account_query = auth_backend
    account_query.return_value = Mock(data=account)
    token = _jwt_with_exp(datetime.now(UTC) + timedelta(minutes=5))

    with pytest.raises(HTTPException) as exc_info:
        await verify_user(f"Bearer {token}")

    assert exc_info.value.status_code == 403
    assert not _auth_cache


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "account",
    [
        pytest.param(_account_row(), id="active"),
        pytest.param(_account_row(status="deleted", deleted_at="2026-09-01T00:00:00Z"), id="soft-deleted"),
        pytest.param(_account_row(deleted_at="2026-09-01T00:00:00Z"), id="active-with-deleted-at"),
    ],
)
async def test_verify_user_allow_deleted_admits_accounts_pending_deletion(
    auth_backend, account: dict[str, object]
) -> None:
    """Accounts awaiting deletion can still reach the GDPR self-service routes."""
    _auth_client, account_query = auth_backend
    account_query.return_value = Mock(data=account)

    result = await verify_user_allow_deleted("Bearer token")

    assert result["account_id"] == "user_123"


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "account",
    [
        pytest.param(_account_row(status="suspended"), id="suspended"),
        pytest.param(_account_row(status="suspended", deleted_at="2026-09-01T00:00:00Z"), id="suspended-and-deleted"),
        pytest.param(_account_row(status="pending_verification"), id="pending-verification"),
    ],
)
async def test_verify_user_allow_deleted_still_rejects_blocked_accounts(
    auth_backend, account: dict[str, object]
) -> None:
    """A suspended account cannot use the deletion allowance, for example to restore itself."""
    _auth_client, account_query = auth_backend
    account_query.return_value = Mock(data=account)

    with pytest.raises(HTTPException) as exc_info:
        await verify_user_allow_deleted("Bearer token")

    assert exc_info.value.status_code == 403


@pytest.mark.asyncio
async def test_account_status_change_takes_effect_on_the_next_cached_request(auth_backend) -> None:
    """Invalidating an account drops its cached auth so a suspension applies immediately."""
    auth_client, account_query = auth_backend
    account_query.return_value = Mock(data=_account_row())
    token = _jwt_with_exp(datetime.now(UTC) + timedelta(minutes=5))
    await verify_user(f"Bearer {token}")

    account_query.return_value = Mock(data=_account_row(status="suspended"))
    invalidate_account_auth_cache("user_123")

    with pytest.raises(HTTPException) as exc_info:
        await verify_user(f"Bearer {token}")
    assert exc_info.value.status_code == 403
    assert auth_client.auth.get_user.call_count == 2


@pytest.mark.asyncio
async def test_restored_account_is_admitted_without_waiting_for_the_cache(auth_backend) -> None:
    """Non-active snapshots are never cached, so cancelling deletion restores access at once."""
    _auth_client, account_query = auth_backend
    account_query.return_value = Mock(data=_account_row(status="deleted", deleted_at="2026-09-01T00:00:00Z"))
    token = _jwt_with_exp(datetime.now(UTC) + timedelta(minutes=5))
    await verify_user_allow_deleted(f"Bearer {token}")

    account_query.return_value = Mock(data=_account_row())

    result = await verify_user(f"Bearer {token}")
    assert result["account"]["status"] == "active"


@pytest.mark.asyncio
@pytest.mark.parametrize("account", INACTIVE_ACCOUNTS)
async def test_verify_admin_rejects_inactive_admin_accounts(auth_backend, account: dict[str, object]) -> None:
    """A suspended or deleted administrator loses admin access."""
    reset_security_metrics()
    _auth_client, account_query = auth_backend
    account_query.return_value = Mock(data=account | {"is_admin": True})

    with pytest.raises(HTTPException) as exc_info:
        await verify_admin("Bearer admin-token")

    assert exc_info.value.status_code == 403
    assert get_admin_metric("forbidden") == 1


def _route_auth_dependencies() -> dict[tuple[str, str], set[object]]:

    auth_dependencies = {verify_user, verify_user_allow_deleted, verify_admin}
    routes: dict[tuple[str, str], set[object]] = {}
    for route in app.routes:
        if not isinstance(route, APIRoute):
            continue
        calls = {dependency.call for dependency in route.dependant.dependencies} & auth_dependencies
        for method in route.methods:
            routes[(method, route.path)] = calls
    return routes


def test_only_gdpr_self_service_routes_admit_accounts_pending_deletion() -> None:
    """The deletion allowance is limited to reading the account, exporting data, and cancelling deletion."""
    allowed = {route for route, calls in _route_auth_dependencies().items() if verify_user_allow_deleted in calls}

    assert allowed == {
        ("GET", "/my/account"),
        ("GET", "/my/gdpr/export-data"),
        ("POST", "/my/gdpr/cancel-deletion"),
    }
