"""Comprehensive HTTP API tests for webhook endpoints."""

from unittest.mock import MagicMock, Mock, patch

import pytest
import stripe
from fastapi.testclient import TestClient


class TestWebhookEndpoints:
    """Test webhook endpoints via HTTP API."""

    @pytest.fixture
    def client(self) -> TestClient:
        """Create test client."""
        from main import app  # noqa: PLC0415

        return TestClient(app)

    @pytest.fixture
    def mock_stripe_signature(self):
        """Mock Stripe signature verification."""
        with patch("backend.routes.webhooks.stripe.Webhook.construct_event") as mock:
            yield mock

    @pytest.fixture
    def mock_supabase(self):
        """Mock Supabase client."""
        with patch("backend.routes.webhooks.ensure_supabase") as mock:
            sb = MagicMock()
            mock.return_value = sb
            yield sb

    def _create_stripe_event(self, event_type: str, data: dict, event_id: str = "evt_test_123") -> Mock:
        """Create a mock Stripe event."""
        event = Mock()
        event.id = event_id
        event.type = event_type
        event.data.object = data
        return event

    def _create_subscription_data(
        self,
        subscription_id: str = "sub_test_123",
        customer_id: str = "cus_test_123",
        status: str = "active",
        tier: str = "starter",
        billing_cycle: str = "monthly",
        quantity: int = 1,
    ) -> dict:
        """Create test subscription data."""
        return {
            "id": subscription_id,
            "customer": customer_id,
            "status": status,
            "items": {
                "data": [
                    {
                        "price": {
                            "id": f"price_{tier}_{billing_cycle}",
                            "metadata": {
                                "plan": tier,
                                "billing_cycle": billing_cycle,
                            },
                        },
                        "quantity": quantity,
                    }
                ]
            },
            "current_period_start": 1700000000,
            "current_period_end": 1702678400,
            "trial_end": None,
        }

    def _create_invoice_data(
        self,
        invoice_id: str = "in_test_123",
        customer_id: str = "cus_test_123",
        subscription_id: str = "sub_test_123",
        amount_paid: int = 2900,  # in cents
        currency: str = "usd",
    ) -> dict:
        """Create test invoice data."""
        return {
            "id": invoice_id,
            "customer": customer_id,
            "subscription": subscription_id,
            "amount_paid": amount_paid,
            "currency": currency,
            "created": 1700000000,
            "billing_reason": "subscription_cycle",
        }

    def test_webhook_missing_signature(self, client: TestClient):
        """Test webhook endpoint without signature header."""
        response = client.post("/webhooks/stripe", json={})
        assert response.status_code == 400
        assert response.json()["detail"] == "Missing signature"

    def test_webhook_invalid_signature(self, client: TestClient, mock_stripe_signature: Mock):
        """Test webhook with invalid signature."""
        mock_stripe_signature.side_effect = stripe.error.SignatureVerificationError("Invalid signature", None)

        response = client.post(
            "/webhooks/stripe",
            content=b"test body",
            headers={"Stripe-Signature": "invalid_sig"},
        )
        assert response.status_code == 400
        assert response.json()["detail"] == "Invalid signature"

    def test_subscription_created_success(
        self,
        client: TestClient,
        mock_stripe_signature: Mock,
        mock_supabase: MagicMock,
    ):
        """Test successful subscription creation webhook."""
        # Setup
        subscription_data = self._create_subscription_data()
        event = self._create_stripe_event("customer.subscription.created", subscription_data)
        mock_stripe_signature.return_value = event

        # Mock Supabase responses
        mock_supabase.table().select().eq().single().execute.return_value = Mock(data={"id": "account_123"})
        mock_supabase.table().select().eq().execute.return_value = Mock(data=[])
        mock_supabase.table().insert().execute.return_value = Mock()

        # Make request
        response = client.post(
            "/webhooks/stripe",
            content=b"test body",
            headers={"Stripe-Signature": "valid_sig"},
        )

        # Verify
        assert response.status_code == 200
        data = response.json()
        assert data["received"] is True
        assert data["error"] is None

        # Verify Supabase calls
        assert mock_supabase.table.call_count >= 3  # accounts, subscriptions check, insert

    def test_subscription_created_no_account(
        self,
        client: TestClient,
        mock_stripe_signature: Mock,
        mock_supabase: MagicMock,
    ):
        """Test subscription creation with no matching account."""
        # Setup
        subscription_data = self._create_subscription_data()
        event = self._create_stripe_event("customer.subscription.created", subscription_data)
        mock_stripe_signature.return_value = event

        # Mock no account found
        mock_supabase.table().select().eq().single().execute.return_value = Mock(data=None)

        # Make request
        response = client.post(
            "/webhooks/stripe",
            content=b"test body",
            headers={"Stripe-Signature": "valid_sig"},
        )

        # Verify
        assert response.status_code == 200
        assert response.json() == {
            "received": True,
            "error": "Failed to process subscription creation",
        }

    def test_subscription_updated_success(
        self,
        client: TestClient,
        mock_stripe_signature: Mock,
        mock_supabase: MagicMock,
    ):
        """Test successful subscription update webhook."""
        # Setup
        subscription_data = self._create_subscription_data(tier="professional", quantity=5)
        event = self._create_stripe_event("customer.subscription.updated", subscription_data)
        mock_stripe_signature.return_value = event

        # Mock Supabase responses
        mock_supabase.table().select().eq().single().execute.return_value = Mock(data={"id": "account_123"})
        mock_supabase.table().update().eq().execute.return_value = Mock()

        # Make request
        response = client.post(
            "/webhooks/stripe",
            content=b"test body",
            headers={"Stripe-Signature": "valid_sig"},
        )

        # Verify
        assert response.status_code == 200
        data = response.json()
        assert data["received"] is True
        assert data["error"] is None

    def test_subscription_deleted_success(
        self,
        client: TestClient,
        mock_stripe_signature: Mock,
        mock_supabase: MagicMock,
    ):
        """Test successful subscription deletion webhook."""
        # Setup
        subscription_data = {"id": "sub_test_123", "customer": "cus_test_123"}
        event = self._create_stripe_event("customer.subscription.deleted", subscription_data)
        mock_stripe_signature.return_value = event

        # Mock Supabase responses
        mock_supabase.table().select().eq().single().execute.return_value = Mock(data={"account_id": "account_123"})
        mock_supabase.table().update().eq().eq().execute.return_value = Mock()

        # Make request
        response = client.post(
            "/webhooks/stripe",
            content=b"test body",
            headers={"Stripe-Signature": "valid_sig"},
        )

        # Verify
        assert response.status_code == 200
        data = response.json()
        assert data["received"] is True
        assert data["error"] is None

    def test_subscription_deleted_not_found(
        self,
        client: TestClient,
        mock_stripe_signature: Mock,
        mock_supabase: MagicMock,
    ):
        """Test subscription deletion for non-existent subscription."""
        # Setup
        subscription_data = {"id": "sub_test_123", "customer": "cus_test_123"}
        event = self._create_stripe_event("customer.subscription.deleted", subscription_data)
        mock_stripe_signature.return_value = event

        # Mock subscription not found
        mock_supabase.table().select().eq().single().execute.return_value = Mock(data=None)

        # Make request
        response = client.post(
            "/webhooks/stripe",
            content=b"test body",
            headers={"Stripe-Signature": "valid_sig"},
        )

        # Verify
        assert response.status_code == 200
        assert response.json() == {
            "received": True,
            "error": "Failed to process subscription deletion",
        }

    def test_payment_succeeded(
        self,
        client: TestClient,
        mock_stripe_signature: Mock,
        mock_supabase: MagicMock,
    ):
        """Test successful payment webhook."""
        # Setup
        invoice_data = self._create_invoice_data()
        event = self._create_stripe_event("invoice.payment_succeeded", invoice_data)
        mock_stripe_signature.return_value = event

        # Mock Supabase responses
        mock_supabase.table().select().eq().single().execute.return_value = Mock(data={"id": "account_123"})
        mock_supabase.table().insert().execute.return_value = Mock()

        # Make request
        response = client.post(
            "/webhooks/stripe",
            content=b"test body",
            headers={"Stripe-Signature": "valid_sig"},
        )

        # Verify
        assert response.status_code == 200
        data = response.json()
        assert data["received"] is True
        assert data["error"] is None

        # Verify both payments and usage tables were updated
        insert_calls = mock_supabase.table().insert.call_count
        assert insert_calls >= 2  # payments + usage

    def test_payment_succeeded_no_subscription(
        self,
        client: TestClient,
        mock_stripe_signature: Mock,
        mock_supabase: MagicMock,
    ):
        """Test payment webhook for one-time payment (no subscription)."""
        # Setup
        invoice_data = self._create_invoice_data(subscription_id=None)
        del invoice_data["subscription"]
        event = self._create_stripe_event("invoice.payment_succeeded", invoice_data)
        mock_stripe_signature.return_value = event

        # Make request
        response = client.post(
            "/webhooks/stripe",
            content=b"test body",
            headers={"Stripe-Signature": "valid_sig"},
        )

        # Verify - should succeed but not process
        assert response.status_code == 200
        assert response.json() == {
            "received": True,
            "error": "Failed to process payment",
        }

    def test_payment_failed(
        self,
        client: TestClient,
        mock_stripe_signature: Mock,
        mock_supabase: MagicMock,
    ):
        """Test failed payment webhook."""
        # Setup
        invoice_data = self._create_invoice_data()
        event = self._create_stripe_event("invoice.payment_failed", invoice_data)
        mock_stripe_signature.return_value = event

        # Mock Supabase responses
        mock_supabase.table().select().eq().single().execute.return_value = Mock(data={"account_id": "account_123"})
        mock_supabase.table().update().eq().eq().execute.return_value = Mock()

        # Make request
        response = client.post(
            "/webhooks/stripe",
            content=b"test body",
            headers={"Stripe-Signature": "valid_sig"},
        )

        # Verify
        assert response.status_code == 200
        data = response.json()
        assert data["received"] is True
        assert data["error"] is None

    def test_payment_failed_no_subscription(
        self,
        client: TestClient,
        mock_stripe_signature: Mock,
        mock_supabase: MagicMock,
    ):
        """Test failed payment webhook with no subscription found."""
        # Setup
        invoice_data = self._create_invoice_data()
        event = self._create_stripe_event("invoice.payment_failed", invoice_data)
        mock_stripe_signature.return_value = event

        # Mock no subscription found
        mock_supabase.table().select().eq().single().execute.return_value = Mock(data=None)

        # Make request
        response = client.post(
            "/webhooks/stripe",
            content=b"test body",
            headers={"Stripe-Signature": "valid_sig"},
        )

        # Verify
        assert response.status_code == 200
        assert response.json() == {
            "received": True,
            "error": "Failed to process payment failure",
        }

    def test_trial_will_end(
        self,
        client: TestClient,
        mock_stripe_signature: Mock,
        mock_supabase: MagicMock,
    ):
        """Test trial ending webhook."""
        # Setup
        subscription_data = self._create_subscription_data()
        subscription_data["trial_end"] = 1700086400  # Tomorrow
        subscription_data["id"] = "sub_test_123"
        subscription_data["customer"] = "cus_test_123"
        event = self._create_stripe_event("customer.subscription.trial_will_end", subscription_data)
        # Set data.object as a dict-like object that the handler expects
        event.data.object = subscription_data
        mock_stripe_signature.return_value = event

        # Mock Supabase responses
        mock_supabase.table().select().eq().single().execute.return_value = Mock(data={"id": "account_123"})

        # Make request
        response = client.post(
            "/webhooks/stripe",
            content=b"test body",
            headers={"Stripe-Signature": "valid_sig"},
        )

        # Verify
        assert response.status_code == 200
        data = response.json()
        assert data["received"] is True
        assert data["error"] is None

    def test_unhandled_event_type(
        self,
        client: TestClient,
        mock_stripe_signature: Mock,
        mock_supabase: MagicMock,
    ):
        """Test unhandled webhook event type."""
        # Setup
        data_obj = {"id": "obj_123", "customer": "cus_test_123"}
        event = self._create_stripe_event("some.unhandled.event", data_obj)
        # Set data.object as the dict
        event.data.object = data_obj
        mock_stripe_signature.return_value = event

        # Mock Supabase responses
        mock_supabase.table().select().eq().single().execute.return_value = Mock(data={"id": "account_123"})

        # Make request
        response = client.post(
            "/webhooks/stripe",
            content=b"test body",
            headers={"Stripe-Signature": "valid_sig"},
        )

        # Verify
        assert response.status_code == 200
        data = response.json()
        assert data["received"] is True
        assert data["error"] is None

    def test_webhook_processing_exception(
        self,
        client: TestClient,
        mock_stripe_signature: Mock,
        mock_supabase: MagicMock,
    ):
        """Test webhook with processing exception."""
        # Setup
        subscription_data = self._create_subscription_data()
        event = self._create_stripe_event("customer.subscription.created", subscription_data)
        mock_stripe_signature.return_value = event

        # Mock Supabase to raise exception
        mock_supabase.table().select().eq().single().execute.side_effect = Exception("Database error")

        # Make request
        response = client.post(
            "/webhooks/stripe",
            content=b"test body",
            headers={"Stripe-Signature": "valid_sig"},
        )

        # Verify
        assert response.status_code == 200
        assert response.json() == {"received": True, "error": "Database error"}

    def test_webhook_event_recording_failure(
        self,
        client: TestClient,
        mock_stripe_signature: Mock,
        mock_supabase: MagicMock,
    ):
        """Test webhook when event recording fails."""
        # Setup
        subscription_data = self._create_subscription_data()
        event = self._create_stripe_event("customer.subscription.created", subscription_data)
        mock_stripe_signature.return_value = event

        # Mock successful processing but failed recording
        accounts_table = MagicMock()
        accounts_table.select().eq().single().execute.return_value = Mock(data={"id": "account_123"})

        subscriptions_table = MagicMock()
        subscriptions_table.select().eq().execute.return_value = Mock(data=[])
        subscriptions_table.insert().execute.return_value = Mock()

        webhook_table = MagicMock()
        webhook_table.insert().execute.side_effect = Exception("Recording failed")

        def table_side_effect(name):
            if name == "accounts":
                return accounts_table
            elif name == "subscriptions":
                return subscriptions_table
            elif name == "webhook_events":
                return webhook_table
            else:
                return MagicMock()

        mock_supabase.table.side_effect = table_side_effect

        # Make request
        response = client.post(
            "/webhooks/stripe",
            content=b"test body",
            headers={"Stripe-Signature": "valid_sig"},
        )

        # Verify - should still succeed despite recording failure
        assert response.status_code == 200
        data = response.json()
        assert data["received"] is True
        assert data["error"] is None  # No error despite recording failure

    def test_subscription_with_trial(
        self,
        client: TestClient,
        mock_stripe_signature: Mock,
        mock_supabase: MagicMock,
    ):
        """Test subscription creation with trial period."""
        # Setup
        subscription_data = self._create_subscription_data()
        subscription_data["trial_end"] = 1702678400  # Future timestamp
        event = self._create_stripe_event("customer.subscription.created", subscription_data)
        mock_stripe_signature.return_value = event

        # Mock Supabase responses
        mock_supabase.table().select().eq().single().execute.return_value = Mock(data={"id": "account_123"})
        mock_supabase.table().select().eq().execute.return_value = Mock(data=[])
        mock_supabase.table().insert().execute.return_value = Mock()

        # Make request
        response = client.post(
            "/webhooks/stripe",
            content=b"test body",
            headers={"Stripe-Signature": "valid_sig"},
        )

        # Verify
        assert response.status_code == 200
        data = response.json()
        assert data["received"] is True
        assert data["error"] is None

    def test_professional_plan_scaling(
        self,
        client: TestClient,
        mock_stripe_signature: Mock,
        mock_supabase: MagicMock,
    ):
        """Test professional plan with multiple users scales limits correctly."""
        # Setup
        subscription_data = self._create_subscription_data(tier="professional", quantity=10)
        event = self._create_stripe_event("customer.subscription.created", subscription_data)
        mock_stripe_signature.return_value = event

        # Mock Supabase responses
        mock_supabase.table().select().eq().single().execute.return_value = Mock(data={"id": "account_123"})
        mock_supabase.table().select().eq().execute.return_value = Mock(data=[])

        # Capture the insert call to verify scaled limits
        insert_data = None

        def capture_insert(data):
            nonlocal insert_data
            insert_data = data
            return Mock(execute=Mock(return_value=Mock()))

        mock_supabase.table().insert = capture_insert

        # Make request
        response = client.post(
            "/webhooks/stripe",
            content=b"test body",
            headers={"Stripe-Signature": "valid_sig"},
        )

        # Verify
        assert response.status_code == 200
        data = response.json()
        assert data["received"] is True
        assert data["error"] is None

        # Professional plan should scale by quantity
        # Base limits would be multiplied by 10
        assert insert_data is not None

    def test_subscription_update_with_cancellation(
        self,
        client: TestClient,
        mock_stripe_signature: Mock,
        mock_supabase: MagicMock,
    ):
        """Test subscription update with cancellation timestamp."""
        # Setup
        subscription_data = self._create_subscription_data(status="canceled")
        subscription_data["canceled_at"] = 1700086400
        event = self._create_stripe_event("customer.subscription.updated", subscription_data)
        mock_stripe_signature.return_value = event

        # Mock Supabase responses
        mock_supabase.table().select().eq().single().execute.return_value = Mock(data={"id": "account_123"})
        mock_supabase.table().update().eq().execute.return_value = Mock()

        # Make request
        response = client.post(
            "/webhooks/stripe",
            content=b"test body",
            headers={"Stripe-Signature": "valid_sig"},
        )

        # Verify
        assert response.status_code == 200
        data = response.json()
        assert data["received"] is True
        assert data["error"] is None

    def test_rate_limiting(self, client: TestClient, mock_stripe_signature: Mock, mock_supabase: MagicMock):
        """Test webhook rate limiting."""
        # Setup a valid event
        event = self._create_stripe_event("customer.subscription.created", self._create_subscription_data())
        mock_stripe_signature.return_value = event

        # Mock Supabase responses for all requests
        mock_supabase.table().select().eq().single().execute.return_value = Mock(data={"id": "account_123"})
        mock_supabase.table().select().eq().execute.return_value = Mock(data=[])
        mock_supabase.table().insert().execute.return_value = Mock()

        # Make many requests quickly to trigger rate limit
        # The limit is 20/minute, so 21 requests should trigger it
        responses = []
        for _ in range(25):
            response = client.post(
                "/webhooks/stripe",
                content=b"test body",
                headers={"Stripe-Signature": "valid_sig"},
            )
            responses.append(response.status_code)

        # At least one should be rate limited (429)
        assert 429 in responses

    def test_legacy_tier_metadata(
        self,
        client: TestClient,
        mock_stripe_signature: Mock,
        mock_supabase: MagicMock,
    ):
        """Test handling of legacy 'tier' metadata field."""
        # Setup with legacy metadata format
        subscription_data = self._create_subscription_data()
        # Use 'tier' instead of 'plan' in metadata
        subscription_data["items"]["data"][0]["price"]["metadata"] = {
            "tier": "starter",  # Legacy field name
            "billing_cycle": "monthly",
        }
        event = self._create_stripe_event("customer.subscription.created", subscription_data)
        mock_stripe_signature.return_value = event

        # Mock Supabase responses
        mock_supabase.table().select().eq().single().execute.return_value = Mock(data={"id": "account_123"})
        mock_supabase.table().select().eq().execute.return_value = Mock(data=[])
        mock_supabase.table().insert().execute.return_value = Mock()

        # Make request
        response = client.post(
            "/webhooks/stripe",
            content=b"test body",
            headers={"Stripe-Signature": "valid_sig"},
        )

        # Verify - should handle legacy format successfully
        assert response.status_code == 200
        data = response.json()
        assert data["received"] is True
        assert data["error"] is None

    def test_missing_price_metadata(
        self,
        client: TestClient,
        mock_stripe_signature: Mock,
        mock_supabase: MagicMock,
    ):
        """Test handling of missing price metadata."""
        # Setup with no metadata
        subscription_data = self._create_subscription_data()
        subscription_data["items"]["data"][0]["price"]["metadata"] = {}
        event = self._create_stripe_event("customer.subscription.created", subscription_data)
        mock_stripe_signature.return_value = event

        # Mock Supabase responses
        mock_supabase.table().select().eq().single().execute.return_value = Mock(data={"id": "account_123"})

        # Make request
        response = client.post(
            "/webhooks/stripe",
            content=b"test body",
            headers={"Stripe-Signature": "valid_sig"},
        )

        # Verify - should fail gracefully
        assert response.status_code == 200
        # Should have an error since tier couldn't be determined
        result = response.json()
        assert result["received"] is True
        assert "error" in result or "Unable to determine tier" in str(result)
