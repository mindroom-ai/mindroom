"""Comprehensive HTTP API tests for Stripe route endpoints."""

from unittest.mock import MagicMock, Mock, patch

import pytest
import stripe
from backend.pricing import get_stripe_price_id
from fastapi.testclient import TestClient


class TestStripeRoutesEndpoints:
    """Test Stripe route endpoints via HTTP API."""

    @pytest.fixture
    def client(self) -> TestClient:
        """Create test client."""
        from main import app  # noqa: PLC0415

        return TestClient(app)

    @pytest.fixture
    def mock_supabase(self):
        """Mock Supabase client."""
        with patch("backend.routes.stripe_routes.ensure_supabase") as mock:
            sb = MagicMock()
            mock.return_value = sb
            yield sb

    @pytest.fixture(autouse=True)
    def account_pending_deletion(self):
        """Accounts are not pending deletion unless a test says so."""
        with patch("backend.services.provisioner_service.account_pending_deletion", return_value=False) as pending:
            yield pending

    @pytest.mark.parametrize("path", ["/stripe/checkout", "/stripe/portal"])
    def test_account_pending_deletion_cannot_change_billing(
        self,
        client: TestClient,
        mock_supabase: MagicMock,
        mock_stripe: Mock,
        mock_verify_user: Mock,
        account_pending_deletion: Mock,
        path: str,
    ):
        """Teardown cancels any subscription without refund, so a pending account must cancel the deletion first."""
        account_pending_deletion.return_value = True

        response = client.post(path, json={"tier": "pro", "billing_cycle": "monthly"})

        assert response.status_code == 409
        mock_stripe.checkout.Session.create.assert_not_called()
        mock_stripe.billing_portal.Session.create.assert_not_called()

    @pytest.fixture
    def mock_stripe(self):
        """Mock Stripe client."""
        with patch("backend.routes.stripe_routes.stripe") as mock:
            mock.api_key = "sk_test_mock"
            yield mock

    @pytest.fixture
    def mock_verify_user(self):
        """Mock user verification."""
        from main import app  # noqa: PLC0415
        from backend.deps import verify_user

        def override_verify_user():
            return {"account_id": "acc_test_123", "email": "test@example.com"}

        app.dependency_overrides[verify_user] = override_verify_user
        yield
        app.dependency_overrides.clear()

    def test_create_checkout_session_success(
        self, client: TestClient, mock_supabase: MagicMock, mock_stripe: Mock, mock_verify_user: Mock
    ):
        """Test creating checkout session successfully."""
        # Setup
        mock_supabase.table().select().eq().single().execute.return_value = Mock(
            data={"stripe_customer_id": "cus_test_123"}
        )

        # Mock pricing functions
        with (
            patch("backend.routes.stripe_routes.get_stripe_price_id", return_value="price_test_123"),
            patch("backend.routes.stripe_routes.is_trial_enabled_for_plan", return_value=False),
        ):
            mock_checkout_session = Mock()
            mock_checkout_session.url = "https://checkout.stripe.com/pay/cs_test_123"
            mock_stripe.checkout.Session.create.return_value = mock_checkout_session

            # Make request
            response = client.post("/stripe/checkout", json={"tier": "byok", "billing_cycle": "monthly"})

            # Verify
            assert response.status_code == 200
            data = response.json()
            assert data["url"] == "https://checkout.stripe.com/pay/cs_test_123"

    def test_create_customer_portal_session_success(
        self, client: TestClient, mock_supabase: MagicMock, mock_stripe: Mock, mock_verify_user: Mock
    ):
        """Test creating customer portal session successfully."""
        # Setup
        mock_supabase.table().select().eq().single().execute.return_value = Mock(
            data={"stripe_customer_id": "cus_test_123"}
        )

        mock_portal_session = Mock()
        mock_portal_session.url = "https://billing.stripe.com/session/test_123"
        mock_stripe.billing_portal.Session.create.return_value = mock_portal_session

        # Make request
        response = client.post("/stripe/portal")

        # Verify
        assert response.status_code == 200
        data = response.json()
        assert data["url"] == "https://billing.stripe.com/session/test_123"

    def test_create_customer_portal_no_customer(
        self, client: TestClient, mock_supabase: MagicMock, mock_stripe: Mock, mock_verify_user: Mock
    ):
        """Test creating portal session when no Stripe customer exists."""
        # Setup
        mock_supabase.table().select().eq().single().execute.return_value = Mock(data={"stripe_customer_id": None})

        # Make request
        response = client.post("/stripe/portal")

        # Verify
        assert response.status_code == 404
        assert "No Stripe customer" in response.json()["detail"]

    def test_create_customer_portal_stripe_error(
        self, client: TestClient, mock_supabase: MagicMock, mock_stripe: Mock, mock_verify_user: Mock
    ):
        """Test handling Stripe error when creating portal session."""
        # Setup
        mock_supabase.table().select().eq().single().execute.return_value = Mock(
            data={"stripe_customer_id": "cus_test_123"}
        )

        # Wrap the side_effect in a try-except in the route
        # The route should catch this Exception and return 500 with proper message
        mock_stripe.billing_portal.Session.create.side_effect = Exception("Portal error")

        # Make request
        response = client.post("/stripe/portal")

        # Verify
        assert response.status_code == 500
        assert "Failed to create" in response.json()["detail"]

    def test_create_checkout_new_customer(
        self, client: TestClient, mock_supabase: MagicMock, mock_stripe: Mock, mock_verify_user: Mock
    ):
        """Test creating checkout for new customer."""
        # Setup - no existing customer
        mock_supabase.table().select().eq().single().execute.return_value = Mock(data={"stripe_customer_id": None})

        # Mock pricing functions
        with (
            patch("backend.routes.stripe_routes.get_stripe_price_id", return_value="price_test_123"),
            patch("backend.routes.stripe_routes.is_trial_enabled_for_plan", return_value=False),
        ):
            # Mock customer creation
            mock_customer = Mock()
            mock_customer.id = "cus_new_123"
            mock_stripe.Customer.create.return_value = mock_customer

            # Mock checkout session
            mock_checkout_session = Mock()
            mock_checkout_session.url = "https://checkout.stripe.com/pay/cs_test_123"
            mock_stripe.checkout.Session.create.return_value = mock_checkout_session

            # Mock update
            mock_supabase.table().update().eq().execute.return_value = Mock()

            # Make request
            response = client.post("/stripe/checkout", json={"tier": "byok", "billing_cycle": "monthly"})

            # Verify
            assert response.status_code == 200
            data = response.json()
            assert data["url"] == "https://checkout.stripe.com/pay/cs_test_123"

    def test_checkout_uses_single_flat_price(
        self, client: TestClient, mock_supabase: MagicMock, mock_stripe: Mock, mock_verify_user: Mock
    ):
        """Test creating checkout for a flat-priced plan."""
        # Setup
        mock_supabase.table().select().eq().single().execute.return_value = Mock(
            data={"stripe_customer_id": "cus_test_123"}
        )

        # Mock pricing functions
        with (
            patch("backend.routes.stripe_routes.get_stripe_price_id", return_value="price_test_123"),
            patch("backend.routes.stripe_routes.is_trial_enabled_for_plan", return_value=False),
        ):
            mock_checkout_session = Mock()
            mock_checkout_session.url = "https://checkout.stripe.com/pay/cs_test_123"
            mock_stripe.checkout.Session.create.return_value = mock_checkout_session

            response = client.post("/stripe/checkout", json={"tier": "pro", "billing_cycle": "monthly"})

            # Verify
            assert response.status_code == 200
            data = response.json()
            assert data["url"] == "https://checkout.stripe.com/pay/cs_test_123"
            call_args = mock_stripe.checkout.Session.create.call_args.kwargs
            assert call_args["line_items"] == [{"price": "price_test_123", "quantity": 1}]

    @pytest.mark.parametrize(
        ("history", "expects_trial"),
        [
            ([], True),
            ([Mock(status="canceled", trial_start=None)], True),
            ([Mock(status="canceled", trial_start=1_780_000_000)], False),
        ],
    )
    def test_checkout_grants_a_trial_only_to_a_customer_who_never_had_one(
        self,
        client: TestClient,
        mock_supabase: MagicMock,
        mock_stripe: Mock,
        mock_verify_user: Mock,
        history: list[Mock],
        expects_trial: bool,
    ):
        """A cancelled trial stays in the customer's Stripe history, so checking out again starts without one."""
        mock_supabase.table().select().eq().single().execute.return_value = Mock(
            data={"stripe_customer_id": "cus_test_123"}
        )
        mock_stripe.Subscription.list.return_value.auto_paging_iter.return_value = history
        mock_stripe.checkout.Session.create.return_value = Mock(url="https://checkout.stripe.com/pay/cs_test_123")

        with patch("backend.routes.stripe_routes.get_stripe_price_id", return_value="price_test_123"):
            response = client.post("/stripe/checkout", json={"tier": "pro", "billing_cycle": "monthly"})

        assert response.status_code == 200
        mock_stripe.Subscription.list.assert_called_once_with(customer="cus_test_123", status="all", limit=100)
        params = mock_stripe.checkout.Session.create.call_args.kwargs
        assert ("trial_period_days" in params["subscription_data"]) is expects_trial

    @pytest.mark.parametrize(
        ("status", "opens_portal"),
        [
            ("active", True),
            ("trialing", True),
            ("past_due", True),
            ("unpaid", True),
            ("paused", True),
            ("incomplete", True),
            ("canceled", False),
            ("incomplete_expired", False),
        ],
    )
    def test_checkout_sends_a_customer_with_an_unended_subscription_to_the_portal(
        self,
        client: TestClient,
        mock_supabase: MagicMock,
        mock_stripe: Mock,
        mock_verify_user: Mock,
        status: str,
        opens_portal: bool,
    ):
        """A subscription Stripe can still bill or resume must be fixed in the portal, not duplicated by a new checkout."""
        mock_supabase.table().select().eq().single().execute.return_value = Mock(
            data={"stripe_customer_id": "cus_test_123"}
        )
        mock_stripe.Subscription.list.return_value.auto_paging_iter.return_value = [
            _subscription("sub_existing", status, "price_test_123")
        ]
        mock_stripe.billing_portal.Session.create.return_value = Mock(url="https://billing.stripe.com/session/x")
        mock_stripe.checkout.Session.create.return_value = Mock(url="https://checkout.stripe.com/pay/cs_test_123")

        with patch("backend.routes.stripe_routes.get_stripe_price_id", return_value="price_test_123"):
            response = client.post("/stripe/checkout", json={"tier": "hobby", "billing_cycle": "monthly"})

        assert response.status_code == 200
        if opens_portal:
            mock_stripe.checkout.Session.create.assert_not_called()
            assert response.json()["url"] == "https://billing.stripe.com/session/x"
        else:
            mock_stripe.billing_portal.Session.create.assert_not_called()
            assert response.json()["url"] == "https://checkout.stripe.com/pay/cs_test_123"

    @pytest.mark.parametrize("status", ["trialing", "active"])
    @pytest.mark.parametrize("configuration_exists", [True, False])
    def test_checkout_opens_the_upgrade_confirmation_for_a_subscriber(
        self,
        client: TestClient,
        mock_supabase: MagicMock,
        mock_stripe: Mock,
        mock_verify_user: Mock,
        configuration_exists: bool,
        status: str,
    ):
        """A subscriber choosing a larger plan confirms the switch in a portal configuration offering only that plan."""
        hobby, pro, pro_yearly = (
            get_stripe_price_id("hobby", "monthly"),
            get_stripe_price_id("pro", "monthly"),
            get_stripe_price_id("pro", "yearly"),
        )
        mock_supabase.table().select().eq().single().execute.return_value = Mock(
            data={"stripe_customer_id": "cus_test_123"}
        )
        mock_stripe.Subscription.list.return_value.auto_paging_iter.return_value = [
            _subscription("sub_existing", status, hobby)
        ]
        other = Mock(id="bpc_other", metadata={"mindroom_upgrade_prices": "price_a,price_b"})
        existing = Mock(id="bpc_pro", metadata={"mindroom_upgrade_prices": f"{pro},{pro_yearly}"})
        mock_stripe.billing_portal.Configuration.list.return_value.auto_paging_iter.return_value = (
            [other, existing] if configuration_exists else [other]
        )
        mock_stripe.billing_portal.Configuration.modify.return_value = Mock(id="bpc_pro")
        mock_stripe.billing_portal.Configuration.create.return_value = Mock(id="bpc_pro")
        mock_stripe.Price.retrieve.return_value = Mock(product="prod_subscription")
        mock_stripe.billing_portal.Session.create.return_value = Mock(url="https://billing.stripe.com/session/x")

        response = client.post("/stripe/checkout", json={"tier": "pro", "billing_cycle": "monthly"})

        assert response.json() == {"url": "https://billing.stripe.com/session/x"}
        mock_stripe.checkout.Session.create.assert_not_called()
        session = mock_stripe.billing_portal.Session.create.call_args.kwargs
        assert session["configuration"] == "bpc_pro"
        assert session["flow_data"]["type"] == "subscription_update_confirm"
        assert session["flow_data"]["subscription_update_confirm"] == {
            "subscription": "sub_existing",
            "items": [{"id": "si_existing", "price": pro, "quantity": 1}],
        }
        assert session["flow_data"]["after_completion"] == {
            "type": "redirect",
            "redirect": {"return_url": "https://app.mindroom.chat/dashboard/billing"},
        }
        if configuration_exists:
            mock_stripe.billing_portal.Configuration.create.assert_not_called()
            assert mock_stripe.billing_portal.Configuration.modify.call_args.args == ("bpc_pro",)
            params = mock_stripe.billing_portal.Configuration.modify.call_args.kwargs
        else:
            mock_stripe.billing_portal.Configuration.modify.assert_not_called()
            params = mock_stripe.billing_portal.Configuration.create.call_args.kwargs
            assert params["metadata"] == {"mindroom_upgrade_prices": f"{pro},{pro_yearly}"}
        assert params["features"]["subscription_update"] == {
            "enabled": True,
            "default_allowed_updates": ["price"],
            "products": [{"product": "prod_subscription", "prices": [pro, pro_yearly]}],
            "billing_cycle_anchor": "now",
            "proration_behavior": "always_invoice",
            "trial_update_behavior": "end_trial",
        }

    @pytest.mark.parametrize(
        ("status", "cancel", "current_tier", "tier", "billing_cycle"),
        [
            ("past_due", {}, "hobby", "pro", "monthly"),
            ("active", {"cancel_at": 1_800_000_000}, "hobby", "pro", "monthly"),
            ("active", {"cancel_at_period_end": True}, "hobby", "pro", "monthly"),
            ("active", {"schedule": "sub_sched_1"}, "hobby", "pro", "monthly"),
            ("active", {"collection_method": "send_invoice"}, "hobby", "pro", "monthly"),
            (
                "active",
                {"items": {"data": [{"id": "si_1", "price": {"id": "x"}}, {"id": "si_2", "price": {"id": "y"}}]}},
                "hobby",
                "pro",
                "monthly",
            ),
            ("active", {}, "pro", "hobby", "monthly"),
            ("trialing", {}, "hobby", "byok", "monthly"),
            ("active", {}, "hobby", "hobby", "yearly"),
        ],
    )
    def test_checkout_opens_the_portal_home_when_the_subscription_cannot_be_upgraded(
        self,
        client: TestClient,
        mock_supabase: MagicMock,
        mock_stripe: Mock,
        mock_verify_user: Mock,
        status: str,
        cancel: dict,
        current_tier: str,
        tier: str,
        billing_cycle: str,
    ):
        """An unpaid or cancelling subscription is settled first, and a downgrade would credit a spent AI budget."""
        mock_supabase.table().select().eq().single().execute.return_value = Mock(
            data={"stripe_customer_id": "cus_test_123"}
        )
        mock_stripe.Subscription.list.return_value.auto_paging_iter.return_value = [
            _subscription("sub_existing", status, get_stripe_price_id(current_tier, "monthly"), **cancel)
        ]
        mock_stripe.billing_portal.Session.create.return_value = Mock(url="https://billing.stripe.com/session/x")

        response = client.post("/stripe/checkout", json={"tier": tier, "billing_cycle": billing_cycle})

        assert response.json() == {"url": "https://billing.stripe.com/session/x"}
        session = mock_stripe.billing_portal.Session.create.call_args.kwargs
        assert "flow_data" not in session
        assert "configuration" not in session
        mock_stripe.billing_portal.Configuration.list.assert_not_called()

    def test_checkout_stripe_error(
        self, client: TestClient, mock_supabase: MagicMock, mock_stripe: Mock, mock_verify_user: Mock
    ):
        """Test handling Stripe error during checkout."""
        # Setup
        mock_supabase.table().select().eq().single().execute.return_value = Mock(
            data={"stripe_customer_id": "cus_test_123"}
        )

        # Mock pricing functions
        with (
            patch("backend.routes.stripe_routes.get_stripe_price_id", return_value="price_test_123"),
            patch("backend.routes.stripe_routes.is_trial_enabled_for_plan", return_value=False),
        ):
            mock_stripe.checkout.Session.create.side_effect = Exception("Checkout error")

            # Make request
            response = client.post("/stripe/checkout", json={"tier": "byok", "billing_cycle": "monthly"})

            # Verify
            assert response.status_code == 500
            assert "Failed to create checkout" in response.json()["detail"]

    def test_portal_account_not_found(
        self, client: TestClient, mock_supabase: MagicMock, mock_stripe: Mock, mock_verify_user: Mock
    ):
        """Test portal when account not found."""
        # Setup
        mock_supabase.table().select().eq().single().execute.return_value = Mock(data=None)

        # Make request
        response = client.post("/stripe/portal")

        # Verify - the route actually returns "No Stripe customer found"
        assert response.status_code == 404
        assert "No Stripe customer" in response.json()["detail"]

    def test_unauthorized_access(self, client: TestClient):
        """Test accessing endpoints without authentication."""
        response = client.post("/stripe/checkout", json={"tier": "byok", "billing_cycle": "monthly"})
        assert response.status_code == 401


def _subscription(subscription_id: str, status: str, price_id: str | None, **fields: object) -> stripe.Subscription:
    """Build a Stripe subscription with one item, read by attribute and by key like the real object."""
    return stripe.Subscription.construct_from(
        {
            "id": subscription_id,
            "status": status,
            "trial_start": None,
            "cancel_at": None,
            "cancel_at_period_end": False,
            "schedule": None,
            "collection_method": "charge_automatically",
            "items": {"data": [{"id": "si_existing", "price": {"id": price_id}}]},
            **fields,
        },
        "sk_test_mock",
    )
