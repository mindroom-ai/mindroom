"""Test dependency injection utilities."""

import base64
import hashlib
import json
from datetime import UTC, datetime, timedelta
from unittest.mock import MagicMock, Mock, patch

import pytest
from fastapi import HTTPException

from backend.metrics import get_admin_metric, reset_security_metrics


class TestDeps:
    """Test dependency injection functions."""

    @pytest.fixture(autouse=True)
    def clear_auth_cache(self):
        """Clear auth cache between tests."""
        from backend.deps import _auth_cache

        _auth_cache.clear()
        yield
        _auth_cache.clear()

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

    @staticmethod
    def _jwt_with_exp(expires_at: datetime) -> str:
        """Build an unsigned JWT-like token for cache behavior tests."""

        def b64url(data: dict) -> str:
            raw = json.dumps(data, separators=(",", ":")).encode("utf-8")
            return base64.urlsafe_b64encode(raw).rstrip(b"=").decode("ascii")

        return f"{b64url({'alg': 'none'})}.{b64url({'exp': int(expires_at.timestamp())})}.sig"

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
            data={"id": "user_123", "email": "test@example.com"}
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
        from backend.deps import verify_user

        # Setup mock user
        mock_user = Mock()
        mock_user.user.id = "new_user_123"
        mock_user.user.email = "new@example.com"
        mock_user.user.user_metadata = {"full_name": "New User"}
        mock_auth_client.auth.get_user.return_value = mock_user

        # First select returns no data (account doesn't exist)
        mock_supabase.table().select().eq().single().execute.side_effect = [
            Exception("Not found"),  # First check fails
            Mock(data={"id": "new_user_123", "email": "new@example.com"}),  # After insert
        ]

        # Mock insert
        mock_supabase.table().insert().execute.return_value = Mock(data={"id": "new_user_123"})

        # Test
        result = await verify_user("Bearer new-user-token")

        # Verify
        assert result["user_id"] == "new_user_123"
        assert result["account_id"] == "new_user_123"

        # Verify insert was called
        insert_call = mock_supabase.table().insert.call_args[0][0]
        assert insert_call["id"] == "new_user_123"
        assert insert_call["email"] == "new@example.com"

    @pytest.mark.asyncio
    async def test_verify_user_cache_hit(self, mock_supabase: MagicMock, mock_auth_client: MagicMock):
        """Test user verification uses cache."""
        from backend.deps import verify_user

        token = self._jwt_with_exp(datetime.now(UTC) + timedelta(minutes=5))
        mock_user = Mock()
        mock_user.user.id = "cached_user"
        mock_user.user.email = "cached@example.com"
        mock_user.user.user_metadata = {}
        mock_auth_client.auth.get_user.return_value = mock_user
        mock_supabase.table().select().eq().single().execute.return_value = Mock(
            data={"id": "cached_user", "email": "cached@example.com"}
        )

        result = await verify_user(f"Bearer {token}")
        cached_result = await verify_user(f"Bearer {token}")

        assert result["user_id"] == "cached_user"
        assert cached_result["user_id"] == "cached_user"
        mock_auth_client.auth.get_user.assert_called_once_with(token)

    @pytest.mark.asyncio
    async def test_verify_user_hashes_auth_cache_keys(self, mock_supabase: MagicMock, mock_auth_client: MagicMock):
        """Auth cache keys should not contain raw bearer tokens."""
        from backend.deps import _auth_cache, verify_user

        token = self._jwt_with_exp(datetime.now(UTC) + timedelta(minutes=5))
        mock_user = Mock()
        mock_user.user.id = "user_123"
        mock_user.user.email = "test@example.com"
        mock_user.user.user_metadata = {}
        mock_auth_client.auth.get_user.return_value = mock_user
        mock_supabase.table().select().eq().single().execute.return_value = Mock(
            data={"id": "user_123", "email": "test@example.com"}
        )

        await verify_user(f"Bearer {token}")

        assert token not in _auth_cache
        assert hashlib.sha256(token.encode("utf-8")).hexdigest() in _auth_cache

    @pytest.mark.asyncio
    async def test_verify_user_does_not_cache_past_jwt_exp(self, mock_supabase: MagicMock, mock_auth_client: MagicMock):
        """Auth cache should not reuse entries beyond JWT exp."""
        from backend.deps import verify_user

        token = self._jwt_with_exp(datetime.now(UTC) - timedelta(minutes=1))
        mock_user = Mock()
        mock_user.user.id = "user_123"
        mock_user.user.email = "test@example.com"
        mock_user.user.user_metadata = {}
        mock_auth_client.auth.get_user.return_value = mock_user
        mock_supabase.table().select().eq().single().execute.return_value = Mock(
            data={"id": "user_123", "email": "test@example.com"}
        )

        await verify_user(f"Bearer {token}")
        await verify_user(f"Bearer {token}")

        assert mock_auth_client.auth.get_user.call_count == 2

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
            data={"id": "admin_123", "email": "admin@example.com", "is_admin": True}
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
